---
name: sdlc-doctor
description: 检查安装或项目体检，对应 codex-sdlc doctor
---

# sdlc-doctor

命令：`codex-sdlc doctor`

执行时请：

1. 先判断用户要查安装、项目体检、重建快照还是深度体检。
2. 安装用 `codex-sdlc doctor-install`；普通体检用 `codex-sdlc doctor`；重建用 `codex-sdlc doctor-repair`；只读深查用 `codex-sdlc doctor-deep`。
3. 普通体检只检查当前 SDLC 状态，不主动提其它流程目录，也不自动导入旧资料。
4. 有旧项目资料需要继续使用时，先迁移成 SDLC 原生材料、经验或需求包。
5. Hooks / Rules 缺失时说明可重跑 `codex-sdlc init` 补齐。
6. 如果深度体检提示“孤儿任务文件”，说明主状态已经不包含这些旧任务文件，执行 `codex-sdlc doctor-repair` 会先备份到 `.codex-sdlc/backups/orphaned-tasks-*` 再清理。
7. 说明缺什么、怎么修，不要只贴命令结果。
8. 本阶段只检查或说明修复办法，不创建需求、不改任务、不开始开发。
