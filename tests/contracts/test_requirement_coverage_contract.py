from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_SPLIT_SCHEMA,
    ensure_requirement_review_ready,
    validate_requirement_contract,
)
from codex_sdlc.core.structured_contract import contract_sha256
from test_requirement_split_contract import requirement_documents


def _refresh_split_hash(split: dict[str, object], coverage: dict[str, object]) -> None:
    coverage["requirement_split_sha256"] = contract_sha256(
        split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )


def _validate(
    root: Path,
    split: dict[str, object],
    coverage: dict[str, object],
    material_hashes: dict[str, str],
    *,
    known_formal_ids: set[str] | None = None,
):
    return validate_requirement_contract(
        split,
        coverage,
        project_root=root,
        current_material_hashes=material_hashes,
        known_formal_ids=known_formal_ids or set(),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda coverage: coverage.update({"extra": True}),
        lambda coverage: coverage["units"][0].update({"status": "自动推断"}),
        lambda coverage: coverage["units"][0].update({"unknown": "不允许"}),
        lambda coverage: coverage["units"][0].update({"covered_by": []}),
        lambda coverage: coverage["units"][0].update(
            {
                "status": "background",
                "covered_by": [],
                "classification": "requirement",
            }
        ),
        lambda coverage: coverage["units"][0].update(
            {
                "status": "excluded_by_decision",
                "covered_by": [],
                "reason": "",
                "decision_refs": [],
            }
        ),
    ],
)
def test_coverage_status_required_fields_and_unknown_fields_are_strict(
    tmp_path: Path, mutate
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    mutate(coverage)

    with pytest.raises(SdlcError, match="Schema 校验失败"):
        _validate(tmp_path, split, coverage, material_hashes)


def test_split_hash_and_draft_id_must_match_the_other_file(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    coverage["requirement_split_sha256"] = "f" * 64

    with pytest.raises(SdlcError, match="requirement_split_sha256"):
        _validate(tmp_path, split, coverage, material_hashes)

    _refresh_split_hash(split, coverage)
    coverage["draft_id"] = "DRAFT-002"
    with pytest.raises(SdlcError, match="draft_id 不一致"):
        _validate(tmp_path, split, coverage, material_hashes)


def test_every_fr_gr_and_ac_needs_an_explicit_reverse_coverage_reference(
    tmp_path: Path,
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    coverage["units"] = coverage["units"][:-1]

    with pytest.raises(SdlcError, match="缺少：ac-course-visible"):
        _validate(tmp_path, split, coverage, material_hashes)


def test_covered_by_rejects_wrong_type_and_unknown_formal_id(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    coverage["units"][0]["covered_by"] = ["@client:src-rule"]
    with pytest.raises(SdlcError, match="必须引用 AC/FR/GR"):
        _validate(tmp_path, split, coverage, material_hashes)

    coverage["units"][0]["covered_by"] = ["GR-099"]
    with pytest.raises(SdlcError, match="正式编号不存在：GR-099"):
        _validate(tmp_path, split, coverage, material_hashes)


def test_duplicate_src_client_key_is_rejected(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    duplicate = deepcopy(coverage["units"][0])
    duplicate["covered_by"] = ["@client:fr-course-access"]
    coverage["units"].append(duplicate)

    with pytest.raises(SdlcError, match="client_key 重复：src-rule"):
        _validate(tmp_path, split, coverage, material_hashes)


@pytest.mark.parametrize("status", ["needs_user", "needs_material"])
def test_unresolved_coverage_status_is_valid_but_blocks_requirement_review(
    tmp_path: Path, status: str
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    unresolved = deepcopy(coverage["units"][0])
    unresolved.update(
        {
            "client_key": f"src-{status}",
            "classification": "other",
            "covered_by": [],
            "status": status,
            "reason": "等待明确输入",
        }
    )
    coverage["units"].append(unresolved)

    result = _validate(tmp_path, split, coverage, material_hashes)

    assert result.review_blockers == (f"src-{status}:{status}",)
    with pytest.raises(SdlcError, match="不能进入需求审核"):
        ensure_requirement_review_ready(result)


def test_open_questions_block_review_without_being_auto_removed(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    split["open_questions"] = ["课程过期后是否允许只读访问？"]
    _refresh_split_hash(split, coverage)

    result = _validate(tmp_path, split, coverage, material_hashes)

    assert split["open_questions"] == ["课程过期后是否允许只读访问？"]
    assert result.review_blockers == ("open_questions[0]",)
    with pytest.raises(SdlcError, match=r"open_questions\[0\]"):
        ensure_requirement_review_ready(result)


def test_background_and_out_of_scope_are_resolved_non_coverage_states(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    source_ref = deepcopy(coverage["units"][0]["source_ref"])
    coverage["units"].extend(
        [
            {
                "client_key": "src-background",
                "source_ref": deepcopy(source_ref),
                "classification": "background",
                "covered_by": [],
                "status": "background",
                "reason": "项目背景",
                "decision_refs": [],
                "relations": [],
            },
            {
                "client_key": "src-out-of-scope",
                "source_ref": deepcopy(source_ref),
                "classification": "out_of_scope",
                "covered_by": [],
                "status": "out_of_scope",
                "reason": "交付范围不包含课程编辑",
                "decision_refs": [],
                "relations": [],
            },
        ]
    )

    result = _validate(tmp_path, split, coverage, material_hashes)

    assert result.review_blockers == ()
    ensure_requirement_review_ready(result)


def test_excluded_by_decision_requires_an_existing_explicit_decision(
    tmp_path: Path,
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    excluded = deepcopy(coverage["units"][0])
    excluded.update(
        {
            "client_key": "src-excluded",
            "classification": "other",
            "covered_by": [],
            "status": "excluded_by_decision",
            "reason": "用户决定暂不交付离线模式",
            "decision_refs": ["DEC-007"],
        }
    )
    coverage["units"].append(excluded)

    with pytest.raises(SdlcError, match="用户决定不存在：DEC-007"):
        _validate(tmp_path, split, coverage, material_hashes)

    result = _validate(
        tmp_path,
        split,
        coverage,
        material_hashes,
        known_formal_ids={"DEC-007"},
    )
    assert result.review_blockers == ()


@pytest.mark.parametrize(
    ("forward", "backward"),
    [("split_into", "split_from"), ("merged_into", "merged_from")],
)
def test_source_split_and_merge_relations_are_explicit_and_bidirectional(
    tmp_path: Path, forward: str, backward: str
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    first = coverage["units"][0]
    second = coverage["units"][1]
    first["relations"] = [
        {"kind": forward, "target_ref": "@client:src-requirement"}
    ]
    second["relations"] = [
        {"kind": backward, "target_ref": "@client:src-rule"}
    ]

    _validate(tmp_path, split, coverage, material_hashes)

    second["relations"] = []
    with pytest.raises(SdlcError, match=f"缺少反向 {backward}"):
        _validate(tmp_path, split, coverage, material_hashes)


def test_coverage_validation_never_invents_missing_relation_or_unit(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    before = deepcopy(coverage)

    _validate(tmp_path, split, coverage, material_hashes)

    assert coverage == before
