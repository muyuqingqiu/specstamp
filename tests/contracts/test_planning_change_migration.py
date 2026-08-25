from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from codex_sdlc.commands import add_cmd, change_cmd, grill_cmd, plan_cmd
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths


COMMAND_MODULES = (plan_cmd, add_cmd, change_cmd, grill_cmd)


def _imports(module: object) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_planning_and_change_commands_do_not_import_old_task_pack_writers() -> None:
    forbidden = {
        "codex_sdlc.core.task_pack",
        "codex_sdlc.core.task_pack_contract",
        "codex_sdlc.commands.task_cmd",
    }

    for module in COMMAND_MODULES:
        assert _imports(module).isdisjoint(forbidden), module.__name__


def test_commands_no_longer_recommend_brief_or_invalidate_task_pack() -> None:
    for module in COMMAND_MODULES:
        source = inspect.getsource(module)
        assert "$sdlc-brief" not in source, module.__name__
        assert "mark_requirement_task_packs_stale" not in source, module.__name__

    accept_source = inspect.getsource(change_cmd.run_accept)
    assert "accept_change_package" in accept_source
    assert "prepare_requirement_for_work" not in accept_source


def test_grill_uses_task_plan_review_and_current_task_run(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "brief" not in grill_cmd.GRILL_MODES
    assert "task_plan" in grill_cmd.GRILL_MODES

    requirement = {
        "requirement_id": "REQ-001",
        "folder_name": "REQ-001-任务规划",
        "task_plan_review_state": {
            "reviews": [
                {
                    "review_id": "REV-003",
                    "is_current": True,
                    "effective_status": "passed",
                }
            ]
        },
    }
    planning = grill_cmd.build_grill_association(
        None,
        requirement=requirement,
        task=None,
        mode="task_plan",
    )
    assert planning == {
        "association_type": "task_plan_review",
        "task_plan_review_id": "REV-003",
        "task_plan_review_status": "passed",
    }

    task = {"task_id": "T-001"}
    monkeypatch.setattr(
        "codex_sdlc.core.task_run.load_task_run_context",
        lambda *_args, **_kwargs: {
            "run": {"run_number": 2, "status": "active"},
            "run_path": Path("/tmp/task-run.v1.json"),
        },
    )
    runtime = grill_cmd.build_grill_association(
        object(),
        requirement=requirement,
        task=task,
        mode="task",
    )
    assert runtime["association_type"] == "task_run"
    assert runtime["task_run_number"] == 2
    assert runtime["task_run_status"] == "active"


def test_runtime_grill_rejects_missing_task_run(monkeypatch: pytest.MonkeyPatch) -> None:
    requirement = {"requirement_id": "REQ-001"}
    task = {"task_id": "T-001"}

    def missing(*_args, **_kwargs):
        raise SdlcError("当前任务没有运行轮次。", exit_code=1)

    monkeypatch.setattr("codex_sdlc.core.task_run.load_task_run_context", missing)
    with pytest.raises(SdlcError, match="当前 task-run"):
        grill_cmd.build_grill_association(
            object(),
            requirement=requirement,
            task=task,
            mode="task",
        )


def test_mutable_plan_syncs_task_plan_and_coverage_without_task_pack(tmp_path: Path) -> None:
    paths = build_paths(tmp_path)
    requirement = {
        "requirement_id": "REQ-001",
        "folder_name": "REQ-001-规划",
        "structured": {
            "requirement_points": [{"id": "FR-001"}],
            "acceptance_points": [{"id": "AC-001"}],
        },
        "tasks": [
            {
                "task_id": "T-001",
                "depends_on": [],
                "coverage_points": ["FR-001"],
                "coverage_acceptance": ["AC-001"],
                "design_refs": ["DATA-001"],
                "coverage_change_ids": [],
            },
            {
                "task_id": "T-002",
                "depends_on": ["T-001"],
                "coverage_points": ["FR-001"],
                "coverage_acceptance": ["AC-001"],
                "design_refs": ["DATA-001"],
                "coverage_change_ids": [],
            },
        ],
    }

    plan_cmd.sync_mutable_task_plan_contract(paths, requirement)

    root = tmp_path / ".codex-sdlc/requirements/REQ-001-规划"
    plan = __import__("json").loads((root / "tasks/task-plan.v2.json").read_text())
    coverage = __import__("json").loads((root / "task-coverage.v1.json").read_text())
    assert plan["tasks"] == ["T-001", "T-002"]
    assert plan["dependencies"] == [{"from": "T-001", "to": "T-002"}]
    assert coverage["functional_requirements"]["FR-001"]["tasks"] == ["T-001", "T-002"]
    assert coverage["acceptance_criteria"]["AC-001"]["test_refs"] == [
        "T-001#automated_tests/0",
        "T-002#automated_tests/0",
    ]
    assert not list(root.rglob("task-pack*"))


def test_import_receipt_rejects_partial_plan_mutation(tmp_path: Path) -> None:
    paths = build_paths(tmp_path)
    requirement = {"requirement_id": "REQ-001", "folder_name": "REQ-001-规划"}
    receipt = tmp_path / ".codex-sdlc/requirements/REQ-001-规划/tasks/.task-import-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="不能拆开覆盖"):
        plan_cmd.ensure_mutable_task_plan_contract(paths, requirement)


def test_mutable_plan_does_not_keep_closed_task_in_contract(tmp_path: Path) -> None:
    paths = build_paths(tmp_path)
    requirement = {
        "requirement_id": "REQ-001",
        "folder_name": "REQ-001-规划",
        "structured": {"requirement_points": [{"id": "FR-001"}]},
        "tasks": [
            {
                "task_id": "T-001",
                "status": "todo",
                "depends_on": [],
                "coverage_points": ["FR-001"],
            },
            {
                "task_id": "T-002",
                "status": "closed",
                "depends_on": ["T-001"],
                "coverage_points": ["FR-001"],
            },
        ],
    }

    plan_cmd.sync_mutable_task_plan_contract(paths, requirement)

    root = tmp_path / ".codex-sdlc/requirements/REQ-001-规划"
    plan = __import__("json").loads((root / "tasks/task-plan.v2.json").read_text())
    coverage = __import__("json").loads((root / "task-coverage.v1.json").read_text())
    assert plan["tasks"] == ["T-001"]
    assert coverage["functional_requirements"]["FR-001"]["tasks"] == ["T-001"]
