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
from codex_sdlc.core.design_artifact_contract import (
    design_artifact_history,
    design_artifact_records,
    rebuild_design_artifact_projections,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.state import load_events, refresh_materialized_state
from codex_sdlc.services.design_service import DesignArtifactService
from test_cli_v1 import run_cli
from test_design_plan_contract import (
    _confirmed_project,
    _import as _import_plan,
    _module,
    _plan,
    _write_plan,
)


def _content(module_type: str) -> dict[str, object]:
    """每类夹具只填写该模块真正需要的字段，用测试防止合同退化成统一大模板。"""

    contents: dict[str, dict[str, object]] = {
        "data": {
            "entities": [
                {
                    "entity_id": "ENT-001",
                    "name": "用户",
                    "storage_name": "users",
                    "fields": [
                        {
                            "field_id": "DF-001",
                            "name": "用户编号",
                            "type": "string",
                            "nullable": False,
                            "default": None,
                            "unique": True,
                        }
                    ],
                    "unique_constraints": [["DF-001"]],
                    "indexes": [
                        {
                            "index_id": "IDX-001",
                            "field_ids": ["DF-001"],
                            "unique": True,
                        }
                    ],
                    "relations": [],
                }
            ],
            "lifecycle": {"retention": "账号存续期间", "deletion": "注销后立即删除"},
            "migration_steps": ["创建 users 表"],
            "rollback_steps": ["删除 users 表"],
        },
        "api": {
            "endpoints": [
                {
                    "endpoint_id": "EP-001",
                    "name": "读取用户",
                    "caller": "网页端",
                    "provider": "用户服务",
                    "transport": "HTTP",
                    "path_or_event": "GET /users/{id}",
                    "authentication": "登录态",
                    "request_fields": [
                        {
                            "field_id": "AF-001",
                            "name": "用户编号",
                            "type": "string",
                            "required": True,
                            "data_field_ref": "DATA-001#DF-001",
                        }
                    ],
                    "response_fields": [
                        {
                            "field_id": "AF-002",
                            "name": "用户编号",
                            "type": "string",
                            "required": True,
                            "data_field_ref": "DATA-001#DF-001",
                        }
                    ],
                    "errors": [
                        {
                            "error_id": "ERR-001",
                            "code": "USER_NOT_FOUND",
                            "condition": "用户不存在",
                            "response": "返回不存在结果",
                        }
                    ],
                    "idempotency": "只读接口天然幂等",
                    "retry": "网络失败最多重试一次",
                    "timeout_ms": 3000,
                }
            ]
        },
        "page": {
            "pages": [
                {
                    "page_id": "PG-001",
                    "name": "用户详情",
                    "route": "/users/:id",
                    "navigation_refs": [],
                    "elements": [
                        {
                            "element_id": "EL-001",
                            "name": "用户编号",
                            "data_source_refs": ["API-001#EP-001"],
                        }
                    ],
                    "states": {
                        "initial": "等待进入页面",
                        "loading": "显示加载状态",
                        "empty": "显示空数据状态",
                        "ready": "显示用户详情",
                        "error": "显示读取失败状态",
                        "forbidden": "显示无权限状态",
                    },
                    "layout": "单列详情布局",
                    "interactions": ["进入页面后读取用户"],
                    "responsive": ["窄屏改为纵向排列"],
                    "ui_material_refs": ["MAT-002"],
                }
            ]
        },
        "component": {
            "components": [
                {
                    "component_id": "CM-001",
                    "name": "用户摘要卡片",
                    "responsibilities": ["显示用户主要信息"],
                    "inputs": ["用户编号"],
                    "outputs": ["用户摘要"],
                    "dependencies": [],
                    "states": ["加载中", "可用", "失败"],
                    "error_handling": ["读取失败时显示重试入口"],
                }
            ]
        },
        "security": {
            "controls": [
                {
                    "control_id": "SEC-001",
                    "name": "用户数据访问控制",
                    "assets": ["用户资料"],
                    "actors": ["登录用户"],
                    "permissions": ["只能读取本人资料"],
                    "sensitive_data": ["用户编号"],
                    "authentication": ["校验登录态"],
                    "audit": ["记录访问结果"],
                    "threats": ["越权读取"],
                    "mitigations": ["服务端校验资源归属"],
                }
            ]
        },
        "deployment": {
            "environments": [
                {
                    "environment_id": "ENV-001",
                    "name": "测试环境",
                    "configuration_refs": ["用户服务地址"],
                    "dependencies": ["数据库"],
                }
            ],
            "rollout_steps": ["先部署用户服务"],
            "migration_steps": ["执行用户表变更"],
            "rollback_steps": ["恢复服务和数据库"],
            "health_checks": ["用户读取接口返回成功"],
        },
        "field": {
            "scenarios": [
                {
                    "scenario_id": "FV-001",
                    "name": "真实设备登录验证",
                    "prerequisites": ["设备已经联网"],
                    "environment": "客户测试环境",
                    "account_requirements": ["普通测试账号"],
                    "data_requirements": ["账号存在用户资料"],
                    "steps": ["登录并打开用户详情"],
                    "expected_results": ["完整显示用户详情"],
                    "evidence_requirements": ["保存页面截图"],
                    "cleanup_steps": ["退出测试账号"],
                }
            ]
        },
        "special": {
            "reason": "供应商专有协议不能归入固定模块",
            "design_items": [
                {
                    "spec_id": "SP-001",
                    "name": "供应商协议适配",
                    "inputs": ["供应商协议文件"],
                    "outputs": ["适配结果"],
                    "dependencies": [],
                    "review_method": "使用供应商校验工具核对",
                    "acceptance": ["协议文件可被正确处理"],
                    "rollback_steps": ["恢复适配前处理器"],
                }
            ],
        },
    }
    return deepcopy(contents[module_type])


def _artifact(
    artifact_id: str,
    module_type: str,
    *,
    depends_on: list[str] | None = None,
    global_rule_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "design-artifact.v1",
        "draft_id": "DRAFT-001",
        "artifact_id": artifact_id,
        "type": module_type,
        "requirement_refs": ["FR-001"],
        "global_rule_refs": global_rule_refs or [],
        "material_refs": ["MAT-002"],
        "depends_on": depends_on or [],
        "content": _content(module_type),
        "open_questions": [],
    }


def _write_artifact(
    project: Path,
    document: dict[str, object],
    name: str,
) -> Path:
    path = project / name
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _import_artifact(project: Path, path: Path):
    return run_cli(
        ["design-artifact", "DRAFT-001", "--file", path.name],
        cwd=project,
    )


def _project_with_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    modules: list[dict[str, object]],
):
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    result = _import_plan(project, _write_plan(project, _plan(modules)))
    assert result.returncode == 0, result.stderr
    return project, paths


@pytest.mark.parametrize(
    ("module_type", "artifact_id"),
    [
        ("data", "DATA-001"),
        ("api", "API-001"),
        ("page", "PAGE-001"),
        ("component", "COMP-001"),
        ("security", "SAFE-001"),
        ("deployment", "DEPLOY-001"),
        ("field", "FIELD-001"),
        ("special", "SPEC-001"),
    ],
)
def test_eight_module_contents_use_independent_strict_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_type: str,
    artifact_id: str,
) -> None:
    module = _module(f"{module_type}-main", module_type)
    if module_type == "special":
        module["special_reason"] = "供应商专有协议不能归入固定模块"
    project, paths = _project_with_plan(tmp_path, monkeypatch, [module])
    document = _artifact(artifact_id, module_type)
    if module_type == "api":
        for endpoint in document["content"]["endpoints"]:
            for group in ("request_fields", "response_fields"):
                for field in endpoint[group]:
                    field["data_field_ref"] = None
    if module_type == "page":
        for page in document["content"]["pages"]:
            for element in page["elements"]:
                element["data_source_refs"] = []

    accepted = _import_artifact(
        project,
        _write_artifact(project, document, f"{module_type}.json"),
    )
    wrong = deepcopy(document)
    wrong["content"]["不属于模块的字段"] = True
    rejected = _import_artifact(
        project,
        _write_artifact(project, wrong, f"{module_type}-错误.json"),
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0
    assert "未知字段" in rejected.stderr
    assert design_artifact_records(paths, draft_id="DRAFT-001")[0]["type"] == module_type


def test_data_api_fields_dependencies_and_page_six_states_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = [
        _module("data-main", "data"),
        _module("api-main", "api", depends_on=["@client:data-main"]),
        _module("page-main", "page", depends_on=["@client:api-main"]),
    ]
    project, paths = _project_with_plan(tmp_path, monkeypatch, modules)
    api_path = _write_artifact(
        project,
        _artifact("API-001", "api", depends_on=["DATA-001"]),
        "接口.json",
    )
    before = paths.events_file.read_bytes()

    dependency_rejected = _import_artifact(project, api_path)
    assert dependency_rejected.returncode != 0
    assert "依赖模块" in dependency_rejected.stderr
    assert paths.events_file.read_bytes() == before

    data_path = _write_artifact(project, _artifact("DATA-001", "data"), "数据.json")
    assert _import_artifact(project, data_path).returncode == 0

    wrong_api = _artifact("API-001", "api", depends_on=["DATA-001"])
    wrong_api["content"]["endpoints"][0]["request_fields"][0]["type"] = "integer"
    mismatch = _import_artifact(
        project,
        _write_artifact(project, wrong_api, "字段不一致.json"),
    )
    assert mismatch.returncode != 0
    assert "数据字段类型不一致" in mismatch.stderr
    assert _import_artifact(project, api_path).returncode == 0

    page = _artifact("PAGE-001", "page", depends_on=["API-001"])
    page["content"]["pages"][0]["states"].pop("forbidden")
    page_rejected = _import_artifact(
        project,
        _write_artifact(project, page, "页面状态缺失.json"),
    )
    assert page_rejected.returncode != 0
    assert "forbidden" in page_rejected.stderr

    page["content"]["pages"][0]["states"]["forbidden"] = "显示无权限状态"
    assert _import_artifact(
        project,
        _write_artifact(project, page, "页面.json"),
    ).returncode == 0
    assert [item["artifact_id"] for item in design_artifact_records(paths)] == [
        "API-001",
        "DATA-001",
        "PAGE-001",
    ]


def test_plan_status_hash_refs_and_no_empty_module_files_are_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modules = [
        _module("required-main", "data"),
        _module("provided-main", "component", status="provided"),
        _module("blocked-main", "field", status="blocked"),
        _module("unused-main", "deployment", status="not_applicable"),
    ]
    modules[0]["inputs"].append("GR-001")
    project, paths = _project_with_plan(tmp_path, monkeypatch, modules)
    before = paths.events_file.read_bytes()

    blocked = _import_artifact(
        project,
        _write_artifact(project, _artifact("FIELD-001", "field"), "阻塞.json"),
    )
    unused = _import_artifact(
        project,
        _write_artifact(project, _artifact("DEPLOY-001", "deployment"), "不适用.json"),
    )
    self_reported = _artifact("DATA-001", "data")
    self_reported["input_hashes"] = {"plan": "0" * 64}
    fake_hash = _import_artifact(
        project,
        _write_artifact(project, self_reported, "自报哈希.json"),
    )

    assert blocked.returncode != 0 and "blocked" in blocked.stderr
    assert unused.returncode != 0 and "不适用" in unused.stderr
    assert fake_hash.returncode != 0 and "自报" in fake_hash.stderr
    assert paths.events_file.read_bytes() == before
    assert not any(
        path.name.startswith(("FIELD-001", "DEPLOY-001"))
        for path in paths.draft_design_dir("DRAFT-001").rglob("*")
    )

    assert _import_artifact(
        project,
        _write_artifact(
            project,
            _artifact(
                "DATA-001",
                "data",
                global_rule_refs=["GR-001"],
            ),
            "必需.json",
        ),
    ).returncode == 0
    assert _import_artifact(
        project,
        _write_artifact(project, _artifact("COMP-001", "component"), "已提供.json"),
    ).returncode == 0
    provided = next(
        item
        for item in design_artifact_records(paths)
        if item["artifact_id"] == "COMP-001"
    )
    assert provided["plan_status"] == "provided"
    assert provided["output_path"] == "设计/模块/COMP-001.design-artifact.v1.json"


def test_open_questions_missing_refs_and_required_rollback_reject_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("data-main", "data")],
    )
    before = paths.events_file.read_bytes()
    open_question = _artifact("DATA-001", "data")
    open_question["open_questions"] = ["字段保留期限待确认"]
    missing_ref = _artifact("DATA-001", "data")
    missing_ref["requirement_refs"] = ["FR-999"]
    no_rollback = _artifact("DATA-001", "data")
    no_rollback["content"]["rollback_steps"] = []

    results = [
        _import_artifact(
            project,
            _write_artifact(project, open_question, "待确认.json"),
        ),
        _import_artifact(
            project,
            _write_artifact(project, missing_ref, "错误引用.json"),
        ),
        _import_artifact(
            project,
            _write_artifact(project, no_rollback, "无回滚.json"),
        ),
    ]

    assert all(item.returncode != 0 for item in results)
    assert "待确认问题" in results[0].stderr
    assert "计划" in results[1].stderr
    assert "rollback_steps" in results[2].stderr
    assert paths.events_file.read_bytes() == before
    assert design_artifact_records(paths) == []


def test_idempotent_revision_events_rebuild_json_and_complete_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("data-main", "data")],
    )
    first_document = _artifact("DATA-001", "data")
    first_path = _write_artifact(project, first_document, "数据设计.json")
    first = _import_artifact(project, first_path)
    event_count = len(load_events(paths))
    duplicate = _import_artifact(project, first_path)

    second_document = deepcopy(first_document)
    second_document["content"]["lifecycle"]["retention"] = "注销后保留三十天"
    second = _import_artifact(
        project,
        _write_artifact(project, second_document, "数据设计二.json"),
    )
    history = design_artifact_history(paths, draft_id="DRAFT-001")

    assert first.returncode == 0 and duplicate.returncode == 0 and second.returncode == 0
    assert "已经存在" in duplicate.stdout
    assert len(load_events(paths)) == event_count + 1
    assert [item["revision"] for item in history] == [1, 2]
    assert history[1]["previous_artifact_sha256"] == history[0]["artifact_sha256"]

    latest = design_artifact_records(paths)[0]
    json_path = paths.draft_dir("DRAFT-001") / latest["output_path"]
    markdown_path = json_path.with_suffix(".md")
    json_path.unlink()
    markdown_path.write_text("# 人工改写\n\nPrompt 内容\n", encoding="utf-8")
    rebuilt = rebuild_design_artifact_projections(paths, "DRAFT-001")

    assert rebuilt == [latest]
    assert json.loads(json_path.read_text(encoding="utf-8")) == latest
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "人工改写" not in markdown and "Prompt 内容" not in markdown
    assert "注销后保留三十天" in markdown
    assert "input_hashes" in markdown


def test_stale_input_and_projection_failure_leave_no_event_projection_or_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("data-main", "data")],
    )
    path = _write_artifact(project, _artifact("DATA-001", "data"), "数据.json")
    before = paths.events_file.read_bytes()
    original_replace = draft_artifacts._replace_projection

    def fail_projection(source: Path, target: Path) -> None:
        if target.name.endswith("design-artifact.v1.json"):
            raise OSError("注入模块投影失败")
        original_replace(source, target)

    monkeypatch.setattr(draft_artifacts, "_replace_projection", fail_projection)
    with pytest.raises(OSError, match="注入模块投影失败"):
        DesignArtifactService(paths).import_file("DRAFT-001", path.name)
    assert paths.events_file.read_bytes() == before
    assert design_artifact_records(paths) == []
    assert not list(paths.draft_dir("DRAFT-001").rglob("*.design-artifact.v1.json"))
    assert not list(paths.draft_staging_dir("DRAFT-001").glob("projection-*"))

    monkeypatch.setattr(draft_artifacts, "_replace_projection", original_replace)
    assert _import_artifact(project, path).returncode == 0
    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    latest = design_artifact_records(paths)[0]
    projection = paths.draft_dir("DRAFT-001") / latest["output_path"]
    original = projection.read_bytes()
    with pytest.raises(SdlcError, match="输入哈希已经变化"):
        rebuild_design_artifact_projections(paths, "DRAFT-001")
    assert projection.read_bytes() == original
    with pytest.raises(SdlcError, match="输入哈希已经变化"):
        refresh_materialized_state(paths)
