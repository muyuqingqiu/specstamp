from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
for import_path in (TESTS_ROOT, Path(__file__).resolve().parent):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import load_events
from test_change_workspace_contract import _minimal_formal_project
from test_cli_v1 import init_demo_repo, run_cli, run_cli_raw
from test_design_reference_contract import (
    create_confirmed_design_project,
    import_reference,
    write_design_reference,
)


COMMAND_FILES = (
    REPO_ROOT / "src/codex_sdlc/commands/design_cmd.py",
    REPO_ROOT / "src/codex_sdlc/commands/material_cmd.py",
)
FORBIDDEN_MODULES = {
    "codex_sdlc.core.task_pack",
    "codex_sdlc.core.task_pack_contract",
    "codex_sdlc.commands.brief_cmd",
    "codex_sdlc.commands.brief_augment_cmd",
}


def _task_pack_paths(project: Path) -> list[str]:
    """只按真实目录名检查写入结果，不把普通说明文字误算成任务包。"""

    root = project / ".codex-sdlc"
    if not root.exists():
        return []
    return sorted(
        path.relative_to(project).as_posix()
        for path in root.rglob("*")
        if path.name in {"task-pack", "task-packs"}
    )


def test_draft_design_and_material_stay_in_owned_directories_without_task_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _source_bytes, material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
    )

    imported = import_reference(project, write_design_reference(project))

    assert imported.returncode == 0, imported.stderr
    assert (paths.draft_dir("DRAFT-001") / str(material["stored_path"])).is_file()
    assert paths.draft_design_reference_index_file("DRAFT-001").is_file()
    assert ".codex-sdlc/drafts/DRAFT-001/设计/des-index.v1.json" in imported.stdout
    assert "brief" not in imported.stdout.lower()
    assert _task_pack_paths(project) == []


def test_formal_requirement_design_change_is_rejected_to_chg_without_writes(
    tmp_path: Path,
) -> None:
    project, _requirement_dir = _minimal_formal_project(tmp_path)
    paths = build_paths(project)
    before = paths.events_file.read_bytes()
    designs_dir = project / ".codex-sdlc/designs"
    before_design_files = sorted(
        path.relative_to(project).as_posix()
        for path in designs_dir.rglob("*")
        if path.is_file()
    )

    result = run_cli_raw(
        ["design", "REQ-001", "把正式接口设计改成新版本"],
        cwd=project,
    )

    assert result.returncode == 1
    assert "正式 REQ" in result.stderr
    assert "change-create" in result.stderr
    assert "CHG" in result.stderr
    assert "<" not in result.stderr and ">" not in result.stderr
    assert "brief" not in result.stderr.lower()
    assert paths.events_file.read_bytes() == before
    assert sorted(
        path.relative_to(project).as_posix()
        for path in designs_dir.rglob("*")
        if path.is_file()
    ) == before_design_files
    assert _task_pack_paths(project) == []


def test_material_rejects_formal_req_task_and_unknown_owner_before_writes(
    tmp_path: Path,
) -> None:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project).returncode == 0
    assert run_cli(["draft", "create", "资料分流测试"], cwd=project).returncode == 0
    source = project / "资料.md"
    source.write_text("资料原文\n", encoding="utf-8")
    paths = build_paths(project)
    before_events = load_events(paths)

    formal = run_cli(
        [
            "material",
            "REQ-001",
            "--type",
            "requirement",
            "--title",
            "正式资料",
            "--file",
            source.name,
        ],
        cwd=project,
    )
    task = run_cli(
        [
            "material",
            "T-001",
            "--type",
            "field-evidence",
            "--title",
            "任务证据",
            "--file",
            source.name,
        ],
        cwd=project,
    )
    unknown = run_cli(
        [
            "material",
            "UNKNOWN-001",
            "--type",
            "other",
            "--title",
            "归属不明资料",
            "--file",
            source.name,
        ],
        cwd=project,
    )

    assert formal.returncode == task.returncode == unknown.returncode == 1
    assert "change-material" in formal.stderr and "task-evidence" in formal.stderr
    assert "task-evidence" in task.stderr
    assert "归属" in unknown.stderr
    assert load_events(paths) == before_events
    assert not list(paths.draft_dir("DRAFT-001").rglob("MAT-*"))
    assert _task_pack_paths(project) == []


def test_design_and_material_commands_do_not_import_task_pack_writers_or_recommend_brief() -> None:
    for command_file in COMMAND_FILES:
        source = command_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(command_file))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        assert imported_modules.isdisjoint(FORBIDDEN_MODULES)
        assert "task_pack" not in source
        assert "task-pack" not in source
        assert "brief" not in source.lower()
