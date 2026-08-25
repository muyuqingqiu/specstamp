from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

# 合同测试复用真实 CLI 临时项目工厂，避免另写一套初始化和进程调用口径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_sdlc.commands import material_cmd
from codex_sdlc.commands.export_cmd import sanitize_export_payload
from codex_sdlc.core.backup import create_backup, sanitize_backup_metadata
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.external_version import (
    compare_external_version_evidence,
    normalize_external_url,
    normalized_url_sha256,
)
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.state import append_event, derive_state, load_events
from codex_sdlc.core.structured_contract import sha256_file, validate_schema_document
from test_cli_v1 import init_demo_repo, run_cli


def create_draft_project(tmp_path: Path) -> Path:
    project_dir = init_demo_repo(tmp_path)
    initialized = run_cli(["init-basic"], cwd=project_dir)
    assert initialized.returncode == 0, initialized.stderr
    created = run_cli(["draft", "create", "资料归档测试"], cwd=project_dir)
    assert created.returncode == 0, created.stderr
    return project_dir


def material_events(project_dir: Path) -> list[dict[str, object]]:
    return [event for event in load_events(build_paths(project_dir)) if event.get("event_type") == "draft_material_added"]


def material_command(
    source_flag: str,
    source_value: str,
    *,
    title: str = "原始资料",
    material_type: str = "requirement",
    extra: list[str] | None = None,
) -> list[str]:
    return [
        "material",
        "DRAFT-001",
        "--type",
        material_type,
        "--title",
        title,
        source_flag,
        source_value,
        *(extra or []),
    ]


def read_manifest(project_dir: Path) -> dict[str, object]:
    return json.loads(
        (project_dir / ".codex-sdlc/drafts/DRAFT-001/material-manifest.v1.json").read_text(encoding="utf-8")
    )


def archived_files(project_dir: Path) -> list[Path]:
    return sorted((project_dir / ".codex-sdlc/drafts/DRAFT-001/原始资料").glob("MAT-*"))


def test_file_material_keeps_original_bytes_and_versioned_manifest(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "需求说明.md"
    source.write_bytes(("# 需求\n" + "完整原文，不做改写。\n" * 2000).encode("utf-8") + b"\x00\xff")

    result = run_cli(
        material_command("--file", source.name, extra=["--sensitivity", "internal", "--scope", "FR-001"]),
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    assert "MAT-001" in result.stdout
    assert len(archived_files(project_dir)) == 1
    archived = archived_files(project_dir)[0]
    assert archived.read_bytes() == source.read_bytes()
    assert sha256_file(archived) == sha256_file(source)
    manifest = read_manifest(project_dir)
    validate_schema_document(manifest, schema_name="material-manifest.v1")
    material = manifest["materials"][0]
    assert material["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert material["size_bytes"] == len(source.read_bytes())
    assert material["source_path"] == source.name
    assert material["stored_path"].startswith("原始资料/MAT-001_")
    assert (project_dir / ".codex-sdlc/drafts/DRAFT-001/需求/material-manifest.v1.json").exists()
    artifact_index = json.loads(
        (project_dir / ".codex-sdlc/drafts/DRAFT-001/artifact-index.v1.json").read_text(encoding="utf-8")
    )
    assert any(item["source_path"] == "需求/material-manifest.v1.json" for item in artifact_index["artifacts"])


def test_same_hash_is_idempotent_and_new_role_only_updates_one_mat(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "same.bin"
    source.write_bytes(b"same-bytes\x00\xff")
    command = material_command("--file", source.name, material_type="other")

    first = run_cli(command, cwd=project_dir)
    before_events = len(material_events(project_dir))
    second = run_cli(command, cwd=project_dir)
    role_result = run_cli(
        material_command("--file", source.name, material_type="sample-data", title="同一字节的样例数据角色"),
        cwd=project_dir,
    )

    assert first.returncode == second.returncode == role_result.returncode == 0
    assert len(material_events(project_dir)) == before_events + 1
    assert len(archived_files(project_dir)) == 1
    materials = read_manifest(project_dir)["materials"]
    assert len(materials) == 1
    assert materials[0]["type"] == "other"
    assert materials[0]["roles"] == ["other", "sample-data"]


def test_revision_creates_new_mat_and_never_overwrites_old_file(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "同名说明.md"
    source.write_bytes(b"revision-one")
    first = run_cli(material_command("--file", source.name), cwd=project_dir)
    assert first.returncode == 0, first.stderr
    first_file = archived_files(project_dir)[0]
    first_bytes = first_file.read_bytes()

    source.write_bytes(b"revision-two")
    second = run_cli(material_command("--file", source.name, extra=["--supersedes", "MAT-001"]), cwd=project_dir)

    assert second.returncode == 0, second.stderr
    assert [path.name.split("_", 1)[0] for path in archived_files(project_dir)] == ["MAT-001", "MAT-002"]
    assert first_file.read_bytes() == first_bytes
    materials = read_manifest(project_dir)["materials"]
    assert materials[0]["status"] == "archived"
    assert materials[1]["supersedes"] == "MAT-001"
    assert materials[1]["status"] == "active"


@pytest.mark.parametrize("case", ["missing", "directory", "traversal", "outside-absolute", "outside-symlink", "private-key"])
def test_invalid_file_sources_leave_no_event_file_or_number(tmp_path: Path, case: str) -> None:
    project_dir = create_draft_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    raw_path = "missing.txt"
    if case == "directory":
        (project_dir / "资料目录").mkdir()
        raw_path = "资料目录"
    elif case == "traversal":
        raw_path = "../outside.txt"
    elif case == "outside-absolute":
        raw_path = str(outside)
    elif case == "outside-symlink":
        (project_dir / "outside-link").symlink_to(outside)
        raw_path = "outside-link"
    elif case == "private-key":
        private_key = project_dir / "identity.pem"
        private_key.write_bytes(
            b"x" * (2 * 1024 * 1024 + 17)
            + "-----BEGIN PRIVATE KEY-----\n测试占位内容\n-----END PRIVATE KEY-----\n".encode("utf-8")
        )
        raw_path = private_key.name

    before = len(load_events(build_paths(project_dir)))
    result = run_cli(material_command("--file", raw_path), cwd=project_dir)

    assert result.returncode != 0
    assert len(load_events(build_paths(project_dir))) == before
    assert archived_files(project_dir) == []
    assert not list((project_dir / ".codex-sdlc/drafts/DRAFT-001/.staging").glob("material-*"))


def test_all_common_private_key_headers_are_rejected_before_numbering(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    headers = [
        "ENCRYPTED PRIVATE KEY",
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ]
    for index, header in enumerate(headers):
        source = project_dir / f"private-{index}.pem"
        source.write_text(
            f"-----BEGIN {header}-----\n测试占位内容\n-----END {header}-----\n",
            encoding="utf-8",
        )
        result = run_cli(material_command("--file", source.name, material_type="environment"), cwd=project_dir)
        assert result.returncode != 0
        assert "测试占位内容" not in result.stdout + result.stderr
        assert not material_events(project_dir)
        assert archived_files(project_dir) == []

    ordinary = project_dir / "ordinary.txt"
    ordinary.write_text("普通资料", encoding="utf-8")
    accepted = run_cli(material_command("--file", ordinary.name), cwd=project_dir)
    assert accepted.returncode == 0, accepted.stderr
    assert read_manifest(project_dir)["materials"][0]["material_id"] == "MAT-001"


def test_project_internal_file_and_directory_symlinks_are_rejected(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    real_file = project_dir / "real.txt"
    real_file.write_text("真实文件", encoding="utf-8")
    file_link = project_dir / "inside-link.txt"
    file_link.symlink_to(real_file)
    real_directory = project_dir / "real-directory"
    real_directory.mkdir()
    (real_directory / "inside.txt").write_text("目录内文件", encoding="utf-8")
    directory_link = project_dir / "inside-directory-link"
    directory_link.symlink_to(real_directory, target_is_directory=True)

    for raw_path in (file_link.name, f"{directory_link.name}/inside.txt"):
        result = run_cli(material_command("--file", raw_path), cwd=project_dir)
        assert result.returncode != 0
        assert not material_events(project_dir)
        assert archived_files(project_dir) == []


def test_directory_replaced_by_symlink_during_open_is_rejected_without_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = create_draft_project(tmp_path)
    source_directory = project_dir / "source-directory"
    source_directory.mkdir()
    (source_directory / "source.txt").write_text("原始资料", encoding="utf-8")
    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    (outside_directory / "source.txt").write_text("替换内容", encoding="utf-8")
    moved_directory = project_dir / "source-directory-moved"
    original_resolver = material_cmd._resolve_project_file

    def resolve_then_replace(root: Path, raw_path: str, *, label: str) -> tuple[Path, str]:
        resolved = original_resolver(root, raw_path, label=label)
        source_directory.rename(moved_directory)
        source_directory.symlink_to(outside_directory, target_is_directory=True)
        return resolved

    monkeypatch.setattr(material_cmd, "_resolve_project_file", resolve_then_replace)
    args = SimpleNamespace(
        draft="DRAFT-001",
        content=None,
        title="竞态资料",
        type="other",
        file="source-directory/source.txt",
        url="",
        secret_reference="",
        version_evidence="",
        access_condition="public",
        sensitivity="internal",
        role=[],
        scope=[],
        supersedes="",
        task=[],
        executed_commands=[],
        source="",
        status="",
    )
    previous_cwd = Path.cwd()
    os.chdir(project_dir)
    try:
        with pytest.raises(SdlcError, match="原始资料读取失败"):
            material_cmd.run(args)
    finally:
        os.chdir(previous_cwd)

    assert not material_events(project_dir)
    assert archived_files(project_dir) == []


def test_formal_req_and_task_evidence_are_rejected_before_any_write(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "资料.md"
    source.write_text("资料", encoding="utf-8")
    before = (project_dir / ".codex-sdlc/events.jsonl").read_bytes()

    requirement_result = run_cli(
        ["material", "REQ-001", "--type", "requirement", "--title", "资料", "--file", source.name],
        cwd=project_dir,
    )
    evidence_result = run_cli(
        [
            "material",
            "DRAFT-001",
            "任务测试日志",
            "--type",
            "field-evidence",
            "--title",
            "任务证据",
            "--task",
            "T-001",
        ],
        cwd=project_dir,
    )

    assert requirement_result.returncode != 0
    assert "change-material" in requirement_result.stderr
    assert "task-evidence" in requirement_result.stderr
    assert evidence_result.returncode != 0
    assert (project_dir / ".codex-sdlc/events.jsonl").read_bytes() == before
    assert archived_files(project_dir) == []


def test_started_draft_rejects_material_without_event_or_file(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    paths = build_paths(project_dir)
    source = project_dir / "late-material.md"
    source.write_text("正式建档后到达的普通资料", encoding="utf-8")
    append_event(
        paths,
        event_type="draft_started",
        source="test-started-material-boundary",
        summary="把 DRAFT 标记为已正式建档",
        payload={"draft_id": "DRAFT-001", "started_requirement_id": "REQ-001"},
        requirement_id="REQ-001",
    )
    before = paths.events_file.read_bytes()

    result = run_cli(material_command("--file", source.name), cwd=project_dir)

    assert result.returncode != 0
    assert "已经正式建档" in result.stderr
    assert paths.events_file.read_bytes() == before
    assert archived_files(project_dir) == []


def test_unversioned_url_is_blocked_without_fabricated_content_hash(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://Example.com:443/design?a=2&b=1#page"

    result = run_cli(material_command("--url", url, material_type="ui-design", title="页面设计"), cwd=project_dir)

    assert result.returncode == 0, result.stderr
    material = read_manifest(project_dir)["materials"][0]
    assert material["url"] == normalize_external_url(url)
    assert material["normalized_url_sha256"] == normalized_url_sha256(url)
    assert material["version_evidence"]["status"] == "unversioned"
    assert "sha256" not in material
    state = derive_state(build_paths(project_dir))
    draft = state["drafts"]["DRAFT-001"]
    assert draft["material_gate"] == {
        "status": "blocked",
        "can_review": False,
        "blocking_material_ids": ["MAT-001"],
    }
    assert draft["assessment"]["can_start"] is False
    append_event(
        build_paths(project_dir),
        event_type="draft_mutated",
        source="test-material-gate",
        summary="模拟阻塞后再次提交复核",
        payload={
            "draft_id": "DRAFT-001",
            "operation": "update",
            "changes": {"model_review": {"schema": "test-review"}},
        },
    )
    replayed = derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]
    assert replayed["model_review"] is None
    assert replayed["material_gate"]["can_review"] is False


@pytest.mark.parametrize(
    "url_template",
    [
        "https://example.com/spec?X-Amz-Signature={secret}",
        "https://example.com/spec?accessToken={secret}",
        "https://example.com/spec?access-token={secret}",
        "https://example.com/spec?access_token={secret}",
        "https://example.com/spec?access%2554oken={secret}",
        "https://example.com/spec?X-API-Key={secret}",
        "https://example.com/spec#token%3D{secret}",
        "https://example.com/spec#accessToken%253D{secret}",
    ],
)
def test_url_with_secret_parameter_is_rejected_without_echo_or_event(tmp_path: Path, url_template: str) -> None:
    project_dir = create_draft_project(tmp_path)
    fake_signature = "测试签名占位值123456789"
    url = url_template.format(secret=fake_signature)
    before = (project_dir / ".codex-sdlc/events.jsonl").read_bytes()

    result = run_cli(material_command("--url", url, material_type="api-document"), cwd=project_dir)

    assert result.returncode != 0
    assert fake_signature not in result.stdout + result.stderr
    assert (project_dir / ".codex-sdlc/events.jsonl").read_bytes() == before
    assert not material_events(project_dir)


def test_url_sensitive_check_keeps_normal_business_parameters(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://example.com/spec?pageToken=page-2&value=普通业务值"

    result = run_cli(material_command("--url", url, material_type="api-document"), cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert read_manifest(project_dir)["materials"][0]["url"] == normalize_external_url(url)


def evidence_document(url: str, revision: str) -> dict[str, object]:
    return {
        "schema_version": "external-version-evidence.v1",
        "normalized_url_sha256": normalized_url_sha256(url),
        "status": "confirmed",
        "evidence": {
            "kind": "immutable_revision",
            "provider": "document",
            "revision": revision,
        },
    }


def test_external_version_only_compares_explicit_evidence_and_detects_drift(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://example.com/spec"
    expected = evidence_document(url, "rev-001")
    observed_same = evidence_document(url, "rev-001")
    observed_changed = evidence_document(url, "rev-002")
    evidence_file = project_dir / "external-version.json"
    evidence_file.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    result = run_cli(
        material_command("--url", url, extra=["--version-evidence", evidence_file.name]),
        cwd=project_dir,
    )

    assert result.returncode == 0, result.stderr
    assert read_manifest(project_dir)["materials"][0]["status"] == "confirmed"
    assert derive_state(build_paths(project_dir))["drafts"]["DRAFT-001"]["material_gate"]["can_review"] is True
    assert compare_external_version_evidence(expected, observed_same)["status"] == "confirmed"
    assert compare_external_version_evidence(expected, observed_changed)["status"] == "drifted"
    assert normalize_external_url("HTTPS://Example.com:443/a?z=2&a=1#ignored") == "https://example.com/a?a=1&z=2"


def test_external_revision_drift_requires_explicit_supersedes(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://example.com/spec"
    evidence_file = project_dir / "external-version.json"
    evidence_file.write_text(json.dumps(evidence_document(url, "rev-001")), encoding="utf-8")
    first = run_cli(material_command("--url", url, extra=["--version-evidence", evidence_file.name]), cwd=project_dir)
    assert first.returncode == 0, first.stderr
    evidence_file.write_text(json.dumps(evidence_document(url, "rev-002")), encoding="utf-8")
    before = len(material_events(project_dir))

    drift = run_cli(material_command("--url", url, extra=["--version-evidence", evidence_file.name]), cwd=project_dir)
    revised = run_cli(
        material_command(
            "--url",
            url,
            extra=["--version-evidence", evidence_file.name, "--supersedes", "MAT-001"],
        ),
        cwd=project_dir,
    )

    assert drift.returncode != 0
    assert len(material_events(project_dir)) == before + 1
    assert revised.returncode == 0, revised.stderr
    materials = read_manifest(project_dir)["materials"]
    assert materials[0]["status"] == "archived"
    assert materials[1]["status"] == "confirmed"


def test_external_revision_rejects_unrelated_target_and_archives_only_current_url_mat(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://example.com/versioned-spec"
    evidence_file = project_dir / "external-version.json"
    evidence_file.write_text(json.dumps(evidence_document(url, "rev-001")), encoding="utf-8")
    first = run_cli(material_command("--url", url, extra=["--version-evidence", evidence_file.name]), cwd=project_dir)
    assert first.returncode == 0, first.stderr
    local_file = project_dir / "unrelated.txt"
    local_file.write_text("无关本地资料", encoding="utf-8")
    second = run_cli(material_command("--file", local_file.name), cwd=project_dir)
    assert second.returncode == 0, second.stderr
    evidence_file.write_text(json.dumps(evidence_document(url, "rev-002")), encoding="utf-8")
    before = len(material_events(project_dir))

    wrong = run_cli(
        material_command(
            "--url",
            url,
            extra=["--version-evidence", evidence_file.name, "--supersedes", "MAT-002"],
        ),
        cwd=project_dir,
    )

    assert wrong.returncode != 0
    assert len(material_events(project_dir)) == before
    correct = run_cli(
        material_command(
            "--url",
            url,
            extra=["--version-evidence", evidence_file.name, "--supersedes", "MAT-001"],
        ),
        cwd=project_dir,
    )
    assert correct.returncode == 0, correct.stderr
    materials = read_manifest(project_dir)["materials"]
    assert [(item["material_id"], item["status"]) for item in materials] == [
        ("MAT-001", "archived"),
        ("MAT-002", "active"),
        ("MAT-003", "confirmed"),
    ]
    assert materials[2]["supersedes"] == "MAT-001"
    assert len(
        [
            item
            for item in materials
            if item["source_kind"] == "external-reference" and item["status"] != "archived"
        ]
    ) == 1


def test_external_revision_rejects_multiple_active_versions_deterministically(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    url = "https://example.com/duplicate-active"
    first = {
        "material_id": "MAT-001",
        "source_kind": "external-reference",
        "type": "requirement",
        "roles": ["requirement"],
        "title": "版本一",
        "url": normalize_external_url(url),
        "normalized_url_sha256": normalized_url_sha256(url),
        "access_condition": "public",
        "version_evidence": evidence_document(url, "rev-001"),
        "sensitivity": "internal",
        "applies_to": [],
        "status": "confirmed",
    }
    second = json.loads(json.dumps(first))
    second["material_id"] = "MAT-002"
    second["title"] = "版本二"
    second["version_evidence"] = evidence_document(url, "rev-002")
    draft = {"draft_id": "DRAFT-001", "materials": [first, second]}
    evidence_file = project_dir / "external-version.json"
    evidence_file.write_text(json.dumps(evidence_document(url, "rev-003")), encoding="utf-8")
    args = SimpleNamespace(
        url=url,
        version_evidence=evidence_file.name,
        secret_reference="",
        access_condition="public",
        sensitivity="internal",
    )

    with pytest.raises(SdlcError, match="多个活动版本"):
        material_cmd._reference_material(
            project_dir,
            build_paths(project_dir),
            draft,
            args,
            title="版本三",
            material_type="requirement",
            roles=["requirement"],
            scopes=[],
            supersedes="MAT-001",
        )


def test_external_local_snapshot_evidence_checks_real_archived_hash(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    snapshot_source = project_dir / "snapshot.bin"
    snapshot_source.write_bytes(b"stable-local-snapshot")
    archived = run_cli(material_command("--file", snapshot_source.name, material_type="other"), cwd=project_dir)
    assert archived.returncode == 0, archived.stderr
    snapshot = read_manifest(project_dir)["materials"][0]
    url = "https://example.com/external-design"
    evidence = {
        "schema_version": "external-version-evidence.v1",
        "normalized_url_sha256": normalized_url_sha256(url),
        "status": "confirmed",
        "evidence": {
            "kind": "local_snapshot",
            "material_id": "MAT-001",
            "sha256": snapshot["sha256"],
        },
    }
    evidence_file = project_dir / "local-snapshot-evidence.json"
    evidence_file.write_text(json.dumps(evidence), encoding="utf-8")

    accepted = run_cli(
        material_command(
            "--url",
            url,
            title="外部设计本地快照",
            material_type="ui-design",
            extra=["--version-evidence", evidence_file.name],
        ),
        cwd=project_dir,
    )

    assert accepted.returncode == 0, accepted.stderr
    materials = read_manifest(project_dir)["materials"]
    assert materials[1]["status"] == "confirmed"
    assert materials[1]["version_evidence"]["evidence"]["material_id"] == "MAT-001"


def test_secret_reference_rejects_value_and_never_leaks_through_outputs_or_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = create_draft_project(tmp_path)
    fake_secret = "测试占位敏感值-123456789"
    invalid_file = project_dir / "invalid-secret.json"
    invalid_file.write_text(
        json.dumps(
            {
                "schema_version": "secret-reference.v1",
                "kind": "environment-variable",
                "identifier": "DEMO_TOKEN",
                "access": "runtime-only",
                "value": fake_secret,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    invalid = run_cli(
        material_command(
            "--secret-reference",
            invalid_file.name,
            material_type="environment",
            extra=["--sensitivity", "secret-reference"],
        ),
        cwd=project_dir,
    )
    assert invalid.returncode != 0
    assert fake_secret not in invalid.stdout + invalid.stderr
    assert not material_events(project_dir)

    valid_reference = {
        "schema_version": "secret-reference.v1",
        "kind": "environment-variable",
        "identifier": "DEMO_TOKEN",
        "access": "runtime-only",
    }
    valid_file = project_dir / "secret-reference.json"
    valid_file.write_text(json.dumps(valid_reference), encoding="utf-8")
    accepted = run_cli(
        material_command(
            "--secret-reference",
            valid_file.name,
            material_type="environment",
            title="运行凭据引用",
            extra=["--sensitivity", "secret-reference"],
        ),
        cwd=project_dir,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert fake_secret not in accepted.stdout + accepted.stderr
    assert read_manifest(project_dir)["materials"][0]["secret_reference"] == valid_reference

    malicious_payload = {
        "secret_reference": {**valid_reference, "secret_value": fake_secret},
        "password": fake_secret,
        "ClientSecret": "测试客户端秘密占位值-123456",
        "nested": [
            {"refresh-token": "测试刷新令牌占位值-123456"},
            {"AUTH_TOKEN": "测试鉴权令牌占位值-123456"},
            {"X-API-Key": "测试接口密钥占位值-123456"},
            {"value": "普通业务值必须保留"},
        ],
    }
    secret_values = [
        fake_secret,
        "测试客户端秘密占位值-123456",
        "测试刷新令牌占位值-123456",
        "测试鉴权令牌占位值-123456",
        "测试接口密钥占位值-123456",
    ]
    sanitized_backup = sanitize_backup_metadata(malicious_payload)
    sanitized_export = sanitize_export_payload(malicious_payload)
    for secret_value in secret_values:
        assert secret_value not in json.dumps(sanitized_backup, ensure_ascii=False)
        assert secret_value not in json.dumps(sanitized_export, ensure_ascii=False)
    assert sanitized_backup["nested"][3]["value"] == "普通业务值必须保留"
    assert sanitized_export["nested"][3]["value"] == "普通业务值必须保留"

    # 项目级备份会包含 events.jsonl；额外写一条异常结构，验证归档层仍会字段级脱敏。
    append_event(
        build_paths(project_dir),
        event_type="malformed-secret-test",
        source="test-secret-sanitizer",
        summary="验证异常秘密字段仍会脱敏",
        payload=malicious_payload,
    )
    # 再写一条事件，让事件备份也真实包含异常字段，项目归档必须同时清理主事件和 .jsonl.bak。
    append_event(
        build_paths(project_dir),
        event_type="material-sanitizer-followup",
        source="test-secret-sanitizer",
        summary="生成包含异常字段的事件备份",
        payload={"status": "done"},
    )
    exported = run_cli(["export"], cwd=project_dir)
    assert exported.returncode == 0, exported.stderr
    export_bytes = (
        project_dir / ".codex-sdlc/exports/all-requirements.md"
    ).read_bytes()
    for secret_value in secret_values:
        assert secret_value not in exported.stdout + exported.stderr
        assert secret_value.encode("utf-8") not in export_bytes
    backup_home = tmp_path / "backup-home"
    monkeypatch.setenv("CODEX_SDLC_BACKUP_HOME", str(backup_home))
    backup_result = create_backup(build_paths(project_dir))
    archive_path = Path(backup_result["project_snapshots"][0]["archive"])
    with tarfile.open(archive_path, "r:gz") as archive:
        archive_bytes = b"".join(
            extracted.read()
            for member in archive.getmembers()
            if member.isfile() and (extracted := archive.extractfile(member)) is not None
        )
    for secret_value in secret_values:
        assert secret_value.encode("utf-8") not in archive_bytes
    assert "普通业务值必须保留".encode("utf-8") in archive_bytes


def test_source_hash_drift_is_rejected_and_temporary_file_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    temporary = tmp_path / "copy.tmp"
    source.write_bytes(b"first-version")
    expected = sha256_file(source)
    original_copy = material_cmd.shutil.copyfileobj

    def copy_and_mutate(source_handle: io.BufferedReader, target_handle: io.BufferedWriter, length: int) -> None:
        original_copy(source_handle, target_handle, length)
        source.write_bytes(b"second-version")

    monkeypatch.setattr(material_cmd.shutil, "copyfileobj", copy_and_mutate)
    with pytest.raises(SdlcError, match="复制期间发生变化"):
        material_cmd._copy_original_file(source, temporary, expected)
    assert not temporary.exists()


def test_projection_failure_rolls_back_file_event_and_material_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "rollback.bin"
    source.write_bytes(b"rollback-source")
    paths = build_paths(project_dir)
    before_events = paths.events_file.read_bytes()
    before_backups = {
        path.name: path.read_bytes()
        for path in paths.backups_dir.glob("events-*.jsonl.bak")
    }

    def fail_refresh(_current_paths: object) -> object:
        raise SdlcError("注入资料投影失败", exit_code=1)

    monkeypatch.setattr(material_cmd, "refresh_materialized_state", fail_refresh)
    args = SimpleNamespace(
        draft="DRAFT-001",
        content=None,
        title="回滚资料",
        type="other",
        file=source.name,
        url="",
        secret_reference="",
        version_evidence="",
        access_condition="public",
        sensitivity="internal",
        role=[],
        scope=[],
        supersedes="",
        task=[],
        executed_commands=[],
        source="",
        status="",
    )
    previous_cwd = Path.cwd()
    os.chdir(project_dir)
    try:
        with pytest.raises(SdlcError, match="注入资料投影失败"):
            material_cmd.run(args)
    finally:
        os.chdir(previous_cwd)

    assert paths.events_file.read_bytes() == before_events
    assert {
        path.name: path.read_bytes()
        for path in paths.backups_dir.glob("events-*.jsonl.bak")
    } == before_backups
    assert archived_files(project_dir) == []
    assert not list((project_dir / ".codex-sdlc/drafts/DRAFT-001/.staging").glob("material-*"))


def test_versioned_schemas_reject_unknown_enums_and_secret_value_fields() -> None:
    bad_secret = {
        "schema_version": "secret-reference.v1",
        "kind": "inline-value",
        "identifier": "DEMO_TOKEN",
        "access": "runtime-only",
        "secret_value": "测试占位值",
    }
    with pytest.raises(SdlcError):
        validate_schema_document(bad_secret, schema_name="secret-reference.v1")

    invalid_manifest = {
        "schema_version": "material-manifest.v1",
        "draft_id": "DRAFT-001",
        "materials": [
            {
                "material_id": "MAT-001",
                "source_kind": "external-reference",
                "type": "api-document",
                "roles": ["api-document"],
                "title": "非法外部版本证据",
                "sensitivity": "internal",
                "applies_to": [],
                "status": "confirmed",
                "url": "https://example.com/api",
                "normalized_url_sha256": "a" * 64,
                "access_condition": "public",
                "version_evidence": {"client_secret": "测试 Schema 秘密占位值"},
            }
        ],
    }
    with pytest.raises(SdlcError):
        validate_schema_document(invalid_manifest, schema_name="material-manifest.v1")
    invalid_manifest["materials"][0]["version_evidence"] = {
        **evidence_document("https://example.com/api", "rev-001"),
        "client_secret": "测试 Schema 秘密占位值",
    }
    with pytest.raises(SdlcError):
        validate_schema_document(invalid_manifest, schema_name="material-manifest.v1")
    invalid_manifest["materials"][0]["version_evidence"] = {
        "schema_version": "external-version-evidence.v1",
        "normalized_url_sha256": normalized_url_sha256("https://example.com/api"),
        "status": "confirmed",
        "evidence": None,
    }
    with pytest.raises(SdlcError):
        validate_schema_document(invalid_manifest, schema_name="material-manifest.v1")
    invalid_manifest["materials"][0]["version_evidence"]["status"] = "arbitrary"
    with pytest.raises(SdlcError):
        validate_schema_document(invalid_manifest, schema_name="material-manifest.v1")

    url = "https://example.com/api"
    etag_evidence = {
        "schema_version": "external-version-evidence.v1",
        "normalized_url_sha256": normalized_url_sha256(url),
        "status": "confirmed",
        "evidence": {
            "kind": "etag_content",
            "etag": '"revision-1"',
            "fetched_sha256": "a" * 64,
            "fetched_at": "2026-07-16T03:00:00+08:00",
        },
    }
    validate_schema_document(etag_evidence, schema_name="external-version-evidence.v1")
    changed = json.loads(json.dumps(etag_evidence))
    changed["evidence"]["fetched_sha256"] = "b" * 64
    assert compare_external_version_evidence(etag_evidence, changed)["status"] == "drifted"


@pytest.mark.parametrize(
    ("material_status", "evidence_status", "valid"),
    [
        ("confirmed", "confirmed", True),
        ("unversioned", "unversioned", True),
        ("archived", "confirmed", True),
        ("archived", "unversioned", True),
        ("confirmed", "unversioned", False),
        ("unversioned", "confirmed", False),
    ],
)
def test_external_material_status_matches_version_evidence_status(
    material_status: str, evidence_status: str, valid: bool
) -> None:
    url = "https://example.com/status-contract"
    version_evidence = (
        evidence_document(url, "rev-001")
        if evidence_status == "confirmed"
        else {
            "schema_version": "external-version-evidence.v1",
            "normalized_url_sha256": normalized_url_sha256(url),
            "status": "unversioned",
            "evidence": None,
        }
    )
    manifest = {
        "schema_version": "material-manifest.v1",
        "draft_id": "DRAFT-001",
        "materials": [
            {
                "material_id": "MAT-001",
                "source_kind": "external-reference",
                "type": "requirement",
                "roles": ["requirement"],
                "title": "外部状态合同",
                "sensitivity": "internal",
                "applies_to": [],
                "status": material_status,
                "url": normalize_external_url(url),
                "normalized_url_sha256": normalized_url_sha256(url),
                "access_condition": "public",
                "version_evidence": version_evidence,
            }
        ],
    }

    if valid:
        validate_schema_document(manifest, schema_name="material-manifest.v1")
    else:
        with pytest.raises(SdlcError):
            validate_schema_document(manifest, schema_name="material-manifest.v1")


@pytest.mark.parametrize(
    ("interrupt_point", "event_committed"),
    [
        ("after_temp_copy", False),
        ("after_atomic_rename", False),
        ("after_event_append", True),
    ],
)
def test_real_process_interrupt_recovers_to_complete_result(
    tmp_path: Path, interrupt_point: str, event_committed: bool
) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "interrupt.bin"
    source.write_bytes(os.urandom(1024 * 1024))
    command = material_command("--file", source.name, material_type="other")

    interrupted = run_cli(
        command,
        cwd=project_dir,
        extra_env={"CODEX_SDLC_MATERIAL_INTERRUPT_AT": interrupt_point},
    )

    assert interrupted.returncode == 86
    assert bool(material_events(project_dir)) is event_committed
    journals = list((project_dir / ".codex-sdlc/drafts/DRAFT-001/.staging").glob("material-transaction-*.json"))
    assert len(journals) == 1

    recovered = run_cli(command, cwd=project_dir)

    assert recovered.returncode == 0, recovered.stderr
    assert len(material_events(project_dir)) == 1
    assert len(archived_files(project_dir)) == 1
    assert archived_files(project_dir)[0].read_bytes() == source.read_bytes()
    assert not list((project_dir / ".codex-sdlc/drafts/DRAFT-001/.staging").glob("material-*"))


def test_recovery_discards_only_partial_last_event_line(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "partial-event.bin"
    source.write_bytes(b"partial-event-boundary")
    command = material_command("--file", source.name, material_type="other")
    interrupted = run_cli(
        command,
        cwd=project_dir,
        extra_env={"CODEX_SDLC_MATERIAL_INTERRUPT_AT": "after_atomic_rename"},
    )
    assert interrupted.returncode == 86
    with (project_dir / ".codex-sdlc/events.jsonl").open("ab") as handle:
        handle.write(b'{"event_id":"incomplete-material-event"')

    recovered = run_cli(command, cwd=project_dir)

    assert recovered.returncode == 0, recovered.stderr
    assert len(material_events(project_dir)) == 1
    assert len(archived_files(project_dir)) == 1
    assert not list((project_dir / ".codex-sdlc/drafts/DRAFT-001/.staging").glob("material-*"))


def test_reference_event_interrupt_is_rebuilt_by_idempotent_retry(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    command = material_command("--url", "https://example.com/reference", material_type="api-document")

    interrupted = run_cli(
        command,
        cwd=project_dir,
        extra_env={"CODEX_SDLC_MATERIAL_INTERRUPT_AT": "after_event_append"},
    )

    assert interrupted.returncode == 86
    assert len(material_events(project_dir)) == 1
    assert not (project_dir / ".codex-sdlc/drafts/DRAFT-001/material-manifest.v1.json").exists()

    recovered = run_cli(command, cwd=project_dir)

    assert recovered.returncode == 0, recovered.stderr
    assert len(material_events(project_dir)) == 1
    assert read_manifest(project_dir)["materials"][0]["status"] == "unversioned"


def test_archived_hash_drift_blocks_refresh_without_changing_event_log(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    source = project_dir / "source.bin"
    source.write_bytes(b"immutable-original")
    result = run_cli(material_command("--file", source.name, material_type="other"), cwd=project_dir)
    assert result.returncode == 0, result.stderr
    events_before = (project_dir / ".codex-sdlc/events.jsonl").read_bytes()
    archived_files(project_dir)[0].write_bytes(b"tampered")

    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project_dir)

    assert refreshed.returncode != 0
    assert "哈希已经变化" in refreshed.stderr
    assert (project_dir / ".codex-sdlc/events.jsonl").read_bytes() == events_before


def test_material_does_not_touch_existing_formal_original_directory(tmp_path: Path) -> None:
    project_dir = create_draft_project(tmp_path)
    formal_original = project_dir / ".codex-sdlc/requirements/REQ-001-existing/original/requirement.v1.md"
    formal_original.parent.mkdir(parents=True)
    formal_original.write_bytes(b"formal-original\x00\xff")
    before = formal_original.read_bytes()
    source = project_dir / "draft-source.md"
    source.write_text("DRAFT 原始资料", encoding="utf-8")

    result = run_cli(material_command("--file", source.name), cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert formal_original.read_bytes() == before
