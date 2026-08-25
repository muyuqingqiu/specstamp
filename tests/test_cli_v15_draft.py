from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "contracts"))

from test_cli_v1 import create_minimal_requirement_by_start_file, init_demo_repo, run_cli, run_cli_raw
from codex_sdlc.core import draft_lifecycle
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state, refresh_materialized_state
from codex_sdlc.core.structured_contract import sha256_file
from formal_package_factory import (
    formal_business_from_draft,
    install_valid_draft_facts,
    write_document_first_formal_v3_package,
    write_formal_v3_from_draft,
    write_formal_v3_package,
)
from test_contract_cli_regressions import _ready_project
from test_cli_v6_discuss_prepare import append_structured_cap, reference


@pytest.fixture(scope="module")
def document_first_ready_template(tmp_path_factory: pytest.TempPathFactory):
    """完整审核只跑一次，五个迁移用例每次从同一路径恢复，避免重复慢速建档准备。"""

    root = tmp_path_factory.mktemp("t044-document-first")
    patcher = pytest.MonkeyPatch()
    try:
        project_dir, _paths, _package = _ready_project(root, patcher)
    finally:
        patcher.undo()
    baseline = root / "文档优先开工基准"
    shutil.copytree(project_dir, baseline)
    return project_dir, baseline


@pytest.fixture
def document_first_ready_project(document_first_ready_template):
    """恢复到审核完成但尚未建档的真实项目，项目绝对路径保持不变。"""

    project_dir, baseline = document_first_ready_template
    if project_dir.exists():
        shutil.rmtree(project_dir)
    shutil.copytree(baseline, project_dir)
    return project_dir, build_paths(project_dir)


def start_document_first_ready_project(project_dir: Path, paths):
    """统一走真实 document-first 包，避免迁移用例重新落回 facts 便利入口。"""

    package_file, package = write_document_first_formal_v3_package(project_dir)
    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)
    assert result.returncode == 0, result.stderr
    requirement_dir = paths.requirements_dir / "REQ-001"
    assert requirement_dir.is_dir()
    return result, package, requirement_dir


def read_effective_json(requirement_dir: Path, name: str) -> dict[str, object]:
    return json.loads(
        (requirement_dir / "effective" / name).read_text(encoding="utf-8")
    )


def formal_source_path(
    project_dir: Path,
    package: dict[str, object],
    *,
    artifact_type: str,
    business_id: str | None = None,
) -> Path:
    """从系统生成的正式清单定位来源，避免测试重新硬编码旧目录或展示文件。"""

    matches = [
        item
        for item in package["artifact_manifest"]
        if item["artifact_type"] == artifact_type
        and (business_id is None or item["business_id"] == business_id)
    ]
    assert len(matches) == 1
    # 正式清单路径以 DRAFT 根目录为基准，按相同合同定位才能改到真实受审来源。
    return (
        project_dir
        / ".codex-sdlc"
        / "drafts"
        / package["source_draft_id"]
        / matches[0]["source_path"]
    )


def assert_document_first_start_rejects_changed_source(
    project_dir: Path,
    paths,
    *,
    source_path: Path,
    mutate,
):
    """先生成审核绑定的正式包，再改变真实来源，证明旧包不能绕过哈希门禁。"""

    package_file, _package = write_document_first_formal_v3_package(project_dir)
    before_events = paths.events_file.read_bytes()
    mutate(source_path)
    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)
    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
    return result


def overwrite_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_structured_discussion(
    project_dir: Path,
    *,
    increment: str,
    capture_type: str = "fact",
    submission_key: str = "v15-migrated-discussion",
    requirement_body: str = "",
):
    """旧测试改走结构化 CAP，避免重新开放自然语言 discuss 生产入口。"""

    created = run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir)
    assert created.returncode == 0, created.stderr
    if requirement_body:
        updated = run_cli(
            ["draft", "requirement", "DRAFT-001", requirement_body],
            cwd=project_dir,
        )
        assert updated.returncode == 0, updated.stderr
    result = append_structured_cap(
        project_dir,
        submission_key=submission_key,
        capture_type=capture_type,
        increment=increment,
    )
    assert result.returncode == 0, result.stderr
    return result


def append_structured_decision(
    project_dir: Path,
    *,
    question_capture: dict[str, object],
    selection: str,
    submission_key: str,
):
    """按当前 DEC 合同记录选择，问题正文和来源定位都取真实 CAP。"""

    selection_source = project_dir / f"{submission_key}.txt"
    selection_source.write_text(selection + "\n", encoding="utf-8")
    target_path = (
        project_dir
        / ".codex-sdlc"
        / "drafts"
        / "DRAFT-001"
        / "requirement.draft.md"
    )
    target = {
        "target_id": "DRAFT-001",
        "reference": reference(project_dir, target_path),
    }
    source_reference = reference(project_dir, selection_source)
    document = {
        "schema_version": "capture-increment.v1",
        "submission_key": submission_key,
        "draft_id": "DRAFT-001",
        "client_key": submission_key,
        "capture_type": "decision",
        "targets": [deepcopy(target)],
        "source_reference": deepcopy(source_reference),
        "source_sha256": sha256_file(selection_source),
        "increment": selection,
        "status": "pending",
        "decisions": [
            {
                "schema_version": "decision-input.v1",
                "client_key": f"{submission_key}-decision",
                "question": {
                    "text": question_capture["increment"],
                    "capture_ref": question_capture["capture_id"],
                    "reference": deepcopy(question_capture["source_reference"]),
                },
                "candidates": ["沿用当前订单列表字段", "使用独立导出字段"],
                "selection": selection,
                "scope": [deepcopy(target)],
                "source_reference": deepcopy(source_reference),
                "confirmed_at": "2026-07-17T09:00:00+08:00",
            }
        ],
    }
    input_file = project_dir / f"{submission_key}.json"
    input_file.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_cli(["discuss", "--file", input_file.name], cwd=project_dir)


def write_order_delete_start_package(path: Path, *, source_draft_id: str | None = None) -> None:
    package = {
        "title": "订单删除确认稿",
        "slug": "order-delete",
        "description": "运营需要在订单详情里删除误创建的未支付订单，并且删除动作要能留下清楚状态。",
        "background": "运营需要在订单详情里删除误创建的未支付订单，并且删除动作要能留下清楚状态。",
        "goal": "管理员可以删除单条未支付订单，并看到 deleted 状态和可读提示。",
        "user_scenarios": ["管理员在订单详情页删除误创建的未支付订单。"],
        "scope": ["只做单条未支付订单删除"],
        "out_of_scope": ["不做批量删除订单", "不做范围"],
        "business_rules": ["已支付订单不能删除", "删除成功后订单状态固定为 deleted"],
        "permission_rules": ["仅管理员可以删除订单"],
        "data_state_rules": ["删除请求成功后写入 deleted 状态，并记录删除人"],
        "interface_scope": ["页面：订单详情页删除按钮", "接口：DELETE /api/orders/{orderId}"],
        "exception_rules": ["订单不存在时提示 ORDER_NOT_FOUND", "非管理员删除时提示 PERMISSION_DENIED"],
        "test_focus": ["覆盖管理员删除、非管理员拒绝、已支付订单不可删除和订单不存在提示。"],
        "functional_requirements": [
            {
                "id": "FR-001",
                "title": "管理员删除单条订单",
                "description": "管理员在订单详情删除一条未支付订单。",
                "inputs": ["订单号", "删除原因"],
                "outputs": ["deleted 状态", "删除人"],
                "triggers": ["管理员确认删除未支付订单"],
                "data_changes": ["写入 deleted 状态、删除人和删除原因"],
                "permissions": ["仅管理员可以删除订单"],
                "rules": ["已支付订单不能删除"],
                "exceptions": ["订单不存在时提示 ORDER_NOT_FOUND"],
                "boundaries": ["不做批量删除订单"],
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "requirement_ids": ["FR-001"],
                "operation": "管理员删除未支付订单",
                "expected": "接口返回 deleted 状态",
                "pass_standard": "页面提示删除成功，状态同步为 deleted",
            }
        ],
        "test_cases": [
            {
                "id": "TC-001",
                "acceptance_ids": ["AC-001"],
                "requirement_ids": ["FR-001"],
                "type": "integration_test",
                "method": "用管理员调用 DELETE /api/orders/{orderId}",
                "operation": "用管理员调用 DELETE /api/orders/{orderId}",
                "expected": "返回 deleted 状态",
                "pass_standard": "状态和删除人都写入成功",
            }
        ],
        "design": {
            "title": "订单删除技术草稿",
            "summary": "复用订单详情页入口完成单条未支付订单删除。",
            "technical_goal": "完成单条未支付订单删除和状态反馈。",
            "modules": ["订单详情页", "订单删除接口"],
            "data_structures": ["OrderDeleteRequest：orderId、reason、operatorId", "Order：status、deletedBy、deleteReason"],
            "interfaces": ["DELETE /api/orders/{orderId}"],
            "state_flow": ["normal -> deleting -> deleted，失败时回到 normal 并展示错误"],
            "data_flow": ["页面提交订单号和删除原因，接口校验后写入 deleted 状态"],
            "permissions_security": ["仅管理员可以删除订单，非管理员返回 PERMISSION_DENIED"],
            "error_handling": ["ORDER_NOT_FOUND 和 PERMISSION_DENIED 都展示可读提示"],
            "test_strategy": ["跑 TC-001，并补非管理员和已支付订单的反向验证"],
            "risks": ["删除动作只允许未支付订单，避免误删真实有效订单"],
            "out_of_scope": ["不做批量删除订单", "不做范围"],
            "requirement_coverage": ["FR-001：覆盖订单删除入口、权限校验和状态写入。"],
        },
    }
    if source_draft_id:
        package["source_draft_id"] = source_draft_id
    write_formal_v3_package(path, package)


def test_draft_events_derive_state_and_materialize_files(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    paths = build_paths(project_dir)

    append_event(
        paths,
        event_type="draft_created",
        source="sdlc-draft-test",
        summary="创建 DRAFT-001",
        payload={
            "draft_id": "DRAFT-001",
            "title": "订单导出确认稿",
            "status": "discussing",
            "requirement_summary": "先确认订单导出的需求边界。",
        },
    )
    append_event(
        paths,
        event_type="draft_requirement_updated",
        source="sdlc-draft-test",
        summary="更新 DRAFT-001 需求草稿",
        payload={
            "draft_id": "DRAFT-001",
            "requirement_summary": "订单导出只允许运营角色使用。",
            "requirement_body": "# 需求草稿\n\n订单导出只允许运营角色使用，并保留失败提示。",
            "questions": ["导出字段是否沿用当前列表字段？"],
        },
    )
    append_event(
        paths,
        event_type="draft_design_updated",
        source="sdlc-draft-test",
        summary="更新 DRAFT-001 技术草稿",
        payload={
            "draft_id": "DRAFT-001",
            "design_summary": "前端展示按钮，后端继续做权限校验。",
            "design_body": "# 技术草稿\n\n前端按角色展示按钮，后端接口做最终权限校验。",
        },
    )
    append_event(
        paths,
        event_type="draft_review_recorded",
        source="sdlc-draft-test",
        summary="记录 DRAFT-001 审查结果",
        payload={
            "draft_id": "DRAFT-001",
            "review_items": ["需求和技术都覆盖了运营权限边界。"],
        },
    )
    append_event(
        paths,
        event_type="draft_decision_recorded",
        source="sdlc-draft-test",
        summary="记录 DRAFT-001 用户决定",
        payload={
            "draft_id": "DRAFT-001",
            "decision": "导出字段沿用当前订单列表字段。",
        },
    )
    append_event(
        paths,
        event_type="draft_status_changed",
        source="sdlc-draft-test",
        summary="DRAFT-001 进入可建档状态",
        payload={"draft_id": "DRAFT-001", "status": "start_ready"},
    )
    append_event(
        paths,
        event_type="draft_started",
        source="sdlc-draft-test",
        summary="DRAFT-001 已生成正式需求",
        payload={"draft_id": "DRAFT-001", "started_requirement_id": "REQ-001"},
    )

    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["draft_id"] == "DRAFT-001"
    assert draft["status"] == "started"
    assert draft["title"] == "订单导出确认稿"
    assert draft["requirement_summary"] == "订单导出只允许运营角色使用。"
    assert "运营角色使用" in draft["requirement_body"]
    assert draft["design_summary"] == "前端展示按钮，后端继续做权限校验。"
    assert "后端接口做最终权限校验" in draft["design_body"]
    assert draft["questions"] == []
    assert draft["decisions"] == ["导出字段沿用当前订单列表字段。"]
    assert draft["review_items"] == ["需求和技术都覆盖了运营权限边界。"]
    assert draft["started_requirement_id"] == "REQ-001"

    refreshed_state = refresh_materialized_state(paths)
    assert "DRAFT-001" in refreshed_state["drafts"]

    draft_dir = paths.drafts_dir / "DRAFT-001"
    status_json = json.loads((draft_dir / "status.json").read_text(encoding="utf-8"))
    assert status_json["draft_id"] == "DRAFT-001"
    assert status_json["status"] == "started"
    assert status_json["started_requirement_id"] == "REQ-001"

    assert "订单导出只允许运营角色使用" in (draft_dir / "requirement.draft.md").read_text(encoding="utf-8")
    assert "前端按角色展示按钮" in (draft_dir / "design.draft.md").read_text(encoding="utf-8")
    assert "需求和技术都覆盖" in (draft_dir / "review.md").read_text(encoding="utf-8")
    questions_text = (draft_dir / "questions.md").read_text(encoding="utf-8")
    assert "## 内容" in questions_text
    assert "暂无待确认问题" not in questions_text
    assert "导出字段沿用当前订单列表字段" in (draft_dir / "decisions.md").read_text(encoding="utf-8")

    with sqlite3.connect(project_dir / ".codex-sdlc" / "sdlc.db") as connection:
        row = connection.execute(
            "SELECT status, requirement_summary, design_summary, started_requirement_id FROM drafts WHERE draft_id = ?",
            ("DRAFT-001",),
        ).fetchone()
    assert row == (
        "started",
        "订单导出只允许运营角色使用。",
        "前端展示按钮，后端继续做权限校验。",
        "REQ-001",
    )


def test_old_project_without_drafts_dir_keeps_status_next_and_doctor_usable(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    paths = build_paths(project_dir)

    # 旧项目里本来没有 drafts 目录。这里删除目录后再跑命令，
    # 是为了确认新增 DRAFT 能力不会让旧项目的日常状态检查报错。
    if paths.drafts_dir.exists():
        for child in paths.drafts_dir.iterdir():
            if child.is_file():
                child.unlink()
        paths.drafts_dir.rmdir()

    for command in [["status"], ["next"], ["doctor"]]:
        result = run_cli(command, cwd=project_dir)
        assert result.returncode == 0, result.stderr


def test_draft_cli_create_and_update_content(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0

    requirement_file = project_dir / "需求草稿.md"
    requirement_file.write_text("# 需求草稿\n\n订单导出只给运营角色使用。", encoding="utf-8")
    design_file = project_dir / "技术草稿.md"
    design_file.write_text("# 技术草稿\n\n前端控制按钮展示，后端继续做权限校验。", encoding="utf-8")
    review_file = project_dir / "审查结果.md"
    review_file.write_text("# 审查\n\n需求和技术对权限边界的说法一致。", encoding="utf-8")

    create_result = run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir)
    assert create_result.returncode == 0, create_result.stderr
    assert "已创建 DRAFT：DRAFT-001" in create_result.stdout

    for command in [
        ["draft", "requirement", "DRAFT-001", "--file", str(requirement_file)],
        ["draft", "design", "DRAFT-001", "--file", str(design_file)],
        ["draft", "question", "DRAFT-001", "导出字段是否沿用当前列表字段？"],
        ["draft", "decision", "DRAFT-001", "导出字段沿用当前订单列表字段。"],
        ["draft", "review", "DRAFT-001", "--file", str(review_file)],
        ["draft", "status", "DRAFT-001"],
    ]:
        result = run_cli(command, cwd=project_dir)
        assert result.returncode == 0, result.stderr

    paths = build_paths(project_dir)
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["title"] == "订单导出确认稿"
    assert draft["status"] == "needs_user"
    assert "订单导出只给运营角色使用" in draft["requirement_body"]
    assert "前端控制按钮展示" in draft["design_body"]
    assert draft["questions"] == ["导出字段是否沿用当前列表字段？"]
    assert draft["decisions"] == ["导出字段沿用当前订单列表字段。"]
    assert any("权限边界的说法一致" in item for item in draft["review_items"])
    assert "订单导出只给运营角色使用" in (paths.drafts_dir / "DRAFT-001" / "requirement.draft.md").read_text(encoding="utf-8")


def test_draft_cli_rejects_missing_draft_and_started_status(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0

    missing_result = run_cli(["draft", "status", "DRAFT-999"], cwd=project_dir)
    assert missing_result.returncode == 1
    assert "没有找到 DRAFT `DRAFT-999`" in missing_result.stderr

    started_result = run_cli(["draft", "status", "DRAFT-001", "started"], cwd=project_dir)
    assert started_result.returncode != 0


def test_draft_cli_requires_content_or_file_for_markdown_commands(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0

    result = run_cli(["draft", "requirement", "DRAFT-001"], cwd=project_dir)
    assert result.returncode == 1
    assert "需求草稿不能为空" in result.stderr


def test_discuss_creates_draft_and_keeps_capture_record(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0

    result = create_structured_discussion(
        project_dir,
        increment="订单导出只允许运营角色使用，并且导出失败时要给出清楚提示。",
    )
    assert result.returncode == 0, result.stderr
    assert "已记录结构化 CAP：CAP-001" in result.stdout

    paths = build_paths(project_dir)
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["requirement_body"] == ""
    assert draft["structured_captures"][0]["increment"] == (
        "订单导出只允许运营角色使用，并且导出失败时要给出清楚提示。"
    )

    capture = state["captures"][0]
    assert capture["capture_id"] == "CAP-001"
    assert capture["target_type"] == "requirement_increment"
    assert capture["status"] == "pending"
    assert (paths.captures_dir / "CAP-001.md").exists()


def test_discuss_updates_same_draft_questions_and_decisions(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    create_structured_discussion(
        project_dir,
        increment="订单导出需要先确认权限边界。",
        submission_key="v15-permission-fact",
    )
    question_result = append_structured_cap(
        project_dir,
        submission_key="v15-export-fields-question",
        capture_type="question",
        increment="导出字段是否沿用当前订单列表字段？",
    )
    assert question_result.returncode == 0, question_result.stderr
    question_capture = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"][
        "structured_captures"
    ][1]
    decision_result = append_structured_decision(
        project_dir,
        question_capture=question_capture,
        selection="沿用当前订单列表字段",
        submission_key="v15-export-fields-decision",
    )
    assert decision_result.returncode == 0, decision_result.stderr
    assert "DEC-001" in decision_result.stdout

    paths = build_paths(project_dir)
    state = derive_state(paths)
    assert list(state["drafts"].keys()) == ["DRAFT-001"]
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["questions"] == []
    assert draft["decisions"] == []
    assert [item["decision_id"] for item in draft["decision_records"]] == ["DEC-001"]
    assert draft["decision_records"][0]["selection"] == "沿用当前订单列表字段"
    assert draft["assessment"]["open_questions"] == []


def test_next_recommends_discuss_for_incomplete_requirement_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    create_structured_discussion(
        project_dir,
        increment="订单导出只允许运营角色使用，并且失败时要给出清楚提示。",
    )

    next_result = run_cli(["next"], cwd=project_dir)
    status_result = run_cli(["status"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert status_result.returncode == 0, status_result.stderr
    assert "- 主推荐：$sdlc-material" in next_result.stdout
    assert "当前活跃 DRAFT" in next_result.stdout
    assert "DRAFT-001 [discussing]" in next_result.stdout
    assert "material_missing:DRAFT-001:missing" in next_result.stdout
    assert "当前活跃 DRAFT" in status_result.stdout
    assert "DRAFT-001 [discussing]" in status_result.stdout
    assert "- 推荐：$sdlc-material" in status_result.stdout


def test_next_recommends_continuing_discussion_for_discussing_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0

    next_result = run_cli(["next"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-discuss 继续完善需求草案" in next_result.stdout
    assert "DRAFT-001 [discussing]" in next_result.stdout


def test_next_prioritizes_questions_for_needs_user_draft_and_skips_started_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "旧需求确认稿"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir).returncode == 0
    paths = build_paths(project_dir)
    append_event(
        paths,
        event_type="draft_started",
        source="sdlc-draft-test",
        summary="DRAFT-001 已正式建档",
        payload={"draft_id": "DRAFT-001", "started_requirement_id": "REQ-001"},
    )
    refresh_materialized_state(paths)

    assert run_cli(["draft", "create", "新需求确认稿"], cwd=project_dir).returncode == 0
    assert (
        run_cli(["draft", "question", "DRAFT-002", "导出字段是否沿用当前订单列表字段？"], cwd=project_dir).returncode
        == 0
    )

    next_result = run_cli(["next"], cwd=project_dir)
    status_result = run_cli(["status"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert status_result.returncode == 0, status_result.stderr
    assert "- 主推荐：$sdlc-discuss 补充已确认结论" in next_result.stdout
    assert "待用户回答" in next_result.stdout
    assert "导出字段是否沿用当前订单列表字段？" in next_result.stdout
    assert "DRAFT-002 [needs_user]" in next_result.stdout
    assert "DRAFT-001 [started]" not in next_result.stdout
    assert "DRAFT-002 [needs_user]" in status_result.stdout
    assert "DRAFT-001 [started]" not in status_result.stdout


def test_next_uses_content_assessment_instead_of_manual_design_ready_status(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0
    requirement_file = project_dir / "需求草稿.md"
    requirement_file.write_text("# 需求草稿\n\n订单导出只给运营角色使用。", encoding="utf-8")
    assert run_cli(["draft", "requirement", "DRAFT-001", "--file", str(requirement_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir).returncode == 0

    next_result = run_cli(["next"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-discuss 继续完善需求草案" in next_result.stdout
    assert "DRAFT-001 [discussing]" in next_result.stdout


def test_design_natural_text_requires_material_reference(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0

    result = run_cli(["design", "前端加按钮，后端做权限校验。"], cwd=project_dir)

    assert result.returncode == 1
    assert "technical-solution 原样归档" in result.stderr
    assert "design-reference --file" in result.stderr


def test_design_natural_text_does_not_modify_structured_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    create_structured_discussion(
        project_dir,
        increment="订单导出只允许运营角色使用，并且失败时要给出清楚提示。",
        requirement_body="# 需求草稿\n\n订单导出只允许运营角色使用。",
    )

    result = run_cli(["design", "前端按钮按角色展示，后端导出接口继续做权限校验，并补运营和非运营两类测试。"], cwd=project_dir)

    assert result.returncode == 1
    assert "technical-solution 原样归档" in result.stderr
    paths = build_paths(project_dir)
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert not draft.get("design_body")
    assert not (paths.designs_dir / "DES-001.md").exists()


def test_design_command_rejects_free_text_without_inferring_semantics(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    create_structured_discussion(
        project_dir,
        increment="订单导出只做单次导出，不做批量导出，只允许运营角色使用。",
        requirement_body="# 需求草稿\n\n订单导出只做单次导出，不做批量导出。",
    )

    result = run_cli(["design", "后端新增批量导出多个订单文件能力，并允许所有用户使用。"], cwd=project_dir)

    assert result.returncode == 1
    assert "technical-solution 原样归档" in result.stderr
    paths = build_paths(project_dir)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["questions"] == []
    assert "后端新增批量导出多个订单文件能力" not in (
        paths.drafts_dir / "DRAFT-001" / "design.draft.md"
    ).read_text(encoding="utf-8")


def test_only_current_integrated_design_passed_can_reach_start_ready() -> None:
    draft = {
        "draft_id": "DRAFT-001",
        "_requirement_confirmation_state": {
            "current_confirmation": {
                "confirmation_sha256": "a" * 64,
            }
        },
        "design_references": [
            {
                "design_id": "DES-001",
                "status": "confirmed",
                "requirement_confirmation_sha256": "a" * 64,
            }
        ],
        "design_stage": {
            "plan_status": "current",
            "ready_for_review": True,
            "blockers": [],
        },
        "_integrated_design_review_state": {
            "status": "empty",
            "can_advance": False,
            "reviews": [],
        },
    }

    waiting = draft_lifecycle._assess_structured_design_stage(draft)
    assert waiting.effective_status == "design_reviewing"
    assert waiting.can_start is False
    assert waiting.blockers[0].code == "integrated_design_review_pending"

    draft["_integrated_design_review_state"] = {
        "status": "ready",
        "can_advance": True,
        "reviews": [
            {
                "review_id": "REV-002",
                "stage": "integrated_design",
                "owner_id": "DRAFT-001",
                "request_status": "completed",
                "effective_status": "passed",
                "is_current": True,
                "can_advance": True,
            }
        ],
    }
    passed = draft_lifecycle._assess_structured_design_stage(draft)
    assert passed.effective_status == "start_ready"
    assert passed.can_start is True
    assert passed.next_action == "$sdlc-start"

    draft["_integrated_design_review_state"]["can_advance"] = False
    draft["_integrated_design_review_state"]["reviews"][0][
        "effective_status"
    ] = "stale"
    draft["_integrated_design_review_state"]["reviews"][0]["can_advance"] = False
    stale = draft_lifecycle._assess_structured_design_stage(draft)
    assert stale.effective_status == "design_reviewing"
    assert stale.blockers[0].code == "integrated_design_review_stale"



def write_dev006_draft_files(project_dir: Path, *, conflict_review: bool = False) -> tuple[Path, Path, Path]:
    requirement_file = project_dir / "DEV006需求草稿.md"
    requirement_file.write_text(
        "\n".join(
            [
                "# 订单导出确认稿",
                "",
                "## 背景和目标",
                "运营需要在订单列表里导出当前筛选结果。",
                "",
                "## 本轮范围",
                "- 只做订单列表单次导出。",
                "",
                "## 用户和使用场景",
                "- 运营在订单列表里导出当前筛选结果。",
                "",
                "## 不做范围",
                "- 不做批量导出多个文件。",
                "",
                "## 业务规则",
                "- 导出内容必须沿用当前筛选条件。",
                "",
                "## 权限规则",
                "- 只允许运营角色导出订单。",
                "",
                "## 数据和状态规则",
                "- 导出任务生成后记录导出人和筛选条件。",
                "",
                "## 接口或页面范围",
                "- 页面：订单列表导出入口。",
                "- 接口：POST /api/orders/export。",
                "",
                "## 异常和边界",
                "- 导出失败时给出可读错误提示。",
                "",
                "## 功能需求",
                "### FR-001 单次订单导出",
                "- 说明：运营在订单列表导出当前筛选结果。",
                "- 输入：当前筛选条件。",
                "- 输出：导出文件。",
                "- 触发条件：运营点击导出入口。",
                "- 保存数据：保存导出人、筛选条件和导出任务状态。",
                "- 权限：只允许运营角色导出订单。",
                "- 规则：导出内容必须沿用当前筛选条件。",
                "- 异常：导出失败时给出可读错误提示。",
                "- 边界：不做批量导出多个文件。",
                "- 验收关联：AC-001。",
                "",
                "## 验收标准",
                "### AC-001 运营导出成功",
                "- 覆盖需求：FR-001",
                "- 操作：运营角色点击导出入口。",
                "- 预期：可以拿到导出文件。",
                "- 通过标准：导出文件内容符合当前筛选条件。",
                "",
                "## 测试关注点",
                "- 覆盖运营角色成功导出和非运营角色不可见入口。",
                "",
                "## 测试矩阵",
                "### TC-001 运营导出当前筛选结果",
                "- 覆盖验收：AC-001",
                "- 覆盖需求：FR-001",
                "- 类型：manual_only",
                "- 操作：运营角色点击导出入口。",
                "- 预期：下载导出文件。",
                "- 通过标准：导出文件内容符合当前筛选条件。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    design_file = project_dir / "DEV006技术草稿.md"
    design_file.write_text(
        "\n".join(
            [
                "# 订单导出技术草稿",
                "",
                "## 技术目标",
                "前端复用订单列表筛选条件调用导出接口。",
                "",
                "## 涉及模块",
                "- 订单列表页面",
                "- 订单导出接口",
                "",
                "## 数据结构",
                "- OrderExportJob：filter、operatorId、downloadUrl。",
                "",
                "## 接口设计",
                "- POST /api/orders/export。",
                "",
                "## 数据流",
                "- 页面传入筛选条件，接口生成导出文件并返回下载信息。",
                "",
                "## 状态流",
                "- idle -> exporting -> exported，失败时回到 idle 并展示错误。",
                "",
                "## 权限和安全",
                "- 只允许运营角色导出订单。",
                "",
                "## 错误处理",
                "- 导出失败时展示后端返回的错误提示。",
                "",
                "## 测试策略",
                "覆盖运营角色成功导出和非运营角色不可见入口。",
                "",
                "## 风险处理",
                "- 导出字段变动时先按当前筛选条件和订单列表字段复核。",
                "",
                "## 本轮不做",
                "- 不做批量订单导出。",
                "",
                "## 对需求草稿的覆盖说明",
                "- FR-001：已覆盖订单导出、运营权限、筛选条件和导出失败提示。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    review_file = project_dir / "DEV006审查结果.md"
    review_file.write_text(
        ("存在阻塞冲突：需求说只做单次导出，技术方案疑似批量导出。" if conflict_review else "需求草稿和技术草稿已对齐，可以建档。")
        + "\n",
        encoding="utf-8",
    )
    return requirement_file, design_file, review_file


def prepare_dev006_draft(project_dir: Path, *, requirement: bool = True, design: bool = True, conflict_review: bool = False) -> None:
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0
    requirement_file, design_file, review_file = write_dev006_draft_files(project_dir, conflict_review=conflict_review)
    if requirement:
        assert run_cli(["draft", "requirement", "DRAFT-001", "--file", str(requirement_file)], cwd=project_dir).returncode == 0
    if design:
        assert run_cli(["draft", "design", "DRAFT-001", "--file", str(design_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "review", "DRAFT-001", "--file", str(review_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir).returncode == 0


def write_complete_order_delete_draft_files(
    project_dir: Path,
    *,
    design_extra_section: str = "## 本轮不做\n- 不做批量删除订单。\n\n## 对需求草稿的覆盖说明\n- FR-001：已覆盖单条删除、管理员权限、状态变化和异常提示。",
) -> tuple[Path, Path]:
    requirement_file = project_dir / "完整订单删除需求草稿.md"
    requirement_file.write_text(
        "\n".join(
            [
                "# 订单删除确认稿",
                "",
                "## 背景和目标",
                "运营需要在订单详情里删除误创建的未支付订单，并且删除动作要能留下清楚状态。",
                "",
                "## 用户和使用场景",
                "- 管理员在订单详情确认订单确实误创建后，点击删除并看到成功提示。",
                "",
                "## 本轮范围",
                "- 只做单条未支付订单删除。",
                "",
                "## 不做范围",
                "- 不做批量删除订单。",
                "",
                "## 业务规则",
                "- 已支付订单不能删除。",
                "- 删除成功后订单状态固定为 deleted。",
                "",
                "## 权限规则",
                "- 仅管理员可以删除订单。",
                "",
                "## 数据和状态规则",
                "- 删除请求成功后写入 deleted 状态，并记录删除人。",
                "",
                "## 接口或页面范围",
                "- 页面：订单详情页删除按钮。",
                "- 接口：DELETE /api/orders/{orderId}。",
                "",
                "## 异常和边界",
                "- 订单不存在时提示 ORDER_NOT_FOUND。",
                "- 非管理员删除时提示 PERMISSION_DENIED。",
                "",
                "## 功能需求",
                "### FR-001 管理员删除单条订单",
                "- 说明：管理员在订单详情删除一条未支付订单。",
                "- 输入：订单号、删除原因。",
                "- 输出：deleted 状态、删除人。",
                "- 触发条件：管理员确认删除未支付订单。",
                "- 保存数据：写入 deleted 状态、删除人和删除原因。",
                "- 权限：仅管理员可以删除订单。",
                "- 规则：已支付订单不能删除。",
                "- 异常：订单不存在时提示 ORDER_NOT_FOUND。",
                "- 边界：本轮不做批量删除订单。",
                "- 验收关联：AC-001。",
                "",
                "## 验收标准",
                "### AC-001 单条删除成功",
                "- 覆盖需求：FR-001",
                "- 操作：管理员删除未支付订单。",
                "- 预期：接口返回 deleted 状态。",
                "- 通过标准：页面提示删除成功，状态同步为 deleted。",
                "",
                "## 测试关注点",
                "- 覆盖管理员成功删除、非管理员被拒绝、已支付订单被拒绝。",
                "",
                "## 测试矩阵",
                "### TC-001 管理员删除未支付订单",
                "- 覆盖验收：AC-001",
                "- 覆盖需求：FR-001",
                "- 类型：integration_test",
                "- 操作：用管理员调用 DELETE /api/orders/{orderId}。",
                "- 预期：返回 deleted 状态。",
                "- 通过标准：状态和删除人都写入成功。",
                "",
                "## 未确认问题",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    design_file = project_dir / "完整订单删除技术草稿.md"
    design_file.write_text(
        "\n".join(
            [
                "# 订单删除技术草稿",
                "",
                "## 技术目标",
                "复用订单详情页入口完成单条未支付订单删除。",
                "",
                "## 涉及模块",
                "- 订单详情页",
                "- 订单删除接口",
                "",
                "## 数据结构",
                "- OrderDeleteRequest：orderId、reason、operatorId。",
                "- Order：status、deletedBy、deleteReason。",
                "",
                "## 接口设计",
                "- DELETE /api/orders/{orderId}",
                "",
                "## 数据流",
                "- 页面提交订单号和删除原因，接口校验后写入 deleted 状态。",
                "",
                "## 状态流",
                "- normal -> deleting -> deleted，失败时回到 normal 并展示错误。",
                "",
                "## 权限和安全",
                "- 仅管理员可以删除订单，非管理员返回 PERMISSION_DENIED。",
                "",
                "## 错误处理",
                "- ORDER_NOT_FOUND 和 PERMISSION_DENIED 都展示可读提示。",
                "",
                "## 测试策略",
                "- 跑 TC-001，并补非管理员和已支付订单的反向验证。",
                "",
                "## 风险处理",
                "- 删除动作只允许未支付订单，避免误删真实有效订单。",
                "",
                design_extra_section.strip(),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return requirement_file, design_file


def prepare_complete_order_delete_draft(project_dir: Path, *, design_extra_section: str | None = None) -> None:
    assert run_cli(["draft", "create", "订单删除确认稿"], cwd=project_dir).returncode == 0
    requirement_file, design_file = write_complete_order_delete_draft_files(
        project_dir,
        design_extra_section=(
            "## 本轮不做\n- 不做批量删除订单。\n\n## 对需求草稿的覆盖说明\n- FR-001：已覆盖单条删除、管理员权限、状态变化和异常提示。"
            if design_extra_section is None
            else design_extra_section
        ),
    )
    assert run_cli(["draft", "requirement", "DRAFT-001", "--file", str(requirement_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "design", "DRAFT-001", "--file", str(design_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir).returncode == 0


def test_start_consumes_start_ready_draft_and_marks_started(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    start_result, package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )

    assert "已创建正式需求：REQ-001" in start_result.stdout
    assert "来源 DRAFT：DRAFT-001" in start_result.stdout
    assert package["workflow_profile"] == "document-first.v1"
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "started"
    assert draft["started_requirement_id"] == "REQ-001"
    requirement = state["requirements"]["REQ-001"]
    assert requirement["native_start"]["source_draft_id"] == "DRAFT-001"
    assert requirement["native_start"]["reviews"] == {
        "requirement_split": "REV-001",
        "integrated_design": "REV-002",
    }
    assert (requirement_dir / "effective" / "requirement.current.json").exists()


def test_start_can_review_and_consume_design_ready_draft(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    before = derive_state(paths)["drafts"]["DRAFT-001"]
    assert before["status"] == "start_ready"
    assert before["_integrated_design_review_state"]["can_advance"] is True

    _result, package, _requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )

    assert package["reviews"]["requirement_split"] == "REV-001"
    assert package["reviews"]["integrated_design"] == "REV-002"
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["status"] == "started"
    assert state["drafts"]["DRAFT-001"]["started_requirement_id"] == "REQ-001"


def test_start_from_draft_does_not_link_unrelated_accepted_design(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    append_event(
        paths,
        event_type="design_recorded",
        source="sdlc-test",
        summary="记录无关技术方案 DES-999",
        payload={
            "design_id": "DES-999",
            "draft_id": "DRAFT-999",
            "title": "无关技术方案",
            "summary": "这是另一个需求的技术方案，不能被 DRAFT-001 正式建档吞掉。",
            "status": "draft",
            "file_path": ".codex-sdlc/designs/DES-999.md",
        },
    )
    append_event(
        paths,
        event_type="design_accepted",
        source="sdlc-test",
        summary="确认无关技术方案 DES-999",
        payload={"design_ids": ["DES-999"]},
    )
    refresh_materialized_state(paths)

    start_result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )

    assert "已纳入已确认技术方案" not in start_result.stdout
    state = derive_state(paths)
    archived_design = read_effective_json(requirement_dir, "design.current.json")
    assert "DES-001" in {
        item["artifact_id"] for item in archived_design["artifacts"]
    }
    assert "DES-999" not in {
        item["artifact_id"] for item in archived_design["artifacts"]
    }
    unrelated_design = next(
        design for design in state["designs"] if design["design_id"] == "DES-999"
    )
    assert unrelated_design["requirement_id"] is None
    assert unrelated_design["status"] == "accepted"


def test_start_file_without_source_does_not_link_draft_bound_design(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    # 当前正式包必须带来源，因此这里用另一个 DRAFT 的旧方案确认建档不会串用来源。
    append_event(
        paths,
        event_type="design_recorded",
        source="sdlc-test",
        summary="记录旧 DRAFT 技术方案 DES-001",
        payload={
            "design_id": "DES-001",
            "draft_id": "DRAFT-OLD",
            "title": "旧 DRAFT 技术方案",
            "summary": "这个技术方案带有 DRAFT 来源，不能被无来源 start --file 吞掉。",
            "status": "draft",
            "file_path": ".codex-sdlc/designs/DES-001.md",
        },
    )
    append_event(
        paths,
        event_type="design_accepted",
        source="sdlc-test",
        summary="确认旧 DRAFT 技术方案 DES-001",
        payload={"design_ids": ["DES-001"]},
    )
    refresh_materialized_state(paths)
    start_package, _package = write_document_first_formal_v3_package(project_dir)

    start_result = run_cli(["start", "--file", str(start_package)], cwd=project_dir)

    assert start_result.returncode == 0, start_result.stderr
    assert "已纳入已确认技术方案" not in start_result.stdout
    state = derive_state(paths)
    requirement = state["requirements"]["REQ-001"]
    linked_design_ids = [design["design_id"] for design in requirement["designs"]]
    assert "DES-001" not in linked_design_ids
    old_design = next(design for design in state["designs"] if design["design_id"] == "DES-001")
    assert old_design["requirement_id"] is None


def test_light_start_offline_does_not_link_draft_bound_design_without_active_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    paths = build_paths(project_dir)
    append_event(
        paths,
        event_type="draft_created",
        source="sdlc-test",
        summary="创建已结束 DRAFT-001",
        payload={"draft_id": "DRAFT-001", "title": "旧确认稿", "status": "started"},
    )
    append_event(
        paths,
        event_type="design_recorded",
        source="sdlc-test",
        summary="记录旧确认稿技术方案 DES-001",
        payload={
            "design_id": "DES-001",
            "draft_id": "DRAFT-001",
            "title": "旧确认稿技术方案",
            "summary": "这个技术方案已经绑定旧 DRAFT，轻量需求不能自动拿走。",
            "status": "draft",
            "file_path": ".codex-sdlc/designs/DES-001.md",
        },
    )
    append_event(
        paths,
        event_type="design_accepted",
        source="sdlc-test",
        summary="确认旧确认稿技术方案 DES-001",
        payload={"design_ids": ["DES-001"]},
    )
    refresh_materialized_state(paths)

    light_result = run_cli(["light-start", "修一个按钮文案"], cwd=project_dir)

    assert light_result.returncode == 0, light_result.stderr
    assert "light-start 已下线" in light_result.stdout
    assert "已纳入技术方案" not in light_result.stdout
    state = derive_state(paths)
    assert state["requirements"] == {}
    old_design = next(design for design in state["designs"] if design["design_id"] == "DES-001")
    assert old_design["requirement_id"] is None


def test_start_allows_out_of_scope_repeated_in_design_not_do_section(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    start_result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )

    assert "已创建正式需求：REQ-001" in start_result.stdout
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    assert requirement["out_of_scope"] == ["课程内容编辑"]
    assert requirement["functional_requirements"][0]["out_of_scope"] == [
        "编辑课程"
    ]


def test_start_cannot_bypass_review_for_legacy_positive_design(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    assert run_cli(["draft", "create", "旧设计文本确认稿"], cwd=project_dir).returncode == 0
    design_file = project_dir / "旧设计正向结论.md"
    design_file.write_text(
        "# 技术草稿\n\n设计已经完成，可以直接建档。\n",
        encoding="utf-8",
    )
    assert run_cli(
        ["draft", "design", "DRAFT-002", "--file", str(design_file)],
        cwd=project_dir,
    ).returncode == 0

    with pytest.raises(SdlcError, match="不是 start_ready"):
        write_document_first_formal_v3_package(project_dir, draft_id="DRAFT-002")

    draft = derive_state(paths)["drafts"]["DRAFT-002"]
    assert "_integrated_design_review_state" not in draft
    assert draft["status"] == "discussing"
    assert "REQ-001" not in derive_state(paths)["requirements"]


def test_start_from_draft_preserves_top_level_rules_and_test_focus(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    matrix = read_effective_json(requirement_dir, "test-matrix.current.json")

    assert requirement["global_rules"][0]["description"] == (
        "课程访问统一使用当前登录状态。"
    )
    assert requirement["user_scenarios"] == ["用户登录后打开课程页"]
    assert requirement["functional_requirements"][0]["rules"] == [
        "只有登录用户可以查看课程"
    ]
    assert matrix["acceptance_criteria"][0]["operation"] == (
        "使用已登录用户打开课程页"
    )
    assert matrix["acceptance_criteria"][0]["pass_standard"] == (
        "标题和内容完整显示且没有访问错误"
    )


def test_start_accepts_exception_alias_and_outputs_standard_heading(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")

    # 当前流程不再识别 Markdown 标题别名，异常直接来自 FR 的结构化字段。
    assert requirement["functional_requirements"][0]["states_and_exceptions"] == [
        "登录失效时拒绝访问"
    ]
    assert "异常和边界情况" not in (
        requirement_dir / "effective" / "requirement.current.md"
    ).read_text(encoding="utf-8")


def test_start_top_sections_do_not_mix_into_data_state_section(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    design = read_effective_json(requirement_dir, "design.current.json")

    requirement_text = json.dumps(requirement, ensure_ascii=False)
    design_text = json.dumps(design, ensure_ascii=False)
    assert "登录失效时拒绝访问" in requirement_text
    assert "GET /users/{id}" not in requirement_text
    assert "USER_NOT_FOUND" not in requirement_text
    assert "GET /users/{id}" in design_text
    assert "USER_NOT_FOUND" in design_text


def test_start_blocks_placeholder_draft_and_writes_missing_items(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    assert run_cli(["draft", "create", "优化设置页体验"], cwd=project_dir).returncode == 0
    before_events = paths.events_file.read_bytes()

    # 粗略草稿没有资料、结构化需求和三类审核，正式包工厂必须在落盘前拒绝。
    with pytest.raises(
        SdlcError,
        match="DRAFT-002 不是 start_ready，不能生成文档优先正式包",
    ):
        write_document_first_formal_v3_package(project_dir, draft_id="DRAFT-002")

    assert paths.events_file.read_bytes() == before_events
    assert derive_state(paths)["drafts"]["DRAFT-002"]["status"] == "discussing"
    assert not list(paths.requirements_dir.glob("REQ-*"))


def test_start_from_draft_splits_markdown_fr_ac_tc_into_current_json(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    matrix = read_effective_json(requirement_dir, "test-matrix.current.json")

    # 当前流程直接投影结构化 FR/AC；旧 Markdown 章节和独立 TC 不再参与建档。
    assert [item["id"] for item in requirement["functional_requirements"]] == [
        "FR-001"
    ]
    acceptance = requirement["functional_requirements"][0]["acceptance_criteria"]
    assert [item["id"] for item in acceptance] == ["AC-001"]
    assert acceptance[0]["owner_fr_ref"] == "FR-001"
    assert acceptance[0]["operation"] == "使用已登录用户打开课程页"
    assert acceptance[0]["expected"] == "页面显示课程标题和内容"
    assert acceptance[0]["pass_standard"] == "标题和内容完整显示且没有访问错误"
    assert matrix["acceptance_criteria"][0]["requirement_id"] == "FR-001"
    assert "test_cases" not in matrix


def test_start_blocks_obvious_interface_path_conflict_and_writes_p0_review(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    api_path = formal_source_path(
        project_dir,
        package,
        artifact_type="design_artifact_json",
        business_id="API-001",
    )
    before_events = paths.events_file.read_bytes()
    overwrite_json(
        api_path,
        lambda document: document["content"]["endpoints"][0].update(
            {"path_or_event": "GET /accounts/{id}"}
        ),
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_confirmed_out_of_scope_missing_from_requirement_draft(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()
    overwrite_json(split_path, lambda document: document.update({"out_of_scope": []}))

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_without_review_ready_draft_guides_next_step(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0

    # 当前开工入口只消费完整正式包，未完成草稿应由同一项目的 next 引导继续补资料。
    with pytest.raises(
        SdlcError,
        match="DRAFT-002 不是 start_ready，不能生成文档优先正式包",
    ):
        write_document_first_formal_v3_package(project_dir, draft_id="DRAFT-002")
    next_result = run_cli(["next"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-discuss" in next_result.stdout
    assert derive_state(paths)["drafts"]["DRAFT-002"]["status"] == "discussing"
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_start_ready_draft_without_requirement_body(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()
    split_path.unlink()

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_can_recover_after_missing_requirement_review_failure(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    broken = deepcopy(package)
    broken["reviews"]["requirement_split"] = "REV-999"
    package_file.write_text(
        json.dumps(broken, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    before_events = paths.events_file.read_bytes()

    first_start = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert first_start.returncode == 1
    assert "审核 REV 不是当前需求审核和整体设计审核" in first_start.stderr
    assert paths.events_file.read_bytes() == before_events
    package_file.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    second_start = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert second_start.returncode == 0, second_start.stderr
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "started"
    assert draft["started_requirement_id"] == "REQ-001"
def test_start_blocks_start_ready_draft_without_design_body(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    design_path = formal_source_path(
        project_dir,
        package,
        artifact_type="design_artifact_json",
        business_id="API-001",
    )
    before_events = paths.events_file.read_bytes()
    design_path.unlink()

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_does_not_interpret_free_text_review_item(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, _package = write_document_first_formal_v3_package(project_dir)
    review_projection = paths.draft_dir("DRAFT-001") / "review.md"
    review_projection.write_text(
        review_projection.read_text(encoding="utf-8") + "\n- 阻塞：展示文字不能替代结构化审核结果。\n",
        encoding="utf-8",
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert "阻塞：展示文字不能替代结构化审核结果" in review_projection.read_text(
        encoding="utf-8"
    )
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "started"
def test_start_preserves_nested_round_out_of_scope_in_formal_docs(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    requirement_md = (
        requirement_dir / "effective" / "requirement.current.md"
    ).read_text(encoding="utf-8")

    # 顶层和 FR 内的不做范围分别保留，正式投影不依赖旧 Markdown 嵌套标题。
    assert requirement["out_of_scope"] == ["课程内容编辑"]
    assert requirement["functional_requirements"][0]["out_of_scope"] == ["编辑课程"]
    assert "课程内容编辑" in requirement_md
    assert "编辑课程" in requirement_md
def test_start_blocks_multi_fr_acceptance_without_requirement_link_before_normalize(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()
    overwrite_json(
        split_path,
        lambda document: document["functional_requirements"][0][
            "acceptance_criteria"
        ][0].update({"owner_fr_ref": "FR-999"}),
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_keeps_manual_p0_questions_blocking(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    prepare_complete_order_delete_draft(project_dir)
    assert run_cli(["draft", "question", "DRAFT-001", "P0 用户还没确认是否允许删除已归档订单。"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-001"], cwd=project_dir).returncode == 0

    start_result = run_cli(["start"], cwd=project_dir)

    assert start_result.returncode == 1
    paths = build_paths(project_dir)
    questions_text = (paths.drafts_dir / "DRAFT-001" / "questions.md").read_text(encoding="utf-8")
    assert "P0 用户还没确认" in questions_text
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "needs_user"


def test_start_matches_confirmed_out_of_scope_by_core_phrase(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")

    # 当前合同按结构化数组保留范围，不再从用户确认句子里匹配“核心短语”。
    assert requirement["out_of_scope"] == ["课程内容编辑"]
    assert requirement["functional_requirements"][0]["out_of_scope"] == ["编辑课程"]
def test_start_ignores_question_like_text_inside_requirement_body(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, _package = write_document_first_formal_v3_package(project_dir)
    projection = paths.draft_dir("DRAFT-001") / "requirement.draft.md"
    projection.write_text(
        projection.read_text(encoding="utf-8")
        + "\n删除原因字段是否必填还需要用户确认？\n",
        encoding="utf-8",
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "started"
    assert len(list(paths.requirements_dir.glob("REQ-*"))) == 1
def test_draft_decision_resolve_question_clears_current_question_and_updates_next(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    prepare_dev006_draft(project_dir)
    assert run_cli(["draft", "question", "DRAFT-001", "CSV 字段顺序还需要用户确认。"], cwd=project_dir).returncode == 0

    decision_result = run_cli(
        [
            "draft",
            "decision",
            "DRAFT-001",
            "用户确认：CSV 字段顺序沿用订单列表。",
            "--resolve-question",
            "CSV 字段顺序还需要用户确认。",
        ],
        cwd=project_dir,
    )

    assert decision_result.returncode == 0, decision_result.stderr
    assert "已解决待确认问题" in decision_result.stdout
    paths = build_paths(project_dir)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["questions"] == []
    assert draft["status"] == "reviewing"
    assert draft["decisions"] == ["用户确认：CSV 字段顺序沿用订单列表。"]
    questions_text = (paths.drafts_dir / "DRAFT-001" / "questions.md").read_text(encoding="utf-8")
    decisions_text = (paths.drafts_dir / "DRAFT-001" / "decisions.md").read_text(encoding="utf-8")
    assert "## 内容" in questions_text
    assert "暂无待确认问题" not in questions_text
    assert "CSV 字段顺序沿用订单列表" in decisions_text
    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-design" in next_result.stdout


def test_draft_decision_rejects_question_fragment_and_only_accepts_exact_question(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    prepare_dev006_draft(project_dir)
    question = "CSV 字段顺序还需要用户确认。"
    assert run_cli(["draft", "question", "DRAFT-001", question], cwd=project_dir).returncode == 0

    fragment_result = run_cli(
        [
            "draft",
            "decision",
            "DRAFT-001",
            "用户确认：CSV 字段顺序沿用订单列表。",
            "--resolve-question",
            "CSV 字段顺序",
        ],
        cwd=project_dir,
    )

    assert fragment_result.returncode == 1
    assert "必须传入完整问题文本" in fragment_result.stderr
    draft_after_fragment = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert draft_after_fragment["questions"] == [question]
    assert draft_after_fragment["decisions"] == []

    exact_result = run_cli(
        [
            "draft",
            "decision",
            "DRAFT-001",
            "用户确认：CSV 字段顺序沿用订单列表。",
            "--resolve-question",
            question,
        ],
        cwd=project_dir,
    )

    assert exact_result.returncode == 0, exact_result.stderr
    draft_after_exact = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert draft_after_exact["questions"] == []
    assert draft_after_exact["resolved_questions"] == [question]


def test_next_prioritizes_active_draft_when_active_requirement_exists(
    document_first_ready_project,
) -> None:
    project_dir, _paths = document_first_ready_project
    # 先建立第二份活动 DRAFT，再用同一项目里的 DRAFT-001 完成正式建档，
    # 这样 next 观察到的活动需求和活动 DRAFT 都来自真实 document-first 流程。
    assert run_cli(["draft", "create", "新需求确认稿"], cwd=project_dir).returncode == 0
    start_package, _package = write_document_first_formal_v3_package(project_dir)
    assert run_cli(["start", "--file", str(start_package)], cwd=project_dir).returncode == 0

    next_result = run_cli(["next"], cwd=project_dir)
    status_result = run_cli(["status"], cwd=project_dir)

    assert next_result.returncode == 0, next_result.stderr
    assert status_result.returncode == 0, status_result.stderr
    assert "- 主推荐：$sdlc-discuss 继续完善需求草案" in next_result.stdout
    assert "DRAFT-002 [discussing]" in next_result.stdout
    assert "其它活跃需求" in next_result.stdout
    assert "REQ-001 [planning]" in next_result.stdout
    assert "- 推荐：$sdlc-discuss 继续完善需求草案" in status_result.stdout


def test_start_blocks_draft_acceptance_without_executable_fields(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()

    def remove_executable_fields(document):
        acceptance = document["functional_requirements"][0]["acceptance_criteria"][0]
        for field in ("operation", "expected", "pass_standard"):
            acceptance.pop(field)

    overwrite_json(split_path, remove_executable_fields)
    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_draft_test_case_without_executable_fields(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()

    # document-first 测试矩阵由 AC 投影，缺少可执行字段时不能靠旧 TC 章节补齐。
    overwrite_json(
        split_path,
        lambda document: document["functional_requirements"][0][
            "acceptance_criteria"
        ][0].pop("pass_standard"),
    )
    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_file_requires_source_draft_when_start_ready_draft_exists(
    document_first_ready_project,
) -> None:
    project_dir, _paths = document_first_ready_project
    start_package, package = write_document_first_formal_v3_package(project_dir)
    # document-first 正式包不能省略来源，防止绕过当前 start_ready DRAFT。
    package.pop("source_draft_id")
    start_package.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    start_result = run_cli(["start", "--file", str(start_package)], cwd=project_dir)

    assert start_result.returncode == 1
    assert "source_draft_id" in start_result.stderr

    draft = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert draft["status"] == "start_ready"


def test_start_file_blocks_unfinished_draft_without_source_draft(
    document_first_ready_project,
) -> None:
    project_dir, _paths = document_first_ready_project
    start_package, package = write_document_first_formal_v3_package(project_dir)
    assert run_cli(["draft", "create", "待确认的新需求"], cwd=project_dir).returncode == 0
    package.pop("source_draft_id")
    start_package.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    start_result = run_cli(["start", "--file", str(start_package)], cwd=project_dir)

    assert start_result.returncode == 1
    assert "source_draft_id" in start_result.stderr
    state = derive_state(build_paths(project_dir))
    draft = state["drafts"]["DRAFT-002"]
    assert draft["status"] == "discussing"
    assert draft["questions"] == []
    assert "REQ-001" not in state["requirements"]


def test_start_file_with_source_draft_reuses_full_draft_quality_gate(
    document_first_ready_project,
) -> None:
    project_dir, _paths = document_first_ready_project
    assert run_cli(["draft", "create", "低质确认稿"], cwd=project_dir).returncode == 0
    requirement_file = project_dir / "低质需求草稿.md"
    requirement_file.write_text("# 需求草稿\n\n只记录一句摘要，没有范围、权限、接口、验收和测试矩阵。\n", encoding="utf-8")
    design_file = project_dir / "低质技术草稿.md"
    design_file.write_text("# 技术草稿\n\n只记录一句技术想法，没有模块、接口、状态流和错误处理。\n", encoding="utf-8")
    assert run_cli(["draft", "requirement", "DRAFT-002", "--file", str(requirement_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "design", "DRAFT-002", "--file", str(design_file)], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "status", "DRAFT-002"], cwd=project_dir).returncode == 0

    state_before = derive_state(build_paths(project_dir))
    # 低质量 DRAFT 无法生成与自身审核、修订和清单一致的正式包，拒绝发生在真实工厂边界。
    with pytest.raises(
        SdlcError,
        match="DRAFT-002 不是 start_ready，不能生成文档优先正式包",
    ):
        write_document_first_formal_v3_package(project_dir, draft_id="DRAFT-002")

    state = derive_state(build_paths(project_dir))
    assert state == state_before
    assert state["drafts"]["DRAFT-002"]["status"] == "discussing"
    assert "REQ-001" not in state["requirements"]


def test_start_file_with_source_draft_marks_draft_started(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    result, package, _requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )

    assert result.returncode == 0
    assert package["source_draft_id"] == "DRAFT-001"
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["status"] == "started"
    assert draft["started_requirement_id"] == "REQ-001"

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "$sdlc-start" not in next_result.stdout
    assert "REQ-001" in next_result.stdout
def test_light_start_offline_does_not_consume_unfinished_draft(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    discuss_result = create_structured_discussion(
        project_dir,
        increment="普通成员能否导出订单？",
        capture_type="question",
    )
    assert discuss_result.returncode == 0, discuss_result.stderr

    light_result = run_cli(["light-start", "修一个按钮文案"], cwd=project_dir)

    assert light_result.returncode == 0, light_result.stderr
    assert "light-start 已下线" in light_result.stdout
    assert "已纳入需求讨论草案" not in light_result.stdout
    state = derive_state(build_paths(project_dir))
    draft = state["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"
    assert draft["questions"] == []
    assert draft["assessment"]["open_questions"] == ["CAP-001"]
    assert not draft["started_requirement_id"]


def test_start_description_guides_to_draft_flow_instead_of_file_entry(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0

    start_result = run_cli_raw(["start", "优化设置页体验"], cwd=project_dir)
    help_result = run_cli_raw(["start", "--help"], cwd=project_dir)

    # 位置说明入口已经下线，帮助只公开 document-first 的显式文件入口。
    assert start_result.returncode == 1
    assert "必须使用 start --file" in start_result.stderr
    assert help_result.returncode == 0, help_result.stderr
    assert "--file PACKAGE_FILE" in help_result.stdout
    assert "document-first.v1" in help_result.stdout
def replace_markdown_section(text: str, heading: str, replacement: str) -> str:
    # 测试里经常要模拟用户删掉某个章节；这里按二级标题整段替换，避免手写一大份重复草稿。
    marker = f"## {heading}"
    start = text.index(marker)
    next_start = text.find("\n## ", start + len(marker))
    if next_start == -1:
        return text[:start] + replacement.rstrip() + "\n"
    return text[:start] + replacement.rstrip() + "\n" + text[next_start + 1 :]


def test_legacy_design_edits_cannot_replace_current_design_review(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    projection = paths.draft_dir("DRAFT-001") / "design.draft.md"
    projection.write_text(
        "# 技术草稿\n\n删除权限、安全和错误处理。\n",
        encoding="utf-8",
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert package["reviews"]["integrated_design"] == "REV-002"
    design = read_effective_json(
        paths.requirements_dir / "REQ-001",
        "design.current.json",
    )
    assert any(
        item["business_id"] == "SAFE-001"
        for item in package["artifact_manifest"]
    )
    assert any(
        item["artifact_id"] == "SAFE-001"
        for item in design["artifacts"]
    )
def test_start_blocks_draft_without_explicit_test_cases(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    split_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_split",
    )
    before_events = paths.events_file.read_bytes()

    # 旧独立 TC 已退出主线；当前等价门禁要求每个 FR 至少有结构化 AC。
    overwrite_json(
        split_path,
        lambda document: document["functional_requirements"][0].update(
            {"acceptance_criteria": []}
        ),
    )
    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_design_without_permission_security(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    security_path = formal_source_path(
        project_dir,
        package,
        artifact_type="design_artifact_json",
        business_id="SAFE-001",
    )
    before_events = paths.events_file.read_bytes()
    overwrite_json(
        security_path,
        lambda document: document["content"]["controls"][0].update(
            {"permissions": []}
        ),
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_design_missing_required_state_or_error_code(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    api_path = formal_source_path(
        project_dir,
        package,
        artifact_type="design_artifact_json",
        business_id="API-001",
    )
    before_events = paths.events_file.read_bytes()
    overwrite_json(
        api_path,
        lambda document: document["content"]["endpoints"][0].update(
            {"errors": []}
        ),
    )

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_blocks_design_without_requirement_coverage_section(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, package = write_document_first_formal_v3_package(project_dir)
    coverage_path = formal_source_path(
        project_dir,
        package,
        artifact_type="requirement_coverage",
    )
    before_events = paths.events_file.read_bytes()
    coverage_path.unlink()

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 1
    assert paths.events_file.read_bytes() == before_events
    assert not list(paths.requirements_dir.glob("REQ-*"))
def test_start_preserves_nested_draft_fr_ac_tc_fields(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    matrix = read_effective_json(requirement_dir, "test-matrix.current.json")
    point = requirement["functional_requirements"][0]
    acceptance = point["acceptance_criteria"][0]

    assert point["elements"] == ["课程标题", "课程内容"]
    assert point["flow"] == ["用户登录", "打开课程页", "系统展示课程"]
    assert point["facts"] == ["课程资料已经归档"]
    assert point["rules"] == ["只有登录用户可以查看课程"]
    assert point["constraints"] == ["不包含课程编辑"]
    assert point["states_and_exceptions"] == ["登录失效时拒绝访问"]
    assert acceptance["operation"] == "使用已登录用户打开课程页"
    assert acceptance["expected"] == "页面显示课程标题和内容"
    assert acceptance["pass_standard"] == "标题和内容完整显示且没有访问错误"
    assert matrix["acceptance_criteria"][0]["requirement_id"] == "FR-001"
def test_requirement_current_keeps_requirement_and_design_boundaries_separate(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    design = read_effective_json(requirement_dir, "design.current.json")
    requirement_text = json.dumps(requirement, ensure_ascii=False)
    design_text = json.dumps(design, ensure_ascii=False)

    assert "GET /users/{id}" not in requirement_text
    assert "USER_NOT_FOUND" not in requirement_text
    assert "用户数据访问控制" not in requirement_text
    assert "GET /users/{id}" in design_text
    assert "USER_NOT_FOUND" in design_text
    assert "用户数据访问控制" in design_text
def test_design_current_technical_goal_does_not_duplicate_full_draft(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    design = read_effective_json(requirement_dir, "design.current.json")
    artifacts = design["artifacts"]
    archive_paths = [item["archive_path"] for item in artifacts]
    business_ids = {
        item["artifact_id"]
        for item in artifacts
        if isinstance(item.get("artifact_id"), str)
    }

    # 当前设计正文由清单中的结构化产物组成，不再复制一份自由文本“技术目标”。
    assert len(archive_paths) == len(set(archive_paths))
    assert {"API-001", "COMP-001", "DATA-001", "PAGE-001", "SAFE-001"} <= business_ids
    assert "technical_goal" not in design
def test_requirement_current_does_not_mix_design_module_into_requirement_scope(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    _result, _package, requirement_dir = start_document_first_ready_project(
        project_dir,
        paths,
    )
    requirement = read_effective_json(requirement_dir, "requirement.current.json")
    design = read_effective_json(requirement_dir, "design.current.json")
    requirement_text = json.dumps(requirement, ensure_ascii=False)
    design_text = json.dumps(design, ensure_ascii=False)

    assert "用户摘要卡片" not in requirement_text
    assert "用户服务" not in requirement_text
    assert "用户摘要卡片" in design_text
    assert "用户服务" in design_text
def test_discuss_second_round_appends_without_losing_confirmed_content(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0
    confirmed_body = "# 需求草稿\n\n订单导出已经确认沿用当前订单列表字段。\n"
    assert run_cli(
        ["draft", "requirement", "DRAFT-001", confirmed_body], cwd=project_dir
    ).returncode == 0

    first = append_structured_cap(
        project_dir,
        submission_key="v15-export-interface",
        capture_type="fact",
        increment="订单导出接口必须使用 POST /api/orders/export，operator 可以导出订单",
    )
    assert first.returncode == 0, first.stderr
    second = append_structured_cap(
        project_dir,
        submission_key="v15-export-error",
        capture_type="fact",
        increment="导出失败返回 EXPORT_FAILED",
    )
    assert second.returncode == 0, second.stderr

    draft = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert draft["requirement_body"].strip() == confirmed_body.strip()
    assert [item["increment"] for item in draft["structured_captures"]] == [
        "订单导出接口必须使用 POST /api/orders/export，operator 可以导出订单",
        "导出失败返回 EXPORT_FAILED",
    ]


def test_discuss_structured_replacement_accepts_body_and_rechecks_contract(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    old_body = "\n".join(
        [
            "# 需求草稿",
            "",
            "## 背景和目标",
            "订单导出给运营使用。",
            "",
            "## 用户和使用场景",
            "- operator 在订单列表导出数据。",
            "",
            "## 本轮范围",
            "- POST /api/orders/export 导出订单。",
            "",
            "## 不做范围",
            "- 不做批量导入。",
            "",
            "## 功能需求",
            "### FR-001 订单导出",
            "- 说明：operator 调用 POST /api/orders/export 导出订单。",
            "",
            "## 权限规则",
            "- operator 可以导出订单。",
            "",
            "## 接口或页面范围",
            "- POST /api/orders/export。",
            "",
            "## 异常和边界",
            "- 导出失败返回 EXPORT_FAILED。",
            "",
            "## 验收标准",
            "### AC-001 导出成功",
            "- 覆盖需求：FR-001",
            "- 通过标准：operator 可以导出。",
            "",
            "## 测试关注点",
            "- 覆盖 operator 权限和 EXPORT_FAILED。",
            "",
            "## 测试矩阵",
            "### TC-001 operator 导出订单",
            "- 覆盖验收：AC-001",
            "- 覆盖需求：FR-001",
            "- 类型：manual_only",
            "- 操作：operator 导出订单。",
            "- 预期：返回导出文件。",
            "- 通过标准：导出成功。",
            "",
            "## 未确认问题",
            "",
        ]
    )
    assert run_cli(["draft", "create", "订单导出确认稿"], cwd=project_dir).returncode == 0
    assert run_cli(
        ["draft", "requirement", "DRAFT-001", old_body], cwd=project_dir
    ).returncode == 0

    losing_body = "\n".join(
        [
            "# 需求草稿",
            "",
            "## 背景和目标",
            "订单导出体验优化。",
            "",
            "## 用户和使用场景",
            "- 运营导出数据。",
            "",
            "## 本轮范围",
            "- 优化导出入口。",
            "",
            "## 不做范围",
            "- 不做批量导入。",
            "",
            "## 功能需求",
            "### FR-001 导出入口优化",
            "- 说明：优化导出入口。",
            "",
            "## 验收标准",
            "### AC-001 入口可见",
            "- 覆盖需求：FR-001",
            "- 通过标准：运营能看到入口。",
            "",
            "## 测试关注点",
            "- 覆盖入口展示。",
            "",
            "## 测试矩阵",
            "### TC-001 入口展示",
            "- 覆盖验收：AC-001",
            "- 覆盖需求：FR-001",
            "- 类型：manual_only",
            "- 操作：查看入口。",
            "- 预期：入口展示。",
            "- 通过标准：入口存在。",
            "",
            "## 未确认问题",
            "",
        ]
    )
    source = project_dir / "v15-replacement-attempt.txt"
    source.write_text(losing_body, encoding="utf-8")
    target = project_dir / ".codex-sdlc/drafts/DRAFT-001/requirement.draft.md"
    document = {
        "schema_version": "capture-increment.v1",
        "submission_key": "v15-replacement-attempt",
        "draft_id": "DRAFT-001",
        "client_key": "v15-replacement-attempt",
        "capture_type": "fact",
        "targets": [
            {
                "target_id": "DRAFT-001",
                "reference": reference(project_dir, target),
            }
        ],
        "source_reference": reference(project_dir, source),
        "source_sha256": sha256_file(source),
        "increment": losing_body,
        "status": "pending",
        "decisions": [],
    }
    input_file = project_dir / "v15-replacement-attempt.json"
    input_file.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = run_cli(["discuss", "--file", input_file.name], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    paths = build_paths(project_dir)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assessment = draft_lifecycle.assess_draft(draft)
    assert draft["status"] == "discussing"
    assert draft["requirement_body"].strip() == old_body.strip()
    assert "POST /api/orders/export" in draft["requirement_body"]
    assert "EXPORT_FAILED" in draft["requirement_body"]
    assert draft["structured_captures"][0]["increment"].strip() == losing_body.strip()
    assert assessment.effective_status == "discussing"
    assert {item.code for item in assessment.blockers} >= {
        "material_missing",
        "requirement_artifacts_missing",
        "pending_capture",
    }
    assert assessment.conflicts == ()
    assert assessment.can_start is False


def test_discuss_rough_requirement_stays_discussing_and_next_recommends_discuss(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0

    discuss_result = create_structured_discussion(
        project_dir,
        increment="优化设置页体验",
    )
    assert discuss_result.returncode == 0, discuss_result.stderr
    assert "已记录结构化 CAP：CAP-001" in discuss_result.stdout
    draft = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert draft["status"] == "discussing"

    next_result = run_cli(["next"], cwd=project_dir)
    assert next_result.returncode == 0, next_result.stderr
    assert "- 主推荐：$sdlc-material" in next_result.stdout
    assert "material_missing:DRAFT-001:missing" in next_result.stdout
    assert "requirement_artifacts_missing:DRAFT-001:missing" in next_result.stdout
    assert "$sdlc-design 技术方案草案" not in next_result.stdout.split("备选指令", 1)[0]


def test_start_ignores_markdown_display_text_when_structured_questions_are_empty(
    document_first_ready_project,
) -> None:
    project_dir, paths = document_first_ready_project
    package_file, _package = write_document_first_formal_v3_package(project_dir)
    projection = paths.draft_dir("DRAFT-001") / "requirement.draft.md"
    projection.write_text(
        projection.read_text(encoding="utf-8") + "\n- 暂无待确认问题\n",
        encoding="utf-8",
    )
    assert derive_state(paths)["drafts"]["DRAFT-001"]["questions"] == []

    result = run_cli_raw(["start", "--file", str(package_file)], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["status"] == "started"
    assert "已创建正式需求：REQ-001" in result.stdout
