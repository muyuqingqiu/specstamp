from __future__ import annotations

from typing import Any


FORMAL_TASK_SOFT_LIMIT = 20
TASK_GROUP_TARGET = 12
TASK_MIN_GROUP_SIZE = 4

# 任务主线只有这五个需求状态。把名称集中在一个结构化合同里，状态页、next 和技能
# 才不会各自发明一套阶段名称，导致同一份任务在不同入口显示成不同阶段。
DIRECT_EXECUTION_REQUIREMENT_STATES = (
    "planning_tasks",
    "ready_for_development",
    "developing",
    "verifying",
    "accepted",
)


def task_plan_stop_reason(requirement: dict[str, Any]) -> str:
    """返回任务不能直接开工的正式原因；空字符串表示审核门禁已经通过。"""

    tasks = [item for item in requirement.get("tasks", []) if isinstance(item, dict)]
    if not tasks:
        return "当前需求还没有正式任务清单。"

    # 明确阻塞会让审核入口本身拒绝创建请求，因此必须先告诉用户真实阻塞，不能误报成“尚未审核”。
    blocked = [
        str(task.get("task_id") or "")
        for task in tasks
        if task.get("blocking_conditions") or str(task.get("status") or "") == "blocked"
    ]
    if blocked:
        return "以下任务仍有明确阻塞条件，不能开始开发：" + "、".join(blocked) + "。"

    review = requirement.get("task_plan_review_state")
    if not isinstance(review, dict):
        return "当前任务不是结构化 task-plan.v2，不能进入直接执行主线。"
    reviews = [item for item in review.get("reviews", []) if isinstance(item, dict)]
    if not reviews:
        return "当前整套任务审核尚未创建，不能开始开发。"
    if review.get("can_advance") is not True:
        stale = [item for item in reviews if item.get("effective_status") == "stale"]
        needs_fix = [item for item in reviews if item.get("effective_status") == "needs_fix"]
        pending = [item for item in reviews if item.get("effective_status") == "pending"]
        if stale:
            return "当前整套任务审核已经过期，请按当前正式输入重新创建审核。"
        if needs_fix:
            return "当前整套任务审核要求返修，请先修正任务清单并重新审核。"
        if pending:
            return "当前整套任务审核还没有提交结论，不能开始开发。"
        rejection = str(review.get("rejection_reason") or "").strip()
        return rejection or "当前整套任务审核没有通过，不能开始开发。"

    evidence_status = str(requirement.get("task_planning_evidence_status") or "")
    if evidence_status != "current":
        return "任务规划使用的代码证据已经缺失或过期，请刷新任务规划后重新审核。"

    return ""


def project_direct_requirement_status(
    requirement: dict[str, Any],
    *,
    acceptance_current: bool,
    has_open_changes: bool,
) -> str:
    """按正式审核、任务状态和需求验收投影直接执行主线状态。"""

    tasks = [item for item in requirement.get("tasks", []) if isinstance(item, dict)]
    if not tasks:
        return "planning_tasks"
    if acceptance_current and all(
        str(task.get("status") or "") in {"done", "closed"} for task in tasks
    ):
        return "accepted"
    if all(str(task.get("status") or "") in {"done", "closed"} for task in tasks):
        return "verifying"
    if any(
        str(task.get("status") or "")
        in {"doing", "ready_for_user_check", "test_failed"}
        for task in tasks
    ):
        return "developing"
    if not has_open_changes and not task_plan_stop_reason(requirement):
        return "ready_for_development"
    return "planning_tasks"

def compact_group_size(task_count: int) -> int:
    if task_count <= 0:
        return TASK_MIN_GROUP_SIZE
    by_target = (task_count + TASK_GROUP_TARGET - 1) // TASK_GROUP_TARGET
    return max(TASK_MIN_GROUP_SIZE, by_target)


def analyze_task_quality(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """只报告可以从结构直接确定的问题，不判断任务应该怎样拆分。"""

    open_tasks = [task for task in tasks if task.get("status") != "closed"]
    warnings: list[str] = []
    suggestions: list[str] = []

    missing_test_items = [task for task in open_tasks if not task.get("test_items")]
    missing_manual_checks = [task for task in open_tasks if not task.get("manual_checks")]
    if missing_test_items or missing_manual_checks:
        warnings.append(
            f"测试和验收信息不完整：{len(missing_test_items)} 个任务缺自动测试项，{len(missing_manual_checks)} 个任务缺人工验收点。"
        )
        suggestions.append("请在任务合同中明确填写自动测试项和人工验收点后重新提交。")

    return {
        "status": "needs_attention" if warnings else "passed",
        "warnings": warnings,
        "suggestions": list(dict.fromkeys(suggestions)),
    }
