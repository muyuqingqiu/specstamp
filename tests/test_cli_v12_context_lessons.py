from __future__ import annotations

import json
from pathlib import Path

from test_cli_v1 import create_minimal_requirement_by_start_file, init_demo_repo, read_events, run_cli, write_events


def first_requirement_dir(project_dir: Path) -> Path:
    return next((project_dir / ".codex-sdlc" / "requirements").glob("REQ-001-*"))


def rewrite_first_task_as_search_file_task(project_dir: Path) -> None:
    events = read_events(project_dir)
    for event in events:
        if event.get("event_type") == "task_created" and event.get("task_id") == "T-001":
            event["payload"]["title"] = "在 `src/feature/search.ts` 中接入多语言搜索"
            event["payload"]["summary"] = "修改 `src/feature/search.ts`，让搜索入口支持当前语言下的关键词匹配。"
            event["payload"]["test_items"] = ["验证 `src/feature/search.ts` 支持多语言搜索关键词"]
            event["payload"]["manual_checks"] = ["人工确认搜索入口能按当前语言匹配关键词"]
            break
    write_events(project_dir, events)


def test_context_save_list_and_restore_preview_show_branch_worktree_snapshots(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="增加订单导出按钮")

    context_result = run_cli(["context"], cwd=project_dir, extra_env=env)
    assert context_result.returncode == 0, context_result.stderr
    assert "当前 SDLC 上下文" in context_result.stdout
    assert "身份状态：一致" in context_result.stdout

    save_result = run_cli(["context", "save", "--pin", "--label", "切分支前保存"], cwd=project_dir, extra_env=env)
    assert save_result.returncode == 0, save_result.stderr
    assert "已保存 SDLC 上下文" in save_result.stdout
    assert "保护状态：已固定" in save_result.stdout

    list_result = run_cli(["context", "list"], cwd=project_dir, extra_env=env)
    assert list_result.returncode == 0, list_result.stderr
    assert "可恢复的 SDLC 上下文" in list_result.stdout
    assert "项目快照：" in list_result.stdout
    assert "需求快照：" in list_result.stdout
    assert "pinned" in list_result.stdout

    preview_result = run_cli(["context", "restore", "REQ-001", "--dry-run"], cwd=project_dir, extra_env=env)
    assert preview_result.returncode == 0, preview_result.stderr
    assert "需求上下文恢复预览" in preview_result.stdout
    assert "只预览，不写入文件" in preview_result.stdout


def test_context_and_doctor_keep_legacy_task_pack_bytes_unchanged(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="既有执行档案只读兼容")
    pack_dir = first_requirement_dir(project_dir) / "task-packs" / "T-001"
    pack_dir.mkdir(parents=True)
    markdown = b"# T-001 legacy archive\n"
    metadata = (
        json.dumps(
            {
                "requirement_id": "REQ-001",
                "task_id": "T-001",
                "status": "ready",
                "context_files": [{"path": "src/feature.py"}],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    (pack_dir / "task-pack.md").write_bytes(markdown)
    (pack_dir / "task-pack.json").write_bytes(metadata)

    status_result = run_cli(["status"], cwd=project_dir, extra_env=env)
    doctor_result = run_cli(["doctor-repair"], cwd=project_dir, extra_env=env)
    save_result = run_cli(
        ["context", "save", "REQ-001", "--label", "既有档案只读检查"],
        cwd=project_dir,
        extra_env=env,
    )
    context_result = run_cli(["context"], cwd=project_dir, extra_env=env)

    assert status_result.returncode == 0, status_result.stderr
    assert "只供追溯，不参与当前任务状态和下一步推荐" in status_result.stdout
    assert "$sdlc-brief" not in status_result.stdout
    assert doctor_result.returncode == 0, doctor_result.stderr
    assert "既有任务执行包只读完整性正常" in doctor_result.stdout
    assert "已清理历史任务执行包" not in doctor_result.stdout
    assert save_result.returncode == 0, save_result.stderr
    assert context_result.returncode == 0, context_result.stderr
    assert (pack_dir / "task-pack.md").read_bytes() == markdown
    assert (pack_dir / "task-pack.json").read_bytes() == metadata


def test_context_save_preserves_damaged_legacy_task_pack(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    backup_home = tmp_path / "sdlc-backups"
    env = {"CODEX_SDLC_BACKUP_HOME": str(backup_home)}
    assert run_cli(["init"], cwd=project_dir, extra_env=env).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="损坏旧档案只读兼容")
    pack_dir = first_requirement_dir(project_dir) / "task-packs" / "T-001"
    pack_dir.mkdir(parents=True)
    (pack_dir / "task-pack.md").write_bytes(b"# damaged legacy archive\n")
    damaged = b"{broken legacy json\n"
    (pack_dir / "task-pack.json").write_bytes(damaged)

    save_result = run_cli(
        ["context", "save", "REQ-001", "--label", "损坏档案原样保存"],
        cwd=project_dir,
        extra_env=env,
    )
    doctor_result = run_cli(["doctor"], cwd=project_dir, extra_env=env)

    assert save_result.returncode == 0, save_result.stderr
    assert doctor_result.returncode == 0, doctor_result.stderr
    assert "json_invalid" not in doctor_result.stdout
    assert "task-pack.json 不是有效 UTF-8 JSON" in doctor_result.stdout
    assert (pack_dir / "task-pack.json").read_bytes() == damaged












def test_lessons_read_commands_skip_identity_guard_but_write_commands_check_identity(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="设置页经验扫描")
    identity_path = project_dir / ".codex-sdlc" / "identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["branch_key"] = "branch_wrong"
    identity_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    scan_result = run_cli(["lessons", "scan"], cwd=project_dir)
    assert scan_result.returncode == 0, scan_result.stderr
    assert "已生成经验扫描报告" in scan_result.stdout

    add_result = run_cli(["lessons", "add", "这个写入动作必须先通过身份检查。", "--level", "project"], cwd=project_dir)
    assert add_result.returncode != 0
    assert "身份和当前 Git 状态不一致" in add_result.stderr


def test_lessons_promote_demote_removes_old_level_copy(tmp_path: Path) -> None:
    project_dir = init_demo_repo(tmp_path)
    assert run_cli(["init"], cwd=project_dir).returncode == 0
    create_minimal_requirement_by_start_file(project_dir, title="经验分层")
    add_result = run_cli(
        [
            "lessons",
            "add",
            "设置页搜索数据要从配置里维护，不能反扫页面文字。",
            "--level",
            "project",
            "--scope",
            "设置页",
        ],
        cwd=project_dir,
    )
    assert add_result.returncode == 0, add_result.stderr
    project_lesson = project_dir / ".codex-sdlc" / "lessons" / "project" / "LES-001.json"
    cross_lesson = project_dir / ".codex-sdlc" / "lessons" / "cross-requirement" / "LES-001.json"
    assert project_lesson.exists()

    demote_result = run_cli(["lessons", "demote", "LES-001", "--to", "cross-requirement"], cwd=project_dir)
    assert demote_result.returncode == 0, demote_result.stderr
    assert not project_lesson.exists()
    assert cross_lesson.exists()

    list_result = run_cli(["lessons", "list"], cwd=project_dir)
    assert list_result.returncode == 0
    assert list_result.stdout.count("LES-001") == 1
