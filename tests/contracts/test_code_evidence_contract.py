from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from codex_sdlc.core.code_evidence import (
    assess_code_evidence,
    capture_code_evidence,
    repository_identity,
    validate_code_evidence,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths


def _git_project(tmp_path: Path) -> tuple[Path, object]:
    project = tmp_path / "代码证据项目"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "合同测试"], cwd=project, check=True)
    (project / "AGENTS.md").write_text("# 项目规则\n", encoding="utf-8")
    (project / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (project / "src").mkdir()
    (project / "src/app.py").write_text(
        "# 这个值用于验证关联文件发生变化时只让对应证据失效。\nVALUE = 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "初始化证据夹具"], cwd=project, check=True)
    return project, build_paths(project)


def _selection() -> dict[str, object]:
    return {
        "purpose": "integrated_design",
        "rules": ["AGENTS.md"],
        "dependencies": ["package-lock.json"],
        "code_files": [{"path": "src/app.py", "reason_ref": "FR-001"}],
        "upstream_outputs": [],
    }


def test_non_git_directory_returns_formal_actionable_error(tmp_path: Path) -> None:
    project = tmp_path / "普通目录"
    project.mkdir()

    with pytest.raises(
        SdlcError,
        match="Git 仓库身份校验失败，请确认当前目录属于可用的 Git 工作树后重试",
    ):
        repository_identity(build_paths(project))


def test_capture_reads_real_files_and_records_git_identity(tmp_path: Path) -> None:
    project, paths = _git_project(tmp_path)
    evidence = capture_code_evidence(
        paths,
        owner_id="DRAFT-001",
        selection=_selection(),
    )

    identity = repository_identity(paths)
    assert evidence["repo_key"] == identity["repo_key"]
    assert evidence["worktree_key"] == identity["worktree_key"]
    assert evidence["git_head"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert evidence["rules"][0]["path"] == "AGENTS.md"
    assert evidence["dependencies"][0]["path"] == "package-lock.json"
    assert evidence["code_files"][0]["reason_ref"] == "FR-001"
    assert assess_code_evidence(paths, evidence)["status"] == "current"


def test_head_and_unrelated_dirty_file_do_not_invalidate_evidence(tmp_path: Path) -> None:
    project, paths = _git_project(tmp_path)
    evidence = capture_code_evidence(paths, owner_id="DRAFT-001", selection=_selection())
    (project / "无关文件.txt").write_text("无关变化\n", encoding="utf-8")
    subprocess.run(["git", "add", "无关文件.txt"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "只改无关文件"], cwd=project, check=True)
    (project / "另一个无关文件.txt").write_text("仍然无关\n", encoding="utf-8")

    assessment = assess_code_evidence(paths, evidence)

    assert assessment["status"] == "current"
    assert assessment["git_head_changed"] is True
    assert assessment["dirty_paths_changed"] is False
    assert assessment["changed_paths"] == []


@pytest.mark.parametrize("action", ["修改", "删除"])
def test_related_file_change_is_reported_as_stale(tmp_path: Path, action: str) -> None:
    project, paths = _git_project(tmp_path)
    evidence = capture_code_evidence(paths, owner_id="DRAFT-001", selection=_selection())
    target = project / "src/app.py"
    if action == "修改":
        target.write_text("VALUE = 2\n", encoding="utf-8")
    else:
        target.unlink()

    assessment = assess_code_evidence(paths, evidence)

    assert assessment["status"] == "stale"
    assert assessment["changed_paths"] == ["src/app.py"]


def test_selected_dirty_state_change_is_detected_even_when_file_bytes_stay_same(
    tmp_path: Path,
) -> None:
    project, paths = _git_project(tmp_path)
    target = project / "src/app.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    evidence = capture_code_evidence(paths, owner_id="DRAFT-001", selection=_selection())
    subprocess.run(["git", "add", "src/app.py"], cwd=project, check=True)

    assessment = assess_code_evidence(paths, evidence)

    assert assessment["status"] == "stale"
    assert assessment["dirty_paths_changed"] is True
    assert assessment["changed_paths"] == ["src/app.py"]


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing.py", "不存在"),
        ("src", "普通文件"),
        ("../outside.py", "上级目录"),
        ("/tmp/outside.py", "绝对路径"),
    ],
)
def test_missing_directory_and_outside_paths_are_rejected(
    tmp_path: Path,
    path: str,
    message: str,
) -> None:
    _project, paths = _git_project(tmp_path)
    selection = _selection()
    selection["code_files"] = [{"path": path, "reason_ref": "FR-001"}]

    with pytest.raises(SdlcError, match=message):
        capture_code_evidence(paths, owner_id="DRAFT-001", selection=selection)


def test_symlink_and_non_utf8_evidence_are_rejected(tmp_path: Path) -> None:
    project, paths = _git_project(tmp_path)
    (project / "src/link.py").symlink_to(project / "src/app.py")
    link_selection = _selection()
    link_selection["code_files"] = [{"path": "src/link.py", "reason_ref": "FR-001"}]
    with pytest.raises(SdlcError, match="符号链接"):
        capture_code_evidence(paths, owner_id="DRAFT-001", selection=link_selection)

    (project / "src/binary.dat").write_bytes(b"\xff\xfe\x00")
    binary_selection = _selection()
    binary_selection["code_files"] = [
        {"path": "src/binary.dat", "reason_ref": "FR-001"}
    ]
    with pytest.raises(SdlcError, match="UTF-8"):
        capture_code_evidence(paths, owner_id="DRAFT-001", selection=binary_selection)


def test_caller_hash_and_record_tampering_cannot_pass_contract(tmp_path: Path) -> None:
    _project, paths = _git_project(tmp_path)
    selection = _selection()
    selection["repo_key"] = "0" * 64
    selection["git_head"] = "0" * 40
    with pytest.raises(SdlcError, match="不能自报"):
        capture_code_evidence(paths, owner_id="DRAFT-001", selection=selection)

    evidence = capture_code_evidence(paths, owner_id="DRAFT-001", selection=_selection())
    tampered = deepcopy(evidence)
    tampered["code_files"][0]["sha256"] = "0" * 64
    with pytest.raises(SdlcError, match="哈希"):
        validate_code_evidence(tampered)


def test_evidence_json_round_trip_preserves_reproducible_snapshot(tmp_path: Path) -> None:
    _project, paths = _git_project(tmp_path)
    evidence = capture_code_evidence(paths, owner_id="DRAFT-001", selection=_selection())
    restored = json.loads(json.dumps(evidence, ensure_ascii=False))

    assert validate_code_evidence(restored) == evidence
    assert assess_code_evidence(paths, restored)["status"] == "current"
