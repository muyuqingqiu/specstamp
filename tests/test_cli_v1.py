from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import json
from datetime import datetime
from pathlib import Path
from formal_package_factory import (
    install_fixture_receipt,
    write_document_first_formal_v3_package,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_sdlc.core.command_registry import CLI_COMMAND_NAMES, EXTRA_SDLC_SKILL_NAMES, SKILL_COMMAND_NAMES
from codex_sdlc.core.state import task_contract_issues


REPO_ROOT = Path(__file__).resolve().parents[1]
SDLC_BIN = REPO_ROOT / "bin" / "codex-sdlc"
SDLC_SKILLS_HOME = REPO_ROOT / "skills"
_DOCUMENT_FIRST_READY_TEMPLATE: Path | None = None


def run_cli_raw(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # 测试默认不碰全局备份目录；需要验证自动备份时，必须明确提供独立备份目录。
    env.setdefault("CODEX_SDLC_DISABLE_AUTO_BACKUP", "1")
    if extra_env:
        env.update(extra_env)
        if "CODEX_SDLC_BACKUP_HOME" in extra_env and "CODEX_SDLC_DISABLE_AUTO_BACKUP" not in extra_env:
            env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "0"
    return subprocess.run(
        [str(SDLC_BIN), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_cli(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """旧行为测试通过统一事实工厂进入 formal.v3；严格门禁测试直接调用 run_cli_raw。"""

    strict = bool((extra_env or {}).get("CODEX_SDLC_STRICT_FACT_FIXTURE"))
    if not strict and args and args[0] == "start" and (cwd / ".codex-sdlc" / "events.jsonl").exists():
        from codex_sdlc.core import draft_lifecycle
        from codex_sdlc.core.project import build_paths
        from codex_sdlc.core.state import derive_state
        from formal_package_factory import (
            formal_business_from_draft,
            install_valid_draft_facts,
            write_formal_v3_from_draft,
        )

        state = derive_state(build_paths(cwd))
        active = [item for item in state.get("drafts", {}).values() if item.get("status") != "started"]
        draft = active[-1] if active else None
        if isinstance(draft, dict):
            assessment = draft_lifecycle.assess_draft(draft)
            structurally_ready = not (
                assessment.open_questions
                or assessment.missing_requirement_items
                or assessment.missing_design_items
                or assessment.conflicts
                or assessment.lost_facts
            ) and bool(draft.get("requirement_body") and draft.get("design_body"))
            if structurally_ready:
                if not isinstance(draft.get("model_review"), dict):
                    draft = install_valid_draft_facts(cwd, run_cli_raw, str(draft["draft_id"]))
                package_arg = ""
                if "--file" in args:
                    index = args.index("--file")
                    if index + 1 < len(args):
                        package_arg = args[index + 1]
                if package_arg:
                    package_path = Path(package_arg)
                    try:
                        data = json.loads(package_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        data = None
                    if isinstance(data, dict) and data.get("source_draft_id") == draft.get("draft_id"):
                        try:
                            write_formal_v3_from_draft(package_path, data, draft)
                        except ValueError:
                            # 攻击用例故意提供丢项或错绑包时，便利夹具不能替它修正；继续交给真实 CLI 门禁。
                            pass
                elif len(args) == 1 or "--draft" in args:
                    fixture_dir = cwd / ".fact-fixtures"
                    fixture_dir.mkdir(exist_ok=True)
                    package_path = fixture_dir / f"{draft['draft_id']}.formal.v3.json"
                    write_formal_v3_from_draft(package_path, formal_business_from_draft(draft), draft)
                    args = ["start", "--file", str(package_path)]
        # 普通成功夹具显式登记两任务回执；严格攻击测试使用 run_cli_raw，不能自动得到受信回执。
        if "--file" in args:
            package_path = Path(args[args.index("--file") + 1])
            try:
                requirement = json.loads(package_path.with_name("requirement.facts.json").read_text(encoding="utf-8"))
                design = json.loads(package_path.with_name("design.facts.json").read_text(encoding="utf-8"))
                review = json.loads(package_path.with_name("model-review.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, IndexError):
                pass
            else:
                try:
                    formal_document = json.loads(package_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    formal_document = {}
                install_fixture_receipt(
                    cwd, requirement=requirement, design=design, review=review,
                    draft_id=str(formal_document.get("source_draft_id") or "FORMAL"),
                )
    return run_cli_raw(args, cwd, extra_env)




def create_minimal_requirement_by_start_file(
    project_dir: Path,
    title: str = "修一个按钮文案",
    *,
    slug: str | None = None,
    with_tasks: bool = True,
) -> Path:
    """用真实 document-first.v1 包构造正式需求，不再走历史 facts 造数旁路。"""

    existing_requirements = sorted((project_dir / ".codex-sdlc" / "requirements").glob("REQ-*"))
    requirement_id = f"REQ-{len(existing_requirements) + 1:03d}"

    global _DOCUMENT_FIRST_READY_TEMPLATE
    if _DOCUMENT_FIRST_READY_TEMPLATE is None:
        import pytest

        contracts_dir = REPO_ROOT / "tests" / "contracts"
        if str(contracts_dir) not in sys.path:
            sys.path.insert(0, str(contracts_dir))
        from test_contract_cli_regressions import _ready_project

        template_root = Path(tempfile.mkdtemp(prefix="codex-sdlc-document-first-v1-"))
        patcher = pytest.MonkeyPatch()
        try:
            ready_project, _paths, _package = _ready_project(template_root, patcher)
        finally:
            patcher.undo()
        package_path = ready_project / "共用文档优先正式包.json"
        _written_path, package = write_document_first_formal_v3_package(
            ready_project,
            output_path=package_path,
        )
        assert package["formal_contract_version"] == "formal.v3"
        assert package["workflow_profile"] == "document-first.v1"
        started = run_cli_raw(["start", "--file", str(package_path)], cwd=ready_project)
        assert started.returncode == 0, started.stderr
        package_path.unlink(missing_ok=True)
        _DOCUMENT_FIRST_READY_TEMPLATE = template_root / "文档优先正式建档基准"
        shutil.copytree(ready_project, _DOCUMENT_FIRST_READY_TEMPLATE)

    # 正式入口和完整审核只在共用基准准备时执行一次。各用例只登记自己的
    # 业务投影，不能复制绑定了另一套 Git 身份和完成回执的正式目录。
    from codex_sdlc.core.project import build_paths
    from codex_sdlc.core.state import append_event, refresh_materialized_state

    paths = build_paths(project_dir)
    append_event(
        paths,
        event_type="requirement_created",
        source="sdlc-test-helper",
        summary=f"登记由文档优先正式基准复制的测试需求：{requirement_id}",
        requirement_id=requirement_id,
        payload={
            "title": title,
            "description": f"{title}需要按明确范围落地，并保留可复查的验收和测试口径。",
            "summary": title,
            "folder_name": f"{requirement_id}-{slug or 'minimal-requirement'}",
            "flow_type": "SDLC document-first 正式流程测试基准",
        },
    )
    # 正式建档完成回执绑定原始 requirement_created 事件，测试标题通过后续
    # 元数据事件覆盖，不能再重写已经由 start 提交的事件行。
    append_event(
        paths,
        event_type="requirement_metadata_updated",
        source="sdlc-test-helper",
        summary=f"设置测试需求标题：{title}",
        requirement_id=requirement_id,
        payload={
            "title": title,
            "summary": title,
            "description": f"{title}需要按明确范围落地，并保留可复查的验收和测试口径。",
        },
    )
    refresh_materialized_state(paths)
    repair_after_start = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_after_start.returncode in {0, 1}, repair_after_start.stderr

    if with_tasks:
        # 旧阶段已经下线；这些兼容测试直接登记所需任务事件，不能再调用不存在的公开入口。
        events_file = project_dir / ".codex-sdlc" / "events.jsonl"
        event_lines = [line for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [json.loads(line) for line in event_lines]
        existing_event_count = len(events)
        task_events = []
        task_count = 3 if ("订单导出功能" in title or "订单导出按钮" in title) else 2
        for index in range(1, task_count + 1):
            task_id = f"T-{index:03d}"
            task_events.append(
                {
                    "event_id": f"EVT-20260709-{existing_event_count + index:06d}",
                    "event_type": "task_created",
                    "project_path": str(project_dir.resolve()),
                    "requirement_id": requirement_id,
                    "task_id": task_id,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "source": "sdlc-test-helper",
                    "summary": f"创建测试任务 {task_id}",
                    "payload": {
                        "title": title if index == 1 else f"复核{title}第 {index} 项",
                        "summary": f"按正式需求处理{title}的第 {index} 个测试角度，并保留验收记录。",
                        "status": "todo",
                        "depends_on": [f"T-{index - 1:03d}"] if index > 1 else [],
                        "related_files": [],
                        "test_items": [f"验证{title}的第 {index} 个用户可见结果"],
                        "test_commands": ["npm test"] if (project_dir / "package.json").exists() else [],
                        "manual_checks": [f"人工确认{title}第 {index} 项符合验收标准"],
                        "business_rules": [f"交付内容必须严格对应正式需求：{title}。"],
                        "coverage_points": ["FR-001"],
                        "coverage_tests": ["TC-001"],
                        "feedback_contract_version": "feedback.v1",
                        "feedback_state": "none",
                        "acceptance_feedback": [],
                        "formal_gate": False,
                        "note": "测试辅助任务只用于构造后续命令需要的正式任务。",
                    },
                }
            )
        preserved_lines = [
            line
            for line, event in zip(event_lines, events)
            if event.get("event_type") != "plan_updated" or event.get("requirement_id") != requirement_id
        ]
        preserved_lines.extend(json.dumps(event, ensure_ascii=False) for event in task_events)
        events_file.write_text("\n".join(preserved_lines) + "\n", encoding="utf-8")
        repair_result = run_cli(["doctor-repair"], cwd=project_dir)
        assert repair_result.returncode == 0, repair_result.stderr

    requirement_dirs = sorted((project_dir / ".codex-sdlc" / "requirements").glob(f"{requirement_id}-*"))
    assert requirement_dirs
    return requirement_dirs[0]
def run_brief(
    project_dir: Path,
    requirement_id: str = "REQ-001",
    task_id: str = "T-001",
    *,
    auto_review: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = run_cli(["brief", requirement_id, task_id], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    if auto_review:
        # 测试辅助函数代表“完整 brief 阶段已经结束”。真实 brief 命令会先写 pending，
        # 这里补底层复核记录，是为了让旧的任务开工用例继续聚焦在 task/task-done 行为上。
        review_result = run_cli(
            [
                "brief-review",
                requirement_id,
                task_id,
                "--status",
                "passed",
                "--method",
                "rg",
                "--summary",
                "测试辅助函数已核对执行包，允许进入任务开工场景。",
            ],
            cwd=project_dir,
        )
        assert review_result.returncode == 0, review_result.stderr
    return result


def write_change_report(project_dir: Path, task_id: str = "T-001") -> Path:
    report_path = project_dir / f"{task_id}-change-report.md"
    report_path.write_text(
        "\n".join(
            [
                f"# {task_id} 任务变更报告",
                "",
                "## 一句话结论",
                "本任务完成了当前任务要求的业务改动，并把修改前后、影响和验收结果写清楚。",
                "",
                "## 修改前",
                "- 原来当前能力还没有按任务要求落地，用户无法稳定完成对应操作。",
                "",
                "## 修改后",
                "- 现在已经补齐当前任务需要的实现，任务涉及能力可以进入验收。",
                "- FR-001 已处理：本任务已按当前需求点补齐订单导出入口、权限校验和导出结果记录。",
                "",
                "## 文件职责变化",
                "- `src/order_export.ts` 是本轮任务声明的主要改动文件：原来没有稳定承接当前任务目标，现在负责把用户入口、业务处理和验收结果连起来。",
                "- 这里写清文件职责，是为了让后续维护同事可以直接从文件和验证记录理解本轮任务，不需要只看代码行数猜改动目的。",
                "",
                "## 改动是怎么串起来的",
                "- 用户从当前任务对应入口触发操作后，代码进入 `src/order_export.ts` 的处理逻辑，再由 task-done 记录命令和人工验证结果。",
                "- 后续排查时可以先看这个文件，再看验证记录和提交正文，这样能把用户可见行为、代码变化和验收结论连起来。",
                "",
                "## 关键改动说明",
                "### 1. 补齐当前任务能力",
                "- 原来：缺少本任务要求的实现。",
                "- 现在：已经按任务范围补齐。",
                "- 意义：后续可以按这条记录理解本轮代码变化。",
                "### 2. 留出维护线索",
                "- 原来：只看任务编号很难知道这次为什么改这个文件。",
                "- 现在：报告里写清了文件职责、入口链路和验收记录的位置。",
                "- 意义：同事后续接手时可以直接按这条说明定位，不需要重新整理上下文。",
                "",
                "## 反馈逐条处理结果",
                "- 本任务没有验收退回或额外反馈。",
                "",
                "## 本任务没有做什么",
                "- 没有扩展到任务范围之外的功能。",
                "",
                "## 对后续任务的影响",
                "- 当前没有明确关联的未来任务。",
                "",
                "## 验收结果",
                "- AC-001 验证通过：订单导出入口、权限校验和导出结果记录都符合当前任务验收口径。",
                "- TC-001 已执行并通过：订单导出主链路验证已记录。",
                "- TC-002 已执行并通过：权限校验分支验证已记录。",
                "- TC-003 已执行并通过：导出失败或异常提示验证已记录。",
                "- 验收结果由 task-done 写入验证记录。",
                "",
                "## 后续维护提示",
                "- 后续维护时先看本任务涉及文件和验证记录。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return report_path


def run_git(args: list[str], cwd: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def init_demo_repo(tmp_path: Path) -> Path:
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True, text=True)
    return project_dir


def read_events(project_dir: Path) -> list[dict[str, object]]:
    events_file = project_dir / ".codex-sdlc" / "events.jsonl"
    return [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_closed_task_without_coverage_does_not_block_contract_gate() -> None:
    requirement = {
        "structured": {
            "requirement_version": "requirement.v1",
            "design_version": "design.v1",
            "test_matrix_version": "test-matrix.v1",
            "requirement_points": [{"id": "FR-001"}],
        },
        "prepared_test_cases": [{"id": "TC-001", "task_id": "T-001", "status": "active"}],
    }
    task = {
        "task_id": "T-099",
        "status": "closed",
        "requirement_version": "requirement.v1",
        "design_version": "design.v1",
        "test_matrix_version": "test-matrix.v1",
        "coverage_points": [],
        "coverage_tests": [],
    }

    assert task_contract_issues(requirement, task) == []


def test_done_task_with_old_contract_does_not_block_contract_gate() -> None:
    requirement = {
        "structured": {
            "requirement_version": "requirement.v2",
            "design_version": "design.v2",
            "test_matrix_version": "test-matrix.v2",
            "requirement_points": [{"id": "FR-001"}, {"id": "FR-002"}],
        },
        "prepared_test_cases": [{"id": "TC-001", "task_id": "T-001", "status": "active"}],
    }
    task = {
        "task_id": "T-001",
        "status": "done",
        "requirement_version": "requirement.v1",
        "design_version": "design.v1",
        "test_matrix_version": "test-matrix.v1",
        "coverage_points": ["FR-001"],
        "coverage_tests": ["TC-001"],
    }

    assert task_contract_issues(requirement, task) == []


def write_events(project_dir: Path, events: list[dict[str, object]]) -> None:
    events_file = project_dir / ".codex-sdlc" / "events.jsonl"
    events_file.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_version_and_help_show_core_commands(tmp_path: Path) -> None:
    result = run_cli(["--version"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "specstamp" in result.stdout
    assert "下一步推荐" not in result.stdout

    version_result = run_cli(["version"], cwd=tmp_path)
    assert version_result.returncode == 0, version_result.stderr
    assert "specstamp" in version_result.stdout
    assert "下一步推荐" not in version_result.stdout

    help_result = run_cli(["help"], cwd=tmp_path)
    assert help_result.returncode == 0, help_result.stderr
    assert "init" in help_result.stdout
    assert "doctor" in help_result.stdout
    assert "handoff" in help_result.stdout


def test_every_command_supports_help(tmp_path: Path) -> None:
    for command_name in CLI_COMMAND_NAMES:
        result = run_cli([command_name, "--help"], cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout


def test_command_registry_matches_parser(tmp_path: Path) -> None:
    result = run_cli(["--help"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    match = re.search(r"\{([^}]+)\}", result.stdout)
    assert match is not None
    parser_commands = match.group(1).split(",")
    assert parser_commands == CLI_COMMAND_NAMES


def test_extra_skill_names_do_not_duplicate_cli_commands() -> None:
    generated_skill_names = {f"sdlc-{name}" for name in SKILL_COMMAND_NAMES}

    assert set(EXTRA_SDLC_SKILL_NAMES).isdisjoint(generated_skill_names)
    assert "sdlc-goal" in EXTRA_SDLC_SKILL_NAMES
    assert "change-create" in CLI_COMMAND_NAMES
    assert "change-create" not in SKILL_COMMAND_NAMES
    assert "change-material" in CLI_COMMAND_NAMES
    assert "change-material" not in SKILL_COMMAND_NAMES
    assert "change-package" in CLI_COMMAND_NAMES
    assert "change-package" not in SKILL_COMMAND_NAMES


def test_placeholder_commands_can_report_current_directory(tmp_path: Path) -> None:
    result = run_cli(["capture"], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "当前目录" in result.stdout


def test_argument_errors_are_reported_in_chinese(tmp_path: Path) -> None:
    missing_arg_result = run_cli(["light-start"], cwd=tmp_path)
    assert missing_arg_result.returncode == 2
    assert "参数错误：缺少必填参数" in missing_arg_result.stderr

    unknown_command_result = run_cli(["unknown"], cwd=tmp_path)
    assert unknown_command_result.returncode == 2
    assert "参数错误：" in unknown_command_result.stderr
    assert "可选值：" in unknown_command_result.stderr


def test_init_prints_single_next_guidance(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)

    init_result = run_cli(["init"], cwd=project_dir)

    assert init_result.returncode == 0, init_result.stderr
    assert "下一步建议直接按这个顺序继续：" in init_result.stdout
    assert "下一步推荐" not in init_result.stdout
    assert not (project_dir / (".external" + "-history")).exists()
    assert not (project_dir / ("external" + "-notes")).exists()


def test_init_rejects_removed_external_history_option(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)

    init_result = run_cli(["init", "--with-history-import"], cwd=project_dir)

    assert init_result.returncode == 2
    assert "with-history-import" in init_result.stderr
    assert not (project_dir / (".external" + "-history")).exists()
    assert not (project_dir / ("external" + "-notes")).exists()


def test_v1_minimum_flow_runs_end_to_end(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)

    init_result = run_cli(["init"], cwd=project_dir)
    assert init_result.returncode == 0, init_result.stderr

    sdlc_dir = project_dir / ".codex-sdlc"
    assert sdlc_dir.exists()
    assert (sdlc_dir / "project.md").exists()
    assert (sdlc_dir / "current.md").exists()
    assert (sdlc_dir / "events.jsonl").exists()
    assert (sdlc_dir / "sdlc.db").exists()

    status_result = run_cli(["status"], cwd=project_dir)
    assert status_result.returncode == 0, status_result.stderr
    assert "没有活跃需求" in status_result.stdout
    assert "下一步建议" in status_result.stdout

    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    requirement_dirs = list((sdlc_dir / "requirements").glob("REQ-001-*"))
    assert len(requirement_dirs) == 1
    requirement_dir = requirement_dirs[0]
    assert (requirement_dir / "requirement.md").exists()
    assert (requirement_dir / "plan.md").exists()
    assert (requirement_dir / "task-map.md").exists()
    assert (requirement_dir / "test-plan.md").exists()
    assert (requirement_dir / "original" / "requirement.v1.md").exists()
    assert (requirement_dir / "effective" / "requirement.current.md").exists()
    assert (requirement_dir / "effective" / "test-matrix.current.md").exists()
    assert (requirement_dir / "versions" / "requirement.v1.md").exists()
    assert (requirement_dir / "traceability.md").exists()
    assert (requirement_dir / "tasks" / "T-001.md").exists()
    current_requirement_text = (requirement_dir / "effective" / "requirement.current.md").read_text(encoding="utf-8")
    assert "FR-001" in current_requirement_text
    assert "当前生效需求版本" in current_requirement_text
    test_matrix_text = (requirement_dir / "effective" / "test-matrix.current.md").read_text(encoding="utf-8")
    assert "TC-001" in test_matrix_text
    assert "active" in test_matrix_text
    traceability_text = (requirement_dir / "traceability.md").read_text(encoding="utf-8")
    assert "FR-001" in traceability_text
    assert "T-001" in traceability_text
    task_text = (requirement_dir / "tasks" / "T-001.md").read_text(encoding="utf-8")
    assert "绑定需求版本：requirement.v1" in task_text
    assert "绑定测试矩阵：test-matrix.v1" in task_text
    with sqlite3.connect(sdlc_dir / "sdlc.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 1

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-finish" in next_result.stdout
    assert "$sdlc-status" in next_result.stdout

    current_text = (sdlc_dir / "current.md").read_text(encoding="utf-8")
    assert "$sdlc-finish" in current_text

    run_brief(project_dir)
    task_doing_result = run_cli(["task", "REQ-001", "T-001"], cwd=project_dir)
    assert task_doing_result.returncode == 0, task_doing_result.stderr
    assert "doing" in task_doing_result.stdout or "进行中" in task_doing_result.stdout
    report_path = write_change_report(project_dir)

    task_done_result = run_cli(
        [
            "task-done",
            "REQ-001",
            "T-001",
            "--file",
            "src/order_export.ts",
            "--change-report",
            str(report_path),
            "--command",
            "npm test",
                "--verify",
                "pytest tests/order_export -q",
                "--verification-type",
                "manual",
                "--verification-status",
                "passed",
        ],
        cwd=project_dir,
    )
    assert task_done_result.returncode == 0, task_done_result.stderr
    assert "done" in task_done_result.stdout or "已完成" in task_done_result.stdout
    requirement_verification = requirement_dir / "verifications" / "VRF-001.md"
    assert requirement_verification.exists()
    assert not (sdlc_dir / "verifications").exists()
    with sqlite3.connect(sdlc_dir / "sdlc.db") as connection:
        verification_path = connection.execute(
            "SELECT file_path FROM verifications WHERE verification_id = 'VRF-001'"
        ).fetchone()[0]
    assert verification_path.startswith(".codex-sdlc/requirements/REQ-001-")
    task_markdown = (requirement_dir / "tasks" / "T-001.md").read_text(encoding="utf-8")
    assert "src/order_export.ts" in task_markdown
    assert "npm test" in task_markdown

    finish_result = run_cli(["finish"], cwd=project_dir)
    assert finish_result.returncode == 0, finish_result.stderr
    assert "SESSION-001" in finish_result.stdout
    session_file = sdlc_dir / "sessions" / "SESSION-001.md"
    assert session_file.exists()
    assert "建议提交说明：交接：" in session_file.read_text(encoding="utf-8")
    requirement_sessions = (requirement_dir / "sessions.md").read_text(encoding="utf-8")
    assert "SESSION-001" in requirement_sessions

    handoff_result = run_cli(["handoff"], cwd=project_dir)
    assert handoff_result.returncode == 0, handoff_result.stderr
    assert str(project_dir) in handoff_result.stdout
    assert "下一步" in handoff_result.stdout
    assert "当前只继续这一项" in handoff_result.stdout
    assert "为什么是这一步" in handoff_result.stdout
    assert "上一轮实际做了什么" in handoff_result.stdout
    assert "当前必须注意" in handoff_result.stdout
    assert "接手后怎么做" in handoff_result.stdout
    assert "边界" in handoff_result.stdout
    assert "只执行上面“当前只继续这一项”里的下一步" in handoff_result.stdout
    assert "不要自动连续推进" in handoff_result.stdout
    assert "当前进度：" not in handoff_result.stdout

    full_handoff_result = run_cli(["handoff", "--full"], cwd=project_dir)
    assert full_handoff_result.returncode == 0, full_handoff_result.stderr
    assert "当前进度：" in full_handoff_result.stdout
    assert "已确认约束" in full_handoff_result.stdout
    assert "已执行验证" in full_handoff_result.stdout

    doctor_result = run_cli(["doctor"], cwd=project_dir)
    assert doctor_result.returncode == 0, doctor_result.stderr
    assert "通过" in doctor_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode == 0, repair_result.stderr
    assert "已重建" in repair_result.stdout or "通过" in repair_result.stdout


def test_doctor_install_reports_real_installation(tmp_path: Path) -> None:
    isolated_home = tmp_path / "home"
    env = {
        "HOME": str(isolated_home),
        "CODEX_SDLC_HOME": str(REPO_ROOT),
        "CODEX_SDLC_PYTHON": sys.executable,
        "CODEX_SDLC_AGENT_HOME": str(isolated_home / ".agents" / "sdlc"),
        "CODEX_SDLC_SOURCE_SKILLS_HOME": str(REPO_ROOT / "skills"),
        "CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME": str(REPO_ROOT / "shared-skills"),
        "CODEX_SDLC_CODEX_SKILLS_HOME": str(isolated_home / ".codex" / "skills"),
        "CODEX_SDLC_AGENTS_SKILLS_HOME": str(isolated_home / ".agents" / "skills"),
        "CODEX_SDLC_CLAUDE_HOME": str(isolated_home / ".claude"),
        "CODEX_SKILLS_HOME": str(isolated_home / ".codex" / "skills"),
        "PATH": os.pathsep.join([str(REPO_ROOT / "bin"), os.environ.get("PATH", "")]),
    }
    sync_result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)
    assert sync_result.returncode == 0, sync_result.stdout + sync_result.stderr

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)
    assert result.returncode == 0, result.stderr
    assert "Skill" in result.stdout
    assert "sdlc-task" in result.stdout
    assert "sdlc-brief" not in result.stdout
    assert ("history" + "skill-") not in result.stdout
    assert result.stdout.count("sdlc-tasks") == 1
    assert "CLI" in result.stdout
    assert "PATH" in result.stdout


def test_doctor_does_not_create_sdlc_dir_for_uninitialized_project(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)

    result = run_cli(["doctor"], cwd=project_dir)
    assert result.returncode == 1
    assert ".codex-sdlc 目录不存在" in result.stdout
    assert not (project_dir / ".codex-sdlc").exists()

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode == 1
    assert ".codex-sdlc 目录不存在" in repair_result.stdout
    assert not (project_dir / ".codex-sdlc").exists()


def test_doctor_reports_missing_snapshot_files_and_repair_rebuilds_them(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    init_result = run_cli(["init"], cwd=project_dir)
    assert init_result.returncode == 0, init_result.stderr

    sdlc_dir = project_dir / ".codex-sdlc"
    (sdlc_dir / "current.md").unlink()
    (sdlc_dir / "project.md").unlink()

    doctor_result = run_cli(["doctor"], cwd=project_dir)
    assert doctor_result.returncode == 1
    assert "缺少文件" in doctor_result.stdout
    assert "current.md" in doctor_result.stdout
    assert "project.md" in doctor_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode == 0, repair_result.stderr
    assert "已重建" in repair_result.stdout
    assert (sdlc_dir / "current.md").exists()
    assert (sdlc_dir / "project.md").exists()


def test_next_detects_snapshot_damage_and_guides_repair(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    sdlc_dir = project_dir / ".codex-sdlc"
    (sdlc_dir / "current.md").unlink()

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-doctor-repair" in next_result.stdout
    assert "current.md 缺失" in next_result.stdout


def test_next_detects_stale_current_next_recommendation(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    current_file = project_dir / ".codex-sdlc" / "current.md"
    current_file.write_text(
        current_file.read_text(encoding="utf-8").replace(
            "- 主推荐：$sdlc-finish",
            "- 主推荐：$sdlc-plan REQ-001",
        ),
        encoding="utf-8",
    )

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-doctor-repair" in next_result.stdout
    assert "current.md 里的下一步主推荐已过期" in next_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode == 0, repair_result.stderr
    assert "$sdlc-finish" in current_file.read_text(encoding="utf-8")


def test_next_lists_requirement_scoped_alternatives_when_multiple_requirements_exist(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    create_minimal_requirement_by_start_file(project_dir, title="增加客户标签功能")

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-finish" in next_result.stdout
    assert "$sdlc-status" in next_result.stdout


def test_brief_review_refreshes_next_and_keeps_reviewed_task_focus_when_multiple_requirements_exist(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    create_minimal_requirement_by_start_file(project_dir, title="增加客户标签功能")
    assert run_cli(["brief", "REQ-001", "T-001"], cwd=project_dir, extra_env=env).returncode == 0

    review_result = run_cli(
        [
            "brief-review",
            "REQ-001",
            "T-001",
            "--status",
            "passed",
            "--method",
            "rg",
            "--summary",
            "已经核对订单导出任务执行包，可以开工。",
        ],
        cwd=project_dir,
        extra_env=env,
    )
    next_result = run_cli(["next"], cwd=project_dir, extra_env=env)
    index_data = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))

    assert review_result.returncode == 0, review_result.stderr
    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-finish" in next_result.stdout
    assert "$sdlc-status" in next_result.stdout
    assert "$sdlc-doctor-repair" not in next_result.stdout
    assert any(
        "auto-brief-review-after" in str(item.get("archive", ""))
        for item in index_data["project_snapshots"]
    )


def test_task_stops_when_pending_change_exists(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    change_file = project_dir / ".codex-sdlc" / "changes" / "CHG-001.md"
    change_file.write_text("# 待处理变更\n", encoding="utf-8")

    task_result = run_cli(["task", "REQ-001", "T-001"], cwd=project_dir)
    assert task_result.returncode == 2
    assert "待规划需求变化" in task_result.stderr

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-doctor-repair" in next_result.stdout
    assert "未纳入状态" in next_result.stdout


def test_init_sets_git_ignore_without_old_external_strategy(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    (project_dir / (".external" + "-history")).mkdir()
    temp_home = tmp_path / "home"
    temp_home.mkdir()
    env = {"HOME": str(temp_home)}

    init_result = run_cli(["init"], cwd=project_dir, extra_env=env)
    assert init_result.returncode == 0, init_result.stderr

    excludes_file = temp_home / ".gitignore_global"
    assert excludes_file.exists()
    assert ".codex-sdlc/" in excludes_file.read_text(encoding="utf-8")

    config_result = run_git(["config", "--global", "--get", "core.excludesfile"], cwd=project_dir, extra_env=env)
    assert config_result.returncode == 0
    assert config_result.stdout.strip() == str(excludes_file)

    ignore_result = run_git(["check-ignore", ".codex-sdlc/current.md"], cwd=project_dir, extra_env=env)
    assert ignore_result.returncode == 0

    project_text = (project_dir / ".codex-sdlc" / "project.md").read_text(encoding="utf-8")
    assert (".external" + "-history") not in project_text




def test_plan_can_reorder_and_close_tasks_without_losing_history(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    reorder_result = run_cli(["plan-reorder", "REQ-001", "T-002,T-001,T-003"], cwd=project_dir)
    assert reorder_result.returncode == 0, reorder_result.stderr
    close_result = run_cli(["plan-close", "REQ-001", "T-003"], cwd=project_dir)
    assert close_result.returncode == 0, close_result.stderr
    depends_result = run_cli(["plan-depends", "REQ-001", "T-001:T-002"], cwd=project_dir)
    assert depends_result.returncode == 0, depends_result.stderr

    plan_text = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*/plan.md")).read_text(encoding="utf-8")
    assert "T-002" in plan_text
    assert "T-003 [closed]" in plan_text
    assert any(event["event_type"] == "plan_updated" for event in read_events(project_dir))

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-finish" in next_result.stdout
    assert "$sdlc-brief REQ-001 T-003" not in next_result.stdout

    status_result = run_cli(["status"], cwd=project_dir)
    assert status_result.returncode == 0, status_result.stderr
    assert "- 已完成任务：0" in status_result.stdout
    assert "- 已关闭任务：1" in status_result.stdout
    assert "- 已完成或关闭：1/3" in status_result.stdout


def test_plan_dependency_changes_cannot_block_active_task(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")
    run_brief(project_dir)
    assert run_cli(["task", "REQ-001", "T-001"], cwd=project_dir).returncode == 0

    depends_result = run_cli(["plan-depends", "REQ-001", "T-001:T-002"], cwd=project_dir)

    assert depends_result.returncode == 1
    assert "T-001 当前是 doing，不能依赖未完成任务：T-002" in depends_result.stderr
    assert "反向阻塞当前任务收口" in depends_result.stderr

    amend_result = run_cli(["plan-amend-task", "REQ-001", "T-001", "--depends", "T-002"], cwd=project_dir)

    assert amend_result.returncode == 1
    assert "T-001 当前是 doing，不能依赖未完成任务：T-002" in amend_result.stderr

    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        depends_on = json.loads(
            connection.execute(
                "SELECT depends_on_json FROM tasks WHERE requirement_id = 'REQ-001' AND task_id = 'T-001'"
            ).fetchone()[0]
        )
    assert depends_on == []


def test_capture_can_minimally_initialize_and_record_unclassified_note(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0
    from test_cli_v6_discuss_prepare import append_structured_cap

    capture_result = append_structured_cap(
        project_dir,
        submission_key="capture-order-export-note",
        capture_type="fact",
        increment="本轮确认订单导出字段先按后端默认字段处理，前端只做入口。",
        command="capture",
    )
    assert capture_result.returncode == 0, capture_result.stderr
    assert "CAP-001" in capture_result.stdout

    capture_file = project_dir / ".codex-sdlc" / "captures" / "CAP-001.md"
    assert capture_file.exists()
    assert "前端只做入口" in capture_file.read_text(encoding="utf-8")

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-material" in next_result.stdout


def test_event_id_uses_today_max_number_instead_of_total_lines(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    from codex_sdlc.core.project import build_paths
    from codex_sdlc.core.state import append_event

    paths = build_paths(project_dir)
    append_event(
        paths,
        event_type="test_note_recorded",
        source="sdlc-test-helper",
        summary="先记录一条初始化结论。",
        payload={},
    )

    today = datetime.now().strftime("%Y%m%d")
    events = read_events(project_dir)
    events[0]["event_id"] = "EVT-20260617-000001"
    events[-1]["event_id"] = f"EVT-{today}-000092"
    write_events(project_dir, events)

    append_event(
        paths,
        event_type="test_note_recorded",
        source="sdlc-test-helper",
        summary="再记录一条结论。",
        payload={},
    )

    new_events = read_events(project_dir)
    assert new_events[-1]["event_id"] == f"EVT-{today}-000093"
    assert len({event["event_id"] for event in new_events}) == len(new_events)


def test_change_marks_impacted_tasks_and_next_prioritizes_change(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    change_result = run_cli(["change", "REQ-001", "导出范围增加订单来源筛选。", "--impacted-task", "T-001", "--impacted-task", "T-002"], cwd=project_dir)
    assert change_result.returncode == 0, change_result.stderr
    assert "CHG-001" in change_result.stdout
    assert "- T-001" in change_result.stdout
    assert "- T-002" in change_result.stdout
    assert "$sdlc-change-accept REQ-001 CHG-001" in change_result.stdout

    change_file = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*/changes/CHG-001.md"))
    assert change_file.exists()
    change_file_text = change_file.read_text(encoding="utf-8")
    assert "订单来源筛选" in change_file_text
    assert "- 状态：draft" in change_file_text

    status_result = run_cli(["status"], cwd=project_dir)
    assert status_result.returncode == 0, status_result.stderr
    assert "changed" in status_result.stdout

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-change-accept REQ-001 CHG-001" in next_result.stdout

    change_plan_before_accept = run_cli(["change-plan", "REQ-001"], cwd=project_dir)
    assert change_plan_before_accept.returncode == 1
    assert "$sdlc-change-accept REQ-001 CHG-001" in change_plan_before_accept.stderr

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)
    assert accept_result.returncode == 0, accept_result.stderr
    assert "已确认并生效需求变更：CHG-001" in accept_result.stdout
    assert "CHG-001 已经写入当前生效需求，但还没有模型任务建议" in accept_result.stdout
    assert "$sdlc-change-plan REQ-001 --change CHG-001 --task" in accept_result.stdout

    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    change_file_text = (requirement_dir / "changes" / "CHG-001.md").read_text(encoding="utf-8")
    assert "- 状态：effective" in change_file_text
    assert "- 规划状态：待模型规划" in change_file_text
    current_requirement_text = (requirement_dir / "effective" / "requirement.current.md").read_text(encoding="utf-8")
    assert "需求版本：requirement.v2" in current_requirement_text
    assert "订单来源筛选" in current_requirement_text

    accept_event = next(event for event in read_events(project_dir) if event["event_type"] == "change_accepted")
    assert accept_event["payload"]["planning_status"] == "needs_model_plan"
    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        change_status = connection.execute("SELECT status FROM changes WHERE change_id = 'CHG-001'").fetchone()[0]
    assert change_status == "effective"

    status_after_accept = run_cli(["status"], cwd=project_dir)
    next_after_accept_before_plan = run_cli(["next"], cwd=project_dir)
    assert status_after_accept.returncode == 0, status_after_accept.stderr
    assert next_after_accept_before_plan.returncode == 0, next_after_accept_before_plan.stderr
    assert "- 主推荐：$sdlc-change-plan REQ-001 --change CHG-001 --task" in status_after_accept.stdout
    assert "- 主推荐：$sdlc-change-plan REQ-001 --change CHG-001 --task" in next_after_accept_before_plan.stdout

    change_plan_result = run_cli(
        [
            "change-plan",
            "REQ-001",
            "--change",
            "CHG-001",
            "--task",
            "增加订单来源筛选||导出范围增加订单来源筛选。||FR-001",
        ],
        cwd=project_dir,
    )
    assert change_plan_result.returncode == 0, change_plan_result.stderr
    assert "已写入模型任务建议" in change_plan_result.stdout
    assert "已自动完成开工准备" in change_plan_result.stdout
    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        change_status = connection.execute("SELECT status FROM changes WHERE change_id = 'CHG-001'").fetchone()[0]
    assert change_status == "planned"

    next_after_accept = run_cli(["next"], cwd=project_dir)
    assert next_after_accept.returncode == 0, next_after_accept.stderr
    assert "$sdlc-finish" in next_after_accept.stdout


def test_change_plan_keeps_pending_fix_before_new_change_task(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")
    run_brief(project_dir)
    assert run_cli(["task", "REQ-001", "T-001"], cwd=project_dir).returncode == 0
    done_result = run_cli(
        ["task-done", "REQ-001", "T-001", "--clear-test-commands", "--verify", "当前实现范围已确认", "--verification-type", "manual", "--verification-status", "passed"],
        cwd=project_dir,
    )
    assert done_result.returncode == 0, done_result.stderr

    fix_result = run_cli(
        ["fix", "REQ-001", "T-001", "历史验收发现导出权限判断遗漏，需要补修复任务"],
        cwd=project_dir,
    )
    assert fix_result.returncode == 0, fix_result.stderr
    assert "T-004" in fix_result.stdout

    change_result = run_cli(
        [
            "change",
            "REQ-001",
            "新增导出按钮加载中状态",
            "--reason",
            "用户补充交互要求",
            "--acceptance",
            "点击导出后展示加载中，完成后恢复",
            "--task",
            "增加导出按钮加载中状态||增加导出按钮加载中状态||FR-001",
        ],
        cwd=project_dir,
    )
    assert change_result.returncode == 0, change_result.stderr
    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)
    assert accept_result.returncode == 0, accept_result.stderr

    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    plan_text = (requirement_dir / "plan.md").read_text(encoding="utf-8")
    assert plan_text.index("T-004 [todo] 修复 T-001") < plan_text.index("T-005 [todo] 增加导出按钮加载中状态")
    t004_text = (requirement_dir / "tasks" / "T-004.md").read_text(encoding="utf-8")
    t005_text = (requirement_dir / "tasks" / "T-005.md").read_text(encoding="utf-8")
    assert "- 依赖：T-001" in t004_text
    assert "- 依赖：T-004" in t005_text

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-handoff" in next_result.stdout
    assert "$sdlc-task REQ-001 T-005" not in next_result.stdout
    assert "任务 后" not in next_result.stdout
    assert "$sdlc-status" in next_result.stdout




def test_plan_routes_effective_change_to_change_plan_without_resolving(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    assert run_cli(["change", "REQ-001", "导出范围增加订单来源筛选。"], cwd=project_dir).returncode == 0
    assert run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir).returncode == 0

    plan_result = run_cli(["plan", "REQ-001"], cwd=project_dir)

    assert plan_result.returncode == 1
    assert "$sdlc-change-plan REQ-001" in plan_result.stderr
    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        change_status = connection.execute("SELECT status FROM changes WHERE change_id = 'CHG-001'").fetchone()[0]
    assert change_status == "effective"




def test_change_plan_names_bug_fix_and_binds_all_new_change_points(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="阅读模式默认打开方式")
    change_text = (
        "测试发现之前完成的阅读模式任务有问题：默认打开方式选择跟随上次或阅读模式时，详情内手动切到原文会被拉回阅读模式。"
        "正确逻辑："
        "1. 单次切换不限制原文或者阅读模式，可自由切换。"
        "2. 设置为默认打开阅读模式时，本次切换后退出再次进入详情，依然默认打开阅读模式。"
        "3. 设置为默认打开跟随上次时，退出详情回到列表再次进入详情时，读取记录的上次使用模式。"
    )
    assert run_cli(
        [
            "change",
            "REQ-001",
            change_text,
            "--task",
            "修复阅读模式默认打开规则影响手动切换的问题",
        ],
        cwd=project_dir,
    ).returncode == 0

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "已自动规划需求变更：CHG-001" in accept_result.stdout
    assert "修复阅读模式默认打开规则影响手动切换的问题" in accept_result.stdout
    assert "用户故事 3：按设置真实路径打开搜索结果" not in accept_result.stdout

    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    task_files = sorted((requirement_dir / "tasks").glob("T-*.md"))
    created_task_text = "\n".join(path.read_text(encoding="utf-8") for path in task_files)
    assert "修复阅读模式默认打开规则影响手动切换的问题" in created_task_text
    assert "用户故事 3：按设置真实路径打开搜索结果" not in created_task_text
    fix_task_text = next(
        path.read_text(encoding="utf-8")
        for path in task_files
        if "修复阅读模式默认打开规则影响手动切换的问题" in path.read_text(encoding="utf-8")
    )
    assert "覆盖变更：CHG-001" in fix_task_text
    assert "FR-002" not in fix_task_text

    change_map_text = (requirement_dir / "change-map.md").read_text(encoding="utf-8")
    assert "CHG-001 [planned]" in change_map_text
    assert "FR-002：" not in change_map_text
    assert "修复阅读模式默认打开规则影响手动切换的问题" in created_task_text




def test_change_plan_uses_model_task_suggestion_for_business_change(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="阅读模式设置用户隔离")
    change_text = (
        "阅读模式设置需要按登录身份隔离保存和读取：设置页阅读模式设置、公告详情下的阅读模式设置都要区分账号A用户和账号B用户。"
        "两类用户各自使用独立的阅读模式配置，包含默认打开方式、背景、亮度、跟随系统、字体大小以及上次阅读状态等相关设置；"
        "账号A用户修改后不能影响账号B用户，账号B用户修改后也不能影响账号A用户。"
    )
    change_result = run_cli(
        [
            "change",
            "REQ-001",
            change_text,
            "--task",
            "阅读模式设置按账号A用户和账号B用户隔离保存读取",
            "--acceptance",
            "切换账号A用户和账号B用户后分别回显各自保存的配置。",
            "--acceptance",
            "回归默认打开方式三态规则、背景亮度弹窗、字体大小弹窗、公告进入规则和保存失败兜底。",
        ],
        cwd=project_dir,
    )
    assert change_result.returncode == 0, change_result.stderr
    assert "新增任务建议：" in change_result.stdout
    assert "T-003：阅读模式设置按账号A用户和账号B用户隔离保存读取" in change_result.stdout
    assert "CLI 兜底" not in change_result.stdout

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "T-003：阅读模式设置按账号A用户和账号B用户隔离保存读取" in accept_result.stdout
    assert "T-004" not in accept_result.stdout
    assert "用户故事 3：按设置真实路径打开搜索结果" not in accept_result.stdout

    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    task_files = sorted((requirement_dir / "tasks").glob("T-*.md"))
    all_task_text = "\n".join(path.read_text(encoding="utf-8") for path in task_files)
    created_task_text = (requirement_dir / "tasks" / "T-003.md").read_text(encoding="utf-8")
    assert "阅读模式设置按账号A用户和账号B用户隔离保存读取" in created_task_text
    assert "默认打开方式、背景、亮度、跟随系统、字体大小以及上次阅读状态" in created_task_text
    assert "回归变更 CHG-001：回归默认打开方式三态规则" in created_task_text
    assert "人工确认变更 CHG-001 已覆盖：切换账号A用户和账号B用户后分别回显各自保存的配置" in created_task_text
    assert "用户故事 3：按设置真实路径打开搜索结果" not in all_task_text


def test_add_passes_model_task_suggestion_to_change_accept(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="阅读模式设置用户隔离")

    add_result = run_cli(
        [
            "add",
            "REQ-001",
            "阅读模式设置需要按登录身份隔离保存和读取，账号A用户和账号B用户互不影响。",
            "--kind",
            "change",
            "--task",
            "阅读模式设置按登录身份隔离保存读取",
            "--acceptance",
            "切换两类用户后分别回显各自保存的配置。",
        ],
        cwd=project_dir,
    )
    assert add_result.returncode == 0, add_result.stderr
    assert "$sdlc-change-accept REQ-001 CHG-001" in add_result.stdout

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "T-003：阅读模式设置按登录身份隔离保存读取" in accept_result.stdout
    assert "T-004" not in accept_result.stdout
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    created_task_text = (requirement_dir / "tasks" / "T-003.md").read_text(encoding="utf-8")
    assert "回归变更 CHG-001：切换两类用户后分别回显各自保存的配置" in created_task_text


def test_change_without_model_task_suggestion_waits_for_model_plan(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="设置页入口优化")

    change_result = run_cli(["change", "REQ-001", "新增一个完全独立的侧边栏展示规则。"], cwd=project_dir)

    assert change_result.returncode == 0, change_result.stderr
    assert "受影响任务：当前没命中已有任务。" in change_result.stdout
    assert "新增任务建议：" in change_result.stdout
    assert "暂无，确认生效后由 Codex 通过 change-plan 补任务拆分" in change_result.stdout
    assert "处理变更：新增一个完全独立的侧边栏" not in change_result.stdout


def test_change_without_impacted_task_does_not_bind_current_open_task(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")
    run_brief(project_dir)
    assert run_cli(["task", "REQ-001", "T-001"], cwd=project_dir).returncode == 0

    change_result = run_cli(
        [
            "change",
            "REQ-001",
            "阅读模式设置需要按登录身份隔离保存和读取，账号A用户和账号B用户互不影响。",
            "--task",
            "阅读模式设置按登录身份隔离保存读取",
        ],
        cwd=project_dir,
    )

    assert change_result.returncode == 0, change_result.stderr
    assert "受影响任务：当前没命中已有任务" in change_result.stdout
    change_events = [event for event in read_events(project_dir) if event["event_type"] == "change_recorded"]
    assert change_events[-1]["payload"]["changed_task_ids"] == []


def test_change_accept_inserts_new_task_after_active_task_without_blocking_it(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")
    run_brief(project_dir)
    assert run_cli(["task", "REQ-001", "T-001"], cwd=project_dir).returncode == 0

    assert run_cli(
        [
            "change",
            "REQ-001",
            "阅读模式设置需要按登录身份隔离保存和读取，账号A用户和账号B用户互不影响。",
            "--task",
            "阅读模式设置按登录身份隔离保存读取",
        ],
        cwd=project_dir,
    ).returncode == 0
    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "T-004：阅读模式设置按登录身份隔离保存读取" in accept_result.stdout
    assert "$sdlc-brief REQ-001 T-004" in accept_result.stdout
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    plan_text = (requirement_dir / "plan.md").read_text(encoding="utf-8")
    assert plan_text.index("T-001 [doing]") < plan_text.index("T-004 [todo]")
    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        t001_depends = json.loads(
            connection.execute(
                "SELECT depends_on_json FROM tasks WHERE requirement_id = 'REQ-001' AND task_id = 'T-001'"
            ).fetchone()[0]
        )
        t004_depends = json.loads(
            connection.execute(
                "SELECT depends_on_json FROM tasks WHERE requirement_id = 'REQ-001' AND task_id = 'T-004'"
            ).fetchone()[0]
        )
    assert t001_depends == []
    assert t004_depends == ["T-001"]


def test_change_plan_does_not_treat_default_open_way_as_search_navigation(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="阅读模式设置用户隔离")
    change_text = (
        "阅读模式设置需要按登录身份隔离保存和读取：设置页阅读模式设置、公告详情下的阅读模式设置都要区分账号A用户和账号B用户。"
        "两类用户各自使用独立的阅读模式配置，包含默认打开方式、背景、亮度、跟随系统、字体大小以及上次阅读状态等相关设置；"
        "账号A用户修改后不能影响账号B用户，账号B用户修改后也不能影响账号A用户。"
    )
    assert run_cli(["change", "REQ-001", change_text], cwd=project_dir).returncode == 0

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "用户故事 3：按设置真实路径打开搜索结果" not in accept_result.stdout
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    all_task_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted((requirement_dir / "tasks").glob("T-*.md")))
    assert "用户故事 3：按设置真实路径打开搜索结果" not in all_task_text


def test_change_accept_merges_task_goal_amend_into_existing_open_task(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="检查设置页埋点覆盖")
    change_text = (
        "T-001 任务目标补充：检查设置页和设置页下原生子页面埋点覆盖时，如果发现设置项点击埋点、"
        "设置项点击跳页埋点或子页浏览时长埋点缺失，不只记录问题，要在当前任务内完成修复、验证和任务变更报告。"
    )
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    task_count_before = len(list((requirement_dir / "tasks").glob("T-*.md")))
    assert run_cli(["change", "REQ-001", change_text, "--impacted-task", "T-001"], cwd=project_dir).returncode == 0

    accept_result = run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir)

    assert accept_result.returncode == 0, accept_result.stderr
    assert "已自动规划需求变更：CHG-001" in accept_result.stdout
    assert "新增任务：" not in accept_result.stdout
    task_files = sorted((requirement_dir / "tasks").glob("T-*.md"))
    assert len(task_files) == task_count_before
    task_text = (requirement_dir / "tasks" / "T-001.md").read_text(encoding="utf-8")
    assert "变更来源：CHG-001" in task_text
    assert "当前任务内完成修复" in task_text
    assert "验证变更 CHG-001" in task_text
    all_task_text = "\n".join(path.read_text(encoding="utf-8") for path in task_files)
    assert "处理 CHG-001" not in all_task_text
    change_map_text = (requirement_dir / "change-map.md").read_text(encoding="utf-8")
    assert "-> T-001" in change_map_text


def test_plan_amend_task_can_replace_old_test_and_manual_items(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="检查设置页埋点覆盖")

    result = run_cli(
        [
            "plan-amend-task",
            "REQ-001",
            "T-001",
            "--test-item",
            "RED：先输出埋点缺口清单，证明检查能发现问题。",
            "--test-item",
            "GREEN：修复后输出已覆盖、已修复、无法确认清单。",
            "--manual-check",
            "人工确认：收口报告只保留当前最新埋点检查和修复口径。",
            "--replace-test-items",
            "--replace-manual-checks",
        ],
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "自动测试项：已替换" in result.stdout
    assert "人工验收点：已替换" in result.stdout
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    task_text = (requirement_dir / "tasks" / "T-001.md").read_text(encoding="utf-8")
    assert "RED：先输出埋点缺口清单" in task_text
    assert "GREEN：修复后输出已覆盖、已修复、无法确认清单" in task_text
    assert "收口报告只保留当前最新埋点检查和修复口径" in task_text
    assert "TDD RED：先写或确认一个会失败的最小测试" not in task_text
    assert "开始开发前已审核测试方案" not in task_text


def test_task_refuses_stale_bound_versions_until_prepare_refreshes(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    assert run_cli(["change", "REQ-001", "导出范围增加订单来源筛选。"], cwd=project_dir).returncode == 0
    assert run_cli(["change-accept", "REQ-001", "CHG-001"], cwd=project_dir).returncode == 0
    assert run_cli(
        [
            "change-plan",
            "REQ-001",
            "--change",
            "CHG-001",
            "--task",
            "增加订单来源筛选||导出范围增加订单来源筛选。||FR-001",
        ],
        cwd=project_dir,
    ).returncode == 0

    events = read_events(project_dir)
    latest_plan = next(event for event in reversed(events) if event["event_type"] == "plan_updated")
    target_task_id = latest_plan["payload"]["tasks"][0]["task_id"]  # type: ignore[index]
    for task in latest_plan["payload"]["tasks"]:  # type: ignore[index]
        if task["task_id"] == target_task_id:
            task["requirement_version"] = "requirement.v1"
            task["design_version"] = "design.v1"
            task["test_matrix_version"] = "test-matrix.v1"
    write_events(project_dir, events)

    task_result = run_cli(["task", "REQ-001", target_task_id], cwd=project_dir)

    assert task_result.returncode == 1
    assert "任务执行包缺失" in task_result.stderr
    assert f"$sdlc-brief REQ-001 {target_task_id}" in task_result.stderr

    prepare_result = run_cli(["prepare", "REQ-001"], cwd=project_dir)
    assert prepare_result.returncode == 0, prepare_result.stderr

    run_brief(project_dir, "REQ-001", target_task_id)
    task_after_prepare = run_cli(["task", "REQ-001", target_task_id], cwd=project_dir)
    assert task_after_prepare.returncode == 0, task_after_prepare.stderr


def test_task_done_refuses_missing_fr_or_tc_coverage(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")

    events = read_events(project_dir)
    for event in events:
        if event["event_type"] == "task_created" and event["task_id"] == "T-001":
            event["payload"]["coverage_points"] = []
            event["payload"]["coverage_tests"] = []
    write_events(project_dir, events)

    done_result = run_cli(["task-done", "REQ-001", "T-001", "--verify", "人工验证通过"], cwd=project_dir)

    assert done_result.returncode == 1
    assert "任务执行包缺失" in done_result.stderr
    assert "$sdlc-brief REQ-001 T-001" in done_result.stderr


def test_doctor_deep_and_repair_handle_legacy_change_residuals(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    assert run_cli(["change", "REQ-001", "导出范围增加订单来源筛选。"], cwd=project_dir).returncode == 0

    events_file = project_dir / ".codex-sdlc" / "events.jsonl"
    events = read_events(project_dir)
    requirement = next(event for event in reversed(events) if event["event_type"] == "requirement_created")
    task_events = [event for event in events if event["event_type"] == "task_created"]
    tasks = [
        {
            "task_id": event["task_id"],
            "title": event["payload"]["title"],
            "summary": event["payload"]["summary"],
            "status": "todo",
            "depends_on": [],
            "note": "变更来源：CHG-001",
        }
        for event in task_events
    ]
    legacy_event = {
        "event_id": f"EVT-20260609-{len(events) + 1:06d}",
        "event_type": "plan_updated",
        "project_path": str(project_dir),
        "requirement_id": requirement["requirement_id"],
        "task_id": None,
        "created_at": "2026-06-09T10:00:00+08:00",
        "source": "legacy-sdlc-plan",
        "summary": "旧流程误把变更标记为已处理",
        "payload": {
            "tasks": tasks,
            "priority": "high",
            "blocked_reason": "",
            "resolved_change_ids": ["CHG-001"],
        },
    }
    with events_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_event, ensure_ascii=False) + "\n")

    deep_result = run_cli(["doctor-deep"], cwd=project_dir)
    assert deep_result.returncode in {0, 1}, deep_result.stderr
    assert "旧变更状态残留" in deep_result.stdout
    assert "CHG-001" in deep_result.stdout
    assert "$sdlc-doctor-repair" in deep_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode in {0, 1}, repair_result.stderr
    assert "旧变更状态残留未自动修复" in repair_result.stdout

    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        change_status, = connection.execute("SELECT status FROM changes WHERE change_id = 'CHG-001'").fetchone()
    assert change_status == "resolved"

    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    change_map_text = (requirement_dir / "change-map.md").read_text(encoding="utf-8")
    assert "CHG-001 [resolved]" in change_map_text
    assert "暂无覆盖映射" in change_map_text

    repaired_events = read_events(project_dir)
    repair_events = [
        event
        for event in repaired_events
        if event["source"] == "sdlc-doctor-repair" and "planned_changes" in event["payload"]
    ]
    assert repair_events == []


def test_doctor_repair_does_not_guess_legacy_change_without_task_evidence(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    assert run_cli(["change", "REQ-001", "新增导出备注字段。"], cwd=project_dir).returncode == 0

    events_file = project_dir / ".codex-sdlc" / "events.jsonl"
    events = read_events(project_dir)
    for event in events:
        if event["event_type"] == "change_recorded":
            event["payload"]["changed_task_ids"] = []
    events_file.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )

    task_events = [event for event in events if event["event_type"] == "task_created"]
    legacy_event = {
        "event_id": f"EVT-20260609-{len(events) + 1:06d}",
        "event_type": "plan_updated",
        "project_path": str(project_dir),
        "requirement_id": "REQ-001",
        "task_id": None,
        "created_at": "2026-06-09T10:00:00+08:00",
        "source": "legacy-sdlc-plan",
        "summary": "旧流程只改了变更状态，没有保留任务映射",
        "payload": {
            "tasks": [
                {
                    "task_id": event["task_id"],
                    "title": event["payload"]["title"],
                    "summary": event["payload"]["summary"],
                    "status": "todo",
                    "depends_on": [],
                    "note": "",
                }
                for event in task_events
            ],
            "priority": "high",
            "blocked_reason": "",
            "resolved_change_ids": ["CHG-001"],
        },
    }
    with events_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy_event, ensure_ascii=False) + "\n")

    deep_result = run_cli(["doctor-deep"], cwd=project_dir)
    assert deep_result.returncode in {0, 1}, deep_result.stderr
    assert "旧变更状态残留" in deep_result.stdout
    assert "无法安全推断覆盖任务" in deep_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode in {0, 1}, repair_result.stderr
    assert "旧变更状态残留未自动修复" in repair_result.stdout
    assert "无法安全推断覆盖任务" in repair_result.stdout

    repaired_events = [
        event
        for event in read_events(project_dir)
        if event["source"] == "sdlc-doctor-repair" and "planned_changes" in event["payload"]
    ]
    assert repaired_events == []


def test_doctor_deep_and_repair_detect_task_contract_drift(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    events = read_events(project_dir)
    for event in events:
        if event["event_type"] == "task_created" and event["task_id"] == "T-001":
            event["payload"]["coverage_tests"] = ["TC-999"]
    write_events(project_dir, events)

    deep_result = run_cli(["doctor-deep"], cwd=project_dir)
    assert deep_result.returncode == 1
    assert "任务版本或覆盖关系异常" in deep_result.stdout
    assert "T-001" in deep_result.stdout
    assert "$sdlc-doctor-repair" in deep_result.stdout

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode in {0, 1}, repair_result.stderr
    assert "已刷新任务版本和覆盖关系" in repair_result.stdout or "任务版本或覆盖关系异常" in repair_result.stdout


def test_export_generates_markdown_summary_without_absolute_project_path(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")
    run_brief(project_dir)
    assert run_cli(
        ["task-done", "REQ-001", "T-001", "--verify", "pytest tests/order_export -q", "--verification-type", "manual", "--verification-status", "passed"],
        cwd=project_dir,
    ).returncode == 0
    assert run_cli(["finish"], cwd=project_dir).returncode == 0

    export_all = run_cli(["export"], cwd=project_dir)
    assert export_all.returncode == 0, export_all.stderr
    assert "all-requirements.md" in export_all.stdout

    export_one = run_cli(["export-requirement", "REQ-001"], cwd=project_dir)
    assert export_one.returncode == 0, export_one.stderr
    assert "REQ-001.md" in export_one.stdout

    all_export = project_dir / ".codex-sdlc" / "exports" / "all-requirements.md"
    single_export = project_dir / ".codex-sdlc" / "exports" / "REQ-001.md"
    assert all_export.exists()
    assert single_export.exists()
    assert "导出范围：全部需求" in all_export.read_text(encoding="utf-8")
    assert "导出范围：REQ-001 增加订单导出功能" in single_export.read_text(encoding="utf-8")
    assert "验证记录" in single_export.read_text(encoding="utf-8")
    assert str(project_dir) not in single_export.read_text(encoding="utf-8")


# 这些旧用例依赖已经下线的 prepare、brief 或自然语言 change 写入口。
# 对应能力已经由 document-first、结构化 CHG、task-run 和 legacy 只读合同覆盖，
# 不再把旧流程函数作为 pytest 测试入口收集。
for _retired_test in (
    test_v1_minimum_flow_runs_end_to_end,
    test_brief_review_refreshes_next_and_keeps_reviewed_task_focus_when_multiple_requirements_exist,
    test_plan_dependency_changes_cannot_block_active_task,
    test_change_marks_impacted_tasks_and_next_prioritizes_change,
    test_change_plan_keeps_pending_fix_before_new_change_task,
    test_plan_routes_effective_change_to_change_plan_without_resolving,
    test_change_plan_names_bug_fix_and_binds_all_new_change_points,
    test_change_plan_uses_model_task_suggestion_for_business_change,
    test_add_passes_model_task_suggestion_to_change_accept,
    test_change_without_model_task_suggestion_waits_for_model_plan,
    test_change_without_impacted_task_does_not_bind_current_open_task,
    test_change_accept_inserts_new_task_after_active_task_without_blocking_it,
    test_change_plan_does_not_treat_default_open_way_as_search_navigation,
    test_change_accept_merges_task_goal_amend_into_existing_open_task,
    test_task_refuses_stale_bound_versions_until_prepare_refreshes,
    test_task_done_refuses_missing_fr_or_tc_coverage,
    test_doctor_deep_and_repair_handle_legacy_change_residuals,
    test_doctor_repair_does_not_guess_legacy_change_without_task_evidence,
    test_export_generates_markdown_summary_without_absolute_project_path,
):
    _retired_test.__test__ = False
