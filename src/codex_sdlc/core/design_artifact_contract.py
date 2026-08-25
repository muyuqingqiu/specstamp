from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from codex_sdlc.core.design_plan_contract import (
    MODULE_PREFIXES,
    assess_design_plan,
    design_plan_records,
    validate_design_plan_record,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import project_lock
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)


DESIGN_ARTIFACT_SCHEMA = "design-artifact.v1"
DESIGN_ARTIFACT_EVENT = "draft_design_artifact_imported"
ENABLED_PLAN_STATUSES = {
    "required",
    "provided",
    "supplement_required",
    "completed",
}
FORMAL_FIELDS = {
    "producer_run_id",
    "input_hashes",
    "code_evidence_paths",
    "plan_status",
    "output_path",
    "revision",
    "previous_artifact_sha256",
    "plan_sha256",
    "plan_module_sha256",
    "submission_sha256",
    "artifact_sha256",
}
ARTIFACT_ID_PATTERN = re.compile(
    r"^(DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}$"
)
GR_ID_PATTERN = re.compile(r"^GR-[0-9]{3,}$")
CONTENT_FIELDS = {
    "data": {"entities", "lifecycle", "migration_steps", "rollback_steps"},
    "api": {"endpoints"},
    "page": {"pages"},
    "component": {"components"},
    "security": {"controls"},
    "deployment": {
        "environments",
        "rollout_steps",
        "migration_steps",
        "rollback_steps",
        "health_checks",
    },
    "field": {"scenarios"},
    "special": {"reason", "design_items"},
}
PAGE_STATES = {"initial", "loading", "empty", "ready", "error", "forbidden"}


def _sorted_strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SdlcError(f"模块化设计产物的 {field} 必须是数组。", exit_code=1)
    return sorted(str(item) for item in value)


def _unique_identifiers(values: Iterable[object], *, label: str) -> None:
    normalized = [str(item or "") for item in values]
    repeated = sorted(
        item for item in set(normalized) if normalized.count(item) > 1
    )
    if repeated:
        raise SdlcError(
            f"{label} 包含重复稳定编号：{', '.join(repeated)}。",
            exit_code=1,
        )


def _validate_content_identifiers(
    module_type: str,
    content: Mapping[str, object],
) -> None:
    """Schema 负责字段形状，这里补充 JSON Schema 不擅长表达的跨数组唯一性。"""

    if module_type == "data":
        entities = content["entities"]
        _unique_identifiers(
            (item["entity_id"] for item in entities),  # type: ignore[index]
            label="数据实体",
        )
        fields = [
            field
            for entity in entities  # type: ignore[union-attr]
            for field in entity["fields"]
        ]
        _unique_identifiers(
            (item["field_id"] for item in fields),
            label="数据字段",
        )
        indexes = [
            index
            for entity in entities  # type: ignore[union-attr]
            for index in entity["indexes"]
        ]
        _unique_identifiers(
            (item["index_id"] for item in indexes),
            label="数据索引",
        )
        relations = [
            relation
            for entity in entities  # type: ignore[union-attr]
            for relation in entity["relations"]
        ]
        _unique_identifiers(
            (item["relation_id"] for item in relations),
            label="数据关系",
        )
        for entity in entities:  # type: ignore[union-attr]
            local_field_ids = {
                str(item["field_id"]) for item in entity["fields"]
            }
            for group in entity["unique_constraints"]:
                missing = sorted(
                    set(str(item) for item in group) - local_field_ids
                )
                if missing:
                    raise SdlcError(
                        f"数据唯一约束引用了不存在的字段：{', '.join(missing)}。",
                        exit_code=1,
                    )
            for index in entity["indexes"]:
                missing = sorted(
                    set(str(item) for item in index["field_ids"])
                    - local_field_ids
                )
                if missing:
                    raise SdlcError(
                        f"数据索引引用了不存在的字段：{', '.join(missing)}。",
                        exit_code=1,
                    )
            for relation in entity["relations"]:
                if str(relation["from_field_id"]) not in local_field_ids:
                    raise SdlcError(
                        f"数据关系引用了不存在的起始字段：{relation['from_field_id']}。",
                        exit_code=1,
                    )
        return

    if module_type == "api":
        endpoints = content["endpoints"]
        _unique_identifiers(
            (item["endpoint_id"] for item in endpoints),  # type: ignore[index]
            label="接口",
        )
        fields = [
            field
            for endpoint in endpoints  # type: ignore[union-attr]
            for group in ("request_fields", "response_fields")
            for field in endpoint[group]
        ]
        _unique_identifiers(
            (item["field_id"] for item in fields),
            label="接口字段",
        )
        errors = [
            error
            for endpoint in endpoints  # type: ignore[union-attr]
            for error in endpoint["errors"]
        ]
        _unique_identifiers(
            (item["error_id"] for item in errors),
            label="接口错误",
        )
        return

    key_by_type = {
        "page": ("pages", "page_id", "页面"),
        "component": ("components", "component_id", "组件"),
        "security": ("controls", "control_id", "安全控制"),
        "deployment": ("environments", "environment_id", "部署环境"),
        "field": ("scenarios", "scenario_id", "现场场景"),
        "special": ("design_items", "spec_id", "专项设计"),
    }
    collection_key, identifier_key, label = key_by_type[module_type]
    collection = content[collection_key]
    _unique_identifiers(
        (item[identifier_key] for item in collection),  # type: ignore[index]
        label=label,
    )
    if module_type == "page":
        elements = [
            element
            for page in collection  # type: ignore[union-attr]
            for element in page["elements"]
        ]
        _unique_identifiers(
            (item["element_id"] for item in elements),
            label="页面元素",
        )


def _preflight_submission_content(document: Mapping[str, object]) -> None:
    """先给出明确业务错误，避免 oneOf 把具体字段问题压成另一个分支的泛化提示。"""

    module_type = str(document.get("type") or "")
    content = document.get("content")
    if module_type not in CONTENT_FIELDS or not isinstance(content, Mapping):
        return
    unexpected = sorted(set(content) - CONTENT_FIELDS[module_type])
    if unexpected:
        raise SdlcError(
            f"{module_type} 模块 content 包含未知字段：{', '.join(unexpected)}。",
            exit_code=1,
        )
    artifact_id = str(document.get("artifact_id") or "模块")
    if module_type in {"data", "deployment"} and not content.get("rollback_steps"):
        raise SdlcError(
            f"模块 {artifact_id} 的 rollback_steps 不能为空。",
            exit_code=1,
        )
    if module_type == "special":
        for item in content.get("design_items", []):  # type: ignore[union-attr]
            if isinstance(item, Mapping) and not item.get("rollback_steps"):
                raise SdlcError(
                    f"模块 {artifact_id} 的专项设计 rollback_steps 不能为空。",
                    exit_code=1,
                )
    if module_type == "page":
        for page in content.get("pages", []):  # type: ignore[union-attr]
            states = page.get("states") if isinstance(page, Mapping) else None
            if not isinstance(states, Mapping):
                continue
            missing = sorted(PAGE_STATES - set(states))
            extra = sorted(set(states) - PAGE_STATES)
            if missing or extra:
                details = []
                if missing:
                    details.append(f"缺少 {', '.join(missing)}")
                if extra:
                    details.append(f"多出 {', '.join(extra)}")
                raise SdlcError(
                    f"模块 {artifact_id} 的页面状态必须完整包含六种固定状态：{'；'.join(details)}。",
                    exit_code=1,
                )


def normalize_design_artifact_submission(
    value: Mapping[str, object],
) -> dict[str, object]:
    document = deepcopy(dict(value))
    reported = sorted(FORMAL_FIELDS.intersection(document))
    if reported:
        raise SdlcError(
            f"design-artifact 导入文件不能自报正式哈希、版本或运行字段：{', '.join(reported)}。",
            exit_code=1,
        )
    _preflight_submission_content(document)
    validate_schema_document(document, schema_name=DESIGN_ARTIFACT_SCHEMA)
    for field in (
        "requirement_refs",
        "global_rule_refs",
        "material_refs",
        "depends_on",
        "open_questions",
    ):
        document[field] = _sorted_strings(document[field], field)
    module_type = str(document["type"])
    _validate_content_identifiers(module_type, document["content"])  # type: ignore[arg-type]
    artifact_id = str(document["artifact_id"])
    expected_prefix = MODULE_PREFIXES[module_type] + "-"
    if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id) or not artifact_id.startswith(
        expected_prefix
    ):
        raise SdlcError(
            f"模块 {artifact_id} 的类型和编号前缀不匹配。",
            exit_code=1,
        )
    if document["open_questions"]:
        raise SdlcError(
            f"模块 {artifact_id} 仍有待确认问题，不能登记为完整产物。",
            exit_code=1,
        )
    return document


def _artifact_hash_source(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in record.items()
        if key != "artifact_sha256"
    }


def validate_design_artifact_record(
    value: Mapping[str, object],
) -> dict[str, object]:
    record = deepcopy(dict(value))
    validate_schema_document(record, schema_name=DESIGN_ARTIFACT_SCHEMA)
    module_type = str(record["type"])
    artifact_id = str(record["artifact_id"])
    expected_prefix = MODULE_PREFIXES[module_type] + "-"
    if not artifact_id.startswith(expected_prefix):
        raise SdlcError(
            f"模块 {artifact_id} 的类型和编号前缀不匹配。",
            exit_code=1,
        )
    for field in (
        "requirement_refs",
        "global_rule_refs",
        "material_refs",
        "depends_on",
        "code_evidence_paths",
    ):
        values = record[field]
        if list(values) != sorted(str(item) for item in values):  # type: ignore[arg-type]
            raise SdlcError(
                f"模块 {artifact_id} 的 {field} 没有按稳定顺序保存。",
                exit_code=1,
            )
    _validate_content_identifiers(module_type, record["content"])  # type: ignore[arg-type]
    revision = int(record["revision"])
    previous = record["previous_artifact_sha256"]
    if revision == 1 and previous is not None:
        raise SdlcError(
            f"模块 {artifact_id} 的首个版本不能引用上一版本。",
            exit_code=1,
        )
    if revision > 1 and previous is None:
        raise SdlcError(
            f"模块 {artifact_id} 的版本链缺少上一版本哈希。",
            exit_code=1,
        )
    expected = canonical_sha256(_artifact_hash_source(record))
    if record["artifact_sha256"] != expected:
        raise SdlcError(
            f"模块 {artifact_id} 的记录哈希与事件内容不一致。",
            exit_code=1,
        )
    return record


def design_artifact_history(
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
        if event.get("event_type") != DESIGN_ARTIFACT_EVENT:
            continue
        payload = event.get("payload")
        artifact = payload.get("artifact") if isinstance(payload, Mapping) else None
        if not isinstance(artifact, Mapping):
            raise SdlcError("模块化设计事件缺少结构化产物。", exit_code=1)
        record = validate_design_artifact_record(artifact)
        if draft_id is not None and record["draft_id"] != draft_id:
            continue
        grouped.setdefault(str(record["artifact_id"]), []).append(record)

    ordered: list[dict[str, object]] = []
    for artifact_id in sorted(grouped):
        revisions = sorted(grouped[artifact_id], key=lambda item: int(item["revision"]))
        seen_revisions: dict[int, str] = {}
        previous_hash: str | None = None
        for expected_revision, record in enumerate(revisions, start=1):
            revision = int(record["revision"])
            digest = str(record["artifact_sha256"])
            if revision in seen_revisions:
                if seen_revisions[revision] != digest:
                    raise SdlcError(
                        f"模块 {artifact_id} 的版本 {revision} 包含冲突事件。",
                        exit_code=1,
                    )
                continue
            if revision != expected_revision:
                raise SdlcError(
                    f"模块 {artifact_id} 的版本序号不连续。",
                    exit_code=1,
                )
            if record["previous_artifact_sha256"] != previous_hash:
                raise SdlcError(
                    f"模块 {artifact_id} 的版本链与事件记录不一致。",
                    exit_code=1,
                )
            seen_revisions[revision] = digest
            previous_hash = digest
            ordered.append(record)
    return ordered


def design_artifact_records(
    paths,
    *,
    draft_id: str | None = None,
    events: Iterable[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    history = design_artifact_history(paths, draft_id=draft_id, events=events)
    latest: dict[str, dict[str, object]] = {}
    for record in history:
        latest[str(record["artifact_id"])] = record
    return [latest[key] for key in sorted(latest)]


def _plan_for_draft(
    paths,
    draft_id: str,
    events: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    plans = design_plan_records(paths, draft_id=draft_id, events=events)
    if len(plans) != 1:
        raise SdlcError(
            f"{draft_id} 缺少唯一有效的开发设计总计划。",
            exit_code=1,
        )
    return validate_design_plan_record(plans[0])


def plan_module_for_artifact(
    plan: Mapping[str, object],
    artifact_id: str,
) -> dict[str, object]:
    matches = [
        deepcopy(module)
        for module in plan["modules"]  # type: ignore[index]
        if str(module["module_id"]) == artifact_id
    ]
    if len(matches) != 1:
        raise SdlcError(
            f"模块 {artifact_id} 不在当前设计总计划中。",
            exit_code=1,
        )
    return matches[0]


def design_artifact_output_path(module: Mapping[str, object]) -> str:
    artifact_id = str(module["module_id"])
    outputs = [str(item) for item in module["outputs"]]  # type: ignore[index]
    if not outputs and module["status"] == "provided":
        return f"设计/模块/{artifact_id}.design-artifact.v1.json"
    if len(outputs) != 1:
        raise SdlcError(
            f"模块 {artifact_id} 必须在设计总计划中声明唯一 JSON 产物路径。",
            exit_code=1,
        )
    return outputs[0]


def _formal_requirement_entities(
    draft: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    split = draft.get("requirement_split")
    import_record = draft.get("requirement_import")
    mapping = (
        import_record.get("mapping")
        if isinstance(import_record, Mapping)
        else None
    )
    if not isinstance(split, Mapping) or not isinstance(mapping, Mapping):
        raise SdlcError("当前 DRAFT 缺少已经导入的结构化需求。", exit_code=1)

    functional: dict[str, object] = {}
    global_rules: dict[str, object] = {}
    for collection_name, target in (
        ("functional_requirements", functional),
        ("global_rules", global_rules),
    ):
        collection = split.get(collection_name)
        if not isinstance(collection, list):
            raise SdlcError(
                f"当前结构化需求缺少 {collection_name}。",
                exit_code=1,
            )
        for raw in collection:
            if not isinstance(raw, Mapping):
                raise SdlcError("结构化需求实体格式无效。", exit_code=1)
            client_key = str(raw.get("client_key") or "")
            formal_id = str(mapping.get(client_key) or "")
            if formal_id:
                target[formal_id] = deepcopy(dict(raw))
    return functional, global_rules


def _current_confirmation_sha256(draft: Mapping[str, object]) -> str:
    state = draft.get("_requirement_confirmation_state")
    confirmation = (
        state.get("current_confirmation")
        if isinstance(state, Mapping)
        else None
    )
    digest = (
        str(confirmation.get("confirmation_sha256") or "")
        if isinstance(confirmation, Mapping)
        else ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SdlcError("当前需求确认记录缺失或已失效。", exit_code=1)
    return digest


def expected_global_rule_refs(module: Mapping[str, object]) -> list[str]:
    """总计划没有另设 GR 字段，只认其结构化 inputs 中明确列出的正式编号。"""

    return sorted(
        {
            str(item)
            for item in module["inputs"]  # type: ignore[index]
            if GR_ID_PATTERN.fullmatch(str(item))
        }
    )


def _material_by_id(
    draft: Mapping[str, object],
    material_id: str,
) -> dict[str, object]:
    matches = [
        deepcopy(dict(item))
        for item in draft.get("materials", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
        and item.get("material_id") == material_id
        and item.get("status") == "active"
    ]
    if len(matches) != 1:
        raise SdlcError(
            f"模块引用的资料不存在、不是活动版本或编号不唯一：{material_id}。",
            exit_code=1,
        )
    return matches[0]


def _material_input_digest(material: Mapping[str, object]) -> str:
    source_kind = str(material.get("source_kind") or "")
    if source_kind == "file":
        digest = str(material.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SdlcError(
                f"资料 {material.get('material_id', '')} 缺少有效文件哈希。",
                exit_code=1,
            )
        return digest
    if source_kind == "external-reference":
        # 外部资料只绑定公开地址摘要和版本证据，不把页面正文复制进设计产物。
        return canonical_sha256(
            {
                "normalized_url_sha256": material.get("normalized_url_sha256"),
                "access_condition": material.get("access_condition"),
                "version_evidence": material.get("version_evidence"),
                "status": material.get("status"),
            }
        )
    if source_kind == "secret-reference":
        # 秘密正文始终留在外部安全存储，设计产物只绑定不含秘密值的引用元数据。
        return canonical_sha256(
            {
                "secret_reference": material.get("secret_reference"),
                "status": material.get("status"),
            }
        )
    raise SdlcError(
        f"资料 {material.get('material_id', '')} 的来源类型不受支持。",
        exit_code=1,
    )


def _code_evidence_entries(plan: Mapping[str, object]) -> dict[str, dict[str, object]]:
    evidence = plan["code_evidence"]
    result: dict[str, dict[str, object]] = {}
    for group in ("rules", "dependencies", "code_files", "upstream_outputs"):
        for item in evidence[group]:  # type: ignore[index]
            result[str(item["path"])] = deepcopy(dict(item))
    return result


def expected_design_artifact_input_hashes(
    paths,
    draft: Mapping[str, object],
    plan: Mapping[str, object],
    module: Mapping[str, object],
) -> dict[str, str]:
    """所有哈希都从事件和真实文件生成，导入文件没有替换这些值的入口。"""

    from codex_sdlc.core.state import validate_draft_material_files

    validate_draft_material_files(paths, dict(draft))
    functional, global_rules = _formal_requirement_entities(draft)
    input_hashes: dict[str, str] = {
        "design-plan": str(plan["plan_sha256"]),
        "plan-module": canonical_sha256(module),
        "requirement-confirmation": _current_confirmation_sha256(draft),
    }
    for requirement_id in module["requirement_refs"]:  # type: ignore[index]
        requirement = functional.get(str(requirement_id))
        if requirement is None:
            raise SdlcError(
                f"模块引用的需求不存在：{requirement_id}。",
                exit_code=1,
            )
        input_hashes[f"requirement:{requirement_id}"] = canonical_sha256(
            requirement
        )
    for global_rule_id in expected_global_rule_refs(module):
        global_rule = global_rules.get(global_rule_id)
        if global_rule is None:
            raise SdlcError(
                f"模块引用的全局规则不存在：{global_rule_id}。",
                exit_code=1,
            )
        input_hashes[f"global-rule:{global_rule_id}"] = canonical_sha256(
            global_rule
        )
    for material_id in module["material_refs"]:  # type: ignore[index]
        material = _material_by_id(draft, str(material_id))
        input_hashes[f"material:{material_id}"] = _material_input_digest(material)
    references = {
        str(item.get("design_id") or ""): item
        for item in draft.get("design_references", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    }
    for design_id in module["design_refs"]:  # type: ignore[index]
        reference = references.get(str(design_id))
        if reference is None or reference.get("status") != "confirmed":
            raise SdlcError(
                f"模块引用的技术方案不存在或尚未确认：{design_id}。",
                exit_code=1,
            )
        input_hashes[f"design:{design_id}"] = str(reference["record_sha256"])

    evidence = _code_evidence_entries(plan)
    for raw_path in module["code_evidence_paths"]:  # type: ignore[index]
        path = str(raw_path)
        entry = evidence.get(path)
        if entry is None:
            raise SdlcError(
                f"模块引用的代码证据不在当前计划中：{path}。",
                exit_code=1,
            )
        target = paths.root / path
        if target.is_symlink() or not target.is_file():
            raise SdlcError(
                f"模块代码证据文件不存在或不是普通文件：{path}。",
                exit_code=1,
            )
        input_hashes[f"code:{path}"] = sha256_file(target)
        # Git 状态也是 T-010 证据的一部分；记录原值后由计划有效性检查核对当前状态。
        input_hashes[f"code-git:{path}"] = str(entry["git_state_sha256"])
    return dict(sorted(input_hashes.items()))


def validate_design_artifact_against_plan(
    paths,
    draft: Mapping[str, object],
    plan: Mapping[str, object],
    record: Mapping[str, object],
    *,
    require_current_inputs: bool = True,
) -> tuple[dict[str, object], dict[str, object]]:
    checked = validate_design_artifact_record(record)
    artifact_id = str(checked["artifact_id"])
    module = plan_module_for_artifact(plan, artifact_id)
    status = str(module["status"])
    if status == "not_applicable":
        raise SdlcError(
            f"模块 {artifact_id} 在当前计划中不适用，不能生成产物文件。",
            exit_code=1,
        )
    if status == "blocked":
        raise SdlcError(
            f"模块 {artifact_id} 在当前计划中为 blocked，必须先解除阻塞。",
            exit_code=1,
        )
    if status not in ENABLED_PLAN_STATUSES:
        raise SdlcError(
            f"模块 {artifact_id} 的计划状态不能导入完整产物：{status}。",
            exit_code=1,
        )
    exact_pairs = (
        ("draft_id", str(plan["draft_id"])),
        ("type", str(module["type"])),
        ("plan_status", status),
        ("plan_sha256", str(plan["plan_sha256"])),
        ("plan_module_sha256", canonical_sha256(module)),
        ("output_path", design_artifact_output_path(module)),
    )
    for field, expected in exact_pairs:
        if checked[field] != expected:
            raise SdlcError(
                f"模块 {artifact_id} 的 {field} 与当前计划不一致。",
                exit_code=1,
            )
    expected_lists = (
        ("requirement_refs", sorted(str(item) for item in module["requirement_refs"])),
        ("global_rule_refs", expected_global_rule_refs(module)),
        ("material_refs", sorted(str(item) for item in module["material_refs"])),
        ("depends_on", sorted(str(item) for item in module["depends_on"])),
        (
            "code_evidence_paths",
            sorted(str(item) for item in module["code_evidence_paths"]),
        ),
    )
    for field, expected in expected_lists:
        if checked[field] != expected:
            raise SdlcError(
                f"模块 {artifact_id} 的 {field} 与当前计划输入不一致。",
                exit_code=1,
            )
    expected_hashes = expected_design_artifact_input_hashes(
        paths,
        draft,
        plan,
        module,
    )
    if checked["input_hashes"] != expected_hashes:
        raise SdlcError(
            f"模块 {artifact_id} 的输入哈希已经变化，不能继续使用当前产物。",
            exit_code=1,
        )
    if require_current_inputs:
        assessment = assess_design_plan(paths, plan)
        if assessment["status"] != "current":
            changed = "、".join(assessment["changed_paths"])  # type: ignore[arg-type]
            raise SdlcError(
                f"模块 {artifact_id} 的输入哈希已经变化：{changed}。",
                exit_code=1,
            )
    return checked, module


def _data_fields(
    records: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for record in records:
        if record["type"] != "data":
            continue
        artifact_id = str(record["artifact_id"])
        for entity in record["content"]["entities"]:  # type: ignore[index]
            for field in entity["fields"]:
                result[f"{artifact_id}#{field['field_id']}"] = deepcopy(field)
    return result


def _api_endpoints(
    records: Iterable[Mapping[str, object]],
) -> set[str]:
    return {
        f"{record['artifact_id']}#{endpoint['endpoint_id']}"
        for record in records
        if record["type"] == "api"
        for endpoint in record["content"]["endpoints"]  # type: ignore[index]
    }


def _page_items(
    records: Iterable[Mapping[str, object]],
) -> set[str]:
    return {
        f"{record['artifact_id']}#{page['page_id']}"
        for record in records
        if record["type"] == "page"
        for page in record["content"]["pages"]  # type: ignore[index]
    }


def _require_reference_dependency(
    record: Mapping[str, object],
    reference: str,
) -> None:
    owner = reference.split("#", 1)[0]
    if owner != record["artifact_id"] and owner not in record["depends_on"]:
        raise SdlcError(
            f"模块 {record['artifact_id']} 引用了 {reference}，但当前计划没有声明对 {owner} 的依赖。",
            exit_code=1,
        )


def validate_design_artifact_relations(
    records: Iterable[Mapping[str, object]],
) -> None:
    checked = [validate_design_artifact_record(item) for item in records]
    data_fields = _data_fields(checked)
    api_endpoints = _api_endpoints(checked)
    page_items = _page_items(checked)
    known_artifacts = {str(item["artifact_id"]) for item in checked}
    for record in checked:
        artifact_id = str(record["artifact_id"])
        missing_dependencies = sorted(
            set(str(item) for item in record["depends_on"]) - known_artifacts
        )
        if missing_dependencies:
            raise SdlcError(
                f"模块 {artifact_id} 的依赖模块尚未完成：{', '.join(missing_dependencies)}。",
                exit_code=1,
            )
        if record["type"] == "data":
            for entity in record["content"]["entities"]:  # type: ignore[index]
                for relation in entity["relations"]:
                    reference = str(relation["to_field_ref"])
                    if reference not in data_fields:
                        raise SdlcError(
                            f"模块 {artifact_id} 引用了不存在的数据字段：{reference}。",
                            exit_code=1,
                        )
                    _require_reference_dependency(record, reference)
        elif record["type"] == "api":
            for endpoint in record["content"]["endpoints"]:  # type: ignore[index]
                for group in ("request_fields", "response_fields"):
                    for field in endpoint[group]:
                        reference = field["data_field_ref"]
                        if reference is None:
                            continue
                        reference = str(reference)
                        data_field = data_fields.get(reference)
                        if data_field is None:
                            raise SdlcError(
                                f"模块 {artifact_id} 引用了不存在的数据字段：{reference}。",
                                exit_code=1,
                            )
                        if str(field["type"]) != str(data_field["type"]):
                            raise SdlcError(
                                f"模块 {artifact_id} 的接口字段与 {reference} 数据字段类型不一致。",
                                exit_code=1,
                            )
                        _require_reference_dependency(record, reference)
        elif record["type"] == "page":
            for page in record["content"]["pages"]:  # type: ignore[index]
                for reference in page["navigation_refs"]:
                    reference = str(reference)
                    if reference not in page_items:
                        raise SdlcError(
                            f"模块 {artifact_id} 引用了不存在的页面：{reference}。",
                            exit_code=1,
                        )
                    _require_reference_dependency(record, reference)
                invalid_materials = sorted(
                    set(str(item) for item in page["ui_material_refs"])
                    - set(str(item) for item in record["material_refs"])
                )
                if invalid_materials:
                    raise SdlcError(
                        f"模块 {artifact_id} 的页面引用了外壳未登记的资料：{', '.join(invalid_materials)}。",
                        exit_code=1,
                    )
                for element in page["elements"]:
                    for reference in element["data_source_refs"]:
                        reference = str(reference)
                        if (
                            reference not in data_fields
                            and reference not in api_endpoints
                        ):
                            raise SdlcError(
                                f"模块 {artifact_id} 引用了不存在的页面数据来源：{reference}。",
                                exit_code=1,
                            )
                        _require_reference_dependency(record, reference)
        elif record["type"] in {"component", "special"}:
            collection = (
                record["content"]["components"]  # type: ignore[index]
                if record["type"] == "component"
                else record["content"]["design_items"]  # type: ignore[index]
            )
            declared = set(str(item) for item in record["depends_on"])
            for item in collection:
                content_dependencies = set(
                    str(dependency) for dependency in item["dependencies"]
                )
                missing = sorted(content_dependencies - known_artifacts)
                if missing:
                    raise SdlcError(
                        f"模块 {artifact_id} 的内容引用了尚未完成的模块：{', '.join(missing)}。",
                        exit_code=1,
                    )
                undeclared = sorted(content_dependencies - declared)
                if undeclared:
                    raise SdlcError(
                        f"模块 {artifact_id} 的内容依赖没有写入计划 depends_on：{', '.join(undeclared)}。",
                        exit_code=1,
                    )


def design_artifact_markdown(record: Mapping[str, object]) -> str:
    artifact = validate_design_artifact_record(record)
    full_document = json.dumps(
        artifact,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    lines = [
        f"# {artifact['artifact_id']} 模块化设计",
        "",
        f"- 类型：{artifact['type']}",
        f"- 计划状态：{artifact['plan_status']}",
        f"- 版本序号：{artifact['revision']}",
        f"- 适用需求：{'、'.join(artifact['requirement_refs'])}",
        f"- 全局规则：{'、'.join(artifact['global_rule_refs']) or '无'}",
        f"- 资料：{'、'.join(artifact['material_refs']) or '无'}",
        f"- 依赖模块：{'、'.join(artifact['depends_on']) or '无'}",
        "",
        "## 完整结构",
        "",
        "```json",
        full_document,
        "```",
        "",
    ]
    return "\n".join(lines)


def _markdown_output_path(output_path: str) -> str:
    return str(Path(output_path).with_suffix(".md")).replace("\\", "/")


def design_artifact_projection_documents(
    paths,
    draft: Mapping[str, object],
    *,
    events: Iterable[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, bytes]]:
    source_events = list(events)
    draft_id = str(draft.get("draft_id") or "")
    plan = _plan_for_draft(paths, draft_id, source_events)
    records = design_artifact_records(
        paths,
        draft_id=draft_id,
        events=source_events,
    )
    for record in records:
        validate_design_artifact_against_plan(
            paths,
            draft,
            plan,
            record,
        )
    validate_design_artifact_relations(records)
    documents: dict[str, bytes] = {}
    for record in records:
        output_path = str(record["output_path"])
        markdown_path = _markdown_output_path(output_path)
        if output_path in documents or markdown_path in documents:
            raise SdlcError(
                f"模块 {record['artifact_id']} 的投影路径与其他模块冲突。",
                exit_code=1,
            )
        documents[output_path] = canonical_json_text(record).encode("utf-8")
        documents[markdown_path] = design_artifact_markdown(record).encode("utf-8")
    return records, documents


def rebuild_design_artifact_projections(
    paths,
    draft_id: str,
) -> list[dict[str, object]]:
    from codex_sdlc.core import draft_artifacts
    from codex_sdlc.core.state import derive_state, load_events

    with project_lock(paths):
        state = derive_state(paths)
        draft = state.get("drafts", {}).get(draft_id)
        if not isinstance(draft, Mapping):
            raise SdlcError(f"没有找到 DRAFT `{draft_id}`。", exit_code=1)
        records, documents = design_artifact_projection_documents(
            paths,
            draft,
            events=load_events(paths),
        )
        draft_artifacts.write_projection_bundle(
            paths.draft_dir(draft_id),
            documents,
        )
        return records


def design_artifact_plan_status(
    paths,
    draft_id: str,
    *,
    events: Iterable[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    source_events = list(events) if events is not None else None
    if source_events is None:
        from codex_sdlc.core.state import load_events

        source_events = load_events(paths)
    plan = _plan_for_draft(paths, draft_id, source_events)
    records = design_artifact_records(
        paths,
        draft_id=draft_id,
        events=source_events,
    )
    completed = {str(item["artifact_id"]) for item in records}
    enabled = {
        str(module["module_id"])
        for module in plan["modules"]  # type: ignore[index]
        if module["status"] in ENABLED_PLAN_STATUSES
    }
    blocked = sorted(
        str(module["module_id"])
        for module in plan["modules"]  # type: ignore[index]
        if module["status"] == "blocked"
    )
    return {
        "status": "complete" if enabled == completed else "pending",
        "completed": sorted(completed),
        "pending": sorted(enabled - completed),
        "blocked": blocked,
    }


__all__ = [
    "DESIGN_ARTIFACT_EVENT",
    "DESIGN_ARTIFACT_SCHEMA",
    "ENABLED_PLAN_STATUSES",
    "FORMAL_FIELDS",
    "design_artifact_history",
    "design_artifact_markdown",
    "design_artifact_output_path",
    "design_artifact_plan_status",
    "design_artifact_projection_documents",
    "design_artifact_records",
    "expected_design_artifact_input_hashes",
    "expected_global_rule_refs",
    "normalize_design_artifact_submission",
    "plan_module_for_artifact",
    "rebuild_design_artifact_projections",
    "validate_design_artifact_against_plan",
    "validate_design_artifact_record",
    "validate_design_artifact_relations",
]
