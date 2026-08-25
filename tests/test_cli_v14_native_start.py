from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys

import pytest


TESTS_DIR = Path(__file__).resolve().parent
CONTRACTS_DIR = TESTS_DIR / "contracts"
sys.path.insert(0, str(CONTRACTS_DIR))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.start_transaction import commit_prepared_start
from codex_sdlc.core.state import append_event, derive_state, load_events
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
)
from codex_sdlc.services import start_service
from test_cli_v1 import init_demo_repo, run_cli_raw
from test_contract_cli_regressions import _ready_project


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory", "")
    return result


@pytest.fixture(scope="module")
def document_first_template(tmp_path_factory: pytest.TempPathFactory):
    """只构建一次真实 start_ready DRAFT，其他用例复制 prepared 快照后独立提交。"""

    root = tmp_path_factory.mktemp("cli-v14-document-first")
    patcher = pytest.MonkeyPatch()
    try:
        live_project, live_paths, package = _ready_project(root, patcher)
        prepared = start_service.prepare_document_first_start(live_paths, package)
    finally:
        patcher.undo()
    snapshot = root / "prepared-template"
    shutil.copytree(live_project, snapshot)
    return {
        "snapshot": snapshot,
        "live_project": live_project,
        "live_paths": live_paths,
        "package": package,
        "prepared": prepared,
    }


def _clone_prepared(
    tmp_path: Path,
    document_first_template,
) -> tuple[Path, ProjectPaths, dict[str, object]]:
    project = tmp_path / "project"
    shutil.copytree(document_first_template["snapshot"], project)
    paths = ProjectPaths(project)
    staging = next(paths.start_staging_root.glob("start-*"))

    # 模板复制后项目根发生变化。历史事件中的结构化 project_path 必须同步，
    # prepared 事务也要绑定更新后的真实事件字节，不能靠目录名继续提交。
    events = load_events(paths)
    for event in events:
        event["project_path"] = str(project)
    event_bytes = b"".join(
        (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        for event in events
    )
    paths.events_file.write_bytes(event_bytes)
    transaction_path = staging / "start-transaction.json"
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    transaction["event_file_size"] = len(event_bytes)
    transaction["event_count"] = len(events)
    transaction["event_sha256"] = sha256_bytes(event_bytes)
    transaction_path.write_text(canonical_json_text(transaction), encoding="utf-8")
    prepared = {
        **document_first_template["prepared"],
        "staging_directory": str(staging),
    }
    return project, paths, prepared


def test_start_position_description_is_rejected_without_writing(
    tmp_path: Path,
) -> None:
    project = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project).returncode == 0
    events_before = (project / ".codex-sdlc/events.jsonl").read_bytes()

    result = run_cli_raw(["start", "旧位置说明"], cwd=project)

    assert result.returncode == 1
    assert "必须使用 start --file" in result.stderr
    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == events_before
    assert not list((project / ".codex-sdlc/requirements").glob("REQ-*"))


def test_facts_formal_v3_direct_write_is_rejected(tmp_path: Path) -> None:
    project = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project).returncode == 0
    package = tmp_path / "facts-formal.v3.json"
    package.write_text(
        json.dumps(
            {
                "formal_contract_version": "formal.v3",
                "source_draft_id": "DRAFT-001",
                "title": "旧 facts 正式包",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    events_before = (project / ".codex-sdlc/events.jsonl").read_bytes()

    result = run_cli_raw(["start", "--file", str(package)], cwd=project)

    assert result.returncode == 1
    assert "只接受 document-first.v1 正式包" in result.stderr
    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == events_before
    assert not list((project / ".codex-sdlc/requirements").glob("REQ-*"))


def test_document_first_cli_start_and_repeat_return_same_result(
    document_first_template,
) -> None:
    project = document_first_template["live_project"]
    paths = document_first_template["live_paths"]
    package_file = project / "document-first.formal.v3.json"
    package_file.write_text(
        json.dumps(
            document_first_template["package"],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    first = run_cli_raw(["start", "--file", str(package_file)], cwd=project)
    original = _file_snapshot(paths.requirements_dir / "REQ-001/original")
    second = run_cli_raw(["start", "--file", str(package_file)], cwd=project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "已创建正式需求：REQ-001" in first.stdout
    assert "相同正式建档事务已经完成，已返回原结果" in second.stdout
    assert _file_snapshot(paths.requirements_dir / "REQ-001/original") == original
    created = [
        event
        for event in load_events(paths)
        if event.get("event_type") == "requirement_created"
        and event.get("requirement_id") == "REQ-001"
    ]
    assert len(created) == 1


def test_prepared_contains_document_first_versions(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    staging = Path(str(prepared["staging_directory"]))

    for name in ("requirement", "design", "test-matrix"):
        current = json.loads(
            (staging / f"effective/{name}.current.json").read_text(
                encoding="utf-8"
            )
        )
        version = json.loads(
            (staging / f"versions/{name}.v1.json").read_text(encoding="utf-8")
        )
        comparable = deepcopy(current)
        comparable["is_current"] = False
        assert current["requirement_id"] == "REQ-001"
        assert current["is_current"] is True
        assert version["is_current"] is False
        assert canonical_sha256(comparable) == canonical_sha256(version)
    assert not list(paths.requirements_dir.glob("REQ-*"))


def test_formal_original_matches_manifest_before_and_after_commit(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    original_before = _file_snapshot(staging / "original")
    formal = json.loads(
        (staging / "original/formal.v3.json").read_text(encoding="utf-8")
    )
    for item in formal["artifact_manifest"]:
        archive = staging / item["archive_path"]
        assert sha256_bytes(archive.read_bytes()) == item["sha256"]

    result = commit_prepared_start(paths, prepared)

    target = paths.requirements_dir / str(result["target_directory"])
    assert _file_snapshot(target / "original") == original_before
    assert json.loads((target / "status.json").read_text(encoding="utf-8"))[
        "status"
    ] == "active"


def test_completed_transaction_keeps_formal_reference_index(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )

    result = commit_prepared_start(paths, prepared)

    target = paths.requirements_dir / str(result["target_directory"])
    reference = json.loads(
        (target / "reference-index.v1.json").read_text(encoding="utf-8")
    )
    assert reference["requirement_id"] == "REQ-001"
    assert reference["entries"]
    for entry in reference["entries"].values():
        assert entry["path"].startswith("original/")
        assert len(entry["sha256"]) == 64


def test_completed_transaction_projects_requirement_and_started_draft(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )

    commit_prepared_start(paths, prepared)
    state = derive_state(paths)

    assert "REQ-001" in state["requirements"]
    assert state["drafts"]["DRAFT-001"]["status"] == "started"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == "REQ-001"
    assert paths.database_file.is_file()
    assert paths.current_md.is_file()


def test_same_prepared_submission_is_idempotent(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )

    first = commit_prepared_start(paths, prepared)
    events_after_first = paths.events_file.read_bytes()
    second = commit_prepared_start(paths, prepared)

    assert second["transaction_id"] == first["transaction_id"]
    assert second["requirement_id"] == first["requirement_id"]
    assert second["formal_directory"] == first["formal_directory"]
    assert second["idempotent"] is True
    assert paths.events_file.read_bytes() == events_after_first


def test_completed_receipt_remains_idempotent_after_later_event(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    first = commit_prepared_start(paths, prepared)
    append_event(
        paths,
        event_type="requirement_metadata_updated",
        source="sdlc-test",
        summary="验证完成回执只固定自己的事件区间",
        requirement_id="REQ-001",
        payload={},
    )
    events_after_later_write = paths.events_file.read_bytes()

    second = commit_prepared_start(paths, prepared)

    assert second["transaction_id"] == first["transaction_id"]
    assert second["idempotent"] is True
    assert paths.events_file.read_bytes() == events_after_later_write


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_events_append",
        "after_directory_commit",
        "during_projection_refresh",
        "after_integrity_check",
    ],
)
def test_ordinary_commit_failure_rolls_back_events_directory_and_state(
    tmp_path: Path,
    document_first_template,
    fault_point: str,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    before_events = paths.events_file.read_bytes()
    before_state = derive_state(paths)

    def fail(point: str, _transaction_path: Path) -> None:
        if point == fault_point:
            raise RuntimeError(fault_point)

    with pytest.raises(SdlcError, match="已完整回滚"):
        commit_prepared_start(paths, prepared, fault_injector=fail)

    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
    assert not list(paths.start_staging_root.glob("start-*"))
    assert not list(
        (paths.sdlc_dir / "start-transactions/active").iterdir()
    )
    state = derive_state(paths)
    assert (
        state["drafts"]["DRAFT-001"]["status"]
        == before_state["drafts"]["DRAFT-001"]["status"]
    )
    assert "REQ-001" not in state["requirements"]


def test_target_conflict_preserves_existing_original_and_prepared(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    prepared_before = _tree_snapshot(staging)
    target = paths.requirements_dir / "REQ-001-conflict"
    (target / "original").mkdir(parents=True)
    protected = target / "original/protected.txt"
    protected.write_bytes(b"protected-original")

    with pytest.raises(SdlcError, match="正式需求目标已经存在|编号冲突"):
        commit_prepared_start(paths, prepared)

    assert protected.read_bytes() == b"protected-original"
    assert _tree_snapshot(staging) == prepared_before
    assert not list(
        (paths.sdlc_dir / "start-transactions/active").iterdir()
    )


def test_event_boundary_change_rejects_commit_without_deleting_prepared(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    prepared_before = _tree_snapshot(staging)
    with paths.events_file.open("ab") as handle:
        handle.write(b'{"event_id":"EVT-OUTSIDE"}\n')

    with pytest.raises(SdlcError, match="事件边界"):
        commit_prepared_start(paths, prepared)

    assert _tree_snapshot(staging) == prepared_before
    assert not list(paths.requirements_dir.glob("REQ-*"))


def test_document_first_status_and_versions_do_not_depend_on_markdown_titles(
    tmp_path: Path,
    document_first_template,
) -> None:
    _project, paths, prepared = _clone_prepared(
        tmp_path,
        document_first_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    traceability = staging / "traceability.md"
    traceability.write_text("# 被修改的展示标题\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="哈希|生成文件清单"):
        commit_prepared_start(paths, prepared)

    assert not list(paths.requirements_dir.glob("REQ-*"))
    assert staging.is_dir()


def test_cli_recovers_start_transaction_before_identity_and_auto_backup() -> None:
    cli_source = (
        Path(__file__).resolve().parents[1] / "src/codex_sdlc/cli.py"
    ).read_text(encoding="utf-8")
    main_body = cli_source.split("def main(", 1)[1]

    recovery = main_body.index("run_start_transaction_recovery()")
    identity = main_body.index("run_identity_guard(args)")
    backup = main_body.index('run_auto_backup(args, phase="before")')

    # 恢复失败必须直接进入统一 SdlcError 出口，不能先让身份检查或备份读取半成品。
    assert recovery < identity < backup
