---
name: sdlc-change
description: 创建 CHG 工作区，归档变更资料并提交完整结构化预计版本
---

# sdlc-change

## 输入

- 当前正式需求编号和用户已经明确的业务变化。
- 当前需求、设计、测试矩阵、引用索引和任务计划五份正式基础版本。
- 变更资料原文、外部版本证据或秘密引用。
- 模型整理的 `change-package.v1` 和五份完整预计版本。

业务方向仍有多个合理选择时先确认。CLI 只校验结构、编号、引用、哈希、显式影响和状态转换，不从自然语言正文猜受影响对象。

## 执行

1. 为同一次创建动作生成稳定请求键，重试时复用原键：

`codex-sdlc change-create REQ-001 --request-key change-request-001`

2. 记录命令返回的 `CHG-xxx` 和工作区路径，读取 `status.json`。只使用其中 `base_versions` 固定的五份基础版本路径和 SHA-256。
3. 把会影响正式需求、技术方案、设计或引用的资料归档到当前 CHG。普通文件示例：

`codex-sdlc change-material REQ-001 CHG-001 --type requirement --file docs/变更说明.md`

外部地址使用 `--url`，需要稳定版本时同时使用 `--version-evidence`；秘密引用使用 `--secret-reference`。测试日志、截图和当前任务现场证据继续进入对应任务运行轮次。
4. 模型对照基础版本、已归档资料和真实任务状态，生成：
   - `change-package.v1`
   - `projected-requirement.v2`
   - `projected-design.v2`
   - `projected-test-matrix.v2`
   - `projected-reference-index.v2`
   - `projected-task-plan.v2`
5. `change-package.v1` 必须写全 `base_versions`、`source_refs`、需求和验收操作、设计和资料操作、`task_impacts`、`review_impacts`、`open_questions`。五份预计版本必须包含变更后的完整当前内容，不能只写差异或附录。
6. 提交结构化变更包和五份预计版本：

`codex-sdlc change-package REQ-001 CHG-001 --package tmp/change-package.v1.json --projected-requirement tmp/projected-requirement.v2.json --projected-design tmp/projected-design.v2.json --projected-test-matrix tmp/projected-test-matrix.v2.json --projected-reference-index tmp/projected-reference-index.v2.json --projected-task-plan tmp/projected-task-plan.v2.json`

7. 按 `review_impacts` 复用 `requirement_split`、`integrated_design`、`task_plan` 三类正式审核。需求变化示例：

`codex-sdlc review create --review-id REV-002 --stage requirement_split --owner CHG-001 --input tmp/projected-requirement.v2.json`

8. 审核者在独立任务中提交 `review-result.v1`，再读取当前审核状态：

`codex-sdlc review submit --request REV-002 --file tmp/change-review-result.v1.json`

`codex-sdlc review status --review REV-002`

## 输出

- `changes/CHG-xxx/status.json` 和已归档的变更资料。
- 当前 CHG 的 `change-package.v1.json`。
- 五份经过规范等值校验的 `projected-*.v2.json`。
- 受影响的正式审核请求、审核结果和当前状态。
- 正式 `effective` 和版本目录在用户确认前保持原样。

## 阻塞条件

- 正式需求不存在、稳定请求键不合法或 CHG 工作区身份不一致。
- 任一基础版本路径、内容或 SHA-256 漂移。
- 变更资料缺少稳定来源，外部资料缺少规定的版本证据。
- 操作关系、正式编号、来源引用、任务影响或审核影响不完整。
- `open_questions` 非空。
- CLI 计算结果与任一完整预计版本不等值。
- 受影响审核缺失、未通过、已过期，或审核者与生产者身份相同。
- 活动任务受影响但尚未按正式保护流程处理。

任一条件成立时只修正当前 CHG 工作区，不直接修改 `effective`、版本目录、任务状态或当前任务运行轮次。

## 审核点

变更不增加第四类固定审核。`review_impacts` 影响需求时重做 `requirement_split`，影响设计时重做 `integrated_design`，影响任务时重做 `task_plan`。每类审核都绑定当前 CHG 中的完整预计版本和变更包哈希。

## 停止位置

完整预计版本已经提交，全部受影响审核已经得到当前有效结果后停止，等待用户核对预计需求结果。用户明确确认后改用 `$sdlc-change-accept REQ-001 CHG-001`；本技能不替用户确认，也不直接生效变更。
