from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable, Mapping

from codex_sdlc.core import draft_artifacts
from codex_sdlc.core.code_evidence import (
    assess_code_evidence,
    validate_code_evidence,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import AllocationObject, build_allocation_order
from codex_sdlc.core.state import load_events
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    validate_schema_document,
)


DESIGN_PLAN_SCHEMA = "design-plan.v1"
DESIGN_PLAN_EVENT = "draft_design_plan_imported"
MODULE_PREFIXES = {
    "data": "DATA",
    "api": "API",
    "page": "PAGE",
    "component": "COMP",
    "security": "SAFE",
    "deployment": "DEPLOY",
    "field": "FIELD",
    "special": "SPEC",
}
OUTPUT_PLACEHOLDER = "{module_id}"
FORMAL_MODULE_ID = re.compile(
    r"^(DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}$"
)
FORMAL_MODULE_TEXT = re.compile(
    r"(?:^|[^A-Z0-9])(DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}(?:$|[^0-9])"
)


def _module_status_contract(module: Mapping[str, object]) -> None:
    module_name = str(module.get("client_key") or module.get("module_id") or "")
    status = str(module.get("status") or "")
    outputs = module.get("outputs")
    blocked_by = module.get("blocked_by")
    material_refs = module.get("material_refs")
    evidence_paths = module.get("code_evidence_paths")
    if (
        not isinstance(outputs, list)
        or not isinstance(blocked_by, list)
        or not isinstance(material_refs, list)
        or not isinstance(evidence_paths, list)
    ):
        raise SdlcError(
            f"模块 {module_name} 的输出、阻塞对象或证据引用格式无效。",
            exit_code=1,
        )
    if status in {"required", "supplement_required", "completed"} and not outputs:
        raise SdlcError(f"模块 {module_name} 的状态为 {status}，必须声明输出。", exit_code=1)
    if status == "provided" and not material_refs and not evidence_paths:
        raise SdlcError(
            f"模块 {module_name} 的状态为 provided，必须引用有效资料或真实代码证据。",
            exit_code=1,
        )
    if status == "not_applicable" and outputs:
        raise SdlcError(f"模块 {module_name} 不适用，不能声明输出文件。", exit_code=1)
    if status == "blocked" and not blocked_by:
        raise SdlcError(f"模块 {module_name} 的状态为 blocked，必须声明明确阻塞对象。", exit_code=1)
    if status != "blocked" and blocked_by:
        raise SdlcError(f"模块 {module_name} 不是 blocked，不能声明阻塞对象。", exit_code=1)


def _normalized_selection(selection: Mapping[str, object]) -> dict[str, object]:
    code_files = [
        {"path": str(item["path"]), "reason_ref": str(item["reason_ref"])}
        for item in selection["code_files"]  # type: ignore[index]
    ]
    code_files.sort(key=lambda item: (item["path"], item["reason_ref"]))
    return {
        "purpose": "integrated_design",
        "rules": sorted(str(item) for item in selection["rules"]),  # type: ignore[index]
        "dependencies": sorted(str(item) for item in selection["dependencies"]),  # type: ignore[index]
        "code_files": code_files,
        "upstream_outputs": sorted(
            str(item) for item in selection["upstream_outputs"]  # type: ignore[index]
        ),
    }


def normalize_design_plan_submission(value: Mapping[str, object]) -> dict[str, object]:
    document = deepcopy(dict(value))
    validate_schema_document(document, schema_name=DESIGN_PLAN_SCHEMA)
    modules: list[dict[str, object]] = []
    for raw_module in document["modules"]:  # type: ignore[index]
        module = deepcopy(raw_module)
        for field in (
            "requirement_refs",
            "design_refs",
            "material_refs",
            "code_evidence_paths",
            "inputs",
            "outputs",
            "depends_on",
            "blocked_by",
        ):
            module[field] = sorted(str(item) for item in module[field])
        modules.append(module)
    modules.sort(key=lambda item: str(item["client_key"]))
    normalized = {
        "schema_version": DESIGN_PLAN_SCHEMA,
        "draft_id": str(document["draft_id"]),
        "global_impact": sorted(str(item) for item in document["global_impact"]),  # type: ignore[index]
        "modules": modules,
        "code_evidence": _normalized_selection(document["code_evidence"]),  # type: ignore[arg-type]
    }
    validate_design_plan_submission(normalized)
    return normalized


def validate_design_plan_submission(value: Mapping[str, object]) -> dict[str, object]:
    document = deepcopy(dict(value))
    validate_schema_document(document, schema_name=DESIGN_PLAN_SCHEMA)
    if "mapping" in document or "producer_run_id" in document:
        raise SdlcError("design-plan.v1 导入文件不能自报正式编号或运行身份。", exit_code=1)
    if not document["global_impact"]:
        raise SdlcError("设计总计划必须声明全局影响范围。", exit_code=1)

    selection = document["code_evidence"]
    if not isinstance(selection, Mapping):
        raise SdlcError("设计总计划缺少代码证据选择。", exit_code=1)
    selected_paths: list[str] = []
    for group in ("rules", "dependencies", "upstream_outputs"):
        selected_paths.extend(str(item) for item in selection[group])  # type: ignore[index]
    selected_paths.extend(
        str(item["path"]) for item in selection["code_files"]  # type: ignore[index]
    )
    if not selected_paths:
        raise SdlcError("设计总计划至少要选择一个真实代码证据文件。", exit_code=1)
    duplicates = sorted(
        path for path in set(selected_paths) if selected_paths.count(path) > 1
    )
    if duplicates:
        raise SdlcError(f"代码证据路径不能跨分组重复：{', '.join(duplicates)}。", exit_code=1)
    selected_set = set(selected_paths)

    modules = document["modules"]
    if not isinstance(modules, list):
        raise SdlcError("设计模块必须是数组。", exit_code=1)
    keys: set[str] = set()
    objects: list[AllocationObject] = []
    output_templates: set[str] = set()
    for module in modules:
        if not isinstance(module, Mapping):
            raise SdlcError("设计模块必须是对象。", exit_code=1)
        key = str(module["client_key"])
        if key in keys:
            raise SdlcError(f"设计模块 client_key 重复：{key}。", exit_code=1)
        keys.add(key)
        module_type = str(module["type"])
        _module_status_contract(module)
        if module_type == "special" and not str(module.get("special_reason") or "").strip():
            raise SdlcError(f"专项模块 {key} 必须说明不能归入固定模块的原因。", exit_code=1)
        for path in module["code_evidence_paths"]:  # type: ignore[index]
            if str(path) not in selected_set:
                raise SdlcError(f"模块 {key} 引用了未选择的代码证据路径：{path}。", exit_code=1)
        for output in module["outputs"]:  # type: ignore[index]
            output_text = str(output)
            if output_text.count(OUTPUT_PLACEHOLDER) != 1:
                raise SdlcError(
                    f"模块 {key} 的输出必须包含一次 {OUTPUT_PLACEHOLDER} 占位符。",
                    exit_code=1,
                )
            if "@client:" in output_text or FORMAL_MODULE_TEXT.search(output_text):
                raise SdlcError(f"模块 {key} 的输出不能自报正式模块编号。", exit_code=1)
            probe = output_text.replace(OUTPUT_PLACEHOLDER, "MODULE")
            probe_path = Path(probe)
            if (
                probe_path.is_absolute()
                or ".." in probe_path.parts
                or "\\" in probe
                or not probe_path.parts
                or probe_path.parts[0] != "设计"
                or probe_path.suffix != ".json"
            ):
                raise SdlcError(
                    f"模块 {key} 的输出必须是设计目录内的 JSON 相对路径。",
                    exit_code=1,
                )
            if output_text in output_templates:
                raise SdlcError(f"设计模块输出模板重复：{output_text}。", exit_code=1)
            output_templates.add(output_text)
        objects.append(
            AllocationObject(
                client_key=key,
                id_prefix=MODULE_PREFIXES[module_type],
                depends_on=tuple(str(item) for item in module["depends_on"]),  # type: ignore[index]
            )
        )
    build_allocation_order(objects)
    return document


def _validate_record_dependencies(modules: list[dict[str, object]]) -> None:
    by_id = {str(module["module_id"]): module for module in modules}
    if len(by_id) != len(modules):
        raise SdlcError("设计总计划包含重复模块编号。", exit_code=1)
    incoming: dict[str, int] = {module_id: 0 for module_id in by_id}
    dependents: dict[str, list[str]] = {module_id: [] for module_id in by_id}
    for module_id, module in by_id.items():
        for dependency in module["depends_on"]:  # type: ignore[index]
            dependency_id = str(dependency)
            if dependency_id not in by_id:
                raise SdlcError(f"模块 {module_id} 引用了计划外依赖：{dependency_id}。", exit_code=1)
            incoming[module_id] += 1
            dependents[dependency_id].append(module_id)
    ready = sorted(module_id for module_id, count in incoming.items() if count == 0)
    visited: list[str] = []
    while ready:
        current = ready.pop(0)
        visited.append(current)
        for dependent in sorted(dependents[current]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(visited) != len(modules):
        raise SdlcError("设计总计划的正式模块依赖存在环。", exit_code=1)


def validate_design_plan_record(value: Mapping[str, object]) -> dict[str, object]:
    record = deepcopy(dict(value))
    validate_schema_document(record, schema_name=DESIGN_PLAN_SCHEMA)
    if "mapping" not in record:
        raise SdlcError("事件中的设计总计划缺少正式编号映射。", exit_code=1)
    evidence = validate_code_evidence(record["code_evidence"])  # type: ignore[arg-type]
    if evidence["owner_id"] != record["draft_id"]:
        raise SdlcError("代码证据 owner_id 与设计总计划 DRAFT 不一致。", exit_code=1)
    input_hashes = record["input_hashes"]
    if not isinstance(input_hashes, Mapping) or input_hashes.get(
        "code_evidence"
    ) != evidence["relevant_content_sha256"]:
        raise SdlcError("设计总计划的代码证据输入哈希不一致。", exit_code=1)
    selected_evidence_paths = {
        str(item["path"])
        for group in ("rules", "dependencies", "code_files", "upstream_outputs")
        for item in evidence[group]  # type: ignore[index]
    }
    mapping = record["mapping"]
    modules = record["modules"]
    if not isinstance(mapping, Mapping) or not isinstance(modules, list):
        raise SdlcError("设计总计划的映射或模块格式无效。", exit_code=1)
    if {str(module["client_key"]) for module in modules} != set(mapping):
        raise SdlcError("设计总计划的 client_key 与正式编号映射不一致。", exit_code=1)

    normalized_modules: list[dict[str, object]] = []
    outputs: set[str] = set()
    for raw_module in modules:
        if not isinstance(raw_module, Mapping):
            raise SdlcError("事件中的设计模块必须是对象。", exit_code=1)
        module = deepcopy(dict(raw_module))
        module_id = str(module["module_id"])
        module_type = str(module["type"])
        key = str(module["client_key"])
        expected = str(mapping.get(key) or "")
        if module_id != expected:
            raise SdlcError(f"模块 {key} 的正式编号与映射不一致。", exit_code=1)
        expected_prefix = MODULE_PREFIXES[module_type] + "-"
        if not FORMAL_MODULE_ID.fullmatch(module_id) or not module_id.startswith(expected_prefix):
            raise SdlcError(f"模块 {module_id} 的类型和编号前缀不匹配。", exit_code=1)
        _module_status_contract(module)
        missing_evidence = sorted(
            set(str(item) for item in module["code_evidence_paths"])
            - selected_evidence_paths
        )
        if missing_evidence:
            raise SdlcError(
                f"模块 {module_id} 引用了计划外代码证据：{', '.join(missing_evidence)}。",
                exit_code=1,
            )
        for output in module["outputs"]:  # type: ignore[index]
            output_text = str(output)
            if OUTPUT_PLACEHOLDER in output_text or "@client:" in output_text:
                raise SdlcError(f"模块 {module_id} 仍包含未重写的临时输出。", exit_code=1)
            referenced_ids = {
                match.group(0)
                for match in re.finditer(
                    r"(?:DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}",
                    output_text,
                )
            }
            if referenced_ids != {module_id}:
                raise SdlcError(f"模块 {module_id} 的输出没有绑定自身正式编号。", exit_code=1)
            output_path = Path(output_text)
            if (
                output_path.is_absolute()
                or ".." in output_path.parts
                or "\\" in output_text
                or not output_path.parts
                or output_path.parts[0] != "设计"
                or output_path.suffix != ".json"
            ):
                raise SdlcError(
                    f"模块 {module_id} 的输出不是设计目录内的 JSON 相对路径。",
                    exit_code=1,
                )
            if output_text in outputs:
                raise SdlcError(f"设计总计划包含重复输出：{output_text}。", exit_code=1)
            outputs.add(output_text)
        normalized_modules.append(module)
    _validate_record_dependencies(normalized_modules)
    design_refs = {
        str(design_id)
        for module in normalized_modules
        for design_id in module["design_refs"]  # type: ignore[index]
    }
    expected_input_keys = {
        "requirement_confirmation",
        "code_evidence",
        *(f"design:{design_id}" for design_id in design_refs),
    }
    if set(input_hashes) != expected_input_keys:
        raise SdlcError("设计总计划的结构化输入哈希范围不完整。", exit_code=1)
    reverse_mapping = {str(value): str(key) for key, value in mapping.items()}
    allocation_objects = [
        AllocationObject(
            client_key=str(module["client_key"]),
            id_prefix=MODULE_PREFIXES[str(module["type"])],
            depends_on=tuple(
                f"@client:{reverse_mapping[str(dependency)]}"
                for dependency in module["depends_on"]  # type: ignore[index]
            ),
        )
        for module in normalized_modules
    ]
    expected_order = [
        item.client_key for item in build_allocation_order(allocation_objects)
    ]
    actual_order = [str(module["client_key"]) for module in normalized_modules]
    if actual_order != expected_order:
        raise SdlcError("设计总计划模块没有按稳定依赖顺序保存。", exit_code=1)
    expected_hash = canonical_sha256(
        {key: item for key, item in record.items() if key != "plan_sha256"}
    )
    if record.get("plan_sha256") != expected_hash:
        raise SdlcError("设计总计划记录哈希与事件内容不一致。", exit_code=1)
    return record


def design_plan_records(
    paths, *, draft_id: str | None = None, events: Iterable[Mapping[str, object]] | None = None
) -> list[dict[str, object]]:
    source_events = list(events) if events is not None else load_events(paths)
    by_draft: dict[str, dict[str, object]] = {}
    module_owners: dict[str, tuple[str, str]] = {}
    for event in source_events:
        if event.get("event_type") != DESIGN_PLAN_EVENT:
            continue
        payload = event.get("payload")
        plan = payload.get("design_plan") if isinstance(payload, Mapping) else None
        if not isinstance(plan, Mapping):
            raise SdlcError("设计总计划事件缺少结构化计划。", exit_code=1)
        record = validate_design_plan_record(plan)
        owner = str(record["draft_id"])
        if owner in by_draft and by_draft[owner]["plan_sha256"] != record["plan_sha256"]:
            raise SdlcError(f"{owner} 包含互相冲突的设计总计划事件。", exit_code=1)
        by_draft[owner] = record
        for module in record["modules"]:  # type: ignore[index]
            module_id = str(module["module_id"])
            digest = canonical_sha256(module)
            previous = module_owners.get(module_id)
            if previous is not None and previous != (owner, digest):
                raise SdlcError(f"正式模块编号 {module_id} 被不同内容重复使用。", exit_code=1)
            module_owners[module_id] = (owner, digest)
    records = [by_draft[key] for key in sorted(by_draft)]
    if draft_id is not None:
        records = [record for record in records if record["draft_id"] == draft_id]
    return records


def design_plan_markdown(record: Mapping[str, object]) -> str:
    plan = validate_design_plan_record(record)
    lines = [f"# {plan['draft_id']} 开发设计总计划", "", "## 全局影响范围", ""]
    lines.extend(f"- {item}" for item in plan["global_impact"])  # type: ignore[index]
    lines.extend(["", "## 设计模块", ""])
    for module in plan["modules"]:  # type: ignore[index]
        lines.extend(
            [
                f"### {module['module_id']}",
                "",
                f"- 类型：{module['type']}",
                f"- 状态：{module['status']}",
                f"- 原因：{module['reason']}",
                f"- 适用需求：{'、'.join(module['requirement_refs'])}",
                f"- 适用技术方案：{'、'.join(module['design_refs'])}",
                f"- 依赖：{'、'.join(module['depends_on']) or '无'}",
                f"- 输出：{'、'.join(module['outputs']) or '无'}",
                f"- 代码证据：{'、'.join(module['code_evidence_paths']) or '无'}",
                f"- 阻塞对象：{'、'.join(module['blocked_by']) or '无'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def rebuild_design_plan_projections(paths, draft_id: str) -> dict[str, object] | None:
    records = design_plan_records(paths, draft_id=draft_id)
    if not records:
        return None
    record = records[0]
    draft_artifacts.write_projection_bundle(
        paths.draft_dir(draft_id),
        {
            "设计/design-plan.v1.json": canonical_json_text(record).encode("utf-8"),
            "设计/code-evidence.v1.json": canonical_json_text(
                record["code_evidence"]
            ).encode("utf-8"),
            "设计/开发设计总计划.md": design_plan_markdown(record).encode("utf-8"),
        },
    )
    return record


def assess_design_plan(paths, record: Mapping[str, object]) -> dict[str, object]:
    plan = validate_design_plan_record(record)
    evidence = assess_code_evidence(paths, plan["code_evidence"])  # type: ignore[arg-type]
    return {
        "status": evidence["status"],
        "changed_paths": evidence["changed_paths"],
        "evidence_sha256": plan["code_evidence"]["evidence_sha256"],  # type: ignore[index]
    }


__all__ = [
    "DESIGN_PLAN_EVENT",
    "DESIGN_PLAN_SCHEMA",
    "MODULE_PREFIXES",
    "OUTPUT_PLACEHOLDER",
    "assess_design_plan",
    "design_plan_markdown",
    "design_plan_records",
    "normalize_design_plan_submission",
    "rebuild_design_plan_projections",
    "validate_design_plan_record",
    "validate_design_plan_submission",
]
