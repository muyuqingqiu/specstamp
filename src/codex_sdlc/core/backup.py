from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.git_tools import find_git_root, run_git
from codex_sdlc.core.lessons import all_global_lessons
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.state import (
    derive_state,
    refresh_materialized_state,
    refresh_start_transaction_state,
    repair_task_contracts,
)
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_bytes, validate_schema_document
from codex_sdlc.legacy.task_pack_reader import inspect_legacy_task_packs


BACKUP_VERSION = 1
IDENTITY_VERSION = 1
DOCUMENT_FIRST_PROFILE = "document-first.v1"
DOCUMENT_FIRST_FORMAL_SCHEMA = "formal-document-first.v3"
LEGACY_READ_ONLY_PROFILE = "legacy_read_only"
AUTO_BACKUP_WINDOW_SECONDS = 60
BACKUP_WRITE_LOCK_NAME = ".codex-sdlc-backup.lock"
AUTO_BACKUP_COMMIT_PREFIX = "自动备份 SDLC 资料："
IDENTITY_COMPARE_FIELDS = ["repo_key", "branch_key", "worktree_key"]
IDENTITY_FIELD_NAMES = {
    "repo_key": "仓库",
    "branch_key": "分支",
    "worktree_key": "工作树",
}
SECRET_VALUE_FIELDS = {
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "client_secret",
    "credential",
    "id_token",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "secret_value",
    "signature",
    "session_token",
    "token",
    "x_api_key",
}
SECRET_VALUE_SUFFIXES = ("api_key", "credential", "password", "passwd", "private_key", "secret", "signature", "token")
SECRET_VALUE_COMPACT_FIELDS = {field.replace("_", "") for field in SECRET_VALUE_FIELDS}
SECRET_REFERENCE_FIELDS = {"schema_version", "kind", "identifier", "access"}
REQUIRED_REQUIREMENT_ARCHIVE_PATHS = [
    "effective/requirement.current.md",
    "effective/design.current.md",
    "effective/test-matrix.current.md",
    "traceability.md",
    "change-map.md",
]
REQUIRED_REQUIREMENT_ARCHIVE_DIRS = [
    "versions/",
    "task-packs/",
    "verifications/",
]
BACKUP_GITIGNORE = "\n".join(
    [
        "# SpecStamp backup git repo",
        ".DS_Store",
        "*.tmp",
        "*.lock",
        ".auto-backup-failures.jsonl",
        "",
    ]
)


def backup_home() -> Path:
    return Path(os.environ.get("CODEX_SDLC_BACKUP_HOME", "~/.codex/sdlc/backups")).expanduser()


def short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalize_remote_url(value: str) -> str:
    clean = value.strip()
    if clean.endswith(".git"):
        clean = clean[:-4]
    return clean.lower()


def git_text(root: Path, args: list[str]) -> str:
    result = run_git(args, root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def current_git_identity(root: Path) -> dict[str, Any]:
    git_root = find_git_root(root)
    if git_root is None:
        path_key = short_hash(f"path:{root.resolve()}")
        return {
            "git_root": "",
            "remote": "",
            "repo_key": f"repo_{path_key}",
            "branch": "",
            "branch_ref": "",
            "branch_key": f"branch_nongit_{path_key}",
            "upstream": "",
            "head": "",
            "git_common_dir": "",
            "gitdir": "",
            "worktree_key": f"wt_{path_key}",
            "dirty": False,
        }

    remote = git_text(git_root, ["config", "--get", "remote.origin.url"])
    if not remote:
        remotes = git_text(git_root, ["remote"]).splitlines()
        if remotes:
            remote = git_text(git_root, ["config", "--get", f"remote.{remotes[0]}.url"])
    common_dir_raw = git_text(git_root, ["rev-parse", "--git-common-dir"])
    gitdir_raw = git_text(git_root, ["rev-parse", "--git-dir"])
    common_dir = str((git_root / common_dir_raw).resolve()) if common_dir_raw and not Path(common_dir_raw).is_absolute() else common_dir_raw
    gitdir = str((git_root / gitdir_raw).resolve()) if gitdir_raw and not Path(gitdir_raw).is_absolute() else gitdir_raw

    if remote:
        repo_source = "remote:" + normalize_remote_url(remote)
    elif common_dir:
        repo_source = "common-dir:" + common_dir
    else:
        repo_source = "path:" + str(git_root.resolve())

    branch_ref = git_text(git_root, ["symbolic-ref", "-q", "HEAD"])
    branch = git_text(git_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    head = git_text(git_root, ["rev-parse", "--short", "HEAD"])
    upstream = git_text(git_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    branch_source = branch_ref or f"detached:{head}" or "unknown"
    dirty = bool(git_text(git_root, ["status", "--short"]))
    return {
        "git_root": str(git_root.resolve()),
        "remote": remote,
        "repo_key": f"repo_{short_hash(repo_source)}",
        "branch": "" if branch == "HEAD" else branch,
        "branch_ref": branch_ref,
        "branch_key": f"branch_{short_hash(branch_source)}",
        "upstream": upstream,
        "head": head,
        "git_common_dir": common_dir,
        "gitdir": gitdir,
        "worktree_key": f"wt_{short_hash(str(root.resolve()) + '|' + gitdir)}",
        "dirty": dirty,
    }


def sdlc_identity_payload(paths: ProjectPaths, identity: dict[str, Any] | None = None) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    current = identity or current_git_identity(paths.root)
    existing = read_json(paths.identity_file, {})
    return {
        "identity_version": IDENTITY_VERSION,
        "created_at": existing.get("created_at") or now,
        "last_checked_at": now,
        "project_path": str(paths.root),
        **current,
    }


def write_sdlc_identity(paths: ProjectPaths, identity: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = sdlc_identity_payload(paths, identity)
    write_json(paths.identity_file, payload)
    return payload


def read_sdlc_identity(paths: ProjectPaths) -> dict[str, Any]:
    return read_json(paths.identity_file, {})


def identity_label(identity: dict[str, Any]) -> str:
    branch = identity.get("branch") or identity.get("branch_ref") or "非 Git 分支"
    head = identity.get("head") or "未知提交"
    return f"{branch}（{head}）"


def identity_mismatch_items(paths: ProjectPaths) -> list[dict[str, str]]:
    stored = read_sdlc_identity(paths)
    if not stored:
        return []
    current = current_git_identity(paths.root)
    mismatches: list[dict[str, str]] = []
    for field in IDENTITY_COMPARE_FIELDS:
        if stored.get(field) != current.get(field):
            mismatches.append(
                {
                    "field": field,
                    "name": IDENTITY_FIELD_NAMES[field],
                    "stored": str(stored.get(field, "")),
                    "current": str(current.get(field, "")),
                }
            )
    return mismatches


def require_matching_sdlc_identity(paths: ProjectPaths) -> None:
    # 写命令的身份检查发生在自动备份之前。这里先收口结构化变更事务，
    # 可以保证身份和备份都只看到完整旧版本或完整新版本。
    active_change_dir = paths.change_transactions_dir / "accept-active"
    if active_change_dir.is_dir() and any(active_change_dir.iterdir()):
        from codex_sdlc.services.change_service import (
            recover_change_accept_transactions,
        )

        recover_change_accept_transactions(paths)
    if not paths.events_file.exists():
        return
    if not paths.identity_file.exists():
        # 老项目首次升级时补一份身份文件；后续再发生分支或 worktree 变化就能拦住。
        write_sdlc_identity(paths)
        return
    mismatches = identity_mismatch_items(paths)
    if not mismatches:
        return
    stored = read_sdlc_identity(paths)
    current = current_git_identity(paths.root)
    lines = [
        "当前 `.codex-sdlc/` 的身份和当前 Git 状态不一致，已停止继续执行。",
        "原因：继续读写会把需求、任务或验证记录写到错误分支或错误工作树。",
        f"- `.codex-sdlc/` 记录：{identity_label(stored)}",
        f"- 当前 Git 状态：{identity_label(current)}",
        "不匹配项：",
    ]
    lines.extend(f"- {item['name']}：记录={item['stored']}，当前={item['current']}" for item in mismatches)
    lines.extend(
        [
            "如果想保留当前需求包：先切回 `.codex-sdlc/` 记录的分支执行 `$sdlc-backup --pin`，确认有备份后再切到目标分支恢复。",
            "如果已经有备份：先用 `$sdlc-backup-list` 查看候选，再用 `$sdlc-restore REQ-xxx --dry-run` 预览，确认后恢复到当前分支。",
            "下一步建议：",
            "- $sdlc-backup-list",
            "- $sdlc-restore --dry-run",
        ]
    )
    raise SdlcError("\n".join(lines), exit_code=1)


def stable_requirement_uid(requirement: dict[str, Any]) -> str:
    existing = str(requirement.get("requirement_uid", "")).strip()
    if existing:
        return existing
    source = "|".join(
        [
            str(requirement.get("requirement_id", "")),
            str(requirement.get("folder_name", "")),
            str(requirement.get("created_at", "")),
            str(requirement.get("title", "")),
        ]
    )
    return f"req_{short_hash(source)}"


def one_line_text(value: str, length: int = 96) -> str:
    clean = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    if len(clean) <= length:
        return clean
    return clean[: max(0, length - 1)].rstrip() + "…"


def requirement_description(requirement: dict[str, Any]) -> str:
    return str(requirement.get("description") or requirement.get("summary") or requirement.get("title") or "")


def normalize_metadata_key(value: object) -> str:
    """把大小写、驼峰、短横线和下划线统一成可比较的字段名。"""

    text = str(value or "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def is_sensitive_metadata_key(value: object) -> bool:
    """只按字段名识别秘密值，不能把普通业务 value 一并清空。"""

    normalized = normalize_metadata_key(value)
    return normalized in SECRET_VALUE_FIELDS or normalized.replace("_", "") in SECRET_VALUE_COMPACT_FIELDS or any(
        normalized.endswith(f"_{suffix}") for suffix in SECRET_VALUE_SUFFIXES
    )


def sanitize_backup_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("schema_version") == "secret-reference.v1":
            # 秘密引用只允许合同内的定位元数据进入备份，未知字段不能跟着业务事件扩散。
            return {
                key: sanitize_backup_metadata(value[key])
                for key in SECRET_REFERENCE_FIELDS
                if key in value
            }
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_metadata_key(key):
                sanitized[key] = "[已脱敏]"
            else:
                sanitized[key] = sanitize_backup_metadata(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_backup_metadata(item) for item in value]
    return value


def sanitize_backup_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [sanitize_backup_metadata(event) for event in events]


def clean_requirement_display_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<!--\s*文件[:：][^\n]*", " ", text)
    text = re.sub(r"\|\s*(功能|来源|路径)[:：][^\n]*", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = text.replace("`", "")
    clean = " ".join(text.split())
    return clean.strip()


def requirement_display_description(requirement: dict[str, Any], length: int = 96) -> str:
    # 备份列表面向人阅读，优先展示摘要和标题；导入资料的文件头只保留在归档里，不进入列表输出。
    for key in ["summary", "description_excerpt", "title", "description"]:
        clean = clean_requirement_display_text(requirement.get(key))
        if clean:
            return one_line_text(clean, length)
    return ""


def requirement_display_title(requirement: dict[str, Any], length: int = 80) -> str:
    clean = clean_requirement_display_text(requirement.get("title"))
    if clean:
        return one_line_text(clean, length)
    return one_line_text(str(requirement.get("title") or "未命名需求"), length)


def safe_timestamp(label: str | None = None) -> str:
    base = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    if not label:
        return base
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in label.strip())[:32]
    return f"{base}-{clean}" if clean else base


def build_backup_metadata(*, automatic: bool, command: str = "", phase: str = "") -> dict[str, str]:
    return {
        "mode": "auto" if automatic else "manual",
        "command": command.strip(),
        "phase": phase.strip(),
    }


def ensure_backup_home() -> Path:
    root = backup_home()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def backup_write_lock(root: Path):
    """让所有项目共用一把备份锁，避免同时改全局索引和 Git 仓库。"""

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / BACKUP_WRITE_LOCK_NAME
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_auto_backup_failure(*, command: str, phase: str, message: str) -> None:
    """自动备份不能打断主命令，但失败必须留下可复查记录。"""

    try:
        root = ensure_backup_home()
        with backup_write_lock(root):
            record = {
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "command": command,
                "phase": phase,
                "message": message,
            }
            with (root / ".auto-backup-failures.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 记录失败不能覆盖原先的命令结果。
        return


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 索引和清单会被恢复、列表和备份同时读取，完成写入后再替换，读取方不会拿到半截 JSON。
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def run_backup_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def ensure_backup_git_repo(root: Path) -> dict[str, Any]:
    if shutil.which("git") is None:
        return {"status": "unavailable", "message": "本机没有找到 git 命令"}

    root.mkdir(parents=True, exist_ok=True)
    initialized = False
    if not (root / ".git").exists():
        init_result = run_backup_git(root, ["init"])
        if init_result.returncode != 0:
            return {"status": "failed", "message": init_result.stderr.strip() or init_result.stdout.strip()}
        initialized = True

    for key, value in [
        ("user.name", "SpecStamp Backup"),
        ("user.email", "codex-sdlc-backup@local"),
        # 自动维护会在提交后脱离当前命令做完整打包，只能由受控的维护命令串行执行。
        ("maintenance.auto", "false"),
    ]:
        run_backup_git(root, ["config", key, value])

    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(BACKUP_GITIGNORE, encoding="utf-8")
    elif ".auto-backup-failures.jsonl" not in gitignore.read_text(encoding="utf-8"):
        gitignore.write_text(gitignore.read_text(encoding="utf-8").rstrip() + "\n.auto-backup-failures.jsonl\n", encoding="utf-8")

    return {"status": "ready", "initialized": initialized}


def can_amend_recent_auto_backup(root: Path) -> bool:
    result = run_backup_git(root, ["log", "-1", "--format=%ct%x00%s"])
    if result.returncode != 0 or "\x00" not in result.stdout:
        return False
    timestamp_text, subject = result.stdout.strip().split("\x00", 1)
    try:
        created_at = datetime.fromtimestamp(int(timestamp_text)).astimezone()
    except ValueError:
        return False
    return subject.startswith(AUTO_BACKUP_COMMIT_PREFIX) and datetime.now().astimezone() - created_at <= timedelta(
        seconds=AUTO_BACKUP_WINDOW_SECONDS
    )


def backup_git_commit(root: Path, message: str, *, automatic: bool = False) -> dict[str, Any]:
    repo = ensure_backup_git_repo(root)
    if repo.get("status") != "ready":
        return repo

    status_result = run_backup_git(root, ["status", "--short"])
    if status_result.returncode != 0:
        return {"status": "failed", "message": status_result.stderr.strip() or status_result.stdout.strip()}
    if not status_result.stdout.strip():
        return {"status": "clean", "message": "没有新的备份变更需要提交"}

    add_result = run_backup_git(root, ["add", "-A"])
    if add_result.returncode != 0:
        return {"status": "failed", "message": add_result.stderr.strip() or add_result.stdout.strip()}

    amend = automatic and can_amend_recent_auto_backup(root)
    commit_args = ["commit", "--amend", "-m", message] if amend else ["commit", "-m", message]
    commit_result = run_backup_git(root, commit_args)
    if commit_result.returncode != 0:
        return {"status": "failed", "message": commit_result.stderr.strip() or commit_result.stdout.strip()}

    head_result = run_backup_git(root, ["rev-parse", "--short", "HEAD"])
    return {
        "status": "committed",
        "message": commit_result.stdout.strip(),
        "head": head_result.stdout.strip() if head_result.returncode == 0 else "",
        "initialized": repo.get("initialized", False),
        "amended": amend,
    }


def _read_archive_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效的 UTF-8 JSON：{path}。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是 JSON 对象：{path}。", exit_code=1)
    return document


def _controlled_archive_file(root: Path, relative_path: object, *, label: str) -> Path:
    """备份只能消费正式目录内的真实文件，不能让链接在校验后换到目录外。"""

    clean = str(relative_path or "").strip().replace("\\", "/")
    relative = Path(clean)
    if (
        not clean
        or "\x00" in clean
        or relative.is_absolute()
        or relative == Path(".")
        or ".." in relative.parts
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise SdlcError(f"{label}不是安全的档案内相对路径：{relative_path}。", exit_code=1)
    if root.is_symlink() or not root.is_dir():
        raise SdlcError(f"{label}的档案根目录必须是真实目录。", exit_code=1)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"{label}不能经过符号链接：{clean}。", exit_code=1)
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{label}不存在或越过正式档案目录：{clean}。", exit_code=1) from exc
    if not current.is_file():
        raise SdlcError(f"{label}必须是普通文件：{clean}。", exit_code=1)
    return current


def _requirement_file_hashes(requirement_dir: Path, relative_root: str) -> dict[str, str]:
    root = requirement_dir / relative_root
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise SdlcError(f"{requirement_dir.name} 的 {relative_root} 必须是真实目录。", exit_code=1)
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(requirement_dir).as_posix()
        if path.is_symlink():
            raise SdlcError(f"{requirement_dir.name} 的档案不能包含符号链接：{relative}。", exit_code=1)
        if path.is_file():
            result[relative] = sha256_bytes(path.read_bytes())
        elif not path.is_dir():
            raise SdlcError(f"{requirement_dir.name} 的档案包含不支持的文件类型：{relative}。", exit_code=1)
    return result


def requirement_archive_contract(requirement_dir: Path) -> dict[str, object]:
    """识别两类正式档案；历史 facts 只记录只读边界，不进入新流程门禁。"""

    original_hashes = _requirement_file_hashes(requirement_dir, "original")
    formal_path = requirement_dir / "original/formal.v3.json"
    if not formal_path.exists() and not formal_path.is_symlink():
        return {
            "workflow_profile": LEGACY_READ_ONLY_PROFILE,
            "formal_kind": "materialized_legacy",
            "original_files": original_hashes,
        }

    formal_path = _controlled_archive_file(
        requirement_dir,
        "original/formal.v3.json",
        label="formal.v3",
    )
    formal = _read_archive_json(formal_path, label="formal.v3")
    profile = formal.get("workflow_profile")
    if formal.get("formal_contract_version") == "formal.v3" and profile is None:
        return {
            "workflow_profile": LEGACY_READ_ONLY_PROFILE,
            "formal_kind": "facts",
            "original_files": original_hashes,
            "formal_sha256": sha256_bytes(formal_path.read_bytes()),
        }
    if profile != DOCUMENT_FIRST_PROFILE:
        raise SdlcError(
            f"{requirement_dir.name} 的 workflow_profile 不受支持：{profile}。",
            exit_code=1,
        )

    from codex_sdlc.core.artifact_index import formal_manifest_entries, validate_artifact_index_document
    from codex_sdlc.core.reference_index import validate_reference_index_file

    validate_schema_document(formal, schema_name=DOCUMENT_FIRST_FORMAL_SCHEMA)
    artifact_reference = formal.get("artifact_index")
    if not isinstance(artifact_reference, dict):
        raise SdlcError(f"{requirement_dir.name} 的 formal.v3 缺少 artifact_index。", exit_code=1)
    index_path = _controlled_archive_file(
        requirement_dir,
        artifact_reference.get("archive_path"),
        label="artifact-index archive_path",
    )
    if sha256_bytes(index_path.read_bytes()) != artifact_reference.get("sha256"):
        raise SdlcError(f"{requirement_dir.name} 的 artifact-index 完整哈希与 formal.v3 不一致。", exit_code=1)
    artifact_index = validate_artifact_index_document(
        _read_archive_json(index_path, label="artifact-index.v1")
    )
    if (
        artifact_index.get("draft_id") != formal.get("source_draft_id")
        or artifact_index.get("draft_revision_sha256") != formal.get("source_revision_sha256")
    ):
        raise SdlcError(f"{requirement_dir.name} 的 formal.v3 与 artifact-index 来源不一致。", exit_code=1)
    manifest = formal.get("artifact_manifest")
    if (
        not isinstance(manifest, list)
        or canonical_sha256(manifest) != canonical_sha256(formal_manifest_entries(artifact_index))
    ):
        raise SdlcError(f"{requirement_dir.name} 的 formal.v3 与 artifact-index 正式清单不一致。", exit_code=1)

    expected_original = {
        "original/formal.v3.json",
        str(artifact_reference.get("archive_path") or ""),
    }
    for item in manifest:
        if not isinstance(item, dict):
            raise SdlcError(f"{requirement_dir.name} 的正式清单必须是对象列表。", exit_code=1)
        archive_path = str(item.get("archive_path") or "")
        target = _controlled_archive_file(requirement_dir, archive_path, label="正式 archive_path")
        if sha256_bytes(target.read_bytes()) != item.get("sha256"):
            raise SdlcError(f"{requirement_dir.name} 的正式原文哈希不一致：{archive_path}。", exit_code=1)
        expected_original.add(archive_path)
    if set(original_hashes) != expected_original:
        missing = sorted(expected_original - set(original_hashes))
        extra = sorted(set(original_hashes) - expected_original)
        details = []
        if missing:
            details.append("缺少：" + "、".join(missing))
        if extra:
            details.append("未登记：" + "、".join(extra))
        raise SdlcError(f"{requirement_dir.name} 的 original 文件集合不一致：" + "；".join(details), exit_code=1)

    reference_path = _controlled_archive_file(
        requirement_dir,
        "reference-index.v1.json",
        label="reference-index.v1",
    )
    reference = validate_reference_index_file(requirement_dir, reference_path)
    status_path = _controlled_archive_file(requirement_dir, "status.json", label="status.json")
    status = _read_archive_json(status_path, label="status.json")
    requirement_id = str(status.get("requirement_id") or "")
    if (
        reference.get("requirement_id") != requirement_id
        or not (
            requirement_dir.name == requirement_id
            or requirement_dir.name.startswith(f"{requirement_id}-")
        )
        or status.get("source_draft_id") != formal.get("source_draft_id")
    ):
        raise SdlcError(f"{requirement_dir.name} 的状态、引用索引和正式来源不一致。", exit_code=1)

    protected_files = {
        **original_hashes,
        "reference-index.v1.json": sha256_bytes(reference_path.read_bytes()),
        "status.json": sha256_bytes(status_path.read_bytes()),
    }
    for mapping_name in ("current_files",):
        mapping = status.get(mapping_name)
        if not isinstance(mapping, dict):
            raise SdlcError(f"{requirement_dir.name} 的 status.json 缺少 {mapping_name}。", exit_code=1)
        for relative_path in mapping.values():
            target = _controlled_archive_file(requirement_dir, relative_path, label="当前结构化版本")
            protected_files[str(relative_path)] = sha256_bytes(target.read_bytes())
    versions = status.get("versions")
    if not isinstance(versions, dict):
        raise SdlcError(f"{requirement_dir.name} 的 status.json 缺少 versions。", exit_code=1)
    for version in versions.values():
        relative_path = f"versions/{version}.json"
        target = _controlled_archive_file(requirement_dir, relative_path, label="正式结构化版本")
        protected_files[relative_path] = sha256_bytes(target.read_bytes())
    return {
        "workflow_profile": DOCUMENT_FIRST_PROFILE,
        "formal_kind": "document_first",
        "requirement_id": requirement_id,
        "source_draft_id": formal.get("source_draft_id"),
        "source_revision_sha256": formal.get("source_revision_sha256"),
        "formal_manifest_sha256": canonical_sha256(manifest),
        "artifact_index_sha256": sha256_bytes(index_path.read_bytes()),
        "reference_index_sha256": sha256_bytes(reference_path.read_bytes()),
        "original_files": original_hashes,
        "protected_files": protected_files,
    }


def requirement_contracts(paths: ProjectPaths, state: dict[str, Any]) -> dict[str, dict[str, object]]:
    contracts: dict[str, dict[str, object]] = {}
    for requirement in state.get("requirements", {}).values():
        folder_name = str(requirement.get("folder_name") or "")
        requirement_dir = paths.requirements_dir / folder_name
        if not requirement_dir.is_dir() or requirement_dir.is_symlink():
            raise SdlcError(f"{folder_name or '正式需求目录'} 不存在或不是受控目录，不能备份。", exit_code=1)
        contracts[folder_name] = requirement_archive_contract(requirement_dir)
    return contracts


def _preserve_raw_archive_bytes(root: Path, path: Path) -> bool:
    """需求档案已经有自己的哈希合同，备份不能为了脱敏而重新排版正式 JSON。"""

    relative = path.relative_to(root)
    return len(relative.parts) >= 3 and relative.parts[:2] == (".codex-sdlc", "requirements")


def add_path_tree_to_archive(
    archive: tarfile.TarFile,
    root: Path,
    relative_path: str,
    *,
    preserve_events_raw: bool = False,
) -> None:
    path = root / relative_path
    if not path.exists():
        return
    if path.is_file():
        add_file_to_archive(archive, root, path, preserve_events_raw=preserve_events_raw)
        return
    for child in sorted(path.rglob("*")):
        arcname = str(child.relative_to(root))
        if child.is_file():
            add_file_to_archive(archive, root, child, preserve_events_raw=preserve_events_raw)
        elif child.is_dir() and not any(child.iterdir()):
            add_empty_dir_to_archive(archive, arcname)


def add_file_to_archive(
    archive: tarfile.TarFile,
    root: Path,
    path: Path,
    *,
    preserve_events_raw: bool = False,
) -> None:
    arcname = str(path.relative_to(root))
    keep_raw = _preserve_raw_archive_bytes(root, path) or (
        preserve_events_raw
        and (
            arcname == ".codex-sdlc/events.jsonl"
            or arcname.startswith(".codex-sdlc/drafts/")
        )
    )
    sanitized = None if keep_raw else sanitized_structured_file(path)
    if sanitized is None:
        archive.add(path, arcname=arcname, recursive=False)
        return
    info = tarfile.TarInfo(arcname)
    info.size = len(sanitized)
    info.mode = path.stat().st_mode & 0o777
    info.mtime = int(path.stat().st_mtime)
    archive.addfile(info, fileobj=io.BytesIO(sanitized))


def sanitized_structured_file(path: Path) -> bytes | None:
    """事件和结构化投影进入归档前再做一次字段级脱敏，防止异常事件绕过命令合同。"""

    if path.suffix == ".json":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return (json.dumps(sanitize_backup_metadata(document), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.name.endswith(".jsonl") or path.name.endswith(".jsonl.bak"):
        try:
            lines = []
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                lines.append(json.dumps(sanitize_backup_metadata(json.loads(raw_line)), ensure_ascii=False) + "\n")
            return "".join(lines).encode("utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
    return None


def add_empty_dir_to_archive(archive: tarfile.TarFile, arcname: str) -> None:
    clean_arcname = arcname.rstrip("/")
    existing_names = set(archive.getnames())
    if clean_arcname in existing_names or f"{clean_arcname}/" in existing_names:
        return
    info = tarfile.TarInfo(clean_arcname)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = int(datetime.now().timestamp())
    archive.addfile(info)


def create_project_archive(root: Path, archive_path: Path, manifest: dict[str, Any]) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    preserve_events_raw = any(
        isinstance(item, dict)
        and isinstance(item.get("archive_contract"), dict)
        and item["archive_contract"].get("workflow_profile") == DOCUMENT_FIRST_PROFILE
        for item in manifest.get("sdlc", {}).get("requirements", [])
    )
    with tarfile.open(archive_path, "w:gz") as archive:
        add_path_tree_to_archive(
            archive,
            root,
            ".codex-sdlc",
            preserve_events_raw=preserve_events_raw,
        )
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest_bytes)
        archive.addfile(info, fileobj=io.BytesIO(manifest_bytes))


def create_requirement_archive(
    root: Path,
    archive_path: Path,
    requirement: dict[str, Any],
    *,
    archive_contract: dict[str, object] | None = None,
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_dir = root / ".codex-sdlc" / "requirements" / requirement["folder_name"]
    with tarfile.open(archive_path, "w:gz") as archive:
        if requirement_dir.exists():
            add_path_tree_to_archive(archive, root, str(requirement_dir.relative_to(root)))
            # 新需求还没有执行包或验证记录时，这些目录可能是空的。
            # 显式写入空目录，恢复和体检时就能看出“资料结构完整，只是还没产生记录”。
            requirement_prefix = str(requirement_dir.relative_to(root)).rstrip("/")
            for relative_path in REQUIRED_REQUIREMENT_ARCHIVE_DIRS:
                add_empty_dir_to_archive(archive, f"{requirement_prefix}/{relative_path}")
            if (archive_contract or {}).get("workflow_profile") == DOCUMENT_FIRST_PROFILE:
                status = _read_archive_json(requirement_dir / "status.json", label="status.json")
                transaction_id = str(status.get("transaction_id") or "")
                receipt = root / ".codex-sdlc/start-transactions/completed" / f"{transaction_id}.json"
                if not transaction_id or not receipt.is_file() or receipt.is_symlink():
                    raise SdlcError(
                        f"{requirement['requirement_id']} 缺少建档完成回执，不能生成可恢复快照。",
                        exit_code=1,
                    )
                add_file_to_archive(archive, root, receipt)
        lessons_dir = root / ".codex-sdlc" / "lessons"
        if lessons_dir.exists():
            add_path_tree_to_archive(archive, root, str(lessons_dir.relative_to(root)))


def project_manifest(
    paths: ProjectPaths,
    state: dict[str, Any],
    identity: dict[str, Any],
    created_at: str,
    *,
    archive_contracts: dict[str, dict[str, object]] | None = None,
    pinned: bool = False,
    backup: dict[str, str] | None = None,
) -> dict[str, Any]:
    contracts = archive_contracts or {}
    requirements = [
        {
            "requirement_id": requirement["requirement_id"],
            "requirement_uid": stable_requirement_uid(requirement),
            "folder_name": requirement["folder_name"],
            "title": requirement_display_title(requirement, 160),
            "summary": requirement_display_description(requirement, 160) or requirement_display_title(requirement, 160),
            "description": requirement_display_description(requirement, 240),
            "description_excerpt": requirement_display_description(requirement),
            "status": requirement["status"],
            "task_count": len(requirement.get("tasks", [])),
            "archive_contract": contracts.get(str(requirement.get("folder_name") or ""), {}),
        }
        for requirement in state["requirements"].values()
    ]
    manifest = {
        "backup_version": BACKUP_VERSION,
        "backup_kind": "project",
        "created_at": created_at,
        "pinned": pinned,
        "backup": sanitize_backup_metadata(backup or build_backup_metadata(automatic=False)),
        "project_name": paths.root.name,
        "project_path": str(paths.root),
        "repo_key": identity["repo_key"],
        "branch_key": identity["branch_key"],
        "worktree_key": identity["worktree_key"],
        "git": identity,
        "sdlc": {
            "active_requirements": [item["requirement_id"] for item in state["active_requirements"]],
            "requirement_count": len(state["requirements"]),
            "task_count": sum(len(item.get("tasks", [])) for item in state["requirements"].values()),
            "global_lesson_count": len(all_global_lessons(paths)),
            "last_event_id": state["events"][-1]["event_id"] if state["events"] else "",
            "requirements": requirements,
        },
    }
    return sanitize_backup_metadata(manifest)


def requirement_manifest(
    paths: ProjectPaths,
    requirement: dict[str, Any],
    identity: dict[str, Any],
    created_at: str,
    *,
    archive_contract: dict[str, object] | None = None,
    pinned: bool = False,
    backup: dict[str, str] | None = None,
) -> dict[str, Any]:
    manifest = {
        "backup_version": BACKUP_VERSION,
        "backup_kind": "requirement",
        "created_at": created_at,
        "pinned": pinned,
        "backup": sanitize_backup_metadata(backup or build_backup_metadata(automatic=False)),
        "project_name": paths.root.name,
        "project_path": str(paths.root),
        "repo_key": identity["repo_key"],
        "branch_key": identity["branch_key"],
        "worktree_key": identity["worktree_key"],
        "requirement_uid": stable_requirement_uid(requirement),
        "requirement_id": requirement["requirement_id"],
        "folder_name": requirement["folder_name"],
        "title": requirement_display_title(requirement, 160),
        "summary": requirement_display_description(requirement, 160) or requirement_display_title(requirement, 160),
        "description": requirement_display_description(requirement, 240),
        "description_excerpt": requirement_display_description(requirement),
        "status": requirement["status"],
        "task_count": len(requirement.get("tasks", [])),
        "archive_contract": archive_contract or {},
        "git": identity,
        "branch_binding": {
            "branch": identity.get("branch", ""),
            "branch_ref": identity.get("branch_ref", ""),
            "upstream": identity.get("upstream", ""),
            "head": identity.get("head", ""),
            "status": requirement["status"],
            "last_task": next((task["task_id"] for task in reversed(requirement.get("tasks", [])) if task["status"] != "todo"), ""),
        },
    }
    return sanitize_backup_metadata(manifest)


def requirement_event_slice(state: dict[str, Any], requirement_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in state["events"]
        if event.get("requirement_id") == requirement_id
        or (
            event.get("event_type") == "project_initialized"
            and not event.get("requirement_id")
        )
    ]


def backup_tree_has_snapshots(root: Path) -> bool:
    repos_dir = root / "repos"
    if not repos_dir.exists():
        return False
    return any(repos_dir.glob("*/project-snapshots/*/*/*.manifest.json")) or any(
        repos_dir.glob("*/requirements/*/snapshots/*/manifest.json")
    )


def read_index_data(base: Path) -> dict[str, Any]:
    default = {"backup_version": BACKUP_VERSION, "project_snapshots": [], "requirement_snapshots": []}
    index_path = base / "index.json"
    data = read_json(index_path, default)
    if not isinstance(data, dict):
        data = default
    data.setdefault("backup_version", BACKUP_VERSION)
    data.setdefault("project_snapshots", [])
    data.setdefault("requirement_snapshots", [])
    data["project_snapshots"] = sanitize_backup_metadata(data["project_snapshots"])
    data["requirement_snapshots"] = sanitize_backup_metadata(data["requirement_snapshots"])
    return data


def index_needs_rebuild(base: Path, data: dict[str, Any]) -> bool:
    return backup_tree_has_snapshots(base) and (
        not (base / "index.json").exists() or (not data["project_snapshots"] and not data["requirement_snapshots"])
    )


def load_index(root: Path | None = None, *, rebuild_if_missing: bool = True) -> dict[str, Any]:
    base = root or backup_home()
    data = read_index_data(base)
    if not rebuild_if_missing or not index_needs_rebuild(base, data):
        return data
    # 缺索引时重建也属于写操作，必须和正在生成快照的进程共用同一把锁。
    with backup_write_lock(base):
        data = read_index_data(base)
        if index_needs_rebuild(base, data):
            return _rebuild_index_from_disk_locked(base)
    return data


def save_index(data: dict[str, Any], root: Path | None = None) -> None:
    base = root or ensure_backup_home()
    write_json(base / "index.json", sanitize_backup_metadata(data))


def upsert_index_entry(entries: list[dict[str, Any]], entry: dict[str, Any], key: str = "archive") -> None:
    entries[:] = [item for item in entries if item.get(key) != entry.get(key)]
    entries.append(entry)
    entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)


def backup_entry_metadata(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "mode": str(entry.get("backup_mode", "manual")),
        "command": str(entry.get("backup_command", "")),
        "phase": str(entry.get("backup_phase", "")),
    }


def backup_metadata_index_fields(metadata: dict[str, str]) -> dict[str, str]:
    return {
        "backup_mode": metadata["mode"],
        "backup_command": metadata["command"],
        "backup_phase": metadata["phase"],
    }


def entry_is_recent_auto_backup(entry: dict[str, Any], identity: dict[str, Any], metadata: dict[str, str]) -> bool:
    if backup_entry_metadata(entry) != metadata:
        return False
    if any(entry.get(field) != identity.get(field) for field in IDENTITY_COMPARE_FIELDS):
        return False
    try:
        created_at = datetime.fromisoformat(str(entry.get("created_at", "")))
    except ValueError:
        return False
    return datetime.now().astimezone() - created_at <= timedelta(seconds=AUTO_BACKUP_WINDOW_SECONDS)


def remove_project_snapshot(entry: dict[str, Any]) -> None:
    archive = Path(str(entry.get("archive", "")))
    archive.unlink(missing_ok=True)
    archive.with_name(archive.name.replace(".tar.gz", ".manifest.json")).unlink(missing_ok=True)


def remove_requirement_snapshot(entry: dict[str, Any]) -> None:
    snapshot_dir = Path(str(entry.get("snapshot_dir", "")))
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def coalesce_recent_auto_snapshots(
    index_data: dict[str, Any], identity: dict[str, Any], metadata: dict[str, str]
) -> dict[str, int]:
    """同一项目的短时间自动快照只留最新一份，手动和固定快照永远不参与合并。"""

    removed_project = 0
    removed_requirement = 0
    for key, remove_snapshot in [
        ("project_snapshots", remove_project_snapshot),
        ("requirement_snapshots", remove_requirement_snapshot),
    ]:
        kept_entries = []
        for entry in index_data.get(key, []):
            if entry.get("pinned") or not entry_is_recent_auto_backup(entry, identity, metadata):
                kept_entries.append(entry)
                continue
            remove_snapshot(entry)
            if key == "project_snapshots":
                removed_project += 1
            else:
                removed_requirement += 1
        index_data[key] = kept_entries
    return {"project": removed_project, "requirement": removed_requirement}


def update_latest_project_archive(archive_path: Path, latest_path: Path) -> None:
    latest_path.unlink(missing_ok=True)
    try:
        # 最新快照只是快捷入口，不应再复制一份完整归档占用磁盘。
        os.link(archive_path, latest_path)
    except OSError:
        shutil.copy2(archive_path, latest_path)


def create_backup(
    paths: ProjectPaths,
    *,
    requirement_id: str | None = None,
    label: str | None = None,
    pinned: bool = False,
    automatic: bool = False,
    command: str = "",
    phase: str = "",
) -> dict[str, Any]:
    from codex_sdlc.core.start_transaction import (
        require_no_unrecovered_start_transaction,
    )
    from codex_sdlc.services.change_service import (
        require_no_unrecovered_change_accept_transaction,
    )

    # 未完成建档可能已经追加事件但还没有正式目录。这样的快照无法完整恢复，
    # 所以手动和自动备份都必须在读取状态、创建归档之前统一拒绝。
    require_no_unrecovered_start_transaction(paths)
    require_no_unrecovered_change_accept_transaction(paths)
    root = ensure_backup_home()
    with backup_write_lock(root):
        return _create_backup_locked(
            paths,
            root=root,
            requirement_id=requirement_id,
            label=label,
            pinned=pinned,
            automatic=automatic,
            command=command,
            phase=phase,
        )


def _create_backup_locked(
    paths: ProjectPaths,
    *,
    root: Path,
    requirement_id: str | None,
    label: str | None,
    pinned: bool,
    automatic: bool,
    command: str,
    phase: str,
) -> dict[str, Any]:
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，不能备份。")
    state = derive_state(paths)
    legacy_state: dict[str, object] = state
    if requirement_id:
        legacy_state = {
            "requirements": {
                key: requirement
                for key, requirement in state.get("requirements", {}).items()
                if key == requirement_id or stable_requirement_uid(requirement) == requirement_id
            }
        }
    legacy_task_packs = inspect_legacy_task_packs(paths, legacy_state)
    unsafe_legacy_links = [
        issue
        for result in legacy_task_packs
        for issue in result.issues
        if issue.code == "symlink"
    ]
    if unsafe_legacy_links:
        details = "；".join(f"{item.path}：{item.message}" for item in unsafe_legacy_links)
        raise SdlcError(
            f"既有任务执行包包含符号链接，不能创建可恢复备份：{details}。旧档案保持原样，请人工处理后重试。",
            exit_code=1,
        )
    # 正式档案先完整体检，再创建任何快照或更新索引，损坏档案不能留下半份可选备份。
    archive_contracts = requirement_contracts(paths, state)
    identity = current_git_identity(paths.root)
    if not paths.identity_file.exists():
        write_sdlc_identity(paths, identity)
    timestamp = safe_timestamp(label)
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    backup_metadata = build_backup_metadata(automatic=automatic, command=command, phase=phase)
    repo_dir = root / "repos" / identity["repo_key"]
    project_results: list[dict[str, Any]] = []
    requirement_results: list[dict[str, Any]] = []

    target_requirements = list(state["requirements"].values())
    if requirement_id:
        target_requirements = [
            requirement
            for requirement in target_requirements
            if requirement["requirement_id"] == requirement_id or stable_requirement_uid(requirement) == requirement_id
        ]
        if not target_requirements:
            raise SdlcError(f"没有找到需求：{requirement_id}")

    index_data = read_index_data(root)
    if index_needs_rebuild(root, index_data):
        index_data = _rebuild_index_from_disk_locked(root)
    coalesced = {"project": 0, "requirement": 0}
    if automatic:
        coalesced = coalesce_recent_auto_snapshots(index_data, identity, backup_metadata)

    if requirement_id is None:
        manifest = project_manifest(
            paths,
            state,
            identity,
            created_at,
            archive_contracts=archive_contracts,
            pinned=pinned,
            backup=backup_metadata,
        )
        snapshot_dir = repo_dir / "project-snapshots" / identity["branch_key"] / identity["worktree_key"]
        archive_path = snapshot_dir / f"{timestamp}.tar.gz"
        create_project_archive(paths.root, archive_path, manifest)
        latest_path = snapshot_dir / "latest.tar.gz"
        update_latest_project_archive(archive_path, latest_path)
        write_json(snapshot_dir / f"{timestamp}.manifest.json", manifest)
        write_json(snapshot_dir / "manifest.json", manifest)
        project_results.append({"archive": str(archive_path), "latest": str(latest_path), "manifest": manifest})

    for requirement in target_requirements:
        req_uid = stable_requirement_uid(requirement)
        req_snapshot_dir = repo_dir / "requirements" / req_uid / "snapshots" / timestamp
        folder_name = str(requirement.get("folder_name") or "")
        archive_contract = archive_contracts.get(folder_name, {})
        req_manifest = requirement_manifest(
            paths,
            requirement,
            identity,
            created_at,
            archive_contract=archive_contract,
            pinned=pinned,
            backup=backup_metadata,
        )
        req_archive = req_snapshot_dir / "requirement-package.tar.gz"
        create_requirement_archive(
            paths.root,
            req_archive,
            requirement,
            archive_contract=archive_contract,
        )
        events_slice = sanitize_backup_events(requirement_event_slice(state, requirement["requirement_id"]))
        write_json(req_snapshot_dir / "manifest.json", req_manifest)
        write_json(req_snapshot_dir / "branch-bindings.json", {"bindings": [req_manifest["branch_binding"]]})
        (req_snapshot_dir / "events.slice.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events_slice),
            encoding="utf-8",
        )
        write_json(repo_dir / "requirements" / req_uid / "manifest.json", req_manifest)
        requirement_results.append({"archive": str(req_archive), "manifest": req_manifest})

    repo_manifest = {
        "repo_key": identity["repo_key"],
        "updated_at": created_at,
        "remote": identity.get("remote", ""),
        "project_name": paths.root.name,
    }
    write_json(repo_dir / "manifest.json", repo_manifest)

    for result in project_results:
        manifest = result["manifest"]
        upsert_index_entry(
            index_data["project_snapshots"],
            {
                "created_at": manifest["created_at"],
                "repo_key": manifest["repo_key"],
                "branch_key": manifest["branch_key"],
                "worktree_key": manifest["worktree_key"],
                "project_path": manifest["project_path"],
                "project_name": manifest["project_name"],
                "archive": result["archive"],
                "latest": result["latest"],
                "snapshot": Path(result["archive"]).name.replace(".tar.gz", ""),
                "requirements": sanitize_backup_metadata(manifest["sdlc"]["requirements"]),
                "pinned": manifest.get("pinned", False),
                "branch": manifest.get("git", {}).get("branch", ""),
                "branch_ref": manifest.get("git", {}).get("branch_ref", ""),
                "head": manifest.get("git", {}).get("head", ""),
                **backup_metadata_index_fields(backup_metadata),
            },
        )
    for result in requirement_results:
        manifest = result["manifest"]
        upsert_index_entry(
            index_data["requirement_snapshots"],
            {
                "created_at": manifest["created_at"],
                "repo_key": manifest["repo_key"],
                "branch_key": manifest["branch_key"],
                "worktree_key": manifest["worktree_key"],
                "project_path": manifest["project_path"],
                "project_name": manifest["project_name"],
                "requirement_uid": manifest["requirement_uid"],
                "requirement_id": manifest["requirement_id"],
                "title": clean_requirement_display_text(manifest["title"]),
                "summary": clean_requirement_display_text(manifest.get("summary", manifest["title"])),
                "description": clean_requirement_display_text(manifest.get("description", "")),
                "description_excerpt": requirement_display_description(manifest),
                "status": manifest["status"],
                "archive": result["archive"],
                "snapshot_dir": str(Path(result["archive"]).parent),
                "snapshot": Path(result["archive"]).parent.name,
                "pinned": manifest.get("pinned", False),
                "branch": manifest.get("git", {}).get("branch", ""),
                "branch_ref": manifest.get("git", {}).get("branch_ref", ""),
                "head": manifest.get("git", {}).get("head", ""),
                **backup_metadata_index_fields(backup_metadata),
            },
        )
    save_index(index_data, root)
    git_message = f"{AUTO_BACKUP_COMMIT_PREFIX}{timestamp}" if automatic else f"备份 SDLC 资料：{timestamp}"
    git_result = backup_git_commit(root, git_message, automatic=automatic)
    return {
        "project_snapshots": project_results,
        "requirement_snapshots": requirement_results,
        "backup_home": str(root),
        "git": git_result,
        "coalesced": coalesced,
        "legacy_task_packs": [item.as_dict() for item in legacy_task_packs],
    }


def candidate_score(identity: dict[str, Any], entry: dict[str, Any], project_path: Path) -> int:
    score = 0
    same_repo = entry.get("repo_key") == identity.get("repo_key")
    same_path = entry.get("project_path") == str(project_path)
    if not same_repo and not same_path:
        return 0
    if same_repo:
        score += 50
    else:
        # Git 目录重建后 repo_key 可能变化，同一路径的备份仍然值得提示，但不能和同仓库候选混在同一优先级。
        score += 30
    if entry.get("branch_key") == identity.get("branch_key"):
        score += 20
    if entry.get("worktree_key") == identity.get("worktree_key"):
        score += 20
    if same_path:
        score += 10
    return score


def candidate_snapshot(entry: dict[str, Any]) -> str:
    if entry.get("snapshot"):
        return str(entry["snapshot"])
    snapshot_dir = str(entry.get("snapshot_dir") or "")
    if snapshot_dir:
        return Path(snapshot_dir).name
    archive = str(entry.get("archive") or "")
    if archive:
        return Path(archive).name.replace(".tar.gz", "")
    return ""


def snapshot_matches(entry: dict[str, Any], snapshot: str | None) -> bool:
    if not snapshot:
        return True
    token = snapshot.strip()
    if not token:
        return True
    values = [
        candidate_snapshot(entry),
        str(entry.get("created_at", "")),
        str(entry.get("archive", "")),
        str(entry.get("snapshot_dir", "")),
    ]
    return any(token in value for value in values)


def enrich_project_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    archive = Path(candidate.get("archive", ""))
    manifest_file = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))
    manifest = read_json(manifest_file, {})
    if manifest.get("sdlc", {}).get("requirements"):
        candidate["requirements"] = sanitize_backup_metadata(manifest["sdlc"]["requirements"])
    candidate["snapshot"] = candidate_snapshot(candidate)
    return sanitize_backup_metadata(candidate)


def enrich_requirement_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    snapshot_dir = Path(str(candidate.get("snapshot_dir", "")))
    manifest = read_json(snapshot_dir / "manifest.json", {})
    for key in ["summary", "description", "description_excerpt", "folder_name"]:
        if manifest.get(key) and not candidate.get(key):
            candidate[key] = manifest[key]
    candidate["snapshot"] = candidate_snapshot(candidate)
    return sanitize_backup_metadata(candidate)


def project_backup_candidates(root: Path, *, limit: int = 10, snapshot: str | None = None) -> list[dict[str, Any]]:
    index_data = load_index()
    identity = current_git_identity(root)
    candidates = []
    for entry in index_data.get("project_snapshots", []):
        archive = Path(entry.get("archive", ""))
        if not archive.exists():
            continue
        if not snapshot_matches(entry, snapshot):
            continue
        score = candidate_score(identity, entry, root)
        if score <= 0:
            continue
        candidate = dict(entry)
        enrich_project_candidate(candidate)
        candidate["score"] = score
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            item["score"],
            len(item.get("requirements", [])),
            item.get("created_at", ""),
            item.get("archive", ""),
        ),
        reverse=True,
    )
    return candidates[:limit]


def requirement_backup_candidates(
    root: Path,
    requirement: str | None = None,
    *,
    limit: int = 20,
    strict_repo: bool = True,
    snapshot: str | None = None,
) -> list[dict[str, Any]]:
    index_data = load_index()
    identity = current_git_identity(root)
    candidates = []
    for entry in index_data.get("requirement_snapshots", []):
        archive = Path(entry.get("archive", ""))
        if not archive.exists():
            continue
        if not snapshot_matches(entry, snapshot):
            continue
        if requirement and requirement not in {entry.get("requirement_id"), entry.get("requirement_uid")}:
            continue
        if requirement and strict_repo and entry.get("repo_key") != identity.get("repo_key"):
            continue
        score = candidate_score(identity, entry, root)
        if score <= 0 and requirement is None:
            continue
        candidate = dict(entry)
        enrich_requirement_candidate(candidate)
        candidate["score"] = score
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (item["score"], item.get("created_at", ""), item.get("snapshot_dir", "")),
        reverse=True,
    )
    return candidates[:limit]


def render_backup_candidates(root: Path) -> list[str]:
    project_candidates = project_backup_candidates(root, limit=3)
    requirement_candidates = requirement_backup_candidates(root, limit=5)
    if not project_candidates and not requirement_candidates:
        return []
    lines = ["找到本机 SDLC 备份："]
    for item in project_candidates:
        lines.append(
            f"- 项目快照：{item.get('project_name')} {item.get('created_at')}，包含 {len(item.get('requirements', []))} 个需求"
        )
    for item in requirement_candidates:
        lines.append(
            f"- 需求快照：{item.get('requirement_id')} {item.get('title')} {item.get('created_at')}"
        )
    lines.append("- 可执行 `$sdlc-restore --dry-run` 预览，确认后用 `$sdlc-restore --confirm` 恢复。")
    return lines


def tar_members(archive_path: Path) -> set[str]:
    if not archive_path.exists():
        return set()
    with tarfile.open(archive_path, "r:gz") as archive:
        return {member.name for member in archive.getmembers()}


def archive_has_path(members: set[str], path: str) -> bool:
    clean = path.rstrip("/")
    if path.endswith("/"):
        return clean in members or path in members or any(member.startswith(path) for member in members)
    return path in members


def requirement_archive_missing_paths(archive_path: Path, folder_name: str) -> list[str]:
    members = tar_members(archive_path)
    prefix = f".codex-sdlc/requirements/{folder_name}/"
    missing: list[str] = []
    def add_missing(relative_path: str) -> None:
        if relative_path not in missing:
            missing.append(relative_path)

    for relative_path in REQUIRED_REQUIREMENT_ARCHIVE_PATHS:
        if not archive_has_path(members, prefix + relative_path):
            add_missing(relative_path)
    for relative_path in REQUIRED_REQUIREMENT_ARCHIVE_DIRS:
        if not archive_has_path(members, prefix + relative_path):
            add_missing(relative_path)
    return missing


def require_matching_archive_contract(
    expected: dict[str, object],
    actual: dict[str, object],
) -> None:
    """外部 manifest 与归档内真实字节必须互相证明，不能只相信其中一边。"""

    protected_fields = (
        "workflow_profile",
        "formal_kind",
        "requirement_id",
        "source_draft_id",
        "source_revision_sha256",
        "formal_manifest_sha256",
        "artifact_index_sha256",
        "reference_index_sha256",
        "original_files",
        "protected_files",
    )
    for field in protected_fields:
        if field in expected and expected.get(field) != actual.get(field):
            raise SdlcError(f"备份 manifest 的 {field} 与归档内正式档案不一致。", exit_code=1)


def installed_requirement_contracts(paths: ProjectPaths) -> dict[str, dict[str, object]]:
    contracts: dict[str, dict[str, object]] = {}
    if not paths.requirements_dir.exists():
        return contracts
    if paths.requirements_dir.is_symlink() or not paths.requirements_dir.is_dir():
        raise SdlcError("恢复后的正式需求根目录不是受控目录。", exit_code=1)
    for requirement_dir in sorted(paths.requirements_dir.iterdir()):
        if not requirement_dir.is_dir() or requirement_dir.is_symlink():
            raise SdlcError(f"恢复后的需求目录不受支持：{requirement_dir.name}。", exit_code=1)
        contracts[requirement_dir.name] = requirement_archive_contract(requirement_dir)
    return contracts


def reject_document_first_conflicts(
    current: dict[str, dict[str, object]],
    incoming: dict[str, dict[str, object]],
) -> None:
    """replace 只替换可重建状态；已存在的正式 original 不允许静默换成另一份。"""

    for folder_name in sorted(set(current) | set(incoming)):
        current_contract = current.get(folder_name)
        incoming_contract = incoming.get(folder_name)
        if current_contract is None:
            continue
        document_first = any(
            contract is not None and contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
            for contract in (current_contract, incoming_contract)
        )
        if not document_first:
            continue
        if (
            current_contract is None
            or incoming_contract is None
            or current_contract.get("original_files") != incoming_contract.get("original_files")
            or current_contract.get("reference_index_sha256") != incoming_contract.get("reference_index_sha256")
        ):
            raise SdlcError(
                f"{folder_name} 已存在的正式原文或引用与待恢复快照冲突，不能用 `--replace` 覆盖。",
                exit_code=1,
            )


def backup_integrity_checks(paths: ProjectPaths, state: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    passed: list[str] = []
    warnings: list[str] = []
    failed: list[str] = []

    if paths.events_file.exists():
        if not paths.identity_file.exists():
            warnings.append("当前项目还没有 `.codex-sdlc/identity.json`，下次写状态前会自动补齐。")
        else:
            mismatches = identity_mismatch_items(paths)
            if mismatches:
                names = "、".join(item["name"] for item in mismatches)
                failed.append(f"当前 `.codex-sdlc/` 和 Git 状态不匹配：{names}。请先用 `$sdlc-restore --dry-run` 确认要恢复的状态。")
            else:
                passed.append("当前 `.codex-sdlc/` 和 Git 分支、工作树身份匹配")

    project_candidates = project_backup_candidates(paths.root, limit=1)
    if project_candidates:
        candidate = project_candidates[0]
        members = tar_members(Path(str(candidate.get("archive", ""))))
        if ".codex-sdlc" in members or any(member.startswith(".codex-sdlc/") for member in members):
            passed.append("最近项目级备份包含 `.codex-sdlc/`")
        else:
            failed.append("最近项目级备份缺少 `.codex-sdlc/`，不能作为完整项目恢复来源。")
    else:
        warnings.append("当前项目没有匹配的项目级备份，工作树误删后只能依赖需求级快照或人工副本。")

    for requirement in state.get("active_requirements", []):
        requirement_id = str(requirement.get("requirement_id", ""))
        candidates = requirement_backup_candidates(paths.root, requirement_id, limit=1)
        if not candidates:
            warnings.append(f"{requirement_id} 没有匹配的需求级备份，建议执行 `$sdlc-backup {requirement_id}`。")
            continue
        candidate = candidates[0]
        snapshot_dir = Path(str(candidate.get("snapshot_dir", "")))
        archive_path = Path(str(candidate.get("archive", "")))
        manifest = read_json(snapshot_dir / "manifest.json", {})
        folder_name = str(manifest.get("folder_name") or candidate.get("folder_name") or requirement.get("folder_name") or "")
        if not (snapshot_dir / "events.slice.jsonl").exists():
            failed.append(f"{requirement_id} 的最近需求备份缺少 `events.slice.jsonl`，不能可靠恢复流水。")
        else:
            passed.append(f"{requirement_id} 的最近需求备份包含事件切片")
        if not archive_path.exists():
            failed.append(f"{requirement_id} 的最近需求备份缺少需求包归档。")
            continue
        missing = requirement_archive_missing_paths(archive_path, folder_name)
        if missing:
            warnings.append(f"{requirement_id} 的最近需求备份缺少这些需求包资料：" + "、".join(missing))
        else:
            passed.append(f"{requirement_id} 的最近需求备份包含 current、版本、执行包和验证资料")
        expected_contract = manifest.get("archive_contract")
        if isinstance(expected_contract, dict) and expected_contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE:
            try:
                with tempfile.TemporaryDirectory(prefix="codex-sdlc-backup-check-") as temp_dir:
                    temp_root = Path(temp_dir)
                    safe_extract_requirement_tar(
                        archive_path,
                        temp_root,
                        source_folder_name=folder_name,
                    )
                    actual_contract = requirement_archive_contract(
                        temp_root / ".codex-sdlc/requirements" / folder_name
                    )
                    require_matching_archive_contract(expected_contract, actual_contract)
            except SdlcError as exc:
                failed.append(f"{requirement_id} 的文档优先备份不可恢复：{exc.message}")
            else:
                passed.append(f"{requirement_id} 的正式清单、原文和引用哈希已在备份中复核")

    return passed, warnings, failed


def validate_tar_members(archive: tarfile.TarFile) -> None:
    for member in archive.getmembers():
        path = Path(member.name)
        if (
            not member.name
            or "\x00" in member.name
            or path.is_absolute()
            or ".." in path.parts
            or member.issym()
            or member.islnk()
            or not (member.isfile() or member.isdir())
        ):
            raise SdlcError("备份包包含不安全或不支持的文件项，已停止恢复。", exit_code=1)


def safe_extract_tar(archive_path: Path, target_dir: Path) -> None:
    target = target_dir.resolve()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            validate_tar_members(archive)
            for member in archive.getmembers():
                member_path = (target / member.name).resolve()
                if target not in [member_path, *member_path.parents]:
                    raise SdlcError("备份包路径不安全，已停止恢复。")
            for member in archive.getmembers():
                extract_tar_member(archive, member, target)
    except SdlcError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise SdlcError("备份包无法读取或已经损坏，已停止恢复。", exit_code=1) from exc


def safe_extract_requirement_tar(
    archive_path: Path,
    target_dir: Path,
    *,
    source_folder_name: str,
    target_folder_name: str | None = None,
) -> str:
    target = target_dir.resolve()
    restored_folder_name = target_folder_name or source_folder_name
    source_prefix = f".codex-sdlc/requirements/{source_folder_name}"
    target_prefix = f".codex-sdlc/requirements/{restored_folder_name}"
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            validate_tar_members(archive)
            for member in archive.getmembers():
                member_name = member.name
                if member_name == source_prefix or member_name.startswith(source_prefix + "/"):
                    member_name = target_prefix + member_name[len(source_prefix) :]
                member_path = (target / member_name).resolve()
                if target not in [member_path, *member_path.parents]:
                    raise SdlcError("备份包路径不安全，已停止恢复。")

                new_member = copy.copy(member)
                new_member.name = member_name
                extract_tar_member(archive, new_member, target)
    except SdlcError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise SdlcError("需求备份包无法读取或已经损坏，已停止恢复。", exit_code=1) from exc
    return restored_folder_name


def extract_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    try:
        archive.extract(member, target, filter="data")
    except TypeError:
        archive.extract(member, target)


def repair_restored_contracts(paths: ProjectPaths) -> dict[str, list[str]]:
    if not paths.events_file.exists():
        return {"repaired": [], "warnings": []}
    state = derive_state(paths)
    report = repair_task_contracts(paths, state)
    document_first_ids = [
        str(requirement.get("requirement_id") or "")
        for requirement in state.get("requirements", {}).values()
        if isinstance(requirement.get("native_start"), dict)
        and requirement["native_start"].get("workflow_profile") == DOCUMENT_FIRST_PROFILE
    ]
    if document_first_ids:
        # 新流程的 REQ 目录已经由正式档案完整恢复；这里只重建全局投影，不能调用旧渲染器覆盖 original。
        refresh_start_transaction_state(
            paths,
            committed_requirement_id=document_first_ids[0],
        )
    else:
        refresh_materialized_state(paths)
    return report


def restore_project(root: Path, *, confirm: bool, replace: bool = False, snapshot: str | None = None) -> dict[str, Any]:
    candidates = project_backup_candidates(root, limit=1, snapshot=snapshot)
    if not candidates:
        raise SdlcError("没有找到可匹配的项目快照。")
    candidate = candidates[0]
    if not confirm:
        return {"mode": "preview", "candidate": candidate}
    managed_paths = [root / ".codex-sdlc"]
    existing_paths = [path for path in managed_paths if path.exists()]
    if existing_paths and not replace:
        names = "、".join(f"`{path.name}/`" for path in existing_paths)
        raise SdlcError(f"当前目录已经存在 {names}，请先使用 `$sdlc-restore --dry-run` 检查；确认覆盖时加 `--replace`。")
    current_contracts = (
        installed_requirement_contracts(ProjectPaths(root=root))
        if (root / ".codex-sdlc").exists()
        else {}
    )
    with tempfile.TemporaryDirectory(prefix="codex-sdlc-project-restore-") as temp_dir:
        temp_root = Path(temp_dir)
        safe_extract_tar(Path(candidate["archive"]), temp_root)
        restored_sdlc = temp_root / ".codex-sdlc"
        if not restored_sdlc.is_dir() or restored_sdlc.is_symlink():
            raise SdlcError("项目快照缺少完整 `.codex-sdlc/`，已停止恢复。", exit_code=1)
        incoming_paths = ProjectPaths(root=temp_root)
        if not incoming_paths.events_file.is_file() or incoming_paths.events_file.is_symlink():
            raise SdlcError("项目快照缺少有效的 `events.jsonl`，已停止恢复。", exit_code=1)
        incoming_contracts = installed_requirement_contracts(incoming_paths)
        for item in candidate.get("requirements", []):
            if not isinstance(item, dict):
                continue
            folder_name = str(item.get("folder_name") or "")
            expected = item.get("archive_contract")
            if folder_name and folder_name not in incoming_contracts:
                raise SdlcError(
                    f"项目快照缺少 manifest 登记的需求目录：.codex-sdlc/requirements/{folder_name}。",
                    exit_code=1,
                )
            if folder_name and isinstance(expected, dict):
                require_matching_archive_contract(expected, incoming_contracts[folder_name])
        reject_document_first_conflicts(current_contracts, incoming_contracts)

        moved_paths: list[str] = []
        moved_originals: list[tuple[Path, Path]] = []
        try:
            if replace:
                timestamp = safe_timestamp()
                for path in existing_paths:
                    backup_dir = root / f"{path.name}.pre-restore-{timestamp}"
                    shutil.move(str(path), str(backup_dir))
                    moved_paths.append(str(backup_dir))
                    moved_originals.append((path, backup_dir))
            shutil.move(str(restored_sdlc), str(root / ".codex-sdlc"))
            paths = ProjectPaths(root=root)
            write_sdlc_identity(paths)
            repair_report = repair_restored_contracts(paths)
        except Exception:
            failed_target = root / ".codex-sdlc"
            if failed_target.exists():
                shutil.rmtree(failed_target, ignore_errors=True)
            for original, backup_dir in reversed(moved_originals):
                if backup_dir.exists():
                    shutil.move(str(backup_dir), str(original))
            raise
    return {
        "mode": "restored",
        "candidate": candidate,
        "contract_repaired": repair_report["repaired"],
        "moved_paths": moved_paths,
    }


def load_events_slice(snapshot_dir: Path) -> list[dict[str, Any]]:
    events_file = snapshot_dir / "events.slice.jsonl"
    if not events_file.exists():
        raise SdlcError("需求快照缺少 `events.slice.jsonl`，不能恢复。")
    events: list[dict[str, Any]] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def requirement_folder_from_archive(archive_path: Path) -> str:
    prefix = ".codex-sdlc/requirements/"
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            validate_tar_members(archive)
            for member in archive.getmembers():
                if not member.name.startswith(prefix):
                    continue
                remainder = member.name[len(prefix) :].strip("/")
                if remainder:
                    return remainder.split("/", 1)[0]
    except SdlcError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise SdlcError("需求备份包无法读取或已经损坏，已停止恢复。", exit_code=1) from exc
    raise SdlcError("需求快照里没有找到需求包目录，不能恢复。")


def new_requirement_folder_name(original_folder_name: str, old_requirement_id: str, new_requirement_id: str) -> str:
    if original_folder_name.startswith(old_requirement_id + "-"):
        return new_requirement_id + original_folder_name[len(old_requirement_id) :]
    return f"{new_requirement_id}-imported-{short_hash(original_folder_name, 8)}"


def rewrite_requirement_value(
    value: Any,
    *,
    old_requirement_id: str,
    new_requirement_id: str,
    old_folder_name: str,
    new_folder_name: str,
) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, child in value.items():
            if key == "requirement_id" and child == old_requirement_id:
                rewritten[key] = new_requirement_id
            elif key == "folder_name" and child == old_folder_name:
                rewritten[key] = new_folder_name
            else:
                rewritten[key] = rewrite_requirement_value(
                    child,
                    old_requirement_id=old_requirement_id,
                    new_requirement_id=new_requirement_id,
                    old_folder_name=old_folder_name,
                    new_folder_name=new_folder_name,
                )
        return rewritten
    if isinstance(value, list):
        return [
            rewrite_requirement_value(
                item,
                old_requirement_id=old_requirement_id,
                new_requirement_id=new_requirement_id,
                old_folder_name=old_folder_name,
                new_folder_name=new_folder_name,
            )
            for item in value
        ]
    if isinstance(value, str):
        return value.replace(old_folder_name, new_folder_name).replace(old_requirement_id, new_requirement_id)
    return value


def rewrite_requirement_event(
    event: dict[str, Any],
    *,
    old_requirement_id: str,
    new_requirement_id: str,
    old_folder_name: str,
    new_folder_name: str,
) -> dict[str, Any]:
    rewritten = rewrite_requirement_value(
        event,
        old_requirement_id=old_requirement_id,
        new_requirement_id=new_requirement_id,
        old_folder_name=old_folder_name,
        new_folder_name=new_folder_name,
    )
    if isinstance(rewritten, dict) and rewritten.get("event_type") == "requirement_created":
        payload = rewritten.setdefault("payload", {})
        if isinstance(payload, dict):
            payload["folder_name"] = new_folder_name
            payload["imported_from_requirement_id"] = old_requirement_id
    return rewritten


def synthetic_project_initialized_event(root: Path) -> dict[str, Any]:
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "event_id": "EVT-RESTORE-000001",
        "event_type": "project_initialized",
        "project_path": str(root),
        "requirement_id": None,
        "task_id": None,
        "created_at": created_at,
        "source": "sdlc-restore",
        "summary": "恢复项目基础状态",
        "payload": {
            "project_name": root.name,
            "project_path": str(root),
        },
    }


def rebind_restored_document_first_receipt(
    paths: ProjectPaths,
    requirement_dir: Path,
    restored_events: list[dict[str, Any]],
) -> None:
    contract = requirement_archive_contract(requirement_dir)
    if contract.get("workflow_profile") != DOCUMENT_FIRST_PROFILE:
        return
    status = _read_archive_json(requirement_dir / "status.json", label="status.json")
    transaction_id = str(status.get("transaction_id") or "")
    receipt_path = paths.sdlc_dir / "start-transactions/completed" / f"{transaction_id}.json"
    receipt = _read_archive_json(receipt_path, label="建档完成回执")
    formal_events = [
        event
        for event in restored_events
        if event.get("requirement_id") == contract.get("requirement_id")
        and event.get("event_type") in {"requirement_created", "draft_started"}
    ]
    if len(formal_events) != 2:
        raise SdlcError("需求快照没有两条可核对的文档优先建档事件。", exit_code=1)
    event_bytes = b"".join(
        (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        for event in formal_events
    )
    current = paths.events_file.read_bytes()
    start = current.find(event_bytes)
    if start < 0 or current.find(event_bytes, start + 1) >= 0:
        raise SdlcError("恢复后的建档事件无法和完成回执唯一对应。", exit_code=1)
    receipt["events"] = formal_events
    receipt["event_start_size"] = start
    receipt["event_end_size"] = start + len(event_bytes)
    write_json(receipt_path, receipt)


def restore_requirement(
    root: Path,
    requirement: str,
    *,
    confirm: bool,
    replace: bool = False,
    snapshot: str | None = None,
    new_requirement_id: str | None = None,
) -> dict[str, Any]:
    candidates = requirement_backup_candidates(root, requirement, limit=1, snapshot=snapshot)
    if not candidates:
        raise SdlcError(f"没有找到需求备份：{requirement}")
    candidate = candidates[0]
    if not confirm:
        result: dict[str, Any] = {"mode": "preview", "candidate": candidate}
        if new_requirement_id:
            result["new_requirement_id"] = new_requirement_id
        return result
    paths = ProjectPaths(root=root)
    has_events = paths.events_file.exists()
    state = derive_state(paths) if has_events else {"events": [], "requirements": {}}
    source_requirement_id = str(candidate["requirement_id"])
    snapshot_dir = Path(candidate["snapshot_dir"])
    archive_path = Path(candidate["archive"])
    source_folder_name = requirement_folder_from_archive(archive_path)
    target_requirement_id = new_requirement_id or source_requirement_id
    existing = next(
        (
            item
            for item in state["requirements"].values()
            if item["requirement_id"] == target_requirement_id
            or (not new_requirement_id and stable_requirement_uid(item) == candidate["requirement_uid"])
        ),
        None,
    )
    if existing is not None and not replace:
        raise SdlcError("当前项目已有同编号或同 UID 的需求；确认覆盖时加 `--replace`，或用 `$sdlc-context import ... --as REQ-xxx` 导入成新编号。")

    folder_name = source_folder_name
    restored_folder_name = new_requirement_folder_name(folder_name, source_requirement_id, new_requirement_id) if new_requirement_id else folder_name
    restored_events = sanitize_backup_events(load_events_slice(snapshot_dir))
    current_events = list(state["events"])
    existing_folder_name = str(existing.get("folder_name") or "") if existing is not None else restored_folder_name
    existing_dir = paths.requirements_dir / existing_folder_name
    target_dir = paths.requirements_dir / restored_folder_name
    if target_dir.exists() and existing is None and not replace:
        raise SdlcError("当前项目已有同名需求包目录；确认覆盖时加 `--replace`。")

    with tempfile.TemporaryDirectory(prefix="codex-sdlc-requirement-restore-") as temp_dir:
        temp_root = Path(temp_dir)
        safe_extract_requirement_tar(
            archive_path,
            temp_root,
            source_folder_name=source_folder_name,
        )
        incoming_dir = temp_root / ".codex-sdlc/requirements" / source_folder_name
        if not incoming_dir.is_dir() or incoming_dir.is_symlink():
            raise SdlcError("需求快照缺少完整正式需求目录，已停止恢复。", exit_code=1)
        incoming_contract = requirement_archive_contract(incoming_dir)
        expected_contract = read_json(snapshot_dir / "manifest.json", {}).get("archive_contract")
        if isinstance(expected_contract, dict):
            require_matching_archive_contract(expected_contract, incoming_contract)
        if (
            incoming_contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
            and new_requirement_id
        ):
            raise SdlcError(
                "文档优先正式档案不能改成新 REQ 编号恢复；请恢复原编号或使用项目快照。",
                exit_code=1,
            )
        current_contract = requirement_archive_contract(existing_dir) if existing_dir.is_dir() else None
        if current_contract is not None:
            reject_document_first_conflicts(
                {source_folder_name: current_contract},
                {source_folder_name: incoming_contract},
            )

        keep_document_first_events = (
            current_contract is not None
            and current_contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
            and incoming_contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
        )
        if new_requirement_id:
            restored_events = [
                rewrite_requirement_event(
                    event,
                    old_requirement_id=source_requirement_id,
                    new_requirement_id=new_requirement_id,
                    old_folder_name=folder_name,
                    new_folder_name=restored_folder_name,
                )
                for event in restored_events
            ]
            candidate = {**candidate, "requirement_id": new_requirement_id, "folder_name": restored_folder_name}
        if not keep_document_first_events:
            if existing is not None:
                current_events = [
                    event
                    for event in current_events
                    if event.get("requirement_id") != existing["requirement_id"]
                ]
            if not current_events and not any(event.get("event_type") == "project_initialized" for event in restored_events):
                restored_events.insert(0, synthetic_project_initialized_event(root))
            if any(event.get("event_type") == "project_initialized" for event in current_events):
                restored_events = [event for event in restored_events if event.get("event_type") != "project_initialized"]
            existing_event_ids = {event["event_id"] for event in current_events}
            next_number = len(current_events)
            for event in restored_events:
                event["project_path"] = str(root)
                if event.get("event_type") == "project_initialized" and isinstance(event.get("payload"), dict):
                    event["payload"]["project_path"] = str(root)
                    event["payload"]["project_name"] = root.name
                if event["event_id"] in existing_event_ids:
                    next_number += 1
                    event["event_id"] = f"EVT-RESTORE-{next_number:06d}"
                existing_event_ids.add(event["event_id"])

        old_events = paths.events_file.read_bytes() if paths.events_file.exists() else None
        moved_dir: Path | None = None
        receipt_backups: dict[Path, bytes | None] = {}
        try:
            paths.sdlc_dir.mkdir(parents=True, exist_ok=True)
            if not keep_document_first_events:
                event_stream = b"".join(
                    (json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                    for event in [*current_events, *restored_events]
                )
                paths.events_file.write_bytes(event_stream)
            if existing_dir.exists():
                moved_dir = existing_dir.with_name(f"{existing_dir.name}.pre-restore-{safe_timestamp()}")
                shutil.move(str(existing_dir), str(moved_dir))
            if target_dir.exists() and target_dir != existing_dir:
                if not replace:
                    raise SdlcError("当前项目已有同名需求包目录，已停止恢复。", exit_code=1)
                collision_dir = target_dir.with_name(f"{target_dir.name}.pre-restore-{safe_timestamp()}")
                shutil.move(str(target_dir), str(collision_dir))
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(incoming_dir), str(target_dir))

            incoming_receipts = temp_root / ".codex-sdlc/start-transactions/completed"
            if incoming_receipts.is_dir():
                target_receipts = paths.sdlc_dir / "start-transactions/completed"
                target_receipts.mkdir(parents=True, exist_ok=True)
                for receipt in sorted(incoming_receipts.glob("*.json")):
                    target_receipt = target_receipts / receipt.name
                    receipt_backups[target_receipt] = (
                        target_receipt.read_bytes() if target_receipt.exists() else None
                    )
                    shutil.copy2(receipt, target_receipt)
            incoming_lessons = temp_root / ".codex-sdlc/lessons"
            if incoming_lessons.is_dir():
                shutil.copytree(incoming_lessons, paths.sdlc_dir / "lessons", dirs_exist_ok=True)
            if new_requirement_id:
                write_json(
                    target_dir / "restore-alias.json",
                    {
                        "source_requirement_id": source_requirement_id,
                        "imported_as": new_requirement_id,
                        "source_folder_name": source_folder_name,
                        "folder_name": restored_folder_name,
                        "snapshot": snapshot_dir.name,
                        "restored_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                )
            if (
                incoming_contract.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
                and not keep_document_first_events
            ):
                rebind_restored_document_first_receipt(paths, target_dir, restored_events)
            write_sdlc_identity(paths)
            repair_report = repair_restored_contracts(paths)
        except Exception:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            if moved_dir is not None and moved_dir.exists():
                shutil.move(str(moved_dir), str(existing_dir))
            if old_events is None:
                paths.events_file.unlink(missing_ok=True)
            else:
                paths.events_file.write_bytes(old_events)
            for receipt, previous in receipt_backups.items():
                if previous is None:
                    receipt.unlink(missing_ok=True)
                else:
                    receipt.write_bytes(previous)
            raise
    folder_name = restored_folder_name
    return {
        "mode": "restored",
        "candidate": candidate,
        "folder_name": folder_name,
        "new_requirement_id": new_requirement_id or "",
        "contract_repaired": repair_report["repaired"],
    }


def rebuild_index_from_disk(root: Path | None = None) -> dict[str, Any]:
    base = root or ensure_backup_home()
    with backup_write_lock(base):
        return _rebuild_index_from_disk_locked(base)


def manifest_backup_metadata(manifest: dict[str, Any]) -> dict[str, str]:
    raw_metadata = manifest.get("backup", {})
    if not isinstance(raw_metadata, dict):
        return build_backup_metadata(automatic=False)
    return {
        "mode": str(raw_metadata.get("mode", "manual")),
        "command": str(raw_metadata.get("command", "")),
        "phase": str(raw_metadata.get("phase", "")),
    }


def _rebuild_index_from_disk_locked(base: Path) -> dict[str, Any]:
    index_data = {"backup_version": BACKUP_VERSION, "project_snapshots": [], "requirement_snapshots": []}
    for manifest_file in base.glob("repos/*/project-snapshots/*/*/*.manifest.json"):
        manifest = read_json(manifest_file, {})
        archive = manifest_file.with_suffix("").with_suffix(".tar.gz")
        if archive.exists():
            upsert_index_entry(
                index_data["project_snapshots"],
                {
                    "created_at": manifest.get("created_at", ""),
                    "repo_key": manifest.get("repo_key", ""),
                    "branch_key": manifest.get("branch_key", ""),
                    "worktree_key": manifest.get("worktree_key", ""),
                    "project_path": manifest.get("project_path", ""),
                    "project_name": manifest.get("project_name", ""),
                    "archive": str(archive),
                    "requirements": sanitize_backup_metadata(manifest.get("sdlc", {}).get("requirements", [])),
                    "pinned": manifest.get("pinned", False),
                    "branch": manifest.get("git", {}).get("branch", ""),
                    "branch_ref": manifest.get("git", {}).get("branch_ref", ""),
                    "head": manifest.get("git", {}).get("head", ""),
                    **backup_metadata_index_fields(manifest_backup_metadata(manifest)),
                },
            )
    for manifest_file in base.glob("repos/*/requirements/*/snapshots/*/manifest.json"):
        manifest = read_json(manifest_file, {})
        archive = manifest_file.parent / "requirement-package.tar.gz"
        if archive.exists():
            upsert_index_entry(
                index_data["requirement_snapshots"],
                {
                    "created_at": manifest.get("created_at", ""),
                    "repo_key": manifest.get("repo_key", ""),
                    "branch_key": manifest.get("branch_key", ""),
                    "worktree_key": manifest.get("worktree_key", ""),
                    "project_path": manifest.get("project_path", ""),
                    "project_name": manifest.get("project_name", ""),
                    "requirement_uid": manifest.get("requirement_uid", ""),
                    "requirement_id": manifest.get("requirement_id", ""),
                    "title": clean_requirement_display_text(manifest.get("title", "")),
                    "summary": clean_requirement_display_text(manifest.get("summary", manifest.get("title", ""))),
                    "description": clean_requirement_display_text(manifest.get("description", "")),
                    "description_excerpt": requirement_display_description(manifest),
                    "status": manifest.get("status", ""),
                    "archive": str(archive),
                    "snapshot_dir": str(manifest_file.parent),
                    "pinned": manifest.get("pinned", False),
                    "branch": manifest.get("git", {}).get("branch", ""),
                    "branch_ref": manifest.get("git", {}).get("branch_ref", ""),
                    "head": manifest.get("git", {}).get("head", ""),
                    **backup_metadata_index_fields(manifest_backup_metadata(manifest)),
                },
            )
    save_index(index_data, base)
    return index_data


def snapshot_date_from_name(name: str) -> date | None:
    try:
        return datetime.strptime(name[:8], "%Y%m%d").date()
    except ValueError:
        return None


def retention_keep_set(paths: list[Path], *, keep_recent: int, keep_days: int) -> set[Path]:
    kept: set[Path] = set(paths[:keep_recent])
    kept_dates = {snapshot_date for path in kept if (snapshot_date := snapshot_date_from_name(path.name)) is not None}
    today = datetime.now().astimezone().date()
    for path in paths[keep_recent:]:
        snapshot_date = snapshot_date_from_name(path.name)
        if snapshot_date is None or snapshot_date in kept_dates:
            continue
        if (today - snapshot_date).days > keep_days:
            continue
        kept.add(path)
        kept_dates.add(snapshot_date)
    return kept


def project_archive_is_protected(archive: Path) -> bool:
    manifest = read_json(archive.with_name(archive.name.replace(".tar.gz", ".manifest.json")), {})
    # 只有明确固定的快照才能跳过清理，完成过需求不能让以后所有自动快照永久保留。
    return bool(manifest.get("pinned"))


def requirement_snapshot_is_protected(snapshot: Path) -> bool:
    manifest = read_json(snapshot / "manifest.json", {})
    return bool(manifest.get("pinned"))


def auto_snapshot_is_expired(path: Path, *, manifest_path: Path, keep_auto_days: int) -> bool:
    manifest = read_json(manifest_path, {})
    if manifest_backup_metadata(manifest).get("mode") != "auto":
        return False
    snapshot_date = snapshot_date_from_name(path.name)
    if snapshot_date is None:
        return False
    return (datetime.now().astimezone().date() - snapshot_date).days > keep_auto_days


def clean_backups(
    *, keep_project: int = 20, keep_requirement: int = 50, keep_days: int = 90, keep_auto_days: int = 7
) -> dict[str, int]:
    root = ensure_backup_home()
    with backup_write_lock(root):
        return _clean_backups_locked(
            root, keep_project=keep_project, keep_requirement=keep_requirement, keep_days=keep_days, keep_auto_days=keep_auto_days
        )


def _clean_backups_locked(
    root: Path, *, keep_project: int, keep_requirement: int, keep_days: int, keep_auto_days: int
) -> dict[str, int]:
    removed_project = 0
    removed_requirement = 0
    for worktree_dir in root.glob("repos/*/project-snapshots/*/*"):
        timestamp_archives = sorted(
            [path for path in worktree_dir.glob("*.tar.gz") if path.name != "latest.tar.gz"],
            key=lambda path: path.name,
            reverse=True,
        )
        kept_archives = retention_keep_set(timestamp_archives, keep_recent=keep_project, keep_days=keep_days)
        kept_archives.update(path for path in timestamp_archives if project_archive_is_protected(path))
        kept_archives.difference_update(
            path
            for path in timestamp_archives
            if not project_archive_is_protected(path)
            and auto_snapshot_is_expired(
                path,
                manifest_path=path.with_name(path.name.replace(".tar.gz", ".manifest.json")),
                keep_auto_days=keep_auto_days,
            )
        )
        for archive in timestamp_archives:
            if archive in kept_archives:
                continue
            manifest = worktree_dir / archive.name.replace(".tar.gz", ".manifest.json")
            archive.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
            removed_project += 1
        remaining_archives = [archive for archive in timestamp_archives if archive.exists()]
        latest_path = worktree_dir / "latest.tar.gz"
        if remaining_archives:
            update_latest_project_archive(max(remaining_archives, key=lambda path: path.name), latest_path)
        else:
            latest_path.unlink(missing_ok=True)
    for req_dir in root.glob("repos/*/requirements/*/snapshots"):
        snapshot_dirs = sorted([path for path in req_dir.iterdir() if path.is_dir()], key=lambda path: path.name, reverse=True)
        kept_snapshots = retention_keep_set(snapshot_dirs, keep_recent=keep_requirement, keep_days=keep_days)
        kept_snapshots.update(path for path in snapshot_dirs if requirement_snapshot_is_protected(path))
        kept_snapshots.difference_update(
            path
            for path in snapshot_dirs
            if not requirement_snapshot_is_protected(path)
            and auto_snapshot_is_expired(path, manifest_path=path / "manifest.json", keep_auto_days=keep_auto_days)
        )
        for snapshot in snapshot_dirs:
            if snapshot in kept_snapshots:
                continue
            shutil.rmtree(snapshot, ignore_errors=True)
            removed_requirement += 1
    _rebuild_index_from_disk_locked(root)
    git_result = backup_git_commit(root, "清理旧 SDLC 备份")
    return {"project": removed_project, "requirement": removed_requirement, "git": git_result}
