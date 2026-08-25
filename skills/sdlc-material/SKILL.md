---
name: sdlc-material
description: 把原始需求、技术方案、设计稿、接口资料和现场证据原样归档到未建档 DRAFT，对应 codex-sdlc material
---

# sdlc-material

把真实来源保存到当前未建档 DRAFT。这里只归档资料，不拆需求、不做设计、不写代码。

## 输入

- 明确的 `DRAFT-xxx`。
- 资料标题和资料类型。
- `--file`、`--url`、`--secret-reference` 三种来源中的一种。
- 可选的角色、适用范围、外部版本证据、访问条件和替代关系。
- 资料类型只能使用 CLI 当前支持的值：`requirement`、`technical-solution`、`ui-design`、`api-document`、`database-document`、`sample-data`、`account`、`environment`、`field-evidence`、`other`。

## 执行

项目内文件使用：

```bash
codex-sdlc material DRAFT-001 --title 原始需求 --type requirement --file docs/需求.md --role requirement --scope 当前需求
```

外部资料使用 `--url`。需要进入审核的外部资料同时提供当前
`external-version-evidence.v1`：

```bash
codex-sdlc material DRAFT-001 --title 接口文档 --type api-document --url https://example.com/api --version-evidence tmp/external-version-evidence.v1.json --access-condition public --sensitivity internal --role api-document --scope FR-001
```

秘密资料只保存受控引用，不把秘密原值写进资料正文：

```bash
codex-sdlc material DRAFT-001 --title 测试账号 --type account --secret-reference tmp/secret-reference.v1.json --sensitivity secret-reference --role account --scope 现场验证
```

资料修订使用 `--supersedes MAT-xxx` 明确替代旧资料，不能覆盖原记录。

## 输出

- 新的 `MAT-xxx`。
- DRAFT 下的稳定原文副本或受控外部引用。
- 当前 `material-manifest.v1.json` 和产物索引记录。
- CLI 生成的来源路径、编号和完整哈希。

## 阻塞条件

- 目标不是未建档 `DRAFT-xxx`。
- 三种来源没有提供、提供多种或来源不可读取。
- 文件不在项目内，外部版本证据与 URL 不匹配，或秘密引用的敏感级别不正确。
- 资料修订没有明确旧 `MAT-xxx`，或旧资料不属于当前 DRAFT。

遇到阻塞时保留原资料和 DRAFT 状态，不改用正式 `REQ`、任务证据或普通正文参数绕过。

## 停止位置

成功归档一份资料后停止，报告 `MAT-xxx`、稳定路径和适用范围。需要整理需求时进入
`$sdlc-discuss`；本技能不继续创建审核或正式建档。
