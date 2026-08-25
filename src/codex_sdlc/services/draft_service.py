from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import json
import os
import re
from typing import Any, Mapping

from codex_sdlc.core import draft_artifacts, draft_contract, draft_lifecycle, draft_sections, fact_artifacts, fact_gate, fact_review_trust, fact_schema
from codex_sdlc.core.atomic_import import (
    IMPORT_PACKAGE_SCHEMA,
    AtomicImportPrecommitContext,
    ImportResult,
    atomic_import,
    collect_known_formal_ids,
    load_import_registry,
    recover_atomic_imports,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import FORMAL_ID_PATTERN
from codex_sdlc.core.project import project_lock
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_COVERAGE_SCHEMA,
    REQUIREMENT_SPLIT_SCHEMA,
    validate_requirement_contract,
)
from codex_sdlc.core import review_contract
from codex_sdlc.core.state import append_event, derive_state, draft_body_text, draft_list_markdown, load_events, next_number, now_iso, refresh_materialized_state
from codex_sdlc.core.structured_contract import canonical_sha256, contract_sha256, sha256_file, validate_schema_document


REQUIREMENT_CONFIRMATION_SCHEMA = "requirement-confirmation.v1"


def clean_text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class RequirementImportOutcome:
    """返回原子导入结果和审核阻断项，命令层不再读取投影猜测状态。"""

    result: ImportResult
    review_blockers: tuple[str, ...]


def _add_controlled_formal_id(result: set[str], value: object) -> None:
    """只接收明确编号字段，普通正文即使长得像编号也不会进入集合。"""

    if isinstance(value, str) and FORMAL_ID_PATTERN.fullmatch(value):
        result.add(value)


def _structured_formal_ids(state: Mapping[str, object]) -> set[str]:
    """读取状态中有固定含义的编号字段，不递归扫描标题、决定或 Markdown。"""

    result: set[str] = set()
    requirements = state.get("requirements")
    if isinstance(requirements, dict):
        for requirement in requirements.values():
            if not isinstance(requirement, dict):
                continue
            _add_controlled_formal_id(result, requirement.get("requirement_id"))
            for task in requirement.get("tasks", []):
                if isinstance(task, dict):
                    _add_controlled_formal_id(result, task.get("task_id"))
            for field in ("requirement_points", "acceptance_points", "test_cases"):
                for item in requirement.get(field, []):
                    if isinstance(item, dict):
                        _add_controlled_formal_id(result, item.get("id"))

    drafts = state.get("drafts")
    if isinstance(drafts, dict):
        for draft in drafts.values():
            if not isinstance(draft, dict):
                continue
            receipt = draft.get("requirement_import")
            mapping = receipt.get("mapping") if isinstance(receipt, dict) else None
            if isinstance(mapping, dict):
                for formal_id in mapping.values():
                    _add_controlled_formal_id(result, formal_id)
            # 旧决定文字不参与编号；只有阶段一写入的结构化 DEC 才提供正式编号。
            for decision in draft.get("decision_records", []):
                if isinstance(decision, dict):
                    _add_controlled_formal_id(result, decision.get("decision_id"))
    return result


def _decision_state_fingerprint(draft: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """锁前锁内都比较明确 DEC 编号和内容哈希，普通展示文字不能替代用户决定。"""

    records = draft.get("decision_records")
    if not isinstance(records, list):
        return ()
    result = [
        (str(item.get("decision_id") or ""), str(item.get("decision_sha256") or ""))
        for item in records
        if isinstance(item, dict)
        and str(item.get("decision_id") or "")
        and str(item.get("decision_sha256") or "")
    ]
    return tuple(sorted(result))


def _apply_capture_to_candidate(
    candidate: dict[str, Any], capture: dict[str, Any] | None
) -> None:
    """提交前评估和事件重放使用同一批 CAP/DEC，不让服务返回旧状态。"""

    if not isinstance(capture, dict):
        return
    increment = capture.get("structured_increment")
    decisions = capture.get("decision_records")
    if not isinstance(increment, dict) or not isinstance(decisions, list):
        return
    current_captures = [
        deepcopy(item)
        for item in candidate.get("structured_captures", [])
        if isinstance(item, dict)
    ]
    if not any(item.get("capture_id") == increment.get("capture_id") for item in current_captures):
        current_captures.append(deepcopy(increment))
    current_decisions = [
        deepcopy(item)
        for item in candidate.get("decision_records", [])
        if isinstance(item, dict)
    ]
    for decision in decisions:
        if isinstance(decision, dict) and not any(
            item.get("decision_id") == decision.get("decision_id")
            for item in current_decisions
        ):
            current_decisions.append(deepcopy(decision))
    candidate["structured_captures"] = current_captures
    candidate["decision_records"] = current_decisions
    candidate["_structured_stage_enabled"] = True


def _apply_capture_transition_to_candidate(
    candidate: dict[str, Any], transition: Mapping[str, object]
) -> None:
    """候选状态只追加转换事实，绝不改写初始 CAP 记录。"""

    capture_id = str(transition.get("capture_id") or "")
    captures = [
        item
        for item in candidate.get("structured_captures", [])
        if isinstance(item, dict) and item.get("capture_id") == capture_id
    ]
    if len(captures) != 1:
        raise SdlcError(f"{capture_id} 的初始 CAP 记录不唯一。", exit_code=1)
    capture = captures[0]
    if capture.get("record_sha256") != transition.get("source_record_sha256"):
        raise SdlcError(f"{capture_id} 的原始记录哈希与转换事实不一致。", exit_code=1)
    transitions = [
        deepcopy(item)
        for item in candidate.get("capture_transitions", [])
        if isinstance(item, dict)
    ]
    if any(item.get("capture_id") == capture_id for item in transitions):
        raise SdlcError(f"{capture_id} 已经存在状态转换。", exit_code=1)
    statuses = deepcopy(candidate.get("capture_statuses"))
    if not isinstance(statuses, dict):
        statuses = {
            str(item.get("capture_id") or ""): str(item.get("status") or "")
            for item in candidate.get("structured_captures", [])
            if isinstance(item, dict) and str(item.get("capture_id") or "")
        }
    if statuses.get(capture_id) != transition.get("from_status"):
        raise SdlcError(f"{capture_id} 的前置状态与转换事实不一致。", exit_code=1)
    transitions.append(deepcopy(dict(transition)))
    statuses[capture_id] = str(transition.get("to_status") or "")
    candidate["capture_transitions"] = transitions
    candidate["capture_statuses"] = statuses


def _known_formal_ids(paths, state: Mapping[str, object]) -> set[str]:
    """复用 T-002 公共入口，再补业务状态中有固定含义的编号字段。"""

    public_ids = collect_known_formal_ids(
        load_events(paths), load_import_registry(paths)
    )
    return set(public_ids) | _structured_formal_ids(state)


def _confirmation_body(document: Mapping[str, object]) -> dict[str, object]:
    """确认哈希只排除自身字段，其余审核和输入绑定都不可省略。"""

    return {
        key: deepcopy(value)
        for key, value in document.items()
        if key != "confirmation_sha256"
    }


def validate_requirement_confirmation(
    document: Mapping[str, object],
) -> dict[str, Any]:
    """统一校验确认合同，事件重放和写入入口不能各自放宽字段。"""

    candidate = deepcopy(dict(document))
    validate_schema_document(candidate, schema_name=REQUIREMENT_CONFIRMATION_SCHEMA)
    if candidate["confirmation_sha256"] != canonical_sha256(
        _confirmation_body(candidate)
    ):
        raise SdlcError("需求确认记录的 confirmation_sha256 与内容不一致。", exit_code=1)
    return candidate


def _load_requirement_review_snapshot(
    paths,
    request: Mapping[str, object],
) -> tuple[str, dict[str, Any]]:
    """从审核请求登记的受控路径读取唯一需求审核快照，不按文件名猜版本。"""

    matches: list[tuple[str, dict[str, Any]]] = []
    for relative_path in request.get("input_paths", []):
        if not isinstance(relative_path, str):
            continue
        target = paths.root / relative_path
        if target.is_symlink() or not target.is_file():
            continue
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("schema") == "sdlc.requirement-review-input.v1"
            and value.get("stage") == "requirement_split"
            and value.get("owner_id") == request.get("owner_id")
        ):
            matches.append((relative_path, value))
    if len(matches) != 1:
        raise SdlcError("当前审核请求没有唯一、可读取的需求审核输入快照。", exit_code=1)
    relative_path, snapshot = matches[0]
    if snapshot.get("snapshot_sha256") != canonical_sha256(
        {key: deepcopy(value) for key, value in snapshot.items() if key != "snapshot_sha256"}
    ):
        raise SdlcError("需求审核输入快照哈希不一致。", exit_code=1)
    return relative_path, snapshot


def _current_requirement_confirmation_binding(
    paths,
    *,
    draft: Mapping[str, object],
    review_id: str | None = None,
    review_state: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """从当前有效审核登记和真实文件生成确认绑定，调用方不能传入任何哈希。"""

    from codex_sdlc.services.review_service import requirement_review_status

    clean_draft_id = clean_text(draft.get("draft_id")).upper()
    status = (
        deepcopy(dict(review_state))
        if isinstance(review_state, Mapping)
        else requirement_review_status(
            paths,
            draft_id=clean_draft_id,
            review_id=review_id,
        )
    )
    if status.get("status") == "rejected":
        raise SdlcError(str(status.get("rejection_reason") or "需求审核登记不可用。"), exit_code=1)
    reviews = status.get("reviews")
    current = [
        item
        for item in reviews
        if isinstance(item, dict) and item.get("is_current") is True
    ] if isinstance(reviews, list) else []
    if len(current) != 1:
        raise SdlcError("当前 DRAFT 没有唯一的当前需求审核。", exit_code=1)
    review = current[0]
    if review_id and review.get("review_id") != clean_text(review_id).upper():
        raise SdlcError("指定的审核轮次已经不是当前需求审核。", exit_code=1)
    required = {
        "stage": "requirement_split",
        "owner_id": clean_draft_id,
        "request_status": "completed",
        "effective_status": "passed",
        "is_current": True,
        "can_advance": True,
    }
    for field, expected in required.items():
        if review.get(field) != expected:
            raise SdlcError(
                f"当前需求审核不能确认：{field}={review.get(field)!r}。",
                exit_code=1,
            )

    registry = fact_review_trust.load_review_registry(paths)
    review_key = str(review["review_id"])
    request_record = registry["requests"].get(review_key)
    if not isinstance(request_record, dict):
        raise SdlcError("当前需求审核缺少可信请求登记。", exit_code=1)
    registration_id = request_record.get("result_registration_id")
    registration = registry["registrations"].get(registration_id)
    if not isinstance(registration, dict):
        raise SdlcError("当前需求审核缺少可信结果登记。", exit_code=1)
    request = request_record["request"]
    if request.get("stage") != "requirement_split" or request.get("owner_id") != clean_draft_id:
        raise SdlcError("当前审核登记不属于该 DRAFT 的需求拆分阶段。", exit_code=1)
    actual_input_hashes = review_contract.controlled_input_hashes(
        paths, request["input_paths"]
    )
    if actual_input_hashes != request["input_hashes"]:
        raise SdlcError("当前审核输入文件哈希已经变化。", exit_code=1)

    snapshot_path, snapshot = _load_requirement_review_snapshot(paths, request)
    package = snapshot.get("requirement_package")
    if not isinstance(package, dict):
        raise SdlcError("需求审核输入快照缺少结构化需求包。", exit_code=1)
    split = draft.get("requirement_split")
    coverage = draft.get("requirement_coverage")
    if not isinstance(split, dict) or not isinstance(coverage, dict):
        raise SdlcError("当前 DRAFT 缺少结构化需求拆分或覆盖合同。", exit_code=1)
    split_sha256 = contract_sha256(split, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    coverage_sha256 = contract_sha256(coverage, schema_name=REQUIREMENT_COVERAGE_SCHEMA)
    if package.get("split_contract_sha256") != split_sha256:
        raise SdlcError("当前 requirement-split 与审核输入不一致。", exit_code=1)
    if package.get("coverage_contract_sha256") != coverage_sha256:
        raise SdlcError("当前 requirement-coverage 与审核输入不一致。", exit_code=1)

    return {
        "draft_id": clean_draft_id,
        "stage": "requirement_split",
        "review_id": review_key,
        "review_request_sha256": request_record["request_sha256"],
        "review_registration_id": str(registration_id),
        "review_registration_sha256": canonical_sha256(registration),
        "review_result_sha256": registration["result_sha256"],
        "review_input_path": snapshot_path,
        "review_input_sha256": sha256_file(paths.root / snapshot_path),
        "review_input_snapshot_sha256": snapshot["snapshot_sha256"],
        "requirement_split_sha256": split_sha256,
        "requirement_coverage_sha256": coverage_sha256,
        "material_snapshot_sha256": canonical_sha256(snapshot.get("applicable_materials")),
        "decision_snapshot_sha256": canonical_sha256(snapshot.get("effective_decisions")),
        "dependency_snapshot_sha256": canonical_sha256(
            request_record["dependency_snapshot"]
        ),
    }


def requirement_confirmation_status(
    paths,
    *,
    draft_id: str,
    draft: Mapping[str, object] | None = None,
    review_id: str | None = None,
    review_state: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """只读判断最新确认是否仍绑定当前审核和当前完整输入。"""

    clean_id = clean_text(draft_id).upper()
    current_draft = deepcopy(dict(draft)) if isinstance(draft, Mapping) else None
    if current_draft is None:
        state = derive_state(paths)
        value = state.get("drafts", {}).get(clean_id)
        if not isinstance(value, dict):
            return {
                "status": "rejected",
                "can_advance": False,
                "rejection_reason": f"没有找到 DRAFT `{clean_id}`。",
                "current_confirmation": None,
                "stale_reasons": [],
            }
        current_draft = value
    confirmations = [
        validate_requirement_confirmation(item)
        for item in current_draft.get("requirement_confirmations", [])
        if isinstance(item, dict)
    ]
    latest = confirmations[-1] if confirmations else None
    try:
        binding = _current_requirement_confirmation_binding(
            paths,
            draft=current_draft,
            review_id=review_id,
            review_state=review_state,
        )
    except SdlcError as exc:
        return {
            "status": "stale" if latest else "empty",
            "can_advance": False,
            "rejection_reason": exc.message,
            "current_confirmation": deepcopy(latest),
            "stale_reasons": [exc.message] if latest else [],
        }
    if latest is None:
        return {
            "status": "empty",
            "can_advance": False,
            "rejection_reason": "",
            "current_confirmation": None,
            "stale_reasons": [],
        }
    stale_reasons = [
        f"{field} 已变化"
        for field, value in binding.items()
        if latest.get(field) != value
    ]
    return {
        "status": "stale" if stale_reasons else "ready",
        "can_advance": not stale_reasons,
        "rejection_reason": "",
        "current_confirmation": deepcopy(latest),
        "stale_reasons": stale_reasons,
    }


def _current_requirement_material_hashes(
    draft: dict[str, Any], declared_hashes: object
) -> dict[str, str]:
    """按明确资料编号核对活动文件，不把 URL 哈希冒充来源内容哈希。"""

    if not isinstance(declared_hashes, dict):
        raise SdlcError("input_material_hashes 必须是资料编号到 SHA-256 的对象。", exit_code=1)
    active_files = {
        str(item.get("material_id") or ""): item
        for item in draft.get("materials", [])
        if isinstance(item, dict)
        and item.get("status") != "archived"
        and item.get("source_kind") == "file"
        and str(item.get("material_id") or "").strip()
    }
    required_ids = {
        material_id
        for material_id, item in active_files.items()
        if item.get("type") == "requirement" or "requirement" in item.get("roles", [])
    }
    missing_required = sorted(required_ids - set(declared_hashes))
    if missing_required:
        raise SdlcError(
            f"input_material_hashes 缺少当前需求资料：{', '.join(missing_required)}。",
            exit_code=1,
        )
    current: dict[str, str] = {}
    for material_id in declared_hashes:
        material = active_files.get(str(material_id))
        if material is None:
            raise SdlcError(
                f"input_material_hashes 引用的资料不是当前活动文件：{material_id}。",
                exit_code=1,
            )
        digest = str(material.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SdlcError(f"资料 {material_id} 没有可核对的完整 SHA-256。", exit_code=1)
        current[str(material_id)] = digest
    return current


def _requirement_import_package(
    draft_id: str,
    split_document: dict[str, object],
    coverage_document: dict[str, object],
    validation,
) -> dict[str, object]:
    """把阶段一结果装入 T-002 原子包，正式编号和引用重写仍由公共导入器完成。"""

    content_key = canonical_sha256(
        {
            "split_sha256": validation.split_sha256,
            "coverage_sha256": canonical_sha256(coverage_document),
        }
    )
    package: dict[str, object] = {
        "schema": IMPORT_PACKAGE_SCHEMA,
        "package_key": f"draft-requirements:{draft_id}:{content_key}",
        "package_sha256": "0" * 64,
        "destination": (
            f".codex-sdlc/drafts/{draft_id}/需求/requirements-{content_key}"
        ),
        "objects": [
            {
                "client_key": item.client_key,
                "id_prefix": item.id_prefix,
                "depends_on": [],
            }
            for item in validation.allocation_objects
        ],
        "files": [
            {
                "relative_path": "requirement-split.v1.json",
                "content": deepcopy(split_document),
            },
            {
                "relative_path": "requirement-coverage.v1.json",
                "content": deepcopy(coverage_document),
            },
        ],
    }
    package["package_sha256"] = contract_sha256(
        package, schema_name=IMPORT_PACKAGE_SCHEMA
    )
    return package


def _finalize_requirement_files(
    _mapping: Mapping[str, str], files: Mapping[str, object]
) -> Mapping[str, object]:
    """正式引用重写后重算拆分哈希，保证最终双文件互相指向同一内容。"""

    finalized = deepcopy(dict(files))
    split = finalized.get("requirement-split.v1.json")
    coverage = finalized.get("requirement-coverage.v1.json")
    if not isinstance(split, dict) or not isinstance(coverage, dict):
        raise SdlcError("需求导入包缺少可最终化的拆分或覆盖文件。")
    coverage["requirement_split_sha256"] = contract_sha256(
        split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    validate_schema_document(split, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    validate_schema_document(coverage, schema_name=REQUIREMENT_COVERAGE_SCHEMA)
    return finalized


def _requirements_process_barrier(stage: str) -> None:
    """使用继承的管道控制真实进程交错，只在显式测试环境中启用。"""

    if os.environ.get("CODEX_SDLC_REQUIREMENTS_BARRIER_STAGE", "").strip() != stage:
        return
    try:
        ready_fd = int(os.environ["CODEX_SDLC_REQUIREMENTS_READY_FD"])
        continue_fd = int(os.environ["CODEX_SDLC_REQUIREMENTS_CONTINUE_FD"])
    except (KeyError, ValueError) as exc:
        raise SdlcError("需求导入进程屏障缺少有效管道。") from exc
    os.write(ready_fd, b"1")
    if os.read(continue_fd, 1) != b"1":
        raise SdlcError("需求导入进程屏障被意外关闭。")


def _requirements_interruption_hook(stage: str) -> None:
    """真实进程中断测试沿用 T-002 的正式提交点，不另造模拟事务。"""

    if os.environ.get("CODEX_SDLC_REQUIREMENTS_INTERRUPT_AT", "").strip() == stage:
        os._exit(73)


class DraftMutationService:
    """DRAFT 唯一写入入口。

    服务先在内存中生成候选草稿，再执行保真、结构和一致性合同。合同失败时
    不会追加事件，也不会刷新任何物化文件。成功时只写一条完整业务事件。
    """

    def __init__(self, paths, *, source: str) -> None:
        self.paths = paths
        self.source = source

    def _state(self) -> dict[str, Any]:
        return derive_state(self.paths)

    def _draft(self, draft_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        current_state = state or self._state()
        drafts = current_state.get("drafts", {})
        clean_id = clean_text(draft_id).upper()
        draft = drafts.get(clean_id) if isinstance(drafts, dict) else None
        if not isinstance(draft, dict):
            raise SdlcError(f"没有找到 DRAFT `{clean_id}`。", exit_code=1)
        return draft

    @staticmethod
    def _ensure_editable(draft: dict[str, Any]) -> None:
        if draft_lifecycle.is_started_draft(draft):
            raise SdlcError(
                f"{draft.get('draft_id') or 'DRAFT'} 已经正式建档，不能再修改、回退或补写草稿内容。",
                exit_code=1,
            )

    def _commit(
        self,
        *,
        summary: str,
        payload: dict[str, Any],
        projection_draft: dict[str, Any],
        event_type: str = "draft_mutated",
    ) -> dict[str, Any]:
        """目录和投影都成功后才保留事件，失败时恢复提交前的 DRAFT 文件。"""

        draft_id = clean_text(payload.get("draft_id")).upper()
        layout = draft_artifacts.ensure_draft_layout(self.paths, draft_id)
        managed_snapshot = draft_artifacts.snapshot_managed_files(layout.draft_dir)
        existed = self.paths.events_file.exists()
        original = self.paths.events_file.read_bytes() if existed else b""
        try:
            draft_artifacts.preflight_artifact_updates(
                layout.draft_dir,
                projection_draft,
                payload.get("artifact_updates"),
            )
            event = append_event(
                self.paths,
                event_type=event_type,
                source=self.source,
                summary=summary,
                payload=payload,
            )
            refresh_materialized_state(self.paths)
            return event
        except Exception:
            if existed:
                self.paths.events_file.write_bytes(original)
            else:
                self.paths.events_file.unlink(missing_ok=True)
            if layout.created_root:
                draft_artifacts.remove_new_draft_layout(layout)
            else:
                draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            # 其它全局投影仍按原事件重建；即使重建过程再次失败，DRAFT 受管文件也会回到提交前。
            if self.paths.events_file.exists():
                try:
                    refresh_materialized_state(self.paths)
                except Exception:
                    if not layout.created_root:
                        draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            raise

    def create(
        self,
        title: str,
        *,
        initial_changes: dict[str, Any] | None = None,
        capture: dict[str, Any] | None = None,
    ) -> tuple[str, draft_lifecycle.DraftAssessment]:
        clean_title = clean_text(title)
        if not clean_title:
            raise SdlcError("DRAFT 标题不能为空。", exit_code=1)
        state = self._state()
        drafts = state.get("drafts", {})
        draft_id = next_number(list(drafts.keys()) if isinstance(drafts, dict) else [], "DRAFT")
        candidate = {
            "draft_id": draft_id,
            "title": clean_title,
            "status": "discussing",
            "requirement_body": "",
            "design_body": "",
            "questions": [],
            "decisions": [],
            "review_items": [],
        }
        for key, value in (initial_changes or {}).items():
            candidate[key] = deepcopy(value)
        _apply_capture_to_candidate(candidate, capture)
        assessment = draft_lifecycle.assess_draft(candidate)
        if assessment.conflicts or assessment.lost_facts:
            raise SdlcError(assessment.reason, exit_code=1)
        payload = {
            "draft_id": draft_id,
            "operation": "create",
            "changes": candidate,
            "assessment": asdict(assessment),
            "artifact_updates": draft_artifacts.build_projection_updates(
                candidate,
                draft_artifacts.BUILTIN_PROJECTION_SPECS,
                producer_task_id=self.source,
            ),
        }
        if capture is not None:
            payload["capture"] = capture
        self._commit(summary=f"创建 {draft_id}", payload=payload, projection_draft=candidate)
        return draft_id, assessment

    def mutate(
        self,
        draft_id: str,
        *,
        operation: str,
        changes: dict[str, Any],
        allow_conflicts: bool = False,
        capture: dict[str, Any] | None = None,
    ) -> draft_lifecycle.DraftAssessment:
        state = self._state()
        current = self._draft(draft_id, state)
        self._ensure_editable(current)
        candidate = deepcopy(current)
        for key, value in changes.items():
            candidate[key] = deepcopy(value)
        _apply_capture_to_candidate(candidate, capture)

        assessment = draft_lifecycle.assess_draft(candidate, previous_draft=current)
        if assessment.lost_facts:
            lines = ["DRAFT 更新被拦住：草稿改写会丢失已经确认的内容："]
            lines.extend(f"- {fact.field}：{fact.value}" for fact in assessment.lost_facts)
            raise SdlcError("\n".join(lines), exit_code=1)
        # 冲突草稿可以作为讨论中的中间状态保存，但评估会把它固定为
        # needs_user，start 也会阻断。真正不可恢复的有损覆盖仍在上面拒绝。

        clean_id = clean_text(draft_id).upper()
        payload = {
            "draft_id": clean_id,
            "operation": operation,
            "changes": changes,
            "assessment": asdict(assessment),
            "artifact_updates": draft_artifacts.build_projection_updates(
                candidate,
                changes.keys(),
                producer_task_id=self.source,
            ),
        }
        if capture is not None:
            payload["capture"] = capture
        self._commit(summary=f"{operation} {clean_id}", payload=payload, projection_draft=candidate)
        return assessment

    def record_capture_transition(
        self,
        draft_id: str,
        transition: Mapping[str, object],
        transition_submission: Mapping[str, object],
    ) -> draft_lifecycle.DraftAssessment:
        """在项目锁内追加独立 CAP 转换事件，并用同一候选状态刷新全部投影。"""

        state = self._state()
        current = self._draft(draft_id, state)
        self._ensure_editable(current)
        candidate = deepcopy(current)
        _apply_capture_transition_to_candidate(candidate, transition)
        assessment = draft_lifecycle.assess_draft(candidate, previous_draft=current)
        clean_id = clean_text(draft_id).upper()
        payload = {
            "draft_id": clean_id,
            "transition": deepcopy(dict(transition)),
            # 原始提交独立于派生转换记录保存，重放时可识别协调重算记录内哈希的改写。
            "transition_submission": deepcopy(dict(transition_submission)),
            "assessment": asdict(assessment),
            "artifact_updates": [],
        }
        self._commit(
            summary=f"转换结构化 CAP 状态 {transition.get('capture_id')}",
            payload=payload,
            projection_draft=candidate,
            event_type="structured_capture_transitioned",
        )
        return assessment

    def confirm_requirement(
        self,
        draft_id: str,
        *,
        review_id: str,
        confirmed_at: str | None = None,
    ) -> dict[str, Any]:
        """在项目锁内绑定当前有效审核；调用方只能选择审核轮次，不能提交自报哈希。"""

        clean_id = clean_text(draft_id).upper()
        clean_review_id = clean_text(review_id).upper()
        if not clean_review_id:
            raise SdlcError("需求确认必须明确当前审核轮次。", exit_code=1)
        with project_lock(self.paths):
            state = self._state()
            current = self._draft(clean_id, state)
            self._ensure_editable(current)
            assessment = draft_lifecycle.assess_draft(current)
            base_blockers = [
                item
                for item in assessment.blockers
                if item.code
                not in {
                    "requirement_review_pending",
                    "requirement_review_invalid",
                    "requirement_confirmation_pending",
                    "requirement_confirmation_stale",
                }
            ]
            only_design_blockers = bool(base_blockers) and all(
                item.code.startswith("design_")
                or item.code.startswith("integrated_design_")
                for item in base_blockers
            )
            if base_blockers and not only_design_blockers:
                raise SdlcError(
                    "当前 DRAFT 仍有需求阻断项，不能确认："
                    + "；".join(
                        f"{item.code}:{item.source_id}:{item.status}"
                        for item in base_blockers
                    )
                    + "。",
                    exit_code=1,
                )
            from codex_sdlc.services.review_service import requirement_review_status

            review_state = requirement_review_status(
                self.paths,
                draft_id=clean_id,
                review_id=clean_review_id,
            )
            binding = _current_requirement_confirmation_binding(
                self.paths,
                draft=current,
                review_id=clean_review_id,
                review_state=review_state,
            )
            existing = [
                validate_requirement_confirmation(item)
                for item in current.get("requirement_confirmations", [])
                if isinstance(item, dict)
            ]
            if existing and all(
                existing[-1].get(field) == value for field, value in binding.items()
            ):
                return {
                    "action": "idempotent",
                    "confirmation": deepcopy(existing[-1]),
                    "status": "requirement_confirmed",
                }

            if base_blockers:
                # 已进入设计阶段后，幂等确认可以先返回同一记录；只有要写新确认时，
                # 设计阶段后来出现的阻断项才会阻止继续写入。
                raise SdlcError(
                    "当前 DRAFT 仍有需求阻断项，不能确认："
                    + "；".join(
                        f"{item.code}:{item.source_id}:{item.status}"
                        for item in base_blockers
                    )
                    + "。",
                    exit_code=1,
                )

            confirmation_id = next_number(
                [str(item["confirmation_id"]) for item in existing],
                "RCF",
            )
            confirmation = {
                "schema_version": REQUIREMENT_CONFIRMATION_SCHEMA,
                "confirmation_id": confirmation_id,
                **binding,
                "confirmed_at": clean_text(confirmed_at) or now_iso(),
            }
            confirmation["confirmation_sha256"] = canonical_sha256(confirmation)
            confirmation = validate_requirement_confirmation(confirmation)

            candidate = deepcopy(current)
            candidate.setdefault("requirement_confirmations", [])
            candidate["requirement_confirmations"].append(deepcopy(confirmation))
            candidate["_requirement_review_state"] = deepcopy(review_state)
            candidate["_requirement_confirmation_state"] = {
                "status": "ready",
                "can_advance": True,
                "rejection_reason": "",
                "current_confirmation": deepcopy(confirmation),
                "stale_reasons": [],
            }
            final_assessment = draft_lifecycle.assess_draft(
                candidate,
                previous_draft=current,
            )
            payload = {
                "draft_id": clean_id,
                "confirmation": deepcopy(confirmation),
                "assessment": asdict(final_assessment),
            }
            self._commit(
                summary=f"确认 {clean_id} 当前需求版本",
                payload=payload,
                projection_draft=candidate,
                event_type="draft_requirement_confirmed",
            )
            return {
                "action": "created",
                "confirmation": deepcopy(confirmation),
                "status": final_assessment.effective_status,
            }

    def update_requirement(self, draft_id: str, body: str, *, summary: str = "") -> draft_lifecycle.DraftAssessment:
        changes: dict[str, Any] = {"requirement_body": body}
        if summary:
            changes["requirement_summary"] = summary
        return self.mutate(draft_id, operation="更新需求草稿", changes=changes)

    def import_requirements(
        self,
        draft_id: str,
        split_document: dict[str, object],
        coverage_document: dict[str, object],
    ) -> RequirementImportOutcome:
        """校验双文件后交给统一原子导入，并从事件重建固定阅读投影。"""

        clean_id = clean_text(draft_id).upper()
        source_split = deepcopy(split_document)
        source_coverage = deepcopy(coverage_document)
        recovered = recover_atomic_imports(self.paths)
        if recovered:
            # 改名后中断的事务已经是完整成功，先把事件对应投影补齐再处理当前重试。
            with project_lock(self.paths):
                refresh_materialized_state(self.paths)
        state = self._state()
        draft = self._draft(clean_id, state)
        self._ensure_editable(draft)
        requirements_dir = self.paths.draft_requirements_dir(clean_id)
        if not requirements_dir.is_dir() or requirements_dir.is_symlink():
            raise SdlcError(f"{clean_id} 缺少安全的需求目录，不能导入结构化需求。", exit_code=1)
        current_hashes = _current_requirement_material_hashes(
            draft, source_split.get("input_material_hashes")
        )
        validation = validate_requirement_contract(
            source_split,
            source_coverage,
            project_root=self.paths.root,
            current_material_hashes=current_hashes,
            expected_draft_id=clean_id,
            expected_producer_run_id=draft_artifacts.producer_run_id(self.source),
            known_formal_ids=_known_formal_ids(self.paths, state),
        )
        package = _requirement_import_package(
            clean_id, source_split, source_coverage, validation
        )
        initial_decisions = _decision_state_fingerprint(draft)

        def validate_locked_precommit(
            locked_paths, context: AtomicImportPrecommitContext
        ) -> set[str]:
            """在 T-002 已持有的同一项目锁内重新核对全部业务前提。"""

            _requirements_process_barrier("inside_locked_precommit")
            latest_state = derive_state(locked_paths)
            latest_draft = self._draft(clean_id, latest_state)
            self._ensure_editable(latest_draft)
            latest_requirements_dir = locked_paths.draft_requirements_dir(clean_id)
            if (
                not latest_requirements_dir.is_dir()
                or latest_requirements_dir.is_symlink()
            ):
                raise SdlcError(
                    f"{clean_id} 缺少安全的需求目录，不能导入结构化需求。",
                    exit_code=1,
                )
            latest_decisions = _decision_state_fingerprint(latest_draft)
            if latest_decisions != initial_decisions:
                raise SdlcError("DRAFT 用户决定已变化，需求包必须按最新决定重新生成。")
            latest_hashes = _current_requirement_material_hashes(
                latest_draft, source_split.get("input_material_hashes")
            )
            structured_ids = _structured_formal_ids(latest_state)
            validate_requirement_contract(
                source_split,
                source_coverage,
                project_root=locked_paths.root,
                current_material_hashes=latest_hashes,
                expected_draft_id=clean_id,
                expected_producer_run_id=draft_artifacts.producer_run_id(self.source),
                known_formal_ids=set(context.known_formal_ids) | structured_ids,
            )
            return structured_ids

        # 真实进程测试会在锁外初检和 T-002 锁内终检之间修改资料，验证旧包被拒绝。
        _requirements_process_barrier("after_prevalidation")
        result = atomic_import(
            self.paths,
            package,
            interruption_hook=_requirements_interruption_hook,
            locked_precommit_validator=validate_locked_precommit,
            files_finalizer=_finalize_requirement_files,
        )
        # 较早进程可以在这里暂停，让较晚包先提交。恢复后必须在锁内重新派生最新
        # 事件并写投影，不能继续使用当前调用在提交前或提交后的旧状态快照。
        _requirements_process_barrier("before_projection_lock")
        with project_lock(self.paths):
            _requirements_process_barrier("inside_projection_lock")
            refreshed = refresh_materialized_state(self.paths)
            refreshed_draft = refreshed.get("drafts", {}).get(clean_id)
            requirement_import = (
                refreshed_draft.get("requirement_import")
                if isinstance(refreshed_draft, dict)
                else None
            )
            if not isinstance(requirement_import, dict):
                raise SdlcError("需求原子包已经提交，但最新事件投影没有重建成功。", exit_code=1)
        return RequirementImportOutcome(
            result=result,
            review_blockers=validation.review_blockers,
        )

    def update_design(self, draft_id: str, body: str, *, summary: str = "") -> draft_lifecycle.DraftAssessment:
        changes: dict[str, Any] = {"design_body": body}
        if summary:
            changes["design_summary"] = summary
        # 技术草稿允许先保存不完整或有冲突的讨论稿，但评估结果必须立即降为
        # needs_user，且 start 会继续阻断；已经确认的技术事实仍由丢失检查保护。
        return self.mutate(draft_id, operation="更新技术草稿", changes=changes, allow_conflicts=True)

    def replace_questions(self, draft_id: str, questions: list[str]) -> draft_lifecycle.DraftAssessment:
        return self.mutate(draft_id, operation="更新待确认问题", changes={"questions": questions}, allow_conflicts=True)

    def add_question(self, draft_id: str, question: str) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        questions = [clean_text(item) for item in draft.get("questions", []) if clean_text(item)]
        clean_question = clean_text(question)
        if not clean_question:
            raise SdlcError("问题内容不能为空。", exit_code=1)
        if clean_question not in questions:
            questions.append(clean_question)
        return self.replace_questions(draft_id, questions)

    def record_review(self, draft_id: str, content: str) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        items = [clean_text(item) for item in draft.get("review_items", []) if clean_text(item)]
        clean_content = clean_text(content)
        if clean_content and clean_content not in items:
            items.append(clean_content)
        return self.mutate(draft_id, operation="记录审查结果", changes={"review_items": items}, allow_conflicts=True)

    def generate_source_index(self, draft_id: str) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        source = draft_source_projection(draft)
        index = fact_artifacts.build_source_index(source, source_kind="draft", draft_id=draft_id)
        return self.mutate(
            draft_id,
            operation="生成模型事实来源索引",
            changes={"fact_source_projection": source, "fact_source_index": index},
            allow_conflicts=True,
        )

    def write_fact_artifact(self, draft_id: str, kind: str, document: dict[str, Any]) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        owner = "requirement" if kind == "requirement" else "design"
        issues = fact_schema.fact_document_issues(document, owner=owner)
        if issues:
            raise SdlcError("模型事实文件未通过检查：\n" + "\n".join(f"- {item}" for item in issues), exit_code=1)
        if document.get("artifact_sha256") != fact_artifacts.artifact_sha256(document):
            raise SdlcError("模型事实文件的 artifact hash 与内容不一致。", exit_code=1)
        if document.get("semantic_sha256") != fact_artifacts.semantic_sha256(document["semantic"]):
            raise SdlcError("模型事实文件的 semantic digest 与语义内容不一致。", exit_code=1)
        if document.get("draft_id") != draft_id:
            raise SdlcError("模型事实文件的 draft_id 与当前 DRAFT 不一致。", exit_code=1)
        current = current_draft_context(draft)
        if document.get("context_targets") != current:
            raise SdlcError("模型事实文件对应的草稿内容已经变化，请重新生成来源索引并提取事实。", exit_code=1)
        index = draft["fact_source_index"]
        reference_issue = fact_gate.validate_fact_artifact_references(
            document,
            index,
            owner=owner,
            entry_kind="draft",
            origin_index=index,
        )
        if reference_issue:
            raise SdlcError(f"模型事实文件的原文锚点或覆盖无效：{reference_issue[1]}", exit_code=1)
        run = fact_review_trust.record_fact_run(
            self.paths,
            draft_id=draft_id,
            owner=owner,
            artifact_sha256=fact_artifacts.artifact_sha256(document),
        )
        current_runs = deepcopy(draft.get("fact_run_ids")) if isinstance(draft.get("fact_run_ids"), dict) else {}
        current_runs[owner] = run["record_id"]
        # 任一 facts 被替换后，原复核请求和回执都立即作废，不能沿用旧任务证明。
        return self.mutate(
            draft_id,
            operation=f"写入{owner}模型事实",
            changes={f"{owner}_facts": deepcopy(document), "fact_run_ids": current_runs, "review_request_id": "", "review_receipt": None},
            allow_conflicts=True,
        )

    def create_review_request(self, draft_id: str) -> tuple[draft_lifecycle.DraftAssessment, str]:
        draft = self._draft(draft_id)
        requirement = draft.get("requirement_facts")
        design = draft.get("design_facts")
        runs = draft.get("fact_run_ids")
        if not all(isinstance(item, dict) for item in (requirement, design, runs)):
            raise SdlcError("请先在事实产出任务写入需求 facts 和技术 facts。", exit_code=1)
        target = fact_review_trust.review_target_sha256(requirement, design, requirement["context_targets"])
        request = fact_review_trust.create_review_request(
            self.paths,
            draft_id=draft_id,
            target_sha256=target,
            fact_run_ids=[str(runs.get("requirement") or ""), str(runs.get("design") or "")],
        )
        assessment = self.mutate(
            draft_id,
            operation="创建独立复核请求",
            changes={"review_request_id": request["request_id"], "review_receipt": None},
            allow_conflicts=True,
        )
        return assessment, str(request["request_id"])

    def write_model_review(self, draft_id: str, document: dict[str, Any]) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        issues = fact_schema.review_document_issues(document)
        if issues:
            raise SdlcError("模型复核文件未通过检查：\n" + "\n".join(f"- {item}" for item in issues), exit_code=1)
        if document.get("artifact_sha256") != fact_artifacts.artifact_sha256(document):
            raise SdlcError("模型复核文件的 artifact hash 与内容不一致。", exit_code=1)
        bundle = draft_fact_bundle(draft, review=document, review_receipt=None)
        freshness = fact_gate.review_freshness(bundle)
        if freshness.status == "stale":
            raise SdlcError(f"模型复核对应的输入已经变化：{freshness.reason}", exit_code=1)
        receipt = None
        if document.get("status") == "passed":
            request_id = str(draft.get("review_request_id") or "")
            if not request_id:
                raise SdlcError("缺少一次性独立复核请求，请先执行 draft review-request。", exit_code=1)
            target = fact_review_trust.review_target_sha256(
                bundle["requirement"], bundle["design"], document["targets"]
            )
            receipt = fact_review_trust.submit_review(
                self.paths,
                request_id=request_id,
                target_sha256=target,
                review_sha256=fact_artifacts.artifact_sha256(document),
            )
            bundle["review_receipt"] = {**receipt, "trusted": True}
            result = fact_gate.FactGate.verify(bundle, entry_kind="draft")
            if not result.passed:
                raise SdlcError(f"模型复核不能标记为 passed：{result.message}", exit_code=1)
        return self.mutate(
            draft_id,
            operation="写入独立模型复核",
            # 事件只保存回执编号。签名、范围和 trusted 结论必须在每次读取时从受管登记表重算。
            changes={
                "model_review": deepcopy(document),
                "review_receipt": ({"receipt_id": str(receipt["receipt_id"])} if receipt else None),
            },
            allow_conflicts=True,
        )

    def record_decision(
        self,
        draft_id: str,
        decision: str,
        *,
        remaining_questions: list[str] | None = None,
        resolved_questions: list[str] | None = None,
    ) -> draft_lifecycle.DraftAssessment:
        draft = self._draft(draft_id)
        decisions = [clean_text(item) for item in draft.get("decisions", []) if clean_text(item)]
        clean_decision = clean_text(decision)
        if not clean_decision:
            raise SdlcError("决定内容不能为空。", exit_code=1)
        if clean_decision not in decisions:
            decisions.append(clean_decision)
        changes: dict[str, Any] = {"decisions": decisions}
        if remaining_questions is not None:
            changes["questions"] = remaining_questions
            changes["resolved_questions"] = resolved_questions or []
        return self.mutate(draft_id, operation="记录用户决定", changes=changes, allow_conflicts=True)


def evaluate_draft(draft: dict[str, Any]) -> draft_lifecycle.DraftAssessment:
    """给 start、status、next 和 doctor-deep 复用的唯一评估入口。"""

    return draft_lifecycle.assess_draft(draft)


def draft_source_projection(draft: dict[str, Any]) -> dict[str, Any]:
    """四份输入与实际 DRAFT 文件字节一致，来源锚点可以由 CLI 直接复核。"""

    return fact_artifacts.build_draft_source_projection(
        draft_body_text(draft, "requirement_body", f"{draft['draft_id']} 需求草稿"),
        draft_body_text(draft, "design_body", f"{draft['draft_id']} 技术草稿"),
        draft_list_markdown(draft, "待确认问题", "questions"),
        draft_list_markdown(draft, "已确认决定", "decisions"),
    )


def current_draft_context(draft: dict[str, Any]) -> dict[str, str]:
    index = draft.get("fact_source_index")
    if not isinstance(index, dict):
        raise SdlcError("当前 DRAFT 缺少 source-index.json，请先生成模型事实来源索引。", exit_code=1)
    source = draft_source_projection(draft)
    rebuilt = fact_artifacts.build_source_index(source, source_kind="draft", draft_id=str(draft.get("draft_id") or ""))
    if rebuilt != index:
        raise SdlcError("当前 DRAFT 内容已经变化，请重新生成 source-index.json。", exit_code=1)
    return fact_artifacts.build_context_targets(source, index)


def resolve_draft_review_receipt(paths, draft: dict[str, Any], *, review: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """从受管登记表恢复 DRAFT 回执，不能复用事件中可编辑的 trusted 布尔值。"""

    requirement = draft.get("requirement_facts")
    design = draft.get("design_facts")
    selected_review = review if review is not None else draft.get("model_review")
    event_receipt = draft.get("review_receipt")
    if not all(isinstance(item, dict) for item in (requirement, design, selected_review, event_receipt)):
        return None
    receipt_id = str(event_receipt.get("receipt_id") or "")
    draft_id = str(draft.get("draft_id") or "").strip().upper()
    if not receipt_id or not draft_id:
        return None
    target = fact_review_trust.review_target_sha256(requirement, design, selected_review.get("targets", {}))
    return fact_review_trust.trusted_receipt(
        paths,
        receipt_id=receipt_id,
        target_sha256=target,
        review_sha256=fact_artifacts.artifact_sha256(selected_review),
        draft_id=draft_id,
        entry_scope="draft",
    )


def draft_fact_bundle(
    draft: dict[str, Any],
    *,
    review: dict[str, Any] | None = None,
    review_receipt: dict[str, Any] | None | object = ...,
) -> dict[str, Any]:
    source = draft_source_projection(draft)
    index = draft.get("fact_source_index")
    requirement = draft.get("requirement_facts")
    design = draft.get("design_facts")
    selected_review = review if review is not None else draft.get("model_review")
    manifest = None
    if all(isinstance(item, dict) for item in (index, requirement, design, selected_review)):
        manifest = fact_artifacts.build_fact_manifest(source, index, requirement, design, selected_review)
    return {
        "source": source,
        "index": index,
        "requirement": requirement,
        "design": design,
        "review": selected_review,
        "manifest": manifest,
        "origin_index": index,
        "review_receipt": deepcopy(
            draft.get("_verified_review_receipt") if review_receipt is ... else review_receipt
        ),
    }


class DiscussionDraftService:
    """需求讨论的草稿生成、追加和写入规则都收口在服务层。"""

    def __init__(self, paths) -> None:
        self.paths = paths
        self.mutations = DraftMutationService(paths, source="sdlc-discuss")
        self.required_sections = draft_sections.requirement_section_canonicals()

    @staticmethod
    def _active_draft(state: dict[str, object]) -> dict[str, object] | None:
        drafts = state.get("drafts", {})
        if not isinstance(drafts, dict):
            return None
        active = [
            draft
            for draft in drafts.values()
            if isinstance(draft, dict) and str(draft.get("status") or "") != "started"
        ]
        return sorted(active, key=lambda item: int(item.get("_updated_seq", 0)), reverse=True)[0] if active else None

    @staticmethod
    def _has_heading(text: str, heading: str) -> bool:
        return bool(re.search(rf"^##+\s+{re.escape(heading)}\s*$", text, flags=re.M))

    @staticmethod
    def _question_lines(questions: list[str]) -> str:
        visible = [question.strip() for question in questions if question.strip()]
        return "\n".join(f"- {question}" for question in visible)

    def _body(self, summary: str, questions: list[str]) -> str:
        clean_summary = summary.strip()
        if any(draft_sections.requirement_section_present(clean_summary, section) for section in self.required_sections):
            body = clean_summary if clean_summary.startswith("#") else f"# 需求草稿\n\n{clean_summary}"
            for section in draft_sections.missing_requirement_sections(body):
                content = self._question_lines(questions) if section == "未确认问题" else "- __PENDING__"
                body += f"\n\n## {section}\n\n{content}"
            return body.rstrip() + "\n"

        question_lines = self._question_lines(questions)
        section_content = {
            "背景和目标": clean_summary,
            "用户和使用场景": "- __PENDING__",
            "本轮范围": f"- {clean_summary}",
            "功能需求": f"- {clean_summary}",
            "未确认问题": question_lines,
        }
        sections = []
        for section in self.required_sections:
            content = section_content.get(section, "- __PENDING__")
            sections.append(f"## {section}\n\n{content}")
        return "# 需求草稿\n\n" + "\n\n".join(sections) + "\n"

    def _append_pending_record(self, previous_body: str, summary: str) -> str:
        body = previous_body.rstrip() if previous_body.strip() else self._body("", [])
        line = f"- {summary.strip()}"
        if self._has_heading(body, "待整理补充记录"):
            return body.rstrip() + "\n" + line + "\n"
        return body.rstrip() + "\n\n## 待整理补充记录\n\n" + line + "\n"

    @staticmethod
    def _short_title(summary: str) -> str:
        clean = " ".join(summary.split())
        return clean[:48] if len(clean) <= 48 else clean[:47] + "…"

    def next_draft_id(self, state: dict[str, object]) -> str:
        active = self._active_draft(state)
        if active is not None:
            return str(active["draft_id"])
        drafts = state.get("drafts", {})
        return next_number(list(drafts.keys()) if isinstance(drafts, dict) else [], "DRAFT")

    def record(
        self,
        state: dict[str, object],
        *,
        summary: str,
        questions: list[str],
        decisions: list[str],
        capture: dict[str, Any],
    ) -> tuple[str, str, bool]:
        active = self._active_draft(state)
        created = active is None
        previous_body = str(active.get("requirement_body") or "") if active else ""
        previous_questions = [
            str(item).strip()
            for item in (active.get("questions", []) if active else [])
            if str(item).strip()
        ]
        previous_decisions = [str(item).strip() for item in (active.get("decisions", []) if active else []) if str(item).strip()]
        clean_questions = [
            question.strip()
            for question in questions
            if question.strip()
        ]
        structured = any(self._has_heading(summary, section) for section in self.required_sections)
        body = self._body(summary, clean_questions) if structured or not previous_body.strip() else self._append_pending_record(previous_body, summary)
        changes = {
            "requirement_summary": self._short_title(summary),
            "requirement_body": body,
            "questions": clean_questions or ([] if decisions else previous_questions),
            "decisions": list(dict.fromkeys([*previous_decisions, *decisions])),
        }
        if active is None:
            draft_id, assessment = self.mutations.create(self._short_title(summary), initial_changes=changes, capture=capture)
        else:
            draft_id = str(active["draft_id"])
            assessment = self.mutations.mutate(draft_id, operation="更新需求讨论草稿", changes=changes, capture=capture)
        return draft_id, assessment.effective_status, created
