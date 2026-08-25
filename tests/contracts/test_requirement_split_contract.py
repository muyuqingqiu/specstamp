from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_SPLIT_SCHEMA,
    read_requirement_document,
    rewrite_requirement_documents,
    validate_requirement_contract,
)
from codex_sdlc.core.structured_contract import contract_sha256, sha256_file


def _source_ref(source: Path, root: Path) -> dict[str, object]:
    return {
        "material_id": "MAT-001",
        "reference": {
            "schema_version": "reference-locator.v1",
            "path": source.relative_to(root).as_posix(),
            "sha256": sha256_file(source),
            "locator": {"kind": "whole_file"},
        },
    }


def requirement_documents(
    root: Path,
    *,
    source_text: str = "用户登录后可以查看课程。\n",
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, str]]:
    source = root / "需求原文.md"
    source.write_text(source_text, encoding="utf-8")
    source_ref = _source_ref(source, root)
    split: dict[str, object] = {
        "schema_version": "requirement-split.v1",
        "draft_id": "DRAFT-001",
        "producer_run_id": "thread-contract-test",
        "title": "课程访问需求",
        "background": "用户需要在登录后访问课程。",
        "goal": "提供可验收的课程访问结果。",
        "scope": ["登录状态下查看课程"],
        "out_of_scope": ["课程内容编辑"],
        "user_scenarios": ["用户登录后打开课程页"],
        "input_material_hashes": {"MAT-001": sha256_file(source)},
        "global_rules": [
            {
                "client_key": "gr-login-state",
                "title": "统一登录状态",
                "description": "课程访问统一使用当前登录状态。",
                "type": "state",
                "applies_to": ["@client:fr-course-access"],
                "source_refs": [deepcopy(source_ref)],
                "relations": [],
            }
        ],
        "functional_requirements": [
            {
                "client_key": "fr-course-access",
                "title": "查看课程",
                "description": "用户登录后可以查看课程。",
                "elements": ["课程标题", "课程内容"],
                "flow": ["用户登录", "打开课程页", "系统展示课程"],
                "facts": ["课程资料已经归档"],
                "rules": ["只有登录用户可以查看课程"],
                "constraints": ["不包含课程编辑"],
                "states_and_exceptions": ["登录失效时拒绝访问"],
                "acceptance_criteria": [
                    {
                        "client_key": "ac-course-visible",
                        "owner_fr_ref": "@client:fr-course-access",
                        "operation": "使用已登录用户打开课程页",
                        "expected": "页面显示课程标题和内容",
                        "pass_standard": "标题和内容完整显示且没有访问错误",
                        "source_refs": [deepcopy(source_ref)],
                        "relations": [],
                    }
                ],
                "global_rule_refs": ["@client:gr-login-state"],
                "source_refs": [deepcopy(source_ref)],
                "material_refs": ["MAT-001"],
                "depends_on": [],
                "out_of_scope": ["编辑课程"],
                "relations": [],
            }
        ],
        "open_questions": [],
    }
    coverage: dict[str, object] = {
        "schema_version": "requirement-coverage.v1",
        "draft_id": "DRAFT-001",
        "requirement_split_sha256": contract_sha256(
            split, schema_name=REQUIREMENT_SPLIT_SCHEMA
        ),
        "units": [
            {
                "client_key": "src-rule",
                "source_ref": deepcopy(source_ref),
                "classification": "global_rule",
                "covered_by": ["@client:gr-login-state"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
            {
                "client_key": "src-requirement",
                "source_ref": deepcopy(source_ref),
                "classification": "requirement",
                "covered_by": ["@client:fr-course-access"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
            {
                "client_key": "src-acceptance",
                "source_ref": deepcopy(source_ref),
                "classification": "acceptance",
                "covered_by": ["@client:ac-course-visible"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
        ],
    }
    return source, split, coverage, {"MAT-001": sha256_file(source)}


def _refresh_split_hash(split: dict[str, object], coverage: dict[str, object]) -> None:
    coverage["requirement_split_sha256"] = contract_sha256(
        split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )


def test_valid_split_contract_exposes_t002_allocation_objects_without_mutation(
    tmp_path: Path,
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    before_split = deepcopy(split)
    before_coverage = deepcopy(coverage)

    result = validate_requirement_contract(
        split,
        coverage,
        project_root=tmp_path,
        current_material_hashes=material_hashes,
        expected_draft_id="DRAFT-001",
        expected_producer_run_id="thread-contract-test",
    )

    assert [(item.client_key, item.id_prefix) for item in result.allocation_objects] == [
        ("ac-course-visible", "AC"),
        ("fr-course-access", "FR"),
        ("gr-login-state", "GR"),
        ("src-acceptance", "SRC"),
        ("src-requirement", "SRC"),
        ("src-rule", "SRC"),
    ]
    assert result.review_blockers == ()
    assert split == before_split
    assert coverage == before_coverage


@pytest.mark.parametrize(
    "mutate",
    [
        lambda split: split.pop("goal"),
        lambda split: split.update({"unknown": True}),
        lambda split: split["global_rules"][0].update({"type": "模型自行判断"}),
        lambda split: split["functional_requirements"][0].update(
            {"client_key": "FR-WRONG"}
        ),
        lambda split: split["functional_requirements"][0]["acceptance_criteria"][0].update(
            {"extra": "不允许"}
        ),
    ],
)
def test_required_unknown_enum_and_client_key_boundaries_are_strict(
    tmp_path: Path, mutate
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    mutate(split)
    _refresh_split_hash(split, coverage)

    with pytest.raises(SdlcError, match="Schema 校验失败"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
        )


def test_duplicate_json_field_and_duplicate_client_key_are_rejected(tmp_path: Path) -> None:
    duplicate_json = tmp_path / "重复字段.json"
    duplicate_json.write_text(
        '{"schema_version":"requirement-split.v1","schema_version":"requirement-split.v1"}',
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="重复字段：schema_version"):
        read_requirement_document(duplicate_json, schema_name=REQUIREMENT_SPLIT_SCHEMA)

    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    split["global_rules"][0]["client_key"] = "fr-course-access"
    _refresh_split_hash(split, coverage)
    with pytest.raises(SdlcError, match="client_key 重复"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
        )


def test_ac_owner_must_match_the_fr_that_contains_it(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    criterion = split["functional_requirements"][0]["acceptance_criteria"][0]
    criterion["owner_fr_ref"] = "@client:gr-login-state"
    _refresh_split_hash(split, coverage)

    with pytest.raises(SdlcError, match="AC ac-course-visible 错属 FR"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda split: split["functional_requirements"][0].update(
                {"depends_on": ["@client:not-in-package"]}
            ),
            "跨包或悬空临时引用",
        ),
        (
            lambda split: split["functional_requirements"][0].update(
                {"description": "错误地嵌入 @client:fr-course-access"}
            ),
            "Schema 校验失败",
        ),
        (
            lambda split: split["functional_requirements"][0].update(
                {"global_rule_refs": ["@client:ac-course-visible"]}
            ),
            "必须引用 GR",
        ),
        (
            lambda split: split["functional_requirements"][0]["source_refs"][0][
                "reference"
            ]["locator"].update({"display_heading": "@client:fr-course-access"}),
            "reference 不能包含 @client",
        ),
        (
            lambda split: split["functional_requirements"][0]["source_refs"][0][
                "reference"
            ]["locator"].update({"unknown": "不允许"}),
            "reference-locator.v1 Schema 校验失败",
        ),
    ],
)
def test_temporary_references_only_work_as_complete_typed_reference_values(
    tmp_path: Path, mutate, message: str
) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    mutate(split)
    _refresh_split_hash(split, coverage)

    with pytest.raises(SdlcError, match=message):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
        )


def test_explicit_replacement_relation_accepts_known_same_type_only(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    requirement = split["functional_requirements"][0]
    requirement["relations"] = [{"kind": "replaces", "target_ref": "FR-099"}]
    _refresh_split_hash(split, coverage)

    validate_requirement_contract(
        split,
        coverage,
        project_root=tmp_path,
        current_material_hashes=material_hashes,
        known_formal_ids={"FR-099"},
    )

    requirement["relations"] = [{"kind": "replaces", "target_ref": "AC-099"}]
    _refresh_split_hash(split, coverage)
    with pytest.raises(SdlcError, match="必须引用 FR"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
            known_formal_ids={"AC-099"},
        )


def test_material_hash_and_source_location_drift_are_both_rejected(tmp_path: Path) -> None:
    source, split, coverage, material_hashes = requirement_documents(tmp_path)

    with pytest.raises(SdlcError, match="输入资料内容已经变化"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes={"MAT-001": "f" * 64},
        )

    source.write_text("来源已经变化。\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="引用文件内容已经变化"):
        validate_requirement_contract(
            split,
            coverage,
            project_root=tmp_path,
            current_material_hashes=material_hashes,
        )


def test_long_document_is_hashed_and_validated_without_truncating_text(tmp_path: Path) -> None:
    long_text = "精确数字 1234567890、状态 READY、错误码 E-2048。\n" * 30000
    _, split, coverage, material_hashes = requirement_documents(
        tmp_path, source_text=long_text
    )
    split["background"] = long_text
    split["functional_requirements"][0]["description"] = long_text
    before = deepcopy(split)
    _refresh_split_hash(split, coverage)

    result = validate_requirement_contract(
        split,
        coverage,
        project_root=tmp_path,
        current_material_hashes=material_hashes,
    )

    assert split == before
    assert len(split["background"]) == len(long_text)
    assert len(split["functional_requirements"][0]["description"]) == len(long_text)
    assert result.split_sha256 == coverage["requirement_split_sha256"]


def test_contract_does_not_judge_text_semantics_or_auto_fill_content(tmp_path: Path) -> None:
    _, split, coverage, material_hashes = requirement_documents(tmp_path)
    split["functional_requirements"][0]["description"] = "甲"
    split["functional_requirements"][0]["rules"] = []
    split["functional_requirements"][0]["constraints"] = []
    before = deepcopy(split)
    _refresh_split_hash(split, coverage)

    validate_requirement_contract(
        split,
        coverage,
        project_root=tmp_path,
        current_material_hashes=material_hashes,
    )

    assert split == before


def test_rewrite_reuses_t002_complete_value_rule(tmp_path: Path) -> None:
    _, split, coverage, _ = requirement_documents(tmp_path)
    mapping = {
        "gr-login-state": "GR-001",
        "fr-course-access": "FR-001",
        "ac-course-visible": "AC-001",
        "src-rule": "SRC-001",
        "src-requirement": "SRC-002",
        "src-acceptance": "SRC-003",
    }

    rewritten_split, rewritten_coverage = rewrite_requirement_documents(
        split, coverage, mapping
    )

    assert rewritten_split["global_rules"][0]["applies_to"] == ["FR-001"]
    assert rewritten_split["functional_requirements"][0]["global_rule_refs"] == [
        "GR-001"
    ]
    assert rewritten_coverage["units"][2]["covered_by"] == ["AC-001"]

    split["title"] = "普通文字中不能嵌入 @client:fr-course-access"
    with pytest.raises(SdlcError, match="不能嵌入普通文字"):
        rewrite_requirement_documents(split, coverage, mapping)
