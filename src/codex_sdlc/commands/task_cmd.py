from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core import task_contract as formal_task_contract
from codex_sdlc.core.git_tools import current_git_changed_files, detect_project_commands, find_git_root, run_git
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    compute_next_actions,
    derive_state,
    event_write_lock,
    load_events,
    next_event_id,
    next_number,
    next_task_business_lines,
    now_iso,
    refresh_materialized_state,
    refresh_task_feedback_state,
    refresh_task_runtime_state,
    lesson_brief_lines,
    resolve_task,
    task_contract_gate_message,
    tasks_with_contract_issues,
    verification_ids,
)
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.structured_contract import canonical_json_text, validate_schema_document
from codex_sdlc.core.task_outputs import (
    create_task_output_contract,
    empty_formal_task_output_index,
    formal_task_output_index_path,
    replace_formal_task_output_index,
)
from codex_sdlc.core.task_quality import analyze_task_quality
from codex_sdlc.core.task_run import (
    _sync_run_status as sync_task_run_status,
    complete_task_run,
    confirm_task_read,
    initialize_task_run,
    load_task_run_context,
    recover_task_start_transaction,
    require_active_task_run,
    restore_task_run,
)
from codex_sdlc.core.task_evidence import register_task_evidence
from codex_sdlc.services import review_service


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("task", help="推进当前任务，省略编号时自动选择")
    parser.add_argument("first_id", nargs="?", help="任务编号，或需求编号")
    parser.add_argument("second_id", nargs="?", help="可选任务编号")
    parser.add_argument("--done", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--note", default="", help="补充说明")
    parser.add_argument("--file", action="append", default=[], help="涉及文件，可重复传入")
    parser.add_argument("--command", dest="executed_commands", action="append", default=[], help="执行命令，可重复传入")
    parser.add_argument("--verify", action="append", default=[], help="验证结果，可重复传入")
    parser.add_argument("--verification-type", choices=["manual", "visual", "automated"], default="", help="显式验证类型")
    parser.add_argument("--verification-status", choices=["passed", "failed", "blocked"], default="", help="显式验证状态")
    parser.add_argument("--change-report", default="", help="Codex 写好的人读任务变更报告 Markdown 文件")
    parser.add_argument("--test-item", action="append", default=[], help="补充当前任务测试项，可重复传入")
    parser.add_argument("--test-command", action="append", default=[], help="补充当前任务测试命令，可重复传入")
    parser.add_argument("--replace-test-command", action="append", default=[], help="替换当前任务测试命令，可重复传入")
    parser.add_argument("--clear-test-commands", action="store_true", help="清空当前任务测试命令")
    parser.add_argument("--test-script", action="append", default=[], help="补充当前任务可重复测试脚本路径，可重复传入")
    parser.add_argument("--replace-test-script", action="append", default=[], help="替换当前任务可重复测试脚本路径，可重复传入")
    parser.add_argument("--clear-test-scripts", action="store_true", help="清空当前任务可重复测试脚本")
    parser.add_argument("--manual-check", action="append", default=[], help="补充当前任务人工验收点，可重复传入")
    parser.set_defaults(func=run)

    read_confirm_parser = subparsers.add_parser(
        "task-read-confirm", help="确认同一任务线程已经读取当前完整清单"
    )
    read_confirm_parser.add_argument("requirement_id", help="需求编号")
    read_confirm_parser.add_argument("task_id", help="任务编号")
    read_confirm_parser.add_argument(
        "--manifest-sha256", required=True, help="当前读取清单的完整 SHA-256"
    )
    read_confirm_parser.set_defaults(func=run_task_read_confirm)

    run_check_parser = subparsers.add_parser(
        "task-run-check", help="重新校验当前任务运行基线和允许修改范围"
    )
    run_check_parser.add_argument("requirement_id", help="需求编号")
    run_check_parser.add_argument("task_id", help="任务编号")
    run_check_parser.set_defaults(func=run_task_run_check)

    evidence_parser = subparsers.add_parser(
        "task-evidence", help="把测试、人工验收或用户反馈绑定当前任务轮次"
    )
    evidence_parser.add_argument("requirement_id", help="需求编号")
    evidence_parser.add_argument("task_id", help="任务编号")
    evidence_parser.add_argument(
        "--kind",
        required=True,
        choices=["test", "script", "screenshot", "field", "verification", "feedback"],
        help="证据类型",
    )
    evidence_parser.add_argument("--source-file", required=True, help="项目内证据来源文件")
    evidence_parser.add_argument("--sha256", required=True, help="证据来源文件的完整 SHA-256")
    evidence_parser.add_argument(
        "--command",
        dest="evidence_command",
        default="",
        help="实际执行命令或人工操作名称",
    )
    evidence_parser.add_argument("--exit-code", type=int, default=None, help="实际整数退出码")
    evidence_parser.add_argument(
        "--result", choices=["passed", "failed", "blocked"], default="", help="证据结果"
    )
    evidence_parser.add_argument("--test-item", default="", help="task.v2 中原样登记的测试项")
    evidence_parser.set_defaults(func=run_task_evidence)

    done_parser = subparsers.add_parser("task-done", help="完成当前任务并自动验证")
    done_parser.add_argument("first_id", nargs="?", help="任务编号，或需求编号")
    done_parser.add_argument("second_id", nargs="?", help="可选任务编号")
    done_parser.add_argument("--note", default="", help="补充说明")
    done_parser.add_argument("--file", action="append", default=[], help="涉及文件，可重复传入")
    done_parser.add_argument("--command", dest="executed_commands", action="append", default=[], help="执行命令，可重复传入")
    done_parser.add_argument("--verify", action="append", default=[], help="验证结果，可重复传入")
    done_parser.add_argument("--verification-type", choices=["manual", "visual", "automated"], default="", help="显式验证类型")
    done_parser.add_argument("--verification-status", choices=["passed", "failed", "blocked"], default="", help="显式验证状态")
    done_parser.add_argument("--change-report", default="", help="Codex 写好的人读任务变更报告 Markdown 文件")
    done_parser.add_argument("--change-report-template", default="", help="生成人读任务变更报告模板到指定 Markdown 文件，不收口任务")
    done_parser.add_argument("--keep-change-report-source", action="store_true", help="归档后保留 --change-report 源文件")
    done_parser.add_argument("--replace-test-command", action="append", default=[], help="替换当前任务测试命令并用新命令验证，可重复传入")
    done_parser.add_argument("--clear-test-commands", action="store_true", help="清空当前任务测试命令，本次不再运行旧命令")
    done_parser.add_argument("--test-script", action="append", default=[], help="补充当前任务可重复测试脚本路径并执行，可重复传入")
    done_parser.add_argument("--replace-test-script", action="append", default=[], help="替换当前任务可重复测试脚本路径并执行，可重复传入")
    done_parser.add_argument("--clear-test-scripts", action="store_true", help="清空当前任务可重复测试脚本，本次不再运行旧脚本")
    done_parser.add_argument("--await-user-check", action="store_true", help="自动测试通过后进入待用户验收，不标记完成也不提交")
    done_parser.set_defaults(func=run_done, done=True)

    restore_parser = subparsers.add_parser("task-restore", help="根据人工反馈恢复任务并补测试项")
    restore_parser.add_argument("items", nargs="*", help="反馈内容，或 需求编号 任务编号 反馈内容")
    restore_parser.add_argument("--feedback-contract", default="", help="模型整理好的 feedback.v1 JSON 文件")
    restore_parser.set_defaults(func=run_restore)

    pause_parser = subparsers.add_parser("task-pause", help="暂停当前进行中任务并退回待执行")
    pause_parser.add_argument("first_id", nargs="?", help="任务编号，或需求编号")
    pause_parser.add_argument("second_id", nargs="?", help="可选任务编号")
    pause_parser.add_argument("--reason", default="", help="暂停原因")
    pause_parser.set_defaults(func=run_pause)

    fix_parser = subparsers.add_parser("fix", help="为已完成的历史任务插入修复任务")
    fix_parser.add_argument("items", nargs="+", help="反馈内容，或 需求编号 任务编号 反馈内容")
    fix_parser.set_defaults(func=run_fix)

    audit_parser = subparsers.add_parser("audit", help="插入已完成任务质量复查任务")
    audit_parser.add_argument("items", nargs="*", help="可选：需求编号、任务编号范围或复查说明")
    audit_parser.add_argument("--note", default="", help="复查说明")
    audit_parser.set_defaults(func=run_audit)


def run_done(args: argparse.Namespace) -> int:
    args.done = True
    return run(args)


def run_task_read_confirm(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    with project_lock(paths):
        state = derive_state(paths)
        _requirement, task = resolve_task(
            state, str(args.requirement_id), str(args.task_id)
        )
        if task.get("status") != "doing":
            raise SdlcError("只有 doing 状态的当前任务可以确认读取。", exit_code=1)
        result = confirm_task_read(
            paths,
            requirement_id=str(args.requirement_id),
            task_id=str(args.task_id),
            manifest_sha256=str(args.manifest_sha256),
        )
    run = result["run"]
    print(f"任务读取已确认：{args.requirement_id} / {args.task_id}")
    print(f"运行轮次：{int(run['run_number']):04d}")
    print("运行状态：active")
    if result.get("idempotent"):
        print("当前线程已经确认过同一份完整读取清单，本次没有重复写入。")
    # 读取确认已经给出完整结果，沿用 CLI 现有的“命令自行给出下一步”标记，
    # 避免旧状态推荐器在新运行合同已经 active 后又提示 prepare 或 brief。
    if hasattr(args, "command"):
        args.command = "status"
    return 0


def run_task_run_check(args: argparse.Namespace) -> int:
    """提供不收口任务的正式检查入口，开发中可以反复确认当前轮次仍然有效。"""

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    with project_lock(paths):
        state = derive_state(paths)
        _requirement, task = resolve_task(
            state, str(args.requirement_id), str(args.task_id)
        )
        if task.get("status") != "doing":
            raise SdlcError("只有 doing 状态的当前任务可以校验运行基线。", exit_code=1)
        require_active_task_run(
            paths,
            requirement_id=str(args.requirement_id),
            task_id=str(args.task_id),
        )
    print(f"任务运行基线有效：{args.requirement_id} / {args.task_id}")
    print("运行状态：active")
    if hasattr(args, "command"):
        args.command = "status"
    return 0


def run_task_evidence(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    with project_lock(paths):
        record = register_task_evidence(
            paths,
            requirement_id=str(args.requirement_id),
            task_id=str(args.task_id),
            kind=str(args.kind),
            source_file=str(args.source_file),
            source_sha256=str(args.sha256),
            command=str(args.evidence_command),
            exit_code=args.exit_code,
            result=str(args.result),
            test_item=str(args.test_item),
        )
        if record.get("handling") == "formal_change":
            refresh_task_feedback_state(paths)
    print(f"任务证据已登记：{args.requirement_id} / {args.task_id}")
    print(f"证据编号：{record['evidence_id']}；类型：{record['kind']}")
    print(f"来源文件：{record['source_file']}")
    print(f"来源 SHA-256：{record['source_sha256']}")
    if record.get("handling") == "formal_change":
        print(f"反馈已转为正式变更：{record['change_id']}")
        print("运行状态：stale；这条反馈不能直接进入任务完成记录。")
    else:
        print("运行状态：active")
    return 0


def is_requirement_id(value: str | None) -> bool:
    return bool(value and value.startswith("REQ-"))


def is_task_id(value: str | None) -> bool:
    return bool(value and value.startswith("T-"))


def usable_commands(commands: list[str]) -> list[str]:
    return [command for command in commands if command]


def unique_command_list(*groups: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for command in group:
            if command and command not in seen:
                result.append(command)
                seen.add(command)
    return result


def task_goal_text(task: dict[str, object]) -> str:
    """任务展示直接读取正式任务合同，不再从执行包摘要补目标。"""

    return str(
        task.get("goal")
        or task.get("summary")
        or task.get("title")
        or "按任务合同完成实现和验证。"
    ).strip()


def replace_default_task_line(text: str, _goal: str) -> str:
    """保留原始任务条目，避免展示层重新解释正式合同。"""

    return str(text).strip()


def must_do_lines(
    task: dict[str, object], subtask_titles: list[str], goal: str
) -> list[str]:
    """开工提示只展示 task.v2 已有字段，不生成新的业务要求。"""

    items = subtask_titles or [goal, *list(task.get("kind_requirements") or [])]
    clean = [str(item).strip() for item in items if str(item).strip()]
    return [f"- {item}" for item in dict.fromkeys(clean)] or ["- 按任务合同完成当前任务范围"]


def ensure_formal_task_output_index(
    paths: ProjectPaths, requirement: dict[str, object]
) -> None:
    """为正式任务建立空交付物索引，保证失败恢复也能原子创建下一轮。"""

    path = formal_task_output_index_path(paths, requirement)
    if path.exists():
        return
    replace_formal_task_output_index(
        paths,
        requirement,
        empty_formal_task_output_index(str(requirement["requirement_id"])),
    )


def validate_test_command_edit_args(args: argparse.Namespace) -> None:
    replace_commands = getattr(args, "replace_test_command", [])
    append_commands = getattr(args, "test_command", [])
    clear_commands = getattr(args, "clear_test_commands", False)
    if clear_commands and (replace_commands or append_commands):
        raise SdlcError("`--clear-test-commands` 不能和 `--test-command` 或 `--replace-test-command` 同时使用。")
    if replace_commands and append_commands:
        raise SdlcError("`--test-command` 用于追加，`--replace-test-command` 用于替换；一次只能选择一种。")
    replace_scripts = getattr(args, "replace_test_script", [])
    append_scripts = getattr(args, "test_script", [])
    clear_scripts = getattr(args, "clear_test_scripts", False)
    if clear_scripts and (replace_scripts or append_scripts):
        raise SdlcError("`--clear-test-scripts` 不能和 `--test-script` 或 `--replace-test-script` 同时使用。")
    if replace_scripts and append_scripts:
        raise SdlcError("`--test-script` 用于追加，`--replace-test-script` 用于替换；一次只能选择一种。")
    if getattr(args, "await_user_check", False) and getattr(args, "verify", []):
        raise SdlcError("`--await-user-check` 表示等待用户验收，不能同时传 `--verify`。用户验收通过后再单独记录验证结论。")
    verification_type = str(getattr(args, "verification_type", "") or "")
    verification_status = str(getattr(args, "verification_status", "") or "")
    if bool(verification_type) != bool(verification_status):
        raise SdlcError("`--verification-type` 和 `--verification-status` 必须同时传入。")
    if (verification_type or verification_status) and not getattr(args, "verify", []):
        raise SdlcError("结构化验证状态必须同时通过 `--verify` 提供验证记录。")


def test_command_payload_mode(args: argparse.Namespace) -> str | None:
    if getattr(args, "clear_test_commands", False):
        return "clear"
    if getattr(args, "replace_test_command", []):
        return "replace"
    return None


def edited_test_commands(args: argparse.Namespace) -> list[str]:
    if getattr(args, "clear_test_commands", False):
        return []
    replace_commands = getattr(args, "replace_test_command", [])
    if replace_commands:
        return usable_commands([str(command) for command in replace_commands])
    return []


def test_script_payload_mode(args: argparse.Namespace) -> str | None:
    if getattr(args, "clear_test_scripts", False):
        return "clear"
    if getattr(args, "replace_test_script", []):
        return "replace"
    return None


def usable_scripts(scripts: list[str]) -> list[str]:
    return [script for script in scripts if script]


def edited_test_scripts(args: argparse.Namespace) -> list[str]:
    if getattr(args, "clear_test_scripts", False):
        return []
    replace_scripts = getattr(args, "replace_test_script", [])
    if replace_scripts:
        return usable_scripts([str(script) for script in replace_scripts])
    return []


def project_test_commands(state: dict[str, object]) -> list[str]:
    project = state.get("project", {})
    commands = project.get("test_commands", []) if isinstance(project, dict) else []
    return usable_commands([str(command) for command in commands])


def task_test_commands(task: dict[str, object], state: dict[str, object]) -> list[str]:
    commands = usable_commands([str(command) for command in task.get("test_commands", [])])
    return commands or project_test_commands(state)


def task_test_scripts(task: dict[str, object]) -> list[str]:
    return usable_scripts([str(script) for script in task.get("test_scripts", [])])


def is_manual_verification_only_done_update(task: dict[str, object], args: argparse.Namespace) -> bool:
    if not getattr(args, "done", False) or task.get("status") != "done":
        return False
    if getattr(args, "file", []):
        return False
    if getattr(args, "change_report", "") or getattr(args, "change_report_template", ""):
        return False
    if test_command_payload_mode(args) or test_script_payload_mode(args):
        return False
    if getattr(args, "test_script", []):
        return False
    verifications = [str(item) for item in getattr(args, "verify", [])]
    return bool(verifications) and getattr(args, "verification_type", "") in {"manual", "visual"} and getattr(args, "verification_status", "") == "passed"


def parse_task_target(args: argparse.Namespace) -> tuple[str | None, str | None, bool]:
    first_id = getattr(args, "first_id", None)
    second_id = getattr(args, "second_id", None)
    if not first_id:
        return None, None, False
    if second_id:
        return first_id, second_id, True
    if is_requirement_id(first_id):
        return first_id, None, False
    return None, first_id, True


def task_key(requirement: dict[str, object], task: dict[str, object]) -> tuple[str, str]:
    return str(requirement["requirement_id"]), str(task["task_id"])


def candidate_line(requirement: dict[str, object], task: dict[str, object]) -> str:
    return f"{requirement['requirement_id']} / {task['task_id']}：{task['title']}"


def dependency_ready(requirement: dict[str, object], task: dict[str, object]) -> bool:
    task_map = {item["task_id"]: item for item in requirement["tasks"]}  # type: ignore[index]
    return all(task_map[dependency]["status"] in {"done", "closed"} for dependency in task["depends_on"])  # type: ignore[index]


def startable_tasks(state: dict[str, object], requirement_id: str | None = None) -> list[tuple[dict[str, object], dict[str, object]]]:
    candidates: list[tuple[dict[str, object], dict[str, object]]] = []
    for requirement in state["active_requirements"]:  # type: ignore[index]
        if requirement_id and requirement["requirement_id"] != requirement_id:
            continue
        for task in requirement["tasks"]:
            if task["status"] in {"todo", "test_failed"} and dependency_ready(requirement, task):
                candidates.append((requirement, task))
    return candidates


def ready_for_user_check_tasks(
    state: dict[str, object],
    requirement_id: str | None = None,
) -> list[tuple[dict[str, object], dict[str, object]]]:
    candidates: list[tuple[dict[str, object], dict[str, object]]] = []
    for requirement in state["active_requirements"]:  # type: ignore[index]
        if requirement_id and requirement["requirement_id"] != requirement_id:
            continue
        for task in requirement["tasks"]:
            if task["status"] == "ready_for_user_check" and dependency_ready(requirement, task):
                candidates.append((requirement, task))
    return candidates


def user_check_task_message(
    candidates: list[tuple[dict[str, object], dict[str, object]]],
) -> str:
    requirement, task = candidates[0]
    return (
        f"{requirement['requirement_id']} / {task['task_id']} 正在等待用户验收，不能用 `$sdlc-task` 重新拉回开发。\n"
        f"- 验收通过：执行 `$sdlc-task-done {requirement['requirement_id']} {task['task_id']}` 记录验收并收口。\n"
        f"- 验收发现问题：执行 `$sdlc-task-restore {requirement['requirement_id']} {task['task_id']} 反馈内容` 恢复当前任务。"
    )


def recent_task_from_events(
    state: dict[str, object],
    candidates: list[tuple[dict[str, object], dict[str, object]]],
    *,
    sources: set[str] | None = None,
) -> tuple[dict[str, object], dict[str, object]] | None:
    candidate_map = {task_key(requirement, task): (requirement, task) for requirement, task in candidates}
    for event in reversed(state["events"]):  # type: ignore[index]
        if sources and event.get("source") not in sources:
            continue
        requirement_id = event.get("requirement_id")
        task_id = event.get("task_id")
        if not requirement_id or not task_id:
            continue
        match = candidate_map.get((str(requirement_id), str(task_id)))
        if match:
            return match
    return None


def select_single_candidate(
    candidates: list[tuple[dict[str, object], dict[str, object]]],
    *,
    many_message: str,
    none_message: str,
) -> tuple[dict[str, object], dict[str, object]]:
    if not candidates:
        raise SdlcError(none_message, exit_code=1)
    if len(candidates) > 1:
        lines = [many_message]
        lines.extend(f"- {candidate_line(requirement, task)}" for requirement, task in candidates)
        raise SdlcError("\n".join(lines), exit_code=1)
    return candidates[0]


def select_task_to_start(state: dict[str, object], args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    requirement_id, task_id, explicit_task = parse_task_target(args)
    if explicit_task and task_id:
        return resolve_task(state, requirement_id, task_id)

    candidates = startable_tasks(state, requirement_id)
    if not candidates:
        user_check_candidates = ready_for_user_check_tasks(state, requirement_id)
        if user_check_candidates:
            raise SdlcError(user_check_task_message(user_check_candidates), exit_code=1)
    if requirement_id:
        return select_single_candidate(
            candidates,
            many_message=f"{requirement_id} 有多个可执行任务，请指定任务编号：",
            none_message=f"{requirement_id} 当前没有可执行任务。",
        )

    recent = recent_task_from_events(state, candidates, sources={"sdlc-task", "sdlc-plan"})
    if recent:
        return recent
    return select_single_candidate(
        candidates,
        many_message="多个可执行任务，请指定要开始哪一个：",
        none_message="当前没有可执行任务。",
    )


def select_task_to_finish(state: dict[str, object], args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    requirement_id, task_id, explicit_task = parse_task_target(args)
    if explicit_task and task_id:
        return resolve_task(state, requirement_id, task_id)

    statuses = {"doing", "ready_for_user_check", "test_failed"}
    candidates = [
        (requirement, task)
        for requirement in state["requirements"].values()  # type: ignore[index]
        for task in requirement["tasks"]
        if (not requirement_id or requirement["requirement_id"] == requirement_id) and task["status"] in statuses
    ]
    recent = recent_task_from_events(state, candidates, sources={"sdlc-task"})
    if recent:
        return recent
    return select_single_candidate(
        candidates,
        many_message="多个进行中或待验证任务都可能是当前任务，请指定要完成哪一个：",
        none_message="当前没有正在进行或待验证的任务，请先使用 `$sdlc-task`。",
    )


def select_task_to_pause(state: dict[str, object], args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    requirement_id, task_id, explicit_task = parse_task_target(args)
    if explicit_task and task_id:
        requirement, task = resolve_task(state, requirement_id, task_id)
        if task["status"] != "doing":
            raise SdlcError(f"{requirement['requirement_id']} / {task['task_id']} 当前不是 doing，不能暂停。", exit_code=1)
        return requirement, task

    candidates = [
        (requirement, task)
        for requirement in state["requirements"].values()  # type: ignore[index]
        for task in requirement["tasks"]
        if (not requirement_id or requirement["requirement_id"] == requirement_id) and task["status"] == "doing"
    ]
    recent = recent_task_from_events(state, candidates, sources={"sdlc-task"})
    if recent:
        return recent
    return select_single_candidate(
        candidates,
        many_message="多个进行中任务都可能要暂停，请指定需求编号和任务编号：",
        none_message="当前没有正在进行的任务可暂停。",
    )


def parse_restore_target(items: list[str]) -> tuple[str | None, str | None, str]:
    if len(items) >= 3 and is_requirement_id(items[0]) and is_task_id(items[1]):
        return items[0], items[1], " ".join(items[2:]).strip()
    if len(items) >= 2 and is_task_id(items[0]):
        return None, items[0], " ".join(items[1:]).strip()
    return None, None, " ".join(items).strip()


def select_task_to_restore(
    state: dict[str, object],
    requirement_id: str | None,
    task_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    if task_id:
        return resolve_task(state, requirement_id, task_id)

    candidates = [
        (requirement, task)
        for requirement in state["requirements"].values()  # type: ignore[index]
        for task in requirement["tasks"]
        if task["status"] in {"done", "ready_for_user_check", "test_failed"}
        or (task["status"] == "doing" and isinstance(task.get("task_contract"), dict))
    ]
    recent = recent_task_from_events(state, candidates, sources={"sdlc-task"})
    if recent:
        return recent
    return select_single_candidate(
        candidates,
        many_message="多个任务都可能要恢复，请指定需求编号和任务编号：",
        none_message="没有找到可恢复的任务。",
    )


def parse_fix_target(items: list[str]) -> tuple[str | None, str | None, str]:
    requirement_id, task_id, feedback = parse_restore_target(items)
    if task_id:
        return requirement_id, task_id, feedback

    text = " ".join(items).strip()
    requirement_match = next(iter(re.finditer(r"REQ-\d+", text)), None)
    task_match = next(iter(re.finditer(r"T-\d+", text)), None)
    if task_match:
        return (
            requirement_match.group(0) if requirement_match else None,
            task_match.group(0),
            text,
        )
    return None, None, text


def short_feedback_text(text: str, length: int = 28) -> str:
    clean = " ".join(text.split()).strip()
    if len(clean) <= length:
        return clean
    return clean[: length - 1] + "…"


def task_plan_snapshot(task: dict[str, object]) -> dict[str, object]:
    snapshot = {
        "task_id": task["task_id"],
        "source_task_id": task.get("source_task_id", ""),
        "subtasks": list(task.get("subtasks", [])),
        "title": task["title"],
        "summary": task.get("summary", task["title"]),
        "status": task.get("status", "todo"),
        "depends_on": list(task.get("depends_on", [])),
        "changed_files": list(task.get("changed_files", [])),
        "commands": list(task.get("commands", [])),
        "test_items": list(task.get("test_items", [])),
        "test_commands": list(task.get("test_commands", [])),
        "test_scripts": list(task.get("test_scripts", [])),
        "manual_checks": list(task.get("manual_checks", [])),
        "verifications": list(task.get("verifications", [])),
        "note": task.get("note", ""),
    }
    # 任务重排和插入修复任务时必须原样保留结构化合同，不能退回到标题推断。
    for field in (
        "context_files",
        "output_files",
        "related_files",
        "business_rules",
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
    ):
        if field in task:
            value = task[field]
            snapshot[field] = list(value) if isinstance(value, list) else value
    return snapshot


def task_index(requirement: dict[str, object], task_id: str) -> int:
    for index, task in enumerate(requirement["tasks"]):  # type: ignore[index]
        if task["task_id"] == task_id:
            return index
    return -1


TASK_PROGRESS_STATUSES = {"doing", "done", "ready_for_user_check", "test_failed"}
CURRENT_TASK_PROTECTION_STATUSES = {"doing", "ready_for_user_check", "test_failed"}


def changes_that_block_task_progress(
    changes: list[dict[str, object]],
    task: dict[str, object],
) -> list[dict[str, object]]:
    if task.get("status") not in CURRENT_TASK_PROTECTION_STATUSES:
        return changes
    task_id = str(task.get("task_id", ""))
    # 当前任务已经进入收口链路时，只有明确绑定当前任务的变更才阻断它，其它变更排到后面处理。
    return [
        change
        for change in changes
        if task_id in {str(item) for item in change.get("changed_task_ids", []) if str(item).strip()}
    ]


def current_task_change_block_message(
    requirement: dict[str, object],
    task: dict[str, object],
    change: dict[str, object],
) -> str:
    requirement_id = str(requirement["requirement_id"])
    task_id = str(task["task_id"])
    return "\n".join(
        [
            f"{change['change_id']} 明确绑定了当前任务 {task_id}，不能静默继续收口。",
            "请先按正式分流处理：",
            f"- 当前任务目标要补：$sdlc-plan-amend-task {requirement_id} {task_id}",
            f"- 当前任务验收或测试不过：$sdlc-task-restore {requirement_id} {task_id} 反馈内容",
            f"- 需求目标整体变了：$sdlc-change {requirement_id} 变更内容",
        ]
    )


def latest_done_event_index(
    state: dict[str, object],
    requirement: dict[str, object],
    task: dict[str, object],
) -> int | None:
    requirement_id = str(requirement["requirement_id"])
    task_id = str(task["task_id"])
    for index, event in reversed(list(enumerate(state["events"]))):  # type: ignore[index]
        if event.get("event_type") != "task_updated" or event.get("source") != "sdlc-task":
            continue
        if str(event.get("requirement_id")) != requirement_id or str(event.get("task_id")) != task_id:
            continue
        payload = event.get("payload", {})
        if isinstance(payload, dict) and payload.get("status") == "done":
            return index
    return None


def later_progress_exists(
    state: dict[str, object],
    requirement: dict[str, object],
    task: dict[str, object],
) -> bool:
    latest_done_index = latest_done_event_index(state, requirement, task)
    if latest_done_index is not None:
        requirement_id = str(requirement["requirement_id"])
        task_id = str(task["task_id"])
        for event in list(state["events"])[latest_done_index + 1 :]:  # type: ignore[index]
            if event.get("event_type") != "task_updated" or event.get("source") != "sdlc-task":
                continue
            if str(event.get("requirement_id")) != requirement_id:
                continue
            if str(event.get("task_id")) == task_id:
                continue
            payload = event.get("payload", {})
            if isinstance(payload, dict) and payload.get("status") in TASK_PROGRESS_STATUSES:
                return True
        return False

    index = task_index(requirement, str(task["task_id"]))
    if index < 0:
        return False
    later_tasks = list(requirement["tasks"])[index + 1 :]  # type: ignore[index]
    return any(item["status"] in TASK_PROGRESS_STATUSES for item in later_tasks)


def impacted_tasks_after(requirement: dict[str, object], source_task: dict[str, object]) -> list[dict[str, object]]:
    index = task_index(requirement, str(source_task["task_id"]))
    if index < 0:
        return []
    return [
        task
        for task in list(requirement["tasks"])[index + 1 :]  # type: ignore[index]
        if task["status"] != "closed"
    ]


def build_impacted_summary(tasks: list[dict[str, object]]) -> str:
    if not tasks:
        return "无"
    return "、".join(f"{task['task_id']}[{task['status']}]" for task in tasks)


def task_id_number(task_id: str) -> int | None:
    match = re.fullmatch(r"T-(\d+)", task_id)
    if not match:
        return None
    return int(match.group(1))


def task_id_range(start_id: str, end_id: str) -> list[str]:
    start_number = task_id_number(start_id)
    end_number = task_id_number(end_id)
    if start_number is None or end_number is None:
        return [start_id, end_id]
    if start_number > end_number:
        start_number, end_number = end_number, start_number
    width = max(len(start_id.split("-", 1)[1]), len(end_id.split("-", 1)[1]))
    return [f"T-{number:0{width}d}" for number in range(start_number, end_number + 1)]


def parse_audit_target(items: list[str], note: str) -> tuple[str | None, list[str], str]:
    text = " ".join(items).strip()
    requirement_match = re.search(r"REQ-\d+", text)
    task_ids = re.findall(r"T-\d+", text)
    clean_note = note.strip()
    if not clean_note:
        clean_note = re.sub(r"REQ-\d+", "", text)
        clean_note = re.sub(r"T-\d+", "", clean_note)
        clean_note = re.sub(r"(^|\s+)(到|至|~|-)(\s+|$)", " ", clean_note)
        clean_note = re.sub(r"\s+", " ", clean_note).strip(" ：:，,")
    if len(task_ids) == 2:
        task_ids = task_id_range(task_ids[0], task_ids[1])
    return (
        requirement_match.group(0) if requirement_match else None,
        task_ids,
        clean_note,
    )


def select_requirement_for_audit(state: dict[str, object], requirement_id: str | None) -> dict[str, object]:
    if requirement_id:
        for requirement in state["requirements"].values():  # type: ignore[index]
            if requirement["requirement_id"] == requirement_id:
                return requirement
        raise SdlcError(f"没有找到需求 `{requirement_id}`。", exit_code=1)
    active = list(state["active_requirements"])  # type: ignore[index]
    if len(active) == 1:
        return active[0]
    if active:
        lines = ["有多个活跃需求，请指定要复查哪一个："]
        lines.extend(f"- {item['requirement_id']}：{item['title']}" for item in active)
        raise SdlcError("\n".join(lines), exit_code=1)
    raise SdlcError("当前没有活跃需求，不能插入复查任务。", exit_code=1)


def select_audited_tasks(requirement: dict[str, object], task_ids: list[str]) -> list[dict[str, object]]:
    task_map = {task["task_id"]: task for task in requirement["tasks"]}  # type: ignore[index]
    if task_ids:
        missing = [task_id for task_id in task_ids if task_id not in task_map]
        if missing:
            raise SdlcError(f"这些任务不存在，不能复查：{', '.join(missing)}", exit_code=1)
        tasks = [task_map[task_id] for task_id in task_ids]
    else:
        tasks = [task for task in requirement["tasks"] if task["status"] == "done"]  # type: ignore[index]
    if not tasks:
        raise SdlcError("没有找到已完成任务，不能插入质量复查任务。", exit_code=1)
    not_done = [task for task in tasks if task["status"] != "done"]
    if not_done:
        lines = ["质量复查任务只复查已完成任务，下面这些任务还不是 done："]
        lines.extend(f"- {task['task_id']} [{task['status']}] {task['title']}" for task in not_done)
        raise SdlcError("\n".join(lines), exit_code=1)
    return tasks


def audit_scope_text(tasks: list[dict[str, object]]) -> str:
    ids = [str(task["task_id"]) for task in tasks]
    if not ids:
        return "已完成任务"
    numbers = [task_id_number(task_id) for task_id in ids]
    if all(number is not None for number in numbers) and numbers == list(range(numbers[0], numbers[0] + len(numbers))):  # type: ignore[arg-type]
        return f"{ids[0]} 到 {ids[-1]}"
    return "、".join(ids)


def find_existing_audit_task(
    requirement: dict[str, object],
    audited_tasks: list[dict[str, object]],
) -> dict[str, object] | None:
    matches: list[dict[str, object]] = []
    for task in requirement["tasks"]:  # type: ignore[index]
        if task["status"] in {"done", "closed"}:
            continue
        if str(task.get("source_task_id", "")) == "AUDIT":
            matches.append(task)
    if len(matches) > 1:
        lines = ["发现多个可能的质量复查任务，请先指定或关闭多余任务："]
        lines.extend(f"- {task['task_id']} [{task['status']}] {task['title']}" for task in matches)
        raise SdlcError("\n".join(lines), exit_code=1)
    return matches[0] if matches else None


def build_audit_task(
    requirement: dict[str, object],
    audited_tasks: list[dict[str, object]],
    note: str,
    paused_tasks: list[dict[str, object]],
    existing_task: dict[str, object] | None = None,
) -> dict[str, object]:
    existing_ids = [str(task["task_id"]) for task in requirement["tasks"]]  # type: ignore[index]
    audit_task_id = str(existing_task["task_id"]) if existing_task else next_number(existing_ids, "T")
    scope = audit_scope_text(audited_tasks)
    clean_note = note or "实现质量、验证记录和代码提交边界"
    paused_summary = build_impacted_summary(paused_tasks)
    def merged_contract_values(field: str) -> list[object]:
        return list(
            dict.fromkeys(
                item
                for task in audited_tasks
                for item in (task.get(field) or [])
            )
        )

    model_tiers = {str(task.get("model_tier") or "medium") for task in audited_tasks}
    model_tier = "high" if "high" in model_tiers else ("medium" if "medium" in model_tiers else "low")
    return {
        "task_id": audit_task_id,
        "source_task_id": "AUDIT",
        "subtasks": [],
        "title": f"复查 {scope}：{short_feedback_text(clean_note, 32)}",
        "summary": f"复查 {scope} 的实现质量、验证记录和代码提交边界。",
        "status": "todo",
        "depends_on": [str(task["task_id"]) for task in audited_tasks],
        "changed_files": [],
        "commands": [],
        "test_items": [
            f"复查范围：{scope}",
            "检查实现是否符合需求和技术方案",
            "检查验证记录是否可信",
            "检查 Git 提交边界是否干净",
            "判断后续任务是否受影响",
        ],
        "test_commands": [],
        "test_scripts": [],
        "manual_checks": [
            f"人工确认 {scope} 的复查结论已写清",
            "人工确认后续处理建议已明确",
        ],
        "verifications": [],
        "note": "\n".join(
            [
                f"复查范围：{scope}",
                f"复查说明：{clean_note}",
                f"自动暂停任务：{paused_summary}",
                "处理规则：本任务只做质量复查，不直接修业务代码；发现明确问题后再分流到 fix、restore、change 或 design。",
            ]
        ),
        "coverage_points": merged_contract_values("coverage_points"),
        "coverage_change_ids": merged_contract_values("coverage_change_ids"),
        "coverage_acceptance": merged_contract_values("coverage_acceptance"),
        "coverage_tests": merged_contract_values("coverage_tests"),
        "formal_requirement_refs": merged_contract_values("formal_requirement_refs"),
        "formal_design_refs": merged_contract_values("formal_design_refs"),
        "formal_test_refs": merged_contract_values("formal_test_refs"),
        "out_of_scope": merged_contract_values("out_of_scope"),
        "business_rules": merged_contract_values("business_rules"),
        "feedback_contract_version": "feedback.v1",
        "feedback_state": "none",
        "acceptance_feedback": [],
        "task_kind": "audit",
        "model_tier": model_tier,
    }


def insert_audit_task_and_gate_future_tasks(
    requirement: dict[str, object],
    audit_task: dict[str, object],
) -> list[dict[str, object]]:
    snapshots = [
        task_plan_snapshot(task)
        for task in requirement["tasks"]  # type: ignore[index]
        if task["task_id"] != audit_task["task_id"]
    ]
    finished = [task for task in snapshots if task["status"] in {"done", "closed"}]
    open_tasks = [task for task in snapshots if task["status"] not in {"done", "closed"}]
    for task in open_tasks:
        dependencies = list(task.get("depends_on", []))
        if audit_task["task_id"] not in dependencies:
            dependencies.append(audit_task["task_id"])
        task["depends_on"] = dependencies
    return finished + [audit_task] + open_tasks


def pause_doing_tasks_for_audit(
    requirement: dict[str, object],
    reason: str,
    *,
    skip_task_id: str = "",
) -> list[dict[str, object]]:
    paused: list[dict[str, object]] = []
    for task in requirement["tasks"]:  # type: ignore[index]
        if task["task_id"] == skip_task_id:
            continue
        if task["status"] != "doing":
            continue
        note = str(task.get("note", ""))
        pause_note = f"暂停说明：{reason}"
        if pause_note not in note:
            task["note"] = note + ("\n" if note else "") + pause_note
        task["status"] = "todo"
        paused.append(task)
    return paused


def build_fix_task(
    requirement: dict[str, object],
    source_task: dict[str, object],
    feedback: str,
    impacted_tasks: list[dict[str, object]],
) -> dict[str, object]:
    existing_ids = [str(task["task_id"]) for task in requirement["tasks"]]  # type: ignore[index]
    fix_task_id = next_number(existing_ids, "T")
    source_task_id = str(source_task["task_id"])
    impacted_summary = build_impacted_summary(impacted_tasks)
    return {
        "task_id": fix_task_id,
        "source_task_id": f"FIX-{source_task_id}",
        "subtasks": [],
        "title": f"修复 {source_task_id}：{short_feedback_text(feedback)}",
        "summary": f"修复已完成任务 {source_task_id} 后发现的问题：{feedback}",
        "status": "todo",
        "depends_on": [source_task_id],
        "changed_files": [],
        "commands": [],
        "test_items": [
            f"复现并修复反馈：{feedback}",
            f"回归来源任务 {source_task_id}：{source_task['title']}",
        ],
        "test_commands": list(source_task.get("test_commands", [])),
        "test_scripts": list(source_task.get("test_scripts", [])),
        "manual_checks": [
            f"人工确认 {source_task_id} 的反馈已解决：{feedback}",
            f"人工复查受影响任务：{impacted_summary}",
        ],
        "verifications": [],
        "note": "\n".join(
            [
                f"修复来源任务：{requirement['requirement_id']} / {source_task_id}",
                f"反馈：{feedback}",
                f"影响复查：{impacted_summary}",
                "处理规则：不改来源任务历史状态；本任务完成后用新的验证记录和新的 Git 提交收口。",
            ]
        ),
        "coverage_points": list(source_task.get("coverage_points") or []),
        "coverage_change_ids": list(source_task.get("coverage_change_ids") or []),
        "coverage_acceptance": list(source_task.get("coverage_acceptance") or []),
        "coverage_tests": list(source_task.get("coverage_tests") or []),
        "formal_requirement_refs": list(source_task.get("formal_requirement_refs") or []),
        "formal_design_refs": list(source_task.get("formal_design_refs") or []),
        "formal_test_refs": list(source_task.get("formal_test_refs") or []),
        "out_of_scope": list(source_task.get("out_of_scope") or []),
        "business_rules": list(source_task.get("business_rules") or []),
        "feedback_contract_version": "feedback.v1",
        "feedback_state": "none",
        "acceptance_feedback": [],
        "task_kind": "fix",
        "model_tier": str(source_task.get("model_tier") or "medium"),
    }


def insert_fix_task_and_gate_future_tasks(
    requirement: dict[str, object],
    source_task: dict[str, object],
    fix_task: dict[str, object],
) -> list[dict[str, object]]:
    source_index = task_index(requirement, str(source_task["task_id"]))
    tasks = [task_plan_snapshot(task) for task in requirement["tasks"]]  # type: ignore[index]
    insert_at = source_index + 1 if source_index >= 0 else len(tasks)
    tasks.insert(insert_at, fix_task)

    for task in tasks[insert_at + 1 :]:
        if task["status"] in {"todo", "test_failed", "ready_for_user_check"}:
            dependencies = list(task.get("depends_on", []))
            if fix_task["task_id"] not in dependencies:
                dependencies.append(fix_task["task_id"])
            task["depends_on"] = dependencies
        if task["status"] in {"done", "doing"}:
            note = str(task.get("note", ""))
            impact_note = f"受 {fix_task['task_id']} 可能影响，修复完成后需要复查。"
            if impact_note not in note:
                task["note"] = note + ("\n" if note else "") + impact_note
    return tasks


def run_test_commands(root: Path, commands: list[str]) -> tuple[bool, list[str]]:
    outputs: list[str] = []
    for command in commands:
        result = subprocess.run(
            command,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
        )
        output = "\n".join(item for item in [result.stdout.strip(), result.stderr.strip()] if item)
        if len(output) > 1200:
            output = output[:1200] + "\n...输出已截断..."
        outputs.append(f"$ {command}\n{output or '无输出'}")
        if result.returncode != 0:
            return False, outputs
    return True, outputs


def run_test_scripts(root: Path, scripts: list[str]) -> tuple[bool, list[str]]:
    outputs: list[str] = []
    for script in scripts:
        script_path = Path(script)
        resolved_path = script_path if script_path.is_absolute() else root / script_path
        quoted_path = shlex.quote(str(resolved_path))
        if resolved_path.exists() and os.access(resolved_path, os.X_OK):
            command = quoted_path
        elif resolved_path.suffix in {".mjs", ".js", ".cjs"}:
            command = f"node {quoted_path}"
        elif resolved_path.suffix == ".py":
            command = f"python3 {quoted_path}"
        elif resolved_path.suffix == ".sh":
            command = f"bash {quoted_path}"
        else:
            command = quoted_path
        result = subprocess.run(
            command,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            check=False,
        )
        output = "\n".join(item for item in [result.stdout.strip(), result.stderr.strip()] if item)
        if len(output) > 1200:
            output = output[:1200] + "\n...输出已截断..."
        outputs.append(f"$ {script}\n{output or '无输出'}")
        if result.returncode != 0:
            return False, outputs
    return True, outputs


PROCESS_ARTIFACT_PREFIXES = (
    ".codex-sdlc/",
    ".codex/hooks",
    ".codex/rules",
)


def normalize_changed_file(path: str) -> str:
    clean = path.strip()
    if clean.startswith('"') and clean.endswith('"'):
        clean = clean[1:-1]
    if " -> " in clean:
        clean = clean.split(" -> ", 1)[-1].strip()
    return clean


def strip_current_dir_prefix(path: str) -> str:
    clean = path
    while clean.startswith("./"):
        clean = clean[2:]
    return clean


def normalize_project_path(root: Path, path: str) -> str:
    clean = normalize_changed_file(path)
    if not clean:
        return ""
    candidate = Path(clean).expanduser()
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return Path(strip_current_dir_prefix(clean)).as_posix()


def requirement_mjs_test_script_path(root: Path, requirement: dict[str, object], path: str) -> str:
    relative_path = normalize_project_path(root, path)
    parts = relative_path.split("/")
    if len(parts) != 5:
        return ""
    expected_prefix = [".codex-sdlc", "requirements", str(requirement["folder_name"]), "tests"]
    if parts[:4] != expected_prefix:
        return ""
    if Path(parts[-1]).suffix != ".mjs":
        return ""
    return "/".join(parts)


def iso_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def task_current_round_started_at(state: dict[str, object], requirement: dict[str, object], task: dict[str, object]) -> float | None:
    for event in reversed(state.get("events", [])):  # type: ignore[union-attr]
        if event.get("event_type") != "task_updated":
            continue
        if event.get("requirement_id") != requirement["requirement_id"] or event.get("task_id") != task["task_id"]:
            continue
        payload = event.get("payload", {})
        if isinstance(payload, dict) and payload.get("status") == "doing":
            return iso_timestamp(event.get("created_at"))
    return None


def changed_requirement_mjs_test_scripts(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    state: dict[str, object],
    changed_files: list[str],
) -> list[str]:
    scripts: list[str] = []
    candidate_files = [
        *[str(item) for item in changed_files],
        *[str(item) for item in state.get("git_changed_files", [])],  # type: ignore[union-attr]
    ]
    for item in candidate_files:
        script_path = requirement_mjs_test_script_path(paths.root, requirement, item)
        if script_path:
            scripts.append(script_path)

    started_at = task_current_round_started_at(state, requirement, task)
    if started_at is not None:
        tests_dir = paths.requirements_dir / str(requirement["folder_name"]) / "tests"
        if tests_dir.exists():
            for script_file in tests_dir.glob("*.mjs"):
                try:
                    modified_at = script_file.stat().st_mtime
                except OSError:
                    continue
                # `.codex-sdlc/` 默认可能被 Git 忽略，所以这里用当前任务开工时间兜底识别
                # 本轮新增或修改的需求包专项脚本，避免只靠 `git status` 漏掉收口门禁。
                if modified_at > started_at:
                    script_path = requirement_mjs_test_script_path(paths.root, requirement, str(script_file))
                    if script_path:
                        scripts.append(script_path)
    return unique_command_list(scripts)


def missing_registered_requirement_mjs_scripts(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    state: dict[str, object],
    *,
    changed_files: list[str],
    test_scripts: list[str],
) -> list[str]:
    changed_scripts = changed_requirement_mjs_test_scripts(paths, requirement, task, state, changed_files)
    registered_scripts = {
        normalize_project_path(paths.root, str(script))
        for script in test_scripts
        if str(script).strip()
    }
    return [script for script in changed_scripts if script not in registered_scripts]


def ensure_changed_requirement_mjs_scripts_registered(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    state: dict[str, object],
    *,
    changed_files: list[str],
    test_scripts: list[str],
) -> None:
    missing_scripts = missing_registered_requirement_mjs_scripts(
        paths,
        requirement,
        task,
        state,
        changed_files=changed_files,
        test_scripts=test_scripts,
    )
    if not missing_scripts:
        return
    lines = [
        f"检测到本任务使用了测试脚本 {missing_scripts[0]}，但它还没有进入当前任务测试契约。",
        "请通过 `--test-script` 登记后再收口，例如：",
    ]
    lines.extend(
        f"`codex-sdlc task-done {requirement['requirement_id']} {task['task_id']} --test-script {script}`"
        for script in missing_scripts
    )
    raise SdlcError("\n".join(lines), exit_code=1)


def is_process_artifact(path: str) -> bool:
    clean = strip_current_dir_prefix(normalize_changed_file(path))
    return any(clean == prefix.rstrip("/") or clean.startswith(prefix) for prefix in PROCESS_ARTIFACT_PREFIXES)


def changed_file_exists_in_git_status(root: Path, path: str) -> bool:
    clean = normalize_changed_file(path)
    if not clean or Path(clean).is_absolute() or is_process_artifact(clean):
        return False
    result = run_git(["status", "--short", "--", clean], root)
    return result.returncode == 0 and bool(result.stdout.strip())


def short_commit_hash(root: Path) -> str:
    result = run_git(["rev-parse", "--short", "HEAD"], root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def clean_commit_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def trim_commit_subject(text: str, max_length: int = 54) -> str:
    clean = clean_commit_text(text)
    if len(clean) <= max_length:
        return clean
    return clean[: max_length - 1].rstrip(" ：:，,。") + "…"


def task_business_phrases(task: dict[str, object]) -> list[str]:
    explicit = task.get("commit_body_items", [])
    return [clean_commit_text(item) for item in explicit if clean_commit_text(item)]


def build_commit_subject(requirement: dict[str, object], task: dict[str, object]) -> str:
    scope = clean_commit_text(task.get("commit_subject") or task.get("title") or task.get("task_id") or "当前任务")
    scope = trim_commit_subject(scope)
    return scope


def commit_body_section(title: str, lines: list[str]) -> list[str]:
    clean_lines = [clean_commit_text(item) for item in lines if clean_commit_text(item)]
    if not clean_lines:
        return []
    return [title, *[f"- {item}" for item in clean_lines], ""]


def build_commit_body(requirement: dict[str, object], task: dict[str, object], safe_files: list[str]) -> str:
    requirement_title = clean_commit_text(requirement.get("title", ""))
    task_title = clean_commit_text(task.get("title", ""))
    implementation_lines = task_business_phrases(task)[:6]
    if task.get("summary"):
        implementation_lines.insert(0, clean_commit_text(task.get("summary", "")))
    verification_lines = [
        clean_commit_text(item.get("summary", ""))
        for item in task.get("verifications", [])
        if isinstance(item, dict)
    ]
    command_lines = [clean_commit_text(item) for item in task.get("commands", [])]

    body_lines = [
        f"需求：{requirement_title or requirement.get('requirement_id', '')}",
        f"SDLC：{requirement.get('requirement_id', '')} / {task.get('task_id', '')} {task_title}".rstrip(),
        "",
    ]
    body_lines.extend(commit_body_section("实现范围：", implementation_lines))
    body_lines.extend(commit_body_section("涉及文件：", safe_files))
    body_lines.extend(commit_body_section("验证记录：", verification_lines))
    body_lines.extend(commit_body_section("执行命令：", command_lines[:8]))
    return "\n".join(body_lines).strip()


def auto_commit_task_changes(root: Path, requirement: dict[str, object], task: dict[str, object], changed_files: list[str]) -> dict[str, str]:
    git_root = find_git_root(root)
    if git_root is None:
        return {"status": "skipped", "message": "当前目录不是 Git 仓库，无法自动提交。"}

    explicit_files = [normalize_changed_file(item) for item in changed_files if normalize_changed_file(item)]
    if not explicit_files:
        explicit_files = current_git_changed_files(root)

    safe_files: list[str] = []
    skipped_files: list[str] = []
    seen: set[str] = set()
    for file_path in explicit_files:
        clean = normalize_changed_file(file_path)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        if is_process_artifact(clean):
            skipped_files.append(clean)
            continue
        if changed_file_exists_in_git_status(git_root, clean):
            safe_files.append(clean)

    if not safe_files:
        if skipped_files:
            return {
                "status": "skipped",
                "message": "只有流程产物或无需提交的文件，已跳过自动提交。",
            }
        return {"status": "none", "message": "当前任务没有可提交的代码改动。"}

    add_result = run_git(["add", "--", *safe_files], git_root)
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip()
        return {"status": "failed", "message": f"git add 失败：{detail}"}

    message = build_commit_subject(requirement, task)
    body = build_commit_body(requirement, task, safe_files)
    commit_args = ["commit", "-m", message]
    if body:
        commit_args.extend(["-m", body])
    commit_result = run_git(commit_args, git_root)
    if commit_result.returncode != 0:
        detail = (commit_result.stderr or commit_result.stdout).strip()
        return {"status": "failed", "message": f"git commit 失败：{detail}"}

    commit_hash = short_commit_hash(git_root)
    files_text = "、".join(safe_files)
    hash_text = f"，提交哈希：{commit_hash}" if commit_hash else ""
    return {
        "status": "committed",
        "message": f"已提交 {len(safe_files)} 个文件：{files_text}{hash_text}；提交信息：{message}",
        "commit_hash": commit_hash,
        "commit_message": message,
    }


def requirement_package_dir(paths: ProjectPaths, requirement: dict[str, object]) -> Path:
    return paths.requirements_dir / str(requirement["folder_name"])


def task_change_brief_path(paths: ProjectPaths, requirement: dict[str, object], task: dict[str, object]) -> Path:
    return requirement_package_dir(paths, requirement) / "task-change-reports" / f"{task['task_id']}.md"


def collect_task_change_files(root: Path, changed_files: list[str], *, fallback_to_git: bool = True) -> list[str]:
    candidates = [normalize_changed_file(item) for item in changed_files if normalize_changed_file(item)]
    if not candidates and fallback_to_git:
        candidates = current_git_changed_files(root)
    result: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        clean = strip_current_dir_prefix(normalize_changed_file(item))
        if not clean or clean in seen or Path(clean).is_absolute() or is_process_artifact(clean):
            continue
        seen.add(clean)
        result.append(clean)
    return result


def task_change_candidate_files(root: Path, task: dict[str, object], changed_files: list[str]) -> list[str]:
    explicit_files = collect_task_change_files(root, changed_files, fallback_to_git=False)
    if explicit_files:
        return explicit_files
    task_files = collect_task_change_files(root, [str(item) for item in task.get("changed_files", [])], fallback_to_git=False)
    if task_files:
        return task_files
    return collect_task_change_files(root, [], fallback_to_git=True)


CHANGE_REPORT_REQUIRED_SECTIONS = [
    "## 一句话结论",
    "## 修改前",
    "## 修改后",
    "## 文件职责变化",
    "## 改动是怎么串起来的",
    "## 关键改动说明",
    "## 反馈逐条处理结果",
    "## 本任务没有做什么",
    "## 对后续任务的影响",
    "## 验收结果",
    "## 后续维护提示",
]












def task_change_report_relevant_files(root: Path, task: dict[str, object], args: argparse.Namespace) -> list[str]:
    explicit_files = [str(item) for item in getattr(args, "file", []) if str(item).strip()]
    if explicit_files:
        return collect_task_change_files(root, explicit_files, fallback_to_git=False)
    if task.get("status") == "ready_for_user_check":
        task_files = collect_task_change_files(root, [str(item) for item in task.get("changed_files", [])], fallback_to_git=False)
        if task_files:
            return task_files
    # 任务卡里旧的 changed_files 可能来自开工前已有的脏文件，例如测试项目里的 package.json。
    # 普通完成只卡本次 task-done 明确声明的文件；待用户验收任务会继续使用进入待验收时记录的任务文件。
    return []


def read_change_report_source(root: Path, raw_path: str) -> tuple[str, Path]:
    report_path = Path(raw_path).expanduser()
    if not report_path.is_absolute():
        report_path = root / report_path
    if not report_path.exists() or not report_path.is_file():
        raise SdlcError(f"任务变更报告文件不存在：{report_path}")
    text = report_path.read_text(encoding="utf-8")
    if not text.strip():
        raise SdlcError(f"任务变更报告文件是空的：{report_path}")
    return text, report_path


def change_report_template_text(
    requirement: dict[str, object],
    task: dict[str, object],
    *,
    candidate_files: list[str] | None = None,
) -> str:
    requirement_title = str(requirement.get("title") or requirement.get("summary") or requirement.get("requirement_id"))
    task_id = str(task.get("task_id") or "T-xxx")
    task_title = str(task.get("title") or task.get("summary") or "当前任务")
    files = candidate_files or []
    file_hint = "、".join(files[:8]) if files else "先根据真实改动补上文件路径，例如 entry/src/main/ets/xxx.ets"
    ui_hint = ""
    if task_is_ui_report(task):
        ui_hint = (
            "\n\n页面、入口、弹窗或样式类任务按真实类型写清：入口/跳转任务写入口和目标页面；"
            "弹窗或设置状态任务写状态来源、保存回显和关闭返回；视觉样式任务写参考资料和验收点。"
        )
    formal_coverage_hint = task_change_report_formal_hint(task)
    return "\n".join(
        [
            f"# {task_id} 任务变更报告",
            "",
            "## 一句话结论",
            f"请用一句直白的话说明：`{task_title}` 这次让用户或后续开发实际多了什么能力，或者修掉了什么问题。",
            "",
            "## 修改前",
            "请写清楚改动前真实是什么样：用户怎么操作会遇到什么问题，代码原来在哪一层处理，为什么会影响本任务目标。",
            "",
            "## 修改后",
            "请写清楚现在变成什么样：用户看到或使用时有什么变化，代码现在怎么保证这条链路成立。",
            "",
            "## 文件职责变化",
            f"- 相关文件参考：{file_hint}",
            "- 请按文件写清楚：这个文件原来负责什么，现在新增或调整了什么，它和其他文件怎么配合。",
            "",
            "## 改动是怎么串起来的",
            "请写成真实链路：用户从哪个入口触发，经过哪些页面、服务或方法，最后得到什么结果；如果本任务涉及状态、保存、回显、失败或关闭，也要写清对应处理。",
            "",
            "## 关键改动说明",
            "请逐条写关键改动，每条都说明：修改前的问题、修改后的行为、这样改对用户或后续维护有什么意义。",
            "",
            "## 反馈逐条处理结果",
            "如果本任务来自验收退回、测试反馈、用户补充或返工，请按“反馈 1 / 处理结果 / 验证方式”逐条写清。",
            "如果没有反馈，写：本任务没有验收退回或额外反馈。",
            "",
            "## 本任务没有做什么",
            "请写清楚本任务没有扩展到哪些范围，避免后续同事误以为这里已经顺手处理了其他能力。",
            "",
            "## 对后续任务的影响",
            "只写明确有关联的未来任务：当前任务给后续准备了什么，后续会补什么，继续开发要注意什么边界。没有关联就写当前没有明确关联的未来任务。",
            "",
            "## 验收结果",
            "请写清跑过哪些命令、脚本、模拟器或人工验收点；没有跑的验收要写明原因和风险。",
            formal_coverage_hint,
            "",
            "## 后续维护提示",
            f"请写给后续维护同事：以后排查 `{requirement_title}` 相关问题时，优先看哪些文件、验证记录或任务产出。{ui_hint}",
            "",
        ]
    )


def task_list_values_from_keys(task: dict[str, object] | None, *keys: str) -> list[str]:
    if not task:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for key in keys:
        value = task.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if item is None:
                continue
            clean = str(item).strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
    return result


def task_change_report_formal_hint(task: dict[str, object] | None) -> str:
    coverage_points = task_list_values_from_keys(task, "coverage_points")
    coverage_change_ids = task_list_values_from_keys(task, "coverage_change_ids")
    coverage_acceptance = task_list_values_from_keys(task, "coverage_acceptance")
    coverage_tests = task_list_values_from_keys(task, "coverage_tests")
    out_of_scope = task_list_values_from_keys(task, "out_of_scope", "not_in_scope")
    if not (coverage_points or coverage_change_ids or coverage_acceptance or coverage_tests or out_of_scope):
        return "请补一句：本任务未记录 FR / AC / TC 正式覆盖项，已按 task.v2 中的测试和人工验收点完成验证。"
    lines = [
        "请逐项补齐正式覆盖说明，方便 task-done 判断这次没有漏需求、漏验收或越界：",
    ]
    if coverage_points:
        lines.append(f"- FR 处理或验证说明：{', '.join(coverage_points)}")
    if coverage_change_ids:
        lines.append(f"- CHG 处理或验证说明：{', '.join(coverage_change_ids)}")
    if coverage_acceptance:
        lines.append(f"- AC 验证结论：{', '.join(coverage_acceptance)}")
    if coverage_tests:
        lines.append(f"- TC 执行结果或阻塞原因：{', '.join(coverage_tests)}")
    if out_of_scope:
        lines.append("- 不做范围确认：写清没有违反本任务的不做范围。")
    lines.append("- 如果发现新增了正式文档没有的能力，先走需求变更或任务修正，不要直接收口。")
    return "\n".join(lines)


def write_change_report_template(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    raw_path: str,
    *,
    candidate_files: list[str] | None = None,
) -> Path:
    output_path = Path(raw_path).expanduser()
    if not output_path.is_absolute():
        output_path = paths.root / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        change_report_template_text(requirement, task, candidate_files=candidate_files),
        encoding="utf-8",
    )
    return output_path


def markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    body: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section:
            body.append(line)
    return "\n".join(body).strip()












def task_is_ui_report(task: dict[str, object] | None) -> bool:
    """报告模板只读取任务卡显式类型。"""

    if not task:
        return False
    return str(task.get("task_kind") or "").strip().lower() == "ui"














































def task_change_report_quality_issues(text: str, task: dict[str, object] | None = None) -> list[str]:
    """报告只做固定 Markdown 合同检查，不判断正文表达的含义。"""

    issues: list[str] = []
    for section in CHANGE_REPORT_REQUIRED_SECTIONS:
        if section not in text:
            issues.append(f"缺少章节：{section}")
        elif not markdown_section(text, section):
            issues.append(f"章节内容为空：{section}")
    return issues


def validate_change_report_text(text: str, task: dict[str, object] | None = None) -> None:
    issues = task_change_report_quality_issues(text, task)
    if issues:
        raise SdlcError(
            "任务变更报告质量检查未通过，请先由 Codex 重写人读版报告：\n"
            + "\n".join(f"- {item}" for item in issues)
            + "\n可先生成标准模板再重写："
            + "`codex-sdlc task-done REQ-001 T-001 --change-report-template /tmp/sdlc-change-report-T-001.md`。"
        )


def extract_report_conclusion(text: str) -> str:
    return "已归档人读任务变更报告。"


def ensure_task_change_report_input(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    args: argparse.Namespace,
) -> tuple[str | None, Path | None]:
    report_arg = str(getattr(args, "change_report", "") or "").strip()
    if report_arg:
        text, source_path = read_change_report_source(paths.root, report_arg)
        validate_change_report_text(text, task)
        return text, source_path
    if task_change_brief_path(paths, requirement, task).exists():
        return None, None
    relevant_files = task_change_report_relevant_files(paths.root, task, args)
    if relevant_files:
        raise SdlcError(
            "当前任务有明确代码改动，但没有传入人读版任务变更报告。\n"
            "请先让 Codex 根据任务包、真实 diff、验证记录和后续任务关系写好 Markdown 报告，"
            "再使用 `--change-report 报告路径` 交给 CLI 归档。CLI 不再自动生成这份说明。\n"
            "需要模板时，先执行 "
            f"`codex-sdlc task-done {requirement['requirement_id']} {task['task_id']} "
            "--change-report-template /tmp/sdlc-change-report-"
            f"{task['task_id']}.md`。"
        )
    return None, None


def path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_temp_change_report_source(source_path: Path) -> bool:
    temp_roots = {Path(tempfile.gettempdir()), Path("/tmp")}
    tmpdir = tempfile.gettempdir()
    if tmpdir:
        temp_roots.add(Path(tmpdir))
    return any(path_is_under(source_path, root) for root in temp_roots)


def cleanup_change_report_source(
    source_path: Path | None,
    archived_path: Path,
    *,
    project_root: Path,
    keep_source: bool,
) -> tuple[bool | None, str]:
    if source_path is None:
        return None, ""
    if source_path.resolve() == archived_path.resolve():
        return False, f"正式报告已归档，源文件就是正式文件，未删除：{source_path}"
    # 有些测试仓库或临时工作树本身就在系统临时目录下；项目内报告仍然按用户文件处理，不能自动删。
    if path_is_under(source_path, project_root):
        return False, f"正式报告已归档，源文件在当前项目内，未删除：{source_path}"
    if keep_source:
        return False, f"正式报告已归档，源文件按参数保留：{source_path}"
    if not is_temp_change_report_source(source_path):
        return False, f"正式报告已归档，源文件未删除：{source_path}"
    try:
        source_path.unlink()
        return True, f"正式报告已归档，临时源文件已删除：{source_path}"
    except OSError as exc:
        return False, f"正式报告已归档，但临时源文件删除失败：{source_path}（{exc}）"


def existing_task_change_report_summary(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    *,
    changed_files: list[str],
) -> dict[str, object] | None:
    report_path = task_change_brief_path(paths, requirement, task)
    if not report_path.exists():
        return None
    relative_path = report_path.relative_to(paths.root).as_posix()
    files = task_change_candidate_files(paths.root, task, changed_files)
    text = report_path.read_text(encoding="utf-8", errors="ignore")
    return {
        "path": relative_path,
        "files": [{"file": item} for item in files],
        "file_count": len(files),
        "primary_before": "",
        "primary_after": "",
        "impact": extract_report_conclusion(text),
    }


def save_task_change_report(
    paths: ProjectPaths,
    requirement: dict[str, object],
    task: dict[str, object],
    *,
    changed_files: list[str],
    report_text: str | None,
    source_path: Path | None = None,
    keep_source: bool = False,
) -> dict[str, object] | None:
    if report_text is None:
        return existing_task_change_report_summary(paths, requirement, task, changed_files=changed_files)

    validate_change_report_text(report_text, task)
    report_path = task_change_brief_path(paths, requirement, task)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text.rstrip() + "\n", encoding="utf-8")
    source_deleted, source_notice = cleanup_change_report_source(
        source_path,
        report_path,
        project_root=paths.root,
        keep_source=keep_source,
    )
    relative_path = report_path.relative_to(paths.root).as_posix()
    files = task_change_candidate_files(paths.root, task, changed_files)
    append_event(
        paths,
        event_type="task_change_report_saved",
        source="sdlc-task-done",
        summary=f"归档人读任务变更报告 {task['task_id']}",
        requirement_id=requirement["requirement_id"],
        task_id=task["task_id"],
        payload={
            "file_path": relative_path,
            "source": "codex-generated",
            "source_path": str(source_path) if source_path else "",
            "source_deleted": source_deleted,
        },
    )
    return {
        "path": relative_path,
        "files": [{"file": item} for item in files],
        "file_count": len(files),
        "primary_before": "",
        "primary_after": "",
        "impact": extract_report_conclusion(report_text),
        "source_notice": source_notice,
        "source_deleted": source_deleted,
    }


def print_task_change_brief_summary(brief: dict[str, object] | None) -> None:
    print("任务变更说明（给人看）：")
    if not brief:
        print("- 未归档任务变更说明：当前任务没有明确代码文件，或本次没有传入 Codex 写好的人读报告。")
        return
    print(f"- 文件：{brief['path']}")
    print(f"- 修改文件：{brief['file_count']} 个")
    print(f"- 结论：{brief['impact']}")
    source_notice = str(brief.get("source_notice") or "").strip()
    if source_notice:
        print(f"- 源文件：{source_notice}")


def print_task_output_summary(output: dict[str, object] | None) -> None:
    print("SDLC 精简产出（后续任务自动使用）：")
    if not output:
        print("- 未生成 SDLC 精简产出")
        return
    output_id = str(output.get("output_id", "OUT"))
    source_task = str(output.get("source_task_id", ""))
    files = "、".join(str(item) for item in output.get("files", [])[:4]) if isinstance(output.get("files"), list) else ""
    symbols = "、".join(str(item) for item in output.get("symbols", [])[:4]) if isinstance(output.get("symbols"), list) else ""
    print(f"- 已生成：{output_id}，来源任务：{source_task}")
    print(f"- 相关文件：{files or '未记录'}")
    print(f"- 关键符号：{symbols or '未记录'}")
    print("- 说明：精简索引只供后续正式任务读取上游产出，不作为面向用户的交付说明。")


def print_verification_summary(
    verification_records: list[dict[str, str]],
    *,
    tests_passed: bool | None,
    test_outputs: list[str] | None = None,
    scripts_passed: bool | None = None,
    script_outputs: list[str] | None = None,
) -> None:
    print("验证结果：")
    if tests_passed is True:
        print("- 自动测试通过")
    elif tests_passed is False:
        print("- 自动测试失败")
    else:
        print("- 未执行自动测试")
    if test_outputs:
        for output in test_outputs:
            print(output)
    if scripts_passed is True and script_outputs:
        print("- 可重复测试脚本通过")
    elif scripts_passed is False:
        print("- 可重复测试脚本失败")
    elif scripts_passed is True or script_outputs is not None:
        print("- 未执行可重复测试脚本")
    if script_outputs:
        for output in script_outputs:
            print(output)
    if verification_records:
        print("已记录验证：")
        for item in verification_records:
            print(f"- {item['verification_id']}：{item['summary']}")
    elif tests_passed is not False:
        print("- 暂无验证记录")


def print_git_commit_summary(commit_result: dict[str, str] | None) -> None:
    print("Git 提交状态：")
    if commit_result is None:
        print("- 未执行提交")
        return
    print(f"- {commit_result['message']}")


def print_next_task_recommendation(paths: object, state: dict[str, object]) -> None:
    next_actions = compute_next_actions(paths, state)  # type: ignore[arg-type]
    print("下一步推荐：")
    print(f"- 主推荐：{next_actions['primary']}")
    print(f"- 原因：{next_actions['reason']}")
    task_business_lines = next_task_business_lines(next_actions)
    if task_business_lines:
        print("下一任务说明：")
        for line in task_business_lines:
            print(line)


def verification_summary_with_contract(task: dict[str, object], summary: str) -> str:
    coverage_points = "、".join(str(item) for item in task.get("coverage_points", [])) or "未记录"
    coverage_tests = "、".join(str(item) for item in task.get("coverage_tests", [])) or "未记录"
    return "\n".join(
        [
            summary,
            "",
            f"覆盖需求点：{coverage_points}",
            f"覆盖测试：{coverage_tests}",
        ]
    )


def print_item_lines(items: list[object], *, empty_text: str, limit: int = 8) -> None:
    clean_items = [str(item).strip() for item in items if str(item).strip()]
    if not clean_items:
        print(f"- {empty_text}")
        return
    for item in clean_items[:limit]:
        print(f"- {item}")
    if len(clean_items) > limit:
        print(f"- 还有 {len(clean_items) - limit} 项，详见任务文件。")


def task_subtask_titles(task: dict[str, object]) -> list[str]:
    titles: list[str] = []
    for item in task.get("subtasks", []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_task_id", "")).strip()
        title = str(item.get("title", "")).strip()
        if source and title:
            titles.append(f"{source}：{title}")
        elif title:
            titles.append(title)
    return titles


def print_task_start_brief(paths: ProjectPaths, requirement: dict[str, object], task: dict[str, object]) -> None:
    goal = task_goal_text(task)
    print("当前任务目标：")
    print(f"- 任务：{requirement['requirement_id']} / {task['task_id']} {task['title']}")
    print(f"- 目标：{goal}")
    if task.get("note"):
        print(f"- 说明：{task['note']}")

    print("具体要实现：")
    implementation_items = [
        item.removeprefix("- ").strip()
        for item in must_do_lines(task, task_subtask_titles(task), goal)
    ]
    print_item_lines(implementation_items, empty_text="按任务摘要完成当前任务范围")

    print("实现完的预期效果：")
    manual_items = [
        replace_default_task_line(str(item), goal)
        for item in task.get("manual_checks", [])
    ]
    expected_items = manual_items or [
        replace_default_task_line(str(item), goal)
        for item in task.get("test_items", [])
    ]
    print_item_lines(expected_items, empty_text="当前任务可以通过测试和人工验收")

    if task.get("needs_design_material") is True:
        print("UI 设计资料提醒：")
        print("- 当前任务涉及页面或样式，请按完整读取清单中的设计和资料引用开发。")

    # 需求级经验来自当前正式状态；设计、资料、项目规则和前置交付物已经由
    # task-read-manifest.v1 固定路径与哈希，这里不再读取任何摘要副本。
    lessons = [line.removeprefix("- ").strip() for line in lesson_brief_lines(requirement)]
    if lessons:
        print("本任务可复用的需求级经验：")
        print_item_lines(lessons, empty_text="暂无需求级经验")


def print_task_completion_detail(
    task: dict[str, object],
    *,
    changed_files: list[str],
    executed_commands: list[str],
    test_commands: list[str],
    test_scripts: list[str],
    verification_records: list[dict[str, str]],
) -> None:
    print("落地情况：")
    print(f"- 任务：{task['task_id']} {task['title']}")
    print(f"- 状态：{task['status']}")
    print("- 涉及文件：")
    print_item_lines(changed_files or [str(item) for item in task.get("changed_files", [])], empty_text="没有记录涉及文件")
    print("- 已执行命令：")
    recorded_commands = unique_command_list(
        executed_commands,
        test_commands,
        test_scripts,
    )
    print_item_lines(recorded_commands, empty_text="没有记录执行命令")

    print("验收情况：")
    print("- 自动测试命令：")
    print_item_lines(test_commands, empty_text="未执行自动测试命令")
    print("- 可重复测试脚本：")
    print_item_lines(test_scripts, empty_text="未执行可重复测试脚本")
    print("- 验证记录：")
    verification_lines = [f"{item['verification_id']}：{item['summary']}" for item in verification_records]
    print_item_lines(verification_lines, empty_text="暂无验证记录")


def user_check_items(task: dict[str, object]) -> list[str]:
    goal = task_goal_text(task)
    manual_items = [
        replace_default_task_line(str(item), goal)
        for item in task.get("manual_checks", [])
        if str(item).strip()
    ]
    if manual_items:
        return manual_items
    return [
        replace_default_task_line(str(item), goal)
        for item in task.get("test_items", [])
        if str(item).strip()
    ]


def print_user_check_instructions(task: dict[str, object]) -> None:
    print("待用户验收：")
    print("- 代码已推进到可验收阶段，但还没有记录用户验收通过。")
    print("- 请按下面这些点验证：")
    print_item_lines(user_check_items(task), empty_text="按任务目标做人工或视觉验收")
    print("- 验收没问题：回复“没问题”或“验收通过”，再由 Codex 记录真实验收结论并收口。")
    print("- 验收有问题：直接描述问题，Codex 会恢复当前任务并按反馈修复。")


def append_task_update(
    paths,
    *,
    requirement: dict[str, object],
    task: dict[str, object],
    status: str,
    note: str,
    changed_files: list[str],
    commands: list[str],
    verifications: list[dict[str, str]],
    test_items: list[str] | None = None,
    test_commands: list[str] | None = None,
    test_commands_mode: str | None = None,
    test_scripts: list[str] | None = None,
    test_scripts_mode: str | None = None,
    manual_checks: list[str] | None = None,
    acceptance_feedback: list[dict[str, object]] | None = None,
    feedback_contract_version: str = "",
    feedback_state: str = "",
    source: str = "sdlc-task",
) -> None:
    payload = {
        "status": status,
        "note": note,
        "changed_files": changed_files,
        "commands": commands,
        "test_items": test_items or [],
        "test_commands": test_commands or [],
        "test_scripts": test_scripts or [],
        "manual_checks": manual_checks or [],
        "verifications": verifications,
    }
    if test_commands_mode:
        payload["test_commands_mode"] = test_commands_mode
    if test_scripts_mode:
        payload["test_scripts_mode"] = test_scripts_mode
    if acceptance_feedback is not None:
        if not feedback_contract_version or not feedback_state:
            raise SdlcError("写入验收反馈时必须同时提供 feedback_contract_version 和 feedback_state。", exit_code=1)
        payload["acceptance_feedback"] = acceptance_feedback
        payload["feedback_contract_version"] = feedback_contract_version
        payload["feedback_state"] = feedback_state
    append_event(
        paths,
        event_type="task_updated",
        source=source,
        summary=f"更新任务 {task['task_id']} 为 {status}",
        requirement_id=requirement["requirement_id"],
        task_id=task["task_id"],
        payload=payload,
    )


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    validate_test_command_edit_args(args)

    with project_lock(paths):
        state = derive_state(paths)
        requirement, task = select_task_to_finish(state, args) if args.done else select_task_to_start(state, args)
        task_id = task["task_id"]
        if args.done and isinstance(task.get("task_contract"), dict):
            if (
                getattr(args, "verify", [])
                or getattr(args, "executed_commands", [])
                or getattr(args, "file", [])
            ):
                raise SdlcError(
                    "结构化任务必须先用 task-evidence 登记带来源文件和哈希的证据，再执行 task-done。",
                    exit_code=1,
                )
            run_context = load_task_run_context(
                paths,
                requirement_id=str(requirement["requirement_id"]),
                task_id=str(task_id),
            )
            current_run = run_context["run"]
            failed_test_recorded = isinstance(current_run, dict) and any(
                isinstance(record, dict)
                and record.get("kind") in {"test", "script"}
                and (
                    record.get("result") != "passed"
                    or record.get("exit_code") != 0
                )
                for record in current_run.get("test_records", [])
            )
            try:
                result = complete_task_run(
                    paths,
                    requirement_id=str(requirement["requirement_id"]),
                    task_id=str(task_id),
                )
            except SdlcError:
                if failed_test_recorded:
                    # 失败证据保留在当前轮次，只把任务投影改成 test_failed，
                    # task-restore 随后会关闭旧轮次并创建全新的 reading 轮次。
                    append_task_update(
                        paths,
                        requirement=requirement,
                        task=task,
                        status="test_failed",
                        note=str(task.get("note") or ""),
                        changed_files=[],
                        commands=[],
                        verifications=[],
                        source="sdlc-task-done",
                    )
                    refresh_task_runtime_state(paths)
                raise
            refreshed_state = refresh_task_runtime_state(paths)
            _refreshed_requirement, refreshed_task = resolve_task(
                refreshed_state, str(requirement["requirement_id"]), str(task_id)
            )
            print(f"任务已完成：{requirement['requirement_id']} / {task_id}")
            print(f"任务状态：{refreshed_task['status']}；运行状态：closed")
            print(
                f"证据数量：测试 {result['test_count']} 条，"
                f"人工验收 {len(result['verification_records'])} 条，"
                f"用户反馈 {result['feedback_count']} 条"
            )
            changed_files = result["changed_files"]
            if isinstance(changed_files, list) and changed_files:
                print("实际修改范围：")
                for changed_file in changed_files:
                    print(f"- {changed_file}")
            else:
                print("实际修改范围：没有检测到 Git 文件变化")
            print("任务和当前运行轮次已经一次性关闭。")
            return 0
        if not args.done and isinstance(task.get("task_contract"), dict):
            # 新主线直接从正式 task.v2 开工。先收好可能中断的同一轮事务，再重新读取事件状态，
            # 这样不会把旧执行包或准备阶段的判断带回开工门禁。
            recover_task_start_transaction(
                paths,
                str(requirement["requirement_id"]),
                str(task_id),
            )
            state = derive_state(paths)
            requirement, task = resolve_task(
                state,
                str(requirement["requirement_id"]),
                str(task_id),
            )
            # task-run 的读取清单和恢复事务都引用正式交付物索引。首个任务还没有
            # 已完成交付物时先写入空索引，不能等到成功完成后才补这份合同。
            ensure_formal_task_output_index(paths, requirement)
            result = initialize_task_run(paths, state, requirement, task)
            run = result["run"]
            manifest = result["manifest"]
            requirement_root = str(manifest["requirement_root"])
            run_number = int(run["run_number"])
            manifest_path = (
                f"{requirement_root}/runtime/{task_id}/runs/{run_number:04d}/"
                "task-read-manifest.v1.json"
            )
            print(f"任务已开工：{requirement['requirement_id']} / {task_id}")
            print(f"任务状态：doing；运行状态：reading；运行轮次：{run_number:04d}")
            print(f"完整读取清单：{manifest_path}")
            print(f"读取清单 SHA-256：{run['read_manifest_sha256']}")
            print("请按清单读取全部原文和前置交付物，再由同一任务线程执行：")
            print(
                f"codex-sdlc task-read-confirm {requirement['requirement_id']} {task_id} "
                f"--manifest-sha256 {run['read_manifest_sha256']}"
            )
            return 0
        task_map = {item["task_id"]: item for item in requirement["tasks"]}
        related_draft_changes = [
            item
            for item in requirement.get("changes", [])
            if item.get("status") == "draft"
        ]
        blocking_draft_changes = changes_that_block_task_progress(related_draft_changes, task)
        if blocking_draft_changes:
            first_change = blocking_draft_changes[0]
            if task.get("status") in CURRENT_TASK_PROTECTION_STATUSES:
                raise SdlcError(current_task_change_block_message(requirement, task, first_change), exit_code=1)
            raise SdlcError(
                "当前还有待确认需求变化，请先执行 "
                f"`$sdlc-change-accept {requirement['requirement_id']} {first_change['change_id']}`，再继续推进这个任务。"
            )
        related_effective_changes = [
            item
            for item in requirement.get("changes", [])
            if item.get("status") in {"effective", "pending"}
        ]
        blocking_effective_changes = changes_that_block_task_progress(related_effective_changes, task)
        if blocking_effective_changes:
            if task.get("status") in CURRENT_TASK_PROTECTION_STATUSES:
                raise SdlcError(
                    current_task_change_block_message(requirement, task, blocking_effective_changes[0]),
                    exit_code=1,
                )
            first_change = blocking_effective_changes[0]
            change_plan_command = (
                f'$sdlc-change-plan {requirement["requirement_id"]} '
                f'--change {first_change.get("change_id", "CHG-xxx")} --task "任务标题||任务目标"'
            )
            raise SdlcError(
                "当前还有已生效但未规划需求变化，请先执行 "
                f"`{change_plan_command}`，再继续推进这个任务。"
            )
        known_requirement_change_files = {
            str(paths.root / item.get("file_path", ""))
            for item in requirement.get("changes", [])
            if item.get("file_path")
        }
        orphan_change_files = [
            item
            for item in state.get("pending_change_files", [])
            if str(item) not in known_requirement_change_files
            and (str(paths.changes_dir) in str(item) or requirement["requirement_id"] in str(item))
        ]
        if orphan_change_files:
            raise SdlcError(
                "当前还有未纳入状态的待确认或待规划需求变化，请先执行 `$sdlc-doctor-repair` 或 `$sdlc-status`，再继续推进这个任务。"
            )
        # 只有正式 task.v2 才具备可校验的运行合同。兼容任务没有 task_contract，
        # 不能因为缺少新字段而被误判成合同不完整，继续沿用原有任务流程。
        contract_issues = (
            tasks_with_contract_issues(requirement, [task])
            if isinstance(task.get("task_contract"), dict)
            else []
        )
        if contract_issues:
            raise SdlcError(task_contract_gate_message(requirement, contract_issues), exit_code=1)
        if args.done and getattr(args, "change_report_template", ""):
            template_path = write_change_report_template(
                paths,
                requirement,
                task,
                str(args.change_report_template),
                candidate_files=task_change_report_relevant_files(paths.root, task, args),
            )
            print(f"已生成任务变更报告模板：{template_path}")
            print("这一步只生成模板，不改任务状态、不跑测试、不提交 Git。")
            print(
                "写完报告后，再执行 "
                f"`codex-sdlc task-done {requirement['requirement_id']} {task_id} "
                f"--change-report {template_path}` 归档并继续收口。"
            )
            return 0
        if is_manual_verification_only_done_update(task, args):
            used_ids = verification_ids(state)
            verification_records = []
            for summary in args.verify:
                verification_id = next_number(used_ids, "VRF")
                used_ids.append(verification_id)
                verification_records.append(
                    {
                        "verification_id": verification_id,
                        "created_at": now_iso(),
                        "type": args.verification_type,
                        "status": args.verification_status,
                        "summary": verification_summary_with_contract(task, str(summary)),
                        "file_path": (
                            f".codex-sdlc/requirements/{requirement['folder_name']}"
                            f"/verifications/{verification_id}.md"
                        ),
                    }
                )
            append_task_update(
                paths,
                requirement=requirement,
                task=task,
                status="done",
                note=args.note or task.get("note", ""),
                changed_files=[],
                commands=args.executed_commands,
                verifications=verification_records,
                source="sdlc-task-done",
            )
            refreshed_state = refresh_materialized_state(paths)
            refreshed_requirement, refreshed_task = resolve_task(
                refreshed_state, requirement["requirement_id"], task_id
            )
            print(f"已补记人工验收：{refreshed_requirement['requirement_id']} / {task_id}")
            print(f"当前状态：{refreshed_task['status']}")
            print_verification_summary(verification_records, tests_passed=None, scripts_passed=None)
            print_git_commit_summary(None)
            print_next_task_recommendation(paths, refreshed_state)
            print("这一步只补充人工、模拟器或视觉验收记录，不重新跑自动测试、不提交 Git。")
            return 0
        if args.file:
            changed_files = args.file
        elif args.done and task.get("changed_files"):
            changed_files = [str(item) for item in task.get("changed_files", [])]
        else:
            changed_files = state["git_changed_files"]
        if not args.done and task.get("status") == "ready_for_user_check":
            raise SdlcError(user_check_task_message([(requirement, task)]), exit_code=1)
        if task["status"] == "done" and not args.done:
            print(f"{requirement['requirement_id']} / {task_id} 已经是 done。")
            return 0

        if task["status"] == "todo":
            for dependency in task["depends_on"]:
                if task_map[dependency]["status"] not in {"done", "closed"}:
                    raise SdlcError(
                        f"`{task_id}` 依赖 `{dependency}`，请先完成前置任务。"
                    )

        change_report_text, change_report_source_path = (
            ensure_task_change_report_input(paths, requirement, task, args)
            if args.done and not getattr(args, "await_user_check", False)
            else (None, None)
        )
        verification_records: list[dict[str, str]] = []

        if not args.done:
            new_status = "doing"
            test_commands = edited_test_commands(args) if test_command_payload_mode(args) else args.test_command
            test_scripts = edited_test_scripts(args) if test_script_payload_mode(args) else args.test_script
            append_task_update(
                paths,
                requirement=requirement,
                task=task,
                status=new_status,
                note=args.note or task.get("note", ""),
                changed_files=changed_files,
                commands=args.executed_commands,
                verifications=[],
                test_items=args.test_item,
                test_commands=test_commands,
                test_commands_mode=test_command_payload_mode(args),
                test_scripts=test_scripts,
                test_scripts_mode=test_script_payload_mode(args),
                manual_checks=args.manual_check,
            )
            refreshed_state = refresh_materialized_state(paths)
        else:
            test_commands_mode = test_command_payload_mode(args)
            if test_commands_mode:
                test_commands = edited_test_commands(args)
            else:
                test_commands = task_test_commands(task, state)
            test_scripts_mode = test_script_payload_mode(args)
            if test_scripts_mode:
                test_scripts = edited_test_scripts(args)
            else:
                test_scripts = unique_command_list(
                    task_test_scripts(task), getattr(args, "test_script", [])
                )
            ensure_changed_requirement_mjs_scripts_registered(
                paths,
                requirement,
                task,
                state,
                changed_files=changed_files,
                test_scripts=test_scripts,
            )
            if not test_commands:
                _scripts, detected_test_commands = detect_project_commands(paths.root)
                if not test_commands_mode:
                    test_commands = usable_commands(detected_test_commands)
            if not test_commands and not test_scripts:
                if args.verify and args.verification_status == "passed":
                    used_ids = verification_ids(state)
                    for summary in args.verify:
                        verification_id = next_number(used_ids, "VRF")
                        used_ids.append(verification_id)
                        verification_records.append(
                            {
                                "verification_id": verification_id,
                                "created_at": now_iso(),
                                "type": args.verification_type,
                                "status": args.verification_status,
                                "summary": verification_summary_with_contract(task, summary),
                                "file_path": (
                                    f".codex-sdlc/requirements/{requirement['folder_name']}"
                                    f"/verifications/{verification_id}.md"
                                ),
                            }
                        )
                    append_task_update(
                        paths,
                        requirement=requirement,
                        task=task,
                        status="done",
                        note=args.note or task.get("note", ""),
                        changed_files=changed_files,
                        commands=args.executed_commands,
                        verifications=verification_records,
                        test_commands=test_commands,
                        test_commands_mode=test_commands_mode,
                        test_scripts=test_scripts,
                        test_scripts_mode=test_scripts_mode,
                    )
                    refreshed_state = refresh_materialized_state(paths)
                    refreshed_requirement, refreshed_task = resolve_task(
                        refreshed_state, requirement["requirement_id"], task_id
                    )
                    change_brief = save_task_change_report(
                        paths,
                        refreshed_requirement,
                        refreshed_task,
                        changed_files=changed_files,
                        report_text=change_report_text,
                        source_path=change_report_source_path,
                        keep_source=getattr(args, "keep_change_report_source", False),
                    )
                    task_output = create_task_output_contract(
                        paths,
                        refreshed_requirement,
                        refreshed_task,
                        changed_files=changed_files,
                        change_brief=change_brief,
                    )
                    if task_output:
                        append_event(
                            paths,
                            event_type="task_output_created",
                            source="sdlc-task-done",
                            summary=f"生成 SDLC 精简产出 {task_output['output_id']}",
                            requirement_id=refreshed_requirement["requirement_id"],
                            task_id=task_id,
                            payload={
                                "output_id": task_output["output_id"],
                                "file_path": (
                                    f".codex-sdlc/requirements/{refreshed_requirement['folder_name']}"
                                    f"/task-outputs/{task_output['output_id']}.md"
                                ),
                            },
                        )
                    commit_result = auto_commit_task_changes(paths.root, refreshed_requirement, refreshed_task, changed_files)
                    print(f"任务已更新：{refreshed_requirement['requirement_id']} / {task_id}")
                    print(f"当前状态：{refreshed_task['status']}")
                    print_task_completion_detail(
                        refreshed_task,
                        changed_files=changed_files,
                        executed_commands=[str(item) for item in args.executed_commands],
                        test_commands=test_commands,
                        test_scripts=test_scripts,
                        verification_records=verification_records,
                    )
                    print_task_change_brief_summary(change_brief)
                    print_task_output_summary(task_output)
                    print_verification_summary(verification_records, tests_passed=None, scripts_passed=None)
                    print_git_commit_summary(commit_result)
                    print("下一步推荐只作为建议输出，本命令不会自动开始下一个任务。")
                    return 0

                if args.verify and args.verification_status in {"failed", "blocked"}:
                    raise SdlcError("结构化验证状态不是 passed，任务不能标记为 done。")
                if args.verify:
                    raise SdlcError(
                        "仅有验证说明不能完成任务。没有自动测试时，请同时传入 "
                        "`--verification-type manual --verification-status passed`。"
                    )

                note = task.get("note", "")
                extra_note = "缺少自动测试命令，任务已进入人工检查状态。"
                note = note + ("\n" if note else "") + extra_note
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status="ready_for_user_check",
                    note=note,
                    changed_files=changed_files,
                    commands=[],
                    verifications=[],
                    test_commands=test_commands,
                    test_commands_mode=test_commands_mode,
                    test_scripts=test_scripts,
                    test_scripts_mode=test_scripts_mode,
                )
                refreshed_state = refresh_materialized_state(paths)
                print(f"任务已更新：{requirement['requirement_id']} / {task_id}")
                print("当前状态：ready_for_user_check")
                print_task_completion_detail(
                    task,
                    changed_files=changed_files,
                    executed_commands=[str(item) for item in args.executed_commands],
                    test_commands=test_commands,
                    test_scripts=test_scripts,
                    verification_records=[],
                )
                print_verification_summary([], tests_passed=None, scripts_passed=None)
                print_user_check_instructions(task)
                print_git_commit_summary(None)
                print(
                    "缺少自动测试命令。下一步请使用 "
                    f"`$sdlc-task-done {requirement['requirement_id']} {task_id}` 收口当前待验证任务；"
                    "可以让 Codex 使用 computer use 做真实界面验证。"
                    "如果验证卡住或范围不清，先停下来询问用户。"
                )
                print("下一步推荐只作为建议输出，本命令不会自动开始下一个任务。")
                return 0

            tests_passed, test_outputs = run_test_commands(paths.root, test_commands)
            if not tests_passed:
                note = task.get("note", "")
                extra_note = "自动测试失败：\n" + "\n\n".join(test_outputs)
                note = note + ("\n" if note else "") + extra_note
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status="test_failed",
                    note=note,
                    changed_files=changed_files,
                    commands=unique_command_list(args.executed_commands, test_commands),
                    verifications=[],
                    test_commands=test_commands,
                    test_commands_mode=test_commands_mode,
                    test_scripts=test_scripts,
                    test_scripts_mode=test_scripts_mode,
                )
                failed_state = refresh_materialized_state(paths)
                _failed_requirement, failed_task = resolve_task(failed_state, requirement["requirement_id"], task_id)
                print(f"任务已更新：{requirement['requirement_id']} / {task_id}")
                print("当前状态：test_failed")
                print_task_completion_detail(
                    failed_task,
                    changed_files=changed_files,
                    executed_commands=[str(item) for item in args.executed_commands],
                    test_commands=test_commands,
                    test_scripts=test_scripts,
                    verification_records=[],
                )
                print_verification_summary([], tests_passed=False, test_outputs=test_outputs, scripts_passed=None)
                print_git_commit_summary(None)
                print("测试失败时必须先修复并重新执行 `$sdlc-task-done`，不能继续推进下一个任务。")
                return 1

            scripts_passed, script_outputs = run_test_scripts(paths.root, test_scripts)
            if not scripts_passed:
                note = task.get("note", "")
                extra_note = "可重复测试脚本失败：\n" + "\n\n".join(script_outputs)
                note = note + ("\n" if note else "") + extra_note
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status="test_failed",
                    note=note,
                    changed_files=changed_files,
                    commands=unique_command_list(args.executed_commands, test_commands, test_scripts),
                    verifications=[],
                    test_commands=test_commands,
                    test_commands_mode=test_commands_mode,
                    test_scripts=test_scripts,
                    test_scripts_mode=test_scripts_mode,
                )
                failed_state = refresh_materialized_state(paths)
                _failed_requirement, failed_task = resolve_task(failed_state, requirement["requirement_id"], task_id)
                print(f"任务已更新：{requirement['requirement_id']} / {task_id}")
                print("当前状态：test_failed")
                print_task_completion_detail(
                    failed_task,
                    changed_files=changed_files,
                    executed_commands=[str(item) for item in args.executed_commands],
                    test_commands=test_commands,
                    test_scripts=test_scripts,
                    verification_records=[],
                )
                print_verification_summary(
                    [],
                    tests_passed=True,
                    test_outputs=test_outputs,
                    scripts_passed=False,
                    script_outputs=script_outputs,
                )
                print_git_commit_summary(None)
                print("测试脚本失败时必须先修复并重新执行 `$sdlc-task-done`，不能继续推进下一个任务。")
                return 1

            if getattr(args, "await_user_check", False):
                note = args.note or task.get("note", "")
                extra_note = "等待用户验收：自动测试和可重复脚本已通过，当前任务需要用户按人工/视觉验收点确认。"
                note = str(note) + ("\n" if note else "") + extra_note
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status="ready_for_user_check",
                    note=note,
                    changed_files=changed_files,
                    commands=unique_command_list(args.executed_commands, test_commands, test_scripts),
                    verifications=[],
                    test_commands=test_commands,
                    test_commands_mode=test_commands_mode,
                    test_scripts=test_scripts,
                    test_scripts_mode=test_scripts_mode,
                )
                refreshed_state = refresh_materialized_state(paths)
                _refreshed_requirement, refreshed_task = resolve_task(refreshed_state, requirement["requirement_id"], task_id)
                print(f"任务已更新：{requirement['requirement_id']} / {task_id}")
                print("当前状态：ready_for_user_check")
                print_task_completion_detail(
                    refreshed_task,
                    changed_files=changed_files,
                    executed_commands=[str(item) for item in args.executed_commands],
                    test_commands=test_commands,
                    test_scripts=test_scripts,
                    verification_records=[],
                )
                print_verification_summary(
                    [],
                    tests_passed=True,
                    test_outputs=test_outputs,
                    scripts_passed=scripts_passed,
                    script_outputs=script_outputs,
                )
                print_user_check_instructions(refreshed_task)
                print_git_commit_summary(None)
                print(
                    "下一步：等待用户验收反馈。没问题时再执行 "
                    f"`$sdlc-task-done {requirement['requirement_id']} {task_id}` 记录验收并收口；"
                    "有问题时使用 `$sdlc-task-restore 反馈内容` 恢复当前任务。"
                )
                print("下一步推荐只作为建议输出，本命令不会自动开始下一个任务。")
                return 0

            used_ids = verification_ids(state)
            evidence_parts = [*test_commands, *test_scripts]
            summaries = args.verify or [f"自动验证通过：{'；'.join(evidence_parts)}"]
            for summary in summaries:
                verification_id = next_number(used_ids, "VRF")
                used_ids.append(verification_id)
                verification_records.append(
                    {
                        "verification_id": verification_id,
                        "created_at": now_iso(),
                        "type": args.verification_type or "automated",
                        "status": args.verification_status or "passed",
                        "summary": verification_summary_with_contract(task, summary),
                        # 验证记录属于具体需求，主文件直接放进需求包，避免全局目录再复制一份。
                        "file_path": (
                            f".codex-sdlc/requirements/{requirement['folder_name']}"
                            f"/verifications/{verification_id}.md"
                        ),
                    }
                )
            append_task_update(
                paths,
                requirement=requirement,
                task=task,
                status="done",
                note=args.note or task.get("note", ""),
                changed_files=changed_files,
                commands=unique_command_list(args.executed_commands, test_commands, test_scripts),
                verifications=verification_records,
                test_commands=test_commands,
                test_commands_mode=test_commands_mode,
                test_scripts=test_scripts,
                test_scripts_mode=test_scripts_mode,
            )
            refreshed_state = refresh_materialized_state(paths)

    refreshed_requirement, refreshed_task = resolve_task(refreshed_state, requirement["requirement_id"], task_id)
    change_brief = (
        save_task_change_report(
            paths,
            refreshed_requirement,
            refreshed_task,
            changed_files=changed_files,
            report_text=change_report_text,
            source_path=change_report_source_path,
            keep_source=getattr(args, "keep_change_report_source", False),
        )
        if args.done and refreshed_task["status"] == "done"
        else None
    )
    task_output = (
        create_task_output_contract(
            paths,
            refreshed_requirement,
            refreshed_task,
            changed_files=changed_files,
            change_brief=change_brief,
        )
        if args.done and refreshed_task["status"] == "done"
        else None
    )
    if task_output:
        append_event(
            paths,
            event_type="task_output_created",
            source="sdlc-task-done",
            summary=f"生成 SDLC 精简产出 {task_output['output_id']}",
            requirement_id=refreshed_requirement["requirement_id"],
            task_id=task_id,
            payload={
                "output_id": task_output["output_id"],
                "file_path": (
                    f".codex-sdlc/requirements/{refreshed_requirement['folder_name']}"
                    f"/task-outputs/{task_output['output_id']}.md"
                ),
            },
        )
    commit_result = auto_commit_task_changes(paths.root, refreshed_requirement, refreshed_task, changed_files) if args.done and refreshed_task["status"] == "done" else None
    print(f"任务已更新：{refreshed_requirement['requirement_id']} / {task_id}")
    print(f"当前状态：{refreshed_task['status']}")
    if not args.done:
        print_task_start_brief(paths, refreshed_requirement, refreshed_task)
        added_test_parts = []
        if args.test_item:
            added_test_parts.append(f"测试项 {len(args.test_item)} 条")
        if args.test_command:
            added_test_parts.append(f"测试命令 {len(args.test_command)} 条")
        if args.test_script:
            added_test_parts.append(f"测试脚本 {len(args.test_script)} 条")
        if args.manual_check:
            added_test_parts.append(f"人工验收点 {len(args.manual_check)} 条")
        if added_test_parts:
            print("测试方案审核：已补充" + "、".join(added_test_parts))
    if args.done:
        print_task_completion_detail(
            refreshed_task,
            changed_files=changed_files,
            executed_commands=[str(item) for item in args.executed_commands],
            test_commands=test_commands,
            test_scripts=test_scripts,
            verification_records=verification_records,
        )
        print_task_change_brief_summary(change_brief)
        print_task_output_summary(task_output)
        print_verification_summary(
            verification_records,
            tests_passed=True,
            test_outputs=test_outputs,
            scripts_passed=scripts_passed,
            script_outputs=script_outputs,
        )
        print_git_commit_summary(commit_result)
        print_next_task_recommendation(paths, refreshed_state)
        print("下一步推荐只作为建议输出，本命令不会自动开始下一个任务。")
    return 0


def run_restore(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    if args.feedback_contract:
        raise SdlcError(
            "task-restore 不再读取旧反馈合同。请先把反馈原文保存为项目内 task-feedback.v1 文件，"
            "用 task-evidence 绑定当前轮次，再按正式变更或恢复流程处理。",
            exit_code=1,
        )

    requirement_id, task_id, feedback = parse_restore_target(args.items)
    if not feedback:
        raise SdlcError("请写清楚这次要恢复任务的反馈内容。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement, task = select_task_to_restore(state, requirement_id, task_id)
        if task["status"] == "done" and later_progress_exists(state, requirement, task):
            raise SdlcError(
                f"{requirement['requirement_id']} / {task['task_id']} 已经是历史完成任务，后续任务也已经推进。\n"
                "这类问题不要恢复旧任务，请使用 `$sdlc-fix` 插入新的修复任务。",
                exit_code=1,
            )
        if isinstance(task.get("task_contract"), dict):
            ensure_formal_task_output_index(paths, requirement)
            if task.get("status") == "test_failed":
                # 失败轮次没有已完成交付物，不能走“撤销已完成任务”的恢复事务。
                # 先把失败证据所在轮次固定为 stale，再由 task.v2 和同一审核结果
                # 创建全新 reading 轮次；旧证据原样保留，不复制到新轮次。
                context = load_task_run_context(
                    paths,
                    requirement_id=str(requirement["requirement_id"]),
                    task_id=str(task["task_id"]),
                )
                old_run = context["run"]
                current = context["current"]
                old_run_path = context["run_path"]
                current_path = context["current_path"]
                assert isinstance(old_run, dict)
                assert isinstance(current, dict)
                assert isinstance(old_run_path, Path)
                assert isinstance(current_path, Path)
                if old_run.get("status") not in {"active", "stale"}:
                    raise SdlcError("失败任务缺少可恢复的活动轮次。", exit_code=1)
                if old_run.get("status") == "active":
                    sync_task_run_status(
                        run_path=old_run_path,
                        current_path=current_path,
                        run=old_run,
                        current=current,
                        status="stale",
                    )
                restore_note = f"恢复说明：{feedback}"
                old_note = str(task.get("note") or "")
                if restore_note not in old_note:
                    append_task_update(
                        paths,
                        requirement=requirement,
                        task=task,
                        status="test_failed",
                        note=old_note + ("\n" if old_note else "") + restore_note,
                        changed_files=[],
                        commands=[],
                        verifications=[],
                        source="sdlc-task-restore",
                    )
                state = derive_state(paths)
                requirement, task = resolve_task(
                    state,
                    str(requirement["requirement_id"]),
                    str(task["task_id"]),
                )
                result = initialize_task_run(paths, state, requirement, task)
            else:
                result = restore_task_run(
                    paths,
                    state=state,
                    requirement=requirement,
                    task=task,
                    reason=feedback,
                )
            run = result["run"]
            manifest = result["manifest"]
            assert isinstance(run, dict)
            assert isinstance(manifest, dict)
            run_number = int(run["run_number"])
            print(f"已恢复任务：{requirement['requirement_id']} / {task['task_id']}")
            print(f"反馈：{feedback}")
            print(f"任务状态：doing；运行状态：reading；运行轮次：{run_number:04d}")
            print(
                "完整读取清单："
                f"{manifest['requirement_root']}/runtime/{task['task_id']}"
                f"/runs/{run_number:04d}/task-read-manifest.v1.json"
            )
            print(f"读取清单 SHA-256：{run['read_manifest_sha256']}")
            if result.get("idempotent"):
                print("同一条恢复请求已返回当前轮次，没有重复占用运行号。")
            return 0
        # 没有 task.v2 的档案没有 task-run，只保留事件级恢复兼容；这里直接使用
        # 任务字段，不读取、失效或重写任务包。正式任务始终走上面的运行轮次。
        note = str(task.get("note") or "")
        test_item = f"新增测试项：{feedback}"
        note = note + ("\n" if note else "") + f"验收反馈：{feedback}\n{test_item}"
        append_task_update(
            paths,
            requirement=requirement,
            task=task,
            status="doing",
            note=note,
            changed_files=state["git_changed_files"],
            commands=[],
            verifications=[],
            test_items=[test_item],
            manual_checks=[f"人工确认反馈已解决：{feedback}"],
            source="sdlc-task-restore",
        )
        refresh_materialized_state(paths)

    print(f"已恢复任务：{requirement['requirement_id']} / {task['task_id']}")
    print(f"反馈：{feedback}")
    print("处理方式：当前档案没有 task.v2；已记录原任务验收返工，补齐正式合同后会创建 task-run 轮次。")
    print("下一步建议：先把旧任务迁入完整 task.v2 和覆盖关系，再继续登记任务证据。")
    return 0


def run_pause(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement, task = select_task_to_pause(state, args)
        formal_task = isinstance(task.get("task_contract"), dict)
        if formal_task:
            requirement_id = str(requirement["requirement_id"])
            task_id = str(task["task_id"])
            context = load_task_run_context(
                paths,
                requirement_id=requirement_id,
                task_id=task_id,
            )
            run = context["run"]
            current = context["current"]
            run_path = context["run_path"]
            assert isinstance(run, dict)
            assert isinstance(current, dict)
            assert isinstance(run_path, Path)
            # reading 和 active 都是尚未关闭的真实轮次。暂停只校验轮次身份和
            # 读取清单，不要求 reading 先假装进入 active；关闭或错位轮次一律拒绝。
            if (
                run.get("requirement_id") != requirement_id
                or run.get("task_id") != task_id
                or run.get("run_number") != current.get("run_number")
                or run.get("status") != current.get("status")
                or run.get("status") not in {"reading", "active"}
            ):
                raise SdlcError("当前任务运行轮次身份不一致或已经关闭，不能暂停。", exit_code=1)
            manifest_path = run_path.parent / "task-read-manifest.v1.json"
            manifest_sha256 = str(run.get("read_manifest_sha256") or "")
            if (
                not manifest_path.is_file()
                or manifest_path.is_symlink()
                or current.get("read_manifest_sha256") != manifest_sha256
                or sha256_file(manifest_path) != manifest_sha256
            ):
                raise SdlcError("当前任务读取清单缺失、损坏或轮次身份不一致，不能暂停。", exit_code=1)
            sync_task_run_status(
                run_path=run_path,
                current_path=context["current_path"],
                run=run,
                current=current,
                status="stale",
            )
        note = str(task.get("note", ""))
        reason = args.reason.strip() or "用户要求暂停当前任务，等待重新安排。"
        pause_note = f"暂停说明：{reason}"
        if pause_note not in note:
            note = note + ("\n" if note else "") + pause_note
        append_task_update(
            paths,
            requirement=requirement,
            task=task,
            status="todo",
            note=note,
            changed_files=state["git_changed_files"],
            commands=[],
            verifications=[],
        )
        if formal_task:
            # 正式任务的真实状态由事件和 task-run 投影组成，不再触发旧正式包的
            # Markdown 全量渲染；否则历史 formal.v2 字段会反过来阻塞暂停。
            refresh_task_runtime_state(paths)
        else:
            refresh_materialized_state(paths)

    print(f"已暂停任务：{requirement['requirement_id']} / {task['task_id']}")
    print(f"原因：{reason}")
    print("当前状态：todo")
    print("下一步建议：$sdlc-next")
    return 0


def _unique_contract_items(items: list[object]) -> list[object]:
    """合并正式任务字段时保留原顺序，避免把业务文字重新解释或改写。"""

    result: list[object] = []
    fingerprints: set[str] = set()
    for item in items:
        fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            result.append(deepcopy(item))
    return result


def _build_follow_up_task_contract(
    source_tasks: list[dict[str, object]],
    *,
    task_id: str,
    kind: str,
    detail: str,
) -> dict[str, object]:
    """只合并已有结构化字段并原样登记输入，不猜测自然语言里的业务含义。"""

    contracts = [
        task.get("task_contract")
        for task in source_tasks
        if isinstance(task.get("task_contract"), dict)
    ]
    if len(contracts) != len(source_tasks) or not contracts:
        raise SdlcError("正式后续任务缺少可复用的 task.v2 结构化输入。", exit_code=1)
    merged = deepcopy(contracts[0])
    assert isinstance(merged, dict)
    source_ids = [str(task["task_id"]) for task in source_tasks]
    scope_text = source_ids[0] if len(source_ids) == 1 else f"{source_ids[0]} 到 {source_ids[-1]}"
    if kind == "fix":
        merged["title"] = f"修复 {source_ids[0]} 的已确认问题"
        merged["goal"] = f"按原文问题记录修复 {source_ids[0]}，并重跑它的正式验收。"
        instruction = f"待处理问题原文：{detail}"
        deliverable = f"{task_id} 的修复结果和正式验证证据"
    else:
        merged["title"] = f"复查 {scope_text} 的任务质量"
        merged["goal"] = f"按现有正式合同复查 {scope_text}，不改写原任务的完成记录。"
        instruction = f"质量复查说明原文：{detail or '按现有正式合同逐项复查'}"
        deliverable = f"{task_id} 的质量复查结果和正式验证证据"

    list_fields = (
        "requirement_refs",
        "global_rule_refs",
        "technical_solution_refs",
        "design_refs",
        "material_refs",
        "change_refs",
        "acceptance_refs",
        "deliverables",
        "implementation_requirements",
        "data_api_page_component_requirements",
        "states_and_exceptions",
        "security_and_privacy",
        "automated_tests",
        "manual_checks",
        "out_of_scope",
        "blocking_conditions",
        "definition_of_done",
    )
    for field in list_fields:
        merged[field] = _unique_contract_items(
            [
                item
                for contract in contracts
                if isinstance(contract, dict)
                for item in contract.get(field, [])
            ]
        )
    scopes = [
        contract.get("code_scope", {})
        for contract in contracts
        if isinstance(contract, dict)
    ]
    merged["code_scope"] = {
        field: _unique_contract_items(
            [
                item
                for scope in scopes
                if isinstance(scope, dict)
                for item in scope.get(field, [])
            ]
        )
        for field in ("read_paths", "likely_change_paths", "protected_paths")
    }
    merged["task_id"] = task_id
    merged["depends_on"] = source_ids
    merged["deliverables"] = _unique_contract_items(
        [*merged["deliverables"], deliverable]
    )
    merged["implementation_requirements"] = _unique_contract_items(
        [*merged["implementation_requirements"], instruction]
    )
    validate_schema_document(merged, schema_name="task.v2")
    return merged


def _extend_formal_coverage(
    coverage: dict[str, object],
    *,
    source_ids: list[str],
    new_task: dict[str, object],
) -> dict[str, object]:
    """沿用来源任务的显式覆盖边，不新增任何没有结构化依据的业务关系。"""

    result = deepcopy(coverage)
    new_task_id = str(new_task["task_id"])
    sections = (
        "functional_requirements",
        "design_artifacts",
        "acceptance_criteria",
        "effective_changes",
    )
    for section in sections:
        records = result.get(section)
        if not isinstance(records, dict):
            raise SdlcError("正式 task-coverage.v1 缺少完整覆盖分区。", exit_code=1)
        for record in records.values():
            if not isinstance(record, dict):
                raise SdlcError("正式 task-coverage.v1 包含损坏的覆盖记录。", exit_code=1)
            tasks = [str(item) for item in record.get("tasks", [])]
            matched_sources = [item for item in source_ids if item in tasks]
            if not matched_sources:
                continue
            record["tasks"] = [*tasks, new_task_id]
            if section == "functional_requirements":
                record["status"] = "planned"
            if section == "acceptance_criteria":
                old_refs = [str(item) for item in record.get("test_refs", [])]
                indexes = [
                    match.group(1)
                    for ref in old_refs
                    for source_id in matched_sources
                    if (match := re.fullmatch(rf"{re.escape(source_id)}#automated_tests/([0-9]+)", ref))
                ]
                if not indexes:
                    indexes = ["0"]
                record["test_refs"] = [
                    *old_refs,
                    *(f"{new_task_id}#automated_tests/{index}" for index in dict.fromkeys(indexes)),
                ]
    validate_schema_document(result, schema_name="task-coverage.v1")
    return result


def _task_status_restore_payload(task: dict[str, object]) -> dict[str, object]:
    """整包事件会重建任务投影，这里把历史执行字段按原值接回去。"""

    return {
        "status": task.get("status", "todo"),
        "note": task.get("note", ""),
        "changed_files": deepcopy(task.get("changed_files", [])),
        "context_files": deepcopy(task.get("context_files", [])),
        "output_files": deepcopy(task.get("output_files", [])),
        "related_files": deepcopy(task.get("related_files", [])),
        "commands": deepcopy(task.get("commands", [])),
    }


def _append_event_document(
    events: list[dict[str, object]],
    *,
    paths: object,
    event_type: str,
    source: str,
    summary: str,
    requirement_id: str,
    task_id: str | None,
    payload: dict[str, object],
) -> dict[str, object]:
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
    events.append(event)
    return event


def _revise_formal_task_plan(
    paths: object,
    state: dict[str, object],
    requirement: dict[str, object],
    source_tasks: list[dict[str, object]],
    *,
    kind: str,
    detail: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """把后续任务作为新一版完整正式整包提交，历史轮次和证据目录不参与改写。"""

    unsafe = [
        str(task.get("task_id") or "")
        for task in requirement.get("tasks", [])
        if isinstance(task, dict)
        and str(task.get("status") or "") not in {"todo", "blocked", "done", "closed"}
    ]
    if unsafe:
        raise SdlcError(
            "正式任务仍有未关闭轮次，请先用 task-pause 或完成当前任务：" + "、".join(unsafe) + "。",
            exit_code=1,
        )

    all_ids = [
        str(task.get("task_id") or "")
        for current_requirement in state["requirements"].values()
        for task in current_requirement.get("tasks", [])
        if isinstance(task, dict)
    ]
    new_task_id = next_number(all_ids, "T")
    new_task = _build_follow_up_task_contract(
        source_tasks,
        task_id=new_task_id,
        kind=kind,
        detail=detail,
    )
    requirement_id = str(requirement["requirement_id"])
    requirement_root = paths.requirements_dir / str(requirement["folder_name"])
    task_directory = requirement_root / "tasks"
    coverage_path = requirement_root / "task-coverage.v1.json"

    # 先用公开读取面交叉验证现有整包，再从已记录的 task.v2 确定性生成新整包。
    formal_task_contract.load_task_plan_record(paths, requirement_id)
    previous_receipt = formal_task_contract._validate_committed_directory(task_directory)
    previous_package_sha256 = str(previous_receipt["package_sha256"])
    plan = json.loads((task_directory / "task-plan.v2.json").read_text(encoding="utf-8"))
    task_records = [
        json.loads((task_directory / f"{task_id}.json").read_text(encoding="utf-8"))
        for task_id in plan["tasks"]
    ]
    task_records.append(new_task)
    mapping = {str(key): str(value) for key, value in plan["mapping"].items()}
    client_key = f"{kind}-{new_task_id.lower()}"
    mapping[client_key] = new_task_id
    plan["producer_run_id"] = f"sdlc-{kind}-{requirement_id.lower()}-{new_task_id.lower()}"
    plan["tasks"] = [str(task["task_id"]) for task in task_records]
    plan["mapping"] = mapping
    plan["dependencies"] = [
        {"from": str(dependency), "to": str(task["task_id"])}
        for task in task_records
        for dependency in task.get("depends_on", [])
    ]
    coverage = _extend_formal_coverage(
        json.loads(coverage_path.read_text(encoding="utf-8")),
        source_ids=[str(task["task_id"]) for task in source_tasks],
        new_task=new_task,
    )
    validate_schema_document(plan, schema_name="task-plan.v2")
    reference_index_sha256 = str(previous_receipt["reference_index_sha256"])
    package_sha256 = formal_task_contract._formal_package_sha256(
        plan=plan,
        tasks=task_records,
        coverage=coverage,
        reference_index_sha256=reference_index_sha256,
    )
    files = formal_task_contract._bundle_files(plan, task_records)

    with event_write_lock(paths):
        events = load_events(paths)
        new_events: list[dict[str, object]] = []
        preserved = [
            deepcopy(task)
            for task in requirement.get("tasks", [])
            if isinstance(task, dict) and task.get("status") not in {"todo", "blocked"}
        ]
        for task in preserved:
            new_events.append(
                _append_event_document(
                    events,
                    paths=paths,
                    event_type="task_updated",
                    source=f"sdlc-{kind}",
                    summary=f"准备正式整包修订 {task['task_id']}",
                    requirement_id=requirement_id,
                    task_id=str(task["task_id"]),
                    payload={"status": "todo", "note": task.get("note", "")},
                )
            )
        revision_event_id = next_event_id(events)
        receipt = formal_task_contract._build_receipt(
            requirement_id=requirement_id,
            package_sha256=package_sha256,
            reference_index_sha256=reference_index_sha256,
            mapping=mapping,
            event_id=revision_event_id,
            files=files,
            coverage=coverage,
        )
        revision_event = formal_task_contract._event_for_import(
            paths=paths,
            event_id=revision_event_id,
            requirement_id=requirement_id,
            package_sha256=package_sha256,
            reference_index_sha256=reference_index_sha256,
            mapping=mapping,
            plan=plan,
            tasks=task_records,
            coverage=coverage,
            receipt=receipt,
            event_type=formal_task_contract.TASK_PLAN_REVISION_EVENT,
            previous_package_sha256=previous_package_sha256,
        )
        formal_task_contract._validate_import_event(revision_event, receipt)
        events.append(revision_event)
        new_events.append(revision_event)
        transaction_root = requirement_root / ".task-plan-follow-up"
        staging_root = transaction_root / uuid.uuid4().hex
        staging_tasks = staging_root / "tasks"
        backup_tasks = staging_root / "previous-tasks"
        backup_coverage = staging_root / "previous-task-coverage.v1.json"
        events_before = paths.events_file.read_bytes()
        try:
            staging_root.mkdir(parents=True, exist_ok=False)
            formal_task_contract._write_staging_bundle(staging_tasks, files, receipt)
            os.rename(task_directory, backup_tasks)
            os.rename(coverage_path, backup_coverage)
            os.rename(staging_tasks, task_directory)
            formal_task_contract._atomic_write_bytes(
                coverage_path,
                canonical_json_text(coverage).encode("utf-8"),
            )
            suffix = b"" if not events_before or events_before.endswith(b"\n") else b"\n"
            event_bytes = b"".join(
                (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                for event in new_events
            )
            formal_task_contract._atomic_write_bytes(
                paths.events_file,
                events_before + suffix + event_bytes,
            )
            # 写完立即走正式读取和状态推导；任何一项不一致都恢复上一整包。
            formal_task_contract.load_task_plan_record(paths, requirement_id)
            derive_state(paths)
        except Exception:
            formal_task_contract._atomic_write_bytes(paths.events_file, events_before)
            if task_directory.exists():
                shutil.rmtree(task_directory)
            if backup_tasks.exists():
                os.rename(backup_tasks, task_directory)
            coverage_path.unlink(missing_ok=True)
            if backup_coverage.exists():
                os.rename(backup_coverage, coverage_path)
            raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            if transaction_root.exists() and not any(transaction_root.iterdir()):
                transaction_root.rmdir()
    return new_task, preserved


def _restore_formal_task_statuses(
    paths: object,
    requirement_id: str,
    tasks: list[dict[str, object]],
    *,
    source: str,
) -> None:
    """审核请求绑定新整包后，把历史任务的完成状态按原值恢复。"""

    with project_lock(paths):
        for task in tasks:
            append_event(
                paths,
                event_type="task_updated",
                source=source,
                summary=f"保留历史任务状态 {task['task_id']}",
                requirement_id=requirement_id,
                task_id=str(task["task_id"]),
                payload=_task_status_restore_payload(task),
            )
        refresh_task_runtime_state(paths)


def run_audit(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    requirement_id, task_ids, note = parse_audit_target(args.items, args.note)
    reason = "先做已完成任务质量复查，暂停当前进行中任务。"

    with project_lock(paths):
        state = derive_state(paths)
        requirement = select_requirement_for_audit(state, requirement_id)
        audited_tasks = select_audited_tasks(requirement, task_ids)
        formal_audit = all(isinstance(item.get("task_contract"), dict) for item in audited_tasks)
        if formal_audit:
            audit_contract, preserved_tasks = _revise_formal_task_plan(
                paths,
                state,
                requirement,
                audited_tasks,
                kind="audit",
                detail=note,
            )
            audit_task = {"task_id": audit_contract["task_id"]}
            existing_audit_task = None
            paused_tasks = []
        else:
            existing_audit_task = find_existing_audit_task(requirement, audited_tasks)
            paused_tasks = pause_doing_tasks_for_audit(
                requirement,
                reason,
                skip_task_id=str(existing_audit_task["task_id"]) if existing_audit_task else "",
            )
            audit_task = build_audit_task(requirement, audited_tasks, note, paused_tasks, existing_audit_task)
            tasks = insert_audit_task_and_gate_future_tasks(requirement, audit_task)
            quality_report = analyze_task_quality(tasks)
            append_event(
                paths,
                event_type="plan_updated",
                source="sdlc-audit",
                summary=f"插入质量复查任务 {audit_task['task_id']}",
                requirement_id=requirement["requirement_id"],
                task_id=audit_task["task_id"],
                payload={
                    "tasks": tasks,
                    "priority": requirement.get("priority", "normal"),
                    "blocked_reason": requirement.get("blocked_reason", ""),
                    "resolved_change_ids": [],
                    "task_quality": quality_report,
                },
            )
            refresh_materialized_state(paths)

    if formal_audit:
        review_result = review_service.create_task_plan_review(
            paths,
            requirement_id=str(requirement["requirement_id"]),
        )
        _restore_formal_task_statuses(
            paths,
            str(requirement["requirement_id"]),
            preserved_tasks,
            source="sdlc-audit",
        )

    action = "已整理质量复查任务" if existing_audit_task else "已插入质量复查任务"
    print(f"{action}：{requirement['requirement_id']} / {audit_task['task_id']}")
    print(f"复查范围：{audit_scope_text(audited_tasks)}")
    if note:
        print(f"复查说明：{note}")
    if paused_tasks:
        print(f"已暂停进行中任务：{build_impacted_summary(paused_tasks)}")
    print("处理边界：本命令只调整任务队列，不写业务代码、不跑测试、不提交代码。")
    if formal_audit:
        print("正式任务整包已更新；旧任务轮次和证据保持不变。")
        print(f"新任务审核请求：{review_result['request']['review_id']}")
        print("下一步建议：重新完成整套任务审核，通过后用 task 正式入口开工。")
    else:
        print("下一步建议：重新生成完整 task-plan.v2、task.v2 和 task-coverage.v1，")
        print("通过 tasks 正式入口提交整包修订并重新完成任务审核。")
    return 0


def select_done_task_to_fix(
    state: dict[str, object],
    requirement_id: str | None,
    task_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    if task_id:
        requirement, task = resolve_task(state, requirement_id, task_id)
    else:
        candidates = [
            (requirement, task)
            for requirement in state["requirements"].values()  # type: ignore[index]
            for task in requirement["tasks"]
            if task["status"] == "done"
        ]
        recent = recent_task_from_events(state, candidates, sources={"sdlc-task"})
        if recent:
            requirement, task = recent
        else:
            requirement, task = select_single_candidate(
                candidates,
                many_message="多个已完成任务都可能需要修复，请指定需求编号和任务编号：",
                none_message="没有找到已完成的历史任务，不能插入修复任务。",
            )

    status = str(task["status"])
    if status != "done":
        target = f"{requirement['requirement_id']} / {task['task_id']}"
        if status in {"doing", "ready_for_user_check", "test_failed"}:
            raise SdlcError(f"{target} 还属于当前任务反馈，请使用 `$sdlc-task-restore`。", exit_code=1)
        if status == "todo":
            raise SdlcError(f"{target} 还没完成，请继续使用 `$sdlc-task` 推进它。", exit_code=1)
        raise SdlcError(f"{target} 当前状态是 {status}，不能插入历史修复任务。", exit_code=1)
    return requirement, task


def run_fix(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    requirement_id, task_id, feedback = parse_fix_target(args.items)
    if not feedback:
        raise SdlcError("请写清楚这次要修复的历史问题。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement, source_task = select_done_task_to_fix(state, requirement_id, task_id)
        formal_fix = isinstance(source_task.get("task_contract"), dict)
        impacted_tasks = impacted_tasks_after(requirement, source_task)
        if formal_fix:
            fix_contract, preserved_tasks = _revise_formal_task_plan(
                paths,
                state,
                requirement,
                [source_task],
                kind="fix",
                detail=feedback,
            )
            fix_task = {"task_id": fix_contract["task_id"]}
        else:
            fix_task = build_fix_task(requirement, source_task, feedback, impacted_tasks)
            tasks = insert_fix_task_and_gate_future_tasks(requirement, source_task, fix_task)
            quality_report = analyze_task_quality(tasks)
            append_event(
                paths,
                event_type="plan_updated",
                source="sdlc-fix",
                summary=f"插入修复任务 {fix_task['task_id']}，来源 {source_task['task_id']}",
                requirement_id=requirement["requirement_id"],
                task_id=fix_task["task_id"],
                payload={
                    "tasks": tasks,
                    "priority": requirement.get("priority", "normal"),
                    "blocked_reason": requirement.get("blocked_reason", ""),
                    "resolved_change_ids": [],
                    "task_quality": quality_report,
                },
            )
            refresh_materialized_state(paths)

    if formal_fix:
        review_result = review_service.create_task_plan_review(
            paths,
            requirement_id=str(requirement["requirement_id"]),
        )
        _restore_formal_task_statuses(
            paths,
            str(requirement["requirement_id"]),
            preserved_tasks,
            source="sdlc-fix",
        )

    print(f"已插入修复任务：{requirement['requirement_id']} / {fix_task['task_id']}")
    print(f"修复来源：{requirement['requirement_id']} / {source_task['task_id']}")
    print(f"反馈：{feedback}")
    print(f"影响复查：{build_impacted_summary(impacted_tasks)}")
    doing_impacted = [task for task in impacted_tasks if task["status"] == "doing"]
    if doing_impacted:
        print("提醒：当前有进行中任务可能受影响，本命令不会自动暂停它；请先确认是继续当前任务，还是先处理修复任务。")
    if formal_fix:
        print("正式任务整包已更新；旧任务轮次和证据保持不变。")
        print(f"新任务审核请求：{review_result['request']['review_id']}")
        print("下一步建议：重新完成整套任务审核，通过后用 task 正式入口开工。")
    else:
        print("下一步建议：重新生成完整 task-plan.v2、task.v2 和 task-coverage.v1，")
        print("通过 tasks 正式入口提交整包修订并重新完成任务审核。")
    return 0
