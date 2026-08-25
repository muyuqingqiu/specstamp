from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from codex_sdlc.core.code_evidence import (
    FILESYSTEM_IDENTITY_CONTRACT,
    assess_task_planning_code_evidence,
    capture_code_evidence,
    effective_rule_paths,
    task_planning_identity,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)


TASK_COVERAGE_SCHEMA = "task-coverage.v1"
_TASK_FILE_PATTERN = re.compile(
    r"^(?P<client_key>[a-z0-9][a-z0-9._-]{0,127})\.task\.v2\.json$"
)
_TASK_REFERENCE_PATTERN = re.compile(
    r"^(?:@client:[a-z0-9][a-z0-9._-]{0,127}|T-[0-9]{3,})$"
)
_TEST_REFERENCE_PATTERN = re.compile(
    r"^(?P<task_ref>@client:[a-z0-9][a-z0-9._-]{0,127}|T-[0-9]{3,})"
    r"#automated_tests/(?P<index>[0-9]+)$"
)
_DESIGN_ID_PATTERN = re.compile(
    r"^(?:DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}$"
)
_SOURCE_TYPE_BY_PREFIX = {
    "FR": "functional_requirement",
    "AC": "acceptance_criterion",
    "CHG": "effective_change",
    "DATA": "design_artifact",
    "API": "design_artifact",
    "PAGE": "design_artifact",
    "COMP": "design_artifact",
    "SAFE": "design_artifact",
    "DEPLOY": "design_artifact",
    "FIELD": "design_artifact",
    "SPEC": "design_artifact",
}
_DEPENDENCY_EVIDENCE_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "requirements.txt",
    "poetry.lock",
    "uv.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle",
    "gradle.lockfile",
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"任务规划 JSON 包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"任务规划 JSON 包含非标准数字：{value}。", exit_code=1)


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise SdlcError(f"{label}必须是普通 JSON 文件：{source}。", exit_code=1)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是合法 JSON：{source.name}。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是对象：{source.name}。", exit_code=1)
    canonical_json_bytes(document)
    return document


def _task_documents(tasks_dir: Path) -> list[dict[str, object]]:
    source = Path(tasks_dir)
    if source.is_symlink() or not source.is_dir():
        raise SdlcError("任务目录必须是普通目录。", exit_code=1)
    result: list[dict[str, object]] = []
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        match = _TASK_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            raise SdlcError(
                f"任务文件名必须使用 <client_key>.task.v2.json：{path.name}。",
                exit_code=1,
            )
        document = _read_json(path, label="task.v2")
        if document.get("client_key") != match.group("client_key"):
            raise SdlcError(f"{path.name} 与任务 client_key 不一致。", exit_code=1)
        result.append(document)
    if not result:
        raise SdlcError("任务目录至少需要一份 task.v2 文件。", exit_code=1)
    return result


def _task_contract(task: Mapping[str, object]) -> Mapping[str, object]:
    contract = task.get("task_contract")
    return contract if isinstance(contract, Mapping) else task


def _task_aliases(tasks: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    aliases: dict[str, Mapping[str, object]] = {}
    for task in tasks:
        contract = _task_contract(task)
        client_key = str(contract.get("client_key") or "")
        task_id = str(contract.get("task_id") or task.get("task_id") or "")
        for alias in (
            f"@client:{client_key}" if client_key else "",
            task_id,
        ):
            if not alias:
                continue
            if alias in aliases and aliases[alias] is not task:
                raise SdlcError(f"任务覆盖使用了重复任务引用：{alias}。", exit_code=1)
            aliases[alias] = task
    return aliases


def _source_type(source_id: str) -> str:
    prefix = source_id.split("-", 1)[0]
    return _SOURCE_TYPE_BY_PREFIX.get(prefix, "")


def _no_development_sources(
    value: object,
) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        raise SdlcError("task-coverage.v1 的 no_development_items 必须是数组。", exit_code=1)
    result: dict[str, dict[str, object]] = {}
    for raw_item in value:
        if not isinstance(raw_item, Mapping):
            raise SdlcError("无需开发项必须是对象，并写明原因和依据。", exit_code=1)
        item = deepcopy(dict(raw_item))
        source_id = str(item.get("source_id") or "")
        reason = str(item.get("reason") or "").strip()
        basis_refs = item.get("basis_refs")
        if (
            not source_id
            or not reason
            or not isinstance(basis_refs, list)
            or not basis_refs
            or any(not str(ref).strip() for ref in basis_refs)
        ):
            raise SdlcError(
                f"无需开发项 {source_id or '未编号来源'} 必须写明原因和正式依据。",
                exit_code=1,
            )
        if item.get("source_type") != _source_type(source_id):
            raise SdlcError(f"无需开发项 {source_id} 的来源类型不一致。", exit_code=1)
        if source_id in result:
            raise SdlcError(f"无需开发项重复：{source_id}。", exit_code=1)
        result[source_id] = item
    return result


def _validate_source_set(
    *,
    field_name: str,
    coverage: Mapping[str, object],
    expected: set[str],
    no_development: Mapping[str, object],
) -> None:
    raw_mapping = coverage.get(field_name)
    if not isinstance(raw_mapping, Mapping):
        raise SdlcError(f"task-coverage.v1 的 {field_name} 必须是对象。", exit_code=1)
    covered = {str(source_id) for source_id in raw_mapping}
    excused = {
        source_id
        for source_id in no_development
        if _source_type(source_id)
        == {
            "functional_requirements": "functional_requirement",
            "design_artifacts": "design_artifact",
            "acceptance_criteria": "acceptance_criterion",
            "effective_changes": "effective_change",
        }[field_name]
    }
    overlap = sorted(covered.intersection(excused))
    if overlap:
        raise SdlcError(
            "来源不能同时分配任务和标记无需开发：" + "、".join(overlap) + "。",
            exit_code=1,
        )
    missing = sorted(expected - covered - excused)
    extra = sorted((covered | excused) - expected)
    if missing:
        raise SdlcError(
            f"{field_name} 缺少主责任或无需开发依据：" + "、".join(missing) + "。",
            exit_code=1,
        )
    if extra:
        raise SdlcError(
            f"{field_name} 包含不在正式来源中的编号：" + "、".join(extra) + "。",
            exit_code=1,
        )


def _reference_values(task: Mapping[str, object], field: str) -> set[str]:
    contract = _task_contract(task)
    raw = contract.get(field)
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def _task_automated_tests(task: Mapping[str, object]) -> list[object]:
    contract = _task_contract(task)
    raw = contract.get("automated_tests")
    if isinstance(raw, list):
        return raw
    legacy = task.get("test_items")
    return list(legacy) if isinstance(legacy, list) else []


def _validate_mapping_tasks(
    *,
    coverage: Mapping[str, object],
    tasks_by_ref: Mapping[str, Mapping[str, object]],
    requirement_id: str,
) -> None:
    source_fields = (
        ("functional_requirements", "requirement_refs"),
        ("design_artifacts", "design_refs"),
        ("acceptance_criteria", "acceptance_refs"),
        ("effective_changes", "change_refs"),
    )
    for field, task_field in source_fields:
        raw_mapping = coverage[field]
        if not isinstance(raw_mapping, Mapping):
            continue
        for source_id, raw_entry in raw_mapping.items():
            if not isinstance(raw_entry, Mapping):
                raise SdlcError(f"{source_id} 的任务覆盖必须是对象。", exit_code=1)
            raw_tasks = raw_entry.get("tasks")
            if not isinstance(raw_tasks, list) or not raw_tasks:
                raise SdlcError(f"{source_id} 必须分配至少一个主责任任务。", exit_code=1)
            primary = str(raw_entry.get("primary_task") or raw_tasks[0])
            if primary not in [str(item) for item in raw_tasks]:
                raise SdlcError(f"{source_id} 的主责任任务不在 tasks 中。", exit_code=1)
            for raw_task_ref in raw_tasks:
                task_ref = str(raw_task_ref)
                if _TASK_REFERENCE_PATTERN.fullmatch(task_ref) is None:
                    raise SdlcError(f"{source_id} 的任务引用格式无效：{task_ref}。", exit_code=1)
                task = tasks_by_ref.get(task_ref)
                if task is None:
                    raise SdlcError(f"{source_id} 引用了不存在的任务：{task_ref}。", exit_code=1)
                contract = _task_contract(task)
                # 跨需求前置任务只核对真实存在；当前需求任务还必须在 task.v2 里显式引用来源。
                if (
                    str(contract.get("requirement_id") or task.get("requirement_id") or "")
                    == requirement_id
                    and source_id not in _reference_values(task, task_field)
                ):
                    raise SdlcError(
                        f"{source_id} 分配给 {task_ref}，但对应 task.v2 没有显式引用该来源。",
                        exit_code=1,
                    )

    raw_acceptance = coverage["acceptance_criteria"]
    if not isinstance(raw_acceptance, Mapping):
        return
    for acceptance_id, raw_entry in raw_acceptance.items():
        if not isinstance(raw_entry, Mapping):
            continue
        covered_tasks = {str(item) for item in raw_entry.get("tasks", [])}  # type: ignore[arg-type]
        test_refs = raw_entry.get("test_refs")
        if not isinstance(test_refs, list) or not test_refs:
            raise SdlcError(f"{acceptance_id} 必须绑定具体任务测试路径。", exit_code=1)
        for raw_test_ref in test_refs:
            test_ref = str(raw_test_ref)
            match = _TEST_REFERENCE_PATTERN.fullmatch(test_ref)
            if match is None:
                raise SdlcError(f"{acceptance_id} 的测试路径无效：{test_ref}。", exit_code=1)
            task_ref = match.group("task_ref")
            task = tasks_by_ref.get(task_ref)
            index = int(match.group("index"))
            if (
                task_ref not in covered_tasks
                or task is None
                or index >= len(_task_automated_tests(task))
            ):
                raise SdlcError(
                    f"{acceptance_id} 的测试路径没有指向已覆盖任务的真实自动测试：{test_ref}。",
                    exit_code=1,
                )


def validate_task_coverage_contract(
    value: Mapping[str, object],
    *,
    tasks: Iterable[Mapping[str, object]],
    functional_requirement_ids: set[str],
    design_artifact_ids: set[str],
    acceptance_criterion_ids: set[str],
    effective_change_ids: set[str],
    basis_reference_ids: set[str] | None = None,
) -> dict[str, object]:
    coverage = deepcopy(dict(value))
    no_development = _no_development_sources(coverage.get("no_development_items"))
    raw_acceptance = coverage.get("acceptance_criteria")
    if isinstance(raw_acceptance, Mapping):
        for acceptance_id, raw_entry in raw_acceptance.items():
            if not isinstance(raw_entry, Mapping):
                continue
            raw_test_refs = raw_entry.get("test_refs")
            if isinstance(raw_test_refs, list):
                for test_ref in raw_test_refs:
                    if _TEST_REFERENCE_PATTERN.fullmatch(str(test_ref)) is None:
                        raise SdlcError(
                            f"{acceptance_id} 的测试路径必须指向 automated_tests 下的具体序号。",
                            exit_code=1,
                        )
    validate_schema_document(coverage, schema_name=TASK_COVERAGE_SCHEMA)
    requirement_id = str(coverage.get("requirement_id") or "")
    expected_sets = {
        "functional_requirements": set(functional_requirement_ids),
        "design_artifacts": set(design_artifact_ids),
        "acceptance_criteria": set(acceptance_criterion_ids),
        "effective_changes": set(effective_change_ids),
    }
    for field_name, expected in expected_sets.items():
        _validate_source_set(
            field_name=field_name,
            coverage=coverage,
            expected=expected,
            no_development=no_development,
        )
    if basis_reference_ids is not None:
        for source_id, item in no_development.items():
            unknown_basis = sorted(
                str(reference)
                for reference in item["basis_refs"]  # type: ignore[index]
                if str(reference) not in basis_reference_ids
            )
            if unknown_basis:
                raise SdlcError(
                    f"无需开发项 {source_id} 引用了不存在的正式依据："
                    + "、".join(unknown_basis)
                    + "。",
                    exit_code=1,
                )
    task_list = list(tasks)
    _validate_mapping_tasks(
        coverage=coverage,
        tasks_by_ref=_task_aliases(task_list),
        requirement_id=requirement_id,
    )
    return coverage


def _formal_source_ids(
    entries: Mapping[str, object],
    effective_change_ids: set[str],
) -> dict[str, set[str]]:
    identifiers = {str(key).split("#", 1)[0] for key in entries}
    return {
        "functional_requirement_ids": {
            identifier for identifier in identifiers if identifier.startswith("FR-")
        },
        "design_artifact_ids": {
            identifier for identifier in identifiers if _DESIGN_ID_PATTERN.fullmatch(identifier)
        },
        "acceptance_criterion_ids": {
            identifier for identifier in identifiers if identifier.startswith("AC-")
        },
        "effective_change_ids": set(effective_change_ids),
    }


def _selection_from_plan(
    paths: ProjectPaths,
    plan: Mapping[str, object],
) -> Mapping[str, object] | None:
    raw = plan.get("code_evidence")
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        if raw.get("schema_version") == "code-evidence.v1":
            raise SdlcError("task-plan.v2 不能自报已经生成的代码证据和哈希。", exit_code=1)
        return raw
    if isinstance(raw, str):
        selection_path = resolve_project_path(paths.root, raw, must_exist=True)
        return _read_json(selection_path, label="任务规划代码证据选择")
    raise SdlcError("task-plan.v2 的 code_evidence 必须是选择对象或项目相对路径。", exit_code=1)


def _default_task_planning_selection(
    paths: ProjectPaths,
    requirement_root: Path,
    tasks: Iterable[Mapping[str, object]],
    existing_tasks: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    """缺少单独选择文件时，只使用 CLI 确实读取的规则、任务路径和正式上游文件。"""

    task_documents = list(tasks)
    existing_task_documents = list(existing_tasks)
    identity_contract = task_planning_identity(paths)["identity_contract"]
    record_missing_files = identity_contract == FILESYSTEM_IDENTITY_CONTRACT
    rules = effective_rule_paths(paths)
    dependencies = [
        path
        for path in _DEPENDENCY_EVIDENCE_FILES
        if (paths.root / path).is_file()
    ]
    upstream_outputs = [
        path.relative_to(paths.root).as_posix()
        for path in (
            requirement_root / "reference-index.v1.json",
            requirement_root / "original" / "formal.v3.json",
        )
        if path.is_file()
    ]
    upstream_outputs.extend(
        _completed_prerequisite_outputs(
            paths,
            tasks=task_documents,
            existing_tasks=existing_task_documents,
            record_missing_files=record_missing_files,
        )
    )
    upstream_outputs = sorted(
        {
            _selection_item_path(item): item
            for item in upstream_outputs
        }.values(),
        key=_selection_item_path,
    )
    occupied = set(rules) | set(dependencies) | {
        _selection_item_path(item) for item in upstream_outputs
    }
    code_files = [
        item
        for item in _required_task_code_files(
            paths,
            task_documents,
            record_missing_files=record_missing_files,
        )
        if item["path"] not in occupied
    ]
    return {
        "purpose": "task_planning",
        "rules": rules,
        "dependencies": dependencies,
        "code_files": code_files,
        "upstream_outputs": upstream_outputs,
    }


def _selection_item_path(item: object) -> str:
    if isinstance(item, Mapping):
        return str(item.get("path") or "")
    return str(item)


def _project_files_for_scope(
    paths: ProjectPaths,
    raw_path: object,
    *,
    record_missing_files: bool,
) -> list[dict[str, str]]:
    path_text = str(raw_path or "").strip().rstrip("/")
    if not path_text or Path(path_text).is_absolute() or ".." in Path(path_text).parts:
        raise SdlcError(
            f"task.v2 read_paths 不是合法项目相对路径：{path_text or '空路径'}。",
            exit_code=1,
        )
    target = paths.root / path_text
    if target.is_symlink() or not target.exists():
        if record_missing_files and not target.is_symlink():
            return [{"path": Path(path_text).as_posix(), "state": "missing"}]
        raise SdlcError(
            f"任务规划代码证据无法核对 task.v2 read_paths：{path_text}。",
            exit_code=1,
        )
    candidates = [target] if target.is_file() else sorted(target.rglob("*"))
    result: list[dict[str, str]] = []
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            candidate.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        result.append({"path": candidate.relative_to(paths.root).as_posix()})
    if not result:
        raise SdlcError(
            f"任务规划代码证据没有找到可核对的 UTF-8 代码文件：{path_text}。",
            exit_code=1,
        )
    return result


def _required_task_code_files(
    paths: ProjectPaths,
    tasks: Iterable[Mapping[str, object]],
    *,
    record_missing_files: bool,
) -> list[dict[str, str]]:
    by_path: dict[str, dict[str, str]] = {}
    for raw_task in tasks:
        task = _task_contract(raw_task)
        requirement_refs = task.get("requirement_refs")
        reason_ref = (
            str(requirement_refs[0])
            if isinstance(requirement_refs, list) and requirement_refs
            else ""
        )
        scope = task.get("code_scope")
        read_paths = scope.get("read_paths") if isinstance(scope, Mapping) else []
        if not isinstance(read_paths, list):
            raise SdlcError("task.v2 的 code_scope.read_paths 必须是数组。", exit_code=1)
        for raw_path in read_paths:
            if not reason_ref:
                raise SdlcError(
                    "task.v2 的代码读取路径缺少可追溯的 FR 引用。",
                    exit_code=1,
                )
            for item in _project_files_for_scope(
                paths,
                raw_path,
                record_missing_files=record_missing_files,
            ):
                path = item["path"]
                by_path.setdefault(
                    path,
                    {
                        **item,
                        "reason_ref": reason_ref,
                    },
                )
    if not by_path:
        raise SdlcError(
            "任务规划代码证据至少要绑定一个 task.v2 实际读取的代码文件。",
            exit_code=1,
        )
    return [by_path[path] for path in sorted(by_path)]


def _completed_prerequisite_outputs(
    paths: ProjectPaths,
    *,
    tasks: Iterable[Mapping[str, object]],
    existing_tasks: Iterable[Mapping[str, object]],
    record_missing_files: bool,
) -> list[object]:
    dependency_ids: set[str] = set()
    for raw_task in tasks:
        raw_dependencies = _task_contract(raw_task).get("depends_on")
        if not isinstance(raw_dependencies, list):
            raise SdlcError("task.v2 的 depends_on 必须是数组。", exit_code=1)
        dependency_ids.update(
            str(dependency)
            for dependency in raw_dependencies
            if re.fullmatch(r"T-[0-9]{3,}", str(dependency))
        )
    by_id = {
        str(task.get("task_id") or ""): task
        for task in existing_tasks
        if str(task.get("task_id") or "")
    }
    completed_statuses = {"done", "closed", "accepted", "verified"}
    result: dict[str, object] = {}
    for dependency_id in sorted(dependency_ids):
        task = by_id.get(dependency_id)
        if task is None or str(task.get("status") or "") not in completed_statuses:
            continue
        raw_outputs = [
            *(
                task.get("output_files", [])
                if isinstance(task.get("output_files"), list)
                else []
            ),
            *(
                task.get("changed_files", [])
                if isinstance(task.get("changed_files"), list)
                else []
            ),
        ]
        task_outputs: dict[str, object] = {}
        for raw_output in raw_outputs:
            for item in _project_files_for_scope(
                paths,
                raw_output,
                record_missing_files=record_missing_files,
            ):
                task_outputs.setdefault(item["path"], item)
        if not task_outputs:
            if not record_missing_files:
                raise SdlcError(
                    f"已完成前置任务 {dependency_id} 没有可核对的真实交付文件。",
                    exit_code=1,
                )
            missing_path = f"@task-output/{dependency_id}"
            task_outputs[missing_path] = {
                "path": missing_path,
                "state": "missing",
            }
        result.update(task_outputs)
    return [result[path] for path in sorted(result)]


def _selection_paths(
    selection: Mapping[str, object],
    group: str,
) -> set[str]:
    raw_items = selection.get(group)
    if not isinstance(raw_items, list):
        raise SdlcError(f"代码证据 {group} 必须是数组。", exit_code=1)
    result: set[str] = set()
    for raw_item in raw_items:
        if isinstance(raw_item, Mapping):
            if group not in {"code_files", "upstream_outputs"}:
                raise SdlcError(f"代码证据 {group} 项必须是路径字符串。", exit_code=1)
            if group == "code_files" and not str(raw_item.get("reason_ref") or ""):
                raise SdlcError("代码证据 code_files 项必须是对象。", exit_code=1)
            result.add(str(raw_item.get("path") or ""))
        else:
            result.add(str(raw_item))
    return result


def _validate_complete_selection(
    selection: Mapping[str, object],
    required: Mapping[str, object],
) -> None:
    missing: list[str] = []
    for group in ("rules", "dependencies", "code_files", "upstream_outputs"):
        required_paths = _selection_paths(required, group)
        selected_paths = _selection_paths(selection, group)
        missing.extend(
            f"{group}:{path}"
            for path in sorted(required_paths - selected_paths)
        )
    if missing:
        raise SdlcError(
            "任务规划代码证据选择缺少实际读取项：" + "、".join(missing) + "。",
            exit_code=1,
        )


def _existing_evidence(requirement_root: Path) -> Mapping[str, object] | None:
    task_root = requirement_root / "tasks"
    if task_root.is_symlink():
        raise SdlcError("现有正式任务目录不能是符号链接。", exit_code=1)
    plan_path = task_root / "task-plan.v2.json"
    if not plan_path.is_file() or plan_path.is_symlink():
        return None
    existing_plan = _read_json(plan_path, label="现有正式 task-plan.v2")
    evidence = existing_plan.get("code_evidence")
    return evidence if isinstance(evidence, Mapping) else None


def _submission_snapshot(
    *,
    plan: Mapping[str, object],
    tasks: Iterable[Mapping[str, object]],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    return {
        "plan_sha256": canonical_sha256(plan),
        "tasks": {
            str(task.get("client_key") or ""): canonical_sha256(task)
            for task in tasks
        },
        "coverage_sha256": canonical_sha256(coverage),
    }


def verify_task_planning_input_snapshot(
    paths: ProjectPaths,
    *,
    requirement_root: Path,
    plan_file: Path,
    tasks_dir: Path,
    coverage_file: Path,
    expected_snapshot: Mapping[str, object],
    expected_reference_index_sha256: str,
    expected_events_sha256: str,
    evidence: Mapping[str, object],
) -> None:
    """由锁内导入入口触发，确认提交所读输入仍是刚才完整校验的同一份内容。"""

    try:
        current_snapshot = _submission_snapshot(
            plan=_read_json(plan_file, label="task-plan.v2"),
            tasks=_task_documents(tasks_dir),
            coverage=_read_json(coverage_file, label="task-coverage.v1"),
        )
        evidence_status = assess_task_planning_code_evidence(
            paths,
            evidence,
            tasks=[],
        )
        unchanged = (
            current_snapshot == dict(expected_snapshot)
            and sha256_file(requirement_root / "reference-index.v1.json")
            == expected_reference_index_sha256
            and sha256_file(paths.events_file) == expected_events_sha256
            and evidence_status.get("status") == "current"
        )
    except SdlcError as exc:
        raise SdlcError(
            "任务规划输入在完整校验后发生变化，已拒绝提交。",
            exit_code=1,
        ) from exc
    if not unchanged:
        raise SdlcError(
            "任务规划输入在完整校验后发生变化，已拒绝提交。",
            exit_code=1,
        )


def prepare_task_planning_documents(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    requirement_root: Path,
    plan_file: Path,
    tasks_dir: Path,
    coverage_file: Path,
    effective_change_ids: set[str],
    existing_tasks: Iterable[Mapping[str, object]] = (),
    expected_events_sha256: str | None = None,
) -> dict[str, object]:
    """在任务事务写入前固定覆盖全集，并把模型选择转换成真实规划证据。"""

    try:
        resolved_requirement_root = requirement_root.resolve(strict=True)
        resolved_requirement_root.relative_to(
            paths.requirements_dir.resolve(strict=True)
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("当前正式需求目录不存在或越过项目边界。", exit_code=1) from exc
    if requirement_root.is_symlink() or not resolved_requirement_root.is_dir():
        raise SdlcError("当前正式需求目录必须是项目内普通目录。", exit_code=1)
    requirement_root = resolved_requirement_root
    events_sha256 = expected_events_sha256 or sha256_file(paths.events_file)
    if sha256_file(paths.events_file) != events_sha256:
        raise SdlcError(
            "任务规划状态在完整校验前发生变化，已拒绝提交。",
            exit_code=1,
        )
    plan = _read_json(plan_file, label="task-plan.v2")
    source_plan = deepcopy(plan)
    coverage = _read_json(coverage_file, label="task-coverage.v1")
    task_documents = _task_documents(tasks_dir)
    existing_task_documents = list(existing_tasks)
    source_snapshot = _submission_snapshot(
        plan=source_plan,
        tasks=task_documents,
        coverage=coverage,
    )
    if plan.get("requirement_id") != requirement_id:
        raise SdlcError("task-plan.v2 的 requirement_id 与命令目标不一致。", exit_code=1)
    if coverage.get("requirement_id") != requirement_id:
        raise SdlcError("task-coverage.v1 的 requirement_id 与命令目标不一致。", exit_code=1)
    reference_index_path = requirement_root / "reference-index.v1.json"
    reference_index_sha256 = sha256_file(reference_index_path)
    reference_index = _read_json(
        reference_index_path,
        label="正式引用索引",
    )
    if sha256_file(reference_index_path) != reference_index_sha256:
        raise SdlcError(
            "正式引用索引在完整校验时发生变化，已拒绝提交。",
            exit_code=1,
        )
    if (
        reference_index.get("schema_version") != "reference-index.v1"
        or reference_index.get("requirement_id") != requirement_id
        or not isinstance(reference_index.get("entries"), Mapping)
    ):
        raise SdlcError("正式引用索引缺少当前需求的结构化 entries。", exit_code=1)
    source_sets = _formal_source_ids(
        reference_index["entries"],  # type: ignore[arg-type,index]
        effective_change_ids,
    )
    source_sets["basis_reference_ids"] = {
        *[str(key) for key in reference_index["entries"]],  # type: ignore[union-attr,index]
        *effective_change_ids,
    }
    normalized_coverage = validate_task_coverage_contract(
        coverage,
        tasks=[*task_documents, *existing_task_documents],
        **source_sets,
    )

    required_selection = _default_task_planning_selection(
        paths,
        requirement_root,
        task_documents,
        existing_task_documents,
    )
    selection = _selection_from_plan(paths, plan)
    if selection is None:
        selection = required_selection
    else:
        _validate_complete_selection(selection, required_selection)
    plan["code_evidence"] = capture_code_evidence(
        paths,
        owner_id=requirement_id,
        selection=selection,
        reuse_evidence=_existing_evidence(requirement_root),
    )
    supplied_input_hashes = plan.get("input_hashes")
    if supplied_input_hashes is not None and not isinstance(
        supplied_input_hashes,
        Mapping,
    ):
        raise SdlcError("task-plan.v2 的 input_hashes 必须是对象。", exit_code=1)
    supplied_reference_hash = str(
        (supplied_input_hashes or {}).get("reference_index") or ""  # type: ignore[union-attr]
    )
    if (
        supplied_reference_hash
        and supplied_reference_hash != reference_index_sha256
    ):
        raise SdlcError(
            "task-plan.v2 的正式引用索引哈希与当前文件不一致。",
            exit_code=1,
        )
    plan["input_hashes"] = {
        **dict(supplied_input_hashes or {}),
        "reference_index": reference_index_sha256,
    }
    if sha256_file(paths.events_file) != events_sha256:
        raise SdlcError(
            "任务规划状态在完整校验时发生变化，已拒绝提交。",
            exit_code=1,
        )
    return {
        "plan": plan,
        "tasks": task_documents,
        "coverage": normalized_coverage,
        "source_snapshot": source_snapshot,
        "reference_index_sha256": reference_index_sha256,
        "events_sha256": events_sha256,
    }


def apply_task_coverage_to_state_tasks(
    tasks: list[dict[str, object]],
    coverage: Mapping[str, object],
) -> None:
    """状态投影只消费正式覆盖合同，不再从旧 formal_* 字段反推关系。"""

    by_id = {str(task.get("task_id") or ""): task for task in tasks}
    for task in tasks:
        task["coverage_points"] = []
        task["coverage_design_refs"] = []
        task["coverage_change_ids"] = []
        task["coverage_acceptance"] = []
        task["task_test_refs"] = []

    field_targets = (
        ("functional_requirements", "coverage_points"),
        ("design_artifacts", "coverage_design_refs"),
        ("effective_changes", "coverage_change_ids"),
        ("acceptance_criteria", "coverage_acceptance"),
    )
    for coverage_field, task_field in field_targets:
        raw_mapping = coverage.get(coverage_field)
        if not isinstance(raw_mapping, Mapping):
            raise SdlcError(f"正式覆盖合同缺少 {coverage_field}。", exit_code=1)
        for source_id in sorted(raw_mapping):
            entry = raw_mapping[source_id]
            if not isinstance(entry, Mapping):
                raise SdlcError(f"正式覆盖合同的 {source_id} 不是对象。", exit_code=1)
            for task_id in entry.get("tasks", []):  # type: ignore[union-attr]
                task = by_id.get(str(task_id))
                if task is None:
                    continue
                task[task_field].append(str(source_id))  # type: ignore[union-attr]
            if coverage_field == "acceptance_criteria":
                for test_ref in entry.get("test_refs", []):  # type: ignore[union-attr]
                    match = _TEST_REFERENCE_PATTERN.fullmatch(str(test_ref))
                    if match is None:
                        continue
                    task = by_id.get(match.group("task_ref"))
                    if task is not None:
                        task["task_test_refs"].append(str(test_ref))  # type: ignore[union-attr]


__all__ = [
    "TASK_COVERAGE_SCHEMA",
    "apply_task_coverage_to_state_tasks",
    "prepare_task_planning_documents",
    "verify_task_planning_input_snapshot",
    "validate_task_coverage_contract",
]
