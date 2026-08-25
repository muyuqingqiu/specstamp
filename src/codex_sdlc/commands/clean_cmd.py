from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from codex_sdlc.core.codex_assets import (
    HOOKS_JSON_PATH,
    HOOK_SCRIPT_DIR,
    RULES_FILE_PATH,
    render_hooks_json,
    render_rules_file,
    render_session_start_hook,
    render_stop_hook,
    render_user_prompt_hook,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import resolve_project_root


@dataclass(frozen=True)
class CleanTarget:
    path: Path
    label: str
    kind: str
    removable: bool = True
    reason: str = ""


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    preview_parser = subparsers.add_parser("clean", help="预览会清理哪些本机 SDLC 产物")
    preview_parser.set_defaults(func=run_preview)

    confirm_parser = subparsers.add_parser("clean-confirm", help="确认清理本机 SDLC 产物")
    confirm_parser.set_defaults(func=run_confirm)


def generated_codex_files(root: Path) -> dict[Path, str]:
    return {
        root / HOOKS_JSON_PATH: render_hooks_json(),
        root / HOOK_SCRIPT_DIR / "sdlc_session_start.py": render_session_start_hook(),
        root / HOOK_SCRIPT_DIR / "sdlc_user_prompt_submit.py": render_user_prompt_hook(),
        root / HOOK_SCRIPT_DIR / "sdlc_stop.py": render_stop_hook(),
        root / RULES_FILE_PATH: render_rules_file(),
    }


def is_generated_file(path: Path, expected_text: str) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        return path.read_text(encoding="utf-8") == expected_text
    except OSError:
        return False


def collect_targets(root: Path) -> list[CleanTarget]:
    targets: list[CleanTarget] = []
    for relative_path, label in [
        (Path(".codex-sdlc"), "SDLC 状态、需求、任务、验证和交接记录"),
    ]:
        path = root / relative_path
        if path.exists():
            targets.append(CleanTarget(path=path, label=label, kind="dir"))

    for path, expected_text in generated_codex_files(root).items():
        if not path.exists():
            continue
        if is_generated_file(path, expected_text):
            targets.append(CleanTarget(path=path, label="SDLC 项目级 Codex 配置", kind="file"))
        else:
            targets.append(
                CleanTarget(
                    path=path,
                    label="项目级 Codex 配置",
                    kind="file",
                    removable=False,
                    reason="文件内容不是当前 SDLC 自动生成内容，已保留",
                )
            )
    return targets


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def print_targets(root: Path, targets: list[CleanTarget]) -> None:
    removable = [target for target in targets if target.removable]
    preserved = [target for target in targets if not target.removable]
    if removable:
        print("将清理：")
        for target in removable:
            print(f"- {relative_path(root, target.path)}：{target.label}")
    else:
        print("没有发现可清理的 SDLC 本机产物。")

    if preserved:
        print("将保留：")
        for target in preserved:
            print(f"- {relative_path(root, target.path)}：{target.reason}")


def remove_empty_parent_dirs(root: Path, directories: list[Path]) -> None:
    for directory in sorted(set(directories), key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current != root and current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def remove_targets(root: Path, targets: list[CleanTarget]) -> list[str]:
    removed: list[str] = []
    parent_dirs: list[Path] = []
    for target in targets:
        if not target.removable or not target.path.exists():
            continue
        # 这里按收集到的白名单删除，避免把用户自己的 .codex 或 .agents 内容一起清掉。
        if target.path.is_dir():
            shutil.rmtree(target.path)
        else:
            target.path.unlink()
            parent_dirs.append(target.path.parent)
        removed.append(relative_path(root, target.path))

    parent_dirs.extend([root / ".agents" / "skills", root / ".agents", root / ".codex" / "hooks", root / ".codex" / "rules", root / ".codex"])
    remove_empty_parent_dirs(root, parent_dirs)
    return removed


def resolve_clean_root() -> Path:
    try:
        return resolve_project_root(Path.cwd())
    except SdlcError as exc:
        raise SdlcError(
            exc.message + " 清理命令只会处理当前项目目录里的本机产物。",
            exit_code=exc.exit_code,
        ) from exc


def run_preview(_args: argparse.Namespace) -> int:
    root = resolve_clean_root()
    targets = collect_targets(root)
    print(f"清理预览：{root}")
    print_targets(root, targets)
    print("确认清理请使用：$sdlc-clean-confirm")
    print("说明：不会修改 Git 全局忽略配置。")
    return 0


def run_confirm(_args: argparse.Namespace) -> int:
    root = resolve_clean_root()
    targets = collect_targets(root)
    print(f"确认清理：{root}")
    print_targets(root, targets)
    removed = remove_targets(root, targets)
    if removed:
        print("已清理：")
        for item in removed:
            print(f"- {item}")
    else:
        print("没有实际清理任何文件。")
    print("Git 全局忽略配置已保留。")
    return 0
