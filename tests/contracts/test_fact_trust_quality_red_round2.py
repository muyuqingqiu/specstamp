from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import sys

import pytest

# 该合同测试仍会读取 tests 根目录中的历史只读夹具；把夹具目录显式加入导入路径，
# 让任务规定的 PYTHONPATH=src 命令也能单独运行，不改变 facts 的业务断言。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_sdlc.core import fact_artifacts, fact_review_trust, fact_schema
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from formal_package_factory import (
    build_valid_draft_fact_bundle,
    build_valid_formal_v3_bundle,
    formal_business_from_draft,
    formal_v2_package,
    write_formal_v3_from_draft,
)
from test_cli_v1 import init_demo_repo, run_cli_raw
from test_cli_v15_draft import prepare_complete_order_delete_draft
from tests.contracts.test_contract_cli_regressions import _ready_project, _write_package


def _valid_draft_with_real_review(project_dir: Path, draft_id: str = "DRAFT-001") -> dict:
    assert run_cli_raw(["draft", "source-index", draft_id], cwd=project_dir).returncode == 0
    draft = derive_state(build_paths(project_dir))["drafts"][draft_id]
    bundle = build_valid_draft_fact_bundle(draft)
    fixture_dir = project_dir / f".round2-fixtures-{draft_id.lower()}"
    fixture_dir.mkdir(exist_ok=True)
    for owner in ("requirement", "design"):
        path = fixture_dir / f"{owner}.facts.json"
        fact_artifacts.write_json(path, bundle[owner])
        result = run_cli_raw(
            ["draft", "facts", draft_id, "--kind", owner, "--file", str(path)],
            cwd=project_dir,
            extra_env={"CODEX_THREAD_ID": "round2-producer"},
        )
        assert result.returncode == 0, result.stderr
    request = run_cli_raw(
        ["draft", "review-request", draft_id],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "round2-producer"},
    )
    assert request.returncode == 0, request.stderr
    review_path = fixture_dir / "model-review.json"
    fact_artifacts.write_json(review_path, bundle["review"])
    submitted = run_cli_raw(
        ["draft", "model-review", draft_id, "--file", str(review_path)],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "round2-reviewer"},
    )
    assert submitted.returncode == 0, submitted.stderr
    return derive_state(build_paths(project_dir))["drafts"][draft_id]


@pytest.mark.parametrize("damage", ["missing-registry", "bad-signature", "other-draft"])
def test_draft_read_paths_revalidate_managed_receipt(tmp_path: Path, damage: str) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    prepare_complete_order_delete_draft(project_dir)
    _valid_draft_with_real_review(project_dir)
    paths = build_paths(project_dir)
    baseline = run_cli_raw(["draft", "status", "DRAFT-001"], cwd=project_dir)
    assert baseline.returncode == 0 and "facts_passed" in baseline.stdout
    registry_path = paths.sdlc_dir / "trust" / "fact-reviews" / "registry.json"
    if damage == "missing-registry":
        shutil.rmtree(paths.sdlc_dir / "trust")
    else:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        receipt = next(iter(registry["receipts"].values()))
        if damage == "bad-signature":
            receipt["signature"] = "0" * 64
        else:
            # 即使登记表里存在签名正确的其他 DRAFT 回执，也不能把它当成当前 DRAFT 的授权。
            receipt["draft_id"] = "DRAFT-002"
            body = {name: value for name, value in receipt.items() if name != "signature"}
            key = (paths.sdlc_dir / "trust" / "fact-reviews" / ".key").read_bytes()
            receipt["signature"] = hmac.new(
                key, fact_artifacts.canonical_json_bytes(body), hashlib.sha256
            ).hexdigest()
        registry_path.write_text(fact_artifacts.canonical_json_text(registry), encoding="utf-8")

    status = run_cli_raw(["draft", "status", "DRAFT-001"], cwd=project_dir)
    next_result = run_cli_raw(["next"], cwd=project_dir)
    start = run_cli_raw(["start"], cwd=project_dir)
    doctor = run_cli_raw(["doctor-deep"], cwd=project_dir)
    assert "facts_passed" not in status.stdout and "start_ready" not in status.stdout
    assert "$sdlc-start" not in next_result.stdout
    assert "DRAFT 事实已经通过" not in start.stderr
    assert doctor.returncode != 0


def test_production_trust_module_has_no_test_receipt_backdoor() -> None:
    assert not hasattr(fact_review_trust, "install_test_receipt")
    source = Path(fact_review_trust.__file__).read_text(encoding="utf-8")
    assert "install_test_receipt" not in source


def test_one_requirement_can_have_two_compatible_implementation_relations() -> None:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    requirement = bundle["requirement"]
    design = bundle["design"]
    permission = next(item for item in requirement["semantic"]["facts"] if item["category"] == "permission")
    first_design = next(item for item in design["semantic"]["facts"] if item["category"] == "permission_enforcement")
    second_design = deepcopy(first_design)
    second_design["fact_id"] = "DF-9998"
    second_design["statement"] = "服务层再次执行同一权限校验。"
    design["semantic"]["facts"].append(second_design)
    design["semantic"]["relations"].append(
        {"type": "implements", "requirement_fact_id": permission["fact_id"], "design_fact_id": second_design["fact_id"]}
    )
    requirement_ids = {item["fact_id"] for item in requirement["semantic"]["facts"]}
    assert fact_schema.fact_document_issues(design, owner="design", requirement_fact_ids=requirement_ids) == []


def test_fixture_maps_repeated_high_risk_facts_to_distinct_current_design_facts() -> None:
    business = formal_v2_package()
    business["permission_rules"].append("只允许审计员查看审批记录。")
    business["design"]["permissions_security"].append("服务层只允许审计员查看审批记录。")
    _formal, bundle = build_valid_formal_v3_bundle(business)
    requirement = bundle["requirement"]
    design = bundle["design"]
    permission_ids = {item["fact_id"] for item in requirement["semantic"]["facts"] if item["category"] == "permission"}
    related_design_ids = {
        item["design_fact_id"]
        for item in design["semantic"]["relations"]
        if item["requirement_fact_id"] in permission_ids
    }
    assert len(permission_ids) >= 2
    assert len(related_design_ids) >= 2


@pytest.mark.parametrize("decision_refs", [[], ["RU-0001", "RU-0001"]])
def test_non_contractual_approval_requires_nonempty_unique_decisions(decision_refs: list[str]) -> None:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    review = bundle["review"]
    review["non_contractual_approvals"] = [
        {
            "approval_id": "AP-0001",
            "unit_id": "RU-0001",
            "owner": "requirement",
            "reason": "用户决定该重复说明不形成额外合同事实。",
            "decision_refs": decision_refs,
        }
    ]
    assert fact_schema.review_document_issues(review)


@pytest.mark.parametrize("certainty", ["confirmed", "inferred"])
def test_draft_decision_refs_materialize_by_decision_content_and_keep_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certainty: str,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    prepare_complete_order_delete_draft(project_dir)
    decision_text = "用户确认订单删除必须保留操作人。"
    assert run_cli_raw(["draft", "decision", "DRAFT-001", decision_text], cwd=project_dir).returncode == 0
    assert run_cli_raw(["draft", "source-index", "DRAFT-001"], cwd=project_dir).returncode == 0
    draft = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    bundle = build_valid_draft_fact_bundle(draft)
    decision_unit = next(
        item for item in bundle["index"]["units"]
        if item["unit_id"].startswith("CU-") and decision_text in item["quote"]
    )
    requirement = bundle["requirement"]
    requirement["semantic"]["facts"][0]["certainty"] = certainty
    requirement["semantic"]["facts"][0]["decision_refs"] = [decision_unit["unit_id"]]
    requirement["semantic_sha256"] = fact_artifacts.semantic_sha256(requirement["semantic"])
    requirement["artifact_sha256"] = fact_artifacts.artifact_sha256(requirement)
    review = fact_artifacts.build_review_artifact(requirement, bundle["design"], requirement["context_targets"], status="passed", issues=[])
    fixture_dir = project_dir / ".decision-fixtures"
    fixture_dir.mkdir()
    for owner, document in (("requirement", requirement), ("design", bundle["design"])):
        path = fixture_dir / f"{owner}.facts.json"
        fact_artifacts.write_json(path, document)
        result = run_cli_raw(
            ["draft", "facts", "DRAFT-001", "--kind", owner, "--file", str(path)],
            cwd=project_dir,
            extra_env={"CODEX_THREAD_ID": "decision-producer"},
        )
        assert result.returncode == 0, result.stderr
    request = run_cli_raw(
        ["draft", "review-request", "DRAFT-001"], cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "decision-producer"},
    )
    assert request.returncode == 0, request.stderr
    review_path = fixture_dir / "model-review.json"
    fact_artifacts.write_json(review_path, review)
    submitted = run_cli_raw(
        ["draft", "model-review", "DRAFT-001", "--file", str(review_path)], cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "decision-reviewer"},
    )
    assert submitted.returncode == 0, submitted.stderr
    draft = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    package = project_dir / "formal.v3.json"
    write_formal_v3_from_draft(
        package, formal_business_from_draft(draft), draft, install_receipt=False
    )
    formal_requirement = json.loads(package.with_name("requirement.facts.json").read_text(encoding="utf-8"))
    formal_index = json.loads(package.with_name("source-index.json").read_text(encoding="utf-8"))
    expected_formal_decision = next(
        item["unit_id"] for item in formal_index["units"]
        if item["section_key"] == "decisions" and item["quote"] == decision_text
    )
    assert formal_requirement["semantic_sha256"] == requirement["semantic_sha256"]
    assert formal_requirement["semantic"]["facts"][0]["decision_refs"] == [expected_formal_decision]

    # 上面的 facts 产物只承担历史只读兼容断言。正式写入改用当前
    # document-first.v1 包，避免测试重新开放已经下线的 facts 写入入口。
    current_root = tmp_path / "当前文档优先正式写入"
    current_root.mkdir()
    current_project, current_paths, current_package = _ready_project(
        current_root,
        monkeypatch,
    )
    assert current_package["workflow_profile"] == "document-first.v1"
    current_package_file = current_project / "formal.v3.json"
    _write_package(current_package_file, current_package)
    started = run_cli_raw(["start", "--file", str(current_package_file)], cwd=current_project)
    assert started.returncode == 0, started.stderr
    archived_package = json.loads(
        (current_paths.requirements_dir / "REQ-001/original/formal.v3.json").read_text(
            encoding="utf-8"
        )
    )
    assert archived_package == current_package


@pytest.mark.parametrize("attack", ["missing-requirement", "relation-mismatch"])
def test_requirement_coverage_machine_refs_match_current_relation(attack: str) -> None:
    _formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())
    requirement = bundle["requirement"]
    design = bundle["design"]
    coverage_fact = next(item for item in design["semantic"]["facts"] if item["category"] == "requirement_coverage")
    if attack == "missing-requirement":
        coverage_fact["normalized"]["requirement_fact_id"] = "RF-9999"
    else:
        existing = [item["fact_id"] for item in requirement["semantic"]["facts"]]
        related = {
            item["requirement_fact_id"] for item in design["semantic"]["relations"]
            if item["design_fact_id"] == coverage_fact["fact_id"]
        }
        coverage_fact["normalized"]["requirement_fact_id"] = next(item for item in existing if item not in related)
    requirement_ids = {item["fact_id"] for item in requirement["semantic"]["facts"]}
    assert fact_schema.fact_document_issues(design, owner="design", requirement_fact_ids=requirement_ids)
