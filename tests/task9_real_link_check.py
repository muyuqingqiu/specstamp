from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pypdf import PdfWriter
import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.reference_index import (
    validate_reference_index_document,
    validate_reference_index_file,
    write_reference_index_file,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    sha256_bytes,
)


def _reference(path: Path, root: Path, locator: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "reference-locator.v1",
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(path.read_bytes()),
        "locator": locator,
    }


def test_real_files_use_exact_reference_index_locators() -> None:
    """在仓库外用真实字节证明六类定位和普通 PNG 降级使用同一正式索引。"""

    with tempfile.TemporaryDirectory(prefix="codex-sdlc-T016-real-link-") as raw:
        root = Path(raw)
        requirement_json = root / "original/requirement-split.v1.json"
        requirement_json.parent.mkdir(parents=True)
        requirement_json.write_text(
            canonical_json_text(
                {
                    "functional_requirements": [
                        {"id": "FR-001", "body": "读取正式原文"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        text = root / "original/技术方案.md"
        text.write_text("# 技术方案\n架构正文\n约束正文\n", encoding="utf-8")
        lines = text.read_text(encoding="utf-8").splitlines(keepends=True)
        fragment = lines[1].encode("utf-8")
        binary = root / "original/样例.bin"
        binary.write_bytes(b"prefix-target-suffix")

        pdf = root / "original/手册.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        with pdf.open("wb") as handle:
            writer.write(handle)

        design = root / "original/界面.fig"
        design.write_bytes(b"fixed-design-bytes")
        node_index = root / "original/界面.nodes.v1.json"
        node_index.write_text(
            canonical_json_text(
                {
                    "schema_version": "design-node-index.v1",
                    "design_path": "original/界面.fig",
                    "design_sha256": sha256_bytes(design.read_bytes()),
                    "pages": [
                        {
                            "page_id": "Page-A",
                            "nodes": [{"node_id": "Node-A"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        png = root / "original/截图.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nT016")

        document = {
            "schema_version": "reference-index.v1",
            "requirement_id": "REQ-001",
            "entries": {
                "FR-001": _reference(
                    requirement_json,
                    root,
                    {
                        "kind": "json_pointer",
                        "value": "/functional_requirements/0",
                    },
                ),
                "DES-001#architecture": _reference(
                    text,
                    root,
                    {
                        "kind": "text_range",
                        "line_start": 2,
                        "line_end": 2,
                        "fragment_sha256": sha256_bytes(fragment),
                        "display_heading": "重复标题",
                    },
                ),
                "SPEC-001#bytes": _reference(
                    binary,
                    root,
                    {
                        "kind": "byte_range",
                        "byte_start": 7,
                        "byte_end": 13,
                        "fragment_sha256": sha256_bytes(b"target"),
                    },
                ),
                "MAT-001#page": _reference(
                    pdf,
                    root,
                    {"kind": "pdf_region", "page": 1},
                ),
                "MAT-002#node": _reference(
                    design,
                    root,
                    {
                        "kind": "design_node",
                        "page_id": "Page-A",
                        "node_id": "Node-A",
                        "node_index_path": "original/界面.nodes.v1.json",
                        "node_index_sha256": sha256_bytes(node_index.read_bytes()),
                        "node_index_schema": "design-node-index.v1",
                    },
                ),
                "MAT-003": _reference(
                    png,
                    root,
                    {"kind": "whole_file"},
                ),
            },
            "display": {
                "DES-001#architecture": {"display_name": "架构原名"},
                "FR-001": {"heading": "重复标题"},
            },
        }
        validated = validate_reference_index_document(root, document)
        output = root / "reference-index.v1.json"
        write_reference_index_file(root, output, validated)
        assert validate_reference_index_file(root, output) == validated
        assert validated["entries"]["MAT-003"]["locator"] == {
            "kind": "whole_file"
        }
        assert "summary" not in json.dumps(validated, ensure_ascii=False).lower()

        # 标题重复或改变不影响定位；真实片段变化则必须由完整哈希拒绝。
        validated["display"]["FR-001"]["heading"] = "重复标题"
        validate_reference_index_document(root, validated)
        text.write_text("# 技术方案\n架构正文已变化\n约束正文\n", encoding="utf-8")
        with pytest.raises(SdlcError, match="sha256 不一致"):
            validate_reference_index_document(root, validated)
