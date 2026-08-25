---
name: sdlc-change-plan
description: 在结构化变更包中整理任务影响、预计任务计划和审核关系
---

# sdlc-change-plan

本技能处理结构化 CHG 的任务规划，不把业务变化作为命令行正文提交。

执行时请：

1. 读取 CHG 工作区的 `status.json`、`change-package.v1`、`projected-task-plan.v2`，以及当前正式 `task-plan.v2`、所有相关 `task.v2`、`task-coverage.v1` 和 `task_plan` 审核状态。
2. 把每项任务影响明确写入 `change-package.v1` 的 `task_impacts`：
   - `restore`：已完成任务需要按新要求重新处理。
   - `add`：需要新增独立交付任务。
   - `close`：已有未完成任务被当前变更明确替代。
   - `unaffected`：任务不受影响，并给出可以核对的依据。
3. 同步生成完整 `projected-task-plan.v2`。新增任务使用稳定 `@client:` 临时引用，依赖、顺序和任务身份必须能由 CLI 一次性重写；不能提前伪造正式 `T-xxx`。
4. 每个新增或恢复任务都要有明确目标、交付结果、FR/AC/设计引用、自动测试、人工验收、不包含内容、允许修改范围和完成条件。验收或回归内容写入任务测试与验收字段，不单独拆成空泛任务。
5. 对照 `task-coverage.v1` 检查 FR、设计产物、AC 和当前 CHG 的主责任关系；没有开发动作的内容必须有明确的 `no_development_items` 依据。
6. 任务计划发生变化时，在 `review_impacts` 中登记 `task_plan`，并在变更生效后重新创建整套任务审核。旧审核输入哈希不能继续复用。
7. 重新运行 `codex-sdlc change-package` 的六文件固定参数提交修订结果。CLI 只接受结构化文件、显式引用和哈希，不会根据标题或摘要补任务。
8. 变更生效并通过新的 `task_plan` 审核后，需求状态才可进入 `ready_for_development`。任务开工直接使用 `$sdlc-task REQ-xxx T-xxx`。
