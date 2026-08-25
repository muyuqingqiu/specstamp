---
name: sdlc-light-start
description: 已下线的兼容命令，对应 codex-sdlc light-start
---

# sdlc-light-start

`codex-sdlc light-start` 已下线，不再支持一句话直接生成正式需求。

执行时请：

1. 不要再用 `$sdlc-light-start` 创建 REQ、任务或技术方案。
2. 小需求也走 DRAFT 主流程：先用 `$sdlc-discuss` 沉淀需求草稿，再用 `$sdlc-design` 和 `$sdlc-design-accept` 确认技术方案，最后用 `$sdlc-start` 正式建档。
3. 如果已经有结构化正式建档 JSON 包，使用内部兼容入口：`codex-sdlc start --file <json>`。
4. 历史 `flow_type = 轻量流程` 的旧需求仍可读取、导出、brief、doctor 和继续执行任务，不做批量迁移。
5. 本 Skill 只保留旧入口说明；执行 `codex-sdlc light-start "需求内容"` 只会输出下线提示，不会写入 `.codex-sdlc` 状态。
