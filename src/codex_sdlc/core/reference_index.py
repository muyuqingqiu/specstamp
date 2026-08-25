from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
import hmac
import json
import os
from pathlib import Path
import re
import tempfile

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.reference_locator import (
    REFERENCE_LOCATOR_SCHEMA,
    validate_reference,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)


REFERENCE_INDEX_SCHEMA = "reference-index.v1"
REFERENCE_INDEX_PATH = "reference-index.v1.json"
_REFERENCE_ID_PATTERN = re.compile(
    r"^(?:FR|GR|AC|DES|MAT|DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-"
    r"[0-9]{3,}(?:#[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REQUIREMENT_RECEIPT_PATH = "需求/需求导入回执.json"
_REQUIREMENT_RECEIPT_FIELDS = {
    "schema",
    "package_key",
    "package_sha256",
    "mapping",
    "destination",
    "files",
    "event_id",
    "imported_at",
    "producer_run_id",
    "review_blockers",
}
_BINARY_SUFFIXES = {
    ".avif",
    ".bmp",
    ".fig",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".psd",
    ".sketch",
    ".webp",
    ".zip",
}


def _strict_json(path: Path, label: str) -> object:
    """重复字段会让同一编号出现两种解释，因此读取时必须直接拒绝。"""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SdlcError(f"{label}包含重复字段：{key}。", exit_code=1)
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise SdlcError(f"{label}包含非标准数字：{value}。", exit_code=1)

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是有效 JSON：{path.name}。", exit_code=1) from exc


def _clean_relative_path(value: object, *, label: str) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    candidate = Path(clean)
    if (
        not clean
        or "\x00" in clean
        or candidate.is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise SdlcError(f"{label}不是安全的相对路径：{value}。", exit_code=1)
    return candidate.as_posix()


def _strict_file(root: Path, relative_path: object, *, label: str) -> Path:
    """正式定位不接受任何目录链接，避免同一相对路径随后指向别的业务文件。"""

    clean = _clean_relative_path(relative_path, label=label)
    try:
        resolved_root = Path(root).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SdlcError(f"{label}的允许根目录不存在。", exit_code=1) from exc
    if not resolved_root.is_dir():
        raise SdlcError(f"{label}的允许根目录不是目录。", exit_code=1)
    current = Path(root)
    if current.is_symlink():
        raise SdlcError(f"{label}的允许根目录不能是符号链接。", exit_code=1)
    for part in Path(clean).parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"{label}不能经过符号链接：{clean}。", exit_code=1)
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{label}不存在或越过允许根目录：{clean}。", exit_code=1) from exc
    if not resolved.is_file():
        raise SdlcError(f"{label}不是普通文件：{clean}。", exit_code=1)
    return resolved


def _require_sha256(value: object, *, label: str) -> str:
    digest = str(value or "")
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise SdlcError(f"{label}必须是64位小写十六进制 SHA-256。", exit_code=1)
    return digest


def _manifest_items(
    artifact_manifest: Mapping[str, object] | Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if isinstance(artifact_manifest, Mapping):
        if artifact_manifest.get("schema_version") == "artifact-index.v1":
            from codex_sdlc.core.artifact_index import validate_artifact_index_document

            index = validate_artifact_index_document(artifact_manifest)
            raw_items = [
                item
                for item in index["artifacts"]  # type: ignore[union-attr]
                if isinstance(item, Mapping) and item.get("include_in_formal") is True
            ]
        elif isinstance(artifact_manifest.get("artifact_manifest"), list):
            raw_items = artifact_manifest["artifact_manifest"]  # type: ignore[assignment]
        else:
            raise SdlcError("引用索引缺少已校验的 artifact manifest。", exit_code=1)
    elif isinstance(artifact_manifest, Sequence) and not isinstance(
        artifact_manifest, (str, bytes, bytearray)
    ):
        raw_items = list(artifact_manifest)
    else:
        raise SdlcError("artifact manifest 必须是对象或数组。", exit_code=1)

    items: list[dict[str, object]] = []
    artifact_ids: set[str] = set()
    source_paths: set[str] = set()
    archive_paths: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise SdlcError("artifact manifest 的每一项都必须是对象。", exit_code=1)
        item = deepcopy(dict(raw))
        artifact_id = str(item.get("artifact_id") or "")
        source_path = _clean_relative_path(
            item.get("source_path"), label="DRAFT source_path"
        )
        archive_path = _clean_relative_path(
            item.get("archive_path"), label="正式 archive_path"
        )
        if not archive_path.startswith("original/"):
            raise SdlcError(
                f"正式 archive_path 不在 original 下：{archive_path}。",
                exit_code=1,
            )
        if not artifact_id or artifact_id in artifact_ids:
            raise SdlcError(f"artifact manifest 的 ART 编号缺失或重复：{artifact_id}。", exit_code=1)
        if source_path in source_paths:
            raise SdlcError(f"artifact manifest 的 source_path 重复：{source_path}。", exit_code=1)
        if archive_path in archive_paths:
            raise SdlcError(f"artifact manifest 的 archive_path 重复：{archive_path}。", exit_code=1)
        _require_sha256(item.get("sha256"), label=f"{artifact_id} sha256")
        artifact_ids.add(artifact_id)
        source_paths.add(source_path)
        archive_paths.add(archive_path)
        item["source_path"] = source_path
        item["archive_path"] = archive_path
        items.append(item)
    if not items:
        raise SdlcError("artifact manifest 不能为空。", exit_code=1)
    return items


def _current_artifact_index(
    source_root: Path,
) -> dict[str, object]:
    """每次生成都从真实 DRAFT 重新读索引，避免调用方旧缓存替换当前路径或集合。"""

    from codex_sdlc.core.artifact_index import (
        ARTIFACT_INDEX_PATH,
        validate_artifact_index_document,
    )

    index_path = _strict_file(
        source_root,
        ARTIFACT_INDEX_PATH,
        label="当前 artifact-index.v1",
    )
    raw_index = _strict_json(index_path, "当前 artifact-index.v1")
    if not isinstance(raw_index, Mapping):
        raise SdlcError("当前 artifact-index.v1 顶层必须是对象。", exit_code=1)
    index = validate_artifact_index_document(raw_index)
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list):
        raise SdlcError("当前 artifact-index.v1 缺少 artifacts。", exit_code=1)
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise SdlcError("当前 artifact-index.v1 包含无效产物项。", exit_code=1)
        source_path = str(raw.get("source_path") or "")
        source_file = _strict_file(
            source_root,
            source_path,
            label="当前 artifact-index.v1 source_path",
        )
        expected = _require_sha256(
            raw.get("sha256"),
            label=f"{raw.get('artifact_id')} sha256",
        )
        if not hmac.compare_digest(sha256_file(source_file), expected):
            raise SdlcError(
                f"当前 artifact-index.v1 的文件哈希不一致：{source_path}。",
                exit_code=1,
            )
    return index


def _manifest_comparison_items(
    items: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """把正式清单规范成稳定字段集合，显式补出 include_in_formal 后再做等值比较。"""

    result: list[dict[str, object]] = []
    for item in items:
        include = item.get("include_in_formal", True)
        if include is not True:
            raise SdlcError("调用方正式清单包含未批准归档的产物。", exit_code=1)
        result.append(
            {
                "artifact_id": item.get("artifact_id"),
                "business_id": item.get("business_id"),
                "artifact_type": item.get("artifact_type"),
                "source_path": item.get("source_path"),
                "archive_path": item.get("archive_path"),
                "sha256": item.get("sha256"),
                "include_in_formal": True,
                "review_relations": deepcopy(item.get("review_relations")),
            }
        )
    return sorted(result, key=lambda item: str(item["artifact_id"]))


def _verified_manifest_items(
    source_root: Path,
    artifact_manifest: Mapping[str, object] | Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """调用方清单只作一致性声明，后续生成只消费当前真实 artifact-index.v1。"""

    from codex_sdlc.core.artifact_index import (
        formal_manifest_entries,
        validate_artifact_index_document,
    )

    current_index = _current_artifact_index(source_root)
    if (
        isinstance(artifact_manifest, Mapping)
        and artifact_manifest.get("schema_version") == "artifact-index.v1"
    ):
        supplied_index = validate_artifact_index_document(artifact_manifest)
        if canonical_sha256(supplied_index) != canonical_sha256(current_index):
            raise SdlcError(
                "调用方 artifact-index.v1 与当前真实索引不一致。",
                exit_code=1,
            )
    supplied_items = _manifest_items(artifact_manifest)
    current_items = _manifest_items(formal_manifest_entries(current_index))
    if canonical_sha256(_manifest_comparison_items(supplied_items)) != canonical_sha256(
        _manifest_comparison_items(current_items)
    ):
        raise SdlcError(
            "调用方 artifact manifest 与当前真实 artifact-index.v1 不一致。",
            exit_code=1,
        )
    return current_items, current_index


def _validate_manifest_files(
    source_root: Path,
    items: Sequence[Mapping[str, object]],
    *,
    archive_root: Path | None,
) -> None:
    """先复核 DRAFT 原字节；正式目录存在时再逐项复核 archive_path。"""

    for item in items:
        source_path = str(item["source_path"])
        expected = str(item["sha256"])
        source_file = _strict_file(source_root, source_path, label="DRAFT source_path")
        if not hmac.compare_digest(sha256_file(source_file), expected):
            raise SdlcError(
                f"DRAFT source_path 文件哈希不一致：{source_path}。",
                exit_code=1,
            )
        if archive_root is None:
            continue
        archive_path = str(item["archive_path"])
        archive_file = _strict_file(
            archive_root, archive_path, label="正式 archive_path"
        )
        if not hmac.compare_digest(sha256_file(archive_file), expected):
            raise SdlcError(
                f"正式 archive_path 文件哈希不一致：{archive_path}。",
                exit_code=1,
            )


def _item_by_business_id(
    items: Sequence[Mapping[str, object]], business_id: str
) -> Mapping[str, object]:
    matches = [item for item in items if item.get("business_id") == business_id]
    if len(matches) != 1:
        raise SdlcError(f"正式资料编号没有唯一归档文件：{business_id}。", exit_code=1)
    return matches[0]


def _selected_path(item: Mapping[str, object], *, archive: bool) -> str:
    return str(item["archive_path" if archive else "source_path"])


def _reference(
    item: Mapping[str, object],
    locator: Mapping[str, object],
    *,
    archive: bool,
) -> dict[str, object]:
    return {
        "schema_version": REFERENCE_LOCATOR_SCHEMA,
        "path": _selected_path(item, archive=archive),
        "sha256": str(item["sha256"]),
        "locator": deepcopy(dict(locator)),
    }


def _put_entry(
    entries: dict[str, dict[str, object]],
    display: dict[str, dict[str, str]],
    reference_id: object,
    reference: Mapping[str, object],
    *,
    heading: object = None,
    display_name: object = None,
) -> None:
    clean_id = str(reference_id or "")
    if _REFERENCE_ID_PATTERN.fullmatch(clean_id) is None:
        raise SdlcError(f"正式引用编号无效：{clean_id}。", exit_code=1)
    if clean_id in entries:
        raise SdlcError(f"正式引用编号重复：{clean_id}。", exit_code=1)
    entries[clean_id] = deepcopy(dict(reference))
    display_item: dict[str, str] = {}
    if isinstance(heading, str) and heading:
        display_item["heading"] = heading
    if isinstance(display_name, str) and display_name:
        display_item["display_name"] = display_name
    if display_item:
        # 展示字段单独保存，定位和 doctor 校验始终只消费 reference-locator.v1。
        display[clean_id] = display_item


def _requirement_entries(
    entries: dict[str, dict[str, object]],
    display: dict[str, dict[str, str]],
    item: Mapping[str, object],
    document: Mapping[str, object],
    mapping: Mapping[str, object],
    *,
    archive: bool,
) -> None:
    validate_schema_document(document, schema_name="requirement-split.v1")
    expected_keys: dict[str, str] = {}

    def add(
        raw: object,
        prefix: str,
        pointer: str,
        *,
        heading: object = None,
    ) -> None:
        if not isinstance(raw, Mapping):
            raise SdlcError(f"{pointer} 不是结构化需求对象。", exit_code=1)
        client_key = str(raw.get("client_key") or "")
        formal_id = str(mapping.get(client_key) or "")
        if not client_key or re.fullmatch(rf"{prefix}-[0-9]{{3,}}", formal_id) is None:
            raise SdlcError(f"需求对象缺少合法稳定编号：{client_key}。", exit_code=1)
        expected_keys[client_key] = formal_id
        _put_entry(
            entries,
            display,
            formal_id,
            _reference(item, {"kind": "json_pointer", "value": pointer}, archive=archive),
            heading=heading,
        )

    for index, raw in enumerate(document.get("global_rules", [])):  # type: ignore[union-attr]
        add(raw, "GR", f"/global_rules/{index}", heading=raw.get("title") if isinstance(raw, Mapping) else None)
    for fr_index, raw in enumerate(document.get("functional_requirements", [])):  # type: ignore[union-attr]
        add(
            raw,
            "FR",
            f"/functional_requirements/{fr_index}",
            heading=raw.get("title") if isinstance(raw, Mapping) else None,
        )
        if not isinstance(raw, Mapping):
            continue
        for ac_index, criterion in enumerate(raw.get("acceptance_criteria", [])):  # type: ignore[union-attr]
            add(
                criterion,
                "AC",
                f"/functional_requirements/{fr_index}/acceptance_criteria/{ac_index}",
            )
    missing = set(expected_keys) - set(mapping)
    if missing:
        raise SdlcError(
            f"需求编号映射缺少 FR、GR、AC 对象：{', '.join(sorted(missing))}。",
            exit_code=1,
        )
    extra = set(mapping) - set(expected_keys)
    if any(
        re.fullmatch(r"SRC-[0-9]{3,}", str(mapping[key] or "")) is None
        for key in extra
    ):
        raise SdlcError("需求编号映射包含无法核对的额外对象。", exit_code=1)
    mapping_values = [str(value) for value in mapping.values()]
    if len(set(mapping_values)) != len(mapping_values):
        raise SdlcError("需求编号映射包含重复正式编号。", exit_code=1)


def _current_requirement_mapping(
    source_root: Path,
    document: Mapping[str, object],
    current_index: Mapping[str, object],
) -> dict[str, str]:
    """稳定编号必须按当前真实回执复核，不能从标题、正文或调用方缓存推断。"""

    artifacts = current_index.get("artifacts")
    receipt_items = [
        item
        for item in artifacts if isinstance(item, Mapping)  # type: ignore[union-attr]
        and item.get("artifact_type") == "requirement_import_receipt"
    ]
    if len(receipt_items) != 1:
        raise SdlcError(
            "当前 artifact-index.v1 没有唯一需求导入回执记录。",
            exit_code=1,
        )
    receipt_item = receipt_items[0]
    # 必须先证明回执类型在全局只有一份，再检查固定路径和归档属性；否则先按路径
    # 过滤会静默忽略另一条真实登记，让正式编号同时存在两个事实来源。
    if (
        receipt_item.get("source_path") != _REQUIREMENT_RECEIPT_PATH
        or receipt_item.get("include_in_formal") is not False
    ):
        raise SdlcError(
            "当前 artifact-index.v1 的需求导入回执路径或归档属性无效。",
            exit_code=1,
        )
    receipt_path = _strict_file(
        source_root,
        _REQUIREMENT_RECEIPT_PATH,
        label="当前需求导入回执",
    )
    receipt = _strict_json(receipt_path, "当前需求导入回执")
    if not isinstance(receipt, Mapping):
        raise SdlcError("当前需求导入回执顶层必须是对象。", exit_code=1)
    if set(receipt) != _REQUIREMENT_RECEIPT_FIELDS:
        raise SdlcError("当前需求导入回执字段不完整或包含未知字段。", exit_code=1)
    draft_id = str(document.get("draft_id") or "")
    package_key = str(receipt.get("package_key") or "")
    match = re.fullmatch(
        r"draft-requirements:(DRAFT-[0-9]{3,}):([0-9a-f]{64})",
        package_key,
    )
    if (
        receipt.get("schema") != "draft-requirement-import-receipt.v1"
        or match is None
        or match.group(1) != draft_id
        or str(receipt.get("producer_run_id") or "")
        != str(document.get("producer_run_id") or "")
    ):
        raise SdlcError("当前需求导入回执与需求拆分不一致。", exit_code=1)
    _require_sha256(receipt.get("package_sha256"), label="需求导入包 sha256")
    destination = str(receipt.get("destination") or "")
    expected_destination = (
        f".codex-sdlc/drafts/{draft_id}/需求/requirements-{match.group(2)}"
    )
    raw_files = receipt.get("files")
    expected_files = {
        f"{expected_destination}/requirement-split.v1.json",
        f"{expected_destination}/requirement-coverage.v1.json",
    }
    if (
        destination != expected_destination
        or not isinstance(raw_files, list)
        or raw_files != sorted(expected_files)
        or not all(isinstance(value, str) for value in raw_files)
    ):
        raise SdlcError("当前需求导入回执的目标或文件清单无效。", exit_code=1)
    if not all(
        isinstance(receipt.get(field), str) and bool(receipt.get(field))
        for field in ("event_id", "imported_at")
    ):
        raise SdlcError("当前需求导入回执缺少有效事件信息。", exit_code=1)
    blockers = receipt.get("review_blockers")
    if not isinstance(blockers, list) or not all(
        isinstance(value, str) for value in blockers
    ):
        raise SdlcError("当前需求导入回执的审核阻塞项无效。", exit_code=1)
    raw_mapping = receipt.get("mapping")
    if not isinstance(raw_mapping, Mapping) or not raw_mapping:
        raise SdlcError("当前需求导入回执缺少稳定编号映射。", exit_code=1)
    mapping: dict[str, str] = {}
    for client_key, formal_id in raw_mapping.items():
        if (
            not isinstance(client_key, str)
            or not client_key
            or not isinstance(formal_id, str)
            or re.fullmatch(r"(?:FR|GR|AC|SRC)-[0-9]{3,}", formal_id) is None
        ):
            raise SdlcError("当前需求导入回执包含无效稳定编号映射。", exit_code=1)
        mapping[client_key] = formal_id
    if len(set(mapping.values())) != len(mapping):
        raise SdlcError("当前需求导入回执包含重复正式编号。", exit_code=1)

    if not hmac.compare_digest(
        str(receipt_item.get("sha256") or ""),
        sha256_file(receipt_path),
    ):
        raise SdlcError("当前需求导入回执与 artifact-index.v1 不一致。", exit_code=1)
    return mapping


def _manifest_item_for_source_path(
    items: Sequence[Mapping[str, object]], raw_path: object
) -> Mapping[str, object]:
    clean = _clean_relative_path(raw_path, label="节点索引 source_path")
    matches = [
        item
        for item in items
        if clean == item["source_path"] or clean.endswith(f"/{item['source_path']}")
    ]
    if len(matches) != 1:
        raise SdlcError(
            f"节点索引没有唯一 artifact manifest 条目：{clean}。",
            exit_code=1,
        )
    return matches[0]


def _formal_locator(
    locator: Mapping[str, object],
    items: Sequence[Mapping[str, object]],
    *,
    archive: bool,
) -> dict[str, object]:
    result = deepcopy(dict(locator))
    kind = result.get("kind", result.get("type"))
    if kind != "design_node":
        return result
    index_item = _manifest_item_for_source_path(items, result.get("node_index_path"))
    declared_hash = _require_sha256(
        result.get("node_index_sha256"), label="node_index_sha256"
    )
    if not hmac.compare_digest(declared_hash, str(index_item["sha256"])):
        raise SdlcError("design_node 的节点索引哈希与归档清单不一致。", exit_code=1)
    result["node_index_path"] = _selected_path(index_item, archive=archive)
    return result


def _design_entries(
    entries: dict[str, dict[str, object]],
    display: dict[str, dict[str, str]],
    items: Sequence[Mapping[str, object]],
    design_references: Iterable[Mapping[str, object]],
    *,
    source_root: Path,
    archive: bool,
) -> None:
    from codex_sdlc.services.design_service import validate_design_reference_record

    for raw in design_references:
        record = validate_design_reference_record(raw)
        if record.get("status") != "confirmed":
            continue
        record_items = [
            candidate
            for candidate in items
            if candidate.get("artifact_type") == "design_reference"
            and candidate.get("business_id") == record["design_id"]
        ]
        if len(record_items) != 1:
            raise SdlcError(
                f"{record['design_id']} 没有唯一结构化引用记录。",
                exit_code=1,
            )
        record_path = _strict_file(
            source_root,
            record_items[0]["source_path"],
            label=f"{record['design_id']} 引用记录 source_path",
        )
        disk_record = _strict_json(record_path, f"{record['design_id']} 引用记录")
        if canonical_sha256(disk_record) != canonical_sha256(record):
            raise SdlcError(
                f"{record['design_id']} 的结构化对象与真实引用记录不一致。",
                exit_code=1,
            )
        material_id = str(record["material_id"])
        item = _item_by_business_id(items, material_id)
        if not hmac.compare_digest(str(record["sha256"]), str(item["sha256"])):
            raise SdlcError(f"{record['design_id']} 的主文件哈希与 MAT 归档不一致。", exit_code=1)
        source_path = str(item["source_path"])
        main_path = _clean_relative_path(record["path"], label=f"{record['design_id']} path")
        if main_path != source_path and not main_path.endswith(f"/{source_path}"):
            raise SdlcError(f"{record['design_id']} 的主文件路径与 MAT 归档不一致。", exit_code=1)
        for anchor in record["anchors"]:
            locator = _formal_locator(anchor["locator"], items, archive=archive)
            _put_entry(
                entries,
                display,
                anchor["key"],
                _reference(item, locator, archive=archive),
                display_name=anchor.get("display_name"),
            )


def _module_entries(
    entries: dict[str, dict[str, object]],
    display: dict[str, dict[str, str]],
    items: Sequence[Mapping[str, object]],
    design_artifacts: Iterable[Mapping[str, object]],
    *,
    source_root: Path,
    archive: bool,
) -> None:
    from codex_sdlc.core.design_artifact_contract import (
        validate_design_artifact_record,
    )

    for raw in design_artifacts:
        document = validate_design_artifact_record(raw)
        artifact_id = str(document.get("artifact_id") or "")
        item = _item_by_business_id(items, artifact_id)
        source_file = _strict_file(
            source_root,
            item["source_path"],
            label=f"{artifact_id} source_path",
        )
        disk_document = _strict_json(source_file, f"{artifact_id} 模块文件")
        if canonical_sha256(disk_document) != canonical_sha256(document):
            raise SdlcError(f"{artifact_id} 的结构化对象与真实模块文件不一致。", exit_code=1)
        _put_entry(
            entries,
            display,
            artifact_id,
            _reference(item, {"kind": "json_pointer", "value": "/"}, archive=archive),
        )


def _material_entries(
    entries: dict[str, dict[str, object]],
    display: dict[str, dict[str, str]],
    items: Sequence[Mapping[str, object]],
    material_references: Iterable[Mapping[str, object]],
    *,
    archive: bool,
) -> None:
    material_items = [
        item
        for item in items
        if isinstance(item.get("business_id"), str)
        and re.fullmatch(r"MAT-[0-9]{3,}", str(item["business_id"]))
        and item.get("artifact_type") == "material"
    ]
    for item in material_items:
        material_id = str(item["business_id"])
        # 普通二进制资料没有明确节点清单时只给整文件定位，不根据标题或扩展名补造节点。
        _put_entry(
            entries,
            display,
            material_id,
            _reference(item, {"kind": "whole_file"}, archive=archive),
        )
    for raw in material_references:
        if not isinstance(raw, Mapping):
            raise SdlcError("资料细粒度引用必须是对象。", exit_code=1)
        material_id = str(raw.get("material_id") or "")
        reference_id = str(raw.get("reference_id") or material_id)
        locator = raw.get("locator")
        if not isinstance(locator, Mapping):
            raise SdlcError(f"{reference_id} 缺少 locator。", exit_code=1)
        item = _item_by_business_id(items, material_id)
        formal_locator = _formal_locator(locator, items, archive=archive)
        kind = formal_locator.get("kind", formal_locator.get("type"))
        suffix = Path(str(item["source_path"])).suffix.lower()
        if suffix in _BINARY_SUFFIXES and kind not in {
            "whole_file",
            "pdf_region",
            "design_node",
        }:
            raise SdlcError(
                f"普通二进制资料 {material_id} 只能使用 whole_file、PDF 或有效 design_node 定位。",
                exit_code=1,
            )
        # 基础 MAT 已经占用编号，细粒度资料引用必须使用显式锚点键。
        if reference_id == material_id:
            raise SdlcError(
                f"资料细粒度引用必须使用 {material_id}#锚点 形式。",
                exit_code=1,
            )
        _put_entry(
            entries,
            display,
            reference_id,
            _reference(item, formal_locator, archive=archive),
            display_name=raw.get("display_name"),
        )


def build_reference_index_document(
    source_root: Path,
    requirement_id: str,
    artifact_manifest: Mapping[str, object] | Sequence[Mapping[str, object]],
    *,
    archive_root: Path | None = None,
    requirement_split: Mapping[str, object] | None = None,
    requirement_mapping: Mapping[str, object] | None = None,
    design_references: Iterable[Mapping[str, object]] = (),
    design_artifacts: Iterable[Mapping[str, object]] = (),
    material_references: Iterable[Mapping[str, object]] = (),
) -> dict[str, object]:
    """从受管清单生成引用；正式目录存在时只输出 archive_path。"""

    clean_requirement_id = str(requirement_id or "").strip().upper()
    if re.fullmatch(r"REQ-[0-9]{3,}", clean_requirement_id) is None:
        raise SdlcError("reference-index.v1 缺少合法 REQ 编号。", exit_code=1)
    items, current_index = _verified_manifest_items(
        Path(source_root),
        artifact_manifest,
    )
    _validate_manifest_files(Path(source_root), items, archive_root=archive_root)
    archive = archive_root is not None
    entries: dict[str, dict[str, object]] = {}
    display: dict[str, dict[str, str]] = {}

    requirement_items = [
        item
        for item in items
        if item.get("artifact_type") == "requirement_split"
        or str(item["source_path"]).endswith("requirement-split.v1.json")
    ]
    if requirement_split is not None or requirement_items:
        if len(requirement_items) != 1:
            raise SdlcError("需求拆分没有唯一 artifact manifest 条目。", exit_code=1)
        split_item = requirement_items[0]
        split_path = _strict_file(
            Path(source_root), split_item["source_path"], label="需求拆分 source_path"
        )
        disk_split = _strict_json(split_path, "需求拆分")
        if not isinstance(disk_split, Mapping):
            raise SdlcError("需求拆分顶层必须是对象。", exit_code=1)
        selected_split = requirement_split or disk_split
        if canonical_sha256(selected_split) != canonical_sha256(disk_split):
            raise SdlcError("传入的需求拆分与真实归档输入不一致。", exit_code=1)
        if str(selected_split.get("draft_id") or "") != str(
            current_index.get("draft_id") or ""
        ):
            raise SdlcError("需求拆分与当前 artifact-index.v1 的 DRAFT 编号不一致。", exit_code=1)
        if not isinstance(requirement_mapping, Mapping):
            raise SdlcError("生成 FR、GR、AC 引用必须提供稳定编号映射。", exit_code=1)
        current_mapping = _current_requirement_mapping(
            Path(source_root),
            selected_split,
            current_index,
        )
        supplied_mapping = {
            str(key): str(value)
            for key, value in requirement_mapping.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if len(supplied_mapping) != len(requirement_mapping) or canonical_sha256(
            supplied_mapping
        ) != canonical_sha256(current_mapping):
            raise SdlcError(
                "调用方需求编号映射与当前真实需求导入回执不一致。",
                exit_code=1,
            )
        _requirement_entries(
            entries,
            display,
            split_item,
            selected_split,
            current_mapping,
            archive=archive,
        )

    selected_design_references = list(design_references)
    if any(not isinstance(item, Mapping) for item in selected_design_references):
        raise SdlcError("DES 结构化对象必须是对象。", exit_code=1)
    manifest_design_references = [
        item
        for item in items
        if item.get("artifact_type") == "design_reference"
        and isinstance(item.get("business_id"), str)
        and re.fullmatch(r"DES-[0-9]{3,}", str(item["business_id"]))
    ]
    if not selected_design_references:
        for item in manifest_design_references:
            path = _strict_file(
                Path(source_root),
                item["source_path"],
                label=f"{item['business_id']} source_path",
            )
            document = _strict_json(path, f"{item['business_id']} 引用记录")
            if not isinstance(document, Mapping):
                raise SdlcError(
                    f"{item['business_id']} 引用记录顶层必须是对象。",
                    exit_code=1,
                )
            selected_design_references.append(document)
    elif {
        str(item.get("design_id") or "") for item in selected_design_references
    } != {str(item["business_id"]) for item in manifest_design_references}:
        raise SdlcError("DES 结构化对象与正式归档清单不完全一致。", exit_code=1)

    _design_entries(
        entries,
        display,
        items,
        selected_design_references,
        source_root=Path(source_root),
        archive=archive,
    )

    selected_design_artifacts = list(design_artifacts)
    if any(not isinstance(item, Mapping) for item in selected_design_artifacts):
        raise SdlcError("设计模块结构化对象必须是对象。", exit_code=1)
    manifest_design_artifacts = [
        item
        for item in items
        if item.get("artifact_type") in {"design_artifact", "design_artifact_json"}
        and isinstance(item.get("business_id"), str)
        and re.fullmatch(
            r"(?:DATA|API|PAGE|COMP|SAFE|DEPLOY|FIELD|SPEC)-[0-9]{3,}",
            str(item["business_id"]),
        )
    ]
    if not selected_design_artifacts:
        for item in manifest_design_artifacts:
            path = _strict_file(
                Path(source_root),
                item["source_path"],
                label=f"{item['business_id']} source_path",
            )
            document = _strict_json(path, f"{item['business_id']} 模块文件")
            if not isinstance(document, Mapping):
                raise SdlcError(
                    f"{item['business_id']} 模块文件顶层必须是对象。",
                    exit_code=1,
                )
            selected_design_artifacts.append(document)
    elif {
        str(item.get("artifact_id") or "") for item in selected_design_artifacts
    } != {str(item["business_id"]) for item in manifest_design_artifacts}:
        raise SdlcError("设计模块结构化对象与正式归档清单不完全一致。", exit_code=1)

    _module_entries(
        entries,
        display,
        items,
        selected_design_artifacts,
        source_root=Path(source_root),
        archive=archive,
    )
    _material_entries(
        entries,
        display,
        items,
        material_references,
        archive=archive,
    )
    document: dict[str, object] = {
        "schema_version": REFERENCE_INDEX_SCHEMA,
        "requirement_id": clean_requirement_id,
        "entries": {key: entries[key] for key in sorted(entries)},
    }
    if display:
        document["display"] = {key: display[key] for key in sorted(display)}
    validation_root = Path(archive_root) if archive else Path(source_root)
    return validate_reference_index_document(validation_root, document)


def _strict_reference_paths(root: Path, reference: Mapping[str, object]) -> None:
    _strict_file(root, reference.get("path"), label="正式引用 path")
    locator = reference.get("locator")
    if isinstance(locator, Mapping) and locator.get("kind") == "design_node":
        _strict_file(
            root,
            locator.get("node_index_path"),
            label="design_node node_index_path",
        )


def validate_reference_index_document(
    root: Path, document: Mapping[str, object]
) -> dict[str, object]:
    """只按稳定编号、版本化结构和真实定位检查，不读取展示标题做业务判断。"""

    normalized = deepcopy(dict(document))
    validate_schema_document(normalized, schema_name=REFERENCE_INDEX_SCHEMA)
    entries = normalized["entries"]
    if not isinstance(entries, Mapping):
        raise SdlcError("reference-index.v1 entries 必须是对象。", exit_code=1)
    display = normalized.get("display", {})
    if isinstance(display, Mapping) and not set(display) <= set(entries):
        raise SdlcError("引用索引的展示项必须对应真实稳定编号。", exit_code=1)
    for reference_id in sorted(entries):
        if _REFERENCE_ID_PATTERN.fullmatch(str(reference_id)) is None:
            raise SdlcError(f"正式引用编号无效：{reference_id}。", exit_code=1)
        reference = entries[reference_id]
        if not isinstance(reference, Mapping):
            raise SdlcError(f"{reference_id} 的引用必须是对象。", exit_code=1)
        # 每一项直接走 T-001 的完整 reference-locator.v1，索引本身不复制字段规则。
        _strict_reference_paths(Path(root), reference)
        validate_reference(Path(root), reference)
    return normalized


def validate_reference_index_file(root: Path, index_path: Path) -> dict[str, object]:
    """读取正式索引并检查结构、路径、定位及两个完整文件哈希。"""

    path = Path(index_path)
    if path.is_symlink() or not path.is_file():
        raise SdlcError("reference-index.v1.json 必须是普通文件。", exit_code=1)
    document = _strict_json(path, "reference-index.v1")
    if not isinstance(document, Mapping):
        raise SdlcError("reference-index.v1 顶层必须是对象。", exit_code=1)
    validated = validate_reference_index_document(Path(root), document)
    _require_formal_archive_paths(validated)
    return validated


def _require_formal_archive_paths(document: Mapping[str, object]) -> None:
    entries = document.get("entries")
    if not isinstance(entries, Mapping):
        raise SdlcError("reference-index.v1 entries 必须是对象。", exit_code=1)
    for reference_id, reference in entries.items():
        if not isinstance(reference, Mapping):
            continue
        path = str(reference.get("path") or "")
        if not path.startswith("original/"):
            raise SdlcError(
                f"正式引用不能继续使用 DRAFT source_path：{reference_id}。",
                exit_code=1,
            )
        locator = reference.get("locator")
        if (
            isinstance(locator, Mapping)
            and locator.get("kind") == "design_node"
            and not str(locator.get("node_index_path") or "").startswith("original/")
        ):
            raise SdlcError(
                f"正式 design_node 不能继续使用 DRAFT node_index_path：{reference_id}。",
                exit_code=1,
            )


def write_reference_index_file(
    root: Path,
    output_path: Path,
    document: Mapping[str, object],
) -> Path:
    """先完成全部只读校验再原子替换，拒绝时不留下正式文件或临时文件。"""

    validated = validate_reference_index_document(Path(root), document)
    _require_formal_archive_paths(validated)
    output = Path(output_path)
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink():
        raise SdlcError("引用索引目标目录不存在或不是普通目录。", exit_code=1)
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("引用索引目标目录越过允许根目录。", exit_code=1) from exc
    current = Path(root)
    try:
        relative_parent = resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise SdlcError("引用索引目标目录越过允许根目录。", exit_code=1) from exc
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError("引用索引目标目录不能经过符号链接。", exit_code=1)
    if output.is_symlink():
        raise SdlcError("引用索引目标文件不能是符号链接。", exit_code=1)

    content = canonical_json_text(validated).encode("utf-8")
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        temporary = None
    except OSError as exc:
        raise SdlcError("reference-index.v1.json 写入失败。", exit_code=1) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return output


def reference_index_issues(root: Path, index_path: Path) -> list[str]:
    """doctor 使用的只读入口，只返回问题，不改定位、编号、事件或状态。"""

    try:
        validate_reference_index_file(root, index_path)
    except SdlcError as exc:
        return [exc.message]
    return []


__all__ = [
    "REFERENCE_INDEX_PATH",
    "REFERENCE_INDEX_SCHEMA",
    "build_reference_index_document",
    "reference_index_issues",
    "validate_reference_index_document",
    "validate_reference_index_file",
    "write_reference_index_file",
]
