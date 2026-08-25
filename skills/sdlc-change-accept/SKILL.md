---
name: sdlc-change-accept
description: 核对结构化变更包、审核和任务保护后事务生效新版本
---

# sdlc-change-accept

正式入口：`codex-sdlc change-protect` 和 `codex-sdlc change-accept`。

执行时请：

1. 只处理已经提交 `change-package.v1` 和五份预计结果的结构化 CHG。用户指定 `REQ-xxx CHG-xxx` 时使用该身份；同一需求存在多个候选时必须让用户明确选择。
2. 读取工作区 `status.json`、预计结果事件和 `change-package.v1`，确认基础版本、输入身份和 CHG 所有权仍一致。
3. 执行 `codex-sdlc review status`，核对 `review_impacts` 要求的 `requirement_split`、`integrated_design`、`task_plan` 审核都对当前输入有效且为 `passed`。审核缺失、`pending`、`stale` 或未通过时停止。
4. 用户确认预计需求版本后执行：
   `codex-sdlc change-protect REQ-001 CHG-001 --confirm-requirement`
5. `change-protect` 只登记保护结果：受影响的活动 task-run 必须变为 `stale` 并暂停对应任务，已完成任务保持不可改写，`unaffected` 关系必须有结构化依据。核对 `change_protected` 事件和保护结果哈希；没有得到 `protected` 结果时不得继续。
6. 保护完成后执行：
   `codex-sdlc change-accept REQ-001 CHG-001`
7. 核对输出中的事务、目标版本、完成回执和幂等标记，并重新读取正式需求、设计、测试矩阵、引用索引和任务计划，确认它们属于同一 CHG 与目标版本。
8. 基础漂移、审核失效、活动任务未保护、事务冲突或恢复失败时命令应非零退出。旧 `effective` 仍是正式版本；修正明确原因后对同一 CHG 重试，不创建替代 CHG 绕过失败。
9. 生效后按当前 `task-plan.v2`、`task.v2`、任务覆盖关系和 `task_plan` 审核状态决定下一步；未通过当前任务审核的任务不能开工。
