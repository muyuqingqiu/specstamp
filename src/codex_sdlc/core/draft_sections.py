from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_sdlc.core.markdown_contract import markdown_clean_lines, markdown_section_body, markdown_section_present


@dataclass(frozen=True)
class DraftSectionSpec:
    key: str
    canonical: str
    aliases: tuple[str, ...]
    nested_under: dict[str, tuple[str, ...]] | None = None


REQUIREMENT_SECTION_SPECS: tuple[DraftSectionSpec, ...] = (
    DraftSectionSpec("background", "背景和目标", ("背景和目标",)),
    DraftSectionSpec("user_scenarios", "用户和使用场景", ("用户和使用场景", "用户场景", "使用场景")),
    DraftSectionSpec("scope", "本轮范围", ("本轮范围", "范围")),
    DraftSectionSpec("out_of_scope", "不做范围", ("不做范围", "本轮不做"), {"本轮范围": ("本轮不做", "不做范围")} ),
    DraftSectionSpec("functional_requirements", "功能需求", ("功能需求",)),
    DraftSectionSpec("business_rules", "业务规则", ("业务规则",)),
    DraftSectionSpec("permission_rules", "权限规则", ("权限规则",)),
    DraftSectionSpec("data_state_rules", "数据和状态规则", ("数据和状态规则", "数据状态规则")),
    DraftSectionSpec("interface_scope", "接口或页面范围", ("接口或页面范围", "接口范围", "页面范围")),
    DraftSectionSpec(
        "exceptions",
        "异常和边界",
        ("异常和边界情况", "异常和边界", "异常和回退规则", "异常处理", "错误处理", "边界情况"),
    ),
    DraftSectionSpec("acceptance", "验收标准", ("验收标准", "验收条件")),
    DraftSectionSpec("test_focus", "测试关注点", ("测试关注点",)),
    DraftSectionSpec("test_matrix", "测试矩阵", ("测试矩阵", "测试用例")),
    DraftSectionSpec("open_questions", "未确认问题", ("未确认问题", "待确认问题")),
)

_SPEC_BY_KEY = {spec.key: spec for spec in REQUIREMENT_SECTION_SPECS}
_SPEC_BY_CANONICAL = {spec.canonical: spec for spec in REQUIREMENT_SECTION_SPECS}


def requirement_section_specs() -> tuple[DraftSectionSpec, ...]:
    return REQUIREMENT_SECTION_SPECS


def requirement_section_canonicals() -> tuple[str, ...]:
    return tuple(spec.canonical for spec in REQUIREMENT_SECTION_SPECS)


def section_spec(name_or_key: str) -> DraftSectionSpec:
    clean = str(name_or_key or "").strip()
    spec = _SPEC_BY_KEY.get(clean) or _SPEC_BY_CANONICAL.get(clean)
    if spec is None:
        for candidate in REQUIREMENT_SECTION_SPECS:
            if clean in candidate.aliases:
                return candidate
        raise KeyError(clean)
    return spec


def section_aliases(name_or_key: str) -> tuple[str, ...]:
    return section_spec(name_or_key).aliases


def nested_section_body(markdown: str, parent_headings: tuple[str, ...], child_headings: tuple[str, ...]) -> str:
    for parent in parent_headings:
        parent_body = markdown_section_body(markdown, (parent,), level=2)
        if not parent_body:
            continue
        for child in child_headings:
            child_body = markdown_section_body(parent_body, (child,), level=3)
            if child_body:
                return child_body
    return ""


def requirement_section_body(markdown: str, name_or_key: str) -> str:
    spec = section_spec(name_or_key)
    body = markdown_section_body(markdown, spec.aliases, level=2)
    if body:
        return body
    if spec.nested_under:
        for parent, children in spec.nested_under.items():
            body = nested_section_body(markdown, (parent,), children)
            if body:
                return body
    return ""


def requirement_section_clean_lines(
    markdown: str,
    name_or_key: str,
    *,
    pending_markers: tuple[str, ...] = (),
) -> list[str]:
    spec = section_spec(name_or_key)
    lines = markdown_clean_lines(
        markdown,
        spec.aliases,
        pending_markers=pending_markers,
    )
    if lines:
        return lines
    if spec.nested_under:
        collected: list[str] = []
        for parent, children in spec.nested_under.items():
            parent_body = markdown_section_body(markdown, (parent,), level=2)
            if not parent_body:
                continue
            for child in children:
                collected.extend(
                    markdown_clean_lines(
                        parent_body,
                        (child,),
                        level=3,
                        pending_markers=pending_markers,
                    )
                )
        return list(dict.fromkeys(collected))
    return []


def requirement_section_present(markdown: str, name_or_key: str) -> bool:
    spec = section_spec(name_or_key)
    if markdown_section_present(markdown, spec.aliases, level=2):
        return True
    if spec.nested_under:
        for parent, children in spec.nested_under.items():
            parent_body = markdown_section_body(markdown, (parent,), level=2)
            if parent_body and markdown_section_present(parent_body, children, level=3):
                return True
    return False


def missing_requirement_sections(markdown: str) -> list[str]:
    missing: list[str] = []
    for spec in REQUIREMENT_SECTION_SPECS:
        if not requirement_section_present(markdown, spec.key):
            missing.append(spec.canonical)
    return missing
