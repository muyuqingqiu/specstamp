from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_cli_v1 import init_demo_repo, run_cli

from codex_sdlc.core.backup import clean_backups, create_backup, load_index
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths


def initialize_project_without_auto_backup(tmp_path: Path) -> Path:
    project_dir = init_demo_repo(tmp_path)
    result = run_cli(["init"], cwd=project_dir, extra_env={"CODEX_SDLC_DISABLE_AUTO_BACKUP": "1"})
    assert result.returncode == 0, result.stderr
    return project_dir


def test_auto_backup_keeps_one_snapshot_for_same_project_command_and_window(tmp_path: Path, monkeypatch) -> None:
    backup_home = tmp_path / "sdlc-backups"
    monkeypatch.setenv("CODEX_SDLC_BACKUP_HOME", str(backup_home))
    project_dir = initialize_project_without_auto_backup(tmp_path)
    paths = build_paths(project_dir)

    first = create_backup(paths, label="auto-draft-after", automatic=True, command="draft", phase="after")
    second = create_backup(paths, label="auto-draft-after", automatic=True, command="draft", phase="after")

    index_data = load_index(backup_home)
    automatic_projects = [item for item in index_data["project_snapshots"] if item.get("backup_mode") == "auto"]
    assert first["coalesced"] == {"project": 0, "requirement": 0}
    assert second["coalesced"] == {"project": 1, "requirement": 0}
    assert second["git"]["amended"] is True
    assert len(automatic_projects) == 1
    assert automatic_projects[0]["backup_command"] == "draft"
    assert automatic_projects[0]["backup_phase"] == "after"

    config_result = subprocess.run(
        ["git", "config", "--get", "maintenance.auto"],
        cwd=backup_home,
        text=True,
        capture_output=True,
        check=False,
    )
    assert config_result.returncode == 0
    assert config_result.stdout.strip() == "false"
    commit_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=backup_home, text=True, capture_output=True, check=False
    )
    assert commit_count.returncode == 0
    assert commit_count.stdout.strip() == "1"


def test_global_backup_lock_blocks_another_project_writer(tmp_path: Path, monkeypatch) -> None:
    backup_home = tmp_path / "sdlc-backups"
    monkeypatch.setenv("CODEX_SDLC_BACKUP_HOME", str(backup_home))
    project_dir = initialize_project_without_auto_backup(tmp_path)
    source_root = Path(__file__).resolve().parents[1] / "src"
    worker_code = "\n".join(
        [
            "import sys",
            "import time",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from codex_sdlc.core.backup import backup_write_lock",
            "with backup_write_lock(Path(sys.argv[2])):",
            "    print('locked', flush=True)",
            "    time.sleep(0.45)",
        ]
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(source_root), str(backup_home)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdout.readline().strip() == "locked"
        started_at = time.monotonic()
        result = create_backup(build_paths(project_dir), label="等待全局锁")
        elapsed = time.monotonic() - started_at
        assert result["git"]["status"] == "committed"
        assert elapsed >= 0.3
    finally:
        worker.wait(timeout=3)
    assert worker.returncode == 0, worker.stderr.read() if worker.stderr is not None else ""


def test_auto_snapshot_hard_expiry_ignores_recent_count(tmp_path: Path, monkeypatch) -> None:
    backup_home = tmp_path / "sdlc-backups"
    monkeypatch.setenv("CODEX_SDLC_BACKUP_HOME", str(backup_home))
    snapshot_name = (datetime.now().astimezone() - timedelta(days=8)).strftime("%Y%m%d-120000-000000-auto-draft-after")
    snapshot_dir = backup_home / "repos" / "repo_a" / "project-snapshots" / "branch_a" / "wt_a"
    archive = snapshot_dir / f"{snapshot_name}.tar.gz"
    manifest = snapshot_dir / f"{snapshot_name}.manifest.json"
    snapshot_dir.mkdir(parents=True)
    archive.write_bytes(b"backup")
    manifest.write_text(json.dumps({"pinned": False, "backup": {"mode": "auto", "command": "draft", "phase": "after"}}), encoding="utf-8")

    result = clean_backups(keep_project=20, keep_requirement=50, keep_days=90, keep_auto_days=7)

    assert result["project"] == 1
    assert not archive.exists()
    assert not manifest.exists()


def test_backup_rejects_any_unrecovered_start_transaction_before_writing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backup_home = tmp_path / "sdlc-backups"
    monkeypatch.setenv("CODEX_SDLC_BACKUP_HOME", str(backup_home))
    project_dir = initialize_project_without_auto_backup(tmp_path)
    paths = build_paths(project_dir)
    active = paths.sdlc_dir / "start-transactions" / "active"
    active.mkdir(parents=True)
    # 门禁不能先尝试解释或修补事务；只要活动记录还在，备份就必须停止。
    (active / "STX-evidence.json").write_text("{", encoding="utf-8")

    with pytest.raises(SdlcError, match="尚未恢复的正式建档事务"):
        create_backup(paths, label="不应写入")

    assert not backup_home.exists()
