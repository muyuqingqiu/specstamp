#!/usr/bin/env python3
"""按线程角色渲染 Goal 任务调度提示词。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """读取脚本参数。"""

    parser = argparse.ArgumentParser(description="渲染 Goal 任务调度线程提示词。")
    parser.add_argument(
        "--role",
        required=True,
        choices=["executor", "qa", "stop", "replacement-executor", "replacement-qa"],
        help="线程角色",
    )
    parser.add_argument("--task-doc", required=True, help="任务文档路径")
    parser.add_argument("--requirement-doc", help="需求文档路径")
    parser.add_argument("--project-root", required=True, help="项目根路径")
    parser.add_argument("--progress-file", required=True, help="进度文件路径")
    parser.add_argument("--task-id", required=True, help="任务编号")
    parser.add_argument("--task-title", required=True, help="任务标题")
    parser.add_argument("--environment", required=True, help="当前环境")
    parser.add_argument("--complexity", required=True, help="复杂度判断")
    parser.add_argument("--model-tier", required=True, help="推荐模型档位")
    parser.add_argument("--requested-model", required=True, help="请求模型")
    parser.add_argument("--requested-thinking", required=True, help="请求思考深度")
    parser.add_argument("--model-reason", required=True, help="模型判断原因")
    parser.add_argument("--thinking-reason", required=True, help="思考深度判断原因")
    parser.add_argument("--polling-interval", required=True, help="主线程检查间隔")
    parser.add_argument("--task-scope", default="待主线程补充", help="任务边界")
    parser.add_argument("--done-condition", default="待主线程补充", help="完成条件")
    parser.add_argument("--qa-scope", default="待主线程补充", help="质检范围")
    parser.add_argument("--pass-condition", default="待主线程补充", help="质检通过标准")
    parser.add_argument("--stop-reason", default="待主线程补充", help="停止旧线程原因")
    parser.add_argument("--old-thread-id", default="null", help="旧线程 id")
    parser.add_argument("--old-stop-reason", default="待主线程补充", help="旧线程停止原因")
    parser.add_argument("--handoff-summary", default="", help="交接摘要文字")
    parser.add_argument("--handoff-summary-file", help="交接摘要文件路径")
    parser.add_argument("--output", help="输出文件路径；不传则打印到 stdout")
    return parser.parse_args()


def requirement_text(path: str | None) -> str:
    """统一需求文档的空值显示。"""

    return path if path else "null"


def build_dispatch_decision(args: argparse.Namespace) -> str:
    """渲染主线程派发决策块。"""

    role_map = {
        "executor": "执行员",
        "qa": "质检员",
        "stop": "停止旧线程",
        "replacement-executor": "替代执行员",
        "replacement-qa": "替代质检员",
    }
    return "\n".join(
        [
            "派发决策：",
            f"- 当前环境：{args.environment}",
            f"- 任务编号：{args.task_id}",
            f"- 任务名称：{args.task_title}",
            f"- 线程职责：{role_map[args.role]}",
            f"- 复杂度判断：{args.complexity}",
            f"- 推荐模型档位：{args.model_tier}",
            f"- 请求模型：{args.requested_model}",
            f"- 请求思考深度：{args.requested_thinking}",
            f"- 模型判断原因：{args.model_reason}",
            f"- 思考深度判断原因：{args.thinking_reason}",
            f"- 主线程检查间隔：{args.polling_interval}",
            f"- 进度文件：{args.progress_file}",
            "- 范围：只处理当前任务，不进入下一个任务",
        ]
    )


def load_handoff_summary(args: argparse.Namespace) -> str:
    """优先读取文件版交接摘要，避免长文本不好转义。"""

    if args.handoff_summary_file:
        return Path(args.handoff_summary_file).expanduser().read_text(encoding="utf-8").strip()
    return args.handoff_summary.strip() or "待旧线程补充"


def render_prompt(args: argparse.Namespace) -> str:
    """按角色生成对应提示词。"""

    decision_block = build_dispatch_decision(args)
    requirement_doc_or_none = requirement_text(args.requirement_doc)

    if args.role == "executor":
        return "\n".join(
            [
                "你是当前任务的【任务执行员】线程。",
                "",
                f"任务文档：{args.task_doc}",
                f"需求文档：{requirement_doc_or_none}",
                f"项目路径：{args.project_root}",
                f"进度文件：{args.progress_file}",
                f"任务编号：{args.task_id}",
                f"任务名称：{args.task_title}",
                f"任务边界：{args.task_scope}",
                f"完成条件：{args.done_condition}",
                "主线程派发决策：",
                decision_block,
                "",
                "必须遵守：",
                "1. 只处理当前任务，不处理后续任务。",
                "2. 如果提供了需求文档，开工前先核对需求文档，再开始开发。",
                "3. 开工前先按固定格式输出“承接确认”。",
                "4. 承接确认里必须复述你当前实际使用的模型、当前实际使用的思考深度、和主线程计划是否一致、以及你为什么适合承接这个任务。",
                "5. 开发完成后，把状态写回进度文件。",
                "6. 最终汇报必须包含：实际改动、验证结果、阻塞项、下一步建议。",
                "7. 如果卡住、跑偏或无法继续，不要硬撑，直接把最小证据和建议写回进度文件，等待主线程判断是否替换线程。",
                "8. 主线程会按检查间隔等待，不会连续轮询你；如果你已经完成，需要在本线程最终汇报里明确写“开发完成，待质检”。",
            ]
        )

    if args.role == "qa":
        return "\n".join(
            [
                "你是当前任务的【任务质检员】线程。",
                "",
                f"任务文档：{args.task_doc}",
                f"需求文档：{requirement_doc_or_none}",
                f"项目路径：{args.project_root}",
                f"进度文件：{args.progress_file}",
                f"任务编号：{args.task_id}",
                f"任务名称：{args.task_title}",
                f"验收范围：{args.qa_scope}",
                f"通过标准：{args.pass_condition}",
                "主线程派发决策：",
                decision_block,
                "",
                "必须遵守：",
                "1. 只做当前任务的测试、验收和问题记录，不直接改代码。",
                "2. 如果提供了需求文档，开工前先核对需求文档，再开始质检。",
                "3. 开工前先按固定格式输出“承接确认”。",
                "4. 承接确认里必须复述你当前实际使用的模型、当前实际使用的思考深度、和主线程计划是否一致、以及你为什么适合承接这个质检任务。",
                "5. 通过时写回 `passed`；失败时写回 `test_failed`，并写清问题、证据和建议返修点。",
                "6. 最终汇报必须包含：测试项、未测项、结论、证据位置、下一步建议。",
                "7. 主线程会按检查间隔等待，不会连续轮询你；如果你已经完成，需要在本线程最终汇报里明确写 `passed` 或 `test_failed`。",
            ]
        )

    if args.role == "stop":
        return "\n".join(
            [
                "请停止你当前负责的这项工作，不再继续推进新动作。",
                "",
                f"停止原因：{args.stop_reason}",
                f"当前任务：{args.task_id} {args.task_title}",
                f"进度文件：{args.progress_file}",
                "",
                "请只做这两件事：",
                "1. 用最短的话写出你已经完成了什么、还差什么。",
                "2. 给出一个可交接摘要，方便替代线程接手。",
                "",
                "不要再继续开发、继续测试或继续扩展范围。",
            ]
        )

    role_name = "执行员" if args.role == "replacement-executor" else "质检员"
    handoff_summary = load_handoff_summary(args)
    return "\n".join(
        [
            f"你是当前任务的替代{role_name}线程。",
            "",
            f"任务文档：{args.task_doc}",
            f"需求文档：{requirement_doc_or_none}",
            f"项目路径：{args.project_root}",
            f"进度文件：{args.progress_file}",
            f"任务编号：{args.task_id}",
            f"任务名称：{args.task_title}",
            f"旧线程 id：{args.old_thread_id}",
            f"旧线程停止原因：{args.old_stop_reason}",
            "交接摘要：",
            handoff_summary,
            "主线程派发决策：",
            decision_block,
            "",
            "必须遵守：",
            "1. 先阅读交接摘要，再继续当前任务。",
            "2. 不要重复旧线程已经完成的部分，除非你判断它的结论不可靠，并说明原因。",
            "3. 开工前先按固定格式输出“承接确认”。",
            "4. 继续只处理当前任务，不进入下一个任务。",
        ]
    )


def main() -> int:
    """脚本入口。"""

    args = parse_args()
    prompt = render_prompt(args)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt, encoding="utf-8")
    else:
        sys.stdout.write(prompt)
        if not prompt.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
