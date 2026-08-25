from __future__ import annotations

import json
from pathlib import Path


HOOKS_JSON_PATH = Path(".codex/hooks.json")
RULES_FILE_PATH = Path(".codex/rules/default.rules")
HOOK_SCRIPT_DIR = Path(".codex/hooks")


def render_hooks_json() -> str:
    content = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear",
                    "hooks": [{"type": "command", "command": "python3 .codex/hooks/sdlc_session_start.py"}],
                }
            ],
            "UserPromptSubmit": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": "python3 .codex/hooks/sdlc_user_prompt_submit.py"}],
                }
            ],
            "Stop": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": "python3 .codex/hooks/sdlc_stop.py"}],
                }
            ],
        }
    }
    return json.dumps(content, ensure_ascii=False, indent=2) + "\n"


def render_session_start_hook() -> str:
    return '#!/usr/bin/env python3\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport sys\nimport tempfile\nimport time\nfrom pathlib import Path\n\nACTIVATION_TTL_SECONDS = 12 * 60 * 60\nSESSION_KEYS = ("session_id", "conversation_id", "thread_id", "codex_session_id", "chat_id")\nPROMPT_KEYS = ("prompt", "message", "input", "user_prompt", "text")\n\n\ndef read_payload() -> dict[str, object]:\n    raw = sys.stdin.read().strip()\n    return json.loads(raw) if raw else {}\n\n\ndef payload_text(payload: dict[str, object]) -> str:\n    return "\\n".join(str(payload[key]) for key in PROMPT_KEYS if isinstance(payload.get(key), str))\n\n\ndef has_explicit_sdlc_command(payload: dict[str, object]) -> bool:\n    text = payload_text(payload)\n    return "$sdlc-" in text or "codex-sdlc " in text\n\n\ndef session_id(payload: dict[str, object]) -> str | None:\n    for key in SESSION_KEYS:\n        value = payload.get(key)\n        if isinstance(value, str) and value.strip():\n            return value.strip()\n    return None\n\n\ndef activation_path(root: Path, payload: dict[str, object]) -> Path | None:\n    sid = session_id(payload)\n    if not sid:\n        return None\n    seed = f"{root}\\n{sid}".encode("utf-8")\n    name = hashlib.sha256(seed).hexdigest()[:32] + ".flag"\n    return Path(tempfile.gettempdir()) / "codex-sdlc-hook-sessions" / name\n\n\ndef mark_session_active(root: Path, payload: dict[str, object]) -> None:\n    path = activation_path(root, payload)\n    if path is None:\n        return\n    path.parent.mkdir(parents=True, exist_ok=True)\n    path.write_text(str(time.time()), encoding="utf-8")\n\n\ndef session_is_active(root: Path, payload: dict[str, object]) -> bool:\n    path = activation_path(root, payload)\n    if path is None or not path.exists():\n        return False\n    try:\n        activated_at = float(path.read_text(encoding="utf-8").strip() or "0")\n    except ValueError:\n        return False\n    return time.time() - activated_at <= ACTIVATION_TTL_SECONDS\n\n\ndef should_emit_sdlc_context(root: Path, payload: dict[str, object]) -> bool:\n    if has_explicit_sdlc_command(payload):\n        mark_session_active(root, payload)\n        return True\n    return session_is_active(root, payload)\n\n\ndef build_context(root: Path) -> str:\n    prefix = "当前会话已经通过显式 SDLC 命令进入流程。"\n    current_file = root / ".codex-sdlc" / "current.md"\n    if current_file.exists():\n        text = current_file.read_text(encoding="utf-8").strip()\n        if text:\n            return prefix + "\\n\\n" + text[:1200]\n    return prefix + " 可使用 `$sdlc-status` 或 `$sdlc-next` 查看状态。"\n\n\ndef main() -> int:\n    payload = read_payload()\n    root = Path(str(payload.get("cwd") or ".")).resolve()\n    if not (root / ".codex-sdlc").exists() or not should_emit_sdlc_context(root, payload):\n        return 0\n    print(json.dumps({"continue": True, "additionalContext": build_context(root)}, ensure_ascii=False))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def render_user_prompt_hook() -> str:
    return '#!/usr/bin/env python3\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport sys\nimport tempfile\nimport time\nfrom pathlib import Path\n\nACTIVATION_TTL_SECONDS = 12 * 60 * 60\nSESSION_KEYS = ("session_id", "conversation_id", "thread_id", "codex_session_id", "chat_id")\nPROMPT_KEYS = ("prompt", "message", "input", "user_prompt", "text")\n\n\ndef read_payload() -> dict[str, object]:\n    raw = sys.stdin.read().strip()\n    return json.loads(raw) if raw else {}\n\n\ndef payload_text(payload: dict[str, object]) -> str:\n    return "\\n".join(str(payload[key]) for key in PROMPT_KEYS if isinstance(payload.get(key), str))\n\n\ndef has_explicit_sdlc_command(payload: dict[str, object]) -> bool:\n    text = payload_text(payload)\n    return "$sdlc-" in text or "codex-sdlc " in text\n\n\ndef session_id(payload: dict[str, object]) -> str | None:\n    for key in SESSION_KEYS:\n        value = payload.get(key)\n        if isinstance(value, str) and value.strip():\n            return value.strip()\n    return None\n\n\ndef activation_path(root: Path, payload: dict[str, object]) -> Path | None:\n    sid = session_id(payload)\n    if not sid:\n        return None\n    seed = f"{root}\\n{sid}".encode("utf-8")\n    name = hashlib.sha256(seed).hexdigest()[:32] + ".flag"\n    return Path(tempfile.gettempdir()) / "codex-sdlc-hook-sessions" / name\n\n\ndef mark_session_active(root: Path, payload: dict[str, object]) -> None:\n    path = activation_path(root, payload)\n    if path is None:\n        return\n    path.parent.mkdir(parents=True, exist_ok=True)\n    path.write_text(str(time.time()), encoding="utf-8")\n\n\ndef session_is_active(root: Path, payload: dict[str, object]) -> bool:\n    path = activation_path(root, payload)\n    if path is None or not path.exists():\n        return False\n    try:\n        activated_at = float(path.read_text(encoding="utf-8").strip() or "0")\n    except ValueError:\n        return False\n    return time.time() - activated_at <= ACTIVATION_TTL_SECONDS\n\n\ndef should_emit_sdlc_context(root: Path, payload: dict[str, object]) -> bool:\n    if has_explicit_sdlc_command(payload):\n        mark_session_active(root, payload)\n        return True\n    return session_is_active(root, payload)\n\n\ndef build_prompt_context(root: Path) -> str:\n    current_file = root / ".codex-sdlc" / "current.md"\n    status = current_file.read_text(encoding="utf-8")[:1200] if current_file.exists() else ""\n    return "当前会话已经通过显式 SDLC 命令进入流程。" + (("\\n\\n" + status) if status.strip() else "")\n\n\ndef main() -> int:\n    payload = read_payload()\n    root = Path(str(payload.get("cwd") or ".")).resolve()\n    if not (root / ".codex-sdlc").exists() or not should_emit_sdlc_context(root, payload):\n        return 0\n    print(json.dumps({"continue": True, "additionalContext": build_prompt_context(root)}, ensure_ascii=False))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def render_stop_hook() -> str:
    return '#!/usr/bin/env python3\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport sys\nimport tempfile\nimport time\nfrom pathlib import Path\n\nACTIVATION_TTL_SECONDS = 12 * 60 * 60\nSESSION_KEYS = ("session_id", "conversation_id", "thread_id", "codex_session_id", "chat_id")\nPROMPT_KEYS = ("prompt", "message", "input", "user_prompt", "text")\n\n\ndef read_payload() -> dict[str, object]:\n    raw = sys.stdin.read().strip()\n    return json.loads(raw) if raw else {}\n\n\ndef payload_text(payload: dict[str, object]) -> str:\n    return "\\n".join(str(payload[key]) for key in PROMPT_KEYS if isinstance(payload.get(key), str))\n\n\ndef has_explicit_sdlc_command(payload: dict[str, object]) -> bool:\n    text = payload_text(payload)\n    return "$sdlc-" in text or "codex-sdlc " in text\n\n\ndef session_id(payload: dict[str, object]) -> str | None:\n    for key in SESSION_KEYS:\n        value = payload.get(key)\n        if isinstance(value, str) and value.strip():\n            return value.strip()\n    return None\n\n\ndef activation_path(root: Path, payload: dict[str, object]) -> Path | None:\n    sid = session_id(payload)\n    if not sid:\n        return None\n    seed = f"{root}\\n{sid}".encode("utf-8")\n    name = hashlib.sha256(seed).hexdigest()[:32] + ".flag"\n    return Path(tempfile.gettempdir()) / "codex-sdlc-hook-sessions" / name\n\n\ndef mark_session_active(root: Path, payload: dict[str, object]) -> None:\n    path = activation_path(root, payload)\n    if path is None:\n        return\n    path.parent.mkdir(parents=True, exist_ok=True)\n    path.write_text(str(time.time()), encoding="utf-8")\n\n\ndef session_is_active(root: Path, payload: dict[str, object]) -> bool:\n    path = activation_path(root, payload)\n    if path is None or not path.exists():\n        return False\n    try:\n        activated_at = float(path.read_text(encoding="utf-8").strip() or "0")\n    except ValueError:\n        return False\n    return time.time() - activated_at <= ACTIVATION_TTL_SECONDS\n\n\ndef should_emit_sdlc_context(root: Path, payload: dict[str, object]) -> bool:\n    if has_explicit_sdlc_command(payload):\n        mark_session_active(root, payload)\n        return True\n    return session_is_active(root, payload)\n\n\nimport subprocess\n\n\ndef list_changed_files(root: Path) -> list[str]:\n    if not (root / ".git").exists():\n        return []\n    result = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=False)\n    if result.returncode != 0:\n        return []\n    return [line[3:].strip() for line in result.stdout.splitlines() if len(line) >= 4 and not line[3:].strip().startswith((".codex-sdlc/", ".codex/"))]\n\n\ndef main() -> int:\n    payload = read_payload()\n    root = Path(str(payload.get("cwd") or ".")).resolve()\n    if not (root / ".codex-sdlc").exists() or not should_emit_sdlc_context(root, payload):\n        return 0\n    if bool(payload.get("stop_hook_active")) or not list_changed_files(root):\n        return 0\n    context = "当前显式 SDLC 会话还有未提交代码改动，可按需要使用 `$sdlc-finish` 或 `$sdlc-handoff`。"\n    print(json.dumps({"continue": True, "additionalContext": context}, ensure_ascii=False))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def render_rules_file() -> str:
    return """# 这些 Rules 只做提醒和风险控制，真正的安全边界仍然依赖沙箱、审批策略和人工确认。

prefix_rule(
    pattern = ["rm", "-rf"],
    decision = "prompt",
    justification = "这是高风险删除命令，请先确认影响范围，再决定是否继续。",
)

prefix_rule(
    pattern = ["git", "reset", "--hard"],
    decision = "prompt",
    justification = "这个命令会直接丢掉本地改动，请先确认没有要保留的内容。",
)

prefix_rule(
    pattern = ["git", "clean", "-fd"],
    decision = "prompt",
    justification = "这个命令会删除未跟踪文件，请先确认没有要保留的临时内容。",
)

prefix_rule(
    pattern = ["docker", "compose", "down", "-v"],
    decision = "prompt",
    justification = "这个命令会连卷一起清掉，请先确认当前环境和数据都可以重建。",
)

prefix_rule(
    pattern = ["npm", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["pnpm", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["yarn", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["bun", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["cargo", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["poetry", "publish"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)

prefix_rule(
    pattern = ["twine", "upload"],
    decision = "forbidden",
    justification = "发布命令默认禁止。需要发布时请先人工确认，不要把 Rules 当成唯一安全边界。",
)
"""


def is_generated_codex_asset(relative_path: Path, content: str) -> bool:
    path_text = relative_path.as_posix()
    markers = {
        ".codex/hooks.json": [
            ['"sdlc_session_start.py"', '"sdlc_user_prompt_submit.py"', '"sdlc_stop.py"'],
        ],
        ".codex/hooks/sdlc_session_start.py": [
            ["当前项目已经启用 SDLC"],
            ["should_emit_sdlc_context", "当前会话已经明确进入 SDLC 相关流程"],
        ],
        ".codex/hooks/sdlc_user_prompt_submit.py": [
            ["当前目录已经在用 SDLC 记录状态"],
            ["should_emit_sdlc_context", "当前会话已经明确进入 SDLC 相关流程"],
            ["looks_like_sdlc_feedback", "$sdlc-fix", "$sdlc-task-restore"],
        ],
        ".codex/hooks/sdlc_stop.py": [
            ["普通开发问答不需要先执行 SDLC 交接"],
            ["should_emit_sdlc_context", "当前会话已经明确进入 SDLC 相关流程"],
            ["检测到本轮还有代码改动", "codex-sdlc finish"],
            ["read_feedback_context", "$sdlc-fix", "$sdlc-task-restore"],
        ],
        ".codex/rules/default.rules": [["# 这些 Rules 只做提醒和风险控制"]],
    }
    marker_sets = markers.get(path_text, [])
    return any(all(marker in content for marker in marker_set) for marker_set in marker_sets)


def install_project_codex_assets(root: Path) -> None:
    files = {
        HOOKS_JSON_PATH: render_hooks_json(),
        HOOK_SCRIPT_DIR / "sdlc_session_start.py": render_session_start_hook(),
        HOOK_SCRIPT_DIR / "sdlc_user_prompt_submit.py": render_user_prompt_hook(),
        HOOK_SCRIPT_DIR / "sdlc_stop.py": render_stop_hook(),
        RULES_FILE_PATH: render_rules_file(),
    }
    for relative_path, content in files.items():
        target_path = root / relative_path
        if target_path.exists():
            current_content = target_path.read_text(encoding="utf-8")
            if current_content == content:
                continue
            if not is_generated_codex_asset(relative_path, current_content):
                continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
        if target_path.suffix == ".py":
            target_path.chmod(0o755)


def get_project_codex_asset_status(root: Path) -> dict[str, object]:
    hooks_json = root / HOOKS_JSON_PATH
    rules_file = root / RULES_FILE_PATH
    hook_scripts = [
        root / HOOK_SCRIPT_DIR / "sdlc_session_start.py",
        root / HOOK_SCRIPT_DIR / "sdlc_user_prompt_submit.py",
        root / HOOK_SCRIPT_DIR / "sdlc_stop.py",
    ]
    hooks_ready = hooks_json.exists() and all(path.exists() for path in hook_scripts)
    return {
        "hooks_json_exists": hooks_json.exists(),
        "hooks_ready": hooks_ready,
        "rules_ready": rules_file.exists(),
        "hooks_json_path": str(hooks_json),
        "rules_file_path": str(rules_file),
    }
