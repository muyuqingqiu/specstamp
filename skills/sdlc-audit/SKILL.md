---
name: sdlc-audit
description: 怀疑一批已完成任务质量、想从头复查已完成任务或误推进后需要插入复查任务时使用
---

# sdlc-audit

命令：`codex-sdlc audit`

执行时请：

1. 只在“复查一批已完成任务质量”时使用，比如从 T-001 到 T-003 重新看实现、验证和提交边界。
2. 当前任务刚完成但验收不过，用 `$sdlc-task-restore`；历史单个明确 bug，用 `$sdlc-fix`；需求目标变了，用 `$sdlc-change`；技术方案错了，回到 `$sdlc-design`，确认后由 `$sdlc-design-accept` 同步明确任务影响。
3. 用户给出范围时，执行 `codex-sdlc audit REQ-001 T-001 到 T-003 --note "复查说明"`。
4. 用户没有给范围时，默认复查当前需求里所有 `done` 任务；多个活跃需求时询问用户。
5. 命令会自动插入一个新的质量复查任务，并把未完成任务放到它后面。
6. 如果当前已有 `doing` 任务，命令会先退回 `todo`，避免继续推进错任务。
7. 本阶段只调整任务队列，不写业务代码、不跑测试、不提交代码。
8. 复查任务执行后如果没发现问题，用 `$sdlc-task-done` 收口复查任务，并回到原主线。
9. 复查任务执行后如果发现历史实现 bug，用 `$sdlc-fix` 插入修复任务；需求目标变了用 `$sdlc-change`；技术方案错了回到 `$sdlc-design`。
10. 不能把旧的 `done` 任务直接改回 `todo`，也不要用 `$sdlc-plan-add-task` 和 `$sdlc-plan-reorder` 手动模拟复查。
11. 完成后必须停下，只推荐 `$sdlc-task REQ-001 T-xxx` 或 `$sdlc-next`。
