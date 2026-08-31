**English** · [简体中文](zh-CN/快速开始.md)

# Quick Start

Goal: complete your first SpecStamp run in about ten minutes.

## Requirements

- macOS, Linux, or another POSIX system
- Python 3.10 or newer
- Network access to install `specstamp`, `jsonschema`, and `pypdf`

## 1. Install from PyPI

Create a temporary virtual environment so the first run does not write to global Agent directories:

```bash
python3 -m venv /tmp/specstamp-demo/venv
/tmp/specstamp-demo/venv/bin/pip install specstamp
```

Everything in this example stays under `/tmp/specstamp-demo/`.

## 2. Initialize a project

```bash
mkdir -p /tmp/specstamp-demo/project
cd /tmp/specstamp-demo/project
/tmp/specstamp-demo/venv/bin/specstamp init-plain
```

Use `specstamp init` inside a Git repository. Initialization creates `.codex-sdlc/` and project support files; it does not create requirements or tasks automatically.

## 3. Ask for the next step

```bash
/tmp/specstamp-demo/venv/bin/specstamp next
```

`next` recommends one formal next step based on current project state. Use `specstamp status` for a broader status summary.

## Record a requirement

Requirements and decisions are stored as structured JSON rather than free-form CLI text. A coding agent normally turns the conversation into the required JSON, then imports it:

```bash
/tmp/specstamp-demo/venv/bin/specstamp discuss --file requirement-record.json
```

Capture an intermediate conclusion with:

```bash
/tmp/specstamp-demo/venv/bin/specstamp capture --file decision-record.json
```

## Synchronize Agent skills

Preview all global changes first:

```bash
/tmp/specstamp-demo/venv/bin/specstamp agent-sync --dry-run
```

When the plan is correct, synchronize and verify managed entries:

```bash
/tmp/specstamp-demo/venv/bin/specstamp agent-sync --confirm
/tmp/specstamp-demo/venv/bin/specstamp agent-sync --check
```

This can write managed entries to Codex, shared Agent, and Claude Code directories. The preview command remains read-only.

## Verify the installation

```bash
/tmp/specstamp-demo/venv/bin/specstamp --help
/tmp/specstamp-demo/venv/bin/specstamp version
/tmp/specstamp-demo/venv/bin/specstamp doctor-install
```

`doctor-install` checks command entry points, dependencies, and Agent skill sources without modifying them.

## Managed source installation

Contributors or users who want the repository-managed `.venv` and `$HOME/.local/bin` links can clone the repository and run its installer:

```bash
git clone https://github.com/muyuqingqiu/specstamp.git
cd specstamp
python3 scripts/install_specstamp.py --dry-run-agent-sync
python3 scripts/install_specstamp.py --confirm-agent-sync
export PATH="$HOME/.local/bin:$PATH"
```

## Cleanup

- Project data: preview with `specstamp clean`, then explicitly run `specstamp clean-confirm`.
- Global installation: remove only the managed command links and entries created by the installer or `agent-sync`; do not delete entire Codex, Agent, or Claude directories.

## Common installation problems

- Command not found: ensure the virtual environment or `$HOME/.local/bin` is on `PATH`.
- Managed environment missing: rerun the repository installer.
- Invalid `CODEX_SDLC_PYTHON`: run `unset CODEX_SDLC_PYTHON`, then reinstall or use the virtual environment command directly.
