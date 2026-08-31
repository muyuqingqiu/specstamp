**English** · [简体中文](zh-CN/理念与灵感.md)

# Philosophy and Inspiration

## Design principles

### Files are more durable than chat history

Chats can be lost, reordered, or become stale. Project facts should remain in files that any authorized agent or developer can inspect.

### Evidence is more reliable than an agent's claim

"Done" is not evidence. A reproducible command, an integer exit code, and a verifiable file hash provide a standard for acceptance and repair.

### Explicit change is safer than silent drift

Requirement changes are normal. Recording exactly which designs, tasks, and acceptance checks are affected is safer than letting implementation silently diverge.

### Agents may change; project facts should not

Models, tools, and sessions come and go. Requirements, designs, tasks, and evidence should remain available to the next agent.

### Small steps should be inspectable and recoverable

Stable identifiers, hashes, task boundaries, and backups make each step reviewable. A failed step can be restored without discarding the whole project.

### Humans decide; agents organize and execute

AI is effective at understanding natural language, structuring information, and performing repeatable work. People remain responsible for business decisions, integrated approval, and acceptance. The CLI performs deterministic storage, validation, identification, and linking rather than interpreting business meaning on its own.

## Sources of inspiration

- Spec-driven development: implementation follows an explicit specification.
- Documentation-first development: documentation is an input to development, not only an output.
- Composable Unix tools: each command performs one focused operation connected through files and conventions.
- Event records: changes remain ordered, replayable, and inspectable.
- Traceable delivery: every result can point back to its source and decision.

## Boundaries

- Local first: project state does not depend on a hosted SpecStamp account.
- Tool independent: Codex, Claude Code, and other agents can consume the same workflow.
- POSIX only: macOS and Linux are supported; Windows is not.
- Collaboration: SpecStamp does not provide cloud-based real-time editing and is best suited to individual developers and small teams.
