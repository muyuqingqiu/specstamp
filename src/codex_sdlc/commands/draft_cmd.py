from __future__ import annotations

import argparse
import json
from pathlib import Path

from codex_sdlc.core import draft_artifacts, draft_lifecycle
from codex_sdlc.core.atomic_import import recover_atomic_imports
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_COVERAGE_SCHEMA,
    REQUIREMENT_SPLIT_SCHEMA,
    read_requirement_document,
)
from codex_sdlc.core.structured_contract import canonical_json_text
from codex_sdlc.core.state import (
    create_project_initialized_event,
    derive_state,
    refresh_materialized_state,
    shorten_text,
)
from codex_sdlc.services.draft_service import DraftMutationService, evaluate_draft
from codex_sdlc.services.review_service import (
    create_requirement_review,
    requirement_review_status,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("draft", help="写入、查看或刷新 DRAFT 工作包")
    draft_subparsers = parser.add_subparsers(dest="draft_command", parser_class=type(parser))

    create_parser = draft_subparsers.add_parser("create", help="创建新的 DRAFT")
    create_parser.add_argument("title", help="草稿标题")
    create_parser.set_defaults(func=run_create)

    requirement_parser = draft_subparsers.add_parser("requirement", help="写入需求草稿")
    requirement_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    requirement_parser.add_argument("content", nargs="?", default="", help="可直接传入草稿内容")
    requirement_parser.add_argument("--file", dest="content_file", default="", help="从 Markdown 文件读取需求草稿")
    requirement_parser.set_defaults(func=run_requirement)

    requirements_parser = draft_subparsers.add_parser(
        "requirements", help="原子导入需求拆分和覆盖关系"
    )
    requirements_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    requirements_parser.add_argument(
        "--split-file", required=True, help="requirement-split.v1 JSON 文件"
    )
    requirements_parser.add_argument(
        "--coverage-file", required=True, help="requirement-coverage.v1 JSON 文件"
    )
    requirements_parser.set_defaults(func=run_requirements)

    requirement_review_parser = draft_subparsers.add_parser(
        "requirement-review", help="创建或查看当前需求拆分审核"
    )
    requirement_review_children = requirement_review_parser.add_subparsers(
        dest="requirement_review_command", parser_class=type(parser)
    )
    requirement_review_create = requirement_review_children.add_parser(
        "create", help="按当前完整需求输入创建审核请求"
    )
    requirement_review_create.add_argument(
        "draft_id", help="DRAFT 编号，例如 DRAFT-001"
    )
    requirement_review_create.set_defaults(func=run_requirement_review_create)
    requirement_review_status_parser = requirement_review_children.add_parser(
        "status", help="查看当前有效、待修复或过期的需求审核"
    )
    requirement_review_status_parser.add_argument(
        "draft_id", help="DRAFT 编号，例如 DRAFT-001"
    )
    requirement_review_status_parser.add_argument(
        "--review", default="", help="只查看一个审核请求编号"
    )
    requirement_review_status_parser.set_defaults(func=run_requirement_review_status)

    requirement_confirm_parser = draft_subparsers.add_parser(
        "requirement-confirm",
        help="把用户确认绑定到当前已通过的需求审核",
    )
    requirement_confirm_parser.add_argument(
        "draft_id",
        help="DRAFT 编号，例如 DRAFT-001",
    )
    requirement_confirm_parser.add_argument(
        "--review",
        required=True,
        help="当前已通过的需求审核编号，例如 REV-001",
    )
    requirement_confirm_parser.set_defaults(func=run_requirement_confirm)

    design_parser = draft_subparsers.add_parser("design", help="写入技术草稿")
    design_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    design_parser.add_argument("content", nargs="?", default="", help="可直接传入草稿内容")
    design_parser.add_argument("--file", dest="content_file", default="", help="从 Markdown 文件读取技术草稿")
    design_parser.set_defaults(func=run_design)

    question_parser = draft_subparsers.add_parser("question", help="记录待用户确认的问题")
    question_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    question_parser.add_argument("question", help="需要用户确认的问题")
    question_parser.set_defaults(func=run_question)

    decision_parser = draft_subparsers.add_parser("decision", help="记录用户已经确认的决定")
    decision_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    decision_parser.add_argument("decision", help="用户确认内容")
    decision_parser.add_argument(
        "--resolve-question",
        nargs="?",
        const="__AUTO__",
        default="",
        help="同时把已回答的问题从 questions.md 移除；不传具体问题时，只允许当前只有一个待确认问题",
    )
    decision_parser.set_defaults(func=run_decision)

    resolve_parser = draft_subparsers.add_parser("resolve", help="解决当前待确认问题并记录用户决定")
    resolve_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    resolve_parser.add_argument("question", help="要标记为已解决的问题")
    resolve_parser.add_argument("--decision", required=True, help="用户确认内容")
    resolve_parser.set_defaults(func=run_resolve)

    review_parser = draft_subparsers.add_parser("review", help="记录草稿审查结果")
    review_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    review_parser.add_argument("content", nargs="?", default="", help="可直接传入审查内容")
    review_parser.add_argument("--file", dest="content_file", default="", help="从 Markdown 文件读取审查内容")
    review_parser.set_defaults(func=run_review)

    status_parser = draft_subparsers.add_parser("status", help="查看按草稿内容计算出的有效状态")
    status_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    status_parser.set_defaults(func=run_status)

    refresh_parser = draft_subparsers.add_parser("refresh", help="按事件和 JSON 数据重建 DRAFT 阅读文件")
    refresh_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    refresh_parser.set_defaults(func=run_refresh)

    source_index_parser = draft_subparsers.add_parser("source-index", help="按当前四份 DRAFT 输入生成来源索引")
    source_index_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    source_index_parser.set_defaults(func=run_source_index)

    facts_parser = draft_subparsers.add_parser("facts", help="写入模型生成并带原文锚点的事实文件")
    facts_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    facts_parser.add_argument("--kind", choices=("requirement", "design"), required=True, help="事实归属")
    facts_parser.add_argument("--file", dest="facts_file", required=True, help="模型事实 JSON 文件")
    facts_parser.set_defaults(func=run_facts)

    model_review_parser = draft_subparsers.add_parser("model-review", help="写入独立角色生成的模型复核文件")
    model_review_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    model_review_parser.add_argument("--file", dest="review_file", required=True, help="模型复核 JSON 文件")
    model_review_parser.set_defaults(func=run_model_review)

    review_request_parser = draft_subparsers.add_parser("review-request", help="冻结当前 facts 并创建一次性独立复核请求")
    review_request_parser.add_argument("draft_id", help="DRAFT 编号，例如 DRAFT-001")
    review_request_parser.set_defaults(func=run_review_request)

    parser.set_defaults(func=run_default)


def run_default(_args: argparse.Namespace) -> int:
    print("请指定 DRAFT 子命令：create / requirement / requirements / requirement-review / requirement-confirm / design / question / decision / resolve / review / source-index / facts / model-review / status / refresh")
    return 0


def clean_text(value: object) -> str:
    return str(value or "").strip()


def ensure_initialized(paths) -> None:
    if not paths.events_file.exists() or not paths.events_file.read_text(encoding="utf-8").strip():
        create_project_initialized_event(paths)


def load_markdown_content(raw_text: str, file_path: str, *, label: str) -> str:
    text = clean_text(raw_text)
    file_value = clean_text(file_path)
    if text and file_value:
        raise SdlcError(f"{label} 不能同时传正文和 --file，请二选一。", exit_code=1)
    if file_value:
        path = Path(file_value).expanduser()
        if not path.exists():
            raise SdlcError(f"{label}文件不存在：{path}", exit_code=1)
        text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise SdlcError(f"{label}不能为空，请直接传内容或使用 --file。", exit_code=1)
    return text


def ensure_draft(state: dict[str, object], draft_id: str) -> dict[str, object]:
    drafts = state.get("drafts", {})
    if not isinstance(drafts, dict):
        raise SdlcError("当前 DRAFT 状态读取失败。", exit_code=1)
    clean_draft_id = clean_text(draft_id).upper()
    draft = drafts.get(clean_draft_id)
    if not isinstance(draft, dict):
        raise SdlcError(f"没有找到 DRAFT `{clean_draft_id}`。", exit_code=1)
    return draft


def ensure_draft_editable(draft: dict[str, object]) -> None:
    """正式建档后的草稿只保留追溯用途，任何普通写入都不能重新打开它。"""

    if draft_lifecycle.is_started_draft(draft):
        raise SdlcError(
            f"{draft.get('draft_id') or 'DRAFT'} 已经正式建档，不能再修改、回退或补写草稿内容。",
            exit_code=1,
        )


def draft_title_from_content(prefix: str, content: str) -> str:
    # 这里故意只做很轻的标题收口，目的是让状态列表能快速看懂内容方向，不替模型改写正文。
    return f"{prefix}：{shorten_text(content, 24)}"


def run_create(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    title = clean_text(args.title)
    if not title:
        raise SdlcError("DRAFT 标题不能为空。", exit_code=1)

    with project_lock(paths):
        ensure_initialized(paths)
        draft_id, _assessment = DraftMutationService(paths, source="sdlc-draft").create(title)

    print(f"已创建 DRAFT：{draft_id}")
    print(f"标题：{title}")
    print(f"目录：.codex-sdlc/drafts/{draft_id}")
    return 0


def run_requirement(args: argparse.Namespace) -> int:
    return run_markdown_update(
        args,
        label="需求草稿",
        event_type="draft_requirement_updated",
        content_key="requirement_body",
        summary_key="requirement_summary",
        summary_prefix="需求草稿",
        output_label="已写入需求草稿",
    )


def run_requirements(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    # 两份输入先严格读取完毕；第二份无效时不会把第一份交给任何写入入口。
    split_document = read_requirement_document(
        Path(args.split_file).expanduser(), schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    coverage_document = read_requirement_document(
        Path(args.coverage_file).expanduser(), schema_name=REQUIREMENT_COVERAGE_SCHEMA
    )
    outcome = DraftMutationService(paths, source="sdlc-draft").import_requirements(
        clean_text(args.draft_id).upper(),
        split_document,
        coverage_document,
    )

    print(f"已导入需求拆分与覆盖关系：{clean_text(args.draft_id).upper()}")
    print(f"导入包：{outcome.result.package_key}")
    print(f"重复提交：{'是' if outcome.result.duplicate else '否'}")
    print("编号映射：")
    for client_key, formal_id in sorted(outcome.result.mapping.items()):
        print(f"- {client_key} -> {formal_id}")
    if outcome.review_blockers:
        print("需求审核仍被以下明确状态阻止：")
        for blocker in outcome.review_blockers:
            print(f"- {blocker}")
    return 0


def run_requirement_review_create(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    result = create_requirement_review(
        build_paths(root), draft_id=clean_text(args.draft_id).upper()
    )
    print(canonical_json_text(result), end="")
    return 0


def run_requirement_review_status(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    result = requirement_review_status(
        build_paths(root),
        draft_id=clean_text(args.draft_id).upper(),
        review_id=clean_text(args.review).upper() or None,
    )
    print(canonical_json_text(result), end="")
    return 0


def run_requirement_confirm(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    draft_id = clean_text(args.draft_id).upper()
    review_id = clean_text(args.review).upper()
    # CLI 只把用户选中的审核轮次交给现有服务；输入哈希、审核有效性、
    # 确认编号和时间仍由统一需求确认合同生成，命令层不复制业务判断。
    outcome = DraftMutationService(
        build_paths(root),
        source="sdlc-draft-requirement-confirm",
    ).confirm_requirement(
        draft_id,
        review_id=review_id,
    )
    confirmation = outcome["confirmation"]
    if outcome["action"] == "idempotent":
        print(f"需求已经确认：{draft_id}")
    else:
        print(f"已确认需求：{draft_id}")
    print(f"需求确认：{confirmation['confirmation_id']}")
    print(f"需求审核：{confirmation['review_id']}")
    print(f"DRAFT 状态：{outcome['status']}")
    print(
        "文件："
        f".codex-sdlc/drafts/{draft_id}/需求/requirement-confirmation.v1.json"
    )
    return 0


def run_design(args: argparse.Namespace) -> int:
    return run_markdown_update(
        args,
        label="技术草稿",
        event_type="draft_design_updated",
        content_key="design_body",
        summary_key="design_summary",
        summary_prefix="技术草稿",
        output_label="已写入技术草稿",
    )


def run_review(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    content = load_markdown_content(args.content, args.content_file, label="审查内容")
    draft_id = clean_text(args.draft_id).upper()

    with project_lock(paths):
        ensure_initialized(paths)
        DraftMutationService(paths, source="sdlc-draft").record_review(draft_id, content)

    print(f"已写入审查结果：{draft_id}")
    return 0


def run_question(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    question = clean_text(args.question)
    draft_id = clean_text(args.draft_id).upper()
    if not question:
        raise SdlcError("问题内容不能为空。", exit_code=1)

    with project_lock(paths):
        ensure_initialized(paths)
        DraftMutationService(paths, source="sdlc-draft").add_question(draft_id, question)

    print(f"已记录待确认问题：{draft_id}")
    return 0


def draft_status_after_resolve(draft: dict[str, object], questions: list[str]) -> str:
    # 清完问题后回到哪个阶段，统一走生命周期规则，避免命令层各写一套状态判断。
    return draft_lifecycle.status_after_question_resolve(draft, questions)


def resolve_questions(draft: dict[str, object], raw_target: str) -> tuple[list[str], list[str]]:
    questions = [clean_text(item) for item in draft.get("questions", []) if clean_text(item)]
    target = clean_text(raw_target)
    if not target:
        return questions, []
    if target == "__AUTO__":
        if not questions:
            raise SdlcError("当前没有待确认问题，不需要清理。", exit_code=1)
        if len(questions) > 1:
            lines = ["当前有多个待确认问题，请在 --resolve-question 后写清要解决哪一个："]
            lines.extend(f"- {item}" for item in questions)
            raise SdlcError("\n".join(lines), exit_code=1)
        return [], questions

    # 待确认问题是状态合同的一部分，只允许完整值精确命中。片段包含关系会把两句相似问题误当成同一个问题。
    matched = [item for item in questions if item == target]
    if not matched:
        lines = [f"没有找到要解决的问题：{target}", "必须传入完整问题文本；当前待确认问题："]
        lines.extend(f"- {item}" for item in questions or ["暂无待确认问题"])
        raise SdlcError("\n".join(lines), exit_code=1)
    if len(matched) > 1:
        lines = ["匹配到多个问题，请传更完整的问题文本："]
        lines.extend(f"- {item}" for item in matched)
        raise SdlcError("\n".join(lines), exit_code=1)
    resolved = matched[0]
    return [item for item in questions if item != resolved], [resolved]


def record_decision(paths, *, draft: dict[str, object], draft_id: str, decision: str, resolve_question: str = "") -> tuple[list[str], list[str], str]:
    remaining_questions, resolved_questions = resolve_questions(draft, resolve_question)
    assessment = DraftMutationService(paths, source="sdlc-draft").record_decision(
        draft_id,
        decision,
        remaining_questions=remaining_questions if resolve_question else None,
        resolved_questions=resolved_questions,
    )
    next_status = assessment.effective_status
    return remaining_questions, resolved_questions, next_status


def run_decision(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    decision = clean_text(args.decision)
    draft_id = clean_text(args.draft_id).upper()
    resolve_question = clean_text(getattr(args, "resolve_question", ""))
    if not decision:
        raise SdlcError("决定内容不能为空。", exit_code=1)

    with project_lock(paths):
        ensure_initialized(paths)
        state = derive_state(paths)
        draft = ensure_draft(state, draft_id)
        ensure_draft_editable(draft)
        _remaining_questions, resolved_questions, next_status = record_decision(
            paths,
            draft=draft,
            draft_id=draft_id,
            decision=decision,
            resolve_question=resolve_question,
        )

    print(f"已记录用户决定：{draft_id}")
    if resolved_questions:
        for question in resolved_questions:
            print(f"已解决待确认问题：{question}")
        print(f"DRAFT 状态：{next_status}")
    return 0


def run_resolve(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    decision = clean_text(args.decision)
    draft_id = clean_text(args.draft_id).upper()
    question = clean_text(args.question)
    if not decision:
        raise SdlcError("决定内容不能为空。", exit_code=1)
    if not question:
        raise SdlcError("要解决的问题不能为空。", exit_code=1)

    with project_lock(paths):
        ensure_initialized(paths)
        state = derive_state(paths)
        draft = ensure_draft(state, draft_id)
        ensure_draft_editable(draft)
        _remaining_questions, resolved_questions, next_status = record_decision(
            paths,
            draft=draft,
            draft_id=draft_id,
            decision=decision,
            resolve_question=question,
        )

    print(f"已记录用户决定：{draft_id}")
    for resolved in resolved_questions:
        print(f"已解决待确认问题：{resolved}")
    print(f"DRAFT 状态：{next_status}")
    return 0


def run_status(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    with project_lock(paths):
        ensure_initialized(paths)
        state = derive_state(paths)
        draft = ensure_draft(state, draft_id)
        # 读取旧 DRAFT 时只补固定目录，不登记不存在的业务产物，也不改写旧正文和 facts。
        draft_artifacts.ensure_draft_layout(paths, draft_id)
        assessment = evaluate_draft(draft)

    print(f"DRAFT 状态：{draft_id} -> {assessment.effective_status}")
    print(f"原因：{assessment.reason}")
    print(f"模型事实：{assessment.facts_status}")
    if assessment.next_action:
        print(f"下一步：{assessment.next_action}")
    return 0


def run_refresh(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    # 原子目录已经改名但事件或登记尚未补齐时，先按 T-002 事务日志恢复。
    if paths.events_file.exists():
        recover_atomic_imports(paths)
    with project_lock(paths):
        ensure_initialized(paths)
        state = derive_state(paths)
        ensure_draft(state, draft_id)
        refresh_materialized_state(paths)
    print(f"已重建 DRAFT 阅读文件：{draft_id}")
    return 0


def load_json_object(path_value: str, *, label: str) -> dict[str, object]:
    path = Path(path_value).expanduser()
    if not path.exists():
        raise SdlcError(f"{label}不存在：{path}", exit_code=1)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效的 UTF-8 JSON：{exc}", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}必须是 JSON 对象。", exit_code=1)
    return document


def run_source_index(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    with project_lock(paths):
        ensure_initialized(paths)
        DraftMutationService(paths, source="sdlc-draft").generate_source_index(draft_id)
    print(f"已生成 DRAFT 来源索引：{draft_id}/source-index.json")
    return 0


def run_facts(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    document = load_json_object(args.facts_file, label="模型事实文件")
    with project_lock(paths):
        ensure_initialized(paths)
        DraftMutationService(paths, source="sdlc-draft").write_fact_artifact(draft_id, args.kind, document)
    print(f"已写入 {args.kind} 模型事实：{draft_id}")
    return 0


def run_model_review(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    document = load_json_object(args.review_file, label="模型复核文件")
    with project_lock(paths):
        ensure_initialized(paths)
        DraftMutationService(paths, source="sdlc-draft").write_model_review(draft_id, document)
    print(f"已写入独立模型复核：{draft_id}")
    return 0


def run_review_request(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    with project_lock(paths):
        ensure_initialized(paths)
        _assessment, request_id = DraftMutationService(paths, source="sdlc-draft").create_review_request(draft_id)
    print(f"已创建独立复核请求：{request_id}")
    print("请由独立复核人员检查 facts，并提交 model-review.json。")
    return 0


def run_markdown_update(
    args: argparse.Namespace,
    *,
    label: str,
    event_type: str,
    content_key: str,
    summary_key: str,
    summary_prefix: str,
    output_label: str,
) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    draft_id = clean_text(args.draft_id).upper()
    content = load_markdown_content(args.content, args.content_file, label=label)

    with project_lock(paths):
        ensure_initialized(paths)
        service = DraftMutationService(paths, source="sdlc-draft")
        summary = draft_title_from_content(summary_prefix, content)
        if content_key == "requirement_body":
            service.update_requirement(draft_id, content, summary=summary)
        else:
            service.update_design(draft_id, content, summary=summary)

    print(f"{output_label}：{draft_id}")
    return 0
