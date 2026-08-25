---
name: sdlc-plan
description: 调整结构化任务计划、任务合同、覆盖关系和整套任务审核
---

# sdlc-plan

任务规划以 `task-plan.v2`、逐任务 `task.v2` 和 `task-coverage.v1` 为正式输入，整套计划只使用 `task_plan` 审核。

执行时请：

1. 先读取当前正式需求、设计、引用索引、`tasks/task-plan.v2.json`、全部相关 `task.v2`、`task-coverage.v1.json`、代码证据和 `task_plan` 审核状态。
2. 新建整套计划或需要同时改多个任务和覆盖关系时，生成完整导入目录并执行：
   `codex-sdlc tasks REQ-001 --plan-file <task-plan.v2路径> --tasks-dir <task.v2目录> --coverage-file <task-coverage.v1路径>`
3. 新任务使用 `client_key`，计划、依赖和覆盖关系用 `@client:` 引用；CLI 成功后才使用返回的正式 `T-xxx` 映射。
4. 单一调整分别使用 `$sdlc-plan-add-task`、`$sdlc-plan-amend-task`、`$sdlc-plan-reorder`、`$sdlc-plan-depends`、`$sdlc-plan-close` 或 `$sdlc-plan-priority`，不要把多类动作混在一条模糊命令里。
5. 任意任务正文、计划顺序、依赖、覆盖关系、引用索引或代码证据变化后，旧 `task_plan` 审核都应变为 `stale`。不得继续使用旧 `passed` 结果。
6. 修订完成后通过 `codex-sdlc review create --stage task_plan` 创建整套任务审核；由独立任务执行 `review submit`，再用 `review status` 核对当前输入。
7. 覆盖不全、引用无效、依赖成环、任务存在阻塞条件或审核未通过时保持 `planning_tasks`。审核对当前输入有效且为 `passed` 后才是 `ready_for_development`。
8. 本阶段只修改结构化任务计划和审核状态，不创建 task-run、不写业务代码、不跑业务测试。
