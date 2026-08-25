from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

# 复用真实需求确认、设计计划、模块产物和总体说明入口，避免用手工拼出的弱夹具
# 掩盖整体审核漏绑输入的问题。
TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import dependency_graph, fact_review_trust, review_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import project_lock
from codex_sdlc.core.state import derive_state, refresh_materialized_state
from codex_sdlc.core.structured_contract import canonical_sha256
from codex_sdlc.services import review_service
from test_cli_v1 import run_cli
from test_design_artifact_contract import (
    _artifact,
    _import_artifact,
    _project_with_plan,
    _write_artifact,
)
from test_design_plan_contract import _module
from test_design_summary_contract import (
    _import_summary,
    _prepared_project,
    _summary,
    _write_summary,
)


def _submission(
    request: dict[str, object],
    *,
    status: str = "passed",
) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    if status == "needs_fix":
        issues = [
            {
                "issue_id": "ISSUE-001",
                "severity": "P1",
                "title": "页面错误态与接口错误码没有统一",
                "description": "页面模块没有明确消费接口模块的 USER_NOT_FOUND。",
                "evidence_refs": ["PAGE-001#PG-001", "API-001#ERR-001"],
                "affected_refs": ["PAGE-001", "API-001"],
                "required_fix": "补齐页面错误态与接口错误码的显式引用。",
            }
        ]
    return {
        "schema_version": "review-result.v1",
        "review_id": request["review_id"],
        "stage": request["stage"],
        "owner_id": request["owner_id"],
        "reviewer_run_id": "提交文件不能覆盖真实任务身份",
        "input_hashes": deepcopy(request["input_hashes"]),
        "status": status,
        "issues": issues,
        "notes": [],
        "reviewed_at": "2026-07-17T11:00:00Z",
    }


def _run_review_cli(
    args: list[str],
    *,
    cwd: Path,
    thread_id: str,
) -> subprocess.CompletedProcess[str]:
    """直接执行公开 review_cmd 模块，避免把尚未注册该命令的总入口当成测试对象。"""

    env = os.environ.copy()
    env["CODEX_THREAD_ID"] = thread_id
    # 子进程工作目录是临时项目，必须传入仓库源码的绝对路径，否则相对
    # PYTHONPATH 会错误指向临时项目自己的 src 目录。
    source_root = str(TESTS_DIR.parent / "src")
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        source_root if not inherited else f"{source_root}{os.pathsep}{inherited}"
    )
    return subprocess.run(
        [sys.executable, "-m", "codex_sdlc.commands.review_cmd", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _create(paths, monkeypatch: pytest.MonkeyPatch, *, producer: str = "设计生产任务"):
    monkeypatch.setenv("CODEX_THREAD_ID", producer)
    return review_service.create_integrated_design_review(
        paths,
        draft_id="DRAFT-001",
        created_at="2026-07-17T10:00:00Z",
    )


def _submit(
    paths,
    request: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "passed",
    reviewer: str = "独立设计审核任务",
):
    monkeypatch.setenv("CODEX_THREAD_ID", reviewer)
    return review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status=status),
    )


def _snapshot_files(project: Path) -> list[Path]:
    return sorted(
        (project / ".codex-sdlc/drafts/DRAFT-001/质检").glob(
            "整体设计审核输入-*.json"
        )
    )


def _register_historical_pass(
    paths,
    request: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接走 T-007 可信登记模拟旧记录，避免用当前专用入口自动修好坏请求。"""

    with project_lock(paths):
        graph = dependency_graph.load_dependency_graph(paths)
        dependency_snapshot = dependency_graph.build_dependency_snapshot(
            paths,
            request,
            graph,
        )
        fact_review_trust.register_trusted_review_request_locked(
            paths,
            request=request,
            dependency_snapshot=dependency_snapshot,
        )
    monkeypatch.setenv("CODEX_THREAD_ID", "历史独立审核任务")
    with project_lock(paths):
        fact_review_trust.submit_trusted_review_result_locked(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request),
        )


def _single_module_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("component-main", "component")],
    )
    imported = _import_artifact(
        project,
        _write_artifact(
            project,
            _artifact("COMP-001", "component"),
            "组件设计.json",
        ),
    )
    assert imported.returncode == 0, imported.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"
    return project, paths


def _project_with_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    """多模块设计只有导入总体说明后才完整，避免测试绕过真实阶段门禁。"""

    project, paths = _prepared_project(tmp_path, monkeypatch)
    imported = _import_summary(
        project,
        _write_summary(project, _summary(), "总体设计.json"),
    )
    assert imported.returncode == 0, imported.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"
    return project, paths


def test_complete_input_binds_confirmed_requirement_material_design_and_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_summary(tmp_path, monkeypatch)
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"

    outcome = _create(paths, monkeypatch)
    request = outcome["request"]
    snapshot_path = next(
        path for path in request["input_paths"] if "整体设计审核输入-" in path
    )
    snapshot = json.loads((project / snapshot_path).read_text(encoding="utf-8"))

    assert outcome["action"] == "created"
    assert request["review_id"] == "REV-002"
    assert request["stage"] == "integrated_design"
    assert request["owner_id"] == "DRAFT-001"
    assert request["required_checks"] == list(
        review_service.INTEGRATED_DESIGN_REVIEW_CHECKS
    )
    assert snapshot["requirement_confirmation"]["confirmation_id"] == "RCF-001"
    assert [item["design_id"] for item in snapshot["confirmed_designs"]] == ["DES-001"]
    assert snapshot["design_plan"]["draft_id"] == "DRAFT-001"
    assert [item["artifact_id"] for item in snapshot["design_artifacts"]] == [
        "API-001",
        "COMP-001",
        "DATA-001",
        "PAGE-001",
        "SAFE-001",
    ]
    assert snapshot["design_summary"]["summary_id"] == "DSUM-001"
    assert snapshot["summary_status"] == "current"
    assert snapshot["code_evidence"]["purpose"] == "integrated_design"
    assert {item["material_id"] for item in snapshot["applicable_materials"]} >= {
        "MAT-001",
        "MAT-002",
    }
    assert all(
        len(digest) == 64
        for digest in request["input_hashes"].values()
    )
    assert any(path.endswith("需求/requirement-confirmation.v1.json") for path in request["input_paths"])
    assert any(path.endswith("设计/design-plan.v1.json") for path in request["input_paths"])
    assert any(path.endswith("设计/code-evidence.v1.json") for path in request["input_paths"])
    assert any(path.endswith("设计/design-summary.v1.json") for path in request["input_paths"])
    assert any(path.endswith("src/app.py") for path in request["input_paths"])
    assert all((project / path).is_file() for path in request["input_paths"])
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"
    assert not list(project.rglob("*formal.v3*"))

    retry = _create(paths, monkeypatch, producer="同输入重试任务")
    assert retry["action"] == "idempotent"
    assert retry["request"]["review_id"] == request["review_id"]
    assert len(_snapshot_files(project)) == 1


def test_public_review_command_uses_complete_builder_and_ignores_caller_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _single_module_project(tmp_path, monkeypatch)
    created_cli = _run_review_cli(
        [
            "review",
            "create",
            "--review-id",
            "REV-999",
            "--stage",
            "integrated_design",
            "--owner",
            "DRAFT-001",
            "--input",
            "src/app.py",
            "--check",
            "调用方试图缩减检查范围",
        ],
        cwd=project,
        thread_id="公开设计生产任务",
    )
    assert created_cli.returncode == 0, created_cli.stderr
    created = json.loads(created_cli.stdout)
    request = created["request"]
    assert request["review_id"] != "REV-999"
    assert request["required_checks"] == list(
        review_service.INTEGRATED_DESIGN_REVIEW_CHECKS
    )
    assert "调用方试图缩减检查范围" not in request["required_checks"]
    assert len(_snapshot_files(project)) == 1
    assert len(request["input_paths"]) > 1
    assert set(request["input_paths"]) != {"src/app.py"}

    # 直接调用通用服务也必须进入同一专用构建器；不存在的调用方路径若被
    # 错误求值会立即失败，因此这里同时固定“完全忽略”和幂等两项合同。
    monkeypatch.setenv("CODEX_THREAD_ID", "公开设计生产任务")
    repeated = review_service.create_review(
        paths,
        review_id="REV-998",
        stage="integrated_design",
        owner_id="DRAFT-001",
        input_paths=["调用方不存在的文件.json"],
        required_checks=["调用方污染检查项"],
    )
    assert repeated["action"] == "idempotent"
    assert repeated["request"] == request
    assert len(_snapshot_files(project)) == 1

    result_file = project / "整体设计审核结果.json"
    result_file.write_text(
        json.dumps(_submission(request), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    submitted_cli = _run_review_cli(
        [
            "review",
            "submit",
            "--request",
            str(request["review_id"]),
            "--file",
            str(result_file),
        ],
        cwd=project,
        thread_id="公开独立审核任务",
    )
    assert submitted_cli.returncode == 0, submitted_cli.stderr
    assert json.loads(submitted_cli.stdout)["effective_status"] == "passed"
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "start_ready"


def test_public_review_command_failure_leaves_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("field-main", "field", status="blocked")],
    )
    registry_path = paths.sdlc_dir / "trust/reviews/registry.json"
    before_registry = registry_path.read_bytes()
    before_events = paths.events_file.read_bytes()
    before_status = paths.draft_status_file("DRAFT-001").read_bytes()

    failed_cli = _run_review_cli(
        [
            "review",
            "create",
            "--review-id",
            "REV-999",
            "--stage",
            "integrated_design",
            "--owner",
            "DRAFT-001",
            "--input",
            "src/app.py",
        ],
        cwd=project,
        thread_id="公开失败设计任务",
    )
    assert failed_cli.returncode == 1
    assert "尚未满足整体设计审核前置条件" in failed_cli.stderr
    assert registry_path.read_bytes() == before_registry
    assert paths.events_file.read_bytes() == before_events
    assert paths.draft_status_file("DRAFT-001").read_bytes() == before_status
    assert not _snapshot_files(project)


@pytest.mark.parametrize(
    "damage",
    ["missing_snapshot", "wrong_checks", "incomplete_inputs"],
)
def test_historical_incomplete_integrated_reviews_never_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    project, paths = _single_module_project(tmp_path, monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "历史设计生产任务")
    if damage == "missing_snapshot":
        input_paths = ["src/app.py"]
        checks: list[str] = []
    else:
        projected = json.loads(
            paths.draft_status_file("DRAFT-001").read_text(encoding="utf-8")
        )
        review_input = review_service._validate_integrated_design_review_input(
            review_service._build_integrated_design_review_input(
                paths,
                projected,
            )
        )
        snapshot_path, _created = (
            review_service._write_integrated_design_review_input(
                paths,
                review_input,
            )
        )
        if damage == "wrong_checks":
            input_paths = [snapshot_path, *review_input["input_paths"]]
            checks = []
        else:
            input_paths = [snapshot_path, *review_input["input_paths"][:-1]]
            checks = list(review_service.INTEGRATED_DESIGN_REVIEW_CHECKS)
    request = review_contract.build_review_request(
        paths,
        review_id="REV-900",
        stage="integrated_design",
        owner_id="DRAFT-001",
        input_paths=input_paths,
        required_checks=checks,
        created_at="2026-07-17T12:00:00Z",
    )
    _register_historical_pass(paths, request, monkeypatch)

    status = review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )
    assert status["can_advance"] is False
    assert status["has_review_request"] is True
    assert status["reviews"][0]["recorded_status"] == "passed"
    assert status["reviews"][0]["effective_status"] == "stale"
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "design_reviewing"
    assert draft["assessment"]["can_start"] is False
    assert not list(project.rglob("*formal.v3*"))


def test_only_current_passed_review_advances_start_ready_and_keeps_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_root = tmp_path / "失败轮次"
    failed_root.mkdir()
    _failed_project, failed_paths = _single_module_project(
        failed_root,
        monkeypatch,
    )
    failed = _create(failed_paths, monkeypatch)
    _submit(
        failed_paths,
        failed["request"],
        monkeypatch,
        status="needs_fix",
    )
    failed_draft = derive_state(failed_paths)["drafts"]["DRAFT-001"]
    assert failed_draft["status"] == "design_reviewing"
    assert failed_draft["assessment"]["can_start"] is False
    assert failed_draft["_integrated_design_review_state"]["reviews"][0][
        "issues"
    ][0]["issue_id"] == "ISSUE-001"

    passed_root = tmp_path / "通过轮次"
    passed_root.mkdir()
    _passed_project, passed_paths = _single_module_project(
        passed_root,
        monkeypatch,
    )
    passed = _create(passed_paths, monkeypatch)
    _submit(passed_paths, passed["request"], monkeypatch)
    passed_draft = derive_state(passed_paths)["drafts"]["DRAFT-001"]
    assert passed_draft["status"] == "start_ready"
    assert passed_draft["assessment"]["can_start"] is True
    assert passed_draft["_integrated_design_review_state"]["can_advance"] is True

    reused = _create(passed_paths, monkeypatch, producer="通过后重试任务")
    assert reused["action"] == "reused"
    assert reused["request"]["review_id"] == passed["request"]["review_id"]


def test_input_changes_make_old_passed_stale_without_losing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_summary(tmp_path, monkeypatch)
    passed = _create(paths, monkeypatch)
    _submit(paths, passed["request"], monkeypatch)
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "start_ready"

    summary = _summary()
    summary["common_objects"][0]["definition"]["contract"] = (
        "用户实体统一由数据模块提供，接口层不得重新定义。"
    )
    summary["affected_modules"] = ["API-001", "DATA-001"]
    revised = _import_summary(
        project,
        _write_summary(project, summary, "总体设计修订.json"),
    )
    assert revised.returncode == 0, revised.stderr

    status = review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )
    old = status["reviews"][0]
    assert old["recorded_status"] == "passed"
    assert old["effective_status"] == "stale"
    assert old["can_advance"] is False
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "design_reviewing"

    current = _create(paths, monkeypatch, producer="总体设计修订任务")
    assert current["action"] == "created"
    assert current["request"]["review_id"] == "REV-003"
    _submit(paths, current["request"], monkeypatch)
    reviews = review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )["reviews"]
    assert [(item["review_id"], item["effective_status"]) for item in reviews] == [
        ("REV-002", "stale"),
        ("REV-003", "passed"),
    ]


def test_related_material_and_code_drift_stale_but_unrelated_file_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_summary(tmp_path, monkeypatch)
    passed = _create(paths, monkeypatch)
    _submit(paths, passed["request"], monkeypatch)

    unrelated = project / "src/unrelated.py"
    unrelated.write_text("# 与当前设计判断无关。\n", encoding="utf-8")
    assert review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )["reviews"][0]["effective_status"] == "passed"

    ui_source = project / "页面补充稿.md"
    ui_source.write_text("用户详情页必须覆盖空状态和无权限状态。\n", encoding="utf-8")
    material = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "ui-design",
            "--title",
            "页面补充稿",
            "--file",
            ui_source.name,
            "--scope",
            "PAGE-001",
        ],
        cwd=project,
    )
    assert material.returncode == 0, material.stderr
    assert review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )["reviews"][0]["effective_status"] == "stale"

    code_root = tmp_path / "代码漂移"
    code_root.mkdir()
    code_project, code_paths = _single_module_project(code_root, monkeypatch)
    code_passed = _create(code_paths, monkeypatch)
    _submit(code_paths, code_passed["request"], monkeypatch)
    (code_project / "src/app.py").write_text(
        "# 关联代码已经变化。\nVALUE = 2\n",
        encoding="utf-8",
    )
    code_status = review_service.integrated_design_review_status(
        code_paths,
        draft_id="DRAFT-001",
    )["reviews"][0]
    assert code_status["recorded_status"] == "passed"
    assert code_status["effective_status"] == "stale"
    code_draft = derive_state(code_paths)["drafts"]["DRAFT-001"]
    assert code_draft["status"] == "design_reviewing"
    assert {
        item["code"] for item in code_draft["assessment"]["blockers"]
    } >= {
        "design_plan_stale",
        "integrated_design_review_stale",
    }
    assert code_draft["assessment"]["next_action"].startswith(
        "codex-sdlc design-plan"
    )


@pytest.mark.parametrize(
    "drift",
    ["code_evidence", "design_plan", "design_artifact", "design_summary"],
)
def test_each_review_input_drift_keeps_design_reviewing_after_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    if drift == "design_summary":
        project, paths = _project_with_summary(tmp_path, monkeypatch)
    else:
        project, paths = _single_module_project(tmp_path, monkeypatch)
    outcome = _create(paths, monkeypatch)
    _submit(paths, outcome["request"], monkeypatch)
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "start_ready"

    if drift == "code_evidence":
        (project / "src/app.py").write_text(
            "# 关联实现已经变化。\nVALUE = 11\n",
            encoding="utf-8",
        )
    elif drift == "design_plan":
        plan_path = paths.draft_design_plan_file("DRAFT-001")
        plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
    elif drift == "design_artifact":
        revised = _artifact("COMP-001", "component")
        revised["content"]["components"][0]["responsibilities"] = [
            "显示用户主要信息",
            "明确展示读取失败状态",
        ]
        imported = _import_artifact(
            project,
            _write_artifact(project, revised, "组件设计修订.json"),
        )
        assert imported.returncode == 0, imported.stderr
    else:
        summary = _summary()
        summary["common_objects"][0]["definition"]["contract"] = (
            "用户实体统一由数据模块提供，接口模块只引用统一定义。"
        )
        summary["affected_modules"] = ["API-001", "DATA-001"]
        imported = _import_summary(
            project,
            _write_summary(project, summary, "总体设计修订.json"),
        )
        assert imported.returncode == 0, imported.stderr

    review = review_service.integrated_design_review_status(
        paths,
        draft_id="DRAFT-001",
    )["reviews"][0]
    assert review["recorded_status"] == "passed"
    assert review["effective_status"] == "stale"
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "design_reviewing"
    assert draft["assessment"]["can_start"] is False
    assert "integrated_design_review_stale" in {
        item["code"] for item in draft["assessment"]["blockers"]
    }


def test_incomplete_design_without_review_stays_designing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, paths = _project_with_plan(
        tmp_path,
        monkeypatch,
        [_module("component-main", "component")],
    )

    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "designing"
    assert draft["_integrated_design_review_state"]["has_review_request"] is False
    assert "design_artifact_missing" in {
        item["code"] for item in draft["assessment"]["blockers"]
    }


def test_blockers_hash_identity_and_publish_failure_leave_no_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_root = tmp_path / "阻塞模块"
    blocked_root.mkdir()
    blocked_project, blocked_paths = _project_with_plan(
        blocked_root,
        monkeypatch,
        [_module("field-main", "field", status="blocked")],
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "阻塞检查任务")
    with pytest.raises(SdlcError, match="尚未满足整体设计审核前置条件"):
        review_service.create_integrated_design_review(
            blocked_paths,
            draft_id="DRAFT-001",
        )
    assert not _snapshot_files(blocked_project)

    open_root = tmp_path / "未清零问题"
    open_root.mkdir()
    open_project, open_paths = _single_module_project(open_root, monkeypatch)
    real_open_builder = review_service._build_integrated_design_review_input

    def unresolved_question_builder(*args, **kwargs):
        document = real_open_builder(*args, **kwargs)
        document["design_artifacts"][0]["open_questions"] = [
            "组件是否允许匿名访问？"
        ]
        body = {
            key: value
            for key, value in document.items()
            if key != "snapshot_sha256"
        }
        document["snapshot_sha256"] = canonical_sha256(body)
        return document

    with monkeypatch.context() as open_patch:
        open_patch.setenv("CODEX_THREAD_ID", "问题检查任务")
        open_patch.setattr(
            review_service,
            "_build_integrated_design_review_input",
            unresolved_question_builder,
        )
        with pytest.raises(SdlcError, match="仍有待确认问题"):
            review_service.create_integrated_design_review(
                open_paths,
                draft_id="DRAFT-001",
            )
    assert not _snapshot_files(open_project)

    hash_root = tmp_path / "哈希不完整"
    hash_root.mkdir()
    hash_project, hash_paths = _single_module_project(hash_root, monkeypatch)
    real_builder = review_service._build_integrated_design_review_input

    def incomplete_hash_builder(*args, **kwargs):
        document = real_builder(*args, **kwargs)
        first_path = document["input_paths"][0]
        document["input_hashes"][first_path] = "0" * 63
        body = {
            key: value
            for key, value in document.items()
            if key != "snapshot_sha256"
        }
        document["snapshot_sha256"] = canonical_sha256(body)
        return document

    with monkeypatch.context() as hash_patch:
        hash_patch.setenv("CODEX_THREAD_ID", "哈希检查任务")
        hash_patch.setattr(
            review_service,
            "_build_integrated_design_review_input",
            incomplete_hash_builder,
        )
        with pytest.raises(SdlcError, match="哈希必须完整"):
            review_service.create_integrated_design_review(
                hash_paths,
                draft_id="DRAFT-001",
            )
    assert not _snapshot_files(hash_project)

    identity_root = tmp_path / "身份与发布失败"
    identity_root.mkdir()
    identity_project, identity_paths = _single_module_project(
        identity_root,
        monkeypatch,
    )
    identity_registry = (
        identity_paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    )
    registry_before = identity_registry.read_bytes()
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SdlcError, match="CODEX_THREAD_ID"):
        review_service.create_integrated_design_review(
            identity_paths,
            draft_id="DRAFT-001",
        )
    assert not _snapshot_files(identity_project)
    assert identity_registry.read_bytes() == registry_before

    monkeypatch.setenv("CODEX_THREAD_ID", "发布失败任务")

    def fail_publish(*_args, **_kwargs):
        raise OSError("模拟整体设计审核登记发布失败")

    monkeypatch.setattr(
        fact_review_trust,
        "register_trusted_review_request_locked",
        fail_publish,
    )
    with pytest.raises(OSError, match="模拟整体设计审核登记发布失败"):
        review_service.create_integrated_design_review(
            identity_paths,
            draft_id="DRAFT-001",
        )
    assert not _snapshot_files(identity_project)
    assert identity_registry.read_bytes() == registry_before


def test_same_thread_and_stale_submission_cannot_register_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _single_module_project(tmp_path, monkeypatch)
    outcome = _create(paths, monkeypatch, producer="设计生产任务")
    request = outcome["request"]
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    before = registry_path.read_bytes()

    monkeypatch.setenv("CODEX_THREAD_ID", "设计生产任务")
    with pytest.raises(SdlcError, match="必须使用不同"):
        review_service.submit_review(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request),
        )
    assert registry_path.read_bytes() == before

    (project / "src/app.py").write_text(
        "# 审核期间关联代码发生变化。\nVALUE = 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "独立设计审核任务")
    with pytest.raises(SdlcError, match="已经失效"):
        review_service.submit_review(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request),
        )
    assert registry_path.read_bytes() == before


def test_event_rebuild_restores_current_passed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _single_module_project(tmp_path, monkeypatch)
    outcome = _create(paths, monkeypatch)
    _submit(paths, outcome["request"], monkeypatch)
    refresh_materialized_state(paths)
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "start_ready"

    status_file = paths.draft_status_file("DRAFT-001")
    module_file = project / ".codex-sdlc/drafts/DRAFT-001/设计/component-main_COMP-001.design-artifact.v1.json"
    status_file.unlink()
    module_file.unlink()
    refresh_materialized_state(paths)
    refresh_materialized_state(paths)

    rebuilt = derive_state(paths)["drafts"]["DRAFT-001"]
    assert rebuilt["status"] == "start_ready"
    assert module_file.is_file()
    assert rebuilt["_integrated_design_review_state"]["reviews"][0][
        "effective_status"
    ] == "passed"
