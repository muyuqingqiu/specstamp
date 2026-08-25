from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codex_sdlc.core import fact_artifacts, fact_review_trust, fact_schema
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("facts", help="确定性准备模型事实来源索引")
    children = parser.add_subparsers(dest="facts_command", parser_class=type(parser))
    source = children.add_parser("source-index", help="从 formal.v3 业务内容生成 source projection 和索引")
    source.add_argument("--file", required=True, help="不含 fact_bundle 的正式业务 JSON")
    source.add_argument("--output", required=True, help="source-index.json 输出路径")
    source.set_defaults(func=run_source_index)
    freeze = children.add_parser("freeze", help="记录当前任务生成的两份正式 facts")
    _fact_pair_arguments(freeze)
    freeze.set_defaults(func=run_freeze)
    request = children.add_parser("review-request", help="为已经冻结的正式 facts 创建一次性复核请求")
    _fact_pair_arguments(request)
    request.set_defaults(func=run_review_request)
    submit = children.add_parser("review-submit", help="在独立任务中提交正式 facts 复核结果")
    _fact_pair_arguments(submit)
    submit.add_argument("--review", required=True, help="model-review.json 路径")
    submit.add_argument("--request", required=True, help="一次性复核请求编号")
    submit.set_defaults(func=run_review_submit)
    parser.set_defaults(func=run_default)


def run_default(_args: argparse.Namespace) -> int:
    print("请指定 facts 子命令：source-index / freeze / review-request / review-submit")
    return 0


def _fact_pair_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--requirement-facts", required=True, help="requirement.facts.json 路径")
    parser.add_argument("--design-facts", required=True, help="design.facts.json 路径")
    parser.add_argument("--draft-id", default="FORMAL", help="来源 DRAFT 编号；无 DRAFT 时保持 FORMAL")


def _load_fact_pair(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    values = []
    for owner, raw in (("requirement", args.requirement_facts), ("design", args.design_facts)):
        try:
            value = json.loads(Path(raw).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SdlcError(f"{owner} facts 无法读取：{exc}", exit_code=1) from exc
        issues = fact_schema.fact_document_issues(value, owner=owner)
        if issues or value.get("artifact_sha256") != fact_artifacts.artifact_sha256(value):
            raise SdlcError(f"{owner} facts 未通过 schema 或 artifact hash 检查。", exit_code=1)
        values.append(value)
    requirement_ids = {
        str(item.get("fact_id"))
        for item in values[0].get("semantic", {}).get("facts", [])
        if isinstance(item, dict)
    }
    if fact_schema.fact_document_issues(values[1], owner="design", requirement_fact_ids=requirement_ids):
        raise SdlcError("design facts 的技术关系没有指向当前需求事实。", exit_code=1)
    return values[0], values[1]


def run_freeze(args: argparse.Namespace) -> int:
    requirement, design = _load_fact_pair(args)
    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    with project_lock(paths):
        fact_review_trust.record_fact_run(paths, draft_id=str(args.draft_id).strip().upper(), owner="requirement", artifact_sha256=fact_artifacts.artifact_sha256(requirement))
        fact_review_trust.record_fact_run(paths, draft_id=str(args.draft_id).strip().upper(), owner="design", artifact_sha256=fact_artifacts.artifact_sha256(design))
    print("已记录当前任务生成的两份正式 facts。")
    return 0


def run_review_request(args: argparse.Namespace) -> int:
    requirement, design = _load_fact_pair(args)
    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    target = fact_review_trust.review_target_sha256(requirement, design, requirement["context_targets"])
    with project_lock(paths):
        run_ids = fact_review_trust.matching_fact_runs(
            paths,
            requirement_sha256=fact_artifacts.artifact_sha256(requirement),
            design_sha256=fact_artifacts.artifact_sha256(design),
            draft_id=str(args.draft_id).strip().upper(),
        )
        request = fact_review_trust.create_review_request(paths, draft_id=str(args.draft_id).strip().upper(), target_sha256=target, fact_run_ids=run_ids, entry_scope="formal")
    print(f"已创建独立复核请求：{request['request_id']}")
    print("请在另一个 Codex 任务中提交复核结果。")
    return 0


def run_review_submit(args: argparse.Namespace) -> int:
    requirement, design = _load_fact_pair(args)
    try:
        review = json.loads(Path(args.review).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"model-review.json 无法读取：{exc}", exit_code=1) from exc
    if fact_schema.review_document_issues(review) or review.get("artifact_sha256") != fact_artifacts.artifact_sha256(review):
        raise SdlcError("model-review.json 未通过 schema 或 artifact hash 检查。", exit_code=1)
    target = fact_review_trust.review_target_sha256(requirement, design, review["targets"])
    paths = build_paths(resolve_project_root(Path.cwd(), allow_plain_directory=True))
    with project_lock(paths):
        receipt = fact_review_trust.submit_review(
            paths,
            request_id=str(args.request),
            target_sha256=target,
            review_sha256=fact_artifacts.artifact_sha256(review),
        )
    print(f"独立复核回执已登记：{receipt['receipt_id']}")
    return 0


def run_source_index(args: argparse.Namespace) -> int:
    source_file = Path(args.file).expanduser()
    output_file = Path(args.output).expanduser()
    try:
        business = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"正式业务 JSON 无法读取：{exc}", exit_code=1) from exc
    if not isinstance(business, dict):
        raise SdlcError("正式业务 JSON 必须是对象。", exit_code=1)
    try:
        projection = fact_artifacts.build_formal_source_projection(business)
    except ValueError as exc:
        raise SdlcError(str(exc), exit_code=1) from exc
    index = fact_artifacts.build_source_index(projection, source_kind="formal")
    fact_artifacts.write_json(output_file, index)
    fact_artifacts.write_json(output_file.with_name("source-projection.json"), projection)
    print(f"已生成来源索引：{output_file}")
    print(f"来源投影：{output_file.with_name('source-projection.json')}")
    return 0
