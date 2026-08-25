from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codex_sdlc.commands import review_cmd
from codex_sdlc.core import dependency_graph, fact_review_trust
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import canonical_sha256
from codex_sdlc.services import review_service


REPO_ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path, name: str = "审核项目") -> tuple[Path, object]:
    project = tmp_path / name
    project.mkdir()
    (project / "输入").mkdir()
    (project / "输入" / "原始需求.md").write_text("必须保留完成条件。\n", encoding="utf-8")
    (project / "输入" / "需求拆分.json").write_text('{"id":"FR-001"}\n', encoding="utf-8")
    (project / ".codex-sdlc").mkdir()
    (project / ".codex-sdlc" / "events.jsonl").write_text("", encoding="utf-8")
    return project, build_paths(project)


def _submission(request: dict, *, status: str = "passed", note: str = "") -> dict:
    issues = []
    if status == "needs_fix":
        issues = [
            {
                "issue_id": "ISSUE-001",
                "severity": "P1",
                "title": "遗漏完成条件",
                "description": "需求拆分没有保留原始资料中的完成条件。",
                "evidence_refs": ["输入/原始需求.md#完成条件"],
                "affected_refs": ["FR-001"],
                "required_fix": "补回完成条件并重新审核。",
            }
        ]
    return {
        "schema_version": "review-result.v1",
        "review_id": request["review_id"],
        "stage": request["stage"],
        "owner_id": request["owner_id"],
        "reviewer_run_id": "输入文件中的身份不可信",
        "input_hashes": deepcopy(request["input_hashes"]),
        "status": status,
        "issues": issues,
        "notes": [note] if note else [],
        "reviewed_at": "2026-07-16T04:00:00Z",
    }


def _create(
    paths,
    monkeypatch: pytest.MonkeyPatch,
    *,
    review_id: str = "REV-001",
    stage: str = "requirement_split",
    owner_id: str = "DRAFT-001",
    inputs: list[str] | None = None,
) -> dict:
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    return review_service.create_review(
        paths,
        review_id=review_id,
        stage=stage,
        owner_id=owner_id,
        input_paths=inputs or ["输入/原始需求.md", "输入/需求拆分.json"],
        required_checks=["检查完整覆盖"],
    )["request"]


def _submit(paths, request: dict, monkeypatch: pytest.MonkeyPatch, *, status: str = "passed") -> dict:
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    return review_service.submit_review(
        paths,
        request_id=request["review_id"],
        submission=_submission(request, status=status),
    )


def test_review_create_submit_status_entry_uses_real_files_and_current_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _paths = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_THREAD_ID", "formal-development-thread")
    assert review_cmd.main(
        [
            "review",
            "create",
            "--review-id",
            "REV-001",
            "--stage",
            "requirement_split",
            "--owner",
            "DRAFT-001",
            "--input",
            "输入/原始需求.md",
            "--input",
            "输入/需求拆分.json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["action"] == "created"
    assert created["request"]["producer_run_id"] == "formal-development-thread"

    result_file = project / "审核结果.json"
    result_file.write_text(
        json.dumps(_submission(created["request"]), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "independent-review-thread")
    assert review_cmd.main(
        ["review", "submit", "--request", "REV-001", "--file", str(result_file)]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["effective_status"] == "passed"
    assert submitted["reviewer_run_id"] == "independent-review-thread"

    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert review_cmd.main(["review", "status", "--review", "REV-001"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["can_advance"] is True
    assert status["reviews"][0]["effective_status"] == "passed"


def test_needs_fix_keeps_complete_issues_and_empty_issues_can_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir, paths = _project(tmp_path)
    request = _create(paths, monkeypatch)
    _submit(paths, request, monkeypatch, status="needs_fix")
    status = review_service.review_status(paths, review_id="REV-001")
    assert status["can_advance"] is False
    assert status["reviews"][0]["effective_status"] == "needs_fix"
    assert status["reviews"][0]["issues"] == _submission(request, status="needs_fix")["issues"]
    project_dir.joinpath("输入", "原始需求.md").write_text("完成条件变化。\n", encoding="utf-8")
    stale = review_service.review_status(paths, review_id="REV-001")["reviews"][0]
    assert stale["effective_status"] == "stale"
    assert stale["issues"] == _submission(request, status="needs_fix")["issues"]

    other_project, other_paths = _project(tmp_path, "无问题审核项目")
    assert other_project.exists()
    passed_request = _create(other_paths, monkeypatch)
    _submit(other_paths, passed_request, monkeypatch)
    passed = review_service.review_status(other_paths, review_id="REV-001")
    assert passed["reviews"][0]["issues"] == []
    assert passed["can_advance"] is True


def test_passed_is_reused_then_input_drift_becomes_stale_and_new_passed_is_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    first_request = _create(paths, monkeypatch)
    first_registration = _submit(paths, first_request, monkeypatch)

    monkeypatch.setenv("CODEX_THREAD_ID", "next-producer")
    reused = review_service.create_review(
        paths,
        review_id="REV-002",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=first_request["input_paths"],
        required_checks=first_request["required_checks"],
    )
    assert reused["action"] == "reused"
    assert reused["registration_id"] == first_registration["registration_id"]
    assert set(fact_review_trust.load_review_registry(paths)["requests"]) == {"REV-001"}

    (project / "输入" / "需求拆分.json").write_text('{"id":"FR-001","fixed":true}\n', encoding="utf-8")
    stale = review_service.review_status(paths, review_id="REV-001")
    assert stale["reviews"][0]["recorded_status"] == "passed"
    assert stale["reviews"][0]["effective_status"] == "stale"
    assert stale["reviews"][0]["can_advance"] is False
    assert stale["reviews"][0]["issues"] == []

    second_request = _create(paths, monkeypatch, review_id="REV-002")
    _submit(paths, second_request, monkeypatch)
    combined = review_service.review_status(paths)
    by_id = {item["review_id"]: item for item in combined["reviews"]}
    assert by_id["REV-001"]["effective_status"] == "stale"
    assert by_id["REV-001"]["is_current"] is False
    assert by_id["REV-002"]["effective_status"] == "passed"
    assert by_id["REV-002"]["is_current"] is True
    assert combined["can_advance"] is True


@pytest.mark.parametrize("damage", ["review_id", "stage", "owner_id", "missing_hash", "extra_hash"])
def test_submit_rejects_request_binding_changes_before_registry_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    _project_dir, paths = _project(tmp_path)
    request = _create(paths, monkeypatch)
    submission = _submission(request)
    if damage == "review_id":
        submission["review_id"] = "REV-999"
    elif damage == "stage":
        submission["stage"] = "integrated_design"
    elif damage == "owner_id":
        submission["owner_id"] = "DRAFT-999"
    elif damage == "missing_hash":
        submission["input_hashes"].pop(next(iter(submission["input_hashes"])))
    else:
        submission["input_hashes"]["输入/额外.md"] = "0" * 64
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    before = registry_path.read_bytes()
    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError):
        review_service.submit_review(paths, request_id="REV-001", submission=submission)
    assert registry_path.read_bytes() == before


def test_submit_rejects_missing_request_missing_identity_same_identity_and_different_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    request = _create(paths, monkeypatch)
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    before = registry_path.read_bytes()

    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError, match="请求不存在"):
        review_service.submit_review(paths, request_id="REV-999", submission=_submission(request))
    assert registry_path.read_bytes() == before

    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SdlcError, match="CODEX_THREAD_ID"):
        review_service.submit_review(paths, request_id="REV-001", submission=_submission(request))
    assert registry_path.read_bytes() == before

    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    with pytest.raises(SdlcError, match="必须使用不同"):
        review_service.submit_review(paths, request_id="REV-001", submission=_submission(request))
    assert registry_path.read_bytes() == before

    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    first = review_service.submit_review(paths, request_id="REV-001", submission=_submission(request))
    retry = review_service.submit_review(paths, request_id="REV-001", submission=_submission(request))
    assert retry["registration_id"] == first["registration_id"]
    committed = registry_path.read_bytes()
    different = _submission(request, note="另一份结果")
    with pytest.raises(SdlcError, match="已经被其他结果消费"):
        review_service.submit_review(paths, request_id="REV-001", submission=different)
    assert registry_path.read_bytes() == committed


def test_tampered_registration_is_rejected_by_submit_status_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    request = _create(paths, monkeypatch)
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["requests"]["REV-001"]["input_fingerprint"] = "0" * 64
    registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    tampered = registry_path.read_bytes()

    status = review_service.review_status(paths)
    assert status["status"] == "rejected"
    assert status["can_advance"] is False
    assert "已被改写" in status["rejection_reason"]
    assert derive_state(paths)["review_state"] == status

    monkeypatch.setenv("CODEX_THREAD_ID", "reviewer-run")
    with pytest.raises(SdlcError, match="已被改写"):
        review_service.submit_review(paths, request_id="REV-001", submission=_submission(request))
    assert registry_path.read_bytes() == tampered


def test_explicit_depends_on_and_applies_to_propagate_stale_without_text_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    (project / "输入" / "技术方案.json").write_text('{"version":1}\n', encoding="utf-8")
    (project / "输入" / "资料.bin").write_bytes(b"material-v1")
    dependency_graph.register_dependency_records(
        paths,
        [
            {"path": "输入/原始需求.md", "applies_to": [], "depends_on": []},
            {
                "path": "输入/需求拆分.json",
                "applies_to": [],
                "depends_on": ["输入/原始需求.md"],
            },
            {
                "path": "输入/资料.bin",
                "applies_to": [{"stage": "integrated_design", "owner_id": "DRAFT-002"}],
                "depends_on": [],
            },
            {"path": "输入/技术方案.json", "applies_to": [], "depends_on": []},
        ],
    )

    requirement_request = _create(
        paths,
        monkeypatch,
        review_id="REV-001",
        inputs=["输入/需求拆分.json"],
    )
    _submit(paths, requirement_request, monkeypatch)
    design_request = _create(
        paths,
        monkeypatch,
        review_id="REV-002",
        stage="integrated_design",
        owner_id="DRAFT-002",
        inputs=["输入/技术方案.json"],
    )
    _submit(paths, design_request, monkeypatch)

    (project / "输入" / "原始需求.md").write_text("完成条件已经变化。\n", encoding="utf-8")
    requirement_status = review_service.review_status(paths, review_id="REV-001")["reviews"][0]
    assert requirement_status["effective_status"] == "stale"
    assert requirement_status["stale_reasons"] == [
        {
            "kind": "depends_on_changed",
            "path": "输入/需求拆分.json",
            "source_paths": ["输入/原始需求.md"],
        }
    ]

    (project / "输入" / "资料.bin").write_bytes(b"material-v2")
    design_status = review_service.review_status(paths, review_id="REV-002")["reviews"][0]
    assert design_status["effective_status"] == "stale"
    assert {item["kind"] for item in design_status["stale_reasons"]} == {"applies_to_changed"}

    with pytest.raises(SdlcError, match="只包含"):
        dependency_graph.register_dependency_records(
            paths,
            [
                {
                    "path": "输入/技术方案.json",
                    "applies_to": [],
                    "depends_on": [],
                    "display_text": "不能参与依赖判断",
                }
            ],
        )


def test_old_fact_trust_records_do_not_become_new_review_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "old-fact-producer")
    requirement_run = fact_review_trust.record_fact_run(
        paths, draft_id="FORMAL", owner="requirement", artifact_sha256="1" * 64
    )
    design_run = fact_review_trust.record_fact_run(
        paths, draft_id="FORMAL", owner="design", artifact_sha256="2" * 64
    )
    fact_review_trust.create_review_request(
        paths,
        draft_id="FORMAL",
        target_sha256="3" * 64,
        fact_run_ids=[requirement_run["record_id"], design_run["record_id"]],
    )
    assert derive_state(paths)["review_state"] == {
        "status": "empty",
        "can_advance": False,
        "rejection_reason": "",
        "reviews": [],
    }


def _run_interrupt(project: Path, script: str, *, thread_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["CODEX_THREAD_ID"] = thread_id
    return subprocess.run(
        [sys.executable, "-c", script, str(project)],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_create_and_submit_real_interruptions_recover_without_half_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    create_script = r'''
import os
from pathlib import Path
import sys
from codex_sdlc.core import fact_review_trust
from codex_sdlc.core.project import build_paths
from codex_sdlc.services import review_service
project = Path(sys.argv[1])
original = os.replace
def interrupt(source, target):
    if Path(target).name == "registry.json":
        os._exit(73)
    original(source, target)
fact_review_trust.os.replace = interrupt
review_service.create_review(
    build_paths(project), review_id="REV-001", stage="requirement_split",
    owner_id="DRAFT-001", input_paths=["输入/原始需求.md"]
)
'''
    interrupted = _run_interrupt(project, create_script, thread_id="producer-run")
    assert interrupted.returncode == 73
    trust_dir = paths.sdlc_dir / "trust" / "reviews"
    assert not (trust_dir / "registry.json").exists()
    assert list(trust_dir.glob(".registry.json.*.tmp"))

    request = _create(paths, monkeypatch, inputs=["输入/原始需求.md"])
    assert not list(trust_dir.glob(".registry.json.*.tmp"))
    assert set(fact_review_trust.load_review_registry(paths)["requests"]) == {"REV-001"}

    submission_file = project / "结果.json"
    submission_file.write_text(json.dumps(_submission(request), ensure_ascii=False), encoding="utf-8")
    submit_script = r'''
import os
from pathlib import Path
import sys
from codex_sdlc.core import fact_review_trust
from codex_sdlc.core.project import build_paths
from codex_sdlc.services import review_service
project = Path(sys.argv[1])
original = os.replace
def interrupt(source, target):
    if Path(target).name == "registry.json":
        os._exit(74)
    original(source, target)
fact_review_trust.os.replace = interrupt
review_service.submit_review(
    build_paths(project), request_id="REV-001", submission_file=project / "结果.json"
)
'''
    interrupted_submit = _run_interrupt(project, submit_script, thread_id="reviewer-run")
    assert interrupted_submit.returncode == 74
    assert list(trust_dir.glob(".registry.json.*.tmp"))
    assert fact_review_trust.load_review_registry(paths)["requests"]["REV-001"]["status"] == "pending"

    _submit(paths, request, monkeypatch)
    assert not list(trust_dir.glob(".registry.json.*.tmp"))
    registry = fact_review_trust.load_review_registry(paths)
    assert registry["requests"]["REV-001"]["status"] == "completed"
    assert len(registry["registrations"]) == 1


def test_dependency_graph_real_interrupt_recovers_and_keeps_previous_file(
    tmp_path: Path,
) -> None:
    project, paths = _project(tmp_path)
    graph_script = r'''
import os
from pathlib import Path
import sys
from codex_sdlc.core import dependency_graph
from codex_sdlc.core.project import build_paths
project = Path(sys.argv[1])
original = os.replace
def interrupt(source, target):
    if Path(target).name == "dependency-graph.json":
        os._exit(75)
    original(source, target)
dependency_graph.os.replace = interrupt
dependency_graph.register_dependency_records(
    build_paths(project),
    [{"path":"输入/原始需求.md","applies_to":[],"depends_on":[]}],
)
'''
    interrupted = _run_interrupt(project, graph_script, thread_id="unused")
    assert interrupted.returncode == 75
    graph_path = dependency_graph.dependency_graph_path(paths)
    assert not graph_path.exists()
    assert list(graph_path.parent.glob(".dependency-graph.json.*.tmp"))

    graph = dependency_graph.register_dependency_records(
        paths,
        [{"path": "输入/原始需求.md", "applies_to": [], "depends_on": []}],
    )
    assert set(graph["artifacts"]) == {"输入/原始需求.md"}
    assert not list(graph_path.parent.glob(".dependency-graph.json.*.tmp"))


def test_dependency_change_reregister_keeps_old_snapshot_stale_and_new_review_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    dependency = project / "输入" / "外部资料.bin"
    dependency.write_bytes(b"v1")
    record = {
        "path": "输入/外部资料.bin",
        "applies_to": [{"stage": "requirement_split", "owner_id": "DRAFT-001"}],
        "depends_on": [],
    }
    dependency_graph.register_dependency_records(paths, [record])
    old_request = _create(paths, monkeypatch, review_id="REV-001", inputs=["输入/需求拆分.json"])
    _submit(paths, old_request, monkeypatch)

    dependency.write_bytes(b"v2")
    assert review_service.review_status(paths, review_id="REV-001")["reviews"][0]["effective_status"] == "stale"
    monkeypatch.setenv("CODEX_THREAD_ID", "new-producer")
    new_outcome = review_service.create_review(
        paths,
        review_id="REV-002",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["输入/需求拆分.json"],
        required_checks=["检查完整覆盖"],
    )
    assert new_outcome["action"] == "created"

    dependency_graph.register_dependency_records(paths, [record])
    old_status = review_service.review_status(paths, review_id="REV-001")
    assert old_status["reviews"][0]["effective_status"] == "stale"
    assert old_status["can_advance"] is False
    _submit(paths, new_outcome["request"], monkeypatch)
    combined = review_service.review_status(paths)
    assert {item["review_id"]: item["effective_status"] for item in combined["reviews"]} == {
        "REV-001": "stale",
        "REV-002": "passed",
    }
    assert combined["can_advance"] is True


def test_dependency_stale_passed_is_not_reused_when_direct_input_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    dependency = project / "输入" / "资料.bin"
    dependency.write_bytes(b"v1")
    dependency_graph.register_dependency_records(
        paths,
        [
            {
                "path": "输入/资料.bin",
                "applies_to": [{"stage": "requirement_split", "owner_id": "DRAFT-001"}],
                "depends_on": [],
            }
        ],
    )
    request = _create(paths, monkeypatch, inputs=["输入/需求拆分.json"])
    _submit(paths, request, monkeypatch)
    dependency.write_bytes(b"v2")

    monkeypatch.setenv("CODEX_THREAD_ID", "new-producer")
    outcome = review_service.create_review(
        paths,
        review_id="REV-002",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["输入/需求拆分.json"],
        required_checks=["检查完整覆盖"],
    )
    assert outcome["action"] == "created"
    assert outcome["effective_status"] == "pending"
    assert set(fact_review_trust.load_review_registry(paths)["requests"]) == {"REV-001", "REV-002"}


def test_signed_dependency_snapshot_tampering_is_rejected_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    _create(paths, monkeypatch)
    registry_path = paths.sdlc_dir / "trust" / "reviews" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["requests"]["REV-001"]["dependency_snapshot"]["closure_sha256"] = "0" * 64
    registry_path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    tampered = registry_path.read_bytes()

    status = review_service.review_status(paths)
    assert status["status"] == "rejected"
    assert "已被改写" in status["rejection_reason"]
    assert registry_path.read_bytes() == tampered


def test_explicit_old_passed_is_visible_but_never_can_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    old = _create(paths, monkeypatch, review_id="REV-001")
    _submit(paths, old, monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "next-producer")
    new = review_service.create_review(
        paths,
        review_id="REV-002",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=old["input_paths"],
        required_checks=["新的检查项"],
    )["request"]
    _submit(paths, new, monkeypatch, status="needs_fix")

    selected = review_service.review_status(paths, review_id="REV-001")
    assert selected["reviews"][0]["effective_status"] == "passed"
    assert selected["reviews"][0]["is_current"] is False
    assert selected["reviews"][0]["can_advance"] is False
    assert selected["can_advance"] is False


@pytest.mark.parametrize(
    "damage",
    ["self", "duplicate_applies", "duplicate_depends", "unsorted_applies", "unsorted_depends"],
)
def test_persisted_dependency_graph_rejects_invalid_relations_even_with_recomputed_digest(
    tmp_path: Path,
    damage: str,
) -> None:
    project, paths = _project(tmp_path)
    (project / "输入" / "A.json").write_text("{}\n", encoding="utf-8")
    (project / "输入" / "B.json").write_text("{}\n", encoding="utf-8")
    dependency_graph.register_dependency_records(
        paths,
        [
            {"path": "输入/A.json", "applies_to": [], "depends_on": []},
            {"path": "输入/B.json", "applies_to": [], "depends_on": []},
            {"path": "输入/需求拆分.json", "applies_to": [], "depends_on": []},
        ],
    )
    graph_path = dependency_graph.dependency_graph_path(paths)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    record = graph["artifacts"]["输入/A.json"]
    if damage == "self":
        record["depends_on"] = ["输入/A.json"]
    elif damage == "duplicate_applies":
        target = {"stage": "requirement_split", "owner_id": "DRAFT-001"}
        record["applies_to"] = [target, deepcopy(target)]
    elif damage == "duplicate_depends":
        record["depends_on"] = ["输入/B.json", "输入/B.json"]
    elif damage == "unsorted_applies":
        record["applies_to"] = [
            {"stage": "task_plan", "owner_id": "REQ-001"},
            {"stage": "requirement_split", "owner_id": "DRAFT-001"},
        ]
    else:
        record["depends_on"] = ["输入/需求拆分.json", "输入/B.json"]
    body = {"schema": graph["schema"], "artifacts": graph["artifacts"]}
    graph["graph_sha256"] = canonical_sha256(body)
    graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SdlcError):
        dependency_graph.load_dependency_graph(paths)


def test_create_hash_snapshot_and_registry_publish_share_one_process_lock(
    tmp_path: Path,
) -> None:
    project, paths = _project(tmp_path)
    creator_script = r'''
import os, sys, time
from pathlib import Path
from codex_sdlc.core import review_contract
from codex_sdlc.core.project import build_paths
from codex_sdlc.services import review_service
project = Path(sys.argv[1])
original = review_contract._hash_regular_file
signaled = False
def pause_after_hash(paths, relative_path):
    global signaled
    digest = original(paths, relative_path)
    if not signaled:
        signaled = True
        (project / "哈希完成.signal").write_text("ok")
        while not (project / "继续.signal").exists():
            time.sleep(0.01)
    return digest
review_contract._hash_regular_file = pause_after_hash
review_service.create_review(
    build_paths(project), review_id="REV-001", stage="requirement_split",
    owner_id="DRAFT-001", input_paths=["输入/需求拆分.json"]
)
(project / "创建完成.signal").write_text("ok")
'''
    writer_script = r'''
import sys
from pathlib import Path
from codex_sdlc.core.project import build_paths, project_lock
project = Path(sys.argv[1]); paths = build_paths(project)
(project / "写进程启动.signal").write_text("ok")
with project_lock(paths):
    registry_exists = (paths.sdlc_dir / "trust/reviews/registry.json").exists()
    (project / "输入/需求拆分.json").write_text('{"id":"FR-002"}\n')
    (project / "写入完成.signal").write_text("registry=" + str(registry_exists))
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["CODEX_THREAD_ID"] = "producer-run"
    creator = subprocess.Popen([sys.executable, "-c", creator_script, str(project)], cwd=project, env=env)
    for _ in range(300):
        if (project / "哈希完成.signal").exists():
            break
        import time
        time.sleep(0.01)
    assert (project / "哈希完成.signal").exists()
    writer = subprocess.Popen([sys.executable, "-c", writer_script, str(project)], cwd=project, env=env)
    for _ in range(100):
        if (project / "写进程启动.signal").exists():
            break
        import time
        time.sleep(0.01)
    assert (project / "写进程启动.signal").exists()
    import time
    time.sleep(0.1)
    assert not (project / "写入完成.signal").exists()
    (project / "继续.signal").write_text("go")
    assert creator.wait(timeout=10) == 0
    assert writer.wait(timeout=10) == 0
    assert (project / "写入完成.signal").read_text() == "registry=True"
    registry = fact_review_trust.load_review_registry(paths)
    assert set(registry["requests"]) == {"REV-001"}
    assert review_service.review_status(paths)["reviews"][0]["effective_status"] == "stale"
