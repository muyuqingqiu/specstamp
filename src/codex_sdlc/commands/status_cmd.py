from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, resolve_project_root
from codex_sdlc.core.state import derive_state, render_status_text


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("status", help="查看当前项目状态")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    state = derive_state(paths)
    print(render_status_text(paths, state), end="")
    return 0
