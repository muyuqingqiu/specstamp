---
name: sdlc-doctor-install
description: 检查本机 SDLC 安装情况，对应 codex-sdlc doctor-install
---

# sdlc-doctor-install

这个 Skill 对应命令：`codex-sdlc doctor-install`

执行时请：

1. 执行 `codex-sdlc doctor-install`。
2. 说明 CLI、PATH、Skill 哪些已就绪，哪些缺失。
3. 如果缺 Skill，先说明运行时入口缺失；修复时应检查 `<SpecStamp 仓库>/skills/<技能名>/SKILL.md` 是否存在，再执行 `codex-sdlc agent-sync --dry-run` 和 `codex-sdlc agent-sync --confirm`，不要手写 `~/.codex/skills` 运行时副本。
4. 本阶段只检查本机安装情况，不初始化项目、不创建需求、不改任务。
