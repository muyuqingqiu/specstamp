from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.reference_locator import (
    DESIGN_NODE_INDEX_SCHEMA,
    REFERENCE_LOCATOR_SCHEMA,
    SUPPORTED_LOCATOR_KINDS,
    locate_reference,
    validate_reference,
)
from codex_sdlc.core.structured_contract import sha256_bytes, sha256_file


def reference(path: Path, project: Path, locator: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": REFERENCE_LOCATOR_SCHEMA,
        "path": path.relative_to(project).as_posix(),
        "sha256": sha256_file(path),
        "locator": locator,
    }


def write_node_index(
    project: Path,
    design: Path,
    *,
    pages: list[dict[str, object]] | None = None,
    design_path: str | None = None,
    design_sha256: str | None = None,
    schema_version: str = DESIGN_NODE_INDEX_SCHEMA,
    filename: str = "设计节点索引.json",
) -> Path:
    index_path = project / filename
    document = {
        "schema_version": schema_version,
        "design_path": design_path or design.relative_to(project).as_posix(),
        "design_sha256": design_sha256 or sha256_file(design),
        "pages": pages or [{"page_id": "后台", "nodes": [{"node_id": "课程列表"}]}],
    }
    index_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return index_path


def design_node_locator(
    index_path: Path,
    project: Path,
    *,
    page_id: str = "后台",
    node_id: str = "课程列表",
) -> dict[str, object]:
    return {
        "kind": "design_node",
        "page_id": page_id,
        "node_id": node_id,
        "node_index_path": index_path.relative_to(project).as_posix(),
        "node_index_sha256": sha256_file(index_path),
        "node_index_schema": DESIGN_NODE_INDEX_SCHEMA,
    }


def test_text_range_hits_exact_lines_and_checks_both_hashes(tmp_path: Path) -> None:
    source = tmp_path / "需求.md"
    source.write_bytes("第一行\r\n第二行\r\n第三行\r\n".encode("utf-8"))
    fragment = "第二行\r\n第三行\r\n".encode("utf-8")
    item = reference(
        source,
        tmp_path,
        {
            "kind": "text_range",
            "line_start": 2,
            "line_end": 3,
            "fragment_sha256": sha256_bytes(fragment),
            "display_heading": "需求规则",
        },
    )

    result = locate_reference(tmp_path, item)

    assert result.kind == "text_range"
    assert result.content == "第二行\r\n第三行\r\n"
    assert result.fragment_sha256 == sha256_bytes(fragment)
    assert result.file_sha256 == item["sha256"]


def test_text_range_accepts_main_design_field_names(tmp_path: Path) -> None:
    source = tmp_path / "说明.md"
    source.write_text("一\n二\n", encoding="utf-8")
    item = reference(
        source,
        tmp_path,
        {
            "type": "text_range",
            "start_line": 1,
            "end_line": 1,
            "fragment_sha256": sha256_bytes("一\n".encode("utf-8")),
        },
    )

    assert validate_reference(tmp_path, item).content == "一\n"


def test_byte_range_uses_zero_based_end_exclusive_offsets(tmp_path: Path) -> None:
    source = tmp_path / "资料.bin"
    source.write_bytes(b"0123456789")
    item = reference(
        source,
        tmp_path,
        {
            "kind": "byte_range",
            "byte_start": 2,
            "byte_end": 6,
            "fragment_sha256": sha256_bytes(b"2345"),
        },
    )

    result = locate_reference(tmp_path, item)

    assert result.content == b"2345"
    assert result.fragment_sha256 == sha256_bytes(b"2345")


def test_json_pointer_hits_object_array_escaped_key_and_root(tmp_path: Path) -> None:
    source = tmp_path / "需求.json"
    source.write_text('{"items":[{"a/b":{"~key":"命中"}}]}\n', encoding="utf-8")
    nested = reference(
        source,
        tmp_path,
        {"kind": "json_pointer", "value": "/items/0/a~1b/~0key"},
    )
    root = reference(source, tmp_path, {"kind": "json_pointer", "value": "/"})

    assert locate_reference(tmp_path, nested).content == "命中"
    assert locate_reference(tmp_path, root).content == {"items": [{"a/b": {"~key": "命中"}}]}


def test_pdf_region_hits_real_page_and_checks_region_bounds(tmp_path: Path) -> None:
    source = tmp_path / "设计.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.add_blank_page(width=400, height=500)
    with source.open("wb") as handle:
        writer.write(handle)
    item = reference(
        source,
        tmp_path,
        {
            "kind": "pdf_region",
            "page": 2,
            "region": {"x": 10, "y": 20, "width": 100, "height": 120},
        },
    )

    result = locate_reference(tmp_path, item)

    assert result.content == {
        "page": 2,
        "region": {"x": 10.0, "y": 20.0, "width": 100.0, "height": 120.0},
    }


def test_pdf_region_allows_whole_page_location(tmp_path: Path) -> None:
    source = tmp_path / "整页.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    with source.open("wb") as handle:
        writer.write(handle)

    result = locate_reference(
        tmp_path,
        reference(source, tmp_path, {"kind": "pdf_region", "page": 1}),
    )

    assert result.content == {"page": 1, "region": None}


def test_design_node_uses_a_bound_index_and_hits_one_exact_node(tmp_path: Path) -> None:
    source = tmp_path / "界面.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    index_path = write_node_index(tmp_path, source)
    item = reference(
        source,
        tmp_path,
        design_node_locator(index_path, tmp_path),
    )

    result = locate_reference(tmp_path, item)

    assert result.content == {
        "page_id": "后台",
        "node_id": "课程列表",
        "node_index_path": "设计节点索引.json",
    }
    assert result.file_sha256 == sha256_file(source)


def test_whole_file_hits_real_binary_without_copying_content(tmp_path: Path) -> None:
    source = tmp_path / "附件.bin"
    source.write_bytes(b"binary-content")

    result = locate_reference(
        tmp_path,
        reference(source, tmp_path, {"kind": "whole_file"}),
    )

    assert result.kind == "whole_file"
    assert result.content is None
    assert result.file_sha256 == sha256_file(source)


def test_all_six_locator_kinds_have_real_success_cases() -> None:
    assert SUPPORTED_LOCATOR_KINDS == {
        "text_range",
        "byte_range",
        "json_pointer",
        "pdf_region",
        "design_node",
        "whole_file",
    }


def test_file_hash_drift_is_rejected_before_location(tmp_path: Path) -> None:
    source = tmp_path / "资料.md"
    source.write_text("原内容\n", encoding="utf-8")
    item = reference(source, tmp_path, {"kind": "whole_file"})
    source.write_text("内容变化\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="sha256 不一致"):
        locate_reference(tmp_path, item)


@pytest.mark.parametrize("kind", ["text_range", "byte_range"])
def test_fragment_hash_drift_is_rejected_even_when_file_hash_is_current(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "资料.txt"
    source.write_text("abcdef\n", encoding="utf-8")
    locator: dict[str, object]
    if kind == "text_range":
        locator = {"kind": kind, "line_start": 1, "line_end": 1, "fragment_sha256": "0" * 64}
    else:
        locator = {"kind": kind, "byte_start": 1, "byte_end": 4, "fragment_sha256": "0" * 64}

    with pytest.raises(SdlcError, match="fragment_sha256 不一致"):
        locate_reference(tmp_path, reference(source, tmp_path, locator))


@pytest.mark.parametrize(
    "locator",
    [
        {"kind": "text_range", "line_start": 0, "line_end": 1, "fragment_sha256": "0" * 64},
        {"kind": "text_range", "line_start": 1, "line_end": 9, "fragment_sha256": "0" * 64},
        {"kind": "byte_range", "byte_start": -1, "byte_end": 1, "fragment_sha256": "0" * 64},
        {"kind": "byte_range", "byte_start": 0, "byte_end": 99, "fragment_sha256": "0" * 64},
    ],
)
def test_invalid_text_and_byte_ranges_are_rejected(tmp_path: Path, locator: dict[str, object]) -> None:
    source = tmp_path / "资料.txt"
    source.write_text("一行\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="范围"):
        locate_reference(tmp_path, reference(source, tmp_path, locator))


@pytest.mark.parametrize("pointer", ["items/0", "/items/9", "/items/-", "/items/00", "/items/~2"])
def test_invalid_or_missing_json_pointer_is_rejected(tmp_path: Path, pointer: str) -> None:
    source = tmp_path / "资料.json"
    source.write_text('{"items":[1]}\n', encoding="utf-8")

    with pytest.raises(SdlcError, match="JSON Pointer"):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "json_pointer", "value": pointer}),
        )


def test_invalid_json_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "错误.json"
    source.write_text("{invalid}\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="无法解析"):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "json_pointer", "value": "/"}),
        )


@pytest.mark.parametrize("content", ['{"same":1,"same":2}\n', '{"value":NaN}\n'])
def test_ambiguous_or_non_standard_json_is_rejected(tmp_path: Path, content: str) -> None:
    source = tmp_path / "非标准.json"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(SdlcError, match="JSON 引用文件"):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "json_pointer", "value": "/"}),
        )


def test_pdf_missing_page_and_out_of_bounds_region_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "设计.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with source.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(SdlcError, match="只有 1 页"):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "pdf_region", "page": 2}),
        )
    with pytest.raises(SdlcError, match="超出页面边界"):
        locate_reference(
            tmp_path,
            reference(
                source,
                tmp_path,
                {
                    "kind": "pdf_region",
                    "page": 1,
                    "region": {"x": 90, "y": 90, "width": 20, "height": 20},
                },
            ),
        )


def test_invalid_pdf_file_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "错误.pdf"
    source.write_bytes(b"not-a-pdf")

    with pytest.raises(SdlcError, match="无法解析"):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "pdf_region", "page": 1}),
        )


def test_plain_png_without_a_valid_node_index_can_only_use_whole_file(tmp_path: Path) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")

    with pytest.raises(SdlcError):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, {"kind": "design_node", "page": "后台", "node": "课程列表"}),
        )
    assert locate_reference(
        tmp_path,
        reference(source, tmp_path, {"kind": "whole_file"}),
    ).kind == "whole_file"


@pytest.mark.parametrize(
    ("page_id", "node_id", "message"),
    [
        ("不存在页面", "课程列表", "page_id"),
        ("后台", "不存在节点", "node_id"),
        ("后台", "课程列表 ", "node_id"),
    ],
)
def test_design_node_rejects_wrong_page_node_and_case_exact_mismatch(
    tmp_path: Path,
    page_id: str,
    node_id: str,
    message: str,
) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")
    index_path = write_node_index(tmp_path, source)

    with pytest.raises(SdlcError, match=message):
        locate_reference(
            tmp_path,
            reference(
                source,
                tmp_path,
                design_node_locator(index_path, tmp_path, page_id=page_id, node_id=node_id),
            ),
        )


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        (
            [
                {"page_id": "后台", "nodes": [{"node_id": "课程列表"}]},
                {"page_id": "后台", "nodes": [{"node_id": "其他节点"}]},
            ],
            "重复 page_id",
        ),
        (
            [
                {
                    "page_id": "后台",
                    "nodes": [{"node_id": "课程列表"}, {"node_id": "课程列表"}],
                }
            ],
            "重复 node_id",
        ),
    ],
)
def test_design_node_rejects_duplicate_identifiers(
    tmp_path: Path,
    pages: list[dict[str, object]],
    message: str,
) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")
    index_path = write_node_index(tmp_path, source, pages=pages)

    with pytest.raises(SdlcError, match=message):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, design_node_locator(index_path, tmp_path)),
        )


def test_design_node_rejects_index_hash_drift(tmp_path: Path) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")
    index_path = write_node_index(tmp_path, source)
    item = reference(source, tmp_path, design_node_locator(index_path, tmp_path))
    index_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SdlcError, match="node_index_sha256 不一致"):
        locate_reference(tmp_path, item)


@pytest.mark.parametrize(
    ("design_path", "design_sha256", "message"),
    [
        ("另一个设计.png", None, "design_path"),
        (None, "0" * 64, "design_sha256"),
    ],
)
def test_design_node_rejects_main_file_binding_mismatch(
    tmp_path: Path,
    design_path: str | None,
    design_sha256: str | None,
    message: str,
) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")
    index_path = write_node_index(
        tmp_path,
        source,
        design_path=design_path,
        design_sha256=design_sha256,
    )

    with pytest.raises(SdlcError, match=message):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, design_node_locator(index_path, tmp_path)),
        )


@pytest.mark.parametrize("invalid_kind", ["wrong_version", "unknown", "missing", "nan", "duplicate"])
def test_design_node_rejects_invalid_or_non_standard_index(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    source = tmp_path / "设计.png"
    source.write_bytes(b"image")
    index_path = write_node_index(
        tmp_path,
        source,
        schema_version="design-node-index.v2" if invalid_kind == "wrong_version" else DESIGN_NODE_INDEX_SCHEMA,
    )
    document = json.loads(index_path.read_text(encoding="utf-8"))
    if invalid_kind == "unknown":
        document["unknown"] = True
    elif invalid_kind == "missing":
        document.pop("pages")
    elif invalid_kind == "nan":
        index_path.write_text(
            '{"schema_version":"design-node-index.v1","design_path":"设计.png",'
            f'"design_sha256":"{sha256_file(source)}","pages":NaN}}\n',
            encoding="utf-8",
        )
    elif invalid_kind == "duplicate":
        index_path.write_text(
            '{"schema_version":"design-node-index.v1","schema_version":"design-node-index.v1",'
            f'"design_path":"设计.png","design_sha256":"{sha256_file(source)}",'
            '"pages":[{"page_id":"后台","nodes":[{"node_id":"课程列表"}]}]}\n',
            encoding="utf-8",
        )
    if invalid_kind in {"unknown", "missing"}:
        index_path.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(SdlcError):
        locate_reference(
            tmp_path,
            reference(source, tmp_path, design_node_locator(index_path, tmp_path)),
        )


def test_design_node_index_path_traversal_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "设计.png"
    source.write_bytes(b"image")
    outside_index = write_node_index(tmp_path, source, design_path="设计.png")
    (project / "索引链接.json").symlink_to(outside_index)
    base_locator = {
        "kind": "design_node",
        "page_id": "后台",
        "node_id": "课程列表",
        "node_index_sha256": sha256_file(outside_index),
        "node_index_schema": DESIGN_NODE_INDEX_SCHEMA,
    }

    for unsafe_path in ("../设计节点索引.json", "索引链接.json"):
        with pytest.raises(SdlcError):
            locate_reference(
                project,
                reference(source, project, {**base_locator, "node_index_path": unsafe_path}),
            )


def test_path_traversal_absolute_path_and_symlink_escape_are_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容\n", encoding="utf-8")
    (project / "escape.txt").symlink_to(outside)
    base = {
        "schema_version": REFERENCE_LOCATOR_SCHEMA,
        "sha256": sha256_file(outside),
        "locator": {"kind": "whole_file"},
    }

    for path in ("../outside.txt", str(outside), "escape.txt"):
        with pytest.raises(SdlcError):
            locate_reference(project, {**base, "path": path})


@pytest.mark.parametrize(
    "locator",
    [
        {"kind": "unknown"},
        {"kind": "whole_file", "type": "whole_file"},
        {"kind": "whole_file", "page": 1},
        {"kind": "text_range", "line_start": 1, "start_line": 1, "line_end": 1, "end_line": 1},
        {"kind": "json_pointer", "value": "/", "pointer": "/"},
    ],
)
def test_unknown_ambiguous_and_extra_locator_fields_are_rejected(
    tmp_path: Path,
    locator: dict[str, object],
) -> None:
    source = tmp_path / "资料.json"
    source.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SdlcError):
        locate_reference(tmp_path, reference(source, tmp_path, locator))


def test_unsupported_contract_version_and_invalid_hash_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "资料.bin"
    source.write_bytes(b"content")
    item = reference(source, tmp_path, {"kind": "whole_file"})
    wrong_version = {**item, "schema_version": "reference-locator.v2"}
    wrong_hash = {**item, "sha256": "不是哈希"}

    with pytest.raises(SdlcError, match="版本不受支持"):
        locate_reference(tmp_path, wrong_version)
    with pytest.raises(SdlcError, match="64位"):
        locate_reference(tmp_path, wrong_hash)


def test_non_string_locator_field_name_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "资料.bin"
    source.write_bytes(b"content")
    item = reference(source, tmp_path, {"kind": "whole_file"})
    item["locator"] = {"kind": "whole_file", 1: "无效字段"}

    with pytest.raises(SdlcError, match="字段名必须是字符串"):
        locate_reference(tmp_path, item)


def test_validation_failure_does_not_modify_reference_or_create_project_state(tmp_path: Path) -> None:
    source = tmp_path / "资料.bin"
    source.write_bytes(b"content")
    item = reference(source, tmp_path, {"kind": "whole_file"})
    item["sha256"] = "0" * 64
    original = deepcopy(item)

    with pytest.raises(SdlcError):
        locate_reference(tmp_path, item)

    assert item == original
    assert not (tmp_path / ".codex-sdlc").exists()
