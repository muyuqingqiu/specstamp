from __future__ import annotations

import argparse
import re
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.state import derive_state
from codex_sdlc.services.design_service import (
    DesignArtifactService,
    DesignPlanService,
    DesignReferenceService,
    DesignSummaryService,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    summary_parser = subparsers.add_parser(
        "design-summary",
        help="导入 DRAFT 的总体设计说明和跨模块稳定引用",
    )
    summary_parser.add_argument(
        "draft",
        help="明确的 DRAFT 编号，例如 DRAFT-001",
    )
    summary_parser.add_argument(
        "--file",
        required=True,
        help="项目内 design-summary.v1 JSON 文件",
    )
    summary_parser.set_defaults(func=run_design_summary, command="design")

    artifact_parser = subparsers.add_parser(
        "design-artifact",
        help="导入 DRAFT 中已启用的模块化设计产物",
    )
    artifact_parser.add_argument(
        "draft",
        help="明确的 DRAFT 编号，例如 DRAFT-001",
    )
    artifact_parser.add_argument(
        "--file",
        required=True,
        help="项目内 design-artifact.v1 JSON 文件",
    )
    artifact_parser.set_defaults(func=run_design_artifact, command="design")

    plan_parser = subparsers.add_parser(
        "design-plan",
        help="导入 DRAFT 的结构化开发设计总计划和真实代码证据",
    )
    plan_parser.add_argument("draft", help="明确的 DRAFT 编号，例如 DRAFT-001")
    plan_parser.add_argument(
        "--file",
        required=True,
        help="项目内 design-plan.v1 JSON 文件",
    )
    plan_parser.set_defaults(func=run_design_plan, command="design")

    import_parser = subparsers.add_parser(
        "design-reference",
        help="从 technical-solution 原始资料导入技术方案引用",
    )
    import_parser.add_argument("draft", help="明确的 DRAFT 编号，例如 DRAFT-001")
    import_parser.add_argument(
        "--file",
        required=True,
        help="项目内 design-reference.v1 JSON 文件",
    )
    # CLI 的通用页脚仍把设计命令按旧命令名列入“已自行给出结果”的集合；
    # 这里沿用该分类，避免成功后追加一个已经下线的自由文本设计建议。
    import_parser.set_defaults(func=run_design_reference, command="design")

    confirm_parser = subparsers.add_parser(
        "design-reference-confirm",
        help="确认 DRAFT 中当前有效的技术方案引用",
    )
    confirm_parser.add_argument("draft", help="明确的 DRAFT 编号，例如 DRAFT-001")
    confirm_parser.add_argument("design", help="明确的 DES 编号，例如 DES-001")
    confirm_parser.set_defaults(func=run_design_reference_confirm, command="design-accept")

    # 旧命令只保留明确分流，不再接收自由文本，也不会生成固定章节或修改技术方案原文。
    legacy_parser = subparsers.add_parser("design", help="使用 design-reference 导入技术方案原文引用")
    legacy_parser.add_argument("items", nargs="*", help=argparse.SUPPRESS)
    legacy_parser.set_defaults(func=reject_legacy_design)

    legacy_accept_parser = subparsers.add_parser(
        "design-accept",
        help="使用 design-reference-confirm 确认技术方案引用",
    )
    legacy_accept_parser.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    legacy_accept_parser.set_defaults(func=reject_legacy_design_accept)


def _paths():
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    return root, build_paths(root)


def _print_draft_status(paths, draft_id: str) -> None:
    """写命令完成后读取统一派生状态，避免命令层自行猜测设计是否齐全。"""

    draft = derive_state(paths).get("drafts", {}).get(draft_id)
    if isinstance(draft, dict):
        print(f"DRAFT 状态：{draft['status']}")


def run_design_reference(args: argparse.Namespace) -> int:
    root, paths = _paths()
    outcome = DesignReferenceService(paths).import_file(
        str(args.draft or ""),
        str(args.file or ""),
    )
    record = outcome["record"]
    if outcome["action"] == "idempotent":
        print(f"技术方案引用已经存在：{record['design_id']}")
    else:
        print(f"已导入技术方案引用：{record['design_id']}")
    print(f"映射：{record['client_key']} -> {record['design_id']}")
    print(f"原始资料：{record['material_id']}")
    print(f"文件：.codex-sdlc/drafts/{record['draft_id']}/设计/des-index.v1.json")
    _print_draft_status(paths, str(record["draft_id"]))
    print(f"当前目录：{root}")
    return 0


def run_design_plan(args: argparse.Namespace) -> int:
    root, paths = _paths()
    outcome = DesignPlanService(paths).import_file(
        str(args.draft or ""),
        str(args.file or ""),
    )
    record = outcome["record"]
    assessment = outcome["assessment"]
    if outcome["action"] == "idempotent":
        print(f"开发设计总计划已经存在：{record['draft_id']}")
    else:
        print(f"已导入开发设计总计划：{record['draft_id']}")
    for client_key, module_id in record["mapping"].items():
        print(f"映射：{client_key} -> {module_id}")
    if assessment["status"] == "stale":
        print(f"代码证据已变化：{'、'.join(assessment['changed_paths'])}")
    else:
        print(f"代码证据有效：{record['code_evidence']['evidence_sha256']}")
    print(f"文件：.codex-sdlc/drafts/{record['draft_id']}/设计/design-plan.v1.json")
    _print_draft_status(paths, str(record["draft_id"]))
    print(f"当前目录：{root}")
    return 0


def run_design_artifact(args: argparse.Namespace) -> int:
    root, paths = _paths()
    outcome = DesignArtifactService(paths).import_file(
        str(args.draft or ""),
        str(args.file or ""),
    )
    record = outcome["record"]
    completion = outcome["completion"]
    if outcome["action"] == "idempotent":
        print(
            f"模块化设计产物已经存在：{record['artifact_id']}（版本 {record['revision']}）"
        )
    else:
        print(
            f"已导入模块化设计产物：{record['artifact_id']}（版本 {record['revision']}）"
        )
    print(f"JSON：.codex-sdlc/drafts/{record['draft_id']}/{record['output_path']}")
    markdown_path = Path(str(record["output_path"])).with_suffix(".md").as_posix()
    print(f"Markdown：.codex-sdlc/drafts/{record['draft_id']}/{markdown_path}")
    if completion["status"] == "complete":
        print("当前计划的启用模块均已有完整产物。")
    else:
        print(f"待导入模块：{'、'.join(completion['pending']) or '无'}")
        if completion["blocked"]:
            print(f"阻塞模块：{'、'.join(completion['blocked'])}")
    _print_draft_status(paths, str(record["draft_id"]))
    print(f"当前目录：{root}")
    return 0


def run_design_summary(args: argparse.Namespace) -> int:
    root, paths = _paths()
    outcome = DesignSummaryService(paths).import_file(
        str(args.draft or ""),
        str(args.file or ""),
    )
    record = outcome["record"]
    if outcome["action"] == "idempotent":
        print(
            f"总体设计说明已经存在：{record['summary_id']}（版本 {record['revision']}）"
        )
    else:
        print(
            f"已导入总体设计说明：{record['summary_id']}（版本 {record['revision']}）"
        )
    print(
        "JSON："
        f".codex-sdlc/drafts/{record['draft_id']}/设计/design-summary.v1.json"
    )
    print(
        "Markdown："
        f".codex-sdlc/drafts/{record['draft_id']}/设计/总体设计说明.md"
    )
    if record["invalidated_modules"]:
        print(f"需要重新核对模块：{'、'.join(record['invalidated_modules'])}")
        print("整体设计审核状态：stale")
    else:
        print("跨模块公共对象已经与当前完整模块产物对齐。")
    _print_draft_status(paths, str(record["draft_id"]))
    print(f"当前目录：{root}")
    return 0


def run_design_reference_confirm(args: argparse.Namespace) -> int:
    _root, paths = _paths()
    outcome = DesignReferenceService(paths, source="sdlc-design-reference-confirm").confirm(
        str(args.draft or ""),
        str(args.design or ""),
    )
    record = outcome["record"]
    if outcome["action"] == "idempotent":
        print(f"技术方案引用已经确认：{record['design_id']}")
    else:
        print(f"已确认技术方案引用：{record['design_id']}")
    print(f"原始资料：{record['material_id']}")
    print(f"适用需求：{'、'.join(record['applies_to'])}")
    print(f"文件：.codex-sdlc/drafts/{record['draft_id']}/设计/des-index.v1.json")
    _print_draft_status(paths, str(record["draft_id"]))
    return 0


def reject_legacy_design(args: argparse.Namespace) -> int:
    items = [str(item).strip() for item in getattr(args, "items", [])]
    if items and re.fullmatch(r"REQ-[0-9]+", items[0].upper()):
        # 正式需求的设计变化必须先建立变更工作区，不能再落回旧的自由文本设计记录。
        requirement_id = items[0].upper()
        raise SdlcError(
            f"正式 REQ 不能用 design 直接修改设计。请先用 change-create 为 {requirement_id} 建立 CHG，--request-key 填写本次变更的稳定请求键，再提交结构化变更包。",
            exit_code=1,
        )
    raise SdlcError(
        "技术方案必须先用 material 以 technical-solution 原样归档，再用 design-reference --file 导入结构化引用。",
        exit_code=1,
    )


def reject_legacy_design_accept(_args: argparse.Namespace) -> int:
    raise SdlcError(
        "技术方案引用必须用 design-reference-confirm 并明确指定 DRAFT 和 DES。",
        exit_code=1,
    )


__all__ = [
    "register",
    "run_design_artifact",
    "run_design_plan",
    "run_design_reference",
    "run_design_reference_confirm",
    "run_design_summary",
]
