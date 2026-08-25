from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    create_project_initialized_event,
    derive_state,
    grill_ids,
    next_number,
    refresh_materialized_state,
    resolve_requirement,
    resolve_task,
)


GRILL_MODES = ["requirement", "product", "design", "task_plan", "change", "goal", "task"]
GRILL_STATUSES = ["resolved", "no_issue", "needs_user"]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("grill", help="记录需求、设计、任务规划或任务运行的质询结果")
    parser.add_argument("summary", nargs="?", help="质询结论，用自然语言写清楚即可")
    parser.add_argument("--requirement", help="关联需求编号，例如 REQ-001")
    parser.add_argument("--task", help="关联任务编号，例如 T-004")
    parser.add_argument("--mode", choices=GRILL_MODES, default="requirement", help="质询阶段")
    parser.add_argument("--status", choices=GRILL_STATUSES, default="resolved", help="质询处理状态")
    parser.add_argument("--question", action="append", default=[], help="关键问题，可重复传入")
    parser.add_argument("--answer", action="append", default=[], help="已有回答，可重复传入")
    parser.add_argument("--recommendation", default="", help="推荐处理方式")
    parser.add_argument("--source", default="", help="来源说明，例如用户回答、Goal 自答、代码核对")
    parser.set_defaults(func=run)


def single_active_requirement(state: dict[str, object]) -> dict[str, object] | None:
    active_requirements = list(state.get("active_requirements", []))
    if len(active_requirements) == 1:
        return active_requirements[0]
    return None


def clean_list(items: list[str]) -> list[str]:
    return [str(item).strip() for item in items if str(item).strip()]


def validate_grill_args(args: argparse.Namespace) -> None:
    questions = clean_list(args.question)
    answers = clean_list(args.answer)
    if args.mode == "goal":
        return

    if args.status == "needs_user":
        if not questions:
            raise SdlcError("普通阶段记录待用户回答的质询时，必须写清楚要问用户的问题。", exit_code=1)
        return

    if not answers:
        raise SdlcError("普通阶段的质询记录必须带用户回答；如果只是等待用户回答，请使用 --status needs_user 并写 --question。", exit_code=1)


def build_grill_association(
    paths,
    *,
    requirement: dict[str, object] | None,
    task: dict[str, object] | None,
    mode: str,
) -> dict[str, object]:
    """规划问题绑定整套任务审核，运行问题绑定当前任务轮次。"""

    if mode == "task_plan":
        if requirement is None:
            raise SdlcError("任务规划质询必须关联正式需求。", exit_code=1)
        review_state = requirement.get("task_plan_review_state")
        reviews = review_state.get("reviews", []) if isinstance(review_state, dict) else []
        current = [
            review
            for review in reviews
            if isinstance(review, dict) and review.get("is_current") is True
        ]
        if len(current) > 1:
            raise SdlcError("当前整套任务审核身份不唯一，不能记录规划质询。", exit_code=1)
        review = current[0] if current else {}
        return {
            "association_type": "task_plan_review",
            "task_plan_review_id": str(review.get("review_id") or ""),
            "task_plan_review_status": str(review.get("effective_status") or "missing"),
        }

    if mode != "task":
        return {}
    if requirement is None or task is None:
        raise SdlcError("任务运行质询必须同时关联需求和任务。", exit_code=1)

    from codex_sdlc.core.task_run import load_task_run_context

    try:
        context = load_task_run_context(
            paths,
            requirement_id=str(requirement["requirement_id"]),
            task_id=str(task["task_id"]),
        )
    except SdlcError as exc:
        raise SdlcError(
            "任务运行质询必须关联当前 task-run：" + exc.message,
            exit_code=exc.exit_code,
        ) from exc
    run = context.get("run")
    if not isinstance(run, dict):
        raise SdlcError("当前 task-run 内容不完整，不能记录运行质询。", exit_code=1)
    run_path = context.get("run_path")
    if isinstance(run_path, Path) and hasattr(paths, "root"):
        try:
            run_path_text = run_path.relative_to(paths.root).as_posix()
        except ValueError:
            run_path_text = str(run_path)
    else:
        run_path_text = str(run_path or "")
    return {
        "association_type": "task_run",
        "task_run_number": int(run["run_number"]),
        "task_run_status": str(run.get("status") or ""),
        "task_run_path": run_path_text,
    }


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not args.summary:
        print(f"当前目录：{root}")
        print(
            "请补上真实需要质询的业务或技术问题，例如："
            "`$sdlc-grill 阅读模式右侧回显规则和截图不一致，需要用户确认 --mode requirement --status needs_user "
            "--question \"右侧回显按截图颜色还是按文字规则？\"`。"
        )
        return 0

    summary = args.summary.strip()
    if not summary:
        raise SdlcError("质询结论不能为空。")
    validate_grill_args(args)

    with project_lock(paths):
        if not paths.events_file.exists() or not paths.events_file.read_text(encoding="utf-8").strip():
            create_project_initialized_event(paths)
        state = derive_state(paths)
        requirement = None
        task = None
        if args.requirement and args.task:
            requirement, task = resolve_task(state, args.requirement, args.task)
        elif args.requirement:
            requirement = resolve_requirement(state, args.requirement)
        elif args.task:
            requirement = single_active_requirement(state)
            if requirement is None:
                raise SdlcError("只指定任务时，需要当前项目只有一个活跃需求；否则请同时传入 --requirement。", exit_code=1)
            requirement, task = resolve_task(state, requirement["requirement_id"], args.task)

        association = build_grill_association(
            paths,
            requirement=requirement,
            task=task,
            mode=args.mode,
        )

        grill_id = next_number(grill_ids(state), "GRILL")
        requirement_id = requirement["requirement_id"] if requirement else None
        task_id = task["task_id"] if task else ""
        if requirement is not None:
            file_path = f".codex-sdlc/requirements/{requirement['folder_name']}/grills/{grill_id}.md"
        else:
            file_path = f".codex-sdlc/grills/{grill_id}.md"

        append_event(
            paths,
            event_type="grill_recorded",
            source="sdlc-grill",
            summary=f"记录质询 {grill_id}",
            requirement_id=requirement_id,
            task_id=task_id or None,
            payload={
                "grill_id": grill_id,
                "mode": args.mode,
                "status": args.status,
                "summary": summary,
                "questions": args.question,
                "answers": args.answer,
                "recommendation": args.recommendation,
                "source": args.source,
                "task_id": task_id,
                "file_path": file_path,
                **association,
            },
        )
        refresh_materialized_state(paths)

    print(f"已记录质询：{grill_id}")
    print(f"- 阶段：{args.mode}")
    print(f"- 状态：{args.status}")
    print(f"- 文件：{file_path}")
    if requirement_id:
        print(f"- 需求：{requirement_id}")
    if task_id:
        print(f"- 任务：{task_id}")
    if association.get("association_type") == "task_plan_review":
        print(f"- 任务审核：{association['task_plan_review_id'] or '尚未创建'}")
    if association.get("association_type") == "task_run":
        print(f"- 运行轮次：{int(association['task_run_number']):04d}")
    if args.status == "needs_user":
        print("下一步建议：先补齐用户回答或需求资料，再继续当前阶段。")
    else:
        print("下一步建议：$sdlc-next")
    return 0
