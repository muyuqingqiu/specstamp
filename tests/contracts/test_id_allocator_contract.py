from __future__ import annotations

from copy import deepcopy

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    allocate_stable_ids,
    build_allocation_order,
    rewrite_temporary_references,
)


def _object(client_key: str, prefix: str = "FR", *dependencies: str) -> AllocationObject:
    return AllocationObject(
        client_key=client_key,
        id_prefix=prefix,
        depends_on=tuple(f"@client:{item}" for item in dependencies),
    )


def test_stable_ids_follow_dependency_topology_and_rewrite_exact_references() -> None:
    objects = [
        _object("page", "PAGE", "api"),
        _object("api", "API", "data"),
        _object("data", "DATA"),
        _object("audit", "DATA"),
    ]

    order = build_allocation_order(objects)
    mapping = allocate_stable_ids(
        objects,
        existing_ids=["DATA-002", "API-009", "PAGE-001", "FR-999-extra"],
    )

    assert [item.client_key for item in order] == ["audit", "data", "api", "page"]
    assert mapping == {
        "audit": "DATA-003",
        "data": "DATA-004",
        "api": "API-010",
        "page": "PAGE-002",
    }
    source = {
        "page_id": "@client:page",
        "links": ["@client:data", {"api": "@client:api"}],
        "existing": "FR-001",
    }
    snapshot = deepcopy(source)
    assert rewrite_temporary_references(source, mapping) == {
        "page_id": "PAGE-002",
        "links": ["DATA-004", {"api": "API-010"}],
        "existing": "FR-001",
    }
    assert source == snapshot


def test_independent_objects_use_client_key_as_deterministic_tie_breaker() -> None:
    first = [_object("zeta"), _object("alpha"), _object("middle")]
    second = list(reversed(first))

    assert allocate_stable_ids(first, existing_ids=[]) == {
        "alpha": "FR-001",
        "middle": "FR-002",
        "zeta": "FR-003",
    }
    assert allocate_stable_ids(second, existing_ids=[]) == allocate_stable_ids(
        first, existing_ids=[]
    )


@pytest.mark.parametrize(
    ("objects", "message"),
    [
        ([_object("same"), _object("same")], "client_key 重复"),
        ([_object("first", "FR", "missing")], "跨包或悬空"),
        ([_object("first", "FR", "second"), _object("second", "FR", "first")], "依赖环"),
        ([_object("first", "FR", "first")], "依赖环"),
    ],
)
def test_invalid_allocation_graph_is_rejected_before_any_mapping(
    objects: list[AllocationObject], message: str
) -> None:
    with pytest.raises(SdlcError, match=message):
        allocate_stable_ids(objects, existing_ids=["FR-007"])


def test_rewriter_rejects_cross_package_and_embedded_temporary_references() -> None:
    mapping = {"local": "FR-001"}

    with pytest.raises(SdlcError, match="跨包或悬空"):
        rewrite_temporary_references({"ref": "@client:outside"}, mapping)
    with pytest.raises(SdlcError, match="必须完整占用一个字段值"):
        rewrite_temporary_references({"text": "前缀 @client:local 后缀"}, mapping)
    with pytest.raises(SdlcError, match="字段名不能使用"):
        rewrite_temporary_references({"@client:local": "value"}, mapping)
