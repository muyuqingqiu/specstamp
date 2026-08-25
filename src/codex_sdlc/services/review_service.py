from __future__ import annotations

from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from codex_sdlc.core import dependency_graph, fact_review_trust, review_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, project_lock
from codex_sdlc.core.requirement_contract import (
    REQUIREMENT_COVERAGE_SCHEMA,
    REQUIREMENT_SPLIT_SCHEMA,
    ensure_requirement_review_ready,
    validate_requirement_contract,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_sha256,
    contract_sha256,
)


_TRUST_TEMP_PATTERN = re.compile(r"^\.registry\.json\.[a-z0-9_]{8}\.tmp$")
_REVIEW_ID_PATTERN = re.compile(r"^REV-([0-9]{3,})$")
_REQUIREMENT_INPUT_PATTERN = re.compile(
    r"^\.codex-sdlc/drafts/(?P<draft_id>DRAFT-[0-9]{3,})/质检/"
    r"需求审核输入-(?P<digest>[0-9a-f]{64})\.json$"
)
_REQUIREMENT_INPUT_TEMP_PATTERN = re.compile(
    r"^\.需求审核输入-[0-9a-f]{64}\.[a-z0-9_]{8}\.tmp$"
)
_REQUIREMENT_REVIEW_INPUT_SCHEMA = "sdlc.requirement-review-input.v1"
_INTEGRATED_DESIGN_INPUT_PATTERN = re.compile(
    r"^\.codex-sdlc/drafts/(?P<draft_id>DRAFT-[0-9]{3,})/质检/"
    r"整体设计审核输入-(?P<digest>[0-9a-f]{64})\.json$"
)
_INTEGRATED_DESIGN_INPUT_TEMP_PATTERN = re.compile(
    r"^\.整体设计审核输入-[0-9a-f]{64}\.[a-z0-9_]{8}\.tmp$"
)
_INTEGRATED_DESIGN_REVIEW_INPUT_SCHEMA = (
    "sdlc.integrated-design-review-input.v1"
)
_TASK_PLAN_INPUT_PATTERN = re.compile(
    r"^\.codex-sdlc/requirements/(?P<folder>[^/]+)/质检/"
    r"任务审核输入-(?P<digest>[0-9a-f]{64})\.json$"
)
_TASK_PLAN_INPUT_TEMP_PATTERN = re.compile(
    r"^\.任务审核输入-[0-9a-f]{64}\.[a-z0-9_]{8}\.tmp$"
)
_TASK_PLAN_REVIEW_INPUT_SCHEMA = "sdlc.task-plan-review-input.v1"

# 需求审核入口固定使用总设计中的十项检查。调用方不能删减、增补或重新排序，
# 这样同一完整输入只会得到同一个审核指纹。
REQUIREMENT_REVIEW_CHECKS = tuple(
    sorted(
        (
            "原始需求是否遗漏",
            "FR 是否拆得过粗或过碎",
            "局部事实、规则和约束是否仍属于正确 FR",
            "GR 是否确实影响多个 FR",
            "数字、时间、状态、权限、字段和异常是否被改变",
            "是否增加了没有来源的业务要求",
            "待确认内容是否被误写为已确认",
            "每条 FR 是否具备可执行验收标准",
            "覆盖矩阵是否真实完整",
            "原文和拆分结果是否能够双向追溯",
        )
    )
)

# 整体设计只有这一个固定审核点。检查项来自总设计的明确清单，固定排序可以让
# 同一组真实输入稳定复用同一个审核指纹。
INTEGRATED_DESIGN_REVIEW_CHECKS = tuple(
    sorted(
        (
            "每条 FR 是否有对应设计支持",
            "数据字段和接口字段是否一致",
            "接口是否能够支持页面和业务流程",
            "页面是否覆盖正常、空、加载、错误、权限和异常状态",
            "状态、错误码和公共类型是否统一",
            "公共组件和模块能力是否重复设计",
            "provided 模块的显式资料和代码证据是否真实充分",
            "not_applicable 模块是否确实不需要",
            "是否仍有必须在开发前决定的问题",
            "全部设计是否已经具备正式建档条件",
        )
    )
)

# 整套任务只在计划完成后集中审核一次。这里固定的是审核者必须逐项阅读和判断的
# 清单，CLI 只验证结构化事实，不会根据任务标题或数量替审核者判断业务质量。
TASK_PLAN_REVIEW_CHECKS = tuple(
    sorted(
        (
            "每条 FR 是否由任务承接或有正式无需开发依据",
            "每份必要设计产物是否由任务使用",
            "数据库、接口、页面、组件、安全、部署、测试和交付工作是否遗漏",
            "每个任务的边界是否清楚",
            "任务是否重复或互相冲突",
            "任务依赖是否正确",
            "全局设计是否已经在开发前决定",
            "每个任务是否能够直接执行",
            "自动测试和人工验收是否足以证明需求完成",
            "所有需求、设计、资料、任务和测试引用是否真实有效",
        )
    )
)


def _draft_relative_path(draft_id: str, *parts: str) -> str:
    return Path(".codex-sdlc", "drafts", draft_id, *parts).as_posix()


def _is_managed_draft_owner(
    paths: ProjectPaths,
    owner_id: object,
) -> bool:
    """只把真实 DRAFT 工作区视为业务审核对象，保留通用合同的抽象夹具能力。"""

    draft_id = str(owner_id or "").strip().upper()
    if not re.fullmatch(r"DRAFT-[0-9]{3,}", draft_id):
        return False
    directory = paths.drafts_dir / draft_id
    return directory.exists() and not directory.is_symlink() and directory.is_dir()


def _is_managed_task_plan_owner(
    paths: ProjectPaths,
    owner_id: object,
) -> bool:
    """只让已经落盘的正式 task-plan.v2 进入整套任务专用构建器。"""

    requirement_id = str(owner_id or "").strip().upper()
    if not re.fullmatch(r"REQ-[0-9]{3,}", requirement_id):
        return False
    if not paths.requirements_dir.is_dir() or paths.requirements_dir.is_symlink():
        return False
    matches = [
        directory
        for directory in paths.requirements_dir.iterdir()
        if directory.is_dir()
        and not directory.is_symlink()
        and (
            directory.name == requirement_id
            or directory.name.startswith(f"{requirement_id}-")
        )
        and (directory / "tasks/task-plan.v2.json").is_file()
    ]
    return len(matches) == 1


def _read_controlled_json(
    paths: ProjectPaths,
    relative_path: str,
    *,
    label: str,
) -> dict[str, Any]:
    """先后两次安全取哈希，并确认实际解析的完整字节正是受控文件。"""

    normalized = review_contract.normalize_input_path(relative_path)
    before = review_contract.controlled_input_hashes(paths, [normalized])[normalized]
    target = paths.root / normalized
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise SdlcError(f"{label}无法读取：{normalized}。", exit_code=1) from exc
    if hashlib.sha256(raw).hexdigest() != before:
        raise SdlcError(f"{label}在读取期间发生变化：{normalized}。", exit_code=1)
    after = review_contract.controlled_input_hashes(paths, [normalized])[normalized]
    if after != before:
        raise SdlcError(f"{label}在读取期间发生变化：{normalized}。", exit_code=1)
    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效 JSON：{normalized}。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是对象：{normalized}。", exit_code=1)
    return document


def _visible_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): deepcopy(value)
        for key, value in draft.items()
        if not str(key).startswith("_")
    }


def _load_draft_status(
    paths: ProjectPaths,
    draft_id: str,
    *,
    expected_draft: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    relative = _draft_relative_path(draft_id, "status.json")
    document = _read_controlled_json(paths, relative, label="DRAFT 状态文件")
    if document.get("draft_id") != draft_id:
        raise SdlcError(f"DRAFT 状态文件不属于 {draft_id}。", exit_code=1)
    if expected_draft is not None and document != _visible_draft(expected_draft):
        raise SdlcError(f"{draft_id} 的 status.json 与当前事件状态不一致，请先刷新。", exit_code=1)
    return document


def _material_is_applicable(
    material: Mapping[str, Any],
    *,
    draft_id: str,
    formal_ids: set[str],
) -> bool:
    roles = material.get("roles")
    role_names = {str(item) for item in roles} if isinstance(roles, list) else set()
    scopes = material.get("applies_to")
    scope_names = {str(item) for item in scopes} if isinstance(scopes, list) else set()
    return bool(
        material.get("type") == "requirement"
        or "requirement" in role_names
        or not scope_names
        or draft_id in scope_names
        or "requirement_split" in scope_names
        or scope_names.intersection(formal_ids)
    )


def _material_is_integrated_design_applicable(
    material: Mapping[str, Any],
    *,
    draft_id: str,
    formal_ids: set[str],
    explicit_material_ids: set[str],
) -> bool:
    """适用资料只认类型、显式范围和计划引用，不扫描资料标题或正文猜用途。"""

    material_id = str(material.get("material_id") or "")
    roles = material.get("roles")
    role_names = {str(item) for item in roles} if isinstance(roles, list) else set()
    scopes = material.get("applies_to")
    scope_names = {str(item) for item in scopes} if isinstance(scopes, list) else set()
    return bool(
        material_id in explicit_material_ids
        or material.get("type") in {"requirement", "technical-solution"}
        or role_names.intersection({"requirement", "technical-solution"})
        or not scope_names
        or draft_id in scope_names
        or "integrated_design" in scope_names
        or scope_names.intersection(formal_ids)
    )


def _material_file_path(draft_id: str, material: Mapping[str, Any]) -> str:
    stored_path = review_contract.normalize_input_path(str(material.get("stored_path") or ""))
    expected_prefix = f"原始资料/{material.get('material_id')}_"
    if not stored_path.startswith(expected_prefix):
        raise SdlcError(f"资料 {material.get('material_id')} 的归档路径无效。", exit_code=1)
    return _draft_relative_path(draft_id, stored_path)


def _requirement_package_paths(
    draft_id: str,
    receipt: Mapping[str, Any],
) -> tuple[str, str, str, str, str]:
    destination = review_contract.normalize_input_path(str(receipt.get("destination") or ""))
    expected_prefix = f".codex-sdlc/drafts/{draft_id}/需求/requirements-"
    if not destination.startswith(expected_prefix):
        raise SdlcError("需求导入回执的不可变目录不属于当前 DRAFT。", exit_code=1)
    source_split = f"{destination}/requirement-split.v1.json"
    source_coverage = f"{destination}/requirement-coverage.v1.json"
    declared_files = receipt.get("files")
    if not isinstance(declared_files, list) or declared_files != sorted(
        [source_coverage, source_split]
    ):
        raise SdlcError("需求导入回执的文件清单不完整或没有按路径排序。", exit_code=1)
    return (
        source_split,
        source_coverage,
        _draft_relative_path(draft_id, "需求", "requirement-split.v1.json"),
        _draft_relative_path(draft_id, "需求", "requirement-coverage.v1.json"),
        _draft_relative_path(draft_id, "需求", "需求导入回执.json"),
    )


def _requirement_known_ids(draft: Mapping[str, Any]) -> set[str]:
    receipt = draft.get("requirement_import")
    mapping = receipt.get("mapping") if isinstance(receipt, dict) else None
    result = {
        str(value)
        for value in (mapping.values() if isinstance(mapping, dict) else [])
        if isinstance(value, str)
    }
    for decision in draft.get("decision_records", []):
        if isinstance(decision, dict) and isinstance(decision.get("decision_id"), str):
            result.add(str(decision["decision_id"]))
    return result


def _current_contract_material_hashes(
    draft: Mapping[str, Any],
    declared: object,
) -> dict[str, str]:
    if not isinstance(declared, dict):
        raise SdlcError("需求拆分缺少 input_material_hashes。", exit_code=1)
    active = {
        str(item.get("material_id") or ""): item
        for item in draft.get("materials", [])
        if isinstance(item, dict)
        and item.get("status") != "archived"
        and item.get("source_kind") == "file"
        and str(item.get("material_id") or "")
    }
    result: dict[str, str] = {}
    for material_id in sorted(declared):
        material = active.get(str(material_id))
        if material is None:
            raise SdlcError(f"需求拆分引用的活动资料不存在：{material_id}。", exit_code=1)
        digest = str(material.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SdlcError(f"资料 {material_id} 缺少完整 SHA-256。", exit_code=1)
        result[str(material_id)] = digest
    return result


def _temporary_requirement_documents(
    split: Mapping[str, Any],
    coverage: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """只还原固定引用字段，正文即使恰好等于 FR-xxx 也保持原样。"""

    inverse = {
        str(formal_id): f"@client:{client_key}"
        for client_key, formal_id in mapping.items()
        if isinstance(client_key, str) and isinstance(formal_id, str)
    }
    temporary_split = deepcopy(dict(split))
    temporary_coverage = deepcopy(dict(coverage))

    def replace_list(record: dict[str, Any], field: str) -> None:
        values = record.get(field)
        if isinstance(values, list):
            record[field] = [inverse.get(str(item), item) for item in values]

    def replace_relations(record: dict[str, Any]) -> None:
        relations = record.get("relations")
        if not isinstance(relations, list):
            return
        for relation in relations:
            if isinstance(relation, dict) and isinstance(relation.get("target_ref"), str):
                relation["target_ref"] = inverse.get(
                    str(relation["target_ref"]), relation["target_ref"]
                )

    for rule in temporary_split.get("global_rules", []):
        if isinstance(rule, dict):
            replace_list(rule, "applies_to")
            replace_relations(rule)
    for requirement in temporary_split.get("functional_requirements", []):
        if not isinstance(requirement, dict):
            continue
        replace_list(requirement, "global_rule_refs")
        replace_list(requirement, "depends_on")
        replace_relations(requirement)
        for criterion in requirement.get("acceptance_criteria", []):
            if not isinstance(criterion, dict):
                continue
            if isinstance(criterion.get("owner_fr_ref"), str):
                criterion["owner_fr_ref"] = inverse.get(
                    str(criterion["owner_fr_ref"]), criterion["owner_fr_ref"]
                )
            replace_relations(criterion)
    for unit in temporary_coverage.get("units", []):
        if isinstance(unit, dict):
            replace_list(unit, "covered_by")
            replace_relations(unit)
    temporary_coverage["requirement_split_sha256"] = contract_sha256(
        temporary_split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    return temporary_split, temporary_coverage


def _validated_material_inputs(
    paths: ProjectPaths,
    draft: Mapping[str, Any],
    *,
    formal_ids: set[str],
    stage: str = "requirement_split",
    explicit_material_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    draft_id = str(draft.get("draft_id") or "")
    materials = [
        deepcopy(item)
        for item in draft.get("materials", [])
        if isinstance(item, dict)
    ]
    by_id = {str(item.get("material_id") or ""): item for item in materials}
    explicit_ids = set(explicit_material_ids or set())
    applicable = [
        item
        for item in materials
        if item.get("status") != "archived"
        and (
            _material_is_applicable(
                item,
                draft_id=draft_id,
                formal_ids=formal_ids,
            )
            if stage == "requirement_split"
            else _material_is_integrated_design_applicable(
                item,
                draft_id=draft_id,
                formal_ids=formal_ids,
                explicit_material_ids=explicit_ids,
            )
        )
    ]
    if not applicable:
        label = "当前需求" if stage == "requirement_split" else "当前整体设计"
        raise SdlcError(f"{label}没有可审核的适用资料。", exit_code=1)

    version_materials: dict[str, dict[str, Any]] = {}
    file_paths: set[str] = set()
    for material in applicable:
        material_id = str(material.get("material_id") or "MAT")
        status = str(material.get("status") or "")
        if status in {"unversioned", "blocked", "drifted"}:
            review_name = "需求审核" if stage == "requirement_split" else "整体设计审核"
            raise SdlcError(
                f"资料 {material_id} 尚未形成稳定版本，不能创建{review_name}。",
                exit_code=1,
            )
        source_kind = material.get("source_kind")
        if source_kind == "file":
            path = _material_file_path(draft_id, material)
            actual = review_contract.controlled_input_hashes(paths, [path])[path]
            if actual != material.get("sha256"):
                raise SdlcError(f"资料 {material_id} 的归档文件哈希不一致。", exit_code=1)
            file_paths.add(path)
        elif source_kind == "external-reference":
            evidence = material.get("version_evidence")
            if (
                status != "confirmed"
                or not isinstance(evidence, dict)
                or evidence.get("status") != "confirmed"
                or evidence.get("evidence") is None
            ):
                raise SdlcError(f"外部资料 {material_id} 缺少稳定版本证据。", exit_code=1)
            detail = evidence.get("evidence")
            if isinstance(detail, dict) and detail.get("kind") == "local_snapshot":
                snapshot_id = str(detail.get("material_id") or "")
                snapshot = by_id.get(snapshot_id)
                if snapshot is None or snapshot.get("source_kind") != "file":
                    raise SdlcError(f"外部资料 {material_id} 的本地版本快照不存在。", exit_code=1)
                path = _material_file_path(draft_id, snapshot)
                actual = review_contract.controlled_input_hashes(paths, [path])[path]
                if actual != detail.get("sha256") or actual != snapshot.get("sha256"):
                    raise SdlcError(f"外部资料 {material_id} 的本地版本快照哈希不一致。", exit_code=1)
                file_paths.add(path)
                version_materials[snapshot_id] = deepcopy(snapshot)
        elif source_kind != "secret-reference":
            raise SdlcError(f"资料 {material_id} 的来源类型不受支持。", exit_code=1)

    return (
        sorted(applicable, key=lambda item: str(item.get("material_id") or "")),
        [version_materials[key] for key in sorted(version_materials)],
        sorted(file_paths),
    )


def _effective_decisions(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    decisions = [
        deepcopy(item)
        for item in draft.get("decision_records", [])
        if isinstance(item, dict) and item.get("status") == "confirmed"
    ]
    identifiers = [str(item.get("decision_id") or "") for item in decisions]
    if "" in identifiers or len(set(identifiers)) != len(identifiers):
        raise SdlcError("当前有效 DEC 的编号缺失或重复。", exit_code=1)
    return sorted(decisions, key=lambda item: str(item["decision_id"]))


def _ensure_no_pending_captures(draft: Mapping[str, Any]) -> None:
    statuses = draft.get("capture_statuses")
    status_map = statuses if isinstance(statuses, dict) else {}
    pending = sorted(
        str(item.get("capture_id") or "CAP")
        for item in draft.get("structured_captures", [])
        if isinstance(item, dict)
        and str(status_map.get(str(item.get("capture_id") or ""), item.get("status") or ""))
        == "pending"
    )
    if pending:
        raise SdlcError(f"当前还有 pending CAP，不能创建需求审核：{', '.join(pending)}。", exit_code=1)


def _build_requirement_review_input(
    paths: ProjectPaths,
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    """从当前结构化 DRAFT 和受控真实文件生成唯一审核输入。"""

    draft_id = str(draft.get("draft_id") or "").strip().upper()
    if not re.fullmatch(r"DRAFT-[0-9]{3,}", draft_id):
        raise SdlcError("需求审核目标 DRAFT 编号无效。", exit_code=1)
    split = draft.get("requirement_split")
    coverage = draft.get("requirement_coverage")
    receipt = draft.get("requirement_import")
    if not all(isinstance(item, dict) for item in (split, coverage, receipt)):
        raise SdlcError("需求拆分、覆盖关系或导入回执不完整。", exit_code=1)
    assert isinstance(split, dict) and isinstance(coverage, dict) and isinstance(receipt, dict)

    (
        source_split_path,
        source_coverage_path,
        fixed_split_path,
        fixed_coverage_path,
        receipt_path,
    ) = _requirement_package_paths(draft_id, receipt)
    source_split = _read_controlled_json(paths, source_split_path, label="不可变需求拆分")
    source_coverage = _read_controlled_json(paths, source_coverage_path, label="不可变需求覆盖")
    fixed_split = _read_controlled_json(paths, fixed_split_path, label="当前需求拆分")
    fixed_coverage = _read_controlled_json(paths, fixed_coverage_path, label="当前需求覆盖")
    fixed_receipt = _read_controlled_json(paths, receipt_path, label="需求导入回执")
    if source_split != split or fixed_split != split:
        raise SdlcError("当前需求拆分与不可变导入包不一致。", exit_code=1)
    if source_coverage != coverage or fixed_coverage != coverage:
        raise SdlcError("当前需求覆盖与不可变导入包不一致。", exit_code=1)
    if fixed_receipt != receipt:
        raise SdlcError("当前需求导入回执与事件状态不一致。", exit_code=1)

    formal_ids = _requirement_known_ids(draft)
    mapping = receipt.get("mapping")
    if not isinstance(mapping, dict):
        raise SdlcError("需求导入回执缺少正式编号映射。", exit_code=1)
    validation_split, validation_coverage = _temporary_requirement_documents(
        split, coverage, mapping
    )
    validation = validate_requirement_contract(
        validation_split,
        validation_coverage,
        project_root=paths.root,
        current_material_hashes=_current_contract_material_hashes(
            draft, split.get("input_material_hashes")
        ),
        expected_draft_id=draft_id,
        known_formal_ids=formal_ids,
    )
    ensure_requirement_review_ready(validation)
    _ensure_no_pending_captures(draft)
    materials, version_materials, material_paths = _validated_material_inputs(
        paths, draft, formal_ids=formal_ids
    )
    decisions = _effective_decisions(draft)

    direct_paths = sorted(
        {
            source_split_path,
            source_coverage_path,
            fixed_split_path,
            fixed_coverage_path,
            receipt_path,
            *material_paths,
        }
    )
    input_hashes = review_contract.controlled_input_hashes(paths, direct_paths)
    dependency_ids = sorted(
        formal_ids
        | {str(item.get("material_id")) for item in materials}
        | {str(item.get("material_id")) for item in version_materials}
        | {str(item.get("decision_id")) for item in decisions}
    )
    body: dict[str, Any] = {
        "schema": _REQUIREMENT_REVIEW_INPUT_SCHEMA,
        "stage": "requirement_split",
        "owner_id": draft_id,
        "requirement_package": {
            "package_key": str(receipt.get("package_key") or ""),
            "package_sha256": str(receipt.get("package_sha256") or ""),
            "mapping": deepcopy(receipt.get("mapping")),
            "source_split_path": source_split_path,
            "source_coverage_path": source_coverage_path,
            "split_contract_sha256": contract_sha256(
                split, schema_name=REQUIREMENT_SPLIT_SCHEMA
            ),
            "coverage_contract_sha256": contract_sha256(
                coverage, schema_name=REQUIREMENT_COVERAGE_SCHEMA
            ),
        },
        "applicable_materials": materials,
        "version_materials": version_materials,
        "effective_decisions": decisions,
        "input_paths": direct_paths,
        "input_hashes": input_hashes,
        "required_checks": list(REQUIREMENT_REVIEW_CHECKS),
        "dependencies": [
            {
                "artifact": "requirement_coverage",
                "depends_on_paths": sorted([source_split_path, source_coverage_path]),
                "depends_on_ids": [],
            },
            {
                "artifact": "requirement_review",
                "depends_on_paths": direct_paths,
                "depends_on_ids": dependency_ids,
            },
            {
                "artifact": "requirement_split",
                "depends_on_paths": sorted([source_split_path, *material_paths]),
                "depends_on_ids": sorted(
                    {str(item.get("material_id")) for item in [*materials, *version_materials]}
                    | {str(item.get("decision_id")) for item in decisions}
                ),
            },
        ],
    }
    return {**body, "snapshot_sha256": canonical_sha256(body)}


def _validate_requirement_review_input(document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "stage",
        "owner_id",
        "requirement_package",
        "applicable_materials",
        "version_materials",
        "effective_decisions",
        "input_paths",
        "input_hashes",
        "required_checks",
        "dependencies",
        "snapshot_sha256",
    }:
        raise SdlcError("需求审核输入快照结构不完整。", exit_code=1)
    if document["schema"] != _REQUIREMENT_REVIEW_INPUT_SCHEMA:
        raise SdlcError("需求审核输入快照版本不正确。", exit_code=1)
    body = {key: deepcopy(value) for key, value in document.items() if key != "snapshot_sha256"}
    if document["snapshot_sha256"] != canonical_sha256(body):
        raise SdlcError("需求审核输入快照摘要不一致。", exit_code=1)
    paths = document.get("input_paths")
    hashes = document.get("input_hashes")
    if (
        not isinstance(paths, list)
        or paths != sorted(paths)
        or len(set(paths)) != len(paths)
        or not isinstance(hashes, dict)
        or list(hashes) != paths
    ):
        raise SdlcError("需求审核输入路径和哈希必须完整、唯一并按路径排序。", exit_code=1)
    if document.get("required_checks") != list(REQUIREMENT_REVIEW_CHECKS):
        raise SdlcError("需求审核检查项与固定合同不一致。", exit_code=1)
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list) or dependencies != sorted(
        dependencies, key=lambda item: str(item.get("artifact") or "") if isinstance(item, dict) else ""
    ):
        raise SdlcError("需求审核显式依赖关系没有按产物排序。", exit_code=1)
    seen: set[str] = set()
    for relation in dependencies:
        if not isinstance(relation, dict) or set(relation) != {
            "artifact",
            "depends_on_paths",
            "depends_on_ids",
        }:
            raise SdlcError("需求审核显式依赖关系结构不正确。", exit_code=1)
        artifact = str(relation["artifact"])
        if not artifact or artifact in seen:
            raise SdlcError("需求审核显式依赖产物不能为空或重复。", exit_code=1)
        seen.add(artifact)
        for field in ("depends_on_paths", "depends_on_ids"):
            values = relation[field]
            if not isinstance(values, list) or values != sorted(values) or len(set(values)) != len(values):
                raise SdlcError(f"需求审核 {field} 必须唯一并按顺序排列。", exit_code=1)
    for field, key in (
        ("applicable_materials", "material_id"),
        ("version_materials", "material_id"),
        ("effective_decisions", "decision_id"),
    ):
        values = document.get(field)
        if not isinstance(values, list):
            raise SdlcError(f"需求审核输入的 {field} 必须是数组。", exit_code=1)
        identifiers = [str(item.get(key) or "") for item in values if isinstance(item, dict)]
        if len(identifiers) != len(values) or identifiers != sorted(identifiers) or len(set(identifiers)) != len(identifiers):
            raise SdlcError(f"需求审核输入的 {field} 必须使用唯一且排序的正式编号。", exit_code=1)
    return deepcopy(document)


def _requirement_input_relative_path(draft_id: str, digest: str) -> str:
    return _draft_relative_path(draft_id, "质检", f"需求审核输入-{digest}.json")


def _write_requirement_review_input(
    paths: ProjectPaths,
    document: Mapping[str, Any],
) -> tuple[str, bool]:
    validated = _validate_requirement_review_input(dict(document))
    draft_id = str(validated["owner_id"])
    digest = str(validated["snapshot_sha256"])
    relative = _requirement_input_relative_path(draft_id, digest)
    target = paths.root / relative
    content = canonical_json_bytes(validated) + b"\n"
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            raise SdlcError("同摘要的需求审核输入文件内容不一致。", exit_code=1)
        return relative, False
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".需求审核输入-{digest}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return relative, True


def _requirement_input_path_from_request(request: Mapping[str, Any]) -> str | None:
    if request.get("stage") != "requirement_split":
        return None
    matches = [
        path
        for path in request.get("input_paths", [])
        if isinstance(path, str) and _REQUIREMENT_INPUT_PATTERN.fullmatch(path)
    ]
    if len(matches) != 1:
        return None
    match = _REQUIREMENT_INPUT_PATTERN.fullmatch(matches[0])
    assert match is not None
    if match.group("draft_id") != request.get("owner_id"):
        return None
    return matches[0]


def _requirement_business_staleness(
    paths: ProjectPaths,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot_path = _requirement_input_path_from_request(request)
    if snapshot_path is None:
        return {"stale": False, "changed_files": [], "reasons": []}
    status_path = _draft_relative_path(str(request["owner_id"]), "status.json")
    manifest_path = _draft_relative_path(str(request["owner_id"]), "material-manifest.v1.json")
    try:
        recorded = _validate_requirement_review_input(
            _read_controlled_json(paths, snapshot_path, label="需求审核输入快照")
        )
        expected_paths = sorted([snapshot_path, *recorded["input_paths"]])
        if request.get("input_paths") != expected_paths:
            raise SdlcError("审核请求没有完整绑定需求审核输入快照。", exit_code=1)
        if request.get("required_checks") != recorded["required_checks"]:
            raise SdlcError("审核请求检查项与需求审核输入快照不一致。", exit_code=1)
        request_hashes = request.get("input_hashes")
        if not isinstance(request_hashes, dict) or any(
            request_hashes.get(path) != digest
            for path, digest in recorded["input_hashes"].items()
        ):
            raise SdlcError("审核请求哈希与需求审核输入快照不一致。", exit_code=1)
        current_draft = _load_draft_status(paths, str(request["owner_id"]))
        current = _validate_requirement_review_input(
            _build_requirement_review_input(paths, current_draft)
        )
    except SdlcError as exc:
        return {
            "stale": True,
            "changed_files": [status_path],
            "reasons": [
                {
                    "kind": "requirement_input_invalid",
                    "path": status_path,
                    "source_paths": [status_path],
                    "detail": exc.message,
                }
            ],
        }
    if current["snapshot_sha256"] == recorded["snapshot_sha256"]:
        return {"stale": False, "changed_files": [], "reasons": []}
    changed = sorted(
        path
        for path in set(recorded["input_hashes"]) | set(current["input_hashes"])
        if recorded["input_hashes"].get(path) != current["input_hashes"].get(path)
    )
    if recorded["effective_decisions"] != current["effective_decisions"]:
        changed.append(status_path)
    if (
        recorded["applicable_materials"] != current["applicable_materials"]
        or recorded["version_materials"] != current["version_materials"]
    ):
        changed.append(manifest_path)
    changed = sorted(set(changed or [status_path]))
    return {
        "stale": True,
        "changed_files": changed,
        "reasons": [
            {
                "kind": "requirement_input_changed",
                "path": path,
                "source_paths": [path],
            }
            for path in changed
        ],
    }


def _integrated_design_input_relative_path(
    draft_id: str,
    digest: str,
) -> str:
    return _draft_relative_path(
        draft_id,
        "质检",
        f"整体设计审核输入-{digest}.json",
    )


def _selected_code_evidence_paths(
    evidence: Mapping[str, Any],
) -> list[str]:
    paths: list[str] = []
    for group in ("rules", "dependencies", "code_files", "upstream_outputs"):
        values = evidence.get(group)
        if not isinstance(values, list):
            raise SdlcError(f"代码证据 {group} 必须是数组。", exit_code=1)
        for item in values:
            path = item.get("path") if isinstance(item, Mapping) else item
            paths.append(review_contract.normalize_input_path(str(path or "")))
    if not paths or len(set(paths)) != len(paths):
        raise SdlcError("整体设计代码证据路径不能为空或重复。", exit_code=1)
    return sorted(paths)


def _build_integrated_design_review_input(
    paths: ProjectPaths,
    draft: Mapping[str, Any],
) -> dict[str, Any]:
    """从事件记录和受管投影重建唯一整体设计审核输入。"""

    from codex_sdlc.core.code_evidence import (
        assess_code_evidence,
        validate_code_evidence,
    )
    from codex_sdlc.core.design_artifact_contract import (
        ENABLED_PLAN_STATUSES,
        design_artifact_records,
        validate_design_artifact_against_plan,
        validate_design_artifact_relations,
    )
    from codex_sdlc.core.design_plan_contract import (
        assess_design_plan,
        design_plan_records,
        validate_design_plan_record,
    )
    from codex_sdlc.core.design_summary_contract import (
        design_summary_records,
        validate_design_summary_against_modules,
    )
    from codex_sdlc.core.state import load_events
    from codex_sdlc.services.design_service import (
        validate_design_reference_record,
        validate_design_reference_source,
    )
    from codex_sdlc.services.draft_service import requirement_confirmation_status

    current = deepcopy(dict(draft))
    draft_id = str(current.get("draft_id") or "").strip().upper()
    if not re.fullmatch(r"DRAFT-[0-9]{3,}", draft_id):
        raise SdlcError("整体设计审核目标 DRAFT 编号无效。", exit_code=1)
    _ensure_no_pending_captures(current)

    confirmation_state = requirement_confirmation_status(
        paths,
        draft_id=draft_id,
        draft=current,
    )
    confirmation = confirmation_state.get("current_confirmation")
    if (
        confirmation_state.get("can_advance") is not True
        or not isinstance(confirmation, dict)
    ):
        raise SdlcError("当前需求确认已经失效，不能创建整体设计审核。", exit_code=1)
    current["_requirement_confirmation_state"] = deepcopy(confirmation_state)
    confirmation_sha256 = str(confirmation.get("confirmation_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", confirmation_sha256):
        raise SdlcError("当前需求确认缺少完整 SHA-256。", exit_code=1)

    split = current.get("requirement_split")
    coverage = current.get("requirement_coverage")
    if not isinstance(split, dict) or not isinstance(coverage, dict):
        raise SdlcError("当前已确认需求缺少拆分或覆盖合同。", exit_code=1)
    requirement_paths = {
        "split": _draft_relative_path(
            draft_id,
            "需求",
            "requirement-split.v1.json",
        ),
        "coverage": _draft_relative_path(
            draft_id,
            "需求",
            "requirement-coverage.v1.json",
        ),
        "confirmation": _draft_relative_path(
            draft_id,
            "需求",
            "requirement-confirmation.v1.json",
        ),
    }
    expected_requirement_documents = {
        requirement_paths["split"]: split,
        requirement_paths["coverage"]: coverage,
        requirement_paths["confirmation"]: confirmation,
    }
    for relative_path, expected in expected_requirement_documents.items():
        if _read_controlled_json(
            paths,
            relative_path,
            label="整体设计审核的已确认需求",
        ) != expected:
            raise SdlcError(
                f"已确认需求投影与当前事件状态不一致：{relative_path}。",
                exit_code=1,
            )

    events = load_events(paths)
    plans = design_plan_records(paths, draft_id=draft_id, events=events)
    if len(plans) != 1:
        raise SdlcError(
            f"{draft_id} 缺少唯一有效的开发设计总计划。",
            exit_code=1,
        )
    plan = validate_design_plan_record(plans[0])
    plan_assessment = assess_design_plan(paths, plan)
    if plan_assessment["status"] != "current":
        changed = "、".join(str(item) for item in plan_assessment["changed_paths"])
        raise SdlcError(
            f"开发设计总计划的代码证据已经变化：{changed}。",
            exit_code=1,
        )
    if plan["input_hashes"].get("requirement_confirmation") != confirmation_sha256:
        raise SdlcError("设计总计划没有绑定当前需求确认完整哈希。", exit_code=1)
    code_evidence = validate_code_evidence(plan["code_evidence"])
    evidence_assessment = assess_code_evidence(paths, code_evidence)
    if evidence_assessment["status"] != "current":
        changed = "、".join(str(item) for item in evidence_assessment["changed_paths"])
        raise SdlcError(f"整体设计代码证据已经变化：{changed}。", exit_code=1)

    enabled_modules = [
        deepcopy(item)
        for item in plan["modules"]
        if item["status"] in ENABLED_PLAN_STATUSES
    ]
    blocked_modules = sorted(
        str(item["module_id"])
        for item in plan["modules"]
        if item["status"] == "blocked"
    )
    if blocked_modules:
        raise SdlcError(
            "尚未满足整体设计审核前置条件：整体设计仍有 blocked 模块："
            + "、".join(blocked_modules)
            + "。",
            exit_code=1,
        )
    enabled_ids = sorted(str(item["module_id"]) for item in enabled_modules)

    references: list[dict[str, Any]] = []
    for raw_reference in current.get("design_references", []):
        if not isinstance(raw_reference, Mapping):
            continue
        reference = validate_design_reference_record(raw_reference)
        if (
            reference["status"] != "confirmed"
            or reference["requirement_confirmation_sha256"]
            != confirmation_sha256
        ):
            continue
        validate_design_reference_source(paths, current, reference)
        references.append(reference)
    references.sort(key=lambda item: str(item["design_id"]))
    if not references:
        raise SdlcError("当前整体设计缺少已确认 DES。", exit_code=1)
    confirmed_ids = {str(item["design_id"]) for item in references}
    plan_design_ids = {
        str(design_id)
        for module in enabled_modules
        for design_id in module["design_refs"]
    }
    if not plan_design_ids.issubset(confirmed_ids):
        missing = sorted(plan_design_ids - confirmed_ids)
        raise SdlcError(
            f"设计总计划引用了未确认 DES：{', '.join(missing)}。",
            exit_code=1,
        )

    artifacts = design_artifact_records(
        paths,
        draft_id=draft_id,
        events=events,
    )
    artifact_ids = sorted(str(item["artifact_id"]) for item in artifacts)
    if artifact_ids != enabled_ids:
        missing = sorted(set(enabled_ids) - set(artifact_ids))
        extra = sorted(set(artifact_ids) - set(enabled_ids))
        details = []
        if missing:
            details.append("缺少 " + "、".join(missing))
        if extra:
            details.append("多出 " + "、".join(extra))
        raise SdlcError(
            "全部启用模块尚未形成唯一当前产物："
            + "；".join(details or ["模块集合不一致"])
            + "。",
            exit_code=1,
        )
    for artifact in artifacts:
        validate_design_artifact_against_plan(
            paths,
            current,
            plan,
            artifact,
        )
        if artifact["open_questions"]:
            raise SdlcError(
                f"模块 {artifact['artifact_id']} 仍有待确认问题，不能创建整体设计审核。",
                exit_code=1,
            )
        if not re.fullmatch(r"[0-9a-f]{64}", str(artifact["artifact_sha256"])):
            raise SdlcError(
                f"模块 {artifact['artifact_id']} 缺少完整 SHA-256。",
                exit_code=1,
            )
    validate_design_artifact_relations(artifacts)

    summaries = design_summary_records(
        paths,
        draft_id=draft_id,
        events=events,
    )
    summary_required = len(enabled_ids) >= 2
    summary: dict[str, Any] | None = None
    summary_status = "not_required"
    if summary_required:
        if len(summaries) != 1:
            raise SdlcError(
                f"{draft_id} 缺少唯一当前总体设计说明。",
                exit_code=1,
            )
        checked_summary, _records = validate_design_summary_against_modules(
            paths,
            current,
            summaries[0],
            events=events,
        )
        if checked_summary["open_questions"]:
            raise SdlcError("总体设计说明仍有待确认问题。", exit_code=1)
        summary = deepcopy(checked_summary)
        summary_status = "current"

    explicit_material_ids = {
        str(item["material_id"])
        for item in references
    } | {
        str(material_id)
        for module in plan["modules"]
        for material_id in module["material_refs"]
    }
    formal_ids = {
        *_requirement_known_ids(current),
        *confirmed_ids,
        *(str(item["module_id"]) for item in plan["modules"]),
    }
    materials, version_materials, material_paths = _validated_material_inputs(
        paths,
        current,
        formal_ids=formal_ids,
        stage="integrated_design",
        explicit_material_ids=explicit_material_ids,
    )
    available_material_ids = {
        str(item["material_id"]) for item in [*materials, *version_materials]
    }
    if not explicit_material_ids.issubset(available_material_ids):
        missing = sorted(explicit_material_ids - available_material_ids)
        raise SdlcError(
            f"整体设计显式引用的适用 MAT 不完整：{', '.join(missing)}。",
            exit_code=1,
        )

    plan_path = _draft_relative_path(draft_id, "设计", "design-plan.v1.json")
    code_evidence_path = _draft_relative_path(
        draft_id,
        "设计",
        "code-evidence.v1.json",
    )
    if _read_controlled_json(paths, plan_path, label="开发设计总计划") != plan:
        raise SdlcError("开发设计总计划投影与事件记录不一致。", exit_code=1)
    if (
        _read_controlled_json(paths, code_evidence_path, label="整体设计代码证据")
        != code_evidence
    ):
        raise SdlcError("代码证据投影与设计总计划不一致。", exit_code=1)

    artifact_paths: list[str] = []
    for artifact in artifacts:
        relative_path = _draft_relative_path(
            draft_id,
            *Path(str(artifact["output_path"])).parts,
        )
        if _read_controlled_json(
            paths,
            relative_path,
            label=f"模块 {artifact['artifact_id']} 产物",
        ) != artifact:
            raise SdlcError(
                f"模块 {artifact['artifact_id']} 投影与事件记录不一致。",
                exit_code=1,
            )
        artifact_paths.append(relative_path)

    summary_paths: list[str] = []
    if summary is not None:
        summary_path = _draft_relative_path(
            draft_id,
            "设计",
            "design-summary.v1.json",
        )
        if _read_controlled_json(paths, summary_path, label="总体设计说明") != summary:
            raise SdlcError("总体设计说明投影与事件记录不一致。", exit_code=1)
        summary_paths.append(summary_path)

    design_source_paths = sorted(
        {
            _draft_relative_path(draft_id, *Path(str(item["path"])).parts)
            for item in references
        }
    )
    code_paths = _selected_code_evidence_paths(code_evidence)
    direct_paths = sorted(
        {
            *requirement_paths.values(),
            *material_paths,
            *design_source_paths,
            plan_path,
            code_evidence_path,
            *artifact_paths,
            *summary_paths,
            *code_paths,
        }
    )
    input_hashes = review_contract.controlled_input_hashes(paths, direct_paths)
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in input_hashes.values()
    ):
        raise SdlcError("整体设计审核输入缺少完整 SHA-256。", exit_code=1)

    body: dict[str, Any] = {
        "schema": _INTEGRATED_DESIGN_REVIEW_INPUT_SCHEMA,
        "stage": "integrated_design",
        "owner_id": draft_id,
        "requirement_confirmation": deepcopy(confirmation),
        "requirement_contract_hashes": {
            "requirement_split": contract_sha256(
                split,
                schema_name=REQUIREMENT_SPLIT_SCHEMA,
            ),
            "requirement_coverage": contract_sha256(
                coverage,
                schema_name=REQUIREMENT_COVERAGE_SCHEMA,
            ),
        },
        "applicable_materials": materials,
        "version_materials": version_materials,
        "confirmed_designs": references,
        "design_plan": deepcopy(plan),
        "design_artifacts": [deepcopy(item) for item in artifacts],
        "summary_status": summary_status,
        "design_summary": deepcopy(summary),
        "code_evidence": deepcopy(code_evidence),
        "input_paths": direct_paths,
        "input_hashes": input_hashes,
        "required_checks": list(INTEGRATED_DESIGN_REVIEW_CHECKS),
        "dependencies": [
            {
                "artifact": "confirmed_requirement",
                "depends_on_paths": sorted(requirement_paths.values()),
                "depends_on_ids": [str(confirmation["confirmation_id"])],
            },
            {
                "artifact": "design_plan",
                "depends_on_paths": sorted(
                    {
                        plan_path,
                        code_evidence_path,
                        *design_source_paths,
                        *code_paths,
                    }
                ),
                "depends_on_ids": sorted(confirmed_ids),
            },
            {
                "artifact": "integrated_design_review",
                "depends_on_paths": direct_paths,
                "depends_on_ids": sorted(
                    {
                        str(confirmation["confirmation_id"]),
                        *confirmed_ids,
                        *enabled_ids,
                        *(str(item["material_id"]) for item in materials),
                    }
                ),
            },
        ],
    }
    return {**body, "snapshot_sha256": canonical_sha256(body)}


def _validate_integrated_design_review_input(
    document: object,
) -> dict[str, Any]:
    from codex_sdlc.core.code_evidence import validate_code_evidence
    from codex_sdlc.core.design_artifact_contract import (
        validate_design_artifact_record,
    )
    from codex_sdlc.core.design_plan_contract import validate_design_plan_record
    from codex_sdlc.core.design_summary_contract import (
        validate_design_summary_record,
    )
    from codex_sdlc.services.design_service import (
        validate_design_reference_record,
    )

    expected_fields = {
        "schema",
        "stage",
        "owner_id",
        "requirement_confirmation",
        "requirement_contract_hashes",
        "applicable_materials",
        "version_materials",
        "confirmed_designs",
        "design_plan",
        "design_artifacts",
        "summary_status",
        "design_summary",
        "code_evidence",
        "input_paths",
        "input_hashes",
        "required_checks",
        "dependencies",
        "snapshot_sha256",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise SdlcError("整体设计审核输入快照结构不完整。", exit_code=1)
    if (
        document["schema"] != _INTEGRATED_DESIGN_REVIEW_INPUT_SCHEMA
        or document["stage"] != "integrated_design"
        or not re.fullmatch(r"DRAFT-[0-9]{3,}", str(document["owner_id"]))
    ):
        raise SdlcError("整体设计审核输入快照版本或目标不正确。", exit_code=1)
    body = {
        key: deepcopy(value)
        for key, value in document.items()
        if key != "snapshot_sha256"
    }
    if document["snapshot_sha256"] != canonical_sha256(body):
        raise SdlcError("整体设计审核输入快照摘要不一致。", exit_code=1)
    input_paths = document["input_paths"]
    input_hashes = document["input_hashes"]
    if (
        not isinstance(input_paths, list)
        or input_paths != sorted(input_paths)
        or len(set(input_paths)) != len(input_paths)
        or not isinstance(input_hashes, dict)
        or list(input_hashes) != input_paths
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(digest))
            for digest in input_hashes.values()
        )
    ):
        raise SdlcError(
            "整体设计审核输入路径和哈希必须完整、唯一并按路径排序。",
            exit_code=1,
        )
    if document["required_checks"] != list(INTEGRATED_DESIGN_REVIEW_CHECKS):
        raise SdlcError("整体设计审核检查项与固定合同不一致。", exit_code=1)
    confirmation = document["requirement_confirmation"]
    contract_hashes = document["requirement_contract_hashes"]
    if (
        not isinstance(confirmation, Mapping)
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(confirmation.get("confirmation_sha256") or ""),
        )
        or not isinstance(contract_hashes, dict)
        or set(contract_hashes) != {
            "requirement_split",
            "requirement_coverage",
        }
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(digest))
            for digest in contract_hashes.values()
        )
    ):
        raise SdlcError("整体设计审核的需求确认哈希不完整。", exit_code=1)
    plan = validate_design_plan_record(document["design_plan"])
    code_evidence = validate_code_evidence(document["code_evidence"])
    if plan["code_evidence"] != code_evidence:
        raise SdlcError("整体设计审核的计划与代码证据不一致。", exit_code=1)
    for field, key in (
        ("applicable_materials", "material_id"),
        ("version_materials", "material_id"),
        ("confirmed_designs", "design_id"),
        ("design_artifacts", "artifact_id"),
    ):
        values = document[field]
        identifiers = [
            str(item.get(key) or "")
            for item in values
            if isinstance(item, Mapping)
        ] if isinstance(values, list) else []
        if (
            len(identifiers) != len(values)
            or identifiers != sorted(identifiers)
            or len(set(identifiers)) != len(identifiers)
            or any(not item for item in identifiers)
        ):
            raise SdlcError(
                f"整体设计审核输入的 {field} 必须使用唯一且排序的正式编号。",
                exit_code=1,
            )
    references = [
        validate_design_reference_record(item)
        for item in document["confirmed_designs"]
    ]
    if any(
        item["status"] != "confirmed"
        or item["requirement_confirmation_sha256"]
        != confirmation["confirmation_sha256"]
        for item in references
    ):
        raise SdlcError("整体设计审核包含未确认或已经失效的 DES。", exit_code=1)
    raw_artifacts = document["design_artifacts"]
    if any(
        isinstance(item, Mapping) and item.get("open_questions")
        for item in raw_artifacts
    ):
        raise SdlcError("整体设计审核的模块仍有待确认问题。", exit_code=1)
    artifacts = [
        validate_design_artifact_record(item)
        for item in raw_artifacts
    ]
    summary_status = document["summary_status"]
    summary = document["design_summary"]
    if (
        (summary_status == "current" and not isinstance(summary, dict))
        or (summary_status == "not_required" and summary is not None)
        or summary_status not in {"current", "not_required"}
    ):
        raise SdlcError("整体设计审核的总体说明状态不一致。", exit_code=1)
    if isinstance(summary, dict):
        if summary.get("open_questions"):
            raise SdlcError("整体设计审核的总体说明仍有待确认问题。", exit_code=1)
        checked_summary = validate_design_summary_record(summary)
    dependencies = document["dependencies"]
    if not isinstance(dependencies, list) or dependencies != sorted(
        dependencies,
        key=lambda item: str(item.get("artifact") or "")
        if isinstance(item, Mapping)
        else "",
    ):
        raise SdlcError("整体设计审核显式依赖没有按产物排序。", exit_code=1)
    seen: set[str] = set()
    for relation in dependencies:
        if not isinstance(relation, dict) or set(relation) != {
            "artifact",
            "depends_on_paths",
            "depends_on_ids",
        }:
            raise SdlcError("整体设计审核显式依赖结构不正确。", exit_code=1)
        artifact = str(relation["artifact"])
        if not artifact or artifact in seen:
            raise SdlcError("整体设计审核显式依赖产物不能为空或重复。", exit_code=1)
        seen.add(artifact)
        for field in ("depends_on_paths", "depends_on_ids"):
            values = relation[field]
            if (
                not isinstance(values, list)
                or values != sorted(values)
                or len(set(values)) != len(values)
            ):
                raise SdlcError(
                    f"整体设计审核 {field} 必须唯一并按顺序排列。",
                    exit_code=1,
                )
    return deepcopy(document)


def _write_integrated_design_review_input(
    paths: ProjectPaths,
    document: Mapping[str, Any],
) -> tuple[str, bool]:
    validated = _validate_integrated_design_review_input(dict(document))
    draft_id = str(validated["owner_id"])
    digest = str(validated["snapshot_sha256"])
    relative = _integrated_design_input_relative_path(draft_id, digest)
    target = paths.root / relative
    content = canonical_json_bytes(validated) + b"\n"
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            raise SdlcError("同摘要的整体设计审核输入文件内容不一致。", exit_code=1)
        return relative, False
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".整体设计审核输入-{digest}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return relative, True


def _task_plan_review_input_relative_path(
    requirement_root: Path,
    digest: str,
) -> str:
    return (
        requirement_root
        / "质检"
        / f"任务审核输入-{digest}.json"
    ).as_posix()


def _regular_files_under(
    paths: ProjectPaths,
    directory: Path,
    *,
    label: str,
    required: bool,
) -> list[str]:
    """审核输入只收普通文件，目录中出现链接时直接拒绝而不是跟随读取。"""

    if not directory.exists():
        if required:
            raise SdlcError(f"{label}目录不存在。", exit_code=1)
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise SdlcError(f"{label}目录不是普通目录。", exit_code=1)
    result: list[str] = []
    for current_root, directory_names, file_names in os.walk(
        directory,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        for name in list(directory_names):
            child = current / name
            if child.is_symlink():
                raise SdlcError(f"{label}不能包含符号链接：{child.name}。", exit_code=1)
        for name in file_names:
            child = current / name
            if child.is_symlink() or not child.is_file():
                raise SdlcError(f"{label}只能包含普通文件：{child.name}。", exit_code=1)
            try:
                relative = child.relative_to(paths.root).as_posix()
            except ValueError as exc:
                raise SdlcError(f"{label}文件越过项目目录。", exit_code=1) from exc
            result.append(review_contract.normalize_input_path(relative))
    return sorted(result)


def _task_plan_code_input_paths(
    paths: ProjectPaths,
    evidence: Mapping[str, Any],
) -> list[str]:
    """外层请求绑定项目内真实代码；父仓规则和缺失占位由证据快照继续校验。"""

    result: set[str] = set()
    for group in ("rules", "dependencies", "code_files", "upstream_outputs"):
        values = evidence.get(group)
        if not isinstance(values, list):
            raise SdlcError(f"任务规划代码证据 {group} 必须是数组。", exit_code=1)
        for item in values:
            if not isinstance(item, Mapping):
                raise SdlcError(f"任务规划代码证据 {group} 项结构无效。", exit_code=1)
            if str(item.get("state") or "present") != "present":
                continue
            raw_path = str(item.get("path") or "")
            if raw_path.startswith("@repo/") or raw_path.startswith("@"):
                # @repo/ 可以指向项目父仓，通用审核合同只允许项目内路径；完整内容哈希、
                # Git 状态和工作树身份已经包含在 code-evidence.v1 及当前审核快照中。
                continue
            normalized = review_contract.normalize_input_path(raw_path)
            target = paths.root / normalized
            if target.exists() and target.is_file() and not target.is_symlink():
                result.add(normalized)
    return sorted(result)


def _task_plan_requirement_root(
    paths: ProjectPaths,
    requirement: Mapping[str, Any],
) -> tuple[Path, str]:
    requirement_id = str(requirement.get("requirement_id") or "").strip().upper()
    folder_name = str(requirement.get("folder_name") or "").strip()
    if (
        not re.fullmatch(r"REQ-[0-9]{3,}", requirement_id)
        or not folder_name
        or Path(folder_name).name != folder_name
    ):
        raise SdlcError("正式需求缺少合法编号或稳定目录。", exit_code=1)
    root = paths.requirements_dir / folder_name
    if root.is_symlink() or not root.is_dir():
        raise SdlcError(f"{requirement_id} 的正式需求目录无效。", exit_code=1)
    return root, root.relative_to(paths.root).as_posix()


def _build_task_plan_review_input(
    paths: ProjectPaths,
    requirement: Mapping[str, Any],
) -> dict[str, Any]:
    """从任务导入回执、正式文件和状态投影重建唯一整套任务审核输入。"""

    from codex_sdlc.core.code_evidence import (
        assess_task_planning_code_evidence,
        validate_code_evidence,
    )
    from codex_sdlc.core.reference_index import validate_reference_index_file
    from codex_sdlc.core.task_contract import load_task_plan_record

    requirement_id = str(requirement.get("requirement_id") or "").strip().upper()
    requirement_root, requirement_root_relative = _task_plan_requirement_root(
        paths,
        requirement,
    )
    coverage_relative = (
        requirement_root
        .joinpath("task-coverage.v1.json")
        .relative_to(paths.root)
        .as_posix()
    )
    coverage = _read_controlled_json(
        paths,
        coverage_relative,
        label="正式任务覆盖文件",
    )
    if requirement.get("task_coverage_contract") != coverage:
        raise SdlcError("正式任务覆盖文件与当前任务状态不一致。", exit_code=1)
    task_plan = load_task_plan_record(paths, requirement_id)
    state_plan = requirement.get("task_plan_contract")
    if not isinstance(state_plan, Mapping) or dict(state_plan) != task_plan:
        raise SdlcError("正式 task-plan.v2 与当前任务状态不一致。", exit_code=1)
    task_ids = [str(item) for item in task_plan.get("tasks", [])]
    if (
        not task_ids
        or len(task_ids) != len(set(task_ids))
        or any(not re.fullmatch(r"T-[0-9]{3,}", task_id) for task_id in task_ids)
    ):
        raise SdlcError("正式 task-plan.v2 的任务清单无效。", exit_code=1)

    tasks: list[dict[str, Any]] = []
    state_tasks = {
        str(item.get("task_id") or ""): item
        for item in requirement.get("tasks", [])
        if isinstance(item, Mapping)
    }
    for task_id in task_ids:
        relative = (
            requirement_root
            .joinpath("tasks", f"{task_id}.json")
            .relative_to(paths.root)
            .as_posix()
        )
        task = _read_controlled_json(
            paths,
            relative,
            label=f"正式任务 {task_id}",
        )
        projected = state_tasks.get(task_id)
        if (
            not isinstance(projected, Mapping)
            or projected.get("task_contract") != task
        ):
            raise SdlcError(f"正式任务 {task_id} 与当前任务状态不一致。", exit_code=1)
        blockers = task.get("blocking_conditions")
        if not isinstance(blockers, list):
            raise SdlcError(f"正式任务 {task_id} 的阻塞条件结构无效。", exit_code=1)
        if blockers:
            raise SdlcError(
                f"任务 {task_id} 仍有阻塞条件，不能创建整套任务审核。",
                exit_code=1,
            )
        if projected.get("status") != "todo":
            raise SdlcError(
                f"任务 {task_id} 已经离开待开发状态，不能重新创建整套任务审核。",
                exit_code=1,
            )
        tasks.append(task)

    reference_relative = (
        requirement_root
        .joinpath("reference-index.v1.json")
        .relative_to(paths.root)
        .as_posix()
    )
    reference_index = validate_reference_index_file(
        requirement_root,
        requirement_root / "reference-index.v1.json",
    )
    if task_plan.get("input_hashes", {}).get(
        "reference_index"
    ) != review_contract.controlled_input_hashes(
        paths,
        [reference_relative],
    )[reference_relative]:
        raise SdlcError("正式引用索引哈希与 task-plan.v2 不一致。", exit_code=1)

    evidence = validate_code_evidence(task_plan.get("code_evidence", {}))
    if (
        evidence.get("purpose") != "task_planning"
        or evidence.get("owner_id") != requirement_id
    ):
        raise SdlcError("task-plan.v2 没有绑定当前需求的任务规划代码证据。", exit_code=1)
    evidence_state = assess_task_planning_code_evidence(
        paths,
        evidence,
        tasks=requirement.get("tasks", []),
    )
    if evidence_state["status"] != "current":
        changed = "、".join(str(item) for item in evidence_state["changed_paths"])
        raise SdlcError(
            f"任务规划代码证据已经过期：{changed or '工作树身份变化'}。",
            exit_code=1,
        )

    formal_input_paths = sorted(
        {
            reference_relative,
            *_regular_files_under(
                paths,
                requirement_root / "original",
                label="正式原文",
                required=True,
            ),
            *_regular_files_under(
                paths,
                requirement_root / "effective",
                label="当前生效需求",
                required=False,
            ),
            *_regular_files_under(
                paths,
                requirement_root / "changes",
                label="正式变更",
                required=False,
            ),
        }
    )
    task_input_paths = sorted(
        {
            coverage_relative,
            *_regular_files_under(
                paths,
                requirement_root / "tasks",
                label="正式任务",
                required=True,
            ),
        }
    )
    code_input_paths = _task_plan_code_input_paths(paths, evidence)
    input_paths = sorted(
        set(formal_input_paths)
        | set(task_input_paths)
        | set(code_input_paths)
    )
    input_hashes = review_contract.controlled_input_hashes(paths, input_paths)
    bound_input_paths = sorted(set(formal_input_paths) | set(task_input_paths))

    body: dict[str, Any] = {
        "schema": _TASK_PLAN_REVIEW_INPUT_SCHEMA,
        "stage": "task_plan",
        "owner_id": requirement_id,
        "requirement_root": requirement_root_relative,
        "task_ids": task_ids,
        "task_plan": deepcopy(task_plan),
        "tasks": deepcopy(tasks),
        "task_coverage": deepcopy(coverage),
        "reference_index": deepcopy(reference_index),
        "code_evidence": deepcopy(evidence),
        "code_evidence_state": deepcopy(evidence_state),
        "formal_input_paths": formal_input_paths,
        "task_input_paths": task_input_paths,
        "code_input_paths": code_input_paths,
        "bound_input_paths": bound_input_paths,
        "input_paths": input_paths,
        "input_hashes": input_hashes,
        "required_checks": list(TASK_PLAN_REVIEW_CHECKS),
        "dependencies": [
            {
                "artifact": "formal_requirement_and_design",
                "depends_on_paths": formal_input_paths,
                "depends_on_ids": sorted(
                    str(reference_id)
                    for reference_id in reference_index["entries"]
                ),
            },
            {
                "artifact": "task_plan",
                "depends_on_paths": task_input_paths,
                "depends_on_ids": task_ids,
            },
            {
                "artifact": "task_planning_code_evidence",
                "depends_on_paths": code_input_paths,
                "depends_on_ids": [str(evidence["evidence_sha256"])],
            },
        ],
    }
    body["dependencies"].sort(key=lambda item: str(item["artifact"]))
    return {**body, "snapshot_sha256": canonical_sha256(body)}


def _validate_task_plan_review_input(
    document: object,
) -> dict[str, Any]:
    from codex_sdlc.core.code_evidence import validate_code_evidence

    expected_fields = {
        "schema",
        "stage",
        "owner_id",
        "requirement_root",
        "task_ids",
        "task_plan",
        "tasks",
        "task_coverage",
        "reference_index",
        "code_evidence",
        "code_evidence_state",
        "formal_input_paths",
        "task_input_paths",
        "code_input_paths",
        "bound_input_paths",
        "input_paths",
        "input_hashes",
        "required_checks",
        "dependencies",
        "snapshot_sha256",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise SdlcError("任务审核输入快照结构不完整。", exit_code=1)
    if (
        document["schema"] != _TASK_PLAN_REVIEW_INPUT_SCHEMA
        or document["stage"] != "task_plan"
        or not re.fullmatch(r"REQ-[0-9]{3,}", str(document["owner_id"]))
    ):
        raise SdlcError("任务审核输入快照版本或目标不正确。", exit_code=1)
    body = {
        key: deepcopy(value)
        for key, value in document.items()
        if key != "snapshot_sha256"
    }
    if document["snapshot_sha256"] != canonical_sha256(body):
        raise SdlcError("任务审核输入快照摘要不一致。", exit_code=1)
    task_ids = document["task_ids"]
    tasks = document["tasks"]
    task_plan = document["task_plan"]
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or len(task_ids) != len(set(task_ids))
        or not isinstance(tasks, list)
        or [item.get("task_id") for item in tasks if isinstance(item, Mapping)]
        != task_ids
        or not isinstance(task_plan, Mapping)
        or task_plan.get("tasks") != task_ids
    ):
        raise SdlcError("任务审核输入的计划和任务集合不一致。", exit_code=1)
    if any(
        not isinstance(item, Mapping) or item.get("blocking_conditions")
        for item in tasks
    ):
        raise SdlcError("任务审核输入包含阻塞条件。", exit_code=1)
    evidence = validate_code_evidence(document["code_evidence"])
    evidence_state = document["code_evidence_state"]
    if (
        evidence.get("purpose") != "task_planning"
        or evidence.get("owner_id") != document["owner_id"]
        or not isinstance(evidence_state, Mapping)
        or evidence_state.get("status") != "current"
    ):
        raise SdlcError("任务审核输入的规划代码证据无效。", exit_code=1)
    list_fields = (
        "formal_input_paths",
        "task_input_paths",
        "code_input_paths",
        "bound_input_paths",
        "input_paths",
    )
    for field in list_fields:
        values = document[field]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or values != sorted(values)
            or len(values) != len(set(values))
        ):
            raise SdlcError(f"任务审核输入的 {field} 必须唯一并按路径排序。", exit_code=1)
    if not document["formal_input_paths"] or not document["task_input_paths"]:
        raise SdlcError("任务审核输入缺少正式需求、设计或任务文件。", exit_code=1)
    if document["bound_input_paths"] != sorted(
        set(document["formal_input_paths"]) | set(document["task_input_paths"])
    ):
        raise SdlcError("任务审核请求绑定的正式输入集合不完整。", exit_code=1)
    if document["input_paths"] != sorted(
        set(document["bound_input_paths"]) | set(document["code_input_paths"])
    ):
        raise SdlcError("任务审核输入的完整路径集合不一致。", exit_code=1)
    input_hashes = document["input_hashes"]
    if (
        not isinstance(input_hashes, dict)
        or list(input_hashes) != document["input_paths"]
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(digest))
            for digest in input_hashes.values()
        )
    ):
        raise SdlcError("任务审核输入路径和哈希集合不完整。", exit_code=1)
    if document["required_checks"] != list(TASK_PLAN_REVIEW_CHECKS):
        raise SdlcError("任务审核检查项与固定合同不一致。", exit_code=1)
    dependencies = document["dependencies"]
    if (
        not isinstance(dependencies, list)
        or any(not isinstance(item, Mapping) for item in dependencies)
        or dependencies != sorted(
            dependencies,
            key=lambda item: str(item.get("artifact") or "")
            if isinstance(item, Mapping)
            else "",
        )
    ):
        raise SdlcError("任务审核显式依赖没有按产物排序。", exit_code=1)
    artifacts: set[str] = set()
    for relation in dependencies:
        if set(relation) != {
            "artifact",
            "depends_on_paths",
            "depends_on_ids",
        }:
            raise SdlcError("任务审核显式依赖结构不完整。", exit_code=1)
        artifact = str(relation["artifact"])
        if not artifact or artifact in artifacts:
            raise SdlcError("任务审核显式依赖产物不能为空或重复。", exit_code=1)
        artifacts.add(artifact)
        for field in ("depends_on_paths", "depends_on_ids"):
            values = relation[field]
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or values != sorted(values)
                or len(values) != len(set(values))
            ):
                raise SdlcError(
                    f"任务审核显式依赖 {field} 必须唯一并按顺序排列。",
                    exit_code=1,
                )
    return deepcopy(document)


def _write_task_plan_review_input(
    paths: ProjectPaths,
    requirement_root: Path,
    document: Mapping[str, Any],
) -> tuple[str, bool]:
    validated = _validate_task_plan_review_input(dict(document))
    digest = str(validated["snapshot_sha256"])
    relative_root = Path(str(validated["requirement_root"]))
    relative = _task_plan_review_input_relative_path(relative_root, digest)
    target = paths.root / relative
    try:
        target.relative_to(requirement_root)
    except ValueError as exc:
        raise SdlcError("任务审核输入快照不属于当前正式需求。", exit_code=1) from exc
    content = canonical_json_bytes(validated) + b"\n"
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
            raise SdlcError("同摘要的任务审核输入文件内容不一致。", exit_code=1)
        return relative, False
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".任务审核输入-{digest}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return relative, True


def _task_plan_input_path_from_request(
    request: Mapping[str, Any],
) -> str | None:
    if request.get("stage") != "task_plan":
        return None
    matches = [
        path
        for path in request.get("input_paths", [])
        if isinstance(path, str) and _TASK_PLAN_INPUT_PATTERN.fullmatch(path)
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _task_plan_business_staleness(
    paths: ProjectPaths,
    request: Mapping[str, Any],
    *,
    current_tasks: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    from codex_sdlc.core.code_evidence import assess_task_planning_code_evidence

    snapshot_path = _task_plan_input_path_from_request(request)
    if snapshot_path is None:
        return {"stale": False, "changed_files": [], "reasons": []}
    try:
        recorded = _validate_task_plan_review_input(
            _read_controlled_json(
                paths,
                snapshot_path,
                label="任务审核输入快照",
            )
        )
        expected_request_paths = sorted(
            [snapshot_path, *recorded["bound_input_paths"]]
        )
        if request.get("input_paths") != expected_request_paths:
            raise SdlcError("审核请求没有完整绑定任务审核正式输入。", exit_code=1)
        if request.get("required_checks") != recorded["required_checks"]:
            raise SdlcError("审核请求检查项与任务审核输入快照不一致。", exit_code=1)
        request_hashes = request.get("input_hashes")
        snapshot_digest = review_contract.controlled_input_hashes(
            paths,
            [snapshot_path],
        )[snapshot_path]
        if (
            not isinstance(request_hashes, dict)
            or set(request_hashes) != set(expected_request_paths)
            or request_hashes.get(snapshot_path) != snapshot_digest
            or any(
                request_hashes.get(path) != recorded["input_hashes"].get(path)
                for path in recorded["bound_input_paths"]
            )
        ):
            raise SdlcError("审核请求哈希与任务审核输入快照不一致。", exit_code=1)
        requirement_root = paths.root / str(recorded["requirement_root"])
        current_formal = sorted(
            {
                (
                    requirement_root
                    / "reference-index.v1.json"
                ).relative_to(paths.root).as_posix(),
                *_regular_files_under(
                    paths,
                    requirement_root / "original",
                    label="正式原文",
                    required=True,
                ),
                *_regular_files_under(
                    paths,
                    requirement_root / "effective",
                    label="当前生效需求",
                    required=False,
                ),
                *_regular_files_under(
                    paths,
                    requirement_root / "changes",
                    label="正式变更",
                    required=False,
                ),
            }
        )
        current_task_inputs = sorted(
            {
                (
                    requirement_root
                    / "task-coverage.v1.json"
                ).relative_to(paths.root).as_posix(),
                *_regular_files_under(
                    paths,
                    requirement_root / "tasks",
                    label="正式任务",
                    required=True,
                ),
            }
        )
        evidence_state = assess_task_planning_code_evidence(
            paths,
            recorded["code_evidence"],
            tasks=(
                list(current_tasks)
                if current_tasks is not None
                else recorded["tasks"]
            ),
        )
    except (SdlcError, OSError, ValueError) as exc:
        detail = exc.message if isinstance(exc, SdlcError) else str(exc)
        return {
            "stale": True,
            "changed_files": [snapshot_path],
            "reasons": [
                {
                    "kind": "task_plan_input_invalid",
                    "path": snapshot_path,
                    "source_paths": [snapshot_path],
                    "detail": detail,
                }
            ],
        }
    changed = sorted(
        set(recorded["formal_input_paths"]).symmetric_difference(current_formal)
        | set(recorded["task_input_paths"]).symmetric_difference(current_task_inputs)
        | set(str(item) for item in evidence_state["changed_paths"])
    )
    if not changed:
        return {"stale": False, "changed_files": [], "reasons": []}
    return {
        "stale": True,
        "changed_files": changed,
        "reasons": [
            {
                "kind": "task_plan_input_changed",
                "path": path,
                "source_paths": [path],
            }
            for path in changed
        ],
    }


def _integrated_design_input_path_from_request(
    request: Mapping[str, Any],
) -> str | None:
    if request.get("stage") != "integrated_design":
        return None
    matches = [
        path
        for path in request.get("input_paths", [])
        if isinstance(path, str)
        and _INTEGRATED_DESIGN_INPUT_PATTERN.fullmatch(path)
    ]
    if len(matches) != 1:
        return None
    match = _INTEGRATED_DESIGN_INPUT_PATTERN.fullmatch(matches[0])
    assert match is not None
    if match.group("draft_id") != request.get("owner_id"):
        return None
    return matches[0]


def _integrated_design_business_staleness(
    paths: ProjectPaths,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    snapshot_path = _integrated_design_input_path_from_request(request)
    if snapshot_path is None:
        if (
            request.get("stage") == "integrated_design"
            and _is_managed_draft_owner(paths, request.get("owner_id"))
        ):
            draft_id = str(request.get("owner_id") or "")
            status_path = _draft_relative_path(draft_id, "status.json")
            return {
                "stale": True,
                "changed_files": [status_path],
                "reasons": [
                    {
                        "kind": "integrated_design_snapshot_missing",
                        "path": status_path,
                        "source_paths": [status_path],
                    }
                ],
            }
        return {"stale": False, "changed_files": [], "reasons": []}
    draft_id = str(request["owner_id"])
    status_path = _draft_relative_path(draft_id, "status.json")
    material_manifest_path = _draft_relative_path(
        draft_id,
        "material-manifest.v1.json",
    )
    design_index_path = _draft_relative_path(
        draft_id,
        "设计",
        "des-index.v1.json",
    )
    confirmation_path = _draft_relative_path(
        draft_id,
        "需求",
        "requirement-confirmation.v1.json",
    )
    try:
        recorded = _validate_integrated_design_review_input(
            _read_controlled_json(
                paths,
                snapshot_path,
                label="整体设计审核输入快照",
            )
        )
        expected_paths = sorted([snapshot_path, *recorded["input_paths"]])
        if request.get("input_paths") != expected_paths:
            raise SdlcError("审核请求没有完整绑定整体设计审核输入快照。", exit_code=1)
        if request.get("required_checks") != recorded["required_checks"]:
            raise SdlcError("审核请求检查项与整体设计审核输入快照不一致。", exit_code=1)
        request_hashes = request.get("input_hashes")
        snapshot_digest = review_contract.controlled_input_hashes(
            paths,
            [snapshot_path],
        )[snapshot_path]
        if (
            not isinstance(request_hashes, dict)
            or set(request_hashes) != set(expected_paths)
            or request_hashes.get(snapshot_path) != snapshot_digest
            or any(
                request_hashes.get(path) != digest
                for path, digest in recorded["input_hashes"].items()
            )
        ):
            raise SdlcError("审核请求哈希与整体设计审核输入快照不一致。", exit_code=1)
        current_draft = _load_draft_status(paths, draft_id)
        current = _validate_integrated_design_review_input(
            _build_integrated_design_review_input(paths, current_draft)
        )
    except SdlcError as exc:
        return {
            "stale": True,
            "changed_files": [status_path],
            "reasons": [
                {
                    "kind": "integrated_design_input_invalid",
                    "path": status_path,
                    "source_paths": [status_path],
                    "detail": exc.message,
                }
            ],
        }
    if current["snapshot_sha256"] == recorded["snapshot_sha256"]:
        return {"stale": False, "changed_files": [], "reasons": []}
    changed = [
        path
        for path in set(recorded["input_hashes"]) | set(current["input_hashes"])
        if recorded["input_hashes"].get(path) != current["input_hashes"].get(path)
    ]
    if recorded["requirement_confirmation"] != current["requirement_confirmation"]:
        changed.append(confirmation_path)
    if recorded["confirmed_designs"] != current["confirmed_designs"]:
        changed.append(design_index_path)
    if (
        recorded["applicable_materials"] != current["applicable_materials"]
        or recorded["version_materials"] != current["version_materials"]
    ):
        changed.append(material_manifest_path)
    changed = sorted(set(changed or [status_path]))
    return {
        "stale": True,
        "changed_files": changed,
        "reasons": [
            {
                "kind": "integrated_design_input_changed",
                "path": path,
                "source_paths": [path],
            }
            for path in changed
        ],
    }


def _review_trust_dir(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "trust" / "reviews"


def _cleanup_trust_temps(paths: ProjectPaths) -> None:
    directory = _review_trust_dir(paths)
    if not directory.exists():
        return
    removed = False
    for candidate in directory.iterdir():
        if not _TRUST_TEMP_PATTERN.fullmatch(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed = True
    if removed:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                    raise
        finally:
            os.close(descriptor)


def recover_review_storage(paths: ProjectPaths) -> None:
    """写入口先清理审核登记和依赖图的自有临时文件，正式文件保持原样。"""

    with project_lock(paths):
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)


def _latest_by_target(registry: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    latest: dict[tuple[str, str], str] = {}
    for review_id, request_record in registry["requests"].items():
        request = request_record["request"]
        target = (request["stage"], request["owner_id"])
        previous_id = latest.get(target)
        if previous_id is None:
            latest[target] = review_id
            continue
        previous = registry["requests"][previous_id]["request"]
        if (request["created_at"], review_id) > (previous["created_at"], previous_id):
            latest[target] = review_id
    return latest


def _effective_record(
    paths: ProjectPaths,
    registry: Mapping[str, Any],
    graph: Mapping[str, Any],
    review_id: str,
    *,
    latest_by_target: Mapping[tuple[str, str], str],
    task_plan_tasks: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """复用、status 和推进判断共用同一套真实有效性计算。"""

    request_record = registry["requests"][review_id]
    request = request_record["request"]
    registration_id = request_record["result_registration_id"]
    registration = registry["registrations"].get(registration_id) if registration_id else None
    result = registration["result"] if isinstance(registration, dict) else None
    stale = dependency_graph.review_staleness(
        paths,
        request,
        request_record["dependency_snapshot"],
        graph,
    )
    # 通用依赖图负责文件漂移，业务快照重建负责“记录仍在但输入集合已经变化”。
    # 两类结果必须合并，否则新增 MAT、替换 DES 等集合变化会绕过旧请求。
    for business_stale in (
        _requirement_business_staleness(paths, request),
        _integrated_design_business_staleness(paths, request),
        _task_plan_business_staleness(
            paths,
            request,
            current_tasks=task_plan_tasks,
        ),
    ):
        if business_stale["stale"]:
            stale = {
                "stale": True,
                "changed_files": sorted(
                    set(stale["changed_files"])
                    | set(business_stale["changed_files"])
                ),
                "reasons": [*stale["reasons"], *business_stale["reasons"]],
            }
    recorded_status = result["status"] if isinstance(result, dict) else "pending"
    effective_status = "stale" if stale["stale"] else recorded_status
    target = (request["stage"], request["owner_id"])
    is_current = latest_by_target[target] == review_id
    can_advance = (
        is_current
        and request_record["status"] == "completed"
        and effective_status == "passed"
    )
    return {
        "review_id": review_id,
        "stage": request["stage"],
        "owner_id": request["owner_id"],
        "request_status": request_record["status"],
        "recorded_status": recorded_status,
        "effective_status": effective_status,
        "is_current": is_current,
        "can_advance": can_advance,
        "registration_id": registration_id,
        "input_hashes": deepcopy(request["input_hashes"]),
        "changed_files": stale["changed_files"],
        "stale_reasons": stale["reasons"],
        "issues": deepcopy(result["issues"]) if isinstance(result, dict) else [],
        "notes": deepcopy(result["notes"]) if isinstance(result, dict) else [],
        "reviewer_run_id": result["reviewer_run_id"] if isinstance(result, dict) else "",
    }


def _load_registry_if_present(paths: ProjectPaths) -> dict[str, Any] | None:
    trust_dir = _review_trust_dir(paths)
    if not (trust_dir / ".key").exists() and not (trust_dir / "registry.json").exists():
        return None
    return fact_review_trust.load_review_registry(paths)


def _register_review_request_locked(
    paths: ProjectPaths,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """调用方持有项目锁时，复用同一套快照、幂等和可信登记合同。"""

    graph = dependency_graph.load_dependency_graph(paths)
    snapshot = dependency_graph.build_dependency_snapshot(paths, request, graph)
    registry = _load_registry_if_present(paths)
    reusable_registration_id = None
    if registry is not None:
        latest = _latest_by_target(registry)
        fingerprint = review_contract.review_input_fingerprint(request)
        for existing_id, request_record in registry["requests"].items():
            if request_record["input_fingerprint"] != fingerprint:
                continue
            effective = _effective_record(
                paths,
                registry,
                graph,
                existing_id,
                latest_by_target=latest,
            )
            if effective["can_advance"]:
                reusable_registration_id = effective["registration_id"]
                break
    outcome = fact_review_trust.register_trusted_review_request_locked(
        paths,
        request=request,
        dependency_snapshot=snapshot,
        reusable_registration_id=reusable_registration_id,
    )
    action = "reused" if outcome.reused else "idempotent" if outcome.idempotent else "created"
    registration = outcome.registration
    effective_status = "pending"
    if isinstance(registration, dict):
        effective_status = registration["result"]["status"]
    return {
        "action": action,
        "request": deepcopy(outcome.request),
        "registration_id": (
            registration.get("registration_id") if isinstance(registration, dict) else None
        ),
        "effective_status": effective_status,
    }


def create_review(
    paths: ProjectPaths,
    *,
    review_id: str,
    stage: str,
    owner_id: str,
    input_paths: Iterable[str | Path],
    required_checks: Iterable[str] = (),
    created_at: str | None = None,
) -> dict[str, Any]:
    clean_owner_id = str(owner_id or "").strip().upper()
    if re.fullmatch(r"CHG-[0-9]{3,}", clean_owner_id):
        # 正式变更审核继续走三类通用审核；输入必须从 T-032 已登记的六份文件重建，
        # 调用方给出的 REV、input 和 check 都不能缩减或替换真实影响范围。
        from codex_sdlc.services.change_service import change_review_context_locked

        with project_lock(paths):
            _cleanup_trust_temps(paths)
            dependency_graph.recover_dependency_graph_storage_locked(paths)
            context = change_review_context_locked(
                paths,
                change_id=clean_owner_id,
                stage=stage,
            )
            registry = _load_registry_if_present(paths)
            request = review_contract.build_review_request(
                paths,
                review_id=_next_review_id(registry),
                stage=stage,
                owner_id=clean_owner_id,
                input_paths=context["input_paths"],
                required_checks=context["required_checks"],
                created_at=created_at,
            )
            return _register_review_request_locked(paths, request)
    if stage == "integrated_design" and _is_managed_draft_owner(paths, owner_id):
        # 路由必须位于通用锁和参数求值之前：专用构建器会自己持有项目锁，
        # 同时调用方传入的编号、文件和检查项都不能污染系统重建的完整快照。
        return create_integrated_design_review(
            paths,
            draft_id=owner_id,
            created_at=created_at,
        )
    if stage == "task_plan" and _is_managed_task_plan_owner(paths, owner_id):
        # 正式整套任务审核同样不信任调用方给出的编号、输入和检查项，避免把
        # 一份任务或一份覆盖文件伪装成完整任务计划审核。
        return create_task_plan_review(
            paths,
            requirement_id=owner_id,
            created_at=created_at,
        )
    raw_inputs = list(input_paths)
    checks = tuple(required_checks)
    with project_lock(paths):
        # 恢复、词法路径终检、哈希、依赖快照、复用判断和发布全部属于同一临界区。
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)
        request = review_contract.build_review_request(
            paths,
            review_id=review_id,
            stage=stage,
            owner_id=owner_id,
            input_paths=raw_inputs,
            required_checks=checks,
            created_at=created_at,
        )
        return _register_review_request_locked(paths, request)


def _next_review_id(registry: Mapping[str, Any] | None) -> str:
    numbers = [
        int(match.group(1))
        for review_id in (registry.get("requests", {}) if isinstance(registry, Mapping) else {})
        if (match := _REVIEW_ID_PATTERN.fullmatch(str(review_id))) is not None
    ]
    return f"REV-{max(numbers, default=0) + 1:03d}"


def _cleanup_requirement_review_inputs_locked(
    paths: ProjectPaths,
    draft_id: str,
    registry: Mapping[str, Any] | None,
) -> None:
    """只清理没有被可信请求引用的本入口文件和临时文件。"""

    quality_dir = paths.root / _draft_relative_path(draft_id, "质检")
    if not quality_dir.exists() or quality_dir.is_symlink() or not quality_dir.is_dir():
        return
    referenced = {
        path
        for record in (
            registry.get("requests", {}).values() if isinstance(registry, Mapping) else []
        )
        if isinstance(record, dict)
        for path in record.get("request", {}).get("input_paths", [])
        if isinstance(path, str) and _REQUIREMENT_INPUT_PATTERN.fullmatch(path)
    }
    removed = False
    for candidate in quality_dir.iterdir():
        relative = candidate.relative_to(paths.root).as_posix()
        is_snapshot = _REQUIREMENT_INPUT_PATTERN.fullmatch(relative) is not None
        is_temp = _REQUIREMENT_INPUT_TEMP_PATTERN.fullmatch(candidate.name) is not None
        if not is_temp and (not is_snapshot or relative in referenced):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed = True
    if removed:
        descriptor = os.open(quality_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_integrated_design_review_inputs_locked(
    paths: ProjectPaths,
    draft_id: str,
    registry: Mapping[str, Any] | None,
) -> None:
    """只删除整体设计审核入口遗留的临时文件和未登记快照。"""

    quality_dir = paths.root / _draft_relative_path(draft_id, "质检")
    if not quality_dir.exists() or quality_dir.is_symlink() or not quality_dir.is_dir():
        return
    referenced = {
        path
        for record in (
            registry.get("requests", {}).values()
            if isinstance(registry, Mapping)
            else []
        )
        if isinstance(record, dict)
        for path in record.get("request", {}).get("input_paths", [])
        if isinstance(path, str)
        and _INTEGRATED_DESIGN_INPUT_PATTERN.fullmatch(path)
    }
    removed = False
    for candidate in quality_dir.iterdir():
        relative = candidate.relative_to(paths.root).as_posix()
        is_snapshot = _INTEGRATED_DESIGN_INPUT_PATTERN.fullmatch(relative) is not None
        is_temp = (
            _INTEGRATED_DESIGN_INPUT_TEMP_PATTERN.fullmatch(candidate.name)
            is not None
        )
        if not is_temp and (not is_snapshot or relative in referenced):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed = True
    if removed:
        descriptor = os.open(quality_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_task_plan_review_inputs_locked(
    paths: ProjectPaths,
    requirement_root: Path,
    registry: Mapping[str, Any] | None,
) -> None:
    """只删除当前整套任务审核入口遗留的临时文件和未登记快照。"""

    quality_dir = requirement_root / "质检"
    if not quality_dir.exists() or quality_dir.is_symlink() or not quality_dir.is_dir():
        return
    referenced = {
        path
        for record in (
            registry.get("requests", {}).values()
            if isinstance(registry, Mapping)
            else []
        )
        if isinstance(record, dict)
        for path in record.get("request", {}).get("input_paths", [])
        if isinstance(path, str) and _TASK_PLAN_INPUT_PATTERN.fullmatch(path)
    }
    removed = False
    for candidate in quality_dir.iterdir():
        try:
            relative = candidate.relative_to(paths.root).as_posix()
        except ValueError:
            continue
        is_snapshot = _TASK_PLAN_INPUT_PATTERN.fullmatch(relative) is not None
        is_temp = _TASK_PLAN_INPUT_TEMP_PATTERN.fullmatch(candidate.name) is not None
        if not is_temp and (not is_snapshot or relative in referenced):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed = True
    if removed:
        descriptor = os.open(quality_dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def create_requirement_review(
    paths: ProjectPaths,
    *,
    draft_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """为当前活动 DRAFT 创建完整且不可删减的需求审核请求。"""

    clean_id = str(draft_id or "").strip().upper()
    if not re.fullmatch(r"DRAFT-[0-9]{3,}", clean_id):
        raise SdlcError(f"DRAFT 编号格式不正确：{clean_id}。", exit_code=1)
    # 身份门禁先于任何入口文件写入，不能用本地默认值代替真实 Codex 任务。
    review_contract.current_thread_id(action="创建需求审核请求")
    created_snapshot: Path | None = None
    with project_lock(paths):
        trust_dir_existed = _review_trust_dir(paths).exists()
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)
        registry = _load_registry_if_present(paths)
        _cleanup_requirement_review_inputs_locked(paths, clean_id, registry)

        # 延迟导入避免 state.py 投影通用审核状态时形成模块循环。
        from codex_sdlc.core.state import active_draft, derive_state

        state = derive_state(paths)
        target = state.get("drafts", {}).get(clean_id)
        if not isinstance(target, dict) or target.get("status") == "started":
            raise SdlcError(f"没有找到可审核的活动 DRAFT：{clean_id}。", exit_code=1)
        current = active_draft(state)
        if not isinstance(current, dict) or current.get("draft_id") != clean_id:
            current_id = str(current.get("draft_id") or "无") if isinstance(current, dict) else "无"
            raise SdlcError(
                f"{clean_id} 不是当前活动 DRAFT，当前活动 DRAFT 为 {current_id}。",
                exit_code=1,
            )
        if target.get("status") != "requirement_reviewing":
            raise SdlcError(
                f"{clean_id} 当前状态为 {target.get('status')}，尚未满足需求审核前置条件。",
                exit_code=1,
            )
        projected = _load_draft_status(paths, clean_id, expected_draft=target)
        review_input = _validate_requirement_review_input(
            _build_requirement_review_input(paths, projected)
        )
        snapshot_path, snapshot_created = _write_requirement_review_input(paths, review_input)
        if snapshot_created:
            created_snapshot = paths.root / snapshot_path
        try:
            review_id = _next_review_id(registry)
            request = review_contract.build_review_request(
                paths,
                review_id=review_id,
                stage="requirement_split",
                owner_id=clean_id,
                input_paths=[snapshot_path, *review_input["input_paths"]],
                required_checks=REQUIREMENT_REVIEW_CHECKS,
                created_at=created_at,
            )
            for path, digest in review_input["input_hashes"].items():
                if request["input_hashes"].get(path) != digest:
                    raise SdlcError(f"审核输入在创建请求期间发生变化：{path}。", exit_code=1)

            graph = dependency_graph.load_dependency_graph(paths)
            if registry is not None:
                latest = _latest_by_target(registry)
                target_key = ("requirement_split", clean_id)
                current_id = latest.get(target_key)
                if current_id is not None:
                    current_effective = _effective_record(
                        paths,
                        registry,
                        graph,
                        current_id,
                        latest_by_target=latest,
                    )
                    same_fingerprint = (
                        registry["requests"][current_id]["input_fingerprint"]
                        == review_contract.review_input_fingerprint(request)
                    )
                    if current_effective["effective_status"] == "pending" and same_fingerprint:
                        return {
                            "action": "idempotent",
                            "request": deepcopy(registry["requests"][current_id]["request"]),
                            "registration_id": None,
                            "effective_status": "pending",
                        }
                    if current_effective["effective_status"] == "needs_fix" and same_fingerprint:
                        raise SdlcError(
                            "需求审核返回 needs_fix 后，必须先改变受控输入再创建新审核轮次。",
                            exit_code=1,
                        )
            return _register_review_request_locked(paths, request)
        except Exception:
            if created_snapshot is not None:
                created_snapshot.unlink(missing_ok=True)
            trust_dir = _review_trust_dir(paths)
            if (
                not trust_dir_existed
                and trust_dir.exists()
                and not (trust_dir / "registry.json").exists()
            ):
                _cleanup_trust_temps(paths)
                (trust_dir / ".key").unlink(missing_ok=True)
                try:
                    trust_dir.rmdir()
                    trust_dir.parent.rmdir()
                except OSError:
                    pass
            raise


def create_integrated_design_review(
    paths: ProjectPaths,
    *,
    draft_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """为当前完整设计创建唯一审核请求，失败时不留下未登记快照。"""

    clean_id = str(draft_id or "").strip().upper()
    if not re.fullmatch(r"DRAFT-[0-9]{3,}", clean_id):
        raise SdlcError(f"DRAFT 编号格式不正确：{clean_id}。", exit_code=1)
    # 先验证真实任务身份，再碰任何受管文件，避免身份缺失留下半成品。
    review_contract.current_thread_id(action="创建整体设计审核请求")
    created_snapshot: Path | None = None
    with project_lock(paths):
        trust_dir_existed = _review_trust_dir(paths).exists()
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)
        registry = _load_registry_if_present(paths)
        _cleanup_integrated_design_review_inputs_locked(
            paths,
            clean_id,
            registry,
        )

        # 状态层会重建所有结构化设计输入；延迟导入避免形成模块循环。
        from codex_sdlc.core.state import active_draft, derive_state

        state = derive_state(paths)
        target = state.get("drafts", {}).get(clean_id)
        if not isinstance(target, dict) or target.get("status") == "started":
            raise SdlcError(
                f"没有找到可审核的活动 DRAFT：{clean_id}。",
                exit_code=1,
            )
        current = active_draft(state)
        if not isinstance(current, dict) or current.get("draft_id") != clean_id:
            current_id = (
                str(current.get("draft_id") or "无")
                if isinstance(current, dict)
                else "无"
            )
            raise SdlcError(
                f"{clean_id} 不是当前活动 DRAFT，当前活动 DRAFT 为 {current_id}。",
                exit_code=1,
            )
        # 审核登记不是 DRAFT 事件，passed/stale 会立即改变内存状态，但不会改写
        # status.json。整体设计输入只读取事件投影中的业务产物，不能因此要求状态
        # 展示字段也在同一次只读调用中被重写。
        projected = _load_draft_status(paths, clean_id)
        review_input = _validate_integrated_design_review_input(
            _build_integrated_design_review_input(paths, projected)
        )
        design_stage = target.get("design_stage")
        if (
            not isinstance(design_stage, Mapping)
            or design_stage.get("ready_for_review") is not True
            or design_stage.get("blockers")
            or target.get("status") not in {"design_reviewing", "start_ready"}
        ):
            raise SdlcError(
                f"{clean_id} 尚未满足整体设计审核前置条件。",
                exit_code=1,
            )
        snapshot_path, snapshot_created = _write_integrated_design_review_input(
            paths,
            review_input,
        )
        if snapshot_created:
            created_snapshot = paths.root / snapshot_path
        try:
            review_id = _next_review_id(registry)
            request = review_contract.build_review_request(
                paths,
                review_id=review_id,
                stage="integrated_design",
                owner_id=clean_id,
                input_paths=[snapshot_path, *review_input["input_paths"]],
                required_checks=INTEGRATED_DESIGN_REVIEW_CHECKS,
                created_at=created_at,
            )
            for path, digest in review_input["input_hashes"].items():
                if request["input_hashes"].get(path) != digest:
                    raise SdlcError(
                        f"整体设计审核输入在创建请求期间发生变化：{path}。",
                        exit_code=1,
                    )

            graph = dependency_graph.load_dependency_graph(paths)
            if registry is not None:
                latest = _latest_by_target(registry)
                current_id = latest.get(("integrated_design", clean_id))
                if current_id is not None:
                    current_effective = _effective_record(
                        paths,
                        registry,
                        graph,
                        current_id,
                        latest_by_target=latest,
                    )
                    same_fingerprint = (
                        registry["requests"][current_id]["input_fingerprint"]
                        == review_contract.review_input_fingerprint(request)
                    )
                    if same_fingerprint and current_effective["effective_status"] == "pending":
                        return {
                            "action": "idempotent",
                            "request": deepcopy(
                                registry["requests"][current_id]["request"]
                            ),
                            "registration_id": None,
                            "effective_status": "pending",
                        }
                    if (
                        same_fingerprint
                        and current_effective["effective_status"] == "needs_fix"
                    ):
                        raise SdlcError(
                            "整体设计审核返回 needs_fix 后，必须先改变受控输入"
                            "再创建新审核轮次。",
                            exit_code=1,
                        )
            return _register_review_request_locked(paths, request)
        except Exception:
            if created_snapshot is not None:
                created_snapshot.unlink(missing_ok=True)
            trust_dir = _review_trust_dir(paths)
            if (
                not trust_dir_existed
                and trust_dir.exists()
                and not (trust_dir / "registry.json").exists()
            ):
                _cleanup_trust_temps(paths)
                (trust_dir / ".key").unlink(missing_ok=True)
                try:
                    trust_dir.rmdir()
                    trust_dir.parent.rmdir()
                except OSError:
                    pass
            raise


def create_task_plan_review(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """为当前正式整套任务创建唯一审核请求，审核角色不接触任务写入。"""

    clean_id = str(requirement_id or "").strip().upper()
    if not re.fullmatch(r"REQ-[0-9]{3,}", clean_id):
        raise SdlcError(f"REQ 编号格式不正确：{clean_id}。", exit_code=1)
    review_contract.current_thread_id(action="创建整套任务审核请求")
    created_snapshot: Path | None = None
    quality_dir_created = False
    with project_lock(paths):
        trust_dir_existed = _review_trust_dir(paths).exists()
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)
        registry = _load_registry_if_present(paths)

        from codex_sdlc.core.state import derive_state, resolve_requirement

        state = derive_state(paths)
        requirement = resolve_requirement(state, clean_id)
        if not isinstance(requirement.get("task_plan_contract"), Mapping):
            raise SdlcError(
                f"{clean_id} 还没有正式 task-plan.v2，不能创建整套任务审核。",
                exit_code=1,
            )
        requirement_root, _relative_root = _task_plan_requirement_root(
            paths,
            requirement,
        )
        quality_dir = requirement_root / "质检"
        quality_dir_created = not quality_dir.exists()
        _cleanup_task_plan_review_inputs_locked(
            paths,
            requirement_root,
            registry,
        )
        review_input = _validate_task_plan_review_input(
            _build_task_plan_review_input(paths, requirement)
        )
        try:
            snapshot_path, snapshot_created = _write_task_plan_review_input(
                paths,
                requirement_root,
                review_input,
            )
        except Exception:
            if quality_dir_created and quality_dir.is_dir() and not any(
                quality_dir.iterdir()
            ):
                quality_dir.rmdir()
            raise
        if snapshot_created:
            created_snapshot = paths.root / snapshot_path
        try:
            review_id = _next_review_id(registry)
            request = review_contract.build_review_request(
                paths,
                review_id=review_id,
                stage="task_plan",
                owner_id=clean_id,
                input_paths=[
                    snapshot_path,
                    *review_input["bound_input_paths"],
                ],
                required_checks=TASK_PLAN_REVIEW_CHECKS,
                created_at=created_at,
            )
            for path in review_input["bound_input_paths"]:
                if (
                    request["input_hashes"].get(path)
                    != review_input["input_hashes"].get(path)
                ):
                    raise SdlcError(
                        f"任务审核输入在创建请求期间发生变化：{path}。",
                        exit_code=1,
                    )

            graph = dependency_graph.load_dependency_graph(paths)
            if registry is not None:
                latest = _latest_by_target(registry)
                current_id = latest.get(("task_plan", clean_id))
                if current_id is not None:
                    current_effective = _effective_record(
                        paths,
                        registry,
                        graph,
                        current_id,
                        latest_by_target=latest,
                    )
                    same_fingerprint = (
                        registry["requests"][current_id]["input_fingerprint"]
                        == review_contract.review_input_fingerprint(request)
                    )
                    if (
                        same_fingerprint
                        and current_effective["effective_status"] == "pending"
                    ):
                        return {
                            "action": "idempotent",
                            "request": deepcopy(
                                registry["requests"][current_id]["request"]
                            ),
                            "registration_id": None,
                            "effective_status": "pending",
                        }
                    if (
                        same_fingerprint
                        and current_effective["effective_status"] == "passed"
                    ):
                        return {
                            "action": "idempotent",
                            "request": deepcopy(
                                registry["requests"][current_id]["request"]
                            ),
                            "registration_id": current_effective[
                                "registration_id"
                            ],
                            "effective_status": "passed",
                        }
                    if (
                        same_fingerprint
                        and current_effective["effective_status"] == "needs_fix"
                    ):
                        raise SdlcError(
                            "整套任务审核返回 needs_fix 后，必须先改变受控输入"
                            "再创建新审核轮次。",
                            exit_code=1,
                        )
            return _register_review_request_locked(paths, request)
        except Exception:
            if created_snapshot is not None:
                created_snapshot.unlink(missing_ok=True)
            if quality_dir_created and quality_dir.is_dir() and not any(
                quality_dir.iterdir()
            ):
                quality_dir.rmdir()
            trust_dir = _review_trust_dir(paths)
            if (
                not trust_dir_existed
                and trust_dir.exists()
                and not (trust_dir / "registry.json").exists()
            ):
                _cleanup_trust_temps(paths)
                (trust_dir / ".key").unlink(missing_ok=True)
                try:
                    trust_dir.rmdir()
                    trust_dir.parent.rmdir()
                except OSError:
                    pass
            raise


def requirement_review_status(
    paths: ProjectPaths,
    *,
    draft_id: str,
    review_id: str | None = None,
) -> dict[str, Any]:
    """只显示目标 DRAFT 的需求审核，不受其它审核对象状态干扰。"""

    clean_id = str(draft_id or "").strip().upper()
    return _target_review_status(
        paths,
        stage="requirement_split",
        owner_id=clean_id,
        review_id=review_id,
        label="需求审核",
    )


def integrated_design_review_status(
    paths: ProjectPaths,
    *,
    draft_id: str,
    review_id: str | None = None,
) -> dict[str, Any]:
    """只返回指定 DRAFT 的固定整体设计审核状态。"""

    clean_id = str(draft_id or "").strip().upper()
    return _target_review_status(
        paths,
        stage="integrated_design",
        owner_id=clean_id,
        review_id=review_id,
        label="整体设计审核",
    )


def task_plan_review_status(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    review_id: str | None = None,
    tasks: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """只返回指定正式需求的整套任务固定审核状态。"""

    clean_id = str(requirement_id or "").strip().upper()
    return _target_review_status(
        paths,
        stage="task_plan",
        owner_id=clean_id,
        review_id=review_id,
        label="整套任务审核",
        task_plan_tasks=tasks,
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"审核结果文件包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"审核结果文件包含非标准数字：{value}。", exit_code=1)


def load_review_submission(path: Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    if target.is_symlink() or not target.is_file():
        raise SdlcError("审核结果文件不存在或不是普通文件。", exit_code=1)
    try:
        value = json.loads(
            target.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("审核结果文件无法读取或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(value, dict):
        raise SdlcError("审核结果文件顶层必须是对象。", exit_code=1)
    return value


def submit_review(
    paths: ProjectPaths,
    *,
    request_id: str,
    submission: dict[str, Any] | None = None,
    submission_file: Path | None = None,
) -> dict[str, Any]:
    if (submission is None) == (submission_file is None):
        raise SdlcError("审核结果必须在 submission 和 submission_file 中选择一种来源。", exit_code=1)
    document = deepcopy(submission) if submission is not None else load_review_submission(Path(submission_file))
    with project_lock(paths):
        _cleanup_trust_temps(paths)
        dependency_graph.recover_dependency_graph_storage_locked(paths)
        registry = fact_review_trust.load_review_registry(paths)
        clean_request_id = str(request_id).strip().upper()
        request_record = registry["requests"].get(clean_request_id)
        if not isinstance(request_record, dict):
            raise SdlcError("审核请求不存在。", exit_code=1)
        request = review_contract.validate_review_request(
            paths,
            request_record["request"],
            verify_files=False,
        )
        # 先按通用合同捕获真实身份并核对结果结构，再检查整体设计输入是否漂移。
        # 这个顺序既保证自审明确失败，也保证 stale 请求不会写入可信登记。
        review_contract.capture_review_result(request, document)
        if request["stage"] == "integrated_design":
            stale = _integrated_design_business_staleness(paths, request)
            if stale["stale"]:
                raise SdlcError(
                    "整体设计审核请求已经失效，不能提交审核结果。",
                    exit_code=1,
                )
        if request["stage"] == "task_plan":
            stale = _task_plan_business_staleness(paths, request)
            if stale["stale"]:
                raise SdlcError(
                    "整套任务审核请求已经失效，不能提交审核结果。",
                    exit_code=1,
                )
        registration = fact_review_trust.submit_trusted_review_result_locked(
            paths,
            request_id=request_id,
            submission=document,
        )
    result = registration["result"]
    return {
        "action": "registered",
        "registration_id": registration["registration_id"],
        "review_id": result["review_id"],
        "effective_status": result["status"],
        "issues": deepcopy(result["issues"]),
        "notes": deepcopy(result["notes"]),
        "reviewer_run_id": result["reviewer_run_id"],
    }


def _status_rejected(reason: str) -> dict[str, Any]:
    return {
        "status": "rejected",
        "can_advance": False,
        "rejection_reason": reason,
        "reviews": [],
    }


def _integrated_design_snapshot_evidence(
    paths: ProjectPaths,
    draft_id: str,
) -> bool:
    """已登记快照会在审核失败或登记损坏时保留，用它证明 DRAFT 已进入过审核。"""

    quality_dir = paths.root / _draft_relative_path(draft_id, "质检")
    if (
        not quality_dir.exists()
        or quality_dir.is_symlink()
        or not quality_dir.is_dir()
    ):
        return False
    return any(
        candidate.is_file()
        and not candidate.is_symlink()
        and _INTEGRATED_DESIGN_INPUT_PATTERN.fullmatch(
            candidate.relative_to(paths.root).as_posix()
        )
        for candidate in quality_dir.iterdir()
    )


def _target_review_status(
    paths: ProjectPaths,
    *,
    stage: str,
    owner_id: str,
    review_id: str | None,
    label: str,
    task_plan_tasks: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """按固定对象读取审核，避免计算无关阶段时形成状态重建循环。"""

    snapshot_evidence = bool(
        stage == "integrated_design"
        and _integrated_design_snapshot_evidence(paths, owner_id)
    )
    trust_dir = _review_trust_dir(paths)
    if not (trust_dir / ".key").exists() and not (
        trust_dir / "registry.json"
    ).exists():
        result = {
            "status": "empty",
            "can_advance": False,
            "rejection_reason": "",
            "reviews": [],
        }
        if stage == "integrated_design":
            result["has_review_request"] = snapshot_evidence
            if snapshot_evidence:
                result["status"] = "rejected"
                result["rejection_reason"] = "整体设计审核快照缺少可信登记。"
        return result
    try:
        registry = fact_review_trust.load_review_registry(paths)
        graph = dependency_graph.load_dependency_graph(paths)
    except SdlcError as exc:
        result = _status_rejected(exc.message)
        if stage == "integrated_design":
            result["has_review_request"] = snapshot_evidence
        return result
    selected_id = str(review_id or "").strip().upper()
    selected_record = (
        registry["requests"].get(selected_id)
        if selected_id
        else None
    )
    if selected_id and not isinstance(selected_record, dict):
        result = _status_rejected(f"审核请求不存在：{selected_id}。")
        if stage == "integrated_design":
            result["has_review_request"] = snapshot_evidence
        return result
    matching_ids = [
        current_id
        for current_id, record in registry["requests"].items()
        if (
            not selected_id or current_id == selected_id
        )
        and isinstance(record, Mapping)
        and record.get("request", {}).get("stage") == stage
        and record.get("request", {}).get("owner_id") == owner_id
    ]
    if selected_id and not matching_ids:
        result = _status_rejected(
            f"审核请求不属于 {owner_id} 的{label}：{selected_id}。"
        )
        if stage == "integrated_design":
            result["has_review_request"] = snapshot_evidence
        return result
    latest_by_target = _latest_by_target(registry)
    reviews = [
        _effective_record(
            paths,
            registry,
            graph,
            current_id,
            latest_by_target=latest_by_target,
            task_plan_tasks=task_plan_tasks,
        )
        for current_id in matching_ids
    ]
    current = [item for item in reviews if item["is_current"]]
    result = {
        "status": "ready" if reviews else "empty",
        "can_advance": bool(current)
        and all(item["can_advance"] for item in current),
        "rejection_reason": "",
        "reviews": reviews,
    }
    if stage == "integrated_design":
        result["has_review_request"] = bool(matching_ids) or snapshot_evidence
    return result


def _task_plan_runtime_tasks_for_status(
    paths: ProjectPaths,
    request: Mapping[str, Any],
) -> list[Mapping[str, Any]] | None:
    """只从正式事件补任务状态，避免通用审核状态反向调用完整状态重建。"""

    snapshot_path = _task_plan_input_path_from_request(request)
    if snapshot_path is None:
        return None
    try:
        recorded = _validate_task_plan_review_input(
            _read_controlled_json(
                paths,
                snapshot_path,
                label="任务审核输入快照",
            )
        )
        from codex_sdlc.core.state import load_events

        events = load_events(paths)
    except (SdlcError, OSError, ValueError):
        # 无法核对正式事件时保持严格校验，不能擅自忽略规划证据变化。
        return None
    tasks = [deepcopy(task) for task in recorded["tasks"]]
    tasks_by_id = {
        str(task.get("task_id") or ""): task
        for task in tasks
    }
    for task in tasks:
        task["status"] = "todo"
    owner_id = str(request.get("owner_id") or "")
    for event in events:
        if (
            event.get("event_type") != "task_updated"
            or event.get("requirement_id") != owner_id
        ):
            continue
        task = tasks_by_id.get(str(event.get("task_id") or ""))
        payload = event.get("payload")
        if task is None or not isinstance(payload, Mapping):
            continue
        if payload.get("status") is not None:
            task["status"] = str(payload["status"])
    return tasks


def review_status(paths: ProjectPaths, *, review_id: str | None = None) -> dict[str, Any]:
    """只读计算审核状态，不创建密钥、登记、事件或临时文件。"""

    trust_dir = _review_trust_dir(paths)
    if not (trust_dir / ".key").exists() and not (trust_dir / "registry.json").exists():
        return {"status": "empty", "can_advance": False, "rejection_reason": "", "reviews": []}
    try:
        registry = fact_review_trust.load_review_registry(paths)
        graph = dependency_graph.load_dependency_graph(paths)
    except SdlcError as exc:
        return _status_rejected(exc.message)
    if not registry["requests"]:
        return {"status": "empty", "can_advance": False, "rejection_reason": "", "reviews": []}

    selected_id = str(review_id or "").strip().upper()
    if selected_id and selected_id not in registry["requests"]:
        return _status_rejected(f"审核请求不存在：{selected_id}。")

    latest_by_target = _latest_by_target(registry)

    reviews: list[dict[str, Any]] = []
    for current_id, request_record in registry["requests"].items():
        if selected_id and current_id != selected_id:
            continue
        request = request_record["request"]
        reviews.append(
            _effective_record(
                paths,
                registry,
                graph,
                current_id,
                latest_by_target=latest_by_target,
                task_plan_tasks=(
                    _task_plan_runtime_tasks_for_status(paths, request)
                    if request.get("stage") == "task_plan"
                    else None
                ),
            )
        )
    current_reviews = [item for item in reviews if item["is_current"]]
    can_advance = bool(current_reviews) and all(item["can_advance"] for item in current_reviews)
    return {
        "status": "ready",
        "can_advance": can_advance,
        "rejection_reason": "",
        "reviews": reviews,
    }


__all__ = [
    "INTEGRATED_DESIGN_REVIEW_CHECKS",
    "create_integrated_design_review",
    "create_review",
    "create_requirement_review",
    "create_task_plan_review",
    "integrated_design_review_status",
    "load_review_submission",
    "recover_review_storage",
    "requirement_review_status",
    "REQUIREMENT_REVIEW_CHECKS",
    "review_status",
    "submit_review",
    "TASK_PLAN_REVIEW_CHECKS",
    "task_plan_review_status",
]
