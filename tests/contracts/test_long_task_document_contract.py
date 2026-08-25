from __future__ import annotations

import json
from pathlib import Path

from codex_sdlc.core.project import build_paths
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_contract import import_task_plan_bundle


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_task_larger_than_30kb_is_saved_to_json_and_markdown_without_truncation(tmp_path: Path) -> None:
    project = tmp_path / "长任务临时项目"
    requirement_root = project / ".codex-sdlc" / "requirements" / "REQ-001-长任务"
    original = requirement_root / "original" / "正式依据.md"
    original.parent.mkdir(parents=True)
    original.write_text("长任务正式依据。\n", encoding="utf-8")
    digest = sha256_file(original)
    reference = {
        "schema_version": "reference-locator.v1",
        "path": "original/正式依据.md",
        "sha256": digest,
        "locator": {"kind": "whole_file"},
    }
    _write_json(
        requirement_root / "reference-index.v1.json",
        {
            "schema_version": "reference-index.v1",
            "requirement_id": "REQ-001",
            "entries": {
                "FR-001": reference,
                "AC-001": reference,
                "DES-001#architecture": reference,
            },
        },
    )
    _write_json(
        requirement_root / "original" / "formal.v3.json",
        {
            "formal_contract_version": "formal.v3",
            "workflow_profile": "document-first.v1",
        },
    )
    event = {
        "event_id": "EVT-20260720-000001",
        "event_type": "requirement_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": None,
        "created_at": "2026-07-20T10:00:00+08:00",
        "source": "合同测试",
        "summary": "创建正式需求 REQ-001",
        "payload": {
            "title": "长任务",
            "description": "验证完整保存。",
            "summary": "验证完整保存。",
            "folder_name": "REQ-001-长任务",
            "flow_type": "SDLC 原生正式流程",
            "native_start": {"formal_contract_version": "formal.v3"},
        },
    }
    (project / ".codex-sdlc" / "events.jsonl").write_text(
        json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    marker = "长任务尾部唯一标记-不得截断"
    long_requirement = "每一条实现要求都必须完整保留。" * 1800 + marker
    plan_file = tmp_path / "模型输出" / "task-plan.v2.json"
    tasks_dir = tmp_path / "模型输出" / "任务"
    coverage_file = tmp_path / "模型输出" / "task-coverage.v1.json"
    _write_json(
        plan_file,
        {
            "schema_version": "task-plan.v2",
            "requirement_id": "REQ-001",
            "tasks": ["@client:long-task"],
            "dependencies": [],
        },
    )
    task = {
        "schema_version": "task.v2",
        "requirement_id": "REQ-001",
        "client_key": "long-task",
        "title": "完整保存长任务",
        "goal": "完整保存所有字段，不按字符、字节或行数截断。",
        "deliverables": ["完整 JSON 和完整 Markdown。"],
        "depends_on": [],
        "requirement_refs": ["FR-001"],
        "global_rule_refs": [],
        "technical_solution_refs": [
            {"id": "DES-001", "reference_key": "DES-001#architecture"}
        ],
        "design_refs": [],
        "material_refs": [],
        "change_refs": [],
        "acceptance_refs": ["AC-001"],
        "code_scope": {
            "read_paths": ["src"],
            "likely_change_paths": ["src/long_task.py"],
            "protected_paths": [".codex-sdlc/requirements"],
        },
        "implementation_requirements": [long_requirement],
        "data_api_page_component_requirements": ["不涉及页面；只验证长任务文件。"],
        "states_and_exceptions": ["写入失败时不能留下半份任务。"],
        "security_and_privacy": ["不保存真实秘密。"],
        "automated_tests": ["逐字核对长字段。"],
        "manual_checks": ["在 Markdown 中搜索尾部唯一标记。"],
        "out_of_scope": ["不启动任务运行轮次。"],
        "blocking_conditions": [],
        "definition_of_done": ["JSON 与 Markdown 均包含完整尾部标记。"],
    }
    _write_json(tasks_dir / "long-task.task.v2.json", task)
    _write_json(
        coverage_file,
        {
            "schema_version": "task-coverage.v1",
            "requirement_id": "REQ-001",
            "functional_requirements": {
                "FR-001": {"tasks": ["@client:long-task"], "status": "implemented"}
            },
            "design_artifacts": {},
            "acceptance_criteria": {
                "AC-001": {
                    "tasks": ["@client:long-task"],
                    "test_refs": ["@client:long-task#automated_tests/0"],
                }
            },
            "effective_changes": {},
            "no_development_items": [],
        },
    )

    import_task_plan_bundle(
        build_paths(project),
        requirement_id="REQ-001",
        plan_file=plan_file,
        tasks_dir=tasks_dir,
        coverage_file=coverage_file,
    )

    task_json_path = requirement_root / "tasks" / "T-001.json"
    task_markdown_path = requirement_root / "tasks" / "T-001.md"
    saved = json.loads(task_json_path.read_text(encoding="utf-8"))
    markdown = task_markdown_path.read_text(encoding="utf-8")
    assert task_json_path.stat().st_size > 30 * 1024
    assert task_markdown_path.stat().st_size > 30 * 1024
    assert saved["implementation_requirements"][0] == long_requirement
    assert marker in markdown
    assert "..." not in saved["implementation_requirements"][0]
    assert "…" not in saved["implementation_requirements"][0]
