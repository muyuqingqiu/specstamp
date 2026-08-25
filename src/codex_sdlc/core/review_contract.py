from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Iterable, Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    validate_schema_document,
)


REVIEW_REQUEST_SCHEMA = "review-request.v1"
REVIEW_RESULT_SCHEMA = "review-result.v1"
REVIEW_STAGES = frozenset({"requirement_split", "integrated_design", "task_plan"})


def current_thread_id(*, action: str) -> str:
    """只从当前进程环境捕获任务标识，不能使用提交文件里的同名字段兜底。"""

    run_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not run_id:
        raise SdlcError(
            f"当前任务没有可验证的 CODEX_THREAD_ID，不能{action}。",
            exit_code=1,
        )
    return run_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: object, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SdlcError(f"{field_name} 必须是带时区的时间。", exit_code=1)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SdlcError(f"{field_name} 必须是带时区的时间。", exit_code=1) from exc
    if parsed.tzinfo is None:
        raise SdlcError(f"{field_name} 必须是带时区的时间。", exit_code=1)


def _clean_relative_path(value: str | Path) -> str:
    raw_path = str(value).strip().replace("\\", "/")
    if not raw_path or "\x00" in raw_path:
        raise SdlcError("审核输入路径不能为空。", exit_code=1)
    pure_path = PurePosixPath(raw_path)
    if pure_path.is_absolute() or pure_path == PurePosixPath(".") or ".." in pure_path.parts:
        raise SdlcError(f"审核输入路径必须是项目内相对文件：{raw_path}。", exit_code=1)
    normalized = pure_path.as_posix()
    # 保留调用方原始词法边界，避免 ./、重复分隔符等写法在检查前被悄悄折叠。
    if normalized != raw_path:
        raise SdlcError(f"审核输入路径必须使用规范的项目内相对路径：{raw_path}。", exit_code=1)
    return normalized


def normalize_input_path(value: str | Path) -> str:
    """只规范审核输入路径文本，不读取文件，供显式依赖合同复用。"""

    return _clean_relative_path(value)


def _lexical_metadata(root: Path, relative_path: str) -> list[os.stat_result]:
    """沿原始相对路径逐段 lstat，不能先 resolve 后丢失链接信息。"""

    current = root
    metadata: list[os.stat_result] = []
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            item = current.lstat()
        except FileNotFoundError as exc:
            raise SdlcError(f"审核输入文件不存在：{relative_path}。", exit_code=1) from exc
        except OSError as exc:
            raise SdlcError(f"审核输入路径无法读取：{relative_path}。", exit_code=1) from exc
        if stat.S_ISLNK(item.st_mode):
            raise SdlcError(f"审核输入路径不能经过符号链接：{relative_path}。", exit_code=1)
        if index < len(parts) - 1 and not stat.S_ISDIR(item.st_mode):
            raise SdlcError(f"审核输入路径中的目录无效：{relative_path}。", exit_code=1)
        metadata.append(item)
    if not metadata or not stat.S_ISREG(metadata[-1].st_mode):
        raise SdlcError(f"审核输入必须是项目内普通文件：{relative_path}。", exit_code=1)
    return metadata


def _same_lexical_identity(
    root: Path,
    relative_path: str,
    expected: list[os.stat_result],
) -> bool:
    try:
        actual = _lexical_metadata(root, relative_path)
    except SdlcError:
        return False
    if len(actual) != len(expected):
        return False
    return all(
        (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode))
        == (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        for current, before in zip(actual, expected)
    )


def _hash_regular_file(paths: ProjectPaths, relative_path: str) -> str:
    root = paths.root.resolve(strict=True)
    before_segments = _lexical_metadata(root, relative_path)
    parts = PurePosixPath(relative_path).parts
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = read_flags | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(root, directory_flags)
        for index, part in enumerate(parts[:-1]):
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            opened = os.fstat(next_descriptor)
            expected = before_segments[index]
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                os.close(next_descriptor)
                raise SdlcError(f"审核输入路径在读取期间发生变化：{relative_path}。", exit_code=1)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(parts[-1], read_flags, dir_fd=directory_descriptor)
        opened_file = os.fstat(descriptor)
        expected_file = before_segments[-1]
        if not stat.S_ISREG(opened_file.st_mode) or (
            opened_file.st_dev,
            opened_file.st_ino,
        ) != (expected_file.st_dev, expected_file.st_ino):
            raise SdlcError(f"审核输入文件在读取期间发生变化：{relative_path}。", exit_code=1)
    except SdlcError:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise SdlcError(f"审核输入文件无法安全读取：{relative_path}。", exit_code=1) from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    digest = hashlib.sha256()
    assert descriptor is not None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after_file = os.fstat(handle.fileno())
    except OSError as exc:
        raise SdlcError(f"审核输入文件读取失败：{relative_path}。", exit_code=1) from exc
    if (
        (opened_file.st_dev, opened_file.st_ino, opened_file.st_size, opened_file.st_mtime_ns)
        != (after_file.st_dev, after_file.st_ino, after_file.st_size, after_file.st_mtime_ns)
        or not _same_lexical_identity(root, relative_path, before_segments)
    ):
        raise SdlcError(f"审核输入路径在读取期间发生变化：{relative_path}。", exit_code=1)
    return digest.hexdigest()


def controlled_input_hashes(
    paths: ProjectPaths,
    input_paths: Iterable[str | Path],
) -> dict[str, str]:
    """从受控项目文件计算完整哈希；调用方没有传入或覆盖哈希的入口。"""

    clean_paths = [_clean_relative_path(item) for item in input_paths]
    if not clean_paths:
        raise SdlcError("审核请求至少需要一份真实输入文件。", exit_code=1)
    if len(set(clean_paths)) != len(clean_paths):
        raise SdlcError("审核输入路径不能重复。", exit_code=1)

    result: dict[str, str] = {}
    for relative_path in sorted(clean_paths):
        result[relative_path] = _hash_regular_file(paths, relative_path)
    return result


def build_review_request(
    paths: ProjectPaths,
    *,
    review_id: str,
    stage: str,
    owner_id: str,
    input_paths: Iterable[str | Path],
    required_checks: Iterable[str] = (),
    created_at: str | None = None,
) -> dict[str, Any]:
    """建立三类审核共用请求，生产任务标识和输入哈希都由当前环境生成。"""

    input_hashes = controlled_input_hashes(paths, input_paths)
    checks = sorted(str(item).strip() for item in required_checks)
    if any(not item for item in checks) or len(set(checks)) != len(checks):
        raise SdlcError("审核检查项不能为空或重复。", exit_code=1)
    document: dict[str, Any] = {
        "schema_version": REVIEW_REQUEST_SCHEMA,
        "review_id": str(review_id).strip().upper(),
        "stage": str(stage).strip(),
        "owner_id": str(owner_id).strip().upper(),
        "producer_run_id": current_thread_id(action="创建审核请求"),
        "input_hashes": input_hashes,
        "input_paths": list(input_hashes),
        "required_checks": checks,
        "created_at": str(created_at or _utc_now()).strip(),
        "status": "pending",
    }
    validate_review_request(paths, document)
    return document


def validate_review_request(
    paths: ProjectPaths,
    document: object,
    *,
    verify_files: bool = True,
) -> dict[str, Any]:
    """校验请求结构，并按需重新读取受控文件确认哈希没有漂移。"""

    validate_schema_document(document, schema_name=REVIEW_REQUEST_SCHEMA)
    assert isinstance(document, dict)
    input_paths = document["input_paths"]
    input_hashes = document["input_hashes"]
    _validate_timestamp(document["created_at"], field_name="created_at")
    if not document["producer_run_id"].strip():
        raise SdlcError("producer_run_id 不能为空。", exit_code=1)
    if input_paths != sorted(input_paths) or set(input_paths) != set(input_hashes):
        raise SdlcError("审核请求的输入路径和哈希集合必须完整一致并按路径排序。", exit_code=1)
    if verify_files:
        actual_hashes = controlled_input_hashes(paths, input_paths)
        if actual_hashes != input_hashes:
            raise SdlcError("审核请求的真实输入文件已经变化。", exit_code=1)
    return deepcopy(document)


def review_input_fingerprint(request: Mapping[str, Any]) -> str:
    """相同审核对象、阶段、检查项和输入哈希得到同一复用标识。"""

    return canonical_sha256(
        {
            "stage": request.get("stage"),
            "owner_id": request.get("owner_id"),
            "input_hashes": request.get("input_hashes"),
            "required_checks": request.get("required_checks"),
        }
    )


def _validate_result_binding(result: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    expected = {
        "review_id": request.get("review_id"),
        "stage": request.get("stage"),
        "owner_id": request.get("owner_id"),
        "input_hashes": request.get("input_hashes"),
    }
    mismatched = [name for name, value in expected.items() if result.get(name) != value]
    if mismatched:
        raise SdlcError(
            "审核结果与请求不一致：" + "、".join(mismatched) + "。",
            exit_code=1,
        )


def _validate_issue_semantics(result: Mapping[str, Any]) -> None:
    issues = result.get("issues")
    status = result.get("status")
    if status == "passed" and issues:
        raise SdlcError("审核结果为 passed 时 issues 必须为空。", exit_code=1)
    if status == "needs_fix" and not issues:
        raise SdlcError("审核结果为 needs_fix 时必须登记真实问题。", exit_code=1)
    if isinstance(issues, list):
        issue_ids = [item.get("issue_id") for item in issues if isinstance(item, dict)]
        if len(issue_ids) != len(set(issue_ids)):
            raise SdlcError("同一审核结果中的 issue_id 不能重复。", exit_code=1)


def capture_review_result(
    request: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """把审核提交转换为可信结果；输入里的 reviewer_run_id 只占合同位置，不参与身份判断。"""

    if not isinstance(submission, Mapping):
        raise SdlcError("审核结果顶层必须是对象。", exit_code=1)
    result = deepcopy(dict(submission))
    # 先覆盖再校验，既保留严格合同，也不会让伪造的同名字段进入可信登记。
    result["reviewer_run_id"] = current_thread_id(action="提交审核结果")
    validate_schema_document(result, schema_name=REVIEW_RESULT_SCHEMA)
    _validate_timestamp(result["reviewed_at"], field_name="reviewed_at")
    _validate_result_binding(result, request)
    _validate_issue_semantics(result)
    if result["reviewer_run_id"] == request.get("producer_run_id"):
        raise SdlcError("生产任务和审核任务必须使用不同的 CODEX_THREAD_ID。", exit_code=1)
    return result


def validate_review_result(
    document: object,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """重新核对已经捕获身份的结果，适用于登记表读取和后续状态计算。"""

    validate_schema_document(document, schema_name=REVIEW_RESULT_SCHEMA)
    assert isinstance(document, dict)
    _validate_timestamp(document["reviewed_at"], field_name="reviewed_at")
    if not document["reviewer_run_id"].strip():
        raise SdlcError("reviewer_run_id 不能为空。", exit_code=1)
    _validate_result_binding(document, request)
    _validate_issue_semantics(document)
    if document.get("reviewer_run_id") == request.get("producer_run_id"):
        raise SdlcError("生产任务和审核任务标识相同。", exit_code=1)
    return deepcopy(document)


__all__ = [
    "REVIEW_REQUEST_SCHEMA",
    "REVIEW_RESULT_SCHEMA",
    "REVIEW_STAGES",
    "build_review_request",
    "capture_review_result",
    "controlled_input_hashes",
    "current_thread_id",
    "normalize_input_path",
    "review_input_fingerprint",
    "validate_review_request",
    "validate_review_result",
]
