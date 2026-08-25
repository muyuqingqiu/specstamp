from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    collect_docs_actions,
    derive_state,
    latest_unresolved_manual_pending_event,
    now_iso,
    refresh_materialized_state,
    resolve_requirement,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("accept", help="确认需求真正结束")
    parser.add_argument("requirement_id", nargs="?", help="可选需求编号")
    parser.set_defaults(func=run)


def candidate_line(requirement: dict[str, object]) -> str:
    return f"{requirement['requirement_id']}：{requirement['title']}"


def recent_requirement(state: dict[str, object], candidates: list[dict[str, object]]) -> dict[str, object] | None:
    candidate_map = {requirement["requirement_id"]: requirement for requirement in candidates}
    for event in reversed(state["events"]):  # type: ignore[index]
        requirement_id = event.get("requirement_id")
        if requirement_id in candidate_map and event.get("source") in {"sdlc-task", "sdlc-plan", "sdlc-doctor"}:
            return candidate_map[requirement_id]
    return None


def select_requirement_to_accept(state: dict[str, object], requirement_id: str | None) -> dict[str, object]:
    if requirement_id:
        return resolve_requirement(state, requirement_id)

    done_candidates = [
        requirement
        for requirement in state["requirements"].values()  # type: ignore[index]
        if requirement["status"] == "done"
    ]
    recent_done = recent_requirement(state, done_candidates)
    if recent_done:
        return recent_done
    if len(done_candidates) == 1:
        return done_candidates[0]
    if len(done_candidates) > 1:
        lines = ["多个需求都已完成，请指定要接受哪一个："]
        lines.extend(f"- {candidate_line(requirement)}" for requirement in done_candidates)
        raise SdlcError("\n".join(lines), exit_code=1)

    active = list(state["active_requirements"])  # type: ignore[index]
    if len(active) == 1:
        return active[0]
    if active:
        lines = ["当前没有已完成需求，且有多个活跃需求，请指定需求编号："]
        lines.extend(f"- {candidate_line(requirement)}" for requirement in active)
        raise SdlcError("\n".join(lines), exit_code=1)
    raise SdlcError("当前没有可接受的需求。", exit_code=1)


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement = select_requirement_to_accept(state, args.requirement_id)
        unfinished = [
            task
            for task in requirement["tasks"]  # type: ignore[index]
            if task["status"] not in {"done", "closed"}
        ]
        if unfinished:
            lines = [f"{requirement['requirement_id']} 还有未完成任务，不能接受需求："]
            lines.extend(f"- {task['task_id']} [{task['status']}] {task['title']}" for task in unfinished)
            raise SdlcError("\n".join(lines), exit_code=1)
        pending_manual = latest_unresolved_manual_pending_event(state, requirement)
        if pending_manual is not None:
            payload = pending_manual.get("payload", {}) if isinstance(pending_manual.get("payload", {}), dict) else {}
            manual_items = payload.get("manual_items", []) if isinstance(payload, dict) else []
            lines = [f"{requirement['requirement_id']} 还有人工或模拟器验收项没确认，不能接受需求："]
            for item in manual_items[:8]:
                if not isinstance(item, dict):
                    continue
                case_ids = "、".join(str(case_id) for case_id in item.get("case_ids", []) if str(case_id).strip())
                title = str(item.get("task_title", "")).strip()
                task_id = str(item.get("task_id", "")).strip()
                lines.append(f"- {task_id} {title}" + (f"；待确认测试：{case_ids}" if case_ids else ""))
            lines.append(
                "请先完成上面的人工/视觉验收，再写入验证记录，"
                "例如 `$sdlc-task-done REQ-001 T-001 --verify \"人工验收通过：...\"`。"
            )
            raise SdlcError("\n".join(lines), exit_code=1)

        append_event(
            paths,
            event_type="requirement_accepted",
            source="sdlc-accept",
            summary=f"接受需求 {requirement['requirement_id']}",
            requirement_id=requirement["requirement_id"],
            payload={"accepted_at": now_iso()},
        )
        refreshed_state = refresh_materialized_state(paths)
        docs_actions = [
            action
            for action in collect_docs_actions(refreshed_state, paths.root)
            if action["command"].startswith(f"$sdlc-docs {requirement['requirement_id']}")
        ]
        next_command = docs_actions[0]["command"] if docs_actions else "$sdlc-finish"

    print(f"已接受需求：{requirement['requirement_id']}")
    print("状态：accepted")
    print(f"下一步建议：{next_command}")
    return 0
