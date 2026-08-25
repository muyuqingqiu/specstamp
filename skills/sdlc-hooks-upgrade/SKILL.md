---
name: sdlc-hooks-upgrade
description: 升级当前项目的 Codex Hook 和安全规则，对应 codex-sdlc hooks-upgrade。用于修复旧项目 Stop Hook 误拦截普通开发问答、SessionStart 上下文过度引导 SDLC、普通会话未提到 SDLC 也被提醒、或身份不一致导致 init 不能升级 Hook 的情况。
---

# sdlc-hooks-upgrade

命令：`codex-sdlc hooks-upgrade`

执行时请：

1. 说明这一步只升级当前项目的 `.codex/hooks` 和 `.codex/rules` 自动生成文件。
2. 执行 `codex-sdlc hooks-upgrade`。
3. 不执行 `codex-sdlc init`，不读写 `.codex-sdlc/`，不创建需求，不拆任务，不生成交接。
4. 完成后检查：
   - `.codex/hooks/sdlc_stop.py` 不包含 `"decision": "block"`。
   - `.codex/hooks/sdlc_stop.py` 不再要求先执行 `codex-sdlc finish` 或 `codex-sdlc handoff`。
   - `.codex/hooks/sdlc_session_start.py`、`.codex/hooks/sdlc_user_prompt_submit.py` 和 `.codex/hooks/sdlc_stop.py` 都包含 `should_emit_sdlc_context`。
   - 普通提示词没有出现 `sdlc` 时，Hook 不输出 SDLC 提醒；同一会话里出现过 `sdlc` 后，才允许输出 SDLC 状态或收口提醒。
5. 如果当前会话仍显示旧钩子提示，说明旧会话可能已经加载过旧上下文；新一轮 Stop Hook 会按文件最新内容执行。
6. 本指令只修 Hook，不处理业务代码、不处理需求状态。
