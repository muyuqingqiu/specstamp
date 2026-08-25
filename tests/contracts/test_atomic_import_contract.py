from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

import pytest

from codex_sdlc.core.atomic_import import (
    AtomicImportPrecommitContext,
    IMPORT_PACKAGE_SCHEMA,
    atomic_import,
    collect_known_formal_ids,
    load_import_registry,
    recover_atomic_imports,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, ensure_base_dirs
from codex_sdlc.core.structured_contract import canonical_sha256, contract_sha256


REPO_ROOT = Path(__file__).resolve().parents[2]


def _init_project(tmp_path: Path, name: str = "project") -> tuple[Path, object]:
    root = tmp_path / name
    root.mkdir()
    (root / "README.md").write_text("# 临时项目\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    paths = build_paths(root)
    ensure_base_dirs(paths)
    paths.events_file.write_text("", encoding="utf-8")
    return root, paths


def _package(
    package_key: str,
    *,
    destination: str | None = None,
    prefix: str = "FR",
    content_suffix: str = "",
) -> dict[str, object]:
    destination = destination or f".codex-sdlc/imports/{package_key}"
    package: dict[str, object] = {
        "schema": IMPORT_PACKAGE_SCHEMA,
        "package_key": package_key,
        "package_sha256": "0" * 64,
        "destination": destination,
        "objects": [
            {"client_key": "storage", "id_prefix": prefix, "depends_on": []},
            {
                "client_key": "service",
                "id_prefix": prefix,
                "depends_on": ["@client:storage"],
            },
        ],
        "files": [
            {
                "relative_path": "plan.json",
                "content": {
                    "name": f"计划{content_suffix}",
                    "order": ["@client:storage", "@client:service"],
                },
            },
            {
                "relative_path": "objects/@client:storage/value.json",
                "content": {"id": "@client:storage", "depends_on": []},
            },
            {
                "relative_path": "objects/@client:service/value.json",
                "content": {
                    "id": "@client:service",
                    "depends_on": ["@client:storage"],
                },
            },
        ],
    }
    package["package_sha256"] = contract_sha256(package, schema_name=IMPORT_PACKAGE_SCHEMA)
    return package


def _read_events(paths: object) -> list[dict[str, object]]:
    event_path = paths.events_file
    return [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line]


def _state_snapshot(paths: object) -> dict[str, object]:
    return {
        "events": paths.events_file.read_bytes(),
        "registry": paths.import_registry_file.read_bytes()
        if paths.import_registry_file.exists()
        else None,
        "imports": sorted(
            str(path.relative_to(paths.root))
            for path in paths.imports_dir.rglob("*")
        ),
    }


def test_atomic_import_rewrites_all_refs_and_same_package_retry_is_idempotent(tmp_path: Path) -> None:
    _, paths = _init_project(tmp_path)
    package = _package("requirement-a")

    first = atomic_import(paths, package)
    event_bytes = paths.events_file.read_bytes()
    registry_bytes = paths.import_registry_file.read_bytes()
    file_snapshot = {
        path.relative_to(paths.root): path.read_bytes()
        for path in paths.imports_dir.rglob("*")
        if path.is_file()
    }
    second = atomic_import(paths, deepcopy(package))

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.mapping == first.mapping == {"storage": "FR-001", "service": "FR-002"}
    assert second.event_ids == first.event_ids
    assert paths.events_file.read_bytes() == event_bytes
    assert paths.import_registry_file.read_bytes() == registry_bytes
    assert {
        path.relative_to(paths.root): path.read_bytes()
        for path in paths.imports_dir.rglob("*")
        if path.is_file()
    } == file_snapshot
    plan = json.loads((paths.root / ".codex-sdlc/imports/requirement-a/plan.json").read_text())
    service = json.loads(
        (
            paths.root
            / ".codex-sdlc/imports/requirement-a/objects/FR-002/value.json"
        ).read_text()
    )
    assert plan["order"] == ["FR-001", "FR-002"]
    assert service == {"depends_on": ["FR-001"], "id": "FR-002"}
    assert len(_read_events(paths)) == 1
    assert len(load_import_registry(paths)["packages"]) == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda package: package["objects"][0].update({"id_prefix": "WRONG"}), "Schema 校验失败"),
        (
            lambda package: package["objects"][1].update(
                {"depends_on": ["@client:outside"]}
            ),
            "跨包或悬空",
        ),
        (
            lambda package: package["objects"][0].update(
                {"depends_on": ["@client:service"]}
            ),
            "依赖环",
        ),
        (lambda package: package.update({"package_sha256": "f" * 64}), "规范摘要冲突"),
        (lambda package: package.update({"unknown": True}), "Schema 校验失败"),
    ],
)
def test_invalid_package_is_rejected_without_event_file_or_number(
    tmp_path: Path, mutate, message: str
) -> None:
    _, paths = _init_project(tmp_path)
    package = _package("invalid-package")
    before = _state_snapshot(paths)
    mutate(package)
    if package.get("package_sha256") != "f" * 64:
        package["package_sha256"] = contract_sha256(package, schema_name=IMPORT_PACKAGE_SCHEMA)

    with pytest.raises(SdlcError, match=message):
        atomic_import(paths, package)

    assert _state_snapshot(paths) == before
    valid = _package("valid-after-reject")
    assert atomic_import(paths, valid).mapping == {"storage": "FR-001", "service": "FR-002"}


def test_same_package_key_with_changed_digest_is_rejected_without_new_state(tmp_path: Path) -> None:
    _, paths = _init_project(tmp_path)
    atomic_import(paths, _package("stable-key"))
    changed = _package("stable-key", content_suffix="变更")
    before = _state_snapshot(paths)

    with pytest.raises(SdlcError, match="已经登记了不同的规范摘要"):
        atomic_import(paths, changed)

    assert _state_snapshot(paths) == before


def test_existing_event_ids_are_reserved_without_rewriting_historical_event_bytes(
    tmp_path: Path,
) -> None:
    _, paths = _init_project(tmp_path)
    historical_event = {
        "event_id": "EVT-20200101-000001",
        "event_type": "fixture_created",
        "project_path": str(paths.root),
        "requirement_id": "REQ-007",
        "task_id": None,
        "created_at": "2020-01-01T00:00:00+08:00",
        "source": "fixture",
        "summary": "保留原始事件格式",
        "payload": {"title": "FR-999"},
    }
    original_bytes = json.dumps(historical_event, ensure_ascii=False).encode("utf-8")
    paths.events_file.write_bytes(original_bytes)

    result = atomic_import(paths, _package("after-existing-event", prefix="REQ"))

    assert result.mapping == {"storage": "REQ-008", "service": "REQ-009"}
    assert paths.events_file.read_bytes().startswith(original_bytes + b"\n")
    assert len(_read_events(paths)) == 2


def test_plain_event_text_does_not_create_formal_ids_or_validate_false_reference(
    tmp_path: Path,
) -> None:
    _, paths = _init_project(tmp_path)
    plain_text_event = {
        "event_id": "EVT-20200101-000001",
        "event_type": "plain_text_recorded",
        "project_path": str(paths.root),
        "requirement_id": None,
        "task_id": None,
        "created_at": "2020-01-01T00:00:00+08:00",
        "source": "fixture",
        "summary": "FR-999",
        "payload": {
            "title": "FR-999",
            "reason": "DEC-999",
            "description": "FR-999",
            "user_decision": "DEC-999",
            "markdown": "FR-999",
        },
    }
    paths.events_file.write_text(
        json.dumps(plain_text_event, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    known = collect_known_formal_ids(_read_events(paths), load_import_registry(paths))
    assert "FR-999" not in known
    assert "DEC-999" not in known

    package = _package("false-history-reference")
    package["files"][0]["content"]["historical_ref"] = "FR-999"
    package["package_sha256"] = contract_sha256(package, schema_name=IMPORT_PACKAGE_SCHEMA)

    def reject_false_reference(
        _paths: object, context: AtomicImportPrecommitContext
    ) -> None:
        if "FR-999" not in context.known_formal_ids:
            raise SdlcError("正式引用不存在：FR-999。")

    before = _state_snapshot(paths)
    with pytest.raises(SdlcError, match="正式引用不存在"):
        atomic_import(paths, package, locked_precommit_validator=reject_false_reference)
    assert _state_snapshot(paths) == before
    assert atomic_import(paths, _package("first-real-fr")).mapping == {
        "storage": "FR-001",
        "service": "FR-002",
    }
    assert atomic_import(paths, _package("first-real-dec", prefix="DEC")).mapping == {
        "storage": "DEC-001",
        "service": "DEC-002",
    }


def test_atomic_registry_mapping_remains_an_authoritative_number_source(
    tmp_path: Path,
) -> None:
    _, paths = _init_project(tmp_path)
    first = atomic_import(paths, _package("registered-history"))
    registry = load_import_registry(paths)

    assert collect_known_formal_ids([], registry) == frozenset(first.mapping.values())
    second = atomic_import(paths, _package("after-registered-history"))
    assert second.mapping == {"storage": "FR-003", "service": "FR-004"}


def test_different_package_cannot_overwrite_an_existing_destination(tmp_path: Path) -> None:
    _, paths = _init_project(tmp_path)
    atomic_import(paths, _package("destination-owner"))
    conflicting = _package(
        "destination-conflict",
        destination=".codex-sdlc/imports/destination-owner",
    )
    before = _state_snapshot(paths)

    with pytest.raises(SdlcError, match="已经存在但没有幂等登记"):
        atomic_import(paths, conflicting)

    assert _state_snapshot(paths) == before


def test_write_failure_cleans_partial_stage_and_reuses_uncommitted_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, paths = _init_project(tmp_path)
    from codex_sdlc.core import atomic_import as module

    original = module._write_file_bytes
    calls = 0

    def fail_second_file(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("注入写入失败")
        original(path, content)

    monkeypatch.setattr(module, "_write_file_bytes", fail_second_file)
    with pytest.raises(OSError, match="注入写入失败"):
        atomic_import(paths, _package("write-failure"))

    assert _read_events(paths) == []
    assert not any(paths.imports_dir.iterdir())
    assert not list(paths.import_transactions_dir.glob("*.json"))
    monkeypatch.setattr(module, "_write_file_bytes", original)
    assert atomic_import(paths, _package("after-write-failure")).mapping["storage"] == "FR-001"


def _state_drift_worker_script(path: Path) -> Path:
    script = path / "并发状态变化子进程.py"
    script.write_text(
        """
from __future__ import annotations
import json
from pathlib import Path
import sys
import time
from codex_sdlc.core.project import build_paths, project_lock

root = Path(sys.argv[1])
state_path = Path(sys.argv[2])
with project_lock(build_paths(root)):
    state_path.write_text(json.dumps({'version': 2}), encoding='utf-8')
    print('updated', flush=True)
    time.sleep(0.4)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def test_locked_precommit_validation_rejects_concurrent_state_drift_without_number(
    tmp_path: Path,
) -> None:
    root, paths = _init_project(tmp_path)
    state_path = paths.sdlc_dir / "generic-business-state.json"
    state_path.write_text('{"version":1}', encoding="utf-8")
    script = _state_drift_worker_script(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    worker = subprocess.Popen(
        [sys.executable, str(script), str(root), str(state_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert worker.stdout is not None
    assert worker.stdout.readline().strip() == "updated"
    before = _state_snapshot(paths)

    def validate_latest_state(
        _paths: object, context: AtomicImportPrecommitContext
    ) -> None:
        assert context.package_key == "stale-package"
        latest = json.loads(state_path.read_text(encoding="utf-8"))
        if latest["version"] != 1:
            raise SdlcError("锁内最终校验发现业务状态已经变化。")

    with pytest.raises(SdlcError, match="业务状态已经变化"):
        atomic_import(
            paths,
            _package("stale-package"),
            locked_precommit_validator=validate_latest_state,
        )
    stdout, stderr = worker.communicate(timeout=30)
    assert worker.returncode == 0, (stdout, stderr)
    assert _state_snapshot(paths) == before
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))
    assert atomic_import(paths, _package("after-stale-reject")).mapping == {
        "storage": "FR-001",
        "service": "FR-002",
    }


def _derived_hash_package(package_key: str) -> dict[str, object]:
    package = _package(package_key)
    package["files"] = [
        {
            "relative_path": "split.json",
            "content": {"requirement_id": "@client:storage", "title": "正式拆分"},
        },
        {
            "relative_path": "coverage.json",
            "content": {
                "requirement_id": "@client:service",
                "split_sha256": "0" * 64,
            },
        },
    ]
    package["package_sha256"] = contract_sha256(package, schema_name=IMPORT_PACKAGE_SCHEMA)
    return package


def _finalize_derived_hash(
    mapping: Mapping[str, str], files: Mapping[str, object]
) -> Mapping[str, object]:
    assert mapping == {"storage": "FR-001", "service": "FR-002"}
    finalized = deepcopy(dict(files))
    finalized["coverage.json"]["split_sha256"] = canonical_sha256(
        finalized["split.json"]
    )
    return finalized


def test_mapping_aware_file_finalizer_updates_hash_and_all_commit_evidence(
    tmp_path: Path,
) -> None:
    _, paths = _init_project(tmp_path)
    package = _derived_hash_package("finalized-hash")
    source_package_sha256 = str(package["package_sha256"])

    first = atomic_import(paths, package, files_finalizer=_finalize_derived_hash)
    destination = paths.root / first.destination
    split = json.loads((destination / "split.json").read_text(encoding="utf-8"))
    coverage = json.loads((destination / "coverage.json").read_text(encoding="utf-8"))
    receipt = json.loads(
        (destination / ".codex-import-receipt.json").read_text(encoding="utf-8")
    )
    registration = load_import_registry(paths)["packages"][0]
    event = _read_events(paths)[0]

    assert split["requirement_id"] == "FR-001"
    assert coverage["requirement_id"] == "FR-002"
    assert coverage["split_sha256"] == canonical_sha256(split)
    assert first.package_sha256 != source_package_sha256
    assert receipt["package_sha256"] == first.package_sha256
    assert registration["package_sha256"] == first.package_sha256
    assert event["payload"]["package_sha256"] == first.package_sha256
    assert event["payload"]["bundle_sha256"] == registration["bundle_sha256"]

    before = _state_snapshot(paths)
    retry = atomic_import(paths, deepcopy(package), files_finalizer=_finalize_derived_hash)
    assert retry.duplicate is True
    assert retry.mapping == first.mapping
    assert retry.package_sha256 == first.package_sha256
    assert _state_snapshot(paths) == before


def test_file_finalizer_failure_rolls_back_everything_and_does_not_take_number(
    tmp_path: Path,
) -> None:
    _, paths = _init_project(tmp_path)
    before = _state_snapshot(paths)

    def fail_finalization(
        _mapping: Mapping[str, str], _files: Mapping[str, object]
    ) -> Mapping[str, object]:
        raise SdlcError("注入映射后文件最终化失败。")

    with pytest.raises(SdlcError, match="最终化失败"):
        atomic_import(
            paths,
            _derived_hash_package("failed-finalization"),
            files_finalizer=fail_finalization,
        )
    assert _state_snapshot(paths) == before
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))
    assert atomic_import(paths, _package("after-finalization-failure")).mapping == {
        "storage": "FR-001",
        "service": "FR-002",
    }


def _worker_script(path: Path) -> Path:
    script = path / "原子导入子进程.py"
    script.write_text(
        """
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
from codex_sdlc.core import atomic_import as atomic_import_module
from codex_sdlc.core.project import build_paths

root = Path(sys.argv[1])
package = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
interrupt_at = sys.argv[3] if len(sys.argv) > 3 else ''
paths = build_paths(root)
real_replace = atomic_import_module.os.replace

def replace_with_interrupt(source: object, target: object) -> None:
    target_path = Path(target)
    if interrupt_at == 'before_events_replace' and target_path == paths.events_file:
        os._exit(73)
    if interrupt_at == 'before_registry_replace' and target_path == paths.import_registry_file:
        os._exit(73)
    real_replace(source, target)

def interrupt(stage: str) -> None:
    if stage == interrupt_at:
        os._exit(73)

atomic_import_module.os.replace = replace_with_interrupt
result = atomic_import_module.atomic_import(paths, package, interruption_hook=interrupt)
print(json.dumps(result.as_dict(), ensure_ascii=False))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def _start_worker(root: Path, package_path: Path, script: Path, stage: str = "") -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.Popen(
        [sys.executable, str(script), str(root), str(package_path), stage],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def test_two_real_processes_import_different_packages_without_duplicate_ids(tmp_path: Path) -> None:
    root, paths = _init_project(tmp_path)
    script = _worker_script(tmp_path)
    package_paths = []
    for name in ("concurrent-a", "concurrent-b"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(_package(name), ensure_ascii=False), encoding="utf-8")
        package_paths.append(path)

    workers = [_start_worker(root, path, script) for path in package_paths]
    completed = [worker.communicate(timeout=30) for worker in workers]

    assert [worker.returncode for worker in workers] == [0, 0], completed
    results = [json.loads(stdout) for stdout, _ in completed]
    all_ids = [formal_id for result in results for formal_id in result["mapping"].values()]
    assert len(all_ids) == len(set(all_ids)) == 4
    assert set(all_ids) == {"FR-001", "FR-002", "FR-003", "FR-004"}
    assert len(_read_events(paths)) == 2
    assert len(load_import_registry(paths)["packages"]) == 2
    assert all((paths.root / result["destination"]).is_dir() for result in results)


@pytest.mark.parametrize(
    ("stage", "committed"),
    [
        ("after_staging", False),
        ("after_rename", True),
        ("after_event_registration", True),
        ("after_registration", True),
    ],
)
def test_real_process_interruption_recovers_to_one_clear_boundary(
    tmp_path: Path, stage: str, committed: bool
) -> None:
    root, paths = _init_project(tmp_path, stage)
    script = _worker_script(tmp_path)
    package = _package(f"interrupt-{stage}")
    package_path = tmp_path / f"{stage}.json"
    package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")

    worker = _start_worker(root, package_path, script, stage)
    stdout, stderr = worker.communicate(timeout=30)
    assert worker.returncode == 73, (stdout, stderr)

    recovered = recover_atomic_imports(paths)
    destination = paths.root / str(package["destination"])
    registry = load_import_registry(paths)
    events = _read_events(paths)
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))
    if committed:
        assert recovered == [str(package["package_key"])]
        assert destination.is_dir()
        assert len(events) == 1
        assert len(registry["packages"]) == 1
        before = _state_snapshot(paths)
        retry = atomic_import(paths, deepcopy(package))
        assert retry.duplicate is True
        assert _state_snapshot(paths) == before
    else:
        assert recovered == []
        assert not destination.exists()
        assert events == []
        assert registry["packages"] == []
        assert atomic_import(paths, deepcopy(package)).mapping["storage"] == "FR-001"


@pytest.mark.parametrize(
    ("stage", "target_name"),
    [
        ("before_events_replace", "events.jsonl"),
        ("before_registry_replace", "import-registry.json"),
    ],
)
def test_real_process_interruption_before_atomic_replace_cleans_owned_temp_file(
    tmp_path: Path, stage: str, target_name: str
) -> None:
    root, paths = _init_project(tmp_path, stage)
    script = _worker_script(tmp_path)
    package = _package(f"interrupt-{stage}")
    package_path = tmp_path / f"{stage}.json"
    package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")

    worker = _start_worker(root, package_path, script, stage)
    stdout, stderr = worker.communicate(timeout=30)
    assert worker.returncode == 73, (stdout, stderr)
    target_pattern = f".{target_name}.*.tmp"
    assert len(list(paths.sdlc_dir.glob(target_pattern))) == 1

    recovered = recover_atomic_imports(paths)
    destination = paths.root / str(package["destination"])
    assert recovered == [str(package["package_key"])]
    assert list(paths.sdlc_dir.glob(target_pattern)) == []
    assert destination.is_dir()
    assert len(_read_events(paths)) == 1
    assert len(load_import_registry(paths)["packages"]) == 1
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))

    before = _state_snapshot(paths)
    retry = atomic_import(paths, deepcopy(package))
    assert retry.duplicate is True
    assert retry.mapping == {"storage": "FR-001", "service": "FR-002"}
    assert _state_snapshot(paths) == before


def test_recovery_does_not_delete_unrelated_temporary_files(tmp_path: Path) -> None:
    _, paths = _init_project(tmp_path)
    unrelated_files = [
        paths.sdlc_dir / ".unrelated.tmp",
        paths.sdlc_dir / ".events.jsonl.not-owned.tmp",
        paths.sdlc_dir / ".import-registry.json.not-owned.tmp",
    ]
    for path in unrelated_files:
        path.write_text("保留\n", encoding="utf-8")

    assert recover_atomic_imports(paths) == []
    assert all(path.read_text(encoding="utf-8") == "保留\n" for path in unrelated_files)


def test_retry_detects_committed_bundle_drift_instead_of_hiding_it(tmp_path: Path) -> None:
    _, paths = _init_project(tmp_path)
    package = _package("drifted-package")
    atomic_import(paths, package)
    plan_path = paths.root / ".codex-sdlc/imports/drifted-package/plan.json"
    plan_path.write_text('{"tampered":true}\n', encoding="utf-8")
    before_events = paths.events_file.read_bytes()

    with pytest.raises(SdlcError, match="完整哈希不一致"):
        atomic_import(paths, package)

    assert paths.events_file.read_bytes() == before_events
