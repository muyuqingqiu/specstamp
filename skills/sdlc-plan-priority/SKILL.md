---
name: sdlc-plan-priority
description: 调整结构化需求任务计划的优先级
---

# sdlc-plan-priority

正式入口：`codex-sdlc plan-priority`。

执行时请：

1. 读取当前 `task-plan.v2` 和 `task_plan` 审核状态，确认需求编号无歧义。
2. 优先级只能是 `low`、`normal`、`high`。执行：
   `codex-sdlc plan-priority REQ-001 high`
3. 成功后重新读取 `task-plan.v2`，核对优先级和需求身份，不根据展示文字判断写入成功。
4. 如果优先级属于整套任务审核输入，旧 `task_plan` 审核应变为 `stale`；重新创建审核并以 `review status` 的当前输入结果为准。
5. 本阶段不改变任务目标、依赖或覆盖关系，不创建 task-run、不写业务代码、不跑业务测试。
