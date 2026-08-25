from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.services import review_service
from test_task_plan_review_flow import _create_review, _import_task_plan, _submission
from test_task_planning_code_evidence import _project, _task_args, _write_task_submission


def test_task_plan_stage_only_advances_for_current_passed_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _import_task_plan(tmp_path, monkeypatch)
    paths = build_paths(project)

    before = derive_state(paths)["requirements"]["REQ-001"]
    assert before["status"] == "planning_tasks"
    assert before["task_plan_review_state"]["can_advance"] is False

    request = _create_review(project, monkeypatch)["request"]
    pending = derive_state(paths)["requirements"]["REQ-001"]
    assert pending["status"] == "planning_tasks"
    assert pending["task_plan_review_state"]["reviews"][0]["effective_status"] == "pending"

    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    ready = derive_state(paths)["requirements"]["REQ-001"]
    assert ready["status"] == "ready_for_development"
    assert ready["task_plan_review_state"]["can_advance"] is True

    (project / "与任务规划无关.txt").write_text("不影响任务审核。\n", encoding="utf-8")
    unrelated = derive_state(paths)["requirements"]["REQ-001"]
    assert unrelated["status"] == "ready_for_development"


def test_handwritten_event_cannot_skip_task_plan_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _import_task_plan(tmp_path, monkeypatch)
    events_path = project / ".codex-sdlc/events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "EVT-20260720-999999",
                    "event_type": "task_plan_review_passed",
                    "project_path": str(project),
                    "requirement_id": "REQ-001",
                    "task_id": None,
                    "created_at": "2026-07-20T22:40:00+08:00",
                    "source": "手工事件",
                    "summary": "不能越过可信审核登记",
                    "payload": {"status": "passed"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    requirement = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert requirement["status"] == "planning_tasks"
    assert requirement["task_plan_review_state"]["can_advance"] is False


def test_legacy_single_task_does_not_gain_an_extra_fixed_review(
    tmp_path: Path,
) -> None:
    project, _requirement_root = _project(tmp_path, git_project=False)
    events_path = project / ".codex-sdlc/events.jsonl"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "EVT-20260720-000002",
                    "event_type": "task_created",
                    "project_path": str(project),
                    "requirement_id": "REQ-001",
                    "task_id": "T-001",
                    "created_at": "2026-07-20T22:45:00+08:00",
                    "source": "旧任务入口",
                    "summary": "创建单条旧任务",
                    "payload": {
                        "title": "保持旧任务可读",
                        "summary": "旧任务不增加整套任务固定审核。",
                        "status": "todo",
                        "depends_on": [],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    requirement = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert requirement["status"] == "active"
    assert "task_plan_review_state" not in requirement


def test_tasks_command_points_to_the_single_task_plan_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, requirement_root = _project(tmp_path)
    submission = _write_task_submission(tmp_path / "命令输出", requirement_root)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")

    assert run_tasks_finalize(_task_args(submission)) == 0
    output = capsys.readouterr().out

    assert "整套任务独立审核" in output
    assert "task_plan" in output
    assert output.count("整套任务独立审核") == 1
