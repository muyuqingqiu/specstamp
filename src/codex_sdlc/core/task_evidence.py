from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Mapping

from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, resolve_project_path
from codex_sdlc.core.state import change_ids, derive_state, next_number, now_iso, resolve_task
from codex_sdlc.core.structured_contract import sha256_file, validate_schema_document
from codex_sdlc.core.task_run import (
    load_task_run_context,
    record_task_run_entry,
    require_active_task_run,
)


FEEDBACK_SCHEMA = "task-feedback.v1"
EVIDENCE_KINDS = {"test", "script", "screenshot", "field", "verification", "feedback"}
RESULTS = {"passed", "failed", "blocked"}
CONTRACT_REFERENCE_PATTERN = re.compile(r"^(?:FR|GR|AC|DES)-[0-9]{3,}(?:#.+)?$")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"证据 JSON 包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"证据 JSON 包含非标准数字：{value}。", exit_code=1)


def _load_json_source(path: Path, label: str) -> dict[str, object]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"{label}不是可读取的 JSON 对象。", exit_code=1) from exc
    if not isinstance(document, dict):
        raise SdlcError(f"{label}顶层必须是 JSON 对象。", exit_code=1)
    return document


def _validated_source(
    paths: ProjectPaths,
    source_file: str,
    source_sha256: str,
) -> tuple[Path, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise SdlcError("证据来源 SHA-256 必须是 64 位小写十六进制。", exit_code=1)
    source = resolve_project_path(paths.root, source_file, must_exist=True)
    if source.is_symlink() or not source.is_file():
        raise SdlcError("证据来源必须是项目内普通文件，不能使用目录或符号链接。", exit_code=1)
    actual_sha256 = sha256_file(source)
    if actual_sha256 != source_sha256:
        raise SdlcError("证据来源文件的 SHA-256 与登记值不一致。", exit_code=1)
    return source, source.relative_to(paths.root).as_posix()


def _next_evidence_id(run: Mapping[str, object]) -> str:
    used: list[int] = []
    for field in ("test_records", "feedback_records", "verification_records"):
        for record in run.get(field, []):  # type: ignore[union-attr]
            if not isinstance(record, Mapping):
                continue
            match = re.fullmatch(r"EVD-(\d{4,})", str(record.get("evidence_id") or ""))
            if match:
                used.append(int(match.group(1)))
    return f"EVD-{(max(used) if used else 0) + 1:04d}"


def _validate_verification_document(document: Mapping[str, object]) -> list[dict[str, str]]:
    environment = document.get("environment")
    if not isinstance(environment, (str, dict)) or not environment:
        raise SdlcError("人工验收证据必须记录实际验收环境。", exit_code=1)
    checks = document.get("checks")
    if not isinstance(checks, list) or not checks:
        raise SdlcError("人工验收证据必须包含完整检查项，不能只有一句结论。", exit_code=1)
    normalized: list[dict[str, str]] = []
    for index, check in enumerate(checks, start=1):
        if not isinstance(check, Mapping):
            raise SdlcError(f"人工验收第 {index} 个检查项必须是对象。", exit_code=1)
        item = str(check.get("item") or "").strip()
        expected = str(check.get("expected") or "").strip()
        actual = str(check.get("actual") or "").strip()
        result = str(check.get("result") or "").strip()
        if not item or not expected or not actual or result not in RESULTS:
            raise SdlcError(
                f"人工验收第 {index} 个检查项必须写明检查项、预期、实际结果和状态。",
                exit_code=1,
            )
        normalized.append(
            {"item": item, "expected": expected, "actual": actual, "result": result}
        )
    return normalized


def _feedback_event(
    paths: ProjectPaths,
    *,
    requirement: Mapping[str, object],
    task_id: str,
    feedback: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    state = derive_state(paths)
    change_id = next_number(change_ids(state), "CHG")
    return change_id, {
        "event_type": "change_recorded",
        "source": "sdlc-task-evidence",
        "summary": f"把任务反馈转为正式变更 {change_id}",
        "payload": {
            "change_id": change_id,
            "summary": str(feedback["content"]),
            "description": str(feedback["content"]),
            "reason": "任务运行中的用户反馈改变了需求、全局规则、验收标准或设计。",
            "status": "draft",
            "confirmation": "待确认",
            "changed_task_ids": [task_id],
            "added_tasks": [],
            "closed_task_ids": [],
            "acceptance_points": [],
            "priority": "high",
            "blocked_reason": "待确认需求变化",
            "capture_ids": [],
            "file_path": (
                f".codex-sdlc/requirements/{requirement['folder_name']}"
                f"/changes/{change_id}.md"
            ),
        },
    }


def register_task_evidence(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
    kind: str,
    source_file: str,
    source_sha256: str,
    command: str = "",
    exit_code: int | None = None,
    result: str = "",
    test_item: str = "",
) -> dict[str, object]:
    """登记当前活动轮次证据；所有校验完成前不会写运行文件。"""

    if kind not in EVIDENCE_KINDS:
        raise SdlcError(f"不支持的任务证据类型：{kind}。", exit_code=2)
    require_active_task_run(paths, requirement_id=requirement_id, task_id=task_id)
    context = load_task_run_context(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    run = context["run"]
    assert isinstance(run, Mapping)
    source, normalized_source_file = _validated_source(
        paths, source_file, source_sha256
    )
    state = derive_state(paths)
    requirement, task = resolve_task(state, requirement_id, task_id)
    evidence_id = _next_evidence_id(run)
    base_record: dict[str, object] = {
        "evidence_id": evidence_id,
        "requirement_id": requirement_id,
        "task_id": task_id,
        "run_number": int(run["run_number"]),
        "kind": kind,
        "source_file": normalized_source_file,
        "source_sha256": source_sha256,
        "recorded_at": now_iso(),
    }

    if kind == "feedback":
        feedback = _load_json_source(source, "用户反馈合同")
        validate_schema_document(feedback, schema_name=FEEDBACK_SCHEMA)
        expected_identity = {
            "requirement_id": requirement_id,
            "task_id": task_id,
            "run_number": int(run["run_number"]),
        }
        changed_identity = [
            field for field, value in expected_identity.items() if feedback.get(field) != value
        ]
        if changed_identity:
            raise SdlcError(
                "用户反馈合同不属于当前活动轮次：" + "、".join(changed_identity) + "。",
                exit_code=1,
            )
        record = {
            **base_record,
            "feedback_id": str(feedback["feedback_id"]),
            "document": deepcopy(feedback),
            "handling": "retained",
        }
        event = None
        status = "active"
        if feedback.get("changes_contract") is True:
            affected_refs = [str(item) for item in feedback.get("affected_refs", [])]
            if not any(CONTRACT_REFERENCE_PATTERN.fullmatch(item) for item in affected_refs):
                raise SdlcError("改变正式合同的反馈必须明确引用 FR、GR、AC 或设计编号。", exit_code=1)
            change_id, event = _feedback_event(
                paths,
                requirement=requirement,
                task_id=task_id,
                feedback=feedback,
            )
            record["handling"] = "formal_change"
            record["change_id"] = change_id
            status = "stale"
        record_task_run_entry(
            paths,
            requirement_id=requirement_id,
            task_id=task_id,
            field="feedback_records",
            record=record,
            status=status,
            event=event,
        )
        return record

    if not command.strip():
        raise SdlcError("任务证据必须保存实际执行命令或人工操作名称。", exit_code=1)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise SdlcError("任务证据必须保存整数退出码。", exit_code=1)
    if result not in RESULTS:
        raise SdlcError("任务证据结果必须是 passed、failed 或 blocked。", exit_code=1)
    record = {
        **base_record,
        "command": command,
        "exit_code": exit_code,
        "result": result,
    }
    target_field = "test_records"
    if kind == "verification":
        document = _load_json_source(source, "人工验收证据")
        checks = _validate_verification_document(document)
        if result == "passed" and any(check["result"] != "passed" for check in checks):
            raise SdlcError("人工验收检查项没有全部通过，不能把总结果写成 passed。", exit_code=1)
        record["environment"] = deepcopy(document["environment"])
        record["checks"] = checks
        record["summary"] = str(document.get("summary") or "人工验收记录完整")
        target_field = "verification_records"
    else:
        clean_test_item = test_item.strip()
        if kind == "test":
            if not clean_test_item:
                raise SdlcError("自动测试证据必须绑定任务合同中的测试项。", exit_code=1)
            required_items = [str(item) for item in task.get("test_items", [])]
            if clean_test_item not in required_items:
                raise SdlcError("自动测试证据引用了当前任务合同之外的测试项。", exit_code=1)
        if clean_test_item:
            record["test_item"] = clean_test_item

    record_task_run_entry(
        paths,
        requirement_id=requirement_id,
        task_id=task_id,
        field=target_field,
        record=record,
    )
    return record


def _revalidate_record_source(paths: ProjectPaths, record: Mapping[str, object]) -> None:
    source_file = str(record.get("source_file") or "")
    source_sha256 = str(record.get("source_sha256") or "")
    _validated_source(paths, source_file, source_sha256)


def validate_completion_evidence(
    paths: ProjectPaths,
    *,
    task: Mapping[str, object],
    run: Mapping[str, object],
) -> dict[str, object]:
    """完成和需求回归共用同一份证据判断，避免两个入口给出不同结果。"""

    test_records = run.get("test_records")
    feedback_records = run.get("feedback_records")
    verification_records = run.get("verification_records")
    if not isinstance(test_records, list) or not isinstance(feedback_records, list) or not isinstance(verification_records, list):
        raise SdlcError("任务运行轮次的证据记录不完整。", exit_code=1)
    for record in [*test_records, *feedback_records, *verification_records]:
        if not isinstance(record, Mapping):
            raise SdlcError("任务运行轮次包含无效证据记录。", exit_code=1)
        _revalidate_record_source(paths, record)

    failed_tests = [
        record
        for record in test_records
        if isinstance(record, Mapping)
        and (
            record.get("kind") in {"test", "script"}
            and (record.get("result") != "passed" or record.get("exit_code") != 0)
        )
    ]
    if failed_tests:
        raise SdlcError("当前活动轮次仍有失败测试，不能完成任务。", exit_code=1)
    required_tests = [str(item) for item in task.get("test_items", [])]
    passed_test_items = {
        str(record.get("test_item"))
        for record in test_records
        if isinstance(record, Mapping)
        and record.get("kind") == "test"
        and record.get("result") == "passed"
        and record.get("exit_code") == 0
    }
    missing_tests = [item for item in required_tests if item not in passed_test_items]
    if missing_tests:
        raise SdlcError(
            "规定测试还没有全部通过：" + "；".join(missing_tests) + "。",
            exit_code=1,
        )

    required_manual = [str(item) for item in task.get("manual_checks", [])]
    passed_manual = {
        str(check.get("item"))
        for record in verification_records
        if isinstance(record, Mapping)
        and record.get("result") == "passed"
        and record.get("exit_code") == 0
        for check in record.get("checks", [])  # type: ignore[union-attr]
        if isinstance(check, Mapping) and check.get("result") == "passed"
    }
    missing_manual = [item for item in required_manual if item not in passed_manual]
    if missing_manual:
        raise SdlcError(
            "人工验收还没有完整覆盖：" + "；".join(missing_manual) + "。",
            exit_code=1,
        )
    for record in feedback_records:
        if not isinstance(record, Mapping):
            continue
        document = record.get("document")
        validate_schema_document(document, schema_name=FEEDBACK_SCHEMA)
        if isinstance(document, Mapping) and document.get("changes_contract") is True:
            raise SdlcError("当前轮次包含改变需求或设计的反馈，必须先处理正式变更。", exit_code=1)

    commands = [
        str(record.get("command"))
        for record in [*test_records, *verification_records]
        if isinstance(record, Mapping) and str(record.get("command") or "")
    ]
    return {
        "test_count": len(test_records),
        "feedback_count": len(feedback_records),
        "verification_count": len(verification_records),
        "commands": list(dict.fromkeys(commands)),
        "verification_records": deepcopy(verification_records),
    }


def read_completed_task_evidence(
    paths: ProjectPaths,
    *,
    requirement_id: str,
    task_id: str,
) -> dict[str, object]:
    """需求级回归只读取已关闭轮次的原始证据，不重新猜测任务结果。"""

    context = load_task_run_context(
        paths, requirement_id=requirement_id, task_id=task_id
    )
    run = context["run"]
    current = context["current"]
    assert isinstance(run, Mapping)
    assert isinstance(current, Mapping)
    if run.get("status") != "closed" or current.get("status") != "closed":
        raise SdlcError("任务运行轮次尚未完整关闭，不能用于需求回归。", exit_code=1)
    state = derive_state(paths)
    _requirement, task = resolve_task(state, requirement_id, task_id)
    if task.get("status") not in {"done", "closed"}:
        raise SdlcError("任务状态与已关闭运行轮次不一致，不能用于需求回归。", exit_code=1)
    summary = validate_completion_evidence(paths, task=task, run=run)
    return {**summary, "run_number": int(run["run_number"]), "status": "passed"}


__all__ = [
    "FEEDBACK_SCHEMA",
    "read_completed_task_evidence",
    "register_task_evidence",
    "validate_completion_evidence",
]
