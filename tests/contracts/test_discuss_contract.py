from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_bytes, sha256_file
from codex_sdlc.services import discuss_service
from test_cli_v1 import init_demo_repo, read_events, run_cli


def reference(
    project: Path,
    path: Path,
    *,
    locator: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "reference-locator.v1",
        "path": path.relative_to(project).as_posix(),
        "sha256": sha256_file(path),
        "locator": locator,
    }


def text_reference(project: Path, path: Path) -> dict[str, object]:
    return reference(
        project,
        path,
        locator={
            "kind": "text_range",
            "line_start": 1,
            "line_end": 1,
            "fragment_sha256": sha256_bytes(path.read_bytes()),
        },
    )


def write_document(project: Path, name: str, document: dict[str, object]) -> Path:
    path = project / name
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def create_draft_project(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project).returncode == 0
    result = run_cli(["draft", "create", "订单导出"], cwd=project)
    assert result.returncode == 0, result.stderr
    target_path = project / ".codex-sdlc" / "drafts" / "DRAFT-001" / "requirement.draft.md"
    target = {
        "target_id": "DRAFT-001",
        "reference": reference(project, target_path, locator={"kind": "whole_file"}),
    }
    return project, target


def increment_document(
    *,
    submission_key: str,
    client_key: str,
    capture_type: str,
    target: dict[str, object],
    source: dict[str, object],
    increment: str,
    decisions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "capture-increment.v1",
        "submission_key": submission_key,
        "draft_id": "DRAFT-001",
        "client_key": client_key,
        "capture_type": capture_type,
        "targets": [deepcopy(target)],
        "source_reference": deepcopy(source),
        "source_sha256": source["sha256"],
        "increment": increment,
        "status": "pending",
        "decisions": deepcopy(decisions or []),
    }


def run_increment(
    project: Path,
    name: str,
    document: dict[str, object],
    *,
    command: str = "discuss",
):
    return run_cli([command, "--file", str(write_document(project, name, document))], cwd=project)


def test_discuss_quality_does_not_read_question_state_from_markdown_alias() -> None:
    body = """# 需求草稿

## 待确认问题
- 用户还没确认导出字段。
"""

    quality = discuss_service.requirement_draft_quality(body, [])

    assert quality["status"] == "discussing"
    assert quality["open_questions"] == []


def test_discuss_quality_uses_structured_questions() -> None:
    quality = discuss_service.requirement_draft_quality(
        "# 需求草稿\n\n## 待确认问题\n\n- 这行只是展示。\n",
        ["用户还没确认导出字段。"],
    )

    assert quality["status"] == "needs_user"
    assert quality["open_questions"] == ["用户还没确认导出字段。"]


def test_multi_round_structured_caps_only_append_and_keep_confirmed_content(tmp_path: Path) -> None:
    project, _initial_target = create_draft_project(tmp_path)
    requirement_body = "# 订单导出需求\n\n这段已确认原文必须保持不变。\n"
    requirement_result = run_cli(
        ["draft", "requirement", "DRAFT-001", requirement_body], cwd=project
    )
    assert requirement_result.returncode == 0, requirement_result.stderr
    decision_result = run_cli(
        ["draft", "decision", "DRAFT-001", "原有兼容决定"], cwd=project
    )
    assert decision_result.returncode == 0, decision_result.stderr

    target_path = project / ".codex-sdlc" / "drafts" / "DRAFT-001" / "requirement.draft.md"
    target = {
        "target_id": "DRAFT-001",
        "reference": reference(project, target_path, locator={"kind": "whole_file"}),
    }
    before = derive_state(build_paths(project))["drafts"]["DRAFT-001"]
    before_requirement = before["requirement_body"]
    before_decisions = list(before["decisions"])

    first_source_path = project / "第一轮.txt"
    first_source_path.write_text("新增导出字段说明\n", encoding="utf-8")
    first_source = text_reference(project, first_source_path)
    first = increment_document(
        submission_key="round-one",
        client_key="cap-round-one",
        capture_type="fact",
        target=target,
        source=first_source,
        increment="新增导出字段说明",
    )
    first_result = run_increment(project, "第一轮.json", first)
    assert first_result.returncode == 0, first_result.stderr

    second_source_path = project / "第二轮.txt"
    second_source_path.write_text("是否需要包含订单来源？\n", encoding="utf-8")
    second_source = text_reference(project, second_source_path)
    second = increment_document(
        submission_key="round-two",
        client_key="cap-round-two",
        capture_type="question",
        target=target,
        source=second_source,
        increment="是否需要包含订单来源？",
    )
    second_result = run_increment(project, "第二轮.json", second, command="capture")
    assert second_result.returncode == 0, second_result.stderr

    after = derive_state(build_paths(project))["drafts"]["DRAFT-001"]
    assert after["requirement_body"] == before_requirement
    assert after["decisions"] == before_decisions
    assert after["questions"] == before["questions"]
    assert [item["capture_id"] for item in derive_state(build_paths(project))["captures"]] == [
        "CAP-001",
        "CAP-002",
    ]

    structured = discuss_service.structured_increment_records(read_events(project))
    assert [item["capture"]["capture_type"] for item in structured] == ["fact", "question"]
    assert [item["capture"]["capture_id"] for item in structured] == ["CAP-001", "CAP-002"]
    assert all(item["capture"]["status"] == "pending" for item in structured)
    assert all(item["decisions"] == [] for item in structured)
    assert requirement_body.strip() not in (
        project / ".codex-sdlc" / "captures" / "CAP-001.md"
    ).read_text(encoding="utf-8")


def test_structured_decision_has_stable_id_and_retry_is_idempotent(tmp_path: Path) -> None:
    project, target = create_draft_project(tmp_path)
    question_path = project / "问题.txt"
    question_path.write_text("导出格式选哪一种？\n", encoding="utf-8")
    question_reference = text_reference(project, question_path)
    question_document = increment_document(
        submission_key="question-format",
        client_key="cap-question-format",
        capture_type="question",
        target=target,
        source=question_reference,
        increment="导出格式选哪一种？",
    )
    assert run_increment(project, "问题.json", question_document).returncode == 0

    selection_path = project / "用户选择.txt"
    selection_path.write_text("Excel\n", encoding="utf-8")
    selection_reference = text_reference(project, selection_path)
    decision = {
        "schema_version": "decision-input.v1",
        "client_key": "decision-format",
        "question": {
            "text": "导出格式选哪一种？",
            "capture_ref": "CAP-001",
            "reference": question_reference,
        },
        "candidates": ["CSV", "Excel"],
        "selection": "Excel",
        "scope": [deepcopy(target)],
        "source_reference": selection_reference,
        "confirmed_at": "2026-07-16T08:00:00+08:00",
    }
    document = increment_document(
        submission_key="decision-format",
        client_key="cap-decision-format",
        capture_type="decision",
        target=target,
        source=selection_reference,
        increment="Excel",
        decisions=[decision],
    )
    input_file = write_document(project, "决定.json", document)
    first = run_cli(["discuss", "--file", str(input_file)], cwd=project)
    assert first.returncode == 0, first.stderr
    assert "CAP-002" in first.stdout
    assert "DEC-001" in first.stdout
    event_count = len(read_events(project))

    retry = run_cli(["discuss", "--file", str(input_file)], cwd=project)
    assert retry.returncode == 0, retry.stderr
    assert "重复提交：是" in retry.stdout
    assert "CAP-002" in retry.stdout
    assert "DEC-001" in retry.stdout
    assert len(read_events(project)) == event_count

    records = discuss_service.structured_increment_records(read_events(project))
    stored_decision = records[-1]["decisions"][0]
    assert stored_decision["decision_id"] == "DEC-001"
    assert stored_decision["question"] == {
        "text": "导出格式选哪一种？",
        "capture_ref": "CAP-001",
        "reference": question_reference,
    }
    assert stored_decision["candidates"] == ["CSV", "Excel"]
    assert stored_decision["selection"] == "Excel"
    assert stored_decision["scope"] == [target]
    assert stored_decision["source_capture_id"] == "CAP-002"
    assert stored_decision["confirmed_at"] == "2026-07-16T08:00:00+08:00"
    assert len(stored_decision["decision_sha256"]) == 64


def test_hash_drift_invalid_target_and_wrong_question_reference_leave_no_event(
    tmp_path: Path,
) -> None:
    project, target = create_draft_project(tmp_path)
    source_path = project / "来源.txt"
    source_path.write_text("新增事实\n", encoding="utf-8")
    source = text_reference(project, source_path)
    document = increment_document(
        submission_key="reject-drift",
        client_key="cap-reject-drift",
        capture_type="fact",
        target=target,
        source=source,
        increment="新增事实",
    )
    source_path.write_text("内容已经变化\n", encoding="utf-8")
    before = len(read_events(project))
    drift = run_increment(project, "漂移.json", document)
    assert drift.returncode != 0
    assert "sha256 不一致" in drift.stderr
    assert len(read_events(project)) == before

    valid_source_path = project / "合法来源.txt"
    valid_source_path.write_text("新增事实\n", encoding="utf-8")
    valid_source = text_reference(project, valid_source_path)
    unknown_target = deepcopy(target)
    unknown_target["target_id"] = "FR-999"
    invalid = increment_document(
        submission_key="reject-target",
        client_key="cap-reject-target",
        capture_type="fact",
        target=unknown_target,
        source=valid_source,
        increment="新增事实",
    )
    invalid_result = run_increment(project, "非法目标.json", invalid)
    assert invalid_result.returncode == 1
    assert "目标编号不存在：FR-999" in invalid_result.stderr
    assert len(read_events(project)) == before

    question_path = project / "正式问题.txt"
    question_path.write_text("是否导出来源？\n", encoding="utf-8")
    question_reference = text_reference(project, question_path)
    question_document = increment_document(
        submission_key="valid-question",
        client_key="cap-valid-question",
        capture_type="question",
        target=target,
        source=question_reference,
        increment="是否导出来源？",
    )
    assert run_increment(project, "正式问题.json", question_document).returncode == 0
    unrelated_path = project / "错误问题定位.txt"
    unrelated_path.write_text("是否导出来源？\n", encoding="utf-8")
    unrelated_reference = text_reference(project, unrelated_path)
    selection_path = project / "选择.txt"
    selection_path.write_text("是\n", encoding="utf-8")
    selection_reference = text_reference(project, selection_path)
    wrong_decision = {
        "schema_version": "decision-input.v1",
        "client_key": "wrong-question-ref",
        "question": {
            "text": "是否导出来源？",
            "capture_ref": "CAP-001",
            "reference": unrelated_reference,
        },
        "candidates": ["是", "否"],
        "selection": "是",
        "scope": [deepcopy(target)],
        "source_reference": selection_reference,
        "confirmed_at": "2026-07-16T08:10:00+08:00",
    }
    wrong_document = increment_document(
        submission_key="wrong-question-ref",
        client_key="cap-wrong-question-ref",
        capture_type="decision",
        target=target,
        source=selection_reference,
        increment="是",
        decisions=[wrong_decision],
    )
    before_wrong = len(read_events(project))
    wrong_result = run_increment(project, "错误问题引用.json", wrong_document)
    assert wrong_result.returncode == 1
    assert "question.reference 不属于所引用 CAP" in wrong_result.stderr
    assert len(read_events(project)) == before_wrong

    corrected_decision = deepcopy(wrong_decision)
    corrected_decision["client_key"] = "correct-question-ref"
    corrected_decision["question"]["reference"] = deepcopy(question_reference)
    corrected_document = increment_document(
        submission_key="correct-question-ref",
        client_key="cap-correct-question-ref",
        capture_type="decision",
        target=target,
        source=selection_reference,
        increment="是",
        decisions=[corrected_decision],
    )
    corrected = run_increment(project, "正确问题引用.json", corrected_document)
    assert corrected.returncode == 0, corrected.stderr
    assert "CAP-002" in corrected.stdout
    assert "DEC-001" in corrected.stdout
    draft = derive_state(build_paths(project))["drafts"]["DRAFT-001"]
    assert draft["assessment"]["open_questions"] == []


def test_replay_rejects_coordinated_rehash_that_rewrites_initial_cap_status(
    tmp_path: Path,
) -> None:
    project, target = create_draft_project(tmp_path)
    source_path = project / "待处理事实.txt"
    source_path.write_text("待处理事实\n", encoding="utf-8")
    document = increment_document(
        submission_key="tamper-status",
        client_key="cap-tamper-status",
        capture_type="fact",
        target=target,
        source=text_reference(project, source_path),
        increment="待处理事实",
    )
    assert run_increment(project, "待处理事实.json", document).returncode == 0
    paths = build_paths(project)
    events = read_events(project)
    capture_payload = events[-1]["payload"]["capture"]
    record = capture_payload["structured_increment"]
    record["status"] = "absorbed"
    record["record_sha256"] = canonical_sha256(
        {key: deepcopy(value) for key, value in record.items() if key != "record_sha256"}
    )
    # 外层状态也一起改掉，重放仍会根据不可变初始合同拒绝这组协调篡改。
    capture_payload["status"] = "absorbed"
    paths.events_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="初始记录只能是 pending"):
        derive_state(paths)


def test_conflicting_duplicate_decision_is_rejected_before_allocating_ids(tmp_path: Path) -> None:
    project, target = create_draft_project(tmp_path)
    question_path = project / "问题.txt"
    question_path.write_text("是否导出来源？\n", encoding="utf-8")
    question_reference = text_reference(project, question_path)
    question_document = increment_document(
        submission_key="question-source",
        client_key="cap-question-source",
        capture_type="question",
        target=target,
        source=question_reference,
        increment="是否导出来源？",
    )
    assert run_increment(project, "问题.json", question_document).returncode == 0

    def decision_document(selection: str, suffix: str) -> dict[str, object]:
        selection_path = project / f"选择-{suffix}.txt"
        selection_path.write_text(selection + "\n", encoding="utf-8")
        selection_reference = text_reference(project, selection_path)
        decision = {
            "schema_version": "decision-input.v1",
            "client_key": f"decision-{suffix}",
            "question": {
                "text": "是否导出来源？",
                "capture_ref": "CAP-001",
                "reference": question_reference,
            },
            "candidates": ["是", "否"],
            "selection": selection,
            "scope": [deepcopy(target)],
            "source_reference": selection_reference,
            "confirmed_at": "2026-07-16T08:20:00+08:00",
        }
        return increment_document(
            submission_key=f"decision-{suffix}",
            client_key=f"cap-decision-{suffix}",
            capture_type="decision",
            target=target,
            source=selection_reference,
            increment=selection,
            decisions=[decision],
        )

    accepted = run_increment(project, "接受决定.json", decision_document("是", "yes"))
    assert accepted.returncode == 0, accepted.stderr
    before_conflict = len(read_events(project))
    conflict = run_increment(project, "冲突决定.json", decision_document("否", "no"))
    assert conflict.returncode == 1
    assert "DEC-001 冲突" in conflict.stderr
    assert len(read_events(project)) == before_conflict

    next_source_path = project / "后续事实.txt"
    next_source_path.write_text("后续事实\n", encoding="utf-8")
    next_source = text_reference(project, next_source_path)
    next_document = increment_document(
        submission_key="after-rejection",
        client_key="cap-after-rejection",
        capture_type="fact",
        target=target,
        source=next_source,
        increment="后续事实",
    )
    next_result = run_increment(project, "后续事实.json", next_document)
    assert next_result.returncode == 0, next_result.stderr
    assert "CAP-003" in next_result.stdout


def test_free_text_and_markdown_cannot_create_decision_or_modify_draft(tmp_path: Path) -> None:
    project, target = create_draft_project(tmp_path)
    before_events = len(read_events(project))
    before_draft = deepcopy(derive_state(build_paths(project))["drafts"]["DRAFT-001"])

    free_discuss = run_cli(
        ["discuss", "用户选择 Excel", "--decision", "Excel"], cwd=project
    )
    assert free_discuss.returncode == 1
    assert "只能追加结构化 CAP" in free_discuss.stderr
    free_capture = run_cli(["capture", "用户选择 Excel"], cwd=project)
    assert free_capture.returncode == 1
    assert "只能追加结构化 CAP" in free_capture.stderr
    assert len(read_events(project)) == before_events
    assert derive_state(build_paths(project))["drafts"]["DRAFT-001"] == before_draft

    markdown_path = project / "普通讨论.md"
    markdown = "# 用户决定\n\n- 决定使用 Excel\n"
    markdown_path.write_text(markdown, encoding="utf-8")
    markdown_reference = reference(
        project,
        markdown_path,
        locator={
            "kind": "text_range",
            "line_start": 1,
            "line_end": 3,
            "fragment_sha256": sha256_bytes(markdown_path.read_bytes()),
        },
    )
    document = increment_document(
        submission_key="markdown-is-not-decision",
        client_key="cap-markdown-is-not-decision",
        capture_type="fact",
        target=target,
        source=markdown_reference,
        increment=markdown.strip(),
    )
    result = run_increment(project, "普通讨论.json", document)
    assert result.returncode == 0, result.stderr
    stored = discuss_service.structured_increment_records(read_events(project))[-1]
    assert stored["capture"]["capture_type"] == "fact"
    assert stored["decisions"] == []
    after_draft = derive_state(build_paths(project))["drafts"]["DRAFT-001"]
    assert after_draft["decisions"] == before_draft["decisions"]
    assert after_draft["questions"] == before_draft["questions"]
    assert after_draft["requirement_body"] == before_draft["requirement_body"]
