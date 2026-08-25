from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import task_dependencies_ready
from codex_sdlc.core.structured_contract import canonical_json_text, sha256_bytes, sha256_file
from codex_sdlc.core.task_contract import (
    INTERRUPT_AFTER_DIRECTORY_COMMIT,
    import_task_plan_bundle,
    load_task_plan_record,
    task_markdown,
    task_plan_markdown,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _whole_file_reference(relative_path: str, digest: str) -> dict[str, object]:
    return {
        "schema_version": "reference-locator.v1",
        "path": relative_path,
        "sha256": digest,
        "locator": {"kind": "whole_file"},
    }


def _create_requirement(project: Path, requirement_id: str, folder_name: str) -> Path:
    requirement_root = project / ".codex-sdlc" / "requirements" / folder_name
    original = requirement_root / "original" / "正式依据.md"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("需求、设计和验收均已正式确认。\n", encoding="utf-8")
    digest = sha256_file(original)
    entries = {
        "FR-001": _whole_file_reference("original/正式依据.md", digest),
        "GR-001": _whole_file_reference("original/正式依据.md", digest),
        "AC-001": _whole_file_reference("original/正式依据.md", digest),
        "DES-001#architecture": _whole_file_reference("original/正式依据.md", digest),
        "DATA-001": _whole_file_reference("original/正式依据.md", digest),
        "MAT-001": _whole_file_reference("original/正式依据.md", digest),
    }
    _write_json(
        requirement_root / "reference-index.v1.json",
        {
            "schema_version": "reference-index.v1",
            "requirement_id": requirement_id,
            "entries": entries,
        },
    )
    _write_json(
        requirement_root / "original" / "formal.v3.json",
        {
            "formal_contract_version": "formal.v3",
            "workflow_profile": "document-first.v1",
        },
    )
    return requirement_root


def _create_project(tmp_path: Path, requirements: int = 1) -> tuple[Path, list[tuple[str, str, Path]]]:
    project = tmp_path / "任务导入项目"
    sdlc_dir = project / ".codex-sdlc"
    sdlc_dir.mkdir(parents=True)
    records: list[tuple[str, str, Path]] = []
    events: list[dict[str, object]] = []
    for index in range(1, requirements + 1):
        requirement_id = f"REQ-{index:03d}"
        folder_name = f"{requirement_id}-任务合同"
        root = _create_requirement(project, requirement_id, folder_name)
        records.append((requirement_id, folder_name, root))
        events.append(
            {
                "event_id": f"EVT-20260720-{index:06d}",
                "event_type": "requirement_created",
                "project_path": str(project),
                "requirement_id": requirement_id,
                "task_id": None,
                "created_at": "2026-07-20T10:00:00+08:00",
                "source": "合同测试",
                "summary": f"创建正式需求 {requirement_id}",
                "payload": {
                    "title": f"任务合同 {index}",
                    "description": "验证任务计划原子导入。",
                    "summary": "验证任务计划原子导入。",
                    "folder_name": folder_name,
                    "flow_type": "SDLC 原生正式流程",
                    "native_start": {"formal_contract_version": "formal.v3"},
                },
            }
        )
    (sdlc_dir / "events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    return project, records


def _task(client_key: str, title: str, *, depends_on: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": "task.v2",
        "requirement_id": "REQ-001",
        "client_key": client_key,
        "title": title,
        "goal": f"完整实现并验证：{title}",
        "deliverables": [f"交付 {title} 的可运行实现。"],
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
            "read_paths": ["src"],
            "likely_change_paths": [f"src/{client_key}.py"],
            "protected_paths": [".codex-sdlc/requirements"],
        },
        "implementation_requirements": ["保持任务合同字段原样保存。"],
        "data_api_page_component_requirements": ["不涉及页面；只处理任务结构化合同。"],
        "states_and_exceptions": ["任一校验失败时整包不写入。"],
        "security_and_privacy": ["任务产物不得包含账号、密码或令牌。"],
        "automated_tests": ["运行任务合同定向测试。"],
        "manual_checks": ["核对 JSON 与 Markdown 内容逐项一致。"],
        "out_of_scope": ["不启动任务运行轮次。"],
        "blocking_conditions": [],
        "definition_of_done": ["任务 JSON、Markdown、事件和回执可以相互核对。"],
    }


def _write_submission(
    source_root: Path,
    *,
    requirement_id: str = "REQ-001",
    tasks: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path]:
    selected = deepcopy(tasks or [_task("storage", "实现任务存储"), _task("api", "实现任务接口", depends_on=["@client:storage"])])
    for task in selected:
        task["requirement_id"] = requirement_id
    plan_tasks = [f"@client:{task['client_key']}" for task in selected]
    dependencies = [
        {"from": dependency, "to": f"@client:{task['client_key']}"}
        for task in selected
        for dependency in task["depends_on"]
    ]
    plan_file = source_root / "task-plan.v2.json"
    tasks_dir = source_root / "任务文件"
    coverage_file = source_root / "task-coverage.v1.json"
    _write_json(
        plan_file,
        {
            "schema_version": "task-plan.v2",
            "requirement_id": requirement_id,
            "tasks": plan_tasks,
            "dependencies": dependencies,
        },
    )
    tasks_dir.mkdir(parents=True)
    for task in selected:
        _write_json(tasks_dir / f"{task['client_key']}.task.v2.json", task)
    _write_json(
        coverage_file,
        {
            "schema_version": "task-coverage.v1",
            "requirement_id": requirement_id,
            "functional_requirements": {
                "FR-001": {"tasks": plan_tasks, "status": "implemented"}
            },
            "design_artifacts": {"DATA-001": {"tasks": plan_tasks}},
            "acceptance_criteria": {
                "AC-001": {
                    "tasks": plan_tasks,
                    "test_refs": [f"{plan_tasks[0]}#automated_tests/0"],
                }
            },
            "effective_changes": {},
            "no_development_items": [],
        },
    )
    return plan_file, tasks_dir, coverage_file


def _import(project: Path, requirement_id: str, submission: tuple[Path, Path, Path]):
    return import_task_plan_bundle(
        build_paths(project),
        requirement_id=requirement_id,
        plan_file=submission[0],
        tasks_dir=submission[1],
        coverage_file=submission[2],
    )


def test_task_plan_assigns_stable_ids_and_rewrites_all_task_references(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "模型输出")

    result = _import(project, "REQ-001", submission)

    assert result.mapping == {"storage": "T-001", "api": "T-002"}
    assert result.duplicate is False
    plan = json.loads((requirement_root / "tasks" / "task-plan.v2.json").read_text(encoding="utf-8"))
    assert plan["tasks"] == ["T-001", "T-002"]
    assert plan["dependencies"] == [{"from": "T-001", "to": "T-002"}]
    assert plan["input_hashes"]["reference_index"] == sha256_file(
        requirement_root / "reference-index.v1.json"
    )
    second = json.loads((requirement_root / "tasks" / "T-002.json").read_text(encoding="utf-8"))
    assert second["task_id"] == "T-002"
    assert "client_key" not in second
    assert second["depends_on"] == ["T-001"]
    coverage = json.loads((requirement_root / "task-coverage.v1.json").read_text(encoding="utf-8"))
    assert coverage["functional_requirements"]["FR-001"]["tasks"] == ["T-001", "T-002"]
    assert "@client:" not in canonical_json_text(coverage)
    assert load_task_plan_record(build_paths(project), "REQ-001")["tasks"] == [
        "T-001",
        "T-002",
    ]


def test_empty_tasks_directory_created_by_start_can_be_replaced(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    (requirement_root / "tasks").mkdir()
    submission = _write_submission(tmp_path / "带空目录的模型输出")

    result = _import(project, "REQ-001", submission)

    assert result.mapping == {"storage": "T-001", "api": "T-002"}
    assert (requirement_root / "tasks" / "task-plan.v2.json").is_file()
    assert (requirement_root / "tasks" / "T-001.json").is_file()


def test_interruption_after_empty_placeholder_removal_recovers_to_full_failure(
    tmp_path: Path,
) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    (requirement_root / "tasks").mkdir()
    submission = _write_submission(tmp_path / "空目录中断输出")
    script = f"""
import os
from pathlib import Path
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_contract import import_task_plan_bundle, INTERRUPT_AFTER_PLACEHOLDER_REMOVAL
def stop(stage):
    if stage == INTERRUPT_AFTER_PLACEHOLDER_REMOVAL:
        os._exit(74)
import_task_plan_bundle(
    build_paths(Path({str(project)!r})),
    requirement_id='REQ-001',
    plan_file=Path({str(submission[0])!r}),
    tasks_dir=Path({str(submission[1])!r}),
    coverage_file=Path({str(submission[2])!r}),
    interruption_hook=stop,
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    interrupted = subprocess.run([sys.executable, "-c", script], env=env, check=False)
    assert interrupted.returncode == 74
    assert not (requirement_root / "tasks").exists()

    recovered = _import(project, "REQ-001", submission)

    assert recovered.duplicate is False
    assert (requirement_root / "tasks" / "T-001.json").is_file()
    events = [
        json.loads(line)
        for line in (project / ".codex-sdlc" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([event for event in events if event["event_type"] == "task_plan_imported"]) == 1


def test_tasks_command_refreshes_readable_state_without_rewriting_formal_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "命令输出")
    monkeypatch.chdir(project)

    exit_code = run_tasks_finalize(
        type(
            "参数",
            (),
            {
                "requirement_id": "REQ-001",
                "plan_file": str(submission[0]),
                "tasks_dir": str(submission[1]),
                "coverage_file": str(submission[2]),
            },
        )()
    )

    assert exit_code == 0
    assert "storage -> T-001" in capsys.readouterr().out
    first_markdown = (requirement_root / "tasks" / "T-001.md").read_bytes()
    assert (project / ".codex-sdlc" / "sdlc.db").is_file()
    # 再次刷新只能核对正式任务，不能让旧任务渲染器覆盖 task.v2 Markdown。
    run_tasks_finalize(
        type(
            "参数",
            (),
            {
                "requirement_id": "REQ-001",
                "plan_file": str(submission[0]),
                "tasks_dir": str(submission[1]),
                "coverage_file": str(submission[2]),
            },
        )()
    )
    assert (requirement_root / "tasks" / "T-001.md").read_bytes() == first_markdown


def test_same_package_is_idempotent_and_conflicting_package_is_rejected(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "模型输出")
    first = _import(project, "REQ-001", submission)
    event_bytes = (project / ".codex-sdlc" / "events.jsonl").read_bytes()
    file_bytes = {
        path.relative_to(requirement_root).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    }

    duplicate = _import(project, "REQ-001", submission)

    assert duplicate.duplicate is True
    assert duplicate.mapping == first.mapping
    assert (project / ".codex-sdlc" / "events.jsonl").read_bytes() == event_bytes
    assert {
        path.relative_to(requirement_root).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    } == file_bytes

    changed_tasks = [_task("storage", "被修改的任务"), _task("api", "实现任务接口", depends_on=["@client:storage"])]
    conflict = _write_submission(tmp_path / "冲突输出", tasks=changed_tasks)
    with pytest.raises(SdlcError, match="不同内容|不能覆盖"):
        _import(project, "REQ-001", conflict)
    assert (project / ".codex-sdlc" / "events.jsonl").read_bytes() == event_bytes


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda plan, tasks, coverage: tasks[0].pop("manual_checks"), "缺少必填字段"),
        (lambda plan, tasks, coverage: tasks[1].update(depends_on=["@client:missing"]), "悬空|计划不一致"),
        (lambda plan, tasks, coverage: plan["dependencies"].append({"from": "@client:api", "to": "@client:storage"}), "依赖|成环|不一致"),
        (lambda plan, tasks, coverage: tasks[0]["requirement_refs"].append("FR-999"), "正式引用"),
        (lambda plan, tasks, coverage: tasks[0].update(task_id="T-999"), "未知字段"),
    ],
)
def test_invalid_bundle_is_rejected_without_task_event_or_files(
    tmp_path: Path, mutate, message: str
) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "非法输出")
    plan = json.loads(submission[0].read_text(encoding="utf-8"))
    task_values = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(submission[1].glob("*.json"))
    ]
    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    mutate(plan, task_values, coverage)
    _write_json(submission[0], plan)
    for path, task in zip(sorted(submission[1].glob("*.json")), task_values, strict=True):
        _write_json(path, task)
    _write_json(submission[2], coverage)
    before = (project / ".codex-sdlc" / "events.jsonl").read_bytes()

    with pytest.raises(SdlcError, match=message):
        _import(project, "REQ-001", submission)

    assert not (requirement_root / "tasks").exists()
    assert not (requirement_root / "task-coverage.v1.json").exists()
    assert (project / ".codex-sdlc" / "events.jsonl").read_bytes() == before


def test_reference_hash_drift_and_completed_legacy_task_are_rejected(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "哈希漂移")
    plan = json.loads(submission[0].read_text(encoding="utf-8"))
    plan["input_hashes"] = {"reference_index": "0" * 64}
    _write_json(submission[0], plan)
    with pytest.raises(SdlcError, match="引用索引.*哈希"):
        _import(project, "REQ-001", submission)
    assert not (requirement_root / "tasks").exists()

    events_file = project / ".codex-sdlc" / "events.jsonl"
    events = events_file.read_text(encoding="utf-8")
    legacy = {
        "event_id": "EVT-20260720-000099",
        "event_type": "task_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": "T-009",
        "created_at": "2026-07-20T10:01:00+08:00",
        "source": "合同测试",
        "summary": "已完成旧任务",
        "payload": {
            "title": "已完成旧任务",
            "summary": "已完成旧任务",
            "status": "done",
            "manual_checks": ["已验收"],
            "test_items": ["已测试"],
        },
    }
    events_file.write_text(events + json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
    clean_submission = _write_submission(tmp_path / "旧任务保护")
    with pytest.raises(SdlcError, match="已有任务|已完成"):
        _import(project, "REQ-001", clean_submission)
    assert not (requirement_root / "tasks").exists()


def test_task_event_and_formal_file_cannot_drift_from_each_other(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "任务正文漂移")
    _import(project, "REQ-001", submission)
    events_file = project / ".codex-sdlc" / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    original_events = deepcopy(events)
    import_event = next(event for event in events if event["event_type"] == "task_plan_imported")
    import_event["payload"]["tasks"][0]["goal"] = "被改写的任务目标"
    events_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="正文与正式文件哈希"):
        load_task_plan_record(build_paths(project), "REQ-001")

    events_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in original_events),
        encoding="utf-8",
    )
    task_file = requirement_root / "tasks" / "T-001.json"
    task = json.loads(task_file.read_text(encoding="utf-8"))
    task["goal"] = "磁盘上的任务目标也不能单独改写"
    _write_json(task_file, task)

    with pytest.raises(SdlcError, match="正式文件哈希漂移"):
        load_task_plan_record(build_paths(project), "REQ-001")


def test_formal_package_digest_rejects_coordinated_content_and_receipt_tampering(
    tmp_path: Path,
) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "协调篡改")
    first = _import(project, "REQ-001", submission)
    task_directory = requirement_root / "tasks"
    receipt_path = task_directory / ".task-import-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    original_package_sha256 = receipt["package_sha256"]
    task_records = [
        json.loads((task_directory / f"T-{index:03d}.json").read_text(encoding="utf-8"))
        for index in (1, 2)
    ]
    task_records[0]["goal"] = "协调改写后的任务目标"
    plan = json.loads((task_directory / "task-plan.v2.json").read_text(encoding="utf-8"))
    changed_files = {
        "T-001.json": canonical_json_text(task_records[0]).encode("utf-8"),
        "T-001.md": task_markdown(task_records[0]).encode("utf-8"),
        "任务总览.md": task_plan_markdown(plan, task_records).encode("utf-8"),
    }
    for name, content in changed_files.items():
        (task_directory / name).write_bytes(content)
        receipt["files"][name] = sha256_bytes(content)
    receipt_path.write_text(canonical_json_text(receipt), encoding="utf-8")

    events_file = project / ".codex-sdlc" / "events.jsonl"
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    event = next(item for item in events if item["event_type"] == "task_plan_imported")
    event["payload"]["tasks"] = task_records
    event["payload"]["files"] = deepcopy(receipt["files"])
    assert event["payload"]["package_sha256"] == original_package_sha256
    assert first.package_sha256 == original_package_sha256
    events_file.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="整包摘要|package_sha256"):
        load_task_plan_record(build_paths(project), "REQ-001")
    with pytest.raises(SdlcError, match="整包摘要|package_sha256"):
        _import(project, "REQ-001", submission)


def test_new_task_can_depend_on_existing_formal_task(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path, requirements=2)
    events_file = project / ".codex-sdlc" / "events.jsonl"
    existing_event = {
        "event_id": "EVT-20260720-000099",
        "event_type": "task_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": "T-009",
        "created_at": "2026-07-20T10:01:00+08:00",
        "source": "合同测试",
        "summary": "创建已有正式任务",
        "payload": {
            "title": "已有正式任务",
            "summary": "已有正式任务",
            "status": "done",
            "manual_checks": ["已验收"],
            "test_items": ["已测试"],
        },
    }
    events_file.write_text(
        events_file.read_text(encoding="utf-8")
        + json.dumps(existing_event, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    new_task = _task("new-task", "依赖已有正式任务", depends_on=["T-009"])
    submission = _write_submission(
        tmp_path / "正式依赖",
        requirement_id="REQ-002",
        tasks=[new_task],
    )
    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    coverage["functional_requirements"]["FR-001"]["tasks"].insert(0, "T-009")
    coverage["acceptance_criteria"]["AC-001"]["tasks"].insert(0, "T-009")
    coverage["acceptance_criteria"]["AC-001"]["test_refs"].insert(
        0,
        "T-009#automated_tests/0",
    )
    _write_json(submission[2], coverage)

    result = _import(project, "REQ-002", submission)

    assert result.mapping == {"new-task": "T-010"}
    requirement_root = records[1][2]
    plan = json.loads((requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8"))
    task = json.loads((requirement_root / "tasks/T-010.json").read_text(encoding="utf-8"))
    saved_coverage = json.loads(
        (requirement_root / "task-coverage.v1.json").read_text(encoding="utf-8")
    )
    assert plan["dependencies"] == [{"from": "T-009", "to": "T-010"}]
    assert task["depends_on"] == ["T-009"]
    assert saved_coverage["functional_requirements"]["FR-001"]["tasks"] == [
        "T-009",
        "T-010",
    ]
    assert saved_coverage["acceptance_criteria"]["AC-001"]["test_refs"] == [
        "T-009#automated_tests/0",
        "T-010#automated_tests/0",
    ]
    assert load_task_plan_record(build_paths(project), "REQ-002")["tasks"] == ["T-010"]
    duplicate = _import(project, "REQ-002", submission)
    assert duplicate.duplicate is True
    assert duplicate.mapping == {"new-task": "T-010"}


def test_tasks_command_refreshes_cross_requirement_dependency_on_first_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, records = _create_project(tmp_path, requirements=2)
    events_file = project / ".codex-sdlc/events.jsonl"
    existing_event = {
        "event_id": "EVT-20260720-000099",
        "event_type": "task_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": "T-009",
        "created_at": "2026-07-20T10:01:00+08:00",
        "source": "合同测试",
        "summary": "创建已完成正式任务",
        "payload": {
            "title": "已完成正式任务",
            "summary": "已完成正式任务",
            "status": "done",
            "manual_checks": ["已验收"],
            "test_items": ["已测试"],
        },
    }
    events_file.write_text(
        events_file.read_text(encoding="utf-8")
        + json.dumps(existing_event, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    submission = _write_submission(
        tmp_path / "正常命令正式依赖",
        requirement_id="REQ-002",
        tasks=[_task("new-task", "正常命令跨需求依赖", depends_on=["T-009"])],
    )
    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    coverage["functional_requirements"]["FR-001"]["tasks"].insert(0, "T-009")
    coverage["acceptance_criteria"]["AC-001"]["tasks"].insert(0, "T-009")
    coverage["acceptance_criteria"]["AC-001"]["test_refs"].insert(
        0,
        "T-009#automated_tests/0",
    )
    _write_json(submission[2], coverage)
    args = type(
        "参数",
        (),
        {
            "requirement_id": "REQ-002",
            "plan_file": str(submission[0]),
            "tasks_dir": str(submission[1]),
            "coverage_file": str(submission[2]),
        },
    )()
    monkeypatch.chdir(project)

    assert run_tasks_finalize(args) == 0
    assert run_tasks_finalize(args) == 0

    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    assert len(
        [
            event
            for event in events
            if event["event_type"] == "task_plan_imported"
            and event["requirement_id"] == "REQ-002"
        ]
    ) == 1
    assert (records[1][2] / "tasks/T-010.json").is_file()
    assert (records[1][2] / "task-coverage.v1.json").is_file()


def test_project_dependency_readiness_handles_done_unfinished_and_unknown() -> None:
    dependency = {"task_id": "T-009", "status": "done"}
    task = {"task_id": "T-010", "depends_on": ["T-009"]}
    state = {
        "requirements": {
            "REQ-001": {"tasks": [dependency]},
            "REQ-002": {"tasks": [task]},
        }
    }

    assert task_dependencies_ready(state, task) is True
    dependency["status"] = "closed"
    assert task_dependencies_ready(state, task) is True
    dependency["status"] = "todo"
    assert task_dependencies_ready(state, task) is False
    task["depends_on"] = ["T-999"]
    assert task_dependencies_ready(state, task) is False


def test_missing_existing_formal_dependency_is_rejected_without_files(
    tmp_path: Path,
) -> None:
    project, records = _create_project(tmp_path)
    task = _task("new-task", "拒绝不存在的正式依赖", depends_on=["T-999"])
    submission = _write_submission(tmp_path / "正式依赖缺失", tasks=[task])
    before = (project / ".codex-sdlc/events.jsonl").read_bytes()

    with pytest.raises(SdlcError, match="正式任务不存在：T-999"):
        _import(project, "REQ-001", submission)

    requirement_root = records[0][2]
    assert not (requirement_root / "tasks").exists()
    assert not (requirement_root / "task-coverage.v1.json").exists()
    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == before


def test_coverage_cannot_reference_unknown_formal_task(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    submission = _write_submission(
        tmp_path / "覆盖正式任务缺失",
        tasks=[_task("only", "拒绝覆盖中的未知正式任务")],
    )
    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    coverage["functional_requirements"]["FR-001"]["tasks"].insert(0, "T-999")
    coverage["acceptance_criteria"]["AC-001"]["test_refs"].insert(
        0,
        "T-999#automated_tests/0",
    )
    _write_json(submission[2], coverage)
    before = (project / ".codex-sdlc/events.jsonl").read_bytes()

    with pytest.raises(SdlcError, match="计划外任务：T-999"):
        _import(project, "REQ-001", submission)

    requirement_root = records[0][2]
    assert not (requirement_root / "tasks").exists()
    assert not (requirement_root / "task-coverage.v1.json").exists()
    assert (project / ".codex-sdlc/events.jsonl").read_bytes() == before


def test_existing_formal_task_coverage_survives_interruption_recovery(
    tmp_path: Path,
) -> None:
    project, records = _create_project(tmp_path, requirements=2)
    events_file = project / ".codex-sdlc/events.jsonl"
    existing_event = {
        "event_id": "EVT-20260720-000099",
        "event_type": "task_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": "T-009",
        "created_at": "2026-07-20T10:01:00+08:00",
        "source": "合同测试",
        "summary": "创建已有正式任务",
        "payload": {
            "title": "已有正式任务",
            "summary": "已有正式任务",
            "status": "done",
            "manual_checks": ["已验收"],
            "test_items": ["已测试"],
        },
    }
    events_file.write_text(
        events_file.read_text(encoding="utf-8")
        + json.dumps(existing_event, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    submission = _write_submission(
        tmp_path / "正式覆盖中断",
        requirement_id="REQ-002",
        tasks=[_task("new-task", "中断恢复正式覆盖", depends_on=["T-009"])],
    )
    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    coverage["functional_requirements"]["FR-001"]["tasks"].insert(0, "T-009")
    coverage["acceptance_criteria"]["AC-001"]["test_refs"].insert(
        0,
        "T-009#automated_tests/0",
    )
    _write_json(submission[2], coverage)
    script = f"""
import os
from pathlib import Path
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_contract import import_task_plan_bundle, INTERRUPT_AFTER_DIRECTORY_COMMIT
def stop(stage):
    if stage == INTERRUPT_AFTER_DIRECTORY_COMMIT:
        os._exit(75)
import_task_plan_bundle(
    build_paths(Path({str(project)!r})),
    requirement_id='REQ-002',
    plan_file=Path({str(submission[0])!r}),
    tasks_dir=Path({str(submission[1])!r}),
    coverage_file=Path({str(submission[2])!r}),
    interruption_hook=stop,
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    interrupted = subprocess.run([sys.executable, "-c", script], env=env, check=False)
    assert interrupted.returncode == 75

    recovered = _import(project, "REQ-002", submission)

    assert recovered.duplicate is True
    saved_coverage = json.loads(
        (records[1][2] / "task-coverage.v1.json").read_text(encoding="utf-8")
    )
    assert saved_coverage["functional_requirements"]["FR-001"]["tasks"] == [
        "T-009",
        "T-010",
    ]
    events = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    assert len([event for event in events if event["event_type"] == "task_plan_imported"]) == 1


def test_plain_task_text_containing_client_reference_example_is_preserved(
    tmp_path: Path,
) -> None:
    project, records = _create_project(tmp_path)
    task = _task("only", "保留技术示例文字")
    example = "命令帮助必须原样展示 @client:sample，不能把正文改成正式任务编号。"
    task["implementation_requirements"] = [example]
    submission = _write_submission(tmp_path / "正文示例", tasks=[task])

    _import(project, "REQ-001", submission)

    requirement_root = records[0][2]
    saved = json.loads((requirement_root / "tasks/T-001.json").read_text(encoding="utf-8"))
    markdown = (requirement_root / "tasks/T-001.md").read_text(encoding="utf-8")
    assert saved["implementation_requirements"] == [example]
    assert example in markdown


def test_real_process_interruption_recovers_to_one_complete_success(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "中断输出")
    script = f"""
import os
from pathlib import Path
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_contract import import_task_plan_bundle, INTERRUPT_AFTER_DIRECTORY_COMMIT
def stop(stage):
    if stage == INTERRUPT_AFTER_DIRECTORY_COMMIT:
        os._exit(73)
import_task_plan_bundle(
    build_paths(Path({str(project)!r})),
    requirement_id='REQ-001',
    plan_file=Path({str(submission[0])!r}),
    tasks_dir=Path({str(submission[1])!r}),
    coverage_file=Path({str(submission[2])!r}),
    interruption_hook=stop,
)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    interrupted = subprocess.run([sys.executable, "-c", script], env=env, check=False)
    assert interrupted.returncode == 73

    recovered = _import(project, "REQ-001", submission)

    assert recovered.duplicate is True
    assert (requirement_root / "tasks" / "T-001.json").is_file()
    assert (requirement_root / "tasks" / "T-002.md").is_file()
    assert (requirement_root / "task-coverage.v1.json").is_file()
    events = [
        json.loads(line)
        for line in (project / ".codex-sdlc" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([event for event in events if event["event_type"] == "task_plan_imported"]) == 1
    transaction_root = project / ".codex-sdlc" / "task-import-transactions"
    assert not list(transaction_root.glob("*.json"))
    assert not list((transaction_root / "staging").iterdir())


def test_two_real_processes_allocate_unique_task_ids(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path, requirements=2)
    first = _write_submission(tmp_path / "并发一", requirement_id="REQ-001", tasks=[_task("first", "并发任务一")])
    second_task = _task("second", "并发任务二")
    second = _write_submission(tmp_path / "并发二", requirement_id="REQ-002", tasks=[second_task])
    script = """
from pathlib import Path
import sys
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_contract import import_task_plan_bundle
project, requirement_id, plan, tasks, coverage = sys.argv[1:]
result = import_task_plan_bundle(
    build_paths(Path(project)),
    requirement_id=requirement_id,
    plan_file=Path(plan),
    tasks_dir=Path(tasks),
    coverage_file=Path(coverage),
)
print(next(iter(result.mapping.values())))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(project),
                requirement_id,
                str(submission[0]),
                str(submission[1]),
                str(submission[2]),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for requirement_id, submission in (("REQ-001", first), ("REQ-002", second))
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert set(outputs) == {"T-001", "T-002"}


def test_task_quality_does_not_fill_missing_contract_fields(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    task = _task("only", "不允许默认补全")
    task["automated_tests"] = []
    submission = _write_submission(tmp_path / "缺测试", tasks=[task])

    with pytest.raises(SdlcError, match="automated_tests|自动测试"):
        _import(project, "REQ-001", submission)

    assert not (requirement_root / "tasks").exists()


def test_import_file_name_cannot_pretend_to_be_formal_task_id(tmp_path: Path) -> None:
    project, records = _create_project(tmp_path)
    requirement_root = records[0][2]
    submission = _write_submission(tmp_path / "伪装文件名", tasks=[_task("only", "文件名检查")])
    only_file = next(submission[1].iterdir())
    only_file.rename(submission[1] / "T-999.task.v2.json")

    with pytest.raises(SdlcError, match="文件名|client_key"):
        _import(project, "REQ-001", submission)

    assert not (requirement_root / "tasks").exists()


def test_schema_hash_uses_complete_content_instead_of_source_file_size(tmp_path: Path) -> None:
    project, _ = _create_project(tmp_path)
    submission = _write_submission(tmp_path / "摘要")
    first = _import(project, "REQ-001", submission)
    source_payload = {
        "plan": json.loads(submission[0].read_text(encoding="utf-8")),
        "tasks": [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(submission[1].glob("*.json"))
        ],
        "coverage": json.loads(submission[2].read_text(encoding="utf-8")),
    }
    assert first.package_sha256 != sha256_bytes(
        str(sum(path.stat().st_size for path in submission[1].glob("*.json"))).encode("utf-8")
    )
    assert len(canonical_json_text(source_payload)) > 100
