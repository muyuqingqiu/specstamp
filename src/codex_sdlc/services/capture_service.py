from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from codex_sdlc.core.state import append_event, derive_state, load_events
from codex_sdlc.core.structured_contract import canonical_json_text
from codex_sdlc.services.discuss_service import (
    PreparedCaptureTransition,
    PreparedIncrement,
    prepare_capture_transition,
    prepare_increment,
)


class CaptureService:
    """capture 普通业务事件的统一写入边界，命令层只负责参数和输出。"""

    def __init__(self, paths, *, source: str) -> None:
        self.paths = paths
        self.source = source

    def record_capture(self, payload: dict[str, Any], *, requirement_id: str | None) -> None:
        append_event(
            self.paths,
            event_type="capture_recorded",
            source=self.source,
            summary=f"记录捕获 {payload['capture_id']}",
            requirement_id=requirement_id,
            payload=payload,
        )

    def record_structured_increment(
        self,
        document: Mapping[str, object],
        *,
        state: Mapping[str, object] | None = None,
    ) -> PreparedIncrement:
        """在写事件前完成 CAP、DEC、哈希和精确引用的整体验证。"""

        current_state = state if state is not None else derive_state(self.paths)
        prepared = prepare_increment(
            self.paths.root,
            document,
            state=current_state,
            events=load_events(self.paths),
        )
        if prepared.duplicate:
            return prepared

        capture_record = prepared.capture
        decision_records = [deepcopy(item) for item in prepared.decisions]
        referenced_paths = {
            str(capture_record["source_reference"]["path"]),
            *(str(item["reference"]["path"]) for item in capture_record["targets"]),
            *(str(item["source_reference"]["path"]) for item in decision_records),
            *(str(item["question"]["reference"]["path"]) for item in decision_records),
        }
        capture_payload = {
            "capture_id": capture_record["capture_id"],
            "summary": capture_record["increment"],
            # 人读 CAP 文件只展示结构化事件的规范 JSON 投影，不拿它反向推导业务状态。
            "note": canonical_json_text(
                {
                    "capture": capture_record,
                    "decisions": decision_records,
                }
            ).rstrip("\n"),
            "status": capture_record["status"],
            "target_type": "requirement_increment",
            "changed_files": sorted(referenced_paths),
            "commands": [],
            "questions": [],
            "draft_id": capture_record["draft_id"],
            "linked_change_id": None,
            "file_path": f".codex-sdlc/captures/{capture_record['capture_id']}.md",
            "requirement_id": None,
            "submission_key": capture_record["submission_key"],
            "submission_sha256": capture_record["submission_sha256"],
            "structured_increment": deepcopy(capture_record),
            "decision_records": decision_records,
        }

        # 复用既有 DRAFT 原子写入边界，但 changes 为空，所以不会改需求正文、问题、决定或状态。
        from codex_sdlc.services.draft_service import DraftMutationService

        DraftMutationService(self.paths, source=self.source).mutate(
            str(capture_record["draft_id"]),
            operation="追加结构化 CAP 增量",
            changes={},
            allow_conflicts=True,
            capture=capture_payload,
        )
        return prepared

    def record_capture_transition(
        self,
        document: Mapping[str, object],
        *,
        state: Mapping[str, object] | None = None,
    ) -> PreparedCaptureTransition:
        """先绑定不可变初始记录和产物关系，再原子追加 CAP 转换事件。"""

        current_state = state if state is not None else derive_state(self.paths)
        prepared = prepare_capture_transition(
            self.paths.root,
            document,
            state=current_state,
            events=load_events(self.paths),
        )
        if prepared.duplicate:
            return prepared

        from codex_sdlc.services.draft_service import DraftMutationService

        DraftMutationService(self.paths, source=self.source).record_capture_transition(
            str(prepared.transition["draft_id"]),
            prepared.transition,
            prepared.submission,
        )
        return prepared

    def link_captures(self, *, requirement_id: str, capture_ids: list[str]) -> None:
        append_event(
            self.paths,
            event_type="capture_linked",
            source=self.source,
            summary=f"纳入需求讨论草案到 {requirement_id}",
            requirement_id=requirement_id,
            payload={"capture_ids": capture_ids, "target_type": "decision"},
        )

    def create_requirement(self, *, requirement_id: str, payload: dict[str, Any]) -> None:
        append_event(
            self.paths,
            event_type="requirement_created",
            source=self.source,
            summary=f"由 capture 转新需求 {requirement_id}",
            requirement_id=requirement_id,
            payload=payload,
        )

    def create_task(self, *, requirement_id: str, task_id: str, payload: dict[str, Any]) -> None:
        task_payload = {
            "feedback_contract_version": "feedback.v1",
            "feedback_state": "none",
            "acceptance_feedback": [],
            **payload,
        }
        append_event(
            self.paths,
            event_type="task_created",
            source=self.source,
            summary=f"由 capture 创建任务 {task_id}",
            requirement_id=requirement_id,
            task_id=task_id,
            payload=task_payload,
        )
