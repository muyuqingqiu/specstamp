from __future__ import annotations

from pathlib import Path
import sys

from test_cli_v1 import init_demo_repo, run_cli
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.services.draft_service import DraftMutationService
from formal_package_factory import write_document_first_formal_v3_package

# 需求确认命令流复用阶段一真实审核夹具，不在 CLI 迁移测试里另造审核登记。
sys.path.insert(0, str(Path(__file__).resolve().parent / "contracts"))
from test_contract_cli_regressions import _ready_project as _ready_start_project
from test_requirement_review_flow import (
    _create,
    _ready_project as _ready_requirement_project,
    _submit,
)


def test_start_file_cannot_mark_stale_requirement_input_started(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir, paths, _package = _ready_start_project(tmp_path, monkeypatch)
    package_file, _package = write_document_first_formal_v3_package(project_dir)
    revised_source = project_dir / "课程访问需求修订.md"
    revised_source.write_text(
        "用户登录后可以查看课程，并显示输入已经更新。\n",
        encoding="utf-8",
    )
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
        cwd=project_dir,
    )
    assert revised.returncode == 0, revised.stderr
    stale = derive_state(paths)["drafts"]["DRAFT-001"]
    assert stale["status"] == "requirement_reviewing"
    assert stale["_requirement_confirmation_state"]["status"] == "stale"

    result = run_cli(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert "DRAFT-001" in result.stderr
    assert "start_ready" in result.stderr or "修订已经过期" in result.stderr
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "requirement_reviewing"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == ""
    assert state["requirements"] == {}


def test_start_file_does_not_interpret_nonempty_field_wording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir, paths, _package = _ready_start_project(tmp_path, monkeypatch)
    package_file, package = write_document_first_formal_v3_package(project_dir)
    # document-first 正式包只保存来源、审核和产物哈希，历史自由文本字段不再是建档输入。
    assert package["workflow_profile"] == "document-first.v1"
    assert not {
        "background",
        "goal",
        "functional_requirements",
        "acceptance_criteria",
        "test_cases",
        "design",
    }.intersection(package)

    result = run_cli(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "started"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == "REQ-001"
    assert "REQ-001" in state["requirements"]


def test_discuss_does_not_infer_requirement_state_from_free_text(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    requirement_text = "\n".join(
        [
            "# 需求草稿",
            "",
            "## 背景和目标",
            "订单导出只做单次导出。",
            "",
            "## 本轮范围",
            "- 单次订单列表导出。",
            "",
            "## 不做范围",
            "- 批量导出多个订单文件。",
            "",
            "## 权限规则",
            "- 只允许运营角色使用。",
            "",
            "## 验收标准",
            "- 非运营角色看不到导出入口。",
        ]
    )
    result = run_cli(["discuss", requirement_text], cwd=project_dir)

    assert result.returncode == 1
    assert "只能追加结构化 CAP" in result.stderr
    assert derive_state(build_paths(project_dir))["drafts"] == {}


def test_draft_status_projects_requirement_confirmation_without_entering_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, paths, _material = _ready_requirement_project(tmp_path)
    review = _create(paths, monkeypatch)
    _submit(paths, review["request"], monkeypatch)
    confirmed = DraftMutationService(
        paths,
        source="需求确认命令流测试",
    ).confirm_requirement(
        "DRAFT-001",
        review_id="REV-001",
        confirmed_at="2026-07-16T10:00:00Z",
    )
    assert confirmed["status"] == "requirement_confirmed"

    status = run_cli(["draft", "status", "DRAFT-001"], cwd=project)

    assert status.returncode == 0, status.stderr
    assert "requirement_confirmed" in status.stdout
    assert "设计阶段还缺少与该确认记录一致的技术方案引用" in status.stdout
    assert "design-reference-confirm DRAFT-001 DES-NNN" in status.stdout
    assert "$sdlc-start" not in status.stdout
