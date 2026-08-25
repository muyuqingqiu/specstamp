from __future__ import annotations

import argparse
import json
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.change_workspace import environment_interruption_hook
from codex_sdlc.core.change_workspace import change_package_environment_interruption_hook
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    change_ids,
    derive_state,
    load_events,
    next_number,
    refresh_materialized_state,
    resolve_requirement,
)


CURRENT_TASK_PROTECTION_STATUSES = {"doing", "ready_for_user_check", "test_failed"}


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    migrate_parser = subparsers.add_parser(
        "change-migrate", help="只读扫描旧变更并显式登记迁移分类"
    )
    migrate_children = migrate_parser.add_subparsers(dest="change_migrate_command")
    migrate_scan = migrate_children.add_parser("scan", help="只读列出全部旧变更来源和原始哈希")
    migrate_scan.set_defaults(func=run_migrate_scan)
    migrate_confirm = migrate_children.add_parser("confirm", help="校验完整分类确认并原子写入登记表")
    migrate_confirm.add_argument("--file", required=True, help="项目内 change-migration-confirmation.v1 JSON 路径")
    migrate_confirm.set_defaults(func=run_migrate_confirm)
    migrate_parser.set_defaults(func=run_migrate_default)

    create_parser = subparsers.add_parser(
        "change-create", help="创建结构化变更工作区并固定当前基础版本"
    )
    create_parser.add_argument("requirement_id", help="正式需求编号")
    create_parser.add_argument(
        "--request-key",
        required=True,
        help="同一次创建动作使用的稳定请求键",
    )
    create_parser.set_defaults(func=run_create)

    package_parser = subparsers.add_parser(
        "change-package", help="校验并提交完整变更包与五份预计结果"
    )
    package_parser.add_argument("requirement_id", help="正式需求编号")
    package_parser.add_argument("change_id", help="结构化变更编号")
    package_parser.add_argument("--package", required=True, help="change-package.v1 项目相对 JSON 路径")
    package_parser.add_argument(
        "--projected-requirement", required=True, help="预计需求完整版本项目相对 JSON 路径"
    )
    package_parser.add_argument(
        "--projected-design", required=True, help="预计设计完整版本项目相对 JSON 路径"
    )
    package_parser.add_argument(
        "--projected-test-matrix", required=True, help="预计测试矩阵项目相对 JSON 路径"
    )
    package_parser.add_argument(
        "--projected-reference-index", required=True, help="预计引用索引项目相对 JSON 路径"
    )
    package_parser.add_argument(
        "--projected-task-plan", required=True, help="预计任务计划项目相对 JSON 路径"
    )
    package_parser.set_defaults(func=run_package)

    protect_parser = subparsers.add_parser(
        "change-protect", help="核对变更审核并保护受影响活动任务"
    )
    protect_parser.add_argument("requirement_id", help="正式需求编号")
    protect_parser.add_argument("change_id", help="结构化变更编号")
    protect_parser.add_argument(
        "--confirm-requirement",
        action="store_true",
        help="用户确认当前 CHG 已通过审核的预计需求版本",
    )
    protect_parser.set_defaults(func=run_protect)

    # 通用审核服务已经存在，但主 CLI 还没有注册其三类入口。正式变更必须复用
    # review create / submit / status，不能再造一套只对 CHG 生效的审核命令。
    review_parser = subparsers.add_parser("review", help="创建、提交和读取通用独立审核")
    review_children = review_parser.add_subparsers(dest="review_command")
    review_create = review_children.add_parser("create", help="从当前受控输入创建审核请求")
    review_create.add_argument("--review-id", required=True, help="审核请求编号")
    review_create.add_argument(
        "--stage",
        required=True,
        choices=("requirement_split", "integrated_design", "task_plan"),
        help="固定审核阶段",
    )
    review_create.add_argument("--owner", required=True, help="被审核对象编号")
    review_create.add_argument("--input", action="append", required=True, help="受控输入文件")
    review_create.add_argument("--check", action="append", default=[], help="审核检查项")
    review_create.set_defaults(func=run_review_create)
    review_submit = review_children.add_parser("submit", help="在独立任务中提交审核结果")
    review_submit.add_argument("--request", required=True, help="审核请求编号")
    review_submit.add_argument("--file", required=True, help="review-result.v1 JSON 文件")
    review_submit.set_defaults(func=run_review_submit)
    review_status = review_children.add_parser("status", help="读取当前审核状态")
    review_status.add_argument("--review", default="", help="只查看一个审核请求编号")
    review_status.set_defaults(func=run_review_status)
    review_parser.set_defaults(func=run_review_default)

    parser = subparsers.add_parser("change", help="记录需求变化并标记受影响任务")
    parser.add_argument("requirement_id", help="需求编号")
    parser.add_argument("description", help="变更内容")
    parser.add_argument("--reason", default="", help="变更原因")
    parser.add_argument("--confirm", default="待确认", help="用户确认结果")
    parser.add_argument("--capture", action="append", default=[], help="引用的 capture 编号，可重复传入")
    parser.add_argument("--acceptance", action="append", default=[], help="验收或回归要求，可重复传入")
    parser.add_argument("--task", action="append", default=[], help="Codex 模型判断后的任务建议，可重复传入；格式为“标题”或“标题||摘要”")
    parser.add_argument("--impacted-task", action="append", default=[], help="显式声明受影响任务编号，可重复传入")
    parser.set_defaults(func=run)

    capture_parser = subparsers.add_parser("change-capture", help="记录需求变化并关联 capture")
    capture_parser.add_argument("requirement_id", help="需求编号")
    capture_parser.add_argument("capture_id", help="引用的 capture 编号")
    capture_parser.add_argument("description", help="变更内容")
    capture_parser.add_argument("--reason", default="", help="变更原因")
    capture_parser.add_argument("--confirm", default="待确认", help="用户确认结果")
    capture_parser.add_argument("--acceptance", action="append", default=[], help="验收或回归要求，可重复传入")
    capture_parser.add_argument("--task", action="append", default=[], help="Codex 模型判断后的任务建议，可重复传入；格式为“标题”或“标题||摘要”")
    capture_parser.add_argument("--impacted-task", action="append", default=[], help="显式声明受影响任务编号，可重复传入")
    capture_parser.set_defaults(func=run_capture)

    accept_parser = subparsers.add_parser("change-accept", help="确认需求变更并生成新的当前生效版本")
    accept_parser.add_argument("requirement_id", help="需求编号")
    accept_parser.add_argument("change_id", nargs="?", help="可选变更编号；不传时自动选择唯一待确认变更")
    accept_parser.set_defaults(func=run_accept)


def run_create(args: argparse.Namespace) -> int:
    """命令层只转交两个结构化参数，不接收或理解业务变更正文。"""

    from codex_sdlc.services.change_service import create_change_workspace
    from codex_sdlc.core.change_migration import ensure_change_migration_allows_progress

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    ensure_change_migration_allows_progress(paths, allow_blocked_rebuild=True)
    result = create_change_workspace(
        paths,
        requirement_id=args.requirement_id,
        request_key=args.request_key,
        interruption_hook=environment_interruption_hook(),
    )
    print(f"需求：{result.requirement_id}")
    print(f"变更：{result.change_id}")
    print(f"工作区：{result.workspace_path}")
    print(f"创建事件：{result.created_event_id}")
    return 0


def run_package(args: argparse.Namespace) -> int:
    """命令层只组装六个固定文件参数，不接受自然语言正文或目录。"""

    from codex_sdlc.services.change_service import submit_change_package
    from codex_sdlc.core.change_migration import ensure_change_migration_allows_progress

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    ensure_change_migration_allows_progress(paths, allow_blocked_rebuild=True)
    result = submit_change_package(
        paths,
        requirement_id=args.requirement_id,
        change_id=args.change_id,
        package_path=args.package,
        projected_paths={
            "projected-requirement.v2.json": args.projected_requirement,
            "projected-design.v2.json": args.projected_design,
            "projected-test-matrix.v2.json": args.projected_test_matrix,
            "projected-reference-index.v2.json": args.projected_reference_index,
            "projected-task-plan.v2.json": args.projected_task_plan,
        },
        interruption_hook=change_package_environment_interruption_hook(),
    )
    print(f"需求：{result.requirement_id}")
    print(f"变更：{result.change_id}")
    print(f"工作区：{result.workspace_path}")
    print(f"预计结果事件：{result.projected_event_id}")
    print(f"输入身份：{result.package_identity_sha256}")
    print(f"正式编号映射：{json.dumps(result.id_mapping, ensure_ascii=False, sort_keys=True)}")
    print(f"幂等重试：{'是' if result.duplicate else '否'}")
    return 0


def run_protect(args: argparse.Namespace) -> int:
    """正式生效前只登记审核与任务保护结果，不切换任何有效版本。"""

    from codex_sdlc.services.change_service import protect_change_package
    from codex_sdlc.core.change_migration import ensure_change_migration_allows_progress

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    ensure_change_migration_allows_progress(paths, allow_blocked_rebuild=True)
    result = protect_change_package(
        paths,
        requirement_id=args.requirement_id,
        change_id=args.change_id,
        confirm_requirement=bool(args.confirm_requirement),
    )
    print(f"需求：{result['requirement_id']}")
    print(f"变更：{result['change_id']}")
    print(f"保护结果哈希：{result['protection_sha256']}")
    print(f"幂等重试：{'是' if result['idempotent'] else '否'}")
    return 0


def run_migrate_default(_args: argparse.Namespace) -> int:
    print("请指定 change-migrate 子命令：scan / confirm")
    return 0


def run_migrate_scan(_args: argparse.Namespace) -> int:
    """扫描只输出结构、路径和哈希，不读取 Markdown 正文推断业务关系。"""

    from codex_sdlc.core.change_migration import scan_legacy_change_records
    from codex_sdlc.core.structured_contract import canonical_json_text

    # 顶层 CLI 会给未知的新命令追加通用 next 文本。扫描结果必须保持为单一
    # JSON 文档，复用已有 change 的“已自行给出下一步”标记来关闭该展示尾注。
    _args.command = "change"
    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    print(canonical_json_text(scan_legacy_change_records(paths)), end="")
    return 0


def run_migrate_confirm(args: argparse.Namespace) -> int:
    from codex_sdlc.core.change_migration import register_change_migration

    args.command = "change"
    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    with project_lock(paths):
        result = register_change_migration(paths, args.file)
    print(f"迁移分类已登记：{result['registered_count']} 条")
    print(f"登记表：{result['registry_path']}")
    print(f"登记表哈希：{result['registry_sha256']}")
    print(f"幂等重试：{'是' if result['idempotent'] else '否'}")
    return 0


def _print_review_json(value: object) -> None:
    from codex_sdlc.core.structured_contract import canonical_json_text

    print(canonical_json_text(value), end="")


def run_review_default(_args: argparse.Namespace) -> int:
    print("请指定 review 子命令：create / submit / status")
    return 0


def run_review_create(args: argparse.Namespace) -> int:
    from codex_sdlc.services import review_service

    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    result = review_service.create_review(
        paths,
        review_id=args.review_id,
        stage=args.stage,
        owner_id=args.owner,
        input_paths=args.input,
        required_checks=args.check,
    )
    _print_review_json(result)
    return 0


def run_review_submit(args: argparse.Namespace) -> int:
    from codex_sdlc.services import review_service

    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    result = review_service.submit_review(
        paths,
        request_id=args.request,
        submission_file=Path(args.file),
    )
    _print_review_json(result)
    return 0


def run_review_status(args: argparse.Namespace) -> int:
    from codex_sdlc.services import review_service

    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    _print_review_json(review_service.review_status(paths, review_id=args.review or None))
    return 0


def run_capture(args: argparse.Namespace) -> int:
    args.capture = [args.capture_id]
    return run(args)


def parse_model_task_specs(requirement: dict[str, object], task_specs: list[str]) -> list[dict[str, object]]:
    """只解析固定分隔符字段，不从任务标题或变更正文推断覆盖关系。"""

    tasks: list[dict[str, object]] = []
    next_task_number = len(requirement["tasks"]) + 1  # type: ignore[arg-type]
    for index, raw_spec in enumerate(task_specs):
        spec = raw_spec.strip()
        if not spec:
            continue
        parts = [item.strip() for item in spec.split("||", 2)]
        title = parts[0] if parts else ""
        summary = parts[1] if len(parts) > 1 else ""
        points = [item.strip() for item in parts[2].split(",") if item.strip()] if len(parts) > 2 else []
        if not title:
            continue
        tasks.append(
            {
                "task_id": f"T-{next_task_number + index:03d}",
                "title": title,
                "summary": summary or title,
                "source": "model",
                "points": points,
            }
        )
    return tasks


def has_model_task_suggestions(change: dict[str, object]) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("source", "")) == "model" and str(item.get("title", "")).strip()
        for item in change.get("added_tasks", [])  # type: ignore[union-attr]
    )


def safe_existing_task_binding(requirement: dict[str, object], change: dict[str, object]) -> bool:
    changed_task_ids = [str(item) for item in change.get("changed_task_ids", []) if str(item).strip()]  # type: ignore[union-attr]
    if len(changed_task_ids) != 1:
        return False
    target_task_id = changed_task_ids[0]
    task = next(
        (
            item
            for item in requirement.get("tasks", [])  # type: ignore[union-attr]
            if str(item.get("task_id", "")) == target_task_id
        ),
        None,
    )
    return task is not None and str(task.get("status", "")) not in {"done", "closed"}


def should_wait_for_model_plan(requirement: dict[str, object], change: dict[str, object]) -> bool:
    return not has_model_task_suggestions(change) and not safe_existing_task_binding(requirement, change)


def build_added_tasks(
    requirement: dict[str, object],
    description: str,
    impacted_task_ids: list[str],
    task_specs: list[str] | None = None,
) -> list[dict[str, object]]:
    model_tasks = parse_model_task_specs(requirement, task_specs or [])
    if model_tasks:
        return model_tasks
    # 复杂业务变更不能由 CLI 猜一个“处理变更”的泛任务。
    # 没有模型任务建议时，先只记录变更，等 change-accept 后交给 Codex 通过 change-plan 正式写回拆分结果。
    return []


def record_change(
    paths,
    *,
    state: dict[str, object],
    requirement: dict[str, object],
    description: str,
    reason: str,
    confirm: str,
    capture_ids: list[str] | None = None,
    change_id: str | None = None,
    acceptance_points: list[str] | None = None,
    task_specs: list[str] | None = None,
    impacted_task_ids: list[str] | None = None,
) -> tuple[str, list[str], list[dict[str, object]]]:
    change_id = change_id or next_number(change_ids(state), "CHG")
    known_task_ids = {str(task.get("task_id") or "") for task in requirement.get("tasks", [])}  # type: ignore[union-attr]
    impacted_task_ids = list(
        dict.fromkeys(
            str(item).strip()
            for item in (impacted_task_ids or [])
            if str(item).strip() in known_task_ids
        )
    )
    added_tasks = build_added_tasks(requirement, description, impacted_task_ids, task_specs=task_specs)
    append_event(
        paths,
        event_type="change_recorded",
        source="sdlc-change",
        summary=f"登记变更 {change_id}",
        requirement_id=requirement["requirement_id"],
        payload={
            "change_id": change_id,
            "summary": description,
            "description": description,
            "reason": reason,
            "status": "draft",
            "confirmation": confirm,
            "changed_task_ids": impacted_task_ids,
            "added_tasks": added_tasks,
            "closed_task_ids": [],
            "acceptance_points": acceptance_points or [],
            "priority": "high",
            "blocked_reason": "待确认需求变化",
            "capture_ids": capture_ids or [],
            "file_path": f".codex-sdlc/requirements/{requirement['folder_name']}/changes/{change_id}.md",
        },
    )
    return change_id, impacted_task_ids, added_tasks


def select_change_to_accept(requirement: dict[str, object], change_id: str | None) -> dict[str, object]:
    changes = requirement.get("changes", [])  # type: ignore[assignment]
    if change_id:
        change = next((item for item in changes if item["change_id"] == change_id), None)  # type: ignore[index]
        if change is None:
            raise SdlcError(f"{requirement['requirement_id']} 没有找到变更 `{change_id}`。")
        return change  # type: ignore[return-value]

    draft_changes = [item for item in changes if item["status"] == "draft"]  # type: ignore[index]
    if not draft_changes:
        # 兼容旧项目：老版本记录的是 pending，但本质也是“已记录未确认/未规划”。
        draft_changes = [item for item in changes if item["status"] == "pending"]  # type: ignore[index]
    if not draft_changes:
        raise SdlcError(f"{requirement['requirement_id']} 当前没有待确认需求变更。")
    if len(draft_changes) > 1:
        lines = [
            f"{requirement['requirement_id']} 有多个待确认变更，请指定一个变更编号。",
            "可选变更：",
        ]
        lines.extend(f"- {item['change_id']}：{item['summary']}" for item in draft_changes)  # type: ignore[index]
        raise SdlcError("\n".join(lines), exit_code=1)
    return draft_changes[0]  # type: ignore[return-value]


def active_tasks_bound_to_change(
    requirement: dict[str, object],
    change: dict[str, object],
) -> list[dict[str, object]]:
    changed_ids = {str(task_id) for task_id in change.get("changed_task_ids", []) if str(task_id).strip()}
    if not changed_ids:
        return []
    return [
        task
        for task in requirement.get("tasks", [])  # type: ignore[union-attr]
        if str(task.get("task_id", "")) in changed_ids
        and str(task.get("status", "")) in CURRENT_TASK_PROTECTION_STATUSES
    ]


def raise_for_active_task_bound_change(requirement: dict[str, object], change: dict[str, object]) -> None:
    active_tasks = active_tasks_bound_to_change(requirement, change)
    if not active_tasks:
        return
    first_task = active_tasks[0]
    requirement_id = str(requirement["requirement_id"])
    task_id = str(first_task["task_id"])
    lines = [
        f"{change['change_id']} 明确绑定了当前任务 {task_id}，不能自动确认并重排。",
        "这样会打断当前任务收口，请先按正式分流处理：",
        f"- 当前任务目标要补：$sdlc-plan-amend-task {requirement_id} {task_id}",
        f"- 当前任务验收或测试不过：$sdlc-task-restore {requirement_id} {task_id} 反馈内容",
        f"- 需求目标整体变了：$sdlc-change {requirement_id} 变更内容",
    ]
    if len(active_tasks) > 1:
        lines.append("本次变更还命中了这些当前任务：")
        lines.extend(f"- {task['task_id']} [{task['status']}]" for task in active_tasks[1:])
    raise SdlcError("\n".join(lines), exit_code=1)


def run_accept(args: argparse.Namespace) -> int:
    """只允许结构化变更包进入正式生效事务，不再回退到旧变更事件。"""

    from codex_sdlc.core.change_migration import ensure_change_migration_allows_progress
    from codex_sdlc.services.change_service import (
        accept_change_package,
        change_accept_environment_interruption_hook,
    )

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    ensure_change_migration_allows_progress(paths)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    structured: list[str] = []
    for event in load_events(paths):
        payload = event.get("payload")
        if (
            event.get("event_type") == "change_package_projected"
            and isinstance(payload, dict)
            and payload.get("requirement_id") == args.requirement_id
            and (not args.change_id or payload.get("change_id") == args.change_id)
        ):
            structured.append(str(payload.get("change_id") or ""))
    structured = sorted(set(item for item in structured if item))
    if len(structured) > 1:
        raise SdlcError(
            "当前需求有多个已经提交完整变更包的 CHG，请明确传入变更编号："
            + "、".join(structured)
            + "。",
            exit_code=1,
        )
    if not structured:
        raise SdlcError(
            "没有找到可生效的结构化 change-package.v1。请先完成 change-create、change-package 和 change-protect，旧变更记录不会回退处理。",
            exit_code=1,
        )

    result = accept_change_package(
        paths,
        requirement_id=args.requirement_id,
        change_id=structured[0],
        interruption_hook=change_accept_environment_interruption_hook(),
    )
    print(f"需求：{result['requirement_id']}")
    print(f"变更：{result['change_id']}")
    print(f"事务：{result['transaction_id']}")
    print(f"目标版本：v{result['target_version']}")
    print(f"完成回执：{result['receipt_path']}")
    print(f"幂等重试：{'是' if result['idempotent'] else '否'}")
    return 0


def run(args: argparse.Namespace) -> int:
    """旧自然语言变更入口保留明确拒绝，避免重新生成非结构化 CHG。"""

    if not str(args.description or "").strip():
        raise SdlcError("变更内容不能为空。", exit_code=1)
    raise SdlcError(
        "change 不再登记自然语言变更。请先使用 `codex-sdlc change-create REQ-xxx --request-key <稳定请求键>` 创建结构化 CHG 工作区。",
        exit_code=1,
    )
