---
name: sdlc-backup
description: Use when 用户需要备份当前项目或指定需求的本机 SDLC 资料
---

# sdlc-backup

用户侧指令：`$sdlc-backup`
底层执行：`codex-sdlc backup`

执行时请：

1. 说明这条指令只备份 SDLC 本机资料，不备份业务源码、不提交业务 Git。
2. 用户没有指定需求时，底层执行 `codex-sdlc backup`，生成项目快照和全部需求快照。项目快照只包含 `.codex-sdlc/`；旧项目资料需要先迁移为 SDLC 原生材料或需求包后再备份。
3. 用户指定需求时，底层执行 `codex-sdlc backup REQ-001`，备份该需求包、需求事件切片，以及 `.codex-sdlc/lessons/` 里的跨需求和项目级经验。
4. 如果用户给了说明标签，使用 `--label "标签"`。
5. 如果用户说“固定、长期保留、重要快照、不想被清理”，加 `--pin`。
6. 完成后告诉用户备份目录、项目快照和需求快照是否已生成，是否为 pinned 快照，以及备份目录自己的 Git 是否已提交。
7. `backup` 会检查 `.codex-sdlc/identity.json`。如果输出身份不一致，停止，不要强行备份；推荐 `$sdlc-backup-list` 和 `$sdlc-restore --dry-run`。
8. 本阶段只做备份，不恢复、不改任务、不写代码、不跑测试。
