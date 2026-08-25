from __future__ import annotations

import json
from copy import deepcopy
import argparse
from pathlib import Path

import pytest

from codex_sdlc.core import task_run
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import derive_state
from codex_sdlc.core.structured_contract import sha256_file
from codex_sdlc.core.task_evidence import register_task_evidence, validate_completion_evidence
from codex_sdlc.commands import regression_cmd
from test_task_run_contract import _activate_run


def _run_root(requirement_root: Path) -> Path:
    return requirement_root / "runtime/T-001/runs/0001"


def _source(requirement_root: Path, name: str, content: str) -> tuple[str, str]:
    path = _run_root(requirement_root) / "evidence" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    project = requirement_root.parents[2]
    return path.relative_to(project).as_posix(), sha256_file(path)


def _task(project: Path) -> dict[str, object]:
    return derive_state(build_paths(project))["requirements"]["REQ-001"]["tasks"][0]


def _record_test(
    project: Path,
    requirement_root: Path,
    *,
    result: str = "passed",
    exit_code: int = 0,
) -> None:
    source_file, source_sha256 = _source(requirement_root, f"test-{result}.log", f"{result}\n")
    register_task_evidence(
        build_paths(project),
        requirement_id="REQ-001",
        task_id="T-001",
        kind="test",
        source_file=source_file,
        source_sha256=source_sha256,
        command="python3 -m pytest -q",
        exit_code=exit_code,
        result=result,
        test_item=str(_task(project)["test_items"][0]),
    )


def _record_manual(project: Path, requirement_root: Path) -> None:
    manual_check = str(_task(project)["manual_checks"][0])
    document = {
        "environment": "仓库外临时 Git 项目",
        "checks": [
            {
                "item": manual_check,
                "expected": "任务 JSON 与 Markdown 内容一致",
                "actual": "逐字段核对一致",
                "result": "passed",
            }
        ],
    }
    source_file, source_sha256 = _source(
        requirement_root,
        "manual.json",
        json.dumps(document, ensure_ascii=False),
    )
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


def test_task_done_requires_all_tests_and_manual_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, requirement_root = _activate_run(tmp_path, monkeypatch)
    _record_test(project, requirement_root)
    active_run = json.loads(
        (_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8")
    )
    failed_run = deepcopy(active_run)
    failed_record = deepcopy(failed_run["test_records"][0])
    failed_record["evidence_id"] = "EVD-9999"
    failed_record["result"] = "failed"
    failed_record["exit_code"] = 1
    failed_run["test_records"].append(failed_record)
    with pytest.raises(SdlcError, match="失败"):
        validate_completion_evidence(
            build_paths(project), task=_task(project), run=failed_run
        )

    with pytest.raises(SdlcError, match="人工验收"):
        task_run.complete_task_run(build_paths(project), requirement_id="REQ-001", task_id="T-001")

    assert _task(project)["status"] == "doing"
    _record_manual(project, requirement_root)

    def interrupt(_run: dict[str, object]) -> None:
        raise OSError("故障注入：轮次已准备、当前指针尚未提交")

    monkeypatch.setattr(task_run, "_before_completion_current_commit", interrupt)
    with pytest.raises(SdlcError, match="完成事务失败"):
        task_run.complete_task_run(
            build_paths(project), requirement_id="REQ-001", task_id="T-001"
        )
    run = json.loads((_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8"))
    current = json.loads((requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8"))
    assert run["status"] == current["status"] == "active"
    assert _task(project)["status"] == "doing"

    monkeypatch.setattr(task_run, "_before_completion_current_commit", lambda _run: None)
    result = task_run.complete_task_run(
        build_paths(project), requirement_id="REQ-001", task_id="T-001"
    )
    run = json.loads((_run_root(requirement_root) / "task-run.v1.json").read_text(encoding="utf-8"))
    current = json.loads((requirement_root / "runtime/T-001/current.json").read_text(encoding="utf-8"))
    assert result["run"]["status"] == run["status"] == current["status"] == "closed"
    assert _task(project)["status"] == "done"
    assert regression_cmd.run(argparse.Namespace(items=["REQ-001", "T-001"])) == 0
