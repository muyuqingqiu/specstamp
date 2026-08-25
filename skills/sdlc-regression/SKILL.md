---
name: sdlc-regression
description: 在 verifying 状态消费已关闭 task-run 证据并执行需求级回归
---

# sdlc-regression

命令：`codex-sdlc regression REQ-001`

1. 需求状态只使用 `planning_tasks`、`ready_for_development`、`developing`、`verifying`、`accepted`；正常需求级回归入口只在 `verifying` 使用。
2. 先确认全部正式任务为 `done` 或 `closed`，每个完成任务都有最终 `closed` task-run，读取清单、测试、人工验收、反馈和完成证据可以互相核对。
3. 回归范围来自当前正式需求、技术方案、测试矩阵、任务覆盖和已关闭 task-run，不从展示摘要猜测。
4. 执行正式回归命令、可重复脚本和人工验收，保存命令、整数退出码、结果、来源文件和 SHA-256 原值。
5. 任一 task-run 缺失、失效、证据不完整、任务未完成或正式输入发生变化时停止回归，指出具体任务和修复入口，不能猜测通过。
6. 回归失败属于原任务问题时调用 `codex-sdlc task-restore`恢复对应任务，需求回到 `developing`；需求或设计发生变化时进入正式变更流程。
7. 回归全部通过后保持 `verifying`，等待用户执行需求接受；用户确认后需求才进入 `accepted`。
8. 需求仍为 `planning_tasks`、`ready_for_development` 或 `developing` 时，不跳过未完成任务提前回归。
9. 本技能不自动接受需求，不自动开始新任务。
