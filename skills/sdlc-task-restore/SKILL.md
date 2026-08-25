---
name: sdlc-task-restore
description: 用结构化反馈关闭旧运行轮次并创建新的读取轮次
---

# sdlc-task-restore

正式入口：`codex-sdlc task-restore`、`codex-sdlc task-read-confirm` 和 `codex-sdlc task-run-check`。

执行时请：

1. 先读取状态并确定任务身份：当前 `doing` 任务中的开发问题继续当前轮次；刚完成、待人工检查或测试失败任务的验收反馈使用本技能；历史任务问题使用 `$sdlc-fix`；需求或设计变化使用 `$sdlc-change`。
2. 反馈必须明确。多个候选任务同样相关时让用户选择，不能根据标题猜任务。
3. 把用户反馈整理成一条明确的恢复原因，写清没有通过的验收点和需要重新处理的范围。不要把需求或设计变化伪装成任务恢复原因。
4. 执行：
   `codex-sdlc task-restore REQ-001 T-001 "验收发现无权限账号仍能提交订单，需要按原任务权限规则返工"`
5. 命令应关闭旧 `task-run.v1`，保留旧轮次的测试、反馈、证据和读取清单，再分配递增的新运行号，创建新的 `task-run.v1` 与 `task-read-manifest.v1`。新轮次状态必须是 `reading`，`current.json` 只指向新轮次。
6. 读取新 `task-read-manifest.v1` 列出的完整任务文档、FR/GR、技术方案、设计、资料和前置任务交付物，逐项核对路径、定位与 SHA-256。
7. 使用 CLI 输出的清单哈希执行：
   `codex-sdlc task-read-confirm REQ-001 T-001 --manifest-sha256 <SHA-256>`
8. 确认后执行 `codex-sdlc task-run-check REQ-001 T-001`。只有当前轮次变为 `active` 且基线检查通过，才能继续修复开发。
9. 反馈缺失、任务身份不一致、旧轮次不可恢复、清单漂移或线程身份不符时命令应非零退出。不得覆盖旧轮次、复用旧清单哈希或跳过读取确认。
