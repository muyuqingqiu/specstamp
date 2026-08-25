from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# 复用 T-004、T-005 和 T-008 的真实夹具，避免为 DES 另造一套较弱的 DRAFT 状态。
TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock
from codex_sdlc.core.state import (
    append_event,
    derive_state,
    load_events,
    refresh_materialized_state,
)
from codex_sdlc.core.structured_contract import canonical_sha256
from codex_sdlc.services import design_service
from codex_sdlc.services.design_service import (
    DesignReferenceService,
    design_reference_identity_sha256,
)
from codex_sdlc.services.draft_service import DraftMutationService
from test_cli_v1 import SDLC_BIN, run_cli
from test_cli_v17_draft_contract import (
    create_draft_with_material,
    import_command,
    requirement_documents,
    write_documents,
)
from test_requirement_review_flow import _create, _submit


def technical_solution_text(*, duplicate_heading: bool = False, long: bool = True) -> str:
    repeated = "\n## 架构\n\n重复标题下的备用说明。\n" if duplicate_heading else ""
    long_section = "".join(f"- 约束 {index:04d}：保持原始资料字节不变。\n" for index in range(800)) if long else ""
    return (
        "# 项目技术方案\n\n"
        "## 架构\n\n"
        "| 层级 | 实现 |\n"
        "|---|---|\n"
        "| 服务 | Python |\n\n"
        "```python\n"
        "def build_service():\n"
        "    return \"stable\"\n"
        "```\n"
        f"{repeated}\n"
        "## 约束\n\n"
        f"{long_section}"
    )


def _line_fragment(text: str, line_start: int, line_end: int) -> bytes:
    lines = text.splitlines(keepends=True)
    return "".join(lines[line_start - 1 : line_end]).encode("utf-8")


def create_confirmed_design_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    duplicate_heading: bool = False,
    long: bool = True,
) -> tuple[Path, object, bytes, dict[str, object]]:
    project, paths, requirement_material = create_draft_with_material(tmp_path)
    text = technical_solution_text(duplicate_heading=duplicate_heading, long=long)
    source = project / "项目技术方案.md"
    source_bytes = text.encode("utf-8")
    source.write_bytes(source_bytes)
    archived = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "technical-solution",
            "--title",
            "项目技术方案",
            "--file",
            source.name,
        ],
        cwd=project,
    )
    assert archived.returncode == 0, archived.stderr

    split, coverage = requirement_documents(project, requirement_material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    review = _create(paths, monkeypatch)
    _submit(paths, review["request"], monkeypatch)
    confirmed = DraftMutationService(paths, source="T-009 合同测试").confirm_requirement(
        "DRAFT-001",
        review_id="REV-001",
        confirmed_at="2026-07-17T01:00:00Z",
    )
    assert confirmed["status"] == "requirement_confirmed"
    technical_material = next(
        item
        for item in derive_state(paths)["drafts"]["DRAFT-001"]["materials"]
        if item["type"] == "technical-solution"
    )
    return project, paths, source_bytes, technical_material


def write_design_reference(
    project: Path,
    *,
    source_text: str | None = None,
    client_key: str = "project-technical-solution",
    display_name: str = "项目技术方案",
    anchor_key: str = "architecture",
    anchor_display_name: str = "架构",
    line_start: int = 3,
    line_end: int = 7,
    display_heading: str | None = "架构",
    fragment_sha256: str | None = None,
    material_id: str = "MAT-002",
    applies_to: list[str] | None = None,
    supersedes: str = "",
    file_name: str = "design-reference.json",
) -> Path:
    text = source_text
    if text is None:
        text = (project / "项目技术方案.md").read_text(encoding="utf-8")
    fragment_hash = fragment_sha256 or hashlib.sha256(
        _line_fragment(text, line_start, line_end)
    ).hexdigest()
    locator: dict[str, object] = {
        "kind": "text_range",
        "line_start": line_start,
        "line_end": line_end,
        "fragment_sha256": fragment_hash,
    }
    if display_heading:
        locator["display_heading"] = display_heading
    document: dict[str, object] = {
        "schema_version": "design-reference.v1",
        "draft_id": "DRAFT-001",
        "client_key": client_key,
        "display_name": display_name,
        "material_id": material_id,
        "anchors": [
            {
                "client_key": anchor_key,
                "display_name": anchor_display_name,
                "locator": locator,
            }
        ],
        "applies_to": applies_to or ["FR-001"],
    }
    if supersedes:
        document["supersedes"] = supersedes
    path = project / file_name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def import_reference(project: Path, reference_file: Path):
    return run_cli(
        ["design-reference", "DRAFT-001", "--file", reference_file.name],
        cwd=project,
    )


def test_original_file_with_table_code_and_long_sections_stays_byte_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, original_bytes, material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )
    reference_file = write_design_reference(project)

    imported = import_reference(project, reference_file)
    confirmed = run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    )

    assert imported.returncode == 0, imported.stderr
    assert confirmed.returncode == 0, confirmed.stderr
    archived_path = paths.draft_dir("DRAFT-001") / str(material["stored_path"])
    assert archived_path.read_bytes() == original_bytes
    assert (project / "项目技术方案.md").read_bytes() == original_bytes

    state = derive_state(paths)
    record = state["design_references"][0]
    assert state["drafts"]["DRAFT-001"]["status"] == "requirement_confirmed"
    assert record["design_id"] == "DES-001"
    assert record["client_key"] == "project-technical-solution"
    assert record["anchors"][0]["key"] == "DES-001#architecture"
    assert record["anchors"][0]["fragment_sha256"] == record["anchors"][0]["locator"]["fragment_sha256"]
    assert record["path"] == material["stored_path"]
    assert record["sha256"] == material["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert record["applies_to"] == ["FR-001"]
    assert record["status"] == "confirmed"
    assert "title" not in record and "summary" not in record

    index = json.loads(paths.draft_design_reference_index_file("DRAFT-001").read_text(encoding="utf-8"))
    assert index["design_references"] == [record]
    display = paths.draft_design_reference_markdown_file("DRAFT-001").read_text(encoding="utf-8")
    assert "项目技术方案" in display
    assert "def build_service" not in display
    assert "约束 0799" not in display


def test_import_is_idempotent_and_display_names_do_not_change_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    first_file = write_design_reference(project)
    first = import_reference(project, first_file)
    before_events = load_events(paths)

    second_file = write_design_reference(
        project,
        display_name="只改变展示名称",
        anchor_display_name="只改变锚点展示名称",
        file_name="design-reference-display.json",
    )
    second = import_reference(project, second_file)

    assert first.returncode == 0 and second.returncode == 0
    assert "技术方案引用已经存在：DES-001" in second.stdout
    assert load_events(paths) == before_events
    assert [item["design_id"] for item in derive_state(paths)["design_references"]] == ["DES-001"]


def test_same_client_key_different_content_rejects_and_revision_uses_new_des(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    before_events = load_events(paths)

    conflict = write_design_reference(
        project,
        line_start=9,
        line_end=12,
        display_heading=None,
        file_name="同键不同内容.json",
    )
    rejected = import_reference(project, conflict)
    assert rejected.returncode == 1
    assert "不能原地覆盖" in rejected.stderr
    assert load_events(paths) == before_events

    revision = write_design_reference(
        project,
        client_key="project-technical-solution-r2",
        line_start=9,
        line_end=12,
        display_heading=None,
        supersedes="DES-001",
        file_name="技术方案修订.json",
    )
    revised = import_reference(project, revision)
    assert revised.returncode == 0, revised.stderr
    references = derive_state(paths)["design_references"]
    assert [item["design_id"] for item in references] == ["DES-001", "DES-002"]
    assert references[0]["status"] == "confirmed"
    assert references[1]["supersedes"] == "DES-001"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"material_id": "MAT-999"}, "资料不存在"),
        ({"applies_to": ["FR-999"]}, "不存在的 FR"),
        ({"line_start": 999, "line_end": 1000, "display_heading": None}, "未命中"),
        ({"fragment_sha256": "0" * 64}, "片段内容已经变化"),
    ],
)
def test_invalid_material_fr_anchor_and_fragment_leave_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
    message: str,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    before_events = paths.events_file.read_bytes()
    before_managed = draft_artifact_bytes(paths.draft_dir("DRAFT-001"))
    reference = write_design_reference(project, **change)

    result = import_reference(project, reference)

    assert result.returncode == 1
    assert message in result.stderr
    assert paths.events_file.read_bytes() == before_events
    assert draft_artifact_bytes(paths.draft_dir("DRAFT-001")) == before_managed
    assert derive_state(paths)["design_references"] == []
    assert not paths.draft_design_reference_index_file("DRAFT-001").exists()


def test_unconfirmed_requirement_rejects_before_des_write(
    tmp_path: Path,
) -> None:
    project, paths, _material = create_draft_with_material(tmp_path)
    source = project / "项目技术方案.md"
    source.write_text(technical_solution_text(long=False), encoding="utf-8")
    assert run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "technical-solution",
            "--title",
            "项目技术方案",
            "--file",
            source.name,
        ],
        cwd=project,
    ).returncode == 0
    reference = write_design_reference(project)
    before_events = paths.events_file.read_bytes()

    result = import_reference(project, reference)

    assert result.returncode == 1
    assert "需求尚未确认" in result.stderr
    assert paths.events_file.read_bytes() == before_events
    assert derive_state(paths)["design_references"] == []


def test_submission_rejects_title_summary_path_and_self_reported_file_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    valid_path = write_design_reference(project)
    valid = json.loads(valid_path.read_text(encoding="utf-8"))
    before_events = paths.events_file.read_bytes()

    for field, value in (
        ("title", "不能作为技术方案正文替代"),
        ("summary", "不能作为技术方案正文替代"),
        ("path", "调用方自报路径.md"),
        ("sha256", "0" * 64),
    ):
        candidate = deepcopy(valid)
        candidate[field] = value
        path = project / f"非法自报-{field}.json"
        path.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = import_reference(project, path)
        assert result.returncode != 0
        assert "Schema 校验失败" in result.stderr
        assert paths.events_file.read_bytes() == before_events

    assert derive_state(paths)["design_references"] == []


def test_duplicate_heading_is_rejected_when_heading_is_used_for_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
        duplicate_heading=True,
        long=False,
    )
    reference = write_design_reference(project)
    before_events = load_events(paths)

    result = import_reference(project, reference)

    assert result.returncode == 1
    assert "标题重复" in result.stderr
    assert load_events(paths) == before_events


def test_duplicate_heading_can_be_located_by_exact_range_without_title_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
        duplicate_heading=True,
        long=False,
    )
    reference = write_design_reference(project, display_heading=None)

    result = import_reference(project, reference)

    assert result.returncode == 0, result.stderr
    assert derive_state(paths)["design_references"][0]["anchors"][0]["key"] == "DES-001#architecture"


def test_file_drift_rejects_confirmation_without_confirmation_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, material = create_confirmed_design_project(tmp_path, monkeypatch)
    assert import_reference(project, write_design_reference(project)).returncode == 0
    before = load_events(paths)
    archived = paths.draft_dir("DRAFT-001") / str(material["stored_path"])
    archived.write_bytes(archived.read_bytes() + "\n漂移\n".encode("utf-8"))

    result = run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    )

    assert result.returncode == 1
    assert "尚未确认" in result.stderr or "哈希" in result.stderr
    assert load_events(paths) == before
    assert not any(event["event_type"] == "draft_design_reference_confirmed" for event in load_events(paths))


def draft_artifact_bytes(draft_dir: Path) -> dict[str, bytes]:
    return {
        path.relative_to(draft_dir).as_posix(): path.read_bytes()
        for path in sorted(draft_dir.rglob("*"))
        if path.is_file() and "原始资料" not in path.relative_to(draft_dir).parts
    }


def test_projection_failure_rolls_back_event_number_and_all_managed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    reference = write_design_reference(project)
    original_events = paths.events_file.read_bytes()
    original_files = draft_artifact_bytes(paths.draft_dir("DRAFT-001"))
    real_refresh = design_service.refresh_materialized_state
    calls = 0

    def refresh_then_fail_once(current_paths):
        nonlocal calls
        calls += 1
        state = real_refresh(current_paths)
        if calls == 1:
            raise OSError("模拟 DES 投影提交失败")
        return state

    monkeypatch.setattr(design_service, "refresh_materialized_state", refresh_then_fail_once)
    with pytest.raises(OSError, match="模拟 DES 投影提交失败"):
        DesignReferenceService(paths).import_file("DRAFT-001", reference.name)

    assert paths.events_file.read_bytes() == original_events
    assert draft_artifact_bytes(paths.draft_dir("DRAFT-001")) == original_files
    assert derive_state(paths)["design_references"] == []
    assert not list(paths.draft_staging_dir("DRAFT-001").glob("*.tmp"))

    monkeypatch.setattr(design_service, "refresh_materialized_state", real_refresh)
    retried = DesignReferenceService(paths).import_file("DRAFT-001", reference.name)
    assert retried["record"]["design_id"] == "DES-001"


def test_events_rebuild_json_records_and_display_without_reading_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    design_dir = paths.draft_design_dir("DRAFT-001")
    expected = derive_state(paths)["design_references"]
    paths.draft_design_reference_index_file("DRAFT-001").unlink()
    paths.draft_design_reference_markdown_file("DRAFT-001").write_text(
        "# 被改坏的展示文字\n\nstatus: draft\nsummary: 不可信\n",
        encoding="utf-8",
    )
    for path in paths.draft_design_reference_records_dir("DRAFT-001").glob("*.json"):
        path.unlink()

    rebuilt = refresh_materialized_state(paths)

    assert rebuilt["design_references"] == expected
    index = json.loads(paths.draft_design_reference_index_file("DRAFT-001").read_text(encoding="utf-8"))
    assert index["design_references"] == expected
    assert json.loads(
        (paths.draft_design_reference_records_dir("DRAFT-001") / "DES-001.json").read_text(encoding="utf-8")
    ) == expected[0]
    display = paths.draft_design_reference_markdown_file("DRAFT-001").read_text(encoding="utf-8")
    assert "被改坏" not in display and "summary: 不可信" not in display


def test_same_design_number_with_different_event_content_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    assert import_reference(project, write_design_reference(project)).returncode == 0
    record = deepcopy(derive_state(paths)["design_references"][0])
    record["client_key"] = "conflicting-reference"
    record["anchors"][0]["key"] = "DES-001#conflicting-anchor"
    record["identity_sha256"] = design_reference_identity_sha256(record)
    record.pop("record_sha256")
    record["record_sha256"] = canonical_sha256(record)

    with project_lock(paths):
        append_event(
            paths,
            event_type="draft_design_reference_imported",
            source="冲突事件合同测试",
            summary="写入冲突 DES 编号",
            payload={
                "draft_id": "DRAFT-001",
                "reference": record,
                "artifact_updates": [],
            },
        )

    with pytest.raises(SdlcError, match="编号重复且内容冲突"):
        derive_state(paths)


def test_two_concurrent_imports_allocate_distinct_des_numbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    first = write_design_reference(project, client_key="concurrent-a", file_name="并发A.json")
    second = write_design_reference(
        project,
        client_key="concurrent-b",
        anchor_key="architecture-b",
        file_name="并发B.json",
    )
    environment = os.environ.copy()
    environment["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    processes = [
        subprocess.Popen(
            [str(SDLC_BIN), "design-reference", "DRAFT-001", "--file", path.name],
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for path in (first, second)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], outputs
    references = derive_state(paths)["design_references"]
    assert [item["design_id"] for item in references] == ["DES-001", "DES-002"]
    assert {item["client_key"] for item in references} == {"concurrent-a", "concurrent-b"}
    assert len(
        [
            event
            for event in load_events(paths)
            if event["event_type"] == "draft_design_reference_imported"
        ]
    ) == 2


def test_multiple_drafts_never_allow_implicit_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    created = run_cli(["draft", "create", "第二份草稿"], cwd=project)
    assert created.returncode == 0, created.stderr
    reference = write_design_reference(project)
    before_events = paths.events_file.read_bytes()

    missing_target = run_cli(
        ["design-reference", "--file", reference.name],
        cwd=project,
    )
    explicit = import_reference(project, reference)

    assert missing_target.returncode == 2
    assert "缺少必填参数" in missing_target.stderr
    assert explicit.returncode == 0, explicit.stderr
    state = derive_state(paths)
    assert len(state["drafts"]["DRAFT-001"]["design_references"]) == 1
    assert state["drafts"]["DRAFT-002"]["design_references"] == []
    assert paths.events_file.read_bytes() != before_events


def test_legacy_free_text_design_commands_are_read_only_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _bytes, _material = create_confirmed_design_project(tmp_path, monkeypatch)
    before_events = paths.events_file.read_bytes()
    before_files = draft_artifact_bytes(paths.draft_dir("DRAFT-001"))

    design = run_cli(["design", "自由文本标题和摘要"], cwd=project)
    accept = run_cli(["design-accept", "DES-001"], cwd=project)

    assert design.returncode == 1 and "design-reference" in design.stderr
    assert accept.returncode == 1 and "design-reference-confirm" in accept.stderr
    assert paths.events_file.read_bytes() == before_events
    assert draft_artifact_bytes(paths.draft_dir("DRAFT-001")) == before_files
