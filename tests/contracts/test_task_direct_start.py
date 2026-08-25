from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state, load_events
from codex_sdlc.services import review_service
from test_task_plan_review_flow import (
    _create_review,
    _import_task_plan,
    _submission,
)


def _start_args() -> argparse.Namespace:
    return argparse.Namespace(
        first_id="REQ-001",
        second_id="T-001",
        done=False,
        note="",
        file=[],
        executed_commands=[],
        verify=[],
        verification_type="",
        verification_status="",
        change_report="",
        test_item=[],
        test_command=[],
        replace_test_command=[],
        clear_test_commands=False,
        test_script=[],
        replace_test_script=[],
        clear_test_scripts=False,
        manual_check=[],
    )


def _reviewed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "任务开发线程")
    monkeypatch.chdir(project)
    return project, requirement_root


def test_task_without_task_pack_creates_one_complete_reading_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert not list(requirement_root.glob("task-packs/**"))

    assert task_cmd.run(_start_args()) == 0

    run_root = requirement_root / "runtime/T-001/runs/0001"
    run = json.loads((run_root / "task-run.v1.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (run_root / "task-read-manifest.v1.json").read_text(encoding="utf-8")
    )
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    assert run["status"] == current["status"] == "reading"
    assert run["run_number"] == current["run_number"] == 1
    assert run["runner_thread_id"] == "任务开发线程"
    assert len(run["task_sha256"]) == 64
    assert len(run["task_review_sha256"]) == 64
    assert len(run["read_manifest_sha256"]) == 64
    assert run["code_baseline"]["project_path"] == str(project)
    assert len(run["code_baseline"]["repo_key"]) > 5
    assert len(run["code_baseline"]["branch_key"]) > 5
    assert len(run["code_baseline"]["worktree_key"]) > 5
    assert set(run["upstream_hashes"]) == {
        "requirement",
        "design",
        "reference_index",
        "project_rules",
        "dependencies",
        "predecessor_outputs",
    }
    assert manifest["task_file"] == "tasks/T-001.md"
    assert {item["id"] for item in manifest["references"]} == {
        "FR-001",
        "GR-001",
        "AC-001",
        "DES-001#architecture",
        "DATA-001",
        "MAT-001",
    }
    assert all("summary" not in item and "content" not in item for item in manifest["references"])
    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]["status"] == "doing"


@pytest.mark.parametrize("gate", ["review", "reference", "dependency", "thread"])
def test_task_start_gate_failure_has_no_runtime_or_task_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    if gate == "review":
        (requirement_root / "tasks/T-001.json").write_bytes(
            (requirement_root / "tasks/T-001.json").read_bytes() + b"\n"
        )
    elif gate == "reference":
        (requirement_root / "original/formal.v3.json").write_text("{}\n", encoding="utf-8")
    elif gate == "dependency":
        state = derive_state(paths)
        state["requirements"]["REQ-001"]["tasks"][0]["depends_on"] = ["T-999"]
        monkeypatch.setattr(task_cmd, "derive_state", lambda _paths: state)
    else:
        monkeypatch.delenv("CODEX_THREAD_ID")
    before = load_events(paths)

    with pytest.raises(SdlcError):
        task_cmd.run(_start_args())

    assert not (requirement_root / "runtime/T-001").exists()
    assert load_events(paths) == before


def test_task_start_write_failure_rolls_back_runtime_event_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    before = load_events(paths)
    event_backups_before = set(paths.backups_dir.glob("events-*.jsonl.bak"))

    from codex_sdlc.core import task_run

    def interrupt(_journal: dict[str, object]) -> None:
        raise OSError("故障注入：当前指针写入前中断")

    monkeypatch.setattr(task_run, "_before_current_commit", interrupt)
    with pytest.raises(SdlcError, match="开工事务"):
        task_cmd.run(_start_args())

    assert not (requirement_root / "runtime/T-001").exists()
    assert load_events(paths) == before
    assert set(paths.backups_dir.glob("events-*.jsonl.bak")) == event_backups_before
    assert derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]["status"] == "todo"
