from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.project import resolve_project_root


def build_placeholder_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str, stage: str) -> None:
    parser = subparsers.add_parser(name, help=f"{name} 命令入口")
    parser.set_defaults(func=lambda args, command_name=name, release_stage=stage: run_placeholder(command_name, release_stage))
    parser.description = f"`{name}` 命令会在 {stage} 提供完整能力。"


def run_placeholder(command_name: str, release_stage: str) -> int:
    project_root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    print(f"`{command_name}` 入口已经预留好，但完整能力会在 {release_stage} 落地。")
    print(f"当前目录：{project_root}")
    print("现在先继续使用已经完成的 V1 命令。")
    return 0
