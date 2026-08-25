from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
from typing import Any

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    FORMAL_ID_PATTERN,
    TEMPORARY_REFERENCE_PATTERN,
    build_allocation_order,
    rewrite_temporary_references,
)
from codex_sdlc.core.reference_locator import validate_reference
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    contract_sha256,
    validate_schema_document,
)


REQUIREMENT_SPLIT_SCHEMA = "requirement-split.v1"
REQUIREMENT_COVERAGE_SCHEMA = "requirement-coverage.v1"
REVIEW_BLOCKING_COVERAGE_STATUSES = frozenset({"needs_user", "needs_material"})


@dataclass(frozen=True)
class _EntityRecord:
    """保存包内对象的临时身份和正式编号类型，不从正文猜测对象种类。"""

    client_key: str
    id_prefix: str
    payload: Mapping[str, object]
    parent_client_key: str | None = None

    @property
    def temporary_reference(self) -> str:
        return f"@client:{self.client_key}"


@dataclass(frozen=True)
class RequirementContractValidation:
    """阶段二可直接消费的只读校验结果，不在阶段一写文件或分配编号。"""

    split_sha256: str
    allocation_objects: tuple[AllocationObject, ...]
    review_blockers: tuple[str, ...]


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SdlcError(f"{label}必须是 JSON 对象。")
    if any(not isinstance(key, str) for key in value):
        raise SdlcError(f"{label}的字段名必须是字符串。")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SdlcError(f"{label}必须是数组。")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"需求合同 JSON 包含重复字段：{key}。")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"需求合同 JSON 包含非标准数字：{value}。")


def read_requirement_document(path: Path, *, schema_name: str) -> dict[str, object]:
    """严格读取一份需求合同；重复 JSON 字段不能在解析时被后值覆盖。"""

    if schema_name not in {REQUIREMENT_SPLIT_SCHEMA, REQUIREMENT_COVERAGE_SCHEMA}:
        raise SdlcError(f"不支持的需求合同版本：{schema_name}。")
    source = Path(path)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"需求合同无法读取或不是有效 JSON：{source.name}。") from exc
    validate_schema_document(document, schema_name=schema_name)
    return dict(_require_mapping(document, "需求合同"))


def _collect_entities(
    split_document: Mapping[str, object],
    coverage_document: Mapping[str, object],
) -> dict[str, _EntityRecord]:
    records: dict[str, _EntityRecord] = {}

    def add(
        payload: object,
        *,
        id_prefix: str,
        label: str,
        parent_client_key: str | None = None,
    ) -> _EntityRecord:
        item = _require_mapping(payload, label)
        client_key = str(item.get("client_key") or "")
        if client_key in records:
            existing = records[client_key]
            if id_prefix == "AC" or existing.id_prefix == "AC":
                raise SdlcError(f"AC 只能属于一个明确的 FR，client_key 重复：{client_key}。")
            raise SdlcError(f"需求合同 client_key 重复：{client_key}。")
        record = _EntityRecord(
            client_key=client_key,
            id_prefix=id_prefix,
            payload=item,
            parent_client_key=parent_client_key,
        )
        records[client_key] = record
        return record

    for index, item in enumerate(_require_list(split_document.get("global_rules"), "global_rules")):
        add(item, id_prefix="GR", label=f"global_rules[{index}]")

    for fr_index, item in enumerate(
        _require_list(split_document.get("functional_requirements"), "functional_requirements")
    ):
        fr = add(item, id_prefix="FR", label=f"functional_requirements[{fr_index}]")
        acceptance = _require_list(fr.payload.get("acceptance_criteria"), "acceptance_criteria")
        for ac_index, criterion in enumerate(acceptance):
            add(
                criterion,
                id_prefix="AC",
                label=f"functional_requirements[{fr_index}].acceptance_criteria[{ac_index}]",
                parent_client_key=fr.client_key,
            )

    for index, item in enumerate(_require_list(coverage_document.get("units"), "units")):
        add(item, id_prefix="SRC", label=f"units[{index}]")
    return records


def build_requirement_allocation_objects(
    split_document: object,
    coverage_document: object,
) -> tuple[AllocationObject, ...]:
    """把 FR、GR、AC、SRC 交给 T-002 的统一编号对象，不复制编号规则。"""

    validate_schema_document(split_document, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    validate_schema_document(coverage_document, schema_name=REQUIREMENT_COVERAGE_SCHEMA)
    split = _require_mapping(split_document, "需求拆分合同")
    coverage = _require_mapping(coverage_document, "需求覆盖合同")
    records = _collect_entities(split, coverage)
    objects = tuple(
        AllocationObject(client_key=record.client_key, id_prefix=record.id_prefix, depends_on=())
        for record in records.values()
    )
    # 公共入口再次固定 client_key 格式、唯一性、前缀白名单和确定性顺序。
    return build_allocation_order(objects)


def rewrite_requirement_documents(
    split_document: object,
    coverage_document: object,
    mapping: Mapping[str, str],
) -> tuple[object, object]:
    """只使用 T-002 的完整字段值重写规则，不替换正文中的相似文字。"""

    return (
        rewrite_temporary_references(split_document, mapping),
        rewrite_temporary_references(coverage_document, mapping),
    )


def _formal_ids(values: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or FORMAL_ID_PATTERN.fullmatch(value) is None:
            raise SdlcError(f"已存在正式编号格式不正确：{value}。")
        result.add(value)
    return result


def _resolve_entity_reference(
    reference: object,
    *,
    records: Mapping[str, _EntityRecord],
    known_formal_ids: set[str],
    allowed_prefixes: set[str],
    label: str,
) -> _EntityRecord | None:
    if not isinstance(reference, str):
        raise SdlcError(f"{label}必须是字符串引用。")
    temporary = TEMPORARY_REFERENCE_PATTERN.fullmatch(reference)
    if temporary is not None:
        client_key = temporary.group("client_key")
        record = records.get(client_key)
        if record is None:
            raise SdlcError(f"{label}包含跨包或悬空临时引用：{reference}。")
        if record.id_prefix not in allowed_prefixes:
            expected = "/".join(sorted(allowed_prefixes))
            raise SdlcError(f"{label}必须引用 {expected}，实际引用了 {record.id_prefix}。")
        return record

    formal = FORMAL_ID_PATTERN.fullmatch(reference)
    if formal is None or formal.group("prefix") not in allowed_prefixes:
        expected = "/".join(sorted(allowed_prefixes))
        raise SdlcError(f"{label}必须引用 {expected} 或完整的 @client: 临时引用。")
    if reference not in known_formal_ids:
        raise SdlcError(f"{label}引用的正式编号不存在：{reference}。")
    return None


def _walk_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)


def _validate_source_reference(
    source_ref: object,
    *,
    label: str,
    project_root: Path,
    input_material_hashes: Mapping[str, object],
) -> None:
    item = _require_mapping(source_ref, label)
    material_id = str(item.get("material_id") or "")
    expected_hash = input_material_hashes.get(material_id)
    if not isinstance(expected_hash, str):
        raise SdlcError(f"{label}引用的资料不在 input_material_hashes 中：{material_id}。")
    reference = _require_mapping(item.get("reference"), f"{label}.reference")
    # 来源定位合同不是临时编号容器，路径、标题或定位字段里都不能夹带待重写引用。
    if any("@client:" in value for value in _walk_strings(reference)):
        raise SdlcError(f"{label}.reference 不能包含 @client: 临时引用。")
    actual_reference_hash = reference.get("sha256")
    if not isinstance(actual_reference_hash, str) or not hmac.compare_digest(
        expected_hash, actual_reference_hash
    ):
        raise SdlcError(f"{label}的资料哈希与 input_material_hashes 不一致：{material_id}。")
    # 具体定位类型、字段组合、路径边界、文件哈希和片段漂移全部复用 T-001 公共合同。
    validate_reference(project_root, reference)


def _validate_source_refs(
    records: Mapping[str, _EntityRecord],
    *,
    project_root: Path,
    input_material_hashes: Mapping[str, object],
) -> None:
    for record in records.values():
        if record.id_prefix == "SRC":
            source_refs = [record.payload.get("source_ref")]
        else:
            source_refs = _require_list(record.payload.get("source_refs"), "source_refs")
        seen: set[str] = set()
        for index, source_ref in enumerate(source_refs):
            digest = canonical_sha256(source_ref)
            if digest in seen:
                raise SdlcError(f"{record.client_key} 的 source_refs 包含重复定位。")
            seen.add(digest)
            _validate_source_reference(
                source_ref,
                label=f"{record.client_key}.source_refs[{index}]",
                project_root=project_root,
                input_material_hashes=input_material_hashes,
            )


def _validate_material_hashes(
    declared: Mapping[str, object], current_material_hashes: Mapping[str, str]
) -> None:
    current = dict(current_material_hashes)
    if set(declared) != set(current):
        missing = sorted(set(current) - set(declared))
        unknown = sorted(set(declared) - set(current))
        details = []
        if missing:
            details.append(f"缺少 {', '.join(missing)}")
        if unknown:
            details.append(f"包含不存在的 {', '.join(unknown)}")
        raise SdlcError(f"input_material_hashes 与当前资料清单不一致：{'；'.join(details)}。")
    for material_id, digest in declared.items():
        current_digest = current.get(material_id)
        if not isinstance(digest, str) or not isinstance(current_digest, str) or not hmac.compare_digest(
            digest, current_digest
        ):
            raise SdlcError(f"输入资料内容已经变化：{material_id} 的 SHA-256 不一致。")


_RELATION_COUNTERPARTS = {
    "replaces": "replaced_by",
    "replaced_by": "replaces",
    "split_from": "split_into",
    "split_into": "split_from",
    "merged_from": "merged_into",
    "merged_into": "merged_from",
    "supersedes": "superseded_by",
    "superseded_by": "supersedes",
}


def _validate_relations(
    records: Mapping[str, _EntityRecord], known_formal_ids: set[str]
) -> None:
    relation_index: set[tuple[str, str, str]] = set()
    for record in records.values():
        relations = _require_list(record.payload.get("relations"), f"{record.client_key}.relations")
        for relation in relations:
            item = _require_mapping(relation, f"{record.client_key}.relations")
            kind = str(item.get("kind") or "")
            target_ref = item.get("target_ref")
            target = _resolve_entity_reference(
                target_ref,
                records=records,
                known_formal_ids=known_formal_ids,
                allowed_prefixes={record.id_prefix},
                label=f"{record.client_key}.{kind}",
            )
            if target is not None and target.client_key == record.client_key:
                raise SdlcError(f"{record.client_key} 的 {kind} 不能引用自身。")
            relation_index.add((record.client_key, kind, str(target_ref)))

    # 包内两端都可见时必须写成完整双向关系；指向历史正式编号时由阶段二结合旧投影核对。
    for source_key, kind, target_ref in relation_index:
        temporary = TEMPORARY_REFERENCE_PATTERN.fullmatch(target_ref)
        if temporary is None:
            continue
        target_key = temporary.group("client_key")
        counterpart = _RELATION_COUNTERPARTS[kind]
        source_ref = f"@client:{source_key}"
        if (target_key, counterpart, source_ref) not in relation_index:
            raise SdlcError(
                f"{source_key} 与 {target_key} 的 {kind} 关系缺少反向 {counterpart}。"
            )


def _validate_split_cross_references(
    split: Mapping[str, object],
    records: Mapping[str, _EntityRecord],
    known_formal_ids: set[str],
) -> None:
    applies_pairs: set[tuple[str, str]] = set()
    reverse_pairs: set[tuple[str, str]] = set()
    dependency_graph: dict[str, set[str]] = {}

    for record in records.values():
        if record.id_prefix == "GR":
            for reference in _require_list(record.payload.get("applies_to"), "applies_to"):
                target = _resolve_entity_reference(
                    reference,
                    records=records,
                    known_formal_ids=known_formal_ids,
                    allowed_prefixes={"FR"},
                    label=f"{record.client_key}.applies_to",
                )
                if target is not None:
                    applies_pairs.add((record.client_key, target.client_key))
            continue
        if record.id_prefix != "FR":
            continue

        fr_ref = record.temporary_reference
        material_refs = set(_require_list(record.payload.get("material_refs"), "material_refs"))
        source_materials: set[object] = {
            _require_mapping(item, "source_ref").get("material_id")
            for item in _require_list(record.payload.get("source_refs"), "source_refs")
        }
        for criterion in _require_list(record.payload.get("acceptance_criteria"), "acceptance_criteria"):
            ac = _require_mapping(criterion, "acceptance_criterion")
            if ac.get("owner_fr_ref") != fr_ref:
                raise SdlcError(
                    f"AC {ac.get('client_key')} 错属 FR：owner_fr_ref 必须是 {fr_ref}。"
                )
            source_materials.update(
                _require_mapping(item, "source_ref").get("material_id")
                for item in _require_list(ac.get("source_refs"), "source_refs")
            )
        if material_refs != source_materials:
            raise SdlcError(
                f"{record.client_key}.material_refs 必须与该 FR 及其 AC 的 source_refs 资料集合一致。"
            )

        for reference in _require_list(record.payload.get("global_rule_refs"), "global_rule_refs"):
            target = _resolve_entity_reference(
                reference,
                records=records,
                known_formal_ids=known_formal_ids,
                allowed_prefixes={"GR"},
                label=f"{record.client_key}.global_rule_refs",
            )
            if target is not None:
                reverse_pairs.add((target.client_key, record.client_key))

        local_dependencies: set[str] = set()
        for reference in _require_list(record.payload.get("depends_on"), "depends_on"):
            target = _resolve_entity_reference(
                reference,
                records=records,
                known_formal_ids=known_formal_ids,
                allowed_prefixes={"FR"},
                label=f"{record.client_key}.depends_on",
            )
            if target is not None:
                if target.client_key == record.client_key:
                    raise SdlcError(f"{record.client_key}.depends_on 不能引用自身。")
                local_dependencies.add(target.client_key)
        dependency_graph[record.client_key] = local_dependencies

    if applies_pairs != reverse_pairs:
        raise SdlcError("GR.applies_to 与 FR.global_rule_refs 的包内双向关系不一致。")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(client_key: str) -> None:
        if client_key in visiting:
            raise SdlcError(f"FR.depends_on 存在依赖环：{client_key}。")
        if client_key in visited:
            return
        visiting.add(client_key)
        for dependency in dependency_graph.get(client_key, set()):
            visit(dependency)
        visiting.remove(client_key)
        visited.add(client_key)

    for client_key in sorted(dependency_graph):
        visit(client_key)

    # 用公共重写器检查所有 @client: 是否完整占用字段值以及是否属于当前包。
    placeholder_mapping = {
        record.client_key: f"{record.id_prefix}-000"
        for record in records.values()
    }
    rewrite_temporary_references(split, placeholder_mapping)


def _validate_coverage_cross_references(
    coverage: Mapping[str, object],
    records: Mapping[str, _EntityRecord],
    known_formal_ids: set[str],
) -> None:
    covered_local_keys: set[str] = set()
    for record in records.values():
        if record.id_prefix != "SRC":
            continue
        status = str(record.payload.get("status") or "")
        for reference in _require_list(record.payload.get("covered_by"), "covered_by"):
            target = _resolve_entity_reference(
                reference,
                records=records,
                known_formal_ids=known_formal_ids,
                allowed_prefixes={"FR", "GR", "AC"},
                label=f"{record.client_key}.covered_by",
            )
            if target is not None:
                covered_local_keys.add(target.client_key)
        for decision_ref in _require_list(record.payload.get("decision_refs"), "decision_refs"):
            if not isinstance(decision_ref, str) or decision_ref not in known_formal_ids:
                raise SdlcError(
                    f"{record.client_key}.decision_refs 引用的用户决定不存在：{decision_ref}。"
                )
        if status == "covered" and not record.payload.get("covered_by"):
            raise SdlcError(f"{record.client_key} 标记 covered 时必须提供 covered_by。")

    required_local_keys = {
        record.client_key
        for record in records.values()
        if record.id_prefix in {"FR", "GR", "AC"}
    }
    missing = sorted(required_local_keys - covered_local_keys)
    if missing:
        raise SdlcError(f"FR、GR、AC 必须被覆盖矩阵显式反向引用，缺少：{', '.join(missing)}。")

    placeholder_mapping = {
        record.client_key: f"{record.id_prefix}-000"
        for record in records.values()
    }
    rewrite_temporary_references(coverage, placeholder_mapping)


def _review_blockers(
    split: Mapping[str, object], coverage: Mapping[str, object]
) -> tuple[str, ...]:
    blockers = [
        f"open_questions[{index}]"
        for index, _ in enumerate(_require_list(split.get("open_questions"), "open_questions"))
    ]
    for item in _require_list(coverage.get("units"), "units"):
        unit = _require_mapping(item, "coverage unit")
        status = str(unit.get("status") or "")
        if status in REVIEW_BLOCKING_COVERAGE_STATUSES:
            blockers.append(f"{unit.get('client_key')}:{status}")
    return tuple(blockers)


def validate_requirement_contract(
    split_document: object,
    coverage_document: object,
    *,
    project_root: Path,
    current_material_hashes: Mapping[str, str],
    expected_draft_id: str | None = None,
    expected_producer_run_id: str | None = None,
    known_formal_ids: Iterable[str] = (),
) -> RequirementContractValidation:
    """完整校验双文件合同，只检查明确结构、哈希、定位和引用，不读写项目状态。"""

    validate_schema_document(split_document, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    validate_schema_document(coverage_document, schema_name=REQUIREMENT_COVERAGE_SCHEMA)
    split = _require_mapping(split_document, "需求拆分合同")
    coverage = _require_mapping(coverage_document, "需求覆盖合同")

    if split.get("draft_id") != coverage.get("draft_id"):
        raise SdlcError("需求拆分与覆盖合同的 draft_id 不一致。")
    if expected_draft_id is not None and split.get("draft_id") != expected_draft_id:
        raise SdlcError(f"需求合同不属于目标 DRAFT：应为 {expected_draft_id}。")
    if (
        expected_producer_run_id is not None
        and split.get("producer_run_id") != expected_producer_run_id
    ):
        raise SdlcError("producer_run_id 与当前模型任务标识不一致。")

    split_sha256 = contract_sha256(split, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    declared_split_hash = coverage.get("requirement_split_sha256")
    if not isinstance(declared_split_hash, str) or not hmac.compare_digest(
        split_sha256, declared_split_hash
    ):
        raise SdlcError("覆盖合同引用的 requirement_split_sha256 与需求拆分文件不一致。")

    input_material_hashes = _require_mapping(
        split.get("input_material_hashes"), "input_material_hashes"
    )
    _validate_material_hashes(input_material_hashes, current_material_hashes)
    records = _collect_entities(split, coverage)
    allocation_objects = build_requirement_allocation_objects(split, coverage)
    formal_ids = _formal_ids(known_formal_ids)
    _validate_split_cross_references(split, records, formal_ids)
    _validate_coverage_cross_references(coverage, records, formal_ids)
    _validate_relations(records, formal_ids)
    _validate_source_refs(
        records,
        project_root=Path(project_root),
        input_material_hashes=input_material_hashes,
    )

    return RequirementContractValidation(
        split_sha256=split_sha256,
        allocation_objects=allocation_objects,
        review_blockers=_review_blockers(split, coverage),
    )


def ensure_requirement_review_ready(validation: RequirementContractValidation) -> None:
    """只按显式问题和覆盖状态设置审核门禁，不分析任何正文语义。"""

    if not isinstance(validation, RequirementContractValidation):
        raise SdlcError("需求审核门禁必须使用完整合同校验结果。")
    if validation.review_blockers:
        raise SdlcError(
            f"需求合同还有未解决项，不能进入需求审核：{', '.join(validation.review_blockers)}。"
        )


def read_and_validate_requirement_contract(
    split_path: Path,
    coverage_path: Path,
    **validation_options: Any,
) -> RequirementContractValidation:
    """严格读取并校验双文件，供阶段二在写事件前一次调用。"""

    split = read_requirement_document(split_path, schema_name=REQUIREMENT_SPLIT_SCHEMA)
    coverage = read_requirement_document(
        coverage_path, schema_name=REQUIREMENT_COVERAGE_SCHEMA
    )
    return validate_requirement_contract(split, coverage, **validation_options)


__all__ = [
    "REQUIREMENT_COVERAGE_SCHEMA",
    "REQUIREMENT_SPLIT_SCHEMA",
    "REVIEW_BLOCKING_COVERAGE_STATUSES",
    "RequirementContractValidation",
    "build_requirement_allocation_objects",
    "ensure_requirement_review_ready",
    "read_and_validate_requirement_contract",
    "read_requirement_document",
    "rewrite_requirement_documents",
    "validate_requirement_contract",
]
