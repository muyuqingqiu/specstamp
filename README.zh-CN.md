<p align="right"><a href="README.md">English</a> · <strong>简体中文</strong></p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/assets/brand/specstamp-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/assets/brand/specstamp-logo.svg" alt="SpecStamp" width="640">
  </picture>
</p>

<p align="center">
  <strong>让 AI 编码不再只靠聊天记录。</strong><br>
  从需求到验收，全程保存在本机，可检查、可恢复、可追溯。
</p>

<p align="center">
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/ci.yml"><img alt="CI 状态" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/codeql.yml"><img alt="CodeQL 状态" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/codeql.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/actions/workflows/full-tests.yml"><img alt="全量测试状态" src="https://github.com/muyuqingqiu/specstamp/actions/workflows/full-tests.yml/badge.svg?branch=main"></a>
  <a href="https://codecov.io/gh/muyuqingqiu/specstamp"><img alt="代码覆盖率" src="https://codecov.io/gh/muyuqingqiu/specstamp/branch/main/graph/badge.svg"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/blob/main/pyproject.toml"><img alt="Python 3.10 至 3.13" src="https://img.shields.io/badge/Python-3.10--3.13-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/blob/main/LICENSE"><img alt="Apache 2.0 许可证" src="https://img.shields.io/github/license/muyuqingqiu/specstamp?color=F5B942"></a>
  <a href="https://github.com/muyuqingqiu/specstamp/discussions"><img alt="GitHub Discussions" src="https://img.shields.io/badge/GitHub-Discussions-8250DF?logo=github&logoColor=white"></a>
</p>

<p align="center">
  <a href="#快速体验">快速体验</a> ·
  <a href="#核心能力">核心能力</a> ·
  <a href="#工作流程">工作流程</a> ·
  <a href="https://muyuqingqiu.github.io/specstamp/">在线文档</a> ·
  <a href="#参与项目">参与项目</a>
</p>

# SpecStamp：本机优先的 AI 编码工作流

SpecStamp 是一个使用 Python 构建的本机优先（local-first）CLI，为 Codex、Claude Code 等 AI coding agent 提供 spec-driven development 工作流。它把需求管理、技术设计、任务执行、变更管理和测试验收证据保存在项目中，让开发过程不依赖某一次对话或某一个 Agent。

项目通过 `specstamp` 命令和配套的 `sdlc-*` Agent 技能提供能力。为兼容已有使用方式，同时保留功能一致的 `codex-sdlc` 命令。

> **当前状态：** 项目处于 Beta 阶段，支持 Python 3.10～3.13 和 macOS、Linux 等 POSIX 系统。可通过 PyPI 安装：`pip install specstamp`。

## 为什么需要 SpecStamp

| 常见问题 | 只依赖聊天记录 | 使用 SpecStamp |
| --- | --- | --- |
| 换模型或重开会话 | 上下文容易断开 | 项目事实从结构化文件恢复 |
| 需求中途变化 | 设计、任务和验收容易不同步 | 通过显式变更生成新的生效版本 |
| Agent 声称任务完成 | 很难确认实际做了什么 | 完成前必须登记命令、退出码和证据哈希 |
| 资料散落各处 | 需求、代码和验收串不起来 | 从原始资料到验收结果保留完整引用关系 |

## 核心能力

- **本机优先**：资料和状态保存在项目的 `.codex-sdlc/` 目录，不依赖云端账号，也不会自动上传。
- **跨 Agent 使用**：同一套技能可以同步到 Codex、Claude Code 和通用 Agent 入口，换工具不换流程。
- **过程可追溯**：原始资料、讨论结论、设计、任务和变更按编号落盘，并保留引用和 SHA-256 哈希。
- **完成有证据**：任务完成前必须登记测试命令、整数退出码、来源文件、证据哈希和人工验收结果。
- **可恢复推进**：换会话、切分支或暂停任务后，可以从正式状态和备份继续，而不是重新解释整个项目。

## 适合谁

- 使用 Codex、Claude Code 或其他 AI 编码 Agent 的个人开发者和小型团队。
- 希望落地 spec-driven development、requirements management 或可审计开发流程的项目。
- 需要把需求、设计、任务、测试和验收串成一条可复核记录的长期项目。

SpecStamp 目前不适合 Windows 环境，也不提供云端多人实时协作。

## 快速体验

<p align="center">
  <img src="https://raw.githubusercontent.com/muyuqingqiu/specstamp/main/docs/%E8%B5%84%E6%BA%90/%E5%BF%AB%E9%80%9F%E4%BD%93%E9%AA%8C.gif" alt="SpecStamp 初始化项目、归档需求资料并给出下一步建议的真实终端演示" width="920">
</p>

下面的命令会把源码、虚拟环境和示例项目都放进临时目录，不写 Agent 全局目录：

```bash
DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/specstamp-demo.XXXXXX")"
git clone --depth 1 https://github.com/muyuqingqiu/specstamp.git "$DEMO_DIR/source"
python3 -m venv "$DEMO_DIR/venv"
"$DEMO_DIR/venv/bin/pip" install "$DEMO_DIR/source"
mkdir "$DEMO_DIR/project"
cd "$DEMO_DIR/project"
"$DEMO_DIR/venv/bin/specstamp" init-plain
"$DEMO_DIR/venv/bin/specstamp" next
```

安装时需要联网获取运行依赖 `jsonschema` 和 `pypdf`。初始化后执行 `next`，SpecStamp 会根据当前正式状态告诉你最推荐的下一步。

需要在真实项目中使用并同步 Agent 入口时，请继续阅读 [快速开始](https://muyuqingqiu.github.io/specstamp/zh-CN/%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B/)。完整安装前可以先运行 `--dry-run-agent-sync` 查看会写入哪些位置。

仓库还提供一个会保留运行结果、便于逐项查看的 [最小可运行示例](https://github.com/muyuqingqiu/specstamp/blob/main/examples/%E6%9C%80%E5%B0%8F%E9%A1%B9%E7%9B%AE/%E7%A4%BA%E4%BE%8B%E8%AF%B4%E6%98%8E.md)。

## 工作流程

```text
需求想法 → 归档原始资料 → 讨论并确认需求 → 完成整体设计 → 正式建档
→ 拆分任务 → 开发并登记证据 → 完成验收 → 后续变化走显式变更
```

| 阶段 | 常用命令 | 结果 |
| --- | --- | --- |
| 初始化与判断下一步 | `init`、`init-plain`、`status`、`next` | 建立项目状态并给出当前主推荐 |
| 需求资料与讨论 | `material`、`discuss`、`capture`、`grill` | 保存原始资料、结构化需求和关键决定 |
| 设计与正式建档 | `design`、`draft`、`start --file` | 形成经过确认的需求和设计版本 |
| 任务计划与执行 | `tasks`、`plan`、`task` | 建立任务边界、依赖、测试和验收要求 |
| 证据与完成 | `task-evidence`、`task-done`、`regression` | 用真实退出码和证据完成任务与需求验证 |
| 变化与恢复 | `change-*`、`backup`、`restore`、`handoff` | 显式处理变化并从中断处继续 |

## 支持的 Agent

- **Codex**：通过 `agent-sync` 同步 `sdlc-*` 技能。
- **Claude Code**：通过 `agent-sync` 同步 `/sdlc-*` 命令。
- **通用 Agent**：通过 `agent-sync` 同步共享技能入口。

```bash
specstamp agent-sync --dry-run   # 只读预览
specstamp agent-sync --confirm   # 确认后写入全局目录
specstamp agent-sync --check     # 同步后只读复核
```

## 设计理念

文件比聊天记录可靠，证据比 Agent 自述可靠，显式变更比悄悄漂移可靠。Agent 可以更换，项目事实不能丢；小步推进，每一步都可以检查和恢复；人负责决定，Agent 负责整理和执行。

详见 [理念与灵感](docs/zh-CN/理念与灵感.md)。

## 文档入口

- [中文在线文档](https://muyuqingqiu.github.io/specstamp/zh-CN/)：带导航和搜索的完整中文文档。
- [快速开始](https://muyuqingqiu.github.io/specstamp/zh-CN/%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B/)：十分钟完成首次体验和 Agent 入口同步。
- [使用指南](https://muyuqingqiu.github.io/specstamp/zh-CN/%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97/)：按需求、设计、任务、变更、备份等场景查看命令。
- [常见问题](https://muyuqingqiu.github.io/specstamp/zh-CN/%E5%B8%B8%E8%A7%81%E9%97%AE%E9%A2%98/)：系统支持、数据位置、安装和卸载说明。
- [理念与灵感](https://muyuqingqiu.github.io/specstamp/zh-CN/%E7%90%86%E5%BF%B5%E4%B8%8E%E7%81%B5%E6%84%9F/)：项目背后的设计原则与适用边界。
- [质量与安全](https://muyuqingqiu.github.io/specstamp/zh-CN/%E8%B4%A8%E9%87%8F%E4%B8%8E%E5%AE%89%E5%85%A8/)：查看自动测试、覆盖率、依赖更新和安全扫描范围。

## 参与项目

- 有使用问题、流程想法或经验想分享，请到 [GitHub Discussions](https://github.com/muyuqingqiu/specstamp/discussions)。
- 发现可以稳定复现的问题，请提交 [Issue](https://github.com/muyuqingqiu/specstamp/issues/new/choose)。
- 准备贡献代码前，请阅读 [参与贡献](CONTRIBUTING.zh-CN.md) 和 [社区行为准则](CODE_OF_CONDUCT.zh-CN.md)。
- 涉及安全影响的问题，请使用 [私密漏洞报告](https://github.com/muyuqingqiu/specstamp/security/advisories/new)。

如果 SpecStamp 的方法对你的 AI 编码流程有帮助，欢迎 Star 项目、分享使用场景或参与讨论。

## 许可证

SpecStamp 使用 [Apache-2.0 许可证](LICENSE)。版本变化见 [变更记录](CHANGELOG.zh-CN.md)。
