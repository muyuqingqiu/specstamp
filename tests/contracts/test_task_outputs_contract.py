from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.core import task_run
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import canonical_json_text, sha256_file
from codex_sdlc.core.task_evidence import register_task_evidence
from codex_sdlc.core.task_outputs import (
    bind_predecessor_outputs,
    formal_index_sha256,
    formal_task_output_index_path,
    index_completed_task,
    load_formal_task_output_index,
    remove_completed_task,
    replace_formal_task_output_index,
)
from test_task_done_run_gate import _record_manual, _record_test
from test_task_run_contract import _activate_run
from test_task_plan_review_flow import (
    _add_second_task_to_submission,
    _create_review,
    _set_submission_dependencies,
    _submission,
)
from test_task_planning_code_evidence import (
    _project,
    _task_args,
    _write_task_submission,
)
from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.services import review_service


def _formal_fixture(tmp_path: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
    project = tmp_path / "正式索引项目"
    requirement_root = project / ".codex-sdlc/requirements/REQ-001-正式索引"
    run_root = requirement_root / "runtime/T-001/runs/0001"
    (requirement_root / "tasks").mkdir(parents=True)
    run_root.mkdir(parents=True)
    output = project / "src/结果.txt"
    output.parent.mkdir(parents=True)
    output.write_text("正式交付结果\n", encoding="utf-8")
    task_file = requirement_root / "tasks/T-001.json"
    task_file.write_text('{"schema_version":"task.v2"}\n', encoding="utf-8")
    run = {
        "schema_version": "task-run.v1",
        "requirement_id": "REQ-001",
        "task_id": "T-001",
        "run_number": 1,
        "status": "closed",
        "task_sha256": sha256_file(task_file),
        "allowed_output_paths": ["src"],
    }
    run_path = run_root / "task-run.v1.json"
    run_path.write_text(canonical_json_text(run), encoding="utf-8")
    current_path = requirement_root / "runtime/T-001/current.json"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text(
        canonical_json_text(
            {
                "task_id": "T-001",
                "run_number": 1,
                "status": "closed",
                "task_run_sha256": sha256_file(run_path),
            }
        ),
        encoding="utf-8",
    )
    requirement = {"requirement_id": "REQ-001", "folder_name": requirement_root.name}
    return project, requirement, run


def test_formal_index_has_stable_identity_sorted_files_and_canonical_bytes(tmp_path: Path) -> None:
    project, requirement, run = _formal_fixture(tmp_path)
    paths = build_paths(project)
    second = project / "src/另一个结果.txt"
    second.write_text("第二个结果\n", encoding="utf-8")

    document = index_completed_task(
        paths,
        requirement,
        task_id="T-001",
        closed_run=run,
        actual_changed_files=["src/结果.txt", "src/另一个结果.txt", "src/结果.txt"],
    )
    path = replace_formal_task_output_index(paths, requirement, document)
    first_bytes = path.read_bytes()
    first_hash = sha256_file(path)
    rebuilt = index_completed_task(
        paths,
        requirement,
        task_id="T-001",
        closed_run=run,
        actual_changed_files=["src/另一个结果.txt", "src/结果.txt"],
        base_document=document,
    )
    replace_formal_task_output_index(paths, requirement, rebuilt)

    assert path.read_bytes() == first_bytes
    assert sha256_file(path) == first_hash == formal_index_sha256(document)
    assert first_bytes.endswith(b"\n")
    assert b"created_at" not in first_bytes and b"updated_at" not in first_bytes
    entry = document["task_outputs"][0]
    assert entry["entry_id"] == "REQ-001:T-001:run-0001"
    assert [item["path"] for item in entry["files"]] == ["src/另一个结果.txt", "src/结果.txt"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"updated_at": "2026-07-22"}), "顶层结构"),
        (lambda value: value["task_outputs"].append(deepcopy(value["task_outputs"][0])), "身份、排序"),
        (lambda value: value["task_outputs"][0]["files"].append(deepcopy(value["task_outputs"][0]["files"][0])), "文件身份"),
        (lambda value: value["task_outputs"][0]["files"][0].update({"path": "../越界.txt"}), "文件身份|路径"),
    ],
)
def test_formal_index_rejects_extra_duplicate_and_invalid_paths(
    tmp_path: Path, mutate, message: str
) -> None:
    project, requirement, run = _formal_fixture(tmp_path)
    paths = build_paths(project)
    document = index_completed_task(
        paths, requirement, task_id="T-001", closed_run=run, actual_changed_files=["src/结果.txt"]
    )
    mutate(document)
    path = formal_task_output_index_path(paths, requirement)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(SdlcError, match=message):
        load_formal_task_output_index(paths, requirement)


def test_predecessor_manifest_and_run_hash_use_the_same_formal_index(tmp_path: Path) -> None:
    project, requirement, run = _formal_fixture(tmp_path)
    paths = build_paths(project)
    document = index_completed_task(
        paths, requirement, task_id="T-001", closed_run=run, actual_changed_files=["src/结果.txt"]
    )
    index_path = replace_formal_task_output_index(paths, requirement, document)
    manifest, hashes = bind_predecessor_outputs(
        paths,
        requirement,
        {"task_id": "T-002", "depends_on": ["T-001"]},
        {"predecessor_outputs": []},
        {"predecessor_outputs": "0" * 64},
    )
    assert manifest["predecessor_outputs"] == [
        {
            "task_id": "T-001",
            "path": "src/结果.txt",
            "locator": {"kind": "whole_file"},
            "sha256": sha256_file(project / "src/结果.txt"),
        }
    ]
    assert hashes["predecessor_outputs"] == sha256_file(index_path)

    (project / "src/结果.txt").write_text("内容被改动\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="已经变化"):
        bind_predecessor_outputs(
            paths,
            requirement,
            {"task_id": "T-002", "depends_on": ["T-001"]},
            {"predecessor_outputs": []},
            {"predecessor_outputs": "0" * 64},
        )


def test_completed_task_writes_index_and_restore_removes_old_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _record_test(project, requirement_root)
    _record_manual(project, requirement_root)
    paths = build_paths(project)
    task_run.complete_task_run(paths, requirement_id="REQ-001", task_id="T-001")
    requirement = {"requirement_id": "REQ-001", "folder_name": requirement_root.name}
    completed = load_formal_task_output_index(paths, requirement)
    assert completed["task_outputs"][0]["files"][0]["path"] == "src/app.py"
    old_run = (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_bytes()

    state = task_run.derive_state(paths)
    task_run.restore_task_run(
        paths,
        state=state,
        requirement=state["requirements"]["REQ-001"],
        task=state["requirements"]["REQ-001"]["tasks"][0],
        reason="重新核对正式索引",
    )
    restored = load_formal_task_output_index(paths, requirement)
    assert restored["task_outputs"] == []
    # 恢复只把旧轮次状态按 T-027 合同关闭，不会改写已经保存的证据文件。
    assert b'"test_records"' in old_run
    assert (requirement_root / "runtime/T-001/runs/0001/evidence").is_dir()


def test_completion_index_interruption_rolls_back_every_formal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    _record_test(project, requirement_root)
    _record_manual(project, requirement_root)
    paths = build_paths(project)
    index_path = requirement_root / "task-outputs/task-output-index.v1.json"
    run_path = requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    current_path = requirement_root / "runtime/T-001/current.json"
    assert index_path.is_file()
    index_before = index_path.read_bytes()
    run_before = run_path.read_bytes()
    current_before = current_path.read_bytes()
    events_before = paths.events_file.read_bytes()
    event_backups_before = {
        path.name: path.read_bytes()
        for path in sorted(paths.backups_dir.glob("events-*.jsonl.bak"))
    }

    monkeypatch.setenv("CODEX_SDLC_TASK_OUTPUT_INTERRUPT_AT", "after_index")
    with pytest.raises(SdlcError, match="任务完成事务失败"):
        task_run.complete_task_run(paths, requirement_id="REQ-001", task_id="T-001")

    # 完成事务已经替换过索引后仍必须逐字节恢复，不能只检查状态字段看起来仍是 active。
    assert index_path.read_bytes() == index_before
    assert run_path.read_bytes() == run_before
    assert current_path.read_bytes() == current_before
    assert paths.events_file.read_bytes() == events_before
    assert {
        path.name: path.read_bytes()
        for path in sorted(paths.backups_dir.glob("events-*.jsonl.bak"))
    } == event_backups_before


def _record_task_completion_evidence(
    project: Path,
    requirement_root: Path,
    *,
    task_id: str,
    run_number: int,
) -> None:
    """两条串行任务都走同一证据门禁，避免测试用内部状态直接伪造完成。"""

    paths = build_paths(project)
    state = derive_state(paths)
    task = next(
        item
        for item in state["requirements"]["REQ-001"]["tasks"]
        if item["task_id"] == task_id
    )
    evidence_root = (
        requirement_root / "runtime" / task_id / "runs" / f"{run_number:04d}" / "evidence"
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    test_source = evidence_root / "test.log"
    test_source.write_text("1 passed\n", encoding="utf-8")
    register_task_evidence(
        paths,
        requirement_id="REQ-001",
        task_id=task_id,
        kind="test",
        source_file=test_source.relative_to(project).as_posix(),
        source_sha256=sha256_file(test_source),
        command="python3 -m pytest -q",
        exit_code=0,
        result="passed",
        test_item=str(task["test_items"][0]),
    )
    manual_source = evidence_root / "manual.json"
    manual_source.write_text(
        json.dumps(
            {
                "environment": "仓库外临时 Git 项目",
                "checks": [
                    {
                        "item": str(task["manual_checks"][0]),
                        "expected": "正式任务证据哈希一致",
                        "actual": "逐项核对一致",
                        "result": "passed",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_task_evidence(
        paths,
        requirement_id="REQ-001",
        task_id=task_id,
        kind="verification",
        source_file=manual_source.relative_to(project).as_posix(),
        source_sha256=sha256_file(manual_source),
        command="人工逐项核对",
        exit_code=0,
        result="passed",
    )


def test_dependent_task_can_complete_the_same_output_path_and_keeps_both_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path)
    submission = _write_task_submission(tmp_path / "正式任务输入", requirement_root)
    _add_second_task_to_submission(submission)
    _set_submission_dependencies(submission, {"main": [], "second": ["@client:main"]})
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "T028同路径任务规划")
    assert run_tasks_finalize(_task_args(submission)) == 0
    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "T028同路径任务审核")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )

    monkeypatch.setenv("CODEX_THREAD_ID", "T028前置任务")
    state = derive_state(paths)
    requirement = state["requirements"]["REQ-001"]
    first_task = requirement["tasks"][0]
    first = task_run.initialize_task_run(paths, state, requirement, first_task)
    task_run.confirm_task_read(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        manifest_sha256=str(first["run"]["read_manifest_sha256"]),
    )
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    first_sha256 = sha256_file(project / "src/app.py")
    _record_task_completion_evidence(
        project, requirement_root, task_id="T-001", run_number=1
    )
    task_run.complete_task_run(paths, requirement_id="REQ-001", task_id="T-001")

    monkeypatch.setenv("CODEX_THREAD_ID", "T028后续任务")
    state = derive_state(paths)
    requirement = state["requirements"]["REQ-001"]
    second_task = requirement["tasks"][1]
    second = task_run.initialize_task_run(paths, state, requirement, second_task)
    task_run.confirm_task_read(
        paths,
        requirement_id="REQ-001",
        task_id="T-002",
        manifest_sha256=str(second["run"]["read_manifest_sha256"]),
    )
    (project / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
    second_sha256 = sha256_file(project / "src/app.py")
    _record_task_completion_evidence(
        project, requirement_root, task_id="T-002", run_number=1
    )
    task_run.complete_task_run(paths, requirement_id="REQ-001", task_id="T-002")

    index = load_formal_task_output_index(
        paths,
        {"requirement_id": "REQ-001", "folder_name": requirement_root.name},
    )
    entries = {item["task_id"]: item for item in index["task_outputs"]}
    assert entries["T-001"]["files"] == [
        {
            "output_id": "REQ-001:T-001:run-0001:src/app.py",
            "path": "src/app.py",
            "sha256": first_sha256,
        }
    ]
    assert entries["T-002"]["files"] == [
        {
            "output_id": "REQ-001:T-002:run-0001:src/app.py",
            "path": "src/app.py",
            "sha256": second_sha256,
        }
    ]
    manifest, _hashes = bind_predecessor_outputs(
        paths,
        {"requirement_id": "REQ-001", "folder_name": requirement_root.name},
        {"task_id": "T-003", "depends_on": ["T-002"]},
        {"predecessor_outputs": []},
        {"predecessor_outputs": "0" * 64},
    )
    assert manifest["predecessor_outputs"] == [
        {
            "task_id": "T-002",
            "path": "src/app.py",
            "locator": {"kind": "whole_file"},
            "sha256": second_sha256,
        }
    ]
