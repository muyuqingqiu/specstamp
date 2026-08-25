from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from codex_sdlc.core import draft_sections
from codex_sdlc.core.markdown_contract import (
    MarkdownBlock,
    extract_public_ids,
    markdown_clean_lines,
    markdown_heading_blocks,
    markdown_labeled_ids,
    markdown_labeled_text,
    markdown_labeled_values,
    markdown_section_body,
    strip_list_marker,
)


PENDING_MARKERS = ("__PENDING__",)
EXCEPTION_HEADINGS = ("异常和边界情况", "异常和边界", "异常和回退规则", "异常处理", "错误处理", "边界情况")


@dataclass(frozen=True)
class ParsedRequirementDraft:
    body: str
    fr_blocks: list[MarkdownBlock]
    ac_blocks: list[MarkdownBlock]
    tc_blocks: list[MarkdownBlock]


def is_placeholder_text(text: str) -> bool:
    return str(text or "").strip() in PENDING_MARKERS


def section_clean_lines(markdown: str, headings: tuple[str, ...]) -> list[str]:
    return markdown_clean_lines(
        markdown,
        headings,
        pending_markers=PENDING_MARKERS,
    )


def parse_requirement_draft(markdown: str) -> ParsedRequirementDraft:
    return ParsedRequirementDraft(
        body=markdown,
        fr_blocks=markdown_heading_blocks(markdown, ("功能需求",), "FR"),
        ac_blocks=markdown_heading_blocks(markdown, ("验收标准",), "AC"),
        tc_blocks=markdown_heading_blocks(markdown, ("测试矩阵", "测试用例"), "TC"),
    )


def requirement_missing_items(markdown: str) -> list[str]:
    parsed = parse_requirement_draft(markdown)
    missing: list[str] = []
    if not draft_sections.requirement_section_clean_lines(markdown, "user_scenarios", pending_markers=PENDING_MARKERS):
        missing.append("用户和使用场景")
    if not draft_sections.requirement_section_clean_lines(markdown, "scope", pending_markers=PENDING_MARKERS):
        missing.append("本轮范围和不做范围")
    elif not draft_sections.requirement_section_clean_lines(markdown, "out_of_scope", pending_markers=PENDING_MARKERS):
        missing.append("不做范围")
    if not parsed.fr_blocks:
        missing.append("功能需求 FR")
    if not parsed.ac_blocks:
        missing.append("验收标准 AC")
    if not parsed.tc_blocks:
        missing.append("测试矩阵 TC")
    if not draft_sections.requirement_section_clean_lines(markdown, "interface_scope", pending_markers=PENDING_MARKERS):
        missing.append("接口或页面范围")
    if not draft_sections.requirement_section_clean_lines(markdown, "permission_rules", pending_markers=PENDING_MARKERS):
        missing.append("权限规则")
    if not draft_sections.requirement_section_clean_lines(markdown, "test_focus", pending_markers=PENDING_MARKERS):
        missing.append("测试关注点")
    return list(dict.fromkeys(missing))


EXPLICIT_PERMISSION_LABELS = ("主体", "方向", "动作", "资源")


def explicit_permission_field_issues(lines: list[str], owner: str) -> list[str]:
    """只检查用户明确选择的权限标签语法，不从普通中文里猜业务语义。"""

    issues: list[str] = []
    for raw_line in lines:
        text = strip_list_marker(str(raw_line or "").strip()).rstrip("。")
        if not text.startswith("主体："):
            continue
        fields: dict[str, str] = {}
        for part in re.split(r"[；;]", text):
            if "：" not in part:
                continue
            label, value = part.split("：", 1)
            label = label.strip()
            if label in EXPLICIT_PERMISSION_LABELS:
                fields[label] = value.strip()
        for label in EXPLICIT_PERMISSION_LABELS:
            if not fields.get(label):
                issues.append(f"{owner}明确权限字段缺少{label}。")
        direction = fields.get("方向", "")
        if direction and direction not in {"allow", "deny"}:
            issues.append(f"{owner}明确权限字段的方向只能是 allow 或 deny。")
    return list(dict.fromkeys(issues))


def executable_acceptance_issues(markdown: str) -> list[str]:
    issues: list[str] = []
    for block in parse_requirement_draft(markdown).ac_blocks:
        missing: list[str] = []
        if not markdown_labeled_ids(block.body, ("覆盖需求", "关联需求", "需求关联"), "FR"):
            missing.append("覆盖需求")
        if not markdown_labeled_text(block.body, ("操作",)):
            missing.append("操作")
        if not markdown_labeled_text(block.body, ("预期",)):
            missing.append("预期")
        if not markdown_labeled_text(block.body, ("通过标准",)):
            missing.append("通过标准")
        if missing:
            issues.append(f"验收标准 {block.id} 缺少可执行字段：{'、'.join(missing)}。")
    return issues


def executable_test_case_issues(markdown: str) -> list[str]:
    issues: list[str] = []
    for block in parse_requirement_draft(markdown).tc_blocks:
        missing: list[str] = []
        if not markdown_labeled_ids(block.body, ("覆盖验收", "关联验收", "验收关联"), "AC"):
            missing.append("覆盖验收")
        if not markdown_labeled_ids(block.body, ("覆盖需求", "关联需求", "需求关联"), "FR"):
            missing.append("覆盖需求")
        if not markdown_labeled_text(block.body, ("类型",)):
            missing.append("类型")
        if not markdown_labeled_text(block.body, ("操作", "方法")):
            missing.append("操作")
        if not markdown_labeled_text(block.body, ("预期",)):
            missing.append("预期")
        if not markdown_labeled_text(block.body, ("通过标准",)):
            missing.append("通过标准")
        if missing:
            issues.append(f"测试用例 {block.id} 缺少可执行字段：{'、'.join(missing)}。")
    return issues


def fr_items_from_markdown(requirement_body: str, draft_id: str, title: str, decisions: list[str]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for block in parse_requirement_draft(requirement_body).fr_blocks:
        description = markdown_labeled_text(block.body, ("说明", "描述")) or "\n".join(
            line for line in block.body.splitlines() if line.strip() and not re.match(r"^[-*•]?\s*[^：:]{1,12}\s*[：:]", line.strip())
        ).strip()
        points.append(
            {
                "id": block.id,
                "title": block.title,
                "description": description,
                "rules": [
                    *markdown_labeled_values(block.body, ("规则", "业务规则", "状态值", "错误码")),
                    *decisions,
                ],
                "inputs": markdown_labeled_values(block.body, ("输入",)),
                "outputs": markdown_labeled_values(block.body, ("输出",)),
                "triggers": markdown_labeled_values(block.body, ("触发条件", "触发")),
                "data_changes": markdown_labeled_values(block.body, ("保存数据", "改变数据", "保存或改变的数据", "数据变化")),
                "permissions": markdown_labeled_values(block.body, ("权限", "权限规则")),
                "exceptions": markdown_labeled_values(block.body, ("异常", "异常回退", "错误处理")),
                "boundaries": markdown_labeled_values(block.body, ("边界", "边界情况")),
                "acceptance_ids": markdown_labeled_ids(block.body, ("验收关联", "关联验收", "覆盖验收"), "AC"),
                "material_refs": [draft_id],
            }
        )
    if points:
        return points
    return []


def ac_items_from_markdown(requirement_body: str, requirement_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for block in parse_requirement_draft(requirement_body).ac_blocks:
        requirement_ids = markdown_labeled_ids(block.body, ("覆盖需求", "关联需求", "需求关联"), "FR")
        operation = markdown_labeled_text(block.body, ("操作",))
        expected = markdown_labeled_text(block.body, ("预期",))
        pass_standard = markdown_labeled_text(block.body, ("通过标准",))
        description = markdown_labeled_text(block.body, ("说明", "描述")) or expected or pass_standard or block.title or block.id
        points.append(
            {
                "id": block.id,
                "title": block.title or description,
                "requirement_ids": requirement_ids,
                "description": description,
                "operation": operation,
                "expected": expected,
                "pass_standard": pass_standard,
            }
        )
    if points:
        return points
    return []


def tc_items_from_markdown(requirement_body: str, acceptance_points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for block in parse_requirement_draft(requirement_body).tc_blocks:
        acceptance_ids = markdown_labeled_ids(block.body, ("覆盖验收", "关联验收", "验收关联"), "AC")
        requirement_ids = markdown_labeled_ids(block.body, ("覆盖需求", "关联需求", "需求关联"), "FR")
        operation = markdown_labeled_text(block.body, ("操作", "方法"))
        expected = markdown_labeled_text(block.body, ("预期",))
        pass_standard = markdown_labeled_text(block.body, ("通过标准",))
        description = markdown_labeled_text(block.body, ("说明", "描述")) or block.title or operation or pass_standard or block.id
        case_type = markdown_labeled_text(block.body, ("类型",))
        cases.append(
            {
                "id": block.id,
                "acceptance_ids": acceptance_ids,
                "requirement_ids": list(dict.fromkeys(requirement_ids)),
                "description": description,
                "type": case_type,
                "operation": operation,
                "method": operation,
                "expected": expected,
                "pass_standard": pass_standard,
            }
        )
    if cases:
        return cases
    return []
