from __future__ import annotations

import argparse
from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any

from codex_sdlc.core import draft_contract, draft_lifecycle, draft_sections, fact_artifacts, fact_gate, fact_review_trust, fact_schema, start_contract
from codex_sdlc.core.draft_ownership import (
    pending_requirement_draft_capture_ids,
    unlinked_accepted_design_ids_for_start,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.markdown_contract import markdown_sections
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    build_design_title,
    build_requirement_title,
    derive_state,
    design_ids,
    generate_requirement_folder,
    next_number,
    refresh_materialized_state,
    requirement_ids,
)
from codex_sdlc.services.draft_service import DraftMutationService

def usable_test_commands(state: dict[str, object]) -> list[str]:
    commands = state.get("project", {}).get("test_commands", [])  # type: ignore[union-attr]
    return [command for command in commands if command]


def clean_text(value: object) -> str:
    return str(value or "").strip()


def clean_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    clean = clean_text(value)
    return [clean] if clean else []


def first_existing_value(data: dict[str, Any], keys: list[str]) -> object:
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def clean_string_list_field(data: dict[str, Any], keys: list[str], field_name: str, owner: str) -> list[str]:
    value = first_existing_value(data, keys)
    if value is None:
        return []
    if isinstance(value, str):
        clean = value.strip()
        return [clean] if clean else []
    if isinstance(value, list):
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SdlcError(f"{owner} 的{field_name}必须是字符串或字符串列表。", exit_code=1)
            clean = item.strip()
            if clean:
                cleaned.append(clean)
        return cleaned
    # 新增正式建档字段会被后续渲染直接使用，所以这里提前拦住对象、数字等模糊结构，避免状态里混进不可预测内容。
    raise SdlcError(f"{owner} 的{field_name}必须是字符串或字符串列表。", exit_code=1)


def clean_string_field(data: dict[str, Any], keys: list[str], field_name: str, owner: str, *, default: str = "") -> str:
    value = first_existing_value(data, keys)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    # 这些字段后续会原样进入正式 current JSON。对象或数字如果被 str() 悄悄转掉，质检时很难追出来源。
    raise SdlcError(f"{owner} 的{field_name}必须是字符串。", exit_code=1)


def load_start_package(package_file: str) -> dict[str, Any]:
    path = Path(package_file).expanduser()
    if not path.exists():
        raise SdlcError(f"正式建档 JSON 包不存在：{path}", exit_code=1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SdlcError(f"正式建档 JSON 包不是合法 JSON：第 {exc.lineno} 行 {exc.msg}", exit_code=1) from exc
    if not isinstance(data, dict):
        raise SdlcError("正式建档 JSON 包必须是对象。", exit_code=1)
    return data


def load_json_artifact(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise SdlcError(f"正式 JSON 缺少{label}：{path}", exit_code=1)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效的 UTF-8 JSON：{exc}", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}必须是 JSON 对象。", exit_code=1)
    return document


def load_verified_formal_bundle(raw_package: dict[str, Any], args: argparse.Namespace, paths) -> fact_gate.VerifiedFactBundle:
    if raw_package.get("formal_contract_version") != "formal.v3":
        raise SdlcError("新正式建档固定使用 formal.v3，并且必须提供模型事实包。formal.v2 只用于读取已有历史需求。", exit_code=1)
    manifest = raw_package.get("fact_bundle")
    if not isinstance(manifest, dict):
        raise SdlcError("正式 JSON 缺少 fact_bundle 清单，正式建档未执行。", exit_code=1)
    business_issues = fact_schema.formal_business_issues(raw_package)
    if business_issues:
        raise SdlcError("formal.v3 业务字段未通过检查：\n" + "\n".join(f"- {item}" for item in business_issues), exit_code=1)
    package_dir = Path(args.package_file).expanduser().resolve().parent
    file_args = {
        "index": (getattr(args, "source_index", ""), manifest.get("source_index_file"), "source index"),
        "requirement": (getattr(args, "requirement_facts", ""), manifest.get("requirement_facts_file"), "需求模型事实"),
        "design": (getattr(args, "design_facts", ""), manifest.get("design_facts_file"), "技术模型事实"),
        "review": (getattr(args, "model_review", ""), manifest.get("model_review_file"), "模型复核"),
    }
    loaded: dict[str, dict[str, Any]] = {}
    for key, (explicit, listed, label) in file_args.items():
        selected = clean_text(explicit) or clean_text(listed)
        if not selected:
            raise SdlcError(f"正式 JSON 缺少{label}文件声明，正式建档未执行。", exit_code=1)
        artifact_path = Path(selected).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = package_dir / artifact_path
        loaded[key] = load_json_artifact(artifact_path, label=label)
    business = {name: raw_package[name] for name in fact_schema.FORMAL_BUSINESS_FIELDS if name in raw_package}
    try:
        source = fact_artifacts.build_formal_source_projection(business)
    except ValueError as exc:
        raise SdlcError(f"formal.v3 业务内容未通过 source projection 检查：{exc}", exit_code=1) from exc

    source_draft_id = clean_text(raw_package.get("source_draft_id"))
    origin_index = None
    origin_semantic = None
    origin_req = None
    origin_design = None
    origin_source = None
    origin_review = None
    if source_draft_id:
        state = derive_state(paths)
        draft = state.get("drafts", {}).get(source_draft_id)
        if not isinstance(draft, dict):
            raise SdlcError(f"正式建档包引用了不存在的 DRAFT：{source_draft_id}", exit_code=1)
        origin_index = draft.get("fact_source_index") if isinstance(draft.get("fact_source_index"), dict) else None
        origin_req = draft.get("requirement_facts") if isinstance(draft.get("requirement_facts"), dict) else None
        origin_design = draft.get("design_facts") if isinstance(draft.get("design_facts"), dict) else None
        origin_source = draft.get("fact_source_projection") if isinstance(draft.get("fact_source_projection"), dict) else None
        origin_review = draft.get("model_review") if isinstance(draft.get("model_review"), dict) else None
        if origin_index is None or origin_req is None or origin_design is None:
            raise SdlcError(f"{source_draft_id} 缺少已经通过的 DRAFT 模型事实产物。", exit_code=1)
        origin_semantic = {"requirement": str(origin_req.get("semantic_sha256") or ""), "design": str(origin_design.get("semantic_sha256") or "")}

    verified = fact_gate.VerifiedFactBundle(
        source=source,
        index=loaded["index"],
        requirement=loaded["requirement"],
        design=loaded["design"],
        review=loaded["review"],
        manifest=manifest,
        origin_index=origin_index,
        origin_semantic_sha256=origin_semantic,
        origin_requirement=origin_req,
        origin_design=origin_design,
        origin_source=origin_source,
        origin_review=origin_review,
        review_receipt=fact_review_trust.find_trusted_receipt(
            paths,
            target_sha256=fact_review_trust.review_target_sha256(loaded["requirement"], loaded["design"], loaded["review"]["targets"]),
            review_sha256=fact_artifacts.artifact_sha256(loaded["review"]),
            draft_id=source_draft_id or "FORMAL",
            entry_scope="formal",
        ),
    )
    result = fact_gate.FactGate.verify(verified.as_dict(), entry_kind="formal")
    if not result.passed:
        raise SdlcError(f"正式模型事实包未通过检查：{result.message}\n下一步：回到 $sdlc-start 重新生成、物化并复核事实。", exit_code=1)
    return verified


def normalize_existing_public_id(prefix: str, value: object, owner: str) -> str:
    clean = clean_text(value).upper()
    if re.fullmatch(rf"{prefix}-\d{{3}}", clean):
        return clean
    raise SdlcError(f"{owner} 编号不合法，必须显式提供 {prefix}-xxx。", exit_code=1)


def normalize_requirement_points(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = data.get("functional_requirements") or data.get("requirements") or data.get("requirement_points") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise SdlcError("正式建档包至少需要 1 条功能需求。", exit_code=1)

    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = raw_item if isinstance(raw_item, dict) else {"description": clean_text(raw_item)}
        point_id = normalize_existing_public_id("FR", item.get("id"), f"功能需求第 {index} 条")
        if point_id in seen:
            raise SdlcError(f"功能需求编号重复：{point_id}", exit_code=1)
        title = clean_text(item.get("title") or item.get("summary") or item.get("description"))
        description = clean_text(item.get("description") or title)
        if not title or not description:
            raise SdlcError(f"{point_id} 缺少标题或说明。", exit_code=1)
        points.append(
            {
                "id": point_id,
                "title": title,
                "summary": clean_text(item.get("summary") or title),
                "description": description,
                "status": clean_text(item.get("status") or "active"),
                "inputs": clean_string_list_field(item, ["inputs", "input"], "输入", point_id),
                "outputs": clean_string_list_field(item, ["outputs", "output"], "输出", point_id),
                "triggers": clean_string_list_field(item, ["triggers", "trigger", "trigger_conditions"], "触发条件", point_id),
                "data_changes": clean_string_list_field(item, ["data_changes", "saved_data", "changed_data"], "保存或改变的数据", point_id),
                "permissions": clean_string_list_field(item, ["permissions", "permission", "permission_rules"], "权限", point_id),
                "exceptions": clean_string_list_field(item, ["exceptions", "exception", "error_handling"], "异常", point_id),
                "boundaries": clean_string_list_field(item, ["boundaries", "boundary", "limits"], "边界", point_id),
                "rules": clean_string_list_field(item, ["rules"], "规则", point_id),
                "acceptance_ids": clean_string_list_field(
                    item,
                    ["acceptance_ids", "ac_ids", "acceptance_refs", "coverage_acceptance"],
                    "验收关联",
                    point_id,
                ),
                "material_refs": clean_list(item.get("material_refs") or item.get("materials")),
            }
        )
        seen.add(point_id)
    return points


def normalize_acceptance_points(data: dict[str, Any], requirement_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items = data.get("acceptance_criteria") or data.get("acceptance_points") or data.get("acceptance") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise SdlcError("正式建档包至少需要 1 条验收标准。", exit_code=1)

    requirement_ids = {str(point["id"]) for point in requirement_points}
    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = raw_item if isinstance(raw_item, dict) else {"description": clean_text(raw_item)}
        point_id = normalize_existing_public_id("AC", item.get("id"), f"验收标准第 {index} 条")
        if point_id in seen:
            raise SdlcError(f"验收标准编号重复：{point_id}", exit_code=1)
        refs = clean_list(item.get("requirement_ids") or item.get("fr_ids") or item.get("fr_refs"))
        unknown_refs = [ref for ref in refs if ref not in requirement_ids]
        if unknown_refs:
            raise SdlcError(f"{point_id} 需要关联有效的功能需求编号。", exit_code=1)
        title = clean_string_field(item, ["title", "summary"], "标题", point_id)
        operation = clean_string_field(item, ["operation"], "操作", point_id)
        expected = clean_string_field(item, ["expected"], "预期", point_id)
        pass_standard = clean_string_field(item, ["pass_standard"], "通过标准", point_id)
        description = clean_text(
            item.get("description")
            or expected
            or pass_standard
            or title
        )
        if not description:
            raise SdlcError(f"{point_id} 缺少验收说明。", exit_code=1)
        points.append(
            {
                "id": point_id,
                "title": title or description,
                "requirement_ids": refs,
                "description": description,
                "operation": operation,
                "expected": expected,
                "pass_standard": pass_standard,
                "status": clean_text(item.get("status") or "active"),
            }
        )
        seen.add(point_id)
    return points


def normalize_test_cases(data: dict[str, Any], acceptance_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items = data.get("test_cases") or data.get("tests") or data.get("test_matrix") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise SdlcError("正式建档包至少需要 1 条测试用例。", exit_code=1)

    acceptance_ids = {str(point["id"]) for point in acceptance_points}
    acceptance_requirement_ids = {
        str(point["id"]): [str(ref) for ref in point.get("requirement_ids", [])]
        for point in acceptance_points
    }
    known_requirement_ids = {
        requirement_id
        for refs in acceptance_requirement_ids.values()
        for requirement_id in refs
    }
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(raw_items, start=1):
        item = raw_item if isinstance(raw_item, dict) else {"description": clean_text(raw_item)}
        case_id = normalize_existing_public_id("TC", item.get("id"), f"测试用例第 {index} 条")
        if case_id in seen:
            raise SdlcError(f"测试用例编号重复：{case_id}", exit_code=1)
        refs = clean_list(item.get("acceptance_ids") or item.get("ac_ids") or item.get("ac_refs"))
        unknown_refs = [ref for ref in refs if ref not in acceptance_ids]
        if unknown_refs:
            raise SdlcError(f"{case_id} 需要关联有效的验收标准编号。", exit_code=1)
        requirement_refs = clean_string_list_field(
            item,
            ["requirement_ids", "fr_ids", "fr_refs", "coverage_requirements"],
            "覆盖需求",
            case_id,
        )
        unknown_requirement_refs = [ref for ref in requirement_refs if ref not in known_requirement_ids]
        if unknown_requirement_refs:
            raise SdlcError(f"{case_id} 需要关联有效的功能需求编号。", exit_code=1)
        description = clean_text(item.get("description") or item.get("method") or item.get("pass_standard"))
        if not description:
            raise SdlcError(f"{case_id} 缺少测试说明。", exit_code=1)
        case_type = clean_string_field(item, ["type"], "类型", case_id)
        operation = clean_string_field(item, ["operation", "method"], "操作", case_id)
        expected = clean_string_field(item, ["expected"], "预期", case_id)
        pass_standard = clean_string_field(item, ["pass_standard"], "通过标准", case_id)
        cases.append(
            {
                "id": case_id,
                "acceptance_ids": refs,
                "requirement_ids": requirement_refs,
                "description": description,
                "type": case_type,
                "operation": operation,
                "method": clean_string_field(item, ["method"], "操作", case_id, default=operation),
                "expected": expected,
                "pass_standard": pass_standard,
                "status": clean_text(item.get("status") or "active"),
                "task_id": clean_text(item.get("task_id")),
            }
        )
        seen.add(case_id)
    return cases


def normalize_start_package(data: dict[str, Any]) -> dict[str, Any]:
    requirement_points = normalize_requirement_points(data)
    acceptance_points = normalize_acceptance_points(data, requirement_points)
    derived_acceptance_ids: dict[str, list[str]] = {str(point["id"]): [] for point in requirement_points}
    for acceptance in acceptance_points:
        for requirement_id in acceptance.get("requirement_ids", []):
            derived_acceptance_ids.setdefault(str(requirement_id), []).append(str(acceptance["id"]))
    for point in requirement_points:
        provided = [str(item) for item in point.get("acceptance_ids", [])]
        derived = derived_acceptance_ids.get(str(point["id"]), [])
        if provided and set(provided) != set(derived):
            raise SdlcError(f"{point['id']} 的验收关联与 AC 覆盖需求不一致。", exit_code=1)
        # AC.requirement_ids 是唯一可编辑来源，验证后才反向生成只读关联。
        point["acceptance_ids"] = derived
    test_cases = normalize_test_cases(data, acceptance_points)
    title = clean_text(data.get("title") or data.get("name") or requirement_points[0]["title"])
    description = clean_text(data.get("description") or "\n".join(point["description"] for point in requirement_points))
    if not title or not description:
        raise SdlcError("正式建档包缺少需求标题或需求说明。", exit_code=1)
    design = data.get("design") if isinstance(data.get("design"), dict) else {}
    design_summary = clean_text(
        design.get("summary")
        or design.get("description")
        or data.get("design_summary")
        or data.get("technical_design")
    )
    if not design_summary:
        raise SdlcError("正式建档包缺少已确认技术方案。", exit_code=1)
    design_title = clean_text(design.get("title") or "正式技术方案")
    return {
        "title": title,
        "slug": clean_text(data.get("slug") or data.get("key")),
        "description": description,
        "summary": clean_text(data.get("summary") or title),
        "background": clean_text(data.get("background")),
        "goal": clean_text(data.get("goal") or data.get("target")),
        "scope": clean_list(data.get("scope")),
        "out_of_scope": clean_list(data.get("out_of_scope") or data.get("non_goals")),
        "user_scenarios": clean_list(data.get("user_scenarios") or data.get("users") or data.get("scenarios")),
        "business_rules": clean_list(data.get("business_rules")),
        "risks": clean_list(data.get("risks")),
        "assumptions": clean_list(data.get("assumptions")),
        "permission_rules": clean_list(data.get("permission_rules")),
        "data_state_rules": clean_list(data.get("data_state_rules")),
        "interface_scope": clean_list(data.get("interface_scope")),
        "exception_rules": clean_list(data.get("exception_rules")),
        "test_focus": clean_list(data.get("test_focus")),
        "open_questions": clean_list(data.get("open_questions")),
        "decisions": clean_list(data.get("decisions")),
        "source_refs": clean_list(data.get("source_refs")),
        "source_draft_id": clean_text(data.get("source_draft_id") or data.get("draft_id")),
        "formal_contract_version": clean_text(data.get("formal_contract_version")),
        "fact_bundle": deepcopy(data.get("fact_bundle")) if isinstance(data.get("fact_bundle"), dict) else {},
        "requirement_points": requirement_points,
        "acceptance_points": acceptance_points,
        "test_cases": test_cases,
        "design": {
            "title": design_title,
            "summary": design_summary,
            "technical_goal": clean_string_field(design, ["technical_goal", "goal"], "技术目标", "技术方案"),
            "modules": clean_string_list_field(design, ["modules", "involved_modules"], "涉及模块", "技术方案"),
            "data_structures": clean_string_list_field(design, ["data_structures", "data_structure"], "数据结构", "技术方案"),
            "interfaces": clean_string_list_field(design, ["interfaces", "interface_design"], "接口设计", "技术方案"),
            "state_flow": clean_string_list_field(design, ["state_flow"], "状态流", "技术方案"),
            "data_flow": clean_string_list_field(design, ["data_flow"], "数据流", "技术方案"),
            "permissions_security": clean_string_list_field(
                design,
                ["permissions_security", "permissions_and_security", "security"],
                "权限和安全",
                "技术方案",
            ),
            "error_handling": clean_string_list_field(design, ["error_handling", "errors"], "错误处理", "技术方案"),
            "test_strategy": clean_string_list_field(design, ["test_strategy"], "测试策略", "技术方案"),
            "risks": clean_string_list_field(design, ["risks"], "风险和处理方式", "技术方案"),
            "out_of_scope": clean_string_list_field(design, ["out_of_scope", "not_in_scope"], "本轮不做", "技术方案"),
            "requirement_coverage": clean_string_list_field(
                design,
                ["requirement_coverage", "fr_coverage", "coverage"],
                "对需求草稿的覆盖说明",
                "技术方案",
            ),
        },
    }


def ensure_start_package_can_create_requirement(
    package: dict[str, Any],
    *,
    raw_package: dict[str, Any] | None = None,
    source_draft: dict[str, Any] | None = None,
) -> None:
    # 正式包能不能建档只问 StartContract，服务层不再自己追加 AC/TC、占位或保真规则。
    issues: list[str] = []
    if raw_package is not None:
        issues.extend(start_contract.raw_start_package_contract_issues(raw_package))
    issues.extend(start_contract.start_package_contract_issues(package, source_draft=source_draft))
    issues = list(dict.fromkeys(issues))
    if not issues:
        return
    raise SdlcError(
        "正式建档包未通过 start 前审查，不能直接创建正式需求：\n"
        + "\n".join(f"- {item}" for item in issues),
        exit_code=1,
    )


def draft_display_label(draft: dict[str, Any]) -> str:
    draft_id = clean_text(draft.get("draft_id") or "DRAFT")
    status = clean_text(draft.get("status") or "unknown")
    title = clean_text(draft.get("title") or "")
    return f"{draft_id} [{status}]" + (f" {title}" if title else "")


def unfinished_drafts(state: dict[str, Any]) -> list[dict[str, Any]]:
    drafts = state.get("drafts", {})
    if not isinstance(drafts, dict):
        return []
    return [
        draft
        for draft in drafts.values()
        if isinstance(draft, dict) and draft_lifecycle.is_unfinished_draft(draft)
    ]


def ensure_source_draft_can_start(paths, package: dict[str, Any]) -> None:
    source_draft_id = clean_text(package.get("source_draft_id"))
    if not source_draft_id:
        return
    state = derive_state(paths)
    drafts = state.get("drafts", {})
    draft = drafts.get(source_draft_id) if isinstance(drafts, dict) else None
    if not isinstance(draft, dict):
        raise SdlcError(f"正式建档包引用了不存在的 DRAFT：{source_draft_id}", exit_code=1)
    assessment = draft_lifecycle.assess_draft(draft)
    if not assessment.can_start:
        raise SdlcError(
            f"{source_draft_id} 未通过 DRAFT 内容评估，不能通过 start --file 绕过 DRAFT 门禁：{assessment.reason}",
            exit_code=1,
        )

    requirement_body = clean_text(draft.get("requirement_body"))
    if requirement_body:
        draft_fr_count = len(draft_contract.parse_requirement_draft(requirement_body).fr_blocks)
        draft_ac_count = len(draft_contract.parse_requirement_draft(requirement_body).ac_blocks)
        draft_tc_count = len(draft_contract.parse_requirement_draft(requirement_body).tc_blocks)
        if draft_fr_count and len(package.get("requirement_points", [])) < draft_fr_count:
            raise SdlcError(f"{source_draft_id} 有 {draft_fr_count} 条 FR，但正式建档包只有 {len(package.get('requirement_points', []))} 条。", exit_code=1)
        if draft_ac_count and len(package.get("acceptance_points", [])) < draft_ac_count:
            raise SdlcError(f"{source_draft_id} 有 {draft_ac_count} 条 AC，但正式建档包只有 {len(package.get('acceptance_points', []))} 条。", exit_code=1)
        if draft_tc_count and len(package.get("test_cases", [])) < draft_tc_count:
            raise SdlcError(f"{source_draft_id} 有 {draft_tc_count} 条 TC，但正式建档包只有 {len(package.get('test_cases', []))} 条。", exit_code=1)


def select_start_ready_draft(state: dict[str, Any], draft_id: str = "") -> dict[str, Any]:
    drafts = state.get("drafts", {})
    if not isinstance(drafts, dict):
        raise SdlcError("当前 DRAFT 状态读取失败。", exit_code=1)
    if clean_text(draft_id):
        clean_id = clean_text(draft_id).upper()
        draft = drafts.get(clean_id)
        if not isinstance(draft, dict):
            raise SdlcError(f"没有找到 DRAFT `{clean_id}`。", exit_code=1)
        if draft_lifecycle.is_started_draft(draft):
            raise SdlcError(f"{clean_id} 已经正式建档，不能重复执行 start。", exit_code=1)
        return draft

    candidates = [
        draft
        for draft in drafts.values()
        if isinstance(draft, dict) and not draft_lifecycle.is_started_draft(draft)
    ]
    if not candidates:
        raise SdlcError("当前没有可审查的 DRAFT，请先通过 discuss 创建需求草稿。", exit_code=1)
    return sorted(candidates, key=lambda item: int(item.get("_updated_seq", 0)), reverse=True)[0]


def ensure_file_start_respects_active_draft(state: dict[str, Any], package: dict[str, Any]) -> None:
    active_drafts = unfinished_drafts(state)
    if not active_drafts:
        return
    source_draft_id = clean_text(package.get("source_draft_id"))
    if not source_draft_id:
        draft_list = "、".join(draft_display_label(draft) for draft in active_drafts)
        raise SdlcError(
            "当前存在未完成的 DRAFT，不能用无来源的 start --file 直接建档。\n"
            f"未完成 DRAFT：{draft_list}\n"
            "请改用 `codex-sdlc start` 消费当前 DRAFT，或在正式建档 JSON 里补 `source_draft_id`。",
            exit_code=1,
        )
    active_ids = {clean_text(draft.get("draft_id")) for draft in active_drafts}
    if source_draft_id not in active_ids:
        raise SdlcError(
            f"正式建档 JSON 指向 `{source_draft_id}`，但当前未完成 DRAFT 不是它。"
            "请确认来源 DRAFT，避免把错误确认稿建成正式需求。",
            exit_code=1,
        )


def create_native_requirement_from_package(paths, package: dict[str, Any]) -> dict[str, Any]:
    state = derive_state(paths)
    source_draft_id = clean_text(package.get("source_draft_id"))
    draft_capture_ids = pending_requirement_draft_capture_ids(state, source_draft_id)
    unlinked_accepted_design_ids = unlinked_accepted_design_ids_for_start(state, source_draft_id)
    requirement_id = next_number(requirement_ids(state), "REQ")
    should_create_design_event = package.get("create_design_event", True) is not False
    created_design_id = "" if unlinked_accepted_design_ids or not should_create_design_event else next_number(design_ids(state), "DES")
    folder_name = generate_requirement_folder(requirement_id, package["slug"] or package["title"])
    source_refs = list(package["source_refs"])
    if source_draft_id and source_draft_id not in source_refs:
        source_refs.append(source_draft_id)
    drafts_to_mark_started: list[str] = []
    if source_draft_id:
        drafts_to_mark_started.append(source_draft_id)
    elif draft_capture_ids:
        active_draft_ids = [
            str(draft_id)
            for draft_id, draft in state.get("drafts", {}).items()
            if isinstance(draft, dict) and clean_text(draft.get("status")) != "started"
        ]
        if len(active_draft_ids) == 1:
            drafts_to_mark_started.append(active_draft_ids[0])
        elif len(active_draft_ids) > 1:
            raise SdlcError(
                "start --file 已链接需求讨论草案，但当前存在多个活跃 DRAFT，不能自动判断要收口哪一个。"
                "请在正式建档 JSON 里补 source_draft_id，或使用默认 codex-sdlc start 消费 start_ready DRAFT。",
                exit_code=1,
            )
    native_start = {
        "migration_status": "native",
        "formal_contract_version": package.get("formal_contract_version", "formal.v2"),
        "background": package["background"],
        "goal": package["goal"],
        "scope": package["scope"],
        "out_of_scope": package["out_of_scope"],
        "user_scenarios": package["user_scenarios"],
        "business_rules": package["business_rules"],
        "risks": package["risks"],
        "assumptions": package["assumptions"],
        "permission_rules": package["permission_rules"],
        "data_state_rules": package["data_state_rules"],
        "interface_scope": package["interface_scope"],
        "exception_rules": package["exception_rules"],
        "test_focus": package["test_focus"],
        "open_questions": package["open_questions"],
        "decisions": package.get("decisions", []),
        "source_refs": source_refs,
        "requirement_points": package["requirement_points"],
        "acceptance_points": package["acceptance_points"],
        "test_cases": package["test_cases"],
        "design": package["design"],
        "fact_bundle": package.get("fact_bundle", {}),
    }
    if source_draft_id:
        native_start["source_draft_id"] = source_draft_id
    append_event(
        paths,
        event_type="requirement_created",
        source="sdlc-start",
        summary=f"创建正式需求 {requirement_id}",
        requirement_id=requirement_id,
        payload={
            "title": package["title"],
            "description": package["description"],
            "summary": package["summary"],
            "folder_name": folder_name,
            "flow_type": clean_text(package.get("flow_type") or "SDLC 原生正式流程"),
            "native_start": native_start,
        },
    )
    if created_design_id:
        append_event(
            paths,
            event_type="design_recorded",
            source="sdlc-start",
            summary=f"记录正式技术方案 {created_design_id}",
            requirement_id=requirement_id,
            payload={
                "design_id": created_design_id,
                "title": build_design_title(package["design"]["title"]),
                "summary": package["design"]["summary"],
                "details": package["design"],
                "status": "draft",
                "file_path": f".codex-sdlc/requirements/{folder_name}/design.md",
            },
        )
        append_event(
            paths,
            event_type="design_accepted",
            source="sdlc-start",
            summary=f"确认正式技术方案 {created_design_id}",
            requirement_id=requirement_id,
            payload={"design_ids": [created_design_id]},
        )
    if draft_capture_ids:
        append_event(
            paths,
            event_type="capture_linked",
            source="sdlc-discuss",
            summary=f"纳入需求讨论草案到 {requirement_id}",
            requirement_id=requirement_id,
            payload={"capture_ids": draft_capture_ids, "target_type": "decision"},
        )
    if unlinked_accepted_design_ids:
        append_event(
            paths,
            event_type="design_linked",
            source="sdlc-design",
            summary=f"纳入已确认技术方案到 {requirement_id}",
            requirement_id=requirement_id,
            payload={"design_ids": unlinked_accepted_design_ids},
        )
    for draft_id_to_start in drafts_to_mark_started:
        append_event(
            paths,
            event_type="draft_started",
            source="sdlc-start",
            summary=f"{draft_id_to_start} 已生成正式需求 {requirement_id}",
            requirement_id=requirement_id,
            payload={"draft_id": draft_id_to_start, "started_requirement_id": requirement_id},
        )
    refresh_materialized_state(paths)
    return {
        "requirement_id": requirement_id,
        "folder_name": folder_name,
        "draft_capture_ids": draft_capture_ids,
        "unlinked_accepted_design_ids": unlinked_accepted_design_ids,
    }


def print_native_start_result(package: dict[str, Any], result: dict[str, Any]) -> None:
    requirement_id = result["requirement_id"]
    requirement_dir = f".codex-sdlc/requirements/{result['folder_name']}"
    print(f"已创建正式需求：{requirement_id}")
    print(f"需求标题：{package['title']}")
    if package.get("source_draft_id"):
        print(f"来源 DRAFT：{package['source_draft_id']}")
    print(f"需求包：{requirement_dir}")
    print("已生成当前生效需求、技术方案、测试矩阵和追溯矩阵。")
    print(f"- 功能需求：{len(package['requirement_points'])} 条")
    print(f"- 验收标准：{len(package['acceptance_points'])} 条")
    print(f"- 测试用例：{len(package['test_cases'])} 条")
    if result["draft_capture_ids"]:
        print("已纳入需求讨论草案：")
        for capture_id in result["draft_capture_ids"]:
            print(f"- {capture_id}")
    if result["unlinked_accepted_design_ids"]:
        print("已纳入已确认技术方案：")
        for linked_design_id in result["unlinked_accepted_design_ids"]:
            print(f"- {linked_design_id}")
    print(f"下一步建议：$sdlc-tasks {requirement_id}")


def run_native_start(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    raw_package = load_start_package(args.package_file)
    if (
        raw_package.get("formal_contract_version") != "formal.v3"
        or raw_package.get("workflow_profile") != "document-first.v1"
    ):
        raise SdlcError(
            "start --file 只接受 document-first.v1 正式包；facts profile 只保留历史档案读取和体检。"
            "下一步：从当前 start_ready DRAFT 重新生成文档优先正式包。",
            exit_code=1,
        )
    legacy_arguments = [
        name
        for name in (
            "description",
            "draft_id",
            "source_index",
            "requirement_facts",
            "design_facts",
            "model_review",
        )
        if clean_text(getattr(args, name, ""))
    ]
    if legacy_arguments:
        # 旧位置说明和旧拼装参数必须在任何 DRAFT、状态或哈希校验前拒绝；
        # 静默忽略会让调用方误以为这些输入参与了正式来源选择。
        raise SdlcError(
            "document-first 正式建档不能同时使用旧位置说明、--draft、--source-index、"
            "--requirement-facts、--design-facts 或 --model-review。"
            "下一步：只保留 start --file，并在正式包中写明 source_draft_id。",
            exit_code=1,
        )

    from codex_sdlc.core.start_transaction import (
        commit_prepared_start,
        find_completed_start,
    )

    # 相同 formal.v3 在第一次提交后，来源 DRAFT 已经变为 started，不能再
    # 重跑前置编号分配。先按完整正式包哈希读取完成回执，才能稳定返回同一结果。
    completed = find_completed_start(paths, raw_package)
    if completed is None:
        prepared = prepare_document_first_start(paths, raw_package)
        result = commit_prepared_start(paths, prepared)
    else:
        result = completed
    print(f"已创建正式需求：{result['requirement_id']}")
    print(f"来源 DRAFT：{raw_package['source_draft_id']}")
    print(f"需求包：.codex-sdlc/requirements/{result['target_directory']}")
    if result.get("idempotent"):
        print("相同正式建档事务已经完成，已返回原结果。")
    else:
        print("正式事件、需求目录和状态投影已经完整提交。")
    return 0


def prepare_document_first_start(paths, raw_package: dict[str, Any], *, fault_injector=None) -> dict[str, object]:
    """消费 T-017 只读门禁结果并构建独立候选，不提前执行 T-019 的提交动作。"""

    from codex_sdlc.core.start_staging import build_prepared_start_staging

    # 编号和正式目标在预写期间也可能被别的命令改变，因此门禁与完整构建必须
    # 共用项目锁；锁内仍只写独立 staging，不追加事件或占用正式目录。
    with project_lock(paths):
        preflight = start_contract.preflight_document_first_start(paths, raw_package)
        return build_prepared_start_staging(
            paths,
            preflight,
            fault_injector=fault_injector,
        )


def run_legacy_fact_start(args: argparse.Namespace) -> int:
    raise SdlcError(
        "facts profile 只用于读取和体检已有正式档案，不能新建 document-first 正式需求。"
        "下一步：使用 start --file 提交带显式 source_draft_id 的 document-first.v1 正式包。",
        exit_code=1,
    )


def run_draft_start(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        draft = select_start_ready_draft(state, args.draft_id)
        assessment = draft_lifecycle.assess_draft(draft)
        if assessment.facts_status != "facts_passed":
            raise SdlcError(
                f"{draft.get('draft_id')} 尚未完成模型事实提取和独立复核：{assessment.reason}\n"
                f"下一步：{assessment.next_action}\n正式建档未执行，DRAFT 状态未改变。",
                exit_code=1,
            )
        raise SdlcError(
            f"{draft.get('draft_id')} 的 DRAFT 事实已经通过，但还没有正式 JSON Pointer 来源映射。\n"
            "下一步：执行 $sdlc-start，让模型生成 formal.v3、正式 source index、materialized facts 和独立复核文件，再调用 start --file。\n"
            "正式建档未执行，DRAFT 状态未改变。",
            exit_code=1,
        )



def start(args: argparse.Namespace) -> int:
    """统一建档服务入口，命令层只负责把 argparse 结果交进来。"""

    mode = clean_text(getattr(args, "_sdlc_start_mode", "formal"))
    if mode == "light":
        return run_light_start(args)
    return run_formal_start(args)


def run_formal_start(args: argparse.Namespace) -> int:
    if args.package_file:
        return run_native_start(args)

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    raise SdlcError(
        "正式建档必须使用 start --file <formal.v3.json>，并在包内显式提供 source_draft_id。"
        "下一步：从当前 start_ready DRAFT 生成 document-first.v1 正式包后重试。",
        exit_code=1,
    )


def print_light_start_offline_notice() -> None:
    print("light-start 已下线，不再支持一句话直接生成正式需求。")
    print("正式需求请走 DRAFT 主流程：discuss -> design -> start。")
    print("如果已经有结构化正式建档包，请使用 start --file。")


def run_light_start(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    description = args.description.strip()
    if not description:
        raise SdlcError("需求内容不能为空。")

    print_light_start_offline_notice()
    return 0
