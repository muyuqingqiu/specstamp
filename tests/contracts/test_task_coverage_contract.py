from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.task_coverage_contract import (
    apply_task_coverage_to_state_tasks,
    validate_task_coverage_contract,
)


ROOT = Path(__file__).resolve().parents[2]


def _task(task_id: str = "T-001") -> dict[str, object]:
    return {
        "task_id": task_id,
        "requirement_id": "REQ-001",
        "requirement_refs": ["FR-001"],
        "design_refs": ["DATA-001"],
        "acceptance_refs": ["AC-001"],
        "change_refs": ["CHG-001"],
        "automated_tests": ["运行覆盖合同定向测试。"],
    }


def _coverage() -> dict[str, object]:
    return {
        "schema_version": "task-coverage.v1",
        "requirement_id": "REQ-001",
        "functional_requirements": {
            "FR-001": {"tasks": ["T-001"], "status": "implemented"}
        },
        "design_artifacts": {"DATA-001": {"tasks": ["T-001"]}},
        "acceptance_criteria": {
            "AC-001": {
                "tasks": ["T-001"],
                "test_refs": ["T-001#automated_tests/0"],
            }
        },
        "effective_changes": {"CHG-001": {"tasks": ["T-001"]}},
        "no_development_items": [],
    }


def _validate(value: dict[str, object]) -> dict[str, object]:
    return validate_task_coverage_contract(
        value,
        tasks=[_task()],
        functional_requirement_ids={"FR-001"},
        design_artifact_ids={"DATA-001"},
        acceptance_criterion_ids={"AC-001"},
        effective_change_ids={"CHG-001"},
    )


def test_complete_coverage_keeps_one_stable_primary_task_for_every_source() -> None:
    result = _validate(_coverage())

    assert result["functional_requirements"]["FR-001"]["tasks"] == ["T-001"]
    assert result["design_artifacts"]["DATA-001"]["tasks"] == ["T-001"]
    assert result["acceptance_criteria"]["AC-001"]["test_refs"] == [
        "T-001#automated_tests/0"
    ]
    assert result["effective_changes"]["CHG-001"]["tasks"] == ["T-001"]


@pytest.mark.parametrize(
    ("field", "source_id", "message"),
    [
        ("functional_requirements", "FR-001", "FR-001"),
        ("design_artifacts", "DATA-001", "DATA-001"),
        ("acceptance_criteria", "AC-001", "AC-001"),
        ("effective_changes", "CHG-001", "CHG-001"),
    ],
)
def test_missing_formal_source_is_rejected(
    field: str,
    source_id: str,
    message: str,
) -> None:
    coverage = _coverage()
    coverage[field].pop(source_id)  # type: ignore[union-attr]

    with pytest.raises(SdlcError, match=message):
        _validate(coverage)


def test_no_development_item_needs_reason_and_formal_basis() -> None:
    coverage = _coverage()
    coverage["design_artifacts"] = {}
    coverage["no_development_items"] = [
        {
            "source_type": "design_artifact",
            "source_id": "DATA-001",
            "reason": "",
            "basis_refs": [],
        }
    ]

    with pytest.raises(SdlcError, match="无需开发.*依据"):
        _validate(coverage)

    coverage["no_development_items"] = [
        {
            "source_type": "design_artifact",
            "source_id": "DATA-001",
            "reason": "该产物只约束全局数据命名，不产生独立开发输出。",
            "basis_refs": ["DES-001#architecture"],
        }
    ]
    result = _validate(coverage)
    assert result["no_development_items"][0]["source_id"] == "DATA-001"


@pytest.mark.parametrize(
    "test_ref",
    [
        "T-001#manual_checks/0",
        "T-001#automated_tests/1",
        "T-999#automated_tests/0",
    ],
)
def test_acceptance_criterion_must_point_to_existing_automated_test(
    test_ref: str,
) -> None:
    coverage = _coverage()
    coverage["acceptance_criteria"]["AC-001"]["test_refs"] = [test_ref]  # type: ignore[index]

    with pytest.raises(SdlcError, match="AC-001.*测试"):
        _validate(coverage)


def test_coverage_cannot_claim_a_task_that_does_not_reference_the_source() -> None:
    task = _task()
    task["requirement_refs"] = []

    with pytest.raises(SdlcError, match="FR-001.*task.v2"):
        validate_task_coverage_contract(
            _coverage(),
            tasks=[task],
            functional_requirement_ids={"FR-001"},
            design_artifact_ids={"DATA-001"},
            acceptance_criterion_ids={"AC-001"},
            effective_change_ids={"CHG-001"},
        )


def test_state_projection_uses_coverage_contract_instead_of_old_formal_fields() -> None:
    state_task = {
        "task_id": "T-001",
        "coverage_points": ["FR-999"],
        "coverage_change_ids": [],
        "coverage_acceptance": [],
        "coverage_tests": [],
    }

    apply_task_coverage_to_state_tasks([state_task], _coverage())

    assert state_task["coverage_points"] == ["FR-001"]
    assert state_task["coverage_design_refs"] == ["DATA-001"]
    assert state_task["coverage_change_ids"] == ["CHG-001"]
    assert state_task["coverage_acceptance"] == ["AC-001"]
    assert state_task["task_test_refs"] == ["T-001#automated_tests/0"]


def test_coverage_validation_does_not_modify_callers_input() -> None:
    coverage = _coverage()
    original = deepcopy(coverage)

    _validate(coverage)

    assert coverage == original


def test_task_coverage_contract_does_not_branch_on_chinese_error_text() -> None:
    source_path = ROOT / "src/codex_sdlc/core/task_coverage_contract.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    violations: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops):
            continue
        expressions = [node.left, *node.comparators]
        if any(
            isinstance(expression, ast.Constant)
            and isinstance(expression.value, str)
            and any("\u4e00" <= char <= "\u9fff" for char in expression.value)
            for expression in expressions
        ):
            violations.append(node.lineno)
    assert violations == []
