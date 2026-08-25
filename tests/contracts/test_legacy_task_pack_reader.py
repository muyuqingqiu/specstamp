from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codex_sdlc.core.project import build_paths
from codex_sdlc.core.task_outputs import write_upstream_match
from codex_sdlc.legacy.task_pack_reader import (
    inspect_legacy_task_packs,
    read_legacy_task_pack,
)


def _requirement() -> dict[str, object]:
    return {
        "requirement_id": "REQ-001",
        "folder_name": "REQ-001-只读兼容",
        "tasks": [{"task_id": "T-001", "status": "todo"}],
    }


def _pack_dir(project: Path) -> Path:
    return project / ".codex-sdlc/requirements/REQ-001-只读兼容/task-packs/T-001"


def _write_complete_pack(project: Path) -> Path:
    pack_dir = _pack_dir(project)
    pack_dir.mkdir(parents=True)
    (pack_dir / "task-pack.md").write_text("# T-001 既有任务执行包\n", encoding="utf-8")
    (pack_dir / "task-pack.json").write_text(
        json.dumps(
            {
                "requirement_id": "REQ-001",
                "task_id": "T-001",
                "status": "ready",
                "context_files": [{"path": "src/feature.py"}],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return pack_dir


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_complete_legacy_task_pack_is_readable_and_never_joins_current_workflow(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pack_dir = _write_complete_pack(project)
    paths = build_paths(project)
    before = _tree_hashes(project)

    result = read_legacy_task_pack(paths, _requirement(), {"task_id": "T-001"})

    assert result.schema_version == "legacy-task-pack-read.v1"
    assert result.status == "readable"
    assert result.requirement_id == "REQ-001"
    assert result.task_id == "T-001"
    assert result.participates_in_current_workflow is False
    assert result.file_sha256 == {
        "task-pack.json": hashlib.sha256((pack_dir / "task-pack.json").read_bytes()).hexdigest(),
        "task-pack.md": hashlib.sha256((pack_dir / "task-pack.md").read_bytes()).hexdigest(),
    }
    assert result.issues == ()
    assert _tree_hashes(project) == before


def test_missing_legacy_task_pack_is_explicit_and_does_not_block_current_workflow(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = read_legacy_task_pack(build_paths(project), _requirement(), {"task_id": "T-001"})

    assert result.status == "missing"
    assert result.participates_in_current_workflow is False
    assert result.issues == ()


@pytest.mark.parametrize(
    ("mutation", "issue_code"),
    [
        ("invalid_json", "json_invalid"),
        ("missing_field", "field_missing"),
        ("outside_path", "path_outside"),
    ],
)
def test_damaged_legacy_task_pack_is_reported_without_repair(
    tmp_path: Path,
    mutation: str,
    issue_code: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pack_dir = _write_complete_pack(project)
    json_path = pack_dir / "task-pack.json"
    if mutation == "invalid_json":
        json_path.write_text("{broken\n", encoding="utf-8")
    elif mutation == "missing_field":
        json_path.write_text('{"requirement_id":"REQ-001","status":"ready"}\n', encoding="utf-8")
    else:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        data["context_files"] = [{"path": "../outside.txt"}]
        json_path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    before = _tree_hashes(project)

    result = read_legacy_task_pack(build_paths(project), _requirement(), {"task_id": "T-001"})

    assert result.status == "damaged"
    assert issue_code in {item.code for item in result.issues}
    assert result.participates_in_current_workflow is False
    assert _tree_hashes(project) == before


def test_symlink_legacy_task_pack_is_reported_without_following_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pack_dir = _write_complete_pack(project)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"保持原样"}\n', encoding="utf-8")
    (pack_dir / "task-pack.json").unlink()
    (pack_dir / "task-pack.json").symlink_to(outside)
    outside_before = outside.read_bytes()

    result = read_legacy_task_pack(build_paths(project), _requirement(), {"task_id": "T-001"})

    assert result.status == "damaged"
    assert "symlink" in {item.code for item in result.issues}
    assert result.metadata is None
    assert outside.read_bytes() == outside_before


def test_reader_scan_reports_existing_archives_without_requiring_missing_archives(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write_complete_pack(project)
    requirement = _requirement()
    state = {"requirements": {"REQ-001": requirement}}

    existing = inspect_legacy_task_packs(build_paths(project), state)
    including_missing = inspect_legacy_task_packs(build_paths(project), state, include_missing=True)

    assert [item.status for item in existing] == ["readable"]
    assert [item.status for item in including_missing] == ["readable"]


def test_read_only_consumers_do_not_import_task_pack_write_module() -> None:
    root = Path(__file__).resolve().parents[2]
    consumers = [
        "src/codex_sdlc/commands/doctor_cmd.py",
        "src/codex_sdlc/core/state.py",
        "src/codex_sdlc/core/task_outputs.py",
        "src/codex_sdlc/core/backup.py",
        "src/codex_sdlc/commands/docs_cmd.py",
    ]
    for relative in consumers:
        source = (root / relative).read_text(encoding="utf-8")
        assert "codex_sdlc.core.task_pack" not in source, relative


def test_write_and_workflow_modules_do_not_import_legacy_reader() -> None:
    root = Path(__file__).resolve().parents[2] / "src/codex_sdlc"
    allowed = {
        "commands/doctor_cmd.py",
        "commands/docs_cmd.py",
        "core/backup.py",
        "core/state.py",
        "core/task_outputs.py",
        "legacy/task_pack_reader.py",
    }
    import_text = "codex_sdlc.legacy.task_pack_reader"
    unexpected = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if import_text in path.read_text(encoding="utf-8") and relative not in allowed:
            unexpected.append(relative)
    assert unexpected == []


def test_task_output_match_is_written_outside_legacy_task_pack(tmp_path: Path) -> None:
    project = tmp_path / "project"
    requirement_root = project / ".codex-sdlc/requirements/REQ-001-只读兼容"
    requirement_root.mkdir(parents=True)
    paths = build_paths(project)

    output = write_upstream_match(
        paths,
        _requirement(),
        {"task_id": "T-001"},
        {"strong": [], "candidates": [], "log": []},
    )

    assert output == requirement_root / "task-outputs/upstream-matches/T-001.json"
    assert output.is_file()
    assert not (requirement_root / "task-packs").exists()
