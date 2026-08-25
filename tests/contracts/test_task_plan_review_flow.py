from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.commands.plan_cmd import run_tasks_finalize
from codex_sdlc.core import task_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state, load_events
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_file
from codex_sdlc.services import review_service
from test_task_planning_code_evidence import (
    _project,
    _task_args,
    _write_task_submission,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _import_task_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    blocking_conditions: list[str] | None = None,
    review_issue_fixture: bool = False,
    task_title: str = "",
) -> tuple[Path, Path]:
    project, requirement_root = _project(tmp_path)
    submission = _write_task_submission(tmp_path / "正式任务输入", requirement_root)
    if review_issue_fixture:
        first_task_path = submission[1] / "main.task.v2.json"
        first_task = json.loads(first_task_path.read_text(encoding="utf-8"))
        repeated_task = deepcopy(first_task)
        repeated_task["client_key"] = "repeated"
        repeated_task["title"] = "重复交付规划证据"
        repeated_task["goal"] = "再次交付同一份任务覆盖和规划证据。"
        repeated_task["depends_on"] = ["@client:main"]
        _write_json(submission[1] / "repeated.task.v2.json", repeated_task)
        plan = json.loads(submission[0].read_text(encoding="utf-8"))
        plan["tasks"] = ["@client:main", "@client:repeated"]
        plan["dependencies"] = [
            {"from": "@client:main", "to": "@client:repeated"}
        ]
        _write_json(submission[0], plan)
        coverage = json.loads(submission[2].read_text(encoding="utf-8"))
        for field in ("functional_requirements", "design_artifacts"):
            for record in coverage[field].values():
                record["tasks"] = ["@client:main", "@client:repeated"]
        coverage["acceptance_criteria"]["AC-001"]["tasks"] = [
            "@client:main",
            "@client:repeated",
        ]
        _write_json(submission[2], coverage)
    if blocking_conditions is not None:
        task_file = submission[1] / "main.task.v2.json"
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["blocking_conditions"] = blocking_conditions
        _write_json(task_file, task)
    if task_title:
        task_file = submission[1] / "main.task.v2.json"
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task["title"] = task_title
        _write_json(task_file, task)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    assert run_tasks_finalize(_task_args(submission)) == 0
    return project, requirement_root


def _submission(
    request: dict[str, object],
    *,
    status: str,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": "review-result.v1",
        "review_id": request["review_id"],
        "stage": "task_plan",
        "owner_id": "REQ-001",
        "reviewer_run_id": "提交文件不能指定审核身份",
        "input_hashes": deepcopy(request["input_hashes"]),
        "status": status,
        "issues": issues,
        "notes": ["整套任务已按固定检查项完成审核。"],
        "reviewed_at": "2026-07-20T22:30:00+08:00",
    }


def _create_review(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    return review_service.create_review(
        build_paths(project),
        review_id="REV-999",
        stage="task_plan",
        owner_id="REQ-001",
        input_paths=["src/app.py"],
        required_checks=["调用方不能缩减固定检查项"],
    )


def _return_current_task_plan_for_revision(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通过正式审核入口退回当前任务包，让后续用例只关注修订依赖合同。"""

    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(
            request,
            status="needs_fix",
            issues=[
                {
                    "issue_id": "ISSUE-001",
                    "severity": "P1",
                    "title": "任务依赖需要修订",
                    "description": "任务依赖必须使用当前任务包内引用或其他需求的正式任务编号。",
                    "evidence_refs": ["T-001#depends_on"],
                    "affected_refs": ["T-001"],
                    "required_fix": "修正任务依赖后重新提交整套任务计划。",
                }
            ],
        ),
    )


def _add_second_task_to_submission(
    submission: tuple[Path, Path, Path],
) -> None:
    """生成两个无依赖任务，便于随后把修订输入改成正式编号交叉环。"""

    main_path = submission[1] / "main.task.v2.json"
    second = json.loads(main_path.read_text(encoding="utf-8"))
    second["client_key"] = "second"
    second["title"] = "实现第二项规划证据"
    _write_json(submission[1] / "second.task.v2.json", second)

    plan = json.loads(submission[0].read_text(encoding="utf-8"))
    plan["tasks"] = ["@client:main", "@client:second"]
    _write_json(submission[0], plan)

    coverage = json.loads(submission[2].read_text(encoding="utf-8"))
    for field in ("functional_requirements", "design_artifacts"):
        for record in coverage[field].values():
            record["tasks"] = ["@client:main", "@client:second"]
    coverage["acceptance_criteria"]["AC-001"]["tasks"] = [
        "@client:main",
        "@client:second",
    ]
    coverage["acceptance_criteria"]["AC-001"]["test_refs"] = [
        "@client:main#automated_tests/0",
        "@client:second#automated_tests/0",
    ]
    _write_json(submission[2], coverage)


def _set_submission_dependencies(
    submission: tuple[Path, Path, Path],
    dependencies: dict[str, list[str]],
) -> None:
    """同步 task.v2 与 task-plan.v2，避免用结构不一致掩盖依赖图问题。"""

    plan_dependencies: list[dict[str, str]] = []
    for client_key, dependency_ids in dependencies.items():
        task_path = submission[1] / f"{client_key}.task.v2.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["depends_on"] = dependency_ids
        _write_json(task_path, task)
        plan_dependencies.extend(
            {"from": dependency_id, "to": f"@client:{client_key}"}
            for dependency_id in dependency_ids
        )
    plan = json.loads(submission[0].read_text(encoding="utf-8"))
    plan["dependencies"] = plan_dependencies
    _write_json(submission[0], plan)


def _add_external_formal_task(
    project: Path,
    *,
    depends_on: list[str] | None = None,
) -> None:
    """通过正式事件加入其他需求任务，供修订依赖图合同使用。"""

    paths = build_paths(project)
    (project / "src/external.py").write_text("EXTERNAL_READY = True\n", encoding="utf-8")
    append_event(
        paths,
        event_type="requirement_created",
        source="合同测试",
        summary="创建外部依赖所属需求",
        payload={
            "title": "外部依赖需求",
            "description": "提供可由其他需求依赖的正式任务。",
            "folder_name": "REQ-002-外部依赖需求",
            "flow_type": "SDLC 原生正式流程",
        },
        requirement_id="REQ-002",
    )
    append_event(
        paths,
        event_type="task_created",
        source="合同测试",
        summary="创建外部正式任务",
        payload={
            "title": "外部正式任务",
            "summary": "为 REQ-001 提供已存在的正式前置任务。",
            "status": "done",
            "depends_on": depends_on or [],
            "output_files": ["src/external.py"],
            "test_items": ["外部任务已测试"],
            "manual_checks": ["外部任务已验收"],
        },
        requirement_id="REQ-002",
        task_id="T-009",
    )


def test_task_plan_uses_one_complete_managed_review_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)

    created = _create_review(project, monkeypatch)
    request = created["request"]

    assert created["action"] == "created"
    assert request["review_id"] == "REV-001"
    assert request["stage"] == "task_plan"
    assert request["owner_id"] == "REQ-001"
    assert request["required_checks"] == sorted(
        review_service.TASK_PLAN_REVIEW_CHECKS
    )
    assert "调用方不能缩减固定检查项" not in request["required_checks"]
    assert (
        requirement_root
        .relative_to(project)
        .joinpath("tasks/task-plan.v2.json")
        .as_posix()
        in request["input_paths"]
    )
    assert (
        requirement_root
        .relative_to(project)
        .joinpath("tasks/T-001.json")
        .as_posix()
        in request["input_paths"]
    )
    assert (
        requirement_root
        .relative_to(project)
        .joinpath("tasks/T-001.md")
        .as_posix()
        in request["input_paths"]
    )
    assert (
        requirement_root
        .relative_to(project)
        .joinpath("task-coverage.v1.json")
        .as_posix()
        in request["input_paths"]
    )
    assert (
        requirement_root
        .relative_to(project)
        .joinpath("reference-index.v1.json")
        .as_posix()
        in request["input_paths"]
    )
    snapshots = [
        path
        for path in request["input_paths"]
        if "任务审核输入-" in path
    ]
    assert len(snapshots) == 1
    snapshot = json.loads((project / snapshots[0]).read_text(encoding="utf-8"))
    assert snapshot["task_ids"] == ["T-001"]
    assert snapshot["task_plan"]["code_evidence"]["purpose"] == "task_planning"
    assert snapshot["task_coverage"]["functional_requirements"] == {
        "FR-001": {"status": "implemented", "tasks": ["T-001"]}
    }
    assert snapshot["reference_index"]["entries"]["FR-001"]
    assert snapshot["formal_input_paths"]
    assert snapshot["task_input_paths"]
    assert snapshot["code_input_paths"]
    assert "src/app.py" in snapshot["code_input_paths"]

    repeated = _create_review(project, monkeypatch)
    assert repeated["request"]["review_id"] == request["review_id"]
    registry = json.loads(
        (
            project / ".codex-sdlc/trust/reviews/registry.json"
        ).read_text(encoding="utf-8")
    )
    task_reviews = [
        record
        for record in registry["requests"].values()
        if record["request"]["stage"] == "task_plan"
        and record["request"]["owner_id"] == "REQ-001"
    ]
    assert len(task_reviews) == 1


def test_task_title_and_task_count_do_not_replace_independent_business_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _import_task_plan(
        tmp_path,
        monkeypatch,
        task_title="重复、冲突、错误依赖和遗漏检查",
    )
    paths = build_paths(project)
    request = _create_review(project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")

    review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="passed", issues=[]),
    )

    requirement = derive_state(paths)["requirements"]["REQ-001"]
    assert len(requirement["tasks"]) == 1
    assert requirement["tasks"][0]["title"] == "重复、冲突、错误依赖和遗漏检查"
    assert requirement["status"] == "ready_for_development"


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("coverage", "覆盖"),
        ("reference", "引用"),
        ("evidence", "代码证据"),
        ("blocking", "阻塞"),
    ],
)
def test_task_plan_review_rejects_four_deterministic_blockers_without_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
    expected: str,
) -> None:
    blockers = ["等待外部接口确认"] if problem == "blocking" else None
    project, requirement_root = _import_task_plan(
        tmp_path,
        monkeypatch,
        blocking_conditions=blockers,
    )
    if problem == "coverage":
        coverage_path = requirement_root / "task-coverage.v1.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage["functional_requirements"] = {}
        _write_json(coverage_path, coverage)
    elif problem == "reference":
        index_path = requirement_root / "reference-index.v1.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["entries"]["FR-001"]["sha256"] = "0" * 64
        _write_json(index_path, index)
    elif problem == "evidence":
        (project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")

    trust_root = project / ".codex-sdlc/trust/reviews"
    with pytest.raises(SdlcError, match=expected):
        _create_review(project, monkeypatch)

    assert not (trust_root / "registry.json").exists()
    quality_dir = requirement_root / "质检"
    assert not quality_dir.exists() or not list(quality_dir.iterdir())


def test_needs_fix_keeps_issues_and_passed_review_can_become_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _import_task_plan(
        tmp_path,
        monkeypatch,
        review_issue_fixture=True,
    )
    paths = build_paths(project)
    created = _create_review(project, monkeypatch)
    request = created["request"]
    task_bytes_before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in (project / ".codex-sdlc/requirements").rglob("*")
        if path.is_file() and "/tasks/" in path.as_posix()
    }
    issues = [
        {
            "issue_id": "ISSUE-001",
            "severity": "P1",
            "title": "任务边界重复",
            "description": "T-001 与 T-002 重复交付同一份规划证据，边界无法独立验收。",
            "evidence_refs": [
                "T-001#deliverables/0",
                "T-002#deliverables/0",
            ],
            "affected_refs": ["T-001", "T-002"],
            "required_fix": "合并重复交付或重新划清任务边界。",
        },
        {
            "issue_id": "ISSUE-002",
            "severity": "P1",
            "title": "任务依赖方向错误",
            "description": "T-002 依赖 T-001，但两项交付实际重复，当前依赖不能表达真实前置关系。",
            "evidence_refs": ["task-plan.v2#dependencies"],
            "affected_refs": ["T-001", "T-002"],
            "required_fix": "按真实交付顺序修正显式依赖。",
        },
    ]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    result = review_service.submit_review(
        paths,
        request_id=str(request["review_id"]),
        submission=_submission(request, status="needs_fix", issues=issues),
    )

    assert result["effective_status"] == "needs_fix"
    status = review_service.task_plan_review_status(
        paths,
        requirement_id="REQ-001",
    )
    assert status["can_advance"] is False
    assert status["reviews"][0]["issues"] == issues
    assert derive_state(paths)["requirements"]["REQ-001"]["status"] == "planning_tasks"
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in (project / ".codex-sdlc/requirements").rglob("*")
        if path.is_file() and "/tasks/" in path.as_posix()
    } == task_bytes_before

    # 已通过的新项目用于验证完整哈希失效；审核角色始终没有改任务文件。
    passed_project, requirement_root = _import_task_plan(
        tmp_path / "通过项目",
        monkeypatch,
    )
    passed_paths = build_paths(passed_project)
    passed_request = _create_review(passed_project, monkeypatch)["request"]
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    review_service.submit_review(
        passed_paths,
        request_id=str(passed_request["review_id"]),
        submission=_submission(passed_request, status="passed", issues=[]),
    )
    reused_passed = _create_review(passed_project, monkeypatch)
    assert reused_passed["action"] == "idempotent"
    assert reused_passed["effective_status"] == "passed"
    assert reused_passed["request"]["review_id"] == passed_request["review_id"]
    assert (
        derive_state(passed_paths)["requirements"]["REQ-001"]["status"]
        == "ready_for_development"
    )

    # 开工后允许任务声明的交付文件变化，避免正常开发把整套任务审核误判为失效。
    started_tasks = deepcopy(
        derive_state(passed_paths)["requirements"]["REQ-001"]["tasks"]
    )
    started_tasks[0]["status"] = "doing"
    (passed_project / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
    during_development = review_service.task_plan_review_status(
        passed_paths,
        requirement_id="REQ-001",
        tasks=started_tasks,
    )
    assert during_development["can_advance"] is True
    assert during_development["reviews"][0]["effective_status"] == "passed"
    append_event(
        passed_paths,
        event_type="task_updated",
        source="合同测试",
        summary="启动正式任务",
        payload={
            "status": "doing",
            "output_files": ["src/app.py"],
        },
        requirement_id="REQ-001",
        task_id="T-001",
    )
    public_status = review_service.review_status(
        passed_paths,
        review_id=str(passed_request["review_id"]),
    )
    assert public_status["reviews"][0]["effective_status"] == "passed"

    task_path = requirement_root / "tasks/T-001.json"
    task_path.write_bytes(task_path.read_bytes() + b"\n")
    stale = review_service.task_plan_review_status(
        passed_paths,
        requirement_id="REQ-001",
    )
    assert stale["reviews"][0]["recorded_status"] == "passed"
    assert stale["reviews"][0]["effective_status"] == "stale"
    assert stale["reviews"][0]["issues"] == []
    # 已经开工的任务保持运行状态；审核失效会留在状态证据中，但不会把任务倒退回规划阶段。
    assert (
        derive_state(passed_paths)["requirements"]["REQ-001"]["status"]
        == "developing"
    )


def test_task_plan_review_write_failure_and_stale_submit_leave_no_false_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    paths = build_paths(project)

    def reject_registration(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise SdlcError("审核登记写入失败。", exit_code=1)

    monkeypatch.setattr(
        review_service,
        "_register_review_request_locked",
        reject_registration,
    )
    with pytest.raises(SdlcError, match="登记写入失败"):
        _create_review(project, monkeypatch)
    assert not list((requirement_root / "质检").glob("任务审核输入-*.json"))
    assert not (
        project / ".codex-sdlc/trust/reviews/registry.json"
    ).exists()

    monkeypatch.undo()
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    request = _create_review(project, monkeypatch)["request"]
    task_path = requirement_root / "tasks/T-001.json"
    task_path.write_bytes(task_path.read_bytes() + b"\n")
    monkeypatch.setenv("CODEX_THREAD_ID", "独立任务审核任务")
    with pytest.raises(SdlcError, match="失效|变化"):
        review_service.submit_review(
            paths,
            request_id=str(request["review_id"]),
            submission=_submission(request, status="passed", issues=[]),
        )
    registry = json.loads(
        (
            project / ".codex-sdlc/trust/reviews/registry.json"
        ).read_text(encoding="utf-8")
    )
    record = registry["requests"][request["review_id"]]
    assert record["status"] == "pending"
    assert record["result_registration_id"] is None
    assert registry["registrations"] == {}


def test_needs_fix_allows_atomic_task_plan_revision_and_new_passed_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    capsys.readouterr()
    paths = build_paths(project)
    first_request = _create_review(project, monkeypatch)["request"]
    first_issues = [
        {
            "issue_id": "ISSUE-001",
            "severity": "P1",
            "title": "任务不能直接执行",
            "description": "任务缺少明确输入输出、测试命令和预期结果。",
            "evidence_refs": ["T-001#goal", "T-001#automated_tests/0"],
            "affected_refs": ["T-001", "AC-001"],
            "required_fix": "补齐明确行为、交付文件、测试命令和人工验收步骤。",
        }
    ]
    monkeypatch.setenv("CODEX_THREAD_ID", "第一轮独立审核任务")
    review_service.submit_review(
        paths,
        request_id=str(first_request["review_id"]),
        submission=_submission(
            first_request,
            status="needs_fix",
            issues=first_issues,
        ),
    )
    assert derive_state(paths)["requirements"]["REQ-001"]["status"] == "planning_tasks"

    revised_submission = _write_task_submission(
        tmp_path / "修订任务输入",
        requirement_root,
    )
    revised_task_path = revised_submission[1] / "main.task.v2.json"
    revised_task = json.loads(revised_task_path.read_text(encoding="utf-8"))
    revised_task.update(
        {
            "goal": "实现读取订单金额并返回含税总额的 calculate_total 函数。",
            "deliverables": [
                "src/app.py 提供 calculate_total(amount, tax_rate) 函数。",
                "tests/test_app.py 覆盖正常金额和非法负数输入。",
            ],
            "implementation_requirements": [
                "输入 amount 和 tax_rate 为数字，返回保留两位小数的含税总额。",
                "amount 小于零时抛出 ValueError，错误文字为“金额不能为负数”。",
            ],
            "data_api_page_component_requirements": [
                "函数输入为 amount、tax_rate，输出为浮点数；不涉及页面和外部接口。"
            ],
            "states_and_exceptions": [
                "正常输入返回计算结果；负数金额抛出 ValueError。"
            ],
            "automated_tests": [
                "PYTHONPATH=. python3 -m pytest -q tests/test_app.py"
            ],
            "manual_checks": [
                "运行 python3 -c \"from src.app import calculate_total; print(calculate_total(100, 0.06))\"，输出应为 106.0。"
            ],
            "definition_of_done": [
                "函数、异常、自动测试和人工命令全部符合 AC-001。"
            ],
        }
    )
    _write_json(revised_task_path, revised_task)

    protected_before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    }
    events_before = paths.events_file.read_bytes()
    review_registry = project / ".codex-sdlc/trust/reviews/registry.json"
    review_registry_before = review_registry.read_bytes()
    original_ensure_event = task_contract._ensure_event
    failed_once = False

    def fail_revision_event(
        current_paths: object,
        event: object,
    ) -> None:
        nonlocal failed_once
        if (
            not failed_once
            and isinstance(event, dict)
            and event.get("event_type") == "task_plan_revised"
        ):
            failed_once = True
            raise RuntimeError("模拟修订事件写入失败")
        original_ensure_event(current_paths, event)  # type: ignore[arg-type]

    monkeypatch.setattr(task_contract, "_ensure_event", fail_revision_event)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    with pytest.raises(RuntimeError, match="模拟修订事件写入失败"):
        run_tasks_finalize(_task_args(revised_submission))
    assert paths.events_file.read_bytes() == events_before
    assert review_registry.read_bytes() == review_registry_before
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    } == protected_before
    transaction_root = project / ".codex-sdlc/task-import-transactions"
    assert not transaction_root.exists() or not [
        path for path in transaction_root.rglob("*") if path.is_file()
    ]

    monkeypatch.setattr(task_contract, "_ensure_event", original_ensure_event)
    assert run_tasks_finalize(_task_args(revised_submission)) == 0
    revision_output = capsys.readouterr().out
    assert "已修订正式任务计划：REQ-001" in revision_output
    assert "--review-id REV-002" in revision_output

    events = load_events(paths)
    assert [
        event["event_type"]
        for event in events
        if event["event_type"] in {"task_plan_imported", "task_plan_revised"}
    ] == ["task_plan_imported", "task_plan_revised"]
    requirement = derive_state(paths)["requirements"]["REQ-001"]
    assert requirement["tasks"][0]["task_id"] == "T-001"
    assert requirement["tasks"][0]["task_contract"]["goal"].startswith(
        "实现读取订单金额"
    )
    receipt = json.loads(
        (requirement_root / "tasks/.task-import-receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["event_id"] == events[-1]["event_id"]
    assert receipt["mapping"] == {"main": "T-001"}
    assert all(
        sha256_file(requirement_root / "tasks" / relative_path) == digest
        for relative_path, digest in receipt["files"].items()
    )
    coverage = json.loads(
        (requirement_root / "task-coverage.v1.json").read_text(encoding="utf-8")
    )
    assert canonical_sha256(coverage) == receipt["coverage_sha256"]
    first_status = review_service.task_plan_review_status(
        paths,
        requirement_id="REQ-001",
    )
    assert first_status["reviews"][0]["recorded_status"] == "needs_fix"
    assert first_status["reviews"][0]["effective_status"] == "stale"
    assert first_status["reviews"][0]["issues"] == first_issues

    second_request = _create_review(project, monkeypatch)["request"]
    assert second_request["review_id"] == "REV-002"
    monkeypatch.setenv("CODEX_THREAD_ID", "第二轮独立审核任务")
    review_service.submit_review(
        paths,
        request_id=str(second_request["review_id"]),
        submission=_submission(
            second_request,
            status="passed",
            issues=[],
        ),
    )
    final_status = review_service.task_plan_review_status(
        paths,
        requirement_id="REQ-001",
    )
    assert [review["recorded_status"] for review in final_status["reviews"]] == [
        "needs_fix",
        "passed",
    ]
    assert final_status["reviews"][0]["issues"] == first_issues
    assert final_status["can_advance"] is True
    assert derive_state(paths)["requirements"]["REQ-001"]["status"] == (
        "ready_for_development"
    )


def test_revision_rejects_previous_formal_id_that_becomes_self_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    _return_current_task_plan_for_revision(project, monkeypatch)
    revised_submission = _write_task_submission(
        tmp_path / "自依赖修订输入",
        requirement_root,
    )
    _set_submission_dependencies(revised_submission, {"main": ["T-001"]})

    events_before = build_paths(project).events_file.read_bytes()
    formal_before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    }
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    with pytest.raises(SdlcError, match="当前任务包|@client|自依赖"):
        run_tasks_finalize(_task_args(revised_submission))

    assert build_paths(project).events_file.read_bytes() == events_before
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in requirement_root.rglob("*")
        if path.is_file()
    } == formal_before


def test_revision_rejects_previous_formal_ids_that_become_dependency_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _project(tmp_path)
    initial_submission = _write_task_submission(tmp_path / "双任务初版", requirement_root)
    _add_second_task_to_submission(initial_submission)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    assert run_tasks_finalize(_task_args(initial_submission)) == 0
    _return_current_task_plan_for_revision(project, monkeypatch)

    revised_submission = _write_task_submission(tmp_path / "交叉环修订输入", requirement_root)
    _add_second_task_to_submission(revised_submission)
    _set_submission_dependencies(
        revised_submission,
        {"main": ["T-002"], "second": ["T-001"]},
    )

    events_before = build_paths(project).events_file.read_bytes()
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    with pytest.raises(SdlcError, match="当前任务包|@client|依赖环"):
        run_tasks_finalize(_task_args(revised_submission))
    assert build_paths(project).events_file.read_bytes() == events_before


def test_revision_keeps_other_requirement_formal_task_as_external_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    _return_current_task_plan_for_revision(project, monkeypatch)
    _add_external_formal_task(project)

    revised_submission = _write_task_submission(
        tmp_path / "合法外部依赖修订输入",
        requirement_root,
    )
    _set_submission_dependencies(revised_submission, {"main": ["T-009"]})
    revised_plan = json.loads(revised_submission[0].read_text(encoding="utf-8"))
    revised_plan["code_evidence"]["upstream_outputs"].append("src/external.py")
    _write_json(revised_submission[0], revised_plan)
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")

    assert run_tasks_finalize(_task_args(revised_submission)) == 0
    saved_task = json.loads(
        (requirement_root / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    saved_plan = json.loads(
        (requirement_root / "tasks/task-plan.v2.json").read_text(encoding="utf-8")
    )
    assert saved_task["depends_on"] == ["T-009"]
    assert saved_plan["dependencies"] == [{"from": "T-009", "to": "T-001"}]


def test_revision_rejects_dependency_cycle_reaching_other_requirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _import_task_plan(tmp_path, monkeypatch)
    _return_current_task_plan_for_revision(project, monkeypatch)
    _add_external_formal_task(project, depends_on=["T-001"])

    revised_submission = _write_task_submission(
        tmp_path / "跨需求交叉环修订输入",
        requirement_root,
    )
    _set_submission_dependencies(revised_submission, {"main": ["T-009"]})
    revised_plan = json.loads(revised_submission[0].read_text(encoding="utf-8"))
    revised_plan["code_evidence"]["upstream_outputs"].append("src/external.py")
    _write_json(revised_submission[0], revised_plan)

    events_before = build_paths(project).events_file.read_bytes()
    monkeypatch.setenv("CODEX_THREAD_ID", "任务计划生产任务")
    with pytest.raises(SdlcError, match="依赖环"):
        run_tasks_finalize(_task_args(revised_submission))
    assert build_paths(project).events_file.read_bytes() == events_before
