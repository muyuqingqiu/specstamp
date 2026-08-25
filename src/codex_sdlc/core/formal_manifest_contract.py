from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from codex_sdlc.core.artifact_index import (
    ARTIFACT_INDEX_PATH,
    FORMAL_ARCHIVE_ROOT,
    artifact_index_bytes,
    build_artifact_index_document,
    formal_manifest_entries,
    validate_artifact_index_document,
    validate_artifact_index_files,
    validate_draft_root,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.structured_contract import (
    canonical_sha256,
    sha256_bytes,
    validate_schema_document,
)


DOCUMENT_FIRST_FORMAL_SCHEMA = "formal-document-first.v3"
DOCUMENT_FIRST_PROFILE = "document-first.v1"
FORMAL_CONTRACT_VERSION = "formal.v3"


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
        raise SdlcError(f"{label}不是安全的相对路径：{value}。", exit_code=1)
    return candidate.as_posix()


def _current_review_id(
    draft: Mapping[str, object],
    *,
    state_field: str,
    stage: str,
    label: str,
) -> str:
    review_state = draft.get(state_field)
    if (
        not isinstance(review_state, Mapping)
        or review_state.get("can_advance") is not True
    ):
        raise SdlcError(f"{label}没有通过或已经失效。", exit_code=1)
    reviews = review_state.get("reviews")
    current = [
        item
        for item in reviews
        if isinstance(item, Mapping)
        and item.get("stage") == stage
        and item.get("owner_id") == draft.get("draft_id")
        and item.get("is_current") is True
        and item.get("request_status") == "completed"
        and item.get("effective_status") == "passed"
        and item.get("can_advance") is True
    ] if isinstance(reviews, list) else []
    if len(current) != 1:
        raise SdlcError(f"{label}缺少唯一有效的当前 REV。", exit_code=1)
    review_id = str(current[0].get("review_id") or "")
    if not review_id:
        raise SdlcError(f"{label}缺少 REV 编号。", exit_code=1)
    return review_id


def _require_ready_draft(
    state: Mapping[str, object],
    draft_id: str,
) -> Mapping[str, object]:
    drafts = state.get("drafts")
    draft = drafts.get(draft_id) if isinstance(drafts, Mapping) else None
    if not isinstance(draft, Mapping):
        raise SdlcError(f"正式包引用的 DRAFT 不存在：{draft_id}。", exit_code=1)
    assessment = draft.get("assessment")
    if (
        draft.get("status") != "start_ready"
        or not isinstance(assessment, Mapping)
        or assessment.get("can_start") is not True
    ):
        raise SdlcError(f"{draft_id} 不是 start_ready，不能生成文档优先正式包。", exit_code=1)
    confirmation = draft.get("_requirement_confirmation_state")
    if (
        not isinstance(confirmation, Mapping)
        or confirmation.get("status") != "ready"
        or confirmation.get("can_advance") is not True
        or not isinstance(confirmation.get("current_confirmation"), Mapping)
    ):
        raise SdlcError(f"{draft_id} 的需求确认没有通过或已经失效。", exit_code=1)
    return draft


def _state_or_current(paths, state: Mapping[str, object] | None) -> Mapping[str, object]:
    if isinstance(state, Mapping):
        return state
    # 延迟导入可以避免 state 在写投影时反向加载正式包合同。
    from codex_sdlc.core.state import derive_state

    return derive_state(paths)


def _manifest_uniqueness(manifest: object) -> list[dict[str, object]]:
    if not isinstance(manifest, list):
        raise SdlcError("formal.v3 缺少完整 artifact_manifest。", exit_code=1)
    normalized = [deepcopy(dict(item)) for item in manifest if isinstance(item, Mapping)]
    if len(normalized) != len(manifest):
        raise SdlcError("artifact_manifest 的每一项都必须是对象。", exit_code=1)
    source_paths = [
        _safe_relative_path(item.get("source_path"), label="DRAFT 来源路径")
        for item in normalized
    ]
    archive_paths = [
        _safe_relative_path(item.get("archive_path"), label="REQ 归档路径")
        for item in normalized
    ]
    artifact_ids = [str(item.get("artifact_id") or "") for item in normalized]
    if len(set(source_paths)) != len(source_paths):
        raise SdlcError("artifact_manifest 的 source_path 不能重复。", exit_code=1)
    if len(set(archive_paths)) != len(archive_paths):
        raise SdlcError("artifact_manifest 的 archive_path 不能重复。", exit_code=1)
    if len(set(artifact_ids)) != len(artifact_ids):
        raise SdlcError("artifact_manifest 的 ART 编号不能重复。", exit_code=1)
    for item, archive_path in zip(normalized, archive_paths):
        if not archive_path.startswith(f"{FORMAL_ARCHIVE_ROOT}/"):
            raise SdlcError(
                f"归档目标不在 {FORMAL_ARCHIVE_ROOT} 下：{archive_path}。",
                exit_code=1,
            )
        relations = item.get("review_relations")
        if (
            not isinstance(relations, Mapping)
            or not isinstance(relations.get("applies_to"), list)
            or not relations["applies_to"]
        ):
            raise SdlcError(
                f"清单产物缺少审核关系：{item.get('source_path', '')}。",
                exit_code=1,
            )
    if source_paths != sorted(source_paths):
        raise SdlcError("artifact_manifest 必须按 source_path 规范排序。", exit_code=1)
    return normalized


def build_document_first_formal_package(
    paths,
    draft_id: str,
    *,
    state: Mapping[str, object] | None = None,
    artifact_index: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """从当前结构化状态和索引生成清单包，不复制需求、设计或模块正文。"""

    clean_id = str(draft_id or "").strip().upper()
    # 真实项目已经有事件源时必须重新派生状态，调用方传入的对象只能作为缓存；
    # 单元合同夹具没有事件文件，才使用显式 state 构造最小受控场景。
    current_state = (
        _state_or_current(paths, None)
        if paths.events_file.is_file()
        else _state_or_current(paths, state)
    )
    draft = _require_ready_draft(current_state, clean_id)
    validate_draft_root(paths, clean_id)
    requirement_review = _current_review_id(
        draft,
        state_field="_requirement_review_state",
        stage="requirement_split",
        label="需求审核",
    )
    design_review = _current_review_id(
        draft,
        state_field="_integrated_design_review_state",
        stage="integrated_design",
        label="整体设计审核",
    )
    # 落盘索引必须逐项复核真实文件和符号链接，不能只因调用方缓存与旧索引字节
    # 相同就返回 formal 包；随后再从当前状态重建集合，防止合法但漏项的旧索引。
    index = validate_artifact_index_files(paths, clean_id)
    expected_index = build_artifact_index_document(
        paths,
        draft,
        events=(
            current_state.get("events", [])
            if isinstance(current_state.get("events"), list)
            else []
        ),
        documents={},
    )
    if canonical_sha256(index) != canonical_sha256(expected_index):
        raise SdlcError(
            "artifact-index.v1 与当前事件和真实受管产物不一致。",
            exit_code=1,
        )
    if isinstance(artifact_index, Mapping):
        supplied_index = validate_artifact_index_document(artifact_index)
        if canonical_sha256(supplied_index) != canonical_sha256(index):
            raise SdlcError(
                "调用方提供的 artifact-index.v1 不是当前真实索引。",
                exit_code=1,
            )
    index_bytes = artifact_index_bytes(index)
    return {
        "formal_contract_version": FORMAL_CONTRACT_VERSION,
        "workflow_profile": DOCUMENT_FIRST_PROFILE,
        "source_draft_id": clean_id,
        "source_revision_sha256": index["draft_revision_sha256"],
        "reviews": {
            "requirement_split": requirement_review,
            "integrated_design": design_review,
        },
        "artifact_index": {
            "source_path": ARTIFACT_INDEX_PATH,
            "archive_path": f"{FORMAL_ARCHIVE_ROOT}/{ARTIFACT_INDEX_PATH}",
            "sha256": sha256_bytes(index_bytes),
        },
        "artifact_manifest": formal_manifest_entries(index),
        "open_questions": [],
    }


def _validate_document_first(
    paths,
    package: Mapping[str, object],
    *,
    state: Mapping[str, object] | None,
) -> dict[str, object]:
    candidate = deepcopy(dict(package))
    validate_schema_document(candidate, schema_name=DOCUMENT_FIRST_FORMAL_SCHEMA)
    manifest = _manifest_uniqueness(candidate["artifact_manifest"])
    current_state = _state_or_current(paths, state)
    draft_id = str(candidate["source_draft_id"])
    draft = _require_ready_draft(current_state, draft_id)
    validate_draft_root(paths, draft_id)

    requirement_review = _current_review_id(
        draft,
        state_field="_requirement_review_state",
        stage="requirement_split",
        label="需求审核",
    )
    design_review = _current_review_id(
        draft,
        state_field="_integrated_design_review_state",
        stage="integrated_design",
        label="整体设计审核",
    )
    reviews = candidate["reviews"]
    if not isinstance(reviews, Mapping):
        raise SdlcError("formal.v3 缺少两类审核 REV。", exit_code=1)
    if reviews.get("requirement_split") != requirement_review:
        raise SdlcError("formal.v3 的需求审核 REV 不是当前有效审核。", exit_code=1)
    if reviews.get("integrated_design") != design_review:
        raise SdlcError("formal.v3 的整体设计审核 REV 不是当前有效审核。", exit_code=1)

    index = validate_artifact_index_files(paths, draft_id)
    expected_index = build_artifact_index_document(
        paths,
        draft,
        events=(
            current_state.get("events", [])
            if isinstance(current_state.get("events"), list)
            else []
        ),
        documents={},
    )
    if canonical_sha256(index) != canonical_sha256(expected_index):
        raise SdlcError("artifact-index.v1 与当前事件和真实受管产物不一致。", exit_code=1)
    if candidate["source_revision_sha256"] != index["draft_revision_sha256"]:
        raise SdlcError("formal.v3 引用的 DRAFT 修订已经过期。", exit_code=1)

    index_reference = candidate["artifact_index"]
    if not isinstance(index_reference, Mapping):
        raise SdlcError("formal.v3 缺少 artifact_index 引用。", exit_code=1)
    index_bytes = artifact_index_bytes(index)
    if index_reference.get("sha256") != sha256_bytes(index_bytes):
        raise SdlcError("formal.v3 引用的 artifact-index.v1 哈希已经过期。", exit_code=1)

    expected_manifest = formal_manifest_entries(index)
    if canonical_sha256(manifest) != canonical_sha256(expected_manifest):
        raise SdlcError(
            "artifact_manifest 与当前 include_in_formal 应归档集合不完全一致。",
            exit_code=1,
        )
    return candidate


def validate_formal_package_contract(
    paths,
    package: Mapping[str, object],
    *,
    state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """按显式 profile 分流；未带 profile 的 formal.v3 只保留历史读取语义。"""

    candidate = deepcopy(dict(package))
    version = candidate.get("formal_contract_version")
    profile = candidate.get("workflow_profile")
    if version == FORMAL_CONTRACT_VERSION and profile is None:
        return {"mode": "legacy_read_only", "package": candidate}
    if version != FORMAL_CONTRACT_VERSION or profile != DOCUMENT_FIRST_PROFILE:
        raise SdlcError("正式包版本或 workflow_profile 不受支持。", exit_code=1)
    return {
        "mode": "document-first",
        "package": _validate_document_first(paths, candidate, state=state),
    }


__all__ = [
    "DOCUMENT_FIRST_FORMAL_SCHEMA",
    "DOCUMENT_FIRST_PROFILE",
    "FORMAL_CONTRACT_VERSION",
    "build_document_first_formal_package",
    "validate_formal_package_contract",
]
