from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
CONTRACT_TESTS_DIR = REPO_ROOT / "tests" / "contracts"
if str(CONTRACT_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(CONTRACT_TESTS_DIR))

from codex_sdlc.core.project import build_paths
from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.core.state import derive_state, split_requirement_point_text
from codex_sdlc.services import review_service
from formal_package_factory import write_formal_v3_package
from test_cli_v1 import SDLC_SKILLS_HOME, init_demo_repo, run_cli
from test_model_fact_cli_flow import install_historical_fact_archive
from test_task_plan_review_flow import (
    _add_second_task_to_submission,
    _create_review,
    _import_task_plan,
    _submission,
)
from test_task_planning_code_evidence import _project, _task_args, _write_task_submission


def run_python_hook(script_path: Path, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
    )


def create_minimal_requirement_by_start_file(
    project_dir: Path,
    title: str = "修一个按钮文案",
    *,
    slug: str | None = None,
    with_tasks: bool = True,
) -> Path:
    """这些兼容测试只需要既有档案，不能再调用已停止接收 facts 包的建档入口。"""

    del slug
    existing = sorted((project_dir / ".codex-sdlc/requirements").glob("REQ-*"))
    requirement_id = f"REQ-{len(existing) + 1:03d}"
    return install_historical_fact_archive(
        project_dir,
        title=title,
        requirement_id=requirement_id,
        with_tasks=with_tasks,
    )


def test_capture_can_link_to_requirement_and_change_can_reference_capture(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    events_before = (project_dir / ".codex-sdlc/events.jsonl").read_bytes()

    capture_result = run_cli(
        ["capture", "--requirement", "REQ-001", "导出字段确认增加订单来源。"],
        cwd=project_dir,
    )

    assert capture_result.returncode == 1
    assert "只能追加结构化 CAP" in capture_result.stderr
    assert (project_dir / ".codex-sdlc/events.jsonl").read_bytes() == events_before

def test_capture_and_decision_markdown_wraps_long_text(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    long_summary = (
        "本轮确认导出入口只给运营角色展示。非运营角色不展示按钮。导出接口继续保留后端权限校验。"
        "导出失败时保持当前页面不跳转。后续如果新增渠道字段，需要先回到需求变更流程补任务和测试。"
        "这条记录故意写得比较长，用来验证自由文本不会绕过结构化 CAP 合同。"
    )

    capture_result = run_cli(["capture", long_summary], cwd=project_dir)

    assert capture_result.returncode == 1
    assert "只能追加结构化 CAP" in capture_result.stderr
    assert not list((project_dir / ".codex-sdlc/captures").glob("CAP-*.md"))

def test_sdlc_start_skill_describes_native_start_flow() -> None:
    skill_text = (SDLC_SKILLS_HOME / "sdlc-start/SKILL.md").read_text(encoding="utf-8")

    assert "$sdlc-start" in skill_text
    assert "$sdlc-tasks" in skill_text
    assert "codex-sdlc start --file" in skill_text
    assert "functional_requirements" in skill_text
    assert "acceptance_criteria" in skill_text
    assert "test_cases" in skill_text


def test_sdlc_tasks_skill_exists_as_user_facing_tasks_entry() -> None:
    skill_text = (SDLC_SKILLS_HOME / "sdlc-tasks/SKILL.md").read_text(encoding="utf-8")

    assert "$sdlc-prepare" not in skill_text
    assert "$sdlc-brief" not in skill_text
    assert "planning_tasks" in skill_text
    assert "ready_for_development" in skill_text
    assert "$sdlc-task REQ-001 T-001" in skill_text
    assert "codex-sdlc tasks" in skill_text
    assert "`task-plan.v2`" in skill_text
    assert "每份 `task.v2`" in skill_text
    assert "`task-coverage.v1`" in skill_text
    assert "全部 FR、GR、AC、设计产物和已接受变更" in skill_text
    assert "`no_development_items`" in skill_text


@pytest.mark.parametrize(
    "skill_name, required_entry",
    [
        ("sdlc-task", "task-read-confirm"),
        ("sdlc-task-done", "task-run"),
        ("sdlc-goal", "$sdlc-task"),
        ("sdlc-next", "ready_for_development"),
        ("sdlc-status", "developing"),
        ("sdlc-regression", "verifying"),
    ],
)
def test_task_mainline_skills_use_the_direct_execution_contract(
    skill_name: str,
    required_entry: str,
) -> None:
    """七份任务技能必须共用正式审核和 task-run，不能各自保留一套旧阶段。"""

    skill_text = (SDLC_SKILLS_HOME / f"{skill_name}/SKILL.md").read_text(encoding="utf-8")

    assert required_entry in skill_text
    if skill_name == "sdlc-task":
        # 直接开工技能只负责审核通过后的当前任务，不再重复描述规划、回归和验收阶段。
        assert "已通过当前 `task_plan` 审核" in skill_text
        assert "ready_for_development" in skill_text
        assert "developing" in skill_text
        assert "task-run.v1.json" in skill_text
    else:
        assert "planning_tasks" in skill_text
        assert "ready_for_development" in skill_text
        assert "developing" in skill_text
        assert "verifying" in skill_text
        assert "accepted" in skill_text
    assert "$sdlc-prepare" not in skill_text
    assert "$sdlc-brief" not in skill_text
    assert "task-pack.md" not in skill_text


def test_status_and_next_only_advance_a_current_reviewed_task_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务审核是直接开工的唯一准入依据，旧执行包不能改变状态或候选任务。"""

    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    unreviewed_status = run_cli(["status"], cwd=project)
    unreviewed_next = run_cli(["next"], cwd=project)
    assert unreviewed_status.returncode == 0, unreviewed_status.stderr
    assert unreviewed_next.returncode == 0, unreviewed_next.stderr
    assert "[planning_tasks]" in unreviewed_status.stdout
    assert "$sdlc-task REQ-001 T-001" not in unreviewed_next.stdout
    assert "整套任务审核" in unreviewed_next.stdout

    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        build_paths(project),
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )
    old_pack = requirement_root / "task-packs/T-001/task-pack.md"
    old_pack.parent.mkdir(parents=True)
    old_pack.write_text("既有档案，不参与状态和候选任务。\n", encoding="utf-8")

    reviewed = derive_state(build_paths(project))["requirements"]["REQ-001"]
    reviewed_status = run_cli(["status"], cwd=project)
    reviewed_next = run_cli(["next"], cwd=project)
    assert reviewed["status"] == "ready_for_development"
    assert reviewed_status.returncode == 0, reviewed_status.stderr
    assert reviewed_next.returncode == 0, reviewed_next.stderr
    assert "[ready_for_development]" in reviewed_status.stdout
    assert "$sdlc-task REQ-001 T-001" in reviewed_next.stdout
    assert "既有任务执行包档案" in reviewed_status.stdout
    assert "不参与当前任务状态" in reviewed_status.stdout
    assert "执行包" not in reviewed_next.stdout

    # 状态页允许如实展示只读档案，但真正推进需求的只能是审核通过后的直接 task 开工。
    started = run_cli(
        ["task", "REQ-001", "T-001"],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": "直接开发线程"},
    )
    assert started.returncode == 0, started.stderr
    developing = derive_state(build_paths(project))["requirements"]["REQ-001"]
    developing_status = run_cli(["status"], cwd=project)
    developing_next = run_cli(["next"], cwd=project)
    assert developing["status"] == "developing"
    assert developing_status.returncode == 0, developing_status.stderr
    assert developing_next.returncode == 0, developing_next.stderr
    assert "[developing]" in developing_status.stdout
    assert "task-read-confirm REQ-001 T-001" in developing_next.stdout
    for output in (
        reviewed_status.stdout,
        reviewed_next.stdout,
        developing_status.stdout,
        developing_next.stdout,
    ):
        assert "$sdlc-prepare" not in output
        assert "$sdlc-brief" not in output


def _approve_task_plan(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试统一走独立审核服务，避免手写 passed 状态绕过正式输入哈希。"""

    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        build_paths(project),
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )


def _reviewed_direct_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    two_tasks: bool = False,
) -> tuple[Path, Path]:
    """动态生成结构化任务，所有状态探针都通过正式 CLI 运行。"""

    project, requirement_root = _project(tmp_path)
    formal_path = requirement_root / "original/formal.v3.json"
    write_tasks_formal_binding_start_package(formal_path)
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    formal["workflow_profile"] = "document-first.v1"
    formal_path.write_text(json.dumps(formal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    events_path = project / ".codex-sdlc/events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line]
    events[0]["payload"]["native_start"] = formal
    events_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    formal_sha256 = hashlib.sha256(formal_path.read_bytes()).hexdigest()
    reference_index_path = requirement_root / "reference-index.v1.json"
    reference_index = json.loads(reference_index_path.read_text(encoding="utf-8"))
    for entry in reference_index["entries"].values():
        entry["sha256"] = formal_sha256
    reference_index_path.write_text(
        json.dumps(reference_index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    submission = _write_task_submission(
        tmp_path / ("双任务正式输入" if two_tasks else "单任务正式输入"),
        requirement_root,
    )
    if two_tasks:
        _add_second_task_to_submission(submission)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    assert run_tasks_finalize(_task_args(submission)) == 0
    _approve_task_plan(project, monkeypatch)
    return project, requirement_root


def _start_and_confirm_task(
    project: Path,
    requirement_root: Path,
    *,
    task_id: str = "T-001",
    thread_id: str = "直接开发任务",
) -> Path:
    start = run_cli(
        ["task", "REQ-001", task_id],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": thread_id},
    )
    assert start.returncode == 0, start.stderr
    current = json.loads(
        (requirement_root / f"runtime/{task_id}/current.json").read_text(encoding="utf-8")
    )
    manifest = requirement_root / "runtime" / task_id / str(current["manifest_path"])
    confirm = run_cli(
        [
            "task-read-confirm",
            "REQ-001",
            task_id,
            "--manifest-sha256",
            str(current["read_manifest_sha256"]),
        ],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": thread_id},
    )
    assert confirm.returncode == 0, confirm.stderr
    return manifest


def test_active_status_and_next_stop_on_manifest_or_upstream_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """展示状态前必须只读重算正式运行合同，不能等 task-done 才发现失效。"""

    project, requirement_root = _reviewed_direct_project(tmp_path, monkeypatch)
    manifest = _start_and_confirm_task(project, requirement_root)
    manifest_bytes = manifest.read_bytes()

    manifest.unlink()
    for command in ("status", "next"):
        result = run_cli([command], cwd=project)
        assert result.returncode == 0, result.stderr
        assert "$sdlc-task-done REQ-001 T-001" not in result.stdout
        assert "读取清单缺失" in result.stdout

    manifest.write_bytes(manifest_bytes + b" ")
    for command in ("status", "next"):
        result = run_cli([command], cwd=project)
        assert result.returncode == 0, result.stderr
        assert "$sdlc-task-done REQ-001 T-001" not in result.stdout
        assert "读取清单哈希不一致" in result.stdout

    manifest.write_bytes(manifest_bytes)
    rules = project / "AGENTS.md"
    rules.write_text(rules.read_text(encoding="utf-8") + "新增正式项目规则。\n", encoding="utf-8")
    for command in ("status", "next"):
        result = run_cli([command], cwd=project)
        assert result.returncode == 0, result.stderr
        assert "$sdlc-task-done REQ-001 T-001" not in result.stdout
        assert "task_review" in result.stdout
        assert "project_rules" in result.stdout

    current_path = requirement_root / "runtime/T-001/current.json"
    run_path = requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
    assert json.loads(current_path.read_text(encoding="utf-8"))["status"] == "active"
    assert json.loads(run_path.read_text(encoding="utf-8"))["status"] == "active"
    explicit_check = run_cli(
        ["task-run-check", "REQ-001", "T-001"],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": "直接开发任务"},
    )
    assert explicit_check.returncode == 1
    assert "task_review、project_rules" in explicit_check.stderr
    assert json.loads(current_path.read_text(encoding="utf-8"))["status"] == "stale"


def test_status_and_next_stop_when_multiple_task_runs_are_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态层必须列出全部活动身份，不能替调用线程挑选另一条 task-run。"""

    project, _requirement_root = _reviewed_direct_project(
        tmp_path,
        monkeypatch,
        two_tasks=True,
    )
    for task_id, thread_id in (("T-001", "第一开发线程"), ("T-002", "第二开发线程")):
        started = run_cli(
            ["task", "REQ-001", task_id],
            cwd=project,
            extra_env={"CODEX_THREAD_ID": thread_id},
        )
        assert started.returncode == 0, started.stderr

    for command in ("status", "next"):
        result = run_cli([command], cwd=project)
        assert result.returncode == 0, result.stderr
        assert "多个活动 task-run 冲突" in result.stdout
        assert "REQ-001/T-001/run-0001" in result.stdout
        assert "REQ-001/T-002/run-0001" in result.stdout
        assert "task-read-confirm" not in result.stdout
        assert "$sdlc-task-done" not in result.stdout


def test_stale_doing_run_uses_pause_then_task_to_create_next_reading_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进行中轮次只能先暂停再直接重新开工，不能改走 restore。"""

    project, requirement_root = _reviewed_direct_project(tmp_path, monkeypatch)
    _start_and_confirm_task(project, requirement_root, thread_id="第一轮开发线程")
    active = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert active["status"] == "developing"
    assert active["tasks"][0]["status"] == "doing"

    next_result = run_cli(["next"], cwd=project)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-task-done REQ-001 T-001" in next_result.stdout
    assert "task-restore" not in next_result.stdout
    next_skill_text = (SDLC_SKILLS_HOME / "sdlc-next/SKILL.md").read_text(encoding="utf-8")
    assert "`stale`、缺失或身份冲突时给出明确停止原因" in next_skill_text
    assert "task-restore" not in next_skill_text
    goal_skill_text = (SDLC_SKILLS_HOME / "sdlc-goal/SKILL.md").read_text(encoding="utf-8")
    assert "按正式暂停和恢复入口处理上游漂移" in goal_skill_text
    assert "再创建递增的新轮次" in goal_skill_text
    assert "旧轮次和证据保持只读" in goal_skill_text

    # task-pause 的正式准入是尚未关闭的 reading 或 active 轮次；暂停会把旧轮次标成
    # stale 并把任务退回 todo，随后才能由 task 创建新的 reading 轮次。
    paused = run_cli(
        ["task-pause", "REQ-001", "T-001", "--reason", "当前轮次停止，准备重新开工"],
        cwd=project,
    )
    assert paused.returncode == 0, paused.stderr
    paused_state = derive_state(build_paths(project))["requirements"]["REQ-001"]
    old_run = json.loads(
        (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    paused_next = run_cli(["next"], cwd=project)
    assert paused_state["status"] == "ready_for_development"
    assert paused_state["tasks"][0]["status"] == "todo"
    assert old_run["status"] == "stale"
    assert paused_next.returncode == 0, paused_next.stderr
    assert "$sdlc-task REQ-001 T-001" in paused_next.stdout
    assert "task-restore" not in paused_next.stdout

    restarted = run_cli(
        ["task", "REQ-001", "T-001"],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": "第二轮开发线程"},
    )
    assert restarted.returncode == 0, restarted.stderr
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    assert current["run_number"] == 2
    assert current["status"] == "reading"
    restarted_state = derive_state(build_paths(project))["requirements"]["REQ-001"]
    assert restarted_state["status"] == "developing"


def test_active_handoff_lists_task_done_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task-done 必须先命中专用交接分支，不能再被通用 task 前缀重复匹配。"""

    project, requirement_root = _reviewed_direct_project(tmp_path, monkeypatch)
    _start_and_confirm_task(project, requirement_root)

    result = run_cli(["handoff"], cwd=project)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("$sdlc-task-done REQ-001 T-001") == 1
    assert "task-pack" not in result.stdout


def write_tasks_formal_binding_start_package(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "title": "企业 Key 权限管理",
                "slug": "enterprise-key-permission",
                "description": "管理员可以创建企业 Key，普通成员只能查看自己有权限的 Key。",
                "background": "企业后台需要把 Key 创建能力限制给管理员。",
                "goal": "管理员能创建 Key，普通成员不能越权创建。",
                "scope": ["管理员创建 Key", "普通成员创建拦截"],
                "out_of_scope": ["本轮不做 Key 轮换", "本轮不做批量导入 Key"],
                "business_rules": ["Key 值只在创建成功后展示一次"],
                "user_scenarios": ["管理员在企业后台创建 Key，普通成员尝试创建时被拦截。"],
                "permission_rules": ["仅管理员可创建企业 Key。"],
                "data_state_rules": ["Key 创建后记录创建人、到期时间和 active 状态。"],
                "interface_scope": ["POST /api/enterprise/keys", "GET /api/enterprise/keys"],
                "exception_rules": ["普通成员创建返回 403。"],
                "test_focus": ["覆盖管理员成功创建、普通成员越权拦截和 Key 只展示一次。"],
                "functional_requirements": [
                    {
                        "id": "FR-001",
                        "title": "管理员创建企业 Key",
                        "description": "管理员在企业后台创建 Key，并得到可复制的 Key 值。",
                        "rules": ["同一企业下 Key 名称不能重复"],
                        "inputs": ["Key 名称和到期时间"],
                        "outputs": ["创建后的 Key 值和 active 状态"],
                        "triggers": ["管理员提交创建表单"],
                        "data_changes": ["新增 Key，并保存创建人、到期时间和 active 状态"],
                        "permissions": ["仅管理员可创建企业 Key"],
                        "exceptions": ["名称为空时提示必填"],
                        "boundaries": ["本轮不做 Key 轮换"],
                        "acceptance_ids": ["AC-001"],
                    },
                    {
                        "id": "FR-002",
                        "title": "普通成员禁止创建 Key",
                        "description": "普通成员访问创建入口时不能提交创建请求。",
                        "rules": ["普通成员不展示创建按钮"],
                        "inputs": ["当前登录成员角色"],
                        "outputs": ["隐藏创建按钮，直接调用接口时返回 403"],
                        "triggers": ["普通成员打开 Key 管理页或调用创建接口"],
                        "data_changes": ["不涉及：拒绝操作时不写入 Key"],
                        "permissions": ["普通成员不能创建企业 Key"],
                        "exceptions": ["直接调用创建接口返回 403"],
                        "boundaries": ["本轮不做批量导入 Key"],
                        "acceptance_ids": ["AC-002"],
                    },
                ],
                "acceptance_criteria": [
                    {
                        "id": "AC-001",
                        "title": "管理员创建成功",
                        "requirement_ids": ["FR-001"],
                        "operation": "管理员填写 Key 名称并提交",
                        "expected": "页面显示创建成功，并展示 Key 值",
                        "pass_standard": "Key 可复制，刷新后仍能在列表看到",
                    },
                    {
                        "id": "AC-002",
                        "title": "普通成员被拦截",
                        "requirement_ids": ["FR-002"],
                        "operation": "普通成员打开 Key 管理页",
                        "expected": "页面不展示创建按钮",
                        "pass_standard": "直接调用创建接口会返回 403",
                    },
                ],
                "test_cases": [
                    {
                        "id": "TC-001",
                        "type": "integration_test",
                        "description": "验证管理员创建 Key 的完整链路",
                        "operation": "调用创建 Key 接口后查询 Key 列表",
                        "expected": "接口返回成功，列表包含新 Key",
                        "pass_standard": "返回码、Key 名称和权限范围都符合预期",
                        "requirement_ids": ["FR-001"],
                        "acceptance_ids": ["AC-001"],
                    },
                    {
                        "id": "TC-002",
                        "type": "negative_regression",
                        "description": "验证普通成员不能创建 Key",
                        "operation": "普通成员调用创建 Key 接口",
                        "expected": "接口返回 403",
                        "pass_standard": "不会写入 Key 记录",
                        "requirement_ids": ["FR-002"],
                        "acceptance_ids": ["AC-002"],
                    },
                ],
                "design": {
                    "title": "企业 Key 权限方案",
                    "summary": "在现有企业后台里新增 Key 创建和列表读取逻辑。",
                    "technical_goal": "完成企业 Key 创建、列表读取和角色权限隔离。",
                    "modules": ["企业后台 Key 页面", "Key 管理接口"],
                    "data_structures": ["Key 记录名称、创建人、到期时间和 active 状态"],
                    "interfaces": ["POST /api/enterprise/keys", "GET /api/enterprise/keys"],
                    "state_flow": ["创建成功后进入 active 状态"],
                    "data_flow": ["页面提交 Key 信息，接口校验管理员权限后写入并返回 Key 值"],
                    "permissions_security": ["仅管理员可以创建企业 Key；普通成员不能创建企业 Key"],
                    "error_handling": ["权限不足返回 403，名称重复返回 409"],
                    "test_strategy": ["补接口集成测试和权限反向测试"],
                    "risks": ["Key 值泄露；创建成功后只展示一次"],
                    "out_of_scope": ["本轮不做 Key 轮换"],
                    "requirement_coverage": ["FR-001：覆盖管理员创建", "FR-002：覆盖普通成员拦截"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_formal_v3_package(path, json.loads(path.read_text(encoding="utf-8")))


def test_tasks_bind_to_formal_current_documents_and_render_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_dir = _import_task_plan(tmp_path, monkeypatch)

    task_text = (requirement_dir / "tasks/T-001.md").read_text(encoding="utf-8")
    coverage = json.loads((requirement_dir / "task-coverage.v1.json").read_text(encoding="utf-8"))
    assert "## 任务目标" in task_text
    assert "交付可复核的任务覆盖和规划证据" in task_text
    assert "## 代码和模块范围" in task_text
    assert "## 自动测试" in task_text
    assert "## 人工验收点" in task_text
    assert "## 不包含内容" in task_text
    assert coverage["functional_requirements"]["FR-001"]["tasks"] == ["T-001"]
    assert coverage["acceptance_criteria"]["AC-001"]["tasks"] == ["T-001"]
    assert coverage["acceptance_criteria"]["AC-001"]["test_refs"] == ["T-001#automated_tests/0"]
    assert derive_state(build_paths(project))["requirements"]["REQ-001"]["status"] == "planning_tasks"

def test_tasks_derive_acceptance_for_old_task_packages(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(
        project_dir,
        title="企业 Key 权限管理",
        with_tasks=False,
    )
    legacy_package = tmp_path / "旧任务拆分包.json"
    legacy_package.write_text('{"tasks": []}\n', encoding="utf-8")

    tasks_result = run_cli(
        ["tasks", "REQ-001", "--file", str(legacy_package)],
        cwd=project_dir,
    )

    assert tasks_result.returncode == 2
    assert "--plan-file" in tasks_result.stderr
    assert "--tasks-dir" in tasks_result.stderr
    assert "--coverage-file" in tasks_result.stderr
    requirement = derive_state(build_paths(project_dir))["requirements"]["REQ-001"]
    assert requirement["tasks"] == []

def test_user_facing_entrypoints_describe_native_flow() -> None:
    files = [
        REPO_ROOT / "README.md",
        (SDLC_SKILLS_HOME / "sdlc-init/SKILL.md"),
        (SDLC_SKILLS_HOME / "sdlc-discuss/SKILL.md"),
        (SDLC_SKILLS_HOME / "sdlc-design/SKILL.md"),
        (SDLC_SKILLS_HOME / "sdlc-start/SKILL.md"),
        (SDLC_SKILLS_HOME / "sdlc-tasks/SKILL.md"),
    ]

    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "sdlc-" in text or "$sdlc" in text or "codex-sdlc" in text


def test_manual_section_is_preserved_when_snapshots_are_repaired(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")

    requirement_file = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*/requirement.md"))
    requirement_text = requirement_file.read_text(encoding="utf-8")
    assert "## 人工补充" in requirement_text
    requirement_file.write_text(
        requirement_text.replace("- 暂无人工补充", "- 用户补充：导出字段以后可能增加订单来源。"),
        encoding="utf-8",
    )

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)
    assert repair_result.returncode == 0, repair_result.stderr

    repaired_text = requirement_file.read_text(encoding="utf-8")
    assert "## 人工补充" in repaired_text
    assert "用户补充：导出字段以后可能增加订单来源" in repaired_text


def test_deep_doctor_reports_manual_change_outside_manual_section(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")

    task_file = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*/tasks/T-001.md"))
    task_file.write_text(
        task_file.read_text(encoding="utf-8").replace("- 状态：todo", "- 状态：done"),
        encoding="utf-8",
    )

    deep_result = run_cli(["doctor-deep"], cwd=project_dir)
    assert deep_result.returncode == 0, deep_result.stderr
    assert "生成区被手动修改" in deep_result.stdout
    assert "不会自动进入主状态" in deep_result.stdout


def test_deep_doctor_reports_missing_events_without_silent_recovery(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    events_file = project_dir / ".codex-sdlc" / "events.jsonl"
    events_file.rename(project_dir / ".codex-sdlc" / "events.jsonl.missing-test")

    deep_result = run_cli(["doctor-deep"], cwd=project_dir)
    assert deep_result.returncode != 0
    assert "events.jsonl 缺失" in deep_result.stdout
    assert "恢复备份" in deep_result.stdout


def test_events_are_backed_up_before_new_events_are_appended(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")

    backup_files = sorted((project_dir / ".codex-sdlc" / "backups").glob("events-*.jsonl.bak"))
    assert backup_files
    assert "project_initialized" in backup_files[-1].read_text(encoding="utf-8")


def test_doctor_repair_cleans_transient_files_and_prunes_backups(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    sdlc_dir = project_dir / ".codex-sdlc"
    (sdlc_dir / ".DS_Store").write_text("macos", encoding="utf-8")
    backups_dir = sdlc_dir / "backups"
    backups_dir.mkdir(exist_ok=True)
    for index in range(12):
        (backups_dir / f"events-202606041200{index:02d}.jsonl.bak").write_text("backup\n", encoding="utf-8")

    repair_result = run_cli(["doctor-repair"], cwd=project_dir)

    assert repair_result.returncode == 0, repair_result.stderr
    assert not (sdlc_dir / ".DS_Store").exists()
    assert len(list(backups_dir.glob("events-*.jsonl.bak"))) <= 10


def test_requirement_point_text_is_not_split_by_free_text_numbering() -> None:
    source = "7. 语速继续使用已有 0.75x、1.0x、1.25x、1.5x、2.0x 能力。8. 搜索只进入页面。"
    parts = split_requirement_point_text(source)

    assert parts == [source]
    second = "正确逻辑：1. 单次切换自由。2. 再次进入按默认规则。"
    assert split_requirement_point_text(second) == [second]


def test_init_installs_optional_hooks_and_rules_assets(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    init_result = run_cli(["init"], cwd=project_dir)
    assert init_result.returncode == 0, init_result.stderr

    hooks_json = project_dir / ".codex" / "hooks.json"
    rules_file = project_dir / ".codex" / "rules" / "default.rules"
    assert hooks_json.exists()
    assert rules_file.exists()

    session_start_hook = project_dir / ".codex" / "hooks" / "sdlc_session_start.py"
    user_prompt_hook = project_dir / ".codex" / "hooks" / "sdlc_user_prompt_submit.py"
    stop_hook = project_dir / ".codex" / "hooks" / "sdlc_stop.py"
    assert session_start_hook.exists()
    assert user_prompt_hook.exists()
    assert stop_hook.exists()

    prompt_result = run_python_hook(
        user_prompt_hook,
        {
            "cwd": str(project_dir),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "普通依赖问题，不是 SDLC 任务",
            "session_id": "plain-session",
        },
    )
    assert prompt_result.returncode == 0, prompt_result.stderr
    assert prompt_result.stdout == ""

    sdlc_prompt_result = run_python_hook(
        user_prompt_hook,
        {
            "cwd": str(project_dir),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "$sdlc-status",
            "session_id": "sdlc-session",
        },
    )
    assert sdlc_prompt_result.returncode == 0, sdlc_prompt_result.stderr
    assert "additionalContext" in sdlc_prompt_result.stdout
    assert "当前会话已经通过显式 SDLC 命令进入流程" in sdlc_prompt_result.stdout

    stop_result = run_python_hook(
        stop_hook,
        {
            "cwd": str(project_dir),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "session_id": "sdlc-session",
        },
    )
    assert stop_result.returncode == 0, stop_result.stderr
    stop_payload = json.loads(stop_result.stdout)
    assert stop_payload["continue"] is True
    assert "additionalContext" in stop_payload
    assert "codex-sdlc finish" not in stop_result.stdout


def test_init_upgrades_generated_user_prompt_hook_without_overwriting_custom_file(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0

    user_prompt_hook = project_dir / ".codex" / "hooks" / "sdlc_user_prompt_submit.py"
    user_prompt_hook.write_text(
        "#!/usr/bin/env python3\n"
        "# 当前目录已经在用 SDLC 记录状态。\n"
        "print('old generated hook')\n",
        encoding="utf-8",
    )

    assert run_cli(["init"], cwd=project_dir).returncode == 0

    upgraded_text = user_prompt_hook.read_text(encoding="utf-8")
    assert "ready_for_user_check" not in upgraded_text
    assert "$sdlc-" in upgraded_text

    user_prompt_hook.write_text("#!/usr/bin/env python3\nprint('user custom hook')\n", encoding="utf-8")

    assert run_cli(["init"], cwd=project_dir).returncode == 0

    assert "user custom hook" in user_prompt_hook.read_text(encoding="utf-8")


def test_hooks_upgrade_repairs_generated_hooks_even_when_sdlc_identity_mismatches(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    assert run_cli(["status"], cwd=project_dir).returncode == 0
    identity_file = project_dir / ".codex-sdlc" / "identity.json"
    identity = json.loads(identity_file.read_text(encoding="utf-8"))
    identity["branch_key"] = "branch_wrong_for_test"
    identity_file.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    status_result = run_cli(["status"], cwd=project_dir)
    assert status_result.returncode == 1
    assert "身份和当前 Git 状态不一致" in status_result.stderr

    stop_hook = project_dir / ".codex" / "hooks" / "sdlc_stop.py"
    stop_hook.write_text(
        "#!/usr/bin/env python3\n"
        "# 检测到本轮还有代码改动\n"
        "# codex-sdlc finish\n"
        "print('old generated stop hook')\n",
        encoding="utf-8",
    )

    upgrade_result = run_cli(["hooks-upgrade"], cwd=project_dir)
    assert upgrade_result.returncode == 0, upgrade_result.stderr
    assert "should_emit_sdlc_context" in stop_hook.read_text(encoding="utf-8")
