from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from codex_sdlc.core import draft_contract, draft_lifecycle
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    CLIENT_KEY_PATTERN,
    FORMAL_ID_PATTERN,
    TEMPORARY_REFERENCE_PATTERN,
    allocate_stable_ids,
)
from codex_sdlc.core.project import resolve_project_path
from codex_sdlc.core.reference_locator import (
    REFERENCE_LOCATOR_SCHEMA,
    validate_reference,
)
from codex_sdlc.core.structured_contract import canonical_sha256, validate_schema_document


CAPTURE_INCREMENT_SCHEMA = "capture-increment.v1"
CAPTURE_TRANSITION_SCHEMA = "capture-transition.v1"
DECISION_INPUT_SCHEMA = "decision-input.v1"
DECISION_SCHEMA = "decision.v1"
CAPTURE_STATUSES = frozenset(
    {"pending", "absorbed", "superseded", "rejected", "converted"}
)
CAPTURE_TYPES = frozenset(
    {"fact", "question", "decision", "material", "exclusion", "correction"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DRAFT_ID_PATTERN = re.compile(r"^DRAFT-[0-9]{3,}$")
_CAPTURE_ID_PATTERN = re.compile(r"^CAP-[0-9]{3,}$")
_TRANSITION_TARGET_KINDS = {
    "absorbed": frozenset({"requirement_artifact", "material", "decision"}),
    "superseded": frozenset({"capture"}),
    "rejected": frozenset({"rejection_record"}),
    "converted": frozenset({"requirement_artifact", "material", "decision"}),
}


@dataclass(frozen=True)
class PreparedIncrement:
    """一次通过全部校验的 CAP 与 DEC 写入结果。"""

    capture: dict[str, Any]
    decisions: tuple[dict[str, Any], ...]
    duplicate: bool


@dataclass(frozen=True)
class PreparedCaptureTransition:
    """一次通过完整绑定校验的 CAP 状态转换结果。"""

    transition: dict[str, Any]
    submission: dict[str, Any]
    duplicate: bool


def requirement_draft_quality(requirement_body: str, questions: list[str]) -> dict[str, Any]:
    """保留旧 DRAFT 的只读质量判断，阶段二再切换状态计算。"""

    open_questions = [question for question in questions if question.strip()]
    open_questions = list(dict.fromkeys(open_questions))

    missing_items = draft_contract.requirement_missing_items(requirement_body)
    placeholder_items = [
        phrase for phrase in draft_contract.PENDING_MARKERS if phrase in requirement_body
    ]
    status = draft_lifecycle.status_after_discuss_quality(
        open_questions=open_questions,
        missing_items=missing_items,
        placeholder_items=placeholder_items,
    )
    return {
        "status": status,
        "missing_items": missing_items,
        "placeholder_items": placeholder_items,
        "open_questions": open_questions,
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"结构化 CAP 文件包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"结构化 CAP 文件包含非标准数字：{value}。", exit_code=1)


def read_increment_document(path: Path) -> dict[str, object]:
    """完整读取严格 JSON；解析失败前不会进入任何写入入口。"""

    source = Path(path).expanduser()
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"结构化 CAP 文件无法解析：{source}。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("结构化 CAP 文件根节点必须是 JSON 对象。", exit_code=1)
    if any(not isinstance(key, str) for key in document):
        raise SdlcError("结构化 CAP 文件的字段名必须是字符串。", exit_code=1)
    return document


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SdlcError(f"{label}必须是字段名为字符串的 JSON 对象。", exit_code=1)
    return value


def _require_exact_fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    label: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise SdlcError(f"{label}缺少字段：{', '.join(missing)}。", exit_code=1)
    unknown = sorted(set(value) - required)
    if unknown:
        raise SdlcError(f"{label}包含未知字段：{', '.join(unknown)}。", exit_code=1)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SdlcError(f"{label}必须是非空字符串。", exit_code=1)
    return value.strip()


def _require_sha256(value: object, label: str) -> str:
    digest = _require_text(value, label)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise SdlcError(f"{label}必须是64位小写十六进制 SHA-256。", exit_code=1)
    return digest


def _require_text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SdlcError(f"{label}必须是非空字符串数组。", exit_code=1)
    items = [_require_text(item, f"{label}条目") for item in value]
    if len(set(items)) != len(items):
        raise SdlcError(f"{label}不能包含重复条目。", exit_code=1)
    return items


def _validate_confirmed_at(value: object) -> str:
    confirmed_at = _require_text(value, "confirmed_at")
    try:
        parsed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SdlcError("confirmed_at 必须是有效的 ISO 8601 时间。", exit_code=1) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SdlcError("confirmed_at 必须包含时区。", exit_code=1)
    return confirmed_at


def structured_increment_records(
    events: Iterable[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """只从明确事件字段读取结构化 CAP，不扫描摘要、标题或 Markdown。"""

    records: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") if isinstance(event, Mapping) else None
        capture = payload.get("capture") if isinstance(payload, dict) else None
        record = capture.get("structured_increment") if isinstance(capture, dict) else None
        if not isinstance(record, dict):
            continue
        decisions = capture.get("decision_records")
        records.append(
            {
                "capture": deepcopy(record),
                "decisions": deepcopy(decisions) if isinstance(decisions, list) else [],
            }
        )
    return records


def structured_transition_records(
    events: Iterable[Mapping[str, object]],
) -> list[dict[str, Any]]:
    """只读取独立转换事件，初始 CAP 记录不会被转换动作改写。"""

    result: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") != "structured_capture_transitioned":
            continue
        payload = event.get("payload")
        transition = payload.get("transition") if isinstance(payload, dict) else None
        if isinstance(transition, dict):
            result.append(deepcopy(transition))
    return result


def _structured_transition_entries(
    events: Iterable[Mapping[str, object]],
) -> list[dict[str, dict[str, Any]]]:
    """同时读取转换记录与事件单独保存的原始提交，供幂等校验使用。"""

    result: list[dict[str, dict[str, Any]]] = []
    for event in events:
        if (
            not isinstance(event, Mapping)
            or event.get("event_type") != "structured_capture_transitioned"
        ):
            continue
        payload = event.get("payload")
        transition = payload.get("transition") if isinstance(payload, dict) else None
        submission = (
            payload.get("transition_submission")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(transition, dict) or not isinstance(submission, dict):
            raise SdlcError("CAP 状态转换事件缺少独立原始 submission。", exit_code=1)
        result.append(
            {
                "transition": deepcopy(transition),
                "submission": deepcopy(submission),
            }
        )
    return result


def _known_explicit_ids(
    state: Mapping[str, object], records: Iterable[Mapping[str, object]]
) -> set[str]:
    """只收集固定编号字段，普通文字即使形似编号也不会成为合法目标。"""

    result: set[str] = set()
    drafts = state.get("drafts")
    if isinstance(drafts, dict):
        for draft_id, draft in drafts.items():
            if isinstance(draft_id, str) and FORMAL_ID_PATTERN.fullmatch(draft_id):
                result.add(draft_id)
            if not isinstance(draft, dict):
                continue
            for material in draft.get("materials", []):
                if isinstance(material, dict):
                    material_id = material.get("material_id")
                    if isinstance(material_id, str) and FORMAL_ID_PATTERN.fullmatch(material_id):
                        result.add(material_id)
            receipt = draft.get("requirement_import")
            mapping = receipt.get("mapping") if isinstance(receipt, dict) else None
            if isinstance(mapping, dict):
                for formal_id in mapping.values():
                    if isinstance(formal_id, str) and FORMAL_ID_PATTERN.fullmatch(formal_id):
                        result.add(formal_id)

    requirements = state.get("requirements")
    if isinstance(requirements, dict):
        for requirement_id, requirement in requirements.items():
            if isinstance(requirement_id, str) and FORMAL_ID_PATTERN.fullmatch(requirement_id):
                result.add(requirement_id)
            if not isinstance(requirement, dict):
                continue
            for field in ("tasks", "requirement_points", "acceptance_points", "test_cases"):
                for item in requirement.get(field, []):
                    if not isinstance(item, dict):
                        continue
                    formal_id = item.get("task_id") if field == "tasks" else item.get("id")
                    if isinstance(formal_id, str) and FORMAL_ID_PATTERN.fullmatch(formal_id):
                        result.add(formal_id)

    captures = state.get("captures")
    if isinstance(captures, list):
        for capture in captures:
            capture_id = capture.get("capture_id") if isinstance(capture, dict) else None
            if isinstance(capture_id, str) and FORMAL_ID_PATTERN.fullmatch(capture_id):
                result.add(capture_id)

    for item in records:
        capture = item.get("capture")
        capture_id = capture.get("capture_id") if isinstance(capture, dict) else None
        if isinstance(capture_id, str) and FORMAL_ID_PATTERN.fullmatch(capture_id):
            result.add(capture_id)
        decisions = item.get("decisions")
        if not isinstance(decisions, list):
            continue
        for decision in decisions:
            decision_id = decision.get("decision_id") if isinstance(decision, dict) else None
            if isinstance(decision_id, str) and FORMAL_ID_PATTERN.fullmatch(decision_id):
                result.add(decision_id)
    return result


def _reference_identity(reference: Mapping[str, object]) -> str:
    return canonical_sha256(reference)


def _validate_target(
    project_root: Path,
    value: object,
    *,
    known_ids: set[str],
    historical_replay: bool = False,
) -> dict[str, object]:
    target = _require_object(value, "targets 条目")
    _require_exact_fields(
        target,
        required={"target_id", "reference"},
        label="targets 条目",
    )
    target_id = _require_text(target["target_id"], "target_id").upper()
    if FORMAL_ID_PATTERN.fullmatch(target_id) is None:
        raise SdlcError(f"目标编号格式不正确：{target_id}。", exit_code=1)
    if target_id not in known_ids:
        raise SdlcError(f"目标编号不存在：{target_id}。", exit_code=1)
    reference = _require_object(target["reference"], "目标 reference")
    if historical_replay:
        # 历史事件已经保存原始 submission、完整 reference 和各自哈希。重放时只核对
        # 结构与受控路径，不能再拿旧哈希读取已经合法更新的当前需求投影。
        validate_schema_document(reference, schema_name=REFERENCE_LOCATOR_SCHEMA)
        relative_path = _require_text(reference.get("path"), "目标 reference.path")
        resolve_project_path(project_root, relative_path)
        content = None
    else:
        # 写入入口始终走真实文件定位和哈希校验，漂移引用不能进入事件历史。
        match = validate_reference(project_root, reference)
        content = match.content
        if (
            content is None
            and match.kind == "whole_file"
            and match.path.suffix.lower() == ".json"
        ):
            try:
                content = json.loads(match.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SdlcError(
                    "目标 reference 指向的 JSON 文件无法解析。", exit_code=1
                ) from exc

    # 只读取受控编号字段；即使定位整份 JSON，也不能拿包含另一个对象编号的文件冒充目标。
    controlled_fields = {
        "id",
        "draft_id",
        "material_id",
        "requirement_id",
        "capture_id",
        "decision_id",
    }
    explicit_ids: list[str] = []

    def collect_explicit_ids(value: object) -> None:
        if isinstance(value, dict):
            for field, item in value.items():
                if (
                    field in controlled_fields
                    and isinstance(item, str)
                    and FORMAL_ID_PATTERN.fullmatch(item)
                ):
                    explicit_ids.append(item)
                collect_explicit_ids(item)
        elif isinstance(value, list):
            for item in value:
                collect_explicit_ids(item)

    collect_explicit_ids(content)
    explicit_ids = list(dict.fromkeys(explicit_ids))
    if explicit_ids and target_id not in explicit_ids:
        raise SdlcError(
            f"目标 reference 命中的是 {', '.join(explicit_ids)}，不是 {target_id}。",
            exit_code=1,
        )
    if _DRAFT_ID_PATTERN.fullmatch(target_id):
        expected_prefix = f".codex-sdlc/drafts/{target_id}/"
        relative_path = _require_text(reference.get("path"), "目标 reference.path")
        if not relative_path.startswith(expected_prefix):
            raise SdlcError(
                f"{target_id} 的目标 reference 必须定位到自己的 DRAFT 目录。",
                exit_code=1,
            )
    return {"target_id": target_id, "reference": deepcopy(reference)}


def capture_transition_target_bindings(
    state: Mapping[str, object],
) -> dict[str, tuple[dict[str, object], ...]]:
    """收集编号对应的真实来源定位，供写入与重放核对同一个目标。"""

    bindings: dict[str, list[dict[str, object]]] = {}

    def append_exact(target_id: object, reference: object) -> None:
        if (
            isinstance(target_id, str)
            and FORMAL_ID_PATTERN.fullmatch(target_id)
            and isinstance(reference, dict)
        ):
            bindings.setdefault(target_id, []).append(
                {"mode": "exact", "reference": deepcopy(reference)}
            )

    drafts = state.get("drafts")
    if isinstance(drafts, Mapping):
        for draft_id, draft in drafts.items():
            if not isinstance(draft, Mapping):
                continue
            for capture in draft.get("structured_captures", []):
                if isinstance(capture, Mapping):
                    append_exact(capture.get("capture_id"), capture.get("source_reference"))
            for decision in draft.get("decision_records", []):
                if isinstance(decision, Mapping):
                    append_exact(decision.get("decision_id"), decision.get("source_reference"))
            for material in draft.get("materials", []):
                if not isinstance(material, Mapping) or material.get("source_kind") != "file":
                    continue
                material_id = material.get("material_id")
                stored_path = material.get("stored_path")
                digest = material.get("sha256")
                if (
                    isinstance(material_id, str)
                    and FORMAL_ID_PATTERN.fullmatch(material_id)
                    and isinstance(draft_id, str)
                    and isinstance(stored_path, str)
                    and isinstance(digest, str)
                ):
                    bindings.setdefault(material_id, []).append(
                        {
                            "mode": "file",
                            "path": f".codex-sdlc/drafts/{draft_id}/{stored_path}",
                            "sha256": digest,
                        }
                    )
    return {
        target_id: tuple(
            sorted(items, key=lambda item: canonical_sha256(item))
        )
        for target_id, items in bindings.items()
    }


def capture_transition_known_ids(state: Mapping[str, object]) -> set[str]:
    """按写入时相同的受控编号字段收集转换目标，普通文字不会产生目标。"""

    records: list[dict[str, object]] = []
    drafts = state.get("drafts")
    if isinstance(drafts, Mapping):
        for draft in drafts.values():
            if not isinstance(draft, Mapping):
                continue
            captures = draft.get("structured_captures")
            decisions = draft.get("decision_records")
            if not isinstance(captures, list):
                continue
            for capture in captures:
                if isinstance(capture, Mapping):
                    records.append(
                        {
                            "capture": capture,
                            "decisions": decisions if isinstance(decisions, list) else [],
                        }
                    )
    return _known_explicit_ids(state, records)


def validate_capture_transition_relation(
    project_root: Path,
    relation: object,
    *,
    to_status: str,
    capture_id: str,
    known_ids: set[str],
    target_bindings: Mapping[str, tuple[dict[str, object], ...]] | None = None,
    historical_replay: bool = False,
) -> dict[str, object]:
    """确定性校验 CAP 转换关系；只有事件重放不读取可变的当前投影。"""

    relation_input = _require_object(relation, "relation")
    _require_exact_fields(
        relation_input,
        required={"kind", "target_id", "reference"},
        label="relation",
    )
    relation_kind = _require_text(relation_input["kind"], "relation.kind")
    if (
        to_status not in _TRANSITION_TARGET_KINDS
        or relation_kind not in _TRANSITION_TARGET_KINDS[to_status]
    ):
        raise SdlcError(
            f"{to_status} 不接受 relation.kind={relation_kind}。", exit_code=1
        )
    target = _validate_target(
        project_root,
        {
            "target_id": relation_input["target_id"],
            "reference": relation_input["reference"],
        },
        known_ids=known_ids,
        historical_replay=historical_replay,
    )
    target_id = str(target["target_id"])
    expected_prefixes = {
        "material": "MAT-",
        "decision": "DEC-",
        "capture": "CAP-",
        "rejection_record": "DRAFT-",
    }
    expected_prefix = expected_prefixes.get(relation_kind)
    if expected_prefix and not target_id.startswith(expected_prefix):
        raise SdlcError(
            f"relation.kind={relation_kind} 必须引用 {expected_prefix} 编号。",
            exit_code=1,
        )
    if relation_kind == "capture" and target_id == capture_id:
        raise SdlcError("superseded 不能把 CAP 自己作为替代来源。", exit_code=1)
    bindings = target_bindings.get(target_id, ()) if target_bindings is not None else ()
    if bindings:
        reference = target["reference"]
        matched_binding = any(
            (
                binding.get("mode") == "exact"
                and canonical_sha256(binding.get("reference"))
                == canonical_sha256(reference)
            )
            or (
                binding.get("mode") == "file"
                and isinstance(reference, Mapping)
                and reference.get("path") == binding.get("path")
                and reference.get("sha256") == binding.get("sha256")
            )
            for binding in bindings
        )
        if not matched_binding:
            raise SdlcError(
                f"目标 reference 没有命中 {target_id} 的真实来源定位。",
                exit_code=1,
            )
    return {
        "kind": relation_kind,
        "target_id": target_id,
        "reference": deepcopy(target["reference"]),
    }


def _validate_targets(
    project_root: Path,
    value: object,
    *,
    known_ids: set[str],
    label: str,
) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise SdlcError(f"{label}必须是非空数组。", exit_code=1)
    targets = [
        _validate_target(project_root, item, known_ids=known_ids) for item in value
    ]
    identities = [_reference_identity(item) for item in targets]
    if len(set(identities)) != len(identities):
        raise SdlcError(f"{label}不能包含重复定位。", exit_code=1)
    return sorted(
        targets,
        key=lambda item: (str(item["target_id"]), _reference_identity(item)),
    )


def _validate_source_reference(
    project_root: Path,
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], object | None]:
    reference = _require_object(value, label)
    match = validate_reference(project_root, reference)
    return deepcopy(reference), deepcopy(match.content)


def _validate_source_content(source_content: object, expected: str, *, label: str) -> None:
    """有可直接读取的文字或结构化字段时执行精确相等，不做包含或近似匹配。"""

    if isinstance(source_content, str) and source_content.strip() != expected:
        raise SdlcError(f"{label}与精确来源定位命中的文字不一致。", exit_code=1)
    if isinstance(source_content, dict):
        explicit = source_content.get("increment")
        if isinstance(explicit, str) and explicit.strip() != expected:
            raise SdlcError(f"{label}与精确来源定位命中的 increment 不一致。", exit_code=1)


def _decision_identity(decision: Mapping[str, object]) -> str:
    return draft_lifecycle.decision_identity_sha256(decision)


def _validate_decision_input(
    project_root: Path,
    value: object,
    *,
    known_ids: set[str],
    root_client_key: str,
    current_reference_ids: set[str],
    target_reference_ids: set[str],
) -> dict[str, object]:
    decision = _require_object(value, "decisions 条目")
    _require_exact_fields(
        decision,
        required={
            "schema_version",
            "client_key",
            "question",
            "candidates",
            "selection",
            "scope",
            "source_reference",
            "confirmed_at",
        },
        label="decisions 条目",
    )
    if decision["schema_version"] != DECISION_INPUT_SCHEMA:
        raise SdlcError(
            f"决定输入版本不受支持：{decision['schema_version']}。", exit_code=1
        )
    client_key = _require_text(decision["client_key"], "决定 client_key")
    if CLIENT_KEY_PATTERN.fullmatch(client_key) is None or client_key == root_client_key:
        raise SdlcError(f"决定 client_key 格式不正确或与 CAP 重复：{client_key}。", exit_code=1)

    question = _require_object(decision["question"], "决定 question")
    _require_exact_fields(
        question,
        required={"text", "capture_ref", "reference"},
        label="决定 question",
    )
    question_text = _require_text(question["text"], "decision question.text")
    capture_ref = _require_text(question["capture_ref"], "决定 question.capture_ref")
    if TEMPORARY_REFERENCE_PATTERN.fullmatch(capture_ref) is not None:
        raise SdlcError("决定只能引用已经登记的结构化问题 CAP。", exit_code=1)
    if _CAPTURE_ID_PATTERN.fullmatch(capture_ref) is None or capture_ref not in known_ids:
        raise SdlcError(f"决定引用的 CAP 不存在：{capture_ref}。", exit_code=1)
    question_reference, question_content = _validate_source_reference(
        project_root, question["reference"], label="决定 question.reference"
    )
    _validate_source_content(question_content, question_text, label="决定问题")

    candidates = _require_text_list(decision["candidates"], "决定 candidates")
    selection = _require_text(decision["selection"], "决定 selection")
    if selection not in candidates:
        raise SdlcError("决定 selection 必须精确等于一个 candidates 条目。", exit_code=1)
    scope = _validate_targets(
        project_root,
        decision["scope"],
        known_ids=known_ids,
        label="决定 scope",
    )
    scope_reference_ids = {_reference_identity(item) for item in scope}
    if not scope_reference_ids <= target_reference_ids:
        raise SdlcError("决定 scope 必须是当前 CAP targets 的明确子集。", exit_code=1)

    source_reference, source_content = _validate_source_reference(
        project_root, decision["source_reference"], label="决定 source_reference"
    )
    if _reference_identity(source_reference) not in current_reference_ids:
        raise SdlcError("决定 source_reference 必须来自当前 CAP 的精确引用。", exit_code=1)
    _validate_source_content(source_content, selection, label="用户选择")

    return {
        "schema_version": DECISION_INPUT_SCHEMA,
        "client_key": client_key,
        "question": {
            "text": question_text,
            "capture_ref": capture_ref,
            "reference": question_reference,
        },
        "candidates": candidates,
        "selection": selection,
        "scope": scope,
        "source_reference": source_reference,
        "confirmed_at": _validate_confirmed_at(decision["confirmed_at"]),
    }


def _existing_submission(
    records: Iterable[Mapping[str, object]], submission_key: str
) -> Mapping[str, object] | None:
    matches: list[Mapping[str, object]] = []
    for item in records:
        capture = item.get("capture")
        if isinstance(capture, dict) and capture.get("submission_key") == submission_key:
            matches.append(item)
    if len(matches) > 1:
        raise SdlcError(f"结构化 CAP 事件包含重复 submission_key：{submission_key}。", exit_code=1)
    return matches[0] if matches else None


def _validate_question_capture_binding(
    decision: Mapping[str, object],
    *,
    captures_by_id: Mapping[str, Mapping[str, object]],
) -> None:
    question = decision.get("question")
    if not isinstance(question, dict):
        raise SdlcError("决定缺少结构化 question。", exit_code=1)
    capture_ref = str(question.get("capture_ref") or "")
    referenced_capture = captures_by_id.get(capture_ref)
    if referenced_capture is None:
        raise SdlcError(f"决定引用的 CAP 不存在：{capture_ref}。", exit_code=1)
    if not draft_lifecycle.decision_matches_question_capture(decision, referenced_capture):
        raise SdlcError(
            "决定 question.reference 不属于所引用 CAP 的 source_reference 精确定位。",
            exit_code=1,
        )


def prepare_increment(
    project_root: Path,
    document: Mapping[str, object],
    *,
    state: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
) -> PreparedIncrement:
    """先完成结构、定位、哈希、引用、编号和冲突检查，再返回可写事件数据。"""

    data = _require_object(dict(document), "结构化 CAP")
    _require_exact_fields(
        data,
        required={
            "schema_version",
            "submission_key",
            "draft_id",
            "client_key",
            "capture_type",
            "targets",
            "source_reference",
            "source_sha256",
            "increment",
            "status",
            "decisions",
        },
        label="结构化 CAP",
    )
    if data["schema_version"] != CAPTURE_INCREMENT_SCHEMA:
        raise SdlcError(
            f"结构化 CAP 版本不受支持：{data['schema_version']}。", exit_code=1
        )
    submission_key = _require_text(data["submission_key"], "submission_key")
    client_key = _require_text(data["client_key"], "client_key")
    if CLIENT_KEY_PATTERN.fullmatch(submission_key) is None:
        raise SdlcError(f"submission_key 格式不正确：{submission_key}。", exit_code=1)
    if CLIENT_KEY_PATTERN.fullmatch(client_key) is None:
        raise SdlcError(f"client_key 格式不正确：{client_key}。", exit_code=1)

    draft_id = _require_text(data["draft_id"], "draft_id").upper()
    if _DRAFT_ID_PATTERN.fullmatch(draft_id) is None:
        raise SdlcError(f"DRAFT 编号格式不正确：{draft_id}。", exit_code=1)
    drafts = state.get("drafts")
    draft = drafts.get(draft_id) if isinstance(drafts, dict) else None
    if not isinstance(draft, dict):
        raise SdlcError(f"没有找到 DRAFT `{draft_id}`。", exit_code=1)
    if str(draft.get("status") or "") == "started":
        raise SdlcError(f"{draft_id} 已经正式建档，不能再追加 CAP。", exit_code=1)

    capture_type = _require_text(data["capture_type"], "capture_type")
    if capture_type not in CAPTURE_TYPES:
        raise SdlcError(f"不支持的 capture_type：{capture_type}。", exit_code=1)
    status = _require_text(data["status"], "status")
    if status not in CAPTURE_STATUSES:
        raise SdlcError(f"不支持的 CAP 状态：{status}。", exit_code=1)
    if status != "pending":
        raise SdlcError("新 CAP 只能以 pending 状态登记。", exit_code=1)
    increment = _require_text(data["increment"], "increment")

    existing_records = structured_increment_records(events)
    known_ids = _known_explicit_ids(state, existing_records)
    targets = _validate_targets(
        project_root,
        data["targets"],
        known_ids=known_ids,
        label="targets",
    )
    source_reference, source_content = _validate_source_reference(
        project_root, data["source_reference"], label="source_reference"
    )
    source_sha256 = _require_sha256(data["source_sha256"], "source_sha256")
    if source_reference.get("sha256") != source_sha256:
        raise SdlcError("source_sha256 与 source_reference.sha256 不一致。", exit_code=1)
    _validate_source_content(source_content, increment, label="CAP 增量")

    raw_decisions = data["decisions"]
    if not isinstance(raw_decisions, list):
        raise SdlcError("decisions 必须是数组。", exit_code=1)
    if capture_type == "decision" and not raw_decisions:
        raise SdlcError("decision 类型的 CAP 必须提供结构化 decisions。", exit_code=1)
    if capture_type != "decision" and raw_decisions:
        raise SdlcError("只有 decision 类型的 CAP 可以登记用户决定。", exit_code=1)

    current_reference_ids = {
        _reference_identity(source_reference),
        *(_reference_identity(item["reference"]) for item in targets),
    }
    target_reference_ids = {_reference_identity(item) for item in targets}
    decisions = [
        _validate_decision_input(
            project_root,
            item,
            known_ids=known_ids,
            root_client_key=client_key,
            current_reference_ids=current_reference_ids,
            target_reference_ids=target_reference_ids,
        )
        for item in raw_decisions
    ]
    decision_client_keys = [str(item["client_key"]) for item in decisions]
    if len(set(decision_client_keys)) != len(decision_client_keys):
        raise SdlcError("decisions 的 client_key 不能重复。", exit_code=1)

    submission_sha256 = canonical_sha256(data)
    existing_submission = _existing_submission(existing_records, submission_key)
    if existing_submission is not None:
        existing_capture = existing_submission.get("capture")
        if not isinstance(existing_capture, dict):
            raise SdlcError("已有 CAP 事件缺少结构化记录。", exit_code=1)
        if existing_capture.get("submission_sha256") != submission_sha256:
            raise SdlcError(
                f"submission_key={submission_key} 已存在，但提交内容不同。",
                exit_code=1,
            )
        existing_decisions = existing_submission.get("decisions")
        return PreparedIncrement(
            capture=deepcopy(existing_capture),
            decisions=tuple(
                deepcopy(existing_decisions)
                if isinstance(existing_decisions, list)
                else []
            ),
            duplicate=True,
        )

    captures_by_id = {
        str(item["capture"].get("capture_id")): item["capture"]
        for item in existing_records
        if isinstance(item.get("capture"), dict)
        and isinstance(item["capture"].get("capture_id"), str)
    }
    existing_decisions = [
        decision
        for item in existing_records
        for decision in (item.get("decisions") if isinstance(item.get("decisions"), list) else [])
        if isinstance(decision, dict)
    ]
    # 问题 CAP、来源定位和决定冲突都在编号分配前完成校验，失败不会占用 CAP/DEC 编号。
    checked_decisions: list[dict[str, object]] = []
    for item in decisions:
        _validate_question_capture_binding(item, captures_by_id=captures_by_id)
        identity = _decision_identity(item)
        for previous in [*existing_decisions, *checked_decisions]:
            previous_identity = previous.get("decision_identity_sha256") or _decision_identity(previous)
            if previous_identity != identity:
                continue
            if previous.get("selection") != item.get("selection"):
                raise SdlcError(
                    f"用户决定与 {previous.get('decision_id') or '已有决定'} 冲突。",
                    exit_code=1,
                )
            raise SdlcError(
                f"用户决定与 {previous.get('decision_id') or '已有决定'} 重复。",
                exit_code=1,
            )
        checked_decisions.append(item)

    allocation_objects = [
        AllocationObject(client_key=client_key, id_prefix="CAP", depends_on=()),
        *(
            AllocationObject(
                client_key=str(item["client_key"]),
                id_prefix="DEC",
                depends_on=(str(item["question"]["capture_ref"]),)
                if TEMPORARY_REFERENCE_PATTERN.fullmatch(str(item["question"]["capture_ref"]))
                else (),
            )
            for item in decisions
        ),
    ]
    mapping = allocate_stable_ids(allocation_objects, existing_ids=known_ids)
    capture_id = mapping[client_key]
    capture_record: dict[str, Any] = {
        "schema_version": CAPTURE_INCREMENT_SCHEMA,
        "submission_key": submission_key,
        "submission_sha256": submission_sha256,
        "capture_id": capture_id,
        "draft_id": draft_id,
        "capture_type": capture_type,
        "targets": targets,
        "source_reference": source_reference,
        "source_sha256": source_sha256,
        "increment": increment,
        "increment_sha256": canonical_sha256(increment),
        "status": status,
        "decision_ids": [mapping[str(item["client_key"])] for item in decisions],
    }
    capture_record["record_sha256"] = canonical_sha256(capture_record)

    prepared_decisions: list[dict[str, Any]] = []
    for item in decisions:
        question = deepcopy(item["question"])
        capture_ref = str(question["capture_ref"])
        temporary = TEMPORARY_REFERENCE_PATTERN.fullmatch(capture_ref)
        if temporary is not None:
            question["capture_ref"] = mapping[temporary.group("client_key")]
        decision_record: dict[str, Any] = {
            "schema_version": DECISION_SCHEMA,
            "decision_id": mapping[str(item["client_key"])],
            "status": "confirmed",
            "question": question,
            "candidates": deepcopy(item["candidates"]),
            "selection": item["selection"],
            "scope": deepcopy(item["scope"]),
            # 决定来源是当前登记 CAP；被回答的问题另由 question.capture_ref 精确引用。
            "source_capture_id": capture_id,
            "source_reference": deepcopy(item["source_reference"]),
            "confirmed_at": item["confirmed_at"],
        }
        decision_record["decision_identity_sha256"] = _decision_identity(decision_record)
        decision_record["decision_sha256"] = canonical_sha256(decision_record)
        prepared_decisions.append(decision_record)

    return PreparedIncrement(
        capture=capture_record,
        decisions=tuple(prepared_decisions),
        duplicate=False,
    )


def prepare_capture_transition(
    project_root: Path,
    document: Mapping[str, object],
    *,
    state: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
) -> PreparedCaptureTransition:
    """绑定初始 CAP、提交哈希和明确产物关系后生成独立状态转换事实。"""

    data = _require_object(dict(document), "CAP 状态转换")
    _require_exact_fields(
        data,
        required={
            "schema_version",
            "transition_key",
            "draft_id",
            "capture_id",
            "source_submission_key",
            "source_submission_sha256",
            "source_record_sha256",
            "from_status",
            "to_status",
            "relation",
        },
        label="CAP 状态转换",
    )
    if data["schema_version"] != CAPTURE_TRANSITION_SCHEMA:
        raise SdlcError(
            f"CAP 状态转换版本不受支持：{data['schema_version']}。", exit_code=1
        )
    transition_key = _require_text(data["transition_key"], "transition_key")
    if CLIENT_KEY_PATTERN.fullmatch(transition_key) is None:
        raise SdlcError(f"transition_key 格式不正确：{transition_key}。", exit_code=1)
    draft_id = _require_text(data["draft_id"], "draft_id").upper()
    capture_id = _require_text(data["capture_id"], "capture_id").upper()
    if _DRAFT_ID_PATTERN.fullmatch(draft_id) is None:
        raise SdlcError(f"DRAFT 编号格式不正确：{draft_id}。", exit_code=1)
    if _CAPTURE_ID_PATTERN.fullmatch(capture_id) is None:
        raise SdlcError(f"CAP 编号格式不正确：{capture_id}。", exit_code=1)
    source_submission_key = _require_text(
        data["source_submission_key"], "source_submission_key"
    )
    source_submission_sha256 = _require_sha256(
        data["source_submission_sha256"], "source_submission_sha256"
    )
    source_record_sha256 = _require_sha256(
        data["source_record_sha256"], "source_record_sha256"
    )
    from_status = _require_text(data["from_status"], "from_status")
    to_status = _require_text(data["to_status"], "to_status")
    if from_status != "pending" or to_status not in _TRANSITION_TARGET_KINDS:
        raise SdlcError(
            "CAP 只允许从 pending 转换为 absorbed、superseded、rejected 或 converted。",
            exit_code=1,
        )

    transition_submission_sha256 = canonical_sha256(data)
    transition_entries = _structured_transition_entries(events)
    transitions = [item["transition"] for item in transition_entries]
    key_matches = [
        item
        for item in transition_entries
        if item["transition"].get("transition_key") == transition_key
    ]
    if len(key_matches) > 1:
        raise SdlcError(
            f"CAP 状态转换事件包含重复 transition_key：{transition_key}。",
            exit_code=1,
        )
    if key_matches:
        existing = key_matches[0]["transition"]
        if existing.get("transition_submission_sha256") != transition_submission_sha256:
            raise SdlcError(
                f"transition_key={transition_key} 已存在，但提交内容不同。",
                exit_code=1,
            )
        return PreparedCaptureTransition(
            transition=deepcopy(existing),
            submission=deepcopy(key_matches[0]["submission"]),
            duplicate=True,
        )

    drafts = state.get("drafts")
    draft = drafts.get(draft_id) if isinstance(drafts, dict) else None
    if not isinstance(draft, dict):
        raise SdlcError(f"没有找到 DRAFT `{draft_id}`。", exit_code=1)
    structured_captures = [
        item
        for item in draft.get("structured_captures", [])
        if isinstance(item, dict) and item.get("capture_id") == capture_id
    ]
    global_captures = [
        item
        for item in state.get("captures", [])
        if isinstance(item, dict) and item.get("capture_id") == capture_id
    ]
    if len(structured_captures) != 1 or len(global_captures) != 1:
        raise SdlcError(f"{capture_id} 的全局记录和 DRAFT 记录不唯一。", exit_code=1)
    source_capture = structured_captures[0]
    global_capture = global_captures[0]
    bindings = {
        "draft_id": draft_id,
        "submission_key": source_submission_key,
        "submission_sha256": source_submission_sha256,
        "record_sha256": source_record_sha256,
    }
    for field, expected in bindings.items():
        if source_capture.get(field) != expected:
            raise SdlcError(f"{capture_id} 的原始 {field} 与转换输入不一致。", exit_code=1)
    if canonical_sha256(global_capture.get("structured_increment")) != canonical_sha256(
        source_capture
    ):
        raise SdlcError(f"{capture_id} 的全局记录与 DRAFT 初始记录不一致。", exit_code=1)
    statuses = draft.get("capture_statuses")
    draft_status = statuses.get(capture_id) if isinstance(statuses, dict) else source_capture.get("status")
    if source_capture.get("status") != "pending" or draft_status != global_capture.get("status"):
        raise SdlcError(f"{capture_id} 的初始状态或全局状态不一致。", exit_code=1)
    if draft_status != from_status:
        raise SdlcError(
            f"{capture_id} 当前状态是 {draft_status}，不能再从 {from_status} 转换。",
            exit_code=1,
        )
    if any(item.get("capture_id") == capture_id for item in transitions):
        raise SdlcError(f"{capture_id} 已经存在状态转换，不能登记第二种状态。", exit_code=1)

    relation = validate_capture_transition_relation(
        project_root,
        data["relation"],
        to_status=to_status,
        capture_id=capture_id,
        known_ids=capture_transition_known_ids(state),
        target_bindings=capture_transition_target_bindings(state),
    )

    transition: dict[str, Any] = {
        "schema_version": CAPTURE_TRANSITION_SCHEMA,
        "transition_key": transition_key,
        "transition_submission_sha256": transition_submission_sha256,
        "draft_id": draft_id,
        "capture_id": capture_id,
        "source_submission_key": source_submission_key,
        "source_submission_sha256": source_submission_sha256,
        "source_record_sha256": source_record_sha256,
        "from_status": from_status,
        "to_status": to_status,
        "previous_transition_sha256": "",
        "relation": relation,
    }
    transition["transition_sha256"] = canonical_sha256(transition)
    return PreparedCaptureTransition(
        transition=transition,
        # 原始提交和转换记录分别存放。重放时二者交叉核对，不能只重算记录内自哈希。
        submission=deepcopy(data),
        duplicate=False,
    )


__all__ = [
    "CAPTURE_INCREMENT_SCHEMA",
    "CAPTURE_TRANSITION_SCHEMA",
    "CAPTURE_STATUSES",
    "CAPTURE_TYPES",
    "capture_transition_target_bindings",
    "capture_transition_known_ids",
    "DECISION_INPUT_SCHEMA",
    "DECISION_SCHEMA",
    "PreparedIncrement",
    "PreparedCaptureTransition",
    "prepare_capture_transition",
    "prepare_increment",
    "read_increment_document",
    "requirement_draft_quality",
    "structured_increment_records",
    "structured_transition_records",
    "validate_capture_transition_relation",
]
