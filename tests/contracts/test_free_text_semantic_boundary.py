from __future__ import annotations

from copy import deepcopy

from codex_sdlc.core import draft_lifecycle, fact_artifacts, fact_gate
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from formal_package_factory import (
    build_valid_draft_fact_bundle,
    formal_business_from_draft,
    install_valid_draft_facts,
    write_formal_v3_from_draft,
)
from test_cli_v1 import init_demo_repo, run_cli_raw
from test_cli_v15_draft import write_complete_order_delete_draft_files


复合权限句 = "管理员可以查看订单，并在必要时下载发票。"


def _prepare_draft(project_dir, requirement_permission: str, design_permission: str) -> dict[str, object]:
    assert run_cli_raw(["draft", "create", "复合权限确认稿"], cwd=project_dir).returncode == 0
    requirement_file, design_file = write_complete_order_delete_draft_files(project_dir)
    requirement = requirement_file.read_text(encoding="utf-8").replace(
        "仅管理员可以删除订单。", requirement_permission
    )
    design = design_file.read_text(encoding="utf-8").replace(
        "仅管理员可以删除订单，非管理员返回 PERMISSION_DENIED。", design_permission
    )
    requirement_file.write_text(requirement, encoding="utf-8")
    design_file.write_text(design, encoding="utf-8")
    assert run_cli_raw(
        ["draft", "requirement", "DRAFT-001", "--file", str(requirement_file)], cwd=project_dir
    ).returncode == 0
    assert run_cli_raw(
        ["draft", "design", "DRAFT-001", "--file", str(design_file)], cwd=project_dir
    ).returncode == 0
    return derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]


def test_text_001_and_text_009_free_text_waits_for_facts_instead_of_creating_conflict(tmp_path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    draft = _prepare_draft(project_dir, 复合权限句, 复合权限句)

    assessment = draft_lifecycle.assess_draft(draft)
    status = run_cli_raw(["draft", "status", "DRAFT-001"], cwd=project_dir)
    next_result = run_cli_raw(["next"], cwd=project_dir)
    start = run_cli_raw(["start", "--draft", "DRAFT-001"], cwd=project_dir)

    assert assessment.effective_status == "reviewing"
    assert assessment.facts_status == "facts_missing"
    assert assessment.conflicts == ()
    assert "冲突" not in assessment.reason
    assert "reviewing" in status.stdout and "facts_missing" in status.stdout
    assert "- 主推荐：$sdlc-design 生成技术事实并完成独立复核" in next_result.stdout
    assert start.returncode == 1
    assert "start --file <formal.v3.json>" in start.stderr and "冲突" not in start.stderr


def test_text_001_legacy_facts_package_cannot_bypass_document_first_start(tmp_path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    _prepare_draft(project_dir, 复合权限句, 复合权限句)
    draft = install_valid_draft_facts(project_dir, run_cli_raw)
    assert draft_lifecycle.assess_draft(draft).can_start is True

    package = project_dir / "formal.v3.json"
    write_formal_v3_from_draft(package, formal_business_from_draft(draft), draft)
    result = run_cli_raw(["start", "--file", str(package)], cwd=project_dir)

    assert result.returncode == 1
    assert "只接受 document-first.v1 正式包" in result.stderr


def test_text_002_business_object_word_is_not_a_draft_blocker(tmp_path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    draft = _prepare_draft(project_dir, "管理员可以查看订单和退款记录。", "管理员可以查看订单和退款记录。")

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "reviewing"
    assert assessment.conflicts == ()


def test_text_003_to_text_005_natural_language_shapes_do_not_enter_cli_semantic_blocking(tmp_path) -> None:
    samples = [
        "管理员在订单存在时可查看，财务人员可下载发票。",
        "管理员可以查看订单，但不能删除草稿。",
        "管理员获准查看订单，必要时也能取得对应发票。",
    ]
    for number, text in enumerate(samples, 1):
        sample_root = tmp_path / str(number)
        sample_root.mkdir()
        project_dir = init_demo_repo(sample_root)
        assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
        draft = _prepare_draft(project_dir, text, text)
        assessment = draft_lifecycle.assess_draft(draft)
        assert assessment.effective_status == "reviewing"
        assert assessment.conflicts == ()


def test_text_006_to_text_008_real_meaning_change_is_blocked_by_review_and_fact_gate(tmp_path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    draft = _prepare_draft(project_dir, 复合权限句, 复合权限句)
    bundle = build_valid_draft_fact_bundle(draft)

    for issue_type in ("meaning_changed", "wrong_relation"):
        changed = deepcopy(bundle)
        changed["review"] = fact_artifacts.build_review_artifact(
            changed["requirement"],
            changed["design"],
            changed["requirement"]["context_targets"],
            status="needs_review",
            issues=[
                {
                    "issue_id": "MR-I001",
                    "severity": "high",
                    "type": issue_type,
                    "message": "独立复核确认主体、方向或资源发生实质变化。",
                    "recovery": "重新提取事实并完成独立复核。",
                }
            ],
        )
        changed["manifest"] = fact_artifacts.build_fact_manifest(
            changed["source"], changed["index"], changed["requirement"], changed["design"], changed["review"]
        )
        result = fact_gate.FactGate.verify(changed, entry_kind="draft")
        assert result.passed is False
        assert result.code == issue_type


def test_text_010_explicit_permission_labels_still_require_resource(tmp_path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    text = "主体：管理员；方向：allow；动作：查看；资源："
    draft = _prepare_draft(project_dir, text, text)

    assessment = draft_lifecycle.assess_draft(draft)

    assert assessment.effective_status == "needs_user"
    assert any("明确权限字段缺少资源" in item for item in assessment.conflicts)
