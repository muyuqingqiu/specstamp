**English** · [简体中文](zh-CN/质量与安全.md)

# Quality and Security

SpecStamp separates fast pull-request checks, the scheduled full test suite, and security scanning. This keeps normal contributions responsive while preserving complete test and coverage evidence.

## Standard CI

Every push and pull request builds release artifacts on Python 3.10–3.13 and installs the wheel in a clean virtual environment. CI checks:

- the primary `specstamp` command;
- the compatible `codex-sdlc` command;
- package imports and bundled Schema loading;
- initialization outside a Git repository;
- public installation and Agent synchronization tests on Python 3.12 and 3.13.

Python 3.10 and 3.11 currently validate builds and clean installation; they do not run the full pytest suite.

## Full test suite and coverage

The **Full test suite and coverage** workflow runs manually, on version tags, and every Monday. It splits every repository `test_*.py` file across four parallel jobs, then combines coverage only after every shard succeeds.

Published evidence includes:

- the terminal coverage summary;
- `coverage.xml`;
- a browsable HTML report;
- historical trends on [Codecov](https://codecov.io/gh/muyuqingqiu/specstamp).

The README coverage badge represents this full workflow rather than a small smoke-test subset.

## Dependency updates

Dependabot checks Python dependencies and GitHub Actions every week. Runtime dependencies, development dependencies, and Actions are grouped separately. Security updates still pass the existing CI before they are merged.

## Security scanning and reporting

CodeQL scans Python on pushes to `main`, pull requests, and a weekly schedule with extended security queries. Report vulnerabilities, exposed secrets, arbitrary file access, data-integrity risks, or command-execution issues through [private vulnerability reporting](https://github.com/muyuqingqiu/specstamp/security/advisories/new), not a public issue.

## Branch protection

Force pushes and deletion are disabled for `main`. Normal contributions use pull requests, pass required checks, resolve review discussions, and receive maintainer approval. Maintainers retain an administrative path for first releases and emergency fixes.
