---
name: sdlc-init-plain
description: 在非 Git 目录初始化 SDLC，对应 codex-sdlc init-plain
---

# sdlc-init-plain

这个 Skill 对应命令：`codex-sdlc init-plain`

执行时请：

1. 先说明这条指令适合没有 Git 仓库的本机目录。
2. 确认当前目录就是目标目录。
3. 执行 `codex-sdlc init-plain`。
4. 告诉用户 `.codex-sdlc/` 是否初始化成功。
5. 本阶段只准备本机环境，不创建需求、不讨论方案、不拆任务、不写代码。
6. 最后只优先推荐 `$sdlc-discuss 需求想法`；小需求也走 DRAFT 主流程，不再推荐 `$sdlc-light-start`。
7. 完成后必须停下，等待用户进入需求讨论阶段。
8. 不要主动创建、维护或介绍其它流程目录；旧项目资料需要迁移时，单独导入为 SDLC 原生材料或需求包。
