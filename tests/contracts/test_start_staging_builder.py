from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.reference_index import build_reference_index_document
from codex_sdlc.core.start_staging import (
    build_prepared_start_staging,
    cleanup_incomplete_start_staging,
    validate_prepared_start_staging,
)
from codex_sdlc.core import start_staging
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    sha256_bytes,
)
from codex_sdlc.services import start_service
from test_reference_index_contract import _fixture as reference_fixture
from test_contract_cli_regressions import _ready_project


def _copy_tree(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())


def _workspace(root: Path) -> tuple[ProjectPaths, dict[str, object]]:
    source_fixture = reference_fixture(root / "reference-fixture")
    paths = ProjectPaths(root / "project")
    draft_dir = paths.draft_dir("DRAFT-001")
    draft_dir.mkdir(parents=True)
    paths.requirements_dir.mkdir(parents=True)
    _copy_tree(source_fixture["source"], draft_dir)  # type: ignore[arg-type]

    artifact_index = source_fixture["artifact_index"]
    index_bytes = canonical_json_text(artifact_index).encode("utf-8")
    (draft_dir / "artifact-index.v1.json").write_bytes(index_bytes)

    archive = source_fixture["archive"]
    reference_index = build_reference_index_document(
        source_fixture["source"],  # type: ignore[arg-type]
        "REQ-001",
        source_fixture["manifest"],  # type: ignore[arg-type]
        archive_root=archive,  # type: ignore[arg-type]
        requirement_mapping=source_fixture["requirement_mapping"],  # type: ignore[arg-type]
        design_references=[source_fixture["design"]],  # type: ignore[list-item]
        design_artifacts=[source_fixture["module"]],  # type: ignore[list-item]
        material_references=source_fixture["material_references"],  # type: ignore[arg-type]
    )
    package = {
        "formal_contract_version": "formal.v3",
        "workflow_profile": "document-first.v1",
        "source_draft_id": "DRAFT-001",
        "source_revision_sha256": artifact_index["draft_revision_sha256"],
        "reviews": {
            "requirement_split": "REV-001",
            "integrated_design": "REV-002",
        },
        "artifact_index": {
            "source_path": "artifact-index.v1.json",
            "archive_path": "original/artifact-index.v1.json",
            "sha256": sha256_bytes(index_bytes),
        },
        "artifact_manifest": deepcopy(source_fixture["manifest"]),
        "open_questions": [],
    }
    preflight = {
        "mode": "document-first",
        "package": package,
        "source_draft_id": "DRAFT-001",
        "requirement_id": "REQ-001",
        "target_directory": "REQ-001",
        "reference_index": reference_index,
    }
    paths.events_file.write_bytes(b'{"event_id":"EVT-001"}\n')
    paths.database_file.write_bytes(b"sqlite-before")
    return paths, preflight


def _business_snapshot(paths: ProjectPaths) -> dict[str, Any]:
    def snapshot_tree(root: Path) -> dict[str, tuple[str, bytes | str]]:
        if not root.exists():
            return {}
        result: dict[str, tuple[str, bytes | str]] = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", path.read_bytes())
            elif path.is_dir():
                result[relative] = ("dir", "")
        return result

    return {
        "events": paths.events_file.read_bytes(),
        "database": paths.database_file.read_bytes(),
        "draft": snapshot_tree(paths.drafts_dir),
        "requirements": snapshot_tree(paths.requirements_dir),
    }


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
    result: dict[str, tuple[str, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("dir", "")
    return result


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.write_text(canonical_json_text(document), encoding="utf-8")


def _transaction(staging: Path) -> dict[str, object]:
    return json.loads(
        (staging / "start-transaction.json").read_text(encoding="utf-8")
    )


def _rewrite_generated_json(
    staging: Path,
    transaction: dict[str, object],
    relative: str,
    document: dict[str, object],
) -> None:
    content = canonical_json_text(document).encode("utf-8")
    (staging / relative).write_bytes(content)
    transaction["generated_files"][relative] = sha256_bytes(content)  # type: ignore[index]


def _rewrite_transaction_identity(
    staging: Path,
    transaction: dict[str, object],
) -> Path:
    formal = json.loads(
        (staging / "original/formal.v3.json").read_text(encoding="utf-8")
    )
    transaction_id = start_staging._transaction_id_from_records(
        transaction,
        formal,
    )
    suffix = staging.name.rsplit("-", 1)[-1]
    new_name = f"start-{transaction_id[6:22]}-{suffix}"
    transaction["transaction_id"] = transaction_id
    transaction["build_directory"] = new_name
    _write_json(staging / "start-transaction.json", transaction)
    target = staging.with_name(new_name)
    staging.rename(target)
    return target


def _assert_rejected_without_new_side_effects(
    paths: ProjectPaths,
    staging: Path,
) -> None:
    business_before = _business_snapshot(paths)
    candidate_before = _tree_snapshot(staging)
    with pytest.raises(SdlcError):
        validate_prepared_start_staging(paths, staging)
    assert _business_snapshot(paths) == business_before
    assert _tree_snapshot(staging) == candidate_before


def test_real_formal_bundle_builds_complete_prepared_staging(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    baseline = _business_snapshot(paths)

    result = build_prepared_start_staging(paths, preflight)
    staging = Path(result["staging_directory"])
    transaction = validate_prepared_start_staging(paths, staging)

    assert transaction["state"] == "prepared"
    assert transaction["prepared"] is True
    assert transaction["source_draft_id"] == "DRAFT-001"
    assert transaction["requirement_id"] == "REQ-001"
    assert transaction["event_file_size"] == len(paths.events_file.read_bytes())
    assert (staging / "original/formal.v3.json").is_file()
    assert (staging / "original/artifact-index.v1.json").is_file()
    assert (staging / "reference-index.v1.json").is_file()
    assert (staging / "effective/requirement.current.json").is_file()
    assert (staging / "effective/design.current.json").is_file()
    assert (staging / "effective/test-matrix.current.json").is_file()
    assert (staging / "versions/requirement.v1.json").is_file()
    assert (staging / "versions/design.v1.json").is_file()
    assert (staging / "versions/test-matrix.v1.json").is_file()
    assert (staging / "tasks").is_dir()
    assert (staging / "status.json").is_file()
    assert _business_snapshot(paths) == baseline

    source_root = paths.draft_dir("DRAFT-001")
    for item in preflight["package"]["artifact_manifest"]:  # type: ignore[index,union-attr]
        source = source_root / item["source_path"]
        archived = staging / item["archive_path"]
        assert archived.read_bytes() == source.read_bytes()
        assert sha256_bytes(archived.read_bytes()) == item["sha256"]


def test_production_t017_preflight_builds_prepared_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, paths, package = _ready_project(tmp_path, monkeypatch)
    baseline = _business_snapshot(paths)

    result = start_service.prepare_document_first_start(paths, package)
    transaction = validate_prepared_start_staging(
        paths,
        Path(result["staging_directory"]),
    )

    assert transaction["state"] == "prepared"
    assert transaction["formal_manifest"] == package["artifact_manifest"]
    assert _business_snapshot(paths) == baseline


def test_reference_index_uses_only_formal_archive_paths(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    reference = json.loads(
        (staging / "reference-index.v1.json").read_text(encoding="utf-8")
    )

    for entry in reference["entries"].values():
        assert entry["path"].startswith("original/")
        locator = entry.get("locator", {})
        if locator.get("kind") == "design_node":
            assert locator["node_index_path"].startswith("original/")


@pytest.mark.parametrize(
    "mutation",
    [
        "source_missing",
        "source_directory",
        "source_symlink",
        "source_escape",
        "source_hash",
        "manifest_missing",
        "manifest_extra",
        "manifest_archive_drift",
        "reference_drift",
    ],
)
def test_invalid_source_manifest_or_reference_is_rejected_and_cleaned(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    package = preflight["package"]  # type: ignore[assignment]
    manifest = package["artifact_manifest"]  # type: ignore[index]
    first = manifest[0]
    source = paths.draft_dir("DRAFT-001") / first["source_path"]
    baseline = _business_snapshot(paths)

    if mutation == "source_missing":
        source.unlink()
    elif mutation == "source_directory":
        source.unlink()
        source.mkdir()
    elif mutation == "source_symlink":
        original = source.read_bytes()
        source.unlink()
        outside = tmp_path / "outside.bin"
        outside.write_bytes(original)
        source.symlink_to(outside)
    elif mutation == "source_escape":
        first["source_path"] = "../outside.bin"
    elif mutation == "source_hash":
        source.write_bytes(source.read_bytes() + b"drift")
    elif mutation == "manifest_missing":
        manifest.pop()
    elif mutation == "manifest_extra":
        manifest.append(deepcopy(first))
        manifest[-1]["artifact_id"] = "ART-999"
    elif mutation == "manifest_archive_drift":
        first["archive_path"] = "original/错误位置.bin"
    elif mutation == "reference_drift":
        reference = preflight["reference_index"]  # type: ignore[assignment]
        next(iter(reference["entries"].values()))["sha256"] = "0" * 64

    changed_baseline = _business_snapshot(paths)
    with pytest.raises(SdlcError):
        build_prepared_start_staging(paths, preflight)
    assert _business_snapshot(paths) == changed_baseline
    assert not list(paths.start_staging_root.glob("start-*"))
    assert baseline["events"] == changed_baseline["events"]
    assert baseline["database"] == changed_baseline["database"]
    assert baseline["requirements"] == changed_baseline["requirements"]


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_transaction_preparing",
        "after_first_original",
        "before_reference_index",
        "before_effective",
        "before_versions",
        "before_integrity_check",
        "before_prepared_replace",
        "after_prepared_replace",
    ],
)
def test_faults_remove_only_current_staging_without_business_side_effects(
    tmp_path: Path,
    fault_point: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    baseline = _business_snapshot(paths)
    preserved = paths.start_staging_root / "start-preserved"
    preserved.mkdir(parents=True)
    (preserved / "marker").write_text("不能删除", encoding="utf-8")

    def fail(point: str, _staging: Path) -> None:
        if point == fault_point:
            raise RuntimeError(fault_point)

    with pytest.raises(RuntimeError, match=fault_point):
        build_prepared_start_staging(paths, preflight, fault_injector=fail)

    assert _business_snapshot(paths) == baseline
    assert (preserved / "marker").read_text(encoding="utf-8") == "不能删除"
    assert sorted(paths.start_staging_root.iterdir()) == [preserved]


def _interrupt_worker(
    root: str,
    preflight: dict[str, object],
    fault_point: str,
) -> None:
    paths = ProjectPaths(Path(root))

    def interrupt(point: str, _staging: Path) -> None:
        if point == fault_point:
            os._exit(91)

    build_prepared_start_staging(paths, preflight, fault_injector=interrupt)


@pytest.mark.parametrize(
    "fault_point",
    ["after_first_original", "before_prepared_replace"],
)
def test_real_process_interrupt_is_cleaned_from_structured_transaction(
    tmp_path: Path,
    fault_point: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    baseline = _business_snapshot(paths)
    process = multiprocessing.get_context("spawn").Process(
        target=_interrupt_worker,
        args=(str(paths.root), preflight, fault_point),
    )
    process.start()
    process.join(30)

    assert process.exitcode == 91
    leftovers = list(paths.start_staging_root.glob("start-*"))
    assert len(leftovers) == 1
    transaction = json.loads(
        (leftovers[0] / "start-transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "preparing"
    cleanup = cleanup_incomplete_start_staging(paths)
    assert cleanup["removed"] == [str(leftovers[0])]
    assert not list(paths.start_staging_root.glob("start-*"))
    assert _business_snapshot(paths) == baseline


def test_prepared_is_not_removed_but_damaged_candidates_are(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    prepared = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    missing = paths.start_staging_root / "start-missing"
    missing.mkdir()
    damaged = paths.start_staging_root / "start-damaged"
    damaged.mkdir()
    (damaged / "start-transaction.json").write_text("{", encoding="utf-8")

    result = cleanup_incomplete_start_staging(paths)

    assert prepared.is_dir()
    assert result["kept"] == [str(prepared)]
    assert set(result["removed"]) == {str(missing), str(damaged)}


def test_repeated_and_concurrent_builds_do_not_share_directory(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    first = build_prepared_start_staging(paths, preflight)
    second = build_prepared_start_staging(paths, preflight)
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = list(
            executor.map(
                lambda _index: build_prepared_start_staging(paths, preflight),
                range(2),
            )
        )

    assert first["transaction_id"] == second["transaction_id"]
    assert first["staging_directory"] != second["staging_directory"]
    all_results = [first, second, *concurrent]
    assert len({item["staging_directory"] for item in all_results}) == 4
    assert len({item["transaction_id"] for item in all_results}) == 1
    for item in all_results:
        validate_prepared_start_staging(paths, Path(item["staging_directory"]))


def test_unique_name_collision_never_overwrites_existing_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, preflight = _workspace(tmp_path)
    transaction_id = start_staging._transaction_id(preflight)
    collision = (
        paths.start_staging_root
        / f"start-{transaction_id[6:22]}-{'a' * 16}"
    )
    paths.start_staging_root.mkdir()
    collision.mkdir()
    (collision / "marker").write_text("原目录", encoding="utf-8")
    tokens = iter(["a" * 16, "b" * 16])
    monkeypatch.setattr(start_staging.secrets, "token_hex", lambda _size: next(tokens))

    result = build_prepared_start_staging(paths, preflight)

    assert Path(result["staging_directory"]).name.endswith("-" + "b" * 16)
    assert (collision / "marker").read_text(encoding="utf-8") == "原目录"


@pytest.mark.parametrize("unsafe_part", ["staging_root", "requirements", "draft_parent"])
def test_controlled_roots_reject_symlinks(tmp_path: Path, unsafe_part: str) -> None:
    paths, preflight = _workspace(tmp_path)
    real = tmp_path / f"real-{unsafe_part}"
    real.mkdir()
    if unsafe_part == "staging_root":
        paths.start_staging_root.parent.mkdir(parents=True, exist_ok=True)
        paths.start_staging_root.symlink_to(real, target_is_directory=True)
    elif unsafe_part == "requirements":
        paths.requirements_dir.rmdir()
        paths.requirements_dir.symlink_to(real, target_is_directory=True)
    else:
        draft = paths.draft_dir("DRAFT-001")
        moved = tmp_path / "moved-draft"
        draft.rename(moved)
        draft.symlink_to(moved, target_is_directory=True)

    with pytest.raises(SdlcError):
        build_prepared_start_staging(paths, preflight)


def test_existing_target_and_unsafe_target_name_are_rejected(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    (paths.requirements_dir / "REQ-001-existing").mkdir()
    with pytest.raises(SdlcError, match="目标"):
        build_prepared_start_staging(paths, preflight)

    shutil.rmtree(paths.requirements_dir / "REQ-001-existing")
    preflight["target_directory"] = "../REQ-001"
    with pytest.raises(SdlcError, match="目标"):
        build_prepared_start_staging(paths, preflight)


def test_source_change_during_copy_is_rejected(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    changed = False

    def mutate(point: str, _staging: Path) -> None:
        nonlocal changed
        if point == "after_source_read" and not changed:
            item = preflight["package"]["artifact_manifest"][0]  # type: ignore[index,union-attr]
            source = paths.draft_dir("DRAFT-001") / item["source_path"]
            source.write_bytes(source.read_bytes() + b"changed-during-copy")
            changed = True

    with pytest.raises(SdlcError, match="复制期间"):
        build_prepared_start_staging(paths, preflight, fault_injector=mutate)
    assert not list(paths.start_staging_root.glob("start-*"))


def test_transaction_revision_cannot_self_prove_after_identity_is_rebuilt(
    tmp_path: Path,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    validate_prepared_start_staging(paths, staging)
    transaction = _transaction(staging)
    transaction["source_revision_sha256"] = "0" * 64
    staging = _rewrite_transaction_identity(staging, transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


@pytest.mark.parametrize(
    "alignment",
    ["transaction_and_formal", "transaction_and_artifact"],
)
def test_revision_must_match_transaction_formal_and_artifact_index(
    tmp_path: Path,
    alignment: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    formal_path = staging / "original/formal.v3.json"
    formal = json.loads(formal_path.read_text(encoding="utf-8"))
    artifact_path = staging / "original/artifact-index.v1.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    drift = "0" * 64
    if alignment == "transaction_and_formal":
        transaction["source_revision_sha256"] = drift
        formal["source_revision_sha256"] = drift
        _rewrite_generated_json(
            staging,
            transaction,
            "original/formal.v3.json",
            formal,
        )
    else:
        formal["source_revision_sha256"] = drift
        _rewrite_generated_json(
            staging,
            transaction,
            "original/formal.v3.json",
            formal,
        )
    staging = _rewrite_transaction_identity(staging, transaction)

    _assert_rejected_without_new_side_effects(paths, staging)
    assert artifact["draft_revision_sha256"] != drift


@pytest.mark.parametrize("owner", ["transaction", "formal", "artifact"])
def test_source_draft_must_match_transaction_formal_and_artifact_index(
    tmp_path: Path,
    owner: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    if owner == "transaction":
        transaction["source_draft_id"] = "DRAFT-999"
    elif owner == "formal":
        formal = json.loads(
            (staging / "original/formal.v3.json").read_text(encoding="utf-8")
        )
        formal["source_draft_id"] = "DRAFT-999"
        _rewrite_generated_json(
            staging,
            transaction,
            "original/formal.v3.json",
            formal,
        )
    else:
        artifact = json.loads(
            (staging / "original/artifact-index.v1.json").read_text(encoding="utf-8")
        )
        artifact["draft_id"] = "DRAFT-999"
        _rewrite_generated_json(
            staging,
            transaction,
            "original/artifact-index.v1.json",
            artifact,
        )
    _write_json(staging / "start-transaction.json", transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


@pytest.mark.parametrize("owner", ["transaction", "formal", "artifact"])
@pytest.mark.parametrize("bad_value", [None, 123, "不是完整哈希"])
def test_revision_missing_type_and_format_errors_are_rejected(
    tmp_path: Path,
    owner: str,
    bad_value: object,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    if owner == "transaction":
        if bad_value is None:
            transaction.pop("source_revision_sha256")
        else:
            transaction["source_revision_sha256"] = bad_value
        _write_json(staging / "start-transaction.json", transaction)
    elif owner == "formal":
        formal = json.loads(
            (staging / "original/formal.v3.json").read_text(encoding="utf-8")
        )
        if bad_value is None:
            formal.pop("source_revision_sha256")
        else:
            formal["source_revision_sha256"] = bad_value
        _rewrite_generated_json(
            staging,
            transaction,
            "original/formal.v3.json",
            formal,
        )
        _write_json(staging / "start-transaction.json", transaction)
    else:
        artifact = json.loads(
            (staging / "original/artifact-index.v1.json").read_text(encoding="utf-8")
        )
        if bad_value is None:
            artifact.pop("draft_revision_sha256")
        else:
            artifact["draft_revision_sha256"] = bad_value
        _rewrite_generated_json(
            staging,
            transaction,
            "original/artifact-index.v1.json",
            artifact,
        )
        _write_json(staging / "start-transaction.json", transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


@pytest.mark.parametrize(
    "fields",
    [
        ("event_file_size",),
        ("event_count",),
        ("event_sha256",),
        ("event_file_size", "event_count", "event_sha256"),
    ],
)
def test_transaction_event_boundary_fields_bind_real_events(
    tmp_path: Path,
    fields: tuple[str, ...],
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    replacements: dict[str, object] = {
        "event_file_size": 999999,
        "event_count": 999,
        "event_sha256": "f" * 64,
    }
    for field in fields:
        transaction[field] = replacements[field]
    _write_json(staging / "start-transaction.json", transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


@pytest.mark.parametrize("mutation", ["append", "truncate", "same_size"])
def test_real_events_changes_invalidate_prepared_candidate(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    original = paths.events_file.read_bytes()
    if mutation == "append":
        paths.events_file.write_bytes(original + b'{"event_id":"EVT-002"}\n')
    elif mutation == "truncate":
        paths.events_file.write_bytes(original[:-1])
    else:
        replacement = bytearray(original)
        replacement[2] = ord("X") if replacement[2] != ord("X") else ord("Y")
        paths.events_file.write_bytes(bytes(replacement))
        assert len(paths.events_file.read_bytes()) == len(original)

    _assert_rejected_without_new_side_effects(paths, staging)


def test_missing_events_file_is_validated_as_empty_without_creation(
    tmp_path: Path,
) -> None:
    paths, preflight = _workspace(tmp_path)
    paths.events_file.unlink()
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])

    transaction = validate_prepared_start_staging(paths, staging)

    assert transaction["event_file_size"] == 0
    assert transaction["event_count"] == 0
    assert transaction["event_sha256"] == sha256_bytes(b"")
    assert not paths.events_file.exists()


@pytest.mark.parametrize(
    "target",
    [
        "REQ-999",
        "REQ-001/子目录",
        "/tmp/REQ-001",
        "../REQ-001",
        "",
        ".",
        "REQ-001/../REQ-001",
    ],
)
def test_target_directory_cannot_self_prove_with_rebuilt_identity(
    tmp_path: Path,
    target: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    transaction["target_directory"] = target
    staging = _rewrite_transaction_identity(staging, transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


def test_target_directory_symlink_is_rejected_after_prepare(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (paths.requirements_dir / "REQ-001").symlink_to(
        outside,
        target_is_directory=True,
    )

    _assert_rejected_without_new_side_effects(paths, staging)


def test_requirement_id_change_is_still_rejected(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    transaction["requirement_id"] = "REQ-999"
    transaction["target_directory"] = "REQ-999"
    staging = _rewrite_transaction_identity(staging, transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


def test_generated_files_missing_entry_is_still_rejected(tmp_path: Path) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    transaction = _transaction(staging)
    transaction["generated_files"].pop("status.json")  # type: ignore[union-attr]
    _write_json(staging / "start-transaction.json", transaction)

    _assert_rejected_without_new_side_effects(paths, staging)


@pytest.mark.parametrize("mutation", ["delete", "rewrite"])
def test_prepared_formal_file_delete_or_rewrite_is_still_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    paths, preflight = _workspace(tmp_path)
    staging = Path(build_prepared_start_staging(paths, preflight)["staging_directory"])
    formal_path = staging / "original/formal.v3.json"
    if mutation == "delete":
        formal_path.unlink()
    else:
        formal_path.write_bytes(formal_path.read_bytes() + b"\n")

    _assert_rejected_without_new_side_effects(paths, staging)
