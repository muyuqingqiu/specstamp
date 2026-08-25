from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from codex_sdlc.core.formal_manifest_contract import (
    build_document_first_formal_package,
)
from codex_sdlc.core.state import derive_state, refresh_materialized_state
from codex_sdlc.services.draft_service import DraftMutationService
from test_cli_v1 import SDLC_BIN
from test_cli_v1 import run_cli
from test_cli_v17_draft_contract import (
    create_draft_with_material,
    import_command,
    requirement_documents,
    write_documents,
)
from test_contract_cli_regressions import _write_package
from test_design_artifact_contract import (
    _artifact,
    _import_artifact,
    _write_artifact,
)
from test_design_plan_contract import (
    _import as _import_design_plan,
    _module,
    _plan,
    _write_plan,
)
from test_design_reference_contract import (
    import_reference,
    write_design_reference,
)
from test_design_summary_contract import (
    _import_summary,
    _summary,
    _write_summary,
)
from test_integrated_design_review_flow import (
    _create as _create_design_review,
    _submit as _submit_design_review,
)
from test_requirement_review_flow import (
    _create as _create_requirement_review,
    _submit as _submit_requirement_review,
)
from test_task_planning_code_evidence import _write_task_submission


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FORMAL_CLI = Path(SDLC_BIN).resolve()
RUN_ID = os.environ.get("T043_V2_RUN_ID") or (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    + f"-{os.getpid()}"
)
PROJECT_SPECS = {
    "完整项目": {
        "files": {
            "backend/service.py": "def load_profile():\n    return {'name': '完整项目'}\n",
            "frontend/App.vue": "<template><main>完整项目</main></template>\n",
            "database/schema.sql": "CREATE TABLE profile(id INTEGER PRIMARY KEY);\n",
        },
        "goal": "同时交付服务、页面和数据结构的任务运行证据。",
        "requirement": "服务、页面和数据结构都必须进入任务读取范围。",
        "modules": ("data", "api", "page", "component", "security"),
        "design_refs": ("API-001", "COMP-001", "DATA-001", "PAGE-001", "SAFE-001"),
        "material_refs": ("MAT-001", "MAT-002"),
    },
    "既有界面": {
        "files": {
            "web/components/Profile.vue": (
                "<template><section class=\"profile\">既有界面</section></template>\n"
            )
        },
        "goal": "在既有界面文件上完成受控任务运行。",
        "requirement": "只能修改既有 Profile 组件，不新建第二套页面。",
        "modules": ("page",),
        "design_refs": ("PAGE-001",),
        "material_refs": ("MAT-002",),
    },
    "数据接口": {
        "files": {
            "api/openapi.yaml": (
                "openapi: 3.0.0\ninfo:\n  title: Profile API\n  version: 1.0.0\n"
            )
        },
        "goal": "交付数据接口定义的受控任务运行证据。",
        "requirement": "接口路径、输入和输出必须由同一份 OpenAPI 文件承接。",
        "modules": ("data", "api"),
        "design_refs": ("API-001", "DATA-001"),
        "material_refs": ("MAT-002",),
    },
    "纯前端": {
        "files": {
            "frontend/index.html": (
                "<!doctype html><html><body><main>纯前端页面</main></body></html>\n"
            )
        },
        "goal": "在没有新增服务端文件的前提下完成前端任务运行。",
        "requirement": "交付范围只包含浏览器可直接读取的前端文件。",
        "modules": ("page", "component"),
        "design_refs": ("COMP-001", "PAGE-001"),
        "material_refs": ("MAT-002",),
    },
    "小修改": {
        "files": {"config/feature.json": '{"profile_enabled": true}\n'},
        "goal": "只修改一个配置文件并保留完整任务门禁。",
        "requirement": "小修改不能跳过任务计划、独立审核和读取确认。",
        "modules": ("component",),
        "design_refs": ("COMP-001",),
        "material_refs": ("MAT-001",),
    },
    "输入变化恢复": {
        "files": {"src/app.py": "VALUE = 2\n"},
        "goal": "证明任务输入失效后恢复原值可以重新通过运行检查。",
        "requirement": "项目规则变化必须使当前轮次失效，恢复原值后必须恢复有效。",
        "modules": ("component", "security"),
        "design_refs": ("COMP-001", "SAFE-001"),
        "material_refs": ("MAT-001", "MAT-002"),
    },
}

MODULE_IDS = {
    "api": "API-001",
    "component": "COMP-001",
    "data": "DATA-001",
    "page": "PAGE-001",
    "security": "SAFE-001",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run(
    project: Path,
    *,
    args: list[str],
    thread_id: str,
    records: list[dict[str, object]],
    expected: int,
) -> subprocess.CompletedProcess[str]:
    """所有被验证的业务动作都从正式 CLI 进程进入，并保存原始输出。"""

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPOSITORY_ROOT / "src"),
            str(REPOSITORY_ROOT / "tests"),
            str(REPOSITORY_ROOT / "tests/contracts"),
        ]
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    env["CODEX_THREAD_ID"] = thread_id
    command = [str(FORMAL_CLI), *args]
    completed = subprocess.run(
        command,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    records.append(
        {
            "command": command,
            "cwd": str(project),
            "thread_id": thread_id,
            "exit_code": int(completed.returncode),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    assert completed.returncode == expected
    return completed


def _git(project: Path, *args: str) -> dict[str, object]:
    completed = subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    return {
        "command": ["git", *args],
        "exit_code": int(completed.returncode),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _prepare_kind_files(
    project: Path,
    spec: dict[str, object],
) -> tuple[list[str], list[dict[str, object]]]:
    paths: list[str] = []
    for relative, content in dict(spec["files"]).items():
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        paths.append(relative)
    git_records = [
        _git(project, "add", "--", *paths),
        _git(project, "commit", "-m", "建立六类V2项目业务基线"),
    ]
    return paths, git_records


def _scenario_summary(module_types: tuple[str, ...]) -> dict[str, object]:
    """总体说明只保留当前项目真实存在的跨模块关系。"""

    module_ids = {MODULE_IDS[module_type] for module_type in module_types}
    if module_ids == {"API-001", "COMP-001", "DATA-001", "PAGE-001", "SAFE-001"}:
        # 完整项目继续使用覆盖五类模块真实关系的完整总体说明。
        return _summary()
    if module_ids == {"API-001", "DATA-001"}:
        common_objects = [
            item
            for item in _summary()["common_objects"]
            if item["business_id"]
            in {"COMMON-001", "COMMON-002", "COMMON-003", "COMMON-005", "COMMON-011"}
        ]
    elif {"COMP-001", "PAGE-001"}.issubset(module_ids):
        common_objects = [
            item
            for item in _summary()["common_objects"]
            if item["business_id"] == "COMMON-010"
        ]
    elif module_ids == {"COMP-001", "SAFE-001"}:
        common_objects = [
            {
                "business_id": "COMMON-001",
                "object_type": "component",
                "source_refs": ["COMP-001#CM-001"],
                "applies_to_modules": ["COMP-001", "SAFE-001"],
                "definition": {
                    "canonical_name": "受保护的任务状态组件",
                    "contract": "组件显示的任务状态必须服从安全控制和输入失效规则。",
                },
            }
        ]
    else:
        raise AssertionError(f"没有定义总体设计组合：{sorted(module_ids)}")
    return {
        "schema_version": "design-summary.v1",
        "draft_id": "DRAFT-001",
        "common_objects": common_objects,
        "affected_modules": sorted(module_ids),
        "open_questions": [],
    }


def _scenario_ready_project(
    case_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    kind: str,
    spec: dict[str, object],
) -> tuple[Path, object, dict[str, object]]:
    """为每类业务建立自己的需求、资料和适用设计，不复用统一正式包。"""

    project, paths, requirement_material = create_draft_with_material(
        case_root,
        draft_title=f"{kind}交付需求",
    )
    # 技术资料必须先归档再审核需求，否则新增适用资料会按正式规则使需求确认失效。
    solution = (
        f"# {kind}技术方案\n\n"
        "## 适用模块\n\n"
        f"{'、'.join(str(item) for item in spec['modules'])}\n\n"
        "## 资料约束\n\n"
        f"{spec['requirement']}\n"
    )
    solution_path = project / f"{kind}技术方案.md"
    solution_path.write_text(solution, encoding="utf-8")
    archived = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "technical-solution",
            "--title",
            f"{kind}技术方案",
            "--file",
            solution_path.name,
        ],
        cwd=project,
    )
    assert archived.returncode == 0, archived.stderr

    suffix = f"t043-{tuple(PROJECT_SPECS).index(kind) + 1}"
    split, coverage = requirement_documents(
        project,
        requirement_material,
        suffix=suffix,
        long_description=str(spec["requirement"]),
    )
    split.update(
        {
            "title": f"{kind}交付需求",
            "background": f"{kind}项目需要通过正式文档优先流程交付。",
            "goal": str(spec["goal"]),
            "scope": [str(spec["requirement"])],
            "out_of_scope": [f"{kind}范围以外的模块"],
            "user_scenarios": [f"开发人员按{kind}业务范围执行任务"],
        }
    )
    requirement = split["functional_requirements"][0]
    requirement["title"] = f"{kind}业务交付"
    requirement["description"] = str(spec["requirement"])
    requirement["elements"] = [f"{kind}业务产物"]
    requirement["flow"] = ["读取正式需求", "完成适用范围实现", "核对正式产物"]
    requirement["rules"] = [str(spec["requirement"])]
    requirement["constraints"] = [f"不得加入{kind}范围以外的模块"]
    requirement["out_of_scope"] = [f"{kind}范围以外的模块"]
    requirement["acceptance_criteria"][0].update(
        {
            "operation": f"执行{kind}正式任务链",
            "expected": str(spec["goal"]),
            "pass_standard": f"{kind}适用产物、状态和哈希均可复核",
        }
    )
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    requirement_review = _create_requirement_review(paths, monkeypatch)
    _submit_requirement_review(paths, requirement_review["request"], monkeypatch)
    confirmed = DraftMutationService(
        paths,
        source="T-043 六类真实 CLI V2",
    ).confirm_requirement(
        "DRAFT-001",
        review_id=str(requirement_review["request"]["review_id"]),
        confirmed_at="2026-07-25T00:00:00Z",
    )
    assert confirmed["status"] == "requirement_confirmed"

    reference_path = write_design_reference(
        project,
        source_text=solution,
        display_name=f"{kind}技术方案",
        anchor_display_name="适用模块",
        line_start=3,
        line_end=5,
        display_heading="适用模块",
    )
    assert import_reference(project, reference_path).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0

    (project / "AGENTS.md").write_text("# 项目规则\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "src").mkdir(exist_ok=True)
    (project / "src/app.py").write_text(
        f"# {kind}设计计划使用的真实代码证据。\\nVALUE = 1\\n",
        encoding="utf-8",
    )
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "config", "user.name", "合同测试")
    _git(project, "add", ".")
    _git(project, "commit", "-m", f"建立{kind}正式设计基线")

    module_types = tuple(str(item) for item in spec["modules"])
    modules = []
    for module_type in module_types:
        depends_on: list[str] = []
        if module_type == "api" and "data" in module_types:
            depends_on = ["@client:data-main"]
        elif module_type == "page" and "api" in module_types:
            depends_on = ["@client:api-main"]
        modules.append(_module(f"{module_type}-main", module_type, depends_on=depends_on))
    imported_plan = _import_design_plan(
        project,
        _write_plan(project, _plan(modules)),
    )
    assert imported_plan.returncode == 0, imported_plan.stderr

    for module_type in module_types:
        artifact_id = MODULE_IDS[module_type]
        depends_on = (
            ["DATA-001"]
            if module_type == "api" and "data" in module_types
            else ["API-001"]
            if module_type == "page" and "api" in module_types
            else []
        )
        document = _artifact(artifact_id, module_type, depends_on=depends_on)
        if module_type == "page" and "api" not in module_types:
            document["content"]["pages"][0]["elements"][0]["data_source_refs"] = []
        imported_artifact = _import_artifact(
            project,
            _write_artifact(project, document, f"{kind}-{module_type}.json"),
        )
        assert imported_artifact.returncode == 0, imported_artifact.stderr

    if len(module_types) >= 2:
        imported_summary = _import_summary(
            project,
            _write_summary(
                project,
                _scenario_summary(module_types),
                f"{kind}总体设计.json",
            ),
        )
        assert imported_summary.returncode == 0, imported_summary.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"
    design_review = _create_design_review(paths, monkeypatch)
    _submit_design_review(paths, design_review["request"], monkeypatch)
    refresh_materialized_state(paths)
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "start_ready"
    return project, paths, build_document_first_formal_package(paths, "DRAFT-001")


def _prepare_task_input(
    source_root: Path,
    requirement_root: Path,
    *,
    kind: str,
    spec: dict[str, object],
    business_paths: list[str],
) -> tuple[Path, Path, Path]:
    plan_file, tasks_dir, coverage_file = _write_task_submission(
        source_root,
        requirement_root,
    )
    task_path = tasks_dir / "main.task.v2.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["title"] = f"{kind}真实CLI任务"
    task["goal"] = str(spec["goal"])
    task["deliverables"] = [str(spec["requirement"])]
    design_refs = [str(item) for item in spec["design_refs"]]
    task["design_refs"] = design_refs
    task["material_refs"] = [str(item) for item in spec["material_refs"]]
    task["code_scope"]["read_paths"] = business_paths
    task["code_scope"]["likely_change_paths"] = business_paths
    task["implementation_requirements"] = [str(spec["requirement"])]
    task["data_api_page_component_requirements"] = [str(spec["requirement"])]
    task["automated_tests"] = [f"复核 {kind} 正式 CLI 运行证据。"]
    task["manual_checks"] = [f"核对 {kind} 业务产物哈希。"]
    _write_json(task_path, task)

    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    plan["code_evidence"] = {
        "purpose": "task_planning",
        "rules": ["AGENTS.md"],
        "dependencies": ["package-lock.json"],
        "code_files": [
            {"path": path, "reason_ref": "FR-001"}
            for path in business_paths
        ],
        "upstream_outputs": [
            requirement_root.relative_to(requirement_root.parents[2])
            .joinpath(relative)
            .as_posix()
            for relative in ("reference-index.v1.json", "original/formal.v3.json")
        ],
    }
    _write_json(plan_file, plan)

    coverage = json.loads(coverage_file.read_text(encoding="utf-8"))
    coverage["design_artifacts"] = {
        design_id: {"tasks": ["@client:main"]}
        for design_id in design_refs
    }
    _write_json(coverage_file, coverage)
    return plan_file, tasks_dir, coverage_file


def _current_task_review(project: Path) -> dict[str, object]:
    registry = json.loads(
        (project / ".codex-sdlc/trust/reviews/registry.json").read_text(
            encoding="utf-8"
        )
    )
    matches = [
        record["request"]
        for record in registry["requests"].values()
        if record["request"]["stage"] == "task_plan"
        and record["request"]["owner_id"] == "REQ-001"
        and record["request"]["status"] == "pending"
    ]
    assert matches
    return max(matches, key=lambda item: str(item["created_at"]))


def _copy_artifacts(
    evidence_dir: Path,
    *,
    project: Path,
    requirement_root: Path,
    review_result: Path,
) -> list[dict[str, object]]:
    sources = {
        "formal.v3.json": requirement_root / "original/formal.v3.json",
        "requirement.current.json": (
            requirement_root / "effective/requirement.current.json"
        ),
        "design.current.json": requirement_root / "effective/design.current.json",
        "task-plan.v2.json": requirement_root / "tasks/task-plan.v2.json",
        "T-001.json": requirement_root / "tasks/T-001.json",
        "review-result.v1.json": review_result,
        "task-run.v1.json": (
            requirement_root / "runtime/T-001/runs/0001/task-run.v1.json"
        ),
        "task-read-manifest.v1.json": (
            requirement_root
            / "runtime/T-001/runs/0001/task-read-manifest.v1.json"
        ),
        "current.json": requirement_root / "runtime/T-001/current.json",
        "events.jsonl": project / ".codex-sdlc/events.jsonl",
    }
    output: list[dict[str, object]] = []
    artifact_root = evidence_dir / "产物"
    artifact_root.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        assert source.is_file()
        target = artifact_root / name
        shutil.copy2(source, target)
        assert _sha256(source) == _sha256(target)
        output.append(
            {
                "name": name,
                "source_path": str(source),
                "evidence_path": str(target),
                "sha256": _sha256(target),
                "bytes": target.stat().st_size,
            }
        )
    return output


@pytest.mark.parametrize("kind", tuple(PROJECT_SPECS))
def test_document_first_to_task_run_covers_six_project_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    spec = PROJECT_SPECS[kind]
    case_root = tmp_path / kind
    case_root.mkdir()
    project, paths, package = _scenario_ready_project(
        case_root,
        monkeypatch,
        kind=kind,
        spec=spec,
    )
    assert FORMAL_CLI == (REPOSITORY_ROOT / "bin" / "codex-sdlc").resolve()

    evidence_root = Path(
        os.environ.get("T043_V2_EVIDENCE_ROOT", str(tmp_path / "六类V2证据"))
    )
    evidence_dir = evidence_root / RUN_ID / kind
    evidence_dir.mkdir(parents=True, exist_ok=True)
    commands: list[dict[str, object]] = []
    setup: list[dict[str, object]] = []

    package_file = case_root / "formal.v3.json"
    _write_package(package_file, package)
    _run(
        project,
        args=["start", "--file", str(package_file)],
        thread_id=f"T043-V2-{kind}-建档",
        records=commands,
        expected=0,
    )
    requirement_root = paths.requirements_dir / "REQ-001"
    assert (requirement_root / "original/formal.v3.json").is_file()
    assert (requirement_root / "effective/requirement.current.json").is_file()
    assert (requirement_root / "effective/design.current.json").is_file()

    business_paths, git_records = _prepare_kind_files(project, spec)
    setup.extend(git_records)
    submission = _prepare_task_input(
        case_root / "任务输入",
        requirement_root,
        kind=kind,
        spec=spec,
        business_paths=business_paths,
    )
    _run(
        project,
        args=[
            "tasks",
            "REQ-001",
            "--plan-file",
            str(submission[0]),
            "--tasks-dir",
            str(submission[1]),
            "--coverage-file",
            str(submission[2]),
        ],
        thread_id=f"T043-V2-{kind}-任务规划",
        records=commands,
        expected=0,
    )

    events_path = project / ".codex-sdlc/events.jsonl"
    events_before_rejection = _sha256(events_path)
    runtime_root = requirement_root / "runtime/T-001"
    _run(
        project,
        args=["task", "REQ-001", "T-001"],
        thread_id=f"T043-V2-{kind}-开发",
        records=commands,
        expected=1,
    )
    pre_review_failure = {
        "exit_code": 1,
        "events_unchanged": _sha256(events_path) == events_before_rejection,
        "runtime_absent": not runtime_root.exists(),
    }
    assert all(pre_review_failure.values())

    plan_relative = (
        requirement_root / "tasks/task-plan.v2.json"
    ).relative_to(project).as_posix()
    _run(
        project,
        args=[
            "review",
            "create",
            "--review-id",
            "REV-001",
            "--stage",
            "task_plan",
            "--owner",
            "REQ-001",
            "--input",
            plan_relative,
        ],
        thread_id=f"T043-V2-{kind}-任务规划",
        records=commands,
        expected=0,
    )
    request = _current_task_review(project)
    review_result = case_root / "review-result.v1.json"
    _write_json(
        review_result,
        {
            "schema_version": "review-result.v1",
            "review_id": request["review_id"],
            "stage": "task_plan",
            "owner_id": "REQ-001",
            "reviewer_run_id": "正式入口会绑定真实审核线程",
            "input_hashes": request["input_hashes"],
            "status": "passed",
            "issues": [],
            "notes": [f"{kind}任务计划通过独立审核。"],
            "reviewed_at": "2026-07-24T00:00:00+08:00",
        },
    )
    _run(
        project,
        args=[
            "review",
            "submit",
            "--request",
            str(request["review_id"]),
            "--file",
            str(review_result),
        ],
        thread_id=f"T043-V2-{kind}-独立审核",
        records=commands,
        expected=0,
    )
    _run(
        project,
        args=["task", "REQ-001", "T-001"],
        thread_id=f"T043-V2-{kind}-开发",
        records=commands,
        expected=0,
    )
    manifest_path = (
        requirement_root
        / "runtime/T-001/runs/0001/task-read-manifest.v1.json"
    )
    _run(
        project,
        args=[
            "task-read-confirm",
            "REQ-001",
            "T-001",
            "--manifest-sha256",
            _sha256(manifest_path),
        ],
        thread_id=f"T043-V2-{kind}-开发",
        records=commands,
        expected=0,
    )
    _run(
        project,
        args=["task-run-check", "REQ-001", "T-001"],
        thread_id=f"T043-V2-{kind}-开发",
        records=commands,
        expected=0,
    )

    input_recovery: dict[str, object] = {"applicable": False}
    if kind == "输入变化恢复":
        rules_path = project / "AGENTS.md"
        original_rules = rules_path.read_bytes()
        rules_path.write_bytes(original_rules + "输入变化必须使任务轮次失效。\n".encode())
        _run(
            project,
            args=["task-run-check", "REQ-001", "T-001"],
            thread_id=f"T043-V2-{kind}-开发",
            records=commands,
            expected=1,
        )
        current_path = requirement_root / "runtime/T-001/current.json"
        stale_status = json.loads(current_path.read_text(encoding="utf-8"))["status"]
        rules_path.write_bytes(original_rules)
        _run(
            project,
            args=["task-run-check", "REQ-001", "T-001"],
            thread_id=f"T043-V2-{kind}-开发",
            records=commands,
            expected=0,
        )
        recovered_status = json.loads(
            current_path.read_text(encoding="utf-8")
        )["status"]
        input_recovery = {
            "applicable": True,
            "stale_exit_code": 1,
            "stale_status": stale_status,
            "restored_rules_sha256": _sha256(rules_path),
            "original_rules_sha256": _bytes_sha256(original_rules),
            "recovery_exit_code": 0,
            "recovered_status": recovered_status,
        }
        assert stale_status == "stale"
        assert recovered_status == "active"
        assert input_recovery["restored_rules_sha256"] == input_recovery[
            "original_rules_sha256"
        ]

    status = _run(
        project,
        args=["status"],
        thread_id=f"T043-V2-{kind}-开发",
        records=commands,
        expected=0,
    )
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(
            encoding="utf-8"
        )
    )
    assert current["status"] == "active"
    assert "REQ-001" in status.stdout

    active_residuals = [
        str(path.relative_to(project))
        for path in project.rglob("*")
        if path.name
        in {
            ".task-start-transaction.json",
            ".restore-transaction.json",
            ".task-evidence-transaction.json",
        }
    ]
    assert active_residuals == []
    artifacts = _copy_artifacts(
        evidence_dir,
        project=project,
        requirement_root=requirement_root,
        review_result=review_result,
    )
    evidence = {
        "schema_version": "t043-six-project-cli-v2.v1",
        "project_kind": kind,
        "formal_cli": str(FORMAL_CLI),
        "project_path": str(project),
        "business_paths": business_paths,
        "git_setup": setup,
        "commands": commands,
        "pre_review_failure": pre_review_failure,
        "input_recovery": input_recovery,
        "final_status": current,
        "artifacts": artifacts,
        "active_transaction_residuals": active_residuals,
        "cleanup": {"project_removed": False, "task_input_removed": False},
    }
    evidence_file = evidence_dir / "验证证据.json"
    _write_json(evidence_file, evidence)

    monkeypatch.chdir(REPOSITORY_ROOT)
    shutil.rmtree(project)
    shutil.rmtree(case_root / "任务输入")
    evidence["cleanup"] = {
        "project_removed": not project.exists(),
        "task_input_removed": not (case_root / "任务输入").exists(),
    }
    assert all(evidence["cleanup"].values())
    _write_json(evidence_file, evidence)
    (evidence_dir / "验证证据.sha256").write_text(
        _sha256(evidence_file) + "  验证证据.json\n",
        encoding="utf-8",
    )
