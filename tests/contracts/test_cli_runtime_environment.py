from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "bin" / "specstamp"


def _copy_launcher(tmp_path: Path) -> tuple[Path, Path]:
    """复制正式启动器，避免缺环境测试碰到仓库里真实的受管环境。"""

    isolated_root = tmp_path / "isolated-sdlc"
    isolated_bin = isolated_root / "bin"
    isolated_bin.mkdir(parents=True)
    isolated_launcher = isolated_bin / "specstamp"
    shutil.copy2(LAUNCHER, isolated_launcher)
    isolated_launcher.chmod(isolated_launcher.stat().st_mode | stat.S_IXUSR)
    return isolated_root, isolated_launcher


def _run_launcher(
    launcher: Path,
    *arguments: str,
    python_override: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """用不带激活状态的环境运行启动器，明确控制解释器覆盖值。"""

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("CONDA_PREFIX", None)
    env.pop("CODEX_SDLC_PYTHON", None)
    if python_override is not None:
        env["CODEX_SDLC_PYTHON"] = python_override
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(launcher), *arguments],
        cwd=launcher.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_fake_python(path: Path, marker: Path) -> None:
    """构造只记录调用的解释器，测试选择顺序时不导入真实业务代码。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"printf '%s\\n' \"$*\" >> '{marker}'",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_python_without_site_packages(path: Path) -> None:
    """让真实 Python 执行启动代码，但不加载安装在环境里的第三方依赖。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"exec {shlex.quote(sys.executable)} -S \"$@\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_missing_managed_environment_returns_chinese_install_command(tmp_path: Path) -> None:
    _root, launcher = _copy_launcher(tmp_path)

    result = _run_launcher(launcher, "--help")

    assert result.returncode != 0
    assert "找不到可用的 SpecStamp 受管解释器" in result.stderr
    assert "请运行安装命令" in result.stderr
    assert "scripts/install_specstamp.py" in result.stderr
    assert "Traceback" not in result.stderr


def test_invalid_explicit_interpreter_does_not_fall_back_to_managed_environment(
    tmp_path: Path,
) -> None:
    root, launcher = _copy_launcher(tmp_path)
    managed_marker = tmp_path / "managed-called.txt"
    _write_fake_python(root / ".venv" / "bin" / "python", managed_marker)

    result = _run_launcher(
        launcher,
        "version",
        python_override=str(tmp_path / "missing-python"),
    )

    assert result.returncode != 0
    assert "CODEX_SDLC_PYTHON" in result.stderr
    assert "不可用" in result.stderr
    assert not managed_marker.exists()
    assert "Traceback" not in result.stderr


def test_non_executable_managed_interpreter_is_rejected(tmp_path: Path) -> None:
    root, launcher = _copy_launcher(tmp_path)
    managed_python = root / ".venv" / "bin" / "python"
    managed_python.parent.mkdir(parents=True)
    managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    managed_python.chmod(stat.S_IRUSR | stat.S_IWUSR)

    result = _run_launcher(launcher, "--help")

    assert result.returncode != 0
    assert "找不到可用的 SpecStamp 受管解释器" in result.stderr
    assert "请运行安装命令" in result.stderr
    assert "Traceback" not in result.stderr


def test_explicit_interpreter_has_priority_and_receives_business_arguments(
    tmp_path: Path,
) -> None:
    root, launcher = _copy_launcher(tmp_path)
    explicit_marker = tmp_path / "explicit-called.txt"
    managed_marker = tmp_path / "managed-called.txt"
    explicit_python = tmp_path / "configured" / "python"
    _write_fake_python(explicit_python, explicit_marker)
    _write_fake_python(root / ".venv" / "bin" / "python", managed_marker)

    result = _run_launcher(
        launcher,
        "version",
        python_override=str(explicit_python),
    )

    assert result.returncode == 0
    assert explicit_marker.exists()
    assert "version" in explicit_marker.read_text(encoding="utf-8")
    assert not managed_marker.exists()


def test_missing_dependency_returns_chinese_repair_steps_without_traceback(
    tmp_path: Path,
) -> None:
    root, launcher = _copy_launcher(tmp_path)
    broken_python = root / ".venv" / "bin" / "python"
    _write_python_without_site_packages(broken_python)

    result = _run_launcher(launcher, "--help")

    assert result.returncode != 0
    assert "受管解释器缺少 SpecStamp 运行依赖" in result.stderr
    assert "请运行安装命令" in result.stderr
    assert "Traceback" not in result.stderr


def test_missing_only_pypdf_is_rejected_without_falling_back(
    tmp_path: Path,
) -> None:
    root, launcher = _copy_launcher(tmp_path)
    managed_marker = tmp_path / "managed-called.txt"
    _write_fake_python(root / ".venv" / "bin" / "python", managed_marker)
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "pypdf.py").write_text(
        "raise ModuleNotFoundError('测试环境缺少 pypdf')\n",
        encoding="utf-8",
    )

    result = _run_launcher(
        launcher,
        "version",
        python_override=sys.executable,
        extra_env={
            "PYTHONPATH": os.pathsep.join([str(blocker), str(REPO_ROOT / "src")]),
        },
    )

    assert result.returncode != 0
    assert "受管解释器缺少 SpecStamp 运行依赖" in result.stderr
    assert "请运行安装命令" in result.stderr
    assert not managed_marker.exists()
    assert "Traceback" not in result.stderr


def test_project_declares_runtime_and_development_dependencies() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    runtime_dependencies = document["project"]["dependencies"]
    development_dependencies = document["project"]["optional-dependencies"]["dev"]

    assert any(item.startswith("jsonschema") for item in runtime_dependencies)
    assert any(item.startswith("pypdf") for item in runtime_dependencies)
    assert any(item.startswith("pytest") for item in development_dependencies)


def test_gitignore_is_precise_for_local_install_artifacts() -> None:
    expected_ignored = [
        ".venv/bin/python",
        ".codex-sdlc.pre-restore-20260727/events.jsonl",
        "src/codex_sdlc.egg-info/PKG-INFO",
    ]
    expected_visible = [
        ".venv-backup/bin/python",
        ".codex-sdlc.pre-restore/events.jsonl",
        "tmp/.venv/bin/python",
        "src/codex_sdlc/runtime.py",
    ]

    for relative_path in expected_ignored:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", relative_path],
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 0, relative_path

    for relative_path in expected_visible:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", relative_path],
            cwd=REPO_ROOT,
            check=False,
        )
        assert result.returncode == 1, relative_path


def test_project_declares_primary_and_compatible_cli_entries() -> None:
    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["name"] == "specstamp"
    assert document["project"]["scripts"] == {
        "specstamp": "codex_sdlc.cli:main",
        "codex-sdlc": "codex_sdlc.cli:main",
    }

    primary = REPO_ROOT / "bin" / "specstamp"
    compatible = REPO_ROOT / "bin" / "codex-sdlc"
    assert primary.read_bytes() == compatible.read_bytes()
    assert os.access(primary, os.X_OK)
    assert os.access(compatible, os.X_OK)
