from __future__ import annotations

from pathlib import Path

import pytest

from codex_sdlc.core.state import derive_state
from codex_sdlc.services import start_service
from test_contract_cli_regressions import _args, _ready_project, _write_package


def test_document_first_full_flow_archives_original_and_creates_current_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, package = _ready_project(tmp_path, monkeypatch)
    package_file = project / "文档优先正式包.json"
    _write_package(package_file, package)
    monkeypatch.chdir(project)

    assert start_service.start(_args(package_file)) == 0

    requirement_root = paths.requirements_dir / "REQ-001"
    assert (requirement_root / "original/formal.v3.json").is_file()
    assert (requirement_root / "effective/requirement.current.json").is_file()
    assert (requirement_root / "effective/design.current.json").is_file()
    assert (requirement_root / "effective/test-matrix.current.json").is_file()
    assert derive_state(paths)["requirements"]["REQ-001"]["status"] == "planning"
