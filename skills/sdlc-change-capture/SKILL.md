---
name: sdlc-change-capture
description: 把指定 capture 作为结构化 CHG 的来源资料
---

# sdlc-change-capture

本技能不会把 capture 的自然语言正文直接交给变更命令。

执行时请：

1. 确认正式需求编号、capture 编号和 capture 的项目内来源文件，核对它确实属于当前项目和当前需求。
2. 为同一次转换生成并复用稳定请求键，例如 `cap-001-change-001`：
   `codex-sdlc change-create REQ-001 --request-key cap-001-change-001`
3. 记录返回的 CHG 工作区，使用 `codex-sdlc change-material REQ-001 CHG-001 --type requirement --file <capture来源文件>` 保存原始来源，取得 `CMAT-xxx`、路径和 SHA-256。
4. 模型读取 capture、当前正式版本和相关任务，把 `CMAT-xxx` 写入 `change-package.v1` 的 `source_refs`，并生成五份完整预计结果。capture 只是来源证据，不能代替明确的需求、验收、设计、资料和 `task_impacts` 操作。
5. 按 `$sdlc-change` 的六文件固定参数执行 `codex-sdlc change-package`，再按 `review_impacts` 创建审核。
6. 输出 capture、REQ、CHG、CMAT 和输入身份的对应关系。路径、所有权、哈希或引用不一致时停止，不写正式版本。
7. 变更包与审核完成后停止，等待用户确认预计需求结果。
