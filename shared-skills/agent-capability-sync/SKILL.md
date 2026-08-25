---
name: agent-capability-sync
description: 维护本机自建 Agent 能力和开发技能的全局同步规则。Use when creating, editing, migrating, auditing, or syncing local Codex skills, Claude commands, agent tools, hooks, or other reusable developer capabilities across Codex, Claude Code, and shared agent directories.
---

# agent-capability-sync

用于创建、修改、迁移或审查本机自建技能、Slash Command、Hook、Agent 工具和可复用开发能力。

执行规则：

1. 先判断能力类型：
   - 本机规则优先级高于 `skill-creator` 的默认路径。`skill-creator` 里提到默认放 `$CODEX_HOME/skills` 或 `~/.codex/skills` 时，在本机不要照搬。
   - `sdlc-*` 属于 SDLC 命令族，版本化来源在 `<SpecStamp 仓库>/skills`，用 `codex-sdlc agent-sync` 同步到 Codex 技能、运行时标准目录和 Claude Commands。
   - 已纳入管理的共享技能版本化来源在 `<SpecStamp 仓库>/shared-skills`，例如本技能 `agent-capability-sync`；改这里，再用 `codex-sdlc agent-sync` 同步到 `$HOME/.agents/skills`。
   - `codex-dev`、`split-development-tasks`、`orchestrate-development-tasks`、`assist-milestone-acceptance`、`assist-code-review` 和 `module-architecture-guide` 使用独立的Codex项目开发工作流仓库。版本化项目根从 `$HOME/.agents/codex-dev-workflow/当前安装.json` 的 `project_root` 读取；先在该项目修改并运行完整测试，再使用项目 `scripts/manage_skills.py` 的 `dry-run`、`confirm` 和 `check` 管理 `$HOME/.agents/skills` 运行副本。`codex-sdlc agent-sync`只做全局重名审计，不负责安装这六个技能。
   - 不要直接修改 `$HOME/.codex/skills/sdlc-*`、`$HOME/.agents/sdlc/skills/sdlc-*` 或 `$HOME/.claude/commands/sdlc-*.md` 里的运行时副本；这些目录由 `agent-sync` 生成。
   - 不要直接修改上述六个Codex项目开发工作流技能的 `$HOME/.agents/skills` 运行副本，也不要把同名副本放进 `$HOME/.codex/skills`。
   - 普通通用开发技能默认放在 `$HOME/.agents/skills`，让 Codex 和 Claude Code 共享同一份入口；不要默认放到 `$HOME/.codex/skills`。
   - 工具专属配置只放在对应工具目录，例如 Claude Slash Commands 放 `$HOME/.claude/commands`。
2. 不能把同名技能同时放在 `$HOME/.codex/skills` 和 `$HOME/.agents/skills`。确实需要迁移时，先备份，再保留一个入口作为主入口。
3. 修改任何 `sdlc-*` 或全局入口规则后，必须执行：
   - `codex-sdlc agent-sync --dry-run`
   - 用户已明确要求落地或确认同步时，再执行 `codex-sdlc agent-sync --confirm`
   - dry-run 输出里的“来源状态”必须是“正在使用仓库内版本化技能”；如果不是，先处理来源目录，不要继续同步。
   - 同步后检查 Codex `$sdlc-*` 和 Claude Code `/sdlc-*` 是否仍是同一套语义，重点看 DRAFT、`start --file`、`review`、task-run、`change-package`、当前任务保护、Goal 工作线程、子代理决策留痕和禁止手写状态文件。
   - 同步结果不能包含 `sdlc-prepare`、`sdlc-brief`、`sdlc-brief-augment` 或 `sdlc-brief-review`，也不能继续推荐已经下线的阶段。
4. 修改非 SDLC 共享技能后，也要执行 `agent-sync --dry-run` 做全局审计，确认：
   - `.agents/skills` 没有 `sdlc-*` 重复入口。
   - `manifest.json` 能看到共享技能数量。
   - 没有非 SDLC 同名技能同时存在于 `.codex/skills` 和 `.agents/skills`。
5. 创建或更新技能时遵守技能结构：
   - 技能目录名用小写字母、数字和连字符。
   - 必须有 `SKILL.md`。
   - frontmatter 只写 `name` 和 `description`。
   - `description` 要写清什么时候触发，不把触发条件藏在正文里。
   - 正文只写执行所需规则，不写过程复盘和无关背景。
6. 完成后输出：
   - 改了哪些技能或入口。
   - 版本化来源或共享入口在哪里。
   - 是否执行了 `agent-sync --dry-run` 和 `--confirm`。
   - 是否检查了 Codex 技能、Claude Commands 和共享技能里的文档优先主流程。
   - 是否发现重名、重复入口或需要用户处理的风险。
7. 本技能只维护 Agent 能力入口，不创建项目需求，不拆业务任务，不写业务代码。
