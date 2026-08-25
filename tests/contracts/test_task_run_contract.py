from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_run import require_active_task_run
from test_task_direct_start import _reviewed_project, _start_args


def _activate_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    run = json.loads((run_root / "task-run.v1.json").read_text(encoding="utf-8"))
    args = argparse.Namespace(
        requirement_id="REQ-001",
        task_id="T-001",
        manifest_sha256=run["read_manifest_sha256"],
    )
    assert task_cmd.run_task_read_confirm(args) == 0
    return project, requirement_root


def test_same_inputs_and_allowed_output_change_keep_run_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    run_path = requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    before = json.loads(run_path.read_text(encoding="utf-8"))

    require_active_task_run(paths, requirement_id="REQ-001", task_id="T-001")
    require_active_task_run(paths, requirement_id="REQ-001", task_id="T-001")
    assert json.loads(run_path.read_text(encoding="utf-8"))["upstream_hashes"] == before[
        "upstream_hashes"
    ]

    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert task_cmd.run_task_run_check(
        argparse.Namespace(requirement_id="REQ-001", task_id="T-001")
    ) == 0
    run = json.loads(run_path.read_text(encoding="utf-8"))
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    assert run["status"] == current["status"] == "active"
    assert run["upstream_hashes"] == before["upstream_hashes"]
