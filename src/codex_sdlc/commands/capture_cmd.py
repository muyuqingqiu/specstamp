from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.commands.change_cmd import record_change
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    build_requirement_title,
    capture_ids,
    create_project_initialized_event,
    derive_state,
    generate_requirement_folder,
    next_number,
    refresh_materialized_state,
    requirement_ids,
    resolve_requirement,
)
from codex_sdlc.services.capture_service import CaptureService
from codex_sdlc.services.discuss_service import read_increment_document



def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    discuss_parser = subparsers.add_parser("discuss", help="追加结构化需求讨论 CAP")
    discuss_parser.add_argument("summary", nargs="?", help="自由文本不写入，请使用 --file")
    add_common_capture_fields(discuss_parser)
    discuss_parser.add_argument("--decision", action="append", default=[], help="自由文本决定不写入，请在 CAP JSON 中提供 decisions")
    discuss_parser.set_defaults(func=run_discuss)

    discuss_link_parser = subparsers.add_parser("discuss-link", help="把需求讨论草案纳入指定需求")
    discuss_link_parser.add_argument("requirement", help="需求编号")
    discuss_link_parser.add_argument("capture_ids", nargs="*", help="可选 capture 编号；不填则纳入所有待处理需求草案")
    discuss_link_parser.set_defaults(func=run_discuss_link)

    parser = subparsers.add_parser("capture", help="追加结构化 CAP")
    parser.add_argument("summary", nargs="?", help="自由文本不写入，请使用 --file")
    parser.add_argument("--note", default="", help="补充说明")
    parser.add_argument("--requirement", help="明确关联到哪个需求")
    parser.add_argument("--to-requirement", action="store_true", help="把这条 capture 直接转成新需求")
    parser.add_argument("--to-change", help="把这条 capture 直接转成指定需求的变更")
    parser.add_argument("--file", action="append", default=[], help="结构化 CAP JSON 文件；转换命令中可记录涉及文件")
    parser.add_argument("--command", dest="executed_commands", action="append", default=[], help="涉及命令，可重复传入")
    parser.add_argument("--question", action="append", default=[], help="待确认问题，可重复传入")
    parser.add_argument("--lesson", action="store_true", help="把这条记录作为需求级经验沉淀")
    parser.add_argument("--increment-file", default="", help="结构化 CAP JSON 文件")
    parser.set_defaults(func=run)

    transition_parser = subparsers.add_parser(
        "capture-transition", help="追加结构化 CAP 状态转换"
    )
    transition_parser.add_argument("--file", required=True, help="CAP 状态转换 JSON 文件")
    transition_parser.set_defaults(func=run_capture_transition)

    link_parser = subparsers.add_parser("capture-link", help="记录中途结论并直接关联到指定需求")
    link_parser.add_argument("requirement", help="需求编号")
    link_parser.add_argument("summary", help="本轮要补记的结论")
    add_common_capture_fields(link_parser)
    link_parser.set_defaults(func=run_link)

    requirement_parser = subparsers.add_parser("capture-requirement", help="把中途结论直接转成新需求")
    requirement_parser.add_argument("summary", help="本轮要补记的结论")
    add_common_capture_fields(requirement_parser)
    requirement_parser.set_defaults(func=run_to_requirement)

    change_parser = subparsers.add_parser("capture-change", help="把中途结论直接转成指定需求的变更")
    change_parser.add_argument("requirement", help="需求编号")
    change_parser.add_argument("summary", help="本轮要补记的结论")
    add_common_capture_fields(change_parser)
    change_parser.set_defaults(func=run_to_change)


def add_common_capture_fields(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--note", default="", help="补充说明")
    parser.add_argument("--file", action="append", default=[], help="结构化 CAP JSON 文件；转换命令中可记录涉及文件")
    parser.add_argument("--command", dest="executed_commands", action="append", default=[], help="涉及命令，可重复传入")
    parser.add_argument("--question", action="append", default=[], help="待确认问题，可重复传入")
    parser.add_argument("--lesson", action="store_true", help="把这条记录作为需求级经验沉淀")
    parser.add_argument("--increment-file", default="", help="结构化 CAP JSON 文件")


def run_link(args: argparse.Namespace) -> int:
    args.to_requirement = False
    args.to_change = None
    args.legacy_link = True
    return run(args)


def run_discuss(args: argparse.Namespace) -> int:
    args.requirement = None
    args.to_requirement = False
    args.to_change = None
    args.discussion = True
    return run(args)


def run_discuss_link(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")

    with project_lock(paths):
        state = derive_state(paths)
        requirement = resolve_requirement(state, args.requirement)
        requested_ids = set(args.capture_ids)
        pending_drafts = [
            capture
            for capture in state["captures"]
            if capture["status"] == "pending"
            and capture.get("target_type") == "requirement_draft"
            and (not requested_ids or capture["capture_id"] in requested_ids)
        ]
        if requested_ids:
            found_ids = {capture["capture_id"] for capture in pending_drafts}
            missing_ids = sorted(requested_ids - found_ids)
            if missing_ids:
                raise SdlcError(f"这些需求讨论记录不存在或已处理：{', '.join(missing_ids)}", exit_code=1)
        if not pending_drafts:
            raise SdlcError("当前没有待纳入的需求讨论草案。", exit_code=1)

        capture_id_list = [capture["capture_id"] for capture in pending_drafts]
        CaptureService(paths, source="sdlc-discuss").link_captures(
            requirement_id=requirement["requirement_id"],
            capture_ids=capture_id_list,
        )
        refresh_materialized_state(paths)

    print(f"已纳入需求讨论草案：{requirement['requirement_id']}")
    for capture_id in capture_id_list:
        print(f"- {capture_id}")
    return 0


def run_to_requirement(args: argparse.Namespace) -> int:
    args.requirement = None
    args.to_requirement = True
    args.to_change = None
    return run(args)


def run_to_change(args: argparse.Namespace) -> int:
    args.to_requirement = False
    args.to_change = args.requirement
    return run(args)


def single_active_requirement(state: dict[str, object], summary: str) -> dict[str, object] | None:
    active_requirements = state["active_requirements"]
    if len(active_requirements) == 1:
        return active_requirements[0]
    return None


def structured_increment_path(args: argparse.Namespace) -> Path | None:
    """无正文时允许 --file 直接作为结构化输入，修正 discuss 只记路径的旧行为。"""

    explicit = str(getattr(args, "increment_file", "") or "").strip()
    files = list(getattr(args, "file", []) or [])
    if explicit:
        if files:
            raise SdlcError("结构化 CAP 不能同时使用 --increment-file 和 --file。", exit_code=1)
        return Path(explicit).expanduser()
    if not getattr(args, "summary", None) and files:
        if len(files) != 1:
            raise SdlcError("结构化 CAP 只能指定一个 JSON 文件。", exit_code=1)
        return Path(files[0]).expanduser()
    return None


def run_structured_increment(args: argparse.Namespace, input_path: Path) -> int:
    """结构化文件是唯一能追加讨论 CAP 和用户决定的新写入入口。"""

    mixed_fields = (
        str(getattr(args, "summary", "") or "").strip(),
        str(getattr(args, "note", "") or "").strip(),
        list(getattr(args, "executed_commands", []) or []),
        list(getattr(args, "question", []) or []),
        list(getattr(args, "decision", []) or []),
        bool(getattr(args, "lesson", False)),
        getattr(args, "requirement", None),
        bool(getattr(args, "to_requirement", False)),
        getattr(args, "to_change", None),
    )
    if any(mixed_fields):
        raise SdlcError("结构化 CAP 文件不能和自由文本、问题、决定或转换参数混用。", exit_code=1)

    document = read_increment_document(input_path)
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。", exit_code=1)

    source = "sdlc-discuss" if getattr(args, "discussion", False) else "sdlc-capture"
    with project_lock(paths):
        state = derive_state(paths)
        prepared = CaptureService(paths, source=source).record_structured_increment(
            document,
            state=state,
        )

    capture_id = str(prepared.capture["capture_id"])
    print(f"已记录结构化 CAP：{capture_id}")
    print(f"DRAFT：{prepared.capture['draft_id']}")
    print(f"重复提交：{'是' if prepared.duplicate else '否'}")
    for decision in prepared.decisions:
        print(f"已记录用户决定：{decision['decision_id']}")
    print(f"文件：.codex-sdlc/captures/{capture_id}.md")
    return 0


def run_capture_transition(args: argparse.Namespace) -> int:
    """CAP 转换使用独立命令，初始登记文件始终保持不可变。"""

    document = read_increment_document(Path(args.file).expanduser())
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。", exit_code=1)
    with project_lock(paths):
        state = derive_state(paths)
        prepared = CaptureService(
            paths, source="sdlc-capture-transition"
        ).record_capture_transition(document, state=state)

    transition = prepared.transition
    print(f"已转换结构化 CAP：{transition['capture_id']}")
    print(f"状态：{transition['from_status']} -> {transition['to_status']}")
    print(f"重复提交：{'是' if prepared.duplicate else '否'}")
    return 0





def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    input_path = structured_increment_path(args)
    if input_path is not None:
        return run_structured_increment(args, input_path)
    if not args.summary:
        if getattr(args, "discussion", False):
            print(f"当前目录：{root}")
            if paths.events_file.exists():
                state = derive_state(paths)
                pending_items = [
                    item
                    for item in state["captures"]
                    if item["status"] == "pending"
                ]
                if pending_items:
                    print("当前未归类讨论和 capture：")
                    for item in pending_items:
                        print(f"- {item['capture_id']}：{item['summary']}")
            print("请使用 `--file <结构化CAP.json>` 提交需求讨论增量。")
            return 0
        print(f"当前目录：{root}")
        print("请使用 `--file <结构化CAP.json>` 提交中途增量。")
        return 0

    summary = args.summary.strip()
    if not summary:
        raise SdlcError("capture 内容不能为空。")
    if getattr(args, "discussion", False):
        raise SdlcError(
            "需求讨论只能追加结构化 CAP；请把增量写入 JSON 文件后使用 `--file`。",
            exit_code=1,
        )
    if (
        not getattr(args, "to_requirement", False)
        and not getattr(args, "to_change", None)
        and not getattr(args, "lesson", False)
        and not getattr(args, "legacy_link", False)
    ):
        raise SdlcError(
            "capture 只能追加结构化 CAP；请把增量写入 JSON 文件后使用 `--file`。",
            exit_code=1,
        )

    with project_lock(paths):
        if not paths.events_file.exists() or not paths.events_file.read_text(encoding="utf-8").strip():
            create_project_initialized_event(paths)
        state = derive_state(paths)
        capture_id = next_number(capture_ids(state), "CAP")
        changed_files = args.file or state["git_changed_files"]
        target_requirement = None
        if args.requirement:
            target_requirement = resolve_requirement(state, args.requirement)
        elif args.to_change:
            target_requirement = resolve_requirement(state, args.to_change)
        elif not args.to_requirement and not getattr(args, "discussion", False):
            target_requirement = single_active_requirement(state, summary)

        capture_status = "pending"
        linked_requirement_id = None
        target_type = "requirement_draft" if getattr(args, "discussion", False) else "capture"
        linked_change_id = None
        if target_requirement is not None:
            capture_status = "linked"
            linked_requirement_id = target_requirement["requirement_id"]
            target_type = "decision"
        if args.to_requirement:
            capture_status = "converted_requirement"
            target_type = "new_requirement"
        if args.to_change:
            capture_status = "converted_change"
            target_type = "change"
            linked_requirement_id = target_requirement["requirement_id"] if target_requirement else None
        is_lesson = bool(getattr(args, "lesson", False))
        if is_lesson and not args.to_requirement and not args.to_change:
            target_type = "lesson"
        discussion_draft_id = ""

        capture_payload = {
            "capture_id": capture_id,
            "summary": summary,
            "note": args.note,
            "status": capture_status,
            "target_type": target_type,
            "changed_files": changed_files,
            "commands": args.executed_commands,
            "questions": args.question,
            "draft_id": discussion_draft_id,
            "linked_change_id": linked_change_id,
            "file_path": f".codex-sdlc/captures/{capture_id}.md",
            "requirement_id": linked_requirement_id,
        }

        if not getattr(args, "discussion", False):
            CaptureService(paths, source="sdlc-capture").record_capture(
                capture_payload,
                requirement_id=str(linked_requirement_id) if linked_requirement_id else None,
            )

        created_requirement_id = None
        created_change_id = None
        if args.to_requirement:
            requirement_id = next_number(requirement_ids(state), "REQ")
            title = build_requirement_title(summary)
            folder_name = generate_requirement_folder(requirement_id, summary)
            CaptureService(paths, source="sdlc-capture").create_requirement(
                requirement_id=requirement_id,
                payload={
                    "title": title,
                    "description": summary,
                    "summary": title,
                    "folder_name": folder_name,
                    "flow_type": "capture 转需求",
                },
            )
            created_requirement_id = requirement_id

        if args.to_change and target_requirement is not None:
            created_change_id, _, _ = record_change(
                paths,
                state=state,
                requirement=target_requirement,
                description=summary,
                reason=args.note,
                confirm="capture 转变更",
                capture_ids=[capture_id],
            )

        refresh_materialized_state(paths)

    if target_type == "lesson":
        print(f"已记录需求级经验：{capture_id}")
    else:
        print(f"已记录 capture：{capture_id}")
    print(f"文件：.codex-sdlc/captures/{capture_id}.md")
    if linked_requirement_id:
        print(f"已关联需求：{linked_requirement_id}")
    if created_requirement_id:
        print(f"已转成新需求：{created_requirement_id}")
    if created_change_id:
        print(f"已转成需求变更：{created_change_id}")
    print("下一步建议：先用 `$sdlc-next` 看看这条结论接下来该落到哪一步。")
    return 0
