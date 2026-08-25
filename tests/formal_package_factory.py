from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
from typing import Any

from codex_sdlc.core import draft_contract, draft_sections, fact_artifacts, fact_review_trust, fact_schema
from codex_sdlc.core.formal_manifest_contract import (
    build_document_first_formal_package,
)
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.services.draft_service import draft_source_projection
from codex_sdlc.services.start_service import normalize_start_package


def build_document_first_formal_v3_package(
    project_dir: Path,
    *,
    draft_id: str = "DRAFT-001",
) -> dict[str, Any]:
    """从真实 start_ready DRAFT 生成新流程清单，夹具不复制正文或生成 facts。"""

    package = build_document_first_formal_package(
        build_paths(project_dir),
        draft_id,
    )
    return deepcopy(package)


def write_document_first_formal_v3_package(
    project_dir: Path,
    *,
    draft_id: str = "DRAFT-001",
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """把文档优先正式包写到显式测试路径，历史 facts 旁路文件不会一起生成。"""

    package = build_document_first_formal_v3_package(
        project_dir,
        draft_id=draft_id,
    )
    target = output_path or (project_dir / f"{draft_id}-formal.v3.json")
    fact_artifacts.write_json(target, package)
    return target, package


def install_fixture_receipt(
    project_dir: Path,
    *,
    requirement: dict[str, Any],
    design: dict[str, Any],
    review: dict[str, Any],
    draft_id: str = "FORMAL",
) -> str:
    """只给非信任主题的成功夹具准备登记；信任合同测试必须走公开 CLI。"""

    paths = build_paths(project_dir)
    previous = os.environ.get("CODEX_THREAD_ID")
    try:
        os.environ["CODEX_THREAD_ID"] = "fixture-producer-thread"
        requirement_run = fact_review_trust.record_fact_run(
            paths, draft_id=draft_id, owner="requirement",
            artifact_sha256=fact_artifacts.artifact_sha256(requirement),
        )
        design_run = fact_review_trust.record_fact_run(
            paths, draft_id=draft_id, owner="design",
            artifact_sha256=fact_artifacts.artifact_sha256(design),
        )
        target = fact_review_trust.review_target_sha256(requirement, design, review["targets"])
        request = fact_review_trust.create_review_request(
            paths,
            draft_id=draft_id,
            target_sha256=target,
            fact_run_ids=[requirement_run["record_id"], design_run["record_id"]],
            entry_scope="formal",
        )
        os.environ["CODEX_THREAD_ID"] = "fixture-reviewer-thread"
        receipt = fact_review_trust.submit_review(
            paths,
            request_id=request["request_id"],
            target_sha256=target,
            review_sha256=fact_artifacts.artifact_sha256(review),
        )
        return str(receipt["receipt_id"])
    finally:
        if previous is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = previous


def formal_v2_package() -> dict[str, Any]:
    """返回当前业务字段夹具；函数名暂留，避免一次改动掩盖测试本身的意图。"""

    return {
        "title": "订单审批",
        "description": "管理员审批本人负责的待处理订单。",
        "background": "订单需要经过明确审批后才能继续处理。",
        "goal": "提供可追溯的订单审批能力。",
        "user_scenarios": ["管理员查看并审批本人负责的订单。"],
        "scope": ["支持管理员审批本人负责的订单。"],
        "out_of_scope": ["不涉及：不处理批量审批。"],
        "business_rules": ["每个订单只能完成一次审批。"],
        "permission_rules": ["只允许管理员审批本人负责的订单。"],
        "data_state_rules": ["pending -> approved，触发条件：管理员确认审批。"],
        "interface_scope": [
            "POST /orders/{id}/approve；请求字段：comment；响应字段：id,status；状态码：200,403。"
        ],
        "exception_rules": ["无权限返回 ORDER_FORBIDDEN，HTTP 状态：403，不重试。"],
        "test_focus": ["覆盖本人数据、越权数据、重复审批和接口字段。"],
        "open_questions": [],
        "decisions": ["使用单笔审批并保留权限审计。"],
        "functional_requirements": [
            {
                "id": "FR-001",
                "title": "审批订单",
                "description": "管理员审批本人负责的待处理订单。",
                "rules": ["订单状态必须为 pending。"],
                "inputs": ["订单编号和审批说明。"],
                "outputs": ["审批后的订单编号和状态。"],
                "triggers": ["管理员确认审批。"],
                "data_changes": ["把订单状态从 pending 改为 approved。"],
                "permissions": ["只允许管理员审批本人负责的订单。"],
                "exceptions": ["无权限返回 ORDER_FORBIDDEN 和 HTTP 403。"],
                "boundaries": ["不涉及：不处理批量审批。"],
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "title": "本人订单审批成功",
                "requirement_ids": ["FR-001"],
                "operation": "管理员审批本人负责的 pending 订单。",
                "expected": "订单状态变为 approved。",
                "pass_standard": "接口返回 200，响应包含订单编号和 approved 状态。",
            }
        ],
        "test_cases": [
            {
                "id": "TC-001",
                "acceptance_ids": ["AC-001"],
                "requirement_ids": ["FR-001"],
                "description": "验证本人订单审批成功。",
                "type": "api_test",
                "operation": "调用 POST /orders/{id}/approve。",
                "expected": "返回订单编号和 approved 状态。",
                "pass_standard": "HTTP 状态为 200，字段和值与合同一致。",
            }
        ],
        "design": {
            "title": "订单审批技术方案",
            "summary": "在订单服务中增加审批用例并沿用现有存储。",
            "technical_goal": "实现可追溯的单订单审批。",
            "modules": ["订单应用服务和订单存储。"],
            "data_structures": ["订单表写入 status 和 approved_at。"],
            "interfaces": [
                "POST /orders/{id}/approve；请求字段：comment；响应字段：id,status；状态码：200,403。"
            ],
            "state_flow": ["pending -> approved，触发条件：管理员确认审批。"],
            "data_flow": ["接口校验权限后更新订单并返回最新状态。"],
            "permissions_security": ["只允许管理员审批本人负责的订单。"],
            "error_handling": ["无权限返回 ORDER_FORBIDDEN，HTTP 状态：403，不重试。"],
            "test_strategy": ["执行接口、权限、状态和重复审批回归。"],
            "risks": ["并发重复审批；使用条件更新保证只成功一次。"],
            "out_of_scope": ["不涉及：不处理批量审批。"],
            "requirement_coverage": ["FR-001：由订单审批接口、权限校验和状态更新共同覆盖。"],
        },
    }


def copied_formal_v2_package() -> dict[str, Any]:
    return deepcopy(formal_v2_package())


def _fixture_semantic(owner: str, index: dict[str, Any]) -> dict[str, Any]:
    """为非语义主题的成功测试生成最小合法事实，不证明一段原文已经穷尽全部事实。

    混合语义、漏项和改义测试必须显式手写事实及逐项关系，不能用这个便利函数的 passed 结果
    代替模型语义完整性证明。
    """

    units = [item for item in index["units"] if item["owner"] == owner and item.get("classification") != "structural"]
    prefix = "RF" if owner == "requirement" else "DF"
    def category_for(unit: dict[str, Any], target_owner: str = owner) -> str:
        section = str(unit.get("section_key") or "")
        pointer = str(unit.get("json_pointer") or "")
        quote = str(unit.get("quote") or "")
        if target_owner == "requirement":
            if section in {"title", "description", "background", "goal", "背景和目标"}:
                return "goal"
            if section in {"user_scenarios", "用户和使用场景"}: return "actor_scenario"
            if section in {"scope", "本轮范围", "必须做"}: return "scope"
            if section in {"out_of_scope", "不做范围", "本轮不做", "暂不做"} or "/boundaries/" in pointer or quote.startswith("- 边界："): return "out_of_scope"
            if section in {"permission_rules", "权限规则"} or "/permissions/" in pointer or quote.startswith("- 权限："): return "permission"
            if section.endswith("/权限"): return "permission"
            if section == "interface_scope": return "interface"
            if section == "接口或页面范围": return "page_behavior" if quote.startswith("- 页面：") else "interface"
            if section in {"data_state_rules", "数据和状态规则"}: return "state_transition"
            if section == "exception_rules" or "异常" in section or "/exceptions/" in pointer or quote.startswith("- 异常："): return "error"
            if section.endswith("/边界"): return "out_of_scope"
            if "/data_changes/" in pointer or quote.startswith("- 保存数据：") or section.endswith("/保存数据"): return "data_change"
            if section == "acceptance_criteria" or section.startswith("AC-"): return "acceptance"
            if section in {"test_cases", "test_focus", "测试关注点"} or section.startswith("TC-"): return "test_case"
            return "business_rule"
        if "/interfaces/" in pointer or section == "接口设计": return "interface_implementation"
        if "/permissions_security/" in pointer or section == "权限和安全": return "permission_enforcement"
        if "/state_flow/" in pointer or section == "状态流": return "state_implementation"
        if "/data_structures/" in pointer or "/data_flow/" in pointer or section in {"数据结构", "数据流"}: return "data_implementation"
        if "/error_handling/" in pointer or section == "错误处理": return "error_handling"
        if "/requirement_coverage/" in pointer or "/out_of_scope/" in pointer or section in {"本轮不做", "对需求草稿的覆盖说明"}: return "requirement_coverage"
        if "/test_strategy/" in pointer or section == "测试策略": return "test_strategy"
        if "/risks/" in pointer or "风险" in section: return "risk"
        return "module"

    def normalized_for(category: str, unit: dict[str, Any], fact_id: str) -> dict[str, str]:
        source = str(unit["unit_id"])
        values = {
            "goal": {"value": source}, "actor_scenario": {"actor": "用户", "scenario": source, "trigger": "业务触发"},
            "scope": {"included": source}, "out_of_scope": {"excluded": source}, "business_rule": {"rule": source},
            "permission": {"subject": "业务角色", "direction": "allow", "action": "执行", "resource": source},
            "interface": {"method": "POST", "path": f"/fixture/{source.lower()}"},
            "page_behavior": {"page": "业务页面", "action": "操作", "result": source},
            "state_transition": {"from": "before", "to": "after", "trigger": source},
            "error": {"condition": source, "result": "业务错误"},
            "data_change": {"entity": "fixture", "change": source},
            "acceptance": {"expected": source}, "test_case": {"operation": source, "expected": "通过"},
            "module": {"module": source}, "interface_implementation": {"method": "POST", "path": f"/fixture/{source.lower()}"},
            "permission_enforcement": {"subject": "业务角色", "direction": "allow", "action": "执行", "resource": source},
            "state_implementation": {"from": "before", "to": "after", "trigger": source},
            "data_implementation": {"entity": "fixture", "change": source}, "error_handling": {"condition": source, "result": "业务错误"},
            "requirement_coverage": {"requirement_fact_id": "RF-0001", "design_fact_id": fact_id, "coverage": source},
            "test_strategy": {"strategy": source}, "risk": {"risk": source, "mitigation": "执行保护措施"},
        }
        return values[category]
    facts = []
    for number, unit in enumerate(units, 1):
        category = category_for(unit)
        fact_id = f"{prefix}-{number:04d}"
        facts.append(
            {
                "fact_id": fact_id,
                "category": category,
                "owner": owner,
                "statement": unit["quote"],
                "normalized": normalized_for(category, unit, fact_id),
                "certainty": "confirmed",
                "source_refs": [unit["unit_id"]],
                "decision_refs": [],
                "ambiguity_id": None,
            }
        )
    relations = []
    if owner == "design":
        requirement_units = [
            item for item in index["units"] if item["owner"] == "requirement" and item.get("classification") != "structural"
        ]
        category_target = {
            "permission": "permission_enforcement", "interface": "interface_implementation",
            "state_transition": "state_implementation", "error": "error_handling",
            "data_change": "data_implementation", "out_of_scope": "requirement_coverage",
        }
        design_by_category: dict[str, list[str]] = {}
        for item in facts:
            design_by_category.setdefault(item["category"], []).append(item["fact_id"])
        requirement_categories = [category_for(unit, "requirement") for unit in requirement_units]
        fallback_ids = design_by_category.get("requirement_coverage") or [facts[0]["fact_id"]]
        category_offsets: dict[str, int] = {}
        relations = []
        for number, requirement_category in enumerate(requirement_categories, 1):
            target_category = category_target.get(requirement_category)
            candidates = design_by_category.get(target_category or "", fallback_ids)
            offset_key = target_category or "requirement_coverage"
            offset = category_offsets.get(offset_key, 0)
            design_fact_id = candidates[offset % len(candidates)]
            category_offsets[offset_key] = offset + 1
            relations.append({"type": "implements", "requirement_fact_id": f"RF-{number:04d}", "design_fact_id": design_fact_id})
        # requirement_coverage 的机器编号必须与当前事实及至少一条真实关系一致，
        # 不能保留为了满足非空校验而写死的占位编号。
        for fact in facts:
            if fact["category"] != "requirement_coverage":
                continue
            relation = next((item for item in relations if item["design_fact_id"] == fact["fact_id"]), None)
            if relation is not None:
                fact["normalized"]["requirement_fact_id"] = relation["requirement_fact_id"]
                fact["normalized"]["design_fact_id"] = fact["fact_id"]
    return {"facts": facts, "relations": relations, "ambiguities": []}


def build_valid_formal_v3_bundle(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """构造没有 workflow_profile 的历史 facts 档案，只供历史只读合同测试。"""

    business = deepcopy(data)
    for derived in (
        "formal_contract_version",
        "fact_bundle",
        "source_draft_id",
        "slug",
        "summary",
        "create_design_event",
        "risks",
        "assumptions",
        "source_refs",
        "flow_type",
    ):
        business.pop(derived, None)
    description = str(business.get("description") or business.get("title") or "正式需求")
    business.setdefault("background", description)
    business.setdefault("goal", description)
    business.setdefault("open_questions", [])
    business.setdefault("decisions", [])
    source = fact_artifacts.build_formal_source_projection(business)
    index = fact_artifacts.build_source_index(source, source_kind="formal")
    targets = fact_artifacts.build_context_targets(source, index)
    requirement = fact_artifacts.build_fact_artifact("requirement", _fixture_semantic("requirement", index), targets, index)
    design = fact_artifacts.build_fact_artifact("design", _fixture_semantic("design", index), targets, index)
    review = fact_artifacts.build_review_artifact(requirement, design, targets, status="passed", issues=[])
    manifest = fact_artifacts.build_fact_manifest(source, index, requirement, design, review)
    formal = {"formal_contract_version": "formal.v3", **business, "fact_bundle": manifest}
    for metadata in ("slug", "summary", "create_design_event", "source_refs", "flow_type"):
        if metadata in data:
            formal[metadata] = deepcopy(data[metadata])
    if data.get("source_draft_id"):
        formal["source_draft_id"] = data["source_draft_id"]
    return formal, {"source": source, "index": index, "requirement": requirement, "design": design, "review": review, "manifest": manifest}


def write_formal_v3_package(path: Path, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """写历史 facts 正式包和四份旁路产物，不得用于文档优先建档测试。"""

    formal, bundle = build_valid_formal_v3_bundle(data or formal_v2_package())
    fact_artifacts.write_json(path, formal)
    fact_artifacts.write_json(path.with_name("source-index.json"), bundle["index"])
    fact_artifacts.write_json(path.with_name("requirement.facts.json"), bundle["requirement"])
    fact_artifacts.write_json(path.with_name("design.facts.json"), bundle["design"])
    fact_artifacts.write_json(path.with_name("model-review.json"), bundle["review"])
    install_fixture_receipt(
        path.parent, requirement=bundle["requirement"], design=bundle["design"], review=bundle["review"]
    )
    return formal


def build_valid_draft_fact_bundle(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = draft_source_projection(draft)
    draft_id = str(draft.get("draft_id") or "DRAFT-001")
    index = fact_artifacts.build_source_index(source, source_kind="draft", draft_id=draft_id)
    targets = fact_artifacts.build_context_targets(source, index)
    requirement = fact_artifacts.build_fact_artifact("requirement", _fixture_semantic("requirement", index), targets, index, draft_id=draft_id)
    design = fact_artifacts.build_fact_artifact("design", _fixture_semantic("design", index), targets, index, draft_id=draft_id)
    review = fact_artifacts.build_review_artifact(requirement, design, targets, status="passed", issues=[])
    manifest = fact_artifacts.build_fact_manifest(source, index, requirement, design, review)
    return {"source": source, "index": index, "requirement": requirement, "design": design, "review": review, "manifest": manifest}


_FIXTURE_PREFIXES = (
    "页面：", "接口：", "权限：", "异常回退：", "错误处理：", "异常：", "边界：", "保存数据：", "说明：", "规则：",
    "输入：", "输出：", "触发条件：", "状态值：", "错误码：", "预期：", "操作：", "通过标准：", "不做：",
)


def _fixture_text(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("- "):
        text = text[2:].strip()
    for prefix in _FIXTURE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.rstrip("。；; ")


def _materialize_fixture_source_refs(
    semantic: dict[str, Any], formal_index: dict[str, Any], *, owner: str,
    non_contractual_unit_ids: set[str] | None = None,
) -> dict[str, Any]:
    """按正式 JSON Pointer 类别和明确原文逐项绑定；无法唯一解释的夹具直接失败。"""

    result = deepcopy(semantic)
    facts = [item for item in result.get("facts", []) if isinstance(item, dict)]
    for fact in facts:
        fact["source_refs"] = []
    for unit in formal_index.get("units", []):
        if (
            not isinstance(unit, dict)
            or unit.get("classification") == "structural"
            or unit.get("owner") != owner
            or str(unit.get("unit_id")) in (non_contractual_unit_ids or set())
        ):
            continue
        allowed = fact_artifacts.formal_unit_allowed_categories(unit)
        candidates = [fact for fact in facts if fact.get("category") in allowed]
        if not candidates:
            raise ValueError(f"正式来源 {unit.get('json_pointer')} 没有兼容事实。")
        unit_text = _fixture_text(unit.get("quote"))
        exact = [fact for fact in candidates if _fixture_text(fact.get("statement")) == unit_text]
        selected = exact
        pointer = str(unit.get("json_pointer") or "")
        if not selected and len(candidates) == 1:
            selected = candidates
        if not selected and pointer in {"/title", "/design/title", "/design/summary", "/design/technical_goal"}:
            # 标题和方案摘要是固定投影字段，明确归到当前第一条目标或模块事实，不参与高风险类别兜底。
            selected = [candidates[0]]
        if not selected:
            raise ValueError(f"正式来源 {pointer} 无法唯一对应当前事实，请拆出明确测试样本。")
        for fact in selected:
            fact["source_refs"].append(str(unit["unit_id"]))
    missing = [str(fact.get("fact_id")) for fact in facts if not fact.get("source_refs")]
    if missing:
        raise ValueError("存在没有正式来源映射的事实：" + "、".join(missing))
    return result


def _non_contractual_unit_mapping(
    origin_document: dict[str, Any], origin_index: dict[str, Any], formal_index: dict[str, Any]
) -> dict[str, str]:
    origin_units = {
        str(item["unit_id"]): item for item in origin_index.get("units", []) if isinstance(item, dict)
    }
    formal_units = [
        item for item in formal_index.get("units", [])
        if isinstance(item, dict) and item.get("classification") != "structural"
    ]
    unit_mapping: dict[str, str] = {}
    for item in origin_document.get("coverage", []):
        if not isinstance(item, dict) or item.get("status") != "non_contractual":
            continue
        origin_unit = origin_units.get(str(item.get("unit_id") or ""))
        if origin_unit is None:
            raise ValueError("non_contractual 覆盖引用了不存在的 DRAFT 单元。")
        if origin_unit.get("classification") == "structural":
            continue
        matches = [
            unit for unit in formal_units
            if unit.get("owner") == origin_unit.get("owner")
            and _fixture_text(unit.get("quote")) == _fixture_text(origin_unit.get("quote"))
        ]
        section = str(origin_unit.get("section_key") or "")
        preferred_prefixes = {
            "业务规则": ("/business_rules/",),
            "权限规则": ("/permission_rules/",),
            "数据和状态规则": ("/data_state_rules/",),
            "接口或页面范围": ("/interface_scope/",),
            "异常和边界": ("/exception_rules/",),
            "不做范围": ("/out_of_scope/",),
            "本轮不做": ("/design/out_of_scope/",),
        }.get(section)
        if preferred_prefixes:
            matches = [
                unit for unit in matches
                if str(unit.get("json_pointer") or "").startswith(preferred_prefixes)
            ]
        if len(matches) != 1:
            raise ValueError("non_contractual 内容无法唯一映射到正式来源。")
        unit_mapping[str(origin_unit["unit_id"])] = str(matches[0]["unit_id"])
    return unit_mapping


def _formal_coverage(
    semantic: dict[str, Any], origin_document: dict[str, Any], unit_mapping: dict[str, str]
) -> list[dict[str, Any]]:
    facts_by_unit: dict[str, list[str]] = {}
    for fact in semantic.get("facts", []):
        for unit_id in fact.get("source_refs", []):
            facts_by_unit.setdefault(str(unit_id), []).append(str(fact["fact_id"]))
    coverage = [
        {"unit_id": unit_id, "fact_ids": fact_ids, "status": "covered", "reason": ""}
        for unit_id, fact_ids in facts_by_unit.items()
    ]
    for item in origin_document.get("coverage", []):
        if not isinstance(item, dict) or item.get("status") != "non_contractual":
            continue
        origin_unit_id = str(item.get("unit_id") or "")
        if origin_unit_id not in unit_mapping:
            continue
        copied = deepcopy(item)
        copied["unit_id"] = unit_mapping[origin_unit_id]
        coverage.append(copied)
    return coverage


def write_formal_v3_from_draft(
    path: Path,
    data: dict[str, Any],
    draft: dict[str, Any],
    *,
    install_receipt: bool = True,
) -> dict[str, Any]:
    """把已复核 DRAFT 事实只做来源物化，semantic payload 保持原样。"""

    business = {key: deepcopy(data[key]) for key in fact_schema.FORMAL_BUSINESS_FIELDS if key in data}
    source = fact_artifacts.build_formal_source_projection(business)
    index = fact_artifacts.build_source_index(source, source_kind="formal")
    targets = fact_artifacts.build_context_targets(source, index)
    origin_requirement = deepcopy(draft["requirement_facts"])
    origin_design = deepcopy(draft["design_facts"])
    origin_index = draft["fact_source_index"]
    materialized_requirement = fact_artifacts.materialize_fact_document_decision_refs(
        origin_requirement, origin_index, index
    )
    materialized_design = fact_artifacts.materialize_fact_document_decision_refs(
        origin_design, origin_index, index
    )
    requirement_unit_mapping = _non_contractual_unit_mapping(
        materialized_requirement, origin_index, index
    )
    design_unit_mapping = _non_contractual_unit_mapping(materialized_design, origin_index, index)
    requirement_semantic = _materialize_fixture_source_refs(
        materialized_requirement["semantic"], index, owner="requirement",
        non_contractual_unit_ids=set(requirement_unit_mapping.values()),
    )
    design_semantic = _materialize_fixture_source_refs(
        materialized_design["semantic"], index, owner="design",
        non_contractual_unit_ids=set(design_unit_mapping.values()),
    )
    requirement_coverage = _formal_coverage(
        requirement_semantic, materialized_requirement, requirement_unit_mapping
    )
    design_coverage = _formal_coverage(design_semantic, materialized_design, design_unit_mapping)
    unit_mapping = {**requirement_unit_mapping, **design_unit_mapping}
    requirement = fact_artifacts.build_fact_artifact(
        "requirement", requirement_semantic, targets, index, coverage=requirement_coverage
    )
    design = fact_artifacts.build_fact_artifact(
        "design", design_semantic, targets, index, coverage=design_coverage
    )
    requirement["bindings"]["origin_refs"] = deepcopy(origin_requirement["bindings"]["origin_refs"])
    design["bindings"]["origin_refs"] = deepcopy(origin_design["bindings"]["origin_refs"])
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)
    design["artifact_sha256"] = fact_artifacts.artifact_sha256(design)
    approvals = fact_artifacts.materialize_review_approvals(
        draft.get("model_review", {}), origin_index, index, unit_mapping=unit_mapping
    )
    review = fact_artifacts.build_review_artifact(
        requirement, design, targets, status="passed", issues=[], non_contractual_approvals=approvals
    )
    manifest = fact_artifacts.build_fact_manifest(source, index, requirement, design, review)
    formal = {
        "formal_contract_version": "formal.v3",
        **business,
        "source_draft_id": str(draft["draft_id"]),
        "fact_bundle": manifest,
    }
    for metadata in ("slug", "summary", "create_design_event", "source_refs", "flow_type"):
        if metadata in data:
            formal[metadata] = deepcopy(data[metadata])
    fact_artifacts.write_json(path, formal)
    fact_artifacts.write_json(path.with_name("source-index.json"), index)
    fact_artifacts.write_json(path.with_name("requirement.facts.json"), requirement)
    fact_artifacts.write_json(path.with_name("design.facts.json"), design)
    fact_artifacts.write_json(path.with_name("model-review.json"), review)
    if install_receipt:
        install_fixture_receipt(
            path.parent, requirement=requirement, design=design, review=review, draft_id=str(draft["draft_id"])
        )
    return formal


def install_valid_draft_facts(project_dir: Path, run_cli, draft_id: str = "DRAFT-001") -> dict[str, Any]:
    """通过真实 draft 子命令写入测试事实，验证事件和物化链路。"""

    result = run_cli(["draft", "source-index", draft_id], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    draft = derive_state(build_paths(project_dir))["drafts"][draft_id]
    bundle = build_valid_draft_fact_bundle(draft)
    fixture_dir = project_dir / ".fact-fixtures"
    fixture_dir.mkdir(exist_ok=True)
    for kind in ("requirement", "design"):
        path = fixture_dir / f"{kind}.facts.json"
        fact_artifacts.write_json(path, bundle[kind])
        result = run_cli(
            ["draft", "facts", draft_id, "--kind", kind, "--file", str(path)],
            cwd=project_dir,
            extra_env={"CODEX_THREAD_ID": "test-producer-thread"},
        )
        assert result.returncode == 0, result.stderr
    request = run_cli(
        ["draft", "review-request", draft_id],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "test-producer-thread"},
    )
    assert request.returncode == 0, request.stderr
    review_path = fixture_dir / "model-review.json"
    fact_artifacts.write_json(review_path, bundle["review"])
    result = run_cli(
        ["draft", "model-review", draft_id, "--file", str(review_path)],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "test-reviewer-thread"},
    )
    assert result.returncode == 0, result.stderr
    return derive_state(build_paths(project_dir))["drafts"][draft_id]


def start_package_from_draft_fixture(draft: dict[str, Any]) -> dict[str, Any]:
    """测试夹具模拟模型生成 formal.v3；生产 CLI 不再从 Markdown 推断正式业务结构。"""

    draft_id = str(draft.get("draft_id") or "DRAFT").strip()
    title = str(draft.get("title") or draft_id).strip()
    requirement_body = str(draft.get("requirement_body") or "").strip()
    design_body = str(draft.get("design_body") or "").strip()

    def requirement_lines(section_key: str) -> list[str]:
        return draft_sections.requirement_section_clean_lines(
            requirement_body,
            section_key,
            pending_markers=draft_contract.PENDING_MARKERS,
        )

    def design_lines(*headings: str) -> list[str]:
        body = draft_contract.markdown_section_body(design_body, headings)
        return [line.strip(" \t-•。；;") for line in body.splitlines() if line.strip(" \t-•。；;")]

    scope_body = draft_contract.markdown_section_body(requirement_body, ("本轮范围",))
    scope: list[str] = []
    in_negative_subsection = False
    for raw_line in scope_body.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            in_negative_subsection = heading.group(1).strip() in {"本轮不做", "不做范围"}
            continue
        clean = re.sub(r"^(?:[-*•]|\d+[.、])\s*", "", line).strip(" \t。；;")
        if clean and not in_negative_subsection and not draft_contract.is_placeholder_text(clean):
            scope.append(clean)

    decisions = [str(item).strip() for item in draft.get("decisions", []) if str(item).strip()]
    requirement_points = draft_contract.fr_items_from_markdown(requirement_body, draft_id, title, decisions)
    acceptance_points = draft_contract.ac_items_from_markdown(requirement_body, requirement_points)
    test_cases = draft_contract.tc_items_from_markdown(requirement_body, acceptance_points)
    technical_goal = "\n".join(design_lines("技术目标")).strip()
    design_summary = str(draft.get("design_summary") or "正式技术方案").strip()
    return normalize_start_package(
        {
            "title": title,
            "slug": draft_id.lower(),
            "description": requirement_body,
            "summary": str(draft.get("requirement_summary") or title).strip(),
            "background": "\n".join(requirement_lines("background")),
            "goal": "\n".join(requirement_lines("background")),
            "user_scenarios": requirement_lines("user_scenarios"),
            "scope": list(dict.fromkeys(scope)),
            "out_of_scope": requirement_lines("out_of_scope"),
            "business_rules": requirement_lines("business_rules"),
            "assumptions": [],
            "permission_rules": requirement_lines("permission_rules"),
            "data_state_rules": requirement_lines("data_state_rules"),
            "interface_scope": requirement_lines("interface_scope"),
            "exception_rules": requirement_lines("exceptions"),
            "test_focus": requirement_lines("test_focus"),
            "risks": draft_contract.section_clean_lines(requirement_body, ("风险和处理方式", "风险处理")),
            # 正式包只接收 DRAFT 的结构化问题合同；Markdown 章节是展示内容，不能反向改变建档状态。
            "open_questions": list(
                dict.fromkeys(str(item).strip() for item in draft.get("questions", []) if str(item).strip())
            ),
            "source_refs": [draft_id],
            "source_draft_id": draft_id,
            "functional_requirements": requirement_points,
            "acceptance_criteria": acceptance_points,
            "test_cases": test_cases,
            "design": {
                "title": design_summary,
                "summary": technical_goal or design_summary,
                "technical_goal": technical_goal,
                "modules": design_lines("涉及模块"),
                "data_structures": design_lines("数据结构"),
                "interfaces": design_lines("接口设计"),
                "state_flow": design_lines("状态流"),
                "data_flow": design_lines("数据流"),
                "permissions_security": design_lines("权限和安全", "权限安全"),
                "error_handling": design_lines("错误处理"),
                "test_strategy": design_lines("测试策略"),
                "risks": design_lines("风险和处理方式", "风险处理"),
                "out_of_scope": design_lines("本轮不做", "不做范围"),
                "requirement_coverage": design_lines("对需求草稿的覆盖说明"),
            },
        }
    )


def formal_business_from_draft(draft: dict[str, Any]) -> dict[str, Any]:
    package = start_package_from_draft_fixture(draft)
    return {
        "slug": str(draft.get("draft_id") or "draft").lower(),
        "title": package["title"],
        "description": package["description"],
        "background": package["background"],
        "goal": package["goal"],
        "user_scenarios": package["user_scenarios"],
        "scope": package["scope"],
        "out_of_scope": package["out_of_scope"],
        "business_rules": package["business_rules"],
        "permission_rules": package["permission_rules"],
        "data_state_rules": package["data_state_rules"],
        "interface_scope": package["interface_scope"],
        "exception_rules": package["exception_rules"],
        "test_focus": package["test_focus"],
        "open_questions": package["open_questions"],
        "decisions": list(draft.get("decisions", [])),
        "functional_requirements": package["requirement_points"],
        "acceptance_criteria": package["acceptance_points"],
        "test_cases": package["test_cases"],
        "design": package["design"],
    }


def legacy_package() -> dict[str, Any]:
    """历史读取夹具直接标记为旧合同，不经过当前严格 start 入口。"""

    return {
        "formal_contract_version": "formal.v1",
        "title": "历史需求",
        "description": "历史包只用于读取兼容测试。",
        "requirement_points": [{"id": "FR-001", "description": "历史功能。"}],
        "acceptance_points": [{"id": "AC-001", "description": "历史验收。"}],
        "test_cases": [{"id": "TC-001", "description": "历史测试。"}],
        "design": {"summary": "历史技术摘要。"},
    }
