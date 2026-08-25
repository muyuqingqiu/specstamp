---
name: sdlc-export
description: 导出需求、任务、变更、交接和验证汇总，并调用 codex-sdlc export
---

# sdlc-export

这个 Skill 对应命令：`codex-sdlc export`

执行时请按下面顺序处理：

1. 先说明这次会导出 Markdown 交付记录。
2. 如果用户没给需求编号，执行 `codex-sdlc export` 导出全部需求。
3. 如果用户给了需求编号，改用 `$sdlc-export-requirement`。
4. 告诉用户导出文件保存到了 `.codex-sdlc/exports/`。
5. 最后补一句：导出内容默认不带本机绝对路径，适合交接、归档或复盘。
6. 本阶段只导出记录，不修改需求、不改任务、不写代码。
