from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_sdlc.core.git_tools import find_git_root, run_git

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


METADATA_FILE = "sdlc-codegraph.json"
CGCX_METADATA_FILE = "cgcx-state.json"
MAX_CODE_SEARCH_TERMS = 14
MAX_TASK_PACK_CONTEXT_FILES = 20
MAX_CODE_SEARCH_FILES_PER_TERM = 5
DEFAULT_BRIEF_TIMEOUT_SECONDS = 20
DEFAULT_DOCTOR_TIMEOUT_SECONDS = 120
CODE_SEARCH_EXTENSIONS = {
    ".arkts",
    ".css",
    ".ets",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".json5",
    ".jsx",
    ".kt",
    ".less",
    ".py",
    ".rs",
    ".scss",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
RG_EXCLUDE_GLOBS = [
    "!.git/**",
    "!.codex-sdlc/**",
    "!.codegraphcontext/**",
    "!node_modules/**",
    "!oh_modules/**",
    "!build/**",
    "!dist/**",
]


@dataclass(frozen=True)
class CodeGraphCommand:
    parts: list[str]
    source: str
    adapter: str = "codegraphcontext"


def clean_items(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = item.strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def project_has_codegraph_marker(root: Path) -> bool:
    return (root / ".codegraphcontext").exists() or (root / ".cgcignore").exists()


def metadata_path(root: Path) -> Path:
    return root / ".codegraphcontext" / METADATA_FILE


def cgcx_metadata_path(root: Path) -> Path:
    return root / ".codegraphcontext" / CGCX_METADATA_FILE


def metadata_candidates(root: Path) -> list[Path]:
    return [cgcx_metadata_path(root), metadata_path(root)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_codegraph_metadata(root: Path) -> tuple[dict[str, Any], Path | None]:
    for path in metadata_candidates(root):
        data = read_json(path)
        if data:
            return data, path
    return {}, None


def metadata_git_value(metadata: dict[str, Any], key: str) -> str:
    git_data = metadata.get("git") if isinstance(metadata.get("git"), dict) else {}
    if key == "git_root":
        return str(metadata.get("project_root") or git_data.get("git_root") or "")
    return str(metadata.get(key) or git_data.get(key) or "")


def same_head(recorded_head: str, current_head: str) -> bool:
    if not recorded_head or not current_head:
        return True
    return recorded_head.startswith(current_head) or current_head.startswith(recorded_head)


def current_git_context(root: Path) -> dict[str, str]:
    git_root = find_git_root(root)
    if git_root is None:
        return {"branch": "", "head": "", "git_root": ""}
    branch_result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], git_root)
    head_result = run_git(["rev-parse", "--short", "HEAD"], git_root)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    return {
        "branch": "" if branch == "HEAD" else branch,
        "head": head_result.stdout.strip() if head_result.returncode == 0 else "",
        "git_root": str(git_root.resolve()),
    }


def cgcignore_mtime(root: Path) -> float:
    candidates = [root / ".cgcignore", root / ".codegraphcontext" / ".cgcignore"]
    mtimes = [path.stat().st_mtime for path in candidates if path.exists()]
    return max(mtimes) if mtimes else 0.0


def local_db_exists(root: Path) -> bool:
    db_dir = root / ".codegraphcontext" / "db"
    if not db_dir.exists():
        return False
    return any(path.exists() for path in [db_dir / "kuzudb", db_dir / "falkordb"])


def codegraph_timeout(env_name: str, default_value: int) -> int:
    raw_value = os.environ.get(env_name, "").strip() or os.environ.get("CODEX_SDLC_CODEGRAPH_TIMEOUT", "").strip()
    if not raw_value:
        return default_value
    try:
        return max(1, int(raw_value))
    except ValueError:
        return default_value


def brief_timeout() -> int:
    return codegraph_timeout("CODEX_SDLC_CODEGRAPH_BRIEF_TIMEOUT", DEFAULT_BRIEF_TIMEOUT_SECONDS)


def doctor_timeout() -> int:
    return codegraph_timeout("CODEX_SDLC_CODEGRAPH_DOCTOR_TIMEOUT", DEFAULT_DOCTOR_TIMEOUT_SECONDS)


def command_exists(command: str) -> bool:
    path = Path(command).expanduser()
    if path.is_absolute() or "/" in command:
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(command) is not None


def command_from_codex_config() -> CodeGraphCommand | None:
    if tomllib is None:
        return None
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return None
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    server = ((data.get("mcp_servers") or {}).get("codegraphcontext") or {})
    command = str(server.get("command") or "").strip()
    args = [str(item) for item in server.get("args") or []]
    if command and command_exists(command):
        return command_from_parts([command, *args], "codex-config")
    return None


def is_cgcx_command(parts: list[str]) -> bool:
    if not parts:
        return False
    return Path(parts[0]).name == "cgcx"


def command_from_parts(parts: list[str], source: str) -> CodeGraphCommand:
    return CodeGraphCommand(parts, source, "cgcx" if is_cgcx_command(parts) else "codegraphcontext")


def discover_command() -> CodeGraphCommand | None:
    cgcx_env_command = os.environ.get("CODEX_SDLC_CGCX_CMD", "").strip()
    if cgcx_env_command:
        parts = shlex.split(cgcx_env_command)
        if parts and command_exists(parts[0]):
            return CodeGraphCommand(parts, "env-cgcx", "cgcx")

    env_command = os.environ.get("CODEX_SDLC_CODEGRAPH_CMD", "").strip()
    if env_command:
        parts = shlex.split(env_command)
        if parts and command_exists(parts[0]):
            return command_from_parts(parts, "env")

    cgcx = shutil.which("cgcx")
    if cgcx:
        return CodeGraphCommand([cgcx], "cgcx", "cgcx")

    config_command = command_from_codex_config()
    if config_command:
        return config_command

    direct = shutil.which("codegraphcontext")
    if direct:
        return CodeGraphCommand([direct], "path")
    uvx = shutil.which("uvx")
    if uvx:
        return CodeGraphCommand([uvx, "--with", "kuzu", "codegraphcontext"], "uvx")
    return None


def command_for_cli(command: CodeGraphCommand, extra_args: list[str]) -> list[str]:
    if command.adapter == "cgcx":
        if extra_args == ["index"]:
            return [*command.parts, "refresh"]
        if len(extra_args) >= 3 and extra_args[0] == "find" and extra_args[1] in {"pattern", "content", "name"}:
            return [*command.parts, "find", f"--{extra_args[1]}", *extra_args[2:]]
        return [*command.parts, *extra_args]
    parts = list(command.parts)
    if parts[-2:] == ["mcp", "start"]:
        parts = parts[:-2]
    return [*parts, *extra_args]


def run_codegraph(
    command: CodeGraphCommand,
    root: Path,
    args: list[str],
    *,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command_for_cli(command, args),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def compact_message(text: str, limit: int = 240) -> str:
    return " ".join(text.strip().split())[:limit]


def command_output(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(item for item in [result.stderr, result.stdout] if item)


def tool_error_message(output: str) -> str:
    return ""


def locked_database_reason(root: Path) -> str:
    lsof = shutil.which("lsof")
    if not lsof:
        return ""
    db_dir = root / ".codegraphcontext" / "db"
    candidates = [db_dir / "kuzudb", db_dir / "falkordb"]
    for db_path in candidates:
        if not db_path.exists():
            continue
        try:
            result = subprocess.run(
                [lsof, str(db_path)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) > 1:
            holder = " ".join(lines[1].split()[:3])
            return f"CodeGraphContext 数据库正在被其他进程使用：{holder}"
    return ""


def index_freshness(root: Path) -> tuple[str, str]:
    if not project_has_codegraph_marker(root):
        return "missing", "当前项目没有 CodeGraphContext 标记"
    if not local_db_exists(root):
        return "stale", "索引库不存在"

    metadata, _path = read_codegraph_metadata(root)
    git_context = current_git_context(root)
    if not metadata:
        return "stale", "缺少 cgcx/SDLC 索引元数据"
    if metadata_git_value(metadata, "git_root") != git_context.get("git_root"):
        return "stale", "索引项目路径和当前项目不一致"
    if metadata_git_value(metadata, "branch") != git_context.get("branch"):
        return "stale", "索引分支和当前分支不一致"
    if not same_head(metadata_git_value(metadata, "head"), git_context.get("head", "")):
        return "soft", "索引提交和当前提交不一致，仍可作为代码线索，最终以真实源码为准"
    if float(metadata.get("cgcignore_mtime") or 0) < cgcignore_mtime(root):
        return "stale", ".cgcignore 比索引记录更新"
    return "fresh", ""


def stale_reason(root: Path) -> str:
    level, reason = index_freshness(root)
    return reason if level == "stale" else ""


def index_status_text(root: Path) -> str:
    level, reason = index_freshness(root)
    if level == "stale":
        return f"已过期：{reason}"
    if level == "soft":
        return f"可用但可能偏旧：{reason}"
    if level == "missing":
        return "当前项目没有图谱标记"
    return "可用"


def refresh_index(command: CodeGraphCommand, root: Path, reason: str, *, timeout_seconds: int) -> tuple[bool, str]:
    try:
        result = run_codegraph(command, root, ["index"], timeout_seconds=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"刷新失败或超时：{exc}"
    tool_error = tool_error_message(command_output(result))
    if tool_error:
        return False, f"刷新失败：{tool_error}"
    if result.returncode != 0:
        message = compact_message(command_output(result))
        return False, "刷新失败" + (f"：{message}" if message else "")

    if command.adapter == "cgcx":
        return True, "已通过 cgcx 刷新索引"

    write_json(
        metadata_path(root),
        {
            "schema": "codex-sdlc.codegraph.v1",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "refresh_reason": reason,
            "git": current_git_context(root),
            "cgcignore_mtime": cgcignore_mtime(root),
        },
    )
    return True, "已刷新索引"


def query_terms(requirement: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """查询词只读取模型写入任务卡的显式字段。"""

    values: list[str] = []
    for key in ("query_terms", "symbols", "output_symbols"):
        raw = task.get(key, [])
        values.extend(str(item).strip() for item in raw if str(item).strip())
    return clean_items(values)[:MAX_CODE_SEARCH_TERMS]


def parse_files_from_output(root: Path, output: str) -> list[str]:
    escaped_root = re.escape(str(root.resolve()))
    pattern = re.compile(escaped_root + r"/([^\s:│┃]+):\d+")
    return clean_items(match.group(1) for match in pattern.finditer(output))


def parse_rg_files(output: str) -> list[str]:
    files: list[str] = []
    for line in output.splitlines():
        match = re.match(r"^([^:\n]+):\d+:", line)
        if match and Path(match.group(1)).suffix.lower() in CODE_SEARCH_EXTENSIONS:
            files.append(match.group(1))
    return clean_items(files)


def fallback_terms(terms: list[str]) -> list[str]:
    return clean_items(terms)


def code_search_fallback_files(
    root: Path,
    terms: list[str],
    *,
    max_files: int = MAX_TASK_PACK_CONTEXT_FILES,
    max_files_per_term: int = MAX_CODE_SEARCH_FILES_PER_TERM,
    timeout_seconds: int = 4,
) -> tuple[list[str], list[str], str]:
    rg = shutil.which("rg")
    if not rg:
        return [], [], "未找到 rg，无法做代码搜索兜底"
    files: list[str] = []
    executed_queries: list[str] = []
    for term in fallback_terms(terms):
        args = [
            rg,
            "-n",
            "--fixed-strings",
            "--no-heading",
            *[f"--glob={item}" for item in RG_EXCLUDE_GLOBS],
            "--",
            term,
        ]
        executed_queries.append(f"rg {term}")
        try:
            result = subprocess.run(
                args,
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return clean_items(files), executed_queries, f"代码搜索兜底失败或超时：{exc}"
        if result.returncode == 0:
            files.extend(parse_rg_files(result.stdout)[:max_files_per_term])
        elif result.returncode > 1:
            return clean_items(files), executed_queries, f"代码搜索兜底失败：{compact_message(result.stderr or result.stdout)}"
        if len(clean_items(files)) >= max_files:
            break
    return clean_items(files)[:max_files], executed_queries, ""


def append_fallback_metadata(
    metadata: dict[str, Any],
    root: Path,
    terms: list[str],
    reason_prefix: str,
) -> list[str]:
    fallback_files, fallback_queries, fallback_error = code_search_fallback_files(root, terms)
    metadata["fallback_tool"] = "rg"
    metadata["fallback_queries"] = fallback_queries
    metadata["fallback_matched_files"] = fallback_files
    if fallback_error:
        metadata["fallback_reason"] = fallback_error
    if fallback_files:
        metadata["matched_files"] = clean_items([*metadata.get("matched_files", []), *fallback_files])
        metadata["reason"] = f"{reason_prefix}；代码搜索兜底命中 {len(fallback_files)} 个建议文件"
    elif fallback_error:
        metadata["reason"] = f"{reason_prefix}；{fallback_error}"
    else:
        metadata["reason"] = f"{reason_prefix}；代码搜索兜底命中 0 个建议文件"
    return fallback_files


def query_context_files(
    command: CodeGraphCommand,
    root: Path,
    terms: list[str],
    *,
    timeout_seconds: int,
) -> tuple[list[str], list[str], str]:
    files: list[str] = []
    executed_queries: list[str] = []
    for term in terms:
        for query_type in ["pattern", "content"]:
            if len(files) >= MAX_TASK_PACK_CONTEXT_FILES:
                return clean_items(files)[:MAX_TASK_PACK_CONTEXT_FILES], executed_queries, ""
            args = ["find", query_type, term]
            executed_queries.append(" ".join(args))
            try:
                result = run_codegraph(command, root, args, timeout_seconds=timeout_seconds)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return clean_items(files), executed_queries, f"查询失败或超时：{exc}"
            tool_error = tool_error_message(command_output(result))
            if tool_error:
                return clean_items(files), executed_queries, f"查询失败：{tool_error}"
            if result.returncode != 0:
                message = compact_message(command_output(result))
                return clean_items(files), executed_queries, "查询失败" + (f"：{message}" if message else "")
            files.extend(parse_files_from_output(root, result.stdout))
    return clean_items(files)[:MAX_TASK_PACK_CONTEXT_FILES], executed_queries, ""


def enhance_context_files(
    root: Path,
    requirement: dict[str, Any],
    task: dict[str, Any],
    existing_files: list[str],
    *,
    refresh_mode: str = "ask",
) -> tuple[list[str], dict[str, Any]]:
    metadata: dict[str, Any] = {
        "status": "skipped",
        "used": False,
        "refreshed": False,
        "reason": "",
        "command_source": "",
        "queries": [],
        "matched_files": [],
    }
    if os.environ.get("CODEX_SDLC_CODEGRAPH", "").strip().lower() in {"0", "false", "off"}:
        metadata["reason"] = "已通过环境变量关闭"
        return existing_files, metadata
    if not project_has_codegraph_marker(root):
        metadata["reason"] = "当前项目没有 CodeGraphContext 标记"
        return existing_files, metadata

    command = discover_command()
    if command is None:
        metadata["status"] = "unavailable"
        metadata["display_status"] = "不可用"
        metadata["capability_status"] = "没有找到 cgcx 或 codegraphcontext"
        metadata["reason"] = "未找到 cgcx 或 codegraphcontext 命令"
        return existing_files, metadata
    metadata["command_source"] = command.source
    metadata["capability_status"] = "cgcx 可用" if command.adapter == "cgcx" else "CodeGraphContext 可用"

    metadata["index_status"] = index_status_text(root)
    level, reason = index_freshness(root)
    timeout_seconds = brief_timeout()
    if level == "soft":
        metadata["index_hint"] = reason
    if level == "stale" and reason:
        metadata["refresh_reason"] = reason
        if refresh_mode == "skip":
            metadata["status"] = "skipped"
            metadata["reason"] = f"索引明显过期，已按选择跳过：{reason}"
            return existing_files, metadata
        if refresh_mode != "refresh":
            metadata["status"] = "needs_choice"
            metadata["reason"] = f"CodeGraphContext 索引明显过期：{reason}"
            return existing_files, metadata
        refreshed, message = refresh_index(command, root, reason, timeout_seconds=doctor_timeout())
        metadata["refreshed"] = refreshed
        metadata["refresh_message"] = message
        if not refreshed:
            metadata["status"] = "degraded"
            metadata["reason"] = message
            return existing_files, metadata

    terms = query_terms(requirement, task)
    if not terms:
        metadata["status"] = "ready"
        metadata["used"] = True
        metadata["reason"] = "没有提取到适合图谱查询的关键词"
        return existing_files, metadata

    lock_reason = locked_database_reason(root)
    if lock_reason:
        metadata["status"] = "degraded"
        fallback_files = append_fallback_metadata(metadata, root, terms, lock_reason)
        return clean_items([*existing_files, *fallback_files]), metadata

    matched_files, queries, error = query_context_files(command, root, terms, timeout_seconds=timeout_seconds)
    metadata["queries"] = queries
    metadata["matched_files"] = matched_files
    metadata["used"] = True
    if error:
        metadata["status"] = "degraded"
        fallback_files = append_fallback_metadata(metadata, root, terms, error)
        return clean_items([*existing_files, *fallback_files]), metadata

    metadata["status"] = "ready"
    if not matched_files:
        fallback_files = append_fallback_metadata(metadata, root, terms, "CodeGraphContext 命中 0 个建议文件")
        return clean_items([*existing_files, *fallback_files]), metadata
    metadata["reason"] = f"命中 {len(matched_files)} 个建议文件"
    return clean_items([*existing_files, *matched_files]), metadata


def doctor_check(root: Path, *, auto_refresh: bool = False) -> tuple[list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []
    if not project_has_codegraph_marker(root):
        warnings.append("CodeGraphContext 未启用；没有发现 `.codegraphcontext/` 或 `.cgcignore`")
        return passed, warnings

    command = discover_command()
    if command is None:
        warnings.append("CodeGraphContext 已有项目标记，但本机没有找到可用命令")
        return passed, warnings

    level, reason = index_freshness(root)
    if level == "soft":
        passed.append(f"CodeGraphContext 索引可用但可能偏旧：{reason}")
        return passed, warnings
    if level == "stale" and reason:
        if auto_refresh:
            refreshed, message = refresh_index(command, root, reason, timeout_seconds=doctor_timeout())
            if refreshed:
                passed.append(f"CodeGraphContext 索引已刷新：{reason}")
            else:
                warnings.append(f"CodeGraphContext 刷新失败，后续会降级：{message}")
            return passed, warnings
        warnings.append(f"CodeGraphContext 索引需要刷新：{reason}")
        return passed, warnings

    passed.append(f"CodeGraphContext 索引可用（命令来源：{command.source}）")
    return passed, warnings
