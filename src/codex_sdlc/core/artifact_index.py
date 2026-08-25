from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from codex_sdlc.core.design_artifact_contract import design_artifact_records
from codex_sdlc.core.design_plan_contract import design_plan_records
from codex_sdlc.core.design_summary_contract import design_summary_records
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    validate_schema_document,
)


ARTIFACT_INDEX_SCHEMA = "artifact-index.v1"
ARTIFACT_INDEX_RECORD_VERSION = "artifact-index-record.v1"
ARTIFACT_INDEX_PATH = "artifact-index.v1.json"
FORMAL_ARCHIVE_ROOT = "original"
_INDEX_ONLY_FIELDS = {
    "record_version",
    "include_in_formal",
    "producer_task_id",
    "producer_run_id",
    "input_hashes",
}
_PROVENANCE_FIELDS = {"producer_task_id", "producer_run_id", "input_hashes"}
_REVIEW_INPUT_SPECS = (
    (
        "_requirement_review_state",
        "requirement_split",
        "sdlc.requirement-review-input.v1",
        "requirement_review_input",
    ),
    (
        "_integrated_design_review_state",
        "integrated_design",
        "sdlc.integrated-design-review-input.v1",
        "integrated_design_review_input",
    ),
)


def _review_relations(
    draft_id: str,
    *,
    stages: Iterable[str] = (),
    depends_on_business_ids: Iterable[str] = (),
) -> dict[str, object]:
    """审核关系只保存受控阶段和稳定业务编号，不能反向写入 REV 造成循环哈希。"""

    return {
        "applies_to": [
            {"stage": stage, "owner_id": draft_id}
            for stage in sorted(set(str(item) for item in stages))
        ],
        "depends_on_business_ids": sorted(
            set(str(item) for item in depends_on_business_ids)
        ),
    }


def _safe_relative_path(value: object, *, label: str) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    candidate = Path(clean)
    if (
        not clean
        or "\x00" in clean
        or candidate.is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or any(part in {"", "."} for part in candidate.parts)
    ):
        raise SdlcError(f"{label}不是安全的项目内相对路径：{value}。", exit_code=1)
    return candidate.as_posix()


def _archive_path(source_path: str) -> str:
    """正式归档统一放到 original 下，来源路径与归档路径不会混成同一个含义。"""

    return f"{FORMAL_ARCHIVE_ROOT}/{_safe_relative_path(source_path, label='DRAFT 来源路径')}"


def _stable_artifact_id(draft_id: str, source_path: str) -> str:
    """ART 由 DRAFT 和受管路径稳定生成，新增文件不会挤占已有文件的编号。"""

    digest = hashlib.sha256(
        f"{draft_id}\0{source_path}".encode("utf-8")
    ).digest()
    # 取 48 位十进制编号，既保持稳定又把碰撞概率压到可忽略范围。
    return f"ART-{int.from_bytes(digest[:6], 'big'):015d}"


def _entry(
    *,
    draft_id: str,
    source_path: str,
    content: bytes,
    business_id: str | None,
    artifact_type: str,
    include_in_formal: bool,
    review_relations: Mapping[str, object],
    producer_task_id: object = None,
    producer_run_id: object = None,
    input_hashes: object = None,
) -> dict[str, object]:
    """路径、类型和哈希都由当前受管对象生成，formal 输入没有自报字段的入口。"""

    clean_source = _safe_relative_path(source_path, label="DRAFT 来源路径")
    entry: dict[str, object] = {
        "record_version": ARTIFACT_INDEX_RECORD_VERSION,
        "artifact_id": _stable_artifact_id(draft_id, clean_source),
        "business_id": business_id,
        "artifact_type": artifact_type,
        "source_path": clean_source,
        "archive_path": _archive_path(clean_source),
        "sha256": sha256_bytes(content),
        "include_in_formal": include_in_formal,
        "review_relations": deepcopy(dict(review_relations)),
    }
    # 前置任务已经把生成来源写进登记事件；继续保留这些字段，才能在统一成新索引后
    # 仍然从同一事件追到生产任务，同时 formal 清单会明确剔除这些索引专用信息。
    if isinstance(producer_task_id, str) and producer_task_id:
        entry["producer_task_id"] = producer_task_id
    if isinstance(producer_run_id, str) and producer_run_id:
        entry["producer_run_id"] = producer_run_id
    if isinstance(input_hashes, Mapping):
        entry["input_hashes"] = {
            str(key): str(value)
            for key, value in sorted(input_hashes.items(), key=lambda item: str(item[0]))
        }
    return entry


def validate_draft_root(paths, draft_id: str) -> Path:
    """拒绝根目录和固定父目录链接，避免不同入口对同一 DRAFT 得出不同边界。"""

    root = paths.root
    fixed_drafts_root = paths.drafts_dir
    draft_root = paths.draft_dir(draft_id)
    for candidate, label in (
        (paths.sdlc_dir, "SDLC 根目录"),
        (fixed_drafts_root, "DRAFT 固定根目录"),
        (draft_root, "DRAFT 根目录"),
    ):
        if candidate.is_symlink():
            raise SdlcError(f"{label}不能是符号链接。", exit_code=1)
    try:
        resolved_project = root.resolve(strict=True)
        resolved_drafts = fixed_drafts_root.resolve(strict=True)
        resolved_draft = draft_root.resolve(strict=True)
        resolved_drafts.relative_to(resolved_project)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError("DRAFT 根目录不存在或不在当前项目内。", exit_code=1) from exc
    # 这里要求直接子目录而不是只做前缀判断，防止链接或异常路径把同名 DRAFT
    # 指向 drafts 下的其他层级，导致生成索引和校验清单读取到不同对象。
    if resolved_draft.parent != resolved_drafts or resolved_draft.name != draft_id:
        raise SdlcError("DRAFT 根目录不在项目固定 drafts 根目录下。", exit_code=1)
    return draft_root


def _document_bytes(
    paths,
    draft_id: str,
    documents: Mapping[str, bytes],
    source_path: str,
) -> bytes:
    content = documents.get(source_path)
    if content is not None:
        if not isinstance(content, bytes):
            raise SdlcError(
                f"受管投影不是字节内容：{source_path}。",
                exit_code=1,
            )
        return content
    draft_root = validate_draft_root(paths, draft_id)
    target = draft_root / source_path
    try:
        resolved_root = draft_root.resolve(strict=True)
        target.resolve(strict=True).relative_to(resolved_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise SdlcError(
            f"产物索引路径不存在或越过 DRAFT：{source_path}。",
            exit_code=1,
        ) from exc
    current = target
    while current != draft_root:
        if current.is_symlink():
            raise SdlcError(
                f"产物索引路径不能经过符号链接：{source_path}。",
                exit_code=1,
            )
        current = current.parent
    if not target.is_file():
        raise SdlcError(
            f"产物索引找不到真实受管文件：{source_path}。",
            exit_code=1,
        )
    try:
        return target.read_bytes()
    except OSError as exc:
        raise SdlcError(
            f"产物索引读取真实受管文件失败：{source_path}。",
            exit_code=1,
        ) from exc


def _has_document(
    paths,
    draft_id: str,
    documents: Mapping[str, bytes],
    source_path: str,
) -> bool:
    return source_path in documents or (paths.draft_dir(draft_id) / source_path).is_file()


def _put_entry(
    entries: dict[str, dict[str, object]],
    paths,
    draft_id: str,
    documents: Mapping[str, bytes],
    *,
    source_path: str,
    business_id: str | None,
    artifact_type: str,
    include_in_formal: bool,
    stages: Iterable[str] = (),
    depends_on_business_ids: Iterable[str] = (),
    producer_task_id: object = None,
    producer_run_id: object = None,
    input_hashes: object = None,
) -> None:
    clean_source = _safe_relative_path(source_path, label="DRAFT 来源路径")
    if not _has_document(paths, draft_id, documents, clean_source):
        return
    candidate = _entry(
        draft_id=draft_id,
        source_path=clean_source,
        content=_document_bytes(paths, draft_id, documents, clean_source),
        business_id=business_id,
        artifact_type=artifact_type,
        include_in_formal=include_in_formal,
        review_relations=_review_relations(
            draft_id,
            stages=stages if include_in_formal else (),
            depends_on_business_ids=(
                depends_on_business_ids if include_in_formal else ()
            ),
        ),
        producer_task_id=producer_task_id,
        producer_run_id=producer_run_id,
        input_hashes=input_hashes,
    )
    existing = entries.get(clean_source)
    if existing is not None and existing != candidate:
        existing_contract = {
            key: value for key, value in existing.items() if key not in _PROVENANCE_FIELDS
        }
        candidate_contract = {
            key: value for key, value in candidate.items() if key not in _PROVENANCE_FIELDS
        }
        if existing_contract == candidate_contract:
            # 同一事件登记产物也可能被阶段专用收集器命中；保留登记来源即可，
            # 不能因为后一条路径没有重复携带生产信息就误判成业务冲突。
            return
        raise SdlcError(f"同一 DRAFT 来源路径出现冲突登记：{clean_source}。", exit_code=1)
    entries[clean_source] = candidate


def _registered_entries(
    entries: dict[str, dict[str, object]],
    paths,
    draft: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    draft_id = str(draft.get("draft_id") or "")
    for raw in draft.get("artifact_records", []):  # type: ignore[union-attr]
        if not isinstance(raw, Mapping):
            continue
        source_path = str(raw.get("source_path") or "")
        projection_kind = str(raw.get("projection_kind") or "")
        raw_type = str(raw.get("artifact_type") or "managed_artifact")
        include = projection_kind == "structured_json"
        business_id: str | None = None
        document = raw.get("document")
        if isinstance(document, Mapping):
            for field in (
                "material_id",
                "confirmation_id",
                "design_id",
                "artifact_id",
                "summary_id",
            ):
                value = document.get(field)
                if isinstance(value, str) and value:
                    business_id = value
                    break
        stages = (
            ("integrated_design",)
            if raw_type.startswith("design_") or raw_type.startswith("code_")
            else ("requirement_split", "integrated_design")
        )
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path=source_path,
            business_id=business_id,
            artifact_type=raw_type,
            include_in_formal=include,
            stages=stages if include else (),
            producer_task_id=raw.get("producer_task_id"),
            producer_run_id=raw.get("producer_run_id"),
            input_hashes=raw.get("input_hashes"),
        )


def _review_input_entries(
    entries: dict[str, dict[str, object]],
    paths,
    draft: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    """只从当前有效审核记录的受控输入哈希中找快照，不扫描质检目录或文件名。"""

    draft_id = str(draft.get("draft_id") or "")
    draft_prefix = paths.draft_dir(draft_id).relative_to(paths.root)
    for state_field, stage, schema, artifact_type in _REVIEW_INPUT_SPECS:
        review_state = draft.get(state_field)
        if not isinstance(review_state, Mapping):
            continue
        reviews = review_state.get("reviews")
        current = [
            item
            for item in reviews
            if isinstance(item, Mapping)
            and item.get("stage") == stage
            and item.get("owner_id") == draft_id
            and item.get("is_current") is True
        ] if isinstance(reviews, list) else []
        if len(current) > 1:
            raise SdlcError(f"{stage} 存在多个当前审核记录。", exit_code=1)
        if not current:
            continue
        review = current[0]
        # stale、未提交和未通过的审核都不是当前正式输入；保留文件但不能进入清单。
        if not (
            review.get("request_status") == "completed"
            and review.get("effective_status") == "passed"
            and review.get("can_advance") is True
        ):
            continue
        input_hashes = review.get("input_hashes")
        if not isinstance(input_hashes, Mapping):
            raise SdlcError(f"{stage} 当前审核缺少受控输入哈希。", exit_code=1)
        matches: list[str] = []
        input_changed = False
        for project_path, expected_hash in sorted(
            input_hashes.items(), key=lambda item: str(item[0])
        ):
            try:
                relative = Path(
                    _safe_relative_path(project_path, label="审核输入路径")
                ).relative_to(draft_prefix).as_posix()
            except ValueError:
                continue
            content = _document_bytes(paths, draft_id, documents, relative)
            if sha256_bytes(content) != expected_hash:
                # 同一事务里新业务投影尚未落盘时，审核状态仍可能来自上一份真实文件；
                # 只要任一受控输入已变化，就把整轮审核从当前集合移除，不能阻断合法变更。
                input_changed = True
                break
            try:
                candidate = json.loads(content.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(candidate, Mapping) or not (
                candidate.get("schema") == schema
                and candidate.get("stage") == stage
                and candidate.get("owner_id") == draft_id
            ):
                continue
            body = {
                key: deepcopy(value)
                for key, value in candidate.items()
                if key != "snapshot_sha256"
            }
            if candidate.get("snapshot_sha256") != canonical_sha256(body):
                raise SdlcError(f"{stage} 当前审核输入快照摘要不一致。", exit_code=1)
            matches.append(relative)
        if input_changed:
            continue
        if len(matches) != 1:
            raise SdlcError(f"{stage} 缺少唯一可信的当前审核输入文件。", exit_code=1)
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path=matches[0],
            business_id=str(review.get("review_id") or "") or None,
            artifact_type=artifact_type,
            include_in_formal=True,
            stages=(stage,),
        )


def _material_entries(
    entries: dict[str, dict[str, object]],
    paths,
    draft: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    draft_id = str(draft.get("draft_id") or "")
    for raw in draft.get("materials", []):  # type: ignore[union-attr]
        if (
            not isinstance(raw, Mapping)
            or raw.get("source_kind") != "file"
            or raw.get("status") == "archived"
        ):
            continue
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path=str(raw.get("stored_path") or ""),
            business_id=str(raw.get("material_id") or "") or None,
            artifact_type="material",
            include_in_formal=True,
            stages=("requirement_split", "integrated_design"),
        )


def _fixed_requirement_entries(
    entries: dict[str, dict[str, object]],
    paths,
    draft: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    draft_id = str(draft.get("draft_id") or "")
    specs = (
        ("需求/requirement-split.v1.json", None, "requirement_split", True),
        ("需求/requirement-coverage.v1.json", None, "requirement_coverage", True),
        ("需求/需求拆分.md", None, "requirement_split_markdown", False),
        ("需求/需求覆盖矩阵.md", None, "requirement_coverage_markdown", False),
        ("需求/需求导入回执.json", None, "requirement_import_receipt", False),
    )
    for source_path, business_id, artifact_type, include in specs:
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path=source_path,
            business_id=business_id,
            artifact_type=artifact_type,
            include_in_formal=include,
            stages=("requirement_split", "integrated_design"),
        )
    confirmations = [
        item
        for item in draft.get("requirement_confirmations", [])  # type: ignore[union-attr]
        if isinstance(item, Mapping)
    ]
    latest = confirmations[-1] if confirmations else None
    if isinstance(latest, Mapping):
        confirmation_id = str(latest.get("confirmation_id") or "") or None
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path="需求/requirement-confirmation.v1.json",
            business_id=confirmation_id,
            artifact_type="requirement_confirmation",
            include_in_formal=True,
            stages=("requirement_split", "integrated_design"),
        )
    for confirmation in confirmations:
        confirmation_id = str(confirmation.get("confirmation_id") or "")
        if confirmation_id:
            _put_entry(
                entries,
                paths,
                draft_id,
                documents,
                source_path=f"需求/确认记录/{confirmation_id}.json",
                business_id=confirmation_id,
                artifact_type="requirement_confirmation_history",
                include_in_formal=False,
            )


def _design_reference_entries(
    entries: dict[str, dict[str, object]],
    paths,
    draft: Mapping[str, object],
    documents: Mapping[str, bytes],
) -> None:
    draft_id = str(draft.get("draft_id") or "")
    _put_entry(
        entries,
        paths,
        draft_id,
        documents,
        source_path="设计/des-index.v1.json",
        business_id=None,
        artifact_type="design_reference_index",
        include_in_formal=True,
        stages=("integrated_design",),
    )
    _put_entry(
        entries,
        paths,
        draft_id,
        documents,
        source_path="设计/技术方案引用.md",
        business_id=None,
        artifact_type="design_reference_markdown",
        include_in_formal=False,
    )
    for raw in draft.get("design_references", []):  # type: ignore[union-attr]
        if not isinstance(raw, Mapping):
            continue
        design_id = str(raw.get("design_id") or "")
        if not design_id:
            continue
        _put_entry(
            entries,
            paths,
            draft_id,
            documents,
            source_path=f"设计/引用记录/{design_id}.json",
            business_id=design_id,
            artifact_type="design_reference",
            include_in_formal=raw.get("status") == "confirmed",
            stages=("integrated_design",),
        )


def validate_artifact_index_document(
    document: Mapping[str, object],
) -> dict[str, object]:
    normalized = deepcopy(dict(document))
    validate_schema_document(normalized, schema_name=ARTIFACT_INDEX_SCHEMA)
    artifacts = normalized["artifacts"]
    source_paths = [str(item["source_path"]) for item in artifacts]  # type: ignore[index]
    if source_paths != sorted(source_paths) or len(set(source_paths)) != len(
        source_paths
    ):
        raise SdlcError(
            "产物索引路径必须唯一并按顺序保存。",
            exit_code=1,
        )
    archive_paths = [str(item["archive_path"]) for item in artifacts]  # type: ignore[index]
    artifact_ids = [str(item["artifact_id"]) for item in artifacts]  # type: ignore[index]
    if len(set(archive_paths)) != len(archive_paths):
        raise SdlcError("产物索引的归档目标不能重复。", exit_code=1)
    if len(set(artifact_ids)) != len(artifact_ids):
        raise SdlcError("产物索引的 ART 编号不能重复。", exit_code=1)
    for item in artifacts:  # type: ignore[assignment]
        _safe_relative_path(item["source_path"], label="DRAFT 来源路径")
        archive_path = _safe_relative_path(item["archive_path"], label="REQ 归档路径")
        if not archive_path.startswith(f"{FORMAL_ARCHIVE_ROOT}/"):
            raise SdlcError(
                f"产物 {item['source_path']} 的归档路径不在 {FORMAL_ARCHIVE_ROOT} 下。",
                exit_code=1,
            )
        relations = item["review_relations"]
        depends_on = relations["depends_on_business_ids"]
        if depends_on != sorted(depends_on) or len(set(depends_on)) != len(
            depends_on
        ):
            raise SdlcError(
                f"产物 {item['source_path']} 的审核依赖必须唯一并按编号排序。",
                exit_code=1,
            )
        if item["include_in_formal"] and not relations["applies_to"]:
            raise SdlcError(
                f"应归档产物缺少审核关系：{item['source_path']}。",
                exit_code=1,
            )
    expected_manifest = formal_manifest_entries(normalized)
    expected_revision = canonical_sha256(
        {
            "draft_id": normalized["draft_id"],
            "artifact_manifest": expected_manifest,
        }
    )
    if normalized["draft_revision_sha256"] != expected_revision:
        raise SdlcError("DRAFT 修订哈希与当前应归档集合不一致。", exit_code=1)
    body = {
        "schema_version": normalized["schema_version"],
        "draft_id": normalized["draft_id"],
        "draft_revision_sha256": normalized["draft_revision_sha256"],
        "artifacts": normalized["artifacts"],
    }
    if normalized["index_sha256"] != canonical_sha256(body):
        raise SdlcError("产物索引哈希与内容不一致。", exit_code=1)
    return normalized


def build_artifact_index_document(
    paths,
    draft: Mapping[str, object],
    *,
    events: Iterable[Mapping[str, object]],
    documents: Mapping[str, bytes],
) -> dict[str, object]:
    """从事件确定业务对象，再从受管文件字节生成路径、类型和完整哈希。"""

    source_events = list(events)
    draft_id = str(draft.get("draft_id") or "")
    validate_draft_root(paths, draft_id)
    entries: dict[str, dict[str, object]] = {}
    _registered_entries(entries, paths, draft, documents)
    _material_entries(entries, paths, draft, documents)
    _fixed_requirement_entries(entries, paths, draft, documents)
    _design_reference_entries(entries, paths, draft, documents)
    _review_input_entries(entries, paths, draft, documents)

    plans = design_plan_records(paths, draft_id=draft_id, events=source_events)
    if len(plans) > 1:
        raise SdlcError(
            f"{draft_id} 缺少唯一有效的开发设计总计划。",
            exit_code=1,
        )
    if plans:
        for source_path, artifact_type, include in (
            ("设计/design-plan.v1.json", "design_plan_json", True),
            ("设计/code-evidence.v1.json", "code_evidence_json", True),
            ("设计/开发设计总计划.md", "design_plan_markdown", False),
        ):
            _put_entry(
                entries,
                paths,
                draft_id,
                documents,
                source_path=source_path,
                business_id=draft_id,
                artifact_type=artifact_type,
                include_in_formal=include,
                stages=("integrated_design",),
            )

    modules = design_artifact_records(
        paths,
        draft_id=draft_id,
        events=source_events,
    )
    for record in modules:
        module_id = str(record["artifact_id"])
        output_path = str(record["output_path"])
        markdown_path = str(Path(output_path).with_suffix(".md")).replace(
            "\\", "/"
        )
        relations = _review_relations(
            draft_id,
            depends_on_business_ids=record["depends_on"],  # type: ignore[arg-type]
        )
        for source_path, artifact_type, include in (
            (output_path, "design_artifact_json", True),
            (markdown_path, "design_artifact_markdown", False),
        ):
            _put_entry(
                entries,
                paths,
                draft_id,
                documents,
                source_path=source_path,
                business_id=module_id,
                artifact_type=artifact_type,
                include_in_formal=include,
                stages=("integrated_design",),
                depends_on_business_ids=relations[
                    "depends_on_business_ids"
                ],  # type: ignore[arg-type]
            )

    summaries = design_summary_records(
        paths,
        draft_id=draft_id,
        events=source_events,
    )
    if len(summaries) > 1:
        raise SdlcError(
            f"{draft_id} 缺少唯一有效的总体设计说明。",
            exit_code=1,
        )
    if summaries:
        summary = summaries[0]
        summary_dependencies = sorted(
            key.split(":", 1)[1]
            for key in summary["input_hashes"]  # type: ignore[union-attr]
            if str(key).startswith("module:")
        )
        for source_path, artifact_type, include in (
            ("设计/design-summary.v1.json", "design_summary_json", True),
            ("设计/总体设计说明.md", "design_summary_markdown", False),
        ):
            _put_entry(
                entries,
                paths,
                draft_id,
                documents,
                source_path=source_path,
                business_id=str(summary["summary_id"]),
                artifact_type=artifact_type,
                include_in_formal=include,
                stages=("integrated_design",),
                depends_on_business_ids=summary_dependencies,
            )
    elif _has_document(
        paths, draft_id, documents, "设计/design-summary.v1.json"
    ):
        # 合同测试和受控迁移可以只提供真实文件；生产链路仍由事件记录补齐 DSUM 编号。
        for source_path, artifact_type, include in (
            ("设计/design-summary.v1.json", "design_summary_json", True),
            ("设计/总体设计说明.md", "design_summary_markdown", False),
        ):
            _put_entry(
                entries,
                paths,
                draft_id,
                documents,
                source_path=source_path,
                business_id=None,
                artifact_type=artifact_type,
                include_in_formal=include,
                stages=("integrated_design",),
            )

    artifacts = [entries[key] for key in sorted(entries)]
    manifest = [
        {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in _INDEX_ONLY_FIELDS
        }
        for item in artifacts
        if item["include_in_formal"]
    ]
    draft_revision_sha256 = canonical_sha256(
        {"draft_id": draft_id, "artifact_manifest": manifest}
    )
    body: dict[str, object] = {
        "schema_version": ARTIFACT_INDEX_SCHEMA,
        "draft_id": draft_id,
        "draft_revision_sha256": draft_revision_sha256,
        "artifacts": artifacts,
    }
    return validate_artifact_index_document(
        {**body, "index_sha256": canonical_sha256(body)}
    )


def artifact_index_bytes(document: Mapping[str, object]) -> bytes:
    return canonical_json_text(
        validate_artifact_index_document(document)
    ).encode("utf-8")


def formal_manifest_entries(
    document: Mapping[str, object],
) -> list[dict[str, object]]:
    """formal 只消费显式 include 集合，索引自身和展示投影不会进入修订哈希。"""

    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list):
        raise SdlcError("产物索引缺少 artifacts。", exit_code=1)
    return [
        {
            key: deepcopy(value)
            for key, value in item.items()
            if key not in _INDEX_ONLY_FIELDS
        }
        for item in artifacts
        if isinstance(item, Mapping) and item.get("include_in_formal") is True
    ]


def validate_artifact_index_files(paths, draft_id: str) -> dict[str, object]:
    validate_draft_root(paths, draft_id)
    index_path = paths.draft_artifact_index_file(draft_id)
    if index_path.is_symlink() or not index_path.is_file():
        raise SdlcError("产物索引不存在或不是普通文件。", exit_code=1)
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("产物索引读取失败或不是有效 JSON。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError("产物索引顶层必须是 JSON 对象。", exit_code=1)
    validated = validate_artifact_index_document(document)
    if validated["draft_id"] != draft_id:
        raise SdlcError("产物索引的 draft_id 与目标 DRAFT 不一致。", exit_code=1)
    for item in validated["artifacts"]:  # type: ignore[index]
        content = _document_bytes(
            paths,
            draft_id,
            {},
            str(item["source_path"]),
        )
        if sha256_bytes(content) != item["sha256"]:
            raise SdlcError(
                f"产物索引哈希与真实文件不一致：{item['source_path']}。",
                exit_code=1,
            )
    return validated


__all__ = [
    "ARTIFACT_INDEX_PATH",
    "ARTIFACT_INDEX_RECORD_VERSION",
    "ARTIFACT_INDEX_SCHEMA",
    "artifact_index_bytes",
    "build_artifact_index_document",
    "formal_manifest_entries",
    "validate_draft_root",
    "validate_artifact_index_document",
    "validate_artifact_index_files",
]
