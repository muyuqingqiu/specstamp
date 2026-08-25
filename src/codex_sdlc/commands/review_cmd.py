from __future__ import annotations

import argparse
from pathlib import Path
import sys

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.structured_contract import canonical_json_text
from codex_sdlc.services import review_service


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("review", help="创建、提交和读取通用独立审核")
    children = parser.add_subparsers(dest="review_command", parser_class=type(parser))

    create = children.add_parser("create", help="从当前受控输入创建审核请求")
    create.add_argument(
        "--review-id",
        required=True,
        help="审核请求编号，例如 REV-001",
    )
    create.add_argument(
        "--stage",
        required=True,
        choices=("requirement_split", "integrated_design", "task_plan"),
        help="固定审核阶段",
    )
    create.add_argument("--owner", required=True, help="被审核对象编号，例如 DRAFT-001 或 REQ-001")
    create.add_argument(
        "--input",
        action="append",
        required=True,
        help="项目内受控输入文件，可重复传入",
    )
    create.add_argument(
        "--check",
        action="append",
        default=[],
        help="审核检查项，可重复传入",
    )
    create.set_defaults(func=run_create)

    submit = children.add_parser("submit", help="在独立任务中提交审核结果")
    submit.add_argument("--request", required=True, help="审核请求编号")
    submit.add_argument("--file", required=True, help="review-result.v1 JSON 文件")
    submit.set_defaults(func=run_submit)

    status = children.add_parser("status", help="只读显示当前有效、待处理或过期审核")
    status.add_argument("--review", default="", help="只查看一个审核请求编号")
    status.set_defaults(func=run_status)
    parser.set_defaults(func=run_default)


def _paths():
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    return build_paths(root)


def _print_json(value: object) -> None:
    print(canonical_json_text(value), end="")


def run_default(_args: argparse.Namespace) -> int:
    print("请指定 review 子命令：create / submit / status")
    return 0


def run_create(args: argparse.Namespace) -> int:
    result = review_service.create_review(
        _paths(),
        review_id=args.review_id,
        stage=args.stage,
        owner_id=args.owner,
        input_paths=args.input,
        required_checks=args.check,
    )
    _print_json(result)
    return 0


def run_submit(args: argparse.Namespace) -> int:
    result = review_service.submit_review(
        _paths(),
        request_id=args.request,
        submission_file=Path(args.file),
    )
    _print_json(result)
    return 0


def run_status(args: argparse.Namespace) -> int:
    _print_json(review_service.review_status(_paths(), review_id=args.review or None))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specstamp")
    register(parser.add_subparsers(dest="command"))
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if not hasattr(args, "func"):
            return run_default(args)
        return int(args.func(args))
    except SdlcError as exc:
        print(f"错误：{exc.message}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
