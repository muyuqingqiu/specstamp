from __future__ import annotations

import argparse
from pathlib import Path

from codex_sdlc.core.codex_assets import install_project_codex_assets
from codex_sdlc.core.project import resolve_project_root


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("hooks-upgrade", help="只升级项目级 Codex Hook 和安全规则，不读写 SDLC 状态")
    parser.set_defaults(func=run_hooks_upgrade)


def run_hooks_upgrade(_args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    install_project_codex_assets(root)
    print("已升级项目级 Codex Hook 和安全规则")
    print(f"- 项目：{root}")
    print("- 范围：只更新 `.codex/hooks` 和 `.codex/rules` 中由 SDLC 自动生成的文件。")
    print("- 边界：不读写 `.codex-sdlc/`，不创建需求，不拆任务，不生成交接。")
    print("下一步建议：普通开发问答可以直接继续；需要 SDLC 流程时再使用 `$sdlc-status`。")
    return 0
