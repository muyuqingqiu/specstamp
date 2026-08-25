from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
for import_path in (SRC_ROOT, TESTS_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from codex_sdlc.core.backup import write_sdlc_identity
from codex_sdlc.core.change_workspace import (
    BASE_VERSION_PATHS,
    CHANGE_INTERRUPT_ENV,
    INTERRUPT_AFTER_DIRECTORY_PUBLISH,
    INTERRUPT_AFTER_EVENT_APPEND,
    INTERRUPT_BEFORE_DIRECTORY_PUBLISH,
    build_status_document,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, ensure_base_dirs
from codex_sdlc.core.state import change_ids, derive_state, load_events
from codex_sdlc.core.structured_contract import sha256_file, validate_schema_document
from codex_sdlc.services.change_service import create_change_workspace
from test_cli_v1 import (
    run_cli_raw,
)
from test_contract_cli_regressions import _ready_project
from test_task_plan_v2_contract import _task, _write_submission


def _event(
    project: Path,
    *,
    event_id: str,
    event_type: str,
    requirement_id: str | None,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "project_path": str(project),
        "requirement_id": requirement_id,
        "task_id": None,
        "created_at": "2026-07-22T13:40:00+08:00",
        "source": "t030-contract-fixture",
        "summary": f"测试事件 {event_type}",
        "payload": payload,
    }


def _write_events(project: Path, events: list[dict[str, object]]) -> None:
    (project / ".codex-sdlc/events.jsonl").write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def _base_hashes(requirement_dir: Path) -> dict[str, str]:
    return {
        suffix: sha256_file(requirement_dir / suffix)
        for suffix in BASE_VERSION_PATHS.values()
    }


def _protected_requirement_snapshot(requirement_dir: Path) -> dict[str, bytes]:
    """变更工作区以外的正式文件都属于当前任务保护范围。"""

    return {
        path.relative_to(requirement_dir).as_posix(): path.read_bytes()
        for path in sorted(requirement_dir.rglob("*"))
        if path.is_file() and "changes" not in path.relative_to(requirement_dir).parts
    }


def _minimal_formal_project(
    tmp_path: Path,
    *,
    include_legacy_change: bool = False,
) -> tuple[Path, Path]:
    project = tmp_path / "结构化变更项目"
    project.mkdir()
    paths = build_paths(project)
    ensure_base_dirs(paths)
    requirement_dir = paths.requirements_dir / "REQ-001-订单审批"
    for index, suffix in enumerate(BASE_VERSION_PATHS.values(), start=1):
        target = requirement_dir / suffix
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"fixture": index}, ensure_ascii=False, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
    events = [
        _event(
            project,
            event_id="EVT-20260722-000001",
            event_type="project_initialized",
            requirement_id=None,
            payload={"project_name": project.name},
        ),
        _event(
            project,
            event_id="EVT-20260722-000002",
            event_type="requirement_created",
            requirement_id="REQ-001",
            payload={
                "title": "订单审批",
                "description": "订单审批",
                "folder_name": requirement_dir.name,
            },
        ),
    ]
    if include_legacy_change:
        events.append(
            _event(
                project,
                event_id="EVT-20260722-000003",
                event_type="change_recorded",
                requirement_id="REQ-001",
                payload={
                    "change_id": "CHG-009",
                    "summary": "历史变更",
                    "description": "历史变更",
                    "file_path": (
                        f".codex-sdlc/requirements/{requirement_dir.name}/"
                        "changes/CHG-009.md"
                    ),
                },
            )
        )
    _write_events(project, events)
    write_sdlc_identity(paths)
    return project, requirement_dir


def _creation_events(project: Path) -> list[dict[str, object]]:
    return [
        event
        for event in load_events(build_paths(project))
        if event.get("event_type") == "change_workspace_created"
    ]


def test_change_workspace_schema_rejects_extra_field_and_wrong_base_keys() -> None:
    bases = {
        name: {"path": f".codex-sdlc/requirements/REQ-001/{suffix}", "sha256": "a" * 64}
        for name, suffix in BASE_VERSION_PATHS.items()
    }
    status = build_status_document(
        requirement_id="REQ-001",
        change_id="CHG-001",
        request_key="t030-schema",
        workspace_path=".codex-sdlc/requirements/REQ-001/changes/CHG-001",
        base_versions=bases,
        created_event_id="EVT-20260722-000001",
    )
    validate_schema_document(status, schema_name="change-workspace.v1")

    extra = {**status, "created_at": "2026-07-22"}
    with pytest.raises(SdlcError, match="未知字段"):
        validate_schema_document(extra, schema_name="change-workspace.v1")
    missing_base = deepcopy(status)
    del missing_base["base_versions"]["task_plan"]
    with pytest.raises(SdlcError, match="缺少必填字段"):
        validate_schema_document(missing_base, schema_name="change-workspace.v1")


def test_create_builds_empty_workspace_and_keeps_all_base_files_unchanged(
    tmp_path: Path,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    before = _base_hashes(requirement_dir)

    result = create_change_workspace(
        paths,
        requirement_id="REQ-001",
        request_key="t030-first-create",
    )

    assert result.change_id == "CHG-001"
    assert result.duplicate is False
    workspace = project / result.workspace_path
    assert {item.name for item in workspace.iterdir()} == {
        "status.json",
        "原始资料",
        "reviews",
    }
    assert list((workspace / "原始资料").iterdir()) == []
    assert list((workspace / "reviews").iterdir()) == []
    status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
    assert set(status) == {
        "schema_version",
        "requirement_id",
        "change_id",
        "request_key",
        "workspace_path",
        "status",
        "base_versions",
        "created_event_id",
    }
    assert status["schema_version"] == "change-workspace.v1"
    assert status["status"] == "draft"
    assert status["created_event_id"] == result.created_event_id
    for name, suffix in BASE_VERSION_PATHS.items():
        assert status["base_versions"][name] == {
            "path": f".codex-sdlc/requirements/{requirement_dir.name}/{suffix}",
            "sha256": before[suffix],
        }
    assert _base_hashes(requirement_dir) == before
    events = _creation_events(project)
    assert len(events) == 1
    assert events[0]["payload"] == {
        "requirement_id": "REQ-001",
        "request_key": "t030-first-create",
        "change_id": "CHG-001",
        "workspace_path": result.workspace_path,
        "status_sha256": sha256_file(workspace / "status.json"),
    }
    assert list(paths.change_transactions_dir.glob("*.json")) == []


def test_same_request_is_idempotent_and_detects_status_or_base_drift(
    tmp_path: Path,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    first = create_change_workspace(
        paths, requirement_id="REQ-001", request_key="t030-idempotent"
    )
    original_events = paths.events_file.read_bytes()

    duplicate = create_change_workspace(
        paths, requirement_id="REQ-001", request_key="t030-idempotent"
    )
    assert duplicate.change_id == first.change_id
    assert duplicate.created_event_id == first.created_event_id
    assert duplicate.duplicate is True
    assert paths.events_file.read_bytes() == original_events

    status_path = project / first.workspace_path / "status.json"
    status_path.write_bytes(status_path.read_bytes() + b" ")
    with pytest.raises(SdlcError, match="整体哈希"):
        create_change_workspace(
            paths, requirement_id="REQ-001", request_key="t030-idempotent"
        )
    status_path.write_bytes(status_path.read_bytes()[:-1])
    (requirement_dir / "effective/requirement.current.json").write_text(
        '{"fixture":"drift"}\n', encoding="utf-8"
    )
    with pytest.raises(SdlcError, match="基础版本"):
        create_change_workspace(
            paths, requirement_id="REQ-001", request_key="t030-idempotent"
        )


def test_allocator_consumes_legacy_events_and_new_event_enters_old_state_view(
    tmp_path: Path,
) -> None:
    project, _requirement_dir = _minimal_formal_project(
        tmp_path, include_legacy_change=True
    )
    paths = build_paths(project)
    result = create_change_workspace(
        paths, requirement_id="REQ-001", request_key="t030-after-legacy"
    )
    assert result.change_id == "CHG-010"
    assert change_ids(derive_state(paths)) == ["CHG-009", "CHG-010"]


@pytest.mark.parametrize(
    "stage",
    [
        INTERRUPT_BEFORE_DIRECTORY_PUBLISH,
        INTERRUPT_AFTER_DIRECTORY_PUBLISH,
        INTERRUPT_AFTER_EVENT_APPEND,
    ],
)
def test_interruption_retry_recovers_to_one_workspace_and_one_event(
    tmp_path: Path,
    stage: str,
) -> None:
    project, _requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)

    def interrupt(current_stage: str) -> None:
        if current_stage == stage:
            raise SdlcError(f"测试中断：{stage}")

    with pytest.raises(SdlcError, match="测试中断"):
        create_change_workspace(
            paths,
            requirement_id="REQ-001",
            request_key=f"t030-{stage}",
            interruption_hook=interrupt,
        )

    recovered = create_change_workspace(
        paths,
        requirement_id="REQ-001",
        request_key=f"t030-{stage}",
    )
    workspaces = list(
        (project / ".codex-sdlc/requirements/REQ-001-订单审批/changes").glob(
            "CHG-*"
        )
    )
    assert [item.name for item in workspaces] == ["CHG-001"]
    assert recovered.change_id == "CHG-001"
    assert len(_creation_events(project)) == 1
    assert list(paths.change_transactions_dir.glob("*.json")) == []
    assert list(paths.change_staging_root.iterdir()) == []


def test_unregistered_workspace_and_event_without_directory_are_rejected(
    tmp_path: Path,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    orphan = requirement_dir / "changes/CHG-001"
    (orphan / "原始资料").mkdir(parents=True)
    (orphan / "reviews").mkdir()
    (orphan / "status.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="没有唯一合法创建登记"):
        create_change_workspace(
            paths, requirement_id="REQ-001", request_key="t030-orphan-directory"
        )

    # 合法创建后只移走目录，事件仍在；下一次请求必须停止，不能静默重建证据。
    for child in sorted(orphan.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    orphan.rmdir()
    result = create_change_workspace(
        paths, requirement_id="REQ-001", request_key="t030-event-orphan"
    )
    workspace = project / result.workspace_path
    renamed = workspace.with_name("暂存证据")
    workspace.rename(renamed)
    with pytest.raises(SdlcError, match="缺少对应 CHG 工作区"):
        create_change_workspace(
            paths, requirement_id="REQ-001", request_key="t030-another-request"
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_requirement", "没有找到正式需求"),
        ("duplicate_requirement", "对应多个目录"),
        ("missing_base", "基础版本缺失"),
        ("base_symlink", "符号链接"),
    ],
)
def test_expected_requirement_and_base_rejections(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    requirement_id = "REQ-001"
    if mutation == "missing_requirement":
        requirement_id = "REQ-999"
    elif mutation == "duplicate_requirement":
        duplicate = paths.requirements_dir / "REQ-001-重复目录"
        duplicate.mkdir()
    elif mutation == "missing_base":
        (requirement_dir / "tasks/task-plan.v2.json").unlink()
    elif mutation == "base_symlink":
        target = requirement_dir / "effective/design.current.json"
        outside = project / "外部设计.json"
        outside.write_text("{}\n", encoding="utf-8")
        target.unlink()
        target.symlink_to(outside)

    with pytest.raises(SdlcError, match=message):
        create_change_workspace(
            paths,
            requirement_id=requirement_id,
            request_key=f"t030-reject-{mutation}",
        )
    assert _creation_events(project) == []
    changes = requirement_dir / "changes"
    assert not changes.exists() or list(changes.glob("CHG-*")) == []


@pytest.mark.parametrize(
    "request_key",
    ["", "UPPER", " leading", "has space", "a" * 129],
)
def test_invalid_request_key_is_rejected_without_writes(
    tmp_path: Path,
    request_key: str,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    before = paths.events_file.read_bytes()
    with pytest.raises(SdlcError, match="request-key 格式不正确"):
        create_change_workspace(
            paths, requirement_id="REQ-001", request_key=request_key
        )
    assert paths.events_file.read_bytes() == before
    assert not (requirement_dir / "changes").exists()


def test_cli_rejects_natural_language_argument_and_project_identity_mismatch(
    tmp_path: Path,
) -> None:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    extra_argument = run_cli_raw(
        [
            "change-create",
            "REQ-001",
            "--request-key",
            "t030-cli-structure",
            "这里是自然语言变更正文",
        ],
        cwd=project,
    )
    assert extra_argument.returncode == 2
    assert "无法识别这些参数" in extra_argument.stderr
    assert not (requirement_dir / "changes").exists()

    identity = json.loads(paths.identity_file.read_text(encoding="utf-8"))
    identity["repo_key"] = "repo_被改动的身份"
    paths.identity_file.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths.change_transactions_dir.rmdir()
    with pytest.raises(SdlcError, match="身份和当前 Git 状态不一致"):
        create_change_workspace(
            paths,
            requirement_id="REQ-001",
            request_key="t030-identity-mismatch",
        )
    assert _creation_events(project) == []
    assert not paths.change_transactions_dir.exists()


def _run_change_create(project: Path, request_key: str, *, stage: str = ""):
    env = {
        "CODEX_SDLC_DISABLE_AUTO_BACKUP": "1",
        CHANGE_INTERRUPT_ENV: stage,
    }
    return run_cli_raw(
        ["change-create", "REQ-001", "--request-key", request_key],
        cwd=project,
        extra_env=env,
    )


def test_v2_change_create_real_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓库外 Git 项目通过正式 start 与 change-create 入口完成V2。"""

    ready_root = tmp_path / "文档优先准备"
    ready_root.mkdir()
    project, paths, formal_package = _ready_project(ready_root, monkeypatch)
    package_file = tmp_path / "文档优先正式包.json"
    package_file.write_text(
        json.dumps(formal_package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    started = run_cli_raw(["start", "--file", str(package_file)], cwd=project)
    assert started.returncode == 0, started.stdout + started.stderr
    requirement_dir = paths.requirements_dir / "REQ-001"

    change_task = _task("change-base", "准备正式变更基础任务")
    change_task["design_refs"] = [
        "API-001",
        "COMP-001",
        "DATA-001",
        "PAGE-001",
        "SAFE-001",
    ]
    task_submission = _write_submission(
        tmp_path / "任务计划模型输出",
        tasks=[change_task],
    )
    coverage = json.loads(task_submission[2].read_text(encoding="utf-8"))
    coverage["design_artifacts"] = {
        design_id: {"tasks": ["@client:change-base"]}
        for design_id in change_task["design_refs"]
    }
    task_submission[2].write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tasks_imported = run_cli_raw(
        [
            "tasks",
            "REQ-001",
            "--plan-file",
            str(task_submission[0]),
            "--tasks-dir",
            str(task_submission[1]),
            "--coverage-file",
            str(task_submission[2]),
        ],
        cwd=project,
    )
    assert tasks_imported.returncode == 0, tasks_imported.stdout + tasks_imported.stderr
    assert (requirement_dir / "tasks/task-plan.v2.json").is_file()
    write_sdlc_identity(paths)
    before = _base_hashes(requirement_dir)
    protected_before = _protected_requirement_snapshot(requirement_dir)

    first = _run_change_create(project, "t030-v2-create-001")
    assert first.returncode == 0, first.stdout + first.stderr
    duplicate = _run_change_create(project, "t030-v2-create-001")
    assert duplicate.returncode == 0, duplicate.stdout + duplicate.stderr
    assert "变更：CHG-001" in first.stdout
    assert "变更：CHG-001" in duplicate.stdout

    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    first_parallel = subprocess.Popen(
        [
            str(REPO_ROOT / "bin/codex-sdlc"),
            "change-create",
            "REQ-001",
            "--request-key",
            "t030-v2-parallel-a",
        ],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    second_parallel = subprocess.Popen(
        [
            str(REPO_ROOT / "bin/codex-sdlc"),
            "change-create",
            "REQ-001",
            "--request-key",
            "t030-v2-parallel-b",
        ],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_stdout, first_stderr = first_parallel.communicate(timeout=30)
    second_stdout, second_stderr = second_parallel.communicate(timeout=30)
    assert first_parallel.returncode == 0, first_stdout + first_stderr
    assert second_parallel.returncode == 0, second_stdout + second_stderr
    parallel_changes = {
        line.split("：", 1)[1]
        for output in (first_stdout, second_stdout)
        for line in output.splitlines()
        if line.startswith("变更：")
    }
    assert parallel_changes == {"CHG-002", "CHG-003"}

    for index, stage in enumerate(
        (
            INTERRUPT_BEFORE_DIRECTORY_PUBLISH,
            INTERRUPT_AFTER_DIRECTORY_PUBLISH,
            INTERRUPT_AFTER_EVENT_APPEND,
        ),
        start=1,
    ):
        request_key = f"t030-v2-interrupt-{index}"
        interrupted = _run_change_create(project, request_key, stage=stage)
        assert interrupted.returncode != 0
        recovered = _run_change_create(project, request_key)
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr

    workspaces = sorted((requirement_dir / "changes").glob("CHG-*"))
    assert [workspace.name for workspace in workspaces] == [
        "CHG-001",
        "CHG-002",
        "CHG-003",
        "CHG-004",
        "CHG-005",
        "CHG-006",
    ]
    assert len(_creation_events(project)) == 6
    assert _base_hashes(requirement_dir) == before
    assert _protected_requirement_snapshot(requirement_dir) == protected_before
    assert list(paths.change_transactions_dir.glob("*.json")) == []
    for workspace in workspaces:
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        validate_schema_document(status, schema_name="change-workspace.v1")
        assert {item.name for item in workspace.iterdir()} == {
            "status.json",
            "原始资料",
            "reviews",
        }
