from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from codex_sdlc.commands.doctor_cmd import (
    DOCUMENT_FIRST_PROFILE,
    inspect_document_first_archive,
)
from codex_sdlc.core.backup import is_sensitive_metadata_key
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.render import join_lines
from codex_sdlc.core.state import derive_state, now_iso, resolve_requirement


SECRET_REFERENCE_FIELDS = {"schema_version", "kind", "identifier", "access"}


def sanitize_export_payload(value: Any) -> Any:
    """普通导出只显示秘密引用定位信息，任何秘密值字段都统一脱敏。"""

    if isinstance(value, dict):
        if value.get("schema_version") == "secret-reference.v1":
            return {key: sanitize_export_payload(value[key]) for key in SECRET_REFERENCE_FIELDS if key in value}
        result: dict[str, Any] = {}
        for key, item in value.items():
            result[key] = "[已脱敏]" if is_sensitive_metadata_key(key) else sanitize_export_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_export_payload(item) for item in value]
    return deepcopy(value)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("export", help="导出当前项目的阶段交付记录")
    parser.add_argument("requirement_id", nargs="?", help="可选需求编号")
    parser.set_defaults(func=run)

    requirement_parser = subparsers.add_parser("export-requirement", help="导出指定需求的阶段交付记录")
    requirement_parser.add_argument("requirement_id", help="需求编号")
    requirement_parser.set_defaults(func=run)


def _formal_archive_export(archive: Mapping[str, object]) -> list[str]:
    """导出只显示正式路径、稳定编号和完整哈希，不复制可能含秘密的原始正文。"""

    formal = archive["formal"]
    reference_index = archive["reference_index"]
    status = archive["status"]
    manifest = archive["manifest"]
    current_documents = archive["current_documents"]
    current_paths = archive["current_paths"]
    if (
        not isinstance(formal, Mapping)
        or not isinstance(reference_index, Mapping)
        or not isinstance(status, Mapping)
        or not isinstance(manifest, list)
        or not isinstance(current_documents, Mapping)
        or not isinstance(current_paths, Mapping)
    ):
        raise SdlcError("正式档案导出数据不完整。", exit_code=1)
    entries = reference_index.get("entries")
    reference_count = len(entries) if isinstance(entries, Mapping) else 0
    lines = [
        "",
        "### 正式档案",
        f"- 流程档案：{formal['workflow_profile']}",
        f"- 来源 DRAFT：{formal['source_draft_id']}",
        f"- 来源修订 SHA-256：`{formal['source_revision_sha256']}`",
        f"- 正式清单：{len(manifest)} 项",
        f"- 正式引用：{reference_count} 项",
    ]
    for status_key, label in (
        ("requirement", "当前需求"),
        ("design", "当前技术方案"),
        ("test_matrix", "当前测试矩阵"),
    ):
        document = current_documents.get(status_key)
        relative = current_paths.get(status_key)
        if not isinstance(document, Mapping) or not isinstance(relative, str):
            raise SdlcError(f"正式档案缺少{label}。", exit_code=1)
        lines.append(f"- {label}：`{relative}`（{document.get('version', '')}）")

    lines.extend(["", "#### 归档清单"])
    for raw_item in manifest:
        if not isinstance(raw_item, Mapping):
            raise SdlcError("正式清单必须是对象列表。", exit_code=1)
        business_id = str(raw_item.get("business_id") or "无业务编号")
        lines.append(
            "- "
            f"{raw_item.get('artifact_id')} / {business_id} / "
            f"{raw_item.get('artifact_type')}："
            f"`{raw_item.get('archive_path')}`；"
            f"SHA-256 `{raw_item.get('sha256')}`"
        )
    return lines


def build_requirement_export(
    requirement: dict[str, object],
    state: dict[str, object],
    *,
    formal_archive: Mapping[str, object] | None = None,
) -> list[str]:
    requirement = sanitize_export_payload(requirement)
    lines = [
        f"## {requirement['requirement_id']} {requirement['title']}",
        f"- 状态：{requirement['status']}",
        f"- 优先级：{requirement.get('priority', 'normal')}",
        f"- 阻塞：{requirement.get('blocked_reason') or '无'}",
        "",
        "### 任务",
    ]
    tasks = requirement["tasks"]  # type: ignore[index]
    if tasks:
        lines.extend([f"- {task['task_id']} [{task['status']}] {task['title']}" for task in tasks])
    else:
        lines.append("- 暂无任务")

    lines.extend(["", "### 技术方案"])
    requirement_designs = requirement.get("designs", [])  # type: ignore[assignment]
    if requirement_designs:
        lines.extend(
            [
                f"- {design['design_id']} [{design['status']}] {design['title']}"
                for design in requirement_designs
            ]
        )
    else:
        lines.append("- 暂无技术方案")

    lines.extend(["", "### 变更记录"])
    requirement_changes = requirement["changes"]  # type: ignore[index]
    if requirement_changes:
        lines.extend([f"- {item['change_id']} [{item['status']}] {item['summary']}" for item in requirement_changes])
    else:
        lines.append("- 暂无变更")

    lines.extend(["", "### 验证记录"])
    verification_lines = []
    for task in tasks:
        for verification in task["verifications"]:
            verification_lines.append(f"- {verification['verification_id']}：{verification['summary']}")
    lines.extend(verification_lines or ["- 暂无验证"])

    lines.extend(["", "### 未完成问题"])
    open_tasks = [task for task in tasks if task["status"] not in {"done", "closed"}]
    if open_tasks:
        lines.extend([f"- {requirement['requirement_id']} / {task['task_id']} 还没完成" for task in open_tasks])
    else:
        lines.append("- 当前没有明显遗留问题")
    if formal_archive is not None:
        lines.extend(_formal_archive_export(sanitize_export_payload(formal_archive)))
    lines.append("")
    return lines


def render_export_text(
    state: dict[str, object],
    selected_requirements: list[dict[str, object]],
    *,
    scope_label: str,
    formal_archives: Mapping[str, Mapping[str, object]] | None = None,
) -> str:
    state = sanitize_export_payload(state)
    selected_requirements = sanitize_export_payload(selected_requirements)
    lines = [
        "# SDLC 阶段交付记录",
        "",
        f"- 项目名称：{state['project'].get('project_name', 'unknown')}",
        f"- 导出时间：{now_iso()}",
        f"- 导出范围：{scope_label}",
        "",
    ]
    for requirement in selected_requirements:
        requirement_id = str(requirement["requirement_id"])
        archive = formal_archives.get(requirement_id) if formal_archives is not None else None
        lines.extend(
            build_requirement_export(
                requirement,
                state,
                formal_archive=archive,
            )
        )

    recent_session = state["recent_session"]
    lines.extend(["## 最近交接"])
    if recent_session is None:
        lines.append("- 暂无正式交接记录")
    else:
        lines.append(f"- {recent_session['session_id']}：{recent_session['summary']}")
        lines.append(f"- 下一步：{recent_session['next_step']}")
    return join_lines(lines)


def _load_formal_archives(
    paths,
    requirements: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """显式 profile 决定读取分流；历史 facts 档案不进入文档优先导出门槛。"""

    result: dict[str, dict[str, object]] = {}
    for requirement in requirements:
        native_start = requirement.get("native_start")
        if not isinstance(native_start, Mapping) or native_start.get("workflow_profile") != DOCUMENT_FIRST_PROFILE:
            continue
        folder_name = str(requirement.get("folder_name") or "")
        candidate = Path(folder_name)
        if (
            not folder_name
            or candidate.name != folder_name
            or folder_name in {".", ".."}
        ):
            raise SdlcError(
                f"{requirement.get('requirement_id')} 的正式目录记录不合法。",
                exit_code=1,
            )
        archive = inspect_document_first_archive(
            paths,
            paths.requirements_dir / folder_name,
            expected_requirement=requirement,
        )
        if archive is None:
            raise SdlcError(
                f"{requirement.get('requirement_id')} 缺少文档优先正式档案。",
                exit_code=1,
            )
        result[str(requirement["requirement_id"])] = archive
    return result


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    state = derive_state(paths)
    if args.requirement_id:
        requirement = resolve_requirement(state, args.requirement_id)
        filename = f"{requirement['requirement_id']}.md"
        selected_requirements = [requirement]
        scope_label = f"{requirement['requirement_id']} {requirement['title']}"
    else:
        filename = "all-requirements.md"
        selected_requirements = list(state["requirements"].values())
        scope_label = "全部需求"

    # 先完整读取并核对正式档案，再创建导出文件，失败时不会留下半份可信报告。
    formal_archives = _load_formal_archives(paths, selected_requirements)
    output_path = paths.exports_dir / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_text = render_export_text(
        state,
        selected_requirements,
        scope_label=scope_label,
        formal_archives=formal_archives,
    )
    output_path.write_text(output_text, encoding="utf-8")

    print(f"已导出：.codex-sdlc/exports/{filename}")
    print(output_text, end="")
    return 0
