---
name: sdlc-agent-sync
description: 同步全局 Agent 工具入口，把 SDLC 版本化技能来源、Codex 技能和 Claude Commands 对齐
---

# sdlc-agent-sync

命令：`codex-sdlc agent-sync`

执行时请：

1. 说明这一步是维护全局 Agent 入口，不修改当前项目的 `.codex-sdlc/` 需求资料。
2. 修改 `sdlc-*` 技能时，先改 `<SpecStamp 仓库>/skills/<技能名>/SKILL.md`，不要直接改 `$HOME/.codex/skills`、`$HOME/.agents/sdlc/skills` 或 `$HOME/.claude/commands` 里的运行时副本。
3. 修改已纳入管理的共享技能时，先改 `<SpecStamp 仓库>/shared-skills/<技能名>/SKILL.md`，不要直接改 `$HOME/.agents/skills/<技能名>` 里的运行时副本。
4. 默认先执行 `codex-sdlc agent-sync --dry-run`，展示版本化技能来源、共享技能来源、仓库内技能目录、来源状态、运行时标准目录、Codex 技能目录、Claude Commands 目录、重复入口数量和备份目录。
5. 如果输出提示“当前没有使用仓库内版本化技能”或“当前没有使用仓库内版本化共享技能”，先确认是否设置了 `CODEX_SDLC_SOURCE_SKILLS_HOME` 或 `CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME`；普通维护不要用运行时副本当来源。
6. 只有用户明确要求落地、确认或修复重复入口时，才执行 `codex-sdlc agent-sync --confirm`。
7. 确认同步后，必须检查：
   - `$HOME/.agents/skills` 下不能再有 `sdlc-*` 目录。
   - `$HOME/.agents/sdlc/manifest.json` 存在并能解析。
   - `$HOME/.agents/sdlc/manifest.json` 里的 `source_is_versioned` 为 `true`。
   - `$HOME/.agents/sdlc/manifest.json` 里的 `shared_source_is_versioned` 为 `true`。
   - `$HOME/.codex/skills` 保留 `sdlc-*` 技能入口。
   - `$HOME/.claude/commands` 保留 `/sdlc-*` Slash Commands。
8. 同步后的 Codex `$sdlc-*` 和 Claude Code `/sdlc-*` 必须是同一套语义；重点检查 DRAFT、`start --file`、`review`、task-run、`change-package`、当前任务保护、Goal 工作线程、子代理决策留痕和禁止手写状态文件。
9. 同步结果不能包含 `sdlc-prepare`、`sdlc-brief`、`sdlc-brief-augment` 或 `sdlc-brief-review`，也不能继续推荐已经下线的阶段。
10. 如果本轮还修改了未纳入 `shared-skills/` 的非 SDLC 技能、Hook、Slash Command 或其他 Agent 能力，同时按 `$HOME/.agents/skills/agent-capability-sync/SKILL.md` 的规则检查共享入口和重名风险。
11. 如果发现 Codex 当前会话仍显示重复入口，告诉用户这是会话启动时加载的旧技能列表，重开 Codex 或刷新命令面板后再看。
12. 本指令只处理全局入口同步，不创建需求、不拆任务、不写业务代码。
