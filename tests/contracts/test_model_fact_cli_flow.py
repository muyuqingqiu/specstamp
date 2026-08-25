from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys


TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import fact_artifacts, fact_gate, fact_review_trust
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, refresh_materialized_state
from codex_sdlc.core.structured_contract import sha256_bytes
from formal_package_factory import (
    build_valid_formal_v3_bundle,
    formal_v2_package,
    install_fixture_receipt,
    write_formal_v3_package,
)
from test_cli_v1 import init_demo_repo, run_cli_raw


def _establish_formal_receipt(project_dir: Path, package: Path, *, draft_id: str = "FORMAL") -> None:
    """保留给历史 facts 信任合同的兼容夹具；生产入口不再调用这条写入链。"""

    pair = [
        "--requirement-facts", str(package.with_name("requirement.facts.json")),
        "--design-facts", str(package.with_name("design.facts.json")),
        "--draft-id", draft_id,
    ]
    frozen = run_cli_raw(
        ["facts", "freeze", *pair],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "producer-thread"},
    )
    assert frozen.returncode == 0, frozen.stderr
    request = run_cli_raw(
        ["facts", "review-request", *pair],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "producer-thread"},
    )
    assert request.returncode == 0, request.stderr
    request_id = request.stdout.split("：", 1)[1].splitlines()[0].strip()
    submitted = run_cli_raw(
        [
            "facts",
            "review-submit",
            *pair,
            "--review",
            str(package.with_name("model-review.json")),
            "--request",
            request_id,
        ],
        cwd=project_dir,
        extra_env={"CODEX_THREAD_ID": "reviewer-thread"},
    )
    assert submitted.returncode == 0, submitted.stderr


def install_historical_fact_archive(
    project_dir: Path,
    *,
    title: str = "订单审批",
    requirement_id: str = "REQ-001",
    with_tasks: bool = True,
) -> Path:
    """直接安装一份历史只读档案，避免测试再借已下线的 facts 建档入口制造新状态。"""

    paths = build_paths(project_dir)
    data = formal_v2_package()
    data["title"] = title
    data["description"] = title
    formal, bundle = build_valid_formal_v3_bundle(data)
    receipt_id = install_fixture_receipt(
        project_dir,
        requirement=bundle["requirement"],
        design=bundle["design"],
        review=bundle["review"],
    )
    receipt = fact_review_trust.trusted_receipt(
        paths,
        receipt_id=receipt_id,
        target_sha256=fact_review_trust.review_target_sha256(
            bundle["requirement"],
            bundle["design"],
            bundle["review"]["targets"],
        ),
        review_sha256=fact_artifacts.artifact_sha256(bundle["review"]),
        draft_id="FORMAL",
        entry_scope="formal",
    )
    assert receipt is not None
    bundle["review_receipt"] = receipt
    native_start = deepcopy(formal)
    native_start["migration_status"] = "legacy_read_only"
    native_start["requirement_points"] = deepcopy(formal["functional_requirements"])
    native_start["acceptance_points"] = deepcopy(formal["acceptance_criteria"])
    native_start["test_cases"] = deepcopy(formal["test_cases"])
    folder_name = f"{requirement_id}-legacy-facts"
    append_event(
        paths,
        event_type="requirement_created",
        source="历史只读测试夹具",
        summary=f"安装历史只读档案 {requirement_id}",
        requirement_id=requirement_id,
        payload={
            "title": title,
            "description": title,
            "summary": title,
            "folder_name": folder_name,
            "flow_type": "历史 facts 只读档案",
            "native_start": native_start,
        },
    )
    if with_tasks:
        task_count = 3 if ("订单导出功能" in title or "订单导出按钮" in title) else 2
        for number in range(1, task_count + 1):
            task_id = f"T-{number:03d}"
            task_title = title if number == 1 else f"复核{title}第 {number} 项"
            append_event(
                paths,
                event_type="task_created",
                source="历史只读测试夹具",
                summary=f"创建历史任务 {task_id}",
                requirement_id=requirement_id,
                task_id=task_id,
                payload={
                    "title": task_title,
                    "summary": task_title,
                    "status": "todo",
                    "depends_on": [] if number == 1 else [f"T-{number - 1:03d}"],
                    "test_items": [f"验证：{task_title}"],
                    "manual_checks": [f"人工确认：{task_title}"],
                    "business_rules": [f"交付内容必须严格对应正式需求：{title}。"],
                    "coverage_points": ["FR-001"],
                    "coverage_tests": ["TC-001"],
                    "feedback_contract_version": "feedback.v1",
                    "feedback_state": "none",
                    "acceptance_feedback": [],
                    "formal_gate": False,
                    "note": "历史只读测试夹具只为备份命令准备既有任务。",
                },
            )
    refresh_materialized_state(paths)
    requirement_dir = paths.requirements_dir / folder_name
    original = requirement_dir / "original"
    # 历史正式包和旁路 facts 只作为已有档案落盘；新 profile 不读取也不生成这些文件。
    fact_artifacts.write_verified_bundle(requirement_dir, data, bundle)
    formal_bytes = fact_artifacts.canonical_json_bytes(formal) + b"\n"
    (original / "formal.v3.json").write_bytes(formal_bytes)
    fact_artifacts.write_json(
        requirement_dir / "formal-integrity.json",
        fact_artifacts.build_formal_integrity_index(
            requirement_dir,
            formal_bytes,
            bundle["manifest"],
        ),
    )
    return requirement_dir


def _original_hashes(requirement_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(requirement_dir).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted((requirement_dir / "original").rglob("*"))
        if path.is_file()
    }


def test_historical_fact_archive_remains_readable_without_joining_new_profile_gate(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    requirement_dir = install_historical_fact_archive(project_dir)
    before = _original_hashes(requirement_dir)

    status = run_cli_raw(["status"], cwd=project_dir)
    doctor = run_cli_raw(["doctor"], cwd=project_dir)
    exported = run_cli_raw(["export-requirement", "REQ-001"], cwd=project_dir)

    assert status.returncode == 0 and "REQ-001" in status.stdout
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert exported.returncode == 0, exported.stdout + exported.stderr
    formal = json.loads((requirement_dir / "original/formal.v3.json").read_text(encoding="utf-8"))
    assert formal["formal_contract_version"] == "formal.v3"
    assert "workflow_profile" not in formal
    assert _original_hashes(requirement_dir) == before


def test_legacy_fact_package_cannot_create_a_new_requirement(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli_raw(["init-basic"], cwd=project_dir).returncode == 0
    package = project_dir / "formal.v3.json"
    write_formal_v3_package(package)
    before = build_paths(project_dir).events_file.read_bytes()

    result = run_cli_raw(["start", "--file", str(package)], cwd=project_dir)

    assert result.returncode == 1
    assert "facts profile" in result.stderr or "workflow_profile" in result.stderr
    assert build_paths(project_dir).events_file.read_bytes() == before
    assert not list(build_paths(project_dir).requirements_dir.glob("REQ-*"))


def test_historical_fact_helpers_still_validate_existing_hash_and_review_contracts() -> None:
    formal, bundle = build_valid_formal_v3_bundle(formal_v2_package())

    assert formal["fact_bundle"] == bundle["manifest"]
    assert fact_gate.review_freshness(bundle).status == "passed"
    assert fact_review_trust.review_target_sha256(
        bundle["requirement"],
        bundle["design"],
        bundle["review"]["targets"],
    )
