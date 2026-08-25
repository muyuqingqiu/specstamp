from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

# 复用阶段一真实资料、需求原子包和独立审核夹具，确认入口不另造弱化数据。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.state import derive_state, load_events
from codex_sdlc.core.structured_contract import canonical_sha256, validate_schema_document
from codex_sdlc.services import draft_service
from codex_sdlc.services.draft_service import DraftMutationService
from test_cli_v1 import run_cli
from test_cli_v17_draft_contract import requirement_documents, write_documents
from test_cli_v6_discuss_prepare import append_structured_cap
from test_requirement_review_flow import (
    _create,
    _ready_project,
    _record_real_decision_and_finish_caps,
    _reimport,
    _submit,
)


def _passed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object, dict[str, object], dict[str, object]]:
    project, paths, material = _ready_project(tmp_path)
    outcome = _create(paths, monkeypatch)
    _submit(paths, outcome["request"], monkeypatch)
    return project, paths, material, outcome["request"]


def _confirm(paths, review_id: str = "REV-001") -> dict[str, object]:
    return DraftMutationService(paths, source="用户确认合同测试").confirm_requirement(
        "DRAFT-001",
        review_id=review_id,
        confirmed_at="2026-07-16T10:00:00Z",
    )


def test_public_cli_confirms_current_passed_requirement_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)

    result = run_cli(
        [
            "draft",
            "requirement-confirm",
            "DRAFT-001",
            "--review",
            "REV-001",
        ],
        cwd=project,
    )

    assert result.returncode == 0, result.stderr
    assert "已确认需求：DRAFT-001" in result.stdout
    assert "需求确认：RCF-001" in result.stdout
    assert "需求审核：REV-001" in result.stdout
    assert "DRAFT 状态：requirement_confirmed" in result.stdout
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "requirement_confirmed"


def test_public_cli_rejects_unpassed_review_without_writing(
    tmp_path: Path,
) -> None:
    project, paths, _material = _ready_project(tmp_path)
    events_before = paths.events_file.read_bytes()

    result = run_cli(
        [
            "draft",
            "requirement-confirm",
            "DRAFT-001",
            "--review",
            "REV-001",
        ],
        cwd=project,
    )

    assert result.returncode == 1
    assert "没有唯一的当前需求审核" in result.stderr
    assert paths.events_file.read_bytes() == events_before
    assert not (
        project
        / ".codex-sdlc/drafts/DRAFT-001/需求/requirement-confirmation.v1.json"
    ).exists()


def test_public_cli_repeated_confirmation_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)
    command = [
        "draft",
        "requirement-confirm",
        "DRAFT-001",
        "--review",
        "REV-001",
    ]
    first = run_cli(command, cwd=project)
    event_count = len(load_events(paths))

    second = run_cli(command, cwd=project)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "需求已经确认：DRAFT-001" in second.stdout
    assert len(load_events(paths)) == event_count


def test_passed_review_creates_strict_confirmation_and_confirmed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)

    outcome = _confirm(paths)
    confirmation = outcome["confirmation"]

    assert outcome["action"] == "created"
    assert outcome["status"] == "requirement_confirmed"
    assert confirmation["schema_version"] == "requirement-confirmation.v1"
    assert confirmation["confirmation_id"] == "RCF-001"
    assert confirmation["draft_id"] == "DRAFT-001"
    assert confirmation["stage"] == "requirement_split"
    assert confirmation["review_id"] == "REV-001"
    assert confirmation["confirmation_sha256"] == canonical_sha256(
        {
            key: value
            for key, value in confirmation.items()
            if key != "confirmation_sha256"
        }
    )
    validate_schema_document(
        confirmation,
        schema_name="requirement-confirmation.v1",
    )
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "requirement_confirmed"
    assert draft["assessment"]["can_start"] is False
    assert draft["_requirement_review_state"]["can_advance"] is True
    assert draft["_requirement_confirmation_state"]["can_advance"] is True
    assert (
        project
        / ".codex-sdlc/drafts/DRAFT-001/需求/requirement-confirmation.v1.json"
    ).is_file()
    assert (
        project / ".codex-sdlc/drafts/DRAFT-001/需求/确认记录/RCF-001.json"
    ).is_file()


def test_unreviewed_needs_fix_and_old_review_are_rejected_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, material = _ready_project(tmp_path)
    service = DraftMutationService(paths, source="用户确认拒绝测试")
    before_events = load_events(paths)

    with pytest.raises(SdlcError, match="没有唯一的当前需求审核"):
        service.confirm_requirement("DRAFT-001", review_id="REV-001")
    assert load_events(paths) == before_events

    first = _create(paths, monkeypatch)
    _submit(paths, first["request"], monkeypatch, status="needs_fix")
    with pytest.raises(SdlcError, match="effective_status"):
        service.confirm_requirement("DRAFT-001", review_id="REV-001")

    split, coverage = requirement_documents(project, material, suffix="main")
    monkeypatch.setenv("CODEX_THREAD_ID", "修复需求包任务")
    split["producer_run_id"] = "修复需求包任务"
    split["functional_requirements"][0]["description"] += "，并保留完成条件"
    split_path, coverage_path = write_documents(
        project,
        split,
        coverage,
        suffix="-fixed",
    )
    imported = run_cli(
        [
            "draft",
            "requirements",
            "DRAFT-001",
            "--split-file",
            str(split_path),
            "--coverage-file",
            str(coverage_path),
        ],
        cwd=project,
    )
    assert imported.returncode == 0, imported.stderr
    second = _create(paths, monkeypatch, producer="修复后生产任务")
    _submit(paths, second["request"], monkeypatch)
    assert second["request"]["review_id"] == "REV-002"

    with pytest.raises(SdlcError, match="不是当前|不属于|没有唯一"):
        service.confirm_requirement("DRAFT-001", review_id="REV-001")
    assert not (
        project
        / ".codex-sdlc/drafts/DRAFT-001/需求/requirement-confirmation.v1.json"
    ).exists()


def test_same_review_and_input_is_idempotent_without_new_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)
    first = _confirm(paths)
    event_count = len(load_events(paths))

    second = _confirm(paths)

    assert second["action"] == "idempotent"
    assert second["confirmation"] == first["confirmation"]
    assert len(load_events(paths)) == event_count
    assert len(derive_state(paths)["drafts"]["DRAFT-001"]["requirement_confirmations"]) == 1


def test_conflicting_confirmation_replay_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)
    first = _confirm(paths)["confirmation"]
    conflict = deepcopy(first)
    conflict["confirmed_at"] = "2026-07-16T11:00:00Z"
    conflict["confirmation_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in conflict.items()
            if key != "confirmation_sha256"
        }
    )
    events = load_events(paths)
    events.append(
        {
            **deepcopy(events[-1]),
            "event_id": "EVT-20990101-999",
            "payload": {
                "draft_id": "DRAFT-001",
                "confirmation": conflict,
                "assessment": {},
            },
        }
    )
    paths.events_file.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
            for item in events
        ),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="编号重复且内容冲突"):
        derive_state(paths)


@pytest.mark.parametrize(
    "mutation",
    [
        "split",
        "coverage",
        "fr",
        "gr",
    ],
)
def test_requirement_artifact_drift_makes_confirmation_stale_and_state_reviewing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    project, paths, material, _request = _passed_project(tmp_path, monkeypatch)
    _confirm(paths)
    split, coverage = requirement_documents(project, material, suffix="main")
    monkeypatch.setenv("CODEX_THREAD_ID", f"{mutation}漂移任务")
    split["producer_run_id"] = f"{mutation}漂移任务"
    if mutation == "split":
        split["title"] += " 修订"
    elif mutation == "coverage":
        coverage["units"] = list(reversed(coverage["units"]))
    elif mutation == "fr":
        split["functional_requirements"][0]["description"] += "，展示来源"
    else:
        split["global_rules"][0]["description"] += "，并记录登录状态"
    split_path, coverage_path = write_documents(
        project,
        split,
        coverage,
        suffix=f"-{mutation}",
    )
    result = run_cli(
        [
            "draft",
            "requirements",
            "DRAFT-001",
            "--split-file",
            str(split_path),
            "--coverage-file",
            str(coverage_path),
        ],
        cwd=project,
    )
    assert result.returncode == 0, result.stderr

    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "requirement_reviewing"
    assert draft["_requirement_review_state"]["reviews"][0]["effective_status"] == "stale"
    assert draft["_requirement_confirmation_state"]["status"] == "stale"
    assert len(draft["requirement_confirmations"]) == 1


def test_material_and_decision_drift_keep_history_and_require_new_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)
    _confirm(paths)
    revised_source = project / "课程访问需求修订.md"
    revised_source.write_text("用户登录后可以查看课程，并显示来源。\n", encoding="utf-8")
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
    material_state = derive_state(paths)["drafts"]["DRAFT-001"]
    assert material_state["status"] == "requirement_reviewing"
    assert material_state["_requirement_confirmation_state"]["status"] == "stale"

    other_root = tmp_path / "决定漂移"
    other_root.mkdir()
    other_project, other_paths, _material2, _request2 = _passed_project(
        other_root,
        monkeypatch,
    )
    _confirm(other_paths)
    _record_real_decision_and_finish_caps(other_project, other_paths)
    decision_state = derive_state(other_paths)["drafts"]["DRAFT-001"]
    assert decision_state["status"] == "requirement_reviewing"
    assert decision_state["_requirement_review_state"]["reviews"][0]["effective_status"] == "stale"
    assert decision_state["_requirement_confirmation_state"]["status"] == "stale"
    assert [item["confirmation_id"] for item in decision_state["requirement_confirmations"]] == [
        "RCF-001"
    ]


def test_new_passed_review_and_confirmation_restore_confirmed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, material, _request = _passed_project(tmp_path, monkeypatch)
    _confirm(paths)
    split, coverage = requirement_documents(project, material, suffix="main")
    monkeypatch.setenv("CODEX_THREAD_ID", "修复需求输入任务")
    split["producer_run_id"] = "修复需求输入任务"
    split["functional_requirements"][0]["description"] += "，展示课程来源"
    split_path, coverage_path = write_documents(
        project,
        split,
        coverage,
        suffix="-restored",
    )
    imported = run_cli(
        [
            "draft",
            "requirements",
            "DRAFT-001",
            "--split-file",
            str(split_path),
            "--coverage-file",
            str(coverage_path),
        ],
        cwd=project,
    )
    assert imported.returncode == 0, imported.stderr
    second = _create(paths, monkeypatch, producer="修复后审核生产任务")
    assert second["request"]["review_id"] == "REV-002"
    _submit(paths, second["request"], monkeypatch)

    restored = _confirm(paths, review_id="REV-002")
    state = derive_state(paths)["drafts"]["DRAFT-001"]

    assert restored["action"] == "created"
    assert restored["confirmation"]["confirmation_id"] == "RCF-002"
    assert state["status"] == "requirement_confirmed"
    assert [item["confirmation_id"] for item in state["requirement_confirmations"]] == [
        "RCF-001",
        "RCF-002",
    ]
    reviews = state["_requirement_review_state"]["reviews"]
    assert [(item["review_id"], item["effective_status"]) for item in reviews] == [
        ("REV-001", "stale"),
        ("REV-002", "passed"),
    ]


@pytest.mark.parametrize(
    ("change_name", "suffix", "mutate"),
    [
        (
            "split",
            "history-split",
            lambda split, _coverage: split.update({"title": "历史转换后的拆分标题"}),
        ),
        (
            "coverage",
            "history-coverage",
            lambda _split, coverage: coverage["units"][0].update(
                {"reason": "历史转换后的覆盖关系重新核对"}
            ),
        ),
        (
            "FR",
            "history-fr",
            lambda split, _coverage: split["functional_requirements"][0].update(
                {"description": "历史转换后的 FR 内容已经改变。"}
            ),
        ),
        (
            "GR",
            "history-gr",
            lambda split, _coverage: split["global_rules"][0].update(
                {"description": "历史转换后的 GR 内容已经改变。"}
            ),
        ),
    ],
)
def test_requirement_drift_after_dec_and_cap_history_keeps_state_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_name: str,
    suffix: str,
    mutate,
) -> None:
    project, paths, material = _ready_project(tmp_path)

    # 先保留一轮带问题的审核，再通过真实修复形成首份确认，确保后续漂移不会丢失旧 issues。
    first = _create(paths, monkeypatch, producer=f"{change_name}首轮生产任务")
    _submit(paths, first["request"], monkeypatch, status="needs_fix")
    monkeypatch.setenv("CODEX_THREAD_ID", f"{change_name}首轮修复任务")
    _reimport(
        project,
        material,
        suffix=f"{suffix}-fixed",
        mutate=lambda split, _coverage: split["functional_requirements"][0].update(
            {"description": "用户登录后可以查看课程，并保留完整完成条件。"}
        ),
    )
    second = _create(paths, monkeypatch, producer=f"{change_name}修复后审核任务")
    _submit(paths, second["request"], monkeypatch)
    _confirm(paths, review_id="REV-002")

    # 真实 discuss 与 capture-transition 入口会把当时的需求投影写进历史关系。
    _record_real_decision_and_finish_caps(project, paths)
    third = _create(paths, monkeypatch, producer=f"{change_name}决定后审核任务")
    _submit(paths, third["request"], monkeypatch)
    _confirm(paths, review_id="REV-003")

    monkeypatch.setenv("CODEX_THREAD_ID", f"{change_name}漂移任务")
    _reimport(project, material, suffix=suffix, mutate=mutate)

    # 当前投影变化只应让审核和确认 stale；历史 CAP/DEC 事件必须始终可以重放。
    drifted = derive_state(paths)["drafts"]["DRAFT-001"]
    assert drifted["status"] == "requirement_reviewing"
    reviews = drifted["_requirement_review_state"]["reviews"]
    assert [item["review_id"] for item in reviews] == ["REV-001", "REV-002", "REV-003"]
    assert reviews[0]["issues"][0]["issue_id"] == "ISSUE-001"
    assert reviews[-1]["effective_status"] == "stale"
    assert [
        item["confirmation_id"] for item in drifted["requirement_confirmations"]
    ] == ["RCF-001", "RCF-002"]
    assert drifted["_requirement_confirmation_state"]["status"] == "stale"
    assert [item["decision_id"] for item in drifted["decision_records"]] == ["DEC-001"]
    capture_ids = [item["capture_id"] for item in drifted["structured_captures"]]
    assert capture_ids == ["CAP-001", "CAP-002"]
    assert [item["capture_id"] for item in drifted["capture_transitions"]] == capture_ids
    assert drifted["capture_statuses"] == {
        "CAP-001": "absorbed",
        "CAP-002": "absorbed",
    }

    refreshed_projection = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)
    assert refreshed_projection.returncode == 0, refreshed_projection.stderr
    fourth = _create(paths, monkeypatch, producer=f"{change_name}漂移后审核任务")
    _submit(paths, fourth["request"], monkeypatch)
    restored = _confirm(paths, review_id="REV-004")
    current = derive_state(paths)["drafts"]["DRAFT-001"]
    assert restored["confirmation"]["confirmation_id"] == "RCF-003"
    assert current["status"] == "requirement_confirmed"
    assert [item["review_id"] for item in current["_requirement_review_state"]["reviews"]] == [
        "REV-001",
        "REV-002",
        "REV-003",
        "REV-004",
    ]
    assert [
        item["confirmation_id"] for item in current["requirement_confirmations"]
    ] == ["RCF-001", "RCF-002", "RCF-003"]
    assert [item["decision_id"] for item in current["decision_records"]] == ["DEC-001"]
    assert [item["capture_id"] for item in current["capture_transitions"]] == capture_ids


@pytest.mark.parametrize(
    "blocker",
    [
        "open_questions",
        "needs_user",
        "needs_material",
        "unversioned",
        "pending_cap",
    ],
)
def test_current_blockers_reject_reconfirmation_without_new_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    project, paths, material, _request = _passed_project(tmp_path, monkeypatch)
    _confirm(paths)
    if blocker in {"open_questions", "needs_user", "needs_material"}:
        split, coverage = requirement_documents(project, material, suffix="main")
        monkeypatch.setenv("CODEX_THREAD_ID", f"{blocker}阻断输入任务")
        split["producer_run_id"] = f"{blocker}阻断输入任务"
        if blocker == "open_questions":
            split["open_questions"] = ["是否允许游客访问？"]
        else:
            covered_copy = deepcopy(coverage["units"][0])
            covered_copy["client_key"] = f"{blocker}-covered-copy"
            coverage["units"].append(covered_copy)
            coverage["units"][0]["status"] = blocker
            coverage["units"][0]["covered_by"] = []
            coverage["units"][0]["reason"] = "等待明确输入"
        split_path, coverage_path = write_documents(
            project,
            split,
            coverage,
            suffix=f"-{blocker}",
        )
        changed = run_cli(
            [
                "draft",
                "requirements",
                "DRAFT-001",
                "--split-file",
                str(split_path),
                "--coverage-file",
                str(coverage_path),
            ],
            cwd=project,
        )
        assert changed.returncode == 0, changed.stderr
    elif blocker == "unversioned":
        changed = run_cli(
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
            cwd=project,
        )
        assert changed.returncode == 0, changed.stderr
    else:
        changed = append_structured_cap(
            project,
            submission_key="pending-confirmation-fact",
            capture_type="fact",
            increment="确认后新增的事实尚未归入需求产物。",
        )
        assert changed.returncode == 0, changed.stderr

    before_events = len(load_events(paths))
    with pytest.raises(SdlcError, match="阻断项"):
        _confirm(paths)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert len(load_events(paths)) == before_events
    assert [item["confirmation_id"] for item in draft["requirement_confirmations"]] == [
        "RCF-001"
    ]
    assert draft["status"] == "requirement_reviewing"


def test_registry_tampering_and_projection_failure_leave_no_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _material, _request = _passed_project(tmp_path, monkeypatch)
    registry_path = project / ".codex-sdlc/trust/reviews/registry.json"
    original_registry = registry_path.read_text(encoding="utf-8")
    registry = json.loads(original_registry)
    registration = next(iter(registry["registrations"].values()))
    registration["result_sha256"] = "0" * 64
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SdlcError, match="登记"):
        _confirm(paths)
    assert not any(
        event["event_type"] == "draft_requirement_confirmed"
        for event in load_events(paths)
    )
    registry_path.write_text(original_registry, encoding="utf-8")

    before_events = paths.events_file.read_bytes()
    original_refresh = draft_service.refresh_materialized_state

    def fail_refresh(_paths):
        raise SdlcError("模拟投影写入失败。")

    monkeypatch.setattr(draft_service, "refresh_materialized_state", fail_refresh)
    with pytest.raises(SdlcError, match="模拟投影写入失败"):
        _confirm(paths)
    monkeypatch.setattr(draft_service, "refresh_materialized_state", original_refresh)

    assert paths.events_file.read_bytes() == before_events
    assert not (
        project
        / ".codex-sdlc/drafts/DRAFT-001/需求/requirement-confirmation.v1.json"
    ).exists()
    assert not list(
        (project / ".codex-sdlc/drafts/DRAFT-001").rglob("*.tmp")
    )
