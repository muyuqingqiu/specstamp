---
name: sdlc-backup-list
description: Use when 用户需要查看当前项目匹配的本机 SDLC 备份
---

# sdlc-backup-list

用户侧指令：`$sdlc-backup-list`
底层执行：`codex-sdlc backup-list`

执行时请：

1. 底层执行 `codex-sdlc backup-list` 查看当前项目匹配的项目快照和需求快照；默认输出只展示每类最近的少量候选，避免被自动备份刷屏。
2. 用户指定需求时，底层执行 `codex-sdlc backup-list REQ-001`。
3. 用户要排查历史备份时，再使用 `codex-sdlc backup-list --all`；只想多看几条时用 `codex-sdlc backup-list --limit 20`。
4. 如果 `index.json` 丢失或为空，工具会尝试从备份目录重建索引；不要因为索引文件缺失就判断“没有备份”。
5. 输出时说明候选快照的项目、需求号、需求简介、来源分支、来源工作树、提交、快照名、时间、匹配程度，以及是否 pinned。
6. 如果看到跨分支或跨工作树候选，提醒用户先确认来源分支和工作树是否就是要恢复的那份。
7. 如果用户想恢复某个具体快照，下一步推荐 `$sdlc-restore REQ-001 --snapshot 快照名 --dry-run`。
8. 如果用户只想恢复当前最匹配的项目快照，下一步推荐 `$sdlc-restore --dry-run`。
9. 本阶段只查看备份，不写项目文件、不改当前 SDLC 状态、不开始任务。
