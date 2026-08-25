from __future__ import annotations

import argparse
import re
from pathlib import Path

from codex_sdlc.commands.task_cmd import (
    append_task_update,
    run_test_commands,
    run_test_scripts,
    task_test_commands,
    task_test_scripts,
    unique_command_list,
    verification_summary_with_contract,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.task_evidence import read_completed_task_evidence
from codex_sdlc.core.state import (
    append_event,
    current_test_cases_for_task,
    derive_state,
    next_number,
    now_iso,
    refresh_materialized_state,
    refresh_task_runtime_state,
    resolve_requirement,
    resolve_task,
    shorten_text,
    task_contract_gate_message,
    tasks_with_contract_issues,
    verification_ids,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("regression", help="执行需求或任务范围的回归测试")
    parser.add_argument("items", nargs="*", help="可选：current、REQ-001、REQ-001 T-001..T-005 或任务编号")
    parser.set_defaults(func=run)


def is_requirement_id(value: str | None) -> bool:
    return bool(value and value.startswith("REQ-"))


def is_task_token(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"T-\d+(?:\.\.T-\d+)?", value))


def task_id_number(task_id: str) -> int | None:
    match = re.fullmatch(r"T-(\d+)", task_id)
    if not match:
        return None
    return int(match.group(1))


def expand_task_token(token: str) -> list[str]:
    if ".." not in token:
        return [token]
    start_id, end_id = token.split("..", 1)
    start_number = task_id_number(start_id)
    end_number = task_id_number(end_id)
    if start_number is None or end_number is None:
        return [start_id, end_id]
    if start_number > end_number:
        start_number, end_number = end_number, start_number
    width = max(len(start_id.split("-", 1)[1]), len(end_id.split("-", 1)[1]))
    return [f"T-{number:0{width}d}" for number in range(start_number, end_number + 1)]


def regression_ready_requirements(state: dict[str, object]) -> list[dict[str, object]]:
    return [
        requirement
        for requirement in state["requirements"].values()  # type: ignore[index]
        if requirement["status"] in {"done", "active", "doing"} and requirement.get("tasks")
    ]


def select_requirement(state: dict[str, object], requirement_id: str | None) -> dict[str, object]:
    if requirement_id:
        return resolve_requirement(state, requirement_id)

    active = list(state["active_requirements"])  # type: ignore[index]
    if len(active) == 1:
        return active[0]

    ready = regression_ready_requirements(state)
    if len(ready) == 1:
        return ready[0]

    if active or ready:
        candidates = active or ready
        lines = ["有多个可能要回归的需求，请指定需求编号："]
        lines.extend(f"- {item['requirement_id']} [{item['status']}] {item['title']}" for item in candidates)
        raise SdlcError("\n".join(lines), exit_code=1)
    raise SdlcError("当前没有可以回归的需求。", exit_code=1)


def select_tasks(
    state: dict[str, object],
    requirement: dict[str, object],
    task_tokens: list[str],
) -> list[dict[str, object]]:
    if not task_tokens:
        return [task for task in requirement["tasks"] if task["status"] != "closed"]  # type: ignore[index]

    task_ids: list[str] = []
    for token in task_tokens:
        task_ids.extend(expand_task_token(token))

    selected: list[dict[str, object]] = []
    for task_id in task_ids:
        _requirement, task = resolve_task(state, str(requirement["requirement_id"]), task_id)
        if task["status"] == "closed":
            continue
        selected.append(task)
    return selected


def parse_target(state: dict[str, object], items: list[str]) -> tuple[dict[str, object], list[dict[str, object]]]:
    clean_items = [item for item in items if item and item != "current"]
    requirement_id = clean_items[0] if clean_items and is_requirement_id(clean_items[0]) else None
    task_tokens = clean_items[1:] if requirement_id else clean_items
    if any(not is_task_token(token) for token in task_tokens):
        raise SdlcError("回归范围格式不对，请使用 `$sdlc-regression REQ-001` 或 `$sdlc-regression REQ-001 T-001..T-005`。", exit_code=2)
    requirement = select_requirement(state, requirement_id)
    tasks = select_tasks(state, requirement, task_tokens)
    if not tasks:
        raise SdlcError("没有找到可回归的任务。", exit_code=1)
    return requirement, tasks


def regression_case_labels(cases: list[dict[str, object]]) -> list[str]:
    return [f"{case['id']}[{case.get('status', 'active')}]" for case in cases]


def regression_case_ids(cases: list[dict[str, object]]) -> list[str]:
    return [str(case["id"]) for case in cases]


def is_manual_case(case: dict[str, object]) -> bool:
    return str(case.get("status", "")) == "manual_only" or str(case.get("type", "")) == "manual_only"


def split_manual_cases(cases: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    automated: list[dict[str, object]] = []
    manual: list[dict[str, object]] = []
    for case in cases:
        if is_manual_case(case):
            manual.append(case)
        else:
            automated.append(case)
    return automated, manual


def regression_case_descriptions(cases: list[dict[str, object]], *, limit: int = 3) -> list[str]:
    descriptions = [shorten_text(str(case.get("description", case.get("id", ""))), 64) for case in cases]
    if len(descriptions) <= limit:
        return descriptions
    return [*descriptions[:limit], f"还有 {len(descriptions) - limit} 项，见证据明细"]


def task_label(task: dict[str, object]) -> str:
    return f"{task['task_id']}：{shorten_text(str(task.get('title', '未命名任务')), 42)}"


def verification_summary(task: dict[str, object], commands: list[str], scripts: list[str], cases: list[dict[str, object]]) -> str:
    evidence = [*commands, *scripts]
    case_ids = regression_case_ids(cases)
    case_text = f"回归测试项：{'、'.join(case_ids)}；" if case_ids else ""
    task_for_contract = {**task, "coverage_tests": case_ids or task.get("coverage_tests", [])}
    if evidence:
        return verification_summary_with_contract(task_for_contract, f"回归验证通过：{task['task_id']}；{case_text}执行：{'；'.join(evidence)}")
    return verification_summary_with_contract(task_for_contract, f"回归验证待人工确认：{task['task_id']}；{case_text}".rstrip("；"))


def formal_coverage_cases(
    requirement: dict[str, object], task: dict[str, object]
) -> list[dict[str, object]]:
    """从 task-coverage.v1 读取当前正式任务的来源和测试路径。"""

    coverage = requirement.get("task_coverage_contract")
    if not isinstance(coverage, dict) or coverage.get("schema_version") != "task-coverage.v1":
        raise SdlcError("当前需求缺少正式 task-coverage.v1，不能执行回归。", exit_code=1)
    task_id = str(task["task_id"])
    relations: list[str] = []
    test_refs: list[str] = []
    for field in (
        "functional_requirements",
        "design_artifacts",
        "acceptance_criteria",
        "effective_changes",
    ):
        mapping = coverage.get(field)
        if not isinstance(mapping, dict):
            raise SdlcError(f"task-coverage.v1 缺少 {field} 覆盖关系。", exit_code=1)
        for source_id, entry in mapping.items():
            if not isinstance(entry, dict):
                continue
            covered_tasks = {str(item) for item in entry.get("tasks", [])}
            if task_id not in covered_tasks:
                continue
            relations.append(str(source_id))
            if field == "acceptance_criteria":
                test_refs.extend(
                    str(item)
                    for item in entry.get("test_refs", [])
                    if str(item).startswith(f"{task_id}#automated_tests/")
                )
    if not relations:
        raise SdlcError(f"{task_id} 在 task-coverage.v1 中没有覆盖关系，不能执行回归。", exit_code=1)
    if not test_refs:
        raise SdlcError(f"{task_id} 没有绑定验收标准对应的自动测试路径，不能执行回归。", exit_code=1)
    return [
        {
            "id": test_ref,
            "status": "active",
            "description": "覆盖 " + "、".join(dict.fromkeys(relations)),
        }
        for test_ref in dict.fromkeys(test_refs)
    ]


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement, tasks = parse_target(state, args.items)
        legacy_tasks = [
            task for task in tasks if not isinstance(task.get("task_contract"), dict)
        ]
        contract_issues = tasks_with_contract_issues(requirement, legacy_tasks)
        if contract_issues:
            raise SdlcError(task_contract_gate_message(requirement, contract_issues), exit_code=1)
        formal_cases = {
            str(task["task_id"]): formal_coverage_cases(requirement, task)
            for task in tasks
            if isinstance(task.get("task_contract"), dict)
        }
        used_ids = verification_ids(state)
        runtime_evidence_only = all(
            isinstance(task.get("task_contract"), dict) for task in tasks
        )
        failed = False
        manual_only: list[tuple[dict[str, object], list[dict[str, object]]]] = []
        recorded: list[dict[str, str]] = []
        recorded_details: list[dict[str, object]] = []
        failed_items: list[dict[str, object]] = []

        print(f"回归范围：{requirement['requirement_id']}，共 {len(tasks)} 个任务。")
        print("本轮只做回归验证和记录，不改业务代码，也不提交 Git。")
        print("待检查任务：")
        for task in tasks:
            print(f"- {task_label(task)}")

        for task in tasks:
            if isinstance(task.get("task_contract"), dict):
                print()
                print(f"读取任务证据：{requirement['requirement_id']} / {task['task_id']}")
                # task_contract_issues 已经确认覆盖关系存在；这里继续读取当前需求
                # 测试矩阵的有效项，把覆盖编号和关闭轮次证据一起写进回归结果。
                cases = formal_cases[str(task["task_id"])]
                case_ids = regression_case_ids(cases)
                try:
                    evidence = read_completed_task_evidence(
                        paths,
                        requirement_id=str(requirement["requirement_id"]),
                        task_id=str(task["task_id"]),
                    )
                except SdlcError as exc:
                    failed = True
                    failed_items.append(
                        {
                            "task": task,
                            "stage": "任务运行证据",
                            "cases": [],
                            "outputs": [str(exc)],
                        }
                    )
                    print(f"任务运行证据读取失败：{exc}")
                    continue
                verification_id = next_number(used_ids, "VRF")
                used_ids.append(verification_id)
                evidence_summary = (
                    f"回归读取第 {evidence['run_number']} 次任务运行证据通过："
                    f"测试 {evidence['test_count']} 条，人工验收 {evidence['verification_count']} 条，"
                    f"反馈 {evidence['feedback_count']} 条；"
                    f"覆盖当前测试矩阵 {'、'.join(case_ids)}。"
                )
                record = {
                    "verification_id": verification_id,
                    "created_at": now_iso(),
                    "summary": evidence_summary,
                    "file_path": (
                        f".codex-sdlc/requirements/{requirement['folder_name']}"
                        f"/verifications/{verification_id}.md"
                    ),
                }
                recorded.append(record)
                recorded_details.append(
                    {
                        **record,
                        "task_id": str(task["task_id"]),
                        "task_title": str(task.get("title", "")),
                        "commands": list(evidence["commands"]),
                        "case_labels": regression_case_labels(cases),
                        "case_ids": case_ids,
                        "case_descriptions": [
                            f"已读取当前关闭轮次的 {evidence['test_count']} 条测试证据和 "
                            f"{evidence['verification_count']} 条人工验收证据",
                            *regression_case_descriptions(cases),
                        ],
                    }
                )
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status=str(task["status"]),
                    note=str(task.get("note", "")),
                    changed_files=[],
                    commands=list(evidence["commands"]),
                    verifications=[record],
                )
                print(evidence_summary)
                continue
            cases = current_test_cases_for_task(requirement, task)
            automated_cases, manual_cases = split_manual_cases(cases)
            if manual_cases:
                manual_only.append((task, manual_cases))
            commands = task_test_commands(task, state)
            scripts = task_test_scripts(task)
            if not commands and not scripts:
                if automated_cases:
                    manual_only.append((task, automated_cases))
                continue

            print()
            print(f"执行回归：{requirement['requirement_id']} / {task['task_id']}")
            case_labels = regression_case_labels(cases)
            if case_labels:
                print("测试矩阵：" + "、".join(case_labels))
            commands_passed, command_outputs = run_test_commands(paths.root, commands)
            for output in command_outputs:
                print(output)
            if not commands_passed:
                failed = True
                failed_items.append(
                    {
                        "task": task,
                        "stage": "自动测试命令",
                        "cases": automated_cases,
                        "outputs": command_outputs,
                    }
                )
                note = str(task.get("note", ""))
                note = note + ("\n" if note else "") + "回归测试失败：\n" + "\n\n".join(command_outputs)
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status=str(task["status"]),
                    note=note,
                    changed_files=[],
                    commands=commands,
                    verifications=[],
                )
                continue

            scripts_passed, script_outputs = run_test_scripts(paths.root, scripts)
            for output in script_outputs:
                print(output)
            if not scripts_passed:
                failed = True
                failed_items.append(
                    {
                        "task": task,
                        "stage": "可重复测试脚本",
                        "cases": automated_cases,
                        "outputs": script_outputs,
                    }
                )
                note = str(task.get("note", ""))
                note = note + ("\n" if note else "") + "回归测试脚本失败：\n" + "\n\n".join(script_outputs)
                append_task_update(
                    paths,
                    requirement=requirement,
                    task=task,
                    status=str(task["status"]),
                    note=note,
                    changed_files=[],
                    commands=unique_command_list(commands, scripts),
                    verifications=[],
                )
                continue

            verification_id = next_number(used_ids, "VRF")
            used_ids.append(verification_id)
            record = {
                "verification_id": verification_id,
                "created_at": now_iso(),
                "summary": verification_summary(task, commands, scripts, automated_cases),
                "file_path": (
                    f".codex-sdlc/requirements/{requirement['folder_name']}"
                    f"/verifications/{verification_id}.md"
                ),
            }
            detail = {
                **record,
                "task_id": str(task["task_id"]),
                "task_title": str(task.get("title", "")),
                "commands": unique_command_list(commands, scripts),
                "case_labels": regression_case_labels(cases),
                "case_ids": regression_case_ids(automated_cases),
                "case_descriptions": regression_case_descriptions(automated_cases),
            }
            recorded.append(record)
            recorded_details.append(detail)
            append_task_update(
                paths,
                requirement=requirement,
                task=task,
                status=str(task["status"]),
                note=str(task.get("note", "")),
                changed_files=[],
                commands=unique_command_list(commands, scripts),
                verifications=[record],
            )

        if runtime_evidence_only:
            refresh_task_runtime_state(paths)
        else:
            refresh_materialized_state(paths)
        if manual_only:
            append_event(
                paths,
                event_type="regression_manual_pending",
                source="sdlc-regression",
                summary=f"{requirement['requirement_id']} 回归仍有人工或模拟器待确认项",
                requirement_id=requirement["requirement_id"],
                payload={
                    "manual_items": [
                        {
                            "task_id": str(task["task_id"]),
                            "task_title": str(task.get("title", "")),
                            "case_ids": regression_case_ids(cases),
                            "case_descriptions": regression_case_descriptions(cases, limit=8),
                        }
                        for task, cases in manual_only
                    ]
                },
            )
            if runtime_evidence_only:
                refresh_task_runtime_state(paths)
            else:
                refresh_materialized_state(paths)

    print()
    print("本轮回归结果")
    print()
    print("说明：本轮没有改业务代码，也没有提交 Git；只写入 SDLC 验证记录。")
    if failed:
        print()
        print("结论：回归失败，先处理失败项，暂时不要接受需求。")
        print()
        print("失败项：")
        for item in failed_items:
            task = item["task"]
            case_text = "；覆盖 " + "、".join(regression_case_ids(item["cases"])) if item["cases"] else ""
            print(f"- {task_label(task)}：{item['stage']}失败{case_text}。")
        print()
        print("下一步：")
        print("- 历史已完成任务出问题：用 `$sdlc-fix` 插入修复任务。")
        print("- 当前待验收任务出问题：用 `$sdlc-task-restore` 补反馈后返工。")
        print()
        print("证据明细：")
        if recorded:
            print("- 已通过并写入的验证记录：")
            for record in recorded:
                print(f"  - {record['verification_id']}：{record['summary']}")
        for item in failed_items:
            task = item["task"]
            case_labels = regression_case_labels(item["cases"])
            case_text = "；测试矩阵：" + "、".join(case_labels) if case_labels else ""
            print(f"- {task['task_id']}：{item['stage']}失败{case_text}")
        return 1

    if recorded:
        if manual_only:
            print()
            print(
                f"结论：能自动跑的检查都通过了；还有 {len(manual_only)} 个任务需要人工或模拟器确认，"
                f"所以 {requirement['requirement_id']} 还不能算最终验完。"
            )
        else:
            print()
            print(f"结论：本轮自动回归通过，{requirement['requirement_id']} 当前范围没有剩余人工/视觉待验项。")
        print()
        print("已自动通过：")
        for record in recorded_details:
            command_text = "；执行：" + "；".join(record["commands"]) if record["commands"] else ""
            case_text = "；覆盖：" + "、".join(record["case_descriptions"]) if record["case_descriptions"] else ""
            print(f"- {record['task_id']}：{shorten_text(str(record['task_title']), 42)}{case_text}{command_text}")
    if manual_only:
        if not recorded:
            print()
            print(f"结论：本轮没有可自动执行的检查；{requirement['requirement_id']} 需要人工或模拟器确认后才能收口。")
        print()
        print("还需要人工或模拟器确认：")
        for task, cases in manual_only:
            descriptions = regression_case_descriptions(cases)
            case_text = "；重点：" + "、".join(descriptions) if descriptions else ""
            print(f"- {task_label(task)}：没有自动测试命令或可重复脚本，需要按人工/视觉验收点检查{case_text}。")
    if not recorded and not manual_only:
        print()
        print("结论：本轮没有找到可执行或可确认的回归项。")
    print()
    print("下一步：")
    if manual_only:
        print("- 要让 Codex 继续验：告诉 Codex 继续做人工/视觉验收，必要时使用模拟器、电脑操作或 IDE。")
        print("- 你自己验：按上面的人工项检查，通过或发现问题后告诉 Codex 记录结果。")
        print(f"- 全部确认通过后，再执行 `$sdlc-accept {requirement['requirement_id']}`。")
    elif recorded:
        print(f"- 如果你认可本轮结果，下一步可以执行 `$sdlc-accept {requirement['requirement_id']}`。")
    else:
        print("- 请在正式任务合同中补齐测试命令、可重复测试脚本或人工验收点，通过 `tasks` 重新提交并完成 `review` 后再开工。")
    print("- 需要重新查看当前推荐时，执行 `$sdlc-next`。")
    print()
    print("证据明细：")
    if recorded:
        print("- 新增验证记录：")
        for record in recorded:
            print(f"  - {record['verification_id']}：{record['summary']}")
    if manual_only:
        print("- 待人工/视觉验收任务：")
        for task, cases in manual_only:
            case_text = "；回归测试项：" + "、".join(regression_case_ids(cases)) if cases else ""
            print(f"  - {task['task_id']}：{task['title']}{case_text}")
    if not recorded and not manual_only:
        print("- 没有新增验证记录。")
    return 0
