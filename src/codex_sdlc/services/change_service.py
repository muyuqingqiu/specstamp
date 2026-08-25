from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Mapping
import uuid

from codex_sdlc.core.backup import require_matching_sdlc_identity
from codex_sdlc.core.change_workspace import (
    INTERRUPT_AFTER_MANIFEST_PUBLISH,
    INTERRUPT_AFTER_MATERIAL_EVENT_APPEND,
    INTERRUPT_AFTER_MATERIAL_PUBLISH,
    INTERRUPT_AFTER_DIRECTORY_PUBLISH,
    INTERRUPT_AFTER_EVENT_APPEND,
    INTERRUPT_BEFORE_DIRECTORY_PUBLISH,
    INTERRUPT_AFTER_PACKAGE_EVENT_APPEND,
    INTERRUPT_AFTER_PACKAGE_PUBLISH,
    INTERRUPT_BEFORE_PACKAGE_PUBLISH,
    ChangeMaterialResult,
    ChangeWorkspaceResult,
    InterruptionHook,
    allocate_change_id,
    build_base_versions,
    build_change_material_event,
    build_change_material_transaction,
    build_change_package_event,
    build_change_package_transaction,
    build_created_event,
    build_status_document,
    build_transaction,
    change_material_manifest_bytes,
    change_material_manifest_path,
    change_package_events,
    cleanup_change_material_transaction,
    cleanup_change_package_transaction,
    cleanup_transaction,
    collect_used_change_ids,
    ensure_change_material_event_locked,
    ensure_change_package_event_locked,
    ensure_created_event_locked,
    find_idempotent_event,
    load_change_material_manifest,
    new_event_id,
    prepare_change_material,
    publish_change_material_file,
    publish_change_material_manifest,
    publish_change_package_files,
    publish_workspace,
    recover_change_material_transactions_locked,
    recover_change_package_transactions_locked,
    recover_change_transactions_locked,
    resolve_formal_requirement_dir,
    resolve_registered_change_workspace,
    stage_change_material_transaction,
    stage_change_package_files,
    status_bytes,
    status_sha256,
    validate_registered_workspaces,
    validate_request_key,
    verify_change_material_state,
    verify_workspace_event,
    write_change_material_transaction,
    write_change_package_transaction,
    write_staged_workspace,
    write_transaction_journal,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import (
    ProjectPaths,
    ensure_base_dirs,
    project_lock,
    requirement_dir_for_id,
)
from codex_sdlc.core.state import derive_state, event_write_lock, load_events
from codex_sdlc.core.structured_contract import canonical_sha256, sha256_bytes, sha256_file


CHANGE_ACCEPT_INTERRUPT_ENV = "CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT"
_ACCEPT_TRANSACTION_SCHEMA = "change-accept-transaction.v1"
_ACCEPT_ACTIVE_DIR = "accept-active"
_ACCEPT_COMPLETED_DIR = "accept-completed"
_ACCEPT_STAGING_DIR = "accept-staging"
_VERSION_PATTERN = re.compile(r"^[a-z-]+\.v([0-9]+)$")
_ACCEPT_INTERRUPT_STAGES = {
    "after_version_requirement",
    "after_version_design",
    "after_version_test_matrix",
    "after_version_reference_index",
    "after_version_task_plan",
    "after_change_event_append",
    "after_effective_requirement",
    "after_effective_design",
    "after_effective_test_matrix",
    "after_reference_index",
    "after_reference_source",
    "after_task_plan",
    "after_status",
}


def change_accept_environment_interruption_hook() -> InterruptionHook:
    """把正式入口故障注入限制为事务实际公布的稳定阶段名。"""

    requested = os.environ.get(CHANGE_ACCEPT_INTERRUPT_ENV, "").strip()
    mode = os.environ.get("CODEX_SDLC_CHANGE_ACCEPT_INTERRUPT_MODE", "error").strip()
    if mode not in {"error", "process_exit"}:
        raise SdlcError(f"正式变更事务故障模式不受支持：{mode}。", exit_code=1)
    if requested and requested not in _ACCEPT_INTERRUPT_STAGES:
        raise SdlcError(
            f"{CHANGE_ACCEPT_INTERRUPT_ENV} 的故障注入点不受支持：{requested}。",
            exit_code=1,
        )

    def interrupt(stage: str) -> None:
        if requested and requested == stage:
            if mode == "process_exit":
                # V2 需要验证进程直接退出后的真实恢复，不能把普通 Python
                # 异常回滚冒充强制中断。固定退出码便于保存整数证据。
                os._exit(86)
            raise SdlcError(f"正式变更事务故障注入：{stage}", exit_code=1)

    return interrupt
from codex_sdlc.core.task_run import load_task_run_context, protect_task_run_for_change


def _result_from_event(
    paths: ProjectPaths,
    event: Mapping[str, object],
    *,
    duplicate: bool,
) -> ChangeWorkspaceResult:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError("变更创建事件缺少结构化 payload。")
    return ChangeWorkspaceResult(
        requirement_id=str(payload["requirement_id"]),
        change_id=str(payload["change_id"]),
        workspace_path=str(payload["workspace_path"]),
        created_event_id=str(event["event_id"]),
        duplicate=duplicate,
    )


def create_change_workspace(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    request_key: str,
    interruption_hook: InterruptionHook | None = None,
) -> ChangeWorkspaceResult:
    """在同一项目锁内恢复旧事务、固定基础文件并原子发布一个空工作区。"""

    validate_request_key(request_key)
    if (
        paths.sdlc_dir.is_symlink()
        or not paths.sdlc_dir.is_dir()
        or paths.events_file.is_symlink()
        or not paths.events_file.is_file()
    ):
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    # 身份不匹配属于提交前拒绝，必须先于目录补齐和项目锁文件写入。
    require_matching_sdlc_identity(paths)
    ensure_base_dirs(paths)
    hook = interruption_hook or (lambda _stage: None)

    with project_lock(paths):
        # 身份文件首次补写也要进入项目锁，两个并发创建请求不能同时改同一份身份记录。
        require_matching_sdlc_identity(paths)
        with event_write_lock(paths):
            recover_change_transactions_locked(paths)
            events = load_events(paths)
            requirement_dir = resolve_formal_requirement_dir(
                paths, requirement_id, events
            )
            _, creation_events = validate_registered_workspaces(paths, events)
            existing_event = find_idempotent_event(
                creation_events,
                requirement_id=requirement_id,
                request_key=request_key,
            )
            if existing_event is not None:
                payload = existing_event.get("payload")
                if not isinstance(payload, Mapping):
                    raise SdlcError("幂等创建事件缺少结构化 payload。")
                workspace = paths.root / str(payload["workspace_path"])
                verify_workspace_event(
                    paths,
                    workspace,
                    existing_event,
                    verify_current_bases=True,
                )
                return _result_from_event(paths, existing_event, duplicate=True)

            # 五份文件必须在编号和事务真正提交前、持锁状态下重新读取，
            # 这样 status.json 记录的就是创建这一刻的完整基础版本。
            base_versions = build_base_versions(paths, requirement_dir)
            change_id = allocate_change_id(
                request_key,
                collect_used_change_ids(paths, events),
            )
            changes_dir = requirement_dir / "changes"
            if changes_dir.exists() and (changes_dir.is_symlink() or not changes_dir.is_dir()):
                raise SdlcError("正式需求的 changes 路径不是普通目录。")
            changes_dir.mkdir(parents=True, exist_ok=True)
            workspace = changes_dir / change_id
            workspace_path = workspace.relative_to(paths.root).as_posix()
            event_id = new_event_id(events)
            status = build_status_document(
                requirement_id=requirement_id,
                change_id=change_id,
                request_key=request_key,
                workspace_path=workspace_path,
                base_versions=base_versions,
                created_event_id=event_id,
            )
            content = status_bytes(status)
            digest = status_sha256(content)
            event = build_created_event(
                paths,
                event_id=event_id,
                requirement_id=requirement_id,
                request_key=request_key,
                change_id=change_id,
                workspace_path=workspace_path,
                status_sha256=digest,
            )

            transaction_id = uuid.uuid4().hex
            staging = paths.change_staging_root / transaction_id
            transaction = build_transaction(
                transaction_id=transaction_id,
                requirement_id=requirement_id,
                request_key=request_key,
                change_id=change_id,
                workspace_path=workspace_path,
                staging_path=staging.relative_to(paths.root).as_posix(),
                status_sha256=digest,
                event=event,
            )
            # 日志先于暂存目录落盘。进程在任何后续位置退出时，下一次命令
            # 都能只根据这份日志精确清理或补齐，不会扫描删除来源不明文件。
            journal = write_transaction_journal(paths, transaction)
            write_staged_workspace(paths, staging, content)
            hook(INTERRUPT_BEFORE_DIRECTORY_PUBLISH)

            publish_workspace(staging, workspace)
            hook(INTERRUPT_AFTER_DIRECTORY_PUBLISH)

            ensure_created_event_locked(paths, event)
            hook(INTERRUPT_AFTER_EVENT_APPEND)

            verify_workspace_event(
                paths,
                workspace,
                event,
                verify_current_bases=True,
                verify_initial_layout=True,
            )
            cleanup_transaction(paths, transaction, journal)
            return _result_from_event(paths, event, duplicate=False)


def _change_material_result(
    status: Mapping[str, object],
    material: Mapping[str, object],
    *,
    duplicate: bool,
) -> ChangeMaterialResult:
    return ChangeMaterialResult(
        requirement_id=str(status["requirement_id"]),
        change_id=str(status["change_id"]),
        workspace_path=str(status["workspace_path"]),
        material_id=str(material["material_id"]),
        source_kind=str(material["source_kind"]),
        status=str(material["status"]),
        event_id=str(material["event_id"]),
        duplicate=duplicate,
    )


def add_change_material(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
    material_type: str,
    file_path: str = "",
    url: str = "",
    version_evidence_path: str = "",
    secret_reference_path: str = "",
    interruption_hook: InterruptionHook | None = None,
) -> ChangeMaterialResult:
    """在指定 CHG 内先恢复旧事务，再追加一项不可改写的资料和唯一事件。"""

    if (
        paths.sdlc_dir.is_symlink()
        or not paths.sdlc_dir.is_dir()
        or paths.events_file.is_symlink()
        or not paths.events_file.is_file()
    ):
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    require_matching_sdlc_identity(paths)
    ensure_base_dirs(paths)
    hook = interruption_hook or (lambda _stage: None)

    with project_lock(paths):
        require_matching_sdlc_identity(paths)
        with event_write_lock(paths):
            events = load_events(paths)
            workspace, creation_event, status = resolve_registered_change_workspace(
                paths,
                events,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            status_path = workspace / "status.json"
            status_before = status_path.read_bytes()

            recover_change_material_transactions_locked(paths, workspace, status)
            events = load_events(paths)
            workspace, creation_event, status = resolve_registered_change_workspace(
                paths,
                events,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            if status_path.read_bytes() != status_before:
                raise SdlcError("资料事务恢复过程中 status.json 发生变化。")

            manifest_path = change_material_manifest_path(workspace)
            manifest_existed = manifest_path.exists() and not manifest_path.is_symlink()
            manifest = load_change_material_manifest(workspace, status)
            verify_change_material_state(paths, workspace, status, manifest, events)
            prepared = prepare_change_material(
                paths,
                material_type=material_type,
                file_path=file_path,
                url=url,
                version_evidence_path=version_evidence_path,
                secret_reference_path=secret_reference_path,
            )
            identity_sha256 = str(prepared.material["identity_sha256"])
            materials = manifest.get("materials")
            if not isinstance(materials, list):
                raise SdlcError("变更资料清单 materials 必须是数组。")
            existing = [
                item
                for item in materials
                if isinstance(item, dict)
                and item.get("identity_sha256") == identity_sha256
            ]
            if len(existing) > 1:
                raise SdlcError("相同变更资料身份对应多个 CMAT 编号。")
            if existing:
                # 幂等返回之前已经逐项核对全部旧事件和普通文件字节，不能只比身份字段。
                return _change_material_result(status, existing[0], duplicate=True)

            material_id = f"CMAT-{len(materials) + 1:03d}"
            event_id = new_event_id(events)
            material = deepcopy(prepared.material)
            material["material_id"] = material_id
            material["event_id"] = event_id
            if material.get("source_kind") == "file":
                material["stored_path"] = f"原始资料/{material_id}"
            new_manifest = deepcopy(manifest)
            new_materials = new_manifest.get("materials")
            if not isinstance(new_materials, list):
                raise SdlcError("变更资料清单 materials 必须是数组。")
            new_materials.append(material)
            manifest_content = change_material_manifest_bytes(new_manifest)
            manifest_sha256 = sha256_bytes(manifest_content)
            event = build_change_material_event(
                paths,
                event_id=event_id,
                status=status,
                material=material,
                manifest_sha256=manifest_sha256,
                workspace=workspace,
            )
            previous_manifest_sha256 = (
                sha256_bytes(manifest_path.read_bytes()) if manifest_existed else None
            )
            transaction = build_change_material_transaction(
                status=status,
                workspace=workspace,
                manifest=new_manifest,
                previous_manifest_sha256=previous_manifest_sha256,
                material=material,
                event=event,
            )
            journal = write_change_material_transaction(paths, workspace, transaction)
            stage_change_material_transaction(workspace, transaction, prepared.content)

            if material.get("source_kind") == "file":
                publish_change_material_file(workspace, transaction)
                hook(INTERRUPT_AFTER_MATERIAL_PUBLISH)

            publish_change_material_manifest(workspace, transaction)
            hook(INTERRUPT_AFTER_MANIFEST_PUBLISH)

            ensure_change_material_event_locked(paths, event)
            hook(INTERRUPT_AFTER_MATERIAL_EVENT_APPEND)

            committed_manifest = load_change_material_manifest(workspace, status)
            verify_change_material_state(
                paths, workspace, status, committed_manifest, load_events(paths)
            )
            verify_workspace_event(
                paths,
                workspace,
                creation_event,
                verify_current_bases=True,
            )
            if status_path.read_bytes() != status_before:
                raise SdlcError("归档变更资料时不能修改 status.json。")
            cleanup_change_material_transaction(workspace, transaction, journal)
            return _change_material_result(status, material, duplicate=False)


@dataclass(frozen=True)
class ChangePackageResult:
    """正式入口的稳定返回值；重复提交不会产生第二个事件。"""

    requirement_id: str
    change_id: str
    workspace_path: str
    projected_event_id: str
    package_identity_sha256: str
    id_mapping: dict[str, str]
    committed_files_sha256: dict[str, str]
    duplicate: bool


def _package_result_from_event(
    event: Mapping[str, object],
    *,
    duplicate: bool,
) -> ChangePackageResult:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError("变更包成功事件缺少结构化 payload。")
    mapping = payload.get("id_mapping")
    hashes = payload.get("committed_files_sha256")
    if not isinstance(mapping, Mapping) or not isinstance(hashes, Mapping):
        raise SdlcError("变更包成功事件缺少编号映射或提交文件哈希。")
    return ChangePackageResult(
        requirement_id=str(payload["requirement_id"]),
        change_id=str(payload["change_id"]),
        workspace_path=str(payload["workspace_path"]),
        projected_event_id=str(event["event_id"]),
        package_identity_sha256=str(payload["package_identity_sha256"]),
        id_mapping={str(key): str(value) for key, value in mapping.items()},
        committed_files_sha256={str(key): str(value) for key, value in hashes.items()},
        duplicate=duplicate,
    )


def _source_identity(
    source_documents: Mapping[str, Mapping[str, object]],
    *,
    status: Mapping[str, object],
    material_manifest_sha256: str | None,
    status_sha256_value: str,
) -> tuple[str, dict[str, str]]:
    from codex_sdlc.core.change_contract import COMMITTED_FILE_NAMES

    source_hashes = {
        name: canonical_sha256(source_documents[name])
        for name in COMMITTED_FILE_NAMES
    }
    identity = canonical_sha256(
        {
            "schema_version": "change-package-input.v1",
            "requirement_id": status["requirement_id"],
            "change_id": status["change_id"],
            "status_sha256": status_sha256_value,
            "material_manifest_sha256": material_manifest_sha256,
            "source_files_sha256": {key: source_hashes[key] for key in sorted(source_hashes)},
        }
    )
    return identity, source_hashes


_CHANGE_REVIEW_INPUTS = {
    "requirement_split": (
        "change-package.v1.json",
        "projected-requirement.v2.json",
        "projected-test-matrix.v2.json",
        "projected-reference-index.v2.json",
    ),
    "integrated_design": (
        "change-package.v1.json",
        "projected-requirement.v2.json",
        "projected-design.v2.json",
        "projected-reference-index.v2.json",
    ),
    "task_plan": (
        "change-package.v1.json",
        "projected-requirement.v2.json",
        "projected-design.v2.json",
        "projected-test-matrix.v2.json",
        "projected-reference-index.v2.json",
        "projected-task-plan.v2.json",
    ),
}

_CHANGE_REVIEW_CHECKS = {
    "requirement_split": (
        "核对预计需求、验收和引用只包含当前 CHG 明确声明的变化",
        "核对需求影响没有遗漏整体设计和任务计划审核",
    ),
    "integrated_design": (
        "核对预计设计与需求、引用和变更操作一致",
        "核对无关设计没有被当前 CHG 改写",
    ),
    "task_plan": (
        "核对完整任务计划与 restore、add、close、unaffected 关系一致",
        "核对活动任务保护范围没有遗漏",
    ),
}


def _read_committed_json(path: Path, *, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SdlcError(f"{label}不存在或不是普通文件。", exit_code=1)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是有效 JSON。", exit_code=1) from exc
    if not isinstance(value, dict):
        raise SdlcError(f"{label}顶层必须是对象。", exit_code=1)
    return value


def load_change_package_context_locked(
    paths: ProjectPaths,
    *,
    change_id: str,
    requirement_id: str | None = None,
) -> dict[str, object]:
    """调用方持有项目锁时，只消费 T-032 已登记且六文件哈希一致的结果。"""

    from codex_sdlc.core.change_contract import COMMITTED_FILE_NAMES
    from codex_sdlc.core.change_workspace import ensure_change_package_event_locked

    clean_change_id = str(change_id or "").strip().upper()
    clean_requirement_id = str(requirement_id or "").strip().upper()
    matches = []
    for event in load_events(paths):
        payload = event.get("payload")
        if (
            event.get("event_type") == "change_package_projected"
            and isinstance(payload, Mapping)
            and payload.get("change_id") == clean_change_id
            and (
                not clean_requirement_id
                or payload.get("requirement_id") == clean_requirement_id
            )
        ):
            matches.append(event)
    if len(matches) != 1:
        raise SdlcError(
            f"变更 {clean_change_id} 没有唯一完整的 change_package_projected 登记。",
            exit_code=1,
        )
    event = matches[0]
    ensure_change_package_event_locked(paths, event)
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        raise SdlcError("变更包登记缺少结构化 payload。", exit_code=1)
    workspace = (paths.root / str(payload.get("workspace_path") or "")).resolve()
    try:
        workspace.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise SdlcError("变更工作区不属于当前项目。", exit_code=1) from exc
    if workspace.is_symlink() or not workspace.is_dir():
        raise SdlcError("变更工作区不存在或不是普通目录。", exit_code=1)
    status_path = workspace / "status.json"
    if sha256_file(status_path) != payload.get("status_sha256"):
        raise SdlcError("变更工作区状态哈希已经漂移。", exit_code=1)
    committed_hashes = payload.get("committed_files_sha256")
    if not isinstance(committed_hashes, Mapping) or set(committed_hashes) != set(
        COMMITTED_FILE_NAMES
    ):
        raise SdlcError("变更包登记缺少六份固定文件哈希。", exit_code=1)
    documents: dict[str, dict[str, object]] = {}
    relative_paths: dict[str, str] = {}
    for name in COMMITTED_FILE_NAMES:
        target = workspace / name
        if sha256_file(target) != committed_hashes[name]:
            raise SdlcError(f"已登记变更文件哈希不一致：{name}。", exit_code=1)
        documents[name] = _read_committed_json(target, label=f"已登记变更文件 {name}")
        relative_paths[name] = target.relative_to(paths.root).as_posix()
    package = documents["change-package.v1.json"]
    if (
        package.get("change_id") != clean_change_id
        or package.get("requirement_id") != payload.get("requirement_id")
    ):
        raise SdlcError("已登记变更包与 CHG 所有权不一致。", exit_code=1)
    return {
        "event": deepcopy(event),
        "payload": deepcopy(dict(payload)),
        "workspace": workspace,
        "documents": documents,
        "relative_paths": relative_paths,
    }


def change_review_context_locked(
    paths: ProjectPaths,
    *,
    change_id: str,
    stage: str,
) -> dict[str, object]:
    """为既有 review create 入口重建当前 CHG 的固定审核输入。"""

    if stage not in _CHANGE_REVIEW_INPUTS:
        raise SdlcError(f"正式变更不支持审核阶段：{stage}。", exit_code=1)
    context = load_change_package_context_locked(paths, change_id=change_id)
    documents = context["documents"]
    relative_paths = context["relative_paths"]
    assert isinstance(documents, Mapping)
    assert isinstance(relative_paths, Mapping)
    package = documents["change-package.v1.json"]
    impacts = package.get("review_impacts") if isinstance(package, Mapping) else None
    declared = [
        str(item.get("stage") or "")
        for item in impacts if isinstance(impacts, list) and isinstance(item, Mapping)
    ]
    if declared.count(stage) != 1:
        raise SdlcError(
            f"变更 {change_id} 没有唯一声明 {stage} 审核影响。",
            exit_code=1,
        )
    return {
        "requirement_id": str(package["requirement_id"]),
        "change_id": str(package["change_id"]),
        "stage": stage,
        "input_paths": [str(relative_paths[name]) for name in _CHANGE_REVIEW_INPUTS[stage]],
        "required_checks": list(_CHANGE_REVIEW_CHECKS[stage]),
        "package_identity_sha256": context["payload"]["package_identity_sha256"],
    }


def _locked_task_impact_states(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    package: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str | None]]:
    """在项目锁内读取任务投影，并核对受影响任务的当前运行指针。"""

    state = derive_state(paths)
    requirements = state.get("requirements")
    requirement = requirements.get(requirement_id) if isinstance(requirements, Mapping) else None
    tasks = requirement.get("tasks") if isinstance(requirement, Mapping) else []
    task_states = {
        str(item["task_id"]): str(item["status"])
        for item in tasks
        if isinstance(item, Mapping)
        and isinstance(item.get("task_id"), str)
        and isinstance(item.get("status"), str)
    }
    impacts = package.get("task_impacts")
    affected: set[str] = set()
    if isinstance(impacts, Mapping):
        for field in ("restore", "close"):
            entries = impacts.get(field)
            for item in entries if isinstance(entries, list) else []:
                if isinstance(item, Mapping) and isinstance(item.get("task_id"), str):
                    affected.add(str(item["task_id"]))
    requirement_root = requirement_dir_for_id(paths, requirement_id)
    run_states: dict[str, str | None] = {}
    for task_id in affected:
        current_path = (
            requirement_root / "runtime" / task_id / "current.json"
            if requirement_root is not None
            else None
        )
        if current_path is None or (
            not current_path.exists() and not current_path.is_symlink()
        ):
            run_states[task_id] = None
            continue
        context = load_task_run_context(
            paths,
            requirement_id=requirement_id,
            task_id=task_id,
        )
        current = context["current"]
        run = context["run"]
        if current.get("status") != run.get("status"):
            raise SdlcError(f"任务 {task_id} 的当前指针与运行状态不一致。")
        run_states[task_id] = str(run.get("status") or "")
    return task_states, run_states


def submit_change_package(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
    package_path: str,
    projected_paths: Mapping[str, str],
    interruption_hook: InterruptionHook | None = None,
) -> ChangePackageResult:
    """在同一项目锁和事件锁内完成恢复、纯计算、六文件发布和唯一事件。"""

    from codex_sdlc.core.change_contract import (
        collect_existing_ids,
        load_source_documents,
        prepare_change_package,
    )

    if (
        paths.sdlc_dir.is_symlink()
        or not paths.sdlc_dir.is_dir()
        or paths.events_file.is_symlink()
        or not paths.events_file.is_file()
    ):
        raise SdlcError("当前项目还没初始化，请先使用 `$sdlc-init`。")
    require_matching_sdlc_identity(paths)
    ensure_base_dirs(paths)
    hook = interruption_hook or (lambda _stage: None)

    with project_lock(paths):
        require_matching_sdlc_identity(paths)
        with event_write_lock(paths):
            events = load_events(paths)
            workspace, creation_event, status = resolve_registered_change_workspace(
                paths,
                events,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            status_path = workspace / "status.json"
            status_before = status_path.read_bytes()
            recover_change_package_transactions_locked(paths, workspace)

            events = load_events(paths)
            workspace, creation_event, status = resolve_registered_change_workspace(
                paths,
                events,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            manifest = load_change_material_manifest(workspace, status)
            verify_change_material_state(paths, workspace, status, manifest, events)
            source_documents = load_source_documents(
                paths,
                package_path=package_path,
                projected_paths=projected_paths,
            )
            manifest_path = change_material_manifest_path(workspace)
            manifest_hash = (
                sha256_file(manifest_path)
                if manifest_path.is_file() and not manifest_path.is_symlink()
                else None
            )
            current_status_hash = sha256_file(status_path)
            identity, source_hashes = _source_identity(
                source_documents,
                status=status,
                material_manifest_sha256=manifest_hash,
                status_sha256_value=current_status_hash,
            )
            existing = change_package_events(
                events,
                workspace_path=str(status["workspace_path"]),
            )
            if existing:
                if len(existing) != 1:
                    raise SdlcError("同一 CHG 存在多个 change_package_projected 事件。")
                # 幂等返回也必须重新走完整事件形状校验，不能只相信几项 payload 字段。
                ensure_change_package_event_locked(paths, existing[0])
                payload = existing[0].get("payload")
                if not isinstance(payload, Mapping):
                    raise SdlcError("变更包成功事件缺少结构化 payload。")
                if (
                    payload.get("package_identity_sha256") != identity
                    or payload.get("source_files_sha256") != source_hashes
                    or payload.get("status_sha256") != current_status_hash
                    or payload.get("material_manifest_sha256") != manifest_hash
                ):
                    raise SdlcError("同一 CHG 已经提交了不同身份的完整变更包。")
                committed_hashes = payload.get("committed_files_sha256")
                if not isinstance(committed_hashes, Mapping):
                    raise SdlcError("变更包成功事件缺少六份提交文件哈希。")
                for name, digest in committed_hashes.items():
                    target = workspace / str(name)
                    if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
                        raise SdlcError(f"幂等重试发现已提交文件漂移：{name}。")
                if status_path.read_bytes() != status_before:
                    raise SdlcError("变更包幂等核对过程中 status.json 发生变化。")
                return _package_result_from_event(existing[0], duplicate=True)

            package = source_documents.get("change-package.v1.json")
            if not isinstance(package, Mapping):
                raise SdlcError("变更包来源不是 JSON 对象。")
            task_states, task_run_states = _locked_task_impact_states(
                paths,
                requirement_id=requirement_id,
                package=package,
            )
            prepared = prepare_change_package(
                paths,
                status=status,
                manifest=manifest,
                source_documents=source_documents,
                existing_ids=collect_existing_ids(paths, events),
                task_states=task_states,
                task_run_states=task_run_states,
            )
            if prepared.package_identity_sha256 != identity:
                raise SdlcError("变更包纯计算身份与锁内来源身份不一致。")
            event = build_change_package_event(
                paths,
                event_id=new_event_id(events),
                status=status,
                package_identity_sha256=prepared.package_identity_sha256,
                material_manifest_sha256=prepared.material_manifest_sha256,
                source_files_sha256=prepared.source_files_sha256,
                id_mapping=prepared.id_mapping,
                committed_files_sha256=prepared.committed_files_sha256,
            )
            transaction = build_change_package_transaction(
                status=status,
                prepared=prepared,
                event=event,
            )
            journal = write_change_package_transaction(paths, workspace, transaction)
            stage_change_package_files(
                workspace,
                transaction,
                prepared.committed_file_bytes,
            )
            hook(INTERRUPT_BEFORE_PACKAGE_PUBLISH)
            publish_change_package_files(workspace, transaction)
            hook(INTERRUPT_AFTER_PACKAGE_PUBLISH)
            ensure_change_package_event_locked(paths, event)
            hook(INTERRUPT_AFTER_PACKAGE_EVENT_APPEND)

            for name, digest in prepared.committed_files_sha256.items():
                target = workspace / name
                if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
                    raise SdlcError(f"提交后的变更包文件无法互证：{name}。")
            verify_workspace_event(
                paths,
                workspace,
                creation_event,
                verify_current_bases=True,
            )
            if status_path.read_bytes() != status_before:
                raise SdlcError("提交变更包时不能修改 status.json。")
            cleanup_change_package_transaction(workspace, transaction, journal)
            return _package_result_from_event(event, duplicate=False)


def _protection_body(document: Mapping[str, object]) -> dict[str, object]:
    return {key: deepcopy(value) for key, value in document.items() if key != "protection_sha256"}


def _write_protection_document(path: Path, document: Mapping[str, object]) -> None:
    from codex_sdlc.core.structured_contract import canonical_json_text

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(canonical_json_text(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ensure_change_protection_event(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
    protection_path: str,
    protection_sha256: str,
    package_identity_sha256: str,
) -> None:
    from codex_sdlc.core.state import append_event

    matching = [
        event
        for event in load_events(paths)
        if event.get("event_type") == "change_protected"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("change_id") == change_id
    ]
    expected_payload = {
        "requirement_id": requirement_id,
        "change_id": change_id,
        "protection_path": protection_path,
        "protection_sha256": protection_sha256,
        "package_identity_sha256": package_identity_sha256,
    }
    if matching:
        if len(matching) != 1 or matching[0].get("payload") != expected_payload:
            raise SdlcError(f"变更 {change_id} 已经登记了不同的保护结果。", exit_code=1)
        return
    append_event(
        paths,
        event_type="change_protected",
        source="sdlc-change-protect",
        summary=f"完成变更保护 {change_id}",
        requirement_id=requirement_id,
        payload=expected_payload,
    )


def protect_change_package(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
    confirm_requirement: bool,
) -> dict[str, object]:
    """核对三类审核和 unaffected 证据，再暂停受影响活动任务并登记结果。"""

    from codex_sdlc.core import dependency_graph
    from codex_sdlc.core.state import now_iso, refresh_materialized_state
    from codex_sdlc.services import review_service

    require_matching_sdlc_identity(paths)
    with project_lock(paths):
        context = load_change_package_context_locked(
            paths,
            requirement_id=requirement_id,
            change_id=change_id,
        )
        workspace = context["workspace"]
        payload = context["payload"]
        documents = context["documents"]
        relative_paths = context["relative_paths"]
        assert isinstance(workspace, Path)
        assert isinstance(payload, Mapping)
        assert isinstance(documents, Mapping)
        assert isinstance(relative_paths, Mapping)
        package = documents["change-package.v1.json"]
        if not isinstance(package, Mapping):
            raise SdlcError("已登记变更包不是对象。", exit_code=1)
        clean_requirement_id = str(package["requirement_id"])
        clean_change_id = str(package["change_id"])
        protection_path = workspace / "change-protection.v1.json"
        protection_relative = protection_path.relative_to(paths.root).as_posix()
        if protection_path.exists() or protection_path.is_symlink():
            existing = _read_committed_json(protection_path, label="变更保护结果")
            if existing.get("schema_version") != "change-protection.v1" or (
                existing.get("protection_sha256")
                != canonical_sha256(_protection_body(existing))
            ):
                raise SdlcError("变更保护结果格式或哈希不正确。", exit_code=1)
            if (
                existing.get("requirement_id") != clean_requirement_id
                or existing.get("change_id") != clean_change_id
                or existing.get("package_identity_sha256")
                != payload.get("package_identity_sha256")
            ):
                raise SdlcError("变更保护结果与当前 CHG 所有权不一致。", exit_code=1)
            _ensure_change_protection_event(
                paths,
                requirement_id=clean_requirement_id,
                change_id=clean_change_id,
                protection_path=protection_relative,
                protection_sha256=str(existing["protection_sha256"]),
                package_identity_sha256=str(payload["package_identity_sha256"]),
            )
            return {**deepcopy(existing), "idempotent": True}

        impacts = package.get("review_impacts")
        if not isinstance(impacts, list):
            raise SdlcError("变更包 review_impacts 不是数组。", exit_code=1)
        declared_stages = [
            str(item.get("stage") or "") for item in impacts if isinstance(item, Mapping)
        ]
        if len(declared_stages) != len(impacts) or len(set(declared_stages)) != len(
            declared_stages
        ):
            raise SdlcError("review_impacts 必须为每个固定审核阶段最多声明一次。", exit_code=1)
        required_stages = dependency_graph.required_change_review_stages(package)
        missing_stages = [stage for stage in required_stages if stage not in declared_stages]
        if missing_stages:
            raise SdlcError(
                "变更操作缺少必须重新执行的审核：" + "、".join(missing_stages) + "。",
                exit_code=1,
            )

        review_state = review_service.review_status(paths)
        review_records: list[dict[str, object]] = []
        for stage in declared_stages:
            candidates = [
                item
                for item in review_state.get("reviews", [])
                if isinstance(item, Mapping)
                and item.get("owner_id") == clean_change_id
                and item.get("stage") == stage
                and item.get("is_current") is True
            ]
            if len(candidates) != 1 or candidates[0].get("can_advance") is not True:
                raise SdlcError(
                    f"变更 {clean_change_id} 的 {stage} 审核缺失、失效或未通过。",
                    exit_code=1,
                )
            item = candidates[0]
            review_records.append(
                {
                    "stage": stage,
                    "review_id": item["review_id"],
                    "registration_id": item["registration_id"],
                    "reviewer_run_id": item["reviewer_run_id"],
                    "input_hashes": deepcopy(item["input_hashes"]),
                    "input_fingerprint_sha256": canonical_sha256(item["input_hashes"]),
                }
            )
        if "requirement_split" in declared_stages and not confirm_requirement:
            raise SdlcError(
                "预计需求已经变化，请在审核通过后由用户明确确认当前 CHG 的需求版本。",
                exit_code=1,
            )

        state = derive_state(paths)
        requirements = state.get("requirements")
        requirement = (
            requirements.get(clean_requirement_id)
            if isinstance(requirements, Mapping)
            else None
        )
        if not isinstance(requirement, Mapping):
            raise SdlcError(f"正式需求不存在：{clean_requirement_id}。", exit_code=1)
        tasks = [item for item in requirement.get("tasks", []) if isinstance(item, Mapping)]
        unaffected_items = package.get("task_impacts", {}).get("unaffected", [])
        unaffected_by_id = {
            str(item["task_id"]): item
            for item in unaffected_items
            if isinstance(unaffected_items, list)
            and isinstance(item, Mapping)
            and isinstance(item.get("task_id"), str)
        }
        base_reference = documents["projected-reference-index.v2.json"]["base"]
        base_reference_path = paths.root / str(base_reference["path"])
        if sha256_file(base_reference_path) != base_reference["sha256"]:
            raise SdlcError("基础引用索引已经漂移，不能执行变更保护。", exit_code=1)
        base_reference_document = _read_committed_json(
            base_reference_path, label="基础引用索引"
        )
        projected_reference_document = documents["projected-reference-index.v2.json"]
        projected_reference_content = projected_reference_document.get("content")
        if not isinstance(projected_reference_content, Mapping):
            raise SdlcError("预计引用索引缺少完整 content。", exit_code=1)

        unaffected_proofs: list[dict[str, object]] = []
        affected_task_ids: list[str] = []
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            task_status = str(task.get("status") or "")
            run_status: str | None = None
            requirement_root = requirement_dir_for_id(paths, clean_requirement_id)
            current_path = (
                requirement_root / "runtime" / task_id / "current.json"
                if requirement_root is not None
                else None
            )
            if current_path is not None and (current_path.exists() or current_path.is_symlink()):
                run_context = load_task_run_context(
                    paths,
                    requirement_id=clean_requirement_id,
                    task_id=task_id,
                )
                run_status = str(run_context["run"].get("status") or "")
            active = task_status in {"doing", "ready_for_user_check", "test_failed"} or run_status in {
                "reading",
                "active",
                "stale",
            }
            if not active:
                continue
            unaffected = unaffected_by_id.get(task_id)
            if unaffected is None:
                affected_task_ids.append(task_id)
                continue
            if requirement_root is None:
                raise SdlcError("正式需求目录不存在，不能核对 unaffected。", exit_code=1)
            task_document = _read_committed_json(
                requirement_root / "tasks" / f"{task_id}.json",
                label=f"任务合同 {task_id}",
            )
            proof = dependency_graph.prove_task_unaffected(
                task_document,
                basis_refs=unaffected.get("basis_refs", []),
                base_reference_index=base_reference_document,
                projected_reference_index=projected_reference_content,
            )
            unaffected_proofs.append(proof)

        protected_tasks = [
            protect_task_run_for_change(
                paths,
                requirement_id=clean_requirement_id,
                task_id=task_id,
                change_id=clean_change_id,
            )
            for task_id in affected_task_ids
        ]
        if protected_tasks:
            refresh_materialized_state(paths)

        confirmation = {
            "mode": (
                "confirmed_for_change"
                if "requirement_split" in declared_stages
                else "reused_unchanged_requirement"
            ),
            "requirement_review_id": next(
                (
                    item["review_id"]
                    for item in review_records
                    if item["stage"] == "requirement_split"
                ),
                None,
            ),
            "projected_requirement_sha256": str(
                payload["committed_files_sha256"]["projected-requirement.v2.json"]
            ),
        }
        body: dict[str, object] = {
            "schema_version": "change-protection.v1",
            "requirement_id": clean_requirement_id,
            "change_id": clean_change_id,
            "package_event_id": context["event"]["event_id"],
            "package_identity_sha256": payload["package_identity_sha256"],
            "review_stages": declared_stages,
            "reviews": review_records,
            "requirement_confirmation": confirmation,
            "unaffected_tasks": unaffected_proofs,
            "protected_tasks": protected_tasks,
            "created_at": now_iso(),
        }
        document = {**body, "protection_sha256": canonical_sha256(body)}
        _write_protection_document(protection_path, document)
        _ensure_change_protection_event(
            paths,
            requirement_id=clean_requirement_id,
            change_id=clean_change_id,
            protection_path=protection_relative,
            protection_sha256=str(document["protection_sha256"]),
            package_identity_sha256=str(payload["package_identity_sha256"]),
        )
        return {**deepcopy(document), "idempotent": False}


def _accept_roots(paths: ProjectPaths) -> tuple[Path, Path, Path]:
    root = paths.change_transactions_dir
    active = root / _ACCEPT_ACTIVE_DIR
    completed = root / _ACCEPT_COMPLETED_DIR
    staging = root / _ACCEPT_STAGING_DIR
    for directory in (root, active, completed, staging):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise SdlcError(f"正式变更事务目录必须是真实目录：{directory}。", exit_code=1)
        else:
            directory.mkdir(parents=True, mode=0o700)
    return active, completed, staging


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """先完整落盘再替换目标，避免单个 JSON 文件出现半写状态。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(document: Mapping[str, object]) -> bytes:
    from codex_sdlc.core.structured_contract import canonical_json_text

    return canonical_json_text(document).encode("utf-8")


def _controlled_transaction_path(paths: ProjectPaths, relative: object) -> Path:
    clean = str(relative or "")
    requested = Path(clean)
    if (
        not clean
        or requested.is_absolute()
        or ".." in requested.parts
        or requested.as_posix() != clean
    ):
        raise SdlcError(f"正式变更事务包含不安全路径：{relative}。", exit_code=1)
    target = paths.root / requested
    current = paths.root
    for part in requested.parts:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"正式变更事务路径不能经过符号链接：{clean}。", exit_code=1)
    try:
        target.resolve(strict=False).relative_to(paths.root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(f"正式变更事务路径越过项目目录：{clean}。", exit_code=1) from exc
    return target


def _read_accept_journal(path: Path) -> dict[str, object]:
    document = _read_committed_json(path, label="正式变更事务记录")
    required = {
        "schema_version",
        "transaction_id",
        "requirement_id",
        "change_id",
        "target_version",
        "requirement_root",
        "workspace_path",
        "package_identity_sha256",
        "protection_sha256",
        "event_start_size",
        "event_prefix_sha256",
        "events",
        "files",
        "staging_path",
    }
    if document.get("schema_version") != _ACCEPT_TRANSACTION_SCHEMA or not required <= set(document):
        raise SdlcError(f"正式变更事务记录字段不完整：{path.name}。", exit_code=1)
    if path.stem != document.get("transaction_id"):
        raise SdlcError(f"正式变更事务文件名与事务编号不一致：{path.name}。", exit_code=1)
    return document


def _event_bytes(events: object) -> bytes:
    from codex_sdlc.core.structured_contract import canonical_json_text

    if not isinstance(events, list) or not all(isinstance(item, Mapping) for item in events):
        raise SdlcError("正式变更事务缺少结构化事件。", exit_code=1)
    return b"".join(canonical_json_text(item).encode("utf-8") for item in events)


def _event_position(paths: ProjectPaths, transaction: Mapping[str, object]) -> str:
    try:
        start = int(transaction["event_start_size"])
        expected = _event_bytes(transaction["events"])
        current = paths.events_file.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise SdlcError("无法核对正式变更事务的事件边界。", exit_code=1) from exc
    if (
        start < 0
        or len(current) < start
        or sha256_bytes(current[:start]) != transaction.get("event_prefix_sha256")
    ):
        raise SdlcError("正式变更事务的事件起点无效。", exit_code=1)
    if len(current) == start:
        return "before"
    if len(current) >= start + len(expected) and current[start : start + len(expected)] == expected:
        if len(current) != start + len(expected):
            raise SdlcError("正式变更事务后出现了未核对的新事件，已停止恢复。", exit_code=1)
        return "after"
    raise SdlcError("正式变更事务的事件区间与记录不一致。", exit_code=1)


def _transaction_file_records(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
) -> list[dict[str, object]]:
    records = transaction.get("files")
    if not isinstance(records, list) or not records or not all(isinstance(item, Mapping) for item in records):
        raise SdlcError("正式变更事务缺少完整文件清单。", exit_code=1)
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    staging = _controlled_transaction_path(paths, transaction["staging_path"])
    for raw in records:
        item = dict(raw)
        relative = str(item.get("path") or "")
        if relative in seen:
            raise SdlcError(f"正式变更事务重复登记文件：{relative}。", exit_code=1)
        seen.add(relative)
        target = _controlled_transaction_path(paths, relative)
        new_file = staging / str(item.get("new_file") or "")
        old_name = item.get("old_file")
        old_file = staging / str(old_name) if isinstance(old_name, str) and old_name else None
        if new_file.is_symlink() or not new_file.is_file() or sha256_file(new_file) != item.get("new_sha256"):
            raise SdlcError(f"正式变更事务的新文件证据无效：{relative}。", exit_code=1)
        if old_file is not None and (
            old_file.is_symlink()
            or not old_file.is_file()
            or sha256_file(old_file) != item.get("old_sha256")
        ):
            raise SdlcError(f"正式变更事务的旧文件证据无效：{relative}。", exit_code=1)
        result.append({**item, "target": target, "new_path": new_file, "old_path": old_file})
    return result


def _publish_transaction_files(
    records: list[dict[str, object]],
    hook: InterruptionHook,
) -> None:
    for item in records:
        target = item["target"]
        new_path = item["new_path"]
        assert isinstance(target, Path) and isinstance(new_path, Path)
        if target.is_symlink():
            raise SdlcError(f"正式变更目标不能是符号链接：{item['path']}。", exit_code=1)
        _atomic_write_bytes(target, new_path.read_bytes())
        if sha256_file(target) != item["new_sha256"]:
            raise SdlcError(f"正式变更文件替换后哈希不一致：{item['path']}。", exit_code=1)
        hook(str(item["stage"]))


def _rollback_accept_transaction(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    journal: Path,
) -> None:
    records = _transaction_file_records(paths, transaction)
    for item in reversed(records):
        target = item["target"]
        old_path = item["old_path"]
        assert isinstance(target, Path)
        if isinstance(old_path, Path):
            _atomic_write_bytes(target, old_path.read_bytes())
        else:
            target.unlink(missing_ok=True)
    start = int(transaction["event_start_size"])
    with paths.events_file.open("r+b") as handle:
        handle.truncate(start)
        handle.flush()
        os.fsync(handle.fileno())
    staging = _controlled_transaction_path(paths, transaction["staging_path"])
    shutil.rmtree(staging, ignore_errors=True)
    _completed_receipt_path(paths, str(transaction["transaction_id"])).unlink(
        missing_ok=True
    )
    journal.unlink(missing_ok=True)


def _completed_receipt_path(paths: ProjectPaths, transaction_id: str) -> Path:
    _active, completed, _staging = _accept_roots(paths)
    return completed / f"{transaction_id}.json"


def _receipt_result(receipt: Mapping[str, object], *, idempotent: bool) -> dict[str, object]:
    version_hashes = receipt.get("version_files_sha256")
    if not isinstance(version_hashes, Mapping):
        raise SdlcError("正式变更完成回执缺少五类版本哈希。", exit_code=1)
    return {
        "requirement_id": str(receipt["requirement_id"]),
        "change_id": str(receipt["change_id"]),
        "transaction_id": str(receipt["transaction_id"]),
        "target_version": int(receipt["target_version"]),
        "version_files_sha256": {str(key): str(value) for key, value in version_hashes.items()},
        "receipt_path": str(receipt["receipt_path"]),
        "idempotent": idempotent,
    }


def _verify_completed_receipt(paths: ProjectPaths, receipt_path: Path) -> dict[str, object]:
    receipt = _read_committed_json(receipt_path, label="正式变更完成回执")
    if receipt.get("schema_version") != "change-accept-receipt.v1":
        raise SdlcError(f"正式变更完成回执版本无效：{receipt_path.name}。", exit_code=1)
    transaction_id = str(receipt.get("transaction_id") or "")
    if receipt_path.name != f"{transaction_id}.json":
        raise SdlcError(f"正式变更完成回执文件名与事务编号不一致：{receipt_path.name}。", exit_code=1)
    files = receipt.get("files_sha256")
    if not isinstance(files, Mapping):
        raise SdlcError("正式变更完成回执缺少提交文件哈希。", exit_code=1)
    for relative, digest in files.items():
        target = _controlled_transaction_path(paths, relative)
        if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
            raise SdlcError(f"正式变更完成回执与文件不一致：{relative}。", exit_code=1)
    try:
        start = int(receipt["event_start_size"])
        events = _event_bytes(receipt["events"])
        current = paths.events_file.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        raise SdlcError("正式变更完成回执缺少可核对事件区间。", exit_code=1) from exc
    if len(current) < start + len(events) or current[start : start + len(events)] != events:
        raise SdlcError("正式变更完成回执与事件流水不一致。", exit_code=1)
    return receipt


def _finish_accept_transaction_locked(
    paths: ProjectPaths,
    transaction: Mapping[str, object],
    journal: Path,
) -> dict[str, object]:
    records = _transaction_file_records(paths, transaction)
    position = _event_position(paths, transaction)
    # 只要进程已经进入提交阶段，恢复统一向前补齐，避免重复请求在成功和回滚间摇摆。
    version_records = [
        item
        for item in records
        if "/versions/" in f"/{str(item['path'])}"
    ]
    pointer_records = [item for item in records if item not in version_records]
    _publish_transaction_files(version_records, lambda _stage: None)
    if position == "before":
        with paths.events_file.open("ab") as handle:
            handle.write(_event_bytes(transaction["events"]))
            handle.flush()
            os.fsync(handle.fileno())
    _publish_transaction_files(pointer_records, lambda _stage: None)
    all_hashes = {str(item["path"]): str(item["new_sha256"]) for item in records}
    requirement_prefix = str(transaction["requirement_root"]).rstrip("/") + "/"
    version_hashes = {
        str(item["path"])[len(requirement_prefix) :]: str(item["new_sha256"])
        for item in version_records
        if str(item["path"]).startswith(requirement_prefix)
    }
    receipt_path = _completed_receipt_path(paths, str(transaction["transaction_id"]))
    receipt = {
        "schema_version": "change-accept-receipt.v1",
        "transaction_id": transaction["transaction_id"],
        "requirement_id": transaction["requirement_id"],
        "change_id": transaction["change_id"],
        "target_version": transaction["target_version"],
        "package_identity_sha256": transaction["package_identity_sha256"],
        "protection_sha256": transaction["protection_sha256"],
        "event_start_size": transaction["event_start_size"],
        "events": transaction["events"],
        "files_sha256": all_hashes,
        "version_files_sha256": version_hashes,
        "receipt_path": receipt_path.relative_to(paths.root).as_posix(),
    }
    _atomic_write_bytes(receipt_path, _json_bytes(receipt))
    verified = _verify_completed_receipt(paths, receipt_path)
    staging = _controlled_transaction_path(paths, transaction["staging_path"])
    shutil.rmtree(staging, ignore_errors=True)
    journal.unlink(missing_ok=True)
    return _receipt_result(verified, idempotent=False)


def _active_accept_journals(paths: ProjectPaths) -> list[Path]:
    active, _completed, _staging = _accept_roots(paths)
    entries = sorted(active.iterdir())
    if any(item.is_symlink() or not item.is_file() or item.suffix != ".json" for item in entries):
        raise SdlcError("正式变更活动事务目录包含无法识别的文件。", exit_code=1)
    return entries


def _orphan_accept_staging_entries(paths: ProjectPaths) -> list[Path]:
    """找出没有活动日志归属的暂存项；证据不足时只报告，不猜测删除。"""

    active, _completed, staging = _accept_roots(paths)
    active_transaction_ids = {item.stem for item in _active_accept_journals(paths)}
    orphans: list[Path] = []
    for entry in sorted(staging.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or entry.name not in active_transaction_ids:
            orphans.append(entry)
    return orphans


def _recover_change_accept_transactions_locked(paths: ProjectPaths) -> dict[str, int]:
    completed = 0
    for journal in _active_accept_journals(paths):
        transaction = _read_accept_journal(journal)
        _finish_accept_transaction_locked(paths, transaction, journal)
        completed += 1
    return {"completed": completed}


def recover_change_accept_transactions(paths: ProjectPaths) -> dict[str, int]:
    """普通命令进入业务读取前调用；损坏现场必须报错，不能猜测清理。"""

    if not paths.sdlc_dir.is_dir():
        return {"completed": 0}
    with project_lock(paths):
        with event_write_lock(paths):
            return _recover_change_accept_transactions_locked(paths)


def require_no_unrecovered_change_accept_transaction(paths: ProjectPaths) -> None:
    active = _active_accept_journals(paths)
    if active:
        raise SdlcError(
            "存在尚未恢复的正式变更事务，不能备份："
            + "、".join(item.name for item in active[:5])
            + "。",
            exit_code=1,
        )
    orphans = _orphan_accept_staging_entries(paths)
    if orphans:
        raise SdlcError(
            "存在无法安全归属活动事务的正式变更暂存目录，不能备份："
            + "、".join(item.name for item in orphans[:5])
            + "。",
            exit_code=1,
        )


def inspect_change_accept_transactions(paths: ProjectPaths) -> dict[str, object]:
    """doctor 使用完成回执双向核对正式文件和事件，不根据展示文字猜状态。"""

    active, completed_root, _staging = _accept_roots(paths)
    report: dict[str, object] = {"active": [], "completed": [], "failed": []}
    report["active"] = [item.name for item in sorted(active.iterdir())]
    for entry in _orphan_accept_staging_entries(paths):
        report["failed"].append(
            f"正式变更暂存目录无法安全归属活动事务：{entry.name}。"
        )
    for receipt_path in sorted(completed_root.glob("*.json")):
        try:
            receipt = _verify_completed_receipt(paths, receipt_path)
            report["completed"].append(str(receipt["transaction_id"]))
        except SdlcError as exc:
            report["failed"].append(exc.message)
    return report


def _find_completed_change_receipt(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
) -> dict[str, object] | None:
    _active, completed, _staging = _accept_roots(paths)
    matches: list[dict[str, object]] = []
    for path in sorted(completed.glob("*.json")):
        receipt = _verify_completed_receipt(paths, path)
        if receipt.get("requirement_id") == requirement_id and receipt.get("change_id") == change_id:
            matches.append(receipt)
    if len(matches) > 1:
        raise SdlcError(f"变更 {change_id} 存在多个正式完成回执。", exit_code=1)
    return matches[0] if matches else None


def _validate_change_protection(
    paths: ProjectPaths,
    context: Mapping[str, object],
) -> dict[str, object]:
    workspace = context["workspace"]
    payload = context["payload"]
    assert isinstance(workspace, Path) and isinstance(payload, Mapping)
    protection_path = workspace / "change-protection.v1.json"
    protection = _read_committed_json(protection_path, label="变更保护结果")
    if (
        protection.get("schema_version") != "change-protection.v1"
        or protection.get("protection_sha256") != canonical_sha256(_protection_body(protection))
        or protection.get("requirement_id") != payload.get("requirement_id")
        or protection.get("change_id") != payload.get("change_id")
        or protection.get("package_identity_sha256") != payload.get("package_identity_sha256")
    ):
        raise SdlcError("变更保护结果格式、所有权或哈希不正确。", exit_code=1)
    relative = protection_path.relative_to(paths.root).as_posix()
    matching = [
        event
        for event in load_events(paths)
        if event.get("event_type") == "change_protected"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("change_id") == payload.get("change_id")
    ]
    expected = {
        "requirement_id": payload["requirement_id"],
        "change_id": payload["change_id"],
        "protection_path": relative,
        "protection_sha256": protection["protection_sha256"],
        "package_identity_sha256": payload["package_identity_sha256"],
    }
    if len(matching) != 1 or matching[0].get("payload") != expected:
        raise SdlcError("变更保护结果没有唯一匹配的正式事件。", exit_code=1)
    return protection


def _target_version(documents: Mapping[str, object]) -> int:
    values: list[int] = []
    for filename, field in (
        ("projected-requirement.v2.json", "requirement"),
        ("projected-design.v2.json", "design"),
        ("projected-test-matrix.v2.json", "test-matrix"),
    ):
        document = documents.get(filename)
        content = document.get("content") if isinstance(document, Mapping) else None
        version = content.get("version") if isinstance(content, Mapping) else None
        match = _VERSION_PATTERN.fullmatch(str(version or ""))
        if match is None:
            raise SdlcError(f"{filename} 缺少合法目标版本。", exit_code=1)
        values.append(int(match.group(1)))
    if len(set(values)) != 1 or values[0] < 2:
        raise SdlcError("三类预计正式版本号不一致或没有递增。", exit_code=1)
    return values[0]


def _build_accept_events(
    paths: ProjectPaths,
    *,
    context: Mapping[str, object],
    protection: Mapping[str, object],
    transaction_id: str,
    target_version: int,
) -> list[dict[str, object]]:
    from codex_sdlc.core.state import next_event_id, now_iso

    payload = context["payload"]
    documents = context["documents"]
    assert isinstance(payload, Mapping) and isinstance(documents, Mapping)
    package = documents["change-package.v1.json"]
    assert isinstance(package, Mapping)
    events = load_events(paths)
    created_at = now_iso()

    def add(event_type: str, summary: str, event_payload: dict[str, object], task_id: str | None = None) -> None:
        event = {
            "event_id": next_event_id(events),
            "event_type": event_type,
            "project_path": str(paths.root),
            "requirement_id": payload["requirement_id"],
            "task_id": task_id,
            "created_at": created_at,
            "source": "sdlc-change-accept",
            "summary": summary,
            "payload": event_payload,
        }
        events.append(event)

    add(
        "change_accepted",
        f"事务生效变更 {payload['change_id']}",
        {
            "change_ids": [payload["change_id"]],
            "status": "effective",
            "confirmation": "已确认",
            "transaction_id": transaction_id,
            "target_version": target_version,
            "package_identity_sha256": payload["package_identity_sha256"],
            "protection_sha256": protection["protection_sha256"],
            "task_impacts": deepcopy(package.get("task_impacts", {})),
        },
    )
    add(
        "change_reviews_invalidated",
        f"新版本生效后失效旧审核 {payload['change_id']}",
        {
            "change_id": payload["change_id"],
            "target_version": target_version,
            "review_ids": [
                item.get("review_id")
                for item in protection.get("reviews", [])
                if isinstance(item, Mapping) and item.get("review_id")
            ],
        },
    )
    impacts = package.get("task_impacts")
    if isinstance(impacts, Mapping):
        projected = documents["projected-task-plan.v2.json"]
        projected_content = projected.get("content") if isinstance(projected, Mapping) else None
        mapping = projected_content.get("mapping", {}) if isinstance(projected_content, Mapping) else {}
        for item in impacts.get("restore", []):
            if isinstance(item, Mapping):
                task_id = str(item["task_id"])
                add("task_updated", f"变更恢复任务 {task_id}", {"status": "todo", "note": item["reason"]}, task_id)
        for item in impacts.get("close", []):
            if isinstance(item, Mapping):
                task_id = str(item["task_id"])
                add("task_updated", f"变更关闭任务 {task_id}", {"status": "closed", "note": item["reason"]}, task_id)
        for item in impacts.get("add", []):
            if isinstance(item, Mapping) and isinstance(item.get("next_value"), Mapping):
                task_id = str(mapping.get(str(item.get("client_key"))) or "")
                if not re.fullmatch(r"T-[0-9]{3,}", task_id):
                    raise SdlcError("新增任务缺少预计任务计划中的正式编号。", exit_code=1)
                task_payload = deepcopy(dict(item["next_value"]))
                title = str(task_payload.get("title") or "").strip()
                goal = str(task_payload.get("goal") or "").strip()
                if not title or not goal:
                    raise SdlcError(
                        f"新增任务 {task_id} 缺少完整 title 或 goal，不能写入任务事件。",
                        exit_code=1,
                    )
                # 事件状态仍使用既有 summary 字段；值直接来自结构化 goal，
                # 不从标题或自然语言正文另行推断。
                task_payload["summary"] = goal
                task_payload["status"] = "todo"
                add("task_created", f"变更新增任务 {task_id}", task_payload, task_id)
    return events[len(load_events(paths)) :]


def _formalize_change_reference_paths(
    paths: ProjectPaths,
    *,
    requirement_root: Path,
    workspace: Path,
    reference_index: Mapping[str, object],
) -> tuple[dict[str, object], list[tuple[str, bytes, str]]]:
    """把 CHG 内不可变来源归档到正式目录，再让引用只指向归档副本。"""

    result = deepcopy(dict(reference_index))
    entries = result.get("entries")
    if not isinstance(entries, Mapping):
        raise SdlcError("预计引用索引缺少 entries。", exit_code=1)
    workspace_relative = workspace.relative_to(paths.root).as_posix().rstrip("/")
    archive_root = requirement_root / "original" / "changes" / workspace.name
    archives: dict[str, tuple[str, bytes, str]] = {}

    def formalize(raw_path: object) -> str:
        clean = str(raw_path or "")
        prefix = workspace_relative + "/"
        if not clean.startswith(prefix):
            return clean
        suffix = clean[len(prefix) :]
        source = _controlled_transaction_path(paths, clean)
        if source.is_symlink() or not source.is_file():
            raise SdlcError(f"变更引用来源不存在或不是普通文件：{clean}。", exit_code=1)
        target = archive_root / suffix
        relative = target.relative_to(requirement_root).as_posix()
        archives[relative] = (
            target.relative_to(paths.root).as_posix(),
            source.read_bytes(),
            "after_reference_source",
        )
        return relative

    normalized_entries: dict[str, object] = {}
    for reference_id, raw_reference in entries.items():
        if not isinstance(raw_reference, Mapping):
            raise SdlcError(f"预计引用 {reference_id} 不是对象。", exit_code=1)
        reference = deepcopy(dict(raw_reference))
        reference["path"] = formalize(reference.get("path"))
        locator = reference.get("locator")
        if isinstance(locator, Mapping) and "node_index_path" in locator:
            normalized_locator = deepcopy(dict(locator))
            normalized_locator["node_index_path"] = formalize(
                normalized_locator.get("node_index_path")
            )
            reference["locator"] = normalized_locator
        normalized_entries[str(reference_id)] = reference
    result["entries"] = {
        key: normalized_entries[key] for key in sorted(normalized_entries)
    }
    return result, [archives[key] for key in sorted(archives)]


def _prepare_accept_transaction(
    paths: ProjectPaths,
    *,
    context: Mapping[str, object],
    protection: Mapping[str, object],
) -> tuple[dict[str, object], Path]:
    payload = context["payload"]
    workspace = context["workspace"]
    documents = context["documents"]
    assert isinstance(payload, Mapping) and isinstance(workspace, Path) and isinstance(documents, Mapping)
    requirement_id = str(payload["requirement_id"])
    change_id = str(payload["change_id"])
    requirement_root = workspace.parent.parent
    target_version = _target_version(documents)
    transaction_id = "CHANGE-" + canonical_sha256(
        {
            "requirement_id": requirement_id,
            "change_id": change_id,
            "package_identity_sha256": payload["package_identity_sha256"],
            "protection_sha256": protection["protection_sha256"],
            "target_version": target_version,
        }
    )
    active, _completed, staging_root = _accept_roots(paths)
    journal = active / f"{transaction_id}.json"
    staging = staging_root / transaction_id
    if journal.exists() or staging.exists():
        raise SdlcError(f"正式变更事务发生身份冲突：{transaction_id}。", exit_code=1)
    staging.mkdir(mode=0o700)

    try:
        package = documents["change-package.v1.json"]
        assert isinstance(package, Mapping)
        files: list[tuple[str, bytes, str]] = []
        projected_names = {
            "requirement": "projected-requirement.v2.json",
            "design": "projected-design.v2.json",
            "test_matrix": "projected-test-matrix.v2.json",
            "reference_index": "projected-reference-index.v2.json",
            "task_plan": "projected-task-plan.v2.json",
        }
        contents: dict[str, dict[str, object]] = {}
        for key, filename in projected_names.items():
            projected = documents[filename]
            assert isinstance(projected, Mapping) and isinstance(projected.get("content"), Mapping)
            contents[key] = deepcopy(dict(projected["content"]))
        formal_reference, reference_archives = _formalize_change_reference_paths(
            paths,
            requirement_root=requirement_root,
            workspace=workspace,
            reference_index=contents["reference_index"],
        )
        contents["reference_index"] = formal_reference
        files.extend(reference_archives)

        for key, filename in projected_names.items():
            projected = documents[filename]
            assert isinstance(projected, Mapping) and isinstance(projected.get("content"), Mapping)
            content = contents[key]
            version_name = "test-matrix" if key == "test_matrix" else key.replace("_", "-")
            files.append(
                (
                    (requirement_root / f"versions/{version_name}.v{target_version}.json").relative_to(paths.root).as_posix(),
                    _json_bytes(content),
                    f"after_version_{key}",
                )
            )
        for key, filename in (
            ("requirement", "projected-requirement.v2.json"),
            ("design", "projected-design.v2.json"),
            ("test_matrix", "projected-test-matrix.v2.json"),
        ):
            content = deepcopy(contents[key])
            content["is_current"] = True
            current_name = "test-matrix" if key == "test_matrix" else key
            files.append(
                (
                    (requirement_root / f"effective/{current_name}.current.json").relative_to(paths.root).as_posix(),
                    _json_bytes(content),
                    f"after_effective_{key}",
                )
            )
        files.extend(
            [
                (
                    (requirement_root / "reference-index.v1.json").relative_to(paths.root).as_posix(),
                    _json_bytes(contents["reference_index"]),
                    "after_reference_index",
                ),
                (
                    (requirement_root / "tasks/task-plan.v2.json").relative_to(paths.root).as_posix(),
                    _json_bytes(documents["projected-task-plan.v2.json"]["content"]),  # type: ignore[index]
                    "after_task_plan",
                ),
            ]
        )
        impact_result = {
            "schema_version": "change-task-impact-result.v1",
            "requirement_id": requirement_id,
            "change_id": change_id,
            "transaction_id": transaction_id,
            "target_version": target_version,
            "task_impacts": deepcopy(package.get("task_impacts", {})),
        }
        files.append(
            (
                (workspace / "task-impact-result.v1.json").relative_to(paths.root).as_posix(),
                _json_bytes(impact_result),
                "after_status",
            )
        )
        records: list[dict[str, object]] = []
        for index, (relative, content, stage) in enumerate(files):
            target = _controlled_transaction_path(paths, relative)
            if stage.startswith("after_version_") and (target.exists() or target.is_symlink()):
                raise SdlcError(f"目标正式版本已经存在，拒绝覆盖：{relative}。", exit_code=1)
            old = target.read_bytes() if target.is_file() and not target.is_symlink() else None
            new_name = f"new-{index:03d}.bin"
            old_name = f"old-{index:03d}.bin" if old is not None else None
            _atomic_write_bytes(staging / new_name, content)
            if old is not None and old_name is not None:
                _atomic_write_bytes(staging / old_name, old)
            records.append(
                {
                    "path": relative,
                    "stage": stage,
                    "new_file": new_name,
                    "new_sha256": sha256_bytes(content),
                    "old_file": old_name,
                    "old_sha256": sha256_bytes(old) if old is not None else None,
                }
            )
        transaction = {
            "schema_version": _ACCEPT_TRANSACTION_SCHEMA,
            "transaction_id": transaction_id,
            "requirement_id": requirement_id,
            "change_id": change_id,
            "target_version": target_version,
            "requirement_root": requirement_root.relative_to(paths.root).as_posix(),
            "workspace_path": workspace.relative_to(paths.root).as_posix(),
            "package_identity_sha256": payload["package_identity_sha256"],
            "protection_sha256": protection["protection_sha256"],
            "event_start_size": paths.events_file.stat().st_size,
            "event_prefix_sha256": sha256_bytes(paths.events_file.read_bytes()),
            "events": _build_accept_events(
                paths,
                context=context,
                protection=protection,
                transaction_id=transaction_id,
                target_version=target_version,
            ),
            "files": records,
            "staging_path": staging.relative_to(paths.root).as_posix(),
        }
        _atomic_write_bytes(journal, _json_bytes(transaction))
        return transaction, journal
    except BaseException:
        # 活动日志是可恢复事务的唯一归属证据；日志尚未落盘时，准备失败必须清空本次暂存。
        if not journal.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def accept_change_package(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    change_id: str,
    interruption_hook: InterruptionHook | None = None,
) -> dict[str, object]:
    """在单一可恢复事务中提交五类版本、有效指针、审核和任务影响。"""

    require_matching_sdlc_identity(paths)
    hook = interruption_hook or (lambda _stage: None)
    with project_lock(paths):
        with event_write_lock(paths):
            _recover_change_accept_transactions_locked(paths)
            existing = _find_completed_change_receipt(
                paths,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            if existing is not None:
                return _receipt_result(existing, idempotent=True)
            context = load_change_package_context_locked(
                paths,
                requirement_id=requirement_id,
                change_id=change_id,
            )
            payload = context["payload"]
            documents = context["documents"]
            assert isinstance(payload, Mapping) and isinstance(documents, Mapping)
            package = documents["change-package.v1.json"]
            assert isinstance(package, Mapping)
            for name, base in package["base_versions"].items():  # type: ignore[index,union-attr]
                if not isinstance(base, Mapping):
                    raise SdlcError("变更包基础版本记录不完整。", exit_code=1)
                target = _controlled_transaction_path(paths, base.get("path"))
                if target.is_symlink() or not target.is_file() or sha256_file(target) != base.get("sha256"):
                    raise SdlcError(f"基础版本 {name} 已经漂移，不能生效变更。", exit_code=1)
            protection = _validate_change_protection(paths, context)
            transaction, journal = _prepare_accept_transaction(
                paths,
                context=context,
                protection=protection,
            )
            records = _transaction_file_records(paths, transaction)
            versions = [
                item
                for item in records
                if "/versions/" in f"/{str(item['path'])}"
            ]
            pointers = [item for item in records if item not in versions]
            try:
                _publish_transaction_files(versions, hook)
                with paths.events_file.open("ab") as handle:
                    handle.write(_event_bytes(transaction["events"]))
                    handle.flush()
                    os.fsync(handle.fileno())
                hook("after_change_event_append")
                _publish_transaction_files(pointers, hook)
                return _finish_accept_transaction_locked(paths, transaction, journal)
            except Exception:
                _rollback_accept_transaction(paths, transaction, journal)
                raise


__all__ = [
    "ChangePackageResult",
    "accept_change_package",
    "add_change_material",
    "change_accept_environment_interruption_hook",
    "change_review_context_locked",
    "create_change_workspace",
    "inspect_change_accept_transactions",
    "load_change_package_context_locked",
    "protect_change_package",
    "recover_change_accept_transactions",
    "require_no_unrecovered_change_accept_transaction",
    "submit_change_package",
]
