from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from codex_sdlc.core.design_artifact_contract import (
    design_artifact_markdown,
    design_artifact_plan_status,
    design_artifact_records,
    validate_design_artifact_against_plan,
    validate_design_artifact_record,
    validate_design_artifact_relations,
)
from codex_sdlc.core.design_plan_contract import (
    assess_design_plan,
    design_plan_records,
    validate_design_plan_record,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    validate_schema_document,
)


DESIGN_SUMMARY_SCHEMA = "design-summary.v1"
DESIGN_SUMMARY_EVENT = "draft_design_summary_imported"
DESIGN_SUMMARY_JSON_PATH = "设计/design-summary.v1.json"
DESIGN_SUMMARY_MARKDOWN_PATH = "设计/总体设计说明.md"
FORMAL_FIELDS = {
    "summary_id",
    "producer_run_id",
    "input_hashes",
    "revision",
    "previous_summary_sha256",
    "submission_sha256",
    "invalidated_modules",
    "invalidated_review_targets",
    "summary_sha256",
}
_DRAFT_ID_PATTERN = re.compile(r"^DRAFT-([0-9]{3,})$")


def _sorted_strings(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SdlcError(f"总体设计说明的 {field} 必须是数组。", exit_code=1)
    return sorted(str(item) for item in value)


def _normalize_common_objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise SdlcError("总体设计说明的 common_objects 必须是数组。", exit_code=1)
    normalized: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise SdlcError("总体设计公共对象必须是 JSON 对象。", exit_code=1)
        item = deepcopy(dict(raw))
        item["source_refs"] = _sorted_strings(
            item.get("source_refs"), field="source_refs"
        )
        item["applies_to_modules"] = _sorted_strings(
            item.get("applies_to_modules"), field="applies_to_modules"
        )
        normalized.append(item)
    normalized.sort(key=lambda item: str(item.get("business_id") or ""))
    return normalized


def _validate_unique_common_objects(
    common_objects: Iterable[Mapping[str, object]],
) -> None:
    objects = list(common_objects)
    identifiers = [str(item["business_id"]) for item in objects]
    repeated_ids = sorted(
        item for item in set(identifiers) if identifiers.count(item) > 1
    )
    if repeated_ids:
        raise SdlcError(
            f"总体设计说明包含重复公共对象编号：{', '.join(repeated_ids)}。",
            exit_code=1,
        )
    identities = [
        (
            str(item["object_type"]),
            tuple(str(ref) for ref in item["source_refs"]),  # type: ignore[index]
        )
        for item in objects
    ]
    repeated_objects = sorted(
        f"{object_type}:{','.join(source_refs)}"
        for object_type, source_refs in set(identities)
        if identities.count((object_type, source_refs)) > 1
    )
    if repeated_objects:
        raise SdlcError(
            f"同一公共对象不能重复定义：{'；'.join(repeated_objects)}。",
            exit_code=1,
        )


def normalize_design_summary_submission(
    value: Mapping[str, object],
) -> dict[str, object]:
    document = deepcopy(dict(value))
    reported = sorted(FORMAL_FIELDS.intersection(document))
    if reported:
        raise SdlcError(
            f"design-summary 导入文件不能自报正式哈希、版本、编号或失效结果：{', '.join(reported)}。",
            exit_code=1,
        )
    document["common_objects"] = _normalize_common_objects(
        document.get("common_objects")
    )
    document["affected_modules"] = _sorted_strings(
        document.get("affected_modules"), field="affected_modules"
    )
    document["open_questions"] = _sorted_strings(
        document.get("open_questions"), field="open_questions"
    )
    validate_schema_document(document, schema_name=DESIGN_SUMMARY_SCHEMA)
    _validate_unique_common_objects(document["common_objects"])  # type: ignore[arg-type]
    if document["open_questions"]:
        raise SdlcError(
            "总体设计说明仍有待确认问题，不能登记为完整产物。",
            exit_code=1,
        )
    return document


def _summary_hash_source(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in record.items()
        if key != "summary_sha256"
    }


def validate_design_summary_record(
    value: Mapping[str, object],
) -> dict[str, object]:
    record = deepcopy(dict(value))
    validate_schema_document(record, schema_name=DESIGN_SUMMARY_SCHEMA)
    normalized_objects = _normalize_common_objects(
        record.get("common_objects")
    )
    if record.get("common_objects") != normalized_objects:
        raise SdlcError(
            "总体设计公共对象或其引用没有按稳定编号顺序保存。",
            exit_code=1,
        )
    record["common_objects"] = normalized_objects
    for field in (
        "affected_modules",
        "open_questions",
        "invalidated_modules",
    ):
        normalized_values = _sorted_strings(record.get(field), field=field)
        if record.get(field) != normalized_values:
            raise SdlcError(
                f"总体设计说明的 {field} 没有按稳定编号顺序保存。",
                exit_code=1,
            )
        record[field] = normalized_values
    _validate_unique_common_objects(record["common_objects"])  # type: ignore[arg-type]
    draft_match = _DRAFT_ID_PATTERN.fullmatch(str(record["draft_id"]))
    if draft_match is None or record["summary_id"] != f"DSUM-{draft_match.group(1)}":
        raise SdlcError(
            "总体设计说明编号必须由 DRAFT 编号稳定生成。",
            exit_code=1,
        )
    revision = int(record["revision"])
    previous = record["previous_summary_sha256"]
    if revision == 1 and previous is not None:
        raise SdlcError(
            "总体设计说明首个版本不能引用上一版本。",
            exit_code=1,
        )
    if revision > 1 and previous is None:
        raise SdlcError(
            "总体设计说明新修订缺少上一版本哈希。",
            exit_code=1,
        )
    expected_invalidated = record["affected_modules"] if revision > 1 else []
    if record["invalidated_modules"] != expected_invalidated:
        raise SdlcError(
            "总体设计说明的模块失效结果与显式 affected_modules 不一致。",
            exit_code=1,
        )
    expected_review_targets: list[dict[str, str]] = []
    if revision > 1:
        expected_review_targets = [
            {
                "stage": "integrated_design",
                "owner_id": str(record["draft_id"]),
                "status": "stale",
            }
        ]
    if record["invalidated_review_targets"] != expected_review_targets:
        raise SdlcError(
            "总体设计说明的审核失效目标与修订状态不一致。",
            exit_code=1,
        )
    expected_hash = canonical_sha256(_summary_hash_source(record))
    if record["summary_sha256"] != expected_hash:
        raise SdlcError(
            "总体设计说明记录哈希与事件内容不一致。",
            exit_code=1,
        )
    return record


def design_summary_history(
    paths,
    *,
    draft_id: str | None = None,
    events: Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    if events is None:
        from codex_sdlc.core.state import load_events

        source_events = load_events(paths)
    else:
        source_events = list(events)
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in source_events:
        if event.get("event_type") != DESIGN_SUMMARY_EVENT:
            continue
        payload = event.get("payload")
        summary = payload.get("summary") if isinstance(payload, Mapping) else None
        if not isinstance(summary, Mapping):
            raise SdlcError("总体设计事件缺少结构化说明。", exit_code=1)
        record = validate_design_summary_record(summary)
        if draft_id is not None and record["draft_id"] != draft_id:
            continue
        grouped.setdefault(str(record["draft_id"]), []).append(record)

    ordered: list[dict[str, object]] = []
    for owner in sorted(grouped):
        revisions = sorted(grouped[owner], key=lambda item: int(item["revision"]))
        seen: dict[int, str] = {}
        previous_hash: str | None = None
        for expected_revision, record in enumerate(revisions, start=1):
            revision = int(record["revision"])
            digest = str(record["summary_sha256"])
            if revision in seen:
                if seen[revision] != digest:
                    raise SdlcError(
                        f"{owner} 的总体设计版本 {revision} 包含冲突事件。",
                        exit_code=1,
                    )
                continue
            if revision != expected_revision:
                raise SdlcError(
                    f"{owner} 的总体设计版本序号不连续。",
                    exit_code=1,
                )
            if record["previous_summary_sha256"] != previous_hash:
                raise SdlcError(
                    f"{owner} 的总体设计版本链与事件记录不一致。",
                    exit_code=1,
                )
            seen[revision] = digest
            previous_hash = digest
            ordered.append(record)
    return ordered


def design_summary_records(
    paths,
    *,
    draft_id: str | None = None,
    events: Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    history = design_summary_history(paths, draft_id=draft_id, events=events)
    latest: dict[str, dict[str, object]] = {}
    for record in history:
        latest[str(record["draft_id"])] = record
    return [latest[key] for key in sorted(latest)]


def _design_context(
    paths,
    draft: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    source_events = list(events)
    draft_id = str(draft.get("draft_id") or "")
    plans = design_plan_records(paths, draft_id=draft_id, events=source_events)
    if len(plans) != 1:
        raise SdlcError(
            f"{draft_id} 缺少唯一有效的开发设计总计划。",
            exit_code=1,
        )
    plan = validate_design_plan_record(plans[0])
    assessment = assess_design_plan(paths, plan)
    if assessment["status"] != "current":
        changed = "、".join(assessment["changed_paths"])  # type: ignore[arg-type]
        raise SdlcError(
            f"开发设计总计划的代码证据已变化：{changed}。",
            exit_code=1,
        )
    completion = design_artifact_plan_status(
        paths,
        draft_id,
        events=source_events,
    )
    if completion["status"] != "complete" or completion["blocked"]:
        missing = "、".join(completion["pending"]) or "无"  # type: ignore[arg-type]
        blocked = "、".join(completion["blocked"]) or "无"  # type: ignore[arg-type]
        raise SdlcError(
            f"{draft_id} 缺少当前有效且完整的模块产物；待导入：{missing}；阻塞：{blocked}。",
            exit_code=1,
        )
    records = design_artifact_records(
        paths,
        draft_id=draft_id,
        events=source_events,
    )
    for record in records:
        validate_design_artifact_against_plan(paths, draft, plan, record)
        if record["open_questions"]:
            raise SdlcError(
                f"模块 {record['artifact_id']} 仍有待确认问题，不能生成总体设计说明。",
                exit_code=1,
            )
    validate_design_artifact_relations(records)
    return plan, records


def expected_design_summary_input_hashes(
    plan: Mapping[str, object],
    records: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    return {
        "design-plan": str(plan["plan_sha256"]),
        **{
            f"module:{record['artifact_id']}": str(record["artifact_sha256"])
            for record in sorted(records, key=lambda item: str(item["artifact_id"]))
        },
    }


def _reference_lookup(
    records: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    lookup: dict[str, dict[str, object]] = {}
    for raw_record in records:
        record = validate_design_artifact_record(raw_record)
        module_id = str(record["artifact_id"])
        lookup[module_id] = {
            "kind": "module",
            "module_id": module_id,
            "value": record,
        }
        content = record["content"]
        if record["type"] == "data":
            for entity in content["entities"]:  # type: ignore[index]
                lookup[f"{module_id}#{entity['entity_id']}"] = {
                    "kind": "entity",
                    "module_id": module_id,
                    "value": entity,
                }
                for field in entity["fields"]:
                    lookup[f"{module_id}#{field['field_id']}"] = {
                        "kind": "data_field",
                        "module_id": module_id,
                        "value": field,
                    }
        elif record["type"] == "api":
            for endpoint in content["endpoints"]:  # type: ignore[index]
                lookup[f"{module_id}#{endpoint['endpoint_id']}"] = {
                    "kind": "api_endpoint",
                    "module_id": module_id,
                    "value": endpoint,
                }
                for group in ("request_fields", "response_fields"):
                    for field in endpoint[group]:
                        lookup[f"{module_id}#{field['field_id']}"] = {
                            "kind": "api_field",
                            "module_id": module_id,
                            "value": field,
                        }
                for error in endpoint["errors"]:
                    lookup[f"{module_id}#{error['error_id']}"] = {
                        "kind": "api_error",
                        "module_id": module_id,
                        "value": error,
                    }
        elif record["type"] == "page":
            for page in content["pages"]:  # type: ignore[index]
                lookup[f"{module_id}#{page['page_id']}"] = {
                    "kind": "page",
                    "module_id": module_id,
                    "value": page,
                }
                for state_name, state_text in page["states"].items():
                    lookup[
                        f"{module_id}#{page['page_id']}#STATE-{state_name}"
                    ] = {
                        "kind": "page_state",
                        "module_id": module_id,
                        "value": {
                            "state": state_name,
                            "description": state_text,
                        },
                    }
                for element in page["elements"]:
                    lookup[f"{module_id}#{element['element_id']}"] = {
                        "kind": "page_element",
                        "module_id": module_id,
                        "value": element,
                    }
        elif record["type"] == "component":
            for component in content["components"]:  # type: ignore[index]
                lookup[f"{module_id}#{component['component_id']}"] = {
                    "kind": "component",
                    "module_id": module_id,
                    "value": component,
                }
        elif record["type"] == "security":
            for control in content["controls"]:  # type: ignore[index]
                lookup[f"{module_id}#{control['control_id']}"] = {
                    "kind": "security_control",
                    "module_id": module_id,
                    "value": control,
                }
    return lookup


def _require_kinds(
    business_id: str,
    references: list[dict[str, object]],
    *,
    allowed: set[str],
    label: str,
) -> None:
    invalid = sorted(
        str(item["kind"]) for item in references if item["kind"] not in allowed
    )
    if invalid:
        raise SdlcError(
            f"公共对象 {business_id} 的{label}引用类型不正确：{', '.join(invalid)}。",
            exit_code=1,
        )


def _validate_common_object_references(
    common_object: Mapping[str, object],
    lookup: Mapping[str, Mapping[str, object]],
    records_by_id: Mapping[str, Mapping[str, object]],
) -> None:
    business_id = str(common_object["business_id"])
    source_refs = [str(item) for item in common_object["source_refs"]]  # type: ignore[index]
    references: list[dict[str, object]] = []
    for source_ref in source_refs:
        located = lookup.get(source_ref)
        if located is None:
            raise SdlcError(
                f"公共对象 {business_id} 引用了不存在的模块对象：{source_ref}。",
                exit_code=1,
            )
        references.append(deepcopy(dict(located)))
    applies_to = set(
        str(item) for item in common_object["applies_to_modules"]  # type: ignore[index]
    )
    missing_modules = sorted(applies_to - set(records_by_id))
    if missing_modules:
        raise SdlcError(
            f"公共对象 {business_id} 的适用模块不存在：{', '.join(missing_modules)}。",
            exit_code=1,
        )
    source_modules = {str(item["module_id"]) for item in references}
    if not source_modules.issubset(applies_to):
        missing = sorted(source_modules - applies_to)
        raise SdlcError(
            f"公共对象 {business_id} 没有在 applies_to_modules 中登记来源模块：{', '.join(missing)}。",
            exit_code=1,
        )

    object_type = str(common_object["object_type"])
    if object_type == "entity":
        _require_kinds(
            business_id, references, allowed={"entity"}, label="实体"
        )
        if len(references) != 1:
            raise SdlcError(
                f"公共实体 {business_id} 必须唯一定位到一个真实实体。",
                exit_code=1,
            )
    elif object_type == "data_field":
        _require_kinds(
            business_id, references, allowed={"data_field"}, label="数据字段"
        )
        if len(references) != 1:
            raise SdlcError(
                f"公共数据字段 {business_id} 必须唯一定位到一个真实字段。",
                exit_code=1,
            )
    elif object_type == "api_field":
        _require_kinds(
            business_id,
            references,
            allowed={"api_field", "data_field"},
            label="接口字段",
        )
        api_fields = [item for item in references if item["kind"] == "api_field"]
        data_fields = [item for item in references if item["kind"] == "data_field"]
        if len(api_fields) != 1 or len(data_fields) != 1:
            raise SdlcError(
                f"公共接口字段 {business_id} 必须同时定位一个接口字段和一个数据字段。",
                exit_code=1,
            )
        api_value = api_fields[0]["value"]
        data_value = data_fields[0]["value"]
        assert isinstance(api_value, Mapping) and isinstance(data_value, Mapping)
        data_ref = next(
            ref
            for ref, item in zip(source_refs, references)
            if item["kind"] == "data_field"
        )
        if (
            api_value.get("data_field_ref") != data_ref
            or api_value.get("type") != data_value.get("type")
        ):
            raise SdlcError(
                f"公共接口字段 {business_id} 与真实数据字段引用或类型不一致。",
                exit_code=1,
            )
    elif object_type == "page_source":
        _require_kinds(
            business_id,
            references,
            allowed={"page_element", "data_field", "api_endpoint"},
            label="页面来源",
        )
        elements = [item for item in references if item["kind"] == "page_element"]
        if len(elements) != 1:
            raise SdlcError(
                f"公共页面来源 {business_id} 必须唯一定位到一个页面元素。",
                exit_code=1,
            )
        element = elements[0]["value"]
        assert isinstance(element, Mapping)
        expected_sources = sorted(str(item) for item in element["data_source_refs"])
        actual_sources = sorted(
            ref
            for ref, item in zip(source_refs, references)
            if item["kind"] != "page_element"
        )
        if actual_sources != expected_sources:
            raise SdlcError(
                f"公共页面来源 {business_id} 与真实页面数据来源不一致。",
                exit_code=1,
            )
    elif object_type == "public_type":
        _require_kinds(
            business_id,
            references,
            allowed={"data_field", "api_field"},
            label="公共类型",
        )
        types = {
            str(item["value"]["type"])  # type: ignore[index]
            for item in references
        }
        if len(references) < 2 or len(types) != 1:
            raise SdlcError(
                f"公共类型 {business_id} 引用的字段类型不一致。",
                exit_code=1,
            )
    elif object_type == "state":
        _require_kinds(
            business_id, references, allowed={"page_state"}, label="状态"
        )
    elif object_type == "error_code":
        _require_kinds(
            business_id, references, allowed={"api_error"}, label="错误码"
        )
    elif object_type == "authentication":
        _require_kinds(
            business_id,
            references,
            allowed={"api_endpoint", "security_control"},
            label="鉴权",
        )
        kinds = {str(item["kind"]) for item in references}
        if not {"api_endpoint", "security_control"}.issubset(kinds):
            raise SdlcError(
                f"公共鉴权 {business_id} 必须同时定位接口和安全控制。",
                exit_code=1,
            )
    elif object_type == "route":
        _require_kinds(
            business_id, references, allowed={"page"}, label="路由"
        )
    elif object_type == "component":
        _require_kinds(
            business_id, references, allowed={"component"}, label="组件"
        )
    elif object_type == "module_dependency":
        _require_kinds(
            business_id, references, allowed={"module"}, label="模块依赖"
        )
        if len(references) != 2:
            raise SdlcError(
                f"公共模块依赖 {business_id} 必须明确两个模块编号。",
                exit_code=1,
            )
        relations = [
            (owner, dependency)
            for owner in source_refs
            for dependency in source_refs
            if owner != dependency
            and dependency
            in {
                str(item)
                for item in records_by_id[owner]["depends_on"]  # type: ignore[index]
            }
        ]
        if len(relations) != 1:
            raise SdlcError(
                f"公共模块依赖 {business_id} 与真实 depends_on 关系不一致。",
                exit_code=1,
            )


def validate_design_summary_against_modules(
    paths,
    draft: Mapping[str, object],
    record: Mapping[str, object],
    *,
    events: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    checked = validate_design_summary_record(record)
    source_events = list(events)
    plan, records = _design_context(paths, draft, source_events)
    expected_hashes = expected_design_summary_input_hashes(plan, records)
    if checked["input_hashes"] != expected_hashes:
        raise SdlcError(
            "总体设计说明引用的模块哈希已经变化。",
            exit_code=1,
        )
    records_by_id = {str(item["artifact_id"]): item for item in records}
    lookup = _reference_lookup(records)
    for common_object in checked["common_objects"]:  # type: ignore[index]
        _validate_common_object_references(
            common_object,
            lookup,
            records_by_id,
        )
    return checked, records


def validate_current_design_artifact_files(
    paths,
    records: Iterable[Mapping[str, object]],
) -> None:
    for raw_record in records:
        record = validate_design_artifact_record(raw_record)
        output_path = str(record["output_path"])
        expected_documents = {
            output_path: canonical_json_text(record).encode("utf-8"),
            str(Path(output_path).with_suffix(".md")).replace(
                "\\", "/"
            ): design_artifact_markdown(record).encode("utf-8"),
        }
        for relative_path, expected in expected_documents.items():
            target = paths.draft_dir(str(record["draft_id"])) / relative_path
            if target.is_symlink() or not target.is_file():
                raise SdlcError(
                    f"模块 {record['artifact_id']} 的受管投影不存在：{relative_path}。",
                    exit_code=1,
                )
            try:
                actual = target.read_bytes()
            except OSError as exc:
                raise SdlcError(
                    f"模块 {record['artifact_id']} 的受管投影读取失败：{relative_path}。",
                    exit_code=1,
                ) from exc
            if actual != expected:
                raise SdlcError(
                    f"模块 {record['artifact_id']} 的投影哈希已经变化：{relative_path}。",
                    exit_code=1,
                )


def expected_affected_modules(
    previous: Mapping[str, object] | None,
    common_objects: Iterable[Mapping[str, object]],
    *,
    current_input_hashes: Mapping[str, str] | None = None,
) -> list[str]:
    current = {
        str(item["business_id"]): deepcopy(dict(item))
        for item in common_objects
    }
    if previous is None:
        return sorted(
            {
                str(module_id)
                for item in current.values()
                for module_id in item["applies_to_modules"]  # type: ignore[index]
            }
        )
    old = {
        str(item["business_id"]): deepcopy(dict(item))
        for item in previous["common_objects"]  # type: ignore[index]
    }
    affected: set[str] = set()
    if current_input_hashes is not None:
        previous_hashes = previous.get("input_hashes")
        if not isinstance(previous_hashes, Mapping):
            raise SdlcError(
                "当前总体设计版本缺少结构化输入哈希。",
                exit_code=1,
            )
        for key, current_hash in current_input_hashes.items():
            if not key.startswith("module:"):
                continue
            if previous_hashes.get(key) != current_hash:
                affected.add(key.split(":", 1)[1])
    for business_id in sorted(set(old) | set(current)):
        old_item = old.get(business_id)
        current_item = current.get(business_id)
        if old_item is not None and current_item is not None:
            old_identity = (
                old_item["object_type"],
                old_item["source_refs"],
            )
            current_identity = (
                current_item["object_type"],
                current_item["source_refs"],
            )
            if old_identity != current_identity:
                raise SdlcError(
                    f"公共对象 {business_id} 的类型或稳定来源引用不能原地改写。",
                    exit_code=1,
                )
        if old_item == current_item:
            continue
        for item in (old_item, current_item):
            if item is not None:
                affected.update(
                    str(module_id)
                    for module_id in item["applies_to_modules"]  # type: ignore[index]
                )
    if not affected:
        raise SdlcError(
            "总体设计说明内容没有变化，请直接复用当前版本。",
            exit_code=1,
        )
    return sorted(affected)


def design_summary_markdown(record: Mapping[str, object]) -> str:
    summary = validate_design_summary_record(record)
    labels = {
        "entity": "实体",
        "data_field": "数据字段",
        "api_field": "接口字段",
        "page_source": "页面来源",
        "public_type": "公共类型",
        "state": "状态",
        "error_code": "错误码",
        "authentication": "鉴权",
        "route": "路由",
        "component": "组件",
        "module_dependency": "模块依赖",
    }
    lines = [
        "# 总体设计说明",
        "",
        f"- 编号：{summary['summary_id']}",
        f"- 版本序号：{summary['revision']}",
        f"- 受影响模块：{'、'.join(summary['affected_modules'])}",
        "",
        "## 跨模块公共对象",
        "",
    ]
    for item in summary["common_objects"]:  # type: ignore[index]
        definition = item["definition"]
        lines.extend(
            [
                f"### {item['business_id']} · {definition['canonical_name']}",
                "",
                f"- 类型：{labels[str(item['object_type'])]}",
                f"- 稳定引用：{'、'.join(item['source_refs'])}",
                f"- 适用模块：{'、'.join(item['applies_to_modules'])}",
                f"- 共同规则：{definition['contract']}",
                "",
            ]
        )
    lines.extend(
        [
            "## 完整结构",
            "",
            "```json",
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def design_summary_projection_documents(
    paths,
    draft: Mapping[str, object],
    *,
    events: Iterable[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, bytes]]:
    source_events = list(events)
    records = design_summary_records(
        paths,
        draft_id=str(draft.get("draft_id") or ""),
        events=source_events,
    )
    if len(records) != 1:
        raise SdlcError(
            f"{draft.get('draft_id', '')} 缺少唯一有效的总体设计说明。",
            exit_code=1,
        )
    # 投影只消费已经验真的事件记录。模块后来产生合法新修订时，当前说明可以保持
    # 历史版本并由审核关系判为 stale；若这里重新绑定最新模块，就会阻断模块修订。
    record = validate_design_summary_record(records[0])
    return record, {
        DESIGN_SUMMARY_JSON_PATH: canonical_json_text(record).encode("utf-8"),
        DESIGN_SUMMARY_MARKDOWN_PATH: design_summary_markdown(record).encode(
            "utf-8"
        ),
    }


__all__ = [
    "DESIGN_SUMMARY_EVENT",
    "DESIGN_SUMMARY_JSON_PATH",
    "DESIGN_SUMMARY_MARKDOWN_PATH",
    "DESIGN_SUMMARY_SCHEMA",
    "FORMAL_FIELDS",
    "design_summary_history",
    "design_summary_markdown",
    "design_summary_projection_documents",
    "design_summary_records",
    "expected_affected_modules",
    "expected_design_summary_input_hashes",
    "normalize_design_summary_submission",
    "validate_current_design_artifact_files",
    "validate_design_summary_against_modules",
    "validate_design_summary_record",
]
