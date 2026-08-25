from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from codex_sdlc.core import draft_artifacts, draft_lifecycle
from codex_sdlc.core.atomic_import import (
    collect_known_formal_ids,
    load_import_registry,
)
from codex_sdlc.core.code_evidence import capture_code_evidence
from codex_sdlc.core.design_artifact_contract import (
    DESIGN_ARTIFACT_EVENT,
    FORMAL_FIELDS as DESIGN_ARTIFACT_FORMAL_FIELDS,
    design_artifact_history,
    design_artifact_output_path,
    design_artifact_plan_status,
    design_artifact_records,
    expected_design_artifact_input_hashes,
    expected_global_rule_refs,
    normalize_design_artifact_submission,
    plan_module_for_artifact,
    validate_design_artifact_against_plan,
    validate_design_artifact_record,
    validate_design_artifact_relations,
)
from codex_sdlc.core.design_plan_contract import (
    DESIGN_PLAN_EVENT,
    MODULE_PREFIXES,
    OUTPUT_PLACEHOLDER,
    assess_design_plan,
    design_plan_records,
    normalize_design_plan_submission,
    rebuild_design_plan_projections,
    validate_design_plan_record,
)
from codex_sdlc.core.design_summary_contract import (
    DESIGN_SUMMARY_EVENT,
    DESIGN_SUMMARY_JSON_PATH,
    DESIGN_SUMMARY_MARKDOWN_PATH,
    FORMAL_FIELDS as DESIGN_SUMMARY_FORMAL_FIELDS,
    design_summary_history,
    expected_affected_modules,
    expected_design_summary_input_hashes,
    normalize_design_summary_submission,
    validate_current_design_artifact_files,
    validate_design_summary_against_modules,
    validate_design_summary_record,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    allocate_stable_ids,
    build_allocation_order,
    rewrite_temporary_references,
)
from codex_sdlc.core.project import (
    project_lock,
    resolve_project_path,
)
from codex_sdlc.core.reference_locator import locate_reference
from codex_sdlc.core.state import (
    append_event,
    derive_state,
    design_ids,
    load_events,
    next_number,
    refresh_materialized_state,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    validate_schema_document,
)


DESIGN_REFERENCE_SCHEMA = "design-reference.v1"
DESIGN_REFERENCE_INDEX_SCHEMA = "design-reference-index.v1"
CLIENT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DESIGN_ID_PATTERN = re.compile(r"^DES-[0-9]{3,}$")
DRAFT_ID_PATTERN = re.compile(r"^DRAFT-[0-9]{3,}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_draft_id(value: object) -> str:
    clean = str(value or "").strip().upper()
    if not DRAFT_ID_PATTERN.fullmatch(clean):
        raise SdlcError("必须明确指定合法的 DRAFT 编号。", exit_code=1)
    return clean


def _clean_design_id(value: object) -> str:
    clean = str(value or "").strip().upper()
    if not DESIGN_ID_PATTERN.fullmatch(clean):
        raise SdlcError("必须明确指定合法的 DES 编号。", exit_code=1)
    return clean


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"结构化设计文件包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"design-reference.v1 包含非标准数字：{value}。", exit_code=1)


def _load_json_file(root: Path, raw_path: str) -> dict[str, Any]:
    try:
        path = resolve_project_path(root, raw_path, must_exist=True)
    except SdlcError as exc:
        raise SdlcError(f"技术方案引用文件无效：{exc.message}", exit_code=1) from exc
    if path.is_symlink() or not path.is_file():
        raise SdlcError("技术方案引用文件必须是项目内普通文件。", exit_code=1)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("技术方案引用文件读取失败或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("design-reference.v1 顶层必须是 JSON 对象。", exit_code=1)
    validate_schema_document(document, schema_name=DESIGN_REFERENCE_SCHEMA)
    if "design_id" in document:
        raise SdlcError("design-reference 导入文件不能预占 DES 编号。", exit_code=1)
    return document


def _load_design_plan_file(root: Path, raw_path: str) -> dict[str, object]:
    try:
        path = resolve_project_path(root, raw_path, must_exist=True)
    except SdlcError as exc:
        raise SdlcError(f"设计总计划文件无效：{exc.message}", exit_code=1) from exc
    current = root.resolve()
    for part in Path(raw_path).parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError("设计总计划文件路径不能包含符号链接。", exit_code=1)
    if path.is_symlink() or not path.is_file():
        raise SdlcError("设计总计划文件必须是项目内普通文件。", exit_code=1)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("设计总计划文件读取失败或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("design-plan.v1 顶层必须是 JSON 对象。", exit_code=1)
    return normalize_design_plan_submission(document)


def _load_design_artifact_file(root: Path, raw_path: str) -> dict[str, object]:
    try:
        path = resolve_project_path(root, raw_path, must_exist=True)
    except SdlcError as exc:
        raise SdlcError(f"模块化设计文件无效：{exc.message}", exit_code=1) from exc
    current = root.resolve()
    for part in Path(raw_path).parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError("模块化设计文件路径不能包含符号链接。", exit_code=1)
    if path.is_symlink() or not path.is_file():
        raise SdlcError("模块化设计文件必须是项目内普通文件。", exit_code=1)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("模块化设计文件读取失败或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("design-artifact.v1 顶层必须是 JSON 对象。", exit_code=1)
    reported = sorted(DESIGN_ARTIFACT_FORMAL_FIELDS.intersection(document))
    if reported:
        raise SdlcError(
            f"design-artifact 导入文件不能自报正式哈希、版本或运行字段：{', '.join(reported)}。",
            exit_code=1,
        )
    return normalize_design_artifact_submission(document)


def _load_design_summary_file(root: Path, raw_path: str) -> dict[str, object]:
    try:
        path = resolve_project_path(root, raw_path, must_exist=True)
    except SdlcError as exc:
        raise SdlcError(f"总体设计文件无效：{exc.message}", exit_code=1) from exc
    current = root.resolve()
    for part in Path(raw_path).parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError("总体设计文件路径不能包含符号链接。", exit_code=1)
    if path.is_symlink() or not path.is_file():
        raise SdlcError("总体设计文件必须是项目内普通文件。", exit_code=1)
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("总体设计文件读取失败或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("design-summary.v1 顶层必须是 JSON 对象。", exit_code=1)
    reported = sorted(DESIGN_SUMMARY_FORMAL_FIELDS.intersection(document))
    if reported:
        raise SdlcError(
            f"design-summary 导入文件不能自报正式哈希、版本、编号或失效结果：{', '.join(reported)}。",
            exit_code=1,
        )
    return normalize_design_summary_submission(document)


def _display_free_anchor(anchor: Mapping[str, object]) -> dict[str, object]:
    """展示名称不参与幂等和状态判断，正式身份只保留 client_key 与定位器。"""

    return {
        "client_key": str(anchor.get("client_key") or ""),
        "locator": deepcopy(anchor.get("locator")),
    }


def design_reference_identity(document: Mapping[str, object]) -> dict[str, object]:
    anchors: list[dict[str, object]] = []
    for raw_anchor in document.get("anchors", []):  # type: ignore[union-attr]
        if not isinstance(raw_anchor, Mapping):
            raise SdlcError("技术方案锚点必须是 JSON 对象。", exit_code=1)
        if "key" in raw_anchor:
            key = str(raw_anchor.get("key") or "")
            client_key = key.split("#", 1)[1] if "#" in key else ""
            anchors.append(
                {
                    "client_key": client_key,
                    "locator": deepcopy(raw_anchor.get("locator")),
                }
            )
        else:
            anchors.append(_display_free_anchor(raw_anchor))
    anchors.sort(key=lambda item: str(item["client_key"]))
    identity: dict[str, object] = {
        "draft_id": str(document.get("draft_id") or ""),
        "client_key": str(document.get("client_key") or ""),
        "material_id": str(document.get("material_id") or ""),
        "anchors": anchors,
        "applies_to": sorted(str(item) for item in document.get("applies_to", [])),  # type: ignore[arg-type]
    }
    supersedes = str(document.get("supersedes") or "")
    if supersedes:
        identity["supersedes"] = supersedes
    return identity


def design_reference_identity_sha256(document: Mapping[str, object]) -> str:
    return canonical_sha256(design_reference_identity(document))


def _record_content(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in record.items()
        if key != "record_sha256"
    }


def validate_design_reference_record(record: Mapping[str, object]) -> dict[str, Any]:
    value = deepcopy(dict(record))
    validate_schema_document(value, schema_name=DESIGN_REFERENCE_SCHEMA)
    if "design_id" not in value:
        raise SdlcError("事件中的技术方案引用缺少正式 DES 编号。", exit_code=1)
    design_id = _clean_design_id(value.get("design_id"))
    if value.get("identity_sha256") != design_reference_identity_sha256(value):
        raise SdlcError(f"{design_id} 的规范输入哈希与引用内容不一致。", exit_code=1)
    if value.get("record_sha256") != canonical_sha256(_record_content(value)):
        raise SdlcError(f"{design_id} 的记录哈希与事件内容不一致。", exit_code=1)
    seen: set[str] = set()
    for anchor in value.get("anchors", []):
        key = str(anchor.get("key") or "")
        expected_prefix = f"{design_id}#"
        client_key = key[len(expected_prefix) :] if key.startswith(expected_prefix) else ""
        if not CLIENT_KEY_PATTERN.fullmatch(client_key) or client_key in seen:
            raise SdlcError(f"{design_id} 包含无效或重复的正式锚点键。", exit_code=1)
        seen.add(client_key)
    return value


def design_reference_index_document(draft: Mapping[str, object]) -> dict[str, object]:
    references = [
        validate_design_reference_record(item)
        for item in draft.get("design_references", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    ]
    document = {
        "schema_version": DESIGN_REFERENCE_INDEX_SCHEMA,
        "draft_id": str(draft.get("draft_id") or ""),
        "design_references": sorted(references, key=lambda item: item["design_id"]),
    }
    validate_schema_document(document, schema_name=DESIGN_REFERENCE_INDEX_SCHEMA)
    return document


def _find_material(
    draft: Mapping[str, object],
    material_id: str,
    *,
    require_active: bool = True,
) -> dict[str, Any]:
    matches = [
        deepcopy(item)
        for item in draft.get("materials", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping) and item.get("material_id") == material_id
    ]
    if len(matches) != 1:
        raise SdlcError(f"技术方案资料不存在或编号不唯一：{material_id}。", exit_code=1)
    material = matches[0]
    roles = material.get("roles") if isinstance(material.get("roles"), list) else []
    if material.get("type") != "technical-solution" and "technical-solution" not in roles:
        raise SdlcError(f"{material_id} 不是 technical-solution 原始资料。", exit_code=1)
    if material.get("source_kind") != "file":
        raise SdlcError("技术方案引用只接受已经原样归档的本地文件资料。", exit_code=1)
    if require_active and material.get("status") != "active":
        raise SdlcError(f"技术方案资料 {material_id} 不是当前活动版本。", exit_code=1)
    return material


def _current_fr_ids(draft: Mapping[str, object]) -> set[str]:
    split = draft.get("requirement_split")
    mapping_record = draft.get("requirement_import")
    if not isinstance(split, Mapping) or not isinstance(mapping_record, Mapping):
        raise SdlcError("当前 DRAFT 缺少已经确认的结构化需求。", exit_code=1)
    mapping = mapping_record.get("mapping")
    if not isinstance(mapping, Mapping):
        raise SdlcError("当前 DRAFT 的需求编号映射缺失。", exit_code=1)
    expected_keys = {
        str(item.get("client_key") or "")
        for item in split.get("functional_requirements", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    }
    fr_ids = {
        str(value)
        for key, value in mapping.items()
        if str(key) in expected_keys and re.fullmatch(r"FR-[0-9]{3,}", str(value))
    }
    if len(fr_ids) != len(expected_keys):
        raise SdlcError("当前 DRAFT 的 FR 编号映射不完整。", exit_code=1)
    return fr_ids


def _current_confirmation_sha256(draft: Mapping[str, object]) -> str:
    state = draft.get("_requirement_confirmation_state")
    if not isinstance(state, Mapping) or state.get("can_advance") is not True:
        raise SdlcError("当前需求尚未确认，不能导入或确认技术方案引用。", exit_code=1)
    confirmation = state.get("current_confirmation")
    if not isinstance(confirmation, Mapping):
        raise SdlcError("当前需求确认记录缺失。", exit_code=1)
    digest = str(confirmation.get("confirmation_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SdlcError("当前需求确认记录哈希无效。", exit_code=1)
    return digest


def _material_reference(paths, draft: Mapping[str, object], material: Mapping[str, object]) -> dict[str, object]:
    stored_path = str(material.get("stored_path") or "")
    digest = str(material.get("sha256") or "")
    reference = {
        "schema_version": "reference-locator.v1",
        "path": stored_path,
        "sha256": digest,
        "locator": {"kind": "whole_file"},
    }
    try:
        match = locate_reference(paths.draft_dir(str(draft["draft_id"])), reference)
    except SdlcError as exc:
        raise SdlcError(exc.message, exit_code=1) from exc
    if match.file_sha256 != digest:
        raise SdlcError("技术方案原始资料完整哈希不一致。", exit_code=1)
    return reference


def _validate_display_heading(path: Path, locator: Mapping[str, object]) -> None:
    heading = str(locator.get("display_heading") or "").strip()
    if not heading:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SdlcError("带标题定位的技术方案必须是有效 UTF-8 文本。", exit_code=1) from exc
    pattern = re.compile(rf"^\s*#+\s+{re.escape(heading)}\s*$")
    matched = [index for index, line in enumerate(lines, 1) if pattern.fullmatch(line)]
    if not matched:
        raise SdlcError(f"技术方案中没有找到标题：{heading}。", exit_code=1)
    if len(matched) > 1:
        raise SdlcError(f"技术方案标题重复，不能唯一定位：{heading}。", exit_code=1)
    if str(locator.get("kind") or locator.get("type") or "") == "text_range":
        start = int(locator.get("line_start", locator.get("start_line", 0)))
        end = int(locator.get("line_end", locator.get("end_line", 0)))
        if not start <= matched[0] <= end:
            raise SdlcError(f"标题 {heading} 不在指定文本行范围内。", exit_code=1)


def _validated_anchors(
    paths,
    draft: Mapping[str, object],
    material: Mapping[str, object],
    design_id: str,
    anchors: object,
) -> list[dict[str, object]]:
    if not isinstance(anchors, list):
        raise SdlcError("技术方案引用 anchors 必须是数组。", exit_code=1)
    stored_path = str(material.get("stored_path") or "")
    digest = str(material.get("sha256") or "")
    resolved_path = paths.draft_dir(str(draft["draft_id"])) / stored_path
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_anchor in anchors:
        if not isinstance(raw_anchor, Mapping):
            raise SdlcError("技术方案锚点必须是 JSON 对象。", exit_code=1)
        client_key = str(raw_anchor.get("client_key") or "")
        if not CLIENT_KEY_PATTERN.fullmatch(client_key) or client_key in seen:
            raise SdlcError(f"技术方案锚点 client_key 无效或重复：{client_key}。", exit_code=1)
        seen.add(client_key)
        locator = raw_anchor.get("locator")
        if not isinstance(locator, Mapping):
            raise SdlcError(f"锚点 {client_key} 缺少 locator。", exit_code=1)
        _validate_display_heading(resolved_path, locator)
        try:
            match = locate_reference(
                paths.draft_dir(str(draft["draft_id"])),
                {
                    "schema_version": "reference-locator.v1",
                    "path": stored_path,
                    "sha256": digest,
                    "locator": deepcopy(dict(locator)),
                },
            )
        except SdlcError as exc:
            raise SdlcError(exc.message, exit_code=1) from exc
        fragment_sha256 = (
            match.fragment_sha256
            or (canonical_sha256(match.content) if match.content is not None else match.file_sha256)
        )
        anchor: dict[str, object] = {
            "key": f"{design_id}#{client_key}",
            "locator": deepcopy(dict(locator)),
            "fragment_sha256": fragment_sha256,
        }
        display_name = str(raw_anchor.get("display_name") or "").strip()
        if display_name:
            anchor["display_name"] = display_name
        result.append(anchor)
    return sorted(result, key=lambda item: str(item["key"]))


def validate_design_reference_source(
    paths,
    draft: Mapping[str, object],
    record: Mapping[str, object],
    *,
    require_current_confirmation: bool = True,
    require_active_material: bool = True,
    require_current_requirements: bool = True,
) -> None:
    value = validate_design_reference_record(record)
    material = _find_material(
        draft,
        str(value["material_id"]),
        require_active=require_active_material,
    )
    if str(material.get("stored_path") or "") != value["path"]:
        raise SdlcError(f"{value['design_id']} 的原始资料路径与 MAT 记录不一致。", exit_code=1)
    if str(material.get("sha256") or "") != value["sha256"]:
        raise SdlcError(f"{value['design_id']} 的原始资料完整哈希与 MAT 记录不一致。", exit_code=1)
    if require_current_confirmation and (
        value["requirement_confirmation_sha256"] != _current_confirmation_sha256(draft)
    ):
        raise SdlcError(f"{value['design_id']} 绑定的需求确认已经失效。", exit_code=1)
    if require_current_requirements:
        known_fr = _current_fr_ids(draft)
        invalid_fr = sorted(set(value["applies_to"]) - known_fr)
        if invalid_fr:
            raise SdlcError(f"{value['design_id']} 引用了不存在的 FR：{', '.join(invalid_fr)}。", exit_code=1)
    for anchor in value["anchors"]:
        locator = anchor["locator"]
        _validate_display_heading(
            paths.draft_dir(str(draft["draft_id"])) / str(value["path"]),
            locator,
        )
        try:
            match = locate_reference(
                paths.draft_dir(str(draft["draft_id"])),
                {
                    "schema_version": "reference-locator.v1",
                    "path": value["path"],
                    "sha256": value["sha256"],
                    "locator": locator,
                },
            )
        except SdlcError as exc:
            raise SdlcError(exc.message, exit_code=1) from exc
        actual_fragment = (
            match.fragment_sha256
            or (canonical_sha256(match.content) if match.content is not None else match.file_sha256)
        )
        if actual_fragment != anchor["fragment_sha256"]:
            raise SdlcError(f"{anchor['key']} 的定位片段哈希已经变化。", exit_code=1)


def _index_artifact_record(draft: Mapping[str, object]) -> dict[str, object]:
    index = design_reference_index_document(draft)
    content = canonical_json_text(index).encode("utf-8")
    input_hashes = {
        f"draft://{draft['draft_id']}/design-references": canonical_sha256(
            index["design_references"]
        )
    }
    return draft_artifacts.validate_artifact_record(
        {
            "record_version": "draft-artifact-record.v1",
            "draft_id": str(draft["draft_id"]),
            "source_path": "设计/des-index.v1.json",
            "artifact_type": "design_reference_index",
            "media_type": "application/json",
            "projection_kind": "structured_json",
            "document": index,
            "artifact_sha256": sha256_bytes(content),
            "input_hashes": input_hashes,
            "producer_task_id": "T-009",
            "producer_run_id": draft_artifacts.producer_run_id("T-009"),
        }
    )


def _restore_event_file(paths, existed: bool, original: bytes) -> None:
    if existed:
        paths.events_file.write_bytes(original)
    else:
        paths.events_file.unlink(missing_ok=True)


class DesignReferenceService:
    """把原始技术方案的确定性引用写入事件，并从事件重建全部投影。"""

    def __init__(self, paths, *, source: str = "sdlc-design-reference") -> None:
        self.paths = paths
        self.source = source

    @staticmethod
    def _editable_draft(state: Mapping[str, object], draft_id: str) -> dict[str, Any]:
        drafts = state.get("drafts")
        draft = drafts.get(draft_id) if isinstance(drafts, Mapping) else None
        if not isinstance(draft, dict):
            raise SdlcError(f"没有找到 DRAFT `{draft_id}`。", exit_code=1)
        if draft_lifecycle.is_started_draft(draft):
            raise SdlcError(f"{draft_id} 已经正式建档，不能再写入技术方案引用。", exit_code=1)
        return draft

    def _commit(
        self,
        *,
        draft: dict[str, Any],
        event_type: str,
        summary: str,
        payload: dict[str, object],
    ) -> None:
        layout = draft_artifacts.ensure_draft_layout(self.paths, str(draft["draft_id"]))
        managed_snapshot = draft_artifacts.snapshot_managed_files(layout.draft_dir)
        existed = self.paths.events_file.exists()
        original_events = self.paths.events_file.read_bytes() if existed else b""
        original_event_backups = {
            backup.name: backup.read_bytes()
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak")
            if backup.is_file() and not backup.is_symlink()
        }
        try:
            draft_artifacts.preflight_artifact_updates(
                layout.draft_dir,
                draft,
                payload.get("artifact_updates"),
            )
            append_event(
                self.paths,
                event_type=event_type,
                source=self.source,
                summary=summary,
                payload=payload,
            )
            refresh_materialized_state(self.paths)
        except Exception:
            _restore_event_file(self.paths, existed, original_events)
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak"):
                backup.unlink(missing_ok=True)
            for name, content in original_event_backups.items():
                (self.paths.backups_dir / name).write_bytes(content)
            draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            try:
                refresh_materialized_state(self.paths)
            except Exception:
                draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            raise

    def import_file(self, raw_draft_id: str, raw_file: str) -> dict[str, object]:
        draft_id = _clean_draft_id(raw_draft_id)
        with project_lock(self.paths):
            submission = _load_json_file(self.paths.root, raw_file)
            if str(submission.get("draft_id") or "").upper() != draft_id:
                raise SdlcError("命令目标 DRAFT 与 design-reference.v1 的 draft_id 不一致。", exit_code=1)
            state = derive_state(self.paths)
            draft = self._editable_draft(state, draft_id)
            requirement_confirmation_sha256 = _current_confirmation_sha256(draft)
            known_fr = _current_fr_ids(draft)
            applies_to = sorted(set(str(item) for item in submission["applies_to"]))
            invalid_fr = sorted(set(applies_to) - known_fr)
            if invalid_fr:
                raise SdlcError(f"技术方案引用包含不存在的 FR：{', '.join(invalid_fr)}。", exit_code=1)

            material = _find_material(draft, str(submission["material_id"]))
            whole_reference = _material_reference(self.paths, draft, material)
            existing = [
                validate_design_reference_record(item)
                for item in draft.get("design_references", [])
                if isinstance(item, Mapping)
            ]
            client_key = str(submission["client_key"])
            same_key = [item for item in existing if item["client_key"] == client_key]
            submission_identity = design_reference_identity_sha256(
                {
                    **submission,
                    "applies_to": applies_to,
                }
            )
            if same_key:
                if len(same_key) == 1 and same_key[0]["identity_sha256"] == submission_identity:
                    validate_design_reference_source(self.paths, draft, same_key[0])
                    return {"action": "idempotent", "record": same_key[0]}
                raise SdlcError(
                    f"client_key `{client_key}` 已绑定不同内容；已确认记录不能原地覆盖，请使用新的 client_key 建立修订。",
                    exit_code=1,
                )

            supersedes = str(submission.get("supersedes") or "")
            if supersedes:
                superseded = next((item for item in existing if item["design_id"] == supersedes), None)
                if superseded is None or superseded["status"] != "confirmed":
                    raise SdlcError("技术方案修订只能明确替代当前 DRAFT 中已确认的 DES。", exit_code=1)
                if any(item.get("supersedes") == supersedes for item in existing):
                    raise SdlcError(f"{supersedes} 已经有后续修订，不能再次分叉替代。", exit_code=1)

            new_design_id = next_number(design_ids(state), "DES")
            anchors = _validated_anchors(
                self.paths,
                draft,
                material,
                new_design_id,
                submission["anchors"],
            )
            record: dict[str, object] = {
                "schema_version": DESIGN_REFERENCE_SCHEMA,
                "design_id": new_design_id,
                "draft_id": draft_id,
                "client_key": client_key,
                "material_id": str(material["material_id"]),
                "path": str(whole_reference["path"]),
                "sha256": str(whole_reference["sha256"]),
                "anchors": anchors,
                "applies_to": applies_to,
                "requirement_confirmation_sha256": requirement_confirmation_sha256,
                "identity_sha256": submission_identity,
                "status": "draft",
            }
            display_name = str(submission.get("display_name") or "").strip()
            if display_name:
                record["display_name"] = display_name
            if supersedes:
                record["supersedes"] = supersedes
            record["record_sha256"] = canonical_sha256(record)
            record = validate_design_reference_record(record)
            candidate = deepcopy(draft)
            candidate.setdefault("design_references", []).append(deepcopy(record))
            candidate["design_references"].sort(key=lambda item: str(item["design_id"]))
            candidate["_design_reference_enabled"] = True
            artifact = _index_artifact_record(candidate)
            payload = {
                "draft_id": draft_id,
                "reference": deepcopy(record),
                "artifact_updates": [artifact],
            }
            self._commit(
                draft=candidate,
                event_type="draft_design_reference_imported",
                summary=f"导入 {draft_id} 技术方案引用 {new_design_id}",
                payload=payload,
            )
            return {"action": "created", "record": record}

    def confirm(self, raw_draft_id: str, raw_design_id: str) -> dict[str, object]:
        draft_id = _clean_draft_id(raw_draft_id)
        design_id = _clean_design_id(raw_design_id)
        with project_lock(self.paths):
            state = derive_state(self.paths)
            draft = self._editable_draft(state, draft_id)
            _current_confirmation_sha256(draft)
            references = [
                validate_design_reference_record(item)
                for item in draft.get("design_references", [])
                if isinstance(item, Mapping)
            ]
            record = next((item for item in references if item["design_id"] == design_id), None)
            if record is None:
                raise SdlcError(f"{draft_id} 中没有找到技术方案引用 `{design_id}`。", exit_code=1)
            validate_design_reference_source(self.paths, draft, record)
            if record["status"] == "confirmed":
                return {"action": "idempotent", "record": record}
            confirmed = deepcopy(record)
            confirmed["status"] = "confirmed"
            confirmed["confirmed_at"] = _now_iso()
            confirmed.pop("record_sha256", None)
            confirmed["record_sha256"] = canonical_sha256(confirmed)
            confirmed = validate_design_reference_record(confirmed)
            candidate = deepcopy(draft)
            candidate["design_references"] = [
                deepcopy(confirmed) if item.get("design_id") == design_id else deepcopy(item)
                for item in references
            ]
            candidate["_design_reference_enabled"] = True
            artifact = _index_artifact_record(candidate)
            payload = {
                "draft_id": draft_id,
                "reference": deepcopy(confirmed),
                "artifact_updates": [artifact],
            }
            self._commit(
                draft=candidate,
                event_type="draft_design_reference_confirmed",
                summary=f"确认 {draft_id} 技术方案引用 {design_id}",
                payload=payload,
            )
            return {"action": "confirmed", "record": confirmed}


class DesignPlanService:
    """只接收模型明确提交的结构化计划，模块适用性不会从正文或展示文字推导。"""

    def __init__(self, paths, *, source: str = "sdlc-design-plan") -> None:
        self.paths = paths
        self.source = source

    @staticmethod
    def _confirmed_designs(
        paths,
        draft: Mapping[str, object],
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for raw in draft.get("design_references", []):  # type: ignore[union-attr]
            if not isinstance(raw, Mapping):
                continue
            record = validate_design_reference_record(raw)
            if record["status"] != "confirmed":
                continue
            validate_design_reference_source(paths, draft, record)
            result[str(record["design_id"])] = record
        if not result:
            raise SdlcError("当前 DRAFT 还没有已确认的技术方案引用。", exit_code=1)
        return result

    @staticmethod
    def _validate_plan_refs(
        draft: Mapping[str, object],
        submission: Mapping[str, object],
        confirmed_designs: Mapping[str, Mapping[str, object]],
    ) -> None:
        known_fr = _current_fr_ids(draft)
        known_materials = {
            str(item.get("material_id") or "")
            for item in draft.get("materials", [])  # type: ignore[union-attr]
            if isinstance(item, Mapping) and item.get("status") == "active"
        }
        selected_code_paths = {
            str(item)
            for group in ("rules", "dependencies", "upstream_outputs")
            for item in submission["code_evidence"][group]  # type: ignore[index]
        }
        selected_code_paths.update(
            str(item["path"])
            for item in submission["code_evidence"]["code_files"]  # type: ignore[index]
        )
        for item in submission["code_evidence"]["code_files"]:  # type: ignore[index]
            if str(item["reason_ref"]) not in known_fr:
                raise SdlcError(
                    f"代码证据 {item['path']} 引用了不存在的 FR：{item['reason_ref']}。",
                    exit_code=1,
                )

        for module in submission["modules"]:  # type: ignore[index]
            client_key = str(module["client_key"])
            invalid_fr = sorted(set(module["requirement_refs"]) - known_fr)
            if invalid_fr:
                raise SdlcError(
                    f"模块 {client_key} 引用了不存在的 FR：{', '.join(invalid_fr)}。",
                    exit_code=1,
                )
            invalid_des = sorted(set(module["design_refs"]) - set(confirmed_designs))
            if invalid_des:
                raise SdlcError(
                    f"模块 {client_key} 引用了未确认或不存在的 DES：{', '.join(invalid_des)}。",
                    exit_code=1,
                )
            covered_fr = {
                str(fr)
                for design_id in module["design_refs"]
                for fr in confirmed_designs[str(design_id)]["applies_to"]
            }
            uncovered = sorted(set(module["requirement_refs"]) - covered_fr)
            if uncovered:
                raise SdlcError(
                    f"模块 {client_key} 的 DES 没有覆盖适用 FR：{', '.join(uncovered)}。",
                    exit_code=1,
                )
            invalid_mat = sorted(set(module["material_refs"]) - known_materials)
            if invalid_mat:
                raise SdlcError(
                    f"模块 {client_key} 引用了不存在或非活动的 MAT：{', '.join(invalid_mat)}。",
                    exit_code=1,
                )
            missing_code = sorted(
                set(module["code_evidence_paths"]) - selected_code_paths
            )
            if missing_code:
                raise SdlcError(
                    f"模块 {client_key} 引用了未采集的代码证据：{', '.join(missing_code)}。",
                    exit_code=1,
                )

    def _commit(self, draft_id: str, plan: Mapping[str, object]) -> None:
        layout = draft_artifacts.ensure_draft_layout(self.paths, draft_id)
        managed_snapshot = draft_artifacts.snapshot_managed_files(layout.draft_dir)
        existed = self.paths.events_file.exists()
        original_events = self.paths.events_file.read_bytes() if existed else b""
        original_event_backups = {
            backup.name: backup.read_bytes()
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak")
            if backup.is_file() and not backup.is_symlink()
        }
        try:
            append_event(
                self.paths,
                event_type=DESIGN_PLAN_EVENT,
                source=self.source,
                summary=f"导入 {draft_id} 开发设计总计划",
                payload={"draft_id": draft_id, "design_plan": deepcopy(dict(plan))},
            )
            # 先确认公共状态仍可从事件重放，再由设计计划事件生成三份正式投影。
            refresh_materialized_state(self.paths)
            rebuild_design_plan_projections(self.paths, draft_id)
        except Exception:
            _restore_event_file(self.paths, existed, original_events)
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak"):
                backup.unlink(missing_ok=True)
            for name, content in original_event_backups.items():
                (self.paths.backups_dir / name).write_bytes(content)
            draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            try:
                refresh_materialized_state(self.paths)
                rebuild_design_plan_projections(self.paths, draft_id)
            except Exception:
                draft_artifacts.restore_managed_files(layout.draft_dir, managed_snapshot)
            raise

    def import_file(self, raw_draft_id: str, raw_file: str) -> dict[str, object]:
        draft_id = _clean_draft_id(raw_draft_id)
        with project_lock(self.paths):
            submission = _load_design_plan_file(self.paths.root, raw_file)
            if str(submission["draft_id"]).upper() != draft_id:
                raise SdlcError(
                    "命令目标 DRAFT 与 design-plan.v1 的 draft_id 不一致。",
                    exit_code=1,
                )
            state = derive_state(self.paths)
            draft = DesignReferenceService._editable_draft(state, draft_id)
            confirmation_sha256 = _current_confirmation_sha256(draft)
            confirmed_designs = self._confirmed_designs(self.paths, draft)
            self._validate_plan_refs(draft, submission, confirmed_designs)
            submission_sha256 = canonical_sha256(submission)

            events = load_events(self.paths)
            existing_plans = design_plan_records(
                self.paths, draft_id=draft_id, events=events
            )
            if existing_plans:
                existing = existing_plans[0]
                if existing["submission_sha256"] != submission_sha256:
                    raise SdlcError(
                        f"{draft_id} 已经登记了不同内容的设计总计划，不能原地覆盖。",
                        exit_code=1,
                    )
                return {
                    "action": "idempotent",
                    "record": existing,
                    "assessment": assess_design_plan(self.paths, existing),
                }

            evidence = capture_code_evidence(
                self.paths,
                owner_id=draft_id,
                selection=submission["code_evidence"],  # type: ignore[arg-type]
            )
            registry = load_import_registry(self.paths)
            known_ids = set(collect_known_formal_ids(events, registry))
            known_ids.update(design_ids(state))
            for plan in design_plan_records(self.paths, events=events):
                known_ids.update(
                    str(module["module_id"]) for module in plan["modules"]  # type: ignore[index]
                )
            objects = [
                AllocationObject(
                    client_key=str(module["client_key"]),
                    id_prefix=MODULE_PREFIXES[str(module["type"])],
                    depends_on=tuple(str(item) for item in module["depends_on"]),
                )
                for module in submission["modules"]  # type: ignore[index]
            ]
            mapping = allocate_stable_ids(objects, existing_ids=known_ids)
            source_by_key = {
                str(module["client_key"]): module
                for module in submission["modules"]  # type: ignore[index]
            }
            modules: list[dict[str, object]] = []
            for item in build_allocation_order(objects):
                source_module = deepcopy(source_by_key[item.client_key])
                module_id = mapping[item.client_key]
                source_module["module_id"] = module_id
                source_module["depends_on"] = rewrite_temporary_references(
                    source_module["depends_on"], mapping
                )
                source_module["outputs"] = [
                    str(output).replace(OUTPUT_PLACEHOLDER, module_id)
                    for output in source_module["outputs"]
                ]
                modules.append(source_module)

            used_designs = sorted(
                {
                    str(design_id)
                    for module in modules
                    for design_id in module["design_refs"]
                }
            )
            input_hashes = {
                "requirement_confirmation": confirmation_sha256,
                **{
                    f"design:{design_id}": str(
                        confirmed_designs[design_id]["record_sha256"]
                    )
                    for design_id in used_designs
                },
                "code_evidence": str(evidence["relevant_content_sha256"]),
            }
            record: dict[str, object] = {
                "schema_version": "design-plan.v1",
                "draft_id": draft_id,
                "producer_run_id": os.environ.get("CODEX_THREAD_ID", "local"),
                "input_hashes": input_hashes,
                "global_impact": deepcopy(submission["global_impact"]),
                "modules": modules,
                "mapping": dict(sorted(mapping.items())),
                "code_evidence": evidence,
                "submission_sha256": submission_sha256,
            }
            record["plan_sha256"] = canonical_sha256(record)
            record = validate_design_plan_record(record)
            self._commit(draft_id, record)
            return {
                "action": "created",
                "record": record,
                "assessment": assess_design_plan(self.paths, record),
            }


class DesignArtifactService:
    """模块内容来自显式结构化文件，CLI 只核对当前计划、真实输入和稳定引用。"""

    def __init__(self, paths, *, source: str = "sdlc-design-artifact") -> None:
        self.paths = paths
        self.source = source

    @staticmethod
    def _enabled_plan_module(
        plan: Mapping[str, object],
        artifact_id: str,
    ) -> dict[str, object]:
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
        if status not in {
            "required",
            "provided",
            "supplement_required",
            "completed",
        }:
            raise SdlcError(
                f"模块 {artifact_id} 的计划状态不能导入完整产物：{status}。",
                exit_code=1,
            )
        return module

    def _preflight_output(
        self,
        draft_id: str,
        output_path: str,
        existing_records: list[Mapping[str, object]],
    ) -> None:
        markdown_path = str(Path(output_path).with_suffix(".md")).replace("\\", "/")
        managed = {
            str(item["output_path"])
            for item in existing_records
            if item["draft_id"] == draft_id
        }
        managed.update(
            str(Path(path).with_suffix(".md")).replace("\\", "/")
            for path in tuple(managed)
        )
        draft_dir = self.paths.draft_dir(draft_id)
        for relative in (output_path, markdown_path):
            # output_path 从 DRAFT 根目录起算，直接拼接才能保留计划中的“设计/”层级。
            target = draft_dir / relative
            if target.exists() and relative not in managed:
                raise SdlcError(
                    f"模块化设计目标已经存在但不属于事件投影：{relative}。",
                    exit_code=1,
                )

    def _commit(self, draft_id: str, record: Mapping[str, object]) -> None:
        layout = draft_artifacts.ensure_draft_layout(self.paths, draft_id)
        managed_snapshot = draft_artifacts.snapshot_managed_files(layout.draft_dir)
        existed = self.paths.events_file.exists()
        original_events = self.paths.events_file.read_bytes() if existed else b""
        original_event_backups = {
            backup.name: backup.read_bytes()
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak")
            if backup.is_file() and not backup.is_symlink()
        }
        try:
            append_event(
                self.paths,
                event_type=DESIGN_ARTIFACT_EVENT,
                source=self.source,
                summary=f"导入 {draft_id} 模块化设计产物 {record['artifact_id']}",
                payload={
                    "draft_id": draft_id,
                    "artifact": deepcopy(dict(record)),
                },
            )
            # 事件先通过完整状态重放，再统一生成 JSON 和 Markdown，避免两种投影内容分叉。
            refresh_materialized_state(self.paths)
        except Exception:
            _restore_event_file(self.paths, existed, original_events)
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak"):
                backup.unlink(missing_ok=True)
            for name, content in original_event_backups.items():
                (self.paths.backups_dir / name).write_bytes(content)
            draft_artifacts.restore_managed_files(
                layout.draft_dir,
                managed_snapshot,
            )
            try:
                refresh_materialized_state(self.paths)
            except Exception:
                draft_artifacts.restore_managed_files(
                    layout.draft_dir,
                    managed_snapshot,
                )
            raise

    def import_file(self, raw_draft_id: str, raw_file: str) -> dict[str, object]:
        draft_id = _clean_draft_id(raw_draft_id)
        with project_lock(self.paths):
            submission = _load_design_artifact_file(self.paths.root, raw_file)
            if str(submission["draft_id"]).upper() != draft_id:
                raise SdlcError(
                    "命令目标 DRAFT 与 design-artifact.v1 的 draft_id 不一致。",
                    exit_code=1,
                )
            state = derive_state(self.paths)
            draft = DesignReferenceService._editable_draft(state, draft_id)
            events = load_events(self.paths)
            plans = design_plan_records(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            if len(plans) != 1:
                raise SdlcError(
                    f"{draft_id} 缺少唯一有效的开发设计总计划。",
                    exit_code=1,
                )
            plan = validate_design_plan_record(plans[0])
            assessment = assess_design_plan(self.paths, plan)
            if assessment["status"] != "current":
                changed = "、".join(assessment["changed_paths"])
                raise SdlcError(
                    f"开发设计总计划的代码证据已变化：{changed}。",
                    exit_code=1,
                )

            artifact_id = str(submission["artifact_id"])
            module = self._enabled_plan_module(plan, artifact_id)
            submission_sha256 = canonical_sha256(submission)
            history = design_artifact_history(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            artifact_history = [
                item for item in history if item["artifact_id"] == artifact_id
            ]
            current_records = design_artifact_records(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            if artifact_history and artifact_history[-1]["submission_sha256"] == submission_sha256:
                existing = artifact_history[-1]
                validate_design_artifact_against_plan(
                    self.paths,
                    draft,
                    plan,
                    existing,
                )
                validate_design_artifact_relations(current_records)
                return {
                    "action": "idempotent",
                    "record": existing,
                    "completion": design_artifact_plan_status(
                        self.paths,
                        draft_id,
                        events=events,
                    ),
                }

            output_path = design_artifact_output_path(module)
            self._preflight_output(draft_id, output_path, current_records)
            revision = len(artifact_history) + 1
            record: dict[str, object] = {
                "schema_version": "design-artifact.v1",
                "draft_id": draft_id,
                "artifact_id": artifact_id,
                "type": str(submission["type"]),
                "producer_run_id": os.environ.get("CODEX_THREAD_ID", "local"),
                "input_hashes": expected_design_artifact_input_hashes(
                    self.paths,
                    draft,
                    plan,
                    module,
                ),
                "requirement_refs": sorted(
                    str(item) for item in submission["requirement_refs"]
                ),
                "global_rule_refs": sorted(
                    str(item) for item in submission["global_rule_refs"]
                ),
                "material_refs": sorted(
                    str(item) for item in submission["material_refs"]
                ),
                "depends_on": sorted(
                    str(item) for item in submission["depends_on"]
                ),
                "code_evidence_paths": sorted(
                    str(item) for item in module["code_evidence_paths"]
                ),
                "plan_status": str(module["status"]),
                "output_path": output_path,
                "revision": revision,
                "previous_artifact_sha256": (
                    str(artifact_history[-1]["artifact_sha256"])
                    if artifact_history
                    else None
                ),
                "content": deepcopy(submission["content"]),
                "open_questions": [],
                "plan_sha256": str(plan["plan_sha256"]),
                "plan_module_sha256": canonical_sha256(module),
                "submission_sha256": submission_sha256,
            }
            record["artifact_sha256"] = canonical_sha256(record)
            record = validate_design_artifact_record(record)
            validate_design_artifact_against_plan(
                self.paths,
                draft,
                plan,
                record,
            )
            candidate = {
                str(item["artifact_id"]): item for item in current_records
            }
            candidate[artifact_id] = record
            validate_design_artifact_relations(
                [candidate[key] for key in sorted(candidate)]
            )
            self._commit(draft_id, record)
            committed_events = load_events(self.paths)
            return {
                "action": "created",
                "record": record,
                "completion": design_artifact_plan_status(
                    self.paths,
                    draft_id,
                    events=committed_events,
                ),
            }


class DesignSummaryService:
    """总体说明只汇总显式公共关系，所有来源编号都核对当前模块事件和真实投影。"""

    def __init__(self, paths, *, source: str = "sdlc-design-summary") -> None:
        self.paths = paths
        self.source = source

    def _preflight_output(
        self,
        draft_id: str,
        *,
        has_history: bool,
    ) -> None:
        if has_history:
            return
        draft_dir = self.paths.draft_dir(draft_id)
        for relative_path in (
            DESIGN_SUMMARY_JSON_PATH,
            DESIGN_SUMMARY_MARKDOWN_PATH,
        ):
            target = draft_dir / relative_path
            if target.exists():
                raise SdlcError(
                    f"总体设计目标已经存在但不属于事件投影：{relative_path}。",
                    exit_code=1,
                )

    def _commit(self, draft_id: str, record: Mapping[str, object]) -> None:
        layout = draft_artifacts.ensure_draft_layout(self.paths, draft_id)
        managed_snapshot = draft_artifacts.snapshot_managed_files(
            layout.draft_dir
        )
        existed = self.paths.events_file.exists()
        original_events = self.paths.events_file.read_bytes() if existed else b""
        original_event_backups = {
            backup.name: backup.read_bytes()
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak")
            if backup.is_file() and not backup.is_symlink()
        }
        try:
            append_event(
                self.paths,
                event_type=DESIGN_SUMMARY_EVENT,
                source=self.source,
                summary=f"导入 {draft_id} 总体设计说明",
                payload={
                    "draft_id": draft_id,
                    "summary": deepcopy(dict(record)),
                },
            )
            # 事件追加后统一刷新总体说明、模块投影和产物索引，三者不会各自保存不同哈希。
            refresh_materialized_state(self.paths)
        except Exception:
            _restore_event_file(self.paths, existed, original_events)
            for backup in self.paths.backups_dir.glob("events-*.jsonl.bak"):
                backup.unlink(missing_ok=True)
            for name, content in original_event_backups.items():
                (self.paths.backups_dir / name).write_bytes(content)
            draft_artifacts.restore_managed_files(
                layout.draft_dir,
                managed_snapshot,
            )
            try:
                refresh_materialized_state(self.paths)
            except Exception:
                draft_artifacts.restore_managed_files(
                    layout.draft_dir,
                    managed_snapshot,
                )
            raise

    def import_file(
        self,
        raw_draft_id: str,
        raw_file: str,
    ) -> dict[str, object]:
        draft_id = _clean_draft_id(raw_draft_id)
        with project_lock(self.paths):
            submission = _load_design_summary_file(
                self.paths.root,
                raw_file,
            )
            if str(submission["draft_id"]).upper() != draft_id:
                raise SdlcError(
                    "命令目标 DRAFT 与 design-summary.v1 的 draft_id 不一致。",
                    exit_code=1,
                )
            state = derive_state(self.paths)
            draft = DesignReferenceService._editable_draft(state, draft_id)
            events = load_events(self.paths)
            plans = design_plan_records(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            if len(plans) != 1:
                raise SdlcError(
                    f"{draft_id} 缺少唯一有效的开发设计总计划。",
                    exit_code=1,
                )
            plan = validate_design_plan_record(plans[0])
            assessment = assess_design_plan(self.paths, plan)
            if assessment["status"] != "current":
                changed = "、".join(assessment["changed_paths"])
                raise SdlcError(
                    f"开发设计总计划的代码证据已变化：{changed}。",
                    exit_code=1,
                )
            completion = design_artifact_plan_status(
                self.paths,
                draft_id,
                events=events,
            )
            if completion["status"] != "complete" or completion["blocked"]:
                pending = "、".join(completion["pending"]) or "无"
                blocked = "、".join(completion["blocked"]) or "无"
                raise SdlcError(
                    f"{draft_id} 缺少当前有效且完整的模块产物；待导入：{pending}；阻塞：{blocked}。",
                    exit_code=1,
                )
            modules = design_artifact_records(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            for module in modules:
                validate_design_artifact_against_plan(
                    self.paths,
                    draft,
                    plan,
                    module,
                )
            validate_design_artifact_relations(modules)
            # 总体说明开始前必须确认 T-011 的真实 JSON 和 Markdown 没被删除或改写。
            validate_current_design_artifact_files(self.paths, modules)

            history = design_summary_history(
                self.paths,
                draft_id=draft_id,
                events=events,
            )
            submission_sha256 = canonical_sha256(submission)
            if history and history[-1]["submission_sha256"] == submission_sha256:
                existing = history[-1]
                validate_design_summary_against_modules(
                    self.paths,
                    draft,
                    existing,
                    events=events,
                )
                return {
                    "action": "idempotent",
                    "record": existing,
                }

            previous = history[-1] if history else None
            current_input_hashes = expected_design_summary_input_hashes(
                plan,
                modules,
            )
            expected_affected = expected_affected_modules(
                previous,
                submission["common_objects"],  # type: ignore[arg-type]
                current_input_hashes=current_input_hashes,
            )
            if submission["affected_modules"] != expected_affected:
                raise SdlcError(
                    "总体设计说明的 affected_modules 必须等于发生变化的公共对象显式关联模块："
                    f"{'、'.join(expected_affected)}。",
                    exit_code=1,
                )
            self._preflight_output(
                draft_id,
                has_history=bool(history),
            )
            revision = len(history) + 1
            summary_id = f"DSUM-{draft_id.split('-', 1)[1]}"
            record: dict[str, object] = {
                "schema_version": "design-summary.v1",
                "draft_id": draft_id,
                "summary_id": summary_id,
                "producer_run_id": os.environ.get(
                    "CODEX_THREAD_ID",
                    "local",
                ),
                "input_hashes": current_input_hashes,
                "common_objects": deepcopy(submission["common_objects"]),
                "affected_modules": deepcopy(submission["affected_modules"]),
                "open_questions": [],
                "revision": revision,
                "previous_summary_sha256": (
                    str(previous["summary_sha256"])
                    if previous is not None
                    else None
                ),
                "submission_sha256": submission_sha256,
                "invalidated_modules": (
                    deepcopy(submission["affected_modules"])
                    if revision > 1
                    else []
                ),
                "invalidated_review_targets": (
                    [
                        {
                            "stage": "integrated_design",
                            "owner_id": draft_id,
                            "status": "stale",
                        }
                    ]
                    if revision > 1
                    else []
                ),
            }
            record["summary_sha256"] = canonical_sha256(record)
            record = validate_design_summary_record(record)
            validate_design_summary_against_modules(
                self.paths,
                draft,
                record,
                events=events,
            )
            self._commit(draft_id, record)
            return {
                "action": "created",
                "record": record,
            }


def modular_design_markdown(draft: Mapping[str, object]) -> str:
    """按结构化计划实际启用的模块生成总览，不为不适用模块补空章节。"""

    draft_id = str(draft.get("draft_id") or "")
    stage = (
        draft.get("design_stage")
        if isinstance(draft.get("design_stage"), Mapping)
        else {}
    )
    assert isinstance(stage, Mapping)
    lines = [
        f"# {draft_id} 模块化设计",
        "",
        f"- DRAFT 状态：{draft.get('status', '')}",
        f"- 设计计划状态：{stage.get('plan_status', 'missing')}",
        f"- 总体说明状态：{stage.get('summary_status', 'waiting_for_plan')}",
    ]

    references = [
        item
        for item in draft.get("design_references", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping) and item.get("status") == "confirmed"
    ]
    if references:
        lines.extend(["", "## 技术方案引用", ""])
        for reference in references:
            lines.append(
                f"- {reference['design_id']}：{reference['path']}（SHA-256：{reference['sha256']}）"
            )

    plan = draft.get("_design_plan_record")
    if isinstance(plan, Mapping):
        lines.extend(
            [
                "",
                "## 开发设计总计划",
                "",
                "- 文件：[开发设计总计划](设计/开发设计总计划.md)",
                f"- 完整哈希：{plan['plan_sha256']}",
            ]
        )

    artifacts = {
        str(item.get("artifact_id") or ""): item
        for item in draft.get("design_artifacts", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    }
    module_states = [
        item
        for item in stage.get("module_states", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    ]
    enabled_states = [
        item
        for item in module_states
        if item.get("plan_status")
        in {"required", "provided", "supplement_required", "completed"}
    ]
    if enabled_states:
        lines.extend(["", "## 已启用模块", ""])
        for module in enabled_states:
            module_id = str(module["module_id"])
            lines.extend(
                [
                    f"### {module_id}",
                    "",
                    f"- 类型：{module['type']}",
                    f"- 计划状态：{module['plan_status']}",
                    f"- 产物状态：{module['artifact_status']}",
                ]
            )
            artifact = artifacts.get(module_id)
            if artifact is not None:
                output_path = str(artifact["output_path"])
                markdown_path = Path(output_path).with_suffix(".md").as_posix()
                lines.extend(
                    [
                        f"- JSON：[{output_path}]({output_path})",
                        f"- Markdown：[{markdown_path}]({markdown_path})",
                        f"- 产物哈希：{artifact['artifact_sha256']}",
                    ]
                )
            lines.append("")

    blocked_states = [
        item
        for item in module_states
        if item.get("plan_status") == "blocked"
    ]
    if blocked_states and isinstance(plan, Mapping):
        plan_modules = {
            str(item.get("module_id") or ""): item
            for item in plan.get("modules", [])  # type: ignore[union-attr]
            if isinstance(item, Mapping)
        }
        lines.extend(["", "## 结构化阻塞项", ""])
        for module in blocked_states:
            module_id = str(module["module_id"])
            blocked_by = plan_modules.get(module_id, {}).get("blocked_by", [])
            lines.append(
                f"- {module_id}：{'、'.join(str(item) for item in blocked_by)}"
            )

    summaries = [
        item
        for item in draft.get("design_summaries", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    ]
    if summaries:
        latest = summaries[-1]
        lines.extend(
            [
                "",
                "## 总体设计说明",
                "",
                "- 文件：[总体设计说明](设计/总体设计说明.md)",
                f"- 编号：{latest['summary_id']}",
                f"- 版本序号：{latest['revision']}",
                f"- 完整哈希：{latest['summary_sha256']}",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def design_reference_markdown(draft: Mapping[str, object]) -> str:
    """派生展示只显示引用元数据，不复制技术方案正文或摘要。"""

    lines = [f"# {draft.get('draft_id', '')} 技术方案引用", ""]
    references = [
        validate_design_reference_record(item)
        for item in draft.get("design_references", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    ]
    if not references:
        lines.append("- 暂无技术方案引用")
        return "\n".join(lines).rstrip() + "\n"
    for record in references:
        display_name = str(record.get("display_name") or "").strip()
        heading = f"{record['design_id']}"
        if display_name:
            heading += f" {display_name}"
        lines.extend(
            [
                f"## {heading}",
                "",
                f"- 状态：{record['status']}",
                f"- 原始资料：{record['material_id']}",
                f"- 文件：{record['path']}",
                f"- 完整哈希：{record['sha256']}",
                f"- 适用需求：{'、'.join(record['applies_to'])}",
                "",
                "### 精确锚点",
            ]
        )
        for anchor in record["anchors"]:
            name = str(anchor.get("display_name") or "").strip()
            label = f"（{name}）" if name else ""
            lines.append(
                f"- {anchor['key']}{label}：{json.dumps(anchor['locator'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DESIGN_REFERENCE_INDEX_SCHEMA",
    "DESIGN_REFERENCE_SCHEMA",
    "DesignArtifactService",
    "DesignPlanService",
    "DesignReferenceService",
    "DesignSummaryService",
    "design_reference_identity",
    "design_reference_identity_sha256",
    "design_reference_index_document",
    "design_reference_markdown",
    "modular_design_markdown",
    "validate_design_reference_record",
    "validate_design_reference_source",
]
