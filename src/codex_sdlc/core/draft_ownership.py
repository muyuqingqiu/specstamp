from __future__ import annotations

from pathlib import Path
from typing import Any


def clean_text(value: object) -> str:
    return str(value or "").strip()


def item_draft_id(item: dict[str, Any]) -> str:
    return clean_text(item.get("draft_id"))


def pending_requirement_draft_capture_ids(state: dict[str, Any], source_draft_id: str = "") -> list[str]:
    pending = [
        capture
        for capture in state.get("captures", [])
        if capture.get("status") == "pending" and capture.get("target_type") == "requirement_draft"
    ]
    if not source_draft_id:
        return [str(capture["capture_id"]) for capture in pending if not item_draft_id(capture)]

    matched = [str(capture["capture_id"]) for capture in pending if item_draft_id(capture) == source_draft_id]
    if matched:
        return matched

    # 兼容旧事件：旧 CAP 没有 draft_id，只有现场唯一未完成 DRAFT 时才归到当前 DRAFT。
    active_drafts = [
        draft
        for draft in state.get("drafts", {}).values()
        if isinstance(draft, dict) and clean_text(draft.get("status")) != "started"
    ]
    if len(active_drafts) == 1 and clean_text(active_drafts[0].get("draft_id")) == source_draft_id:
        return [str(capture["capture_id"]) for capture in pending if not item_draft_id(capture)]
    return []


def pending_requirement_drafts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("captures", [])
        if item.get("status") == "pending"
        and item.get("target_type") == "requirement_draft"
        and not item_draft_id(item)
    ]


def pending_requirement_draft_lines(state: dict[str, Any], limit: int = 5) -> list[str]:
    drafts = pending_requirement_drafts(state)
    lines: list[str] = []
    for item in drafts[:limit]:
        summary = str(item.get("summary") or "未命名需求线索").strip()
        capture_id = str(item.get("capture_id") or "CAP").strip()
        lines.append(f"- {capture_id}：{summary}")
    if len(drafts) > limit:
        lines.append(f"- 还有 {len(drafts) - limit} 条未展示，可用 `$sdlc-status` 查看全部。")
    return lines


def is_owned_pending_capture(capture: dict[str, Any], state: dict[str, Any]) -> bool:
    target_type = clean_text(capture.get("target_type"))
    if target_type == "requirement_draft":
        return True
    if item_draft_id(capture):
        return True
    if clean_text(capture.get("requirement_id")):
        return True
    return False


def pending_capture_files(root: Path, captures: list[dict[str, Any]]) -> list[Path]:
    # 这里只返回真正没有归属的 pending capture。需求讨论 CAP 即使还没建档，也属于 DRAFT 线索，
    # 不应在 doctor/status 里被当成“未归类中途结论”吓用户。
    state = {"captures": captures}
    return [
        root / str(capture["file_path"])
        for capture in captures
        if capture.get("status") == "pending" and not is_owned_pending_capture(capture, state)
    ]


def resource_ownership_issues(state: dict[str, Any]) -> list[str]:
    drafts = state.get("drafts", {}) if isinstance(state.get("drafts"), dict) else {}
    issues: list[str] = []
    for capture in state.get("captures", []):
        if not isinstance(capture, dict):
            continue
        draft_id = item_draft_id(capture)
        if not draft_id or capture.get("status") != "pending" or capture.get("target_type") != "requirement_draft":
            continue
        draft = drafts.get(draft_id)
        if isinstance(draft, dict) and clean_text(draft.get("status")) == "started":
            issues.append(f"{capture.get('capture_id')} 已纳入 {draft_id}，但 DRAFT 已建档后仍处于 pending。")
    return issues


def unlinked_accepted_design_ids_for_start(state: dict[str, Any], source_draft_id: str = "") -> list[str]:
    accepted_designs = [
        design
        for design in state.get("designs", [])
        if not design.get("requirement_id") and design.get("status") == "accepted"
    ]
    if not source_draft_id:
        return [str(design["design_id"]) for design in accepted_designs if not item_draft_id(design)]
    return [str(design["design_id"]) for design in accepted_designs if item_draft_id(design) == source_draft_id]
