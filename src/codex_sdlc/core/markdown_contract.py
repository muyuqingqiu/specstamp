from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MarkdownBlock:
    """Markdown 中一个带编号标题的内容块，例如 FR-001 / AC-001 / TC-001。"""

    id: str
    title: str
    body: str


def clean_text(value: object) -> str:
    return str(value or "").strip()


def heading_matches(markdown: str) -> list[tuple[int, str, int, int]]:
    """返回 Markdown 标题列表：标题等级、标题文本、标题起点、标题行结束位置。"""

    matches: list[tuple[int, str, int, int]] = []
    for match in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", markdown, flags=re.M):
        matches.append((len(match.group(1)), match.group(2).strip(), match.start(), match.end()))
    return matches


def markdown_section_body(markdown: str, headings: tuple[str, ...], *, level: int = 2) -> str:
    """读取指定等级标题的正文，只被同级或更高等级标题截断。"""

    wanted = {heading.strip() for heading in headings if heading.strip()}
    if not wanted:
        return ""
    matches = heading_matches(markdown)
    for index, (heading_level, title, _start, end) in enumerate(matches):
        if heading_level != level or title not in wanted:
            continue
        next_start = len(markdown)
        for next_level, _next_title, start, _next_end in matches[index + 1 :]:
            if next_level <= heading_level:
                next_start = start
                break
        return markdown[end:next_start].strip()
    return ""


def markdown_section_present(markdown: str, headings: tuple[str, ...], *, level: int = 2) -> bool:
    """只按标题结构判断章节是否存在，空章节也算已经显式提供。"""

    wanted = {heading.strip() for heading in headings if heading.strip()}
    return any(heading_level == level and title in wanted for heading_level, title, _start, _end in heading_matches(markdown))


def markdown_sections(markdown: str, *, level: int = 2) -> list[tuple[str, str]]:
    """按指定标题等级切出章节，章节正文包含更低等级标题。"""

    matches = heading_matches(markdown)
    sections: list[tuple[str, str]] = []
    for index, (heading_level, title, _start, end) in enumerate(matches):
        if heading_level != level:
            continue
        next_start = len(markdown)
        for next_level, _next_title, start, _next_end in matches[index + 1 :]:
            if next_level <= heading_level:
                next_start = start
                break
        sections.append((title, markdown[end:next_start].strip()))
    return sections


def strip_list_marker(raw_line: str) -> str:
    clean = re.sub(r"^(?:[-*•]|\d+[.、])\s*", "", raw_line.strip()).strip(" \t。；;,.，")
    return clean


def markdown_clean_lines(
    markdown: str,
    headings: tuple[str, ...],
    *,
    level: int = 2,
    pending_markers: tuple[str, ...] = (),
) -> list[str]:
    """读取章节里的非空正文行，只过滤明确约定的机器占位符。"""

    body = markdown_section_body(markdown, headings, level=level)
    lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        clean = strip_list_marker(line)
        if not clean:
            continue
        if clean in pending_markers:
            continue
        lines.append(clean)
    return lines


def heading_blocks(section_body: str, prefix: str, *, level: int = 3) -> list[MarkdownBlock]:
    """从一段章节正文里读取 `### FR-001 标题` 这类编号块。"""

    pattern = re.compile(rf"^({'#' * level})\s+({re.escape(prefix)}-\d{{3}})\b\s*(.*?)\s*$", flags=re.M | re.I)
    matches = list(pattern.finditer(section_body))
    blocks: list[MarkdownBlock] = []
    for index, match in enumerate(matches):
        heading_level = len(match.group(1))
        next_start = len(section_body)
        for later in matches[index + 1 :]:
            next_start = later.start()
            break
        # 如果编号块里有 H2 或更高等级标题，说明输入混乱；为了避免串段，仍按更高等级标题截断。
        higher = re.search(rf"^#{{1,{heading_level}}}\s+.+$", section_body[match.end() : next_start], flags=re.M)
        if higher:
            next_start = match.end() + higher.start()
        blocks.append(
            MarkdownBlock(
                id=match.group(2).upper(),
                title=clean_text(match.group(3)),
                body=section_body[match.end() : next_start].strip(),
            )
        )
    return blocks


def markdown_heading_blocks(markdown: str, section_headings: tuple[str, ...], prefix: str, *, section_level: int = 2) -> list[MarkdownBlock]:
    section = markdown_section_body(markdown, section_headings, level=section_level)
    return heading_blocks(section, prefix, level=section_level + 1)


def split_markdown_value(raw_value: str) -> list[str]:
    clean = clean_text(raw_value).strip(" \t。；;")
    return [clean] if clean else []


def markdown_indented_values(lines: list[str], start_index: int) -> list[str]:
    values: list[str] = []
    for raw_line in lines[start_index + 1 :]:
        if not raw_line.strip():
            continue
        if raw_line == raw_line.lstrip():
            break
        clean = re.sub(r"^[-*•]\s*", "", raw_line.strip()).strip(" \t。；;")
        if clean:
            values.append(clean)
    return values


def markdown_labeled_values(block: str, labels: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    lines = block.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*•]\s*", "", line)
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[：:]\s*(.*)$", line)
            if not match:
                continue
            inline_values = split_markdown_value(match.group(1))
            values.extend(inline_values or markdown_indented_values(lines, index))
            break
    return values


def markdown_labeled_text(block: str, labels: tuple[str, ...]) -> str:
    values = markdown_labeled_values(block, labels)
    return values[0] if values else ""


def extract_public_ids(text: str, prefix: str) -> list[str]:
    ids: list[str] = []
    for match in re.finditer(rf"\b{re.escape(prefix)}-\d{{3}}\b", text, flags=re.I):
        item = match.group(0).upper()
        if item not in ids:
            ids.append(item)
    return ids


def markdown_labeled_ids(block: str, labels: tuple[str, ...], prefix: str) -> list[str]:
    ids: list[str] = []
    lines = block.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*•]\s*", "", line)
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[：:]\s*(.*)$", line)
            if not match:
                continue
            raw_values = [match.group(1)] if match.group(1).strip() else markdown_indented_values(lines, index)
            for item in extract_public_ids("\n".join(raw_values), prefix):
                if item not in ids:
                    ids.append(item)
            break
    return ids


def unlabeled_markdown_lines(block: str) -> list[str]:
    lines: list[str] = []
    for raw_line in block.splitlines():
        line = re.sub(r"^[-*•]\s*", "", raw_line.strip()).strip()
        if not line or line.startswith("#") or re.match(r"^[^：:]{1,12}\s*[：:]", line):
            continue
        lines.append(line)
    return lines
