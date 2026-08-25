---
name: sdlc-accept
description: 用户确认需求真正结束，对应 codex-sdlc accept
---

# sdlc-accept

命令：`codex-sdlc accept`

执行时请：

1. 只有用户明确确认需求真的结束时才执行。开发者或 AI 自己不能替用户接受需求。
2. 按会话上下文找刚完成全部任务、刚通过验证且最相关的需求。
3. 只有一个高相关已完成需求时执行 `codex-sdlc accept`。
4. 多个需求高度相关或 CLI 列候选时，转述候选并让用户选择，再执行 `codex-sdlc accept REQ-001`。
5. 本阶段只做用户最终接受，不补代码、不补任务、不跑测试、不提交代码。
6. 还有未完成任务时不要接受，只说明缺口并推荐 `$sdlc-next`；如果下一步是开发任务，由 `$sdlc-task REQ-001 T-001` 按正式任务引用直接开工。
7. 接受完成后必须停下；下一步优先推荐 `$sdlc-docs REQ-001`，生成给后续开发维护看的需求逻辑梳理文档。
8. 如果需求维护文档已经生成，下一步再推荐 `$sdlc-finish` 或 `$sdlc-status`。
