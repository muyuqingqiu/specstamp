---
name: sdlc-doctor-repair
description: 重建 SQLite 和 Markdown 快照，对应 codex-sdlc doctor-repair
---

# sdlc-doctor-repair

这个 Skill 对应命令：`codex-sdlc doctor-repair`

执行时请：

1. 先说明这条指令会以 `events.jsonl` 为准重建 `sdlc.db` 和 Markdown 快照。
2. 说明它也会处理能安全推断的旧变更状态残留：补齐规划任务、覆盖映射，并把误标为已处理但任务未完成的变更改回 `planned`。
3. 说明它也会刷新能确定的任务版本绑定和 FR/TC 覆盖关系，让任务重新对齐当前 `effective/*.current.md`。
4. 说明它只修复 SDLC 状态和快照，不改业务代码、不创建需求、不开始任务、不跑测试、不提交 Git。
5. 说明 `## 人工补充` 区会保留，自动生成区会按 `events.jsonl` 重写。
6. 执行 `codex-sdlc doctor-repair`。
7. 告诉用户是否重建成功、是否修复旧变更状态残留、是否刷新任务版本和覆盖关系，以及有没有孤儿任务文件备份。
8. 如果提示无法安全推断覆盖任务，说明本次没有自动改那条变更；已生效或旧 `pending` 变更优先用 `$sdlc-change-plan`，明确补任务、调顺序或关任务时再用 `$sdlc-plan-*`。
9. 如果提示 `events.jsonl` 缺失，先停止并让用户恢复备份。
10. 如果 `events.jsonl` 本身记录错了，不要继续 repair；先和用户确认要怎么恢复或重新记录状态。
