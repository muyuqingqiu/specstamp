from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
for import_path in (SRC_ROOT, TESTS_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from codex_sdlc.core.backup import write_sdlc_identity
from codex_sdlc.core.change_workspace import (
    CHANGE_MATERIAL_INTERRUPT_ENV,
    INTERRUPT_AFTER_MANIFEST_PUBLISH,
    INTERRUPT_AFTER_MATERIAL_EVENT_APPEND,
    INTERRUPT_AFTER_MATERIAL_PUBLISH,
    change_material_identity_document,
    manifest_prefix_document,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.external_version import normalized_url_sha256
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, load_events
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    validate_schema_document,
)
from codex_sdlc.services.change_service import (
    add_change_material,
    create_change_workspace,
)
from test_change_workspace_contract import (
    _base_hashes,
    _minimal_formal_project,
    _protected_requirement_snapshot,
)
from test_cli_v1 import run_cli_raw
from test_contract_cli_regressions import _ready_project
from test_task_plan_v2_contract import _task, _write_submission


def _workspace_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    result = create_change_workspace(
        build_paths(project),
        requirement_id="REQ-001",
        request_key="t031-contract-workspace",
    )
    return project, requirement_dir, project / result.workspace_path


def _material_events(project: Path, change_id: str = "CHG-001") -> list[dict[str, object]]:
    return [
        event
        for event in load_events(build_paths(project))
        if event.get("event_type") == "change_material_added"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("change_id") == change_id
    ]


def _manifest(workspace: Path) -> dict[str, object]:
    content = (workspace / "change-material-manifest.v1.json").read_bytes()
    document = json.loads(content)
    assert content == canonical_json_bytes(document)
    validate_schema_document(document, schema_name="change-material-manifest.v1")
    return document


def _write_evidence(path: Path, url: str, revision: str, *, indent: int | None = 2) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "external-version-evidence.v1",
                "normalized_url_sha256": normalized_url_sha256(url),
                "status": "confirmed",
                "evidence": {
                    "kind": "immutable_revision",
                    "provider": "document",
                    "revision": revision,
                },
            },
            ensure_ascii=False,
            indent=indent,
            separators=None if indent else (",", ":"),
        )
        + ("\n" if indent else ""),
        encoding="utf-8",
    )


def _write_secret(path: Path, *, compact: bool) -> dict[str, str]:
    document = {
        "schema_version": "secret-reference.v1",
        "kind": "environment-variable",
        "identifier": "T031_TEST_TOKEN",
        "access": "runtime-only",
    }
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
        + ("" if compact else "\n"),
        encoding="utf-8",
    )
    return document


def test_change_material_help_is_registered(tmp_path: Path) -> None:
    result = run_cli_raw(["change-material", "--help"], cwd=tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "requirement_id" in result.stdout
    assert "change_id" in result.stdout
    assert "--type" in result.stdout
    assert "--file" in result.stdout
    assert "--url" in result.stdout
    assert "--version-evidence" in result.stdout
    assert "--secret-reference" in result.stdout


def test_schema_and_three_identity_documents_are_exact() -> None:
    file_material = {
        "source_kind": "file",
        "type": "requirement",
        "source_path": "资料/说明.md",
        "sha256": "a" * 64,
    }
    external_material = {
        "source_kind": "external-reference",
        "type": "ui-design",
        "normalized_url_sha256": "b" * 64,
        "version_evidence_sha256": "c" * 64,
    }
    secret_material = {
        "source_kind": "secret-reference",
        "type": "environment",
        "secret_reference_sha256": "d" * 64,
    }

    assert change_material_identity_document(file_material) == {
        "source_kind": "file",
        "type": "requirement",
        "source": {"source_path": "资料/说明.md", "sha256": "a" * 64},
    }
    assert change_material_identity_document(external_material) == {
        "source_kind": "external-reference",
        "type": "ui-design",
        "source": {
            "normalized_url_sha256": "b" * 64,
            "version_evidence_sha256": "c" * 64,
        },
    }
    assert change_material_identity_document(secret_material) == {
        "source_kind": "secret-reference",
        "type": "environment",
        "source": {"secret_reference_sha256": "d" * 64},
    }

    valid = {
        "schema_version": "change-material-manifest.v1",
        "requirement_id": "REQ-001",
        "change_id": "CHG-001",
        "workspace_path": ".codex-sdlc/requirements/REQ-001/changes/CHG-001",
        "materials": [],
    }
    validate_schema_document(valid, schema_name="change-material-manifest.v1")
    with pytest.raises(SdlcError, match="未知字段"):
        validate_schema_document(
            {**valid, "created_at": "2026-07-22"},
            schema_name="change-material-manifest.v1",
        )


def test_files_keep_original_bytes_path_identity_idempotency_and_event_prefixes(
    tmp_path: Path,
) -> None:
    project, requirement_dir, workspace = _workspace_project(tmp_path)
    paths = build_paths(project)
    status_before = (workspace / "status.json").read_bytes()
    protected_before = _protected_requirement_snapshot(requirement_dir)
    text = project / "资料一.md"
    same_bytes_other_path = project / "资料二.md"
    binary = project / "原始图.bin"
    text.write_bytes("保留来源原值\n第二行".encode("utf-8"))
    same_bytes_other_path.write_bytes(text.read_bytes())
    binary.write_bytes(bytes(range(256)) + b"\x00\xff")

    first = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="requirement",
        file_path="资料一.md",
    )
    duplicate = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="requirement",
        file_path="资料一.md",
    )
    different_path = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="requirement",
        file_path="资料二.md",
    )
    binary_result = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="sample-data",
        file_path="原始图.bin",
    )

    assert first.material_id == duplicate.material_id == "CMAT-001"
    assert duplicate.duplicate is True
    assert different_path.material_id == "CMAT-002"
    assert binary_result.material_id == "CMAT-003"
    manifest = _manifest(workspace)
    materials = manifest["materials"]
    assert isinstance(materials, list)
    assert [item["material_id"] for item in materials] == [
        "CMAT-001",
        "CMAT-002",
        "CMAT-003",
    ]
    assert materials[0]["identity_sha256"] != materials[1]["identity_sha256"]
    for item, source in zip(materials, (text, same_bytes_other_path, binary), strict=True):
        stored = workspace / item["stored_path"]
        assert stored.read_bytes() == source.read_bytes()
        assert item["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert item["size_bytes"] == len(source.read_bytes())

    events = _material_events(project)
    assert len(events) == 3
    for index, item in enumerate(materials, start=1):
        event = next(event for event in events if event["event_id"] == item["event_id"])
        assert event["payload"]["manifest_sha256"] == sha256_bytes(
            canonical_json_bytes(manifest_prefix_document(manifest, index))
        )
    assert events[-1]["payload"]["manifest_sha256"] == sha256_bytes(
        canonical_json_bytes(manifest)
    )
    assert (workspace / "status.json").read_bytes() == status_before
    assert _protected_requirement_snapshot(requirement_dir) == protected_before


def test_external_versions_and_secret_reference_use_fixed_identity_rules(
    tmp_path: Path,
) -> None:
    project, _, workspace = _workspace_project(tmp_path)
    paths = build_paths(project)
    url = "HTTPS://Example.COM:443/design?id=7&view=main#section"
    evidence_one = project / "版本一.json"
    evidence_two = project / "版本二.json"
    _write_evidence(evidence_one, url, "rev-001")
    _write_evidence(evidence_two, url, "rev-002")
    secret_pretty = project / "秘密引用一.json"
    secret_compact = project / "秘密引用二.json"
    reference = _write_secret(secret_pretty, compact=False)
    _write_secret(secret_compact, compact=True)

    first_url = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="ui-design",
        url=url,
        version_evidence_path="版本一.json",
    )
    duplicate_url = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="ui-design",
        url=url,
        version_evidence_path="版本一.json",
    )
    changed_version = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="ui-design",
        url=url,
        version_evidence_path="版本二.json",
    )
    blocked = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="api-document",
        url="https://example.com/api",
    )
    first_secret = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="environment",
        secret_reference_path="秘密引用一.json",
    )
    duplicate_secret = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="environment",
        secret_reference_path="秘密引用二.json",
    )

    assert first_url.material_id == duplicate_url.material_id == "CMAT-001"
    assert changed_version.material_id == "CMAT-002"
    assert blocked.material_id == "CMAT-003"
    assert blocked.status == "blocked"
    assert first_secret.material_id == duplicate_secret.material_id == "CMAT-004"
    manifest = _manifest(workspace)
    materials = manifest["materials"]
    assert isinstance(materials, list)
    assert materials[0]["url"] == url
    assert materials[0]["version_evidence_sha256"] == hashlib.sha256(
        evidence_one.read_bytes()
    ).hexdigest()
    assert materials[1]["version_evidence_sha256"] == hashlib.sha256(
        evidence_two.read_bytes()
    ).hexdigest()
    assert materials[0]["identity_sha256"] != materials[1]["identity_sha256"]
    assert materials[2]["status"] == "blocked"
    assert materials[2]["version_evidence"]["status"] == "unversioned"
    assert materials[3]["secret_reference"] == reference
    assert materials[3]["secret_reference_sha256"] == canonical_sha256(reference)
    assert len(_material_events(project)) == 4


def test_ownership_bad_paths_bad_structures_and_old_evidence_drift_are_rejected(
    tmp_path: Path,
) -> None:
    project, _, workspace = _workspace_project(tmp_path)
    paths = build_paths(project)
    status_before = (workspace / "status.json").read_bytes()
    source = project / "资料.md"
    source.write_text("原始内容", encoding="utf-8")
    outside = tmp_path / "项目外.md"
    outside.write_text("不能读取", encoding="utf-8")
    directory = project / "目录输入"
    directory.mkdir()
    symlink = project / "资料链接.md"
    symlink.symlink_to(source)
    invalid_secret = project / "损坏秘密引用.json"
    invalid_secret.write_text('{"schema_version":"secret-reference.v1"}', encoding="utf-8")
    wrong_evidence = project / "错绑版本.json"
    _write_evidence(wrong_evidence, "https://example.com/other", "rev-wrong")
    other_requirement = paths.requirements_dir / "REQ-002-其他需求"
    other_requirement.mkdir()
    append_event(
        paths,
        event_type="requirement_created",
        source="t031-contract-fixture",
        summary="建立跨需求所有权测试夹具",
        requirement_id="REQ-002",
        payload={
            "title": "其他需求",
            "description": "只用于核对 CHG 所有权",
            "folder_name": other_requirement.name,
        },
    )

    rejected_calls = [
        lambda: add_change_material(
            paths,
            requirement_id="REQ-002",
            change_id="CHG-001",
            material_type="requirement",
            file_path="资料.md",
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-999",
            material_type="requirement",
            file_path="资料.md",
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path=str(outside),
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="目录输入",
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="资料链接.md",
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="ui-design",
            url="https://example.com/design",
            version_evidence_path="错绑版本.json",
        ),
        lambda: add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="environment",
            secret_reference_path="损坏秘密引用.json",
        ),
    ]
    for call in rejected_calls:
        with pytest.raises(SdlcError):
            call()
        assert not (workspace / "change-material-manifest.v1.json").exists()
        assert _material_events(project) == []
        assert (workspace / "status.json").read_bytes() == status_before

    created = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="requirement",
        file_path="资料.md",
    )
    manifest_before = (workspace / "change-material-manifest.v1.json").read_bytes()
    events_before = paths.events_file.read_bytes()
    stored = workspace / "原始资料" / created.material_id
    stored.write_text("被改动", encoding="utf-8")
    with pytest.raises(SdlcError, match="归档文件哈希"):
        add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="资料.md",
        )
    assert (workspace / "change-material-manifest.v1.json").read_bytes() == manifest_before
    assert paths.events_file.read_bytes() == events_before
    stored.write_bytes(source.read_bytes())

    event_documents = [
        json.loads(line) for line in paths.events_file.read_text(encoding="utf-8").splitlines()
    ]
    target_event = next(
        event for event in event_documents if event.get("event_type") == "change_material_added"
    )
    target_event["payload"]["manifest_sha256"] = "0" * 64
    paths.events_file.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in event_documents
        ),
        encoding="utf-8",
    )
    with pytest.raises(SdlcError, match="事件与清单前缀"):
        add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="资料.md",
        )
    assert (workspace / "change-material-manifest.v1.json").read_bytes() == manifest_before


@pytest.mark.parametrize(
    "stage",
    [
        INTERRUPT_AFTER_MATERIAL_PUBLISH,
        INTERRUPT_AFTER_MANIFEST_PUBLISH,
        INTERRUPT_AFTER_MATERIAL_EVENT_APPEND,
    ],
)
def test_three_interruptions_recover_without_rewriting_committed_state(
    tmp_path: Path,
    stage: str,
) -> None:
    project, requirement_dir, workspace = _workspace_project(tmp_path)
    paths = build_paths(project)
    source = project / "中断资料.bin"
    source.write_bytes(b"\x00T031\xff\x10")
    status_before = (workspace / "status.json").read_bytes()
    protected_before = _protected_requirement_snapshot(requirement_dir)

    def interrupt(current_stage: str) -> None:
        if current_stage == stage:
            raise SdlcError(f"测试中断：{stage}")

    with pytest.raises(SdlcError, match="测试中断"):
        add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="sample-data",
            file_path="中断资料.bin",
            interruption_hook=interrupt,
        )

    recovered = add_change_material(
        paths,
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="sample-data",
        file_path="中断资料.bin",
    )
    assert recovered.material_id == "CMAT-001"
    manifest = _manifest(workspace)
    assert len(manifest["materials"]) == 1
    assert (workspace / "原始资料/CMAT-001").read_bytes() == source.read_bytes()
    assert len(_material_events(project)) == 1
    assert not (workspace / ".material-transactions").exists()
    assert not (workspace / ".material-staging").exists()
    assert (workspace / "status.json").read_bytes() == status_before
    assert _protected_requirement_snapshot(requirement_dir) == protected_before


def test_corrupt_transaction_cannot_redirect_recovery_cleanup(tmp_path: Path) -> None:
    project, _, workspace = _workspace_project(tmp_path)
    paths = build_paths(project)
    source = project / "事务路径资料.txt"
    source.write_text("事务路径必须固定", encoding="utf-8")
    status_before = (workspace / "status.json").read_bytes()

    def interrupt(stage: str) -> None:
        if stage == INTERRUPT_AFTER_MATERIAL_PUBLISH:
            raise SdlcError("保留事务用于损坏测试")

    with pytest.raises(SdlcError, match="保留事务"):
        add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="事务路径资料.txt",
            interruption_hook=interrupt,
        )
    journal = next((workspace / ".material-transactions").glob("*.json"))
    transaction = json.loads(journal.read_text(encoding="utf-8"))
    transaction["staging_manifest_path"] = "status.json"
    journal.write_text(
        json.dumps(transaction, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(SdlcError, match="清单暂存路径不正确"):
        add_change_material(
            paths,
            requirement_id="REQ-001",
            change_id="CHG-001",
            material_type="requirement",
            file_path="事务路径资料.txt",
        )
    assert (workspace / "status.json").read_bytes() == status_before
    assert (workspace / "原始资料/CMAT-001").read_bytes() == source.read_bytes()
    assert not (workspace / "change-material-manifest.v1.json").exists()
    assert _material_events(project) == []


def test_two_real_cli_processes_allocate_different_cmat_numbers(tmp_path: Path) -> None:
    project, _, workspace = _workspace_project(tmp_path)
    (project / "并发一.txt").write_text("一", encoding="utf-8")
    (project / "并发二.txt").write_text("二", encoding="utf-8")
    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    processes = [
        subprocess.Popen(
            [
                str(REPO_ROOT / "bin/codex-sdlc"),
                "change-material",
                "REQ-001",
                "CHG-001",
                "--type",
                "requirement",
                "--file",
                file_name,
            ],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for file_name in ("并发一.txt", "并发二.txt")
    ]
    outputs: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stdout + stderr
        outputs.append(stdout)

    material_ids = {
        line.split("：", 1)[1]
        for output in outputs
        for line in output.splitlines()
        if line.startswith("已归档变更资料：")
    }
    assert material_ids == {"CMAT-001", "CMAT-002"}
    assert [item["material_id"] for item in _manifest(workspace)["materials"]] == [
        "CMAT-001",
        "CMAT-002",
    ]
    assert len(_material_events(project)) == 2


def test_v2_change_material_real_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓库外 Git 项目只通过正式 CLI 建档、创建 CHG 并归档全部资料场景。"""

    ready_root = tmp_path / "文档优先准备"
    ready_root.mkdir()
    project, paths, formal_package = _ready_project(ready_root, monkeypatch)
    assert (project / ".git").is_dir()
    package_file = tmp_path / "T031正式包.json"
    package_file.write_text(
        json.dumps(formal_package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    started = run_cli_raw(["start", "--file", str(package_file)], cwd=project)
    assert started.returncode == 0, started.stdout + started.stderr
    requirement_dir = paths.requirements_dir / "REQ-001"

    task = _task("t031-v2-base", "准备变更资料正式入口验证")
    task["design_refs"] = ["API-001", "COMP-001", "DATA-001", "PAGE-001", "SAFE-001"]
    task_submission = _write_submission(tmp_path / "T031任务模型输出", tasks=[task])
    coverage = json.loads(task_submission[2].read_text(encoding="utf-8"))
    coverage["design_artifacts"] = {
        design_id: {"tasks": ["@client:t031-v2-base"]}
        for design_id in task["design_refs"]
    }
    task_submission[2].write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tasks_imported = run_cli_raw(
        [
            "tasks",
            "REQ-001",
            "--plan-file",
            str(task_submission[0]),
            "--tasks-dir",
            str(task_submission[1]),
            "--coverage-file",
            str(task_submission[2]),
        ],
        cwd=project,
    )
    assert tasks_imported.returncode == 0, tasks_imported.stdout + tasks_imported.stderr
    assert (requirement_dir / "tasks/task-plan.v2.json").is_file()

    write_sdlc_identity(paths)
    created = run_cli_raw(
        ["change-create", "REQ-001", "--request-key", "t031-v2-workspace-001"],
        cwd=project,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    assert "变更：CHG-001" in created.stdout
    workspace = requirement_dir / "changes/CHG-001"
    status_before = (workspace / "status.json").read_bytes()
    bases_before = _base_hashes(requirement_dir)
    protected_before = _protected_requirement_snapshot(requirement_dir)
    creation_event_before = deepcopy(
        next(
            event
            for event in load_events(paths)
            if event.get("event_type") == "change_workspace_created"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("change_id") == "CHG-001"
        )
    )

    text_file = project / "现场文本.md"
    text_file.write_bytes("正式入口保留原值\n".encode("utf-8"))
    binary_file = project / "现场二进制.bin"
    binary_file.write_bytes(bytes(range(128)) + b"\x00\xffT031")
    same_bytes_other_path = project / "现场文本副本.md"
    same_bytes_other_path.write_bytes(text_file.read_bytes())
    url = "HTTPS://Example.COM:443/design?id=31&view=main#node"
    evidence_one = project / "现场版本一.json"
    evidence_two = project / "现场版本二.json"
    _write_evidence(evidence_one, url, "v2-revision-001")
    _write_evidence(evidence_two, url, "v2-revision-002")
    secret_pretty = project / "现场秘密引用一.json"
    secret_compact = project / "现场秘密引用二.json"
    secret_document = _write_secret(secret_pretty, compact=False)
    _write_secret(secret_compact, compact=True)

    command_results: list[subprocess.CompletedProcess[str]] = []

    def run_material(*arguments: str, stage: str = "") -> subprocess.CompletedProcess[str]:
        result = run_cli_raw(
            ["change-material", "REQ-001", "CHG-001", *arguments],
            cwd=project,
            extra_env={CHANGE_MATERIAL_INTERRUPT_ENV: stage},
        )
        command_results.append(result)
        return result

    first = run_material("--type", "requirement", "--file", "现场文本.md")
    first_manifest_bytes = (workspace / "change-material-manifest.v1.json").read_bytes()
    first_event_count = len(_material_events(project))
    first_retry = run_material("--type", "requirement", "--file", "现场文本.md")
    assert (workspace / "change-material-manifest.v1.json").read_bytes() == first_manifest_bytes
    assert len(_material_events(project)) == first_event_count
    binary = run_material("--type", "sample-data", "--file", "现场二进制.bin")
    external_one = run_material(
        "--type",
        "ui-design",
        "--url",
        url,
        "--version-evidence",
        "现场版本一.json",
    )
    secret_one = run_material(
        "--type", "environment", "--secret-reference", "现场秘密引用一.json"
    )
    other_path = run_material(
        "--type", "requirement", "--file", "现场文本副本.md"
    )
    external_two = run_material(
        "--type",
        "ui-design",
        "--url",
        url,
        "--version-evidence",
        "现场版本二.json",
    )
    secret_retry = run_material(
        "--type", "environment", "--secret-reference", "现场秘密引用二.json"
    )
    for result in (
        first,
        first_retry,
        binary,
        external_one,
        secret_one,
        other_path,
        external_two,
        secret_retry,
    ):
        assert result.returncode == 0, result.stdout + result.stderr
    assert "CMAT-001" in first.stdout and "CMAT-001" in first_retry.stdout
    assert "CMAT-002" in binary.stdout
    assert "CMAT-003" in external_one.stdout
    assert "CMAT-004" in secret_one.stdout and "CMAT-004" in secret_retry.stdout
    assert "CMAT-005" in other_path.stdout
    assert "CMAT-006" in external_two.stdout

    bad_evidence = project / "损坏版本证据.json"
    bad_evidence.write_text('{"schema_version":"external-version-evidence.v1"}', encoding="utf-8")
    bad_secret = project / "损坏秘密引用.json"
    bad_secret.write_text('{"schema_version":"secret-reference.v1"}', encoding="utf-8")
    rejected_before = (workspace / "change-material-manifest.v1.json").read_bytes()
    rejected_event_count = len(_material_events(project))
    damaged_version = run_material(
        "--type",
        "ui-design",
        "--url",
        url,
        "--version-evidence",
        "损坏版本证据.json",
    )
    damaged_secret = run_material(
        "--type", "environment", "--secret-reference", "损坏秘密引用.json"
    )
    cross_requirement = run_cli_raw(
        [
            "change-material",
            "REQ-999",
            "CHG-001",
            "--type",
            "requirement",
            "--file",
            "现场文本.md",
        ],
        cwd=project,
    )
    command_results.append(cross_requirement)
    for result in (damaged_version, damaged_secret, cross_requirement):
        assert result.returncode == 2
    assert (workspace / "change-material-manifest.v1.json").read_bytes() == rejected_before
    assert len(_material_events(project)) == rejected_event_count

    for name, content in (("并发甲.txt", "甲"), ("并发乙.txt", "乙")):
        (project / name).write_text(content, encoding="utf-8")
    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    parallel_processes = [
        subprocess.Popen(
            [
                str(REPO_ROOT / "bin/codex-sdlc"),
                "change-material",
                "REQ-001",
                "CHG-001",
                "--type",
                "other",
                "--file",
                file_name,
            ],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for file_name in ("并发甲.txt", "并发乙.txt")
    ]
    parallel_outputs: list[str] = []
    for process in parallel_processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stdout + stderr
        parallel_outputs.append(stdout)
    assert {
        line.split("：", 1)[1]
        for output in parallel_outputs
        for line in output.splitlines()
        if line.startswith("已归档变更资料：")
    } == {"CMAT-007", "CMAT-008"}

    for index, stage in enumerate(
        (
            INTERRUPT_AFTER_MATERIAL_PUBLISH,
            INTERRUPT_AFTER_MANIFEST_PUBLISH,
            INTERRUPT_AFTER_MATERIAL_EVENT_APPEND,
        ),
        start=1,
    ):
        file_name = f"故障恢复{index}.bin"
        (project / file_name).write_bytes(f"故障点{index}".encode("utf-8") + b"\x00")
        interrupted = run_material(
            "--type", "field-evidence", "--file", file_name, stage=stage
        )
        assert interrupted.returncode == 2
        recovered = run_material("--type", "field-evidence", "--file", file_name)
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr

    manifest = _manifest(workspace)
    materials = manifest["materials"]
    assert isinstance(materials, list)
    assert [item["material_id"] for item in materials] == [
        f"CMAT-{index:03d}" for index in range(1, 12)
    ]
    assert len(_material_events(project)) == 11
    assert (workspace / "原始资料/CMAT-001").read_bytes() == text_file.read_bytes()
    assert (workspace / "原始资料/CMAT-002").read_bytes() == binary_file.read_bytes()
    assert materials[0]["source_path"] == "现场文本.md"
    assert materials[4]["source_path"] == "现场文本副本.md"
    assert materials[0]["identity_sha256"] != materials[4]["identity_sha256"]
    assert materials[2]["url"] == url
    assert materials[2]["version_evidence_sha256"] == hashlib.sha256(
        evidence_one.read_bytes()
    ).hexdigest()
    assert materials[5]["version_evidence_sha256"] == hashlib.sha256(
        evidence_two.read_bytes()
    ).hexdigest()
    assert materials[2]["identity_sha256"] != materials[5]["identity_sha256"]
    assert materials[3]["secret_reference"] == secret_document
    assert materials[3]["secret_reference_sha256"] == canonical_sha256(secret_document)

    events = _material_events(project)
    for index, item in enumerate(materials, start=1):
        event = next(event for event in events if event["event_id"] == item["event_id"])
        assert event["payload"]["manifest_sha256"] == sha256_bytes(
            canonical_json_bytes(manifest_prefix_document(manifest, index))
        )
    assert events[-1]["payload"]["manifest_sha256"] == sha256_bytes(
        canonical_json_bytes(manifest)
    )
    assert not (workspace / ".material-transactions").exists()
    assert not (workspace / ".material-staging").exists()
    assert (workspace / "status.json").read_bytes() == status_before
    assert _base_hashes(requirement_dir) == bases_before
    assert _protected_requirement_snapshot(requirement_dir) == protected_before
    creation_event_after = next(
        event
        for event in load_events(paths)
        if event.get("event_type") == "change_workspace_created"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("change_id") == "CHG-001"
    )
    assert creation_event_after == creation_event_before
    assert all(isinstance(result.returncode, int) for result in command_results)
