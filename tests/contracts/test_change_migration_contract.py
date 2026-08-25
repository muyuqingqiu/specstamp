from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.core.change_workspace import BASE_VERSION_PATHS, build_base_versions
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state, resolve_requirement
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
)
from codex_sdlc.core.task_outputs import empty_formal_task_output_index
from test_cli_v1 import run_cli
from test_task_plan_v2_contract import _create_project, _import, _task, _write_submission


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(value), encoding="utf-8")


def _set_task_status(project: Path, status: str) -> None:
    paths = build_paths(project)
    append_event(
        paths,
        event_type="task_updated",
        source="t035-contract",
        summary=f"把 T-001 设置为 {status}",
        requirement_id="REQ-001",
        task_id="T-001",
        payload={"status": status},
    )


def _write_effective_versions(requirement_dir: Path) -> None:
    """补齐迁移校验需要读取的三份当前正式版本，内容只使用结构化字段。"""

    _write_json(
        requirement_dir / "effective/requirement.current.json",
        {
            "schema_version": "requirement-current.v1",
            "requirement_id": "REQ-001",
            "source_draft_id": "DRAFT-001",
            "version": "requirement.v1",
            "is_current": True,
            "title": "旧变更迁移",
            "background": "已有正式需求需要迁移旧变更记录。",
            "goal": "迁移依据可以从正式结构核对。",
            "scope": ["迁移旧变更"],
            "out_of_scope": [],
            "user_scenarios": ["确认旧变更分类"],
            "global_rules": [],
            "functional_requirements": [
                {
                    "id": "FR-001",
                    "acceptance_criteria": [{"id": "AC-001"}],
                }
            ],
            "open_questions": [],
        },
    )
    _write_json(
        requirement_dir / "effective/design.current.json",
        {
            "schema_version": "design-current.v1",
            "requirement_id": "REQ-001",
            "source_draft_id": "DRAFT-001",
            "version": "design.v1",
            "is_current": True,
            "artifacts": [],
        },
    )
    _write_json(
        requirement_dir / "effective/test-matrix.current.json",
        {
            "schema_version": "test-matrix-current.v1",
            "requirement_id": "REQ-001",
            "source_draft_id": "DRAFT-001",
            "version": "test-matrix.v1",
            "is_current": True,
            "acceptance_criteria": [
                {"id": "AC-001", "requirement_id": "FR-001"}
            ],
        },
    )


def _register_legacy_sources(
    project: Path,
    requirement_dir: Path,
    source_ids: tuple[str, ...],
) -> dict[str, bytes]:
    paths = build_paths(project)
    sources: dict[str, bytes] = {}
    for source_id in source_ids:
        relative_path = (
            requirement_dir / "changes" / f"{source_id}.md"
        ).relative_to(project).as_posix()
        content = f"# {source_id}\n\n旧变更展示正文不能作为迁移依据。\n".encode()
        target = project / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        append_event(
            paths,
            event_type="change_recorded",
            source="t035-contract",
            summary=f"登记旧变更 {source_id}",
            requirement_id="REQ-001",
            payload={
                "change_id": source_id,
                "summary": "旧变更",
                "description": "旧变更",
                "file_path": relative_path,
            },
        )
        sources[relative_path] = content
    return sources


def _migration_project(
    tmp_path: Path,
    *,
    source_ids: tuple[str, ...] = ("CHG-009", "CHG-010", "CHG-011"),
    task_status: str = "closed",
) -> tuple[Path, Path, dict[str, bytes]]:
    project, records = _create_project(tmp_path)
    requirement_dir = records[0][2]
    submission = _write_submission(
        tmp_path / "任务计划输入",
        tasks=[_task("migration", "迁移旧变更")],
    )
    _import(project, "REQ-001", submission)
    _write_effective_versions(requirement_dir)
    _set_task_status(project, task_status)
    sources = _register_legacy_sources(project, requirement_dir, source_ids)
    return project, requirement_dir, sources


def _restored_package(project: Path, requirement_dir: Path) -> dict[str, object]:
    bases = build_base_versions(build_paths(project), requirement_dir)
    return {
        "schema_version": "change-package.v1",
        "requirement_id": "REQ-001",
        "change_id": "CHG-009",
        "producer_run_id": "migration-test",
        "reason": "由显式结构和引用还原",
        "base_versions": bases,
        "source_refs": ["FR-001", "AC-001"],
        "requirement_operations": [],
        "global_rule_operations": [],
        "acceptance_operations": [],
        "design_operations": [],
        "material_operations": [],
        "task_impacts": {
            "restore": [],
            "add": [],
            "close": [],
            "unaffected": [
                {"task_id": "T-001", "basis_refs": ["FR-001", "AC-001"]}
            ],
        },
        "review_impacts": [
            {
                "stage": "requirement_split",
                "reason_refs": ["FR-001", "AC-001"],
            }
        ],
        "open_questions": [],
    }


def _scan(project: Path) -> dict[str, object]:
    result = run_cli(["change-migrate", "scan"], cwd=project)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _confirmation(
    project: Path,
    requirement_dir: Path,
    scan: dict[str, object],
) -> Path:
    package_path = (
        project
        / ".codex-sdlc/change-migration/restored/CHG-009/change-package.v1.json"
    )
    package = _restored_package(project, requirement_dir)
    _write_json(package_path, package)
    records = []
    for scanned in scan["records"]:
        source_path = scanned["source_path"]
        common = {
            "source_id": scanned["source_id"],
            "source_kind": scanned["source_kind"],
            "source_path": source_path,
            "source_sha256": scanned["source_sha256"],
        }
        if source_path.endswith("CHG-009.md"):
            record = {
                **common,
                "classification": "restored",
                "structured_result": {
                    "requirement_id": "REQ-001",
                    "change_id": "CHG-009",
                    "change_package_path": str(package_path.relative_to(project)),
                    "change_package_sha256": sha256_file(package_path),
                    "reference_ids": ["FR-001", "AC-001"],
                    "task_ids": ["T-001"],
                    "version_ids": [
                        package["base_versions"][name]["path"]
                        for name in BASE_VERSION_PATHS
                    ],
                },
            }
        elif source_path.endswith("CHG-010.md"):
            record = {
                **common,
                "classification": "legacy-note",
                "completed_task_ids": ["T-001"],
            }
        else:
            record = {
                **common,
                "classification": "blocked-rebuild",
                "required_materials": [
                    "用户确认后的正式变更范围",
                    "当前版本对应的 FR、AC 和任务引用",
                ],
            }
        records.append(record)
    payload = {
        "schema_version": "change-migration-confirmation.v1",
        "scan_sha256": scan["scan_sha256"],
        "records": records,
    }
    payload["confirmation_sha256"] = canonical_sha256(payload)
    path = project / "迁移分类确认.json"
    _write_json(path, payload)
    return path


def _refresh_confirmation_hash(project: Path, confirmation: Path) -> None:
    document = json.loads(confirmation.read_text(encoding="utf-8"))
    restored = next(
        (
            item
            for item in document["records"]
            if item["classification"] == "restored"
        ),
        None,
    )
    if restored is not None:
        package_path = project / restored["structured_result"]["change_package_path"]
        restored["structured_result"]["change_package_sha256"] = sha256_file(
            package_path
        )
    document.pop("confirmation_sha256", None)
    document["confirmation_sha256"] = canonical_sha256(document)
    _write_json(confirmation, document)


def test_scan_is_read_only_and_confirmation_registers_each_source_once(tmp_path: Path) -> None:
    project, requirement_dir, originals = _migration_project(tmp_path)
    before_events = (project / ".codex-sdlc/events.jsonl").read_bytes()

    scan = _scan(project)

    assert scan["schema_version"] == "change-migration-scan.v1"
    assert [item["source_path"] for item in scan["records"]] == sorted(originals)
    assert scan["scan_sha256"] == canonical_sha256(
        {"schema_version": scan["schema_version"], "records": scan["records"]}
    )
    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == before_events
    assert not (project / ".codex-sdlc/change-migration/registry.v1.json").exists()

    confirmation = _confirmation(project, requirement_dir, scan)
    result = run_cli(
        ["change-migrate", "confirm", "--file", str(confirmation.relative_to(project))],
        cwd=project,
    )

    assert result.returncode == 0, result.stderr
    assert "迁移分类已登记：3 条" in result.stdout
    registry_path = project / ".codex-sdlc/change-migration/registry.v1.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["schema_version"] == "change-migration-registry.v1"
    assert [item["classification"] for item in registry["records"]] == [
        "restored",
        "legacy-note",
        "blocked-rebuild",
    ]
    assert registry["registry_sha256"] == canonical_sha256(
        {key: value for key, value in registry.items() if key != "registry_sha256"}
    )
    assert len({item["source_path"] for item in registry["records"]}) == len(originals)
    for relative_path, original in originals.items():
        assert (project / relative_path).read_bytes() == original


def test_confirmation_rejects_missing_duplicate_and_stale_sources_without_partial_success(tmp_path: Path) -> None:
    project, requirement_dir, _originals = _migration_project(tmp_path)
    scan = _scan(project)
    confirmation = _confirmation(project, requirement_dir, scan)
    document = json.loads(confirmation.read_text(encoding="utf-8"))

    missing = dict(document)
    missing["records"] = document["records"][:-1]
    missing["confirmation_sha256"] = canonical_sha256(
        {key: value for key, value in missing.items() if key != "confirmation_sha256"}
    )
    _write_json(confirmation, missing)
    result = run_cli(["change-migrate", "confirm", "--file", confirmation.name], cwd=project)
    assert result.returncode == 1
    assert "必须逐条覆盖本次扫描清单" in result.stderr

    duplicate = dict(document)
    duplicate["records"] = [*document["records"], document["records"][0]]
    duplicate["confirmation_sha256"] = canonical_sha256(
        {key: value for key, value in duplicate.items() if key != "confirmation_sha256"}
    )
    _write_json(confirmation, duplicate)
    result = run_cli(["change-migrate", "confirm", "--file", confirmation.name], cwd=project)
    assert result.returncode == 1
    assert "同一来源只能分类一次" in result.stderr

    _write_json(confirmation, document)
    source = next(project.glob(".codex-sdlc/requirements/*/changes/CHG-011.md"))
    source.write_text("changed\n", encoding="utf-8")
    result = run_cli(["change-migrate", "confirm", "--file", confirmation.name], cwd=project)
    assert result.returncode == 1
    assert "来源清单已经变化" in result.stderr
    assert not (project / ".codex-sdlc/change-migration/registry.v1.json").exists()


def test_interruption_cleans_owned_temporary_file_and_retry_completes(tmp_path: Path) -> None:
    from codex_sdlc.core.change_migration import (
        INTERRUPT_BEFORE_REGISTRY_PUBLISH,
        register_change_migration,
    )

    project, requirement_dir, _originals = _migration_project(tmp_path)
    scan = _scan(project)
    confirmation = _confirmation(project, requirement_dir, scan)
    paths = build_paths(project)

    def interrupt(stage: str) -> None:
        if stage == INTERRUPT_BEFORE_REGISTRY_PUBLISH:
            raise RuntimeError("固定中断")

    with pytest.raises(RuntimeError, match="固定中断"):
        register_change_migration(paths, confirmation.name, interruption_hook=interrupt)

    registry_dir = project / ".codex-sdlc/change-migration"
    assert not (registry_dir / "registry.v1.json").exists()
    assert list(registry_dir.glob(".registry.v1.json.*.tmp")) == []

    result = register_change_migration(paths, confirmation.name)
    assert result["registered_count"] == 3
    assert (registry_dir / "registry.v1.json").exists()


def test_blocked_or_stale_registry_stops_formal_change_progress(tmp_path: Path) -> None:
    project, requirement_dir, _originals = _migration_project(tmp_path)
    scan = _scan(project)
    confirmation = _confirmation(project, requirement_dir, scan)
    assert run_cli(["change-migrate", "confirm", "--file", confirmation.name], cwd=project).returncode == 0

    package_path = project / ".codex-sdlc/change-migration/restored/CHG-009/change-package.v1.json"
    original_package = package_path.read_bytes()
    package_path.write_text("{}\n", encoding="utf-8")
    changed_package = run_cli(
        ["change-create", "REQ-001", "--request-key", "changed-restored-package"],
        cwd=project,
    )
    assert changed_package.returncode == 1
    assert "迁移分类已经失效" in changed_package.stderr
    assert "change-package.v1" in changed_package.stderr
    package_path.write_bytes(original_package)

    blocked = run_cli(["change-accept", "REQ-001", "CHG-011"], cwd=project)
    assert blocked.returncode == 1
    assert "CHG-011" in blocked.stderr
    assert "blocked-rebuild" in blocked.stderr

    source = next(project.glob(".codex-sdlc/requirements/*/changes/CHG-010.md"))
    source.write_text("stale\n", encoding="utf-8")
    stale = run_cli(
        ["change-create", "REQ-001", "--request-key", "stale-migration"],
        cwd=project,
    )
    assert stale.returncode == 1
    assert "迁移分类已经失效" in stale.stderr


@pytest.mark.parametrize(
    "case",
    [
        "change_owner",
        "requirement_owner",
        "references",
        "task_plan",
        "version_path",
        "version_hash",
    ],
)
def test_restored_rejects_self_reported_basis_without_current_formal_evidence(
    tmp_path: Path,
    case: str,
) -> None:
    project, requirement_dir, _originals = _migration_project(
        tmp_path,
        source_ids=("CHG-009",),
    )
    confirmation = _confirmation(project, requirement_dir, _scan(project))
    document = json.loads(confirmation.read_text(encoding="utf-8"))
    restored = document["records"][0]
    package_path = project / restored["structured_result"]["change_package_path"]
    package = json.loads(package_path.read_text(encoding="utf-8"))

    if case == "change_owner":
        package["change_id"] = "CHG-999"
        restored["structured_result"]["change_id"] = "CHG-999"
    elif case == "requirement_owner":
        package["requirement_id"] = "REQ-999"
        restored["structured_result"]["requirement_id"] = "REQ-999"
    elif case == "references":
        package["source_refs"] = ["FR-999", "AC-999"]
        package["task_impacts"]["unaffected"][0]["basis_refs"] = [
            "FR-999",
            "AC-999",
        ]
        package["review_impacts"][0]["reason_refs"] = ["FR-999", "AC-999"]
        restored["structured_result"]["reference_ids"] = ["FR-999", "AC-999"]
    elif case == "task_plan":
        package["task_impacts"]["unaffected"][0]["task_id"] = "T-999"
        restored["structured_result"]["task_ids"] = ["T-999"]
    elif case == "version_path":
        package["base_versions"]["requirement"]["path"] = ".codex-sdlc/不存在.json"
        restored["structured_result"]["version_ids"][0] = ".codex-sdlc/不存在.json"
    elif case == "version_hash":
        package["base_versions"]["requirement"]["sha256"] = "0" * 64

    _write_json(package_path, package)
    _write_json(confirmation, document)
    _refresh_confirmation_hash(project, confirmation)
    result = run_cli(
        ["change-migrate", "confirm", "--file", confirmation.name],
        cwd=project,
    )

    assert result.returncode != 0
    assert not (project / ".codex-sdlc/change-migration/registry.v1.json").exists()


@pytest.mark.parametrize(
    ("task_status", "completed_task_id"),
    [
        ("closed", "T-999"),
        ("todo", "T-001"),
        ("doing", "T-001"),
        ("blocked", "T-001"),
    ],
)
def test_legacy_note_rejects_missing_or_unfinished_tasks(
    tmp_path: Path,
    task_status: str,
    completed_task_id: str,
) -> None:
    project, requirement_dir, _originals = _migration_project(
        tmp_path,
        source_ids=("CHG-010",),
        task_status=task_status,
    )
    confirmation = _confirmation(project, requirement_dir, _scan(project))
    document = json.loads(confirmation.read_text(encoding="utf-8"))
    document["records"][0]["completed_task_ids"] = [completed_task_id]
    document.pop("confirmation_sha256")
    document["confirmation_sha256"] = canonical_sha256(document)
    _write_json(confirmation, document)

    result = run_cli(
        ["change-migrate", "confirm", "--file", confirmation.name],
        cwd=project,
    )

    assert result.returncode != 0
    assert not (project / ".codex-sdlc/change-migration/registry.v1.json").exists()


def _write_done_task_runtime(project: Path, requirement_dir: Path) -> Path:
    task_path = requirement_dir / "tasks/T-001.json"
    task_sha = sha256_file(task_path)
    run = {
        "schema_version": "task-run.v1",
        "requirement_id": "REQ-001",
        "task_id": "T-001",
        "run_number": 1,
        "runner_thread_id": "t035-contract",
        "status": "closed",
        "task_sha256": task_sha,
        "task_review_sha256": "1" * 64,
        "read_manifest_sha256": "2" * 64,
        "upstream_hashes": {
            "requirement": "3" * 64,
            "design": "4" * 64,
            "reference_index": "5" * 64,
            "project_rules": "6" * 64,
            "dependencies": "7" * 64,
            "predecessor_outputs": "8" * 64,
        },
        "code_baseline": {
            "project_path": str(project),
            "repo_key": "repo",
            "branch_key": "branch",
            "worktree_key": "worktree",
            "git_head": "",
            "worktree_diff_sha256": "9" * 64,
        },
        "allowed_output_paths": [],
        "test_records": [],
        "feedback_records": [],
        "verification_records": [],
        "started_at": "2026-07-23T01:00:00+08:00",
        "read_confirmation": None,
    }
    run_path = requirement_dir / "runtime/T-001/runs/0001/task-run.v1.json"
    _write_json(run_path, run)
    run_sha = sha256_file(run_path)
    _write_json(
        requirement_dir / "runtime/T-001/current.json",
        {
            "schema_version": "task-run-current.v1",
            "requirement_id": "REQ-001",
            "task_id": "T-001",
            "run_number": 1,
            "run_path": "runs/0001/task-run.v1.json",
            "manifest_path": "runs/0001/task-read-manifest.v1.json",
            "status": "closed",
            "task_run_sha256": run_sha,
            "read_manifest_sha256": "2" * 64,
            "updated_at": "2026-07-23T01:01:00+08:00",
        },
    )
    state = derive_state(build_paths(project))
    requirement = resolve_requirement(state, "REQ-001")
    index = empty_formal_task_output_index("REQ-001")
    index["task_outputs"] = [
        {
            "entry_id": "REQ-001:T-001:run-0001",
            "task_id": "T-001",
            "completed_run_number": 1,
            "task_sha256": task_sha,
            "task_run_path": run_path.relative_to(project).as_posix(),
            "task_run_sha256": run_sha,
            "files": [],
        }
    ]
    _write_json(
        requirement_dir / "task-outputs/task-output-index.v1.json",
        index,
    )
    assert requirement["requirement_id"] == "REQ-001"
    return task_path


def test_legacy_note_accepts_done_task_and_rejects_task_hash_drift_without_replacing_registry(
    tmp_path: Path,
) -> None:
    project, requirement_dir, _originals = _migration_project(
        tmp_path,
        source_ids=("CHG-010",),
        task_status="done",
    )
    task_path = _write_done_task_runtime(project, requirement_dir)
    confirmation = _confirmation(project, requirement_dir, _scan(project))

    accepted = run_cli(
        ["change-migrate", "confirm", "--file", confirmation.name],
        cwd=project,
    )
    assert accepted.returncode == 0, accepted.stderr
    registry_path = project / ".codex-sdlc/change-migration/registry.v1.json"
    registry_before = registry_path.read_bytes()

    task_document = json.loads(task_path.read_text(encoding="utf-8"))
    task_document["title"] = "发生状态哈希漂移的任务"
    _write_json(task_path, task_document)
    rejected = run_cli(
        ["change-migrate", "confirm", "--file", confirmation.name],
        cwd=project,
    )

    assert rejected.returncode != 0
    assert "哈希" in rejected.stderr or "关闭轮次" in rejected.stderr
    assert registry_path.read_bytes() == registry_before
