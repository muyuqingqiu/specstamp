---
name: sdlc-design-accept
description: 用户确认 DRAFT 中当前技术方案原文对应的结构化 DES 引用
---

# sdlc-design-accept

本技能只记录用户对技术方案引用的确认，不生成设计计划、不修改任务、不写代码。

## 输入

- 状态为 `requirement_confirmed` 的当前 DRAFT。
- 已通过 `design-reference` 导入、来源仍有效的 `DES-xxx`。
- 用户对具体 `DES-xxx` 的明确确认；有多个候选时必须先让用户选定。

## 执行

确认前读取 `des-index.v1.json`，核对 DES 的原始 `MAT-xxx`、适用 FR、原文锚点和当前
需求确认。用户明确同意后执行：

```bash
codex-sdlc design-reference-confirm DRAFT-001 DES-001
```

核对确认后的 DRAFT 状态：

```bash
codex-sdlc draft status DRAFT-001
```

技术方案需要调整时，先把新原文作为 `technical-solution` MAT 归档，再创建新的
`design-reference.v1` 修订；不能覆盖已确认 DES，也不能调用旧自由文本设计入口。

## 输出

- `des-index.v1.json` 中状态为 `confirmed` 的当前 `DES-xxx`。
- 与当前需求确认、原始 MAT、适用 FR 和原文锚点绑定的确认记录。
- CLI 生成的确认时间、完整哈希和当前 DRAFT 状态。

## 阻塞条件

- 用户没有明确确认，或多个 DES 候选尚未选定。
- 当前需求确认已失效。
- DES 不属于当前 DRAFT、原始 MAT 或锚点已变化、适用 FR 已变化。
- 需要改变技术方案原文，却没有先建立明确修订。

阻塞时不写确认记录，返回 `$sdlc-design` 修正来源或引用。

## 停止位置

DES 确认并核对状态后停止，只推荐再次进入 `$sdlc-design`，继续生成设计计划、模块产物
和整体设计审核。
