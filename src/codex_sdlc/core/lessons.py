from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from codex_sdlc.core.project import ProjectPaths


GLOBAL_LESSON_LEVELS = ("cross-requirement", "project", "agents-candidates")
LESSON_STATUS_ACTIVE = "active"
LESSON_STATUS_STALE = "stale"
LESSON_STATUS_RETIRED = "retired"
CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS = "CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def current_identity(paths: ProjectPaths) -> dict[str, Any]:
    from codex_sdlc.core.backup import current_git_identity

    return current_git_identity(paths.root)


def _deep_scan_roots() -> list[Path]:
    """深度扫描的搜索根：默认工作树目录加环境变量配置的额外根。

    额外根使用 `CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS` 配置，多个路径用
    系统路径分隔符（`os.pathsep`）分隔。结果保留顺序、去重并忽略不存在的路径，
    不把整个主目录当作搜索根。
    """

    roots: list[Path] = [Path.home() / ".codex" / "worktrees"]
    extra_raw = os.environ.get(CODEX_SDLC_EXTRA_LESSON_SCAN_ROOTS, "").strip()
    if extra_raw:
        for raw in extra_raw.split(os.pathsep):
            raw = raw.strip()
            if not raw:
                continue
            roots.append(Path(raw).expanduser())
    result: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.exists():
            continue
        seen.add(root)
        result.append(root)
    return result


def clean_items(items: list[Any] | tuple[Any, ...] | None) -> list[str]:
    result: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def one_line(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def lessons_root(paths: ProjectPaths) -> Path:
    return paths.lessons_dir


def lesson_level_dir(paths: ProjectPaths, level: str) -> Path:
    return lessons_root(paths) / level


def lesson_index_path(paths: ProjectPaths) -> Path:
    return lessons_root(paths) / "index.json"


def scan_dir(paths: ProjectPaths) -> Path:
    return lessons_root(paths) / "scans"


def load_lesson_index(paths: ProjectPaths) -> dict[str, Any]:
    data = read_json(lesson_index_path(paths), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema", "codex-sdlc.lessons.v1")
    data.setdefault("lessons", [])
    data.setdefault("scans", [])
    return data


def save_lesson_index(paths: ProjectPaths, data: dict[str, Any]) -> None:
    data["schema"] = "codex-sdlc.lessons.v1"
    write_json(lesson_index_path(paths), data)


def next_prefixed_id(existing: list[str], prefix: str) -> str:
    numbers: list[int] = []
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for value in existing:
        match = pattern.match(value)
        if match:
            numbers.append(int(match.group(1)))
    return f"{prefix}-{(max(numbers) if numbers else 0) + 1:03d}"


def all_global_lessons(paths: ProjectPaths, *, include_retired: bool = False) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    for level in GLOBAL_LESSON_LEVELS:
        for json_path in sorted(lesson_level_dir(paths, level).glob("LES-*.json")):
            data = read_json(json_path, {})
            if not isinstance(data, dict):
                continue
            if not include_retired and data.get("status") == LESSON_STATUS_RETIRED:
                continue
            lessons.append(data)
    return sorted(lessons, key=lambda item: str(item.get("lesson_id", "")))


def global_lessons_fingerprint(paths: ProjectPaths) -> str:
    payload = [
        {
            "lesson_id": item.get("lesson_id"),
            "level": item.get("level"),
            "status": item.get("status"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "scope": item.get("scope", []),
            "updated_at": item.get("updated_at"),
        }
        for item in all_global_lessons(paths)
    ]
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()[:16]


def lesson_markdown_lines(lesson: dict[str, Any]) -> list[str]:
    files = clean_items(lesson.get("source_files", []))
    commands = clean_items(lesson.get("commands", []))
    scope = clean_items(lesson.get("scope", []))
    return [
        f"# {lesson.get('lesson_id')} {lesson.get('title', '')}",
        "",
        "## 结论",
        str(lesson.get("summary", "")).strip() or "未填写",
        "",
        "## 级别和范围",
        f"- 级别：{lesson.get('level', '')}",
        f"- 状态：{lesson.get('status', LESSON_STATUS_ACTIVE)}",
        f"- 适用范围：{'、'.join(scope) if scope else '未限定'}",
        f"- 不适用范围：{lesson.get('not_for') or '未记录'}",
        "",
        "## 来源",
        f"- 来源需求：{lesson.get('source_requirement') or '未记录'}",
        f"- 来源任务：{lesson.get('source_task') or '未记录'}",
        f"- 来源分支：{lesson.get('source_branch') or '未记录'}",
        f"- 来源工作树：{lesson.get('source_worktree') or '未记录'}",
        "",
        "## 来源文件",
        *([f"- {item}" for item in files] or ["- 暂无"]),
        "",
        "## 可复用命令",
        *([f"- {item}" for item in commands] or ["- 暂无"]),
        "",
        "## 过期检查",
        f"- 最近验证：{lesson.get('last_verified_at') or '未记录'}",
        f"- 过期原因：{lesson.get('stale_reason') or '暂无'}",
        "",
    ]


def write_global_lesson(paths: ProjectPaths, lesson: dict[str, Any]) -> dict[str, Any]:
    level = str(lesson.get("level", "")).strip()
    if level not in GLOBAL_LESSON_LEVELS:
        raise ValueError(f"不支持的经验级别：{level}")
    index = load_lesson_index(paths)
    existing_ids = [
        str(item.get("lesson_id", ""))
        for item in index.get("lessons", [])
        if isinstance(item, dict)
    ]
    lesson_id = str(lesson.get("lesson_id") or "").strip() or next_prefixed_id(existing_ids, "LES")
    now = now_iso()
    identity = current_identity(paths)
    payload = {
        "lesson_id": lesson_id,
        "level": level,
        "status": lesson.get("status") or LESSON_STATUS_ACTIVE,
        "title": lesson.get("title") or one_line(str(lesson.get("summary", "")), 60),
        "summary": str(lesson.get("summary", "")).strip(),
        "scope": clean_items(lesson.get("scope", [])),
        "not_for": str(lesson.get("not_for", "")).strip(),
        "source_requirement": str(lesson.get("source_requirement", "")).strip(),
        "source_task": str(lesson.get("source_task", "")).strip(),
        "source_files": clean_items(lesson.get("source_files", [])),
        "commands": clean_items(lesson.get("commands", [])),
        "source_branch": identity.get("branch") or identity.get("branch_ref") or "",
        "source_worktree": str(paths.root),
        "source_snapshot": str(lesson.get("source_snapshot", "")).strip(),
        "created_at": lesson.get("created_at") or now,
        "updated_at": now,
        "last_verified_at": str(lesson.get("last_verified_at", "")).strip(),
        "stale_reason": str(lesson.get("stale_reason", "")).strip(),
    }
    target_dir = lesson_level_dir(paths, level)
    target_dir.mkdir(parents=True, exist_ok=True)
    write_json(target_dir / f"{lesson_id}.json", payload)
    (target_dir / f"{lesson_id}.md").write_text("\n".join(lesson_markdown_lines(payload)) + "\n", encoding="utf-8")

    entries = [item for item in index.get("lessons", []) if isinstance(item, dict) and item.get("lesson_id") != lesson_id]
    entries.append(
        {
            "lesson_id": lesson_id,
            "level": level,
            "status": payload["status"],
            "title": payload["title"],
            "summary": payload["summary"],
            "scope": payload["scope"],
            "source_requirement": payload["source_requirement"],
            "source_task": payload["source_task"],
            "updated_at": payload["updated_at"],
            "path": f".codex-sdlc/lessons/{level}/{lesson_id}.md",
        }
    )
    index["lessons"] = sorted(entries, key=lambda item: str(item.get("lesson_id", "")))
    save_lesson_index(paths, index)
    return payload




def match_global_lessons(paths: ProjectPaths, *, files: list[str] | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """只按显式文件关系和项目级枚举选取经验。"""

    requested_files = set(clean_items(files or []))
    matched: list[dict[str, Any]] = []
    for lesson in all_global_lessons(paths):
        lesson_files = set(clean_items(lesson.get("source_files", [])))
        if lesson.get("level") == "project" or (requested_files and lesson_files & requested_files):
            matched.append(dict(lesson))
    return matched[:limit]


def lesson_summary_lines(lessons: list[dict[str, Any]]) -> list[str]:
    if not lessons:
        return ["- 当前任务没有命中跨需求或项目级经验"]
    lines: list[str] = []
    for lesson in lessons:
        scope = "、".join(clean_items(lesson.get("scope", []))[:3]) or "未限定"
        stale = "；可能过期" if lesson.get("status") == LESSON_STATUS_STALE else ""
        lines.append(
            f"- {lesson.get('lesson_id')} [{lesson.get('level')}] {one_line(str(lesson.get('summary', '')), 96)}；范围：{scope}{stale}"
        )
    return lines




def requirement_id_from_dir_name(name: str) -> str:
    match = re.match(r"^(REQ-\d+)", name)
    return match.group(1) if match else name




def project_sdlc_roots(paths: ProjectPaths, *, deep: bool = False) -> list[Path]:
    roots: list[Path] = []
    if paths.sdlc_dir.exists():
        roots.append(paths.sdlc_dir)
    identity = current_identity(paths)
    from codex_sdlc.core.backup import load_index

    index = load_index()
    for collection in ["project_snapshots", "requirement_snapshots"]:
        for entry in index.get(collection, []):
            if not isinstance(entry, dict) or entry.get("repo_key") != identity.get("repo_key"):
                continue
            project_path = Path(str(entry.get("project_path", ""))).expanduser()
            sdlc_path = project_path / ".codex-sdlc"
            if sdlc_path.exists() and sdlc_path not in roots:
                roots.append(sdlc_path)
    if deep:
        for base in _deep_scan_roots():
            if not base.exists():
                continue
            for identity_file in list(base.glob("**/.codex-sdlc/identity.json"))[:200]:
                data = read_json(identity_file, {})
                if isinstance(data, dict) and data.get("repo_key") == identity.get("repo_key"):
                    sdlc_path = identity_file.parent
                    if sdlc_path not in roots:
                        roots.append(sdlc_path)
    return roots


def scan_lesson_candidates(paths: ProjectPaths, *, deep: bool = False) -> list[dict[str, Any]]:
    """CLI 不再从 Markdown 正文自动提炼经验候选。"""

    return []


def write_scan(paths: ProjectPaths, candidates: list[dict[str, Any]], *, deep: bool = False) -> dict[str, Any]:
    index = load_lesson_index(paths)
    existing_scan_ids = [str(item.get("scan_id", "")) for item in index.get("scans", []) if isinstance(item, dict)]
    scan_id = next_prefixed_id(existing_scan_ids, "SCAN")
    payload = {
        "scan_id": scan_id,
        "created_at": now_iso(),
        "deep": deep,
        "candidate_count": len(candidates),
        "candidates": [
            {"candidate_id": f"C-{idx:03d}", **candidate}
            for idx, candidate in enumerate(candidates, start=1)
        ],
    }
    scan_dir(paths).mkdir(parents=True, exist_ok=True)
    write_json(scan_dir(paths) / f"{scan_id}.json", payload)
    lines = [
        f"# {scan_id} 经验扫描报告",
        "",
        f"- 扫描模式：{'深扫' if deep else '常规扫描'}",
        f"- 候选数量：{len(candidates)}",
        "",
    ]
    for level, title in [
        ("requirement", "推荐为需求级经验"),
        ("cross-requirement", "推荐为跨需求经验"),
        ("project", "推荐为项目级经验"),
        ("agents-candidates", "推荐为 AGENTS 候选"),
    ]:
        lines.extend([f"## {title}", ""])
        level_candidates = [item for item in payload["candidates"] if item["recommended_level"] == level]
        if not level_candidates:
            lines.append("- 暂无")
        for item in level_candidates:
            lines.append(f"- {item['candidate_id']}：{one_line(item['summary'], 140)}")
            lines.append(f"  - 原因：{item['reason']}")
            lines.append(f"  - 来源：{item.get('source_requirement') or '未知需求'} {item.get('source_task') or ''}".rstrip())
        lines.append("")
    (scan_dir(paths) / f"{scan_id}.md").write_text("\n".join(lines), encoding="utf-8")
    index["scans"] = [
        item for item in index.get("scans", []) if isinstance(item, dict) and item.get("scan_id") != scan_id
    ] + [
        {
            "scan_id": scan_id,
            "created_at": payload["created_at"],
            "candidate_count": len(candidates),
            "path": f".codex-sdlc/lessons/scans/{scan_id}.md",
        }
    ]
    save_lesson_index(paths, index)
    return payload
