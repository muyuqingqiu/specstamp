---
name: sdlc-capture-requirement
description: 把中途结论直接转成新需求，对应 codex-sdlc capture-requirement
---

# sdlc-capture-requirement

这个 Skill 对应命令：`codex-sdlc capture-requirement`

执行时请：

1. 确认用户希望这条结论成为新需求。
2. 执行 `codex-sdlc capture-requirement "结论内容"`。
3. 告诉用户生成了哪条 capture、哪个新需求和初始任务。
4. 本阶段只把结论转成轻量需求和初始任务，不写代码、不跑测试、不提交代码。
5. 完成后必须停下，下一步只推荐 `$sdlc-next`。
