"""统一读取仓库内版本化 JSON Schema。"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from codex_sdlc.core.errors import SdlcError


SCHEMA_DIRECTORY = Path(__file__).resolve().parent
_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@lru_cache(maxsize=1)
def _schema_index() -> dict[str, dict[str, Any]]:
    """一次建立唯一索引，避免不同调用方各自猜测 Schema 路径和版本。"""

    index: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIRECTORY.glob("*.json")):
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(SCHEMA_DIRECTORY)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise SdlcError(f"Schema 文件不在统一目录内：{path.name}。") from exc
        try:
            document = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SdlcError(f"Schema 文件无法读取：{path.name}。") from exc
        if not isinstance(document, dict):
            raise SdlcError(f"Schema 顶层必须是对象：{path.name}。")
        schema_id = document.get("$id")
        if not isinstance(schema_id, str) or not _SCHEMA_NAME_PATTERN.fullmatch(schema_id):
            raise SdlcError(f"Schema 缺少合法的 $id：{path.name}。")

        # 文件名、版本名和 $id 都可作为明确入口；不同文件出现重名时直接拒绝，不能静默选择。
        for name in (path.name, path.stem, schema_id):
            if name in index and index[name] is not document:
                raise SdlcError(f"Schema 名称不唯一：{name}。")
            index[name] = document
    return index


def available_schema_ids() -> tuple[str, ...]:
    """返回所有正式 $id，不暴露目录扫描和文件名约定。"""

    return tuple(sorted({str(item["$id"]) for item in _schema_index().values()}))


def load_schema(schema_name: str) -> dict[str, Any]:
    """按正式 $id、版本名或文件名加载 Schema，并返回独立副本。"""

    clean_name = str(schema_name).strip()
    if not _SCHEMA_NAME_PATTERN.fullmatch(clean_name):
        raise SdlcError("Schema 名称只能包含字母、数字、点、下划线和短横线。")
    document = _schema_index().get(clean_name)
    if document is None:
        raise SdlcError(f"Schema 不存在或版本不受支持：{clean_name}。")
    # 调用方可以自由读取和加工副本，但不能污染后续调用共享的合同定义。
    return deepcopy(document)
