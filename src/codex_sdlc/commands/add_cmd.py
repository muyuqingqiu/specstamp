from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.commands.plan_cmd import (
    added_task_change_redirects,
    append_extra_tasks,
    build_initial_tasks,
    ensure_mutable_task_plan_contract,
    print_current_next_suggestion,
    refresh_task_coverage_tests,
    refresh_planning_state,
    restore_existing_coverage_points,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    bind_tasks_to_current_contract,
    derive_state,
    resolve_requirement,
)
from codex_sdlc.core.task_quality import analyze_task_quality


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("add", help="按模型给出的显式类型追加内容")
    parser.add_argument("requirement_id", help="需求编号")
    parser.add_argument("description", help="要追加的内容")
    parser.add_argument("--kind", choices=["bug", "task", "change"], required=True, help="模型已确认的内容类型")
    parser.add_argument("--source-task", default="", help="bug 修复对应的原任务编号；--kind bug 时必填")
    parser.add_argument("--coverage", action="append", default=[], help="任务显式覆盖的 FR 编号，可重复传入")
    parser.add_argument("--acceptance", action="append", default=[], help="验收或回归要求，可重复传入")
    parser.add_argument("--task", action="append", default=[], help="变更的结构化任务，可重复传入；格式为“标题||摘要||FR-001,FR-002”")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if not str(args.description).strip():
        raise SdlcError("追加内容不能为空。", exit_code=1)

    kind = str(args.kind)
    if kind == "bug":
        raise SdlcError(
            "add 不再创建修复任务；当前任务反馈请使用 `$sdlc-task-restore`，历史已完成任务问题请使用 `$sdlc-fix`。",
            exit_code=1,
        )

    if kind == "task":
        return run_plain_task_autoflow(args)

    raise SdlcError(
        "add 不再登记自然语言变更；请先使用 `codex-sdlc change-create REQ-xxx --request-key <稳定请求键>` 创建结构化 CHG 工作区。",
        exit_code=1,
    )


def append_plain_task(
    paths,
    state: dict[str, object],
    requirement: dict[str, object],
    description: str,
    *,
    allow_broad_scope_tasks: bool = False,
    coverage_points: list[str] | None = None,
) -> tuple[str, dict[str, object] | None, str | None, str]:
    ensure_mutable_task_plan_contract(paths, requirement)
    tasks = build_initial_tasks(requirement)
    saved_coverage = {
        str(task.get("task_id", "")): [str(item) for item in (task.get("coverage_points") or []) if str(item).strip()]
        for task in tasks
    }
    tasks, created_tasks = append_extra_tasks(tasks, [description])
    if created_tasks:
        created_tasks[0]["coverage_points"] = list(dict.fromkeys(str(item).strip() for item in (coverage_points or []) if str(item).strip()))
    bind_tasks_to_current_contract(requirement, tasks, force=True)
    restore_existing_coverage_points(tasks, saved_coverage)
    redirects = added_task_change_redirects(requirement, created_tasks, allow_broad_scope_tasks=allow_broad_scope_tasks)
    if redirects:
        raise SdlcError(
            "--kind task 需要显式的任务覆盖关系：\n"
            + "\n".join(f"- {task['title']}：{reason}" for task, reason in redirects)
            + "\n请通过 --coverage 传入准确的 FR 编号，CLI 不会把它改猜为需求变更。",
            exit_code=1,
        )

    refresh_task_coverage_tests(requirement, tasks)
    quality_report = analyze_task_quality(tasks)
    append_event(
        paths,
        event_type="plan_updated",
        source="sdlc-add",
        summary=f"追加普通任务 {created_tasks[0]['task_id']}",
        requirement_id=requirement["requirement_id"],
        task_id=created_tasks[0]["task_id"],
        payload={
            "tasks": tasks,
            "priority": requirement.get("priority", "normal"),
            "blocked_reason": "",
            "resolved_change_ids": [],
            "task_quality": quality_report,
        },
    )
    refresh_planning_state(paths, str(requirement["requirement_id"]))
    return "task", created_tasks[0], None, ""


def run_plain_task_autoflow(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    description = str(args.description).strip()
    with project_lock(paths):
        state = derive_state(paths)
        requirement = resolve_requirement(state, args.requirement_id)
        _result_kind, created_task, _change_id, _redirect_reason = append_plain_task(
            paths,
            state,
            requirement,
            description,
            allow_broad_scope_tasks=False,
            coverage_points=list(getattr(args, "coverage", []) or []),
        )
        refreshed_state = derive_state(paths)
        refreshed_requirement = resolve_requirement(refreshed_state, args.requirement_id)
        task_id = str(created_task["task_id"]) if created_task else ""

    print("显式类型：已有需求下的普通任务")
    print(f"已自动追加任务：{refreshed_requirement['requirement_id']} / {task_id}")
    print("任务已经加入结构化任务计划，覆盖关系已同步。")
    print_current_next_suggestion(paths)
    return 0
