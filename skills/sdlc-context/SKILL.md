---
name: sdlc-context
description: 查看、保存、恢复和导入当前项目的 SDLC 上下文；用于切换分支、切换 worktree、删除 worktree 前备份、身份不一致和跨分支恢复需求包场景
---

# sdlc-context

用户侧指令：`$sdlc-context`
底层执行：`codex-sdlc context`

执行时请：

1. 本指令只管理 SDLC 上下文：当前项目、分支、worktree、需求包、备份快照和身份匹配状态；不写业务代码、不拆任务、不跑业务测试。
2. 用户说切换分支、切换 worktree、删除 worktree 前保留资料、恢复其它分支需求包、身份不一致时，优先用本指令，不要直接改 `.codex-sdlc/identity.json`。
3. 查看当前状态用 `codex-sdlc context`。
4. 查看同项目可恢复资料用 `codex-sdlc context list`；需要看某个需求时用 `codex-sdlc context list REQ-001`。
5. 删除 worktree、长期暂停或切分支前，先用 `codex-sdlc context save --pin` 固定保存；只保存某个需求时用 `codex-sdlc context save REQ-001 --pin`。`context save` 会检查身份，不一致时不能强行保存。
6. 恢复项目或需求前必须先 dry-run：`codex-sdlc context restore --dry-run` 或 `codex-sdlc context restore REQ-001 --dry-run`。
7. 用户确认后才加 `--confirm`；需要覆盖当前已有 `.codex-sdlc/` 或同需求状态时必须由用户明确同意，再加 `--replace`。
8. 把别的分支或 worktree 的需求带到当前分支时，优先用 `codex-sdlc context import REQ-001 --dry-run`，确认后再 `--confirm`；不要静默覆盖当前同名需求。
9. 如果当前已经有同编号需求，但用户想保留两份，用 `codex-sdlc context import REQ-001 --as REQ-099 --dry-run` 预览，确认后再加 `--confirm`。
10. 需求导入成新编号时，工具会改写事件里的需求编号、恢复成新的需求包目录，并写入 `restore-alias.json` 记录来源。
11. 身份不一致时，输出要给用户清楚选择：恢复当前分支资料、导入旧分支需求、先切回旧分支保存、或取消。
12. 本指令完成后只推荐下一条 `$sdlc-*` 指令，不自动进入 `$sdlc-task`。
