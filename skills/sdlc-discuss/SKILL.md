---
name: sdlc-discuss
description: 从 DRAFT 原始资料生成结构化需求拆分和覆盖关系，完成需求审核并记录用户确认
---

# sdlc-discuss

把用户已经确认的需求内容整理成结构化需求合同。模型负责理解业务和生成输入文件，
CLI 只校验结构、编号、引用、哈希和状态。

## 输入

- 当前未建档 DRAFT，或用户允许创建的新 DRAFT。
- 已归档的需求类 `MAT-xxx`、有效外部版本证据和用户决定。
- 模型生成的 `requirement-split.v1` 与 `requirement-coverage.v1`。
- 独立审核任务生成的 `review-result.v1`。

需求拆分必须覆盖当前适用原始资料、FR、GR、验收和测试关系。内容按 JSON Schema
显式表达，不依赖固定 Markdown 标题，不让 CLI 从自然语言猜业务含义。

## 执行

1. 当前没有 DRAFT 时创建：

```bash
codex-sdlc draft create 订单导出
```

2. 原始资料还没有 `MAT-xxx` 时先用 `$sdlc-material` 归档。发现真正影响范围或验收的
   问题时记录问题，得到用户答复后再解决：

```bash
codex-sdlc draft question DRAFT-001 "是否允许访客导出订单"
codex-sdlc draft resolve DRAFT-001 "是否允许访客导出订单" --decision "只允许已登录运营人员导出"
```

3. 模型按当前资料和决定生成两份结构化输入，再原子导入：

```bash
codex-sdlc draft requirements DRAFT-001 --split-file tmp/requirement-split.v1.json --coverage-file tmp/requirement-coverage.v1.json
```

4. 状态进入 `requirement_reviewing` 后，由需求生产任务创建唯一审核请求：

```bash
codex-sdlc draft requirement-review create DRAFT-001
```

5. 独立审核任务读取请求中的全部受控输入，生成 `review-result.v1` 并提交。生产任务和
   审核任务必须使用不同的真实任务身份：

```bash
codex-sdlc review submit --request REV-001 --file tmp/requirement-review-result.v1.json
```

6. 查看当前审核。`needs_fix` 时修正结构化需求输入，重新导入并创建新审核轮次：

```bash
codex-sdlc draft requirement-review status DRAFT-001 --review REV-001
```

7. 审核为当前 `passed` 后停止等待用户确认。只有用户明确确认当前需求时才绑定该轮审核：

```bash
codex-sdlc draft requirement-confirm DRAFT-001 --review REV-001
```

8. 核对状态已经进入 `requirement_confirmed`：

```bash
codex-sdlc draft status DRAFT-001
```

## 输出

- `requirement-split.v1.json` 和 `requirement-coverage.v1.json`。
- 需求审核输入快照、当前 `REV-xxx` 请求和受管审核结果。
- 用户确认后生成的 `requirement-confirmation.v1.json` 与 `RCF-xxx`。
- 与真实资料、决定和审核绑定的当前 DRAFT 状态。

## 阻塞条件

- 原始需求资料缺失、外部资料没有稳定版本或资料引用失效。
- 结构化需求仍有 `open_questions`、未覆盖项或无效 FR、GR、验收和测试引用。
- 审核尚未完成、结果为 `needs_fix`、审核输入已经变化或审核任务身份不独立。
- 用户没有明确确认，或指定的 `REV-xxx` 不是当前唯一有效审核。

阻塞时停在 `discussing` 或 `requirement_reviewing`，不能直接进入设计或正式建档。

## 停止位置

审核通过但用户未确认时，停在需求确认点。用户确认成功并核对
`requirement_confirmed` 后停止，只推荐 `$sdlc-design`。
