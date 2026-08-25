from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.start_transaction import (
    commit_prepared_start,
    recover_incomplete_start_transactions,
)
from codex_sdlc.core.state import derive_state, load_events
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
)
from codex_sdlc.services import start_service
from test_contract_cli_regressions import _ready_project
from test_cli_v1 import run_cli, run_cli_raw


class _ForcedProcessStop(BaseException):
    """模拟 os._exit 的不可捕获中断，不能被普通异常回滚吞掉。"""


def _original_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def transaction_template(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("start-transaction-template")
    patcher = pytest.MonkeyPatch()
    try:
        project, paths, package = _ready_project(root, patcher)
        prepared = start_service.prepare_document_first_start(paths, package)
    finally:
        patcher.undo()
    return project, package, prepared


def _prepare(tmp_path: Path, transaction_template):
    source_project, package, prepared_template = transaction_template
    project = tmp_path / "project"
    shutil.copytree(source_project, project)
    from codex_sdlc.core.project import ProjectPaths

    paths = ProjectPaths(project)
    staging = next(paths.start_staging_root.glob("start-*"))
    # 模板复制到新根目录后，历史事件的 project_path 必须同步成真实项目根；
    # prepared 事件字节边界也随真实文件重算，不能沿用模板目录的旧字节。
    events = load_events(paths)
    for event in events:
        event["project_path"] = str(project)
    event_bytes = b"".join(
        (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        for event in events
    )
    paths.events_file.write_bytes(event_bytes)
    transaction_path = staging / "start-transaction.json"
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    transaction["event_file_size"] = len(event_bytes)
    transaction["event_count"] = len(events)
    transaction["event_sha256"] = sha256_bytes(event_bytes)
    transaction_path.write_text(canonical_json_text(transaction), encoding="utf-8")
    prepared = {
        **prepared_template,
        "staging_directory": str(staging),
    }
    staging = Path(str(prepared["staging_directory"]))
    original = _original_snapshot(staging / "original")
    return project, paths, package, prepared, original


def _interrupt_transaction(
    paths,
    prepared: dict[str, object],
    fault_point: str,
) -> tuple[Path, dict[str, object]]:
    def stop(point: str, _transaction_path: Path) -> None:
        if point == fault_point:
            raise _ForcedProcessStop(point)

    with pytest.raises(_ForcedProcessStop):
        commit_prepared_start(paths, prepared, fault_injector=stop)
    active = next(
        (paths.sdlc_dir / "start-transactions" / "active").glob("START-*.json")
    )
    transaction = json.loads(active.read_text(encoding="utf-8"))
    return active, transaction


def _write_transaction(path: Path, transaction: dict[str, object]) -> None:
    path.write_text(canonical_json_text(transaction), encoding="utf-8")


def _replace_real_transaction_events(
    paths,
    transaction: dict[str, object],
) -> None:
    """攻击者可重算公开边界，但这些字段不能代替正式对象归属证明。"""

    start = int(transaction["event_start_size"])
    prefix = paths.events_file.read_bytes()[:start]
    events = transaction["events"]
    assert isinstance(events, list)
    event_bytes = b"".join(
        canonical_json_text(event).encode("utf-8")
        for event in events
    )
    paths.events_file.write_bytes(prefix + event_bytes)
    transaction["event_append_size"] = len(event_bytes)
    transaction["event_append_sha256"] = sha256_bytes(event_bytes)
    transaction["event_end_size"] = start + len(event_bytes)
    transaction["event_end_count"] = int(transaction["event_start_count"]) + len(events)
    transaction["event_end_sha256"] = sha256_bytes(prefix + event_bytes)


def test_prepared_transaction_commits_once_and_keeps_original_immutable(
    tmp_path: Path,
    transaction_template,
) -> None:
    _project, paths, _package, prepared, original = _prepare(
        tmp_path,
        transaction_template,
    )
    before_event_count = len(load_events(paths))

    first = commit_prepared_start(paths, prepared)
    second = commit_prepared_start(paths, prepared)

    target = paths.requirements_dir / str(first["target_directory"])
    assert first["state"] == "completed"
    assert first["idempotent"] is False
    assert second["transaction_id"] == first["transaction_id"]
    assert second["formal_directory"] == first["formal_directory"]
    assert second["idempotent"] is True
    assert len(load_events(paths)) == before_event_count + 2
    assert _original_snapshot(target / "original") == original
    assert not (target / "start-transaction.json").exists()
    status = json.loads((target / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "active"
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "started"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == "REQ-001"
    assert "REQ-001" in state["requirements"]


def test_real_cli_start_and_repeat_return_the_same_formal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    package_file = tmp_path / "document-first-formal.v3.json"
    package_file.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    first = run_cli_raw(["start", "--file", str(package_file)], cwd=project)
    original = _original_snapshot(paths.requirements_dir / "REQ-001" / "original")
    second = run_cli_raw(["start", "--file", str(package_file)], cwd=project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "已创建正式需求：REQ-001" in first.stdout
    assert "相同正式建档事务已经完成，已返回原结果" in second.stdout
    assert _original_snapshot(
        paths.requirements_dir / "REQ-001" / "original"
    ) == original
    assert len(
        [
            event
            for event in load_events(paths)
            if event.get("event_type") == "requirement_created"
            and event.get("requirement_id") == "REQ-001"
        ]
    ) == 1


@pytest.mark.parametrize(
    ("fault_point", "expected_state"),
    [
        ("before_events_append", "rolled_back"),
        ("after_events_append", "completed"),
        ("after_directory_commit", "completed"),
        ("during_projection_refresh", "completed"),
        ("before_integrity_check", "completed"),
        ("after_integrity_check", "completed"),
    ],
)
def test_each_forced_stop_recovers_to_one_complete_result(
    tmp_path: Path,
    transaction_template,
    fault_point: str,
    expected_state: str,
) -> None:
    _project, paths, _package, prepared, original = _prepare(
        tmp_path,
        transaction_template,
    )
    before_events = paths.events_file.read_bytes()

    def stop(point: str, _transaction_path: Path) -> None:
        if point == fault_point:
            raise _ForcedProcessStop(point)

    with pytest.raises(_ForcedProcessStop):
        commit_prepared_start(paths, prepared, fault_injector=stop)

    active_files = list(
        (paths.sdlc_dir / "start-transactions" / "active").glob("START-*.json")
    )
    assert len(active_files) == 1
    interrupted_transaction = json.loads(
        active_files[0].read_text(encoding="utf-8")
    )
    assert interrupted_transaction["state"] in {
        "prepared",
        "events_appended",
        "directory_committed",
        "completed",
    }
    if fault_point == "before_integrity_check":
        assert interrupted_transaction["state"] == "directory_committed"
        assert (
            interrupted_transaction["last_confirmed_step"]
            == "projection_refreshed"
        )

    recovered = recover_incomplete_start_transactions(paths)

    assert len(recovered) == 1
    assert recovered[0]["state"] == expected_state
    assert not list(
        (paths.sdlc_dir / "start-transactions" / "active").iterdir()
    )
    target = paths.requirements_dir / "REQ-001"
    events = load_events(paths)
    created = [
        event
        for event in events
        if event.get("event_type") == "requirement_created"
        and event.get("requirement_id") == "REQ-001"
    ]
    started = [
        event
        for event in events
        if event.get("event_type") == "draft_started"
        and event.get("requirement_id") == "REQ-001"
    ]
    if expected_state == "rolled_back":
        assert paths.events_file.read_bytes() == before_events
        assert not target.exists()
        assert created == []
        assert started == []
    else:
        assert len(created) == 1
        assert len(started) == 1
        assert _original_snapshot(target / "original") == original
        assert json.loads(
            (target / "status.json").read_text(encoding="utf-8")
        )["status"] == "active"


def test_real_child_process_exit_is_recovered_by_next_cli_command(
    tmp_path: Path,
    transaction_template,
) -> None:
    project, paths, _package, prepared, original = _prepare(
        tmp_path,
        transaction_template,
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    worker = "\n".join(
        [
            "import os",
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from codex_sdlc.core.project import ProjectPaths",
            "from codex_sdlc.core.start_transaction import commit_prepared_start",
            "def stop(point, _path):",
            "    if point == 'after_events_append':",
            "        os._exit(91)",
            "commit_prepared_start(",
            "    ProjectPaths(Path(sys.argv[2])),",
            "    {'transaction_id': sys.argv[3], 'staging_directory': sys.argv[4]},",
            "    fault_injector=stop,",
            ")",
        ]
    )
    interrupted = subprocess.run(
        [
            sys.executable,
            "-c",
            worker,
            str(source_root),
            str(project),
            str(prepared["transaction_id"]),
            str(prepared["staging_directory"]),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert interrupted.returncode == 91

    next_command = run_cli(["version"], cwd=project)

    assert next_command.returncode == 0, next_command.stderr
    target = paths.requirements_dir / "REQ-001"
    assert target.is_dir()
    assert _original_snapshot(target / "original") == original
    assert len(
        [
            event
            for event in load_events(paths)
            if event.get("event_type") == "requirement_created"
            and event.get("requirement_id") == "REQ-001"
        ]
    ) == 1


def test_directory_conflict_and_event_boundary_drift_are_rejected(
    tmp_path: Path,
    transaction_template,
) -> None:
    _project, paths, _package, prepared, _original = _prepare(
        tmp_path,
        transaction_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    before = _original_snapshot(staging)
    conflict = paths.requirements_dir / "REQ-001-unowned"
    conflict.mkdir()

    with pytest.raises(SdlcError, match="正式需求目标已经存在|编号冲突"):
        commit_prepared_start(paths, prepared)

    assert _original_snapshot(staging) == before
    assert conflict.is_dir()
    conflict.rmdir()
    with paths.events_file.open("ab") as handle:
        handle.write(b'{"event_id":"EVT-OUTSIDE"}\n')

    with pytest.raises(SdlcError, match="事件边界"):
        commit_prepared_start(paths, prepared)
    assert _original_snapshot(staging) == before


def test_damaged_transaction_blocks_cli_before_identity_and_backup(
    tmp_path: Path,
    transaction_template,
) -> None:
    project, paths, _package, _prepared, _original = _prepare(
        tmp_path,
        transaction_template,
    )
    active = paths.sdlc_dir / "start-transactions" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / "STX-damaged.json").write_text("{", encoding="utf-8")
    backup_home = tmp_path / "backup-home"

    result = run_cli(
        ["status"],
        cwd=project,
        extra_env={
            "CODEX_SDLC_BACKUP_HOME": str(backup_home),
            "CODEX_SDLC_DISABLE_AUTO_BACKUP": "0",
        },
    )

    assert result.returncode == 1
    assert "活动建档事务不是有效的 UTF-8 JSON" in result.stderr
    assert not backup_home.exists()


def test_recovery_rejects_synchronized_identity_and_event_boundary_tampering(
    tmp_path: Path,
    transaction_template,
) -> None:
    _project, paths, _package, prepared, _original = _prepare(
        tmp_path,
        transaction_template,
    )
    staging = Path(str(prepared["staging_directory"]))
    active, transaction = _interrupt_transaction(
        paths,
        prepared,
        "after_events_append",
    )
    transaction["requirement_id"] = "REQ-999"
    transaction["target_directory"] = "REQ-999"
    transaction["formal_directory"] = str(paths.requirements_dir / "REQ-999")
    events = transaction["events"]
    assert isinstance(events, list)
    for event in events:
        event["project_path"] = str(tmp_path / "伪造项目")
        event["requirement_id"] = "REQ-999"
    events[0]["payload"]["folder_name"] = "REQ-999"
    events[1]["payload"]["started_requirement_id"] = "REQ-999"
    _replace_real_transaction_events(paths, transaction)
    _write_transaction(active, transaction)
    tampered_events = paths.events_file.read_bytes()

    with pytest.raises(SdlcError, match="真实正式对象身份|prepared staging"):
        recover_incomplete_start_transactions(paths)

    assert active.is_file()
    assert staging.is_dir()
    assert paths.events_file.read_bytes() == tampered_events
    assert not (paths.requirements_dir / "REQ-999").exists()
    status = json.loads((staging / "status.json").read_text(encoding="utf-8"))
    reference = json.loads(
        (staging / "reference-index.v1.json").read_text(encoding="utf-8")
    )
    assert status["requirement_id"] == "REQ-001"
    assert reference["requirement_id"] == "REQ-001"


@pytest.mark.parametrize(
    "tamper",
    [
        "draft",
        "project",
        "created_requirement",
        "folder",
        "started_draft",
        "started_requirement",
        "formal_directory",
        "staging_directory",
    ],
)
def test_recovery_rejects_each_independent_identity_tamper(
    tmp_path: Path,
    transaction_template,
    tamper: str,
) -> None:
    _project, paths, _package, prepared, _original = _prepare(
        tmp_path,
        transaction_template,
    )
    real_staging = Path(str(prepared["staging_directory"]))
    active, transaction = _interrupt_transaction(
        paths,
        prepared,
        "after_events_append",
    )
    events = transaction["events"]
    assert isinstance(events, list)
    if tamper == "draft":
        transaction["source_draft_id"] = "DRAFT-999"
    elif tamper == "project":
        events[0]["project_path"] = str(tmp_path / "伪造项目")
    elif tamper == "created_requirement":
        events[0]["requirement_id"] = "REQ-999"
    elif tamper == "folder":
        events[0]["payload"]["folder_name"] = "REQ-999"
    elif tamper == "started_draft":
        events[1]["payload"]["draft_id"] = "DRAFT-999"
    elif tamper == "started_requirement":
        events[1]["payload"]["started_requirement_id"] = "REQ-999"
    elif tamper == "formal_directory":
        transaction["formal_directory"] = str(paths.requirements_dir / "REQ-999")
    else:
        transaction["staging_directory"] = str(
            paths.start_staging_root / "start-forged"
        )
    _replace_real_transaction_events(paths, transaction)
    _write_transaction(active, transaction)

    with pytest.raises(SdlcError, match="不一致|归属|取证|保留现场"):
        recover_incomplete_start_transactions(paths)

    assert active.is_file()
    assert real_staging.is_dir()
    assert not (paths.requirements_dir / "REQ-999").exists()


def test_recovery_rejects_formal_internal_identity_drift_without_cleanup(
    tmp_path: Path,
    transaction_template,
) -> None:
    _project, paths, _package, prepared, _original = _prepare(
        tmp_path,
        transaction_template,
    )
    active, transaction = _interrupt_transaction(
        paths,
        prepared,
        "after_directory_commit",
    )
    target = paths.requirements_dir / "REQ-001"
    status_path = target / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["requirement_id"] = "REQ-999"
    status_path.write_text(canonical_json_text(status), encoding="utf-8")
    generated = transaction["generated_files"]
    assert isinstance(generated, dict)
    generated["status.json"] = sha256_bytes(status_path.read_bytes())
    _write_transaction(active, transaction)

    with pytest.raises(SdlcError, match="正式需求编号不一致"):
        recover_incomplete_start_transactions(paths)

    assert active.is_file()
    assert target.is_dir()
    assert status["requirement_id"] == "REQ-999"
    assert json.loads(
        (target / "reference-index.v1.json").read_text(encoding="utf-8")
    )["requirement_id"] == "REQ-001"


def test_completed_receipt_identity_tamper_blocks_idempotent_result(
    tmp_path: Path,
    transaction_template,
) -> None:
    _project, paths, _package, prepared, original = _prepare(
        tmp_path,
        transaction_template,
    )
    result = commit_prepared_start(paths, prepared)
    receipt_path = (
        paths.sdlc_dir
        / "start-transactions"
        / "completed"
        / f"{result['transaction_id']}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_draft_id"] = "DRAFT-999"
    receipt["formal_manifest"] = [
        *receipt["formal_manifest"],
        {"artifact_id": "伪造", "sha256": canonical_sha256({"伪造": True})},
    ]
    _write_transaction(receipt_path, receipt)

    with pytest.raises(SdlcError, match="真实正式对象身份|正式清单"):
        commit_prepared_start(paths, prepared)

    target = paths.requirements_dir / "REQ-001"
    assert receipt_path.is_file()
    assert target.is_dir()
    assert _original_snapshot(target / "original") == original
