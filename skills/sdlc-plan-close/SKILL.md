---
name: sdlc-plan-close
description: 关闭结构化任务计划中的未完成任务
---

# sdlc-plan-close

正式入口：`codex-sdlc plan-close`。

执行时请：

1. 读取 `task-plan.v2`、目标任务、`task-coverage.v1`、当前 task-run 和 `task_plan` 审核状态，确认关闭不会留下未承接的 FR、设计或 AC。
2. 执行 `codex-sdlc plan-close REQ-001 T-003`；多个任务可以在同一命令末尾依次传入。
3. 活动 task-run、仍被未完成任务依赖或承担唯一覆盖责任的任务不能静默关闭；先处理运行状态、依赖和覆盖关系。
4. 成功后重新读取 `task-plan.v2` 和 `task-coverage.v1`，核对关闭状态、剩余顺序、依赖与覆盖仍一致。
5. 关闭任务使旧 `task_plan` 审核变为 `stale`。重新创建整套任务审核；审核通过前不开始后续任务。
6. 本阶段不创建 task-run、不写业务代码、不跑业务测试。
