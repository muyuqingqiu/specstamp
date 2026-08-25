from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid
from typing import Any

from codex_sdlc.core import draft_artifacts, draft_lifecycle
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.external_version import (
    load_external_version_evidence,
    normalize_external_url,
    normalized_url_sha256,
    unversioned_evidence,
)
from codex_sdlc.core.change_workspace import change_material_environment_interruption_hook
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.state import append_event, derive_state, load_events, next_number, refresh_materialized_state
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_file,
    validate_schema_document,
)
from codex_sdlc.services.change_service import add_change_material


MATERIAL_MANIFEST_SCHEMA = "material-manifest.v1"
SECRET_REFERENCE_SCHEMA = "secret-reference.v1"
MATERIAL_TYPES = (
    "requirement",
    "technical-solution",
    "ui-design",
    "api-document",
    "database-document",
    "sample-data",
    "account",
    "environment",
    "field-evidence",
    "other",
)
SENSITIVITY_LEVELS = ("public", "internal", "secret-reference")
ACCESS_CONDITIONS = ("public", "authenticated", "restricted")
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"PuTTY-User-Key-File-",
)
SENSITIVE_TEXT_PATTERN = re.compile(
    rb"(?im)^\s*(?:password|passwd|token|api[_-]?key|secret[_-]?key|private[_-]?key|authorization)\s*[:=]\s*[^\s<>{}\[\]]{8,}"
)
COMMON_SECRET_PATTERN = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.)")
MATERIAL_TRANSACTION_PREFIX = "material-transaction-"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("material", help="向未建档 DRAFT 归档原始资料或受控引用")
    parser.add_argument("draft", help="DRAFT 编号，例如 DRAFT-001")
    parser.add_argument("content", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--title", required=True, help="资料标题")
    parser.add_argument("--type", choices=MATERIAL_TYPES, required=True, help="模型已确认的资料类型")
    source_group = parser.add_mutually_exclusive_group(required=False)
    source_group.add_argument("--file", default="", help="项目内原始文件路径")
    source_group.add_argument("--url", default="", help="外部资料 URL")
    source_group.add_argument("--secret-reference", default="", help="secret-reference.v1 JSON 文件")
    parser.add_argument(
        "--version-evidence",
        "--external-version-evidence",
        dest="version_evidence",
        default="",
        help="external-version-evidence.v1 JSON 文件",
    )
    parser.add_argument("--access-condition", choices=ACCESS_CONDITIONS, default="public", help="外部资料访问条件")
    parser.add_argument("--sensitivity", choices=SENSITIVITY_LEVELS, default="internal", help="资料敏感级别")
    parser.add_argument("--role", action="append", choices=MATERIAL_TYPES, default=[], help="资料承担的角色，可重复传入")
    parser.add_argument("--scope", action="append", default=[], help="适用范围，可重复传入")
    parser.add_argument("--supersedes", default="", help="被这份修订替代的 MAT 编号")
    # 旧参数继续由解析器接住，run 会按新边界给出明确错误，不让用户只看到 argparse 用法。
    parser.add_argument("--task", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--command", dest="executed_commands", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--source", default="", help=argparse.SUPPRESS)
    parser.add_argument("--status", default="", help=argparse.SUPPRESS)
    parser.set_defaults(func=run)

    change_parser = subparsers.add_parser(
        "change-material",
        help="向结构化变更工作区归档普通文件、外部版本或秘密引用",
    )
    change_parser.add_argument("requirement_id", help="需求编号，例如 REQ-001")
    change_parser.add_argument("change_id", help="变更编号，例如 CHG-001")
    change_parser.add_argument(
        "--type",
        choices=MATERIAL_TYPES,
        required=True,
        help="资料类型",
    )
    change_source = change_parser.add_mutually_exclusive_group(required=True)
    change_source.add_argument("--file", default="", help="项目内普通文件路径")
    change_source.add_argument("--url", default="", help="外部资料 URL")
    change_source.add_argument(
        "--secret-reference",
        default="",
        help="secret-reference.v1 JSON 文件",
    )
    change_parser.add_argument(
        "--version-evidence",
        default="",
        help="与当前 URL 绑定的 external-version-evidence.v1 JSON 文件",
    )
    change_parser.set_defaults(func=run_change_material)


def _clean_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise SdlcError("资料标题不能为空。", exit_code=1)
    if len(title) > 200:
        raise SdlcError("资料标题不能超过 200 个字符。", exit_code=1)
    return title


def _source_count(args: argparse.Namespace) -> int:
    return sum(bool(str(value or "").strip()) for value in (args.file, args.url, args.secret_reference))


def _validate_new_boundary(args: argparse.Namespace, target: str) -> None:
    if target.startswith("REQ-"):
        raise SdlcError(
            "正式 REQ 不能直接接收 material。会改变正式来源的资料请进入 change-material，任务测试和现场证据请进入 task-evidence。",
            exit_code=1,
        )
    if re.fullmatch(r"T-[0-9]+", target):
        # 任务证据属于当前 task-run；这里仅负责分流，不能复制到 DRAFT 资料或其他执行产物。
        raise SdlcError(
            "任务测试、日志和现场记录请使用 task-evidence，并明确给出 REQ、任务编号、来源文件和 SHA-256。",
            exit_code=1,
        )
    if re.fullmatch(r"CHG-[0-9]+", target):
        raise SdlcError(
            "变更资料请使用 change-material，并明确给出所属 REQ 和 CHG 编号。",
            exit_code=1,
        )
    if not re.fullmatch(r"DRAFT-[0-9]+", target):
        raise SdlcError(
            "资料归属不明确。material 只接受未建档的 DRAFT；正式变更资料使用 change-material，任务证据使用 task-evidence。",
            exit_code=1,
        )
    if str(args.content or "").strip() or args.task or args.executed_commands or str(args.source or "").strip() or str(args.status or "").strip():
        raise SdlcError("material 只接收显式的 --file、--url 或 --secret-reference，不接收资料正文和任务证据。", exit_code=1)
    if _source_count(args) != 1:
        raise SdlcError("资料来源必须在 --file、--url 和 --secret-reference 中选择一个。", exit_code=1)
    if args.version_evidence and not args.url:
        raise SdlcError("外部版本证据只能和 --url 一起使用。", exit_code=1)
    if args.access_condition != "public" and not args.url:
        raise SdlcError("访问条件只能和 --url 一起使用。", exit_code=1)
    if args.secret_reference and args.sensitivity != "secret-reference":
        raise SdlcError("秘密引用的 sensitivity 必须是 secret-reference。", exit_code=1)
    if not args.secret_reference and args.sensitivity == "secret-reference":
        raise SdlcError("普通文件和外部 URL 不能使用 secret-reference 敏感级别。", exit_code=1)


def _resolve_project_file(root: Path, raw_path: str, *, label: str) -> tuple[Path, str]:
    raw = str(raw_path or "").strip()
    if not raw or "\x00" in raw:
        raise SdlcError(f"{label}路径不能为空。", exit_code=1)
    requested = Path(raw).expanduser()
    if ".." in requested.parts:
        raise SdlcError(f"{label}路径不能包含上级目录。", exit_code=1)
    root_resolved = root.resolve(strict=True)
    lexical = requested if requested.is_absolute() else root_resolved / requested
    try:
        relative = lexical.relative_to(root_resolved)
    except ValueError as exc:
        raise SdlcError(f"{label}路径越过项目目录。", exit_code=1) from exc
    if not relative.parts:
        raise SdlcError(f"{label}必须是普通文件。", exit_code=1)

    # 必须沿用户传入的词法路径检查；如果先 resolve，项目内链接也会被消解而漏过。
    current = root_resolved
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise SdlcError(f"{label}不存在。", exit_code=1) from exc
        except OSError as exc:
            raise SdlcError(f"{label}路径读取失败。", exit_code=1) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SdlcError(f"{label}不能经过符号链接。", exit_code=1)
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{label}路径越过项目目录。", exit_code=1) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SdlcError(f"{label}必须是普通文件。", exit_code=1)
    return lexical, relative.as_posix()


def _open_regular_source(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor: int | None = None
    try:
        # 从文件系统根目录逐段 openat，目录或文件在检查后被替换为链接时也不会跟随。
        directory_descriptor = os.open(path.anchor, directory_flags)
        for part in path.parts[1:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(path.name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise SdlcError("原始资料读取失败。", exit_code=1) from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        # 已经打开的文件描述符也必须在检查失败时关闭，避免连续失败把进程资源耗尽。
        os.close(descriptor)
        raise SdlcError("原始资料读取失败。", exit_code=1) from exc
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise SdlcError("原始资料必须是普通文件。", exit_code=1)
    return descriptor, metadata


def _same_source_identity(path: Path, metadata: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino)


def _inspect_source_file(path: Path) -> tuple[str, int]:
    descriptor, before = _open_regular_source(path)
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb") as handle:
            # 逐块检查整份文件并保留块边界前的少量字节，避免秘密值藏在大文件后部或跨块时漏检。
            tail = b""
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                inspection = tail + chunk
                if (
                    any(marker in inspection for marker in PRIVATE_KEY_MARKERS)
                    or SENSITIVE_TEXT_PATTERN.search(inspection)
                    or COMMON_SECRET_PATTERN.search(inspection)
                ):
                    raise SdlcError("检测到密码、令牌或私钥内容，请改用 --secret-reference 保存引用元数据。", exit_code=1)
                digest.update(chunk)
                tail = inspection[-4096:]
            after = os.fstat(handle.fileno())
    except Exception:
        # fdopen 进入后会负责关闭描述符；异常只向上交给统一中文错误处理。
        raise
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or not _same_source_identity(path, before)
    ):
        raise SdlcError("原始资料在读取期间发生变化，已停止归档。", exit_code=1)
    return digest.hexdigest(), before.st_size


def _safe_material_filename(material_id: str, title: str, source: Path) -> str:
    clean = re.sub(r"[^\w.-]+", "-", title, flags=re.UNICODE).strip("-._") or "资料"
    clean = clean[:80].rstrip("-._") or "资料"
    suffix = re.sub(r"[^A-Za-z0-9.]", "", source.suffix)[:16]
    return f"{material_id}_{clean}{suffix}"


def _material_manifest(draft_id: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    document = {
        "schema_version": MATERIAL_MANIFEST_SCHEMA,
        "draft_id": draft_id,
        "materials": [
            {key: deepcopy(value) for key, value in material.items() if not str(key).startswith("_")}
            for material in materials
        ],
    }
    validate_schema_document(document, schema_name=MATERIAL_MANIFEST_SCHEMA)
    return document


def _material_projection_needs_refresh(paths, state: dict[str, Any]) -> bool:
    for draft in state.get("drafts", {}).values():
        if not isinstance(draft, dict) or not draft.get("_material_manifest_enabled"):
            continue
        manifest = _material_manifest(str(draft["draft_id"]), list(draft.get("materials", [])))
        expected = canonical_json_text(manifest).encode("utf-8")
        for path in (
            paths.draft_dir(str(draft["draft_id"])) / "material-manifest.v1.json",
            paths.draft_requirements_dir(str(draft["draft_id"])) / "material-manifest.v1.json",
        ):
            try:
                if path.is_symlink() or path.read_bytes() != expected:
                    return True
            except OSError:
                return True
    return False


def _projected_materials(draft: dict[str, Any], material: dict[str, Any]) -> list[dict[str, Any]]:
    current = [deepcopy(item) for item in draft.get("materials", []) if isinstance(item, dict)]
    supersedes = str(material.get("supersedes") or "")
    for item in current:
        if supersedes and item.get("material_id") == supersedes:
            item["status"] = "archived"
    for index, item in enumerate(current):
        if item.get("material_id") == material["material_id"]:
            current[index] = deepcopy(material)
            break
    else:
        current.append(deepcopy(material))
    return sorted(current, key=lambda item: str(item.get("material_id") or ""))


def _material_input_hashes(materials: list[dict[str, Any]]) -> dict[str, str]:
    hashes = {
        str(item["stored_path"]): str(item["sha256"])
        for item in materials
        if item.get("source_kind") == "file" and item.get("stored_path") and item.get("sha256")
    }
    # URL 和秘密引用没有本地文件哈希；统一记录事件元数据哈希，不能拿 URL 哈希冒充远端内容哈希。
    hashes["draft://material-metadata"] = canonical_sha256(materials)
    return dict(sorted(hashes.items()))


def _manifest_artifact_record(draft_id: str, manifest: dict[str, Any], materials: list[dict[str, Any]]) -> dict[str, Any]:
    content = canonical_json_text(manifest).encode("utf-8")
    return {
        "record_version": "draft-artifact-record.v1",
        "draft_id": draft_id,
        # T-003 的通用登记只管理固定子目录；根目录清单由 state 同步写入，登记副本放在需求目录供索引核对。
        "source_path": "需求/material-manifest.v1.json",
        "artifact_type": "material_manifest",
        "media_type": "application/json",
        "projection_kind": "structured_json",
        "document": deepcopy(manifest),
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "input_hashes": _material_input_hashes(materials),
        "producer_task_id": "T-004",
        "producer_run_id": draft_artifacts.producer_run_id("T-004"),
    }


def _existing_material(draft: dict[str, Any], material_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in draft.get("materials", [])
            if isinstance(item, dict) and item.get("material_id") == material_id
        ),
        None,
    )


def _active_materials(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in draft.get("materials", [])
        if isinstance(item, dict) and item.get("status") != "archived"
    ]


def _validate_supersedes(draft: dict[str, Any], material_type: str, raw_value: str) -> str:
    clean = str(raw_value or "").strip().upper()
    if not clean:
        return ""
    target = _existing_material(draft, clean)
    if target is None:
        raise SdlcError(f"没有找到要替代的资料：{clean}。", exit_code=1)
    if target.get("status") == "archived":
        raise SdlcError(f"{clean} 已经归档，不能再次作为当前修订目标。", exit_code=1)
    if target.get("type") != material_type:
        raise SdlcError("新修订与被替代资料的 type 必须一致。", exit_code=1)
    return clean


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _interrupt_at(point: str) -> None:
    if os.environ.get("CODEX_SDLC_MATERIAL_INTERRUPT_AT", "").strip() == point:
        # 合同测试用独立进程在真实提交点退出，不能用普通异常和重试冒充进程中断。
        os._exit(86)


def _transaction_path(paths, transaction_id: str) -> Path:
    return paths.sdlc_dir / "drafts" / str(transaction_id.split(":", 1)[0]) / ".staging" / (
        MATERIAL_TRANSACTION_PREFIX + transaction_id.split(":", 1)[1] + ".json"
    )


def _new_transaction(paths, draft_id: str, material: dict[str, Any], temp_path: Path, target_path: Path) -> tuple[str, Path]:
    transaction_id = f"{draft_id}:{uuid.uuid4().hex}"
    journal_path = _transaction_path(paths, transaction_id)
    document = {
        "transaction_version": "material-transaction.v1",
        "transaction_id": transaction_id,
        "draft_id": draft_id,
        "material_id": material["material_id"],
        "temp_path": temp_path.relative_to(paths.draft_dir(draft_id)).as_posix(),
        "target_path": target_path.relative_to(paths.draft_dir(draft_id)).as_posix(),
        "sha256": material["sha256"],
        "phase": "copying",
    }
    _write_json_atomic(journal_path, document)
    return transaction_id, journal_path


def _update_transaction(journal_path: Path, phase: str) -> None:
    document = json.loads(journal_path.read_text(encoding="utf-8"))
    document["phase"] = phase
    _write_json_atomic(journal_path, document)


def _safe_transaction_target(paths, document: dict[str, Any], field: str) -> Path:
    draft_id = str(document.get("draft_id") or "")
    relative = Path(str(document.get(field) or ""))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise SdlcError("资料恢复记录包含不安全路径。", exit_code=1)
    target = paths.draft_dir(draft_id) / relative
    try:
        target.resolve(strict=False).relative_to(paths.draft_dir(draft_id).resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("资料恢复记录越过 DRAFT 目录。", exit_code=1) from exc
    return target


def _validate_transaction_document(journal_path: Path, document: object) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("transaction_version") != "material-transaction.v1":
        raise SdlcError("资料恢复记录版本无效。", exit_code=1)
    draft_id = str(document.get("draft_id") or "")
    transaction_id = str(document.get("transaction_id") or "")
    material_id = str(document.get("material_id") or "")
    digest = str(document.get("sha256") or "")
    if not re.fullmatch(r"DRAFT-[0-9]+", draft_id) or journal_path.parent.parent.name != draft_id:
        raise SdlcError("资料恢复记录的 DRAFT 编号无效。", exit_code=1)
    match = re.fullmatch(r"DRAFT-[0-9]+:([0-9a-f]{32})", transaction_id)
    expected_name = f"{MATERIAL_TRANSACTION_PREFIX}{match.group(1)}.json" if match else ""
    if not match or journal_path.name != expected_name:
        raise SdlcError("资料恢复记录的事务编号无效。", exit_code=1)
    if not re.fullmatch(r"MAT-[0-9]+", material_id) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SdlcError("资料恢复记录的资料编号或哈希无效。", exit_code=1)
    temp_path = Path(str(document.get("temp_path") or ""))
    target_path = Path(str(document.get("target_path") or ""))
    if temp_path.parts[:1] != (".staging",) or target_path.parts[:1] != ("原始资料",):
        raise SdlcError("资料恢复记录的暂存或归档路径无效。", exit_code=1)
    if not target_path.name.startswith(f"{material_id}_"):
        raise SdlcError("资料恢复记录的归档文件与 MAT 编号不一致。", exit_code=1)
    return document


def _events_for_material_recovery(paths) -> list[dict[str, Any]]:
    """只丢弃进程中断留下的最后一行残缺 JSON，中间损坏仍然必须人工处理。"""

    raw_lines = paths.events_file.read_bytes().splitlines(keepends=True)
    events: list[dict[str, Any]] = []
    for index, raw_line in enumerate(raw_lines):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line.decode("utf-8"))
            if not isinstance(event, dict):
                raise json.JSONDecodeError("事件不是对象", raw_line.decode("utf-8", errors="ignore"), 0)
            events.append(event)
        except (UnicodeError, json.JSONDecodeError) as exc:
            if index != len(raw_lines) - 1:
                raise SdlcError("events.jsonl 中间存在损坏记录，不能自动恢复资料事务。", exit_code=1) from exc
            valid_content = b"".join(raw_lines[:index])
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".events-material-recovery-",
                suffix=".tmp",
                dir=paths.events_file.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(valid_content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, paths.events_file)
            except Exception:
                Path(temporary_name).unlink(missing_ok=True)
                raise
    return events


def recover_material_transactions(paths) -> dict[str, int]:
    """以事件作为提交边界，把中断现场恢复成完整成功或完整失败。"""

    result = {"committed": 0, "rolled_back": 0}
    journals = sorted(paths.drafts_dir.glob(f"*/.staging/{MATERIAL_TRANSACTION_PREFIX}*.json"))
    if not journals:
        return result
    events = _events_for_material_recovery(paths)
    committed_ids = {
        str(event.get("payload", {}).get("transaction_id") or "")
        for event in events
        if event.get("event_type") == "draft_material_added" and isinstance(event.get("payload"), dict)
    }
    needs_refresh = False
    committed_journals: list[Path] = []
    for journal_path in journals:
        try:
            document = _validate_transaction_document(
                journal_path,
                json.loads(journal_path.read_text(encoding="utf-8")),
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SdlcError("资料恢复记录损坏，不能继续写入。", exit_code=1) from exc
        transaction_id = str(document.get("transaction_id") or "")
        temp_path = _safe_transaction_target(paths, document, "temp_path")
        target_path = _safe_transaction_target(paths, document, "target_path")
        expected_hash = str(document.get("sha256") or "")
        if transaction_id in committed_ids:
            if not target_path.is_file() or sha256_file(target_path) != expected_hash:
                raise SdlcError("已提交资料的归档文件缺失或哈希不一致，不能继续写入。", exit_code=1)
            needs_refresh = True
            result["committed"] += 1
            committed_journals.append(journal_path)
        else:
            if target_path.exists():
                if target_path.is_symlink() or not target_path.is_file():
                    raise SdlcError("未提交资料的目标路径不安全，不能自动清理。", exit_code=1)
                target_path.unlink()
            result["rolled_back"] += 1
        temp_path.unlink(missing_ok=True)
        if transaction_id not in committed_ids:
            journal_path.unlink(missing_ok=True)
    if needs_refresh:
        refresh_materialized_state(paths)
        # 已提交事件只有在全部投影重建成功后才清理恢复记录，下一次调用仍能继续同一恢复动作。
        for journal_path in committed_journals:
            journal_path.unlink(missing_ok=True)
    return result


def _restore_event_file(paths, existed: bool, original: bytes) -> None:
    if existed:
        paths.events_file.write_bytes(original)
    else:
        paths.events_file.unlink(missing_ok=True)


def _commit_material_event(
    paths,
    draft: dict[str, Any],
    material: dict[str, Any],
    *,
    operation: str,
    transaction_id: str = "",
    created_target: Path | None = None,
    managed_snapshot: dict[str, bytes] | None = None,
) -> None:
    projected = _projected_materials(draft, material)
    manifest = _material_manifest(str(draft["draft_id"]), projected)
    artifact_record = _manifest_artifact_record(str(draft["draft_id"]), manifest, projected)
    draft_artifacts.preflight_artifact_updates(
        paths.draft_dir(str(draft["draft_id"])),
        {**draft, "materials": projected},
        [artifact_record],
    )
    existed = paths.events_file.exists()
    original_events = paths.events_file.read_bytes() if existed else b""
    original_event_backups = {
        backup.name: backup.read_bytes()
        for backup in paths.backups_dir.glob("events-*.jsonl.bak")
        if backup.is_file() and not backup.is_symlink()
    }
    snapshot = managed_snapshot if managed_snapshot is not None else draft_artifacts.snapshot_managed_files(paths.draft_dir(str(draft["draft_id"])))
    try:
        append_event(
            paths,
            event_type="draft_material_added",
            source="sdlc-material",
            summary=f"归档 {draft['draft_id']} 资料 {material['material_id']}",
            payload={
                "draft_id": draft["draft_id"],
                "operation": operation,
                "transaction_id": transaction_id,
                "material": deepcopy(material),
                "artifact_updates": [artifact_record],
            },
        )
        _interrupt_at("after_event_append")
        refresh_materialized_state(paths)
    except Exception:
        _restore_event_file(paths, existed, original_events)
        for backup in paths.backups_dir.glob("events-*.jsonl.bak"):
            backup.unlink(missing_ok=True)
        for name, content in original_event_backups.items():
            (paths.backups_dir / name).write_bytes(content)
        if created_target is not None:
            created_target.unlink(missing_ok=True)
        draft_artifacts.restore_managed_files(paths.draft_dir(str(draft["draft_id"])), snapshot)
        try:
            refresh_materialized_state(paths)
        except Exception:
            draft_artifacts.restore_managed_files(paths.draft_dir(str(draft["draft_id"])), snapshot)
        raise


def _copy_original_file(source: Path, temporary: Path, expected_hash: str) -> None:
    source_descriptor, before = _open_regular_source(source)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        # 临时文件创建失败时源文件还没有交给 fdopen，必须在这里主动关闭。
        os.close(source_descriptor)
        raise
    try:
        with os.fdopen(source_descriptor, "rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            after = os.fstat(source_handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if (
        sha256_file(temporary) != expected_hash
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or not _same_source_identity(source, before)
    ):
        temporary.unlink(missing_ok=True)
        raise SdlcError("原始资料在复制期间发生变化，已停止归档。", exit_code=1)


def _commit_file_material(paths, draft: dict[str, Any], material: dict[str, Any], source: Path) -> None:
    layout = draft_artifacts.ensure_draft_layout(paths, str(draft["draft_id"]))
    target = layout.draft_dir / str(material["stored_path"])
    if target.exists() or target.is_symlink():
        raise SdlcError("原始资料目标文件已经存在但没有对应事件，不能覆盖。", exit_code=1)
    managed_snapshot = draft_artifacts.snapshot_managed_files(layout.draft_dir)
    temporary = layout.draft_dir / ".staging" / f"material-{uuid.uuid4().hex}.tmp"
    transaction_id, journal_path = _new_transaction(paths, str(draft["draft_id"]), material, temporary, target)
    try:
        _copy_original_file(source, temporary, str(material["sha256"]))
        _interrupt_at("after_temp_copy")
        os.replace(temporary, target)
        directory_descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if sha256_file(target) != material["sha256"]:
            target.unlink(missing_ok=True)
            raise SdlcError("原始资料原子提交后的哈希不一致。", exit_code=1)
        _update_transaction(journal_path, "file_committed")
        _interrupt_at("after_atomic_rename")
        _commit_material_event(
            paths,
            draft,
            material,
            operation="created",
            transaction_id=transaction_id,
            created_target=target,
            managed_snapshot=managed_snapshot,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        if not any(
            event.get("event_type") == "draft_material_added"
            and event.get("payload", {}).get("transaction_id") == transaction_id
            for event in load_events(paths)
        ):
            target.unlink(missing_ok=True)
        raise
    else:
        temporary.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)


def _file_material(
    root: Path,
    paths,
    draft: dict[str, Any],
    *,
    title: str,
    material_type: str,
    roles: list[str],
    scopes: list[str],
    sensitivity: str,
    raw_file: str,
    supersedes: str,
) -> tuple[dict[str, Any], Path, bool]:
    source, source_path = _resolve_project_file(root, raw_file, label="原始资料")
    digest, source_size = _inspect_source_file(source)
    existing = next(
        (
            item
            for item in _active_materials(draft)
            if item.get("source_kind") == "file" and item.get("sha256") == digest
        ),
        None,
    )
    if existing is not None:
        if supersedes:
            raise SdlcError("相同哈希的资料已经归档，不能把同一字节作为新修订。", exit_code=1)
        updated = deepcopy(existing)
        updated["roles"] = sorted(set(updated.get("roles", [])) | set(roles))
        updated["applies_to"] = sorted(set(updated.get("applies_to", [])) | set(scopes))
        if sensitivity == "internal" and updated.get("sensitivity") == "public":
            # 同一字节只保留一份时采用更严格的普通敏感级别，不能因复用把资料降级为 public。
            updated["sensitivity"] = "internal"
        changed = updated != existing
        return updated, source, changed

    material_id = next_number([str(item.get("material_id") or "") for item in draft.get("materials", [])], "MAT")
    stored_path = f"原始资料/{_safe_material_filename(material_id, title, source)}"
    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    material = {
        "material_id": material_id,
        "source_kind": "file",
        "type": material_type,
        "roles": roles,
        "title": title,
        "stored_path": stored_path,
        "source_path": source_path,
        "sha256": digest,
        "size_bytes": source_size,
        "media_type": media_type,
        "sensitivity": sensitivity,
        "applies_to": scopes,
        "status": "active",
    }
    if supersedes:
        material["supersedes"] = supersedes
    return material, source, True


def _load_secret_reference(root: Path, raw_path: str) -> dict[str, Any]:
    path, _ = _resolve_project_file(root, raw_path, label="秘密引用合同")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("秘密引用合同读取失败或不是有效 JSON。", exit_code=1) from exc
    validate_schema_document(document, schema_name=SECRET_REFERENCE_SCHEMA)
    return deepcopy(document)


def _reference_material(
    root: Path,
    paths,
    draft: dict[str, Any],
    args: argparse.Namespace,
    *,
    title: str,
    material_type: str,
    roles: list[str],
    scopes: list[str],
    supersedes: str,
) -> tuple[dict[str, Any], bool]:
    if args.url:
        normalized_url = normalize_external_url(args.url)
        evidence = (
            load_external_version_evidence(
                _resolve_project_file(root, args.version_evidence, label="外部版本证据")[0],
                raw_url=args.url,
            )
            if args.version_evidence
            else unversioned_evidence(args.url)
        )
        evidence_detail = evidence.get("evidence") if isinstance(evidence.get("evidence"), dict) else {}
        if evidence_detail.get("kind") == "local_snapshot":
            snapshot = _existing_material(draft, str(evidence_detail.get("material_id") or ""))
            if (
                snapshot is None
                or snapshot.get("source_kind") != "file"
                or snapshot.get("sha256") != evidence_detail.get("sha256")
            ):
                raise SdlcError("外部版本证据引用的本地快照不存在或哈希不一致。", exit_code=1)
            stored_path = Path(str(snapshot.get("stored_path") or ""))
            if stored_path.is_absolute() or ".." in stored_path.parts or not stored_path.parts or stored_path.parts[0] != "原始资料":
                raise SdlcError("外部版本证据引用的本地快照路径无效。", exit_code=1)
            snapshot_path = paths.draft_dir(str(draft["draft_id"])) / stored_path
            try:
                snapshot_path.resolve(strict=True).relative_to(paths.draft_original_materials_dir(str(draft["draft_id"])).resolve(strict=True))
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
                raise SdlcError("外部版本证据引用的本地快照路径越过 DRAFT。", exit_code=1) from exc
            if snapshot_path.is_symlink() or not snapshot_path.is_file() or sha256_file(snapshot_path) != snapshot["sha256"]:
                raise SdlcError("外部版本证据引用的本地快照文件缺失或已经变化。", exit_code=1)
        status = str(evidence["status"])
        url_hash = normalized_url_sha256(args.url)
        same_url_materials = [
            item
            for item in _active_materials(draft)
            if item.get("source_kind") == "external-reference"
            and item.get("normalized_url_sha256") == url_hash
        ]
        if len(same_url_materials) > 1:
            raise SdlcError("同一外部地址存在多个活动版本，请先修复资料清单后再归档。", exit_code=1)
        exact = next((item for item in same_url_materials if item.get("version_evidence") == evidence), None)
        if exact is not None:
            if supersedes:
                raise SdlcError("相同外部版本证据已经归档，不能把同一版本作为新修订。", exit_code=1)
            updated = deepcopy(exact)
            updated["roles"] = sorted(set(updated.get("roles", [])) | set(roles))
            updated["applies_to"] = sorted(set(updated.get("applies_to", [])) | set(scopes))
            return updated, updated != exact
        if same_url_materials:
            current_material_id = str(same_url_materials[0].get("material_id") or "")
            if not supersedes:
                raise SdlcError("同一外部地址的版本证据已经变化；请核对后用 --supersedes 归档新修订。", exit_code=1)
            if supersedes != current_material_id:
                raise SdlcError("外部版本修订必须替代同一地址当前唯一的活动 MAT。", exit_code=1)
        elif supersedes:
            raise SdlcError("外部版本修订目标必须是同一地址当前唯一的活动 MAT。", exit_code=1)
        material = {
            "material_id": next_number([str(item.get("material_id") or "") for item in draft.get("materials", [])], "MAT"),
            "source_kind": "external-reference",
            "type": material_type,
            "roles": roles,
            "title": title,
            # 事件只保存已经移除片段、规范化完成的地址，原始输入不会在后续投影中扩散。
            "url": normalized_url,
            "normalized_url_sha256": url_hash,
            "access_condition": args.access_condition,
            "version_evidence": evidence,
            "sensitivity": args.sensitivity,
            "applies_to": scopes,
            "status": status,
        }
    else:
        secret_reference = _load_secret_reference(root, args.secret_reference)
        exact = next(
            (
                item
                for item in _active_materials(draft)
                if item.get("source_kind") == "secret-reference"
                and item.get("type") == material_type
                and item.get("secret_reference") == secret_reference
            ),
            None,
        )
        if exact is not None:
            if supersedes:
                raise SdlcError("相同秘密引用已经归档，不能把同一引用作为新修订。", exit_code=1)
            updated = deepcopy(exact)
            updated["roles"] = sorted(set(updated.get("roles", [])) | set(roles))
            updated["applies_to"] = sorted(set(updated.get("applies_to", [])) | set(scopes))
            return updated, updated != exact
        material = {
            "material_id": next_number([str(item.get("material_id") or "") for item in draft.get("materials", [])], "MAT"),
            "source_kind": "secret-reference",
            "type": material_type,
            "roles": roles,
            "title": title,
            "secret_reference": secret_reference,
            "sensitivity": "secret-reference",
            "applies_to": scopes,
            "status": "active",
        }
    if supersedes:
        material["supersedes"] = supersedes
    return material, True


def run(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    target = str(args.draft or "").strip().upper()
    _validate_new_boundary(args, target)
    title = _clean_title(args.title)
    material_type = str(args.type)
    roles = sorted(set([material_type, *[str(item) for item in args.role]]))
    scopes = sorted(set(str(item).strip() for item in args.scope if str(item).strip()))

    with project_lock(paths):
        recover_material_transactions(paths)
        state = derive_state(paths)
        if _material_projection_needs_refresh(paths, state):
            # 引用事件和角色扩展没有文件事务，只有投影缺失时才做恢复，普通失败不会改写无关文件。
            state = refresh_materialized_state(paths)
        draft = state.get("drafts", {}).get(target)
        if not isinstance(draft, dict):
            raise SdlcError(f"没有找到 DRAFT `{target}`。", exit_code=1)
        if draft_lifecycle.is_started_draft(draft):
            raise SdlcError(f"{target} 已经正式建档，不能再接收普通资料。", exit_code=1)
        draft_artifacts.ensure_draft_layout(paths, target)
        supersedes = _validate_supersedes(draft, material_type, args.supersedes)

        if args.file:
            material, source, changed = _file_material(
                root,
                paths,
                draft,
                title=title,
                material_type=material_type,
                roles=roles,
                scopes=scopes,
                sensitivity=args.sensitivity,
                raw_file=args.file,
                supersedes=supersedes,
            )
            if changed and _existing_material(draft, str(material["material_id"])) is not None:
                _commit_material_event(paths, draft, material, operation="roles-expanded")
            elif changed:
                _commit_file_material(paths, draft, material, source)
        else:
            material, changed = _reference_material(
                root,
                paths,
                draft,
                args,
                title=title,
                material_type=material_type,
                roles=roles,
                scopes=scopes,
                supersedes=supersedes,
            )
            if changed:
                operation = "roles-expanded" if _existing_material(draft, str(material["material_id"])) else "created"
                _commit_material_event(paths, draft, material, operation=operation)
        if not changed:
            # 引用资料和角色扩展没有本地文件事务；若进程在事件提交后退出，幂等重试也要重建缺失投影。
            refresh_materialized_state(paths)

    print(f"已归档 DRAFT 资料：{material['material_id']}")
    print(f"类型：{material['type']}")
    print("角色：" + "、".join(str(item) for item in material["roles"]))
    print(f"来源类型：{material['source_kind']}")
    print(f"状态：{material['status']}")
    if material.get("stored_path"):
        print(f"文件：.codex-sdlc/drafts/{target}/{material['stored_path']}")
    if material.get("status") == "unversioned":
        print("外部资料缺少稳定版本证据，当前不能用于后续需求审核或正式建档。")
    return 0


def run_change_material(args: argparse.Namespace) -> int:
    root = resolve_project_root(Path.cwd(), allow_plain_directory=True)
    paths = build_paths(root)
    result = add_change_material(
        paths,
        requirement_id=str(args.requirement_id),
        change_id=str(args.change_id),
        material_type=str(args.type),
        file_path=str(args.file or ""),
        url=str(args.url or ""),
        version_evidence_path=str(args.version_evidence or ""),
        secret_reference_path=str(args.secret_reference or ""),
        interruption_hook=change_material_environment_interruption_hook(),
    )
    action = "已复用变更资料" if result.duplicate else "已归档变更资料"
    print(f"{action}：{result.material_id}")
    print(f"需求：{result.requirement_id}")
    print(f"变更：{result.change_id}")
    print(f"来源类型：{result.source_kind}")
    print(f"状态：{result.status}")
    print(f"事件：{result.event_id}")
    print(f"资料清单：{result.workspace_path}/change-material-manifest.v1.json")
    return 0


__all__ = [
    "MATERIAL_TYPES",
    "recover_material_transactions",
    "register",
    "run",
    "run_change_material",
]
