from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

# 合同测试复用现有真实 CLI 临时项目工厂，避免另写一套初始化口径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_cli_v1 import init_demo_repo, run_cli
from codex_sdlc.core import draft_artifacts
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock
from codex_sdlc.core.state import append_event, derive_state, load_events, refresh_materialized_state
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_file
from codex_sdlc.services.draft_service import DraftMutationService


def _initialized_project(tmp_path: Path) -> tuple[Path, object]:
    project_dir = init_demo_repo(tmp_path)
    result = run_cli(["init-basic"], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    return project_dir, build_paths(project_dir)


def _create_draft(project_dir: Path, *, run_id: str = "producer-thread-003") -> object:
    result = run_cli(
        ["draft", "create", "订单导出工作包"],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": run_id},
    )
    assert result.returncode == 0, result.stderr
    return build_paths(project_dir)


def _managed_file_snapshot(draft_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(draft_dir).as_posix(): path.read_bytes()
        for path in draft_dir.rglob("*")
        if path.is_file() and path.relative_to(draft_dir).parts[0] != "原始资料"
    }


def test_new_draft_creates_fixed_layout_and_file_level_registry(tmp_path: Path) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    draft_dir = paths.draft_dir("DRAFT-001")

    assert {path.name for path in draft_dir.iterdir() if path.is_dir()} >= {"原始资料", "需求", "设计", "质检"}
    assert paths.draft_staging_dir("DRAFT-001").is_dir()
    assert paths.draft_status_file("DRAFT-001").is_file()
    assert paths.draft_artifact_index_file("DRAFT-001").is_file()

    index = json.loads(paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8"))
    assert index["schema_version"] == "artifact-index.v1"
    assert index["draft_id"] == "DRAFT-001"
    assert {item["source_path"] for item in index["artifacts"]} == {
        "需求/需求草稿.md",
        "设计/技术草稿.md",
        "质检/审查记录.md",
        "待确认问题.md",
        "用户决定.md",
    }
    for item in index["artifacts"]:
        assert item["producer_task_id"] == "sdlc-draft"
        assert item["producer_run_id"] == "producer-thread-003"
        assert item["input_hashes"]
        assert all(len(value) == 64 for value in item["input_hashes"].values())
        assert item["sha256"] == sha256_file(draft_dir / item["source_path"])

    business_event = load_events(paths)[-1]
    assert business_event["event_type"] == "draft_mutated"
    updates = business_event["payload"]["artifact_updates"]
    assert {item["source_path"] for item in updates} == {item["source_path"] for item in index["artifacts"]}
    assert all(item["artifact_sha256"] for item in updates)


def test_refresh_rebuilds_all_markdown_without_touching_original_materials(tmp_path: Path) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    requirement = project_dir / "需求内容.md"
    requirement.write_text("# 需求草稿\n\n运营可以导出当前筛选结果。\n", encoding="utf-8")
    design = project_dir / "技术内容.md"
    design.write_text("# 技术草稿\n\n后端按当前筛选条件生成文件。\n", encoding="utf-8")
    for command in (
        ["draft", "requirement", "DRAFT-001", "--file", str(requirement)],
        ["draft", "design", "DRAFT-001", "--file", str(design)],
        ["draft", "question", "DRAFT-001", "导出文件是否需要保留七天？"],
        ["draft", "decision", "DRAFT-001", "导出文件保留七天。"],
        ["draft", "review", "DRAFT-001", "需求和技术内容一致。"],
    ):
        result = run_cli(command, cwd=project_dir)
        assert result.returncode == 0, result.stderr

    draft_dir = paths.draft_dir("DRAFT-001")
    original_file = paths.draft_original_materials_dir("DRAFT-001") / "MAT-001_原始说明.md"
    original_file.write_bytes(b"original-material\x00\xff")
    original_hash = sha256_file(original_file)
    original_stat = original_file.stat()

    # 阅读文件可以被手工改坏，但状态仍只来自事件；刷新后会恢复登记版本。
    canonical_requirement = draft_dir / "需求/需求草稿.md"
    registered_requirement = canonical_requirement.read_bytes()
    state_before_manual_edit = derive_state(paths)["drafts"]["DRAFT-001"]
    canonical_requirement.write_text("# 手工改写\n\n这段文字不能改变业务状态。\n", encoding="utf-8")
    state_after_manual_edit = derive_state(paths)["drafts"]["DRAFT-001"]
    assert state_after_manual_edit["requirement_body"] == state_before_manual_edit["requirement_body"]
    refresh_materialized_state(paths)
    assert canonical_requirement.read_bytes() == registered_requirement

    expected_markdown = {
        path.relative_to(draft_dir).as_posix(): path.read_bytes()
        for path in draft_dir.rglob("*.md")
        if path != original_file
    }
    assert expected_markdown
    for relative_path in expected_markdown:
        (draft_dir / relative_path).unlink()

    result = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    assert "已重建 DRAFT 阅读文件：DRAFT-001" in result.stdout
    for relative_path, expected in expected_markdown.items():
        assert (draft_dir / relative_path).read_bytes() == expected
    assert sha256_file(original_file) == original_hash
    assert original_file.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert original_file.stat().st_ino == original_stat.st_ino


def test_generic_json_artifact_uses_structured_event_and_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    monkeypatch.setenv("CODEX_THREAD_ID", "structured-producer-run")
    before_count = len(load_events(paths))
    input_file = paths.draft_original_materials_dir("DRAFT-001") / "MAT-001_原始说明.md"
    input_file.write_text("结构化样例的直接输入。\n", encoding="utf-8")

    event = draft_artifacts.register_json_artifact(
        paths,
        draft_id="DRAFT-001",
        source_path="需求/结构化样例.json",
        artifact_type="structured_example",
        document={"items": [{"id": "ITEM-001", "value": 1}]},
        input_hashes={"原始资料/MAT-001_原始说明.md": sha256_file(input_file)},
        producer_task_id="T-003-probe",
        source="test-draft-artifact",
    )

    assert event["event_type"] == "draft_artifact_registered"
    assert len(load_events(paths)) == before_count + 1
    artifact_file = paths.draft_dir("DRAFT-001") / "需求/结构化样例.json"
    original = artifact_file.read_bytes()
    registered = event["payload"]["artifact"]
    assert registered["producer_task_id"] == "T-003-probe"
    assert registered["producer_run_id"] == "structured-producer-run"
    assert registered["artifact_sha256"] == sha256_file(artifact_file)
    assert "markdown" not in registered

    artifact_file.unlink()
    refresh_materialized_state(paths)
    assert artifact_file.read_bytes() == original
    index = json.loads(paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8"))
    item = next(item for item in index["artifacts"] if item["source_path"] == "需求/结构化样例.json")
    assert item["input_hashes"] == registered["input_hashes"]
    assert item["sha256"] == registered["artifact_sha256"]


def test_started_draft_rejects_normal_and_artifact_writes_without_event(tmp_path: Path) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    append_event(
        paths,
        event_type="draft_started",
        source="test",
        summary="DRAFT-001 已正式建档",
        payload={"draft_id": "DRAFT-001", "started_requirement_id": "REQ-001"},
    )
    refresh_materialized_state(paths)
    before_events = paths.events_file.read_bytes()
    before_files = _managed_file_snapshot(paths.draft_dir("DRAFT-001"))

    normal_result = run_cli(["draft", "question", "DRAFT-001", "还能继续修改吗？"], cwd=project_dir)
    assert normal_result.returncode == 1
    assert "已经正式建档" in normal_result.stderr
    with pytest.raises(SdlcError, match="已经正式建档"):
        draft_artifacts.register_json_artifact(
            paths,
            draft_id="DRAFT-001",
            source_path="需求/拒绝写入.json",
            artifact_type="rejected_example",
            document={"value": 1},
            input_hashes={"input.json": canonical_sha256({"value": 1})},
            producer_task_id="T-003-probe",
            source="test-draft-artifact",
        )

    assert paths.events_file.read_bytes() == before_events
    assert _managed_file_snapshot(paths.draft_dir("DRAFT-001")) == before_files
    assert not (paths.draft_dir("DRAFT-001") / "需求/拒绝写入.json").exists()


def test_layout_failure_happens_before_business_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, paths = _initialized_project(tmp_path)
    before_events = paths.events_file.read_bytes()

    def fail_layout(_paths, _draft_id):
        raise OSError("模拟目录创建失败")

    monkeypatch.setattr(draft_artifacts, "ensure_draft_layout", fail_layout)
    with project_lock(paths), pytest.raises(OSError, match="模拟目录创建失败"):
        DraftMutationService(paths, source="test").create("失败工作包")

    assert paths.events_file.read_bytes() == before_events
    assert "DRAFT-001" not in derive_state(paths)["drafts"]
    assert not paths.draft_dir("DRAFT-001").exists()


def test_projection_failure_rolls_back_event_and_new_draft_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, paths = _initialized_project(tmp_path)
    before_events = paths.events_file.read_bytes()

    def fail_projection(_draft_dir, _documents):
        raise OSError("模拟投影写入失败")

    monkeypatch.setattr(draft_artifacts, "write_projection_bundle", fail_projection)
    with project_lock(paths), pytest.raises(OSError, match="模拟投影写入失败"):
        DraftMutationService(paths, source="test").create("失败工作包")

    assert paths.events_file.read_bytes() == before_events
    assert "DRAFT-001" not in derive_state(paths)["drafts"]
    assert not paths.draft_dir("DRAFT-001").exists()


def test_partial_projection_replace_restores_previous_files_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    draft_dir = paths.draft_dir("DRAFT-001")
    original_file = paths.draft_original_materials_dir("DRAFT-001") / "MAT-001.bin"
    original_file.write_bytes(b"keep-original")
    before_events = paths.events_file.read_bytes()
    before_files = _managed_file_snapshot(draft_dir)
    real_replace = draft_artifacts._replace_projection
    calls = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("模拟第二个投影替换失败")
        real_replace(source, target)

    monkeypatch.setattr(draft_artifacts, "_replace_projection", fail_second_replace)
    with project_lock(paths), pytest.raises(OSError, match="模拟第二个投影替换失败"):
        DraftMutationService(paths, source="test").update_requirement("DRAFT-001", "# 需求草稿\n\n不能留下半份文件。")

    assert paths.events_file.read_bytes() == before_events
    assert _managed_file_snapshot(draft_dir) == before_files
    assert original_file.read_bytes() == b"keep-original"
    assert not list(draft_dir.rglob("*.tmp"))
    assert not list(paths.draft_staging_dir("DRAFT-001").iterdir())


def test_legacy_draft_read_only_adds_layout_and_keeps_old_files(tmp_path: Path) -> None:
    project_dir, paths = _initialized_project(tmp_path)
    append_event(
        paths,
        event_type="draft_created",
        source="legacy-test",
        summary="创建 DRAFT-001",
        payload={
            "draft_id": "DRAFT-001",
            "title": "已有确认稿",
            "requirement_body": "# 已有需求正文\n\n保留原来的语义。",
        },
    )
    draft_dir = paths.draft_dir("DRAFT-001")
    draft_dir.mkdir(parents=True)
    requirement_file = draft_dir / "requirement.draft.md"
    facts_file = draft_dir / "requirement.facts.json"
    requirement_file.write_bytes(b"# existing requirement\n\nkeep bytes\n")
    facts_file.write_bytes(b'{"schema":"legacy-facts"}\n')
    before_events = paths.events_file.read_bytes()
    before_requirement = requirement_file.read_bytes()
    before_facts = facts_file.read_bytes()

    result = run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    assert paths.events_file.read_bytes() == before_events
    assert requirement_file.read_bytes() == before_requirement
    assert facts_file.read_bytes() == before_facts
    assert {path.name for path in draft_dir.iterdir() if path.is_dir()} == {"原始资料", "需求", "设计", "质检", ".staging"}
    assert derive_state(paths)["drafts"]["DRAFT-001"]["artifact_records"] == []
    assert not paths.draft_artifact_index_file("DRAFT-001").exists()


def test_artifact_registration_rejects_original_materials_and_bad_input_hash(tmp_path: Path) -> None:
    project_dir, _ = _initialized_project(tmp_path)
    paths = _create_draft(project_dir)
    before_events = paths.events_file.read_bytes()

    with pytest.raises(SdlcError, match="原始资料不能登记"):
        draft_artifacts.register_json_artifact(
            paths,
            draft_id="DRAFT-001",
            source_path="原始资料/不能覆盖.json",
            artifact_type="bad",
            document={"value": 1},
            input_hashes={"input.json": canonical_sha256({"value": 1})},
            producer_task_id="T-003-probe",
            source="test",
        )
    with pytest.raises(SdlcError, match="直接输入哈希无效"):
        draft_artifacts.register_json_artifact(
            paths,
            draft_id="DRAFT-001",
            source_path="需求/错误哈希.json",
            artifact_type="bad",
            document={"value": 1},
            input_hashes={"input.json": "bad-hash"},
            producer_task_id="T-003-probe",
            source="test",
        )
    assert paths.events_file.read_bytes() == before_events
    assert not (paths.draft_dir("DRAFT-001") / "原始资料/不能覆盖.json").exists()
    assert not (paths.draft_dir("DRAFT-001") / "需求/错误哈希.json").exists()
