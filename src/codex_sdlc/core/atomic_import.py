from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import errno
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import MappingProxyType
from typing import Callable, Iterable, Mapping
import uuid

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import (
    AllocationObject,
    FORMAL_ID_PATTERN,
    TEMPORARY_REFERENCE_PATTERN,
    allocate_stable_ids,
    build_allocation_order,
    rewrite_temporary_references,
)
from codex_sdlc.core.project import (
    ProjectPaths,
    ensure_base_dirs,
    project_lock,
    resolve_project_path,
)
from codex_sdlc.core.state import event_write_lock, load_events, next_event_id, now_iso
from codex_sdlc.core.structured_contract import (
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    contract_sha256,
    sha256_file,
    validate_schema_document,
)


IMPORT_PACKAGE_SCHEMA = "sdlc.atomic-import.v1"
IMPORT_RESULT_SCHEMA = "sdlc.atomic-import-result.v1"
IMPORT_REGISTRY_SCHEMA = "sdlc.atomic-import-registry.v1"
IMPORT_TRANSACTION_SCHEMA = "sdlc.atomic-import-transaction.v1"
IMPORT_RECEIPT_NAME = ".codex-import-receipt.json"
INTERRUPT_AFTER_STAGING = "after_staging"
INTERRUPT_AFTER_RENAME = "after_rename"
INTERRUPT_AFTER_EVENT_REGISTRATION = "after_event_registration"
INTERRUPT_AFTER_REGISTRATION = "after_registration"

InterruptionHook = Callable[[str], None]


@dataclass(frozen=True)
class ImportResult:
    """原子导入的稳定结果；重复提交只改变 duplicate，不改变原映射和事件范围。"""

    package_key: str
    package_sha256: str
    mapping: dict[str, str]
    duplicate: bool
    destination: str
    event_ids: tuple[str, ...]
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": IMPORT_RESULT_SCHEMA,
            "package_key": self.package_key,
            "package_sha256": self.package_sha256,
            "mapping": dict(self.mapping),
            "duplicate": self.duplicate,
            "destination": self.destination,
            "event_ids": list(self.event_ids),
            "files": list(self.files),
        }


@dataclass(frozen=True)
class AtomicImportPrecommitContext:
    """锁内最终校验可读取的稳定快照，不允许调用方借此写事件或提交文件。"""

    package_key: str
    source_package_sha256: str
    registry: Mapping[str, object]
    events: tuple[dict[str, object], ...]
    known_formal_ids: frozenset[str]


LockedPrecommitValidator = Callable[
    [ProjectPaths, AtomicImportPrecommitContext], Iterable[str] | None
]
ImportFilesFinalizer = Callable[
    [Mapping[str, str], Mapping[str, object]], Mapping[str, object]
]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"JSON 文件包含重复字段：{key}。")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"JSON 文件包含非标准数字：{value}。")


def _read_json(path: Path, *, label: str) -> object:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法读取或不是有效 JSON：{path.name}。") from exc


def _fsync_directory(path: Path) -> None:
    """文件改名后同步父目录；不支持目录 fsync 的文件系统只跳过对应系统错误。"""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _write_file_bytes(path: Path, content: bytes) -> None:
    """先完整写入并同步单个暂存文件，目录发布前不会暴露半截正文。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


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


def _atomic_write_json(path: Path, document: object) -> None:
    _atomic_write_bytes(path, canonical_json_text(document).encode("utf-8"))


def _cleanup_orphan_atomic_temp_files_locked(paths: ProjectPaths) -> None:
    """只清理本模块为事件和登记表创建的孤儿临时文件。"""

    removed = False
    for target in (paths.events_file, paths.import_registry_file):
        # tempfile 当前生成固定8位小写随机片段。把目标文件名、长度和字符集都写死，
        # 避免恢复时用宽泛的 *.tmp 规则误删同目录中的其他临时资料。
        owned_name = re.compile(
            rf"^\.{re.escape(target.name)}\.[a-z0-9_]{{8}}\.tmp$"
        )
        for candidate in sorted(target.parent.iterdir()):
            if not owned_name.fullmatch(candidate.name):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                continue
            candidate.unlink()
            removed = True
    if removed:
        # 恢复入口已经同时持有项目锁和事件锁；删除完成后同步根目录，
        # 保证下一次进程看到的目录项与恢复结果一致。
        _fsync_directory(paths.sdlc_dir)


def _normalized_project_relative_path(raw_path: object, *, label: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SdlcError(f"{label}必须是非空项目相对路径。")
    if "\\" in raw_path:
        raise SdlcError(f"{label}只能使用正斜杠分隔目录。")
    candidate = Path(raw_path)
    if candidate.is_absolute() or candidate == Path("."):
        raise SdlcError(f"{label}必须是项目内的具体相对路径。")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise SdlcError(f"{label}不能包含空目录、当前目录或上级目录。")
    return candidate.as_posix()


def _resolve_destination(paths: ProjectPaths, raw_path: object) -> tuple[str, Path]:
    relative_path = _normalized_project_relative_path(raw_path, label="导入目标目录")
    if "@client:" in relative_path:
        raise SdlcError("导入目标目录不能保留 @client: 临时引用。")
    parts = Path(relative_path).parts
    if not parts or parts[0] != ".codex-sdlc" or len(parts) < 3:
        raise SdlcError("导入目标目录必须位于 .codex-sdlc 下的具体产物目录。")
    reserved = {
        ".codex-sdlc/events.jsonl",
        ".codex-sdlc/import-registry.json",
        ".codex-sdlc/lock",
        ".codex-sdlc/.events.lock",
    }
    if relative_path in reserved or relative_path.startswith(
        ".codex-sdlc/import-transactions/"
    ):
        raise SdlcError("导入目标目录不能覆盖事件、登记表、锁或事务目录。")
    resolved = resolve_project_path(paths.root, relative_path)
    if not resolved.parent.is_dir():
        raise SdlcError(f"导入目标目录的父目录不存在：{relative_path}。")
    return relative_path, resolved


def _rewrite_file_path(raw_path: object, mapping: Mapping[str, str]) -> str:
    relative_path = _normalized_project_relative_path(raw_path, label="导入文件路径")
    rewritten_parts: list[str] = []
    for part in Path(relative_path).parts:
        match = TEMPORARY_REFERENCE_PATTERN.fullmatch(part)
        if match is not None:
            client_key = match.group("client_key")
            formal_id = mapping.get(client_key)
            if formal_id is None:
                raise SdlcError(
                    f"临时引用跨包或悬空：{part}，当前导入包没有对应 client_key。"
                )
            rewritten_parts.append(formal_id)
            continue
        if "@client:" in part:
            raise SdlcError(
                f"文件路径中的临时引用必须完整占用一个目录名：{relative_path}。"
            )
        rewritten_parts.append(part)
    rewritten = Path(*rewritten_parts).as_posix()
    if rewritten == IMPORT_RECEIPT_NAME:
        raise SdlcError(f"导入文件不能占用系统回执文件名：{IMPORT_RECEIPT_NAME}。")
    return rewritten


def _package_objects(document: Mapping[str, object]) -> tuple[AllocationObject, ...]:
    raw_objects = document.get("objects")
    if not isinstance(raw_objects, list):
        raise SdlcError("结构化导入包的 objects 必须是数组。")
    objects: list[AllocationObject] = []
    for raw_item in raw_objects:
        if not isinstance(raw_item, dict):
            raise SdlcError("结构化导入包的 objects 条目必须是对象。")
        depends_on = raw_item.get("depends_on")
        if not isinstance(depends_on, list) or any(
            not isinstance(item, str) for item in depends_on
        ):
            raise SdlcError("结构化导入对象的 depends_on 必须是字符串数组。")
        objects.append(
            AllocationObject(
                client_key=str(raw_item.get("client_key") or ""),
                id_prefix=str(raw_item.get("id_prefix") or ""),
                depends_on=tuple(depends_on),
            )
        )
    # 在进入项目锁前完成重复键、悬空引用和依赖环检查，错误包不能参与编号计算。
    build_allocation_order(objects)
    return tuple(objects)


def _validate_all_temporary_references(
    document: Mapping[str, object], objects: tuple[AllocationObject, ...]
) -> None:
    placeholder_mapping = {
        item.client_key: f"PLACEHOLDER-{index:03d}"
        for index, item in enumerate(objects, start=1)
    }
    raw_files = document.get("files")
    if not isinstance(raw_files, list):
        raise SdlcError("结构化导入包的 files 必须是数组。")
    rewritten_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise SdlcError("结构化导入包的 files 条目必须是对象。")
        rewritten_path = _rewrite_file_path(raw_file.get("relative_path"), placeholder_mapping)
        if rewritten_path in rewritten_paths:
            raise SdlcError(f"导入文件路径重复：{rewritten_path}。")
        rewritten_paths.add(rewritten_path)
        rewrite_temporary_references(raw_file.get("content"), placeholder_mapping)


def _prepare_package(document: object) -> tuple[dict[str, object], tuple[AllocationObject, ...]]:
    validate_schema_document(document, schema_name=IMPORT_PACKAGE_SCHEMA)
    if not isinstance(document, dict):
        raise SdlcError("结构化导入包顶层必须是对象。")
    package = dict(document)
    actual_digest = contract_sha256(package, schema_name=IMPORT_PACKAGE_SCHEMA)
    expected_digest = str(package.get("package_sha256") or "")
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise SdlcError(
            f"导入包规范摘要冲突：声明为 {expected_digest}，实际为 {actual_digest}。"
        )
    objects = _package_objects(package)
    _validate_all_temporary_references(package, objects)
    return package, objects


def _empty_registry() -> dict[str, object]:
    return {"schema": IMPORT_REGISTRY_SCHEMA, "packages": []}


def load_import_registry(paths: ProjectPaths) -> dict[str, object]:
    """读取完整登记表；内容损坏时直接阻止后续导入，不能猜测修复。"""

    if not paths.import_registry_file.exists():
        return _empty_registry()
    document = _read_json(paths.import_registry_file, label="原子导入登记表")
    validate_schema_document(document, schema_name=IMPORT_REGISTRY_SCHEMA)
    if not isinstance(document, dict) or not isinstance(document.get("packages"), list):
        raise SdlcError("原子导入登记表结构不正确。")
    seen: set[str] = set()
    for entry in document["packages"]:
        package_key = str(entry.get("package_key") or "") if isinstance(entry, dict) else ""
        if package_key in seen:
            raise SdlcError(f"原子导入登记表包含重复 package_key：{package_key}。")
        seen.add(package_key)
    return document


def _registry_entry(
    registry: Mapping[str, object], package_key: str
) -> dict[str, object] | None:
    packages = registry.get("packages")
    if not isinstance(packages, list):
        raise SdlcError("原子导入登记表缺少 packages 数组。")
    matches = [
        item
        for item in packages
        if isinstance(item, dict) and item.get("package_key") == package_key
    ]
    if len(matches) > 1:
        raise SdlcError(f"原子导入登记表包含重复 package_key：{package_key}。")
    return dict(matches[0]) if matches else None


def collect_known_formal_ids(
    events: Iterable[Mapping[str, object]], registry: Mapping[str, object]
) -> frozenset[str]:
    """只从公共事件的受控编号字段和原子登记映射收集正式编号。

    事件中的标题、摘要和 payload 都可能包含用户原文，不能因为一段普通文字刚好
    长得像正式编号就影响分配结果。业务专属结构中的其他编号应由锁内最终校验回调
    明确检查并返回，公共层不猜测字段含义。
    """

    result: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        for field_name in ("requirement_id", "task_id"):
            formal_id = event.get(field_name)
            if isinstance(formal_id, str) and FORMAL_ID_PATTERN.fullmatch(formal_id):
                result.add(formal_id)
    packages = registry.get("packages")
    if isinstance(packages, list):
        for entry in packages:
            if not isinstance(entry, dict) or not isinstance(entry.get("mapping"), dict):
                continue
            for formal_id in entry["mapping"].values():
                if isinstance(formal_id, str) and FORMAL_ID_PATTERN.fullmatch(formal_id):
                    result.add(formal_id)
    return frozenset(result)


def _validated_additional_formal_ids(value: Iterable[str] | None) -> frozenset[str]:
    """校验业务回调明确返回的结构化编号，避免把单个字符串逐字符当成编号集。"""

    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)):
        raise SdlcError("锁内最终校验返回值必须是正式编号集合，不能是单个字符串。")
    try:
        candidates = tuple(value)
    except TypeError as exc:
        raise SdlcError("锁内最终校验返回值必须是可迭代的正式编号集合。") from exc
    result: set[str] = set()
    for formal_id in candidates:
        if not isinstance(formal_id, str) or not FORMAL_ID_PATTERN.fullmatch(formal_id):
            raise SdlcError(f"锁内最终校验返回了无效正式编号：{formal_id!r}。")
        result.add(formal_id)
    return frozenset(result)


def _build_files(
    paths: ProjectPaths,
    package: Mapping[str, object],
    mapping: Mapping[str, str],
    destination_relative: str,
) -> tuple[tuple[str, object], ...]:
    raw_files = package.get("files")
    if not isinstance(raw_files, list):
        raise SdlcError("结构化导入包的 files 必须是数组。")
    files: list[tuple[str, object]] = []
    used_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise SdlcError("结构化导入包的 files 条目必须是对象。")
        relative_path = _rewrite_file_path(raw_file.get("relative_path"), mapping)
        if relative_path in used_paths:
            raise SdlcError(f"临时引用重写后文件路径重复：{relative_path}。")
        used_paths.add(relative_path)
        content = rewrite_temporary_references(raw_file.get("content"), mapping)
        canonical_json_bytes(content)
        # 用项目路径解析器再核对最终公开路径，避免重写结果绕过项目目录边界。
        resolve_project_path(paths.root, f"{destination_relative}/{relative_path}")
        files.append((relative_path, content))
    return tuple(sorted(files, key=lambda item: item[0]))


def _apply_files_finalizer(
    files: tuple[tuple[str, object], ...],
    mapping: Mapping[str, str],
    finalizer: ImportFilesFinalizer | None,
) -> tuple[tuple[str, object], ...]:
    """在正式映射产生后完成派生字段，并把回调结果重新收口为安全 JSON。

    回调只能改已有文件的内容，不能借最终化阶段增删或改名文件。传入和取回都做
    深拷贝，调用方在函数返回后继续修改自己保留的对象也不会改变待提交包。
    """

    if finalizer is None:
        return files
    expected_paths = {relative_path for relative_path, _ in files}
    callback_files = {
        relative_path: deepcopy(content) for relative_path, content in files
    }
    finalized = finalizer(
        MappingProxyType(dict(mapping)),
        MappingProxyType(callback_files),
    )
    if not isinstance(finalized, Mapping):
        raise SdlcError("映射后文件最终化必须返回相对路径到 JSON 内容的映射。")
    if any(not isinstance(relative_path, str) for relative_path in finalized):
        raise SdlcError("映射后文件最终化返回的文件路径必须是字符串。")
    actual_paths = set(finalized)
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        details: list[str] = []
        if missing:
            details.append(f"缺少 {', '.join(missing)}")
        if extra:
            details.append(f"新增 {', '.join(extra)}")
        raise SdlcError(
            "映射后文件最终化不能增删或改名文件：" + "；".join(details) + "。"
        )

    result: list[tuple[str, object]] = []
    for relative_path in sorted(expected_paths):
        # 空映射会拒绝任何被回调重新引入的 @client: 临时引用；返回值同时会被
        # 重建为全新的 JSON 对象，切断回调持有的可变引用。
        content = rewrite_temporary_references(
            deepcopy(finalized[relative_path]), {}
        )
        canonical_json_bytes(content)
        result.append((relative_path, content))
    return tuple(result)


def _finalized_package_sha256(
    *,
    source_package_sha256: str,
    mapping: Mapping[str, str],
    files: tuple[tuple[str, object], ...],
) -> str:
    """把来源摘要、正式映射和最终文件一起纳入最终整包摘要。"""

    return canonical_sha256(
        {
            "schema": "sdlc.atomic-import-finalized.v1",
            "source_package_sha256": source_package_sha256,
            "mapping": dict(sorted(mapping.items())),
            "files": [
                {"relative_path": relative_path, "content": content}
                for relative_path, content in files
            ],
        }
    )


def _result_from_registration(
    registration: Mapping[str, object], *, duplicate: bool
) -> ImportResult:
    mapping = registration.get("mapping")
    files = registration.get("files")
    if not isinstance(mapping, dict) or not isinstance(files, list):
        raise SdlcError("原子导入登记记录缺少编号映射或文件清单。")
    result = ImportResult(
        package_key=str(registration.get("package_key") or ""),
        package_sha256=str(registration.get("package_sha256") or ""),
        mapping={str(key): str(value) for key, value in mapping.items()},
        duplicate=duplicate,
        destination=str(registration.get("destination") or ""),
        event_ids=(str(registration.get("event_id") or ""),),
        files=tuple(str(item) for item in files),
    )
    validate_schema_document(result.as_dict(), schema_name=IMPORT_RESULT_SCHEMA)
    return result


def _bundle_sha256(directory: Path) -> str:
    if not directory.is_dir() or directory.is_symlink():
        raise SdlcError(f"原子导入目录不存在或不是普通目录：{directory}。")
    entries: list[dict[str, str]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise SdlcError(f"原子导入目录不能包含符号链接：{path.name}。")
        if path.is_file():
            entries.append(
                {
                    "relative_path": path.relative_to(directory).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    return canonical_sha256(entries)


def _write_staged_bundle(
    staging_path: Path,
    files: tuple[tuple[str, object], ...],
    receipt: ImportResult,
) -> None:
    staging_path.mkdir(parents=True, exist_ok=False)
    try:
        for relative_path, content in files:
            _write_file_bytes(
                staging_path / relative_path,
                canonical_json_text(content).encode("utf-8"),
            )
        _write_file_bytes(
            staging_path / IMPORT_RECEIPT_NAME,
            canonical_json_text(receipt.as_dict()).encode("utf-8"),
        )
        # 所有文件都已同步后再同步暂存根目录，后续整目录改名才有唯一提交边界。
        for directory in sorted(
            [path for path in staging_path.rglob("*") if path.is_dir()],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(staging_path)
        _fsync_directory(staging_path.parent)
    except Exception:
        shutil.rmtree(staging_path, ignore_errors=True)
        raise


def _event_for_import(
    paths: ProjectPaths,
    event_id: str,
    registration: Mapping[str, object],
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": "structured_package_imported",
        "project_path": str(paths.root),
        "requirement_id": None,
        "task_id": None,
        "created_at": now_iso(),
        "source": "atomic-import",
        "summary": f"原子导入结构化包 {registration['package_key']}",
        "payload": {
            "package_key": registration["package_key"],
            "package_sha256": registration["package_sha256"],
            "mapping": registration["mapping"],
            "destination": registration["destination"],
            "files": registration["files"],
            "bundle_sha256": registration["bundle_sha256"],
        },
    }


def _event_matches_registration(
    event: Mapping[str, object],
    registration: Mapping[str, object],
    *,
    project_root: Path,
) -> bool:
    payload = event.get("payload")
    expected_payload = {
        "package_key": registration.get("package_key"),
        "package_sha256": registration.get("package_sha256"),
        "mapping": registration.get("mapping"),
        "destination": registration.get("destination"),
        "files": registration.get("files"),
        "bundle_sha256": registration.get("bundle_sha256"),
    }
    return (
        event.get("event_id") == registration.get("event_id")
        and event.get("event_type") == "structured_package_imported"
        and event.get("source") == "atomic-import"
        and event.get("project_path") == str(project_root)
        and event.get("requirement_id") is None
        and event.get("task_id") is None
        and isinstance(event.get("created_at"), str)
        and bool(str(event.get("created_at") or "").strip())
        and event.get("summary")
        == f"原子导入结构化包 {registration.get('package_key')}"
        and payload == expected_payload
    )


def _ensure_event(paths: ProjectPaths, event: dict[str, object]) -> None:
    events = load_events(paths)
    matches = [item for item in events if item.get("event_id") == event.get("event_id")]
    if matches:
        if len(matches) != 1 or matches[0] != event:
            raise SdlcError(f"事件编号冲突：{event.get('event_id')}。")
        return
    # 保留已有事件的原始字节，只把新行追加到临时副本后整体替换。
    # 这样既能保证中断时只出现提交前内容或追加后的完整内容，也不会顺手改写历史事件格式。
    existing_content = paths.events_file.read_bytes() if paths.events_file.exists() else b""
    separator = b"\n" if existing_content and not existing_content.endswith(b"\n") else b""
    new_line = (
        json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    content = existing_content + separator + new_line
    _atomic_write_bytes(paths.events_file, content)


def _ensure_registration(paths: ProjectPaths, registration: dict[str, object]) -> None:
    registry = load_import_registry(paths)
    existing = _registry_entry(registry, str(registration["package_key"]))
    if existing is not None:
        if existing != registration:
            raise SdlcError(
                f"package_key {registration['package_key']} 的导入登记与待恢复事务不一致。"
            )
        return
    packages = registry.get("packages")
    if not isinstance(packages, list):
        raise SdlcError("原子导入登记表缺少 packages 数组。")
    packages.append(registration)
    packages.sort(key=lambda item: str(item.get("package_key") or ""))
    validate_schema_document(registry, schema_name=IMPORT_REGISTRY_SCHEMA)
    _atomic_write_json(paths.import_registry_file, registry)


def _verify_registration(
    paths: ProjectPaths,
    registration: Mapping[str, object],
    *,
    events: list[dict[str, object]] | None = None,
) -> None:
    destination_relative, destination_path = _resolve_destination(
        paths, registration.get("destination")
    )
    if destination_relative != registration.get("destination") or not destination_path.is_dir():
        raise SdlcError(
            f"已登记导入包缺少完整目标目录：{registration.get('package_key')}。"
        )
    actual_bundle_hash = _bundle_sha256(destination_path)
    expected_bundle_hash = str(registration.get("bundle_sha256") or "")
    if not hmac.compare_digest(actual_bundle_hash, expected_bundle_hash):
        raise SdlcError(
            f"已登记导入包完整哈希不一致：{registration.get('package_key')}。"
        )

    receipt_path = destination_path / IMPORT_RECEIPT_NAME
    receipt = _read_json(receipt_path, label="原子导入回执")
    validate_schema_document(receipt, schema_name=IMPORT_RESULT_SCHEMA)
    expected_receipt = _result_from_registration(registration, duplicate=False).as_dict()
    if receipt != expected_receipt:
        raise SdlcError(f"原子导入回执与登记不一致：{registration.get('package_key')}。")

    current_events = events if events is not None else load_events(paths)
    event_matches = [
        event
        for event in current_events
        if event.get("event_id") == registration.get("event_id")
    ]
    if len(event_matches) != 1 or not _event_matches_registration(
        event_matches[0], registration, project_root=paths.root
    ):
        raise SdlcError(f"原子导入事件与登记不一致：{registration.get('package_key')}。")


def _load_transaction(path: Path) -> dict[str, object]:
    document = _read_json(path, label="原子导入事务日志")
    validate_schema_document(document, schema_name=IMPORT_TRANSACTION_SCHEMA)
    if not isinstance(document, dict):
        raise SdlcError("原子导入事务日志顶层必须是对象。")
    return document


def _finalize_transaction_locked(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    *,
    interruption_hook: InterruptionHook | None = None,
) -> None:
    registration = transaction.get("registration")
    event = transaction.get("event")
    if not isinstance(registration, dict) or not isinstance(event, dict):
        raise SdlcError("原子导入事务日志缺少登记或事件。")
    _, destination_path = _resolve_destination(paths, registration.get("destination"))
    actual_bundle_hash = _bundle_sha256(destination_path)
    expected_bundle_hash = str(registration.get("bundle_sha256") or "")
    if not hmac.compare_digest(actual_bundle_hash, expected_bundle_hash):
        raise SdlcError(
            f"待恢复导入包完整哈希不一致：{registration.get('package_key')}。"
        )
    _ensure_event(paths, event)
    if interruption_hook is not None:
        # 事件与摘要登记使用两个各自原子的文件替换。这里保留真实中断点，
        # 用事务日志证明只写入事件时也能补全为同一个成功结果。
        interruption_hook(INTERRUPT_AFTER_EVENT_REGISTRATION)
    _ensure_registration(paths, registration)
    _verify_registration(paths, registration)


def _cleanup_transaction(paths: ProjectPaths, transaction: Mapping[str, object], journal: Path) -> None:
    staging_relative = transaction.get("staging_path")
    if isinstance(staging_relative, str):
        try:
            staging_path = resolve_project_path(paths.root, staging_relative)
            staging_path.relative_to(paths.import_transactions_dir / "staging")
        except (SdlcError, ValueError):
            staging_path = None
        if staging_path is not None:
            shutil.rmtree(staging_path, ignore_errors=True)
    journal.unlink(missing_ok=True)
    _fsync_directory(paths.import_transactions_dir)


def _recover_atomic_imports_locked(paths: ProjectPaths) -> list[str]:
    recovered: list[str] = []
    staging_root = paths.import_transactions_dir / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    journals = sorted(paths.import_transactions_dir.glob("*.json"))
    for journal in journals:
        transaction = _load_transaction(journal)
        registration = transaction.get("registration")
        event = transaction.get("event")
        if not isinstance(registration, dict) or not isinstance(event, dict):
            raise SdlcError(f"原子导入事务日志内容不完整：{journal.name}。")
        destination_relative, destination_path = _resolve_destination(
            paths, registration.get("destination")
        )
        registry = load_import_registry(paths)
        existing_registration = _registry_entry(
            registry, str(registration.get("package_key") or "")
        )
        current_events = load_events(paths)
        matching_events = [
            item for item in current_events if item.get("event_id") == event.get("event_id")
        ]
        if destination_path.exists():
            _finalize_transaction_locked(paths, transaction)
            recovered.append(str(registration.get("package_key") or ""))
            _cleanup_transaction(paths, transaction, journal)
            continue
        if existing_registration is not None or matching_events:
            raise SdlcError(
                f"导入事务缺少目标目录，但已经留下登记或事件：{destination_relative}。"
            )
        # 改名前中断属于完整失败：清理暂存和日志，不登记编号，也不追加事件。
        _cleanup_transaction(paths, transaction, journal)

    # 进程可能在事务日志落盘前中断；没有日志引用的暂存目录一定尚未到提交点，可以直接清理。
    for orphan in sorted(staging_root.iterdir()):
        if orphan.is_dir() and not orphan.is_symlink():
            shutil.rmtree(orphan, ignore_errors=True)
        else:
            orphan.unlink(missing_ok=True)
    for temp_file in paths.import_transactions_dir.glob("*.tmp"):
        temp_file.unlink(missing_ok=True)
    _cleanup_orphan_atomic_temp_files_locked(paths)
    return recovered


def recover_atomic_imports(paths: ProjectPaths) -> list[str]:
    """在项目锁和事件锁内恢复全部中断事务，返回被补全为成功的 package_key。"""

    ensure_base_dirs(paths)
    if not paths.events_file.exists():
        raise SdlcError("当前项目还没有 events.jsonl，不能执行结构化原子导入恢复。")
    with project_lock(paths):
        with event_write_lock(paths):
            return _recover_atomic_imports_locked(paths)


def atomic_import(
    paths: ProjectPaths,
    document: object,
    *,
    interruption_hook: InterruptionHook | None = None,
    locked_precommit_validator: LockedPrecommitValidator | None = None,
    files_finalizer: ImportFilesFinalizer | None = None,
) -> ImportResult:
    """校验、编号、重写并提交整包文件；只有整目录改名是正式提交点。

    locked_precommit_validator 在项目锁和事件锁内、编号分配前调用。它可以重新读取
    最新业务状态并拒绝过期包，也可以返回从受控结构字段读取到的额外正式编号。
    files_finalizer 在正式映射产生后、摘要和暂存写入前调用，用于统一重算派生字段
    和跨文件哈希。相同包重试必须使用同样的确定性最终化函数。
    """

    package, objects = _prepare_package(document)
    if not paths.sdlc_dir.is_dir() or not paths.events_file.exists():
        raise SdlcError("当前项目还没有 events.jsonl，不能执行结构化原子导入。")
    destination_relative, _ = _resolve_destination(paths, package.get("destination"))
    package_key = str(package.get("package_key") or "")
    source_package_sha256 = str(package.get("package_sha256") or "")
    hook = interruption_hook or (lambda stage: None)

    with project_lock(paths):
        with event_write_lock(paths):
            _recover_atomic_imports_locked(paths)
            registry = load_import_registry(paths)
            events = load_events(paths)
            existing = _registry_entry(registry, package_key)
            if existing is not None:
                existing_mapping = existing.get("mapping")
                if not isinstance(existing_mapping, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in existing_mapping.items()
                ):
                    raise SdlcError("原子导入登记记录缺少有效编号映射。")
                expected_package_sha256 = source_package_sha256
                if files_finalizer is not None:
                    existing_files = _build_files(
                        paths,
                        package,
                        existing_mapping,
                        destination_relative,
                    )
                    existing_files = _apply_files_finalizer(
                        existing_files,
                        existing_mapping,
                        files_finalizer,
                    )
                    expected_package_sha256 = _finalized_package_sha256(
                        source_package_sha256=source_package_sha256,
                        mapping=existing_mapping,
                        files=existing_files,
                    )
                if existing.get("package_sha256") != expected_package_sha256:
                    raise SdlcError(
                        f"package_key {package_key} 已经登记了不同的规范摘要，不能覆盖原包。"
                    )
                _verify_registration(paths, existing, events=events)
                return _result_from_registration(existing, duplicate=True)

            _, destination_path = _resolve_destination(paths, destination_relative)
            if destination_path.exists():
                raise SdlcError(f"导入目标目录已经存在但没有幂等登记：{destination_relative}。")

            known_formal_ids = collect_known_formal_ids(events, registry)
            additional_formal_ids = frozenset()
            if locked_precommit_validator is not None:
                # 回调在与编号分配、事务登记相同的项目锁内执行。快照先深拷贝再包装，
                # 避免业务校验无意修改公共层随后要写回的登记表或事件列表。
                context = AtomicImportPrecommitContext(
                    package_key=package_key,
                    source_package_sha256=source_package_sha256,
                    registry=MappingProxyType(deepcopy(registry)),
                    events=tuple(deepcopy(events)),
                    known_formal_ids=known_formal_ids,
                )
                additional_formal_ids = _validated_additional_formal_ids(
                    locked_precommit_validator(paths, context)
                )

            mapping = allocate_stable_ids(
                objects,
                existing_ids=known_formal_ids | additional_formal_ids,
            )
            files = _build_files(paths, package, mapping, destination_relative)
            files = _apply_files_finalizer(files, mapping, files_finalizer)
            package_sha256 = source_package_sha256
            if files_finalizer is not None:
                package_sha256 = _finalized_package_sha256(
                    source_package_sha256=source_package_sha256,
                    mapping=mapping,
                    files=files,
                )
            event_id = next_event_id(events)
            project_files = tuple(
                f"{destination_relative}/{relative_path}" for relative_path, _ in files
            )
            result = ImportResult(
                package_key=package_key,
                package_sha256=package_sha256,
                mapping=mapping,
                duplicate=False,
                destination=destination_relative,
                event_ids=(event_id,),
                files=project_files,
            )
            validate_schema_document(result.as_dict(), schema_name=IMPORT_RESULT_SCHEMA)

            transaction_id = uuid.uuid4().hex
            staging_root = paths.import_transactions_dir / "staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging_path = staging_root / transaction_id
            journal_path = paths.import_transactions_dir / f"{transaction_id}.json"
            transaction: dict[str, object] | None = None
            try:
                _write_staged_bundle(staging_path, files, result)
                registration: dict[str, object] = {
                    "package_key": package_key,
                    "package_sha256": package_sha256,
                    "mapping": mapping,
                    "destination": destination_relative,
                    "event_id": event_id,
                    "files": list(project_files),
                    "bundle_sha256": _bundle_sha256(staging_path),
                }
                event = _event_for_import(paths, event_id, registration)
                transaction = {
                    "schema": IMPORT_TRANSACTION_SCHEMA,
                    "transaction_id": transaction_id,
                    "staging_path": staging_path.relative_to(paths.root).as_posix(),
                    "registration": registration,
                    "event": event,
                }
                validate_schema_document(transaction, schema_name=IMPORT_TRANSACTION_SCHEMA)
                _atomic_write_json(journal_path, transaction)
                hook(INTERRUPT_AFTER_STAGING)

                # 同一文件系统内的整目录改名是唯一提交点：之前恢复为失败，之后恢复为成功。
                os.rename(staging_path, destination_path)
                _fsync_directory(destination_path.parent)
                hook(INTERRUPT_AFTER_RENAME)

                _finalize_transaction_locked(
                    paths, transaction, interruption_hook=hook
                )
                hook(INTERRUPT_AFTER_REGISTRATION)
                _cleanup_transaction(paths, transaction, journal_path)
                return result
            except Exception:
                if transaction is not None and destination_path.is_dir():
                    # 改名已经完成时不能再回滚可见正式包；尝试补齐事件和登记，失败则保留日志阻止后续写入。
                    try:
                        _finalize_transaction_locked(paths, transaction)
                        _cleanup_transaction(paths, transaction, journal_path)
                    except Exception:
                        pass
                else:
                    shutil.rmtree(staging_path, ignore_errors=True)
                    journal_path.unlink(missing_ok=True)
                raise


__all__ = [
    "IMPORT_PACKAGE_SCHEMA",
    "IMPORT_REGISTRY_SCHEMA",
    "IMPORT_RESULT_SCHEMA",
    "IMPORT_TRANSACTION_SCHEMA",
    "INTERRUPT_AFTER_EVENT_REGISTRATION",
    "INTERRUPT_AFTER_REGISTRATION",
    "INTERRUPT_AFTER_RENAME",
    "INTERRUPT_AFTER_STAGING",
    "AtomicImportPrecommitContext",
    "ImportFilesFinalizer",
    "ImportResult",
    "LockedPrecommitValidator",
    "atomic_import",
    "collect_known_formal_ids",
    "load_import_registry",
    "recover_atomic_imports",
]
