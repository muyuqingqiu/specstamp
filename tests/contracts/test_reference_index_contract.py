from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from pypdf import PdfWriter
import pytest

from codex_sdlc.core.artifact_index import (
    artifact_index_bytes,
    formal_manifest_entries,
)
from codex_sdlc.core.design_artifact_contract import validate_design_artifact_record
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.reference_index import (
    build_reference_index_document,
    validate_reference_index_document,
    validate_reference_index_file,
    write_reference_index_file,
)
from codex_sdlc.core.state import formal_reference_index_checks
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
)
from codex_sdlc.services.design_service import design_reference_identity_sha256


def _source_reference(material_id: str, path: str, digest: str) -> dict[str, object]:
    return {
        "material_id": material_id,
        "reference": {
            "schema_version": "reference-locator.v1",
            "path": path,
            "sha256": digest,
            "locator": {"kind": "whole_file"},
        },
    }


def _requirement_split(source_digest: str) -> dict[str, object]:
    source = _source_reference("MAT-001", "原始资料/需求原文.md", source_digest)
    return {
        "schema_version": "requirement-split.v1",
        "draft_id": "DRAFT-001",
        "producer_run_id": "T-016-contract",
        "title": "课程需求",
        "background": "课程资料已经归档。",
        "goal": "提供稳定引用。",
        "scope": ["查看课程"],
        "out_of_scope": [],
        "user_scenarios": ["用户查看课程"],
        "input_material_hashes": {"MAT-001": source_digest},
        "global_rules": [
            {
                "client_key": "gr-login",
                "title": "重复标题",
                "description": "使用当前登录状态。",
                "type": "state",
                "applies_to": ["@client:fr-course"],
                "source_refs": [deepcopy(source)],
                "relations": [],
            }
        ],
        "functional_requirements": [
            {
                "client_key": "fr-course",
                "title": "重复标题",
                "description": "用户可以查看课程。",
                "elements": ["课程"],
                "flow": ["打开课程"],
                "facts": ["课程存在"],
                "rules": ["登录后查看"],
                "constraints": [],
                "states_and_exceptions": ["未登录时拒绝"],
                "acceptance_criteria": [
                    {
                        "client_key": "ac-course",
                        "owner_fr_ref": "@client:fr-course",
                        "operation": "打开课程",
                        "expected": "显示课程",
                        "pass_standard": "内容完整",
                        "source_refs": [deepcopy(source)],
                        "relations": [],
                    }
                ],
                "global_rule_refs": ["@client:gr-login"],
                "source_refs": [deepcopy(source)],
                "material_refs": ["MAT-001"],
                "depends_on": [],
                "out_of_scope": [],
                "relations": [],
            }
        ],
        "open_questions": [],
    }


def _special_artifact() -> dict[str, object]:
    digest = "1" * 64
    record: dict[str, object] = {
        "schema_version": "design-artifact.v1",
        "draft_id": "DRAFT-001",
        "artifact_id": "SPEC-001",
        "type": "special",
        "producer_run_id": "T-016-contract",
        "input_hashes": {"plan": digest},
        "requirement_refs": ["FR-001"],
        "global_rule_refs": ["GR-001"],
        "material_refs": ["MAT-001"],
        "depends_on": [],
        "code_evidence_paths": [],
        "plan_status": "completed",
        "output_path": "设计/模块/SPEC-001.design-artifact.v1.json",
        "revision": 1,
        "previous_artifact_sha256": None,
        "content": {
            "reason": "需要单独描述读取规则。",
            "design_items": [
                {
                    "spec_id": "SP-001",
                    "name": "读取规则",
                    "inputs": ["引用编号"],
                    "outputs": ["原文"],
                    "dependencies": [],
                    "review_method": "合同测试",
                    "acceptance": ["定位成功"],
                    "rollback_steps": ["停止读取"],
                }
            ],
        },
        "open_questions": [],
        "plan_sha256": digest,
        "plan_module_sha256": "2" * 64,
        "submission_sha256": "3" * 64,
    }
    record["artifact_sha256"] = canonical_sha256(record)
    return validate_design_artifact_record(record)


def _design_reference(technical_bytes: bytes) -> dict[str, object]:
    lines = technical_bytes.decode("utf-8").splitlines(keepends=True)
    fragment = lines[1].encode("utf-8")
    record: dict[str, object] = {
        "schema_version": "design-reference.v1",
        "design_id": "DES-001",
        "draft_id": "DRAFT-001",
        "client_key": "technical",
        "display_name": "课程技术方案",
        "material_id": "MAT-002",
        "path": "原始资料/技术方案.md",
        "sha256": sha256_bytes(technical_bytes),
        "anchors": [
            {
                "key": "DES-001#architecture",
                "display_name": "架构原名",
                "locator": {
                    "kind": "text_range",
                    "line_start": 2,
                    "line_end": 2,
                    "fragment_sha256": sha256_bytes(fragment),
                    "display_heading": "重复标题",
                },
                "fragment_sha256": sha256_bytes(fragment),
            }
        ],
        "applies_to": ["FR-001"],
        "requirement_confirmation_sha256": "4" * 64,
        "status": "confirmed",
        "confirmed_at": "2026-07-18T10:00:00Z",
    }
    record["identity_sha256"] = design_reference_identity_sha256(record)
    record["record_sha256"] = canonical_sha256(record)
    return record


def _write_pdf(path: Path) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "draft"
    archive = tmp_path / "requirement"
    source.mkdir()
    archive.mkdir()
    manifest: list[dict[str, object]] = []
    next_artifact = 1

    def add(
        source_path: str,
        content: bytes,
        *,
        business_id: str | None,
        artifact_type: str,
    ) -> dict[str, object]:
        nonlocal next_artifact
        source_file = source / source_path
        archive_path = f"original/{source_path}"
        archive_file = archive / archive_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_bytes(content)
        archive_file.write_bytes(content)
        item: dict[str, object] = {
            "artifact_id": f"ART-{next_artifact:03d}",
            "business_id": business_id,
            "artifact_type": artifact_type,
            "source_path": source_path,
            "archive_path": archive_path,
            "sha256": sha256_bytes(content),
            "review_relations": {
                "applies_to": [
                    {"stage": "integrated_design", "owner_id": "DRAFT-001"}
                ],
                "depends_on_business_ids": [],
            },
        }
        next_artifact += 1
        manifest.append(item)
        return item

    requirement_source = b"course requirement\n"
    add(
        "原始资料/需求原文.md",
        requirement_source,
        business_id="MAT-001",
        artifact_type="material",
    )
    split = _requirement_split(sha256_bytes(requirement_source))
    add(
        "需求/requirement-split.v1.json",
        canonical_json_text(split).encode("utf-8"),
        business_id=None,
        artifact_type="requirement_split",
    )
    technical = "# 技术方案\n架构正文\n".encode("utf-8")
    add(
        "原始资料/技术方案.md",
        technical,
        business_id="MAT-002",
        artifact_type="material",
    )
    image = b"\x89PNG\r\n\x1a\nT016"
    add(
        "原始资料/界面.png",
        image,
        business_id="MAT-003",
        artifact_type="material",
    )
    node_index = {
        "schema_version": "design-node-index.v1",
        "design_path": "original/原始资料/界面.png",
        "design_sha256": sha256_bytes(image),
        "pages": [{"page_id": "Page-A", "nodes": [{"node_id": "Node-A"}]}],
    }
    node_item = add(
        "原始资料/界面.nodes.v1.json",
        canonical_json_text(node_index).encode("utf-8"),
        business_id=None,
        artifact_type="design_node_index",
    )
    pdf_temp = tmp_path / "manual.pdf"
    pdf_bytes = _write_pdf(pdf_temp)
    add(
        "原始资料/手册.pdf",
        pdf_bytes,
        business_id="MAT-004",
        artifact_type="material",
    )
    module = _special_artifact()
    add(
        "设计/模块/SPEC-001.design-artifact.v1.json",
        canonical_json_text(module).encode("utf-8"),
        business_id="SPEC-001",
        artifact_type="design_artifact",
    )
    design = _design_reference(technical)
    add(
        "设计/引用记录/DES-001.json",
        canonical_json_text(design).encode("utf-8"),
        business_id="DES-001",
        artifact_type="design_reference",
    )
    material_references = [
        {
            "reference_id": "MAT-003#course-list",
            "material_id": "MAT-003",
            "display_name": "课程列表",
            "locator": {
                "kind": "design_node",
                "page_id": "Page-A",
                "node_id": "Node-A",
                "node_index_path": "原始资料/界面.nodes.v1.json",
                "node_index_sha256": node_item["sha256"],
                "node_index_schema": "design-node-index.v1",
            },
        },
        {
            "reference_id": "MAT-004#first-page",
            "material_id": "MAT-004",
            "locator": {"kind": "pdf_region", "page": 1},
        },
    ]
    requirement_mapping = {
        "gr-login": "GR-001",
        "fr-course": "FR-001",
        "ac-course": "AC-001",
        "src-source": "SRC-001",
    }
    content_key = "5" * 64
    destination = (
        f".codex-sdlc/drafts/DRAFT-001/需求/requirements-{content_key}"
    )
    receipt = {
        "schema": "draft-requirement-import-receipt.v1",
        "package_key": f"draft-requirements:DRAFT-001:{content_key}",
        "package_sha256": "6" * 64,
        "mapping": requirement_mapping,
        "destination": destination,
        "files": [
            f"{destination}/requirement-coverage.v1.json",
            f"{destination}/requirement-split.v1.json",
        ],
        "event_id": "EVT-T016-IMPORT",
        "imported_at": "2026-07-18T10:00:00Z",
        "producer_run_id": "T-016-contract",
        "review_blockers": [],
    }
    receipt_bytes = canonical_json_text(receipt).encode("utf-8")
    receipt_path = source / "需求/需求导入回执.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(receipt_bytes)

    # 正式清单是 artifact-index.v1 的投影；测试夹具也按真实双层合同生成，
    # 才能证明调用方缓存和当前磁盘索引不一致时会在写入前被拒绝。
    indexed_artifacts = [
        {
            "record_version": "artifact-index-record.v1",
            **deepcopy(item),
            "include_in_formal": True,
        }
        for item in manifest
    ]
    indexed_artifacts.append(
        {
            "record_version": "artifact-index-record.v1",
            "artifact_id": f"ART-{next_artifact:03d}",
            "business_id": None,
            "artifact_type": "requirement_import_receipt",
            "source_path": "需求/需求导入回执.json",
            "archive_path": "original/需求/需求导入回执.json",
            "sha256": sha256_bytes(receipt_bytes),
            "include_in_formal": False,
            "review_relations": {
                "applies_to": [],
                "depends_on_business_ids": [],
            },
        }
    )
    indexed_artifacts.sort(key=lambda item: str(item["source_path"]))
    index_seed = {
        "schema_version": "artifact-index.v1",
        "draft_id": "DRAFT-001",
        "artifacts": indexed_artifacts,
    }
    formal_manifest = formal_manifest_entries(index_seed)
    draft_revision_sha256 = canonical_sha256(
        {
            "draft_id": "DRAFT-001",
            "artifact_manifest": formal_manifest,
        }
    )
    index_body = {
        **index_seed,
        "draft_revision_sha256": draft_revision_sha256,
    }
    artifact_index = {
        **index_body,
        "index_sha256": canonical_sha256(index_body),
    }
    (source / "artifact-index.v1.json").write_bytes(
        artifact_index_bytes(artifact_index)
    )
    return {
        "source": source,
        "archive": archive,
        "manifest": formal_manifest,
        "artifact_index": artifact_index,
        "receipt": receipt,
        "requirement_mapping": requirement_mapping,
        "split": split,
        "module": module,
        "design": design,
        "material_references": material_references,
    }


def _build(
    fixture: dict[str, object],
    *,
    manifest: object | None = None,
    mapping: object | None = None,
) -> dict[str, object]:
    return build_reference_index_document(
        fixture["source"],  # type: ignore[arg-type]
        "REQ-001",
        fixture["manifest"] if manifest is None else manifest,  # type: ignore[arg-type]
        archive_root=fixture["archive"],  # type: ignore[arg-type]
        requirement_split=fixture["split"],  # type: ignore[arg-type]
        requirement_mapping=(
            fixture["requirement_mapping"] if mapping is None else mapping
        ),  # type: ignore[arg-type]
        design_references=[fixture["design"]],  # type: ignore[list-item]
        design_artifacts=[fixture["module"]],  # type: ignore[list-item]
        material_references=fixture["material_references"],  # type: ignore[arg-type]
    )


def _rewrite_current_index(fixture: dict[str, object]) -> None:
    """测试修改真实回执后必须同步真实索引，才能单独检验映射合同而非先命中哈希漂移。"""

    source = fixture["source"]  # type: ignore[assignment]
    index = deepcopy(fixture["artifact_index"])
    for item in index["artifacts"]:
        file_path = source / item["source_path"]
        item["sha256"] = sha256_bytes(file_path.read_bytes())
    manifest = formal_manifest_entries(index)
    index["draft_revision_sha256"] = canonical_sha256(
        {
            "draft_id": index["draft_id"],
            "artifact_manifest": manifest,
        }
    )
    body = {
        "schema_version": index["schema_version"],
        "draft_id": index["draft_id"],
        "draft_revision_sha256": index["draft_revision_sha256"],
        "artifacts": index["artifacts"],
    }
    index["index_sha256"] = canonical_sha256(body)
    (source / "artifact-index.v1.json").write_bytes(artifact_index_bytes(index))
    fixture["artifact_index"] = index


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    """失败无残留要同时比较目录、链接目标和文件字节，不能只看目标文件是否生成。"""

    snapshot: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("link", path.readlink().as_posix())
        elif path.is_dir():
            snapshot[relative] = ("directory", "")
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def test_real_archive_builds_all_reference_kinds_without_summary(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    entries = document["entries"]

    assert {
        "FR-001",
        "GR-001",
        "AC-001",
        "DES-001#architecture",
        "SPEC-001",
        "MAT-001",
        "MAT-002",
        "MAT-003",
        "MAT-003#course-list",
        "MAT-004",
        "MAT-004#first-page",
    } <= set(entries)
    assert all(item["path"].startswith("original/") for item in entries.values())
    assert entries["FR-001"]["locator"] == {
        "kind": "json_pointer",
        "value": "/functional_requirements/0",
    }
    assert entries["SPEC-001"]["locator"] == {
        "kind": "json_pointer",
        "value": "/",
    }
    assert entries["MAT-003"]["locator"] == {"kind": "whole_file"}
    assert entries["MAT-003#course-list"]["locator"] == {
        "kind": "design_node",
        "page_id": "Page-A",
        "node_id": "Node-A",
        "node_index_path": "original/原始资料/界面.nodes.v1.json",
        "node_index_sha256": entries["MAT-003#course-list"]["locator"][
            "node_index_sha256"
        ],
        "node_index_schema": "design-node-index.v1",
    }
    assert document["display"]["DES-001#architecture"]["display_name"] == "架构原名"
    assert document["display"]["FR-001"]["heading"] == "重复标题"
    assert document["display"]["GR-001"]["heading"] == "重复标题"
    assert "summary" not in json.dumps(document, ensure_ascii=False).lower()


def test_source_path_is_prevalidated_before_archive_path_is_used(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    source_file = fixture["source"] / "原始资料/需求原文.md"  # type: ignore[operator]
    source_file.write_text("drift", encoding="utf-8")
    with pytest.raises(SdlcError, match="文件哈希不一致"):
        _build(fixture)


def test_source_mode_can_validate_but_cannot_be_written_as_formal_index(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    source_document = build_reference_index_document(
        fixture["source"],  # type: ignore[arg-type]
        "REQ-001",
        fixture["manifest"],  # type: ignore[arg-type]
        requirement_split=fixture["split"],  # type: ignore[arg-type]
        requirement_mapping={
            "gr-login": "GR-001",
            "fr-course": "FR-001",
            "ac-course": "AC-001",
            "src-source": "SRC-001",
        },
    )
    assert source_document["entries"]["FR-001"]["path"] == (
        "需求/requirement-split.v1.json"
    )
    output = fixture["source"] / "reference-index.v1.json"  # type: ignore[operator]
    with pytest.raises(SdlcError, match="不能继续使用 DRAFT source_path"):
        write_reference_index_file(fixture["source"], output, source_document)  # type: ignore[arg-type]
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_path", "../escape.txt", "source_path"),
        ("archive_path", "/tmp/escape.txt", "archive_path"),
        ("archive_path", "other/file.txt", "original"),
    ],
)
def test_manifest_path_boundary_is_strict(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    fixture = _fixture(tmp_path)
    fixture["manifest"][0][field] = value  # type: ignore[index]
    with pytest.raises(SdlcError, match=message):
        _build(fixture)


def test_parent_and_target_symlinks_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    source = fixture["source"]  # type: ignore[assignment]
    target = source / "原始资料/需求原文.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(SdlcError, match="符号链接"):
        _build(fixture)

    fixture = _fixture(tmp_path / "second")
    real_dir = fixture["source"] / "真实资料"  # type: ignore[operator]
    real_dir.mkdir()
    material = fixture["source"] / "原始资料/需求原文.md"  # type: ignore[operator]
    copied = real_dir / "需求原文.md"
    copied.write_bytes(material.read_bytes())
    material.unlink()
    (fixture["source"] / "原始资料").rename(  # type: ignore[union-attr]
        fixture["source"] / "原始资料原目录"  # type: ignore[operator]
    )
    (fixture["source"] / "原始资料").symlink_to(real_dir)  # type: ignore[operator]
    with pytest.raises(SdlcError, match="符号链接"):
        _build(fixture)


def test_duplicate_business_and_formal_ids_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    duplicate = deepcopy(fixture["manifest"][0])  # type: ignore[index]
    duplicate["artifact_id"] = "ART-999"
    duplicate["source_path"] = "原始资料/需求原文副本.md"
    duplicate["archive_path"] = "original/原始资料/需求原文副本.md"
    (fixture["source"] / duplicate["source_path"]).write_bytes(b"course requirement\n")  # type: ignore[operator,index]
    (fixture["archive"] / duplicate["archive_path"]).parent.mkdir(parents=True, exist_ok=True)  # type: ignore[operator,index]
    (fixture["archive"] / duplicate["archive_path"]).write_bytes(b"course requirement\n")  # type: ignore[operator,index]
    fixture["manifest"].append(duplicate)  # type: ignore[union-attr]
    with pytest.raises(SdlcError, match="不一致|没有唯一归档文件|正式引用编号重复"):
        _build(fixture)

    fixture = _fixture(tmp_path / "mapping")
    with pytest.raises(SdlcError, match="不一致|稳定编号|重复正式编号"):
        build_reference_index_document(
            fixture["source"],  # type: ignore[arg-type]
            "REQ-001",
            fixture["manifest"],  # type: ignore[arg-type]
            archive_root=fixture["archive"],  # type: ignore[arg-type]
            requirement_split=fixture["split"],  # type: ignore[arg-type]
            requirement_mapping={
                "gr-login": "GR-001",
                "fr-course": "FR-001",
                "ac-course": "FR-001",
                "src-source": "SRC-001",
            },
        )


def test_duplicate_json_entry_id_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "text.md"
    target.write_text("正文\n", encoding="utf-8")
    digest = sha256_bytes(target.read_bytes())
    index = tmp_path / "reference-index.v1.json"
    entry = (
        '{"schema_version":"reference-locator.v1","path":"text.md","sha256":"'
        + digest
        + '","locator":{"kind":"whole_file"}}'
    )
    index.write_text(
        '{"schema_version":"reference-index.v1","requirement_id":"REQ-001",'
        f'"entries":{{"FR-001":{entry},"FR-001":{entry}}}}}',
        encoding="utf-8",
    )
    with pytest.raises(SdlcError, match="重复字段：FR-001"):
        validate_reference_index_file(tmp_path, index)


def test_text_pointer_file_and_pdf_drift_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    archive = fixture["archive"]  # type: ignore[assignment]

    drifted = deepcopy(document)
    drifted["entries"]["FR-001"]["locator"]["value"] = "/functional_requirements/99"
    with pytest.raises(SdlcError, match="未命中"):
        validate_reference_index_document(archive, drifted)

    drifted = deepcopy(document)
    drifted["entries"]["DES-001#architecture"]["locator"]["fragment_sha256"] = "0" * 64
    with pytest.raises(SdlcError, match="片段内容已经变化"):
        validate_reference_index_document(archive, drifted)

    (archive / "original/原始资料/需求原文.md").write_text("drift", encoding="utf-8")
    with pytest.raises(SdlcError, match="sha256 不一致"):
        validate_reference_index_document(archive, document)

    fixture = _fixture(tmp_path / "pdf")
    document = _build(fixture)
    document["entries"]["MAT-004#first-page"]["locator"]["page"] = 2
    with pytest.raises(SdlcError, match="页码未命中"):
        validate_reference_index_document(fixture["archive"], document)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda node: node.update(schema_version="design-node-index.v2"), "版本|Schema"),
        (lambda node: node.update(design_path="original/错误.png"), "design_path"),
        (lambda node: node.update(design_sha256="0" * 64), "design_sha256"),
        (
            lambda node: node["pages"].append(deepcopy(node["pages"][0])),
            "重复 page_id",
        ),
        (
            lambda node: node["pages"][0]["nodes"].append(
                deepcopy(node["pages"][0]["nodes"][0])
            ),
            "重复 node_id",
        ),
    ],
)
def test_design_node_contract_rejects_invalid_index(
    tmp_path: Path, mutation, message: str
) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    archive = fixture["archive"]  # type: ignore[assignment]
    index_path = archive / "original/原始资料/界面.nodes.v1.json"
    node = json.loads(index_path.read_text(encoding="utf-8"))
    mutation(node)
    index_path.write_text(canonical_json_text(node), encoding="utf-8")
    entry = document["entries"]["MAT-003#course-list"]
    entry["locator"]["node_index_sha256"] = sha256_bytes(index_path.read_bytes())
    with pytest.raises(SdlcError, match=message):
        validate_reference_index_document(archive, document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("page_id", "page-a", "page_id"),
        ("node_id", "node-a", "node_id"),
    ],
)
def test_design_node_matching_is_case_sensitive(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    document["entries"]["MAT-003#course-list"]["locator"][field] = value
    with pytest.raises(SdlcError, match=message):
        validate_reference_index_document(fixture["archive"], document)  # type: ignore[arg-type]


def test_design_node_main_and_index_hash_drift_are_independent(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    archive = fixture["archive"]  # type: ignore[assignment]
    main = archive / "original/原始资料/界面.png"
    main.write_bytes(main.read_bytes() + b"x")
    with pytest.raises(SdlcError, match="sha256 不一致"):
        validate_reference_index_document(archive, document)

    fixture = _fixture(tmp_path / "index")
    document = _build(fixture)
    index = fixture["archive"] / "original/原始资料/界面.nodes.v1.json"  # type: ignore[operator]
    index.write_bytes(index.read_bytes() + b" ")
    with pytest.raises(SdlcError, match="node_index_sha256 不一致"):
        validate_reference_index_document(fixture["archive"], document)  # type: ignore[arg-type]


def test_invalid_pdf_locator_and_binary_fine_grained_guess_are_rejected(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    bad_reference = [
        {
            "reference_id": "MAT-003#guessed",
            "material_id": "MAT-003",
            "locator": {
                "kind": "text_range",
                "line_start": 1,
                "line_end": 1,
                "fragment_sha256": "0" * 64,
            },
        }
    ]
    with pytest.raises(SdlcError, match="普通二进制资料"):
        build_reference_index_document(
            fixture["source"],  # type: ignore[arg-type]
            "REQ-001",
            fixture["manifest"],  # type: ignore[arg-type]
            archive_root=fixture["archive"],  # type: ignore[arg-type]
            requirement_split=fixture["split"],  # type: ignore[arg-type]
            requirement_mapping={
                "gr-login": "GR-001",
                "fr-course": "FR-001",
                "ac-course": "AC-001",
                "src-source": "SRC-001",
            },
            material_references=bad_reference,
        )

    document = _build(fixture)
    document["entries"]["MAT-004#first-page"]["locator"].pop("page")
    with pytest.raises(SdlcError, match="Schema 校验失败"):
        validate_reference_index_document(fixture["archive"], document)  # type: ignore[arg-type]


def test_failed_generation_and_write_leave_no_residue(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    output = fixture["archive"] / "reference-index.v1.json"  # type: ignore[operator]
    before = sorted(path.relative_to(fixture["archive"]).as_posix() for path in fixture["archive"].rglob("*"))  # type: ignore[union-attr]
    bad = _build(fixture)
    bad["entries"]["FR-001"]["sha256"] = "0" * 64
    with pytest.raises(SdlcError):
        write_reference_index_file(fixture["archive"], output, bad)  # type: ignore[arg-type]
    after = sorted(path.relative_to(fixture["archive"]).as_posix() for path in fixture["archive"].rglob("*"))  # type: ignore[union-attr]
    assert before == after
    assert not output.exists()
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))


def test_doctor_reports_drift_without_repairing_index(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    project = tmp_path / "project"
    requirement = project / ".codex-sdlc/requirements/REQ-001-课程"
    requirement.mkdir(parents=True)
    for child in fixture["archive"].iterdir():  # type: ignore[union-attr]
        if child.is_dir():
            import shutil

            shutil.copytree(child, requirement / child.name)
    index_path = requirement / "reference-index.v1.json"
    index_path.write_text(canonical_json_text(document), encoding="utf-8")
    paths = build_paths(project)
    passed, failed = formal_reference_index_checks(paths)
    assert failed == []
    assert any("正式引用索引有效" in item for item in passed)

    before = index_path.read_bytes()
    (requirement / "original/原始资料/界面.png").write_bytes(b"drift")
    passed, failed = formal_reference_index_checks(paths)
    assert passed == []
    assert any("sha256 不一致" in item for item in failed)
    assert index_path.read_bytes() == before

    missing_project = tmp_path / "missing-project"
    missing_requirement = (
        missing_project / ".codex-sdlc/requirements/REQ-002-缺少索引"
    )
    formal = missing_requirement / "original/formal.v3.json"
    formal.parent.mkdir(parents=True)
    formal.write_text(
        canonical_json_text(
            {
                "formal_contract_version": "formal.v3",
                "workflow_profile": "document-first.v1",
            }
        ),
        encoding="utf-8",
    )
    passed, failed = formal_reference_index_checks(build_paths(missing_project))
    assert passed == []
    assert failed == ["REQ-002-缺少索引 缺少 reference-index.v1.json"]


def test_requirement_mapping_must_equal_current_receipt_without_residue(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    archive = fixture["archive"]  # type: ignore[assignment]
    before = _tree_bytes(archive)
    wrong = {
        "gr-login": "GR-099",
        "fr-course": "FR-099",
        "ac-course": "AC-099",
        "src-source": "SRC-001",
    }
    with pytest.raises(SdlcError, match="当前真实需求导入回执不一致"):
        _build(fixture, mapping=wrong)
    assert _tree_bytes(archive) == before
    assert not (archive / "reference-index.v1.json").exists()


@pytest.mark.parametrize(
    "mapping",
    [
        {
            "gr-login": "GR-001",
            "fr-course": "FR-001",
            "ac-course": "AC-001",
        },
        {
            "gr-login": "GR-001",
            "fr-course": "FR-001",
            "ac-course": "AC-001",
            "src-source": "SRC-001",
            "src-extra": "SRC-002",
        },
        {
            "gr-drift": "GR-001",
            "fr-course": "FR-001",
            "ac-course": "AC-001",
            "src-source": "SRC-001",
        },
        {
            "gr-login": "GR-001",
            "fr-course": "FR-001",
            "ac-course": "FR-001",
            "src-source": "SRC-001",
        },
    ],
)
def test_requirement_mapping_missing_extra_client_key_and_duplicate_are_rejected(
    tmp_path: Path,
    mapping: dict[str, str],
) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(SdlcError, match="不一致"):
        _build(fixture, mapping=mapping)


@pytest.mark.parametrize("mode", ["missing", "damaged"])
def test_missing_or_damaged_current_requirement_receipt_is_rejected(
    tmp_path: Path,
    mode: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt_path = fixture["source"] / "需求/需求导入回执.json"  # type: ignore[operator]
    if mode == "missing":
        receipt_path.unlink()
    else:
        receipt_path.write_text("{", encoding="utf-8")
    with pytest.raises(SdlcError, match="需求导入回执|artifact-index"):
        _build(fixture)


@pytest.mark.parametrize("mode", ["client_key", "duplicate_id"])
def test_current_receipt_client_key_drift_and_duplicate_id_are_rejected(
    tmp_path: Path,
    mode: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = deepcopy(fixture["receipt"])
    mapping = receipt["mapping"]
    if mode == "client_key":
        mapping["gr-drift"] = mapping.pop("gr-login")
    else:
        mapping["ac-course"] = mapping["fr-course"]
    receipt_path = fixture["source"] / "需求/需求导入回执.json"  # type: ignore[operator]
    receipt_path.write_text(canonical_json_text(receipt), encoding="utf-8")
    _rewrite_current_index(fixture)
    with pytest.raises(SdlcError, match="回执|编号映射|稳定编号"):
        _build(fixture, mapping=mapping)


@pytest.mark.parametrize("same_bytes", [True, False])
def test_requirement_receipt_type_must_be_globally_unique_without_residue(
    tmp_path: Path,
    same_bytes: bool,
) -> None:
    fixture = _fixture(tmp_path)
    source = fixture["source"]  # type: ignore[assignment]
    second_path = source / "需求/另一个回执.json"
    if same_bytes:
        second_path.write_bytes((source / "需求/需求导入回执.json").read_bytes())
    else:
        second_receipt = deepcopy(fixture["receipt"])
        second_receipt["event_id"] = "EVT-T016-SECOND"
        second_path.write_text(canonical_json_text(second_receipt), encoding="utf-8")
    index = deepcopy(fixture["artifact_index"])
    receipt_item = next(
        item
        for item in index["artifacts"]
        if item["artifact_type"] == "requirement_import_receipt"
    )
    second_item = deepcopy(receipt_item)
    second_item.update(
        artifact_id="ART-999",
        source_path="需求/另一个回执.json",
        archive_path="original/需求/另一个回执.json",
        sha256=sha256_bytes(second_path.read_bytes()),
    )
    index["artifacts"].append(second_item)
    index["artifacts"].sort(key=lambda item: item["source_path"])
    fixture["artifact_index"] = index
    _rewrite_current_index(fixture)

    before = _tree_snapshot(tmp_path)
    with pytest.raises(SdlcError, match="没有唯一需求导入回执记录"):
        _build(fixture)
    assert _tree_snapshot(tmp_path) == before
    assert not (fixture["archive"] / "reference-index.v1.json").exists()  # type: ignore[operator]


def test_single_fixed_requirement_receipt_still_generates(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture)
    assert {"AC-001", "FR-001", "GR-001"} <= set(document["entries"])


@pytest.mark.parametrize("mode", ["wrong_path", "wrong_include", "missing_record"])
def test_single_requirement_receipt_registration_must_keep_fixed_contract(
    tmp_path: Path,
    mode: str,
) -> None:
    fixture = _fixture(tmp_path)
    source = fixture["source"]  # type: ignore[assignment]
    archive = fixture["archive"]  # type: ignore[assignment]
    index = deepcopy(fixture["artifact_index"])
    receipt_item = next(
        item
        for item in index["artifacts"]
        if item["artifact_type"] == "requirement_import_receipt"
    )
    if mode == "wrong_path":
        wrong_path = source / "需求/错误回执.json"
        wrong_path.write_bytes((source / "需求/需求导入回执.json").read_bytes())
        receipt_item["source_path"] = "需求/错误回执.json"
        receipt_item["archive_path"] = "original/需求/错误回执.json"
    elif mode == "wrong_include":
        receipt_item["include_in_formal"] = True
        receipt_item["review_relations"]["applies_to"] = [
            {"stage": "requirement_split", "owner_id": "DRAFT-001"}
        ]
        archive_receipt = archive / receipt_item["archive_path"]
        archive_receipt.parent.mkdir(parents=True, exist_ok=True)
        archive_receipt.write_bytes((source / receipt_item["source_path"]).read_bytes())
    else:
        index["artifacts"].remove(receipt_item)
    index["artifacts"].sort(key=lambda item: item["source_path"])
    fixture["artifact_index"] = index
    _rewrite_current_index(fixture)
    manifest = formal_manifest_entries(fixture["artifact_index"])
    with pytest.raises(SdlcError, match="回执"):
        _build(fixture, manifest=manifest)


def test_requirement_receipt_hash_drift_is_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receipt_path = fixture["source"] / "需求/需求导入回执.json"  # type: ignore[operator]
    receipt = deepcopy(fixture["receipt"])
    receipt["event_id"] = "EVT-T016-DRIFT"
    receipt_path.write_text(canonical_json_text(receipt), encoding="utf-8")
    with pytest.raises(SdlcError, match="文件哈希不一致"):
        _build(fixture)


def test_same_bytes_fake_manifest_paths_are_rejected_without_residue(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    manifest = deepcopy(fixture["manifest"])
    source = fixture["source"]  # type: ignore[assignment]
    archive = fixture["archive"]  # type: ignore[assignment]
    original = source / manifest[0]["source_path"]
    fake_source = source / "伪路径/同名.md"
    fake_archive = archive / "original/伪路径/同名.md"
    fake_source.parent.mkdir(parents=True)
    fake_archive.parent.mkdir(parents=True)
    fake_source.write_bytes(original.read_bytes())
    fake_archive.write_bytes(original.read_bytes())
    manifest[0]["source_path"] = "伪路径/同名.md"
    manifest[0]["archive_path"] = "original/伪路径/同名.md"
    before = _tree_bytes(archive)
    with pytest.raises(SdlcError, match="当前真实 artifact-index.v1 不一致"):
        _build(fixture, manifest=manifest)
    assert _tree_bytes(archive) == before
    assert not (archive / "reference-index.v1.json").exists()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "不一致"),
        ("extra", "不一致"),
        ("duplicate_art", "ART 编号"),
        ("business_id", "不一致"),
        ("source_path", "不一致"),
        ("archive_path", "不一致"),
        ("artifact_type", "不一致"),
        ("include_in_formal", "未批准归档"),
        ("sha256", "不一致"),
    ],
)
def test_manifest_collection_and_fields_must_equal_current_artifact_index(
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    fixture = _fixture(tmp_path)
    manifest = deepcopy(fixture["manifest"])
    if mode == "missing":
        manifest.pop()
    elif mode == "extra":
        extra = deepcopy(manifest[0])
        extra.update(
            artifact_id="ART-999",
            source_path="原始资料/多余.md",
            archive_path="original/原始资料/多余.md",
        )
        manifest.append(extra)
    elif mode == "duplicate_art":
        extra = deepcopy(manifest[0])
        extra.update(
            source_path="原始资料/重复.md",
            archive_path="original/原始资料/重复.md",
        )
        manifest.append(extra)
    elif mode == "business_id":
        manifest[0]["business_id"] = "MAT-999"
    elif mode == "source_path":
        manifest[0]["source_path"] = "原始资料/错误.md"
    elif mode == "archive_path":
        manifest[0]["archive_path"] = "original/原始资料/错误.md"
    elif mode == "artifact_type":
        manifest[0]["artifact_type"] = "other"
    elif mode == "include_in_formal":
        manifest[0]["include_in_formal"] = False
    else:
        manifest[0]["sha256"] = "0" * 64
    with pytest.raises(SdlcError, match=message):
        _build(fixture, manifest=manifest)


def test_equal_current_artifact_index_object_can_generate_reference_index(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    document = _build(fixture, manifest=fixture["artifact_index"])
    assert document["entries"]["FR-001"]["path"].startswith("original/")


def test_stale_deleted_and_symlink_artifact_index_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "stale")
    index_path = fixture["source"] / "artifact-index.v1.json"  # type: ignore[operator]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["index_sha256"] = "0" * 64
    index_path.write_text(canonical_json_text(index), encoding="utf-8")
    with pytest.raises(SdlcError, match="索引哈希"):
        _build(fixture)

    fixture = _fixture(tmp_path / "missing")
    index_path = fixture["source"] / "artifact-index.v1.json"  # type: ignore[operator]
    index_path.unlink()
    with pytest.raises(SdlcError, match="artifact-index"):
        _build(fixture)

    fixture = _fixture(tmp_path / "symlink")
    index_path = fixture["source"] / "artifact-index.v1.json"  # type: ignore[operator]
    real_index = tmp_path / "outside-artifact-index.v1.json"
    real_index.write_bytes(index_path.read_bytes())
    index_path.unlink()
    index_path.symlink_to(real_index)
    with pytest.raises(SdlcError, match="符号链接"):
        _build(fixture)
