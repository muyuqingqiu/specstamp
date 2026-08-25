---
name: sdlc-init
description: 初始化当前项目的本机 SDLC 工作区，并调用 codex-sdlc init
---

# sdlc-init

命令：`codex-sdlc init`

执行时请：

1. 说明只会初始化 `.codex-sdlc/` 和项目级 SDLC 辅助文件。
2. 确认当前目录是目标项目。
3. 执行 `codex-sdlc init`。
4. 告诉用户创建了哪些核心文件，并说明 `.codex-sdlc/identity.json` 会绑定当前仓库、分支和工作树，后续切分支或换 worktree 时会防止状态串用。
5. 本阶段只准备本机环境，不创建需求、不讨论方案、不拆任务、不写代码。
6. 下一步只优先推荐 `$sdlc-discuss 需求想法`；小需求也走 DRAFT 主流程，不再推荐 `$sdlc-light-start`。
7. 完成后必须停下，等待用户进入需求讨论阶段。
8. 不要主动创建、维护或介绍其它流程目录；旧项目资料需要迁移时，单独导入为 SDLC 原生材料或需求包。
