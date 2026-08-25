from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from codex_sdlc.core.project import ProjectPaths


LEGACY_TASK_PACK_READ_SCHEMA = "legacy-task-pack-read.v1"
_REQUIRED_FIELDS = ("requirement_id", "task_id", "status")
_PATH_LIST_FIELDS = {"context_files", "output_files", "related_files", "files"}
RUNTIME_NOISE_PREFIXES = (
    ".codex-sdlc/",
    ".codegraphcontext/",
    "outputs/",
    "output/",
    "temp/",
    "tmp/",
    "dist/",
    "build/",
    "node_modules/",
    "oh_modules/",
)


@dataclass(frozen=True)
class LegacyTaskPackIssue:
    code: str
    message: str
    path: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True)
class LegacyTaskPackReadResult:
    schema_version: str
    requirement_id: str
    task_id: str
    status: str
    path: str
    participates_in_current_workflow: bool
    metadata: dict[str, Any] | None
    file_sha256: dict[str, str]
    issues: tuple[LegacyTaskPackIssue, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "requirement_id": self.requirement_id,
            "task_id": self.task_id,
            "status": self.status,
            "path": self.path,
            "participates_in_current_workflow": self.participates_in_current_workflow,
            "metadata": self.metadata,
            "file_sha256": dict(self.file_sha256),
            "issues": [item.as_dict() for item in self.issues],
        }


def normalized_runtime_path(path: object) -> str:
    normalized = str(path or "").strip().strip("`").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_runtime_noise_path(path: object) -> bool:
    return normalized_runtime_path(path).startswith(RUNTIME_NOISE_PREFIXES)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(paths: ProjectPaths, path: Path) -> str:
    try:
        return path.relative_to(paths.root).as_posix()
    except ValueError:
        return str(path)


def _issue(code: str, message: str, paths: ProjectPaths, path: Path) -> LegacyTaskPackIssue:
    return LegacyTaskPackIssue(code=code, message=message, path=_relative_path(paths, path))


def _pack_path(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
) -> tuple[Path, list[LegacyTaskPackIssue]]:
    folder_name = str(requirement.get("folder_name") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    issues: list[LegacyTaskPackIssue] = []
    if not folder_name or Path(folder_name).name != folder_name or folder_name in {".", ".."}:
        fallback = paths.requirements_dir / "<无效需求目录>" / "task-packs" / (task_id or "<无效任务>")
        issues.append(_issue("path_outside", "需求目录不是受控的单层目录名", paths, fallback))
        return fallback, issues
    if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
        fallback = paths.requirements_dir / folder_name / "task-packs" / "<无效任务>"
        issues.append(_issue("path_outside", "任务编号不能用于受控档案路径", paths, fallback))
        return fallback, issues
    return paths.requirements_dir / folder_name / "task-packs" / task_id, issues


def _symlink_issue(paths: ProjectPaths, target: Path) -> LegacyTaskPackIssue | None:
    current = paths.root
    try:
        relative = target.relative_to(paths.root)
    except ValueError:
        return _issue("path_outside", "既有任务执行包越过项目目录", paths, target)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return _issue("symlink", "既有任务执行包路径不能经过符号链接", paths, current)
    return None


def _reference_issue(paths: ProjectPaths, raw: object) -> LegacyTaskPackIssue | None:
    text = str(raw or "").strip()
    candidate = Path(text)
    if (
        not text
        or "\x00" in text
        or "\\" in text
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return _issue("path_outside", f"既有任务执行包包含越界路径：{text or '<空>'}", paths, paths.root / text)
    target = paths.root / candidate
    symlink = _symlink_issue(paths, target)
    if symlink is not None:
        return LegacyTaskPackIssue(
            code="symlink",
            message=f"既有任务执行包引用经过符号链接：{text}",
            path=text,
        )
    try:
        target.resolve(strict=False).relative_to(paths.root.resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return _issue("path_outside", f"既有任务执行包包含越界路径：{text}", paths, target)
    return None


def _metadata_paths(value: object, *, field: str = "") -> list[object]:
    found: list[object] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text == "path":
                found.append(item)
            elif key_text in _PATH_LIST_FIELDS and isinstance(item, list):
                for record in item:
                    if isinstance(record, dict) and "path" in record:
                        found.append(record.get("path"))
                    elif isinstance(record, str):
                        found.append(record)
                    else:
                        found.extend(_metadata_paths(record, field=key_text))
            else:
                found.extend(_metadata_paths(item, field=key_text))
    elif isinstance(value, list):
        for item in value:
            found.extend(_metadata_paths(item, field=field))
    return found


def read_legacy_task_pack(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
) -> LegacyTaskPackReadResult:
    """读取一份旧执行包；所有异常都变成只读结果，不修复、不补写。"""

    requirement_id = str(requirement.get("requirement_id") or "").strip()
    task_id = str(task.get("task_id") or "").strip()
    pack_dir, issues = _pack_path(paths, requirement, task)
    relative_pack = _relative_path(paths, pack_dir)
    if issues:
        return LegacyTaskPackReadResult(
            LEGACY_TASK_PACK_READ_SCHEMA,
            requirement_id,
            task_id,
            "damaged",
            relative_pack,
            False,
            None,
            {},
            tuple(issues),
        )
    if not pack_dir.exists() and not pack_dir.is_symlink():
        return LegacyTaskPackReadResult(
            LEGACY_TASK_PACK_READ_SCHEMA,
            requirement_id,
            task_id,
            "missing",
            relative_pack,
            False,
            None,
            {},
            (),
        )

    symlink = _symlink_issue(paths, pack_dir)
    if symlink is not None:
        issues.append(symlink)
    elif not pack_dir.is_dir():
        issues.append(_issue("not_directory", "既有任务执行包位置不是目录", paths, pack_dir))

    metadata: dict[str, Any] | None = None
    hashes: dict[str, str] = {}
    for name in ("task-pack.md", "task-pack.json"):
        file_path = pack_dir / name
        if symlink is not None:
            continue
        if not file_path.exists() and not file_path.is_symlink():
            issues.append(_issue("file_missing", f"既有任务执行包缺少 {name}", paths, file_path))
            continue
        file_symlink = _symlink_issue(paths, file_path)
        if file_symlink is not None:
            issues.append(file_symlink)
            continue
        if not file_path.is_file():
            issues.append(_issue("not_file", f"既有任务执行包的 {name} 不是普通文件", paths, file_path))
            continue
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            issues.append(_issue("read_failed", f"既有任务执行包无法读取：{exc}", paths, file_path))
            continue
        hashes[name] = _sha256(raw)
        if name != "task-pack.json":
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(_issue("utf8_invalid", "task-pack.md 不是有效 UTF-8", paths, file_path))
            continue
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            issues.append(_issue("json_invalid", "task-pack.json 不是有效 UTF-8 JSON", paths, file_path))
            continue
        if not isinstance(loaded, dict):
            issues.append(_issue("json_invalid", "task-pack.json 顶层必须是对象", paths, file_path))
            continue
        metadata = loaded

    if metadata is not None:
        for field in _REQUIRED_FIELDS:
            if not str(metadata.get(field) or "").strip():
                issues.append(_issue("field_missing", f"task-pack.json 缺少字段：{field}", paths, pack_dir / "task-pack.json"))
        if metadata.get("requirement_id") not in {None, "", requirement_id}:
            issues.append(_issue("identity_mismatch", "task-pack.json 的需求编号与目录不一致", paths, pack_dir / "task-pack.json"))
        if metadata.get("task_id") not in {None, "", task_id}:
            issues.append(_issue("identity_mismatch", "task-pack.json 的任务编号与目录不一致", paths, pack_dir / "task-pack.json"))
        seen_paths: set[str] = set()
        for raw_path in _metadata_paths(metadata):
            marker = str(raw_path)
            if marker in seen_paths:
                continue
            seen_paths.add(marker)
            path_issue = _reference_issue(paths, raw_path)
            if path_issue is not None:
                issues.append(path_issue)

    return LegacyTaskPackReadResult(
        LEGACY_TASK_PACK_READ_SCHEMA,
        requirement_id,
        task_id,
        "damaged" if issues else "readable",
        relative_pack,
        False,
        metadata,
        hashes,
        tuple(issues),
    )


def inspect_requirement_legacy_task_packs(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    *,
    include_missing: bool = False,
) -> list[LegacyTaskPackReadResult]:
    tasks = [item for item in requirement.get("tasks", []) if isinstance(item, Mapping)]
    expected = {str(item.get("task_id") or ""): item for item in tasks if str(item.get("task_id") or "")}
    folder_name = str(requirement.get("folder_name") or "")
    base = paths.requirements_dir / folder_name / "task-packs"
    discovered: set[str] = set()
    if base.is_dir() and not base.is_symlink():
        for child in sorted(base.iterdir(), key=lambda item: item.name):
            if child.name:
                discovered.add(child.name)
    task_ids = discovered | (set(expected) if include_missing else set())
    if base.is_symlink() and not task_ids:
        task_ids = set(expected) or {"<未知任务>"}
    results = [
        read_legacy_task_pack(paths, requirement, expected.get(task_id, {"task_id": task_id}))
        for task_id in sorted(task_ids)
    ]
    return [item for item in results if include_missing or item.status != "missing"]


def inspect_legacy_task_packs(
    paths: ProjectPaths,
    state: Mapping[str, object],
    *,
    include_missing: bool = False,
) -> list[LegacyTaskPackReadResult]:
    requirements = state.get("requirements")
    if not isinstance(requirements, Mapping):
        return []
    results: list[LegacyTaskPackReadResult] = []
    for requirement in requirements.values():
        if isinstance(requirement, Mapping):
            results.extend(
                inspect_requirement_legacy_task_packs(paths, requirement, include_missing=include_missing)
            )
    return results


def legacy_task_pack_check_messages(
    results: list[LegacyTaskPackReadResult],
) -> tuple[list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []
    for result in results:
        identity = f"{result.requirement_id} / {result.task_id}".strip(" /")
        if result.status == "readable":
            passed.append(f"既有任务执行包只读完整性正常：{identity}；不参与当前流程状态")
        elif result.status == "damaged":
            details = "；".join(item.message for item in result.issues) or "完整性不明"
            warnings.append(f"既有任务执行包只读检查发现问题：{identity}：{details}；不会自动修复，也不参与当前流程状态")
    return passed, warnings


def legacy_task_pack_display_lines(results: list[LegacyTaskPackReadResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        identity = f"{result.requirement_id} / {result.task_id}".strip(" /")
        if result.status == "readable":
            lines.append(f"- {identity}：档案可读，只供追溯，不参与当前任务状态和下一步推荐。")
        elif result.status == "damaged":
            codes = "、".join(dict.fromkeys(item.code for item in result.issues)) or "unknown"
            lines.append(f"- {identity}：档案有完整性问题（{codes}），不会自动修复，也不参与当前任务状态。")
        else:
            lines.append(f"- {identity}：没有既有任务执行包，不影响当前流程。")
    return lines
