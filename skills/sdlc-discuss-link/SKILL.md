---
name: sdlc-discuss-link
description: 已有正式需求后，把需求讨论草案纳入指定需求，对应 codex-sdlc discuss-link
---

# sdlc-discuss-link

这个 Skill 对应命令：`codex-sdlc discuss-link`

执行时请：

1. 先确认用户已经有正式需求编号，比如 `REQ-001`。
2. 如果用户指定了草案编号，执行 `codex-sdlc discuss-link REQ-001 CAP-001`。
3. 如果用户没有指定草案编号，执行 `codex-sdlc discuss-link REQ-001`，默认纳入当前所有待处理需求讨论草案。
4. 这一步只把需求讨论草案纳入需求决策，不创建新需求，不调整任务，不开始写代码。
5. 执行后告诉用户纳入了哪些 `CAP`。
6. 如果命令提示没有待纳入草案，先用 `$sdlc-status` 或 `$sdlc-next` 查看当前状态。
7. 纳入后如果还没有技术方案，建议 `$sdlc-design`；已有方案则 `$sdlc-next`。
8. 完成后必须停下，等待用户推进下一阶段。
