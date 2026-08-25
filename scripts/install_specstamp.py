#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Sequence
import zipfile


SUBPROCESS_TIMEOUT = 120
PROJECT_NAME = "specstamp"
CLI_ENTRIES = {
    "specstamp": "specstamp",
    "codex-sdlc": "codex-sdlc",
}


def clean_python_environment(source: dict[str, str], *, repo_src: Path | None = None) -> dict[str, str]:
    """子进程不继承用户注入的 Python 路径，预览只允许读取当前仓库源码。"""

    env = source.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    if repo_src is not None:
        env["PYTHONPATH"] = str(repo_src)
    return env


def _project_version(repo_root: Path) -> str:
    """从当前仓库源码读取版本，避免被外部 PYTHONPATH 中的同名包替换。"""

    init_file = repo_root / "src" / "codex_sdlc" / "__init__.py"
    try:
        tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"无法读取当前项目版本：{init_file}（{exc}）") from exc
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise ValueError(f"当前项目没有可用的 __version__：{init_file}")


def select_project_wheel(wheelhouse: Path, repo_root: Path) -> Path:
    """在任何安装写入前锁定并核对唯一的当前项目 wheel。"""

    candidates = sorted(
        path
        for path in wheelhouse.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".whl"
        and re.sub(r"[-_.]+", "_", path.name.split("-", 1)[0]).lower() == "specstamp"
    )
    if len(candidates) != 1:
        raise ValueError(
            f"wheelhouse 中 specstamp 项目 wheel 必须恰好有 1 个，实际为 {len(candidates)}：{wheelhouse.absolute()}"
        )

    wheel = candidates[0].absolute()
    expected_version = _project_version(repo_root)
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise ValueError("项目 wheel 必须恰好包含一份 METADATA")
            metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
            name = metadata.get("Name", "")
            version = metadata.get("Version", "")
            normalized_name = re.sub(r"[-_.]+", "-", name).lower()
            if normalized_name != PROJECT_NAME:
                raise ValueError(f"项目 wheel 的 Name 必须为 {PROJECT_NAME}，实际为 {name or '空'}")
            if version != expected_version:
                raise ValueError(f"项目 wheel 版本必须为 {expected_version}，实际为 {version or '空'}")
            entry_points = [item for item in names if item.endswith(".dist-info/entry_points.txt")]
            if len(entry_points) != 1:
                raise ValueError("项目 wheel 必须恰好包含一份命令入口声明")
            entry_point_text = archive.read(entry_points[0]).decode("utf-8")
            expected_entries = {
                "specstamp = codex_sdlc.cli:main",
                "codex-sdlc = codex_sdlc.cli:main",
            }
            missing_entries = sorted(item for item in expected_entries if item not in entry_point_text)
            if missing_entries:
                raise ValueError("项目 wheel 缺少命令入口：" + "、".join(missing_entries))
            if not any(item.startswith("codex_sdlc/schemas/") and item.endswith(".json") for item in names):
                raise ValueError("项目 wheel 缺少 codex_sdlc.schemas 的 Schema 包数据")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"项目 wheel 无法读取：{wheel}（{exc}）") from exc
    return wheel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="创建或更新 SpecStamp 的仓库受管运行环境。",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="用于创建受管环境的 Python，默认使用运行安装脚本的解释器。",
    )
    parser.add_argument(
        "--with-dev",
        action="store_true",
        help="同时安装 pytest 等开发测试依赖。",
    )
    parser.add_argument(
        "--dry-run-agent-sync",
        action="store_true",
        help="仅预览 Agent 同步计划，不写入任何全局目录。",
    )
    parser.add_argument(
        "--confirm-agent-sync",
        action="store_true",
        help="显式授权同步三套全局 Agent 入口（Codex、通用 Agent、Claude）。",
    )
    parser.add_argument(
        "--wheelhouse",
        help="使用本地 wheelhouse 离线安装当前 specstamp 包及其依赖。",
    )
    return parser


def resolve_python(value: str) -> str | None:
    """解释器既可以写绝对路径，也可以写当前 PATH 中的命令名。"""

    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(value)


def print_command_failure(
    label: str,
    command: Sequence[str],
    result: subprocess.CompletedProcess[str],
) -> None:
    """失败时保留可复现命令和真实退出码，但不把成功过程刷满终端。"""

    print(f"错误：{label}失败，退出码为 {result.returncode}。", file=sys.stderr)
    print("执行命令：" + " ".join(command), file=sys.stderr)
    if result.stdout.strip():
        print(result.stdout.rstrip(), file=sys.stderr)
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)


def run_checked(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    label: str,
) -> bool:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except OSError as exc:
        print(f"错误：{label}无法启动：{exc}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print(f"错误：{label}超过 {SUBPROCESS_TIMEOUT} 秒仍未完成。", file=sys.stderr)
        return False
    if result.returncode != 0:
        print_command_failure(label, command, result)
        return False
    print(f"- {label}：通过")
    return True


def cli_link_plan(repo_root: Path) -> dict[Path, Path]:
    """两个命令共用一套实现，但分别指向同名启动器。"""

    bin_dir = Path.home() / ".local" / "bin"
    return {
        bin_dir / link_name: repo_root / "bin" / launcher_name
        for link_name, launcher_name in CLI_ENTRIES.items()
    }


def precheck_cli_links(plan: dict[Path, Path]) -> bool:
    """在创建 venv 前整体检查，避免第二个入口冲突时已经写入一半。"""

    for link, launcher in plan.items():
        if link.is_symlink():
            if link.resolve(strict=False) == launcher.resolve():
                continue
            print(f"错误：命令入口已经指向其它位置，未覆盖：{link}", file=sys.stderr)
            return False
        if link.exists():
            print(f"错误：命令入口已存在且不是符号链接，未覆盖：{link}", file=sys.stderr)
            return False
    return True


def rollback_cli_links(created_links: Sequence[Path]) -> None:
    """只删除本轮创建的链接，不碰原本已经存在的正确入口。"""

    for link in reversed(created_links):
        try:
            link.unlink(missing_ok=True)
        except OSError as exc:
            print(f"错误：回滚命令入口失败 {link}：{exc}", file=sys.stderr)


def ensure_cli_links(plan: dict[Path, Path]) -> tuple[dict[str, Path], list[Path]] | None:
    """一次建立两个命令入口，中途失败时回滚本轮已建链接。"""

    created_links: list[Path] = []
    links_by_name: dict[str, Path] = {}
    try:
        for link, launcher in plan.items():
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.is_symlink():
                link.symlink_to(launcher)
                created_links.append(link)
            links_by_name[link.name] = link
    except OSError as exc:
        print(f"错误：无法创建命令入口 {link}：{exc}", file=sys.stderr)
        rollback_cli_links(created_links)
        return None
    return links_by_name, created_links


def managed_environment_python(environment_dir: Path) -> Path:
    return environment_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def smoke_environment(repo_root: Path, cli_link: Path) -> dict[str, str]:
    """冒烟必须通过正式链接运行，并移除当前终端的环境激活影响。"""

    env = clean_python_environment(os.environ)
    env.pop("CODEX_SDLC_PYTHON", None)
    env.pop("VIRTUAL_ENV", None)
    env.pop("CONDA_PREFIX", None)
    env["CODEX_SDLC_HOME"] = str(repo_root)
    env["PATH"] = os.pathsep.join([str(cli_link.parent), env.get("PATH", "")])
    return env


def _run_preview(base_python: str, repo_root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """使用带包上下文的预览，真实调用同一计划，兼容干净 Python（无 jsonschema 仍可运行）"""
    preview_env = clean_python_environment(env, repo_src=repo_root / "src")
    preview_env["CODEX_SDLC_HOME"] = str(repo_root)
    # 使用 -c 直接导入包内预览入口，而非 spec_from_file_location 的无包上下文加载
    # 预览函数本身仅依赖标准库，因此在 /usr/bin/python3 缺少 jsonschema 时仍可运行
    code = (
        "from codex_sdlc.core.agent_sync import preview_agent_sync_stdlib; "
        "r=preview_agent_sync_stdlib(); "
        "print('Agent 同步预览（只读）：'); "
        "[print(f\"  {k}: {v}\") for k,v in r.items()]"
    )
    command = [base_python, "-c", code]
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            env=preview_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout="",
            stderr=f"同步预览超过 {SUBPROCESS_TIMEOUT} 秒仍未完成。",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=f"同步预览无法启动：{exc}",
        )


def install(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    environment_dir = repo_root / ".venv"
    launchers = {name: repo_root / "bin" / launcher for name, launcher in CLI_ENTRIES.items()}
    base_python = resolve_python(args.python)

    if args.dry_run_agent_sync and args.confirm_agent_sync:
        print("错误：--dry-run-agent-sync 与 --confirm-agent-sync 互斥。", file=sys.stderr)
        return 2
    if not args.dry_run_agent_sync and not args.confirm_agent_sync:
        print("错误：请显式选择安装模式。", file=sys.stderr)
        print("  只预览：python3 scripts/install_specstamp.py --dry-run-agent-sync", file=sys.stderr)
        print("  完整安装：python3 scripts/install_specstamp.py --confirm-agent-sync", file=sys.stderr)
        print("  完整安装（含开发依赖）：python3 scripts/install_specstamp.py --with-dev --confirm-agent-sync", file=sys.stderr)
        return 2
    if base_python is None:
        print(f"错误：创建受管环境的 Python 不可用：{args.python}", file=sys.stderr)
        return 1
    wheelhouse: Path | None = None
    project_wheel: Path | None = None
    if args.wheelhouse:
        wheelhouse = Path(args.wheelhouse).expanduser()
        if not wheelhouse.is_dir():
            print(f"错误：wheelhouse 不是可用目录：{wheelhouse}", file=sys.stderr)
            return 1
        try:
            project_wheel = select_project_wheel(wheelhouse, repo_root)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
    for launcher in launchers.values():
        if not launcher.is_file() or not os.access(launcher, os.X_OK):
            print(f"错误：正式启动器不存在或不可执行：{launcher}", file=sys.stderr)
            return 1

    print("SpecStamp 安装与升级")
    print(f"- 项目目录：{repo_root}")
    print(f"- 受管环境：{environment_dir}")

    # --dry-run 必须是只读路径：任何仓库 venv、HOME 链接或全局目录写入前完成，且不得提前导入完整 codex_sdlc.cli
    if args.dry_run_agent_sync:
        result = _run_preview(base_python, repo_root, os.environ.copy())
        print(result.stdout.rstrip())
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr)
        if result.returncode != 0:
            print(f"错误：同步预览失败，退出码 {result.returncode}", file=sys.stderr)
            return result.returncode
        print("预览完成（只读，未写入 venv/链接/全局目录）。")
        return 0

    link_plan = cli_link_plan(repo_root)
    if not precheck_cli_links(link_plan):
        return 1

    # --confirm 流程：先显示同一份 dry-run，再创建 venv 等
    print("- Agent 同步预览（确认前只读展示）：")
    preview_result = _run_preview(base_python, repo_root, os.environ.copy())
    print(preview_result.stdout.rstrip())
    if preview_result.stderr.strip():
        print(preview_result.stderr.rstrip(), file=sys.stderr)
    if preview_result.returncode != 0:
        print(f"错误：同步预览失败，退出码 {preview_result.returncode}", file=sys.stderr)
        return preview_result.returncode

    # 重复执行 venv 可以修复上次中断留下的基础目录，业务启动器仍会用导入探针拒绝半成品。
    if not run_checked(
        [base_python, "-m", "venv", str(environment_dir)],
        cwd=repo_root,
        env=clean_python_environment(os.environ),
        label="创建或修复受管环境",
    ):
        return 1

    environment_python = managed_environment_python(environment_dir)
    if wheelhouse is None:
        install_target = f"{repo_root}[dev]" if args.with_dev else str(repo_root)
    else:
        # 离线安装必须消费 wheelhouse 中的当前项目 wheel，避免 editable 安装再次触发构建依赖下载。
        if project_wheel is None:
            print("错误：离线安装没有锁定当前项目 wheel。", file=sys.stderr)
            return 1
        install_target = f"{project_wheel}[dev]" if args.with_dev else str(project_wheel)
    pip_command = [
        str(environment_python),
        "-m",
        "pip",
        "install",
        "--upgrade",
    ]
    if wheelhouse is not None:
        pip_command.extend(["--no-index", "--find-links", str(wheelhouse)])
    pip_command.extend([install_target])
    if not run_checked(
        pip_command,
        cwd=repo_root,
        env=clean_python_environment(os.environ),
        label="安装项目和依赖",
    ):
        return 1

    link_result = ensure_cli_links(link_plan)
    if link_result is None:
        return 1
    cli_links, created_links = link_result
    for name in CLI_ENTRIES:
        print(f"- 命令入口：{cli_links[name]}")

    cli_link = cli_links["specstamp"]
    smoke_env = smoke_environment(repo_root, cli_link)
    # 已获显式授权，执行同步写入
    if not run_checked(
        [str(cli_link), "agent-sync", "--confirm"],
        cwd=repo_root,
        env=smoke_env,
        label="Agent 入口同步",
    ):
        rollback_cli_links(created_links)
        return 1
    smoke_commands = [
        ("帮助命令", [str(cli_link), "--help"]),
        ("版本命令", [str(cli_link), "version"]),
        ("安装体检", [str(cli_link), "doctor-install"]),
        ("兼容命令版本", [str(cli_links["codex-sdlc"]), "version"]),
    ]
    for label, command in smoke_commands:
        if not run_checked(command, cwd=repo_root, env=smoke_env, label=label):
            rollback_cli_links(created_links)
            return 1

    if str(cli_link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"- 当前 PATH 尚未包含 {cli_link.parent}")
        print(f'- 请执行：export PATH="{cli_link.parent}:$PATH"')
    print("安装与升级完成。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return install(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
