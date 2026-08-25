from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest


TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.structured_contract import sha256_bytes
from codex_sdlc.core.project import build_paths
from formal_package_factory import write_document_first_formal_v3_package
from test_cli_v1 import run_cli
from test_contract_cli_regressions import _ready_project


def _started_document_first_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object, Path, dict[str, object]]:
    project, paths, _package = _ready_project(tmp_path, monkeypatch)
    package_path, package = write_document_first_formal_v3_package(project)
    result = run_cli(["start", "--file", str(package_path)], cwd=project)
    assert result.returncode == 0, result.stdout + result.stderr
    requirement_dir = paths.requirements_dir / "REQ-001"
    assert requirement_dir.is_dir()
    return project, paths, requirement_dir, package


def _original_hashes(requirement_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(requirement_dir).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted((requirement_dir / "original").rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def document_first_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    """完整审核和建档只执行一次，各测试从同一干净快照恢复，避免重复跑慢速审核。"""

    root = tmp_path_factory.mktemp("t020-formal-reader")
    monkeypatch = pytest.MonkeyPatch()
    project, _paths, _requirement_dir, _package = _started_document_first_project(
        root,
        monkeypatch,
    )
    monkeypatch.undo()
    baseline = root / "正式档案基准"
    shutil.copytree(project, baseline)
    return project, baseline


@pytest.fixture
def document_first_project(
    document_first_template: tuple[Path, Path],
) -> tuple[Path, object, Path, dict[str, object]]:
    project, baseline = document_first_template
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(baseline, project)
    paths = build_paths(project)
    requirement_dir = paths.requirements_dir / "REQ-001"
    package = json.loads(
        (requirement_dir / "original/formal.v3.json").read_text(encoding="utf-8")
    )
    return project, paths, requirement_dir, package


def test_document_first_factory_is_separate_from_legacy_facts(
    document_first_project,
) -> None:
    project, _paths, _requirement_dir, package = document_first_project
    package_path = project / "DRAFT-001-formal.v3.json"

    assert package["formal_contract_version"] == "formal.v3"
    assert package["workflow_profile"] == "document-first.v1"
    assert "fact_bundle" not in package
    assert package_path.is_file()
    for legacy_name in (
        "source-index.json",
        "requirement.facts.json",
        "design.facts.json",
        "model-review.json",
    ):
        assert not package_path.with_name(legacy_name).exists()


def test_doctor_and_export_read_archive_paths_and_keep_original_unchanged(
    document_first_project,
) -> None:
    project, _paths, requirement_dir, package = document_first_project
    before = _original_hashes(requirement_dir)

    doctor = run_cli(["doctor"], cwd=project)
    exported = run_cli(["export-requirement", "REQ-001"], cwd=project)

    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "正式清单、原文、引用、状态和结构化版本一致" in doctor.stdout
    assert exported.returncode == 0, exported.stdout + exported.stderr
    export_text = (
        project / ".codex-sdlc/exports/REQ-001.md"
    ).read_text(encoding="utf-8")
    assert "### 正式档案" in export_text
    assert "流程档案：document-first.v1" in export_text
    assert f"正式清单：{len(package['artifact_manifest'])} 项" in export_text
    for item in package["artifact_manifest"]:
        assert f"`{item['archive_path']}`" in export_text
        assert f"SHA-256 `{item['sha256']}`" in export_text
    assert _original_hashes(requirement_dir) == before


def test_export_rebuilds_readable_state_without_markdown_projections(
    document_first_project,
) -> None:
    project, _paths, requirement_dir, _package = document_first_project
    before = _original_hashes(requirement_dir)
    for relative in (
        ".codex-sdlc/current.md",
        ".codex-sdlc/project.md",
    ):
        (project / relative).unlink()
    for relative in (
        "effective/requirement.current.md",
        "effective/design.current.md",
        "effective/test-matrix.current.md",
        "versions/requirement.v1.md",
        "versions/design.v1.md",
        "versions/test-matrix.v1.md",
        "traceability.md",
    ):
        (requirement_dir / relative).unlink()

    exported = run_cli(["export-requirement", "REQ-001"], cwd=project)
    repaired = run_cli(["doctor-repair"], cwd=project)

    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert "REQ-001" in exported.stdout
    assert "当前需求：`effective/requirement.current.json`" in exported.stdout
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert (project / ".codex-sdlc/current.md").is_file()
    assert (project / ".codex-sdlc/project.md").is_file()
    assert "正式引用索引只检查，不自动修改" in repaired.stdout
    assert _original_hashes(requirement_dir) == before


@pytest.mark.parametrize(
    "target_kind",
    [
        "archive_bytes",
        "reference_hash",
        "status_path",
        "formal_document",
    ],
)
def test_doctor_and_export_reject_formal_archive_drift_without_repairing(
    document_first_project,
    target_kind: str,
) -> None:
    project, _paths, requirement_dir, package = document_first_project
    export_path = project / ".codex-sdlc/exports/REQ-001.md"
    assert not export_path.exists()

    if target_kind == "archive_bytes":
        target = requirement_dir / str(package["artifact_manifest"][0]["archive_path"])
        target.write_bytes(target.read_bytes() + b"\n")
    elif target_kind == "reference_hash":
        target = requirement_dir / "reference-index.v1.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        first_id = sorted(document["entries"])[0]
        document["entries"][first_id]["sha256"] = "0" * 64
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    elif target_kind == "status_path":
        target = requirement_dir / "status.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document["current_files"]["requirement"] = "../drafts/DRAFT-001/status.json"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    else:
        target = requirement_dir / "original/formal.v3.json"
        document = json.loads(target.read_text(encoding="utf-8"))
        document["reviews"]["requirement_split"] = "REV-999"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    changed = target.read_bytes()

    doctor = run_cli(["doctor"], cwd=project)
    exported = run_cli(["export-requirement", "REQ-001"], cwd=project)

    assert doctor.returncode == 1
    assert "正式档案无效" in doctor.stdout
    assert exported.returncode == 1
    assert "正式档案无效" in exported.stderr
    assert not export_path.exists()
    assert target.read_bytes() == changed


def test_document_first_archive_does_not_require_legacy_facts(
    document_first_project,
) -> None:
    project, _paths, requirement_dir, _package = document_first_project
    assert not list(requirement_dir.rglob("*.facts.json"))

    doctor = run_cli(["doctor"], cwd=project)
    exported = run_cli(["export-requirement", "REQ-001"], cwd=project)

    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert "facts" not in (
        project / ".codex-sdlc/exports/REQ-001.md"
    ).read_text(encoding="utf-8")
