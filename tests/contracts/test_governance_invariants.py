from __future__ import annotations

from pathlib import Path

from codex_sdlc.core import draft_lifecycle, start_contract


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
T042_PUBLIC_SKILLS = (
    "sdlc-material",
    "sdlc-accept",
    "sdlc-audit",
    "sdlc-fix",
    "sdlc-capture-link",
    "sdlc-capture-requirement",
    "sdlc-context",
    "sdlc-design-accept",
    "sdlc-doctor-deep",
    "sdlc-grill",
    "sdlc-lessons",
)
RETIRED_STAGE_MARKERS = (
    "sdlc-prepare",
    "sdlc-brief",
    "brief-augment",
    "brief-review",
    "task-pack",
    "task_pack",
    "任务执行包",
    "任务包",
)


def test_t042_public_skills_do_not_recommend_retired_task_pack_stages() -> None:
    """逐份固定责任技能，避免用全仓扫描掩盖公开入口里的死链。"""

    violations: list[str] = []
    for skill_name in T042_PUBLIC_SKILLS:
        skill_file = REPOSITORY_ROOT / "skills" / skill_name / "SKILL.md"
        skill_text = skill_file.read_text(encoding="utf-8").lower()
        for marker in RETIRED_STAGE_MARKERS:
            if marker.lower() in skill_text:
                violations.append(f"{skill_name}: {marker}")

    assert violations == []




def test_draft_assessment_does_not_read_question_state_from_markdown() -> None:
    """Markdown 是展示内容；问题状态只认结构化 questions。"""

    assessment = draft_lifecycle.assess_draft(
        {
            "draft_id": "DRAFT-001",
            "title": "订单导出",
            "status": "start_ready",
            "requirement_body": "# 需求草稿\n\n## 待确认问题\n\n- 导出字段是否需要脱敏？\n",
            "design_body": "# 技术草稿\n\n## 技术目标\n\n- 导出订单。\n",
            "questions": [],
        }
    )

    assert assessment.effective_status == "discussing"
    assert assessment.can_start is False
    assert assessment.next_action == "$sdlc-discuss 继续完善需求草案"
    assert assessment.open_questions == ()


def test_raw_start_contract_does_not_allow_summary_to_fill_fr_title_or_description() -> None:
    """原始输入缺字段必须在归一化之前失败，避免摘要悄悄补出正式字段。"""

    issues = start_contract.raw_start_package_contract_issues(
        {
            "functional_requirements": [
                {"id": "FR-001", "summary": "订单导出", "rules": ["使用当前筛选条件。"]}
            ]
        }
    )

    assert "FR-001 缺少原始标题。" in issues
    assert "FR-001 缺少原始说明。" in issues


def test_design_contract_rejects_summary_only_and_unexplained_not_applicable_sections() -> None:
    """正式技术方案必须具备可执行章节，不能把一句摘要当成技术设计。"""

    summary_only_issues = start_contract.design_contract_issues({"summary": "复用现有导出接口。"})
    not_applicable_issues = start_contract.design_contract_issues(
        {
            "technical_goal": "完成订单导出。",
            "modules": ["不涉及"],
            "data_structures": ["不涉及：不新增数据。"],
            "interfaces": ["不涉及：复用现有接口。"],
            "state_flow": ["不涉及：无状态变化。"],
            "data_flow": ["不涉及：无新增数据流。"],
            "permissions_security": ["不涉及：权限规则不变。"],
            "error_handling": ["不涉及：错误处理不变。"],
            "test_strategy": ["不涉及：复用现有回归。"],
            "risks": ["不涉及：没有新增风险。"],
            "out_of_scope": ["不涉及：没有额外不做范围。"],
            "requirement_coverage": ["FR-001：复用现有实现，不需要代码改动。"],
        }
    )

    assert "正式技术方案缺少技术目标。" in summary_only_issues
    assert "正式技术方案缺少涉及模块。" in summary_only_issues
    assert not_applicable_issues == []
