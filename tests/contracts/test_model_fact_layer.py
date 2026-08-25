from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError


def api():
    """把新合同放在测试执行期导入，RED 时每个 FACT 都能留下独立失败记录。"""

    from codex_sdlc.core import fact_artifacts, fact_gate

    return fact_artifacts, fact_gate


def business() -> dict[str, object]:
    return {
        "title": "订单审批",
        "description": "审批订单",
        "background": "订单需要审批",
        "goal": "安全审批订单",
        "user_scenarios": ["管理员审批订单"],
        "scope": ["单笔审批"],
        "out_of_scope": ["不处理批量审批"],
        "business_rules": ["订单只能审批一次"],
        "permission_rules": ["禁止普通用户导出订单"],
        "data_state_rules": ["pending -> approved"],
        "interface_scope": ["POST /orders/{id}/approve"],
        "exception_rules": ["无权限返回 403"],
        "test_focus": ["权限和状态"],
        "open_questions": [],
        "decisions": ["使用单笔审批"],
        "functional_requirements": [{"id": "FR-001", "description": "管理员审批订单"}],
        "acceptance_criteria": [{"id": "AC-001", "requirement_ids": ["FR-001"], "expected": "审批成功"}],
        "test_cases": [{"id": "TC-001", "acceptance_ids": ["AC-001"], "requirement_ids": ["FR-001"]}],
        "design": {"summary": "订单服务实现审批", "interfaces": ["POST /orders/{id}/approve"]},
    }


def semantic(owner: str) -> dict[str, object]:
    category = "permission" if owner == "requirement" else "permission_enforcement"
    return {
        "facts": [
            {
                "fact_id": "RF-0001" if owner == "requirement" else "DF-0001",
                "category": category,
                "owner": owner,
                "statement": "禁止普通用户导出订单",
                "normalized": {"subject": "普通用户", "direction": "deny", "action": "导出", "resource": "订单"},
                "certainty": "confirmed",
                "source_refs": ["RU-0009" if owner == "requirement" else "DU-0002"],
                "decision_refs": [],
                "ambiguity_id": None,
            }
        ],
        "relations": ([{"type": "implements", "requirement_fact_id": "RF-0001", "design_fact_id": "DF-0001"}] if owner == "design" else []),
        "ambiguities": [],
    }


def valid_bundle() -> dict[str, object]:
    artifacts, _gate = api()
    source = artifacts.build_formal_source_projection(business())
    index = artifacts.build_source_index(source, source_kind="formal")
    targets = artifacts.build_context_targets(source, index)
    req = artifacts.build_fact_artifact("requirement", semantic("requirement"), targets, index)
    design = artifacts.build_fact_artifact("design", semantic("design"), targets, index)
    review = artifacts.build_review_artifact(req, design, targets, status="passed", issues=[])
    manifest = artifacts.build_fact_manifest(source, index, req, design, review)
    return {
        "source": source,
        "index": index,
        "requirement": req,
        "design": design,
        "review": review,
        "manifest": manifest,
    }


def refresh_requirement_chain(bundle: dict[str, object]) -> None:
    """攻击样本重算外层哈希，确保测试真正到达引用或覆盖门禁。"""

    artifacts, _gate = api()
    bundle["requirement"]["artifact_sha256"] = artifacts.artifact_sha256(bundle["requirement"])
    bundle["review"] = artifacts.build_review_artifact(
        bundle["requirement"], bundle["design"], bundle["requirement"]["context_targets"], status="passed", issues=[]
    )
    bundle["manifest"] = artifacts.build_fact_manifest(
        bundle["source"], bundle["index"], bundle["requirement"], bundle["design"], bundle["review"]
    )


def test_fact_hash_compatibility_api_uses_the_only_structured_implementation() -> None:
    artifacts, _gate = api()
    from codex_sdlc.core import structured_contract

    assert artifacts.canonical_json_bytes is structured_contract.canonical_json_bytes
    assert artifacts.canonical_json_text is structured_contract.canonical_json_text
    assert artifacts.sha256_bytes is structured_contract.sha256_bytes
    assert artifacts.canonical_sha256 is structured_contract.canonical_sha256

    with pytest.raises(SdlcError):
        artifacts.canonical_json_bytes({"invalid": float("nan")})


def test_fact_source_index_hash_uses_schema_exclusion_without_changing_business_contract() -> None:
    artifacts, _gate = api()
    from codex_sdlc.core import structured_contract

    index = valid_bundle()["index"]
    structured_contract.validate_schema_document(index, schema_name="sdlc.source-index.v1")
    assert artifacts.artifact_sha256(index) == structured_contract.contract_sha256(
        index,
        schema_name="sdlc.source-index.v1",
    )

    changed_hash = deepcopy(index)
    changed_hash["artifact_sha256"] = "0" * 64
    assert artifacts.artifact_sha256(changed_hash) == artifacts.artifact_sha256(index)

    changed_content = deepcopy(index)
    changed_content["source_projection_sha256"] = "f" * 64
    assert artifacts.artifact_sha256(changed_content) != artifacts.artifact_sha256(index)

    with pytest.raises(SdlcError):
        artifacts.artifact_sha256({"artifact_sha256": "0" * 64})

    wrong_version = {**index, "schema": "sdlc.source-index.v2"}
    unknown_field = {**index, "unknown": "合同外字段"}
    missing_field = {key: value for key, value in index.items() if key != "units"}
    with pytest.raises(SdlcError):
        artifacts.artifact_sha256(wrong_version)
    for invalid in (unknown_field, missing_field):
        with pytest.raises(SdlcError):
            structured_contract.validate_schema_document(
                invalid,
                schema_name="sdlc.source-index.v1",
            )


@pytest.mark.parametrize("entry_kind", ["draft", "formal"])
@pytest.mark.parametrize("missing", ["requirement", "design", "review"])
def test_fact_001_to_003_missing_model_artifact_blocks_both_entries(entry_kind: str, missing: str) -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle.pop(missing)
    result = gate.FactGate.verify(bundle, entry_kind=entry_kind)
    assert not result.passed
    assert result.code == f"missing_{missing}_facts" if missing != "review" else result.code == "missing_model_review"


def test_fact_004_schema_or_artifact_hash_mismatch_blocks() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["requirement"]["artifact_sha256"] = "0" * 64
    result = gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed and result.code == "artifact_hash_mismatch"


def test_fact_005_unknown_source_reference_blocks() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["requirement"]["bindings"]["formal_refs"][0]["unit_id"] = "RU-9999"
    refresh_requirement_chain(bundle)
    assert gate.FactGate.verify(bundle, entry_kind="formal").code == "invalid_source_ref"


def test_fact_006_quote_mismatch_blocks() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["requirement"]["bindings"]["formal_refs"][0]["quote"] = "允许普通用户导出订单"
    refresh_requirement_chain(bundle)
    assert gate.FactGate.verify(bundle, entry_kind="formal").code == "invalid_source_ref"


def test_fact_007_uncovered_source_unit_blocks() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["requirement"]["coverage"] = []
    refresh_requirement_chain(bundle)
    assert gate.FactGate.verify(bundle, entry_kind="formal").code == "coverage_gap"


@pytest.mark.parametrize(
    ("fact_id", "issue_type"),
    [
        ("FACT-008", "omitted_fact"),
        ("FACT-009", "meaning_changed"),
        ("FACT-010", "meaning_changed"),
        ("FACT-011", "wrong_relation"),
        ("FACT-012", "wrong_relation"),
        ("FACT-013", "requirement_design_conflict"),
        ("FACT-014", "ambiguity_unresolved"),
    ],
)
def test_fact_008_to_014_model_review_issue_blocks(fact_id: str, issue_type: str) -> None:
    artifacts, gate = api()
    bundle = valid_bundle()
    bundle["review"] = artifacts.build_review_artifact(
        bundle["requirement"],
        bundle["design"],
        bundle["requirement"]["context_targets"],
        status="needs_user" if issue_type == "ambiguity_unresolved" else "needs_review",
        issues=[{"issue_id": fact_id, "severity": "high", "type": issue_type, "message": "需要修正", "recovery": "重新提取并复核"}],
    )
    bundle["manifest"] = artifacts.build_fact_manifest(
        bundle["source"], bundle["index"], bundle["requirement"], bundle["design"], bundle["review"]
    )
    result = gate.FactGate.verify(bundle, entry_kind="formal")
    assert not result.passed and result.code == issue_type


def test_fact_015_input_change_makes_review_stale() -> None:
    artifacts, gate = api()
    bundle = valid_bundle()
    changed = deepcopy(business())
    changed["goal"] = "安全且快速审批订单"
    bundle["source"] = artifacts.build_formal_source_projection(changed)
    assert gate.review_freshness(bundle).status == "stale"


def test_fact_016_draft_and_formal_semantic_digest_mismatch_blocks() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["origin_semantic_sha256"] = {"requirement": "0" * 64, "design": bundle["design"]["semantic_sha256"]}
    assert gate.FactGate.verify(bundle, entry_kind="formal").code == "entry_contract_mismatch"


def test_fact_017_both_entries_use_same_gate_result() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["review"]["status"] = "needs_review"
    draft_result = gate.FactGate.verify(deepcopy(bundle), entry_kind="draft")
    formal_result = gate.FactGate.verify(deepcopy(bundle), entry_kind="formal")
    assert (draft_result.passed, draft_result.code) == (formal_result.passed, formal_result.code)


def test_fact_018_repeated_packaging_is_byte_identical_and_hash_graph_has_no_cycle() -> None:
    artifacts, _gate = api()
    one = artifacts.package_formal_v3(business(), valid_bundle())
    two = artifacts.package_formal_v3(business(), valid_bundle())
    assert artifacts.canonical_json_bytes(one) == artifacts.canonical_json_bytes(two)
    assert "fact_bundle" not in one["source_projection"]
    assert "full_file_sha256" not in json.dumps(one, ensure_ascii=False)


@pytest.mark.parametrize(("fact_id", "field"), [("FACT-019", "open_questions"), ("FACT-020", "decisions")])
def test_fact_019_020_questions_and_decisions_change_context_and_stale(fact_id: str, field: str) -> None:
    artifacts, gate = api()
    bundle = valid_bundle()
    changed = deepcopy(business())
    changed[field] = [f"{fact_id} 改变一个字节"]
    bundle["source"] = artifacts.build_formal_source_projection(changed)
    assert gate.review_freshness(bundle).status == "stale"


def test_fact_021_source_index_change_stales_review() -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["index"]["units"][0]["quote"] += "。"
    assert gate.review_freshness(bundle).status == "stale"


def test_fact_022_materialized_facts_keep_semantic_digest() -> None:
    artifacts, _gate = api()
    bundle = valid_bundle()
    origin = deepcopy(bundle["requirement"])
    origin["bindings"]["origin_refs"] = origin["bindings"]["formal_refs"]
    origin["bindings"]["formal_refs"] = []
    origin["artifact_sha256"] = artifacts.artifact_sha256(origin)
    materialized = artifacts.materialize_fact_artifact(origin, bundle["index"])
    assert materialized["artifact_sha256"] != origin["artifact_sha256"]
    assert materialized["semantic_sha256"] == origin["semantic_sha256"]


@pytest.mark.parametrize("binding", ["origin_refs", "formal_refs"])
def test_fact_023_forged_origin_or_formal_anchor_blocks(binding: str) -> None:
    _artifacts, gate = api()
    bundle = valid_bundle()
    bundle["requirement"]["bindings"][binding] = [{"unit_id": "RU-0009", "quote": "伪造锚点", "quote_sha256": "0" * 64}]
    refresh_requirement_chain(bundle)
    assert gate.FactGate.verify(bundle, entry_kind="formal").code == "invalid_source_ref"


def test_fact_024_manifest_drift_is_reported_by_integrity_check(tmp_path: Path) -> None:
    artifacts, gate = api()
    bundle = valid_bundle()
    root = tmp_path / "REQ-001"
    artifacts.write_verified_bundle(root, business(), bundle)
    manifest_file = root / "current" / "fact-bundle.current.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["source_index_sha256"] = "0" * 64
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    issues = gate.check_saved_bundle_integrity(root)
    assert any("manifest" in issue for issue in issues)
