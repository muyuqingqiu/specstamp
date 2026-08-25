from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from codex_sdlc.core import draft_contract
from codex_sdlc.core.errors import SdlcError

DOCUMENT_FIRST_FORMAL_SCHEMA = "formal-document-first.v3"
DOCUMENT_FIRST_PROFILE = "document-first.v1"
FORMAL_CONTRACT_VERSION = "formal.v3"

REQUIRED_FORMAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("user_scenarios", "用户和使用场景"),
    ("scope", "本轮范围"),
    ("out_of_scope", "不做范围"),
    ("business_rules", "业务规则"),
    ("permission_rules", "权限规则"),
    ("data_state_rules", "数据和状态规则"),
    ("interface_scope", "接口或页面范围"),
    ("exception_rules", "异常和边界"),
    ("test_focus", "测试关注点"),
)

RAW_FORMAL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "user_scenarios": ("user_scenarios", "users", "scenarios"),
    "scope": ("scope",),
    "out_of_scope": ("out_of_scope", "non_goals"),
    "business_rules": ("business_rules",),
    "permission_rules": ("permission_rules",),
    "data_state_rules": ("data_state_rules",),
    "interface_scope": ("interface_scope",),
    "exception_rules": ("exception_rules",),
    "test_focus": ("test_focus",),
}

DESIGN_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("technical_goal", "技术目标"),
    ("modules", "涉及模块"),
    ("data_structures", "数据结构"),
    ("interfaces", "接口设计"),
    ("state_flow", "状态流"),
    ("data_flow", "数据流"),
    ("permissions_security", "权限和安全"),
    ("error_handling", "错误处理"),
    ("test_strategy", "测试策略"),
    ("risks", "风险和处理方式"),
    ("out_of_scope", "本轮不做"),
    ("requirement_coverage", "对需求草稿的覆盖说明"),
)


def _current_state(paths, supplied: Mapping[str, object] | None) -> Mapping[str, object]:
    """生产入口始终重读事件状态，避免调用方用旧缓存掩盖审核或哈希漂移。"""

    if paths.events_file.is_file():
        from codex_sdlc.core.state import derive_state

        return derive_state(paths)
    if isinstance(supplied, Mapping):
        return supplied
    from codex_sdlc.core.state import derive_state

    return derive_state(paths)


def _document_first_schema(package: Mapping[str, object]) -> dict[str, object]:
    from codex_sdlc.core.structured_contract import validate_schema_document

    candidate = deepcopy(dict(package))
    if (
        candidate.get("formal_contract_version") != FORMAL_CONTRACT_VERSION
        or candidate.get("workflow_profile") != DOCUMENT_FIRST_PROFILE
    ):
        raise SdlcError(
            "start --file 只接受 workflow_profile 为 document-first.v1 的 formal.v3 正式包。"
            "下一步：从当前 start_ready DRAFT 重新生成文档优先正式包。",
            exit_code=1,
        )
    if not str(candidate.get("source_draft_id") or "").strip():
        raise SdlcError(
            "document-first.v1 正式包缺少显式 source_draft_id。"
            "下一步：选择当前项目中唯一明确的 start_ready DRAFT 并重新生成正式包。",
            exit_code=1,
        )
    try:
        validate_schema_document(candidate, schema_name=DOCUMENT_FIRST_FORMAL_SCHEMA)
    except SdlcError as exc:
        raise SdlcError(
            f"{exc}下一步：按 formal-document-first.v3 Schema 修正正式包后重试。",
            exit_code=1,
        ) from exc
    return candidate


def _explicit_draft(
    state: Mapping[str, object],
    package: Mapping[str, object],
) -> Mapping[str, object]:
    draft_id = str(package.get("source_draft_id") or "")
    drafts = state.get("drafts")
    draft = drafts.get(draft_id) if isinstance(drafts, Mapping) else None
    if not isinstance(draft, Mapping):
        raise SdlcError(
            f"正式包指定的来源 DRAFT 不存在：{draft_id}。"
            "下一步：重新读取当前项目的 DRAFT 编号并生成正式包。",
            exit_code=1,
        )
    return draft


def _ready_status(draft: Mapping[str, object]) -> None:
    draft_id = str(draft.get("draft_id") or "")
    if draft.get("status") == "started":
        raise SdlcError(
            f"{draft_id} 已经完成过正式建档，不能重复作为新建档来源。"
            "下一步：读取已经建立的正式需求，或选择另一个明确的 start_ready DRAFT。",
            exit_code=1,
        )
    assessment = draft.get("assessment")
    if (
        draft.get("status") != "start_ready"
        or not isinstance(assessment, Mapping)
        or assessment.get("can_start") is not True
    ):
        raise SdlcError(
            f"{draft_id} 当前不是 start_ready。"
            "下一步：完成需求审核、用户确认和整体设计审核后重新生成正式包。",
            exit_code=1,
        )


def _revision_gate_view(
    state: Mapping[str, object],
    draft: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    """只把已通过审核的文件漂移延后到 revision 之后复核。"""

    if draft.get("status") != "design_reviewing":
        return draft, state
    assessment = draft.get("assessment")
    blockers = assessment.get("blockers") if isinstance(assessment, Mapping) else None
    if (
        not isinstance(blockers, list)
        or len(blockers) != 1
        or not isinstance(blockers[0], Mapping)
        or blockers[0].get("code") != "integrated_design_review_stale"
    ):
        return draft, state
    review_state = draft.get("_integrated_design_review_state")
    reviews = review_state.get("reviews") if isinstance(review_state, Mapping) else None
    current = [
        item
        for item in reviews
        if isinstance(item, Mapping)
        and item.get("stage") == "integrated_design"
        and item.get("owner_id") == draft.get("draft_id")
        and item.get("is_current") is True
    ] if isinstance(reviews, list) else []
    if (
        len(current) != 1
        or current[0].get("request_status") != "completed"
        or current[0].get("recorded_status") != "passed"
        or current[0].get("effective_status") != "stale"
        or not current[0].get("changed_files")
    ):
        return draft, state

    # 文件漂移已经明确把通过记录标成 stale，但它属于 revision 之后的审核输入一致性。
    # 这里只恢复进入 revision 比较所需的结构化事实，不能让 needs_fix、未确认或 started
    # 等独立门禁延后；完整索引和真实文件校验随后仍会拒绝这份失效审核。
    staged_draft = deepcopy(dict(draft))
    staged_draft["status"] = "start_ready"
    staged_assessment = deepcopy(dict(assessment))
    staged_assessment["can_start"] = True
    staged_draft["assessment"] = staged_assessment
    staged_review_state = deepcopy(dict(review_state))
    staged_review_state["can_advance"] = True
    staged_reviews = staged_review_state.get("reviews")
    for item in staged_reviews if isinstance(staged_reviews, list) else []:
        if (
            isinstance(item, dict)
            and item.get("review_id") == current[0].get("review_id")
        ):
            item["effective_status"] = "passed"
            item["can_advance"] = True
    staged_draft["_integrated_design_review_state"] = staged_review_state

    staged_state = dict(state)
    drafts = state.get("drafts")
    staged_drafts = dict(drafts) if isinstance(drafts, Mapping) else {}
    staged_drafts[str(draft.get("draft_id") or "")] = staged_draft
    staged_state["drafts"] = staged_drafts
    return staged_draft, staged_state


def _current_review(
    draft: Mapping[str, object],
    *,
    state_field: str,
    stage: str,
    label: str,
) -> Mapping[str, object]:
    review_state = draft.get(state_field)
    reviews = review_state.get("reviews") if isinstance(review_state, Mapping) else None
    current = [
        item
        for item in reviews
        if isinstance(item, Mapping)
        and item.get("stage") == stage
        and item.get("owner_id") == draft.get("draft_id")
        and item.get("is_current") is True
        and item.get("request_status") == "completed"
        and item.get("effective_status") == "passed"
        and item.get("can_advance") is True
    ] if isinstance(reviews, list) else []
    if (
        not isinstance(review_state, Mapping)
        or review_state.get("can_advance") is not True
        or len(current) != 1
    ):
        raise SdlcError(
            f"{label}缺少唯一、当前且通过的审核结果。"
            f"下一步：重新完成{label}并生成正式包。",
            exit_code=1,
        )
    return current[0]


def _reviews_and_confirmation(
    draft: Mapping[str, object],
    package: Mapping[str, object],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    confirmation = draft.get("_requirement_confirmation_state")
    if (
        not isinstance(confirmation, Mapping)
        or confirmation.get("status") != "ready"
        or confirmation.get("can_advance") is not True
        or not isinstance(confirmation.get("current_confirmation"), Mapping)
    ):
        raise SdlcError(
            "当前需求确认缺失或已经失效。下一步：让用户确认当前需求版本后重新生成正式包。",
            exit_code=1,
        )
    requirement_review = _current_review(
        draft,
        state_field="_requirement_review_state",
        stage="requirement_split",
        label="需求审核",
    )
    design_review = _current_review(
        draft,
        state_field="_integrated_design_review_state",
        stage="integrated_design",
        label="整体设计审核",
    )
    declared = package.get("reviews")
    if (
        not isinstance(declared, Mapping)
        or declared.get("requirement_split") != requirement_review.get("review_id")
        or declared.get("integrated_design") != design_review.get("review_id")
    ):
        raise SdlcError(
            "正式包声明的审核 REV 不是当前需求审核和整体设计审核。"
            "下一步：使用当前审核结果重新生成正式包。",
            exit_code=1,
        )
    return requirement_review, design_review


def _revision_matches(
    paths,
    draft: Mapping[str, object],
    package: Mapping[str, object],
) -> None:
    import json

    from codex_sdlc.core.artifact_index import validate_draft_root

    draft_id = str(draft.get("draft_id") or "")
    validate_draft_root(paths, draft_id)
    index_path = paths.draft_artifact_index_file(draft_id)
    if index_path.is_symlink() or not index_path.is_file():
        raise SdlcError(
            "产物索引不存在或不是普通文件。"
            "下一步：修复当前 DRAFT 的 artifact-index.v1 后重试。",
            exit_code=1,
        )
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(
            "产物索引读取失败或不是有效 JSON。"
            "下一步：修复当前 DRAFT 的 artifact-index.v1 后重试。",
            exit_code=1,
        ) from exc
    current_revision = (
        str(index.get("draft_revision_sha256") or "")
        if isinstance(index, Mapping) and index.get("draft_id") == draft_id
        else ""
    )
    if re.fullmatch(r"[0-9a-f]{64}", current_revision) is None:
        raise SdlcError(
            "产物索引缺少当前 DRAFT 的有效修订哈希。"
            "下一步：修复当前 DRAFT 的 artifact-index.v1 后重试。",
            exit_code=1,
        )
    # 旧 formal 输入必须先和权威 revision 字段比较；只有 revision 当前有效，
    # 后续阶段才校验 manifest、索引内容和审核输入，避免较晚漂移遮住更早错误。
    if package.get("source_revision_sha256") != current_revision:
        raise SdlcError(
            "formal.v3 引用的 DRAFT 修订已经过期。"
            "下一步：从当前 DRAFT 重新生成 artifact-index.v1 和 formal.v3 正式包。",
            exit_code=1,
        )


def _review_inputs_match_manifest(
    package: Mapping[str, object],
    reviews: tuple[Mapping[str, object], Mapping[str, object]],
) -> None:
    manifest = package.get("artifact_manifest")
    items = manifest if isinstance(manifest, list) else []
    expected = {
        ("requirement_review_input", str(reviews[0].get("review_id") or "")),
        ("integrated_design_review_input", str(reviews[1].get("review_id") or "")),
    }
    actual = {
        (str(item.get("artifact_type") or ""), str(item.get("business_id") or ""))
        for item in items
        if isinstance(item, Mapping)
        and str(item.get("artifact_type") or "").endswith("_review_input")
    }
    if actual != expected:
        raise SdlcError(
            "正式清单中的审核输入与当前两类审核不一致。"
            "下一步：重新生成 artifact-index.v1 和 formal.v3 正式包。",
            exit_code=1,
        )


def _reference_index(
    paths,
    state: Mapping[str, object],
    draft: Mapping[str, object],
    package: Mapping[str, object],
    requirement_id: str,
) -> dict[str, object]:
    from codex_sdlc.core.reference_index import build_reference_index_document

    import_record = draft.get("requirement_import")
    mapping = import_record.get("mapping") if isinstance(import_record, Mapping) else None
    if not isinstance(mapping, Mapping):
        raise SdlcError(
            "当前 DRAFT 缺少可复核的需求导入编号映射。"
            "下一步：重新导入需求拆分并生成正式包。",
            exit_code=1,
        )
    return build_reference_index_document(
        paths.draft_dir(str(draft["draft_id"])),
        requirement_id,
        package["artifact_manifest"],  # type: ignore[arg-type]
        requirement_mapping=mapping,
        design_references=(
            draft.get("design_references")
            if isinstance(draft.get("design_references"), list)
            else ()
        ),
        design_artifacts=(
            draft.get("design_artifacts")
            if isinstance(draft.get("design_artifacts"), list)
            else ()
        ),
    )


def _next_requirement_id(state: Mapping[str, object]) -> str:
    requirements = state.get("requirements")
    ids = [
        str(key)
        for key in requirements
        if isinstance(requirements, Mapping)
        and re.fullmatch(r"REQ-[0-9]{3,}", str(key))
    ] if isinstance(requirements, Mapping) else []
    next_value = max((int(item.split("-", 1)[1]) for item in ids), default=0) + 1
    return f"REQ-{next_value:03d}"


def _validate_requirement_target(
    paths,
    state: Mapping[str, object],
    requirement_id: str,
) -> str:
    if requirement_id != _next_requirement_id(state):
        raise SdlcError(
            "正式需求编号在校验期间发生变化。下一步：重新运行 start --file。",
            exit_code=1,
        )
    parent = paths.requirements_dir
    if (
        paths.sdlc_dir.is_symlink()
        or parent.is_symlink()
        or not parent.is_dir()
    ):
        raise SdlcError(
            "正式需求目标目录不存在或不是普通目录。"
            "下一步：先运行项目体检并修复 requirements 目录。",
            exit_code=1,
        )
    try:
        resolved_sdlc = paths.sdlc_dir.resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_sdlc)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(
            "正式需求目标目录越过当前项目边界。下一步：修复 requirements 目录后重试。",
            exit_code=1,
        ) from exc
    collisions = [
        item
        for item in parent.iterdir()
        if item.name == requirement_id or item.name.startswith(f"{requirement_id}-")
    ]
    if collisions:
        raise SdlcError(
            f"正式需求编号或目标目录已经存在：{requirement_id}。"
            "下一步：运行项目体检，确认事件状态与正式目录一致后重试。",
            exit_code=1,
        )
    target = parent / requirement_id
    if target.is_symlink() or target.exists():
        raise SdlcError(
            f"正式需求目标目录已经存在或是符号链接：{target.name}。"
            "下一步：运行项目体检并处理冲突目录。",
            exit_code=1,
        )
    return target.name


def preflight_document_first_start(
    paths,
    package: Mapping[str, object],
    *,
    state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """按固定顺序完成全部只读门禁，任何失败都发生在正式写入之前。"""

    from codex_sdlc.core.formal_manifest_contract import (
        validate_formal_package_contract,
    )

    # 这些来源、状态、清单和完整哈希必须在任何写入前显式复核；否则旧缓存
    # 可以在写入已经发生后才暴露漂移，留下错误编号、目录或业务状态。
    candidate = _document_first_schema(package)
    current_state = _current_state(paths, state)
    draft = _explicit_draft(current_state, candidate)
    draft, current_state = _revision_gate_view(current_state, draft)
    _ready_status(draft)
    reviews = _reviews_and_confirmation(draft, candidate)
    _revision_matches(paths, draft, candidate)
    try:
        validated = validate_formal_package_contract(
            paths,
            candidate,
            state=current_state,
        )["package"]
    except SdlcError as exc:
        raise SdlcError(
            f"{exc}下一步：重新生成当前 DRAFT 的修订、产物清单和审核输入后重试。",
            exit_code=1,
        ) from exc
    _review_inputs_match_manifest(validated, reviews)
    requirement_id = _next_requirement_id(current_state)
    try:
        reference_index = _reference_index(
            paths,
            current_state,
            draft,
            validated,
            requirement_id,
        )
    except SdlcError as exc:
        raise SdlcError(
            f"{exc}下一步：修正当前结构化产物、稳定编号映射和精确引用后重试。",
            exit_code=1,
        ) from exc
    if validated.get("open_questions") != []:
        raise SdlcError(
            "正式包仍有未确认问题。下一步：解决全部 open_questions 后重新生成正式包。",
            exit_code=1,
        )
    # 目标目录校验放在最后重新执行一次，防止引用校验耗时期间出现同名目录。
    target_directory = _validate_requirement_target(
        paths,
        current_state,
        requirement_id,
    )
    return {
        "mode": "document-first",
        "package": validated,
        "source_draft_id": str(draft["draft_id"]),
        "requirement_id": requirement_id,
        "target_directory": target_directory,
        "reference_index": reference_index,
    }


def clean_text(value: object) -> str:
    return str(value or "").strip()


def has_explicit_content(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(clean_text(value))
    if isinstance(value, list):
        return any(has_explicit_content(item) for item in value)
    return bool(clean_text(value))


def first_raw_value(data: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def raw_public_id_issue(prefix: str, label: str, item: object, index: int) -> str:
    if not isinstance(item, dict):
        return f"{label}第 {index} 条必须显式提供合法编号，例如 {prefix}-001。"
    raw_id = item.get("id")
    clean_id = clean_text(raw_id).upper()
    if not clean_id:
        return f"{label}第 {index} 条缺少编号，必须显式提供 {prefix}-xxx。"
    if not re.fullmatch(rf"{prefix}-\d{{3}}", clean_id):
        return f"{label}第 {index} 条编号不合法：{raw_id}，必须使用 {prefix}-xxx。"
    return ""


def raw_start_package_contract_issues(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    raw_groups = [
        ("functional_requirements", ("functional_requirements", "requirements", "requirement_points"), "FR", "功能需求"),
        ("acceptance_criteria", ("acceptance_criteria", "acceptance_points", "acceptance"), "AC", "验收标准"),
        ("test_cases", ("test_cases", "tests", "test_matrix"), "TC", "测试用例"),
    ]
    for _field, keys, prefix, label in raw_groups:
        raw_items = first_raw_value(data, keys)
        if not isinstance(raw_items, list) or not raw_items:
            continue
        for index, item in enumerate(raw_items, start=1):
            issue = raw_public_id_issue(prefix, label, item, index)
            if issue:
                issues.append(issue)
            if not isinstance(item, dict):
                continue
            public_id = clean_text(item.get("id")).upper() or f"{prefix}-{index:03d}"
            # 原始字段检查必须发生在归一化之前。summary、说明或关联关系都
            # 不能相互借用，否则缺字段会在转换时被悄悄伪造成正式内容。
            required_fields = {
                "FR": (
                    ("title", "标题"),
                    ("description", "说明"),
                    ("rules", "规则"),
                    ("inputs", "输入"),
                    ("outputs", "输出"),
                    ("triggers", "触发条件"),
                    ("data_changes", "保存或改变的数据"),
                    ("permissions", "权限"),
                    ("exceptions", "异常"),
                    ("boundaries", "边界"),
                ),
                "AC": (("requirement_ids", "覆盖需求"), ("operation", "操作"), ("expected", "预期"), ("pass_standard", "通过标准")),
                "TC": (("acceptance_ids", "覆盖验收"), ("requirement_ids", "覆盖需求"), ("type", "类型"), ("operation", "操作"), ("expected", "预期"), ("pass_standard", "通过标准")),
            }[prefix]
            for field_name, field_label in required_fields:
                if not has_explicit_content(item.get(field_name)):
                    issues.append(f"{public_id} 缺少原始{field_label}。")

    for field_name, field_label in REQUIRED_FORMAL_FIELDS:
        value = first_raw_value(data, RAW_FORMAL_FIELD_ALIASES[field_name])
        if not has_explicit_content(value):
            issues.append(f"正式建档包缺少{field_label}，如果确实不涉及也要显式写明“不涉及”。")
    return issues


def design_contract_issues(design: dict[str, Any]) -> list[str]:
    """正式技术设计必须有完整章节，summary 只能帮助阅读，不能充当设计。"""

    issues: list[str] = []
    for field_name, field_label in DESIGN_REQUIRED_FIELDS:
        value = design.get(field_name)
        if not has_explicit_content(value):
            issues.append(f"正式技术方案缺少{field_label}。")
    return issues


def traceability_contract_issues(package: dict[str, Any]) -> list[str]:
    """保证 FR、AC、TC 与技术覆盖可以追到同一份执行依据。"""

    issues: list[str] = []
    requirement_ids = {clean_text(item.get("id")) for item in package.get("requirement_points", []) if isinstance(item, dict) and clean_text(item.get("id"))}
    active_cases_by_ac: dict[str, int] = {}
    covered_frs_by_ac: dict[str, set[str]] = {}
    for acceptance in package.get("acceptance_points", []):
        if not isinstance(acceptance, dict):
            continue
        acceptance_id = clean_text(acceptance.get("id"))
        covered_frs_by_ac[acceptance_id] = {clean_text(ref) for ref in acceptance.get("requirement_ids", []) if clean_text(ref)}
    for case in package.get("test_cases", []):
        if not isinstance(case, dict) or clean_text(case.get("status") or "active") != "active":
            continue
        for acceptance_id in case.get("acceptance_ids", []):
            active_cases_by_ac[clean_text(acceptance_id)] = active_cases_by_ac.get(clean_text(acceptance_id), 0) + 1
    for requirement_id in sorted(requirement_ids):
        if not any(requirement_id in covered for covered in covered_frs_by_ac.values()):
            issues.append(f"{requirement_id} 没有验收标准覆盖。")
    for acceptance_id in sorted(covered_frs_by_ac):
        if not active_cases_by_ac.get(acceptance_id):
            issues.append(f"{acceptance_id} 没有 active 测试用例覆盖。")
    coverage_values = _flatten_values(package.get("design", {}).get("requirement_coverage", []) if isinstance(package.get("design"), dict) else [])
    covered_design_ids = {
        match.group(0).upper()
        for value in coverage_values
        for match in re.finditer(r"\bFR-\d{3}\b", value, flags=re.I)
    }
    for requirement_id in sorted(requirement_ids):
        if requirement_id not in covered_design_ids:
            issues.append(f"{requirement_id} 缺少技术覆盖说明。")
    return issues


def formal_section_issues(package: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field_name, field_label in REQUIRED_FORMAL_FIELDS:
        if not has_explicit_content(package.get(field_name)):
            issues.append(f"正式建档包缺少{field_label}，如果确实不涉及也要显式写明“不涉及”。")
    return issues


def executable_package_issues(package: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    requirement_ids: set[str] = set()
    acceptance_ids: set[str] = set()
    for item in package.get("requirement_points", []):
        if not isinstance(item, dict):
            continue
        point_id = clean_text(item.get("id") or "FR")
        if clean_text(item.get("id")):
            requirement_ids.add(clean_text(item.get("id")))
        missing: list[str] = []
        if not clean_text(item.get("id")):
            missing.append("编号")
        if not clean_text(item.get("title")):
            missing.append("标题")
        if not clean_text(item.get("description") or item.get("summary")):
            missing.append("说明")
        rules = [clean_text(rule) for rule in item.get("rules", []) if clean_text(rule)] if isinstance(item.get("rules"), list) else []
        if not rules:
            missing.append("规则")
        for field_name, field_label in (
            ("inputs", "输入"),
            ("outputs", "输出"),
            ("triggers", "触发条件"),
            ("data_changes", "保存或改变的数据"),
            ("permissions", "权限"),
            ("exceptions", "异常"),
            ("boundaries", "边界"),
        ):
            value = item.get(field_name)
            if not has_explicit_content(value):
                missing.append(field_label)
        if missing:
            issues.append(f"{point_id} 缺少可执行字段：{'、'.join(missing)}。")

    for item in package.get("acceptance_points", []):
        if not isinstance(item, dict):
            continue
        point_id = clean_text(item.get("id") or "AC")
        if clean_text(item.get("id")):
            acceptance_ids.add(clean_text(item.get("id")))
        missing: list[str] = []
        if not item.get("requirement_ids"):
            missing.append("覆盖需求")
        if not clean_text(item.get("operation")):
            missing.append("操作")
        if not clean_text(item.get("expected")):
            missing.append("预期")
        if not clean_text(item.get("pass_standard")):
            missing.append("通过标准")
        if missing:
            issues.append(f"{point_id} 缺少可执行字段：{'、'.join(missing)}。")
        unknown_requirement_ids = [
            clean_text(ref)
            for ref in item.get("requirement_ids", [])
            if clean_text(ref) and clean_text(ref) not in requirement_ids
        ] if isinstance(item.get("requirement_ids"), list) else []
        if unknown_requirement_ids:
            issues.append(f"{point_id} 关联了不存在的功能需求：{'、'.join(unknown_requirement_ids)}。")
    for item in package.get("test_cases", []):
        if not isinstance(item, dict):
            continue
        case_id = clean_text(item.get("id") or "TC")
        missing = []
        if not item.get("acceptance_ids"):
            missing.append("覆盖验收")
        if not item.get("requirement_ids"):
            missing.append("覆盖需求")
        if not clean_text(item.get("type")):
            missing.append("类型")
        if not clean_text(item.get("operation") or item.get("method")):
            missing.append("操作")
        if not clean_text(item.get("expected")):
            missing.append("预期")
        if not clean_text(item.get("pass_standard")):
            missing.append("通过标准")
        if missing:
            issues.append(f"{case_id} 缺少可执行字段：{'、'.join(missing)}。")
        unknown_acceptance_ids = [
            clean_text(ref)
            for ref in item.get("acceptance_ids", [])
            if clean_text(ref) and clean_text(ref) not in acceptance_ids
        ] if isinstance(item.get("acceptance_ids"), list) else []
        unknown_requirement_ids = [
            clean_text(ref)
            for ref in item.get("requirement_ids", [])
            if clean_text(ref) and clean_text(ref) not in requirement_ids
        ] if isinstance(item.get("requirement_ids"), list) else []
        if unknown_acceptance_ids:
            issues.append(f"{case_id} 关联了不存在的验收标准：{'、'.join(unknown_acceptance_ids)}。")
        if unknown_requirement_ids:
            issues.append(f"{case_id} 关联了不存在的功能需求：{'、'.join(unknown_requirement_ids)}。")
    return issues


def _flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_values(item))
        return result
    # 项目命令仍需兼容 macOS 自带的 Python 3.9，isinstance 的类型集合使用元组写法。
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_values(item))
        return result
    return [str(value)]


def open_question_issues(package: dict[str, Any]) -> list[str]:
    return [
        f"未确认问题仍未解决：{item}"
        for item in package.get("open_questions", [])
        if clean_text(item)
    ]


def start_package_contract_issues(package: dict[str, Any], *, source_draft: dict[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    issues.extend(open_question_issues(package))
    issues.extend(formal_section_issues(package))
    issues.extend(executable_package_issues(package))
    design = package.get("design") if isinstance(package.get("design"), dict) else {}
    issues.extend(design_contract_issues(design))
    issues.extend(traceability_contract_issues(package))
    return issues


def effective_change_contract_issues(changes: Any) -> list[str]:
    """已确认变更是正式附录，不能缺编号、说明或使用未知状态。"""

    if changes is None:
        return []
    if not isinstance(changes, list):
        return ["已确认变更附录必须是数组。"]
    issues: list[str] = []
    seen: set[str] = set()
    allowed_statuses = {"effective", "planned", "resolved", "verified"}
    for index, change in enumerate(changes, start=1):
        if not isinstance(change, dict):
            issues.append(f"第 {index} 条已确认变更不是对象。")
            continue
        change_id = clean_text(change.get("change_id"))
        if not re.fullmatch(r"CHG-\d{3}", change_id):
            issues.append(f"第 {index} 条已确认变更编号无效：{change_id or '空'}。")
        elif change_id in seen:
            issues.append(f"已确认变更编号重复：{change_id}。")
        seen.add(change_id)
        if clean_text(change.get("status")) not in allowed_statuses:
            issues.append(f"{change_id or f'第 {index} 条变更'} 的状态无效。")
        if not clean_text(change.get("summary")):
            issues.append(f"{change_id or f'第 {index} 条变更'} 缺少摘要。")
        if not clean_text(change.get("description")):
            issues.append(f"{change_id or f'第 {index} 条变更'} 缺少说明。")
        acceptance = change.get("acceptance_points", [])
        if not isinstance(acceptance, list) or any(not clean_text(item) for item in acceptance):
            issues.append(f"{change_id or f'第 {index} 条变更'} 的验收要求格式无效。")
    return issues
