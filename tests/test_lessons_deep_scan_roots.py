from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_sdlc.core.lessons import (  # noqa: E402
    CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS,
    _deep_scan_roots,
)


def test_deep_scan_roots_uses_default_worktrees_only(monkeypatch, tmp_path: Path) -> None:
    """深度扫描默认只搜主目录下的 .codex/worktrees，不扫描整个主目录。"""

    home = tmp_path / "home"
    worktrees = home / ".codex" / "worktrees"
    worktrees.mkdir(parents=True)
    (home / "其它业务目录").mkdir()
    monkeypatch.delenv(CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS, raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)

    roots = _deep_scan_roots()

    assert roots == [worktrees]
    assert home not in roots
    assert home / "其它业务目录" not in roots


def test_deep_scan_roots_extra_roots_ignore_missing(monkeypatch, tmp_path: Path) -> None:
    """额外搜索根来自环境变量，多个路径用系统分隔符分开，不存在的路径被忽略。"""

    home = tmp_path / "home"
    worktrees = home / ".codex" / "worktrees"
    worktrees.mkdir(parents=True)
    extra_existing = tmp_path / "extra-existing"
    extra_missing = tmp_path / "extra-missing"
    extra_existing.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv(
        CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS,
        os.pathsep.join([str(extra_existing), str(extra_missing)]),
    )

    roots = _deep_scan_roots()

    assert roots == [worktrees, extra_existing]
    assert extra_missing not in roots


def test_deep_scan_roots_dedups_repeated_roots(monkeypatch, tmp_path: Path) -> None:
    """重复的额外根和默认根只保留一次，顺序稳定。"""

    home = tmp_path / "home"
    worktrees = home / ".codex" / "worktrees"
    worktrees.mkdir(parents=True)
    extra = tmp_path / "extra"
    extra.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv(
        CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS,
        os.pathsep.join([str(extra), str(extra), str(worktrees)]),
    )

    roots = _deep_scan_roots()

    assert roots == [worktrees, extra]
