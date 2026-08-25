from __future__ import annotations

from dataclasses import dataclass
import heapq
import re
from typing import Iterable, Mapping, Sequence

from codex_sdlc.core.errors import SdlcError


CLIENT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TEMPORARY_REFERENCE_PATTERN = re.compile(
    r"^@client:(?P<client_key>[a-z0-9][a-z0-9._-]{0,127})$"
)
FORMAL_ID_PATTERN = re.compile(r"^(?P<prefix>[A-Z][A-Z0-9]*)-(?P<number>[0-9]+)$")

# 这些前缀来自当前正式设计和已经存在的确定性编号入口。
# 公共分配器拒绝临时发明前缀，后续增加正式编号类型时必须同步更新 Schema 和这里。
SUPPORTED_ID_PREFIXES = frozenset(
    {
        "AC",
        "API",
        "ART",
        "CAP",
        "CHG",
        "COMP",
        "DATA",
        "DEC",
        "DEPLOY",
        "DES",
        "DF",
        "DRAFT",
        "FIELD",
        "FR",
        "GR",
        "GRILL",
        "MAT",
        "PAGE",
        "REQ",
        "REV",
        "RF",
        "SAFE",
        "SESSION",
        "SPEC",
        "SRC",
        "T",
        "TC",
        "VRF",
    }
)


@dataclass(frozen=True)
class AllocationObject:
    """一个待分配正式编号的对象，只保存确定性的临时键、前缀和显式依赖。"""

    client_key: str
    id_prefix: str
    depends_on: tuple[str, ...]


def _validate_object(item: AllocationObject) -> None:
    if not isinstance(item, AllocationObject):
        raise SdlcError("编号分配对象必须使用 AllocationObject。")
    if not CLIENT_KEY_PATTERN.fullmatch(item.client_key):
        raise SdlcError(f"client_key 格式不正确：{item.client_key}。")
    if item.id_prefix not in SUPPORTED_ID_PREFIXES:
        raise SdlcError(f"编号前缀不受支持：{item.id_prefix}。")
    if not isinstance(item.depends_on, tuple):
        raise SdlcError(f"{item.client_key} 的 depends_on 必须是元组。")
    if len(set(item.depends_on)) != len(item.depends_on):
        raise SdlcError(f"{item.client_key} 的 depends_on 不能包含重复引用。")
    for reference in item.depends_on:
        if not isinstance(reference, str) or not TEMPORARY_REFERENCE_PATTERN.fullmatch(reference):
            raise SdlcError(
                f"{item.client_key} 的依赖必须使用完整的 @client:<client_key> 引用。"
            )


def build_allocation_order(objects: Sequence[AllocationObject]) -> tuple[AllocationObject, ...]:
    """使用稳定的拓扑顺序排列对象，依赖相同时按 client_key 排序。"""

    if not objects:
        raise SdlcError("编号分配对象不能为空。")
    by_key: dict[str, AllocationObject] = {}
    for item in objects:
        _validate_object(item)
        if item.client_key in by_key:
            raise SdlcError(f"client_key 重复：{item.client_key}。")
        by_key[item.client_key] = item

    incoming: dict[str, int] = {client_key: 0 for client_key in by_key}
    dependents: dict[str, list[str]] = {client_key: [] for client_key in by_key}
    for item in by_key.values():
        for reference in item.depends_on:
            match = TEMPORARY_REFERENCE_PATTERN.fullmatch(reference)
            dependency_key = match.group("client_key") if match is not None else ""
            if dependency_key not in by_key:
                raise SdlcError(
                    f"临时引用跨包或悬空：{reference}，当前导入包没有对应 client_key。"
                )
            incoming[item.client_key] += 1
            dependents[dependency_key].append(item.client_key)

    ready = [client_key for client_key, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[AllocationObject] = []
    while ready:
        client_key = heapq.heappop(ready)
        ordered.append(by_key[client_key])
        for dependent_key in sorted(dependents[client_key]):
            incoming[dependent_key] -= 1
            if incoming[dependent_key] == 0:
                heapq.heappush(ready, dependent_key)

    if len(ordered) != len(by_key):
        cycle_keys = sorted(client_key for client_key, count in incoming.items() if count > 0)
        raise SdlcError(f"临时引用存在依赖环：{', '.join(cycle_keys)}。")
    return tuple(ordered)


def allocate_stable_ids(
    objects: Sequence[AllocationObject],
    *,
    existing_ids: Iterable[str],
    width: int = 3,
) -> dict[str, str]:
    """按拓扑顺序计算映射；函数本身不写盘，失败不会占用任何编号。"""

    if type(width) is not int or width < 3:
        raise SdlcError("正式编号宽度必须是大于等于3的整数。")
    order = build_allocation_order(objects)
    maxima: dict[str, int] = {}
    for existing_id in existing_ids:
        if not isinstance(existing_id, str):
            continue
        match = FORMAL_ID_PATTERN.fullmatch(existing_id)
        if match is None:
            continue
        prefix = match.group("prefix")
        maxima[prefix] = max(maxima.get(prefix, 0), int(match.group("number")))

    mapping: dict[str, str] = {}
    for item in order:
        next_value = maxima.get(item.id_prefix, 0) + 1
        maxima[item.id_prefix] = next_value
        mapping[item.client_key] = f"{item.id_prefix}-{next_value:0{width}d}"
    return mapping


def rewrite_temporary_references(value: object, mapping: Mapping[str, str]) -> object:
    """只重写完整字段值中的临时引用，不从标题、摘要或普通文字猜测关系。"""

    if isinstance(value, str):
        match = TEMPORARY_REFERENCE_PATTERN.fullmatch(value)
        if match is not None:
            client_key = match.group("client_key")
            formal_id = mapping.get(client_key)
            if formal_id is None:
                raise SdlcError(
                    f"临时引用跨包或悬空：{value}，当前导入包没有对应 client_key。"
                )
            return formal_id
        if "@client:" in value:
            raise SdlcError(
                f"临时引用必须完整占用一个字段值，不能嵌入普通文字：{value}。"
            )
        return value
    if isinstance(value, list):
        return [rewrite_temporary_references(item, mapping) for item in value]
    if isinstance(value, dict):
        rewritten: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SdlcError("结构化导入文件的字段名必须是字符串。")
            if "@client:" in key:
                raise SdlcError("结构化导入文件的字段名不能使用 @client: 临时引用。")
            rewritten[key] = rewrite_temporary_references(item, mapping)
        return rewritten
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise SdlcError("结构化导入内容必须是有效 JSON 数据。")


__all__ = [
    "AllocationObject",
    "CLIENT_KEY_PATTERN",
    "FORMAL_ID_PATTERN",
    "SUPPORTED_ID_PREFIXES",
    "TEMPORARY_REFERENCE_PATTERN",
    "allocate_stable_ids",
    "build_allocation_order",
    "rewrite_temporary_references",
]
