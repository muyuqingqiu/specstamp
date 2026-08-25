from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys


TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import fact_artifacts, fact_schema
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state, refresh_materialized_state
from codex_sdlc.core.structured_contract import sha256_bytes
from formal_package_factory import build_valid_formal_v3_bundle, formal_v2_package
from test_cli_v1 import init_demo_repo, run_cli_raw
from tests.contracts.test_model_fact_cli_flow import install_historical_fact_archive


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_historical_fact_archive_refresh_only_rebuilds_readable_projections(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    requirement_dir = install_historical_fact_archive(project_dir)
    original_before = _file_hashes(requirement_dir / "original")

    state = refresh_materialized_state(build_paths(project_dir))

    assert state["requirements"]["REQ-001"]["native_start"]["migration_status"] == "legacy_read_only"
    assert (requirement_dir / "effective/requirement.current.json").is_file()
    assert _file_hashes(requirement_dir / "original") == original_before


def test_historical_fact_archive_repair_does_not_turn_it_into_document_first(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    requirement_dir = install_historical_fact_archive(project_dir)
    before = _file_hashes(requirement_dir / "original")

    repaired = run_cli_raw(["doctor-repair"], cwd=project_dir)

    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    formal = json.loads((requirement_dir / "original/formal.v3.json").read_text(encoding="utf-8"))
    assert "workflow_profile" not in formal
    assert _file_hashes(requirement_dir / "original") == before


def test_historical_fact_state_keeps_fact_bundle_out_of_new_profile_fields(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    install_historical_fact_archive(project_dir)

    requirement = derive_state(build_paths(project_dir))["requirements"]["REQ-001"]

    assert requirement["native_start"]["formal_contract_version"] == "formal.v3"
    assert "fact_bundle" in requirement["native_start"]
    assert "workflow_profile" not in requirement["native_start"]


def test_historical_mixed_state_and_data_facts_remain_valid_read_contracts() -> None:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    requirement = deepcopy(bundle["requirement"])
    state_fact = next(
        fact
        for fact in requirement["semantic"]["facts"]
        if fact["category"] == "state_transition"
    )
    data_fact = deepcopy(state_fact)
    data_fact["fact_id"] = "RF-HISTORY-DATA"
    data_fact["category"] = "data_change"
    data_fact["normalized"] = {"entity": "订单", "change": "记录操作人"}
    requirement["semantic"]["facts"].append(data_fact)
    requirement["semantic_sha256"] = fact_artifacts.semantic_sha256(requirement["semantic"])
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)

    assert fact_schema.fact_document_issues(requirement, owner="requirement") == []
