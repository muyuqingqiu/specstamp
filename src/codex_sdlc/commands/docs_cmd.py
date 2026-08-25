from __future__ import annotations

import argparse
import re
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.git_tools import find_git_root, run_git
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.render import join_lines, relative_to_project
from codex_sdlc.core.state import (
    append_event,
    compute_next_actions,
    current_runtime_text,
    derive_state,
    display_task_title,
    now_iso,
    refresh_materialized_state,
    resolve_requirement,
    sanitize_runtime_text,
)
from codex_sdlc.legacy.task_pack_reader import (
    inspect_requirement_legacy_task_packs,
    is_runtime_noise_path,
    legacy_task_pack_display_lines,
    normalized_runtime_path,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("docs", help="生成需求维护文档")
    parser.add_argument("requirement_id", nargs="?", help="可选需求编号")
    parser.add_argument("--path", help="可选文档路径，默认写入 docs/guide")
    parser.add_argument("--force", action="store_true", help="允许覆盖已有文档")
    parser.add_argument("--commit", action="store_true", help="客户明确要求提交维护文档时才使用；默认不提交 Git")
    parser.set_defaults(func=run)


def clean_title(title: str) -> str:
    text = title.strip()
    text = re.sub(r"[\\/:*?\"<>|`#]+", "", text)
    text = re.sub(r"\s+", "", text)
    return text[:36] or "需求"


def default_docs_path(root: Path, requirement: dict[str, object]) -> Path:
    title = clean_title(str(requirement.get("title") or requirement["requirement_id"]))
    return root / "docs" / "guide" / f"{title}逻辑梳理.md"


def docs_next_recommendation(paths) -> dict[str, object]:
    state = derive_state(paths)
    next_actions = compute_next_actions(paths, state)
    primary = str(next_actions.get("primary") or "").strip()
    # 维护文档生成后，如果已经有明确的新需求、变更或任务线索，就优先提示那条真实下一步；
    # 如果系统只是回到“再讨论一个新想法”的空闲入口，仍然建议先 finish，把本轮交接收好。
    if primary in {"$sdlc-discuss 需求想法", "$sdlc-handoff", ""}:
        alternatives = unique_items([primary, *list(next_actions.get("alternatives") or [])])
        return {
            "primary": "$sdlc-finish",
            "reason": "维护文档已生成，当前没有更明确的已记录新需求或未完成任务，先生成本轮正式交接。",
            "alternatives": alternatives,
        }
    alternatives = unique_items(["$sdlc-finish", *list(next_actions.get("alternatives") or [])])
    return {
        "primary": primary,
        "reason": str(next_actions.get("reason") or "维护文档已生成，继续处理当前最明确的下一步。"),
        "alternatives": alternatives,
    }


def select_requirement_for_docs(state: dict[str, object], requirement_id: str | None) -> dict[str, object]:
    if requirement_id:
        return resolve_requirement(state, requirement_id)

    accepted = [
        requirement
        for requirement in state["requirements"].values()  # type: ignore[index]
        if requirement.get("status") == "accepted"
    ]
    if len(accepted) == 1:
        return accepted[0]
    if len(accepted) > 1:
        lines = ["多个需求已经接受，请指定要生成哪一个需求的维护文档："]
        lines.extend(f"- {item['requirement_id']}：{item['title']}" for item in accepted)
        raise SdlcError("\n".join(lines), exit_code=1)
    raise SdlcError("当前没有已接受的需求。请先在回归通过后让用户确认 `$sdlc-accept REQ-001`。", exit_code=1)


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().strip("#*- ")
        if stripped:
            return stripped
    return "这轮需求已经完成，具体目标和实现范围见下方说明。"


def clean_human_docs_sentence(text: str) -> str:
    clean = str(text).strip()
    clean = re.sub(r"^任务目标[:：]\s*", "", clean)
    return clean.strip()


def unique_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = item.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def docs_runtime_text(requirement: dict[str, object], text: object) -> str:
    clean = current_runtime_text(requirement, text)
    return sanitize_runtime_text(clean)


def is_business_file_for_docs(file_path: str) -> bool:
    normalized = normalized_runtime_path(file_path)
    if not normalized:
        return False
    if normalized == "AGENTS.md" or normalized.endswith("/AGENTS.md"):
        return False
    return not (normalized.startswith(".codex-sdlc/") or is_runtime_noise_path(normalized))


def normalize_existing_docs_file(root: Path, file_path: str) -> str | None:
    normalized = normalized_runtime_path(file_path)
    if not is_business_file_for_docs(normalized):
        return None
    path = Path(normalized)
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
        path = root / relative
    else:
        relative = normalized
        path = root / relative
    if not path.exists() or path.is_dir():
        return None
    return relative


def requirement_intro_text(requirement: dict[str, object]) -> str:
    """维护文档简介只读取正式结构化字段。"""

    structured = requirement.get("structured") if isinstance(requirement.get("structured"), dict) else {}
    for value in (structured.get("goal"), structured.get("background"), requirement.get("summary"), requirement.get("title")):
        clean = docs_runtime_text(requirement, value)
        if clean:
            return clean
    return str(requirement.get("requirement_id") or "需求")


def task_changed_files(root: Path, requirement: dict[str, object]) -> list[str]:
    files: list[str] = []
    for task in requirement.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for item in task.get("changed_files", []):
            normalized = normalize_existing_docs_file(root, str(item))
            if normalized:
                files.append(normalized)
    return unique_items(files)


def task_change_report_path(root: Path, requirement: dict[str, object], task_id: str) -> Path:
    return root / ".codex-sdlc" / "requirements" / str(requirement["folder_name"]) / "task-change-reports" / f"{task_id}.md"


def legacy_task_brief_path(root: Path, requirement: dict[str, object], task_id: str) -> Path:
    return root / ".codex-sdlc" / "requirements" / str(requirement["folder_name"]) / "task-briefs" / f"{task_id}.md"


def existing_task_change_report_path(root: Path, requirement: dict[str, object], task_id: str) -> Path | None:
    report = task_change_report_path(root, requirement, task_id)
    if report.exists():
        return report
    legacy_report = legacy_task_brief_path(root, requirement, task_id)
    if legacy_report.exists():
        return legacy_report
    return None


def task_output_index_path(root: Path, requirement: dict[str, object]) -> Path:
    return root / ".codex-sdlc" / "requirements" / str(requirement["folder_name"]) / "task-outputs" / "index.md"


def short_task_result(root: Path, requirement: dict[str, object], task: dict[str, object]) -> str:
    """任务结果由任务卡显式提供，不读取人读报告判断结论。"""

    value = task.get("result_summary") or task.get("summary") or task.get("title") or task.get("task_id")
    return docs_runtime_text(requirement, value)


def verification_summaries(requirement: dict[str, object], limit: int = 12) -> list[str]:
    lines: list[str] = []
    for task in requirement.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for verification in task.get("verifications", []):
            if not isinstance(verification, dict):
                continue
            summary = str(verification.get("summary") or "").splitlines()[0].strip()
            if summary:
                lines.append(f"{verification.get('verification_id', 'VRF')}：{docs_runtime_text(requirement, summary)}")
            if len(lines) >= limit:
                return lines
    return lines


def docs_lines(root: Path, requirement: dict[str, object]) -> list[str]:
    title = clean_title(str(requirement.get("title") or requirement["requirement_id"]))
    description = requirement_intro_text(requirement)
    files = task_changed_files(root, requirement)
    output_index = task_output_index_path(root, requirement)
    tasks = [task for task in requirement.get("tasks", []) if isinstance(task, dict)]
    verifications = verification_summaries(requirement)
    legacy_task_packs = inspect_requirement_legacy_task_packs(
        build_paths(root),
        requirement,
    )
    lines = [
        f"<!-- {title}逻辑梳理 | 说明：看完这份文档可以清楚了解本轮需求目标、代码产出、逻辑线和后续维护入口 -->",
        "",
        f"# {title}逻辑梳理",
        "",
        "## 一句话理解",
        "",
        first_non_empty_line(description),
        "",
        "## 这轮需求要解决什么",
        "",
        "- 让开发人员不用翻完整聊天记录，也能知道本轮需求为什么做、做到哪里、哪些地方不能随便改。",
        f"- 需求编号：{requirement['requirement_id']}。",
        f"- 需求状态：{requirement.get('status', 'unknown')}。",
        f"- 需求目录：`.codex-sdlc/requirements/{requirement['folder_name']}`。",
        "",
        "## 产出总览",
        "",
        "| 类型 | 内容 | 用途 |",
        "|------|------|------|",
        f"| 需求文档 | `.codex-sdlc/requirements/{requirement['folder_name']}/effective/requirement.current.md` | 查看当前最终生效需求 |",
        f"| 技术方案 | `.codex-sdlc/requirements/{requirement['folder_name']}/effective/design.current.md` | 查看当前最终技术做法 |",
        f"| 测试矩阵 | `.codex-sdlc/requirements/{requirement['folder_name']}/effective/test-matrix.current.md` | 查看验收和回归口径 |",
        f"| 任务记录 | `.codex-sdlc/requirements/{requirement['folder_name']}/tasks/` | 追溯每个任务做了什么 |",
        f"| 任务变更说明 | `.codex-sdlc/requirements/{requirement['folder_name']}/task-change-reports/` | 给人看，解释每个任务到底改了什么 |",
    ]
    if output_index.exists():
        lines.append(
            f"| SDLC 精简产出 | `{relative_to_project(root, output_index)}` | 给后续任务读取清单绑定上游交付，不作为人看的主文档 |"
        )
    if legacy_task_packs:
        lines.extend(
            [
                "",
                "## 既有任务执行包档案",
                "",
                *legacy_task_pack_display_lines(legacy_task_packs),
            ]
        )
    lines.extend(
        [
            "",
            "## 代码入口和文件分工",
            "",
        ]
    )
    if files:
        for file_path in files:
            related_tasks = [
                str(task["task_id"])
                for task in tasks
                if file_path in [normalize_existing_docs_file(root, str(item)) for item in task.get("changed_files", [])]
            ]
            task_text = "、".join(related_tasks) if related_tasks else "未记录任务"
            lines.append(f"- `{file_path}`：本轮由 {task_text} 触达，维护时先看对应任务变更说明。")
    else:
        lines.append("- 当前任务没有记录代码文件。维护时先看任务变更说明和验证记录，再按业务入口搜索代码。")
    lines.extend(
        [
            "",
            "## 整体逻辑线",
            "",
            "1. 先从需求文档看用户要解决的问题。",
            "2. 再看技术方案，确认本轮选择了哪些现有模块和封装。",
            "3. 进入代码时，优先从上面的代码入口和文件分工开始读。",
            "4. 遇到不清楚的实现原因，先看对应任务变更说明，再看验证记录。",
            "5. 要继续维护时，先确认当前需求是否又发生变更，不要只按旧任务理解代码。",
            "",
            "## 任务改动说明",
            "",
        ]
    )
    if tasks:
        for task in tasks:
            report_path = existing_task_change_report_path(root, requirement, str(task["task_id"]))
            report_line = (
                f"- 变更说明：`{relative_to_project(root, report_path)}`"
                if report_path is not None
                else f"- 变更说明：本任务没有单独人读变更报告；查看 `tasks/{task['task_id']}.md` 和验证记录。"
            )
            lines.extend(
                [
                    f"### {task['task_id']} {docs_runtime_text(requirement, display_task_title(task))}",
                    f"- 状态：{task.get('status', 'unknown')}",
                    f"- 主要结果：{short_task_result(root, requirement, task)}",
                    report_line,
                    "",
                ]
            )
    else:
        lines.append("- 当前需求没有任务记录。")
    lines.extend(
        [
            "## 验证和回归",
            "",
        ]
    )
    if verifications:
        lines.extend(f"- {item}" for item in verifications)
    else:
        lines.append("- 当前没有验证记录。")
    lines.extend(
        [
            "",
            "## 后续维护提示",
            "",
            "- 改代码前先确认当前需求版本，开发和回归只看 `effective/*.current.md`。",
            "- 修改已完成能力时，优先新增修复任务或需求变更，不要直接改旧任务记录。",
            "- 维护时重点看任务变更说明里的“一句话结论、修改前、修改后、改动串联、对后续任务的影响、后续维护提示”，那里会解释每次代码为什么变成现在这样。",
            "- 如果这里的说明和真实代码不一致，以真实代码为准，并及时更新本文档。",
        ]
    )
    return lines


def git_relative_path(git_root: Path, file_path: Path) -> str | None:
    try:
        return str(file_path.resolve().relative_to(git_root.resolve()))
    except ValueError:
        return None


def git_user_config_ready(git_root: Path) -> bool:
    name = run_git(["config", "--get", "user.name"], git_root)
    email = run_git(["config", "--get", "user.email"], git_root)
    return name.returncode == 0 and bool(name.stdout.strip()) and email.returncode == 0 and bool(email.stdout.strip())


def short_commit_hash(git_root: Path) -> str:
    result = run_git(["rev-parse", "--short", "HEAD"], git_root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def auto_commit_docs_file(root: Path, output_path: Path, requirement: dict[str, object]) -> dict[str, str]:
    git_root = find_git_root(root)
    if git_root is None:
        return {"status": "skipped", "message": "当前目录不是 Git 仓库，维护文档已生成但无法提交。"}
    if not git_user_config_ready(git_root):
        return {"status": "skipped", "message": "Git 用户信息未配置，维护文档已生成但未提交。"}

    relative_path = git_relative_path(git_root, output_path)
    if not relative_path:
        return {"status": "skipped", "message": "维护文档不在当前 Git 仓库里，已跳过提交。"}

    status_result = run_git(["status", "--short", "--", relative_path], git_root)
    if status_result.returncode != 0:
        detail = (status_result.stderr or status_result.stdout).strip()
        return {"status": "failed", "message": f"检查维护文档 Git 状态失败：{detail}"}
    ignored_result = run_git(["check-ignore", "-q", "--no-index", "--", relative_path], git_root)
    tracked_result = run_git(["ls-files", "--error-unmatch", "--", relative_path], git_root)
    is_ignored_by_rule = ignored_result.returncode == 0
    is_ignored_untracked = is_ignored_by_rule and tracked_result.returncode != 0
    if not status_result.stdout.strip() and not is_ignored_untracked:
        return {"status": "none", "message": "维护文档没有新增改动，无需提交。"}

    add_args = ["add", "-f", "--", relative_path] if is_ignored_by_rule else ["add", "--", relative_path]
    add_result = run_git(add_args, git_root)
    if add_result.returncode != 0:
        detail = (add_result.stderr or add_result.stdout).strip()
        return {"status": "failed", "message": f"维护文档 git add 失败：{detail}"}

    title = clean_title(str(requirement.get("title") or requirement["requirement_id"]))
    commit_message = f"生成需求维护文档：{title}"
    commit_result = run_git(["commit", "-m", commit_message, "--", relative_path], git_root)
    if commit_result.returncode != 0:
        detail = (commit_result.stderr or commit_result.stdout).strip()
        return {"status": "failed", "message": f"维护文档 git commit 失败：{detail}"}

    commit_hash = short_commit_hash(git_root)
    hash_text = f"，提交哈希：{commit_hash}" if commit_hash else ""
    return {
        "status": "committed",
        "message": f"已提交维护文档：{relative_path}{hash_text}；提交信息：{commit_message}",
        "commit_hash": commit_hash,
        "commit_message": commit_message,
    }


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement = select_requirement_for_docs(state, args.requirement_id)
        if requirement.get("status") != "accepted":
            raise SdlcError(
                f"{requirement['requirement_id']} 还没有 accepted。请先在回归通过并得到用户确认后执行 `$sdlc-accept {requirement['requirement_id']}`。",
                exit_code=1,
            )
        output_path = Path(args.path).expanduser() if args.path else default_docs_path(paths.root, requirement)
        if not output_path.is_absolute():
            output_path = paths.root / output_path
        if output_path.exists() and not args.force:
            raise SdlcError(
                f"维护文档已存在：{relative_to_project(paths.root, output_path)}。"
                f"如需按当前需求状态覆盖刷新，请执行 `$sdlc-docs {requirement['requirement_id']} --force`。",
                exit_code=1,
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(join_lines(docs_lines(paths.root, requirement)), encoding="utf-8")
        relative_path = str(relative_to_project(paths.root, output_path))
        append_event(
            paths,
            event_type="requirement_docs_created",
            source="sdlc-docs",
            summary=f"生成需求维护文档 {relative_path}",
            requirement_id=requirement["requirement_id"],
            payload={
                "file_path": relative_path,
                "created_at": now_iso(),
            },
        )
        refresh_materialized_state(paths)

    # 维护文档默认只落文件，不主动进 Git；这样避免把客户只想本地查看的文档误提交到项目仓库。
    commit_result = (
        auto_commit_docs_file(paths.root, output_path, requirement)
        if args.commit
        else {
            "status": "skipped",
            "message": "默认不提交 Git；只有客户明确要求时才提交维护文档。",
        }
    )
    print(f"已生成需求维护文档：{relative_path}")
    print("文档用途：帮助后续开发人员快速看懂本轮需求目标、代码入口、任务产出、验证结果和维护注意点。")
    print("Git 提交状态：")
    print(f"- {commit_result['message']}")
    next_recommendation = docs_next_recommendation(paths)
    print(f"下一步建议：{next_recommendation['primary']}")
    if next_recommendation["primary"] != "$sdlc-finish":
        print(f"推荐原因：{next_recommendation['reason']}")
        alternatives = [item for item in next_recommendation.get("alternatives", []) if item]
        if alternatives:
            print("备选指令：")
            for item in alternatives[:5]:
                print(f"- {item}")
    return 0
