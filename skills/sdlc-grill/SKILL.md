---
name: sdlc-grill
description: 在需求、产品设计、技术方案、任务执行发现目标冲突或前后矛盾时做受控质询，并记录关键业务或技术决策
---

# sdlc-grill

用户侧指令：`$sdlc-grill`
底层记录：`codex-sdlc grill`

## 使用场景

1. 用户提出需求、产品设计或功能想法，但目标、范围、验收、异常处理或不做范围还不清楚。
2. `$sdlc-start` 正式建档前，发现需求点之间有冲突，或用户口径和已记录资料不一致。
3. `$sdlc-design` 记录技术方案前，发现技术选型、数据流、状态保存、接口、测试策略或风险处理存在多个合理方向。
4. `$sdlc-task` 阶段发现当前任务合同、正式引用、当前代码、用户资料和前置任务产出之间有矛盾，会影响实现方向。
5. `$sdlc-change-accept`、`$sdlc-change-plan` 处理较大变更时，发现变更影响范围、任务主线或验收口径不清楚。
6. Goal 模式自动推进时，主 agent 发现业务、需求、技术或任务执行方向存在疑问，需要先做判断和留痕。

## 执行规则

1. 质询只处理会影响业务目标、需求范围、技术方案、验收口径或任务实现方向的问题；不是下一步推荐，也不是模板化确认。
2. 不要因为“执行包已经 ready”“下一步是否开工”“是否继续下一阶段”这类流程问题单独记录质询；这些交给 `$sdlc-next`、当前指令输出或用户显式命令处理。
3. 普通阶段只有真正需要用户做业务或技术判断时才问用户；可以先查资料、整理问题和给推荐答案，但不能把主 agent 判断当成用户回答写入质询结论。
4. 普通阶段一次最多只问用户一个真正关键的问题，并给出推荐答案或推荐选择。
5. 普通阶段没有发现业务、需求、技术或任务执行矛盾时，不记录 `GRILL-xxx`，直接回到当前阶段的下一步推荐。
6. Goal 模式允许主 agent 自问自答，但也只在发现业务、需求、技术或任务执行矛盾时记录；如果没有疑问，不需要为了“质询通过”生成 `GRILL-xxx`。
7. Goal 模式缺用户决策、缺账号环境、缺设计资料且资料无法从 `$sdlc-material` 找到时，才停下来问用户。
8. 本 Skill 不写业务代码、不跑业务测试、不提交 Git、不推进下一个阶段。
9. 质询有结论时，用 `codex-sdlc grill` 留痕；用户在 Codex 对话里不需要手写完整参数，agent 要把自然语言整理成命令。

## 记录方式

常用记录形式：

- 需求阶段：`codex-sdlc grill "夜间模式跟随系统关闭时，右侧回显到底显示页面背景颜色还是固定文案需要用户确认。" --mode requirement --status needs_user --question "关闭夜间模式跟随系统时，右侧回显用页面背景颜色还是固定文案？" --recommendation "推荐回显页面背景颜色，因为用户能直接看到当前选择。"`
- 产品设计阶段：`codex-sdlc grill "用户确认阅读模式背景亮度右侧回显规则：关闭跟随系统时显示背景色，开启跟随系统时显示固定文案。" --mode product --status resolved --answer "按这个回显规则继续。" --source "用户回答"`
- 技术方案阶段：`codex-sdlc grill "用户确认设置搜索语言切换后需要动态刷新搜索数据，不继续使用静态常量。" --mode design --status resolved --answer "按动态组装数据源实现。" --source "用户回答"`
- 任务运行阶段：`codex-sdlc grill "T-004 当前任务合同和生效需求冲突，需要确认是否先处理需求变更。" --requirement REQ-001 --task T-004 --mode task --status needs_user --question "先完成需求变更，还是继续按当前任务合同执行？" --recommendation "推荐先走 $sdlc-change-accept，让当前生效需求更新后再重新核对任务。"`
- Goal 模式：`codex-sdlc grill "主 agent 根据当前生效需求和代码确认：T-004 的右侧回显应以最新变更为准，旧任务说明已过期。" --requirement REQ-001 --task T-004 --mode goal --status resolved --answer "按最新生效需求执行。" --source "Goal 自答"`

如果确实需要用户回答：

`codex-sdlc grill "缺少设计资料，当前无法判断页面样式。" --requirement REQ-001 --task T-004 --mode task --status needs_user --question "请提供设计稿、截图或 Figma 链接。" --recommendation "优先用 $sdlc-material 保存资料，再重新核对当前任务运行基线。"`

## 阶段边界

1. 需求讨论里，质询结束后只回到 `$sdlc-discuss` 或 `$sdlc-design`。
2. 正式建档里，质询结束后只回到 `$sdlc-start`。
3. 技术设计里，质询结束后只回到 `$sdlc-design` 或 `$sdlc-design-accept`。
4. 任务运行里，只有发现业务、需求、技术或任务执行矛盾时才使用质询；质询结束后只回到 `$sdlc-task`、`$sdlc-material`、`$sdlc-change` 或 `$sdlc-plan-*`。
5. Goal 模式里，质询只是主 agent 的阶段内判断记录，不允许因为质询通过就跳过 `$sdlc-task`、`codex-sdlc task-read-confirm`、`$sdlc-task-done` 的原有顺序。
