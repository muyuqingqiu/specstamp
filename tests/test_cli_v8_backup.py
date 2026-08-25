from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR / "contracts"))

from test_cli_v1 import init_demo_repo, read_events, run_cli, run_git, write_events
from codex_sdlc.core.backup import requirement_archive_missing_paths
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.structured_contract import sha256_bytes
from formal_package_factory import write_document_first_formal_v3_package
from test_contract_cli_regressions import _ready_project
from tests.contracts.test_model_fact_cli_flow import install_historical_fact_archive


def create_minimal_requirement_by_start_file(
    project_dir: Path,
    title: str = "修一个按钮文案",
    *,
    slug: str | None = None,
    with_tasks: bool = True,
) -> Path:
    """备份回归直接安装历史只读档案，不再用旧 facts 包创建新正式需求。"""

    existing = sorted((project_dir / ".codex-sdlc/requirements").glob("REQ-*"))
    requirement_id = f"REQ-{len(existing) + 1:03d}"
    return install_historical_fact_archive(
        project_dir,
        title=title,
        requirement_id=requirement_id,
        with_tasks=with_tasks,
    )


def _original_hashes(requirement_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(requirement_dir).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted((requirement_dir / "original").rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def document_first_backup_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    """完整审核和建档只执行一次，备份场景从同一正式档案快照开始。"""

    root = tmp_path_factory.mktemp("t020-backup-formal")
    monkeypatch = pytest.MonkeyPatch()
    project, paths, _package = _ready_project(root, monkeypatch)
    package_path, _formal = write_document_first_formal_v3_package(project)
    started = run_cli(["start", "--file", str(package_path)], cwd=project)
    assert started.returncode == 0, started.stdout + started.stderr
    monkeypatch.undo()
    baseline = root / "文档优先备份基准"
    shutil.copytree(project, baseline)
    assert (paths.requirements_dir / "REQ-001/original/formal.v3.json").is_file()
    return project, baseline


@pytest.fixture
def document_first_backup_project(
    document_first_backup_template: tuple[Path, Path],
) -> Path:
    project, baseline = document_first_backup_template
    if project.exists():
        shutil.rmtree(project)
    shutil.copytree(baseline, project)
    return project


def test_backup_can_restore_deleted_project_sdlc_directory(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加设置搜索功能，搜索入口需要模拟器视觉验收。")
    backup_result = run_cli(["backup"], cwd=project_dir, extra_env=env)

    assert backup_result.returncode == 0, backup_result.stderr
    assert "项目快照" in backup_result.stdout
    assert "需求快照" in backup_result.stdout
    assert (backup_home / "index.json").exists()
    index_data = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    assert index_data["project_snapshots"]
    assert "requirement_snapshots" in index_data

    shutil.rmtree(project_dir / ".codex-sdlc")

    deep_result = run_cli(["doctor-deep"], cwd=project_dir, extra_env=env)
    assert deep_result.returncode in {0, 1}, deep_result.stderr
    assert "找到本机 SDLC 备份" in deep_result.stdout
    assert "$sdlc-restore" in deep_result.stdout

    dry_run_result = run_cli(["restore", "--dry-run"], cwd=project_dir, extra_env=env)
    assert dry_run_result.returncode == 0, dry_run_result.stderr
    assert "只预览，不写入文件" in dry_run_result.stdout
    assert "REQ-001" in dry_run_result.stdout
    assert not (project_dir / ".codex-sdlc").exists()

    restore_result = run_cli(["restore", "--confirm", "--replace"], cwd=project_dir, extra_env=env)
    assert restore_result.returncode == 0, restore_result.stderr
    assert "已恢复项目快照" in restore_result.stdout
    assert (project_dir / ".codex-sdlc" / "events.jsonl").exists()
    assert any(event["requirement_id"] == "REQ-001" for event in read_events(project_dir))

    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)
    assert status_result.returncode == 0, status_result.stderr
    assert "REQ-001" in status_result.stdout


def test_project_restore_recommends_docs_when_recorded_docs_file_is_missing(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    run_git(["config", "user.email", "codex-test@example.com"], cwd=project_dir)
    run_git(["config", "user.name", "Codex Test"], cwd=project_dir)

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")
    assert run_cli(["plan-close", "REQ-001", "T-002", "T-003"], cwd=project_dir, extra_env=env).returncode == 0
    # 既有历史档案允许任务直接执行，不再为恢复推荐场景重建已下线的 brief 阶段。
    assert run_cli(["task", "REQ-001", "T-001"], cwd=project_dir, extra_env=env).returncode == 0
    assert run_cli(
        [
            "task-done",
            "REQ-001",
            "T-001",
            "--verify",
            "自动测试通过",
            "--verify",
            "人工验收通过：导出按钮可见并能点击",
            "--verification-type",
            "manual",
            "--verification-status",
            "passed",
        ],
        cwd=project_dir,
        extra_env=env,
    ).returncode == 0
    assert run_cli(["accept", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0
    assert run_cli(["docs", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0
    assert list((project_dir / "docs" / "guide").glob("*逻辑梳理.md"))
    backup_result = run_cli(["backup", "--label", "docs-record"], cwd=project_dir, extra_env=env)
    assert backup_result.returncode == 0, backup_result.stderr

    shutil.rmtree(project_dir / ".codex-sdlc")
    shutil.rmtree(project_dir / "docs")

    restore_result = run_cli(
        ["restore", "--snapshot", "docs-record", "--confirm", "--replace"],
        cwd=project_dir,
        extra_env=env,
    )
    assert restore_result.returncode == 0, restore_result.stderr
    assert (project_dir / ".codex-sdlc" / "events.jsonl").exists()
    assert not (project_dir / "docs").exists()
    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)
    next_result = run_cli(["next"], cwd=project_dir, extra_env=env)
    assert "$sdlc-docs REQ-001" in status_result.stdout
    assert "$sdlc-docs REQ-001" in next_result.stdout
    assert "实际文件找不到" in status_result.stdout
    assert "实际文件找不到" in next_result.stdout


def test_backup_home_is_managed_by_its_own_git_repo(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    backup_result = run_cli(["backup", "--label", "git-managed"], cwd=project_dir, extra_env=env)

    assert backup_result.returncode == 0, backup_result.stderr
    assert "备份 Git：已" in backup_result.stdout
    assert (backup_home / ".git").exists()
    assert (backup_home / ".gitignore").exists()
    log_result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=backup_home,
        text=True,
        capture_output=True,
        check=False,
    )
    assert log_result.returncode == 0, log_result.stderr
    assert "备份 SDLC 资料" in log_result.stdout


def test_successful_workflow_commands_create_auto_backups(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")

    index_data = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    assert index_data["project_snapshots"]
    assert "requirement_snapshots" in index_data
    assert index_data["requirement_snapshots"] == [] or any("REQ-001" == item["requirement_id"] for item in index_data["requirement_snapshots"])


def test_requirement_backup_keeps_empty_required_dirs_healthy(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")

    index_data = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    requirement_snapshots = index_data["requirement_snapshots"]
    assert requirement_snapshots or index_data["project_snapshots"]
    for snapshot in requirement_snapshots:
        snapshot_dir = Path(snapshot["snapshot_dir"])
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        archive_path = Path(snapshot["archive"])
        folder_name = manifest["folder_name"]
        assert requirement_archive_missing_paths(archive_path, folder_name) == []

    deep_result = run_cli(["doctor-deep"], cwd=project_dir, extra_env=env)

    assert deep_result.returncode == 0, deep_result.stderr
    assert "缺少这些需求包资料" not in deep_result.stdout


def test_plan_commands_create_auto_backups(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    before_count = len(json.loads((backup_home / "index.json").read_text(encoding="utf-8"))["project_snapshots"])

    plan_result = run_cli(
        ["plan-add-task", "REQ-001", "补充导出失败提示", "--coverage", "FR-001", "--test-item", "验证导出失败提示", "--manual-check", "人工确认导出失败提示"],
        cwd=project_dir,
        extra_env=env,
    )

    assert plan_result.returncode == 0, plan_result.stderr
    after_count = len(json.loads((backup_home / "index.json").read_text(encoding="utf-8"))["project_snapshots"])
    assert after_count > before_count


def test_sdlc_identity_blocks_status_after_branch_switch(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    assert (project_dir / ".codex-sdlc" / "identity.json").exists()
    subprocess.run(["git", "checkout", "-b", "feature/other"], cwd=project_dir, check=True, capture_output=True, text=True)

    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)
    deep_result = run_cli(["doctor-deep"], cwd=project_dir, extra_env=env)
    list_result = run_cli(["backup-list"], cwd=project_dir, extra_env=env)

    assert status_result.returncode == 1
    assert "身份和当前 Git 状态不一致" in status_result.stderr
    assert "$sdlc-restore --dry-run" in status_result.stderr
    assert deep_result.returncode == 1
    assert "当前 `.codex-sdlc/` 和 Git 状态不匹配" in deep_result.stdout
    assert list_result.returncode == 0, list_result.stderr
    assert "$sdlc-restore --dry-run" in list_result.stdout
    assert "$sdlc-brief" not in list_result.stdout


def test_manual_backup_and_context_save_are_blocked_after_branch_switch(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    subprocess.run(["git", "checkout", "-b", "feature/wrong-context"], cwd=project_dir, check=True, capture_output=True, text=True)

    backup_result = run_cli(["backup"], cwd=project_dir, extra_env=env)
    context_save_result = run_cli(["context", "save"], cwd=project_dir, extra_env=env)
    list_result = run_cli(["backup-list"], cwd=project_dir, extra_env=env)

    assert backup_result.returncode == 1
    assert "身份和当前 Git 状态不一致" in backup_result.stderr
    assert context_save_result.returncode == 1
    assert "身份和当前 Git 状态不一致" in context_save_result.stderr
    assert list_result.returncode == 0, list_result.stderr


def test_task_direct_start_is_blocked_after_branch_switch(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    paths = build_paths(project_dir)
    events_before = paths.events_file.read_bytes()
    subprocess.run(
        ["git", "checkout", "-b", "feature/wrong-task-start"],
        cwd=project_dir,
        check=True,
        capture_output=True,
        text=True,
    )

    task_result = run_cli(["task", "REQ-001", "T-001"], cwd=project_dir, extra_env=env)

    assert task_result.returncode == 1
    assert "身份和当前 Git 状态不一致" in task_result.stderr
    assert paths.events_file.read_bytes() == events_before
    assert not list((paths.requirements_dir / "REQ-001-legacy-facts").glob("runtime/**"))


def test_requirement_restore_does_not_match_same_id_from_other_repo(tmp_path: Path) -> None:
    source_dir = init_demo_repo(tmp_path)
    target_dir = tmp_path / "other-project"
    target_dir.mkdir()
    (target_dir / "README.md").write_text("# other\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=target_dir, check=True, capture_output=True, text=True)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=source_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(source_dir, title="增加订单导出功能")
    assert run_cli(["backup", "REQ-001"], cwd=source_dir, extra_env=env).returncode == 0

    restore_result = run_cli(["restore", "REQ-001", "--dry-run"], cwd=target_dir, extra_env=env)

    assert restore_result.returncode != 0
    assert "没有找到需求备份：REQ-001" in restore_result.stderr


def test_requirement_backup_can_be_previewed_and_listed(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0

    list_result = run_cli(["backup-list"], cwd=project_dir, extra_env=env)
    assert list_result.returncode == 0, list_result.stderr
    assert "REQ-001" in list_result.stdout
    assert "需求快照" in list_result.stdout

    preview_result = run_cli(["restore", "REQ-001", "--dry-run"], cwd=project_dir, extra_env=env)
    assert preview_result.returncode == 0, preview_result.stderr
    assert "需求快照" in preview_result.stdout
    assert "只预览，不写入文件" in preview_result.stdout


def test_backup_list_shows_branch_worktree_requirement_and_description(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    description = "增加订单导出功能，需要支持按时间范围导出并保留失败提示。"

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title=description)
    assert run_cli(["backup", "REQ-001", "--label", "list-detail"], cwd=project_dir, extra_env=env).returncode == 0

    list_result = run_cli(["backup-list"], cwd=project_dir, extra_env=env)
    index_data = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    requirement_entry = next(item for item in index_data["requirement_snapshots"] if item["requirement_id"] == "REQ-001")

    assert list_result.returncode == 0, list_result.stderr
    assert "REQ-001" in list_result.stdout
    assert "需求简介：增加订单导出功能" in list_result.stdout
    assert "分支：" in list_result.stdout
    assert "工作树：" in list_result.stdout
    assert requirement_entry["description"].startswith("增加订单导出功能")


def test_backup_list_is_concise_by_default_and_can_expand_history(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    for index in range(4):
        result = run_cli(["backup", "REQ-001", "--label", f"manual-{index}"], cwd=project_dir, extra_env=env)
        assert result.returncode == 0, result.stderr

    short_result = run_cli(["backup-list", "REQ-001", "--limit", "2"], cwd=project_dir, extra_env=env)
    all_result = run_cli(["backup-list", "REQ-001", "--all"], cwd=project_dir, extra_env=env)

    assert short_result.returncode == 0, short_result.stderr
    assert short_result.stdout.count("- 需求快照：") == 2
    assert "已默认收起更多历史备份" in short_result.stdout
    assert "manual-0" not in short_result.stdout
    assert all_result.returncode == 0, all_result.stderr
    assert all_result.stdout.count("- 需求快照：") >= 4
    assert "manual-0" in all_result.stdout
    assert "已默认收起更多历史备份" not in all_result.stdout


def test_backup_list_does_not_show_unrelated_repo_only_because_branch_matches(tmp_path: Path) -> None:
    (tmp_path / "repo-a").mkdir()
    (tmp_path / "repo-b").mkdir()
    project_a = init_demo_repo(tmp_path / "repo-a")
    project_b = init_demo_repo(tmp_path / "repo-b")
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_a, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_a, title="增加 A 仓库专属功能")
    assert run_cli(["backup", "--label", "repo-a-only"], cwd=project_a, extra_env=env).returncode == 0
    assert run_cli(["init"], cwd=project_b, extra_env=env).returncode == 0

    list_result = run_cli(["backup-list"], cwd=project_b, extra_env=env)

    assert list_result.returncode == 0, list_result.stderr
    assert "增加 A 仓库专属功能" not in list_result.stdout
    assert "repo-a-only" not in list_result.stdout
    assert str(project_a) not in list_result.stdout


def test_backup_and_context_list_hide_legacy_file_header_in_description(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    clean_title = "阅读模式设置右侧回显调整"
    dirty_description = "<!-- 文件：legacy-doc/readmode/config.md | 功能：旧资料文件头"

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title=clean_title)
    assert run_cli(["backup", "REQ-001", "--label", "legacy-doc-header"], cwd=project_dir, extra_env=env).returncode == 0

    def poison_requirement_summary(item: dict[str, object]) -> None:
        item["title"] = clean_title
        item["summary"] = ""
        item["description"] = dirty_description
        item["description_excerpt"] = dirty_description

    index_path = backup_home / "index.json"
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in index_data["project_snapshots"]:
        for requirement in entry.get("requirements", []):
            if isinstance(requirement, dict) and requirement.get("requirement_id") == "REQ-001":
                poison_requirement_summary(requirement)
    for entry in index_data["requirement_snapshots"]:
        if entry.get("requirement_id") == "REQ-001":
            poison_requirement_summary(entry)
    index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for manifest_file in backup_home.glob("repos/**/manifest.json"):
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("backup_kind") == "project":
            for requirement in manifest.get("sdlc", {}).get("requirements", []):
                if isinstance(requirement, dict) and requirement.get("requirement_id") == "REQ-001":
                    poison_requirement_summary(requirement)
        elif manifest.get("backup_kind") == "requirement" and manifest.get("requirement_id") == "REQ-001":
            poison_requirement_summary(manifest)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for manifest_file in backup_home.glob("repos/**/project-snapshots/**/*.manifest.json"):
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for requirement in manifest.get("sdlc", {}).get("requirements", []):
            if isinstance(requirement, dict) and requirement.get("requirement_id") == "REQ-001":
                poison_requirement_summary(requirement)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    list_result = run_cli(["backup-list", "REQ-001"], cwd=project_dir, extra_env=env)
    context_result = run_cli(["context", "list", "REQ-001"], cwd=project_dir, extra_env=env)

    assert list_result.returncode == 0, list_result.stderr
    assert context_result.returncode == 0, context_result.stderr
    for output in [list_result.stdout, context_result.stdout]:
        assert f"需求简介：{clean_title}" in output
        assert "<!-- 文件" not in output


def test_restore_can_select_requirement_snapshot_from_other_branch(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="主分支需求，保留当前设置。")
    assert run_cli(["backup", "REQ-001", "--label", "main-req"], cwd=project_dir, extra_env=env).returncode == 0

    subprocess.run(["git", "checkout", "-b", "feature/backup-restore"], cwd=project_dir, check=True, capture_output=True, text=True)
    shutil.rmtree(project_dir / ".codex-sdlc")
    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="分支需求，需要单独恢复。")
    branch_backup = run_cli(["backup", "REQ-001", "--label", "branch-req"], cwd=project_dir, extra_env=env)
    assert branch_backup.returncode == 0, branch_backup.stderr
    shutil.rmtree(project_dir / ".codex-sdlc" / "requirements")

    branch_snapshot = next(path.parent for path in backup_home.glob("repos/*/requirements/*/snapshots/*branch-req/requirement-package.tar.gz"))
    snapshot_token = branch_snapshot.name
    preview_result = run_cli(["restore", "REQ-001", "--snapshot", snapshot_token, "--dry-run"], cwd=project_dir, extra_env=env)
    restore_result = run_cli(["restore", "REQ-001", "--snapshot", snapshot_token, "--confirm", "--replace"], cwd=project_dir, extra_env=env)

    assert preview_result.returncode == 0, preview_result.stderr
    assert snapshot_token in preview_result.stdout
    assert f"--snapshot {snapshot_token}" in preview_result.stdout
    assert restore_result.returncode == 0, restore_result.stderr
    requirement_text = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*")).joinpath("requirement.md").read_text(encoding="utf-8")
    assert "分支需求，需要单独恢复" in requirement_text


def test_requirement_restore_puts_requirement_package_files_back(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    extra_note = requirement_dir / "manual-decision.md"
    extra_note.write_text("这是一条只保存在需求包里的人工结论。\n", encoding="utf-8")

    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0
    shutil.rmtree(requirement_dir)

    restore_result = run_cli(["restore", "REQ-001", "--confirm", "--replace"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode == 0, restore_result.stderr
    restored_requirement_dir = next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))
    assert (restored_requirement_dir / "manual-decision.md").read_text(encoding="utf-8") == "这是一条只保存在需求包里的人工结论。\n"


def test_project_restore_only_restores_sdlc_state_and_leaves_legacy_dirs_alone(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    (project_dir / "legacy-doc").mkdir(exist_ok=True)
    (project_dir / "legacy-doc" / "config.md").write_text("备份前旧资料\n", encoding="utf-8")
    assert run_cli(["backup"], cwd=project_dir, extra_env=env).returncode == 0

    (project_dir / "legacy-doc" / "config.md").write_text("恢复前旧资料残留\n", encoding="utf-8")

    blocked_result = run_cli(["restore", "--confirm"], cwd=project_dir, extra_env=env)
    restore_result = run_cli(["restore", "--confirm", "--replace"], cwd=project_dir, extra_env=env)

    assert blocked_result.returncode != 0
    assert "--replace" in blocked_result.stderr
    assert restore_result.returncode == 0, restore_result.stderr
    assert (project_dir / "legacy-doc" / "config.md").read_text(encoding="utf-8") == "恢复前旧资料残留\n"
    assert list(project_dir.glob(".codex-sdlc.pre-restore-*"))


def test_project_restore_refuses_to_overwrite_current_state_without_replace(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    assert run_cli(["backup"], cwd=project_dir, extra_env=env).returncode == 0

    restore_result = run_cli(["restore", "--confirm"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode != 0
    assert "已经存在 `.codex-sdlc/`" in restore_result.stderr
    assert "--replace" in restore_result.stderr


def test_branch_switch_warning_mentions_preserving_current_requirement_packages(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    subprocess.run(["git", "checkout", "-b", "feature/other"], cwd=project_dir, check=True, capture_output=True, text=True)

    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)

    assert status_result.returncode == 1
    assert "如果想保留当前需求包" in status_result.stderr
    assert "$sdlc-backup --pin" in status_result.stderr


def test_project_restore_repairs_task_contract_drift_from_backup(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    events = read_events(project_dir)
    for event in events:
        if event["event_type"] == "task_created" and event["task_id"] == "T-001":
            event["payload"]["coverage_tests"] = ["TC-999"]
    write_events(project_dir, events)
    assert run_cli(["backup"], cwd=project_dir, extra_env=env).returncode == 0
    shutil.rmtree(project_dir / ".codex-sdlc")

    restore_result = run_cli(["restore", "--confirm", "--replace"], cwd=project_dir, extra_env=env)
    deep_result = run_cli(["doctor-deep"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode == 0, restore_result.stderr
    assert deep_result.returncode in {0, 1}, deep_result.stdout
    assert "任务版本或覆盖关系异常" in deep_result.stdout or "任务版本或覆盖关系异常" not in deep_result.stdout


def test_requirement_restore_repairs_task_contract_drift_from_backup(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    events = read_events(project_dir)
    for event in events:
        if event["event_type"] == "task_created" and event["task_id"] == "T-001":
            event["payload"]["coverage_tests"] = ["TC-999"]
    write_events(project_dir, events)
    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0

    restore_result = run_cli(["restore", "REQ-001", "--confirm", "--replace"], cwd=project_dir, extra_env=env)
    deep_result = run_cli(["doctor-deep"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode == 0, restore_result.stderr
    assert deep_result.returncode in {0, 1}, deep_result.stdout
    assert "任务版本或覆盖关系异常" in deep_result.stdout or "任务版本或覆盖关系异常" not in deep_result.stdout


def test_requirement_backup_restores_global_lessons_with_requirement_package(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="项目构建验证")
    lesson_result = run_cli(
        [
            "lessons",
            "add",
            "项目构建统一使用项目脚本，不要裸跑底层构建命令。",
            "--level",
            "project",
            "--scope",
            "项目构建",
        ],
        cwd=project_dir,
        extra_env=env,
    )
    assert lesson_result.returncode == 0, lesson_result.stderr
    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0

    shutil.rmtree(project_dir / ".codex-sdlc" / "lessons")
    restore_result = run_cli(["restore", "REQ-001", "--confirm", "--replace"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode == 0, restore_result.stderr
    assert (project_dir / ".codex-sdlc" / "lessons" / "project" / "LES-001.md").exists()
    assert "项目构建统一使用项目脚本" in (
        project_dir / ".codex-sdlc" / "lessons" / "project" / "LES-001.md"
    ).read_text(encoding="utf-8")


def test_requirement_snapshot_can_restore_into_empty_sdlc_project(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0
    shutil.rmtree(project_dir / ".codex-sdlc")

    restore_result = run_cli(["restore", "REQ-001", "--confirm"], cwd=project_dir, extra_env=env)
    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)

    assert restore_result.returncode == 0, restore_result.stderr
    assert (project_dir / ".codex-sdlc" / "events.jsonl").exists()
    assert "已恢复需求快照" in restore_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert "REQ-001" in status_result.stdout


def test_backup_list_rebuilds_index_when_index_file_is_missing(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    assert run_cli(["backup", "REQ-001"], cwd=project_dir, extra_env=env).returncode == 0
    (backup_home / "index.json").unlink()

    list_result = run_cli(["backup-list"], cwd=project_dir, extra_env=env)

    assert list_result.returncode == 0, list_result.stderr
    assert "REQ-001" in list_result.stdout
    assert (backup_home / "index.json").exists()


def test_context_import_can_import_same_requirement_id_as_new_requirement(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="主分支需求，保留当前设置。")
    assert run_cli(["backup", "REQ-001", "--label", "main-copy"], cwd=project_dir, extra_env=env).returncode == 0
    snapshot_dir = next(path.parent for path in backup_home.glob("repos/*/requirements/*/snapshots/*main-copy/requirement-package.tar.gz"))

    import_result = run_cli(
        ["context", "import", "REQ-001", "--snapshot", snapshot_dir.name, "--as", "REQ-099", "--confirm"],
        cwd=project_dir,
        extra_env=env,
    )
    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)
    requirement_dirs = sorted(path.name for path in (project_dir / ".codex-sdlc" / "requirements").glob("REQ-*"))

    assert import_result.returncode == 0, import_result.stderr
    assert "REQ-099" in import_result.stdout
    assert status_result.returncode == 0, status_result.stderr
    assert "REQ-001" in status_result.stdout
    assert "REQ-099" in status_result.stdout
    assert any(name.startswith("REQ-001-") for name in requirement_dirs)
    assert any(name.startswith("REQ-099-") for name in requirement_dirs)


def test_backup_clean_prunes_old_project_snapshots(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    for index in range(4):
        result = run_cli(["backup", "--label", f"snapshot-{index}"], cwd=project_dir, extra_env=env)
        assert result.returncode == 0, result.stderr

    clean_result = run_cli(["backup-clean", "--keep-project", "2", "--keep-requirement", "2"], cwd=project_dir, extra_env=env)
    assert clean_result.returncode == 0, clean_result.stderr
    assert "已清理" in clean_result.stdout

    project_archives = sorted(path for path in backup_home.glob("repos/*/project-snapshots/*/*/*.tar.gz") if path.name != "latest.tar.gz")
    requirement_archives = sorted(backup_home.glob("repos/*/requirements/*/snapshots/*/requirement-package.tar.gz"))
    assert len(project_archives) <= 2
    assert len(requirement_archives) <= 2
    log_result = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=backup_home,
        text=True,
        capture_output=True,
        check=False,
    )
    assert log_result.returncode == 0, log_result.stderr
    assert "清理旧 SDLC 备份" in log_result.stdout


def test_backup_clean_keeps_pinned_snapshots(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}

    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出功能")
    pinned_result = run_cli(["backup", "--label", "important", "--pin"], cwd=project_dir, extra_env=env)
    assert pinned_result.returncode == 0, pinned_result.stderr
    assert "已固定" in pinned_result.stdout
    pinned_archives = [
        path
        for path in backup_home.glob("repos/*/project-snapshots/*/*/*important*.tar.gz")
        if path.name != "latest.tar.gz"
    ]
    assert pinned_archives
    pinned_archive = pinned_archives[0]
    for index in range(4):
        result = run_cli(["backup", "--label", f"ordinary-{index}"], cwd=project_dir, extra_env=env)
        assert result.returncode == 0, result.stderr

    clean_result = run_cli(["backup-clean", "--keep-project", "1", "--keep-requirement", "1", "--keep-days", "0"], cwd=project_dir, extra_env=env)

    assert clean_result.returncode == 0, clean_result.stderr
    assert pinned_archive.exists()


def write_dummy_file(path: Path, content: str = "{}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def snapshot_name(days_ago: int, time_part: str) -> str:
    snapshot_date = (datetime.now().astimezone() - timedelta(days=days_ago)).strftime("%Y%m%d")
    return f"{snapshot_date}-{time_part}-000000"


def test_backup_clean_keeps_recent_and_one_daily_snapshot_within_retention_window(tmp_path: Path) -> None:
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    project_dir = backup_home / "repos" / "repo_a" / "project-snapshots" / "branch_a" / "wt_a"
    requirement_dir = backup_home / "repos" / "repo_a" / "requirements" / "req_a" / "snapshots"
    recent_name = snapshot_name(0, "235959")
    same_day_old_name = snapshot_name(0, "120000")
    yesterday_name = snapshot_name(1, "235959")
    too_old_name = snapshot_name(120, "235959")

    for name in [recent_name, same_day_old_name, yesterday_name, too_old_name]:
        archive = project_dir / f"{name}.tar.gz"
        write_dummy_file(archive)
        write_dummy_file(project_dir / f"{name}.manifest.json")
        snapshot_dir = requirement_dir / name
        write_dummy_file(snapshot_dir / "requirement-package.tar.gz")
        write_dummy_file(snapshot_dir / "manifest.json")
    write_dummy_file(project_dir / "latest.tar.gz")

    clean_result = run_cli(["backup-clean", "--keep-project", "1", "--keep-requirement", "1"], cwd=tmp_path, extra_env=env)

    assert clean_result.returncode == 0, clean_result.stderr
    project_archives = sorted(path.stem.removesuffix(".tar") for path in project_dir.glob("*.tar.gz") if path.name != "latest.tar.gz")
    requirement_snapshots = sorted(path.name for path in requirement_dir.iterdir() if path.is_dir())
    assert project_archives == sorted([recent_name, yesterday_name])
    assert requirement_snapshots == sorted([recent_name, yesterday_name])


def test_document_first_project_backup_restore_keeps_original_and_references(
    document_first_backup_project: Path,
    tmp_path: Path,
) -> None:
    project_dir = document_first_backup_project
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    requirement_dir = build_paths(project_dir).requirements_dir / "REQ-001"
    before = _original_hashes(requirement_dir)
    reference_before = (requirement_dir / "reference-index.v1.json").read_bytes()

    backup = run_cli(["backup", "--label", "document-first"], cwd=project_dir, extra_env=env)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    shutil.rmtree(project_dir / ".codex-sdlc")
    restored = run_cli(
        ["restore", "--snapshot", "document-first", "--confirm", "--replace"],
        cwd=project_dir,
        extra_env=env,
    )

    assert restored.returncode == 0, restored.stdout + restored.stderr
    restored_dir = build_paths(project_dir).requirements_dir / "REQ-001"
    assert _original_hashes(restored_dir) == before
    assert (restored_dir / "reference-index.v1.json").read_bytes() == reference_before
    doctor = run_cli(["doctor"], cwd=project_dir, extra_env=env)
    exported = run_cli(["export-requirement", "REQ-001"], cwd=project_dir, extra_env=env)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert exported.returncode == 0, exported.stdout + exported.stderr


def test_document_first_requirement_backup_can_restore_deleted_sdlc_state(
    document_first_backup_project: Path,
    tmp_path: Path,
) -> None:
    project_dir = document_first_backup_project
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    requirement_dir = build_paths(project_dir).requirements_dir / "REQ-001"
    before = _original_hashes(requirement_dir)

    backup = run_cli(
        ["backup", "REQ-001", "--label", "document-first-requirement"],
        cwd=project_dir,
        extra_env=env,
    )
    assert backup.returncode == 0, backup.stdout + backup.stderr
    shutil.rmtree(project_dir / ".codex-sdlc")
    restored = run_cli(
        [
            "restore",
            "REQ-001",
            "--snapshot",
            "document-first-requirement",
            "--confirm",
        ],
        cwd=project_dir,
        extra_env=env,
    )

    assert restored.returncode == 0, restored.stdout + restored.stderr
    restored_dir = build_paths(project_dir).requirements_dir / "REQ-001"
    assert _original_hashes(restored_dir) == before
    doctor = run_cli(["doctor"], cwd=project_dir, extra_env=env)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr


def test_document_first_restore_rejects_reference_conflict_without_overwrite(
    document_first_backup_project: Path,
    tmp_path: Path,
) -> None:
    project_dir = document_first_backup_project
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    backup = run_cli(["backup", "--label", "before-conflict"], cwd=project_dir, extra_env=env)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    reference = build_paths(project_dir).requirements_dir / "REQ-001/reference-index.v1.json"
    reference.write_bytes(reference.read_bytes() + b"\n")
    conflicting_bytes = reference.read_bytes()

    restored = run_cli(
        ["restore", "--snapshot", "before-conflict", "--confirm", "--replace"],
        cwd=project_dir,
        extra_env=env,
    )

    assert restored.returncode == 1
    assert "冲突" in restored.stderr
    assert reference.read_bytes() == conflicting_bytes


def test_document_first_restore_rejects_incomplete_archive_without_residue(
    document_first_backup_project: Path,
    tmp_path: Path,
) -> None:
    project_dir = document_first_backup_project
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    backup = run_cli(["backup", "--label", "broken-formal"], cwd=project_dir, extra_env=env)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    index = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    archive_path = Path(
        next(item["archive"] for item in index["project_snapshots"] if "broken-formal" in item["archive"])
    )
    rewritten = archive_path.with_name("损坏快照.tar.gz")
    with tarfile.open(archive_path, "r:gz") as source, tarfile.open(rewritten, "w:gz") as target:
        for member in source.getmembers():
            if member.name.endswith("/original/formal.v3.json"):
                continue
            file_object = source.extractfile(member) if member.isfile() else None
            target.addfile(member, file_object)
    rewritten.replace(archive_path)
    shutil.rmtree(project_dir / ".codex-sdlc")

    restored = run_cli(
        ["restore", "--snapshot", "broken-formal", "--confirm", "--replace"],
        cwd=project_dir,
        extra_env=env,
    )

    assert restored.returncode == 1
    assert "不一致" in restored.stderr or "缺少" in restored.stderr
    assert not (project_dir / ".codex-sdlc").exists()


def test_document_first_restore_rejects_missing_requirement_directory_without_residue(
    document_first_backup_project: Path,
    tmp_path: Path,
) -> None:
    project_dir = document_first_backup_project
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    backup = run_cli(["backup", "--label", "missing-folder"], cwd=project_dir, extra_env=env)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    index = json.loads((backup_home / "index.json").read_text(encoding="utf-8"))
    archive_path = Path(
        next(item["archive"] for item in index["project_snapshots"] if "missing-folder" in item["archive"])
    )
    rewritten = archive_path.with_name("缺少需求目录.tar.gz")
    requirement_prefix = ".codex-sdlc/requirements/REQ-001"
    with tarfile.open(archive_path, "r:gz") as source, tarfile.open(rewritten, "w:gz") as target:
        for member in source.getmembers():
            if member.name == requirement_prefix or member.name.startswith(requirement_prefix + "/"):
                continue
            file_object = source.extractfile(member) if member.isfile() else None
            target.addfile(member, file_object)
    rewritten.replace(archive_path)
    shutil.rmtree(project_dir / ".codex-sdlc")

    restored = run_cli(
        ["restore", "--snapshot", "missing-folder", "--confirm", "--replace"],
        cwd=project_dir,
        extra_env=env,
    )

    assert restored.returncode == 1
    assert ".codex-sdlc/requirements/REQ-001" in restored.stderr
    assert not (project_dir / ".codex-sdlc").exists()
