from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from codex_sdlc.core.artifact_index import (
    formal_manifest_entries,
    validate_artifact_index_document,
)
from codex_sdlc.core.agent_sync import check_agent_entries, versioned_cli_entry
from codex_sdlc.core.backup import backup_integrity_checks, render_backup_candidates
from codex_sdlc.core.codegraph_context import doctor_check as codegraph_doctor_check
from codex_sdlc.core.environment import get_install_context
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths, project_lock, resolve_project_root
from codex_sdlc.core.reference_index import validate_reference_index_file
from codex_sdlc.core.state import (
    derive_state,
    deep_materialized_checks,
    doctor_checks,
    legacy_change_residual_checks,
    refresh_materialized_state,
    refresh_start_transaction_state,
    repair_imported_requirement_metadata,
    repair_legacy_change_residuals,
    repair_task_contracts,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    validate_schema_document,
)


DOCUMENT_FIRST_PROFILE = "document-first.v1"
DOCUMENT_FIRST_FORMAL_SCHEMA = "formal-document-first.v3"
_REQUIREMENT_ID = re.compile(r"^REQ-[0-9]{3,}$")
_TRANSACTION_ID = re.compile(r"^START-[0-9a-f]{64}$")
_CURRENT_DOCUMENTS = {
    "requirement": "requirement",
    "design": "design",
    "test_matrix": "test-matrix",
}


def _archive_failure(requirement_dir: Path, message: str) -> SdlcError:
    return SdlcError(f"{requirement_dir.name} 的正式档案无效：{message}", exit_code=1)


def _controlled_file(root: Path, relative_path: object, *, label: str) -> Path:
    """正式读者只接受档案根目录内的真实普通文件，不能跟随链接读到目录外。"""

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


def _read_archive_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效的 UTF-8 JSON：{path.name}。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是 JSON 对象：{path.name}。", exit_code=1)
    return document


def _validate_original_file_set(
    requirement_dir: Path,
    expected_paths: set[str],
) -> None:
    original = requirement_dir / "original"
    if original.is_symlink() or not original.is_dir():
        raise _archive_failure(requirement_dir, "original 必须是真实目录。")
    actual: set[str] = set()
    for path in sorted(original.rglob("*")):
        relative = path.relative_to(requirement_dir).as_posix()
        if path.is_symlink():
            raise _archive_failure(requirement_dir, f"original 不能包含符号链接：{relative}。")
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir():
            raise _archive_failure(requirement_dir, f"original 包含不支持的文件类型：{relative}。")
    missing = sorted(expected_paths - actual)
    extra = sorted(actual - expected_paths)
    if missing:
        raise _archive_failure(requirement_dir, "正式清单缺少文件：" + "、".join(missing))
    if extra:
        raise _archive_failure(requirement_dir, "original 包含未登记文件：" + "、".join(extra))


def _event_bytes(events: object) -> bytes:
    if not isinstance(events, list) or not all(isinstance(item, Mapping) for item in events):
        raise SdlcError("完成回执缺少结构化建档事件。", exit_code=1)
    return b"".join(canonical_json_text(item).encode("utf-8") for item in events)


def _validate_completed_receipt(
    paths,
    requirement_dir: Path,
    *,
    requirement_id: str,
    formal: Mapping[str, object],
    manifest: list[dict[str, object]],
    status: Mapping[str, object],
) -> dict[str, object]:
    """完成回执保护只写一次的 original；可重建 Markdown 不作为读取门槛。"""

    transaction_id = str(status.get("transaction_id") or "")
    if _TRANSACTION_ID.fullmatch(transaction_id) is None:
        raise _archive_failure(requirement_dir, "status.json 缺少合法建档事务编号。")
    receipt_relative = f"start-transactions/completed/{transaction_id}.json"
    try:
        receipt_path = _controlled_file(paths.sdlc_dir, receipt_relative, label="建档完成回执")
        receipt = _read_archive_json(receipt_path, label="建档完成回执")
    except SdlcError as exc:
        raise _archive_failure(requirement_dir, exc.message) from exc

    expected_fields = {
        "transaction_id": transaction_id,
        "requirement_id": requirement_id,
        "source_draft_id": formal.get("source_draft_id"),
        "source_revision_sha256": formal.get("source_revision_sha256"),
        "target_directory": requirement_dir.name,
        "formal_directory": str(requirement_dir),
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            raise _archive_failure(requirement_dir, f"完成回执的 {field} 与正式档案不一致。")
    if receipt.get("formal_sha256") != canonical_sha256(formal):
        raise _archive_failure(requirement_dir, "formal.v3.json 与建档完成回执不一致。")
    if canonical_sha256(receipt.get("formal_manifest")) != canonical_sha256(manifest):
        raise _archive_failure(requirement_dir, "正式清单与建档完成回执不一致。")

    generated = receipt.get("generated_files")
    if not isinstance(generated, Mapping):
        raise _archive_failure(requirement_dir, "建档完成回执缺少文件哈希清单。")
    immutable_paths = {
        "original/formal.v3.json",
        str(formal["artifact_index"]["archive_path"]),  # type: ignore[index]
        *(str(item["archive_path"]) for item in manifest),
    }
    for relative in sorted(immutable_paths):
        try:
            target = _controlled_file(requirement_dir, relative, label="只写一次正式原文")
            current_hash = sha256_bytes(target.read_bytes())
        except (OSError, SdlcError) as exc:
            message = exc.message if isinstance(exc, SdlcError) else str(exc)
            raise _archive_failure(requirement_dir, message) from exc
        if generated.get(relative) != current_hash:
            raise _archive_failure(requirement_dir, f"只写一次正式原文与完成回执不一致：{relative}。")

    events = receipt.get("events")
    expected_event_bytes = _event_bytes(events)
    try:
        start = int(receipt["event_start_size"])
        end = int(receipt["event_end_size"])
        current_events = paths.events_file.read_bytes()
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise _archive_failure(requirement_dir, "完成回执缺少可核对的事件字节边界。") from exc
    if (
        start < 0
        or end != start + len(expected_event_bytes)
        or len(current_events) < end
        or current_events[start:end] != expected_event_bytes
    ):
        raise _archive_failure(requirement_dir, "建档事件与完成回执不一致。")
    if not isinstance(events, list) or len(events) != 2:
        raise _archive_failure(requirement_dir, "完成回执必须包含两条正式建档事件。")
    created, started = events
    created_payload = created.get("payload") if isinstance(created, Mapping) else None
    started_payload = started.get("payload") if isinstance(started, Mapping) else None
    if (
        created.get("event_type") != "requirement_created"
        or started.get("event_type") != "draft_started"
        or created.get("requirement_id") != requirement_id
        or started.get("requirement_id") != requirement_id
        or not isinstance(created_payload, Mapping)
        or created_payload.get("folder_name") != requirement_dir.name
        or not isinstance(started_payload, Mapping)
        or started_payload.get("draft_id") != formal.get("source_draft_id")
        or started_payload.get("started_requirement_id") != requirement_id
    ):
        raise _archive_failure(requirement_dir, "建档事件与正式目录、REQ 或来源 DRAFT 不一致。")
    return receipt


def inspect_document_first_archive(
    paths,
    requirement_dir: Path,
    *,
    expected_requirement: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """按 formal 中的 archive_path 读取并交叉核对一份文档优先正式档案。"""

    if (
        paths.requirements_dir.is_symlink()
        or not paths.requirements_dir.is_dir()
        or requirement_dir.parent != paths.requirements_dir
        or requirement_dir.is_symlink()
        or not requirement_dir.is_dir()
    ):
        raise _archive_failure(requirement_dir, "正式需求目录必须是真实的单层受控目录。")
    try:
        formal_path = _controlled_file(
            requirement_dir,
            "original/formal.v3.json",
            label="formal.v3",
        )
        formal = _read_archive_json(formal_path, label="formal.v3")
    except SdlcError as exc:
        raise _archive_failure(requirement_dir, exc.message) from exc

    profile = formal.get("workflow_profile")
    if formal.get("formal_contract_version") == "formal.v3" and profile is None:
        # 没有 profile 的历史 facts 正式包仍由旧只读体检负责，不能接入新门禁。
        return None
    if profile != DOCUMENT_FIRST_PROFILE:
        raise _archive_failure(requirement_dir, f"workflow_profile 不受支持：{profile}。")
    try:
        validate_schema_document(formal, schema_name=DOCUMENT_FIRST_FORMAL_SCHEMA)
    except SdlcError as exc:
        raise _archive_failure(requirement_dir, exc.message) from exc

    artifact_reference = formal.get("artifact_index")
    if not isinstance(artifact_reference, Mapping):
        raise _archive_failure(requirement_dir, "formal.v3 缺少 artifact_index。")
    try:
        index_path = _controlled_file(
            requirement_dir,
            artifact_reference.get("archive_path"),
            label="artifact-index archive_path",
        )
        index_bytes = index_path.read_bytes()
        if sha256_bytes(index_bytes) != artifact_reference.get("sha256"):
            raise SdlcError("artifact-index.v1 完整 SHA-256 与 formal.v3 不一致。", exit_code=1)
        artifact_index = validate_artifact_index_document(
            _read_archive_json(index_path, label="artifact-index.v1")
        )
    except (OSError, SdlcError) as exc:
        message = exc.message if isinstance(exc, SdlcError) else str(exc)
        raise _archive_failure(requirement_dir, message) from exc
    if (
        artifact_index.get("draft_id") != formal.get("source_draft_id")
        or artifact_index.get("draft_revision_sha256")
        != formal.get("source_revision_sha256")
    ):
        raise _archive_failure(requirement_dir, "formal.v3 与 artifact-index.v1 的来源 DRAFT 或修订不一致。")

    manifest = formal.get("artifact_manifest")
    if (
        not isinstance(manifest, list)
        or not all(isinstance(item, Mapping) for item in manifest)
        or canonical_sha256(manifest)
        != canonical_sha256(formal_manifest_entries(artifact_index))
    ):
        raise _archive_failure(requirement_dir, "formal.v3 与 artifact-index.v1 的正式清单不一致。")
    normalized_manifest = [deepcopy(dict(item)) for item in manifest]
    expected_original = {
        "original/formal.v3.json",
        str(artifact_reference["archive_path"]),
    }
    for item in normalized_manifest:
        archive_path = str(item.get("archive_path") or "")
        try:
            target = _controlled_file(requirement_dir, archive_path, label="正式 archive_path")
            actual_hash = sha256_bytes(target.read_bytes())
        except (OSError, SdlcError) as exc:
            message = exc.message if isinstance(exc, SdlcError) else str(exc)
            raise _archive_failure(requirement_dir, message) from exc
        if actual_hash != item.get("sha256"):
            raise _archive_failure(requirement_dir, f"正式 archive_path 文件哈希不一致：{archive_path}。")
        expected_original.add(archive_path)
    _validate_original_file_set(requirement_dir, expected_original)

    try:
        reference_path = _controlled_file(
            requirement_dir,
            "reference-index.v1.json",
            label="reference-index.v1",
        )
        reference_index = validate_reference_index_file(requirement_dir, reference_path)
        status_path = _controlled_file(requirement_dir, "status.json", label="status.json")
        status = _read_archive_json(status_path, label="status.json")
    except SdlcError as exc:
        raise _archive_failure(requirement_dir, exc.message) from exc

    requirement_id = str(status.get("requirement_id") or "")
    if (
        _REQUIREMENT_ID.fullmatch(requirement_id) is None
        or not (
            requirement_dir.name == requirement_id
            or requirement_dir.name.startswith(f"{requirement_id}-")
        )
        or reference_index.get("requirement_id") != requirement_id
        or status.get("source_draft_id") != formal.get("source_draft_id")
        or status.get("status") != "active"
    ):
        raise _archive_failure(requirement_dir, "status、reference-index、目录名与正式来源不一致。")

    versions = status.get("versions")
    current_files = status.get("current_files")
    if not isinstance(versions, Mapping) or not isinstance(current_files, Mapping):
        raise _archive_failure(requirement_dir, "status.json 缺少结构化版本和当前文件映射。")
    current_documents: dict[str, dict[str, object]] = {}
    version_documents: dict[str, dict[str, object]] = {}
    current_paths: dict[str, str] = {}
    for status_key, file_stem in _CURRENT_DOCUMENTS.items():
        version_name = str(versions.get(status_key) or "")
        current_relative = str(current_files.get(status_key) or "")
        if (
            not version_name.startswith(f"{file_stem}.v")
            or not current_relative.startswith("effective/")
            or not current_relative.endswith(".current.json")
        ):
            raise _archive_failure(requirement_dir, f"status.json 的 {status_key} 版本映射不合法。")
        version_relative = f"versions/{version_name}.json"
        try:
            current_path = _controlled_file(
                requirement_dir,
                current_relative,
                label=f"{status_key} 当前版本",
            )
            version_path = _controlled_file(
                requirement_dir,
                version_relative,
                label=f"{status_key} 正式版本",
            )
            current = _read_archive_json(current_path, label=f"{status_key} 当前版本")
            version = _read_archive_json(version_path, label=f"{status_key} 正式版本")
        except SdlcError as exc:
            raise _archive_failure(requirement_dir, exc.message) from exc
        comparable = deepcopy(current)
        comparable["is_current"] = False
        if (
            current.get("requirement_id") != requirement_id
            or current.get("source_draft_id") != formal.get("source_draft_id")
            or current.get("version") != version_name
            or current.get("is_current") is not True
            or version.get("requirement_id") != requirement_id
            or version.get("source_draft_id") != formal.get("source_draft_id")
            or version.get("version") != version_name
            or version.get("is_current") is not False
            or canonical_sha256(comparable) != canonical_sha256(version)
        ):
            raise _archive_failure(requirement_dir, f"{status_key} 的 current 与正式版本不一致。")
        current_documents[status_key] = current
        version_documents[status_key] = version
        current_paths[status_key] = current_relative

    manifest_by_path = {
        str(item["archive_path"]): item
        for item in normalized_manifest
    }
    design_artifacts = current_documents["design"].get("artifacts")
    if not isinstance(design_artifacts, list):
        raise _archive_failure(requirement_dir, "design.current 缺少结构化 artifacts。")
    for artifact in design_artifacts:
        if not isinstance(artifact, Mapping):
            raise _archive_failure(requirement_dir, "design.current 的 artifacts 必须是对象列表。")
        archive_path = str(artifact.get("archive_path") or "")
        source = manifest_by_path.get(archive_path)
        if (
            source is None
            or artifact.get("sha256") != source.get("sha256")
            or artifact.get("artifact_id") != source.get("business_id")
        ):
            raise _archive_failure(requirement_dir, f"design.current 没有按正式清单读取：{archive_path}。")

    if expected_requirement is not None:
        native_start = expected_requirement.get("native_start")
        if (
            expected_requirement.get("requirement_id") != requirement_id
            or expected_requirement.get("folder_name") != requirement_dir.name
            or not isinstance(native_start, Mapping)
            or native_start.get("workflow_profile") != DOCUMENT_FIRST_PROFILE
            or native_start.get("source_draft_id") != formal.get("source_draft_id")
            or native_start.get("source_revision_sha256")
            != formal.get("source_revision_sha256")
            or canonical_sha256(native_start.get("artifact_manifest"))
            != canonical_sha256(normalized_manifest)
        ):
            raise _archive_failure(requirement_dir, "事件重建状态与正式档案不一致。")

    receipt = _validate_completed_receipt(
        paths,
        requirement_dir,
        requirement_id=requirement_id,
        formal=formal,
        manifest=normalized_manifest,
        status=status,
    )
    return {
        "requirement_id": requirement_id,
        "requirement_dir": requirement_dir,
        "formal": formal,
        "artifact_index": artifact_index,
        "manifest": normalized_manifest,
        "reference_index": reference_index,
        "status": status,
        "current_documents": current_documents,
        "version_documents": version_documents,
        "current_paths": current_paths,
        "receipt": receipt,
    }


def formal_archive_checks(
    paths,
    state: Mapping[str, object],
) -> tuple[list[str], list[str]]:
    """同时覆盖事件中登记的 REQ 和磁盘上的正式目录，避免漏掉孤立或重复档案。"""

    passed: list[str] = []
    failed: list[str] = []
    checked: set[Path] = set()
    requirements = state.get("requirements")
    for requirement in requirements.values() if isinstance(requirements, Mapping) else []:
        if not isinstance(requirement, Mapping):
            continue
        native_start = requirement.get("native_start")
        if not isinstance(native_start, Mapping) or native_start.get("workflow_profile") != DOCUMENT_FIRST_PROFILE:
            continue
        folder_name = str(requirement.get("folder_name") or "")
        requirement_dir = paths.requirements_dir / folder_name
        checked.add(requirement_dir)
        try:
            archive = inspect_document_first_archive(
                paths,
                requirement_dir,
                expected_requirement=requirement,
            )
        except SdlcError as exc:
            failed.append(exc.message)
        else:
            if archive is not None:
                passed.append(
                    f"{archive['requirement_id']} 正式清单、原文、引用、状态和结构化版本一致"
                )

    if paths.requirements_dir.is_symlink():
        failed.append("正式需求根目录不能是符号链接。")
    elif paths.requirements_dir.is_dir():
        for requirement_dir in sorted(paths.requirements_dir.iterdir()):
            if requirement_dir in checked or not requirement_dir.is_dir():
                continue
            formal_path = requirement_dir / "original/formal.v3.json"
            if not formal_path.exists() and not formal_path.is_symlink():
                continue
            try:
                archive = inspect_document_first_archive(paths, requirement_dir)
            except SdlcError as exc:
                failed.append(exc.message)
            else:
                if archive is not None:
                    passed.append(
                        f"{archive['requirement_id']} 正式清单、原文、引用、状态和结构化版本一致"
                    )
    if not passed and not failed:
        passed.append("当前没有需要检查的文档优先正式档案")
    return passed, failed


def _refresh_doctor_projections(paths, state: Mapping[str, object]) -> dict[str, object]:
    """文档优先 REQ 已有完整结构化目录，repair 只重建事件派生的全局可读投影。"""

    requirements = state.get("requirements")
    document_first_ids: list[str] = []
    if isinstance(requirements, Mapping):
        for requirement in requirements.values():
            if not isinstance(requirement, Mapping):
                continue
            native_start = requirement.get("native_start")
            if (
                isinstance(native_start, Mapping)
                and native_start.get("workflow_profile") == DOCUMENT_FIRST_PROFILE
            ):
                document_first_ids.append(str(requirement.get("requirement_id") or ""))
    if document_first_ids:
        return refresh_start_transaction_state(
            paths,
            committed_requirement_id=document_first_ids[0],
        )
    return refresh_materialized_state(paths)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("doctor", help="检查安装或项目状态")
    parser.add_argument("--install", action="store_true", help="检查本机安装情况")
    parser.add_argument("--repair", action="store_true", help="尝试重建 SQLite 和 Markdown 快照")
    parser.add_argument("--deep", action="store_true", help="执行深度体检，只读检查人工改动、备份和代码图谱状态")
    parser.set_defaults(func=run)

    install_parser = subparsers.add_parser("doctor-install", help="检查本机 SDLC 安装情况")
    install_parser.set_defaults(func=run_install)

    repair_parser = subparsers.add_parser("doctor-repair", help="重建 SQLite 和 Markdown 快照")
    repair_parser.set_defaults(func=run_repair)

    deep_parser = subparsers.add_parser("doctor-deep", help="执行深度体检，只读检查人工改动、备份和代码图谱状态")
    deep_parser.set_defaults(func=run_deep)


def _first_probe_message(result: subprocess.CompletedProcess[str]) -> str:
    """只摘取第一行真实报错，既能定位问题，也不会把整段帮助刷进体检结果。"""

    for output in (result.stderr, result.stdout):
        for line in output.splitlines():
            if line.strip():
                return line.strip()
    return "命令没有返回可读说明"


def _install_check_target() -> tuple[Path, Path, Path, str, str]:
    """优先检查当前代码对应的入口，同时保留显式受管目录的原有语义。"""

    context = get_install_context()
    if "CODEX_SDLC_HOME" in os.environ:
        install_command = f"python3 {context.sdlc_home / 'scripts/install_specstamp.py'}"
        probe_cwd = context.sdlc_home if context.sdlc_home.is_dir() else Path.cwd()
        return (
            context.bin_dir,
            context.cli_script,
            probe_cwd,
            install_command,
            'export PATH="$HOME/.local/bin:$PATH"',
        )

    current_cli = versioned_cli_entry()
    if current_cli.is_file():
        current_root = current_cli.parent.parent
        install_script = current_root / "scripts" / "install_specstamp.py"
        if install_script.is_file():
            install_command = f"python3 {install_script}"
            probe_cwd = current_root
        else:
            # wheel、普通 pip 和 pipx 安装没有仓库安装脚本，应使用当前解释器修复同一发行版。
            install_command = (
                f"{shlex.quote(sys.executable)} -m pip install --upgrade specstamp"
            )
            probe_cwd = Path.cwd()
        path_repair_command = (
            'export PATH="$HOME/.local/bin:$PATH"'
            if install_script.is_file()
            else f'export PATH="{current_cli.parent}:$PATH"'
        )
        return current_cli.parent, current_cli, probe_cwd, install_command, path_repair_command

    install_command = f"python3 {context.sdlc_home / 'scripts/install_specstamp.py'}"
    probe_cwd = context.sdlc_home if context.sdlc_home.is_dir() else Path.cwd()
    return (
        context.bin_dir,
        context.cli_script,
        probe_cwd,
        install_command,
        'export PATH="$HOME/.local/bin:$PATH"',
    )


def _path_contains_directory(path_entries: list[str], directory: Path) -> bool:
    """按真实路径判断 PATH，兼容 macOS 的 /var 与 /private/var 等价路径。"""

    try:
        expected = directory.resolve()
    except OSError:
        expected = directory.absolute()
    for entry in path_entries:
        if not entry:
            continue
        try:
            actual = Path(entry).resolve()
        except OSError:
            actual = Path(entry).absolute()
        if actual == expected:
            return True
    return False


def build_install_check() -> tuple[str, bool]:
    """只读检查当前终端入口、运行依赖和全部受管技能内容。"""

    bin_dir, cli_script, probe_cwd, install_command, path_repair_command = (
        _install_check_target()
    )
    expected_path_entry = str(bin_dir)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)

    lines = ["安装检查"]
    failed = False

    cli_exists = cli_script.exists()
    cli_executable = os.access(cli_script, os.X_OK)
    lines.append(f"- CLI：{'已安装' if cli_exists else '缺失'} ({cli_script})")
    callable_path = shutil.which("codex-sdlc")
    lines.append(f"- 当前终端可直接调用：{'是' if callable_path else '否'}" + (f" ({callable_path})" if callable_path else ""))
    if not cli_exists or not cli_executable:
        failed = True
        if cli_exists:
            lines.append(f"- 问题：正式 CLI 不可执行：{cli_script}")
    if callable_path and cli_exists:
        try:
            callable_matches_install = (
                Path(callable_path).resolve() == cli_script.resolve()
            )
        except OSError:
            callable_matches_install = False
        if not callable_matches_install:
            failed = True
            lines.append(
                f"- 问题：当前终端调用的是其它入口：{callable_path}；"
                f"当前安装入口是 {cli_script}"
            )

    if _path_contains_directory(path_entries, bin_dir):
        lines.append(f"- PATH：已包含 {expected_path_entry}")
    elif callable_path:
        lines.append(f"- PATH：未包含 {expected_path_entry}，但当前已可通过 {callable_path} 调用")
    else:
        failed = True
        lines.append(f"- PATH：未包含 {expected_path_entry}")
        lines.append(f"- 修复命令：{path_repair_command}")

    if not cli_exists or not cli_executable:
        lines.append(f"- 修复命令：{install_command}")

    # 必须从当前终端能直接找到的正式入口做冒烟，仓库内部函数成功不能代替用户入口可用。
    if callable_path and cli_exists and cli_executable:
        for label, arguments in (("CLI 帮助", ["--help"]), ("CLI 版本", ["version"])):
            try:
                result = subprocess.run(
                    [callable_path, *arguments],
                    cwd=probe_cwd,
                    env=dict(os.environ),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failed = True
                lines.append(f"- {label}：失败（{exc}）")
            else:
                if result.returncode == 0:
                    lines.append(f"- {label}：通过")
                else:
                    failed = True
                    lines.append(
                        f"- {label}：失败（退出码 {result.returncode}，"
                        f"{_first_probe_message(result)}）"
                    )
        if failed:
            lines.append(f"- CLI 修复命令：{install_command}")

    # doctor-install 已经由正式入口启动，但这里仍用同一个解释器单独导入依赖，
    # 让体检输出明确区分依赖问题和普通命令问题。
    try:
        dependency_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import codex_sdlc; import jsonschema; import pypdf",
            ],
            cwd=probe_cwd,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        failed = True
        lines.append(f"- 运行依赖：失败（{exc}）")
        lines.append(f"- 依赖修复命令：{install_command}")
    else:
        if dependency_probe.returncode == 0:
            lines.append("- 运行依赖：通过（codex_sdlc、jsonschema、pypdf）")
        else:
            failed = True
            lines.append(
                f"- 运行依赖：失败（退出码 {dependency_probe.returncode}，"
                f"{_first_probe_message(dependency_probe)}）"
            )
            lines.append(f"- 依赖修复命令：{install_command}")

    def append_agent_repair_commands() -> None:
        lines.append(
            "- 技能来源恢复命令："
            "unset CODEX_SDLC_SOURCE_SKILLS_HOME CODEX_SDLC_SHARED_SOURCE_SKILLS_HOME"
        )
        lines.append("- 技能同步预览：codex-sdlc agent-sync --dry-run")
        lines.append("- 确认预览后执行：codex-sdlc agent-sync --confirm")
        lines.append("- 修复后检查：codex-sdlc agent-sync --check")
        lines.append(f"- 重新安装命令：{install_command}")

    try:
        sync_report = check_agent_entries()
    except (SdlcError, OSError, UnicodeError) as exc:
        failed = True
        message = exc.message if isinstance(exc, SdlcError) else str(exc)
        lines.append(f"- Agent 入口：失败（{message}）")
        append_agent_repair_commands()
    else:
        sync_issues = sync_report["issues"]
        if sync_issues:
            failed = True
            lines.append("- Agent 入口：失败")
            for issue in sync_issues:
                lines.append(f"- 问题：{issue}")
        else:
            lines.append(
                f"- Agent 入口：通过（{sync_report['sdlc_count']} 个 SDLC 技能，"
                f"{sync_report['claude_command_count']} 个 Claude 命令）"
            )
            for skill_name, digest in sync_report["skill_hashes"].items():
                lines.append(f"- Skill：内容哈希一致 {skill_name}（sha256={digest}）")
            for skill_name, digest in sync_report["managed_shared_skill_hashes"].items():
                lines.append(f"- 共享 Skill：内容哈希一致 {skill_name}（sha256={digest}）")
        if sync_issues:
            append_agent_repair_commands()

    lines.append(f"- 安装结论：{'不可用' if failed else '可用'}")
    return "\n".join(lines) + "\n", not failed


def render_install_check() -> str:
    text, _passed = build_install_check()
    return text


def render_missing_events_deep_check(paths) -> str:
    backup_files = sorted(paths.backups_dir.glob("events-*.jsonl.bak")) if paths.backups_dir.exists() else []
    lines = [
        "项目体检",
        "- 问题：events.jsonl 缺失，不能从 Markdown 或 sdlc.db 静默恢复主状态。",
    ]
    if backup_files:
        latest_backup = backup_files[-1].relative_to(paths.root)
        lines.append(f"- 提醒：请先从 `{latest_backup}` 恢复备份，再使用 `$sdlc-doctor-repair`。")
    else:
        lines.append("- 提醒：没有找到可恢复备份，请先人工确认是否还有其它 events.jsonl 副本。")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> int:
    if args.install:
        report, passed = build_install_check()
        print(report, end="")
        return 0 if passed else 1

    root = resolve_project_root(Path.cwd())
    paths = build_paths(root)
    active_change_dir = paths.change_transactions_dir / "accept-active"
    if active_change_dir.is_dir() and any(active_change_dir.iterdir()):
        from codex_sdlc.services.change_service import recover_change_accept_transactions

        recover_change_accept_transactions(paths)
    deep_report = {"passed": [], "warnings": [], "failed": []}
    repair_report = {"repaired": [], "warnings": []}
    metadata_repair_report = {"repaired": [], "warnings": []}
    contract_repair_report = {"repaired": [], "warnings": []}

    if args.deep and paths.sdlc_dir.exists() and not paths.events_file.exists():
        print(render_missing_events_deep_check(paths), end="")
        return 1

    if paths.events_file.exists():
        with project_lock(paths):
            if args.repair:
                state = derive_state(paths)
                repair_report = repair_legacy_change_residuals(paths, state)
                state = derive_state(paths)
                metadata_repair_report = repair_imported_requirement_metadata(paths, state)
                state = derive_state(paths)
                contract_repair_report = repair_task_contracts(paths, state)
                state = derive_state(paths)
                _refresh_doctor_projections(paths, state)
            state = derive_state(paths)
            if args.deep:
                deep_passed, deep_warnings, deep_failed = deep_materialized_checks(paths, state)
                deep_report["passed"].extend(deep_passed)
                deep_report["warnings"].extend(deep_warnings)
                deep_report["failed"].extend(deep_failed)
                change_passed, change_warnings = legacy_change_residual_checks(state)
                deep_report["passed"].extend(change_passed)
                deep_report["warnings"].extend(change_warnings)
                backup_passed, backup_warnings, backup_failed = backup_integrity_checks(paths, state)
                deep_report["passed"].extend(backup_passed)
                deep_report["warnings"].extend(backup_warnings)
                deep_report["failed"].extend(backup_failed)
                codegraph_passed, codegraph_warnings = codegraph_doctor_check(paths.root, auto_refresh=False)
                deep_report["passed"].extend(codegraph_passed)
                deep_report["warnings"].extend(codegraph_warnings)
            elif args.repair:
                change_passed, change_warnings = legacy_change_residual_checks(state)
                deep_report["passed"].extend(change_passed)
                deep_report["warnings"].extend(change_warnings)
    else:
        state = {
            "events": [],
            "requirements": {},
            "active_requirements": [],
            "sessions": [],
            "recent_session": None,
            "verifications": [],
            "counts": {"done_tasks": 0, "closed_tasks": 0, "finished_tasks": 0, "all_tasks": 0, "verified_tasks": 0},
            "project": {"project_path": str(paths.root)},
            "git_changed_files": [],
            "pending_capture_files": [],
            "pending_change_files": [],
        }

    passed, warnings, failed = doctor_checks(paths, state)
    archive_passed, archive_failed = formal_archive_checks(paths, state)
    passed.extend(item for item in archive_passed if item not in passed)
    failed.extend(item for item in archive_failed if item not in failed)
    if paths.events_file.exists():
        from codex_sdlc.services.change_service import inspect_change_accept_transactions

        change_transactions = inspect_change_accept_transactions(paths)
        for transaction_id in change_transactions["completed"]:
            message = f"正式变更事务 {transaction_id} 的回执、事件和正式文件一致"
            if message not in passed:
                passed.append(message)
        for transaction_name in change_transactions["active"]:
            message = f"存在尚未恢复的正式变更事务：{transaction_name}"
            if message not in failed:
                failed.append(message)
        for message in change_transactions["failed"]:
            if message not in failed:
                failed.append(message)
    if args.deep and not paths.events_file.exists():
        for item in render_backup_candidates(paths.root):
            warnings.append(item)
    passed.extend(item for item in deep_report["passed"] if item not in passed)
    warnings.extend(item for item in deep_report["warnings"] if item not in warnings)
    failed.extend(item for item in deep_report["failed"] if item not in failed)
    for item in repair_report["warnings"]:
        warning = f"旧变更状态残留未自动修复：{item}"
        if warning not in warnings:
            warnings.append(warning)
    for item in metadata_repair_report["warnings"]:
        warning = f"需求标题旧前缀未自动清理：{item}"
        if warning not in warnings:
            warnings.append(warning)
    for item in contract_repair_report["warnings"]:
        warning = f"任务版本或覆盖关系未自动修复：{item}"
        if warning not in warnings:
            warnings.append(warning)
    print("项目体检")
    for item in passed:
        print(f"- 通过：{item}")
    for item in warnings:
        print(f"- 提醒：{item}")
    for item in failed:
        print(f"- 问题：{item}")
    if args.repair and paths.events_file.exists():
        for change_id in repair_report["repaired"]:
            print(f"- 结果：已修复旧变更状态残留：{change_id}")
        for requirement_id in metadata_repair_report["repaired"]:
            print(f"- 结果：已清理旧需求标题前缀：{requirement_id}")
        for requirement_id in contract_repair_report["repaired"]:
            print(f"- 结果：已刷新任务版本和覆盖关系：{requirement_id}")
        # 正式引用中的业务编号和定位不能由修复命令猜测，repair 只处理既有可重建投影。
        print("- 结果：已重建 SQLite 和 Markdown 快照；正式引用索引只检查，不自动修改")
    return 0 if not failed else 1


def run_install(_args: argparse.Namespace) -> int:
    report, passed = build_install_check()
    print(report, end="")
    return 0 if passed else 1


def run_repair(args: argparse.Namespace) -> int:
    args.install = False
    args.repair = True
    args.deep = False
    return run(args)


def run_deep(args: argparse.Namespace) -> int:
    args.install = False
    args.repair = False
    args.deep = True
    return run(args)
