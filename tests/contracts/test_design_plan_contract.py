from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import draft_artifacts
from codex_sdlc.core.design_plan_contract import (
    assess_design_plan,
    design_plan_records,
    rebuild_design_plan_projections,
    validate_design_plan_record,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.state import load_events
from codex_sdlc.services.design_service import DesignPlanService
from test_cli_v1 import SDLC_BIN, run_cli
from test_design_reference_contract import (
    create_confirmed_design_project,
    import_reference,
    write_design_reference,
)


def _confirmed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    project, paths, _source, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
        long=False,
    )
    assert import_reference(project, write_design_reference(project)).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    (project / "AGENTS.md").write_text("# 项目规则\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "src").mkdir(exist_ok=True)
    (project / "src/app.py").write_text(
        "# 该文件作为设计计划明确选择的真实代码证据。\nVALUE = 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "合同测试"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "建立设计计划夹具"], cwd=project, check=True)
    return project, paths


def _module(
    key: str,
    module_type: str,
    *,
    status: str = "required",
    depends_on: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict[str, object]:
    outputs = (
        []
        if status in {"provided", "not_applicable", "blocked"}
        else [f"设计/{key}_{{module_id}}.design-artifact.v1.json"]
    )
    return {
        "client_key": key,
        "type": module_type,
        "status": status,
        "reason": f"{key} 需要明确设计边界",
        "requirement_refs": ["FR-001"],
        "design_refs": ["DES-001"],
        "material_refs": ["MAT-002"],
        "code_evidence_paths": evidence if evidence is not None else ["src/app.py"],
        "inputs": ["FR-001", "DES-001"],
        "outputs": outputs,
        "depends_on": depends_on or [],
        "blocked_by": ["真实设备"] if status == "blocked" else [],
    }


def _plan(modules: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": "design-plan.v1",
        "draft_id": "DRAFT-001",
        "global_impact": ["需求实现和现有代码边界"],
        "modules": modules,
        "code_evidence": {
            "purpose": "integrated_design",
            "rules": ["AGENTS.md"],
            "dependencies": ["package-lock.json"],
            "code_files": [{"path": "src/app.py", "reason_ref": "FR-001"}],
            "upstream_outputs": [],
        },
    }


def _write_plan(project: Path, document: dict[str, object], name: str = "设计总计划.json") -> Path:
    path = project / name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _import(project: Path, path: Path):
    return run_cli(["design-plan", "DRAFT-001", "--file", path.name], cwd=project)


def _assert_no_design_plan_residue(paths, original_events: bytes) -> None:
    """拒绝必须停在正式提交前，后续合法导入才能继续使用首个模块编号。"""

    assert paths.events_file.read_bytes() == original_events
    assert design_plan_records(paths) == []
    assert not paths.draft_design_plan_file("DRAFT-001").exists()
    assert not paths.draft_code_evidence_file("DRAFT-001").exists()
    assert not paths.draft_design_plan_markdown_file("DRAFT-001").exists()


@pytest.mark.parametrize(
    ("modules", "types"),
    [
        (
            [
                _module("data-main", "data"),
                _module("api-main", "api", depends_on=["@client:data-main"]),
                _module("page-main", "page", depends_on=["@client:api-main"]),
                _module("safe-main", "security", depends_on=["@client:api-main"]),
            ],
            ["data", "api", "page", "security"],
        ),
        (
            [
                _module("page-main", "page"),
                _module("component-main", "component", depends_on=["@client:page-main"]),
            ],
            ["page", "component"],
        ),
        (
            [
                _module("data-main", "data"),
                _module("api-main", "api", depends_on=["@client:data-main"]),
            ],
            ["data", "api"],
        ),
        ([_module("component-main", "component")], ["component"]),
    ],
)
def test_four_project_plans_only_create_explicit_applicable_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    modules: list[dict[str, object]],
    types: list[str],
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    result = _import(project, _write_plan(project, _plan(modules)))

    assert result.returncode == 0, result.stderr
    record = design_plan_records(paths, draft_id="DRAFT-001")[0]
    assert [item["type"] for item in record["modules"]] == types
    assert len(record["modules"]) == len(types)
    assert all(item["module_id"].split("-", 1)[0] in result.stdout for item in record["modules"])
    assert all("{module_id}" not in output for item in record["modules"] for output in item["outputs"])


def test_numbering_dependency_rewrite_and_idempotency_are_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    document = _plan(
        [
            _module("page-main", "page", depends_on=["@client:api-main"]),
            _module("api-main", "api", depends_on=["@client:data-main"]),
            _module("data-main", "data"),
        ]
    )
    first = _import(project, _write_plan(project, document))
    before = load_events(paths)
    reordered = deepcopy(document)
    reordered["modules"] = list(reversed(reordered["modules"]))
    second = _import(project, _write_plan(project, reordered, "重排计划.json"))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "已经存在" in second.stdout
    assert load_events(paths) == before
    record = design_plan_records(paths)[0]
    assert record["mapping"] == {
        "api-main": "API-001",
        "data-main": "DATA-001",
        "page-main": "PAGE-001",
    }
    by_type = {item["type"]: item for item in record["modules"]}
    assert by_type["api"]["depends_on"] == ["DATA-001"]
    assert by_type["page"]["depends_on"] == ["API-001"]


def test_two_concurrent_same_plan_imports_share_one_event_and_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    path = _write_plan(project, _plan([_module("data-main", "data")]))
    environment = os.environ.copy()
    environment["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"

    def run_once():
        return subprocess.run(
            [str(SDLC_BIN), "design-plan", "DRAFT-001", "--file", path.name],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: run_once(), range(2)))

    assert all(item.returncode == 0 for item in results)
    assert sorted("已经存在" in item.stdout for item in results) == [False, True]
    assert design_plan_records(paths)[0]["mapping"] == {"data-main": "DATA-001"}
    assert len(
        [event for event in load_events(paths) if event["event_type"] == "draft_design_plan_imported"]
    ) == 1


def test_all_module_status_contracts_keep_explicit_outputs_evidence_and_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    modules = [
        _module("required-main", "data"),
        _module("supplement-main", "api", status="supplement_required"),
        _module("provided-main", "component", status="provided"),
        _module("not-applicable-main", "deployment", status="not_applicable"),
        _module("blocked-main", "field", status="blocked"),
        {
            **_module("special-main", "special"),
            "special_reason": "固定模块不能表达供应商专有文件格式",
        },
    ]

    result = _import(project, _write_plan(project, _plan(modules)))

    assert result.returncode == 0, result.stderr
    record = design_plan_records(paths)[0]
    by_status = {item["status"]: item for item in record["modules"]}
    assert by_status["required"]["outputs"]
    assert by_status["supplement_required"]["outputs"]
    assert by_status["provided"]["code_evidence_paths"] == ["src/app.py"]
    assert by_status["not_applicable"]["outputs"] == []
    assert by_status["blocked"]["blocked_by"] == ["真实设备"]
    special = next(item for item in record["modules"] if item["type"] == "special")
    assert special["module_id"] == "SPEC-001"
    assert special["special_reason"]


@pytest.mark.parametrize("evidence_kind", ["material", "code"])
def test_provided_accepts_either_valid_material_or_real_code_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_kind: str,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    module = _module("provided-main", "component", status="provided")
    if evidence_kind == "material":
        module["code_evidence_paths"] = []
    else:
        module["material_refs"] = []

    result = _import(project, _write_plan(project, _plan([module])))

    assert result.returncode == 0, result.stderr
    record = design_plan_records(paths)[0]["modules"][0]
    assert record["status"] == "provided"
    if evidence_kind == "material":
        assert record["material_refs"] == ["MAT-002"]
        assert record["code_evidence_paths"] == []
    else:
        assert record["material_refs"] == []
        assert record["code_evidence_paths"] == ["src/app.py"]


@pytest.mark.parametrize(
    ("material_refs", "message"),
    [
        ([], "必须引用有效资料或真实代码证据"),
        (["MAT-999"], "不存在或非活动的 MAT"),
    ],
)
def test_provided_rejects_missing_or_invalid_material_without_residue_or_number_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    material_refs: list[str],
    message: str,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    invalid = _module("provided-main", "component", status="provided", evidence=[])
    invalid["material_refs"] = material_refs
    before = paths.events_file.read_bytes()

    rejected = _import(project, _write_plan(project, _plan([invalid]), "无效证据计划.json"))

    assert rejected.returncode != 0
    assert message in rejected.stderr
    _assert_no_design_plan_residue(paths, before)
    valid = _module("provided-main", "component", status="provided", evidence=[])
    accepted = _import(project, _write_plan(project, _plan([valid]), "有效资料计划.json"))
    assert accepted.returncode == 0, accepted.stderr
    assert design_plan_records(paths)[0]["mapping"] == {"provided-main": "COMP-001"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda p: p["modules"][0].update(
                status="provided",
                outputs=[],
                material_refs=[],
                code_evidence_paths=[],
            ),
            "有效资料或真实代码证据",
        ),
        (lambda p: p["modules"][0].update(status="blocked", outputs=[], blocked_by=[]), "blocked"),
        (lambda p: p["modules"][0].update(status="required", outputs=[]), "必须声明输出"),
        (lambda p: p["modules"][0].update(status="not_applicable", outputs=["设计/{module_id}.json"]), "不适用"),
        (lambda p: p["modules"][0].update(depends_on=["@client:missing"]), "悬空"),
        (lambda p: p["modules"][0].update(outputs=["../{module_id}.json"]), "设计目录"),
    ],
)
def test_invalid_status_dependency_and_output_reject_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    document = _plan([_module("data-main", "data")])
    mutate(document)
    before = paths.events_file.read_bytes()

    result = _import(project, _write_plan(project, document))

    assert result.returncode != 0
    assert message in result.stderr
    assert paths.events_file.read_bytes() == before
    assert design_plan_records(paths) == []
    assert not paths.draft_design_plan_file("DRAFT-001").exists()


def test_cycle_wrong_refs_and_caller_reported_fields_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    cycle = _plan(
        [
            _module("data-main", "data", depends_on=["@client:api-main"]),
            _module("api-main", "api", depends_on=["@client:data-main"]),
        ]
    )
    wrong_fr = _plan([_module("data-main", "data")])
    wrong_fr["modules"][0]["requirement_refs"] = ["FR-999"]
    caller_fields = _plan([_module("data-main", "data")])
    caller_fields["modules"][0]["module_id"] = "DATA-999"
    caller_fields["repo_key"] = "0" * 64
    before = paths.events_file.read_bytes()

    results = [
        _import(project, _write_plan(project, cycle, "环.json")),
        _import(project, _write_plan(project, wrong_fr, "错误FR.json")),
        _import(project, _write_plan(project, caller_fields, "自报字段.json")),
    ]

    assert all(item.returncode != 0 for item in results)
    assert "依赖环" in results[0].stderr
    assert "不存在的 FR" in results[1].stderr
    assert "未知字段" in results[2].stderr
    assert paths.events_file.read_bytes() == before
    assert design_plan_records(paths) == []


def test_failed_import_does_not_create_number_gap_and_conflict_cannot_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    invalid = _plan([_module("data-main", "data", depends_on=["@client:missing"])])
    assert _import(project, _write_plan(project, invalid, "非法计划.json")).returncode != 0
    valid = _plan([_module("data-main", "data")])
    assert _import(project, _write_plan(project, valid, "合法计划.json")).returncode == 0
    before = load_events(paths)
    conflict = _plan([_module("page-main", "page")])
    rejected = _import(project, _write_plan(project, conflict, "冲突计划.json"))

    assert design_plan_records(paths)[0]["mapping"]["data-main"] == "DATA-001"
    assert rejected.returncode != 0
    assert "不能原地覆盖" in rejected.stderr
    assert load_events(paths) == before


def test_projection_failure_rolls_back_event_files_and_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    document = _plan([_module("data-main", "data")])
    path = _write_plan(project, document)
    before = paths.events_file.read_bytes()
    original_replace = draft_artifacts._replace_projection

    def fail_plan_projection(source: Path, target: Path) -> None:
        if target.name == "design-plan.v1.json":
            raise OSError("注入投影失败")
        original_replace(source, target)

    monkeypatch.setattr(draft_artifacts, "_replace_projection", fail_plan_projection)
    with pytest.raises(OSError, match="注入投影失败"):
        DesignPlanService(paths).import_file("DRAFT-001", path.name)
    assert paths.events_file.read_bytes() == before
    assert design_plan_records(paths) == []
    assert not paths.draft_design_plan_file("DRAFT-001").exists()
    monkeypatch.setattr(draft_artifacts, "_replace_projection", original_replace)
    retried = _import(project, path)

    # 重试成功后只保留一条正式事件，编号仍从 001 开始。
    records = design_plan_records(paths)
    assert retried.returncode == 0, retried.stderr
    assert len(records) == 1
    assert records[0]["mapping"]["data-main"] == "DATA-001"
    assert len(
        [event for event in load_events(paths) if event["event_type"] == "draft_design_plan_imported"]
    ) == 1


def test_events_rebuild_json_evidence_and_visible_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    assert _import(
        project,
        _write_plan(project, _plan([_module("component-main", "component")])),
    ).returncode == 0
    expected = design_plan_records(paths)[0]
    paths.draft_design_plan_file("DRAFT-001").unlink()
    paths.draft_code_evidence_file("DRAFT-001").write_text('{"summary":"不可信"}\n', encoding="utf-8")
    paths.draft_design_plan_markdown_file("DRAFT-001").write_text(
        "# 人工改写\n\nPrompt 内容\n",
        encoding="utf-8",
    )

    rebuilt = rebuild_design_plan_projections(paths, "DRAFT-001")

    assert rebuilt == expected
    assert json.loads(paths.draft_design_plan_file("DRAFT-001").read_text(encoding="utf-8")) == expected
    assert json.loads(paths.draft_code_evidence_file("DRAFT-001").read_text(encoding="utf-8")) == expected["code_evidence"]
    display = paths.draft_design_plan_markdown_file("DRAFT-001").read_text(encoding="utf-8")
    assert "人工改写" not in display and "Prompt 内容" not in display
    assert "COMP-001" in display


def test_related_evidence_stales_only_plan_while_unrelated_changes_do_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    assert _import(
        project,
        _write_plan(project, _plan([_module("component-main", "component")])),
    ).returncode == 0
    record = design_plan_records(paths)[0]
    (project / "无关.txt").write_text("无关变化\n", encoding="utf-8")
    subprocess.run(["git", "add", "无关.txt"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "只改无关文件"], cwd=project, check=True)
    assert assess_design_plan(paths, record)["status"] == "current"

    (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assessment = assess_design_plan(paths, record)
    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["src/app.py"]


def test_record_prefix_dependency_and_hash_tampering_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _confirmed_project(tmp_path, monkeypatch)
    assert _import(
        project,
        _write_plan(project, _plan([_module("data-main", "data")])),
    ).returncode == 0
    record = design_plan_records(paths)[0]
    wrong_prefix = deepcopy(record)
    wrong_prefix["modules"][0]["module_id"] = "PAGE-001"
    wrong_prefix["mapping"]["data-main"] = "PAGE-001"
    with pytest.raises(SdlcError, match="前缀"):
        validate_design_plan_record(wrong_prefix)

    wrong_hash = deepcopy(record)
    wrong_hash["global_impact"] = ["被篡改"]
    with pytest.raises(SdlcError, match="记录哈希"):
        validate_design_plan_record(wrong_hash)
