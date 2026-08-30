from __future__ import annotations

import base64
import hashlib
import io
import importlib.util
from importlib import metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


try:
    SETUPTOOLS_VERSION = metadata.version("setuptools")
except metadata.PackageNotFoundError as exc:
    raise AssertionError(
        "安装测试要求当前测试解释器已安装 setuptools>=77；请先安装项目开发依赖 .[dev]。"
    ) from exc
assert Version(SETUPTOOLS_VERSION) >= Version("77"), (
    f"安装测试要求当前测试解释器使用 setuptools>=77，实际为 {SETUPTOOLS_VERSION}；"
    "请先更新项目开发依赖 .[dev]。"
)


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_specstamp.py"
SUBPROCESS_TIMEOUT = 120


def _entry_state(path: Path) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink():
        return None
    info = os.lstat(path)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"type": "symlink", "mode": mode, "target": os.readlink(path)}
    if stat.S_ISREG(info.st_mode):
        return {"type": "file", "mode": mode, "content": path.read_bytes()}
    if stat.S_ISDIR(info.st_mode):
        children: dict[str, object] = {}
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            children[child.name] = _entry_state(child)
        return {"type": "dir", "mode": mode, "children": children}
    return {"type": "other", "mode": mode}


def tree_state(path: Path) -> dict[str, object] | None:
    """保存类型、权限、链接目标、内容和目录项，失败回滚必须与此完全一致。"""

    return _entry_state(path)


def copy_minimal_repo(dest: Path) -> Path:
    for name in ["src", "bin", "scripts", "skills", "shared-skills"]:
        source = REPO_ROOT / name
        if source.exists():
            shutil.copytree(source, dest / name, symlinks=True)
    for name in ["pyproject.toml", "setup.py", "README.md", "LICENSE", "NOTICE"]:
        source = REPO_ROOT / name
        if source.exists():
            shutil.copy2(source, dest / name)
    (dest / "bin" / "specstamp").chmod(0o755)
    (dest / "bin" / "codex-sdlc").chmod(0o755)
    (dest / "scripts" / "install_specstamp.py").chmod(0o755)
    return dest


def _record_line(filename: str, content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    return f"{filename},sha256={digest},{len(content)}"


def repack_installed_distribution(wheelhouse: Path, distribution_name: str) -> Path:
    """把当前测试解释器已安装的真实发行文件按原 wheel tag 重新封装。"""

    distribution = metadata.distribution(distribution_name)
    files = distribution.files
    assert files, f"当前测试环境缺少 {distribution_name} 的发行文件清单"
    wheel_file = next((item for item in files if str(item).endswith(".dist-info/WHEEL")), None)
    metadata_file = next((item for item in files if str(item).endswith(".dist-info/METADATA")), None)
    record_file = next((item for item in files if str(item).endswith(".dist-info/RECORD")), None)
    assert wheel_file is not None and metadata_file is not None and record_file is not None
    wheel_text = distribution.locate_file(wheel_file).read_text(encoding="utf-8")
    tags = [line.split(":", 1)[1].strip() for line in wheel_text.splitlines() if line.startswith("Tag:")]
    assert tags, f"{distribution_name} 的 WHEEL 没有真实 tag"

    contents: dict[str, bytes] = {}
    for item in files:
        if ".." in item.parts or item == record_file:
            continue
        source = distribution.locate_file(item)
        if source.is_file():
            contents[str(item)] = source.read_bytes()
    assert str(wheel_file) in contents
    assert str(metadata_file) in contents

    record_name = str(record_file)
    record_lines = [_record_line(name, content) for name, content in sorted(contents.items())]
    record_lines.append(f"{record_name},,")
    contents[record_name] = ("\n".join(record_lines) + "\n").encode("utf-8")
    normalized_name = re.sub(r"[-_.]+", "_", distribution.metadata["Name"])
    normalized_version = re.sub(r"[-]+", "_", distribution.version)
    wheel = wheelhouse / f"{normalized_name}-{normalized_version}-{tags[0]}.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in sorted(contents.items()):
            archive.writestr(filename, content)
    return wheel


def _runtime_distribution_names() -> list[str]:
    """按真实 Requires-Dist 递归收集运行依赖，不用测试桩替代依赖行为。"""

    pending = ["jsonschema", "pypdf"]
    selected: dict[str, str] = {}
    while pending:
        requested = pending.pop(0)
        distribution = metadata.distribution(requested)
        canonical = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        if canonical in selected:
            continue
        selected[canonical] = distribution.metadata["Name"]
        for raw_requirement in distribution.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            pending.append(requirement.name)
    return [selected[name] for name in sorted(selected)]


def build_project_wheel(repo: Path, wheelhouse: Path) -> Path:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "--wheel-dir",
        str(wheelhouse),
        str(repo),
    ]
    result = subprocess.run(
        command,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert result.returncode == 0, f"真实项目 wheel 构建失败：{' '.join(command)}\n{result.stdout}\n{result.stderr}"
    wheels = sorted(wheelhouse.glob("specstamp-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def assert_project_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata_text = archive.read(metadata_name).decode("utf-8")
        assert "Name: specstamp\n" in metadata_text
        assert "Requires-Dist: jsonschema" in metadata_text
        assert "Requires-Dist: pypdf" in metadata_text
        entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entry_point_text = archive.read(entry_points).decode("utf-8")
        assert "specstamp = codex_sdlc.cli:main" in entry_point_text
        assert "codex-sdlc = codex_sdlc.cli:main" in entry_point_text
        assert any(name.startswith("codex_sdlc/schemas/") and name.endswith(".json") for name in names)
        assert any(name.endswith("/share/specstamp/skills/sdlc-agent-sync/SKILL.md") for name in names)
        assert any(name.endswith("/share/specstamp/shared-skills/agent-capability-sync/SKILL.md") for name in names)


def build_offline_wheelhouse(root: Path, repo: Path) -> Path:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    for distribution_name in _runtime_distribution_names():
        repack_installed_distribution(wheelhouse, distribution_name)
    project_wheel = build_project_wheel(repo, wheelhouse)
    assert_project_wheel(project_wheel)
    return wheelhouse


def isolated_env(repo: Path, isolated_home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(isolated_home),
            "CODEX_SDLC_AGENT_HOME": str(isolated_home / ".agents" / "sdlc"),
            "CODEX_SDLC_SOURCE_SKILLS_HOME": str(repo / "skills"),
            "CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME": str(repo / "shared-skills"),
            "CODEX_SDLC_CODEX_SKILLS_HOME": str(isolated_home / ".codex" / "skills"),
            "CODEX_SDLC_AGENTS_SKILLS_HOME": str(isolated_home / ".agents" / "skills"),
            "CODEX_SDLC_CLAUDE_HOME": str(isolated_home / ".claude"),
            "CODEX_SKILLS_HOME": str(isolated_home / ".codex" / "skills"),
            "CODEX_SDLC_HOME": str(repo),
            "PIP_NO_INDEX": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def cli_links(isolated_home: Path) -> dict[str, Path]:
    bin_dir = isolated_home / ".local" / "bin"
    return {
        "specstamp": bin_dir / "specstamp",
        "codex-sdlc": bin_dir / "codex-sdlc",
    }


def assert_no_cli_links(isolated_home: Path) -> None:
    for link in cli_links(isolated_home).values():
        assert not link.exists() and not link.is_symlink()


def run_install(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "install_specstamp.py"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )


def load_install_module():
    spec = importlib.util.spec_from_file_location("specstamp_install_for_test", INSTALL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_no_args_is_error_before_write(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_no_args")
    isolated_home = tmp_path / "home_no_args"
    isolated_home.mkdir()
    env = isolated_env(repo, isolated_home)
    result = run_install(repo, env)

    assert result.returncode == 2
    assert "--dry-run-agent-sync" in result.stderr
    assert "--confirm-agent-sync" in result.stderr
    assert not (repo / ".venv").exists()
    assert_no_cli_links(isolated_home)
    assert tree_state(isolated_home / ".codex") is None
    assert tree_state(isolated_home / ".agents") is None
    assert tree_state(isolated_home / ".claude") is None


def test_install_dry_run_is_readonly(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_dry")
    isolated_home = tmp_path / "home_dry"
    isolated_home.mkdir()
    env = isolated_env(repo, isolated_home)
    before = {
        str(path): tree_state(path)
        for path in [isolated_home / ".codex", isolated_home / ".agents", isolated_home / ".claude"]
    }
    result = run_install(repo, env, "--dry-run-agent-sync")

    assert result.returncode == 0, result.stderr
    assert "预览" in result.stdout
    assert not (repo / ".venv").exists()
    assert_no_cli_links(isolated_home)
    for path in [isolated_home / ".codex", isolated_home / ".agents", isolated_home / ".claude"]:
        assert tree_state(path) == before[str(path)]
    assert not (isolated_home / ".agents" / "sdlc" / "backups").exists()


def _assert_install_validation_wrote_nothing(repo: Path, isolated_home: Path) -> None:
    assert not (repo / ".venv").exists()
    assert_no_cli_links(isolated_home)
    assert tree_state(isolated_home / ".codex") is None
    assert tree_state(isolated_home / ".agents") is None
    assert tree_state(isolated_home / ".claude") is None


def test_cli_link_conflict_stops_before_managed_environment_write(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_link_conflict")
    isolated_home = tmp_path / "home_link_conflict"
    conflict = cli_links(isolated_home)["codex-sdlc"]
    conflict.parent.mkdir(parents=True)
    conflict.write_text("其它项目的入口\n", encoding="utf-8")

    result = run_install(
        repo,
        isolated_env(repo, isolated_home),
        "--python",
        sys.executable,
        "--confirm-agent-sync",
    )

    assert result.returncode == 1
    assert "命令入口已存在" in result.stderr
    assert not (repo / ".venv").exists()
    assert not cli_links(isolated_home)["specstamp"].exists()
    assert conflict.read_text(encoding="utf-8") == "其它项目的入口\n"


def test_second_cli_link_creation_failure_rolls_back_first_link(tmp_path: Path, monkeypatch) -> None:
    module = load_install_module()
    home_bin = tmp_path / "home" / ".local" / "bin"
    launchers = tmp_path / "repo" / "bin"
    launchers.mkdir(parents=True)
    primary_launcher = launchers / "specstamp"
    compatible_launcher = launchers / "codex-sdlc"
    primary_launcher.write_text("primary\n", encoding="utf-8")
    compatible_launcher.write_text("compatible\n", encoding="utf-8")
    plan = {
        home_bin / "specstamp": primary_launcher,
        home_bin / "codex-sdlc": compatible_launcher,
    }
    real_symlink_to = Path.symlink_to

    def fail_compatible_link(self: Path, target: Path, target_is_directory: bool = False) -> None:
        if self.name == "codex-sdlc":
            raise OSError("测试第二个入口创建失败")
        real_symlink_to(self, target, target_is_directory=target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", fail_compatible_link)

    assert module.ensure_cli_links(plan) is None
    assert not (home_bin / "specstamp").exists()
    assert not (home_bin / "codex-sdlc").exists()


def test_wheelhouse_without_project_wheel_fails_before_any_write(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_zero_wheel")
    isolated_home = tmp_path / "home_zero_wheel"
    isolated_home.mkdir()
    wheelhouse = tmp_path / "wheelhouse-zero"
    wheelhouse.mkdir()
    result = run_install(
        repo,
        isolated_env(repo, isolated_home),
        "--wheelhouse",
        str(wheelhouse),
        "--confirm-agent-sync",
    )

    assert result.returncode == 1
    assert "项目 wheel 必须恰好有 1 个，实际为 0" in result.stderr
    _assert_install_validation_wrote_nothing(repo, isolated_home)


def test_wheelhouse_with_multiple_project_wheels_fails_before_any_write(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_multiple_wheels")
    isolated_home = tmp_path / "home_multiple_wheels"
    isolated_home.mkdir()
    wheelhouse = tmp_path / "wheelhouse-multiple"
    wheelhouse.mkdir()
    project_wheel = build_project_wheel(repo, wheelhouse)
    shutil.copy2(project_wheel, wheelhouse / project_wheel.name.replace("-py3-", "-2-py3-"))
    result = run_install(
        repo,
        isolated_env(repo, isolated_home),
        "--wheelhouse",
        str(wheelhouse),
        "--confirm-agent-sync",
    )

    assert result.returncode == 1
    assert "项目 wheel 必须恰好有 1 个，实际为 2" in result.stderr
    _assert_install_validation_wrote_nothing(repo, isolated_home)


def test_wheelhouse_with_wrong_project_version_fails_before_any_write(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_wrong_version")
    isolated_home = tmp_path / "home_wrong_version"
    isolated_home.mkdir()
    wheelhouse = tmp_path / "wheelhouse-wrong-version"
    wheelhouse.mkdir()
    project_wheel = build_project_wheel(repo, wheelhouse)
    with zipfile.ZipFile(project_wheel) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    metadata_name = next(name for name in contents if name.endswith(".dist-info/METADATA"))
    contents[metadata_name] = re.sub(
        rb"(?m)^Version: [^\r\n]+$",
        b"Version: 999.0.0",
        contents[metadata_name],
        count=1,
    )
    with zipfile.ZipFile(project_wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in sorted(contents.items()):
            archive.writestr(filename, content)
    result = run_install(
        repo,
        isolated_env(repo, isolated_home),
        "--wheelhouse",
        str(wheelhouse),
        "--confirm-agent-sync",
    )

    assert result.returncode == 1
    assert "项目 wheel 版本必须为" in result.stderr
    assert "实际为 999.0.0" in result.stderr
    _assert_install_validation_wrote_nothing(repo, isolated_home)


def _assert_venv_imports(repo: Path, env: dict[str, str]) -> None:
    python = repo / ".venv" / "bin" / "python"
    pip = repo / ".venv" / "bin" / "pip"
    assert python.is_file()
    assert pip.is_file()
    assert "include-system-site-packages = false" in (repo / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    probe_env = env.copy()
    probe_env.pop("PYTHONPATH", None)
    probe_env.pop("PYTHONHOME", None)
    probe_env["PYTHONNOUSERSITE"] = "1"
    probe_env["PIP_CONFIG_FILE"] = os.devnull
    probe = subprocess.run(
        [
            str(python),
            "-c",
            """
import io
import json
import sys
import attrs
import codex_sdlc
import jsonschema
import jsonschema_specifications
import pip
import pypdf
import referencing
import rpds
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

validator = Draft202012Validator({"type": "object", "required": ["name"]})
try:
    validator.validate({})
except ValidationError:
    jsonschema_negative = True
else:
    jsonschema_negative = False

pdf_data = io.BytesIO()
writer = pypdf.PdfWriter()
writer.add_blank_page(width=72, height=72)
writer.write(pdf_data)
pdf_data.seek(0)
pdf_page_count = len(pypdf.PdfReader(pdf_data).pages)

print(json.dumps({
    "executable": sys.executable,
    "codex_sdlc": codex_sdlc.__file__,
    "jsonschema": jsonschema.__file__,
    "pip": pip.__file__,
    "pypdf": pypdf.__file__,
    "attrs": attrs.__file__,
    "jsonschema_specifications": jsonschema_specifications.__file__,
    "referencing": referencing.__file__,
    "rpds": rpds.__file__,
    "jsonschema_negative": jsonschema_negative,
    "pdf_page_count": pdf_page_count,
}))
""",
        ],
        cwd=repo,
        env=probe_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert probe.returncode == 0, probe.stderr
    data = json.loads(probe.stdout)
    venv_root = (repo / ".venv").resolve()
    for key in [
        "executable",
        "codex_sdlc",
        "jsonschema",
        "pip",
        "pypdf",
        "attrs",
        "jsonschema_specifications",
        "referencing",
        "rpds",
    ]:
        reported = Path(data[key]).absolute() if key == "executable" else Path(data[key]).resolve()
        assert reported.is_relative_to(venv_root), data
    assert data["jsonschema_negative"] is True
    assert data["pdf_page_count"] == 1


def test_install_confirm_creates_all_with_real_offline_wheelhouse(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_confirm")
    isolated_home = tmp_path / "home_confirm"
    isolated_home.mkdir()
    wheelhouse = build_offline_wheelhouse(tmp_path, repo)
    claude_commands = isolated_home / ".claude" / "commands"
    claude_commands.mkdir(parents=True)
    stale_command = claude_commands / "sdlc-old-stale.md"
    stale_command.write_text("old stale", encoding="utf-8")
    unrelated_command = claude_commands / "unrelated.md"
    unrelated_command.write_text("unrelated should stay", encoding="utf-8")
    expired_skill = isolated_home / ".codex" / "skills" / "sdlc-expired"
    expired_skill.mkdir(parents=True)
    (expired_skill / "SKILL.md").write_text("old", encoding="utf-8")
    real_before = {
        str(path): tree_state(path)
        for path in [Path.home() / ".codex" / "skills", Path.home() / ".agents" / "sdlc", Path.home() / ".claude"]
    }
    env = isolated_env(repo, isolated_home)
    malicious_path = tmp_path / "malicious-pythonpath"
    for package in ["codex_sdlc", "jsonschema", "pypdf"]:
        package_dir = malicious_path / package
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "__init__.py").write_text(
            f"raise RuntimeError('错误地导入了恶意 PYTHONPATH 中的 {package}')\n",
            encoding="utf-8",
        )
    env["PYTHONPATH"] = str(malicious_path)
    env["PATH"] = os.pathsep.join([str(isolated_home / ".local" / "bin"), str(repo / "bin"), env.get("PATH", "")])
    result = run_install(repo, env, "--python", sys.executable, "--wheelhouse", str(wheelhouse), "--confirm-agent-sync")

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "预览" in result.stdout
    _assert_venv_imports(repo, env)
    links = cli_links(isolated_home)
    assert links["specstamp"].is_symlink()
    assert links["specstamp"].resolve() == (repo / "bin" / "specstamp").resolve()
    assert links["codex-sdlc"].is_symlink()
    assert links["codex-sdlc"].resolve() == (repo / "bin" / "codex-sdlc").resolve()
    clean_cli_env = env.copy()
    clean_cli_env.pop("PYTHONPATH", None)
    clean_cli_env.pop("PYTHONHOME", None)
    for command in [["--help"], ["version"], ["doctor-install"]]:
        check = subprocess.run(
            [str(links["specstamp"]), *command],
            cwd=repo,
            env=clean_cli_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT,
        )
        assert check.returncode == 0, f"{command}: {check.stdout}\n{check.stderr}"
    compatible_version = subprocess.run(
        [str(links["codex-sdlc"]), "version"],
        cwd=repo,
        env=clean_cli_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SUBPROCESS_TIMEOUT,
    )
    assert compatible_version.returncode == 0, compatible_version.stderr

    standard_skill = isolated_home / ".agents" / "sdlc" / "skills" / "sdlc-agent-sync"
    codex_skill = isolated_home / ".codex" / "skills" / "sdlc-agent-sync"
    claude_command = isolated_home / ".claude" / "commands" / "sdlc-agent-sync.md"
    assert (standard_skill / "SKILL.md").is_file()
    assert (codex_skill / "SKILL.md").is_file()
    assert "description:" in claude_command.read_text(encoding="utf-8")
    assert not stale_command.exists()
    assert unrelated_command.read_text(encoding="utf-8") == "unrelated should stay"
    assert not expired_skill.exists()
    claude_md = isolated_home / ".claude" / "CLAUDE.md"
    manifest = isolated_home / ".agents" / "sdlc" / "manifest.json"
    assert "<!-- SDLC-SYNC:START -->" in claude_md.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.lstat(claude_md).st_mode) == 0o644
    assert json.loads(manifest.read_text(encoding="utf-8"))["schema"] == "codex-sdlc.agent-sync.v1"

    backups = sorted((isolated_home / ".agents" / "sdlc" / "backups").glob("agent-sync-*"))
    assert len(backups) == 1
    assert list((backups[0] / "targets").iterdir())
    assert not any((backups[0] / label).exists() for label in ["standard-skills", "codex-skills", "agents-skills", "claude-commands"])
    for path in [Path.home() / ".codex" / "skills", Path.home() / ".agents" / "sdlc", Path.home() / ".claude"]:
        assert tree_state(path) == real_before[str(path)]


def test_install_failure_after_real_skill_and_command_delete_restores_everything(tmp_path: Path) -> None:
    repo = copy_minimal_repo(tmp_path / "repo_fail")
    isolated_home = tmp_path / "home_fail"
    isolated_home.mkdir()
    wheelhouse = build_offline_wheelhouse(tmp_path, repo)

    agent_home = isolated_home / ".agents" / "sdlc"
    standard_skill = agent_home / "skills" / "sdlc-agent-sync"
    standard_skill.mkdir(parents=True)
    (standard_skill / "SKILL.md").write_text("old standard skill", encoding="utf-8")
    standard_skill.chmod(0o751)
    manifest = agent_home / "manifest.json"
    manifest.write_text('{"old": "manifest"}\n', encoding="utf-8")
    manifest.chmod(0o640)

    codex_skill = isolated_home / ".codex" / "skills" / "sdlc-agent-sync"
    codex_skill.mkdir(parents=True)
    (codex_skill / "SKILL.md").write_text("old codex skill", encoding="utf-8")
    codex_skill.chmod(0o705)

    claude_home = isolated_home / ".claude"
    claude_commands = claude_home / "commands"
    claude_commands.mkdir(parents=True)
    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_text("old claude rules\n", encoding="utf-8")
    claude_md.chmod(0o604)
    stale_command = claude_commands / "sdlc-old-stale.md"
    stale_command.write_text("old stale command", encoding="utf-8")
    stale_command.chmod(0o601)
    unrelated_command = claude_commands / "keep.md"
    unrelated_command.write_text("keep this command", encoding="utf-8")
    unrelated_command.chmod(0o640)

    roots = [agent_home, isolated_home / ".codex", isolated_home / ".agents" / "skills", claude_home]
    before = {str(path): tree_state(path) for path in roots}
    env = isolated_env(
        repo,
        isolated_home,
        {"CODEX_SDLC_AGENT_SYNC_FAIL_POINT": "after_stale_command"},
    )
    result = run_install(repo, env, "--python", sys.executable, "--wheelhouse", str(wheelhouse), "--confirm-agent-sync")

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "同步失败已回滚" in output
    assert_no_cli_links(isolated_home)
    for path in roots:
        assert tree_state(path) == before[str(path)], path
    assert not (agent_home / "backups").exists(), "原不存在的 backup_home 不应残留"
    assert not (isolated_home / ".agents" / "skills").exists(), "原不存在的共享技能父目录不应残留"
