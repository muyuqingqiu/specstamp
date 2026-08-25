from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.codex_assets import install_project_codex_assets
from codex_sdlc.core.project import build_paths, ensure_base_dirs, project_lock, resolve_project_root
from codex_sdlc.core.state import create_project_initialized_event, refresh_materialized_state


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("init", help="初始化 SDLC 本机工作区")
    parser.add_argument("--plain-dir", action="store_true", help="兼容旧用法：允许在非 Git 目录初始化")
    parser.set_defaults(func=run)

    plain_parser = subparsers.add_parser("init-plain", help="在非 Git 目录初始化 SDLC 本机工作区")
    plain_parser.set_defaults(func=run_plain)

    basic_parser = subparsers.add_parser("init-basic", help="兼容旧命令：等同于 init，只初始化 SDLC")
    basic_parser.set_defaults(func=run_basic)

    basic_plain_parser = subparsers.add_parser("init-basic-plain", help="在非 Git 目录只初始化 SDLC")
    basic_plain_parser.set_defaults(func=run_basic_plain)


def run_init(*, plain_dir: bool) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=plain_dir)
    paths = build_paths(root)
    ensure_base_dirs(paths)
    install_project_codex_assets(root)

    with project_lock(paths):
        if not paths.events_file.exists() or not paths.events_file.read_text(encoding="utf-8").strip():
            create_project_initialized_event(paths)
        state = refresh_materialized_state(paths)

    print(f"已初始化：{paths.sdlc_dir}")
    print(f"项目类型：{state['project'].get('project_type', 'generic')}")
    print("下一步建议直接按这个顺序继续：")
    print("- `$sdlc-discuss 需求想法` 先讨论并记录需求草案")
    print("- 需求确认后，用 `$sdlc-design 技术方案草案` 先确认实现方案")
    print("- 技术方案确认且 DRAFT 进入 start_ready 后，用 `$sdlc-start`。小需求也走 DRAFT 主流程。")
    print("- `$sdlc-status` 查看当前状态")
    return 0


def run(args: argparse.Namespace) -> int:
    return run_init(plain_dir=args.plain_dir)


def run_plain(args: argparse.Namespace) -> int:
    return run_init(plain_dir=True)


def run_basic(_args: argparse.Namespace) -> int:
    return run_init(plain_dir=False)


def run_basic_plain(_args: argparse.Namespace) -> int:
    return run_init(plain_dir=True)
