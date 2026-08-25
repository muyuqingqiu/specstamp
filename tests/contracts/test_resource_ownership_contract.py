from __future__ import annotations

from codex_sdlc.core import draft_ownership


def test_start_file_without_source_only_consumes_unbound_designs() -> None:
    state = {
        "designs": [
            {"design_id": "DES-001", "status": "accepted", "draft_id": "DRAFT-001"},
            {"design_id": "DES-002", "status": "accepted", "draft_id": ""},
        ]
    }

    assert draft_ownership.unlinked_accepted_design_ids_for_start(state, "") == ["DES-002"]
    assert draft_ownership.unlinked_accepted_design_ids_for_start(state, "DRAFT-001") == ["DES-001"]


def test_requirement_draft_captures_follow_source_draft() -> None:
    state = {
        "captures": [
            {"capture_id": "CAP-001", "status": "pending", "target_type": "requirement_draft", "draft_id": "DRAFT-001"},
            {"capture_id": "CAP-002", "status": "pending", "target_type": "requirement_draft", "draft_id": ""},
        ],
        "drafts": {"DRAFT-001": {"draft_id": "DRAFT-001", "status": "start_ready"}},
    }

    assert draft_ownership.pending_requirement_draft_capture_ids(state, "") == ["CAP-002"]
    assert draft_ownership.pending_requirement_draft_capture_ids(state, "DRAFT-001") == ["CAP-001"]

def test_pending_requirement_lines_hide_current_draft_capture() -> None:
    state = {
        "drafts": {"DRAFT-001": {"draft_id": "DRAFT-001", "status": "requirement_ready"}},
        "captures": [
            {"capture_id": "CAP-001", "status": "pending", "target_type": "requirement_draft", "draft_id": "DRAFT-001", "summary": "已进 DRAFT"},
            {"capture_id": "CAP-002", "status": "pending", "target_type": "requirement_draft", "summary": "独立线索"},
        ],
    }

    lines = draft_ownership.pending_requirement_draft_lines(state)

    assert lines == ["- CAP-002：独立线索"]


def test_resource_ownership_reports_pending_capture_after_draft_started() -> None:
    state = {
        "drafts": {"DRAFT-001": {"draft_id": "DRAFT-001", "status": "started"}},
        "captures": [
            {"capture_id": "CAP-001", "status": "pending", "target_type": "requirement_draft", "draft_id": "DRAFT-001"},
        ],
    }

    assert draft_ownership.resource_ownership_issues(state) == ["CAP-001 已纳入 DRAFT-001，但 DRAFT 已建档后仍处于 pending。"]
