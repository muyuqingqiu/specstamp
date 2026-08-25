from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.formal_manifest_contract import (
    DOCUMENT_FIRST_PROFILE,
    build_document_first_formal_package,
    validate_formal_package_contract,
)
from test_formal_manifest_completeness import _ready_workspace


def test_complete_document_first_package_passes_without_embedded_body() -> None:
    with TemporaryDirectory(prefix="t015-formal-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        package = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )

        validated = validate_formal_package_contract(paths, package, state=state)

        assert validated["mode"] == "document-first"
        assert validated["package"]["workflow_profile"] == DOCUMENT_FIRST_PROFILE
        assert validated["package"]["reviews"] == {
            "requirement_split": "REV-001",
            "integrated_design": "REV-002",
        }
        assert "fact_bundle" not in validated["package"]
        for forbidden in (
            "title",
            "description",
            "functional_requirements",
            "design",
            "design_summary",
        ):
            assert forbidden not in validated["package"]


def test_legacy_formal_v3_stays_read_only_and_does_not_enter_new_contract() -> None:
    with TemporaryDirectory(prefix="t015-legacy-") as directory:
        paths, state, _index = _ready_workspace(Path(directory))
        legacy = {
            "formal_contract_version": "formal.v3",
            "fact_bundle": {"source_index_file": "source-index.json"},
            "title": "历史正式需求",
        }

        validated = validate_formal_package_contract(paths, legacy, state=state)

        assert validated == {
            "mode": "legacy_read_only",
            "package": legacy,
        }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda package: package.update({"fact_bundle": {}}),
        lambda package: package.update({"title": "不能嵌入需求正文"}),
        lambda package: package["reviews"].update({"requirement_split": "REV-999"}),
        lambda package: package["reviews"].update({"integrated_design": "REV-999"}),
        lambda package: package.update({"open_questions": ["仍有待确认项"]}),
        lambda package: package["artifact_index"].update({"sha256": "0" * 64}),
    ],
)
def test_document_first_cannot_bypass_manifest_or_current_reviews(mutate) -> None:
    with TemporaryDirectory(prefix="t015-no-bypass-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        package = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )
        mutate(package)
        with pytest.raises(SdlcError):
            validate_formal_package_contract(paths, package, state=state)


def test_stale_confirmation_or_review_is_rejected() -> None:
    with TemporaryDirectory(prefix="t015-stale-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        package = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )

        stale_confirmation = deepcopy(state)
        stale_confirmation["drafts"]["DRAFT-001"]["_requirement_confirmation_state"][
            "status"
        ] = "stale"
        with pytest.raises(SdlcError, match="需求确认"):
            validate_formal_package_contract(
                paths,
                package,
                state=stale_confirmation,
            )

        stale_review = deepcopy(state)
        review = stale_review["drafts"]["DRAFT-001"][
            "_integrated_design_review_state"
        ]["reviews"][0]
        review["effective_status"] = "stale"
        review["can_advance"] = False
        with pytest.raises(SdlcError, match="整体设计审核"):
            validate_formal_package_contract(
                paths,
                package,
                state=stale_review,
            )
