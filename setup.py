from __future__ import annotations

from pathlib import Path

from setuptools import setup


ROOT = Path(__file__).parent


def bundled_skill_files() -> list[tuple[str, list[str]]]:
    """保留仓库技能为唯一来源，同时按原目录结构写入 wheel 数据区。"""

    sources = [
        *sorted((ROOT / "skills").glob("sdlc-*")),
        *sorted((ROOT / "shared-skills").iterdir()),
    ]
    data_files: list[tuple[str, list[str]]] = []
    for source_dir in sources:
        if not source_dir.is_dir():
            continue
        for source_file in sorted(path for path in source_dir.rglob("*") if path.is_file()):
            relative_parent = source_file.relative_to(ROOT).parent
            destination = Path("share") / "specstamp" / relative_parent
            data_files.append(
                (destination.as_posix(), [source_file.relative_to(ROOT).as_posix()])
            )
    return data_files


setup(data_files=bundled_skill_files())
