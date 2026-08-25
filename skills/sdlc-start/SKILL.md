---
name: sdlc-start
description: 把 start_ready DRAFT 的完整产物清单整理成 formal.v3，并通过 codex-sdlc start --file 正式建档
---

# sdlc-start

`$sdlc-start` 只消费当前 `start_ready` DRAFT。模型整理文档优先 `formal.v3` 清单，CLI
负责重新核对来源、审核、引用、哈希和原子归档。

## 输入

- 状态为 `start_ready` 的当前 DRAFT。
- 当前 `artifact-index.v1.json`。
- 唯一有效的需求审核 `REV-xxx` 和整体设计审核 `REV-xxx`。
- 模型生成的 `formal.v3` JSON 文件。

formal.v3 只保存清单和引用，字段固定为：

- `formal_contract_version`：固定为 `formal.v3`。
- `workflow_profile`：固定为 `document-first.v1`。
- `source_draft_id`：当前 `DRAFT-xxx`。
- `source_revision_sha256`：当前产物索引记录的 DRAFT 修订摘要。
- `reviews`：当前需求审核和整体设计审核编号。
- `artifact_index`：产物索引的来源路径、正式归档路径和完整哈希。
- `artifact_manifest`：所有 `include_in_formal` 产物的来源路径、归档路径、编号、哈希和审核关系。
- `open_questions`：必须为空数组。

需求正文中的 `functional_requirements`、`acceptance_criteria`、`test_cases` 保留在已经
审核通过的需求产物里，formal.v3 不重复写入这些结构化正文。模型只从当前产物索引逐项
整理清单，不虚构运行身份、来源、审核编号或哈希。

## 执行

建档前重新读取 DRAFT 状态、两类审核状态和产物索引。清单按 `source_path` 排序并与
索引中的正式归档集合完全一致，然后执行：

```bash
codex-sdlc start --file tmp/formal.v3.json
```

建档成功后读取正式状态：

```bash
codex-sdlc status
```

## 输出

- 新的正式 `REQ-xxx`。
- `original/formal.v3.json`、正式原文归档和 `artifact-index.v1.json`。
- `effective/requirement.current.*`、`effective/design.current.*` 和
  `effective/test-matrix.current.*`。
- 正式 `reference-index.v1.json`、追溯关系和已建档 DRAFT 状态。

## 阻塞条件

- DRAFT 不是 `start_ready`，仍有问题或必要设计模块为 `blocked`。
- 需求审核或整体设计审核缺失、未通过、身份不独立或已经失效。
- 产物索引与真实文件不一致，来源文件缺失，清单漏项、重复、顺序错误或哈希变化。
- formal.v3 不是 `document-first.v1`，引用错误，或试图嵌入另一套正文和额外阶段。

失败时不得留下新事件、半成品 REQ 或错误 DRAFT 状态；修正当前输入后重试同一正式入口。

## 停止位置

建档成功后停止，只报告 `REQ-xxx`、正式目录和下一步 `$sdlc-tasks REQ-xxx`。本技能
不拆任务、不开始开发、不执行 Git 写操作。
