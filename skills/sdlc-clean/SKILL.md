---
name: sdlc-clean
description: 预览当前项目会清理哪些本机 SDLC 产物，对应 codex-sdlc clean
---

# sdlc-clean

这个 Skill 对应命令：`codex-sdlc clean`

执行时请：

1. 先说明这是清理预览，不会删除文件。
2. 默认执行 `codex-sdlc clean`。
3. 把将清理和将保留的内容转述给用户。
4. 如果用户确认要清理，再使用 `$sdlc-clean-confirm`。
5. 提醒用户：清理命令不会修改 Git 全局忽略配置。
6. 本阶段只预览清理范围，不删除文件、不继续执行确认清理。
