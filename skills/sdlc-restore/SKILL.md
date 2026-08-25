---
name: sdlc-restore
description: Use when 用户需要从本机备份恢复 SDLC 资料
---

# sdlc-restore

用户侧指令：`$sdlc-restore`
底层执行：`codex-sdlc restore`

执行时请：

1. 恢复前先底层执行 `codex-sdlc restore --dry-run`、`codex-sdlc restore REQ-001 --dry-run`，或 `codex-sdlc restore REQ-001 --snapshot 快照名 --dry-run`，只预览不写文件。
2. 用户要恢复某个跨分支或跨工作树快照时，优先使用 `--snapshot 快照名` 精确指定来源，不要只靠自动匹配。
3. 只有用户明确确认恢复时，才底层执行 `codex-sdlc restore --confirm`、`codex-sdlc restore REQ-001 --confirm`，或带同一个 `--snapshot 快照名` 的确认命令。
4. 当前已有 `.codex-sdlc/` 或同需求状态时，不要静默覆盖；需要覆盖时必须让用户明确同意，再加 `--replace`。
5. 项目快照用于恢复 `.codex-sdlc/` 中备份包包含的资料。确认覆盖时，旧目录会先移到 `*.pre-restore-*`。
6. 需求快照用于恢复指定需求；即使当前项目的 `.codex-sdlc/` 已丢失，只要有同项目需求快照，也可以直接恢复该需求。
7. 需求快照会一起带回 `.codex-sdlc/lessons/` 里的跨需求和项目级经验，避免恢复后执行包缺少历史经验。
8. 指定需求恢复默认只匹配同仓库备份；如果没有找到，不要拿其它项目同名 `REQ-001` 兜底。
9. 恢复预览时说明来源分支、来源工作树、快照名、时间、需求、需求简介和匹配程度；用户确认后再恢复。
10. 恢复后说明恢复来源、恢复了哪些需求、是否已重建 SQLite 和 Markdown、需求包目录是否已恢复。
11. 如果恢复输出提示“已刷新任务版本和覆盖关系”，说明恢复后已经把可确定的任务 contract 对齐当前生效版本。
12. 如果用户是切换分支后想保留当前需求包，先推荐 `$sdlc-backup-list` 查候选；没有可靠备份时，提醒用户切回原分支执行 `$sdlc-backup --pin` 后再回来恢复。
13. 恢复后建议执行 `$sdlc-doctor-deep` 检查身份、备份完整性和任务版本关系。
14. 本阶段只恢复 SDLC 状态，不恢复业务源码、不提交业务 Git、不开始任务。
