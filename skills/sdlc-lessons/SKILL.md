---
name: sdlc-lessons
description: 管理由 Codex 明确提炼的需求级、跨需求、项目级和 AGENTS 候选经验
---

# sdlc-lessons

用户侧指令：`$sdlc-lessons`
底层执行：`codex-sdlc lessons`

执行时请：

1. 本指令只管理“经验”，不保存用户提供的需求背景资料。设计稿、截图、Figma 链接、测试账号、环境说明和产品背景继续使用 `$sdlc-material`。
2. 经验来自开发、测试、回归和排查后的结论，例如真实可用命令、通用代码入口、后续任务要复用的方法、容易踩坑的旧逻辑、模拟器验收路径。
3. 只有需求级经验写入需求包 `requirements/<REQ>/lessons.md`；跨需求、项目级和 AGENTS 候选经验写入 `.codex-sdlc/lessons/`，不归属到单个需求。
4. 跨需求和项目级经验必须保留来源引用：来源需求、来源任务、来源文件、来源分支、来源 worktree 或快照。
5. `AGENTS.md` 是当前项目规则来源，不是普通历史经验。任务开工时按任务相关文件的目录链路读取适用的 `AGENTS.md`，并把命中的规则纳入当前读取确认；不要把整份 `AGENTS.md` 重复写进经验库。
6. 经验是否值得记录、应该是什么级别，全部由 Codex 根据任务目标、复用范围和后续风险判断。CLI 不扫描正文、不做关键词匹配、不推断级别。
7. 判断级别时按实际复用范围写：只服务当前需求的是需求级；同项目多个需求会复用但不适合写进 `AGENTS.md` 的是跨需求级；整个项目长期都该遵守的是项目级；需要用户再决定是否写入 `AGENTS.md` 的才是 AGENTS 候选。
8. 新增经验用 `codex-sdlc lessons add "经验内容"`；跨需求经验加 `--level cross-requirement`，项目级加 `--level project`，AGENTS 候选加 `--level agents-candidates`。
9. 查看经验用 `codex-sdlc lessons list`，查看单条用 `codex-sdlc lessons show LES-001`，按显式文件关系列出经验用 `codex-sdlc lessons match --file 精确路径`。
10. 需要从文档提炼经验时，由 Codex 读取文档并整理级别、范围、来源文件和命令，然后逐条调用 `lessons add`。
11. `lessons scan` 不再从 Markdown 正文产生候选，不把空扫描报告当成经验结论。
12. 经验级别可以调整：晋升用 `codex-sdlc lessons promote LES-001 --to project`，降级用 `codex-sdlc lessons demote LES-001 --to cross-requirement`。
13. 经验不再适用时用 `codex-sdlc lessons retire LES-001 --reason "原因"`，不要直接删除历史文件。
14. 普通模式下，任务完成后识别出的经验候选要先让用户确认再写入；Goal 模式下可以自动写入高置信度的需求级、跨需求和项目级经验，但 AGENTS 候选仍只记录候选，不自动改 `AGENTS.md`。
15. 本指令完成后只汇报写入位置、推荐级别和下一步，不写业务代码、不推进任务。
