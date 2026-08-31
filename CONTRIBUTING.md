**English** · [简体中文](CONTRIBUTING.zh-CN.md)

# Contributing to SpecStamp

Thank you for your interest in SpecStamp.

## Choose the right channel

- Usage questions, workflow ideas, and experience sharing: use [GitHub Discussions](https://github.com/muyuqingqiu/specstamp/discussions).
- Reproducible feature, installation, or documentation problems: open an [Issue](https://github.com/muyuqingqiu/specstamp/issues/new/choose).
- Security-sensitive problems: use [private vulnerability reporting](https://github.com/muyuqingqiu/specstamp/security/advisories/new).

First-time contributors can start with issues labeled [`good first issue`](https://github.com/muyuqingqiu/specstamp/labels/good%20first%20issue) or [`help wanted`](https://github.com/muyuqingqiu/specstamp/labels/help%20wanted).

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
```

To generate a local coverage report:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q \
  --cov=codex_sdlc \
  --cov-report=term-missing \
  --cov-report=html
```

The full suite takes longer. GitHub Actions splits it across four jobs on weekly and manual runs, then combines coverage. Normal pull requests run release-build, clean-install, and public-entry checks.

Do not commit generated `.venv`, `.codex-sdlc`, temporary, or backup directories.

## Submit a change

- Keep each commit focused on one clear goal.
- Add or update relevant tests and documentation when behavior changes.
- Explain compatibility effects when changing Schemas, CLI contracts, or Agent skills.
- Do not submit credentials, local absolute paths, internal task evidence, customer data, or private project material.
- Run the checks relevant to your change before opening a pull request.

A pull request should explain:

1. the problem it solves;
2. affected modules, commands, or Schemas;
3. commands run and their exit codes;
4. user-visible behavior changes;
5. related documentation, skill, or changelog updates.

## Contribution license

By submitting a pull request, you confirm that you have the right to contribute the material and agree to license it under the repository license.
