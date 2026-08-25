from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

# 复用真实 CLI 的资料归档和需求原子导入夹具，避免另造一套较弱的状态。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_sdlc.core import fact_review_trust
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.services import review_service
from test_cli_v1 import run_cli
from test_cli_v17_draft_contract import (
    create_draft_with_material,
    import_command,
    requirement_documents,
    write_documents,
)
from test_cli_v6_discuss_prepare import append_structured_cap, reference


def _ready_project(
    tmp_path: Path,
    *,
    suffix: str = "main",
) -> tuple[Path, object, dict[str, object]]:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material, suffix=suffix)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "requirement_reviewing"
    return project, paths, material


def _submission(request: dict[str, object], *, status: str = "passed") -> dict[str, object]:
    issues: list[dict[str, object]] = []
    if status == "needs_fix":
        issues = [
            {
                "issue_id": "ISSUE-001",
                "severity": "P1",
                "title": "遗漏完成条件",
                "description": "需求拆分没有保留资料中的完成条件。",
                "evidence_refs": ["MAT-001"],
                "affected_refs": ["FR-001"],
                "required_fix": "补回完成条件并更新覆盖关系。",
            }
        ]
    return {
        "schema_version": "review-result.v1",
        "review_id": request["review_id"],
        "stage": request["stage"],
        "owner_id": request["owner_id"],
        "reviewer_run_id": "调用方伪造身份",
        "input_hashes": deepcopy(request["input_hashes"]),
        "status": status,
        "issues": issues,
        "notes": [],
        "reviewed_at": "2026-07-16T08:00:00Z",
    }


def _create(
    paths,
    monkeypatch: pytest.MonkeyPatch,
    *,
    producer: str = "需求生产任务",
) -> dict[str, object]:
    monkeypatch.setenv("CODEX_THREAD_ID", producer)
    return review_service.create_requirement_review(
        paths,
        draft_id="DRAFT-001",
        created_at="2026-07-16T07:00:00Z",
    )


def _submit(
    paths,
    request: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "passed",
) -> dict[str, object]:
    monkeypatch.setenv("CODEX_THREAD_ID", "独立审核任务")
    return review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status=status),
    )


def _reimport(
    project: Path,
    material: dict[str, object],
    *,
    suffix: str,
    mutate,
) -> None:
    split, coverage = requirement_documents(project, material, suffix=suffix)
    mutate(split, coverage)
    split_path, coverage_path = write_documents(project, split, coverage, suffix=f"-{suffix}")
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr


def _snapshot_files(project: Path) -> list[Path]:
    return sorted(
        (project / ".codex-sdlc/drafts/DRAFT-001/质检").glob("需求审核输入-*.json")
    )


def _record_real_decision_and_finish_caps(project: Path, paths) -> None:
    question_result = append_structured_cap(
        project,
        submission_key="question-review-condition",
        capture_type="question",
        increment="是否保留完成条件？",
    )
    assert question_result.returncode == 0, question_result.stderr
    question = derive_state(paths)["drafts"]["DRAFT-001"]["structured_captures"][0]
    source = project / "用户选择.txt"
    source.write_text("保留\n", encoding="utf-8")
    target = project / ".codex-sdlc/drafts/DRAFT-001/需求/requirement-split.v1.json"
    target_record = {
        "target_id": "DRAFT-001",
        "reference": reference(project, target),
    }
    decision_input = {
        "schema_version": "capture-increment.v1",
        "submission_key": "decision-review-condition",
        "draft_id": "DRAFT-001",
        "client_key": "decision-review-condition",
        "capture_type": "decision",
        "targets": [deepcopy(target_record)],
        "source_reference": reference(project, source),
        "source_sha256": sha256_file(source),
        "increment": "保留",
        "status": "pending",
        "decisions": [
            {
                "schema_version": "decision-input.v1",
                "client_key": "decision-review-answer",
                "question": {
                    "text": question["increment"],
                    "capture_ref": question["capture_id"],
                    "reference": deepcopy(question["source_reference"]),
                },
                "candidates": ["保留", "不保留"],
                "selection": "保留",
                "scope": [deepcopy(target_record)],
                "source_reference": reference(project, source),
                "confirmed_at": "2026-07-16T09:00:00Z",
            }
        ],
    }
    decision_file = project / "用户决定输入.json"
    decision_file.write_text(
        json.dumps(decision_input, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    decided = run_cli(["discuss", "--file", decision_file.name], cwd=project)
    assert decided.returncode == 0, decided.stderr

    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert [item["decision_id"] for item in draft["decision_records"]] == ["DEC-001"]
    for capture in draft["structured_captures"]:
        transition = {
            "schema_version": "capture-transition.v1",
            "transition_key": f"absorb-{capture['capture_id'].lower()}",
            "draft_id": "DRAFT-001",
            "capture_id": capture["capture_id"],
            "source_submission_key": capture["submission_key"],
            "source_submission_sha256": capture["submission_sha256"],
            "source_record_sha256": capture["record_sha256"],
            "from_status": "pending",
            "to_status": "absorbed",
            "relation": {
                "kind": "requirement_artifact",
                "target_id": "DRAFT-001",
                "reference": reference(project, target),
            },
        }
        transition_file = project / f"{capture['capture_id']}-归入需求.json"
        transition_file.write_text(
            json.dumps(transition, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        transitioned = run_cli(
            ["capture-transition", "--file", transition_file.name], cwd=project
        )
        assert transitioned.returncode == 0, transitioned.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "requirement_reviewing"


def test_complete_requirement_input_is_deterministic_and_uses_real_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material = _ready_project(tmp_path)

    outcome = _create(paths, monkeypatch)
    request = outcome["request"]
    snapshot_path = next(
        path for path in request["input_paths"] if "需求审核输入-" in path
    )
    snapshot = json.loads((project / snapshot_path).read_text(encoding="utf-8"))

    assert outcome["action"] == "created"
    assert request["review_id"] == "REV-001"
    assert request["stage"] == "requirement_split"
    assert request["owner_id"] == "DRAFT-001"
    assert request["input_paths"] == sorted(set(request["input_paths"]))
    assert request["required_checks"] == list(review_service.REQUIREMENT_REVIEW_CHECKS)
    assert snapshot["required_checks"] == request["required_checks"]
    assert [item["material_id"] for item in snapshot["applicable_materials"]] == [
        "MAT-001"
    ]
    assert snapshot["effective_decisions"] == []
    assert [item["artifact"] for item in snapshot["dependencies"]] == [
        "requirement_coverage",
        "requirement_review",
        "requirement_split",
    ]
    assert all(
        item["depends_on_paths"] == sorted(set(item["depends_on_paths"]))
        and item["depends_on_ids"] == sorted(set(item["depends_on_ids"]))
        for item in snapshot["dependencies"]
    )
    assert any("/requirements-" in path for path in request["input_paths"])
    assert any(path.endswith("/requirement-split.v1.json") for path in request["input_paths"])
    assert any(path.endswith("/requirement-coverage.v1.json") for path in request["input_paths"])
    assert any("/原始资料/MAT-001_" in path for path in request["input_paths"])
    assert all((project / path).is_file() for path in request["input_paths"])
    assert review_service.review_status(paths)["reviews"][0]["effective_status"] == "pending"

    retry = _create(paths, monkeypatch, producer="同输入重试任务")
    assert retry["action"] == "idempotent"
    assert retry["request"]["review_id"] == "REV-001"
    assert len(_snapshot_files(project)) == 1


def test_needs_fix_requires_changed_input_and_new_round_keeps_old_issues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, material = _ready_project(tmp_path)
    first = _create(paths, monkeypatch)
    _submit(paths, first["request"], monkeypatch, status="needs_fix")

    with pytest.raises(SdlcError, match="必须先改变受控输入"):
        _create(paths, monkeypatch, producer="未修复重试任务")

    monkeypatch.setenv("CODEX_THREAD_ID", "修复输入任务")
    _reimport(
        project,
        material,
        suffix="fixed",
        mutate=lambda split, _coverage: split["functional_requirements"][0].update(
            {"description": "用户登录后可以查看课程，并保留完整完成条件。"}
        ),
    )
    stale = review_service.review_status(paths, review_id="REV-001")["reviews"][0]
    assert stale["recorded_status"] == "needs_fix"
    assert stale["effective_status"] == "stale"
    assert stale["issues"][0]["issue_id"] == "ISSUE-001"

    second = _create(paths, monkeypatch, producer="修复后生产任务")
    assert second["action"] == "created"
    assert second["request"]["review_id"] == "REV-002"
    _submit(paths, second["request"], monkeypatch)
    combined = review_service.requirement_review_status(paths, draft_id="DRAFT-001")
    by_id = {item["review_id"]: item for item in combined["reviews"]}
    assert by_id["REV-001"]["issues"][0]["issue_id"] == "ISSUE-001"
    assert by_id["REV-001"]["is_current"] is False
    assert by_id["REV-002"]["effective_status"] == "passed"
    assert combined["can_advance"] is True


@pytest.mark.parametrize(
    ("change_name", "suffix", "mutate"),
    [
        (
            "拆分元数据",
            "split-meta",
            lambda split, _coverage: split.update({"title": "课程访问需求已调整"}),
        ),
        (
            "覆盖关系",
            "coverage-change",
            lambda _split, coverage: coverage["units"][0].update(
                {"reason": "覆盖关系重新核对"}
            ),
        ),
        (
            "FR",
            "fr-change",
            lambda split, _coverage: split["functional_requirements"][0].update(
                {"description": "FR 内容已经改变"}
            ),
        ),
        (
            "GR",
            "gr-change",
            lambda split, _coverage: split["global_rules"][0].update(
                {"description": "GR 内容已经改变"}
            ),
        ),
    ],
)
def test_split_coverage_fr_and_gr_changes_make_passed_review_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_name: str,
    suffix: str,
    mutate,
) -> None:
    project, paths, material = _ready_project(tmp_path)
    first = _create(paths, monkeypatch)
    _submit(paths, first["request"], monkeypatch)

    monkeypatch.setenv("CODEX_THREAD_ID", f"{change_name}修改任务")
    _reimport(project, material, suffix=suffix, mutate=mutate)

    status = review_service.review_status(paths, review_id="REV-001")["reviews"][0]
    assert status["recorded_status"] == "passed"
    assert status["effective_status"] == "stale"
    assert status["can_advance"] is False
    assert status["changed_files"] == sorted(set(status["changed_files"]))


def test_material_revision_and_decision_projection_change_make_review_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material = _ready_project(tmp_path)
    first = _create(paths, monkeypatch)
    _submit(paths, first["request"], monkeypatch)

    revised_source = project / "课程访问需求修订.md"
    revised_source.write_text("用户登录后可以查看课程，并显示课程来源。\n", encoding="utf-8")
    revised = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "requirement",
            "--title",
            "课程访问需求修订",
            "--file",
            revised_source.name,
            "--supersedes",
            "MAT-001",
        ],
        cwd=project,
    )
    assert revised.returncode == 0, revised.stderr
    material_stale = review_service.review_status(paths, review_id="REV-001")["reviews"][0]
    assert material_stale["effective_status"] == "stale"

    # 独立项目通过真实 discuss 和 capture-transition 入口形成 DEC，再检查通用 status。
    other_root = tmp_path / "决定变化"
    other_root.mkdir()
    other_project, other_paths, _ = _ready_project(other_root)
    other = _create(other_paths, monkeypatch, producer="决定项目生产任务")
    _submit(other_paths, other["request"], monkeypatch)
    _record_real_decision_and_finish_caps(other_project, other_paths)
    decision_stale = review_service.review_status(other_paths, review_id="REV-001")[
        "reviews"
    ][0]
    assert decision_stale["effective_status"] == "stale"
    assert (
        other_paths.draft_status_file("DRAFT-001").relative_to(other_project).as_posix()
        in decision_stale["changed_files"]
    )
    refreshed = _create(other_paths, monkeypatch, producer="决定变化后的生产任务")
    refreshed_snapshot_path = next(
        path for path in refreshed["request"]["input_paths"] if "需求审核输入-" in path
    )
    refreshed_snapshot = json.loads(
        (other_project / refreshed_snapshot_path).read_text(encoding="utf-8")
    )
    assert [item["decision_id"] for item in refreshed_snapshot["effective_decisions"]] == [
        "DEC-001"
    ]


def test_only_explicitly_applicable_non_requirement_material_changes_review_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material = _ready_project(tmp_path)
    first = _create(paths, monkeypatch)
    _submit(paths, first["request"], monkeypatch)

    unrelated_source = project / "无关界面稿.txt"
    unrelated_source.write_text("只影响尚不存在的页面。\n", encoding="utf-8")
    unrelated = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "ui-design",
            "--title",
            "无关界面稿",
            "--file",
            unrelated_source.name,
            "--scope",
            "PAGE-999",
        ],
        cwd=project,
    )
    assert unrelated.returncode == 0, unrelated.stderr
    assert review_service.review_status(paths, review_id="REV-001")["reviews"][0][
        "effective_status"
    ] == "passed"

    applicable_source = project / "当前需求界面稿.txt"
    applicable_source.write_text("明确适用于当前 DRAFT。\n", encoding="utf-8")
    applicable = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "ui-design",
            "--title",
            "当前需求界面稿",
            "--file",
            applicable_source.name,
            "--scope",
            "DRAFT-001",
        ],
        cwd=project,
    )
    assert applicable.returncode == 0, applicable.stderr
    assert review_service.review_status(paths, review_id="REV-001")["reviews"][0][
        "effective_status"
    ] == "stale"


@pytest.mark.parametrize("blocker", ["open_questions", "needs_user", "needs_material"])
def test_explicit_requirement_blockers_reject_before_any_review_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    if blocker == "open_questions":
        split["open_questions"] = ["是否允许游客访问？"]
    else:
        covered_copy = deepcopy(coverage["units"][0])
        covered_copy["client_key"] = "src-rule-confirmed-copy"
        coverage["units"].append(covered_copy)
        coverage["units"][0]["status"] = blocker
        coverage["units"][0]["covered_by"] = []
        coverage["units"][0]["reason"] = "等待明确输入"
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr

    monkeypatch.setenv("CODEX_THREAD_ID", "阻断检查任务")
    with pytest.raises(SdlcError, match="尚未满足需求审核前置条件"):
        review_service.create_requirement_review(paths, draft_id="DRAFT-001")
    assert not _snapshot_files(project)
    assert not (paths.sdlc_dir / "trust" / "reviews").exists()


def test_unversioned_pending_cap_missing_identity_and_failed_publish_leave_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unversioned_root = tmp_path / "未版本化资料"
    unversioned_root.mkdir()
    unversioned_project, unversioned_paths, _ = _ready_project(unversioned_root)
    added = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "requirement",
            "--title",
            "未固定版本的外部需求",
            "--url",
            "https://example.com/current-requirement",
        ],
        cwd=unversioned_project,
    )
    assert added.returncode == 0, added.stderr
    monkeypatch.setenv("CODEX_THREAD_ID", "未版本化检查任务")
    with pytest.raises(SdlcError, match="尚未满足需求审核前置条件"):
        review_service.create_requirement_review(
            unversioned_paths, draft_id="DRAFT-001"
        )
    assert not _snapshot_files(unversioned_project)

    pending_root = tmp_path / "待处理CAP"
    pending_root.mkdir()
    pending_project, pending_paths, _ = _ready_project(pending_root)
    appended = append_structured_cap(
        pending_project,
        submission_key="pending-review-fact",
        capture_type="fact",
        increment="审核前新增的事实还没有归入需求产物。",
    )
    assert appended.returncode == 0, appended.stderr
    monkeypatch.setenv("CODEX_THREAD_ID", "CAP检查任务")
    with pytest.raises(SdlcError, match="尚未满足需求审核前置条件"):
        review_service.create_requirement_review(pending_paths, draft_id="DRAFT-001")
    assert not _snapshot_files(pending_project)

    identity_root = tmp_path / "身份与失败"
    identity_root.mkdir()
    project, paths, _material = _ready_project(identity_root)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SdlcError, match="CODEX_THREAD_ID"):
        review_service.create_requirement_review(paths, draft_id="DRAFT-001")
    assert not _snapshot_files(project)
    assert not (paths.sdlc_dir / "trust" / "reviews").exists()

    monkeypatch.setenv("CODEX_THREAD_ID", "写入失败任务")

    def fail_publish(*_args, **_kwargs):
        raise OSError("模拟审核登记发布失败")

    monkeypatch.setattr(
        fact_review_trust, "register_trusted_review_request_locked", fail_publish
    )
    with pytest.raises(OSError, match="模拟审核登记发布失败"):
        review_service.create_requirement_review(paths, draft_id="DRAFT-001")
    assert not _snapshot_files(project)
    registry = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    assert not registry.exists()


def test_passed_input_is_reused_and_same_thread_or_hash_mismatch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, paths, _material = _ready_project(tmp_path)
    first = _create(paths, monkeypatch, producer="生产任务A")
    request = first["request"]

    monkeypatch.setenv("CODEX_THREAD_ID", "生产任务A")
    with pytest.raises(SdlcError, match="必须使用不同"):
        review_service.submit_review(
            paths,
            request_id="REV-001",
            submission=_submission(request),
        )
    forged = _submission(request)
    forged["input_hashes"] = deepcopy(forged["input_hashes"])
    forged["input_hashes"].pop(next(iter(forged["input_hashes"])))
    monkeypatch.setenv("CODEX_THREAD_ID", "独立审核任务")
    with pytest.raises(SdlcError, match="input_hashes"):
        review_service.submit_review(
            paths,
            request_id="REV-001",
            submission=forged,
        )

    _submit(paths, request, monkeypatch)
    reused = _create(paths, monkeypatch, producer="生产任务B")
    assert reused["action"] == "reused"
    assert reused["request"]["review_id"] == "REV-001"
    assert len(fact_review_trust.load_review_registry(paths)["requests"]) == 1


def test_draft_command_only_orchestrates_requirement_review_create_and_status(
    tmp_path: Path,
) -> None:
    project, _paths, _material = _ready_project(tmp_path)
    created = run_cli(
        ["draft", "requirement-review", "create", "DRAFT-001"],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": "真实命令生产任务"},
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert payload["request"]["stage"] == "requirement_split"
    assert payload["request"]["review_id"] == "REV-001"

    status = run_cli(
        ["draft", "requirement-review", "status", "DRAFT-001"],
        cwd=project,
        extra_env={"CODEX_THREAD_ID": ""},
    )
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["status"] == "ready"
    assert status_payload["reviews"][0]["effective_status"] == "pending"
    assert "requirement_confirmed" not in created.stdout
