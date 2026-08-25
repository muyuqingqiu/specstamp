from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_cli_v1 import run_cli
from codex_sdlc.core import agent_sync
from codex_sdlc.core.agent_sync import RETIRED_SDLC_SKILL_NAMES, default_paths, discover_skills


def write_skill(skill_dir: Path, name: str, description: str = "测试技能") -> Path:
    target = skill_dir / name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n命令：`codex-sdlc {name.removeprefix('sdlc-')}`\n",
        encoding="utf-8",
    )
    return target


def agent_sync_env(tmp_path: Path) -> dict[str, str]:
    return {
        "CODEX_SDLC_AGENT_HOME": str(tmp_path / "agents" / "sdlc"),
        "CODEX_SDLC_CODEX_SKILLS_HOME": str(tmp_path / "codex" / "skills"),
        "CODEX_SDLC_AGENTS_SKILLS_HOME": str(tmp_path / "agents" / "skills"),
        "CODEX_SDLC_CLAUDE_HOME": str(tmp_path / "claude"),
        "CODEX_SDLC_SOURCE_SKILLS_HOME": str(tmp_path / "source" / "skills"),
        "CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME": str(tmp_path / "source" / "shared-skills"),
    }


def complete_install_env(tmp_path: Path) -> dict[str, str]:
    """把正式来源同步到隔离目录，安装体检测试不能碰用户正在使用的三套入口。"""

    env = agent_sync_env(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    # 严格体检必须使用当前代码对应的版本化来源。测试只把运行副本放进临时目录，
    # 不能再用临时来源冒充完整安装。
    env["CODEX_SDLC_SOURCE_SKILLS_HOME"] = str(repo_root / "skills")
    env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"] = str(repo_root / "shared-skills")
    sync_result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)
    assert sync_result.returncode == 0, sync_result.stderr

    env["CODEX_SDLC_HOME"] = str(repo_root)
    env["CODEX_SKILLS_HOME"] = env["CODEX_SDLC_CODEX_SKILLS_HOME"]
    env["PATH"] = os.pathsep.join([str(repo_root / "bin"), os.environ.get("PATH", "")])
    return env


def isolated_formal_install(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """复制一套正式安装到仓库外，用它安全构造版本化来源损坏状态。"""

    repo_root = Path(__file__).resolve().parents[1]
    install_root = tmp_path / "isolated-install"
    shutil.copytree(repo_root / "src", install_root / "src")
    shutil.copytree(repo_root / "skills", install_root / "skills")
    shutil.copytree(repo_root / "shared-skills", install_root / "shared-skills")
    shutil.copytree(repo_root / "scripts", install_root / "scripts")
    (install_root / "bin").mkdir(parents=True)
    shutil.copy2(repo_root / "bin" / "codex-sdlc", install_root / "bin" / "codex-sdlc")

    runtime_root = tmp_path / "isolated-runtime"
    env = agent_sync_env(runtime_root)
    env.pop("CODEX_SDLC_SOURCE_SKILLS_HOME")
    env.pop("CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME")
    env["CODEX_SDLC_HOME"] = str(install_root)
    env["CODEX_SDLC_PYTHON"] = sys.executable
    env["CODEX_SKILLS_HOME"] = env["CODEX_SDLC_CODEX_SKILLS_HOME"]
    env["PATH"] = os.pathsep.join([str(install_root / "bin"), os.environ.get("PATH", "")])
    return install_root, env


def run_isolated_cli(
    install_root: Path,
    env: dict[str, str],
    *args: str,
) -> subprocess.CompletedProcess[str]:
    active_env = os.environ.copy()
    # 发布门禁本身会绑定当前仓库来源；复制安装的损坏探针必须清掉这两个外层值，
    # 才能检查复制代码自己的版本化目录，而不是提前停在来源身份不匹配。
    active_env.pop("CODEX_SDLC_SOURCE_SKILLS_HOME", None)
    active_env.pop("CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME", None)
    active_env.update(env)
    return subprocess.run(
        [str(install_root / "bin" / "codex-sdlc"), *args],
        cwd=install_root,
        env=active_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def assert_output_path(output: str, label: str, expected: Path) -> None:
    """macOS临时目录可能显示为等价的/private路径，按真实解析结果核对具体文件。"""

    matching_line = next(line for line in output.splitlines() if label in line)
    reported = matching_line.split(label, 1)[1].split("（", 1)[0].strip()
    assert Path(reported).resolve() == expected.resolve()


def test_agent_sync_dry_run_only_reports_plan(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source_skills, "sdlc-init")

    result = run_cli(["agent-sync", "--dry-run"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert "只预览，不写入文件" in result.stdout
    assert "版本化技能来源" in result.stdout
    assert "仓库内技能目录" in result.stdout
    assert "当前没有使用仓库内版本化技能" in result.stdout
    assert "共享技能来源" in result.stdout
    assert "当前没有使用仓库内版本化共享技能" in result.stdout
    assert "运行时标准目录" in result.stdout
    assert not Path(env["CODEX_SDLC_AGENT_HOME"]).exists()
    assert not (Path(env["CODEX_SDLC_CLAUDE_HOME"]) / "commands").exists()


def test_doctor_install_passes_for_complete_isolated_install(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLI 帮助：通过" in result.stdout
    assert "CLI 版本：通过" in result.stdout
    assert "运行依赖：通过" in result.stdout
    assert "Skill：内容哈希一致 sdlc-init" in result.stdout
    assert "Agent 入口：通过" in result.stdout


def test_doctor_install_rejects_non_versioned_skill_sources(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    shared_source = Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-init", "非版本化入口")
    write_skill(shared_source, "agent-capability-sync", "非版本化共享入口")
    assert run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env).returncode == 0
    repo_root = Path(__file__).resolve().parents[1]
    env["CODEX_SDLC_HOME"] = str(repo_root)
    env["CODEX_SKILLS_HOME"] = env["CODEX_SDLC_CODEX_SKILLS_HOME"]
    env["PATH"] = os.pathsep.join([str(repo_root / "bin"), os.environ.get("PATH", "")])

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert f"实际来源：{source}" in output
    assert f"版本化来源：{repo_root / 'skills'}" in output
    assert f"实际共享来源：{shared_source}" in output
    assert f"版本化共享来源：{repo_root / 'shared-skills'}" in output
    assert "unset CODEX_SDLC_SOURCE_SKILLS_HOME CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME" in output
    assert "codex-sdlc agent-sync --check" in output
    assert "安装结论：不可用" in output


def test_agent_sync_check_rejects_non_versioned_skill_sources(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    shared_source = Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-init", "非版本化入口")
    write_skill(shared_source, "agent-capability-sync", "非版本化共享入口")
    assert run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env).returncode == 0

    result = run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert f"实际来源：{source}" in output
    assert f"版本化来源：{Path(__file__).resolve().parents[1] / 'skills'}" in output
    assert f"实际共享来源：{shared_source}" in output
    assert "codex-sdlc agent-sync --dry-run" in output
    assert "codex-sdlc agent-sync --confirm" in output
    assert "codex-sdlc agent-sync --check" in output


def test_doctor_install_reports_missing_versioned_source_without_traceback(
    tmp_path: Path,
) -> None:
    install_root, env = isolated_formal_install(tmp_path)
    assert run_isolated_cli(install_root, env, "agent-sync", "--confirm").returncode == 0
    shutil.rmtree(install_root / "skills")

    result = run_isolated_cli(install_root, env, "doctor-install")
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert_output_path(output, "找不到技能来源目录：", install_root / "skills")
    assert "codex-sdlc agent-sync --dry-run" in output
    assert "python3" in output
    assert "scripts/install_specstamp.py" in output
    assert "Traceback" not in output


def test_doctor_install_reports_invalid_utf8_source_without_traceback(
    tmp_path: Path,
) -> None:
    install_root, env = isolated_formal_install(tmp_path)
    assert run_isolated_cli(install_root, env, "agent-sync", "--confirm").returncode == 0
    broken_file = install_root / "skills" / "sdlc-init" / "SKILL.md"
    broken_file.write_bytes(b"\xff\xfe\xfa")

    result = run_isolated_cli(install_root, env, "doctor-install")
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert_output_path(output, "技能文件不是有效的 UTF-8：", broken_file)
    assert "codex-sdlc agent-sync --dry-run" in output
    assert "codex-sdlc agent-sync --check" in output
    assert "UnicodeDecodeError" not in output
    assert "Traceback" not in output


def test_doctor_install_reports_unreadable_skill_file_without_traceback(
    tmp_path: Path,
) -> None:
    install_root, env = isolated_formal_install(tmp_path)
    assert run_isolated_cli(install_root, env, "agent-sync", "--confirm").returncode == 0
    unreadable_file = install_root / "skills" / "sdlc-init" / "SKILL.md"
    unreadable_file.chmod(0)
    try:
        result = run_isolated_cli(install_root, env, "doctor-install")
    finally:
        unreadable_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert_output_path(output, "无法读取技能文件：", unreadable_file)
    assert "codex-sdlc agent-sync --dry-run" in output
    assert "codex-sdlc agent-sync --check" in output
    assert "PermissionError" not in output
    assert "Traceback" not in output


def test_doctor_install_reports_source_hash_read_failure_without_traceback(
    tmp_path: Path,
) -> None:
    install_root, env = isolated_formal_install(tmp_path)
    assert run_isolated_cli(install_root, env, "agent-sync", "--confirm").returncode == 0
    unreadable_file = install_root / "skills" / "sdlc-init" / "不可读内容.bin"
    unreadable_file.write_bytes("不可读".encode("utf-8"))
    unreadable_file.chmod(0)
    try:
        result = run_isolated_cli(install_root, env, "doctor-install")
    finally:
        unreadable_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    output = result.stdout + result.stderr

    assert result.returncode == 1
    assert_output_path(output, "无法读取技能内容哈希：", unreadable_file)
    assert "codex-sdlc agent-sync --dry-run" in output
    assert "codex-sdlc agent-sync --check" in output
    assert "PermissionError" not in output
    assert "Traceback" not in output


def test_doctor_install_rejects_cli_that_cannot_start(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    broken_home = tmp_path / "broken-install"
    broken_cli = broken_home / "bin" / "codex-sdlc"
    broken_cli.parent.mkdir(parents=True)
    broken_cli.write_text("#!/bin/sh\nprintf '%s\\n' '测试入口无法启动' >&2\nexit 7\n", encoding="utf-8")
    broken_cli.chmod(broken_cli.stat().st_mode | stat.S_IXUSR)
    env["CODEX_SDLC_HOME"] = str(broken_home)
    env["PATH"] = os.pathsep.join([str(broken_cli.parent), os.environ.get("PATH", "")])

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "CLI 帮助：失败" in result.stdout
    assert "退出码 7" in result.stdout
    assert "python3" in result.stdout
    assert "scripts/install_specstamp.py" in result.stdout


def test_doctor_install_rejects_runtime_skill_drift(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    skill = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]) / "sdlc-init" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "漂移内容\n", encoding="utf-8")

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "Codex 技能内容不同" in result.stdout
    assert str(skill.parent) in result.stdout
    assert "codex-sdlc agent-sync --confirm" in result.stdout


def test_doctor_install_rejects_extra_runtime_entry(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    extra = write_skill(
        Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills",
        "sdlc-retired",
        "多余入口",
    )

    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "多余入口" in result.stdout
    assert str(extra) in result.stdout


def test_doctor_install_does_not_accept_dry_run_as_completed_sync(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    repo_root = Path(__file__).resolve().parents[1]
    env["CODEX_SDLC_SOURCE_SKILLS_HOME"] = str(repo_root / "skills")
    env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"] = str(repo_root / "shared-skills")
    env["CODEX_SDLC_HOME"] = str(repo_root)
    env["CODEX_SKILLS_HOME"] = env["CODEX_SDLC_CODEX_SKILLS_HOME"]
    env["PATH"] = os.pathsep.join([str(repo_root / "bin"), os.environ.get("PATH", "")])

    preview = run_cli(["agent-sync", "--dry-run"], cwd=tmp_path, extra_env=env)
    result = run_cli(["doctor-install"], cwd=tmp_path, extra_env=env)

    assert preview.returncode == 0
    assert result.returncode == 1
    assert "Agent 同步清单缺失" in result.stdout
    assert "codex-sdlc agent-sync --confirm" in result.stdout


def test_doctor_install_formal_entry_rejects_missing_runtime_dependency(
    tmp_path: Path,
) -> None:
    """缺依赖时正式入口会在业务命令前停止，doctor-install 也不能误报成功。"""

    repo_root = Path(__file__).resolve().parents[1]
    isolated_home = tmp_path / "missing-dependency"
    launcher = isolated_home / "bin" / "codex-sdlc"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(repo_root / "bin" / "codex-sdlc", launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    python = isolated_home / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        f"#!/bin/sh\nexec {sys.executable} -S \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    env.pop("CODEX_SDLC_PYTHON", None)
    env["PYTHONPATH"] = str(repo_root / "src")

    result = subprocess.run(
        [str(launcher), "doctor-install"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 1
    assert "受管解释器缺少 SpecStamp 运行依赖" in result.stderr
    assert "请运行安装命令" in result.stderr
    assert "Traceback" not in result.stderr


def test_agent_sync_check_is_read_only_and_reports_runtime_drift(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    codex_skills = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"])
    skill_file = codex_skills / "sdlc-init" / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8") + "\n已经漂移的入口\n",
        encoding="utf-8",
    )
    before = (codex_skills / "sdlc-init" / "SKILL.md").read_text(encoding="utf-8")

    result = run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "Codex 技能内容不同" in result.stdout + result.stderr
    assert (codex_skills / "sdlc-init" / "SKILL.md").read_text(encoding="utf-8") == before


def test_agent_sync_check_passes_after_confirm_and_detects_claude_drift(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    assert run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env).returncode == 0

    command = Path(env["CODEX_SDLC_CLAUDE_HOME"]) / "commands" / "sdlc-init.md"
    command.write_text(command.read_text(encoding="utf-8") + "漂移\n", encoding="utf-8")
    result = run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "Claude 命令内容不同" in result.stdout + result.stderr


def test_agent_sync_check_reports_each_deterministic_manifest_field_drift(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    manifest_path = Path(env["CODEX_SDLC_AGENT_HOME"]) / "manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutations = [
        ("source_skills_home", lambda data: data.__setitem__("source_skills_home", "/错误来源")),
        ("skills.names", lambda data: data["skills"].__setitem__("names", ["sdlc-wrong"])),
        ("skills.sdlc", lambda data: data["skills"].__setitem__("sdlc", 99)),
        (
            "capabilities.managed_shared_skills",
            lambda data: data["capabilities"].__setitem__("managed_shared_skills", ["错误受管技能"]),
        ),
        ("adapters.codex.skills", lambda data: data["adapters"]["codex"].__setitem__("skills", 99)),
        ("adapters.claude.commands", lambda data: data["adapters"]["claude"].__setitem__("commands", 99)),
    ]
    for expected_field, mutate in mutations:
        changed = json.loads(json.dumps(original, ensure_ascii=False))
        mutate(changed)
        manifest_path.write_text(json.dumps(changed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env)
        assert result.returncode == 1
        assert expected_field in result.stdout + result.stderr
    manifest_path.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env).returncode == 0


def test_agent_sync_check_ignores_personal_skill_changes_after_confirm(tmp_path: Path) -> None:
    env = complete_install_env(tmp_path)
    codex_skills = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"])
    agent_skills = Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"])

    personal = write_skill(codex_skills, "later-personal", "确认后新增的个人技能")
    shared = write_skill(agent_skills, "later-shared", "确认后新增的共享个人技能")
    assert run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env).returncode == 0
    (personal / "SKILL.md").write_text("个人技能已修改\n", encoding="utf-8")
    (shared / "SKILL.md").unlink()
    assert run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env).returncode == 0

    managed = codex_skills / "sdlc-init" / "SKILL.md"
    managed.write_text(
        managed.read_text(encoding="utf-8") + "\n受管入口已漂移\n",
        encoding="utf-8",
    )
    result = run_cli(["agent-sync", "--check"], cwd=tmp_path, extra_env=env)
    assert result.returncode == 1
    assert "内容不同" in result.stdout + result.stderr


def test_agent_sync_default_source_uses_versioned_repo_skills(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_SDLC_SOURCE_SKILLS_HOME", raising=False)

    paths = default_paths()
    repo_root = Path(__file__).resolve().parents[1]

    assert paths.source_skills_home == repo_root / "skills"
    assert (paths.source_skills_home / "sdlc-change-plan" / "SKILL.md").exists()
    discovered_names = {entry.name for entry in discover_skills(paths.source_skills_home)}
    assert discovered_names.isdisjoint(RETIRED_SDLC_SKILL_NAMES)


def test_agent_sync_default_dry_run_reports_versioned_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_SDLC_SOURCE_SKILLS_HOME", raising=False)

    result = run_cli(["agent-sync", "--dry-run"], cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    repo_root = Path(__file__).resolve().parents[1]
    assert f"版本化技能来源：{repo_root / 'skills'}" in result.stdout
    assert f"仓库内技能目录：{repo_root / 'skills'}" in result.stdout
    assert "来源状态：正在使用仓库内版本化技能" in result.stdout
    assert f"共享技能来源：{repo_root / 'shared-skills'}" in result.stdout
    assert "共享来源状态：正在使用仓库内版本化共享技能" in result.stdout


def test_agent_sync_confirm_builds_shared_source_and_removes_duplicate_agent_skills(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    shared_source_skills = Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"])
    codex_skills = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"])
    agents_skills = Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"])
    agent_home = Path(env["CODEX_SDLC_AGENT_HOME"])
    claude_home = Path(env["CODEX_SDLC_CLAUDE_HOME"])

    write_skill(source_skills, "sdlc-init", "初始化当前项目")
    write_skill(source_skills, "sdlc-task", "推进当前任务")
    write_skill(shared_source_skills, "agent-capability-sync", "维护 Agent 能力同步规则")
    write_skill(codex_skills, "sdlc-init", "初始化当前项目")
    write_skill(agents_skills, "sdlc-init", "重复入口")
    write_skill(agents_skills, "code-review", "非 SDLC 技能")

    result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert "已完成 Agent 入口同步" in result.stdout
    assert (agent_home / "skills" / "sdlc-init" / "SKILL.md").exists()
    assert (agent_home / "skills" / "sdlc-task" / "SKILL.md").exists()
    assert (codex_skills / "sdlc-init" / "SKILL.md").exists()
    assert not (agents_skills / "sdlc-init").exists()
    assert (agents_skills / "code-review" / "SKILL.md").exists()
    assert (agents_skills / "agent-capability-sync" / "SKILL.md").exists()
    assert (claude_home / "commands" / "sdlc-init.md").exists()
    command_text = (claude_home / "commands" / "sdlc-task.md").read_text(encoding="utf-8")
    assert "如果下方 Skill 明确写了 Goal 模式或连续推进规则" in command_text
    assert "Claude Code 的 `/sdlc-*` 和 Codex 的 `$sdlc-*` 是同一套语义" in command_text
    claude_rules = (claude_home / "CLAUDE.md").read_text(encoding="utf-8")
    expected_flow = (
        "/sdlc-init` -> `/sdlc-discuss` -> `/sdlc-design` -> `/sdlc-design-accept` "
        "-> `/sdlc-start` -> `/sdlc-tasks` -> `/sdlc-task`"
    )
    assert expected_flow in claude_rules
    assert "/sdlc-discuss` -> `/sdlc-start` -> `/sdlc-design" not in claude_rules
    assert "不要引导用户接入、恢复、维护或继续使用其它流程目录" in claude_rules
    assert "自然语言理解全部由 agent 完成" in claude_rules
    assert "CLI 只保存、校验、执行门禁和同步" in claude_rules
    assert "CLI 拒绝结构化字段缺失或关系不唯一的输入" in claude_rules
    assert "`/sdlc-add` 默认先复述理解、判断类型、列出预计任务和验收回归要求" in claude_rules
    assert "用户确认后再实际执行 `codex-sdlc add`" in claude_rules
    assert "--change CHG-xxx --task" in claude_rules
    assert "需求和设计审核统一使用 `review` 合同" in claude_rules
    assert "`task-read-confirm`、`task-run-check` 和 task-run 证据" in claude_rules
    assert "`change-package` 提交完整预计结果" in claude_rules
    for retired_marker in ("sdlc-prepare", "sdlc-brief", "brief-augment", "brief-review", "task-pack"):
        assert retired_marker not in claude_rules
    assert "Goal 模式必须评估是否使用子代理" in claude_rules
    assert "不能因为当前工具列表没预加载子代理就直接判不可用" in claude_rules
    assert "先搜索 multi-agent/subagent 或 agent-task-dispatcher" in claude_rules
    assert "只有纯状态读取、只执行状态命令、只提交 Git 或只写 SDLC 状态时" in claude_rules
    assert "用户说“sdlc 当前需求测试发现”" in claude_rules
    assert "/sdlc-task-restore" in claude_rules
    assert "/sdlc-fix" in claude_rules
    assert "不要直接改代码" in claude_rules
    assert "Goal 模式启动后默认允许创建 Codex 桌面工作线程" in claude_rules
    assert "普通模式不自动创建 Codex 线程" in claude_rules
    assert "只提示用户可以要求开新线程执行下一步" in claude_rules
    assert "主线程直接跑、单次工作线程或任务周期线程" in claude_rules
    assert "一个正式任务默认对应一个任务周期线程" in claude_rules
    assert "同一任务的失败修复或验收反馈修复优先继续原任务周期线程" in claude_rules
    assert "下一个任务必须新开任务周期线程" in claude_rules
    assert "主线程默认是调度员" in claude_rules
    assert "代码实现、长阅读、长排查、测试、验收和文档质量复核默认派工作线程" in claude_rules
    assert "任务外问题排查、长日志排查、只读审查、测试和验收" in claude_rules
    assert "第一版只允许一个工作线程运行" in claude_rules
    assert "list_projects" in claude_rules
    assert "create_thread" in claude_rules
    assert 'target.environment.type = "local"' in claude_rules
    assert "通用 SDLC 线程提示词和合适的 `model`" in claude_rules
    assert "新开线程也必须考虑质量和成本平衡" in claude_rules
    assert "不要默认所有新线程都用高能力模型" in claude_rules
    assert "主线程必须记录计划模型、是否传入 model 字段" in claude_rules
    assert "工作线程必须在承接确认里说明自己看到或实际运行的模型" in claude_rules
    assert "无法确认时写无法确认" in claude_rules
    assert "必须按任务量和复杂度设置固定检查间隔" in claude_rules
    assert "轻量状态或很短只读动作 2 分钟" in claude_rules
    assert "正式任务开发、常规修复、包含构建或测试、多文件实现、UI/模拟器验收、复杂返修、长链路排查 10 分钟" in claude_rules
    assert "15 分钟" not in claude_rules
    assert "正式任务线程没有依据时不允许 60 到 180 秒频繁轮询" in claude_rules
    assert "`thinking` 按规则传" in claude_rules
    assert "任务需要复杂方案判断、多模块根因分析、大范围重构评估" in claude_rules
    assert "传 `thinking` 时必须在执行位置决策里写清原因" in claude_rules
    assert "send_message_to_thread" in claude_rules
    assert "read_thread" in claude_rules
    assert "线程结束前运行 `codex-sdlc status`" in claude_rules
    assert "主线程读取线程结果后再次运行 `codex-sdlc status`" in claude_rules
    assert "任务状态以 SDLC 状态为准" in claude_rules
    assert "工作线程遇到任务外问题时必须及时汇报主线程" in claude_rules
    assert "默认开单次排查线程定位原因" in claude_rules
    assert "不允许直接 task-done" in claude_rules
    assert "主线程准备向用户汇报目标完成前，必须亲自做最终质量检查" in claude_rules
    assert "不能汇报完成" in claude_rules
    assert ".codex-sdlc/events.jsonl" in claude_rules
    assert "Goal 模式每个工作单元开始前都要分开写短决策" in claude_rules
    assert "执行位置决策" in claude_rules
    assert "子代理决策" in claude_rules
    assert "创建或继续工作线程时只传子代理策略和授权" in claude_rules
    assert "不把“使用 / 不使用”写死" in claude_rules
    assert "工作线程收到的是主线程执行位置决策和子代理策略" in claude_rules
    assert "不重新做执行位置决策" in claude_rules
    assert "工作线程开工前必须先输出工作线程承接确认" in claude_rules
    assert "本线程实际模型或无法确认" in claude_rules
    assert "主线程可以在策略里要求工作线程符合条件时必须使用子代理" in claude_rules
    assert "工作线程不能把主线程未预派解释成“用户明确要求不使用子代理”" in claude_rules
    assert "新开线程写 2 / 5 / 10 分钟并说明原因" in claude_rules
    assert "主线程默认只做调度、状态确认和复核" in claude_rules
    assert "需要实现、长阅读、排查、测试、验收或文档质量复核时，优先派工作线程" in claude_rules
    assert "单次耗时工作默认单次工作线程" in claude_rules
    assert "正式任务默认任务周期线程" in claude_rules
    assert "不使用子代理也要写明确原因" in claude_rules
    assert "高能力模型用于重思考、大范围判断和高风险实现" in claude_rules
    assert "经济模型用于文本查找、证据整理、简单修改和局部验证" in claude_rules
    assert "主线程最终汇报必须写复核动作和复核结论" in claude_rules
    assert "当前任务保护覆盖 `doing`、`ready_for_user_check` 和 `test_failed`" in claude_rules
    assert ".codex-sdlc/requirements/<REQ>/tests/*.mjs" in claude_rules
    assert "/sdlc-*` 和 `$sdlc-*` 是同一套语义" in claude_rules
    assert "当前任务代码改动导致执行包过期" not in claude_rules

    manifest = json.loads((agent_home / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "codex-sdlc.agent-sync.v1"
    assert manifest["source_is_versioned"] is False
    assert manifest["shared_source_is_versioned"] is False
    assert manifest["skills"]["total"] == 2
    assert manifest["skills"]["sdlc"] == 2
    assert manifest["adapters"]["codex"]["skills"] == 2
    assert manifest["adapters"]["claude"]["commands"] == 2
    assert manifest["capabilities"]["managed_shared_skills"] == ["agent-capability-sync"]
    assert manifest["capabilities"]["shared_skills"] == ["agent-capability-sync", "code-review"]
    assert manifest["capabilities"]["shared_skill_count"] == 2
    assert manifest["capabilities"]["duplicate_names"] == []

    backups = list((agent_home / "backups").glob("agent-sync-*"))
    assert backups
    assert any(
        (
            agent_sync._backup_path_for_target(
                Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"]) / "sdlc-init",
                backup,
            )
            / "SKILL.md"
        ).exists()
        for backup in backups
    )


def test_agent_sync_confirm_replaces_managed_shared_runtime_copy(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    shared_source_skills = Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"])
    agents_skills = Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"])
    agent_home = Path(env["CODEX_SDLC_AGENT_HOME"])

    write_skill(source_skills, "sdlc-init")
    write_skill(shared_source_skills, "agent-capability-sync", "版本化共享技能")
    runtime_copy = write_skill(agents_skills, "agent-capability-sync", "运行时旧副本")
    (runtime_copy / "marker.txt").write_text("运行时旧内容\n", encoding="utf-8")

    result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stderr
    runtime_text = (agents_skills / "agent-capability-sync" / "SKILL.md").read_text(encoding="utf-8")
    assert "版本化共享技能" in runtime_text
    assert "运行时旧副本" not in runtime_text
    assert not (agents_skills / "agent-capability-sync" / "marker.txt").exists()
    backups = list((agent_home / "backups").glob("agent-sync-*"))
    assert any(
        (
            agent_sync._backup_path_for_target(
                agents_skills / "agent-capability-sync",
                backup,
            )
            / "marker.txt"
        ).exists()
        for backup in backups
    )


def test_agent_sync_confirm_removes_stale_standard_entries(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    standard_skills = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills"

    write_skill(source_skills, "sdlc-init")
    write_skill(standard_skills, "removed-standard-entry", "历史标准入口")

    result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert not (standard_skills / "removed-standard-entry").exists()
    backups = list((Path(env["CODEX_SDLC_AGENT_HOME"]) / "backups").glob("agent-sync-*"))
    assert any(
        (
            agent_sync._backup_path_for_target(
                standard_skills / "removed-standard-entry",
                backup,
            )
            / "SKILL.md"
        ).exists()
        for backup in backups
    )


def test_agent_sync_requires_confirm_for_writes(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    write_skill(Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"]), "sdlc-init")

    result = run_cli(["agent-sync"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 1
    assert "默认只允许预览" in result.stderr


def test_agent_sync_confirm_is_idempotent_when_nothing_changed(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    agents_skills = Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"])
    agent_home = Path(env["CODEX_SDLC_AGENT_HOME"])
    write_skill(source_skills, "sdlc-init")
    write_skill(agents_skills, "sdlc-init", "重复入口")

    first_result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)
    backups_after_first = sorted((agent_home / "backups").glob("agent-sync-*"))
    time.sleep(1.1)
    second_result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)
    backups_after_second = sorted((agent_home / "backups").glob("agent-sync-*"))

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert backups_after_first
    assert backups_after_second == backups_after_first


def test_agent_sync_reports_non_sdlc_duplicate_skills_without_removing_them(tmp_path: Path) -> None:
    env = agent_sync_env(tmp_path)
    source_skills = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    codex_skills = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"])
    agents_skills = Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"])
    agent_home = Path(env["CODEX_SDLC_AGENT_HOME"])

    write_skill(source_skills, "sdlc-init")
    write_skill(codex_skills, "code-review", "Codex 侧同名技能")
    write_skill(codex_skills, "old-skill.backup-20260616-145727", "备份目录")
    write_skill(agents_skills, "code-review", "共享侧同名技能")
    write_skill(agents_skills, "agent-capability-sync", "维护 Agent 能力同步规则")

    result = run_cli(["agent-sync", "--confirm"], cwd=tmp_path, extra_env=env)

    assert result.returncode == 0, result.stderr
    assert "共享开发技能数：2" in result.stdout
    assert "受管共享技能数：0" in result.stdout
    assert "非 SDLC 重名技能：1" in result.stdout
    assert (codex_skills / "code-review" / "SKILL.md").exists()
    assert (agents_skills / "code-review" / "SKILL.md").exists()
    manifest = json.loads((agent_home / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capabilities"]["shared_skills"] == ["agent-capability-sync", "code-review"]
    assert manifest["capabilities"]["codex_local_skills"] == ["code-review"]
    assert manifest["capabilities"]["duplicate_names"] == ["code-review"]


def test_agent_capability_skill_overrides_skill_creator_default_path() -> None:
    skill_text = (Path(__file__).resolve().parents[1] / "shared-skills" / "agent-capability-sync" / "SKILL.md").read_text(encoding="utf-8")

    assert "优先级高于 `skill-creator` 的默认路径" in skill_text
    assert "版本化来源在 `<SpecStamp 仓库>/skills`" in skill_text
    assert "共享技能版本化来源在 `<SpecStamp 仓库>/shared-skills`" in skill_text
    assert "不要直接修改 `$HOME/.codex/skills/sdlc-*`" in skill_text
    assert "来源状态" in skill_text
    assert "版本化来源或共享入口在哪里" in skill_text
    assert "标准源或共享入口在哪里" not in skill_text
    assert "普通通用开发技能默认放在 `$HOME/.agents/skills`" in skill_text
    assert "不要默认放到 `$HOME/.codex/skills`" in skill_text


def test_sdlc_semantic_rules_are_visible_in_key_skills() -> None:
    skill_root = Path(__file__).resolve().parents[1] / "skills"
    shared_skill_root = Path(__file__).resolve().parents[1] / "shared-skills"

    light_start = (skill_root / "sdlc-light-start" / "SKILL.md").read_text(encoding="utf-8")
    tasks = (skill_root / "sdlc-tasks" / "SKILL.md").read_text(encoding="utf-8")
    design_accept = (skill_root / "sdlc-design-accept" / "SKILL.md").read_text(encoding="utf-8")
    lessons = (skill_root / "sdlc-lessons" / "SKILL.md").read_text(encoding="utf-8")
    change_plan = (skill_root / "sdlc-change-plan" / "SKILL.md").read_text(encoding="utf-8")
    change = (skill_root / "sdlc-change" / "SKILL.md").read_text(encoding="utf-8")
    agent_sync = (skill_root / "sdlc-agent-sync" / "SKILL.md").read_text(encoding="utf-8")
    doctor_install = (skill_root / "sdlc-doctor-install" / "SKILL.md").read_text(encoding="utf-8")
    agent_capability = (shared_skill_root / "agent-capability-sync" / "SKILL.md").read_text(encoding="utf-8")

    assert "<SpecStamp 仓库>/skills/<技能名>/SKILL.md" in agent_sync
    assert "<SpecStamp 仓库>/shared-skills/<技能名>/SKILL.md" in agent_sync
    assert "当前没有使用仓库内版本化技能" in agent_sync
    assert "不要手写 `~/.codex/skills` 运行时副本" in doctor_install
    assert "<SpecStamp 仓库>/shared-skills" in agent_capability
    assert "DRAFT、`start --file`、`review`、task-run、`change-package`" in agent_capability
    assert "已下线，不再支持一句话直接生成正式需求" in light_start
    assert "$sdlc-discuss" in light_start
    assert "$sdlc-design" in light_start
    assert "$sdlc-start" in light_start
    assert "当前 `effective/requirement.current.json`" in tasks
    assert "真实代码、已完成前置任务交付物和其它任务规划代码证据" in tasks
    assert "下一步由 `$sdlc-task REQ-001 T-001` 直接开工" in tasks
    assert "技术方案需要调整时，先把新原文作为 `technical-solution` MAT 归档" in design_accept
    assert "不能覆盖已确认 DES" in design_accept
    assert "经验是否值得记录、应该是什么级别，全部由 Codex" in lessons
    assert "`change-package.v1.json`" in change
    assert "`projected-task-plan.v2`" in change
    assert "CLI 只校验结构、编号、引用、哈希、显式影响和状态转换" in change
    assert "不从自然语言正文猜受影响对象" in change
    assert "任务开工直接使用 `$sdlc-task REQ-xxx T-xxx`" in change_plan
    assert "同步后的 Codex `$sdlc-*` 和 Claude Code `/sdlc-*` 必须是同一套语义" in agent_sync
    assert "sdlc-prepare" in agent_sync
    assert "sdlc-brief" in agent_sync
    assert "`review`、task-run、`change-package`" in agent_sync


def exact_path_state(path: Path) -> dict[str, object] | None:
    """精确记录目录项，回滚测试不能只看 exists。"""

    if not path.exists() and not path.is_symlink():
        return None
    info = os.lstat(path)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"type": "symlink", "mode": mode, "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        return {"type": "file", "mode": mode, "content": path.read_bytes()}
    if stat.S_ISDIR(info.st_mode):
        return {
            "type": "dir",
            "mode": mode,
            "children": {
                child.name: exact_path_state(child)
                for child in sorted(path.iterdir(), key=lambda item: item.name)
            },
        }
    return {"type": "other", "mode": mode}


def configure_direct_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def entry_state_with_mtime(path: Path) -> dict[str, object] | None:
    """备份阶段失败时连目录项时间也不能被回滚代码改写。"""

    state = exact_path_state(path)
    if state is None:
        return None
    state["mtime_ns"] = os.lstat(path).st_mtime_ns
    if state["type"] == "dir":
        state["children"] = {
            child.name: entry_state_with_mtime(child)
            for child in sorted(path.iterdir(), key=lambda item: item.name)
        }
    return state


def test_second_backup_copy_failure_never_restores_untouched_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-backup-stage", "new")
    standard = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-backup-stage"
    standard.mkdir(parents=True)
    (standard / "SKILL.md").write_text("old standard", encoding="utf-8")
    standard.chmod(0o751)
    link_source = tmp_path / "old-codex-link"
    link_source.mkdir()
    (link_source / "SKILL.md").write_text("old link", encoding="utf-8")
    codex = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]) / "sdlc-backup-stage"
    codex.parent.mkdir(parents=True)
    codex.symlink_to(link_source, target_is_directory=True)
    before = {str(path): entry_state_with_mtime(path) for path in [standard, codex, link_source]}
    configure_direct_env(monkeypatch, env)
    real_copy = agent_sync._copy_snapshot_to_backup
    copied_existing = 0

    def fail_second_existing_backup(target: Path, snapshot: dict, backup_item: Path) -> None:
        nonlocal copied_existing
        if snapshot.get("exists"):
            copied_existing += 1
            if copied_existing == 2:
                raise OSError(5, "第二个备份复制失败")
        real_copy(target, snapshot, backup_item)

    monkeypatch.setattr(agent_sync, "_copy_snapshot_to_backup", fail_second_existing_backup)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    message = str(error.value)
    assert "同步失败，未开始写入" in message
    assert "回滚失败" not in message
    assert copied_existing == 2
    for path in [standard, codex, link_source]:
        assert entry_state_with_mtime(path) == before[str(path)]
    assert not (Path(env["CODEX_SDLC_AGENT_HOME"]) / "backups").exists()


def test_failed_transaction_preserves_concurrent_parent_file_and_removes_empty_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-concurrent-parent", "new")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    concurrent_file = paths.agent_home / "并发保留.txt"

    def create_concurrent_file_then_fail(target: Path, snapshot: dict, backup_item: Path) -> None:
        # 在事务已经创建父目录、但尚未写目标时，模拟其它进程写入一个不属于本事务的文件。
        concurrent_file.write_text("其它进程的内容", encoding="utf-8")
        raise OSError(5, "备份阶段故障")

    monkeypatch.setattr(agent_sync, "_copy_snapshot_to_backup", create_concurrent_file_then_fail)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    assert "同步失败，未开始写入" in str(error.value)
    assert "回滚失败" not in str(error.value)
    assert concurrent_file.read_text(encoding="utf-8") == "其它进程的内容"
    assert paths.agent_home.is_dir()
    assert not paths.backup_home.exists(), "本轮创建且仍为空的备份父目录应被删除"


def test_parent_rmdir_eacces_reports_restored_target_without_fake_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-parent-eacces", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-parent-eacces"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    before = exact_path_state(target)
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_skill_replace")
    paths = agent_sync.default_paths()
    real_rmdir = Path.rmdir

    def fail_backup_parent_rmdir(value: Path) -> None:
        if value == paths.backup_home:
            raise OSError(errno.EACCES, "父目录无删除权限")
        real_rmdir(value)

    monkeypatch.setattr(Path, "rmdir", fail_backup_parent_rmdir)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    message = str(error.value)
    assert exact_path_state(target) == before
    assert "目标已恢复，但空父目录清理失败" in message
    assert str(paths.backup_home) in message
    assert "父目录无删除权限" in message
    assert "可人工恢复备份" not in message
    assert "可手工清理事务备份" not in message
    assert not list(paths.backup_home.glob("agent-sync-*"))
    assert paths.backup_home.is_dir()


def test_transaction_cleanup_failure_is_reported_separately_with_existing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-transaction-cleanup", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-transaction-cleanup"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    before = exact_path_state(target)
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_skill_replace")
    paths = agent_sync.default_paths()
    real_remove_path = agent_sync._remove_path

    def fail_transaction_remove(value: Path) -> None:
        if value.parent == paths.backup_home and value.name.startswith("agent-sync-"):
            raise OSError(errno.EACCES, "事务目录无删除权限")
        real_remove_path(value)

    monkeypatch.setattr(agent_sync, "_remove_path", fail_transaction_remove)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    backups = list(paths.backup_home.glob("agent-sync-*"))
    assert len(backups) == 1
    backup = backups[0].absolute()
    message = str(error.value)
    assert exact_path_state(target) == before
    assert "目标已恢复，但事务备份清理失败" in message
    assert "事务目录无删除权限" in message
    assert f"可手工清理事务备份：{backup}" in message
    assert "可人工恢复备份" not in message


def test_restore_copy_failure_keeps_unique_manual_recovery_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-restore-copy", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-restore-copy"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_skill_replace")
    real_copytree = agent_sync.shutil.copytree

    def fail_backup_restore(source_path, destination, *args, **kwargs):
        source_value = Path(source_path)
        if "backups" in source_value.parts and source_value.parent.name == "targets" and Path(destination) == target:
            raise OSError(5, "恢复复制失败")
        return real_copytree(source_path, destination, *args, **kwargs)

    monkeypatch.setattr(agent_sync.shutil, "copytree", fail_backup_restore)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    backups = list((Path(env["CODEX_SDLC_AGENT_HOME"]) / "backups").glob("agent-sync-*"))
    assert len(backups) == 1
    backup = backups[0].absolute()
    message = str(error.value)
    assert "同步失败且目标回滚失败" in message
    assert str(target) in message
    recovery_item = agent_sync._backup_path_for_target(target, backup)
    assert f"可人工恢复：{target} -> {recovery_item.absolute()}" in message
    assert f"事务诊断目录：{backup}" in message
    assert (recovery_item / "SKILL.md").read_text(encoding="utf-8") == "old"


def test_new_target_delete_failure_reports_no_fake_recovery_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-new-target-delete", "new")
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_skill_replace")
    paths = agent_sync.default_paths()
    target = paths.standard_skills_home / "sdlc-new-target-delete"
    real_remove_path = agent_sync._remove_path

    def fail_new_target_delete(value: Path) -> None:
        if value == target:
            raise OSError(errno.EACCES, "本轮新目标无删除权限")
        real_remove_path(value)

    monkeypatch.setattr(agent_sync, "_remove_path", fail_new_target_delete)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    backups = list(paths.backup_home.glob("agent-sync-*"))
    assert len(backups) == 1
    transaction_dir = backups[0].absolute()
    message = str(error.value)
    assert target.is_dir()
    assert f"本轮新建目标未能删除：{target}" in message
    assert "可人工恢复" not in message
    assert f"事务诊断目录：{transaction_dir}" in message
    assert not agent_sync._backup_path_for_target(target, transaction_dir).exists()


@pytest.mark.parametrize("second_cleanup_succeeds", [False, True])
def test_atomic_temp_cleanup_is_retried_by_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_cleanup_succeeds: bool,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-atomic-cleanup", "new")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    command_target = paths.claude_commands_home / "sdlc-atomic-cleanup.md"
    real_replace = agent_sync.os.replace
    real_unlink = Path.unlink
    owned_temp: list[Path] = []
    cleanup_attempts = 0

    def fail_command_replace(source_path, destination_path):
        if Path(destination_path) == command_target:
            owned_temp.append(Path(source_path).absolute())
            raise OSError(errno.EIO, "原子替换故障")
        return real_replace(source_path, destination_path)

    def fail_owned_temp_unlink(value: Path, *args, **kwargs):
        nonlocal cleanup_attempts
        if owned_temp and value.absolute() == owned_temp[0]:
            cleanup_attempts += 1
            if cleanup_attempts == 1 or not second_cleanup_succeeds:
                raise OSError(errno.EACCES, f"临时文件清理故障{cleanup_attempts}")
        return real_unlink(value, *args, **kwargs)

    monkeypatch.setattr(agent_sync.os, "replace", fail_command_replace)
    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    assert len(owned_temp) == 1
    temp_path = owned_temp[0]
    message = str(error.value)
    assert "原子替换故障" in message
    assert cleanup_attempts == 2
    if second_cleanup_succeeds:
        assert not temp_path.exists()
        assert "同步失败已回滚" in message
        assert "事务拥有临时文件清理失败" not in message
    else:
        assert temp_path.is_file()
        assert "事务拥有临时文件清理失败" in message
        assert str(temp_path) in message
        assert "临时文件清理故障1" in message
        assert "临时文件清理故障2" in message
        assert "同步失败已回滚" not in message


def test_snapshot_oserror_fails_before_write_and_cleans_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-lstat", "lstat")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    target = paths.standard_skills_home / "sdlc-lstat"
    original_lstat = agent_sync.os.lstat

    with monkeypatch.context() as patcher:
        def fail_lstat(value, *args, **kwargs):
            if Path(value) == target:
                raise OSError(5, "Input/output error")
            return original_lstat(value, *args, **kwargs)

        patcher.setattr(agent_sync.os, "lstat", fail_lstat)
        with pytest.raises(agent_sync.SdlcError, match="无法读取快照"):
            agent_sync.sync_agent_entries(confirm=True)

    assert not target.exists()
    assert not paths.backup_home.exists()
    assert not list(paths.agent_home.parent.glob("agent-sync-*"))


def test_snapshot_readlink_and_read_bytes_use_real_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # readlink 必须确实面对一个已有符号链接，而不是依赖固定技能名短路。
    link_root = tmp_path / "readlink"
    env = agent_sync_env(link_root)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-link", "link")
    paths = agent_sync.AgentSyncPaths(
        agent_home=Path(env["CODEX_SDLC_AGENT_HOME"]),
        source_skills_home=source,
        shared_source_skills_home=Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"]),
        codex_skills_home=Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]),
        agents_skills_home=Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"]),
        claude_home=Path(env["CODEX_SDLC_CLAUDE_HOME"]),
    )
    target = paths.standard_skills_home / "sdlc-link"
    link_source = link_root / "link-source"
    link_source.mkdir(parents=True)
    (link_source / "SKILL.md").write_text("old link", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.symlink_to(link_source, target_is_directory=True)
    original_readlink = agent_sync.os.readlink

    with monkeypatch.context() as patcher:
        def fail_readlink(value, *args, **kwargs):
            if Path(value) == target:
                raise OSError(22, "Invalid argument")
            return original_readlink(value, *args, **kwargs)

        patcher.setattr(agent_sync.os, "readlink", fail_readlink)
        with pytest.raises(agent_sync.SdlcError, match="无法读取快照链接"):
            agent_sync.snapshot_target(target)

    assert target.is_symlink()
    assert os.readlink(target) == str(link_source)
    assert not paths.backup_home.exists()

    # read_bytes 通过已有 CLAUDE.md 触发，错误发生在第一次目标写入前。
    bytes_root = tmp_path / "read-bytes"
    env2 = agent_sync_env(bytes_root)
    source2 = Path(env2["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source2, "sdlc-bytes", "bytes")
    claude_md = Path(env2["CODEX_SDLC_CLAUDE_HOME"]) / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("old claude", encoding="utf-8")
    before = exact_path_state(claude_md)
    configure_direct_env(monkeypatch, env2)
    original_read_bytes = Path.read_bytes

    with monkeypatch.context() as patcher:
        def fail_read_bytes(value: Path):
            if value == claude_md:
                raise OSError(5, "Input/output error")
            return original_read_bytes(value)

        patcher.setattr(Path, "read_bytes", fail_read_bytes)
        with pytest.raises(agent_sync.SdlcError, match="无法读取快照文件"):
            agent_sync.sync_agent_entries(confirm=True)

    assert exact_path_state(claude_md) == before
    assert not (Path(env2["CODEX_SDLC_AGENT_HOME"]) / "backups").exists()


def test_same_path_resolve_readlink_oserror_stops_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-resolve-link", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-resolve-link"
    link_source = tmp_path / "resolve-link-source"
    link_source.mkdir()
    (link_source / "SKILL.md").write_text("old", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.symlink_to(link_source, target_is_directory=True)
    before = exact_path_state(target)
    original_resolve = Path.resolve

    def fail_target_resolve(value: Path, *args, **kwargs):
        if value == target:
            raise OSError(5, "路径解析读取链接失败")
        return original_resolve(value, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_target_resolve)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.same_path(source / "sdlc-resolve-link", target)

    assert "无法解析路径" in str(error.value)
    assert str(target) in str(error.value)
    assert "路径解析读取链接失败" in str(error.value)
    assert exact_path_state(target) == before
    assert not (Path(env["CODEX_SDLC_AGENT_HOME"]) / "backups").exists()


def test_target_registration_merges_lexical_aliases_without_following_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    targets: dict[str, Path] = {}
    roles: dict[str, list[str]] = {}
    relative_target = Path("real/../real/target")
    absolute_target = tmp_path / "real" / "target"
    agent_sync._register_target(targets, roles, relative_target, "相对路径")
    agent_sync._register_target(targets, roles, absolute_target, "绝对路径")

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    link_root = tmp_path / "link-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    agent_sync._register_target(targets, roles, link_root / "target", "符号链接路径")

    assert len(targets) == 2
    lexical_key = agent_sync._lexical_absolute(absolute_target)
    assert targets[lexical_key] == relative_target
    assert roles[lexical_key] == ["相对路径", "绝对路径"]
    assert agent_sync._lexical_absolute(link_root / "target") != agent_sync._lexical_absolute(real_root / "target")
    backup_dir = tmp_path / "backup"
    assert agent_sync._backup_path_for_target(relative_target, backup_dir) == agent_sync._backup_path_for_target(absolute_target, backup_dir)
    assert agent_sync._backup_path_for_target(link_root / "target", backup_dir) != agent_sync._backup_path_for_target(real_root / "target", backup_dir)


def test_real_failure_after_skill_replace_and_command_delete_restores_exact_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    shared = Path(env["CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-rollback", "new skill")
    write_skill(shared, "agent-capability-sync", "new shared skill")

    agent_home = Path(env["CODEX_SDLC_AGENT_HOME"])
    standard = agent_home / "skills" / "sdlc-rollback"
    standard.mkdir(parents=True)
    (standard / "SKILL.md").write_text("old standard", encoding="utf-8")
    standard.chmod(0o751)
    manifest = agent_home / "manifest.json"
    manifest.write_text('{"old": true}\n', encoding="utf-8")
    manifest.chmod(0o640)

    codex = Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]) / "sdlc-rollback"
    codex.mkdir(parents=True)
    (codex / "SKILL.md").write_text("old codex", encoding="utf-8")
    codex.chmod(0o705)
    claude_home = Path(env["CODEX_SDLC_CLAUDE_HOME"])
    claude_commands = claude_home / "commands"
    claude_commands.mkdir(parents=True)
    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_text("old claude\n", encoding="utf-8")
    claude_md.chmod(0o604)
    stale_command = claude_commands / "sdlc-old.md"
    stale_command.write_text("old stale", encoding="utf-8")
    stale_command.chmod(0o601)
    keep_command = claude_commands / "keep.md"
    keep_command.write_text("keep", encoding="utf-8")
    keep_command.chmod(0o640)

    roots = [agent_home, Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]), Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"]), claude_home]
    before = {str(root): exact_path_state(root) for root in roots}
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_stale_command")

    with pytest.raises(agent_sync.SdlcError, match="同步失败已回滚"):
        agent_sync.sync_agent_entries(confirm=True)

    for root in roots:
        assert exact_path_state(root) == before[str(root)], root
    assert not (agent_home / "backups").exists()
    assert not Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"]).exists()
    assert not list(tmp_path.rglob("agent-sync-*"))


def test_restore_target_reports_symlink_directory_and_file_chmod_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = ["symlink", "directory", "file"]
    for case in cases:
        root = tmp_path / case
        root.mkdir()
        target = root / "target"
        if case == "symlink":
            link_source = root / "link-source"
            link_source.write_text("link", encoding="utf-8")
            target.symlink_to(link_source)
            snapshot = agent_sync.snapshot_target(target)
            backup_item = None
        elif case == "directory":
            target.mkdir()
            (target / "item.txt").write_text("old", encoding="utf-8")
            target.chmod(0o751)
            snapshot = agent_sync.snapshot_target(target)
            backup_item = root / "backup"
            shutil.copytree(target, backup_item, symlinks=True)
            (target / "item.txt").write_text("new", encoding="utf-8")
        else:
            target.write_text("old", encoding="utf-8")
            target.chmod(0o640)
            snapshot = agent_sync.snapshot_target(target)
            backup_item = root / "backup.txt"
            shutil.copy2(target, backup_item)
            target.write_text("new", encoding="utf-8")

        real_chmod = agent_sync.os.chmod
        with monkeypatch.context() as patcher:
            def fail_chmod(value, mode, *args, **kwargs):
                if Path(value) == target:
                    raise OSError(13, "Permission denied")
                return real_chmod(value, mode, *args, **kwargs)

            patcher.setattr(agent_sync.os, "chmod", fail_chmod)
            assert agent_sync.restore_target(target, snapshot, backup_item) is False


def test_rollback_chmod_failure_names_unrestored_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-chmod", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-chmod"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    configure_direct_env(monkeypatch, env)
    monkeypatch.setenv("CODEX_SDLC_AGENT_SYNC_FAIL_POINT", "after_skill_replace")
    real_chmod = agent_sync.os.chmod

    def fail_restore_chmod(value, mode, *args, **kwargs):
        if Path(value) == target:
            raise OSError(13, "Permission denied")
        return real_chmod(value, mode, *args, **kwargs)

    monkeypatch.setattr(agent_sync.os, "chmod", fail_restore_chmod)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)
    assert "同步失败且目标回滚失败" in str(error.value)
    assert str(target) in str(error.value)


def test_atomic_write_uses_real_os_calls_and_distinguishes_fds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "atomic.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o601)
    calls: dict[str, list[object]] = {"chmod": [], "replace": [], "open": [], "fsync": []}
    real_chmod = agent_sync.os.chmod
    real_replace = agent_sync.os.replace
    real_open = agent_sync.os.open
    real_fsync = agent_sync.os.fsync

    def record_chmod(value, mode, *args, **kwargs):
        calls["chmod"].append((Path(value), mode))
        return real_chmod(value, mode, *args, **kwargs)

    def record_replace(source, destination):
        calls["replace"].append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def record_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        kind = "directory" if flags & os.O_DIRECTORY else "file"
        calls["open"].append(kind)
        return fd

    def record_fsync(fd):
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls["fsync"].append(kind)
        return real_fsync(fd)

    monkeypatch.setattr(agent_sync.os, "chmod", record_chmod)
    monkeypatch.setattr(agent_sync.os, "replace", record_replace)
    monkeypatch.setattr(agent_sync.os, "open", record_open)
    monkeypatch.setattr(agent_sync.os, "fsync", record_fsync)
    agent_sync.atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o601
    assert any(destination == target for _source, destination in calls["replace"])
    assert "directory" in calls["open"]
    assert set(calls["fsync"]) == {"file", "directory"}
    assert all(path != target for path, _mode in calls["chmod"])
    assert not list(tmp_path.glob("tmp*"))


@pytest.mark.parametrize("failure", ["chmod", "replace", "file_fsync"])
def test_atomic_write_real_failures_leave_original_and_no_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    target = tmp_path / "atomic-failure.txt"
    target.write_text("original", encoding="utf-8")
    target.chmod(0o640)
    original_state = exact_path_state(target)
    real_chmod = agent_sync.os.chmod
    real_replace = agent_sync.os.replace
    real_fsync = agent_sync.os.fsync

    def fail_chmod(value, mode, *args, **kwargs):
        if failure == "chmod":
            raise OSError(13, "Permission denied")
        return real_chmod(value, mode, *args, **kwargs)

    def fail_replace(source, destination):
        if failure == "replace":
            raise OSError(5, "Input/output error")
        return real_replace(source, destination)

    def fail_fsync(fd):
        if failure == "file_fsync" and not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(agent_sync.os, "chmod", fail_chmod)
    monkeypatch.setattr(agent_sync.os, "replace", fail_replace)
    monkeypatch.setattr(agent_sync.os, "fsync", fail_fsync)
    with pytest.raises(agent_sync.SdlcError):
        agent_sync.atomic_write_text(target, "new")

    assert exact_path_state(target) == original_state
    assert not list(tmp_path.glob("tmp*"))


def test_atomic_write_keyboard_interrupt_cleans_owned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "atomic-interrupt.txt"
    target.write_text("original", encoding="utf-8")
    original_state = exact_path_state(target)
    real_fsync = agent_sync.os.fsync
    interrupted = False

    def interrupt_file_fsync(fd: int) -> None:
        nonlocal interrupted
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            interrupted = True
            raise KeyboardInterrupt
        real_fsync(fd)

    monkeypatch.setattr(agent_sync.os, "fsync", interrupt_file_fsync)
    with pytest.raises(KeyboardInterrupt):
        agent_sync.atomic_write_text(target, "new")

    assert interrupted
    assert exact_path_state(target) == original_state
    assert not list(tmp_path.glob("tmp*"))


@pytest.mark.parametrize("resource", ["stream", "dir"])
def test_sigint_is_delivered_only_after_close_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-real-sigint.txt"
    real_fdopen = agent_sync.os.fdopen
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    owned_fds: list[int] = []
    close_completed = False

    class SignalStream:
        def __init__(self, inner) -> None:
            self.inner = inner

        def fileno(self) -> int:
            return self.inner.fileno()

        def write(self, content: str) -> int:
            return self.inner.write(content)

        def flush(self) -> None:
            self.inner.flush()

        def close(self) -> None:
            nonlocal close_completed
            os.kill(os.getpid(), signal.SIGINT)
            self.inner.close()
            close_completed = True

    def controlled_fdopen(fd: int, *args, **kwargs):
        inner = real_fdopen(fd, *args, **kwargs)
        owned_fds.append(fd)
        return SignalStream(inner) if resource == "stream" else inner

    def controlled_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            owned_fds.append(fd)
        return fd

    def controlled_close(fd: int) -> None:
        nonlocal close_completed
        if resource == "dir" and owned_fds and fd == owned_fds[-1]:
            os.kill(os.getpid(), signal.SIGINT)
            real_close(fd)
            close_completed = True
            return
        real_close(fd)

    monkeypatch.setattr(agent_sync.os, "fdopen", controlled_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", controlled_open)
    monkeypatch.setattr(agent_sync.os, "close", controlled_close)
    with pytest.raises(KeyboardInterrupt):
        agent_sync.atomic_write_text(target, "new")

    assert close_completed, "SIGINT 必须在真实 close 完成并恢复信号掩码后才传播"
    assert owned_fds
    with pytest.raises(OSError) as closed_error:
        fcntl.fcntl(owned_fds[-1], fcntl.F_GETFD)
    assert closed_error.value.errno == errno.EBADF


@pytest.mark.parametrize("resource", ["raw", "stream", "dir"])
def test_close_then_same_object_fd_reuse_is_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-same-object-reuse.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    original_interrupt = KeyboardInterrupt(f"{resource} 原始操作中断")
    owned_fds: list[int] = []
    owned_paths: list[Path] = []
    reused_fds: list[int] = []
    original_identities: list[tuple[int, int, int]] = []
    close_calls = 0

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        owned_fds.append(fd)
        owned_paths.append(Path(value))
        return fd, value

    def close_and_reopen_same(fd: int, path: Path, flags: int) -> None:
        nonlocal close_calls
        close_calls += 1
        source_stat = os.stat(path)
        original_identities.append(
            (source_stat.st_dev, source_stat.st_ino, stat.S_IFMT(source_stat.st_mode))
        )
        real_close(fd)
        reused_fds.append(real_open(path, flags))
        raise SystemExit(71)

    class ReusingStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, _content: str) -> None:
            raise original_interrupt

        def flush(self) -> None:
            raise AssertionError("write 中断后不应继续 flush")

        def close(self) -> None:
            close_and_reopen_same(self.fd, owned_paths[0], os.O_RDONLY)

    def controlled_fdopen(fd: int, *args, **kwargs):
        if resource == "raw":
            raise original_interrupt
        if resource == "stream":
            return ReusingStream(fd)
        return agent_sync.os.fdopen(fd, *args, **kwargs)

    original_fdopen = agent_sync.os.fdopen

    def selected_fdopen(fd: int, *args, **kwargs):
        if resource in {"raw", "stream"}:
            return controlled_fdopen(fd, *args, **kwargs)
        return original_fdopen(fd, *args, **kwargs)

    def controlled_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            owned_fds.append(fd)
            owned_paths.append(Path(value))
        return fd

    def controlled_close(fd: int) -> None:
        if owned_fds and fd == owned_fds[-1]:
            flags = os.O_DIRECTORY if resource == "dir" else os.O_RDONLY
            close_and_reopen_same(fd, owned_paths[-1], flags)
        real_close(fd)

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", selected_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", controlled_open)
    monkeypatch.setattr(agent_sync.os, "close", controlled_close)
    try:
        with pytest.raises(agent_sync.AtomicResourceCleanupError) as error:
            agent_sync.atomic_write_text(target, "new")

        if resource in {"raw", "stream"}:
            assert error.value.__cause__ is original_interrupt
        else:
            assert isinstance(error.value.__cause__, SystemExit)
            assert error.value.__cause__.code == 71
        assert close_calls == 1, "close 返回异常后生产代码绝不能重试同一个 fd 数字"
        assert reused_fds == [owned_fds[-1]]
        reused_stat = os.fstat(reused_fds[0])
        assert (reused_stat.st_dev, reused_stat.st_ino, stat.S_IFMT(reused_stat.st_mode)) == original_identities[0]
        assert fcntl.fcntl(reused_fds[0], fcntl.F_GETFD) >= 0
        message = str(error.value)
        assert f"resource={resource}，fd={owned_fds[-1]}" in message
        assert "资源关闭结果不确定/失败" in message
    finally:
        if reused_fds:
            real_close(reused_fds[0])


@pytest.mark.parametrize("resource", ["raw", "stream", "dir"])
def test_close_before_actual_close_reports_uncertain_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-close-uncertain.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    original_interrupt = KeyboardInterrupt(f"{resource} 原始操作中断")
    owned_fds: list[int] = []
    close_calls = 0

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        owned_fds.append(fd)
        return fd, value

    class FailingStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, _content: str) -> None:
            raise original_interrupt

        def flush(self) -> None:
            raise AssertionError("write 中断后不应继续 flush")

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            raise SystemExit(73)

    original_fdopen = agent_sync.os.fdopen

    def controlled_fdopen(fd: int, *args, **kwargs):
        if resource == "raw":
            raise original_interrupt
        if resource == "stream":
            return FailingStream(fd)
        return original_fdopen(fd, *args, **kwargs)

    def controlled_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            owned_fds.append(fd)
        return fd

    def controlled_close(fd: int) -> None:
        nonlocal close_calls
        if owned_fds and fd == owned_fds[-1]:
            close_calls += 1
            raise OSError(errno.EIO, f"{resource} close 在实际关闭前失败")
        real_close(fd)

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", controlled_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", controlled_open)
    monkeypatch.setattr(agent_sync.os, "close", controlled_close)
    try:
        with pytest.raises(agent_sync.AtomicResourceCleanupError) as error:
            agent_sync.atomic_write_text(target, "new")

        assert close_calls == 1
        assert owned_fds
        assert fcntl.fcntl(owned_fds[-1], fcntl.F_GETFD) >= 0
        message = str(error.value)
        assert f"resource={resource}，fd={owned_fds[-1]}" in message
        assert "资源关闭结果不确定/失败" in message
        assert "重试关闭" not in message
        if resource in {"raw", "stream"}:
            assert error.value.__cause__ is original_interrupt
        else:
            assert isinstance(error.value.__cause__, OSError)
            assert error.value.__cause__.errno == errno.EIO
    finally:
        if owned_fds:
            real_close(owned_fds[-1])


@pytest.mark.parametrize("resource", ["raw", "stream", "dir"])
def test_signal_mask_restore_failure_preserves_earliest_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-mask-restore-with-prior.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_fdopen = agent_sync.os.fdopen
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    real_fsync = agent_sync.os.fsync
    original_error = KeyboardInterrupt(f"{resource} 最早业务中断")
    restore_error = OSError(errno.EIO, f"{resource} SIG_SETMASK 失败")
    file_fds: list[int] = []
    directory_fds: list[int] = []
    restore_calls = 0
    fail_restore_call = 2 if resource == "dir" else 1

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        file_fds.append(fd)
        return fd, value

    class PriorErrorStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, _content: str) -> None:
            raise original_error

        def flush(self) -> None:
            raise AssertionError("write 中断后不应继续 flush")

        def close(self) -> None:
            real_close(self.fd)

    def controlled_fdopen(fd: int, *args, **kwargs):
        if resource == "raw":
            raise original_error
        if resource == "stream":
            return PriorErrorStream(fd)
        return real_fdopen(fd, *args, **kwargs)

    def record_directory_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        return fd

    def fail_directory_fsync(fd: int) -> None:
        if resource == "dir" and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise original_error
        real_fsync(fd)

    def fail_selected_restore(how, mask):
        nonlocal restore_calls
        if how == signal.SIG_BLOCK:
            return set()
        assert how == signal.SIG_SETMASK
        restore_calls += 1
        if restore_calls == fail_restore_call:
            raise restore_error
        return set()

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", controlled_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", record_directory_open)
    monkeypatch.setattr(agent_sync.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(agent_sync.signal, "pthread_sigmask", fail_selected_restore)
    with pytest.raises(agent_sync.AtomicResourceCleanupError) as error:
        agent_sync.atomic_write_text(target, "new")

    owned_fd = directory_fds[-1] if resource == "dir" else file_fds[0]
    assert error.value.__cause__ is original_error
    message = str(error.value)
    assert f"resource={resource}，fd={owned_fd}" in message
    assert "阶段=恢复SIGINT信号掩码" in message
    assert "信号掩码恢复失败，当前线程状态不确定" in message
    assert "OSError" in message
    assert f"{resource} SIG_SETMASK 失败" in message
    with pytest.raises(OSError) as closed_error:
        fcntl.fcntl(owned_fd, fcntl.F_GETFD)
    assert closed_error.value.errno == errno.EBADF


@pytest.mark.parametrize("resource", ["stream", "dir"])
def test_signal_mask_restore_failure_without_prior_error_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-mask-restore-no-prior.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_fdopen = agent_sync.os.fdopen
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    restore_error = OSError(errno.EIO, f"{resource} SIG_SETMASK 单独失败")
    file_fds: list[int] = []
    directory_fds: list[int] = []
    restore_calls = 0
    fail_restore_call = 2 if resource == "dir" else 1

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        file_fds.append(fd)
        return fd, value

    class SuccessfulStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, content: str) -> int:
            return len(content)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            real_close(self.fd)

    def controlled_fdopen(fd: int, *args, **kwargs):
        if resource == "stream":
            return SuccessfulStream(fd)
        return real_fdopen(fd, *args, **kwargs)

    def record_directory_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        return fd

    def fail_selected_restore(how, mask):
        nonlocal restore_calls
        if how == signal.SIG_BLOCK:
            return set()
        assert how == signal.SIG_SETMASK
        restore_calls += 1
        if restore_calls == fail_restore_call:
            raise restore_error
        return set()

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", controlled_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", record_directory_open)
    monkeypatch.setattr(agent_sync.signal, "pthread_sigmask", fail_selected_restore)
    with pytest.raises(agent_sync.AtomicResourceCleanupError) as error:
        agent_sync.atomic_write_text(target, "new")

    owned_fd = directory_fds[-1] if resource == "dir" else file_fds[0]
    assert error.value.__cause__ is restore_error
    message = str(error.value)
    assert f"resource={resource}，fd={owned_fd}" in message
    assert "阶段=恢复SIGINT信号掩码" in message
    assert "信号掩码恢复失败，当前线程状态不确定" in message
    assert f"{resource} SIG_SETMASK 单独失败" in message
    with pytest.raises(OSError) as closed_error:
        fcntl.fcntl(owned_fd, fcntl.F_GETFD)
    assert closed_error.value.errno == errno.EBADF


@pytest.mark.parametrize("resource", ["raw", "stream", "dir"])
def test_sigint_arriving_immediately_before_mask_restore_wins_without_losing_prior_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    target = tmp_path / f"{resource}-restore-race.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_fdopen = agent_sync.os.fdopen
    real_open = agent_sync.os.open
    real_close = agent_sync.os.close
    real_fsync = agent_sync.os.fsync
    real_sigmask = agent_sync.signal.pthread_sigmask
    prior_error = RuntimeError(f"{resource} 更早的普通业务错误")
    file_fds: list[int] = []
    directory_fds: list[int] = []
    restore_calls = 0
    deliver_on_call = 2 if resource == "dir" else 1
    original_mask = real_sigmask(signal.SIG_BLOCK, set())

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        file_fds.append(fd)
        return fd, value

    class PriorErrorStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, _content: str) -> None:
            raise prior_error

        def flush(self) -> None:
            raise AssertionError("write 失败后不应继续 flush")

        def close(self) -> None:
            real_close(self.fd)

    def controlled_fdopen(fd: int, *args, **kwargs):
        if resource == "raw":
            raise prior_error
        if resource == "stream":
            return PriorErrorStream(fd)
        return real_fdopen(fd, *args, **kwargs)

    def record_directory_open(value, flags, *args, **kwargs):
        fd = real_open(value, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.append(fd)
        return fd

    def fail_directory_fsync(fd: int) -> None:
        if resource == "dir" and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise prior_error
        real_fsync(fd)

    def deliver_sigint_at_restore(how, mask):
        nonlocal restore_calls
        if how == signal.SIG_SETMASK:
            restore_calls += 1
            if restore_calls == deliver_on_call:
                # SIGINT 在真正恢复掩码的前一刻到达，覆盖旧 sigpending 采样竞态。
                os.kill(os.getpid(), signal.SIGINT)
        return real_sigmask(how, mask)

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", controlled_fdopen)
    monkeypatch.setattr(agent_sync.os, "open", record_directory_open)
    monkeypatch.setattr(agent_sync.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(agent_sync.signal, "pthread_sigmask", deliver_sigint_at_restore)
    with pytest.raises(KeyboardInterrupt) as error:
        agent_sync.atomic_write_text(target, "new")

    owned_fd = directory_fds[-1] if resource == "dir" else file_fds[0]
    assert error.value.__cause__ is prior_error
    assert "恢复失败" not in str(error.value)
    with pytest.raises(OSError) as closed_error:
        fcntl.fcntl(owned_fd, fcntl.F_GETFD)
    assert closed_error.value.errno == errno.EBADF
    assert real_sigmask(signal.SIG_BLOCK, set()) == original_mask


def test_system_exit_during_mask_restore_uses_non_sigint_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "stream-mask-restore-system-exit.txt"
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_close = agent_sync.os.close
    owned_fds: list[int] = []
    restore_exit = SystemExit(79)

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        owned_fds.append(fd)
        return fd, value

    class SuccessfulStream:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def write(self, content: str) -> int:
            return len(content)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            real_close(self.fd)

    def controlled_sigmask(how, mask):
        if how == signal.SIG_BLOCK:
            return set()
        raise restore_exit

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fdopen", lambda fd, *args, **kwargs: SuccessfulStream(fd))
    monkeypatch.setattr(agent_sync.signal, "pthread_sigmask", controlled_sigmask)
    with pytest.raises(agent_sync.AtomicResourceCleanupError) as error:
        agent_sync.atomic_write_text(target, "new")

    assert error.value.__cause__ is restore_exit
    message = str(error.value)
    assert "收到非SIGINT BaseException" in message
    assert "SystemExit" in message
    assert "信号掩码恢复失败" not in message
    with pytest.raises(OSError) as closed_error:
        fcntl.fcntl(owned_fds[0], fcntl.F_GETFD)
    assert closed_error.value.errno == errno.EBADF


def test_atomic_write_close_primitive_works_in_non_main_thread(tmp_path: Path) -> None:
    target = tmp_path / "worker-thread-atomic.txt"
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            agent_sync.atomic_write_text(target, "worker")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert target.read_text(encoding="utf-8") == "worker"

def test_agent_sync_keyboard_interrupt_rolls_back_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-interrupt", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-interrupt"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    roots = [paths.agent_home, paths.codex_skills_home, paths.agents_skills_home, paths.claude_home]
    before = {str(root): exact_path_state(root) for root in roots}
    real_fsync = agent_sync.os.fsync
    interrupted = False

    def interrupt_file_fsync(fd: int) -> None:
        nonlocal interrupted
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            interrupted = True
            raise KeyboardInterrupt
        real_fsync(fd)

    monkeypatch.setattr(agent_sync.os, "fsync", interrupt_file_fsync)
    with pytest.raises(KeyboardInterrupt) as error:
        agent_sync.sync_agent_entries(confirm=True)

    output = capsys.readouterr()
    assert interrupted
    assert "同步失败已回滚" not in str(error.value)
    assert "同步失败已回滚" not in output.out + output.err
    for root in roots:
        assert exact_path_state(root) == before[str(root)], root
    assert not paths.backup_home.exists()
    assert not list(tmp_path.rglob("tmp*"))


def test_interrupt_with_owned_temp_cleanup_interrupt_reports_original_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-interrupt-cleanup", "new")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    real_fsync = agent_sync.os.fsync
    real_mkstemp = agent_sync.tempfile.mkstemp
    real_unlink = Path.unlink
    original_interrupt = KeyboardInterrupt("文件 fsync 用户中断")
    owned_temp: list[Path] = []
    cleanup_attempts = 0

    def record_mkstemp(*args, **kwargs):
        fd, value = real_mkstemp(*args, **kwargs)
        owned_temp.append(Path(value).absolute())
        return fd, value

    def interrupt_file_fsync(fd: int) -> None:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise original_interrupt
        real_fsync(fd)

    def fail_both_owned_temp_cleanups(value: Path, *args, **kwargs):
        nonlocal cleanup_attempts
        if owned_temp and value.absolute() == owned_temp[0]:
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise OSError(errno.EACCES, "首次 unlink 无权限")
            if cleanup_attempts == 2:
                raise KeyboardInterrupt("二次 unlink 用户中断")
        return real_unlink(value, *args, **kwargs)

    monkeypatch.setattr(agent_sync.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(agent_sync.os, "fsync", interrupt_file_fsync)
    monkeypatch.setattr(Path, "unlink", fail_both_owned_temp_cleanups)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    assert len(owned_temp) == 1
    temp_path = owned_temp[0]
    message = str(error.value)
    assert error.value.__cause__ is original_interrupt
    assert cleanup_attempts == 2
    assert temp_path.is_file()
    assert str(temp_path) in message
    assert "首次 unlink 无权限" in message
    assert "KeyboardInterrupt" in message
    assert "二次 unlink 用户中断" in message
    assert "同步失败已回滚" not in message
    assert not list(paths.backup_home.glob("agent-sync-*"))


def test_system_exit_with_restore_interrupt_continues_other_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-system-exit", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-system-exit"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    configure_direct_env(monkeypatch, env)
    paths = agent_sync.default_paths()
    codex_target = paths.codex_skills_home / "sdlc-system-exit"
    real_fsync = agent_sync.os.fsync
    real_remove_path = agent_sync._remove_path
    original_exit = SystemExit(77)
    original_exit_raised = False

    def exit_from_file_fsync(fd: int) -> None:
        nonlocal original_exit_raised
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            original_exit_raised = True
            raise original_exit
        real_fsync(fd)

    def interrupt_one_target_restore(value: Path) -> None:
        if original_exit_raised and value == target:
            raise KeyboardInterrupt("恢复目标时再次中断")
        real_remove_path(value)

    monkeypatch.setattr(agent_sync.os, "fsync", exit_from_file_fsync)
    monkeypatch.setattr(agent_sync, "_remove_path", interrupt_one_target_restore)
    with pytest.raises(agent_sync.SdlcError) as error:
        agent_sync.sync_agent_entries(confirm=True)

    message = str(error.value)
    assert error.value.__cause__ is original_exit
    assert original_exit.code == 77
    assert "同步失败且目标回滚失败" in message
    assert str(target) in message
    assert "KeyboardInterrupt" in message
    assert "恢复目标时再次中断" in message
    assert (target / "SKILL.md").read_bytes() == (source / "sdlc-system-exit" / "SKILL.md").read_bytes()
    assert not codex_target.exists(), "一个目标恢复中断后，后续目标仍必须继续回滚"
    backups = list(paths.backup_home.glob("agent-sync-*"))
    assert len(backups) == 1
    assert agent_sync._backup_path_for_target(target, backups[0]).exists()


def test_directory_fsync_failure_rolls_back_after_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = agent_sync_env(tmp_path)
    source = Path(env["CODEX_SDLC_SOURCE_SKILLS_HOME"])
    write_skill(source, "sdlc-fsync", "new")
    target = Path(env["CODEX_SDLC_AGENT_HOME"]) / "skills" / "sdlc-fsync"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old", encoding="utf-8")
    claude_home = Path(env["CODEX_SDLC_CLAUDE_HOME"])
    claude_home.mkdir(parents=True)
    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_text("old claude", encoding="utf-8")
    roots = [Path(env["CODEX_SDLC_AGENT_HOME"]), Path(env["CODEX_SDLC_CODEX_SKILLS_HOME"]), Path(env["CODEX_SDLC_AGENTS_SKILLS_HOME"]), claude_home]
    before = {str(root): exact_path_state(root) for root in roots}
    configure_direct_env(monkeypatch, env)
    real_fsync = agent_sync.os.fsync
    real_replace = agent_sync.os.replace
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source_path, destination_path):
        replacements.append((Path(source_path), Path(destination_path)))
        return real_replace(source_path, destination_path)

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(agent_sync.os, "replace", record_replace)
    monkeypatch.setattr(agent_sync.os, "fsync", fail_directory_fsync)
    with pytest.raises(agent_sync.SdlcError, match="同步失败已回滚"):
        agent_sync.sync_agent_entries(confirm=True)

    assert replacements, "必须确实执行过 os.replace 后才进入目录 fsync 故障"
    for root in roots:
        assert exact_path_state(root) == before[str(root)], root
    assert not (Path(env["CODEX_SDLC_AGENT_HOME"]) / "backups").exists()
    assert not list(tmp_path.rglob("tmp*"))
