from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

# 旧 facts 回归继续使用 tests 根目录夹具；显式加入路径后，阶段二规定的 PYTHONPATH=src 可直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_sdlc.core import fact_artifacts, fact_gate, fact_schema
from formal_package_factory import _fixture_semantic, build_valid_formal_v3_bundle, formal_v2_package


def _bundle() -> dict[str, object]:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    return bundle


def _rehash(bundle: dict[str, object], owner: str = "requirement") -> None:
    document = bundle[owner]
    document["semantic_sha256"] = fact_artifacts.semantic_sha256(document["semantic"])
    document["artifact_sha256"] = fact_artifacts.artifact_sha256(document)
    bundle["review"] = fact_artifacts.build_review_artifact(
        bundle["requirement"], bundle["design"], bundle["requirement"]["context_targets"], status="passed", issues=[]
    )
    bundle["manifest"] = fact_artifacts.build_fact_manifest(
        bundle["source"], bundle["index"], bundle["requirement"], bundle["design"], bundle["review"]
    )


def test_hand_written_passed_review_without_cli_receipt_is_rejected() -> None:
    bundle = _bundle()
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code == "missing_review_receipt"


def test_unknown_fact_category_is_rejected() -> None:
    bundle = _bundle()
    bundle["requirement"]["semantic"]["facts"][0]["category"] = "anything"
    _rehash(bundle)
    assert fact_schema.fact_document_issues(bundle["requirement"], owner="requirement")


def test_missing_category_normalized_field_is_rejected() -> None:
    bundle = _bundle()
    fact = bundle["requirement"]["semantic"]["facts"][0]
    fact["category"] = "permission"
    fact["normalized"] = {"subject": "管理员"}
    _rehash(bundle)
    assert fact_schema.fact_document_issues(bundle["requirement"], owner="requirement")


def test_generic_non_contractual_reason_cannot_cover_content() -> None:
    bundle = _bundle()
    first = bundle["requirement"]["coverage"][0]
    first.update(status="non_contractual", fact_ids=[], reason="模型已标记该单元不形成独立合同事实。")
    _rehash(bundle)
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code == "untrusted_non_contractual"


@pytest.mark.parametrize("category", ["permission", "interface", "state_transition", "error", "out_of_scope"])
def test_deleted_high_risk_fact_cannot_be_hidden_by_rehashed_manual_pass(category: str) -> None:
    bundle = _bundle()
    fact = next(item for item in bundle["requirement"]["semantic"]["facts"] if item["category"] == category)
    bundle["requirement"]["semantic"]["facts"].remove(fact)
    _rehash(bundle)
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code in {"schema_invalid", "coverage_gap"}


@pytest.mark.parametrize(
    ("category", "normalized"),
    [
        ("permission", {"subject": "访客", "direction": "allow", "action": "导出", "resource": "全部订单"}),
        ("interface", {"method": "DELETE", "path": "/unrelated"}),
        ("state_transition", {"from": "approved", "to": "deleted", "trigger": "任意请求"}),
        ("error", {"condition": "无权限", "result": "继续成功"}),
        ("out_of_scope", {"excluded": "不处理单笔审批"}),
    ],
)
def test_changed_high_risk_meaning_needs_cli_review_receipt(category: str, normalized: dict[str, str]) -> None:
    bundle = _bundle()
    fact = next(item for item in bundle["requirement"]["semantic"]["facts"] if item["category"] == category)
    fact["normalized"] = normalized
    _rehash(bundle)
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code == "missing_review_receipt"


@pytest.mark.parametrize(
    "field",
    [
        "document_id",
        "source_projection_sha256",
        "anchor_kind",
        "json_pointer",
        "section_key",
        "quote",
        "quote_sha256",
        "owner",
        "classification",
    ],
)
def test_formal_source_reference_every_field_is_exact(field: str) -> None:
    bundle = _bundle()
    ref = bundle["requirement"]["bindings"]["formal_refs"][0]
    ref[field] = 7 if field in {"json_pointer", "section_key"} else f"伪造-{field}"
    _rehash(bundle)
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code == "invalid_source_ref"


def test_source_reference_rejects_extra_field() -> None:
    bundle = _bundle()
    bundle["requirement"]["bindings"]["formal_refs"][0]["extra"] = "不允许"
    _rehash(bundle)
    result = fact_gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed
    assert result.code == "invalid_source_ref"


@pytest.mark.parametrize(
    "field",
    [
        "document_id", "relative_path", "document_sha256", "anchor_kind", "line_start",
        "line_end", "section_key", "quote", "quote_sha256", "owner", "classification",
    ],
)
def test_markdown_source_reference_every_field_is_exact(field: str) -> None:
    source = fact_artifacts.build_draft_source_projection("业务规则\n", "技术方案\n", "问题\n", "决定\n")
    index = fact_artifacts.build_source_index(source, source_kind="draft", draft_id="DRAFT-001")
    targets = fact_artifacts.build_context_targets(source, index)
    requirement = fact_artifacts.build_fact_artifact(
        "requirement", _fixture_semantic("requirement", index), targets, index, draft_id="DRAFT-001"
    )
    design = fact_artifacts.build_fact_artifact(
        "design", _fixture_semantic("design", index), targets, index, draft_id="DRAFT-001"
    )
    review = fact_artifacts.build_review_artifact(requirement, design, targets, status="passed", issues=[])
    bundle = {
        "source": source,
        "index": index,
        "requirement": requirement,
        "design": design,
        "review": review,
        "manifest": fact_artifacts.build_fact_manifest(source, index, requirement, design, review),
        "origin_index": index,
    }
    ref = requirement["bindings"]["origin_refs"][0]
    ref[field] = 7 if field in {"line_start", "line_end"} else f"伪造-{field}"
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)
    bundle["review"] = fact_artifacts.build_review_artifact(requirement, design, targets, status="passed", issues=[])
    bundle["manifest"] = fact_artifacts.build_fact_manifest(source, index, requirement, design, bundle["review"])
    result = fact_gate.FactGate.verify(bundle, entry_kind="draft")
    assert not result.passed
    assert result.code == "invalid_source_ref"
