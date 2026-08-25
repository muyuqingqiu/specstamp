---
name: sdlc-capture-change
description: 把中途结论归档为结构化变更工作区的来源资料
---

# sdlc-capture-change

本技能先保存中途结论，再进入结构化 CHG 主线，不使用接收业务正文的旧入口。

执行时请：

1. 确认正式需求编号和要转入变更的 capture。capture 必须已经有稳定编号和项目内来源文件；还没有 capture 时先用 `$sdlc-capture` 保存结论并停下。
2. 为这次转换生成稳定请求键，重试复用同一个键：
   `codex-sdlc change-create REQ-001 --request-key cap-001-to-change`
3. 使用 `codex-sdlc change-material REQ-001 CHG-001 --type requirement --file <capture来源文件>` 归档原始来源，核对返回的 `CMAT-xxx` 和文件 SHA-256。
4. 模型把结论整理到 `change-package.v1` 的显式操作中，并生成五份完整预计结果；来源引用使用归档后的 `CMAT-xxx`，不能从 capture 标题猜正式 FR、AC、设计或任务关系。
5. 按 `$sdlc-change` 的固定流程执行 `codex-sdlc change-package` 并创建必要审核。
6. 输出 capture、REQ、CHG、CMAT、工作区和输入身份。结构化包未提交或审核未完成时不进入生效步骤。
7. 完成后等待用户确认预计需求结果，再使用 `$sdlc-change-accept REQ-001 CHG-001`。
