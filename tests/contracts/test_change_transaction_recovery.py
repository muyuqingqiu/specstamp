from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state, load_events
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_file
from codex_sdlc.services import change_service
from test_change_review_invalidation import _fully_reviewed_projected_change
from test_cli_v1 import run_cli_raw


def _protected_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, dict[str, object]]:
    project, status = _fully_reviewed_projected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    change_service.protect_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        confirm_requirement=True,
    )
    requirement_root = project / ".codex-sdlc/requirements/REQ-001-订单审批"
    return project, requirement_root, status


def _formal_snapshot(requirement_root: Path) -> dict[str, str]:
    return {
        relative: sha256_file(requirement_root / relative)
        for relative in (
            "effective/requirement.current.json",
            "effective/design.current.json",
            "effective/test-matrix.current.json",
            "reference-index.v1.json",
            "tasks/task-plan.v2.json",
        )
    }


def test_accept_commits_five_versions_effective_files_and_one_stable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    result = change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )

    assert result["target_version"] == 2
    assert result["idempotent"] is False
    version_paths = result["version_files_sha256"]
    assert set(version_paths) == {
        "versions/requirement.v2.json",
        "versions/design.v2.json",
        "versions/test-matrix.v2.json",
        "versions/reference-index.v2.json",
        "versions/task-plan.v2.json",
    }
    for relative, digest in version_paths.items():
        assert sha256_file(requirement_root / relative) == digest

    workspace = project / ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001"
    for kind in ("requirement", "design", "test-matrix"):
        projected = json.loads(
            (workspace / f"projected-{kind}.v2.json").read_text(encoding="utf-8")
        )["content"]
        current = json.loads(
            (requirement_root / f"effective/{kind}.current.json").read_text(encoding="utf-8")
        )
        projected["is_current"] = True
        assert current == projected
    projected_reference = json.loads(
        (workspace / "projected-reference-index.v2.json").read_text(encoding="utf-8")
    )["content"]
    for reference_id in ("AC-002", "FR-002"):
        projected_reference["entries"][reference_id]["path"] = (
            "original/changes/CHG-001/change-package.v1.json"
        )
    projected_task_plan = json.loads(
        (workspace / "projected-task-plan.v2.json").read_text(encoding="utf-8")
    )["content"]
    assert json.loads((requirement_root / "reference-index.v1.json").read_text(encoding="utf-8")) == projected_reference
    assert (
        requirement_root / "original/changes/CHG-001/change-package.v1.json"
    ).read_bytes() == (workspace / "change-package.v1.json").read_bytes()
    assert json.loads((requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8")) == projected_task_plan

    accepted = [event for event in load_events(paths) if event.get("event_type") == "change_accepted"]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["transaction_id"] == result["transaction_id"]
    state_requirement = derive_state(paths)["requirements"]["REQ-001"]
    assert state_requirement["structured"]["requirement_version"] == "requirement.v2"
    assert state_requirement["blocked_reason"] == ""
    before_events = paths.events_file.read_bytes()
    retry = change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    assert retry["idempotent"] is True
    assert retry["transaction_id"] == result["transaction_id"]
    assert paths.events_file.read_bytes() == before_events


def test_base_drift_is_rejected_before_transaction_or_new_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    target = requirement_root / "effective/requirement.current.json"
    target.write_bytes(target.read_bytes() + b" ")
    before = paths.events_file.read_bytes()
    with pytest.raises(SdlcError, match="基础版本.*漂移"):
        change_service.accept_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
        )
    assert paths.events_file.read_bytes() == before
    assert not (requirement_root / "versions/requirement.v2.json").exists()
    assert change_service.inspect_change_accept_transactions(paths)["active"] == []


def test_prepare_conflict_cleans_staging_then_same_change_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    conflict = requirement_root / "versions/requirement.v2.json"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text('{"外部冲突":true}\n', encoding="utf-8")

    with pytest.raises(SdlcError, match="目标正式版本已经存在"):
        change_service.accept_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
        )

    active = paths.change_transactions_dir / "accept-active"
    staging = paths.change_transactions_dir / "accept-staging"
    assert list(active.iterdir()) == []
    assert list(staging.iterdir()) == []

    conflict.unlink()
    result = change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    assert result["idempotent"] is False
    assert len(
        [event for event in load_events(paths) if event.get("event_type") == "change_accepted"]
    ) == 1
    assert list(active.iterdir()) == []
    assert list(staging.iterdir()) == []


def test_orphan_accept_staging_makes_doctor_and_backup_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    orphan = paths.change_transactions_dir / "accept-staging/CHANGE-孤立暂存"
    orphan.mkdir(parents=True, mode=0o700)
    (orphan / "new-000.bin").write_bytes(b"orphan")

    doctor = run_cli_raw(["doctor"], project)
    assert doctor.returncode != 0
    assert "无法安全归属" in doctor.stdout + doctor.stderr
    report = change_service.inspect_change_accept_transactions(paths)
    assert any("无法安全归属" in message for message in report["failed"])

    backup = run_cli_raw(
        ["backup", "REQ-001", "--label", "孤立暂存门禁"],
        project,
        extra_env={"CODEX_SDLC_BACKUP_HOME": str(tmp_path / "备份")},
    )
    assert backup.returncode != 0
    assert "无法安全归属" in backup.stdout + backup.stderr


@pytest.mark.parametrize(
    "stage",
    [
        "after_version_requirement",
        "after_version_design",
        "after_version_test_matrix",
        "after_version_reference_index",
        "after_version_task_plan",
        "after_change_event_append",
        "after_effective_requirement",
        "after_effective_design",
        "after_effective_test_matrix",
        "after_reference_index",
        "after_reference_source",
        "after_task_plan",
        "after_status",
    ],
)
def test_each_publish_point_rolls_back_then_retry_commits_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    before_files = _formal_snapshot(requirement_root)
    before_events = paths.events_file.read_bytes()

    def interrupt(current: str) -> None:
        if current == stage:
            raise SdlcError(f"故障注入：{stage}")

    with pytest.raises(SdlcError, match="故障注入"):
        change_service.accept_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            interruption_hook=interrupt,
        )
    assert _formal_snapshot(requirement_root) == before_files
    assert paths.events_file.read_bytes() == before_events
    assert not list((requirement_root / "versions").glob("*.v2.json"))
    assert change_service.inspect_change_accept_transactions(paths)["active"] == []

    change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    assert len([event for event in load_events(paths) if event.get("event_type") == "change_accepted"]) == 1


def test_process_interruption_is_completed_by_recovery_without_duplicate_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)

    class ProcessInterrupted(BaseException):
        pass

    def interrupt(current: str) -> None:
        if current == "after_change_event_append":
            raise ProcessInterrupted()

    with pytest.raises(ProcessInterrupted):
        change_service.accept_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            interruption_hook=interrupt,
        )
    assert len(change_service.inspect_change_accept_transactions(paths)["active"]) == 1

    recovered = change_service.recover_change_accept_transactions(paths)
    assert recovered["completed"] == 1
    assert change_service.inspect_change_accept_transactions(paths)["active"] == []
    assert (requirement_root / "versions/requirement.v2.json").is_file()
    assert len([event for event in load_events(paths) if event.get("event_type") == "change_accepted"]) == 1
    retry = change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    assert retry["idempotent"] is True


def test_doctor_report_cross_checks_completed_receipt_and_formal_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    change_service.accept_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    passed = change_service.inspect_change_accept_transactions(paths)
    assert passed["failed"] == []
    assert len(passed["completed"]) == 1

    (requirement_root / "versions/design.v2.json").write_text("{}\n", encoding="utf-8")
    failed = change_service.inspect_change_accept_transactions(paths)
    assert any("design.v2.json" in item for item in failed["failed"])


def test_protection_hash_or_event_drift_is_rejected_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    protection_path = project / str(status["workspace_path"]) / "change-protection.v1.json"
    protection = json.loads(protection_path.read_text(encoding="utf-8"))
    protection["protection_sha256"] = canonical_sha256({"伪造": True})
    protection_path.write_text(json.dumps(protection, ensure_ascii=False), encoding="utf-8")
    before = _formal_snapshot(requirement_root)
    with pytest.raises(SdlcError, match="保护结果"):
        change_service.accept_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
        )
    assert _formal_snapshot(requirement_root) == before


def test_formal_cli_recovers_process_exit_then_accept_retry_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    interrupted = run_cli_raw(
        ["change-accept", "REQ-001", "CHG-001"],
        project,
        extra_env={
            "CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT": "after_change_event_append",
            "CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT_MODE": "process_exit",
        },
    )
    assert interrupted.returncode == 86
    assert len(change_service.inspect_change_accept_transactions(build_paths(project))["active"]) == 1

    status = run_cli_raw(["status"], project)
    assert status.returncode == 0, status.stderr
    assert change_service.inspect_change_accept_transactions(build_paths(project))["active"] == []
    assert (requirement_root / "versions/requirement.v2.json").is_file()

    retry = run_cli_raw(["change-accept", "REQ-001", "CHG-001"], project)
    assert retry.returncode == 0, retry.stderr
    assert "幂等重试：是" in retry.stdout
    assert len(
        [
            event
            for event in load_events(build_paths(project))
            if event.get("event_type") == "change_accepted"
        ]
    ) == 1


def test_restore_add_and_close_impacts_become_explicit_transaction_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root, _status = _protected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    context = change_service.load_change_package_context_locked(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
    )
    documents = context["documents"]
    documents["change-package.v1.json"]["task_impacts"] = {
        "restore": [{"task_id": "T-001", "reason": "需求重新覆盖", "source_refs": ["FR-001"]}],
        "add": [
            {
                "client_key": "new-task",
                "source_refs": ["FR-002"],
                "next_value": {
                    "title": "补充重试任务",
                    "goal": "交付订单重试能力",
                    "depends_on": [],
                },
            }
        ],
        "close": [{"task_id": "T-003", "reason": "已经被替代", "replacement_refs": ["T-002"]}],
        "unaffected": [],
    }
    documents["projected-task-plan.v2.json"]["content"]["mapping"]["new-task"] = "T-002"
    events = change_service._build_accept_events(
        paths,
        context=context,
        protection={"protection_sha256": "a" * 64, "reviews": []},
        transaction_id="CHANGE-" + "b" * 64,
        target_version=2,
    )
    by_type = {
        (event["event_type"], event.get("task_id")): event for event in events
    }
    assert by_type[("task_updated", "T-001")]["payload"]["status"] == "todo"
    assert by_type[("task_created", "T-002")]["payload"]["summary"] == "交付订单重试能力"
    assert by_type[("task_updated", "T-003")]["payload"]["status"] == "closed"
