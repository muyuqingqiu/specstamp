---
name: sdlc-plan-add-task
description: 向结构化任务计划追加一条完整任务合同
---

# sdlc-plan-add-task

正式入口：`codex-sdlc plan-add-task`。

执行时请：

1. 读取 `task-plan.v2`、已有 `task.v2`、`task-coverage.v1` 和当前 `task_plan` 审核，确认新增内容是当前需求下的执行任务。新的业务规则或设计变化改用 `$sdlc-change`。
2. 一次只追加一条任务，明确标题、目标、FR 覆盖、自动测试、人工验收、任务类型和模型档位：
   `codex-sdlc plan-add-task REQ-001 "任务标题" --summary "任务目标" --coverage FR-001 --test-item "自动测试项" --manual-check "人工验收点" --task-kind generic --model-tier medium`
3. CLI 只校验显式字段，不从标题推断业务关系。多个任务分别调用，并在每次成功后记录正式 `T-xxx`。
4. 重新读取 `task-plan.v2`、新增 `task.v2` 和 `task-coverage.v1`，核对任务身份、顺序、依赖、FR/AC 主责任和完整字段都一致。
5. 新增任务使旧 `task_plan` 审核变为 `stale`。创建新的整套任务审核并等待当前输入通过；通过前不开始任务。
6. 本阶段不创建 task-run、不写业务代码、不跑业务测试。
