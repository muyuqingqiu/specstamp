from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.external_version import normalized_url_sha256
from codex_sdlc.core.state import append_event, derive_state
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_file
from test_cli_v1 import init_demo_repo, read_events, run_cli
from test_cli_v17_draft_contract import (
    create_draft_with_material,
    import_command,
    requirement_documents,
    write_documents,
)


def write_package_json(project_dir: Path) -> None:
    project_dir.joinpath("package.json").write_text(
        json.dumps(
            {
                "name": "sdlc-discuss-prepare-demo",
                "private": True,
                "scripts": {"test": "node -e \"process.exit(0)\""},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def create_structured_draft(tmp_path: Path) -> tuple[Path, object]:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project).returncode == 0
    created = run_cli(["draft", "create", "订单导出"], cwd=project)
    assert created.returncode == 0, created.stderr
    return project, build_paths(project)


def reference(project: Path, path: Path) -> dict[str, object]:
    return {
        "schema_version": "reference-locator.v1",
        "path": path.relative_to(project).as_posix(),
        "sha256": sha256_file(path),
        "locator": {"kind": "whole_file"},
    }


def append_structured_cap(
    project: Path,
    *,
    submission_key: str,
    capture_type: str,
    increment: str,
    command: str = "discuss",
    draft_id: str = "DRAFT-001",
):
    source = project / f"{submission_key}.txt"
    source.write_text(increment + "\n", encoding="utf-8")
    target = project / f".codex-sdlc/drafts/{draft_id}/requirement.draft.md"
    document = {
        "schema_version": "capture-increment.v1",
        "submission_key": submission_key,
        "draft_id": draft_id,
        "client_key": submission_key,
        "capture_type": capture_type,
        "targets": [
            {
                "target_id": draft_id,
                "reference": reference(project, target),
            }
        ],
        "source_reference": reference(project, source),
        "source_sha256": sha256_file(source),
        "increment": increment,
        "status": "pending",
        "decisions": [],
    }
    input_file = project / f"{submission_key}.json"
    input_file.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_cli([command, "--file", input_file.name], cwd=project)


def assessment_codes(draft: dict[str, object]) -> list[str]:
    return [str(item["code"]) for item in draft["assessment"]["blockers"]]


def test_discuss_appends_structured_cap_without_creating_requirement_or_overwriting_draft(
    tmp_path: Path,
) -> None:
    project, paths = create_structured_draft(tmp_path)
    before = derive_state(paths)["drafts"]["DRAFT-001"]

    result = append_structured_cap(
        project,
        submission_key="question-export-source",
        capture_type="question",
        increment="是否需要导出订单来源？",
    )

    assert result.returncode == 0, result.stderr
    assert "CAP-001" in result.stdout
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["requirement_body"] == before["requirement_body"]
    assert draft["questions"] == before["questions"] == []
    assert draft["decisions"] == before["decisions"] == []
    assert [item["capture_id"] for item in draft["structured_captures"]] == ["CAP-001"]
    assert draft["decision_records"] == []
    assert assessment_codes(draft) == [
        "material_missing",
        "requirement_artifacts_missing",
        "open_question",
        "pending_capture",
    ]
    assert draft["assessment"]["open_questions"] == ["CAP-001"]

    with sqlite3.connect(project / ".codex-sdlc/sdlc.db") as connection:
        requirement_count = connection.execute("SELECT COUNT(*) FROM requirements").fetchone()[0]
        capture_count = connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    assert requirement_count == 0
    assert capture_count == 1
    capture_text = (project / ".codex-sdlc/captures/CAP-001.md").read_text(encoding="utf-8")
    assert "requirement_increment" in capture_text


def test_draft_detail_global_status_and_rebuilt_projection_share_state_and_blockers(
    tmp_path: Path,
) -> None:
    project, paths = create_structured_draft(tmp_path)
    result = append_structured_cap(
        project,
        submission_key="question-format",
        capture_type="question",
        increment="导出格式选 CSV 还是 Excel？",
    )
    assert result.returncode == 0, result.stderr

    detail = run_cli(["draft", "status", "DRAFT-001"], cwd=project)
    global_status = run_cli(["status"], cwd=project)
    status_file = paths.draft_dir("DRAFT-001") / "status.json"
    questions_file = paths.draft_dir("DRAFT-001") / "questions.md"
    status_payload = json.loads(status_file.read_text(encoding="utf-8"))

    assert detail.returncode == global_status.returncode == 0
    assert "DRAFT-001 -> discussing" in detail.stdout
    assert "open_question:CAP-001:open" in detail.stdout
    assert "DRAFT-001 [discussing]" in global_status.stdout
    assert "open_question:CAP-001:open" in global_status.stdout
    assert status_payload["status"] == "discussing"
    assert status_payload["assessment"]["open_questions"] == ["CAP-001"]
    assert "CAP-001 [open]" in questions_file.read_text(encoding="utf-8")

    expected_status = status_file.read_bytes()
    expected_questions = questions_file.read_bytes()
    status_file.write_text('{"status":"requirement_confirmed"}\n', encoding="utf-8")
    questions_file.write_text("用户已经确认\n", encoding="utf-8")
    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)

    assert refreshed.returncode == 0, refreshed.stderr
    assert status_file.read_bytes() == expected_status
    assert questions_file.read_bytes() == expected_questions


def test_capture_transition_is_atomic_idempotent_and_keeps_initial_record(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    appended = append_structured_cap(
        project,
        submission_key="fact-after-import",
        capture_type="fact",
        increment="导出结果保留订单号。",
    )
    assert appended.returncode == 0, appended.stderr
    before = derive_state(paths)
    initial = deepcopy(before["drafts"]["DRAFT-001"]["structured_captures"][0])
    relation_path = paths.draft_dir("DRAFT-001") / "需求/requirement-split.v1.json"
    transition = {
        "schema_version": "capture-transition.v1",
        "transition_key": "absorb-fact-after-import",
        "draft_id": "DRAFT-001",
        "capture_id": "CAP-001",
        "source_submission_key": initial["submission_key"],
        "source_submission_sha256": initial["submission_sha256"],
        "source_record_sha256": initial["record_sha256"],
        "from_status": "pending",
        "to_status": "absorbed",
        "relation": {
            "kind": "requirement_artifact",
            "target_id": "DRAFT-001",
            "reference": reference(project, relation_path),
        },
    }
    transition_file = project / "吸收事实.json"
    transition_file.write_text(
        json.dumps(transition, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    event_count = len(read_events(project))
    converted = run_cli(
        ["capture-transition", "--file", transition_file.name], cwd=project
    )

    assert converted.returncode == 0, converted.stderr
    assert "pending -> absorbed" in converted.stdout
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["structured_captures"][0] == initial
    assert draft["capture_statuses"] == {"CAP-001": "absorbed"}
    assert len(draft["capture_transitions"]) == 1
    assert state["captures"][-1]["initial_status"] == "pending"
    assert state["captures"][-1]["status"] == "absorbed"
    assert draft["status"] == "requirement_reviewing"
    assert len(read_events(project)) == event_count + 1

    retry = run_cli(
        ["capture-transition", "--file", transition_file.name], cwd=project
    )
    assert retry.returncode == 0, retry.stderr
    assert "重复提交：是" in retry.stdout
    assert len(read_events(project)) == event_count + 1

    invalid = deepcopy(transition)
    invalid["transition_key"] = "bad-source-record"
    invalid["source_record_sha256"] = "0" * 64
    invalid_file = project / "错误吸收事实.json"
    invalid_file.write_text(
        json.dumps(invalid, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rejected = run_cli(
        ["capture-transition", "--file", invalid_file.name], cwd=project
    )
    assert rejected.returncode == 1
    assert len(read_events(project)) == event_count + 1

    conflicting = deepcopy(transition)
    conflicting["transition_key"] = "reject-already-absorbed"
    conflicting["to_status"] = "rejected"
    conflicting["relation"] = {
        "kind": "rejection_record",
        "target_id": "DRAFT-001",
        "reference": reference(
            project, paths.draft_dir("DRAFT-001") / "requirement.draft.md"
        ),
    }
    conflicting_file = project / "冲突状态.json"
    conflicting_file.write_text(
        json.dumps(conflicting, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    conflicting_result = run_cli(
        ["capture-transition", "--file", conflicting_file.name], cwd=project
    )
    assert conflicting_result.returncode == 1
    assert len(read_events(project)) == event_count + 1

    next_cap = append_structured_cap(
        project,
        submission_key="fact-after-transition",
        capture_type="fact",
        increment="导出结果保留订单来源。",
    )
    assert next_cap.returncode == 0, next_cap.stderr
    assert "CAP-002" in next_cap.stdout

    # 单边改规范记录并重算公开哈希时，原始 submission 仍会把不一致拦住。
    baseline_events = read_events(project)
    mutations = (
        lambda record: record.__setitem__("transition_key", "rewritten-transition"),
        lambda record: record.__setitem__("to_status", "converted"),
        lambda record: record["relation"].__setitem__("kind", "material"),
        lambda record: record["relation"]["reference"].__setitem__(
            "sha256", "0" * 64
        ),
    )
    for mutate in mutations:
        tampered_events = deepcopy(baseline_events)
        transition_record = next(
            event["payload"]["transition"]
            for event in tampered_events
            if event["event_type"] == "structured_capture_transitioned"
        )
        mutate(transition_record)
        transition_record["transition_submission_sha256"] = canonical_sha256(
            {
                key: deepcopy(value)
                for key, value in transition_record.items()
                if key
                not in {
                    "transition_submission_sha256",
                    "previous_transition_sha256",
                    "transition_sha256",
                }
            }
        )
        transition_record["transition_sha256"] = canonical_sha256(
            {
                key: deepcopy(value)
                for key, value in transition_record.items()
                if key != "transition_sha256"
            }
        )
        paths.events_file.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in tampered_events
            ),
            encoding="utf-8",
        )

        with pytest.raises(SdlcError, match="原始 submission 锚点"):
            derive_state(paths)

    # 同时改 submission 和规范记录只排除真实性判断；公开合同本身非法时，重放仍必须确定性拒绝。
    projection_before = paths.draft_status_file("DRAFT-001").read_bytes()
    source_reference = deepcopy(initial["source_reference"])
    outside_draft_reference = reference(project, project / "fact-after-import.txt")
    semantic_cases = (
        (
            "material指向DRAFT",
            lambda relation: relation.__setitem__("kind", "material"),
            "必须引用 MAT-",
        ),
        (
            "MAT目标引用DRAFT文件",
            lambda relation: relation.update(
                {"kind": "material", "target_id": "MAT-001"}
            ),
            "没有命中 MAT-001 的真实来源定位",
        ),
        (
            "capture自引用",
            lambda relation: relation.update(
                {
                    "kind": "capture",
                    "target_id": "CAP-001",
                    "reference": deepcopy(source_reference),
                }
            ),
            "不能把 CAP 自己作为替代来源",
        ),
        (
            "未知目标",
            lambda relation: relation.update(
                {"kind": "material", "target_id": "MAT-999"}
            ),
            "目标编号不存在：MAT-999",
        ),
        (
            "DRAFT目录归属错误",
            lambda relation: relation.update(
                {
                    "kind": "requirement_artifact",
                    "target_id": "DRAFT-001",
                    "reference": deepcopy(outside_draft_reference),
                }
            ),
            "必须定位到自己的 DRAFT 目录",
        ),
    )
    for case_name, mutate_relation, expected_error in semantic_cases:
        tampered_events = deepcopy(baseline_events)
        payload = next(
            event["payload"]
            for event in tampered_events
            if event["event_type"] == "structured_capture_transitioned"
        )
        transition_record = payload["transition"]
        transition_submission = payload["transition_submission"]
        mutate_relation(transition_record["relation"])
        mutate_relation(transition_submission["relation"])
        if transition_record["relation"]["kind"] == "capture":
            transition_record["to_status"] = "superseded"
            transition_submission["to_status"] = "superseded"
        transition_record["transition_submission_sha256"] = canonical_sha256(
            transition_submission
        )
        transition_record["transition_sha256"] = canonical_sha256(
            {
                key: deepcopy(value)
                for key, value in transition_record.items()
                if key != "transition_sha256"
            }
        )
        paths.events_file.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in tampered_events
            ),
            encoding="utf-8",
        )

        try:
            derive_state(paths)
        except SdlcError as exc:
            assert expected_error in str(exc), (case_name, str(exc))
        else:
            pytest.fail(f"{case_name} 没有在事件重放时被拒绝")
        refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)
        assert refreshed.returncode == 1
        assert expected_error in refreshed.stderr
        assert paths.draft_status_file("DRAFT-001").read_bytes() == projection_before

    paths.events_file.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False) + "\n" for event in baseline_events
        ),
        encoding="utf-8",
    )
    assert (
        derive_state(paths)["drafts"]["DRAFT-001"]["capture_statuses"]["CAP-001"]
        == "absorbed"
    )


def test_capture_transition_four_terminal_states_and_concurrent_retry(
    tmp_path: Path,
) -> None:
    """四种合法终态共用同一关系规则，并发重试只能登记一条转换事件。"""

    def complete_project(root: Path) -> tuple[Path, object, Path]:
        root.mkdir(parents=True, exist_ok=True)
        project, paths, material = create_draft_with_material(root)
        split, coverage = requirement_documents(project, material)
        split_path, coverage_path = write_documents(project, split, coverage)
        imported = run_cli(import_command(split_path, coverage_path), cwd=project)
        assert imported.returncode == 0, imported.stderr
        return (
            project,
            paths,
            paths.draft_dir("DRAFT-001") / "需求/requirement-split.v1.json",
        )

    def transition_document(
        capture: dict[str, object],
        *,
        transition_key: str,
        to_status: str,
        relation: dict[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": "capture-transition.v1",
            "transition_key": transition_key,
            "draft_id": "DRAFT-001",
            "capture_id": capture["capture_id"],
            "source_submission_key": capture["submission_key"],
            "source_submission_sha256": capture["submission_sha256"],
            "source_record_sha256": capture["record_sha256"],
            "from_status": "pending",
            "to_status": to_status,
            "relation": relation,
        }

    project, paths, split_path = complete_project(tmp_path / "四终态")
    for index in range(1, 6):
        appended = append_structured_cap(
            project,
            submission_key=f"terminal-{index}",
            capture_type="fact",
            increment=f"终态事实{index}。",
        )
        assert appended.returncode == 0, appended.stderr
    captures = {
        item["capture_id"]: item
        for item in derive_state(paths)["drafts"]["DRAFT-001"]["structured_captures"]
    }
    draft_reference = reference(project, split_path)
    rejection_reference = reference(
        project, paths.draft_dir("DRAFT-001") / "requirement.draft.md"
    )
    terminal_inputs = (
        (
            "CAP-001",
            "absorbed",
            {
                "kind": "requirement_artifact",
                "target_id": "DRAFT-001",
                "reference": deepcopy(draft_reference),
            },
        ),
        (
            "CAP-002",
            "converted",
            {
                "kind": "requirement_artifact",
                "target_id": "DRAFT-001",
                "reference": deepcopy(draft_reference),
            },
        ),
        (
            "CAP-003",
            "rejected",
            {
                "kind": "rejection_record",
                "target_id": "DRAFT-001",
                "reference": rejection_reference,
            },
        ),
        (
            "CAP-004",
            "superseded",
            {
                "kind": "capture",
                "target_id": "CAP-005",
                "reference": deepcopy(captures["CAP-005"]["source_reference"]),
            },
        ),
    )
    event_count = len(read_events(project))
    first_file: Path | None = None
    for capture_id, to_status, relation in terminal_inputs:
        input_file = project / f"{capture_id}-{to_status}.json"
        input_file.write_text(
            json.dumps(
                transition_document(
                    captures[capture_id],
                    transition_key=f"terminal-{capture_id.lower()}",
                    to_status=to_status,
                    relation=relation,
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result = run_cli(
            ["capture-transition", "--file", input_file.name], cwd=project
        )
        assert result.returncode == 0, result.stderr
        first_file = first_file or input_file
    status = derive_state(paths)["drafts"]["DRAFT-001"]["capture_statuses"]
    assert status == {
        "CAP-001": "absorbed",
        "CAP-002": "converted",
        "CAP-003": "rejected",
        "CAP-004": "superseded",
        "CAP-005": "pending",
    }
    assert len(read_events(project)) == event_count + 4
    assert first_file is not None
    retry = run_cli(["capture-transition", "--file", first_file.name], cwd=project)
    assert retry.returncode == 0, retry.stderr
    assert "重复提交：是" in retry.stdout
    assert len(read_events(project)) == event_count + 4

    concurrent_project, concurrent_paths, concurrent_split = complete_project(
        tmp_path / "并发重试"
    )
    appended = append_structured_cap(
        concurrent_project,
        submission_key="concurrent-terminal",
        capture_type="fact",
        increment="并发重试事实。",
    )
    assert appended.returncode == 0, appended.stderr
    capture = derive_state(concurrent_paths)["drafts"]["DRAFT-001"][
        "structured_captures"
    ][0]
    concurrent_input = concurrent_project / "并发转换.json"
    concurrent_input.write_text(
        json.dumps(
            transition_document(
                capture,
                transition_key="concurrent-terminal",
                to_status="absorbed",
                relation={
                    "kind": "requirement_artifact",
                    "target_id": "DRAFT-001",
                    "reference": reference(concurrent_project, concurrent_split),
                },
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before_concurrent = len(read_events(concurrent_project))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: run_cli(
                    ["capture-transition", "--file", concurrent_input.name],
                    cwd=concurrent_project,
                ),
                range(2),
            )
        )
    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    assert len(read_events(concurrent_project)) == before_concurrent + 1
    assert derive_state(concurrent_paths)["drafts"]["DRAFT-001"][
        "capture_statuses"
    ]["CAP-001"] == "absorbed"


def test_material_hash_drift_has_one_state_across_commands_and_projections(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    archived = paths.draft_dir("DRAFT-001") / str(material["stored_path"])
    archived.write_text("归档字节已经漂移\n", encoding="utf-8")

    detail = run_cli(["draft", "status", "DRAFT-001"], cwd=project)
    global_status = run_cli(["status"], cwd=project)
    next_result = run_cli(["next"], cwd=project)
    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)

    assert detail.returncode == global_status.returncode == next_result.returncode == 0
    assert "DRAFT-001 -> discussing" in detail.stdout
    assert "material_unstable:MAT-001:hash_drift" in detail.stdout
    assert "DRAFT-001 [discussing]" in global_status.stdout
    assert "material_unstable:MAT-001:hash_drift" in global_status.stdout
    assert "material_unstable:MAT-001:hash_drift" in next_result.stdout
    assert refreshed.returncode == 1
    assert "哈希已经变化" in refreshed.stderr

    status_payload = json.loads(
        paths.draft_status_file("DRAFT-001").read_text(encoding="utf-8")
    )
    questions = (paths.draft_dir("DRAFT-001") / "questions.md").read_text(
        encoding="utf-8"
    )
    assert status_payload["status"] == "discussing"
    assert status_payload["assessment"]["blockers"][0] == {
        "code": "material_unstable",
        "source_id": "MAT-001",
        "status": "hash_drift",
        "reference": str(material["stored_path"]),
    }
    assert "MAT-001 [hash_drift] material_unstable" in questions


def test_external_version_evidence_is_rechecked_from_structured_fields(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "requirement_reviewing"
    url = "https://example.com/versioned-requirement"
    evidence = {
        "schema_version": "external-version-evidence.v1",
        "normalized_url_sha256": normalized_url_sha256(url),
        "status": "confirmed",
        "evidence": {
            "kind": "immutable_revision",
            "provider": "document",
            "revision": "rev-001",
        },
    }
    evidence_file = project / "外部版本证据.json"
    evidence_file.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    added = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "ui-design",
            "--title",
            "外部需求资料",
            "--url",
            url,
            "--version-evidence",
            evidence_file.name,
        ],
        cwd=project,
    )
    assert added.returncode == 0, added.stderr
    events = read_events(project)
    material_event = next(
        event
        for event in events
        if event["event_type"] == "draft_material_added"
        and event["payload"]["material"].get("source_kind")
        == "external-reference"
    )
    material_event["payload"]["material"]["version_evidence"][
        "normalized_url_sha256"
    ] = "0" * 64
    paths.events_file.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )

    # refresh 先写同一诊断投影，再拒绝用漂移事件覆盖仍可信的资料清单。
    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)
    detail = run_cli(["draft", "status", "DRAFT-001"], cwd=project)
    global_status = run_cli(["status"], cwd=project)
    next_result = run_cli(["next"], cwd=project)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    status_payload = json.loads(
        paths.draft_status_file("DRAFT-001").read_text(encoding="utf-8")
    )
    questions = (paths.draft_dir("DRAFT-001") / "questions.md").read_text(
        encoding="utf-8"
    )

    assert refreshed.returncode == 1
    assert "资料清单事件与派生产物登记不一致" in refreshed.stderr
    assert detail.returncode == global_status.returncode == next_result.returncode == 0
    assert "DRAFT-001 -> discussing" in detail.stdout
    assert "material_unstable:MAT-002:evidence_drift" in detail.stdout
    assert "DRAFT-001 [discussing]" in global_status.stdout
    assert "material_unstable:MAT-002:evidence_drift" in global_status.stdout
    assert "material_unstable:MAT-002:evidence_drift" in next_result.stdout
    assert draft["status"] == "discussing"
    assert draft["assessment"]["blockers"][0] == {
        "code": "material_unstable",
        "source_id": "MAT-002",
        "status": "evidence_drift",
        "reference": "version_evidence",
    }
    assert status_payload["status"] == "discussing"
    assert status_payload["assessment"]["blockers"][0] == draft["assessment"][
        "blockers"
    ][0]
    assert "MAT-002 [evidence_drift] material_unstable" in questions


def test_free_text_decision_cannot_close_structured_question_or_advance_state(
    tmp_path: Path,
) -> None:
    project, paths = create_structured_draft(tmp_path)
    assert append_structured_cap(
        project,
        submission_key="question-permission",
        capture_type="question",
        increment="谁可以导出订单？",
    ).returncode == 0

    legacy_decision = run_cli(
        ["draft", "decision", "DRAFT-001", "用户已经确认 requirement_confirmed"],
        cwd=project,
    )

    assert legacy_decision.returncode == 0, legacy_decision.stderr
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["assessment"]["open_questions"] == ["CAP-001"]
    assert draft["decision_records"] == []
    assert draft["decisions"] == ["用户已经确认 requirement_confirmed"]
    assert "open_question" in assessment_codes(draft)


def test_multi_round_caps_replay_without_losing_prior_structured_content(tmp_path: Path) -> None:
    project, paths = create_structured_draft(tmp_path)

    first = append_structured_cap(
        project,
        submission_key="round-one",
        capture_type="fact",
        increment="导出文件包含订单号。",
    )
    second = append_structured_cap(
        project,
        submission_key="round-two",
        capture_type="question",
        increment="是否包含订单来源？",
        command="capture",
    )

    assert first.returncode == second.returncode == 0
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert [item["capture_id"] for item in draft["structured_captures"]] == [
        "CAP-001",
        "CAP-002",
    ]
    assert [item["increment"] for item in draft["structured_captures"]] == [
        "导出文件包含订单号。",
        "是否包含订单来源？",
    ]
    assert draft["assessment"]["open_questions"] == ["CAP-002"]
    assert [event["source"] for event in read_events(project) if event["event_type"] == "draft_mutated"][-2:] == [
        "sdlc-discuss",
        "sdlc-capture",
    ]


def test_material_and_requirement_artifact_changes_recalculate_requirement_stage(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    before_import = derive_state(paths)["drafts"]["DRAFT-001"]
    assert before_import["status"] == "discussing"
    assert "requirement_artifacts_missing" in assessment_codes(before_import)

    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert imported.returncode == 0, imported.stderr
    ready = derive_state(paths)["drafts"]["DRAFT-001"]
    assert ready["status"] == "requirement_reviewing"
    assert assessment_codes(ready) == ["requirement_review_pending"]
    detail = run_cli(["draft", "status", "DRAFT-001"], cwd=project)
    assert "DRAFT-001 -> requirement_reviewing" in detail.stdout

    revised_source = project / "课程访问需求-修订.md"
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
    stale = derive_state(paths)["drafts"]["DRAFT-001"]
    assert stale["status"] == "discussing"
    assert assessment_codes(stale) == ["requirement_artifacts_stale"]


def test_old_draft_event_remains_readable_without_fabricated_cap_or_decision(
    tmp_path: Path,
) -> None:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project).returncode == 0
    paths = build_paths(project)
    append_event(
        paths,
        event_type="draft_created",
        source="legacy-test",
        summary="读取旧 DRAFT",
        payload={
            "draft_id": "DRAFT-001",
            "title": "旧草稿",
            "status": "needs_user",
            "questions": ["旧问题"],
            "decisions": ["旧决定"],
        },
    )

    draft = derive_state(paths)["drafts"]["DRAFT-001"]

    assert draft["status"] == "needs_user"
    assert draft["questions"] == ["旧问题"]
    assert draft["decisions"] == ["旧决定"]
    assert "structured_captures" not in draft
    assert "decision_records" not in draft


def test_structured_cap_uses_explicit_draft_when_multiple_drafts_exist(
    tmp_path: Path,
) -> None:
    project, paths = create_structured_draft(tmp_path)
    second = run_cli(["draft", "create", "订单导出日志"], cwd=project)
    assert second.returncode == 0, second.stderr

    result = append_structured_cap(
        project,
        submission_key="second-draft-question",
        capture_type="question",
        increment="导出日志是否需要记录筛选条件？",
        draft_id="DRAFT-002",
    )

    assert result.returncode == 0, result.stderr
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["structured_captures"] == []
    assert [
        item["capture_id"]
        for item in state["drafts"]["DRAFT-002"]["structured_captures"]
    ] == ["CAP-001"]
    assert state["drafts"]["DRAFT-002"]["assessment"]["open_questions"] == [
        "CAP-001"
    ]
    assert state["captures"][-1]["draft_id"] == "DRAFT-002"


def test_structured_change_workspace_does_not_modify_current_requirement(
    tmp_path: Path,
) -> None:
    import sys

    contracts_dir = Path(__file__).parent / "contracts"
    if str(contracts_dir) not in sys.path:
        sys.path.insert(0, str(contracts_dir))
    from test_change_workspace_contract import _minimal_formal_project

    project_dir, _requirement_dir = _minimal_formal_project(tmp_path)
    before_events = len(read_events(project_dir))

    created = run_cli(
        [
            "change-create",
            "REQ-001",
            "--request-key",
            "order-source-filter",
        ],
        cwd=project_dir,
    )
    duplicate = run_cli(
        [
            "change-create",
            "REQ-001",
            "--request-key",
            "order-source-filter",
        ],
        cwd=project_dir,
    )

    assert created.returncode == duplicate.returncode == 0
    assert "变更：CHG-001" in created.stdout
    assert "变更：CHG-001" in duplicate.stdout
    requirement_dir = next(
        (project_dir / ".codex-sdlc/requirements").glob("REQ-001-*")
    )
    status_path = requirement_dir / "changes/CHG-001/status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["schema_version"] == "change-workspace.v1"
    assert status["requirement_id"] == "REQ-001"
    assert status["change_id"] == "CHG-001"
    assert status["status"] == "draft"
    for base in status["base_versions"].values():
        assert sha256_file(project_dir / str(base["path"])) == base["sha256"]
    change_events = [
        event
        for event in read_events(project_dir)[before_events:]
        if event["event_type"] == "change_workspace_created"
    ]
    assert len(change_events) == 1


def test_init_and_next_keep_requirement_discussion_as_the_first_step(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)

    init_result = run_cli(["init"], cwd=project_dir)
    next_result = run_cli(["next"], cwd=project_dir)

    assert init_result.returncode == next_result.returncode == 0
    assert "$sdlc-discuss 需求想法" in init_result.stdout
    assert "- 主推荐：$sdlc-discuss 需求想法" in next_result.stdout
    assert "$sdlc-design 技术方案草案" in next_result.stdout
