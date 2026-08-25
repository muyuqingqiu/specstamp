from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hmac
import json
import math
from pathlib import Path
from typing import Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import resolve_project_path
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)


REFERENCE_LOCATOR_SCHEMA = "reference-locator.v1"
DESIGN_NODE_INDEX_SCHEMA = "design-node-index.v1"
SUPPORTED_LOCATOR_KINDS = frozenset(
    {"text_range", "byte_range", "json_pointer", "pdf_region", "design_node", "whole_file"}
)
_COMMON_LOCATOR_FIELDS = {"kind", "type", "display_heading"}


@dataclass(frozen=True)
class ReferenceMatch:
    """精确引用命中结果；只返回已校验内容，不写事件、状态或业务文件。"""

    path: Path
    kind: str
    file_sha256: str
    content: object | None = None
    fragment_sha256: str | None = None


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SdlcError(f"{label}必须是 JSON 对象。")
    if any(not isinstance(key, str) for key in value):
        raise SdlcError(f"{label}的字段名必须是字符串。")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SdlcError(f"{label}必须是非空字符串。")
    return value


def _require_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise SdlcError(f"{label}必须是整数。")
    return value


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SdlcError(f"{label}必须是有限数字。")
    result = float(value)
    if not math.isfinite(result):
        raise SdlcError(f"{label}必须是有限数字。")
    return result


def _require_sha256(value: object, label: str) -> str:
    digest = _require_string(value, label)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise SdlcError(f"{label}必须是64位小写十六进制 SHA-256。")
    return digest


def _reject_unknown_fields(locator: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(locator) - _COMMON_LOCATOR_FIELDS - allowed)
    if unknown:
        raise SdlcError(f"定位器包含未知字段：{', '.join(unknown)}。")


def _locator_kind(locator: Mapping[str, object]) -> str:
    has_kind = "kind" in locator
    has_type = "type" in locator
    if has_kind == has_type:
        raise SdlcError("定位器必须且只能提供 kind 或 type 其中一个字段。")
    kind = _require_string(locator.get("kind") if has_kind else locator.get("type"), "定位类型")
    if kind not in SUPPORTED_LOCATOR_KINDS:
        raise SdlcError(f"不支持的定位类型：{kind}。")
    display_heading = locator.get("display_heading")
    if display_heading is not None:
        _require_string(display_heading, "display_heading")
    return kind


def _paired_range(
    locator: Mapping[str, object],
    *,
    primary: tuple[str, str],
    compatible: tuple[str, str],
    label: str,
) -> tuple[int, int]:
    primary_present = any(field in locator for field in primary)
    compatible_present = any(field in locator for field in compatible)
    if primary_present and compatible_present:
        raise SdlcError(f"{label}不能混用两套字段名。")
    selected = primary if primary_present else compatible
    if not all(field in locator for field in selected):
        raise SdlcError(f"{label}必须同时提供 {selected[0]} 和 {selected[1]}。")
    return (
        _require_integer(locator[selected[0]], selected[0]),
        _require_integer(locator[selected[1]], selected[1]),
    )


def _verify_fragment(actual: bytes, expected: object) -> str:
    expected_hash = _require_sha256(expected, "fragment_sha256")
    actual_hash = sha256_bytes(actual)
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise SdlcError("引用片段内容已经变化，fragment_sha256 不一致。")
    return actual_hash


def _locate_text_range(path: Path, locator: Mapping[str, object], file_sha256: str) -> ReferenceMatch:
    _reject_unknown_fields(
        locator,
        {"line_start", "line_end", "start_line", "end_line", "fragment_sha256"},
    )
    line_start, line_end = _paired_range(
        locator,
        primary=("line_start", "line_end"),
        compatible=("start_line", "end_line"),
        label="文本行范围",
    )
    if line_start < 1 or line_end < line_start:
        raise SdlcError("文本行范围必须从1开始，并且结束行不能小于开始行。")
    try:
        raw_content = path.read_bytes()
        text_content = raw_content.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SdlcError(f"文本引用文件不是有效 UTF-8：{path.name}。") from exc
    # 先按 Unicode 文本分行，再编码回原字节，可以同时保留 CRLF 和 Unicode 换行符。
    lines = [line.encode("utf-8") for line in text_content.splitlines(keepends=True)]
    if line_end > len(lines):
        raise SdlcError(f"文本行范围未命中：文件只有 {len(lines)} 行。")
    fragment = b"".join(lines[line_start - 1 : line_end])
    fragment_sha256 = _verify_fragment(fragment, locator.get("fragment_sha256"))
    return ReferenceMatch(
        path=path,
        kind="text_range",
        file_sha256=file_sha256,
        content=fragment.decode("utf-8"),
        fragment_sha256=fragment_sha256,
    )


def _locate_byte_range(path: Path, locator: Mapping[str, object], file_sha256: str) -> ReferenceMatch:
    _reject_unknown_fields(
        locator,
        {"byte_start", "byte_end", "start_byte", "end_byte", "fragment_sha256"},
    )
    byte_start, byte_end = _paired_range(
        locator,
        primary=("byte_start", "byte_end"),
        compatible=("start_byte", "end_byte"),
        label="字节范围",
    )
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise SdlcError(f"无法读取字节引用文件：{path.name}。") from exc
    # 字节范围采用零开始、结束位置不包含在内的固定规则，避免片段边界产生两种解释。
    if byte_start < 0 or byte_end <= byte_start or byte_end > file_size:
        raise SdlcError(f"字节范围未命中：有效范围是 0 到 {file_size}，结束位置不包含在内。")
    try:
        with path.open("rb") as handle:
            handle.seek(byte_start)
            fragment = handle.read(byte_end - byte_start)
    except OSError as exc:
        raise SdlcError(f"无法读取字节引用文件：{path.name}。") from exc
    if len(fragment) != byte_end - byte_start:
        raise SdlcError("字节范围未完整命中文件内容。")
    fragment_sha256 = _verify_fragment(fragment, locator.get("fragment_sha256"))
    return ReferenceMatch(
        path=path,
        kind="byte_range",
        file_sha256=file_sha256,
        content=fragment,
        fragment_sha256=fragment_sha256,
    )


def _decode_pointer_token(token: str, pointer: str) -> str:
    index = 0
    while index < len(token):
        if token[index] == "~":
            if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                raise SdlcError(f"JSON Pointer 包含无效转义：{pointer}。")
            index += 2
        else:
            index += 1
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_json_pointer(document: object, pointer: str) -> object:
    # 现有正式设计把“/”写成整份 JSON 的根定位；同时兼容标准 JSON Pointer 的空字符串根定位。
    if pointer in {"", "/"}:
        return document
    if not pointer.startswith("/"):
        raise SdlcError("JSON Pointer 必须为空字符串、/ 或以 / 开头。")
    current = document
    for raw_token in pointer[1:].split("/"):
        token = _decode_pointer_token(raw_token, pointer)
        if isinstance(current, dict):
            if token not in current:
                raise SdlcError(f"JSON Pointer 未命中对象字段：{pointer}。")
            current = current[token]
            continue
        if isinstance(current, list):
            if token == "-" or not token.isdigit():
                raise SdlcError(f"JSON Pointer 未命中数组下标：{pointer}。")
            index = int(token)
            if str(index) != token or index >= len(current):
                raise SdlcError(f"JSON Pointer 未命中数组下标：{pointer}。")
            current = current[index]
            continue
        raise SdlcError(f"JSON Pointer 穿过了非容器节点：{pointer}。")
    return current


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"JSON 引用文件包含重复字段：{key}。")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"JSON 引用文件包含非标准数字：{value}。")


def _locate_json_pointer(path: Path, locator: Mapping[str, object], file_sha256: str) -> ReferenceMatch:
    _reject_unknown_fields(locator, {"value", "pointer"})
    provided = [field for field in ("value", "pointer") if field in locator]
    if len(provided) != 1:
        raise SdlcError("JSON Pointer 必须且只能提供 value 或 pointer 其中一个字段。")
    pointer = locator[provided[0]]
    if not isinstance(pointer, str):
        raise SdlcError("JSON Pointer 必须是字符串。")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"JSON 引用文件无法解析：{path.name}。") from exc
    node = _resolve_json_pointer(document, pointer)
    return ReferenceMatch(
        path=path,
        kind="json_pointer",
        file_sha256=file_sha256,
        content=deepcopy(node),
        fragment_sha256=canonical_sha256(node),
    )


def _pdf_region(
    locator: Mapping[str, object],
    page_box: tuple[float, float, float, float],
) -> dict[str, float] | None:
    nested_region = locator.get("region")
    top_level_fields = {field for field in ("x", "y", "width", "height") if field in locator}
    if nested_region is not None and top_level_fields:
        raise SdlcError("PDF 区域不能同时使用 region 和顶层坐标字段。")
    if nested_region is None and not top_level_fields:
        return None
    region = _require_mapping(nested_region, "PDF region") if nested_region is not None else locator
    required = {"x", "y", "width", "height"}
    if not required <= set(region):
        raise SdlcError("PDF 区域必须同时提供 x、y、width 和 height。")
    if nested_region is not None:
        unknown = sorted(set(region) - required)
        if unknown:
            raise SdlcError(f"PDF region 包含未知字段：{', '.join(unknown)}。")
    normalized = {field: _require_number(region[field], f"PDF {field}") for field in required}
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        raise SdlcError("PDF width 和 height 必须大于0。")
    left, bottom, right, top = page_box
    if (
        normalized["x"] < left
        or normalized["y"] < bottom
        or normalized["x"] + normalized["width"] > right
        or normalized["y"] + normalized["height"] > top
    ):
        raise SdlcError("PDF 区域超出页面边界。")
    return normalized


def _locate_pdf_region(path: Path, locator: Mapping[str, object], file_sha256: str) -> ReferenceMatch:
    _reject_unknown_fields(locator, {"page", "region", "x", "y", "width", "height"})
    page_number = _require_integer(locator.get("page"), "PDF page")
    if page_number < 1:
        raise SdlcError("PDF page 必须从1开始。")
    try:
        from pypdf import PdfReader

        reader = PdfReader(path, strict=True)
        if page_number > len(reader.pages):
            raise SdlcError(f"PDF 页码未命中：文件只有 {len(reader.pages)} 页。")
        media_box = reader.pages[page_number - 1].mediabox
        page_box = tuple(
            float(value)
            for value in (media_box.left, media_box.bottom, media_box.right, media_box.top)
        )
    except SdlcError:
        raise
    except Exception as exc:
        # pypdf 会按损坏类型抛出多个解析异常，这里统一收口为稳定的只读校验失败。
        raise SdlcError(f"PDF 引用文件无法解析：{path.name}。") from exc
    region = _pdf_region(locator, page_box)
    return ReferenceMatch(
        path=path,
        kind="pdf_region",
        file_sha256=file_sha256,
        content={"page": page_number, "region": region},
    )


def _load_strict_json(path: Path, label: str) -> object:
    """索引也必须是标准 JSON，避免重复字段或 NaN 被解析器静默接受。"""

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法解析：{path.name}。") from exc


def _locate_design_node(
    project_root: Path,
    path: Path,
    relative_path: str,
    locator: Mapping[str, object],
    file_sha256: str,
) -> ReferenceMatch:
    _reject_unknown_fields(
        locator,
        {
            "page_id",
            "node_id",
            "node_index_path",
            "node_index_sha256",
            "node_index_schema",
        },
    )
    page_id = _require_string(locator.get("page_id"), "设计稿 page_id")
    node_id = _require_string(locator.get("node_id"), "设计稿 node_id")
    node_index_path = _require_string(locator.get("node_index_path"), "node_index_path")
    expected_index_sha256 = _require_sha256(
        locator.get("node_index_sha256"),
        "node_index_sha256",
    )
    node_index_schema = _require_string(locator.get("node_index_schema"), "node_index_schema")
    if node_index_schema != DESIGN_NODE_INDEX_SCHEMA:
        raise SdlcError(f"设计节点索引版本不受支持：{node_index_schema}。")

    index_path = resolve_project_path(project_root, node_index_path, must_exist=True)
    if not index_path.is_file():
        raise SdlcError(f"设计节点索引路径不是普通文件：{node_index_path}。")
    actual_index_sha256 = sha256_file(index_path)
    if not hmac.compare_digest(actual_index_sha256, expected_index_sha256):
        raise SdlcError("设计节点索引内容已经变化，node_index_sha256 不一致。")

    index_document = _load_strict_json(index_path, "设计节点索引")
    validate_schema_document(index_document, schema_name=node_index_schema)
    index = _require_mapping(index_document, "设计节点索引")
    if index["design_path"] != relative_path:
        raise SdlcError("设计节点索引绑定的 design_path 与主设计文件路径不一致。")
    if not hmac.compare_digest(str(index["design_sha256"]), file_sha256):
        raise SdlcError("设计节点索引绑定的 design_sha256 与主设计文件哈希不一致。")

    # 索引中的编号必须先整体保持唯一，再判断目标是否命中，避免同一引用得到多个候选结果。
    page_ids: set[str] = set()
    matched_page: Mapping[str, object] | None = None
    for page_value in index["pages"]:  # type: ignore[union-attr]
        page = _require_mapping(page_value, "设计节点索引 page")
        current_page_id = _require_string(page.get("page_id"), "设计节点索引 page_id")
        if current_page_id in page_ids:
            raise SdlcError(f"设计节点索引包含重复 page_id：{current_page_id}。")
        page_ids.add(current_page_id)

        node_ids: set[str] = set()
        for node_value in page["nodes"]:  # type: ignore[union-attr]
            node = _require_mapping(node_value, "设计节点索引 node")
            current_node_id = _require_string(node.get("node_id"), "设计节点索引 node_id")
            if current_node_id in node_ids:
                raise SdlcError(
                    f"设计节点索引的 page_id={current_page_id} 包含重复 node_id：{current_node_id}。"
                )
            node_ids.add(current_node_id)
        if current_page_id == page_id:
            matched_page = page

    if matched_page is None:
        raise SdlcError(f"设计节点索引未命中 page_id：{page_id}。")
    if not any(node["node_id"] == node_id for node in matched_page["nodes"]):  # type: ignore[index]
        raise SdlcError(f"设计节点索引未命中 node_id：{node_id}。")

    return ReferenceMatch(
        path=path,
        kind="design_node",
        file_sha256=file_sha256,
        content={
            "page_id": page_id,
            "node_id": node_id,
            "node_index_path": node_index_path,
        },
    )


def _locate_whole_file(path: Path, locator: Mapping[str, object], file_sha256: str) -> ReferenceMatch:
    _reject_unknown_fields(locator, set())
    return ReferenceMatch(path=path, kind="whole_file", file_sha256=file_sha256)


def locate_reference(project_root: Path, reference: Mapping[str, object]) -> ReferenceMatch:
    """校验引用路径、文件哈希和具体定位，并返回唯一命中结果。"""

    reference_data = _require_mapping(reference, "引用")
    schema_version = reference_data.get("schema_version")
    if schema_version is not None and schema_version != REFERENCE_LOCATOR_SCHEMA:
        raise SdlcError(f"引用定位合同版本不受支持：{schema_version}。")
    validate_schema_document(reference_data, schema_name=REFERENCE_LOCATOR_SCHEMA)
    relative_path = _require_string(reference_data.get("path"), "引用 path")
    expected_file_sha256 = _require_sha256(reference_data.get("sha256"), "引用 sha256")
    locator = _require_mapping(reference_data.get("locator"), "locator")
    kind = _locator_kind(locator)

    path = resolve_project_path(project_root, relative_path, must_exist=True)
    if not path.is_file():
        raise SdlcError(f"引用路径不是普通文件：{relative_path}。")
    actual_file_sha256 = sha256_file(path)
    if not hmac.compare_digest(actual_file_sha256, expected_file_sha256):
        raise SdlcError("引用文件内容已经变化，sha256 不一致。")

    handlers = {
        "text_range": _locate_text_range,
        "byte_range": _locate_byte_range,
        "json_pointer": _locate_json_pointer,
        "pdf_region": _locate_pdf_region,
        "whole_file": _locate_whole_file,
    }
    if kind == "design_node":
        return _locate_design_node(
            project_root,
            path,
            relative_path,
            locator,
            actual_file_sha256,
        )
    return handlers[kind](path, locator, actual_file_sha256)


def validate_reference(project_root: Path, reference: Mapping[str, object]) -> ReferenceMatch:
    """公共校验入口，与定位入口返回同一个经过校验的结果。"""

    return locate_reference(project_root, reference)


__all__ = [
    "DESIGN_NODE_INDEX_SCHEMA",
    "REFERENCE_LOCATOR_SCHEMA",
    "SUPPORTED_LOCATOR_KINDS",
    "ReferenceMatch",
    "locate_reference",
    "validate_reference",
]
