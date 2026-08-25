from __future__ import annotations

from pathlib import Path

from test_cli_v1 import init_demo_repo, run_cli


def write_extra_local_artifacts(project_dir: Path) -> None:
    old_state_dir = ".external" + "-history"
    old_notes_dir = "external" + "-notes"
    old_skill_name = "history" + "skill-demo"
    project_dir.joinpath(old_state_dir, "memory").mkdir(parents=True)
    project_dir.joinpath(old_state_dir, "memory", "constitution.md").write_text("本机旧资料\n", encoding="utf-8")
    project_dir.joinpath(old_notes_dir, "001-demo").mkdir(parents=True)
    project_dir.joinpath(old_notes_dir, "001-demo", "note.md").write_text("# demo\n", encoding="utf-8")
    project_dir.joinpath(".agents", "skills", old_skill_name).mkdir(parents=True)
    project_dir.joinpath(".agents", "skills", old_skill_name, "SKILL.md").write_text("旧项目资料 Skill\n", encoding="utf-8")
    project_dir.joinpath(".agents", "skills", "custom-tool").mkdir(parents=True)
    project_dir.joinpath(".agents", "skills", "custom-tool", "SKILL.md").write_text("用户自己的 Skill\n", encoding="utf-8")
    project_dir.joinpath(".codex", "notes.md").write_text("用户自己的 Codex 资料\n", encoding="utf-8")


def test_clean_previews_without_deleting_artifacts(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    write_extra_local_artifacts(project_dir)

    result = run_cli(["clean"], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert "清理预览" in result.stdout
    assert ".codex-sdlc" in result.stdout
    assert (".external" + "-history") not in result.stdout
    assert "$sdlc-clean-confirm" in result.stdout
    assert (project_dir / ".codex-sdlc").exists()
    assert (project_dir / (".external" + "-history")).exists()
    assert (project_dir / ("external" + "-notes") / "001-demo" / "note.md").exists()


def test_clean_confirm_removes_generated_artifacts_and_preserves_user_content(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    write_extra_local_artifacts(project_dir)

    result = run_cli(["clean-confirm"], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert "已清理" in result.stdout
    assert "Git 全局忽略配置已保留" in result.stdout
    assert not (project_dir / ".codex-sdlc").exists()
    assert (project_dir / (".external" + "-history")).exists()
    assert (project_dir / ("external" + "-notes")).exists()
    assert (project_dir / ".agents" / "skills" / ("history" + "skill-demo") / "SKILL.md").exists()
    assert (project_dir / ".agents" / "skills" / "custom-tool" / "SKILL.md").exists()
    assert (project_dir / ".codex" / "notes.md").exists()
    assert not (project_dir / ".codex" / "hooks.json").exists()
    assert not (project_dir / ".codex" / "hooks").exists()
    assert not (project_dir / ".codex" / "rules").exists()


def test_clean_confirm_preserves_modified_codex_config(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project_dir).returncode == 0
    hooks_json = project_dir / ".codex" / "hooks.json"
    hooks_json.write_text('{"hooks": {"Custom": []}}\n', encoding="utf-8")

    result = run_cli(["clean-confirm"], cwd=project_dir)

    assert result.returncode == 0, result.stderr
    assert "已保留" in result.stdout
    assert hooks_json.exists()
    assert hooks_json.read_text(encoding="utf-8") == '{"hooks": {"Custom": []}}\n'


def test_clean_preview_keeps_formal_original_until_explicit_whole_cleanup(
    tmp_path: Path,
) -> None:
    project_dir = init_demo_repo(tmp_path)
    initialized = run_cli(["init-basic"], cwd=project_dir)
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    original = (
        project_dir
        / ".codex-sdlc/requirements/REQ-001/original/formal.v3.json"
    )
    original.parent.mkdir(parents=True)
    original.write_text(
        '{"formal_contract_version":"formal.v3","workflow_profile":"document-first.v1"}\n',
        encoding="utf-8",
    )
    before = original.read_bytes()

    preview = run_cli(["clean"], cwd=project_dir)

    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert original.read_bytes() == before

    confirmed = run_cli(["clean-confirm"], cwd=project_dir)

    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    assert not (project_dir / ".codex-sdlc").exists()
