from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hmac
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from codex_sdlc.core.codex_assets import get_project_codex_asset_status
from codex_sdlc.core import draft_artifacts, draft_contract, draft_lifecycle, draft_ownership, external_version, fact_gate, reference_locator, start_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.git_tools import (
    current_git_changed_files,
    current_git_status_lines,
    detect_project_commands,
    ensure_sdlc_global_ignore,
    git_check_ignore,
    read_global_excludesfile,
)
from codex_sdlc.core.project import ProjectPaths, ensure_base_dirs, resolve_project_path
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_COVERAGE_SCHEMA,
    REQUIREMENT_SPLIT_SCHEMA,
    read_requirement_document,
)
from codex_sdlc.core.render import join_lines, relative_to_project
from codex_sdlc.core.structured_contract import canonical_json_text, canonical_sha256, contract_sha256, sha256_file, validate_schema_document
from codex_sdlc.core.task_ids import subtask_display_label
from codex_sdlc.core.task_quality import (
    project_direct_requirement_status,
    task_plan_stop_reason,
)
from codex_sdlc.legacy.task_pack_reader import (
    inspect_legacy_task_packs,
    legacy_task_pack_check_messages,
    legacy_task_pack_display_lines,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - 这套工具主要跑在 macOS，本分支只给其它系统兜底。
    fcntl = None


ACTIVE_TASK_RUN_STATUSES = {"doing", "ready_for_user_check", "test_failed"}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    project_path TEXT NOT NULL,
    requirement_id TEXT,
    task_id TEXT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
    requirement_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    folder_name TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    blocked_reason TEXT NOT NULL,
    flow_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    requirement_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    subtasks_json TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    changed_files_json TEXT NOT NULL,
    commands_json TEXT NOT NULL,
    test_items_json TEXT NOT NULL,
    test_commands_json TEXT NOT NULL,
    test_scripts_json TEXT NOT NULL,
    manual_checks_json TEXT NOT NULL,
    verifications_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY (requirement_id, task_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    next_step TEXT NOT NULL,
    related_requirements_json TEXT NOT NULL,
    related_tasks_json TEXT NOT NULL,
    changed_files_json TEXT NOT NULL,
    commands_json TEXT NOT NULL,
    verifications_json TEXT NOT NULL,
    unresolved_issues_json TEXT NOT NULL,
    suggested_commit TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    capture_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grills (
    grill_id TEXT PRIMARY KEY,
    requirement_id TEXT,
    task_id TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    material_type TEXT NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    change_id TEXT PRIMARY KEY,
    requirement_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS designs (
    design_id TEXT PRIMARY KEY,
    requirement_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    file_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    requirement_summary TEXT NOT NULL,
    design_summary TEXT NOT NULL,
    requirement_body TEXT NOT NULL,
    design_body TEXT NOT NULL,
    questions_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    review_items_json TEXT NOT NULL,
    started_requirement_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    folder_path TEXT NOT NULL
);

"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_events(paths: ProjectPaths) -> list[dict[str, Any]]:
    if not paths.events_file.exists():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in paths.events_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SdlcError(f"`events.jsonl` 第 {len(events) + 1} 行无法解析：{exc.msg}") from exc
    return events


def write_event_line(paths: ProjectPaths, event: dict[str, Any]) -> None:
    ensure_base_dirs(paths)
    if paths.events_file.exists() and paths.events_file.stat().st_size > 0:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f")
        backup_file = paths.backups_dir / f"events-{timestamp}.jsonl.bak"
        shutil.copy2(paths.events_file, backup_file)
        prune_event_backups(paths)
    with paths.events_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


@contextmanager
def event_write_lock(paths: ProjectPaths):
    ensure_base_dirs(paths)
    lock_file = paths.sdlc_dir / ".events.lock"
    with lock_file.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def next_number(existing_ids: list[str], prefix: str, width: int = 3) -> str:
    numbers: list[int] = []
    for item in existing_ids:
        match = re.search(rf"{re.escape(prefix)}-(\d+)", item)
        if match:
            numbers.append(int(match.group(1)))
    next_value = (max(numbers) if numbers else 0) + 1
    return f"{prefix}-{next_value:0{width}d}"


def next_event_id(events: list[dict[str, Any]]) -> str:
    today = datetime.now().strftime("%Y%m%d")
    today_prefix = f"EVT-{today}-"
    used_ids = {str(event.get("event_id", "")) for event in events}
    numbers: list[int] = []
    for event_id in used_ids:
        if not event_id.startswith(today_prefix):
            continue
        match = re.fullmatch(rf"{re.escape(today_prefix)}(\d+)", event_id)
        if match:
            numbers.append(int(match.group(1)))
    next_value = (max(numbers) if numbers else 0) + 1
    # events.jsonl 可能经过恢复或人工清理，中间会缺号；按当天最大号递增，避免总行数回退后撞号。
    while f"{today_prefix}{next_value:06d}" in used_ids:
        next_value += 1
    return f"{today_prefix}{next_value:06d}"


def append_event(
    paths: ProjectPaths,
    *,
    event_type: str,
    source: str,
    summary: str,
    payload: dict[str, Any],
    requirement_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    with event_write_lock(paths):
        events = load_events(paths)
        # 先写事件流水，再去重建索引和快照，这样中途失败时至少还能靠 JSONL 恢复。
        event = {
            "event_id": next_event_id(events),
            "event_type": event_type,
            "project_path": str(paths.root),
            "requirement_id": requirement_id,
            "task_id": task_id,
            "created_at": now_iso(),
            "source": source,
            "summary": summary,
            "payload": payload,
        }
        write_event_line(paths, event)
    return event


def prune_event_backups(paths: ProjectPaths, *, keep: int = 10) -> None:
    if not paths.backups_dir.exists():
        return
    backup_files = sorted(paths.backups_dir.glob("events-*.jsonl.bak"))
    for backup_file in backup_files[:-keep]:
        backup_file.unlink(missing_ok=True)


def cleanup_transient_files(paths: ProjectPaths) -> None:
    if paths.sdlc_dir.exists():
        for ds_store_file in paths.sdlc_dir.rglob(".DS_Store"):
            ds_store_file.unlink(missing_ok=True)
    prune_event_backups(paths)


def slugify_text(text: str) -> str:
    ascii_words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    if ascii_words:
        return "-".join(ascii_words[:4])
    return "item"


def shorten_text(text: str, length: int = 36) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= length:
        return clean
    return clean[: length - 1] + "…"


def build_design_title(text: str, length: int = 28) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    prefix_match = re.match(r"^(技术方案草案|技术方案|方案草案)[:：]?", clean)
    if prefix_match:
        return prefix_match.group(1)
    return shorten_text(clean, length)


def strip_design_prefix(text: str) -> str:
    return re.sub(r"^\s*(技术方案草案|技术方案|方案草案)[:：]\s*", "", text.strip())


def append_design_summary(existing: str, addition: str) -> str:
    clean_existing = existing.strip()
    clean_addition = addition.strip()
    if not clean_addition:
        return clean_existing
    if not clean_existing:
        return clean_addition
    if clean_addition in clean_existing:
        return clean_existing
    return f"{clean_existing}\n\n补充记录：\n{clean_addition}"


def best_readable_cut(content: str, max_length: int) -> int:
    if len(content) <= max_length:
        return len(content)
    candidates = [content.rfind(mark, 0, max_length + 1) for mark in ["。", "；", ";", "，", ",", "、", " "]]
    cut = max(candidates)
    if cut >= max_length // 2:
        return cut + 1
    return len(content)


def split_readable_sentence_lines(
    text: str,
    *,
    max_length: int = 64,
    prefix: str = "",
    continuation_prefix: str | None = None,
) -> list[str]:
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []

    parts = [part for part in re.findall(r"[^。；;！？!?，,、]+[。；;！？!?，,、]?", clean) if part.strip()]
    if not parts:
        parts = [clean]

    content_lines: list[str] = []
    current = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if current and len(current + part) > max_length:
            content_lines.append(current.strip())
            current = part
        else:
            current += part
    if current:
        content_lines.append(current.strip())

    chunks: list[str] = []
    for line in content_lines:
        content = line
        while content:
            cut = best_readable_cut(content, max_length)
            chunk = content[:cut].strip()
            if chunk:
                chunks.append(chunk)
            content = content[cut:].strip()

    wrapped: list[str] = []
    follow_prefix = continuation_prefix if continuation_prefix is not None else prefix
    for index, chunk in enumerate(chunks):
        wrapped.append((prefix if index == 0 else follow_prefix) + chunk)
    return wrapped


def format_markdown_content_lines(text: str, *, max_length: int = 64) -> list[str]:
    clean = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not clean:
        return ["无"]

    lines: list[str] = []
    in_code_block = False
    for raw_line in clean.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue

        if stripped.startswith("```"):
            lines.append(stripped)
            in_code_block = not in_code_block
            continue

        if in_code_block or stripped.startswith(("#", "|")):
            lines.append(stripped)
            continue

        prefix = ""
        content = stripped
        list_match = re.match(r"^([-*+]\s+|\d+[.)]\s+)(.+)$", stripped)
        if list_match:
            prefix = list_match.group(1)
            content = list_match.group(2).strip()

        if len(stripped) <= max_length:
            lines.append(stripped)
            continue
        continuation_prefix = " " * len(prefix) if prefix else ""
        lines.extend(
            split_readable_sentence_lines(
                content,
                max_length=max_length,
                prefix=prefix,
                continuation_prefix=continuation_prefix,
            )
        )

    while lines and lines[-1] == "":
        lines.pop()
    return lines or ["无"]


def format_design_summary_lines(summary: str) -> list[str]:
    raw_text = strip_design_prefix(summary).replace("\r\n", "\n").replace("\r", "\n").strip()
    supplement_marker = "\n\n补充记录：\n"
    if supplement_marker in raw_text:
        sections = raw_text.split(supplement_marker)
        lines = format_design_summary_lines(sections[0])
        for section in sections[1:]:
            if lines and lines[-1]:
                lines.append("")
            lines.extend(["#### 补充记录", ""])
            lines.extend(format_design_summary_lines(section))
        return lines

    text = re.sub(r"\s+", " ", raw_text).strip()
    if not text:
        return ["- 暂无技术方案内容"]

    section_pattern = re.compile(r"([一二三四五六七八九十]+)、([^。；;]+)[。；;]")
    matches = list(section_pattern.finditer(text))
    if not matches:
        return split_readable_sentence_lines(text)

    lines: list[str] = []
    if matches[0].start() > 0:
        lines.extend(split_readable_sentence_lines(text[: matches[0].start()]))
        lines.append("")

    for index, match in enumerate(matches):
        title = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        lines.append(f"#### {title}")
        lines.append("")
        bullet_body = re.sub(r"^(\d{1,2})\.\s*", "- ", body)
        bullet_body = re.sub(r"([。；;])\s*(\d{1,2})\.\s*", r"\1\n- ", bullet_body)
        body_lines = [line.strip() for line in bullet_body.splitlines() if line.strip()]
        if not body_lines:
            lines.append("- 暂无内容")
        for body_line in body_lines:
            if body_line.startswith("- "):
                lines.extend(split_readable_sentence_lines(body_line[2:], prefix="- ", continuation_prefix="  "))
            else:
                lines.extend(split_readable_sentence_lines(body_line))
        lines.append("")

    while lines and not lines[-1]:
        lines.pop()
    return lines


def requirement_design_file_path(requirement: dict[str, Any]) -> str:
    return f".codex-sdlc/requirements/{requirement['folder_name']}/design.md"


def unique_extend(items: list[str], new_items: list[str]) -> list[str]:
    seen = set(items)
    result = list(items)
    for item in new_items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def string_list_value(value: Any) -> list[str]:
    return [str(item).strip() for item in list_value(value) if str(item).strip()]


def extend_unique_strings(items: list[str], additions: list[str]) -> list[str]:
    # 草稿里的问题、决定和审查项要按事件顺序保留，但重复内容没有必要反复写到人读文件里。
    result = list(items)
    seen = set(result)
    for item in additions:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def new_draft_from_event(
    payload: dict[str, Any],
    event: dict[str, Any],
    event_index: int,
    *,
    structured_contract: bool = False,
) -> dict[str, Any]:
    draft_id = str(payload["draft_id"])
    draft = {
        "draft_id": draft_id,
        "status": str(payload.get("status") or "discussing"),
        "title": str(payload.get("title") or draft_id),
        "requirement_summary": str(payload.get("requirement_summary") or ""),
        "design_summary": str(payload.get("design_summary") or ""),
        "requirement_body": str(payload.get("requirement_body") or ""),
        "design_body": str(payload.get("design_body") or ""),
        "questions": string_list_value(payload.get("questions", [])),
        "decisions": string_list_value(payload.get("decisions", [])),
        "review_items": string_list_value(payload.get("review_items", [])),
        # 新事件会显式登记文件级派生产物；旧事件保持空列表，读取时不会伪造新业务对象。
        "artifact_records": [],
        # 资料清单同样只由 draft_material_added 事件开启；读取旧 DRAFT 时不能凭空生成新合同。
        "materials": [],
        "_material_manifest_enabled": False,
        # DES 只由结构化引用事件开启；旧 DRAFT 不会因为刷新而凭空生成技术方案引用。
        "design_references": [],
        "_design_reference_enabled": False,
        # 计划、模块和总体说明都只由专用事件开启，历史 DRAFT 不会凭空生成设计对象。
        "_design_plan_enabled": False,
        "design_artifacts": [],
        "_design_artifact_enabled": False,
        "design_summaries": [],
        "design_summary_invalidation": {
            "stale_modules": [],
            "current_modules": [],
            "review_targets": [],
        },
        "_design_summary_enabled": False,
        "_structured_stage_enabled": False,
        "started_requirement_id": str(payload.get("started_requirement_id") or ""),
        "created_at": event["created_at"],
        "updated_at": event["created_at"],
        "_created_seq": event_index,
        "_updated_seq": event_index,
        "folder_path": f".codex-sdlc/drafts/{draft_id}",
    }
    if structured_contract:
        # 只有新 draft_mutated 创建事件才预留结构化集合；旧 draft_created 不补造新合同对象。
        draft["structured_captures"] = []
        draft["decision_records"] = []
        draft["capture_transitions"] = []
        draft["capture_statuses"] = {}
    return draft


def touch_draft(draft: dict[str, Any], event: dict[str, Any], event_index: int) -> None:
    # 事件流是唯一真相源，更新时间用事件顺序推进，后续状态列表可以直接判断当前稿是否最新。
    draft["updated_at"] = event["created_at"]
    draft["_updated_seq"] = event_index


def _verify_embedded_record_hash(record: dict[str, Any], hash_field: str, label: str) -> None:
    declared = str(record.get(hash_field) or "")
    content = {key: deepcopy(value) for key, value in record.items() if key != hash_field}
    if not declared or declared != canonical_sha256(content):
        raise SdlcError(f"{label}的 {hash_field} 与事件内容不一致。")


def _append_unique_structured_record(
    records: list[dict[str, Any]], record: dict[str, Any], *, id_field: str, label: str
) -> None:
    record_id = str(record.get(id_field) or "")
    if not record_id:
        raise SdlcError(f"{label}缺少 {id_field}。")
    existing = next((item for item in records if item.get(id_field) == record_id), None)
    if existing is None:
        records.append(deepcopy(record))
        records.sort(key=lambda item: str(item.get(id_field) or ""))
        return
    if canonical_sha256(existing) != canonical_sha256(record):
        raise SdlcError(f"{label}编号重复且内容冲突：{record_id}。")


def _apply_structured_capture(draft: dict[str, Any], capture_payload: dict[str, Any]) -> None:
    capture_record = capture_payload.get("structured_increment")
    decision_records = capture_payload.get("decision_records")
    if capture_record is None and decision_records is None:
        return
    if not isinstance(capture_record, dict) or not isinstance(decision_records, list):
        raise SdlcError("结构化 CAP 事件缺少完整的增量或决定记录。")
    if capture_record.get("draft_id") != draft.get("draft_id"):
        raise SdlcError("结构化 CAP 的 draft_id 与事件目标不一致。")
    _verify_embedded_record_hash(capture_record, "record_sha256", "结构化 CAP")
    if capture_record.get("schema_version") != "capture-increment.v1":
        raise SdlcError("结构化 CAP 的 schema_version 不受支持。")
    if capture_record.get("status") != "pending":
        raise SdlcError("结构化 CAP 初始记录只能是 pending，状态变化必须使用独立转换事件。")
    if capture_record.get("increment_sha256") != canonical_sha256(
        capture_record.get("increment")
    ):
        raise SdlcError("结构化 CAP 的增量哈希与内容不一致。")
    source_reference = capture_record.get("source_reference")
    if not isinstance(source_reference, dict) or capture_record.get(
        "source_sha256"
    ) != source_reference.get("sha256"):
        raise SdlcError("结构化 CAP 的来源哈希与精确引用不一致。")
    for payload_field, record_field in (
        ("capture_id", "capture_id"),
        ("draft_id", "draft_id"),
        ("status", "status"),
        ("submission_key", "submission_key"),
        ("submission_sha256", "submission_sha256"),
    ):
        if capture_payload.get(payload_field) != capture_record.get(record_field):
            raise SdlcError(f"结构化 CAP 的 {payload_field} 与初始记录不一致。")
    if capture_payload.get("summary") != capture_record.get("increment"):
        raise SdlcError("结构化 CAP 的全局摘要与初始增量不一致。")
    captures = _mapping_records(draft.get("structured_captures"))
    _append_unique_structured_record(
        captures, capture_record, id_field="capture_id", label="结构化 CAP"
    )
    decisions = _mapping_records(draft.get("decision_records"))
    decision_ids = [
        str(item.get("decision_id") or "")
        for item in decision_records
        if isinstance(item, dict)
    ]
    if decision_ids != capture_record.get("decision_ids"):
        raise SdlcError("结构化 CAP 的 decision_ids 与事件决定记录不一致。")
    for decision in decision_records:
        if not isinstance(decision, dict):
            raise SdlcError("结构化 DEC 记录必须是 JSON 对象。")
        _verify_embedded_record_hash(decision, "decision_sha256", "结构化 DEC")
        if decision.get("source_capture_id") != capture_record.get("capture_id"):
            raise SdlcError("结构化 DEC 的来源 CAP 与当前事件不一致。")
        declared_identity = str(decision.get("decision_identity_sha256") or "")
        if declared_identity != draft_lifecycle.decision_identity_sha256(decision):
            raise SdlcError("结构化 DEC 的决定身份与问题定位不一致。")
        question = decision.get("question")
        question_capture = next(
            (
                item
                for item in captures
                if isinstance(question, dict)
                and item.get("capture_id") == question.get("capture_ref")
            ),
            None,
        )
        if question_capture is None or not draft_lifecycle.decision_matches_question_capture(
            decision, question_capture
        ):
            raise SdlcError("结构化 DEC 没有精确引用问题 CAP 的来源定位。")
        _append_unique_structured_record(
            decisions, decision, id_field="decision_id", label="结构化 DEC"
        )
    draft["structured_captures"] = captures
    draft["decision_records"] = decisions
    statuses = deepcopy(draft.get("capture_statuses"))
    if not isinstance(statuses, dict):
        statuses = {}
    capture_id = str(capture_record.get("capture_id") or "")
    existing_status = statuses.get(capture_id)
    if existing_status not in {None, "pending"}:
        raise SdlcError(f"{capture_id} 的初始记录与已转换状态冲突。")
    statuses[capture_id] = "pending"
    draft["capture_statuses"] = statuses
    draft.setdefault("capture_transitions", [])
    draft["_structured_stage_enabled"] = True


def _apply_structured_capture_transition(
    paths: ProjectPaths,
    draft: dict[str, Any],
    captures: list[dict[str, Any]],
    drafts: Mapping[str, dict[str, Any]],
    requirements: Mapping[str, dict[str, Any]],
    transition: dict[str, Any],
    transition_submission: dict[str, Any],
) -> None:
    """重放独立转换事件，并交叉核对初始记录、前置状态和全局事实。"""

    required = {
        "schema_version",
        "transition_key",
        "transition_submission_sha256",
        "draft_id",
        "capture_id",
        "source_submission_key",
        "source_submission_sha256",
        "source_record_sha256",
        "from_status",
        "to_status",
        "previous_transition_sha256",
        "relation",
        "transition_sha256",
    }
    if set(transition) != required or transition.get("schema_version") != "capture-transition.v1":
        raise SdlcError("CAP 状态转换事件结构不完整或版本不受支持。")
    _verify_embedded_record_hash(
        transition, "transition_sha256", "CAP 状态转换"
    )
    submission_fields = required - {
        "transition_submission_sha256",
        "previous_transition_sha256",
        "transition_sha256",
    }
    if set(transition_submission) != submission_fields:
        raise SdlcError("CAP 状态转换事件缺少独立保存的原始 submission。")
    if transition.get("transition_submission_sha256") != canonical_sha256(
        transition_submission
    ):
        raise SdlcError("CAP 状态转换的原始 submission 锚点与记录不一致。")
    relation_submission = transition_submission.get("relation")
    relation = transition.get("relation")
    # 提交入口允许编号大小写和首尾空格，但完整 reference 不做语义归一化；这里复用同一规则逐字段交叉核对。
    anchored_fields = {
        "schema_version": transition_submission.get("schema_version"),
        "transition_key": str(transition_submission.get("transition_key") or "").strip(),
        "draft_id": str(transition_submission.get("draft_id") or "").strip().upper(),
        "capture_id": str(transition_submission.get("capture_id") or "").strip().upper(),
        "source_submission_key": str(
            transition_submission.get("source_submission_key") or ""
        ).strip(),
        "source_submission_sha256": str(
            transition_submission.get("source_submission_sha256") or ""
        ).strip(),
        "source_record_sha256": str(
            transition_submission.get("source_record_sha256") or ""
        ).strip(),
        "from_status": str(transition_submission.get("from_status") or "").strip(),
        "to_status": str(transition_submission.get("to_status") or "").strip(),
    }
    if any(transition.get(key) != value for key, value in anchored_fields.items()):
        raise SdlcError("CAP 状态转换记录与独立原始 submission 不一致。")
    if (
        not isinstance(relation_submission, dict)
        or set(relation_submission) != {"kind", "target_id", "reference"}
        or not isinstance(relation, dict)
        or relation.get("kind")
        != str(relation_submission.get("kind") or "").strip()
        or relation.get("target_id")
        != str(relation_submission.get("target_id") or "").strip().upper()
        or canonical_sha256(relation.get("reference"))
        != canonical_sha256(relation_submission.get("reference"))
    ):
        raise SdlcError("CAP 状态转换关系与独立原始 submission 不一致。")
    capture_id = str(transition.get("capture_id") or "")
    initial_records = [
        item
        for item in _mapping_records(draft.get("structured_captures"))
        if item.get("capture_id") == capture_id
    ]
    global_records = [item for item in captures if item.get("capture_id") == capture_id]
    if len(initial_records) != 1 or len(global_records) != 1:
        raise SdlcError(f"{capture_id} 的全局记录和 DRAFT 初始记录不唯一。")
    initial = initial_records[0]
    global_record = global_records[0]
    if transition.get("draft_id") != draft.get("draft_id"):
        raise SdlcError(f"{capture_id} 的转换目标 DRAFT 不一致。")
    for transition_field, capture_field in (
        ("source_submission_key", "submission_key"),
        ("source_submission_sha256", "submission_sha256"),
        ("source_record_sha256", "record_sha256"),
    ):
        if transition.get(transition_field) != initial.get(capture_field):
            raise SdlcError(f"{capture_id} 的 {transition_field} 与初始记录不一致。")
    if canonical_sha256(global_record.get("structured_increment")) != canonical_sha256(initial):
        raise SdlcError(f"{capture_id} 的全局初始记录与 DRAFT 初始记录不一致。")
    existing_transitions = _mapping_records(draft.get("capture_transitions"))
    if transition.get("previous_transition_sha256") != "" or any(
        item.get("capture_id") == capture_id for item in existing_transitions
    ):
        raise SdlcError(f"{capture_id} 的转换链不连续或出现两种状态。")
    statuses = deepcopy(draft.get("capture_statuses"))
    if not isinstance(statuses, dict):
        statuses = {}
    if (
        initial.get("status") != "pending"
        or transition.get("from_status") != "pending"
        or statuses.get(capture_id) != "pending"
        or global_record.get("status") != "pending"
    ):
        raise SdlcError(f"{capture_id} 的转换前置状态不一致。")
    to_status = str(transition.get("to_status") or "")
    # 历史事件已经由原始 submission、规范记录和完整哈希相互锚定。这里仍复用服务层
    # 的编号、关系类型、路径和目标绑定校验，但不重新读取可能已合法更新的当前需求投影；
    # capture-transition 写入入口没有传 historical_replay，仍会严格读取真实文件核对哈希。
    from codex_sdlc.services.discuss_service import (
        capture_transition_known_ids,
        capture_transition_target_bindings,
        validate_capture_transition_relation,
    )

    target_state = {
        "drafts": drafts,
        "captures": captures,
        "requirements": requirements,
    }
    validated_relation = validate_capture_transition_relation(
        paths.root,
        relation,
        to_status=to_status,
        capture_id=capture_id,
        known_ids=capture_transition_known_ids(target_state),
        target_bindings=capture_transition_target_bindings(target_state),
        historical_replay=True,
    )
    if canonical_sha256(validated_relation) != canonical_sha256(relation):
        raise SdlcError(f"{capture_id} 的转换关系不是规范记录。")
    existing_transitions.append(deepcopy(transition))
    statuses[capture_id] = to_status
    draft["capture_transitions"] = existing_transitions
    draft["capture_statuses"] = statuses
    global_record["initial_status"] = "pending"
    global_record["status"] = to_status
    global_record["capture_transition"] = deepcopy(transition)


def _mapping_records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _is_historical_requirement_projection_reference(
    paths: ProjectPaths,
    draft_id: str,
    reference: object,
) -> bool:
    """识别事件写入后允许被下一版需求原子包覆盖的当前投影。"""

    if not isinstance(reference, dict):
        return False
    try:
        # 即使不核对旧文件哈希，也必须保留 Schema、相对路径和项目边界检查。
        validate_schema_document(
            reference,
            schema_name=reference_locator.REFERENCE_LOCATOR_SCHEMA,
        )
        target = resolve_project_path(paths.root, str(reference.get("path") or ""))
    except SdlcError:
        return False
    requirement_dir = paths.draft_requirements_dir(draft_id)
    return target in {
        requirement_dir / "requirement-split.v1.json",
        requirement_dir / "requirement-coverage.v1.json",
    }


def _draft_reference_issues(paths: ProjectPaths, draft: dict[str, Any]) -> list[dict[str, str]]:
    """重放时复用 T-001 定位器核对漂移，只返回结构化阻断项。"""

    draft_id = str(draft.get("draft_id") or "")
    references: list[tuple[str, str, object, bool]] = []
    for capture in _mapping_records(draft.get("structured_captures")):
        capture_id = str(capture.get("capture_id") or "CAP")
        references.append(
            (capture_id, "source", capture.get("source_reference"), False)
        )
        targets = capture.get("targets")
        if isinstance(targets, list):
            for index, target in enumerate(targets):
                if isinstance(target, dict):
                    references.append(
                        (
                            capture_id,
                            f"target[{index}]",
                            target.get("reference"),
                            True,
                        )
                    )
    for decision in _mapping_records(draft.get("decision_records")):
        decision_id = str(decision.get("decision_id") or "DEC")
        references.append(
            (decision_id, "source", decision.get("source_reference"), False)
        )
        question = decision.get("question")
        if isinstance(question, dict):
            references.append(
                (decision_id, "question", question.get("reference"), False)
            )
        scope = decision.get("scope")
        if isinstance(scope, list):
            for index, target in enumerate(scope):
                if isinstance(target, dict):
                    references.append(
                        (
                            decision_id,
                            f"scope[{index}]",
                            target.get("reference"),
                            True,
                        )
                    )
    for transition in _mapping_records(draft.get("capture_transitions")):
        capture_id = str(transition.get("capture_id") or "CAP")
        relation = transition.get("relation")
        if isinstance(relation, dict):
            references.append(
                (capture_id, "transition", relation.get("reference"), True)
            )
    issues: list[dict[str, str]] = []
    for source_id, role, reference, historical_target in references:
        if not isinstance(reference, dict):
            issues.append(
                {"source_id": source_id, "status": "invalid", "reference": role}
            )
            continue
        if historical_target and _is_historical_requirement_projection_reference(
            paths,
            draft_id,
            reference,
        ):
            # CAP/DEC 的目标关系是写入时已经严格验真的历史证据；当前 split/coverage
            # 被合法覆盖后只让审核和确认 stale，不能把旧哈希再变成永久阻断项。
            continue
        try:
            reference_locator.validate_reference(paths.root, reference)
        except SdlcError:
            issues.append(
                {"source_id": source_id, "status": "drifted", "reference": role}
            )
    return issues


def _draft_material_integrity_issues(
    paths: ProjectPaths, draft: dict[str, Any]
) -> list[dict[str, str]]:
    """只读核对归档字节和外部版本证据，结果直接进入统一状态计算。"""

    draft_id = str(draft.get("draft_id") or "")
    draft_dir = paths.draft_dir(draft_id)
    original_dir = paths.draft_original_materials_dir(draft_id)
    issues: list[dict[str, str]] = []
    try:
        draft_root = draft_dir.resolve(strict=True)
        original_root = original_dir.resolve(strict=True)
        original_root.relative_to(draft_root)
        if original_dir.is_symlink():
            raise ValueError("symlink")
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        draft_root = None
        original_root = None

    materials = [
        item for item in draft.get("materials", []) if isinstance(item, dict)
    ]
    materials_by_id = {
        str(item.get("material_id") or ""): item
        for item in materials
        if str(item.get("material_id") or "")
    }
    for material in materials:
        material_id = str(material.get("material_id") or "MAT")
        source_kind = material.get("source_kind")
        if source_kind == "file":
            relative = Path(str(material.get("stored_path") or ""))
            if (
                draft_root is None
                or original_root is None
                or relative.is_absolute()
                or ".." in relative.parts
                or not relative.parts
                or relative.parts[0] != "原始资料"
            ):
                issues.append(
                    {"source_id": material_id, "status": "unsafe_path", "reference": str(relative)}
                )
                continue
            target = draft_dir / relative
            try:
                target.resolve(strict=True).relative_to(original_root)
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                issues.append(
                    {"source_id": material_id, "status": "missing", "reference": str(relative)}
                )
                continue
            if target.is_symlink() or not target.is_file():
                issues.append(
                    {"source_id": material_id, "status": "unsafe_file", "reference": str(relative)}
                )
                continue
            if sha256_file(target) != str(material.get("sha256") or ""):
                issues.append(
                    {"source_id": material_id, "status": "hash_drift", "reference": str(relative)}
                )
            continue
        if source_kind != "external-reference":
            continue
        evidence = material.get("version_evidence")
        url = str(material.get("url") or "")
        try:
            expected_url_hash = external_version.normalized_url_sha256(url)
            validate_schema_document(
                evidence, schema_name=external_version.EXTERNAL_VERSION_SCHEMA
            )
        except (SdlcError, TypeError, ValueError):
            issues.append(
                {"source_id": material_id, "status": "invalid_evidence", "reference": "version_evidence"}
            )
            continue
        evidence_hash = evidence.get("normalized_url_sha256") if isinstance(evidence, dict) else ""
        if (
            material.get("normalized_url_sha256") != expected_url_hash
            or evidence_hash != expected_url_hash
            or material.get("status") != evidence.get("status")
        ):
            issues.append(
                {"source_id": material_id, "status": "evidence_drift", "reference": "version_evidence"}
            )
            continue
        detail = evidence.get("evidence") if isinstance(evidence, dict) else None
        if isinstance(detail, dict) and detail.get("kind") == "local_snapshot":
            snapshot_id = str(detail.get("material_id") or "")
            snapshot = materials_by_id.get(snapshot_id)
            if (
                snapshot is None
                or snapshot.get("source_kind") != "file"
                or snapshot.get("sha256") != detail.get("sha256")
            ):
                issues.append(
                    {"source_id": material_id, "status": "snapshot_drift", "reference": snapshot_id}
                )
    return issues


def _draft_design_stage(
    paths: ProjectPaths,
    draft: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, object] | None]:
    """把设计事件归并成状态输入，Markdown 和展示名称不会进入这条链路。"""

    from codex_sdlc.core.design_artifact_contract import (
        ENABLED_PLAN_STATUSES,
        design_artifact_output_path,
        design_artifact_records,
    )
    from codex_sdlc.core.design_plan_contract import (
        assess_design_plan,
        design_plan_records,
    )
    from codex_sdlc.core.design_summary_contract import (
        design_summary_records,
        expected_design_summary_input_hashes,
    )

    draft_id = str(draft.get("draft_id") or "")
    blockers: list[dict[str, str]] = []
    plan_records = design_plan_records(
        paths,
        draft_id=draft_id,
        events=events,
    )
    if not plan_records:
        blockers.append(
            {
                "code": "design_plan_missing",
                "source_id": draft_id,
                "status": "missing",
                "reference": "",
            }
        )
        return (
            {
                "schema_version": "draft-design-stage.v1",
                "plan_status": "missing",
                "confirmed_designs": sorted(
                    str(item.get("design_id") or "")
                    for item in draft.get("design_references", [])
                    if isinstance(item, dict)
                    and item.get("status") == "confirmed"
                ),
                "enabled_modules": [],
                "completed_modules": [],
                "pending_modules": [],
                "stale_modules": [],
                "blocked_modules": [],
                "not_applicable_modules": [],
                "module_states": [],
                "summary_required": False,
                "summary_status": "waiting_for_plan",
                "ready_for_review": False,
                "blockers": blockers,
            },
            None,
        )
    if len(plan_records) != 1:
        # design_plan_records 已拒绝同一 DRAFT 的冲突计划；这里保留显式防线，
        # 避免未来读取逻辑变化后静默选择其中一份。
        raise SdlcError(f"{draft_id} 缺少唯一有效的开发设计总计划。")
    plan = plan_records[0]
    plan_assessment = assess_design_plan(paths, plan)
    plan_status = str(plan_assessment["status"])
    if plan_status != "current":
        blockers.append(
            {
                "code": "design_plan_stale",
                "source_id": draft_id,
                "status": plan_status,
                "reference": ",".join(
                    str(item) for item in plan_assessment["changed_paths"]
                ),
            }
        )

    modules = [
        item for item in plan["modules"] if isinstance(item, dict)
    ]
    module_by_id = {
        str(item["module_id"]): item
        for item in modules
    }
    enabled_modules = sorted(
        module_id
        for module_id, module in module_by_id.items()
        if module["status"] in ENABLED_PLAN_STATUSES
    )
    blocked_modules = sorted(
        module_id
        for module_id, module in module_by_id.items()
        if module["status"] == "blocked"
    )
    not_applicable_modules = sorted(
        module_id
        for module_id, module in module_by_id.items()
        if module["status"] == "not_applicable"
    )
    for module_id in blocked_modules:
        module = module_by_id[module_id]
        blockers.append(
            {
                "code": "design_module_blocked",
                "source_id": module_id,
                "status": "blocked",
                "reference": ",".join(
                    str(item) for item in module["blocked_by"]
                ),
            }
        )

    current_artifacts = {
        str(item["artifact_id"]): item
        for item in design_artifact_records(
            paths,
            draft_id=draft_id,
            events=events,
        )
    }
    completed_modules: list[str] = []
    pending_modules: list[str] = []
    stale_modules: list[str] = []
    module_states: list[dict[str, object]] = []
    for module_id in sorted(module_by_id):
        module = module_by_id[module_id]
        module_status = str(module["status"])
        artifact = current_artifacts.get(module_id)
        artifact_status = (
            "blocked"
            if module_status == "blocked"
            else "not_applicable"
            if module_status == "not_applicable"
            else "pending"
        )
        output_path = ""
        if module_status in ENABLED_PLAN_STATUSES:
            output_path = design_artifact_output_path(module)
            if artifact is None:
                pending_modules.append(module_id)
            elif (
                artifact.get("plan_sha256") != plan["plan_sha256"]
                or artifact.get("plan_module_sha256")
                != canonical_sha256(module)
                or artifact.get("plan_status") != module_status
                or artifact.get("output_path") != output_path
            ):
                stale_modules.append(module_id)
                artifact_status = "stale"
            else:
                completed_modules.append(module_id)
                artifact_status = "completed"
        module_states.append(
            {
                "module_id": module_id,
                "type": str(module["type"]),
                "plan_status": module_status,
                "artifact_status": artifact_status,
                "output_path": output_path,
            }
        )
    blockers.extend(
        {
            "code": "design_artifact_missing",
            "source_id": module_id,
            "status": "missing",
            "reference": "",
        }
        for module_id in pending_modules
    )
    blockers.extend(
        {
            "code": "design_artifact_stale",
            "source_id": module_id,
            "status": "stale",
            "reference": "",
        }
        for module_id in stale_modules
    )

    summaries = design_summary_records(
        paths,
        draft_id=draft_id,
        events=events,
    )
    latest_summary = summaries[0] if summaries else None
    # 单个或零个启用模块没有跨模块共同对象，不能强迫创建空总体说明；
    # 两个及以上启用模块才需要用 design-summary 明确共同关系。
    summary_required = len(enabled_modules) >= 2
    summary_status = "not_required" if not summary_required else "missing"
    modules_ready = not (
        blocked_modules or pending_modules or stale_modules
    )
    if latest_summary is not None:
        expected_summary_hashes = expected_design_summary_input_hashes(
            plan,
            current_artifacts.values(),
        )
        summary_status = (
            "current"
            if latest_summary["input_hashes"] == expected_summary_hashes
            else "stale"
        )
    if (
        summary_required
        and plan_status == "current"
        and modules_ready
        and summary_status != "current"
    ):
        blockers.append(
            {
                "code": (
                    "design_summary_stale"
                    if summary_status == "stale"
                    else "design_summary_missing"
                ),
                "source_id": draft_id,
                "status": summary_status,
                "reference": "",
            }
        )

    ready_for_review = bool(
        plan_status == "current"
        and modules_ready
        and (
            summary_status == "current"
            or summary_status == "not_required"
        )
        and not blockers
    )
    return (
        {
            "schema_version": "draft-design-stage.v1",
            "plan_status": plan_status,
            "plan_sha256": str(plan["plan_sha256"]),
            "confirmed_designs": sorted(
                str(item.get("design_id") or "")
                for item in draft.get("design_references", [])
                if isinstance(item, dict)
                and item.get("status") == "confirmed"
            ),
            "enabled_modules": enabled_modules,
            "completed_modules": completed_modules,
            "pending_modules": pending_modules,
            "stale_modules": stale_modules,
            "blocked_modules": blocked_modules,
            "not_applicable_modules": not_applicable_modules,
            "module_states": module_states,
            "summary_required": summary_required,
            "summary_status": summary_status,
            "ready_for_review": ready_for_review,
            "blockers": blockers,
        },
        plan,
    )


_DRAFT_REQUIREMENTS_PACKAGE_PATTERN = re.compile(
    r"^draft-requirements:(?P<draft_id>DRAFT-[0-9]{3,}):(?P<content_key>[0-9a-f]{64})$"
)


def _structured_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _structured_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _structured_strings(item)


def _requirement_client_prefixes(
    split_document: dict[str, Any], coverage_document: dict[str, Any]
) -> dict[str, str]:
    prefixes: dict[str, str] = {}

    def add(item: Any, prefix: str) -> None:
        if not isinstance(item, dict):
            raise SdlcError("需求导入包包含无效对象，不能重建投影。")
        client_key = str(item.get("client_key") or "")
        if not client_key or client_key in prefixes:
            raise SdlcError(f"需求导入包 client_key 缺失或重复：{client_key}。")
        prefixes[client_key] = prefix

    for item in split_document.get("global_rules", []):
        add(item, "GR")
    for item in split_document.get("functional_requirements", []):
        add(item, "FR")
        if isinstance(item, dict):
            for criterion in item.get("acceptance_criteria", []):
                add(criterion, "AC")
    for item in coverage_document.get("units", []):
        add(item, "SRC")
    return prefixes


def _draft_requirement_import_from_event(
    paths: ProjectPaths, event: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """只识别固定技术键和目录，不从事件摘要或 Markdown 标题猜测包类型。"""

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    package_key = str(payload.get("package_key") or "")
    match = _DRAFT_REQUIREMENTS_PACKAGE_PATTERN.fullmatch(package_key)
    if match is None:
        return None
    # 状态层不复制 T-002 的登记、回执、整包哈希和事件一致性算法；这里按 package_key
    # 找到原登记，再调用同一验证入口，任何源包漂移都会在生成阅读投影前被拦住。
    from codex_sdlc.core.atomic_import import _verify_registration, load_import_registry

    registry = load_import_registry(paths)
    registrations = [
        item
        for item in registry.get("packages", [])
        if isinstance(item, dict) and item.get("package_key") == package_key
    ]
    if len(registrations) != 1:
        raise SdlcError(f"需求导入事件缺少唯一原子登记：{package_key}。")
    _verify_registration(paths, registrations[0], events=load_events(paths))
    draft_id = match.group("draft_id")
    content_key = match.group("content_key")
    destination = str(payload.get("destination") or "")
    expected_destination = (
        f".codex-sdlc/drafts/{draft_id}/需求/requirements-{content_key}"
    )
    if destination != expected_destination:
        raise SdlcError(f"需求导入包目标目录与 package_key 不一致：{package_key}。")
    expected_files = {
        f"{destination}/requirement-split.v1.json",
        f"{destination}/requirement-coverage.v1.json",
    }
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or set(raw_files) != expected_files:
        raise SdlcError(f"需求导入事件文件清单不完整：{package_key}。")

    split_path = resolve_project_path(
        paths.root, f"{destination}/requirement-split.v1.json", must_exist=True
    )
    coverage_path = resolve_project_path(
        paths.root, f"{destination}/requirement-coverage.v1.json", must_exist=True
    )
    split_document = read_requirement_document(
        split_path, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    coverage_document = read_requirement_document(
        coverage_path, schema_name=REQUIREMENT_COVERAGE_SCHEMA
    )
    if split_document.get("draft_id") != draft_id or coverage_document.get("draft_id") != draft_id:
        raise SdlcError(f"需求导入包中的 draft_id 与目标目录不一致：{package_key}。")
    actual_split_sha256 = contract_sha256(
        split_document, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    declared_split_sha256 = coverage_document.get("requirement_split_sha256")
    if not isinstance(declared_split_sha256, str) or not hmac.compare_digest(
        actual_split_sha256, declared_split_sha256
    ):
        raise SdlcError(
            f"需求导入包的正式拆分哈希与覆盖声明不一致：{package_key}。"
        )
    if any(
        "@client:" in value
        for value in _structured_strings(
            {"split": split_document, "coverage": coverage_document}
        )
    ):
        raise SdlcError(f"需求导入包仍含未重写的 @client: 引用：{package_key}。")

    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        raise SdlcError(f"需求导入事件缺少编号映射：{package_key}。")
    client_prefixes = _requirement_client_prefixes(split_document, coverage_document)
    if set(mapping) != set(client_prefixes):
        raise SdlcError(f"需求导入事件编号映射与包内对象不一致：{package_key}。")
    formal_ids: set[str] = set()
    for client_key, prefix in client_prefixes.items():
        formal_id = mapping.get(client_key)
        if not isinstance(formal_id, str) or re.fullmatch(
            rf"{re.escape(prefix)}-[0-9]{{3,}}", formal_id
        ) is None:
            raise SdlcError(f"需求导入事件编号类型不正确：{client_key}。")
        if formal_id in formal_ids:
            raise SdlcError(f"需求导入事件正式编号重复：{formal_id}。")
        formal_ids.add(formal_id)

    review_blockers = [
        f"open_questions[{index}]"
        for index, _ in enumerate(split_document.get("open_questions", []))
    ]
    review_blockers.extend(
        f"{item.get('client_key')}:{item.get('status')}"
        for item in coverage_document.get("units", [])
        if isinstance(item, dict)
        and item.get("status") in {"needs_user", "needs_material"}
    )
    import_record = {
        "schema": "draft-requirement-import-receipt.v1",
        "package_key": package_key,
        "package_sha256": str(payload.get("package_sha256") or ""),
        "mapping": {str(key): str(value) for key, value in mapping.items()},
        "destination": destination,
        "files": sorted(expected_files),
        "event_id": str(event.get("event_id") or ""),
        "imported_at": str(event.get("created_at") or ""),
        "producer_run_id": str(split_document.get("producer_run_id") or ""),
        "review_blockers": review_blockers,
    }
    return draft_id, split_document, coverage_document, import_record


def structured_version_number(requirement: dict[str, Any]) -> int:
    effective_statuses = {"effective", "planned", "resolved", "verified"}
    effective_changes = [
        change
        for change in requirement.get("changes", [])
        if change.get("status") in effective_statuses
    ]
    explicit_target = int(requirement.get("_formal_target_version") or 0)
    return max(1, explicit_target, 1 + len(effective_changes))


def split_requirement_point_text(description: str) -> list[str]:
    """兼容入口只把完整字段作为一个需求点，不拆解自然语言。"""

    clean = str(description).strip()
    return [clean] if clean else []


def is_imported_history_requirement(requirement: dict[str, Any]) -> bool:
    return False


def strip_markdown_noise(text: str) -> str:
    return str(text).strip()


def sanitize_runtime_text(text: str) -> str:
    return " ".join(str(text).split()).strip()


def current_runtime_text(requirement: dict[str, Any], text: object) -> str:
    clean = str(text or "").strip()
    if is_imported_history_requirement(requirement):
        return sanitize_runtime_text(clean)
    return clean


def current_runtime_value(requirement: dict[str, Any], value: Any) -> Any:
    if isinstance(value, str):
        return current_runtime_text(requirement, value)
    if isinstance(value, list):
        return [current_runtime_value(requirement, item) for item in value]
    if isinstance(value, dict):
        return {key: current_runtime_value(requirement, item) for key, item in value.items()}
    return value


def task_acceptance_feedback_lines(task: dict[str, Any]) -> list[str]:
    """交接只展示当前有效的结构化反馈，不再借用旧执行包模块。"""

    if task.get("feedback_contract_version") != "feedback.v1" or task.get("feedback_state") != "structured":
        return []
    records = [item for item in task.get("acceptance_feedback", []) if isinstance(item, dict)]
    superseded = {
        str(feedback_id)
        for item in records
        for feedback_id in item.get("supersedes", [])
        if str(feedback_id).strip()
    }
    lines: list[str] = []
    for item in records:
        feedback_id = str(item.get("feedback_id") or "").strip()
        summary = re.sub(r"\s+", " ", str(item.get("summary") or "").strip())
        if item.get("status") == "active" and feedback_id not in superseded and summary and summary not in lines:
            lines.append(summary)
    return lines














def normalized_requirement_point_text(text: str) -> str:
    return str(text).strip()


def effective_change_requirement_points(requirement: dict[str, Any], version: str, *, start_index: int = 1) -> list[dict[str, str]]:
    points: list[dict[str, str]] = []
    effective_statuses = {"effective", "planned", "resolved", "verified"}
    for change in requirement.get("changes", []):
        if change.get("status") not in effective_statuses:
            continue
        for point_text in split_requirement_point_text(str(change.get("description") or change.get("summary") or "")):
            points.append(
                {
                    "id": f"FR-{start_index + len(points):03d}",
                    "version": version,
                    "status": "active",
                    "summary": shorten_text(point_text, 48),
                    "description": point_text,
                }
            )
    return points


def native_requirement_points(requirement: dict[str, Any], version: str) -> list[dict[str, str]]:
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    raw_points = native_start.get("requirement_points") or []
    points: list[dict[str, str]] = []
    for index, raw_point in enumerate(raw_points, start=1):
        if isinstance(raw_point, dict):
            point = dict(raw_point)
            point["id"] = str(point.get("id") or f"FR-{index:03d}")
            point["summary"] = str(point.get("summary") or point.get("title") or point.get("description") or point["id"])
            point["description"] = str(point.get("description") or point.get("summary") or point.get("title") or "")
            point["version"] = version
            point["status"] = str(point.get("status") or "active")
            points.append(point)  # type: ignore[arg-type]
    return points


def build_requirement_points(requirement: dict[str, Any], version: str) -> list[dict[str, str]]:
    points = native_requirement_points(requirement, version)
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    if not points:
        for index, point_text in enumerate(split_requirement_point_text(str(requirement.get("description", ""))), start=1):
            points.append(
                {
                    "id": f"FR-{index:03d}",
                    "version": version,
                    "status": "active",
                    "summary": shorten_text(point_text, 48),
                    "description": point_text,
                }
            )
    if native_start.get("formal_contract_version") not in {"formal.v2", "formal.v3"}:
        existing_descriptions = {normalized_requirement_point_text(point.get("description", "")) for point in points}
        for point in effective_change_requirement_points(requirement, version, start_index=len(points) + 1):
            normalized = normalized_requirement_point_text(point.get("description", ""))
            if normalized and normalized in existing_descriptions:
                continue
            points.append(point)
            if normalized:
                existing_descriptions.add(normalized)
    return points or [
        {
            "id": "FR-001",
            "version": version,
            "status": "active",
            "summary": str(requirement.get("title", "需求点")),
            "description": str(requirement.get("description", "待补充需求说明")),
        }
    ]


def build_acceptance_points(requirement: dict[str, Any], version: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in requirement.get("tasks", []):
        for text in [*list_value(task.get("manual_checks")), *list_value(task.get("test_items"))]:
            clean = sanitize_runtime_text(text) if is_imported_history_requirement(requirement) else str(text).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            points.append(
                {
                    "id": f"AC-{len(points) + 1:03d}",
                    "version": version,
                    "status": "active",
                    "description": clean,
                    "task_id": task["task_id"],
                }
            )
    if not points:
        points.append(
            {
                "id": "AC-001",
                "version": version,
                "status": "active",
                "description": f"确认 {requirement['title']} 符合当前生效需求",
                "task_id": "",
            }
        )
    return points


CURRENT_TEST_CASE_STATUSES = {"active", "negative_regression", "historical_defect", "manual_only"}


def build_test_cases(requirement: dict[str, Any], version: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    structured = requirement.get("structured") if isinstance(requirement.get("structured"), dict) else {}
    base_cases = native_start.get("test_cases") or structured.get("test_cases") or []
    tasks = requirement.get("tasks", [])
    fallback_description = f"验证 {requirement.get('title', '')} 当前生效需求"
    if (
        tasks
        and len(base_cases) == 1
        and isinstance(base_cases[0], dict)
        and not str(base_cases[0].get("task_id", "")).strip()
        and str(base_cases[0].get("description", "")).strip() == fallback_description
    ):
        base_cases = []
    acceptance_points = native_start.get("acceptance_points") or structured.get("acceptance_points") or []
    acceptance_requirement_ids = {
        str(item.get("id", "")): [str(ref) for ref in item.get("requirement_ids", [])]
        for item in acceptance_points
        if isinstance(item, dict)
    }
    task_by_base_case: dict[str, str] = {}
    for task in requirement.get("tasks", []):
        task_id = str(task.get("task_id", ""))
        coverage_tests = {str(item) for item in list_value(task.get("coverage_tests"))}
        coverage_points = {str(item) for item in list_value(task.get("coverage_points"))}
        for base_case in base_cases:
            if not isinstance(base_case, dict):
                continue
            case_id = str(base_case.get("id", ""))
            if not case_id:
                continue
            acceptance_ids = [str(item) for item in base_case.get("acceptance_ids", [])]
            case_requirement_ids = {
                requirement_id
                for acceptance_id in acceptance_ids
                for requirement_id in acceptance_requirement_ids.get(acceptance_id, [])
            }
            if case_id in coverage_tests or (coverage_points and case_requirement_ids and coverage_points.intersection(case_requirement_ids)):
                task_by_base_case.setdefault(case_id, task_id)

    max_case_number = 0
    for base_case in base_cases:
        if not isinstance(base_case, dict):
            continue
        case_id = str(base_case.get("id", "")).strip()
        if not case_id:
            continue
        match = re.fullmatch(r"TC-(\d+)", case_id)
        if match:
            max_case_number = max(max_case_number, int(match.group(1)))
        case_type = str(base_case.get("type", "") or "manual_only")
        status = str(base_case.get("status", "") or ("manual_only" if case_type == "manual_only" else "active"))
        description = str(base_case.get("description") or base_case.get("method") or base_case.get("pass_standard") or case_id)
        if is_imported_history_requirement(requirement):
            description = sanitize_runtime_text(description)
        task_id = str(base_case.get("task_id") or task_by_base_case.get(case_id, ""))
        cases.append(
            {
                **base_case,
                "id": case_id,
                "version": version,
                "status": status,
                "task_id": task_id,
                "description": description,
                "type": case_type,
            }
        )
        if task_id and description:
            seen.add((task_id, description))

    for task in requirement.get("tasks", []):
        task_id = str(task["task_id"])
        test_sources = [
            *list_value(task.get("test_items")),
            *list_value(task.get("test_scripts")),
            *list_value(task.get("manual_checks")),
        ]
        for text in test_sources:
            if isinstance(text, dict):
                clean = current_runtime_text(requirement, text.get("description") or text.get("title") or "")
                status = str(text.get("status") or "active")
                case_type = str(text.get("type") or "task_check")
            else:
                clean = current_runtime_text(requirement, text)
                status, case_type = "active", "task_check"
            key = (task_id, clean)
            if not clean or key in seen:
                continue
            seen.add(key)
            max_case_number += 1
            cases.append(
                {
                    "id": f"TC-{max_case_number:03d}",
                    "version": version,
                    "status": status,
                    "task_id": task_id,
                    "description": clean,
                    "type": case_type,
                }
            )
    if not cases:
        cases.append(
            {
                "id": "TC-001",
                "version": version,
                "status": "active",
                "task_id": "",
                "description": f"验证 {requirement['title']} 当前生效需求",
                "type": "manual_only",
            }
        )
    return cases


def test_cases_by_id(requirement: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(case.get("id", "")): case
        for case in requirement.get("structured", {}).get("test_cases", [])
        if case.get("id")
    }


def current_test_cases_for_task(requirement: dict[str, Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    cases = test_cases_by_id(requirement)
    selected: list[dict[str, Any]] = []
    for case_id in list_value(task.get("coverage_tests")):
        case = cases.get(str(case_id))
        if not case:
            continue
        if case.get("status", "active") in CURRENT_TEST_CASE_STATUSES:
            selected.append(case)
    return selected


def acceptance_points_by_id(requirement: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(point.get("id", "")): point
        for point in requirement.get("structured", {}).get("acceptance_points", [])
        if point.get("id")
    }


def derive_acceptance_for_task(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """按任务已经绑定的 FR / TC 反推 AC，让旧任务投影也能看到验收覆盖。"""

    existing = [str(item).strip() for item in list_value(task.get("coverage_acceptance")) if str(item).strip()]
    if existing:
        return list(dict.fromkeys(existing))

    coverage_points = {str(item).strip() for item in list_value(task.get("coverage_points")) if str(item).strip()}
    coverage_tests = {str(item).strip() for item in list_value(task.get("coverage_tests")) if str(item).strip()}
    selected: list[str] = []

    for case in test_cases_by_id(requirement).values():
        case_id = str(case.get("id", "")).strip()
        if case_id not in coverage_tests:
            continue
        for acceptance_id in list_value(case.get("acceptance_ids")):
            clean = str(acceptance_id).strip()
            if clean and clean not in selected:
                selected.append(clean)

    for acceptance in acceptance_points_by_id(requirement).values():
        acceptance_id = str(acceptance.get("id", "")).strip()
        if not acceptance_id or acceptance_id in selected:
            continue
        refs = {str(ref).strip() for ref in list_value(acceptance.get("requirement_ids") or acceptance.get("fr_ids")) if str(ref).strip()}
        if coverage_points and refs and coverage_points.intersection(refs):
            selected.append(acceptance_id)

    return selected


def formal_requirement_refs_for_task(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    refs = [str(item).strip() for item in list_value(task.get("formal_requirement_refs")) if str(item).strip()]
    if refs:
        return list(dict.fromkeys(refs))

    requirement_points = {
        str(point.get("id", "")): point
        for point in requirement.get("structured", {}).get("requirement_points", [])
        if str(point.get("id", "")).strip()
    }
    acceptance_points = acceptance_points_by_id(requirement)
    derived: list[str] = []
    for point_id in list_value(task.get("coverage_points")):
        key = str(point_id).strip()
        point = requirement_points.get(key)
        if point:
            title = str(point.get("title") or point.get("summary") or point.get("description") or "").strip()
            derived.append(f"功能需求 / {key} {title}".rstrip())
    for acceptance_id in derive_acceptance_for_task(requirement, task):
        acceptance = acceptance_points.get(str(acceptance_id))
        if acceptance:
            title = str(acceptance.get("title") or acceptance.get("description") or acceptance.get("expected") or "").strip()
            derived.append(f"验收标准 / {acceptance_id} {title}".rstrip())
    return list(dict.fromkeys(derived))


def formal_test_refs_for_task(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    refs = [str(item).strip() for item in list_value(task.get("formal_test_refs")) if str(item).strip()]
    if refs:
        return list(dict.fromkeys(refs))
    cases = test_cases_by_id(requirement)
    derived: list[str] = []
    for case_id in list_value(task.get("coverage_tests")):
        key = str(case_id).strip()
        case = cases.get(key)
        if case:
            title = str(case.get("description") or case.get("operation") or case.get("pass_standard") or "").strip()
            derived.append(f"测试矩阵 / {key} {title}".rstrip())
    return list(dict.fromkeys(derived))


def formal_design_refs_for_task(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    refs = [str(item).strip() for item in list_value(task.get("formal_design_refs")) if str(item).strip()]
    if refs:
        return list(dict.fromkeys(refs))

    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    design = structured.get("design", {}) if isinstance(structured.get("design"), dict) else {}
    section_fields = [
        ("涉及模块", "modules"),
        ("数据结构", "data_structures"),
        ("接口设计", "interfaces"),
        ("状态流", "state_flow"),
        ("数据流", "data_flow"),
        ("权限和安全", "permissions_security"),
        ("错误处理", "error_handling"),
        ("测试策略", "test_strategy"),
        ("本轮不做", "out_of_scope"),
    ]
    derived = [f"技术方案 / {title}" for title, key in section_fields if list_value(design.get(key))]
    return derived or ["技术方案 / 技术目标"]


def task_out_of_scope(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    explicit = [str(item).strip() for item in list_value(task.get("out_of_scope") or task.get("not_in_scope")) if str(item).strip()]
    if explicit:
        return list(dict.fromkeys(explicit))
    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    design = structured.get("design", {}) if isinstance(structured.get("design"), dict) else {}
    inherited = [
        *(str(item).strip() for item in list_value(structured.get("out_of_scope")) if str(item).strip()),
        *(str(item).strip() for item in list_value(design.get("out_of_scope")) if str(item).strip()),
    ]
    return list(dict.fromkeys(inherited))


def task_test_suggestions(task: dict[str, Any]) -> list[str]:
    explicit = [
        str(item).strip()
        for item in list_value(task.get("test_suggestions") or task.get("test_recommendations") or task.get("test_advice"))
        if str(item).strip()
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    return list(dict.fromkeys(str(item).strip() for item in list_value(task.get("test_items")) if str(item).strip()))


def passed_manual_verification_task_ids(event: dict[str, Any]) -> list[str]:
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return []
    return [
        str(item.get("task_id") or event.get("task_id") or "")
        for item in payload.get("verifications", [])
        if isinstance(item, dict) and item.get("type") == "manual" and item.get("status") == "passed"
    ]

def latest_unresolved_manual_pending_event(state: dict[str, Any], requirement: dict[str, Any]) -> dict[str, Any] | None:
    requirement_id = str(requirement.get("requirement_id", ""))
    latest_pending_index = -1
    latest_pending_event: dict[str, Any] | None = None
    confirmed_task_ids: set[str] = set()
    for index, event in enumerate(state.get("events", [])):
        if not isinstance(event, dict) or str(event.get("requirement_id", "")) != requirement_id:
            continue
        if event.get("event_type") == "regression_manual_pending":
            latest_pending_index = index
            latest_pending_event = event
            confirmed_task_ids = set()
            continue
        if latest_pending_event is None or index <= latest_pending_index or event.get("event_type") != "task_updated":
            continue
        if passed_manual_verification_task_ids(event):
            task_id = str(event.get("task_id", "")).strip()
            if task_id:
                confirmed_task_ids.add(task_id)
    if latest_pending_event is not None:
        payload = latest_pending_event.get("payload", {})
        manual_items = payload.get("manual_items", []) if isinstance(payload, dict) else []
        remaining_items = [
            item
            for item in manual_items
            if isinstance(item, dict) and str(item.get("task_id", "")).strip() not in confirmed_task_ids
        ]
        if remaining_items:
            updated_payload = dict(payload) if isinstance(payload, dict) else {}
            updated_payload["manual_items"] = remaining_items
            return {**latest_pending_event, "payload": updated_payload}
    return None


def requirement_has_unresolved_manual_pending(state: dict[str, Any], requirement: dict[str, Any]) -> bool:
    return latest_unresolved_manual_pending_event(state, requirement) is not None


def requirement_acceptance_is_current(requirement: dict[str, Any]) -> bool:
    if not requirement.get("accepted_at"):
        return False
    accepted_seq = int(requirement.get("_accepted_seq", -1))
    if accepted_seq < 0:
        return False
    # 事件时间只有秒级，连续执行时可能相同；用事件顺序判断“接受后是否又改过任务”更稳。
    for task in requirement.get("tasks", []):
        task_created_seq = int(task.get("_created_seq", -1))
        task_updated_seq = int(task.get("_updated_seq", -1))
        if task_created_seq > accepted_seq or task_updated_seq > accepted_seq:
            return False
    for change in requirement.get("changes", []):
        change_created_seq = int(change.get("_created_seq", -1))
        change_updated_seq = int(change.get("_updated_seq", -1))
        if change_created_seq > accepted_seq or change_updated_seq > accepted_seq:
            return False
    return True


def requirement_docs_file_exists(root: Path | None, doc: dict[str, Any]) -> bool:
    if root is None:
        return True
    file_path = str(doc.get("file_path", "")).strip()
    if not file_path:
        return False
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    return path.exists()


def requirement_docs_action(requirement: dict[str, Any], root: Path | None = None) -> dict[str, str] | None:
    if requirement.get("status") != "accepted":
        return None
    docs = requirement.get("docs") or []
    if not docs:
        return {
            "command": f"$sdlc-docs {requirement['requirement_id']}",
            "reason": f"{requirement['requirement_id']} 已经接受，先生成一份给开发维护看的需求逻辑梳理文档。",
        }
    accepted_seq = int(requirement.get("_accepted_seq", -1))
    valid_docs = [item for item in docs if isinstance(item, dict)]
    latest_docs_seq = max((int(item.get("_created_seq", -1)) for item in valid_docs), default=-1)
    docs_after_accept = [
        item
        for item in valid_docs
        if int(item.get("_created_seq", -1)) >= accepted_seq and requirement_docs_file_exists(root, item)
    ]
    if accepted_seq >= 0 and docs_after_accept:
        return None
    if accepted_seq >= 0 and latest_docs_seq >= accepted_seq:
        return {
            "command": f"$sdlc-docs {requirement['requirement_id']}",
            "reason": f"{requirement['requirement_id']} 的维护文档记录存在，但实际文件找不到，需要重新生成。",
        }
    if accepted_seq >= 0 and latest_docs_seq < accepted_seq:
        has_existing_docs_file = any(requirement_docs_file_exists(root, item) for item in valid_docs)
        return {
            "command": f"$sdlc-docs {requirement['requirement_id']}" + (" --force" if has_existing_docs_file else ""),
            "reason": f"{requirement['requirement_id']} 已经重新接受，维护文档早于最新收口结果，需要覆盖刷新。",
        }
    return None


def structured_version_labels(requirement: dict[str, Any]) -> dict[str, str]:
    version_number = structured_version_number(requirement)
    return {
        "requirement_version": f"requirement.v{version_number}",
        "design_version": f"design.v{version_number}",
        "test_matrix_version": f"test-matrix.v{version_number}",
    }


def coverage_for_task(task: dict[str, Any], requirement_points: list[dict[str, str]]) -> list[str]:
    """只接受任务卡显式声明的需求覆盖关系。"""

    known_ids = {str(point.get("id") or "") for point in requirement_points}
    return list(
        dict.fromkeys(
            item
            for item in (str(value).strip() for value in list_value(task.get("coverage_points")))
            if item and item in known_ids
        )
    )


def bind_tasks_to_current_contract(
    requirement: dict[str, Any],
    tasks: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
) -> None:
    target_tasks = tasks if tasks is not None else requirement.get("tasks", [])
    labels = structured_version_labels(requirement)
    requirement_version = labels["requirement_version"]
    test_matrix_version = labels["test_matrix_version"]
    requirement_points = build_requirement_points(requirement, requirement_version)
    task_scope_requirement = {**requirement, "tasks": target_tasks}
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    structured = requirement.get("structured") if isinstance(requirement.get("structured"), dict) else {}
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"}:
        # 当前严格合同的测试矩阵是唯一编号来源，任务自己的测试说明不能在这里
        # 临时造出 TC-005 之类不在 current 文档里的编号。
        test_cases = native_start.get("test_cases") or []
    else:
        test_cases = build_test_cases(task_scope_requirement, test_matrix_version)
    cases_by_task: dict[str, list[str]] = {}
    acceptance_requirement_ids = {
        str(item.get("id", "")): {str(ref) for ref in list_value(item.get("requirement_ids"))}
        for item in (native_start.get("acceptance_points") or structured.get("acceptance_points") or [])
        if isinstance(item, dict)
    }
    task_coverage_points = {
        str(task.get("task_id", "")): set(
            str(item)
            for item in (
                list_value(task.get("coverage_points"))
                or coverage_for_task(task, requirement_points)
            )
        )
        for task in target_tasks
    }
    for case in test_cases:
        task_id = str(case.get("task_id", ""))
        case_id = str(case["id"])
        if task_id:
            cases_by_task.setdefault(task_id, []).append(case_id)
            continue
        case_requirement_ids = {str(ref) for ref in list_value(case.get("requirement_ids"))}
        if not case_requirement_ids:
            for acceptance_id in list_value(case.get("acceptance_ids")):
                case_requirement_ids.update(acceptance_requirement_ids.get(str(acceptance_id), set()))
        for candidate_task_id, coverage_points in task_coverage_points.items():
            if coverage_points and case_requirement_ids and coverage_points.intersection(case_requirement_ids):
                cases_by_task.setdefault(candidate_task_id, []).append(case_id)

    for task in target_tasks:
        for field, value in labels.items():
            if force or not task.get(field):
                task[field] = value
        if force or "coverage_points" not in task or task.get("coverage_points") is None:
            task["coverage_points"] = coverage_for_task(task, requirement_points)
        if force or "coverage_tests" not in task or task.get("coverage_tests") is None:
            task["coverage_tests"] = cases_by_task.get(str(task["task_id"]), [])
        if force or "coverage_acceptance" not in task or task.get("coverage_acceptance") is None:
            task["coverage_acceptance"] = derive_acceptance_for_task(requirement, task)
        if force or "formal_requirement_refs" not in task or task.get("formal_requirement_refs") is None:
            task["formal_requirement_refs"] = formal_requirement_refs_for_task(requirement, task)
        if force or "formal_design_refs" not in task or task.get("formal_design_refs") is None:
            task["formal_design_refs"] = formal_design_refs_for_task(requirement, task)
        if force or "formal_test_refs" not in task or task.get("formal_test_refs") is None:
            task["formal_test_refs"] = formal_test_refs_for_task(requirement, task)
        if force or "out_of_scope" not in task or task.get("out_of_scope") is None:
            task["out_of_scope"] = task_out_of_scope(requirement, task)
        if force or "test_suggestions" not in task or task.get("test_suggestions") is None:
            task["test_suggestions"] = task_test_suggestions(task)


def task_contract_issues(
    requirement: dict[str, Any],
    task: dict[str, Any],
    *,
    require_coverage: bool = True,
) -> list[str]:
    if task.get("status") in {"done", "closed"}:
        # 已完成任务保留的是完成当时的版本绑定。后续变更会生成新任务承接，
        # 不应把历史完成记录当成当前执行链路异常。
        return []
    structured = requirement.get("structured", {})
    labels = structured_version_labels(requirement)
    expected_versions = {
        "requirement_version": structured.get("requirement_version", labels["requirement_version"]),
        "design_version": structured.get("design_version", labels["design_version"]),
        "test_matrix_version": structured.get("test_matrix_version", labels["test_matrix_version"]),
    }
    issue_labels = {
        "requirement_version": "需求",
        "design_version": "技术方案",
        "test_matrix_version": "测试矩阵",
    }
    issues: list[str] = []
    allow_active_version_drift = task.get("status") in ACTIVE_TASK_RUN_STATUSES
    for field, expected in expected_versions.items():
        actual = task.get(field)
        if actual != expected:
            if allow_active_version_drift:
                # 当前任务已进入执行或验收链路时，后续变更不应只因版本号变化卡住它收口。
                continue
            if actual:
                issues.append(f"{issue_labels[field]}版本过期：{actual} -> {expected}")
            else:
                issues.append(f"缺少{issue_labels[field]}绑定：需要 {expected}")
    coverage_points = list_value(task.get("coverage_points"))
    if not coverage_points:
        if require_coverage:
            issues.append("缺少覆盖需求点")
    else:
        current_points = {str(point.get("id", "")) for point in structured.get("requirement_points", [])}
        unknown_points = [str(point_id) for point_id in coverage_points if str(point_id) not in current_points]
        if unknown_points:
            issues.append("覆盖需求点不在当前生效需求：" + "、".join(unknown_points))
    coverage_tests = list_value(task.get("coverage_tests"))
    if not coverage_tests:
        if require_coverage:
            issues.append("缺少覆盖测试")
    else:
        cases = test_cases_by_id(requirement)
        unknown_cases = [str(case_id) for case_id in coverage_tests if str(case_id) not in cases]
        if unknown_cases:
            issues.append("覆盖测试不在当前测试矩阵：" + "、".join(unknown_cases))
        elif not current_test_cases_for_task(requirement, task):
            issues.append("覆盖测试没有当前可执行项")
    coverage_acceptance = list_value(task.get("coverage_acceptance"))
    if coverage_acceptance:
        acceptance_points = acceptance_points_by_id(requirement)
        unknown_acceptance = [str(acceptance_id) for acceptance_id in coverage_acceptance if str(acceptance_id) not in acceptance_points]
        if unknown_acceptance:
            issues.append("覆盖验收不在当前需求说明书：" + "、".join(unknown_acceptance))
    coverage_change_ids = list_value(task.get("coverage_change_ids"))
    if coverage_change_ids:
        current_change_ids = {
            str(change.get("change_id", ""))
            for change in structured.get("effective_changes", [])
            if isinstance(change, dict)
        }
        unknown_changes = [str(change_id) for change_id in coverage_change_ids if str(change_id) not in current_change_ids]
        if unknown_changes:
            issues.append("覆盖变更不在当前已确认变更附录：" + "、".join(unknown_changes))
    return issues


def task_contract_gate_message(requirement: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    lines = [
        "当前任务和当前生效需求版本没有对齐，不能继续执行。",
        f"请重新生成 {requirement['requirement_id']} 的完整任务合同，通过 `tasks` 提交后再用 `review` 完成整套任务审核。",
        "问题明细：",
    ]
    for task in tasks:
        issues = task_contract_issues(requirement, task)
        if not issues:
            continue
        lines.append(f"- {requirement['requirement_id']} / {task['task_id']}：")
        lines.extend(f"  - {issue}" for issue in issues)
    return "\n".join(lines)


def tasks_with_contract_issues(requirement: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task for task in tasks if task_contract_issues(requirement, task)]


def requirement_has_unplanned_changes(requirement: dict[str, Any]) -> bool:
    return any(change.get("status") in {"draft", "effective", "pending"} for change in requirement.get("changes", []))


def task_contract_issue_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for requirement in state.get("requirements", {}).values():
        if requirement_has_unplanned_changes(requirement):
            continue
        for task in tasks_with_contract_issues(requirement, requirement.get("tasks", [])):
            issue_text = "、".join(task_contract_issues(requirement, task))
            lines.append(f"{requirement['requirement_id']} / {task['task_id']}：{issue_text}")
    return lines


def task_plan_payload(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "source_task_id": task.get("source_task_id", ""),
        "subtasks": list(task.get("subtasks", [])),
        "title": task["title"],
        "summary": task["summary"],
        "status": task.get("status", "todo"),
        "depends_on": list_value(task.get("depends_on")),
        "changed_files": list_value(task.get("changed_files")),
        "context_files": list_value(task.get("context_files")),
        "output_files": list_value(task.get("output_files")),
        "related_files": list_value(task.get("related_files")),
        "commands": list_value(task.get("commands")),
        "test_items": list_value(task.get("test_items")),
        "test_commands": list_value(task.get("test_commands")),
        "test_scripts": list_value(task.get("test_scripts")),
        "manual_checks": list_value(task.get("manual_checks")),
        "verifications": list_value(task.get("verifications")),
        "note": task.get("note", ""),
        "business_rules": list_value(task.get("business_rules")),
        "requirement_version": task.get("requirement_version"),
        "design_version": task.get("design_version"),
        "test_matrix_version": task.get("test_matrix_version"),
        "coverage_points": list_value(task.get("coverage_points")) or None,
        "coverage_change_ids": list_value(task.get("coverage_change_ids")) or None,
        "coverage_acceptance": list_value(task.get("coverage_acceptance")) or None,
        "coverage_tests": list_value(task.get("coverage_tests")) or None,
        "feedback_contract_version": task.get("feedback_contract_version", ""),
        "feedback_state": task.get("feedback_state", ""),
        "acceptance_feedback": deepcopy(task.get("acceptance_feedback", [])),
        "formal_requirement_refs": list_value(task.get("formal_requirement_refs")) or None,
        "formal_design_refs": list_value(task.get("formal_design_refs")) or None,
        "formal_test_refs": list_value(task.get("formal_test_refs")) or None,
        "out_of_scope": list_value(task.get("out_of_scope")) or None,
        "test_suggestions": list_value(task.get("test_suggestions")) or None,
        "formal_gate": task.get("formal_gate", True),
        "coverage_acceptance_explicit": task.get("coverage_acceptance_explicit", False),
    }


def repair_task_contracts(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, list[str]]:
    repaired: list[str] = []
    warnings: list[str] = []
    for requirement in state.get("requirements", {}).values():
        if requirement_has_unplanned_changes(requirement):
            continue
        tasks = requirement.get("tasks", [])
        if not tasks_with_contract_issues(requirement, tasks):
            continue
        repaired_tasks = [task_plan_payload(task) for task in tasks]
        bind_tasks_to_current_contract(requirement, repaired_tasks, force=True)
        unresolved = tasks_with_contract_issues(requirement, repaired_tasks)
        if unresolved:
            for task in unresolved:
                warnings.append(f"{requirement['requirement_id']} / {task['task_id']}：" + "、".join(task_contract_issues(requirement, task)))
            continue
        append_event(
            paths,
            event_type="plan_updated",
            source="sdlc-doctor-repair",
            summary=f"刷新 {requirement['requirement_id']} 的任务版本和覆盖关系",
            requirement_id=requirement["requirement_id"],
            payload={
                "tasks": repaired_tasks,
                "priority": requirement.get("priority", "normal"),
                "blocked_reason": requirement.get("blocked_reason", ""),
                "resolved_change_ids": [],
                "task_quality": requirement.get("task_quality", {}),
            },
        )
        repaired.append(requirement["requirement_id"])
    return {"repaired": repaired, "warnings": warnings}


def clean_imported_requirement_title(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def repair_imported_requirement_metadata(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, list[str]]:
    return {"repaired": [], "warnings": []}


def apply_structured_requirement_model(requirement: dict[str, Any]) -> None:
    labels = structured_version_labels(requirement)
    requirement_version = labels["requirement_version"]
    design_version = labels["design_version"]
    test_matrix_version = labels["test_matrix_version"]
    native_start = requirement.get("native_start") or {}
    legacy_fields = requirement_summary_fields_from_text(requirement) if not native_start and is_imported_history_requirement(requirement) else {}
    requirement_points = build_requirement_points(requirement, requirement_version)
    acceptance_points = native_start.get("acceptance_points") or build_acceptance_points(requirement, requirement_version)
    has_effective_changes = any(
        change.get("status") in {"effective", "planned", "resolved", "verified"}
        for change in requirement.get("changes", [])
        if isinstance(change, dict)
    )
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"}:
        # 当前严格合同的测试矩阵是正式真相源，普通规划更新不能用临时用例覆盖它。
        test_cases = native_start.get("test_cases") or (
            requirement.get("prepared_test_cases")
            if native_start.get("workflow_profile") == "document-first.v1"
            else []
        ) or []
    elif has_effective_changes:
        # 已确认变更会带来新 FR 和任务，变更规划完成前先把对应任务测试加入变更期矩阵。
        test_cases = build_test_cases(requirement, test_matrix_version)
    else:
        test_cases = requirement.get("prepared_test_cases") or native_start.get("test_cases") or build_test_cases(requirement, test_matrix_version)
    for point in requirement_points:
        point.setdefault("version", requirement_version)
        point.setdefault("status", "active")
    for point in acceptance_points:
        point.setdefault("version", requirement_version)
        point.setdefault("status", "active")
    for case in test_cases:
        case.setdefault("version", test_matrix_version)
        case.setdefault("status", "active")
        case.setdefault("type", "manual_only")
    bind_tasks_to_current_contract(requirement, force=False)

    requirement["structured"] = {
        "formal_contract_version": native_start.get("formal_contract_version", ""),
        "requirement_version": requirement_version,
        "design_version": design_version,
        "test_matrix_version": test_matrix_version,
                "migration_status": native_start.get("migration_status", "native" if native_start else ("imported-upgraded" if legacy_fields else "structured")),
        "background": native_start.get("background", legacy_fields.get("background", "")),
        "goal": native_start.get("goal", legacy_fields.get("goal", "")),
        "scope": native_start.get("scope", legacy_fields.get("scope", [])),
        "out_of_scope": native_start.get("out_of_scope", legacy_fields.get("out_of_scope", [])),
        "user_scenarios": native_start.get("user_scenarios", []),
        "business_rules": native_start.get("business_rules", []),
        "risks": native_start.get("risks", legacy_fields.get("risks", [])),
        "assumptions": native_start.get("assumptions", []),
        "permission_rules": native_start.get("permission_rules", []),
        "data_state_rules": native_start.get("data_state_rules", []),
        "interface_scope": native_start.get("interface_scope", []),
        "exception_rules": native_start.get("exception_rules", []),
        "test_focus": native_start.get("test_focus", []),
        "open_questions": native_start.get("open_questions", legacy_fields.get("open_questions", [])),
        "source_refs": native_start.get("source_refs", []),
        "design": native_start.get("design", {}),
        # 已确认变更先作为独立合同附录保存，不能把一行自然语言伪造成缺字段 FR。
        # 变更任务同时引用 CHG 和原 FR；只有完整结构化变更进入正式包后才改写 FR。
        "effective_changes": [
            {
                "change_id": change.get("change_id", ""),
                "status": change.get("status", ""),
                "summary": change.get("summary", ""),
                "description": change.get("description", ""),
                "acceptance_points": list_value(change.get("acceptance_points")),
                "planning_status": change.get("planning_status", ""),
            }
            for change in requirement.get("changes", [])
            if isinstance(change, dict) and change.get("status") in {"effective", "planned", "resolved", "verified"}
        ],
        "requirement_points": requirement_points,
        "acceptance_points": acceptance_points,
        "test_cases": test_cases,
    }
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"}:
        uses_task_coverage_contract = (
            isinstance(requirement.get("task_coverage_contract"), Mapping)
            and requirement["task_coverage_contract"].get("schema_version")
            == "task-coverage.v1"
        )
        for task in requirement.get("tasks", []):
            # 已显式绑定的测试编号必须原样保留，doctor-deep 才能报告 TC-999
            # 这类漂移；这里只给完全没有覆盖关系的任务补可确定映射。
            coverage_tests = [str(case_id) for case_id in list_value(task.get("coverage_tests"))]
            if not coverage_tests and not uses_task_coverage_contract:
                coverage_points = {str(point_id) for point_id in list_value(task.get("coverage_points"))}
                coverage_tests = [
                    str(case.get("id"))
                    for case in test_cases
                    if str(case.get("task_id") or "") == str(task.get("task_id") or "")
                    or coverage_points.intersection(str(point_id) for point_id in case.get("requirement_ids", []))
                ]
            task["coverage_tests"] = coverage_tests
            task["formal_test_refs"] = formal_test_refs_for_task(requirement, task)


def derive_state(paths: ProjectPaths) -> dict[str, Any]:
    # status/next 等只读命令不一定经过身份门禁；发现活动变更事务时也必须
    # 先恢复，再从事件和正式文件推导状态，不能展示半次提交。
    active_change_dir = paths.change_transactions_dir / "accept-active"
    if active_change_dir.is_dir() and any(active_change_dir.iterdir()):
        from codex_sdlc.services.change_service import recover_change_accept_transactions

        recover_change_accept_transactions(paths)
    # 恢复轮次会同时写旧轮次、新目录、事件和当前指针。所有状态入口先收好遗留事务，
    # 避免 status、next 或后续命令把中断产生的半完成文件当成正式状态。
    from codex_sdlc.core.task_run import recover_pending_task_restores

    recover_pending_task_restores(paths)
    events = load_events(paths)
    requirements: OrderedDict[str, dict[str, Any]] = OrderedDict()
    sessions: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    grills: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    designs: list[dict[str, Any]] = []
    drafts: OrderedDict[str, dict[str, Any]] = OrderedDict()
    project_data: dict[str, Any] = {
        "project_path": str(paths.root),
        "project_name": paths.root.name,
        "git_ignore_file": read_global_excludesfile(),
        "git_ignore_rule": ".codex-sdlc/",
    }

    for event_index, event in enumerate(events):
        event_type = event["event_type"]
        payload = event["payload"]
        requirement_id = event.get("requirement_id")
        task_id = event.get("task_id")

        if event_type == "project_initialized":
            project_data.update(payload)
            continue

        if event_type == "draft_created":
            draft_id = str(payload.get("draft_id") or "").strip()
            if not draft_id:
                continue
            drafts[draft_id] = new_draft_from_event(payload, event, event_index)
            continue

        if event_type == "draft_mutated":
            draft_id = str(payload.get("draft_id") or "").strip()
            operation = str(payload.get("operation") or "").strip()
            mutation_changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
            capture_payload = payload.get("capture") if isinstance(payload.get("capture"), dict) else None
            if capture_payload is not None:
                # discuss 的 CAP 和 DRAFT 更新放在同一条业务事件里，任一合同失败都不会留下半条记录。
                capture_id = str(capture_payload.get("capture_id") or "")
                if not capture_id or any(item.get("capture_id") == capture_id for item in captures):
                    raise SdlcError(f"结构化 CAP 编号缺失或重复：{capture_id}。")
                captures.append(
                    {
                        "capture_id": capture_payload["capture_id"],
                        "created_at": event["created_at"],
                        "task_id": capture_payload.get("task_id", ""),
                        "summary": capture_payload["summary"],
                        "note": capture_payload.get("note", ""),
                        "status": capture_payload.get("status", "pending"),
                        "initial_status": capture_payload.get("status", "pending"),
                        "requirement_id": capture_payload.get("requirement_id"),
                        "target_type": capture_payload.get("target_type", "requirement_draft"),
                        "draft_id": draft_id,
                        "changed_files": capture_payload.get("changed_files", []),
                        "commands": capture_payload.get("commands", []),
                        "questions": capture_payload.get("questions", []),
                        "linked_change_id": capture_payload.get("linked_change_id"),
                        "file_path": capture_payload["file_path"],
                        "submission_key": capture_payload.get("submission_key", ""),
                        "submission_sha256": capture_payload.get("submission_sha256", ""),
                        "structured_increment": deepcopy(capture_payload.get("structured_increment")),
                        "decision_records": deepcopy(capture_payload.get("decision_records", [])),
                    }
                )
            if operation == "create":
                if draft_id and draft_id not in drafts:
                    drafts[draft_id] = new_draft_from_event(
                        mutation_changes,
                        event,
                        event_index,
                        structured_contract=True,
                    )
                    if capture_payload is not None:
                        _apply_structured_capture(drafts[draft_id], capture_payload)
                    draft_artifacts.apply_artifact_updates(
                        drafts[draft_id],
                        payload.get("artifact_updates"),
                    )
                continue
            draft = drafts.get(draft_id)
            if draft is None and capture_payload is not None:
                raise SdlcError(f"结构化 CAP 找不到对应 DRAFT：{draft_id}。")
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            if capture_payload is not None:
                _apply_structured_capture(draft, capture_payload)
            for key in (
                "title",
                "requirement_summary",
                "requirement_body",
                "design_summary",
                "design_body",
                "questions",
                "decisions",
                "review_items",
                "resolved_questions",
                "fact_source_projection",
                "fact_source_index",
                "requirement_facts",
                "design_facts",
                "model_review",
                "fact_run_ids",
                "review_request_id",
                "review_receipt",
            ):
                if key not in mutation_changes:
                    continue
                if key in {"questions", "decisions", "review_items", "resolved_questions"}:
                    draft[key] = string_list_value(mutation_changes.get(key, []))
                elif key in {"fact_source_projection", "fact_source_index", "requirement_facts", "design_facts", "model_review", "fact_run_ids", "review_receipt"}:
                    draft[key] = deepcopy(mutation_changes.get(key))
                else:
                    draft[key] = str(mutation_changes.get(key) or "")
            draft_artifacts.apply_artifact_updates(draft, payload.get("artifact_updates"))
            touch_draft(draft, event, event_index)
            continue

        if event_type == "structured_capture_transitioned":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            transition = payload.get("transition")
            transition_submission = payload.get("transition_submission")
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(f"CAP 状态转换找不到可编辑 DRAFT：{draft_id}。")
            if not isinstance(transition, dict):
                raise SdlcError("CAP 状态转换事件缺少结构化 transition。")
            if not isinstance(transition_submission, dict):
                raise SdlcError("CAP 状态转换事件缺少独立原始 submission。")
            _apply_structured_capture_transition(
                paths,
                draft,
                captures,
                drafts,
                requirements,
                transition,
                transition_submission,
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_requirement_confirmed":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            confirmation = payload.get("confirmation")
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(f"需求确认找不到可编辑 DRAFT：{draft_id}。")
            if not isinstance(confirmation, dict):
                raise SdlcError("需求确认事件缺少结构化 confirmation。")
            from codex_sdlc.services.draft_service import (
                validate_requirement_confirmation,
            )

            record = validate_requirement_confirmation(confirmation)
            if record["draft_id"] != draft_id:
                raise SdlcError("需求确认记录的 draft_id 与事件目标不一致。")
            confirmations = [
                deepcopy(item)
                for item in draft.get("requirement_confirmations", [])
                if isinstance(item, dict)
            ]
            existing = next(
                (
                    item
                    for item in confirmations
                    if item.get("confirmation_id") == record["confirmation_id"]
                ),
                None,
            )
            if existing is None:
                confirmations.append(record)
                confirmations.sort(key=lambda item: str(item["confirmation_id"]))
            elif canonical_sha256(existing) != canonical_sha256(record):
                raise SdlcError(
                    f"需求确认编号重复且内容冲突：{record['confirmation_id']}。"
                )
            draft["requirement_confirmations"] = confirmations
            draft["_structured_stage_enabled"] = True
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_artifact_registered":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            artifact = payload.get("artifact")
            draft_artifacts.apply_artifact_updates(draft, [artifact] if isinstance(artifact, dict) else [])
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_material_added":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            material = payload.get("material")
            if not isinstance(material, dict) or not str(material.get("material_id") or "").strip():
                continue
            material_id = str(material["material_id"])
            supersedes = str(material.get("supersedes") or "")
            current_materials = [
                deepcopy(item)
                for item in draft.get("materials", [])
                if isinstance(item, dict)
            ]
            for item in current_materials:
                if supersedes and item.get("material_id") == supersedes:
                    # 新修订只改变旧资料的活动状态，旧文件和旧哈希始终保留。
                    item["status"] = "archived"
            for index, item in enumerate(current_materials):
                if item.get("material_id") == material_id:
                    current_materials[index] = deepcopy(material)
                    break
            else:
                current_materials.append(deepcopy(material))
            draft["materials"] = sorted(current_materials, key=lambda item: str(item.get("material_id") or ""))
            draft["_material_manifest_enabled"] = True
            draft["_structured_stage_enabled"] = True
            draft_artifacts.apply_artifact_updates(draft, payload.get("artifact_updates"))
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_design_reference_imported":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(f"技术方案引用找不到可编辑 DRAFT：{draft_id}。")
            reference = payload.get("reference")
            if not isinstance(reference, dict):
                raise SdlcError("技术方案引用事件缺少结构化 reference。")
            from codex_sdlc.services.design_service import (
                design_reference_identity_sha256,
                validate_design_reference_record,
            )

            record = validate_design_reference_record(reference)
            if record["draft_id"] != draft_id:
                raise SdlcError("技术方案引用的 draft_id 与事件目标不一致。")
            current_references = [
                deepcopy(item)
                for item in draft.get("design_references", [])
                if isinstance(item, dict)
            ]
            same_id = next(
                (
                    item
                    for item in current_references
                    if item.get("design_id") == record["design_id"]
                ),
                None,
            )
            if same_id is not None:
                if canonical_sha256(same_id) != canonical_sha256(record):
                    raise SdlcError(
                        f"技术方案引用编号重复且内容冲突：{record['design_id']}。"
                    )
            else:
                same_key = next(
                    (
                        item
                        for item in current_references
                        if item.get("client_key") == record["client_key"]
                    ),
                    None,
                )
                if same_key is not None and (
                    design_reference_identity_sha256(same_key)
                    != design_reference_identity_sha256(record)
                ):
                    raise SdlcError(
                        f"技术方案引用 client_key 重复且内容冲突：{record['client_key']}。"
                    )
                current_references.append(record)
            draft["design_references"] = sorted(
                current_references,
                key=lambda item: str(item.get("design_id") or ""),
            )
            draft["_design_reference_enabled"] = True
            draft["_structured_stage_enabled"] = True
            draft_artifacts.apply_artifact_updates(
                draft, payload.get("artifact_updates")
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_design_reference_confirmed":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(f"技术方案确认找不到可编辑 DRAFT：{draft_id}。")
            reference = payload.get("reference")
            if not isinstance(reference, dict):
                raise SdlcError("技术方案确认事件缺少结构化 reference。")
            from codex_sdlc.services.design_service import (
                design_reference_identity_sha256,
                validate_design_reference_record,
            )

            confirmed = validate_design_reference_record(reference)
            if confirmed["draft_id"] != draft_id or confirmed["status"] != "confirmed":
                raise SdlcError("技术方案确认记录的 DRAFT 或状态无效。")
            current_references = [
                deepcopy(item)
                for item in draft.get("design_references", [])
                if isinstance(item, dict)
            ]
            matches = [
                item
                for item in current_references
                if item.get("design_id") == confirmed["design_id"]
            ]
            if len(matches) != 1:
                raise SdlcError(
                    f"技术方案确认找不到唯一导入记录：{confirmed['design_id']}。"
                )
            existing = matches[0]
            if (
                design_reference_identity_sha256(existing)
                != design_reference_identity_sha256(confirmed)
                or existing.get("path") != confirmed.get("path")
                or existing.get("sha256") != confirmed.get("sha256")
                or existing.get("requirement_confirmation_sha256")
                != confirmed.get("requirement_confirmation_sha256")
            ):
                raise SdlcError(
                    f"技术方案确认内容与导入记录不一致：{confirmed['design_id']}。"
                )
            if existing.get("status") == "confirmed" and (
                canonical_sha256(existing) != canonical_sha256(confirmed)
            ):
                raise SdlcError(
                    f"已确认技术方案引用不能原地覆盖：{confirmed['design_id']}。"
                )
            draft["design_references"] = [
                deepcopy(confirmed)
                if item.get("design_id") == confirmed["design_id"]
                else item
                for item in current_references
            ]
            draft["_design_reference_enabled"] = True
            draft["_structured_stage_enabled"] = True
            draft_artifacts.apply_artifact_updates(
                draft, payload.get("artifact_updates")
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_design_artifact_imported":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(
                    f"模块化设计产物找不到可编辑 DRAFT：{draft_id}。"
                )
            artifact = payload.get("artifact")
            if not isinstance(artifact, dict):
                raise SdlcError("模块化设计事件缺少结构化产物。")
            from codex_sdlc.core.design_artifact_contract import (
                validate_design_artifact_record,
            )

            record = validate_design_artifact_record(artifact)
            if record["draft_id"] != draft_id:
                raise SdlcError(
                    "模块化设计产物的 draft_id 与事件目标不一致。"
                )
            history = [
                deepcopy(item)
                for item in draft.get("design_artifacts", [])
                if isinstance(item, dict)
            ]
            same_revision = next(
                (
                    item
                    for item in history
                    if item.get("artifact_id") == record["artifact_id"]
                    and item.get("revision") == record["revision"]
                ),
                None,
            )
            if same_revision is not None:
                if canonical_sha256(same_revision) != canonical_sha256(record):
                    raise SdlcError(
                        f"模块 {record['artifact_id']} 的版本 {record['revision']} 包含冲突事件。"
                    )
            else:
                previous = [
                    item
                    for item in history
                    if item.get("artifact_id") == record["artifact_id"]
                ]
                if int(record["revision"]) != len(previous) + 1:
                    raise SdlcError(
                        f"模块 {record['artifact_id']} 的版本序号不连续。"
                    )
                expected_previous = (
                    previous[-1].get("artifact_sha256") if previous else None
                )
                if record["previous_artifact_sha256"] != expected_previous:
                    raise SdlcError(
                        f"模块 {record['artifact_id']} 的版本链与事件记录不一致。"
                    )
                history.append(record)
            draft["design_artifacts"] = sorted(
                history,
                key=lambda item: (
                    str(item.get("artifact_id") or ""),
                    int(item.get("revision") or 0),
                ),
            )
            draft["_design_artifact_enabled"] = True
            draft["_structured_stage_enabled"] = True
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_design_summary_imported":
            draft_id = str(payload.get("draft_id") or "").strip()
            draft = drafts.get(draft_id)
            if draft is None or draft_lifecycle.is_started_draft(draft):
                raise SdlcError(
                    f"总体设计说明找不到可编辑 DRAFT：{draft_id}。"
                )
            summary = payload.get("summary")
            if not isinstance(summary, dict):
                raise SdlcError("总体设计事件缺少结构化说明。")
            from codex_sdlc.core.design_summary_contract import (
                validate_design_summary_record,
            )

            record = validate_design_summary_record(summary)
            if record["draft_id"] != draft_id:
                raise SdlcError("总体设计说明的 draft_id 与事件目标不一致。")
            history = [
                deepcopy(item)
                for item in draft.get("design_summaries", [])
                if isinstance(item, dict)
            ]
            same_revision = next(
                (
                    item
                    for item in history
                    if item.get("revision") == record["revision"]
                ),
                None,
            )
            if same_revision is not None:
                if canonical_sha256(same_revision) != canonical_sha256(record):
                    raise SdlcError(
                        f"{draft_id} 的总体设计版本 {record['revision']} 包含冲突事件。"
                    )
            else:
                if int(record["revision"]) != len(history) + 1:
                    raise SdlcError(f"{draft_id} 的总体设计版本序号不连续。")
                expected_previous = (
                    history[-1].get("summary_sha256") if history else None
                )
                if record["previous_summary_sha256"] != expected_previous:
                    raise SdlcError(
                        f"{draft_id} 的总体设计版本链与事件记录不一致。"
                    )
                history.append(record)
            draft["design_summaries"] = sorted(
                history,
                key=lambda item: int(item.get("revision") or 0),
            )
            stale_modules = sorted(
                str(item) for item in record["invalidated_modules"]
            )
            current_module_ids = sorted(
                {
                    str(item.get("artifact_id") or "")
                    for item in draft.get("design_artifacts", [])
                    if isinstance(item, dict)
                    and str(item.get("artifact_id") or "")
                }
            )
            draft["design_summary_invalidation"] = {
                "stale_modules": stale_modules,
                "current_modules": sorted(
                    set(current_module_ids) - set(stale_modules)
                ),
                "review_targets": deepcopy(
                    record["invalidated_review_targets"]
                ),
            }
            draft["_design_summary_enabled"] = True
            draft["_structured_stage_enabled"] = True
            touch_draft(draft, event, event_index)
            continue

        if event_type == "structured_package_imported":
            requirement_import = _draft_requirement_import_from_event(paths, event)
            if requirement_import is None:
                continue
            draft_id, split_document, coverage_document, import_record = requirement_import
            draft = drafts.get(draft_id)
            if draft is None:
                raise SdlcError(f"需求导入事件找不到对应 DRAFT：{draft_id}。")
            if draft_lifecycle.is_started_draft(draft):
                raise SdlcError(f"已经正式建档的 {draft_id} 不能接收需求导入事件。")
            # 最新导入包完整替换上一份结构化拆分和覆盖关系；历史包仍由事件与原子目录保留。
            draft["requirement_split"] = deepcopy(split_document)
            draft["requirement_coverage"] = deepcopy(coverage_document)
            draft["requirement_import"] = deepcopy(import_record)
            draft["_structured_stage_enabled"] = True
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_requirement_updated":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            if "title" in payload and str(payload.get("title") or "").strip():
                draft["title"] = str(payload["title"])
            if "status" in payload and str(payload.get("status") or "").strip():
                draft["status"] = str(payload["status"])
            if "requirement_summary" in payload:
                draft["requirement_summary"] = str(payload.get("requirement_summary") or "")
            if "requirement_body" in payload:
                draft["requirement_body"] = str(payload.get("requirement_body") or "")
            if "questions" in payload:
                # questions 表示“当前还要问用户的问题”，所以按最新事件替换，而不是一直累加。
                draft["questions"] = string_list_value(payload.get("questions", []))
                if draft["questions"] and "status" not in payload:
                    # `draft question` 这类基础写入命令可能只补问题列表，不额外传状态；
                    # 这里按“有待确认问题就进入 needs_user”兜底，保证 next/status 能给出正确提醒。
                    draft["status"] = "needs_user"
            draft_artifacts.refresh_existing_projection_records(
                draft,
                [field for field in ("requirement_body", "questions") if field in payload],
                producer_task_id=str(event.get("source") or "draft_requirement_updated"),
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_design_updated":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            if "title" in payload and str(payload.get("title") or "").strip():
                draft["title"] = str(payload["title"])
            if "status" in payload and str(payload.get("status") or "").strip():
                draft["status"] = str(payload["status"])
            if "design_summary" in payload:
                draft["design_summary"] = str(payload.get("design_summary") or "")
            if "design_body" in payload:
                draft["design_body"] = str(payload.get("design_body") or "")
            if "questions" in payload:
                # design 阶段发现需求草稿和技术草稿打架时，也要能直接把当前待确认问题写进 DRAFT。
                draft["questions"] = string_list_value(payload.get("questions", []))
                if draft["questions"] and "status" not in payload:
                    draft["status"] = "needs_user"
            draft_artifacts.refresh_existing_projection_records(
                draft,
                [field for field in ("design_body", "questions") if field in payload],
                producer_task_id=str(event.get("source") or "draft_design_updated"),
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_review_recorded":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            review_items = string_list_value(payload.get("review_items", []))
            if payload.get("review"):
                review_items.append(str(payload["review"]).strip())
            if payload.get("review_body"):
                review_items.append(str(payload["review_body"]).strip())
            existing_review_items = draft.get("review_items", [])
            draft["review_items"] = extend_unique_strings(existing_review_items, review_items)
            if "status" in payload and str(payload.get("status") or "").strip():
                draft["status"] = str(payload["status"])
            draft_artifacts.refresh_existing_projection_records(
                draft,
                ["review_items"],
                producer_task_id=str(event.get("source") or "draft_review_recorded"),
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_decision_recorded":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            decisions = string_list_value(payload.get("decisions", []))
            if payload.get("decision"):
                decisions.append(str(payload["decision"]).strip())
            draft["decisions"] = extend_unique_strings(draft.get("decisions", []), decisions)
            if "questions" in payload:
                # 带问题解决能力的 decision 表示“当前问题列表已经更新”，不能继续保留旧问题。
                draft["questions"] = string_list_value(payload.get("questions", []))
            if "status" in payload and str(payload.get("status") or "").strip():
                draft["status"] = str(payload["status"])
            draft_artifacts.refresh_existing_projection_records(
                draft,
                ["decisions", *(["questions"] if "questions" in payload else [])],
                producer_task_id=str(event.get("source") or "draft_decision_recorded"),
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "draft_status_changed":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None or draft_lifecycle.is_started_draft(draft):
                continue
            # 旧人工状态事件只保留在 events.jsonl 供审计，不能再改变有效状态。
            continue

        if event_type == "draft_started":
            draft = drafts.get(str(payload.get("draft_id") or "").strip())
            if draft is None:
                continue
            draft["status"] = "started"
            draft["questions"] = []
            draft["started_requirement_id"] = str(payload.get("started_requirement_id") or requirement_id or "")
            # started 是受管系统事件，可以刷新已有问题投影；旧 DRAFT 没有登记时仍只保留旧文件。
            draft_artifacts.refresh_existing_projection_records(
                draft,
                ["questions"],
                producer_task_id=str(event.get("source") or "sdlc-start"),
            )
            touch_draft(draft, event, event_index)
            continue

        if event_type == "requirement_created":
            requirements[requirement_id] = {
                "requirement_id": requirement_id,
                "title": payload["title"],
                "description": payload["description"],
                "summary": payload.get("summary", payload["title"]),
                "folder_name": payload["folder_name"],
                "flow_type": payload.get("flow_type", "轻量流程"),
                "plan_excerpt": payload.get("plan_excerpt", ""),
                "priority": payload.get("priority", "normal"),
                "blocked_reason": payload.get("blocked_reason", ""),
                "task_quality": payload.get("task_quality", {}),
                "preflight_tasks": payload.get("preflight_tasks", []),
                "needs_prepare": payload.get("needs_prepare", False),
                "native_start": payload.get("native_start", {}),
                "created_at": event["created_at"],
                "updated_at": event["created_at"],
                "_created_seq": event_index,
                "_updated_seq": event_index,
                "status": "active",
                "accepted_at": "",
                "_accepted_seq": -1,
                "tasks": [],
                "changes": [],
                "captures": [],
                "grills": [],
                "materials": [],
                "designs": [],
                "docs": [],
            }
            continue

        if event_type == "requirement_metadata_updated":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            if payload.get("title"):
                requirement["title"] = payload["title"]
            if payload.get("summary"):
                requirement["summary"] = payload["summary"]
            if payload.get("description"):
                requirement["description"] = payload["description"]
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "task_created":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            task = {
                "requirement_id": requirement_id,
                "task_id": task_id,
                "source_task_id": payload.get("source_task_id", ""),
                "subtasks": payload.get("subtasks", []),
                "title": payload["title"],
                "summary": payload["summary"],
                "status": payload.get("status", "todo"),
                "depends_on": payload.get("depends_on", []),
                "created_at": event["created_at"],
                "updated_at": event["created_at"],
                "_created_seq": event_index,
                "_updated_seq": event_index,
                "changed_files": payload.get("changed_files", []),
                "context_files": payload.get("context_files", []),
                "output_files": payload.get("output_files", []),
                "related_files": payload.get("related_files", []),
                "commands": [],
                "test_items": payload.get("test_items", [f"验证任务结果：{payload['title']}"]),
                "test_commands": payload.get("test_commands", []),
                "test_scripts": payload.get("test_scripts", []),
                "manual_checks": payload.get("manual_checks", [f"人工确认：{payload['title']} 已符合需求"]),
                "verifications": [],
                "note": payload.get("note", ""),
                "business_rules": payload.get("business_rules", []),
                "formal_gate": (True if payload.get("coverage_acceptance") else payload.get("formal_gate", True)),
                "coverage_acceptance_explicit": "coverage_acceptance" in payload,
            }
            for field in [
                "requirement_version",
                "design_version",
                "test_matrix_version",
                "coverage_points",
                "coverage_change_ids",
                "coverage_acceptance",
                "coverage_tests",
                "feedback_contract_version",
                "feedback_state",
                "acceptance_feedback",
                "formal_requirement_refs",
                "formal_design_refs",
                "formal_test_refs",
                "out_of_scope",
                "test_suggestions",
                "task_kind",
                "model_tier",
                "query_terms",
                "symbols",
                "output_symbols",
                "output_search_terms",
                "replaces",
                "applies_to",
                "lesson_ids",
            ]:
                if field in payload:
                    task[field] = payload[field]
            requirement["tasks"].append(task)
            bind_tasks_to_current_contract(requirement, [task], force=False)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "task_updated":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            task = next((item for item in requirement["tasks"] if item["task_id"] == task_id), None)
            if task is None:
                continue
            task["status"] = payload.get("status", task["status"])
            task["note"] = payload.get("note", task.get("note", ""))
            task["changed_files"] = unique_extend(task["changed_files"], payload.get("changed_files", []))
            task["context_files"] = unique_extend(task.get("context_files", []), payload.get("context_files", []))
            task["output_files"] = unique_extend(task.get("output_files", []), payload.get("output_files", []))
            task["related_files"] = unique_extend(task.get("related_files", []), payload.get("related_files", []))
            task["commands"] = unique_extend(task["commands"], payload.get("commands", []))
            task["test_items"] = unique_extend(task.get("test_items", []), payload.get("test_items", []))
            test_commands_mode = payload.get("test_commands_mode", "append")
            if test_commands_mode == "clear":
                task["test_commands"] = []
            elif test_commands_mode == "replace":
                task["test_commands"] = unique_extend([], payload.get("test_commands", []))
            else:
                task["test_commands"] = unique_extend(task.get("test_commands", []), payload.get("test_commands", []))
            test_scripts_mode = payload.get("test_scripts_mode", "append")
            if test_scripts_mode == "clear":
                task["test_scripts"] = []
            elif test_scripts_mode == "replace":
                task["test_scripts"] = unique_extend([], payload.get("test_scripts", []))
            else:
                task["test_scripts"] = unique_extend(task.get("test_scripts", []), payload.get("test_scripts", []))
            task["manual_checks"] = unique_extend(task.get("manual_checks", []), payload.get("manual_checks", []))
            task["business_rules"] = unique_extend(task.get("business_rules", []), payload.get("business_rules", []))
            if payload.get("acceptance_feedback") is not None:
                # 验收反馈是带状态和替代关系的合同，必须整体替换，不能像普通文字列表一样追加。
                task["acceptance_feedback"] = [
                    item for item in payload.get("acceptance_feedback", []) if isinstance(item, dict)
                ]
                # 历史事件缺少反馈合同时保持缺失，不能根据反馈数组替它补成有效合同。
                task["feedback_contract_version"] = payload.get("feedback_contract_version", "")
                task["feedback_state"] = payload.get("feedback_state", "")
            if payload.get("subtasks") is not None:
                task["subtasks"] = payload.get("subtasks", task.get("subtasks", []))
            task["updated_at"] = event["created_at"]
            task["_updated_seq"] = event_index
            for verification in payload.get("verifications", []):
                normalized = {
                    "verification_id": verification["verification_id"],
                    "requirement_id": requirement_id,
                    "task_id": task_id,
                    "created_at": verification["created_at"],
                    "summary": verification["summary"],
                    "file_path": verification["file_path"],
                    "type": verification.get("type", "automated"),
                    "status": verification.get("status", "passed"),
                }
                task["verifications"].append(normalized)
                verifications.append(normalized)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "capture_recorded":
            capture = {
                "capture_id": payload["capture_id"],
                "created_at": event["created_at"],
                "task_id": task_id or payload.get("task_id", ""),
                "summary": payload["summary"],
                "note": payload.get("note", ""),
                "status": payload.get("status", "pending"),
                "requirement_id": requirement_id,
                "target_type": payload.get("target_type", "capture"),
                "draft_id": payload.get("draft_id", ""),
                "changed_files": payload.get("changed_files", []),
                "commands": payload.get("commands", []),
                "questions": payload.get("questions", []),
                "linked_change_id": payload.get("linked_change_id"),
                "file_path": payload["file_path"],
            }
            captures.append(capture)
            requirement = requirements.get(requirement_id)
            if requirement is not None:
                requirement["captures"].append(capture)
                requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "grill_recorded":
            grill = {
                "grill_id": payload["grill_id"],
                "created_at": event["created_at"],
                "requirement_id": requirement_id,
                "task_id": task_id or payload.get("task_id", ""),
                "mode": payload.get("mode", "requirement"),
                "status": payload.get("status", "resolved"),
                "summary": payload.get("summary", ""),
                "questions": payload.get("questions", []),
                "answers": payload.get("answers", []),
                "recommendation": payload.get("recommendation", ""),
                "source": payload.get("source", ""),
                "file_path": payload["file_path"],
            }
            grills.append(grill)
            requirement = requirements.get(requirement_id)
            if requirement is not None:
                requirement["grills"].append(grill)
                requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "capture_linked":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            linked_ids = set(payload.get("capture_ids", []))
            for capture in captures:
                if capture["capture_id"] not in linked_ids:
                    continue
                capture["status"] = "linked"
                capture["target_type"] = payload.get("target_type", "decision")
                capture["requirement_id"] = requirement_id
                if capture not in requirement["captures"]:
                    requirement["captures"].append(capture)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "material_recorded":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            material = {
                "material_id": payload["material_id"],
                "requirement_id": requirement_id,
                "created_at": event["created_at"],
                "title": payload.get("title", payload.get("summary", "")),
                "material_type": payload.get("material_type", "other"),
                "summary": payload.get("summary", ""),
                "scope": payload.get("scope", []),
                "task_ids": payload.get("task_ids", []),
                "changed_files": payload.get("changed_files", []),
                "asset_files": payload.get("asset_files", []),
                "commands": payload.get("commands", []),
                "source": payload.get("source", ""),
                "status": payload.get("status", "active"),
                "file_path": payload["file_path"],
            }
            materials.append(material)
            requirement["materials"].append(material)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "change_recorded":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            status = payload.get("status", "draft")
            change = {
                "change_id": payload["change_id"],
                "requirement_id": requirement_id,
                "created_at": event["created_at"],
                "_created_seq": event_index,
                "_updated_seq": event_index,
                "accepted_at": payload.get("accepted_at", ""),
                "summary": payload["summary"],
                "description": payload.get("description", payload["summary"]),
                "reason": payload.get("reason", ""),
                "status": status,
                "confirmation": payload.get("confirmation", "待确认"),
                "changed_task_ids": payload.get("changed_task_ids", []),
                "added_tasks": payload.get("added_tasks", []),
                "closed_task_ids": payload.get("closed_task_ids", []),
                "acceptance_points": payload.get("acceptance_points", []),
                "planned_task_ids": payload.get("planned_task_ids", []),
                "coverage": payload.get("coverage", []),
                "planning_status": payload.get("planning_status", ""),
                "capture_ids": payload.get("capture_ids", []),
                "file_path": payload["file_path"],
            }
            requirement["changes"].append(change)
            changes.append(change)
            requirement["priority"] = payload.get("priority", requirement["priority"])
            requirement["blocked_reason"] = payload.get(
                "blocked_reason",
                "待确认需求变化" if status == "draft" else "待处理需求变化",
            )
            if status in {"pending", "effective"}:
                for task in requirement["tasks"]:
                    if task["task_id"] in change["changed_task_ids"] and task["status"] not in {"done", "closed"}:
                        task["status"] = "changed"
                        task["note"] = (task.get("note", "") + "\n" if task.get("note") else "") + f"变更待处理：{change['summary']}"
                        task["updated_at"] = event["created_at"]
                        task["_updated_seq"] = event_index
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "change_accepted":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            if payload.get("transaction_id"):
                # 结构化 CHG 的任务计划已经随同一事务提交，不再进入旧的
                # “已生效待规划”分支；目标版本直接来自事务事件的显式字段。
                requirement["_formal_target_version"] = int(
                    payload.get("target_version") or 1
                )
                requirement["blocked_reason"] = ""
                requirement["updated_at"] = event["created_at"]
                continue
            accepted_ids = set(payload.get("change_ids", []))
            for change in requirement["changes"]:
                if change["change_id"] not in accepted_ids:
                    continue
                change["status"] = payload.get("status", "effective")
                change["confirmation"] = payload.get("confirmation", "已确认")
                change["accepted_at"] = payload.get("accepted_at", event["created_at"])
                if "planning_status" in payload:
                    change["planning_status"] = payload.get("planning_status", "")
                elif change_waits_for_model_plan(change, requirement["tasks"]):
                    change["planning_status"] = "needs_model_plan"
                change["_updated_seq"] = event_index
                for task in requirement["tasks"]:
                    if task["task_id"] in change.get("changed_task_ids", []) and task["status"] not in {"done", "closed"}:
                        task["status"] = "changed"
                        task["note"] = (task.get("note", "") + "\n" if task.get("note") else "") + f"变更已生效待规划：{change['summary']}"
                        task["updated_at"] = event["created_at"]
                        task["_updated_seq"] = event_index
            requirement["blocked_reason"] = payload.get("blocked_reason", "待规划已生效需求变化")
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "change_model_plan_recorded":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            target_change_id = payload.get("change_id")
            change = next(
                (
                    item
                    for item in requirement["changes"]
                    if item["change_id"] == target_change_id
                ),
                None,
            )
            if change is None:
                continue
            existing_task_keys = {
                (str(item.get("title", "")), str(item.get("summary", "")))
                for item in change.get("added_tasks", [])
                if isinstance(item, dict)
            }
            for task in payload.get("added_tasks", []):
                if not isinstance(task, dict):
                    continue
                key = (str(task.get("title", "")), str(task.get("summary", "")))
                if key in existing_task_keys:
                    continue
                change.setdefault("added_tasks", []).append(task)
                existing_task_keys.add(key)
            change["acceptance_points"] = unique_extend(
                [str(item) for item in change.get("acceptance_points", [])],
                [str(item) for item in payload.get("acceptance_points", [])],
            )
            change["planning_status"] = payload.get("planning_status", "model_plan_ready")
            change["_updated_seq"] = event_index
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "design_recorded":
            requirement = requirements.get(requirement_id)
            design = {
                "design_id": payload["design_id"],
                "requirement_id": requirement_id,
                "draft_id": str(payload.get("draft_id") or ""),
                "created_at": event["created_at"],
                "updated_at": event["created_at"],
                "accepted_at": "",
                "title": build_design_title(payload.get("title") or payload["summary"]),
                "summary": payload["summary"],
                "details": payload.get("details", {}),
                "status": payload.get("status", "draft"),
                "file_path": requirement_design_file_path(requirement) if requirement is not None else payload["file_path"],
            }
            designs.append(design)
            if requirement is not None:
                requirement["designs"].append(design)
                requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "design_updated":
            target_id = payload.get("design_id")
            design = next((item for item in designs if item["design_id"] == target_id), None)
            if design is None or design["status"] == "accepted":
                continue
            design["summary"] = append_design_summary(design["summary"], payload.get("summary", ""))
            design["updated_at"] = event["created_at"]
            if "draft_id" in payload and str(payload.get("draft_id") or "").strip():
                design["draft_id"] = str(payload["draft_id"])
            if payload.get("title"):
                design["title"] = build_design_title(payload["title"])
            requirement = requirements.get(design.get("requirement_id"))
            if requirement is not None:
                requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "design_accepted":
            accepted_ids = set(payload.get("design_ids", []))
            for design in designs:
                if design["design_id"] not in accepted_ids:
                    continue
                design["status"] = "accepted"
                design["accepted_at"] = payload.get("accepted_at", event["created_at"])
                design["updated_at"] = event["created_at"]
                requirement = requirements.get(design.get("requirement_id"))
                if requirement is not None:
                    design["file_path"] = requirement_design_file_path(requirement)
                    if design not in requirement["designs"]:
                        requirement["designs"].append(design)
                    requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "design_linked":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            linked_ids = set(payload.get("design_ids", []))
            for design in designs:
                if design["design_id"] not in linked_ids:
                    continue
                design["requirement_id"] = requirement_id
                design["updated_at"] = event["created_at"]
                design["file_path"] = requirement_design_file_path(requirement)
                if design not in requirement["designs"]:
                    requirement["designs"].append(design)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type in {"task_plan_imported", "task_plan_revised"}:
            requirement = requirements.get(requirement_id)
            if requirement is None:
                raise SdlcError(
                    f"任务计划事件找不到正式需求：{requirement_id}。",
                    exit_code=1,
                )
            from codex_sdlc.core.task_contract import state_tasks_from_import_event
            from codex_sdlc.core.task_coverage_contract import (
                apply_task_coverage_to_state_tasks,
            )

            imported_tasks = state_tasks_from_import_event(event)
            coverage_contract = payload.get("task_coverage", {})
            if not isinstance(coverage_contract, Mapping):
                raise SdlcError("任务计划事件缺少结构化任务覆盖合同。", exit_code=1)
            apply_task_coverage_to_state_tasks(imported_tasks, coverage_contract)
            if event_type == "task_plan_imported" and requirement["tasks"]:
                raise SdlcError(
                    f"{requirement_id} 已有任务，不能用 task-plan.v2 事件直接改写。",
                    exit_code=1,
                )
            previous_tasks = {
                str(task.get("task_id") or ""): task
                for task in requirement["tasks"]
                if isinstance(task, Mapping)
            }
            if event_type == "task_plan_revised":
                previous_package_sha256 = str(
                    payload.get("previous_package_sha256") or ""
                )
                if (
                    not previous_tasks
                    or previous_package_sha256
                    != str(requirement.get("task_plan_package_sha256") or "")
                ):
                    raise SdlcError(
                        f"{requirement_id} 的任务计划修订事件没有接在当前正式整包之后。",
                        exit_code=1,
                    )
                started = [
                    task_id
                    for task_id, task in previous_tasks.items()
                    if str(task.get("status") or "") not in {"todo", "blocked"}
                ]
                if started:
                    raise SdlcError(
                        f"{requirement_id} 已有进入开发的任务，不能应用整包修订："
                        + "、".join(started)
                        + "。",
                        exit_code=1,
                    )
            for task in imported_tasks:
                previous_task = previous_tasks.get(str(task["task_id"]))
                task["created_at"] = (
                    previous_task.get("created_at", event["created_at"])
                    if isinstance(previous_task, Mapping)
                    else event["created_at"]
                )
                task["updated_at"] = event["created_at"]
                task["_created_seq"] = (
                    previous_task.get("_created_seq", event_index)
                    if isinstance(previous_task, Mapping)
                    else event_index
                )
                task["_updated_seq"] = event_index
            requirement["tasks"] = imported_tasks
            requirement["task_plan_contract"] = deepcopy(payload.get("task_plan", {}))
            requirement["task_coverage_contract"] = deepcopy(coverage_contract)
            requirement["task_plan_package_sha256"] = str(
                payload.get("package_sha256") or ""
            )
            requirement["task_quality"] = {
                "status": "passed",
                "warnings": [],
                "suggestions": [],
            }
            requirement["needs_prepare"] = False
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "plan_updated":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            native_start = requirement.get("native_start") or {}
            preserve_document_first_matrix = (
                isinstance(native_start, dict)
                and native_start.get("workflow_profile") == "document-first.v1"
                and not payload.get("test_cases")
                and bool(requirement.get("prepared_test_cases"))
            )
            existing_tasks = {item["task_id"]: item for item in requirement["tasks"]}
            rebuilt_tasks: list[dict[str, Any]] = []
            for raw_task in payload.get("tasks", []):
                existing_task = existing_tasks.get(raw_task["task_id"], {})
                coverage_tests = raw_task.get("coverage_tests", existing_task.get("coverage_tests"))
                if preserve_document_first_matrix and not coverage_tests:
                    # document-first 的正式包不把兼容测试矩阵内嵌到 native_start。
                    # 旧计划命令因此可能回写空数组；按任务已有 FR 重新绑定已登记
                    # 矩阵，避免关闭其他任务时顺带抹掉当前任务仍需回归的验收项。
                    coverage_points = {
                        str(item)
                        for item in raw_task.get(
                            "coverage_points", existing_task.get("coverage_points", [])
                        )
                    }
                    matched_tests = [
                        str(case.get("id"))
                        for case in requirement.get("prepared_test_cases", [])
                        if isinstance(case, dict)
                        and coverage_points.intersection(
                            str(item) for item in case.get("requirement_ids", [])
                        )
                    ]
                    coverage_tests = matched_tests or existing_task.get("coverage_tests")
                rebuilt_tasks.append(
                    {
                        "requirement_id": requirement_id,
                        "task_id": raw_task["task_id"],
                        "source_task_id": raw_task.get("source_task_id", existing_task.get("source_task_id", "")),
                        "subtasks": raw_task.get("subtasks", existing_task.get("subtasks", [])),
                        "title": raw_task["title"],
                        "summary": raw_task.get("summary", raw_task["title"]),
                        "status": raw_task.get("status", existing_task.get("status", "todo")),
                        "depends_on": raw_task.get("depends_on", existing_task.get("depends_on", [])),
                        "created_at": existing_task.get("created_at", event["created_at"]),
                        "updated_at": event["created_at"],
                        "_created_seq": existing_task.get("_created_seq", event_index),
                        "_updated_seq": event_index,
                        "changed_files": raw_task.get("changed_files", existing_task.get("changed_files", [])),
                        "context_files": raw_task.get("context_files", existing_task.get("context_files", [])),
                        "output_files": raw_task.get("output_files", existing_task.get("output_files", [])),
                        "related_files": raw_task.get("related_files", existing_task.get("related_files", [])),
                        "commands": raw_task.get("commands", existing_task.get("commands", [])),
                        "test_items": raw_task.get("test_items", existing_task.get("test_items", [f"验证任务结果：{raw_task['title']}"])),
                        "test_commands": raw_task.get("test_commands", existing_task.get("test_commands", [])),
                        "test_scripts": raw_task.get("test_scripts", existing_task.get("test_scripts", [])),
                        "manual_checks": raw_task.get("manual_checks", existing_task.get("manual_checks", [f"人工确认：{raw_task['title']} 已符合需求"])),
                        "verifications": raw_task.get("verifications", existing_task.get("verifications", [])),
                        "note": raw_task.get("note", existing_task.get("note", "")),
                        "business_rules": raw_task.get("business_rules", existing_task.get("business_rules", [])),
                        "requirement_version": raw_task.get("requirement_version", existing_task.get("requirement_version")),
                        "design_version": raw_task.get("design_version", existing_task.get("design_version")),
                        "test_matrix_version": raw_task.get("test_matrix_version", existing_task.get("test_matrix_version")),
                        "coverage_points": raw_task.get("coverage_points", existing_task.get("coverage_points")),
                        "coverage_change_ids": raw_task.get("coverage_change_ids", existing_task.get("coverage_change_ids", [])),
                        "coverage_acceptance": raw_task.get("coverage_acceptance", existing_task.get("coverage_acceptance")),
                        "coverage_tests": coverage_tests,
                        "feedback_contract_version": raw_task.get(
                            "feedback_contract_version",
                            "",
                        ),
                        "feedback_state": raw_task.get("feedback_state", ""),
                        "acceptance_feedback": raw_task.get("acceptance_feedback", existing_task.get("acceptance_feedback", [])),
                        "formal_requirement_refs": raw_task.get("formal_requirement_refs", existing_task.get("formal_requirement_refs")),
                        "formal_design_refs": raw_task.get("formal_design_refs", existing_task.get("formal_design_refs")),
                        "formal_test_refs": raw_task.get("formal_test_refs", existing_task.get("formal_test_refs")),
                        "out_of_scope": raw_task.get("out_of_scope", existing_task.get("out_of_scope")),
                        "test_suggestions": raw_task.get("test_suggestions", existing_task.get("test_suggestions")),
                        "task_kind": raw_task.get("task_kind", existing_task.get("task_kind", "generic")),
                        "model_tier": raw_task.get("model_tier", existing_task.get("model_tier", "medium")),
                        "query_terms": raw_task.get("query_terms", existing_task.get("query_terms", [])),
                        "symbols": raw_task.get("symbols", existing_task.get("symbols", [])),
                        "output_symbols": raw_task.get("output_symbols", existing_task.get("output_symbols", [])),
                        "output_search_terms": raw_task.get("output_search_terms", existing_task.get("output_search_terms", [])),
                        "replaces": raw_task.get("replaces", existing_task.get("replaces", [])),
                        "applies_to": raw_task.get("applies_to", existing_task.get("applies_to", [])),
                        "lesson_ids": raw_task.get("lesson_ids", existing_task.get("lesson_ids", [])),
                        "formal_gate": raw_task.get("formal_gate", existing_task.get("formal_gate", True)),
                        "coverage_acceptance_explicit": raw_task.get("coverage_acceptance_explicit", existing_task.get("coverage_acceptance_explicit", "coverage_acceptance" in raw_task)),
                    }
                )
            requirement["tasks"] = rebuilt_tasks
            requirement["priority"] = payload.get("priority", requirement.get("priority", "normal"))
            requirement["blocked_reason"] = payload.get("blocked_reason", "")
            requirement["task_quality"] = payload.get("task_quality", requirement.get("task_quality", {}))
            requirement["preflight_tasks"] = payload.get("preflight_tasks", requirement.get("preflight_tasks", []))
            requirement["needs_prepare"] = payload.get("needs_prepare", False)
            if "test_cases" in payload and not preserve_document_first_matrix:
                requirement["prepared_test_cases"] = payload.get("test_cases", [])
                requirement["prepared_test_matrix_version"] = payload.get("test_matrix_version", "")
            resolved_change_ids = set(payload.get("resolved_change_ids", []))
            for change in requirement["changes"]:
                if change["change_id"] in resolved_change_ids:
                    change["status"] = "resolved"
            for planned_change in payload.get("planned_changes", []):
                change = next(
                    (
                        item
                        for item in requirement["changes"]
                        if item["change_id"] == planned_change.get("change_id")
                    ),
                    None,
                )
                if change is None:
                    continue
                change["status"] = planned_change.get("status", "planned")
                change["planned_task_ids"] = planned_change.get("task_ids", [])
                change["coverage"] = planned_change.get("coverage", [])
                change["planning_status"] = ""
            bind_tasks_to_current_contract(requirement, requirement["tasks"], force=False)
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "requirement_accepted":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            requirement["accepted_at"] = payload.get("accepted_at", event["created_at"])
            requirement["_accepted_seq"] = event_index
            requirement["blocked_reason"] = ""
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "requirement_docs_created":
            requirement = requirements.get(requirement_id)
            if requirement is None:
                continue
            requirement.setdefault("docs", []).append(
                {
                    "file_path": payload.get("file_path", ""),
                    "created_at": payload.get("created_at", event["created_at"]),
                    "_created_seq": event_index,
                    "summary": event.get("summary", ""),
                }
            )
            requirement["updated_at"] = event["created_at"]
            continue

        if event_type == "session_finished":
            sessions.append(payload)

    for requirement in requirements.values():
        tasks = requirement["tasks"]
        task_statuses = {task["task_id"]: task["status"] for task in tasks}
        for change in requirement["changes"]:
            planned_task_ids = change.get("planned_task_ids", [])
            if change["status"] == "planned" and planned_task_ids:
                if all(task_statuses.get(task_id) in {"done", "closed"} for task_id in planned_task_ids):
                    change["status"] = "resolved"
        draft_changes = [item for item in requirement["changes"] if item["status"] == "draft"]
        effective_changes = [item for item in requirement["changes"] if item["status"] in {"effective", "pending"}]
        open_changes = draft_changes + effective_changes
        if draft_changes and not requirement["blocked_reason"]:
            requirement["blocked_reason"] = "待确认需求变化"
        elif effective_changes and not requirement["blocked_reason"]:
            requirement["blocked_reason"] = "待规划已生效需求变化"
        acceptance_current = requirement_acceptance_is_current(requirement)
        if not tasks:
            if open_changes:
                requirement["status"] = "changed"
            else:
                requirement["status"] = "accepted" if acceptance_current else "planning"
        else:
            open_tasks = [task for task in tasks if task["status"] != "closed"]
            statuses = {task["status"] for task in open_tasks} if open_tasks else {"closed"}
            if open_changes or "changed" in statuses or "test_failed" in statuses:
                requirement["status"] = "changed"
            elif all(task["status"] in {"done", "closed"} for task in tasks):
                requirement["status"] = "accepted" if acceptance_current else "done"
            elif "doing" in statuses or "ready_for_user_check" in statuses:
                requirement["status"] = "doing"
            else:
                requirement["status"] = "active"
        apply_structured_requirement_model(requirement)
        task_plan = requirement.get("task_plan_contract")
        evidence = task_plan.get("code_evidence") if isinstance(task_plan, Mapping) else None
        if (
            isinstance(evidence, Mapping)
            and evidence.get("schema_version") == "code-evidence.v1"
            and evidence.get("purpose") == "task_planning"
        ):
            from codex_sdlc.core.code_evidence import (
                assess_task_planning_code_evidence,
            )

            assessment = assess_task_planning_code_evidence(
                paths,
                evidence,
                tasks=requirement["tasks"],
            )
            requirement["task_planning_code_evidence_state"] = deepcopy(assessment)
            requirement["task_planning_evidence_status"] = assessment["status"]
        else:
            # T-021 之前的任务包可以继续只读；新规划证据不会从旧 formal_* 字段补造。
            requirement["task_planning_code_evidence_state"] = None
            requirement["task_planning_evidence_status"] = "not_recorded"
        if isinstance(task_plan, Mapping):
            # 文档优先整套任务只认受签名的 task_plan 审核登记。手工事件、任务标题、
            # 任务数量和旧 task_quality 提醒都不能把需求推进到待开发。
            from codex_sdlc.services.review_service import (
                task_plan_review_status,
            )

            task_review = task_plan_review_status(
                paths,
                requirement_id=str(requirement.get("requirement_id") or ""),
                tasks=tasks,
            )
            requirement["task_plan_review_state"] = deepcopy(task_review)
            before_development = bool(tasks) and all(
                str(task.get("status") or "") in {"todo", "blocked"}
                for task in tasks
            )
            has_task_blocker = any(
                task.get("blocking_conditions")
                or str(task.get("status") or "") == "blocked"
                for task in tasks
            )
            if before_development and not open_changes:
                if (
                    task_review.get("can_advance") is True
                    and requirement["task_planning_evidence_status"] == "current"
                    and not has_task_blocker
                ):
                    requirement["status"] = "ready_for_development"
                else:
                    requirement["status"] = "planning_tasks"
            # task-plan.v2 从这里开始只使用直接执行主线的五个需求状态。旧事件仍按
            # 原有兼容分支读取，但不能参与当前任务状态计算。
            requirement["status"] = project_direct_requirement_status(
                requirement,
                acceptance_current=acceptance_current,
                has_open_changes=bool(open_changes),
            )

    active_requirements = [item for item in requirements.values() if item["status"] not in {"done", "accepted"}]
    done_tasks = sum(1 for requirement in requirements.values() for task in requirement["tasks"] if task["status"] == "done")
    closed_tasks = sum(1 for requirement in requirements.values() for task in requirement["tasks"] if task["status"] == "closed")
    finished_tasks = done_tasks + closed_tasks
    all_tasks = sum(len(requirement["tasks"]) for requirement in requirements.values())
    verified_tasks = sum(
        1
        for requirement in requirements.values()
        for task in requirement["tasks"]
        if task["status"] == "done" and task["verifications"]
    )
    pending_capture_files = draft_ownership.pending_capture_files(paths.root, captures)
    draft_change_files = [paths.root / item["file_path"] for item in changes if item["status"] == "draft"]
    effective_change_files = [paths.root / item["file_path"] for item in changes if item["status"] in {"effective", "pending"}]
    pending_change_files = draft_change_files + effective_change_files
    known_capture_files = {paths.root / item["file_path"] for item in captures}
    known_change_files = {paths.root / item["file_path"] for item in changes}
    if paths.captures_dir.exists():
        for file_path in sorted(paths.captures_dir.glob("*.md")):
            if file_path not in pending_capture_files and file_path not in known_capture_files:
                pending_capture_files.append(file_path)
    if paths.changes_dir.exists():
        for file_path in sorted(paths.changes_dir.glob("*.md")):
            if file_path not in pending_change_files and file_path not in known_change_files:
                pending_change_files.append(file_path)
    if paths.requirements_dir.exists():
        for file_path in sorted(paths.requirements_dir.glob("*/changes/*.md")):
            if file_path not in pending_change_files and file_path not in known_change_files:
                pending_change_files.append(file_path)
    recent_decision_files = []
    if paths.decisions_dir.exists():
        recent_decision_files.extend(sorted(paths.decisions_dir.glob("*.md")))
    if paths.requirements_dir.exists():
        recent_decision_files.extend(sorted(paths.requirements_dir.glob("*/decisions.md")))
    recent_decision_files = recent_decision_files[-3:]

    codex_asset_status = get_project_codex_asset_status(paths.root)
    project_data.update(codex_asset_status)

    # 新状态只认结构化资料、需求产物、CAP、DEC 和问题；旧 DRAFT 仍按旧合同只读展示。
    for draft in drafts.values():
        if draft_lifecycle.is_started_draft(draft):
            # 已建档 DRAFT 仍会为任务合同和运行读取清单提供受控设计产物。读取这些
            # 产物时必须保留已经验签的需求确认，不能因为状态是 started 就把
            # 确认信息从内存投影中删掉。
            from codex_sdlc.services.draft_service import requirement_confirmation_status
            from codex_sdlc.services.review_service import requirement_review_status

            requirement_review = requirement_review_status(
                paths,
                draft_id=str(draft.get("draft_id") or ""),
            )
            draft["_requirement_review_state"] = deepcopy(requirement_review)
            draft["_requirement_confirmation_state"] = deepcopy(
                requirement_confirmation_status(
                    paths,
                    draft_id=str(draft.get("draft_id") or ""),
                    draft=draft,
                    review_state=requirement_review,
                )
            )
            continue
        statuses = draft.get("capture_statuses")
        transition_keys = [
            str(item.get("transition_key") or "")
            for item in _mapping_records(draft.get("capture_transitions"))
        ]
        if "" in transition_keys or len(set(transition_keys)) != len(transition_keys):
            raise SdlcError("CAP 状态转换的 transition_key 缺失或重复。")
        for initial in _mapping_records(draft.get("structured_captures")):
            capture_id = str(initial.get("capture_id") or "")
            global_records = [
                item for item in captures if item.get("capture_id") == capture_id
            ]
            effective_status = (
                statuses.get(capture_id)
                if isinstance(statuses, dict)
                else initial.get("status")
            )
            if (
                len(global_records) != 1
                or canonical_sha256(global_records[0].get("structured_increment"))
                != canonical_sha256(initial)
                or global_records[0].get("status") != effective_status
            ):
                raise SdlcError(
                    f"{capture_id} 的全局 CAP 状态与 DRAFT 结构化事实不一致。"
                )
        # 事件只保存回执编号和审计快照。每次读取状态都从项目受管登记表重验签名、
        # 项目、DRAFT、入口和目标，登记表丢失时不能继续沿用旧的 trusted=true。
        from codex_sdlc.services.draft_service import resolve_draft_review_receipt

        draft["_verified_review_receipt"] = resolve_draft_review_receipt(paths, draft)
        draft["_material_integrity_issues"] = _draft_material_integrity_issues(
            paths, draft
        )
        draft["_reference_issues"] = _draft_reference_issues(paths, draft)
        from codex_sdlc.services.draft_service import (
            requirement_confirmation_status,
        )
        from codex_sdlc.services.review_service import (
            integrated_design_review_status,
            requirement_review_status,
        )

        requirement_review = requirement_review_status(
            paths,
            draft_id=str(draft.get("draft_id") or ""),
        )
        draft["_requirement_review_state"] = deepcopy(requirement_review)
        requirement_confirmation = requirement_confirmation_status(
            paths,
            draft_id=str(draft.get("draft_id") or ""),
            draft=draft,
            review_state=requirement_review,
        )
        draft["_requirement_confirmation_state"] = deepcopy(
            requirement_confirmation
        )
        if draft_lifecycle.uses_structured_requirement_stage(draft):
            # 设计计划没有写入旧 DRAFT 主事件对象，状态层必须显式从同一事件集合重建，
            # 否则刷新后会退回只看 design.draft.md 的旧章节门禁。
            design_stage, design_plan = _draft_design_stage(
                paths,
                draft,
                events,
            )
            draft["design_stage"] = design_stage
            draft["_design_plan_record"] = deepcopy(design_plan)
            draft["_design_plan_enabled"] = design_plan is not None
            # 整体设计审核必须在设计输入重建完成后计算。生命周期只消费这份
            # 结构化状态，不能从 Markdown 文字或旧的 passed 结论猜是否可开工。
            draft["_integrated_design_review_state"] = deepcopy(
                integrated_design_review_status(
                    paths,
                    draft_id=str(draft.get("draft_id") or ""),
                )
            )
        has_assessable_content = bool(
            draft_lifecycle.uses_structured_requirement_stage(draft)
            or str(draft.get("requirement_body") or "").strip()
            or str(draft.get("design_body") or "").strip()
            or draft.get("questions")
        )
        if has_assessable_content:
            assessment = draft_lifecycle.assess_draft(draft)
            draft["status"] = assessment.effective_status
            assessment_payload = asdict(assessment)
            assessment_payload["lost_facts"] = [str(item) for item in assessment.lost_facts]
            # 内存状态与 status.json 都使用 JSON 数组，避免同一结果在命令层是 tuple、投影里是 list。
            draft["assessment"] = json.loads(
                json.dumps(assessment_payload, ensure_ascii=False)
            )
        else:
            assessment = None
        blocked_material_ids = []
        if assessment is not None:
            blocked_material_ids = sorted(
                {
                    item.source_id
                    for item in assessment.blockers
                    if item.code in {"material_missing", "material_unstable"}
                    and item.source_id.startswith("MAT-")
                }
            )
        has_material_blocker = bool(
            assessment is not None
            and any(
                item.code in {"material_missing", "material_unstable"}
                for item in assessment.blockers
            )
        )
        draft["material_gate"] = {
            "status": "blocked" if has_material_blocker else "ready",
            "can_review": not has_material_blocker,
            "blocking_material_ids": blocked_material_ids,
        }
        if has_material_blocker:
            # 旧模型回执不能越过新的资料证据门禁；状态本身仍由上面的统一 assessment 给出。
            draft["model_review"] = None
            draft["review_receipt"] = None
            draft["_verified_review_receipt"] = None

    # 通用审核状态只从受签名登记和显式依赖图计算；具体需求、设计和任务门禁由后续接入任务消费。
    from codex_sdlc.services.review_service import review_status

    structured_review_state = review_status(paths)

    design_references = [
        deepcopy(reference)
        for draft in drafts.values()
        for reference in draft.get("design_references", [])
        if isinstance(reference, dict)
    ]
    design_artifacts = [
        deepcopy(artifact)
        for draft in drafts.values()
        for artifact in draft.get("design_artifacts", [])
        if isinstance(artifact, dict)
    ]
    design_summaries = [
        deepcopy(summary)
        for draft in drafts.values()
        for summary in draft.get("design_summaries", [])
        if isinstance(summary, dict)
    ]

    return {
        "project": project_data,
        "events": events,
        "requirements": requirements,
        "active_requirements": active_requirements,
        "sessions": sessions,
        "recent_session": sessions[-1] if sessions else None,
        "verifications": verifications,
        "captures": captures,
        "grills": grills,
        "materials": materials,
        "changes": changes,
        "designs": designs,
        "design_references": design_references,
        "design_artifacts": design_artifacts,
        "design_summaries": design_summaries,
        "drafts": drafts,
        "review_state": structured_review_state,
        "counts": {
            "all_tasks": all_tasks,
            "done_tasks": done_tasks,
            "closed_tasks": closed_tasks,
            "finished_tasks": finished_tasks,
            "verified_tasks": verified_tasks,
        },
        "pending_capture_files": pending_capture_files,
        "pending_change_files": pending_change_files,
        "draft_change_files": draft_change_files,
        "effective_change_files": effective_change_files,
        "recent_decision_files": recent_decision_files,
        "git_status_lines": current_git_status_lines(paths.root),
        "git_changed_files": current_git_changed_files(paths.root),
    }


def inspect_materialized_state(paths: ProjectPaths, state: dict[str, Any]) -> tuple[list[str], list[str]]:
    passed: list[str] = []
    issues: list[str] = []
    has_direct_task_mainline = any(
        isinstance(requirement.get("task_plan_contract"), Mapping)
        for requirement in state.get("requirements", {}).values()
    )
    live_material_drift = any(
        draft.get("_material_integrity_issues")
        for draft in state.get("drafts", {}).values()
        if isinstance(draft, dict)
    )

    orphan_change_count = max(
        0,
        len(state.get("pending_change_files", []))
        - len(state.get("draft_change_files", []))
        - len(state.get("effective_change_files", [])),
    )
    if orphan_change_count:
        issues.append(f"还有 {orphan_change_count} 个未纳入状态的变更文件")

    if paths.current_md.exists():
        passed.append("current.md 可读")
        current_text = paths.current_md.read_text(encoding="utf-8")
        if state["active_requirements"]:
            for requirement in state["active_requirements"]:
                if requirement["requirement_id"] not in current_text:
                    issues.append(f"current.md 里缺少 {requirement['requirement_id']} 快照")
                    break
        elif "当前没有活跃需求" not in current_text:
            issues.append("current.md 没有反映“当前没有活跃需求”")
        current_next = extract_current_next_snapshot(current_text)
        expected_next = compute_next_actions(paths, state)
        if current_next is None:
            issues.append("current.md 缺少下一步推荐快照")
        else:
            if (
                not has_direct_task_mainline
                and not live_material_drift
                and current_next["primary"] != expected_next["primary"]
            ):
                issues.append("current.md 里的下一步主推荐已过期")
            if (
                not has_direct_task_mainline
                and not live_material_drift
                and current_next["alternatives"] != expected_next["alternatives"]
            ):
                issues.append("current.md 里的下一步备选指令已过期")
    else:
        issues.append("current.md 缺失")

    if not paths.database_file.exists():
        issues.append("sdlc.db 缺失")
        return passed, issues

    try:
        with sqlite3.connect(paths.database_file) as connection:
            requirement_count = connection.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
            task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            design_count = connection.execute("SELECT COUNT(*) FROM designs").fetchone()[0]
        passed.append("sdlc.db 可读")
        if requirement_count != len(state["requirements"]):
            issues.append("sdlc.db 里的需求数量和事件记录不一致")
        if task_count != state["counts"]["all_tasks"]:
            issues.append("sdlc.db 里的任务数量和事件记录不一致")
        if design_count != len(state["designs"]):
            issues.append("sdlc.db 里的技术方案数量和事件记录不一致")
    except sqlite3.Error as exc:
        issues.append(f"sdlc.db 无法读取：{exc}")

    for design in state.get("designs", []):
        global_design_file = paths.designs_dir / f"{design['design_id']}.md"
        if design.get("requirement_id") and global_design_file.exists():
            issues.append(f"关联到需求的技术方案仍有全局副本：{relative_to_project(paths.root, global_design_file)}")
        design_file = paths.root / design["file_path"]
        if not design_file.exists():
            issues.append(f"技术方案快照缺失：{relative_to_project(paths.root, design_file)}")

    legacy_state = dict(state)
    # task-plan.v2 的覆盖、引用和完整输入已经由整套任务审核固定。status/next
    # 不能再用旧开工阶段的覆盖提示盖过正式审核状态，只对旧任务保留体检提示。
    legacy_requirements = {
        requirement_id: requirement
        for requirement_id, requirement in state.get("requirements", {}).items()
        if not isinstance(requirement.get("task_plan_contract"), Mapping)
    }
    legacy_state["requirements"] = legacy_requirements
    legacy_state["active_requirements"] = [
        requirement
        for requirement in state["active_requirements"]
        if not isinstance(requirement.get("task_plan_contract"), Mapping)
    ]
    contract_lines = task_contract_issue_lines(legacy_state)
    if contract_lines:
        issues.append(
            "任务版本或覆盖关系异常："
            + "；".join(contract_lines)
            + "；请先用 `$sdlc-doctor-repair` 核对旧任务快照。"
        )
    else:
        passed.append("任务版本和覆盖关系已对齐")

    return passed, issues


def extract_current_next_snapshot(current_text: str) -> dict[str, Any] | None:
    marker = "## 建议下一步"
    if marker not in current_text:
        return None
    section = current_text.split(marker, 1)[1].split("\n## ", 1)[0]
    primary = ""
    alternatives: list[str] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if line.startswith("- 主推荐："):
            primary = line.removeprefix("- 主推荐：").strip()
        elif line.startswith("- 备选："):
            alternatives.append(line.removeprefix("- 备选：").strip())
    if not primary:
        return None
    return {"primary": primary, "alternatives": alternatives}


def rebuild_database(paths: ProjectPaths, state: dict[str, Any]) -> None:
    ensure_base_dirs(paths)
    with sqlite3.connect(paths.database_file) as connection:
        for table in [
            "meta",
            "events",
            "requirements",
            "tasks",
            "sessions",
            "verifications",
            "captures",
            "grills",
            "materials",
            "changes",
            "designs",
            "drafts",
        ]:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.executescript(SCHEMA_SQL)

        project = state["project"]
        for key, value in {
            "project_name": project.get("project_name", paths.root.name),
            "project_path": project.get("project_path", str(paths.root)),
            "project_type": project.get("project_type", "unknown"),
        }.items():
            connection.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

        for event in state["events"]:
            connection.execute(
                """
                INSERT INTO events(event_id, event_type, project_path, requirement_id, task_id, created_at, source, summary, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["event_type"],
                    event["project_path"],
                    event.get("requirement_id"),
                    event.get("task_id"),
                    event["created_at"],
                    event["source"],
                    event["summary"],
                    json.dumps(event["payload"], ensure_ascii=False),
                ),
            )

        for requirement in state["requirements"].values():
            connection.execute(
                """
                INSERT INTO requirements(
                    requirement_id, title, folder_name, status, priority, blocked_reason, flow_type, created_at, updated_at, summary, description
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement["requirement_id"],
                    requirement["title"],
                    requirement["folder_name"],
                    requirement["status"],
                    requirement.get("priority", "normal"),
                    requirement.get("blocked_reason", ""),
                    requirement["flow_type"],
                    requirement["created_at"],
                    requirement["updated_at"],
                    requirement["summary"],
                    requirement["description"],
                ),
            )
            for task in requirement["tasks"]:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        requirement_id, task_id, source_task_id, subtasks_json, title, status, depends_on_json, changed_files_json, commands_json,
                        test_items_json, test_commands_json, test_scripts_json, manual_checks_json, verifications_json, created_at, updated_at, summary, note
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        requirement["requirement_id"],
                        task["task_id"],
                        task.get("source_task_id", ""),
                        json.dumps(task.get("subtasks", []), ensure_ascii=False),
                        task["title"],
                        task["status"],
                        json.dumps(task["depends_on"], ensure_ascii=False),
                        json.dumps(task["changed_files"], ensure_ascii=False),
                        json.dumps(task["commands"], ensure_ascii=False),
                        json.dumps(task.get("test_items", []), ensure_ascii=False),
                        json.dumps(task.get("test_commands", []), ensure_ascii=False),
                        json.dumps(task.get("test_scripts", []), ensure_ascii=False),
                        json.dumps(task.get("manual_checks", []), ensure_ascii=False),
                        json.dumps(task["verifications"], ensure_ascii=False),
                        task["created_at"],
                        task["updated_at"],
                        task["summary"],
                        task.get("note", ""),
                    ),
                )

        for session in state["sessions"]:
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, created_at, summary, next_step, related_requirements_json, related_tasks_json,
                    changed_files_json, commands_json, verifications_json, unresolved_issues_json, suggested_commit, file_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    session["created_at"],
                    session["summary"],
                    session["next_step"],
                    json.dumps(session["related_requirements"], ensure_ascii=False),
                    json.dumps(session["related_tasks"], ensure_ascii=False),
                    json.dumps(session["changed_files"], ensure_ascii=False),
                    json.dumps(session["commands"], ensure_ascii=False),
                    json.dumps(session["verifications"], ensure_ascii=False),
                    json.dumps(session["unresolved_issues"], ensure_ascii=False),
                    session["suggested_commit"],
                    session["file_path"],
                ),
            )

        for capture in state["captures"]:
            connection.execute(
                """
                INSERT INTO captures(capture_id, created_at, summary, status, file_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    capture["capture_id"],
                    capture["created_at"],
                    capture["summary"],
                    capture["status"],
                    capture["file_path"],
                ),
            )

        for grill in state["grills"]:
            connection.execute(
                """
                INSERT INTO grills(grill_id, requirement_id, task_id, mode, status, created_at, summary, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    grill["grill_id"],
                    grill.get("requirement_id"),
                    grill.get("task_id", ""),
                    grill.get("mode", ""),
                    grill.get("status", ""),
                    grill["created_at"],
                    grill.get("summary", ""),
                    grill["file_path"],
                ),
            )

        for material in state["materials"]:
            connection.execute(
                """
                INSERT INTO materials(material_id, requirement_id, created_at, title, material_type, status, file_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    material["material_id"],
                    material["requirement_id"],
                    material["created_at"],
                    material["title"],
                    material["material_type"],
                    material["status"],
                    material["file_path"],
                ),
            )

        for change in state["changes"]:
            connection.execute(
                """
                INSERT INTO changes(change_id, requirement_id, created_at, summary, status, file_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    change["change_id"],
                    change["requirement_id"],
                    change["created_at"],
                    change["summary"],
                    change["status"],
                    change["file_path"],
                ),
            )

        for design in state["designs"]:
            connection.execute(
                """
                INSERT INTO designs(
                    design_id, requirement_id, created_at, updated_at, accepted_at, title, summary, status, file_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    design["design_id"],
                    design.get("requirement_id"),
                    design["created_at"],
                    design["updated_at"],
                    design.get("accepted_at", ""),
                    design["title"],
                    design["summary"],
                    design["status"],
                    design["file_path"],
                ),
            )

        for draft in state.get("drafts", {}).values():
            connection.execute(
                """
                INSERT INTO drafts(
                    draft_id, status, title, requirement_summary, design_summary, requirement_body, design_body,
                    questions_json, decisions_json, review_items_json, started_requirement_id, created_at, updated_at, folder_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft["draft_id"],
                    draft["status"],
                    draft["title"],
                    draft.get("requirement_summary", ""),
                    draft.get("design_summary", ""),
                    draft.get("requirement_body", ""),
                    draft.get("design_body", ""),
                    json.dumps(draft.get("questions", []), ensure_ascii=False),
                    json.dumps(draft.get("decisions", []), ensure_ascii=False),
                    json.dumps(draft.get("review_items", []), ensure_ascii=False),
                    draft.get("started_requirement_id", ""),
                    draft["created_at"],
                    draft["updated_at"],
                    draft.get("folder_path", f".codex-sdlc/drafts/{draft['draft_id']}"),
                ),
            )

        for verification in state["verifications"]:
            connection.execute(
                """
                INSERT INTO verifications(verification_id, requirement_id, task_id, created_at, summary, file_path)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    verification["verification_id"],
                    verification["requirement_id"],
                    verification["task_id"],
                    verification["created_at"],
                    verification["summary"],
                    verification["file_path"],
                ),
            )


def write_project_snapshot(paths: ProjectPaths, state: dict[str, Any]) -> None:
    project = state["project"]
    scripts = project.get("detected_scripts", [])
    tests = project.get("test_commands", [])
    lines = [
        "# 项目总览",
        "",
        f"- 项目名称：{project.get('project_name', paths.root.name)}",
        f"- 项目路径：{project.get('project_path', str(paths.root))}",
        f"- 项目类型：{project.get('project_type', 'unknown')}",
        f"- Git 项目：{'是' if project.get('git_repo') else '否'}",
        f"- Git 全局忽略：{project.get('git_ignore_file') or '未配置'}",
        f"- `.codex-sdlc/` 忽略规则：{project.get('git_ignore_rule', '.codex-sdlc/')}",
        f"- `.codex-sdlc/` 当前已忽略：{'是' if project.get('sdlc_git_ignored') else '否'}",
        f"- Hooks：{'已就绪' if project.get('hooks_ready') else '未就绪'}",
        f"- Rules：{'已就绪' if project.get('rules_ready') else '未就绪'}",
        "",
        "## Git 策略",
        "- `.codex-sdlc/` 默认走本机全局忽略，不进项目 Git。",
    ]
    lines.extend(
        [
        "## Hooks 和 Rules",
        f"- hooks.json：{project.get('hooks_json_path') or '未生成'}",
        f"- rules 文件：{project.get('rules_file_path') or '未生成'}",
        "- 说明：Hooks 和 Rules 只做提醒和风险控制，不能替代沙箱、审批策略和人工确认。",
        "",
        "## 识别到的命令",
        ]
    )
    lines.extend([f"- {item}" for item in scripts] or ["- 暂未识别到项目脚本"])
    lines.extend(["", "## 建议测试命令"])
    lines.extend([f"- {item}" for item in tests] or ["- 请手动补充测试命令"])
    paths.project_md.write_text(join_lines(lines), encoding="utf-8")


def task_subtask_count(task: dict[str, Any]) -> int:
    return len(task.get("subtasks", []))


def requirement_subtask_count(requirement: dict[str, Any]) -> int:
    return sum(task_subtask_count(task) for task in requirement["tasks"])


def requirement_done_task_count(requirement: dict[str, Any]) -> int:
    return sum(1 for task in requirement["tasks"] if task["status"] == "done")


def display_task_title(task: dict[str, Any]) -> str:
    """给用户看的任务标题要保留业务目标，隐藏工具落盘细节。"""

    title = sanitize_runtime_text(str(task.get("title", "")).strip())
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title).strip(" ，,；;。")
    return title or str(task.get("title", "")).strip()


def task_display_line(task: dict[str, Any], *, show_subtasks: bool = False) -> str:
    line = f"- {task['task_id']} [{task['status']}] {display_task_title(task)}"
    if show_subtasks:
        count = task_subtask_count(task)
        if count:
            line += f"（含 {count} 个子检查项）"
    return line


def task_scope_count(task: dict[str, Any]) -> int:
    return len(set(str(item) for item in [*list_value(task.get("changed_files")), *list_value(task.get("files")), *list_value(task.get("target_files"))] if str(item).strip()))

def build_task_model_advice(requirement: dict[str, Any], task: dict[str, Any]) -> dict[str, str]:
    """模型档位只读取显式结构，未填写时使用中档，不从任务文字猜复杂度。"""

    explicit = str(task.get("model_tier") or task.get("complexity") or "medium").strip().lower()
    if explicit in {"high", "高级", "高"}:
        return {"level": "high", "model": "高思考模型", "reason": "任务卡显式指定高档。"}
    if explicit in {"low", "basic", "低", "基础"}:
        return {"level": "low", "model": "基础模型", "reason": "任务卡显式指定基础档。"}
    return {"level": "medium", "model": "中思考模型", "reason": "任务卡使用默认中档。"}







def task_business_summary(task: dict[str, Any]) -> str:
    """状态页只展示模型显式写入的业务摘要。"""

    return str(task.get("business_summary") or task.get("summary") or task.get("title") or task.get("task_id") or "").strip()


def task_context(requirement: dict[str, Any], task: dict[str, Any]) -> dict[str, str]:
    return {
        "requirement_id": str(requirement["requirement_id"]),
        "task_id": str(task["task_id"]),
        "title": str(task.get("title", "")),
        "business_summary": task_business_summary(task),
    }


def task_action(requirement: dict[str, Any], task: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "command": f"$sdlc-task {requirement['requirement_id']} {task['task_id']}",
        "finish_command": f"$sdlc-task-done {requirement['requirement_id']} {task['task_id']}",
        "reason": reason,
        "model_advice": build_task_model_advice(requirement, task),
        "task_context": task_context(requirement, task),
    }


def task_finish_action(requirement: dict[str, Any], task: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "command": f"$sdlc-task-done {requirement['requirement_id']} {task['task_id']}",
        "restore_command": f"$sdlc-task-restore {requirement['requirement_id']} {task['task_id']} 反馈内容",
        "reason": reason,
        "model_advice": build_task_model_advice(requirement, task),
        "task_context": task_context(requirement, task),
    }


def task_doing_finish_action(requirement: dict[str, Any], task: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "command": f"$sdlc-task-done {requirement['requirement_id']} {task['task_id']}",
        "continue_command": f"$sdlc-task {requirement['requirement_id']} {task['task_id']}",
        "reason": reason,
        "model_advice": build_task_model_advice(requirement, task),
        "task_context": task_context(requirement, task),
    }


def task_failed_action(requirement: dict[str, Any], task: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "command": f"$sdlc-task {requirement['requirement_id']} {task['task_id']}",
        "finish_command": f"$sdlc-task-done {requirement['requirement_id']} {task['task_id']}",
        "restore_command": f"$sdlc-task-restore {requirement['requirement_id']} {task['task_id']} 反馈内容",
        "reason": reason,
        "model_advice": build_task_model_advice(requirement, task),
        "task_context": task_context(requirement, task),
    }


def model_advice_lines(next_actions: dict[str, Any]) -> list[str]:
    advice = next_actions.get("model_advice")
    if not isinstance(advice, dict):
        return []
    return [
        f"- 任务复杂度：{advice['level']}",
        f"- 推荐模型：{advice['model']}",
        f"- 模型理由：{advice['reason']}",
    ]


def next_task_business_lines(next_actions: dict[str, Any]) -> list[str]:
    context = next_actions.get("task_context")
    if not isinstance(context, dict):
        return []
    requirement_id = str(context.get("requirement_id", "")).strip()
    task_id = str(context.get("task_id", "")).strip()
    title = str(context.get("title", "")).strip()
    summary = str(context.get("business_summary", "")).strip()
    if not task_id or not summary:
        return []
    label = f"{requirement_id} / {task_id}".strip(" /")
    if title:
        label = f"{label} {title}".strip()
    return [
        "- 下一任务：" + label,
        "- 大白话：" + summary,
    ]


def collect_candidate_task_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requirement in state["active_requirements"]:
        doing_tasks = [task for task in requirement["tasks"] if task["status"] == "doing"]
        if doing_tasks:
            task = doing_tasks[0]
            candidates.append(task_action(requirement, task, f"{task['task_id']} 已经在进行中，建议先把它收口。"))
            continue

        for task in requirement["tasks"]:
            if task["status"] != "todo":
                continue
            if task_dependencies_ready(state, task):
                candidates.append(task_action(requirement, task, f"{requirement['requirement_id']} 已经有可执行任务，先做最前面的未完成项。"))
                break
    return candidates


def project_task_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按项目级正式任务编号建立索引，跨需求依赖也使用同一份状态。"""

    requirements = state.get("requirements", {})
    if isinstance(requirements, dict):
        requirement_values = requirements.values()
    else:
        requirement_values = state.get("active_requirements", [])
    task_map: dict[str, dict[str, Any]] = {}
    for requirement in requirement_values:
        if not isinstance(requirement, dict):
            continue
        for task in requirement.get("tasks", []):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id", "")).strip()
            if task_id:
                task_map[task_id] = task
    return task_map


def task_dependencies_ready(state: dict[str, Any], task: dict[str, Any]) -> bool:
    """未知或未完成的项目级依赖都判定为未就绪，不向状态刷新抛出 KeyError。"""

    task_map = project_task_map(state)
    for dependency in task.get("depends_on", []):
        dependency_task = task_map.get(str(dependency))
        if dependency_task is None or dependency_task.get("status") not in {"done", "closed"}:
            return False
    return True


def collect_next_task_stage_actions(paths: ProjectPaths, state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requirement in state["active_requirements"]:
        if isinstance(requirement.get("task_plan_contract"), Mapping):
            if requirement.get("status") != "ready_for_development":
                continue
        else:
            # 没有结构化整套任务审核的旧任务只能继续只读展示，不能因为既有
            # 任务执行包档案恰好存在就进入新主线候选。
            continue
        for task in requirement["tasks"]:
            if task["status"] != "todo":
                continue
            if not task_dependencies_ready(state, task):
                continue
            candidates.append(
                task_action(
                    requirement,
                    task,
                    f"{requirement['requirement_id']} / {task['task_id']} 的整套任务审核有效、依赖已完成且没有阻塞，可以直接开工。",
                )
            )
            break
    return candidates


def collect_ready_for_user_check_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requirement in state["active_requirements"]:
        ready_tasks = [task for task in requirement["tasks"] if task["status"] == "ready_for_user_check"]
        if ready_tasks:
            task = ready_tasks[0]
            candidates.append(
                task_finish_action(
                    requirement,
                    task,
                    f"{task['task_id']} 正在等待真实验收结果，先收口这个任务；通过就完成并提交，不通过就恢复任务。",
                )
            )
    return candidates


def collect_test_failed_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requirement in state["active_requirements"]:
        failed_tasks = [task for task in requirement["tasks"] if task["status"] == "test_failed"]
        if failed_tasks:
            task = failed_tasks[0]
            candidates.append(
                task_failed_action(
                    requirement,
                    task,
                    f"{task['task_id']} 自动测试失败，先修复实现或测试命令，再重新收口当前任务。",
                )
            )
    return candidates


def collect_doing_task_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requirement in state["active_requirements"]:
        doing_tasks = [task for task in requirement["tasks"] if task["status"] == "doing"]
        if doing_tasks:
            task = doing_tasks[0]
            candidates.append(
                task_doing_finish_action(
                    requirement,
                    task,
                    f"{task['task_id']} 已经在进行中，主线先完成并收口这个任务；如果还没做完，就继续按 task-run 读取清单和正式任务合同实现。",
                )
            )
    return candidates


def inspect_direct_task_run(
    paths: ProjectPaths,
    state: dict[str, Any],
    requirement: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """只读复用正式运行校验合同，展示状态时绝不顺手把轮次写成 stale。"""

    requirement_id = str(requirement["requirement_id"])
    task_id = str(task["task_id"])
    try:
        from codex_sdlc.core.task_read_manifest import load_task_read_manifest
        from codex_sdlc.core.task_run import (
            _all_tasks,
            _require_run_worktree_identity,
            _upstream_drift_fields,
            _worktree_scope_changed,
            load_task_run_context,
        )

        context = load_task_run_context(
            paths,
            requirement_id=requirement_id,
            task_id=task_id,
        )
    except SdlcError as exc:
        # 旧事件可能把任务留在 doing，却没有正式运行轮次。此时任何继续动作都会猜测身份。
        return {"valid": False, "status": "missing", "reason": f"缺少可读取的 task-run：{exc.message}"}

    run = context["run"]
    current = context["current"]
    run_path = context["run_path"]
    assert isinstance(run, Mapping)
    assert isinstance(current, Mapping)
    assert isinstance(run_path, Path)
    status = str(run.get("status") or "")
    run_number = int(run.get("run_number") or current.get("run_number") or 0)
    common = {
        "status": status,
        "run_number": run_number,
        "runner_thread_id": str(run.get("runner_thread_id") or ""),
    }
    if status != str(current.get("status") or ""):
        return {**common, "valid": False, "reason": "task-run 与当前指针状态不一致。"}

    manifest_path = run_path.parent / "task-read-manifest.v1.json"
    recorded_sha256 = str(run.get("read_manifest_sha256") or "")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {**common, "valid": False, "reason": "读取清单缺失。"}
    if not recorded_sha256 or sha256_file(manifest_path) != recorded_sha256:
        return {**common, "valid": False, "reason": "读取清单哈希不一致。"}
    try:
        manifest = load_task_read_manifest(manifest_path)
        _require_run_worktree_identity(paths, run)
    except SdlcError as exc:
        return {**common, "valid": False, "reason": exc.message}

    if status == "active":
        if not isinstance(run.get("read_confirmation"), Mapping):
            return {**common, "valid": False, "reason": "读取确认缺失。"}
        changed_upstream = _upstream_drift_fields(
            paths,
            requirement=requirement,
            task=task,
            run=run,
            manifest=manifest,
            all_tasks=_all_tasks(state),
        )
        if changed_upstream:
            fields = "、".join(changed_upstream)
            return {
                **common,
                "valid": False,
                "reason": f"当前任务上游已经变化（{fields}）。",
                "drift_fields": changed_upstream,
            }
        try:
            if _worktree_scope_changed(paths, run):
                return {
                    **common,
                    "valid": False,
                    "reason": "任务允许输出范围外出现了无法归属的工作树变化。",
                }
        except SdlcError as exc:
            return {**common, "valid": False, "reason": exc.message}

    return {**common, "valid": True, "recorded_sha256": recorded_sha256}


def direct_task_run_action(
    paths: ProjectPaths,
    state: dict[str, Any],
    requirement: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """把进行中任务的下一步绑定到通过只读复核的当前 task-run。"""

    requirement_id = str(requirement["requirement_id"])
    task_id = str(task["task_id"])
    probe = inspect_direct_task_run(paths, state, requirement, task)
    status = str(probe.get("status") or "")
    if probe.get("valid") is not True:
        return {
            "primary": "$sdlc-status",
            "reason": f"{requirement_id} / {task_id} 的 task-run 只读校验未通过：{probe['reason']}不能继续身份绑定动作。",
            # task-run-check 会按同一漂移字段显式写入 stale；status 和 next 自身保持只读。
            "alternatives": [f"codex-sdlc task-run-check {requirement_id} {task_id}", "$sdlc-handoff"],
            "model_advice": build_task_model_advice(requirement, task),
            "task_context": task_context(requirement, task),
        }
    if status == "reading":
        recorded_sha256 = str(probe["recorded_sha256"])
        return {
            "primary": (
                f"codex-sdlc task-read-confirm {requirement_id} {task_id} "
                f"--manifest-sha256 {recorded_sha256}"
            ),
            "reason": f"{requirement_id} / {task_id} 已建立 reading 轮次；完整读取清单原文后，由同一任务线程确认清单。",
            "alternatives": [f"codex-sdlc task-run-check {requirement_id} {task_id}", "$sdlc-status", "$sdlc-handoff"],
            "model_advice": build_task_model_advice(requirement, task),
            "task_context": task_context(requirement, task),
        }
    if status == "active":
        return {
            "primary": f"$sdlc-task-done {requirement_id} {task_id}",
            "reason": f"{requirement_id} / {task_id} 的读取清单、任务审核和上游已经只读复核，当前 active 轮次可以继续实现并按门禁收口。",
            "alternatives": [f"codex-sdlc task-run-check {requirement_id} {task_id}", "$sdlc-status", "$sdlc-handoff"],
            "model_advice": build_task_model_advice(requirement, task),
            "task_context": task_context(requirement, task),
        }
    if status == "stale":
        return {
            "primary": f"codex-sdlc task-pause {requirement_id} {task_id} --reason 'task-run 已失效，准备重新开工'",
            # doing 不满足 task-restore 准入；先暂停到 todo，正式输入恢复有效后 task 才能创建递增轮次。
            "reason": f"{requirement_id} / {task_id} 的 task-run 已失效且任务仍是 doing；先暂停任务，恢复造成漂移的正式输入，再执行 codex-sdlc task {requirement_id} {task_id} 创建新的 reading 轮次。",
            "alternatives": [f"codex-sdlc task-run-check {requirement_id} {task_id}", "$sdlc-status", "$sdlc-handoff"],
            "model_advice": build_task_model_advice(requirement, task),
            "task_context": task_context(requirement, task),
        }
    return {
        "primary": "$sdlc-status",
        "reason": f"{requirement_id} / {task_id} 当前 task-run 状态为 {status or '空'}，不能按活动任务继续。",
        "alternatives": [f"codex-sdlc task-run-check {requirement_id} {task_id}", "$sdlc-handoff"],
    }


def current_direct_task_action(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, Any] | None:
    """收集全部活动轮次；身份不唯一时停止，不能静默替线程挑选第一条。"""

    active: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for requirement in state["active_requirements"]:
        if not isinstance(requirement.get("task_plan_contract"), Mapping):
            continue
        for task in requirement["tasks"]:
            if str(task.get("status") or "") in ACTIVE_TASK_RUN_STATUSES:
                active.append((requirement, task))
    if not active:
        return None
    if len(active) > 1:
        identities: list[str] = []
        for requirement, task in active:
            probe = inspect_direct_task_run(paths, state, requirement, task)
            run_number = int(probe.get("run_number") or 0)
            run_label = f"run-{run_number:04d}" if run_number else "run-未知"
            identities.append(f"{requirement['requirement_id']}/{task['task_id']}/{run_label}")
        return {
            "primary": "$sdlc-status",
            "reason": "多个活动 task-run 冲突，不能推荐任何身份绑定动作：" + "、".join(identities) + "。",
            "alternatives": ["$sdlc-handoff"],
        }
    requirement, task = active[0]
    return direct_task_run_action(paths, state, requirement, task)


def task_plan_review_action(requirement: dict[str, Any]) -> dict[str, Any] | None:
    """任务没有通过当前审核时停止开工，并给出能回到正式审核的入口。"""

    if not isinstance(requirement.get("task_plan_contract"), Mapping):
        return None
    reason = task_plan_stop_reason(requirement)
    if not reason:
        return None
    requirement_id = str(requirement["requirement_id"])
    if "明确阻塞条件" in reason:
        return {
            "primary": "$sdlc-status",
            # 审核入口会拒绝仍带阻塞条件的任务，因此这里只提示先清除正式任务里的阻塞。
            "reason": reason + "请先清除正式任务中的阻塞条件，再创建整套任务审核。",
            "alternatives": ["$sdlc-handoff"],
        }
    if "要求返修" in reason or "代码证据已经缺失或过期" in reason:
        return {
            "primary": f"$sdlc-tasks {requirement_id}",
            "reason": reason,
            "alternatives": ["$sdlc-status", "$sdlc-handoff"],
        }
    if "还没有提交结论" in reason:
        return {
            "primary": "$sdlc-status",
            "reason": reason + "请由独立审核任务提交当前审核结论。",
            "alternatives": ["$sdlc-handoff"],
        }
    reviews = requirement.get("task_plan_review_state", {}).get("reviews", [])
    numbers = []
    for review in reviews if isinstance(reviews, list) else []:
        match = re.fullmatch(r"REV-(\d+)", str(review.get("review_id") or "")) if isinstance(review, Mapping) else None
        if match:
            numbers.append(int(match.group(1)))
    review_id = f"REV-{max(numbers, default=0) + 1:03d}"
    task_plan_path = (
        f".codex-sdlc/requirements/{requirement['folder_name']}/tasks/task-plan.v2.json"
    )
    return {
        "primary": (
            f"codex-sdlc review create --review-id {review_id} --stage task_plan "
            f"--owner {requirement_id} --input '{task_plan_path}'"
        ),
        "reason": reason,
        "alternatives": ["$sdlc-status", "$sdlc-handoff"],
    }


def collect_task_quality_actions(state: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for requirement in state["active_requirements"]:
        task_quality = requirement.get("task_quality") or {}
        if task_quality.get("status") != "needs_attention":
            continue
        warnings = task_quality.get("warnings", [])
        first_warning = warnings[0] if warnings else "任务清单质量需要关注。"
        candidates.append(
            {
                "command": f"$sdlc-plan {requirement['requirement_id']}",
                "reason": f"{requirement['requirement_id']} 的任务清单质量需要关注，先整理任务粒度。{first_warning}",
            }
        )
    return candidates


def collect_regression_actions(state: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for requirement in state["requirements"].values():
        if requirement.get("status") not in {"done", "verifying"}:
            continue
        tasks = [task for task in requirement["tasks"] if task["status"] != "closed"]
        if not tasks:
            continue
        candidates.append(
            {
                "command": f"$sdlc-regression {requirement['requirement_id']}",
                "accept_command": (
                    "" if requirement_has_unresolved_manual_pending(state, requirement)
                    else f"$sdlc-accept {requirement['requirement_id']}"
                ),
                "reason": f"{requirement['requirement_id']} 的任务已经全部完成，先做一次需求级回归，再让用户确认需求结束。",
            }
        )
    return candidates


def pending_change_alternative_commands(state: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for item in state["changes"]:
        if item["status"] == "draft":
            commands.append(f"$sdlc-change-accept {item['requirement_id']} {item['change_id']}")
    planned_requirement_ids: set[str] = set()
    for item in state["changes"]:
        if item["status"] not in {"effective", "pending"}:
            continue
        requirement = state.get("requirements", {}).get(item.get("requirement_id")) if isinstance(state.get("requirements"), dict) else None
        tasks = requirement.get("tasks", []) if isinstance(requirement, dict) else []
        if change_waits_for_model_plan(item, tasks):
            commands.append(
                f"$sdlc-change-plan {item['requirement_id']} --change {item['change_id']} "
                '--task "任务标题||任务目标"'
            )
            continue
        requirement_id = str(item["requirement_id"])
        if requirement_id in planned_requirement_ids:
            continue
        planned_requirement_ids.add(requirement_id)
        commands.append(f"$sdlc-change-plan {requirement_id}")
    return unique_extend([], commands)


def change_has_model_task_suggestions(change: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("source", "")) == "model" and str(item.get("title", "")).strip()
        for item in change.get("added_tasks", [])
    )


def change_has_safe_existing_task_binding(change: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> bool:
    changed_task_ids = [str(item) for item in change.get("changed_task_ids", []) if str(item).strip()]
    if len(changed_task_ids) != 1:
        return False
    if tasks is None:
        return True
    target_task_id = changed_task_ids[0]
    task = next((item for item in tasks if str(item.get("task_id", "")) == target_task_id), None)
    return task is not None and str(task.get("status", "")) not in {"done", "closed"}


def change_waits_for_model_plan(change: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> bool:
    if change.get("status") not in {"effective", "pending"}:
        return False
    if change.get("planning_status") == "needs_model_plan":
        return True
    # 旧事件里可能没有 planning_status。只有命中单个未完成任务时，CLI 才能安全合并；
    # 自动命中多个任务时仍交给 Codex 模型拆分，避免生成或规划出空泛任务。
    return not change_has_model_task_suggestions(change) and not change_has_safe_existing_task_binding(change, tasks)


def current_task_priority_actions(
    state: dict[str, Any],
    *,
    quality_alternatives: list[str],
) -> dict[str, Any] | None:
    change_alternatives = pending_change_alternative_commands(state)

    failed_candidates = collect_test_failed_actions(state)
    if failed_candidates:
        primary_candidate = failed_candidates[0]
        alternatives = [primary_candidate["finish_command"], primary_candidate["restore_command"]]
        alternatives.extend(candidate["command"] for candidate in failed_candidates[1:])
        alternatives.extend(quality_alternatives)
        # 当前任务已经进入执行链路时，新变更只能作为备选处理，不能抢走收口主推荐。
        alternatives.extend(change_alternatives)
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_candidate["command"],
            "reason": primary_candidate["reason"],
            "alternatives": unique_extend([], alternatives),
            "model_advice": primary_candidate.get("model_advice"),
        }

    ready_candidates = collect_ready_for_user_check_actions(state)
    if ready_candidates:
        primary_candidate = ready_candidates[0]
        alternatives = [primary_candidate["restore_command"]]
        alternatives.extend(candidate["command"] for candidate in ready_candidates[1:])
        alternatives.extend(quality_alternatives)
        # 当前任务等待验收时，先让用户验收收口；变更确认放到备选，避免反向打断。
        alternatives.extend(change_alternatives)
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_candidate["command"],
            "reason": primary_candidate["reason"],
            "alternatives": unique_extend([], alternatives),
            "model_advice": primary_candidate.get("model_advice"),
        }

    doing_candidates = collect_doing_task_actions(state)
    if doing_candidates:
        primary_candidate = doing_candidates[0]
        alternatives = [primary_candidate["continue_command"]]
        alternatives.extend(candidate["command"] for candidate in doing_candidates[1:])
        alternatives.extend(quality_alternatives)
        # 进行中的任务同样先收口；新变更只提醒后续处理，不插到当前任务前面。
        alternatives.extend(change_alternatives)
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_candidate["command"],
            "reason": primary_candidate["reason"],
            "alternatives": unique_extend([], alternatives),
            "model_advice": primary_candidate.get("model_advice"),
        }

    return None


def collect_docs_actions(state: dict[str, Any], root: Path | None = None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for requirement in state["requirements"].values():
        action = requirement_docs_action(requirement, root)
        if action:
            candidates.append(action)
    return candidates


def pending_requirement_drafts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return draft_ownership.pending_requirement_drafts(state)


def pending_requirement_draft_lines(state: dict[str, Any], limit: int = 5) -> list[str]:
    return draft_ownership.pending_requirement_draft_lines(state, limit)


def active_draft(state: dict[str, Any]) -> dict[str, Any] | None:
    drafts = state.get("drafts", {})
    if not isinstance(drafts, dict):
        return None
    candidates = [
        item
        for item in drafts.values()
        if isinstance(item, dict) and str(item.get("status") or "").strip() != "started"
    ]
    if not candidates:
        return None
    # 当前 DRAFT 仍沿用“最近更新优先”的规则，保证 discuss/design/status 都围绕同一份确认稿继续推进。
    return sorted(candidates, key=lambda item: int(item.get("_updated_seq", 0)), reverse=True)[0]



def draft_questions(draft: dict[str, Any]) -> list[str]:
    return [str(item).strip() for item in draft.get("questions", []) if str(item).strip()]


def draft_summary_text(draft: dict[str, Any], field: str) -> str:
    """摘要由模型显式填写，状态页不再从正文截取并猜测。"""

    return str(draft.get(field) or "").strip()


def active_draft_status_lines(state: dict[str, Any], next_actions: dict[str, Any] | None = None) -> list[str]:
    draft = active_draft(state)
    if draft is None:
        return ["- 当前没有活跃 DRAFT"]

    assessment = draft_lifecycle.assess_draft(draft)
    effective_status = assessment.effective_status
    lines = [f"- {draft['draft_id']} [{effective_status}] {draft['title']}"]
    lines.append(f"  - 模型事实：{assessment.facts_status}")
    requirement_summary = draft_summary_text(draft, "requirement_summary")
    design_summary = draft_summary_text(draft, "design_summary")
    if requirement_summary:
        lines.append(f"  - 需求摘要：{requirement_summary}")
    if design_summary:
        lines.append(f"  - 技术摘要：{design_summary}")
    questions = list(assessment.open_questions)
    if questions:
        lines.append("  - 待用户回答：")
        lines.extend(f"    - {item}" for item in questions)
    if assessment.blockers:
        lines.append("  - 阻断项：")
        lines.extend(
            f"    - {item.code}:{item.source_id}:{item.status}"
            + (f":{item.reference}" if item.reference else "")
            for item in assessment.blockers
        )
    if next_actions is not None:
        # status 里的 DRAFT 区域直接带上推荐动作，避免用户只看总推荐区时还要自己反推当前草稿该怎么继续。
        lines.append(f"  - 推荐：{next_actions['primary']}")
    return lines


def active_draft_is_linked_to_active_requirement(state: dict[str, Any], draft: dict[str, Any]) -> bool:
    # 历史兼容流程可能已经把同一份 DRAFT 对应的 DES 关联到活跃需求。
    # 这种 DRAFT 只是历史草稿，不应该盖过当前需求的任务推荐；真正的新 DRAFT 没有关联到活跃 REQ，仍然优先展示。
    active_requirement_ids = {str(item.get("requirement_id") or "") for item in state.get("active_requirements", [])}
    draft_id = str(draft.get("draft_id") or "")
    if not active_requirement_ids or not draft_id:
        return False
    for design in state.get("designs", []):
        if str(design.get("draft_id") or "") == draft_id and str(design.get("requirement_id") or "") in active_requirement_ids:
            return True
    return False


def active_draft_next_action(state: dict[str, Any]) -> dict[str, Any] | None:
    draft = active_draft(state)
    if draft is None:
        return None
    if active_draft_is_linked_to_active_requirement(state, draft):
        return None

    assessment = draft_lifecycle.assess_draft(draft)
    # 待确认问题和需求缺项永远优先于 DES 确认，不能因为刚好有一个技术草案
    # 就把用户带去 design-accept，从而跳过真正的阻断项。
    if assessment.effective_status in {
        "needs_user",
        "discussing",
        "requirement_ready",
        "requirement_reviewing",
        "requirement_confirmed",
        "design_ready",
        "reviewing",
        "start_ready",
    }:
        return draft_lifecycle.next_action_for_draft(draft)

    draft_id = str(draft.get("draft_id") or "").strip()
    draft_designs = [
        design
        for design in state.get("designs", [])
        if design.get("status") == "draft" and str(design.get("draft_id") or "").strip() == draft_id
    ]
    if draft_designs:
        primary_design = draft_designs[0]
        alternatives = [f"$sdlc-design-accept {design['design_id']}" for design in draft_designs[1:]]
        alternatives.extend(["$sdlc-design 技术方案草案", "$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": f"$sdlc-design-accept {primary_design['design_id']}",
            "reason": f"{primary_design['design_id']} 技术方案还没确认，先确认方案再进入 start 前审查。",
            "alternatives": alternatives,
            "draft_context": f"{draft_id} [{draft.get('status')}] {draft.get('title')}",
        }

    missing_items = draft_contract.requirement_missing_items(str(draft.get("requirement_body") or ""))
    return draft_lifecycle.next_action_for_draft(draft, missing_items=missing_items)


def compute_next_actions(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, Any]:
    draft_action = active_draft_next_action(state)
    if draft_action is not None:
        return draft_action

    draft_designs = [design for design in state.get("designs", []) if design["status"] == "draft"]
    if draft_designs:
        primary_design = draft_designs[0]
        alternatives = [
            f"$sdlc-design-accept {design['design_id']}"
            for design in draft_designs[1:]
        ]
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": f"$sdlc-design-accept {primary_design['design_id']}",
            "reason": f"{primary_design['design_id']} 技术方案还没确认，先确认方案再拆任务或继续开发。",
            "alternatives": alternatives,
        }

    accepted_unlinked_designs = [
        design
        for design in state.get("designs", [])
        if design["status"] == "accepted" and not design.get("requirement_id")
    ]
    if accepted_unlinked_designs and not state["active_requirements"]:
        return {
            "primary": "$sdlc-start",
            "reason": "技术方案已经确认，但还没有创建正式需求，先把需求和方案正式建档。",
            "alternatives": [
                "$sdlc-status",
                "$sdlc-handoff",
            ],
        }

    if state["pending_capture_files"]:
        pending_captures = [item for item in state["captures"] if item["status"] == "pending"]
        pending_drafts = [item for item in pending_captures if item.get("target_type") == "requirement_draft"]
        if pending_captures and len(pending_drafts) == len(pending_captures):
            return {
                "primary": "$sdlc-discuss 继续完善需求草案",
                "reason": f"当前还有 {len(pending_drafts)} 条需求讨论草案未进入正式需求，先继续讨论或确认草案，再确认技术方案。",
                "alternatives": [
                    "$sdlc-design 技术方案草案",
                    "$sdlc-start",
                    "$sdlc-status",
                    "$sdlc-handoff",
                ],
            }
        return {
            "primary": "$sdlc-status",
            "reason": f"当前还有 {len(state['pending_capture_files'])} 条未归类 capture，先确认这些中途结论要不要纳入正式状态。",
            "alternatives": ["$sdlc-handoff", "$sdlc-status"],
        }

    quality_candidates = collect_task_quality_actions(state)
    quality_alternatives = [candidate["command"] for candidate in quality_candidates]
    # 活动任务只看当前 task-run。既有任务执行包档案无论缺失、存在还是过期，都不能
    # 抢走读取确认、完成或恢复的主推荐。
    current_task_action = current_direct_task_action(paths, state)
    if current_task_action:
        return current_task_action

    draft_changes = [item for item in state["changes"] if item["status"] == "draft"]
    if draft_changes:
        first_draft_change = draft_changes[0]
        accept_command = f"$sdlc-change-accept {first_draft_change['requirement_id']} {first_draft_change['change_id']}"
        alternatives = [
            f"$sdlc-change-accept {item['requirement_id']} {item['change_id']}"
            for item in draft_changes[1:]
        ]
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": accept_command,
            "reason": f"当前还有 {len(draft_changes)} 条待确认需求变化，先让用户确认变更是否写入当前生效版本。",
            "alternatives": alternatives,
        }

    effective_changes = [item for item in state["changes"] if item["status"] in {"effective", "pending"}]
    if effective_changes:
        model_plan_changes = []
        for item in effective_changes:
            requirement = state.get("requirements", {}).get(item.get("requirement_id")) if isinstance(state.get("requirements"), dict) else None
            tasks = requirement.get("tasks", []) if isinstance(requirement, dict) else []
            if change_waits_for_model_plan(item, tasks):
                model_plan_changes.append(item)
        if model_plan_changes:
            first_model_change = model_plan_changes[0]
            plan_command = (
                f"$sdlc-change-plan {first_model_change['requirement_id']} --change {first_model_change['change_id']} "
                '--task "任务标题||任务目标"'
            )
            alternatives = [
                f"$sdlc-change-plan {item['requirement_id']} --change {item['change_id']} "
                '--task "任务标题||任务目标"'
                for item in model_plan_changes[1:]
            ]
            alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
            return {
                "primary": plan_command,
                "reason": (
                    f"{first_model_change['change_id']} 已经写入当前生效需求，但还没有模型任务建议。"
                    "下一步由 Codex 补任务拆分，并通过正式命令继续规划。"
                ),
                "alternatives": alternatives,
            }
        first_effective_change = effective_changes[0]
        plan_command = f"$sdlc-change-plan {first_effective_change['requirement_id']}"
        alternatives = [
            f"$sdlc-change-plan {item['requirement_id']}"
            for item in effective_changes[1:]
            if item["requirement_id"] != first_effective_change["requirement_id"]
        ]
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": plan_command,
            "reason": f"当前还有 {len(effective_changes)} 条已生效但未规划的需求变化，先拆分任务并生成覆盖映射。",
            "alternatives": alternatives,
        }

    known_open_change_files = {
        paths.root / item["file_path"]
        for item in state["changes"]
        if item.get("status") in {"draft", "effective", "pending"} and item.get("file_path")
    }
    orphan_change_files = [
        item
        for item in state["pending_change_files"]
        if item not in known_open_change_files
    ]
    if orphan_change_files:
        return {
            "primary": "$sdlc-doctor-repair",
            "reason": f"当前还有 {len(orphan_change_files)} 个未纳入状态的变更文件，先修复快照再继续任务。",
            "alternatives": ["$sdlc-doctor-deep", "$sdlc-status", "$sdlc-handoff"],
        }

    active_requirements = state["active_requirements"]
    if not active_requirements:
        regression_candidates = collect_regression_actions(state)
        if regression_candidates:
            primary_regression = regression_candidates[0]
            alternatives = [primary_regression["accept_command"]] if primary_regression.get("accept_command") else []
            alternatives.extend(candidate["command"] for candidate in regression_candidates[1:])
            alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
            return {
                "primary": primary_regression["command"],
                "reason": primary_regression["reason"],
                "alternatives": alternatives,
            }
        docs_candidates = collect_docs_actions(state, paths.root)
        if docs_candidates:
            primary_docs = docs_candidates[0]
            alternatives = [candidate["command"] for candidate in docs_candidates[1:]]
            alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
            return {
                "primary": primary_docs["command"],
                "reason": primary_docs["reason"],
                "alternatives": alternatives,
            }
        return {
            "primary": "$sdlc-discuss 需求想法",
            "reason": "当前还没有活跃需求，先讨论需求并记录草案，用户确认后先确认技术方案。",
            "alternatives": [
                "$sdlc-design 技术方案草案",
                "$sdlc-status",
                "$sdlc-handoff",
            ],
        }

    requirements_without_tasks = [
        requirement
        for requirement in active_requirements
        if not requirement["tasks"]
    ]
    if requirements_without_tasks:
        primary_requirement = requirements_without_tasks[0]
        alternatives = [
            f"$sdlc-tasks {requirement['requirement_id']}"
            for requirement in requirements_without_tasks[1:]
        ]
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": f"$sdlc-tasks {primary_requirement['requirement_id']}",
            "reason": f"{primary_requirement['requirement_id']} 还没有任务清单，先用任务清单阶段把正式任务拆出来，再准备开工。",
            "alternatives": alternatives,
        }

    regression_candidates = collect_regression_actions(state)
    if regression_candidates:
        primary_regression = regression_candidates[0]
        alternatives = [primary_regression["accept_command"]] if primary_regression.get("accept_command") else []
        alternatives.extend(candidate["command"] for candidate in regression_candidates[1:])
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_regression["command"],
            "reason": primary_regression["reason"],
            "alternatives": unique_extend([], alternatives),
        }

    for requirement in active_requirements:
        review_action = task_plan_review_action(requirement)
        if review_action is not None:
            return review_action

    task_stage_candidates = collect_next_task_stage_actions(paths, state)
    if task_stage_candidates:
        primary_candidate = task_stage_candidates[0]
        alternatives: list[str] = list(primary_candidate.get("alternatives", []))
        alternatives.extend(candidate["command"] for candidate in task_stage_candidates[1:])
        alternatives.extend(quality_alternatives)
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_candidate["command"],
            "reason": primary_candidate["reason"],
            "alternatives": unique_extend([], alternatives),
            "model_advice": primary_candidate.get("model_advice"),
            "task_context": primary_candidate.get("task_context"),
        }

    docs_candidates = collect_docs_actions(state, paths.root)
    if docs_candidates:
        primary_docs = docs_candidates[0]
        alternatives = [candidate["command"] for candidate in docs_candidates[1:]]
        alternatives.extend(["$sdlc-status", "$sdlc-handoff"])
        return {
            "primary": primary_docs["command"],
            "reason": primary_docs["reason"],
            "alternatives": alternatives,
        }

    if state["git_changed_files"]:
        return {
            "primary": "$sdlc-finish",
            "reason": "项目已经有代码改动，先补一份正式交接，避免本轮上下文丢失。",
            "alternatives": ["$sdlc-status", "$sdlc-handoff"],
        }

    return {
        "primary": "$sdlc-handoff",
        "reason": "当前没有可执行任务，先导出交接提示词更稳妥。",
        "alternatives": ["$sdlc-status", "$sdlc-discuss 需求想法"],
    }


def write_current_snapshot(paths: ProjectPaths, state: dict[str, Any]) -> None:
    next_actions = compute_next_actions(paths, state)
    total_subtasks = sum(requirement_subtask_count(requirement) for requirement in state["requirements"].values())
    lines = [
        "# 当前状态",
        "",
        f"- 项目路径：{paths.root}",
        f"- 活跃需求数：{len(state['active_requirements'])}",
        f"- 任务完成数：{state['counts']['done_tasks']}",
        f"- 任务关闭数：{state['counts'].get('closed_tasks', 0)}",
        f"- 已完成或关闭：{state['counts'].get('finished_tasks', state['counts']['done_tasks'])}/{state['counts']['all_tasks']}",
        f"- 子检查项总数：{total_subtasks}",
        f"- 已写验证任务数：{state['counts']['verified_tasks']}",
        "",
        "## 活跃需求",
    ]

    if not state["active_requirements"]:
        lines.append("- 当前没有活跃需求")
    else:
        for requirement in state["active_requirements"]:
            lines.extend(
                [
                    f"### {requirement['requirement_id']} {requirement['title']}",
                    f"- 状态：{requirement['status']}",
                    f"- 优先级：{requirement.get('priority', 'normal')}",
                    f"- 阻塞：{requirement.get('blocked_reason') or '无'}",
                    f"- 技术方案：{requirement_design_status(requirement)}",
                    f"- 任务清单质量：{requirement.get('task_quality', {}).get('status', '未检查')}",
                    f"- 正式任务：{len(requirement['tasks'])} 个",
                    f"- 子检查项：{requirement_subtask_count(requirement)} 个",
                    f"- 目录：{requirement['folder_name']}",
                    "- 当前任务：",
                ]
            )
            if requirement["tasks"]:
                lines.extend(task_display_line(task, show_subtasks=True) for task in requirement["tasks"])
            else:
                lines.append("- 暂无任务")
            lines.append("")

    lines.extend(["## 最近交接"])
    if state["recent_session"] is None:
        lines.append("- 暂无会话交接")
    else:
        lines.append(f"- {state['recent_session']['session_id']}：{state['recent_session']['summary']}")

    lines.extend(
        [
            "",
            "## 建议下一步",
            f"- 主推荐：{next_actions['primary']}",
            f"- 原因：{next_actions['reason']}",
        ]
    )
    lines.extend(model_advice_lines(next_actions))
    for item in next_actions["alternatives"]:
        lines.append(f"- 备选：{item}")
    task_business_lines = next_task_business_lines(next_actions)
    if task_business_lines:
        lines.extend(["", "## 下一任务说明"])
        lines.extend(task_business_lines)

    paths.current_md.write_text(join_lines(lines), encoding="utf-8")


MANUAL_SECTION_HEADING = "## 人工补充"
DEFAULT_MANUAL_LINE = "- 暂无人工补充"


def split_manual_section(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == MANUAL_SECTION_HEADING:
            return "\n".join(lines[:index]).strip(), lines[index + 1 :]
    return text.strip(), []


def manual_section_lines(file_path: Path) -> list[str]:
    if not file_path.exists():
        return [DEFAULT_MANUAL_LINE]
    _generated, manual_lines = split_manual_section(file_path.read_text(encoding="utf-8"))
    if not manual_lines or not "\n".join(manual_lines).strip():
        return [DEFAULT_MANUAL_LINE]
    return manual_lines


def write_markdown_with_manual_section(file_path: Path, generated_lines: list[str]) -> None:
    lines = [
        *generated_lines,
        "",
        MANUAL_SECTION_HEADING,
        *manual_section_lines(file_path),
    ]
    file_path.write_text(join_lines(lines), encoding="utf-8")


def generated_section_changed(file_path: Path, expected_lines: list[str]) -> bool:
    if not file_path.exists():
        return False
    actual_generated, _manual = split_manual_section(file_path.read_text(encoding="utf-8"))
    return actual_generated.strip() != join_lines(expected_lines).strip()


def requirement_design_status(requirement: dict[str, Any]) -> str:
    designs = requirement.get("designs", [])
    if not designs:
        return "未记录"
    if any(design["status"] == "accepted" for design in designs):
        return "accepted"
    if any(design["status"] == "draft" for design in designs):
        return "draft"
    return "、".join(sorted({design["status"] for design in designs}))


def requirement_markdown_lines(requirement: dict[str, Any]) -> list[str]:
    flow_label = requirement["flow_type"]
    description = requirement_source_text(requirement) if is_imported_history_requirement(requirement) else requirement["description"]
    lines = [
        f"# {requirement['requirement_id']} {requirement['title']}",
        "",
        f"- 状态：{requirement['status']}",
        f"- 优先级：{requirement.get('priority', 'normal')}",
        f"- 阻塞：{requirement.get('blocked_reason') or '无'}",
        f"- 流程：{flow_label}",
        f"- 创建时间：{requirement['created_at']}",
        "",
        "## 原始需求",
    ]
    lines.extend(format_markdown_content_lines(description))
    return lines


def plan_markdown_lines(requirement: dict[str, Any]) -> list[str]:
    task_count = len(requirement["tasks"])
    subtask_count = requirement_subtask_count(requirement)
    done_count = requirement_done_task_count(requirement)
    preflight_tasks = requirement.get("preflight_tasks", [])
    plan_lines = [
        f"# {requirement['requirement_id']} 任务计划",
        "",
        "## 任务汇总",
        f"- 正式任务：{task_count} 个",
        f"- 子检查项：{subtask_count} 个",
        f"- 开工准备项：{len(preflight_tasks)} 个",
        f"- 已完成：{done_count}/{task_count}",
        "- 开发规则：Codex 按正式任务推进，子检查项只在对应任务内部覆盖。",
        "- 开工准备项由 `tasks` 正式合同和 `review` 整套任务审核固定，审核通过后直接进入 `$sdlc-task`。",
        "- 任务详情：查看 `tasks/T-xxx.md`。",
        "- 任务映射：查看 `task-map.md`。",
        "",
        "## 任务列表",
    ]
    plan_lines.extend([task_display_line(task, show_subtasks=True) for task in requirement["tasks"]] or ["- 暂无任务"])
    if preflight_tasks:
        plan_lines.extend(["", "## 开工准备项"])
        for item in preflight_tasks:
            source_id = item.get("source_task_id", "SRC")
            title = item.get("title", "")
            plan_lines.append(f"- {source_id} {title}".rstrip())
    if requirement["changes"]:
        plan_lines.extend(["", "## 待处理变更"])
        pending_changes = [
            item
            for item in requirement["changes"]
            if item["status"] in {"draft", "effective", "pending"}
        ]
        plan_lines.extend(
            [f"- {item['change_id']} [{item['status']}] {item['summary']}" for item in pending_changes]
            or ["- 当前没有待处理变更"]
        )
    return plan_lines


def task_map_markdown_lines(requirement: dict[str, Any]) -> list[str]:
    task_count = len(requirement["tasks"])
    subtask_count = requirement_subtask_count(requirement)
    done_count = requirement_done_task_count(requirement)
    preflight_tasks = requirement.get("preflight_tasks", [])
    usage_rules = [
        "- Codex 开发、完成、恢复和验证都按正式任务编号推进。",
        "- 本文件用于查看正式任务、覆盖需求点、覆盖测试和当前任务要注意的业务规则。",
        "- 子检查项只作为任务内部检查，不单独当成正式任务。",
        "- 开工准备项写入 `tasks` 正式合同并进入整套任务审核，不单独执行 `$sdlc-task`。",
    ]
    usage_rules.append("- 当前需求按 SDLC 原生任务清单推进，以正式需求档案、`tasks` 任务合同和 task-run 读取清单为准。")
    usage_rules.append("- 需要看任务大纲时先看 `plan.md` 顶部，需要看覆盖关系时看本文件，需要执行任务时看 `tasks/T-xxx.md`。")
    lines = [
        f"# {requirement['requirement_id']} 任务映射",
        "",
        "## 数量",
        f"- 正式任务：{task_count} 个",
        f"- 子检查项：{subtask_count} 个",
        f"- 开工准备项：{len(preflight_tasks)} 个",
        f"- 已完成：{done_count}/{task_count}",
        "",
        "## 使用规则",
        *usage_rules,
        "",
        "## 映射关系",
    ]
    if not requirement["tasks"]:
        lines.append("- 暂无任务")
        if preflight_tasks:
            lines.extend(["", "## 开工准备项"])
            for item in preflight_tasks:
                lines.append(f"- {item.get('source_task_id', 'SRC')} {item.get('title', '')}".rstrip())
        return lines

    fr_to_tasks: dict[str, list[str]] = {}
    ac_to_tasks: dict[str, list[str]] = {}
    tc_to_tasks: dict[str, list[str]] = {}

    for task in requirement["tasks"]:
        subtasks = task.get("subtasks", [])
        coverage_acceptance = derive_acceptance_for_task(requirement, task)
        coverage_tests = list_value(task.get("coverage_tests"))
        for point_id in list_value(task.get("coverage_points")):
            key = str(point_id).strip()
            if key:
                fr_to_tasks.setdefault(key, []).append(str(task["task_id"]))
        for acceptance_id in coverage_acceptance:
            key = str(acceptance_id).strip()
            if key:
                ac_to_tasks.setdefault(key, []).append(str(task["task_id"]))
        for case_id in coverage_tests:
            key = str(case_id).strip()
            if key:
                tc_to_tasks.setdefault(key, []).append(str(task["task_id"]))
        lines.extend([f"### {task['task_id']} [{task['status']}] {display_task_title(task)}"])
        if task.get("source_task_id"):
            lines.append(f"- 来源：{task.get('source_task_id')}")
        lines.append(f"- 覆盖需求点：{'、'.join(task.get('coverage_points', [])) if task.get('coverage_points') else '暂无'}")
        lines.append(f"- 覆盖验收：{'、'.join(coverage_acceptance) if coverage_acceptance else '暂无'}")
        lines.append(f"- 覆盖测试：{'、'.join(coverage_tests) if coverage_tests else '暂无'}")
        formal_requirement_refs = formal_requirement_refs_for_task(requirement, task)
        formal_design_refs = formal_design_refs_for_task(requirement, task)
        formal_test_refs = formal_test_refs_for_task(requirement, task)
        if formal_requirement_refs or formal_design_refs or formal_test_refs:
            lines.append("- 正式文档依据：")
            if formal_requirement_refs:
                lines.append("  - 正式需求说明书：" + "；".join(formal_requirement_refs))
            if formal_design_refs:
                lines.append("  - 正式技术方案：" + "；".join(formal_design_refs))
            if formal_test_refs:
                lines.append("  - 正式测试矩阵：" + "；".join(formal_test_refs))
        if task.get("business_rules"):
            lines.append("- 本任务必须注意的业务规则：")
            lines.extend(f"  - {current_runtime_text(requirement, item)}" for item in task.get("business_rules", []))
        out_of_scope = task_out_of_scope(requirement, task)
        if out_of_scope:
            lines.append("- 本任务不做范围：")
            lines.extend(f"  - {current_runtime_text(requirement, item)}" for item in out_of_scope)
        lines.append(f"- 子检查项：{len(subtasks)} 个")
        if subtasks:
            lines.append("- 覆盖细项：")
            for item in subtasks:
                source_id = subtask_display_label(item.get("source_task_id", "SRC"))
                title = current_runtime_text(requirement, item.get("title", ""))
                lines.append(f"  - {source_id} {title}".rstrip())
        else:
            lines.append("- 覆盖细项：无")
        lines.append("")

    lines.extend(["## FR 到任务"])
    requirement_points = requirement.get("structured", {}).get("requirement_points", [])
    if requirement_points:
        for point in requirement_points:
            point_id = str(point.get("id", "")).strip()
            if point_id:
                lines.append(f"- {point_id} -> {'、'.join(fr_to_tasks.get(point_id, [])) if fr_to_tasks.get(point_id) else '暂无'}")
    else:
        lines.append("- 暂无功能需求")
    lines.extend(["", "## AC 到任务"])
    acceptance_points = requirement.get("structured", {}).get("acceptance_points", [])
    if acceptance_points:
        for point in acceptance_points:
            point_id = str(point.get("id", "")).strip()
            if point_id:
                lines.append(f"- {point_id} -> {'、'.join(ac_to_tasks.get(point_id, [])) if ac_to_tasks.get(point_id) else '暂无'}")
    else:
        lines.append("- 暂无验收标准")
    lines.extend(["", "## TC 到任务"])
    test_cases = requirement.get("structured", {}).get("test_cases", [])
    if test_cases:
        for case in test_cases:
            case_id = str(case.get("id", "")).strip()
            if case_id:
                lines.append(f"- {case_id} -> {'、'.join(tc_to_tasks.get(case_id, [])) if tc_to_tasks.get(case_id) else '暂无'}")
    else:
        lines.append("- 暂无测试用例")
    lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    if preflight_tasks:
        lines.extend(["", "## 开工准备项"])
        for item in preflight_tasks:
            lines.append(f"- {item.get('source_task_id', 'SRC')} {item.get('title', '')}".rstrip())
    return lines


def requirement_change_map_lines(requirement: dict[str, Any]) -> list[str]:
    lines = [
        f"# {requirement['requirement_id']} 变更覆盖映射",
        "",
        "## 使用规则",
        "- draft 表示变更草案，只记录讨论结果，还没写入当前生效版本。",
        "- effective 表示变更已经确认并写入当前生效版本，还没拆进任务。",
        "- pending 表示旧版本留下的待处理变更，按已生效未规划处理。",
        "- planned 表示变更已拆进任务，后续按任务开发和验收。",
        "- resolved 表示关联任务已经完成或关闭。",
        "",
        "## 覆盖关系",
    ]
    changes = requirement.get("changes", [])
    if not changes:
        lines.append("- 暂无变更")
        return lines

    for change in changes:
        planned_task_lines = [f"- {item}" for item in change.get("planned_task_ids", [])] or ["- 暂无"]
        planning_status = str(change.get("planning_status", "")).strip()
        planning_status_text = "待模型规划" if planning_status == "needs_model_plan" else (planning_status or "无")
        coverage_lines = [
            f"- {str(item.get('point_id', '') + '：') if item.get('point_id') else ''}{item.get('point', '')} -> {item.get('task_id', '')}"
            for item in change.get("coverage", [])
            if item.get("point") and item.get("task_id")
        ] or ["- 暂无覆盖映射"]
        lines.extend(
            [
                "",
                f"### {change['change_id']} [{change['status']}]",
                f"- 说明：{change['summary']}",
                f"- 规划状态：{planning_status_text}",
                "",
                "#### 规划任务",
                *planned_task_lines,
                "",
                "#### 变更点映射",
                *coverage_lines,
            ]
        )
    return lines


def requirement_lessons(requirement: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        capture
        for capture in requirement.get("captures", [])
        if capture.get("target_type") == "lesson" and capture.get("status") != "pending"
    ]


def lesson_brief_lines(requirement: dict[str, Any]) -> list[str]:
    lessons = requirement_lessons(requirement)
    if not lessons:
        return []
    lines: list[str] = []
    for lesson in lessons:
        summary = shorten_text(current_runtime_text(requirement, lesson.get("summary", "")), 96)
        commands = [str(item) for item in lesson.get("commands", []) if str(item).strip()]
        command_text = f"；可复用命令：{'；'.join(commands[:3])}" if commands else ""
        lines.append(f"- {lesson['capture_id']}：{summary}{command_text}")
    return lines


def requirement_lessons_markdown_lines(requirement: dict[str, Any]) -> list[str]:
    lines = [
        f"# {requirement['requirement_id']} 经验沉淀",
        "",
        "## 使用规则",
        "- 这里记录本需求开发、调试、测试和验收过程中已经确认的经验。",
        "- 后续任务开始前要先读取这些经验，避免重复搜索、重复排查和重复踩坑。",
        "- 经验如果后来不适用，追加一条修正经验，不直接删除旧记录。",
        "",
        "## 经验列表",
    ]
    lessons = requirement_lessons(requirement)
    if not lessons:
        lines.append("- 暂无需求级经验")
        return lines

    for lesson in lessons:
        command_lines = [f"- {current_runtime_text(requirement, item)}" for item in lesson.get("commands", [])] or ["- 暂无"]
        file_lines = [f"- {item}" for item in lesson.get("changed_files", [])] or ["- 暂无"]
        question_lines = [f"- {item}" for item in lesson.get("questions", [])] or ["- 暂无"]
        source_task = str(lesson.get("task_id", "")).strip() or "未指定"
        lines.extend(
            [
                "",
                f"### {lesson['capture_id']}",
                f"- 时间：{lesson['created_at']}",
                f"- 状态：{lesson['status']}",
                f"- 来源任务：{source_task}",
                "",
                "#### 结论",
                *format_markdown_content_lines(current_runtime_text(requirement, lesson.get("summary", ""))),
                "",
                "#### 补充说明",
                *format_markdown_content_lines(current_runtime_text(requirement, lesson.get("note") or "无")),
                "",
                "#### 可复用命令",
                *command_lines,
                "",
                "#### 涉及文件",
                *file_lines,
                "",
                "#### 待确认问题",
                *question_lines,
            ]
        )
    return lines


def requirement_materials(requirement: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        material
        for material in requirement.get("materials", [])
        if material.get("status", "active") == "active"
    ]


def requirement_materials_index_lines(requirement: dict[str, Any]) -> list[str]:
    lines = [
        f"# {requirement['requirement_id']} 需求资料",
        "",
        "## 使用规则",
        "- 这里保存需求背景、设计资料、截图、Figma 链接、样式规则、测试账号、测试数据和环境说明。",
        "- task-run 读取清单会按当前任务相关性引用这些资料，执行结果通过 `$sdlc-task-evidence` 登记。",
        "- 资料过期时新增一条更新资料或把旧资料归档，不直接手改历史事件。",
        "",
        "## 资料列表",
    ]
    materials = requirement_materials(requirement)
    if not materials:
        lines.append("- 暂无需求资料")
        return lines
    for material in materials:
        scopes = "、".join(str(item) for item in material.get("scope", []) if str(item).strip()) or "未限定"
        tasks = "、".join(str(item) for item in material.get("task_ids", []) if str(item).strip()) or "未指定"
        lines.append(
            f"- {material['material_id']} [{material.get('material_type', 'other')}] {material.get('title', '')}；范围：{scopes}；任务：{tasks}"
        )
    return lines


def material_markdown_lines(material: dict[str, Any]) -> list[str]:
    scope_lines = [f"- {item}" for item in material.get("scope", [])] or ["- 未限定"]
    task_lines = [f"- {item}" for item in material.get("task_ids", [])] or ["- 未指定"]
    file_lines = [f"- {item}" for item in material.get("changed_files", [])] or ["- 暂无"]
    asset_lines = [
        f"- {item.get('stored', '')}（原始来源：{item.get('source', '未记录')}）"
        for item in material.get("asset_files", [])
        if isinstance(item, dict) and item.get("stored")
    ] or ["- 暂无"]
    command_lines = [f"- {item}" for item in material.get("commands", [])] or ["- 暂无"]
    return [
        f"# {material['material_id']} {material.get('title', '')}",
        "",
        f"- 类型：{material.get('material_type', 'other')}",
        f"- 状态：{material.get('status', 'active')}",
        f"- 时间：{material.get('created_at', '')}",
        f"- 来源：{material.get('source') or '未记录'}",
        "",
        "## 适用范围",
        *scope_lines,
        "",
        "## 关联任务",
        *task_lines,
        "",
        "## 内容",
        *format_markdown_content_lines(material.get("summary", "")),
        "",
        "## 涉及文件或资料",
        *file_lines,
        "",
        "## 已保存附件副本",
        *asset_lines,
        "",
        "## 涉及命令",
        *command_lines,
    ]


def grill_mode_text(mode: str) -> str:
    return {
        "requirement": "需求质询",
        "product": "产品设计质询",
        "design": "技术方案质询",
        "change": "需求变更质询",
        "goal": "Goal 模式质询",
        "task": "任务开工质询",
    }.get(mode, mode or "质询")


def grill_status_text(status: str) -> str:
    return {
        "resolved": "已解决",
        "no_issue": "无需追问",
        "needs_user": "需要用户回答",
    }.get(status, status or "已记录")


def grill_markdown_lines(grill: dict[str, Any]) -> list[str]:
    question_lines = [f"- {item}" for item in grill.get("questions", [])] or ["- 本轮没有需要用户回答的问题"]
    answer_lines = [f"- {item}" for item in grill.get("answers", [])] or ["- 暂无自答或用户回答"]
    target_lines: list[str] = []
    if grill.get("requirement_id"):
        target_lines.append(f"- 需求：{grill['requirement_id']}")
    if grill.get("task_id"):
        target_lines.append(f"- 任务：{grill['task_id']}")
    if not target_lines:
        target_lines.append("- 范围：全局讨论")

    return [
        f"# {grill['grill_id']} {grill_mode_text(str(grill.get('mode', '')))}",
        "",
        f"- 状态：{grill_status_text(str(grill.get('status', '')))}",
        f"- 时间：{grill.get('created_at', '')}",
        f"- 来源：{grill.get('source') or '未记录'}",
        "",
        "## 关联范围",
        *target_lines,
        "",
        "## 结论",
        *format_markdown_content_lines(grill.get("summary") or "本轮质询没有额外结论。"),
        "",
        "## 关键问题",
        *question_lines,
        "",
        "## 已有回答",
        *answer_lines,
        "",
        "## 推荐处理",
        *format_markdown_content_lines(grill.get("recommendation") or "按当前阶段继续推进。"),
    ]


def requirement_grills_index_lines(requirement: dict[str, Any]) -> list[str]:
    lines = [
        f"# {requirement['requirement_id']} 质询记录",
        "",
        "## 使用规则",
        "- 这里记录需求、产品设计、技术方案、任务开工和变更处理中问清楚的问题。",
        "- 已解决或无需追问的记录，用来说明为什么可以继续推进。",
        "- 需要用户回答的记录，要先补齐再继续对应阶段。",
        "",
        "## 记录列表",
    ]
    grills = requirement.get("grills", [])
    if not grills:
        lines.append("- 暂无质询记录")
        return lines
    for grill in grills:
        task_suffix = f"；任务：{grill.get('task_id')}" if grill.get("task_id") else ""
        lines.append(
            f"- {grill['grill_id']} [{grill_status_text(str(grill.get('status', '')))}] "
            f"{grill_mode_text(str(grill.get('mode', '')))}{task_suffix}："
            f"{shorten_text(str(grill.get('summary', '')), 72)}"
        )
    return lines


def global_grills_index_lines(state: dict[str, Any]) -> list[str]:
    lines = [
        "# 全局质询记录",
        "",
        "## 使用规则",
        "- 这里保存尚未关联到具体需求的质询记录。",
        "- 需求创建后，如果这些记录已经纳入正式需求，可在需求包的 `grills/` 里继续查看。",
        "",
        "## 记录列表",
    ]
    global_grills = [grill for grill in state.get("grills", []) if not grill.get("requirement_id")]
    if not global_grills:
        lines.append("- 暂无全局质询记录")
        return lines
    for grill in global_grills:
        lines.append(
            f"- {grill['grill_id']} [{grill_status_text(str(grill.get('status', '')))}] "
            f"{grill_mode_text(str(grill.get('mode', '')))}：{shorten_text(str(grill.get('summary', '')), 72)}"
        )
    return lines


def task_regression_scope(task: dict[str, Any]) -> list[str]:
    scopes: list[str] = []
    dependencies = [str(item) for item in task.get("depends_on", []) if str(item)]
    if dependencies:
        scopes.append("回归前置任务：" + "、".join(dependencies))
    source_task_id = str(task.get("source_task_id", ""))
    if str(task.get("task_kind") or "") == "fix" and source_task_id:
        scopes.append("回归修复来源：" + source_task_id.removeprefix("FIX-"))
    if task.get("subtasks"):
        scopes.append(f"回归子检查项覆盖范围：{task_subtask_count(task)} 个子检查项")
    if not scopes:
        scopes.append("按本任务涉及文件和用户可见行为做局部回归")
    return scopes


def requirement_test_plan_markdown_lines(requirement: dict[str, Any]) -> list[str]:
    task_count = len(requirement["tasks"])
    subtask_count = requirement_subtask_count(requirement)
    done_count = requirement_done_task_count(requirement)
    lines = [
        f"# {requirement['requirement_id']} 测试计划",
        "",
        "## 总览",
        f"- 正式任务：{task_count} 个",
        f"- 子检查项：{subtask_count} 个",
        f"- 已完成任务：{done_count}/{task_count}",
        "",
        "## 测试规则",
        "- 开始任务前先审核测试方案，发现缺口先补测试项、测试命令或人工验收点。",
        "- 新功能、修复和行为变更默认按 TDD 执行：先 RED，再 GREEN，最后回归。",
        "- 没有合适 CLI 测试工具时，可以使用 computer use 做真实界面验证，并把过程和结论写入验证记录。",
        "- 测试失败不能继续下一个任务；任务必须先回到修复和重新验证。",
        "",
        "## 需求级经验",
        *lesson_brief_lines(requirement),
        "",
        "## 任务测试契约",
    ]
    if not requirement["tasks"]:
        lines.append("- 暂无任务")
        return lines

    for task in requirement["tasks"]:
        test_item_lines = [f"- {current_runtime_text(requirement, item)}" for item in list_value(task.get("test_items"))] or ["- 暂无自动测试项"]
        test_command_lines = [f"- {current_runtime_text(requirement, item)}" for item in list_value(task.get("test_commands"))] or ["- 暂无自动测试命令"]
        test_script_lines = [f"- {current_runtime_text(requirement, item)}" for item in list_value(task.get("test_scripts"))] or ["- 暂无可重复测试脚本"]
        manual_check_lines = [f"- {current_runtime_text(requirement, item)}" for item in list_value(task.get("manual_checks"))] or ["- 暂无人工验收点"]
        regression_lines = [f"- {current_runtime_text(requirement, item)}" for item in task_regression_scope(task)]
        verification_lines = [
            f"- {item['verification_id']}：{current_runtime_text(requirement, item['summary'])}"
            for item in task.get("verifications", [])
        ] or ["- 暂无验证记录"]
        lines.extend(
            [
                "",
                f"### {task['task_id']} [{task['status']}] {display_task_title(task)}",
                "",
                "#### 测试项",
                *test_item_lines,
                "",
                "#### 测试命令",
                *test_command_lines,
                "",
                "#### 可重复测试脚本",
                *test_script_lines,
                "",
                "#### 人工验收点",
                *manual_check_lines,
                "",
                "#### 回归范围",
                *regression_lines,
                "",
                "#### 验证记录",
                *verification_lines,
            ]
        )
    return lines


def requirement_design_lines(requirement: dict[str, Any]) -> list[str]:
    lines = [
        f"# {requirement['requirement_id']} 技术方案",
        "",
        f"- 当前状态：{requirement_design_status(requirement)}",
        "",
        "## 方案记录",
    ]
    if not requirement.get("designs"):
        lines.append("- 暂无技术方案记录")
        return lines

    for design in requirement["designs"]:
        lines.extend(
            [
                f"### {design['design_id']} {design['title']}",
                f"- 状态：{design['status']}",
                f"- 创建时间：{design['created_at']}",
                f"- 确认时间：{design.get('accepted_at') or '未确认'}",
                "",
            ]
        )
        lines.extend(format_design_summary_lines(design["summary"]))
        lines.append("")
    return lines


def render_requirement_section_lines(
    values: list[Any],
    *,
    text: Callable[[Any], str],
    empty_text: str,
) -> list[str]:
    items = [text(item).strip() for item in values if text(item).strip()]
    if not items:
        return [f"- {empty_text}"]
    return [f"- {item}" for item in items]


def structured_requirement_lines(requirement: dict[str, Any], *, current: bool) -> list[str]:
    structured = requirement.get("structured", {})
    design = structured.get("design", {}) if isinstance(structured.get("design"), dict) else {}
    version = structured.get("requirement_version", "requirement.v1")
    title = "当前生效需求版本" if current else "需求版本快照"
    text = (lambda value: current_runtime_text(requirement, value)) if current else (lambda value: str(value or ""))
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    requirement_points = [value(raw_point) for raw_point in structured.get("requirement_points", [])]
    acceptance_points = [value(raw_item) for raw_item in structured.get("acceptance_points", [])]
    effective_changes = [value(raw_item) for raw_item in structured.get("effective_changes", [])] if current else []
    # 正式需求只归并需求侧规则；技术内部结构继续留在 design.current.md，避免任务依据串味。
    permission_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("permission_rules"))),
            *(str(item).strip() for point in requirement_points for item in list_value(point.get("permissions"))),
        ]
    ))
    data_state_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("data_state_rules"))),
        ]
    ))
    interface_scope_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("interface_scope"))),
        ]
    ))
    exception_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("exception_rules"))),
            *(str(item).strip() for point in requirement_points for item in list_value(point.get("exceptions"))),
        ]
    ))
    test_focus_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("test_focus"))),
        ]
    ))
    risk_rules = list(dict.fromkeys(
        [
            *(text(item).strip() for item in list_value(structured.get("risks"))),
            *(text(item).strip() for item in list_value(design.get("risks"))),
        ]
    ))
    user_scene_lines = render_requirement_section_lines(
        list_value(structured.get("user_scenarios")),
        text=text,
        empty_text="未记录",
    )
    scope_lines = render_requirement_section_lines(
        list_value(structured.get("scope")),
        text=text,
        empty_text="未记录",
    )
    out_of_scope_lines = render_requirement_section_lines(
        list_value(structured.get("out_of_scope")),
        text=text,
        empty_text="未记录",
    )
    lines = [
        f"# {requirement['requirement_id']} {text(requirement['title'])}",
        "",
        f"- 文档类型：{title}",
        f"- 需求版本：{version}",
        f"- 执行口径：{'是' if current else '否'}",
        f"- 迁移状态：{structured.get('migration_status', 'structured')}",
        "",
        "## 背景",
        *format_markdown_content_lines(text(structured.get("background") or requirement.get("summary") or requirement.get("description", ""))),
        "",
        "## 目标",
        *format_markdown_content_lines(text(structured.get("goal") or requirement.get("summary") or requirement.get("title", ""))),
        "",
        "## 用户和使用场景",
        *user_scene_lines,
        "",
        "## 本轮范围",
        *scope_lines,
        "",
        "## 不做范围",
        *out_of_scope_lines,
        "",
        "## 功能需求",
    ]
    for point in requirement_points:
        heading = point.get("title") or point.get("summary", "")
        lines.extend(
            [
                f"### {point['id']} {heading}".rstrip(),
                f"- 状态：{point.get('status', 'active')}",
                f"- 版本：{point.get('version', version)}",
                "- 说明：",
                *format_markdown_content_lines(point.get("description", "")),
            ]
        )
        rules = point.get("rules", [])
        if rules:
            lines.append("- 规则：")
            lines.extend(f"  - {item}" for item in rules)
        inputs = [str(item).strip() for item in list_value(point.get("inputs")) if str(item).strip()]
        if inputs:
            lines.append("- 输入：")
            lines.extend(f"  - {item}" for item in inputs)
        outputs = [str(item).strip() for item in list_value(point.get("outputs")) if str(item).strip()]
        if outputs:
            lines.append("- 输出：")
            lines.extend(f"  - {item}" for item in outputs)
        triggers = [str(item).strip() for item in list_value(point.get("triggers")) if str(item).strip()]
        if triggers:
            lines.append("- 触发条件：")
            lines.extend(f"  - {item}" for item in triggers)
        data_changes = [str(item).strip() for item in list_value(point.get("data_changes")) if str(item).strip()]
        if data_changes:
            lines.append("- 保存或改变的数据：")
            lines.extend(f"  - {item}" for item in data_changes)
        permissions = [str(item).strip() for item in list_value(point.get("permissions")) if str(item).strip()]
        if permissions:
            lines.append("- 权限：")
            lines.extend(f"  - {item}" for item in permissions)
        exceptions = [str(item).strip() for item in list_value(point.get("exceptions")) if str(item).strip()]
        if exceptions:
            lines.append("- 异常：")
            lines.extend(f"  - {item}" for item in exceptions)
        boundaries = [str(item).strip() for item in list_value(point.get("boundaries")) if str(item).strip()]
        if boundaries:
            lines.append("- 边界：")
            lines.extend(f"  - {item}" for item in boundaries)
        acceptance_ids = [str(item).strip() for item in list_value(point.get("acceptance_ids")) if str(item).strip()]
        if acceptance_ids:
            lines.append("- 验收关联：" + "、".join(acceptance_ids))
        material_refs = point.get("material_refs", []) or point.get("materials", [])
        if material_refs:
            lines.append("- 关联材料：" + "、".join(str(item) for item in material_refs))
        lines.append("")
    lines.append("## 验收标准")
    for item in acceptance_points:
        title_text = str(item.get("title") or item.get("description") or item.get("expected") or "").strip()
        lines.append(f"### {item['id']} {title_text}".rstrip())
        requirement_refs = [str(ref).strip() for ref in item.get("requirement_ids", []) or item.get("fr_ids", []) if str(ref).strip()]
        if requirement_refs:
            lines.append("- 覆盖需求：" + "、".join(requirement_refs))
        if item.get("operation"):
            lines.append(f"- 操作：{item['operation']}")
        if item.get("expected"):
            lines.append(f"- 预期：{item['expected']}")
        if item.get("pass_standard"):
            lines.append(f"- 通过标准：{item['pass_standard']}")
        if item.get("task_id"):
            lines.append(f"- 关联任务：{item['task_id']}")
        lines.append("")
    if effective_changes:
        lines.append("## 已确认变更附录")
        lines.append("- 这些变更是已确认事实，不会在字段不完整时伪装成新的功能需求。")
        lines.append("- 相关任务必须同时读取对应 CHG 记录和原功能需求。")
        for change in effective_changes:
            lines.extend(
                [
                    "",
                    f"### {change.get('change_id', '')} {change.get('summary', '')}".rstrip(),
                    f"- 状态：{change.get('status', '') or 'effective'}",
                    f"- 说明：{change.get('description', '') or change.get('summary', '')}",
                ]
            )
            acceptance = [str(item).strip() for item in list_value(change.get("acceptance_points")) if str(item).strip()]
            if acceptance:
                lines.append("- 验收要求：")
                lines.extend(f"  - {item}" for item in acceptance)
        lines.append("")
    lines.extend(["## 业务规则", *render_requirement_section_lines(list_value(structured.get("business_rules")), text=text, empty_text="暂无额外业务规则"), ""])
    lines.extend(["## 权限规则", *render_requirement_section_lines(permission_rules, text=lambda item: str(item), empty_text="暂无额外权限规则"), ""])
    lines.extend(["## 数据和状态规则", *render_requirement_section_lines(data_state_rules, text=lambda item: str(item), empty_text="暂无额外数据或状态规则"), ""])
    lines.extend(["## 接口或页面范围", *render_requirement_section_lines(interface_scope_rules, text=lambda item: str(item), empty_text="暂无额外接口或页面范围"), ""])
    lines.extend(["## 异常和回退规则", *render_requirement_section_lines(exception_rules, text=lambda item: str(item), empty_text="暂无额外异常或回退规则"), ""])
    lines.extend(["## 测试关注点", *render_requirement_section_lines(test_focus_rules, text=lambda item: str(item), empty_text="未记录"), ""])
    lines.extend(["## 风险和处理方式", *render_requirement_section_lines(risk_rules, text=lambda item: str(item), empty_text="暂无额外风险"), ""])
    lines.extend(["## 未确认问题", *render_requirement_section_lines(list_value(structured.get("open_questions")), text=text, empty_text="暂无未确认问题")])
    return lines


def original_requirement_lines(requirement: dict[str, Any]) -> list[str]:
    lines = structured_requirement_lines(requirement, current=False)
    lines[0] = f"# {requirement['requirement_id']} 原始需求版本"
    lines[3] = "- 执行口径：否"
    lines.extend(["", "## 原始自然语言需求"])
    lines.extend(format_markdown_content_lines(requirement["description"]))
    return lines


def test_matrix_lines(requirement: dict[str, Any], *, current: bool) -> list[str]:
    structured = requirement.get("structured", {})
    version = structured.get("test_matrix_version", "test-matrix.v1")
    text = (lambda value: current_runtime_text(requirement, value)) if current else (lambda value: str(value or ""))
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    acceptance_points: list[dict[str, Any]] = []
    for raw_item in structured.get("acceptance_points", []):
        item = value(raw_item)
        if isinstance(item, dict):
            acceptance_points.append(item)
    acceptance_requirement_ids = {
        str(item.get("id", "")).strip(): [
            str(ref).strip()
            for ref in list_value(item.get("requirement_ids") or item.get("fr_ids"))
            if str(ref).strip()
        ]
        for item in acceptance_points
        if str(item.get("id", "")).strip()
    }
    lines = [
        f"# {requirement['requirement_id']} {'当前生效测试矩阵' if current else '测试矩阵版本快照'}",
        "",
        f"- 测试矩阵版本：{version}",
        f"- 执行口径：{'是' if current else '否'}",
        "",
        "## 测试状态规则",
        "- active：当前正向验收和回归要执行。",
        "- deprecated：旧口径测试，不能再作为正向验收。",
        "- negative_regression：旧口径转成负向回归。",
        "- historical_defect：历史缺陷回归，要在相关范围内继续执行。",
        "- manual_only：需要人工或视觉验收。",
        "- blocked：暂时无法执行，需要说明原因。",
        "",
        "## 当前测试用例",
    ]
    for raw_case in structured.get("test_cases", []):
        case = value(raw_case)
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id", "")).strip()
        if not case_id:
            continue
        description = text(case.get("description") or case.get("method") or case.get("pass_standard") or case_id)
        acceptance_ids = [str(item).strip() for item in list_value(case.get("acceptance_ids")) if str(item).strip()]
        requirement_ids = [str(item).strip() for item in list_value(case.get("requirement_ids")) if str(item).strip()]
        # 这里优先保留正式建档 JSON 里直接给出的 requirement_ids；
        # 旧包只有 acceptance_ids 时，再根据 AC 反推 FR，保证 DEV-010 要求的 TC -> AC / FR 都能在 Markdown 里直接看见。
        if not requirement_ids and acceptance_ids:
            seen_requirement_ids: set[str] = set()
            for acceptance_id in acceptance_ids:
                for requirement_id in acceptance_requirement_ids.get(acceptance_id, []):
                    if requirement_id and requirement_id not in seen_requirement_ids:
                        requirement_ids.append(requirement_id)
                        seen_requirement_ids.add(requirement_id)
        operation = text(case.get("operation") or case.get("method") or "未记录")
        expected = text(case.get("expected") or "未记录")
        pass_standard = text(case.get("pass_standard") or "未记录")
        task_id = text(case.get("task_id"))
        lines.extend(
            [
                f"### {case_id} {description}".rstrip(),
                f"- 状态：{text(case.get('status') or 'active')}",
                f"- 类型：{text(case.get('type') or 'task_check')}",
                f"- 覆盖验收：{'、'.join(acceptance_ids) if acceptance_ids else '暂无'}",
                f"- 覆盖需求：{'、'.join(requirement_ids) if requirement_ids else '暂无'}",
                f"- 操作：{operation}",
                f"- 预期：{expected}",
                f"- 通过标准：{pass_standard}",
                f"- 关联任务：{task_id or '暂无'}",
                "",
            ]
        )
    return lines


def design_current_lines(requirement: dict[str, Any], *, current: bool) -> list[str]:
    structured = requirement.get("structured", {})
    version = structured.get("design_version", "design.v1")
    text = (lambda value: current_runtime_text(requirement, value)) if current else (lambda value: str(value or ""))
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    design = value(structured.get("design", {})) if isinstance(structured.get("design"), dict) else {}
    design_records = requirement.get("designs") or []

    # 这里把正式技术方案固定成 12 个章节，后续 tasks、task-run 和人工验收都能直接按章节拿信息，
    # 不需要再从一段 summary 里猜接口、状态、权限和测试口径。
    def section_lines(values: list[Any], *, empty_text: str, formatter: Callable[[Any], str] | None = None) -> list[str]:
        fn = formatter or (lambda item: text(item))
        return render_requirement_section_lines(values, text=fn, empty_text=empty_text)

    design_title_text = text(design.get("title"))
    design_summary_text = text(design.get("summary"))
    tech_goal_parts = [part for part in [text(design.get("technical_goal")), design_summary_text, design_title_text] if part.strip()]
    tech_goal_source = "\n\n".join(dict.fromkeys(tech_goal_parts))
    requirement_links = [text(item).strip() for item in list_value(design.get("requirement_coverage")) if text(item).strip()]
    module_lines = section_lines(
        list_value(design.get("modules")),
        empty_text="未记录",
    )
    data_structure_lines = section_lines(
        list_value(design.get("data_structures")),
        empty_text="未记录",
    )
    interface_lines = section_lines(
        list_value(design.get("interfaces")),
        empty_text="未记录",
    )
    state_flow_lines = section_lines(
        list_value(design.get("state_flow")),
        empty_text="未记录",
    )
    data_flow_lines = section_lines(
        list_value(design.get("data_flow")),
        empty_text="未记录",
    )
    permission_values = list(dict.fromkeys(text(item).strip() for item in list_value(design.get("permissions_security"))))
    permission_lines = section_lines(
        permission_values,
        empty_text="未记录",
        formatter=lambda item: str(item),
    )
    error_values = list(dict.fromkeys(text(item).strip() for item in list_value(design.get("error_handling"))))
    error_lines = section_lines(
        error_values,
        empty_text="未记录",
        formatter=lambda item: str(item),
    )
    test_strategy_values = list(dict.fromkeys(text(item).strip() for item in list_value(design.get("test_strategy"))))
    test_strategy_lines = section_lines(
        test_strategy_values,
        empty_text="未记录",
        formatter=lambda item: str(item),
    )
    risk_values = list(dict.fromkeys(text(item).strip() for item in list_value(design.get("risks"))))
    risk_lines = section_lines(
        risk_values,
        empty_text="未记录",
        formatter=lambda item: str(item),
    )
    out_of_scope_values = list(dict.fromkeys(text(item).strip() for item in list_value(design.get("out_of_scope"))))
    out_of_scope_lines = section_lines(
        out_of_scope_values,
        empty_text="未记录",
        formatter=lambda item: str(item),
    )
    lines = [
        f"# {requirement['requirement_id']} {'当前生效技术方案' if current else '技术方案版本快照'}",
        "",
        f"- 技术方案版本：{version}",
        f"- 适用需求版本：{structured.get('requirement_version', 'requirement.v1')}",
        f"- 执行口径：{'是' if current else '否'}",
        f"- 当前状态：{requirement_design_status(requirement)}",
    ]
    if design_records:
        latest_design = design_records[-1]
        lines.extend(
            [
                f"- 当前方案记录：{latest_design.get('design_id', '')}".rstrip(),
                f"- 状态：{latest_design.get('status', '') or requirement_design_status(requirement)}",
                f"- 方案标题：{text(latest_design.get('title')) or '未命名技术方案'}",
                f"- 方案确认时间：{latest_design.get('accepted_at') or latest_design.get('created_at') or '未确认'}",
            ]
        )
    lines.extend(
        [
            "",
            "## 技术目标",
            *format_markdown_content_lines(tech_goal_source),
            "",
            "## 对应需求",
            *section_lines(requirement_links, empty_text="未记录", formatter=lambda item: str(item)),
            "",
            "## 涉及模块",
            *module_lines,
            "",
            "## 数据结构",
            *data_structure_lines,
            "",
            "## 接口设计",
            *interface_lines,
            "",
            "## 状态流",
            *state_flow_lines,
            "",
            "## 数据流",
            *data_flow_lines,
            "",
            "## 权限和安全",
            *permission_lines,
            "",
            "## 错误处理",
            *error_lines,
            "",
            "## 测试策略",
            *test_strategy_lines,
            "",
            "## 风险和处理方式",
            *risk_lines,
            "",
            "## 本轮不做",
            *out_of_scope_lines,
        ]
    )
    if not design_records and not any(
        [
            tech_goal_source.strip(),
            requirement_links,
            list_value(design.get("modules")),
            list_value(design.get("data_structures")),
            list_value(design.get("interfaces")),
            list_value(design.get("state_flow")),
            list_value(design.get("data_flow")),
            permission_values,
            error_values,
            test_strategy_values,
            risk_values,
            out_of_scope_values,
        ]
    ):
        lines.extend(["", "> 暂无技术方案记录；后续进入设计阶段后会更新。"])
        return lines
    return lines


def traceability_lines(requirement: dict[str, Any]) -> list[str]:
    structured = requirement.get("structured", {})
    tasks = requirement.get("tasks", [])
    test_to_tasks: dict[str, list[str]] = {}
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        if not task_id:
            continue
        for case_id in list_value(task.get("coverage_tests")):
            case_key = str(case_id)
            if not case_key:
                continue
            test_to_tasks.setdefault(case_key, [])
            if task_id not in test_to_tasks[case_key]:
                test_to_tasks[case_key].append(task_id)
    lines = [
        f"# {requirement['requirement_id']} 追溯矩阵",
        "",
        "## 使用规则",
        "- 追溯看历史，执行看 current。",
        "- 本文件用于查看 FR/AC/TC、任务、变更和验证之间的关系。",
        "",
        "## 版本",
        f"- 需求版本：{structured.get('requirement_version', 'requirement.v1')}",
        f"- 技术方案版本：{structured.get('design_version', 'design.v1')}",
        f"- 测试矩阵版本：{structured.get('test_matrix_version', 'test-matrix.v1')}",
        "",
        "## 需求点覆盖",
    ]
    for point in structured.get("requirement_points", []):
        point_id = str(point["id"])
        covered_tasks = [
            str(task["task_id"])
            for task in tasks
            if point_id in list_value(task.get("coverage_points"))
        ]
        lines.extend(
            [
                f"### {point_id} {point.get('summary', '')}",
                f"- 状态：{point.get('status', 'active')}",
                f"- 版本：{point.get('version', structured.get('requirement_version', 'requirement.v1'))}",
                f"- 覆盖任务：{'、'.join(covered_tasks) if covered_tasks else '暂无'}",
                "",
            ]
        )
    lines.append("## 测试覆盖")
    for case in structured.get("test_cases", []):
        task_ids: list[str] = []
        if case.get("task_id"):
            task_ids.append(str(case.get("task_id")))
        for task_id in test_to_tasks.get(str(case.get("id", "")), []):
            if task_id not in task_ids:
                task_ids.append(task_id)
        lines.append(
            f"- {case['id']} [{case.get('status', 'active')}] -> "
            f"{'、'.join(task_ids) if task_ids else '未绑定任务'}：{current_runtime_text(requirement, case.get('description', ''))}"
        )
    if requirement.get("changes"):
        lines.extend(["", "## 变更追溯"])
        for change in requirement["changes"]:
            lines.append(f"- {change['change_id']} [{change['status']}] {change['summary']}")
    return lines


def task_markdown_lines(task: dict[str, Any]) -> list[str]:
    verification_lines = [f"- {item['verification_id']}：{sanitize_runtime_text(item['summary'])}" for item in list_value(task.get("verifications"))] or ["- 暂无验证记录"]
    changed_file_lines = [f"- {sanitize_runtime_text(item)}" for item in task["changed_files"]] or ["- 暂无记录"]
    file_hint_values = list_value(task.get("context_files")) + list_value(task.get("output_files")) + list_value(task.get("related_files"))
    file_hint_lines = [f"- {sanitize_runtime_text(item)}" for item in dict.fromkeys(file_hint_values)] or ["- 暂无提前记录"]
    command_lines = [f"- {sanitize_runtime_text(item)}" for item in task["commands"]] or ["- 暂无记录"]
    test_item_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("test_items"))] or ["- 暂无自动测试项"]
    test_command_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("test_commands"))] or ["- 暂无自动测试命令"]
    test_script_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("test_scripts"))] or ["- 暂无可重复测试脚本"]
    manual_check_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("manual_checks"))] or ["- 暂无人工验收点"]
    business_rule_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("business_rules"))]
    out_of_scope_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("out_of_scope"))] or ["- 暂无单独不做范围"]
    test_suggestion_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("test_suggestions"))] or ["- 暂无单独测试建议"]
    formal_requirement_ref_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("formal_requirement_refs"))] or ["- 暂无正式需求说明书小节"]
    formal_design_ref_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("formal_design_refs"))] or ["- 暂无正式技术方案小节"]
    formal_test_ref_lines = [f"- {sanitize_runtime_text(item)}" for item in list_value(task.get("formal_test_refs"))] or ["- 暂无正式测试矩阵小节"]
    subtask_lines = [
        f"- [ ] {subtask_display_label(item.get('source_task_id', 'SRC'))} {sanitize_runtime_text(item.get('title', ''))}".rstrip()
        for item in task.get("subtasks", [])
    ] or ["- 暂无子检查项"]
    depends_on_line = "、".join(task["depends_on"]) if task["depends_on"] else "无"
    coverage_points = [str(item) for item in list_value(task.get("coverage_points")) if str(item).strip()]
    coverage_change_ids = [str(item) for item in list_value(task.get("coverage_change_ids")) if str(item).strip()]
    coverage_acceptance = [str(item) for item in list_value(task.get("coverage_acceptance")) if str(item).strip()]
    coverage_tests = [str(item) for item in list_value(task.get("coverage_tests")) if str(item).strip()]
    coverage_points_line = "、".join(coverage_points) if coverage_points else "暂无"
    coverage_acceptance_line = "、".join(coverage_acceptance) if coverage_acceptance else "暂无"
    coverage_tests_line = "、".join(coverage_tests) if coverage_tests else "暂无"
    lines = [
        f"# {task['task_id']} {sanitize_runtime_text(display_task_title(task))}",
        "",
        f"- 状态：{task['status']}",
        f"- 依赖：{depends_on_line}",
        f"- 来源任务：{task.get('source_task_id') or '无'}",
        f"- 绑定需求版本：{task.get('requirement_version', 'requirement.v1')}",
        f"- 绑定技术方案版本：{task.get('design_version', 'design.v1')}",
        f"- 绑定测试矩阵：{task.get('test_matrix_version', 'test-matrix.v1')}",
        f"- 覆盖需求点：{coverage_points_line}",
        f"- 覆盖变更：{'、'.join(coverage_change_ids) if coverage_change_ids else '暂无'}",
        f"- 覆盖验收：{coverage_acceptance_line}",
        f"- 覆盖测试：{coverage_tests_line}",
        f"- 摘要：{sanitize_runtime_text(task['summary'])}",
        f"- 子检查项：{task_subtask_count(task)} 个",
        f"- 最近更新时间：{task['updated_at']}",
        "",
        "## 说明",
    ]
    lines.extend(format_markdown_content_lines(sanitize_runtime_text(task.get("note") or "暂无补充说明")))
    if business_rule_lines:
        lines.extend(
            [
                "",
                "## 本任务必须注意的业务规则",
                *business_rule_lines,
            ]
        )
    lines.extend(
        [
            "",
            "## 正式文档依据",
            "",
            "### 正式需求说明书",
            *formal_requirement_ref_lines,
            "",
            "### 正式技术方案",
            *formal_design_ref_lines,
            "",
            "### 正式测试矩阵",
            *formal_test_ref_lines,
            "",
            "## 本任务不做范围",
            *out_of_scope_lines,
            "",
            "## 测试建议",
            *test_suggestion_lines,
        ]
    )
    if task.get("subtasks"):
        lines.extend(
            [
                "",
                "## 子检查项说明",
                "- Codex 完成本任务时要覆盖下方所有子检查项。",
                "- 子检查项只在当前正式任务内部处理，不单独作为正式任务流转。",
                "- `CHK-xxx` 是子检查项显示编号，括号里的来源编号只用于追溯。",
                "",
                "## 子检查项",
                *subtask_lines,
            ]
        )
    else:
        lines.extend(["", "## 子检查项", "- 暂无子检查项"])
    lines.extend([
        "",
        "## 开工文件线索",
        "- 这些是任务拆分时给出的参考文件，不是文件白名单；执行时仍要按真实代码继续搜索。",
        *file_hint_lines,
        "",
        "## 涉及文件",
        *changed_file_lines,
        "",
        "## 执行命令",
        *command_lines,
        "",
        "## 自动测试项",
        *test_item_lines,
        "",
        "## 自动测试命令",
        *test_command_lines,
        "",
        "## 可重复测试脚本",
        *test_script_lines,
        "",
        "## 人工验收点",
        *manual_check_lines,
        "",
        "## 验证记录",
        *verification_lines,
    ])
    return lines


def requirement_test_script_items(requirement: dict[str, Any]) -> list[tuple[str, str, str]]:
    items: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for task in requirement["tasks"]:
        task_id = str(task["task_id"])
        title = str(task["title"])
        for script in task.get("test_scripts", []):
            script_text = str(script).strip()
            if not script_text:
                continue
            key = (task_id, script_text)
            if key in seen:
                continue
            seen.add(key)
            items.append((task_id, title, script_text))
    return items


def requirement_tests_readme_lines(requirement: dict[str, Any]) -> list[str]:
    items = requirement_test_script_items(requirement)
    lines = [
        f"# {requirement['requirement_id']} 可重复测试脚本",
        "",
        "## 使用规则",
        "- 这里登记的是可以多次执行的专项测试脚本。",
        "- 优先进项目自己的测试目录；不适合进项目测试体系的脚本才放在当前需求包。",
        f"- 回归整个需求：`$sdlc-regression {requirement['requirement_id']}`",
        f"- 回归任务范围：`$sdlc-regression {requirement['requirement_id']} T-001..T-005`",
        "",
        "## 脚本清单",
    ]
    if not items:
        lines.append("- 暂无可重复测试脚本")
        return lines
    for task_id, title, script in items:
        lines.append(f"- {task_id}：{title} -> `{script}`")
    return lines



def formal_contract_package(requirement: dict[str, Any]) -> dict[str, Any]:
    """把当前正式需求还原成建档合同能检查的包。

    doctor-deep 不重新走命令入口，而是检查已经物化出来的正式状态，
    所以这里把 structured / native_start 统一整理成 StartContract 需要的字段。
    """
    structured = requirement.get("structured") if isinstance(requirement.get("structured"), dict) else {}
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    package = dict(native_start)
    package.setdefault("formal_contract_version", native_start.get("formal_contract_version", ""))
    for key in [
        "background",
        "goal",
        "scope",
        "out_of_scope",
        "user_scenarios",
        "business_rules",
        "risks",
        "assumptions",
        "permission_rules",
        "data_state_rules",
        "interface_scope",
        "exception_rules",
        "test_focus",
        "open_questions",
        "decisions",
        "source_refs",
        "design",
        "fact_bundle",
    ]:
        package.setdefault(key, structured.get(key, [] if key.endswith("s") else ""))
    package["requirement_points"] = structured.get("requirement_points", [])
    package["acceptance_points"] = structured.get("acceptance_points", [])
    package["test_cases"] = structured.get("test_cases", [])
    if native_start.get("source_draft_id"):
        package["source_draft_id"] = native_start.get("source_draft_id")
    return package


def deep_contract_checks(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []
    requirements = state.get("requirements", {}) if isinstance(state.get("requirements"), dict) else {}
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}

    for draft in drafts.values():
        if not isinstance(draft, dict) or not draft_lifecycle.is_started_draft(draft):
            continue
        draft_id = str(draft.get("draft_id") or "DRAFT").strip()
        started_requirement_id = str(draft.get("started_requirement_id") or "").strip()
        if not started_requirement_id or started_requirement_id not in requirements:
            warnings.append(f"合同检查：{draft_id} 已标记 started，但没有找到对应正式需求 {started_requirement_id or '空'}。")

    for requirement in requirements.values():
        package = formal_contract_package(requirement)
        requirement_id = str(requirement.get("requirement_id") or "REQ").strip()
        formal_issues = start_contract.start_package_contract_issues(package)
        formal_issues.extend(
            start_contract.effective_change_contract_issues(requirement.get("structured", {}).get("effective_changes", []))
        )
        if formal_issues:
            warnings.append(f"合同检查：{requirement_id} 的正式包不完整：" + "；".join(formal_issues[:8]))
        if package.get("formal_contract_version") not in {"", "formal.v2", "formal.v3"}:
            warnings.append(f"合同检查：{requirement_id} 的正式合同版本不受支持：{package.get('formal_contract_version')}。")
        source_draft_id = str(package.get("source_draft_id") or "").strip()
        if source_draft_id:
            draft = drafts.get(source_draft_id)
            if not isinstance(draft, dict):
                warnings.append(f"合同检查：{requirement_id} 引用了不存在的来源 DRAFT {source_draft_id}。")
            # 正式物化的保真由冻结的 origin facts、semantic digest、来源引用和 FactGate 校验，
            # 不能再从 Markdown 重新猜一遍语义。

    for draft in drafts.values():
        if not isinstance(draft, dict):
            continue
        assessment = draft_lifecycle.assess_draft(draft)
        if not assessment.can_start:
            continue
        action = active_draft_next_action(state)
        if action and str(action.get("draft_context", "")).startswith(str(draft.get("draft_id") or "")):
            if action.get("primary") != "$sdlc-start":
                warnings.append(f"合同检查：{draft.get('draft_id')} 处于可 start 状态，但下一步推荐不是 $sdlc-start。")

    draft_started_requirement = {
        str(draft.get("draft_id") or "").strip(): str(draft.get("started_requirement_id") or "").strip()
        for draft in drafts.values()
        if isinstance(draft, dict)
    }
    for capture in state.get("captures", []):
        if not isinstance(capture, dict):
            continue
        draft_id = str(capture.get("draft_id") or "").strip()
        requirement_id = str(capture.get("requirement_id") or "").strip()
        if draft_id and requirement_id and draft_started_requirement.get(draft_id) not in {"", requirement_id}:
            warnings.append(f"合同检查：{capture.get('capture_id')} 属于 {draft_id}，但被关联到 {requirement_id}。")
    for design in state.get("designs", []):
        if not isinstance(design, dict):
            continue
        draft_id = str(design.get("draft_id") or "").strip()
        requirement_id = str(design.get("requirement_id") or "").strip()
        if draft_id and requirement_id and draft_started_requirement.get(draft_id) not in {"", requirement_id}:
            warnings.append(f"合同检查：{design.get('design_id')} 属于 {draft_id}，但被关联到 {requirement_id}。")

    for issue in draft_ownership.resource_ownership_issues(state):
        warnings.append(f"资源归属检查：{issue}")

    if not warnings:
        passed.append("DRAFT 和正式建档合同没有发现不一致")
    return passed, warnings


def _first_json_difference(expected: Any, actual: Any, path: str = "$") -> str:
    if type(expected) is not type(actual):
        return f"{path} 类型不同"
    if isinstance(expected, dict):
        for key in expected:
            if key not in actual:
                return f"{path}.{key} 被删除"
        for key in actual:
            if key not in expected:
                return f"{path}.{key} 被增加"
        for key in expected:
            difference = _first_json_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path} 数量不同：预期 {len(expected)}，实际 {len(actual)}"
        for index, value in enumerate(expected):
            difference = _first_json_difference(value, actual[index], f"{path}[{index}]")
            if difference:
                return difference
        return ""
    if expected != actual:
        return f"{path} 内容被改写：预期 {expected!r}，实际 {actual!r}"
    return ""


def _first_markdown_difference(expected: str, actual: str) -> str:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    limit = max(len(expected_lines), len(actual_lines))
    for index in range(limit):
        expected_line = expected_lines[index] if index < len(expected_lines) else "<文件结束>"
        actual_line = actual_lines[index] if index < len(actual_lines) else "<文件结束>"
        if expected_line != actual_line:
            return f"第 {index + 1} 行：预期 {expected_line!r}，实际 {actual_line!r}"
    return ""


def current_file_integrity_checks(paths: ProjectPaths, state: dict[str, Any]) -> tuple[list[str], list[str]]:
    """按事件推导的唯一正式包核对三组 current JSON 和 Markdown。"""

    passed: list[str] = []
    warnings: list[str] = []
    for requirement in state.get("requirements", {}).values():
        requirement_dir = paths.requirements_dir / requirement["folder_name"] / "effective"
        specs = (
            (
                "requirement.current",
                requirement_json_payload(requirement, current=True),
                join_lines(structured_requirement_lines(requirement, current=True)),
            ),
            (
                "design.current",
                design_json_payload(requirement, current=True),
                join_lines(design_current_lines(requirement, current=True)),
            ),
            (
                "test-matrix.current",
                test_matrix_json_payload(requirement, current=True),
                join_lines(test_matrix_lines(requirement, current=True)),
            ),
        )
        for basename, expected_json, expected_markdown in specs:
            json_path = requirement_dir / f"{basename}.json"
            markdown_path = requirement_dir / f"{basename}.md"
            relative_json = str(relative_to_project(paths.root, json_path))
            relative_markdown = str(relative_to_project(paths.root, markdown_path))
            if not json_path.exists():
                warnings.append(f"current 完整性检查：{relative_json} 缺失。")
            else:
                try:
                    actual_json = json.loads(json_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    warnings.append(f"current 完整性检查：{relative_json} 无法解析：{exc}。")
                else:
                    difference = _first_json_difference(expected_json, actual_json)
                    if difference:
                        warnings.append(f"current 完整性检查：{relative_json} 与正式状态不一致，{difference}。")
            if not markdown_path.exists():
                warnings.append(f"current 完整性检查：{relative_markdown} 缺失。")
            else:
                actual_markdown = markdown_path.read_text(encoding="utf-8")
                difference = _first_markdown_difference(expected_markdown, actual_markdown)
                if difference:
                    warnings.append(f"current 完整性检查：{relative_markdown} 被人工改写，{difference}。")
    if not warnings:
        passed.append("三组 current JSON 和 Markdown 与正式状态一致")
    return passed, warnings

def deep_materialized_checks(paths: ProjectPaths, state: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []

    changed_files: list[str] = []
    orphan_task_files: list[str] = []
    for requirement in state["requirements"].values():
        requirement_dir = paths.requirements_dir / requirement["folder_name"]
        requirement_file = requirement_dir / "requirement.md"
        plan_file = requirement_dir / "plan.md"

        if generated_section_changed(requirement_file, requirement_markdown_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, requirement_file)))
        if generated_section_changed(plan_file, plan_markdown_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, plan_file)))
        task_map_file = requirement_dir / "task-map.md"
        if generated_section_changed(task_map_file, task_map_markdown_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, task_map_file)))
        test_plan_file = requirement_dir / "test-plan.md"
        if generated_section_changed(test_plan_file, requirement_test_plan_markdown_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, test_plan_file)))
        lessons_file = requirement_dir / "lessons.md"
        if generated_section_changed(lessons_file, requirement_lessons_markdown_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, lessons_file)))
        design_file = requirement_dir / "design.md"
        if generated_section_changed(design_file, requirement_design_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, design_file)))
        grills_index_file = requirement_dir / "grills" / "index.md"
        if generated_section_changed(grills_index_file, requirement_grills_index_lines(requirement)):
            changed_files.append(str(relative_to_project(paths.root, grills_index_file)))

        for task in requirement["tasks"]:
            task_file = requirement_dir / "tasks" / f"{task['task_id']}.md"
            if generated_section_changed(task_file, task_markdown_lines(task)):
                changed_files.append(str(relative_to_project(paths.root, task_file)))
        expected_task_filenames = {f"{task['task_id']}.md" for task in requirement["tasks"]}
        tasks_dir = requirement_dir / "tasks"
        if tasks_dir.exists():
            for task_file in sorted(tasks_dir.glob("T-*.md")):
                if task_file.name not in expected_task_filenames:
                    orphan_task_files.append(str(relative_to_project(paths.root, task_file)))

    if changed_files:
        warnings.append(
            "生成区被手动修改："
            + "、".join(changed_files)
            + "；这部分不会自动进入主状态。请把人工说明写到 `## 人工补充`，或用对应命令写入事件记录。"
        )
    if orphan_task_files:
        warnings.append(
            "发现孤儿任务文件："
            + "、".join(orphan_task_files)
            + "；这些文件已经不在当前主状态里，`$sdlc-doctor-repair` 会先备份再清理。"
        )
    legacy_passed, legacy_warnings = legacy_task_pack_check_messages(
        inspect_legacy_task_packs(paths, state)
    )
    passed.extend(item for item in legacy_passed if item not in passed)
    warnings.extend(item for item in legacy_warnings if item not in warnings)
    if not changed_files and not orphan_task_files:
        passed.append("需求包 Markdown 生成区没有发现手动改动")

    failed: list[str] = []
    contract_passed, contract_warnings = deep_contract_checks(state)
    passed.extend(contract_passed)
    failed.extend(contract_warnings)

    current_passed, current_warnings = current_file_integrity_checks(paths, state)
    passed.extend(current_passed)
    failed.extend(current_warnings)

    for requirement in state.get("requirements", {}).values():
        if not isinstance(requirement, dict):
            continue
        if requirement.get("structured", {}).get("formal_contract_version") != "formal.v3":
            continue
        requirement_root = paths.requirements_dir / str(requirement.get("folder_name") or "")
        fact_issues = fact_gate.check_saved_bundle_integrity(requirement_root)
        if fact_issues:
            failed.extend(f"{requirement.get('requirement_id')} 模型事实层：{item}" for item in fact_issues)
        else:
            passed.append(f"{requirement.get('requirement_id')} 的 original/effective/current 模型事实产物一致")

    for draft in state.get("drafts", {}).values():
        if not isinstance(draft, dict) or draft_lifecycle.is_started_draft(draft):
            continue
        assessment = draft_lifecycle.assess_draft(draft)
        if assessment.missing_requirement_items or assessment.missing_design_items or not draft.get("design_body"):
            continue
        if assessment.facts_status != "facts_passed":
            failed.append(f"{draft.get('draft_id')} 模型事实层未通过：{assessment.reason}")

    return passed, warnings, failed


def explicit_legacy_change_task_ids(change: dict[str, Any], tasks: list[dict[str, Any]]) -> list[str]:
    """历史变更只读取已保存的显式任务关系，不再扫描任务文字猜绑定。"""

    known = {str(task.get("task_id") or "") for task in tasks}
    values = [*list_value(change.get("planned_task_ids")), *list_value(change.get("changed_task_ids"))]
    values.extend(item.get("task_id") for item in list_value(change.get("coverage")) if isinstance(item, dict))
    return list(dict.fromkeys(str(item) for item in values if str(item) in known))

def build_legacy_change_coverage(
    change: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
    task_ids: list[str],
) -> list[dict[str, str]]:
    coverage: list[dict[str, str]] = []
    covered_task_ids: set[str] = set()
    for item in change.get("coverage", []):
        if not isinstance(item, dict):
            continue
        point = str(item.get("point", "")).strip()
        task_id = str(item.get("task_id", "")).strip()
        if point and task_id in tasks_by_id:
            coverage.append({"point": point, "task_id": task_id})
            covered_task_ids.add(task_id)

    for task_id in task_ids:
        if task_id in covered_task_ids:
            continue
        task = tasks_by_id[task_id]
        if len(task_ids) == 1:
            point = shorten_text(str(change.get("summary", change["change_id"])), 48)
        else:
            point = f"{change['change_id']}：{shorten_text(str(task.get('title', task_id)), 40)}"
        coverage.append({"point": point, "task_id": task_id})
        covered_task_ids.add(task_id)
    return coverage


def legacy_change_residual_reports(state: dict[str, Any]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for requirement in state["requirements"].values():
        tasks = requirement.get("tasks", [])
        tasks_by_id = {str(task["task_id"]): task for task in tasks}
        for change in requirement.get("changes", []):
            status = str(change.get("status", ""))
            if status not in {"planned", "resolved"}:
                continue

            explicit_task_ids = explicit_legacy_change_task_ids(change, tasks)
            valid_planned_task_ids = [
                str(task_id)
                for task_id in change.get("planned_task_ids", [])
                if str(task_id) in tasks_by_id
            ]
            valid_coverage = [
                item
                for item in change.get("coverage", [])
                if isinstance(item, dict)
                and str(item.get("task_id", "")) in tasks_by_id
                and str(item.get("point", "")).strip()
            ]
            unfinished_task_ids = [
                task_id
                for task_id in explicit_task_ids
                if str(tasks_by_id[task_id].get("status", "")) not in {"done", "closed"}
            ]

            missing_plan = not valid_planned_task_ids
            missing_coverage = not valid_coverage
            resolved_too_early = status == "resolved" and bool(unfinished_task_ids)
            if not (missing_plan or missing_coverage or resolved_too_early):
                continue

            if not explicit_task_ids:
                reports.append(
                    {
                        "safe": False,
                        "requirement_id": requirement["requirement_id"],
                        "change_id": change["change_id"],
                        "reason": "缺少规划任务和覆盖映射，且无法安全推断覆盖任务",
                    }
                )
                continue

            target_status = "planned" if unfinished_task_ids else status
            reports.append(
                {
                    "safe": True,
                    "requirement_id": requirement["requirement_id"],
                    "change_id": change["change_id"],
                    "reason": "缺少规划任务或覆盖映射"
                    + ("，并且存在未完成覆盖任务" if resolved_too_early else ""),
                    "task_ids": explicit_task_ids,
                    "status": target_status,
                    "coverage": build_legacy_change_coverage(change, tasks_by_id, explicit_task_ids),
                }
            )
    return reports


def legacy_change_residual_checks(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    reports = legacy_change_residual_reports(state)
    if not reports:
        return ["变更状态和覆盖映射没有发现旧残留"], []

    warnings: list[str] = []
    for report in reports:
        target = f"{report['requirement_id']} / {report['change_id']}"
        if report["safe"]:
            task_text = "、".join(report["task_ids"])
            warnings.append(
                f"旧变更状态残留：{target} {report['reason']}，可安全补齐到任务 {task_text}；可执行 `$sdlc-doctor-repair`。"
            )
        else:
            warnings.append(
                f"旧变更状态残留：{target} {report['reason']}；请先人工整理任务，再用 `$sdlc-change-plan {report['requirement_id']}` 或 `$sdlc-plan-add-task {report['requirement_id']} ...` 处理。"
            )
    return [], warnings


def repair_legacy_change_residuals(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, list[str]]:
    reports = legacy_change_residual_reports(state)
    safe_reports = [report for report in reports if report["safe"]]
    unsafe_reports = [report for report in reports if not report["safe"]]
    repaired: list[str] = []
    warnings: list[str] = []

    for requirement in state["requirements"].values():
        requirement_reports = [
            report
            for report in safe_reports
            if report["requirement_id"] == requirement["requirement_id"]
        ]
        if not requirement_reports:
            continue
        planned_changes = [
            {
                "change_id": report["change_id"],
                "task_ids": report["task_ids"],
                "coverage": report["coverage"],
                "status": report["status"],
            }
            for report in requirement_reports
        ]
        append_event(
            paths,
            event_type="plan_updated",
            source="sdlc-doctor-repair",
            summary=f"修复旧变更状态残留：{', '.join(report['change_id'] for report in requirement_reports)}",
            requirement_id=requirement["requirement_id"],
            payload={
                "tasks": requirement["tasks"],
                "priority": requirement.get("priority", "normal"),
                "blocked_reason": requirement.get("blocked_reason", ""),
                "resolved_change_ids": [],
                "planned_changes": planned_changes,
                "task_quality": requirement.get("task_quality", {}),
            },
        )
        repaired.extend(str(report["change_id"]) for report in requirement_reports)

    for report in unsafe_reports:
        warnings.append(
            f"{report['requirement_id']} / {report['change_id']} 无法安全推断覆盖任务，请先人工整理任务。"
        )
    return {"repaired": repaired, "warnings": warnings}


def cleanup_orphan_task_files(paths: ProjectPaths, requirement: dict[str, Any], tasks_dir: Path) -> None:
    expected_task_filenames = {f"{task['task_id']}.md" for task in requirement["tasks"]}
    orphan_files = [
        task_file
        for task_file in sorted(tasks_dir.glob("T-*.md"))
        if task_file.name not in expected_task_filenames
    ]
    if not orphan_files:
        return

    timestamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S%f")
    backup_dir = paths.backups_dir / f"orphaned-tasks-{timestamp}" / requirement["folder_name"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    for task_file in orphan_files:
        shutil.copy2(task_file, backup_dir / task_file.name)
        task_file.unlink(missing_ok=True)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def requirement_json_payload(requirement: dict[str, Any], *, current: bool) -> dict[str, Any]:
    structured = requirement.get("structured", {})
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    return {
        "requirement_id": requirement["requirement_id"],
        "formal_contract_version": structured.get("formal_contract_version", ""),
        "title": value(requirement["title"]),
        "description": value(requirement.get("description", "")),
        "summary": value(requirement.get("summary", "")),
        "version": structured.get("requirement_version", "requirement.v1"),
        "is_current": current,
        "migration_status": structured.get("migration_status", "structured"),
        "background": value(structured.get("background", "")),
        "goal": value(structured.get("goal", "")),
        "scope": value(structured.get("scope", [])),
        "out_of_scope": value(structured.get("out_of_scope", [])),
        "user_scenarios": value(structured.get("user_scenarios", [])),
        "business_rules": value(structured.get("business_rules", [])),
        "risks": value(structured.get("risks", [])),
        "assumptions": value(structured.get("assumptions", [])),
        "permission_rules": value(structured.get("permission_rules", [])),
        "data_state_rules": value(structured.get("data_state_rules", [])),
        "interface_scope": value(structured.get("interface_scope", [])),
        "exception_rules": value(structured.get("exception_rules", [])),
        "test_focus": value(structured.get("test_focus", [])),
        "open_questions": value(structured.get("open_questions", [])),
        "source_refs": value(structured.get("source_refs", [])),
        "functional_requirements": value(structured.get("requirement_points", [])),
        "acceptance_criteria": value(structured.get("acceptance_points", [])),
        "effective_changes": value(structured.get("effective_changes", [])) if current else [],
    }


def design_json_payload(requirement: dict[str, Any], *, current: bool) -> dict[str, Any]:
    structured = requirement.get("structured", {})
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    return {
        "requirement_id": requirement["requirement_id"],
        "formal_contract_version": structured.get("formal_contract_version", ""),
        "version": structured.get("design_version", "design.v1"),
        "requirement_version": structured.get("requirement_version", "requirement.v1"),
        "is_current": current,
        "status": requirement_design_status(requirement),
        "formal_design": value(structured.get("design", {})),
        "designs": [
            {
                "design_id": design.get("design_id", ""),
                "title": value(design.get("title", "")),
                "summary": value(design.get("summary", "")),
                "details": value(design.get("details", {})),
                "status": design.get("status", ""),
                "created_at": design.get("created_at", ""),
                "accepted_at": design.get("accepted_at", ""),
            }
            for design in requirement.get("designs", [])
        ],
    }


def test_matrix_json_payload(requirement: dict[str, Any], *, current: bool) -> dict[str, Any]:
    structured = requirement.get("structured", {})
    value = (lambda item: current_runtime_value(requirement, item)) if current else (lambda item: item)
    return {
        "requirement_id": requirement["requirement_id"],
        "formal_contract_version": structured.get("formal_contract_version", ""),
        "version": structured.get("test_matrix_version", "test-matrix.v1"),
        "requirement_version": structured.get("requirement_version", "requirement.v1"),
        "is_current": current,
        "test_cases": value(structured.get("test_cases", [])),
    }


def write_requirement_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    for requirement in state["requirements"].values():
        native_start = (
            requirement.get("native_start")
            if isinstance(requirement.get("native_start"), dict)
            else {}
        )
        if (
            requirement.get("structured", {}).get("formal_contract_version")
            in {"formal.v2", "formal.v3"}
            and native_start.get("workflow_profile") != "document-first.v1"
        ):
            # document-first 的正式内容由归档清单、引用索引和 effective 文件校验，
            # 不能再把只保存清单元数据的 formal.v3 当成旧业务字段包重复检查。
            issues = start_contract.start_package_contract_issues(formal_contract_package(requirement))
            issues.extend(
                start_contract.effective_change_contract_issues(requirement.get("structured", {}).get("effective_changes", []))
            )
            if issues:
                raise SdlcError(
                    f"{requirement['requirement_id']} 的 formal.v2 正式包合同失败，停止渲染：" + "；".join(issues[:12]),
                    exit_code=1,
                )
        requirement_dir = paths.requirements_dir / requirement["folder_name"]
        tasks_dir = requirement_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        write_markdown_with_manual_section(requirement_dir / "requirement.md", requirement_markdown_lines(requirement))
        write_markdown_with_manual_section(requirement_dir / "plan.md", plan_markdown_lines(requirement))
        write_markdown_with_manual_section(requirement_dir / "task-map.md", task_map_markdown_lines(requirement))
        write_markdown_with_manual_section(requirement_dir / "test-plan.md", requirement_test_plan_markdown_lines(requirement))
        write_markdown_with_manual_section(requirement_dir / "lessons.md", requirement_lessons_markdown_lines(requirement))
        write_markdown_with_manual_section(requirement_dir / "design.md", requirement_design_lines(requirement))
        materials_dir = requirement_dir / "materials"
        materials_dir.mkdir(parents=True, exist_ok=True)
        write_markdown_with_manual_section(materials_dir / "index.md", requirement_materials_index_lines(requirement))
        for material in requirement.get("materials", []):
            (materials_dir / f"{material['material_id']}.md").write_text(
                join_lines(material_markdown_lines(material)),
                encoding="utf-8",
            )
        grills_dir = requirement_dir / "grills"
        grills_dir.mkdir(parents=True, exist_ok=True)
        write_markdown_with_manual_section(grills_dir / "index.md", requirement_grills_index_lines(requirement))
        for grill in requirement.get("grills", []):
            (grills_dir / f"{grill['grill_id']}.md").write_text(
                join_lines(grill_markdown_lines(grill)),
                encoding="utf-8",
            )
        structured = requirement.get("structured", {})
        requirement_version = structured.get("requirement_version", "requirement.v1")
        design_version = structured.get("design_version", "design.v1")
        test_matrix_version = structured.get("test_matrix_version", "test-matrix.v1")
        original_dir = requirement_dir / "original"
        effective_dir = requirement_dir / "effective"
        versions_dir = requirement_dir / "versions"
        original_dir.mkdir(parents=True, exist_ok=True)
        effective_dir.mkdir(parents=True, exist_ok=True)
        versions_dir.mkdir(parents=True, exist_ok=True)
        def write_original_text(name: str, content: str) -> None:
            path = original_dir / name
            if not path.exists():
                path.write_text(content, encoding="utf-8")

        def write_original_json(name: str, content: dict[str, Any]) -> None:
            path = original_dir / name
            if not path.exists():
                write_json(path, content)

        # document-first 的 original 只允许包含正式清单登记的只写一次文件；
        # 兼容 Markdown/JSON 快照只能放 effective 和 versions，不能污染原档案。
        if native_start.get("workflow_profile") != "document-first.v1":
            write_original_text("requirement.v1.md", join_lines(original_requirement_lines(requirement)))
            write_original_text("design.v1.md", join_lines(design_current_lines(requirement, current=False)))
            write_original_text("test-matrix.v1.md", join_lines(test_matrix_lines(requirement, current=False)))
            write_original_text("requirement.original.md", join_lines(original_requirement_lines(requirement)))
            write_original_text("design.original.md", join_lines(design_current_lines(requirement, current=False)))
            write_original_text("test-matrix.original.md", join_lines(test_matrix_lines(requirement, current=False)))
            write_original_json("requirement.v1.json", requirement_json_payload(requirement, current=False))
            write_original_json("design.v1.json", design_json_payload(requirement, current=False))
            write_original_json("test-matrix.v1.json", test_matrix_json_payload(requirement, current=False))
            write_original_json("requirement.original.json", requirement_json_payload(requirement, current=False))
            write_original_json("design.original.json", design_json_payload(requirement, current=False))
            write_original_json("test-matrix.original.json", test_matrix_json_payload(requirement, current=False))
        if native_start.get("workflow_profile") != "document-first.v1":
            (effective_dir / "requirement.current.md").write_text(join_lines(structured_requirement_lines(requirement, current=True)), encoding="utf-8")
            (effective_dir / "design.current.md").write_text(join_lines(design_current_lines(requirement, current=True)), encoding="utf-8")
            (effective_dir / "test-matrix.current.md").write_text(join_lines(test_matrix_lines(requirement, current=True)), encoding="utf-8")
            write_json(effective_dir / "requirement.current.json", requirement_json_payload(requirement, current=True))
            write_json(effective_dir / "design.current.json", design_json_payload(requirement, current=True))
            write_json(effective_dir / "test-matrix.current.json", test_matrix_json_payload(requirement, current=True))
            version_outputs = {
                f"{requirement_version}.md": join_lines(structured_requirement_lines(requirement, current=False)),
                f"{design_version}.md": join_lines(design_current_lines(requirement, current=False)),
                f"{test_matrix_version}.md": join_lines(test_matrix_lines(requirement, current=False)),
            }
            for name, content in version_outputs.items():
                path = versions_dir / name
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
            version_json_outputs = {
                f"{requirement_version}.json": requirement_json_payload(requirement, current=False),
                f"{design_version}.json": design_json_payload(requirement, current=False),
                f"{test_matrix_version}.json": test_matrix_json_payload(requirement, current=False),
            }
            for name, content in version_json_outputs.items():
                path = versions_dir / name
                if not path.exists():
                    write_json(path, content)
        else:
            # document-first 的 effective、versions 和 original 由 start 事务按引用
            # 哈希一次性写入。任务状态刷新只更新任务投影，不能用旧渲染器覆盖正式文件。
            pass
        (requirement_dir / "traceability.md").write_text(join_lines(traceability_lines(requirement)), encoding="utf-8")
        if requirement_test_script_items(requirement):
            tests_dir = requirement_dir / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            write_markdown_with_manual_section(tests_dir / "README.md", requirement_tests_readme_lines(requirement))

        if requirement.get("task_plan_contract"):
            # task.v2 的 JSON 和 Markdown 已由同一事务完整提交。旧渲染器会丢失
            # task.v2 专属字段，所以后续全量刷新只能保留这些正式文件。
            from codex_sdlc.core.task_contract import load_task_plan_record

            load_task_plan_record(paths, str(requirement["requirement_id"]))
        else:
            for task in requirement["tasks"]:
                write_markdown_with_manual_section(
                    tasks_dir / f"{task['task_id']}.md",
                    task_markdown_lines(task),
                )
            cleanup_orphan_task_files(paths, requirement, tasks_dir)

        related_sessions = [
            session for session in state["sessions"] if requirement["requirement_id"] in session["related_requirements"]
        ]
        session_lines = [f"# {requirement['requirement_id']} 会话摘要", ""]
        if related_sessions:
            for session in related_sessions:
                session_lines.extend(
                    [
                        f"## {session['session_id']}",
                        f"- 时间：{session['created_at']}",
                        f"- 摘要：{session['summary']}",
                        f"- 下一步：{session['next_step']}",
                        "",
                    ]
                )
        else:
            session_lines.append("- 暂无会话记录")
        (requirement_dir / "sessions.md").write_text(join_lines(session_lines), encoding="utf-8")

        changes_dir = requirement_dir / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        change_lines = [f"# {requirement['requirement_id']} 变更记录", ""]
        requirement_changes = requirement["changes"]
        if requirement_changes:
            for change in requirement_changes:
                impacted = "、".join(change["changed_task_ids"]) if change["changed_task_ids"] else "无"
                changed_task_lines = [f"- {item}" for item in change["changed_task_ids"]] or ["- 无"]
                added_task_lines = [f"- {item['task_id']} {item['title']}" for item in change["added_tasks"]] or ["- 暂无"]
                closed_task_lines = [f"- {item}" for item in change["closed_task_ids"]] or ["- 暂无"]
                acceptance_lines = [f"- {item}" for item in change.get("acceptance_points", [])] or ["- 暂无"]
                planned_task_lines = [f"- {item}" for item in change.get("planned_task_ids", [])] or ["- 暂无"]
                planning_status = str(change.get("planning_status", "")).strip()
                planning_status_text = "待模型规划" if planning_status == "needs_model_plan" else (planning_status or "无")
                coverage_lines = [
                    f"- {item.get('point', '')} -> {item.get('task_id', '')}"
                    for item in change.get("coverage", [])
                    if item.get("point") and item.get("task_id")
                ] or ["- 暂无覆盖映射"]
                capture_lines = [f"- {item}" for item in change.get("capture_ids", [])] or ["- 暂无"]
                change_lines.extend(
                    [
                        f"## {change['change_id']}",
                        f"- 状态：{change['status']}",
                        f"- 规划状态：{planning_status_text}",
                        f"- 时间：{change['created_at']}",
                        f"- 影响任务：{impacted}",
                        f"- 说明：{change['summary']}",
                        "",
                    ]
                )
                single_change_lines = [
                    f"# {change['change_id']}",
                    "",
                    f"- 状态：{change['status']}",
                    f"- 规划状态：{planning_status_text}",
                    f"- 时间：{change['created_at']}",
                    f"- 确认结果：{change.get('confirmation', '待确认')}",
                    "",
                    "## 变更原因",
                    *format_markdown_content_lines(change.get("reason") or "本次直接按变更内容记录。"),
                    "",
                    "## 变更内容",
                    *format_markdown_content_lines(change["description"]),
                    "",
                    "## 验收和回归要求",
                    *acceptance_lines,
                    "",
                    "## 关联 capture",
                    *capture_lines,
                    "",
                    "## 影响任务",
                    *changed_task_lines,
                    "",
                    "## 新增任务建议",
                    *added_task_lines,
                    "",
                    "## 关闭任务建议",
                    *closed_task_lines,
                    "",
                    "## 规划任务",
                    *planned_task_lines,
                    "",
                    "## 覆盖映射",
                    *coverage_lines,
                ]
                (changes_dir / f"{change['change_id']}.md").write_text(join_lines(single_change_lines), encoding="utf-8")
        else:
            change_lines.append("- 暂无变更记录")
        (requirement_dir / "changes.md").write_text(join_lines(change_lines), encoding="utf-8")
        (requirement_dir / "change-map.md").write_text(join_lines(requirement_change_map_lines(requirement)), encoding="utf-8")

        decisions_lines = [f"# {requirement['requirement_id']} 决策记录", ""]
        linked_captures = [
            capture
            for capture in requirement["captures"]
            if capture["status"] != "pending" and capture.get("target_type") != "lesson"
        ]
        if linked_captures:
            for capture in linked_captures:
                decisions_lines.extend(
                    [
                        f"## {capture['capture_id']}",
                        f"- 状态：{capture['status']}",
                        f"- 时间：{capture['created_at']}",
                        "",
                        "### 结论",
                        *format_markdown_content_lines(capture["summary"]),
                        "",
                    ]
                )
        else:
            decisions_lines.append("- 暂无已纳入需求的 capture 决策")
        (requirement_dir / "decisions.md").write_text(join_lines(decisions_lines), encoding="utf-8")


def write_capture_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    for capture in state["captures"]:
        file_path = paths.root / capture["file_path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        changed_file_lines = [f"- {item}" for item in capture["changed_files"]] or ["- 暂无"]
        command_lines = [f"- {item}" for item in capture["commands"]] or ["- 暂无"]
        question_lines = [f"- {item}" for item in capture["questions"]] or ["- 暂无"]
        lines = [
            f"# {capture['capture_id']}",
            "",
            f"- 状态：{capture['status']}",
            f"- 去向：{capture.get('target_type', 'capture')}",
            f"- 时间：{capture['created_at']}",
            "",
            "## 结论",
            *format_markdown_content_lines(capture["summary"]),
            "",
            "## 补充说明",
            *format_markdown_content_lines(capture.get("note") or "无"),
            "",
            "## 涉及文件",
            *changed_file_lines,
            "",
            "## 涉及命令",
            *command_lines,
            "",
            "## 待确认问题",
            *question_lines,
        ]
        file_path.write_text(join_lines(lines), encoding="utf-8")


def write_grill_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    paths.grills_dir.mkdir(parents=True, exist_ok=True)
    write_markdown_with_manual_section(paths.grills_dir / "index.md", global_grills_index_lines(state))
    for grill in state["grills"]:
        file_path = paths.root / grill["file_path"]
        # 已关联需求的质询记录会在需求包里生成；全局记录单独落在 .codex-sdlc/grills/。
        if grill.get("requirement_id"):
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(join_lines(grill_markdown_lines(grill)), encoding="utf-8")


def write_design_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    for design in state["designs"]:
        global_file = paths.designs_dir / f"{design['design_id']}.md"
        if design.get("requirement_id"):
            if global_file.exists():
                global_file.unlink()
            continue
        file_path = paths.root / design["file_path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {design['design_id']} {design['title']}",
            "",
            f"- 状态：{design['status']}",
            "- 需求：未关联",
            f"- 创建时间：{design['created_at']}",
            f"- 更新时间：{design['updated_at']}",
            f"- 确认时间：{design.get('accepted_at') or '未确认'}",
            "",
            "## 技术方案",
        ]
        lines.extend(format_design_summary_lines(design["summary"]))
        file_path.write_text(join_lines(lines), encoding="utf-8")


def draft_body_text(draft: dict[str, Any], field: str, fallback_title: str) -> str:
    body = str(draft.get(field) or "").strip()
    if body:
        return body + "\n"
    return join_lines([f"# {fallback_title}", "", "暂无内容"])


def draft_design_body_text(draft: dict[str, Any]) -> str:
    """历史正文保持原字节语义，新流程只渲染结构化设计对象。"""

    legacy_body = str(draft.get("design_body") or "").strip()
    if legacy_body:
        # 已经存在的 design.draft.md 是历史档案，缺少旧模板章节也不能被刷新改写。
        return legacy_body + "\n"
    if draft_lifecycle.uses_structured_requirement_stage(draft):
        from codex_sdlc.services.design_service import modular_design_markdown

        return modular_design_markdown(draft)
    return draft_body_text(
        draft,
        "design_body",
        f"{draft['draft_id']} 技术草稿",
    )


def draft_review_bucket(item: Any) -> str:
    """审查分组只读取结构化 severity/status，不解析审查文字。"""

    if not isinstance(item, dict):
        return "审查记录"
    severity = str(item.get("severity") or "").strip().lower()
    status = str(item.get("status") or "").strip().lower()
    if severity in {"p0", "blocker"} or status in {"blocked", "rejected"}:
        return "阻塞问题"
    if severity in {"p1", "p2", "warning"} or status == "warning":
        return "提醒问题"
    if status in {"passed", "accepted"}:
        return "已通过项"
    return "审查记录"


def draft_review_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("description") or item.get("id") or "").strip()
    return str(item).strip()


def draft_review_markdown(draft: dict[str, Any], title: str, field: str, empty_text: str) -> str:
    items = [item for item in draft.get(field, []) if draft_review_text(item)]
    lines = [
        f"# {draft['draft_id']} {title}",
        "",
        f"- 草稿标题：{draft.get('title', '')}",
        f"- 状态：{draft.get('status', '')}",
        f"- 更新时间：{draft.get('updated_at', '')}",
    ]
    if not items:
        lines.extend(["", "## 内容", f"- {empty_text}"])
        return join_lines(lines)
    buckets = {"阻塞问题": [], "提醒问题": [], "已通过项": [], "审查记录": []}
    for item in items:
        buckets[draft_review_bucket(item)].append(item)
    for heading in ("阻塞问题", "提醒问题", "已通过项", "审查记录"):
        lines.extend(["", f"## {heading}"])
        lines.extend([f"- {draft_review_text(item)}" for item in buckets[heading]] or ["- 无"])
    return join_lines(lines)


def draft_list_markdown(draft: dict[str, Any], title: str, field: str, empty_text: str = "") -> str:
    if field == "review_items":
        return draft_review_markdown(draft, title, field, empty_text)
    items: list[str] = []
    if field == "questions" and draft_lifecycle.uses_structured_requirement_stage(draft):
        assessment = draft_lifecycle.assess_draft(draft)
        items = [
            f"{item.question_id} [{item.status}]"
            + (f" {item.text}" if item.text else "")
            for item in assessment.structured_questions
        ]
        represented = {item.source_id for item in assessment.structured_questions}
        items.extend(
            f"{item.source_id} [{item.status}] {item.code}"
            for item in assessment.blockers
            if item.code in {"material_missing", "material_unstable"}
            and item.source_id not in represented
        )
    elif field == "decisions" and draft_lifecycle.uses_structured_requirement_stage(draft):
        items = [
            f"{item.get('decision_id', '')} [{item.get('status', '')}] {item.get('selection', '')}".strip()
            for item in draft.get("decision_records", [])
            if isinstance(item, dict) and str(item.get("decision_id") or "").strip()
        ]
    else:
        items = [str(item).strip() for item in draft.get(field, []) if str(item).strip()]
    lines = [
        f"# {draft['draft_id']} {title}",
        "",
        f"- 草稿标题：{draft.get('title', '')}",
        "",
        "## 内容",
    ]
    lines.extend([f"- {item}" for item in items])
    if not items and empty_text:
        # empty_text 只用于给人看的文档；事实来源文件不传它，避免显示文案被重新解释成业务输入。
        lines.append(f"- {empty_text}")
    return join_lines(lines)


def draft_material_manifest_document(draft: dict[str, Any]) -> dict[str, Any]:
    """资料事件是唯一真相，根目录清单只保存可重复生成的结构化投影。"""

    document = {
        "schema_version": "material-manifest.v1",
        "draft_id": str(draft.get("draft_id") or ""),
        "materials": [
            {
                key: deepcopy(value)
                for key, value in item.items()
                if not str(key).startswith("_")
            }
            for item in draft.get("materials", [])
            if isinstance(item, dict)
        ],
    }
    validate_schema_document(document, schema_name="material-manifest.v1")
    return document


def validate_draft_material_files(paths: ProjectPaths, draft: dict[str, Any]) -> None:
    """每次刷新都重验原始字节，手改或漂移不能被新投影掩盖。"""

    draft_dir = paths.draft_dir(str(draft.get("draft_id") or ""))
    original_dir = paths.draft_original_materials_dir(str(draft.get("draft_id") or ""))
    try:
        draft_root = draft_dir.resolve(strict=True)
        original_root = original_dir.resolve(strict=True)
        original_root.relative_to(draft_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("DRAFT 原始资料目录不存在或路径不安全。", exit_code=1) from exc
    if original_dir.is_symlink():
        raise SdlcError("DRAFT 原始资料目录不能是符号链接。", exit_code=1)

    for material in draft.get("materials", []):
        if not isinstance(material, dict) or material.get("source_kind") != "file":
            continue
        relative = Path(str(material.get("stored_path") or ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "原始资料":
            raise SdlcError(f"资料 {material.get('material_id', '')} 的归档路径无效。", exit_code=1)
        target = draft_dir / relative
        try:
            target.resolve(strict=True).relative_to(original_root)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise SdlcError(f"资料 {material.get('material_id', '')} 的归档文件不存在或越过 DRAFT。", exit_code=1) from exc
        if target.is_symlink() or not target.is_file():
            raise SdlcError(f"资料 {material.get('material_id', '')} 的归档目标不是普通文件。", exit_code=1)
        if sha256_file(target) != str(material.get("sha256") or ""):
            raise SdlcError(f"资料 {material.get('material_id', '')} 的归档文件哈希已经变化。", exit_code=1)


def validate_draft_material_artifact(draft: dict[str, Any], manifest: dict[str, Any]) -> None:
    records = [
        item
        for item in draft.get("artifact_records", [])
        if isinstance(item, dict) and item.get("source_path") == "需求/material-manifest.v1.json"
    ]
    if len(records) != 1 or records[0].get("document") != manifest:
        raise SdlcError("资料清单事件与派生产物登记不一致，不能刷新 DRAFT。", exit_code=1)


def write_draft_material_manifest(path: Path, document: dict[str, Any]) -> None:
    """根目录清单先完整写入临时文件再替换，读取方不会拿到半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json_text(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _append_requirement_text(lines: list[str], heading: str, value: Any) -> None:
    lines.extend(["", f"#### {heading}", str(value or "")])


def _append_requirement_list(lines: list[str], heading: str, values: Any) -> None:
    lines.extend(["", f"#### {heading}"])
    items = values if isinstance(values, list) else []
    lines.extend([f"- {item}" for item in items] or ["- 无"])


def _append_requirement_json_list(lines: list[str], heading: str, values: Any) -> None:
    lines.extend(["", f"#### {heading}"])
    items = values if isinstance(values, list) else []
    lines.extend(
        [
            f"- {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            for item in items
        ]
        or ["- 无"]
    )


def draft_requirement_split_markdown(draft: dict[str, Any]) -> str:
    """从结构化拆分投影完整渲染，不限制正文长度，也不回读 Markdown。"""

    split = draft.get("requirement_split")
    receipt = draft.get("requirement_import")
    if not isinstance(split, dict) or not isinstance(receipt, dict):
        return ""
    mapping = receipt.get("mapping") if isinstance(receipt.get("mapping"), dict) else {}
    lines = [
        f"# {split.get('draft_id', '')} 需求拆分",
        "",
        f"- 标题：{split.get('title', '')}",
        f"- 生产运行标识：{split.get('producer_run_id', '')}",
        f"- 导入包：{receipt.get('package_key', '')}",
    ]
    _append_requirement_text(lines, "项目背景", split.get("background"))
    # 这里仅把合同正文原样写入展示文件，不把正文交给任何判断函数。
    lines.extend(["", "#### 交付目标", str(split.get("goal") or "")])
    _append_requirement_list(lines, "本轮范围", split.get("scope"))
    _append_requirement_list(lines, "不包含内容", split.get("out_of_scope"))
    _append_requirement_list(lines, "用户场景", split.get("user_scenarios"))
    lines.extend(["", "## 输入资料哈希"])
    input_hashes = split.get("input_material_hashes")
    if isinstance(input_hashes, dict):
        lines.extend(
            f"- {material_id}：{digest}"
            for material_id, digest in sorted(input_hashes.items())
        )
    if not isinstance(input_hashes, dict) or not input_hashes:
        lines.append("- 无")

    lines.extend(["", "## 全局规则"])
    global_rules = split.get("global_rules") if isinstance(split.get("global_rules"), list) else []
    if not global_rules:
        lines.append("- 无")
    for rule in global_rules:
        if not isinstance(rule, dict):
            continue
        formal_id = mapping.get(str(rule.get("client_key") or ""), "")
        lines.extend(["", f"### {formal_id} {rule.get('title', '')}"])
        # 描述字段只参与展示，业务状态始终来自结构化枚举和关系字段。
        lines.extend(["", "#### 规则正文", str(rule.get("description") or "")])
        _append_requirement_text(lines, "规则类型", rule.get("type"))
        _append_requirement_list(lines, "适用需求", rule.get("applies_to"))
        _append_requirement_json_list(lines, "原文来源", rule.get("source_refs"))
        _append_requirement_json_list(lines, "替代、拆分和合并关系", rule.get("relations"))

    lines.extend(["", "## 功能需求"])
    requirements = split.get("functional_requirements")
    requirements = requirements if isinstance(requirements, list) else []
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        formal_id = mapping.get(str(requirement.get("client_key") or ""), "")
        lines.extend(["", f"### {formal_id} {requirement.get('title', '')}"])
        # 长正文直接进入 Markdown 投影，不截断，也不从文字中反推状态。
        lines.extend(["", "#### 需求描述", str(requirement.get("description") or "")])
        for field, heading in (
            ("elements", "输入、输出、页面和数据元素"),
            ("flow", "用户操作和系统处理"),
            ("facts", "已确认事实"),
            ("rules", "功能规则"),
            ("constraints", "约束和边界"),
            ("states_and_exceptions", "状态和异常处理"),
            ("global_rule_refs", "全局规则引用"),
            ("material_refs", "关联资料"),
            ("depends_on", "依赖需求"),
            ("out_of_scope", "不包含内容"),
        ):
            _append_requirement_list(lines, heading, requirement.get(field))
        _append_requirement_json_list(lines, "原文来源", requirement.get("source_refs"))
        _append_requirement_json_list(
            lines, "替代、拆分和合并关系", requirement.get("relations")
        )
        lines.extend(["", "#### 验收标准"])
        criteria = requirement.get("acceptance_criteria")
        criteria = criteria if isinstance(criteria, list) else []
        for criterion in criteria:
            if not isinstance(criterion, dict):
                continue
            criterion_id = mapping.get(str(criterion.get("client_key") or ""), "")
            lines.extend(["", f"##### {criterion_id}"])
            _append_requirement_text(lines, "所属需求", criterion.get("owner_fr_ref"))
            _append_requirement_text(lines, "操作", criterion.get("operation"))
            _append_requirement_text(lines, "预期结果", criterion.get("expected"))
            _append_requirement_text(lines, "通过标准", criterion.get("pass_standard"))
            _append_requirement_json_list(lines, "原文来源", criterion.get("source_refs"))
            _append_requirement_json_list(
                lines, "替代、拆分和合并关系", criterion.get("relations")
            )
    _append_requirement_list(lines, "待确认问题", split.get("open_questions"))
    return join_lines(lines)


def draft_requirement_coverage_markdown(draft: dict[str, Any]) -> str:
    """覆盖矩阵只展示结构化状态和引用，不根据 reason 或正文改变归类。"""

    coverage = draft.get("requirement_coverage")
    receipt = draft.get("requirement_import")
    if not isinstance(coverage, dict) or not isinstance(receipt, dict):
        return ""
    mapping = receipt.get("mapping") if isinstance(receipt.get("mapping"), dict) else {}
    lines = [
        f"# {coverage.get('draft_id', '')} 需求覆盖矩阵",
        "",
        f"- 需求拆分哈希：{coverage.get('requirement_split_sha256', '')}",
        f"- 导入包：{receipt.get('package_key', '')}",
        "",
        "## 内容单元",
    ]
    units = coverage.get("units") if isinstance(coverage.get("units"), list) else []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        formal_id = mapping.get(str(unit.get("client_key") or ""), "")
        lines.extend(
            [
                "",
                f"### {formal_id}",
                "",
                f"- 分类：{unit.get('classification', '')}",
                f"- 覆盖状态：{unit.get('status', '')}",
                f"- 覆盖对象：{', '.join(str(item) for item in unit.get('covered_by', [])) or '无'}",
                f"- 用户决定：{', '.join(str(item) for item in unit.get('decision_refs', [])) or '无'}",
                f"- 原因：{unit.get('reason', '')}",
                "",
                "#### 来源定位",
                json.dumps(
                    unit.get("source_ref"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
        _append_requirement_json_list(
            lines, "替代、拆分和合并关系", unit.get("relations")
        )
    return join_lines(lines)


def write_draft_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    paths.drafts_dir.mkdir(parents=True, exist_ok=True)
    for draft in state.get("drafts", {}).values():
        layout = draft_artifacts.ensure_draft_layout(paths, draft["draft_id"])
        draft_dir = layout.draft_dir
        material_manifest = None
        if draft.get("_material_manifest_enabled"):
            material_manifest = draft_material_manifest_document(draft)
        # status.json 给命令和后续自动化读取，Markdown 给人直接复核，不把两类用途混在一个文件里。
        status_payload = {
            key: value
            for key, value in draft.items()
            if not str(key).startswith("_")
        }
        documents: dict[str, bytes] = {
            "status.json": (json.dumps(status_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            # 兼容文件继续从事件重建，现有 start、facts 和历史体检可以原样读取。
            "requirement.draft.md": draft_body_text(
                draft, "requirement_body", f"{draft['draft_id']} 需求草稿"
            ).encode("utf-8"),
            "design.draft.md": draft_design_body_text(draft).encode("utf-8"),
            "review.md": draft_list_markdown(
                draft, "审查记录", "review_items", "暂无审查记录"
            ).encode("utf-8"),
            "questions.md": draft_list_markdown(
                draft, "待确认问题", "questions"
            ).encode("utf-8"),
            "decisions.md": draft_list_markdown(
                draft, "已确认决定", "decisions"
            ).encode("utf-8"),
        }
        requirement_split = draft.get("requirement_split")
        requirement_coverage = draft.get("requirement_coverage")
        requirement_import = draft.get("requirement_import")
        if (
            isinstance(requirement_split, dict)
            and isinstance(requirement_coverage, dict)
            and isinstance(requirement_import, dict)
        ):
            # 固定文件全部由原子导入事件重建；源包保留在版本化子目录，阅读投影损坏时无需解析 Markdown。
            documents.update(
                {
                    "需求/requirement-split.v1.json": canonical_json_text(
                        requirement_split
                    ).encode("utf-8"),
                    "需求/requirement-coverage.v1.json": canonical_json_text(
                        requirement_coverage
                    ).encode("utf-8"),
                    "需求/需求拆分.md": draft_requirement_split_markdown(draft).encode(
                        "utf-8"
                    ),
                    "需求/需求覆盖矩阵.md": draft_requirement_coverage_markdown(
                        draft
                    ).encode("utf-8"),
                    "需求/需求导入回执.json": canonical_json_text(
                        requirement_import
                    ).encode("utf-8"),
                }
            )
        confirmations = [
            item
            for item in draft.get("requirement_confirmations", [])
            if isinstance(item, dict)
        ]
        if confirmations:
            latest_confirmation = confirmations[-1]
            documents["需求/requirement-confirmation.v1.json"] = (
                canonical_json_text(latest_confirmation).encode("utf-8")
            )
            for confirmation in confirmations:
                confirmation_id = str(confirmation.get("confirmation_id") or "")
                if confirmation_id:
                    documents[f"需求/确认记录/{confirmation_id}.json"] = (
                        canonical_json_text(confirmation).encode("utf-8")
                    )
        if draft.get("_design_reference_enabled"):
            from codex_sdlc.services.design_service import (
                design_reference_index_document,
                design_reference_markdown,
                validate_design_reference_record,
                validate_design_reference_source,
            )

            design_index = design_reference_index_document(draft)
            # 写任何投影前先重验 MAT 路径、完整哈希、锚点和片段哈希；历史已确认记录
            # 可以继续引用被新 MAT 替代的原文件，但原字节必须仍然存在且完全一致。
            for reference in design_index["design_references"]:
                validate_design_reference_source(
                    paths,
                    draft,
                    reference,
                    require_current_confirmation=False,
                    require_active_material=False,
                    require_current_requirements=False,
                )
            documents["设计/des-index.v1.json"] = canonical_json_text(
                design_index
            ).encode("utf-8")
            documents["设计/技术方案引用.md"] = design_reference_markdown(
                draft
            ).encode("utf-8")
            for reference in design_index["design_references"]:
                record = validate_design_reference_record(reference)
                documents[f"设计/引用记录/{record['design_id']}.json"] = (
                    canonical_json_text(record).encode("utf-8")
                )
        if draft.get("_design_plan_enabled"):
            from codex_sdlc.core.design_plan_contract import (
                design_plan_markdown,
            )

            plan = draft.get("_design_plan_record")
            if not isinstance(plan, Mapping):
                raise SdlcError(
                    f"{draft['draft_id']} 缺少唯一有效的开发设计总计划。"
                )
            # 计划的三份投影都从事件中的同一份记录生成，不能依赖磁盘上碰巧残留的文件。
            documents.update(
                {
                    "设计/design-plan.v1.json": canonical_json_text(plan).encode(
                        "utf-8"
                    ),
                    "设计/code-evidence.v1.json": canonical_json_text(
                        plan["code_evidence"]
                    ).encode("utf-8"),
                    "设计/开发设计总计划.md": design_plan_markdown(plan).encode(
                        "utf-8"
                    ),
                }
            )
        if draft.get("_design_artifact_enabled"):
            from codex_sdlc.core.design_artifact_contract import (
                design_artifact_projection_documents,
            )

            # 模块 JSON 和 Markdown 都从同一事件记录生成；任何输入漂移或跨模块引用错误
            # 都会在写文件前整包拒绝，因此不会出现只更新一半的模块投影。
            _records, artifact_documents = design_artifact_projection_documents(
                paths,
                draft,
                events=state.get("events", []),
            )
            documents.update(artifact_documents)
        if draft.get("_design_summary_enabled"):
            from codex_sdlc.core.design_summary_contract import (
                design_summary_projection_documents,
            )

            # 总体说明 JSON 和 Markdown 来自同一条版本化事件，删除或改写后都按事件重建。
            _summary, summary_documents = design_summary_projection_documents(
                paths,
                draft,
                events=state.get("events", []),
            )
            documents.update(summary_documents)
        registered_documents = draft_artifacts.build_registered_projection_files(
            draft
        )
        documents.update(registered_documents)
        from codex_sdlc.core.artifact_index import (
            ARTIFACT_INDEX_PATH,
            artifact_index_bytes,
            build_artifact_index_document,
        )

        # 同名 schema 不能在早期阶段写旧结构、到总体说明阶段才换成完整结构；
        # 因此每次受管产物刷新都用当前合同覆盖基础登记投影，逐阶段保持同一语义。
        enhanced_index = build_artifact_index_document(
            paths,
            draft,
            events=state.get("events", []),
            documents=documents,
        )
        documents[ARTIFACT_INDEX_PATH] = artifact_index_bytes(
            enhanced_index
        )
        # 模型事实文件来自事件状态，刷新可以完整重建；旧产物继续保留用于说明 stale 原因。
        fact_files = {
            "source-projection.json": draft.get("fact_source_projection"),
            "source-index.json": draft.get("fact_source_index"),
            "requirement.facts.json": draft.get("requirement_facts"),
            "design.facts.json": draft.get("design_facts"),
            "model-review.json": draft.get("model_review"),
        }
        for file_name, document in fact_files.items():
            if isinstance(document, dict):
                documents[file_name] = canonical_json_text(document).encode("utf-8")
        # 同一 DRAFT 的受管文件先完整暂存再替换；原始资料目录不进入这个写入集合。
        draft_artifacts.write_projection_bundle(draft_dir, documents)
        if draft.get("_material_manifest_enabled"):
            # 事件证据或归档字节漂移时，先写同一 assessment 的诊断投影，再拒绝覆盖资料清单。
            validate_draft_material_artifact(draft, material_manifest)
            validate_draft_material_files(paths, draft)
            write_draft_material_manifest(
                draft_dir / "material-manifest.v1.json",
                material_manifest,
            )


def write_session_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    for session in state["sessions"]:
        file_path = paths.sdlc_dir / session["file_path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        related_requirements = [f"- {item}" for item in session["related_requirements"]] or ["- 无"]
        related_tasks = [f"- {item}" for item in session["related_tasks"]] or ["- 无"]
        changed_files = [f"- {item}" for item in session["changed_files"]] or ["- 无"]
        commands = [f"- {item}" for item in session["commands"]] or ["- 无"]
        verifications = [f"- {item}" for item in session["verifications"]] or ["- 无"]
        unresolved_issues = [f"- {item}" for item in session["unresolved_issues"]] or ["- 无"]
        lines = [
            f"# {session['session_id']}",
            "",
            f"- 时间：{session['created_at']}",
            f"- 摘要：{session['summary']}",
            f"- 下一步：{session['next_step']}",
            f"- 建议提交说明：{session['suggested_commit']}",
            "",
            "## 关联需求",
        ]
        lines.extend(related_requirements)
        lines.extend(["", "## 关联任务"])
        lines.extend(related_tasks)
        lines.extend(["", "## 涉及文件"])
        lines.extend(changed_files)
        lines.extend(["", "## 执行命令"])
        lines.extend(commands)
        lines.extend(["", "## 验证结果"])
        lines.extend(verifications)
        lines.extend(["", "## 遗留问题"])
        lines.extend(unresolved_issues)
        file_path.write_text(join_lines(lines), encoding="utf-8")


def write_verification_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    for verification in state["verifications"]:
        file_path = paths.root / verification["file_path"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# {verification['verification_id']}",
            "",
            f"- 需求：{verification['requirement_id']}",
            f"- 任务：{verification['task_id']}",
            f"- 时间：{verification['created_at']}",
            "",
            "## 结果",
            *format_markdown_content_lines(verification["summary"]),
        ]
        file_path.write_text(join_lines(lines), encoding="utf-8")


def refresh_materialized_state(paths: ProjectPaths) -> dict[str, Any]:
    # 每次都按同一条链路重建，保证 CLI 输出和磁盘快照始终来自同一份事件记录。
    state = derive_state(paths)
    rebuild_database(paths, state)
    write_project_snapshot(paths, state)
    write_current_snapshot(paths, state)
    write_requirement_files(paths, state)
    write_capture_files(paths, state)
    write_grill_files(paths, state)
    write_design_files(paths, state)
    write_draft_files(paths, state)
    write_session_files(paths, state)
    write_verification_files(paths, state)
    cleanup_transient_files(paths)
    return state


def refresh_task_runtime_state(paths: ProjectPaths) -> dict[str, Any]:
    """刷新任务运行状态，不重新解释或改写已经归档的正式需求原文。"""

    # task-done 和基于关闭轮次的 regression 只追加任务状态与验证事件。
    # 正式 task.v2、引用索引和 original 目录都是上游合同，若再次走旧需求渲染器，
    # 会把运行期写入错误扩大成正式档案改写，也会让命令在已经提交完成事务后才报错。
    state = derive_state(paths)
    rebuild_database(paths, state)
    write_project_snapshot(paths, state)
    write_current_snapshot(paths, state)
    write_verification_files(paths, state)
    cleanup_transient_files(paths)
    return state


def _write_runtime_change_files(paths: ProjectPaths, state: dict[str, Any]) -> None:
    """只投影运行期新增的正式变更，不触碰需求原文、任务合同和引用索引。"""

    for requirement in state["requirements"].values():
        requirement_dir = paths.requirements_dir / requirement["folder_name"]
        changes_dir = requirement_dir / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        summary_lines = [f"# {requirement['requirement_id']} 变更记录", ""]
        for change in requirement["changes"]:
            impacted = "、".join(change["changed_task_ids"]) or "无"
            summary_lines.extend(
                [
                    f"## {change['change_id']}",
                    f"- 状态：{change['status']}",
                    f"- 时间：{change['created_at']}",
                    f"- 影响任务：{impacted}",
                    f"- 说明：{change['summary']}",
                    "",
                ]
            )
            acceptance_lines = [
                f"- {item}" for item in change.get("acceptance_points", [])
            ] or ["- 暂无"]
            changed_task_lines = [
                f"- {item}" for item in change.get("changed_task_ids", [])
            ] or ["- 无"]
            capture_lines = [
                f"- {item}" for item in change.get("capture_ids", [])
            ] or ["- 暂无"]
            single_lines = [
                f"# {change['change_id']}",
                "",
                f"- 状态：{change['status']}",
                f"- 时间：{change['created_at']}",
                f"- 确认结果：{change.get('confirmation', '待确认')}",
                "",
                "## 变更原因",
                *format_markdown_content_lines(change.get("reason") or "按变更内容记录。"),
                "",
                "## 变更内容",
                *format_markdown_content_lines(change["description"]),
                "",
                "## 验收和回归要求",
                *acceptance_lines,
                "",
                "## 关联 capture",
                *capture_lines,
                "",
                "## 影响任务",
                *changed_task_lines,
            ]
            (changes_dir / f"{change['change_id']}.md").write_text(
                join_lines(single_lines), encoding="utf-8"
            )
        if not requirement["changes"]:
            summary_lines.append("- 暂无变更记录")
        (requirement_dir / "changes.md").write_text(
            join_lines(summary_lines), encoding="utf-8"
        )
        (requirement_dir / "change-map.md").write_text(
            join_lines(requirement_change_map_lines(requirement)), encoding="utf-8"
        )


def refresh_task_feedback_state(paths: ProjectPaths) -> dict[str, Any]:
    """刷新任务反馈形成的正式变更，同时保护已经归档的正式输入。"""

    state = refresh_task_runtime_state(paths)
    _write_runtime_change_files(paths, state)
    return state


def refresh_start_transaction_state(
    paths: ProjectPaths,
    *,
    committed_requirement_id: str,
) -> dict[str, Any]:
    """刷新建档后的全局投影，同时保持刚提交的正式目录字节不变。

    document-first 的正式目录已经在 staging 中完成逐文件校验。这里不能再走
    ``write_requirement_files``，否则旧渲染器会改写 effective、versions 和
    original，破坏刚刚提交的引用哈希；其余全局、DRAFT 和 SQLite 投影仍按
    同一事件真相源重建。
    """

    state = derive_state(paths)
    if committed_requirement_id not in state.get("requirements", {}):
        raise SdlcError(
            f"建档事件没有投影出正式需求：{committed_requirement_id}。",
            exit_code=1,
        )
    rebuild_database(paths, state)
    write_project_snapshot(paths, state)
    write_current_snapshot(paths, state)
    # DRAFT 与正式需求的细粒度文件在建档前都已经生成并归档。这里若再跑
    # 全量写入，会让旧投影器重新解释已冻结产物；事务提交只需刷新全局摘要
    # 和 SQLite，后续普通命令仍可按事件真相源读取 started 状态。
    cleanup_transient_files(paths)
    return state


def refresh_start_transaction_rollback_state(paths: ProjectPaths) -> dict[str, Any]:
    """回滚建档事件后只重建全局摘要和 SQLite，不改写已有正式原文。"""

    state = derive_state(paths)
    rebuild_database(paths, state)
    write_project_snapshot(paths, state)
    write_current_snapshot(paths, state)
    cleanup_transient_files(paths)
    return state


def detect_project_type(root: Path) -> str:
    markers = {
        "package.json": "node",
        "pyproject.toml": "python",
        "go.mod": "go",
        "Cargo.toml": "rust",
    }
    for filename, project_type in markers.items():
        if (root / filename).exists():
            return project_type
    return "generic"


def create_project_initialized_event(paths: ProjectPaths) -> dict[str, Any]:
    scripts, test_commands = detect_project_commands(paths.root)
    git_ignore_data = ensure_sdlc_global_ignore()
    return append_event(
        paths,
        event_type="project_initialized",
        source="sdlc-init",
        summary="初始化 SDLC 项目",
        payload={
            "project_name": paths.root.name,
            "project_path": str(paths.root),
            "project_type": detect_project_type(paths.root),
            "git_repo": (paths.root / ".git").exists() or (paths.root / ".git").is_dir(),
            "detected_scripts": scripts,
            "test_commands": test_commands,
            "git_ignore_file": git_ignore_data["git_ignore_file"],
            "git_ignore_rule": git_ignore_data["git_ignore_rule"],
            "sdlc_git_ignored": git_check_ignore(paths.root, ".codex-sdlc/current.md"),
        },
    )


def requirement_ids(state: dict[str, Any]) -> list[str]:
    return list(state["requirements"].keys())


def session_ids(state: dict[str, Any]) -> list[str]:
    return [session["session_id"] for session in state["sessions"]]


def verification_ids(state: dict[str, Any]) -> list[str]:
    return [verification["verification_id"] for verification in state["verifications"]]


def capture_ids(state: dict[str, Any]) -> list[str]:
    return [capture["capture_id"] for capture in state["captures"]]


def grill_ids(state: dict[str, Any]) -> list[str]:
    return [grill["grill_id"] for grill in state.get("grills", [])]


def material_ids(state: dict[str, Any]) -> list[str]:
    return [material["material_id"] for material in state.get("materials", [])]


def change_ids(state: dict[str, Any]) -> list[str]:
    result = [change["change_id"] for change in state["changes"]]
    # 新式结构化工作区不进入旧 change 列表，但旧入口仍然存在到 T-041。
    # 把创建事件中的受控编号纳入旧分配视图，避免两个入口在过渡期生成同号 CHG。
    for event in state.get("events", []):
        if not isinstance(event, dict) or event.get("event_type") != "change_workspace_created":
            continue
        payload = event.get("payload")
        change_id = payload.get("change_id") if isinstance(payload, dict) else None
        if isinstance(change_id, str) and re.fullmatch(r"CHG-[0-9]+", change_id):
            result.append(change_id)
    return list(dict.fromkeys(result))


def design_ids(state: dict[str, Any]) -> list[str]:
    return [
        *[design["design_id"] for design in state["designs"]],
        *[
            reference["design_id"]
            for reference in state.get("design_references", [])
            if isinstance(reference, dict) and reference.get("design_id")
        ],
    ]


def draft_ids(state: dict[str, Any]) -> list[str]:
    return list(state.get("drafts", {}).keys())


def build_requirement_title(description: str) -> str:
    clean = shorten_text(description, 24)
    return clean if clean else "未命名需求"


def generate_requirement_folder(requirement_id: str, description: str) -> str:
    return f"{requirement_id}-{slugify_text(description)}"


def resolve_requirement(state: dict[str, Any], requirement_id: str) -> dict[str, Any]:
    requirement = state["requirements"].get(requirement_id)
    if requirement is None:
        raise SdlcError(f"没有找到需求 `{requirement_id}`。")
    return requirement


def resolve_task(state: dict[str, Any], requirement_id: str | None, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for requirement in state["requirements"].values():
        if requirement_id is not None and requirement["requirement_id"] != requirement_id:
            continue
        for task in requirement["tasks"]:
            if task["task_id"] == task_id:
                matches.append((requirement, task))

    if not matches:
        target = f"{requirement_id} / {task_id}" if requirement_id else task_id
        raise SdlcError(f"没有找到任务 `{target}`。")
    if len(matches) > 1:
        raise SdlcError(f"任务编号 `{task_id}` 在多个需求里都存在，请补上需求编号。")
    return matches[0]


def render_status_text(paths: ProjectPaths, state: dict[str, Any]) -> str:
    _, materialized_issues = inspect_materialized_state(paths, state)
    blockers = collect_blockers(state, materialized_issues)
    if materialized_issues:
        docs_candidates = collect_docs_actions(state, paths.root)
        if docs_candidates and not state["active_requirements"]:
            primary_docs = docs_candidates[0]
            next_actions = {
                "primary": primary_docs["command"],
                "reason": primary_docs["reason"],
                "alternatives": ["$sdlc-doctor-repair", "$sdlc-status", "$sdlc-handoff"],
            }
        else:
            next_actions = {
                "primary": "$sdlc-doctor-repair",
                "reason": "当前快照或索引和事件记录对不上，先修复再继续推进更稳妥。",
                "alternatives": ["$sdlc-status", "$sdlc-handoff"],
            }
    else:
        next_actions = compute_next_actions(paths, state)
    total_subtasks = sum(requirement_subtask_count(requirement) for requirement in state["requirements"].values())
    lines = [
        "SDLC 状态",
        f"- 项目：{paths.root}",
        f"- 已初始化：{'是' if state['events'] else '否'}",
        f"- 正式任务总数：{state['counts']['all_tasks']}",
        f"- 子检查项总数：{total_subtasks}",
        "",
        "活跃需求",
    ]

    if not state["active_requirements"]:
        lines.append("- 当前没有活跃需求")
    else:
        for requirement in state["active_requirements"]:
            lines.append(f"- {requirement['requirement_id']} [{requirement['status']}] {requirement['title']}")
            lines.append(f"  - 技术方案：{requirement_design_status(requirement)}")
            lines.append(f"  - 正式任务：{len(requirement['tasks'])} 个")
            lines.append(f"  - 子检查项：{requirement_subtask_count(requirement)} 个")
            lines.append(f"  - 任务汇总：.codex-sdlc/requirements/{requirement['folder_name']}/plan.md")
            lines.append(f"  - 任务映射：.codex-sdlc/requirements/{requirement['folder_name']}/task-map.md")
            for task in requirement["tasks"]:
                line = f"  - {task['task_id']} [{task['status']}] {display_task_title(task)}"
                count = task_subtask_count(task)
                if count:
                    line += f"（含 {count} 个子检查项）"
                if (
                    isinstance(requirement.get("task_plan_contract"), Mapping)
                    and task["status"] in ACTIVE_TASK_RUN_STATUSES
                ):
                    probe = inspect_direct_task_run(paths, state, requirement, task)
                    line += f"；任务运行：{probe.get('status', '状态缺失')}"
                    if probe.get("valid") is not True:
                        line += f"；只读校验：停止（{probe['reason']}）"
                lines.append(line)

    legacy_task_packs = inspect_legacy_task_packs(paths, state)
    if legacy_task_packs:
        lines.extend(["", "既有任务执行包档案"])
        lines.extend(legacy_task_pack_display_lines(legacy_task_packs))

    lines.extend(["", "当前活跃 DRAFT"])
    lines.extend(active_draft_status_lines(state, next_actions))

    draft_lines = pending_requirement_draft_lines(state)
    if draft_lines:
        lines.extend(["", "待继续的新需求线索"])
        lines.extend(draft_lines)
        lines.append("- 推荐：$sdlc-discuss 继续完善需求草案")

    done_requirements = [
        requirement
        for requirement in state["requirements"].values()
        if requirement["status"] == "done"
    ]
    if done_requirements:
        lines.extend(["", "已完成待接受需求"])
        for requirement in done_requirements:
            lines.append(f"- {requirement['requirement_id']} [done] {requirement['title']}")
            lines.append(f"  - 正式任务：{len(requirement['tasks'])} 个")
            lines.append(f"  - 测试计划：.codex-sdlc/requirements/{requirement['folder_name']}/test-plan.md")
            lines.append(f"  - 推荐回归：$sdlc-regression {requirement['requirement_id']}")

    lines.extend(["", "技术方案"])
    if state["designs"]:
        for design in state["designs"][-5:]:
            requirement_id = design.get("requirement_id") or "未关联"
            lines.append(f"- {design['design_id']} [{design['status']}] {requirement_id}：{design['title']}")
    else:
        lines.append("- 暂无技术方案记录")

    recent_session = state["recent_session"]
    lines.extend(["", "最近交接"])
    if recent_session is None:
        lines.append("- 暂无交接记录")
    else:
        lines.append(f"- {recent_session['session_id']}：{recent_session['summary']}")

    lines.extend(["", "阻塞项"])
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- 当前没有明显阻塞")

    lines.extend(
        [
            "",
            "验证覆盖",
            f"- 已完成任务：{state['counts']['done_tasks']}",
            f"- 已关闭任务：{state['counts'].get('closed_tasks', 0)}",
            f"- 已完成或关闭：{state['counts'].get('finished_tasks', state['counts']['done_tasks'])}/{state['counts']['all_tasks']}",
            f"- 已有验证记录的任务：{state['counts']['verified_tasks']}/{state['counts']['done_tasks']}",
            "",
            "下一步建议",
            f"- 主推荐：{next_actions['primary']}",
            f"- 原因：{next_actions['reason']}",
        ]
    )
    lines.extend(model_advice_lines(next_actions))
    for item in next_actions["alternatives"]:
        lines.append(f"- 备选：{item}")
    return join_lines(lines)


def render_next_text(paths: ProjectPaths, state: dict[str, Any]) -> str:
    _, materialized_issues = inspect_materialized_state(paths, state)
    if materialized_issues:
        docs_candidates = collect_docs_actions(state, paths.root)
        if docs_candidates and not state["active_requirements"]:
            primary_docs = docs_candidates[0]
            lines = [
                "下一步推荐",
                f"- 主推荐：{primary_docs['command']}",
                f"- 原因：{primary_docs['reason']}",
                "",
                "状态提醒",
            ]
            lines.extend(f"- {item}" for item in materialized_issues[:5])
            lines.extend(["", "备选指令", "- $sdlc-doctor-repair", "- $sdlc-status"])
            return join_lines(lines)
        lines = [
            "下一步推荐",
            "- 主推荐：$sdlc-doctor-repair",
            "- 原因：当前快照或索引和事件记录对不上，先修复再继续推进更稳妥。",
            "",
            "需要修复的问题",
        ]
        lines.extend(f"- {item}" for item in materialized_issues)
        return join_lines(lines)

    next_actions = compute_next_actions(paths, state)
    lines = [
        "下一步推荐",
        f"- 主推荐：{next_actions['primary']}",
        f"- 原因：{next_actions['reason']}",
    ]
    lines.extend(["", "当前活跃 DRAFT"])
    lines.extend(active_draft_status_lines(state))
    draft_questions = [str(item).strip() for item in next_actions.get("draft_questions", []) if str(item).strip()]
    if draft_questions:
        lines.extend(["", "待用户回答"])
        lines.extend(f"- {item}" for item in draft_questions)
    draft_missing_items = [str(item).strip() for item in next_actions.get("draft_missing_items", []) if str(item).strip()]
    if draft_missing_items:
        lines.extend(["", "当前 DRAFT 还不能进入技术方案，建议先补："])
        lines.extend(f"- {item}" for item in draft_missing_items)
    if next_actions.get("draft_context") and state["active_requirements"]:
        lines.extend(["", "其它活跃需求"])
        for requirement in state["active_requirements"]:
            lines.append(f"- {requirement['requirement_id']} [{requirement['status']}] {requirement['title']}")
    draft_lines = pending_requirement_draft_lines(state)
    if draft_lines:
        lines.extend(["", "待继续的新需求线索"])
        lines.extend(draft_lines)
    task_business_lines = next_task_business_lines(next_actions)
    if task_business_lines:
        lines.extend(["", "下一任务说明"])
        lines.extend(task_business_lines)
    advice_lines = model_advice_lines(next_actions)
    if advice_lines:
        lines.extend(["", "模型建议"])
        lines.extend(advice_lines)
    lines.extend(["", "备选指令"])
    for item in next_actions["alternatives"]:
        lines.append(f"- {item}")
    return join_lines(lines)


def compact_handoff_text(text: str, length: int = 180) -> str:
    compact = re.sub(r"\s+", " ", str(text).strip())
    if len(compact) <= length:
        return compact
    return compact[: length - 3].rstrip() + "..."


def unique_handoff_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = compact_handoff_text(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def split_next_task_command(command: str) -> tuple[str | None, str | None]:
    match = re.search(r"\b(REQ-\d+)\s+(T-\d+)\b", command)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def find_handoff_focus_task(state: dict[str, Any], next_actions: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    requirement_id, task_id = split_next_task_command(str(next_actions.get("primary", "")))
    if requirement_id and task_id:
        try:
            return resolve_task(state, requirement_id, task_id)
        except SdlcError:
            pass

    for status in ["test_failed", "ready_for_user_check", "doing"]:
        for requirement in state["active_requirements"]:
            for task in requirement["tasks"]:
                if task["status"] == status:
                    return requirement, task

    for requirement in state["active_requirements"]:
        for task in requirement["tasks"]:
            if task["status"] == "todo" and task_dependencies_ready(state, task):
                return requirement, task
    return None


def requirement_task_summary(requirement: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for task in requirement["tasks"]:
        status = str(task["status"])
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        return f"{requirement['requirement_id']}：暂无任务。"
    order = ["todo", "doing", "ready_for_user_check", "test_failed", "done", "closed"]
    labels = {
        "todo": "未开始",
        "doing": "进行中",
        "ready_for_user_check": "待用户验收",
        "test_failed": "测试失败",
        "done": "已完成",
        "closed": "已关闭",
    }
    parts = [f"{labels.get(key, key)} {counts[key]}" for key in order if counts.get(key)]
    return f"{requirement['requirement_id']}：{('，'.join(parts))}。全量状态用 `$sdlc-status`。"


def latest_task_verification_lines(state: dict[str, Any], requirement_id: str, task_id: str, limit: int = 3) -> list[str]:
    matches = [
        verification
        for verification in state["verifications"]
        if verification.get("requirement_id") == requirement_id and verification.get("task_id") == task_id
    ][-limit:]
    return [
        f"{item.get('verification_id', 'VRF')}：{compact_handoff_text(item.get('summary', ''), 220)}"
        for item in matches
        if str(item.get("summary", "")).strip()
    ]


def focus_task_reason_lines(
    paths: ProjectPaths,
    state: dict[str, Any],
    requirement: dict[str, Any] | None,
    task: dict[str, Any] | None,
    next_actions: dict[str, Any],
) -> list[str]:
    if requirement is None or task is None:
        reason = compact_handoff_text(next_actions.get("reason", ""))
        return [reason] if reason else ["当前没有明确任务焦点，先按下一步推荐处理流程状态。"]

    status = task["status"]
    feedback = task_acceptance_feedback_lines(task)
    lines: list[str] = []
    if feedback:
        lines.append(f"{task['task_id']} 是原任务验收未通过后的返工，仍然属于当前任务，不是新增修复任务。")
        lines.append(f"用户反馈：{feedback[-1]}")
    elif status == "test_failed":
        lines.append(f"{task['task_id']} 自动测试失败，下一步先修当前任务或修正测试命令。")
    elif status == "ready_for_user_check":
        lines.append(f"{task['task_id']} 正在等待用户验收；通过就收口，不通过就 `$sdlc-task-restore`。")
    elif status == "doing":
        lines.append(f"{task['task_id']} 已经在进行中，下一步继续把当前任务收口。")
    elif status == "todo":
        lines.append(f"{task['task_id']} 是当前依赖已满足的下一条未开始任务。")
    else:
        lines.append(f"{task['task_id']} 当前状态是 {status}，按下一步推荐处理。")

    if isinstance(requirement.get("task_plan_contract"), Mapping):
        stop_reason = task_plan_stop_reason(requirement)
        if stop_reason:
            lines.append(stop_reason)
        else:
            lines.append("当前整套任务审核有效，任务按读取清单和 task-run 继续。")
    return unique_handoff_items(lines)


def focus_task_done_lines(state: dict[str, Any], requirement: dict[str, Any] | None, task: dict[str, Any] | None) -> list[str]:
    if requirement is None or task is None:
        recent = state.get("recent_session")
        if recent:
            return [f"最近正式交接：{recent['session_id']}：{recent['summary']}"]
        return ["暂无和当前下一步直接相关的任务执行记录。"]

    lines: list[str] = []
    changed_files = [str(item) for item in task.get("changed_files", [])][-6:]
    commands = [str(item) for item in task.get("commands", [])][-3:]
    verifications = latest_task_verification_lines(state, requirement["requirement_id"], task["task_id"], limit=3)
    if changed_files:
        lines.append("涉及文件：" + "、".join(changed_files))
    if commands:
        lines.append("最近命令：" + "；".join(commands))
    if verifications:
        lines.append("最近验证：" + "；".join(verifications))
    if not lines:
        lines.append("当前任务还没有可归纳的落地记录，先按正式任务审核和读取清单开工。")
    return unique_handoff_items(lines)


def handoff_attention_lines(
    paths: ProjectPaths,
    state: dict[str, Any],
    requirement: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> list[str]:
    lines: list[str] = []
    if requirement is not None and task is not None:
        for feedback in task_acceptance_feedback_lines(task):
            lines.append(f"验收反馈：{feedback}")
        if isinstance(requirement.get("task_plan_contract"), Mapping):
            stop_reason = task_plan_stop_reason(requirement)
            if stop_reason:
                lines.append(stop_reason)
            elif task["status"] in {"todo", "doing", "test_failed"}:
                lines.append("按正式任务审核、读取清单和当前 task-run 执行。")
        if task["status"] == "ready_for_user_check":
            lines.append("当前任务等待用户验收，不能自动开始下一任务。")
        if task["status"] == "test_failed":
            lines.append("当前任务测试失败，不能跳过失败继续后续任务。")

    if state["git_changed_files"]:
        files = [str(item) for item in state["git_changed_files"][:8]]
        more = " 等" if len(state["git_changed_files"]) > len(files) else ""
        lines.append("工作区有未提交改动：" + "、".join(files) + more)
    if state["pending_change_files"]:
        lines.append("存在待处理需求变更文件，继续开发前先按 `$sdlc-next` 推荐处理。")
    if state["pending_capture_files"]:
        lines.append("存在未归类中途结论，继续前要先确认是否纳入正式状态。")
    return unique_handoff_items(lines) or ["暂无额外阻塞；只按下一步执行即可。"]


def handoff_step_lines(
    requirement: dict[str, Any] | None,
    task: dict[str, Any] | None,
    next_actions: dict[str, Any],
) -> list[str]:
    primary = str(next_actions["primary"])
    if requirement is not None and task is not None:
        # task-done 也以“$sdlc-task”开头，必须先走专用分支，避免把同一收口命令输出两次。
        if primary.startswith("$sdlc-task-done"):
            return [
                "先核对当前 active 轮次的测试、人工验收和范围证据。",
                "按上面列出的下一步收口当前任务，然后停下汇报结果。",
            ]
        if re.fullmatch(r"\$sdlc-task\s+REQ-\d+\s+T-\d+", primary):
            return [
                "先读取当前运行轮次的完整机器清单并核对原文。",
                f"执行 `{primary}` 开始当前任务。",
                "开工后按正式读取清单和 task-run 继续，不提前推荐完成动作。",
            ]
    return [
        f"执行 `{primary}`。",
        "执行完停下汇报结果，不自动连续推进下一条推荐。",
    ]


def render_short_handoff_text(paths: ProjectPaths, state: dict[str, Any]) -> str:
    next_actions = compute_next_actions(paths, state)
    focus = find_handoff_focus_task(state, next_actions)
    requirement, task = focus if focus is not None else (None, None)
    lines = [
        "请继续当前项目开发。",
        "",
        f"项目路径：{paths.root}",
        "",
        "当前只继续这一项：",
    ]
    if requirement is not None and task is not None:
        title = compact_handoff_text(display_task_title(task), 160)
        lines.append(f"- {requirement['requirement_id']} / {task['task_id']} [{task['status']}] {title}")
        lines.append(f"- 下一步：{next_actions['primary']}")
        if isinstance(requirement.get("task_plan_contract"), Mapping):
            lines.append("- 执行依据：整套任务审核、读取清单和当前 task-run")
    else:
        lines.append(f"- 下一步：{next_actions['primary']}")
        lines.append(f"- 原因：{compact_handoff_text(next_actions.get('reason', '按当前流程状态推荐。'))}")

    lines.extend(["", "为什么是这一步："])
    lines.extend(f"- {item}" for item in focus_task_reason_lines(paths, state, requirement, task, next_actions))

    lines.extend(["", "上一轮实际做了什么："])
    lines.extend(f"- {item}" for item in focus_task_done_lines(state, requirement, task))

    lines.extend(["", "当前必须注意："])
    lines.extend(f"- {item}" for item in handoff_attention_lines(paths, state, requirement, task))

    lines.extend(["", "接手后怎么做："])
    for index, step in enumerate(handoff_step_lines(requirement, task, next_actions), start=1):
        lines.append(f"{index}. {step}")

    lines.extend(["", "其他任务概况："])
    if state["active_requirements"]:
        for active_requirement in state["active_requirements"]:
            lines.append(f"- {requirement_task_summary(active_requirement)}")
    else:
        lines.append("- 当前没有活跃需求；全量状态用 `$sdlc-status`。")

    lines.extend(
        [
            "",
            "边界：",
            "- 只执行上面“当前只继续这一项”里的下一步。",
            "- 当前任务完成、测试失败、需要用户确认或遇到阻塞后，先停下来汇报。",
            "- 命令末尾出现的下一步推荐只是建议，不是自动继续执行的授权。",
            "- 不要自动连续推进下一条任务或下一阶段，除非用户明确要求。",
            "- 不处理无关 Git 改动，不重排任务，不回到技术设计；除非执行中发现需求或方案本身已经不成立。",
            "- 需要全量状态时用 `$sdlc-status`，需要完整归档时用 `$sdlc-export`。",
            "- 需要完整交接提示词时用 `$sdlc-handoff --full`。",
        ]
    )
    return join_lines(lines)


def render_full_handoff_text(paths: ProjectPaths, state: dict[str, Any]) -> str:
    next_actions = compute_next_actions(paths, state)
    recent_session = state["recent_session"]
    lines = [
        "请继续当前项目的开发工作。",
        "",
        f"项目路径：{paths.root}",
        "",
        "当前目标：",
    ]

    if state["active_requirements"]:
        for requirement in state["active_requirements"]:
            lines.append(f"- {requirement['requirement_id']}：{requirement['title']}")
    else:
        lines.append("- 当前没有活跃需求，先补登记。")

    lines.extend(["", "当前进度："])
    if state["active_requirements"]:
        for requirement in state["active_requirements"]:
            for task in requirement["tasks"]:
                lines.append(f"- {requirement['requirement_id']} / {task['task_id']} [{task['status']}] {task['title']}")
    else:
        lines.append("- 暂无任务进度。")

    lines.extend(["", "最近交接："])
    if recent_session is None:
        lines.append("- 暂无正式交接记录。")
    else:
        lines.append(f"- {recent_session['session_id']}：{recent_session['summary']}")

    lines.extend(["", "涉及文件："])
    changed_files = state["git_changed_files"]
    if changed_files:
        for item in changed_files:
            lines.append(f"- {item}")
    else:
        lines.append("- 当前 Git 工作区没有未提交文件。")

    lines.extend(["", "已确认约束："])
    project = state["project"]
    lines.append("- 生命周期文档只保存在当前项目的 `.codex-sdlc/` 中，需求相关文档放到对应需求包。")

    lines.extend(["", "已执行验证："])
    if state["verifications"]:
        for verification in state["verifications"][-5:]:
            lines.append(
                f"- {verification['requirement_id']} / {verification['task_id']}：{verification['summary']}"
            )
    else:
        lines.append("- 暂无验证记录。")

    lines.extend(["", "最近变更和决策："])
    if state["pending_change_files"]:
        for file_path in state["pending_change_files"][-5:]:
            lines.append(f"- 待处理变更：{relative_to_project(paths.root, file_path)}")
    elif state["recent_decision_files"]:
        for file_path in state["recent_decision_files"]:
            lines.append(f"- 决策记录：{relative_to_project(paths.root, file_path)}")
    else:
        lines.append("- 暂无额外变更或决策记录。")

    unresolved = collect_unresolved_issues(state)
    lines.extend(["", "未解决问题："])
    if unresolved:
        for issue in unresolved:
            lines.append(f"- {issue}")
    else:
        lines.append("- 暂无明显阻塞。")

    lines.extend(
        [
            "",
            "下一步：",
            f"- {next_actions['primary']}",
            "",
            "执行边界：",
            "- 只执行上面“下一步”里的这一项。",
            "- 当前任务完成、测试失败、需要用户确认或遇到阻塞后，先停下来汇报。",
            "- 命令末尾出现的下一步推荐只是建议，不是自动继续执行的授权。",
            "- 除非用户明确说“连续推进下一个任务”，否则不要自动开始下一个任务。",
            "",
            "执行要求：",
            "- 先执行“下一步”里的这一项，不要跳过未完成任务或待处理变更。",
            "- 需求、任务、变更和验证记录继续写入对应需求包，避免在全局目录重复生成文档。",
            "- 对用户只推荐 `$sdlc-*` 指令，保持当前 SDLC 主线。",
            "",
            "继续方式：",
            "- 推荐继续使用 `$sdlc-*` 指令，保持状态一致。",
        ]
    )
    return join_lines(lines)


def render_handoff_text(paths: ProjectPaths, state: dict[str, Any], *, full: bool = False) -> str:
    if full:
        return render_full_handoff_text(paths, state)
    return render_short_handoff_text(paths, state)


def collect_unresolved_issues(state: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for requirement in state["active_requirements"]:
        for task in requirement["tasks"]:
            if task["status"] not in {"done", "closed"}:
                issues.append(f"{requirement['requirement_id']} / {task['task_id']} 还没完成")
    return issues


def collect_blockers(state: dict[str, Any], materialized_issues: list[str] | None = None) -> list[str]:
    blockers: list[str] = list(materialized_issues or [])
    if state["pending_capture_files"]:
        blockers.append(f"还有 {len(state['pending_capture_files'])} 条未归类 capture")
    if state.get("draft_change_files"):
        blockers.append(f"还有 {len(state['draft_change_files'])} 条待确认需求变化")
    if state.get("effective_change_files"):
        blockers.append(f"还有 {len(state['effective_change_files'])} 条已生效但未规划需求变化")
    orphan_change_count = max(
        0,
        len(state.get("pending_change_files", []))
        - len(state.get("draft_change_files", []))
        - len(state.get("effective_change_files", [])),
    )
    if orphan_change_count:
        blockers.append(f"还有 {orphan_change_count} 个未纳入状态的变更文件")

    task_map = project_task_map(state)
    for requirement in state["active_requirements"]:
        for task in requirement["tasks"]:
            if task["status"] != "todo" or not task["depends_on"]:
                continue
            pending_dependencies = [
                dependency
                for dependency in task["depends_on"]
                if task_map.get(dependency) is None
                or task_map[dependency]["status"] not in {"done", "closed"}
            ]
            if pending_dependencies:
                blockers.append(
                    f"{requirement['requirement_id']} / {task['task_id']} 依赖 {', '.join(pending_dependencies)}，现在还不能开始"
                )
    return blockers


def formal_reference_index_checks(
    paths: ProjectPaths,
) -> tuple[list[str], list[str]]:
    """只核对正式索引；即使 doctor 带 repair，也不能替用户重写业务定位。"""

    passed: list[str] = []
    failed: list[str] = []
    if not paths.requirements_dir.is_dir():
        return passed, failed

    from codex_sdlc.core.reference_index import (
        REFERENCE_INDEX_PATH,
        reference_index_issues,
    )

    checked = 0
    for requirement_dir in sorted(paths.requirements_dir.iterdir()):
        if not requirement_dir.is_dir():
            continue
        if requirement_dir.is_symlink():
            failed.append(f"正式需求目录不能是符号链接：{requirement_dir.name}")
            continue
        index_path = requirement_dir / REFERENCE_INDEX_PATH
        formal_path = requirement_dir / "original" / "formal.v3.json"
        requires_index = False
        if formal_path.is_symlink():
            failed.append(f"{requirement_dir.name} 的 formal.v3.json 不能是符号链接")
            continue
        if formal_path.is_file() and not formal_path.is_symlink():
            try:
                formal = json.loads(formal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                failed.append(f"{requirement_dir.name} 的 formal.v3.json 无法读取")
                continue
            if not isinstance(formal, dict):
                failed.append(f"{requirement_dir.name} 的 formal.v3.json 顶层不是对象")
                continue
            requires_index = (
                formal.get("workflow_profile") == "document-first.v1"
            )
        if not index_path.exists() and not index_path.is_symlink():
            if requires_index:
                failed.append(f"{requirement_dir.name} 缺少 reference-index.v1.json")
            continue
        checked += 1
        issues = reference_index_issues(requirement_dir, index_path)
        if issues:
            failed.extend(
                f"{requirement_dir.name} 的正式引用索引无效：{issue}"
                for issue in issues
            )
        else:
            passed.append(f"{requirement_dir.name} 的正式引用索引有效")
    if checked == 0 and not any("reference-index.v1.json" in item for item in failed):
        passed.append("当前没有需要检查的正式引用索引")
    return passed, failed


def doctor_checks(paths: ProjectPaths, state: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []
    failed: list[str] = []

    if paths.sdlc_dir.exists():
        passed.append(".codex-sdlc 目录存在")
        ds_store_files = sorted(paths.sdlc_dir.rglob(".DS_Store"))
        if ds_store_files:
            warnings.append(f"发现 {len(ds_store_files)} 个 .DS_Store，可执行 `$sdlc-doctor-repair` 清理")
    else:
        failed.append(".codex-sdlc 目录不存在")

    required_dirs = [
        paths.requirements_dir,
        paths.sessions_dir,
        paths.captures_dir,
        paths.grills_dir,
        paths.changes_dir,
        paths.decisions_dir,
        paths.designs_dir,
    ]
    if all(directory.exists() for directory in required_dirs):
        passed.append("基础目录结构完整")
    else:
        missing = [str(relative_to_project(paths.root, directory)) for directory in required_dirs if not directory.exists()]
        failed.append(f"缺少目录：{', '.join(missing)}")

    required_files = [
        paths.project_md,
        paths.current_md,
        paths.events_file,
        paths.database_file,
    ]
    missing_files = [str(relative_to_project(paths.root, file_path)) for file_path in required_files if not file_path.exists()]
    if missing_files:
        failed.append(f"缺少文件：{', '.join(missing_files)}")
    else:
        passed.append("核心文件完整")

    if not paths.events_file.exists():
        return passed, warnings, failed

    try:
        with paths.events_file.open("a+", encoding="utf-8"):
            passed.append("events.jsonl 可读可追加")
    except OSError as exc:
        failed.append(f"events.jsonl 无法追加：{exc}")

    try:
        with sqlite3.connect(paths.database_file) as connection:
            connection.execute("SELECT 1")
        passed.append("sdlc.db 可读")
    except sqlite3.Error as exc:
        failed.append(f"sdlc.db 无法读取：{exc}")

    try:
        _ = derive_state(paths)
        passed.append("JSONL 可以重建当前状态")
    except SdlcError as exc:
        failed.append(f"JSONL 重建失败：{exc.message}")

    materialized_passed, materialized_issues = inspect_materialized_state(paths, state)
    passed.extend(item for item in materialized_passed if item not in passed)
    failed.extend(item for item in materialized_issues if item not in failed)

    reference_passed, reference_failed = formal_reference_index_checks(paths)
    passed.extend(item for item in reference_passed if item not in passed)
    failed.extend(item for item in reference_failed if item not in failed)

    if read_global_excludesfile():
        passed.append("已配置 Git 全局忽略文件")
        if git_check_ignore(paths.root, ".codex-sdlc/current.md"):
            passed.append("`.codex-sdlc/` 已被 Git 忽略")
        else:
            failed.append("`.codex-sdlc/` 还没进入 Git 忽略策略")
    else:
        warnings.append("还没配置 Git 全局忽略文件，后续建议补上 `.codex-sdlc/` 忽略策略")

    if state["pending_capture_files"]:
        warnings.append(f"还有 {len(state['pending_capture_files'])} 条未归类 capture")
    else:
        passed.append("没有未归类 capture")

    if state.get("draft_change_files"):
        warnings.append(f"还有 {len(state['draft_change_files'])} 条待确认需求变化")
    if state.get("effective_change_files"):
        warnings.append(f"还有 {len(state['effective_change_files'])} 条已生效但未规划需求变化")
    orphan_change_count = max(
        0,
        len(state.get("pending_change_files", []))
        - len(state.get("draft_change_files", []))
        - len(state.get("effective_change_files", [])),
    )
    if orphan_change_count:
        warnings.append(f"还有 {orphan_change_count} 个未纳入状态的变更文件")
    if state.get("draft_change_files") or state.get("effective_change_files") or orphan_change_count:
        pass
    else:
        passed.append("没有待规划需求变化")

    project = state["project"]
    if project.get("hooks_ready"):
        passed.append("项目 Hooks 已安装")
    else:
        warnings.append("项目 Hooks 未安装，可重跑 `codex-sdlc init` 补齐")

    if project.get("rules_ready"):
        passed.append("项目 Rules 已安装")
    else:
        warnings.append("项目 Rules 未安装，可重跑 `codex-sdlc init` 补齐")

    legacy_passed, legacy_warnings = legacy_task_pack_check_messages(
        inspect_legacy_task_packs(paths, state)
    )
    passed.extend(item for item in legacy_passed if item not in passed)
    warnings.extend(item for item in legacy_warnings if item not in warnings)

    return passed, warnings, failed
