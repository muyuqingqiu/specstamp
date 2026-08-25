---
name: sdlc-doctor-deep
description: 执行只读深度体检，对应 codex-sdlc doctor-deep
---

# sdlc-doctor-deep

这个 Skill 对应命令：`codex-sdlc doctor-deep`

执行时请：

1. 先说明这条指令只检查，不会自动修复。
2. 执行 `codex-sdlc doctor-deep`。
3. 重点说明生成区是否被手动修改、是否存在旧变更状态残留、任务版本和 FR/TC 覆盖关系是否异常。
4. 旧项目资料差异不影响普通任务开工、测试或验收；需要继续使用时，先导入为 SDLC 原生材料、经验或任务说明。
5. 同时说明备份恢复相关检查：`.codex-sdlc/identity.json` 是否和当前 Git 分支、工作树匹配，最近项目级备份是否存在，活跃需求是否有需求级备份，需求备份是否包含事件切片、current 文档、版本目录、任务运行轮次和验证资料。
6. 如果提示身份不匹配，停止推进开发，只推荐 `$sdlc-backup-list` 和 `$sdlc-restore --dry-run`。
7. 如果提示旧变更状态残留，先说明含义：历史事件里把变更标成已处理，但缺少 `change-map.md` 覆盖关系，或关联任务还没完成。
8. 如果提示任务版本或覆盖关系异常，先推荐 `$sdlc-doctor-repair` 重建和修复状态快照；仍异常时补齐任务计划、正式引用或测试项。
9. 如果输出说明可执行 `$sdlc-doctor-repair`，只推荐 `$sdlc-doctor-repair`，不要推荐裸 CLI。
10. 如果输出说明无法安全推断覆盖任务，不要替用户猜任务；已生效或旧 `pending` 变更优先用 `$sdlc-change-plan`，明确补任务、调顺序或关任务时再用 `$sdlc-plan-*`。
11. 如果输出提醒冲突，不要覆盖文件，先请用户确认处理方式。
12. 本阶段只读检查，不创建需求、不改任务、不开始开发。
