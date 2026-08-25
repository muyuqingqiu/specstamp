from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat

import pytest

from codex_sdlc.core import fact_review_trust, review_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.schemas import load_schema


def _project(tmp_path: Path) -> tuple[Path, object]:
    project = tmp_path / "项目"
    project.mkdir()
    (project / "需求").mkdir()
    (project / "需求" / "原始说明.md").write_text("保留原始需求。\n", encoding="utf-8")
    (project / "需求" / "拆分.json").write_text('{"fr":"FR-001"}\n', encoding="utf-8")
    return project, build_paths(project)


@pytest.mark.parametrize("stage", sorted(review_contract.REVIEW_STAGES))
def test_three_review_stages_share_one_request_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    project, paths = _project(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")

    request = review_contract.build_review_request(
        paths,
        review_id="REV-001",
        stage=stage,
        owner_id="DRAFT-001" if stage != "task_plan" else "REQ-001",
        input_paths=["需求/拆分.json", "需求/原始说明.md"],
        required_checks=["检查完整覆盖"],
        created_at="2026-07-16T00:00:00Z",
    )

    assert load_schema("review-request.v1")["$id"] == "review-request.v1"
    assert request["producer_run_id"] == "producer-run"
    assert request["input_paths"] == ["需求/原始说明.md", "需求/拆分.json"]
    assert request["input_hashes"]["需求/原始说明.md"] == hashlib.sha256(
        (project / "需求" / "原始说明.md").read_bytes()
    ).hexdigest()
    assert review_contract.validate_review_request(paths, request) == request


def test_request_hashes_only_come_from_controlled_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    request = review_contract.build_review_request(
        paths,
        review_id="REV-001",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["需求/原始说明.md", "需求/拆分.json"],
    )

    forged = deepcopy(request)
    forged["input_hashes"]["需求/原始说明.md"] = "0" * 64
    with pytest.raises(SdlcError, match="真实输入文件已经变化"):
        review_contract.validate_review_request(paths, forged)

    missing = deepcopy(request)
    missing["input_hashes"].pop("需求/拆分.json")
    with pytest.raises(SdlcError, match="完整一致"):
        review_contract.validate_review_request(paths, missing, verify_files=False)

    extra = deepcopy(request)
    extra["input_hashes"]["需求/不存在.md"] = "1" * 64
    with pytest.raises(SdlcError, match="完整一致"):
        review_contract.validate_review_request(paths, extra, verify_files=False)

    (project / "需求" / "原始说明.md").write_text("输入已改变。\n", encoding="utf-8")
    with pytest.raises(SdlcError, match="真实输入文件已经变化"):
        review_contract.validate_review_request(paths, request)


@pytest.mark.parametrize("damage", ["missing", "extra", "wrong-stage", "wrong-status"])
def test_request_schema_rejects_incomplete_or_extra_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    _project_dir, paths = _project(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    request = review_contract.build_review_request(
        paths,
        review_id="REV-001",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["需求/原始说明.md"],
    )
    if damage == "missing":
        request.pop("owner_id")
    elif damage == "extra":
        request["caller_input_hash"] = "不可信"
    elif damage == "wrong-stage":
        request["stage"] = "module_review"
    else:
        request["status"] = "passed"

    with pytest.raises(SdlcError, match="Schema 校验失败"):
        review_contract.validate_review_request(paths, request, verify_files=False)


def test_request_rejects_missing_identity_and_unsafe_paths_without_trust_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    with pytest.raises(SdlcError, match="CODEX_THREAD_ID"):
        fact_review_trust.create_trusted_review_request(
            paths,
            review_id="REV-001",
            stage="requirement_split",
            owner_id="DRAFT-001",
            input_paths=["需求/原始说明.md"],
        )
    assert not (paths.sdlc_dir / "trust" / "reviews").exists()

    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    with pytest.raises(SdlcError, match="项目内相对文件"):
        review_contract.build_review_request(
            paths,
            review_id="REV-001",
            stage="requirement_split",
            owner_id="DRAFT-001",
            input_paths=["../越界.md"],
        )


def test_request_registration_is_idempotent_and_failed_write_leaves_no_business_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project_dir, paths = _project(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    first = fact_review_trust.create_trusted_review_request(
        paths,
        review_id="REV-001",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["需求/原始说明.md"],
        created_at="2026-07-16T00:00:00Z",
    )
    retry = fact_review_trust.create_trusted_review_request(
        paths,
        review_id="REV-001",
        stage="requirement_split",
        owner_id="DRAFT-001",
        input_paths=["需求/原始说明.md"],
        created_at="2026-07-16T00:01:00Z",
    )
    assert retry.idempotent is True
    assert retry.request == first.request
    assert len(fact_review_trust.load_review_registry(paths)["requests"]) == 1
    key_path = paths.sdlc_dir / "trust" / "reviews" / ".key"
    assert stat.S_IMODE(key_path.stat().st_mode) & 0o077 == 0

    second_project = tmp_path / "写入失败项目"
    second_project.mkdir()
    (second_project / "输入.md").write_text("内容\n", encoding="utf-8")
    second_paths = build_paths(second_project)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("模拟登记发布失败")

    monkeypatch.setattr(fact_review_trust.os, "replace", fail_replace)
    with pytest.raises(OSError, match="模拟登记发布失败"):
        fact_review_trust.create_trusted_review_request(
            second_paths,
            review_id="REV-002",
            stage="requirement_split",
            owner_id="DRAFT-002",
            input_paths=["输入.md"],
        )
    trust_dir = second_paths.sdlc_dir / "trust" / "reviews"
    assert not (trust_dir / ".key").exists()
    assert not (trust_dir / "registry.json").exists()
    assert not list(trust_dir.glob(".registry.json.*.tmp"))


def test_request_rejects_file_and_directory_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    file_link = project / "需求" / "说明链接.md"
    file_link.symlink_to(project / "需求" / "原始说明.md")
    real_directory = project / "真实目录"
    real_directory.mkdir()
    (real_directory / "输入.md").write_text("真实内容\n", encoding="utf-8")
    directory_link = project / "目录链接"
    directory_link.symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")

    for input_path in ("需求/说明链接.md", "目录链接/输入.md"):
        with pytest.raises(SdlcError, match="符号链接"):
            review_contract.build_review_request(
                paths,
                review_id="REV-001",
                stage="requirement_split",
                owner_id="DRAFT-001",
                input_paths=[input_path],
            )


def test_request_rejects_directory_link_replacement_during_safe_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project(tmp_path)
    source = project / "竞态目录"
    source.mkdir()
    (source / "输入.md").write_text("锁内内容\n", encoding="utf-8")
    outside = tmp_path / "外部目录"
    outside.mkdir()
    (outside / "输入.md").write_text("替换内容\n", encoding="utf-8")
    moved = project / "原竞态目录"
    original = review_contract._lexical_metadata
    calls = 0

    def inspect_then_replace(root: Path, relative_path: str) -> list[os.stat_result]:
        nonlocal calls
        result = original(root, relative_path)
        calls += 1
        if calls == 1:
            source.rename(moved)
            source.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(review_contract, "_lexical_metadata", inspect_then_replace)
    monkeypatch.setenv("CODEX_THREAD_ID", "producer-run")
    with pytest.raises(SdlcError, match="安全读取|发生变化"):
        review_contract.build_review_request(
            paths,
            review_id="REV-001",
            stage="requirement_split",
            owner_id="DRAFT-001",
            input_paths=["竞态目录/输入.md"],
        )
    assert not (paths.sdlc_dir / "trust" / "reviews").exists()
