from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.git_tools import find_git_root, run_git
from codex_sdlc.core.project import (
    ProjectPaths,
    requirement_dir_for_id,
    resolve_project_path,
)
from codex_sdlc.core.state import (
    append_event,
    derive_state,
    load_events,
    next_event_id,
    next_number,
    now_iso,
    resolve_task,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)
from codex_sdlc.core.task_read_manifest import (
    build_task_read_manifest,
    load_task_read_manifest,
)
from codex_sdlc.core.task_outputs import (
    bind_predecessor_outputs,
    formal_index_bytes,
    formal_task_output_index_path,
    index_completed_task,
    load_formal_task_output_index,
    remove_completed_task,
    replace_formal_task_output_index,
    validate_formal_task_output_index,
)


TASK_RUN_SCHEMA = "task-run.v1"
CURRENT_SCHEMA = "task-run-current.v1"
START_TRANSACTION_SCHEMA = "task-run-start-transaction.v1"
READ_CONFIRM_TRANSACTION_SCHEMA = "task-read-confirm-transaction.v1"
RESTORE_TRANSACTION_SCHEMA = "task-run-restore-transaction.v1"
RESTORE_RECORD_SCHEMA = "task-run-restore.v1"


def _before_current_commit(_journal: dict[str, object]) -> None:
    """测试可在这里模拟多文件已经准备好、当前指针尚未提交时的中断。"""


def _before_confirmation_current_commit(_journal: dict[str, object]) -> None:
    """测试可在运行合同已激活、当前指针尚未更新时模拟进程中断。"""


def _before_run_status_current_commit(_run: dict[str, object]) -> None:
    """测试可在轮次状态已写入、当前指针尚未同步时模拟中断。"""


def _before_change_pause_event(_task_id: str) -> None:
    """测试可在运行轮次已失效、任务暂停事件尚未写入时模拟中断。"""


def _before_evidence_current_commit(_run: dict[str, object]) -> None:
    """测试可在证据已经写入轮次、当前指针尚未同步时模拟中断。"""


def _before_completion_current_commit(_run: dict[str, object]) -> None:
    """允许隔离验收在最后一次指针提交前制造可复现中断。"""

    # 完成事务跨事件、轮次和当前指针三份文件；没有稳定故障点就只能靠时序碰撞，
    # 无法证明中断不会留下“任务完成但轮次仍活动”的半成品。
    if os.environ.get("CODEX_SDLC_TASK_DONE_INTERRUPT_AT", "").strip() == "before_current_commit":
        raise OSError("故障注入：任务完成当前指针提交前中断")


def _before_task_output_index_commit(_document: dict[str, object]) -> None:
    """让合同测试能在正式索引替换前确认整个完成事务会回滚。"""

    if os.environ.get("CODEX_SDLC_TASK_OUTPUT_INTERRUPT_AT", "").strip() == "before_index":
        raise OSError("故障注入：正式任务交付物索引替换前中断")


def _after_task_output_index_commit(_document: dict[str, object]) -> None:
    """让合同测试能在索引已替换后确认事件、轮次和索引仍会一起回滚。"""

    if os.environ.get("CODEX_SDLC_TASK_OUTPUT_INTERRUPT_AT", "").strip() == "after_index":
        raise OSError("故障注入：正式任务交付物索引替换后中断")


def _before_restore_output_index_commit(_document: dict[str, object]) -> None:
    """恢复事务在移除旧任务项前提供稳定的故障位置。"""

    if os.environ.get("CODEX_SDLC_TASK_OUTPUT_INTERRUPT_AT", "").strip() == "before_restore_index":
        raise OSError("故障注入：恢复事务替换任务交付物索引前中断")


def _after_restore_output_index_commit(_document: dict[str, object]) -> None:
    """恢复事务在旧任务项已移除后提供稳定的故障位置。"""

    if os.environ.get("CODEX_SDLC_TASK_OUTPUT_INTERRUPT_AT", "").strip() == "after_restore_index":
        raise OSError("故障注入：恢复事务替换任务交付物索引后中断")


def _after_restore_directory_prepare(_journal: dict[str, object]) -> None:
    """测试可在新轮次目录准备好、恢复事件尚未提交时模拟中断。"""

    if os.environ.get("CODEX_SDLC_TASK_RESTORE_INTERRUPT_AT", "").strip() == "directory":
        raise KeyboardInterrupt("故障注入：恢复目录准备后中断")


def _after_restore_event_append(_journal: dict[str, object]) -> None:
    """测试可在恢复事件已经提交、新轮次指针尚未切换时模拟中断。"""

    if os.environ.get("CODEX_SDLC_TASK_RESTORE_INTERRUPT_AT", "").strip() == "event":
        raise KeyboardInterrupt("故障注入：恢复事件追加后中断")


def _before_restore_current_commit(_journal: dict[str, object]) -> None:
    """测试可在新轮次已经就位、当前指针尚未切换时模拟中断。"""

    if os.environ.get("CODEX_SDLC_TASK_RESTORE_INTERRUPT_AT", "").strip() == "current":
        raise KeyboardInterrupt("故障注入：恢复当前指针提交前中断")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"任务运行文件包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"任务运行文件包含非标准数字：{value}。", exit_code=1)


def _strict_json(path: Path, label: str) -> dict[str, object]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}无法解析。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是对象。", exit_code=1)
    return document


def _atomic_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json_text(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _restore_bytes(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".rollback", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _build_task_read_manifest(
    paths: ProjectPaths,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    *,
    run_number: int,
    generated_at: str,
    all_tasks: Mapping[str, Mapping[str, object]],
    index_document: Mapping[str, object] | None = None,
    allowed_changed_paths: object = None,
) -> tuple[dict[str, object], dict[str, str]]:
    """先沿用既有引用清单，再用唯一正式索引替换前置交付物来源。"""

    manifest, upstream_hashes = build_task_read_manifest(
        paths,
        requirement,
        task,
        run_number=run_number,
        generated_at=generated_at,
        all_tasks=all_tasks,
    )
    return bind_predecessor_outputs(
        paths,
        requirement,
        task,
        manifest,
        upstream_hashes,
        index_document=index_document,
        allowed_changed_paths=allowed_changed_paths,
    )


def _all_tasks(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    result: dict[str, Mapping[str, object]] = {}
    duplicates: set[str] = set()
    requirements = state.get("requirements")
    if not isinstance(requirements, Mapping):
        return result
    for requirement in requirements.values():
        if not isinstance(requirement, Mapping):
            continue
        for task in requirement.get("tasks", []):
            if not isinstance(task, Mapping):
                continue
            task_id = str(task.get("task_id") or "")
            if task_id in result:
                duplicates.add(task_id)
            else:
                result[task_id] = task
    for task_id in duplicates:
        result.pop(task_id, None)
    return result


def _passed_task_review(requirement: Mapping[str, object]) -> Mapping[str, object]:
    state = requirement.get("task_plan_review_state")
    if not isinstance(state, Mapping) or state.get("can_advance") is not True:
        raise SdlcError("整套任务审核没有当前有效的 passed 结果，不能开工。", exit_code=1)
    reviews = [
        item
        for item in state.get("reviews", [])
        if isinstance(item, Mapping)
        and item.get("is_current") is True
        and item.get("effective_status") == "passed"
    ]
    if len(reviews) != 1:
        raise SdlcError("整套任务审核的当前 passed 身份不唯一，不能开工。", exit_code=1)
    return reviews[0]


def _project_rules_hash(paths: ProjectPaths) -> str:
    records: list[dict[str, str]] = []
    for relative in (Path("AGENTS.md"), Path(".codex-sdlc/project.md")):
        target = paths.root / relative
        if target.is_file() and not target.is_symlink():
            records.append({"path": relative.as_posix(), "sha256": sha256_file(target)})
    return canonical_sha256(records)


def _normalize_allowed_output_paths(
    paths: ProjectPaths, raw_paths: object
) -> list[str]:
    if not isinstance(raw_paths, list):
        raise SdlcError("任务允许输出路径必须是数组。", exit_code=1)
    normalized: list[str] = []
    for raw_path in raw_paths:
        relative = str(raw_path).strip()
        if not relative:
            continue
        # 路径即使尚未创建也要先证明落在项目目录内，避免 Git 排除规则把项目外变化隐藏掉。
        resolve_project_path(paths.root, relative, must_exist=False)
        normalized.append(Path(relative).as_posix())
    return list(dict.fromkeys(normalized))


def _git_scope_payload(paths: ProjectPaths, allowed_output_paths: list[str]) -> bytes:
    """只摘要任务输出范围之外的工作树，保留开工前已有脏改动作为基线。"""

    # Git pathspec 默认会解释 *、? 和 []；允许输出是 task.v2 的字面项目路径，
    # 必须同时使用 exclude 与 literal，不能让一个特殊字符放大任务可修改范围。
    pathspecs = [
        ".",
        *[f":(exclude,literal){item}" for item in allowed_output_paths],
    ]
    status = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs],
        paths.root,
    )
    diff = run_git(["diff", "--binary", "HEAD", "--", *pathspecs], paths.root)
    untracked = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z", "--", *pathspecs],
        paths.root,
    )
    if status.returncode != 0 or diff.returncode != 0 or untracked.returncode != 0:
        raise SdlcError("无法读取当前 Git 工作树差异，不能校验任务范围。", exit_code=1)
    untracked_records: list[dict[str, str]] = []
    for relative in sorted(item for item in untracked.stdout.split("\0") if item):
        target = paths.root / relative
        if target.is_symlink() or not target.is_file():
            raise SdlcError(f"未跟踪工作树路径不是普通文件：{relative}。", exit_code=1)
        untracked_records.append({"path": relative, "sha256": sha256_file(target)})
    return (
        status.stdout.encode("utf-8")
        + b"\0"
        + diff.stdout.encode("utf-8")
        + b"\0"
        + canonical_json_text(untracked_records).encode("utf-8")
    )


def _code_baseline(
    paths: ProjectPaths, allowed_output_paths: list[str]
) -> dict[str, str]:
    from codex_sdlc.core.backup import current_git_identity

    identity = current_git_identity(paths.root)
    git_root = find_git_root(paths.root)
    if git_root is None:
        return {
            "project_path": str(paths.root.resolve()),
            "repo_key": str(identity["repo_key"]),
            "branch_key": str(identity["branch_key"]),
            "worktree_key": str(identity["worktree_key"]),
            "git_head": "",
            "worktree_diff_sha256": canonical_sha256([]),
        }
    head = run_git(["rev-parse", "HEAD"], git_root)
    if head.returncode != 0:
        raise SdlcError("无法读取当前 Git 工作树身份，不能开工。", exit_code=1)
    return {
        "project_path": str(paths.root.resolve()),
        "repo_key": str(identity["repo_key"]),
        "branch_key": str(identity["branch_key"]),
        "worktree_key": str(identity["worktree_key"]),
        "git_head": head.stdout.strip(),
        "worktree_diff_sha256": sha256_bytes(
            _git_scope_payload(paths, allowed_output_paths)
        ),
    }


def _require_run_worktree_identity(
    paths: ProjectPaths, run: Mapping[str, object]
) -> None:
    from codex_sdlc.core.backup import current_git_identity

    baseline = run.get("code_baseline")
    if not isinstance(baseline, Mapping):
        raise SdlcError("任务运行轮次缺少开工工作树身份。", exit_code=1)
    current = current_git_identity(paths.root)
    expected = {
        "project_path": str(paths.root.resolve()),
        "repo_key": str(current["repo_key"]),
        "branch_key": str(current["branch_key"]),
        "worktree_key": str(current["worktree_key"]),
    }
    changed = [field for field, value in expected.items() if baseline.get(field) != value]
    if changed:
        raise SdlcError(
            "当前项目或工作树身份与任务开工轮次不一致，不能确认读取。",
            exit_code=1,
        )


def _runtime_paths(
    paths: ProjectPaths, requirement_id: str, task_id: str
) -> tuple[Path, Path, Path]:
    requirement_root = requirement_dir_for_id(paths, requirement_id)
    if requirement_root is None:
        raise SdlcError(f"找不到正式需求目录：{requirement_id}。", exit_code=1)
    task_runtime = requirement_root / "runtime" / task_id
    return requirement_root, task_runtime, task_runtime / "current.json"


def _next_run_number(task_runtime: Path) -> int:
    numbers: list[int] = []
    runs_root = task_runtime / "runs"
    if runs_root.exists():
        for item in runs_root.iterdir():
            if item.is_dir() and item.name.isdigit():
                numbers.append(int(item.name))
    return (max(numbers) if numbers else 0) + 1


def _current_document(
    *,
    requirement_id: str,
    task_id: str,
    run_number: int,
    status: str,
    task_run_sha256: str,
    manifest_sha256: str,
    updated_at: str,
) -> dict[str, object]:
    return {
        "schema_version": CURRENT_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": run_number,
        "run_path": f"runs/{run_number:04d}/task-run.v1.json",
        "manifest_path": f"runs/{run_number:04d}/task-read-manifest.v1.json",
        "status": status,
        "task_run_sha256": task_run_sha256,
        "read_manifest_sha256": manifest_sha256,
        "updated_at": updated_at,
    }


def load_task_run_context(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
) -> dict[str, object]:
    """读取当前轮次及指针，并确认两份文件确实描述同一份内容。"""

    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    if not current_path.is_file() or current_path.is_symlink():
        raise SdlcError("当前任务没有可读取的运行轮次。", exit_code=1)
    current = _strict_json(current_path, "任务当前运行指针")
    run_number = _validate_current(current, requirement_id, task_id)
    run_path = task_runtime / "runs" / f"{run_number:04d}" / "task-run.v1.json"
    if not run_path.is_file() or run_path.is_symlink():
        raise SdlcError("当前任务运行轮次文件不完整。", exit_code=1)
    run = _strict_json(run_path, "任务运行轮次")
    validate_schema_document(run, schema_name=TASK_RUN_SCHEMA)
    if current.get("task_run_sha256") != sha256_file(run_path):
        raise SdlcError("当前任务运行轮次和当前指针的哈希不一致。", exit_code=1)
    return {
        "task_runtime": task_runtime,
        "current_path": current_path,
        "run_path": run_path,
        "current": current,
        "run": run,
    }


def _event_backup_snapshot(paths: ProjectPaths) -> tuple[bytes, set[Path]]:
    return (
        paths.events_file.read_bytes() if paths.events_file.exists() else b"",
        set(paths.backups_dir.glob("events-*.jsonl.bak")),
    )


def _rollback_event_snapshot(
    paths: ProjectPaths,
    content: bytes,
    backups_before: set[Path],
) -> None:
    _restore_bytes(paths.events_file, content)
    for backup in set(paths.backups_dir.glob("events-*.jsonl.bak")) - backups_before:
        backup.unlink(missing_ok=True)


def record_task_run_entry(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    field: str,
    record: Mapping[str, object],
    status: str = "active",
    event: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """把一条证据和可选正式事件作为一个可回滚写入保存。"""

    if field not in {"test_records", "feedback_records", "verification_records"}:
        raise SdlcError(f"任务运行轮次不支持写入证据字段：{field}。", exit_code=1)
    if status not in {"active", "stale"}:
        raise SdlcError(f"证据登记不能把运行轮次改为 {status}。", exit_code=1)
    require_active_task_run(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    context = load_task_run_context(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    run_path = context["run_path"]
    current_path = context["current_path"]
    run = context["run"]
    current = context["current"]
    assert isinstance(run_path, Path)
    assert isinstance(current_path, Path)
    assert isinstance(run, Mapping)
    assert isinstance(current, Mapping)

    entries = run.get(field)
    if not isinstance(entries, list):
        raise SdlcError(f"任务运行轮次的 {field} 不是数组。", exit_code=1)
    evidence_id = str(record.get("evidence_id") or record.get("feedback_id") or "")
    for existing in entries:
        if not isinstance(existing, Mapping):
            continue
        existing_id = str(
            existing.get("evidence_id") or existing.get("feedback_id") or ""
        )
        if evidence_id and existing_id == evidence_id:
            raise SdlcError(f"当前轮次已经存在同编号证据：{evidence_id}。", exit_code=1)

    updated_run = deepcopy(dict(run))
    updated_run[field] = [*deepcopy(entries), deepcopy(dict(record))]
    updated_run["status"] = status
    validate_schema_document(updated_run, schema_name=TASK_RUN_SCHEMA)
    updated_run_sha256 = sha256_bytes(
        canonical_json_text(updated_run).encode("utf-8")
    )
    updated_current = _current_document(
        requirement_id=requirement_id,
        task_id=task_id,
        run_number=int(updated_run["run_number"]),
        status=status,
        task_run_sha256=updated_run_sha256,
        manifest_sha256=str(updated_run["read_manifest_sha256"]),
        updated_at=now_iso(),
    )
    run_before = run_path.read_bytes()
    current_before = current_path.read_bytes()
    events_before, event_backups_before = _event_backup_snapshot(paths)
    try:
        if event is not None:
            append_event(
                paths,
                event_type=str(event["event_type"]),
                source=str(event["source"]),
                summary=str(event["summary"]),
                requirement_id=requirement_id,
                task_id=task_id,
                payload=deepcopy(dict(event["payload"])),  # type: ignore[arg-type]
            )
        _atomic_write(run_path, updated_run)
        _before_evidence_current_commit(updated_run)
        _atomic_write(current_path, updated_current)
    except Exception as exc:
        try:
            _restore_bytes(run_path, run_before)
            _restore_bytes(current_path, current_before)
            _rollback_event_snapshot(paths, events_before, event_backups_before)
        except OSError:
            pass
        if isinstance(exc, SdlcError):
            raise
        raise SdlcError(f"任务证据写入失败：{exc}。", exit_code=1) from exc
    return updated_run


def recover_task_start_transaction(
    paths: ProjectPaths, requirement_id: str, task_id: str
) -> None:
    """再次进入命令时收好上次中断点，事件存在就补齐，否则删除未提交暂存。"""

    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    journal_path = task_runtime / ".start-transaction.json"
    if not journal_path.exists():
        return
    journal = _strict_json(journal_path, "任务开工事务记录")
    staging = task_runtime / str(journal.get("staging_name") or "")
    final = task_runtime / str(journal.get("final_name") or "")
    event_id = str(journal.get("event_id") or "")
    event_exists = any(str(event.get("event_id") or "") == event_id for event in load_events(paths))
    if event_exists:
        if not final.exists() and staging.is_dir():
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(staging, final)
        if not final.is_dir():
            raise SdlcError("开工事件已经存在，但运行轮次无法恢复。", exit_code=1)
        current = journal.get("current")
        if not isinstance(current, Mapping):
            raise SdlcError("任务开工事务缺少当前指针。", exit_code=1)
        _atomic_write(current_path, dict(current))
    else:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
    journal_path.unlink(missing_ok=True)


def initialize_task_run(
    paths: ProjectPaths,
    state: Mapping[str, object],
    requirement: Mapping[str, object],
    task: Mapping[str, object],
) -> dict[str, object]:
    requirement_id = str(requirement.get("requirement_id") or "")
    task_id = str(task.get("task_id") or "")
    recover_task_start_transaction(paths, requirement_id, task_id)
    status = str(task.get("status") or "")
    if status not in {"todo", "test_failed"}:
        raise SdlcError(f"{requirement_id} / {task_id} 当前状态不能创建新运行轮次：{status}。", exit_code=1)
    if requirement.get("status") in {"planning_tasks", "done", "accepted"}:
        raise SdlcError("当前需求状态不允许直接开工。", exit_code=1)
    if task.get("blocking_conditions") or task.get("formal_gate") is False:
        raise SdlcError("当前任务仍有阻塞条件，不能开工。", exit_code=1)
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SdlcError("缺少 CODEX_THREAD_ID，不能建立可确认的任务运行轮次。", exit_code=1)
    review = _passed_task_review(requirement)
    all_tasks = _all_tasks(state)
    for dependency_id in task.get("depends_on", []):
        dependency = all_tasks.get(str(dependency_id))
        if dependency is None:
            raise SdlcError(f"前置任务不存在或编号不唯一：{dependency_id}。", exit_code=1)
        if dependency.get("status") not in {"done", "closed"}:
            raise SdlcError(f"{task_id} 依赖 {dependency_id}，请先完成前置任务。", exit_code=1)

    requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    if current_path.exists():
        current = _strict_json(current_path, "任务当前运行指针")
        if current.get("status") in {"reading", "active"}:
            raise SdlcError("当前任务已经存在未关闭的运行轮次。", exit_code=1)
    run_number = _next_run_number(task_runtime)
    timestamp = now_iso()
    manifest, upstream_hashes = _build_task_read_manifest(
        paths,
        requirement,
        task,
        run_number=run_number,
        generated_at=timestamp,
        all_tasks=all_tasks,
    )
    upstream_hashes["project_rules"] = _project_rules_hash(paths)
    task_json_path = requirement_root / "tasks" / f"{task_id}.json"
    manifest_bytes = canonical_json_text(manifest).encode("utf-8")
    manifest_sha256 = sha256_bytes(manifest_bytes)
    # 正式 CLI 会在调用任务命令前执行同一身份门禁；核心入口也在真正写运行文件前复核，
    # 让合同测试和其它受管调用方不能绕过工作树绑定。
    from codex_sdlc.core.backup import require_matching_sdlc_identity

    require_matching_sdlc_identity(paths)
    allowed_output_paths = _normalize_allowed_output_paths(
        paths, list(task.get("changed_files", []))
    )
    task_run: dict[str, object] = {
        "schema_version": TASK_RUN_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": run_number,
        "runner_thread_id": thread_id,
        "status": "reading",
        "task_sha256": sha256_file(task_json_path),
        "task_review_sha256": canonical_sha256(review),
        "read_manifest_sha256": manifest_sha256,
        "upstream_hashes": upstream_hashes,
        "code_baseline": _code_baseline(paths, allowed_output_paths),
        "allowed_output_paths": allowed_output_paths,
        "test_records": [],
        "feedback_records": [],
        "verification_records": [],
        "started_at": timestamp,
        "read_confirmation": None,
    }
    validate_schema_document(task_run, schema_name=TASK_RUN_SCHEMA)

    staging_name = f".staging-{run_number:04d}"
    final_name = f"runs/{run_number:04d}"
    staging = task_runtime / staging_name
    final = task_runtime / final_name
    journal_path = task_runtime / ".start-transaction.json"
    current_before = current_path.read_bytes() if current_path.exists() else None
    events_before = paths.events_file.read_bytes()
    event_backups_before = set(paths.backups_dir.glob("events-*.jsonl.bak"))
    event_id = next_event_id(load_events(paths))
    try:
        if staging.exists() or final.exists():
            raise SdlcError("待分配运行号已经被占用，不能覆盖现有轮次。", exit_code=1)
        staging.mkdir(parents=True)
        _atomic_write(staging / "task-read-manifest.v1.json", manifest)
        _atomic_write(staging / "task-run.v1.json", task_run)
        run_sha256 = sha256_file(staging / "task-run.v1.json")
        current = _current_document(
            requirement_id=requirement_id,
            task_id=task_id,
            run_number=run_number,
            status="reading",
            task_run_sha256=run_sha256,
            manifest_sha256=manifest_sha256,
            updated_at=timestamp,
        )
        journal = {
            "schema_version": START_TRANSACTION_SCHEMA,
            "requirement_id": requirement_id,
            "task_id": task_id,
            "run_number": run_number,
            "staging_name": staging_name,
            "final_name": final_name,
            "event_id": event_id,
            "current": current,
        }
        _atomic_write(journal_path, journal)
        event = append_event(
            paths,
            event_type="task_updated",
            source="sdlc-task",
            summary=f"启动任务 {task_id} 的第 {run_number} 次运行",
            requirement_id=requirement_id,
            task_id=task_id,
            payload={
                "status": "doing",
                "task_run_number": run_number,
                "task_run_status": "reading",
                "read_manifest_sha256": manifest_sha256,
            },
        )
        if event.get("event_id") != event_id:
            raise OSError("开工事件编号与事务记录不一致")
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, final)
        _before_current_commit(journal)
        _atomic_write(current_path, current)
        journal_path.unlink(missing_ok=True)
    except Exception as exc:
        try:
            _restore_bytes(paths.events_file, events_before)
            for backup in set(paths.backups_dir.glob("events-*.jsonl.bak")) - event_backups_before:
                backup.unlink(missing_ok=True)
            _restore_bytes(current_path, current_before)
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            journal_path.unlink(missing_ok=True)
            runs_root = task_runtime / "runs"
            if runs_root.exists() and not any(runs_root.iterdir()):
                runs_root.rmdir()
            if task_runtime.exists() and not any(task_runtime.iterdir()):
                task_runtime.rmdir()
                if task_runtime.parent.exists() and not any(task_runtime.parent.iterdir()):
                    task_runtime.parent.rmdir()
        except OSError:
            pass
        if isinstance(exc, SdlcError):
            raise
        raise SdlcError(f"任务开工事务失败：{exc}。", exit_code=1) from exc
    return {"run": task_run, "manifest": manifest, "current": current}


def _validate_current(
    current: Mapping[str, object], requirement_id: str, task_id: str
) -> int:
    if (
        current.get("schema_version") != CURRENT_SCHEMA
        or current.get("requirement_id") != requirement_id
        or current.get("task_id") != task_id
        or not isinstance(current.get("run_number"), int)
        or int(current["run_number"]) < 1
    ):
        raise SdlcError("任务当前运行指针身份不完整或与命令目标不符。", exit_code=1)
    return int(current["run_number"])


def _read_confirm_journal_path(task_runtime: Path) -> Path:
    return task_runtime / ".read-confirm-transaction.json"


def _validate_read_confirm_journal(
    journal: Mapping[str, object],
    *,
    requirement_id: str,
    task_id: str,
    thread_id: str,
) -> tuple[int, dict[str, object], dict[str, object]]:
    if (
        journal.get("schema_version") != READ_CONFIRM_TRANSACTION_SCHEMA
        or journal.get("requirement_id") != requirement_id
        or journal.get("task_id") != task_id
        or journal.get("thread_id") != thread_id
        or not isinstance(journal.get("run_number"), int)
        or int(journal["run_number"]) < 1
    ):
        raise SdlcError("读取确认事务身份不完整或与当前任务线程不符。", exit_code=1)
    updated_run = journal.get("updated_run")
    updated_current = journal.get("updated_current")
    if not isinstance(updated_run, dict) or not isinstance(updated_current, dict):
        raise SdlcError("读取确认事务缺少完整的目标运行文件。", exit_code=1)
    validate_schema_document(updated_run, schema_name=TASK_RUN_SCHEMA)
    run_number = int(journal["run_number"])
    _validate_current(updated_current, requirement_id, task_id)
    confirmation = updated_run.get("read_confirmation")
    if (
        updated_run.get("requirement_id") != requirement_id
        or updated_run.get("task_id") != task_id
        or updated_run.get("run_number") != run_number
        or updated_run.get("status") != "active"
        or updated_current.get("run_number") != run_number
        or updated_current.get("status") != "active"
        or not isinstance(confirmation, Mapping)
        or confirmation.get("thread_id") != thread_id
        or confirmation.get("manifest_sha256") != journal.get("manifest_sha256")
    ):
        raise SdlcError("读取确认事务的目标状态或确认身份不一致。", exit_code=1)
    updated_run_sha256 = sha256_bytes(
        canonical_json_text(updated_run).encode("utf-8")
    )
    updated_current_sha256 = sha256_bytes(
        canonical_json_text(updated_current).encode("utf-8")
    )
    if (
        journal.get("updated_run_sha256") != updated_run_sha256
        or journal.get("updated_current_sha256") != updated_current_sha256
        or updated_current.get("task_run_sha256") != updated_run_sha256
    ):
        raise SdlcError("读取确认事务的目标文件哈希不一致。", exit_code=1)
    return run_number, updated_run, updated_current


def recover_task_read_confirmation(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    thread_id: str,
    manifest_sha256: str,
) -> bool:
    """确认写到一半时按持久事务补齐两个文件，重复中断仍可再次进入恢复。"""

    requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    journal_path = _read_confirm_journal_path(task_runtime)
    if not journal_path.exists():
        return False
    if journal_path.is_symlink() or not journal_path.is_file():
        raise SdlcError("读取确认事务记录不是普通文件。", exit_code=1)
    journal = _strict_json(journal_path, "读取确认事务记录")
    run_number, updated_run, updated_current = _validate_read_confirm_journal(
        journal,
        requirement_id=requirement_id,
        task_id=task_id,
        thread_id=thread_id,
    )
    # 恢复也是当前确认命令的一部分，必须先绑定调用方提交的清单哈希。
    # 若先补写 current，错误哈希命令会在报错前把任务实际激活。
    if journal.get("manifest_sha256") != manifest_sha256:
        raise SdlcError("读取确认事务绑定的清单哈希与当前命令不一致。", exit_code=1)
    _require_run_worktree_identity(paths, updated_run)
    run_path = task_runtime / "runs" / f"{run_number:04d}" / "task-run.v1.json"
    manifest_path = (
        task_runtime / "runs" / f"{run_number:04d}" / "task-read-manifest.v1.json"
    )
    if any(path.is_symlink() or not path.is_file() for path in (run_path, current_path, manifest_path)):
        raise SdlcError("读取确认事务对应的运行文件不完整。", exit_code=1)
    expected_manifest_sha256 = str(journal.get("manifest_sha256") or "")
    if sha256_file(manifest_path) != expected_manifest_sha256:
        raise SdlcError("读取确认事务对应的清单已经变化，不能恢复。", exit_code=1)
    run_hash = sha256_file(run_path)
    current_hash = sha256_file(current_path)
    allowed_run_hashes = {
        str(journal.get("run_before_sha256") or ""),
        str(journal.get("updated_run_sha256") or ""),
    }
    allowed_current_hashes = {
        str(journal.get("current_before_sha256") or ""),
        str(journal.get("updated_current_sha256") or ""),
    }
    if run_hash not in allowed_run_hashes or current_hash not in allowed_current_hashes:
        raise SdlcError("读取确认事务外的运行文件发生变化，不能自动恢复。", exit_code=1)
    try:
        _atomic_write(run_path, updated_run)
        _before_confirmation_current_commit(dict(journal))
        _atomic_write(current_path, updated_current)
        journal_path.unlink(missing_ok=True)
    except OSError as exc:
        raise SdlcError(f"读取确认事务恢复失败：{exc}。", exit_code=1) from exc
    # requirement_root 由受控需求目录解析得到，这里保留变量是为了明确恢复没有跨出需求目录。
    _ = requirement_root
    return True


def confirm_task_read(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    manifest_sha256: str,
) -> dict[str, object]:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SdlcError("缺少 CODEX_THREAD_ID，不能确认任务读取。", exit_code=1)
    # 格式不合法的输入同样不能触碰中断后留下的恢复事务。
    if len(manifest_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in manifest_sha256):
        raise SdlcError("读取清单 SHA-256 格式不正确。", exit_code=1)
    # task-read-confirm 必须复用 task 的项目身份门禁。先检查复制工作树，再碰任何恢复文件，
    # 避免复制出来的 reading 目录借同一线程标识写成另一份 active。
    from codex_sdlc.core.backup import require_matching_sdlc_identity

    require_matching_sdlc_identity(paths)
    recover_task_start_transaction(paths, requirement_id, task_id)
    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    recover_task_read_confirmation(
        paths,
        requirement_id=requirement_id,
        task_id=task_id,
        thread_id=thread_id,
        manifest_sha256=manifest_sha256,
    )
    if not current_path.is_file() or current_path.is_symlink():
        raise SdlcError("当前任务没有可确认的运行轮次。", exit_code=1)
    current = _strict_json(current_path, "任务当前运行指针")
    run_number = _validate_current(current, requirement_id, task_id)
    run_root = task_runtime / "runs" / f"{run_number:04d}"
    run_path = run_root / "task-run.v1.json"
    manifest_path = run_root / "task-read-manifest.v1.json"
    if any(path.is_symlink() or not path.is_file() for path in (run_path, manifest_path)):
        raise SdlcError("当前运行轮次文件不完整。", exit_code=1)
    run = _strict_json(run_path, "任务运行轮次")
    validate_schema_document(run, schema_name=TASK_RUN_SCHEMA)
    _require_run_worktree_identity(paths, run)
    manifest = load_task_read_manifest(manifest_path)
    actual_manifest_sha256 = sha256_file(manifest_path)
    if not (
        manifest.get("requirement_id") == requirement_id
        and manifest.get("task_id") == task_id
        and manifest.get("run_number") == run_number
        and run.get("requirement_id") == requirement_id
        and run.get("task_id") == task_id
        and run.get("run_number") == run_number
    ):
        raise SdlcError("当前轮次、任务和读取清单身份不一致。", exit_code=1)
    if (
        actual_manifest_sha256 != manifest_sha256
        or run.get("read_manifest_sha256") != manifest_sha256
        or current.get("read_manifest_sha256") != manifest_sha256
    ):
        raise SdlcError("读取清单已经变化或不是当前轮次的完整清单。", exit_code=1)
    if run.get("runner_thread_id") != thread_id:
        raise SdlcError("只有启动当前轮次的同一任务线程可以确认读取。", exit_code=1)
    if current.get("task_run_sha256") != sha256_file(run_path):
        raise SdlcError("当前指针记录的运行轮次哈希已经过期。", exit_code=1)
    if current.get("status") != "reading" or run.get("status") != "reading":
        confirmation = run.get("read_confirmation")
        if (
            current.get("status") == "active"
            and run.get("status") == "active"
            and isinstance(confirmation, Mapping)
            and confirmation.get("thread_id") == thread_id
            and confirmation.get("manifest_sha256") == manifest_sha256
        ):
            return {"run": run, "current": current, "idempotent": True}
        raise SdlcError("当前运行轮次不在 reading 状态，不能确认读取。", exit_code=1)

    timestamp = now_iso()
    updated_run = deepcopy(run)
    updated_run["status"] = "active"
    updated_run["read_confirmation"] = {
        "thread_id": thread_id,
        "manifest_sha256": manifest_sha256,
        "confirmed_at": timestamp,
    }
    validate_schema_document(updated_run, schema_name=TASK_RUN_SCHEMA)
    updated_run_sha256 = sha256_bytes(canonical_json_text(updated_run).encode("utf-8"))
    updated_current = _current_document(
        requirement_id=requirement_id,
        task_id=task_id,
        run_number=run_number,
        status="active",
        task_run_sha256=updated_run_sha256,
        manifest_sha256=manifest_sha256,
        updated_at=timestamp,
    )
    run_before_sha256 = sha256_file(run_path)
    current_before_sha256 = sha256_file(current_path)
    updated_current_sha256 = sha256_bytes(
        canonical_json_text(updated_current).encode("utf-8")
    )
    journal = {
        "schema_version": READ_CONFIRM_TRANSACTION_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": run_number,
        "thread_id": thread_id,
        "manifest_sha256": manifest_sha256,
        "run_before_sha256": run_before_sha256,
        "current_before_sha256": current_before_sha256,
        "updated_run_sha256": updated_run_sha256,
        "updated_current_sha256": updated_current_sha256,
        "updated_run": updated_run,
        "updated_current": updated_current,
    }
    journal_path = _read_confirm_journal_path(task_runtime)
    try:
        _atomic_write(journal_path, journal)
        _atomic_write(run_path, updated_run)
        _before_confirmation_current_commit(journal)
        _atomic_write(current_path, updated_current)
        journal_path.unlink(missing_ok=True)
    except OSError as exc:
        # 事务记录保留完整目标和前后哈希；下一次同线程进入正式命令时会继续恢复。
        raise SdlcError(f"任务读取确认写入失败，可用同一命令重试恢复：{exc}。", exit_code=1) from exc
    return {"run": updated_run, "current": updated_current, "idempotent": False}


def _current_task_review(requirement: Mapping[str, object]) -> Mapping[str, object] | None:
    review_state = requirement.get("task_plan_review_state")
    if not isinstance(review_state, Mapping):
        return None
    reviews = [
        item
        for item in review_state.get("reviews", [])
        if isinstance(item, Mapping) and item.get("is_current") is True
    ]
    return reviews[0] if len(reviews) == 1 else None


def _task_review_change_is_protected(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    review: Mapping[str, object],
) -> bool:
    """审核只因已登记的 unaffected 保护结果变化时，允许原活动轮次继续。"""

    changed_files = review.get("changed_files")
    if not isinstance(changed_files, list) or not changed_files:
        return False
    events = load_events(paths)
    if any(not isinstance(item, str) for item in changed_files):
        return False
    protection_paths = [
        item
        for item in changed_files
        if isinstance(item, str) and item.endswith("/change-protection.v1.json")
    ]
    if not protection_paths:
        return False
    allowed_changed_paths = set(protection_paths)
    from codex_sdlc.core.change_contract import COMMITTED_FILE_NAMES

    for relative_path in protection_paths:
        try:
            protection_path = resolve_project_path(paths.root, relative_path)
            protection = _strict_json(protection_path, "变更保护结果")
        except SdlcError:
            return False
        body = {
            key: deepcopy(value)
            for key, value in protection.items()
            if key != "protection_sha256"
        }
        if (
            protection.get("schema_version") != "change-protection.v1"
            or protection.get("requirement_id") != requirement_id
            or protection.get("protection_sha256") != canonical_sha256(body)
        ):
            return False
        unaffected = protection.get("unaffected_tasks")
        protected = protection.get("protected_tasks")
        if not isinstance(unaffected, list) or not isinstance(protected, list):
            return False
        if any(
            isinstance(item, Mapping) and item.get("task_id") == task_id
            for item in protected
        ):
            return False
        proofs = [
            item
            for item in unaffected
            if isinstance(item, Mapping) and item.get("task_id") == task_id
        ]
        if len(proofs) != 1:
            return False
        proof = proofs[0]
        requirement_root = requirement_dir_for_id(paths, requirement_id)
        if requirement_root is None:
            return False
        try:
            # 保护结果只能作为定位入口，不能作为 unaffected 事实来源。每次继续
            # active 前都重新读取当前任务、基础索引和预计索引，再用同一生成函数
            # 计算完整引用集合、逐项双哈希和 proof_sha256。
            from codex_sdlc.services.change_service import (
                load_change_package_context_locked,
            )

            change_context = load_change_package_context_locked(
                paths,
                change_id=str(protection.get("change_id") or ""),
                requirement_id=requirement_id,
            )
            workspace = change_context.get("workspace")
            payload = change_context.get("payload")
            documents = change_context.get("documents")
            if (
                not isinstance(workspace, Path)
                or workspace != protection_path.parent
                or not isinstance(payload, Mapping)
                or payload.get("package_identity_sha256")
                != protection.get("package_identity_sha256")
                or not isinstance(documents, Mapping)
            ):
                return False
            task_document = _strict_json(
                requirement_root / "tasks" / f"{task_id}.json",
                f"任务合同 {task_id}",
            )
            projected_document = documents.get("projected-reference-index.v2.json")
            if not isinstance(projected_document, Mapping):
                return False
            base = projected_document.get("base")
            projected_content = projected_document.get("content")
            if (
                projected_document.get("schema_version")
                != "projected-reference-index.v2"
                or projected_document.get("requirement_id") != requirement_id
                or projected_document.get("change_id") != protection.get("change_id")
                or not isinstance(base, Mapping)
                or set(base) != {"path", "sha256"}
                or not isinstance(base.get("path"), str)
                or not isinstance(base.get("sha256"), str)
                or not isinstance(projected_content, Mapping)
                or projected_document.get("content_sha256")
                != canonical_sha256(projected_content)
            ):
                return False
            base_path = resolve_project_path(paths.root, str(base["path"]))
            if (
                base_path.is_symlink()
                or not base_path.is_file()
                or sha256_file(base_path) != base["sha256"]
            ):
                return False
            base_document = _strict_json(base_path, "基础引用索引")
            from codex_sdlc.core import dependency_graph

            recomputed = dependency_graph.prove_task_unaffected(
                task_document,
                basis_refs=(
                    proof.get("basis_refs")
                    if isinstance(proof.get("basis_refs"), list)
                    else []
                ),
                base_reference_index=base_document,
                projected_reference_index=projected_content,
            )
        except (OSError, SdlcError):
            return False
        if dict(proof) != recomputed:
            return False
        allowed_changed_paths.update(
            (protection_path.parent / name).relative_to(paths.root).as_posix()
            for name in COMMITTED_FILE_NAMES
        )
        change_id = str(protection.get("change_id") or "")
        matching_events = [
            event
            for event in events
            if event.get("event_type") == "change_protected"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("change_id") == change_id
            and event["payload"].get("protection_path") == relative_path
            and event["payload"].get("protection_sha256")
            == protection.get("protection_sha256")
            and event["payload"].get("package_identity_sha256")
            == protection.get("package_identity_sha256")
        ]
        if len(matching_events) != 1:
            return False
    # 预计版本可能在活动轮次建立后才提交；它们只有与同目录保护结果一起出现、
    # 且上面的当前任务双索引重算通过时，才属于可解释的审核变化。
    return set(changed_files).issubset(allowed_changed_paths)


def _sync_run_status(
    *,
    run_path: Path,
    current_path: Path,
    run: Mapping[str, object],
    current: Mapping[str, object],
    status: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """把轮次和指针同步到重算结果；中断后下一次仍从真实输入重新判断。"""

    updated_run = deepcopy(dict(run))
    updated_run["status"] = status
    validate_schema_document(updated_run, schema_name=TASK_RUN_SCHEMA)
    updated_run_sha256 = sha256_bytes(
        canonical_json_text(updated_run).encode("utf-8")
    )
    updated_current = _current_document(
        requirement_id=str(run["requirement_id"]),
        task_id=str(run["task_id"]),
        run_number=int(run["run_number"]),
        status=status,
        task_run_sha256=updated_run_sha256,
        manifest_sha256=str(run["read_manifest_sha256"]),
        updated_at=now_iso(),
    )
    if (
        run.get("status") == status
        and current.get("status") == status
        and current.get("task_run_sha256") == updated_run_sha256
    ):
        return updated_run, dict(current)
    try:
        _atomic_write(run_path, updated_run)
        _before_run_status_current_commit(updated_run)
        _atomic_write(current_path, updated_current)
    except OSError as exc:
        raise SdlcError(
            f"任务运行状态同步失败，可重新执行同一命令恢复：{exc}。",
            exit_code=1,
        ) from exc
    return updated_run, updated_current


def protect_task_run_for_change(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    change_id: str,
) -> dict[str, object]:
    """先把活动轮次确定性写为 stale，再把任务退回 todo 等待重新开工。"""

    state = derive_state(paths)
    requirement, task = resolve_task(state, requirement_id, task_id)
    task_status = str(task.get("status") or "")
    if task_status == "done":
        raise SdlcError(f"已完成任务 {task_id} 不能被变更保护原地改写。", exit_code=1)
    if task_status not in {"doing", "ready_for_user_check", "test_failed", "todo"}:
        raise SdlcError(f"任务 {task_id} 当前状态不能执行变更保护：{task_status}。", exit_code=1)

    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    run_number: int | None = None
    run_before_status: str | None = None
    if current_path.exists() or current_path.is_symlink():
        # 上一次可能正好中断在 run 已写、current 未写的位置；这里必须读取两份
        # 结构化文件继续同步，不能先用严格哈希门禁把可恢复状态挡在外面。
        if current_path.is_symlink() or not current_path.is_file():
            raise SdlcError("任务当前运行指针不是普通文件。", exit_code=1)
        current = _strict_json(current_path, "任务当前运行指针")
        run_number_value = _validate_current(current, requirement_id, task_id)
        run_path = task_runtime / "runs" / f"{run_number_value:04d}" / "task-run.v1.json"
        if run_path.is_symlink() or not run_path.is_file():
            raise SdlcError("当前任务运行轮次文件不完整。", exit_code=1)
        run = _strict_json(run_path, "任务运行轮次")
        validate_schema_document(run, schema_name=TASK_RUN_SCHEMA)
        if (
            run.get("requirement_id") != requirement_id
            or run.get("task_id") != task_id
            or run.get("run_number") != run_number_value
        ):
            raise SdlcError("当前任务运行轮次与指针所有权不一致。", exit_code=1)
        run_number = int(run["run_number"])
        run_before_status = str(run.get("status") or "")
        if run_before_status == "closed":
            if task_status == "doing":
                raise SdlcError(
                    f"任务 {task_id} 仍是 doing，但当前运行轮次已经 closed，不能猜测暂停结果。",
                    exit_code=1,
                )
        elif run_before_status not in {"reading", "active", "stale"}:
            raise SdlcError(
                f"任务 {task_id} 的运行状态不能执行变更保护：{run_before_status}。",
                exit_code=1,
            )
        else:
            _sync_run_status(
                run_path=run_path,
                current_path=current_path,
                run=run,
                current=current,
                status="stale",
            )
    elif task_status == "doing":
        raise SdlcError(
            f"任务 {task_id} 正在进行但缺少当前运行轮次，不能安全暂停。",
            exit_code=1,
        )

    # 重试时任务可能已经由上一次调用暂停；不能重复追加同一保护事件。
    refreshed = derive_state(paths)
    _refreshed_requirement, refreshed_task = resolve_task(
        refreshed, requirement_id, task_id
    )
    paused = str(refreshed_task.get("status") or "") == "todo"
    if not paused:
        _before_change_pause_event(task_id)
        note = str(refreshed_task.get("note") or "")
        pause_note = f"暂停说明：变更 {change_id} 已使当前任务输入失效，等待重新开工。"
        if pause_note not in note:
            note = note + ("\n" if note else "") + pause_note
        append_event(
            paths,
            event_type="task_updated",
            source="sdlc-change-protect",
            summary=f"变更 {change_id} 暂停受影响任务 {task_id}",
            requirement_id=requirement_id,
            task_id=task_id,
            payload={
                "status": "todo",
                "note": note,
                "change_id": change_id,
                "task_run_number": run_number,
                "task_run_status": "stale" if run_number is not None else None,
            },
        )
    return {
        "task_id": task_id,
        "task_status_before": task_status,
        "task_status_after": "todo",
        "run_number": run_number,
        "run_status_before": run_before_status,
        "run_status_after": "stale" if run_number is not None and run_before_status != "closed" else run_before_status,
    }


def _current_upstream_snapshot(
    paths: ProjectPaths,
    *,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    run_number: int,
    all_tasks: Mapping[str, Mapping[str, object]],
    allowed_changed_paths: object = None,
) -> tuple[dict[str, object], dict[str, str]]:
    manifest, upstream_hashes = _build_task_read_manifest(
        paths,
        requirement,
        task,
        run_number=run_number,
        # 生成时间不进入上游摘要；固定值避免调用方误把检查时间当成合同变化。
        generated_at="1970-01-01T00:00:00+00:00",
        all_tasks=all_tasks,
        allowed_changed_paths=allowed_changed_paths,
    )
    upstream_hashes["project_rules"] = _project_rules_hash(paths)
    return manifest, upstream_hashes


def _upstream_drift_fields(
    paths: ProjectPaths,
    *,
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    run: Mapping[str, object],
    manifest: Mapping[str, object],
    all_tasks: Mapping[str, Mapping[str, object]],
) -> list[str]:
    changed: list[str] = []
    requirement_root = requirement_dir_for_id(
        paths, str(requirement.get("requirement_id") or "")
    )
    if requirement_root is None:
        return ["requirement"]
    task_json_path = requirement_root / "tasks" / f"{task.get('task_id')}.json"
    if (
        not task_json_path.is_file()
        or task_json_path.is_symlink()
        or sha256_file(task_json_path) != run.get("task_sha256")
    ):
        changed.append("task")

    review = _current_task_review(requirement)
    review_changed = review is None or canonical_sha256(review) != run.get(
        "task_review_sha256"
    )
    if review_changed and not (
        review is not None
        and _task_review_change_is_protected(
            paths,
            requirement_id=str(requirement.get("requirement_id") or ""),
            task_id=str(task.get("task_id") or ""),
            review=review,
        )
    ):
        changed.append("task_review")

    try:
        current_manifest, current_upstream_hashes = _current_upstream_snapshot(
            paths,
            requirement=requirement,
            task=task,
            run_number=int(run["run_number"]),
            all_tasks=all_tasks,
            allowed_changed_paths=run.get("allowed_output_paths"),
        )
    except SdlcError as exc:
        message = str(exc)
        if any(label in message for label in ("任务交付物", "前置任务", "task-output-index")):
            return list(dict.fromkeys([*changed, "predecessor_outputs"]))
        # 其它正式引用无法读取时仍按整体上游不可读处理，不能误报为交付物漂移。
        return list(dict.fromkeys([*changed, "upstream_unreadable"]))
    except OSError:
        # 上游已经无法完整计算时不能继续沿用旧快照；调用方会把轮次确定性写成 stale。
        return list(dict.fromkeys([*changed, "upstream_unreadable"]))
    if current_manifest.get("task_file_sha256") != manifest.get("task_file_sha256"):
        changed.append("task")
    recorded_upstream = run.get("upstream_hashes")
    if not isinstance(recorded_upstream, Mapping):
        changed.append("upstream_hashes")
    else:
        changed.extend(
            field
            for field, current_hash in current_upstream_hashes.items()
            if recorded_upstream.get(field) != current_hash
        )
    return list(dict.fromkeys(changed))


def _worktree_scope_changed(paths: ProjectPaths, run: Mapping[str, object]) -> bool:
    baseline = run.get("code_baseline")
    if not isinstance(baseline, Mapping):
        raise SdlcError("任务运行轮次缺少代码基线。", exit_code=1)
    git_root = find_git_root(paths.root)
    if git_root is None:
        return False
    head = run_git(["rev-parse", "HEAD"], paths.root)
    if head.returncode != 0:
        raise SdlcError("无法读取当前 Git HEAD，不能校验任务范围。", exit_code=1)
    allowed_output_paths = _normalize_allowed_output_paths(
        paths, list(run.get("allowed_output_paths", []))
    )
    current_digest = sha256_bytes(_git_scope_payload(paths, allowed_output_paths))
    return (
        head.stdout.strip() != baseline.get("git_head")
        or current_digest != baseline.get("worktree_diff_sha256")
    )


def require_active_task_run(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
) -> None:
    """新 task.v2 主线收口前必须已经由开工线程确认完整读取清单。"""

    recover_task_start_transaction(paths, requirement_id, task_id)
    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    if not current_path.is_file() or current_path.is_symlink():
        raise SdlcError("当前任务还没有 active 运行轮次，不能完成任务。", exit_code=1)
    current = _strict_json(current_path, "任务当前运行指针")
    run_number = _validate_current(current, requirement_id, task_id)
    run_path = task_runtime / "runs" / f"{run_number:04d}" / "task-run.v1.json"
    if not run_path.is_file() or run_path.is_symlink():
        raise SdlcError("当前任务运行轮次文件不完整，不能完成任务。", exit_code=1)
    run = _strict_json(run_path, "任务运行轮次")
    validate_schema_document(run, schema_name=TASK_RUN_SCHEMA)
    if not isinstance(run.get("read_confirmation"), Mapping):
        raise SdlcError("当前任务仍在 reading 或运行凭据已经变化，不能完成任务。", exit_code=1)
    manifest_path = (
        task_runtime / "runs" / f"{run_number:04d}" / "task-read-manifest.v1.json"
    )
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SdlcError("当前任务读取清单不完整，不能完成任务。", exit_code=1)
    manifest = load_task_read_manifest(manifest_path)
    if (
        sha256_file(manifest_path) != run.get("read_manifest_sha256")
        or current.get("read_manifest_sha256") != run.get("read_manifest_sha256")
    ):
        raise SdlcError("当前任务读取清单或运行凭据已经变化，不能完成任务。", exit_code=1)
    # active/stale 两个状态可能来自上次双文件同步的中断；其它状态不能用重算掩盖。
    if current.get("status") not in {"active", "stale"} or run.get("status") not in {
        "active",
        "stale",
    }:
        raise SdlcError("当前任务仍在 reading 或运行凭据已经变化，不能完成任务。", exit_code=1)

    _require_run_worktree_identity(paths, run)
    state = derive_state(paths)
    requirement, task = resolve_task(state, requirement_id, task_id)
    all_tasks = _all_tasks(state)
    changed_upstream = _upstream_drift_fields(
        paths,
        requirement=requirement,
        task=task,
        run=run,
        manifest=manifest,
        all_tasks=all_tasks,
    )
    run, current = _sync_run_status(
        run_path=run_path,
        current_path=current_path,
        run=run,
        current=current,
        status="stale" if changed_upstream else "active",
    )
    if changed_upstream:
        fields = "、".join(changed_upstream)
        raise SdlcError(
            f"[TASK_RUN_STALE] 当前任务上游已经变化（{fields}），请重新开工。",
            exit_code=1,
        )
    if _worktree_scope_changed(paths, run):
        raise SdlcError(
            "[TASK_SCOPE_UNEXPLAINED] 任务允许输出范围外出现了无法归属的工作树变化，不能完成任务。",
            exit_code=1,
        )


def _current_allowed_changes(
    paths: ProjectPaths, run: Mapping[str, object]
) -> list[str]:
    """只记录当前 Git 状态里确实出现的任务输出，不能把计划路径当成实际结果。"""

    if find_git_root(paths.root) is None:
        return []
    status = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        paths.root,
    )
    if status.returncode != 0:
        raise SdlcError("无法读取当前 Git 修改范围，不能完成任务。", exit_code=1)
    allowed = _normalize_allowed_output_paths(
        paths, list(run.get("allowed_output_paths", []))
    )
    changed: list[str] = []
    records = [item for item in status.stdout.split("\0") if item]
    for record in records:
        # porcelain v1 的路径从第 4 个字符开始；重命名记录会额外带一个目标路径，
        # 当前完成门禁只把明确落在允许范围内的实际文件写进完成事件。
        candidate = record[3:] if len(record) >= 4 else ""
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        candidate = candidate.strip()
        if not candidate:
            continue
        candidate_path = Path(candidate)
        for allowed_path in allowed:
            allowed_candidate = Path(allowed_path)
            if candidate_path == allowed_candidate or allowed_candidate in candidate_path.parents:
                changed.append(candidate_path.as_posix())
                break
    return list(dict.fromkeys(changed))


def complete_task_run(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
) -> dict[str, object]:
    """重新校验当前轮次全部证据，并一次性关闭任务和轮次。"""

    require_active_task_run(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    context = load_task_run_context(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    run_path = context["run_path"]
    current_path = context["current_path"]
    run = context["run"]
    current = context["current"]
    assert isinstance(run_path, Path)
    assert isinstance(current_path, Path)
    assert isinstance(run, Mapping)
    assert isinstance(current, Mapping)
    if run.get("status") != "active" or current.get("status") != "active":
        raise SdlcError("当前任务运行轮次不是 active，不能完成任务。", exit_code=1)

    state = derive_state(paths)
    requirement, task = resolve_task(state, requirement_id, task_id)
    from codex_sdlc.core.task_evidence import validate_completion_evidence

    evidence_summary = validate_completion_evidence(paths, task=task, run=run)
    actual_changed_files = _current_allowed_changes(paths, run)
    verification_records = evidence_summary["verification_records"]
    commands = evidence_summary["commands"]
    assert isinstance(verification_records, list)
    assert isinstance(commands, list)

    used_verification_ids = [
        str(item.get("verification_id") or "")
        for item in state.get("verifications", [])
        if isinstance(item, Mapping)
    ]
    materialized_verifications: list[dict[str, str]] = []
    for record in verification_records:
        if not isinstance(record, Mapping):
            continue
        verification_id = next_number(used_verification_ids, "VRF")
        used_verification_ids.append(verification_id)
        materialized_verifications.append(
            {
                "verification_id": verification_id,
                "created_at": str(record.get("recorded_at") or now_iso()),
                "type": "manual",
                "status": "passed",
                "summary": str(record.get("summary") or "人工验收记录完整且通过"),
                "file_path": (
                    f".codex-sdlc/requirements/{requirement['folder_name']}"
                    f"/verifications/{verification_id}.md"
                ),
            }
        )

    updated_run = deepcopy(dict(run))
    updated_run["status"] = "closed"
    validate_schema_document(updated_run, schema_name=TASK_RUN_SCHEMA)
    updated_run_sha256 = sha256_bytes(
        canonical_json_text(updated_run).encode("utf-8")
    )
    updated_current = _current_document(
        requirement_id=requirement_id,
        task_id=task_id,
        run_number=int(updated_run["run_number"]),
        status="closed",
        task_run_sha256=updated_run_sha256,
        manifest_sha256=str(updated_run["read_manifest_sha256"]),
        updated_at=now_iso(),
    )
    output_index_path = formal_task_output_index_path(paths, requirement)
    output_index_before = output_index_path.read_bytes() if output_index_path.exists() else None
    output_index = index_completed_task(
        paths,
        requirement,
        task_id=task_id,
        closed_run=updated_run,
        actual_changed_files=actual_changed_files,
    )
    run_before = run_path.read_bytes()
    current_before = current_path.read_bytes()
    events_before, event_backups_before = _event_backup_snapshot(paths)
    try:
        append_event(
            paths,
            event_type="task_updated",
            source="sdlc-task-done",
            summary=f"完成任务 {task_id} 并关闭第 {run['run_number']} 次运行",
            requirement_id=requirement_id,
            task_id=task_id,
            payload={
                "status": "done",
                "note": str(task.get("note") or ""),
                "changed_files": actual_changed_files,
                "commands": commands,
                "test_items": [],
                "test_commands": [],
                "test_scripts": [],
                "manual_checks": [],
                "verifications": materialized_verifications,
                "task_run_number": int(run["run_number"]),
                "task_run_status": "closed",
            },
        )
        _atomic_write(run_path, updated_run)
        _before_task_output_index_commit(output_index)
        replace_formal_task_output_index(paths, requirement, output_index)
        _after_task_output_index_commit(output_index)
        _before_completion_current_commit(updated_run)
        _atomic_write(current_path, updated_current)
    except Exception as exc:
        try:
            _restore_bytes(run_path, run_before)
            _restore_bytes(current_path, current_before)
            _restore_bytes(output_index_path, output_index_before)
            _rollback_event_snapshot(paths, events_before, event_backups_before)
        except OSError:
            pass
        if isinstance(exc, SdlcError):
            raise
        raise SdlcError(f"任务完成事务失败：{exc}。", exit_code=1) from exc
    return {
        "run": updated_run,
        "current": updated_current,
        "changed_files": actual_changed_files,
        "verification_records": materialized_verifications,
        "test_count": int(evidence_summary["test_count"]),
        "feedback_count": int(evidence_summary["feedback_count"]),
    }


def _restore_request_sha256(requirement_id: str, task_id: str, reason: str) -> str:
    return canonical_sha256(
        {
            "requirement_id": requirement_id,
            "task_id": task_id,
            "reason": reason,
        }
    )


def _restore_journal_path(task_runtime: Path) -> Path:
    return task_runtime / ".restore-transaction.json"


def _restore_record_path(task_runtime: Path, run_number: int) -> Path:
    return task_runtime / "runs" / f"{run_number:04d}" / "task-restore.v1.json"


def _validate_restore_record(
    record: Mapping[str, object],
    *,
    requirement_id: str,
    task_id: str,
    old_run_number: int,
    new_run_number: int,
) -> None:
    required_fields = {
        "schema_version",
        "requirement_id",
        "task_id",
        "closed_run_number",
        "new_run_number",
        "close_reason",
        "closed_at",
        "request_sha256",
        "new_read_manifest_sha256",
        "new_upstream_hashes_sha256",
    }
    hash_fields = (
        "request_sha256",
        "new_read_manifest_sha256",
        "new_upstream_hashes_sha256",
    )
    if (
        set(record) != required_fields
        or record.get("schema_version") != RESTORE_RECORD_SCHEMA
        or record.get("requirement_id") != requirement_id
        or record.get("task_id") != task_id
        or record.get("closed_run_number") != old_run_number
        or record.get("new_run_number") != new_run_number
        or not str(record.get("close_reason") or "").strip()
        or not str(record.get("closed_at") or "").strip()
        or any(
            len(str(record.get(field) or "")) != 64
            or any(ch not in "0123456789abcdef" for ch in str(record.get(field) or ""))
            for field in hash_fields
        )
    ):
        raise SdlcError("任务恢复关闭记录结构、身份或哈希不完整。", exit_code=1)


def _validate_restore_journal(
    journal: Mapping[str, object],
    *,
    requirement_id: str,
    task_id: str,
) -> tuple[int, int, dict[str, object], dict[str, object], dict[str, object]]:
    old_run_number = journal.get("old_run_number")
    new_run_number = journal.get("new_run_number")
    updated_old_run = journal.get("updated_old_run")
    close_record = journal.get("close_record")
    new_run = journal.get("new_run")
    new_manifest = journal.get("new_manifest")
    new_current = journal.get("new_current")
    output_index_before = journal.get("output_index_before")
    output_index_after = journal.get("output_index_after")
    if (
        journal.get("schema_version") != RESTORE_TRANSACTION_SCHEMA
        or journal.get("requirement_id") != requirement_id
        or journal.get("task_id") != task_id
        or not isinstance(old_run_number, int)
        or not isinstance(new_run_number, int)
        or old_run_number < 1
        or new_run_number <= old_run_number
        or not isinstance(updated_old_run, dict)
        or not isinstance(close_record, dict)
        or not isinstance(new_run, dict)
        or not isinstance(new_manifest, dict)
        or not isinstance(new_current, dict)
        or not isinstance(output_index_before, str)
        or not isinstance(output_index_after, dict)
    ):
        raise SdlcError("任务恢复事务身份或轮次编号不完整。", exit_code=1)
    validate_schema_document(updated_old_run, schema_name=TASK_RUN_SCHEMA)
    validate_schema_document(new_run, schema_name=TASK_RUN_SCHEMA)
    validate_schema_document(new_manifest, schema_name="task-read-manifest.v1")
    _validate_current(new_current, requirement_id, task_id)
    _validate_restore_record(
        close_record,
        requirement_id=requirement_id,
        task_id=task_id,
        old_run_number=old_run_number,
        new_run_number=new_run_number,
    )
    try:
        before_document = json.loads(
            output_index_before,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, SdlcError) as exc:
        raise SdlcError("任务恢复事务缺少可回滚的任务交付物索引。", exit_code=1) from exc
    if (
        not isinstance(before_document, Mapping)
        or output_index_after.get("schema_version") != "task-output-index.v1"
        or output_index_after.get("requirement_id") != requirement_id
        or any(
            isinstance(item, Mapping) and item.get("task_id") == task_id
            for item in output_index_after.get("task_outputs", [])
        )
    ):
        raise SdlcError("任务恢复事务的任务交付物索引身份不完整。", exit_code=1)
    manifest_sha256 = sha256_bytes(canonical_json_text(new_manifest).encode("utf-8"))
    new_run_sha256 = sha256_bytes(canonical_json_text(new_run).encode("utf-8"))
    if (
        updated_old_run.get("requirement_id") != requirement_id
        or updated_old_run.get("task_id") != task_id
        or updated_old_run.get("run_number") != old_run_number
        or updated_old_run.get("status") != "closed"
        or new_run.get("requirement_id") != requirement_id
        or new_run.get("task_id") != task_id
        or new_run.get("run_number") != new_run_number
        or new_run.get("status") != "reading"
        or new_manifest.get("requirement_id") != requirement_id
        or new_manifest.get("task_id") != task_id
        or new_manifest.get("run_number") != new_run_number
        or new_current.get("run_number") != new_run_number
        or new_current.get("status") != "reading"
        or new_run.get("read_manifest_sha256") != manifest_sha256
        or new_current.get("read_manifest_sha256") != manifest_sha256
        or new_current.get("task_run_sha256") != new_run_sha256
        or close_record.get("request_sha256") != journal.get("request_sha256")
        or close_record.get("new_read_manifest_sha256") != manifest_sha256
        or close_record.get("new_upstream_hashes_sha256")
        != canonical_sha256(new_run.get("upstream_hashes"))
    ):
        raise SdlcError("任务恢复事务的关闭记录或新轮次指针不一致。", exit_code=1)
    return old_run_number, new_run_number, updated_old_run, close_record, new_current


def recover_task_restore_transaction(
    paths: ProjectPaths,
    requirement_id: str,
    task_id: str,
) -> dict[str, object] | None:
    """按恢复事件是否已经提交，把中断现场收敛到完整旧轮次或完整新轮次。"""

    requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    journal_path = _restore_journal_path(task_runtime)
    if not journal_path.exists():
        return None
    if journal_path.is_symlink() or not journal_path.is_file():
        raise SdlcError("任务恢复事务记录不是普通文件。", exit_code=1)
    journal = _strict_json(journal_path, "任务恢复事务记录")
    (
        old_run_number,
        new_run_number,
        updated_old_run,
        close_record,
        new_current,
    ) = _validate_restore_journal(
        journal, requirement_id=requirement_id, task_id=task_id
    )
    old_run_path = task_runtime / "runs" / f"{old_run_number:04d}" / "task-run.v1.json"
    close_record_path = _restore_record_path(task_runtime, old_run_number)
    staging = task_runtime / f".restore-staging-{new_run_number:04d}"
    final = task_runtime / "runs" / f"{new_run_number:04d}"
    output_index_path = requirement_root / "task-outputs" / "task-output-index.v1.json"
    requirement_identity = {
        "requirement_id": requirement_id,
        "folder_name": requirement_root.name,
    }
    output_index_before = journal.get("output_index_before")
    output_index_after = journal.get("output_index_after")
    assert isinstance(output_index_before, str)
    assert isinstance(output_index_after, dict)
    before_document = json.loads(
        output_index_before,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(before_document, Mapping):
        raise SdlcError("任务恢复事务的旧任务交付物索引不是对象。", exit_code=1)
    validated_before_index = validate_formal_task_output_index(
        paths, requirement_identity, before_document
    )
    validated_after_index = validate_formal_task_output_index(
        paths, requirement_identity, output_index_after
    )
    event_id = str(journal.get("event_id") or "")
    matching_events = [
        event
        for event in load_events(paths)
        if str(event.get("event_id") or "") == event_id
    ]
    if len(matching_events) > 1:
        raise SdlcError("任务恢复事件编号不唯一，不能自动恢复。", exit_code=1)
    event_exists = bool(matching_events)
    if event_exists:
        event = matching_events[0]
        payload = event.get("payload")
        if (
            event.get("event_type") != "task_updated"
            or event.get("source") != "sdlc-task-restore"
            or event.get("requirement_id") != requirement_id
            or event.get("task_id") != task_id
            or not isinstance(payload, Mapping)
            or payload.get("status") != "doing"
            or payload.get("task_run_number") != new_run_number
            or payload.get("restored_from_run_number") != old_run_number
            or payload.get("restore_record_sha256") != canonical_sha256(close_record)
            or payload.get("read_manifest_sha256")
            != close_record.get("new_read_manifest_sha256")
        ):
            raise SdlcError("任务恢复事件和事务文件不能互相核对。", exit_code=1)
    expected_names = {"task-read-manifest.v1.json", "task-run.v1.json"}
    for owned_directory in (staging, final):
        if not owned_directory.exists():
            continue
        if owned_directory.is_symlink() or not owned_directory.is_dir():
            raise SdlcError("任务恢复暂存路径不是受控目录。", exit_code=1)
        unexpected = {
            item.name for item in owned_directory.iterdir() if item.name not in expected_names
        }
        if unexpected:
            raise SdlcError("任务恢复目录出现事务之外的文件，不能自动覆盖或删除。", exit_code=1)
    if event_exists:
        new_run = journal["new_run"]
        new_manifest = journal["new_manifest"]
        assert isinstance(new_run, dict)
        assert isinstance(new_manifest, dict)
        if final.is_symlink() or (final.exists() and not final.is_dir()):
            raise SdlcError("任务恢复的新轮次路径不是受控目录。", exit_code=1)
        final.mkdir(parents=True, exist_ok=True)
        _atomic_write(old_run_path, updated_old_run)
        _atomic_write(close_record_path, close_record)
        _atomic_write(final / "task-read-manifest.v1.json", new_manifest)
        _atomic_write(final / "task-run.v1.json", new_run)
        replace_formal_task_output_index(
            paths, requirement_identity, validated_after_index
        )
        _atomic_write(current_path, new_current)
        shutil.rmtree(staging, ignore_errors=True)
        journal_path.unlink(missing_ok=True)
        return {
            "run": new_run,
            "manifest": new_manifest,
            "current": new_current,
            "close_record": close_record,
            "idempotent": True,
        }

    # 事件还没有提交时，任何准备文件都不算一次正式恢复。恢复原字节后可安全重试，
    # 既不会占用运行号，也不会把旧证据所在轮次留在半关闭状态。
    old_run_before = journal.get("old_run_before")
    current_before = journal.get("current_before")
    if not isinstance(old_run_before, str) or not isinstance(current_before, str):
        raise SdlcError("任务恢复事务缺少可回滚的旧轮次原文。", exit_code=1)
    _restore_bytes(old_run_path, old_run_before.encode("utf-8"))
    _restore_bytes(current_path, current_before.encode("utf-8"))
    _restore_bytes(output_index_path, formal_index_bytes(validated_before_index))
    close_record_path.unlink(missing_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(final, ignore_errors=True)
    journal_path.unlink(missing_ok=True)
    return {"rolled_back": True, "idempotent": True}


def recover_pending_task_restores(paths: ProjectPaths) -> None:
    """状态、任务和恢复命令共用这一步，避免只读入口读到事务中间态。"""

    if not paths.requirements_dir.is_dir():
        return
    journals = sorted(
        paths.requirements_dir.glob("*/runtime/T-*/.restore-transaction.json")
    )
    for journal_path in journals:
        if journal_path.is_symlink() or not journal_path.is_file():
            raise SdlcError("任务恢复事务路径不是项目内普通文件。", exit_code=1)
        journal = _strict_json(journal_path, "任务恢复事务记录")
        requirement_id = str(journal.get("requirement_id") or "")
        task_id = str(journal.get("task_id") or "")
        _validate_restore_journal(
            journal, requirement_id=requirement_id, task_id=task_id
        )
        _requirement_root, task_runtime, _current_path = _runtime_paths(
            paths, requirement_id, task_id
        )
        # 事务身份必须同时和它所在的受控目录一致，不能只相信 JSON 里的编号。
        if _restore_journal_path(task_runtime).resolve() != journal_path.resolve():
            raise SdlcError("任务恢复事务所在目录与记录身份不一致。", exit_code=1)
        recover_task_restore_transaction(paths, requirement_id, task_id)


def restore_task_run(
    paths: ProjectPaths,
    *,
    state: Mapping[str, object],
    requirement: Mapping[str, object],
    task: Mapping[str, object],
    reason: str,
) -> dict[str, object]:
    """关闭可恢复的当前轮次，并从当前正式输入生成一份全新的 reading 轮次。"""

    requirement_id = str(requirement.get("requirement_id") or "")
    task_id = str(task.get("task_id") or "")
    clean_reason = reason.strip()
    if not clean_reason:
        raise SdlcError("请写清楚这次恢复任务的原因。", exit_code=1)
    recover_task_restore_transaction(paths, requirement_id, task_id)
    if requirement.get("status") == "accepted":
        raise SdlcError("需求已经验收接受，不能再恢复其中的任务。", exit_code=1)
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SdlcError("缺少 CODEX_THREAD_ID，不能创建恢复轮次。", exit_code=1)
    _requirement_root, task_runtime, current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    request_sha256 = _restore_request_sha256(requirement_id, task_id, clean_reason)
    if current_path.is_file() and not current_path.is_symlink():
        current_probe = _strict_json(current_path, "任务当前运行指针")
        current_number = _validate_current(current_probe, requirement_id, task_id)
        if current_probe.get("status") == "reading" and current_number > 1:
            close_record_path = _restore_record_path(task_runtime, current_number - 1)
            if close_record_path.is_file() and not close_record_path.is_symlink():
                close_record = _strict_json(close_record_path, "任务恢复关闭记录")
                _validate_restore_record(
                    close_record,
                    requirement_id=requirement_id,
                    task_id=task_id,
                    old_run_number=current_number - 1,
                    new_run_number=current_number,
                )
                if close_record.get("request_sha256") != request_sha256:
                    raise SdlcError(
                        "当前新轮次已经由另一条恢复请求创建，不能连续占用运行号。",
                        exit_code=1,
                    )
                context = load_task_run_context(
                    paths, requirement_id=requirement_id, task_id=task_id
                )
                return {
                    "run": context["run"],
                    "manifest": load_task_read_manifest(
                        task_runtime
                        / "runs"
                        / f"{current_number:04d}"
                        / "task-read-manifest.v1.json"
                    ),
                    "current": context["current"],
                    "close_record": close_record,
                    "idempotent": True,
                }
    if str(task.get("status") or "") not in {
        "done",
        "ready_for_user_check",
        "test_failed",
    }:
        raise SdlcError("当前任务状态没有可恢复的已完成或待验轮次。", exit_code=1)
    context = load_task_run_context(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    old_run_path = context["run_path"]
    old_run = context["run"]
    current = context["current"]
    assert isinstance(old_run_path, Path)
    assert isinstance(old_run, Mapping)
    assert isinstance(current, Mapping)
    old_run_number = int(old_run["run_number"])
    if old_run.get("status") != current.get("status"):
        raise SdlcError("当前轮次和指针状态不一致，不能恢复。", exit_code=1)
    if old_run.get("status") == "closed" and task.get("status") != "done":
        raise SdlcError("当前轮次已经关闭且任务状态不匹配，不能恢复。", exit_code=1)
    if old_run.get("status") not in {"active", "stale", "closed"}:
        raise SdlcError("当前轮次还不能恢复。", exit_code=1)
    if _restore_record_path(task_runtime, old_run_number).exists():
        raise SdlcError("当前轮次已经执行过恢复，不能再次关闭。", exit_code=1)

    review = _passed_task_review(requirement)
    all_tasks = _all_tasks(state)
    for dependency_id in task.get("depends_on", []):
        dependency = all_tasks.get(str(dependency_id))
        if dependency is None or dependency.get("status") not in {"done", "closed"}:
            raise SdlcError(f"{task_id} 的前置任务不完整，不能恢复。", exit_code=1)
    new_run_number = _next_run_number(task_runtime)
    timestamp = now_iso()
    output_index_path = formal_task_output_index_path(paths, requirement)
    output_index_before = output_index_path.read_bytes()
    current_output_index = load_formal_task_output_index(
        paths, requirement, required=True
    )
    restored_output_index = remove_completed_task(
        paths, requirement, task_id, base_document=current_output_index
    )
    new_manifest, upstream_hashes = _build_task_read_manifest(
        paths,
        requirement,
        task,
        run_number=new_run_number,
        generated_at=timestamp,
        all_tasks=all_tasks,
        index_document=restored_output_index,
    )
    upstream_hashes["project_rules"] = _project_rules_hash(paths)
    from codex_sdlc.core.backup import require_matching_sdlc_identity

    require_matching_sdlc_identity(paths)
    allowed_output_paths = _normalize_allowed_output_paths(
        paths, list(task.get("changed_files", []))
    )
    requirement_root, _task_runtime, _current_path = _runtime_paths(
        paths, requirement_id, task_id
    )
    manifest_sha256 = sha256_bytes(
        canonical_json_text(new_manifest).encode("utf-8")
    )
    new_run: dict[str, object] = {
        "schema_version": TASK_RUN_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": new_run_number,
        "runner_thread_id": thread_id,
        "status": "reading",
        "task_sha256": sha256_file(requirement_root / "tasks" / f"{task_id}.json"),
        "task_review_sha256": canonical_sha256(review),
        "read_manifest_sha256": manifest_sha256,
        "upstream_hashes": upstream_hashes,
        "code_baseline": _code_baseline(paths, allowed_output_paths),
        "allowed_output_paths": allowed_output_paths,
        "test_records": [],
        "feedback_records": [],
        "verification_records": [],
        "started_at": timestamp,
        "read_confirmation": None,
    }
    validate_schema_document(new_run, schema_name=TASK_RUN_SCHEMA)
    new_run_sha256 = sha256_bytes(canonical_json_text(new_run).encode("utf-8"))
    new_current = _current_document(
        requirement_id=requirement_id,
        task_id=task_id,
        run_number=new_run_number,
        status="reading",
        task_run_sha256=new_run_sha256,
        manifest_sha256=manifest_sha256,
        updated_at=timestamp,
    )
    updated_old_run = deepcopy(dict(old_run))
    updated_old_run["status"] = "closed"
    validate_schema_document(updated_old_run, schema_name=TASK_RUN_SCHEMA)
    close_record: dict[str, object] = {
        "schema_version": RESTORE_RECORD_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "closed_run_number": old_run_number,
        "new_run_number": new_run_number,
        "close_reason": clean_reason,
        "closed_at": timestamp,
        "request_sha256": request_sha256,
        "new_read_manifest_sha256": manifest_sha256,
        "new_upstream_hashes_sha256": canonical_sha256(upstream_hashes),
    }
    event_id = next_event_id(load_events(paths))
    journal: dict[str, object] = {
        "schema_version": RESTORE_TRANSACTION_SCHEMA,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "old_run_number": old_run_number,
        "new_run_number": new_run_number,
        "event_id": event_id,
        "request_sha256": request_sha256,
        "old_run_before": old_run_path.read_text(encoding="utf-8"),
        "current_before": current_path.read_text(encoding="utf-8"),
        "updated_old_run": updated_old_run,
        "close_record": close_record,
        "new_manifest": new_manifest,
        "new_run": new_run,
        "new_current": new_current,
        "output_index_before": output_index_before.decode("utf-8"),
        "output_index_after": restored_output_index,
    }
    journal_path = _restore_journal_path(task_runtime)
    staging = task_runtime / f".restore-staging-{new_run_number:04d}"
    final = task_runtime / "runs" / f"{new_run_number:04d}"
    events_before, event_backups_before = _event_backup_snapshot(paths)
    try:
        if journal_path.exists() or staging.exists() or final.exists():
            raise SdlcError("待分配恢复运行号已经被占用，不能覆盖现有轮次。", exit_code=1)
        _atomic_write(journal_path, journal)
        staging.mkdir(parents=True)
        _atomic_write(staging / "task-read-manifest.v1.json", new_manifest)
        _atomic_write(staging / "task-run.v1.json", new_run)
        _atomic_write(old_run_path, updated_old_run)
        _atomic_write(_restore_record_path(task_runtime, old_run_number), close_record)
        _before_restore_output_index_commit(restored_output_index)
        replace_formal_task_output_index(paths, requirement, restored_output_index)
        _after_restore_output_index_commit(restored_output_index)
        _after_restore_directory_prepare(journal)
        event = append_event(
            paths,
            event_type="task_updated",
            source="sdlc-task-restore",
            summary=f"恢复任务 {task_id} 并创建第 {new_run_number} 次运行",
            requirement_id=requirement_id,
            task_id=task_id,
            payload={
                "status": "doing",
                "task_run_number": new_run_number,
                "task_run_status": "reading",
                "restored_from_run_number": old_run_number,
                "restore_record_sha256": canonical_sha256(close_record),
                "read_manifest_sha256": manifest_sha256,
            },
        )
        if event.get("event_id") != event_id:
            raise OSError("任务恢复事件编号与事务记录不一致")
        _after_restore_event_append(journal)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.rename(staging, final)
        _before_restore_current_commit(journal)
        _atomic_write(current_path, new_current)
        journal_path.unlink(missing_ok=True)
    except Exception as exc:
        try:
            _rollback_event_snapshot(paths, events_before, event_backups_before)
            recover_task_restore_transaction(paths, requirement_id, task_id)
        except OSError:
            pass
        if isinstance(exc, SdlcError):
            raise
        raise SdlcError(f"任务恢复事务失败：{exc}。", exit_code=1) from exc
    return {
        "run": new_run,
        "manifest": new_manifest,
        "current": new_current,
        "close_record": close_record,
        "idempotent": False,
    }


__all__ = [
    "TASK_RUN_SCHEMA",
    "confirm_task_read",
    "complete_task_run",
    "initialize_task_run",
    "load_task_run_context",
    "record_task_run_entry",
    "protect_task_run_for_change",
    "recover_pending_task_restores",
    "recover_task_restore_transaction",
    "require_active_task_run",
    "recover_task_start_transaction",
    "restore_task_run",
]
