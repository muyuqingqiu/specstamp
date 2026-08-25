---
name: sdlc-task-done
description: 消费当前 active task-run 的证据和范围门禁，严格完成单个任务
---

# sdlc-task-done

命令：`codex-sdlc task-done REQ-001 T-001`

1. 需求状态只使用 `planning_tasks`、`ready_for_development`、`developing`、`verifying`、`accepted`；当前任务收口时需求应为 `developing`。
2. 先读取当前 `task-run.v1.json`、`task-read-manifest.v1.json` 和 `current.json`，三者必须指向同一运行号且哈希一致。
3. task-run 必须为 `active`，并且已有同线程读取确认。`reading`、`stale`、`closed`、运行轮次缺失或读取清单失效都不能完成任务。
4. 按正式任务合同执行全部自动测试、可重复脚本和人工验收。每条证据用 `codex-sdlc task-evidence`绑定当前运行轮次，保存命令、整数退出码、结果、来源文件和 SHA-256 原值。
5. 任务有 FR、AC、TC 时逐项记录处理和验证结论；有反馈时逐条记录处理结果。不能用一句“测试通过”代替结构化证据。
6. 执行 `codex-sdlc task-run-check REQ-001 T-001`，重新核对整套任务审核、正式输入、读取清单、前置任务交付物、项目规则、工作树身份和允许输出范围。
7. 任一测试失败、人工验收缺失、反馈未处理、上游漂移或范围外变化都停止完成，保留已有证据并报告准确原因。
8. 全部门禁通过后执行 `$sdlc-task-done REQ-001 T-001`。命令成功后 task-run 为 `closed`，任务为 `done`；还有可执行任务时需求回到 `ready_for_development`，全部任务完成后需求进入 `verifying`。
9. 完成失败时不能手改任务状态，也不能把失败结果写成完成。
10. 本技能只收口当前任务，不自动开始下一任务，不自动接受需求。
