from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.state import derive_state, render_handoff_text


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("handoff", help="输出可复制到新会话的交接提示词")
    parser.add_argument("--full", action="store_true", help="输出完整交接，包含全部任务和最近验证")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    state = derive_state(paths)
    print(render_handoff_text(paths, state, full=args.full), end="")
    return 0
