from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import pytest

from codex_sdlc.commands import plan_cmd
from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.core.code_evidence import (
    assess_task_planning_code_evidence,
    capture_code_evidence,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_coverage_contract import prepare_task_planning_documents


def _git(project: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _project(
    tmp_path: Path,
    *,
    nested: bool = False,
    git_project: bool = True,
) -> tuple[Path, Path]:
    repository = tmp_path / "规划证据仓库"
    project = repository / "子项目" if nested else repository
    project.mkdir(parents=True)
    rule_root = repository if nested else project
    (rule_root / "AGENTS.md").write_text(
        "所有开发必须运行合同测试。\n",
        encoding="utf-8",
    )
    (project / "pyproject.toml").write_text("[project]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    requirement_root = (
        project
        / ".codex-sdlc"
        / "requirements"
        / "REQ-001-任务规划证据"
    )
    requirement_root.mkdir(parents=True)
    (requirement_root / "original").mkdir()
    (requirement_root / "original" / "formal.v3.json").write_text(
        '{"formal_contract_version":"formal.v3","workflow_profile":"document-first.v1"}\n',
        encoding="utf-8",
    )
    formal_digest = sha256_file(requirement_root / "original" / "formal.v3.json")
    locator = {
        "schema_version": "reference-locator.v1",
        "path": "original/formal.v3.json",
        "sha256": formal_digest,
        "locator": {"kind": "whole_file"},
    }
    reference_index = {
        "schema_version": "reference-index.v1",
        "requirement_id": "REQ-001",
        "entries": {
            "FR-001": locator,
            "GR-001": locator,
            "AC-001": locator,
            "DES-001#architecture": locator,
            "DATA-001": locator,
            "MAT-001": locator,
        },
    }
    (requirement_root / "reference-index.v1.json").write_text(
        json.dumps(reference_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    events_file = project / ".codex-sdlc" / "events.jsonl"
    events_file.write_text(
        json.dumps(
            {
                "event_id": "EVT-20260720-000001",
                "event_type": "requirement_created",
                "project_path": str(project),
                "requirement_id": "REQ-001",
                "task_id": None,
                "created_at": "2026-07-20T18:00:00+08:00",
                "source": "合同测试",
                "summary": "创建任务规划证据需求",
                "payload": {
                    "title": "任务规划证据",
                    "description": "验证任务覆盖和规划证据。",
                    "summary": "验证任务覆盖和规划证据。",
                    "folder_name": requirement_root.name,
                    "flow_type": "SDLC 原生正式流程",
                    "native_start": {
                        "formal_contract_version": "formal.v3"
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    if git_project:
        _git(repository, "init", "-q")
        # 正式项目会把本机状态目录加入 Git 忽略；测试仓库必须自己声明，
        # 不能依赖开发机的全局 excludesfile 或其它测试先修改 HOME。
        (repository / ".git" / "info" / "exclude").write_text(
            ".codex-sdlc/\n",
            encoding="utf-8",
        )
        _git(repository, "config", "user.email", "contract@example.invalid")
        _git(repository, "config", "user.name", "合同测试")
        _git(repository, "add", ".")
        _git(repository, "commit", "-qm", "建立规划证据测试项目")
    return project, requirement_root


def _selection(requirement_root: Path) -> dict[str, object]:
    return {
        "purpose": "task_planning",
        "rules": ["AGENTS.md"],
        "dependencies": ["pyproject.toml"],
        "code_files": [{"path": "src/app.py", "reason_ref": "FR-001"}],
        "upstream_outputs": [
            requirement_root.relative_to(requirement_root.parents[2])
            .joinpath(relative_path)
            .as_posix()
            for relative_path in (
                "reference-index.v1.json",
                "original/formal.v3.json",
            )
        ],
    }


def _write_task_submission(
    root: Path,
    requirement_root: Path,
    *,
    explicit_evidence: bool = True,
    depends_on: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    source = root / "命令任务产物"
    tasks_dir = source / "任务"
    tasks_dir.mkdir(parents=True)
    plan_file = source / "task-plan.v2.json"
    coverage_file = source / "task-coverage.v1.json"
    task = {
        "schema_version": "task.v2",
        "requirement_id": "REQ-001",
        "client_key": "main",
        "title": "实现规划证据",
        "goal": "交付可复核的任务覆盖和规划证据。",
        "deliverables": ["任务覆盖和规划证据可以从正式文件复核。"],
        "depends_on": depends_on or [],
        "requirement_refs": ["FR-001"],
        "global_rule_refs": ["GR-001"],
        "technical_solution_refs": [
            {"id": "DES-001", "reference_key": "DES-001#architecture"}
        ],
        "design_refs": ["DATA-001"],
        "material_refs": ["MAT-001"],
        "change_refs": [],
        "acceptance_refs": ["AC-001"],
        "code_scope": {
            "read_paths": ["src/app.py"],
            "likely_change_paths": ["src/app.py"],
            "protected_paths": [".codex-sdlc/requirements"],
        },
        "implementation_requirements": ["覆盖关系只使用结构化编号。"],
        "data_api_page_component_requirements": ["不涉及页面。"],
        "states_and_exceptions": ["证据漂移时任务审核过期。"],
        "security_and_privacy": ["产物不写入账号、密码或令牌。"],
        "automated_tests": ["运行任务规划证据合同测试。"],
        "manual_checks": ["核对正式计划中的证据哈希。"],
        "out_of_scope": ["不执行任务审核。"],
        "blocking_conditions": [],
        "definition_of_done": ["正式计划、覆盖和事件可以相互核对。"],
    }
    plan: dict[str, object] = {
        "schema_version": "task-plan.v2",
        "requirement_id": "REQ-001",
        "tasks": ["@client:main"],
        "dependencies": [
            {"from": dependency, "to": "@client:main"}
            for dependency in depends_on or []
        ],
    }
    if explicit_evidence:
        plan["code_evidence"] = _selection(requirement_root)
    plan_file.write_text(
        json.dumps(plan, ensure_ascii=False),
        encoding="utf-8",
    )
    (tasks_dir / "main.task.v2.json").write_text(
        json.dumps(task, ensure_ascii=False),
        encoding="utf-8",
    )
    coverage_file.write_text(
        json.dumps(
            {
                "schema_version": "task-coverage.v1",
                "requirement_id": "REQ-001",
                "functional_requirements": {
                    "FR-001": {"tasks": ["@client:main"], "status": "implemented"}
                },
                "design_artifacts": {
                    "DATA-001": {"tasks": ["@client:main"]}
                },
                "acceptance_criteria": {
                    "AC-001": {
                        "tasks": ["@client:main"],
                        "test_refs": ["@client:main#automated_tests/0"],
                    }
                },
                "effective_changes": {},
                "no_development_items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan_file, tasks_dir, coverage_file


def _task_args(submission: tuple[Path, Path, Path]):
    return type(
        "参数",
        (),
        {
            "requirement_id": "REQ-001",
            "plan_file": str(submission[0]),
            "tasks_dir": str(submission[1]),
            "coverage_file": str(submission[2]),
        },
    )()


def _assert_no_task_import_residue(project: Path, requirement_root: Path) -> None:
    assert not (requirement_root / "tasks").exists()
    assert not (requirement_root / "task-coverage.v1.json").exists()
    transaction_root = project / ".codex-sdlc" / "task-import-transactions"
    assert not transaction_root.exists() or not list(transaction_root.rglob("*"))


def _append_task_event(
    project: Path,
    *,
    event_id: str,
    event_type: str,
    task_id: str,
    payload: dict[str, object],
) -> None:
    event = {
        "event_id": event_id,
        "event_type": event_type,
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": task_id,
        "created_at": "2026-07-20T18:01:00+08:00",
        "source": "合同测试",
        "summary": "记录正式任务交付声明",
        "payload": payload,
    }
    with (project / ".codex-sdlc/events.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def test_task_planning_evidence_records_real_rules_code_and_upstream_outputs(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    paths = build_paths(project)

    evidence = capture_code_evidence(
        paths,
        owner_id="REQ-001",
        selection=_selection(requirement_root),
    )

    assert evidence["owner_id"] == "REQ-001"
    assert evidence["purpose"] == "task_planning"
    assert [item["path"] for item in evidence["rules"]] == ["AGENTS.md"]
    assert [item["path"] for item in evidence["code_files"]] == ["src/app.py"]
    assert any(
        item["path"].endswith("reference-index.v1.json")
        for item in evidence["upstream_outputs"]
    )


def test_unchanged_retry_reuses_evidence_and_related_change_becomes_stale(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    paths = build_paths(project)
    selection = _selection(requirement_root)
    first = capture_code_evidence(
        paths,
        owner_id="REQ-001",
        selection=selection,
    )
    time.sleep(1.1)
    second = capture_code_evidence(
        paths,
        owner_id="REQ-001",
        selection=selection,
        reuse_evidence=first,
    )
    assert second == first

    (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assessment = assess_task_planning_code_evidence(paths, first, tasks=[])
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["src/app.py"]


def test_started_task_output_does_not_repeatedly_invalidate_whole_plan(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    paths = build_paths(project)
    evidence = capture_code_evidence(
        paths,
        owner_id="REQ-001",
        selection=_selection(requirement_root),
    )
    (project / "src" / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    assessment = assess_task_planning_code_evidence(
        paths,
        evidence,
        tasks=[
            {
                "task_id": "T-001",
                "status": "doing",
                "task_contract": {
                    "code_scope": {
                        "read_paths": ["src/app.py"],
                        "likely_change_paths": ["src/app.py"],
                        "protected_paths": [],
                    }
                },
            }
        ],
    )

    assert assessment["status"] == "current"
    assert assessment["ignored_task_output_paths"] == ["src/app.py"]


def test_prepare_documents_validates_coverage_and_replaces_selection_with_evidence(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    source = tmp_path / "模型任务产物"
    tasks_dir = source / "任务"
    tasks_dir.mkdir(parents=True)
    plan_file = source / "task-plan.v2.json"
    coverage_file = source / "task-coverage.v1.json"
    task = {
        "schema_version": "task.v2",
        "requirement_id": "REQ-001",
        "client_key": "main",
        "depends_on": [],
        "requirement_refs": ["FR-001"],
        "design_refs": ["DATA-001"],
        "acceptance_refs": ["AC-001"],
        "change_refs": [],
        "automated_tests": ["运行规划证据合同测试。"],
        "code_scope": {
            "read_paths": ["src/app.py"],
            "likely_change_paths": ["src/app.py"],
            "protected_paths": [".codex-sdlc/requirements"],
        },
    }
    plan_file.write_text(
        json.dumps(
            {
                "schema_version": "task-plan.v2",
                "requirement_id": "REQ-001",
                "tasks": ["@client:main"],
                "dependencies": [],
                "code_evidence": _selection(requirement_root),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tasks_dir / "main.task.v2.json").write_text(
        json.dumps(task, ensure_ascii=False),
        encoding="utf-8",
    )
    coverage_file.write_text(
        json.dumps(
            {
                "schema_version": "task-coverage.v1",
                "requirement_id": "REQ-001",
                "functional_requirements": {
                    "FR-001": {"tasks": ["@client:main"], "status": "implemented"}
                },
                "design_artifacts": {
                    "DATA-001": {"tasks": ["@client:main"]}
                },
                "acceptance_criteria": {
                    "AC-001": {
                        "tasks": ["@client:main"],
                        "test_refs": ["@client:main#automated_tests/0"],
                    }
                },
                "effective_changes": {},
                "no_development_items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prepared = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=plan_file,
        tasks_dir=tasks_dir,
        coverage_file=coverage_file,
        effective_change_ids=set(),
    )

    assert prepared["plan"]["code_evidence"]["purpose"] == "task_planning"
    assert prepared["plan"]["code_evidence"]["owner_id"] == "REQ-001"
    assert prepared["coverage"]["acceptance_criteria"]["AC-001"]["test_refs"] == [
        "@client:main#automated_tests/0"
    ]

    implicit_plan = json.loads(plan_file.read_text(encoding="utf-8"))
    implicit_plan.pop("code_evidence")
    plan_file.write_text(
        json.dumps(implicit_plan, ensure_ascii=False),
        encoding="utf-8",
    )
    implicit = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=plan_file,
        tasks_dir=tasks_dir,
        coverage_file=coverage_file,
        effective_change_ids=set(),
    )
    assert implicit["plan"]["code_evidence"]["purpose"] == "task_planning"
    assert [item["path"] for item in implicit["plan"]["code_evidence"]["rules"]] == [
        "AGENTS.md"
    ]


def test_tasks_command_persists_planning_evidence_and_retry_keeps_same_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, requirement_root = _project(tmp_path)
    source = tmp_path / "命令任务产物"
    tasks_dir = source / "任务"
    tasks_dir.mkdir(parents=True)
    plan_file = source / "task-plan.v2.json"
    coverage_file = source / "task-coverage.v1.json"
    task = {
        "schema_version": "task.v2",
        "requirement_id": "REQ-001",
        "client_key": "main",
        "title": "实现规划证据",
        "goal": "交付可复核的任务覆盖和规划证据。",
        "deliverables": ["任务覆盖和规划证据可以从正式文件复核。"],
        "depends_on": [],
        "requirement_refs": ["FR-001"],
        "global_rule_refs": ["GR-001"],
        "technical_solution_refs": [
            {"id": "DES-001", "reference_key": "DES-001#architecture"}
        ],
        "design_refs": ["DATA-001"],
        "material_refs": ["MAT-001"],
        "change_refs": [],
        "acceptance_refs": ["AC-001"],
        "code_scope": {
            "read_paths": ["src/app.py"],
            "likely_change_paths": ["src/app.py"],
            "protected_paths": [".codex-sdlc/requirements"],
        },
        "implementation_requirements": ["覆盖关系只使用结构化编号。"],
        "data_api_page_component_requirements": ["不涉及页面。"],
        "states_and_exceptions": ["证据漂移时任务审核过期。"],
        "security_and_privacy": ["产物不写入账号、密码或令牌。"],
        "automated_tests": ["运行任务规划证据合同测试。"],
        "manual_checks": ["核对正式计划中的证据哈希。"],
        "out_of_scope": ["不执行任务审核。"],
        "blocking_conditions": [],
        "definition_of_done": ["正式计划、覆盖和事件可以相互核对。"],
    }
    plan_file.write_text(
        json.dumps(
            {
                "schema_version": "task-plan.v2",
                "requirement_id": "REQ-001",
                "tasks": ["@client:main"],
                "dependencies": [],
                "code_evidence": _selection(requirement_root),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tasks_dir / "main.task.v2.json").write_text(
        json.dumps(task, ensure_ascii=False),
        encoding="utf-8",
    )
    coverage_file.write_text(
        json.dumps(
            {
                "schema_version": "task-coverage.v1",
                "requirement_id": "REQ-001",
                "functional_requirements": {
                    "FR-001": {"tasks": ["@client:main"], "status": "implemented"}
                },
                "design_artifacts": {
                    "DATA-001": {"tasks": ["@client:main"]}
                },
                "acceptance_criteria": {
                    "AC-001": {
                        "tasks": ["@client:main"],
                        "test_refs": ["@client:main#automated_tests/0"],
                    }
                },
                "effective_changes": {},
                "no_development_items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = type(
        "参数",
        (),
        {
            "requirement_id": "REQ-001",
            "plan_file": str(plan_file),
            "tasks_dir": str(tasks_dir),
            "coverage_file": str(coverage_file),
        },
    )()
    monkeypatch.chdir(project)

    assert run_tasks_finalize(args) == 0
    first_plan = json.loads(
        (requirement_root / "tasks" / "task-plan.v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_plan["code_evidence"]["purpose"] == "task_planning"
    assert first_plan["code_evidence"]["owner_id"] == "REQ-001"
    assert run_tasks_finalize(args) == 0
    second_plan = json.loads(
        (requirement_root / "tasks" / "task-plan.v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert second_plan == first_plan
    state = derive_state(build_paths(project))
    requirement = state["requirements"]["REQ-001"]
    assert requirement["task_planning_evidence_status"] == "current"
    assert requirement["tasks"][0]["coverage_points"] == ["FR-001"]
    assert requirement["tasks"][0]["task_test_refs"] == [
        "T-001#automated_tests/0"
    ]
    task_contract = json.loads(
        json.dumps(requirement["tasks"][0]["task_contract"], ensure_ascii=False)
    )
    (project / "src" / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
    stale_requirement = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert stale_requirement["task_planning_evidence_status"] == "stale"
    assert stale_requirement["tasks"][0]["task_contract"] == task_contract


def test_non_git_task_plan_records_versioned_filesystem_identity_and_detects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path, git_project=False)
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
    )
    monkeypatch.chdir(project)

    assert run_tasks_finalize(_task_args(submission)) == 0

    saved_plan = json.loads(
        (requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8")
    )
    evidence = saved_plan["code_evidence"]
    assert evidence["identity_contract"] == "filesystem-worktree.v1"
    assert len(evidence["repo_key"]) == 64
    assert len(evidence["worktree_key"]) == 64
    assert len(evidence["git_head"]) == 64
    assert evidence["code_files"][0]["path"] == "src/app.py"
    assert (
        derive_state(build_paths(project))["requirements"]["REQ-001"][
            "task_planning_evidence_status"
        ]
        == "current"
    )

    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert changed["task_planning_evidence_status"] == "stale"
    assert changed["task_planning_code_evidence_state"]["changed_paths"] == [
        "src/app.py"
    ]
    assert (
        changed["task_planning_code_evidence_state"]["workspace_state_changed"]
        is True
    )


def test_non_git_missing_read_path_is_recorded_and_becomes_stale_when_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path, git_project=False)
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
    )
    task_file = submission[1] / "main.task.v2.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task["code_scope"]["read_paths"] = ["src/later.py"]
    task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(project)

    assert run_tasks_finalize(_task_args(submission)) == 0

    saved_plan = json.loads(
        (requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8")
    )
    missing_entry = next(
        item
        for item in saved_plan["code_evidence"]["code_files"]
        if item["path"] == "src/later.py"
    )
    assert missing_entry["state"] == "missing"
    assert (
        derive_state(build_paths(project))["requirements"]["REQ-001"][
            "task_planning_evidence_status"
        ]
        == "current"
    )

    (project / "src/later.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert changed["task_planning_evidence_status"] == "stale"
    assert changed["task_planning_code_evidence_state"]["changed_paths"] == [
        "src/later.py"
    ]


def test_non_git_missing_completed_prerequisite_output_is_recorded_and_becomes_stale(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path, git_project=False)
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
        depends_on=["T-009"],
    )
    prepared = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=submission[0],
        tasks_dir=submission[1],
        coverage_file=submission[2],
        effective_change_ids=set(),
        existing_tasks=[
            {
                "task_id": "T-009",
                "status": "done",
                "output_files": ["src/upstream.py"],
            }
        ],
    )

    evidence = prepared["plan"]["code_evidence"]
    missing_entry = next(
        item
        for item in evidence["upstream_outputs"]
        if item["path"] == "src/upstream.py"
    )
    assert missing_entry["state"] == "missing"
    assert assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )["status"] == "current"

    (project / "src/upstream.py").write_text("RESULT = 1\n", encoding="utf-8")
    assessment = assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["src/upstream.py"]


def test_non_git_completed_prerequisite_without_output_paths_tracks_declaration_changes(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path, git_project=False)
    _append_task_event(
        project,
        event_id="EVT-20260720-000002",
        event_type="task_created",
        task_id="T-009",
        payload={
            "title": "已完成前置任务",
            "summary": "已完成前置任务",
            "status": "done",
        },
    )
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
        depends_on=["T-009"],
    )
    prepared = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=submission[0],
        tasks_dir=submission[1],
        coverage_file=submission[2],
        effective_change_ids=set(),
        existing_tasks=[{"task_id": "T-009", "status": "done"}],
    )

    evidence = prepared["plan"]["code_evidence"]
    missing_entry = next(
        item
        for item in evidence["upstream_outputs"]
        if item["path"] == "@task-output/T-009"
    )
    assert missing_entry["state"] == "missing"
    assert assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )["status"] == "current"

    _append_task_event(
        project,
        event_id="EVT-20260720-000003",
        event_type="task_updated",
        task_id="T-009",
        payload={"output_files": ["src/upstream.py"]},
    )
    assessment = assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["@task-output/T-009"]


def test_non_git_missing_delivery_declaration_ignores_current_doing_task_output(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path, git_project=False)
    _append_task_event(
        project,
        event_id="EVT-20260720-000002",
        event_type="task_created",
        task_id="T-009",
        payload={
            "title": "已完成前置任务",
            "summary": "已完成前置任务",
            "status": "done",
        },
    )
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
        depends_on=["T-009"],
    )
    task_file = submission[1] / "main.task.v2.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task["code_scope"]["likely_change_paths"] = ["src/new-task.py"]
    task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    prepared = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=submission[0],
        tasks_dir=submission[1],
        coverage_file=submission[2],
        effective_change_ids=set(),
        existing_tasks=[{"task_id": "T-009", "status": "done"}],
    )
    evidence = prepared["plan"]["code_evidence"]

    (project / "src/new-task.py").write_text("RESULT = 1\n", encoding="utf-8")
    assessment = assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[
            {
                "task_id": "T-010",
                "status": "doing",
                "task_contract": {
                    "code_scope": {
                        "read_paths": ["src/app.py"],
                        "likely_change_paths": ["src/new-task.py"],
                        "protected_paths": [],
                    }
                },
            }
        ],
    )

    assert assessment["status"] == "current"
    assert assessment["changed_paths"] == []
    assert assessment["workspace_state_changed"] is True


@pytest.mark.parametrize("replaced_input", ["coverage", "task"])
def test_validated_task_submission_rejects_input_replacement_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replaced_input: str,
) -> None:
    project, requirement_root = _project(tmp_path)
    submission = _write_task_submission(tmp_path, requirement_root)
    events_before = (project / ".codex-sdlc/events.jsonl").read_bytes()
    original_import = plan_cmd.import_task_plan_bundle

    def replace_before_locked_import(*args, **kwargs):
        if replaced_input == "coverage":
            value = json.loads(submission[2].read_text(encoding="utf-8"))
            value["functional_requirements"] = {}
            submission[2].write_text(
                json.dumps(value, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            task_file = submission[1] / "main.task.v2.json"
            value = json.loads(task_file.read_text(encoding="utf-8"))
            value["acceptance_refs"] = ["AC-999"]
            task_file.write_text(
                json.dumps(value, ensure_ascii=False),
                encoding="utf-8",
            )
        return original_import(*args, **kwargs)

    monkeypatch.setattr(
        plan_cmd,
        "import_task_plan_bundle",
        replace_before_locked_import,
    )
    monkeypatch.chdir(project)

    assert run_tasks_finalize(_task_args(submission)) == 1

    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == events_before
    _assert_no_task_import_residue(project, requirement_root)


def test_default_task_planning_evidence_includes_all_effective_parent_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path, nested=True)
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
    )
    monkeypatch.chdir(project)

    assert run_tasks_finalize(_task_args(submission)) == 0

    saved_plan = json.loads(
        (requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8")
    )
    evidence = saved_plan["code_evidence"]
    assert [item["path"] for item in evidence["rules"]] == ["@repo/AGENTS.md"]

    (project.parent / "AGENTS.md").write_text(
        "所有开发必须运行合同测试和正式档案回归。\n",
        encoding="utf-8",
    )
    assessment = assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["@repo/AGENTS.md"]


def test_explicit_task_planning_selection_cannot_omit_required_inputs(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    submission = _write_task_submission(tmp_path, requirement_root)
    plan = json.loads(submission[0].read_text(encoding="utf-8"))
    selection = plan["code_evidence"]
    selection["rules"] = []
    selection["dependencies"] = []
    selection["code_files"] = []
    submission[0].write_text(
        json.dumps(plan, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="规划代码证据.*缺少"):
        prepare_task_planning_documents(
            build_paths(project),
            requirement_id="REQ-001",
            requirement_root=requirement_root,
            plan_file=submission[0],
            tasks_dir=submission[1],
            coverage_file=submission[2],
            effective_change_ids=set(),
        )


def test_completed_prerequisite_real_outputs_are_bound_and_become_stale(
    tmp_path: Path,
) -> None:
    project, requirement_root = _project(tmp_path)
    upstream_file = project / "src/upstream.py"
    upstream_file.write_text("RESULT = 1\n", encoding="utf-8")
    submission = _write_task_submission(
        tmp_path,
        requirement_root,
        explicit_evidence=False,
        depends_on=["T-009"],
    )
    prepared = prepare_task_planning_documents(
        build_paths(project),
        requirement_id="REQ-001",
        requirement_root=requirement_root,
        plan_file=submission[0],
        tasks_dir=submission[1],
        coverage_file=submission[2],
        effective_change_ids=set(),
        existing_tasks=[
            {
                "task_id": "T-009",
                "status": "done",
                "output_files": ["src/upstream.py"],
            }
        ],
    )

    evidence = prepared["plan"]["code_evidence"]
    assert "src/upstream.py" in [
        item["path"] for item in evidence["upstream_outputs"]
    ]

    upstream_file.write_text("RESULT = 2\n", encoding="utf-8")
    assessment = assess_task_planning_code_evidence(
        build_paths(project),
        evidence,
        tasks=[],
    )
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["src/upstream.py"]
