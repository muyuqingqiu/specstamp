from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.lessons import (
    GLOBAL_LESSON_LEVELS,
    all_global_lessons,
    clean_items,
    lesson_index_path,
    lesson_level_dir,
    load_lesson_index,
    match_global_lessons,
    read_json,
    scan_dir,
    scan_lesson_candidates,
    write_global_lesson,
    write_json,
    write_scan,
)
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import (
    append_event,
    capture_ids,
    derive_state,
    next_number,
    refresh_materialized_state,
    resolve_requirement,
)


LESSON_LEVELS = ("requirement", *GLOBAL_LESSON_LEVELS)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("lessons", help="管理需求级、跨需求、项目级和 AGENTS 候选经验")
    lesson_subparsers = parser.add_subparsers(dest="lesson_command", parser_class=argparse.ArgumentParser)

    add_parser = lesson_subparsers.add_parser("add", help="新增一条经验")
    add_parser.add_argument("content", help="经验内容")
    add_parser.add_argument("--level", choices=LESSON_LEVELS, default="requirement", help="经验级别")
    add_parser.add_argument("--requirement", default="", help="需求编号；需求级经验必填，未填时仅允许使用唯一活跃需求")
    add_parser.add_argument("--task", default="", help="来源任务编号")
    add_parser.add_argument("--title", default="", help="经验标题")
    add_parser.add_argument("--scope", action="append", default=[], help="适用范围，可重复传入")
    add_parser.add_argument("--not-for", default="", help="不适用范围")
    add_parser.add_argument("--file", action="append", default=[], help="来源文件，可重复传入")
    add_parser.add_argument("--command", dest="lesson_commands", action="append", default=[], help="可复用命令，可重复传入")
    add_parser.set_defaults(func=run_add)

    list_parser = lesson_subparsers.add_parser("list", help="查看经验列表")
    list_parser.add_argument("--level", choices=LESSON_LEVELS, default="", help="只看某个级别")
    list_parser.add_argument("--include-retired", action="store_true", help="包含已废弃经验")
    list_parser.set_defaults(func=run_list)

    show_parser = lesson_subparsers.add_parser("show", help="查看单条经验")
    show_parser.add_argument("lesson_id", help="经验编号，例如 LES-001")
    show_parser.set_defaults(func=run_show)

    match_parser = lesson_subparsers.add_parser("match", help="按显式文件关系列出跨需求/项目级经验")
    match_parser.add_argument("--file", action="append", default=[], required=True, help="精确文件路径，可重复传入")
    match_parser.set_defaults(func=run_match)

    scan_parser = lesson_subparsers.add_parser("scan", help="扫描当前项目相关 SDLC 文件，生成经验候选报告")
    scan_parser.add_argument("--deep", action="store_true", help="深扫常用目录中的同项目 SDLC 文件")
    scan_parser.set_defaults(func=run_scan)

    apply_parser = lesson_subparsers.add_parser("apply", help="把扫描报告里的候选经验写入对应层级")
    apply_parser.add_argument("scan_id", help="扫描编号，例如 SCAN-001")
    apply_parser.add_argument("--only", default="", help="只应用指定候选，例如 1,3,5 或 C-001,C-003")
    apply_parser.add_argument("--level", choices=LESSON_LEVELS, default="", help="强制写入指定级别")
    apply_parser.set_defaults(func=run_apply)

    promote_parser = lesson_subparsers.add_parser("promote", help="晋升经验级别")
    promote_parser.add_argument("lesson_id", help="经验编号")
    promote_parser.add_argument("--to", choices=GLOBAL_LESSON_LEVELS, required=True, help="目标级别")
    promote_parser.set_defaults(func=run_promote)

    demote_parser = lesson_subparsers.add_parser("demote", help="降级经验级别")
    demote_parser.add_argument("lesson_id", help="经验编号")
    demote_parser.add_argument("--to", choices=GLOBAL_LESSON_LEVELS, required=True, help="目标级别")
    demote_parser.set_defaults(func=run_demote)

    retire_parser = lesson_subparsers.add_parser("retire", help="废弃一条经验")
    retire_parser.add_argument("lesson_id", help="经验编号")
    retire_parser.add_argument("--reason", default="", help="废弃原因")
    retire_parser.set_defaults(func=run_retire)

    parser.set_defaults(func=run_default)


def resolve_active_requirement(state: dict[str, object], raw_requirement: str) -> dict[str, object]:
    if raw_requirement:
        return resolve_requirement(state, raw_requirement)
    active = [item for item in state.get("active_requirements", []) if isinstance(item, dict)]
    if len(active) == 1:
        return active[0]
    if not active:
        raise SdlcError("当前没有活跃需求，需求级经验请指定 `--requirement REQ-001`。")
    raise SdlcError("当前有多个活跃需求，需求级经验请指定 `--requirement REQ-001`。")


def add_requirement_lesson(paths, state: dict[str, object], args: argparse.Namespace, content: str) -> str:
    requirement = resolve_active_requirement(state, str(args.requirement or ""))
    capture_id = next_number(capture_ids(state), "CAP")
    append_event(
        paths,
        event_type="capture_recorded",
        source="sdlc-lessons",
        summary=f"记录需求级经验 {capture_id}",
        requirement_id=str(requirement["requirement_id"]),
        task_id=str(args.task or "") or None,
        payload={
            "capture_id": capture_id,
            "summary": content,
            "note": "由 $sdlc-lessons 写入的需求级经验。",
            "status": "linked",
            "target_type": "lesson",
            "changed_files": clean_items(args.file),
            "commands": clean_items(getattr(args, "lesson_commands", [])),
            "questions": [],
            "linked_change_id": None,
            "file_path": f".codex-sdlc/captures/{capture_id}.md",
        },
    )
    refresh_materialized_state(paths)
    return capture_id


def run_add(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    content = str(args.content or "").strip()
    if not content:
        raise SdlcError("经验内容不能为空。")
    with project_lock(paths):
        state = derive_state(paths)
        if args.level == "requirement":
            capture_id = add_requirement_lesson(paths, state, args, content)
            print(f"已记录需求级经验：{capture_id}")
            print("位置：当前需求包 `lessons.md`")
        else:
            lesson = write_global_lesson(
                paths,
                {
                    "level": args.level,
                    "title": args.title,
                    "summary": content,
                    "scope": args.scope,
                    "not_for": args.not_for,
                    "source_requirement": args.requirement,
                    "source_task": args.task,
                    "source_files": args.file,
                    "commands": args.lesson_commands,
                },
            )
            print(f"已记录{level_label(args.level)}经验：{lesson['lesson_id']}")
            print(f"位置：.codex-sdlc/lessons/{args.level}/{lesson['lesson_id']}.md")
    print("下一步建议：$sdlc-lessons list")
    return 0


def level_label(level: str) -> str:
    return {
        "requirement": "需求级",
        "cross-requirement": "跨需求",
        "project": "项目级",
        "agents-candidates": "AGENTS 候选",
    }.get(level, level)


def run_list(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    state = derive_state(paths) if paths.events_file.exists() else {"active_requirements": []}
    print("SDLC 经验列表")
    if not args.level or args.level == "requirement":
        for requirement in state.get("active_requirements", []):
            if not isinstance(requirement, dict):
                continue
            lessons = [
                capture
                for capture in requirement.get("captures", [])
                if isinstance(capture, dict) and capture.get("target_type") == "lesson"
            ]
            if lessons:
                print(f"需求级经验：{requirement.get('requirement_id')} {requirement.get('title')}")
                for item in lessons:
                    print(f"- {item.get('capture_id')}：{str(item.get('summary', '')).strip()[:96]}")
    global_lessons = all_global_lessons(paths, include_retired=args.include_retired)
    if args.level:
        global_lessons = [item for item in global_lessons if item.get("level") == args.level]
    if global_lessons:
        print("跨需求/项目级经验：")
        for item in global_lessons:
            status = f"（{item.get('status')}）" if item.get("status") != "active" else ""
            print(f"- {item.get('lesson_id')} [{level_label(str(item.get('level')))}]{status} {item.get('title')}")
    if not global_lessons:
        print("跨需求/项目级经验：暂无")
    print("下一步建议：需要查看文件相关经验时使用 `$sdlc-lessons match --file 精确路径`。")
    return 0


def find_global_lesson(paths, lesson_id: str) -> dict[str, object]:
    for lesson in all_global_lessons(paths, include_retired=True):
        if lesson.get("lesson_id") == lesson_id:
            return lesson
    raise SdlcError(f"没有找到跨需求/项目级经验：{lesson_id}")


def run_show(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    lesson = find_global_lesson(paths, args.lesson_id)
    print(f"{lesson.get('lesson_id')} {lesson.get('title')}")
    print(f"- 级别：{level_label(str(lesson.get('level')))}")
    print(f"- 状态：{lesson.get('status')}")
    print(f"- 结论：{lesson.get('summary')}")
    scope = "、".join(clean_items(lesson.get("scope", []))) or "未限定"
    print(f"- 适用范围：{scope}")
    print(f"- 来源：{lesson.get('source_requirement') or '未记录'} {lesson.get('source_task') or ''}".rstrip())
    print(f"- 文件：.codex-sdlc/lessons/{lesson.get('level')}/{lesson.get('lesson_id')}.md")
    return 0


def run_match(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    lessons = match_global_lessons(paths, files=clean_items(args.file))
    print("经验匹配结果")
    if not lessons:
        print("- 暂无命中")
        return 0
    for lesson in lessons:
        print(f"- {lesson.get('lesson_id')} [{level_label(str(lesson.get('level')))}] score={lesson.get('match_score')}：{lesson.get('title')}")
    return 0


def run_scan(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    with project_lock(paths):
        candidates = scan_lesson_candidates(paths, deep=bool(args.deep))
        scan = write_scan(paths, candidates, deep=bool(args.deep))
    print("已生成经验扫描报告")
    print(f"- 扫描编号：{scan['scan_id']}")
    print(f"- 候选数量：{scan['candidate_count']}")
    print(f"- 文件：.codex-sdlc/lessons/scans/{scan['scan_id']}.md")
    print(f"下一步建议：确认后使用 `$sdlc-lessons apply {scan['scan_id']}`。")
    return 0


def selected_candidates(candidates: list[dict[str, object]], raw_only: str) -> list[dict[str, object]]:
    if not raw_only.strip():
        return candidates
    wanted: set[str] = set()
    for token in raw_only.split(","):
        text = token.strip().upper()
        if not text:
            continue
        if text.isdigit():
            wanted.add(f"C-{int(text):03d}")
        else:
            wanted.add(text)
    return [item for item in candidates if str(item.get("candidate_id", "")).upper() in wanted]


def run_apply(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    scan = read_json(scan_dir(paths) / f"{args.scan_id}.json", {})
    if not isinstance(scan, dict) or not scan.get("candidates"):
        raise SdlcError(f"没有找到扫描报告：{args.scan_id}")
    applied: list[str] = []
    with project_lock(paths):
        state = derive_state(paths)
        for candidate in selected_candidates(scan.get("candidates", []), args.only):
            level = args.level or str(candidate.get("recommended_level") or "requirement")
            content = str(candidate.get("summary", "")).strip()
            namespace = argparse.Namespace(
                requirement=candidate.get("source_requirement") or "",
                task=candidate.get("source_task") or "",
                file=candidate.get("source_files") or [],
                lesson_commands=[],
            )
            if level == "requirement":
                capture_id = add_requirement_lesson(paths, state, namespace, content)
                applied.append(capture_id)
                state = derive_state(paths)
            else:
                lesson = write_global_lesson(
                    paths,
                    {
                        "level": level,
                        "title": candidate.get("title"),
                        "summary": content,
                        "scope": candidate.get("scope", []),
                        "source_requirement": candidate.get("source_requirement", ""),
                        "source_task": candidate.get("source_task", ""),
                        "source_files": candidate.get("source_files", []),
                        "source_snapshot": candidate.get("source_snapshot", ""),
                    },
                )
                applied.append(str(lesson["lesson_id"]))
    print("已应用经验扫描候选")
    print("- 写入：" + ("、".join(applied) if applied else "暂无"))
    print("下一步建议：$sdlc-lessons list")
    return 0


def rewrite_lesson(paths, lesson_id: str, *, level: str | None = None, status: str | None = None, reason: str = "") -> dict[str, object]:
    lesson = dict(find_global_lesson(paths, lesson_id))
    old_level = str(lesson.get("level", ""))
    if level:
        lesson["level"] = level
    if status:
        lesson["status"] = status
    if reason:
        lesson["stale_reason"] = reason
    updated = write_global_lesson(paths, lesson)
    new_level = str(updated.get("level", ""))
    if old_level and old_level != new_level:
        for suffix in [".json", ".md"]:
            old_path = lesson_level_dir(paths, old_level) / f"{lesson_id}{suffix}"
            if old_path.exists():
                old_path.unlink()
    return updated


def run_promote(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    with project_lock(paths):
        lesson = rewrite_lesson(paths, args.lesson_id, level=args.to)
    print(f"已晋升经验：{lesson['lesson_id']} -> {level_label(args.to)}")
    return 0


def run_demote(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    with project_lock(paths):
        lesson = rewrite_lesson(paths, args.lesson_id, level=args.to)
    print(f"已降级经验：{lesson['lesson_id']} -> {level_label(args.to)}")
    return 0


def run_retire(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    with project_lock(paths):
        lesson = rewrite_lesson(paths, args.lesson_id, status="retired", reason=args.reason or "用户废弃")
    print(f"已废弃经验：{lesson['lesson_id']}")
    return 0


def run_default(_args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    index = load_lesson_index(paths)
    print("SDLC 经验库")
    print(f"- 目录：{paths.lessons_dir}")
    print(f"- 跨需求/项目级经验：{len(index.get('lessons', []))}")
    print(f"- 扫描报告：{len(index.get('scans', []))}")
    print("下一步建议：查看用 `$sdlc-lessons list`，扫描用 `$sdlc-lessons scan`。")
    return 0
