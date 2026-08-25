from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.schemas import available_schema_ids, load_schema


# Schema 通过这个扩展字段逐项声明不参与内容哈希的 JSON Pointer。
# 调用方不能临时传入排除字段，避免同一份产物在不同位置算出不同哈希。
HASH_EXCLUDE_POINTERS_KEY = "x-codex-sdlc-hash-exclude"


def _validate_json_value(value: object, active_containers: set[int]) -> None:
    """拒绝会被 json.dumps 暗中改型的 Python 对象，保证输入本身就是 JSON 数据。"""

    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if not isinstance(value, (dict, list)):
        raise SdlcError("内容不是可生成规范 JSON 的有效数据。")

    identity = id(value)
    if identity in active_containers:
        raise SdlcError("内容不是可生成规范 JSON 的有效数据。")
    active_containers.add(identity)
    try:
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise SdlcError("规范 JSON 的对象字段名必须是字符串。")
            for item in value.values():
                _validate_json_value(item, active_containers)
        else:
            for item in value:
                _validate_json_value(item, active_containers)
    finally:
        active_containers.remove(identity)


def canonical_json_bytes(value: object) -> bytes:
    """生成排序、紧凑、保留 Unicode 且拒绝非标准数字的规范 JSON。"""

    try:
        _validate_json_value(value, set())
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8")
    except SdlcError:
        raise
    except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
        raise SdlcError("内容不是可生成规范 JSON 的有效数据。") from exc


def canonical_json_text(value: object) -> str:
    """返回适合写盘的规范 JSON，并固定使用一个结尾换行。"""

    return canonical_json_bytes(value).decode("utf-8") + "\n"


def sha256_bytes(value: bytes) -> str:
    """计算完整字节内容的 SHA-256。"""

    if not isinstance(value, bytes):
        raise SdlcError("SHA-256 输入必须是 bytes。")
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """分块读取真实文件，避免大资料一次性进入内存。"""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError) as exc:
        raise SdlcError(f"无法读取待计算哈希的文件：{path}。") from exc
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    """对完整结构计算规范 JSON SHA-256，不做任何隐式字段排除。"""

    return sha256_bytes(canonical_json_bytes(value))


def validate_schema_document(document: object, *, schema_name: str) -> None:
    """使用唯一目录中的版本化 Schema 校验 JSON 数据，不在调用方重复手写结构规则。"""

    # Schema 校验器会接受部分 Python 扩展值；先走规范 JSON 入口，确保 NaN 和非字符串键统一拒绝。
    canonical_json_bytes(document)
    schema = load_schema(schema_name)
    try:
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    except SchemaError as exc:
        raise SdlcError(f"Schema 自身不符合 JSON Schema 2020-12：{schema_name}。") from exc
    if not errors:
        return

    # oneOf 的顶层错误只会说“没有命中”，真正原因在 context 中；优先报告最深处的具体错误。
    candidates = []
    pending = list(errors)
    while pending:
        candidate = pending.pop()
        if candidate.context:
            pending.extend(candidate.context)
        else:
            candidates.append(candidate)
    validator_priority = {
        "minimum": 0,
        "exclusiveMinimum": 0,
        "pattern": 1,
        "type": 2,
        "required": 3,
        "additionalProperties": 4,
        "minLength": 5,
        "const": 6,
        "oneOf": 7,
    }
    error = min(
        candidates or errors,
        key=lambda item: (
            -len(item.absolute_path),
            validator_priority.get(str(item.validator), 99),
            tuple(str(part) for part in item.absolute_path),
            tuple(str(part) for part in item.absolute_schema_path),
        ),
    )
    location = "/" + "/".join(str(part) for part in error.absolute_path)
    if location == "/":
        location = "根对象"
    reasons = {
        "required": "缺少必填字段",
        "additionalProperties": "包含未知字段",
        "const": "固定值或版本不正确",
        "oneOf": "字段组合不符合合同",
        "type": "字段类型不正确",
        "pattern": "字段格式不正确",
        "minLength": "字段不能为空",
        "minimum": "字段数值超出范围",
        "exclusiveMinimum": "字段数值超出范围",
    }
    reason = reasons.get(str(error.validator), "字段不符合合同")
    if (
        error.validator == "pattern"
        and error.absolute_path
        and str(error.absolute_path[-1]).endswith("sha256")
    ):
        reason = "必须是64位小写十六进制 SHA-256"
    raise SdlcError(f"{schema_name} Schema 校验失败：{location} {reason}。")


def _decode_json_pointer(pointer: str) -> tuple[str, ...]:
    if not pointer.startswith("/") or pointer == "/":
        raise SdlcError(f"哈希排除项必须是指向具体字段的 JSON Pointer：{pointer}。")
    tokens: list[str] = []
    for raw_token in pointer[1:].split("/"):
        index = 0
        while index < len(raw_token):
            if raw_token[index] == "~":
                if index + 1 >= len(raw_token) or raw_token[index + 1] not in {"0", "1"}:
                    raise SdlcError(f"哈希排除项包含无效 JSON Pointer 转义：{pointer}。")
                index += 2
            else:
                index += 1
        tokens.append(raw_token.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


def hash_excluded_pointers(schema_name: str) -> tuple[str, ...]:
    """只从 Schema 读取排除规则，并在使用前检查规则是否明确且无重复。"""

    schema = load_schema(schema_name)
    raw_pointers = schema.get(HASH_EXCLUDE_POINTERS_KEY, [])
    if not isinstance(raw_pointers, list) or any(not isinstance(item, str) for item in raw_pointers):
        raise SdlcError(f"Schema 的 {HASH_EXCLUDE_POINTERS_KEY} 必须是字符串数组。")
    pointers = tuple(raw_pointers)
    if len(set(pointers)) != len(pointers):
        raise SdlcError(f"Schema 的 {HASH_EXCLUDE_POINTERS_KEY} 不能包含重复项。")
    for pointer in pointers:
        _decode_json_pointer(pointer)
    return pointers


def _remove_pointer(value: object, pointer: str) -> None:
    tokens = _decode_json_pointer(pointer)
    current: object = value
    for token in tokens[:-1]:
        if isinstance(current, dict):
            if token not in current:
                return
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return
            current = current[int(token)]
            continue
        return

    terminal = tokens[-1]
    if isinstance(current, dict):
        current.pop(terminal, None)
    elif isinstance(current, list) and terminal.isdigit() and int(terminal) < len(current):
        del current[int(terminal)]


def contract_hash_payload(document: object, *, schema_name: str) -> object:
    """按指定 Schema 的显式规则生成哈希正文，保留原对象不变。"""

    payload = deepcopy(document)
    for pointer in hash_excluded_pointers(schema_name):
        _remove_pointer(payload, pointer)
    return payload


def contract_sha256(document: object, *, schema_name: str) -> str:
    """使用版本化 Schema 中的排除规则计算结构化合同哈希。"""

    return canonical_sha256(contract_hash_payload(document, schema_name=schema_name))


__all__ = [
    "HASH_EXCLUDE_POINTERS_KEY",
    "available_schema_ids",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_sha256",
    "contract_hash_payload",
    "contract_sha256",
    "hash_excluded_pointers",
    "load_schema",
    "sha256_bytes",
    "sha256_file",
    "validate_schema_document",
]
