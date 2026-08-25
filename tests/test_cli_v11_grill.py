from __future__ import annotations

import sqlite3
from pathlib import Path

from test_cli_v1 import create_minimal_requirement_by_start_file, init_demo_repo, read_events, run_cli


def test_grill_records_global_question_before_requirement_exists(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0

    result = run_cli(
        [
            "grill",
            "需求想法已经能说明目标，本轮没有必须打断用户的问题。",
            "--mode",
            "requirement",
            "--status",
            "no_issue",
            "--question",
            "目标用户和使用场景是否足够清楚？",
            "--answer",
            "用户确认当前没有补充，可以进入需求整理。",
            "--source",
            "用户回答",
        ],
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "已记录质询：GRILL-001" in result.stdout
    grill_file = project_dir / ".codex-sdlc" / "grills" / "GRILL-001.md"
    assert grill_file.exists()
    grill_text = grill_file.read_text(encoding="utf-8")
    assert "无需追问" in grill_text
    assert "目标用户和使用场景是否足够清楚" in grill_text
    assert any(event["event_type"] == "grill_recorded" for event in read_events(project_dir))


def test_non_goal_grill_only_requires_explicit_answer_field(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0

    missing_answer = run_cli(
        ["grill", "需求已经清楚，可以继续。", "--mode", "requirement", "--status", "no_issue"],
        cwd=project_dir,
    )
    assert missing_answer.returncode == 1
    assert "必须带用户回答" in missing_answer.stderr

    self_answer = run_cli(
        [
            "grill",
            "主 agent 自答：需求已经清楚，可以继续。",
            "--mode",
            "design",
            "--status",
            "resolved",
            "--answer",
            "主 agent 自答，没有问题。",
        ],
        cwd=project_dir,
    )
    assert self_answer.returncode == 0, self_answer.stderr
    assert "已记录质询：GRILL-001" in self_answer.stdout


def test_goal_grill_allows_agent_self_answer(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0

    result = run_cli(
        [
            "grill",
            "主 agent 自答：执行包和代码线索足够，本轮不需要用户补决策。",
            "--mode",
            "goal",
            "--status",
            "no_issue",
            "--answer",
            "主 agent 自答，没有阻塞问题。",
            "--source",
            "主 agent 自答",
        ],
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "已记录质询：GRILL-001" in result.stdout


def test_grill_records_requirement_and_task_question_inside_requirement_package(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能，支持按时间范围导出 Excel。")

    result = run_cli(
        [
            "grill",
            "T-001 的规划范围清楚，可以继续创建整套任务审核。",
            "--requirement",
            "REQ-001",
            "--task",
            "T-001",
            "--mode",
            "task_plan",
            "--status",
            "resolved",
            "--question",
            "任务目标是否足够具体？",
            "--answer",
            "当前任务只处理订单导出的最小实现，测试点已经绑定到 T-001。",
            "--recommendation",
            "继续创建整套任务审核。",
        ],
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    grill_file = requirement_dir / "grills" / "GRILL-001.md"
    index_file = requirement_dir / "grills" / "index.md"
    assert grill_file.exists()
    assert index_file.exists()
    grill_text = grill_file.read_text(encoding="utf-8")
    assert "task_plan" in grill_text
    assert "任务：T-001" in grill_text
    assert "继续创建整套任务审核" in grill_text
    assert "GRILL-001" in index_file.read_text(encoding="utf-8")
    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        row = connection.execute("SELECT requirement_id, task_id, mode, status FROM grills").fetchone()
    assert row == ("REQ-001", "T-001", "task_plan", "resolved")
