from __future__ import annotations

from dataclasses import dataclass
import errno
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Callable, Iterable, Mapping
import uuid

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.external_version import (
    normalize_external_url,
    normalized_url_sha256,
    unversioned_evidence,
)
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    CLIENT_KEY_PATTERN,
    allocate_stable_ids,
)
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.state import load_events, next_event_id, now_iso
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)


CHANGE_WORKSPACE_SCHEMA = "change-workspace.v1"
CHANGE_TRANSACTION_SCHEMA = "change-create-transaction.v1"
CHANGE_CREATED_EVENT_TYPE = "change_workspace_created"
CHANGE_CREATED_EVENT_SOURCE = "sdlc-change-create"
CHANGE_INTERRUPT_ENV = "CODEX_SDLC_CHANGE_CREATE_INTERRUPT"

INTERRUPT_BEFORE_DIRECTORY_PUBLISH = "before_directory_publish"
INTERRUPT_AFTER_DIRECTORY_PUBLISH = "after_directory_publish"
INTERRUPT_AFTER_EVENT_APPEND = "after_event_append"

InterruptionHook = Callable[[str], None]

REQUIREMENT_ID_PATTERN = re.compile(r"^REQ-[0-9]+$")
CHANGE_ID_PATTERN = re.compile(r"^CHG-[0-9]+$")
EVENT_ID_PATTERN = re.compile(r"^EVT-[0-9]{8}-[0-9]{6,}$")
TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

BASE_VERSION_PATHS = {
    "requirement": "effective/requirement.current.json",
    "design": "effective/design.current.json",
    "test_matrix": "effective/test-matrix.current.json",
    "reference_index": "reference-index.v1.json",
    "task_plan": "tasks/task-plan.v2.json",
}

EVENT_FIELDS = {
    "event_id",
    "event_type",
    "project_path",
    "requirement_id",
    "task_id",
    "created_at",
    "source",
    "summary",
    "payload",
}

EVENT_PAYLOAD_FIELDS = {
    "requirement_id",
    "request_key",
    "change_id",
    "workspace_path",
    "status_sha256",
}

TRANSACTION_FIELDS = {
    "schema_version",
    "transaction_id",
    "requirement_id",
    "request_key",
    "change_id",
    "workspace_path",
    "staging_path",
    "status_sha256",
    "event",
}


@dataclass(frozen=True)
class ChangeWorkspaceResult:
    """创建命令的稳定结果；幂等重试只改变 duplicate 标记。"""

    requirement_id: str
    change_id: str
    workspace_path: str
    created_event_id: str
    duplicate: bool


def validate_request_key(request_key: object) -> str:
    """请求键不做大小写或空白转换，避免两个调用方得到不同幂等身份。"""

    if not isinstance(request_key, str) or not CLIENT_KEY_PATTERN.fullmatch(request_key):
        raise SdlcError(
            "request-key 格式不正确，必须匹配 [a-z0-9][a-z0-9._-]{0,127}。"
        )
    return request_key


def _validate_requirement_id(requirement_id: object) -> str:
    if not isinstance(requirement_id, str) or not REQUIREMENT_ID_PATTERN.fullmatch(
        requirement_id
    ):
        raise SdlcError("需求编号格式不正确，必须是 REQ-数字。")
    return requirement_id


def _project_relative(paths: ProjectPaths, path: Path) -> str:
    try:
        return path.relative_to(paths.root).as_posix()
    except ValueError as exc:
        raise SdlcError(f"路径越过项目目录：{path}。") from exc


def _assert_no_symlink_components(root: Path, path: Path, *, label: str) -> None:
    """逐级检查实际路径，不能只检查最后一个文件是不是符号链接。"""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SdlcError(f"{label}越过项目目录。") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"{label}不能穿过符号链接：{_project_relative(ProjectPaths(root), current)}。")


def resolve_formal_requirement_dir(
    paths: ProjectPaths,
    requirement_id: object,
    events: Iterable[Mapping[str, object]],
) -> Path:
    """按目录与创建事件双向确认唯一正式需求，不能只取 glob 的第一个结果。"""

    clean_requirement_id = _validate_requirement_id(requirement_id)
    if not paths.requirements_dir.is_dir() or paths.requirements_dir.is_symlink():
        raise SdlcError("正式需求目录不存在或不是普通目录。")

    candidates: list[Path] = []
    for entry in sorted(paths.requirements_dir.iterdir(), key=lambda item: item.name):
        if entry.name != clean_requirement_id and not entry.name.startswith(
            f"{clean_requirement_id}-"
        ):
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise SdlcError(f"{clean_requirement_id} 的正式目录不能是符号链接或普通文件。")
        candidates.append(entry)
    if not candidates:
        raise SdlcError(f"没有找到正式需求 `{clean_requirement_id}`。")
    if len(candidates) != 1:
        raise SdlcError(f"正式需求 `{clean_requirement_id}` 对应多个目录，不能继续创建变更。")

    requirement_events = [
        event
        for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == "requirement_created"
        and event.get("requirement_id") == clean_requirement_id
    ]
    if len(requirement_events) != 1:
        raise SdlcError(
            f"正式需求 `{clean_requirement_id}` 必须对应唯一 requirement_created 事件。"
        )
    if requirement_events[0].get("project_path") != str(paths.root):
        raise SdlcError(f"正式需求 `{clean_requirement_id}` 的项目路径与当前项目不一致。")
    payload = requirement_events[0].get("payload")
    folder_name = payload.get("folder_name") if isinstance(payload, Mapping) else None
    if folder_name != candidates[0].name:
        raise SdlcError(f"正式需求 `{clean_requirement_id}` 的目录与创建事件不一致。")
    _assert_no_symlink_components(paths.root, candidates[0], label="正式需求目录")
    return candidates[0]


def build_base_versions(paths: ProjectPaths, requirement_dir: Path) -> dict[str, object]:
    """在持有项目锁时读取五份真实文件，并对包含结尾换行的完整字节计算哈希。"""

    result: dict[str, object] = {}
    for name, suffix in BASE_VERSION_PATHS.items():
        path = requirement_dir / suffix
        _assert_no_symlink_components(paths.root, path, label=f"基础版本 {name}")
        if not path.is_file() or path.is_symlink():
            raise SdlcError(f"基础版本缺失或不是普通文件：{_project_relative(paths, path)}。")
        result[name] = {
            "path": _project_relative(paths, path),
            "sha256": sha256_file(path),
        }
    return result


def build_status_document(
    *,
    requirement_id: str,
    change_id: str,
    request_key: str,
    workspace_path: str,
    base_versions: Mapping[str, object],
    created_event_id: str,
) -> dict[str, object]:
    status = {
        "schema_version": CHANGE_WORKSPACE_SCHEMA,
        "requirement_id": requirement_id,
        "change_id": change_id,
        "request_key": request_key,
        "workspace_path": workspace_path,
        "status": "draft",
        "base_versions": dict(base_versions),
        "created_event_id": created_event_id,
    }
    validate_schema_document(status, schema_name=CHANGE_WORKSPACE_SCHEMA)
    return status


def status_bytes(document: Mapping[str, object]) -> bytes:
    validate_schema_document(document, schema_name=CHANGE_WORKSPACE_SCHEMA)
    return canonical_json_text(document).encode("utf-8")


def build_created_event(
    paths: ProjectPaths,
    *,
    event_id: str,
    requirement_id: str,
    request_key: str,
    change_id: str,
    workspace_path: str,
    status_sha256: str,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": CHANGE_CREATED_EVENT_TYPE,
        "project_path": str(paths.root),
        "requirement_id": requirement_id,
        "task_id": None,
        "created_at": now_iso(),
        "source": CHANGE_CREATED_EVENT_SOURCE,
        "summary": f"创建结构化变更工作区 {change_id}",
        "payload": {
            "requirement_id": requirement_id,
            "request_key": request_key,
            "change_id": change_id,
            "workspace_path": workspace_path,
            "status_sha256": status_sha256,
        },
    }


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SdlcError(f"{label}包含重复字段：{key}。")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise SdlcError(f"{label}包含非标准数字：{value}。")

    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是有效 JSON：{path.name}。") from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是对象：{path.name}。")
    return document


def load_workspace_status(paths: ProjectPaths, workspace: Path) -> dict[str, object]:
    _assert_no_symlink_components(paths.root, workspace, label="变更工作区")
    if workspace.is_symlink() or not workspace.is_dir():
        raise SdlcError(f"变更工作区不是普通目录：{_project_relative(paths, workspace)}。")
    status_path = workspace / "status.json"
    _assert_no_symlink_components(paths.root, status_path, label="变更状态文件")
    if status_path.is_symlink() or not status_path.is_file():
        raise SdlcError(f"变更工作区缺少普通状态文件：{_project_relative(paths, status_path)}。")
    document = _read_json(status_path, label="变更状态文件")
    validate_schema_document(document, schema_name=CHANGE_WORKSPACE_SCHEMA)
    return document


def _validate_event_shape(paths: ProjectPaths, event: Mapping[str, object]) -> dict[str, object]:
    if set(event) != EVENT_FIELDS:
        raise SdlcError("change_workspace_created 事件字段不完整或包含额外字段。")
    payload = event.get("payload")
    if not isinstance(payload, dict) or set(payload) != EVENT_PAYLOAD_FIELDS:
        raise SdlcError("change_workspace_created 事件 payload 字段不完整或包含额外字段。")
    if (
        not isinstance(event.get("event_id"), str)
        or not EVENT_ID_PATTERN.fullmatch(str(event.get("event_id")))
        or event.get("event_type") != CHANGE_CREATED_EVENT_TYPE
        or event.get("project_path") != str(paths.root)
        or event.get("requirement_id") != payload.get("requirement_id")
        or event.get("task_id") is not None
        or not isinstance(event.get("created_at"), str)
        or not str(event.get("created_at")).strip()
        or event.get("source") != CHANGE_CREATED_EVENT_SOURCE
        or event.get("summary") != f"创建结构化变更工作区 {payload.get('change_id')}"
    ):
        raise SdlcError("change_workspace_created 事件固定字段不正确。")
    if (
        not isinstance(payload.get("requirement_id"), str)
        or not REQUIREMENT_ID_PATTERN.fullmatch(str(payload.get("requirement_id")))
        or not isinstance(payload.get("request_key"), str)
        or not CLIENT_KEY_PATTERN.fullmatch(str(payload.get("request_key")))
        or not isinstance(payload.get("change_id"), str)
        or not CHANGE_ID_PATTERN.fullmatch(str(payload.get("change_id")))
        or not isinstance(payload.get("workspace_path"), str)
        or not isinstance(payload.get("status_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(payload.get("status_sha256")))
    ):
        raise SdlcError("change_workspace_created 事件身份、路径或哈希格式不正确。")
    return payload


def verify_workspace_event(
    paths: ProjectPaths,
    workspace: Path,
    event: Mapping[str, object],
    *,
    verify_current_bases: bool,
    verify_initial_layout: bool = False,
) -> dict[str, object]:
    """逐字节核对状态、事件和可选的当前基础文件，幂等重试不能覆盖漂移。"""

    payload = _validate_event_shape(paths, event)
    status = load_workspace_status(paths, workspace)
    workspace_path = _project_relative(paths, workspace)
    status_path = workspace / "status.json"
    expected_pairs = {
        "requirement_id": payload["requirement_id"],
        "request_key": payload["request_key"],
        "change_id": payload["change_id"],
        "workspace_path": workspace_path,
        "created_event_id": event["event_id"],
    }
    if any(status.get(key) != value for key, value in expected_pairs.items()):
        raise SdlcError(f"{workspace_path} 的 status.json 与创建事件不一致。")
    if payload.get("workspace_path") != workspace_path:
        raise SdlcError(f"{workspace_path} 的创建事件记录了错误工作区路径。")
    actual_status_sha256 = sha256_file(status_path)
    if not hmac.compare_digest(
        actual_status_sha256, str(payload.get("status_sha256") or "")
    ):
        raise SdlcError(f"{workspace_path} 的 status.json 整体哈希与创建事件不一致。")

    requirement_dir = workspace.parent.parent
    requirement_id = str(payload["requirement_id"])
    if requirement_dir.name != requirement_id and not requirement_dir.name.startswith(
        f"{requirement_id}-"
    ):
        raise SdlcError(f"{workspace_path} 不属于事件登记的正式需求。")
    base_versions = status.get("base_versions")
    if not isinstance(base_versions, dict):
        raise SdlcError(f"{workspace_path} 缺少基础版本记录。")
    for name, suffix in BASE_VERSION_PATHS.items():
        base = base_versions.get(name)
        expected_path = _project_relative(paths, requirement_dir / suffix)
        if not isinstance(base, dict) or base.get("path") != expected_path:
            raise SdlcError(f"{workspace_path} 的基础版本 {name} 路径不正确。")

    if verify_initial_layout:
        original_materials = workspace / "原始资料"
        reviews = workspace / "reviews"
        if (
            original_materials.is_symlink()
            or not original_materials.is_dir()
            or reviews.is_symlink()
            or not reviews.is_dir()
        ):
            raise SdlcError(f"{workspace_path} 缺少两个空的普通工作目录。")
        if any(original_materials.iterdir()) or any(reviews.iterdir()):
            raise SdlcError(f"{workspace_path} 的初始工作目录不为空。")
        if {item.name for item in workspace.iterdir()} != {
            "status.json",
            "原始资料",
            "reviews",
        }:
            raise SdlcError(f"{workspace_path} 提前包含了后续任务产物。")

    if verify_current_bases:
        current_bases = build_base_versions(paths, requirement_dir)
        if current_bases != base_versions:
            raise SdlcError(f"{workspace_path} 的基础版本已经与当前正式文件不一致。")
    return status


def created_events(events: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        event
        for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == CHANGE_CREATED_EVENT_TYPE
    ]


def _workspace_directories(paths: ProjectPaths) -> list[Path]:
    result: list[Path] = []
    if not paths.requirements_dir.is_dir() or paths.requirements_dir.is_symlink():
        return result
    for requirement_dir in sorted(paths.requirements_dir.iterdir(), key=lambda item: item.name):
        if requirement_dir.is_symlink():
            if requirement_dir.name.startswith("REQ-"):
                raise SdlcError(f"正式需求目录不能是符号链接：{requirement_dir.name}。")
            continue
        if not requirement_dir.is_dir():
            continue
        changes_dir = requirement_dir / "changes"
        if not changes_dir.exists():
            continue
        if changes_dir.is_symlink() or not changes_dir.is_dir():
            raise SdlcError(f"变更目录不能是符号链接或普通文件：{_project_relative(paths, changes_dir)}。")
        for candidate in sorted(changes_dir.iterdir(), key=lambda item: item.name):
            if not CHANGE_ID_PATTERN.fullmatch(candidate.name):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                raise SdlcError(f"CHG 工作区必须是普通目录：{_project_relative(paths, candidate)}。")
            result.append(candidate)
    return result


def _legacy_change_ids(events: Iterable[Mapping[str, object]]) -> set[str]:
    result: set[str] = set()
    single_fields = {
        "change_recorded": "change_id",
        "change_planned": "change_id",
    }
    list_fields = {
        "change_accepted": "change_ids",
        "change_resolved": "change_ids",
        "change_verified": "change_ids",
    }
    for event in events:
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        event_type = str(event.get("event_type") or "")
        if event_type in single_fields:
            value = payload.get(single_fields[event_type])
            if isinstance(value, str) and CHANGE_ID_PATTERN.fullmatch(value):
                result.add(value)
        if event_type in list_fields:
            value = payload.get(list_fields[event_type])
            if isinstance(value, list):
                result.update(
                    item
                    for item in value
                    if isinstance(item, str) and CHANGE_ID_PATTERN.fullmatch(item)
                )
    return result


def collect_used_change_ids(
    paths: ProjectPaths,
    events: Iterable[Mapping[str, object]],
) -> frozenset[str]:
    """只从受控事件字段和正式路径名收集编号，普通文本里的 CHG 字样不占号。"""

    event_list = list(events)
    result = _legacy_change_ids(event_list)
    for event in created_events(event_list):
        payload = event.get("payload")
        change_id = payload.get("change_id") if isinstance(payload, Mapping) else None
        if isinstance(change_id, str) and CHANGE_ID_PATTERN.fullmatch(change_id):
            result.add(change_id)
    if paths.changes_dir.exists() and (
        paths.changes_dir.is_symlink() or not paths.changes_dir.is_dir()
    ):
        raise SdlcError("旧变更记录目录不能是符号链接或普通文件。")
    if paths.changes_dir.is_dir():
        for entry in paths.changes_dir.iterdir():
            match = re.match(r"^(CHG-[0-9]+)(?:\..+)?$", entry.name)
            if match:
                result.add(match.group(1))
    if paths.requirements_dir.is_dir() and not paths.requirements_dir.is_symlink():
        for requirement_dir in paths.requirements_dir.iterdir():
            if requirement_dir.is_symlink() or not requirement_dir.is_dir():
                continue
            changes_dir = requirement_dir / "changes"
            if changes_dir.is_symlink() or not changes_dir.is_dir():
                continue
            for entry in changes_dir.iterdir():
                match = re.match(r"^(CHG-[0-9]+)(?:\..+)?$", entry.name)
                if match:
                    result.add(match.group(1))
    return frozenset(result)


def validate_registered_workspaces(
    paths: ProjectPaths,
    events: Iterable[Mapping[str, object]],
) -> tuple[list[Path], list[Mapping[str, object]]]:
    """新式 CHG 目录和创建事件必须一一对应，任何半状态都会阻止继续编号。"""

    event_list = list(events)
    event_ids = [str(event.get("event_id") or "") for event in event_list]
    if len(event_ids) != len(set(event_ids)):
        raise SdlcError("events.jsonl 包含重复事件编号，不能继续创建变更。")

    workspace_list = _workspace_directories(paths)
    by_change_id: dict[str, list[Path]] = {}
    for workspace in workspace_list:
        by_change_id.setdefault(workspace.name, []).append(workspace)
    duplicates = [change_id for change_id, items in by_change_id.items() if len(items) != 1]
    if duplicates:
        raise SdlcError(f"全项目存在重复 CHG 工作区：{duplicates[0]}。")

    creation_events = created_events(event_list)
    event_by_change_id: dict[str, list[Mapping[str, object]]] = {}
    event_by_identity: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for event in creation_events:
        payload = _validate_event_shape(paths, event)
        change_id = str(payload["change_id"])
        identity = (str(payload["requirement_id"]), str(payload["request_key"]))
        event_by_change_id.setdefault(change_id, []).append(event)
        event_by_identity.setdefault(identity, []).append(event)
    if any(len(items) != 1 for items in event_by_change_id.values()):
        raise SdlcError("同一 CHG 对应多个创建事件，不能继续创建变更。")
    if any(len(items) != 1 for items in event_by_identity.values()):
        raise SdlcError("同一 requirement_id 与 request_key 对应多个创建事件。")

    legacy_ids = _legacy_change_ids(event_list)
    for change_id in by_change_id:
        if change_id in legacy_ids:
            raise SdlcError(f"{change_id} 同时被旧变更记录和结构化工作区占用。")

    for workspace in workspace_list:
        matching_events = event_by_change_id.get(workspace.name, [])
        if len(matching_events) != 1:
            raise SdlcError(f"已有 CHG 目录没有唯一合法创建登记：{workspace.name}。")
        verify_workspace_event(
            paths,
            workspace,
            matching_events[0],
            verify_current_bases=False,
        )
    for change_id, items in event_by_change_id.items():
        if change_id not in by_change_id:
            raise SdlcError(f"创建事件缺少对应 CHG 工作区：{change_id}。")
        payload = items[0]["payload"]
        if not isinstance(payload, Mapping):
            raise SdlcError(f"{change_id} 创建事件 payload 不正确。")
        expected_path = _project_relative(paths, by_change_id[change_id][0])
        if payload.get("workspace_path") != expected_path:
            raise SdlcError(f"{change_id} 创建事件指向了错误工作区。")
    return workspace_list, creation_events


def find_idempotent_event(
    creation_events: Iterable[Mapping[str, object]],
    *,
    requirement_id: str,
    request_key: str,
) -> Mapping[str, object] | None:
    matches: list[Mapping[str, object]] = []
    for event in creation_events:
        payload = event.get("payload")
        if (
            isinstance(payload, Mapping)
            and payload.get("requirement_id") == requirement_id
            and payload.get("request_key") == request_key
        ):
            matches.append(event)
    if len(matches) > 1:
        raise SdlcError("同一创建请求存在多个事件，不能幂等返回。")
    return matches[0] if matches else None


def allocate_change_id(request_key: str, used_ids: Iterable[str]) -> str:
    mapping = allocate_stable_ids(
        [AllocationObject(client_key=request_key, id_prefix="CHG", depends_on=())],
        existing_ids=used_ids,
    )
    return mapping[request_key]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    _atomic_write_bytes(path, canonical_json_text(document).encode("utf-8"))


def ensure_created_event_locked(
    paths: ProjectPaths,
    event: Mapping[str, object],
) -> None:
    """在事件锁内原子追加整行，同时保留已有事件原始字节。"""

    expected = dict(event)
    _validate_event_shape(paths, expected)
    events = load_events(paths)
    same_id = [item for item in events if item.get("event_id") == expected.get("event_id")]
    if same_id:
        if len(same_id) != 1 or same_id[0] != expected:
            raise SdlcError(f"创建事件编号冲突：{expected.get('event_id')}。")
        return
    payload = expected["payload"]
    if not isinstance(payload, Mapping):
        raise SdlcError("待追加创建事件缺少 payload。")
    for existing in created_events(events):
        existing_payload = _validate_event_shape(paths, existing)
        if (
            existing_payload.get("change_id") == payload.get("change_id")
            or (
                existing_payload.get("requirement_id") == payload.get("requirement_id")
                and existing_payload.get("request_key") == payload.get("request_key")
            )
        ):
            raise SdlcError("待追加创建事件与已有 CHG 或幂等身份冲突。")
    existing_content = paths.events_file.read_bytes() if paths.events_file.exists() else b""
    separator = b"\n" if existing_content and not existing_content.endswith(b"\n") else b""
    new_line = (
        json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(paths.events_file, existing_content + separator + new_line)


def build_transaction(
    *,
    requirement_id: str,
    request_key: str,
    change_id: str,
    workspace_path: str,
    staging_path: str,
    status_sha256: str,
    event: Mapping[str, object],
    transaction_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": CHANGE_TRANSACTION_SCHEMA,
        "transaction_id": transaction_id or uuid.uuid4().hex,
        "requirement_id": requirement_id,
        "request_key": request_key,
        "change_id": change_id,
        "workspace_path": workspace_path,
        "staging_path": staging_path,
        "status_sha256": status_sha256,
        "event": dict(event),
    }


def _validate_transaction(paths: ProjectPaths, document: Mapping[str, object]) -> None:
    if set(document) != TRANSACTION_FIELDS:
        raise SdlcError("变更创建事务日志字段不完整或包含额外字段。")
    if (
        document.get("schema_version") != CHANGE_TRANSACTION_SCHEMA
        or not isinstance(document.get("transaction_id"), str)
        or not TRANSACTION_ID_PATTERN.fullmatch(str(document.get("transaction_id")))
        or not isinstance(document.get("requirement_id"), str)
        or not REQUIREMENT_ID_PATTERN.fullmatch(str(document.get("requirement_id")))
        or not isinstance(document.get("request_key"), str)
        or not CLIENT_KEY_PATTERN.fullmatch(str(document.get("request_key")))
        or not isinstance(document.get("change_id"), str)
        or not CHANGE_ID_PATTERN.fullmatch(str(document.get("change_id")))
        or not isinstance(document.get("workspace_path"), str)
        or not isinstance(document.get("staging_path"), str)
        or not isinstance(document.get("status_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(document.get("status_sha256")))
        or not isinstance(document.get("event"), dict)
    ):
        raise SdlcError("变更创建事务日志身份、路径或哈希格式不正确。")
    event = document["event"]
    if not isinstance(event, dict):
        raise SdlcError("变更创建事务日志缺少完整事件。")
    payload = _validate_event_shape(paths, event)
    for field in ("requirement_id", "request_key", "change_id", "workspace_path", "status_sha256"):
        if document.get(field) != payload.get(field):
            raise SdlcError(f"变更创建事务日志的 {field} 与事件不一致。")


def write_transaction_journal(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
) -> Path:
    _validate_transaction(paths, transaction)
    transaction_id = str(transaction["transaction_id"])
    journal = paths.change_transactions_dir / f"{transaction_id}.json"
    _atomic_write_json(journal, transaction)
    return journal


def write_staged_workspace(
    paths: ProjectPaths,
    staging: Path,
    status_content: bytes,
) -> None:
    if staging.exists() or staging.is_symlink():
        raise SdlcError("变更工作区暂存目录已存在，不能覆盖。")
    staging.mkdir(parents=True)
    try:
        status_path = staging / "status.json"
        with status_path.open("xb") as handle:
            handle.write(status_content)
            handle.flush()
            os.fsync(handle.fileno())
        for directory_name in ("原始资料", "reviews"):
            directory = staging / directory_name
            directory.mkdir()
            _fsync_directory(directory)
        _fsync_directory(staging)
        _fsync_directory(staging.parent)
    except Exception:
        # 日志已经先落盘；这里只保留可被下一次精确恢复识别的暂存路径。
        raise


def publish_workspace(staging: Path, workspace: Path) -> None:
    if workspace.exists() or workspace.is_symlink():
        raise SdlcError(f"CHG 工作区已经存在但没有可用幂等登记：{workspace.name}。")
    os.rename(staging, workspace)
    _fsync_directory(workspace.parent)


def cleanup_transaction(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    journal: Path,
) -> None:
    staging_path = resolve_project_path(paths.root, str(transaction.get("staging_path") or ""))
    try:
        staging_path.relative_to(paths.change_staging_root)
    except ValueError as exc:
        raise SdlcError("事务暂存路径不属于变更创建专用目录。") from exc
    if staging_path.exists() or staging_path.is_symlink():
        if staging_path.is_symlink() or not staging_path.is_dir():
            raise SdlcError("事务暂存路径不是当前任务拥有的普通目录。")
        shutil.rmtree(staging_path)
    journal.unlink(missing_ok=True)
    _fsync_directory(paths.change_transactions_dir)


def recover_change_transactions_locked(paths: ProjectPaths) -> list[str]:
    """持有项目锁和事件锁时恢复；改名前清理，改名后补成同一个成功结果。"""

    if paths.change_transactions_dir.is_symlink() or not paths.change_transactions_dir.is_dir():
        raise SdlcError("变更创建事务目录不是项目内普通目录。")
    paths.change_staging_root.mkdir(parents=True, exist_ok=True)
    if paths.change_staging_root.is_symlink() or not paths.change_staging_root.is_dir():
        raise SdlcError("变更创建暂存根目录不是项目内普通目录。")
    recovered: list[str] = []
    for journal in sorted(paths.change_transactions_dir.glob("*.json")):
        if journal.is_symlink() or not journal.is_file():
            raise SdlcError(f"变更创建事务日志不是普通文件：{journal.name}。")
        transaction = _read_json(journal, label="变更创建事务日志")
        _validate_transaction(paths, transaction)
        if journal.name != f"{transaction['transaction_id']}.json":
            raise SdlcError(f"变更创建事务日志文件名与事务编号不一致：{journal.name}。")

        workspace = resolve_project_path(paths.root, str(transaction["workspace_path"]))
        staging = resolve_project_path(paths.root, str(transaction["staging_path"]))
        try:
            staging.relative_to(paths.change_staging_root)
        except ValueError as exc:
            raise SdlcError("变更创建事务暂存路径越过专用目录。") from exc
        event = transaction["event"]
        if not isinstance(event, dict):
            raise SdlcError("变更创建事务缺少完整事件。")
        existing_events = load_events(paths)
        matching_event_ids = [
            item for item in existing_events if item.get("event_id") == event.get("event_id")
        ]

        if workspace.exists() or workspace.is_symlink():
            if workspace.is_symlink() or not workspace.is_dir():
                raise SdlcError(f"待恢复 CHG 路径不是普通目录：{transaction['workspace_path']}。")
            if staging.exists() or staging.is_symlink():
                raise SdlcError("变更创建事务同时存在正式目录和暂存目录，无法确定提交边界。")
            status_path = workspace / "status.json"
            if not status_path.is_file() or status_path.is_symlink():
                raise SdlcError("待恢复变更工作区缺少普通 status.json。")
            if not hmac.compare_digest(
                sha256_file(status_path), str(transaction["status_sha256"])
            ):
                raise SdlcError("待恢复变更工作区的 status.json 哈希与事务日志不一致。")
            if matching_event_ids and (
                len(matching_event_ids) != 1 or matching_event_ids[0] != event
            ):
                raise SdlcError("待恢复变更工作区的创建事件与事务日志冲突。")
            ensure_created_event_locked(paths, event)
            verify_workspace_event(
                paths,
                workspace,
                event,
                verify_current_bases=False,
                verify_initial_layout=True,
            )
            recovered.append(str(transaction["change_id"]))
            cleanup_transaction(paths, transaction, journal)
            continue

        if matching_event_ids:
            raise SdlcError("变更创建事件已经存在，但事务目标工作区缺失。")
        # 改名前没有公开工作区和事件，编号尚未占用；只删除日志明确指向的暂存目录。
        cleanup_transaction(paths, transaction, journal)
    return recovered


def environment_interruption_hook() -> InterruptionHook:
    requested_stage = os.environ.get(CHANGE_INTERRUPT_ENV, "")
    valid_stages = {
        "",
        INTERRUPT_BEFORE_DIRECTORY_PUBLISH,
        INTERRUPT_AFTER_DIRECTORY_PUBLISH,
        INTERRUPT_AFTER_EVENT_APPEND,
    }
    if requested_stage not in valid_stages:
        raise SdlcError(
            f"{CHANGE_INTERRUPT_ENV} 的故障注入点不受支持：{requested_stage}。"
        )

    def interrupt(stage: str) -> None:
        if requested_stage and stage == requested_stage:
            raise SdlcError(f"已在变更创建故障注入点中断：{stage}。")

    return interrupt


def new_event_id(events: Iterable[Mapping[str, object]]) -> str:
    return next_event_id([dict(event) for event in events])


def status_sha256(content: bytes) -> str:
    return sha256_bytes(content)


CHANGE_MATERIAL_MANIFEST_SCHEMA = "change-material-manifest.v1"
CHANGE_MATERIAL_TRANSACTION_SCHEMA = "change-material-transaction.v1"
CHANGE_MATERIAL_EVENT_TYPE = "change_material_added"
CHANGE_MATERIAL_EVENT_SOURCE = "sdlc-change-material"
CHANGE_MATERIAL_INTERRUPT_ENV = "CODEX_SDLC_CHANGE_MATERIAL_INTERRUPT"

INTERRUPT_AFTER_MATERIAL_PUBLISH = "after_material_publish"
INTERRUPT_AFTER_MANIFEST_PUBLISH = "after_manifest_publish"
INTERRUPT_AFTER_MATERIAL_EVENT_APPEND = "after_event_append"

MATERIAL_TYPE_VALUES = (
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

CHANGE_MATERIAL_EVENT_PAYLOAD_FIELDS = {
    "requirement_id",
    "change_id",
    "workspace_path",
    "material_id",
    "identity_sha256",
    "manifest_path",
    "manifest_sha256",
}

CHANGE_MATERIAL_TRANSACTION_FIELDS = {
    "schema_version",
    "transaction_id",
    "requirement_id",
    "change_id",
    "workspace_path",
    "material_id",
    "identity_sha256",
    "manifest_path",
    "manifest",
    "manifest_sha256",
    "previous_manifest_sha256",
    "stored_path",
    "stored_sha256",
    "staging_material_path",
    "staging_manifest_path",
    "event",
}


@dataclass(frozen=True)
class PreparedChangeMaterial:
    """保存已经按来源原值核对过的资料项和可选普通文件字节。"""

    material: dict[str, object]
    content: bytes | None


@dataclass(frozen=True)
class ChangeMaterialResult:
    requirement_id: str
    change_id: str
    workspace_path: str
    material_id: str
    source_kind: str
    status: str
    event_id: str
    duplicate: bool


def _decode_json_bytes(content: bytes, *, label: str) -> dict[str, object]:
    """拒绝重复字段和非标准数字，避免同一份输入出现两个结构解释。"""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SdlcError(f"{label}包含重复字段：{key}。")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise SdlcError(f"{label}包含非标准数字：{value}。")

    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except SdlcError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效 JSON。") from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是对象。")
    return document


def resolve_project_input_file(
    paths: ProjectPaths,
    raw_path: object,
    *,
    label: str,
) -> tuple[Path, str, bytes]:
    """按用户给出的词法路径逐级拒绝符号链接，再读取完整原始字节。"""

    raw = str(raw_path or "")
    if not raw.strip() or "\x00" in raw:
        raise SdlcError(f"{label}路径不能为空。")
    requested = Path(raw).expanduser()
    if ".." in requested.parts:
        raise SdlcError(f"{label}路径不能包含上级目录。")
    root = paths.root.resolve(strict=True)
    lexical = requested if requested.is_absolute() else root / requested
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise SdlcError(f"{label}路径越过项目目录。") from exc
    if not relative.parts:
        raise SdlcError(f"{label}必须是普通文件。")

    current = root
    metadata: os.stat_result | None = None
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise SdlcError(f"{label}不存在或无法读取。") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise SdlcError(f"{label}不能经过符号链接。")
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        raise SdlcError(f"{label}必须是普通文件。")
    try:
        content = lexical.read_bytes()
        after = lexical.lstat()
    except OSError as exc:
        raise SdlcError(f"{label}无法读取。") from exc
    if (
        not stat.S_ISREG(after.st_mode)
        or (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise SdlcError(f"{label}在读取过程中发生变化。")
    return lexical, relative.as_posix(), content


def resolve_registered_change_workspace(
    paths: ProjectPaths,
    events: Iterable[Mapping[str, object]],
    *,
    requirement_id: object,
    change_id: object,
) -> tuple[Path, Mapping[str, object], dict[str, object]]:
    """先按创建事件找 CHG，再核对它确实归当前 REQ 所有。"""

    clean_requirement_id = _validate_requirement_id(requirement_id)
    if not isinstance(change_id, str) or not CHANGE_ID_PATTERN.fullmatch(change_id):
        raise SdlcError("变更编号格式不正确，必须是 CHG-数字。")
    event_list = list(events)
    resolve_formal_requirement_dir(paths, clean_requirement_id, event_list)
    _, creation_events = validate_registered_workspaces(paths, event_list)
    matches: list[Mapping[str, object]] = []
    for event in creation_events:
        payload = event.get("payload")
        if isinstance(payload, Mapping) and payload.get("change_id") == change_id:
            matches.append(event)
    if len(matches) != 1:
        raise SdlcError(f"没有找到唯一登记的变更工作区 `{change_id}`。")
    payload = matches[0].get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError(f"{change_id} 创建事件缺少结构化 payload。")
    if payload.get("requirement_id") != clean_requirement_id:
        raise SdlcError(f"{change_id} 不属于需求 {clean_requirement_id}。")
    workspace = paths.root / str(payload.get("workspace_path") or "")
    status = verify_workspace_event(
        paths,
        workspace,
        matches[0],
        verify_current_bases=True,
    )
    return workspace, matches[0], status


def change_material_manifest_path(workspace: Path) -> Path:
    return workspace / "change-material-manifest.v1.json"


def empty_change_material_manifest(status: Mapping[str, object]) -> dict[str, object]:
    document = {
        "schema_version": CHANGE_MATERIAL_MANIFEST_SCHEMA,
        "requirement_id": status["requirement_id"],
        "change_id": status["change_id"],
        "workspace_path": status["workspace_path"],
        "materials": [],
    }
    validate_schema_document(document, schema_name=CHANGE_MATERIAL_MANIFEST_SCHEMA)
    return document


def change_material_manifest_bytes(document: Mapping[str, object]) -> bytes:
    """变更资料清单使用无结尾换行的规范 JSON，和任务固定字节完全一致。"""

    validate_schema_document(document, schema_name=CHANGE_MATERIAL_MANIFEST_SCHEMA)
    return canonical_json_bytes(document)


def load_change_material_manifest(
    workspace: Path,
    status: Mapping[str, object],
) -> dict[str, object]:
    path = change_material_manifest_path(workspace)
    if not path.exists() and not path.is_symlink():
        return empty_change_material_manifest(status)
    if path.is_symlink() or not path.is_file():
        raise SdlcError("变更资料清单必须是工作区内普通文件。")
    document = _read_json(path, label="变更资料清单")
    validate_schema_document(document, schema_name=CHANGE_MATERIAL_MANIFEST_SCHEMA)
    expected = {
        "requirement_id": status.get("requirement_id"),
        "change_id": status.get("change_id"),
        "workspace_path": status.get("workspace_path"),
    }
    if any(document.get(key) != value for key, value in expected.items()):
        raise SdlcError("变更资料清单与当前工作区所有权不一致。")
    if path.read_bytes() != canonical_json_bytes(document):
        raise SdlcError("变更资料清单不是规定的规范 JSON 字节。")
    return document


def change_material_identity_document(material: Mapping[str, object]) -> dict[str, object]:
    source_kind = material.get("source_kind")
    material_type = material.get("type")
    if source_kind == "file":
        source = {
            "source_path": material.get("source_path"),
            "sha256": material.get("sha256"),
        }
    elif source_kind == "external-reference":
        source = {
            "normalized_url_sha256": material.get("normalized_url_sha256"),
            "version_evidence_sha256": material.get("version_evidence_sha256"),
        }
    elif source_kind == "secret-reference":
        source = {
            "secret_reference_sha256": material.get("secret_reference_sha256"),
        }
    else:
        raise SdlcError("变更资料来源类型不受支持。")
    return {
        "source_kind": source_kind,
        "type": material_type,
        "source": source,
    }


def _validate_material_identity(material: Mapping[str, object]) -> None:
    identity = canonical_sha256(change_material_identity_document(material))
    if material.get("identity_sha256") != identity:
        raise SdlcError(f"{material.get('material_id')} 的资料身份哈希不一致。")


def _validate_material_id_sequence(materials: list[object]) -> None:
    for index, item in enumerate(materials, start=1):
        if not isinstance(item, dict):
            raise SdlcError("变更资料清单项必须是对象。")
        expected = f"CMAT-{index:03d}"
        if item.get("material_id") != expected:
            raise SdlcError("变更资料清单必须按连续 CMAT 编号追加。")


def _validate_file_material(workspace: Path, material: Mapping[str, object]) -> None:
    material_id = str(material.get("material_id") or "")
    if material.get("stored_path") != f"原始资料/{material_id}":
        raise SdlcError(f"{material_id} 的归档路径不正确。")
    source_path = Path(str(material.get("source_path") or ""))
    if source_path.is_absolute() or ".." in source_path.parts or not source_path.parts:
        raise SdlcError(f"{material_id} 的来源路径不是项目相对路径。")
    stored = workspace / str(material["stored_path"])
    _assert_no_symlink_components(workspace, stored, label=f"{material_id} 归档文件")
    if stored.is_symlink() or not stored.is_file():
        raise SdlcError(f"{material_id} 的归档文件缺失。")
    if sha256_file(stored) != material.get("sha256"):
        raise SdlcError(f"{material_id} 的归档文件哈希不一致。")
    if stored.stat().st_size != material.get("size_bytes"):
        raise SdlcError(f"{material_id} 的归档文件大小不一致。")


def _validate_external_material(material: Mapping[str, object]) -> None:
    raw_url = str(material.get("url") or "")
    if material.get("normalized_url_sha256") != normalized_url_sha256(raw_url):
        raise SdlcError(f"{material.get('material_id')} 的外部地址哈希不一致。")
    evidence = material.get("version_evidence")
    validate_schema_document(evidence, schema_name="external-version-evidence.v1")
    if not isinstance(evidence, dict) or evidence.get("normalized_url_sha256") != material.get(
        "normalized_url_sha256"
    ):
        raise SdlcError(f"{material.get('material_id')} 的版本证据没有绑定当前地址。")
    if evidence.get("status") == "confirmed" and material.get("status") != "confirmed":
        raise SdlcError(f"{material.get('material_id')} 的外部资料状态不正确。")
    if evidence.get("status") == "unversioned" and material.get("status") != "blocked":
        raise SdlcError(f"{material.get('material_id')} 缺少版本证据时必须保持 blocked。")


def _validate_secret_material(material: Mapping[str, object]) -> None:
    reference = material.get("secret_reference")
    validate_schema_document(reference, schema_name="secret-reference.v1")
    if material.get("secret_reference_sha256") != canonical_sha256(reference):
        raise SdlcError(f"{material.get('material_id')} 的秘密引用规范哈希不一致。")


def _validate_change_material_event_shape(
    paths: ProjectPaths,
    event: Mapping[str, object],
) -> dict[str, object]:
    if set(event) != EVENT_FIELDS:
        raise SdlcError("change_material_added 事件字段不完整或包含额外字段。")
    payload = event.get("payload")
    if not isinstance(payload, dict) or set(payload) != CHANGE_MATERIAL_EVENT_PAYLOAD_FIELDS:
        raise SdlcError("change_material_added 事件 payload 字段不完整或包含额外字段。")
    if (
        not isinstance(event.get("event_id"), str)
        or not EVENT_ID_PATTERN.fullmatch(str(event.get("event_id")))
        or event.get("event_type") != CHANGE_MATERIAL_EVENT_TYPE
        or event.get("project_path") != str(paths.root)
        or event.get("requirement_id") != payload.get("requirement_id")
        or event.get("task_id") is not None
        or not isinstance(event.get("created_at"), str)
        or not str(event.get("created_at")).strip()
        or event.get("source") != CHANGE_MATERIAL_EVENT_SOURCE
        or event.get("summary") != f"归档变更资料 {payload.get('material_id')}"
    ):
        raise SdlcError("change_material_added 事件固定字段不正确。")
    if (
        not isinstance(payload.get("requirement_id"), str)
        or not REQUIREMENT_ID_PATTERN.fullmatch(str(payload.get("requirement_id")))
        or not isinstance(payload.get("change_id"), str)
        or not CHANGE_ID_PATTERN.fullmatch(str(payload.get("change_id")))
        or not isinstance(payload.get("workspace_path"), str)
        or not isinstance(payload.get("material_id"), str)
        or not re.fullmatch(r"CMAT-[0-9]{3,}", str(payload.get("material_id")))
        or not isinstance(payload.get("identity_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(payload.get("identity_sha256")))
        or not isinstance(payload.get("manifest_path"), str)
        or not isinstance(payload.get("manifest_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(payload.get("manifest_sha256")))
    ):
        raise SdlcError("change_material_added 事件身份、路径或哈希格式不正确。")
    return payload


def _events_for_material_workspace(
    events: Iterable[Mapping[str, object]],
    *,
    change_id: str,
    workspace_path: str,
) -> list[Mapping[str, object]]:
    result: list[Mapping[str, object]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("event_type") != CHANGE_MATERIAL_EVENT_TYPE:
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and (
            payload.get("change_id") == change_id
            or payload.get("workspace_path") == workspace_path
        ):
            result.append(event)
    return result


def manifest_prefix_document(
    manifest: Mapping[str, object],
    material_count: int,
) -> dict[str, object]:
    materials = manifest.get("materials")
    if not isinstance(materials, list) or material_count < 0 or material_count > len(materials):
        raise SdlcError("变更资料清单前缀范围不正确。")
    return {
        "schema_version": manifest["schema_version"],
        "requirement_id": manifest["requirement_id"],
        "change_id": manifest["change_id"],
        "workspace_path": manifest["workspace_path"],
        "materials": materials[:material_count],
    }


def verify_change_material_state(
    paths: ProjectPaths,
    workspace: Path,
    status: Mapping[str, object],
    manifest: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
) -> None:
    """每次从第一项重算全部资料身份、归档字节和事件清单前缀。"""

    validate_schema_document(manifest, schema_name=CHANGE_MATERIAL_MANIFEST_SCHEMA)
    expected_owner = {
        "requirement_id": status.get("requirement_id"),
        "change_id": status.get("change_id"),
        "workspace_path": status.get("workspace_path"),
    }
    if any(manifest.get(key) != value for key, value in expected_owner.items()):
        raise SdlcError("变更资料清单与当前工作区所有权不一致。")
    materials = manifest.get("materials")
    if not isinstance(materials, list):
        raise SdlcError("变更资料清单 materials 必须是数组。")
    _validate_material_id_sequence(materials)
    identities: set[str] = set()
    event_ids: set[str] = set()
    for item in materials:
        if not isinstance(item, dict):
            raise SdlcError("变更资料清单项必须是对象。")
        _validate_material_identity(item)
        identity = str(item["identity_sha256"])
        event_id = str(item["event_id"])
        if identity in identities:
            raise SdlcError("变更资料清单包含重复资料身份。")
        if event_id in event_ids:
            raise SdlcError("变更资料清单包含重复事件编号。")
        identities.add(identity)
        event_ids.add(event_id)
        if item.get("source_kind") == "file":
            _validate_file_material(workspace, item)
        elif item.get("source_kind") == "external-reference":
            _validate_external_material(item)
        elif item.get("source_kind") == "secret-reference":
            _validate_secret_material(item)

    material_events = _events_for_material_workspace(
        events,
        change_id=str(status["change_id"]),
        workspace_path=str(status["workspace_path"]),
    )
    if len(material_events) != len(materials):
        raise SdlcError("变更资料清单与 change_material_added 事件数量不一致。")
    by_id: dict[str, Mapping[str, object]] = {}
    for event in material_events:
        payload = _validate_change_material_event_shape(paths, event)
        event_id = str(event["event_id"])
        if event_id in by_id:
            raise SdlcError("同一变更资料事件编号出现多次。")
        by_id[event_id] = event
        if payload.get("requirement_id") != status.get("requirement_id"):
            raise SdlcError("change_material_added 事件跨需求引用。")

    manifest_path = _project_relative(paths, change_material_manifest_path(workspace))
    for index, item in enumerate(materials, start=1):
        if not isinstance(item, dict):
            raise SdlcError("变更资料清单项必须是对象。")
        event = by_id.get(str(item["event_id"]))
        if event is None:
            raise SdlcError(f"{item['material_id']} 缺少唯一 change_material_added 事件。")
        payload = _validate_change_material_event_shape(paths, event)
        prefix_sha256 = sha256_bytes(
            canonical_json_bytes(manifest_prefix_document(manifest, index))
        )
        expected_payload = {
            "requirement_id": status["requirement_id"],
            "change_id": status["change_id"],
            "workspace_path": status["workspace_path"],
            "material_id": item["material_id"],
            "identity_sha256": item["identity_sha256"],
            "manifest_path": manifest_path,
            "manifest_sha256": prefix_sha256,
        }
        if payload != expected_payload:
            raise SdlcError(f"{item['material_id']} 的事件与清单前缀不一致。")
    if materials:
        latest = materials[-1]
        if not isinstance(latest, dict):
            raise SdlcError("最后一项变更资料不是对象。")
        latest_event = by_id[str(latest["event_id"])]
        latest_payload = _validate_change_material_event_shape(paths, latest_event)
        if latest_payload.get("manifest_sha256") != sha256_bytes(
            canonical_json_bytes(manifest)
        ):
            raise SdlcError("最新变更资料事件没有绑定完整清单哈希。")


def prepare_change_material(
    paths: ProjectPaths,
    *,
    material_type: str,
    file_path: str = "",
    url: str = "",
    version_evidence_path: str = "",
    secret_reference_path: str = "",
) -> PreparedChangeMaterial:
    if material_type not in MATERIAL_TYPE_VALUES:
        raise SdlcError("变更资料类型不受支持。")
    source_count = sum(bool(value) for value in (file_path, url, secret_reference_path))
    if source_count != 1:
        raise SdlcError("资料来源必须在 --file、--url 和 --secret-reference 中选择一个。")
    if version_evidence_path and not url:
        raise SdlcError("外部版本证据只能和 --url 一起使用。")

    if file_path:
        source, source_path, content = resolve_project_input_file(
            paths, file_path, label="原始资料"
        )
        digest = sha256_bytes(content)
        material: dict[str, object] = {
            "source_kind": "file",
            "type": material_type,
            "status": "active",
            "source_path": source_path,
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": mimetypes.guess_type(source.name)[0]
            or "application/octet-stream",
        }
        material["identity_sha256"] = canonical_sha256(
            change_material_identity_document(material)
        )
        return PreparedChangeMaterial(material=material, content=content)

    if url:
        raw_url = url
        normalize_external_url(raw_url)
        url_sha256 = normalized_url_sha256(raw_url)
        if version_evidence_path:
            _, _, evidence_bytes = resolve_project_input_file(
                paths, version_evidence_path, label="外部版本证据"
            )
            evidence = _decode_json_bytes(evidence_bytes, label="外部版本证据")
            validate_schema_document(evidence, schema_name="external-version-evidence.v1")
            if evidence.get("normalized_url_sha256") != url_sha256:
                raise SdlcError("外部版本证据绑定的 URL 与当前资料不一致。")
            if evidence.get("status") != "confirmed":
                raise SdlcError("外部版本证据没有确认稳定版本。")
            status_value = "confirmed"
        else:
            evidence = unversioned_evidence(raw_url)
            evidence_bytes = canonical_json_bytes(evidence)
            status_value = "blocked"
        material = {
            "source_kind": "external-reference",
            "type": material_type,
            "status": status_value,
            "url": raw_url,
            "normalized_url_sha256": url_sha256,
            "version_evidence": evidence,
            "version_evidence_sha256": sha256_bytes(evidence_bytes),
        }
        material["identity_sha256"] = canonical_sha256(
            change_material_identity_document(material)
        )
        return PreparedChangeMaterial(material=material, content=None)

    _, _, reference_bytes = resolve_project_input_file(
        paths, secret_reference_path, label="秘密引用"
    )
    reference = _decode_json_bytes(reference_bytes, label="秘密引用")
    validate_schema_document(reference, schema_name="secret-reference.v1")
    reference_sha256 = canonical_sha256(reference)
    material = {
        "source_kind": "secret-reference",
        "type": material_type,
        "status": "active",
        "secret_reference": reference,
        "secret_reference_sha256": reference_sha256,
    }
    material["identity_sha256"] = canonical_sha256(
        change_material_identity_document(material)
    )
    return PreparedChangeMaterial(material=material, content=None)


def build_change_material_event(
    paths: ProjectPaths,
    *,
    event_id: str,
    status: Mapping[str, object],
    material: Mapping[str, object],
    manifest_sha256: str,
    workspace: Path,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": CHANGE_MATERIAL_EVENT_TYPE,
        "project_path": str(paths.root),
        "requirement_id": status["requirement_id"],
        "task_id": None,
        "created_at": now_iso(),
        "source": CHANGE_MATERIAL_EVENT_SOURCE,
        "summary": f"归档变更资料 {material['material_id']}",
        "payload": {
            "requirement_id": status["requirement_id"],
            "change_id": status["change_id"],
            "workspace_path": status["workspace_path"],
            "material_id": material["material_id"],
            "identity_sha256": material["identity_sha256"],
            "manifest_path": _project_relative(
                paths, change_material_manifest_path(workspace)
            ),
            "manifest_sha256": manifest_sha256,
        },
    }


def ensure_change_material_event_locked(
    paths: ProjectPaths,
    event: Mapping[str, object],
) -> None:
    expected = dict(event)
    payload = _validate_change_material_event_shape(paths, expected)
    events = load_events(paths)
    same_id = [item for item in events if item.get("event_id") == expected.get("event_id")]
    if same_id:
        if len(same_id) != 1 or same_id[0] != expected:
            raise SdlcError(f"变更资料事件编号冲突：{expected.get('event_id')}。")
        return
    for existing in _events_for_material_workspace(
        events,
        change_id=str(payload["change_id"]),
        workspace_path=str(payload["workspace_path"]),
    ):
        existing_payload = _validate_change_material_event_shape(paths, existing)
        if (
            existing_payload.get("material_id") == payload.get("material_id")
            or existing_payload.get("identity_sha256") == payload.get("identity_sha256")
        ):
            raise SdlcError("待追加资料事件与已有 CMAT 或资料身份冲突。")
    existing_content = paths.events_file.read_bytes() if paths.events_file.exists() else b""
    separator = b"\n" if existing_content and not existing_content.endswith(b"\n") else b""
    line = (
        json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(paths.events_file, existing_content + separator + line)


def build_change_material_transaction(
    *,
    status: Mapping[str, object],
    workspace: Path,
    manifest: Mapping[str, object],
    previous_manifest_sha256: str | None,
    material: Mapping[str, object],
    event: Mapping[str, object],
    transaction_id: str | None = None,
) -> dict[str, object]:
    clean_transaction_id = transaction_id or uuid.uuid4().hex
    material_id = str(material["material_id"])
    stored_path = material.get("stored_path")
    staging_material = (
        f".material-staging/{clean_transaction_id}.material"
        if stored_path is not None
        else None
    )
    return {
        "schema_version": CHANGE_MATERIAL_TRANSACTION_SCHEMA,
        "transaction_id": clean_transaction_id,
        "requirement_id": status["requirement_id"],
        "change_id": status["change_id"],
        "workspace_path": status["workspace_path"],
        "material_id": material_id,
        "identity_sha256": material["identity_sha256"],
        "manifest_path": "change-material-manifest.v1.json",
        "manifest": dict(manifest),
        "manifest_sha256": sha256_bytes(canonical_json_bytes(manifest)),
        "previous_manifest_sha256": previous_manifest_sha256,
        "stored_path": stored_path,
        "stored_sha256": material.get("sha256"),
        "staging_material_path": staging_material,
        "staging_manifest_path": f".material-staging/{clean_transaction_id}.manifest",
        "event": dict(event),
    }


def _resolve_workspace_relative(workspace: Path, raw_path: object, *, label: str) -> Path:
    value = str(raw_path or "")
    requested = Path(value)
    if requested.is_absolute() or ".." in requested.parts or not requested.parts:
        raise SdlcError(f"{label}不是工作区内相对路径。")
    resolved = (workspace / requested).resolve(strict=False)
    try:
        resolved.relative_to(workspace.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"{label}越过变更工作区。") from exc
    return resolved


def _validate_change_material_transaction(
    paths: ProjectPaths,
    workspace: Path,
    transaction: Mapping[str, object],
) -> None:
    if set(transaction) != CHANGE_MATERIAL_TRANSACTION_FIELDS:
        raise SdlcError("变更资料事务日志字段不完整或包含额外字段。")
    if (
        transaction.get("schema_version") != CHANGE_MATERIAL_TRANSACTION_SCHEMA
        or not isinstance(transaction.get("transaction_id"), str)
        or not TRANSACTION_ID_PATTERN.fullmatch(str(transaction.get("transaction_id")))
        or not isinstance(transaction.get("requirement_id"), str)
        or not REQUIREMENT_ID_PATTERN.fullmatch(str(transaction.get("requirement_id")))
        or not isinstance(transaction.get("change_id"), str)
        or not CHANGE_ID_PATTERN.fullmatch(str(transaction.get("change_id")))
        or transaction.get("workspace_path") != _project_relative(paths, workspace)
        or not isinstance(transaction.get("material_id"), str)
        or not re.fullmatch(r"CMAT-[0-9]{3,}", str(transaction.get("material_id")))
        or not isinstance(transaction.get("identity_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(transaction.get("identity_sha256")))
        or transaction.get("manifest_path") != "change-material-manifest.v1.json"
        or not isinstance(transaction.get("manifest"), dict)
        or not isinstance(transaction.get("manifest_sha256"), str)
        or not SHA256_PATTERN.fullmatch(str(transaction.get("manifest_sha256")))
        or transaction.get("previous_manifest_sha256") is not None
        and (
            not isinstance(transaction.get("previous_manifest_sha256"), str)
            or not SHA256_PATTERN.fullmatch(str(transaction.get("previous_manifest_sha256")))
        )
        or transaction.get("stored_path") is not None
        and not isinstance(transaction.get("stored_path"), str)
        or transaction.get("stored_sha256") is not None
        and (
            not isinstance(transaction.get("stored_sha256"), str)
            or not SHA256_PATTERN.fullmatch(str(transaction.get("stored_sha256")))
        )
        or transaction.get("staging_material_path") is not None
        and not isinstance(transaction.get("staging_material_path"), str)
        or not isinstance(transaction.get("staging_manifest_path"), str)
        or not isinstance(transaction.get("event"), dict)
    ):
        raise SdlcError("变更资料事务日志身份、路径或哈希格式不正确。")
    manifest = transaction["manifest"]
    if not isinstance(manifest, dict):
        raise SdlcError("变更资料事务日志缺少完整清单。")
    validate_schema_document(manifest, schema_name=CHANGE_MATERIAL_MANIFEST_SCHEMA)
    if sha256_bytes(canonical_json_bytes(manifest)) != transaction.get("manifest_sha256"):
        raise SdlcError("变更资料事务日志的清单哈希不一致。")
    if any(
        manifest.get(key) != transaction.get(key)
        for key in ("requirement_id", "change_id", "workspace_path")
    ):
        raise SdlcError("变更资料事务日志的清单跨工作区引用。")
    materials = manifest.get("materials")
    if not isinstance(materials, list) or not materials or not isinstance(materials[-1], dict):
        raise SdlcError("变更资料事务日志的清单没有待提交资料。")
    material = materials[-1]
    if (
        material.get("material_id") != transaction.get("material_id")
        or material.get("identity_sha256") != transaction.get("identity_sha256")
        or material.get("stored_path") != transaction.get("stored_path")
        or material.get("sha256") != transaction.get("stored_sha256")
    ):
        raise SdlcError("变更资料事务日志与待提交清单项不一致。")
    transaction_id = str(transaction["transaction_id"])
    if transaction.get("staging_manifest_path") != (
        f".material-staging/{transaction_id}.manifest"
    ):
        raise SdlcError("变更资料事务日志的清单暂存路径不正确。")
    if material.get("source_kind") == "file":
        if (
            transaction.get("stored_path")
            != f"原始资料/{transaction['material_id']}"
            or transaction.get("staging_material_path")
            != f".material-staging/{transaction_id}.material"
        ):
            raise SdlcError("变更资料事务日志的普通文件路径不正确。")
    elif any(
        transaction.get(key) is not None
        for key in ("stored_path", "stored_sha256", "staging_material_path")
    ):
        raise SdlcError("外部资料和秘密引用不能登记普通文件事务路径。")
    event = transaction["event"]
    if not isinstance(event, dict):
        raise SdlcError("变更资料事务日志缺少完整事件。")
    payload = _validate_change_material_event_shape(paths, event)
    for key in (
        "requirement_id",
        "change_id",
        "workspace_path",
        "material_id",
        "identity_sha256",
        "manifest_sha256",
    ):
        if transaction.get(key) != payload.get(key):
            raise SdlcError(f"变更资料事务日志的 {key} 与事件不一致。")
    if payload.get("manifest_path") != _project_relative(
        paths, change_material_manifest_path(workspace)
    ):
        raise SdlcError("变更资料事务日志的事件清单路径不正确。")
    for raw_path, label in (
        (transaction["manifest_path"], "清单路径"),
        (transaction["staging_manifest_path"], "清单暂存路径"),
    ):
        _resolve_workspace_relative(workspace, raw_path, label=label)
    if transaction.get("stored_path") is not None:
        _resolve_workspace_relative(workspace, transaction["stored_path"], label="资料路径")
        _resolve_workspace_relative(
            workspace,
            transaction["staging_material_path"],
            label="资料暂存路径",
        )


def write_change_material_transaction(
    paths: ProjectPaths,
    workspace: Path,
    transaction: Mapping[str, object],
) -> Path:
    _validate_change_material_transaction(paths, workspace, transaction)
    directory = workspace / ".material-transactions"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise SdlcError("变更资料事务目录不是普通目录。")
    directory.mkdir(exist_ok=True)
    journal = directory / f"{transaction['transaction_id']}.json"
    _atomic_write_json(journal, transaction)
    return journal


def stage_change_material_transaction(
    workspace: Path,
    transaction: Mapping[str, object],
    content: bytes | None,
) -> None:
    staging_directory = workspace / ".material-staging"
    if staging_directory.exists() and (
        staging_directory.is_symlink() or not staging_directory.is_dir()
    ):
        raise SdlcError("变更资料暂存目录不是普通目录。")
    staging_directory.mkdir(exist_ok=True)
    manifest = transaction.get("manifest")
    if not isinstance(manifest, dict):
        raise SdlcError("变更资料事务缺少完整清单。")
    staging_manifest = _resolve_workspace_relative(
        workspace, transaction["staging_manifest_path"], label="清单暂存路径"
    )
    _atomic_write_bytes(staging_manifest, canonical_json_bytes(manifest))
    staging_material_raw = transaction.get("staging_material_path")
    if staging_material_raw is not None:
        if content is None or sha256_bytes(content) != transaction.get("stored_sha256"):
            raise SdlcError("变更资料暂存字节与事务哈希不一致。")
        staging_material = _resolve_workspace_relative(
            workspace, staging_material_raw, label="资料暂存路径"
        )
        _atomic_write_bytes(staging_material, content)


def publish_change_material_file(
    workspace: Path,
    transaction: Mapping[str, object],
) -> None:
    if transaction.get("stored_path") is None:
        return
    staging = _resolve_workspace_relative(
        workspace, transaction["staging_material_path"], label="资料暂存路径"
    )
    target = _resolve_workspace_relative(
        workspace, transaction["stored_path"], label="资料路径"
    )
    if target.exists() or target.is_symlink():
        raise SdlcError(f"待发布资料路径已经存在：{transaction['material_id']}。")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staging, target)
    _fsync_directory(target.parent)
    if sha256_file(target) != transaction.get("stored_sha256"):
        raise SdlcError("发布后的变更资料哈希不一致。")


def publish_change_material_manifest(
    workspace: Path,
    transaction: Mapping[str, object],
) -> None:
    staging = _resolve_workspace_relative(
        workspace, transaction["staging_manifest_path"], label="清单暂存路径"
    )
    target = change_material_manifest_path(workspace)
    os.replace(staging, target)
    _fsync_directory(target.parent)
    if sha256_file(target) != transaction.get("manifest_sha256"):
        raise SdlcError("发布后的变更资料清单哈希不一致。")


def cleanup_change_material_transaction(
    workspace: Path,
    transaction: Mapping[str, object],
    journal: Path,
) -> None:
    for key in ("staging_material_path", "staging_manifest_path"):
        raw_path = transaction.get(key)
        if raw_path is None:
            continue
        path = _resolve_workspace_relative(workspace, raw_path, label="事务暂存路径")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise SdlcError("事务暂存路径不是普通文件。")
            path.unlink()
    journal.unlink(missing_ok=True)
    for directory in (
        workspace / ".material-staging",
        workspace / ".material-transactions",
    ):
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()


def recover_change_material_transactions_locked(
    paths: ProjectPaths,
    workspace: Path,
    status: Mapping[str, object],
) -> list[str]:
    """清单未发布就回到原状态，清单已发布则只补同一事件并完整核对。"""

    directory = workspace / ".material-transactions"
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise SdlcError("变更资料事务目录不是普通目录。")
    recovered: list[str] = []
    for journal in sorted(directory.glob("*.json")):
        if journal.is_symlink() or not journal.is_file():
            raise SdlcError(f"变更资料事务日志不是普通文件：{journal.name}。")
        transaction = _read_json(journal, label="变更资料事务日志")
        _validate_change_material_transaction(paths, workspace, transaction)
        if journal.name != f"{transaction['transaction_id']}.json":
            raise SdlcError("变更资料事务日志文件名与事务编号不一致。")
        if (
            transaction.get("requirement_id") != status.get("requirement_id")
            or transaction.get("change_id") != status.get("change_id")
            or transaction.get("workspace_path") != status.get("workspace_path")
        ):
            raise SdlcError("变更资料事务日志跨工作区引用。")

        manifest_path = change_material_manifest_path(workspace)
        current_manifest_sha256: str | None = None
        if manifest_path.exists() or manifest_path.is_symlink():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise SdlcError("待恢复变更资料清单不是普通文件。")
            current_manifest_sha256 = sha256_file(manifest_path)
        previous_sha256 = transaction.get("previous_manifest_sha256")
        committed_sha256 = transaction.get("manifest_sha256")
        event = transaction.get("event")
        if not isinstance(event, dict):
            raise SdlcError("变更资料事务缺少完整事件。")
        existing_events = load_events(paths)
        same_event_id = [
            item for item in existing_events if item.get("event_id") == event.get("event_id")
        ]

        if current_manifest_sha256 == committed_sha256:
            stored_path = transaction.get("stored_path")
            if stored_path is not None:
                stored = _resolve_workspace_relative(
                    workspace, stored_path, label="资料路径"
                )
                if stored.is_symlink() or not stored.is_file():
                    raise SdlcError("清单已经发布，但对应资料文件缺失。")
                if sha256_file(stored) != transaction.get("stored_sha256"):
                    raise SdlcError("清单已经发布，但对应资料文件哈希不一致。")
            if same_event_id and (
                len(same_event_id) != 1 or same_event_id[0] != event
            ):
                raise SdlcError("待恢复资料事件与事务日志冲突。")
            ensure_change_material_event_locked(paths, event)
            manifest = load_change_material_manifest(workspace, status)
            verify_change_material_state(
                paths, workspace, status, manifest, load_events(paths)
            )
            recovered.append(str(transaction["material_id"]))
            cleanup_change_material_transaction(workspace, transaction, journal)
            continue

        original_state_matches = (
            previous_sha256 is None and current_manifest_sha256 is None
        ) or current_manifest_sha256 == previous_sha256
        if not original_state_matches:
            raise SdlcError("待恢复变更资料清单与事务提交前后哈希都不一致。")
        if same_event_id:
            raise SdlcError("变更资料事件已经存在，但对应清单尚未提交。")
        stored_path = transaction.get("stored_path")
        if stored_path is not None:
            stored = _resolve_workspace_relative(workspace, stored_path, label="资料路径")
            if stored.exists() or stored.is_symlink():
                if stored.is_symlink() or not stored.is_file():
                    raise SdlcError("待回滚资料路径不是普通文件。")
                if sha256_file(stored) != transaction.get("stored_sha256"):
                    raise SdlcError("待回滚资料文件哈希与事务日志不一致。")
                stored.unlink()
                _fsync_directory(stored.parent)
        cleanup_change_material_transaction(workspace, transaction, journal)
    return recovered


def change_material_environment_interruption_hook() -> InterruptionHook:
    requested_stage = os.environ.get(CHANGE_MATERIAL_INTERRUPT_ENV, "")
    valid_stages = {
        "",
        INTERRUPT_AFTER_MATERIAL_PUBLISH,
        INTERRUPT_AFTER_MANIFEST_PUBLISH,
        INTERRUPT_AFTER_MATERIAL_EVENT_APPEND,
    }
    if requested_stage not in valid_stages:
        raise SdlcError(
            f"{CHANGE_MATERIAL_INTERRUPT_ENV} 的故障注入点不受支持：{requested_stage}。"
        )

    def interrupt(stage: str) -> None:
        if requested_stage and stage == requested_stage:
            raise SdlcError(f"已在变更资料故障注入点中断：{stage}。")

    return interrupt


# T-032 预计结果事务使用 CHG 内的独立目录，不能与创建工作区或资料归档事务混用。
CHANGE_PACKAGE_EVENT_TYPE = "change_package_projected"
CHANGE_PACKAGE_EVENT_SOURCE = "sdlc-change-package"
CHANGE_PACKAGE_TRANSACTION_SCHEMA = "change-package-transaction.v1"
CHANGE_PACKAGE_INTERRUPT_ENV = "CODEX_SDLC_CHANGE_PACKAGE_INTERRUPT"
INTERRUPT_BEFORE_PACKAGE_PUBLISH = "before_files_publish"
INTERRUPT_AFTER_PACKAGE_PUBLISH = "after_files_publish"
INTERRUPT_AFTER_PACKAGE_EVENT_APPEND = "after_event_append"


def projection_transaction_dir(workspace: Path) -> Path:
    return workspace / ".projection-transactions"


def projection_staging_dir(workspace: Path) -> Path:
    return workspace / ".projection-staging"


def build_change_package_event(
    paths: ProjectPaths,
    *,
    event_id: str,
    status: Mapping[str, object],
    package_identity_sha256: str,
    material_manifest_sha256: str | None,
    source_files_sha256: Mapping[str, str],
    id_mapping: Mapping[str, str],
    committed_files_sha256: Mapping[str, str],
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": CHANGE_PACKAGE_EVENT_TYPE,
        "project_path": str(paths.root),
        "requirement_id": status["requirement_id"],
        "task_id": None,
        "created_at": now_iso(),
        "source": CHANGE_PACKAGE_EVENT_SOURCE,
        "summary": f"提交完整变更包 {status['change_id']}",
        "payload": {
            "requirement_id": status["requirement_id"],
            "change_id": status["change_id"],
            "workspace_path": status["workspace_path"],
            "package_identity_sha256": package_identity_sha256,
            "status_sha256": sha256_file(
                paths.root / str(status["workspace_path"]) / "status.json"
            ),
            "material_manifest_sha256": material_manifest_sha256,
            "source_files_sha256": dict(source_files_sha256),
            "id_mapping": dict(id_mapping),
            "committed_files_sha256": dict(committed_files_sha256),
        },
    }


def _validate_change_package_event(
    paths: ProjectPaths,
    event: Mapping[str, object],
) -> Mapping[str, object]:
    if set(event) != EVENT_FIELDS:
        raise SdlcError("change_package_projected 事件字段不完整或包含额外字段。")
    payload = event.get("payload")
    expected_payload = {
        "requirement_id",
        "change_id",
        "workspace_path",
        "package_identity_sha256",
        "status_sha256",
        "material_manifest_sha256",
        "source_files_sha256",
        "id_mapping",
        "committed_files_sha256",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_payload:
        raise SdlcError("change_package_projected 事件 payload 不完整或包含额外字段。")
    if (
        event.get("event_type") != CHANGE_PACKAGE_EVENT_TYPE
        or event.get("source") != CHANGE_PACKAGE_EVENT_SOURCE
        or event.get("project_path") != str(paths.root)
        or event.get("requirement_id") != payload.get("requirement_id")
        or event.get("task_id") is not None
        or event.get("summary") != f"提交完整变更包 {payload.get('change_id')}"
        or not isinstance(event.get("event_id"), str)
        or EVENT_ID_PATTERN.fullmatch(str(event.get("event_id"))) is None
        or not isinstance(event.get("created_at"), str)
        or not str(event.get("created_at")).strip()
    ):
        raise SdlcError("change_package_projected 事件固定字段不正确。")
    for field in ("package_identity_sha256", "status_sha256"):
        if not isinstance(payload.get(field), str) or SHA256_PATTERN.fullmatch(str(payload.get(field))) is None:
            raise SdlcError(f"change_package_projected 事件 {field} 不正确。")
    material_hash = payload.get("material_manifest_sha256")
    if material_hash is not None and (
        not isinstance(material_hash, str) or SHA256_PATTERN.fullmatch(material_hash) is None
    ):
        raise SdlcError("change_package_projected 事件资料清单哈希不正确。")
    for field in ("source_files_sha256", "committed_files_sha256"):
        hashes = payload.get(field)
        if not isinstance(hashes, Mapping) or any(
            not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None
            for value in hashes.values()
        ):
            raise SdlcError(f"change_package_projected 事件 {field} 不正确。")
    mapping = payload.get("id_mapping")
    if not isinstance(mapping, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise SdlcError("change_package_projected 事件正式编号映射不正确。")
    return payload


def change_package_events(
    events: Iterable[Mapping[str, object]],
    *,
    workspace_path: str,
) -> list[Mapping[str, object]]:
    return [
        event
        for event in events
        if isinstance(event, Mapping)
        and event.get("event_type") == CHANGE_PACKAGE_EVENT_TYPE
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("workspace_path") == workspace_path
    ]


def ensure_change_package_event_locked(
    paths: ProjectPaths,
    event: Mapping[str, object],
) -> None:
    expected = dict(event)
    payload = _validate_change_package_event(paths, expected)
    events = load_events(paths)
    same_id = [item for item in events if item.get("event_id") == expected.get("event_id")]
    if same_id:
        if len(same_id) != 1 or same_id[0] != expected:
            raise SdlcError(f"变更包成功事件编号冲突：{expected.get('event_id')}。")
        return
    existing = change_package_events(events, workspace_path=str(payload["workspace_path"]))
    if existing:
        if len(existing) != 1 or existing[0] != expected:
            raise SdlcError("同一 CHG 已经登记了不同身份的完整变更包。")
        return
    existing_content = paths.events_file.read_bytes() if paths.events_file.exists() else b""
    separator = b"\n" if existing_content and not existing_content.endswith(b"\n") else b""
    line = (
        json.dumps(expected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(paths.events_file, existing_content + separator + line)


def build_change_package_transaction(
    *,
    status: Mapping[str, object],
    prepared: object,
    event: Mapping[str, object],
    transaction_id: str | None = None,
) -> dict[str, object]:
    clean_id = transaction_id or uuid.uuid4().hex
    # prepared 是 change_contract.PreparedChangePackage；这里只读取稳定字段，避免核心模块循环导入。
    return {
        "schema_version": CHANGE_PACKAGE_TRANSACTION_SCHEMA,
        "transaction_id": clean_id,
        "requirement_id": status["requirement_id"],
        "change_id": status["change_id"],
        "workspace_path": status["workspace_path"],
        "staging_path": f".projection-staging/{clean_id}",
        "package_identity_sha256": getattr(prepared, "package_identity_sha256"),
        "status_sha256": getattr(prepared, "status_sha256"),
        "material_manifest_sha256": getattr(prepared, "material_manifest_sha256"),
        "source_files_sha256": dict(getattr(prepared, "source_files_sha256")),
        "id_mapping": dict(getattr(prepared, "id_mapping")),
        "committed_files_sha256": dict(getattr(prepared, "committed_files_sha256")),
        "event": dict(event),
    }


def _validate_change_package_transaction(
    paths: ProjectPaths,
    workspace: Path,
    transaction: Mapping[str, object],
) -> None:
    fields = {
        "schema_version",
        "transaction_id",
        "requirement_id",
        "change_id",
        "workspace_path",
        "staging_path",
        "package_identity_sha256",
        "status_sha256",
        "material_manifest_sha256",
        "source_files_sha256",
        "id_mapping",
        "committed_files_sha256",
        "event",
    }
    if set(transaction) != fields or transaction.get("schema_version") != CHANGE_PACKAGE_TRANSACTION_SCHEMA:
        raise SdlcError("变更包事务日志字段不完整或包含额外字段。")
    transaction_id = transaction.get("transaction_id")
    if not isinstance(transaction_id, str) or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None:
        raise SdlcError("变更包事务编号不正确。")
    if transaction.get("workspace_path") != _project_relative(paths, workspace):
        raise SdlcError("变更包事务工作区与当前 CHG 不一致。")
    if transaction.get("staging_path") != f".projection-staging/{transaction_id}":
        raise SdlcError("变更包事务暂存路径不正确。")
    event = transaction.get("event")
    if not isinstance(event, Mapping):
        raise SdlcError("变更包事务缺少完整成功事件。")
    payload = _validate_change_package_event(paths, event)
    for field in (
        "requirement_id",
        "change_id",
        "workspace_path",
        "package_identity_sha256",
        "status_sha256",
        "material_manifest_sha256",
        "source_files_sha256",
        "id_mapping",
        "committed_files_sha256",
    ):
        if transaction.get(field) != payload.get(field):
            raise SdlcError(f"变更包事务 {field} 与成功事件不一致。")


def write_change_package_transaction(
    paths: ProjectPaths,
    workspace: Path,
    transaction: Mapping[str, object],
) -> Path:
    _validate_change_package_transaction(paths, workspace, transaction)
    root = projection_transaction_dir(workspace)
    root.mkdir(exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise SdlcError("变更包事务目录必须是 CHG 内普通目录。")
    journal = root / f"{transaction['transaction_id']}.json"
    _atomic_write_json(journal, transaction)
    return journal


def stage_change_package_files(
    workspace: Path,
    transaction: Mapping[str, object],
    committed_file_bytes: Mapping[str, bytes],
) -> Path:
    staging_root = projection_staging_dir(workspace)
    staging_root.mkdir(exist_ok=True)
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise SdlcError("变更包暂存根目录必须是 CHG 内普通目录。")
    staging = workspace / str(transaction["staging_path"])
    if staging.exists() or staging.is_symlink():
        raise SdlcError("变更包事务暂存目录已经存在。")
    staging.mkdir()
    expected_hashes = transaction["committed_files_sha256"]
    if not isinstance(expected_hashes, Mapping) or set(committed_file_bytes) != set(expected_hashes):
        raise SdlcError("变更包暂存文件集合不完整。")
    for name, content in committed_file_bytes.items():
        if "/" in name or sha256_bytes(content) != expected_hashes.get(name):
            raise SdlcError(f"变更包暂存文件哈希不一致：{name}。")
        target = staging / name
        with target.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    _fsync_directory(staging)
    return staging


def publish_change_package_files(
    workspace: Path,
    transaction: Mapping[str, object],
) -> None:
    staging = workspace / str(transaction["staging_path"])
    hashes = transaction.get("committed_files_sha256")
    if not isinstance(hashes, Mapping):
        raise SdlcError("变更包事务缺少目标哈希。")
    if any((workspace / name).exists() or (workspace / name).is_symlink() for name in hashes):
        raise SdlcError("CHG 已经存在未登记或不同身份的变更包文件。")
    for name in sorted(hashes):
        source = staging / name
        if source.is_symlink() or not source.is_file() or sha256_file(source) != hashes[name]:
            raise SdlcError(f"变更包暂存文件缺失或哈希不一致：{name}。")
    for name in sorted(hashes):
        os.replace(staging / name, workspace / name)
    _fsync_directory(workspace)


def cleanup_change_package_transaction(
    workspace: Path,
    transaction: Mapping[str, object],
    journal: Path,
) -> None:
    staging = workspace / str(transaction["staging_path"])
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink() or not staging.is_dir():
            raise SdlcError("变更包暂存路径不是普通目录。")
        shutil.rmtree(staging)
    journal.unlink(missing_ok=True)
    for directory in (projection_staging_dir(workspace), projection_transaction_dir(workspace)):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    _fsync_directory(workspace)


def recover_change_package_transactions_locked(
    paths: ProjectPaths,
    workspace: Path,
) -> list[str]:
    """发布前清理；六份文件发布后只补同一个事件；半套文件直接停止。"""

    root = projection_transaction_dir(workspace)
    if not root.exists() and not root.is_symlink():
        return []
    if root.is_symlink() or not root.is_dir():
        raise SdlcError("变更包事务目录不是普通目录。")
    recovered: list[str] = []
    for journal in sorted(root.glob("*.json")):
        if journal.is_symlink() or not journal.is_file():
            raise SdlcError("变更包事务日志不是普通文件。")
        transaction = _read_json(journal, label="变更包事务日志")
        _validate_change_package_transaction(paths, workspace, transaction)
        if journal.name != f"{transaction['transaction_id']}.json":
            raise SdlcError("变更包事务日志文件名与事务编号不一致。")
        hashes = transaction["committed_files_sha256"]
        if not isinstance(hashes, Mapping):
            raise SdlcError("变更包事务缺少提交文件哈希。")
        existing = [name for name in hashes if (workspace / name).exists() or (workspace / name).is_symlink()]
        event = transaction["event"]
        if not isinstance(event, Mapping):
            raise SdlcError("变更包事务缺少成功事件。")
        matching_events = [
            item for item in load_events(paths) if item.get("event_id") == event.get("event_id")
        ]
        if not existing:
            if matching_events:
                raise SdlcError("变更包成功事件已经存在，但六份提交文件缺失。")
            cleanup_change_package_transaction(workspace, transaction, journal)
            continue
        if set(existing) != set(hashes):
            raise SdlcError("变更包事务只发布了部分文件，不能猜测补写。")
        for name, digest in hashes.items():
            target = workspace / name
            if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
                raise SdlcError(f"已发布变更包文件与事务日志不一致：{name}。")
        if matching_events and (len(matching_events) != 1 or matching_events[0] != event):
            raise SdlcError("已发布变更包的成功事件与事务日志冲突。")
        ensure_change_package_event_locked(paths, event)
        cleanup_change_package_transaction(workspace, transaction, journal)
        recovered.append(str(transaction["change_id"]))
    return recovered


def change_package_environment_interruption_hook() -> InterruptionHook:
    requested = os.environ.get(CHANGE_PACKAGE_INTERRUPT_ENV, "")
    valid = {
        "",
        INTERRUPT_BEFORE_PACKAGE_PUBLISH,
        INTERRUPT_AFTER_PACKAGE_PUBLISH,
        INTERRUPT_AFTER_PACKAGE_EVENT_APPEND,
    }
    if requested not in valid:
        raise SdlcError(f"不支持的变更包故障注入点：{requested}。")

    def interrupt(stage: str) -> None:
        if requested and stage == requested:
            raise SdlcError(f"按环境变量中断变更包事务：{stage}。")

    return interrupt


__all__ = [
    "BASE_VERSION_PATHS",
    "CHANGE_CREATED_EVENT_TYPE",
    "CHANGE_INTERRUPT_ENV",
    "CHANGE_MATERIAL_EVENT_TYPE",
    "CHANGE_MATERIAL_INTERRUPT_ENV",
    "CHANGE_MATERIAL_MANIFEST_SCHEMA",
    "CHANGE_MATERIAL_TRANSACTION_SCHEMA",
    "CHANGE_PACKAGE_EVENT_TYPE",
    "CHANGE_PACKAGE_INTERRUPT_ENV",
    "CHANGE_TRANSACTION_SCHEMA",
    "CHANGE_WORKSPACE_SCHEMA",
    "INTERRUPT_AFTER_DIRECTORY_PUBLISH",
    "INTERRUPT_AFTER_EVENT_APPEND",
    "INTERRUPT_AFTER_MANIFEST_PUBLISH",
    "INTERRUPT_AFTER_MATERIAL_EVENT_APPEND",
    "INTERRUPT_AFTER_MATERIAL_PUBLISH",
    "INTERRUPT_AFTER_PACKAGE_EVENT_APPEND",
    "INTERRUPT_AFTER_PACKAGE_PUBLISH",
    "INTERRUPT_BEFORE_DIRECTORY_PUBLISH",
    "INTERRUPT_BEFORE_PACKAGE_PUBLISH",
    "ChangeMaterialResult",
    "ChangeWorkspaceResult",
    "InterruptionHook",
    "PreparedChangeMaterial",
    "allocate_change_id",
    "build_base_versions",
    "build_change_material_event",
    "build_change_material_transaction",
    "build_change_package_event",
    "build_change_package_transaction",
    "build_created_event",
    "build_status_document",
    "build_transaction",
    "change_material_environment_interruption_hook",
    "change_material_identity_document",
    "change_material_manifest_bytes",
    "change_material_manifest_path",
    "change_package_environment_interruption_hook",
    "change_package_events",
    "cleanup_change_material_transaction",
    "cleanup_change_package_transaction",
    "cleanup_transaction",
    "collect_used_change_ids",
    "created_events",
    "empty_change_material_manifest",
    "ensure_change_material_event_locked",
    "ensure_change_package_event_locked",
    "ensure_created_event_locked",
    "environment_interruption_hook",
    "find_idempotent_event",
    "load_change_material_manifest",
    "load_workspace_status",
    "manifest_prefix_document",
    "new_event_id",
    "prepare_change_material",
    "publish_change_material_file",
    "publish_change_material_manifest",
    "publish_change_package_files",
    "publish_workspace",
    "recover_change_material_transactions_locked",
    "recover_change_package_transactions_locked",
    "recover_change_transactions_locked",
    "resolve_registered_change_workspace",
    "resolve_formal_requirement_dir",
    "resolve_project_input_file",
    "stage_change_material_transaction",
    "stage_change_package_files",
    "status_bytes",
    "status_sha256",
    "validate_registered_workspaces",
    "validate_request_key",
    "verify_change_material_state",
    "verify_workspace_event",
    "write_change_material_transaction",
    "write_change_package_transaction",
    "write_staged_workspace",
    "write_transaction_journal",
]
