---
name: sdlc-handoff
description: 输出可复制到新会话的交接提示词，对应 codex-sdlc handoff
---

# sdlc-handoff

这个 Skill 对应命令：`codex-sdlc handoff`

执行时请：

1. 说明本次会导出一段可复制到新会话的短交接提示词。
2. 默认执行 `codex-sdlc handoff`，不要主动使用全量模式。
3. 把输出按原样交给用户，方便直接复制到新会话或其他工具。
4. 默认 handoff 只服务“新会话接着当前下一步干活”，重点包含当前任务、为什么是这一步、上一轮实际做了什么、当前必须注意、接手后怎么做和执行边界。
5. 如果用户明确要完整状态、全部任务和最近验证，再执行 `codex-sdlc handoff --full`。
6. 补一句这不会替代正式的 `codex-sdlc finish`；正式归档仍走 `$sdlc-finish`。
7. 本阶段只生成交接提示词，不继续开发、不跑测试、不提交代码。
8. 提醒用户：新会话只执行交接里写的一个下一步，不能自动连续推进。
