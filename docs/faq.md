**English** · [简体中文](zh-CN/常见问题.md)

# FAQ

## Where does SpecStamp store project data?

Requirements, designs, tasks, evidence, and runtime state live under `.codex-sdlc/` in the project. Backup snapshots default to `$HOME/.codex/sdlc/backups`. SpecStamp does not require a cloud account and does not upload this data automatically.

## Which systems are supported?

macOS, Linux, and other POSIX systems are supported. The current implementation depends on `fcntl` and POSIX shell behavior, so Windows is not supported.

## Does SpecStamp provide real-time cloud collaboration?

No. It is a local-first tool for individual developers and small teams. Teams can use their own repository and file synchronization practices around the generated project data.

## How does it work with Codex and Claude Code?

`agent-sync` synchronizes one managed skill source to:

- Codex `sdlc-*` skills;
- Claude Code `/sdlc-*` commands;
- shared Agent skill directories.

Preview with `specstamp agent-sync --dry-run`, write with `--confirm`, and verify with `--check`.

## How do I adopt it in an existing project?

1. Run `specstamp init` from the repository root.
2. Archive original requirements, technical plans, designs, and screenshots with `specstamp material`.
3. Import structured decisions with `specstamp discuss --file`, then follow `specstamp next`.

## How does a new Agent or session recover context?

Formal facts remain in `.codex-sdlc/`. Start with `specstamp status` and `specstamp next`. Identity checks prevent state from being silently reused across the wrong branch or worktree; backups can restore a matching snapshot when needed.

## When is a task complete?

A task must include reproducible evidence: commands, integer exit codes, results, source files, and original SHA-256 values. `task-done` refuses completion when required evidence or acceptance checks are missing.

## What happens when a requirement changes?

Use the explicit change flow: plan the change, create a workspace, submit the projected package, protect affected tasks, review it, and accept a new effective version. Existing tasks do not drift silently.

## What should I do when installation fails?

Run `specstamp doctor-install` first. Common fixes include:

- add the installation directory to `PATH`;
- recreate the managed environment;
- `unset CODEX_SDLC_PYTHON` when it points to an unavailable interpreter.

## How do I uninstall SpecStamp?

- Project state: preview with `specstamp clean`, then explicitly use `specstamp clean-confirm`.
- Global entries: remove only the command links and managed skill entries created by the installer or `agent-sync`. Do not delete entire Codex, shared Agent, or Claude directories.

## Does the temporary quick start write global files?

No. The temporary virtual environment and project remain under `/tmp`. Global Agent directories are written only after `scripts/install_specstamp.py --confirm-agent-sync` or `specstamp agent-sync --confirm`.

## Is `codex-sdlc` still supported?

Yes. `specstamp` is the primary command, while `codex-sdlc` remains a compatible command with the same behavior.
