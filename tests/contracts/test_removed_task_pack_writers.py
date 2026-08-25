from __future__ import annotations

import argparse
import ast
import importlib
from pathlib import Path
import pkgutil
import re

import codex_sdlc.commands
from codex_sdlc.cli import build_parser
from codex_sdlc.core.agent_sync import claude_sync_block, discover_skills
from codex_sdlc.core.command_registry import CLI_COMMAND_NAMES, SKILL_COMMAND_NAMES


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "codex_sdlc"
REMOVED_COMMANDS = {"prepare", "brief", "brief-review", "brief-augment"}
REMOVED_SKILLS = {"sdlc-prepare", "sdlc-brief", "sdlc-brief-augment"}
REMOVED_SKILL_DIRECTORIES = {
    ROOT / "skills" / name
    for name in REMOVED_SKILLS
}
REMOVED_WRITER_FUNCTIONS = {
    "augment_task_pack",
    "compact_task_pack_markdown",
    "ensure_task_pack_ready",
    "mark_requirement_task_packs_stale",
    "mark_task_pack_stale",
    "measure_task_pack",
    "rewrite_task_pack_markdown_from_metadata",
    "task_pack_gate_message",
    "task_pack_quality",
    "task_pack_status",
    "task_pack_status_text",
    "task_pack_test_contract",
    "update_task_pack_model_review",
    "write_task_pack",
}
PROHIBITED_IMPORTS = {
    "codex_sdlc.commands.brief_cmd",
    "codex_sdlc.commands.brief_augment_cmd",
    "codex_sdlc.core.task_pack",
    "codex_sdlc.core.task_pack_contract",
}
REMOVED_STAGE_FUNCTIONS = {"run_prepare", "prepare_requirement_for_work"}
RETIRED_STAGE_TEXT = re.compile(
    r"(?i)(?:\$?sdlc-(?:prepare|brief(?:-review|-augment)?)|task-packs?/|"
    r"(?<![\w-])(?:prepare|brief-review|brief-augment|brief)(?![\w-]))"
)
RETIRED_TEXT_READ_ONLY_ALLOWLIST = {
    # Agent 同步必须保留旧技能名作为拒绝清单，但生成的能力正文由单独合同确认不含这些名称。
    "src/codex_sdlc/core/agent_sync.py",
    # backup 只把既有目录收进只读归档，不会创建或改写任务执行包。
    "src/codex_sdlc/core/backup.py",
    "src/codex_sdlc/legacy/task_pack_reader.py",
}


def _parser_commands() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            return set(choices)
    raise AssertionError("CLI 没有找到命令注册表")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _registered_commands_from_each_module() -> dict[str, set[str]]:
    """逐个调用生产命令模块的注册函数，防止总 CLI 事后隐藏仍可执行的旧入口。"""

    registered: dict[str, set[str]] = {}
    for module_info in pkgutil.iter_modules(codex_sdlc.commands.__path__):
        if not module_info.name.endswith("_cmd"):
            continue
        module_name = f"{codex_sdlc.commands.__name__}.{module_info.name}"
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if not callable(register):
            continue
        parser = argparse.ArgumentParser(add_help=False)
        subparsers = parser.add_subparsers(dest="command")
        register(subparsers)
        registered[module_name] = set(subparsers.choices)
    return registered


def _retired_string_constants(path: Path) -> list[tuple[int, str]]:
    """只扫真正进入运行时的字符串常量，普通变量名和历史说明注释不算生产入口。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and RETIRED_STAGE_TEXT.search(node.value)
    ]


def test_cli_registry_and_help_do_not_expose_removed_commands() -> None:
    assert REMOVED_COMMANDS.isdisjoint(_parser_commands())
    assert REMOVED_COMMANDS.isdisjoint(CLI_COMMAND_NAMES)
    assert REMOVED_COMMANDS.isdisjoint(SKILL_COMMAND_NAMES)


def test_retired_skill_sources_and_empty_command_module_are_absent() -> None:
    """拒绝只隐藏命令但继续保留会误导使用者的可调用源文件。"""

    assert [str(path.relative_to(ROOT)) for path in REMOVED_SKILL_DIRECTORIES if path.exists()] == []
    assert not (SOURCE_ROOT / "commands" / "brief_cmd.py").exists()


def test_each_command_module_register_cannot_restore_removed_commands() -> None:
    violations = {
        module_name: sorted(commands & REMOVED_COMMANDS)
        for module_name, commands in _registered_commands_from_each_module().items()
        if commands & REMOVED_COMMANDS
    }
    assert violations == {}


def test_agent_capability_discovery_ignores_retired_stage_skills(tmp_path: Path) -> None:
    for name in ["sdlc-task", *sorted(REMOVED_SKILLS)]:
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f"---\ndescription: {name}\n---\n\n# {name}\n",
            encoding="utf-8",
        )

    entries = discover_skills(tmp_path)
    assert [entry.name for entry in entries] == ["sdlc-task"]

    rules = claude_sync_block()
    for retired in ["/sdlc-prepare", "/sdlc-brief", "brief-review", "task-packs/"]:
        assert retired not in rules


def test_task_pack_writer_review_budget_and_gate_functions_are_gone() -> None:
    task_pack = SOURCE_ROOT / "core" / "task_pack.py"
    assert REMOVED_WRITER_FUNCTIONS.isdisjoint(_defined_functions(task_pack))
    contract = SOURCE_ROOT / "core" / "task_pack_contract.py"
    assert REMOVED_WRITER_FUNCTIONS.isdisjoint(_defined_functions(contract))
    assert not (SOURCE_ROOT / "commands" / "brief_augment_cmd.py").exists()


def test_production_modules_do_not_import_removed_task_pack_writers() -> None:
    violations: dict[str, list[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        imported = _imported_modules(path)
        forbidden = sorted(imported & PROHIBITED_IMPORTS)
        if forbidden:
            violations[path.relative_to(ROOT).as_posix()] = forbidden
    assert violations == {}


def test_removed_stage_functions_and_runtime_text_are_absent_outside_read_only_allowlist() -> None:
    function_violations: dict[str, list[str]] = {}
    text_violations: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        removed_functions = sorted(_defined_functions(path) & REMOVED_STAGE_FUNCTIONS)
        if removed_functions:
            function_violations[relative] = removed_functions
        if relative in RETIRED_TEXT_READ_ONLY_ALLOWLIST:
            continue
        retired_constants = _retired_string_constants(path)
        if retired_constants:
            text_violations[relative] = retired_constants
    assert function_violations == {}
    assert text_violations == {}


def test_legacy_reader_is_the_only_task_pack_archive_parser() -> None:
    legacy_reader = SOURCE_ROOT / "legacy" / "task_pack_reader.py"
    assert legacy_reader.is_file()
    assert "read_legacy_task_pack" in _defined_functions(legacy_reader)

    allowed_readers = {
        "src/codex_sdlc/commands/docs_cmd.py",
        "src/codex_sdlc/core/backup.py",
        "src/codex_sdlc/core/state.py",
        "src/codex_sdlc/legacy/task_pack_reader.py",
    }
    actual_readers: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "codex_sdlc.legacy.task_pack_reader" in text or path == legacy_reader:
            actual_readers.add(path.relative_to(ROOT).as_posix())
    assert actual_readers == allowed_readers
