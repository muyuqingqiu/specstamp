from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import draft_artifacts
from codex_sdlc.core.design_artifact_contract import design_artifact_records
from codex_sdlc.core.design_summary_contract import (
    design_summary_history,
    design_summary_records,
    normalize_design_summary_submission,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.state import derive_state, load_events, refresh_materialized_state
from codex_sdlc.core.structured_contract import sha256_bytes
from codex_sdlc.services.design_service import DesignSummaryService
from test_cli_v1 import run_cli
from test_design_artifact_contract import (
    _artifact,
    _import_artifact,
    _project_with_plan,
    _write_artifact,
)
from test_design_plan_contract import _module


def _prepared_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    """总体说明必须消费完整模块集合，因此夹具先走真实计划和模块导入入口。"""

    modules = [
        _module("data-main", "data"),
        _module("api-main", "api", depends_on=["@client:data-main"]),
        _module("page-main", "page", depends_on=["@client:api-main"]),
        _module("component-main", "component"),
        _module("security-main", "security"),
    ]
    project, paths = _project_with_plan(tmp_path, monkeypatch, modules)
    documents = [
        _artifact("DATA-001", "data"),
        _artifact("API-001", "api", depends_on=["DATA-001"]),
        _artifact("PAGE-001", "page", depends_on=["API-001"]),
        _artifact("COMP-001", "component"),
        _artifact("SAFE-001", "security"),
    ]
    for index, document in enumerate(documents, start=1):
        result = _import_artifact(
            project,
            _write_artifact(project, document, f"模块-{index}.json"),
        )
        assert result.returncode == 0, result.stderr
    return project, paths


def _object(
    business_id: str,
    object_type: str,
    source_refs: list[str],
    applies_to_modules: list[str],
    *,
    canonical_name: str,
    contract: str,
) -> dict[str, object]:
    return {
        "business_id": business_id,
        "object_type": object_type,
        "source_refs": source_refs,
        "applies_to_modules": applies_to_modules,
        "definition": {
            "canonical_name": canonical_name,
            "contract": contract,
        },
    }


def _summary() -> dict[str, object]:
    common_objects = [
        _object(
            "COMMON-001",
            "entity",
            ["DATA-001#ENT-001"],
            ["API-001", "DATA-001"],
            canonical_name="用户",
            contract="用户是跨数据层和接口层共用的核心实体。",
        ),
        _object(
            "COMMON-002",
            "data_field",
            ["DATA-001#DF-001"],
            ["API-001", "DATA-001"],
            canonical_name="用户编号",
            contract="用户编号统一使用 string。",
        ),
        _object(
            "COMMON-003",
            "api_field",
            ["API-001#AF-001", "DATA-001#DF-001"],
            ["API-001", "DATA-001"],
            canonical_name="接口用户编号",
            contract="接口字段必须引用数据字段并保持类型一致。",
        ),
        _object(
            "COMMON-004",
            "page_source",
            ["API-001#EP-001", "PAGE-001#EL-001"],
            ["API-001", "PAGE-001"],
            canonical_name="用户详情数据来源",
            contract="用户详情元素只读取用户查询接口。",
        ),
        _object(
            "COMMON-005",
            "public_type",
            ["API-001#AF-001", "DATA-001#DF-001"],
            ["API-001", "DATA-001"],
            canonical_name="用户编号类型",
            contract="公共用户编号类型统一为 string。",
        ),
        _object(
            "COMMON-006",
            "state",
            ["PAGE-001#PG-001#STATE-ready"],
            ["API-001", "PAGE-001"],
            canonical_name="页面可用状态",
            contract="接口成功后页面进入 ready 状态。",
        ),
        _object(
            "COMMON-007",
            "error_code",
            ["API-001#ERR-001"],
            ["API-001", "PAGE-001"],
            canonical_name="用户不存在",
            contract="用户不存在统一使用 USER_NOT_FOUND。",
        ),
        _object(
            "COMMON-008",
            "authentication",
            ["API-001#EP-001", "SAFE-001#SEC-001"],
            ["API-001", "SAFE-001"],
            canonical_name="用户数据鉴权",
            contract="接口和安全控制统一校验登录态与资源归属。",
        ),
        _object(
            "COMMON-009",
            "route",
            ["PAGE-001#PG-001"],
            ["COMP-001", "PAGE-001"],
            canonical_name="用户详情路由",
            contract="用户详情统一使用 /users/:id。",
        ),
        _object(
            "COMMON-010",
            "component",
            ["COMP-001#CM-001"],
            ["COMP-001", "PAGE-001"],
            canonical_name="用户摘要卡片",
            contract="页面统一使用用户摘要卡片展示主要信息。",
        ),
        _object(
            "COMMON-011",
            "module_dependency",
            ["API-001", "DATA-001"],
            ["API-001", "DATA-001"],
            canonical_name="接口数据依赖",
            contract="接口模块显式依赖数据模块。",
        ),
    ]
    return {
        "schema_version": "design-summary.v1",
        "draft_id": "DRAFT-001",
        "common_objects": common_objects,
        "affected_modules": [
            "API-001",
            "COMP-001",
            "DATA-001",
            "PAGE-001",
            "SAFE-001",
        ],
        "open_questions": [],
    }


def _write_summary(
    project: Path,
    document: dict[str, object],
    name: str = "总体设计.json",
) -> Path:
    path = project / name
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _import_summary(project: Path, path: Path):
    return run_cli(
        ["design-summary", "DRAFT-001", "--file", path.name],
        cwd=project,
    )


def test_summary_input_cannot_self_report_index_path_hash_or_type() -> None:
    for field, value in (
        ("source_path", "设计/伪造.json"),
        ("sha256", "0" * 64),
        ("artifact_type", "design_summary_json"),
        ("input_hashes", {"module:DATA-001": "0" * 64}),
    ):
        document = _summary()
        document[field] = value
        with pytest.raises(SdlcError):
            normalize_design_summary_submission(document)


def test_summary_import_uses_stable_references_and_real_artifact_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _prepared_project(tmp_path, monkeypatch)
    result = _import_summary(project, _write_summary(project, _summary()))

    assert result.returncode == 0, result.stderr
    record = design_summary_records(paths, draft_id="DRAFT-001")[0]
    assert record["summary_id"] == "DSUM-001"
    assert record["revision"] == 1
    assert record["invalidated_modules"] == []
    assert record["invalidated_review_targets"] == []

    summary_path = paths.draft_design_dir("DRAFT-001") / "design-summary.v1.json"
    markdown_path = paths.draft_design_dir("DRAFT-001") / "总体设计说明.md"
    assert json.loads(summary_path.read_text(encoding="utf-8")) == record
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "COMMON-001" in markdown
    assert "DATA-001#ENT-001" in markdown
    assert "完整结构" in markdown

    index = json.loads(
        paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8")
    )
    entries = {item["source_path"]: item for item in index["artifacts"]}
    summary_entry = entries["设计/design-summary.v1.json"]
    assert summary_entry["business_id"] == "DSUM-001"
    assert summary_entry["artifact_type"] == "design_summary_json"
    assert summary_entry["sha256"] == sha256_bytes(summary_path.read_bytes())
    assert summary_entry["review_relations"]["applies_to"] == [
        {"owner_id": "DRAFT-001", "stage": "integrated_design"}
    ]
    data_entries = [
        item
        for item in entries.values()
        if item["business_id"] == "DATA-001"
        and item["artifact_type"] == "design_artifact_json"
    ]
    assert len(data_entries) == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda document: document["common_objects"][0].update(
                {"source_refs": ["DATA-001#ENT-999"]}
            ),
            "不存在",
        ),
        (
            lambda document: document["common_objects"].append(
                deepcopy(document["common_objects"][0])
            ),
            "重复公共对象",
        ),
        (
            lambda document: document.update(
                {"affected_modules": ["DATA-001"]}
            ),
            "affected_modules",
        ),
        (
            lambda document: document["common_objects"][4].update(
                {"source_refs": ["API-001#AF-001", "DATA-001#ENT-001"]}
            ),
            "公共类型",
        ),
    ],
)
def test_dangling_duplicate_type_and_affected_modules_reject_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:
    project, paths = _prepared_project(tmp_path, monkeypatch)
    before = paths.events_file.read_bytes()
    document = _summary()
    mutate(document)
    result = _import_summary(project, _write_summary(project, document))

    assert result.returncode != 0
    assert message in result.stderr
    assert paths.events_file.read_bytes() == before
    assert design_summary_records(paths, draft_id="DRAFT-001") == []
    assert not (paths.draft_design_dir("DRAFT-001") / "design-summary.v1.json").exists()


def test_incomplete_module_open_question_and_hash_drift_reject_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = [
        _module("data-main", "data"),
        _module("api-main", "api", depends_on=["@client:data-main"]),
    ]
    project, paths = _project_with_plan(tmp_path, monkeypatch, modules)
    assert _import_artifact(
        project,
        _write_artifact(project, _artifact("DATA-001", "data"), "数据.json"),
    ).returncode == 0
    before = paths.events_file.read_bytes()
    incomplete = _summary()
    incomplete["common_objects"] = incomplete["common_objects"][:3]
    incomplete["affected_modules"] = ["API-001", "DATA-001"]
    rejected = _import_summary(project, _write_summary(project, incomplete))
    assert rejected.returncode != 0
    assert "完整的模块产物" in rejected.stderr
    assert paths.events_file.read_bytes() == before

    drift_root = tmp_path / "漂移项目"
    drift_root.mkdir()
    prepared_project, prepared_paths = _prepared_project(
        drift_root,
        monkeypatch,
    )
    open_question = _summary()
    open_question["open_questions"] = ["公共类型保留期限待确认"]
    open_rejected = _import_summary(
        prepared_project,
        _write_summary(prepared_project, open_question, "待确认.json"),
    )
    assert open_rejected.returncode != 0
    assert "待确认问题" in open_rejected.stderr

    data_record = next(
        item
        for item in design_artifact_records(
            prepared_paths,
            draft_id="DRAFT-001",
        )
        if item["artifact_id"] == "DATA-001"
    )
    module_path = prepared_paths.draft_dir("DRAFT-001") / data_record["output_path"]
    module_path.write_text('{"被改写":true}\n', encoding="utf-8")
    before_drift = prepared_paths.events_file.read_bytes()
    drift = _import_summary(
        prepared_project,
        _write_summary(prepared_project, _summary(), "哈希漂移.json"),
    )
    assert drift.returncode != 0
    assert "投影哈希已经变化" in drift.stderr
    assert prepared_paths.events_file.read_bytes() == before_drift
    assert design_summary_records(prepared_paths, draft_id="DRAFT-001") == []


def test_idempotent_revision_keeps_history_and_invalidates_only_explicit_relations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _prepared_project(tmp_path, monkeypatch)
    first_document = _summary()
    first_path = _write_summary(project, first_document)
    first = _import_summary(project, first_path)
    event_count = len(load_events(paths))
    duplicate = _import_summary(project, first_path)

    second_document = deepcopy(first_document)
    public_type = next(
        item
        for item in second_document["common_objects"]
        if item["business_id"] == "COMMON-005"
    )
    public_type["definition"]["contract"] = "公共用户编号在全部模块中统一为 string。"
    second_document["affected_modules"] = ["API-001", "DATA-001"]
    second = _import_summary(
        project,
        _write_summary(project, second_document, "总体设计二.json"),
    )

    assert first.returncode == 0 and duplicate.returncode == 0 and second.returncode == 0
    assert "已经存在" in duplicate.stdout
    assert len(load_events(paths)) == event_count + 1
    history = design_summary_history(paths, draft_id="DRAFT-001")
    assert [item["revision"] for item in history] == [1, 2]
    assert history[1]["previous_summary_sha256"] == history[0]["summary_sha256"]
    assert history[1]["invalidated_modules"] == ["API-001", "DATA-001"]
    assert history[1]["invalidated_review_targets"] == [
        {
            "owner_id": "DRAFT-001",
            "stage": "integrated_design",
            "status": "stale",
        }
    ]
    state = derive_state(paths)["drafts"]["DRAFT-001"]
    assert state["design_summary_invalidation"]["stale_modules"] == [
        "API-001",
        "DATA-001",
    ]
    assert state["design_summary_invalidation"]["current_modules"] == [
        "COMP-001",
        "PAGE-001",
        "SAFE-001",
    ]

    # 模块产生合法新修订后，事件仍能重建上一版总体说明；随后用显式模块列表刷新输入哈希。
    revised_data = _artifact("DATA-001", "data")
    revised_data["content"]["lifecycle"]["retention"] = "注销后三十天"
    module_revision = _import_artifact(
        project,
        _write_artifact(project, revised_data, "数据设计二.json"),
    )
    third_document = deepcopy(second_document)
    third_document["affected_modules"] = ["DATA-001"]
    third = _import_summary(
        project,
        _write_summary(project, third_document, "总体设计三.json"),
    )
    refreshed_history = design_summary_history(paths, draft_id="DRAFT-001")
    assert module_revision.returncode == 0, module_revision.stderr
    assert third.returncode == 0, third.stderr
    assert [item["revision"] for item in refreshed_history] == [1, 2, 3]
    assert refreshed_history[-1]["invalidated_modules"] == ["DATA-001"]
    assert refreshed_history[-1]["previous_summary_sha256"] == (
        refreshed_history[-2]["summary_sha256"]
    )


def test_event_rebuild_and_projection_failure_leave_no_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _prepared_project(tmp_path, monkeypatch)
    path = _write_summary(project, _summary())
    before = paths.events_file.read_bytes()
    original_replace = draft_artifacts._replace_projection

    def fail_projection(source: Path, target: Path) -> None:
        if target.name == "design-summary.v1.json":
            raise OSError("注入总体设计投影失败")
        original_replace(source, target)

    monkeypatch.setattr(draft_artifacts, "_replace_projection", fail_projection)
    with pytest.raises(OSError, match="注入总体设计投影失败"):
        DesignSummaryService(paths).import_file("DRAFT-001", path.name)
    assert paths.events_file.read_bytes() == before
    assert design_summary_records(paths, draft_id="DRAFT-001") == []
    assert not list(paths.draft_staging_dir("DRAFT-001").glob("projection-*"))
    assert not (paths.draft_design_dir("DRAFT-001") / "design-summary.v1.json").exists()

    monkeypatch.setattr(draft_artifacts, "_replace_projection", original_replace)
    assert _import_summary(project, path).returncode == 0
    latest = design_summary_records(paths, draft_id="DRAFT-001")[0]
    summary_path = paths.draft_design_dir("DRAFT-001") / "design-summary.v1.json"
    markdown_path = paths.draft_design_dir("DRAFT-001") / "总体设计说明.md"
    summary_path.unlink()
    markdown_path.write_text("# 人工改写\n\nPrompt 内容\n", encoding="utf-8")
    paths.draft_artifact_index_file("DRAFT-001").unlink()

    refresh_materialized_state(paths)

    assert json.loads(summary_path.read_text(encoding="utf-8")) == latest
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "人工改写" not in markdown and "Prompt 内容" not in markdown
    assert "COMMON-011" in markdown
    index = json.loads(
        paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8")
    )
    assert any(
        item["source_path"] == "设计/design-summary.v1.json"
        for item in index["artifacts"]
    )
