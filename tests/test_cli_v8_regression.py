from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "contracts"))

import pytest

from codex_sdlc.commands.docs_cmd import auto_commit_docs_file
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.services import review_service
from test_cli_v1 import (
    SDLC_SKILLS_HOME,
    read_events,
    run_cli as run_cli_base,
    run_cli_raw,
    run_git,
)
from test_contract_cli_regressions import (
    _ready_project as prepare_document_first_project,
)
from test_task_plan_review_flow import _submission
from test_task_planning_code_evidence import _write_task_submission


SHARED_TEST_ROOT = (
    Path(tempfile.gettempdir()) / f"codex-sdlc-v8-regression-{os.getpid()}"
)
DOCUMENT_FIRST_BASELINE = SHARED_TEST_ROOT / "document-first-baseline"
FORMAL_TASK_BASELINES = SHARED_TEST_ROOT / "formal-task-baselines"


@pytest.fixture(scope="session", autouse=True)
def cleanup_shared_test_root():
    """整文件共享正式建档基准，但无论成功或失败都不能把夹具留在临时目录。"""

    yield
    shutil.rmtree(SHARED_TEST_ROOT, ignore_errors=True)


def run_cli(
    args: list[str],
    *,
    cwd: Path,
    thread_id: str = "任务开发线程",
):
    """所有任务动作都从正式 bin 入口执行，并保持同一任务线程身份。"""

    return run_cli_base(
        args,
        cwd=cwd,
        extra_env={
            "CODEX_THREAD_ID": thread_id,
            "CODEX_SDLC_DISABLE_AUTO_BACKUP": "1",
        },
    )


def _set_thread_id(value: str) -> str | None:
    previous = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = value
    return previous


def _restore_thread_id(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("CODEX_THREAD_ID", None)
    else:
        os.environ["CODEX_THREAD_ID"] = previous


def prepare_formal_task_project(
    tmp_path: Path,
    *,
    task_title: str = "增加订单导出按钮",
) -> tuple[Path, Path]:
    """复用完整 document-first 建档基准，再导入并审核当前 task.v2。

    这组回归关注任务运行、证据和需求级回归，不重复测试需求与设计审核的
    细节；共享基准只减少准备时间，每个用例仍从同一正式建档结果重新复制。
    """

    del tmp_path
    project = SHARED_TEST_ROOT / "demo-project"
    shutil.rmtree(project, ignore_errors=True)
    SHARED_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    formal_baseline = FORMAL_TASK_BASELINES / task_title
    if formal_baseline.is_dir():
        shutil.copytree(formal_baseline, project)
        requirement_dir = first_requirement_dir(project)
        assert (requirement_dir / "tasks/T-001.json").is_file()
        assert (requirement_dir / "tasks/T-001.md").is_file()
        assert not (requirement_dir / "task-packs").exists()
        return project, requirement_dir

    if DOCUMENT_FIRST_BASELINE.is_dir():
        shutil.copytree(DOCUMENT_FIRST_BASELINE, project)
    else:
        patcher = pytest.MonkeyPatch()
        try:
            prepared_project, _paths, package = prepare_document_first_project(
                SHARED_TEST_ROOT, patcher
            )
        finally:
            patcher.undo()
        assert prepared_project == project
        package_path = project / "当前正式需求包.json"
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        started = run_cli_raw(["start", "--file", str(package_path)], cwd=project)
        assert started.returncode == 0, started.stderr
        # document-first 设计审核已经把 AGENTS.md 纳入输入哈希，任务准备必须
        # 继续使用原文件；覆盖它会让后续 accept 正确判定上游已经漂移。
        assert (project / "AGENTS.md").is_file()
        (project / "pyproject.toml").write_text(
            "[project]\nname='v8-regression'\nversion='0.1.0'\n",
            encoding="utf-8",
        )
        # 夹具仓库会继承全局忽略规则；强制纳入任务规划实际读取的依赖文件，
        # 让 task-run 的基线哈希覆盖它，而不是留下未跟踪例外。
        assert (
            run_git(["add", "-f", "pyproject.toml"], cwd=project).returncode == 0
        )
        committed = run_git(
            ["commit", "-m", "建立任务回归正式基准"],
            cwd=project,
        )
        assert committed.returncode == 0, committed.stderr
        shutil.copytree(project, DOCUMENT_FIRST_BASELINE)

    requirement_dir = first_requirement_dir(project)
    submission = _write_task_submission(
        project / "当前任务输入",
        requirement_dir,
    )
    plan_path = submission[0]
    plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_document["code_evidence"]["dependencies"] = [
        "package-lock.json",
        "pyproject.toml",
    ]
    plan_path.write_text(
        json.dumps(plan_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    task_path = submission[1] / "main.task.v2.json"
    task_document = json.loads(task_path.read_text(encoding="utf-8"))
    task_document["title"] = task_title
    task_document["goal"] = f"交付可复核的{task_title}结果。"
    task_document["design_refs"] = [
        "API-001",
        "COMP-001",
        "DATA-001",
        "PAGE-001",
        "SAFE-001",
    ]
    task_document["automated_tests"] = [f"运行{task_title}正式合同测试。"]
    task_document["manual_checks"] = [f"人工确认{task_title}符合验收标准。"]
    task_path.write_text(
        json.dumps(task_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    coverage_path = submission[2]
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    # 当前正式设计包同时包含数据、接口、页面、组件和安全产物；单任务夹具必须
    # 显式承担每份产物，避免沿用只覆盖 DATA-001 的早期窄夹具。
    coverage["design_artifacts"] = {
        artifact_id: {"tasks": ["@client:main"]}
        for artifact_id in (
            "API-001",
            "COMP-001",
            "DATA-001",
            "PAGE-001",
            "SAFE-001",
        )
    }
    coverage_path.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    imported = run_cli(
        [
            "tasks",
            "REQ-001",
            "--plan-file",
            str(submission[0]),
            "--tasks-dir",
            str(submission[1]),
            "--coverage-file",
            str(submission[2]),
        ],
        cwd=project,
        thread_id="任务计划生产任务",
    )
    assert imported.returncode == 0, imported.stderr

    previous = _set_thread_id("任务计划生产任务")
    try:
        request = review_service.create_review(
            build_paths(project),
            review_id="REV-999",
            stage="task_plan",
            owner_id="REQ-001",
            input_paths=["src/app.py"],
            required_checks=["测试调用方不能缩减固定审核项"],
        )["request"]
        os.environ["CODEX_THREAD_ID"] = "独立任务审核任务"
        review_service.submit_review(
            build_paths(project),
            request_id=str(request["review_id"]),
            submission=_submission(request, status="passed", issues=[]),
        )
    finally:
        _restore_thread_id(previous)

    assert (requirement_dir / "tasks/T-001.json").is_file()
    assert (requirement_dir / "tasks/T-001.md").is_file()
    assert not (requirement_dir / "task-packs").exists()
    # 任务导入和独立审核是准备阶段最重的正式动作；按任务标题保存已审核基准，
    # 后续用例复制后仍从 todo 开始，各自的 task-run 和证据不会互相污染。
    formal_baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project, formal_baseline)
    return project, requirement_dir


def first_requirement_dir(project: Path) -> Path:
    return next((project / ".codex-sdlc/requirements").glob("REQ-001*"))


def current_task(project: Path) -> dict[str, object]:
    requirement = derive_state(build_paths(project))["requirements"]["REQ-001"]
    return next(
        task for task in requirement["tasks"] if task["task_id"] == "T-001"
    )


def task_status(
    project: Path,
    requirement_id: str = "REQ-001",
    task_id: str = "T-001",
) -> str:
    requirement = derive_state(build_paths(project))["requirements"][requirement_id]
    return str(
        next(task for task in requirement["tasks"] if task["task_id"] == task_id)[
            "status"
        ]
    )


def run_root(requirement_dir: Path, run_number: int = 1) -> Path:
    return requirement_dir / "runtime/T-001/runs" / f"{run_number:04d}"


def start_and_confirm(project: Path, requirement_dir: Path) -> None:
    started = run_cli(["task", "REQ-001", "T-001"], cwd=project)
    assert started.returncode == 0, started.stderr
    assert "运行状态：reading" in started.stdout
    current = json.loads(
        (requirement_dir / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    confirmed = run_cli(
        [
            "task-read-confirm",
            "REQ-001",
            "T-001",
            "--manifest-sha256",
            str(current["read_manifest_sha256"]),
        ],
        cwd=project,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "运行状态：active" in confirmed.stdout


def write_evidence_source(
    project: Path,
    requirement_dir: Path,
    *,
    name: str,
    content: str,
) -> tuple[Path, str, str]:
    source = run_root(requirement_dir) / "evidence" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    return source, source.relative_to(project).as_posix(), sha256_file(source)


def register_test_evidence(
    project: Path,
    requirement_dir: Path,
    *,
    kind: str = "test",
    name: str = "test.log",
    content: str = "1 passed\n",
    command: str = "python3 -m pytest -q tests/test_contract.py",
    result: str = "passed",
    exit_code: int = 0,
):
    source, relative, digest = write_evidence_source(
        project,
        requirement_dir,
        name=name,
        content=content,
    )
    task = current_task(project)
    recorded = run_cli(
        [
            "task-evidence",
            "REQ-001",
            "T-001",
            "--kind",
            kind,
            "--source-file",
            relative,
            "--sha256",
            digest,
            "--command",
            command,
            "--exit-code",
            str(exit_code),
            "--result",
            result,
            "--test-item",
            str(task["test_items"][0]),
        ],
        cwd=project,
    )
    assert recorded.returncode == 0, recorded.stderr
    assert digest in recorded.stdout
    return recorded, source, digest


def register_manual_evidence(project: Path, requirement_dir: Path):
    task = current_task(project)
    document = {
        "environment": {
            "system": "仓库外临时 Git 项目",
            "entry": str(Path(__file__).resolve().parents[1] / "bin" / "codex-sdlc"),
        },
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
    _source, relative, digest = write_evidence_source(
        project,
        requirement_dir,
        name="manual.json",
        content=json.dumps(document, ensure_ascii=False),
    )
    recorded = run_cli(
        [
            "task-evidence",
            "REQ-001",
            "T-001",
            "--kind",
            "verification",
            "--source-file",
            relative,
            "--sha256",
            digest,
            "--command",
            "人工逐项验收",
            "--exit-code",
            "0",
            "--result",
            "passed",
        ],
        cwd=project,
    )
    assert recorded.returncode == 0, recorded.stderr
    return recorded


def complete_task(
    project: Path,
    requirement_dir: Path,
    *,
    kind: str = "test",
    evidence_name: str = "test.log",
    evidence_content: str = "1 passed\n",
    evidence_command: str = "python3 -m pytest -q tests/test_contract.py",
):
    start_and_confirm(project, requirement_dir)
    register_test_evidence(
        project,
        requirement_dir,
        kind=kind,
        name=evidence_name,
        content=evidence_content,
        command=evidence_command,
    )
    if kind != "test":
        # script 是可复用执行证据，不能冒充任务规定测试的结果；另登记一条
        # test 证据，保证 task-done 同时核对脚本来源和正式测试项。
        register_test_evidence(
            project,
            requirement_dir,
            name="required-test.log",
            content="1 passed\n",
            command="python3 -m pytest -q tests/test_contract.py",
        )
    register_manual_evidence(project, requirement_dir)
    completed = run_cli(["task-done", "REQ-001", "T-001"], cwd=project)
    assert completed.returncode == 0, completed.stderr
    assert "运行状态：closed" in completed.stdout
    assert task_status(project) == "done"
    return completed


def run_regression(project: Path):
    result = run_cli(["regression", "REQ-001", "T-001"], cwd=project)
    assert result.returncode == 0, result.stderr
    assert "回归读取第 1 次任务运行证据通过" in result.stdout
    return result


def accept_requirement(project: Path):
    run_regression(project)
    accepted = run_cli(["accept", "REQ-001"], cwd=project)
    assert accepted.returncode == 0, accepted.stderr
    assert "已接受需求：REQ-001" in accepted.stdout
    return accepted


def write_change_report(
    project: Path,
    task_id: str = "T-001",
    title: str = "订单导出按钮",
) -> Path:
    """历史只读文档用例自己准备报告，不依赖任务自动化测试的辅助函数。"""

    path = project / f"{task_id}-change-report.md"
    path.write_text(
        "\n".join(
            [
                f"# {task_id} 任务变更报告",
                "",
                "## 一句话结论",
                f"{title}已经按正式任务合同完成。",
                "",
                "## 文件职责变化",
                "- `src/app.py` 负责当前任务要求的用户可见结果。",
                "",
                "## 改动是怎么串起来的",
                "- 正式 task-run 保存测试和人工验收证据。",
                "",
                "## 反馈逐条处理结果",
                "- 没有改变需求或设计的反馈。",
                "",
                "## 验收结果",
                "- AC-001 与正式测试证据均通过。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_task_can_record_repeatable_script_and_materialize_tests_readme(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    start_and_confirm(project, requirement_dir)
    script = run_root(requirement_dir) / "evidence/check_order_export.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nprintf 'script ok\\n'\n", encoding="utf-8")
    script.chmod(0o755)
    executed = subprocess.run(
        [str(script)],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert executed.returncode == 0
    recorded, _source, _digest = register_test_evidence(
        project,
        requirement_dir,
        kind="script",
        name=script.name,
        content=script.read_text(encoding="utf-8"),
        command=script.relative_to(project).as_posix(),
    )

    run = json.loads(
        (run_root(requirement_dir) / "task-run.v1.json").read_text(encoding="utf-8")
    )
    task_json = json.loads(
        (requirement_dir / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    task_markdown = (requirement_dir / "tasks/T-001.md").read_text(encoding="utf-8")
    assert recorded.returncode == 0
    assert run["test_records"][0]["kind"] == "script"
    assert run["test_records"][0]["source_file"].endswith(script.name)
    assert task_json["automated_tests"][0] in task_markdown
    assert not (requirement_dir / "task-packs").exists()


def test_task_done_runs_repeatable_scripts_after_test_commands(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    completed = complete_task(
        project,
        requirement_dir,
        kind="script",
        evidence_name="check_order_export.sh",
        evidence_content="#!/bin/sh\nprintf 'script ok\\n'\n",
        evidence_command="sh check_order_export.sh",
    )

    run = json.loads(
        (run_root(requirement_dir) / "task-run.v1.json").read_text(encoding="utf-8")
    )
    assert "任务已完成" in completed.stdout
    assert run["status"] == "closed"
    assert run["test_records"][0]["kind"] == "script"
    assert run["test_records"][0]["command"] == "sh check_order_export.sh"
    assert task_status(project, "REQ-001", "T-001") == "done"


def test_regression_runs_selected_task_scripts_and_records_verification(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(
        project,
        requirement_dir,
        kind="script",
        evidence_name="check_order_export.sh",
        evidence_content="regression ok\n",
        evidence_command="sh check_order_export.sh",
    )

    regression_result = run_regression(project)

    assert "本轮回归结果" in regression_result.stdout
    assert "当前范围没有剩余人工/视觉待验项" in regression_result.stdout
    assert "覆盖当前测试矩阵 T-001#automated_tests/0" in regression_result.stdout
    assert "sh check_order_export.sh" in regression_result.stdout
    verification_files = sorted((requirement_dir / "verifications").glob("VRF-*.md"))
    assert len(verification_files) == 2
    assert "回归读取第 1 次任务运行证据通过" in verification_files[-1].read_text(
        encoding="utf-8"
    )


def test_regression_keeps_manual_cases_pending_when_auto_command_passes(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(
        tmp_path,
        task_title="设置页阅读模式入口优化",
    )
    start_and_confirm(project, requirement_dir)
    register_test_evidence(project, requirement_dir)

    missing_manual = run_cli(["task-done", "REQ-001", "T-001"], cwd=project)
    active_regression = run_cli(["regression", "REQ-001", "T-001"], cwd=project)

    assert missing_manual.returncode == 1
    assert "人工验收" in missing_manual.stderr
    assert active_regression.returncode == 1
    assert "任务运行证据失败" in active_regression.stdout
    assert task_status(project) == "doing"

    register_manual_evidence(project, requirement_dir)
    completed = run_cli(["task-done", "REQ-001", "T-001"], cwd=project)
    assert completed.returncode == 0, completed.stderr
    regression_result = run_regression(project)
    assert "人工验收 1 条" in regression_result.stdout
    accept_result = run_cli(["accept", "REQ-001"], cwd=project)
    assert accept_result.returncode == 0, accept_result.stderr


def test_regression_explains_manual_visual_items_before_evidence_ids(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    start_and_confirm(project, requirement_dir)
    register_test_evidence(project, requirement_dir)

    blocked = run_cli(["task-done", "REQ-001", "T-001"], cwd=project)
    run = json.loads(
        (run_root(requirement_dir) / "task-run.v1.json").read_text(encoding="utf-8")
    )

    assert blocked.returncode == 1
    assert "人工验收还没有完整覆盖" in blocked.stderr
    assert "人工确认" in blocked.stderr
    assert run["verification_records"] == []
    assert [record["evidence_id"] for record in run["test_records"]] == ["EVD-0001"]


def test_regression_does_not_use_rejected_legacy_change_as_task_contract(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    rejected = run_cli(
        ["change", "REQ-001", "导出范围增加订单来源筛选。"],
        cwd=project,
    )

    assert rejected.returncode == 1
    assert "change-create" in rejected.stderr
    assert not any(
        event["event_type"] == "requirement_change_created"
        for event in read_events(project)
    )

    complete_task(project, requirement_dir)
    regression_result = run_regression(project)
    assert "覆盖当前测试矩阵 T-001#automated_tests/0" in regression_result.stdout


def test_regression_uses_current_test_matrix_statuses(tmp_path: Path) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)

    regression_result = run_regression(project)

    assert "覆盖当前测试矩阵 T-001#automated_tests/0" in regression_result.stdout
    assert "测试 1 条，人工验收 1 条" in regression_result.stdout
    task_document = json.loads(
        (requirement_dir / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    assert task_document["requirement_refs"] == ["FR-001"]
    assert task_document["acceptance_refs"] == ["AC-001"]
    assert set(task_document["design_refs"]) == {
        "API-001",
        "COMP-001",
        "DATA-001",
        "PAGE-001",
        "SAFE-001",
    }


def test_regression_refuses_task_with_only_deprecated_tests(tmp_path: Path) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    task_path = requirement_dir / "tasks/T-001.json"
    task_document = json.loads(task_path.read_text(encoding="utf-8"))
    task_document["automated_tests"] = []
    task_path.write_text(
        json.dumps(task_document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    started = run_cli(["task", "REQ-001", "T-001"], cwd=project)

    assert started.returncode == 1
    assert "不允许直接开工" in started.stderr
    assert not (requirement_dir / "runtime/T-001").exists()
    assert task_status(project) == "todo"


def test_next_recommends_regression_after_all_tasks_done_before_accept(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)

    next_result = run_cli(["next"], cwd=project)
    status_result = run_cli(["status"], cwd=project)

    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-regression REQ-001" in next_result.stdout
    assert "$sdlc-accept REQ-001" in next_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert "REQ-001 [verifying]" in status_result.stdout
    assert "主推荐：$sdlc-regression REQ-001" in status_result.stdout


def test_accept_then_docs_generates_requirement_maintenance_guide(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    accept_result = accept_requirement(project)

    assert "$sdlc-docs REQ-001" in accept_result.stdout
    docs_result = run_cli(["docs", "REQ-001"], cwd=project)

    assert docs_result.returncode == 0, docs_result.stderr
    assert "已生成需求维护文档" in docs_result.stdout
    assert "默认不提交 Git；只有客户明确要求时才提交维护文档。" in docs_result.stdout
    docs_file = next((project / "docs/guide").glob("*逻辑梳理.md"))
    docs_text = docs_file.read_text(encoding="utf-8")
    assert "一句话理解" in docs_text
    assert "代码入口和文件分工" in docs_text
    assert "整体逻辑线" in docs_text
    assert "增加订单导出按钮" in docs_text
    assert "task-briefs/T-001.md" not in docs_text
    assert not (requirement_dir / "task-packs").exists()


def test_docs_recommends_pending_requirement_draft_before_finish(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    accept_requirement(project)
    created = run_cli(
        ["draft", "create", "阶段02 观察分组和定时任务"],
        cwd=project,
    )
    assert created.returncode == 0, created.stderr

    docs_result = run_cli(["docs", "REQ-001"], cwd=project)
    next_result = run_cli(["next"], cwd=project)
    status_result = run_cli(["status"], cwd=project)

    assert docs_result.returncode == 0, docs_result.stderr
    assert "下一步建议：$sdlc-discuss 继续完善需求草案" in docs_result.stdout
    assert "推荐原因：DRAFT-002 还在需求讨论阶段" in docs_result.stdout
    assert "- $sdlc-finish" in docs_result.stdout
    assert "DRAFT-002 [discussing]" in next_result.stdout
    assert "DRAFT-002 [discussing]" in status_result.stdout
    assert "CAP-001：阶段02 观察分组和定时任务" not in next_result.stdout


def test_docs_only_commits_maintenance_guide_when_requested(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    accept_requirement(project)
    info_exclude = project / ".git/info/exclude"
    info_exclude.write_text(
        info_exclude.read_text(encoding="utf-8") + "\ndocs/\n",
        encoding="utf-8",
    )

    docs_result = run_cli(["docs", "REQ-001"], cwd=project)

    assert docs_result.returncode == 0, docs_result.stderr
    docs_file = next((project / "docs/guide").glob("*逻辑梳理.md"))
    relative_doc = docs_file.relative_to(project).as_posix()
    assert (
        run_git(["-c", "core.quotePath=false", "ls-files", "--", relative_doc], cwd=project)
        .stdout.strip()
        == ""
    )

    committed = run_cli(["docs", "REQ-001", "--force", "--commit"], cwd=project)

    assert committed.returncode == 0, committed.stderr
    assert "已提交维护文档：docs/guide/" in committed.stdout
    assert (
        run_git(["-c", "core.quotePath=false", "ls-files", "--", relative_doc], cwd=project)
        .stdout.strip()
        == relative_doc
    )
    docs_file.write_text(
        docs_file.read_text(encoding="utf-8") + "\n真实新内容\n",
        encoding="utf-8",
    )
    auto_committed = auto_commit_docs_file(
        project,
        docs_file,
        {"title": "课程访问需求 main", "requirement_id": "REQ-001"},
    )
    assert auto_committed["status"] == "committed"


def test_docs_keeps_current_after_legacy_natural_language_change_is_rejected(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    accept_requirement(project)
    assert run_cli(["docs", "REQ-001"], cwd=project).returncode == 0
    before = [
        event
        for event in read_events(project)
        if event["event_type"] == "requirement_docs_created"
    ]

    rejected = run_cli(
        [
            "add",
            "REQ-001",
            "业务变更：导出按钮增加格式选择。",
            "--kind",
            "change",
        ],
        cwd=project,
    )
    next_after_rejection = run_cli(["next"], cwd=project)

    assert rejected.returncode == 1
    assert "change-create" in rejected.stderr
    assert "$sdlc-docs REQ-001" not in next_after_rejection.stdout
    after = [
        event
        for event in read_events(project)
        if event["event_type"] == "requirement_docs_created"
    ]
    assert len(after) == len(before) == 1


def test_missing_docs_file_is_recommended_to_regenerate(tmp_path: Path) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    accept_requirement(project)
    assert run_cli(["docs", "REQ-001"], cwd=project).returncode == 0
    docs_file = next((project / "docs/guide").glob("*逻辑梳理.md"))
    docs_file.unlink()

    next_result = run_cli(["next"], cwd=project)
    status_result = run_cli(["status"], cwd=project)

    assert next_result.returncode == 0, next_result.stderr
    assert status_result.returncode == 0, status_result.stderr
    assert "$sdlc-docs REQ-001" in next_result.stdout
    assert "--force" not in next_result.stdout
    assert "实际文件找不到" in next_result.stdout
    assert "$sdlc-docs REQ-001" in status_result.stdout


def test_docs_for_native_requirement_uses_current_sdlc_language(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(
        tmp_path,
        task_title="在设置页接入阅读模式设置",
    )
    complete_task(project, requirement_dir)
    accept_requirement(project)

    docs_result = run_cli(["docs", "REQ-001"], cwd=project)

    assert docs_result.returncode == 0, docs_result.stderr
    docs_file = next((project / "docs/guide").glob("*逻辑梳理.md"))
    docs_text = docs_file.read_text(encoding="utf-8")
    assert "一句话理解" in docs_text
    assert "阅读模式设置" in docs_text
    assert ("外部" + "历史" + "资料") not in docs_text
    assert ("external" + "-notes/") not in docs_text
    assert not (requirement_dir / "task-packs").exists()


def test_doctor_repair_keeps_legacy_task_pack_files_read_only(
    tmp_path: Path,
) -> None:
    project, requirement_dir = prepare_formal_task_project(
        tmp_path,
        task_title="设置页阅读模式入口优化",
    )
    pack_dir = requirement_dir / "task-packs/T-001"
    pack_dir.mkdir(parents=True)
    (pack_dir / "task-pack.md").write_text(
        "# T-001 历史任务执行包\n\n只读档案，不参与当前任务运行。\n",
        encoding="utf-8",
    )
    (pack_dir / "task-pack.json").write_text(
        json.dumps(
            {
                "title": "设置页阅读模式入口优化",
                "context_files": [{"path": "missing/file.ts", "exists": "false"}],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_before = (pack_dir / "task-pack.md").read_bytes()
    json_before = (pack_dir / "task-pack.json").read_bytes()

    repaired = run_cli(["doctor-repair"], cwd=project)

    assert repaired.returncode == 0, repaired.stderr
    assert "不会自动修复，也不参与当前流程状态" in repaired.stdout
    assert (pack_dir / "task-pack.md").read_bytes() == markdown_before
    assert (pack_dir / "task-pack.json").read_bytes() == json_before
    assert not (requirement_dir / "runtime/T-001").exists()


def test_docs_keeps_legacy_task_briefs_compatible(tmp_path: Path) -> None:
    project, requirement_dir = prepare_formal_task_project(tmp_path)
    complete_task(project, requirement_dir)
    legacy_dir = requirement_dir / "task-briefs"
    legacy_dir.mkdir(parents=True)
    legacy_report = legacy_dir / "T-001.md"
    report = write_change_report(project)
    report.replace(legacy_report)
    legacy_report.write_text(
        legacy_report.read_text(encoding="utf-8")
        + "\n- 临时产物：outputs/draft.txt\n"
        + "- 临时导出：temp/export-setting-i18n-excel/data.txt\n"
        + "- 缺失文件：missing/file.ts\n",
        encoding="utf-8",
    )
    accept_requirement(project)

    docs_result = run_cli(["docs", "REQ-001"], cwd=project)

    assert docs_result.returncode == 0, docs_result.stderr
    docs_file = next((project / "docs/guide").glob("*逻辑梳理.md"))
    docs_text = docs_file.read_text(encoding="utf-8")
    assert "task-briefs/T-001.md" in docs_text
    assert "src/app.py" in docs_text
    assert "outputs/draft.txt" not in docs_text
    assert "temp/export-setting-i18n-excel/data.txt" not in docs_text
    assert "missing/file.ts" not in docs_text
    assert "增加订单导出按钮" in docs_text


def test_regression_skill_and_command_are_exposed() -> None:
    regression_skill = (
        SDLC_SKILLS_HOME / "sdlc-regression/SKILL.md"
    ).read_text(encoding="utf-8")
    task_done_skill = (
        SDLC_SKILLS_HOME / "sdlc-task-done/SKILL.md"
    ).read_text(encoding="utf-8")
    next_skill = (SDLC_SKILLS_HOME / "sdlc-next/SKILL.md").read_text(
        encoding="utf-8"
    )
    docs_skill = (SDLC_SKILLS_HOME / "sdlc-docs/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "已关闭 task-run 证据" in regression_skill
    assert "codex-sdlc regression REQ-001" in regression_skill
    assert "当前正式需求、技术方案、测试矩阵、任务覆盖" in regression_skill
    assert "当前 active task-run" in task_done_skill
    assert "task-evidence" in task_done_skill
    assert "人工验收缺失" in task_done_skill
    assert "`verifying` 只推荐需求级回归和验收" in next_skill
    assert "只输出当前一步并停止" in next_skill
    assert "$sdlc-docs REQ-001" in docs_skill
    assert "docs/guide" in docs_skill
    assert "默认不提交 Git" in docs_skill
    assert "--commit" in docs_skill
