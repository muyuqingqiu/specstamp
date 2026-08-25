from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from codex_sdlc import __version__
from codex_sdlc.core.command_registry import EXTRA_SDLC_SKILL_NAMES, SKILL_COMMAND_NAMES


@dataclass(frozen=True)
class InstallContext:
    sdlc_home: Path
    skills_home: Path

    @property
    def bin_dir(self) -> Path:
        return self.sdlc_home / "bin"

    @property
    def cli_script(self) -> Path:
        return self.bin_dir / "codex-sdlc"

    @property
    def src_dir(self) -> Path:
        return self.sdlc_home / "src"


def get_install_context() -> InstallContext:
    home = Path(os.environ.get("CODEX_SDLC_HOME", "~/.codex/sdlc")).expanduser()
    skills_home = Path(os.environ.get("CODEX_SKILLS_HOME", "~/.codex/skills")).expanduser()
    return InstallContext(sdlc_home=home, skills_home=skills_home)


def unique_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def expected_skill_names(*, include_internal: bool = False) -> list[str]:
    names = [f"sdlc-{name}" for name in SKILL_COMMAND_NAMES] + EXTRA_SDLC_SKILL_NAMES
    return unique_names(names)


def version_text() -> str:
    return f"specstamp {__version__}"
