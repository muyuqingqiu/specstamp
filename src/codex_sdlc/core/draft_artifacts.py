from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterable

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import canonical_json_text, canonical_sha256, sha256_bytes, sha256_file


ARTIFACT_INDEX_SCHEMA = "artifact-index.v1"
ARTIFACT_RECORD_VERSION = "draft-artifact-record.v1"
FIXED_DIRECTORY_NAMES = ("原始资料", "需求", "设计", "质检")
ORIGINAL_MATERIALS_DIRECTORY = "原始资料"
REQUIREMENTS_DIRECTORY = "需求"
DESIGN_DIRECTORY = "设计"
QUALITY_DIRECTORY = "质检"
STAGING_DIRECTORY_NAME = ".staging"
ARTIFACT_INDEX_FILE_NAME = "artifact-index.v1.json"
STATUS_FILE_NAME = "status.json"

_DRAFT_ID_PATTERN = re.compile(r"DRAFT-[0-9]+")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
QUESTIONS_PROJECTION_PATH = "待确认问题.md"
DECISIONS_PROJECTION_PATH = "用户决定.md"
_RESERVED_ROOT_FILES = {
    ARTIFACT_INDEX_FILE_NAME,
    STATUS_FILE_NAME,
    "requirement.draft.md",
    "design.draft.md",
    "review.md",
    "questions.md",
    "decisions.md",
    "source-projection.json",
    "source-index.json",
    "requirement.facts.json",
    "design.facts.json",
    "model-review.json",
}


@dataclass(frozen=True)
class DraftLayoutResult:
    draft_dir: Path
    created_root: bool
    created_directories: tuple[Path, ...]


@dataclass(frozen=True)
class ProjectionSpec:
    source_path: str
    artifact_type: str
    projection_kind: str
    state_field: str


# 这五份文件只是现有 DRAFT 结构化事件的阅读投影。把映射集中在这里，
# 后续增加正式需求或设计产物时不会再从 Markdown 标题倒推业务类型。
BUILTIN_PROJECTION_SPECS: dict[str, ProjectionSpec] = {
    "requirement_body": ProjectionSpec(
        source_path="需求/需求草稿.md",
        artifact_type="requirement_draft_projection",
        projection_kind="draft_requirement_markdown",
        state_field="requirement_body",
    ),
    "design_body": ProjectionSpec(
        source_path="设计/技术草稿.md",
        artifact_type="design_draft_projection",
        projection_kind="draft_design_markdown",
        state_field="design_body",
    ),
    "review_items": ProjectionSpec(
        source_path="质检/审查记录.md",
        artifact_type="review_projection",
        projection_kind="draft_review_markdown",
        state_field="review_items",
    ),
    "questions": ProjectionSpec(
        source_path="待确认问题.md",
        artifact_type="question_projection",
        projection_kind="draft_questions_markdown",
        state_field="questions",
    ),
    "decisions": ProjectionSpec(
        source_path="用户决定.md",
        artifact_type="decision_projection",
        projection_kind="draft_decisions_markdown",
        state_field="decisions",
    ),
}
_SPEC_BY_PROJECTION_KIND = {spec.projection_kind: spec for spec in BUILTIN_PROJECTION_SPECS.values()}


def _validate_draft_id(draft_id: str) -> str:
    clean_id = str(draft_id or "").strip().upper()
    if not _DRAFT_ID_PATTERN.fullmatch(clean_id):
        raise SdlcError(f"DRAFT 编号格式无效：{draft_id}。", exit_code=1)
    return clean_id


def _ensure_real_directory(path: Path, *, label: str, created: list[Path]) -> None:
    if path.is_symlink():
        raise SdlcError(f"{label}不能是符号链接：{path}。", exit_code=1)
    if path.exists():
        if not path.is_dir():
            raise SdlcError(f"{label}不是目录：{path}。", exit_code=1)
        return
    path.mkdir()
    created.append(path)


def ensure_draft_layout(paths, draft_id: str) -> DraftLayoutResult:
    """只补固定目录，不读取或改写原始资料内容。"""

    clean_id = _validate_draft_id(draft_id)
    paths.drafts_dir.mkdir(parents=True, exist_ok=True)
    if paths.drafts_dir.is_symlink():
        raise SdlcError("DRAFT 根目录不能是符号链接。", exit_code=1)

    draft_dir = paths.draft_dir(clean_id)
    created: list[Path] = []
    try:
        _ensure_real_directory(draft_dir, label=f"{clean_id} 目录", created=created)
        for name in (*FIXED_DIRECTORY_NAMES, STAGING_DIRECTORY_NAME):
            _ensure_real_directory(draft_dir / name, label=f"{clean_id} 的{name}目录", created=created)
    except Exception:
        # 目录建立失败时只撤销本次新建且仍为空的目录，绝不能顺手删除已有资料。
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return DraftLayoutResult(
        draft_dir=draft_dir,
        created_root=draft_dir in created,
        created_directories=tuple(created),
    )


def remove_new_draft_layout(layout: DraftLayoutResult) -> None:
    """创建 DRAFT 的业务事件失败时，删除只属于该次创建的空工作包。"""

    if layout.created_root and layout.draft_dir.exists() and not layout.draft_dir.is_symlink():
        shutil.rmtree(layout.draft_dir)


def producer_run_id(producer_task_id: str) -> str:
    """运行标识优先取当前 Codex 任务；普通本地命令使用明确的命令来源。"""

    task_id = str(producer_task_id or "").strip()
    if not task_id:
        raise SdlcError("派生产物缺少生产任务标识。", exit_code=1)
    return os.environ.get("CODEX_THREAD_ID", "").strip() or task_id


def _input_hashes_for_field(draft: dict[str, Any], field: str) -> dict[str, str]:
    draft_id = _validate_draft_id(str(draft.get("draft_id") or ""))
    return {f"draft://{draft_id}/{field}": canonical_sha256(draft.get(field))}


def _markdown_body(draft: dict[str, Any], field: str, title: str) -> bytes:
    body = str(draft.get(field) or "").strip()
    text = body + "\n" if body else f"# {draft['draft_id']} {title}\n\n暂无内容\n"
    return text.encode("utf-8")


def _visible_item(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("description") or item.get("id") or "").strip()
    return str(item).strip()


def _markdown_list(draft: dict[str, Any], field: str, title: str) -> bytes:
    items = [_visible_item(item) for item in draft.get(field, [])]
    items = [item for item in items if item]
    lines = [f"# {draft['draft_id']} {title}", "", "## 内容"]
    lines.extend(f"- {item}" for item in items)
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def render_registered_artifact(draft: dict[str, Any], record: dict[str, Any]) -> bytes:
    kind = str(record.get("projection_kind") or "")
    if kind == "draft_requirement_markdown":
        return _markdown_body(draft, "requirement_body", "需求草稿")
    if kind == "draft_design_markdown":
        return _markdown_body(draft, "design_body", "技术草稿")
    if kind == "draft_review_markdown":
        return _markdown_list(draft, "review_items", "审查记录")
    if kind == "draft_questions_markdown":
        return _markdown_list(draft, "questions", "待确认问题")
    if kind == "draft_decisions_markdown":
        return _markdown_list(draft, "decisions", "用户决定")
    if kind == "structured_json":
        return canonical_json_text(record.get("document")).encode("utf-8")
    raise SdlcError(f"DRAFT 派生产物使用了不支持的投影类型：{kind}。", exit_code=1)


def _validate_input_hashes(input_hashes: object) -> dict[str, str]:
    if not isinstance(input_hashes, dict) or not input_hashes:
        raise SdlcError("派生产物必须记录至少一个直接输入哈希。", exit_code=1)
    normalized: dict[str, str] = {}
    for raw_path, raw_hash in input_hashes.items():
        path = str(raw_path or "").strip()
        digest = str(raw_hash or "").strip()
        if not path:
            raise SdlcError("派生产物的直接输入路径不能为空。", exit_code=1)
        if not _SHA256_PATTERN.fullmatch(digest):
            raise SdlcError(f"派生产物的直接输入哈希无效：{path}。", exit_code=1)
        normalized[path] = digest
    return dict(sorted(normalized.items()))


def _validate_source_path(source_path: object, *, allow_builtin_root: bool = False) -> str:
    clean_path = str(source_path or "").strip().replace("\\", "/")
    candidate = Path(clean_path)
    if (
        not clean_path
        or "\x00" in clean_path
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate == Path(".")
    ):
        raise SdlcError(f"DRAFT 产物路径无效：{source_path}。", exit_code=1)
    if candidate.parts[0] == ORIGINAL_MATERIALS_DIRECTORY:
        raise SdlcError("原始资料不能登记为可重建派生产物。", exit_code=1)
    if len(candidate.parts) == 1:
        if allow_builtin_root and (
            clean_path == QUESTIONS_PROJECTION_PATH or clean_path == DECISIONS_PROJECTION_PATH
        ):
            return clean_path
        raise SdlcError(f"派生产物必须写入需求、设计或质检目录：{clean_path}。", exit_code=1)
    first_directory = candidate.parts[0]
    if (
        first_directory != REQUIREMENTS_DIRECTORY
        and first_directory != DESIGN_DIRECTORY
        and first_directory != QUALITY_DIRECTORY
    ):
        raise SdlcError(f"派生产物目录不受 DRAFT 管理：{clean_path}。", exit_code=1)
    if clean_path in _RESERVED_ROOT_FILES:
        raise SdlcError(f"派生产物不能覆盖 DRAFT 状态或兼容文件：{clean_path}。", exit_code=1)
    return candidate.as_posix()


def validate_artifact_record(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SdlcError("DRAFT 派生产物登记必须是 JSON 对象。", exit_code=1)
    normalized = deepcopy(record)
    if normalized.get("record_version") != ARTIFACT_RECORD_VERSION:
        raise SdlcError("DRAFT 派生产物登记版本无效。", exit_code=1)
    normalized["draft_id"] = _validate_draft_id(str(normalized.get("draft_id") or ""))
    kind = str(normalized.get("projection_kind") or "").strip()
    normalized["projection_kind"] = kind
    normalized["source_path"] = _validate_source_path(
        normalized.get("source_path"),
        allow_builtin_root=kind in _SPEC_BY_PROJECTION_KIND,
    )
    for field, message in (
        ("artifact_type", "派生产物类型不能为空。"),
        ("media_type", "派生产物媒体类型不能为空。"),
        ("producer_task_id", "派生产物缺少生产任务标识。"),
        ("producer_run_id", "派生产物缺少生产运行标识。"),
    ):
        value = str(normalized.get(field) or "").strip()
        if not value:
            raise SdlcError(message, exit_code=1)
        normalized[field] = value
    normalized["input_hashes"] = _validate_input_hashes(normalized.get("input_hashes"))
    digest = str(normalized.get("artifact_sha256") or "").strip()
    if not _SHA256_PATTERN.fullmatch(digest):
        raise SdlcError("派生产物文件哈希无效。", exit_code=1)
    normalized["artifact_sha256"] = digest
    if kind == "structured_json":
        # 先走公共规范 JSON，拒绝 NaN、非字符串键等无法稳定回放的内容。
        canonical_sha256(normalized.get("document"))
    elif kind not in _SPEC_BY_PROJECTION_KIND:
        raise SdlcError(f"DRAFT 派生产物使用了不支持的投影类型：{kind}。", exit_code=1)
    return normalized


def build_projection_updates(
    draft: dict[str, Any],
    changed_fields: Iterable[str],
    *,
    producer_task_id: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """根据明确字段生成登记，不读取标题、摘要或 Markdown 章节来判断类型。"""

    draft_id = _validate_draft_id(str(draft.get("draft_id") or ""))
    task_id = str(producer_task_id or "").strip()
    current_run_id = str(run_id or producer_run_id(task_id)).strip()
    updates: list[dict[str, Any]] = []
    for field in sorted(set(changed_fields)):
        spec = BUILTIN_PROJECTION_SPECS.get(field)
        if spec is None:
            continue
        record = {
            "record_version": ARTIFACT_RECORD_VERSION,
            "draft_id": draft_id,
            "source_path": spec.source_path,
            "artifact_type": spec.artifact_type,
            "media_type": "text/markdown",
            "projection_kind": spec.projection_kind,
            "input_hashes": _input_hashes_for_field(draft, field),
            "producer_task_id": task_id,
            "producer_run_id": current_run_id,
        }
        record["artifact_sha256"] = sha256_bytes(render_registered_artifact(draft, record))
        updates.append(validate_artifact_record(record))
    return updates


def apply_artifact_updates(draft: dict[str, Any], updates: object) -> None:
    if not isinstance(updates, list):
        return
    current = {
        str(item.get("source_path")): deepcopy(item)
        for item in draft.get("artifact_records", [])
        if isinstance(item, dict) and str(item.get("source_path") or "").strip()
    }
    for raw_record in updates:
        record = validate_artifact_record(raw_record)
        if record["draft_id"] != str(draft.get("draft_id") or ""):
            raise SdlcError("派生产物登记的 draft_id 与当前 DRAFT 不一致。", exit_code=1)
        current[record["source_path"]] = record
    draft["artifact_records"] = [current[path] for path in sorted(current)]


def refresh_existing_projection_records(
    draft: dict[str, Any],
    fields: Iterable[str],
    *,
    producer_task_id: str,
) -> None:
    """系统事件只更新已经登记的投影，旧 DRAFT 不会因此凭空出现新产物。"""

    existing_paths = {
        str(item.get("source_path") or "")
        for item in draft.get("artifact_records", [])
        if isinstance(item, dict)
    }
    selected_fields = [
        field
        for field in fields
        if field in BUILTIN_PROJECTION_SPECS
        and BUILTIN_PROJECTION_SPECS[field].source_path in existing_paths
    ]
    apply_artifact_updates(
        draft,
        build_projection_updates(
            draft,
            selected_fields,
            producer_task_id=producer_task_id,
            run_id=producer_task_id,
        ),
    )


def _expected_builtin_input_hashes(draft: dict[str, Any], record: dict[str, Any]) -> dict[str, str] | None:
    spec = _SPEC_BY_PROJECTION_KIND.get(str(record.get("projection_kind") or ""))
    if spec is None:
        return None
    return _input_hashes_for_field(draft, spec.state_field)


def build_registered_projection_files(draft: dict[str, Any]) -> dict[str, bytes]:
    records = draft.get("artifact_records", [])
    if not isinstance(records, list) or not records:
        return {}

    documents: dict[str, bytes] = {}
    index_records: list[dict[str, Any]] = []
    for raw_record in records:
        record = validate_artifact_record(raw_record)
        expected_inputs = _expected_builtin_input_hashes(draft, record)
        if expected_inputs is not None and record["input_hashes"] != expected_inputs:
            raise SdlcError(
                f"派生产物的直接输入已经变化：{record['source_path']}。",
                exit_code=1,
            )
        content = render_registered_artifact(draft, record)
        digest = sha256_bytes(content)
        if digest != record["artifact_sha256"]:
            raise SdlcError(
                f"派生产物登记哈希与事件投影不一致：{record['source_path']}。",
                exit_code=1,
            )
        documents[record["source_path"]] = content
        index_records.append(
            {
                "record_version": record["record_version"],
                "source_path": record["source_path"],
                "artifact_type": record["artifact_type"],
                "media_type": record["media_type"],
                "sha256": digest,
                "input_hashes": deepcopy(record["input_hashes"]),
                "producer_task_id": record["producer_task_id"],
                "producer_run_id": record["producer_run_id"],
            }
        )

    index_payload = {
        "schema_version": ARTIFACT_INDEX_SCHEMA,
        "draft_id": str(draft.get("draft_id") or ""),
        "artifacts": sorted(index_records, key=lambda item: item["source_path"]),
    }
    documents[ARTIFACT_INDEX_FILE_NAME] = (json.dumps(index_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return documents


def _safe_managed_target(draft_dir: Path, relative_path: str) -> Path:
    normalized = _validate_source_path(relative_path, allow_builtin_root=True) if relative_path not in _RESERVED_ROOT_FILES else relative_path
    candidate = draft_dir / normalized
    try:
        candidate.relative_to(draft_dir)
    except ValueError as exc:
        raise SdlcError(f"DRAFT 投影路径越过工作包：{relative_path}。", exit_code=1) from exc
    current = draft_dir
    for part in Path(normalized).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise SdlcError(f"DRAFT 投影目录不能是符号链接：{relative_path}。", exit_code=1)
    if candidate.is_symlink():
        raise SdlcError(f"DRAFT 投影文件不能是符号链接：{relative_path}。", exit_code=1)
    return candidate


def _replace_projection(source: Path, target: Path) -> None:
    """单独保留替换入口，合同测试可以在真实提交点注入失败。"""

    os.replace(source, target)


def write_projection_bundle(draft_dir: Path, documents: dict[str, bytes]) -> None:
    """先写完整暂存副本，再逐文件替换；失败时恢复提交前的全部受管文件。"""

    if not documents:
        return
    targets: dict[str, Path] = {}
    for relative_path, content in documents.items():
        if not isinstance(content, bytes):
            raise SdlcError(f"DRAFT 投影内容必须是 bytes：{relative_path}。", exit_code=1)
        targets[relative_path] = _safe_managed_target(draft_dir, relative_path)

    staging_root = draft_dir / STAGING_DIRECTORY_NAME
    _ensure_real_directory(staging_root, label="DRAFT 暂存目录", created=[])
    staging_dir = Path(tempfile.mkdtemp(prefix="projection-", dir=staging_root))
    previous: dict[str, bytes | None] = {}
    created_parents: list[Path] = []
    try:
        for relative_path, content in documents.items():
            staged = staging_dir / relative_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
            if sha256_file(staged) != sha256_bytes(content):
                raise SdlcError(f"DRAFT 投影暂存哈希不一致：{relative_path}。", exit_code=1)

        for relative_path, target in targets.items():
            previous[relative_path] = target.read_bytes() if target.exists() else None
            missing: list[Path] = []
            parent = target.parent
            while parent != draft_dir and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir()
                created_parents.append(directory)

        # 先提交正文，最后提交索引和状态；读者不会先看到指向尚未落盘文件的登记。
        order = sorted(
            documents,
            key=lambda item: (item in {ARTIFACT_INDEX_FILE_NAME, STATUS_FILE_NAME}, item),
        )
        for relative_path in order:
            _replace_projection(staging_dir / relative_path, targets[relative_path])
    except Exception:
        # 恢复不走可注入的提交函数，避免一次模拟失败阻断回滚本身。
        for relative_path, old_content in previous.items():
            target = targets[relative_path]
            if old_content is None:
                target.unlink(missing_ok=True)
                continue
            restore_file = staging_dir / ".restore" / relative_path
            restore_file.parent.mkdir(parents=True, exist_ok=True)
            restore_file.write_bytes(old_content)
            os.replace(restore_file, target)
        for directory in reversed(created_parents):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def snapshot_managed_files(draft_dir: Path) -> dict[str, bytes]:
    """失败回滚只快照可重建区，原始资料始终排除在读写范围外。"""

    if not draft_dir.exists():
        return {}
    snapshot: dict[str, bytes] = {}
    for path in sorted(draft_dir.rglob("*")):
        relative = path.relative_to(draft_dir)
        if relative.parts and relative.parts[0] == ORIGINAL_MATERIALS_DIRECTORY:
            continue
        if path.is_symlink():
            raise SdlcError(f"DRAFT 受管区域不能包含符号链接：{relative.as_posix()}。", exit_code=1)
        if path.is_file():
            snapshot[relative.as_posix()] = path.read_bytes()
    return snapshot


def restore_managed_files(draft_dir: Path, snapshot: dict[str, bytes]) -> None:
    if not draft_dir.exists():
        return
    current_files: list[Path] = []
    for path in sorted(draft_dir.rglob("*"), reverse=True):
        relative = path.relative_to(draft_dir)
        if relative.parts and relative.parts[0] == ORIGINAL_MATERIALS_DIRECTORY:
            continue
        if path.is_symlink():
            raise SdlcError(f"DRAFT 受管区域不能包含符号链接：{relative.as_posix()}。", exit_code=1)
        if path.is_file():
            current_files.append(path)
    for path in current_files:
        if path.relative_to(draft_dir).as_posix() not in snapshot:
            path.unlink(missing_ok=True)
    for relative_path, content in snapshot.items():
        target = draft_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, target)


def _preflight_projection(draft_dir: Path, content: bytes) -> str:
    staging_root = draft_dir / STAGING_DIRECTORY_NAME
    _ensure_real_directory(staging_root, label="DRAFT 暂存目录", created=[])
    with tempfile.NamedTemporaryFile(prefix="artifact-", suffix=".tmp", dir=staging_root, delete=False) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        digest = sha256_file(temporary)
        if digest != sha256_bytes(content):
            raise SdlcError("派生产物暂存文件的哈希校验失败。", exit_code=1)
        return digest
    finally:
        temporary.unlink(missing_ok=True)


def preflight_artifact_updates(
    draft_dir: Path,
    draft: dict[str, Any],
    updates: object,
) -> None:
    """业务事件写入前，用真实暂存文件核对每份待更新投影的完整哈希。"""

    if not isinstance(updates, list):
        return
    for raw_record in updates:
        record = validate_artifact_record(raw_record)
        content = render_registered_artifact(draft, record)
        digest = _preflight_projection(draft_dir, content)
        if digest != record["artifact_sha256"]:
            raise SdlcError(f"派生产物暂存哈希与登记不一致：{record['source_path']}。", exit_code=1)


def _validate_direct_input_files(draft_dir: Path, input_hashes: dict[str, str]) -> dict[str, str]:
    normalized = _validate_input_hashes(input_hashes)
    for relative_path, expected_hash in normalized.items():
        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate == Path(".") or ".." in candidate.parts:
            raise SdlcError(f"派生产物的直接输入路径无效：{relative_path}。", exit_code=1)
        input_file = draft_dir / candidate
        try:
            input_file.resolve(strict=True).relative_to(draft_dir.resolve(strict=True))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise SdlcError(f"派生产物的直接输入文件不存在或越过 DRAFT：{relative_path}。", exit_code=1) from exc
        if input_file.is_symlink() or not input_file.is_file():
            raise SdlcError(f"派生产物的直接输入必须是 DRAFT 内普通文件：{relative_path}。", exit_code=1)
        if sha256_file(input_file) != expected_hash:
            raise SdlcError(f"派生产物的直接输入哈希不一致：{relative_path}。", exit_code=1)
    return normalized


def register_json_artifact(
    paths,
    *,
    draft_id: str,
    source_path: str,
    artifact_type: str,
    document: dict[str, Any],
    input_hashes: dict[str, str],
    producer_task_id: str,
    source: str,
) -> dict[str, Any]:
    """登记通用结构化派生产物，供后续需求和设计模块复用。

    公开入口自行持有项目锁。调用方只提交 JSON 真相和直接输入哈希，
    不能提交一份 Markdown 再要求 CLI 从展示文字恢复业务状态。
    """

    from codex_sdlc.core import draft_lifecycle
    from codex_sdlc.core.project import project_lock
    from codex_sdlc.core.state import append_event, derive_state, refresh_materialized_state

    clean_id = _validate_draft_id(draft_id)
    clean_path = _validate_source_path(source_path)
    task_id = str(producer_task_id or "").strip()
    run_id = producer_run_id(task_id)
    content = canonical_json_text(document).encode("utf-8")

    with project_lock(paths):
        state = derive_state(paths)
        draft = state.get("drafts", {}).get(clean_id)
        if not isinstance(draft, dict):
            raise SdlcError(f"没有找到 DRAFT `{clean_id}`。", exit_code=1)
        if draft_lifecycle.is_started_draft(draft):
            raise SdlcError(f"{clean_id} 已经正式建档，不能再登记普通派生产物。", exit_code=1)

        layout = ensure_draft_layout(paths, clean_id)
        snapshot = snapshot_managed_files(layout.draft_dir)
        target = _safe_managed_target(layout.draft_dir, clean_path)
        registered_paths = {
            str(item.get("source_path") or "")
            for item in draft.get("artifact_records", [])
            if isinstance(item, dict)
        }
        if target.exists() and clean_path not in registered_paths:
            raise SdlcError(f"DRAFT 目标文件已经存在但尚未登记：{clean_path}。", exit_code=1)
        checked_input_hashes = _validate_direct_input_files(layout.draft_dir, input_hashes)
        digest = _preflight_projection(layout.draft_dir, content)
        record = validate_artifact_record(
            {
                "record_version": ARTIFACT_RECORD_VERSION,
                "draft_id": clean_id,
                "source_path": clean_path,
                "artifact_type": artifact_type,
                "media_type": "application/json",
                "projection_kind": "structured_json",
                "document": deepcopy(document),
                "artifact_sha256": digest,
                "input_hashes": checked_input_hashes,
                "producer_task_id": task_id,
                "producer_run_id": run_id,
            }
        )
        existed = paths.events_file.exists()
        original_events = paths.events_file.read_bytes() if existed else b""
        try:
            event = append_event(
                paths,
                event_type="draft_artifact_registered",
                source=source,
                summary=f"登记 {clean_id} 派生产物",
                payload={"draft_id": clean_id, "artifact": record},
            )
            refresh_materialized_state(paths)
            return event
        except Exception:
            if existed:
                paths.events_file.write_bytes(original_events)
            else:
                paths.events_file.unlink(missing_ok=True)
            if layout.created_root:
                remove_new_draft_layout(layout)
            else:
                restore_managed_files(layout.draft_dir, snapshot)
            if paths.events_file.exists():
                try:
                    refresh_materialized_state(paths)
                except Exception:
                    if not layout.created_root:
                        restore_managed_files(layout.draft_dir, snapshot)
            raise


__all__ = [
    "ARTIFACT_INDEX_FILE_NAME",
    "ARTIFACT_INDEX_SCHEMA",
    "ARTIFACT_RECORD_VERSION",
    "BUILTIN_PROJECTION_SPECS",
    "DraftLayoutResult",
    "FIXED_DIRECTORY_NAMES",
    "STAGING_DIRECTORY_NAME",
    "STATUS_FILE_NAME",
    "apply_artifact_updates",
    "build_projection_updates",
    "build_registered_projection_files",
    "ensure_draft_layout",
    "producer_run_id",
    "preflight_artifact_updates",
    "refresh_existing_projection_records",
    "register_json_artifact",
    "remove_new_draft_layout",
    "restore_managed_files",
    "snapshot_managed_files",
    "validate_artifact_record",
    "write_projection_bundle",
]
