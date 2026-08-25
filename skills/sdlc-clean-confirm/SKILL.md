---
name: sdlc-clean-confirm
description: 确认清理当前项目的本机 SDLC 产物，对应 codex-sdlc clean-confirm
---

# sdlc-clean-confirm

这个 Skill 对应命令：`codex-sdlc clean-confirm`

执行时请：

1. 只有用户明确要求清理或确认清理时才执行。
2. 默认执行 `codex-sdlc clean-confirm`。
3. 清理后告诉用户删掉了哪些项目产物。
4. 如果命令提示有项目级 Codex 配置被保留，说明这些文件内容不是当前 SDLC 自动生成内容。
5. 提醒用户：Git 全局忽略配置会保留。
6. 本阶段只清理 SDLC 本机产物、旧技能入口和自动生成配置。不创建需求、不改任务、不改业务代码。
