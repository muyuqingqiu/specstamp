from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.core import fact_review_trust, review_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.schemas import load_schema


def _pending_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    review_id: str = "REV-001",
) -> tuple[object, dict]:
    project = tmp_path / review_id
    project.mkdir()
    (project / "原始需求.md").write_text("必须保留完成条件。\n", encoding="utf-8")
    (project / "需求拆分.json").write_text('{"id":"FR-001"}\n', encoding="utf-8")
    paths = build_paths(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    outcome = fact_review_trust.create_trusted_review_request(
        paths,
        review_id=review_id,
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["原始需求.md", "需求拆分.json"],
        required_checks=["检查原始条件是否完整覆盖"],
        created_at="2026-07-16T00:00:00Z",
    )
    return paths, outcome.request


def _result(request: dict, *, status: str = "passed") -> dict:
    issues = []
    if status == "needs_fix":
        issues = [
            {
                "issue_id": "ISSUE-001",
                "severity": "P1",
                "title": "遗漏完成条件",
                "description": "需求拆分没有保留原始资料中的完成条件。",
                "evidence_refs": ["原始需求.md#完成条件"],
                "affected_refs": ["FR-001"],
                "required_fix": "补回完成条件并更新覆盖关系。",
            }
        ]
    return {
        "schema_version": "review-result.v1",
        "review_id": request["review_id"],
        "stage": request["stage"],
        "owner_id": request["owner_id"],
        "reviewer_run_id": "调用方伪造的审核任务",
        "input_hashes": deepcopy(request["input_hashes"]),
        "status": status,
        "issues": issues,
        "notes": [],
        "reviewed_at": "2026-07-16T00:10:00Z",
    }


def test_reviewer_identity_only_uses_current_thread_and_same_task_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    registration = fact_review_trust.submit_trusted_review_result(
        paths,
        request_id=request["review_id"],
        submission=_result(request),
    )
    assert load_schema("review-result.v1")["$id"] == "review-result.v1"
    assert registration["result"]["reviewer_run_id"] == "reviewer-run"
    assert "调用方伪造" not in json.dumps(registration, ensure_ascii=False)

    other_paths, other_request = _pending_request(tmp_path, monkeypatch, review_id="REV-002")
    before = (other_paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes()
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    with pytest.raises(SdlcError, match="必须使用不同"):
        fact_review_trust.submit_trusted_review_result(
            other_paths,
            request_id=other_request["review_id"],
            submission=_result(other_request),
        )
    assert (other_paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes() == before


@pytest.mark.parametrize(
    ("status", "issues", "message"),
    [
        ("passed", [_result.__name__], "passed 时 issues 必须为空"),
        ("needs_fix", [], "needs_fix 时必须登记真实问题"),
    ],
)
def test_issue_list_must_match_result_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    issues: list,
    message: str,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    submission = _result(request, status=status)
    if status == "passed":
        submission["issues"] = [
            {
                "issue_id": "ISSUE-001",
                "severity": "P2",
                "title": "存在问题",
                "description": "有真实问题时不能登记为通过。",
                "evidence_refs": ["FR-001"],
                "affected_refs": ["FR-001"],
                "required_fix": "修复后重新审核。",
            }
        ]
    else:
        submission["issues"] = issues
    before = (paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes()
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError, match=message):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=submission,
        )
    assert (paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    ["review_id", "stage", "owner_id", "missing_hash", "extra_hash", "changed_hash", "extra_field", "missing_field"],
)
def test_result_must_match_the_complete_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    submission = _result(request)
    if damage == "review_id":
        submission["review_id"] = "REV-999"
    elif damage == "stage":
        submission["stage"] = "integrated_design"
    elif damage == "owner_id":
        submission["owner_id"] = "DRAFT-999"
    elif damage == "missing_hash":
        submission["input_hashes"].pop(next(iter(submission["input_hashes"])))
    elif damage == "extra_hash":
        submission["input_hashes"]["额外.md"] = "0" * 64
    elif damage == "changed_hash":
        first_path = next(iter(submission["input_hashes"]))
        submission["input_hashes"][first_path] = "0" * 64
    elif damage == "extra_field":
        submission["trusted"] = True
    else:
        submission.pop("notes")
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=submission,
        )


def test_result_registration_is_one_time_idempotent_and_passed_can_be_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    first = fact_review_trust.submit_trusted_review_result(
        paths,
        request_id=request["review_id"],
        submission=_result(request),
    )
    retry = fact_review_trust.submit_trusted_review_result(
        paths,
        request_id=request["review_id"],
        submission=_result(request),
    )
    assert retry == first

    changed = _result(request)
    changed["notes"] = ["不影响通过的普通说明。"]
    before = (paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes()
    with pytest.raises(SdlcError, match="已经被其他结果消费"):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=changed,
        )
    assert (paths.sdlc_dir / "trust" / "reviews" / "registry.json").read_bytes() == before

    monkeypatch.setenv("CODEX_THREAD_ID", "next-producer-run")
    reused = fact_review_trust.create_trusted_review_request(
        paths,
        review_id="REV-002",
        stage=request["stage"],
        owner_id=request["owner_id"],
        input_paths=request["input_paths"],
        required_checks=request["required_checks"],
    )
    assert reused.reused is True
    assert reused.registration == first
    assert set(fact_review_trust.load_review_registry(paths)["requests"]) == {"REV-001"}


def test_missing_key_and_tampered_registry_are_rejected_without_repairing_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    trust_dir = paths.sdlc_dir / "trust" / "reviews"
    key_path = trust_dir / ".key"
    key_path.unlink()
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError, match="HMAC 密钥"):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=_result(request),
        )
    assert not key_path.exists()

    other_paths, other_request = _pending_request(tmp_path, monkeypatch, review_id="REV-002")
    registry_path = other_paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["requests"]["REV-002"]["input_fingerprint"] = "0" * 64
    registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    tampered_bytes = registry_path.read_bytes()
    with pytest.raises(SdlcError, match="已被改写"):
        fact_review_trust.submit_trusted_review_result(
            other_paths,
            request_id=other_request["review_id"],
            submission=_result(other_request),
        )
    assert registry_path.read_bytes() == tampered_bytes


def test_input_change_and_missing_reviewer_identity_leave_pending_request_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, request = _pending_request(tmp_path, monkeypatch)
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    before = registry_path.read_bytes()
    (paths.root / "原始需求.md").write_text("输入已变化。\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError, match="真实输入文件已经变化"):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=_result(request),
        )
    assert registry_path.read_bytes() == before

    (paths.root / "原始需求.md").write_text("必须保留完成条件。\n", encoding="utf-8")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SdlcError, match="CODEX_THREAD_ID"):
        fact_review_trust.submit_trusted_review_result(
            paths,
            request_id=request["review_id"],
            submission=_result(request),
        )
    assert registry_path.read_bytes() == before
