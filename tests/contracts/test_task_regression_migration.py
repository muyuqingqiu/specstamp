from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest

from codex_sdlc.commands import regression_cmd, task_cmd
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_evidence import register_task_evidence
from codex_sdlc.services import review_service
from test_task_direct_start import _reviewed_project, _start_args
from test_task_plan_review_flow import _submission
from test_task_run_contract import _activate_run


ROOT = Path(__file__).resolve().parents[2]
TASK_COMMAND = ROOT / "src/codex_sdlc/commands/task_cmd.py"
REGRESSION_COMMAND = ROOT / "src/codex_sdlc/commands/regression_cmd.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_task_and_regression_production_paths_do_not_import_task_pack() -> None:
    """T-041 删除旧模块后，两个正式入口仍必须能够独立导入。"""

    forbidden = {
        "codex_sdlc.core.task_pack",
        "codex_sdlc.core.task_pack_contract",
    }
    for path in (TASK_COMMAND, REGRESSION_COMMAND):
        assert not (_imports(path) & forbidden)

    task_source = TASK_COMMAND.read_text(encoding="utf-8")
    for old_consumer in (
        "ensure_task_pack_ready",
        "read_task_pack_metadata",
        "task_pack_test_contract",
        "write_task_pack",
        "mark_task_pack_stale",
    ):
        assert old_consumer not in task_source


def test_formal_task_pause_rejects_missing_runtime_instead_of_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式任务只有真实 task-run 才能暂停，不能只凭 doing 状态猜测。"""

    project, _requirement_root = _reviewed_project(tmp_path, monkeypatch)
    paths = build_paths(project)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    assert isinstance(task.get("task_contract"), dict)
    append_event(
        paths,
        event_type="task_updated",
        source="迁移合同测试",
        summary="构造缺少运行轮次的进行中任务",
        requirement_id="REQ-001",
        task_id="T-001",
        payload={"status": "doing"},
    )

    with pytest.raises(SdlcError, match="运行轮次"):
        task_cmd.run_pause(
            argparse.Namespace(
                first_id="REQ-001",
                second_id="T-001",
                reason="等待重新安排",
            )
        )

    refreshed = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    assert refreshed["status"] == "doing"


def test_formal_task_pause_stales_current_run_before_returning_to_todo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂停只关闭当前运行资格，已有轮次和证据文件仍保留。"""

    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    assert task_cmd.run_pause(
        argparse.Namespace(
            first_id="REQ-001",
            second_id="T-001",
            reason="等待重新安排",
        )
    ) == 0

    paths = build_paths(project)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    old_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert task["status"] == "todo"
    assert old_run["status"] == "stale"

    assert task_cmd.run(_start_args()) == 0
    new_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0002/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert new_run["status"] == "reading"


def test_formal_reading_task_can_pause_and_restart_with_a_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取阶段也是真实活动轮次；暂停后旧清单保留，重新开工必须换新轮次。"""

    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    run_root = requirement_root / "runtime/T-001/runs/0001"
    manifest_path = run_root / "task-read-manifest.v1.json"
    manifest_before = manifest_path.read_bytes()

    assert task_cmd.run_pause(
        argparse.Namespace(
            first_id="REQ-001",
            second_id="T-001",
            reason="读取完成前先暂停",
        )
    ) == 0

    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    old_run = json.loads(
        (run_root / "task-run.v1.json").read_text(encoding="utf-8")
    )
    assert current["status"] == old_run["status"] == "stale"
    assert manifest_path.read_bytes() == manifest_before
    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]["status"] == "todo"

    assert task_cmd.run(_start_args()) == 0
    new_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0002/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert new_run["status"] == "reading"
    assert (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").is_file()


@pytest.mark.parametrize("damage", ["broken_json", "identity_mismatch", "closed"])
def test_formal_task_pause_rejects_damaged_mismatched_or_closed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """暂停不能越过损坏、身份错位或已经关闭的轮次。"""

    project, requirement_root = _reviewed_project(tmp_path, monkeypatch)
    assert task_cmd.run(_start_args()) == 0
    current_path = requirement_root / "runtime/T-001/current.json"
    run_path = requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    if damage == "broken_json":
        current_path.write_text("{\n", encoding="utf-8")
    else:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        current = json.loads(current_path.read_text(encoding="utf-8"))
        if damage == "identity_mismatch":
            run["task_id"] = "T-999"
        else:
            run["status"] = "closed"
            current["status"] = "closed"
        run_path.write_text(json.dumps(run, ensure_ascii=False) + "\n", encoding="utf-8")
        current["task_run_sha256"] = sha256_file(run_path)
        current_path.write_text(
            json.dumps(current, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    with pytest.raises(SdlcError):
        task_cmd.run_pause(
            argparse.Namespace(
                first_id="REQ-001",
                second_id="T-001",
                reason="不应成功",
            )
        )

    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]["status"] == "doing"


def test_regression_uses_formal_coverage_and_rejects_active_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """需求回归使用正式覆盖关系，但活动轮次仍不能冒充完成证据。"""

    _project, _requirement_root = _reviewed_project(tmp_path, monkeypatch)
    start_args = argparse.Namespace(
        first_id="REQ-001",
        second_id="T-001",
        done=False,
        note="",
        file=[],
        executed_commands=[],
        verify=[],
        verification_type="",
        verification_status="",
        change_report="",
        test_item=[],
        test_command=[],
        replace_test_command=[],
        clear_test_commands=False,
        test_script=[],
        replace_test_script=[],
        clear_test_scripts=False,
        manual_check=[],
    )
    assert task_cmd.run(start_args) == 0

    result = regression_cmd.run(argparse.Namespace(items=["REQ-001", "T-001"]))
    assert result == 1


def test_regression_rejects_missing_formal_coverage_relationship() -> None:
    task = {"task_id": "T-001", "task_contract": {"schema_version": "task.v2"}}
    with pytest.raises(SdlcError, match="task-coverage.v1"):
        regression_cmd.formal_coverage_cases({}, task)

    coverage = {
        "schema_version": "task-coverage.v1",
        "functional_requirements": {},
        "design_artifacts": {},
        "acceptance_criteria": {},
        "effective_changes": {},
    }
    with pytest.raises(SdlcError, match="没有覆盖关系"):
        regression_cmd.formal_coverage_cases(
            {"task_coverage_contract": coverage}, task
        )


def test_formal_task_completion_and_regression_share_the_same_run_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    evidence_root = requirement_root / "runtime/T-001/runs/0001/evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)

    for index, test_item in enumerate(task["test_items"], start=1):
        source = evidence_root / f"test-{index}.log"
        source.write_text(f"{test_item}\n通过\n", encoding="utf-8")
        register_task_evidence(
            paths,
            requirement_id="REQ-001",
            task_id="T-001",
            kind="test",
            source_file=source.relative_to(project).as_posix(),
            source_sha256=sha256_file(source),
            command=f"python3 -m pytest tests/test_{index}.py",
            exit_code=0,
            result="passed",
            test_item=str(test_item),
        )

    manual_source = evidence_root / "manual.json"
    manual_source.write_text(
        json.dumps(
            {
                "environment": "本机正式入口",
                "checks": [
                    {
                        "item": str(item),
                        "expected": "符合正式任务合同",
                        "actual": "实际检查通过",
                        "result": "passed",
                    }
                    for item in task["manual_checks"]
                ],
                "summary": "人工验收全部通过",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    register_task_evidence(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        kind="verification",
        source_file=manual_source.relative_to(project).as_posix(),
        source_sha256=sha256_file(manual_source),
        command="人工验收",
        exit_code=0,
        result="passed",
    )

    done_args = _start_args()
    done_args.done = True
    assert task_cmd.run(done_args) == 0
    assert regression_cmd.run(argparse.Namespace(items=["REQ-001", "T-001"])) == 0

    old_run = (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_bytes()
    old_evidence = {
        path.relative_to(requirement_root).as_posix(): path.read_bytes()
        for path in (requirement_root / "runtime/T-001/runs/0001/evidence").iterdir()
    }

    assert task_cmd.run_fix(
        argparse.Namespace(items=["REQ-001", "T-001", "修复回归发现的问题"])
    ) == 0
    after_fix = derive_state(paths)["requirements"]["REQ-001"]
    assert after_fix["tasks"][0]["status"] == "done"
    fix_task = after_fix["tasks"][1]
    assert fix_task["status"] == "todo"
    assert isinstance(fix_task.get("task_contract"), dict)
    assert fix_task["task_id"] in after_fix["task_plan_contract"]["tasks"]
    review_status = review_service.task_plan_review_status(
        paths, requirement_id="REQ-001"
    )
    assert review_status["can_advance"] is False
    current_review = [item for item in review_status["reviews"] if item["is_current"]][0]
    request = {
        "review_id": current_review["review_id"],
        "input_hashes": current_review["input_hashes"],
    }
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "任务开发线程")
    start_fix = _start_args()
    start_fix.second_id = str(fix_task["task_id"])
    assert task_cmd.run(start_fix) == 0

    # 复查命令同样要生成完整正式合同。先把修复任务留在 reading 会被安全拒绝，
    # 暂停后再验证 audit 的正式整包更新和重新审核入口。
    assert task_cmd.run_pause(
        argparse.Namespace(
            first_id="REQ-001",
            second_id=str(fix_task["task_id"]),
            reason="切换到质量复查",
        )
    ) == 0
    assert task_cmd.run_audit(
        argparse.Namespace(
            items=["REQ-001", "T-001"],
            note="复查正式任务质量",
        )
    ) == 0
    after_audit = derive_state(paths)["requirements"]["REQ-001"]
    audit_task = after_audit["tasks"][-1]
    assert audit_task["status"] == "todo"
    assert isinstance(audit_task.get("task_contract"), dict)
    assert audit_task["task_id"] in after_audit["task_plan_contract"]["tasks"]
    audit_review_status = review_service.task_plan_review_status(
        paths, requirement_id="REQ-001"
    )
    audit_review = [
        item for item in audit_review_status["reviews"] if item["is_current"]
    ][0]
    audit_request = {
        "review_id": audit_review["review_id"],
        "input_hashes": audit_review["input_hashes"],
    }
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(audit_request["review_id"]),
        submission=_submission(audit_request, status="passed", issues=[]),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "任务开发线程")
    start_audit = _start_args()
    start_audit.second_id = str(audit_task["task_id"])
    assert task_cmd.run(start_audit) == 0
    assert (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_bytes() == old_run
    assert {
        path.relative_to(requirement_root).as_posix(): path.read_bytes()
        for path in (requirement_root / "runtime/T-001/runs/0001/evidence").iterdir()
    } == old_evidence


def test_failed_formal_test_can_restore_into_a_fresh_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    source = requirement_root / "runtime/T-001/runs/0001/evidence/failed.log"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("1 failed\n", encoding="utf-8")
    register_task_evidence(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        kind="test",
        source_file=source.relative_to(project).as_posix(),
        source_sha256=sha256_file(source),
        command="python3 -m pytest tests/test_failure.py",
        exit_code=1,
        result="failed",
        test_item=str(task["test_items"][0]),
    )

    done_args = _start_args()
    done_args.done = True
    with pytest.raises(SdlcError, match="失败测试"):
        task_cmd.run(done_args)
    failed_task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    assert failed_task["status"] == "test_failed"

    assert task_cmd.run_restore(
        argparse.Namespace(
            items=["REQ-001", "T-001", "修复失败测试后重新验证"],
            feedback_contract="",
        )
    ) == 0
    old_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    new_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0002/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert old_run["status"] == "stale"
    assert old_run["test_records"][0]["result"] == "failed"
    assert new_run["status"] == "reading"
    assert new_run["test_records"] == []
