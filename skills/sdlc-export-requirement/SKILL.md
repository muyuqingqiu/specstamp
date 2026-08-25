---
name: sdlc-export-requirement
description: 导出指定需求的阶段交付记录，对应 codex-sdlc export-requirement
---

# sdlc-export-requirement

这个 Skill 对应命令：`codex-sdlc export-requirement`

执行时请：

1. 确认用户给了需求编号。
2. 执行 `codex-sdlc export-requirement REQ-001`。
3. 告诉用户导出文件保存到了 `.codex-sdlc/exports/REQ-001.md`。
4. 本阶段只导出记录，不修改需求、不改任务、不写代码。
