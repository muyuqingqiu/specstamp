<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/specstamp-logo-dark.svg">
    <img src="assets/brand/specstamp-logo.svg" alt="SpecStamp" width="640">
  </picture>
</p>

<p align="center">
  <img alt="Python 3.10 至 3.13" src="https://img.shields.io/badge/Python-3.10--3.13-2F80ED">
  <img alt="支持 macOS 和 Linux" src="https://img.shields.io/badge/平台-macOS%20%7C%20Linux-22C7A9">
  <img alt="Apache 2.0 许可证" src="https://img.shields.io/badge/许可证-Apache--2.0-F5B942">
</p>

# SpecStamp

让 AI 编码不再只靠聊天记录：从需求到验收，全程保存在本机，可检查、可恢复、可追溯。

SpecStamp 是一套本机优先的软件开发流程工具。它把需求、设计、任务、变更和验收证据连成一条可追溯的线，适合个人开发者和小型团队在使用 AI 编码 Agent 时，把项目事实稳定地留在项目里，而不是留在聊天记录里。

SpecStamp 通过 `specstamp` 命令和配套 Agent 技能提供能力。

为兼容已有使用方式，项目同时保留 `codex-sdlc` 命令，两者功能一致。

## 项目价值

- 需求、设计、任务、测试和验收记录保存在同一条流程里，谁都能按顺序看明白。
- 原始资料原样归档并记录哈希，改没改、改了什么有据可查。
- 任务完成必须绑定证据：命令、退出码、文件哈希、人工验收结果。
- 换 Agent、重开会话、切换分支，项目事实不会丢。

## 痛点

- 和 AI 聊天讨论需求，重开会话或换模型后，上下文接不上。
- 需求说着说着就变了，设计、任务、验收没有跟着更新。
- 任务“做完了”却拿不出可复核的证据。
- 需求、设计、任务、测试、验收散落各处，串不成一条线。

## 四项能力

1. **本机优先**：资料和状态保存在项目的 `.codex-sdlc/` 目录，不依赖云端账号。
2. **跨 Agent**：同一套技能可以同步到 Codex、Claude Code 和通用 Agent 入口，换工具不换流程。
3. **过程可追溯**：原始资料、讨论结论、设计、任务和变更都按编号落盘，并保留引用和哈希。
4. **完成有证据**：任务完成前必须登记测试命令、退出码、文件哈希和人工验收。

## 最短快速开始

以下步骤全部在 `/tmp` 隔离目录完成，不写任何全局目录，适合先体验：

```bash
python3 -m venv /tmp/specstamp-demo/venv
/tmp/specstamp-demo/venv/bin/pip install "<项目源码路径>"
mkdir -p /tmp/specstamp-demo/project
cd /tmp/specstamp-demo/project
/tmp/specstamp-demo/venv/bin/specstamp init-plain
/tmp/specstamp-demo/venv/bin/specstamp next
```

把 `<项目源码路径>` 换成你克隆下来的项目目录。安装时需要联网获取运行依赖 `jsonschema`、`pypdf`。

初始化后执行 `next`，工具会告诉你当前最推荐的下一步。

### 完整安装与 Agent 同步（可选）

在项目根目录执行：

```bash
python3 scripts/install_specstamp.py --dry-run-agent-sync
python3 scripts/install_specstamp.py --confirm-agent-sync
export PATH="$HOME/.local/bin:$PATH"
specstamp --help
specstamp version
specstamp doctor-install
```

- `--dry-run-agent-sync` 只读预览，不写文件。
- `--confirm-agent-sync` 是唯一写全局目录的步骤：会创建仓库内的 `.venv`、`$HOME/.local/bin/specstamp` 主入口、`$HOME/.local/bin/codex-sdlc` 兼容入口，并同步 `$HOME/.codex/skills`、`$HOME/.agents/sdlc`、`$HOME/.agents/skills`、`$HOME/.claude/commands` 三套 Agent 入口。写入带事务备份，失败会自动回滚。

## 简化流程

```text
需求想法 → 归档资料 → 讨论确认 → 整体设计 → 正式建档 → 拆任务
→ 开发 → 登记证据 → 完成验收 → 后续变化走变更
```

- 资料归档：`material`
- 需求讨论与确认：`discuss`、`capture`、`grill`
- 设计与建档：`design`、`draft`、`start --file`
- 任务计划与执行：`tasks`、`plan`、`task`
- 证据与完成：`task-evidence`、`task-done`
- 变化管理：`change-plan`、`change-create`、`change-package`、`change-accept`
- 状态与交接：`status`、`next`、`backup`、`restore`、`export`

## 适用范围

- 适合个人开发者和小型团队。
- 支持 macOS 和 Linux 等 POSIX 系统。
- 暂不支持 Windows。
- 数据保存在本机，不具备云端多人实时协作能力。

## 支持的 Agent

- Codex：通过 `agent-sync` 同步 `sdlc-*` 技能。
- Claude Code：通过 `agent-sync` 同步 `/sdlc-*` 命令。
- 通用 Agent：通过 `agent-sync` 同步共享技能。

## 理念

文件比聊天记录可靠，证据比 Agent 自述可靠，显式变更比悄悄漂移可靠。Agent 可以更换，项目事实不能丢；小步推进，每一步都可以检查和恢复；人负责决定，Agent 负责整理和执行。

详见 [理念与灵感](docs/理念与灵感.md)。

## 文档入口

- [快速开始](docs/快速开始.md)：十分钟完成首次体验。
- [使用指南](docs/使用指南.md)：按场景说明常用流程。
- [理念与灵感](docs/理念与灵感.md)：项目背后的设计理念。
- [常见问题](docs/常见问题.md)：常见疑问与解答。

## 贡献和许可证

- [参与贡献](CONTRIBUTING.md)
- [安全问题报告](SECURITY.md)
- [社区行为准则](CODE_OF_CONDUCT.md)
- [变更记录](CHANGELOG.md)
- 本项目采用 [Apache-2.0 许可证](LICENSE)。
