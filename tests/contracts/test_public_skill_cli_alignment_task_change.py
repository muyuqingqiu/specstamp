from __future__ import annotations

from pathlib import Path
import shlex

import pytest

from codex_sdlc.cli import build_parser


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills"

# 这些命令就是六个保留技能公开给使用者的真实命令。每条示例都交给正式解析器，
# 防止技能正文继续展示已经改名的参数或已经下线的中间阶段。
SKILL_COMMANDS = {
    "sdlc-tasks": (
        "codex-sdlc tasks REQ-001 --plan-file tmp/task-plan.v2.json "
        "--tasks-dir tmp/tasks --coverage-file tmp/task-coverage.v1.json",
        "codex-sdlc review create --review-id REV-001 --stage task_plan "
        "--owner REQ-001 --input .codex-sdlc/requirements/REQ-001/tasks/task-plan.v2.json",
        "codex-sdlc review submit --request REV-001 --file tmp/task-review-result.v1.json",
        "codex-sdlc review status --review REV-001",
        "codex-sdlc status",
        "codex-sdlc next",
    ),
    "sdlc-task": (
        "codex-sdlc status",
        "codex-sdlc next",
        "codex-sdlc task REQ-001 T-001",
        "codex-sdlc task-read-confirm REQ-001 T-001 --manifest-sha256 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "codex-sdlc task-run-check REQ-001 T-001",
    ),
    "sdlc-change": (
        "codex-sdlc change-create REQ-001 --request-key change-request-001",
        "codex-sdlc change-material REQ-001 CHG-001 --type requirement "
        "--file docs/变更说明.md",
        "codex-sdlc change-package REQ-001 CHG-001 "
        "--package tmp/change-package.v1.json "
        "--projected-requirement tmp/projected-requirement.v2.json "
        "--projected-design tmp/projected-design.v2.json "
        "--projected-test-matrix tmp/projected-test-matrix.v2.json "
        "--projected-reference-index tmp/projected-reference-index.v2.json "
        "--projected-task-plan tmp/projected-task-plan.v2.json",
        "codex-sdlc review create --review-id REV-002 --stage requirement_split "
        "--owner CHG-001 --input tmp/projected-requirement.v2.json",
        "codex-sdlc review submit --request REV-002 --file tmp/change-review-result.v1.json",
        "codex-sdlc review status --review REV-002",
    ),
    "sdlc-next": ("codex-sdlc next",),
    "sdlc-status": ("codex-sdlc status",),
    "sdlc-goal": (
        "codex-sdlc status",
        "codex-sdlc next",
        "codex-sdlc task REQ-001 T-001",
        "codex-sdlc task-run-check REQ-001 T-001",
    ),
}


@pytest.mark.parametrize(
    ("skill_name", "commands"),
    tuple(SKILL_COMMANDS.items()),
)
def test_t047_public_skill_commands_match_the_real_cli(
    skill_name: str,
    commands: tuple[str, ...],
) -> None:
    skill_text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    parser = build_parser()

    for command in commands:
        assert command in skill_text
        parsed = parser.parse_args(shlex.split(command)[1:])
        assert callable(parsed.func)


def test_t047_public_skills_describe_the_complete_execution_boundary() -> None:
    for skill_name in SKILL_COMMANDS:
        skill_text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "## 输入" in skill_text
        assert "## 输出" in skill_text
        assert "## 阻塞条件" in skill_text
        assert "## 审核点" in skill_text
        assert "## 停止位置" in skill_text


def test_t047_tasks_skill_uses_three_structured_inputs_and_one_task_review() -> None:
    skill_text = (SKILL_ROOT / "sdlc-tasks" / "SKILL.md").read_text(encoding="utf-8")
    for marker in (
        "task-plan.v2",
        "task.v2",
        "task-coverage.v1",
        "--plan-file",
        "--tasks-dir",
        "--coverage-file",
        "--stage task_plan",
    ):
        assert marker in skill_text


def test_t047_task_skill_starts_directly_and_uses_the_current_run() -> None:
    skill_text = (SKILL_ROOT / "sdlc-task" / "SKILL.md").read_text(encoding="utf-8")
    for marker in (
        "task-read-manifest.v1.json",
        "task-run.v1.json",
        "task-read-confirm",
        "task-run-check",
        "reading",
        "active",
        "stale",
    ):
        assert marker in skill_text


def test_t047_change_skill_uses_workspace_and_five_complete_projections() -> None:
    skill_text = (SKILL_ROOT / "sdlc-change" / "SKILL.md").read_text(encoding="utf-8")
    for marker in (
        "change-package.v1",
        "projected-requirement.v2",
        "projected-design.v2",
        "projected-test-matrix.v2",
        "projected-reference-index.v2",
        "projected-task-plan.v2",
        "base_versions",
        "review_impacts",
    ):
        assert marker in skill_text


def test_t047_public_skills_do_not_restore_retired_or_free_text_stages() -> None:
    joined = "\n".join(
        (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        for skill_name in SKILL_COMMANDS
    )
    forbidden = (
        "$sdlc-prepare",
        "$sdlc-brief",
        "$sdlc-brief-augment",
        "codex-sdlc prepare",
        "codex-sdlc brief",
        "codex-sdlc brief-review",
        "codex-sdlc brief-augment",
        "codex-sdlc tasks REQ-001 --file",
        "codex-sdlc change REQ-001",
        "task-packs/",
    )
    assert [marker for marker in forbidden if marker in joined] == []
