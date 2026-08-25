from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.core import task_run
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.services import review_service
from test_task_plan_review_flow import (
    _add_second_task_to_submission,
    _create_review,
    _set_submission_dependencies,
    _submission,
)
from test_task_planning_code_evidence import (
    _git,
    _project,
    _task_args,
    _write_task_submission,
)
from test_task_run_contract import _activate_run


def _runtime_documents(requirement_root: Path) -> tuple[dict[str, object], dict[str, object]]:
    run = json.loads(
        (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    return run, current


def test_project_rule_drift_marks_run_stale_and_blocks_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    (project / "AGENTS.md").write_text("所有开发必须执行合同测试和回归测试。\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="TASK_RUN_STALE"):
        task_run.require_active_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )

    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "stale"


def test_requirement_design_and_reference_drift_mark_run_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    formal = requirement_root / "original/formal.v3.json"
    formal.write_text(
        '{"formal_contract_version":"formal.v3","workflow_profile":"document-first.v2"}\n',
        encoding="utf-8",
    )
    reference_index_path = requirement_root / "reference-index.v1.json"
    reference_index = json.loads(reference_index_path.read_text(encoding="utf-8"))
    current_hash = sha256_file(formal)
    for entry in reference_index["entries"].values():
        entry["sha256"] = current_hash
    reference_index_path.write_text(
        json.dumps(reference_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="requirement.*design.*reference_index"):
        task_run.require_active_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )


def test_upstream_hash_failure_does_not_reuse_old_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    (requirement_root / "reference-index.v1.json").write_text(
        "{不是有效的 JSON\n", encoding="utf-8"
    )

    with pytest.raises(SdlcError, match="upstream_unreadable"):
        task_run.require_active_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )

    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "stale"


def test_predecessor_output_drift_marks_dependent_run_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path)
    submission_files = _write_task_submission(tmp_path / "正式任务输入", requirement_root)
    _add_second_task_to_submission(submission_files)
    _set_submission_dependencies(submission_files, {"main": [], "second": ["@client:main"]})
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    assert run_tasks_finalize(_task_args(submission_files)) == 0
    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    append_event(
        paths,
        event_type="task_updated",
        source="合同测试",
        summary="完成前置任务",
        requirement_id="REQ-001",
        task_id="T-001",
        payload={"status": "done", "output_files": ["src/app.py"]},
    )
    assert derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]["status"] == "done"
    monkeypatch.setenv("CODEX_THREAD_ID", "任务开发线程")
    assert task_run.initialize_task_run(
        paths,
        derive_state(paths),
        derive_state(paths)["requirements"]["REQ-001"],
        derive_state(paths)["requirements"]["REQ-001"]["tasks"][1],
    )["run"]["status"] == "reading"
    run_path = requirement_root / "runtime/T-002/runs/0001/task-run.v1.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    task_run.confirm_task_read(
        paths,
        requirement_id="REQ-001",
        task_id="T-002",
        manifest_sha256=str(run["read_manifest_sha256"]),
    )

    (project / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="predecessor_outputs"):
        task_run.require_active_task_run(
            paths, requirement_id="REQ-001", task_id="T-002"
        )


def test_unexplained_dirty_change_is_preserved_and_blocks_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _activate_run(tmp_path, monkeypatch)
    unrelated = project / "未归属说明.txt"
    changed = "这项变化不属于当前任务。\n"
    unrelated.write_text(changed, encoding="utf-8")

    with pytest.raises(SdlcError, match="TASK_SCOPE_UNEXPLAINED"):
        task_run.require_active_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )

    assert unrelated.read_text(encoding="utf-8") == changed


def test_allowed_output_path_is_a_literal_git_path_not_a_pattern(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path)
    literal_output = project / "src/[abc].py"
    literal_output.write_text("VALUE = 1\n", encoding="utf-8")
    _git(project, "add", "src/[abc].py")
    _git(project, "commit", "-qm", "加入含通配字符的字面文件")
    submission_files = _write_task_submission(tmp_path / "正式任务输入", requirement_root)
    task_path = submission_files[1] / "main.task.v2.json"
    task_document = json.loads(task_path.read_text(encoding="utf-8"))
    task_document["code_scope"]["likely_change_paths"] = ["src/[abc].py"]
    task_path.write_text(
        json.dumps(task_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    assert run_tasks_finalize(_task_args(submission_files)) == 0
    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "任务开发线程")
    assert task_run.initialize_task_run(
        paths,
        derive_state(paths),
        derive_state(paths)["requirements"]["REQ-001"],
        derive_state(paths)["requirements"]["REQ-001"]["tasks"][0],
    )["run"]["status"] == "reading"
    run_path = requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    task_run.confirm_task_read(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        manifest_sha256=str(run["read_manifest_sha256"]),
    )

    literal_output.write_text("VALUE = 2\n", encoding="utf-8")
    task_run.require_active_task_run(
        paths, requirement_id="REQ-001", task_id="T-001"
    )
    unexpected = project / "src/a.py"
    unexpected.write_text("UNEXPECTED = True\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="TASK_SCOPE_UNEXPLAINED"):
        task_run.require_active_task_run(
            paths, requirement_id="REQ-001", task_id="T-001"
        )
    assert unexpected.read_text(encoding="utf-8") == "UNEXPECTED = True\n"


def test_interrupted_stale_write_recomputes_real_inputs_instead_of_trusting_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    rules = project / "AGENTS.md"
    original = rules.read_text(encoding="utf-8")
    rules.write_text("规则发生漂移。\n", encoding="utf-8")

    def interrupt(_run: dict[str, object]) -> None:
        raise OSError("故障注入：当前指针写入前中断")

    monkeypatch.setattr(task_run, "_before_run_status_current_commit", interrupt)
    with pytest.raises(SdlcError, match="状态同步失败"):
        task_run.require_active_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )
    run, current = _runtime_documents(requirement_root)
    assert run["status"] == "stale"
    assert current["status"] == "active"

    rules.write_text(original, encoding="utf-8")
    monkeypatch.setattr(task_run, "_before_run_status_current_commit", lambda _run: None)
    task_run.require_active_task_run(
        build_paths(project), requirement_id="REQ-001", task_id="T-001"
    )
    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "active"
