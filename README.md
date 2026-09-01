<p align="right"><strong>English</strong> · <a href="README.zh-CN.md">简体中文</a></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/assets/brand/specstamp-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/assets/brand/specstamp-logo.svg" alt="SpecStamp" width="640">
  </picture>
</p>

<p align="center">
  <strong>Give AI coding agents a workflow that survives the chat.</strong><br>
  Keep requirements, designs, tasks, changes, and acceptance evidence local, traceable, and recoverable.
</p>

<p align="center">
  <a href="https://pypi.org/project/specstamp/"><img alt="PyPI" src="https://img.shields.io/pypi/v/specstamp?color=F5B942"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/muyuqingqiu/specstamp?display_name=tag&sort=semver&color=F5B942"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/codeql.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/full-tests.yml"><img alt="Full test suite" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/full-tests.yml/badge.svg?branch=main"></a>
  <a href="https://codecov.io/gh/muyuqingqiu/specstamp"><img alt="Coverage" src="https://codecov.io/gh/muyuqingqiu/specstamp/branch/main/graph/badge.svg"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/blob/main/pyproject.toml"><img alt="Python 3.10 to 3.13" src="https://img.shields.io/badge/Python-3.10--3.13-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/blob/main/LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/github/license/muyuqingqiu/specstamp?color=F5B942"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/discussions"><img alt="GitHub Discussions" src="https://img.shields.io/badge/GitHub-Discussions-8250DF?logo=github&logoColor=white"></a>
</p>

<p align="center">
  <a href="#see-it-in-30-seconds">Demo</a> ·
  <a href="#quick-start">Install</a> ·
  <a href="#why-specstamp">Why SpecStamp</a> ·
  <a href="#workflow">Workflow</a> ·
  <a href="https://muyuqingqiu.github.io/specstamp/">Documentation</a> ·
  <a href="#community">Community</a>
</p>

# SpecStamp: a local-first workflow for AI coding agents

SpecStamp is a Python CLI that brings spec-driven development to Codex, Claude Code, and other AI coding agents. It combines an AI agent workflow, requirements management, task planning, change control, and acceptance evidence in structured project files, so development does not depend on one conversation or one agent.

> **One product, different entry points:** SpecStamp is the product. In a terminal, run `specstamp ...`. In Codex, invoke the bundled `$sdlc-*` skills, such as `$sdlc-init` and `$sdlc-next`. In Claude Code, use `/sdlc-*`. The `sdlc-*` skill names and `codex-sdlc` CLI entry point are retained for compatibility; they are part of SpecStamp, not a separate product.

> **Status:** Beta. SpecStamp supports Python 3.10–3.13 on macOS, Linux, and other POSIX systems. Windows is not currently supported.

## See it in 30 seconds

<p align="center">
  <img src="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/docs/%E8%B5%84%E6%BA%90/quick-tour.en.gif" alt="A 30-second English tour of SpecStamp initialization, requirement material archiving, and next-step guidance" width="920">
</p>

The 30-second tour initializes a clean project, preserves the original requirement material, and returns one state-aware next step instead of asking you to reconstruct context in a new chat. Its concise English captions represent the same verified 0.11.0 workflow without changing CLI behavior.

## Quick start

Install SpecStamp from PyPI in a Python 3.10+ virtual environment, then initialize a disposable project:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install specstamp
mkdir specstamp-demo && cd specstamp-demo
specstamp init-plain
specstamp next
```

This trial stays inside `.venv` and `specstamp-demo`; it does not write to global Agent directories. `specstamp next` reads the current formal state and recommends the next workflow step.

Ready to use it with an Agent? Follow the [10-minute Quick Start](https://muyuqingqiu.github.io/specstamp/quick-start/) to synchronize skills for Codex, Claude Code, or shared Agent directories.

## Why SpecStamp

| Common problem | Chat-only workflow | With SpecStamp |
| --- | --- | --- |
| A model or session changes | Context must be explained again | Resume from formal project state |
| Requirements change midway | Design, tasks, and acceptance drift apart | Apply an explicit, versioned change |
| An agent says a task is done | The claim is difficult to verify | Record commands, exit codes, files, and hashes |
| Source material is scattered | Requirements, code, and tests lose their links | Preserve references from source material to acceptance |

## Core capabilities

- **Local first:** project data lives in `.codex-sdlc/`; no cloud account or automatic upload is required.
- **Cross-agent:** synchronize one versioned skill set to Codex, Claude Code, and shared Agent directories.
- **Traceable:** keep source material, decisions, designs, tasks, and changes under stable identifiers with SHA-256 evidence.
- **Evidence-based completion:** require real commands, integer exit codes, source files, hashes, and acceptance results before completion.
- **Recoverable:** continue after a new session, branch switch, pause, or restored backup without reconstructing the project from chat history.

## Who it is for

- Developers and small teams using Codex, Claude Code, or other coding agents.
- Projects adopting spec-driven development, requirements management, or auditable delivery.
- Long-running work that needs requirements, design, tasks, tests, and acceptance to remain connected.

SpecStamp is not a cloud collaboration platform and does not provide real-time multi-user editing.

## Workflow

```text
idea → source material → reviewed requirements → integrated design → formal version
→ task plan → implementation and evidence → acceptance → explicit changes
```

| Stage | Terminal examples | Result |
| --- | --- | --- |
| Initialize and orient | `specstamp init`, `specstamp status`, `specstamp next` | Establish project state and identify the next step |
| Requirements | `specstamp material`, `specstamp discuss`, `specstamp capture` | Preserve source material, structured requirements, and decisions |
| Design and formalize | `specstamp design`, `specstamp draft`, `specstamp start --file` | Create a reviewed requirement and design version |
| Plan and execute | `specstamp tasks`, `specstamp plan`, `specstamp task` | Define task scope, dependencies, tests, and acceptance |
| Verify and finish | `specstamp task-evidence`, `specstamp task-done`, `specstamp regression` | Complete work with reproducible evidence |
| Change and recover | `specstamp change-plan`, `specstamp backup`, `specstamp restore` | Handle change explicitly and resume interrupted work |

## Agent integration

```bash
specstamp agent-sync --dry-run   # preview without writing
specstamp agent-sync --confirm   # synchronize managed entries
specstamp agent-sync --check     # verify the result without writing
```

After synchronization, use the entry point for the client you are currently in:

**In Codex**

```text
$sdlc-init
$sdlc-next
```

**In Claude Code**

```text
/sdlc-init
/sdlc-next
```

The terminal commands, Codex skills, and Claude Code commands all drive the same SpecStamp workflow. Shared Agent directories receive the same managed skill source.

## Design principles

Files are more durable than chat history. Reproducible evidence is more reliable than an agent's claim. Explicit changes are safer than silent drift. Agents may change; project facts should not.

Read [Philosophy and Inspiration](https://muyuqingqiu.github.io/specstamp/philosophy/) for the full rationale and project boundaries.

## Documentation

- [Documentation home](https://muyuqingqiu.github.io/specstamp/)
- [Quick Start](https://muyuqingqiu.github.io/specstamp/quick-start/)
- [User Guide](https://muyuqingqiu.github.io/specstamp/user-guide/)
- [FAQ](https://muyuqingqiu.github.io/specstamp/faq/)
- [Philosophy and Inspiration](https://muyuqingqiu.github.io/specstamp/philosophy/)
- [Quality and Security](https://muyuqingqiu.github.io/specstamp/quality-and-security/)
- [中文文档](https://muyuqingqiu.github.io/specstamp/zh-CN/)

## Community

- Ask usage questions and share workflows in [GitHub Discussions](https://github.com/muyuqingqiu/specstamp/discussions).
- Report reproducible problems through [GitHub Issues](https://github.com/muyuqingqiu/specstamp/issues/new/choose).
- Read the [contribution guide](CONTRIBUTING.md) before opening a pull request.
- Report security-sensitive issues through [private vulnerability reporting](https://github.com/muyuqingqiu/specstamp/security/advisories/new).

If SpecStamp improves your AI coding workflow, consider starring the repository and sharing what you built with it.

## License

SpecStamp is licensed under [Apache-2.0](LICENSE). See the [changelog](CHANGELOG.md) for release history.
