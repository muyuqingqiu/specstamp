from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import sha256_file
from test_task_direct_start import _reviewed_project, _start_args


def _confirm_args(manifest_sha256: str) -> argparse.Namespace:
    return argparse.Namespace(
        requirement_id="REQ-001",
        task_id="T-001",
        manifest_sha256=manifest_sha256,
    )


def _done_args() -> argparse.Namespace:
    args = _start_args()
    args.done = True
    args.change_report_template = ""
    args.keep_change_report_source = False
    args.await_user_check = False
    return args


def test_same_thread_confirms_current_complete_manifest_and_activates_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    manifest_path = run_root / "task-read-manifest.v1.json"
    digest = sha256_file(manifest_path)

    assert task_cmd.run_task_read_confirm(_confirm_args(digest)) == 0

    run = json.loads((run_root / "task-run.v1.json").read_text(encoding="utf-8"))
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    assert run["status"] == current["status"] == "active"
    assert run["read_confirmation"]["thread_id"] == "任务开发线程"
    assert run["read_confirmation"]["manifest_sha256"] == digest
    assert sha256_file(manifest_path) == digest


def test_task_done_rejects_reading_run_before_legacy_completion_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, _requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0

    with pytest.raises(SdlcError, match="reading|active"):
        task_cmd.run(_done_args())


def test_copied_worktree_cannot_confirm_original_reading_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path / "原工作树", monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    manifest_path = run_root / "task-read-manifest.v1.json"
    digest = sha256_file(manifest_path)

    copied_project = tmp_path / "复制后的工作树"
    shutil.copytree(project, copied_project)
    copied_requirement_root = (
        copied_project / requirement_root.relative_to(project)
    )
    copied_run_path = copied_requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    copied_current_path = copied_requirement_root / "runtime/T-001/current.json"
    copied_manifest_path = copied_requirement_root / "runtime/T-001/runs/0001/task-read-manifest.v1.json"
    copied_events_path = copied_project / ".codex-sdlc/events.jsonl"
    before = {
        "run": copied_run_path.read_bytes(),
        "current": copied_current_path.read_bytes(),
        "manifest": copied_manifest_path.read_bytes(),
        "events": copied_events_path.read_bytes(),
    }
    monkeypatch.chdir(copied_project)

    with pytest.raises(SdlcError, match="身份|工作树"):
        task_cmd.run_task_read_confirm(_confirm_args(digest))

    assert copied_run_path.read_bytes() == before["run"]
    assert copied_current_path.read_bytes() == before["current"]
    assert copied_manifest_path.read_bytes() == before["manifest"]
    assert copied_events_path.read_bytes() == before["events"]


def test_read_confirmation_interruption_recovers_on_same_command_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    run_path = run_root / "task-run.v1.json"
    manifest_path = run_root / "task-read-manifest.v1.json"
    current_path = requirement_root / "runtime/T-001/current.json"
    events_path = project / ".codex-sdlc/events.jsonl"
    digest = sha256_file(manifest_path)
    events_before = events_path.read_bytes()
    manifest_before = manifest_path.read_bytes()

    from codex_sdlc.core import task_run

    def interrupt(_journal: dict[str, object]) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        task_run,
        "_before_confirmation_current_commit",
        interrupt,
        raising=False,
    )
    with pytest.raises(KeyboardInterrupt):
        task_cmd.run_task_read_confirm(_confirm_args(digest))

    assert json.loads(run_path.read_text(encoding="utf-8"))["status"] == "active"
    assert json.loads(current_path.read_text(encoding="utf-8"))["status"] == "reading"
    journal_path = requirement_root / "runtime/T-001/.read-confirm-transaction.json"
    interrupted_bytes = {
        "run": run_path.read_bytes(),
        "current": current_path.read_bytes(),
        "manifest": manifest_path.read_bytes(),
        "events": events_path.read_bytes(),
        "journal": journal_path.read_bytes(),
    }
    monkeypatch.setattr(
        task_run,
        "_before_confirmation_current_commit",
        lambda _journal: None,
        raising=False,
    )

    # 错误哈希必须在恢复事务前被拒绝，避免命令报告失败时轮次却已经被激活。
    with pytest.raises(SdlcError, match="清单|哈希"):
        task_cmd.run_task_read_confirm(_confirm_args("0" * 64))
    assert run_path.read_bytes() == interrupted_bytes["run"]
    assert current_path.read_bytes() == interrupted_bytes["current"]
    assert manifest_path.read_bytes() == interrupted_bytes["manifest"]
    assert events_path.read_bytes() == interrupted_bytes["events"]
    assert journal_path.read_bytes() == interrupted_bytes["journal"]

    assert task_cmd.run_task_read_confirm(_confirm_args(digest)) == 0
    run = json.loads(run_path.read_text(encoding="utf-8"))
    current = json.loads(current_path.read_text(encoding="utf-8"))
    assert run["status"] == current["status"] == "active"
    assert current["task_run_sha256"] == sha256_file(run_path)
    assert run["read_confirmation"]["thread_id"] == "任务开发线程"
    assert not journal_path.exists()
    assert manifest_path.read_bytes() == manifest_before
    assert events_path.read_bytes() == events_before


@pytest.mark.parametrize("failure", ["other_thread", "tampered_manifest", "stale_pointer"])
def test_read_confirmation_failure_keeps_reading_contract_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    run_path = run_root / "task-run.v1.json"
    manifest_path = run_root / "task-read-manifest.v1.json"
    current_path = requirement_root / "runtime/T-001/current.json"
    digest = sha256_file(manifest_path)
    if failure == "other_thread":
        monkeypatch.setenv("CODEX_THREAD_ID", "另一个任务线程")
    elif failure == "tampered_manifest":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["generated_at"] = "2026-07-21T21:00:00+08:00"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["run_number"] = 2
        current_path.write_text(json.dumps(current, ensure_ascii=False) + "\n", encoding="utf-8")
    run_before = run_path.read_bytes()
    current_before = current_path.read_bytes()

    with pytest.raises(SdlcError):
        task_cmd.run_task_read_confirm(_confirm_args(digest))

    assert run_path.read_bytes() == run_before
    assert current_path.read_bytes() == current_before
