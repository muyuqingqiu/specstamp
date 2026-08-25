---
name: sdlc-tasks
description: 从正式需求和设计生成三套结构化任务输入，原子导入并完成整套任务审核
---

# sdlc-tasks

## 输入

- 当前正式需求编号，例如 `REQ-001`。
- 当前 `effective/requirement.current.json`、`effective/design.current.json`、`effective/test-matrix.current.json`、`reference-index.v1.json` 和 `original/formal.v3.json`。
- 项目规则、依赖清单、真实代码、已完成前置任务交付物和其它任务规划代码证据。
- 模型生成的 `task-plan.v2`、任务目录中的每份 `task.v2`、`task-coverage.v1`。

每份 `task.v2` 都要明确目标、交付物、依赖、正式引用、允许读取和修改的路径、实现要求、自动测试、人工验收、不做范围、阻塞条件和完成标准。不能从标题或摘要猜依赖、任务类型和覆盖关系。

## 执行

1. 对照全部 FR、GR、AC、设计产物和已接受变更检查覆盖关系。每个正式对象都要由任务承接，或在 `no_development_items` 中写明无需开发的正式依据。
2. 使用稳定 `client_key` 连接计划、任务和覆盖矩阵。新增任务在导入前不能自行占用正式 `T-xxx` 编号。
3. 执行原子导入：

`codex-sdlc tasks REQ-001 --plan-file tmp/task-plan.v2.json --tasks-dir tmp/tasks --coverage-file tmp/task-coverage.v1.json`

4. 读取命令返回的正式编号映射、任务目录、覆盖文件、规划代码证据哈希和审核建议。命令失败时修正同一套输入后重试，不能改用细调命令拆开写入。
5. 按命令返回的审核编号创建整套任务审核：

`codex-sdlc review create --review-id REV-001 --stage task_plan --owner REQ-001 --input .codex-sdlc/requirements/REQ-001/tasks/task-plan.v2.json`

6. 审核者在独立任务中读取审核请求冻结的全部输入，生成 `review-result.v1`，再提交结果：

`codex-sdlc review submit --request REV-001 --file tmp/task-review-result.v1.json`

7. 读取审核状态，并重新核对项目状态和正式下一步：

`codex-sdlc review status --review REV-001`

`codex-sdlc status`

`codex-sdlc next`

## 输出

- `tasks/task-plan.v2.json`。
- `tasks/T-xxx.json` 和 `tasks/T-xxx.md`。
- `task-coverage.v1.json`。
- 任务导入回执、规划代码证据和 `task_plan` 审核记录。
- 审核通过且没有阻塞项时，需求状态为 `ready_for_development`。

## 阻塞条件

- 三套输入缺失、Schema 不合法、`client_key` 关系不完整或预计覆盖不完整。
- 正式引用不存在、定位或 SHA-256 漂移。
- 规划代码证据缺少适用项目规则、依赖、真实代码或前置交付物。
- 任务依赖成环、依赖不存在、阻塞条件未清空。
- 审核为 `pending`、`needs_fix`、`stale`，或审核输入已经变化。
- 导入来源在校验和提交之间发生变化。

任一条件成立时保持 `planning_tasks`，不得开工。

## 审核点

整套任务只有一个固定 `task_plan` 独立审核点。审核要同时覆盖任务计划、全部任务文件、覆盖矩阵、正式需求、设计、引用索引和规划代码证据。修改其中任一受控输入后，旧审核不能继续使用。

## 停止位置

审核有效通过、规划代码证据仍为当前版本、需求进入 `ready_for_development` 后停止。下一步由 `$sdlc-task REQ-001 T-001` 直接开工；本技能不创建任务运行轮次，也不写业务代码。
