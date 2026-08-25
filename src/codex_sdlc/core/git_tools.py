from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def find_git_root(cwd: Path) -> Path | None:
    result = run_git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def current_git_status_lines(cwd: Path) -> list[str]:
    root = find_git_root(cwd)
    if root is None:
        return []
    result = run_git(["status", "--short"], root)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def current_git_changed_files(cwd: Path) -> list[str]:
    files: list[str] = []
    for line in current_git_status_lines(cwd):
        if len(line) > 3:
            files.append(line[3:].strip())
    return files


def read_global_excludesfile() -> str | None:
    result = subprocess.run(
        ["git", "config", "--global", "--get", "core.excludesfile"],
        text=True,
        capture_output=True,
        check=False,
    )
    value = result.stdout.strip()
    return value or None


def resolve_global_excludesfile() -> Path | None:
    configured = read_global_excludesfile()
    if not configured:
        return None
    return Path(configured).expanduser()


def ensure_global_excludesfile() -> Path:
    configured = resolve_global_excludesfile()
    if configured is None:
        configured = Path.home() / ".gitignore_global"
        subprocess.run(
            ["git", "config", "--global", "core.excludesfile", str(configured)],
            text=True,
            capture_output=True,
            check=False,
        )
    configured.parent.mkdir(parents=True, exist_ok=True)
    if not configured.exists():
        configured.write_text("# SpecStamp 全局忽略\n", encoding="utf-8")
    return configured


def ensure_sdlc_global_ignore() -> dict[str, str]:
    excludesfile = ensure_global_excludesfile()
    existing_lines = excludesfile.read_text(encoding="utf-8").splitlines()
    normalized = {line.strip() for line in existing_lines if line.strip()}
    if ".codex-sdlc/" not in normalized and ".codex-sdlc" not in normalized:
        with excludesfile.open("a", encoding="utf-8") as handle:
            if existing_lines and existing_lines[-1].strip():
                handle.write("\n")
            handle.write(".codex-sdlc/\n")
    return {
        "git_ignore_file": str(excludesfile),
        "git_ignore_rule": ".codex-sdlc/",
    }


def git_check_ignore(cwd: Path, target: str) -> bool:
    root = find_git_root(cwd)
    if root is None:
        return False
    result = run_git(["check-ignore", target], root)
    return result.returncode == 0


def detect_project_commands(root: Path) -> tuple[list[str], list[str]]:
    scripts: list[str] = []
    test_commands: list[str] = []

    package_json = root / "package.json"
    if package_json.exists():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
            for name in sorted((package_data.get("scripts") or {}).keys()):
                scripts.append(f"npm run {name}")
            if "test" in (package_data.get("scripts") or {}):
                test_commands.append("npm test")
        except json.JSONDecodeError:
            pass

    if (root / "pyproject.toml").exists():
        scripts.append("python -m pytest")
        test_commands.append("pytest")
    elif (root / "requirements.txt").exists():
        test_commands.append("pytest")

    if (root / "go.mod").exists():
        scripts.append("go test ./...")
        test_commands.append("go test ./...")

    if (root / "Cargo.toml").exists():
        scripts.append("cargo test")
        test_commands.append("cargo test")

    return scripts, test_commands
