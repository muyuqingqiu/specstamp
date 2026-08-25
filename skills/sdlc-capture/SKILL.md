---
name: sdlc-capture
description: 记录中途结论、待确认问题和涉及文件，并调用 codex-sdlc capture
---

# sdlc-capture

这个 Skill 对应命令：`codex-sdlc capture`

执行时请按下面顺序处理：

1. 先说明这次会把中途结论记进 `.codex-sdlc/captures/`。
2. 如果当前项目还没初始化，也照常继续，让命令做最小初始化。
3. 如果用户已经明确这条结论属于某个需求，改用 `$sdlc-capture-link`。
4. 如果用户想把结论直接转成需求，改用 `$sdlc-capture-requirement`。
5. 如果用户想把结论直接转成变更，改用 `$sdlc-capture-change`。
6. 如果内容是开发经验、测试经验、排查经验、模拟器操作方法、可复用命令或避免重复踩坑的说明，改用 `$sdlc-lessons add`，按需求级、跨需求、项目级或 AGENTS 候选保存。
7. 告诉用户生成了哪条 capture，以及有没有自动关联到需求或变更。
8. 最后提醒用户使用 `$sdlc-next` 看看这条 capture 是否需要尽快处理。
9. 本阶段只记录中途结论，不创建正式需求、不拆任务、不写代码、不跑测试。
10. 完成后必须停下，等待用户决定是否处理这条 capture。
