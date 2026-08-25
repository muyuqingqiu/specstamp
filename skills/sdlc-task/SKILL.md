---
name: sdlc-task
description: 从审核通过的正式任务直接开工，读取完整清单并激活当前任务运行轮次
---

# sdlc-task

## 输入

- 当前正式状态和下一步推荐：

`codex-sdlc status`

`codex-sdlc next`

- 已通过当前 `task_plan` 审核的 `REQ-xxx / T-xxx`。
- 任务合同、正式引用、项目规则、前置任务交付物和真实工作树。
- 当前 Codex 任务身份；读取确认必须由开工时的同一个任务执行。

## 执行

1. 只选择正式下一步返回的可执行任务。候选不唯一、依赖未完成或阻塞条件不为空时停止，不能从标题猜执行顺序。
2. 直接开工：

`codex-sdlc task REQ-001 T-001`

3. 从命令返回的当前运行轮次读取 `task-run.v1.json` 和 `task-read-manifest.v1.json`。按清单逐项读取完整任务文件、FR、GR、AC、技术方案、设计产物、资料、项目规则和前置交付物；路径、定位和 SHA-256 只用于找到原文，不能代替阅读。
4. 计算当前 `task-read-manifest.v1.json` 的完整 SHA-256，并在同一任务中确认：

`codex-sdlc task-read-confirm REQ-001 T-001 --manifest-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`

5. 确认当前轮次从 `reading` 进入 `active` 后，再按 `allowed_output_paths` 和任务合同开发。
6. 开发中反复检查当前运行基线：

`codex-sdlc task-run-check REQ-001 T-001`

7. 测试、人工验收、现场证据和反馈都绑定当前运行轮次。完成动作交给 `$sdlc-task-done`，不能绕过运行轮次和范围门禁。

## 输出

- `runtime/T-001/runs/0001/task-run.v1.json`。
- `runtime/T-001/runs/0001/task-read-manifest.v1.json`。
- 指向当前轮次的 `runtime/T-001/current.json`。
- 任务状态为 `doing`，当前运行状态依次为 `reading`、`active`，上游漂移时为 `stale`。

## 阻塞条件

- 需求不是 `ready_for_development` 或 `developing` 中允许继续的当前任务。
- 整套任务审核无效、规划代码证据过期、依赖未完成或任务有阻塞条件。
- 正式引用缺失、定位或哈希不一致。
- 当前 Codex 任务身份缺失、读取确认来自其它任务，或清单哈希不一致。
- 当前运行轮次缺失、同时存在多个活动轮次或状态为 `stale`。
- 实际变化超出 `allowed_output_paths`，且不能明确归属为开工前已有变化或其它任务变化。

任一条件成立时保留真实现场并停止，不手工改状态文件，也不覆盖来源不明改动。

## 审核点

开工前复用已经通过的整套 `task_plan` 审核，不增加单任务中间审核。开发中以当前 `task-run.v1.json`、读取确认、上游哈希、测试和范围检查作为完成门禁。

## 停止位置

当前运行轮次已经 `active` 后，只执行当前任务。任务未通过测试、人工验收、反馈和范围门禁时不收口；任务完成后停止，不自动启动下一任务。
