from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_evidence import register_task_evidence
from test_task_run_contract import _activate_run


def _run_root(requirement_root: Path) -> Path:
    return requirement_root / "runtime/T-001/runs/0001"


def _write_source(requirement_root: Path, name: str, content: str) -> tuple[str, str]:
    source = _run_root(requirement_root) / "evidence" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    project = requirement_root.parents[2]
    return source.relative_to(project).as_posix(), sha256_file(source)


def _required_test(project: Path) -> str:
    task = derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]
    return str(task["test_items"][0])


def test_test_evidence_is_bound_to_current_run_and_keeps_original_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    source_file, source_sha256 = _write_source(
        requirement_root,
        "test.log",
        "命令原始输出：账号=真实测试账号\n1 passed\n",
    )

    record = register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="test",
        source_file=source_file,
        source_sha256=source_sha256,
        command="PYTHONPATH=src python3 -m pytest -q tests/test_contract.py",
        exit_code=0,
        result="passed",
        test_item=_required_test(project),
    )

    run = json.loads((_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8"))
    current = json.loads((requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8"))
    assert record == run["test_records"][0]
    assert record["run_number"] == 1
    assert record["source_file"] == source_file
    assert record["source_sha256"] == source_sha256
    assert record["command"].endswith("tests/test_contract.py")
    assert record["exit_code"] == 0
    assert current["task_run_sha256"] == sha256_file(_run_root(requirement_root) / "task-run.v1.json")
    failures = [
        ("../outside.log", source_sha256),
        (".codex-sdlc/不存在.log", source_sha256),
        (source_file, "0" * 64),
    ]
    for rejected_file, rejected_sha256 in failures:
        with pytest.raises(SdlcError):
            register_task_evidence(
                build_paths(project),
                requirement_id="REQ-001",
                task_id="T-001",
                kind="test",
                source_file=rejected_file,
                source_sha256=rejected_sha256,
                command="python3 -m pytest",
                exit_code=0,
                result="passed",
                test_item=_required_test(project),
            )

    run = json.loads((_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8"))
    assert run["test_records"] == [record]
    source_file, source_sha256 = _write_source(
        requirement_root,
        "manual.json",
        json.dumps({"summary": "人工验收通过"}, ensure_ascii=False),
    )
    with pytest.raises(SdlcError, match="人工验收"):
        register_task_evidence(
            build_paths(project),
            requirement_id="REQ-001",
            task_id="T-001",
            kind="verification",
            source_file=source_file,
            source_sha256=source_sha256,
            command="人工验收",
            exit_code=0,
            result="passed",
        )
    feedback = {
        "schema_version": "task-feedback.v1",
        "feedback_id": "FB-001",
        "requirement_id": "REQ-001",
        "task_id": "T-001",
        "run_number": 1,
        "source": {"type": "user", "received_at": "2026-07-21T23:30:00+08:00"},
        "content": "FR-001 需要增加导出格式。",
        "affected_refs": ["FR-001"],
        "changes_contract": True,
    }
    source_file, source_sha256 = _write_source(
        requirement_root,
        "feedback.json",
        json.dumps(feedback, ensure_ascii=False),
    )

    record = register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="feedback",
        source_file=source_file,
        source_sha256=source_sha256,
    )

    state = derive_state(build_paths(project))
    run = json.loads((_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8"))
    current = json.loads((requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8"))
    assert record["handling"] == "formal_change"
    assert record["change_id"].startswith("CHG-")
    assert state["requirements"]["REQ-001"]["changes"][-1]["change_id"] == record["change_id"]
    assert run["status"] == current["status"] == "stale"
