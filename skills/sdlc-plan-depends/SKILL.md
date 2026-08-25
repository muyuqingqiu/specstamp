---
name: sdlc-plan-depends
description: 更新结构化任务计划中的显式依赖关系
---

# sdlc-plan-depends

正式入口：`codex-sdlc plan-depends`。

执行时请：

1. 读取 `task-plan.v2`、相关 `task.v2`、任务状态、当前 task-run 和 `task_plan` 审核状态。
2. 执行 `codex-sdlc plan-depends REQ-001 "T-003:T-001,T-002"`；多条依赖规则可以继续作为位置参数传入。
3. 不允许任务依赖自身、形成依赖环或引用不存在的任务。活动任务不能新增尚未完成的前置依赖；需要改变活动任务目标时先走正式恢复或变更流程。
4. 成功后重新读取 `task-plan.v2` 和相关 `task.v2`，核对依赖双方、顺序和任务身份一致。
5. 依赖变化使旧 `task_plan` 审核变为 `stale`。重新创建整套任务审核，当前输入通过后才能开工。
6. 本阶段不创建 task-run、不写业务代码、不跑业务测试。
