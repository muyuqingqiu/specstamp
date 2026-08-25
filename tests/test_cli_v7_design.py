from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
sys.path.insert(0, str(CONTRACTS_DIR))

from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event
from codex_sdlc.core.state import derive_state, load_events, refresh_materialized_state
from test_cli_v1 import SDLC_SKILLS_HOME, init_demo_repo, run_cli
from test_design_artifact_contract import (
    _artifact,
    _import_artifact,
    _project_with_plan,
    _write_artifact,
)
from test_design_plan_contract import _module
from test_design_reference_contract import (
    create_confirmed_design_project,
    import_reference,
    write_design_reference,
)
from test_design_summary_contract import _import_summary, _object, _write_summary


def _minimal_summary() -> dict[str, object]:
    """只登记真实存在的数据与接口共同对象，避免测试靠空总体说明推进状态。"""

    return {
        "schema_version": "design-summary.v1",
        "draft_id": "DRAFT-001",
        "common_objects": [
            _object(
                "COMMON-001",
                "entity",
                ["DATA-001#ENT-001"],
                ["API-001", "DATA-001"],
                canonical_name="用户",
                contract="数据模块和接口模块共同使用同一个用户实体。",
            )
        ],
        "affected_modules": ["API-001", "DATA-001"],
        "open_questions": [],
    }


def test_design_reference_records_then_confirms_original_solution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, original_bytes, material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    reference = write_design_reference(project)

    imported = import_reference(project, reference)
    confirmed = run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    )

    assert imported.returncode == 0, imported.stderr
    assert "已导入技术方案引用：DES-001" in imported.stdout
    assert "project-technical-solution -> DES-001" in imported.stdout
    assert confirmed.returncode == 0, confirmed.stderr
    assert "已确认技术方案引用：DES-001" in confirmed.stdout
    archived = paths.draft_dir("DRAFT-001") / str(material["stored_path"])
    assert archived.read_bytes() == original_bytes
    assert (project / "项目技术方案.md").read_bytes() == original_bytes

    record = derive_state(paths)["design_references"][0]
    assert record["status"] == "confirmed"
    assert record["anchors"][0]["key"] == "DES-001#architecture"
    assert record["path"] == material["stored_path"]
    assert record["sha256"] == material["sha256"]
    assert record["applies_to"] == ["FR-001"]
    assert "title" not in record and "summary" not in record


def test_design_reference_idempotency_uses_client_key_not_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    first = write_design_reference(project)
    assert import_reference(project, first).returncode == 0
    before = load_events(paths)
    renamed = write_design_reference(
        project,
        display_name="更容易阅读的展示名称",
        anchor_display_name="架构展示名称",
        file_name="展示名称变化.json",
    )

    result = import_reference(project, renamed)

    assert result.returncode == 0, result.stderr
    assert "技术方案引用已经存在：DES-001" in result.stdout
    assert load_events(paths) == before
    assert len(derive_state(paths)["design_references"]) == 1


def test_confirmed_design_reference_cannot_be_overwritten_and_revision_gets_new_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    before = load_events(paths)
    conflict = write_design_reference(
        project,
        line_start=9,
        line_end=12,
        display_heading=None,
        file_name="同键改写.json",
    )

    rejected = import_reference(project, conflict)

    assert rejected.returncode == 1
    assert "不能原地覆盖" in rejected.stderr
    assert load_events(paths) == before
    revision = write_design_reference(
        project,
        client_key="project-technical-solution-r2",
        line_start=9,
        line_end=12,
        display_heading=None,
        supersedes="DES-001",
        file_name="新修订.json",
    )
    revised = import_reference(project, revision)
    assert revised.returncode == 0, revised.stderr
    records = derive_state(paths)["design_references"]
    assert [item["design_id"] for item in records] == ["DES-001", "DES-002"]
    assert records[0]["status"] == "confirmed"
    assert records[1]["supersedes"] == "DES-001"


def test_multiple_drafts_require_explicit_target_and_only_change_selected_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    assert run_cli(["draft", "create", "第二份草稿"], cwd=project).returncode == 0
    reference = write_design_reference(project)
    before = paths.events_file.read_bytes()

    missing = run_cli(["design-reference", "--file", reference.name], cwd=project)

    assert missing.returncode == 2
    assert "缺少必填参数" in missing.stderr
    assert paths.events_file.read_bytes() == before
    explicit = import_reference(project, reference)
    assert explicit.returncode == 0, explicit.stderr
    state = derive_state(paths)
    assert [item["design_id"] for item in state["drafts"]["DRAFT-001"]["design_references"]] == ["DES-001"]
    assert state["drafts"]["DRAFT-002"]["design_references"] == []


def test_invalid_fr_and_fragment_hash_do_not_allocate_des_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    invalid_fr = write_design_reference(
        project,
        applies_to=["FR-999"],
        file_name="非法FR.json",
    )
    invalid_fragment = write_design_reference(
        project,
        fragment_sha256="0" * 64,
        file_name="非法片段哈希.json",
    )
    before = load_events(paths)

    fr_result = import_reference(project, invalid_fr)
    hash_result = import_reference(project, invalid_fragment)

    assert fr_result.returncode == 1 and "不存在的 FR" in fr_result.stderr
    assert hash_result.returncode == 1 and "片段内容已经变化" in hash_result.stderr
    assert load_events(paths) == before
    assert derive_state(paths)["design_references"] == []
    valid = import_reference(project, write_design_reference(project, file_name="合法引用.json"))
    assert valid.returncode == 0, valid.stderr
    assert "DES-001" in valid.stdout


def test_design_reference_rebuilds_json_and_display_from_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    expected = derive_state(paths)["design_references"][0]
    index_path = paths.draft_design_reference_index_file("DRAFT-001")
    record_path = paths.draft_design_reference_records_dir("DRAFT-001") / "DES-001.json"
    display_path = paths.draft_design_reference_markdown_file("DRAFT-001")
    index_path.unlink()
    record_path.unlink()
    display_path.write_text("# 人工改写\n\nsummary: 不可信\n", encoding="utf-8")

    refresh_materialized_state(paths)

    assert json.loads(index_path.read_text(encoding="utf-8"))["design_references"] == [expected]
    assert json.loads(record_path.read_text(encoding="utf-8")) == expected
    display = display_path.read_text(encoding="utf-8")
    assert "人工改写" not in display
    assert "summary: 不可信" not in display
    assert "def build_service" not in display


def test_structured_des_does_not_enter_legacy_summary_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0

    with sqlite3.connect(paths.database_file) as connection:
        legacy_rows = connection.execute(
            "SELECT design_id, title, summary FROM designs"
        ).fetchall()

    assert legacy_rows == []
    assert derive_state(paths)["design_references"][0]["design_id"] == "DES-001"


def test_legacy_design_and_accept_commands_reject_free_text_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    before = paths.events_file.read_bytes()

    design = run_cli(["design", "自由文本技术方案"], cwd=project)
    accept = run_cli(["design-accept", "DES-001"], cwd=project)

    assert design.returncode == 1 and "design-reference" in design.stderr
    assert accept.returncode == 1 and "design-reference-confirm" in accept.stderr
    assert paths.events_file.read_bytes() == before
    assert not paths.draft_design_reference_index_file("DRAFT-001").exists()


def test_modular_design_status_uses_structured_plan_artifacts_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = [
        _module("data-main", "data", status="required"),
        _module(
            "api-main",
            "api",
            status="supplement_required",
            depends_on=["@client:data-main"],
        ),
        _module("component-main", "component", status="provided"),
        _module("unused-main", "deployment", status="not_applicable"),
    ]
    project, paths = _project_with_plan(tmp_path, monkeypatch, modules)

    planned = derive_state(paths)["drafts"]["DRAFT-001"]
    assert planned["status"] == "designing"
    assert planned["design_stage"]["pending_modules"] == [
        "API-001",
        "COMP-001",
        "DATA-001",
    ]
    assert planned["design_stage"]["not_applicable_modules"] == ["DEPLOY-001"]

    documents = [
        _artifact("DATA-001", "data"),
        _artifact("API-001", "api", depends_on=["DATA-001"]),
        _artifact("COMP-001", "component"),
    ]
    for index, document in enumerate(documents, start=1):
        result = _import_artifact(
            project,
            _write_artifact(project, document, f"状态模块-{index}.json"),
        )
        assert result.returncode == 0, result.stderr

    complete_modules = derive_state(paths)["drafts"]["DRAFT-001"]
    assert complete_modules["status"] == "designing"
    assert complete_modules["design_stage"]["summary_status"] == "missing"
    projection = (paths.draft_dir("DRAFT-001") / "design.draft.md").read_text(
        encoding="utf-8"
    )
    assert "DATA-001" in projection
    assert "API-001" in projection
    assert "COMP-001" in projection
    assert "DEPLOY-001" not in projection
    assert not list(
        paths.draft_design_dir("DRAFT-001").rglob(
            "DEPLOY-001.design-artifact.v1.*"
        )
    )

    summary = _import_summary(
        project,
        _write_summary(project, _minimal_summary(), "最小总体设计.json"),
    )
    assert summary.returncode == 0, summary.stderr
    assert "DRAFT 状态：design_reviewing" in summary.stdout
    ready = derive_state(paths)["drafts"]["DRAFT-001"]
    assert ready["status"] == "design_reviewing"
    assert ready["design_stage"]["ready_for_review"] is True
    assert ready["design_stage"]["blockers"] == []


def test_single_enabled_module_does_not_create_empty_summary_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("data-main", "data")],
    )
    imported = _import_artifact(
        project,
        _write_artifact(project, _artifact("DATA-001", "data"), "单模块.json"),
    )

    assert imported.returncode == 0, imported.stderr
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "design_reviewing"
    assert draft["design_stage"]["summary_status"] == "not_required"
    assert not (
        paths.draft_design_dir("DRAFT-001") / "design-summary.v1.json"
    ).exists()
    assert "总体设计说明" not in (
        paths.draft_dir("DRAFT-001") / "design.draft.md"
    ).read_text(encoding="utf-8")


def test_blocked_module_keeps_designing_and_failed_summary_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [
            _module("data-main", "data"),
            _module("field-main", "field", status="blocked"),
        ],
    )
    accepted = _import_artifact(
        project,
        _write_artifact(project, _artifact("DATA-001", "data"), "数据模块.json"),
    )
    assert accepted.returncode == 0, accepted.stderr
    before_events = paths.events_file.read_bytes()
    before_projection = (
        paths.draft_dir("DRAFT-001") / "design.draft.md"
    ).read_bytes()

    rejected = _import_summary(
        project,
        _write_summary(project, _minimal_summary(), "阻塞总体设计.json"),
    )

    assert rejected.returncode == 1
    assert "阻塞" in rejected.stderr
    assert paths.events_file.read_bytes() == before_events
    assert (
        paths.draft_dir("DRAFT-001") / "design.draft.md"
    ).read_bytes() == before_projection
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "designing"
    assert draft["design_stage"]["blocked_modules"] == ["FIELD-001"]
    assert any(
        item["code"] == "design_module_blocked"
        and item["source_id"] == "FIELD-001"
        for item in draft["design_stage"]["blockers"]
    )
    assert not list(
        paths.draft_design_dir("DRAFT-001").rglob(
            "FIELD-001.design-artifact.v1.*"
        )
    )


def test_summary_hash_stale_returns_to_designing_without_reading_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [
            _module("data-main", "data"),
            _module(
                "api-main",
                "api",
                depends_on=["@client:data-main"],
            ),
        ],
    )
    for name, document in (
        ("数据模块.json", _artifact("DATA-001", "data")),
        (
            "接口模块.json",
            _artifact("API-001", "api", depends_on=["DATA-001"]),
        ),
    ):
        assert _import_artifact(
            project,
            _write_artifact(project, document, name),
        ).returncode == 0
    assert _import_summary(
        project,
        _write_summary(project, _minimal_summary()),
    ).returncode == 0
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == (
        "design_reviewing"
    )

    revised = _artifact("DATA-001", "data")
    revised["content"]["lifecycle"]["retention"] = "注销后三十天"
    assert _import_artifact(
        project,
        _write_artifact(project, revised, "数据模块修订.json"),
    ).returncode == 0
    display = paths.draft_dir("DRAFT-001") / "design.draft.md"
    display.write_text(
        "# 人工标题\n\n## 总体设计说明\n\n已全部通过。\n",
        encoding="utf-8",
    )

    stale = derive_state(paths)["drafts"]["DRAFT-001"]

    assert stale["status"] == "designing"
    assert stale["design_stage"]["summary_status"] == "stale"
    assert any(
        item["code"] == "design_summary_stale"
        for item in stale["design_stage"]["blockers"]
    )


def test_legacy_design_archive_keeps_original_body_without_fixed_sections(
    tmp_path: Path,
) -> None:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project).returncode == 0
    paths = build_paths(project)
    legacy_body = "# 技术记录\n\n- 保留已经归档的实现结论。"
    append_event(
        paths,
        event_type="draft_created",
        source="legacy-design-test",
        summary="读取已有技术记录",
        payload={
            "draft_id": "DRAFT-001",
            "title": "已有技术记录",
            "status": "design_ready",
            "requirement_body": "# 需求记录\n\n- 保留需求原文。",
            "design_body": legacy_body,
        },
    )

    state = refresh_materialized_state(paths)

    assert state["drafts"]["DRAFT-001"]["assessment"][
        "missing_design_items"
    ] == []
    assert (
        paths.draft_dir("DRAFT-001") / "design.draft.md"
    ).read_text(encoding="utf-8") == legacy_body + "\n"


def test_structured_design_commands_handoff_to_direct_task_mainline() -> None:
    import argparse

    from codex_sdlc.cli import build_parser

    parser = build_parser()
    command_actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]

    assert len(command_actions) == 1
    commands = command_actions[0].choices
    structured_design_commands = {
        "design-reference",
        "design-reference-confirm",
        "design-plan",
        "design-artifact",
        "design-summary",
    }

    assert structured_design_commands <= commands.keys()
    assert all(callable(commands[name].get_default("func")) for name in structured_design_commands)
    # 设计阶段只登记结构化产物，任务执行由全局 task 入口单独接手，
    # 因而不能再把 prepare、brief 或旧 task-pack 当作中间门禁。
    assert callable(commands["task"].get_default("func"))
    assert {"prepare", "brief", "task-pack"}.isdisjoint(commands)


def test_design_skill_requires_selection_completeness_check() -> None:
    skill_text = (SDLC_SKILLS_HOME / "sdlc-design/SKILL.md").read_text(encoding="utf-8")

    # 当前正式设计入口按真实模块组合，不再用固定的前后端技术选型清单制造空壳内容。
    assert "技术方案原文保留在 MAT 中，DES 只保存稳定" in skill_text
    assert "`design-reference.v1`" in skill_text
    assert "`design-plan.v1`" in skill_text
    assert "`design-artifact.v1`" in skill_text
    assert "`design-summary.v1`" in skill_text
    assert "`data`、`api`、`page`、`component`、`security`、`deployment`" in skill_text
    assert "不创建空壳模块" in skill_text
    assert "`open_questions`" in skill_text
    assert "--stage integrated_design" in skill_text
