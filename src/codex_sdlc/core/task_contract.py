from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Iterable, Mapping
import uuid

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    TEMPORARY_REFERENCE_PATTERN,
    allocate_stable_ids,
    build_allocation_order,
)
from codex_sdlc.core.project import (
    ProjectPaths,
    ensure_base_dirs,
    project_lock,
    resolve_project_path,
)
from codex_sdlc.core.reference_index import validate_reference_index_file
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)


TASK_PLAN_SCHEMA = "task-plan.v2"
TASK_SCHEMA = "task.v2"
TASK_PLAN_EVENT = "task_plan_imported"
TASK_PLAN_REVISION_EVENT = "task_plan_revised"
TASK_IMPORT_RECEIPT_SCHEMA = "task-import-receipt.v1"
TASK_IMPORT_TRANSACTION_SCHEMA = "task-import-transaction.v1"
TASK_IMPORT_RECEIPT_FILE = ".task-import-receipt.json"

INTERRUPT_AFTER_STAGING = "after_staging"
INTERRUPT_AFTER_PLACEHOLDER_REMOVAL = "after_placeholder_removal"
INTERRUPT_AFTER_DIRECTORY_COMMIT = "after_directory_commit"
INTERRUPT_AFTER_COVERAGE_COMMIT = "after_coverage_commit"
INTERRUPT_AFTER_EVENT_COMMIT = "after_event_commit"

InterruptionHook = Callable[[str], None]

_TASK_FILE_PATTERN = re.compile(
    r"^(?P<client_key>[a-z0-9][a-z0-9._-]{0,127})\.task\.v2\.json$"
)
_TASK_ID_PATTERN = re.compile(r"^T-[0-9]{3,}$")
_REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-[0-9]{3,}$")
_TASK_REFERENCE_WITH_SUFFIX = re.compile(
    r"^@client:(?P<client_key>[a-z0-9][a-z0-9._-]{0,127})(?P<suffix>#[A-Za-z0-9_./-]+)$"
)
_FORMAL_TASK_REFERENCE_WITH_SUFFIX = re.compile(
    r"^(?P<task_id>T-[0-9]{3,})(?P<suffix>#[A-Za-z0-9_./-]+)$"
)
_TASK_REQUIRED_FIELDS = {
    "schema_version",
    "requirement_id",
    "client_key",
    "title",
    "goal",
    "deliverables",
    "depends_on",
    "requirement_refs",
    "global_rule_refs",
    "technical_solution_refs",
    "design_refs",
    "material_refs",
    "change_refs",
    "acceptance_refs",
    "code_scope",
    "implementation_requirements",
    "data_api_page_component_requirements",
    "states_and_exceptions",
    "security_and_privacy",
    "automated_tests",
    "manual_checks",
    "out_of_scope",
    "blocking_conditions",
    "definition_of_done",
}


@dataclass(frozen=True)
class TaskPlanImportResult:
    """任务整包的稳定结果；幂等重试只改变 duplicate。"""

    requirement_id: str
    package_sha256: str
    mapping: dict[str, str]
    duplicate: bool
    task_directory: str
    coverage_file: str
    event_id: str


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"任务合同 JSON 包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"任务合同 JSON 包含非标准数字：{value}。", exit_code=1)


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise SdlcError(f"{label}必须是普通 JSON 文件：{source}。", exit_code=1)
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是合法 JSON：{source.name}。", exit_code=1) from exc
    if not isinstance(value, dict):
        raise SdlcError(f"{label}顶层必须是对象：{source.name}。", exit_code=1)
    canonical_json_bytes(value)
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_bytes(path, canonical_json_text(value).encode("utf-8"))


def _task_transaction_root(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "task-import-transactions"


def _safe_project_path(paths: ProjectPaths, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SdlcError(f"{label}必须是项目相对路径。", exit_code=1)
    result = resolve_project_path(paths.root, value)
    return result


def _validated_requirement_root(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
) -> Path:
    requirement_id = str(requirement.get("requirement_id") or "")
    folder_name = str(requirement.get("folder_name") or "")
    if not _REQUIREMENT_ID_PATTERN.fullmatch(requirement_id) or not folder_name:
        raise SdlcError("当前正式需求缺少稳定编号或目录。", exit_code=1)
    root = paths.requirements_dir / folder_name
    try:
        resolved_root = root.resolve(strict=True)
        resolved_root.relative_to(paths.requirements_dir.resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{requirement_id} 的正式目录不存在或越过项目边界。", exit_code=1) from exc
    if root.is_symlink() or not root.is_dir():
        raise SdlcError(f"{requirement_id} 的正式目录必须是普通目录。", exit_code=1)
    formal = _read_json(root / "original" / "formal.v3.json", label="正式建档清单")
    if (
        formal.get("formal_contract_version") != "formal.v3"
        or formal.get("workflow_profile") != "document-first.v1"
    ):
        raise SdlcError(
            f"{requirement_id} 不是可接收 task-plan.v2 的文档优先正式档案。",
            exit_code=1,
        )
    return root


def _plan_submission(value: Mapping[str, object], requirement_id: str) -> dict[str, object]:
    plan = deepcopy(dict(value))
    validate_schema_document(plan, schema_name=TASK_PLAN_SCHEMA)
    if "mapping" in plan:
        raise SdlcError("task-plan.v2 导入文件不能自报正式任务编号。", exit_code=1)
    if plan.get("requirement_id") != requirement_id:
        raise SdlcError("task-plan.v2 的 requirement_id 与命令目标不一致。", exit_code=1)
    input_hashes = plan.get("input_hashes", {})
    if not isinstance(input_hashes, dict):
        raise SdlcError("task-plan.v2 的 input_hashes 必须是对象。", exit_code=1)
    unsupported = sorted(set(input_hashes) - {"reference_index"})
    if unsupported:
        raise SdlcError(
            "task-plan.v2 包含当前版本不支持的输入哈希："
            + "、".join(unsupported)
            + "。",
            exit_code=1,
        )
    return plan


def _task_submissions(
    tasks_dir: Path,
    requirement_id: str,
) -> dict[str, dict[str, object]]:
    source = Path(tasks_dir)
    if source.is_symlink() or not source.is_dir():
        raise SdlcError("任务目录必须是普通目录。", exit_code=1)
    entries = sorted(source.iterdir(), key=lambda item: item.name)
    if not entries:
        raise SdlcError("任务目录至少需要一份 task.v2 文件。", exit_code=1)
    tasks: dict[str, dict[str, object]] = {}
    for path in entries:
        if path.is_symlink() or not path.is_file():
            raise SdlcError(f"任务目录只能包含普通 task.v2 文件：{path.name}。", exit_code=1)
        match = _TASK_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            raise SdlcError(
                f"任务文件名必须使用 <client_key>.task.v2.json，不能伪装正式编号：{path.name}。",
                exit_code=1,
            )
        document = _read_json(path, label="task.v2")
        missing = sorted(_TASK_REQUIRED_FIELDS - set(document))
        unknown = sorted(set(document) - _TASK_REQUIRED_FIELDS)
        if missing:
            raise SdlcError(
                f"{path.name} 缺少必填字段：{'、'.join(missing)}。",
                exit_code=1,
            )
        if unknown:
            raise SdlcError(
                f"{path.name} 包含未知字段：{'、'.join(unknown)}。",
                exit_code=1,
            )
        validate_schema_document(document, schema_name=TASK_SCHEMA)
        client_key = match.group("client_key")
        if document.get("client_key") != client_key:
            raise SdlcError(f"{path.name} 与 task.v2 的 client_key 不一致。", exit_code=1)
        if document.get("requirement_id") != requirement_id:
            raise SdlcError(f"{path.name} 的 requirement_id 与命令目标不一致。", exit_code=1)
        if client_key in tasks:
            raise SdlcError(f"任务 client_key 重复：{client_key}。", exit_code=1)
        tasks[client_key] = document
    return tasks


def _coverage_submission(value: Mapping[str, object], requirement_id: str) -> dict[str, object]:
    coverage = deepcopy(dict(value))
    required = {
        "schema_version",
        "requirement_id",
        "functional_requirements",
        "design_artifacts",
        "acceptance_criteria",
        "effective_changes",
        "no_development_items",
    }
    missing = sorted(required - set(coverage))
    extra = sorted(set(coverage) - required)
    if missing:
        raise SdlcError("task-coverage.v1 缺少字段：" + "、".join(missing) + "。", exit_code=1)
    if extra:
        raise SdlcError("task-coverage.v1 包含未知字段：" + "、".join(extra) + "。", exit_code=1)
    if coverage.get("schema_version") != "task-coverage.v1":
        raise SdlcError("覆盖文件版本必须是 task-coverage.v1。", exit_code=1)
    if coverage.get("requirement_id") != requirement_id:
        raise SdlcError("task-coverage.v1 的 requirement_id 与命令目标不一致。", exit_code=1)
    for field in (
        "functional_requirements",
        "design_artifacts",
        "acceptance_criteria",
        "effective_changes",
    ):
        if not isinstance(coverage.get(field), dict):
            raise SdlcError(f"task-coverage.v1 的 {field} 必须是对象。", exit_code=1)
    if not isinstance(coverage.get("no_development_items"), list):
        raise SdlcError("task-coverage.v1 的 no_development_items 必须是数组。", exit_code=1)
    canonical_json_bytes(coverage)
    return coverage


def _allocation_objects(
    plan: Mapping[str, object],
    tasks: Mapping[str, Mapping[str, object]],
    existing_task_ids: set[str],
    current_requirement_task_ids: set[str],
) -> tuple[AllocationObject, ...]:
    plan_refs = [str(item) for item in plan["tasks"]]  # type: ignore[index]
    expected_refs = {f"@client:{client_key}" for client_key in tasks}
    if set(plan_refs) != expected_refs or len(plan_refs) != len(tasks):
        raise SdlcError("task-plan.v2 的任务清单与任务目录不完全一致。", exit_code=1)

    objects: list[AllocationObject] = []
    expected_dependencies: set[tuple[str, str]] = set()
    for client_key, task in tasks.items():
        dependency_refs = tuple(str(item) for item in task["depends_on"])  # type: ignore[index]
        internal_dependencies: list[str] = []
        for dependency in dependency_refs:
            if TEMPORARY_REFERENCE_PATTERN.fullmatch(dependency) is not None:
                internal_dependencies.append(dependency)
            elif _TASK_ID_PATTERN.fullmatch(dependency) is not None:
                if dependency in current_requirement_task_ids:
                    raise SdlcError(
                        f"{client_key} 依赖了当前需求上一版任务 {dependency}；"
                        "当前任务包内依赖必须使用 @client 引用。",
                        exit_code=1,
                    )
                if dependency not in existing_task_ids:
                    raise SdlcError(
                        f"{client_key} 依赖的正式任务不存在：{dependency}。",
                        exit_code=1,
                    )
            else:
                raise SdlcError(
                    f"{client_key} 的依赖必须引用当前包内 @client 任务或已有正式任务。",
                    exit_code=1,
                )
            expected_dependencies.add((dependency, f"@client:{client_key}"))
        objects.append(
            AllocationObject(
                client_key=client_key,
                id_prefix="T",
                depends_on=tuple(internal_dependencies),
            )
        )
    order = build_allocation_order(objects)
    expected_order = [f"@client:{item.client_key}" for item in order]
    if plan_refs != expected_order:
        raise SdlcError("task-plan.v2 的 tasks 必须按稳定依赖顺序排列。", exit_code=1)

    plan_dependencies = {
        (str(item["from"]), str(item["to"]))
        for item in plan["dependencies"]  # type: ignore[index]
        if isinstance(item, Mapping)
    }
    if len(plan_dependencies) != len(plan["dependencies"]):  # type: ignore[arg-type,index]
        raise SdlcError("task-plan.v2 包含重复依赖。", exit_code=1)
    if plan_dependencies != expected_dependencies:
        raise SdlcError("task-plan.v2 的 dependencies 与 task.v2 依赖不一致。", exit_code=1)
    return tuple(order)


def _validate_task_references(
    task: Mapping[str, object],
    reference_entries: Mapping[str, object],
    effective_change_ids: set[str],
) -> None:
    task_name = str(task.get("client_key") or task.get("task_id") or "")
    reference_fields = (
        "requirement_refs",
        "global_rule_refs",
        "design_refs",
        "material_refs",
        "acceptance_refs",
    )
    for field in reference_fields:
        for reference_id in task[field]:  # type: ignore[index]
            if str(reference_id) not in reference_entries:
                raise SdlcError(
                    f"{task_name} 引用了不存在的正式引用：{reference_id}。",
                    exit_code=1,
                )
    for reference in task["technical_solution_refs"]:  # type: ignore[index]
        if not isinstance(reference, Mapping):
            raise SdlcError(f"{task_name} 的技术方案引用格式无效。", exit_code=1)
        design_id = str(reference["id"])
        reference_key = str(reference["reference_key"])
        if reference_key != design_id and not reference_key.startswith(f"{design_id}#"):
            raise SdlcError(
                f"{task_name} 的技术方案定位键不属于 {design_id}。",
                exit_code=1,
            )
        if reference_key not in reference_entries:
            raise SdlcError(
                f"{task_name} 引用了不存在的技术方案定位键：{reference_key}。",
                exit_code=1,
            )
    unknown_changes = sorted(set(str(item) for item in task["change_refs"]) - effective_change_ids)  # type: ignore[index]
    if unknown_changes:
        raise SdlcError(
            f"{task_name} 引用了未生效或不存在的变更：{'、'.join(unknown_changes)}。",
            exit_code=1,
        )


def _rewrite_exact_task_reference(
    value: object,
    mapping: Mapping[str, str],
    *,
    allow_suffix: bool = False,
) -> str:
    """只重写合同明确声明为任务引用的字段，普通正文不参与扫描。"""

    if not isinstance(value, str):
        raise SdlcError("任务引用必须是字符串。", exit_code=1)
    exact = TEMPORARY_REFERENCE_PATTERN.fullmatch(value)
    if exact is not None:
        key = exact.group("client_key")
        if key not in mapping:
            raise SdlcError(f"任务临时引用悬空：{value}。", exit_code=1)
        return mapping[key]
    if allow_suffix:
        suffixed = _TASK_REFERENCE_WITH_SUFFIX.fullmatch(value)
        if suffixed is not None:
            key = suffixed.group("client_key")
            if key not in mapping:
                raise SdlcError(f"任务临时引用悬空：{value}。", exit_code=1)
            return mapping[key] + suffixed.group("suffix")
    if "@client:" in value:
        raise SdlcError(f"任务引用格式无效：{value}。", exit_code=1)
    return value


def _rewrite_plan_dependencies(
    dependencies: object,
    mapping: Mapping[str, str],
) -> list[dict[str, str]]:
    if not isinstance(dependencies, list):
        raise SdlcError("task-plan.v2 的 dependencies 必须是数组。", exit_code=1)
    result: list[dict[str, str]] = []
    for dependency in dependencies:
        if not isinstance(dependency, Mapping):
            raise SdlcError("task-plan.v2 的依赖项必须是对象。", exit_code=1)
        result.append(
            {
                "from": _rewrite_exact_task_reference(dependency.get("from"), mapping),
                "to": _rewrite_exact_task_reference(dependency.get("to"), mapping),
            }
        )
    return result


def _rewrite_coverage_task_references(
    value: object,
    mapping: Mapping[str, str],
) -> object:
    """覆盖矩阵只改写任务编号数组和测试定位数组。"""

    if isinstance(value, list):
        return [_rewrite_coverage_task_references(item, mapping) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result: dict[str, object] = {}
    for key, item in value.items():
        if key in {"tasks", "test_refs"}:
            if not isinstance(item, list):
                raise SdlcError(f"task-coverage.v1 的 {key} 必须是数组。", exit_code=1)
            result[key] = [
                _rewrite_exact_task_reference(
                    reference,
                    mapping,
                    allow_suffix=key == "test_refs",
                )
                for reference in item
            ]
        else:
            result[key] = _rewrite_coverage_task_references(item, mapping)
    return result


def _validate_coverage_task_refs(
    coverage: Mapping[str, object],
    task_ids: set[str],
) -> None:
    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "tasks":
                    if not isinstance(item, list):
                        raise SdlcError(f"{path}/tasks 必须是任务编号数组。", exit_code=1)
                    unknown = sorted(
                        str(task_id)
                        for task_id in item
                        if str(task_id) not in task_ids
                    )
                    if unknown:
                        raise SdlcError(
                            "覆盖文件引用了计划外任务：" + "、".join(unknown) + "。",
                            exit_code=1,
                        )
                walk(item, f"{path}/{key}")
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}/{index}")
            return
        if isinstance(value, str):
            match = _FORMAL_TASK_REFERENCE_WITH_SUFFIX.fullmatch(value)
            if match is not None and match.group("task_id") not in task_ids:
                raise SdlcError(f"覆盖文件测试引用了计划外任务：{value}。", exit_code=1)

    walk(coverage, "")


def _coverage_task_ids_from_plan(plan: Mapping[str, object]) -> set[str]:
    """覆盖矩阵可引用本包任务，也可引用计划依赖中已核对存在的正式前置任务。"""

    task_ids = {str(item) for item in plan["tasks"]}  # type: ignore[index]
    for dependency in plan["dependencies"]:  # type: ignore[index]
        if not isinstance(dependency, Mapping):
            raise SdlcError("task-plan.v2 的依赖项必须是对象。", exit_code=1)
        source_task_id = str(dependency.get("from") or "")
        if _TASK_ID_PATTERN.fullmatch(source_task_id):
            task_ids.add(source_task_id)
    return task_ids


def _record_documents(
    *,
    plan: Mapping[str, object],
    tasks: Mapping[str, Mapping[str, object]],
    coverage: Mapping[str, object],
    mapping: Mapping[str, str],
    reference_index_sha256: str,
    submission_sha256: str,
    producer_run_id_override: str = "",
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    producer_run_id = str(plan.get("producer_run_id") or "").strip()
    if not producer_run_id:
        producer_run_id = producer_run_id_override.strip()
    if not producer_run_id:
        producer_run_id = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not producer_run_id:
        producer_run_id = f"tasks-{submission_sha256[:16]}"
    input_hashes = dict(plan.get("input_hashes") or {})
    supplied_reference_hash = str(input_hashes.get("reference_index") or "")
    if supplied_reference_hash and not hmac.compare_digest(
        supplied_reference_hash, reference_index_sha256
    ):
        raise SdlcError("task-plan.v2 的正式引用索引哈希与当前文件不一致。", exit_code=1)
    input_hashes["reference_index"] = reference_index_sha256

    record_plan: dict[str, object] = {
        "schema_version": TASK_PLAN_SCHEMA,
        "requirement_id": str(plan["requirement_id"]),
        "producer_run_id": producer_run_id,
        "input_hashes": input_hashes,
        "tasks": [
            _rewrite_exact_task_reference(item, mapping)
            for item in plan["tasks"]  # type: ignore[index]
        ],
        "dependencies": _rewrite_plan_dependencies(plan["dependencies"], mapping),  # type: ignore[index]
        "mapping": dict(mapping),
    }
    if "code_evidence" in plan:
        record_plan["code_evidence"] = deepcopy(plan["code_evidence"])
    validate_schema_document(record_plan, schema_name=TASK_PLAN_SCHEMA)

    record_tasks: list[dict[str, object]] = []
    for client_ref in plan["tasks"]:  # type: ignore[index]
        match = TEMPORARY_REFERENCE_PATTERN.fullmatch(str(client_ref))
        client_key = match.group("client_key") if match is not None else ""
        source_task = tasks[client_key]
        record_task = deepcopy(dict(source_task))
        record_task.pop("client_key", None)
        record_task["task_id"] = mapping[client_key]
        record_task["depends_on"] = [
            _rewrite_exact_task_reference(item, mapping)
            for item in record_task["depends_on"]  # type: ignore[index]
        ]
        if not isinstance(record_task, dict):
            raise SdlcError("映射后的 task.v2 必须是对象。", exit_code=1)
        validate_schema_document(record_task, schema_name=TASK_SCHEMA)
        record_tasks.append(record_task)

    record_coverage = _rewrite_coverage_task_references(
        deepcopy(dict(coverage)),
        mapping,
    )
    if not isinstance(record_coverage, dict):
        raise SdlcError("映射后的 task-coverage.v1 必须是对象。", exit_code=1)
    _validate_coverage_task_refs(
        record_coverage,
        _coverage_task_ids_from_plan(record_plan),
    )
    return record_plan, record_tasks, record_coverage


def _formal_package_sha256(
    *,
    plan: Mapping[str, object],
    tasks: Iterable[Mapping[str, object]],
    coverage: Mapping[str, object],
    reference_index_sha256: str,
) -> str:
    """从持久化计划、任务、覆盖、映射和引用索引重算正式整包摘要。"""

    return canonical_sha256(
        {
            "schema_version": "task-import-formal-package.v1",
            "requirement_id": plan.get("requirement_id"),
            "reference_index_sha256": reference_index_sha256,
            "mapping": deepcopy(plan.get("mapping")),
            "task_plan": deepcopy(dict(plan)),
            "tasks": [deepcopy(dict(task)) for task in tasks],
            "task_coverage": deepcopy(dict(coverage)),
        }
    )


def _markdown_items(values: Iterable[object], *, empty: str = "无") -> list[str]:
    items = [str(item) for item in values]
    return [f"- {item}" for item in items] if items else [f"- {empty}"]


def task_markdown(task: Mapping[str, object]) -> str:
    """生成完整任务投影，不摘要、不截断任何合同字段。"""

    record = deepcopy(dict(task))
    validate_schema_document(record, schema_name=TASK_SCHEMA)
    if "task_id" not in record:
        raise SdlcError("只有分配正式编号后的 task.v2 才能生成 Markdown。", exit_code=1)
    technical = [
        f"{item['id']}（{item['reference_key']}）"
        for item in record["technical_solution_refs"]  # type: ignore[index]
    ]
    scope = record["code_scope"]
    if not isinstance(scope, Mapping):
        raise SdlcError("task.v2 的 code_scope 必须是对象。", exit_code=1)
    sections: list[tuple[str, Iterable[object], str]] = [
        ("交付结果", record["deliverables"], "无"),  # type: ignore[arg-type,index]
        ("前置任务", record["depends_on"], "无"),  # type: ignore[arg-type,index]
        ("涉及的小需求", record["requirement_refs"], "无"),  # type: ignore[arg-type,index]
        ("涉及的全局规则", record["global_rule_refs"], "无"),  # type: ignore[arg-type,index]
        ("涉及的技术方案", technical, "无"),
        ("涉及的设计产物", record["design_refs"], "无"),  # type: ignore[arg-type,index]
        ("涉及的资料", record["material_refs"], "无"),  # type: ignore[arg-type,index]
        ("涉及的变更", record["change_refs"], "无"),  # type: ignore[arg-type,index]
        ("涉及的验收标准", record["acceptance_refs"], "无"),  # type: ignore[arg-type,index]
        ("实现要求", record["implementation_requirements"], "无"),  # type: ignore[arg-type,index]
        (
            "数据、接口、页面和组件要求",
            record["data_api_page_component_requirements"],  # type: ignore[arg-type,index]
            "无",
        ),
        ("状态和异常处理", record["states_and_exceptions"], "无"),  # type: ignore[arg-type,index]
        ("安全和隐私要求", record["security_and_privacy"], "无"),  # type: ignore[arg-type,index]
        ("自动测试", record["automated_tests"], "无"),  # type: ignore[arg-type,index]
        ("人工验收点", record["manual_checks"], "无"),  # type: ignore[arg-type,index]
        ("不包含内容", record["out_of_scope"], "无"),  # type: ignore[arg-type,index]
        ("阻塞条件", record["blocking_conditions"], "无阻塞条件"),  # type: ignore[arg-type,index]
        ("完成标准", record["definition_of_done"], "无"),  # type: ignore[arg-type,index]
    ]
    lines = [
        f"# {record['task_id']}：{record['title']}",
        "",
        "## 任务信息",
        "",
        f"- 需求编号：{record['requirement_id']}",
        f"- 任务编号：{record['task_id']}",
        "",
        "## 任务目标",
        "",
        str(record["goal"]),
        "",
        "## 代码和模块范围",
        "",
        "### 读取路径",
        "",
        *_markdown_items(scope["read_paths"]),  # type: ignore[index]
        "",
        "### 预计修改路径",
        "",
        *_markdown_items(scope["likely_change_paths"]),  # type: ignore[index]
        "",
        "### 保护路径",
        "",
        *_markdown_items(scope["protected_paths"]),  # type: ignore[index]
        "",
    ]
    for title, values, empty in sections:
        lines.extend([f"## {title}", "", *_markdown_items(values, empty=empty), ""])
    return "\n".join(lines).rstrip() + "\n"


def task_plan_markdown(
    plan: Mapping[str, object],
    tasks: Iterable[Mapping[str, object]],
) -> str:
    record_tasks = list(tasks)
    by_id = {str(task["task_id"]): task for task in record_tasks}
    lines = [
        f"# {plan['requirement_id']} 任务总览",
        "",
        f"- 任务数量：{len(record_tasks)}",
        f"- 正式引用索引哈希：{plan['input_hashes']['reference_index']}",  # type: ignore[index]
        "",
        "## 任务顺序",
        "",
    ]
    for task_id in plan["tasks"]:  # type: ignore[index]
        task = by_id[str(task_id)]
        lines.extend(
            [
                f"### {task_id}：{task['title']}",
                "",
                str(task["goal"]),
                "",
                f"- 前置任务：{'、'.join(task['depends_on']) or '无'}",
                "",
            ]
        )
    lines.extend(["## 依赖关系", ""])
    dependencies = list(plan["dependencies"])  # type: ignore[index]
    if dependencies:
        lines.extend(
            f"- {item['from']} → {item['to']}"
            for item in dependencies
            if isinstance(item, Mapping)
        )
    else:
        lines.append("- 无")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_files(
    plan: Mapping[str, object],
    tasks: list[Mapping[str, object]],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {
        "task-plan.v2.json": canonical_json_text(plan).encode("utf-8"),
        "任务总览.md": task_plan_markdown(plan, tasks).encode("utf-8"),
    }
    for task in tasks:
        task_id = str(task["task_id"])
        files[f"{task_id}.json"] = canonical_json_text(task).encode("utf-8")
        files[f"{task_id}.md"] = task_markdown(task).encode("utf-8")
    return files


def _build_receipt(
    *,
    requirement_id: str,
    package_sha256: str,
    reference_index_sha256: str,
    mapping: Mapping[str, str],
    event_id: str,
    files: Mapping[str, bytes],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": TASK_IMPORT_RECEIPT_SCHEMA,
        "requirement_id": requirement_id,
        "package_sha256": package_sha256,
        "reference_index_sha256": reference_index_sha256,
        "mapping": dict(mapping),
        "event_id": event_id,
        "files": {
            relative_path: sha256_bytes(content)
            for relative_path, content in sorted(files.items())
        },
        "coverage_sha256": sha256_bytes(canonical_json_bytes(coverage)),
    }


def _validate_receipt(value: Mapping[str, object]) -> dict[str, object]:
    receipt = deepcopy(dict(value))
    expected_fields = {
        "schema_version",
        "requirement_id",
        "package_sha256",
        "reference_index_sha256",
        "mapping",
        "event_id",
        "files",
        "coverage_sha256",
    }
    if set(receipt) != expected_fields or receipt.get("schema_version") != TASK_IMPORT_RECEIPT_SCHEMA:
        raise SdlcError("任务导入回执结构不完整。", exit_code=1)
    for field in ("package_sha256", "reference_index_sha256", "coverage_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field) or "")) is None:
            raise SdlcError(f"任务导入回执的 {field} 无效。", exit_code=1)
    mapping = receipt.get("mapping")
    files = receipt.get("files")
    if (
        not isinstance(mapping, dict)
        or not mapping
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or not _TASK_ID_PATTERN.fullmatch(value)
            for key, value in mapping.items()
        )
        or not isinstance(files, dict)
        or any(
            not isinstance(key, str)
            or re.fullmatch(r"[0-9a-f]{64}", str(value)) is None
            for key, value in files.items()
        )
    ):
        raise SdlcError("任务导入回执缺少有效映射或文件哈希。", exit_code=1)
    return receipt


def _write_staging_bundle(
    task_directory: Path,
    files: Mapping[str, bytes],
    receipt: Mapping[str, object],
) -> None:
    task_directory.mkdir(parents=True, exist_ok=False)
    try:
        for relative_path, content in sorted(files.items()):
            target = task_directory / relative_path
            if target.parent != task_directory:
                raise SdlcError("任务正式文件必须直接位于 tasks 目录。", exit_code=1)
            _write_bytes(target, content)
        _write_bytes(
            task_directory / TASK_IMPORT_RECEIPT_FILE,
            canonical_json_text(receipt).encode("utf-8"),
        )
        _fsync_directory(task_directory)
        _fsync_directory(task_directory.parent)
    except Exception:
        shutil.rmtree(task_directory, ignore_errors=True)
        raise


def _validate_committed_directory(
    task_directory: Path,
    *,
    expected_receipt: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if task_directory.is_symlink() or not task_directory.is_dir():
        raise SdlcError("任务正式目录不存在或不是普通目录。", exit_code=1)
    receipt = _validate_receipt(
        _read_json(task_directory / TASK_IMPORT_RECEIPT_FILE, label="任务导入回执")
    )
    if expected_receipt is not None and canonical_sha256(receipt) != canonical_sha256(
        expected_receipt
    ):
        raise SdlcError("任务正式目录与待恢复事务不一致。", exit_code=1)
    file_hashes = receipt["files"]
    if not isinstance(file_hashes, dict):
        raise SdlcError("任务导入回执缺少文件哈希。", exit_code=1)
    actual_names = {
        path.name
        for path in task_directory.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    expected_names = set(file_hashes) | {TASK_IMPORT_RECEIPT_FILE}
    if actual_names != expected_names or any(
        path.is_symlink() or not path.is_file() for path in task_directory.iterdir()
    ):
        raise SdlcError("任务正式目录文件集合与回执不一致。", exit_code=1)
    for relative_path, expected_hash in file_hashes.items():
        actual_hash = sha256_file(task_directory / str(relative_path))
        if not hmac.compare_digest(actual_hash, str(expected_hash)):
            raise SdlcError(f"任务正式文件哈希漂移：{relative_path}。", exit_code=1)
    return receipt


def _ensure_coverage(path: Path, coverage: Mapping[str, object], expected_hash: str) -> None:
    content = canonical_json_text(coverage).encode("utf-8")
    actual_hash = sha256_bytes(canonical_json_bytes(coverage))
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise SdlcError("任务覆盖文件与导入回执哈希不一致。", exit_code=1)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise SdlcError("现有 task-coverage.v1.json 与任务导入包冲突。", exit_code=1)
        return
    _atomic_write_bytes(path, content)


def _ensure_event(paths: ProjectPaths, event: Mapping[str, object]) -> None:
    from codex_sdlc.core.state import load_events

    events = load_events(paths)
    matches = [item for item in events if item.get("event_id") == event.get("event_id")]
    if matches:
        if len(matches) != 1 or matches[0] != event:
            raise SdlcError(f"任务导入事件编号冲突：{event.get('event_id')}。", exit_code=1)
        return
    current = paths.events_file.read_bytes() if paths.events_file.exists() else b""
    separator = b"\n" if current and not current.endswith(b"\n") else b""
    line = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(paths.events_file, current + separator + line)


def _event_for_import(
    *,
    paths: ProjectPaths,
    event_id: str,
    requirement_id: str,
    package_sha256: str,
    reference_index_sha256: str,
    mapping: Mapping[str, str],
    plan: Mapping[str, object],
    tasks: list[Mapping[str, object]],
    coverage: Mapping[str, object],
    receipt: Mapping[str, object],
    event_type: str = TASK_PLAN_EVENT,
    previous_package_sha256: str = "",
) -> dict[str, object]:
    from codex_sdlc.core.state import now_iso

    if event_type not in {TASK_PLAN_EVENT, TASK_PLAN_REVISION_EVENT}:
        raise SdlcError("任务计划事件类型无效。", exit_code=1)
    is_revision = event_type == TASK_PLAN_REVISION_EVENT
    if is_revision != bool(previous_package_sha256):
        raise SdlcError("任务计划修订事件缺少上一版整包摘要。", exit_code=1)
    payload = {
        "package_sha256": package_sha256,
        "reference_index_sha256": reference_index_sha256,
        "mapping": dict(mapping),
        "task_plan": deepcopy(dict(plan)),
        "tasks": [deepcopy(dict(task)) for task in tasks],
        "task_coverage": deepcopy(dict(coverage)),
        "files": deepcopy(receipt["files"]),
        "coverage_sha256": receipt["coverage_sha256"],
    }
    if is_revision:
        payload["previous_package_sha256"] = previous_package_sha256
    return {
        "event_id": event_id,
        "event_type": event_type,
        "project_path": str(paths.root),
        "requirement_id": requirement_id,
        "task_id": None,
        "created_at": now_iso(),
        "source": "sdlc-tasks",
        "summary": (
            f"修订正式任务计划 {requirement_id}"
            if is_revision
            else f"导入正式任务计划 {requirement_id}"
        ),
        "payload": payload,
    }


def _validate_import_event(
    event: Mapping[str, object],
    receipt: Mapping[str, object],
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    if (
        event.get("event_type") not in {TASK_PLAN_EVENT, TASK_PLAN_REVISION_EVENT}
        or event.get("requirement_id") != receipt.get("requirement_id")
        or event.get("event_id") != receipt.get("event_id")
        or event.get("source") != "sdlc-tasks"
    ):
        raise SdlcError("任务导入事件身份与回执不一致。", exit_code=1)
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError("任务导入事件缺少结构化正文。", exit_code=1)
    previous_package_sha256 = payload.get("previous_package_sha256")
    if event.get("event_type") == TASK_PLAN_REVISION_EVENT:
        if re.fullmatch(r"[0-9a-f]{64}", str(previous_package_sha256 or "")) is None:
            raise SdlcError("任务计划修订事件缺少有效的上一版整包摘要。", exit_code=1)
    elif previous_package_sha256 is not None:
        raise SdlcError("首次任务导入事件不能记录上一版整包摘要。", exit_code=1)
    if (
        payload.get("package_sha256") != receipt.get("package_sha256")
        or payload.get("reference_index_sha256")
        != receipt.get("reference_index_sha256")
        or payload.get("mapping") != receipt.get("mapping")
        or payload.get("files") != receipt.get("files")
        or payload.get("coverage_sha256") != receipt.get("coverage_sha256")
    ):
        raise SdlcError("任务导入事件摘要与回执不一致。", exit_code=1)
    plan = payload.get("task_plan")
    tasks = payload.get("tasks")
    coverage = payload.get("task_coverage")
    if not isinstance(plan, dict) or not isinstance(tasks, list) or not isinstance(coverage, dict):
        raise SdlcError("任务导入事件缺少计划、任务或覆盖文件。", exit_code=1)
    validate_schema_document(plan, schema_name=TASK_PLAN_SCHEMA)
    normalized_tasks: list[dict[str, object]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise SdlcError("任务导入事件中的 task.v2 必须是对象。", exit_code=1)
        validate_schema_document(task, schema_name=TASK_SCHEMA)
        normalized_tasks.append(deepcopy(task))
    if [str(task["task_id"]) for task in normalized_tasks] != list(plan["tasks"]):
        raise SdlcError("任务导入事件的计划顺序与任务文件不一致。", exit_code=1)
    mapping = receipt.get("mapping")
    if (
        plan.get("mapping") != mapping
        or not isinstance(mapping, dict)
        or set(str(value) for value in mapping.values()) != set(plan["tasks"])
    ):
        raise SdlcError("任务导入事件的编号映射与计划不一致。", exit_code=1)
    expected_file_hashes = {
        name: sha256_bytes(content)
        for name, content in _bundle_files(plan, normalized_tasks).items()
    }
    if expected_file_hashes != receipt.get("files"):
        raise SdlcError("任务导入事件正文与正式文件哈希不一致。", exit_code=1)
    if sha256_bytes(canonical_json_bytes(coverage)) != receipt.get("coverage_sha256"):
        raise SdlcError("任务导入事件中的覆盖文件哈希不一致。", exit_code=1)
    formal_package_sha256 = _formal_package_sha256(
        plan=plan,
        tasks=normalized_tasks,
        coverage=coverage,
        reference_index_sha256=str(receipt["reference_index_sha256"]),
    )
    if not hmac.compare_digest(
        formal_package_sha256,
        str(receipt["package_sha256"]),
    ):
        raise SdlcError("任务正式整包摘要与计划、任务和覆盖正文不一致。", exit_code=1)
    _validate_coverage_task_refs(
        coverage,
        _coverage_task_ids_from_plan(plan),
    )
    return deepcopy(plan), normalized_tasks, deepcopy(coverage)


def _journal_document(
    *,
    paths: ProjectPaths,
    transaction_id: str,
    staging_directory: Path,
    task_directory: Path,
    coverage_file: Path,
    had_empty_placeholder: bool,
    receipt: Mapping[str, object],
    event: Mapping[str, object],
    mode: str = "import",
    backup_directory: Path | None = None,
    backup_coverage_file: Path | None = None,
    previous_receipt: Mapping[str, object] | None = None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": TASK_IMPORT_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id,
        "staging_directory": staging_directory.relative_to(paths.root).as_posix(),
        "task_directory": task_directory.relative_to(paths.root).as_posix(),
        "coverage_file": coverage_file.relative_to(paths.root).as_posix(),
        "had_empty_placeholder": had_empty_placeholder,
        "receipt": deepcopy(dict(receipt)),
        "event": deepcopy(dict(event)),
    }
    if mode == "revision":
        if (
            backup_directory is None
            or backup_coverage_file is None
            or previous_receipt is None
        ):
            raise SdlcError("任务修订事务缺少上一版恢复信息。", exit_code=1)
        document.update(
            {
                "mode": "revision",
                "backup_directory": backup_directory.relative_to(
                    paths.root
                ).as_posix(),
                "backup_coverage_file": backup_coverage_file.relative_to(
                    paths.root
                ).as_posix(),
                "previous_receipt": deepcopy(dict(previous_receipt)),
            }
        )
    elif mode != "import":
        raise SdlcError("任务导入事务模式无效。", exit_code=1)
    return document


def _read_journal(path: Path) -> dict[str, object]:
    journal = _read_json(path, label="任务导入事务")
    expected = {
        "schema_version",
        "transaction_id",
        "staging_directory",
        "task_directory",
        "coverage_file",
        "had_empty_placeholder",
        "receipt",
        "event",
    }
    revision_fields = {
        "mode",
        "backup_directory",
        "backup_coverage_file",
        "previous_receipt",
    }
    is_revision = set(journal) == expected | revision_fields
    if (
        set(journal) != expected
        and not is_revision
    ) or journal.get("schema_version") != TASK_IMPORT_TRANSACTION_SCHEMA:
        raise SdlcError(f"任务导入事务结构无效：{path.name}。", exit_code=1)
    if not isinstance(journal.get("receipt"), dict) or not isinstance(journal.get("event"), dict):
        raise SdlcError(f"任务导入事务缺少回执或事件：{path.name}。", exit_code=1)
    if not isinstance(journal.get("had_empty_placeholder"), bool):
        raise SdlcError(f"任务导入事务缺少空目录恢复标记：{path.name}。", exit_code=1)
    _validate_receipt(journal["receipt"])
    _validate_import_event(journal["event"], journal["receipt"])
    if is_revision:
        if journal.get("mode") != "revision" or not isinstance(
            journal.get("previous_receipt"), dict
        ):
            raise SdlcError(f"任务修订事务缺少上一版回执：{path.name}。", exit_code=1)
        _validate_receipt(journal["previous_receipt"])
        if journal["event"].get("event_type") != TASK_PLAN_REVISION_EVENT:
            raise SdlcError(f"任务修订事务事件类型无效：{path.name}。", exit_code=1)
        event_payload = journal["event"].get("payload")
        if (
            not isinstance(event_payload, dict)
            or event_payload.get("previous_package_sha256")
            != journal["previous_receipt"].get("package_sha256")
        ):
            raise SdlcError(f"任务修订事务没有绑定上一版整包：{path.name}。", exit_code=1)
    return journal


def _cleanup_journal(
    paths: ProjectPaths,
    journal_path: Path,
    staging_directory: Path,
) -> None:
    shutil.rmtree(staging_directory.parent, ignore_errors=True)
    journal_path.unlink(missing_ok=True)
    _fsync_directory(_task_transaction_root(paths))


def _recover_task_plan_imports_locked(paths: ProjectPaths) -> list[str]:
    from codex_sdlc.core.state import load_events

    transaction_root = _task_transaction_root(paths)
    staging_root = transaction_root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    recovered: list[str] = []
    for journal_path in sorted(transaction_root.glob("*.json")):
        journal = _read_journal(journal_path)
        receipt = _validate_receipt(journal["receipt"])  # type: ignore[arg-type]
        event = journal["event"]
        if not isinstance(event, dict):
            raise SdlcError("任务导入事务事件无效。", exit_code=1)
        plan, _tasks, coverage = _validate_import_event(event, receipt)
        task_directory = _safe_project_path(
            paths, journal["task_directory"], label="任务正式目录"
        )
        coverage_file = _safe_project_path(
            paths, journal["coverage_file"], label="任务覆盖文件"
        )
        staging_directory = _safe_project_path(
            paths, journal["staging_directory"], label="任务暂存目录"
        )
        try:
            task_directory.relative_to(paths.requirements_dir)
            coverage_file.relative_to(paths.requirements_dir)
            staging_directory.relative_to(staging_root)
        except ValueError as exc:
            raise SdlcError("任务导入事务路径越过受管目录。", exit_code=1) from exc
        if task_directory.name != "tasks" or coverage_file.name != "task-coverage.v1.json":
            raise SdlcError("任务导入事务目标路径不符合正式布局。", exit_code=1)

        if journal.get("mode") == "revision":
            previous_receipt = _validate_receipt(
                journal["previous_receipt"]  # type: ignore[arg-type]
            )
            backup_directory = _safe_project_path(
                paths,
                journal["backup_directory"],
                label="上一版任务备份目录",
            )
            backup_coverage_file = _safe_project_path(
                paths,
                journal["backup_coverage_file"],
                label="上一版覆盖备份文件",
            )
            try:
                backup_directory.relative_to(staging_directory.parent)
                backup_coverage_file.relative_to(staging_directory.parent)
            except ValueError as exc:
                raise SdlcError("任务修订备份路径越过事务暂存目录。", exit_code=1) from exc
            current_events = load_events(paths)
            committed_events = [
                item
                for item in current_events
                if item.get("event_id") == event.get("event_id")
            ]
            if committed_events:
                if len(committed_events) != 1 or committed_events[0] != event:
                    raise SdlcError("任务修订事件编号冲突。", exit_code=1)
                _validate_committed_directory(
                    task_directory,
                    expected_receipt=receipt,
                )
                _ensure_coverage(
                    coverage_file,
                    coverage,
                    str(receipt["coverage_sha256"]),
                )
                recovered.append(str(plan["requirement_id"]))
                _cleanup_journal(paths, journal_path, staging_directory)
                continue

            # 新事件没有落盘时必须完整恢复上一版；审核登记和历史事件都不改写。
            if task_directory.exists() or task_directory.is_symlink():
                current_receipt = _validate_committed_directory(task_directory)
                if canonical_sha256(current_receipt) == canonical_sha256(receipt):
                    if not backup_directory.is_dir() or backup_directory.is_symlink():
                        raise SdlcError(
                            "任务修订失败且上一版任务备份缺失，不能自动恢复。",
                            exit_code=1,
                        )
                    shutil.rmtree(task_directory)
                    _fsync_directory(task_directory.parent)
                elif canonical_sha256(current_receipt) != canonical_sha256(
                    previous_receipt
                ):
                    raise SdlcError("任务修订恢复时发现未知正式任务目录。", exit_code=1)
            if backup_directory.exists() or backup_directory.is_symlink():
                if task_directory.exists() or task_directory.is_symlink():
                    raise SdlcError("任务修订恢复时同时存在两份上一版任务。", exit_code=1)
                if backup_directory.is_symlink() or not backup_directory.is_dir():
                    raise SdlcError("上一版任务备份目录无效。", exit_code=1)
                os.rename(backup_directory, task_directory)
                _fsync_directory(task_directory.parent)
            _validate_committed_directory(
                task_directory,
                expected_receipt=previous_receipt,
            )

            if backup_coverage_file.exists() or backup_coverage_file.is_symlink():
                if backup_coverage_file.is_symlink() or not backup_coverage_file.is_file():
                    raise SdlcError("上一版覆盖备份文件无效。", exit_code=1)
                coverage_file.unlink(missing_ok=True)
                os.rename(backup_coverage_file, coverage_file)
                _fsync_directory(coverage_file.parent)
            previous_events = [
                item
                for item in current_events
                if item.get("event_id") == previous_receipt.get("event_id")
            ]
            if len(previous_events) != 1:
                raise SdlcError("上一版任务事件缺失或冲突，不能完成恢复。", exit_code=1)
            _previous_plan, _previous_tasks, previous_coverage = (
                _validate_import_event(previous_events[0], previous_receipt)
            )
            _ensure_coverage(
                coverage_file,
                previous_coverage,
                str(previous_receipt["coverage_sha256"]),
            )
            _cleanup_journal(paths, journal_path, staging_directory)
            continue

        uncommitted_placeholder = (
            journal["had_empty_placeholder"]
            and task_directory.is_dir()
            and not task_directory.is_symlink()
            and not any(task_directory.iterdir())
        )
        if (task_directory.exists() or task_directory.is_symlink()) and not uncommitted_placeholder:
            _validate_committed_directory(task_directory, expected_receipt=receipt)
            _ensure_coverage(
                coverage_file,
                coverage,
                str(receipt["coverage_sha256"]),
            )
            _ensure_event(paths, event)
            recovered.append(str(plan["requirement_id"]))
            _cleanup_journal(paths, journal_path, staging_directory)
            continue

        # 正式目录没有出现时一定还没越过唯一提交点；此时事件和覆盖文件也不应存在。
        event_id = str(receipt["event_id"])
        if any(item.get("event_id") == event_id for item in load_events(paths)):
            raise SdlcError("任务导入事件已经存在，但正式 tasks 目录缺失。", exit_code=1)
        if coverage_file.exists() or coverage_file.is_symlink():
            raise SdlcError("任务覆盖文件已经存在，但正式 tasks 目录缺失。", exit_code=1)
        if journal["had_empty_placeholder"] and not uncommitted_placeholder:
            task_directory.mkdir()
            _fsync_directory(task_directory.parent)
        _cleanup_journal(paths, journal_path, staging_directory)

    # 没有事务日志引用的暂存内容从未越过正式提交点，可以直接清理。
    for orphan in sorted(staging_root.iterdir()):
        if orphan.is_dir() and not orphan.is_symlink():
            shutil.rmtree(orphan, ignore_errors=True)
        else:
            orphan.unlink(missing_ok=True)
    for temporary in transaction_root.glob("*.tmp"):
        temporary.unlink(missing_ok=True)
    return recovered


def recover_task_plan_imports(paths: ProjectPaths) -> list[str]:
    """恢复全部任务整包事务，结果只能是完整成功或完整失败。"""

    from codex_sdlc.core.state import event_write_lock

    ensure_base_dirs(paths)
    with project_lock(paths):
        with event_write_lock(paths):
            return _recover_task_plan_imports_locked(paths)


def _known_task_ids(state: Mapping[str, object]) -> set[str]:
    requirements = state.get("requirements")
    result: set[str] = set()
    if not isinstance(requirements, Mapping):
        return result
    for requirement in requirements.values():
        if not isinstance(requirement, Mapping):
            continue
        for task in requirement.get("tasks", []):  # type: ignore[union-attr]
            if isinstance(task, Mapping) and _TASK_ID_PATTERN.fullmatch(
                str(task.get("task_id") or "")
            ):
                result.add(str(task["task_id"]))
    return result


def _requirement_task_ids(requirement: Mapping[str, object]) -> set[str]:
    """读取当前需求已经占用的编号，避免修订把包内任务伪装成外部依赖。"""

    return {
        str(task["task_id"])
        for task in requirement.get("tasks", [])  # type: ignore[union-attr]
        if isinstance(task, Mapping)
        and _TASK_ID_PATTERN.fullmatch(str(task.get("task_id") or ""))
    }


def _validate_record_dependency_graph(
    state: Mapping[str, object],
    *,
    requirement_id: str,
    plan: Mapping[str, object],
    tasks: Iterable[Mapping[str, object]],
) -> None:
    """映射正式编号后复核整张可达依赖图，防止编号重写生成自依赖或环。"""

    task_records = [dict(task) for task in tasks]
    current_by_id = {
        str(task.get("task_id") or ""): task
        for task in task_records
    }
    if len(current_by_id) != len(task_records) or "" in current_by_id:
        raise SdlcError("映射后的任务编号重复或为空。", exit_code=1)

    expected_dependencies = {
        (str(dependency), task_id)
        for task_id, task in current_by_id.items()
        for dependency in task.get("depends_on", [])  # type: ignore[union-attr]
    }
    raw_plan_dependencies = list(plan.get("dependencies", []))  # type: ignore[arg-type]
    plan_dependencies = {
        (str(item.get("from") or ""), str(item.get("to") or ""))
        for item in raw_plan_dependencies
        if isinstance(item, Mapping)
    }
    if (
        len(plan_dependencies) != len(raw_plan_dependencies)
        or plan_dependencies != expected_dependencies
    ):
        raise SdlcError("映射后的任务计划依赖与任务依赖不一致。", exit_code=1)

    all_by_id: dict[str, Mapping[str, object]] = {}
    requirements = state.get("requirements")
    if isinstance(requirements, Mapping):
        for raw_requirement_id, requirement in requirements.items():
            if str(raw_requirement_id) == requirement_id or not isinstance(
                requirement, Mapping
            ):
                continue
            for task in requirement.get("tasks", []):  # type: ignore[union-attr]
                if not isinstance(task, Mapping):
                    continue
                task_id = str(task.get("task_id") or "")
                if not _TASK_ID_PATTERN.fullmatch(task_id):
                    continue
                if task_id in all_by_id:
                    raise SdlcError(f"正式任务编号重复：{task_id}。", exit_code=1)
                all_by_id[task_id] = task
    for task_id, task in current_by_id.items():
        if task_id in all_by_id:
            raise SdlcError(f"正式任务编号重复：{task_id}。", exit_code=1)
        all_by_id[task_id] = task

    graph: dict[str, tuple[str, ...]] = {}
    for task_id, task in all_by_id.items():
        dependencies = tuple(
            str(dependency)
            for dependency in task.get("depends_on", [])  # type: ignore[union-attr]
            if str(dependency) in all_by_id
        )
        if task_id in current_by_id and task_id in dependencies:
            raise SdlcError(f"映射后的任务 {task_id} 不能依赖自身。", exit_code=1)
        graph[task_id] = dependencies

    visited: set[str] = set()
    visiting: list[str] = []

    def visit(task_id: str) -> None:
        if task_id in visiting:
            cycle_start = visiting.index(task_id)
            cycle = visiting[cycle_start:] + [task_id]
            raise SdlcError(
                "映射后的正式任务存在依赖环：" + " -> ".join(cycle) + "。",
                exit_code=1,
            )
        if task_id in visited:
            return
        visiting.append(task_id)
        for dependency in graph.get(task_id, ()):
            visit(dependency)
        visiting.pop()
        visited.add(task_id)

    # 从当前包任务出发即可覆盖全部会影响本次修订的内部和跨需求依赖链，
    # 不让其他无关需求的旧数据阻塞当前任务包。
    for task_id in sorted(current_by_id):
        visit(task_id)


def _task_plan_event_for_requirement(
    events: Iterable[Mapping[str, object]],
    requirement_id: str,
) -> dict[str, object] | None:
    matches = [
        dict(event)
        for event in events
        if event.get("event_type") in {TASK_PLAN_EVENT, TASK_PLAN_REVISION_EVENT}
        and event.get("requirement_id") == requirement_id
    ]
    if not matches:
        return None
    if matches[0].get("event_type") != TASK_PLAN_EVENT:
        raise SdlcError(f"{requirement_id} 的任务计划事件链缺少首次导入。", exit_code=1)
    previous_package_sha256 = ""
    for index, event in enumerate(matches):
        expected_type = TASK_PLAN_EVENT if index == 0 else TASK_PLAN_REVISION_EVENT
        if event.get("event_type") != expected_type:
            raise SdlcError(f"{requirement_id} 包含冲突的任务计划事件。", exit_code=1)
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise SdlcError(f"{requirement_id} 的任务计划事件缺少正文。", exit_code=1)
        package_sha256 = str(payload.get("package_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", package_sha256) is None:
            raise SdlcError(f"{requirement_id} 的任务计划事件摘要无效。", exit_code=1)
        if index and not hmac.compare_digest(
            str(payload.get("previous_package_sha256") or ""),
            previous_package_sha256,
        ):
            raise SdlcError(f"{requirement_id} 的任务计划修订链不连续。", exit_code=1)
        previous_package_sha256 = package_sha256
    return matches[-1]


def _require_needs_fix_revision(
    requirement: Mapping[str, object],
    *,
    requirement_id: str,
) -> None:
    """锁内再次确认当前整套任务确实由有效的 needs_fix 审核退回。"""

    review_state = requirement.get("task_plan_review_state")
    reviews = (
        review_state.get("reviews")
        if isinstance(review_state, Mapping)
        else None
    )
    current = [
        review
        for review in reviews or []
        if isinstance(review, Mapping) and review.get("is_current") is True
    ]
    if (
        len(current) != 1
        or current[0].get("request_status") != "completed"
        or current[0].get("recorded_status") != "needs_fix"
        or current[0].get("effective_status") != "needs_fix"
    ):
        raise SdlcError(
            f"{requirement_id} 只有在当前整套任务审核明确返回 needs_fix 后才能修订。",
            exit_code=1,
        )
    started = [
        str(task.get("task_id") or "")
        for task in requirement.get("tasks", [])  # type: ignore[union-attr]
        if isinstance(task, Mapping)
        and str(task.get("status") or "") not in {"todo", "blocked"}
    ]
    if started:
        raise SdlcError(
            f"{requirement_id} 已有进入开发的任务，不能修订整套任务："
            + "、".join(started)
            + "。",
            exit_code=1,
        )


def _revision_mapping(
    objects: Iterable[AllocationObject],
    *,
    previous_mapping: Mapping[str, str],
    existing_ids: set[str],
) -> dict[str, str]:
    """保留仍存在的 client_key 编号，新任务只使用从未占用的新编号。"""

    ordered = list(objects)
    used_ids = set(existing_ids) | {
        str(task_id) for task_id in previous_mapping.values()
    }
    maximum = max(
        (
            int(match.group(1))
            for task_id in used_ids
            if (match := re.fullmatch(r"T-([0-9]+)", task_id)) is not None
        ),
        default=0,
    )
    result: dict[str, str] = {}
    for item in ordered:
        previous_id = str(previous_mapping.get(item.client_key) or "")
        if previous_id:
            if not _TASK_ID_PATTERN.fullmatch(previous_id):
                raise SdlcError("上一版任务编号映射无效。", exit_code=1)
            result[item.client_key] = previous_id
            continue
        maximum += 1
        candidate = f"T-{maximum:03d}"
        while candidate in used_ids:
            maximum += 1
            candidate = f"T-{maximum:03d}"
        used_ids.add(candidate)
        result[item.client_key] = candidate
    return result


def import_task_plan_bundle(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    plan_file: Path,
    tasks_dir: Path,
    coverage_file: Path,
    interruption_hook: InterruptionHook | None = None,
    allow_revision: bool = False,
) -> TaskPlanImportResult:
    """一次校验、编号并提交计划、任务 JSON、完整 Markdown 和覆盖文件。"""

    from codex_sdlc.core.state import (
        derive_state,
        event_write_lock,
        load_events,
        next_event_id,
        resolve_requirement,
    )

    clean_requirement_id = str(requirement_id or "").strip().upper()
    if not _REQUIREMENT_ID_PATTERN.fullmatch(clean_requirement_id):
        raise SdlcError("tasks 命令缺少合法需求编号。", exit_code=1)
    hook = interruption_hook or (lambda stage: None)
    ensure_base_dirs(paths)
    if not paths.events_file.is_file():
        raise SdlcError("当前项目还没有 events.jsonl，不能导入任务计划。", exit_code=1)

    with project_lock(paths):
        with event_write_lock(paths):
            _recover_task_plan_imports_locked(paths)
            state = derive_state(paths)
            requirement = resolve_requirement(state, clean_requirement_id)
            requirement_root = _validated_requirement_root(paths, requirement)
            task_directory = requirement_root / "tasks"
            official_coverage = requirement_root / "task-coverage.v1.json"
            reference_index_path = requirement_root / "reference-index.v1.json"
            reference_index = validate_reference_index_file(
                requirement_root,
                reference_index_path,
            )
            if reference_index.get("requirement_id") != clean_requirement_id:
                raise SdlcError("正式引用索引的 requirement_id 与命令目标不一致。", exit_code=1)
            reference_index_sha256 = sha256_file(reference_index_path)

            plan = _plan_submission(
                _read_json(Path(plan_file), label="task-plan.v2"),
                clean_requirement_id,
            )
            tasks = _task_submissions(Path(tasks_dir), clean_requirement_id)
            coverage = _coverage_submission(
                _read_json(Path(coverage_file), label="task-coverage.v1"),
                clean_requirement_id,
            )
            known_task_ids = _known_task_ids(state)
            objects = _allocation_objects(
                plan,
                tasks,
                known_task_ids,
                _requirement_task_ids(requirement),
            )
            submission_sha256 = canonical_sha256(
                {
                    "schema_version": "task-import-submission.v1",
                    "requirement_id": clean_requirement_id,
                    "reference_index_sha256": reference_index_sha256,
                    "plan": plan,
                    "tasks": {
                        client_key: tasks[client_key] for client_key in sorted(tasks)
                    },
                    "coverage": coverage,
                }
            )

            existing_tasks = list(requirement.get("tasks", []))
            events = load_events(paths)
            existing_event = _task_plan_event_for_requirement(
                events, clean_requirement_id
            )
            empty_placeholder = False
            revision_receipt: dict[str, object] | None = None
            previous_mapping: dict[str, str] = {}
            if task_directory.exists() or task_directory.is_symlink():
                if (
                    not task_directory.is_symlink()
                    and task_directory.is_dir()
                    and not any(task_directory.iterdir())
                ):
                    # 文档优先建档会预建一个空 tasks 目录。它没有业务内容，
                    # 正式任务事务在写暂存成功前只记录这个占位状态。
                    empty_placeholder = True
                    receipt = None
                else:
                    receipt = _validate_committed_directory(task_directory)
                if receipt is None:
                    pass
                elif receipt.get("requirement_id") != clean_requirement_id:
                    raise SdlcError("现有任务目录不属于当前需求。", exit_code=1)
                else:
                    if existing_event is None:
                        raise SdlcError("任务目录已经存在，但缺少对应任务导入事件。", exit_code=1)
                    plan_record, _task_records, coverage_record = _validate_import_event(
                        existing_event, receipt
                    )
                    receipt_mapping = {
                        str(key): str(value)
                        for key, value in receipt["mapping"].items()  # type: ignore[union-attr]
                    }
                    same_reference = hmac.compare_digest(
                        str(receipt["reference_index_sha256"]),
                        reference_index_sha256,
                    )
                    expected_package_sha256 = ""
                    if same_reference and set(receipt_mapping) == set(tasks):
                        expected_plan, expected_tasks, expected_coverage = (
                            _record_documents(
                                plan=plan,
                                tasks=tasks,
                                coverage=coverage,
                                mapping=receipt_mapping,
                                reference_index_sha256=reference_index_sha256,
                                submission_sha256=submission_sha256,
                                producer_run_id_override=(
                                    str(plan_record.get("producer_run_id") or "")
                                    if not plan.get("producer_run_id")
                                    else ""
                                ),
                            )
                        )
                        expected_package_sha256 = _formal_package_sha256(
                            plan=expected_plan,
                            tasks=expected_tasks,
                            coverage=expected_coverage,
                            reference_index_sha256=reference_index_sha256,
                        )
                    if expected_package_sha256 and hmac.compare_digest(
                        str(receipt["package_sha256"]),
                        expected_package_sha256,
                    ):
                        _validate_record_dependency_graph(
                            state,
                            requirement_id=clean_requirement_id,
                            plan=expected_plan,
                            tasks=expected_tasks,
                        )
                        _ensure_coverage(
                            official_coverage,
                            coverage_record,
                            str(receipt["coverage_sha256"]),
                        )
                        return TaskPlanImportResult(
                            requirement_id=clean_requirement_id,
                            package_sha256=str(receipt["package_sha256"]),
                            mapping=receipt_mapping,
                            duplicate=True,
                            task_directory=task_directory.relative_to(
                                paths.root
                            ).as_posix(),
                            coverage_file=official_coverage.relative_to(
                                paths.root
                            ).as_posix(),
                            event_id=str(receipt["event_id"]),
                        )
                    if not allow_revision:
                        if not same_reference:
                            raise SdlcError(
                                "正式引用索引哈希已经漂移，不能返回旧任务映射。",
                                exit_code=1,
                            )
                        raise SdlcError(
                            f"{clean_requirement_id} 已经导入不同内容的任务计划，不能覆盖。",
                            exit_code=1,
                        )
                    _require_needs_fix_revision(
                        requirement,
                        requirement_id=clean_requirement_id,
                    )
                    _ensure_coverage(
                        official_coverage,
                        coverage_record,
                        str(receipt["coverage_sha256"]),
                    )
                    revision_receipt = receipt
                    previous_mapping = receipt_mapping
            is_revision = revision_receipt is not None
            if not is_revision and (
                official_coverage.exists() or official_coverage.is_symlink()
            ):
                raise SdlcError(
                    "现有 task-coverage.v1.json 没有对应任务导入回执，不能覆盖。",
                    exit_code=1,
                )
            if not is_revision and existing_event is not None:
                raise SdlcError("任务计划事件已经存在，但正式 tasks 目录缺失。", exit_code=1)
            if not is_revision and existing_tasks:
                completed = [
                    str(task.get("task_id") or "")
                    for task in existing_tasks
                    if isinstance(task, Mapping)
                    and task.get("status") in {"done", "closed"}
                ]
                suffix = "，其中已完成任务为 " + "、".join(completed) if completed else ""
                raise SdlcError(
                    f"{clean_requirement_id} 已有任务，不能用整包导入直接改写{suffix}。",
                    exit_code=1,
                )

            effective_change_ids = {
                str(change.get("change_id") or "")
                for change in requirement.get("changes", [])
                if isinstance(change, Mapping)
                and change.get("status") in {"effective", "accepted"}
            }
            structured = requirement.get("structured")
            if isinstance(structured, Mapping):
                effective_change_ids.update(
                    str(change.get("change_id") or "")
                    for change in structured.get("effective_changes", [])
                    if isinstance(change, Mapping)
                )
            pending_change_ids = [
                str(change.get("change_id") or "")
                for change in requirement.get("changes", [])
                if isinstance(change, Mapping)
                and change.get("status") in {"draft", "pending"}
            ]
            if pending_change_ids:
                raise SdlcError(
                    f"{clean_requirement_id} 仍有未确认或未生效的变更："
                    + "、".join(pending_change_ids)
                    + "。",
                    exit_code=1,
                )
            entries = reference_index.get("entries")
            if not isinstance(entries, Mapping):
                raise SdlcError("正式引用索引缺少 entries。", exit_code=1)
            for task in tasks.values():
                _validate_task_references(task, entries, effective_change_ids)

            mapping = (
                _revision_mapping(
                    objects,
                    previous_mapping=previous_mapping,
                    existing_ids=known_task_ids,
                )
                if is_revision
                else allocate_stable_ids(
                    objects,
                    existing_ids=known_task_ids,
                )
            )
            record_plan, record_tasks, record_coverage = _record_documents(
                plan=plan,
                tasks=tasks,
                coverage=coverage,
                mapping=mapping,
                reference_index_sha256=reference_index_sha256,
                submission_sha256=submission_sha256,
            )
            _validate_record_dependency_graph(
                state,
                requirement_id=clean_requirement_id,
                plan=record_plan,
                tasks=record_tasks,
            )
            if is_revision:
                removed_task_ids = set(previous_mapping.values()) - set(
                    mapping.values()
                )
                dangling_dependencies = sorted(
                    {
                        str(dependency)
                        for task in record_tasks
                        for dependency in task["depends_on"]  # type: ignore[index]
                        if str(dependency) in removed_task_ids
                    }
                )
                if dangling_dependencies:
                    raise SdlcError(
                        "修订后的任务仍依赖已经移除的上一版任务："
                        + "、".join(dangling_dependencies)
                        + "。",
                        exit_code=1,
                    )
            package_sha256 = _formal_package_sha256(
                plan=record_plan,
                tasks=record_tasks,
                coverage=record_coverage,
                reference_index_sha256=reference_index_sha256,
            )
            files = _bundle_files(record_plan, record_tasks)
            event_id = next_event_id(events)
            receipt = _build_receipt(
                requirement_id=clean_requirement_id,
                package_sha256=package_sha256,
                reference_index_sha256=reference_index_sha256,
                mapping=mapping,
                event_id=event_id,
                files=files,
                coverage=record_coverage,
            )
            event = _event_for_import(
                paths=paths,
                event_id=event_id,
                requirement_id=clean_requirement_id,
                package_sha256=package_sha256,
                reference_index_sha256=reference_index_sha256,
                mapping=mapping,
                plan=record_plan,
                tasks=record_tasks,
                coverage=record_coverage,
                receipt=receipt,
                event_type=(
                    TASK_PLAN_REVISION_EVENT if is_revision else TASK_PLAN_EVENT
                ),
                previous_package_sha256=(
                    str(revision_receipt["package_sha256"])
                    if revision_receipt is not None
                    else ""
                ),
            )
            _validate_import_event(event, receipt)

            transaction_root = _task_transaction_root(paths)
            staging_root = transaction_root / "staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            transaction_id = uuid.uuid4().hex
            staging_parent = staging_root / transaction_id
            staging_directory = staging_parent / "tasks"
            backup_directory = staging_parent / "previous-tasks"
            backup_coverage_file = (
                staging_parent / "previous-task-coverage.v1.json"
            )
            journal_path = transaction_root / f"{transaction_id}.json"
            try:
                _write_staging_bundle(staging_directory, files, receipt)
                journal = _journal_document(
                    paths=paths,
                    transaction_id=transaction_id,
                    staging_directory=staging_directory,
                    task_directory=task_directory,
                    coverage_file=official_coverage,
                    had_empty_placeholder=empty_placeholder,
                    receipt=receipt,
                    event=event,
                    mode="revision" if is_revision else "import",
                    backup_directory=backup_directory if is_revision else None,
                    backup_coverage_file=(
                        backup_coverage_file if is_revision else None
                    ),
                    previous_receipt=revision_receipt,
                )
                _atomic_write_json(journal_path, journal)
                hook(INTERRUPT_AFTER_STAGING)
                if is_revision:
                    if (
                        task_directory.is_symlink()
                        or not task_directory.is_dir()
                        or official_coverage.is_symlink()
                        or not official_coverage.is_file()
                    ):
                        raise SdlcError(
                            "上一版任务正式文件不完整，不能开始修订。",
                            exit_code=1,
                        )
                    os.rename(task_directory, backup_directory)
                    os.rename(official_coverage, backup_coverage_file)
                    _fsync_directory(requirement_root)
                    _fsync_directory(staging_parent)
                elif empty_placeholder:
                    task_directory.rmdir()
                    _fsync_directory(requirement_root)
                    hook(INTERRUPT_AFTER_PLACEHOLDER_REMOVAL)
                os.rename(staging_directory, task_directory)
                _fsync_directory(requirement_root)
                hook(INTERRUPT_AFTER_DIRECTORY_COMMIT)
                _ensure_coverage(
                    official_coverage,
                    record_coverage,
                    str(receipt["coverage_sha256"]),
                )
                hook(INTERRUPT_AFTER_COVERAGE_COMMIT)
                _ensure_event(paths, event)
                hook(INTERRUPT_AFTER_EVENT_COMMIT)
                _cleanup_journal(paths, journal_path, staging_directory)
            except Exception:
                if journal_path.is_file():
                    try:
                        _recover_task_plan_imports_locked(paths)
                    except Exception:
                        pass
                else:
                    _cleanup_journal(paths, journal_path, staging_directory)
                raise
            return TaskPlanImportResult(
                requirement_id=clean_requirement_id,
                package_sha256=package_sha256,
                mapping=dict(mapping),
                duplicate=False,
                task_directory=task_directory.relative_to(paths.root).as_posix(),
                coverage_file=official_coverage.relative_to(paths.root).as_posix(),
                event_id=event_id,
            )


def load_task_plan_record(
    paths: ProjectPaths,
    requirement_id: str,
) -> dict[str, object]:
    """从回执、真实文件和唯一事件交叉读取当前任务计划。"""

    from codex_sdlc.core.state import derive_state, load_events, resolve_requirement

    state = derive_state(paths)
    requirement = resolve_requirement(state, requirement_id)
    root = _validated_requirement_root(paths, requirement)
    task_directory = root / "tasks"
    receipt = _validate_committed_directory(task_directory)
    events = load_events(paths)
    event = _task_plan_event_for_requirement(events, str(requirement["requirement_id"]))
    if event is None:
        raise SdlcError("正式任务计划缺少任务导入事件。", exit_code=1)
    plan, _tasks, coverage = _validate_import_event(event, receipt)
    _ensure_coverage(
        root / "task-coverage.v1.json",
        coverage,
        str(receipt["coverage_sha256"]),
    )
    disk_plan = _read_json(task_directory / "task-plan.v2.json", label="正式 task-plan.v2")
    if canonical_sha256(disk_plan) != canonical_sha256(plan):
        raise SdlcError("正式 task-plan.v2 与任务导入事件不一致。", exit_code=1)
    reference_hash = sha256_file(root / "reference-index.v1.json")
    if not hmac.compare_digest(
        reference_hash, str(receipt["reference_index_sha256"])
    ):
        raise SdlcError("正式引用索引哈希与任务导入回执不一致。", exit_code=1)
    return plan


def state_tasks_from_import_event(
    event: Mapping[str, object],
) -> list[dict[str, object]]:
    """把 task.v2 明确字段投影到现有状态读取面，不补任何业务内容。"""

    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError("任务导入事件缺少结构化正文。", exit_code=1)
    mapping = payload.get("mapping")
    files = payload.get("files")
    receipt = {
        "schema_version": TASK_IMPORT_RECEIPT_SCHEMA,
        "requirement_id": event.get("requirement_id"),
        "package_sha256": payload.get("package_sha256"),
        "reference_index_sha256": payload.get("reference_index_sha256"),
        "mapping": mapping,
        "event_id": event.get("event_id"),
        "files": files,
        "coverage_sha256": payload.get("coverage_sha256"),
    }
    validated_receipt = _validate_receipt(receipt)
    _plan, tasks, _coverage = _validate_import_event(event, validated_receipt)
    created_at = str(event.get("created_at") or "")
    result: list[dict[str, object]] = []
    for task in tasks:
        scope = task["code_scope"]
        if not isinstance(scope, Mapping):
            raise SdlcError("task.v2 的 code_scope 必须是对象。", exit_code=1)
        blocking_conditions = list(task["blocking_conditions"])
        technical_refs = [
            str(item["reference_key"])
            for item in task["technical_solution_refs"]
            if isinstance(item, Mapping)
        ]
        result.append(
            {
                "requirement_id": task["requirement_id"],
                "task_id": task["task_id"],
                "title": task["title"],
                "summary": task["goal"],
                "status": "blocked" if blocking_conditions else "todo",
                "depends_on": deepcopy(task["depends_on"]),
                "created_at": created_at,
                "updated_at": created_at,
                "changed_files": deepcopy(scope["likely_change_paths"]),
                "context_files": deepcopy(scope["read_paths"]),
                "output_files": [],
                "related_files": [],
                "commands": [],
                "test_items": deepcopy(task["automated_tests"]),
                "test_commands": [],
                "test_scripts": [],
                "manual_checks": deepcopy(task["manual_checks"]),
                "verifications": [],
                "note": "",
                "business_rules": [
                    *deepcopy(task["implementation_requirements"]),
                    *deepcopy(task["data_api_page_component_requirements"]),
                    *deepcopy(task["states_and_exceptions"]),
                    *deepcopy(task["security_and_privacy"]),
                ],
                "coverage_points": deepcopy(task["requirement_refs"]),
                "coverage_change_ids": deepcopy(task["change_refs"]),
                "coverage_acceptance": deepcopy(task["acceptance_refs"]),
                "coverage_tests": [],
                "formal_requirement_refs": [
                    *deepcopy(task["requirement_refs"]),
                    *deepcopy(task["global_rule_refs"]),
                ],
                "formal_design_refs": [
                    *technical_refs,
                    *deepcopy(task["design_refs"]),
                    *deepcopy(task["material_refs"]),
                ],
                "formal_test_refs": deepcopy(task["acceptance_refs"]),
                "out_of_scope": deepcopy(task["out_of_scope"]),
                "test_suggestions": [],
                "blocking_conditions": blocking_conditions,
                "formal_gate": not blocking_conditions,
                "coverage_acceptance_explicit": True,
                "task_contract": deepcopy(task),
            }
        )
    return result


__all__ = [
    "INTERRUPT_AFTER_COVERAGE_COMMIT",
    "INTERRUPT_AFTER_DIRECTORY_COMMIT",
    "INTERRUPT_AFTER_EVENT_COMMIT",
    "INTERRUPT_AFTER_PLACEHOLDER_REMOVAL",
    "INTERRUPT_AFTER_STAGING",
    "TASK_IMPORT_RECEIPT_FILE",
    "TASK_PLAN_EVENT",
    "TASK_PLAN_REVISION_EVENT",
    "TASK_PLAN_SCHEMA",
    "TASK_SCHEMA",
    "TaskPlanImportResult",
    "import_task_plan_bundle",
    "load_task_plan_record",
    "recover_task_plan_imports",
    "state_tasks_from_import_event",
    "task_markdown",
    "task_plan_markdown",
]
