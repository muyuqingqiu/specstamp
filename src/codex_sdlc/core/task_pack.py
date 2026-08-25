from __future__ import annotations

from typing import Any


_ALLOWED_TASK_KINDS = {
    "generic",
    "code",
    "ui",
    "route",
    "i18n",
    "analytics",
    "search",
    "test",
    "docs",
    "discovery",
    "audit",
    "fix",
    "audit_fix",
    "closeout",
}


def task_kind(task: dict[str, Any]) -> str:
    """读取任务合同中的显式类型，不保留任何任务执行包生成或写入能力。"""

    value = str(task.get("task_kind") or "generic").strip()
    return value if value in _ALLOWED_TASK_KINDS else "generic"
