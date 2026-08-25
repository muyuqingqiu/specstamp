from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from codex_sdlc.core.backup import create_backup, record_auto_backup_failure, require_matching_sdlc_identity
from codex_sdlc.core.environment import version_text
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.state import derive_state, render_next_text
from codex_sdlc.commands import (
    accept_cmd,
    add_cmd,
    agent_sync_cmd,
    backup_cmd,
    capture_cmd,
    change_cmd,
    clean_cmd,
    context_cmd,
    draft_cmd,
    design_cmd,
    doctor_cmd,
    docs_cmd,
    export_cmd,
    finish_cmd,
    facts_cmd,
    grill_cmd,
    handoff_cmd,
    hooks_cmd,
    init_cmd,
    lessons_cmd,
    material_cmd,
    next_cmd,
    plan_cmd,
    regression_cmd,
    start_cmd,
    status_cmd,
    task_cmd,
)


def translate_argparse_error(message: str) -> str:
    required_match = re.match(r"the following arguments are required: (.+)", message)
    if required_match:
        return f"缺少必填参数：{required_match.group(1)}"

    invalid_choice_match = re.match(r"argument (.+): invalid choice: '(.+)' \((.+)\)", message)
    if invalid_choice_match:
        target, invalid_value, choices = invalid_choice_match.groups()
        return f"{target} 的值 `{invalid_value}` 不支持，{choices.replace('choose from', '可选值：')}"

    unrecognized_match = re.match(r"unrecognized arguments: (.+)", message)
    if unrecognized_match:
        return f"无法识别这些参数：{unrecognized_match.group(1)}"

    expected_match = re.match(r"argument (.+): expected one argument", message)
    if expected_match:
        return f"{expected_match.group(1)} 后面还缺少参数值"

    return message


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        translated = translate_argparse_error(message)
        raise SdlcError(f"{self.format_usage().strip()}\n参数错误：{translated}", exit_code=2)


def render_uninitialized_next_recommendation() -> str:
    return "\n".join(
        [
            "下一步推荐",
            "- 主推荐：$sdlc-init",
            "- 原因：当前目录还没有可用的 SDLC 状态，先初始化项目。",
            "",
            "备选指令",
            "- $sdlc-doctor-install",
            "- $sdlc-status",
        ]
    )


def render_state_read_failed_recommendation(error_message: str) -> str:
    return "\n".join(
        [
            "下一步推荐",
            "- 主推荐：$sdlc-doctor-repair",
            f"- 原因：当前状态读取失败：{error_message}",
            "",
            "备选指令",
            "- $sdlc-doctor-deep",
            "- $sdlc-status",
        ]
    )


def render_cli_next_recommendation() -> str:
    try:
        root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
        paths = build_paths(root)
        if not paths.events_file.exists():
            return render_uninitialized_next_recommendation()
        state = derive_state(paths)
        return render_next_text(paths, state)
    except SdlcError as exc:
        return render_state_read_failed_recommendation(exc.message)


def should_append_next_recommendation(args: argparse.Namespace, exit_code: int) -> bool:
    if exit_code != 0:
        return False
    command = getattr(args, "command", None)
    # 这些命令自己已经给出下一步，或可能处在恢复/体检场景；不要再按当前状态追加通用推荐。
    skip_commands = {
        "help",
        "next",
        "status",
        "init",
        "init-plain",
        "init-basic",
        "init-basic-plain",
        "start",
        "light-start",
        "discuss",
        "discuss-link",
        "capture",
        "capture-link",
        "capture-requirement",
        "capture-change",
        "add",
        "material",
        "design",
        "design-accept",
        "draft",
        "plan",
        "plan-add-task",
        "plan-amend-task",
        "plan-reorder",
        "plan-depends",
        "plan-close",
        "plan-priority",
        "change",
        "change-accept",
        "change-capture",
        "change-plan",
        "task",
        "task-done",
        "task-restore",
        "task-pause",
        "fix",
        "audit",
        "accept",
        "regression",
        "backup",
        "backup-list",
        "backup-clean",
        "restore",
        "context",
        "lessons",
        "doctor",
        "doctor-install",
        "doctor-repair",
        "doctor-deep",
        "docs",
        "finish",
        "handoff",
        "grill",
        "agent-sync",
        "hooks-upgrade",
        "tasks",
        "version",
    }
    return bool(command and command not in skip_commands)


def print_next_recommendation_footer() -> None:
    print()
    print(render_cli_next_recommendation(), end="")


AUTO_BACKUP_COMMANDS = {
    "init",
    "init-plain",
    "init-basic",
    "init-basic-plain",
    "start",
    "light-start",
    "discuss",
    "discuss-link",
    "capture",
    "capture-link",
    "capture-requirement",
    "capture-change",
    "add",
    "grill",
    "material",
    "design",
    "design-accept",
    "draft",
    "tasks",
    "plan",
    "plan-add-task",
    "plan-amend-task",
    "plan-reorder",
    "plan-depends",
    "plan-close",
    "plan-priority",
    "change",
    "change-accept",
    "change-capture",
    "change-plan",
    "task",
    "task-done",
    "task-restore",
    "task-pause",
    "fix",
    "audit",
    "accept",
    "regression",
    "docs",
    "finish",
    "handoff",
    "export",
    "export-requirement",
}

PINNED_AUTO_BACKUP_COMMANDS = {
    "design-accept",
    "task-done",
    "regression",
    "docs",
    "accept",
    "finish",
}

# 大多数写命令成功后的快照已经能恢复上一步状态；只有修复类命令需要额外留下写入前状态。
PRE_CHANGE_AUTO_BACKUP_COMMANDS = {"doctor-repair"}

IDENTITY_CHECK_COMMANDS = {
    *AUTO_BACKUP_COMMANDS,
    "backup",
    "next",
    "status",
    "doctor-repair",
}


def should_auto_backup(args: argparse.Namespace, *, phase: str) -> bool:
    if os.environ.get("CODEX_SDLC_DISABLE_AUTO_BACKUP") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("CODEX_SDLC_BACKUP_HOME"):
        return False
    command = getattr(args, "command", "")
    if phase not in {"before", "after"}:
        return False
    is_repair_command = command == "doctor-repair" or (command == "doctor" and getattr(args, "repair", False))
    if phase == "before":
        return command in PRE_CHANGE_AUTO_BACKUP_COMMANDS or is_repair_command
    if command == "lessons":
        return getattr(args, "lesson_command", "") in {"add", "apply", "promote", "demote", "retire"}
    if command in AUTO_BACKUP_COMMANDS:
        return True
    return is_repair_command


def run_auto_backup(args: argparse.Namespace, *, phase: str) -> None:
    if not should_auto_backup(args, phase=phase):
        return
    try:
        root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
        paths = build_paths(root)
        if not paths.events_file.exists():
            return
        command = getattr(args, "command", "command")
        result = create_backup(
            paths,
            label=f"auto-{command}-{phase}",
            pinned=command in PINNED_AUTO_BACKUP_COMMANDS and phase == "after",
            automatic=True,
            command=command,
            phase=phase,
        )
        git_result = result.get("git", {})
        if isinstance(git_result, dict) and git_result.get("status") == "failed":
            record_auto_backup_failure(
                command=command,
                phase=phase,
                message=str(git_result.get("message") or "Git 提交失败"),
            )
    except Exception as exc:
        # 自动备份不能打断用户当前命令；手动备份失败才需要显式报错。
        record_auto_backup_failure(command=getattr(args, "command", "command"), phase=phase, message=str(exc))
        return


def run_identity_guard(args: argparse.Namespace) -> None:
    command = getattr(args, "command", "")
    if command == "context":
        if getattr(args, "context_command", "") != "save":
            return
        root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
        paths = build_paths(root)
        require_matching_sdlc_identity(paths)
        return
    if command == "lessons":
        if getattr(args, "lesson_command", "") not in {"add", "apply", "promote", "demote", "retire"}:
            return
        root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
        paths = build_paths(root)
        require_matching_sdlc_identity(paths)
        return
    if command not in IDENTITY_CHECK_COMMANDS:
        return
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    require_matching_sdlc_identity(paths)


def run_start_transaction_recovery() -> None:
    """所有普通命令先收口建档事务，不能让身份检查或备份看到半成品。"""

    from codex_sdlc.core.start_transaction import (
        recover_incomplete_start_transactions,
    )

    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.sdlc_dir.is_dir():
        return
    recover_incomplete_start_transactions(paths)


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="specstamp",
        description="SpecStamp 本机软件开发生命周期辅助工具",
    )
    parser.add_argument("--version", action="store_true", help="显示当前版本")
    subparsers = parser.add_subparsers(dest="command", parser_class=ChineseArgumentParser)

    help_parser = subparsers.add_parser("help", help="查看帮助")
    help_parser.add_argument("topic", nargs="?", help="可选命令名")

    version_parser = subparsers.add_parser("version", help="显示当前版本")
    version_parser.set_defaults(func=lambda _args: print(version_text()) or 0)

    init_cmd.register(subparsers)
    agent_sync_cmd.register(subparsers)
    hooks_cmd.register(subparsers)
    clean_cmd.register(subparsers)
    accept_cmd.register(subparsers)
    backup_cmd.register(subparsers)
    context_cmd.register(subparsers)
    next_cmd.register(subparsers)
    capture_cmd.register(subparsers)
    add_cmd.register(subparsers)
    grill_cmd.register(subparsers)
    lessons_cmd.register(subparsers)
    material_cmd.register(subparsers)
    design_cmd.register(subparsers)
    draft_cmd.register(subparsers)
    facts_cmd.register(subparsers)
    start_cmd.register(subparsers)
    plan_cmd.register(subparsers)
    regression_cmd.register(subparsers)
    docs_cmd.register(subparsers)
    task_cmd.register(subparsers)
    change_cmd.register(subparsers)
    finish_cmd.register(subparsers)
    handoff_cmd.register(subparsers)
    status_cmd.register(subparsers)
    doctor_cmd.register(subparsers)
    export_cmd.register(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)

        if args.version:
            print(version_text())
            return 0

        if getattr(args, "command", None):
            run_start_transaction_recovery()

        if args.command == "help":
            topic = getattr(args, "topic", None)
            if not topic:
                parser.print_help()
                return 0
            nested_args = parser.parse_args([topic, "--help"])
            return 0 if nested_args else 0

        if not getattr(args, "command", None):
            parser.print_help()
            return 0

        run_identity_guard(args)
        run_auto_backup(args, phase="before")
        exit_code = int(args.func(args))
        if exit_code == 0:
            run_auto_backup(args, phase="after")
        if should_append_next_recommendation(args, exit_code):
            print_next_recommendation_footer()
        return exit_code
    except SdlcError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
