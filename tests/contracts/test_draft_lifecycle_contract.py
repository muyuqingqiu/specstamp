from __future__ import annotations

from copy import deepcopy

from codex_sdlc.core import draft_lifecycle
from codex_sdlc.core.structured_contract import canonical_sha256, contract_sha256


def structured_draft() -> dict[str, object]:
    split: dict[str, object] = {
        "schema_version": "requirement-split.v1",
        "draft_id": "DRAFT-001",
        "producer_run_id": "test-run",
        "title": "订单导出",
        "background": "订单需要导出。",
        "goal": "提供可验收的导出结果。",
        "scope": ["导出订单"],
        "out_of_scope": [],
        "user_scenarios": ["运营人员导出订单"],
        "input_material_hashes": {"MAT-001": "a" * 64},
        "global_rules": [],
        "functional_requirements": [
            {
                "id": "FR-001",
                "title": "导出订单",
                "description": "导出当前订单。",
                "elements": ["订单"],
                "flow": ["选择订单", "执行导出"],
                "facts": [],
                "rules": [],
                "constraints": [],
                "states_and_exceptions": [],
                "acceptance_criteria": [],
                "global_rule_refs": [],
                "source_refs": [],
                "material_refs": ["MAT-001"],
                "depends_on": [],
                "out_of_scope": [],
                "relations": [],
            }
        ],
        "open_questions": [],
    }
    coverage = {
        "schema_version": "requirement-coverage.v1",
        "draft_id": "DRAFT-001",
        "requirement_split_sha256": contract_sha256(
            split, schema_name="requirement-split.v1"
        ),
        "units": [],
    }
    return {
        "draft_id": "DRAFT-001",
        "title": "订单导出",
        "status": "discussing",
        "_structured_stage_enabled": True,
        "_material_manifest_enabled": True,
        "materials": [
            {
                "material_id": "MAT-001",
                "source_kind": "file",
                "type": "requirement",
                "roles": ["requirement"],
                "status": "active",
                "sha256": "a" * 64,
            }
        ],
        "requirement_split": split,
        "requirement_coverage": coverage,
        "requirement_import": {
            "schema": "draft-requirement-import-receipt.v1",
            "mapping": {},
        },
        "structured_captures": [],
        "decision_records": [],
        "questions": [],
        "decisions": [],
    }


def capture(
    capture_id: str,
    *,
    capture_type: str = "question",
    status: str = "pending",
    source_path: str | None = None,
) -> dict[str, object]:
    reference = {
        "schema_version": "reference-locator.v1",
        "path": source_path or f"资料/{capture_id}.txt",
        "sha256": "b" * 64,
        "locator": {"kind": "whole_file"},
    }
    return {
        "schema_version": "capture-increment.v1",
        "capture_id": capture_id,
        "draft_id": "DRAFT-001",
        "capture_type": capture_type,
        "source_reference": reference,
        "increment": f"{capture_id} 的展示文字",
        "status": status,
    }


def decision(decision_id: str, question_capture: dict[str, object]) -> dict[str, object]:
    question_reference = deepcopy(question_capture["source_reference"])
    record = {
        "schema_version": "decision.v1",
        "decision_id": decision_id,
        "status": "confirmed",
        "question": {
            "text": "展示文字不参与匹配",
            "capture_ref": question_capture["capture_id"],
            "reference": question_reference,
        },
        "selection": "确认",
        "source_capture_id": "CAP-099",
    }
    record["decision_sha256"] = canonical_sha256(record)
    return record


def blocker_codes(assessment: draft_lifecycle.DraftAssessment) -> list[str]:
    return [item.code for item in assessment.blockers]


def test_status_priority_starts_with_material_then_requirement_artifacts() -> None:
    draft = structured_draft()
    draft["materials"] = []
    draft.pop("requirement_split")
    draft.pop("requirement_coverage")
    draft.pop("requirement_import")
    draft["structured_captures"] = [capture("CAP-001")]

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "discussing"
    assert blocker_codes(assessment) == [
        "material_missing",
        "requirement_artifacts_missing",
        "open_question",
        "pending_capture",
    ]
    assert assessment.open_questions == ("CAP-001",)
    assert assessment.can_start is False


def test_complete_structured_requirements_enter_reviewing_not_confirmed() -> None:
    draft = structured_draft()
    draft.update(
        {
            "title": "包含 needs_user 的标题不参与状态",
            "requirement_summary": "pending CAP 只是普通展示文字",
            "requirement_body": "# Markdown\n\n用户已经确认。",
            "questions": ["旧展示问题不能伪造成结构化问题"],
            "decisions": ["旧展示决定不能伪造成 DEC"],
        }
    )

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "requirement_reviewing"
    assert blocker_codes(assessment) == ["requirement_review_pending"]
    assert assessment.open_questions == ()
    assert assessment.can_start is False


def test_decision_only_resolves_the_exact_capture_reference() -> None:
    draft = structured_draft()
    first = capture("CAP-001")
    second = capture("CAP-002")
    decision_source = capture(
        "CAP-099", capture_type="decision", status="absorbed"
    )
    draft["structured_captures"] = [first, second, decision_source]
    draft["decision_records"] = [decision("DEC-001", first)]

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.open_questions == ("CAP-002",)
    assert [item.question_id for item in assessment.structured_questions] == ["CAP-002"]
    assert blocker_codes(assessment) == [
        "pending_capture",
        "open_question",
        "pending_capture",
    ]

    wrong_reference = deepcopy(draft["decision_records"][0])
    wrong_reference["question"]["reference"]["path"] = "资料/其它问题.txt"
    draft["decision_records"] = [wrong_reference]
    unmatched = draft_lifecycle.assess_draft(draft)
    assert unmatched.open_questions == ("CAP-001", "CAP-002")


def test_capture_transition_status_is_used_without_mutating_initial_record() -> None:
    draft = structured_draft()
    initial = capture("CAP-001", capture_type="fact")
    draft["structured_captures"] = [deepcopy(initial)]
    draft["capture_statuses"] = {"CAP-001": "absorbed"}
    draft["capture_transitions"] = [
        {
            "capture_id": "CAP-001",
            "from_status": "pending",
            "to_status": "absorbed",
        }
    ]

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "requirement_reviewing"
    assert blocker_codes(assessment) == ["requirement_review_pending"]
    assert draft["structured_captures"][0]["status"] == "pending"


def test_split_question_and_coverage_blockers_are_structural_and_precise() -> None:
    draft = structured_draft()
    split = draft["requirement_split"]
    split["open_questions"] = ["是否包含订单来源？"]
    coverage = draft["requirement_coverage"]
    coverage["requirement_split_sha256"] = contract_sha256(
        split, schema_name="requirement-split.v1"
    )
    coverage["units"] = [
        {"client_key": "src-user", "status": "needs_user"},
        {"client_key": "src-material", "status": "needs_material"},
    ]
    draft["requirement_import"]["mapping"] = {
        "src-user": "SRC-001",
        "src-material": "SRC-002",
    }

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "discussing"
    assert blocker_codes(assessment) == ["open_question", "needs_user", "needs_material"]
    assert assessment.open_questions[0].endswith(":open_questions[0]")
    assert assessment.open_questions[1:] == ("SRC-001", "SRC-002")


def test_material_revision_invalidates_existing_requirement_artifacts() -> None:
    draft = structured_draft()
    draft["materials"] = [
        {
            "material_id": "MAT-002",
            "source_kind": "file",
            "type": "requirement",
            "roles": ["requirement"],
            "status": "active",
            "sha256": "c" * 64,
        }
    ]

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "discussing"
    assert blocker_codes(assessment) == ["requirement_artifacts_stale"]


def test_unversioned_external_material_has_highest_priority() -> None:
    draft = structured_draft()
    draft["materials"].append(
        {
            "material_id": "MAT-002",
            "source_kind": "external-reference",
            "type": "ui-design",
            "roles": ["ui-design"],
            "status": "unversioned",
            "version_evidence": {"status": "unversioned", "evidence": None},
        }
    )

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "discussing"
    assert assessment.blockers[0] == draft_lifecycle.DraftBlocker(
        "material_unstable", "MAT-002", "unversioned"
    )


def test_old_draft_remains_readable_without_fabricated_cap_or_decision() -> None:
    old = {
        "draft_id": "DRAFT-001",
        "title": "旧草稿",
        "status": "needs_user",
        "questions": ["旧问题"],
        "decisions": ["旧决定"],
    }

    assessment = draft_lifecycle.assess_draft(old)

    assert draft_lifecycle.uses_structured_requirement_stage(old) is False
    assert assessment.effective_status == "needs_user"
    assert assessment.open_questions == ("旧问题",)
    assert assessment.structured_questions == ()
    assert "structured_captures" not in old
    assert "decision_records" not in old


def test_allowed_statuses_include_requirement_stage_and_keep_started_boundary() -> None:
    statuses = draft_lifecycle.allowed_statuses()

    assert "requirement_reviewing" in statuses
    assert "requirement_confirmed" in statuses
    assert "start_ready" in statuses
    assert "started" in statuses
    assert draft_lifecycle.is_unfinished_draft({"status": "requirement_confirmed"})
    assert not draft_lifecycle.is_unfinished_draft({"status": "started"})
