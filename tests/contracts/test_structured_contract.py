from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import resolve_project_path
from codex_sdlc.core.structured_contract import (
    HASH_EXCLUDE_POINTERS_KEY,
    available_schema_ids,
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    contract_hash_payload,
    contract_sha256,
    hash_excluded_pointers,
    load_schema,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)


def test_schema_loader_uses_the_only_versioned_schema_directory() -> None:
    expected = {
        "sdlc.design-facts.v1",
        "sdlc.model-review.v1",
        "sdlc.requirement-facts.v1",
        "sdlc.source-index.v1",
        "reference-locator.v1",
        "design-node-index.v1",
    }

    assert expected <= set(available_schema_ids())
    for schema_id in expected:
        schema = load_schema(schema_id)
        assert schema["$id"] == schema_id
        assert load_schema(schema_id.removeprefix("sdlc."))["$id"] == schema_id
        assert load_schema(schema_id.removeprefix("sdlc.") + ".json")["$id"] == schema_id


def test_new_public_schemas_accept_their_fixed_versions() -> None:
    source_index = {
        "schema": "sdlc.source-index.v1",
        "source_kind": "formal",
        "source_projection_sha256": "1" * 64,
        "normalization": "utf-8-preserve-source-and-canonical-json-pointer-v1",
        "documents": [{"document_id": "formal-package", "sha256": "2" * 64}],
        "units": [],
        "artifact_sha256": "3" * 64,
    }
    reference_locator = {
        "schema_version": "reference-locator.v1",
        "path": "设计稿.png",
        "sha256": "4" * 64,
        "locator": {"kind": "whole_file"},
    }
    design_node_index = {
        "schema_version": "design-node-index.v1",
        "design_path": "设计稿.png",
        "design_sha256": "4" * 64,
        "pages": [{"page_id": "首页", "nodes": [{"node_id": "提交按钮"}]}],
    }

    validate_schema_document(source_index, schema_name="sdlc.source-index.v1")
    validate_schema_document(reference_locator, schema_name="reference-locator.v1")
    validate_schema_document(design_node_index, schema_name="design-node-index.v1")


@pytest.mark.parametrize(
    ("schema_name", "document"),
    [
        (
            "reference-locator.v1",
            {
                "schema_version": "reference-locator.v2",
                "path": "设计稿.png",
                "sha256": "4" * 64,
                "locator": {"kind": "whole_file"},
            },
        ),
        (
            "sdlc.source-index.v1",
            {
                "schema": "sdlc.source-index.v2",
                "source_kind": "formal",
                "source_projection_sha256": "1" * 64,
                "normalization": "utf-8-preserve-source-and-canonical-json-pointer-v1",
                "documents": [{"document_id": "formal-package", "sha256": "2" * 64}],
                "units": [],
                "artifact_sha256": "3" * 64,
            },
        ),
        (
            "design-node-index.v1",
            {
                "schema_version": "design-node-index.v2",
                "design_path": "设计稿.png",
                "design_sha256": "4" * 64,
                "pages": [{"page_id": "首页", "nodes": [{"node_id": "提交按钮"}]}],
            },
        ),
    ],
)
def test_new_public_schemas_reject_wrong_versions(
    schema_name: str,
    document: dict[str, object],
) -> None:
    with pytest.raises(SdlcError, match="Schema 校验失败"):
        validate_schema_document(document, schema_name=schema_name)


def test_schema_validation_rejects_missing_unknown_and_non_json_values() -> None:
    valid = {
        "schema_version": "design-node-index.v1",
        "design_path": "设计稿.png",
        "design_sha256": "4" * 64,
        "pages": [{"page_id": "首页", "nodes": [{"node_id": "提交按钮"}]}],
    }
    missing = {key: value for key, value in valid.items() if key != "pages"}
    unknown = {**valid, "说明": "合同没有这个字段"}
    non_json = {**valid, "pages": float("nan")}

    for document in (missing, unknown, non_json):
        with pytest.raises(SdlcError):
            validate_schema_document(document, schema_name="design-node-index.v1")


def test_schema_loader_returns_an_independent_copy() -> None:
    first = load_schema("sdlc.requirement-facts.v1")
    first["$id"] = "已被调用方修改"

    second = load_schema("sdlc.requirement-facts.v1")

    assert second["$id"] == "sdlc.requirement-facts.v1"


@pytest.mark.parametrize("schema_name", ["missing.v1", "../requirement-facts.v1", "/tmp/schema.json", ""])
def test_schema_loader_rejects_unknown_versions_and_path_input(schema_name: str) -> None:
    with pytest.raises(SdlcError):
        load_schema(schema_name)


def test_canonical_json_is_stable_compact_and_keeps_unicode() -> None:
    left = {"中文": "保留", "items": [1, True, None], "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "items": [1, True, None], "中文": "保留"}
    expected = '{"items":[1,true,null],"nested":{"a":1,"b":2},"中文":"保留"}'

    assert canonical_json_bytes(left) == expected.encode("utf-8")
    assert canonical_json_bytes(right) == canonical_json_bytes(left)
    assert canonical_json_text(left) == expected + "\n"
    assert canonical_sha256(left) == canonical_sha256(right)


@pytest.mark.parametrize(
    "value",
    [
        {"invalid": float("nan")},
        {"invalid": {"不能序列化"}},
        {"invalid": ("元组不能暗中转成数组",)},
        {1: "对象键不能暗中转成字符串"},
    ],
)
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(SdlcError):
        canonical_json_bytes(value)


def test_canonical_json_rejects_recursive_containers() -> None:
    recursive: list[object] = []
    recursive.append(recursive)

    with pytest.raises(SdlcError, match="有效数据"):
        canonical_json_bytes(recursive)


def test_sha256_helpers_detect_any_byte_change(tmp_path: Path) -> None:
    source = tmp_path / "资料.bin"
    source.write_bytes(b"document\x00content")
    first = sha256_file(source)

    assert first == hashlib.sha256(b"document\x00content").hexdigest()
    assert first == sha256_bytes(b"document\x00content")

    source.write_bytes(b"document\x00contenu")
    assert sha256_file(source) != first


def test_sha256_bytes_rejects_text_to_avoid_implicit_encoding() -> None:
    with pytest.raises(SdlcError, match="bytes"):
        sha256_bytes("文本")  # type: ignore[arg-type]


def test_contract_hash_only_excludes_fields_declared_by_schema() -> None:
    schema_id = "sdlc.requirement-facts.v1"
    schema = load_schema(schema_id)
    assert schema[HASH_EXCLUDE_POINTERS_KEY] == ["/artifact_sha256"]
    assert hash_excluded_pointers(schema_id) == ("/artifact_sha256",)
    first = {
        "schema": schema_id,
        "semantic": {"facts": [{"statement": "原始规则"}]},
        "artifact_sha256": "0" * 64,
    }
    only_hash_field_changed = {
        **first,
        "artifact_sha256": "f" * 64,
    }
    content_changed = {
        **first,
        "semantic": {"facts": [{"statement": "规则已改变"}]},
    }

    assert contract_sha256(first, schema_name=schema_id) == contract_sha256(
        only_hash_field_changed,
        schema_name=schema_id,
    )
    assert contract_sha256(content_changed, schema_name=schema_id) != contract_sha256(
        first,
        schema_name=schema_id,
    )
    assert contract_hash_payload(first, schema_name=schema_id) == {
        "schema": schema_id,
        "semantic": {"facts": [{"statement": "原始规则"}]},
    }
    assert first["artifact_sha256"] == "0" * 64


def test_source_index_hash_exclusion_is_declared_by_its_schema() -> None:
    schema_id = "sdlc.source-index.v1"
    first = {
        "schema": schema_id,
        "source_kind": "formal",
        "source_projection_sha256": "1" * 64,
        "normalization": "utf-8-preserve-source-and-canonical-json-pointer-v1",
        "documents": [{"document_id": "formal-package", "sha256": "2" * 64}],
        "units": [],
        "artifact_sha256": "3" * 64,
    }
    changed_hash = {**first, "artifact_sha256": "4" * 64}

    assert hash_excluded_pointers(schema_id) == ("/artifact_sha256",)
    assert contract_sha256(first, schema_name=schema_id) == contract_sha256(
        changed_hash,
        schema_name=schema_id,
    )


def test_undeclared_display_field_still_changes_contract_hash() -> None:
    schema_id = "sdlc.model-review.v1"
    first = {"schema": schema_id, "display_heading": "审核一", "artifact_sha256": "0" * 64}
    second = {**first, "display_heading": "审核二"}

    assert contract_sha256(first, schema_name=schema_id) != contract_sha256(second, schema_name=schema_id)


def test_project_path_accepts_existing_and_planned_files_inside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    existing = project / "资料" / "需求.md"
    existing.parent.mkdir()
    existing.write_text("需求正文\n", encoding="utf-8")

    assert resolve_project_path(project, "资料/需求.md", must_exist=True) == existing
    assert resolve_project_path(project, "产物/索引.json") == project / "产物" / "索引.json"


@pytest.mark.parametrize("unsafe_path", ["", ".", "../outside.txt", "child/../../outside.txt"])
def test_project_path_rejects_empty_root_and_parent_traversal(tmp_path: Path, unsafe_path: str) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(SdlcError):
        resolve_project_path(project, unsafe_path)


def test_project_path_rejects_absolute_and_missing_required_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(SdlcError, match="绝对路径"):
        resolve_project_path(project, tmp_path / "outside.txt")
    with pytest.raises(SdlcError, match="不存在"):
        resolve_project_path(project, "资料/不存在.md", must_exist=True)


def test_project_path_rejects_symlink_escape_and_allows_internal_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("秘密\n", encoding="utf-8")
    (project / "escape").symlink_to(outside, target_is_directory=True)

    internal = project / "internal"
    internal.mkdir()
    (internal / "safe.txt").write_text("安全\n", encoding="utf-8")
    (project / "inside-link").symlink_to(internal, target_is_directory=True)

    with pytest.raises(SdlcError, match="越过项目目录"):
        resolve_project_path(project, "escape/secret.txt", must_exist=True)
    assert resolve_project_path(project, "inside-link/safe.txt", must_exist=True) == internal / "safe.txt"
