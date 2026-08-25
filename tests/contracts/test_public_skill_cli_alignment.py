from __future__ import annotations

from pathlib import Path
import shlex

import pytest

from codex_sdlc.cli import build_parser


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "skills"

# 这些命令就是五个公开技能给用户展示的可执行示例。测试既检查技能正文，
# 也交给正式解析器读取，避免文档看起来合理、实际参数却不能使用。
SKILL_COMMANDS = {
    "sdlc-material": (
        "codex-sdlc material DRAFT-001 --title 原始需求 --type requirement "
        "--file docs/需求.md --role requirement --scope 当前需求",
    ),
    "sdlc-discuss": (
        "codex-sdlc draft create 订单导出",
        "codex-sdlc draft requirements DRAFT-001 "
        "--split-file tmp/requirement-split.v1.json "
        "--coverage-file tmp/requirement-coverage.v1.json",
        "codex-sdlc draft requirement-review create DRAFT-001",
        "codex-sdlc draft requirement-review status DRAFT-001 --review REV-001",
        "codex-sdlc review submit --request REV-001 "
        "--file tmp/requirement-review-result.v1.json",
        "codex-sdlc draft requirement-confirm DRAFT-001 --review REV-001",
        "codex-sdlc draft status DRAFT-001",
    ),
    "sdlc-design": (
        "codex-sdlc design-reference DRAFT-001 --file tmp/design-reference.v1.json",
        "codex-sdlc design-plan DRAFT-001 --file tmp/design-plan.v1.json",
        "codex-sdlc design-artifact DRAFT-001 "
        "--file tmp/PAGE-001.design-artifact.v1.json",
        "codex-sdlc design-summary DRAFT-001 --file tmp/design-summary.v1.json",
        "codex-sdlc review create --review-id REV-002 "
        "--stage integrated_design --owner DRAFT-001 "
        "--input .codex-sdlc/drafts/DRAFT-001/设计/design-plan.v1.json",
        "codex-sdlc review submit --request REV-002 "
        "--file tmp/design-review-result.v1.json",
        "codex-sdlc review status --review REV-002",
        "codex-sdlc draft refresh DRAFT-001",
        "codex-sdlc draft status DRAFT-001",
    ),
    "sdlc-design-accept": (
        "codex-sdlc design-reference-confirm DRAFT-001 DES-001",
        "codex-sdlc draft status DRAFT-001",
    ),
    "sdlc-start": (
        "codex-sdlc start --file tmp/formal.v3.json",
        "codex-sdlc status",
    ),
}


@pytest.mark.parametrize(
    ("skill_name", "commands"),
    tuple(SKILL_COMMANDS.items()),
)
def test_t046_public_skill_commands_match_the_real_cli(
    skill_name: str,
    commands: tuple[str, ...],
) -> None:
    skill_text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    parser = build_parser()

    for command in commands:
        assert command in skill_text
        parsed = parser.parse_args(shlex.split(command)[1:])
        assert callable(parsed.func)


def test_t046_public_skills_describe_inputs_outputs_blockers_and_stop_points() -> None:
    for skill_name in SKILL_COMMANDS:
        skill_text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "## 输入" in skill_text
        assert "## 输出" in skill_text
        assert "## 阻塞条件" in skill_text
        assert "## 停止位置" in skill_text


def test_t046_public_skills_do_not_restore_fact_or_fixed_markdown_stages() -> None:
    joined = "\n".join(
        (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
        for skill_name in SKILL_COMMANDS
    )
    forbidden = (
        "requirement.facts.json",
        "design.facts.json",
        "model-review.json",
        "fact_bundle",
        "facts freeze",
        "draft facts",
        "draft source-index",
        "draft model-review",
        "draft review-request",
        "推荐草稿结构",
        "## 背景和目标",
        "codex-sdlc design DES-",
        "codex-sdlc design-accept",
        "codex-sdlc material REQ-",
    )
    assert [marker for marker in forbidden if marker in joined] == []


def test_t046_start_skill_uses_the_document_first_manifest_only() -> None:
    skill_text = (SKILL_ROOT / "sdlc-start" / "SKILL.md").read_text(encoding="utf-8")
    for field in (
        "formal_contract_version",
        "workflow_profile",
        "source_draft_id",
        "source_revision_sha256",
        "reviews",
        "artifact_index",
        "artifact_manifest",
        "open_questions",
    ):
        assert field in skill_text
    assert "formal.v3 只保存清单和引用" in skill_text
