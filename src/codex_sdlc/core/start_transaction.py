from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Callable, Mapping

from codex_sdlc.core.artifact_index import (
    formal_manifest_entries,
    validate_artifact_index_document,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, project_lock
from codex_sdlc.core.reference_index import validate_reference_index_document
from codex_sdlc.core.start_staging import validate_prepared_start_staging
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
)


FaultInjector = Callable[[str, Path], None]
_TRANSACTION_ID = re.compile(r"^START-[0-9a-f]{64}$")
_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3,}$")


class _StartTransactionEvidenceError(SdlcError):
    """事务归属无法由正式文件证明时，必须保留现场而不是尝试回滚。"""


def _fail(message: str) -> SdlcError:
    return SdlcError(message, exit_code=1)


def _evidence_fail(message: str) -> _StartTransactionEvidenceError:
    return _StartTransactionEvidenceError(message, exit_code=1)


def _transactions_root(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "start-transactions"


def _active_root(paths: ProjectPaths) -> Path:
    return _transactions_root(paths) / "active"


def _completed_root(paths: ProjectPaths) -> Path:
    return _transactions_root(paths) / "completed"


def _ensure_roots(paths: ProjectPaths) -> tuple[Path, Path]:
    root = _transactions_root(paths)
    if root.exists() or root.is_symlink():
        try:
            root_mode = root.lstat().st_mode
        except OSError as exc:
            raise _fail(f"建档事务根目录无法访问：{root}。") from exc
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            raise _fail(f"建档事务根目录必须是真实目录：{root}。")
    else:
        root.mkdir(mode=0o700)
    active = _active_root(paths)
    completed = _completed_root(paths)
    for directory in (active, completed):
        if directory.exists() or directory.is_symlink():
            try:
                mode = directory.lstat().st_mode
            except OSError as exc:
                raise _fail(f"建档事务目录无法访问：{directory}。") from exc
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise _fail(f"建档事务目录必须是真实目录：{directory}。")
        else:
            directory.mkdir(parents=True, mode=0o700)
    return active, completed


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    content = canonical_json_text(document).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise _fail(f"建档事务临时文件发生碰撞：{temporary.name}。")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _load_json_file(path: Path, *, label: str) -> dict[str, object]:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise _fail(f"{label}不存在或无法读取：{path}。") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise _fail(f"{label}必须是普通文件：{path}。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(f"{label}不是有效的 UTF-8 JSON：{path}。") from exc
    if not isinstance(value, dict):
        raise _fail(f"{label}顶层必须是 JSON 对象：{path}。")
    return value


def _active_files(paths: ProjectPaths) -> list[Path]:
    transactions = _transactions_root(paths)
    if transactions.is_symlink() or (
        transactions.exists() and not transactions.is_dir()
    ):
        raise _fail("建档事务根目录损坏，已停止继续写入。")
    root = _active_root(paths)
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise _fail("建档事务活动目录损坏，已停止继续写入。")
    return sorted(root.iterdir())


def require_no_unrecovered_start_transaction(paths: ProjectPaths) -> None:
    """备份只能读取已经恢复完成的项目，损坏记录也不能静默跳过。"""

    active = _active_files(paths)
    if active:
        names = "、".join(path.name for path in active[:5])
        raise _fail(
            "存在尚未恢复的正式建档事务，不能备份。"
            f"请先运行任意 codex-sdlc 命令触发恢复：{names}。"
        )


def _event_bytes(events: object) -> bytes:
    if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
        raise _fail("建档事务缺少结构化事件列表。")
    return b"".join(
        canonical_json_text(item).encode("utf-8")
        for item in events
    )


def _event_position(paths: ProjectPaths, transaction: Mapping[str, object]) -> str:
    try:
        start = int(transaction["event_start_size"])
        start_sha = str(transaction["event_start_sha256"])
        expected = _event_bytes(transaction.get("events"))
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("建档事务缺少有效事件字节边界。") from exc
    if start < 0 or len(start_sha) != 64:
        raise _fail("建档事务的事件起始边界不合法。")
    path = paths.events_file
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _fail("事件文件不是普通文件，无法恢复建档事务。")
    current = path.read_bytes() if path.exists() else b""
    prefix = current[:start]
    if (
        len(current) < start
        or sha256_bytes(prefix) != start_sha
        or sum(1 for line in prefix.splitlines() if line.strip())
        != transaction.get("event_start_count")
    ):
        raise _fail("事件文件起始字节与建档事务记录不一致，已停止自动恢复。")
    suffix = current[start:]
    if suffix == b"":
        return "not_appended"
    if suffix == expected:
        return "appended"
    if expected.startswith(suffix):
        return "partially_appended"
    raise _fail("事件文件结束边界与建档事务记录不一致，已停止自动恢复。")


def _append_events(paths: ProjectPaths, transaction: Mapping[str, object]) -> None:
    expected = _event_bytes(transaction.get("events"))
    start = int(transaction["event_start_size"])
    position = _event_position(paths, transaction)
    if position == "appended":
        return
    # 进程可能在单次 write 中间被强制结束。只有真实后缀是预期事件前缀时
    # 才允许先截回已记录边界，再一次性补齐，其他内容一律拒绝猜测。
    with paths.events_file.open("r+b") as handle:
        handle.truncate(start)
        handle.seek(start)
        handle.write(expected)
        handle.flush()
        os.fsync(handle.fileno())
    if _event_position(paths, transaction) != "appended":
        raise _fail("正式建档事件写入后边界复核失败。")


def _truncate_events(paths: ProjectPaths, transaction: Mapping[str, object]) -> None:
    position = _event_position(paths, transaction)
    if position == "not_appended":
        return
    start = int(transaction["event_start_size"])
    with paths.events_file.open("r+b") as handle:
        handle.truncate(start)
        handle.flush()
        os.fsync(handle.fileno())
    if _event_position(paths, transaction) != "not_appended":
        raise _fail("正式建档事件回滚后边界复核失败。")


def _all_regular_files(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise _fail(f"正式建档目录不是普通目录：{root}。")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise _fail(f"正式建档目录不能包含符号链接：{relative}。")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise _fail(f"正式建档目录只能包含普通文件：{relative}。")
        result[relative] = sha256_bytes(path.read_bytes())
    return result


def _expected_prepared_files(transaction: Mapping[str, object]) -> dict[str, str]:
    generated = transaction.get("generated_files")
    if not isinstance(generated, Mapping):
        raise _fail("建档事务缺少 prepared 文件哈希清单。")
    result = {str(key): str(value) for key, value in generated.items()}
    # start-transaction.json 会在每一步更新，T-018 的 generated_files 有意
    # 不把它纳入静态正式文件哈希；事务文件由活动日志单独保护。
    if "status.json" not in result or "original/formal.v3.json" not in result:
        raise _fail("建档事务的 prepared 文件清单不完整。")
    return result


def _committed_status(transaction: Mapping[str, object], directory: Path) -> bytes:
    status = _load_json_file(directory / "status.json", label="正式状态文件")
    status["status"] = "active"
    status["transaction_id"] = transaction["transaction_id"]
    status["event_start_size"] = transaction["event_start_size"]
    status["event_end_size"] = transaction["event_end_size"]
    return canonical_json_text(status).encode("utf-8")


def _validate_owned_tree(
    directory: Path,
    transaction: Mapping[str, object],
    *,
    allow_prepared_status: bool,
) -> dict[str, str]:
    actual = _all_regular_files(directory)
    expected = _expected_prepared_files(transaction)
    allowed_names = set(expected)
    actual_names = set(actual)
    actual_names.discard("start-transaction.json")
    if actual_names != allowed_names:
        raise _fail("正式目录文件集合与建档事务不一致，已保留现场。")
    for relative, digest in expected.items():
        if relative in {"start-transaction.json", "status.json"}:
            continue
        if actual.get(relative) != digest:
            raise _fail(f"正式目录文件哈希与建档事务不一致：{relative}。")
    formal = _load_json_file(
        directory / "original/formal.v3.json",
        label="正式原文",
    )
    if canonical_sha256(formal) != transaction.get("formal_sha256"):
        raise _fail("正式目录 original/formal.v3.json 与建档事务不一致。")
    prepared_status = expected.get("status.json")
    active_status = sha256_bytes(_committed_status(transaction, directory))
    if actual.get("status.json") not in (
        {prepared_status, active_status} if allow_prepared_status else {active_status}
    ):
        raise _fail("正式目录状态文件不是事务允许的 prepared 或 active 状态。")
    return actual


def _safe_staging_path(paths: ProjectPaths, transaction: Mapping[str, object]) -> Path:
    raw = str(transaction.get("staging_directory") or "")
    candidate = Path(raw)
    try:
        candidate.relative_to(paths.start_staging_root)
    except ValueError as exc:
        raise _fail("建档事务记录的 staging 路径越过当前项目。") from exc
    if candidate.parent != paths.start_staging_root:
        raise _fail("建档事务记录的 staging 必须是暂存根目录下的单层目录。")
    return candidate


def _target_path(paths: ProjectPaths, transaction: Mapping[str, object]) -> Path:
    requirement_id = str(transaction.get("requirement_id") or "")
    name = str(transaction.get("target_directory") or "")
    if (
        _REQUIREMENT_ID.fullmatch(requirement_id) is None
        or Path(name).name != name
        or name in {"", ".", ".."}
        or not (name == requirement_id or name.startswith(f"{requirement_id}-"))
    ):
        raise _fail("建档事务记录的正式目标目录不合法。")
    return paths.requirements_dir / name


def _stable_transaction_id(identity: Mapping[str, object]) -> str:
    """只用正式对象重新得到稳定编号，不使用活动记录自报的编号。"""

    return f"START-{canonical_sha256(identity)}"


def _require_same_field(
    documents: Mapping[str, Mapping[str, object]],
    field: str,
    *,
    label: str,
) -> str:
    values = {
        str(document.get(field) or "")
        for document in documents.values()
    }
    if len(values) != 1 or not next(iter(values)):
        sources = "、".join(documents)
        raise _evidence_fail(f"{sources}中的{label}不一致，已保留建档事务现场。")
    return next(iter(values))


def _formal_identity(directory: Path, *, target_directory: str) -> dict[str, object]:
    """从互相独立的正式文件得出身份，不能让活动日志自己证明自己。"""

    formal = _load_json_file(
        directory / "original/formal.v3.json",
        label="正式原文",
    )
    artifact = validate_artifact_index_document(
        _load_json_file(
            directory / "original/artifact-index.v1.json",
            label="正式 artifact-index",
        )
    )
    reference = _load_json_file(
        directory / "reference-index.v1.json",
        label="正式引用索引",
    )
    validate_reference_index_document(directory, reference)
    status = _load_json_file(directory / "status.json", label="正式状态文件")
    projections: dict[str, dict[str, object]] = {}
    versions: dict[str, dict[str, object]] = {}
    for name in ("requirement", "design", "test-matrix"):
        current = _load_json_file(
            directory / f"effective/{name}.current.json",
            label=f"{name} 当前版本",
        )
        version = _load_json_file(
            directory / f"versions/{name}.v1.json",
            label=f"{name} 初始版本",
        )
        comparable = deepcopy(current)
        comparable["is_current"] = False
        if (
            current.get("is_current") is not True
            or version.get("is_current") is not False
            or canonical_sha256(comparable) != canonical_sha256(version)
        ):
            raise _evidence_fail(
                f"{name} 的 current 与初始 version 关系不一致，已保留建档事务现场。"
            )
        projections[name] = current
        versions[name] = version

    requirement_id = _require_same_field(
        {
            "reference-index": reference,
            "status": status,
            **{f"effective/{name}": value for name, value in projections.items()},
            **{f"versions/{name}": value for name, value in versions.items()},
        },
        "requirement_id",
        label="正式需求编号",
    )
    if (
        _REQUIREMENT_ID.fullmatch(requirement_id) is None
        or not (
            target_directory == requirement_id
            or target_directory.startswith(f"{requirement_id}-")
        )
    ):
        raise _evidence_fail("正式目录名与结构化 REQ 归属不一致，已保留建档事务现场。")

    source_draft_id = _require_same_field(
        {
            "formal.v3": formal,
            "artifact-index": {"source_draft_id": artifact.get("draft_id")},
            "status": status,
            **{f"effective/{name}": value for name, value in projections.items()},
            **{f"versions/{name}": value for name, value in versions.items()},
        },
        "source_draft_id",
        label="来源 DRAFT",
    )
    if re.fullmatch(r"DRAFT-[0-9]{3,}", source_draft_id) is None:
        raise _evidence_fail("正式对象缺少合法来源 DRAFT，已保留建档事务现场。")
    source_revision = _require_same_field(
        {
            "formal.v3": formal,
            "artifact-index": {
                "source_revision_sha256": artifact.get("draft_revision_sha256")
            },
        },
        "source_revision_sha256",
        label="来源修订",
    )
    manifest = formal.get("artifact_manifest")
    if (
        not isinstance(manifest, list)
        or canonical_sha256(manifest)
        != canonical_sha256(formal_manifest_entries(artifact))
    ):
        raise _evidence_fail(
            "formal.v3 与 artifact-index 的正式清单不一致，已保留建档事务现场。"
        )
    identity = {
        "source_draft_id": source_draft_id,
        "requirement_id": requirement_id,
        "target_directory": target_directory,
        "source_revision_sha256": source_revision,
        "artifact_manifest": manifest,
    }
    return {
        **identity,
        "transaction_id": _stable_transaction_id(identity),
        "formal_sha256": canonical_sha256(formal),
    }


def _validate_transaction_events(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    identity: Mapping[str, object],
) -> None:
    events = transaction.get("events")
    if (
        not isinstance(events, list)
        or len(events) != 2
        or not all(isinstance(event, Mapping) for event in events)
    ):
        raise _evidence_fail("建档事务必须恰好包含两条结构化正式事件。")
    created, started = events
    created_payload = created.get("payload")
    started_payload = started.get("payload")
    if not isinstance(created_payload, Mapping) or not isinstance(
        started_payload, Mapping
    ):
        raise _evidence_fail("建档事务事件缺少结构化 payload，已保留现场。")
    requirement_id = identity["requirement_id"]
    draft_id = identity["source_draft_id"]
    target_directory = identity["target_directory"]
    expected_root = str(paths.root)
    if (
        created.get("event_type") != "requirement_created"
        or started.get("event_type") != "draft_started"
        or created.get("project_path") != expected_root
        or started.get("project_path") != expected_root
        or created.get("requirement_id") != requirement_id
        or started.get("requirement_id") != requirement_id
        or created_payload.get("folder_name") != target_directory
        or started_payload.get("draft_id") != draft_id
        or started_payload.get("started_requirement_id") != requirement_id
    ):
        raise _evidence_fail(
            "建档事件与真实项目、REQ、DRAFT 或正式目录归属不一致，已保留现场。"
        )


def _validate_transaction_binding(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    *,
    completed: bool = False,
) -> tuple[Path, dict[str, object]]:
    """恢复和回执读取前重建身份；公开哈希只校验字节，不能替代归属证明。"""

    try:
        staging = _safe_staging_path(paths, transaction)
        target = _target_path(paths, transaction)
        if str(target) != str(transaction.get("formal_directory") or ""):
            raise _evidence_fail("建档事务的正式目录路径与当前项目不一致。")
        staging_exists = staging.exists() or staging.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if completed:
            if staging_exists or not target_exists:
                raise _evidence_fail("已完成回执没有唯一正式目录归属。")
            directory = target
        elif staging_exists == target_exists:
            raise _evidence_fail(
                "活动事务必须且只能保有一个 staging 或正式目录，已保留现场。"
            )
        else:
            directory = staging if staging_exists else target
        if directory.is_symlink() or not directory.is_dir():
            raise _evidence_fail("建档事务取证目录必须是真实目录。")

        embedded_path = directory / "start-transaction.json"
        embedded: dict[str, object] | None = None
        if embedded_path.exists() or embedded_path.is_symlink():
            embedded = _load_json_file(embedded_path, label="prepared 事务记录")
        if directory == staging and embedded is None:
            raise _evidence_fail("staging 缺少独立 prepared 事务记录，已保留现场。")

        target_name = (
            str(embedded.get("target_directory") or "")
            if embedded is not None
            else target.name
        )
        identity = _formal_identity(directory, target_directory=target_name)
        fields = (
            "transaction_id",
            "requirement_id",
            "source_draft_id",
            "source_revision_sha256",
            "target_directory",
            "formal_sha256",
        )
        if any(transaction.get(field) != identity.get(field) for field in fields):
            raise _evidence_fail(
                "活动事务或完成回执与真实正式对象身份不一致，已保留现场。"
            )
        if canonical_sha256(transaction.get("formal_manifest")) != canonical_sha256(
            identity.get("artifact_manifest")
        ):
            raise _evidence_fail(
                "事务正式清单与真实 formal、artifact-index 不一致，已保留现场。"
            )
        if target != paths.requirements_dir / str(identity["target_directory"]):
            raise _evidence_fail("事务目标目录不是正式对象对应的受控目录。")
        if embedded is not None:
            embedded_fields = (
                "transaction_id",
                "requirement_id",
                "source_draft_id",
                "source_revision_sha256",
                "target_directory",
                "formal_manifest",
                "generated_files",
            )
            if (
                embedded.get("build_directory") != staging.name
                or str(transaction.get("staging_directory") or "") != str(staging)
                or any(
                    embedded.get(field) != transaction.get(field)
                    for field in embedded_fields
                )
            ):
                raise _evidence_fail(
                    "活动事务与 prepared staging 的独立记录不一致，已保留现场。"
                )
        _validate_transaction_events(paths, transaction, identity)
        events = transaction.get("events")
        event_bytes = _event_bytes(events)
        start = int(transaction["event_start_size"])
        start_count = int(transaction["event_start_count"])
        if (
            transaction.get("event_append_size") != len(event_bytes)
            or transaction.get("event_append_sha256") != sha256_bytes(event_bytes)
            or transaction.get("event_end_size") != start + len(event_bytes)
            or transaction.get("event_end_count")
            != start_count + len(events if isinstance(events, list) else [])
        ):
            raise _evidence_fail("建档事务事件公开边界字段互相不一致，已保留现场。")
        return directory, identity
    except _StartTransactionEvidenceError:
        raise
    except (SdlcError, KeyError, TypeError, ValueError, OSError) as exc:
        raise _evidence_fail(
            f"无法从 staging 或正式目录证明建档事务归属，已保留现场：{exc}"
        ) from exc


def _call_fault(
    fault_injector: FaultInjector | None,
    point: str,
    transaction_path: Path,
) -> None:
    if fault_injector is not None:
        fault_injector(point, transaction_path)


def _update_transaction(path: Path, transaction: dict[str, object], state: str) -> None:
    transaction["state"] = state
    transaction["last_confirmed_step"] = state
    _atomic_write_json(path, transaction)


def _mark_status_active(
    directory: Path,
    transaction: Mapping[str, object],
) -> None:
    content = _committed_status(transaction, directory)
    path = directory / "status.json"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _receipt_result(receipt: Mapping[str, object]) -> dict[str, object]:
    return {
        "transaction_id": receipt["transaction_id"],
        "requirement_id": receipt["requirement_id"],
        "target_directory": receipt["target_directory"],
        "formal_directory": receipt["formal_directory"],
        "state": "completed",
        "idempotent": True,
    }


def _receipt_path(paths: ProjectPaths, transaction_id: str) -> Path:
    _active, completed = _ensure_roots(paths)
    return completed / f"{transaction_id}.json"


def _validate_receipt(
    paths: ProjectPaths,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    transaction_id = str(receipt.get("transaction_id") or "")
    if _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise _fail("已完成建档回执缺少合法事务编号。")
    _validate_transaction_binding(paths, receipt, completed=True)
    target = _target_path(paths, receipt)
    if str(target) != str(receipt.get("formal_directory") or ""):
        raise _fail("已完成建档回执的正式目录归属不一致。")
    _validate_owned_tree(target, receipt, allow_prepared_status=False)
    try:
        start = int(receipt["event_start_size"])
        end = int(receipt["event_end_size"])
        expected = _event_bytes(receipt.get("events"))
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("已完成建档回执缺少事件起止位置。") from exc
    current = paths.events_file.read_bytes()
    if (
        end != start + len(expected)
        or len(current) < end
        or sha256_bytes(current[:start]) != receipt.get("event_start_sha256")
        or current[start:end] != expected
        or sha256_bytes(current[:end]) != receipt.get("event_end_sha256")
        or sum(1 for line in current[:end].splitlines() if line.strip())
        != receipt.get("event_end_count")
    ):
        raise _fail("已完成建档回执的事件结束边界不一致。")
    return _receipt_result(receipt)


def find_completed_start(
    paths: ProjectPaths,
    formal_package: Mapping[str, object],
) -> dict[str, object] | None:
    """用完整正式包哈希查找幂等回执，不从标题或目录名猜测结果。"""

    completed = _completed_root(paths)
    if not completed.exists():
        return None
    if completed.is_symlink() or not completed.is_dir():
        raise _fail("已完成建档回执目录损坏，不能判断幂等结果。")
    package_sha = canonical_sha256(formal_package)
    source_draft_id = formal_package.get("source_draft_id")
    source_revision = formal_package.get("source_revision_sha256")
    matches: list[dict[str, object]] = []
    for path in sorted(completed.glob("START-*.json")):
        receipt = _load_json_file(path, label="已完成建档回执")
        if path.name != f"{receipt.get('transaction_id')}.json":
            raise _evidence_fail("已完成建档回执编号与文件名不一致。")
        # 查找时也先校验每一份回执，不能通过改掉匹配字段让损坏回执
        # 从候选列表中消失，再继续创建第二份正式结果。
        _validate_receipt(paths, receipt)
        if (
            receipt.get("formal_sha256") == package_sha
            and receipt.get("source_draft_id") == source_draft_id
            and receipt.get("source_revision_sha256") == source_revision
        ):
            matches.append(receipt)
    if not matches:
        return None
    if len(matches) != 1:
        raise _fail("同一正式包对应多个建档回执，已停止幂等返回。")
    return _validate_receipt(paths, matches[0])


def _build_events(
    paths: ProjectPaths,
    prepared: Mapping[str, object],
    formal: Mapping[str, object],
    requirement: Mapping[str, object],
) -> list[dict[str, object]]:
    from codex_sdlc.core.state import load_events, next_event_id, now_iso

    current_events = load_events(paths)
    first_id = next_event_id(current_events)
    created_at = now_iso()
    native_start = deepcopy(dict(formal))
    native_start["migration_status"] = "native"
    native_start["requirement_points"] = deepcopy(
        formal.get("functional_requirements", [])
    )
    native_start["acceptance_points"] = deepcopy(
        formal.get("acceptance_criteria", [])
    )
    native_start["test_cases"] = deepcopy(formal.get("test_cases", []))
    first: dict[str, object] = {
        "event_id": first_id,
        "event_type": "requirement_created",
        "project_path": str(paths.root),
        "requirement_id": prepared["requirement_id"],
        "task_id": None,
        "created_at": created_at,
        "source": "sdlc-start",
        "summary": f"创建正式需求 {prepared['requirement_id']}",
        "payload": {
            "title": requirement.get("title") or formal.get("title") or prepared["requirement_id"],
            "description": formal.get("description") or requirement.get("goal") or "",
            "summary": formal.get("summary") or requirement.get("title") or prepared["requirement_id"],
            "folder_name": prepared["target_directory"],
            "flow_type": "SDLC document-first 正式流程",
            "native_start": native_start,
        },
    }
    current_events.append(first)
    second: dict[str, object] = {
        "event_id": next_event_id(current_events),
        "event_type": "draft_started",
        "project_path": str(paths.root),
        "requirement_id": prepared["requirement_id"],
        "task_id": None,
        "created_at": now_iso(),
        "source": "sdlc-start",
        "summary": (
            f"{prepared['source_draft_id']} 已生成正式需求 "
            f"{prepared['requirement_id']}"
        ),
        "payload": {
            "draft_id": prepared["source_draft_id"],
            "started_requirement_id": prepared["requirement_id"],
        },
    }
    return [first, second]


def _new_transaction(
    paths: ProjectPaths,
    staging: Path,
    prepared: Mapping[str, object],
) -> dict[str, object]:
    formal = _load_json_file(
        staging / "original/formal.v3.json",
        label="prepared 正式原文",
    )
    requirement = _load_json_file(
        staging / "effective/requirement.current.json",
        label="prepared 当前需求",
    )
    events = _build_events(paths, prepared, formal, requirement)
    event_content = _event_bytes(events)
    event_start_bytes = paths.events_file.read_bytes()
    start_size = int(prepared["event_file_size"])
    start_sha = str(prepared["event_sha256"])
    return {
        **deepcopy(dict(prepared)),
        "schema_version": "start-transaction.v1",
        "staging_directory": str(staging),
        "formal_directory": str(_target_path(paths, prepared)),
        "formal_sha256": canonical_sha256(formal),
        "events": events,
        "event_start_size": start_size,
        "event_start_count": int(prepared["event_count"]),
        "event_start_sha256": start_sha,
        "event_append_size": len(event_content),
        "event_append_sha256": sha256_bytes(event_content),
        "event_end_size": start_size + len(event_content),
        "event_end_count": int(prepared["event_count"]) + len(events),
        "event_end_sha256": sha256_bytes(event_start_bytes + event_content),
        "state": "prepared",
        "last_confirmed_step": "prepared",
    }


def _complete_active(
    paths: ProjectPaths,
    transaction_path: Path,
    transaction: dict[str, object],
    *,
    fault_injector: FaultInjector | None,
) -> dict[str, object]:
    from codex_sdlc.core.state import refresh_start_transaction_state

    # 即使事件字节和全部公开哈希都被同步重算，也必须先用独立正式文件
    # 重建事务身份；归属不清时不能进入投影、回滚或回执清理。
    _validate_transaction_binding(paths, transaction)
    receipt_path = _receipt_path(paths, str(transaction["transaction_id"]))
    if receipt_path.exists():
        receipt = _load_json_file(receipt_path, label="已完成建档回执")
        result = _validate_receipt(paths, receipt)
        transaction_path.unlink(missing_ok=True)
        _fsync_directory(transaction_path.parent)
        return result

    staging = _safe_staging_path(paths, transaction)
    target = _target_path(paths, transaction)
    event_position = _event_position(paths, transaction)
    if event_position == "not_appended":
        if not staging.is_dir() or target.exists() or target.is_symlink():
            raise _fail("prepared 事务的目录边界不一致，已停止恢复。")
        _call_fault(fault_injector, "before_events_append", transaction_path)
        _append_events(paths, transaction)
        _call_fault(fault_injector, "after_events_append", transaction_path)
    elif event_position == "partially_appended":
        _append_events(paths, transaction)
        _call_fault(fault_injector, "after_events_append", transaction_path)
    _update_transaction(transaction_path, transaction, "events_appended")

    if target.exists() or target.is_symlink():
        if staging.exists() or staging.is_symlink():
            raise _fail("staging 与正式目录同时存在，无法唯一判断建档目录归属。")
        _validate_owned_tree(target, transaction, allow_prepared_status=True)
    else:
        if not staging.is_dir() or staging.is_symlink():
            raise _fail("事件已追加，但找不到唯一 prepared staging。")
        # 事件追加后 T-018 校验会因为边界变化而拒绝，所以这里改用事务内已经
        # 固定的逐文件哈希确认目录归属，不能降低成只看状态文字。
        actual = _all_regular_files(staging)
        actual.pop("start-transaction.json", None)
        if actual != _expected_prepared_files(transaction):
            raise _fail("事件追加后的 staging 与 prepared 文件哈希清单不一致。")
        os.replace(staging, target)
        _fsync_directory(paths.requirements_dir)
        _call_fault(fault_injector, "after_directory_commit", transaction_path)
    _update_transaction(transaction_path, transaction, "directory_committed")

    _call_fault(fault_injector, "before_projection_refresh", transaction_path)
    refresh_start_transaction_state(
        paths,
        committed_requirement_id=str(transaction["requirement_id"]),
    )
    _call_fault(fault_injector, "during_projection_refresh", transaction_path)
    # 对外事务状态只使用任务合同约定的四个值。投影已经刷新属于步骤证据，
    # 不能把它写成第五种 state，否则后续读取方会遇到未知状态。
    transaction["last_confirmed_step"] = "projection_refreshed"
    _atomic_write_json(transaction_path, transaction)

    _call_fault(fault_injector, "before_integrity_check", transaction_path)
    _mark_status_active(target, transaction)
    (target / "start-transaction.json").unlink(missing_ok=True)
    _fsync_directory(target)
    final_files = _validate_owned_tree(
        target,
        transaction,
        allow_prepared_status=False,
    )
    transaction["committed_files"] = final_files
    _call_fault(fault_injector, "after_integrity_check", transaction_path)
    _update_transaction(transaction_path, transaction, "completed")

    receipt = deepcopy(transaction)
    receipt["completed"] = True
    receipt["state"] = "completed"
    _atomic_write_json(receipt_path, receipt)
    result = _validate_receipt(paths, receipt)
    transaction_path.unlink(missing_ok=True)
    _fsync_directory(transaction_path.parent)
    result["idempotent"] = False
    return result


def _remove_owned_directory(
    directory: Path,
    transaction: Mapping[str, object],
) -> None:
    if not directory.exists() and not directory.is_symlink():
        return
    _validate_owned_tree(directory, transaction, allow_prepared_status=True)
    shutil.rmtree(directory)
    _fsync_directory(directory.parent)


def _rollback_active(
    paths: ProjectPaths,
    transaction_path: Path,
    transaction: dict[str, object],
) -> None:
    from codex_sdlc.core.state import refresh_start_transaction_rollback_state

    staging = _safe_staging_path(paths, transaction)
    target = _target_path(paths, transaction)
    _truncate_events(paths, transaction)
    if target.exists() or target.is_symlink():
        _remove_owned_directory(target, transaction)
    if staging.exists() or staging.is_symlink():
        actual = _all_regular_files(staging)
        actual.pop("start-transaction.json", None)
        if actual != _expected_prepared_files(transaction):
            raise _fail("回滚时 staging 文件哈希不一致，已保留事务取证。")
        shutil.rmtree(staging)
        _fsync_directory(staging.parent)
    refresh_start_transaction_rollback_state(paths)
    transaction_path.unlink(missing_ok=True)
    _fsync_directory(transaction_path.parent)


def _resolve_submission(
    paths: ProjectPaths,
    submission: Path | Mapping[str, object],
) -> tuple[Path | None, str | None]:
    if isinstance(submission, Mapping):
        raw = submission.get("staging_directory")
        transaction_id = str(submission.get("transaction_id") or "")
        return (Path(str(raw)) if raw else None), transaction_id or None
    return Path(submission), None


def _completed_for_submission(
    paths: ProjectPaths,
    staging: Path | None,
    transaction_id: str | None,
) -> dict[str, object] | None:
    candidates: list[Path] = []
    completed = _completed_root(paths)
    if transaction_id and _TRANSACTION_ID.fullmatch(transaction_id):
        candidates = [completed / f"{transaction_id}.json"]
    elif staging is not None:
        candidates = sorted(completed.glob("START-*.json")) if completed.exists() else []
    for path in candidates:
        if not path.exists():
            continue
        receipt = _load_json_file(path, label="已完成建档回执")
        if path.name != f"{receipt.get('transaction_id')}.json":
            raise _evidence_fail("已完成建档回执编号与文件名不一致。")
        validated = _validate_receipt(paths, receipt)
        if staging is None or receipt.get("staging_directory") == str(staging):
            return validated
    return None


def commit_prepared_start(
    paths: ProjectPaths,
    submission: Path | Mapping[str, object],
    *,
    fault_injector: FaultInjector | None = None,
) -> dict[str, object]:
    """提交 prepared 候选；同一事务再次提交只返回同一正式回执。"""

    with project_lock(paths):
        _ensure_roots(paths)
        staging, transaction_id = _resolve_submission(paths, submission)
        completed = _completed_for_submission(paths, staging, transaction_id)
        if completed is not None:
            return completed
        if staging is None:
            raise _fail("提交 prepared 建档事务时缺少 staging 路径。")
        prepared = validate_prepared_start_staging(paths, staging)
        active_path = _active_root(paths) / f"{prepared['transaction_id']}.json"
        unrelated = [
            path
            for path in _active_files(paths)
            if path != active_path
        ]
        if unrelated:
            raise _fail("存在其他尚未恢复的建档事务，不能开始新的正式建档。")
        if active_path.exists():
            transaction = _load_json_file(active_path, label="活动建档事务")
        else:
            transaction = _new_transaction(paths, staging, prepared)
            _atomic_write_json(active_path, transaction)
        try:
            return _complete_active(
                paths,
                active_path,
                transaction,
                fault_injector=fault_injector,
            )
        except _StartTransactionEvidenceError:
            raise
        except Exception as exc:
            try:
                _rollback_active(paths, active_path, transaction)
            except Exception as rollback_exc:
                raise _fail(
                    "正式建档失败且自动回滚没有完成，事务记录和目录已保留。"
                    f"原始错误：{exc}；回滚错误：{rollback_exc}"
                ) from rollback_exc
            if isinstance(exc, SdlcError):
                raise
            raise _fail(f"正式建档失败，已完整回滚：{exc}") from exc


def recover_incomplete_start_transactions(paths: ProjectPaths) -> list[dict[str, object]]:
    """在普通命令、身份检查和自动备份之前恢复活动建档事务。"""

    if not paths.sdlc_dir.is_dir():
        return []
    with project_lock(paths):
        active_files = _active_files(paths)
        results: list[dict[str, object]] = []
        for path in active_files:
            if path.is_symlink() or not path.is_file() or not path.name.endswith(".json"):
                raise _fail(f"发现损坏的活动建档事务记录：{path.name}。")
            transaction = _load_json_file(path, label="活动建档事务")
            transaction_id = str(transaction.get("transaction_id") or "")
            if (
                _TRANSACTION_ID.fullmatch(transaction_id) is None
                or path.name != f"{transaction_id}.json"
            ):
                raise _fail(f"活动建档事务编号与文件名不一致：{path.name}。")
            _validate_transaction_binding(paths, transaction)
            receipt_path = _receipt_path(paths, transaction_id)
            if receipt_path.exists():
                receipt = _load_json_file(receipt_path, label="已完成建档回执")
                results.append(_validate_receipt(paths, receipt))
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
                continue
            position = _event_position(paths, transaction)
            if position == "not_appended":
                # 事务还没有越过事件提交边界，恢复为完整失败最安全；prepared
                # 候选只是一份可重建副本，不应在下一条普通命令里突然完成建档。
                _rollback_active(paths, path, transaction)
                results.append(
                    {
                        "transaction_id": transaction_id,
                        "state": "rolled_back",
                        "requirement_id": transaction.get("requirement_id"),
                    }
                )
                continue
            try:
                results.append(
                    _complete_active(
                        paths,
                        path,
                        transaction,
                        fault_injector=None,
                    )
                )
            except _StartTransactionEvidenceError:
                raise
            except Exception as exc:
                try:
                    _rollback_active(paths, path, transaction)
                except Exception as rollback_exc:
                    raise _fail(
                        "未完成建档事务恢复失败，且无法安全回滚，已阻止后续命令。"
                        f"恢复错误：{exc}；回滚错误：{rollback_exc}"
                    ) from rollback_exc
                raise _fail(f"未完成建档事务恢复失败，已完整回滚：{exc}") from exc
        return results


__all__ = [
    "commit_prepared_start",
    "find_completed_start",
    "recover_incomplete_start_transactions",
    "require_no_unrecovered_start_transaction",
]
