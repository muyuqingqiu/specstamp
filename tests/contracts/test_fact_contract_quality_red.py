from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest


TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import fact_artifacts, fact_gate, fact_schema
from formal_package_factory import build_valid_formal_v3_bundle, formal_v2_package


REQUIREMENT_NORMALIZED = {
    "goal": {"value": "安全审批订单"},
    "actor_scenario": {"actor": "管理员", "scenario": "审批订单", "trigger": "点击审批"},
    "scope": {"included": "单笔审批"},
    "out_of_scope": {"excluded": "批量审批"},
    "business_rule": {"rule": "订单只能审批一次"},
    "permission": {"subject": "管理员", "direction": "allow", "action": "审批", "resource": "订单"},
    "interface": {"method": "POST", "path": "/orders/{id}/approve"},
    "page_behavior": {"page": "订单页", "action": "点击审批", "result": "显示成功"},
    "state_transition": {"from": "pending", "to": "approved", "trigger": "确认审批"},
    "error": {"condition": "无权限", "result": "403"},
    "data_change": {"entity": "order", "change": "status=approved"},
    "acceptance": {"expected": "审批成功"},
    "test_case": {"operation": "调用审批接口", "expected": "返回 200"},
}
DESIGN_NORMALIZED = {
    "module": {"module": "订单服务"},
    "interface_implementation": {"method": "POST", "path": "/orders/{id}/approve"},
    "permission_enforcement": {"subject": "管理员", "direction": "allow", "action": "审批", "resource": "订单"},
    "state_implementation": {"from": "pending", "to": "approved", "trigger": "确认审批"},
    "data_implementation": {"entity": "order", "change": "status=approved"},
    "error_handling": {"condition": "无权限", "result": "403"},
    "requirement_coverage": {"requirement_fact_id": "RF-0001", "design_fact_id": "DF-0001", "coverage": "完整实现"},
    "test_strategy": {"strategy": "接口回归"},
    "risk": {"risk": "并发审批", "mitigation": "条件更新"},
}


def _documents():
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    return bundle["requirement"], bundle["design"]


@pytest.mark.parametrize(
    ("owner", "schema_name", "normalized_by_category"),
    [
        ("requirement", "requirement-facts.v1.json", REQUIREMENT_NORMALIZED),
        ("design", "design-facts.v1.json", DESIGN_NORMALIZED),
    ],
)
def test_json_schema_uses_the_same_authoritative_categories_and_required_fields(
    owner: str,
    schema_name: str,
    normalized_by_category: dict[str, dict[str, str]],
) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "src" / "codex_sdlc" / "schemas" / schema_name
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fact_schema_definition = schema["$defs"]["fact"]
    assert set(fact_schema_definition["properties"]["category"]["enum"]) == fact_schema.FACT_CATEGORIES[owner]
    conditionals = {
        item["if"]["properties"]["category"]["const"]: set(item["then"]["properties"]["normalized"]["required"])
        for item in fact_schema_definition["allOf"]
    }
    assert conditionals == {category: set(values) for category, values in fact_schema.CATEGORY_NORMALIZED_FIELDS.items() if category in normalized_by_category}


@pytest.mark.parametrize(("category", "normalized"), REQUIREMENT_NORMALIZED.items())
def test_authoritative_requirement_categories_are_legal(category: str, normalized: dict[str, str]) -> None:
    requirement, _design = _documents()
    fact = requirement["semantic"]["facts"][0]
    fact.update(category=category, normalized=normalized)
    requirement["semantic_sha256"] = fact_artifacts.semantic_sha256(requirement["semantic"])
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)
    assert fact_schema.fact_document_issues(requirement, owner="requirement") == []


@pytest.mark.parametrize(("category", "normalized"), DESIGN_NORMALIZED.items())
def test_authoritative_design_categories_are_legal(category: str, normalized: dict[str, str]) -> None:
    _requirement, design = _documents()
    fact = design["semantic"]["facts"][0]
    selected_normalized = deepcopy(normalized)
    if category == "requirement_coverage":
        relation = next(
            item for item in design["semantic"]["relations"]
            if item["design_fact_id"] == fact["fact_id"]
        )
        selected_normalized.update(
            requirement_fact_id=relation["requirement_fact_id"], design_fact_id=fact["fact_id"]
        )
    fact.update(category=category, normalized=selected_normalized)
    design["semantic_sha256"] = fact_artifacts.semantic_sha256(design["semantic"])
    design["artifact_sha256"] = fact_artifacts.artifact_sha256(design)
    assert fact_schema.fact_document_issues(design, owner="design") == []


@pytest.mark.parametrize(
    "change",
    [
        {"requirement_fact_id": "RF-9999"},
        {"design_fact_id": "DF-9999"},
        {"type": "任意关系"},
        {"duplicate": True},
    ],
)
def test_relation_structure_and_references_are_checked(change: dict[str, object]) -> None:
    requirement, design = _documents()
    relation = design["semantic"]["relations"][0]
    if change.pop("duplicate", False):
        design["semantic"]["relations"].append(deepcopy(relation))
    else:
        relation.update(change)
    design["semantic_sha256"] = fact_artifacts.semantic_sha256(design["semantic"])
    design["artifact_sha256"] = fact_artifacts.artifact_sha256(design)
    requirement_ids = {item["fact_id"] for item in requirement["semantic"]["facts"]}
    assert fact_schema.fact_document_issues(design, owner="design", requirement_fact_ids=requirement_ids)


def test_coverage_unit_cannot_be_repeated() -> None:
    requirement, _design = _documents()
    requirement["coverage"].append(deepcopy(requirement["coverage"][0]))
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)
    assert fact_schema.fact_document_issues(requirement, owner="requirement")


def test_one_requirement_fact_can_link_multiple_compatible_design_facts() -> None:
    requirement, design = _documents()
    relation = deepcopy(design["semantic"]["relations"][0])
    original = next(
        item for item in design["semantic"]["facts"]
        if item["fact_id"] == relation["design_fact_id"]
    )
    another = deepcopy(original)
    another["fact_id"] = "DF-9998"
    another["statement"] = "同一需求由第二个技术事实共同实现。"
    if another["category"] == "requirement_coverage":
        another["normalized"]["design_fact_id"] = another["fact_id"]
        another["normalized"]["requirement_fact_id"] = relation["requirement_fact_id"]
    design["semantic"]["facts"].append(another)
    relation["design_fact_id"] = another["fact_id"]
    design["semantic"]["relations"].append(relation)
    design["semantic_sha256"] = fact_artifacts.semantic_sha256(design["semantic"])
    design["artifact_sha256"] = fact_artifacts.artifact_sha256(design)
    requirement_ids = {item["fact_id"] for item in requirement["semantic"]["facts"]}
    assert fact_schema.fact_document_issues(design, owner="design", requirement_fact_ids=requirement_ids) == []


@pytest.mark.parametrize("attack", ["wrong-owner-unit", "unrelated-real-fact", "ambiguous-without-detail"])
def test_coverage_must_bind_the_current_unit_owner_and_fact(attack: str) -> None:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    requirement = bundle["requirement"]
    coverage = requirement["coverage"][0]
    if attack == "wrong-owner-unit":
        coverage["unit_id"] = next(item["unit_id"] for item in bundle["index"]["units"] if item["owner"] == "design")
    elif attack == "unrelated-real-fact":
        coverage["fact_ids"] = [
            next(
                item["fact_id"] for item in requirement["semantic"]["facts"]
                if coverage["unit_id"] not in item["source_refs"]
            )
        ]
    else:
        coverage.update(status="ambiguous", fact_ids=[], reason="暂不确定")
    result = fact_gate.validate_fact_artifact_references(
        requirement,
        bundle["index"],
        owner="requirement",
        entry_kind="formal",
        review=bundle["review"],
    )
    assert result is not None
