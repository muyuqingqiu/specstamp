**English** · [简体中文](zh-CN/使用指南.md)

# User Guide

This guide groups SpecStamp commands by common development scenarios. Examples use the primary `specstamp` command. The compatible `codex-sdlc` command provides the same behavior.

## Start a new project

```bash
specstamp init
```

Use `specstamp init-plain` outside a Git repository.

## Bring an existing project into the workflow

1. Run `specstamp init` from the project root.
2. Follow `specstamp next` to create or identify the current DRAFT.
3. Archive original material with `specstamp material DRAFT-001 --title "Requirements" --type requirement --file requirements.md`.
4. Import structured discussion results with `specstamp discuss --file discussion.json`.

Archive existing requirements, technical plans, designs, and screenshots before splitting work. SpecStamp preserves original content and hashes.

## Requirements and design

- `specstamp discuss --file discussion.json`: import a structured requirement discussion.
- `specstamp capture --file decision.json`: record an intermediate decision.
- `specstamp grill`: record controlled questions and answers for requirements, design, planning, or execution.
- `specstamp design`: import a source reference for a technical design.
- `specstamp design-summary DRAFT-001 --file design-summary.v1.json`: import the integrated design summary.
- `specstamp design-plan DRAFT-001 --file design-plan.v1.json`: import the development design plan.
- `specstamp design-artifact DRAFT-001 --file design-artifact.v1.json`: import a modular design artifact.
- `specstamp draft`: write, inspect, or refresh a DRAFT package.
- `specstamp start --file formal.v3.json`: create the formal requirement version.

A coding agent generates structured JSON; the CLI validates, identifies, and stores it deterministically.

## Task planning and execution

- `specstamp tasks REQ-001 --plan-file task-plan.v2.json --tasks-dir tasks --coverage-file task-coverage.v1.json`: import a task plan and task contracts.
- `specstamp plan REQ-001`: complete or adjust the task plan.
- `specstamp plan-add-task REQ-001 "Task title"`: append a task.
- `specstamp plan-amend-task REQ-001 T-001`: amend an unfinished task.
- `specstamp plan-reorder REQ-001 T-002,T-001`: reorder pending tasks.
- `specstamp plan-depends REQ-001 T-003:T-001,T-002`: set task dependencies.
- `specstamp plan-close REQ-001 T-001,T-002`: close tasks that will not be executed.
- `specstamp task REQ-001 T-001`: start or continue a task.
- `specstamp task-read-confirm REQ-001 T-001 --manifest-sha256 <manifest-hash>`: confirm the complete task input was read.
- `specstamp task-run-check REQ-001 T-001`: verify the task baseline and allowed output scope.
- `specstamp task-evidence REQ-001 T-001 --kind test --source-file result.txt --sha256 <evidence-hash> --command "pytest -q" --exit-code 0 --result passed`: register test or acceptance evidence.
- `specstamp task-done REQ-001 T-001`: finish a task after automatic evidence checks.
- `specstamp task-restore REQ-001 T-001`: reopen work from structured feedback.
- `specstamp task-pause REQ-001 T-001`: pause an active task.
- `specstamp fix`: add a repair task for completed work.
- `specstamp audit`: add a quality-review task for completed work.
- `specstamp regression REQ-001`: perform requirement-level regression verification.

Completion evidence includes the command, integer exit code, result, source file, and original SHA-256 value.

## Handle requirement changes

- `specstamp change-plan REQ-001`: plan a requirement change.
- `specstamp change-create REQ-001 --request-key <stable-key>`: create a change workspace.
- `specstamp change-package REQ-001 CHG-001 ...`: validate and submit the complete projected change package.
- `specstamp change-protect REQ-001 CHG-001`: review the change and protect affected active tasks.
- `specstamp change-accept REQ-001 CHG-001`: apply the reviewed change as a new effective version.
- `specstamp review create`, `review submit`, `review status`: create, submit, and inspect independent reviews.

## Backup and restore

- `specstamp backup`: back up the project or a requirement.
- `specstamp backup-list`: list matching backup candidates.
- `specstamp backup-clean`: remove old backups according to the requested retention count.
- `specstamp restore --dry-run`: preview restoration; use `--select` to list candidates and `--confirm` to apply the chosen plan.

## Synchronize Agent entry points

```bash
specstamp agent-sync --dry-run
specstamp agent-sync --confirm
specstamp agent-sync --check
```

The confirmed operation synchronizes managed Codex, shared Agent, and Claude Code entries. Always inspect the preview and verify the result.

## Status, handoff, and export

- `specstamp status`: show current project state.
- `specstamp next`: return one recommended next step.
- `specstamp export`: export project delivery records.
- `specstamp export-requirement REQ-001`: export one requirement's records.
- `specstamp finish`: generate the formal handoff for the current work session.
- `specstamp handoff`: print a prompt that can continue the work in another session.
- `specstamp docs REQ-001`: generate requirement maintenance documentation.
- `specstamp accept REQ-001`: accept a completed requirement.

## Diagnostics

- `specstamp doctor`: inspect installation or project health.
- `specstamp doctor-install`: inspect local installation and skill sources.
- `specstamp doctor-repair`: rebuild SQLite and Markdown projections.
- `specstamp doctor-deep`: perform a read-only deep inspection of project state, backups, and code graph status.
