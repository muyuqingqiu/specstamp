from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core import review_contract
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import load_events
from codex_sdlc.services import review_service, start_service
from test_contract_cli_regressions import _args, _ready_project, _snapshot, _write_package
from test_task_direct_start import _reviewed_project, _start_args
from test_task_plan_review_flow import _create_review, _submission


def test_t043_start_rejects_stale_or_invalid_inputs_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _paths, package = _ready_project(tmp_path, monkeypatch)
    monkeypatch.chdir(project)
    cases = [
        (
            "来源哈希失效",
            lambda value: value.update({"source_revision_sha256": "0" * 64}),
            "修订已经过期",
        ),
        (
            "审核引用失效",
            lambda value: value["reviews"].update({"requirement_split": "REV-999"}),
            "审核 REV 不是当前",
        ),
        (
            "清单哈希失效",
            lambda value: value["artifact_manifest"][0].update({"sha256": "0" * 64}),
            "应归档集合不完全一致",
        ),
    ]
    for label, mutate, message in cases:
        candidate = deepcopy(package)
        mutate(candidate)
        package_file = project / f"{label}.json"
        _write_package(package_file, candidate)
        before = _snapshot(project)
        with pytest.raises(SdlcError, match=message):
            start_service.start(_args(package_file))
        assert _snapshot(project) == before


def test_t043_review_rejects_same_task_identity_and_keeps_request_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "独立审核身份项目"
    project.mkdir()
    (project / "审核输入.json").write_text('{"id":"FR-001"}\n', encoding="utf-8")
    paths = build_paths(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "同一生产任务")
    request = review_contract.build_review_request(
        paths,
        review_id="REV-999",
        stage="task_plan",
        owner_id="REQ-001",
        input_paths=["审核输入.json"],
        required_checks=[],
    )

    with pytest.raises(SdlcError, match="不同"):
        review_contract.capture_review_result(
            request,
            _submission(request, status="passed", issues=[]),
        )


def test_t043_task_input_change_invalidates_review_before_runtime_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    task_file = requirement_root / "tasks/T-001.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task["goal"] = "输入变化后不能继续复用旧审核。"
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = load_events(paths)

    with pytest.raises(SdlcError):
        task_cmd.run(_start_args())

    assert not (requirement_root / "runtime/T-001").exists()
    assert load_events(paths) == before


def test_t043_task_runtime_interruption_rolls_back_directory_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    before = load_events(paths)

    from codex_sdlc.core import task_run

    def interrupt(_journal: dict[str, object]) -> None:
        raise OSError("故障注入：当前指针写入前中断")

    monkeypatch.setattr(task_run, "_before_current_commit", interrupt)
    with pytest.raises(SdlcError, match="开工事务"):
        task_cmd.run(_start_args())

    assert not (requirement_root / "runtime/T-001").exists()
    assert load_events(paths) == before


def test_t043_review_input_hash_change_requires_new_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    request = _create_review(project, monkeypatch)["request"]
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")

    with pytest.raises(SdlcError, match="失效"):
        review_service.submit_review(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request, status="passed", issues=[]),
        )
