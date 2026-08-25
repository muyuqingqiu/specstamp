from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core import start_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.formal_manifest_contract import (
    build_document_first_formal_package,
)
from test_formal_manifest_completeness import _ready_workspace


def _fixture() -> tuple[TemporaryDirectory[str], object, dict[str, object], dict[str, object]]:
    context: TemporaryDirectory[str] = TemporaryDirectory(prefix="t017-start-contract-")
    paths, state, index = _ready_workspace(Path(context.name))
    paths.requirements_dir.mkdir(parents=True)
    package = build_document_first_formal_package(
        paths,
        "DRAFT-001",
        state=state,
        artifact_index=index,
    )
    return context, paths, state, package


def _pass_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        start_contract,
        "_reference_index",
        lambda *_args, **_kwargs: {
            "schema_version": "reference-index.v1",
            "requirement_id": "REQ-001",
            "entries": {},
        },
    )


def test_document_first_preflight_passes_without_facts_or_fixed_body_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, paths, state, package = _fixture()
    _pass_reference(monkeypatch)
    try:
        result = start_contract.preflight_document_first_start(
            paths,
            package,
            state=state,
        )
    finally:
        context.cleanup()

    assert result["mode"] == "document-first"
    assert result["source_draft_id"] == "DRAFT-001"
    assert result["requirement_id"] == "REQ-001"
    assert result["target_directory"] == "REQ-001"
    for forbidden in (
        "fact_bundle",
        "requirement_facts",
        "design_facts",
        "model_review",
        "technical_goal",
        "modules",
        "data_structures",
        "interfaces",
        "state_flow",
        "data_flow",
        "permissions_security",
        "error_handling",
        "test_strategy",
        "risks",
        "out_of_scope",
        "requirement_coverage",
    ):
        assert forbidden not in result["package"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda package, _state: package.pop("source_draft_id"), "source_draft_id"),
        (
            lambda package, _state: package.update({"source_draft_id": "DRAFT-999"}),
            "DRAFT 不存在",
        ),
        (
            lambda _package, state: state["drafts"]["DRAFT-001"].update(
                {"status": "started"}
            ),
            "已经完成过正式建档",
        ),
        (
            lambda _package, state: state["drafts"]["DRAFT-001"].update(
                {"status": "design_reviewing"}
            ),
            "不是 start_ready",
        ),
        (
            lambda _package, state: state["drafts"]["DRAFT-001"][
                "_requirement_confirmation_state"
            ].update({"status": "stale"}),
            "需求确认缺失或已经失效",
        ),
        (
            lambda _package, state: state["drafts"]["DRAFT-001"][
                "_requirement_review_state"
            ]["reviews"][0].update({"effective_status": "needs_fix"}),
            "需求审核缺少唯一",
        ),
        (
            lambda _package, state: state["drafts"]["DRAFT-001"][
                "_integrated_design_review_state"
            ]["reviews"].append(
                deepcopy(
                    state["drafts"]["DRAFT-001"][
                        "_integrated_design_review_state"
                    ]["reviews"][0]
                )
            ),
            "整体设计审核缺少唯一",
        ),
        (
            lambda package, _state: package.update(
                {"source_revision_sha256": "0" * 64}
            ),
            "DRAFT 修订已经过期",
        ),
        (
            lambda package, _state: package["artifact_manifest"].pop(),
            "应归档集合不完全一致",
        ),
        (
            lambda package, _state: package["reviews"].update(
                {"integrated_design": "REV-999"}
            ),
            "审核 REV 不是当前",
        ),
    ],
)
def test_document_first_rejections_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    message: str,
) -> None:
    context, paths, state, package = _fixture()
    _pass_reference(monkeypatch)
    before = {
        path.relative_to(paths.root).as_posix(): path.read_bytes()
        for path in paths.root.rglob("*")
        if path.is_file()
    }
    mutate(package, state)
    try:
        with pytest.raises(SdlcError, match=message):
            start_contract.preflight_document_first_start(
                paths,
                package,
                state=state,
            )
        after = {
            path.relative_to(paths.root).as_posix(): path.read_bytes()
            for path in paths.root.rglob("*")
            if path.is_file()
        }
    finally:
        context.cleanup()

    assert after == before


def test_explicit_source_wins_when_multiple_drafts_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, paths, state, package = _fixture()
    _pass_reference(monkeypatch)
    state["drafts"]["DRAFT-002"] = deepcopy(state["drafts"]["DRAFT-001"])
    state["drafts"]["DRAFT-002"]["draft_id"] = "DRAFT-002"
    try:
        result = start_contract.preflight_document_first_start(
            paths,
            package,
            state=state,
        )
    finally:
        context.cleanup()

    assert result["source_draft_id"] == "DRAFT-001"


def test_legacy_facts_profile_cannot_enter_document_first_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, paths, state, _package = _fixture()
    _pass_reference(monkeypatch)
    legacy = {
        "formal_contract_version": "formal.v3",
        "fact_bundle": {"source_index_file": "source-index.json"},
    }
    try:
        with pytest.raises(SdlcError, match="document-first.v1"):
            start_contract.preflight_document_first_start(
                paths,
                legacy,
                state=state,
            )
    finally:
        context.cleanup()


@pytest.mark.parametrize("collision", ["directory", "symlink"])
def test_target_directory_collision_is_rejected_without_residue(
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    context, paths, state, package = _fixture()
    _pass_reference(monkeypatch)
    target = paths.requirements_dir / "REQ-001"
    if collision == "directory":
        target.mkdir()
    else:
        outside = Path(context.name) / "outside"
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    before = sorted(path.relative_to(paths.root).as_posix() for path in paths.root.rglob("*"))
    try:
        with pytest.raises(SdlcError, match="已经存在"):
            start_contract.preflight_document_first_start(
                paths,
                package,
                state=state,
            )
        after = sorted(path.relative_to(paths.root).as_posix() for path in paths.root.rglob("*"))
    finally:
        context.cleanup()

    assert after == before


def test_requirements_parent_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, paths, state, package = _fixture()
    _pass_reference(monkeypatch)
    real_parent = Path(context.name) / "外部正式目录"
    real_parent.mkdir()
    paths.requirements_dir.rmdir()
    paths.requirements_dir.symlink_to(real_parent, target_is_directory=True)
    before = sorted(
        path.relative_to(paths.root).as_posix()
        for path in paths.root.rglob("*")
    )
    try:
        with pytest.raises(SdlcError, match="目标目录不存在或不是普通目录"):
            start_contract.preflight_document_first_start(
                paths,
                package,
                state=state,
            )
        after = sorted(
            path.relative_to(paths.root).as_posix()
            for path in paths.root.rglob("*")
        )
    finally:
        context.cleanup()

    assert after == before


@pytest.mark.parametrize("later_drift", ["index", "manifest", "review_input"])
def test_old_revision_wins_over_later_drift(
    later_drift: str,
) -> None:
    context, paths, state, package = _fixture()
    package["source_revision_sha256"] = "0" * 64
    if later_drift == "index":
        index_path = paths.draft_artifact_index_file("DRAFT-001")
        document = json.loads(index_path.read_text(encoding="utf-8"))
        document["artifacts"][0]["sha256"] = "f" * 64
        index_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif later_drift == "manifest":
        package["artifact_manifest"].pop()
    else:
        review_item = next(
            item
            for item in package["artifact_manifest"]
            if item["artifact_type"] == "integrated_design_review_input"
        )
        review_path = paths.draft_dir("DRAFT-001") / review_item["source_path"]
        review_path.write_bytes(review_path.read_bytes() + b" ")
    before = {
        path.relative_to(paths.root).as_posix(): path.read_bytes()
        for path in paths.root.rglob("*")
        if path.is_file()
    }
    try:
        with pytest.raises(
            SdlcError,
            match="formal.v3 引用的 DRAFT 修订已经过期",
        ):
            start_contract.preflight_document_first_start(
                paths,
                package,
                state=state,
            )
        after = {
            path.relative_to(paths.root).as_posix(): path.read_bytes()
            for path in paths.root.rglob("*")
            if path.is_file()
        }
    finally:
        context.cleanup()

    assert after == before
