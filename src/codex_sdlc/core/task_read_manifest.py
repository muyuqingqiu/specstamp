from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import (
    ProjectPaths,
    requirement_dir_for_id,
    resolve_project_path,
)
from codex_sdlc.core.reference_index import validate_reference_index_file
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)


TASK_READ_MANIFEST_SCHEMA = "task-read-manifest.v1"


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"任务读取清单包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"任务读取清单包含非标准数字：{value}。", exit_code=1)


def _task_contract(task: Mapping[str, object]) -> Mapping[str, object]:
    contract = task.get("task_contract")
    if not isinstance(contract, Mapping):
        raise SdlcError("当前任务缺少正式 task.v2 合同，不能直接开工。", exit_code=1)
    return contract


def _reference_ids(contract: Mapping[str, object]) -> list[str]:
    result: list[str] = []
    for field in ("requirement_refs", "global_rule_refs", "acceptance_refs"):
        result.extend(str(item) for item in contract.get(field, []) if str(item).strip())
    for item in contract.get("technical_solution_refs", []):
        if isinstance(item, Mapping) and str(item.get("reference_key") or "").strip():
            result.append(str(item["reference_key"]))
    for field in ("design_refs", "material_refs"):
        result.extend(str(item) for item in contract.get(field, []) if str(item).strip())
    # 同一引用可以同时承担需求和验收定位；清单只保留一次，读取时不会重复打开文件。
    return list(dict.fromkeys(result))


def _predecessor_outputs(
    paths: ProjectPaths,
    task: Mapping[str, object],
    all_tasks: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    outputs: list[dict[str, object]] = []
    for dependency_id in task.get("depends_on", []):
        dependency = all_tasks.get(str(dependency_id))
        if dependency is None:
            raise SdlcError(f"前置任务不存在或编号不唯一：{dependency_id}。", exit_code=1)
        for raw_path in dependency.get("output_files", []):
            relative_path = str(raw_path).strip()
            if not relative_path:
                continue
            target = resolve_project_path(paths.root, relative_path, must_exist=True)
            if target.is_symlink() or not target.is_file():
                raise SdlcError(f"前置任务交付物不是可读取普通文件：{relative_path}。", exit_code=1)
            outputs.append(
                {
                    "task_id": str(dependency_id),
                    "path": relative_path,
                    "locator": {"kind": "whole_file"},
                    "sha256": sha256_file(target),
                }
            )
    return outputs


def build_task_read_manifest(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    *,
    run_number: int,
    generated_at: str,
    all_tasks: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, str]]:
    """从正式任务和引用索引生成完整路径清单，不复制任何原文或摘要。"""

    requirement_id = str(requirement.get("requirement_id") or "")
    task_id = str(task.get("task_id") or "")
    requirement_root = requirement_dir_for_id(paths, requirement_id)
    if requirement_root is None:
        raise SdlcError(f"找不到正式需求目录：{requirement_id}。", exit_code=1)
    contract = _task_contract(task)
    task_file = Path("tasks") / f"{task_id}.md"
    task_json = Path("tasks") / f"{task_id}.json"
    task_file_path = resolve_project_path(requirement_root, task_file, must_exist=True)
    task_json_path = resolve_project_path(requirement_root, task_json, must_exist=True)
    if any(path.is_symlink() or not path.is_file() for path in (task_file_path, task_json_path)):
        raise SdlcError("正式任务合同必须是需求目录内的普通文件。", exit_code=1)

    index_path = requirement_root / "reference-index.v1.json"
    index = validate_reference_index_file(requirement_root, index_path)
    entries = index.get("entries")
    if not isinstance(entries, Mapping):
        raise SdlcError("正式引用索引缺少 entries。", exit_code=1)
    references: list[dict[str, object]] = []
    requirement_records: list[dict[str, object]] = []
    design_records: list[dict[str, object]] = []
    for reference_id in _reference_ids(contract):
        raw_reference = entries.get(reference_id)
        if not isinstance(raw_reference, Mapping):
            raise SdlcError(f"正式引用索引缺少任务引用：{reference_id}。", exit_code=1)
        record = {
            "id": reference_id,
            "path": str(raw_reference["path"]),
            "locator": deepcopy(raw_reference["locator"]),
            "sha256": str(raw_reference["sha256"]),
        }
        references.append(record)
        if reference_id.startswith(("FR-", "GR-", "AC-")):
            requirement_records.append(record)
        else:
            design_records.append(record)

    predecessor_outputs = _predecessor_outputs(paths, task, all_tasks)
    document: dict[str, object] = {
        "schema_version": TASK_READ_MANIFEST_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": run_number,
        "requirement_root": requirement_root.relative_to(paths.root).as_posix(),
        "task_file": task_file.as_posix(),
        "task_file_sha256": sha256_file(task_file_path),
        "references": references,
        "predecessor_outputs": predecessor_outputs,
        "generated_at": generated_at,
    }
    validate_schema_document(document, schema_name=TASK_READ_MANIFEST_SCHEMA)
    upstream_hashes = {
        "requirement": canonical_sha256(requirement_records),
        "design": canonical_sha256(design_records),
        "reference_index": sha256_file(index_path),
        "dependencies": canonical_sha256(
            [
                {
                    "task_id": dependency_id,
                    "status": all_tasks[str(dependency_id)].get("status"),
                    "contract": all_tasks[str(dependency_id)].get("task_contract"),
                }
                for dependency_id in task.get("depends_on", [])
            ]
        ),
        "predecessor_outputs": canonical_sha256(predecessor_outputs),
    }
    return document, upstream_hashes


def load_task_read_manifest(path: Path) -> dict[str, object]:
    try:
        document = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("任务读取清单无法解析。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("任务读取清单顶层必须是对象。", exit_code=1)
    validate_schema_document(document, schema_name=TASK_READ_MANIFEST_SCHEMA)
    return document


__all__ = [
    "TASK_READ_MANIFEST_SCHEMA",
    "build_task_read_manifest",
    "load_task_read_manifest",
]
