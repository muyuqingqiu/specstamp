from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
for import_path in (SRC_ROOT, TESTS_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import load_events
from codex_sdlc.core.structured_contract import validate_schema_document
from test_change_package_contract import (
    _formal_project,
    _source_contracts,
    _submit,
    _write_sources,
)


@pytest.mark.parametrize("mutation", ["少项", "多项", "错引用", "错内容哈希"])
def test_full_projection_rejects_any_non_equivalent_content(tmp_path: Path, mutation: str) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    documents = _source_contracts(project, status)
    target = documents["projected-reference-index.v2.json"]
    if mutation == "少项":
        del target["content"]["entries"]["FR-002"]
    elif mutation == "多项":
        target["content"]["entries"]["FR-999"] = deepcopy(target["content"]["entries"]["FR-002"])
    elif mutation == "错引用":
        target["content"]["entries"]["FR-002"]["path"] = "错误.json"
    else:
        target["content_sha256"] = "0" * 64
    if mutation != "错内容哈希":
        from codex_sdlc.core.structured_contract import canonical_sha256

        target["content_sha256"] = canonical_sha256(target["content"])
    paths = _write_sources(project, documents)
    with pytest.raises(SdlcError):
        _submit(project, paths)
    workspace = project / status["workspace_path"]
    assert not (workspace / "change-package.v1.json").exists()
    assert not any(
        item.get("event_type") == "change_package_projected"
        for item in load_events(build_paths(project))
    )


def test_rejects_nonempty_questions_and_different_identity_after_success(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    documents = _source_contracts(project, status)
    paths = _write_sources(project, documents)
    _submit(project, paths)

    changed = deepcopy(documents)
    changed["change-package.v1.json"]["reason"] = "同一 CHG 的另一份输入"
    changed_paths = _write_sources(project, changed)
    with pytest.raises(SdlcError, match="不同身份"):
        _submit(project, changed_paths)

    second_root = tmp_path / "第二个"
    second_root.mkdir()
    fresh_project, _requirement_dir, fresh_status = _formal_project(second_root)
    unresolved = _source_contracts(fresh_project, fresh_status)
    unresolved["change-package.v1.json"]["open_questions"] = [{"question": "是否继续"}]
    unresolved_paths = _write_sources(fresh_project, unresolved)
    with pytest.raises(SdlcError, match="open_questions"):
        _submit(fresh_project, unresolved_paths)


def test_five_projected_schemas_and_artifact_content_are_strict() -> None:
    base = {"path": "base.json", "sha256": "a" * 64}
    invalid_contents = {
        "projected-requirement.v2": {"任意": True},
        "projected-design.v2": {"任意": True},
        "projected-test-matrix.v2": {"任意": True},
        "projected-reference-index.v2": {
            "schema_version": "reference-index.v1",
            "requirement_id": "REQ-001",
            "entries": {},
        },
        "projected-task-plan.v2": {
            "schema_version": "task-plan.v2",
            "requirement_id": "REQ-001",
            "producer_run_id": "run-invalid",
            "input_hashes": {},
            "tasks": ["T-001"],
            "dependencies": [],
            "mapping": {"base": "T-001"},
            "status": "done",
        },
    }
    from codex_sdlc.core.structured_contract import canonical_sha256

    for schema_name, content in invalid_contents.items():
        document = {
            "schema_version": schema_name,
            "requirement_id": "REQ-001",
            "change_id": "CHG-001",
            "base": base,
            "content": content,
            "content_sha256": canonical_sha256(content),
        }
        with pytest.raises(SdlcError):
            validate_schema_document(document, schema_name=schema_name)

    from codex_sdlc.core.change_contract import _validate_next_value

    invalid_page = {
        "schema_version": "design-artifact.v1",
        "type": "page",
        "requirement_refs": ["FR-001"],
        "global_rule_refs": [],
        "material_refs": ["MAT-001"],
        "depends_on": [],
        "content": {"totally_unknown": True},
        "open_questions": [],
    }
    with pytest.raises(SdlcError):
        _validate_next_value("ART", invalid_page)
