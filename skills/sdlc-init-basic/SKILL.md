---
name: sdlc-init-basic
description: 兼容旧命令，只初始化 SDLC，对应 codex-sdlc init-basic
---

# sdlc-init-basic

这个 Skill 对应命令：`codex-sdlc init-basic`

执行时请：

1. 先说明这条指令只准备 `.codex-sdlc/`。
2. 确认当前目录是 Git 项目。
3. 执行 `codex-sdlc init-basic`。
4. 告诉用户这是兼容旧命令；现在 `$sdlc-init` 默认也是只初始化 SDLC。
5. 本阶段只准备本机记录环境，不创建需求、不拆任务、不写代码。
6. 完成后必须停下，等待用户推进下一阶段。
