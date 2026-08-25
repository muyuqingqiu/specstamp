from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from test_cli_v1 import (
    SDLC_SKILLS_HOME,
    init_demo_repo,
    run_cli as run_cli_base,
    run_git,
    write_change_report as write_base_change_report,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "contracts"))

from codex_sdlc.commands.task_cmd import (  # noqa: E402
    run_test_scripts,
    task_change_report_quality_issues,
    validate_change_report_text,
)
from codex_sdlc.core.project import build_paths  # noqa: E402
from codex_sdlc.core.state import derive_state  # noqa: E402
from codex_sdlc.core.structured_contract import sha256_file  # noqa: E402
from codex_sdlc.services import review_service  # noqa: E402
from test_task_direct_start import _reviewed_project  # noqa: E402
from test_task_plan_review_flow import _submission  # noqa: E402


def write_change_report(
    project_dir: Path,
    task_id: str = "T-001",
    title: str = "订单导出按钮",
) -> Path:
    """沿用已验证的人读报告骨架，只替换用例关注的业务标题。"""

    report_path = write_base_change_report(project_dir, task_id)
    report_text = report_path.read_text(encoding="utf-8")
    report_path.write_text(
        report_text.replace("订单导出", title).replace("当前任务能力", title),
        encoding="utf-8",
    )
    return report_path


def _direct_project(tmp_path: Path) -> tuple[Path, Path]:
    """创建经过独立任务审核的正式 task.v2 项目，不再伪造旧任务事件。"""

    patcher = pytest.MonkeyPatch()
    try:
        project, requirement_root = _reviewed_project(tmp_path, patcher)
    finally:
        patcher.undo()
    return project, requirement_root


def _cli(
    project: Path,
    args: list[str],
    *,
    thread_id: str = "任务开发线程",
):
    """所有命令都从正式 bin 入口运行，并显式保留同一任务线程身份。"""

    return run_cli_base(
        args,
        cwd=project,
        extra_env={
            "CODEX_THREAD_ID": thread_id,
            "CODEX_SDLC_DISABLE_AUTO_BACKUP": "1",
        },
    )


def _requirement(project: Path) -> dict[str, object]:
    return derive_state(build_paths(project))["requirements"]["REQ-001"]


def _task(project: Path, task_id: str = "T-001") -> dict[str, object]:
    requirement = _requirement(project)
    return next(item for item in requirement["tasks"] if item["task_id"] == task_id)


def _task_status(project: Path, task_id: str = "T-001") -> str:
    return str(_task(project, task_id)["status"])


def _run_root(requirement_root: Path, task_id: str = "T-001", run_number: int = 1) -> Path:
    return requirement_root / "runtime" / task_id / "runs" / f"{run_number:04d}"


def _start_and_confirm(
    project: Path,
    requirement_root: Path,
    *,
    task_id: str = "T-001",
    infer: bool = False,
) -> tuple[object, object]:
    """真实执行 task 与 task-read-confirm，避免测试跳过 reading 状态。"""

    start_args = ["task"] if infer else ["task", "REQ-001", task_id]
    started = _cli(project, start_args)
    assert started.returncode == 0, started.stderr
    assert "运行状态：reading" in started.stdout
    current = json.loads(
        (requirement_root / "runtime" / task_id / "current.json").read_text(
            encoding="utf-8"
        )
    )
    confirmed = _cli(
        project,
        [
            "task-read-confirm",
            "REQ-001",
            task_id,
            "--manifest-sha256",
            str(current["read_manifest_sha256"]),
        ],
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "运行状态：active" in confirmed.stdout
    return started, confirmed


def _write_evidence(
    project: Path,
    requirement_root: Path,
    *,
    name: str,
    content: str,
    task_id: str = "T-001",
    run_number: int = 1,
) -> tuple[str, str]:
    path = _run_root(requirement_root, task_id, run_number) / "evidence" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.relative_to(project).as_posix(), sha256_file(path)


def _register_test_evidence(
    project: Path,
    requirement_root: Path,
    *,
    result: str = "passed",
    exit_code: int = 0,
    task_id: str = "T-001",
    run_number: int = 1,
) -> object:
    task = _task(project, task_id)
    test_item = str(task["test_items"][0])
    source_file, digest = _write_evidence(
        project,
        requirement_root,
        name=f"test-{result}.log",
        content=f"命令原始输出：{result}\n",
        task_id=task_id,
        run_number=run_number,
    )
    recorded = _cli(
        project,
        [
            "task-evidence",
            "REQ-001",
            task_id,
            "--kind",
            "test",
            "--source-file",
            source_file,
            "--sha256",
            digest,
            "--command",
            "python3 -m pytest -q tests/test_contract.py",
            "--exit-code",
            str(exit_code),
            "--result",
            result,
            "--test-item",
            test_item,
        ],
    )
    assert recorded.returncode == 0, recorded.stderr
    assert digest in recorded.stdout
    return recorded


def _register_manual_evidence(
    project: Path,
    requirement_root: Path,
    *,
    task_id: str = "T-001",
    run_number: int = 1,
) -> object:
    task = _task(project, task_id)
    document = {
        "environment": "仓库外临时 Git 项目",
        "checks": [
            {
                "item": str(item),
                "expected": "符合正式任务合同",
                "actual": "逐项检查通过",
                "result": "passed",
            }
            for item in task["manual_checks"]
        ],
    }
    source_file, digest = _write_evidence(
        project,
        requirement_root,
        name="manual.json",
        content=json.dumps(document, ensure_ascii=False),
        task_id=task_id,
        run_number=run_number,
    )
    recorded = _cli(
        project,
        [
            "task-evidence",
            "REQ-001",
            task_id,
            "--kind",
            "verification",
            "--source-file",
            source_file,
            "--sha256",
            digest,
            "--command",
            "人工逐项验收",
            "--exit-code",
            "0",
            "--result",
            "passed",
        ],
    )
    assert recorded.returncode == 0, recorded.stderr
    return recorded


def _register_feedback_evidence(
    project: Path,
    requirement_root: Path,
    *,
    content: str = "现有范围内补做一次恢复检查。",
    run_number: int = 1,
) -> object:
    feedback = {
        "schema_version": "task-feedback.v1",
        "feedback_id": "FB-001",
        "requirement_id": "REQ-001",
        "task_id": "T-001",
        "run_number": run_number,
        "source": {
            "type": "user",
            "received_at": "2026-07-23T23:00:00+08:00",
        },
        "content": content,
        "affected_refs": [],
        "changes_contract": False,
    }
    source_file, digest = _write_evidence(
        project,
        requirement_root,
        name="feedback.json",
        content=json.dumps(feedback, ensure_ascii=False),
        run_number=run_number,
    )
    recorded = _cli(
        project,
        [
            "task-evidence",
            "REQ-001",
            "T-001",
            "--kind",
            "feedback",
            "--source-file",
            source_file,
            "--sha256",
            digest,
        ],
    )
    assert recorded.returncode == 0, recorded.stderr
    return recorded


def _complete_current_task(
    project: Path,
    requirement_root: Path,
    *,
    infer: bool = False,
    with_feedback: bool = False,
    run_number: int = 1,
) -> object:
    _register_test_evidence(
        project,
        requirement_root,
        run_number=run_number,
    )
    _register_manual_evidence(
        project,
        requirement_root,
        run_number=run_number,
    )
    if with_feedback:
        _register_feedback_evidence(
            project,
            requirement_root,
            run_number=run_number,
        )
    done_args = ["task-done"] if infer else ["task-done", "REQ-001", "T-001"]
    done = _cli(project, done_args)
    assert done.returncode == 0, done.stderr
    assert "运行状态：closed" in done.stdout
    assert _task_status(project) == "done"
    return done


def _approve_current_task_plan(project: Path) -> None:
    """fix/audit 会生成新的正式任务计划；测试通过正式审核服务使它可开工。"""

    status = review_service.task_plan_review_status(
        build_paths(project),
        requirement_id="REQ-001",
    )
    current = next(item for item in status["reviews"] if item["is_current"])
    request = {
        "review_id": current["review_id"],
        "input_hashes": current["input_hashes"],
    }
    old_thread = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = "独立任务审核任务"
    try:
        review_service.submit_review(
            build_paths(project),
            request_id=str(request["review_id"]),
            submission=_submission(request, status="passed", issues=[]),
        )
    finally:
        if old_thread is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = old_thread


def test_task_change_report_quality_requires_new_developer_sections(tmp_path: Path) -> None:
    report_path = write_change_report(tmp_path)
    report_text = report_path.read_text(encoding="utf-8")
    assert task_change_report_quality_issues(
        report_text, {"title": "增加订单导出按钮"}
    ) == []

    thin_report = report_text.replace("## 文件职责变化\n", "").replace(
        "## 反馈逐条处理结果\n", ""
    )
    issues = task_change_report_quality_issues(
        thin_report, {"title": "增加订单导出按钮"}
    )

    assert "缺少章节：## 文件职责变化" in issues
    assert "缺少章节：## 反馈逐条处理结果" in issues


def test_task_change_report_quality_error_points_to_template(tmp_path: Path) -> None:
    thin_report = "# T-001 任务变更报告\n\n## 一句话结论\n只写一句话是不够的。\n"

    with pytest.raises(Exception) as error:
        validate_change_report_text(thin_report, {"title": "增加订单导出按钮"})

    message = str(error.value)
    assert "任务变更报告质量检查未通过" in message
    assert "--change-report-template" in message
    assert "/tmp/sdlc-change-report-T-001.md" in message


def test_route_change_report_does_not_require_stateful_ui_details(tmp_path: Path) -> None:
    report_text = write_change_report(
        tmp_path, title="阅读模式入口跳转"
    ).read_text(encoding="utf-8")
    task = {
        "title": "补齐设置页阅读模式入口跳转",
        "summary": "点击入口进入已有阅读模式设置页",
        "note": "本任务只处理入口展示和跳转，不调整目标页内部样式。",
    }
    assert task_change_report_quality_issues(report_text, task) == []


def test_task_change_report_quality_accepts_mjs_paths(tmp_path: Path) -> None:
    report_path = write_change_report(tmp_path, title="设置页右侧回显")
    report_text = report_path.read_text(encoding="utf-8").replace(
        "src/order_export.ts", "src/settings_display.mjs"
    )
    report_text += "\n- 可重复测试脚本：`tests/check-settings-display.mjs`。\n"
    assert task_change_report_quality_issues(
        report_text, {"title": "设置页右侧回显"}
    ) == []


def test_run_test_scripts_uses_node_for_non_executable_mjs(tmp_path: Path) -> None:
    script = tmp_path / "tests" / "check-settings-display.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log('settings display ok');\n", encoding="utf-8")

    success, outputs = run_test_scripts(
        tmp_path, ["tests/check-settings-display.mjs"]
    )

    assert success
    assert "settings display ok" in "\n".join(outputs)


def test_task_done_blocks_changed_requirement_mjs_until_test_script_registered(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    script = requirement_root / "tests/check-settings-display.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("console.log('正式脚本原始输出');\n", encoding="utf-8")

    blocked = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert blocked.returncode == 1
    assert "规定测试还没有全部通过" in blocked.stderr
    script.unlink()
    _register_test_evidence(project, requirement_root)
    _register_manual_evidence(project, requirement_root)
    done = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert done.returncode == 0, done.stderr


def test_task_done_ignores_project_tests_dir_and_non_mjs_requirement_files(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    (project / "tests").mkdir(exist_ok=True)
    (project / "tests/project-only.mjs").write_text(
        "console.log('普通项目脚本');\n", encoding="utf-8"
    )
    note = requirement_root / "tests/check-note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("普通说明文件。\n", encoding="utf-8")
    # 这些文件在开工前已经存在，当前轮次只核对开工后的实际变化。
    _start_and_confirm(project, requirement_root)
    done = _complete_current_task(project, requirement_root)
    assert "任务已完成" in done.stdout


def test_task_done_can_replace_or_clear_registered_test_scripts(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(
        project, requirement_root, result="failed", exit_code=1
    )
    failed = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert failed.returncode == 1
    assert _task_status(project) == "test_failed"
    restored = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "改用修复后的正式测试证据"],
    )
    assert restored.returncode == 0, restored.stderr
    assert "运行轮次：0002" in restored.stdout
    _start_and_confirm_after_restore(project, requirement_root, run_number=2)
    _register_test_evidence(project, requirement_root, run_number=2)
    _register_manual_evidence(project, requirement_root, run_number=2)
    done = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert done.returncode == 0, done.stderr
    old_run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert old_run["test_records"][0]["result"] == "failed"


def _start_and_confirm_after_restore(
    project: Path,
    requirement_root: Path,
    *,
    run_number: int,
) -> None:
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    assert current["run_number"] == run_number
    confirmed = _cli(
        project,
        [
            "task-read-confirm",
            "REQ-001",
            "T-001",
            "--manifest-sha256",
            str(current["read_manifest_sha256"]),
        ],
    )
    assert confirmed.returncode == 0, confirmed.stderr


def test_task_and_task_done_can_infer_current_task_and_run_tests(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    started, _confirmed = _start_and_confirm(
        project, requirement_root, infer=True
    )
    assert "REQ-001 / T-001" in started.stdout
    done = _complete_current_task(
        project, requirement_root, infer=True
    )
    assert "证据数量：测试 1 条，人工验收 1 条" in done.stdout


def test_task_done_summary_shows_recorded_command_without_auto_test(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(project, requirement_root)
    run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert run["test_records"][0]["command"] == (
        "python3 -m pytest -q tests/test_contract.py"
    )


def test_task_start_brief_preserves_explicit_checks_without_text_classification(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    started = _cli(project, ["task", "REQ-001", "T-001"])
    assert started.returncode == 0, started.stderr
    manifest = json.loads(
        (_run_root(requirement_root) / "task-read-manifest.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["task_file"].endswith("tasks/T-001.md")
    assert {item["id"] for item in manifest["references"]} >= {
        "FR-001",
        "AC-001",
    }
    assert "完整读取清单" in started.stdout


def test_task_start_can_record_reviewed_test_plan_and_task_done_uses_it(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    task_before = json.loads(
        (requirement_root / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(project, requirement_root)
    _register_manual_evidence(project, requirement_root)
    done = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert done.returncode == 0, done.stderr
    task_after = json.loads(
        (requirement_root / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    assert task_after["automated_tests"] == task_before["automated_tests"]
    assert task_after["manual_checks"] == task_before["manual_checks"]


def test_task_done_can_replace_wrong_test_command_and_drop_old_one(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(
        project, requirement_root, result="failed", exit_code=1
    )
    assert _cli(project, ["task-done", "REQ-001", "T-001"]).returncode == 1
    assert _task_status(project) == "test_failed"
    restored = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "失败命令已修复"],
    )
    assert restored.returncode == 0, restored.stderr
    _start_and_confirm_after_restore(project, requirement_root, run_number=2)
    _complete_current_task(project, requirement_root, run_number=2)
    new_run = json.loads(
        (_run_root(requirement_root, run_number=2) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert all(record["result"] == "passed" for record in new_run["test_records"])


def test_task_done_can_clear_wrong_test_commands_and_wait_for_user_check(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(
        project, requirement_root, result="failed", exit_code=1
    )
    assert _cli(project, ["task-done", "REQ-001", "T-001"]).returncode == 1
    restored = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "重新执行而不是覆盖旧证据"],
    )
    assert restored.returncode == 0, restored.stderr
    new_run = json.loads(
        (_run_root(requirement_root, run_number=2) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert new_run["test_records"] == []
    assert json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )["test_records"][0]["result"] == "failed"


def test_task_done_auto_commits_related_task_files_after_tests_pass(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    old_head = run_git(["rev-parse", "HEAD"], cwd=project).stdout.strip()
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    done = _complete_current_task(project, requirement_root)
    assert "实际修改范围：" in done.stdout
    assert "src/app.py" in done.stdout
    # 直接任务主线只关闭任务轮次，不再悄悄恢复旧流程的自动提交副作用。
    assert run_git(["rev-parse", "HEAD"], cwd=project).stdout.strip() == old_head


def test_task_done_can_write_change_report_template_without_closing_task(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    template = project / "任务变更报告.md"
    result = _cli(
        project,
        [
            "task-done",
            "REQ-001",
            "T-001",
            "--change-report-template",
            str(template),
        ],
    )
    assert result.returncode == 1
    assert not template.exists()
    assert _task_status(project) == "doing"


def test_task_done_archives_codex_written_change_report(tmp_path: Path) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert run["status"] == "closed"
    assert (_run_root(requirement_root) / "evidence/manual.json").is_file()


def test_task_done_deletes_tmp_change_report_after_archive(tmp_path: Path) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    report = project / "临时变更报告.md"
    _complete_current_task(project, requirement_root)
    assert not report.exists()
    assert (_run_root(requirement_root) / "evidence/test-passed.log").is_file()


def test_task_done_refuses_code_change_without_codex_change_report(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    refused = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert refused.returncode == 1
    assert "规定测试还没有全部通过" in refused.stderr
    assert _task_status(project) == "doing"


def test_task_done_ignores_stale_legacy_task_pack(tmp_path: Path) -> None:
    project, requirement_root = _direct_project(tmp_path)
    assert not (requirement_root / "task-packs").exists()
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    assert not (requirement_root / "task-packs").exists()


def test_task_done_allows_current_task_related_file_changes_after_start(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    (project / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
    checked = _cli(project, ["task-run-check", "REQ-001", "T-001"])
    assert checked.returncode == 0, checked.stderr
    done = _complete_current_task(project, requirement_root)
    assert "src/app.py" in done.stdout


def test_legacy_task_done_does_not_read_task_pack_snapshot_after_card_change(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    manifest_before = (
        _run_root(requirement_root) / "task-read-manifest.v1.json"
    ).read_bytes()
    _complete_current_task(project, requirement_root)
    assert (
        _run_root(requirement_root) / "task-read-manifest.v1.json"
    ).read_bytes() == manifest_before
    assert not (requirement_root / "task-packs").exists()


def test_task_done_reclosing_keeps_committed_change_detail_in_brief(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    run_path = _run_root(requirement_root) / "task-run.v1.json"
    before = run_path.read_bytes()
    repeated = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert repeated.returncode == 1
    assert run_path.read_bytes() == before


def test_task_done_failure_marks_task_failed_and_keeps_verification_empty(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(
        project, requirement_root, result="failed", exit_code=1
    )
    done = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert done.returncode == 1
    assert _task_status(project) == "test_failed"
    run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert run["verification_records"] == []


def test_task_done_skill_requires_commit_after_confirmed_task_completion() -> None:
    skill_text = (SDLC_SKILLS_HOME / "sdlc-task-done/SKILL.md").read_text(
        encoding="utf-8"
    )
    next_skill_text = (SDLC_SKILLS_HOME / "sdlc-next/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "task-run.v1.json" in skill_text
    assert "task-read-manifest.v1.json" in skill_text
    assert "task-evidence" in skill_text
    assert "整数退出码" in skill_text
    assert "人工验收缺失" in skill_text
    assert "任务为 `done`" in skill_text
    assert "不自动开始下一任务" in skill_text
    assert "ready_for_development" in next_skill_text
    assert "verifying" in next_skill_text


def test_task_help_hides_legacy_done_flag(tmp_path: Path) -> None:
    project = init_demo_repo(tmp_path)
    help_result = run_cli_base(["task", "--help"], cwd=project)
    assert help_result.returncode == 0
    assert "--done" not in help_result.stdout
    assert "task-done" not in help_result.stdout


def test_task_skill_requires_test_plan_review_before_implementation() -> None:
    skills_home = Path(__file__).resolve().parents[1] / "skills"
    skill_text = (skills_home / "sdlc-task/SKILL.md").read_text(encoding="utf-8")
    assert "已通过当前 `task_plan` 审核" in skill_text
    assert "task-read-manifest.v1.json" in skill_text
    assert "当前运行轮次" in skill_text
    assert "路径、定位和 SHA-256 只用于找到原文" in skill_text
    assert "task-read-confirm" in skill_text
    assert "确认当前轮次从 `reading` 进入 `active`" in skill_text
    assert "`allowed_output_paths`" in skill_text
    assert "测试、人工验收、现场证据和反馈都绑定当前运行轮次" in skill_text


def test_task_done_skill_requires_comment_pass_before_completion() -> None:
    skill_text = (SDLC_SKILLS_HOME / "sdlc-task-done/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "执行全部自动测试、可重复脚本和人工验收" in skill_text
    assert "任务有 FR、AC、TC 时逐项记录" in skill_text
    assert "任一测试失败" in skill_text


def test_global_agents_allows_explicit_sdlc_task_without_template_confirm() -> None:
    agents_text = (Path.home() / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "用户已经给出明确执行指令时，直接按指令推进" in agents_text
    assert "明确文件修改、明确命令执行，都视为已经授权当前动作" in agents_text
    assert "不要再做模板化二次确认" in agents_text
    assert "不要固定追问“是否还有补充”" in agents_text
    assert "任何任务开始前，都要使用【问题提问工具】不断向用户发问" not in agents_text


def test_task_done_without_test_command_waits_for_user_check(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    done = _cli(project, ["task-done", "REQ-001", "T-001"])
    assert done.returncode == 1
    assert "规定测试还没有全部通过" in done.stderr
    assert _task_status(project) == "doing"


def test_task_done_await_user_check_runs_tests_but_does_not_finish_or_commit(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(project, requirement_root)
    done = _cli(
        project,
        ["task-done", "REQ-001", "T-001", "--await-user-check"],
    )
    assert done.returncode == 1
    assert "人工验收" in done.stderr
    assert _task_status(project) == "doing"


def test_next_keeps_ready_for_user_check_without_reopening_legacy_task(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    before = _task_status(project)
    result = _cli(project, ["next"])
    assert result.returncode == 0, result.stderr
    assert _task_status(project) == before == "doing"
    assert "task-evidence" in result.stdout or "task-run-check" in result.stdout


def _assert_legacy_add_rejected_without_state_change(
    tmp_path: Path,
    state: str,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    started = _cli(project, ["task", "REQ-001", "T-001"])
    assert started.returncode == 0, started.stderr
    if state != "reading":
        _start_and_confirm_after_restore(project, requirement_root, run_number=1)
    if state == "test_failed":
        _register_test_evidence(
            project, requirement_root, result="failed", exit_code=1
        )
        assert _cli(project, ["task-done", "REQ-001", "T-001"]).returncode == 1
    before = _task_status(project)
    rejected = _cli(project, ["add", "自然语言变化"])
    assert rejected.returncode != 0
    assert _task_status(project) == before


def test_legacy_add_change_rejection_keeps_ready_task_state(tmp_path: Path) -> None:
    _assert_legacy_add_rejected_without_state_change(tmp_path, "reading")


def test_legacy_add_change_rejection_keeps_doing_task_state(tmp_path: Path) -> None:
    _assert_legacy_add_rejected_without_state_change(tmp_path, "active")


def test_legacy_add_change_rejection_keeps_test_failed_task_state(
    tmp_path: Path,
) -> None:
    _assert_legacy_add_rejected_without_state_change(tmp_path, "test_failed")


def test_legacy_change_rejection_keeps_current_task_binding_unchanged(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    current_path = requirement_root / "runtime/T-001/current.json"
    before = current_path.read_bytes()
    rejected = _cli(project, ["change", "REQ-001", "自然语言变化"])
    assert rejected.returncode != 0
    assert current_path.read_bytes() == before


def test_task_refuses_to_restart_ready_for_user_check_task(tmp_path: Path) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    restarted = _cli(project, ["task", "REQ-001", "T-001"])
    assert restarted.returncode == 1
    assert "doing" in restarted.stderr


def test_next_keeps_test_failed_state_without_reopening_legacy_task(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _register_test_evidence(
        project, requirement_root, result="failed", exit_code=1
    )
    assert _cli(project, ["task-done", "REQ-001", "T-001"]).returncode == 1
    result = _cli(project, ["next"])
    assert result.returncode == 0, result.stderr
    assert _task_status(project) == "test_failed"


def test_task_done_can_infer_ready_for_user_check_and_record_manual_verification(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root, infer=True)
    _register_test_evidence(project, requirement_root)
    _register_manual_evidence(project, requirement_root)
    done = _cli(project, ["task-done"])
    assert done.returncode == 0, done.stderr
    assert "人工验收 1 条" in done.stdout


def test_task_restore_infers_recent_task_and_records_feedback(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root, with_feedback=True)
    restored = _cli(project, ["task-restore", "补做现有范围内的恢复检查"])
    assert restored.returncode == 0, restored.stderr
    assert "运行轮次：0002" in restored.stdout
    old_run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(old_run["feedback_records"]) == 1


def test_task_restore_refuses_historical_done_task_after_later_progress(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    first = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "第一次恢复"],
    )
    assert first.returncode == 0, first.stderr
    rejected = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "不同原因不能复用当前轮次"],
    )
    assert rejected.returncode == 1
    assert _run_root(requirement_root, run_number=2).is_dir()
    assert not _run_root(requirement_root, run_number=3).exists()


def test_task_restore_allows_latest_done_task_when_later_queue_has_old_progress(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    restored = _cli(
        project,
        ["task-restore", "REQ-001", "T-001", "恢复最近完成任务"],
    )
    assert restored.returncode == 0, restored.stderr
    assert _task_status(project) == "doing"
    assert json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )["run_number"] == 2


def test_task_pause_moves_current_doing_task_back_to_todo(tmp_path: Path) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    paused = _cli(
        project,
        [
            "task-pause",
            "REQ-001",
            "T-001",
            "--reason",
            "误启动，先回到任务规划",
        ],
    )
    assert paused.returncode == 0, paused.stderr
    assert _task_status(project) == "todo"
    old_run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert old_run["status"] == "stale"


def test_fix_inserts_repair_task_without_reopening_completed_source_task(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    fixed = _cli(
        project,
        ["fix", "REQ-001", "T-001", "修复回归发现的问题"],
    )
    assert fixed.returncode == 0, fixed.stderr
    requirement = _requirement(project)
    assert requirement["tasks"][0]["status"] == "done"
    repair = requirement["tasks"][-1]
    assert repair["status"] == "todo"
    assert isinstance(repair.get("task_contract"), dict)
    assert repair["task_id"] != "T-001"


def test_audit_inserts_quality_review_task_and_pauses_current_work(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    audited = _cli(
        project,
        ["audit", "REQ-001", "T-001", "--note", "复查正式任务质量"],
    )
    assert audited.returncode == 0, audited.stderr
    requirement = _requirement(project)
    assert requirement["tasks"][0]["status"] == "done"
    audit_task = requirement["tasks"][-1]
    assert audit_task["status"] == "todo"
    assert isinstance(audit_task.get("task_contract"), dict)


def test_plan_add_task_reports_created_task_id_and_reorder_blocks_finished_tasks(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    before = [item["task_id"] for item in _requirement(project)["tasks"]]
    old_plan = _cli(
        project,
        [
            "plan-add-task",
            "REQ-001",
            "旧入口追加任务",
            "--coverage",
            "FR-001",
        ],
    )
    assert old_plan.returncode == 1
    assert [item["task_id"] for item in _requirement(project)["tasks"]] == before
    fixed = _cli(
        project,
        ["fix", "REQ-001", "T-001", "通过直接修复入口追加任务"],
    )
    assert fixed.returncode == 0, fixed.stderr
    assert len(_requirement(project)["tasks"]) == len(before) + 1


def test_accept_infers_done_requirement_and_blocks_unfinished_work(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    blocked = _cli(project, ["accept"])
    assert blocked.returncode == 1
    assert "未完成" in blocked.stderr or "不能验收" in blocked.stderr
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    after_done = _cli(project, ["accept"])
    assert "未完成任务" not in after_done.stderr


def test_task_without_context_asks_user_when_multiple_tasks_can_start(
    tmp_path: Path,
) -> None:
    project, requirement_root = _direct_project(tmp_path)
    _start_and_confirm(project, requirement_root)
    _complete_current_task(project, requirement_root)
    assert _cli(
        project,
        ["fix", "REQ-001", "T-001", "第一项修复"],
    ).returncode == 0
    assert _cli(
        project,
        ["audit", "REQ-001", "T-001", "--note", "并行质量复查"],
    ).returncode == 0
    _approve_current_task_plan(project)
    candidates = [
        item for item in _requirement(project)["tasks"] if item["status"] == "todo"
    ]
    assert len(candidates) >= 2
    selected = _cli(project, ["task"])
    assert selected.returncode == 1
    assert "多个" in selected.stderr or "明确" in selected.stderr
