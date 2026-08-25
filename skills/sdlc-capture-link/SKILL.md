---
name: sdlc-capture-link
description: 记录中途结论并关联需求；普通结论用 capture-link，开发测试经验优先改用 sdlc-lessons
---

# sdlc-capture-link

这个 Skill 对应命令：`codex-sdlc capture-link`

执行时请：

1. 确认用户给了需求编号和结论内容。
2. 如果内容是开发经验、测试经验、排查经验、模拟器操作方法、可复用命令或避免重复踩坑的说明，优先改用 `$sdlc-lessons add`；不要继续用 `capture-link --lesson` 作为新流程入口。
3. 如果上一轮 `$sdlc-task-done` 已经列出候选经验，用户说“加入全部”“加入第 1 条”“可以加入经验”“这条加入经验”等明确同意语句时，按候选经验原文整理为经验内容，并执行 `$sdlc-lessons add "经验内容" --level requirement/cross-requirement/project/agents-candidates`；能从上下文确定需求编号时不再追问。
4. 记录候选经验前再做一次价值检查。单个 UI 样式值、单个 Figma 节点、单个文案或颜色、一次性验收修正、只影响一个组件内部的局部实现，更适合放在任务变更报告或代码注释里，不写入经验库。
5. 如果用户只说“加入经验”但当前会话里有多条候选经验且无法判断要加哪条，先询问用户选择全部还是指定编号。
6. 普通结论执行 `codex-sdlc capture-link REQ-001 "结论内容"`。
7. 有涉及文件、命令或待确认问题时继续用 `--file`、`--command`、`--question` 记录。
8. 告诉用户生成了哪条 capture，并确认已关联到需求；经验内容请使用 `$sdlc-lessons`，后续任务通过正式引用和读取清单获取相关内容。
9. 本阶段只记录并关联结论，不改任务、不写代码、不跑测试。
10. 完成后必须停下，等待用户推进下一阶段。
