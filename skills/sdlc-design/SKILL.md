---
name: sdlc-design
description: 为已确认需求建立 DES、设计计划、模块产物和总体说明，并完成唯一一次整体设计审核
---

# sdlc-design

把已确认需求转换成可追溯的模块化设计。技术方案原文保留在 MAT 中，DES 只保存稳定
引用；模块设计按当前需求实际需要组合，不创建空壳模块。

## 输入

- 状态为 `requirement_confirmed` 的当前 DRAFT。
- 适用的需求、资料和用户确认记录。
- 已归档为 `technical-solution` 的技术方案 `MAT-xxx`。
- 模型生成的 `design-reference.v1`、`design-plan.v1`、启用模块对应的
  `design-artifact.v1`，以及多模块时的 `design-summary.v1`。
- 真实项目规则、依赖、代码文件和上游产物组成的最小代码证据。

输入文件只写业务设计、显式引用和真实代码路径。正式编号、生产任务身份、输入哈希、
产物哈希和修订号由 CLI 记录，不能虚构或手工覆盖。

## 执行

### 1. 建立待确认 DES

技术方案原文尚未归档时，先用 `$sdlc-material` 以 `technical-solution` 类型归档。
模型按原文锚点和适用 FR 生成引用文件，再导入：

```bash
codex-sdlc design-reference DRAFT-001 --file tmp/design-reference.v1.json
```

导入后停下，等待用户通过 `$sdlc-design-accept` 确认 `DES-xxx`。没有已确认 DES
时不能生成设计计划。

### 2. 导入设计计划和模块产物

用户确认 DES 后再次进入本技能。模型读取当前需求确认、全部已确认 DES、适用 MAT 和
真实代码，选择 `data`、`api`、`page`、`component`、`security`、`deployment`、
`field`、`special` 中实际需要的模块：

```bash
codex-sdlc design-plan DRAFT-001 --file tmp/design-plan.v1.json
```

按计划逐个生成并导入启用模块。示例：

```bash
codex-sdlc design-artifact DRAFT-001 --file tmp/PAGE-001.design-artifact.v1.json
```

两个及以上启用模块必须生成总体说明，统一公共对象和跨模块关系：

```bash
codex-sdlc design-summary DRAFT-001 --file tmp/design-summary.v1.json
```

单个启用模块不需要空的总体说明。模块有 `open_questions`、状态为 `blocked`，或代码
证据已经变化时，先修正当前设计，不能创建审核。

### 3. 完成整体设计审核

设计完整并进入 `design_reviewing` 后，由设计生产任务创建唯一整体设计审核请求：

```bash
codex-sdlc review create --review-id REV-002 --stage integrated_design --owner DRAFT-001 --input .codex-sdlc/drafts/DRAFT-001/设计/design-plan.v1.json
```

对受管 DRAFT，CLI 会从当前结构化状态重新收集全部审核输入，并生成真实审核编号和固定
检查项；命令中的通用编号和输入参数不能缩减审核范围。

独立审核任务读取请求，原样复用请求的 `input_hashes` 生成 `review-result.v1`，再提交：

```bash
codex-sdlc review submit --request REV-002 --file tmp/design-review-result.v1.json
```

查看审核和 DRAFT 状态：

```bash
codex-sdlc review status --review REV-002
codex-sdlc draft refresh DRAFT-001
codex-sdlc draft status DRAFT-001
```

`draft refresh` 会按当前事件和受管审核登记重建阅读投影与产物索引，不改变审核结论。
`needs_fix` 时修正对应结构化输入并创建新轮次。只有当前、完整且为 `passed` 的整体设计
审核可以进入 `start_ready`。

## 输出

- `des-index.v1.json` 中待确认或已确认的 `DES-xxx` 引用。
- `design-plan.v1.json`、`code-evidence.v1.json` 和启用模块设计产物。
- 多模块时的 `design-summary.v1.json`。
- 唯一当前整体设计审核请求、结果和 `start_ready` 状态。

## 阻塞条件

- 当前需求确认缺失或已经失效。
- 技术方案 MAT 不存在、引用锚点无效，或 DES 尚未由用户确认。
- 计划包含不真实状态，启用模块缺产物，模块仍有问题，或总体说明缺失。
- 需求、MAT、DES、设计计划、模块产物、总体说明或代码证据在审核后变化。
- 审核未完成、结果为 `needs_fix` 或生产任务与审核任务身份相同。

阻塞时停在 DES 确认点、`designing` 或 `design_reviewing`，不能生成正式包。

## 停止位置

待确认 DES 导入后停止并推荐 `$sdlc-design-accept`。整体审核通过后核对 DRAFT 为
`start_ready`，再停止并推荐 `$sdlc-start`。
