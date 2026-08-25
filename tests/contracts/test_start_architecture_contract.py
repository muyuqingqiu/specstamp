from __future__ import annotations

from pathlib import Path
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.services import start_service
from test_contract_cli_regressions import (
    _args,
    _ready_project,
    _snapshot,
    _write_package,
)


ROOT = Path(__file__).resolve().parents[2]


def test_start_command_is_thin_and_documents_file_only_entry() -> None:
    command = (
        ROOT / "src/codex_sdlc/commands/start_cmd.py"
    ).read_text(encoding="utf-8")

    assert "start_service.start(" in command
    assert "document-first.v1 formal.v3" in command
    assert "start_contract" not in command
    assert "append_event" not in command
    assert "generate_requirement_folder" not in command


def test_document_first_preflight_has_fixed_read_only_order() -> None:
    contract = (
        ROOT / "src/codex_sdlc/core/start_contract.py"
    ).read_text(encoding="utf-8")
    body = contract.split("def preflight_document_first_start(", 1)[1]
    ordered_calls = [
        "_document_first_schema(package)",
        "_explicit_draft(current_state, candidate)",
        "_ready_status(draft)",
        "_reviews_and_confirmation(draft, candidate)",
        "_revision_matches(paths, draft, candidate)",
        "validate_formal_package_contract(",
        "_review_inputs_match_manifest(validated, reviews)",
        "_reference_index(",
        'validated.get("open_questions")',
        "_validate_requirement_target(",
    ]

    positions = [body.index(call) for call in ordered_calls]
    assert positions == sorted(positions)
    for forbidden in (
        "append_event(",
        "refresh_materialized_state(",
        "mkdir(",
        "write_text(",
        "write_bytes(",
        "project_lock(",
        "generate_requirement_folder(",
    ):
        assert forbidden not in body


def test_document_first_start_does_not_call_fact_gate_or_legacy_writer() -> None:
    service = (
        ROOT / "src/codex_sdlc/services/start_service.py"
    ).read_text(encoding="utf-8")
    native_body = service.split("def run_native_start(", 1)[1].split(
        "def run_legacy_fact_start(", 1
    )[0]

    assert "preflight_document_first_start" in native_body
    assert "load_verified_formal_bundle" not in native_body
    assert "create_native_requirement_from_package" not in native_body
    assert "append_event" not in native_body
    assert "write_verified_bundle" not in native_body


def test_fact_gate_keeps_saved_bundle_read_only_check() -> None:
    fact_gate = (
        ROOT / "src/codex_sdlc/core/fact_gate.py"
    ).read_text(encoding="utf-8")

    assert "def check_saved_bundle_integrity(" in fact_gate
    assert "class FactGate:" in fact_gate
    assert "fact_bundle" in fact_gate


def test_start_contract_comment_explains_prewrite_source_and_hash_recheck() -> None:
    contract = (
        ROOT / "src/codex_sdlc/core/start_contract.py"
    ).read_text(encoding="utf-8")

    assert "必须在任何写入前显式复核" in contract
    assert "旧缓存" in contract
    assert "留下错误编号、目录或业务状态" in contract


def test_revision_error_is_observable_before_review_input_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    package["source_revision_sha256"] = "0" * 64
    package_file = tmp_path / "架构顺序旧修订包.json"
    _write_package(package_file, package)
    review_item = next(
        item
        for item in package["artifact_manifest"]
        if item["artifact_type"] == "integrated_design_review_input"
    )
    review_path = paths.draft_dir("DRAFT-001") / review_item["source_path"]
    review_path.write_bytes(review_path.read_bytes() + b"\n")
    monkeypatch.chdir(project)
    before = _snapshot(project)

    with pytest.raises(SdlcError, match="formal.v3 引用的 DRAFT 修订已经过期"):
        start_service.start(_args(package_file))

    assert _snapshot(project) == before
