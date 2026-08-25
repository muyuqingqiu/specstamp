from __future__ import annotations

import re
from typing import Any


REQUIREMENT_FACTS_SCHEMA = "sdlc.requirement-facts.v1"
DESIGN_FACTS_SCHEMA = "sdlc.design-facts.v1"
MODEL_REVIEW_SCHEMA = "sdlc.model-review.v1"
FACT_BUNDLE_SCHEMA = "sdlc.fact-bundle.v1"
FORMAL_SOURCE_PROJECTION_SCHEMA = "sdlc.formal-source-projection.v1"
DRAFT_SOURCE_PROJECTION_SCHEMA = "sdlc.draft-source-projection.v1"
SOURCE_INDEX_SCHEMA = "sdlc.source-index.v1"
REVIEW_INPUTS_SCHEMA = "sdlc.review-inputs.v1"

FORMAL_BUSINESS_FIELDS = (
    "title",
    "description",
    "background",
    "goal",
    "user_scenarios",
    "scope",
    "out_of_scope",
    "business_rules",
    "permission_rules",
    "data_state_rules",
    "interface_scope",
    "exception_rules",
    "test_focus",
    "open_questions",
    "decisions",
    "functional_requirements",
    "acceptance_criteria",
    "test_cases",
    "design",
)

FACT_CERTAINTIES = {"confirmed", "ambiguous", "inferred"}
FACT_CATEGORIES = {
    "requirement": {
        "goal", "actor_scenario", "scope", "out_of_scope", "business_rule",
        "permission", "interface", "page_behavior", "state_transition", "error",
        "data_change", "acceptance", "test_case",
    },
    "design": {
        "module", "interface_implementation", "permission_enforcement",
        "state_implementation", "data_implementation", "error_handling",
        "requirement_coverage", "test_strategy", "risk",
    },
}
CATEGORY_NORMALIZED_FIELDS = {
    "goal": {"value"},
    "actor_scenario": {"actor", "scenario", "trigger"},
    "scope": {"included"},
    "business_rule": {"rule"},
    "page_behavior": {"page", "action", "result"},
    "module": {"module"},
    "permission": {"subject", "direction", "action", "resource"},
    "permission_enforcement": {"subject", "direction", "action", "resource"},
    "interface": {"method", "path"},
    "interface_implementation": {"method", "path"},
    "state_transition": {"from", "to", "trigger"},
    "state_implementation": {"from", "to", "trigger"},
    "error": {"condition", "result"},
    "error_handling": {"condition", "result"},
    "data_change": {"entity", "change"},
    "data_implementation": {"entity", "change"},
    "out_of_scope": {"excluded"},
    "acceptance": {"expected"},
    "test_case": {"operation", "expected"},
    "requirement_coverage": {"requirement_fact_id", "design_fact_id", "coverage"},
    "test_strategy": {"strategy"},
    "risk": {"risk", "mitigation"},
}
RELATION_TYPES = {"implements", "verifies", "mitigates"}
REVIEW_STATUSES = {"pending", "needs_review", "needs_user", "passed", "stale", "rejected"}
REVIEW_ISSUE_TYPES = {
    "omitted_fact",
    "meaning_changed",
    "invalid_source_ref",
    "wrong_owner",
    "wrong_section",
    "wrong_relation",
    "coverage_gap",
    "ambiguity_unresolved",
    "requirement_design_conflict",
    "entry_contract_mismatch",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_TARGET_KEYS = {
    "source_projection_sha256",
    "source_index_sha256",
    "requirement_source_sha256",
    "design_source_sha256",
    "questions_sha256",
    "decisions_sha256",
    "context_inputs_sha256",
}


def _sha256_issues(value: object, field: str) -> list[str]:
    return [] if isinstance(value, str) and _SHA256_RE.fullmatch(value) else [f"{field} 必须是 64 位小写 SHA-256。"]


def _context_issues(value: object, *, review: bool = False) -> list[str]:
    if not isinstance(value, dict):
        return ["targets 必须是对象。" if review else "context_targets 必须是对象。"]
    required = set(_CONTEXT_TARGET_KEYS)
    if review:
        required.update(
            {
                "requirement_facts_sha256",
                "design_facts_sha256",
                "requirement_semantic_sha256",
                "design_semantic_sha256",
            }
        )
    issues: list[str] = []
    for name in sorted(required):
        issues.extend(_sha256_issues(value.get(name), name))
    return issues


def schema_issues(document: object, *, expected: str) -> list[str]:
    """只校验机器合同结构，不在 CLI 内猜测自然语言含义。"""

    if not isinstance(document, dict):
        return ["产物必须是 JSON 对象。"]
    issues: list[str] = []
    if document.get("schema") != expected:
        issues.append(f"schema 必须是 {expected}。")
    return issues


def fact_document_issues(
    document: object,
    *,
    owner: str,
    requirement_fact_ids: set[str] | None = None,
) -> list[str]:
    expected = REQUIREMENT_FACTS_SCHEMA if owner == "requirement" else DESIGN_FACTS_SCHEMA
    issues = schema_issues(document, expected=expected)
    if not isinstance(document, dict):
        return issues
    semantic = document.get("semantic")
    if not isinstance(semantic, dict):
        issues.append("semantic 必须是对象。")
        return issues
    for name in ("facts", "relations", "ambiguities"):
        if not isinstance(semantic.get(name), list):
            issues.append(f"semantic.{name} 必须是数组。")
    fact_ids: set[str] = set()
    prefix = "RF-" if owner == "requirement" else "DF-"
    for fact in semantic.get("facts", []) if isinstance(semantic.get("facts"), list) else []:
        if not isinstance(fact, dict):
            issues.append("每条事实必须是对象。")
            continue
        if fact.get("owner") != owner:
            issues.append("事实 owner 与文件类型不一致。")
        fact_id = fact.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id.startswith(prefix):
            issues.append(f"事实 fact_id 必须使用 {prefix} 编号。")
        elif fact_id in fact_ids:
            issues.append(f"事实编号重复：{fact_id}。")
        else:
            fact_ids.add(fact_id)
        category = fact.get("category")
        if category not in FACT_CATEGORIES[owner]:
            issues.append(f"事实 category 不受支持：{category}。")
        if not isinstance(fact.get("statement"), str) or not fact.get("statement"):
            issues.append("事实 statement 不能为空。")
        if not isinstance(fact.get("normalized"), dict):
            issues.append("事实 normalized 必须是对象。")
        elif category in CATEGORY_NORMALIZED_FIELDS:
            missing_normalized = sorted(
                name for name in CATEGORY_NORMALIZED_FIELDS[category]
                if not isinstance(fact["normalized"].get(name), str) or not fact["normalized"][name].strip()
            )
            if missing_normalized:
                issues.append(f"{category} 的 normalized 缺少字段：{', '.join(missing_normalized)}。")
        if fact.get("certainty") not in FACT_CERTAINTIES:
            issues.append("事实 certainty 不受支持。")
        if not isinstance(fact.get("source_refs"), list) or not fact.get("source_refs"):
            issues.append("每条事实至少要引用一个原文单元。")
        decision_refs = fact.get("decision_refs")
        if not isinstance(decision_refs, list) or not all(isinstance(item, str) and item for item in decision_refs):
            issues.append("事实 decision_refs 必须是数组。")
        elif len(decision_refs) != len(set(decision_refs)):
            issues.append("事实 decision_refs 不能重复。")
        if "ambiguity_id" not in fact:
            issues.append("事实必须显式提供 ambiguity_id。")
    relations = semantic.get("relations", []) if isinstance(semantic.get("relations"), list) else []
    if owner == "requirement" and relations:
        issues.append("需求事实文件不能声明技术实现关系。")
    seen_relations: set[tuple[str, str, str]] = set()
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != {"type", "requirement_fact_id", "design_fact_id"}:
            issues.append("关系必须完整提供 type、requirement_fact_id 和 design_fact_id。")
            continue
        relation_key = (str(relation["type"]), str(relation["requirement_fact_id"]), str(relation["design_fact_id"]))
        if relation_key in seen_relations:
            issues.append("关系不能重复。")
        seen_relations.add(relation_key)
        if relation["type"] not in RELATION_TYPES:
            issues.append("关系类型不受支持。")
        if relation["design_fact_id"] not in fact_ids:
            issues.append("关系引用了不存在的技术事实。")
        if requirement_fact_ids is not None and relation["requirement_fact_id"] not in requirement_fact_ids:
            issues.append("关系引用了不存在的需求事实。")
    relation_pairs = {
        (str(item.get("requirement_fact_id")), str(item.get("design_fact_id")))
        for item in relations if isinstance(item, dict)
    }
    if owner == "design":
        for fact in semantic.get("facts", []):
            if not isinstance(fact, dict) or fact.get("category") != "requirement_coverage":
                continue
            normalized = fact.get("normalized", {})
            requirement_fact_id = str(normalized.get("requirement_fact_id") or "") if isinstance(normalized, dict) else ""
            design_fact_id = str(normalized.get("design_fact_id") or "") if isinstance(normalized, dict) else ""
            if design_fact_id != fact.get("fact_id"):
                issues.append("requirement_coverage 的 design_fact_id 必须指向当前技术事实自身。")
            if requirement_fact_ids is not None and requirement_fact_id not in requirement_fact_ids:
                issues.append("requirement_coverage 引用了不存在的需求事实。")
            if (requirement_fact_id, design_fact_id) not in relation_pairs:
                issues.append("requirement_coverage 的机器引用与当前技术关系不一致。")
    issues.extend(_sha256_issues(document.get("semantic_sha256"), "semantic_sha256"))
    issues.extend(_sha256_issues(document.get("artifact_sha256"), "artifact_sha256"))
    issues.extend(_context_issues(document.get("context_targets")))
    bindings = document.get("bindings")
    if not isinstance(bindings, dict):
        issues.append("bindings 必须是对象。")
    else:
        for name in ("origin_refs", "formal_refs"):
            if not isinstance(bindings.get(name), list):
                issues.append(f"bindings.{name} 必须是数组。")
    producer = document.get("producer")
    if not isinstance(producer, dict) or producer.get("kind") != "model" or not producer.get("actor_id"):
        issues.append("producer 必须记录模型产出角色。")
    if not isinstance(document.get("coverage"), list):
        issues.append("coverage 必须是数组。")
    else:
        seen_coverage: set[str] = set()
        for item in document["coverage"]:
            if not isinstance(item, dict) or item.get("status") not in {"covered", "non_contractual", "ambiguous"}:
                issues.append("coverage 条目状态无效。")
                continue
            allowed_coverage_fields = {"unit_id", "fact_ids", "status", "reason", "decision_refs", "approval_refs"}
            if not {"unit_id", "fact_ids", "status", "reason"} <= set(item) or not set(item) <= allowed_coverage_fields:
                issues.append("coverage 条目字段不完整或包含未定义字段。")
            if not isinstance(item.get("unit_id"), str) or not isinstance(item.get("fact_ids"), list):
                issues.append("coverage 条目必须提供 unit_id 和 fact_ids。")
            elif item["unit_id"] in seen_coverage:
                issues.append("coverage 不能重复记录同一内容单元。")
            else:
                seen_coverage.add(item["unit_id"])
            coverage_fact_ids = item.get("fact_ids")
            if isinstance(coverage_fact_ids, list):
                if not all(isinstance(value, str) for value in coverage_fact_ids):
                    issues.append("coverage 的 fact_ids 必须是事实编号字符串。")
                elif len(coverage_fact_ids) != len(set(coverage_fact_ids)):
                    issues.append("coverage 的 fact_ids 不能重复。")
            for ref_name in ("decision_refs", "approval_refs"):
                refs = item.get(ref_name, [])
                if not isinstance(refs, list) or not all(isinstance(value, str) for value in refs):
                    issues.append(f"coverage 的 {ref_name} 必须是编号字符串数组。")
                elif len(refs) != len(set(refs)):
                    issues.append(f"coverage 的 {ref_name} 不能重复。")
            if item.get("status") == "non_contractual" and not item.get("reason"):
                issues.append("non_contractual 覆盖必须说明原因。")
    return issues


def review_document_issues(document: object) -> list[str]:
    issues = schema_issues(document, expected=MODEL_REVIEW_SCHEMA)
    if not isinstance(document, dict):
        return issues
    if document.get("status") not in REVIEW_STATUSES:
        issues.append("复核状态不受支持。")
    if not isinstance(document.get("review_id"), str) or not document.get("review_id"):
        issues.append("review_id 不能为空。")
    issues.extend(_context_issues(document.get("targets"), review=True))
    reviewer = document.get("reviewer")
    if not isinstance(reviewer, dict) or reviewer.get("role") != "independent_checker" or not reviewer.get("actor_id"):
        issues.append("reviewer 必须记录独立复核角色。")
    if document.get("coverage_status") not in {"complete", "incomplete"}:
        issues.append("coverage_status 不受支持。")
    issues.extend(_sha256_issues(document.get("artifact_sha256"), "artifact_sha256"))
    review_issues = document.get("issues")
    if not isinstance(review_issues, list):
        issues.append("issues 必须是数组。")
    else:
        for item in review_issues:
            if not isinstance(item, dict) or item.get("type") not in REVIEW_ISSUE_TYPES:
                issues.append("复核问题类型不受支持。")
            elif not all(isinstance(item.get(name), str) and item.get(name) for name in ("issue_id", "severity", "message", "recovery")):
                issues.append("复核问题必须提供编号、级别、说明和恢复动作。")
    approvals = document.get("non_contractual_approvals", [])
    if not isinstance(approvals, list):
        issues.append("non_contractual_approvals 必须是数组。")
    else:
        seen_approvals: set[str] = set()
        for approval in approvals:
            required = {"approval_id", "unit_id", "owner", "reason", "decision_refs"}
            if not isinstance(approval, dict) or set(approval) != required:
                issues.append("非合同批准必须完整绑定批准编号、内容单元、owner、理由和决定引用。")
                continue
            if approval["approval_id"] in seen_approvals:
                issues.append("非合同批准编号不能重复。")
            seen_approvals.add(approval["approval_id"])
            decision_refs = approval.get("decision_refs")
            if approval["owner"] not in {"requirement", "design"} or not all(
                isinstance(approval.get(name), str) and approval[name] for name in ("approval_id", "unit_id", "reason")
            ) or not isinstance(decision_refs, list):
                issues.append("非合同批准字段无效。")
            elif not decision_refs or not all(isinstance(item, str) and item for item in decision_refs):
                issues.append("非合同批准必须引用至少一条真实用户决定。")
            elif len(decision_refs) != len(set(decision_refs)):
                issues.append("非合同批准的决定引用不能重复。")
    return issues


def formal_business_issues(document: object) -> list[str]:
    if not isinstance(document, dict):
        return ["正式业务内容必须是 JSON 对象。"]
    missing = [name for name in FORMAL_BUSINESS_FIELDS if name not in document]
    entry_metadata = {
        "formal_contract_version",
        "source_draft_id",
        "fact_bundle",
        "slug",
        "summary",
        "create_design_event",
        "source_refs",
        "flow_type",
    }
    extra = sorted(set(document) - set(FORMAL_BUSINESS_FIELDS) - entry_metadata)
    issues = [f"正式业务内容缺少字段：{name}。" for name in missing]
    issues.extend(f"正式业务内容包含未登记字段：{name}。" for name in extra)
    if "open_questions" in document and not isinstance(document["open_questions"], list):
        issues.append("open_questions 必须是数组。")
    if "decisions" in document and not isinstance(document["decisions"], list):
        issues.append("decisions 必须是数组。")
    return issues
