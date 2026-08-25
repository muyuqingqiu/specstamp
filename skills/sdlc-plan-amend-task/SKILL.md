---
name: sdlc-plan-amend-task
description: 修改未完成任务的结构化目标、测试、验收或依赖
---

# sdlc-plan-amend-task

正式入口：`codex-sdlc plan-amend-task`。

执行时请：

1. 只修改未完成任务。先读取 `task-plan.v2`、目标 `task.v2`、`task-coverage.v1`、当前 task-run 和 `task_plan` 审核状态。
2. 已完成或已关闭任务不能直接改写；历史实现问题使用 `$sdlc-fix`，需求或设计变化使用 `$sdlc-change`，当前验收反馈需要新运行轮次时使用 `$sdlc-task-restore`。
3. 执行示例：
   `codex-sdlc plan-amend-task REQ-001 T-026 --summary "新的任务目标" --test-item "验证内容" --manual-check "人工确认内容" --depends "T-022"`
4. 旧测试或验收口径会误导执行时使用 `--replace-test-items` 或 `--replace-manual-checks`；清空依赖使用 `--depends ""`。不要把活动任务改成依赖未完成任务。
5. 命令成功后重新读取目标 `task.v2`、`task-plan.v2` 和 `task-coverage.v1`，核对目标、测试、验收、依赖和覆盖关系完整一致。
6. 修改使旧 `task_plan` 审核变为 `stale`。重新创建整套任务审核；当前输入通过前不得开工或继续完成任务。
7. 本阶段不创建新的 task-run、不写业务代码、不跑业务测试。
