---
name: sdlc-plan-reorder
description: 重排结构化任务计划中的未完成任务顺序
---

# sdlc-plan-reorder

正式入口：`codex-sdlc plan-reorder`。

执行时请：

1. 读取 `task-plan.v2`、任务状态、依赖关系、当前 task-run 和 `task_plan` 审核状态。
2. 执行 `codex-sdlc plan-reorder REQ-001 "T-002,T-003"`。部分重排只调整未完成队列，不能把 `done` 或 `closed` 任务混入新顺序。
3. 新顺序不能违反依赖关系，也不能让新任务越过正在收口的活动任务。误启动任务使用 `$sdlc-task-pause`，不要靠重排改写运行状态。
4. 成功后重新读取 `task-plan.v2`，逐项核对顺序、依赖和任务身份。
5. 顺序变化使旧 `task_plan` 审核变为 `stale`。重新创建整套任务审核，当前输入通过后才能开工。
6. 本阶段不创建 task-run、不写业务代码、不跑业务测试。
