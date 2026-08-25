from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    collect_unresolved_issues,
    compute_next_actions,
    derive_state,
    next_number,
    now_iso,
    refresh_materialized_state,
    session_ids,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("finish", help="生成本轮正式交接")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        session_id = next_number(session_ids(state), "SESSION")
        related_requirements = [item["requirement_id"] for item in state["active_requirements"]] or list(state["requirements"].keys())[-1:]
        related_tasks = [
            f"{requirement['requirement_id']} / {task['task_id']}"
            for requirement in state["requirements"].values()
            for task in requirement["tasks"]
            if requirement["requirement_id"] in related_requirements and task["status"] in {"doing", "done"}
        ]
        commands = [
            command
            for requirement in state["requirements"].values()
            for task in requirement["tasks"]
            if requirement["requirement_id"] in related_requirements
            for command in task["commands"]
        ]
        verifications = [
            f"{verification['requirement_id']} / {verification['task_id']}：{verification['summary']}"
            for verification in state["verifications"][-5:]
        ]
        next_actions = compute_next_actions(paths, state)
        unresolved = collect_unresolved_issues(state)
        summary = f"收口 {', '.join(related_requirements) or '当前项目'} 本轮进展"
        suggested_commit = f"交接：记录 {related_requirements[0] if related_requirements else '当前项目'} 本轮进展"

        append_event(
            paths,
            event_type="session_finished",
            source="sdlc-finish",
            summary=f"生成会话交接 {session_id}",
            payload={
                "session_id": session_id,
                "created_at": now_iso(),
                "summary": summary,
                "next_step": next_actions["primary"],
                "related_requirements": related_requirements,
                "related_tasks": related_tasks,
                "changed_files": state["git_changed_files"],
                "commands": commands,
                "verifications": verifications,
                "unresolved_issues": unresolved,
                "suggested_commit": suggested_commit,
                "file_path": f"sessions/{session_id}.md",
            },
        )
        refreshed_state = refresh_materialized_state(paths)

    latest_session = refreshed_state["recent_session"]
    print(f"已生成会话交接：{latest_session['session_id']}")
    print(f"文件：.codex-sdlc/{latest_session['file_path']}")
    print(f"下一步：{latest_session['next_step']}")
    return 0
