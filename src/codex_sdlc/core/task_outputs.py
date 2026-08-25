from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.render import join_lines
from codex_sdlc.core.structured_contract import canonical_json_text, sha256_bytes, sha256_file


TASK_OUTPUT_DIR = "task-outputs"
TASK_OUTPUT_INDEX_SCHEMA = "task-output-index.v1"
TASK_OUTPUT_INDEX_NAME = "task-output-index.v1.json"
CONTRACT_ID_PREFIXES = ("FR-", "AC-", "TC-", "CHG-")
_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3,}$")
_TASK_ID = re.compile(r"^T-[0-9]{3,}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def clean_items(items: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def file_hash(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def requirement_dir(paths: ProjectPaths, requirement: Mapping[str, object]) -> Path:
    return paths.requirements_dir / str(requirement["folder_name"])


def upstream_match_path(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
) -> Path:
    """上游匹配属于任务交付记录，不能再写进既有任务执行包目录。"""

    return task_outputs_dir(paths, dict(requirement)) / "upstream-matches" / f"{task['task_id']}.json"


def _unique_formal_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"任务交付物索引包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_formal_constant(value: str) -> object:
    raise SdlcError(f"任务交付物索引包含非标准数字：{value}。", exit_code=1)


def _formal_requirement_root(
    paths: ProjectPaths, requirement: Mapping[str, object]
) -> Path:
    requirement_id = str(requirement.get("requirement_id") or "")
    folder_name = str(requirement.get("folder_name") or "")
    if (
        not _REQUIREMENT_ID.fullmatch(requirement_id)
        or not folder_name
        or Path(folder_name).name != folder_name
        or folder_name in {".", ".."}
    ):
        raise SdlcError("正式需求身份不完整，不能读写任务交付物索引。", exit_code=1)
    root = paths.requirements_dir / folder_name
    if root.is_symlink() or not root.is_dir():
        raise SdlcError(f"找不到正式需求目录：{requirement_id}。", exit_code=1)
    return root


def formal_task_output_index_path(
    paths: ProjectPaths, requirement: Mapping[str, object]
) -> Path:
    return _formal_requirement_root(paths, requirement) / TASK_OUTPUT_DIR / TASK_OUTPUT_INDEX_NAME


def _normalized_project_file(paths: ProjectPaths, raw_path: object) -> tuple[str, Path]:
    relative = str(raw_path) if isinstance(raw_path, str) else ""
    candidate = Path(relative)
    if (
        not relative
        or candidate.is_absolute()
        or "\\" in relative
        or candidate.as_posix() != relative
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise SdlcError(f"任务交付物路径不合规：{relative or '<空>'}。", exit_code=1)
    target = paths.root.joinpath(candidate)
    try:
        target.resolve(strict=True).relative_to(paths.root.resolve())
    except (OSError, ValueError) as exc:
        raise SdlcError(f"任务交付物路径不在项目内：{relative}。", exit_code=1) from exc
    if target.is_symlink() or not target.is_file():
        raise SdlcError(f"任务交付物不是项目内普通文件：{relative}。", exit_code=1)
    return relative, target


def _path_is_allowed(relative: str, allowed_paths: object) -> bool:
    if not isinstance(allowed_paths, list):
        return False
    candidate = Path(relative)
    for raw in allowed_paths:
        allowed = str(raw) if isinstance(raw, str) else ""
        if not allowed or "\\" in allowed or Path(allowed).is_absolute():
            continue
        allowed_path = Path(allowed)
        if allowed_path.as_posix() != allowed or any(part in {".", ".."} for part in allowed_path.parts):
            continue
        if candidate == allowed_path or allowed_path in candidate.parents:
            return True
    return False


def _validate_formal_index_shape(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    document: Mapping[str, object],
    *,
    verify_files: bool,
    verify_runtime: bool = False,
) -> dict[str, object]:
    requirement_id = str(requirement.get("requirement_id") or "")
    if (
        set(document) != {"schema_version", "requirement_id", "index_id", "task_outputs"}
        or document.get("schema_version") != TASK_OUTPUT_INDEX_SCHEMA
        or document.get("requirement_id") != requirement_id
        or document.get("index_id") != f"{requirement_id}:{TASK_OUTPUT_INDEX_SCHEMA}"
        or not isinstance(document.get("task_outputs"), list)
    ):
        raise SdlcError("任务交付物索引顶层结构或身份不符合 task-output-index.v1。", exit_code=1)
    outputs = document["task_outputs"]
    assert isinstance(outputs, list)
    seen_tasks: set[str] = set()
    seen_entries: set[str] = set()
    seen_outputs: set[str] = set()
    previous_task = ""
    for entry in outputs:
        if not isinstance(entry, Mapping) or set(entry) != {
            "entry_id", "task_id", "completed_run_number", "task_sha256",
            "task_run_path", "task_run_sha256", "files",
        }:
            raise SdlcError("任务交付物索引的任务项字段不完整或包含额外字段。", exit_code=1)
        task_id = str(entry.get("task_id") or "")
        run_number = entry.get("completed_run_number")
        entry_id = str(entry.get("entry_id") or "")
        if (
            not _TASK_ID.fullmatch(task_id)
            or not isinstance(run_number, int)
            or isinstance(run_number, bool)
            or run_number < 1
            or entry_id != f"{requirement_id}:{task_id}:run-{run_number:04d}"
            or not _SHA256.fullmatch(str(entry.get("task_sha256") or ""))
            or not _SHA256.fullmatch(str(entry.get("task_run_sha256") or ""))
            or not isinstance(entry.get("files"), list)
            or task_id in seen_tasks
            or entry_id in seen_entries
            or (previous_task and task_id <= previous_task)
        ):
            raise SdlcError("任务交付物索引的任务身份、排序或哈希不合法。", exit_code=1)
        expected_run_path = (
            _formal_requirement_root(paths, requirement)
            / "runtime" / task_id / "runs" / f"{run_number:04d}" / "task-run.v1.json"
        ).relative_to(paths.root).as_posix()
        if entry.get("task_run_path") != expected_run_path:
            raise SdlcError("任务交付物索引的关闭轮次路径不正确。", exit_code=1)
        seen_tasks.add(task_id)
        seen_entries.add(entry_id)
        previous_task = task_id
        previous_path = ""
        task_paths: set[str] = set()
        files = entry["files"]
        assert isinstance(files, list)
        for file_entry in files:
            if not isinstance(file_entry, Mapping) or set(file_entry) != {"output_id", "path", "sha256"}:
                raise SdlcError("任务交付物索引的文件项字段不完整或包含额外字段。", exit_code=1)
            relative = str(file_entry.get("path") or "")
            output_id = str(file_entry.get("output_id") or "")
            if (
                output_id != f"{entry_id}:{relative}"
                or not _SHA256.fullmatch(str(file_entry.get("sha256") or ""))
                or output_id in seen_outputs
                or relative in task_paths
                or (previous_path and relative <= previous_path)
            ):
                raise SdlcError("任务交付物索引的文件身份、排序或哈希不合法。", exit_code=1)
            if verify_files:
                normalized, target = _normalized_project_file(paths, relative)
                if normalized != relative or sha256_file(target) != file_entry.get("sha256"):
                    raise SdlcError(f"任务交付物已经变化：{relative}。", exit_code=1)
            else:
                # 即使调用方暂时不复核文件字节，路径语法也不能因此放宽。
                if not relative or Path(relative).is_absolute() or "\\" in relative or Path(relative).as_posix() != relative or any(part in {".", ".."} for part in Path(relative).parts):
                    raise SdlcError(f"任务交付物路径不合规：{relative}。", exit_code=1)
            seen_outputs.add(output_id)
            task_paths.add(relative)
            previous_path = relative
        if verify_runtime:
            _validate_formal_entry_runtime(paths, requirement, entry)
    return dict(document)


def _validate_formal_entry_runtime(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    entry: Mapping[str, object],
) -> dict[str, object]:
    """逐项核对关闭轮次和当前指针，不能只相信索引里自报的哈希。"""

    task_id = str(entry["task_id"])
    run_number = int(entry["completed_run_number"])
    run_path = paths.root / str(entry["task_run_path"])
    if (
        run_path.is_symlink()
        or not run_path.is_file()
        or sha256_file(run_path) != entry.get("task_run_sha256")
    ):
        raise SdlcError(f"前置任务 {task_id} 的最终关闭轮次已经变化。", exit_code=1)
    try:
        closed_run = json.loads(
            run_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_formal_object,
            parse_constant=_reject_formal_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"前置任务 {task_id} 的最终关闭轮次无法解析。", exit_code=1) from exc
    requirement_root = _formal_requirement_root(paths, requirement)
    current_path = requirement_root / "runtime" / task_id / "current.json"
    task_path = requirement_root / "tasks" / f"{task_id}.json"
    try:
        current = json.loads(
            current_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_formal_object,
            parse_constant=_reject_formal_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"前置任务 {task_id} 的当前关闭指针无法解析。", exit_code=1) from exc
    if (
        not isinstance(closed_run, Mapping)
        or closed_run.get("status") != "closed"
        or closed_run.get("task_id") != task_id
        or closed_run.get("run_number") != run_number
        or closed_run.get("task_sha256") != entry.get("task_sha256")
        or not isinstance(current, Mapping)
        or current.get("status") != "closed"
        or current.get("task_id") != task_id
        or current.get("run_number") != run_number
        or current.get("task_run_sha256") != entry.get("task_run_sha256")
        or task_path.is_symlink()
        or not task_path.is_file()
        or sha256_file(task_path) != entry.get("task_sha256")
    ):
        raise SdlcError(f"前置任务 {task_id} 的关闭轮次、当前指针或任务合同不匹配。", exit_code=1)
    files = entry.get("files")
    assert isinstance(files, list)
    if any(
        not isinstance(item, Mapping)
        or not _path_is_allowed(str(item.get("path") or ""), closed_run.get("allowed_output_paths"))
        for item in files
    ):
        raise SdlcError(f"前置任务 {task_id} 登记了范围外交付物。", exit_code=1)
    return dict(closed_run)


def empty_formal_task_output_index(requirement_id: str) -> dict[str, object]:
    return {
        "schema_version": TASK_OUTPUT_INDEX_SCHEMA,
        "requirement_id": requirement_id,
        "index_id": f"{requirement_id}:{TASK_OUTPUT_INDEX_SCHEMA}",
        "task_outputs": [],
    }


def validate_formal_task_output_index(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    document: Mapping[str, object],
    *,
    verify_files: bool = False,
) -> dict[str, object]:
    """供跨文件事务在落盘前复核暂存文档，避免恢复记录绕过正式读取门禁。"""

    return _validate_formal_index_shape(
        paths,
        requirement,
        document,
        verify_files=verify_files,
        verify_runtime=True,
    )


def load_formal_task_output_index(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    *,
    required: bool = False,
    verify_files: bool = False,
    verify_runtime: bool = True,
) -> dict[str, object]:
    path = formal_task_output_index_path(paths, requirement)
    if not path.exists():
        if required:
            raise SdlcError("缺少正式 task-output-index.v1，不能读取前置任务交付物。", exit_code=1)
        return empty_formal_task_output_index(str(requirement.get("requirement_id") or ""))
    if path.is_symlink() or not path.is_file():
        raise SdlcError("正式任务交付物索引不是普通文件。", exit_code=1)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_formal_object,
            parse_constant=_reject_formal_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("正式任务交付物索引无法解析。", exit_code=1) from exc
    if not isinstance(document, Mapping):
        raise SdlcError("正式任务交付物索引顶层必须是对象。", exit_code=1)
    return _validate_formal_index_shape(
        paths,
        requirement,
        document,
        verify_files=verify_files,
        verify_runtime=verify_runtime,
    )


def formal_index_bytes(document: Mapping[str, object]) -> bytes:
    return canonical_json_text(dict(document)).encode("utf-8")


def formal_index_sha256(document: Mapping[str, object]) -> str:
    return sha256_bytes(formal_index_bytes(document))


def replace_formal_task_output_index(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    document: Mapping[str, object],
) -> Path:
    # 同一路径可以由多个已完成任务分别登记不同字节，写盘时只校验结构和路径语法；
    # 新任务项的真实文件哈希已经在 index_completed_task 中从当前普通文件计算。
    validated = _validate_formal_index_shape(
        paths, requirement, document, verify_files=False
    )
    path = formal_task_output_index_path(paths, requirement)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(formal_index_bytes(validated))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def index_completed_task(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    *,
    task_id: str,
    closed_run: Mapping[str, object],
    actual_changed_files: list[str],
    base_document: Mapping[str, object] | None = None,
) -> dict[str, object]:
    requirement_id = str(requirement.get("requirement_id") or "")
    run_number = closed_run.get("run_number")
    if (
        not _TASK_ID.fullmatch(task_id)
        or closed_run.get("schema_version") != "task-run.v1"
        or closed_run.get("requirement_id") != requirement_id
        or closed_run.get("task_id") != task_id
        or closed_run.get("status") != "closed"
        or not isinstance(run_number, int)
        or run_number < 1
        or not _SHA256.fullmatch(str(closed_run.get("task_sha256") or ""))
    ):
        raise SdlcError("只有身份完整的最终 closed 轮次可以登记交付物。", exit_code=1)
    requirement_root = _formal_requirement_root(paths, requirement)
    task_json = requirement_root / "tasks" / f"{task_id}.json"
    if task_json.is_symlink() or not task_json.is_file() or sha256_file(task_json) != closed_run.get("task_sha256"):
        raise SdlcError("关闭轮次记录的任务合同哈希与正式任务不一致。", exit_code=1)
    entry_id = f"{requirement_id}:{task_id}:run-{run_number:04d}"
    normalized: dict[str, Path] = {}
    for raw_path in actual_changed_files:
        relative, target = _normalized_project_file(paths, raw_path)
        if not _path_is_allowed(relative, closed_run.get("allowed_output_paths")):
            raise SdlcError(f"任务交付物不在该轮次允许范围内：{relative}。", exit_code=1)
        normalized[relative] = target
    run_relative = (
        requirement_root / "runtime" / task_id / "runs" / f"{run_number:04d}" / "task-run.v1.json"
    ).relative_to(paths.root).as_posix()
    entry = {
        "entry_id": entry_id,
        "task_id": task_id,
        "completed_run_number": run_number,
        "task_sha256": str(closed_run["task_sha256"]),
        "task_run_path": run_relative,
        "task_run_sha256": sha256_bytes(canonical_json_text(dict(closed_run)).encode("utf-8")),
        "files": [
            {"output_id": f"{entry_id}:{relative}", "path": relative, "sha256": sha256_file(target)}
            for relative, target in sorted(normalized.items())
        ],
    }
    base = dict(base_document) if base_document is not None else load_formal_task_output_index(paths, requirement)
    _validate_formal_index_shape(
        paths, requirement, base, verify_files=False, verify_runtime=True
    )
    existing = base.get("task_outputs")
    assert isinstance(existing, list)
    result = empty_formal_task_output_index(requirement_id)
    result["task_outputs"] = sorted(
        [dict(item) for item in existing if isinstance(item, Mapping) and item.get("task_id") != task_id] + [entry],
        key=lambda item: str(item["task_id"]),
    )
    return _validate_formal_index_shape(paths, requirement, result, verify_files=False)


def remove_completed_task(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task_id: str,
    *,
    base_document: Mapping[str, object] | None = None,
) -> dict[str, object]:
    base = dict(base_document) if base_document is not None else load_formal_task_output_index(paths, requirement, required=True)
    validated = _validate_formal_index_shape(
        paths, requirement, base, verify_files=False, verify_runtime=True
    )
    outputs = validated["task_outputs"]
    assert isinstance(outputs, list)
    if sum(1 for item in outputs if isinstance(item, Mapping) and item.get("task_id") == task_id) != 1:
        raise SdlcError(f"正式任务交付物索引缺少 {task_id} 的唯一有效任务项。", exit_code=1)
    result = empty_formal_task_output_index(str(requirement.get("requirement_id") or ""))
    result["task_outputs"] = [dict(item) for item in outputs if isinstance(item, Mapping) and item.get("task_id") != task_id]
    return _validate_formal_index_shape(paths, requirement, result, verify_files=False)


def bind_predecessor_outputs(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    manifest: dict[str, object],
    upstream_hashes: dict[str, str],
    *,
    index_document: Mapping[str, object] | None = None,
    allowed_changed_paths: object = None,
) -> tuple[dict[str, object], dict[str, str]]:
    dependencies = sorted({str(item) for item in task.get("depends_on", []) if str(item)})
    if index_document is None:
        index_path = formal_task_output_index_path(paths, requirement)
        if dependencies and not index_path.exists():
            requirement_root = _formal_requirement_root(paths, requirement)
            has_formal_runtime = any(
                (requirement_root / "runtime" / dependency_id / "current.json").exists()
                for dependency_id in dependencies
            )
            if not has_formal_runtime:
                # 旧档案没有 task-run，也就不可能伪造符合 v1 的关闭轮次。这里仅保留原有只读结果，
                # 一旦任一前置任务进入正式运行主线，缺索引就会按新合同明确拒绝。
                legacy_records = manifest.get("predecessor_outputs")
                if not isinstance(legacy_records, list):
                    raise SdlcError("旧任务交付物读取结果不是数组。", exit_code=1)
                return dict(manifest), dict(upstream_hashes)
        document = load_formal_task_output_index(paths, requirement, required=bool(dependencies))
        index_hash = sha256_file(index_path) if index_path.is_file() and not index_path.is_symlink() else formal_index_sha256(document)
    else:
        document = _validate_formal_index_shape(
            paths,
            requirement,
            index_document,
            verify_files=False,
            verify_runtime=True,
        )
        index_hash = formal_index_sha256(document)
    entries = document["task_outputs"]
    assert isinstance(entries, list)
    by_task = {str(item["task_id"]): item for item in entries if isinstance(item, Mapping)}
    records: list[dict[str, object]] = []
    for dependency_id in dependencies:
        entry = by_task.get(dependency_id)
        if not isinstance(entry, Mapping):
            raise SdlcError(f"正式任务交付物索引缺少前置任务 {dependency_id}。", exit_code=1)
        run_path = paths.root / str(entry["task_run_path"])
        if run_path.is_symlink() or not run_path.is_file() or sha256_file(run_path) != entry.get("task_run_sha256"):
            raise SdlcError(f"前置任务 {dependency_id} 的最终关闭轮次已经变化。", exit_code=1)
        try:
            closed_run = json.loads(run_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_formal_object, parse_constant=_reject_formal_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SdlcError(f"前置任务 {dependency_id} 的最终关闭轮次无法解析。", exit_code=1) from exc
        if (
            not isinstance(closed_run, Mapping)
            or closed_run.get("status") != "closed"
            or closed_run.get("task_id") != dependency_id
            or closed_run.get("run_number") != entry.get("completed_run_number")
            or closed_run.get("task_sha256") != entry.get("task_sha256")
        ):
            raise SdlcError(f"前置任务 {dependency_id} 的最终关闭轮次不匹配。", exit_code=1)
        requirement_root = _formal_requirement_root(paths, requirement)
        current_path = requirement_root / "runtime" / dependency_id / "current.json"
        task_path = requirement_root / "tasks" / f"{dependency_id}.json"
        try:
            current = json.loads(
                current_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_formal_object,
                parse_constant=_reject_formal_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SdlcError(f"前置任务 {dependency_id} 的当前关闭指针无法解析。", exit_code=1) from exc
        if (
            not isinstance(current, Mapping)
            or current.get("status") != "closed"
            or current.get("task_id") != dependency_id
            or current.get("run_number") != entry.get("completed_run_number")
            or current.get("task_run_sha256") != entry.get("task_run_sha256")
            or task_path.is_symlink()
            or not task_path.is_file()
            or sha256_file(task_path) != entry.get("task_sha256")
        ):
            raise SdlcError(f"前置任务 {dependency_id} 的当前指针或任务合同不匹配。", exit_code=1)
        for file_entry in entry["files"]:
            assert isinstance(file_entry, Mapping)
            relative = str(file_entry["path"])
            if not _path_is_allowed(relative, closed_run.get("allowed_output_paths")):
                raise SdlcError(f"前置任务 {dependency_id} 登记了范围外交付物。", exit_code=1)
            normalized, target = _normalized_project_file(paths, relative)
            if normalized != relative:
                raise SdlcError(f"任务交付物路径不合规：{relative}。", exit_code=1)
            if (
                sha256_file(target) != file_entry.get("sha256")
                and not _path_is_allowed(relative, allowed_changed_paths)
            ):
                raise SdlcError(f"任务交付物已经变化：{relative}。", exit_code=1)
            records.append({
                "task_id": dependency_id,
                "path": relative,
                "locator": {"kind": "whole_file"},
                "sha256": str(file_entry["sha256"]),
            })
    updated_manifest = dict(manifest)
    updated_manifest["predecessor_outputs"] = sorted(records, key=lambda item: (str(item["task_id"]), str(item["path"])))
    updated_hashes = dict(upstream_hashes)
    updated_hashes["predecessor_outputs"] = index_hash
    return updated_manifest, updated_hashes


def task_outputs_dir(paths: ProjectPaths, requirement: dict[str, Any]) -> Path:
    return requirement_dir(paths, requirement) / TASK_OUTPUT_DIR


def task_output_index_json_path(paths: ProjectPaths, requirement: dict[str, Any]) -> Path:
    return task_outputs_dir(paths, requirement) / "index.json"


def task_output_index_md_path(paths: ProjectPaths, requirement: dict[str, Any]) -> Path:
    return task_outputs_dir(paths, requirement) / "index.md"


def next_output_id(existing: list[dict[str, Any]]) -> str:
    numbers: list[int] = []
    for item in existing:
        raw = str(item.get("output_id", ""))
        if raw.startswith("OUT-") and raw[4:].isdigit():
            numbers.append(int(raw[4:]))
    return f"OUT-{(max(numbers) if numbers else 0) + 1:03d}"


def read_task_output_index(paths: ProjectPaths, requirement: dict[str, Any]) -> list[dict[str, Any]]:
    path = task_output_index_json_path(paths, requirement)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        outputs = data.get("outputs", [])
        return [item for item in outputs if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def write_task_output_index(paths: ProjectPaths, requirement: dict[str, Any], outputs: list[dict[str, Any]]) -> None:
    directory = task_outputs_dir(paths, requirement)
    directory.mkdir(parents=True, exist_ok=True)
    index_data = {
        "requirement_id": requirement.get("requirement_id", ""),
        "updated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "outputs": outputs,
    }
    task_output_index_json_path(paths, requirement).write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 任务产出索引",
        "",
        f"- 需求：{requirement.get('requirement_id', '')} {requirement.get('title', '')}",
        f"- 产出数量：{len(outputs)}",
        "",
        "## 产出列表",
    ]
    if not outputs:
        lines.append("- 暂无任务产出。")
    for item in outputs:
        files = "、".join(clean_items(item.get("files", []))) or "未记录"
        lines.append(
            f"- {item.get('output_id')}：{item.get('title')}；来源任务：{item.get('source_task_id')}；文件：{files}"
        )
    task_output_index_md_path(paths, requirement).write_text(join_lines(lines), encoding="utf-8")


def is_process_artifact(path: str) -> bool:
    normalized = path.strip()
    return normalized.startswith(".codex-sdlc/")


def normalize_changed_files(files: list[str]) -> list[str]:
    result: list[str] = []
    for raw in files:
        item = str(raw).strip().lstrip("./")
        if not item or Path(item).is_absolute() or is_process_artifact(item):
            continue
        result.append(item)
    return clean_items(result)


def task_output_markdown_lines(output: dict[str, Any]) -> list[str]:
    files = clean_items(output.get("files", []))
    symbols = clean_items(output.get("symbols", []))
    replaces = clean_items(output.get("replaces", []))
    coverage_points = clean_items(output.get("coverage_points", []))
    coverage_tests = clean_items(output.get("coverage_tests", []))
    search_terms = clean_items(output.get("search_terms", []))
    freshness_files = [
        str(item.get("path", ""))
        for item in output.get("freshness_files", [])
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    ]
    return [
        f"# {output['output_id']} SDLC 精简产出",
        "",
        "> 这份文件供后续任务通过 task-run 读取清单匹配上游产出，不是给用户阅读当前任务改动的主说明。任务改动请查看 `task-change-reports/T-xxx.md`。",
        "",
        "## 基本信息",
        f"- 来源任务：{output.get('source_task_id', '')}",
        f"- 标题：{output.get('title', '')}",
        f"- 类型：{output.get('type', '')}",
        f"- 状态：{output.get('status', '')}",
        "",
        "## 新增能力",
        f"- {output.get('new_capability', '')}",
        "",
        "## 替代旧写法",
        *([f"- {item}" for item in replaces] if replaces else ["- 未记录明确替代项"]),
        "",
        "## 适用场景",
        f"- {output.get('applies_to', '') or '后续任务如果命中相关文件、符号或需求点，需要先核对本产出。'}",
        "",
        "## 相关文件",
        *([f"- {item}" for item in files] if files else ["- 未记录"]),
        "",
        "## 关键符号",
        *([f"- {item}" for item in symbols] if symbols else ["- 未记录"]),
        "",
        "## 搜索词",
        *([f"- {item}" for item in search_terms[:24]] if search_terms else ["- 未记录"]),
        "",
        "## 覆盖范围",
        "- 覆盖需求点：" + ("、".join(coverage_points) if coverage_points else "未记录"),
        "- 覆盖测试：" + ("、".join(coverage_tests) if coverage_tests else "未记录"),
        "",
        "## 使用规则",
        "- 后续任务命中本产出时，先读取相关文件，再决定是否复用或调整。",
        "- 本产出不是文件白名单；如果任务需要，仍然要继续搜索真实代码。",
        "- 如果相关文件已经变化，先复核现状，不要只按旧结论开发。",
        "",
        "## 新鲜度关联文件",
        *([f"- {item}" for item in freshness_files] if freshness_files else ["- 未记录"]),
    ]


def create_task_output_contract(
    paths: ProjectPaths,
    requirement: dict[str, Any],
    task: dict[str, Any],
    *,
    changed_files: list[str],
    change_brief: dict[str, Any] | None,
) -> dict[str, Any] | None:
    files = normalize_changed_files(changed_files)
    if not files:
        return None

    existing = read_task_output_index(paths, requirement)
    output_id = next_output_id(existing)
    # 产出符号和替代关系必须由模型或任务卡显式填写，CLI 不扫描源码和标题猜含义。
    symbols = clean_items(task.get("output_symbols") or task.get("symbols") or [])
    replaces = clean_items(task.get("replaces") or [])
    title = str(task.get("title", "")).strip() or output_id
    summary = str(task.get("summary", "")).strip() or title
    coverage_points = clean_items(task.get("coverage_points", []))
    coverage_tests = clean_items(task.get("coverage_tests", []))
    search_terms = clean_items(task.get("output_search_terms") or [])
    freshness_files = [{"path": item, "hash": file_hash(paths.root / item)} for item in files]
    output = {
        "output_id": output_id,
        "source_task_id": task.get("task_id", ""),
        "title": title,
        "type": "代码产出",
        "status": "active",
        "created_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "new_capability": summary,
        "replaces": replaces,
        "applies_to": clean_items(task.get("applies_to") or []),
        "files": files,
        "symbols": symbols,
        "search_terms": search_terms,
        "discovered_old_entries": replaces,
        "coverage_points": coverage_points,
        "coverage_tests": coverage_tests,
        "usage_rules": [
            "后续任务命中本产出时，先读取相关文件，再决定是否复用或调整。",
            "本产出不是文件白名单，任务执行时仍可继续搜索真实代码。",
        ],
        "scope_limits": ["只代表来源任务完成时的代码现状。"],
        "freshness_files": freshness_files,
        "change_brief": change_brief.get("path", "") if isinstance(change_brief, dict) else "",
        "fingerprint": short_hash(json.dumps({"task": task.get("task_id"), "files": files, "terms": search_terms}, ensure_ascii=False, sort_keys=True)),
    }

    directory = task_outputs_dir(paths, requirement)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{output_id}.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (directory / f"{output_id}.md").write_text(join_lines(task_output_markdown_lines(output)), encoding="utf-8")
    outputs = [item for item in existing if item.get("output_id") != output_id]
    outputs.append(output)
    write_task_output_index(paths, requirement, outputs)
    return output


def compact_output_match(output: dict[str, Any], score: int, reasons: list[str]) -> dict[str, Any]:
    return {
        "output_id": output.get("output_id", ""),
        "source_task_id": output.get("source_task_id", ""),
        "title": output.get("title", ""),
        "score": score,
        "reasons": reasons,
        "files": clean_items(output.get("files", [])),
        "symbols": clean_items(output.get("symbols", [])),
        "coverage_points": clean_items(output.get("coverage_points", [])),
        "coverage_tests": clean_items(output.get("coverage_tests", [])),
        "status": output.get("status", "active"),
    }


def output_match_key(item: dict[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    output_id = str(item.get("output_id", "")).strip()
    source_task_id = str(item.get("source_task_id", "")).strip()
    title = str(item.get("title", "")).strip()
    files = tuple(clean_items(item.get("files", [])))
    return output_id, source_task_id, title, files


def output_content_fingerprint(item: dict[str, Any]) -> str:
    existing = str(item.get("content_hash") or item.get("fingerprint") or "").strip()
    if existing:
        return existing
    payload = {
        # 重复内容可能由不同任务再次登记；来源任务不同不能掩盖同一份可复用产出。
        "files": sorted(clean_items(item.get("files", []))),
        "symbols": sorted(clean_items(item.get("symbols", []))),
        "coverage_points": sorted(clean_items(item.get("coverage_points", []))),
        "coverage_tests": sorted(clean_items(item.get("coverage_tests", []))),
        "new_capability": str(item.get("new_capability") or "").strip(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def dedupe_output_matches(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for item in items:
        key = output_match_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def dedupe_output_matches_by_content(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for item in items:
        fingerprint = output_content_fingerprint(item)
        if fingerprint in seen:
            duplicates.append(str(item.get("output_id") or "OUT"))
            continue
        seen.add(fingerprint)
        result.append(item)
    return result, duplicates


def direct_output_reasons(output: dict[str, Any], task: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    source_task_id = str(output.get("source_task_id") or "").strip()
    if source_task_id and source_task_id in clean_items(task.get("depends_on", [])):
        reasons.append("直接依赖来源任务")

    explicit_files = [
        *clean_items(task.get("files") or []),
        *clean_items(task.get("file_hints") or []),
        *clean_items(task.get("target_files") or []),
        *clean_items(task.get("related_files") or []),
    ]
    task_files = {Path(item).as_posix().lstrip("./") for item in explicit_files}
    output_files = {Path(item).as_posix().lstrip("./") for item in clean_items(output.get("files", []))}
    if task_files & output_files:
        reasons.append("命中精确文件")

    task_symbols = set(clean_items([*(task.get("symbols") or []), *(task.get("related_symbols") or [])]))
    if task_symbols & set(clean_items(output.get("symbols", []))):
        reasons.append("命中完整符号")

    output_ids = {
        item
        for item in clean_items([*(output.get("coverage_points") or []), *(output.get("coverage_tests") or [])])
        if item.startswith(CONTRACT_ID_PREFIXES)
    }
    task_ids = {
        item
        for item in clean_items(
            [
                *clean_items(task.get("coverage_points") or []),
                *clean_items(task.get("coverage_acceptance") or []),
                *clean_items(task.get("coverage_tests") or []),
                *clean_items(task.get("coverage_change_ids") or []),
            ]
        )
        if item.startswith(CONTRACT_ID_PREFIXES)
    }
    if output_ids & task_ids:
        reasons.append("命中明确合同编号")
    return reasons


def match_upstream_outputs(paths: ProjectPaths, requirement: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    strong: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for output in read_task_output_index(paths, requirement):
        direct_reasons = direct_output_reasons(output, task)
        score = len(direct_reasons) * 100
        item = compact_output_match(output, score, direct_reasons)
        item["content_hash"] = output_content_fingerprint(output)
        if output.get("status") == "active" and direct_reasons:
            strong.append(item)
        elif output.get("status") != "deprecated":
            log.append(item)
    strong.sort(key=lambda item: int(item.get("score", 0)), reverse=True)
    candidates.sort(key=lambda item: int(item.get("score", 0)), reverse=True)
    log.sort(key=lambda item: int(item.get("score", 0)), reverse=True)
    strong, duplicate_strong = dedupe_output_matches_by_content(strong)
    candidates, duplicate_candidates = dedupe_output_matches_by_content(candidates)
    log = dedupe_output_matches(log)
    return {
        "strong": strong[:5],
        "candidates": candidates[:5],
        "log": log[:20],
        "duplicates": duplicate_strong,
        "candidate_duplicates": duplicate_candidates,
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_upstream_match(paths: ProjectPaths, requirement: dict[str, Any], task: dict[str, Any], match: dict[str, Any]) -> Path:
    path = upstream_match_path(paths, requirement, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(match, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def upstream_match_counts(match: dict[str, Any]) -> tuple[int, int]:
    return len(match.get("strong", []) or []), len(match.get("candidates", []) or [])
