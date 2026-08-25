from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_sdlc.core import dependency_graph, task_run
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
)
from codex_sdlc.services import change_service
from test_task_run_contract import _activate_run


def _runtime_documents(requirement_root: Path) -> tuple[dict[str, object], dict[str, object]]:
    run = json.loads(
        (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_text(
            encoding="utf-8"
        )
    )
    current = json.loads(
        (requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8")
    )
    return run, current


def test_affected_active_run_becomes_stale_before_task_is_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    result = task_run.protect_task_run_for_change(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        change_id="CHG-001",
    )
    run, current = _runtime_documents(requirement_root)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    assert run["status"] == current["status"] == "stale"
    assert task["status"] == "todo"
    assert result["run_status_after"] == "stale"
    assert (requirement_root / "runtime/T-001/runs/0001/task-read-manifest.v1.json").is_file()


def test_interrupted_stale_write_never_pauses_task_before_both_run_files_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)

    def interrupt(_run: dict[str, object]) -> None:
        raise OSError("故障注入：当前指针提交前中断")

    monkeypatch.setattr(task_run, "_before_run_status_current_commit", interrupt)
    with pytest.raises(SdlcError, match="状态同步失败"):
        task_run.protect_task_run_for_change(
            paths,
            requirement_id="REQ-001",
            task_id="T-001",
            change_id="CHG-001",
        )
    run, current = _runtime_documents(requirement_root)
    task = derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]
    assert run["status"] == "stale"
    assert current["status"] == "active"
    assert task["status"] == "doing"

    monkeypatch.setattr(task_run, "_before_run_status_current_commit", lambda _run: None)
    task_run.protect_task_run_for_change(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
        change_id="CHG-001",
    )
    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "stale"
    assert derive_state(paths)["requirements"]["REQ-001"]["tasks"][0]["status"] == "todo"


def test_unaffected_requires_full_task_references_and_unchanged_locator_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _requirement_root = _activate_run(tmp_path, monkeypatch)
    task = json.loads(
        (
            project
            / ".codex-sdlc/requirements/REQ-001-任务规划证据/tasks/T-001.json"
        ).read_text(encoding="utf-8")
    )
    reference_path = (
        project
        / ".codex-sdlc/requirements/REQ-001-任务规划证据/reference-index.v1.json"
    )
    base = json.loads(reference_path.read_text(encoding="utf-8"))
    projected = deepcopy(base)
    basis = ["FR-001", "GR-001", "AC-001", "DATA-001", "MAT-001"]
    proof = dependency_graph.prove_task_unaffected(
        task,
        basis_refs=basis,
        base_reference_index=base,
        projected_reference_index=projected,
    )
    assert proof["basis_refs"] == sorted(basis)
    assert proof["covered_task_refs"] == sorted(basis)

    projected["entries"]["FR-001"]["sha256"] = "0" * 64
    with pytest.raises(SdlcError, match="已经变化"):
        dependency_graph.prove_task_unaffected(
            task,
            basis_refs=basis,
            base_reference_index=base,
            projected_reference_index=projected,
        )
    with pytest.raises(SdlcError, match="不属于真实引用"):
        dependency_graph.prove_task_unaffected(
            task,
            basis_refs=["FR-999"],
            base_reference_index=base,
            projected_reference_index=base,
        )


def test_done_task_is_not_rewritten_by_change_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    append_event(
        paths,
        event_type="task_updated",
        source="合同测试",
        summary="任务已经完成",
        requirement_id="REQ-001",
        task_id="T-001",
        payload={"status": "done"},
    )
    before = (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_bytes()
    with pytest.raises(SdlcError, match="已完成任务"):
        task_run.protect_task_run_for_change(
            paths,
            requirement_id="REQ-001",
            task_id="T-001",
            change_id="CHG-001",
        )
    assert (requirement_root / "runtime/T-001/runs/0001/task-run.v1.json").read_bytes() == before


def _register_unaffected_protection(
    project: Path,
    paths: object,
    *,
    proof: dict[str, object],
) -> None:
    relative = (
        ".codex-sdlc/requirements/REQ-001-任务规划证据/changes/"
        "CHG-001/change-protection.v1.json"
    )
    body = {
        "schema_version": "change-protection.v1",
        "requirement_id": "REQ-001",
        "change_id": "CHG-001",
        "package_event_id": "EVT-20260722-000099",
        "package_identity_sha256": "a" * 64,
        "review_stages": [],
        "reviews": [],
        "requirement_confirmation": {"mode": "reused_unchanged_requirement"},
        "unaffected_tasks": [proof],
        "protected_tasks": [],
        "created_at": "2026-07-22T23:00:00+08:00",
    }
    document = {**body, "protection_sha256": canonical_sha256(body)}
    target = project / relative
    target.parent.mkdir(parents=True)
    reference_path = (
        project
        / ".codex-sdlc/requirements/REQ-001-任务规划证据/reference-index.v1.json"
    )
    reference_index = json.loads(reference_path.read_text(encoding="utf-8"))
    projected = {
        "schema_version": "projected-reference-index.v2",
        "requirement_id": "REQ-001",
        "change_id": "CHG-001",
        "base": {
            "path": reference_path.relative_to(project).as_posix(),
            "sha256": sha256_file(reference_path),
        },
        "content": reference_index,
        "content_sha256": canonical_sha256(reference_index),
    }
    (target.parent / "projected-reference-index.v2.json").write_text(
        canonical_json_text(projected), encoding="utf-8"
    )
    target.write_text(canonical_json_text(document), encoding="utf-8")
    append_event(
        paths,
        event_type="change_protected",
        source="sdlc-change-protect",
        summary="完成变更保护 CHG-001",
        requirement_id="REQ-001",
        payload={
            "requirement_id": "REQ-001",
            "change_id": "CHG-001",
            "protection_path": relative,
            "protection_sha256": document["protection_sha256"],
            "package_identity_sha256": "a" * 64,
        },
    )


def _real_unaffected_proof(project: Path) -> dict[str, object]:
    requirement_root = project / ".codex-sdlc/requirements/REQ-001-任务规划证据"
    task = json.loads(
        (requirement_root / "tasks/T-001.json").read_text(encoding="utf-8")
    )
    reference_index = json.loads(
        (requirement_root / "reference-index.v1.json").read_text(encoding="utf-8")
    )
    task_refs = sorted(
        {
            reference
            for field in (
                "requirement_refs",
                "global_rule_refs",
                "acceptance_refs",
                "design_refs",
                "material_refs",
            )
            for reference in task.get(field, [])
        }
    )
    return dependency_graph.prove_task_unaffected(
        task,
        basis_refs=task_refs,
        base_reference_index=reference_index,
        projected_reference_index=deepcopy(reference_index),
    )


def _use_registered_projected_context(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (
        project
        / ".codex-sdlc/requirements/REQ-001-任务规划证据/changes/CHG-001"
    )
    projected = json.loads(
        (workspace / "projected-reference-index.v2.json").read_text(encoding="utf-8")
    )

    def load_context(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "workspace": workspace,
            "payload": {"package_identity_sha256": "a" * 64},
            "documents": {"projected-reference-index.v2.json": projected},
        }

    # 当前用例只隔离验证证明内容；正式 change_package_projected 事件与六文件
    # 哈希由 change_service 合同测试和本任务仓库外正式入口共同覆盖。
    monkeypatch.setattr(change_service, "load_change_package_context_locked", load_context)


@pytest.mark.parametrize(
    "forgery",
    [
        "covered_task_refs缺少",
        "evidence缺少",
        "base_projected哈希不等",
        "proof_sha256伪造",
    ],
)
def test_forged_unaffected_protection_makes_existing_run_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    proof = _real_unaffected_proof(project)
    references = list(proof["covered_task_refs"])
    target = references[0]
    if forgery == "covered_task_refs缺少":
        proof["covered_task_refs"] = references[1:]
    elif forgery == "evidence缺少":
        del proof["evidence"][target]
    elif forgery == "base_projected哈希不等":
        proof["evidence"][target]["projected_sha256"] = "0" * 64
    else:
        proof["proof_sha256"] = "c" * 64
    _register_unaffected_protection(project, paths, proof=proof)
    _use_registered_projected_context(project, monkeypatch)

    with pytest.raises(SdlcError, match="TASK_RUN_STALE"):
        task_run.require_active_task_run(
            paths,
            requirement_id="REQ-001",
            task_id="T-001",
        )
    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "stale"


def test_complete_unaffected_protection_allows_existing_run_to_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    paths = build_paths(project)
    proof = _real_unaffected_proof(project)
    _register_unaffected_protection(project, paths, proof=proof)
    _use_registered_projected_context(project, monkeypatch)

    task_run.require_active_task_run(
        paths,
        requirement_id="REQ-001",
        task_id="T-001",
    )
    run, current = _runtime_documents(requirement_root)
    assert run["status"] == current["status"] == "active"
