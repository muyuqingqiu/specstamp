from __future__ import annotations

import argparse

from codex_sdlc.core.agent_sync import check_agent_entries, sync_agent_entries


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("agent-sync", help="同步全局 Agent 工具入口并清理重复 SDLC 技能")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入文件")
    parser.add_argument("--confirm", action="store_true", help="确认写入运行时入口并清理重复入口")
    parser.add_argument("--check", action="store_true", help="只读检查版本化来源和运行时入口是否一致")
    parser.set_defaults(func=run_agent_sync)


def run_agent_sync(args: argparse.Namespace) -> int:
    if args.check:
        if args.dry_run or args.confirm:
            print("问题：`--check` 不能和 `--dry-run` 或 `--confirm` 同时使用。")
            return 1
        report = check_agent_entries()
        if report["issues"]:
            print("Agent 入口一致性检查")
            for issue in report["issues"]:
                print(f"- 问题：{issue}")
            return 1
        print(
            f"Agent 入口内容一致：{report['sdlc_count']} 个 SDLC 技能，"
            f"{report['claude_command_count']} 个 Claude 命令。"
        )
        return 0
    report = sync_agent_entries(dry_run=args.dry_run, confirm=args.confirm)
    if report["mode"] == "preview":
        print("Agent 入口同步预览")
        print("- 结果：只预览，不写入文件。")
    else:
        print("已完成 Agent 入口同步")
        if report.get("manifest"):
            print(f"- 清单：{report['manifest']}")
    print(f"- 版本化技能来源：{report['paths']['source_skills']}")
    print(f"- 仓库内技能目录：{report['paths']['versioned_skills']}")
    if report["source_is_versioned"]:
        print("- 来源状态：正在使用仓库内版本化技能。")
    else:
        print("- 来源状态：当前没有使用仓库内版本化技能，请确认这只是临时覆盖。")
    print(f"- 共享技能来源：{report['paths']['shared_source_skills']}")
    print(f"- 仓库内共享技能目录：{report['paths']['versioned_shared_skills']}")
    if report["shared_source_is_versioned"]:
        print("- 共享来源状态：正在使用仓库内版本化共享技能。")
    else:
        print("- 共享来源状态：当前没有使用仓库内版本化共享技能，请确认这只是临时覆盖。")
    print(f"- 运行时标准目录：{report['paths']['standard_home']}")
    print(f"- Codex 技能目录：{report['paths']['codex_skills']}")
    print(f"- 通用 Agent 技能目录：{report['paths']['agents_skills']}")
    print(f"- Claude Commands：{report['paths']['claude_commands']}")
    print(f"- 技能总数：{report['skill_count']}（sdlc={report['sdlc_count']}）")
    print(f"- Claude 命令数：{report['claude_command_count']}")
    print(f"- 将清理或已清理的重复入口：{report['duplicate_count']}")
    print(f"- 将清理或已清理的旧标准入口：{report['stale_standard_count']}")
    print(f"- 将清理或已清理的旧 Codex 入口：{report['stale_codex_count']}")
    print(f"- 共享开发技能数：{report['shared_skill_count']}")
    print(f"- 受管共享技能数：{report['managed_shared_skill_count']}")
    print(f"- Codex 本地技能数：{report['codex_local_skill_count']}")
    print(f"- 非 SDLC 重名技能：{report['duplicate_non_sdlc_count']}")
    if report["duplicate_non_sdlc_count"]:
        print("- 重名技能：" + "、".join(str(item) for item in report["duplicate_non_sdlc"]))
    if report["mode"] == "preview":
        print(f"- 预计备份目录：{report['backup_dir']}")
        print("下一步建议：确认无误后执行 `codex-sdlc agent-sync --confirm`。")
    else:
        if report.get("backup_created"):
            print(f"- 备份目录：{report['backup_dir']}")
        else:
            print("- 备份目录：本次没有需要备份的旧入口或旧文件。")
        print("下一步建议：重开 Codex/Claude Code 或刷新命令面板后检查入口是否只剩一套。")
    return 0
