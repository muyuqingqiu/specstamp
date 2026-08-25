from __future__ import annotations

import argparse

from codex_sdlc.services import start_service


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("start", help="正式建档入口")
    parser.add_argument("description", nargs="?", default="", help=argparse.SUPPRESS)
    parser.add_argument("--file", dest="package_file", default="", help="带显式 source_draft_id 的 document-first.v1 formal.v3 正式包")
    # 旧参数只用于给已有调用返回明确迁移动作，服务层不会让它们进入文档优先流程。
    parser.add_argument("--draft", dest="draft_id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--source-index", default="", help=argparse.SUPPRESS)
    parser.add_argument("--requirement-facts", default="", help=argparse.SUPPRESS)
    parser.add_argument("--design-facts", default="", help=argparse.SUPPRESS)
    parser.add_argument("--model-review", default="", help=argparse.SUPPRESS)
    parser.set_defaults(func=run_formal_start, _sdlc_start_mode="formal")

    light_parser = subparsers.add_parser("light-start", help="已下线：保留命令识别，只输出下线提示")
    light_parser.add_argument("description", help="需求内容")
    light_parser.set_defaults(func=run_light_start, _sdlc_start_mode="light")


def run_formal_start(args: argparse.Namespace) -> int:
    return start_service.start(args)


def run_light_start(args: argparse.Namespace) -> int:
    return start_service.start(args)


run = run_light_start
