from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.commands import task_cmd
from codex_sdlc.core import task_run
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state, load_events
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_evidence import register_task_evidence
from test_task_done_run_gate import _record_manual, _record_test
from test_task_run_contract import _activate_run


def _run_root(requirement_root: Path, run_number: int) -> Path:
    return requirement_root / "runtime/T-001/runs" / f"{run_number:04d}"


def _record_feedback(project: Path, requirement_root: Path) -> None:
    source = _run_root(requirement_root, 1) / "evidence/feedback.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps(
            {
                "schema_version": "task-feedback.v1",
                "feedback_id": "FB-001",
                "requirement_id": "REQ-001",
                "task_id": "T-001",
                "run_number": 1,
                "source": {"type": "user", "received_at": "2026-07-22T05:30:00+08:00"},
                "content": "现有范围内的验收记录需要保留。",
                "affected_refs": [],
                "changes_contract": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="feedback",
        source_file=source.relative_to(project).as_posix(),
        source_sha256=sha256_file(source),
    )


def _completed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    _record_test(project, requirement_root)
    _record_manual(project, requirement_root)
    _record_feedback(project, requirement_root)
    task_run.complete_task_run(
        build_paths(project), requirement_id="REQ-001", task_id="T-001"
    )
    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0][
        "status"
    ] == "done"
    return project, requirement_root


def _restore(project: Path, reason: str = "补做无权限账号的导出检查") -> dict[str, object]:
    paths = build_paths(project)
    state = derive_state(paths)
    requirement = state["requirements"]["REQ-001"]
    task = requirement["tasks"][0]
    return task_run.restore_task_run(
        paths,
        state=state,
        requirement=requirement,
        task=task,
        reason=reason,
    )


def test_restore_closes_old_run_preserves_evidence_and_creates_empty_reading_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    old_root = _run_root(requirement_root, 1)
    protected = {
        path.relative_to(old_root).as_posix(): (path.read_bytes(), sha256_file(path))
        for path in old_root.rglob("*")
        if path.is_file()
    }

    result = _restore(project)

    new_root = _run_root(requirement_root, 2)
    old_run = json.loads((old_root / "task-run.v1.json").read_text(encoding="utf-8"))
    new_run = json.loads((new_root / "task-run.v1.json").read_text(encoding="utf-8"))
    new_manifest = json.loads(
        (new_root / "task-read-manifest.v1.json").read_text(encoding="utf-8")
    )
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    close_record = json.loads(
        (old_root / "task-restore.v1.json").read_text(encoding="utf-8")
    )
    assert result["idempotent"] is False
    assert old_run["status"] == "closed"
    assert close_record["close_reason"] == "补做无权限账号的导出检查"
    assert close_record["closed_at"]
    assert close_record["closed_run_number"] == 1
    assert close_record["new_run_number"] == 2
    assert new_run["run_number"] == new_manifest["run_number"] == current["run_number"] == 2
    assert new_run["status"] == current["status"] == "reading"
    assert new_run["read_manifest_sha256"] == sha256_file(
        new_root / "task-read-manifest.v1.json"
    )
    assert current["task_run_sha256"] == sha256_file(new_root / "task-run.v1.json")
    assert new_run["test_records"] == []
    assert new_run["feedback_records"] == []
    assert new_run["verification_records"] == []
    assert new_run["read_confirmation"] is None
    restored_state = derive_state(build_paths(project))
    restored_requirement = restored_state["requirements"]["REQ-001"]
    restored_task = restored_requirement["tasks"][0]
    assert restored_task["status"] == "doing"
    _manifest, current_upstream = task_run._current_upstream_snapshot(
        build_paths(project),
        requirement=restored_requirement,
        task=restored_task,
        run_number=2,
        all_tasks={"T-001": restored_task},
    )
    assert new_run["upstream_hashes"] == current_upstream
    # 关闭记录是恢复新增的旁路元数据；原轮次正文、清单与证据文件必须逐字不变。
    for relative, (content, digest) in protected.items():
        target = old_root / relative
        assert target.read_bytes() == content
        assert sha256_file(target) == digest


def test_restore_is_idempotent_and_run_numbers_are_never_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    first = _restore(project)
    repeated = _restore(project)

    assert first["run"]["run_number"] == repeated["run"]["run_number"] == 2
    assert repeated["idempotent"] is True
    assert sorted(path.name for path in (requirement_root / "runtime/T-001/runs").iterdir()) == [
        "0001",
        "0002",
    ]
    with pytest.raises(SdlcError, match="恢复请求|当前轮次"):
        _restore(project, "另一条反馈不能悄悄占用新轮次")
    assert not _run_root(requirement_root, 3).exists()


def test_restore_twice_keeps_every_run_independently_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    _restore(project, "第一次恢复")
    second_root = _run_root(requirement_root, 2)
    second_run = json.loads(
        (second_root / "task-run.v1.json").read_text(encoding="utf-8")
    )
    task_run.confirm_task_read(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        manifest_sha256=str(second_run["read_manifest_sha256"]),
    )
    task = derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]
    test_source = second_root / "evidence/second-test.log"
    test_source.parent.mkdir(parents=True, exist_ok=True)
    test_source.write_text("1 passed\n", encoding="utf-8")
    register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="test",
        source_file=test_source.relative_to(project).as_posix(),
        source_sha256=sha256_file(test_source),
        command="python3 -m pytest -q",
        exit_code=0,
        result="passed",
        test_item=str(task["test_items"][0]),
    )
    manual_source = second_root / "evidence/second-manual.json"
    manual_source.write_text(
        json.dumps(
            {
                "environment": "仓库外临时 Git 项目",
                "checks": [
                    {
                        "item": str(task["manual_checks"][0]),
                        "expected": "恢复后的行为符合任务合同",
                        "actual": "逐项检查通过",
                        "result": "passed",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="verification",
        source_file=manual_source.relative_to(project).as_posix(),
        source_sha256=sha256_file(manual_source),
        command="人工逐项验收",
        exit_code=0,
        result="passed",
    )
    task_run.complete_task_run(
        build_paths(project), requirement_id="REQ-001", task_id="T-001"
    )

    third = _restore(project, "第二次恢复")

    assert third["run"]["run_number"] == 3
    for run_number, expected_status in ((1, "closed"), (2, "closed"), (3, "reading")):
        run_root = _run_root(requirement_root, run_number)
        run = json.loads((run_root / "task-run.v1.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (run_root / "task-read-manifest.v1.json").read_text(encoding="utf-8")
        )
        assert run["run_number"] == manifest["run_number"] == run_number
        assert run["status"] == expected_status
    assert len(json.loads((_run_root(requirement_root, 1) / "task-run.v1.json").read_text(encoding="utf-8"))["test_records"]) == 1
    assert len(json.loads((_run_root(requirement_root, 2) / "task-run.v1.json").read_text(encoding="utf-8"))["test_records"]) == 1
    assert json.loads((_run_root(requirement_root, 3) / "task-run.v1.json").read_text(encoding="utf-8"))["test_records"] == []


def test_task_restore_command_uses_structured_run_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    args = type(
        "Args",
        (),
        {
            "items": ["REQ-001", "T-001", "从正式命令恢复"],
            "feedback_contract": "",
        },
    )()

    assert task_cmd.run_restore(args) == 0

    output = capsys.readouterr().out
    assert "任务状态：doing；运行状态：reading；运行轮次：0002" in output
    assert "task-read-manifest.v1.json" in output
    assert _run_root(requirement_root, 2).is_dir()


@pytest.mark.parametrize(
    ("hook_name", "committed"),
    [
        ("_after_restore_directory_prepare", False),
        ("_after_restore_event_append", True),
        ("_before_restore_current_commit", True),
    ],
)
def test_restore_interruption_recovers_to_one_complete_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
    committed: bool,
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    old_root = _run_root(requirement_root, 1)
    old_run_before = (old_root / "task-run.v1.json").read_bytes()
    current_before = (requirement_root / "runtime/T-001/current.json").read_bytes()

    def interrupt(_journal: dict[str, object]) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(task_run, hook_name, interrupt)
    with pytest.raises(KeyboardInterrupt):
        _restore(project)
    monkeypatch.setattr(task_run, hook_name, lambda _journal: None)

    # derive_state 是 status 等正式只读入口共用的状态读取；它必须先收好恢复事务。
    state = derive_state(paths)
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    if committed:
        assert state["requirements"]["REQ-001"]["tasks"][0]["status"] == "doing"
        assert current["run_number"] == 2
        assert _run_root(requirement_root, 2).is_dir()
        assert (old_root / "task-restore.v1.json").is_file()
    else:
        assert state["requirements"]["REQ-001"]["tasks"][0]["status"] == "done"
        assert current_before == (requirement_root / "runtime/T-001/current.json").read_bytes()
        assert old_run_before == (old_root / "task-run.v1.json").read_bytes()
        assert not _run_root(requirement_root, 2).exists()
        assert not (old_root / "task-restore.v1.json").exists()
    assert not (requirement_root / "runtime/T-001/.restore-transaction.json").exists()


def test_restore_rejections_leave_old_evidence_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _completed_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    runtime = requirement_root / "runtime/T-001"
    old_bytes = {
        path.relative_to(runtime).as_posix(): path.read_bytes()
        for path in runtime.rglob("*")
        if path.is_file()
    }
    current_path = runtime / "current.json"
    current_path.unlink()
    with pytest.raises(SdlcError, match="当前.*轮次"):
        _restore(project)
    current_path.write_bytes(old_bytes["current.json"])

    state = derive_state(paths)
    accepted_requirement = dict(state["requirements"]["REQ-001"])
    accepted_requirement["status"] = "accepted"
    with pytest.raises(SdlcError, match="验收接受"):
        task_run.restore_task_run(
            paths,
            state=state,
            requirement=accepted_requirement,
            task=accepted_requirement["tasks"][0],
            reason="已接受任务不能恢复",
        )

    (requirement_root / "reference-index.v1.json").write_text("{不是有效 JSON\n", encoding="utf-8")
    with pytest.raises(SdlcError):
        _restore(project)

    for relative, content in old_bytes.items():
        assert (runtime / relative).read_bytes() == content
    assert not _run_root(requirement_root, 2).exists()
    assert not (runtime / ".restore-transaction.json").exists()
    assert not any(event.get("source") == "sdlc-task-restore" for event in load_events(paths))
