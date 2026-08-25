from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.formal_manifest_contract import (
    build_document_first_formal_package,
)
from codex_sdlc.core.state import (
    append_event,
    derive_state,
    load_events,
    refresh_materialized_state,
)
from codex_sdlc.services import start_service
from test_cli_v1 import run_cli
from test_integrated_design_review_flow import (
    _create as _create_design_review,
    _project_with_summary,
    _submit as _submit_design_review,
)
from test_requirement_confirmation_contract import (
    _passed_project as _requirement_review_passed_project,
)


def _snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory", "")
    return result


def _args(package_file: Path, **overrides: str) -> argparse.Namespace:
    values = {
        "_sdlc_start_mode": "formal",
        "package_file": str(package_file),
        "description": "",
        "draft_id": "",
        "source_index": "",
        "requirement_facts": "",
        "design_facts": "",
        "model_review": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _write_package(path: Path, package: dict[str, object]) -> None:
    path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _ready_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object, dict[str, object]]:
    project, paths = _project_with_summary(tmp_path, monkeypatch)
    review = _create_design_review(paths, monkeypatch)
    _submit_design_review(paths, review["request"], monkeypatch)
    refresh_materialized_state(paths)
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "start_ready"
    package = build_document_first_formal_package(paths, "DRAFT-001")
    return project, paths, package


def test_production_start_preflight_success_rejections_and_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    package_file = tmp_path / "formal.v3.json"
    _write_package(package_file, package)
    monkeypatch.chdir(project)
    baseline = _snapshot(project)
    source_archives = {
        str(item["archive_path"]): (
            paths.draft_dir("DRAFT-001") / str(item["source_path"])
        ).read_bytes()
        for item in package["artifact_manifest"]
    }

    with pytest.raises(SdlcError, match="旧位置说明"):
        start_service.start(_args(package_file, description="旧位置说明"))
    assert _snapshot(project) == baseline

    cli_result = run_cli(
        ["start", "旧位置说明", "--file", str(package_file)],
        cwd=project,
    )
    assert cli_result.returncode == 1
    assert "旧位置说明" in cli_result.stderr
    assert "只保留 start --file" in cli_result.stderr
    assert _snapshot(project) == baseline

    mutations = [
        (
            "无显式来源",
            lambda value: value.pop("source_draft_id"),
            "source_draft_id",
        ),
        (
            "错误来源",
            lambda value: value.update({"source_draft_id": "DRAFT-999"}),
            "DRAFT 不存在",
        ),
        (
            "旧修订哈希",
            lambda value: value.update({"source_revision_sha256": "0" * 64}),
            "修订已经过期",
        ),
        (
            "清单字段漂移",
            lambda value: value["artifact_manifest"][0].update(
                {"sha256": "0" * 64}
            ),
            "应归档集合不完全一致",
        ),
        (
            "审核引用漂移",
            lambda value: value["reviews"].update(
                {"requirement_split": "REV-999"}
            ),
            "审核 REV 不是当前",
        ),
        (
            "待确认问题",
            lambda value: value.update({"open_questions": ["仍有待确认项"]}),
            "open_questions",
        ),
    ]
    for label, mutate, message in mutations:
        candidate = deepcopy(package)
        mutate(candidate)
        candidate_file = tmp_path / f"{label}.json"
        _write_package(candidate_file, candidate)
        before = _snapshot(project)
        with pytest.raises(SdlcError, match=message):
            start_service.start(_args(candidate_file))
        assert _snapshot(project) == before

    for field_name in (
        "draft_id",
        "source_index",
        "requirement_facts",
        "design_facts",
        "model_review",
    ):
        with pytest.raises(SdlcError, match="不能同时使用"):
            start_service.start(_args(package_file, **{field_name: "旧输入"}))
        assert _snapshot(project) == baseline

    facts_file = tmp_path / "facts-formal.v3.json"
    _write_package(
        facts_file,
        {
            "formal_contract_version": "formal.v3",
            "fact_bundle": {"source_index_file": "source-index.json"},
        },
    )
    with pytest.raises(SdlcError, match="只保留历史档案读取和体检"):
        start_service.start(_args(facts_file))
    assert _snapshot(project) == baseline

    no_file_args = _args(package_file)
    no_file_args.package_file = ""
    with pytest.raises(SdlcError, match="必须使用 start --file"):
        start_service.start(no_file_args)
    assert _snapshot(project) == baseline

    before_events = load_events(paths)
    assert start_service.start(_args(package_file)) == 0
    first_output = capsys.readouterr().out
    assert "已创建正式需求：REQ-001" in first_output
    assert "来源 DRAFT：DRAFT-001" in first_output
    assert "需求包：.codex-sdlc/requirements/REQ-001" in first_output
    assert "正式事件、需求目录和状态投影已经完整提交" in first_output

    target = paths.requirements_dir / "REQ-001"
    assert target.is_dir()
    status = json.loads((target / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "active"
    assert status["requirement_id"] == "REQ-001"
    assert status["source_draft_id"] == "DRAFT-001"
    events_after_first = load_events(paths)
    assert len(events_after_first) == len(before_events) + 2
    assert len(
        [
            event
            for event in events_after_first
            if event.get("event_type") == "requirement_created"
            and event.get("requirement_id") == "REQ-001"
        ]
    ) == 1
    assert len(
        [
            event
            for event in events_after_first
            if event.get("event_type") == "draft_started"
            and event.get("requirement_id") == "REQ-001"
            and event.get("payload", {}).get("draft_id") == "DRAFT-001"
        ]
    ) == 1
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "started"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == "REQ-001"
    assert "REQ-001" in state["requirements"]
    for archive_path, source_content in source_archives.items():
        assert (target / archive_path).read_bytes() == source_content

    original_after_first = _snapshot(target / "original")
    completed_root = paths.sdlc_dir / "start-transactions" / "completed"
    receipts_after_first = _snapshot(completed_root)
    project_after_first = _snapshot(project)

    assert start_service.start(_args(package_file)) == 0
    second_output = capsys.readouterr().out
    assert "已创建正式需求：REQ-001" in second_output
    assert "相同正式建档事务已经完成，已返回原结果" in second_output
    assert _snapshot(project) == project_after_first
    assert _snapshot(target / "original") == original_after_first
    assert _snapshot(completed_root) == receipts_after_first
    assert load_events(paths) == events_after_first

    assert [path.name for path in paths.requirements_dir.iterdir()] == ["REQ-001"]
    assert not list(paths.start_staging_root.iterdir())
    assert not list(paths.draft_staging_dir("DRAFT-001").iterdir())


def test_reference_and_review_hash_drift_fail_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    package_file = tmp_path / "formal.v3.json"
    _write_package(package_file, package)
    monkeypatch.chdir(project)
    baseline = _snapshot(project)

    review_item = next(
        item
        for item in package["artifact_manifest"]
        if item["artifact_type"] == "integrated_design_review_input"
    )
    review_path = paths.draft_dir("DRAFT-001") / review_item["source_path"]
    original_review = review_path.read_bytes()
    review_path.write_bytes(original_review + b" ")
    drifted = _snapshot(project)
    with pytest.raises(SdlcError, match="start_ready|哈希|审核"):
        start_service.start(_args(package_file))
    assert _snapshot(project) == drifted
    review_path.write_bytes(original_review)
    assert _snapshot(project) == baseline

    index_path = paths.draft_artifact_index_file("DRAFT-001")
    index_document = json.loads(index_path.read_text(encoding="utf-8"))
    original_index = index_path.read_bytes()
    index_document["artifacts"][0]["sha256"] = "0" * 64
    index_path.write_text(
        json.dumps(index_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    drifted = _snapshot(project)
    with pytest.raises(SdlcError, match="哈希|索引|artifact"):
        start_service.start(_args(package_file))
    assert _snapshot(project) == drifted
    index_path.write_bytes(original_index)
    assert _snapshot(project) == baseline


def test_event_backed_review_input_drift_keeps_revision_error_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    review_item = next(
        item
        for item in package["artifact_manifest"]
        if item["artifact_type"] == "integrated_design_review_input"
    )
    review_path = paths.draft_dir("DRAFT-001") / review_item["source_path"]
    review_path.write_bytes(review_path.read_bytes() + b"\n")
    monkeypatch.chdir(project)
    before = _snapshot(project)
    old_revision = deepcopy(package)
    old_revision["source_revision_sha256"] = "0" * 64
    old_revision_file = tmp_path / "旧修订与审核输入漂移.json"
    _write_package(old_revision_file, old_revision)

    with pytest.raises(
        SdlcError,
        match="formal.v3 引用的 DRAFT 修订已经过期",
    ):
        start_service.start(_args(old_revision_file))
    assert _snapshot(project) == before

    current_revision_file = tmp_path / "当前修订与审核输入漂移.json"
    _write_package(current_revision_file, package)
    with pytest.raises(
        SdlcError,
        match="产物索引哈希与真实文件不一致.*审核输入",
    ):
        start_service.start(_args(current_revision_file))
    assert _snapshot(project) == before

    index_path = paths.draft_artifact_index_file("DRAFT-001")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["artifacts"][0]["sha256"] = "f" * 64
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    all_drift = deepcopy(old_revision)
    all_drift["artifact_manifest"].pop()
    all_drift_file = tmp_path / "旧修订与三类后续漂移.json"
    _write_package(all_drift_file, all_drift)
    all_drift_before = _snapshot(project)
    with pytest.raises(
        SdlcError,
        match="formal.v3 引用的 DRAFT 修订已经过期",
    ):
        start_service.start(_args(all_drift_file))

    assert _snapshot(project) == all_drift_before


def test_event_backed_independent_gates_still_reject_before_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_root = tmp_path / "已就绪"
    ready_root.mkdir()
    ready_project, ready_paths, package = _ready_project(ready_root, monkeypatch)
    old_revision = deepcopy(package)
    old_revision["source_revision_sha256"] = "0" * 64
    package_file = tmp_path / "独立门禁使用的旧修订包.json"
    _write_package(package_file, old_revision)

    append_event(
        ready_paths,
        event_type="draft_started",
        source="start 前置门禁合同测试",
        summary="DRAFT-001 已由既有正式需求消费",
        requirement_id="REQ-999",
        payload={
            "draft_id": "DRAFT-001",
            "started_requirement_id": "REQ-999",
        },
    )
    monkeypatch.chdir(ready_project)
    started_before = _snapshot(ready_project)
    with pytest.raises(SdlcError) as started_error:
        start_service.start(_args(package_file))
    assert "已经完成过正式建档" in str(started_error.value)
    assert "修订已经过期" not in str(started_error.value)
    assert _snapshot(ready_project) == started_before

    needs_fix_root = tmp_path / "审核返修"
    needs_fix_root.mkdir()
    needs_fix_project, needs_fix_paths = _project_with_summary(
        needs_fix_root,
        monkeypatch,
    )
    review = _create_design_review(needs_fix_paths, monkeypatch)
    _submit_design_review(
        needs_fix_paths,
        review["request"],
        monkeypatch,
        status="needs_fix",
    )
    assert (
        derive_state(needs_fix_paths)["drafts"]["DRAFT-001"][
            "_integrated_design_review_state"
        ]["reviews"][0]["recorded_status"]
        == "needs_fix"
    )
    monkeypatch.chdir(needs_fix_project)
    needs_fix_before = _snapshot(needs_fix_project)
    with pytest.raises(SdlcError) as needs_fix_error:
        start_service.start(_args(package_file))
    assert "start_ready" in str(needs_fix_error.value)
    assert "修订已经过期" not in str(needs_fix_error.value)
    assert _snapshot(needs_fix_project) == needs_fix_before

    unconfirmed_root = tmp_path / "等待确认"
    unconfirmed_root.mkdir()
    unconfirmed_project, unconfirmed_paths, _material, _request = (
        _requirement_review_passed_project(unconfirmed_root, monkeypatch)
    )
    confirmation = derive_state(unconfirmed_paths)["drafts"]["DRAFT-001"][
        "_requirement_confirmation_state"
    ]
    assert confirmation["can_advance"] is False
    monkeypatch.chdir(unconfirmed_project)
    unconfirmed_before = _snapshot(unconfirmed_project)
    with pytest.raises(SdlcError) as unconfirmed_error:
        start_service.start(_args(package_file))
    assert "start_ready" in str(unconfirmed_error.value)
    assert "修订已经过期" not in str(unconfirmed_error.value)
    assert _snapshot(unconfirmed_project) == unconfirmed_before


def test_cli_rejects_legacy_assembly_options_without_business_write(
    tmp_path: Path,
) -> None:
    project = tmp_path / "cli-project"
    project.mkdir()
    (project / "README.md").write_text("# CLI 合同项目\n", encoding="utf-8")
    assert run_cli(["init-basic"], cwd=project).returncode == 0
    assert run_cli(["status"], cwd=project).returncode == 0
    before = _snapshot(project / ".codex-sdlc")

    result = run_cli(["start", "--draft", "DRAFT-001"], cwd=project)

    assert result.returncode == 1
    assert "必须使用 start --file" in result.stderr
    after = _snapshot(project / ".codex-sdlc")
    assert after == before
