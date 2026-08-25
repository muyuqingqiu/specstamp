from __future__ import annotations

import re


def subtask_public_id(source_id: object) -> str:
    raw = str(source_id or "").strip()
    if not raw:
        return "CHK"
    task_match = re.fullmatch(r"T-?(\d+)", raw, flags=re.IGNORECASE)
    if task_match:
        return f"CHK-{int(task_match.group(1)):03d}"
    return f"CHK-{raw}"


def subtask_display_label(source_id: object) -> str:
    raw = str(source_id or "").strip()
    public_id = subtask_public_id(raw)
    if not raw:
        return public_id
    return f"{public_id}（来源 {raw}）"
