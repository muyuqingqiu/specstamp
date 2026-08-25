---
name: sdlc-backup-clean
description: Use when 用户需要清理旧的本机 SDLC 备份
---

# sdlc-backup-clean

用户侧指令：`$sdlc-backup-clean`
底层执行：`codex-sdlc backup-clean`

执行时请：

1. 说明这条指令只清理项目外的本机 SDLC 旧备份，不碰当前项目目录和业务代码。
2. 默认底层执行 `codex-sdlc backup-clean`，保留每个 worktree 最近 20 个项目快照、每个需求最近 50 个需求快照，并在最近 90 天内每天额外保留一个代表快照；自动快照最多保留 7 天。
3. 用户明确给保留数量或保留天数时，使用 `--keep-project`、`--keep-requirement`、`--keep-days` 和 `--keep-auto-days`。
4. 说明 pinned 快照不会被自动清理；需要长期保留的节点应在备份时使用 `--pin`。
5. 完成后说明清理了多少项目快照和需求快照。
6. 本阶段只清理旧备份，不恢复、不改任务、不提交 Git。
