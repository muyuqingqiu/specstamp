from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Mapping

from codex_sdlc.core.change_workspace import (
    BASE_VERSION_PATHS,
    build_base_versions,
    resolve_formal_requirement_dir,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.state import derive_state, load_events, resolve_requirement
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)
from codex_sdlc.core.task_outputs import load_formal_task_output_index


SCAN_SCHEMA = "change-migration-scan.v1"
CONFIRMATION_SCHEMA = "change-migration-confirmation.v1"
REGISTRY_SCHEMA = "change-migration-registry.v1"
CLASSIFICATIONS = {"restored", "legacy-note", "blocked-rebuild"}
INTERRUPT_BEFORE_REGISTRY_PUBLISH = "before_registry_publish"

_CHANGE_ID = re.compile(r"^CHG-[0-9]{3,}$")
_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3,}$")
_TASK_ID = re.compile(r"^T-[0-9]{3,}$")
_REFERENCE_ID = re.compile(
    r"^(?:FR|GR|AC|DES|MAT|DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC|T|CMAT|CHG)-[0-9]{3,}$"
)


def migration_registry_path(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "change-migration" / "registry.v1.json"


def _project_relative(paths: ProjectPaths, path: Path) -> str:
    try:
        return path.resolve(strict=True).relative_to(paths.root.resolve(strict=True)).as_posix()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"旧变更来源不在项目受控目录：{path}。") from exc


def _controlled_existing_file(paths: ProjectPaths, relative_path: object, *, label: str) -> Path:
    if not isinstance(relative_path, str):
        raise SdlcError(f"{label}必须使用项目相对路径。")
    target = resolve_project_path(paths.root, relative_path, must_exist=True)
    if not target.is_file():
        raise SdlcError(f"{label}不是普通文件：{relative_path}。")
    return target


def _explicit_change_grill_paths(paths: ProjectPaths) -> list[tuple[str, str, Path]]:
    """只读取事件里的显式 mode 和路径，不解析 Markdown 展示文字。"""

    if not paths.events_file.exists():
        return []
    records: list[tuple[str, str, Path]] = []
    for line_number, raw_line in enumerate(paths.events_file.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise SdlcError(f"events.jsonl 第 {line_number} 行不是有效 JSON，不能扫描旧变更。") from exc
        payload = event.get("payload")
        if event.get("event_type") != "grill_recorded" or not isinstance(payload, dict):
            continue
        if payload.get("mode") != "change":
            continue
        grill_id = str(payload.get("grill_id") or "")
        if not re.fullmatch(r"GRILL-[0-9]{3,}", grill_id):
            raise SdlcError("change 质询记录缺少有效的 GRILL 编号，不能猜测来源身份。")
        target = _controlled_existing_file(paths, payload.get("file_path"), label=f"{grill_id} 来源路径")
        records.append((grill_id, "change-grill", target))
    return records


def _legacy_source_records(paths: ProjectPaths) -> list[dict[str, str]]:
    candidates: list[tuple[str, str, Path]] = []
    if paths.changes_dir.exists():
        candidates.extend((path.stem, "change-record", path) for path in paths.changes_dir.glob("CHG-*.md"))
    if paths.requirements_dir.exists():
        candidates.extend(
            (path.stem, "change-record", path)
            for path in paths.requirements_dir.glob("*/changes/CHG-*.md")
        )
    candidates.extend(_explicit_change_grill_paths(paths))

    by_path: dict[str, dict[str, str]] = {}
    for source_id, source_kind, raw_path in candidates:
        if source_kind == "change-record" and not _CHANGE_ID.fullmatch(source_id):
            continue
        relative_path = _project_relative(paths, raw_path)
        controlled = _controlled_existing_file(paths, relative_path, label=f"{source_id} 来源路径")
        current = {
            "source_id": source_id,
            "source_kind": source_kind,
            "source_path": relative_path,
            "source_sha256": sha256_file(controlled),
        }
        previous = by_path.get(relative_path)
        if previous is not None and previous != current:
            raise SdlcError(f"同一旧变更来源出现冲突身份：{relative_path}。")
        by_path[relative_path] = current
    return [by_path[path] for path in sorted(by_path)]


def scan_legacy_change_records(paths: ProjectPaths) -> dict[str, object]:
    """返回完整来源清单；扫描不会写事件、登记表或原始 Markdown。"""

    document: dict[str, object] = {
        "schema_version": SCAN_SCHEMA,
        "records": _legacy_source_records(paths),
    }
    document["scan_sha256"] = canonical_sha256(document)
    return document


def _load_json_file(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise SdlcError(f"{label}必须是 JSON 对象。")
    return value


def _require_exact_keys(value: dict[str, object], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if unknown:
            details.append("包含未知字段 " + "、".join(unknown))
        raise SdlcError(f"{label}字段不完整：{'；'.join(details)}。")


def _require_string_list(value: object, *, label: str, pattern: re.Pattern[str] | None = None) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise SdlcError(f"{label}必须是非空字符串数组。")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise SdlcError(f"{label}不能包含重复项。")
    if pattern is not None and any(not pattern.fullmatch(item) for item in normalized):
        raise SdlcError(f"{label}包含无效编号。")
    return normalized


def _all_string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_all_string_values(item))
        return result
    if isinstance(value, dict):
        result = set()
        for item in value.values():
            result.update(_all_string_values(item))
        return result
    return set()


def _source_owner(paths: ProjectPaths, record: Mapping[str, object]) -> tuple[str, str]:
    """只从来源事件读取所属需求和变更，不使用 Markdown 正文或确认文件反推。"""

    source_id = str(record.get("source_id") or "")
    source_kind = str(record.get("source_kind") or "")
    source_path = str(record.get("source_path") or "")
    matches: list[tuple[str, str]] = []
    for event in load_events(paths):
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("file_path") != source_path:
            continue
        requirement_id = event.get("requirement_id")
        if not isinstance(requirement_id, str) or not _REQUIREMENT_ID.fullmatch(requirement_id):
            continue
        if event.get("project_path") != str(paths.root):
            continue
        if source_kind == "change-record":
            if (
                event.get("event_type") == "change_recorded"
                and payload.get("change_id") == source_id
                and _CHANGE_ID.fullmatch(source_id)
            ):
                matches.append((requirement_id, source_id))
            continue
        if source_kind == "change-grill":
            change_id = payload.get("change_id")
            if (
                event.get("event_type") == "grill_recorded"
                and payload.get("mode") == "change"
                and payload.get("grill_id") == source_id
                and isinstance(change_id, str)
                and _CHANGE_ID.fullmatch(change_id)
            ):
                matches.append((requirement_id, change_id))
            continue
        raise SdlcError(f"{source_id} 的来源类型不受支持，不能确定所属关系。", exit_code=1)
    if len(matches) != 1:
        raise SdlcError(
            f"{source_id} 必须由唯一正式事件明确关联所属 REQ 和 CHG，不能从展示正文推断。",
            exit_code=1,
        )
    return matches[0]


def _load_current_formal_evidence(
    paths: ProjectPaths,
    requirement_id: str,
) -> tuple[Path, dict[str, object], dict[str, object], set[str], dict[str, str]]:
    """读取当前正式版本、稳定引用和任务计划，并核对它们属于同一需求。"""

    requirement_dir = resolve_formal_requirement_dir(paths, requirement_id, load_events(paths))
    bases = build_base_versions(paths, requirement_dir)
    requirement = _load_json_file(
        requirement_dir / BASE_VERSION_PATHS["requirement"],
        label=f"{requirement_id} 当前需求版本",
    )
    design = _load_json_file(
        requirement_dir / BASE_VERSION_PATHS["design"],
        label=f"{requirement_id} 当前设计版本",
    )
    test_matrix = _load_json_file(
        requirement_dir / BASE_VERSION_PATHS["test_matrix"],
        label=f"{requirement_id} 当前测试矩阵",
    )
    for label, document, schema_version in (
        ("当前需求版本", requirement, "requirement-current.v1"),
        ("当前设计版本", design, "design-current.v1"),
        ("当前测试矩阵", test_matrix, "test-matrix-current.v1"),
    ):
        if (
            document.get("schema_version") != schema_version
            or document.get("requirement_id") != requirement_id
            or document.get("is_current") is not True
        ):
            raise SdlcError(f"{requirement_id} 的{label}身份或当前状态不正确。", exit_code=1)

    reference_index = _load_json_file(
        requirement_dir / BASE_VERSION_PATHS["reference_index"],
        label=f"{requirement_id} reference-index.v1",
    )
    validate_schema_document(reference_index, schema_name="reference-index.v1")
    if reference_index.get("requirement_id") != requirement_id:
        raise SdlcError(f"{requirement_id} 的引用索引所属需求不一致。", exit_code=1)
    entries = reference_index.get("entries")
    if not isinstance(entries, dict):
        raise SdlcError(f"{requirement_id} 的引用索引 entries 不完整。", exit_code=1)

    functional = requirement.get("functional_requirements")
    if not isinstance(functional, list):
        raise SdlcError(f"{requirement_id} 的当前需求缺少功能需求列表。", exit_code=1)
    ac_owners: dict[str, str] = {}
    for raw_fr in functional:
        if not isinstance(raw_fr, Mapping):
            raise SdlcError(f"{requirement_id} 的功能需求结构不完整。", exit_code=1)
        fr_id = raw_fr.get("id")
        if not isinstance(fr_id, str) or not re.fullmatch(r"FR-[0-9]{3,}", fr_id):
            raise SdlcError(f"{requirement_id} 的功能需求缺少稳定 FR 编号。", exit_code=1)
        acceptance = raw_fr.get("acceptance_criteria")
        if not isinstance(acceptance, list):
            raise SdlcError(f"{fr_id} 的验收标准结构不完整。", exit_code=1)
        for raw_ac in acceptance:
            ac_id = raw_ac.get("id") if isinstance(raw_ac, Mapping) else None
            if not isinstance(ac_id, str) or not re.fullmatch(r"AC-[0-9]{3,}", ac_id):
                raise SdlcError(f"{fr_id} 的验收标准缺少稳定 AC 编号。", exit_code=1)
            if ac_id in ac_owners:
                raise SdlcError(f"验收标准 {ac_id} 在当前需求中不唯一。", exit_code=1)
            ac_owners[ac_id] = fr_id

    matrix_acceptance = test_matrix.get("acceptance_criteria")
    if not isinstance(matrix_acceptance, list):
        raise SdlcError(f"{requirement_id} 的当前测试矩阵结构不完整。", exit_code=1)
    matrix_owners: dict[str, str] = {}
    for raw_ac in matrix_acceptance:
        if not isinstance(raw_ac, Mapping):
            raise SdlcError(f"{requirement_id} 的测试矩阵条目不完整。", exit_code=1)
        ac_id = raw_ac.get("id")
        owner_fr = raw_ac.get("requirement_id")
        if not isinstance(ac_id, str) or not isinstance(owner_fr, str) or ac_id in matrix_owners:
            raise SdlcError(f"{requirement_id} 的测试矩阵编号或归属不明确。", exit_code=1)
        matrix_owners[ac_id] = owner_fr
    if any(matrix_owners.get(ac_id) != owner for ac_id, owner in ac_owners.items()):
        raise SdlcError(f"{requirement_id} 的需求版本与测试矩阵 AC 归属不一致。", exit_code=1)

    task_plan = _load_json_file(
        requirement_dir / BASE_VERSION_PATHS["task_plan"],
        label=f"{requirement_id} task-plan.v2",
    )
    validate_schema_document(task_plan, schema_name="task-plan.v2")
    if task_plan.get("requirement_id") != requirement_id:
        raise SdlcError(f"{requirement_id} 的任务计划所属需求不一致。", exit_code=1)
    raw_task_ids = task_plan.get("tasks")
    if not isinstance(raw_task_ids, list) or any(not isinstance(item, str) for item in raw_task_ids):
        raise SdlcError(f"{requirement_id} 的任务计划 tasks 不完整。", exit_code=1)
    return requirement_dir, bases, task_plan, set(entries), ac_owners


def _validate_formal_task_file(
    requirement_dir: Path,
    requirement_id: str,
    task_id: str,
) -> None:
    task = _load_json_file(
        requirement_dir / "tasks" / f"{task_id}.json",
        label=f"{requirement_id} / {task_id} 正式任务",
    )
    validate_schema_document(task, schema_name="task.v2")
    if task.get("requirement_id") != requirement_id or task.get("task_id") != task_id:
        raise SdlcError(f"正式任务 {task_id} 的需求或任务身份不一致。", exit_code=1)


def _validate_restored(paths: ProjectPaths, record: dict[str, object]) -> None:
    _require_exact_keys(
        record,
        {
            "source_id",
            "source_kind",
            "source_path",
            "source_sha256",
            "classification",
            "structured_result",
        },
        label=f"{record.get('source_id', 'restored')} 分类",
    )
    result = record.get("structured_result")
    if not isinstance(result, dict):
        raise SdlcError("restored 分类必须提供 structured_result。")
    _require_exact_keys(
        result,
        {
            "requirement_id",
            "change_id",
            "change_package_path",
            "change_package_sha256",
            "reference_ids",
            "task_ids",
            "version_ids",
        },
        label="restored 结构化结果",
    )
    requirement_id = result.get("requirement_id")
    change_id = result.get("change_id")
    if not isinstance(requirement_id, str) or not _REQUIREMENT_ID.fullmatch(requirement_id):
        raise SdlcError("restored 结构化结果缺少有效 REQ 编号。")
    if not isinstance(change_id, str) or not _CHANGE_ID.fullmatch(change_id):
        raise SdlcError("restored 结构化结果缺少有效 CHG 编号。")
    source_requirement_id, source_change_id = _source_owner(paths, record)
    if requirement_id != source_requirement_id or change_id != source_change_id:
        raise SdlcError("restored 结构化结果与来源事件的 REQ 或 CHG 归属不一致。", exit_code=1)
    requirement_dir, current_bases, task_plan, formal_reference_ids, ac_owners = (
        _load_current_formal_evidence(paths, requirement_id)
    )
    package_path = _controlled_existing_file(
        paths,
        result.get("change_package_path"),
        label=f"{change_id} change-package.v1 路径",
    )
    declared_hash = result.get("change_package_sha256")
    if not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        raise SdlcError("restored 结构化结果缺少有效的变更包 SHA-256。")
    if sha256_file(package_path) != declared_hash:
        raise SdlcError(f"{change_id} change-package.v1 的 SHA-256 与确认文件不一致。")
    package = _load_json_file(package_path, label=f"{change_id} change-package.v1")
    validate_schema_document(package, schema_name="change-package.v1")
    if package.get("requirement_id") != requirement_id or package.get("change_id") != change_id:
        raise SdlcError("restored 结构化结果与 change-package.v1 的 REQ 或 CHG 身份不一致。")
    if package.get("open_questions"):
        raise SdlcError("restored 的 change-package.v1 仍有 open_questions，不能确定性还原。")
    reference_ids = _require_string_list(
        result.get("reference_ids"),
        label="restored reference_ids",
        pattern=_REFERENCE_ID,
    )
    task_ids = _require_string_list(result.get("task_ids"), label="restored task_ids", pattern=_TASK_ID)
    version_ids = _require_string_list(result.get("version_ids"), label="restored version_ids")
    explicit_values = _all_string_values(package)
    if any(item not in explicit_values for item in [*reference_ids, *task_ids]):
        raise SdlcError("restored 的引用或任务编号没有在 change-package.v1 中显式出现。")
    if package.get("base_versions") != current_bases:
        raise SdlcError("restored 的基础版本路径或 SHA-256 与当前正式版本不一致。", exit_code=1)
    current_version_paths = [
        str(current_bases[name]["path"])  # type: ignore[index]
        for name in BASE_VERSION_PATHS
    ]
    if version_ids != current_version_paths:
        raise SdlcError("restored version_ids 必须逐项对应当前五份正式版本路径。", exit_code=1)
    if any(reference_id not in formal_reference_ids for reference_id in reference_ids):
        raise SdlcError("restored reference_ids 包含当前正式引用索引中不存在的编号。", exit_code=1)
    fr_ids = {item for item in reference_ids if item.startswith("FR-")}
    ac_ids = {item for item in reference_ids if item.startswith("AC-")}
    if not fr_ids or not ac_ids:
        raise SdlcError("restored reference_ids 必须显式包含正式 FR 和 AC 编号。", exit_code=1)
    if any(ac_id not in ac_owners or ac_owners[ac_id] not in fr_ids for ac_id in ac_ids):
        raise SdlcError("restored 的 AC 不属于本次显式引用的正式 FR。", exit_code=1)
    planned_task_ids = task_plan.get("tasks")
    if not isinstance(planned_task_ids, list) or any(task_id not in planned_task_ids for task_id in task_ids):
        raise SdlcError("restored task_ids 包含当前正式任务计划中不存在的任务。", exit_code=1)
    for task_id in task_ids:
        _validate_formal_task_file(requirement_dir, requirement_id, task_id)


def _validate_legacy_note(paths: ProjectPaths, record: dict[str, object]) -> None:
    completed_task_ids = _require_string_list(
        record.get("completed_task_ids"),
        label="legacy-note completed_task_ids",
        pattern=_TASK_ID,
    )
    requirement_id, _change_id = _source_owner(paths, record)
    requirement_dir, _bases, task_plan, _reference_ids, _ac_owners = (
        _load_current_formal_evidence(paths, requirement_id)
    )
    planned_task_ids = task_plan.get("tasks")
    if not isinstance(planned_task_ids, list) or any(
        task_id not in planned_task_ids for task_id in completed_task_ids
    ):
        raise SdlcError("legacy-note 包含当前正式任务计划中不存在的任务。", exit_code=1)

    state = derive_state(paths)
    requirement = resolve_requirement(state, requirement_id)
    raw_tasks = requirement.get("tasks")
    if not isinstance(raw_tasks, list):
        raise SdlcError(f"{requirement_id} 的正式任务状态不完整。", exit_code=1)
    tasks = {
        str(task.get("task_id")): task
        for task in raw_tasks
        if isinstance(task, Mapping) and isinstance(task.get("task_id"), str)
    }
    for task_id in completed_task_ids:
        task = tasks.get(task_id)
        if task is None:
            raise SdlcError(f"legacy-note 引用的正式任务 {task_id} 不存在。", exit_code=1)
        status = task.get("status")
        if status not in {"done", "closed"}:
            raise SdlcError(f"legacy-note 引用的任务 {task_id} 尚未正式完成，当前状态为 {status}。", exit_code=1)
        _validate_formal_task_file(requirement_dir, requirement_id, task_id)

    done_ids = {
        task_id for task_id in completed_task_ids if tasks[task_id].get("status") == "done"
    }
    if done_ids:
        output_index = load_formal_task_output_index(
            paths,
            requirement,
            required=True,
            verify_runtime=True,
        )
        outputs = output_index.get("task_outputs")
        output_task_ids = {
            str(item.get("task_id"))
            for item in outputs
            if isinstance(item, Mapping)
        } if isinstance(outputs, list) else set()
        if not done_ids.issubset(output_task_ids):
            raise SdlcError("legacy-note 引用的 done 任务缺少正式关闭轮次和哈希证据。", exit_code=1)


def _validate_classified_record(paths: ProjectPaths, record: dict[str, object]) -> None:
    classification = record.get("classification")
    if classification not in CLASSIFICATIONS:
        raise SdlcError("迁移分类必须是 restored、legacy-note 或 blocked-rebuild。")
    if classification == "restored":
        _validate_restored(paths, record)
        return
    common = {"source_id", "source_kind", "source_path", "source_sha256", "classification"}
    if classification == "legacy-note":
        _require_exact_keys(record, common | {"completed_task_ids"}, label=f"{record.get('source_id')} 分类")
        _validate_legacy_note(paths, record)
        return
    _require_exact_keys(record, common | {"required_materials"}, label=f"{record.get('source_id')} 分类")
    _require_string_list(record.get("required_materials"), label="blocked-rebuild required_materials")


def _validate_confirmation(
    paths: ProjectPaths,
    confirmation: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    _require_exact_keys(
        confirmation,
        {"schema_version", "scan_sha256", "records", "confirmation_sha256"},
        label="迁移分类确认",
    )
    if confirmation.get("schema_version") != CONFIRMATION_SCHEMA:
        raise SdlcError(f"迁移分类确认 schema_version 必须是 {CONFIRMATION_SCHEMA}。")
    declared_confirmation_hash = confirmation.get("confirmation_sha256")
    confirmation_payload = {key: value for key, value in confirmation.items() if key != "confirmation_sha256"}
    if declared_confirmation_hash != canonical_sha256(confirmation_payload):
        raise SdlcError("迁移分类确认的 confirmation_sha256 不正确。")
    records = confirmation.get("records")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise SdlcError("迁移分类确认 records 必须是对象数组。")
    paths_in_confirmation = [str(item.get("source_path") or "") for item in records]
    if len(set(paths_in_confirmation)) != len(paths_in_confirmation):
        raise SdlcError("同一来源只能分类一次。", exit_code=1)

    scan = scan_legacy_change_records(paths)
    if confirmation.get("scan_sha256") != scan["scan_sha256"]:
        raise SdlcError("旧变更来源清单已经变化，请重新执行 change-migrate scan。", exit_code=1)
    scanned_by_path = {str(item["source_path"]): item for item in scan["records"]}  # type: ignore[index]
    if set(paths_in_confirmation) != set(scanned_by_path):
        raise SdlcError(
            "迁移分类确认必须逐条覆盖本次扫描清单，不能漏项或增加清单外来源。",
            exit_code=1,
        )

    validated: list[dict[str, object]] = []
    for raw_record in records:
        record = dict(raw_record)
        source_path = str(record.get("source_path") or "")
        scanned = scanned_by_path[source_path]
        for field in ("source_id", "source_kind", "source_path", "source_sha256"):
            if record.get(field) != scanned.get(field):
                raise SdlcError(f"{source_path} 的 {field} 与只读扫描结果不一致。")
        _validate_classified_record(paths, record)
        entry_payload = dict(record)
        record["entry_sha256"] = canonical_sha256(entry_payload)
        validated.append(record)
    return scan, validated


def _atomic_write_registry(
    path: Path,
    document: dict[str, object],
    interruption_hook: Callable[[str], None] | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json_text(document))
            handle.flush()
            os.fsync(handle.fileno())
        if interruption_hook is not None:
            interruption_hook(INTERRUPT_BEFORE_REGISTRY_PUBLISH)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def register_change_migration(
    paths: ProjectPaths,
    confirmation_path: str | Path,
    *,
    interruption_hook: Callable[[str], None] | None = None,
) -> dict[str, object]:
    confirmation_file = _controlled_existing_file(paths, str(confirmation_path), label="迁移分类确认文件")
    confirmation = _load_json_file(confirmation_file, label="迁移分类确认文件")
    scan, records = _validate_confirmation(paths, confirmation)
    registry: dict[str, object] = {
        "schema_version": REGISTRY_SCHEMA,
        "source_scan_sha256": scan["scan_sha256"],
        "confirmation_sha256": confirmation["confirmation_sha256"],
        "records": records,
    }
    registry["registry_sha256"] = canonical_sha256(registry)

    target = migration_registry_path(paths)
    if target.exists():
        existing = _load_json_file(target, label="旧变更迁移登记表")
        if existing == registry:
            return {
                "registered_count": len(records),
                "registry_path": _project_relative(paths, target),
                "registry_sha256": registry["registry_sha256"],
                "idempotent": True,
            }
    _atomic_write_registry(target, registry, interruption_hook)
    return {
        "registered_count": len(records),
        "registry_path": _project_relative(paths, target),
        "registry_sha256": registry["registry_sha256"],
        "idempotent": False,
    }


def _load_current_registry(paths: ProjectPaths) -> tuple[dict[str, object], dict[str, object]]:
    scan = scan_legacy_change_records(paths)
    if not scan["records"]:
        return scan, {}
    target = migration_registry_path(paths)
    if not target.exists():
        raise SdlcError(
            "发现尚未分类的旧变更记录，请先执行 change-migrate scan 和 confirm。",
            exit_code=1,
        )
    registry = _load_json_file(target, label="旧变更迁移登记表")
    if registry.get("schema_version") != REGISTRY_SCHEMA:
        raise SdlcError("旧变更迁移登记表版本不受支持。")
    declared_hash = registry.get("registry_sha256")
    payload = {key: value for key, value in registry.items() if key != "registry_sha256"}
    if declared_hash != canonical_sha256(payload):
        raise SdlcError("旧变更迁移登记表哈希不正确。")
    if registry.get("source_scan_sha256") != scan["scan_sha256"]:
        raise SdlcError(
            "旧变更迁移分类已经失效，来源文件发生变化，请重新扫描和确认。",
            exit_code=1,
        )
    records = registry.get("records")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise SdlcError("旧变更迁移登记表 records 不完整。")
    scanned_paths = {str(item["source_path"]) for item in scan["records"]}  # type: ignore[index]
    registered_paths = {str(item.get("source_path") or "") for item in records}
    if len(registered_paths) != len(records) or registered_paths != scanned_paths:
        raise SdlcError("旧变更迁移登记表没有为每个来源保留唯一分类。")
    for raw_record in records:
        record = dict(raw_record)
        entry_hash = record.pop("entry_sha256", None)
        if entry_hash != canonical_sha256(record):
            raise SdlcError("旧变更迁移登记表的单条分类哈希不正确。")
        try:
            _validate_classified_record(paths, record)
        except SdlcError as exc:
            raise SdlcError(
                f"旧变更迁移分类已经失效，{record.get('source_id', '未知来源')} 的结构化结果不再有效：{exc.message}",
                exit_code=1,
            ) from exc
    return scan, registry


def ensure_change_migration_allows_progress(
    paths: ProjectPaths,
    *,
    allow_blocked_rebuild: bool = False,
) -> None:
    """新变更流程只消费分类状态；不会读取旧记录正文补业务关系。"""

    _scan, registry = _load_current_registry(paths)
    if not registry:
        return
    records = registry.get("records")
    if not isinstance(records, list):
        raise SdlcError("旧变更迁移登记表 records 不完整。")
    blocked = [
        str(item.get("source_id") or item.get("source_path") or "未知来源")
        for item in records
        if isinstance(item, dict) and item.get("classification") == "blocked-rebuild"
    ]
    if blocked and not allow_blocked_rebuild:
        raise SdlcError(
            "旧变更仍处于 blocked-rebuild，必须先用所需原始资料重建正式变更包："
            + "、".join(blocked)
            + "。",
            exit_code=1,
        )


__all__ = [
    "CONFIRMATION_SCHEMA",
    "INTERRUPT_BEFORE_REGISTRY_PUBLISH",
    "REGISTRY_SCHEMA",
    "SCAN_SCHEMA",
    "ensure_change_migration_allows_progress",
    "migration_registry_path",
    "register_change_migration",
    "scan_legacy_change_records",
]
