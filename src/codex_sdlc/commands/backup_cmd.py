from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.backup import (
    clean_backups,
    create_backup,
    load_index,
    project_backup_candidates,
    requirement_display_description,
    requirement_display_title,
    requirement_backup_candidates,
    require_matching_sdlc_identity,
    restore_project,
    restore_requirement,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root


def print_backup_git_result(result: dict[str, object]) -> None:
    git_result = result.get("git")
    if not isinstance(git_result, dict):
        return
    status = git_result.get("status")
    if status == "committed":
        head = git_result.get("head") or "未知提交"
        prefix = "已初始化并提交" if git_result.get("initialized") else "已提交"
        print(f"- 备份 Git：{prefix}（{head}）")
    elif status == "clean":
        print("- 备份 Git：没有新的变更需要提交")
    elif status == "unavailable":
        print(f"- 备份 Git：未启用（{git_result.get('message') or 'git 不可用'}）")
    elif status == "failed":
        print(f"- 备份 Git：提交失败（{git_result.get('message') or '未知原因'}）")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup_parser = subparsers.add_parser("backup", help="备份当前项目或指定需求的 SDLC 资料")
    backup_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID")
    backup_parser.add_argument("--label", default="", help="备份标签，便于人工识别")
    backup_parser.add_argument("--pin", action="store_true", help="固定这次快照，清理旧备份时不会自动删除")
    backup_parser.set_defaults(func=run_backup)

    list_parser = subparsers.add_parser("backup-list", help="查看本机 SDLC 备份")
    list_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID")
    list_parser.add_argument("--limit", type=int, default=3, help="每类最多展示多少条候选，默认 3 条")
    list_parser.add_argument("--all", action="store_true", help="展示所有匹配候选，适合排查历史备份")
    list_parser.set_defaults(func=run_list)

    clean_parser = subparsers.add_parser("backup-clean", help="清理旧的本机 SDLC 备份")
    clean_parser.add_argument("--keep-project", type=int, default=20, help="每个 worktree 保留多少个项目快照")
    clean_parser.add_argument("--keep-requirement", type=int, default=50, help="每个需求保留多少个需求快照")
    clean_parser.add_argument("--keep-days", type=int, default=90, help="最近多少天内每天额外保留一个代表快照")
    clean_parser.add_argument("--keep-auto-days", type=int, default=7, help="自动快照最多保留多少天，固定快照不受影响")
    clean_parser.set_defaults(func=run_clean)

    restore_parser = subparsers.add_parser("restore", help="从本机备份恢复 SDLC 资料")
    restore_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID；不填则恢复项目快照")
    restore_parser.add_argument("--dry-run", action="store_true", help="只预览，不写入文件")
    restore_parser.add_argument("--confirm", action="store_true", help="确认执行恢复")
    restore_parser.add_argument("--replace", action="store_true", help="允许覆盖当前已有 SDLC 状态或同需求状态")
    restore_parser.add_argument("--select", action="store_true", help="列出候选，不执行恢复")
    restore_parser.add_argument("--snapshot", default="", help="按快照名称、时间或路径片段精确选择备份")
    restore_parser.set_defaults(func=run_restore)


def run_backup(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，不能备份。")
    require_matching_sdlc_identity(paths)
    with project_lock(paths):
        result = create_backup(paths, requirement_id=args.requirement, label=args.label or None, pinned=args.pin)

    print("已完成本机 SDLC 备份")
    print(f"备份目录：{result['backup_home']}")
    if args.pin:
        print("保护状态：已固定，自动清理不会删除这次快照。")
    if result["project_snapshots"]:
        for item in result["project_snapshots"]:
            print(f"- 项目快照：{item['archive']}")
    if result["requirement_snapshots"]:
        for item in result["requirement_snapshots"]:
            manifest = item["manifest"]
            print(f"- 需求快照：{manifest['requirement_id']} {requirement_display_title(manifest)} -> {item['archive']}")
    print_backup_git_result(result)
    print("下一步建议：$sdlc-next")
    return 0


def print_project_entry(entry: dict[str, object]) -> None:
    branch = entry.get("branch") or entry.get("branch_ref") or "未知分支"
    worktree = entry.get("project_path") or entry.get("worktree_key") or "未知工作树"
    pin_text = " pinned" if entry.get("pinned") else ""
    print(
        f"- 项目快照：{entry.get('project_name')} {entry.get('created_at')} "
        f"score={entry.get('score', '-')}{pin_text}"
    )
    if entry.get("snapshot"):
        print(f"  - 快照：{entry.get('snapshot')}")
    print(f"  - 分支：{branch} {entry.get('head') or ''}".rstrip())
    print(f"  - 工作树：{worktree}")
    requirements = entry.get("requirements", [])
    if isinstance(requirements, list) and requirements:
        req_text = "、".join(
            f"{item.get('requirement_id')} {requirement_display_title(item)}（{item.get('status', '未知状态')}）"
            for item in requirements[:5]
            if isinstance(item, dict)
        )
        print(f"  - 需求：{req_text}")
        for item in requirements[:3]:
            if not isinstance(item, dict):
                continue
            description = requirement_display_description(item, 80)
            if description:
                print(f"  - {item.get('requirement_id')} 需求简介：{description}")


def print_requirement_entry(entry: dict[str, object]) -> None:
    branch = entry.get("branch") or entry.get("branch_ref") or "未知分支"
    worktree = entry.get("project_path") or entry.get("worktree_key") or "未知工作树"
    pin_text = " pinned" if entry.get("pinned") else ""
    print(
        f"- 需求快照：{entry.get('requirement_id')} {requirement_display_title(entry)} "
        f"{entry.get('created_at')} score={entry.get('score', '-')}{pin_text}"
    )
    if entry.get("snapshot"):
        print(f"  - 快照：{entry.get('snapshot')}")
    print(f"  - 分支：{branch} {entry.get('head') or ''}".rstrip())
    print(f"  - 工作树：{worktree}")
    description = requirement_display_description(entry, 96)
    if description:
        print(f"  - 需求简介：{description}")


def restore_confirm_command(requirement: object | None = None, snapshot: str | None = None) -> str:
    parts = ["$sdlc-restore"]
    if requirement:
        parts.append(str(requirement))
    if snapshot:
        parts.extend(["--snapshot", snapshot])
    parts.append("--confirm")
    return " ".join(parts)


def run_list(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    show_all = bool(getattr(args, "all", False))
    raw_limit = int(getattr(args, "limit", 3) or 3)
    if raw_limit < 1:
        raise SdlcError("展示数量必须大于 0。")
    display_limit = 10_000 if show_all else raw_limit
    project_candidates = project_backup_candidates(root, limit=display_limit + (0 if show_all else 1))
    requirement_candidates = requirement_backup_candidates(
        root,
        args.requirement,
        limit=display_limit + (0 if show_all else 1),
    )
    project_hidden = not show_all and len(project_candidates) > display_limit
    requirement_hidden = not show_all and len(requirement_candidates) > display_limit
    project_items = project_candidates[:display_limit]
    requirement_items = requirement_candidates[:display_limit]
    if not project_items and not requirement_items:
        index_data = load_index()
        if not index_data.get("project_snapshots") and not index_data.get("requirement_snapshots"):
            print("当前没有本机 SDLC 备份。")
        else:
            print("没有找到和当前项目匹配的备份。")
        print("下一步建议：$sdlc-backup")
        return 0

    print("本机 SDLC 备份")
    if project_items:
        print("项目快照：")
        for item in project_items:
            print_project_entry(item)
    if requirement_items:
        print("需求快照：")
        for item in requirement_items:
            print_requirement_entry(item)
    if project_hidden or requirement_hidden:
        print(
            f"已默认收起更多历史备份；查看更多用 `$sdlc-backup-list --all`，"
            f"或用 `$sdlc-backup-list --limit {max(display_limit * 2, display_limit + 1)}`。"
        )
    print("下一步建议：需要恢复时先用 `$sdlc-restore --dry-run`。")
    return 0


def run_clean(args: argparse.Namespace) -> int:
    if args.keep_project < 1 or args.keep_requirement < 1 or args.keep_days < 0 or args.keep_auto_days < 0:
        raise SdlcError("保留数量必须大于 0，保留天数不能小于 0。")
    result = clean_backups(
        keep_project=args.keep_project,
        keep_requirement=args.keep_requirement,
        keep_days=args.keep_days,
        keep_auto_days=args.keep_auto_days,
    )
    print("已清理本机 SDLC 旧备份")
    print(f"- 已清理项目快照：{result['project']}")
    print(f"- 已清理需求快照：{result['requirement']}")
    print(f"- 保留策略：最近 {args.keep_days} 天内每天最多额外保留一个代表快照。")
    print(f"- 自动快照最多保留：{args.keep_auto_days} 天。")
    print_backup_git_result(result)
    print("下一步建议：$sdlc-backup-list")
    return 0


def run_restore(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    if args.select:
        return run_list(argparse.Namespace(requirement=args.requirement))
    confirm = bool(args.confirm and not args.dry_run)
    snapshot = args.snapshot or None
    if args.requirement:
        result = restore_requirement(root, args.requirement, confirm=confirm, replace=args.replace, snapshot=snapshot)
        candidate = result["candidate"]
        print("需求快照恢复预览" if result["mode"] == "preview" else "已恢复需求快照")
        print(f"- 需求：{candidate.get('requirement_id')} {requirement_display_title(candidate)}")
        print(f"- 时间：{candidate.get('created_at')}")
        if candidate.get("snapshot"):
            print(f"- 快照：{candidate.get('snapshot')}")
        branch = candidate.get("branch") or candidate.get("branch_ref") or "未知分支"
        print(f"- 来源分支：{branch} {candidate.get('head') or ''}".rstrip())
        print(f"- 来源工作树：{candidate.get('project_path') or candidate.get('worktree_key') or '未知工作树'}")
        description = requirement_display_description(candidate, 96)
        if description:
            print(f"- 需求简介：{description}")
        if result["mode"] == "preview":
            confirm_command = restore_confirm_command(candidate.get("requirement_id"), str(candidate.get("snapshot") or "") or None)
            print(f"- 结果：只预览，不写入文件。确认恢复请使用 `{confirm_command}`。")
        else:
            folder_name = result.get("folder_name", "")
            if folder_name:
                print(f"- 结果：已合并事件、重建 SQLite 和 Markdown，并恢复需求包目录 `{folder_name}`。")
            else:
                print("- 结果：已合并事件并重建 SQLite 和 Markdown 快照。")
            repaired = result.get("contract_repaired", [])
            if repaired:
                print("- 结果：已刷新任务版本和覆盖关系：" + "、".join(str(item) for item in repaired))
        return 0

    result = restore_project(root, confirm=confirm, replace=args.replace, snapshot=snapshot)
    candidate = result["candidate"]
    print("项目快照恢复预览" if result["mode"] == "preview" else "已恢复项目快照")
    print(f"- 项目：{candidate.get('project_name')}")
    print(f"- 时间：{candidate.get('created_at')}")
    if candidate.get("snapshot"):
        print(f"- 快照：{candidate.get('snapshot')}")
    branch = candidate.get("branch") or candidate.get("branch_ref") or "未知分支"
    print(f"- 来源分支：{branch} {candidate.get('head') or ''}".rstrip())
    print(f"- 来源工作树：{candidate.get('project_path') or candidate.get('worktree_key') or '未知工作树'}")
    for requirement in candidate.get("requirements", [])[:8]:
        if isinstance(requirement, dict):
            print(f"- 需求：{requirement.get('requirement_id')} {requirement_display_title(requirement)}")
    if result["mode"] == "preview":
        confirm_command = restore_confirm_command(snapshot=str(candidate.get("snapshot") or "") or None)
        print(f"- 结果：只预览，不写入文件。确认恢复请使用 `{confirm_command}`。")
    else:
        print("- 结果：已恢复 `.codex-sdlc/` 中备份包包含的资料，并重建 SQLite 和 Markdown 快照。")
        moved_paths = result.get("moved_paths", [])
        if moved_paths:
            print("- 恢复前旧目录已移走：" + "、".join(str(item) for item in moved_paths))
        repaired = result.get("contract_repaired", [])
        if repaired:
            print("- 结果：已刷新任务版本和覆盖关系：" + "、".join(str(item) for item in repaired))
    return 0
