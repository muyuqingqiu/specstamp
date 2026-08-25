from __future__ import annotations

from pathlib import Path

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from test_task_direct_start import _reviewed_project, _start_args


def test_reviewed_task_starts_without_retired_task_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)

    assert task_cmd.run(_start_args()) == 0

    assert not (requirement_root / "task-packs").exists()
    assert (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").is_file()
    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]["status"] == "doing"
