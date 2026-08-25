#!/usr/bin/env python3
"""根据任务文档生成任务进度文件。"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class TaskItem:
    """保存单个任务的基础信息。"""

    task_id: str
    title: str


def parse_args() -> argparse.Namespace:
    """读取命令行参数。"""

    parser = argparse.ArgumentParser(description="根据任务文档初始化 Goal 任务进度文件。")
    parser.add_argument("--task-doc", required=True, help="任务文档路径")
    parser.add_argument("--requirement-doc", help="需求文档路径，可选")
    parser.add_argument("--project-root", help="项目根路径，可选")
    parser.add_argument("--progress-file", help="进度文件路径，可选")
    parser.add_argument("--environment", default="通用兜底环境", help="当前运行环境")
    parser.add_argument("--model-tier", default="待主线程判断", help="推荐模型档位")
    parser.add_argument("--requested-model", default="null", help="请求模型")
    parser.add_argument("--requested-thinking", default="null", help="请求思考深度")
    parser.add_argument("--model-reason", default="待主线程补充", help="模型判断原因")
    parser.add_argument("--thinking-reason", default="待主线程补充", help="思考深度判断原因")
    parser.add_argument("--force", action="store_true", help="如果目标文件已存在，允许覆盖")
    return parser.parse_args()


def strip_frontmatter(text: str) -> str:
    """去掉 Markdown 顶部 frontmatter，避免把元数据当任务正文。"""

    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :]


def normalize_title(raw: str) -> str:
    """把一行任务文本收敛成可读标题。"""

    title = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s*", "", raw).strip()
    title = re.sub(r"^\s*\d+[.)、]\s*", "", title).strip()
    title = re.sub(r"^\s*#+\s*", "", title).strip()
    return title


def split_task_id(title: str) -> tuple[str | None, str]:
    """如果标题里自带任务编号，就拆出来继续复用。"""

    match = re.match(r"^([A-Za-z]+-\d{2,}|\w+-\d+)\s*[:：-]?\s*(.*)$", title)
    if not match:
        return None, title
    task_id = match.group(1).strip()
    task_title = match.group(2).strip() or task_id
    return task_id, task_title


def parse_tasks(task_doc: Path) -> list[TaskItem]:
    """尽量从任务文档里提取多个任务；提取不到时退化成单任务。"""

    text = strip_frontmatter(task_doc.read_text(encoding="utf-8"))
    tasks: list[TaskItem] = []
    seen_titles: set[str] = set()

    # 先优先识别最明确的 Markdown checklist。
    for line in text.splitlines():
        if re.match(r"^\s*[-*+]\s+\[[ xX]\]\s+", line):
            title = normalize_title(line)
            if title and title not in seen_titles:
                seen_titles.add(title)
                tasks.append(make_task_item(title, len(tasks) + 1))

    # 如果 checklist 没识别到，再退回到常见的编号列表。
    if not tasks:
        for line in text.splitlines():
            if re.match(r"^\s*\d+[.)、]\s+", line):
                title = normalize_title(line)
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    tasks.append(make_task_item(title, len(tasks) + 1))

    # 如果还是没有，再找像 “## T-001 xxx” 或 “### 任务 1 xxx” 这样的标题。
    if not tasks:
        for line in text.splitlines():
            if not re.match(r"^\s*#{1,6}\s+", line):
                continue
            title = normalize_title(line)
            if not title:
                continue
            if re.match(r"^[A-Za-z]+-\d+", title) or "任务" in title:
                if title not in seen_titles:
                    seen_titles.add(title)
                    tasks.append(make_task_item(title, len(tasks) + 1))

    # 最后兜底成一个单任务，至少保证技能能起跑。
    if not tasks:
        fallback_title = task_doc.stem or "未命名任务"
        tasks.append(make_task_item(fallback_title, 1))

    return tasks


def make_task_item(raw_title: str, index: int) -> TaskItem:
    """把标题转成标准任务项。"""

    normalized = normalize_title(raw_title)
    task_id, task_title = split_task_id(normalized)
    if not task_id:
        task_id = f"T-{index:03d}"
    return TaskItem(task_id=task_id, title=task_title)


def find_git_root(start: Path) -> Path | None:
    """从给定目录向上查找 git 根目录。"""

    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def determine_project_root(task_doc: Path, explicit_project_root: str | None) -> Path:
    """按技能规则推导项目根目录。"""

    if explicit_project_root:
        return Path(explicit_project_root).expanduser().resolve()

    current_root = Path.cwd().resolve()
    task_doc_resolved = task_doc.resolve()
    try:
        task_doc_resolved.relative_to(current_root)
        return current_root
    except ValueError:
        git_root = find_git_root(task_doc_resolved.parent)
        return git_root or current_root


def sanitize_file_stem(task_doc: Path) -> str:
    """把任务文档名转成适合放进进度文件名的短名字。"""

    stem = task_doc.stem.strip() or "未命名任务"
    # 这里只替换掉文件系统里容易出问题的字符，保留中文方便人工识别。
    return re.sub(r"[\\/:*?\"<>|]+", "-", stem)


def determine_progress_file(
    explicit_progress_file: str | None, project_root: Path, task_doc: Path
) -> Path:
    """生成默认进度文件路径。"""

    if explicit_progress_file:
        return Path(explicit_progress_file).expanduser().resolve()
    file_name = f"任务进度-{sanitize_file_stem(task_doc)}.md"
    return project_root / "tmp" / "任务调度器" / file_name


def render_progress_file(
    *,
    tasks: list[TaskItem],
    task_doc: Path,
    requirement_doc: Path | None,
    project_root: Path,
    environment: str,
    model_tier: str,
    requested_model: str,
    requested_thinking: str,
    model_reason: str,
    thinking_reason: str,
) -> str:
    """生成多任务版进度文件正文。"""

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    requirement_doc_text = str(requirement_doc) if requirement_doc else "null"

    overview_rows = [
        f"| {task.task_id} | {task.title} | pending | null | null | {model_tier} | {requested_model} | null | {requested_thinking} | null | 等待主线程派发 |"
        for task in tasks
    ]

    details: list[str] = []
    for task in tasks:
        details.append(
            "\n".join(
                [
                    f"### {task.task_id} {task.title}",
                    "",
                    "- 当前状态：`pending`",
                    f"- 任务边界：先以“{task.title}”为当前任务标题，详细边界由主线程补充",
                    "- 完成条件：待主线程结合任务文档补充",
                    f"- 当前环境：{environment}",
                    f"- 推荐模型档位：{model_tier}",
                    f"- 请求模型：`{requested_model}`",
                    "- 实际模型：`null`",
                    f"- 请求思考深度：`{requested_thinking}`",
                    "- 实际思考深度：`null`",
                    f"- 模型判断原因：{model_reason}",
                    f"- 思考深度判断原因：{thinking_reason}",
                    "- 执行员线程 id：`null`",
                    "- 质检员线程 id：`null`",
                    "- 最近结论摘要：待开始",
                    "- 测试结果：待开始",
                    "- 下一步动作：创建执行员线程",
                    "",
                    "#### 线程事件记录",
                    "",
                    "| 时间 | 线程职责 | 线程 id | 动作 | 结果 |",
                    "| --- | --- | --- | --- | --- |",
                    f"| {now} | 主线程 | main | 初始化任务条目 | 待派发执行员线程 |",
                    "",
                    "#### 替换与交接记录",
                    "",
                    "| 时间 | 被替换线程 id | 职责 | 停止原因 | 交接摘要 |",
                    "| --- | --- | --- | --- | --- |",
                    "| - | - | - | - | - |",
                    "",
                    "#### 验证记录",
                    "",
                    "| 时间 | 验证人 | 结论 | 证据 |",
                    "| --- | --- | --- | --- |",
                    "| - | - | - | - |",
                ]
            )
        )

    return "\n".join(
        [
            "# 任务进度文件",
            "",
            "## 基本信息",
            "",
            f"- 任务文档：`{task_doc}`",
            f"- 需求文档：`{requirement_doc_text}`",
            f"- 项目路径：`{project_root}`",
            f"- 当前环境：`{environment}`",
            f"- 创建时间：`{now}`",
            f"- 最近更新时间：`{now}`",
            "",
            "## 调度规则摘要",
            "",
            "- 一次只推进一个任务",
            "- 同一任务同一职责只允许一个活跃线程",
            "- 进度文件每 5 分钟检查一次",
            "- 正式开发线程和正式测试线程每 10 分钟检查一次",
            "- 线程卡住或跑偏时，先停旧线程，再开新线程",
            "- 检查间隔没有真实经过前，主线程不能再次读取同一线程或连续读取进度文件",
            "",
            "## 当前任务总览",
            "",
            "| 任务编号 | 任务名称 | 当前状态 | 执行员线程 id | 质检员线程 id | 推荐模型档位 | 请求模型 | 实际模型 | 请求思考深度 | 实际思考深度 | 下一步动作 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            *overview_rows,
            "",
            "## 任务明细",
            "",
            "\n\n".join(details),
            "",
        ]
    )


def main() -> int:
    """脚本入口。"""

    args = parse_args()
    task_doc = Path(args.task_doc).expanduser().resolve()
    if not task_doc.exists():
        print(f"任务文档不存在：{task_doc}", file=sys.stderr)
        return 1

    requirement_doc = None
    if args.requirement_doc:
        requirement_doc = Path(args.requirement_doc).expanduser().resolve()
        if not requirement_doc.exists():
            print(f"需求文档不存在：{requirement_doc}", file=sys.stderr)
            return 1

    project_root = determine_project_root(task_doc, args.project_root)
    progress_file = determine_progress_file(args.progress_file, project_root, task_doc)
    if progress_file.exists() and not args.force:
        print(f"进度文件已存在，请改路径或加 --force 覆盖：{progress_file}", file=sys.stderr)
        return 1

    tasks = parse_tasks(task_doc)
    content = render_progress_file(
        tasks=tasks,
        task_doc=task_doc,
        requirement_doc=requirement_doc,
        project_root=project_root,
        environment=args.environment,
        model_tier=args.model_tier,
        requested_model=args.requested_model,
        requested_thinking=args.requested_thinking,
        model_reason=args.model_reason,
        thinking_reason=args.thinking_reason,
    )

    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(content, encoding="utf-8")
    print(progress_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
