from __future__ import annotations

from pathlib import Path

from test_change_package_contract import _formal_project


def test_change_workspace_keeps_formal_base_stable_before_acceptance(tmp_path: Path) -> None:
    project, workspace, state = _formal_project(tmp_path)

    assert project.is_dir()
    assert workspace.is_dir()
    assert set(state["base_versions"]) == {
        "requirement",
        "design",
        "test_matrix",
        "reference_index",
        "task_plan",
    }
    assert all(
        len(value["sha256"]) == 64
        for value in state["base_versions"].values()
    )
