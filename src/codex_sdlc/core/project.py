from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.git_tools import find_git_root


PROJECT_MARKERS = [
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "README.md",
]


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def sdlc_dir(self) -> Path:
        return self.root / ".codex-sdlc"

    @property
    def current_md(self) -> Path:
        return self.sdlc_dir / "current.md"

    @property
    def project_md(self) -> Path:
        return self.sdlc_dir / "project.md"

    @property
    def requirements_dir(self) -> Path:
        return self.sdlc_dir / "requirements"

    @property
    def start_staging_root(self) -> Path:
        """正式建档候选只放在项目内专用暂存区，不能借用 DRAFT 或系统临时目录。"""

        return self.sdlc_dir / ".staging"

    def start_staging_dir(self, directory_name: str) -> Path:
        return self.start_staging_root / directory_name

    @property
    def sessions_dir(self) -> Path:
        return self.sdlc_dir / "sessions"

    @property
    def captures_dir(self) -> Path:
        return self.sdlc_dir / "captures"

    @property
    def grills_dir(self) -> Path:
        return self.sdlc_dir / "grills"

    @property
    def changes_dir(self) -> Path:
        return self.sdlc_dir / "changes"

    @property
    def decisions_dir(self) -> Path:
        return self.sdlc_dir / "decisions"

    @property
    def designs_dir(self) -> Path:
        return self.sdlc_dir / "designs"

    @property
    def drafts_dir(self) -> Path:
        return self.sdlc_dir / "drafts"

    def draft_dir(self, draft_id: str) -> Path:
        return self.drafts_dir / draft_id

    def draft_original_materials_dir(self, draft_id: str) -> Path:
        """原始资料单独落目录，刷新投影时不会把它当成可重建文件处理。"""

        return self.draft_dir(draft_id) / "原始资料"

    def draft_requirements_dir(self, draft_id: str) -> Path:
        return self.draft_dir(draft_id) / "需求"

    def draft_design_dir(self, draft_id: str) -> Path:
        return self.draft_dir(draft_id) / "设计"

    def draft_design_reference_index_file(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "des-index.v1.json"

    def draft_design_reference_records_dir(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "引用记录"

    def draft_design_reference_markdown_file(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "技术方案引用.md"

    def draft_design_plan_file(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "design-plan.v1.json"

    def draft_code_evidence_file(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "code-evidence.v1.json"

    def draft_design_plan_markdown_file(self, draft_id: str) -> Path:
        return self.draft_design_dir(draft_id) / "开发设计总计划.md"

    def draft_quality_dir(self, draft_id: str) -> Path:
        return self.draft_dir(draft_id) / "质检"

    def draft_status_file(self, draft_id: str) -> Path:
        return self.draft_dir(draft_id) / "status.json"

    def draft_artifact_index_file(self, draft_id: str) -> Path:
        return self.draft_dir(draft_id) / "artifact-index.v1.json"

    def draft_staging_dir(self, draft_id: str) -> Path:
        """派生产物先写到 DRAFT 自己的暂存区，不能混进原始资料目录。"""

        return self.draft_dir(draft_id) / ".staging"

    @property
    def verifications_dir(self) -> Path:
        return self.sdlc_dir / "verifications"

    @property
    def exports_dir(self) -> Path:
        return self.sdlc_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.sdlc_dir / "backups"

    @property
    def imports_dir(self) -> Path:
        return self.sdlc_dir / "imports"

    @property
    def import_transactions_dir(self) -> Path:
        return self.sdlc_dir / "import-transactions"

    @property
    def import_registry_file(self) -> Path:
        return self.sdlc_dir / "import-registry.json"

    @property
    def change_transactions_dir(self) -> Path:
        """结构化变更创建事务与通用导入事务分开，恢复时不会误删彼此资料。"""

        return self.sdlc_dir / "change-transactions"

    @property
    def change_staging_root(self) -> Path:
        return self.change_transactions_dir / "staging"

    @property
    def lessons_dir(self) -> Path:
        return self.sdlc_dir / "lessons"

    @property
    def identity_file(self) -> Path:
        return self.sdlc_dir / "identity.json"

    @property
    def events_file(self) -> Path:
        return self.sdlc_dir / "events.jsonl"

    @property
    def database_file(self) -> Path:
        return self.sdlc_dir / "sdlc.db"

    @property
    def lock_file(self) -> Path:
        return self.sdlc_dir / "lock"

    def task_runtime_dir(self, requirement_folder: str, task_id: str) -> Path:
        """运行轮次跟随正式需求保存，避免不同需求的同号任务互相覆盖。"""

        return self.requirements_dir / requirement_folder / "runtime" / task_id

    def task_current_run_file(self, requirement_folder: str, task_id: str) -> Path:
        return self.task_runtime_dir(requirement_folder, task_id) / "current.json"


def locate_initialized_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".codex-sdlc").is_dir():
            return candidate
    return None


def looks_like_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def resolve_project_root(start: Path, allow_plain_directory: bool = False) -> Path:
    initialized_root = locate_initialized_root(start)
    if initialized_root is not None:
        return initialized_root

    git_root = find_git_root(start)
    if git_root is not None:
        return git_root

    if allow_plain_directory or looks_like_project(start):
        return start.resolve()

    raise SdlcError("当前目录还不是可识别项目，请先进入项目目录，或在明确项目目录中使用 `$sdlc-init`。")


def build_paths(root: Path) -> ProjectPaths:
    return ProjectPaths(root=root.resolve())


def resolve_project_path(
    project_root: Path,
    relative_path: str | Path,
    *,
    must_exist: bool = False,
) -> Path:
    """把相对路径安全解析到项目内，同时拦截绝对路径、越界和符号链接逃逸。"""

    raw_path = str(relative_path)
    if not raw_path.strip() or "\x00" in raw_path:
        raise SdlcError("项目内路径不能为空或包含空字符。")
    requested = Path(raw_path)
    if requested.is_absolute():
        raise SdlcError("项目内路径不能使用绝对路径。")
    if requested == Path(".") or ".." in requested.parts:
        raise SdlcError("项目内路径不能指向项目根目录或包含上级目录。")

    try:
        root = Path(project_root).resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise SdlcError(f"项目根目录不存在或无法访问：{project_root}。") from exc
    if not root.is_dir():
        raise SdlcError(f"项目根路径不是目录：{project_root}。")

    try:
        resolved = (root / requested).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"路径越过项目目录：{raw_path}。") from exc
    if must_exist and not resolved.exists():
        raise SdlcError(f"项目内路径不存在：{raw_path}。")
    return resolved


def ensure_base_dirs(paths: ProjectPaths) -> None:
    paths.sdlc_dir.mkdir(parents=True, exist_ok=True)
    for directory in [
        paths.requirements_dir,
        paths.sessions_dir,
        paths.captures_dir,
        paths.grills_dir,
        paths.changes_dir,
        paths.decisions_dir,
        paths.designs_dir,
        # DRAFT 是 start 前的统一确认稿，放进基础目录可以让新旧项目走同一套刷新逻辑。
        paths.drafts_dir,
        paths.exports_dir,
        paths.backups_dir,
        paths.lessons_dir,
        # 导入包和事务日志分开放置。正式包只会在整目录改名后出现，
        # 未完成事务则留在专用目录，便于下一次持锁恢复时准确判断提交边界。
        paths.imports_dir,
        paths.import_transactions_dir,
        paths.change_transactions_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


@contextmanager
def project_lock(paths: ProjectPaths):
    ensure_base_dirs(paths)
    with paths.lock_file.open("a+", encoding="utf-8") as handle:
        # 状态文件会被多个命令反复改写，这里统一加锁，避免 JSONL、SQLite 和 Markdown 不同步。
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def requirement_dir_for_id(paths: ProjectPaths, requirement_id: str) -> Path | None:
    matches = sorted(paths.requirements_dir.glob(f"{requirement_id}-*"))
    if matches:
        return matches[0]
    direct_dir = paths.requirements_dir / requirement_id
    if direct_dir.exists():
        return direct_dir
    return None
