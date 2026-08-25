from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.commands.backup_cmd import print_backup_git_result, print_project_entry, print_requirement_entry
from codex_sdlc.core.backup import (
    clean_backups,
    create_backup,
    current_git_identity,
    identity_mismatch_items,
    project_backup_candidates,
    requirement_display_title,
    requirement_backup_candidates,
    require_matching_sdlc_identity,
    restore_project,
    restore_requirement,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import derive_state


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("context", help="查看、保存和恢复当前项目的 SDLC 上下文")
    context_subparsers = parser.add_subparsers(dest="context_command", parser_class=argparse.ArgumentParser)

    list_parser = context_subparsers.add_parser("list", help="查看同项目可恢复的 SDLC 上下文")
    list_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID")
    list_parser.set_defaults(func=run_list)

    save_parser = context_subparsers.add_parser("save", help="保存当前项目或需求的 SDLC 上下文")
    save_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID")
    save_parser.add_argument("--label", default="", help="快照标签")
    save_parser.add_argument("--pin", action="store_true", help="固定快照，避免自动清理")
    save_parser.set_defaults(func=run_save)

    restore_parser = context_subparsers.add_parser("restore", help="恢复项目或需求上下文")
    restore_parser.add_argument("requirement", nargs="?", help="可选需求编号或需求 UID")
    restore_parser.add_argument("--snapshot", default="", help="快照名、时间或路径片段")
    restore_parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    restore_parser.add_argument("--confirm", action="store_true", help="确认恢复")
    restore_parser.add_argument("--replace", action="store_true", help="允许覆盖当前已有状态")
    restore_parser.set_defaults(func=run_restore)

    import_parser = context_subparsers.add_parser("import", help="把其它分支或工作树的需求导入当前上下文")
    import_parser.add_argument("requirement", help="需求编号或需求 UID")
    import_parser.add_argument("--snapshot", default="", help="快照名、时间或路径片段")
    import_parser.add_argument("--as", dest="import_as", default="", help="导入成新的需求编号，例如 REQ-099")
    import_parser.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    import_parser.add_argument("--confirm", action="store_true", help="确认导入")
    import_parser.set_defaults(func=run_import)

    switch_parser = context_subparsers.add_parser("switch-check", help="检查切分支后当前 SDLC 上下文是否还能继续使用")
    switch_parser.set_defaults(func=run_switch_check)

    clean_parser = context_subparsers.add_parser("clean", help="清理旧上下文快照")
    clean_parser.add_argument("--keep-project", type=int, default=20, help="每个 worktree 保留多少个项目快照")
    clean_parser.add_argument("--keep-requirement", type=int, default=50, help="每个需求保留多少个需求快照")
    clean_parser.add_argument("--keep-days", type=int, default=90, help="最近多少天内每天额外保留一个代表快照")
    clean_parser.add_argument("--keep-auto-days", type=int, default=7, help="自动快照最多保留多少天，固定快照不受影响")
    clean_parser.set_defaults(func=run_clean)

    parser.set_defaults(func=run_default)


def identity_text(identity: dict[str, object]) -> str:
    branch = identity.get("branch") or identity.get("branch_ref") or "非 Git 分支"
    head = identity.get("head") or "未知提交"
    return f"{branch}（{head}）"


def print_context_summary(paths) -> None:
    identity = current_git_identity(paths.root)
    print("当前 SDLC 上下文")
    print(f"- 项目目录：{paths.root}")
    print(f"- 当前 Git：{identity_text(identity)}")
    print(f"- SDLC 目录：{paths.sdlc_dir}")
    if not paths.events_file.exists():
        print("- 初始化状态：还没有 `.codex-sdlc/events.jsonl`")
        print("下一步建议：$sdlc-init")
        return
    mismatches = identity_mismatch_items(paths)
    if mismatches:
        print("- 身份状态：不一致，不能直接继续写 SDLC 状态")
        for item in mismatches:
            print(f"  - {item['name']}：记录={item['stored']}，当前={item['current']}")
        print("可选处理：")
        print("1. `$sdlc-context list` 查看当前项目可恢复资料。")
        print("2. `$sdlc-context restore --dry-run` 预览恢复当前分支资料。")
        print("3. 要保留旧分支需求，先切回旧分支 `$sdlc-context save --pin`，再回来恢复或导入。")
        return
    print("- 身份状态：一致，可以继续使用当前 SDLC 状态。")
    state = derive_state(paths)
    active = state.get("active_requirements", [])
    if active:
        for requirement in active:
            if isinstance(requirement, dict):
                tasks = requirement.get("tasks", [])
                done = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") == "done")
                print(f"- 活跃需求：{requirement.get('requirement_id')} {requirement_display_title(requirement)}，任务 {done}/{len(tasks)}")
    else:
        print("- 活跃需求：暂无")


def run_default(_args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    print_context_summary(paths)
    return 0


def run_list(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    project_items = project_backup_candidates(root, limit=10)
    requirement_items = requirement_backup_candidates(root, args.requirement, limit=30)
    print("可恢复的 SDLC 上下文")
    if project_items:
        print("项目快照：")
        for item in project_items:
            print_project_entry(item)
    else:
        print("项目快照：暂无")
    if requirement_items:
        print("需求快照：")
        for item in requirement_items:
            print_requirement_entry(item)
    else:
        print("需求快照：暂无")
    print("下一步建议：恢复前先用 `$sdlc-context restore --dry-run` 或 `$sdlc-context import REQ-xxx --dry-run`。")
    return 0


def run_save(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，不能保存 SDLC 上下文。")
    require_matching_sdlc_identity(paths)
    with project_lock(paths):
        result = create_backup(paths, requirement_id=args.requirement, label=args.label or None, pinned=args.pin)
    print("已保存 SDLC 上下文")
    if args.pin:
        print("- 保护状态：已固定，自动清理不会删除这次快照。")
    for item in result["project_snapshots"]:
        print(f"- 项目快照：{item['archive']}")
    for item in result["requirement_snapshots"]:
        manifest = item["manifest"]
        print(f"- 需求快照：{manifest['requirement_id']} {requirement_display_title(manifest)} -> {item['archive']}")
    print_backup_git_result(result)
    print("下一步建议：$sdlc-context list")
    return 0


def print_restore_result(result: dict[str, object], *, requirement_mode: bool, action_name: str) -> None:
    candidate = result["candidate"]
    if requirement_mode:
        title = "需求上下文恢复预览" if result["mode"] == "preview" else f"已{action_name}需求上下文"
        print(title)
        print(f"- 需求：{candidate.get('requirement_id')} {requirement_display_title(candidate)}")
        if result.get("new_requirement_id"):
            print(f"- 导入编号：{result.get('new_requirement_id')}")
    else:
        title = "项目上下文恢复预览" if result["mode"] == "preview" else "已恢复项目上下文"
        print(title)
        print(f"- 项目：{candidate.get('project_name')}")
    print(f"- 时间：{candidate.get('created_at')}")
    if candidate.get("snapshot"):
        print(f"- 快照：{candidate.get('snapshot')}")
    branch = candidate.get("branch") or candidate.get("branch_ref") or "未知分支"
    print(f"- 来源分支：{branch} {candidate.get('head') or ''}".rstrip())
    print(f"- 来源工作树：{candidate.get('project_path') or candidate.get('worktree_key') or '未知工作树'}")
    if result["mode"] == "preview":
        print("- 结果：只预览，不写入文件。确认后再加 `--confirm`。")
    else:
        print("- 结果：已重建 SQLite 和 Markdown 快照。")
        if result.get("folder_name"):
            print(f"- 需求包目录：{result.get('folder_name')}")


def run_restore(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    confirm = bool(args.confirm and not args.dry_run)
    snapshot = args.snapshot or None
    if args.requirement:
        result = restore_requirement(root, args.requirement, confirm=confirm, replace=args.replace, snapshot=snapshot)
        print_restore_result(result, requirement_mode=True, action_name="恢复")
    else:
        result = restore_project(root, confirm=confirm, replace=args.replace, snapshot=snapshot)
        print_restore_result(result, requirement_mode=False, action_name="恢复")
    return 0


def run_import(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    confirm = bool(args.confirm and not args.dry_run)
    result = restore_requirement(
        root,
        args.requirement,
        confirm=confirm,
        replace=False,
        snapshot=args.snapshot or None,
        new_requirement_id=args.import_as or None,
    )
    print_restore_result(result, requirement_mode=True, action_name="导入")
    if result["mode"] == "preview":
        print("导入默认不会覆盖当前已有同名需求；如果要保留两份，请使用 `--as REQ-xxx` 指定新编号。")
    return 0


def run_switch_check(_args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    print_context_summary(paths)
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
    print("已清理旧 SDLC 上下文快照")
    print(f"- 已清理项目快照：{result['project']}")
    print(f"- 已清理需求快照：{result['requirement']}")
    print_backup_git_result(result)
    return 0
