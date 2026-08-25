from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from codex_sdlc.core.change_workspace import BASE_VERSION_PATHS
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    FORMAL_ID_PATTERN,
    TEMPORARY_REFERENCE_PATTERN,
    allocate_stable_ids,
    rewrite_temporary_references,
)
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    load_schema,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)


PACKAGE_SCHEMA = "change-package.v1"
PROJECTED_FILES = {
    "projected-requirement.v2.json": ("projected-requirement.v2", "requirement"),
    "projected-design.v2.json": ("projected-design.v2", "design"),
    "projected-test-matrix.v2.json": ("projected-test-matrix.v2", "test_matrix"),
    "projected-reference-index.v2.json": (
        "projected-reference-index.v2",
        "reference_index",
    ),
    "projected-task-plan.v2.json": ("projected-task-plan.v2", "task_plan"),
}
PACKAGE_FILE = "change-package.v1.json"
COMMITTED_FILE_NAMES = (PACKAGE_FILE, *PROJECTED_FILES)

_OBJECT_KEYS = {
    "GR": {
        "client_key",
        "title",
        "description",
        "type",
        "applies_to",
        "source_refs",
        "relations",
    },
    "FR": {
        "client_key",
        "title",
        "description",
        "elements",
        "flow",
        "facts",
        "rules",
        "constraints",
        "states_and_exceptions",
        "acceptance_criteria",
        "global_rule_refs",
        "source_refs",
        "material_refs",
        "depends_on",
        "out_of_scope",
        "relations",
    },
    "AC": {
        "client_key",
        "owner_fr_ref",
        "operation",
        "expected",
        "pass_standard",
        "source_refs",
        "relations",
    },
    "DES": {
        "schema_version",
        "client_key",
        "display_name",
        "material_id",
        "anchors",
        "applies_to",
        "supersedes",
    },
    "ART": {
        "schema_version",
        "type",
        "requirement_refs",
        "global_rule_refs",
        "material_refs",
        "depends_on",
        "content",
        "open_questions",
    },
}
_OPTIONAL_KEYS = {"DES": {"display_name", "supersedes"}}
_RELINK_KEYS = {
    "GR": {"applies_to", "source_refs", "relations"},
    "FR": {"global_rule_refs", "source_refs", "material_refs", "depends_on", "relations"},
    "AC": {"owner_fr_ref", "source_refs", "relations"},
    "DES": {"material_id", "applies_to", "supersedes"},
    "ART": {"requirement_refs", "global_rule_refs", "material_refs", "depends_on"},
}
_ARTIFACT_PREFIX = {
    "data": "DATA",
    "api": "API",
    "page": "PAGE",
    "component": "COMP",
    "security": "SAFE",
    "deployment": "DEPLOY",
    "field": "FIELD",
    "special": "SPEC",
}
_VERSION_PATTERN = re.compile(r"^(?P<name>[a-z-]+)\.v(?P<number>[0-9]+)$")
_EXPLICIT_REFERENCE_FIELDS = {
    "acceptance_refs",
    "applies_to",
    "basis_refs",
    "change_refs",
    "depends_on",
    "design_refs",
    "global_rule_refs",
    "material_id",
    "material_refs",
    "owner_fr_ref",
    "reason_refs",
    "relations",
    "replacement_refs",
    "requirement_refs",
    "source_refs",
    "supersedes",
    "target_ref",
    "technical_solution_refs",
}


@dataclass(frozen=True)
class PreparedChangePackage:
    """纯计算完成后的六份固定文件；服务层只负责锁、事务和事件。"""

    package_identity_sha256: str
    source_files_sha256: dict[str, str]
    committed_files_sha256: dict[str, str]
    committed_file_bytes: dict[str, bytes]
    id_mapping: dict[str, str]
    status_sha256: str
    material_manifest_sha256: str | None


def _decode_json(content: bytes, *, label: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SdlcError(f"{label}包含重复字段：{key}。")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise SdlcError(f"{label}包含非标准数字：{value}。")

    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except SdlcError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效的 UTF-8 JSON 对象。") from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是 JSON 对象。")
    return document


def _read_project_json(paths: ProjectPaths, raw_path: str, *, label: str) -> dict[str, object]:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise SdlcError(f"{label}必须是项目相对 POSIX 路径。")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != raw_path:
        raise SdlcError(f"{label}必须是项目相对 POSIX 路径。")
    lexical = paths.root / relative
    current = paths.root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"{label}不能穿过符号链接。")
    if lexical.is_symlink() or not lexical.is_file():
        raise SdlcError(f"{label}必须是项目内普通 JSON 文件。")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(paths.root.resolve(strict=True))
        content = lexical.read_bytes()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{label}无法安全读取。") from exc
    return _decode_json(content, label=label)


def load_source_documents(
    paths: ProjectPaths,
    *,
    package_path: str,
    projected_paths: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    if set(projected_paths) != set(PROJECTED_FILES):
        raise SdlcError("必须一次提供五份固定预计结果文件。")
    result = {
        PACKAGE_FILE: _read_project_json(paths, package_path, label="变更包"),
    }
    for name in PROJECTED_FILES:
        result[name] = _read_project_json(
            paths,
            projected_paths[name],
            label=name,
        )
    return result


def _validate_local_definition(document: Mapping[str, object], definition_name: str) -> None:
    schema = load_schema("requirement-split.v1")
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{definition_name}",
        "$defs": schema["$defs"],
    }
    errors = list(Draft202012Validator(wrapper).iter_errors(document))
    if errors:
        raise SdlcError(f"{definition_name} 完整对象字段不符合正式合同。")


def _validate_schema_definition(
    document: Mapping[str, object],
    *,
    schema_name: str,
    definition_name: str,
    label: str,
) -> None:
    """复用已有严格 Schema 的局部合同，避免变更入口另造一套宽松规则。"""

    schema = load_schema(schema_name)
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/$defs/{definition_name}",
        "$defs": schema["$defs"],
    }
    errors = list(Draft202012Validator(wrapper).iter_errors(document))
    if errors:
        raise SdlcError(f"{label}不符合正式合同。")


def _validate_next_value(kind: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SdlcError(f"{kind} next_value 必须是对象。")
    allowed = _OBJECT_KEYS[kind]
    required = allowed - _OPTIONAL_KEYS.get(kind, set())
    if set(value) - allowed or not required <= set(value):
        raise SdlcError(f"{kind} next_value 字段不完整或包含表外字段。")
    if kind == "GR":
        _validate_local_definition(value, "global_rule")
    elif kind == "FR":
        _validate_local_definition(value, "functional_requirement")
    elif kind == "AC":
        _validate_local_definition(value, "acceptance_criterion")
    elif kind == "DES":
        if value.get("schema_version") != "design-reference.v1":
            raise SdlcError("DES next_value 版本必须是 design-reference.v1。")
        # 预计对象已经分配正式 DES 编号，因此只补提交合同里的 draft_id 后复用原合同。
        candidate = deepcopy(value)
        candidate["draft_id"] = "DRAFT-000"
        _validate_schema_definition(
            candidate,
            schema_name="design-reference.v1",
            definition_name="submission",
            label="DES next_value ",
        )
    elif kind == "ART":
        if value.get("schema_version") != "design-artifact.v1" or value.get("type") not in _ARTIFACT_PREFIX:
            raise SdlcError("设计产物 next_value 版本或 type 不正确。")
        if value.get("open_questions"):
            raise SdlcError("设计产物仍有 open_questions，不能提交预计结果。")
        artifact_type = str(value["type"])
        content = value.get("content")
        if not isinstance(content, Mapping):
            raise SdlcError("设计产物 content 必须是对象。")
        _validate_schema_definition(
            content,
            schema_name="design-artifact.v1",
            definition_name=f"{artifact_type}_content",
            label=f"{artifact_type} 设计产物 content ",
        )
    return deepcopy(value)


def _kind_operations(package: Mapping[str, object]) -> tuple[tuple[str, list[dict[str, object]]], ...]:
    result: list[tuple[str, list[dict[str, object]]]] = []
    for field, kind in (
        ("global_rule_operations", "GR"),
        ("requirement_operations", "FR"),
        ("acceptance_operations", "AC"),
    ):
        raw = package.get(field)
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise SdlcError(f"{field} 必须是对象数组。")
        result.append((kind, [deepcopy(item) for item in raw]))
    design = package.get("design_operations")
    if not isinstance(design, list) or any(not isinstance(item, dict) for item in design):
        raise SdlcError("design_operations 必须是对象数组。")
    for kind, object_kind in (("DES", "design"), ("ART", "artifact")):
        result.append(
            (
                kind,
                [
                    {key: deepcopy(value) for key, value in item.items() if key != "object_kind"}
                    for item in design
                    if item.get("object_kind") == object_kind
                ],
            )
        )
    return tuple(result)


def _operation_new_values(operation: Mapping[str, object]) -> list[tuple[str, object]]:
    name = operation.get("operation")
    if name == "add":
        return [(str(operation.get("client_key") or ""), operation.get("next_value"))]
    if name == "split":
        outputs = operation.get("outputs")
        return [
            (str(item.get("client_key") or ""), item.get("next_value"))
            for item in outputs if isinstance(outputs, list) and isinstance(item, Mapping)
        ]
    if name == "merge":
        output = operation.get("output")
        if isinstance(output, Mapping):
            return [(str(output.get("client_key") or ""), output.get("next_value"))]
    return []


def _collect_temp_refs(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, str):
        match = TEMPORARY_REFERENCE_PATTERN.fullmatch(value)
        if match is not None:
            result.add(match.group("client_key"))
        elif "@client:" in value:
            raise SdlcError(f"临时引用必须完整占用字段值：{value}。")
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_temp_refs(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if "@client:" in key:
                raise SdlcError("字段名不能使用 @client: 临时引用。")
            result.update(_collect_temp_refs(item))
    return result


def _allocation_objects(package: Mapping[str, object]) -> list[AllocationObject]:
    pending: list[tuple[str, str, set[str]]] = []
    acceptance_order_dependencies: dict[str, set[str]] = {}
    # 同一新增 FR 内的 AC 按嵌套数组顺序占号。把前一项写成后一项的显式依赖，
    # 避免两个无依赖 AC 因 client_key 字典序得到另一套编号。
    for kind, operations in _kind_operations(package):
        if kind != "FR":
            continue
        for operation in operations:
            for _fr_key, raw_value in _operation_new_values(operation):
                if not isinstance(raw_value, Mapping):
                    continue
                acceptance = raw_value.get("acceptance_criteria")
                previous: str | None = None
                for item in acceptance if isinstance(acceptance, list) else []:
                    if not isinstance(item, Mapping) or not isinstance(item.get("client_key"), str):
                        continue
                    current = str(item["client_key"])
                    if previous is not None:
                        acceptance_order_dependencies.setdefault(current, set()).add(previous)
                    previous = current
    for kind, operations in _kind_operations(package):
        for operation in operations:
            for client_key, raw_value in _operation_new_values(operation):
                value = _validate_next_value(kind, raw_value)
                if kind != "ART" and value.get("client_key") != client_key:
                    raise SdlcError(f"{kind} 外层与 next_value.client_key 不一致。")
                prefix = _ARTIFACT_PREFIX[str(value["type"])] if kind == "ART" else kind
                dependencies = _collect_temp_refs(value) - {client_key}
                dependencies.update(acceptance_order_dependencies.get(client_key, set()))
                pending.append((client_key, prefix, dependencies))
    materials = package.get("material_operations")
    if not isinstance(materials, list):
        raise SdlcError("material_operations 必须是数组。")
    for item in materials:
        if isinstance(item, Mapping) and item.get("operation") == "add":
            pending.append((str(item["client_key"]), "MAT", set()))
    impacts = package.get("task_impacts")
    add_tasks = impacts.get("add") if isinstance(impacts, Mapping) else None
    if not isinstance(add_tasks, list):
        raise SdlcError("task_impacts.add 必须是数组。")
    for item in add_tasks:
        if not isinstance(item, Mapping):
            raise SdlcError("task_impacts.add 每项必须是对象。")
        next_value = item.get("next_value")
        client_key = str(item.get("client_key") or "")
        if not isinstance(next_value, dict) or next_value.get("client_key") != client_key:
            raise SdlcError("新增任务外层与 next_value.client_key 不一致。")
        validate_schema_document(next_value, schema_name="task.v2")
        dependencies = tuple(
            f"@client:{key}"
            for key in sorted(_collect_temp_refs(next_value) - {client_key})
        )
        pending.append(
            (
                client_key,
                "T",
                {item.removeprefix("@client:") for item in dependencies},
            )
        )
    objects = [
        AllocationObject(
            client_key,
            prefix,
            tuple(f"@client:{item}" for item in sorted(dependencies)),
        )
        for client_key, prefix, dependencies in pending
    ]
    if len({item.client_key for item in objects}) != len(objects):
        raise SdlcError("变更包内 client_key 必须全局唯一。")
    return objects


def collect_existing_ids(paths: ProjectPaths, events: Iterable[Mapping[str, object]]) -> set[str]:
    result: set[str] = set()
    requirements = paths.requirements_dir
    if requirements.is_dir() and not requirements.is_symlink():
        for path in sorted(requirements.glob("REQ-*")):
            if path.is_symlink() or not path.is_dir():
                continue
            # 正式设计编号可能只以带锚点的键出现在引用索引中，例如
            # DES-001#architecture。直接读取五份基础版本，才能把 DES-001
            # 本身识别为已经存在的合法编号，避免真实 start --file 项目误报跨包引用。
            for relative in dict.fromkeys(
                ("reference-index.v1.json", "tasks/task-plan.v2.json", *BASE_VERSION_PATHS.values())
            ):
                target = path / relative
                if target.is_file() and not target.is_symlink():
                    document = _decode_json(target.read_bytes(), label=relative)
                    result.update(_collect_formal_ids(document))
            # 另一个 CHG 可能已经发布六份文件、只差成功事件。项目锁内必须把这类
            # 已提交事务的映射计入占号，不能因为事件尚未补写就分配重复编号。
            for journal in sorted(path.glob("changes/CHG-*/.projection-transactions/*.json")):
                if journal.is_symlink() or not journal.is_file():
                    raise SdlcError("待核对的变更包事务日志不是普通文件。")
                transaction = _decode_json(journal.read_bytes(), label="待恢复变更包事务")
                hashes = transaction.get("committed_files_sha256")
                mapping = transaction.get("id_mapping")
                workspace_path = transaction.get("workspace_path")
                if not isinstance(hashes, Mapping) or not isinstance(mapping, Mapping) or not isinstance(workspace_path, str):
                    raise SdlcError("待恢复变更包事务缺少文件哈希或正式编号映射。")
                if any(not isinstance(name, str) or not isinstance(digest, str) for name, digest in hashes.items()):
                    raise SdlcError("待恢复变更包事务的文件哈希字段不正确。")
                workspace = resolve_project_path(paths.root, workspace_path)
                committed = all(
                    (workspace / name).is_file()
                    and not (workspace / name).is_symlink()
                    and sha256_file(workspace / name) == digest
                    for name, digest in hashes.items()
                ) and bool(hashes)
                if committed:
                    result.update(str(value) for value in mapping.values() if isinstance(value, str))
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") != "change_package_projected":
            continue
        payload = event.get("payload")
        mapping = payload.get("id_mapping") if isinstance(payload, Mapping) else None
        if isinstance(mapping, Mapping):
            result.update(str(item) for item in mapping.values() if isinstance(item, str))
    return result


def _collect_formal_ids(value: object) -> set[str]:
    result: set[str] = set()
    if isinstance(value, str) and FORMAL_ID_PATTERN.fullmatch(value):
        result.add(value)
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_formal_ids(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if FORMAL_ID_PATTERN.fullmatch(key):
                result.add(key)
            result.update(_collect_formal_ids(item))
    return result


def _collect_declared_formal_ids(value: object) -> set[str]:
    """只读取合同明确声明为引用的字段，普通说明文字不参与引用判定。"""

    result: set[str] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_collect_declared_formal_ids(item))
        return result
    if not isinstance(value, Mapping):
        return result
    for key, item in value.items():
        if key in _EXPLICIT_REFERENCE_FIELDS:
            result.update(_collect_reference_field_ids(key, item))
        elif isinstance(item, (Mapping, list)):
            result.update(_collect_declared_formal_ids(item))
    return result


def _collect_reference_field_ids(field: str, value: object) -> set[str]:
    """引用容器里的 locator.path 等普通字符串也不能被递归误判。"""

    if isinstance(value, str):
        return {value} if FORMAL_ID_PATTERN.fullmatch(value) else set()
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_collect_reference_field_ids(field, item))
        return result
    if not isinstance(value, Mapping):
        return set()
    result: set[str] = set()
    for key, item in value.items():
        if key in _EXPLICIT_REFERENCE_FIELDS:
            result.update(_collect_reference_field_ids(key, item))
        elif field == "source_refs" and key == "material_id":
            result.update(_collect_reference_field_ids(key, item))
        elif field == "technical_solution_refs" and key in {"id", "reference_key"}:
            result.update(_collect_reference_field_ids(key, item))
    return result


def _object_identifier(value: Mapping[str, object]) -> str:
    for field in ("id", "design_id", "artifact_id", "task_id"):
        candidate = value.get(field)
        if isinstance(candidate, str) and FORMAL_ID_PATTERN.fullmatch(candidate):
            return candidate
    return ""


def _validate_operation_targets(
    kind: str,
    operations: Sequence[Mapping[str, object]],
    base_objects: Sequence[Mapping[str, object]],
) -> None:
    known = {_object_identifier(item): item for item in base_objects}
    used: set[str] = set()
    for operation in operations:
        name = str(operation.get("operation") or "")
        if name == "add":
            continue
        targets = operation.get("target_ids") if name == "merge" else [operation.get("target_id")]
        targets = targets if isinstance(targets, list) else []
        if name == "merge" and targets != sorted(targets):
            raise SdlcError(f"{kind} merge.target_ids 必须按正式编号排序。")
        for target in targets:
            if not isinstance(target, str) or target not in known:
                raise SdlcError(f"{kind} 操作目标不存在：{target}。")
            if target in used:
                raise SdlcError(f"{kind} 同一正式目标不能出现在多个操作中：{target}。")
            used.add(target)
        if name in {"replace", "deprecate", "split", "relink"}:
            target = str(operation.get("target_id") or "")
            if operation.get("base_revision_sha256") != canonical_sha256(known[target]):
                raise SdlcError(f"{kind} {target} 的基础修订哈希不一致。")
        elif name == "merge":
            revisions = [
                {"target_id": target, "revision_sha256": canonical_sha256(known[str(target)])}
                for target in targets
            ]
            if operation.get("base_revision_sha256") != canonical_sha256(revisions):
                raise SdlcError(f"{kind} merge 基础修订哈希不一致。")


def _new_object(kind: str, value: object, mapping: Mapping[str, str], client_key: str) -> dict[str, object]:
    copied = _validate_next_value(kind, value)
    formal_id = mapping[client_key]
    if kind in {"GR", "FR", "AC"}:
        return {"id": formal_id, **copied}
    if kind == "DES":
        return {"design_id": formal_id, **copied}
    return {"artifact_id": formal_id, **copied}


def apply_object_operations(
    kind: str,
    base_objects: Sequence[Mapping[str, object]],
    operations: Sequence[Mapping[str, object]],
    *,
    mapping: Mapping[str, str],
    change_id: str,
) -> tuple[list[dict[str, object]], set[str]]:
    """按原位置、废止保留和正式编号追加规则计算一类完整对象。"""

    _validate_operation_targets(kind, operations, base_objects)
    result = [deepcopy(dict(item)) for item in base_objects]
    changed: set[str] = set()
    additions: list[dict[str, object]] = []
    by_target: dict[str, Mapping[str, object]] = {}
    merge_by_target: dict[str, Mapping[str, object]] = {}
    for operation in operations:
        if operation.get("operation") == "merge":
            for target in operation.get("target_ids", []):
                merge_by_target[str(target)] = operation
        elif operation.get("operation") != "add":
            by_target[str(operation.get("target_id") or "")] = operation
        else:
            for client_key, value in _operation_new_values(operation):
                additions.append(_new_object(kind, value, mapping, client_key))
                changed.add(mapping[client_key])

    emitted_merges: set[int] = set()
    output: list[dict[str, object]] = []
    for current in result:
        target_id = _object_identifier(current)
        operation = by_target.get(target_id)
        merge = merge_by_target.get(target_id)
        if merge is not None:
            marker = id(merge)
            lifecycle = {
                "status": "deprecated",
                "change_id": change_id,
                "reason": merge["reason"],
                "replacement_refs": [mapping[str(merge["output"]["client_key"])]],
            }
            deprecated = deepcopy(current)
            deprecated["lifecycle"] = lifecycle
            output.append(deprecated)
            changed.add(target_id)
            if marker not in emitted_merges:
                client_key, value = _operation_new_values(merge)[0]
                output.append(_new_object(kind, value, mapping, client_key))
                changed.add(mapping[client_key])
                emitted_merges.add(marker)
            continue
        if operation is None:
            output.append(current)
            continue
        name = str(operation["operation"])
        changed.add(target_id)
        if name == "replace":
            replacement = _validate_next_value(kind, operation["next_value"])
            if kind != "ART" and replacement.get("client_key") != current.get("client_key"):
                raise SdlcError(f"{kind} replace 不能改变 client_key。")
            id_field = "id" if kind in {"GR", "FR", "AC"} else "design_id" if kind == "DES" else "artifact_id"
            output.append({id_field: target_id, **replacement})
        elif name == "deprecate":
            deprecated = deepcopy(current)
            deprecated["lifecycle"] = {
                "status": "deprecated",
                "change_id": change_id,
                "reason": operation["reason"],
                "replacement_refs": deepcopy(operation["replacement_refs"]),
            }
            output.append(deprecated)
        elif name == "relink":
            expected = _RELINK_KEYS[kind]
            references = operation.get("references")
            if not isinstance(references, dict) or set(references) != expected:
                raise SdlcError(f"{kind} relink.references 字段必须完整且不能增加表外字段。")
            relinked = deepcopy(current)
            for key, value in references.items():
                relinked[key] = deepcopy(value)
            output.append(relinked)
        elif name == "split":
            deprecated = deepcopy(current)
            replacement_refs = [mapping[key] for key, _value in _operation_new_values(operation)]
            deprecated["lifecycle"] = {
                "status": "deprecated",
                "change_id": change_id,
                "reason": operation["reason"],
                "replacement_refs": replacement_refs,
            }
            output.append(deprecated)
            for client_key, value in _operation_new_values(operation):
                output.append(_new_object(kind, value, mapping, client_key))
                changed.add(mapping[client_key])
        else:
            raise SdlcError(f"不支持的 {kind} 操作：{name}。")
    output.extend(sorted(additions, key=lambda item: _object_identifier(item)))
    return output, changed


def _flatten_acceptance(requirement: Mapping[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    functional = requirement.get("functional_requirements")
    for item in functional if isinstance(functional, list) else []:
        if not isinstance(item, Mapping):
            raise SdlcError("基础需求 functional_requirements 必须是对象数组。")
        acceptance = item.get("acceptance_criteria")
        for criterion in acceptance if isinstance(acceptance, list) else []:
            if not isinstance(criterion, Mapping):
                raise SdlcError("基础验收标准必须是对象。")
            result.append(deepcopy(dict(criterion)))
    return result


def _next_version(value: object, expected_name: str) -> str:
    if not isinstance(value, str):
        raise SdlcError(f"基础 {expected_name} 版本缺失。")
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None or match.group("name") != expected_name:
        raise SdlcError(f"基础 {expected_name} 版本格式不正确。")
    return f"{expected_name}.v{int(match.group('number')) + 1}"


def _project_requirement(
    base: Mapping[str, object],
    package: Mapping[str, object],
    mapping: Mapping[str, str],
) -> tuple[dict[str, object], set[str]]:
    if base.get("schema_version") != "requirement-current.v1" or base.get("requirement_id") != package.get("requirement_id"):
        raise SdlcError("基础需求完整版本身份不正确。")
    global_rules = base.get("global_rules")
    functional = base.get("functional_requirements")
    if not isinstance(global_rules, list) or not isinstance(functional, list):
        raise SdlcError("基础需求缺少完整 GR 或 FR 数组。")
    rewritten_package = rewrite_temporary_references(package, mapping)
    kind_ops = dict(_kind_operations(rewritten_package))
    projected_gr, changed_gr = apply_object_operations(
        "GR", global_rules, kind_ops["GR"], mapping=mapping, change_id=str(package["change_id"])
    )
    base_acceptance = _flatten_acceptance(base)
    projected_ac, changed_ac = apply_object_operations(
        "AC", base_acceptance, kind_ops["AC"], mapping=mapping, change_id=str(package["change_id"])
    )
    projected_fr, changed_fr = apply_object_operations(
        "FR", functional, kind_ops["FR"], mapping=mapping, change_id=str(package["change_id"])
    )

    acceptance_by_owner: dict[str, list[dict[str, object]]] = {}
    for criterion in projected_ac:
        owner = criterion.get("owner_fr_ref")
        if not isinstance(owner, str):
            raise SdlcError("AC owner_fr_ref 必须指向唯一 FR。")
        acceptance_by_owner.setdefault(owner, []).append(criterion)
    known_fr = {_object_identifier(item) for item in projected_fr}
    if set(acceptance_by_owner) - known_fr:
        raise SdlcError("AC owner_fr_ref 指向不存在或跨包 FR。")

    for fr in projected_fr:
        fr_id = _object_identifier(fr)
        computed = acceptance_by_owner.get(fr_id, [])
        raw_nested = fr.get("acceptance_criteria")
        if not isinstance(raw_nested, list) or not raw_nested:
            raise SdlcError(f"{fr_id} 必须保留完整非空 acceptance_criteria。")
        if fr_id not in changed_fr:
            if not computed:
                raise SdlcError(f"{fr_id} 的 AC 操作不能移除全部验收标准。")
            fr["acceptance_criteria"] = computed
            continue
        computed_by_client = {
            str(item.get("client_key")): _object_identifier(item)
            for item in computed
            if isinstance(item.get("client_key"), str)
        }
        desired_ids: list[str] = []
        for item in raw_nested:
            if not isinstance(item, Mapping):
                raise SdlcError(f"{fr_id} 的 acceptance_criteria 必须是对象数组。")
            item_id = _object_identifier(item)
            if not item_id:
                client_key = item.get("client_key")
                if isinstance(client_key, str) and client_key in mapping:
                    item_id = mapping[client_key]
                elif isinstance(client_key, str):
                    item_id = computed_by_client.get(client_key, "")
            desired_ids.append(item_id)
        computed_by_id = {_object_identifier(item): item for item in computed}
        if set(desired_ids) != set(computed_by_id) or len(desired_ids) != len(computed_by_id):
            raise SdlcError(f"{fr_id} 嵌套 AC 与 acceptance_operations 不完全一致。")
        ordered = [computed_by_id[item_id] for item_id in desired_ids]
        for submitted, actual in zip(raw_nested, ordered):
            comparable = {key: value for key, value in actual.items() if key not in {"id", "lifecycle"}}
            submitted_comparable = {key: value for key, value in submitted.items() if key != "id"}
            if canonical_sha256(submitted_comparable) != canonical_sha256(comparable):
                raise SdlcError(f"{fr_id} 嵌套 AC 正文与 acceptance_operations 不一致。")
        fr["acceptance_criteria"] = ordered

    result = deepcopy(dict(base))
    result["version"] = _next_version(base.get("version"), "requirement")
    result["is_current"] = False
    result["global_rules"] = projected_gr
    result["functional_requirements"] = projected_fr
    result["open_questions"] = []
    return result, changed_gr | changed_fr | changed_ac


def _design_documents(base: Mapping[str, object]) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    artifacts = base.get("artifacts")
    if not isinstance(artifacts, list):
        raise SdlcError("基础设计缺少 artifacts 数组。")
    documents: list[dict[str, object]] = []
    wrappers: dict[str, dict[str, object]] = {}
    for wrapper in artifacts:
        if not isinstance(wrapper, Mapping) or not isinstance(wrapper.get("document"), Mapping):
            raise SdlcError("基础设计产物必须包含完整 document。")
        document = deepcopy(dict(wrapper["document"]))
        stable_id = _object_identifier(document) or str(wrapper.get("artifact_id") or "")
        if not FORMAL_ID_PATTERN.fullmatch(stable_id):
            raise SdlcError("基础设计产物缺少正式编号。")
        if not _object_identifier(document):
            field = "design_id" if stable_id.startswith("DES-") else "artifact_id"
            document[field] = stable_id
        documents.append(document)
        wrappers[stable_id] = deepcopy(dict(wrapper))
    return documents, wrappers


def _project_design(
    base: Mapping[str, object],
    package: Mapping[str, object],
    mapping: Mapping[str, str],
    *,
    package_relative_path: str,
    package_sha256: str,
) -> tuple[dict[str, object], set[str]]:
    if base.get("schema_version") != "design-current.v1":
        raise SdlcError("基础设计完整版本身份不正确。")
    documents, wrappers = _design_documents(base)
    rewritten_package = rewrite_temporary_references(package, mapping)
    operations = dict(_kind_operations(rewritten_package))
    base_des = [item for item in documents if _object_identifier(item).startswith("DES-")]
    base_art = [item for item in documents if not _object_identifier(item).startswith("DES-")]
    projected_des, changed_des = apply_object_operations(
        "DES", base_des, operations["DES"], mapping=mapping, change_id=str(package["change_id"])
    )
    projected_art, changed_art = apply_object_operations(
        "ART", base_art, operations["ART"], mapping=mapping, change_id=str(package["change_id"])
    )
    projected_by_id = {_object_identifier(item): item for item in projected_des + projected_art}
    ordered_ids = [
        stable_id
        for stable_id in wrappers
        if stable_id in projected_by_id
    ] + sorted(set(projected_by_id) - set(wrappers))
    artifacts: list[dict[str, object]] = []
    for stable_id in ordered_ids:
        document = projected_by_id[stable_id]
        old = wrappers.get(stable_id)
        if old is None:
            artifact_type = "design_reference" if stable_id.startswith("DES-") else "design_artifact"
            wrapper = {
                "artifact_id": stable_id,
                "artifact_type": artifact_type,
                "archive_path": package_relative_path,
                "sha256": package_sha256,
                "document": document,
            }
        else:
            wrapper = deepcopy(old)
            wrapper["document"] = document
            if stable_id in changed_des | changed_art:
                wrapper["archive_path"] = package_relative_path
                wrapper["sha256"] = package_sha256
        artifacts.append(wrapper)
    result = deepcopy(dict(base))
    result["version"] = _next_version(base.get("version"), "design")
    result["is_current"] = False
    result["artifacts"] = artifacts
    return result, changed_des | changed_art


def _project_test_matrix(base: Mapping[str, object], requirement: Mapping[str, object]) -> dict[str, object]:
    if base.get("schema_version") != "test-matrix-current.v1":
        raise SdlcError("基础测试矩阵完整版本身份不正确。")
    acceptance: list[dict[str, object]] = []
    for fr in requirement.get("functional_requirements", []):
        if not isinstance(fr, Mapping) or fr.get("lifecycle", {}).get("status") == "deprecated":
            continue
        fr_id = _object_identifier(fr)
        for item in fr.get("acceptance_criteria", []):
            if not isinstance(item, Mapping) or item.get("lifecycle", {}).get("status") == "deprecated":
                continue
            acceptance.append(
                {
                    "id": item.get("id"),
                    "requirement_id": fr_id,
                    **{key: deepcopy(value) for key, value in item.items() if key != "id"},
                }
            )
    result = deepcopy(dict(base))
    result["version"] = _next_version(base.get("version"), "test-matrix")
    result["is_current"] = False
    result["acceptance_criteria"] = acceptance
    return result


def _material_reference(
    operation: Mapping[str, object],
    material: Mapping[str, object],
    *,
    workspace_path: str,
    manifest_path: str,
    manifest_sha256: str,
    material_index: int,
) -> dict[str, object]:
    source_kind = material.get("source_kind")
    if source_kind == "file":
        if material.get("status") != "active":
            raise SdlcError(f"{material.get('material_id')} 普通文件资料不是 active 状态。")
        expected = {
            "workspace_path": material.get("stored_path"),
            "sha256": material.get("sha256"),
            "version_evidence": {"kind": "local_snapshot", "sha256": material.get("sha256")},
        }
        reference = {
            "schema_version": "reference-locator.v1",
            "path": f"{workspace_path}/{material['stored_path']}",
            "sha256": material["sha256"],
            "locator": {"kind": "whole_file"},
        }
    elif source_kind == "external-reference":
        if material.get("status") != "confirmed":
            raise SdlcError(f"{material.get('material_id')} 外部资料没有确认稳定版本。")
        expected = {
            "workspace_path": "change-material-manifest.v1.json",
            "sha256": material.get("version_evidence_sha256"),
            "version_evidence": material.get("version_evidence"),
        }
        reference = {
            "schema_version": "reference-locator.v1",
            "path": manifest_path,
            "sha256": manifest_sha256,
            "locator": {"kind": "json_pointer", "value": f"/materials/{material_index}"},
        }
    elif source_kind == "secret-reference":
        if material.get("status") != "active":
            raise SdlcError(f"{material.get('material_id')} 秘密引用资料不是 active 状态。")
        expected = {
            "workspace_path": "change-material-manifest.v1.json",
            "sha256": material.get("secret_reference_sha256"),
            "version_evidence": None,
        }
        reference = {
            "schema_version": "reference-locator.v1",
            "path": manifest_path,
            "sha256": manifest_sha256,
            "locator": {"kind": "json_pointer", "value": f"/materials/{material_index}"},
        }
    else:
        raise SdlcError("资料清单包含不支持的 source_kind。")
    for key, value in expected.items():
        if operation.get(key) != value:
            raise SdlcError(f"资料操作 {key} 与当前 CMAT 清单不一致。")
    return reference


def _project_reference_index(
    base: Mapping[str, object],
    package: Mapping[str, object],
    mapping: Mapping[str, str],
    *,
    package_relative_path: str,
    package_sha256: str,
    manifest: Mapping[str, object],
    manifest_relative_path: str,
    manifest_sha256: str | None,
    changed_ids: set[str],
) -> dict[str, object]:
    if base.get("schema_version") != "reference-index.v1":
        raise SdlcError("基础正式引用索引版本不正确。")
    entries = base.get("entries")
    if not isinstance(entries, Mapping):
        raise SdlcError("基础正式引用索引缺少 entries。")
    result = deepcopy(dict(base))
    projected_entries = deepcopy(dict(entries))
    package_reference = {
        "schema_version": "reference-locator.v1",
        "path": package_relative_path,
        "sha256": package_sha256,
        "locator": {"kind": "whole_file"},
    }
    for stable_id in changed_ids:
        if not stable_id.startswith("MAT-"):
            projected_entries[stable_id] = deepcopy(package_reference)

    material_by_id = {
        str(item.get("material_id")): (index, item)
        for index, item in enumerate(manifest.get("materials", []))
        if isinstance(item, Mapping)
    }
    consumed: set[str] = set()
    for operation in package.get("material_operations", []):
        if not isinstance(operation, Mapping):
            raise SdlcError("material_operations 每项必须是对象。")
        name = operation.get("operation")
        if name in {"add", "replace"}:
            source_id = str(operation.get("source_material_id") or "")
            if source_id in consumed or source_id not in material_by_id:
                raise SdlcError("资料操作使用了重复、不存在或跨 CHG 的 CMAT。")
            consumed.add(source_id)
            index, material = material_by_id[source_id]
            target = mapping[str(operation["client_key"])] if name == "add" else str(operation["target_id"])
            if name == "replace":
                current = projected_entries.get(target)
                if not isinstance(current, Mapping) or operation.get("base_revision_sha256") != canonical_sha256(current):
                    raise SdlcError(f"{target} 基础资料引用修订哈希不一致。")
            if manifest_sha256 is None:
                raise SdlcError("资料操作存在时必须有可核对的资料清单。")
            projected_entries[target] = _material_reference(
                operation,
                material,
                workspace_path=str(manifest["workspace_path"]),
                manifest_path=manifest_relative_path,
                manifest_sha256=manifest_sha256,
                material_index=index,
            )
        elif name in {"deprecate", "relink"}:
            target = str(operation.get("target_id") or "")
            current = projected_entries.get(target)
            if not isinstance(current, Mapping) or operation.get("base_revision_sha256") != canonical_sha256(current):
                raise SdlcError(f"{target} 基础资料引用修订哈希不一致。")
            if name == "deprecate":
                deprecated = deepcopy(dict(current))
                deprecated["lifecycle"] = {
                    "status": "deprecated",
                    "change_id": str(package["change_id"]),
                    "reason": str(operation["reason"]),
                    "replacement_refs": deepcopy(operation["replacement_refs"]),
                }
                projected_entries[target] = deprecated
            else:
                references = operation.get("references")
                if not isinstance(references, dict) or set(references) != {"applies_to", "supersedes"}:
                    raise SdlcError("MAT relink.references 字段必须完整且不能增加表外字段。")
        else:
            raise SdlcError(f"资料操作不支持：{name}。")
    expected_consumed = set(material_by_id)
    if consumed != expected_consumed:
        omitted = sorted(expected_consumed - consumed)
        raise SdlcError(f"变更资料必须各自由一个 add 或 replace 操作消费：{', '.join(omitted)}。")
    result["entries"] = {key: projected_entries[key] for key in sorted(projected_entries)}
    if isinstance(result.get("display"), Mapping):
        result["display"] = {key: result["display"][key] for key in sorted(result["display"])}
    validate_schema_document(result, schema_name="reference-index.v1")
    return result


def _project_task_plan(
    base: Mapping[str, object],
    package: Mapping[str, object],
    mapping: Mapping[str, str],
) -> dict[str, object]:
    validate_schema_document(base, schema_name="task-plan.v2")
    result = deepcopy(dict(base))
    impacts = package.get("task_impacts")
    if not isinstance(impacts, Mapping):
        raise SdlcError("task_impacts 必须是对象。")
    base_tasks = result.get("tasks")
    base_dependencies = result.get("dependencies")
    base_mapping = result.get("mapping")
    if not isinstance(base_tasks, list) or not isinstance(base_dependencies, list) or not isinstance(base_mapping, dict):
        raise SdlcError("基础 task-plan.v2 不是完整记录。")
    classified: list[str] = []
    for field in ("restore", "close", "unaffected"):
        for item in impacts.get(field, []):
            if not isinstance(item, Mapping) or item.get("task_id") not in base_tasks:
                raise SdlcError(f"task_impacts.{field} 引用了不存在的任务。")
            classified.append(str(item["task_id"]))
    if len(classified) != len(set(classified)):
        raise SdlcError("同一现有任务不能出现在多个 task_impacts 数组。")
    new_tasks: list[str] = []
    dependencies = deepcopy(base_dependencies)
    mapping_result = deepcopy(base_mapping)
    for item in impacts.get("add", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("next_value"), Mapping):
            raise SdlcError("task_impacts.add 必须包含完整 next_value。")
        client_key = str(item["client_key"])
        task_id = mapping[client_key]
        next_value = rewrite_temporary_references(item["next_value"], mapping)
        if not isinstance(next_value, Mapping):
            raise SdlcError("新增任务重写结果必须是对象。")
        dependencies.extend(
            {"from": dependency, "to": task_id}
            for dependency in next_value.get("depends_on", [])
        )
        new_tasks.append(task_id)
        if client_key in mapping_result:
            raise SdlcError(f"新增任务 client_key 已经存在：{client_key}。")
        mapping_result[client_key] = task_id
    result["producer_run_id"] = package["producer_run_id"]
    input_hashes = deepcopy(result.get("input_hashes", {}))
    input_hashes["change_package"] = canonical_sha256(package)
    for name, base_item in package["base_versions"].items():
        input_hashes[f"base_{name}"] = base_item["sha256"]
    result["input_hashes"] = input_hashes
    result["tasks"] = base_tasks + sorted(new_tasks)
    unique_dependencies = {
        (str(item["from"]), str(item["to"])): {"from": item["from"], "to": item["to"]}
        for item in dependencies
        if isinstance(item, Mapping)
    }
    result["dependencies"] = [unique_dependencies[key] for key in sorted(unique_dependencies)]
    result["mapping"] = {key: mapping_result[key] for key in sorted(mapping_result)}
    validate_schema_document(result, schema_name="task-plan.v2")
    return result


def _load_bases(paths: ProjectPaths, status: Mapping[str, object]) -> dict[str, dict[str, object]]:
    raw_bases = status.get("base_versions")
    if not isinstance(raw_bases, Mapping) or set(raw_bases) != set(BASE_VERSION_PATHS):
        raise SdlcError("status.json 缺少五份完整基础版本。")
    result: dict[str, dict[str, object]] = {}
    for name in BASE_VERSION_PATHS:
        item = raw_bases.get(name)
        if not isinstance(item, Mapping):
            raise SdlcError(f"基础版本 {name} 记录不完整。")
        path = resolve_project_path(paths.root, str(item.get("path") or ""))
        try:
            path.resolve(strict=True).relative_to(paths.root.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise SdlcError(f"基础版本 {name} 路径不安全。") from exc
        if path.is_symlink() or not path.is_file() or sha256_file(path) != item.get("sha256"):
            raise SdlcError(f"基础版本 {name} 已经漂移。")
        result[name] = _decode_json(path.read_bytes(), label=f"基础版本 {name}")
    return result


def _validate_projected_source(
    name: str,
    document: Mapping[str, object],
    *,
    package: Mapping[str, object],
    status: Mapping[str, object],
) -> None:
    schema_name, base_name = PROJECTED_FILES[name]
    validate_schema_document(document, schema_name=schema_name)
    expected = {
        "requirement_id": package["requirement_id"],
        "change_id": package["change_id"],
        "base": status["base_versions"][base_name],
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise SdlcError(f"{name} 的 REQ、CHG 或基础版本不一致。")
    if document.get("content_sha256") != canonical_sha256(document.get("content")):
        raise SdlcError(f"{name} 的 content_sha256 不正确。")


def _validate_task_impact_states(
    package: Mapping[str, object],
    *,
    task_states: Mapping[str, str],
    task_run_states: Mapping[str, str | None],
) -> None:
    """任务影响必须以同一把项目锁内读到的任务和运行状态为准。"""

    impacts = package.get("task_impacts")
    if not isinstance(impacts, Mapping):
        return
    for field in ("restore", "close"):
        entries = impacts.get(field)
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, Mapping) or not isinstance(item.get("task_id"), str):
                continue
            task_id = str(item["task_id"])
            task_state = task_states.get(task_id)
            run_state = task_run_states.get(task_id)
            if field == "restore":
                if task_state != "done" or run_state != "closed":
                    raise SdlcError(
                        f"restore 只允许恢复已有 closed 运行记录的已完成任务：{task_id}。"
                    )
            elif task_state != "todo" or run_state is not None:
                raise SdlcError(f"close 只允许关闭尚未开始且没有任务运行记录的任务：{task_id}。")


def prepare_change_package(
    paths: ProjectPaths,
    *,
    status: Mapping[str, object],
    manifest: Mapping[str, object],
    source_documents: Mapping[str, Mapping[str, object]],
    existing_ids: Iterable[str],
    task_states: Mapping[str, str] | None = None,
    task_run_states: Mapping[str, str | None] | None = None,
) -> PreparedChangePackage:
    """严格校验六份来源、独立计算五份结果并返回固定提交字节。"""

    if set(source_documents) != set(COMMITTED_FILE_NAMES):
        raise SdlcError("变更包必须一次提交六份固定 JSON 文件。")
    package = deepcopy(dict(source_documents[PACKAGE_FILE]))
    validate_schema_document(package, schema_name=PACKAGE_SCHEMA)
    expected_owner = {
        "requirement_id": status.get("requirement_id"),
        "change_id": status.get("change_id"),
        "base_versions": status.get("base_versions"),
    }
    if any(package.get(key) != value for key, value in expected_owner.items()):
        raise SdlcError("change-package.v1 与当前 CHG 所有权或基础版本不一致。")
    if package.get("open_questions"):
        raise SdlcError("change-package.v1 仍有 open_questions，不能提交。")
    if package.get("requirement_id") != manifest.get("requirement_id") or package.get("change_id") != manifest.get("change_id"):
        raise SdlcError("变更资料清单与当前变更包所有权不一致。")
    for name in PROJECTED_FILES:
        _validate_projected_source(name, source_documents[name], package=package, status=status)

    # 状态不合法时必须在正式编号分配和任何事务文件写入之前结束。
    _validate_task_impact_states(
        package,
        task_states=task_states or {},
        task_run_states=task_run_states or {},
    )

    existing_id_set = set(existing_ids)
    allocation = _allocation_objects(package)
    mapping = allocate_stable_ids(allocation, existing_ids=existing_id_set) if allocation else {}
    rewritten_package = rewrite_temporary_references(package, mapping)
    if not isinstance(rewritten_package, dict):
        raise SdlcError("变更包临时引用重写结果无效。")
    valid_refs = existing_id_set | set(mapping.values()) | {
        str(package["requirement_id"]),
        str(package["change_id"]),
    }
    valid_refs.update(
        str(item.get("material_id"))
        for item in manifest.get("materials", [])
        if isinstance(item, Mapping)
    )
    business_prefixes = {
        "REQ", "CHG", "CMAT", "FR", "GR", "AC", "DES", "MAT",
        "DATA", "API", "PAGE", "COMP", "SAFE", "DEPLOY", "FIELD", "SPEC", "T",
    }
    unknown_refs = sorted(
        reference
        for reference in _collect_declared_formal_ids(rewritten_package)
        if reference.split("-", 1)[0] in business_prefixes and reference not in valid_refs
    )
    if unknown_refs:
        raise SdlcError(f"变更包包含不存在或跨包的正式引用：{', '.join(unknown_refs)}。")
    package_content = canonical_json_text(rewritten_package).encode("utf-8")
    package_sha256 = sha256_bytes(package_content)
    workspace_path = str(status["workspace_path"])
    package_relative_path = f"{workspace_path}/{PACKAGE_FILE}"
    manifest_path = f"{workspace_path}/change-material-manifest.v1.json"
    manifest_file = paths.root / manifest_path
    manifest_sha256 = sha256_file(manifest_file) if manifest_file.is_file() and not manifest_file.is_symlink() else None

    bases = _load_bases(paths, status)
    requirement, changed_requirement = _project_requirement(bases["requirement"], package, mapping)
    design, changed_design = _project_design(
        bases["design"],
        package,
        mapping,
        package_relative_path=package_relative_path,
        package_sha256=package_sha256,
    )
    test_matrix = _project_test_matrix(bases["test_matrix"], requirement)
    reference_index = _project_reference_index(
        bases["reference_index"],
        rewritten_package,
        mapping,
        package_relative_path=package_relative_path,
        package_sha256=package_sha256,
        manifest=manifest,
        manifest_relative_path=manifest_path,
        manifest_sha256=manifest_sha256,
        changed_ids=changed_requirement | changed_design,
    )
    task_plan = _project_task_plan(bases["task_plan"], rewritten_package, mapping)
    expected_contents = {
        "projected-requirement.v2.json": requirement,
        "projected-design.v2.json": design,
        "projected-test-matrix.v2.json": test_matrix,
        "projected-reference-index.v2.json": reference_index,
        "projected-task-plan.v2.json": task_plan,
    }

    committed_documents: dict[str, dict[str, object]] = {PACKAGE_FILE: rewritten_package}
    for name, expected_content in expected_contents.items():
        rewritten = rewrite_temporary_references(source_documents[name], mapping)
        if not isinstance(rewritten, dict):
            raise SdlcError(f"{name} 临时引用重写结果无效。")
        rewritten["content_sha256"] = canonical_sha256(rewritten["content"])
        if canonical_sha256(rewritten["content"]) != canonical_sha256(expected_content):
            raise SdlcError(f"{name} 与 CLI 独立计算的完整预计结果不等值。")
        rewritten["content"] = expected_content
        rewritten["content_sha256"] = canonical_sha256(expected_content)
        validate_schema_document(rewritten, schema_name=PROJECTED_FILES[name][0])
        committed_documents[name] = rewritten

    source_hashes = {
        name: canonical_sha256(source_documents[name])
        for name in COMMITTED_FILE_NAMES
    }
    status_file = paths.root / str(status["workspace_path"]) / "status.json"
    status_digest = sha256_file(status_file)
    identity = canonical_sha256(
        {
            "schema_version": "change-package-input.v1",
            "requirement_id": package["requirement_id"],
            "change_id": package["change_id"],
            "status_sha256": status_digest,
            "material_manifest_sha256": manifest_sha256,
            "source_files_sha256": {key: source_hashes[key] for key in sorted(source_hashes)},
        }
    )
    committed_bytes = {
        name: canonical_json_text(committed_documents[name]).encode("utf-8")
        for name in COMMITTED_FILE_NAMES
    }
    committed_hashes = {name: sha256_bytes(content) for name, content in committed_bytes.items()}
    return PreparedChangePackage(
        package_identity_sha256=identity,
        source_files_sha256=source_hashes,
        committed_files_sha256=committed_hashes,
        committed_file_bytes=committed_bytes,
        id_mapping={key: mapping[key] for key in sorted(mapping)},
        status_sha256=status_digest,
        material_manifest_sha256=manifest_sha256,
    )


__all__ = [
    "COMMITTED_FILE_NAMES",
    "PACKAGE_FILE",
    "PROJECTED_FILES",
    "PreparedChangePackage",
    "collect_existing_ids",
    "load_source_documents",
    "prepare_change_package",
]
