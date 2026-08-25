from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.structured_contract import canonical_json_text, canonical_sha256, sha256_bytes
from codex_sdlc.services import change_service, review_service
from test_change_package_contract import (
    _formal_project,
    _source_contracts,
    _submit,
    _write_sources,
)
from test_review_invalidation import _submission


def _projected_change(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    project, _requirement_root, status = _formal_project(tmp_path)
    sources = _write_sources(project, _source_contracts(project, status))
    _submit(project, sources)
    return project, status


def _fully_reviewed_projected_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object]]:
    project, _requirement_root, status = _formal_project(tmp_path)
    documents = _source_contracts(project, status)
    package = documents["change-package.v1.json"]
    package["review_impacts"] = [
        {"stage": "requirement_split", "reason_refs": ["FR-001"]},
        {"stage": "integrated_design", "reason_refs": ["FR-001"]},
        {"stage": "task_plan", "reason_refs": ["FR-001"]},
    ]
    rewritten = deepcopy(package)
    rewritten["requirement_operations"][0]["next_value"]["acceptance_criteria"][0][
        "owner_fr_ref"
    ] = "FR-002"
    rewritten["acceptance_operations"][0]["next_value"]["owner_fr_ref"] = "FR-002"
    package_sha256 = sha256_bytes(canonical_json_text(rewritten).encode("utf-8"))
    reference = documents["projected-reference-index.v2.json"]["content"]
    for reference_id in ("FR-002", "AC-002"):
        reference["entries"][reference_id]["sha256"] = package_sha256
    task_plan = documents["projected-task-plan.v2.json"]["content"]
    task_plan["input_hashes"]["change_package"] = canonical_sha256(rewritten)
    for name in (
        "projected-reference-index.v2.json",
        "projected-task-plan.v2.json",
    ):
        documents[name]["content_sha256"] = canonical_sha256(documents[name]["content"])
    _submit(project, _write_sources(project, documents))

    paths = build_paths(project)
    requests = []
    monkeypatch.setenv("CODEX_THREAD_ID", "变更开发任务")
    for stage in ("requirement_split", "integrated_design", "task_plan"):
        requests.append(
            review_service.create_review(
                paths,
                review_id="REV-999",
                stage=stage,
                owner_id="CHG-001",
                input_paths=["资料.json"],
            )["request"]
        )
    for index, request in enumerate(requests, start=1):
        monkeypatch.setenv("CODEX_THREAD_ID", f"独立变更审核任务{index}")
        review_service.submit_review(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request),
        )
    return project, status


def test_change_review_rebuilds_fixed_inputs_and_keeps_independent_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _status = _projected_change(tmp_path)
    paths = build_paths(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "变更开发任务")
    created = review_service.create_review(
        paths,
        review_id="REV-999",
        stage="requirement_split",
        owner_id="CHG-001",
        input_paths=["资料.json"],
        required_checks=["调用方不能缩减固定检查项"],
    )
    request = created["request"]
    assert request["review_id"] == "REV-001"
    assert request["owner_id"] == "CHG-001"
    assert request["stage"] == "requirement_split"
    assert request["input_paths"] == sorted(
        [
            ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001/change-package.v1.json",
            ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001/projected-reference-index.v2.json",
            ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001/projected-requirement.v2.json",
            ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001/projected-test-matrix.v2.json",
        ]
    )

    with pytest.raises(SdlcError, match="必须使用不同"):
        review_service.submit_review(
            paths,
            request_id="REV-001",
            submission=_submission(request),
        )
    monkeypatch.setenv("CODEX_THREAD_ID", "独立变更审核任务")
    review_service.submit_review(
        paths,
        request_id="REV-001",
        submission=_submission(request),
    )
    current = review_service.review_status(paths, review_id="REV-001")
    assert current["reviews"][0]["effective_status"] == "passed"
    assert current["reviews"][0]["can_advance"] is True


def test_committed_projected_input_drift_makes_change_review_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _status = _projected_change(tmp_path)
    paths = build_paths(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "变更开发任务")
    request = review_service.create_review(
        paths,
        review_id="REV-999",
        stage="requirement_split",
        owner_id="CHG-001",
        input_paths=["资料.json"],
    )["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立变更审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request),
    )
    target = (
        project
        / ".codex-sdlc/requirements/REQ-001-订单审批/changes/CHG-001/projected-requirement.v2.json"
    )
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    stale = review_service.review_status(paths, review_id=str(request["review_id"]))
    assert stale["reviews"][0]["effective_status"] == "stale"
    assert stale["reviews"][0]["can_advance"] is False


def test_protection_rejects_missing_cascaded_reviews_before_writing_result(
    tmp_path: Path,
) -> None:
    project, status = _projected_change(tmp_path)
    protection = (
        project
        / str(status["workspace_path"])
        / "change-protection.v1.json"
    )
    with pytest.raises(SdlcError, match="缺少必须重新执行的审核"):
        change_service.protect_change_package(
            build_paths(project),
            requirement_id="REQ-001",
            change_id="CHG-001",
            confirm_requirement=True,
        )
    assert not protection.exists()


def test_change_review_does_not_accept_a_fourth_fixed_stage(
    tmp_path: Path,
) -> None:
    project, _status = _projected_change(tmp_path)
    with pytest.raises(SdlcError, match="不支持审核阶段"):
        review_service.create_review(
            build_paths(project),
            review_id="REV-999",
            stage="change",
            owner_id="CHG-001",
            input_paths=["资料.json"],
        )


def test_all_impacted_reviews_and_user_confirmation_create_hashed_protection_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, status = _fully_reviewed_projected_change(tmp_path, monkeypatch)
    paths = build_paths(project)
    with pytest.raises(SdlcError, match="用户明确确认"):
        change_service.protect_change_package(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            confirm_requirement=False,
        )
    result = change_service.protect_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        confirm_requirement=True,
    )
    assert [item["stage"] for item in result["reviews"]] == [
        "requirement_split",
        "integrated_design",
        "task_plan",
    ]
    assert result["requirement_confirmation"]["mode"] == "confirmed_for_change"
    protection = project / str(status["workspace_path"]) / "change-protection.v1.json"
    assert protection.is_file()
    stored = __import__("json").loads(protection.read_text(encoding="utf-8"))
    body = {key: value for key, value in stored.items() if key != "protection_sha256"}
    assert stored["protection_sha256"] == canonical_sha256(body)
    retry = change_service.protect_change_package(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        confirm_requirement=False,
    )
    assert retry["idempotent"] is True
