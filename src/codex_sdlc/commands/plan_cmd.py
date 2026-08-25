from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import shlex
from pathlib import Path
import sys
import tempfile
from typing import Callable

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    bind_tasks_to_current_contract,
    build_requirement_points,
    build_test_cases,
    change_ids as state_change_ids,
    compute_next_actions,
    derive_acceptance_for_task,
    derive_state,
    formal_design_refs_for_task,
    formal_requirement_refs_for_task,
    formal_test_refs_for_task,
    next_number,
    refresh_materialized_state,
    resolve_requirement,
    structured_version_labels,
    task_out_of_scope,
    task_test_suggestions,
)
from codex_sdlc.core.task_quality import analyze_task_quality
from codex_sdlc.core.task_contract import import_task_plan_bundle
from codex_sdlc.core.task_coverage_contract import (
    prepare_task_planning_documents,
    verify_task_planning_input_snapshot,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    sha256_file,
    validate_schema_document,
)


def print_current_next_suggestion(paths) -> None:
    state = derive_state(paths)
    next_actions = compute_next_actions(paths, state)
    print(f"下一步建议：{next_actions['primary']}")


def stale_task_plan_review_ids(requirement: dict[str, object]) -> list[str]:
    """规划变化只读取整套任务审核状态，不再触碰旧任务执行包。"""

    review_state = requirement.get("task_plan_review_state")
    reviews = review_state.get("reviews", []) if isinstance(review_state, dict) else []
    return [
        str(review.get("review_id") or "")
        for review in reviews
        if isinstance(review, dict)
        and review.get("is_current") is True
        and review.get("effective_status") == "stale"
        and str(review.get("review_id") or "")
    ]


def _planning_contract_paths(paths, requirement: dict[str, object]) -> tuple[Path, Path, Path]:
    requirement_root = paths.requirements_dir / str(requirement.get("folder_name") or "")
    return (
        requirement_root / "tasks" / "task-plan.v2.json",
        requirement_root / "task-coverage.v1.json",
        requirement_root / "tasks" / ".task-import-receipt.json",
    )


def ensure_mutable_task_plan_contract(paths, requirement: dict[str, object]) -> None:
    """原子导入整包不能被旧细调命令拆开覆盖，必须明确拒绝而不是降级。"""

    _plan_path, _coverage_path, receipt_path = _planning_contract_paths(paths, requirement)
    if receipt_path.exists() or receipt_path.is_symlink():
        raise SdlcError(
            "当前 task-plan.v2 已由原子导入回执固定，plan 细调入口不能拆开覆盖。"
            "请重新生成完整 task-plan.v2、task.v2 和 task-coverage.v1，并通过 tasks 正式入口提交整包修订。",
            exit_code=1,
        )


def _atomic_write_planning_json(path: Path, document: dict[str, object]) -> None:
    """结构化规划文件先写同目录临时文件，再替换正式文件，避免读到半截 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json_text(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _task_ids_for_field(
    tasks: list[dict[str, object]],
    field: str,
    value: str,
) -> list[str]:
    return [
        str(task.get("task_id") or "")
        for task in tasks
        if value in {str(item) for item in (task.get(field) or [])}
        and str(task.get("task_id") or "")
    ]


def sync_mutable_task_plan_contract(paths, requirement: dict[str, object]) -> None:
    """把可变规划事件同步成 task-plan.v2 和覆盖矩阵，不读取或写入旧摘要。"""

    plan_path, coverage_path, receipt_path = _planning_contract_paths(paths, requirement)
    if receipt_path.exists() or receipt_path.is_symlink():
        return
    # task-plan.v2 只保存仍需交付的任务；关闭任务留在事件历史中，不能继续占用
    # 计划清单和覆盖关系，否则 plan-close 对结构化合同不会产生任何可见变化。
    tasks = [
        dict(task)
        for task in requirement.get("tasks", [])
        if isinstance(task, dict) and task.get("status") != "closed"
    ]
    if not tasks:
        return

    existing_plan: dict[str, object] = {}
    if plan_path.is_file() and not plan_path.is_symlink():
        value = json.loads(plan_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            existing_plan = value
    task_ids = [str(task["task_id"]) for task in tasks]
    existing_mapping = existing_plan.get("mapping")
    mapping = {
        str(key): str(value)
        for key, value in existing_mapping.items()
        if isinstance(existing_mapping, dict) and str(value) in task_ids
    } if isinstance(existing_mapping, dict) else {}
    mapped_ids = set(mapping.values())
    for task_id in task_ids:
        if task_id not in mapped_ids:
            mapping[f"task-{task_id.removeprefix('T-')}"] = task_id
    plan = {
        "schema_version": "task-plan.v2",
        "requirement_id": str(requirement["requirement_id"]),
        "producer_run_id": str(existing_plan.get("producer_run_id") or "sdlc-plan"),
        "input_hashes": dict(existing_plan.get("input_hashes") or {}),
        "tasks": task_ids,
        "dependencies": [
            {"from": str(dependency), "to": str(task["task_id"])}
            for task in tasks
            for dependency in (task.get("depends_on") or [])
        ],
        "mapping": mapping,
    }
    if existing_plan.get("code_evidence") is not None:
        plan["code_evidence"] = deepcopy(existing_plan["code_evidence"])
    validate_schema_document(plan, schema_name="task-plan.v2")

    structured = requirement.get("structured") if isinstance(requirement.get("structured"), dict) else {}
    functional_ids = [
        str(item.get("id") or "")
        for item in structured.get("requirement_points", [])
        if isinstance(item, dict) and str(item.get("id") or "").startswith("FR-")
    ]
    design_ids = sorted(
        {
            str(item)
            for task in tasks
            for item in (task.get("design_refs") or [])
            if re.fullmatch(r"(?:DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}", str(item))
        }
    )
    acceptance_ids = [
        str(item.get("id") or "")
        for item in structured.get("acceptance_points", [])
        if isinstance(item, dict) and str(item.get("id") or "").startswith("AC-")
    ]
    change_ids = sorted(
        {
            str(item)
            for task in tasks
            for item in (task.get("coverage_change_ids") or [])
            if re.fullmatch(r"CHG-[0-9]{3,}", str(item))
        }
    )
    coverage = {
        "schema_version": "task-coverage.v1",
        "requirement_id": str(requirement["requirement_id"]),
        "functional_requirements": {
            item: {"tasks": matched, "status": "planned"}
            for item in functional_ids
            if (matched := _task_ids_for_field(tasks, "coverage_points", item))
        },
        "design_artifacts": {
            item: {"tasks": matched}
            for item in design_ids
            if (matched := _task_ids_for_field(tasks, "design_refs", item))
        },
        "acceptance_criteria": {
            item: {
                "tasks": matched,
                "test_refs": [f"{task_id}#automated_tests/0" for task_id in matched],
            }
            for item in acceptance_ids
            if (matched := _task_ids_for_field(tasks, "coverage_acceptance", item))
        },
        "effective_changes": {
            item: {"tasks": matched}
            for item in change_ids
            if (matched := _task_ids_for_field(tasks, "coverage_change_ids", item))
        },
        "no_development_items": [],
    }
    validate_schema_document(coverage, schema_name="task-coverage.v1")
    _atomic_write_planning_json(plan_path, plan)
    _atomic_write_planning_json(coverage_path, coverage)


def refresh_planning_state(paths, requirement_id: str) -> tuple[dict[str, object], dict[str, object], list[str]]:
    """统一刷新任务计划、覆盖投影和审核状态，返回已经失效的当前审核。"""

    state = refresh_materialized_state(paths)
    requirement = resolve_requirement(state, requirement_id)
    sync_mutable_task_plan_contract(paths, requirement)
    state = derive_state(paths)
    requirement = resolve_requirement(state, requirement_id)
    return state, requirement, stale_task_plan_review_ids(requirement)


class _ValidatedSnapshotPath:
    """路径进入 T-021 项目锁后先核对原输入，再返回已经完整校验的仓库外快照。"""

    def __init__(self, snapshot_path: Path, validate: Callable[[], None]) -> None:
        self._snapshot_path = snapshot_path
        self._validate = validate

    def __fspath__(self) -> str:
        self._validate()
        return str(self._snapshot_path)


class _TaskPlanningInputChanged(SdlcError):
    """区分校验后输入替换，命令入口可直接返回稳定的非零退出码。"""


def _remove_new_empty_directory_tree(path: Path, *, existed_before: bool) -> None:
    """输入守卫拒绝时只清理由当前调用新建的空事务目录，不碰任何恢复文件。"""

    if existed_before or not path.is_dir() or path.is_symlink():
        return
    directories = sorted(
        (item for item in path.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    try:
        for directory in directories:
            directory.rmdir()
        path.rmdir()
    except OSError:
        # 目录里一旦存在事务文件就保留给 T-021 恢复协议处理，不能扩大清理范围。
        return


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    tasks_parser = subparsers.add_parser("tasks", help="原子导入正式任务计划和任务合同")
    tasks_parser.add_argument("requirement_id", help="需求编号")
    tasks_parser.add_argument("--plan-file", required=True, help="task-plan.v2.json 文件")
    tasks_parser.add_argument("--tasks-dir", required=True, help="包含 <client_key>.task.v2.json 的目录")
    tasks_parser.add_argument("--coverage-file", required=True, help="task-coverage.v1.json 文件")
    tasks_parser.set_defaults(func=run_tasks_finalize)

    parser = subparsers.add_parser("plan", help="补齐或调整需求任务计划")
    parser.add_argument("requirement_id", help="需求编号")
    parser.add_argument("--task", action="append", default=[], help="追加一个新任务标题；使用结构化字段时一次只能追加一条")
    add_explicit_task_fields(parser)
    parser.add_argument("--reorder", default="", help="按逗号给出新的任务顺序，例如 T-002,T-001")
    parser.add_argument("--depends", action="append", default=[], help="设置依赖，例如 T-003:T-001,T-002")
    parser.add_argument("--close", action="append", default=[], help="关闭一个任务，可重复传入")
    parser.add_argument("--priority", choices=["low", "normal", "high"], default=None, help="需求优先级")
    parser.set_defaults(func=run)

    add_task_parser = subparsers.add_parser("plan-add-task", help="给需求追加任务")
    add_task_parser.add_argument("requirement_id", help="需求编号")
    add_task_parser.add_argument("task_titles", nargs="+", help="新增任务标题；每次命令只追加一条")
    add_explicit_task_fields(add_task_parser)
    add_task_parser.set_defaults(func=run_add_task)

    amend_parser = subparsers.add_parser("plan-amend-task", help="修改已有未完成任务的目标、测试项、验收点或依赖")
    amend_parser.add_argument("requirement_id", help="需求编号")
    amend_parser.add_argument("task_id", help="任务编号")
    amend_parser.add_argument("--title", default="", help="新的任务标题")
    amend_parser.add_argument("--summary", default="", help="新的任务摘要或目标")
    amend_parser.add_argument("--note", action="append", default=[], help="追加到任务说明的内容，可重复传入")
    amend_parser.add_argument("--test-item", action="append", default=[], help="追加自动测试项，可重复传入")
    amend_parser.add_argument("--manual-check", action="append", default=[], help="追加人工验收点，可重复传入")
    amend_parser.add_argument("--replace-test-items", action="store_true", help="用本次 --test-item 覆盖旧自动测试项")
    amend_parser.add_argument("--replace-manual-checks", action="store_true", help="用本次 --manual-check 覆盖旧人工验收点")
    amend_parser.add_argument("--depends", default=None, help="覆盖依赖任务，逗号分隔；传空字符串表示清空依赖")
    amend_parser.set_defaults(func=run_amend_task)

    reorder_parser = subparsers.add_parser("plan-reorder", help="重排需求任务顺序")
    reorder_parser.add_argument("requirement_id", help="需求编号")
    reorder_parser.add_argument("order", help="新的任务顺序，例如 T-002,T-001")
    reorder_parser.set_defaults(func=run_reorder)

    depends_parser = subparsers.add_parser("plan-depends", help="设置任务依赖")
    depends_parser.add_argument("requirement_id", help="需求编号")
    depends_parser.add_argument("dependency_rules", nargs="+", help="依赖规则，例如 T-003:T-001,T-002")
    depends_parser.set_defaults(func=run_depends)

    close_parser = subparsers.add_parser("plan-close", help="关闭需求中的任务")
    close_parser.add_argument("requirement_id", help="需求编号")
    close_parser.add_argument("task_ids", nargs="+", help="要关闭的任务编号")
    close_parser.set_defaults(func=run_close)

    priority_parser = subparsers.add_parser("plan-priority", help="调整需求优先级")
    priority_parser.add_argument("requirement_id", help="需求编号")
    priority_parser.add_argument("priority", choices=["low", "normal", "high"], help="需求优先级")
    priority_parser.set_defaults(func=run_priority)

    change_plan_parser = subparsers.add_parser("change-plan", help="规划待处理需求变更")
    change_plan_parser.add_argument("requirement_id", help="需求编号")
    change_plan_parser.add_argument("--change", default="", help="指定要规划的变更编号")
    change_plan_parser.add_argument("--task", action="append", default=[], help="Codex 模型拆出的任务建议，可重复传入；格式为“标题”或“标题||摘要”")
    change_plan_parser.add_argument("--acceptance", action="append", default=[], help="补充到该变更的验收或回归要求，可重复传入")
    change_plan_parser.set_defaults(func=run_change_plan)


def add_explicit_task_fields(parser: argparse.ArgumentParser) -> None:
    """追加任务必须由模型显式给出合同字段，CLI 不从标题推断。"""

    parser.add_argument("--summary", default="", help="任务目标摘要")
    parser.add_argument("--coverage", action="append", default=[], help="覆盖的 FR 编号，可重复传入")
    parser.add_argument("--test-item", action="append", default=[], help="自动测试项，可重复传入")
    parser.add_argument("--manual-check", action="append", default=[], help="人工验收点，可重复传入")
    parser.add_argument("--task-kind", default="generic", help="显式任务类型")
    parser.add_argument("--model-tier", choices=("low", "medium", "high"), default="medium", help="显式模型档位")


def normalized_args(
    args: argparse.Namespace,
    *,
    task: list[str] | None = None,
    reorder: str = "",
    depends: list[str] | None = None,
    close: list[str] | None = None,
    priority: str | None = None,
) -> argparse.Namespace:
    args.task = task or []
    args.reorder = reorder
    args.depends = depends or []
    args.close = close or []
    args.priority = priority
    return args


def clean_text(value: object) -> str:
    return str(value or "").strip()


def clean_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [clean_text(item) for item in value if clean_text(item)]
    clean = clean_text(value)
    return [clean] if clean else []


def clean_file_hints(*values: object) -> list[str]:
    files: list[str] = []
    for value in values:
        files.extend(clean_list(value))
    return list(dict.fromkeys(files))


def load_tasks_package(package_file: str) -> dict[str, object]:
    path = Path(package_file).expanduser()
    if not path.exists():
        raise SdlcError(f"任务清单 JSON 包不存在：{path}", exit_code=1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SdlcError(f"任务清单 JSON 包不是合法 JSON：第 {exc.lineno} 行 {exc.msg}", exit_code=1) from exc
    if not isinstance(data, dict):
        raise SdlcError("任务清单 JSON 包必须是对象。", exit_code=1)
    return data


def normalized_task_id(index: int, value: object) -> str:
    clean = clean_text(value).upper()
    if re.fullmatch(r"T-\d{3}", clean):
        return clean
    return f"T-{index:03d}"


def derive_test_ids_for_requirements(requirement: dict[str, object], coverage_points: list[str]) -> list[str]:
    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    acceptance_by_fr: set[str] = set()
    for acceptance in structured.get("acceptance_points", []):  # type: ignore[union-attr]
        refs = [str(item) for item in acceptance.get("requirement_ids", [])]  # type: ignore[union-attr]
        if any(ref in coverage_points for ref in refs):
            acceptance_by_fr.add(str(acceptance.get("id", "")))
    test_ids: list[str] = []
    for case in structured.get("test_cases", []):  # type: ignore[union-attr]
        refs = [str(item) for item in case.get("acceptance_ids", [])]  # type: ignore[union-attr]
        if any(ref in acceptance_by_fr for ref in refs):
            test_ids.append(str(case.get("id", "")))
    return [item for item in test_ids if item]


def derive_acceptance_ids_for_requirements(requirement: dict[str, object], coverage_points: list[str]) -> list[str]:
    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    selected: list[str] = []
    for acceptance in structured.get("acceptance_points", []):  # type: ignore[union-attr]
        if not isinstance(acceptance, dict):
            continue
        refs = [str(item) for item in acceptance.get("requirement_ids", [])]
        acceptance_id = str(acceptance.get("id", ""))
        if acceptance_id and any(ref in coverage_points for ref in refs) and acceptance_id not in selected:
            selected.append(acceptance_id)
    return selected


def derive_business_rules_for_task(requirement: dict[str, object], coverage_points: list[str]) -> list[str]:
    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    rules: list[str] = []
    for point in structured.get("requirement_points", []):  # type: ignore[union-attr]
        if not isinstance(point, dict) or str(point.get("id", "")) not in coverage_points:
            continue
        rules.extend(clean_list(point.get("rules")))
        rules.extend(clean_list(point.get("permissions")))
        rules.extend(clean_list(point.get("exceptions")))
        rules.extend(clean_list(point.get("boundaries")))
    rules.extend(clean_list(structured.get("business_rules")))
    return list(dict.fromkeys(rules))


def normalize_tasks_package(requirement: dict[str, object], data: dict[str, object]) -> list[dict[str, object]]:
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise SdlcError("任务清单 JSON 包至少需要 1 个任务。", exit_code=1)

    structured = requirement.get("structured", {}) if isinstance(requirement.get("structured"), dict) else {}
    requirement_point_ids = {
        str(point.get("id", ""))
        for point in structured.get("requirement_points", [])  # type: ignore[union-attr]
        if str(point.get("id", "")).strip()
    }
    test_case_ids = {
        str(case.get("id", ""))
        for case in structured.get("test_cases", [])  # type: ignore[union-attr]
        if str(case.get("id", "")).strip()
    }
    acceptance_point_ids = {
        str(point.get("id", ""))
        for point in structured.get("acceptance_points", [])  # type: ignore[union-attr]
        if str(point.get("id", "")).strip()
    }
    effective_change_ids = {
        str(change.get("change_id", ""))
        for change in structured.get("effective_changes", [])  # type: ignore[union-attr]
        if isinstance(change, dict) and str(change.get("change_id", "")).strip()
    }
    tasks: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    for index, raw_item in enumerate(raw_tasks, start=1):
        item = raw_item if isinstance(raw_item, dict) else {"title": clean_text(raw_item)}
        task_id = normalized_task_id(index, item.get("id") or item.get("task_id"))
        if task_id in seen_task_ids:
            raise SdlcError(f"任务编号重复：{task_id}", exit_code=1)
        title = clean_text(item.get("title"))
        summary = clean_text(item.get("summary") or item.get("goal"))
        if not title or not summary:
            raise SdlcError(f"{task_id} 缺少任务标题或目标。", exit_code=1)
        coverage_points = clean_list(item.get("coverage_points") or item.get("requirement_ids") or item.get("fr_ids"))
        unknown_points = [point_id for point_id in coverage_points if point_id not in requirement_point_ids]
        if not coverage_points or unknown_points:
            raise SdlcError(f"{task_id} 需要关联有效的功能需求编号。", exit_code=1)
        coverage_tests = clean_list(item.get("coverage_tests") or item.get("test_ids") or item.get("tc_ids"))
        unknown_tests = [test_id for test_id in coverage_tests if test_id not in test_case_ids]
        if unknown_tests:
            raise SdlcError(f"{task_id} 关联了不存在的测试用例：{', '.join(unknown_tests)}", exit_code=1)
        coverage_acceptance = clean_list(
            item.get("coverage_acceptance")
            or item.get("acceptance_ids")
            or item.get("ac_ids")
            or item.get("coverage_acceptance_points")
        )
        unknown_acceptance = [acceptance_id for acceptance_id in coverage_acceptance if acceptance_point_ids and acceptance_id not in acceptance_point_ids]
        if unknown_acceptance:
            raise SdlcError(f"{task_id} 关联了不存在的验收标准：{', '.join(unknown_acceptance)}", exit_code=1)
        coverage_change_ids = clean_list(item.get("coverage_change_ids") or item.get("change_ids"))
        unknown_changes = [change_id for change_id in coverage_change_ids if change_id not in effective_change_ids]
        if unknown_changes:
            raise SdlcError(f"{task_id} 关联了不存在的生效变更：{', '.join(unknown_changes)}", exit_code=1)
        acceptance_feedback = [
            deepcopy(feedback)
            for feedback in (item.get("acceptance_feedback") or [])
            if isinstance(feedback, dict)
        ]
        test_items = clean_list(item.get("test_items"))
        manual_checks = clean_list(item.get("manual_checks"))
        business_rules = clean_list(item.get("business_rules") or item.get("important_rules") or item.get("rules"))
        context_files = clean_file_hints(
            item.get("context_files"),
            item.get("read_files"),
            item.get("reference_files"),
            item.get("source_files"),
        )
        output_files = clean_file_hints(
            item.get("output_files"),
            item.get("target_files"),
            item.get("expected_files"),
            item.get("likely_files"),
        )
        related_files = clean_file_hints(
            item.get("related_files"),
            item.get("files"),
            item.get("file_hints"),
        )
        task = {
            "task_id": task_id,
            "source_task_id": clean_text(item.get("source_task_id")),
            "subtasks": item.get("subtasks", []) if isinstance(item.get("subtasks", []), list) else [],
            "title": title,
            "summary": summary,
            "status": clean_text(item.get("status") or "todo"),
            "depends_on": clean_list(item.get("depends_on")),
            "changed_files": clean_list(item.get("changed_files")),
            "context_files": context_files,
            "output_files": output_files,
            "related_files": related_files,
            "commands": clean_list(item.get("commands")),
            "test_items": test_items,
            "test_commands": clean_list(item.get("test_commands")),
            "test_scripts": clean_list(item.get("test_scripts")),
            "manual_checks": manual_checks,
            "verifications": [],
            "note": clean_text(item.get("note")),
            "business_rules": business_rules,
            "coverage_points": coverage_points,
            "coverage_change_ids": coverage_change_ids,
            "coverage_acceptance": coverage_acceptance,
            "coverage_tests": coverage_tests,
            "acceptance_feedback": acceptance_feedback,
            "formal_requirement_refs": clean_list(item.get("formal_requirement_refs") or item.get("requirement_refs")),
            "formal_design_refs": clean_list(item.get("formal_design_refs") or item.get("design_refs")),
            "formal_test_refs": clean_list(item.get("formal_test_refs") or item.get("test_refs") or item.get("test_matrix_refs")),
            "out_of_scope": clean_list(item.get("out_of_scope") or item.get("not_in_scope") or item.get("non_goals")),
            "test_suggestions": clean_list(item.get("test_suggestions") or item.get("test_recommendations") or item.get("test_advice")),
            "task_kind": clean_text(item.get("task_kind") or "generic"),
            "model_tier": clean_text(item.get("model_tier") or "medium"),
            "query_terms": clean_list(item.get("query_terms")),
            "symbols": clean_list(item.get("symbols")),
            "output_symbols": clean_list(item.get("output_symbols")),
            "output_search_terms": clean_list(item.get("output_search_terms")),
            "replaces": clean_list(item.get("replaces")),
            "applies_to": clean_list(item.get("applies_to")),
            "lesson_ids": clean_list(item.get("lesson_ids")),
        }
        # 模型任务文件没有反馈合同时必须保留缺失状态，后续任务运行门禁会明确拒绝。
        # 只有模型明确写入 feedback.v1 和 none，才能表示当前任务确认没有验收反馈。
        for feedback_field in ("feedback_contract_version", "feedback_state"):
            if feedback_field in item:
                task[feedback_field] = clean_text(item.get(feedback_field))
        tasks.append(task)
        seen_task_ids.add(task_id)

    valid_task_ids = {str(task["task_id"]) for task in tasks}
    for task in tasks:
        unknown_depends = [task_id for task_id in task.get("depends_on", []) if task_id not in valid_task_ids]
        if unknown_depends:
            raise SdlcError(f"{task['task_id']} 依赖了不存在的任务：{', '.join(unknown_depends)}", exit_code=1)
    return tasks


def run_tasks_finalize(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    events_sha256 = sha256_file(paths.events_file)
    state_before_import = derive_state(paths)
    if sha256_file(paths.events_file) != events_sha256:
        raise SdlcError(
            "任务规划状态在读取时发生变化，请重新执行命令。",
            exit_code=1,
        )
    requirement_before_import = resolve_requirement(
        state_before_import,
        args.requirement_id,
    )
    folder_name = str(requirement_before_import.get("folder_name") or "")
    if not folder_name:
        raise SdlcError("当前正式需求缺少稳定目录，不能导入任务计划。", exit_code=1)
    task_review_state = requirement_before_import.get("task_plan_review_state")
    current_reviews = [
        review
        for review in (
            task_review_state.get("reviews", [])
            if isinstance(task_review_state, dict)
            else []
        )
        if isinstance(review, dict) and review.get("is_current") is True
    ]
    allow_revision = (
        len(current_reviews) == 1
        and current_reviews[0].get("request_status") == "completed"
        and current_reviews[0].get("recorded_status") == "needs_fix"
        and current_reviews[0].get("effective_status") == "needs_fix"
    )
    effective_change_ids = {
        str(change.get("change_id") or "")
        for change in requirement_before_import.get("changes", [])
        if isinstance(change, dict)
        and change.get("status") in {"effective", "accepted", "planned", "resolved", "verified"}
    }
    structured = requirement_before_import.get("structured")
    if isinstance(structured, dict):
        effective_change_ids.update(
            str(change.get("change_id") or "")
            for change in structured.get("effective_changes", [])
            if isinstance(change, dict)
        )
    effective_change_ids.discard("")
    existing_tasks = [
        task
        for requirement in state_before_import.get("requirements", {}).values()
        if isinstance(requirement, dict)
        and not (
            allow_revision
            and requirement.get("requirement_id")
            == requirement_before_import.get("requirement_id")
        )
        for task in requirement.get("tasks", [])
        if isinstance(task, dict)
    ]
    prepared = prepare_task_planning_documents(
        paths,
        requirement_id=str(requirement_before_import["requirement_id"]),
        requirement_root=paths.requirements_dir / folder_name,
        plan_file=Path(args.plan_file),
        tasks_dir=Path(args.tasks_dir),
        coverage_file=Path(args.coverage_file),
        effective_change_ids=effective_change_ids,
        existing_tasks=existing_tasks,
        expected_events_sha256=events_sha256,
    )
    # 计划、任务和覆盖都使用同一份已校验快照；路径在 T-021 持有项目锁后
    # 还会核对原输入、正式索引、事件和代码证据，变化时不进入写入阶段。
    with tempfile.TemporaryDirectory(prefix="codex-sdlc-task-planning-") as temp_dir:
        snapshot_root = Path(temp_dir)
        prepared_plan_file = snapshot_root / "task-plan.v2.json"
        prepared_tasks_dir = snapshot_root / "tasks"
        prepared_coverage_file = snapshot_root / "task-coverage.v1.json"
        prepared_plan_file.write_text(
            canonical_json_text(prepared["plan"]),
            encoding="utf-8",
        )
        prepared_tasks_dir.mkdir()
        for raw_task in prepared["tasks"]:  # type: ignore[union-attr]
            task = dict(raw_task)
            client_key = str(task.get("client_key") or "")
            (prepared_tasks_dir / f"{client_key}.task.v2.json").write_text(
                canonical_json_text(task),
                encoding="utf-8",
            )
        prepared_coverage_file.write_text(
            canonical_json_text(prepared["coverage"]),
            encoding="utf-8",
        )

        def validate_original_inputs() -> None:
            try:
                verify_task_planning_input_snapshot(
                    paths,
                    requirement_root=paths.requirements_dir / folder_name,
                    plan_file=Path(args.plan_file),
                    tasks_dir=Path(args.tasks_dir),
                    coverage_file=Path(args.coverage_file),
                    expected_snapshot=prepared["source_snapshot"],  # type: ignore[arg-type]
                    expected_reference_index_sha256=str(
                        prepared["reference_index_sha256"]
                    ),
                    expected_events_sha256=str(prepared["events_sha256"]),
                    evidence=prepared["plan"]["code_evidence"],  # type: ignore[index,arg-type]
                )
            except SdlcError as exc:
                raise _TaskPlanningInputChanged(
                    exc.message,
                    exit_code=exc.exit_code,
                ) from exc

        task_transaction_root = paths.sdlc_dir / "task-import-transactions"
        transaction_root_existed = task_transaction_root.exists()
        try:
            result = import_task_plan_bundle(
                paths,
                requirement_id=args.requirement_id,
                plan_file=_ValidatedSnapshotPath(
                    prepared_plan_file,
                    validate_original_inputs,
                ),
                tasks_dir=_ValidatedSnapshotPath(
                    prepared_tasks_dir,
                    validate_original_inputs,
                ),
                coverage_file=_ValidatedSnapshotPath(
                    prepared_coverage_file,
                    validate_original_inputs,
                ),
                allow_revision=allow_revision,
            )
        except _TaskPlanningInputChanged as exc:
            _remove_new_empty_directory_tree(
                task_transaction_root,
                existed_before=transaction_root_existed,
            )
            print(exc.message, file=sys.stderr)
            return exc.exit_code
        except SdlcError:
            _remove_new_empty_directory_tree(
                task_transaction_root,
                existed_before=transaction_root_existed,
            )
            raise
    # 文档优先正式目录已经由任务事务写完；这里只刷新全局状态和 SQLite，
    # 不能调用旧渲染器再次解释并覆盖任务 Markdown。
    from codex_sdlc.core.state import refresh_start_transaction_state

    state = refresh_start_transaction_state(
        paths,
        committed_requirement_id=result.requirement_id,
    )
    requirement = resolve_requirement(state, result.requirement_id)
    action = "修订" if allow_revision and not result.duplicate else "导入"
    print(f"已{action}正式任务计划：{result.requirement_id}")
    print(f"任务数量：{len(result.mapping)}")
    print(
        "任务目录："
        + result.task_directory
    )
    print("覆盖文件：" + result.coverage_file)
    planning_evidence = prepared["plan"].get("code_evidence")  # type: ignore[union-attr]
    if isinstance(planning_evidence, dict) and planning_evidence.get("evidence_sha256"):
        print("规划代码证据哈希：" + str(planning_evidence["evidence_sha256"]))
    print("编号映射：")
    for client_key, task_id in sorted(result.mapping.items()):
        print(f"- {client_key} -> {task_id}")
    if result.duplicate:
        print("导入结果：正式任务合同内容未变化，已返回原编号映射")
    blocked = [
        task["task_id"]
        for task in requirement.get("tasks", [])
        if task.get("blocking_conditions")
    ]
    if blocked:
        print("存在阻塞条件的任务：" + "、".join(blocked))
        print("请先清空阻塞条件，再创建任务审核请求。")
    else:
        task_plan_path = f"{result.task_directory}/task-plan.v2.json"
        current_review_state = requirement.get("task_plan_review_state")
        review_ids = [
            str(review.get("review_id") or "")
            for review in (
                current_review_state.get("reviews", [])
                if isinstance(current_review_state, dict)
                else []
            )
            if isinstance(review, dict)
        ]
        latest_review_number = max(
            (
                int(match.group(1))
                for review_id in review_ids
                if (match := re.fullmatch(r"REV-([0-9]+)", review_id))
                is not None
            ),
            default=0,
        )
        next_review_id = f"REV-{latest_review_number + 1:03d}"
        print(
            "下一步：创建整套任务独立审核："
            "codex-sdlc review create "
            f"--review-id {next_review_id} "
            "--stage task_plan "
            f"--owner {result.requirement_id} "
            f"--input {shlex.quote(task_plan_path)}"
        )
    return 0


def run_add_task(args: argparse.Namespace) -> int:
    return run(normalized_args(args, task=args.task_titles))


def run_amend_task(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement = resolve_requirement(state, args.requirement_id)
        ensure_mutable_task_plan_contract(paths, requirement)
        raise_for_draft_changes(requirement)
        raise_for_effective_changes(requirement, prefix="普通任务目标修改不会直接处理")
        tasks = build_initial_tasks(requirement)
        saved_coverage = {
            str(item.get("task_id", "")): [str(point) for point in (item.get("coverage_points") or []) if str(point).strip()]
            for item in tasks
        }
        task = next((item for item in tasks if item["task_id"] == args.task_id), None)
        if task is None:
            raise SdlcError(f"{requirement['requirement_id']} 没有找到任务 `{args.task_id}`。", exit_code=1)
        if task.get("status") in {"done", "closed"}:
            raise SdlcError(
                f"{requirement['requirement_id']} / {args.task_id} 已经是 {task['status']}，不能直接改目标；历史问题请用 `$sdlc-fix`。",
                exit_code=1,
            )

        if args.title.strip():
            task["title"] = args.title.strip()
        if args.summary.strip():
            task["summary"] = args.summary.strip()
        if args.depends is not None:
            task["depends_on"] = [item.strip() for item in args.depends.split(",") if item.strip()]
        if args.note:
            existing_note = str(task.get("note", "")).strip()
            appended_note = "\n".join(item.strip() for item in args.note if item.strip())
            task["note"] = "\n".join(item for item in [existing_note, appended_note] if item)
        incoming_test_items = [item.strip() for item in args.test_item if item.strip()]
        incoming_manual_checks = [item.strip() for item in args.manual_check if item.strip()]
        if args.replace_test_items:
            task["test_items"] = unique_list(incoming_test_items)
        elif incoming_test_items:
            task["test_items"] = unique_list([*list(task.get("test_items", [])), *incoming_test_items])
        if args.replace_manual_checks:
            task["manual_checks"] = unique_list(incoming_manual_checks)
        elif incoming_manual_checks:
            task["manual_checks"] = unique_list([*list(task.get("manual_checks", [])), *incoming_manual_checks])

        validate_task_dependencies(tasks)
        bind_tasks_to_current_contract(requirement, tasks, force=True)
        restore_existing_coverage_points(tasks, saved_coverage)
        test_matrix_version = structured_version_labels(requirement)["test_matrix_version"]
        test_cases = refresh_task_coverage_tests(requirement, tasks)
        quality_report = analyze_task_quality(tasks)
        append_event(
            paths,
            event_type="plan_updated",
            source="sdlc-plan-amend-task",
            summary=f"更新任务 {args.task_id} 的目标和验收",
            requirement_id=requirement["requirement_id"],
            task_id=args.task_id,
            payload={
                "tasks": tasks,
                "priority": requirement.get("priority", "normal"),
                "blocked_reason": "",
                "resolved_change_ids": [],
                "task_quality": quality_report,
                "test_matrix_version": test_matrix_version,
                "test_cases": test_cases,
            },
        )
        _state, requirement, stale_ids = refresh_planning_state(
            paths,
            str(requirement["requirement_id"]),
        )

    print(f"已更新任务目标：{requirement['requirement_id']} / {args.task_id}")
    if args.title.strip():
        print(f"- 标题：{args.title.strip()}")
    if args.summary.strip():
        print(f"- 目标：{args.summary.strip()}")
    if args.depends is not None:
        print("- 依赖：" + ("、".join(task.get("depends_on", [])) or "无"))
    if args.replace_test_items:
        print("- 自动测试项：已替换")
    if args.replace_manual_checks:
        print("- 人工验收点：已替换")
    if stale_ids:
        print("已失效的整套任务审核：" + "、".join(stale_ids))
    print_current_next_suggestion(paths)
    return 0


def run_reorder(args: argparse.Namespace) -> int:
    return run(normalized_args(args, reorder=args.order))


def run_depends(args: argparse.Namespace) -> int:
    return run(normalized_args(args, depends=args.dependency_rules))


def run_close(args: argparse.Namespace) -> int:
    return run(normalized_args(args, close=args.task_ids))


def run_priority(args: argparse.Namespace) -> int:
    return run(normalized_args(args, priority=args.priority))


def unique_list(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result













def normalize_added_task_title(title: str) -> str:
    return title.strip()


def added_task_change_redirects(
    requirement: dict[str, object],
    created_tasks: list[dict[str, object]],
    *,
    allow_broad_scope_tasks: bool = False,
) -> list[tuple[dict[str, object], str]]:
    """新增任务只检查显式覆盖字段，不读取标题含义。"""

    redirects: list[tuple[dict[str, object], str]] = []
    for task in created_tasks:
        coverage_points = [str(item) for item in task.get("coverage_points", []) if str(item).strip()]
        if not coverage_points:
            redirects.append((task, "这条任务没有显式绑定当前生效需求点，需要先补齐 coverage_points。"))
    return redirects


def draft_changes(requirement: dict[str, object]) -> list[dict[str, object]]:
    return [item for item in requirement["changes"] if item["status"] == "draft"]  # type: ignore[index]


def effective_unplanned_changes(requirement: dict[str, object]) -> list[dict[str, object]]:
    # pending 是旧版本留下的状态；在新流程里按“已生效但未规划”兼容处理。
    return [item for item in requirement["changes"] if item["status"] in {"effective", "pending"}]  # type: ignore[index]


def change_plan_task_command(requirement_id: str, change_id: str) -> str:
    return f'$sdlc-change-plan {requirement_id} --change {change_id} --task "任务标题||任务目标"'


def raise_for_draft_changes(requirement: dict[str, object]) -> None:
    changes = draft_changes(requirement)
    if not changes:
        return
    first = changes[0]
    lines = [
        f"{requirement['requirement_id']} 还有待确认需求变化，不能直接规划或开工。",
        f"请先执行 `$sdlc-change-accept {requirement['requirement_id']} {first['change_id']}` 确认是否写入当前生效版本。",
        "待确认变更：",
    ]
    lines.extend(f"- {item['change_id']}：{item['summary']}" for item in changes)
    raise SdlcError("\n".join(lines), exit_code=1)


def raise_for_effective_changes(requirement: dict[str, object], *, prefix: str) -> None:
    changes = effective_unplanned_changes(requirement)
    if not changes:
        return
    first = changes[0]
    lines = [
        f"{requirement['requirement_id']} 还有已生效但未规划需求变化，{prefix}。",
        f"请先执行 `{change_plan_task_command(str(requirement['requirement_id']), str(first['change_id']))}` 规划变更。",
        "待规划变更：",
    ]
    lines.extend(f"- {item['change_id']}：{item['summary']}" for item in changes)
    raise SdlcError("\n".join(lines), exit_code=1)


def build_initial_tasks(requirement: dict[str, object]) -> list[dict[str, object]]:
    raw_tasks = requirement["tasks"]  # type: ignore[index]
    if raw_tasks:
        return [
            {
                "task_id": task["task_id"],
                "source_task_id": task.get("source_task_id", ""),
                "title": task["title"],
                "summary": task["summary"],
                "status": "todo" if task["status"] == "changed" else task["status"],
                "subtasks": list(task.get("subtasks", [])),
                "depends_on": list(task["depends_on"]),
                "changed_files": list(task["changed_files"]),
                "context_files": list(task.get("context_files", [])),
                "output_files": list(task.get("output_files", [])),
                "related_files": list(task.get("related_files", [])),
                "commands": list(task["commands"]),
                "test_items": list(task.get("test_items", [])),
                "test_commands": list(task.get("test_commands", [])),
                "test_scripts": list(task.get("test_scripts", [])),
                "manual_checks": list(task.get("manual_checks", [])),
                "verifications": list(task["verifications"]),
                "note": task.get("note", ""),
                "business_rules": list(task.get("business_rules", [])),
                "requirement_version": task.get("requirement_version"),
                "design_version": task.get("design_version"),
                "test_matrix_version": task.get("test_matrix_version"),
                "coverage_points": list(task.get("coverage_points", [])) if task.get("coverage_points") is not None else None,
                "coverage_change_ids": list(task.get("coverage_change_ids", [])) if task.get("coverage_change_ids") is not None else None,
                "coverage_acceptance": list(task.get("coverage_acceptance", [])) if task.get("coverage_acceptance") is not None else None,
                "coverage_tests": list(task.get("coverage_tests", [])) if task.get("coverage_tests") is not None else None,
                "feedback_contract_version": task.get("feedback_contract_version", ""),
                "feedback_state": task.get("feedback_state", ""),
                "acceptance_feedback": deepcopy(task.get("acceptance_feedback", [])),
                "formal_requirement_refs": list(task.get("formal_requirement_refs", [])) if task.get("formal_requirement_refs") is not None else None,
                "formal_design_refs": list(task.get("formal_design_refs", [])) if task.get("formal_design_refs") is not None else None,
                "formal_test_refs": list(task.get("formal_test_refs", [])) if task.get("formal_test_refs") is not None else None,
                "out_of_scope": list(task.get("out_of_scope", [])) if task.get("out_of_scope") is not None else None,
                "test_suggestions": list(task.get("test_suggestions", [])) if task.get("test_suggestions") is not None else None,
                "task_kind": task.get("task_kind", "generic"),
                "model_tier": task.get("model_tier", "medium"),
                "query_terms": list(task.get("query_terms", [])),
                "symbols": list(task.get("symbols", [])),
                "output_symbols": list(task.get("output_symbols", [])),
                "output_search_terms": list(task.get("output_search_terms", [])),
                "replaces": list(task.get("replaces", [])),
                "applies_to": list(task.get("applies_to", [])),
                "lesson_ids": list(task.get("lesson_ids", [])),
            }
            for task in raw_tasks
        ]

    return []


def append_extra_tasks(
    tasks: list[dict[str, object]],
    task_titles: list[str],
    *,
    summary: str = "",
    coverage_points: list[str] | None = None,
    test_items: list[str] | None = None,
    manual_checks: list[str] | None = None,
    task_kind: str = "generic",
    model_tier: str = "medium",
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if len([title for title in task_titles if title.strip()]) > 1:
        raise SdlcError("追加任务需要逐条提供结构化字段，请每次命令只追加一条任务。", exit_code=1)
    next_number = len(tasks) + 1
    created_tasks: list[dict[str, object]] = []
    for title in task_titles:
        clean_title = normalize_added_task_title(title)
        if not clean_title:
            continue
        task = {
            "task_id": f"T-{next_number:03d}",
            "title": clean_title,
            "summary": summary.strip() or clean_title,
            "status": "todo",
            "subtasks": [],
            "depends_on": [tasks[-1]["task_id"]] if tasks else [],
            "changed_files": [],
            "context_files": [],
            "output_files": [],
            "related_files": [],
            "commands": [],
            "test_items": [item.strip() for item in (test_items or []) if item.strip()],
            "test_commands": [],
            "test_scripts": [],
            "manual_checks": [item.strip() for item in (manual_checks or []) if item.strip()],
            "verifications": [],
            "note": "",
            "coverage_points": [item.strip() for item in (coverage_points or []) if item.strip()],
            "feedback_contract_version": "feedback.v1",
            "feedback_state": "none",
            "acceptance_feedback": [],
            "task_kind": task_kind,
            "model_tier": model_tier,
        }
        tasks.append(task)
        created_tasks.append(task)
        next_number += 1
    return tasks, created_tasks


def next_task_id(tasks: list[dict[str, object]]) -> str:
    numbers: list[int] = []
    for task in tasks:
        raw_id = str(task["task_id"])
        if raw_id.startswith("T-") and raw_id[2:].isdigit():
            numbers.append(int(raw_id[2:]))
    return f"T-{(max(numbers) if numbers else 0) + 1:03d}"


def split_change_points(text: str) -> list[str]:
    clean = text.strip()
    return [clean] if clean else []






def split_change_points_by_role(text: str) -> tuple[list[str], list[str]]:
    """验收点必须由 --acceptance 显式传入。"""

    return split_change_points(text), []

























ACTIVE_TASK_STATUSES = {"doing", "ready_for_user_check", "test_failed"}
TASK_CONTRACT_FIELDS = [
    "requirement_version",
    "design_version",
    "test_matrix_version",
    "coverage_points",
    "coverage_change_ids",
    "coverage_acceptance",
    "coverage_tests",
    "acceptance_feedback",
    "formal_requirement_refs",
    "formal_design_refs",
    "formal_test_refs",
    "out_of_scope",
    "test_suggestions",
]


def protected_task_contracts(tasks: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    snapshots: dict[str, dict[str, object]] = {}
    for task in tasks:
        if task.get("status") not in ACTIVE_TASK_STATUSES:
            continue
        task_id = str(task.get("task_id", ""))
        if not task_id:
            continue
        snapshots[task_id] = {
            field: list(task.get(field, [])) if isinstance(task.get(field), list) else task.get(field)
            for field in TASK_CONTRACT_FIELDS
            if task.get(field) is not None
        }
    return snapshots


def restore_protected_task_contracts(tasks: list[dict[str, object]], snapshots: dict[str, dict[str, object]]) -> None:
    if not snapshots:
        return
    for task in tasks:
        snapshot = snapshots.get(str(task.get("task_id", "")))
        if not snapshot:
            continue
        # 当前任务已经开工或在验收，后续变更不能改写它绑定的合同版本，否则活动轮次会被误判失效。
        for field, value in snapshot.items():
            task[field] = list(value) if isinstance(value, list) else value


def non_active_task_ids(requirement: dict[str, object]) -> list[str]:
    return [
        str(task.get("task_id", ""))
        for task in requirement.get("tasks", [])  # type: ignore[union-attr]
        if str(task.get("task_id", "")).strip()
        and str(task.get("status", "")) not in ACTIVE_TASK_STATUSES
    ]


def protected_task_insert_floor(tasks: list[dict[str, object]]) -> int:
    floor = 0
    for index, task in enumerate(tasks):
        if task.get("status") in ACTIVE_TASK_STATUSES and not is_fix_task(task):
            floor = max(floor, index + 1)
    return floor


def validate_task_dependencies(tasks: list[dict[str, object]]) -> None:
    task_map = {str(task.get("task_id", "")): task for task in tasks}
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        dependencies = [str(item) for item in task.get("depends_on", []) if str(item).strip()]
        unknown = [dependency for dependency in dependencies if dependency not in task_map]
        if unknown:
            raise SdlcError(f"{task_id} 依赖了不存在的任务：{', '.join(unknown)}", exit_code=1)

        status = str(task.get("status", ""))
        if status not in ACTIVE_TASK_STATUSES:
            continue

        unfinished = [
            dependency
            for dependency in dependencies
            if str(task_map[dependency].get("status", "")) not in {"done", "closed"}
        ]
        if unfinished:
            raise SdlcError(
                f"{task_id} 当前是 {status}，不能依赖未完成任务：{', '.join(unfinished)}。"
                "这会反向阻塞当前任务收口；请先完成或暂停当前任务，再调整依赖。",
                exit_code=1,
            )


def first_impacted_open_index(tasks: list[dict[str, object]], impacted_task_ids: list[str]) -> int:
    insert_floor = protected_task_insert_floor(tasks)
    impacted = set(impacted_task_ids)
    for index, task in enumerate(tasks):
        if index < insert_floor:
            continue
        if task["task_id"] in impacted and task["status"] not in {"done", "closed"} and not is_fix_task(task):
            return index
    for index, task in enumerate(tasks):
        if index < insert_floor:
            continue
        if task["status"] not in {"done", "closed"} and not is_fix_task(task):
            return index
    return len(tasks)


def previous_open_task_id(tasks: list[dict[str, object]], before_index: int) -> str | None:
    for task in reversed(tasks[:before_index]):
        if task["status"] != "closed":
            return str(task["task_id"])
    return None


def is_fix_task(task: dict[str, object]) -> bool:
    # 修复任务由任务合同里的显式类型决定，标题和摘要只负责给人看，不能反过来改变任务类型。
    return str(task.get("task_kind") or "").strip() == "fix"


def build_change_task(
    change_id: str,
    task_id: str,
    title: str,
    points: list[str],
    depends_on: list[str],
    *,
    acceptance_points: list[str] | None = None,
    summary: str = "",
) -> dict[str, object]:
    point_lines = "\n".join(f"- {point}" for point in points)
    acceptance_points = acceptance_points or []
    test_items = [f"验证变更 {change_id}：{point}" for point in points]
    test_items.extend(f"回归变更 {change_id}：{point}" for point in acceptance_points)
    manual_checks = [f"人工确认变更 {change_id} 已覆盖：{point}" for point in points]
    manual_checks.extend(f"人工确认变更 {change_id} 已覆盖：{point}" for point in acceptance_points)
    summary_title = (summary or title).removeprefix("处理需求变更：")
    return {
        "task_id": task_id,
        "title": title,
        "summary": f"处理 {change_id}：{summary_title}",
        "status": "todo",
        "subtasks": [],
        "depends_on": depends_on,
        "changed_files": [],
        "context_files": [],
        "output_files": [],
        "related_files": [],
        "commands": [],
        "test_items": test_items,
        "test_commands": [],
        "test_scripts": [],
        "manual_checks": manual_checks,
        "verifications": [],
        "coverage_change_ids": [change_id],
        "feedback_contract_version": "feedback.v1",
        "feedback_state": "none",
        "acceptance_feedback": [],
        "note": f"变更来源：{change_id}\n变更覆盖：\n{point_lines}",
    }






def merge_change_into_task(
    tasks: list[dict[str, object]],
    change: dict[str, object],
    points: list[str],
    acceptance_points: list[str],
) -> tuple[bool, list[dict[str, str]]]:
    target_task = merge_target_task(tasks, change)
    if target_task is None:
        return False, []

    change_id = str(change["change_id"])
    target_task_id = str(target_task["task_id"])
    description = str(change.get("description") or change.get("summary") or "").strip()
    existing_note = str(target_task.get("note", "")).strip()
    note_lines = [
        existing_note,
        f"变更来源：{change_id}",
        "这次变更是对当前任务目标、执行方式或验收方式的补充，直接合并进本任务，不新增单独任务。",
        description,
    ]
    target_task["note"] = "\n".join(item for item in note_lines if item)
    target_task["coverage_change_ids"] = unique_list(
        [*[str(item) for item in (target_task.get("coverage_change_ids") or [])], change_id]
    )

    target_task["test_items"] = unique_list(
        [
            *[str(item) for item in target_task.get("test_items", [])],
            *[f"验证变更 {change_id}：{point}" for point in points],
            *[f"回归变更 {change_id}：{point}" for point in acceptance_points],
        ]
    )
    target_task["manual_checks"] = unique_list(
        [
            *[str(item) for item in target_task.get("manual_checks", [])],
            *[f"人工确认变更 {change_id} 已合并到 {target_task_id}：{point}" for point in points],
            *[f"人工确认变更 {change_id} 已覆盖：{point}" for point in acceptance_points],
        ]
    )
    coverage = [{"point": point, "task_id": target_task_id} for point in points]
    return True, coverage


def model_task_groups(change: dict[str, object], points: list[str]) -> list[dict[str, object]]:
    """模型任务与变更点的关系必须显式提供，多任务时禁止相似度匹配。"""

    added_tasks = [
        item
        for item in change.get("added_tasks", [])
        if isinstance(item, dict) and str(item.get("source", "")) == "model" and str(item.get("title", "")).strip()
    ]
    if not added_tasks:
        return []
    if len(added_tasks) == 1:
        item = added_tasks[0]
        explicit_points = [str(value).strip() for value in item.get("points", []) if str(value).strip()]
        return [{
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "points": explicit_points or points,
        }]
    groups: list[dict[str, object]] = []
    for item in added_tasks:
        explicit_points = [str(value).strip() for value in item.get("points", []) if str(value).strip()]
        if not explicit_points:
            return []
        groups.append({
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "points": explicit_points,
        })
    return groups


def has_model_task_groups(change: dict[str, object], points: list[str]) -> bool:
    return bool(model_task_groups(change, points))


def merge_target_task(tasks: list[dict[str, object]], change: dict[str, object]) -> dict[str, object] | None:
    impacted_task_ids = [str(item) for item in change.get("changed_task_ids", []) if str(item).strip()]
    if len(impacted_task_ids) != 1:
        return None
    target_task_id = impacted_task_ids[0]
    target_task = next((task for task in tasks if str(task.get("task_id", "")) == target_task_id), None)
    if target_task is None or target_task.get("status") in {"done", "closed"}:
        return None
    return target_task


def change_needs_model_task_plan(tasks: list[dict[str, object]], change: dict[str, object]) -> bool:
    change_text = str(change.get("description") or change.get("summary") or "")
    points, _inline_acceptance_points = split_change_points_by_role(change_text)
    if has_model_task_groups(change, points):
        return False
    if merge_target_task(tasks, change) is not None:
        return False
    return True


def plan_single_change(tasks: list[dict[str, object]], change: dict[str, object]) -> tuple[list[dict[str, object]], list[dict[str, str]], list[dict[str, str]], list[dict[str, object]]]:
    change_id = str(change["change_id"])
    change_text = str(change.get("description") or change.get("summary") or "")
    points, inline_acceptance_points = split_change_points_by_role(change_text)
    acceptance_points = unique_list([*inline_acceptance_points, *[str(item) for item in change.get("acceptance_points", []) if str(item).strip()]])
    groups = model_task_groups(change, points)
    if not groups:
        merged, merged_coverage = merge_change_into_task(tasks, change, points, acceptance_points)
        if merged:
            return tasks, merged_coverage, [], []
        # 走到这里说明没有模型任务建议，也没法安全合并到一个已有任务。
        # CLI 不再自动猜“处理变更”任务，调用方会把这类变更留给 Codex 通过 change-plan --task 写回。
        return tasks, [], [], []
    insert_index = first_impacted_open_index(tasks, list(change.get("changed_task_ids", [])))
    previous_id = previous_open_task_id(tasks, insert_index)
    created_tasks: list[dict[str, object]] = []
    coverage: list[dict[str, str]] = []
    created_refs: list[dict[str, str]] = []

    for group_index, group in enumerate(groups):
        task_id = next_task_id([*tasks, *created_tasks])
        depends_on = [previous_id] if previous_id else []
        task_points = [str(item) for item in group["points"]]  # type: ignore[index]
        task_acceptance_points = acceptance_points if group_index == len(groups) - 1 else []
        task = build_change_task(
            change_id,
            task_id,
            str(group["title"]),
            task_points,
            depends_on,
            acceptance_points=task_acceptance_points,
            summary=str(group.get("summary", "")),
        )
        created_tasks.append(task)
        created_refs.append({"task_id": task_id, "title": str(group["title"])})
        coverage.extend({"point": point, "task_id": task_id} for point in task_points)
        previous_id = task_id

    if created_tasks:
        tasks = [*tasks[:insert_index], *created_tasks, *tasks[insert_index:]]
        if insert_index + len(created_tasks) < len(tasks):
            next_task = tasks[insert_index + len(created_tasks)]
            if next_task["status"] not in {"done", "closed"}:
                dependencies = list(next_task.get("depends_on", []))
                if previous_id and previous_id not in dependencies:
                    next_task["depends_on"] = [previous_id, *dependencies]
    return tasks, coverage, created_refs, created_tasks


def normalized_requirement_point_text(text: str) -> str:
    return str(text).strip()


def match_requirement_point_ids(requirement_points: list[dict[str, str]], point_texts: list[str]) -> list[str]:
    """覆盖绑定只接受精确 FR 编号。"""

    known_ids = {str(point.get("id") or "") for point in requirement_points}
    return unique_list([str(item).strip() for item in point_texts if str(item).strip() in known_ids])


def apply_change_coverage_to_created_tasks(
    requirement: dict[str, object],
    tasks: list[dict[str, object]],
    planned_changes: list[dict[str, object]],
    *,
    existing_task_ids: set[str] | None = None,
) -> None:
    labels = structured_version_labels(requirement)
    requirement_points = build_requirement_points(requirement, labels["requirement_version"])
    tasks_by_id = {str(task["task_id"]): task for task in tasks}
    existing_task_ids = existing_task_ids or set()
    for planned_change in planned_changes:
        coverage_items = [item for item in planned_change.get("coverage", []) if isinstance(item, dict)]
        task_point_texts: dict[str, list[str]] = {}
        for item in coverage_items:
            task_id = str(item.get("task_id", ""))
            point_text = str(item.get("point", ""))
            if task_id and point_text:
                task_point_texts.setdefault(task_id, []).append(point_text)
        for task_id, point_texts in task_point_texts.items():
            point_ids = match_requirement_point_ids(requirement_points, point_texts)
            if not point_ids:
                continue
            task = tasks_by_id.get(task_id)
            if task is not None:
                if task_id in existing_task_ids:
                    task["coverage_points"] = unique_list([*list(task.get("coverage_points") or []), *point_ids])
                else:
                    task["coverage_points"] = point_ids
            for item in coverage_items:
                if str(item.get("task_id", "")) == task_id:
                    item_point_ids = match_requirement_point_ids(requirement_points, [str(item.get("point", ""))])
                    if item_point_ids:
                        item["point_id"] = item_point_ids[0]


def restore_existing_coverage_points(tasks: list[dict[str, object]], saved_coverage: dict[str, list[str]]) -> None:
    for task in tasks:
        task_id = str(task.get("task_id", ""))
        coverage_points = saved_coverage.get(task_id, [])
        if coverage_points:
            task["coverage_points"] = coverage_points


def refresh_task_coverage_tests(requirement: dict[str, object], tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    test_matrix_version = structured_version_labels(requirement)["test_matrix_version"]
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    has_effective_changes = any(
        change.get("status") in {"effective", "planned", "resolved", "verified"}
        for change in requirement.get("changes", [])
        if isinstance(change, dict)
    )
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"} and not has_effective_changes:
        # 当前合同只允许任务引用正式矩阵已有编号，任务说明和人工验收点
        # 仍保存在任务卡里，但不能在计划命令中临时生成新的 TC 编号。
        test_cases = [dict(case) for case in native_start.get("test_cases", []) if isinstance(case, dict)]
    else:
        test_cases = build_test_cases({**requirement, "tasks": tasks}, test_matrix_version)
    cases_by_task: dict[str, list[str]] = {}
    acceptance_requirement_ids = {
        str(item.get("id", "")): {str(ref) for ref in item.get("requirement_ids", [])}
        for item in native_start.get("acceptance_points", [])
        if isinstance(item, dict)
    }
    for case in test_cases:
        task_id = str(case.get("task_id", ""))
        case_id = str(case.get("id", ""))
        if task_id and case_id:
            cases_by_task.setdefault(task_id, []).append(case_id)
            continue
        case_requirement_ids = {str(ref) for ref in case.get("requirement_ids", [])}
        if not case_requirement_ids:
            for acceptance_id in case.get("acceptance_ids", []):
                case_requirement_ids.update(acceptance_requirement_ids.get(str(acceptance_id), set()))
        for task in tasks:
            candidate_task_id = str(task.get("task_id", ""))
            coverage_points = {str(point) for point in task.get("coverage_points", [])}
            if coverage_points and case_requirement_ids and coverage_points.intersection(case_requirement_ids):
                cases_by_task.setdefault(candidate_task_id, []).append(case_id)
    for task in tasks:
        task["coverage_tests"] = cases_by_task.get(str(task.get("task_id", "")), [])
    return test_cases


def apply_reorder(tasks: list[dict[str, object]], reorder: str) -> list[dict[str, object]]:
    if not reorder:
        return tasks
    requested_order = [item.strip() for item in reorder.split(",") if item.strip()]
    known_ids = {task["task_id"] for task in tasks}
    unknown_ids = [task_id for task_id in requested_order if task_id not in known_ids]
    if unknown_ids:
        raise SdlcError(f"这些任务编号不存在，没法重排：{', '.join(unknown_ids)}")

    is_full_reorder = len(requested_order) == len(tasks) and set(requested_order) == known_ids
    task_map = {task["task_id"]: task for task in tasks}
    if not is_full_reorder:
        finished_ids = [
            task_id
            for task_id in requested_order
            if task_map[task_id]["status"] in {"done", "closed"}
        ]
        if finished_ids:
            raise SdlcError(
                "部分重排只能调整未完成队列，不能混入已完成或已关闭任务："
                + "、".join(finished_ids)
                + "。如果要完整重建顺序，请给出全部任务编号。",
                exit_code=1,
            )
        active_ids = [task["task_id"] for task in tasks if task["status"] not in {"done", "closed"}]
        sort_index = {task_id: index for index, task_id in enumerate(requested_order)}
        reordered_active = sorted(
            [task for task in tasks if task["status"] not in {"done", "closed"}],
            key=lambda item: sort_index.get(item["task_id"], len(sort_index) + active_ids.index(item["task_id"])),
        )
        finished_tasks = [task for task in tasks if task["status"] in {"done", "closed"}]
        reordered = finished_tasks + reordered_active
    else:
        reordered = [task_map[task_id] for task_id in requested_order]

    previous_open_task_id: str | None = None
    for task in reordered:
        if task["status"] == "closed":
            task["depends_on"] = []
            continue
        task["depends_on"] = [previous_open_task_id] if previous_open_task_id else []
        previous_open_task_id = str(task["task_id"])
    return reordered


def apply_close(tasks: list[dict[str, object]], close_ids: list[str]) -> list[dict[str, object]]:
    close_set = {item.strip() for item in close_ids if item.strip()}
    for task in tasks:
        if task["task_id"] in close_set:
            task["status"] = "closed"
    return tasks


def apply_dependencies(tasks: list[dict[str, object]], dependency_rules: list[str]) -> list[dict[str, object]]:
    task_map = {task["task_id"]: task for task in tasks}
    for rule in dependency_rules:
        if ":" not in rule:
            raise SdlcError(f"依赖格式不对：`{rule}`，请用 `任务:依赖1,依赖2`。")
        task_id, raw_dependencies = rule.split(":", 1)
        task_id = task_id.strip()
        dependencies = [item.strip() for item in raw_dependencies.split(",") if item.strip()]
        if task_id not in task_map:
            raise SdlcError(f"没有找到任务 `{task_id}`。")
        for dependency in dependencies:
            if dependency not in task_map:
                raise SdlcError(f"依赖任务 `{dependency}` 不存在。")
        task_map[task_id]["depends_on"] = dependencies
    return list(task_map.values())


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement = resolve_requirement(state, args.requirement_id)
        ensure_mutable_task_plan_contract(paths, requirement)
        raise_for_draft_changes(requirement)
        raise_for_effective_changes(requirement, prefix="普通 `$sdlc-plan` 不会直接处理")
        tasks = build_initial_tasks(requirement)
        saved_coverage = {
            str(task.get("task_id", "")): [str(item) for item in (task.get("coverage_points") or []) if str(item).strip()]
            for task in tasks
        }
        protected_contracts = protected_task_contracts(tasks)
        tasks, created_tasks = append_extra_tasks(
            tasks,
            args.task,
            summary=str(getattr(args, "summary", "") or ""),
            coverage_points=list(getattr(args, "coverage", []) or []),
            test_items=list(getattr(args, "test_item", []) or []),
            manual_checks=list(getattr(args, "manual_check", []) or []),
            task_kind=str(getattr(args, "task_kind", "generic") or "generic"),
            model_tier=str(getattr(args, "model_tier", "medium") or "medium"),
        )
        tasks = apply_reorder(tasks, args.reorder)
        tasks = apply_close(tasks, args.close)
        tasks = apply_dependencies(tasks, args.depends)
        validate_task_dependencies(tasks)
        bind_tasks_to_current_contract(requirement, tasks, force=True)
        restore_existing_coverage_points(tasks, saved_coverage)
        redirects = added_task_change_redirects(requirement, created_tasks)
        if redirects:
            raise SdlcError(
                "新增任务缺少显式合同字段：\n"
                + "\n".join(f"- {task['title']}：{reason}" for task, reason in redirects)
                + "\n请由模型通过 --coverage 明确关联的 FR 编号。",
                exit_code=1,
            )
        test_matrix_version = structured_version_labels(requirement)["test_matrix_version"]
        test_cases = refresh_task_coverage_tests(requirement, tasks)
        restore_protected_task_contracts(tasks, protected_contracts)
        quality_report = analyze_task_quality(tasks)
        append_event(
            paths,
            event_type="plan_updated",
            source="sdlc-plan",
            summary=f"更新需求 {requirement['requirement_id']} 的任务计划",
            requirement_id=requirement["requirement_id"],
            payload={
                "tasks": tasks,
                "priority": args.priority or requirement.get("priority", "normal"),
                "blocked_reason": "",
                "resolved_change_ids": [],
                "task_quality": quality_report,
                "test_matrix_version": test_matrix_version,
                "test_cases": test_cases,
            },
        )
        _state, requirement, stale_ids = refresh_planning_state(
            paths,
            str(requirement["requirement_id"]),
        )

    print(f"已更新任务计划：{requirement['requirement_id']}")
    print(f"任务数量：{len(tasks)}")
    if created_tasks:
        print("新增任务：")
        for task in created_tasks:
            print(f"- {task['task_id']}：{task['title']}")
    if stale_ids:
        print("已失效的整套任务审核：" + "、".join(stale_ids))
    print_current_next_suggestion(paths)
    return 0


def parse_change_plan_task_specs(requirement: dict[str, object], task_specs: list[str]) -> list[dict[str, object]]:
    """解析固定分隔符字段，不按任务文字推断关系。"""

    tasks = build_initial_tasks(requirement)
    created_tasks: list[dict[str, object]] = []
    model_tasks: list[dict[str, object]] = []
    for raw_spec in task_specs:
        parts = [item.strip() for item in raw_spec.split("||", 2)]
        title = parts[0] if parts else ""
        summary = parts[1] if len(parts) > 1 else ""
        points = [item.strip() for item in parts[2].split(",") if item.strip()] if len(parts) > 2 else []
        if not title:
            continue
        task_id = next_task_id([*tasks, *created_tasks])
        task: dict[str, object] = {
            "task_id": task_id,
            "title": title,
            "summary": summary or title,
            "points": points,
            "source": "model",
        }
        model_tasks.append(task)
        created_tasks.append(task)
    return model_tasks


def select_effective_change(requirement: dict[str, object], change_id: str) -> dict[str, object]:
    changes = effective_unplanned_changes(requirement)
    if change_id:
        change = next((item for item in changes if str(item.get("change_id", "")) == change_id), None)
        if change is None:
            raise SdlcError(f"{requirement['requirement_id']} 没有找到待规划变更 `{change_id}`。", exit_code=1)
        return change
    if len(changes) != 1:
        lines = [
            f"{requirement['requirement_id']} 有 {len(changes)} 条已生效但未规划变更，请用 --change 指定要补哪一条。",
            "待规划变更：",
        ]
        lines.extend(f"- {item['change_id']}：{item['summary']}" for item in changes)
        raise SdlcError("\n".join(lines), exit_code=1)
    return changes[0]


def record_model_task_suggestions(paths, requirement: dict[str, object], change_id: str, task_specs: list[str], acceptance_points: list[str]) -> list[dict[str, str]]:
    model_tasks = parse_change_plan_task_specs(requirement, task_specs)
    if not model_tasks:
        raise SdlcError("请至少通过 --task 传入一条有效任务建议，格式可以是“标题”或“标题||目标”。", exit_code=1)
    append_event(
        paths,
        event_type="change_model_plan_recorded",
        source="sdlc-change-plan",
        summary=f"为变更 {change_id} 写入模型任务建议",
        requirement_id=requirement["requirement_id"],
        payload={
            "change_id": change_id,
            "added_tasks": model_tasks,
            "acceptance_points": [item.strip() for item in acceptance_points if item.strip()],
            "planning_status": "model_plan_ready",
        },
    )
    return model_tasks


def run_change_plan(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    model_tasks: list[dict[str, str]] = []
    with project_lock(paths):
        state = derive_state(paths)
        requirement = resolve_requirement(state, args.requirement_id)
        raise_for_draft_changes(requirement)
        selected_change_id = str(args.change or "").strip()
        if args.task:
            selected_change = select_effective_change(requirement, selected_change_id)
            selected_change_id = str(selected_change["change_id"])
            model_tasks = record_model_task_suggestions(
                paths,
                requirement,
                selected_change_id,
                args.task,
                args.acceptance,
            )
            state = refresh_materialized_state(paths)
            requirement = resolve_requirement(state, args.requirement_id)
        elif selected_change_id:
            select_effective_change(requirement, selected_change_id)

        result = plan_effective_requirement_changes(
            paths,
            requirement,
            event_source="sdlc-change-plan",
            change_id=selected_change_id or None,
        )
        pending_changes = result["pending_changes"]
        if not pending_changes:
            print(f"{requirement['requirement_id']} 当前没有待规划需求变更。")
            print("下一步建议：$sdlc-next")
            return 0
        needs_model_plan_changes = list(result.get("needs_model_plan_changes", []))
        if needs_model_plan_changes:
            first_change = needs_model_plan_changes[0]
            print(f"{first_change['change_id']} 已经写入当前生效需求，但还没有模型任务建议。")
            print("下一步由 Codex 补任务拆分，并通过正式 change-plan 命令继续规划。")
            print(
                "下一步建议："
                f"$sdlc-change-plan {requirement['requirement_id']} --change {first_change['change_id']} "
                '--task "任务标题||任务目标"'
            )
            return 0
        tasks = result["tasks"]
        all_created_tasks = result["created_tasks"]
        stale_ids = result["stale_ids"]

    print(f"已规划需求变更：{', '.join(str(item['change_id']) for item in pending_changes)}")
    print(f"需求：{requirement['requirement_id']}")
    if model_tasks:
        print("已写入模型任务建议：")
        for task in model_tasks:
            print(f"- {task['task_id']}：{task['title']}")
    print(f"任务数量：{len(tasks)}")
    if all_created_tasks:
        print("新增任务：")
        for task in all_created_tasks:
            print(f"- {task['task_id']}：{task['title']}")
    if stale_ids:
        print("已失效的整套任务审核：" + "、".join(stale_ids))
    print("覆盖映射：.codex-sdlc/requirements/{}/change-map.md".format(requirement["folder_name"]))
    print_current_next_suggestion(paths)
    return 0


def plan_effective_requirement_changes(
    paths,
    requirement: dict[str, object],
    *,
    event_source: str,
    change_id: str | None = None,
) -> dict[str, object]:
    ensure_mutable_task_plan_contract(paths, requirement)
    pending_changes = effective_unplanned_changes(requirement)
    if change_id:
        pending_changes = [item for item in pending_changes if str(item.get("change_id", "")) == change_id]
    if not pending_changes:
        return {
            "pending_changes": [],
            "tasks": build_initial_tasks(requirement),
            "created_tasks": [],
            "planned_changes": [],
            "stale_ids": [],
            "needs_model_plan_changes": [],
        }

    tasks = build_initial_tasks(requirement)
    saved_existing_coverage = {
        str(task.get("task_id", "")): [str(item) for item in (task.get("coverage_points") or []) if str(item).strip()]
        for task in tasks
    }
    protected_contracts = protected_task_contracts(tasks)
    existing_task_ids = set(saved_existing_coverage)
    planned_changes: list[dict[str, object]] = []
    all_created_tasks: list[dict[str, object]] = []
    needs_model_plan_changes = [
        change
        for change in pending_changes
        if change_needs_model_task_plan(tasks, change)
    ]
    if needs_model_plan_changes:
        return {
            "pending_changes": pending_changes,
            "tasks": tasks,
            "created_tasks": [],
            "planned_changes": [],
            "stale_ids": [],
            "needs_model_plan_changes": needs_model_plan_changes,
        }
    for change in pending_changes:
        tasks, coverage, created_refs, created_tasks = plan_single_change(tasks, change)
        validate_task_dependencies(tasks)
        planned_changes.append(
            {
                "change_id": change["change_id"],
                "task_ids": [item["task_id"] for item in created_refs],
                "coverage": coverage,
            }
        )
        all_created_tasks.extend(created_tasks)
    validate_task_dependencies(tasks)
    bind_tasks_to_current_contract(requirement, tasks, force=True)
    restore_existing_coverage_points(tasks, saved_existing_coverage)
    apply_change_coverage_to_created_tasks(requirement, tasks, planned_changes, existing_task_ids=existing_task_ids)
    test_matrix_version = structured_version_labels(requirement)["test_matrix_version"]
    native_start = requirement.get("native_start") if isinstance(requirement.get("native_start"), dict) else {}
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"}:
        # formal.v2 的 TC 是正式执行依据，规划更新只能绑定现有 TC，不能另造 TC 覆盖正式矩阵。
        test_cases = [dict(case) for case in native_start.get("test_cases", []) if isinstance(case, dict)]
        known_case_ids = {str(case.get("id")) for case in test_cases if str(case.get("id") or "").strip()}
        for task in tasks:
            task["coverage_tests"] = [
                str(case_id)
                for case_id in task.get("coverage_tests", [])
                if str(case_id) in known_case_ids
            ]
    else:
        test_cases = refresh_task_coverage_tests(requirement, tasks)
    restore_protected_task_contracts(tasks, protected_contracts)
    if native_start.get("formal_contract_version") in {"formal.v2", "formal.v3"}:
        known_case_ids = {str(case.get("id")) for case in test_cases if str(case.get("id") or "").strip()}
        for task in tasks:
            coverage_tests = [
                str(case_id)
                for case_id in task.get("coverage_tests", [])
                if str(case_id) in known_case_ids
            ]
            if not coverage_tests:
                coverage_points = {str(point_id) for point_id in task.get("coverage_points", [])}
                coverage_tests = [
                    str(case.get("id"))
                    for case in test_cases
                    if coverage_points.intersection(str(point_id) for point_id in case.get("requirement_ids", []))
                ]
            task["coverage_tests"] = coverage_tests
    quality_report = analyze_task_quality(tasks)
    append_event(
        paths,
        event_type="plan_updated",
        source=event_source,
        summary=f"规划需求 {requirement['requirement_id']} 的待处理变更",
        requirement_id=requirement["requirement_id"],
        payload={
            "tasks": tasks,
            "priority": requirement.get("priority", "normal"),
            "blocked_reason": "",
            "resolved_change_ids": [],
            "planned_changes": planned_changes,
            "task_quality": quality_report,
            "test_matrix_version": test_matrix_version,
            "test_cases": test_cases,
        },
    )
    _state, _requirement, stale_ids = refresh_planning_state(
        paths,
        str(requirement["requirement_id"]),
    )
    return {
        "pending_changes": pending_changes,
        "tasks": tasks,
        "created_tasks": all_created_tasks,
        "planned_changes": planned_changes,
        "stale_ids": stale_ids,
        "needs_model_plan_changes": [],
    }
