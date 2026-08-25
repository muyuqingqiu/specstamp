from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Iterable, Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)


CODE_EVIDENCE_SCHEMA = "code-evidence.v1"
EVIDENCE_GROUPS = ("rules", "dependencies", "code_files", "upstream_outputs")
EVIDENCE_PURPOSES = ("integrated_design", "task_planning")
REPOSITORY_PATH_PREFIX = "@repo/"
MISSING_TASK_OUTPUT_PREFIX = "@task-output/"
GIT_IDENTITY_CONTRACT = "git-worktree.v1"
FILESYSTEM_IDENTITY_CONTRACT = "filesystem-worktree.v1"


def _run_git(root: Path, args: list[str], *, binary: bool = False):
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=not binary,
            check=False,
        )
    except OSError as exc:
        raise SdlcError(
            "Git 仓库身份校验失败，请确认当前目录属于可用的 Git 工作树后重试。",
            exit_code=1,
        ) from exc
    if completed.returncode != 0:
        raise SdlcError(
            "Git 仓库身份校验失败，请确认当前目录属于可用的 Git 工作树后重试。",
            exit_code=1,
        )
    return completed.stdout


def _git_path(root: Path, argument: str) -> Path:
    raw = str(_run_git(root, ["rev-parse", argument])).strip()
    path = Path(raw)
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def repository_identity(paths: ProjectPaths) -> dict[str, str]:
    """身份由真实 Git 元数据生成，调用方不能用自报字段替换当前工作树。"""

    git_root = _git_path(paths.root, "--show-toplevel")
    common_dir = _git_path(paths.root, "--git-common-dir")
    git_dir = _git_path(paths.root, "--git-dir")
    try:
        paths.root.resolve().relative_to(git_root)
    except ValueError as exc:
        raise SdlcError("SDLC 项目目录不在当前 Git 工作树内。", exit_code=1) from exc

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=paths.root,
        capture_output=True,
        text=True,
        check=False,
    )
    git_head = head.stdout.strip() if head.returncode == 0 else "UNBORN"
    dirty = _run_git(
        paths.root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."],
        binary=True,
    )
    if not isinstance(dirty, bytes):
        raise SdlcError("Git 脏文件范围读取结果无效。", exit_code=1)
    return {
        "identity_contract": GIT_IDENTITY_CONTRACT,
        "repo_key": canonical_sha256({"git_common_dir": str(common_dir)}),
        "worktree_key": canonical_sha256(
            {"git_dir": str(git_dir), "project_root": str(paths.root.resolve())}
        ),
        "git_head": git_head,
        "dirty_paths_sha256": hashlib.sha256(dirty).hexdigest(),
    }


def _has_git_marker(root: Path) -> bool:
    current = root.resolve()
    for directory in (current, *current.parents):
        marker = directory / ".git"
        if marker.exists() or marker.is_symlink():
            return True
    return False


def _raise_walk_error(error: OSError) -> None:
    raise error


def _filesystem_tree_sha256(root: Path) -> str:
    """固定非 Git 项目的普通工作文件；SDLC 运行目录不参与自身身份循环。"""

    manifest: list[dict[str, object]] = []
    try:
        for current_text, raw_directories, raw_files in os.walk(
            root,
            topdown=True,
            onerror=_raise_walk_error,
            followlinks=False,
        ):
            current = Path(current_text)
            relative_current = current.relative_to(root)
            directories: list[str] = []
            for name in sorted(raw_directories):
                candidate = current / name
                if relative_current == Path(".") and name in {".codex-sdlc", ".git"}:
                    continue
                metadata = candidate.lstat()
                relative = candidate.relative_to(root).as_posix()
                if stat.S_ISLNK(metadata.st_mode):
                    manifest.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": os.readlink(candidate),
                        }
                    )
                    continue
                directories.append(name)
                manifest.append(
                    {
                        "path": relative,
                        "kind": "directory",
                        "mode": stat.S_IMODE(metadata.st_mode),
                    }
                )
            raw_directories[:] = directories
            for name in sorted(raw_files):
                candidate = current / name
                metadata = candidate.lstat()
                relative = candidate.relative_to(root).as_posix()
                if stat.S_ISLNK(metadata.st_mode):
                    manifest.append(
                        {
                            "path": relative,
                            "kind": "symlink",
                            "target": os.readlink(candidate),
                        }
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    manifest.append(
                        {
                            "path": relative,
                            "kind": "file",
                            "mode": stat.S_IMODE(metadata.st_mode),
                            "size": metadata.st_size,
                            "sha256": sha256_file(candidate),
                        }
                    )
                else:
                    manifest.append(
                        {
                            "path": relative,
                            "kind": "other",
                            "mode": stat.S_IMODE(metadata.st_mode),
                        }
                    )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("非 Git 项目工作树无法完整读取。", exit_code=1) from exc
    return canonical_sha256(
        {
            "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
            "entries": manifest,
        }
    )


def _filesystem_identity(paths: ProjectPaths) -> dict[str, str]:
    """用真实目录身份和完整工作文件清单建立版本化非 Git 工作树证据。"""

    try:
        root = paths.root.resolve(strict=True)
        metadata = root.stat()
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SdlcError("非 Git 项目根目录不存在或无法读取。", exit_code=1) from exc
    if not root.is_dir() or root.is_symlink():
        raise SdlcError("非 Git 项目根目录必须是普通目录。", exit_code=1)
    workspace_state = _filesystem_tree_sha256(root)
    return {
        "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
        "repo_key": canonical_sha256(
            {
                "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
                "project_root": str(root),
            }
        ),
        "worktree_key": canonical_sha256(
            {
                "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
                "project_root": str(root),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
        ),
        # 沿用既有字段保存非 Git 工作文件基线；identity_contract 明确它不是 Git 提交号。
        "git_head": workspace_state,
        "dirty_paths_sha256": canonical_sha256(
            {
                "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
                "workspace_state_sha256": workspace_state,
            }
        ),
    }


def task_planning_identity(paths: ProjectPaths) -> dict[str, str]:
    """任务规划优先使用 Git；确认没有 Git 元数据时使用正式文件系统身份。"""

    try:
        return repository_identity(paths)
    except SdlcError:
        if _has_git_marker(paths.root):
            raise
        return _filesystem_identity(paths)


def repository_root(paths: ProjectPaths) -> Path:
    """返回项目所在的真实 Git 工作树根目录，供父级规则路径统一定位。"""

    identity = repository_identity(paths)
    if not identity.get("repo_key"):
        raise SdlcError("Git 仓库身份缺少稳定仓库编号。", exit_code=1)
    return _git_path(paths.root, "--show-toplevel")


def effective_rule_paths(paths: ProjectPaths) -> list[str]:
    """列出 Git 根目录到 SDLC 项目根目录之间全部实际生效的 AGENTS.md。"""

    identity = task_planning_identity(paths)
    project_root = paths.root.resolve()
    if identity["identity_contract"] == FILESYSTEM_IDENTITY_CONTRACT:
        rule = project_root / "AGENTS.md"
        return ["AGENTS.md"] if rule.is_file() and not rule.is_symlink() else []

    git_root = repository_root(paths)
    try:
        project_parts = project_root.relative_to(git_root).parts
    except ValueError as exc:
        raise SdlcError("SDLC 项目目录不在当前 Git 工作树内。", exit_code=1) from exc

    directories = [git_root]
    current = git_root
    for part in project_parts:
        current = current / part
        directories.append(current)

    result: list[str] = []
    for directory in directories:
        rule = directory / "AGENTS.md"
        if not rule.is_file() or rule.is_symlink():
            continue
        if directory == project_root:
            result.append("AGENTS.md")
        else:
            result.append(
                REPOSITORY_PATH_PREFIX
                + rule.relative_to(git_root).as_posix()
            )
    return result


def _git_pathspec(path: str) -> str:
    if path.startswith(REPOSITORY_PATH_PREFIX):
        relative = path.removeprefix(REPOSITORY_PATH_PREFIX)
        return f":(top,literal){relative}"
    return path


def _path_git_state_sha256(root: Path, path: str) -> str:
    """绑定单个关联文件的暂存和未暂存差异，避免无关脏文件误伤证据。"""

    pathspec = _git_pathspec(path)
    chunks: list[bytes] = []
    for command in (
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", pathspec],
        ["diff", "--binary", "--no-ext-diff", "--", pathspec],
        ["diff", "--cached", "--binary", "--no-ext-diff", "--", pathspec],
    ):
        output = _run_git(root, command, binary=True)
        if not isinstance(output, bytes):
            raise SdlcError("Git 关联路径差异读取结果无效。", exit_code=1)
        chunks.append(output)
    return canonical_sha256(
        [
            {"part": index, "sha256": hashlib.sha256(content).hexdigest()}
            for index, content in enumerate(chunks)
        ]
    )


def _task_output_declaration_sha256(root: Path, path: str) -> str:
    """只绑定指定前置任务的正式交付字段，不让其他任务的工作文件改变占位状态。"""

    task_id = path.removeprefix(MISSING_TASK_OUTPUT_PREFIX)
    events_file = root / ".codex-sdlc" / "events.jsonl"
    try:
        lines = events_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SdlcError("正式任务事件无法读取，不能核对前置交付声明。", exit_code=1) from exc

    found = False
    changed_files: list[str] = []
    output_files: list[str] = []

    def clean_paths(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]

    def extend_unique(target: list[str], value: object) -> None:
        for item in clean_paths(value):
            if item not in target:
                target.append(item)

    try:
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, Mapping):
                raise ValueError
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if event_type == "task_created" and event.get("task_id") == task_id:
                found = True
                changed_files = clean_paths(payload.get("changed_files"))
                output_files = clean_paths(payload.get("output_files"))
                continue
            if event_type == "task_updated" and event.get("task_id") == task_id:
                extend_unique(changed_files, payload.get("changed_files"))
                extend_unique(output_files, payload.get("output_files"))
                continue
            if event_type == "plan_updated":
                raw_tasks = payload.get("tasks")
                if not isinstance(raw_tasks, list):
                    continue
                raw_task = next(
                    (
                        item
                        for item in raw_tasks
                        if isinstance(item, Mapping)
                        and item.get("task_id") == task_id
                    ),
                    None,
                )
                if raw_task is None:
                    continue
                found = True
                if "changed_files" in raw_task:
                    changed_files = clean_paths(raw_task.get("changed_files"))
                if "output_files" in raw_task:
                    output_files = clean_paths(raw_task.get("output_files"))
                continue
            if event_type == "task_plan_imported":
                raw_tasks = payload.get("tasks")
                if not isinstance(raw_tasks, list):
                    continue
                raw_task = next(
                    (
                        item
                        for item in raw_tasks
                        if isinstance(item, Mapping)
                        and item.get("task_id") == task_id
                    ),
                    None,
                )
                if raw_task is None:
                    continue
                found = True
                scope = raw_task.get("code_scope")
                if isinstance(scope, Mapping):
                    changed_files = clean_paths(scope.get("likely_change_paths"))
                output_files = []
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SdlcError("正式任务事件不是可核对的结构化记录。", exit_code=1) from exc

    return canonical_sha256(
        {
            "task_id": task_id,
            "formal_task_recorded": found,
            "changed_files": sorted(set(changed_files)),
            "output_files": sorted(set(output_files)),
        }
    )


def _path_filesystem_state_sha256(
    root: Path,
    path: str,
    resolved: Path | None,
) -> str:
    if path.startswith(MISSING_TASK_OUTPUT_PREFIX):
        return canonical_sha256(
            {
                "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
                "path": path,
                "state": "missing_task_output",
                "declaration_sha256": _task_output_declaration_sha256(root, path),
            }
        )
    manifest = _filesystem_path_manifest(root, path)
    if resolved is None:
        return canonical_sha256(
            {
                "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
                **manifest,
            }
        )
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise SdlcError(f"非 Git 证据路径状态无法读取：{path}。", exit_code=1) from exc
    return canonical_sha256(
        {
            "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
            "path": path,
            "state": "file",
            "mode": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size,
            "sha256": sha256_file(resolved),
        }
    )


def _path_state_sha256(
    root: Path,
    path: str,
    *,
    identity_contract: str,
    resolved: Path | None,
) -> str:
    if identity_contract == FILESYSTEM_IDENTITY_CONTRACT:
        return _path_filesystem_state_sha256(
            root,
            path,
            resolved,
        )
    return _path_git_state_sha256(root, path)


def _filesystem_path_manifest(root: Path, path: str) -> dict[str, object]:
    """读取非 Git 证据路径当前类型，不把目录或链接误判成仍然缺失。"""

    relative = Path(path)
    if (
        not path
        or path.startswith(REPOSITORY_PATH_PREFIX)
        or path.startswith(MISSING_TASK_OUTPUT_PREFIX)
        or relative.is_absolute()
        or ".." in relative.parts
        or relative == Path(".")
    ):
        raise SdlcError(f"非 Git 证据路径不是合法项目相对路径：{path or '空路径'}。", exit_code=1)
    current = root.resolve()
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            metadata = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return {"path": relative.as_posix(), "state": "missing"}
        except OSError as exc:
            raise SdlcError(f"非 Git 证据路径状态无法读取：{path}。", exit_code=1) from exc
        if stat.S_ISLNK(metadata.st_mode):
            return {
                "path": relative.as_posix(),
                "state": "symlink",
                "component": Path(*relative.parts[: index + 1]).as_posix(),
                "target": os.readlink(current),
            }
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return {
                "path": relative.as_posix(),
                "state": "blocked",
                "component": Path(*relative.parts[: index + 1]).as_posix(),
                "mode": stat.S_IMODE(metadata.st_mode),
            }
    if stat.S_ISREG(metadata.st_mode):
        return {
            "path": relative.as_posix(),
            "state": "file",
            "mode": stat.S_IMODE(metadata.st_mode),
            "size": metadata.st_size,
            "sha256": sha256_file(current),
        }
    if stat.S_ISDIR(metadata.st_mode):
        return {
            "path": relative.as_posix(),
            "state": "directory",
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return {
        "path": relative.as_posix(),
        "state": "other",
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _normalize_missing_evidence_path(root: Path, raw_path: object) -> str:
    path = str(raw_path or "").strip()
    if re.fullmatch(r"@task-output/T-[0-9]{3,}", path):
        return path
    manifest = _filesystem_path_manifest(root, path)
    if manifest.get("state") != "missing":
        raise SdlcError(f"代码证据路径当前不是缺失状态：{path}。", exit_code=1)
    return str(manifest["path"])


def _missing_content_sha256(path: str) -> str:
    return canonical_sha256(
        {
            "identity_contract": FILESYSTEM_IDENTITY_CONTRACT,
            "path": path,
            "state": "missing",
        }
    )


def _regular_utf8_file(root: Path, raw_path: object) -> tuple[str, Path]:
    path_text = str(raw_path or "").strip()
    base = root.resolve()
    relative_text = path_text
    if path_text.startswith(REPOSITORY_PATH_PREFIX):
        relative_text = path_text.removeprefix(REPOSITORY_PATH_PREFIX)
        base = _git_path(root, "--show-toplevel")
    try:
        resolved = resolve_project_path(base, relative_text, must_exist=True)
    except SdlcError as exc:
        raise SdlcError(f"代码证据路径无效：{exc.message}", exit_code=1) from exc

    # resolve 会跟随符号链接，所以还要逐层检查调用方实际写出的路径，不能让链接伪装成普通文件。
    current = base
    for part in Path(relative_text).parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"代码证据路径不能包含符号链接：{path_text}。", exit_code=1)
    if not resolved.is_file() or resolved.is_symlink():
        raise SdlcError(f"代码证据必须指向项目内普通文件：{path_text}。", exit_code=1)
    try:
        resolved.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SdlcError(f"代码证据文件必须是有效 UTF-8 文本：{path_text}。", exit_code=1) from exc
    normalized = (
        REPOSITORY_PATH_PREFIX + Path(relative_text).as_posix()
        if path_text.startswith(REPOSITORY_PATH_PREFIX)
        else Path(path_text).as_posix()
    )
    return normalized, resolved


def _selection_entries(
    selection: Mapping[str, object],
) -> tuple[str, list[tuple[str, str, str, str]]]:
    allowed_fields = {"purpose", *EVIDENCE_GROUPS}
    extra_fields = sorted(set(selection) - allowed_fields)
    if extra_fields:
        raise SdlcError(
            f"代码证据选择不能自报身份、哈希或状态字段：{', '.join(extra_fields)}。",
            exit_code=1,
        )
    purpose = str(selection.get("purpose") or "")
    if purpose not in EVIDENCE_PURPOSES:
        raise SdlcError(
            "代码证据 purpose 必须是 integrated_design 或 task_planning。",
            exit_code=1,
        )
    result: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for group in EVIDENCE_GROUPS:
        raw_items = selection.get(group)
        if not isinstance(raw_items, list):
            raise SdlcError(f"代码证据 {group} 必须是数组。", exit_code=1)
        for raw_item in raw_items:
            if group == "code_files":
                if not isinstance(raw_item, Mapping):
                    raise SdlcError("code_files 项必须是对象。", exit_code=1)
                extra_fields = sorted(set(raw_item) - {"path", "reason_ref", "state"})
                if extra_fields:
                    raise SdlcError(
                        "code_files 选择项包含不允许的字段："
                        + "、".join(extra_fields)
                        + "。",
                        exit_code=1,
                    )
                path = str(raw_item.get("path") or "")
                reason_ref = str(raw_item.get("reason_ref") or "")
            else:
                if isinstance(raw_item, Mapping):
                    if group != "upstream_outputs":
                        raise SdlcError(
                            f"代码证据 {group} 项必须是路径字符串。",
                            exit_code=1,
                        )
                    extra_fields = sorted(set(raw_item) - {"path", "state"})
                    if extra_fields:
                        raise SdlcError(
                            "upstream_outputs 选择项包含不允许的字段："
                            + "、".join(extra_fields)
                            + "。",
                            exit_code=1,
                        )
                    path = str(raw_item.get("path") or "")
                elif isinstance(raw_item, str):
                    path = str(raw_item or "")
                else:
                    raise SdlcError(f"代码证据 {group} 项必须是路径字符串。", exit_code=1)
                reason_ref = ""
            state = (
                str(raw_item.get("state") or "present")
                if isinstance(raw_item, Mapping)
                else "present"
            )
            if state not in {"present", "missing"}:
                raise SdlcError(f"代码证据路径状态无效：{path}。", exit_code=1)
            if state == "missing" and group not in {"code_files", "upstream_outputs"}:
                raise SdlcError(
                    f"代码证据 {group} 不能记录缺失路径：{path}。",
                    exit_code=1,
                )
            if path in seen:
                raise SdlcError(f"代码证据路径不能重复：{path}。", exit_code=1)
            seen.add(path)
            result.append((group, path, reason_ref, state))
    if not result:
        label = "设计总计划" if purpose == "integrated_design" else "任务规划"
        raise SdlcError(f"{label}至少要列出一个真实证据文件。", exit_code=1)
    return purpose, result


def _reuse_comparison(value: Mapping[str, object]) -> dict[str, object]:
    """重试只比较会影响规划判断的内容，采集时间和无关 HEAD 不制造新包。"""

    ignored = {"captured_at", "evidence_sha256"}
    if value.get("identity_contract", GIT_IDENTITY_CONTRACT) == GIT_IDENTITY_CONTRACT:
        ignored.add("git_head")
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key not in ignored
    }


def capture_code_evidence(
    paths: ProjectPaths,
    *,
    owner_id: str,
    selection: Mapping[str, object],
    reuse_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, str]]] = {group: [] for group in EVIDENCE_GROUPS}
    resolved_paths: set[Path] = set()
    purpose, entries = _selection_entries(selection)
    identity = (
        task_planning_identity(paths)
        if purpose == "task_planning"
        else repository_identity(paths)
    )
    identity_contract = identity["identity_contract"]
    for group, raw_path, reason_ref, expected_state in entries:
        resolved: Path | None = None
        if expected_state == "missing":
            if identity_contract != FILESYSTEM_IDENTITY_CONTRACT:
                raise SdlcError(
                    f"Git 任务规划代码证据不能记录缺失路径：{raw_path}。",
                    exit_code=1,
                )
            normalized = _normalize_missing_evidence_path(paths.root, raw_path)
            resolved = None
            item = {
                "path": normalized,
                "state": "missing",
                "sha256": _missing_content_sha256(normalized),
                "git_state_sha256": _path_state_sha256(
                    paths.root,
                    normalized,
                    identity_contract=identity_contract,
                    resolved=None,
                ),
            }
        else:
            normalized, resolved = _regular_utf8_file(paths.root, raw_path)
            if resolved in resolved_paths:
                raise SdlcError(f"代码证据路径指向同一个文件：{raw_path}。", exit_code=1)
            resolved_paths.add(resolved)
            item = {
                "path": normalized,
                "state": "present",
                "sha256": sha256_file(resolved),
                "git_state_sha256": _path_state_sha256(
                    paths.root,
                    normalized,
                    identity_contract=identity_contract,
                    resolved=resolved,
                ),
            }
        if reason_ref:
            item["reason_ref"] = reason_ref
        grouped[group].append(item)
    for group in EVIDENCE_GROUPS:
        grouped[group].sort(key=lambda item: item["path"])

    selected_snapshot = [
        {"group": group, **deepcopy(item)}
        for group in EVIDENCE_GROUPS
        for item in grouped[group]
    ]
    content_snapshot = [
        {key: value for key, value in item.items() if key != "git_state_sha256"}
        for item in selected_snapshot
    ]
    dirty_snapshot = [
        {
            "group": item["group"],
            "path": item["path"],
            "git_state_sha256": item["git_state_sha256"],
        }
        for item in selected_snapshot
    ]
    evidence: dict[str, object] = {
        "schema_version": CODE_EVIDENCE_SCHEMA,
        "owner_id": owner_id,
        "purpose": purpose,
        **identity,
        **grouped,
        # 这个字段只绑定计划明确读取的路径；全仓库状态仍可由 git_head 单独审计。
        "dirty_paths_sha256": canonical_sha256(dirty_snapshot),
        "selected_paths_sha256": canonical_sha256(
            [{"group": item["group"], "path": item["path"]} for item in selected_snapshot]
        ),
        "relevant_content_sha256": canonical_sha256(content_snapshot),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    evidence["evidence_sha256"] = canonical_sha256(evidence)
    validate_code_evidence(evidence)
    if reuse_evidence is not None:
        existing = validate_code_evidence(reuse_evidence)
        if _reuse_comparison(existing) == _reuse_comparison(evidence):
            return existing
    return evidence


def validate_code_evidence(value: Mapping[str, object]) -> dict[str, object]:
    evidence = deepcopy(dict(value))
    validate_schema_document(evidence, schema_name=CODE_EVIDENCE_SCHEMA)
    identity_contract = str(
        evidence.get("identity_contract") or GIT_IDENTITY_CONTRACT
    )
    if identity_contract not in {
        GIT_IDENTITY_CONTRACT,
        FILESYSTEM_IDENTITY_CONTRACT,
    }:
        raise SdlcError("代码证据使用了不支持的工作树身份合同。", exit_code=1)
    if (
        identity_contract == FILESYSTEM_IDENTITY_CONTRACT
        and evidence.get("purpose") != "task_planning"
    ):
        raise SdlcError(
            "非 Git 文件系统身份只能用于任务规划代码证据。",
            exit_code=1,
        )
    expected = canonical_sha256(
        {key: item for key, item in evidence.items() if key != "evidence_sha256"}
    )
    if evidence.get("evidence_sha256") != expected:
        raise SdlcError("代码证据记录哈希与内容不一致。", exit_code=1)

    selected = [
        {"group": group, **deepcopy(item)}
        for group in EVIDENCE_GROUPS
        for item in evidence[group]  # type: ignore[index]
    ]
    if not selected:
        raise SdlcError("代码证据记录至少要包含一个真实文件。", exit_code=1)
    paths_seen: set[str] = set()
    for group in EVIDENCE_GROUPS:
        for item in evidence[group]:  # type: ignore[index]
            path = str(item["path"])
            path_state = str(item.get("state") or "present")
            if path_state not in {"present", "missing"}:
                raise SdlcError(f"代码证据路径状态无效：{path}。", exit_code=1)
            if path_state == "missing":
                if (
                    identity_contract != FILESYSTEM_IDENTITY_CONTRACT
                    or group not in {"code_files", "upstream_outputs"}
                ):
                    raise SdlcError(
                        f"代码证据不能在当前身份合同中记录缺失路径：{path}。",
                        exit_code=1,
                    )
                if path.startswith(MISSING_TASK_OUTPUT_PREFIX):
                    if (
                        group != "upstream_outputs"
                        or re.fullmatch(r"@task-output/T-[0-9]{3,}", path) is None
                    ):
                        raise SdlcError(
                            f"已完成前置任务缺失记录无效：{path}。",
                            exit_code=1,
                        )
                elif path.startswith("@"):
                    raise SdlcError(f"非 Git 缺失证据路径无效：{path}。", exit_code=1)
                if item["sha256"] != _missing_content_sha256(path):
                    raise SdlcError(
                        f"缺失代码证据内容哈希与路径状态不一致：{path}。",
                        exit_code=1,
                    )
            elif path.startswith(MISSING_TASK_OUTPUT_PREFIX):
                raise SdlcError(
                    f"已完成前置任务占位路径必须明确记录 missing：{path}。",
                    exit_code=1,
                )
        group_paths = [str(item["path"]) for item in evidence[group]]  # type: ignore[index]
        if group_paths != sorted(group_paths):
            raise SdlcError(f"代码证据 {group} 必须按路径稳定排序。", exit_code=1)
        duplicates = sorted(paths_seen.intersection(group_paths))
        if duplicates or len(group_paths) != len(set(group_paths)):
            repeated = duplicates or sorted(
                path for path in set(group_paths) if group_paths.count(path) > 1
            )
            raise SdlcError(f"代码证据路径不能重复：{', '.join(repeated)}。", exit_code=1)
        paths_seen.update(group_paths)
    if evidence.get("selected_paths_sha256") != canonical_sha256(
        [{"group": item["group"], "path": item["path"]} for item in selected]
    ):
        raise SdlcError("代码证据路径范围哈希与文件清单不一致。", exit_code=1)
    content_selected = [
        {key: value for key, value in item.items() if key != "git_state_sha256"}
        for item in selected
    ]
    if evidence.get("relevant_content_sha256") != canonical_sha256(content_selected):
        raise SdlcError("代码证据内容范围哈希与文件清单不一致。", exit_code=1)
    dirty_selected = [
        {
            "group": item["group"],
            "path": item["path"],
            "git_state_sha256": item["git_state_sha256"],
        }
        for item in selected
    ]
    if evidence.get("dirty_paths_sha256") != canonical_sha256(dirty_selected):
        raise SdlcError("代码证据脏路径摘要与逐文件 Git 状态不一致。", exit_code=1)
    return evidence


def assess_code_evidence(
    paths: ProjectPaths, value: Mapping[str, object]
) -> dict[str, object]:
    """有效性只看仓库身份和关联文件；无关 HEAD 或脏文件变化不会误伤计划。"""

    evidence = validate_code_evidence(value)
    recorded_identity_contract = str(
        evidence.get("identity_contract") or GIT_IDENTITY_CONTRACT
    )
    identity = (
        task_planning_identity(paths)
        if recorded_identity_contract == FILESYSTEM_IDENTITY_CONTRACT
        else repository_identity(paths)
    )
    current_identity_contract = identity["identity_contract"]
    changed: list[str] = []
    if recorded_identity_contract != current_identity_contract:
        changed.append("identity_contract")
    if evidence["repo_key"] != identity["repo_key"]:
        changed.append("repo_key")
    if evidence["worktree_key"] != identity["worktree_key"]:
        changed.append("worktree_key")
    current_dirty: list[dict[str, str]] = []
    for group in EVIDENCE_GROUPS:
        for item in evidence[group]:  # type: ignore[index]
            path = str(item["path"])
            path_state = str(item.get("state") or "present")
            resolved: Path | None = None
            if path_state == "present":
                try:
                    _normalized, resolved = _regular_utf8_file(paths.root, path)
                except SdlcError:
                    pass
            actual_git_state = _path_state_sha256(
                paths.root,
                path,
                identity_contract=current_identity_contract,
                resolved=resolved,
            )
            current_dirty.append(
                {
                    "group": group,
                    "path": path,
                    "git_state_sha256": actual_git_state,
                }
            )
            if path_state == "missing":
                if actual_git_state != item["git_state_sha256"]:
                    changed.append(path)
                continue
            if resolved is None:
                changed.append(path)
                continue
            actual = sha256_file(resolved)
            if (
                actual != item["sha256"]
                or actual_git_state != item["git_state_sha256"]
            ):
                changed.append(path)
    current_dirty_sha256 = canonical_sha256(current_dirty)
    workspace_state_changed = evidence["git_head"] != identity["git_head"]
    return {
        "status": "stale" if changed else "current",
        "changed_paths": sorted(set(changed)),
        "git_head_changed": workspace_state_changed,
        "workspace_state_changed": workspace_state_changed,
        "dirty_paths_changed": evidence["dirty_paths_sha256"] != current_dirty_sha256,
    }


def _task_output_paths(tasks: Iterable[Mapping[str, object]]) -> set[str]:
    """只接受已经开工任务声明的输出范围，未开工任务不能吞掉证据漂移。"""

    started_statuses = {
        "doing",
        "done",
        "ready_for_user_check",
        "test_failed",
        "changed",
        "paused",
    }
    result: set[str] = set()
    for task in tasks:
        if str(task.get("status") or "") not in started_statuses:
            continue
        contract = task.get("task_contract")
        source = contract if isinstance(contract, Mapping) else task
        scope = source.get("code_scope") if isinstance(source, Mapping) else None
        if not isinstance(scope, Mapping):
            continue
        raw_paths = scope.get("likely_change_paths")
        if not isinstance(raw_paths, list):
            continue
        result.update(str(path).strip().rstrip("/") for path in raw_paths if str(path).strip())
    return result


def _path_in_declared_output(path: str, declared_outputs: set[str]) -> bool:
    return any(
        path == output or path.startswith(f"{output}/")
        for output in declared_outputs
        if output
    )


def assess_task_planning_code_evidence(
    paths: ProjectPaths,
    value: Mapping[str, object],
    *,
    tasks: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """规划证据变化会使审核过期，已开工任务的正常代码输出不会反复误伤整套计划。"""

    evidence = validate_code_evidence(value)
    if evidence.get("purpose") != "task_planning":
        raise SdlcError("任务规划只能使用 purpose=task_planning 的代码证据。", exit_code=1)
    assessment = assess_code_evidence(paths, evidence)
    code_paths = {
        str(item["path"])
        for item in evidence["code_files"]  # type: ignore[index]
        if isinstance(item, Mapping)
        and str(item.get("state") or "present") == "present"
    }
    declared_outputs = _task_output_paths(tasks)
    ignored = sorted(
        path
        for path in assessment["changed_paths"]  # type: ignore[index]
        if path in code_paths and _path_in_declared_output(path, declared_outputs)
    )
    remaining = sorted(set(assessment["changed_paths"]) - set(ignored))  # type: ignore[arg-type,index]
    return {
        **assessment,
        "status": "stale" if remaining else "current",
        "changed_paths": remaining,
        "ignored_task_output_paths": ignored,
    }


__all__ = [
    "CODE_EVIDENCE_SCHEMA",
    "EVIDENCE_GROUPS",
    "EVIDENCE_PURPOSES",
    "FILESYSTEM_IDENTITY_CONTRACT",
    "GIT_IDENTITY_CONTRACT",
    "assess_code_evidence",
    "assess_task_planning_code_evidence",
    "capture_code_evidence",
    "effective_rule_paths",
    "repository_identity",
    "repository_root",
    "task_planning_identity",
    "validate_code_evidence",
]
