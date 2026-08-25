from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from codex_sdlc.core import draft_contract, draft_sections
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import canonical_sha256, contract_sha256

UNFINISHED_STATUSES = {
    "discussing",
    "needs_user",
    "requirement_ready",
    "requirement_reviewing",
    "requirement_confirmed",
    "designing",
    "design_reviewing",
    "design_ready",
    "reviewing",
    "start_ready",
}

ALLOWED_STATUSES = (*UNFINISHED_STATUSES, "started")
START_REVIEWABLE_STATUSES = {
    "design_reviewing",
    "design_ready",
    "reviewing",
    "start_ready",
}


@dataclass(frozen=True)
class DraftBlocker:
    """状态计算使用的结构化阻断项，不把展示文字当成判断条件。"""

    code: str
    source_id: str
    status: str
    reference: str = ""


@dataclass(frozen=True)
class DraftQuestion:
    """从明确 CAP、拆分位置或覆盖单元得到的待处理问题。"""

    question_id: str = ""
    kind: str = ""
    source_id: str = ""
    status: str = ""
    text: str = ""


@dataclass(frozen=True)
class DraftAssessment:
    """从草稿内容推导出的唯一状态结论。

    事件里的 status 只是历史记录，待确认问题只认结构化 questions 和模型问题合同。
    status、next、start 和体检都应基于这份结果给用户同一个答案。
    """

    effective_status: str
    open_questions: tuple[str, ...]
    missing_requirement_items: tuple[str, ...]
    missing_design_items: tuple[str, ...]
    conflicts: tuple[str, ...]
    lost_facts: tuple[object, ...]
    can_start: bool
    next_action: str
    reason: str
    facts_status: str = "facts_missing"
    blockers: tuple[DraftBlocker, ...] = ()
    structured_questions: tuple[DraftQuestion, ...] = ()


def clean_status(value: object) -> str:
    return str(value or "").strip()


def draft_status(draft: dict[str, Any]) -> str:
    return clean_status(draft.get("status"))


def is_unfinished_draft(draft: dict[str, Any]) -> bool:
    return draft_status(draft) in UNFINISHED_STATUSES


def can_start_review(draft: dict[str, Any]) -> bool:
    return draft_status(draft) in START_REVIEWABLE_STATUSES


def is_started_draft(draft: dict[str, Any]) -> bool:
    return draft_status(draft) == "started"


def draft_questions(draft: dict[str, Any]) -> list[str]:
    return [str(item).strip() for item in draft.get("questions", []) if str(item).strip()]


def uses_structured_requirement_stage(draft: dict[str, Any]) -> bool:
    """只有明确的新合同对象才能开启新状态链，旧 DRAFT 展示文字不会触发迁移。"""

    return bool(
        draft.get("_structured_stage_enabled") is True
        or isinstance(draft.get("requirement_split"), dict)
        or isinstance(draft.get("requirement_coverage"), dict)
        or draft.get("structured_captures")
        or draft.get("decision_records")
        or draft.get("_material_manifest_enabled") is True
    )


def _mapping_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _active_materials(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in _mapping_items(draft.get("materials"))
        if item.get("status") != "archived"
    ]


def decision_identity_sha256(decision: Mapping[str, object]) -> str:
    """决定身份固定包含问题全文定位，错误定位不会挡住修正后的决定。"""

    question = decision.get("question")
    return canonical_sha256(
        {
            "question": {
                "text": question.get("text") if isinstance(question, dict) else None,
                "capture_ref": (
                    question.get("capture_ref") if isinstance(question, dict) else None
                ),
                "reference": (
                    question.get("reference") if isinstance(question, dict) else None
                ),
            },
            "scope": decision.get("scope"),
        }
    )


def decision_matches_question_capture(
    decision: Mapping[str, object], capture: Mapping[str, object]
) -> bool:
    """DEC 只认问题 CAP 的来源定位；写入、重放和状态计算共用这一条规则。"""

    question = decision.get("question")
    if (
        not isinstance(question, dict)
        or capture.get("capture_type") != "question"
        or question.get("capture_ref") != capture.get("capture_id")
    ):
        return False
    reference = question.get("reference")
    source_reference = capture.get("source_reference")
    if not isinstance(reference, dict) or not isinstance(source_reference, dict):
        return False
    try:
        return canonical_sha256(reference) == canonical_sha256(source_reference)
    except (SdlcError, TypeError, ValueError):
        return False


def capture_effective_status(
    draft: Mapping[str, object], capture: Mapping[str, object]
) -> str:
    """初始 CAP 保持不可变，当前状态只从独立转换事实表读取。"""

    capture_id = str(capture.get("capture_id") or "")
    statuses = draft.get("capture_statuses")
    if isinstance(statuses, dict):
        current = statuses.get(capture_id)
        if isinstance(current, str) and current:
            return current
    return str(capture.get("status") or "")


def _requirement_material(item: dict[str, Any]) -> bool:
    roles = item.get("roles")
    return item.get("type") == "requirement" or (
        isinstance(roles, list) and "requirement" in roles
    )


def _exact_decision_question_refs(draft: dict[str, Any]) -> set[str]:
    """DEC 只按 capture_ref 和完整引用匹配问题，问题文字不参与关闭判断。"""

    captures = {
        str(item.get("capture_id") or ""): item
        for item in _mapping_items(draft.get("structured_captures"))
        if str(item.get("capture_id") or "")
    }
    resolved: set[str] = set()
    for decision in _mapping_items(draft.get("decision_records")):
        if decision.get("status") != "confirmed":
            continue
        question = decision.get("question")
        if not isinstance(question, dict):
            continue
        capture_id = str(question.get("capture_ref") or "")
        capture = captures.get(capture_id)
        decision_capture = captures.get(str(decision.get("source_capture_id") or ""))
        if (
            capture is None
            or capture.get("capture_type") != "question"
            or decision_capture is None
            or decision_capture.get("capture_type") != "decision"
        ):
            continue
        if decision_matches_question_capture(decision, capture):
            resolved.add(capture_id)
    return resolved


def _requirement_artifacts_current(draft: dict[str, Any]) -> bool:
    split = draft.get("requirement_split")
    coverage = draft.get("requirement_coverage")
    receipt = draft.get("requirement_import")
    if not all(isinstance(item, dict) for item in (split, coverage, receipt)):
        return False
    try:
        split_sha256 = contract_sha256(split, schema_name="requirement-split.v1")
    except (SdlcError, TypeError, ValueError):
        return False
    if coverage.get("requirement_split_sha256") != split_sha256:
        return False
    declared = split.get("input_material_hashes")
    if not isinstance(declared, dict):
        return False
    active_files = {
        str(item.get("material_id") or ""): str(item.get("sha256") or "")
        for item in _active_materials(draft)
        if item.get("source_kind") == "file" and str(item.get("material_id") or "")
    }
    required_ids = {
        str(item.get("material_id") or "")
        for item in _active_materials(draft)
        if item.get("source_kind") == "file" and _requirement_material(item)
    }
    if not required_ids or not required_ids.issubset(set(declared)):
        return False
    return all(active_files.get(str(material_id)) == digest for material_id, digest in declared.items())


def _structured_reason(blockers: list[DraftBlocker]) -> str:
    if not blockers:
        return "需求资料、拆分结果、覆盖关系、CAP 和结构化问题已经满足需求审核前置条件。"
    rendered = "；".join(
        f"{item.code}:{item.source_id}:{item.status}"
        + (f":{item.reference}" if item.reference else "")
        for item in blockers
    )
    return f"阻断项：{rendered}。"


def _structured_next_action(blockers: list[DraftBlocker]) -> str:
    if not blockers:
        return "$sdlc-status"
    first = blockers[0].code
    if first in {"material_missing", "material_unstable"}:
        return "$sdlc-material"
    if first in {"requirement_artifacts_missing", "requirement_artifacts_stale"}:
        return "codex-sdlc draft requirements DRAFT-xxx --split-file ... --coverage-file ..."
    return "$sdlc-discuss --file 结构化增量.json"


def _structured_design_next_action(
    draft_id: str,
    blockers: tuple[DraftBlocker, ...],
) -> str:
    """下一步只看稳定阻断码，不从错误文字或 Markdown 标题猜命令。"""

    if not blockers:
        return "$sdlc-status"
    first = blockers[0].code
    if first == "design_reference_missing":
        return f"codex-sdlc design-reference-confirm {draft_id} DES-NNN"
    if first in {"design_plan_missing", "design_plan_stale"}:
        return f"codex-sdlc design-plan {draft_id} --file 设计总计划.json"
    if first in {
        "design_artifact_missing",
        "design_artifact_stale",
    }:
        return f"codex-sdlc design-artifact {draft_id} --file 模块设计.json"
    if first == "design_module_blocked":
        return "$sdlc-status"
    if first in {"design_summary_missing", "design_summary_stale"}:
        return f"codex-sdlc design-summary {draft_id} --file 总体设计.json"
    return "$sdlc-status"


def _assess_structured_design_stage(
    draft: dict[str, Any],
) -> DraftAssessment:
    """需求确认后只消费 DES、计划、模块、总体说明和结构化阻断项。"""

    draft_id = str(draft.get("draft_id") or "DRAFT")
    review_state = draft.get("_integrated_design_review_state")
    reviews = (
        review_state.get("reviews")
        if isinstance(review_state, Mapping)
        else []
    )
    current_reviews = [
        item
        for item in reviews
        if isinstance(item, Mapping)
        and item.get("stage") == "integrated_design"
        and item.get("owner_id") == draft_id
        and item.get("is_current") is True
    ] if isinstance(reviews, list) else []
    review_started = bool(
        current_reviews
        or (
            isinstance(review_state, Mapping)
            and review_state.get("has_review_request") is True
        )
    )
    confirmation_state = draft.get("_requirement_confirmation_state")
    current_confirmation = (
        confirmation_state.get("current_confirmation")
        if isinstance(confirmation_state, Mapping)
        else None
    )
    current_confirmation_sha256 = (
        str(current_confirmation.get("confirmation_sha256") or "")
        if isinstance(current_confirmation, Mapping)
        else ""
    )
    current_designs = [
        item
        for item in _mapping_items(draft.get("design_references"))
        if item.get("status") == "confirmed"
        and item.get("requirement_confirmation_sha256")
        == current_confirmation_sha256
    ]
    reference_blockers: tuple[DraftBlocker, ...] = ()
    if not current_designs:
        blocker = DraftBlocker(
            "design_reference_missing",
            draft_id,
            "missing",
        )
        if not review_started:
            return DraftAssessment(
                effective_status="requirement_confirmed",
                open_questions=(),
                missing_requirement_items=(),
                missing_design_items=("已确认技术方案引用",),
                conflicts=(),
                lost_facts=(),
                can_start=False,
                next_action=(
                    f"codex-sdlc design-reference-confirm {draft_id} DES-NNN"
                ),
                reason="当前需求已经确认，设计阶段还缺少与该确认记录一致的技术方案引用。",
                facts_status="structured_requirement",
                blockers=(blocker,),
                structured_questions=(),
            )
        reference_blockers = (blocker,)

    stage = draft.get("design_stage")
    if not isinstance(stage, Mapping):
        stage = {
            "ready_for_review": False,
            "blockers": [
                {
                    "code": "design_plan_missing",
                    "source_id": draft_id,
                    "status": "missing",
                    "reference": "",
                }
            ],
        }
    raw_blockers = stage.get("blockers")
    stage_blockers = tuple(
        DraftBlocker(
            code=str(item.get("code") or "design_stage_invalid"),
            source_id=str(item.get("source_id") or draft_id),
            status=str(item.get("status") or "blocked"),
            reference=str(item.get("reference") or ""),
        )
        for item in raw_blockers
        if isinstance(item, Mapping)
    ) if isinstance(raw_blockers, list) else (
        DraftBlocker("design_stage_invalid", draft_id, "invalid"),
    )
    blockers = (*reference_blockers, *stage_blockers)
    if stage.get("plan_status") == "missing" and not review_started:
        # T-009 的独立交付只确认技术方案引用，尚未选择模块；在总计划真正登记前
        # 保持 requirement_confirmed，既保留累计合同，也避免把半个设计阶段写成已开工。
        return DraftAssessment(
            effective_status="requirement_confirmed",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=("开发设计总计划",),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action=_structured_design_next_action(draft_id, blockers),
            reason="技术方案引用已经确认，开发设计总计划尚未登记。",
            facts_status="structured_design",
            blockers=blockers,
            structured_questions=(),
        )
    if stage.get("ready_for_review") is True and not blockers:
        if (
            len(current_reviews) == 1
            and current_reviews[0].get("request_status") == "completed"
            and current_reviews[0].get("effective_status") == "passed"
            and current_reviews[0].get("can_advance") is True
        ):
            return DraftAssessment(
                effective_status="start_ready",
                open_questions=(),
                missing_requirement_items=(),
                missing_design_items=(),
                conflicts=(),
                lost_facts=(),
                can_start=True,
                next_action="$sdlc-start",
                reason="当前完整设计已经通过独立审核，可以正式建档。",
                facts_status="structured_design",
                blockers=(),
                structured_questions=(),
            )

    if review_started or (
        stage.get("ready_for_review") is True and not blockers
    ):
        current_review = current_reviews[0] if len(current_reviews) == 1 else None
        if isinstance(current_review, Mapping):
            review_status = str(
                current_review.get("effective_status") or "pending"
            )
        elif isinstance(review_state, Mapping):
            review_status = str(review_state.get("status") or "pending")
        else:
            review_status = "pending"
        if (
            isinstance(review_state, Mapping)
            and review_state.get("status") == "rejected"
        ):
            blocker_code = "integrated_design_review_invalid"
            blocker_status = "invalid"
        elif review_status == "stale":
            blocker_code = "integrated_design_review_stale"
            blocker_status = "stale"
        elif review_status == "needs_fix":
            blocker_code = "integrated_design_review_needs_fix"
            blocker_status = "needs_fix"
        else:
            blocker_code = "integrated_design_review_pending"
            blocker_status = "pending"
        review_blocker = DraftBlocker(
            blocker_code,
            draft_id,
            blocker_status,
        )
        combined_blockers = (*blockers, review_blocker)
        return DraftAssessment(
            effective_status="design_reviewing",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=tuple(
                item.source_id for item in blockers
            ),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action=(
                _structured_design_next_action(draft_id, blockers)
                if blockers
                else "$sdlc-design"
            ),
            reason=_structured_reason(list(combined_blockers)),
            facts_status="structured_design",
            blockers=combined_blockers,
            structured_questions=(),
        )

    return DraftAssessment(
        effective_status="designing",
        open_questions=(),
        missing_requirement_items=(),
        missing_design_items=tuple(
            item.source_id for item in blockers
        ),
        conflicts=(),
        lost_facts=(),
        can_start=False,
        next_action=_structured_design_next_action(draft_id, blockers),
        reason=_structured_reason(list(blockers)),
        facts_status="structured_design",
        blockers=blockers,
        structured_questions=(),
    )


def _assess_structured_requirement_stage(draft: dict[str, Any]) -> DraftAssessment:
    draft_id = str(draft.get("draft_id") or "DRAFT")
    blockers: list[DraftBlocker] = []
    questions: list[DraftQuestion] = []
    active_materials = _active_materials(draft)
    requirement_materials = [item for item in active_materials if _requirement_material(item)]
    if not requirement_materials:
        blockers.append(DraftBlocker("material_missing", draft_id, "missing"))
    for material in active_materials:
        material_id = str(material.get("material_id") or "MAT")
        if material.get("source_kind") == "file":
            digest = str(material.get("sha256") or "")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                blockers.append(DraftBlocker("material_unstable", material_id, "invalid_hash"))
        elif material.get("source_kind") == "external-reference":
            evidence = material.get("version_evidence")
            evidence_status = evidence.get("status") if isinstance(evidence, dict) else "missing"
            if material.get("status") != "confirmed" or evidence_status != "confirmed":
                blockers.append(
                    DraftBlocker("material_unstable", material_id, str(evidence_status or "missing"))
                )

    # 文件字节和外部版本证据由状态层只读复核，这里只消费结构化复核结果。
    for issue in _mapping_items(draft.get("_material_integrity_issues")):
        blockers.append(
            DraftBlocker(
                "material_unstable",
                str(issue.get("source_id") or "MAT"),
                str(issue.get("status") or "drifted"),
                str(issue.get("reference") or ""),
            )
        )

    for issue in _mapping_items(draft.get("_reference_issues")):
        blockers.append(
            DraftBlocker(
                "reference_drift",
                str(issue.get("source_id") or draft_id),
                str(issue.get("status") or "drifted"),
                str(issue.get("reference") or ""),
            )
        )

    split = draft.get("requirement_split")
    coverage = draft.get("requirement_coverage")
    receipt = draft.get("requirement_import")
    has_artifacts = all(isinstance(item, dict) for item in (split, coverage, receipt))
    if not has_artifacts:
        blockers.append(DraftBlocker("requirement_artifacts_missing", draft_id, "missing"))
    elif not _requirement_artifacts_current(draft):
        blockers.append(DraftBlocker("requirement_artifacts_stale", draft_id, "stale"))

    if isinstance(split, dict):
        try:
            split_key = contract_sha256(split, schema_name="requirement-split.v1")[:12]
        except (SdlcError, TypeError, ValueError):
            split_key = "invalid-split"
        raw_questions = split.get("open_questions")
        if isinstance(raw_questions, list):
            for index, text in enumerate(raw_questions):
                question_id = f"{split_key}:open_questions[{index}]"
                questions.append(
                    DraftQuestion(question_id, "requirement_open_question", question_id, "open", str(text))
                )
                blockers.append(DraftBlocker("open_question", question_id, "open"))

    if isinstance(coverage, dict):
        mapping = receipt.get("mapping") if isinstance(receipt, dict) else {}
        mapping = mapping if isinstance(mapping, dict) else {}
        for unit in _mapping_items(coverage.get("units")):
            status = str(unit.get("status") or "")
            if status not in {"needs_user", "needs_material"}:
                continue
            client_key = str(unit.get("client_key") or "")
            source_id = str(mapping.get(client_key) or client_key or "coverage-unit")
            questions.append(DraftQuestion(source_id, status, source_id, status))
            blockers.append(DraftBlocker(status, source_id, status))

    resolved_capture_ids = _exact_decision_question_refs(draft)
    for capture in _mapping_items(draft.get("structured_captures")):
        capture_id = str(capture.get("capture_id") or "CAP")
        status = capture_effective_status(draft, capture)
        if capture.get("capture_type") == "question" and status == "pending" and capture_id not in resolved_capture_ids:
            questions.append(
                DraftQuestion(capture_id, "capture_question", capture_id, "open", str(capture.get("increment") or ""))
            )
            blockers.append(DraftBlocker("open_question", capture_id, "open"))
        if status == "pending":
            blockers.append(DraftBlocker("pending_capture", capture_id, "pending"))

    if blockers and draft.get("requirement_confirmations"):
        stale_blocker = DraftBlocker(
            "requirement_confirmation_stale",
            draft_id,
            "stale",
        )
        return DraftAssessment(
            effective_status="requirement_reviewing",
            open_questions=tuple(item.question_id for item in questions),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action="重新审核并确认当前需求输入",
            reason=_structured_reason([*blockers, stale_blocker]),
            facts_status="structured_requirement",
            blockers=tuple([*blockers, stale_blocker]),
            structured_questions=tuple(questions),
        )

    if blockers:
        return DraftAssessment(
            effective_status="discussing",
            open_questions=tuple(item.question_id for item in questions),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action=_structured_next_action(blockers),
            reason=_structured_reason(blockers),
            facts_status="structured_requirement",
            blockers=tuple(blockers),
            structured_questions=tuple(questions),
        )

    confirmation_state = draft.get("_requirement_confirmation_state")
    confirmation_valid = bool(
        isinstance(confirmation_state, Mapping)
        and confirmation_state.get("status") == "ready"
        and confirmation_state.get("can_advance") is True
    )
    if confirmation_valid:
        return _assess_structured_design_stage(draft)

    confirmation_status = (
        str(confirmation_state.get("status") or "pending")
        if isinstance(confirmation_state, Mapping)
        else "pending"
    )
    confirmation_blocker = DraftBlocker(
        "requirement_confirmation_stale"
        if confirmation_status == "stale"
        else "requirement_review_pending",
        draft_id,
        "stale" if confirmation_status == "stale" else "pending",
    )
    return DraftAssessment(
        effective_status="requirement_reviewing",
        open_questions=(),
        missing_requirement_items=(),
        missing_design_items=(),
        conflicts=(),
        lost_facts=(),
        can_start=False,
        next_action=(
            f"codex-sdlc draft requirement-review create {draft_id}"
            if confirmation_status != "stale"
            else "重新审核并确认当前需求输入"
        ),
        reason=_structured_reason([confirmation_blocker]),
        facts_status="structured_requirement",
        blockers=(confirmation_blocker,),
        structured_questions=(),
    )


def assess_draft(draft: dict[str, Any], *, previous_draft: dict[str, Any] | None = None) -> DraftAssessment:
    """所有命令和投影都从这里取得同一份确定性结果。"""

    if is_started_draft(draft):
        return DraftAssessment(
            effective_status="started",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action="",
            reason="该草稿已经生成正式需求，不能再修改或重复建档。",
        )
    if uses_structured_requirement_stage(draft):
        return _assess_structured_requirement_stage(draft)
    return _assess_legacy_draft(draft, previous_draft=previous_draft)


def _assess_legacy_draft(draft: dict[str, Any], *, previous_draft: dict[str, Any] | None = None) -> DraftAssessment:
    """按固定优先级计算草稿状态，避免各命令各自猜下一步。"""

    if is_started_draft(draft):
        return DraftAssessment(
            effective_status="started",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action="",
            reason="该草稿已经生成正式需求，不能再修改或重复建档。",
        )

    requirement_body = str(draft.get("requirement_body") or "")
    design_body = str(draft.get("design_body") or "")
    # 普通自然语言的主体、方向、动作、资源和条件只由模型事实与独立复核判断。
    # CLI 这里只检查用户主动选择的明确标签语法，不再把自然语言猜测升级为冲突。
    conflicts = [
        *draft_contract.explicit_permission_field_issues(
            draft_sections.requirement_section_clean_lines(requirement_body, "permission_rules"), "需求"
        ),
        *draft_contract.explicit_permission_field_issues(
            draft_contract.section_clean_lines(design_body, ("权限和安全", "权限安全")), "技术方案"
        ),
    ]
    lost_facts: tuple[object, ...] = ()
    # 业务问题和事实 freshness 是两条独立状态。即使仍有待确认问题，也要先算出旧复核是否已经过期，
    # 这样 status、next、doctor-deep 和 start 才不会给出四种不同答案。
    facts_status = "facts_missing"
    try:
        from codex_sdlc.services.draft_service import draft_fact_bundle
        from codex_sdlc.core import fact_gate

        fact_bundle = draft_fact_bundle(draft)
        if all(isinstance(fact_bundle.get(name), dict) for name in ("index", "requirement", "design", "review")):
            freshness = fact_gate.review_freshness(fact_bundle)
            facts_status = "stale" if freshness.status == "stale" else freshness.status
    except (KeyError, TypeError, ValueError):
        facts_status = "stale"
    # requirement Markdown 只是结构化 questions 的人读投影，不能反向改变状态。
    questions = list(draft_questions(draft))
    if questions:
        return DraftAssessment(
            effective_status="needs_user",
            open_questions=tuple(questions),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=tuple(conflicts),
            lost_facts=tuple(lost_facts),
            can_start=False,
            next_action="$sdlc-discuss 补充已确认结论",
            reason=(
                "存在会影响实现或验收的待确认问题；旧事实复核也已过期。确认问题后，请重新生成来源索引、facts，并由另一个任务完成独立复核。"
                if facts_status == "stale" else "存在会影响实现或验收的待确认问题。"
            ),
            facts_status=facts_status,
        )

    if conflicts:
        reason = "明确结构化字段未通过检查。"
        return DraftAssessment(
            effective_status="needs_user",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=tuple(conflicts),
            lost_facts=tuple(lost_facts),
            can_start=False,
            next_action="$sdlc-discuss 补充已确认结论",
            reason=reason,
        )

    missing_requirement = draft_contract.requirement_missing_items(requirement_body)
    if missing_requirement:
        return DraftAssessment(
            effective_status="discussing",
            open_questions=(),
            missing_requirement_items=tuple(missing_requirement),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action="$sdlc-discuss 继续完善需求草案",
            reason="需求草稿还有必填内容未收口。",
        )

    if not design_body.strip():
        return DraftAssessment(
            effective_status="requirement_ready",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=("技术草稿",),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action="$sdlc-design 技术方案草案",
            reason="需求草稿已完整，技术草稿尚未形成。",
        )

    # 历史技术正文继续只读展示，章节名称和数量不再参与任何新状态判断。
    # 历史流程仍沿用其已保存的事实复核，避免把缺少旧模板标题误判成设计缺失。
    from codex_sdlc.services.draft_service import draft_fact_bundle
    from codex_sdlc.core import fact_gate

    bundle = draft_fact_bundle(draft)
    result = fact_gate.FactGate.verify(bundle, entry_kind="draft")
    if not result.passed:
        facts_status = result.code if result.code in {"needs_user", "needs_review", "stale"} else "facts_missing"
        action = "$sdlc-discuss 生成需求事实" if result.code == "missing_requirement_facts" else "$sdlc-design 生成技术事实并完成独立复核"
        return DraftAssessment(
            effective_status="reviewing",
            open_questions=(),
            missing_requirement_items=(),
            missing_design_items=(),
            conflicts=(),
            lost_facts=(),
            can_start=False,
            next_action=action,
            reason=f"模型事实层尚未通过：{result.message}",
            facts_status=facts_status,
        )

    return DraftAssessment(
        effective_status="start_ready",
        open_questions=(),
        missing_requirement_items=(),
        missing_design_items=(),
        conflicts=(),
        lost_facts=(),
        can_start=True,
        next_action="$sdlc-start",
        reason="需求、技术、测试和模型事实复核均已通过。",
        facts_status="facts_passed",
    )


def allowed_statuses() -> tuple[str, ...]:
    return tuple(ALLOWED_STATUSES)


def status_after_discuss_quality(*, open_questions: list[str], missing_items: list[str], placeholder_items: list[str]) -> str:
    if open_questions:
        return "needs_user"
    if missing_items or placeholder_items:
        return "discussing"
    return "requirement_ready"


def status_after_design_accept(draft: dict[str, Any]) -> str:
    if draft_questions(draft):
        return "needs_user"
    if str(draft.get("requirement_body") or "").strip() and str(draft.get("design_body") or "").strip():
        return "start_ready"
    return "design_ready"


def status_after_question_resolve(draft: dict[str, Any], remaining_questions: list[str]) -> str:
    if remaining_questions:
        return "needs_user"
    if str(draft.get("design_body") or "").strip():
        return "design_ready"
    if str(draft.get("requirement_body") or "").strip():
        return "requirement_ready"
    return "discussing"


def next_action_for_draft(draft: dict[str, Any], *, missing_items: list[str] | None = None) -> dict[str, Any] | None:
    # 旧事件在升级前可能只留下状态，没有正文。展示历史项目时保留原状态，
    # 新草稿一旦有正文或问题就必须改走内容驱动的统一评估。
    has_content = bool(
        uses_structured_requirement_stage(draft)
        or str(draft.get("requirement_body") or "").strip()
        or str(draft.get("design_body") or "").strip()
        or draft_questions(draft)
    )
    assessment = assess_draft(draft) if has_content else None
    status = assessment.effective_status if assessment is not None else draft_status(draft)
    draft_id = str(draft.get("draft_id") or "DRAFT").strip()
    title = str(draft.get("title") or draft_id).strip()
    questions = list(assessment.open_questions) if assessment is not None else draft_questions(draft)
    base_alternatives = ["$sdlc-status", "$sdlc-handoff"]

    if questions:
        return {
            "primary": assessment.next_action if assessment is not None else "$sdlc-discuss 补充已确认结论",
            "reason": assessment.reason if assessment is not None else f"{draft_id} 还有 {len(questions)} 个会影响实现或验收的问题，先让用户回答再继续推进。",
            "alternatives": base_alternatives,
            "draft_questions": questions,
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status == "discussing":
        return {
            "primary": assessment.next_action if assessment is not None else "$sdlc-discuss 继续完善需求草案",
            "reason": assessment.reason if assessment is not None else f"{draft_id} 还在需求讨论阶段，先把范围、规则和验收补齐，再进入技术方案。",
            "alternatives": base_alternatives,
            "draft_context": f"{draft_id} [{status}] {title}",
            "draft_missing_items": (list(assessment.missing_requirement_items) if assessment is not None else []) or missing_items or [],
        }

    if status == "requirement_reviewing":
        return {
            "primary": assessment.next_action if assessment is not None else "$sdlc-status",
            "reason": assessment.reason if assessment is not None else f"{draft_id} 正在等待需求审核。",
            "alternatives": base_alternatives,
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status == "requirement_confirmed":
        return {
            "primary": (
                assessment.next_action
                if assessment is not None
                else "$sdlc-design 技术方案草案"
            ),
            "reason": (
                assessment.reason
                if assessment is not None
                else f"{draft_id} 的需求已经按结构化确认记录确认，可以进入技术方案。"
            ),
            "alternatives": base_alternatives,
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status in {"designing", "design_reviewing"}:
        return {
            "primary": (
                assessment.next_action
                if assessment is not None
                else "$sdlc-status"
            ),
            "reason": (
                assessment.reason
                if assessment is not None
                else f"{draft_id} 正在处理模块化设计。"
            ),
            "alternatives": base_alternatives,
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status == "requirement_ready":
        return {
            "primary": "$sdlc-design 技术方案草案",
            "reason": f"{draft_id} 的需求草稿已经收口，但技术草稿还没准备好，下一步先补技术方案。",
            "alternatives": ["$sdlc-discuss 继续完善需求草案", *base_alternatives],
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status in {"design_ready", "reviewing"}:
        return {
            "primary": assessment.next_action if assessment is not None else "$sdlc-start",
            "reason": assessment.reason if assessment is not None else f"{draft_id} 的技术草稿或模型事实仍需补齐。",
            "alternatives": ["$sdlc-design 继续补齐技术方案", "$sdlc-discuss 补充已确认结论", *base_alternatives],
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    if status == "start_ready":
        return {
            "primary": "$sdlc-start",
            "reason": f"{draft_id} 已经到可建档状态，下一步把当前确认稿正式建档更合适。",
            "alternatives": base_alternatives,
            "draft_context": f"{draft_id} [{status}] {title}",
        }

    return None
