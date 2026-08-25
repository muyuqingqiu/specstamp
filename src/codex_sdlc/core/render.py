from __future__ import annotations

from pathlib import Path


def join_lines(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def relative_to_project(project_root: Path, file_path: Path) -> str:
    try:
        return str(file_path.relative_to(project_root))
    except ValueError:
        return str(file_path)
