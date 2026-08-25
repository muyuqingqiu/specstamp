from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Callable, Mapping, Sequence

from codex_sdlc.core.artifact_index import (
    formal_manifest_entries,
    validate_artifact_index_document,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.reference_index import validate_reference_index_document
from codex_sdlc.core.structured_contract import canonical_json_text, canonical_sha256


FaultInjector = Callable[[str, Path], None]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3,}$")
_STAGING_NAME = re.compile(r"^start-[a-z0-9-]+$")
_DESIGN_TYPES = {
    "design_plan",
    "design_summary",
    "design_artifact",
    "design_artifact_json",
    "design_reference",
}
_REQUIRED_DIRECTORIES = {"original", "effective", "versions", "tasks"}
_REQUIRED_GENERATED_FILES = {
    "original/formal.v3.json",
    "original/artifact-index.v1.json",
    "reference-index.v1.json",
    "effective/requirement.current.json",
    "effective/requirement.current.md",
    "effective/design.current.json",
    "effective/design.current.md",
    "effective/test-matrix.current.json",
    "effective/test-matrix.current.md",
    "versions/requirement.v1.json",
    "versions/requirement.v1.md",
    "versions/design.v1.json",
    "versions/design.v1.md",
    "versions/test-matrix.v1.json",
    "versions/test-matrix.v1.md",
    "traceability.md",
    "status.json",
}


def _fail(message: str) -> SdlcError:
    return SdlcError(message, exit_code=1)


def _call_fault(
    fault_injector: FaultInjector | None,
    point: str,
    staging: Path,
) -> None:
    if fault_injector is not None:
        fault_injector(point, staging)


def _safe_relative(value: object, *, label: str) -> Path:
    raw = str(value or "")
    candidate = Path(raw)
    if (
        not raw.strip()
        or "\x00" in raw
        or candidate.is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or any(not part or part in {".", ".."} for part in candidate.parts)
    ):
        raise _fail(f"{label}不是安全的项目内相对路径：{raw!r}。")
    return candidate


def _require_plain_directory(path: Path, *, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise _fail(f"{label}不存在或无法访问：{path}。") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _fail(f"{label}必须是真实目录，不能是符号链接：{path}。")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _fail(f"{label}无法安全解析：{path}。") from exc


def _require_controlled_roots(paths: ProjectPaths) -> tuple[Path, Path, Path]:
    project = _require_plain_directory(paths.root, label="项目根目录")
    sdlc = _require_plain_directory(paths.sdlc_dir, label="SDLC 目录")
    requirements = _require_plain_directory(
        paths.requirements_dir,
        label="正式需求父目录",
    )
    try:
        sdlc.relative_to(project)
        requirements.relative_to(sdlc)
    except ValueError as exc:
        raise _fail("正式建档受控目录越过当前项目边界。") from exc
    return project, sdlc, requirements


def _ensure_staging_root(paths: ProjectPaths) -> Path:
    _project, sdlc, _requirements = _require_controlled_roots(paths)
    root = paths.start_staging_root
    if root.exists() or root.is_symlink():
        resolved = _require_plain_directory(root, label="正式建档暂存根目录")
    else:
        try:
            root.mkdir(mode=0o700)
        except OSError as exc:
            raise _fail(f"无法创建正式建档暂存根目录：{root}。") from exc
        resolved = _require_plain_directory(root, label="正式建档暂存根目录")
    try:
        resolved.relative_to(sdlc)
    except ValueError as exc:
        raise _fail("正式建档暂存根目录越过 SDLC 目录。") from exc
    return resolved


def _require_no_symlink_chain(root: Path, relative: Path, *, label: str) -> Path:
    resolved_root = _require_plain_directory(root, label=f"{label}根目录")
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise _fail(f"{label}无法读取：{relative.as_posix()}。") from exc
            if stat.S_ISLNK(mode):
                raise _fail(f"{label}路径中不能包含符号链接：{relative.as_posix()}。")
    try:
        candidate = (root / relative).resolve(strict=False)
        candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(f"{label}越过受控目录：{relative.as_posix()}。") from exc
    return root / relative


def _read_json_bytes(content: bytes, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(f"{label}不是有效的 UTF-8 JSON。") from exc
    if not isinstance(document, dict):
        raise _fail(f"{label}顶层必须是 JSON 对象。")
    return document


def _json_bytes(document: Mapping[str, object]) -> bytes:
    return canonical_json_text(document).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise _fail(f"原子写入临时文件发生碰撞：{temporary.name}。")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _write_new_file(staging: Path, relative: str, content: bytes) -> Path:
    rel = _safe_relative(relative, label="staging 写入路径")
    target = _require_no_symlink_chain(staging, rel, label="staging 写入路径")
    if target.exists() or target.is_symlink():
        raise _fail(f"staging 目标文件已经存在：{relative}。")
    target.parent.mkdir(parents=True, exist_ok=True)
    _require_no_symlink_chain(staging, rel.parent, label="staging 中间目录")
    try:
        with target.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _fail(f"staging 文件写入失败：{relative}。") from exc
    return target


def _copy_verified_source(
    source_root: Path,
    staging: Path,
    item: Mapping[str, object],
    *,
    fault_injector: FaultInjector | None,
) -> None:
    source_relative = _safe_relative(
        item.get("source_path"),
        label="artifact_manifest source_path",
    )
    archive_relative = _safe_relative(
        item.get("archive_path"),
        label="artifact_manifest archive_path",
    )
    if not archive_relative.parts or archive_relative.parts[0] != "original":
        raise _fail("artifact_manifest archive_path 必须位于 original/。")
    source = _require_no_symlink_chain(
        source_root,
        source_relative,
        label="DRAFT 来源",
    )
    try:
        before = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise _fail(f"DRAFT 来源文件不存在：{source_relative.as_posix()}。") from exc
    if not stat.S_ISREG(before.st_mode):
        raise _fail(f"DRAFT 来源必须是普通文件：{source_relative.as_posix()}。")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened_before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _fail(f"DRAFT 来源读取失败：{source_relative.as_posix()}。") from exc
    content = b"".join(chunks)
    _call_fault(fault_injector, "after_source_read", staging)
    try:
        after = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise _fail(f"DRAFT 来源在复制期间消失：{source_relative.as_posix()}。") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_opened_before = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
        opened_before.st_ctime_ns,
    )
    identity_opened_after = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
        opened_after.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if not (
        identity_before
        == identity_opened_before
        == identity_opened_after
        == identity_after
    ):
        raise _fail(f"DRAFT 来源在复制期间发生变化：{source_relative.as_posix()}。")
    expected = str(item.get("sha256") or "")
    if _SHA256.fullmatch(expected) is None or _sha256(content) != expected:
        raise _fail(f"DRAFT 来源完整 SHA-256 不一致：{source_relative.as_posix()}。")
    archived = _write_new_file(staging, archive_relative.as_posix(), content)
    if _sha256(archived.read_bytes()) != expected:
        raise _fail(f"归档文件完整 SHA-256 复核失败：{archive_relative.as_posix()}。")


def _manifest(
    package: Mapping[str, object],
    artifact_index: Mapping[str, object],
) -> list[dict[str, object]]:
    raw = package.get("artifact_manifest")
    if not isinstance(raw, list) or not raw:
        raise _fail("formal.v3 缺少非空 artifact_manifest。")
    if any(not isinstance(item, Mapping) for item in raw):
        raise _fail("artifact_manifest 每一项都必须是对象。")
    candidate = [deepcopy(dict(item)) for item in raw]
    expected = formal_manifest_entries(artifact_index)
    if canonical_sha256(candidate) != canonical_sha256(expected):
        raise _fail("artifact_manifest 与真实 artifact-index.v1 不完全一致。")
    source_paths = [str(item.get("source_path") or "") for item in candidate]
    archive_paths = [str(item.get("archive_path") or "") for item in candidate]
    artifact_ids = [str(item.get("artifact_id") or "") for item in candidate]
    if (
        len(source_paths) != len(set(source_paths))
        or len(archive_paths) != len(set(archive_paths))
        or len(artifact_ids) != len(set(artifact_ids))
    ):
        raise _fail("artifact_manifest 的来源、归档路径和产物编号必须逐项唯一。")
    return candidate


def _verify_source_manifest_unchanged(
    source_root: Path,
    manifest: Sequence[Mapping[str, object]],
) -> None:
    """prepared 前再读一次全部来源，防止较早复制的文件在后续渲染期间变化。"""

    for item in manifest:
        relative = _safe_relative(
            item.get("source_path"),
            label="artifact_manifest source_path",
        )
        source = _require_no_symlink_chain(
            source_root,
            relative,
            label="DRAFT 来源复核",
        )
        if source.is_symlink() or not source.is_file():
            raise _fail(f"DRAFT 来源在构建期间失效：{relative.as_posix()}。")
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise _fail(f"DRAFT 来源在构建期间无法读取：{relative.as_posix()}。") from exc
        if _sha256(content) != item.get("sha256"):
            raise _fail(f"DRAFT 来源在构建期间发生变化：{relative.as_posix()}。")


def _archive_reference_index(
    reference_index: Mapping[str, object],
    manifest: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    document = deepcopy(dict(reference_index))
    entries = document.get("entries")
    if not isinstance(entries, dict):
        raise _fail("reference-index.v1 缺少 entries。")
    path_map: dict[str, tuple[str, str]] = {}
    for item in manifest:
        source = str(item.get("source_path") or "")
        archive = str(item.get("archive_path") or "")
        digest = str(item.get("sha256") or "")
        path_map[source] = (archive, digest)
        path_map[archive] = (archive, digest)
    for reference_id, raw_entry in entries.items():
        if not isinstance(raw_entry, dict):
            raise _fail(f"{reference_id} 的正式引用必须是对象。")
        source_path = str(raw_entry.get("path") or "")
        mapped = path_map.get(source_path)
        if mapped is None:
            raise _fail(f"{reference_id} 的引用路径不在正式清单中。")
        if raw_entry.get("sha256") != mapped[1]:
            raise _fail(f"{reference_id} 的引用哈希与正式清单不一致。")
        raw_entry["path"] = mapped[0]
        locator = raw_entry.get("locator")
        if isinstance(locator, dict) and locator.get("kind") == "design_node":
            node_source = str(locator.get("node_index_path") or "")
            node_mapped = path_map.get(node_source)
            if node_mapped is None:
                raise _fail(f"{reference_id} 的设计节点索引不在正式清单中。")
            if locator.get("node_index_sha256") != node_mapped[1]:
                raise _fail(f"{reference_id} 的设计节点索引哈希与正式清单不一致。")
            locator["node_index_path"] = node_mapped[0]
    return document


def _load_manifest_json(
    staging: Path,
    manifest: Sequence[Mapping[str, object]],
    *,
    artifact_types: set[str],
) -> list[tuple[Mapping[str, object], dict[str, object]]]:
    result: list[tuple[Mapping[str, object], dict[str, object]]] = []
    for item in manifest:
        if str(item.get("artifact_type") or "") not in artifact_types:
            continue
        relative = _safe_relative(item.get("archive_path"), label="归档 JSON 路径")
        content = (staging / relative).read_bytes()
        result.append((item, _read_json_bytes(content, label=relative.as_posix())))
    return result


def _pointer_id_map(reference_index: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    entries = reference_index.get("entries")
    for stable_id, raw_entry in entries.items() if isinstance(entries, Mapping) else []:
        if not isinstance(raw_entry, Mapping):
            continue
        locator = raw_entry.get("locator")
        if isinstance(locator, Mapping) and locator.get("kind") == "json_pointer":
            value = str(locator.get("value") or "")
            if value.startswith("/functional_requirements/") or value.startswith(
                "/global_rules/"
            ):
                result[value] = str(stable_id)
    return result


def _requirement_projection(
    requirement_id: str,
    source_draft_id: str,
    split: Mapping[str, object],
    reference_index: Mapping[str, object],
) -> dict[str, object]:
    pointers = _pointer_id_map(reference_index)
    global_rules: list[dict[str, object]] = []
    for index, item in enumerate(split.get("global_rules", [])):
        if not isinstance(item, Mapping):
            raise _fail("requirement-split.v1 的 global_rules 必须是对象列表。")
        stable_id = pointers.get(f"/global_rules/{index}")
        if stable_id is None or not stable_id.startswith("GR-"):
            raise _fail("reference-index.v1 缺少 GR 稳定编号定位。")
        global_rules.append({"id": stable_id, **deepcopy(dict(item))})
    functional_requirements: list[dict[str, object]] = []
    for index, item in enumerate(split.get("functional_requirements", [])):
        if not isinstance(item, Mapping):
            raise _fail("requirement-split.v1 的 functional_requirements 必须是对象列表。")
        stable_id = pointers.get(f"/functional_requirements/{index}")
        if stable_id is None or not stable_id.startswith("FR-"):
            raise _fail("reference-index.v1 缺少 FR 稳定编号定位。")
        copied = deepcopy(dict(item))
        acceptance: list[dict[str, object]] = []
        raw_acceptance = copied.get("acceptance_criteria", [])
        if not isinstance(raw_acceptance, list):
            raise _fail("requirement-split.v1 的 acceptance_criteria 必须是列表。")
        for acceptance_index, raw_item in enumerate(raw_acceptance):
            if not isinstance(raw_item, Mapping):
                raise _fail("验收标准必须是结构化对象。")
            pointer = (
                f"/functional_requirements/{index}/acceptance_criteria/"
                f"{acceptance_index}"
            )
            acceptance_id = pointers.get(pointer)
            if acceptance_id is None or not acceptance_id.startswith("AC-"):
                raise _fail("reference-index.v1 缺少 AC 稳定编号定位。")
            acceptance.append({"id": acceptance_id, **deepcopy(dict(raw_item))})
        copied["acceptance_criteria"] = acceptance
        functional_requirements.append({"id": stable_id, **copied})
    return {
        "schema_version": "requirement-current.v1",
        "requirement_id": requirement_id,
        "source_draft_id": source_draft_id,
        "version": "requirement.v1",
        "is_current": True,
        "title": split.get("title"),
        "background": split.get("background"),
        "goal": split.get("goal"),
        "scope": deepcopy(split.get("scope", [])),
        "out_of_scope": deepcopy(split.get("out_of_scope", [])),
        "user_scenarios": deepcopy(split.get("user_scenarios", [])),
        "global_rules": global_rules,
        "functional_requirements": functional_requirements,
        "open_questions": deepcopy(split.get("open_questions", [])),
    }


def _design_projection(
    requirement_id: str,
    source_draft_id: str,
    documents: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> dict[str, object]:
    return {
        "schema_version": "design-current.v1",
        "requirement_id": requirement_id,
        "source_draft_id": source_draft_id,
        "version": "design.v1",
        "is_current": True,
        "artifacts": [
            {
                "artifact_id": item.get("business_id"),
                "artifact_type": item.get("artifact_type"),
                "archive_path": item.get("archive_path"),
                "sha256": item.get("sha256"),
                "document": deepcopy(dict(document)),
            }
            for item, document in documents
        ],
    }


def _test_matrix_projection(
    requirement_id: str,
    source_draft_id: str,
    requirement: Mapping[str, object],
) -> dict[str, object]:
    acceptance: list[dict[str, object]] = []
    functional = requirement.get("functional_requirements")
    for item in functional if isinstance(functional, list) else []:
        if not isinstance(item, Mapping):
            continue
        fr_id = str(item.get("id") or "")
        for raw_acceptance in item.get("acceptance_criteria", []):
            if isinstance(raw_acceptance, Mapping):
                acceptance.append(
                    {
                        "id": raw_acceptance.get("id"),
                        "requirement_id": fr_id,
                        **{
                            key: deepcopy(value)
                            for key, value in raw_acceptance.items()
                            if key != "id"
                        },
                    }
                )
    return {
        "schema_version": "test-matrix-current.v1",
        "requirement_id": requirement_id,
        "source_draft_id": source_draft_id,
        "version": "test-matrix.v1",
        "is_current": True,
        "acceptance_criteria": acceptance,
    }


def _markdown_projection(title: str, document: Mapping[str, object]) -> bytes:
    text = (
        f"# {title}\n\n"
        "```json\n"
        f"{json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)}\n"
        "```\n"
    )
    return text.encode("utf-8")


def _version_document(document: Mapping[str, object]) -> dict[str, object]:
    result = deepcopy(dict(document))
    result["is_current"] = False
    return result


def _all_regular_files(staging: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(staging.rglob("*")):
        relative = path.relative_to(staging).as_posix()
        if path.is_symlink():
            raise _fail(f"prepared staging 不能包含符号链接：{relative}。")
        if path.is_file():
            if relative == "start-transaction.json":
                continue
            files[relative] = _sha256(path.read_bytes())
        elif not path.is_dir():
            raise _fail(f"prepared staging 包含不支持的文件类型：{relative}。")
    return files


def _required_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _fail(f"{label}必须是完整的 64 位小写 SHA-256。")
    return value


def _validate_prepared_target(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
) -> tuple[str, str]:
    requirement_id = transaction.get("requirement_id")
    if not isinstance(requirement_id, str) or _REQUIREMENT_ID.fullmatch(
        requirement_id
    ) is None:
        raise _fail("start-transaction.json 缺少合法的正式需求编号。")
    target_value = transaction.get("target_directory")
    if not isinstance(target_value, str):
        raise _fail("start-transaction.json 的正式目标目录必须是字符串。")
    target = _safe_relative(target_value, label="正式目标目录")
    if len(target.parts) != 1 or not (
        target.name == requirement_id
        or target.name.startswith(f"{requirement_id}-")
    ):
        raise _fail("正式目标目录必须是当前 REQ 编号对应的安全单层目录。")
    collisions = [
        item
        for item in paths.requirements_dir.iterdir()
        if item.name == requirement_id or item.name.startswith(f"{requirement_id}-")
    ]
    if collisions:
        raise _fail(f"正式需求目标已经存在、发生编号冲突或包含符号链接：{requirement_id}。")
    return requirement_id, target.name


def _validate_current_event_boundary(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
) -> None:
    event_path = paths.events_file
    if event_path.is_symlink():
        raise _fail("当前事件文件不能是符号链接。")
    if event_path.exists() and not event_path.is_file():
        raise _fail("当前事件路径必须是普通文件。")
    try:
        event_bytes = event_path.read_bytes() if event_path.exists() else b""
    except OSError as exc:
        raise _fail("当前事件文件无法读取。") from exc
    expected_size = len(event_bytes)
    expected_count = sum(1 for line in event_bytes.splitlines() if line.strip())
    expected_sha256 = _sha256(event_bytes)
    if (
        type(transaction.get("event_file_size")) is not int
        or transaction.get("event_file_size") != expected_size
        or type(transaction.get("event_count")) is not int
        or transaction.get("event_count") != expected_count
        or transaction.get("event_sha256") != expected_sha256
    ):
        raise _fail("start-transaction.json 的事件边界与当前真实事件文件不一致。")


def _validate_static_tree(
    paths: ProjectPaths,
    staging: Path,
    transaction: Mapping[str, object],
    *,
    require_prepared: bool,
) -> dict[str, object]:
    for directory in _REQUIRED_DIRECTORIES:
        target = staging / directory
        if target.is_symlink() or not target.is_dir():
            raise _fail(f"prepared staging 缺少真实目录：{directory}。")
    actual_files = _all_regular_files(staging)
    missing = sorted(_REQUIRED_GENERATED_FILES - set(actual_files))
    if missing:
        raise _fail("prepared staging 缺少正式文件：" + "、".join(missing))
    manifest = transaction.get("formal_manifest")
    if not isinstance(manifest, list):
        raise _fail("start-transaction.json 缺少正式清单。")
    expected_original = {
        str(item.get("archive_path") or "")
        for item in manifest
        if isinstance(item, Mapping)
    }
    original_files = {
        path
        for path in actual_files
        if path.startswith("original/")
        and path not in {"original/formal.v3.json", "original/artifact-index.v1.json"}
    }
    if original_files != expected_original:
        raise _fail("prepared staging 的 original 文件集合与正式清单不等值。")
    for item in manifest:
        if not isinstance(item, Mapping):
            raise _fail("start-transaction.json 的正式清单项必须是对象。")
        archive_path = str(item.get("archive_path") or "")
        if actual_files.get(archive_path) != item.get("sha256"):
            raise _fail(f"prepared staging 归档哈希不一致：{archive_path}。")
    formal = _read_json_bytes(
        (staging / "original/formal.v3.json").read_bytes(),
        label="formal.v3",
    )
    if canonical_sha256(formal.get("artifact_manifest")) != canonical_sha256(
        manifest
    ):
        raise _fail("formal.v3 与事务记录的正式清单不一致。")
    artifact_index = _read_json_bytes(
        (staging / "original/artifact-index.v1.json").read_bytes(),
        label="artifact-index.v1",
    )
    validated_index = validate_artifact_index_document(artifact_index)

    # prepared 不能只靠 transaction 自己重算事务标识来自证。这里先把事务、
    # formal 和真实 artifact-index 的独立 DRAFT 来源与修订三方绑定，再允许
    # 计算稳定 transaction_id，避免调用方同步改标识和目录名绕过正式来源。
    transaction_draft = transaction.get("source_draft_id")
    formal_draft = formal.get("source_draft_id")
    artifact_draft = validated_index.get("draft_id")
    if (
        not isinstance(transaction_draft, str)
        or re.fullmatch(r"DRAFT-[0-9]{3,}", transaction_draft) is None
        or transaction_draft != formal_draft
        or transaction_draft != artifact_draft
        or formal_draft != artifact_draft
    ):
        raise _fail("事务、formal.v3 与 artifact-index.v1 的来源 DRAFT 不一致。")
    transaction_revision = _required_sha256(
        transaction.get("source_revision_sha256"),
        label="事务来源修订",
    )
    formal_revision = _required_sha256(
        formal.get("source_revision_sha256"),
        label="formal.v3 来源修订",
    )
    artifact_revision = _required_sha256(
        validated_index.get("draft_revision_sha256"),
        label="artifact-index.v1 DRAFT 修订",
    )
    if not (
        transaction_revision == formal_revision == artifact_revision
    ):
        raise _fail("事务、formal.v3 与 artifact-index.v1 的来源修订不一致。")

    # 目标和事件边界同样必须由正式编号及当前真实事件文件独立证明，不能把
    # transaction 中格式正确但已被篡改的字段继续带入事务标识计算。
    _validate_prepared_target(paths, transaction)
    _validate_current_event_boundary(paths, transaction)
    expected_transaction_id = _transaction_id_from_records(transaction, formal)
    if transaction.get("transaction_id") != expected_transaction_id:
        raise _fail("start-transaction.json 的稳定事务标识与结构化输入不一致。")
    if not staging.name.startswith(
        f"start-{expected_transaction_id[6:22]}-"
    ):
        raise _fail("staging 目录与稳定事务标识不一致。")
    if canonical_sha256(formal_manifest_entries(validated_index)) != canonical_sha256(
        manifest
    ):
        raise _fail("artifact-index.v1 与事务记录的正式清单不一致。")
    reference = _read_json_bytes(
        (staging / "reference-index.v1.json").read_bytes(),
        label="reference-index.v1",
    )
    validate_reference_index_document(staging, reference)
    if reference.get("requirement_id") != transaction.get("requirement_id"):
        raise _fail("reference-index.v1 与事务记录的正式需求编号不一致。")
    status = _read_json_bytes(
        (staging / "status.json").read_bytes(),
        label="正式状态",
    )
    if (
        status.get("requirement_id") != transaction.get("requirement_id")
        or status.get("source_draft_id") != transaction.get("source_draft_id")
        or status.get("status") != "prepared"
    ):
        raise _fail("正式状态与事务记录不一致。")
    for name in ("requirement", "design", "test-matrix"):
        current = _read_json_bytes(
            (staging / f"effective/{name}.current.json").read_bytes(),
            label=f"{name} current",
        )
        version = _read_json_bytes(
            (staging / f"versions/{name}.v1.json").read_bytes(),
            label=f"{name} version",
        )
        if (
            current.get("requirement_id") != transaction.get("requirement_id")
            or current.get("is_current") is not True
            or version.get("requirement_id") != transaction.get("requirement_id")
            or version.get("is_current") is not False
        ):
            raise _fail(f"{name} current 与 version 指向不一致。")
        comparable = deepcopy(current)
        comparable["is_current"] = False
        if canonical_sha256(comparable) != canonical_sha256(version):
            raise _fail(f"{name} current 与初始 version 内容不一致。")
    if require_prepared:
        if transaction.get("state") != "prepared" or transaction.get("prepared") is not True:
            raise _fail("start-transaction.json 尚未标记 prepared。")
        generated = transaction.get("generated_files")
        if not isinstance(generated, Mapping) or dict(generated) != actual_files:
            raise _fail("start-transaction.json 的生成文件清单与真实目录不一致。")
    return deepcopy(dict(transaction))


def _safe_remove_current(staging_root: Path, staging: Path) -> None:
    try:
        staging.relative_to(staging_root)
    except ValueError as exc:
        raise _fail("拒绝清理暂存根目录之外的路径。") from exc
    if staging == staging_root:
        raise _fail("拒绝清理正式建档暂存根目录。")
    if staging.is_symlink():
        staging.unlink()
    elif staging.exists():
        shutil.rmtree(staging)


def _transaction_id(preflight: Mapping[str, object]) -> str:
    package = preflight.get("package")
    if not isinstance(package, Mapping):
        raise _fail("T-017 前置结果缺少真实 formal 包。")
    identity = {
        "source_draft_id": preflight.get("source_draft_id"),
        "requirement_id": preflight.get("requirement_id"),
        "target_directory": preflight.get("target_directory"),
        "source_revision_sha256": package.get("source_revision_sha256"),
        "artifact_manifest": package.get("artifact_manifest"),
    }
    return f"START-{canonical_sha256(identity)}"


def _transaction_id_from_records(
    transaction: Mapping[str, object],
    formal: Mapping[str, object],
) -> str:
    identity = {
        "source_draft_id": transaction.get("source_draft_id"),
        "requirement_id": transaction.get("requirement_id"),
        "target_directory": transaction.get("target_directory"),
        "source_revision_sha256": transaction.get("source_revision_sha256"),
        "artifact_manifest": formal.get("artifact_manifest"),
    }
    return f"START-{canonical_sha256(identity)}"


def _validate_preflight(
    paths: ProjectPaths,
    preflight: Mapping[str, object],
) -> tuple[dict[str, object], Path, str, str, str]:
    if preflight.get("mode") != "document-first":
        raise _fail("只接受 T-017 已验证的 document-first 前置结果。")
    package = preflight.get("package")
    reference = preflight.get("reference_index")
    if not isinstance(package, Mapping) or not isinstance(reference, Mapping):
        raise _fail("T-017 前置结果缺少 formal 包或 reference-index。")
    source_draft_id = str(preflight.get("source_draft_id") or "")
    requirement_id = str(preflight.get("requirement_id") or "")
    target_directory = str(preflight.get("target_directory") or "")
    if (
        package.get("formal_contract_version") != "formal.v3"
        or package.get("workflow_profile") != "document-first.v1"
        or package.get("source_draft_id") != source_draft_id
    ):
        raise _fail("T-017 前置结果与 formal.v3 显式来源不一致。")
    if _REQUIREMENT_ID.fullmatch(requirement_id) is None:
        raise _fail("T-017 前置结果缺少合法正式需求编号。")
    target = _safe_relative(target_directory, label="正式目标目录")
    if len(target.parts) != 1 or not (
        target.name == requirement_id or target.name.startswith(f"{requirement_id}-")
    ):
        raise _fail("正式目标目录必须使用当前 REQ 编号且不能包含中间路径。")
    _project, _sdlc, requirements = _require_controlled_roots(paths)
    collisions = [
        item
        for item in requirements.iterdir()
        if item.name == requirement_id or item.name.startswith(f"{requirement_id}-")
    ]
    if collisions:
        raise _fail(f"正式需求目标已经存在或编号冲突：{requirement_id}。")
    source_root = paths.draft_dir(source_draft_id)
    _require_plain_directory(source_root, label="来源 DRAFT 目录")
    try:
        source_root.resolve(strict=True).relative_to(paths.drafts_dir.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail("来源 DRAFT 目录越过受控 drafts 目录。") from exc
    return deepcopy(dict(package)), source_root, source_draft_id, requirement_id, target.name


def build_prepared_start_staging(
    paths: ProjectPaths,
    preflight: Mapping[str, object],
    *,
    fault_injector: FaultInjector | None = None,
) -> dict[str, object]:
    """只构建独立 prepared 候选；失败时删除当前目录，不碰事件、DRAFT 和正式目录。"""

    package, source_root, source_draft_id, requirement_id, target_directory = (
        _validate_preflight(paths, preflight)
    )
    staging_root = _ensure_staging_root(paths)
    transaction_id = _transaction_id(preflight)
    staging: Path | None = None
    for _attempt in range(32):
        candidate = staging_root / (
            f"start-{transaction_id[6:22]}-{secrets.token_hex(8)}"
        )
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise _fail("无法创建唯一的正式建档 staging。") from exc
        staging = candidate
        break
    if staging is None:
        raise _fail("连续发生正式建档 staging 唯一名称碰撞。")
    if _STAGING_NAME.fullmatch(staging.name) is None:
        _safe_remove_current(staging_root, staging)
        raise _fail("生成的 staging 名称不安全。")

    transaction_path = staging / "start-transaction.json"
    try:
        artifact_reference = package.get("artifact_index")
        if not isinstance(artifact_reference, Mapping):
            raise _fail("formal.v3 缺少 artifact-index.v1 引用。")
        if (
            artifact_reference.get("source_path") != "artifact-index.v1.json"
            or artifact_reference.get("archive_path")
            != "original/artifact-index.v1.json"
        ):
            raise _fail("formal.v3 的 artifact-index.v1 来源或归档路径不合法。")
        index_source_relative = _safe_relative(
            artifact_reference.get("source_path"),
            label="artifact-index source_path",
        )
        index_source = _require_no_symlink_chain(
            source_root,
            index_source_relative,
            label="artifact-index 来源",
        )
        if index_source.is_symlink() or not index_source.is_file():
            raise _fail("artifact-index.v1 来源必须是普通文件。")
        index_bytes = index_source.read_bytes()
        if _sha256(index_bytes) != artifact_reference.get("sha256"):
            raise _fail("artifact-index.v1 完整 SHA-256 与 formal.v3 不一致。")
        artifact_index = validate_artifact_index_document(
            _read_json_bytes(index_bytes, label="artifact-index.v1")
        )
        if (
            artifact_index.get("draft_id") != source_draft_id
            or artifact_index.get("draft_revision_sha256")
            != package.get("source_revision_sha256")
        ):
            raise _fail("artifact-index.v1 与 formal.v3 的 DRAFT 修订不一致。")
        manifest = _manifest(package, artifact_index)
        event_bytes = paths.events_file.read_bytes() if paths.events_file.is_file() else b""
        event_count = sum(1 for line in event_bytes.splitlines() if line.strip())
        transaction: dict[str, object] = {
            "schema_version": "start-transaction.v1",
            "transaction_id": transaction_id,
            "build_directory": staging.name,
            "source_draft_id": source_draft_id,
            "source_revision_sha256": package.get("source_revision_sha256"),
            "requirement_id": requirement_id,
            "target_directory": target_directory,
            "event_file_size": len(event_bytes),
            "event_count": event_count,
            "event_sha256": _sha256(event_bytes),
            "formal_manifest": deepcopy(manifest),
            "state": "preparing",
            "prepared": False,
        }
        _atomic_write(transaction_path, _json_bytes(transaction))
        _call_fault(fault_injector, "after_transaction_preparing", staging)

        _write_new_file(staging, "original/formal.v3.json", _json_bytes(package))
        _write_new_file(
            staging,
            "original/artifact-index.v1.json",
            index_bytes,
        )
        for index, item in enumerate(manifest):
            _copy_verified_source(
                source_root,
                staging,
                item,
                fault_injector=fault_injector,
            )
            if index == 0:
                _call_fault(fault_injector, "after_first_original", staging)

        _call_fault(fault_injector, "before_reference_index", staging)
        raw_reference = preflight.get("reference_index")
        if not isinstance(raw_reference, Mapping):
            raise _fail("T-017 前置结果缺少 reference-index.v1。")
        reference = _archive_reference_index(raw_reference, manifest)
        validate_reference_index_document(staging, reference)
        _write_new_file(
            staging,
            "reference-index.v1.json",
            _json_bytes(reference),
        )

        split_documents = _load_manifest_json(
            staging,
            manifest,
            artifact_types={"requirement_split"},
        )
        if len(split_documents) != 1:
            raise _fail("正式清单必须包含唯一 requirement-split.v1。")
        split = split_documents[0][1]
        if split.get("draft_id") != source_draft_id:
            raise _fail("requirement-split.v1 与来源 DRAFT 不一致。")
        requirement = _requirement_projection(
            requirement_id,
            source_draft_id,
            split,
            reference,
        )
        design_documents = _load_manifest_json(
            staging,
            manifest,
            artifact_types=_DESIGN_TYPES,
        )
        design = _design_projection(
            requirement_id,
            source_draft_id,
            design_documents,
        )
        test_matrix = _test_matrix_projection(
            requirement_id,
            source_draft_id,
            requirement,
        )

        _call_fault(fault_injector, "before_effective", staging)
        for name, title, document in (
            ("requirement", f"{requirement_id} 当前生效需求", requirement),
            ("design", f"{requirement_id} 当前生效技术方案", design),
            ("test-matrix", f"{requirement_id} 当前测试矩阵", test_matrix),
        ):
            _write_new_file(
                staging,
                f"effective/{name}.current.json",
                _json_bytes(document),
            )
            _write_new_file(
                staging,
                f"effective/{name}.current.md",
                _markdown_projection(title, document),
            )

        _call_fault(fault_injector, "before_versions", staging)
        for name, title, document in (
            ("requirement", f"{requirement_id} 需求版本 requirement.v1", requirement),
            ("design", f"{requirement_id} 技术方案版本 design.v1", design),
            (
                "test-matrix",
                f"{requirement_id} 测试矩阵版本 test-matrix.v1",
                test_matrix,
            ),
        ):
            version = _version_document(document)
            _write_new_file(
                staging,
                f"versions/{name}.v1.json",
                _json_bytes(version),
            )
            _write_new_file(
                staging,
                f"versions/{name}.v1.md",
                _markdown_projection(title, version),
            )
        (staging / "tasks").mkdir()

        traceability_lines = [
            f"# {requirement_id} 正式引用",
            "",
            *[
                f"- {stable_id}：{entry.get('path', '')}"
                for stable_id, entry in sorted(
                    reference.get("entries", {}).items()
                    if isinstance(reference.get("entries"), Mapping)
                    else []
                )
                if isinstance(entry, Mapping)
            ],
            "",
        ]
        _write_new_file(
            staging,
            "traceability.md",
            "\n".join(traceability_lines).encode("utf-8"),
        )
        status = {
            "schema_version": "formal-status.v1",
            "requirement_id": requirement_id,
            "source_draft_id": source_draft_id,
            "status": "prepared",
            "versions": {
                "requirement": "requirement.v1",
                "design": "design.v1",
                "test_matrix": "test-matrix.v1",
            },
            "current_files": {
                "requirement": "effective/requirement.current.json",
                "design": "effective/design.current.json",
                "test_matrix": "effective/test-matrix.current.json",
            },
        }
        _write_new_file(staging, "status.json", _json_bytes(status))

        _call_fault(fault_injector, "before_integrity_check", staging)
        _verify_source_manifest_unchanged(source_root, manifest)
        if index_source.read_bytes() != index_bytes:
            raise _fail("artifact-index.v1 在构建期间发生变化。")
        current_event_bytes = (
            paths.events_file.read_bytes() if paths.events_file.is_file() else b""
        )
        if current_event_bytes != event_bytes:
            raise _fail("事件文件在 staging 构建期间发生变化。")
        if any(
            item.name == requirement_id
            or item.name.startswith(f"{requirement_id}-")
            for item in paths.requirements_dir.iterdir()
        ):
            raise _fail("正式需求目标在 staging 构建期间发生碰撞。")
        _validate_static_tree(paths, staging, transaction, require_prepared=False)
        transaction["generated_files"] = _all_regular_files(staging)
        transaction["state"] = "prepared"
        transaction["prepared"] = True
        prepared_bytes = _json_bytes(transaction)
        temporary = transaction_path.with_name(".start-transaction.prepared.tmp")
        with temporary.open("xb") as handle:
            handle.write(prepared_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        _call_fault(fault_injector, "before_prepared_replace", staging)
        os.replace(temporary, transaction_path)
        _call_fault(fault_injector, "after_prepared_replace", staging)
        validated = validate_prepared_start_staging(paths, staging)
        return {
            "transaction_id": transaction_id,
            "staging_directory": str(staging),
            "target_directory": target_directory,
            "state": validated["state"],
        }
    except BaseException:
        # 构建目录不具备事务提交意义，任何普通失败都必须整目录删除；
        # 这样后续任务不会把残缺 original 或 preparing 记录误当成正式候选。
        _safe_remove_current(staging_root, staging)
        raise


def validate_prepared_start_staging(
    paths: ProjectPaths,
    staging_directory: Path,
) -> dict[str, object]:
    """完整复核 prepared 候选，不从目录名、Markdown 或摘要猜测成功状态。"""

    staging_root = _ensure_staging_root(paths)
    staging = Path(staging_directory)
    try:
        staging.relative_to(staging_root)
    except ValueError as exc:
        raise _fail("prepared staging 不在当前项目受控暂存区。") from exc
    if staging.parent != staging_root or _STAGING_NAME.fullmatch(staging.name) is None:
        raise _fail("prepared staging 必须是暂存根目录下的单层 start-* 目录。")
    _require_plain_directory(staging, label="prepared staging")
    transaction_path = staging / "start-transaction.json"
    if transaction_path.is_symlink() or not transaction_path.is_file():
        raise _fail("prepared staging 缺少普通 start-transaction.json。")
    transaction = _read_json_bytes(
        transaction_path.read_bytes(),
        label="start-transaction.json",
    )
    required = {
        "transaction_id",
        "build_directory",
        "source_draft_id",
        "source_revision_sha256",
        "requirement_id",
        "target_directory",
        "event_file_size",
        "event_count",
        "event_sha256",
        "formal_manifest",
        "generated_files",
        "state",
        "prepared",
    }
    if not required <= set(transaction):
        raise _fail("start-transaction.json 缺少 prepared 必需字段。")
    if transaction.get("build_directory") != staging.name:
        raise _fail("start-transaction.json 与真实 staging 目录不一致。")
    return _validate_static_tree(
        paths,
        staging,
        transaction,
        require_prepared=True,
    )


def cleanup_incomplete_start_staging(paths: ProjectPaths) -> dict[str, list[str]]:
    """只清理当前项目 start-* 中未完成或损坏的候选，完整 prepared 必须保留。"""

    staging_root = _ensure_staging_root(paths)
    removed: list[str] = []
    kept: list[str] = []
    for candidate in sorted(staging_root.iterdir()):
        if not candidate.name.startswith("start-"):
            continue
        if candidate.is_symlink():
            candidate.unlink()
            removed.append(str(candidate))
            continue
        if not candidate.is_dir():
            candidate.unlink()
            removed.append(str(candidate))
            continue
        try:
            validate_prepared_start_staging(paths, candidate)
        except SdlcError:
            # 恢复只信结构化事务和真实目录；transaction 缺失、损坏或仍为
            # preparing 都不能消费，删除后才能保证下一次构建从干净候选开始。
            _safe_remove_current(staging_root, candidate)
            removed.append(str(candidate))
        else:
            kept.append(str(candidate))
    return {"removed": removed, "kept": kept}


__all__ = [
    "build_prepared_start_staging",
    "cleanup_incomplete_start_staging",
    "validate_prepared_start_staging",
]
