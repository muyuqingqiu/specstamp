from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
from typing import Any

from codex_sdlc.core import fact_artifacts
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, project_lock
from codex_sdlc.core import review_contract
from codex_sdlc.core.structured_contract import validate_schema_document


TRUST_SCHEMA = "sdlc.fact-review-trust.v1"
REVIEW_TRUST_SCHEMA = "sdlc.review-trust-registry.v1"


def project_context_sha256(paths) -> str:
    # 项目绝对根路径与项目内私有签名密钥共同限定上下文。identity.json 会在首次 Git 体检时补字段，
    # 不能把这种正常更新误判为跨项目重放。
    return fact_artifacts.canonical_sha256({"root": str(paths.root.resolve())})


def _thread_id() -> str:
    value = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not value:
        raise SdlcError(
            "当前任务没有可验证的 CODEX_THREAD_ID，不能写入高风险事实或复核回执。请在原事实任务重新生成 facts，再由另一个任务完成独立复核。",
            exit_code=1,
        )
    return value


def _trust_dir(paths) -> Path:
    return paths.sdlc_dir / "trust" / "fact-reviews"


def _key_path(paths) -> Path:
    return _trust_dir(paths) / ".key"


def _registry_path(paths) -> Path:
    return _trust_dir(paths) / "registry.json"


def _key(paths, *, create: bool) -> bytes:
    path = _key_path(paths)
    if not path.exists():
        if not create:
            raise SdlcError("缺少本项目的事实复核信任记录，请重新冻结 facts 并完成独立复核。", exit_code=1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def _load(paths) -> dict[str, Any]:
    path = _registry_path(paths)
    if not path.exists():
        return {"schema": TRUST_SCHEMA, "fact_runs": {}, "requests": {}, "receipts": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SdlcError(f"事实复核信任记录无法读取：{exc}", exit_code=1) from exc
    if not isinstance(value, dict) or value.get("schema") != TRUST_SCHEMA:
        raise SdlcError("事实复核信任记录格式无效。", exit_code=1)
    for name in ("fact_runs", "requests", "receipts"):
        value.setdefault(name, {})
    return value


def _write(paths, registry: dict[str, Any]) -> None:
    path = _registry_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(fact_artifacts.canonical_json_text(registry), encoding="utf-8")
    # 请求、回执和消费状态必须一起落盘，不能让中断留下半份登记表。
    temporary.replace(path)


def _signed(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    body = deepcopy(payload)
    signature = hmac.new(key, fact_artifacts.canonical_json_bytes(body), hashlib.sha256).hexdigest()
    return {**body, "signature": signature}


def _valid_signed(value: object, key: bytes) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("signature"), str):
        return False
    body = {name: item for name, item in value.items() if name != "signature"}
    expected = hmac.new(key, fact_artifacts.canonical_json_bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(value["signature"], expected)


def record_fact_run(paths, *, draft_id: str, owner: str, artifact_sha256: str) -> dict[str, Any]:
    """事实写入时只信当前任务环境，输入 JSON 里的 actor/run 不能覆盖它。"""

    key = _key(paths, create=True)
    registry = _load(paths)
    record_id = secrets.token_hex(16)
    record = _signed(
        {
            "record_id": record_id,
            "draft_id": draft_id,
            "owner": owner,
            "artifact_sha256": artifact_sha256,
            "thread_id": _thread_id(),
            "project_context_sha256": project_context_sha256(paths),
        },
        key,
    )
    registry["fact_runs"][record_id] = record
    _write(paths, registry)
    return record


def create_review_request(
    paths,
    *,
    draft_id: str,
    target_sha256: str,
    fact_run_ids: list[str],
    entry_scope: str | None = None,
) -> dict[str, Any]:
    key = _key(paths, create=True)
    registry = _load(paths)
    runs = []
    for record_id in fact_run_ids:
        record = registry["fact_runs"].get(record_id)
        if not _valid_signed(record, key):
            raise SdlcError("facts 的 CLI 运行记录缺失或已损坏，请重新生成 facts。", exit_code=1)
        runs.append(record)
    clean_draft_id = str(draft_id or "FORMAL").strip().upper()
    project_context = project_context_sha256(paths)
    if {str(item.get("owner")) for item in runs} != {"requirement", "design"} or any(
        item.get("draft_id") != clean_draft_id or item.get("project_context_sha256") != project_context for item in runs
    ):
        raise SdlcError("facts 运行记录不属于当前项目、入口或同一 DRAFT。", exit_code=1)
    request_id = secrets.token_hex(16)
    request = _signed(
        {
            "request_id": request_id,
            "draft_id": clean_draft_id,
            "entry_scope": entry_scope or ("formal" if clean_draft_id == "FORMAL" else "draft"),
            "project_context_sha256": project_context,
            "target_sha256": target_sha256,
            "fact_run_ids": fact_run_ids,
            "producer_threads": sorted({str(item["thread_id"]) for item in runs}),
            "challenge": secrets.token_hex(32),
            "status": "pending",
        },
        key,
    )
    registry["requests"][request_id] = request
    _write(paths, registry)
    return request


def matching_fact_runs(paths, *, requirement_sha256: str, design_sha256: str, draft_id: str = "FORMAL") -> list[str]:
    key = _key(paths, create=False)
    registry = _load(paths)
    matched: dict[str, str] = {}
    expected = {"requirement": requirement_sha256, "design": design_sha256}
    for record_id, record in registry["fact_runs"].items():
        owner = str(record.get("owner") or "") if isinstance(record, dict) else ""
        if _valid_signed(record, key) and owner in expected and record.get("artifact_sha256") == expected[owner] and record.get("draft_id") == draft_id.strip().upper():
            matched[owner] = record_id
    if set(matched) != {"requirement", "design"}:
        raise SdlcError("没有找到当前两份 facts 的 CLI 冻结记录，请先在事实产出任务执行 facts freeze。", exit_code=1)
    return [matched["requirement"], matched["design"]]


def submit_review(paths, *, request_id: str, target_sha256: str, review_sha256: str) -> dict[str, Any]:
    key = _key(paths, create=False)
    registry = _load(paths)
    request = registry["requests"].get(request_id)
    if not _valid_signed(request, key) or request.get("status") != "pending":
        raise SdlcError("复核请求不存在、已消费或已损坏，请重新创建一次性复核请求。", exit_code=1)
    if request.get("target_sha256") != target_sha256:
        raise SdlcError("复核目标已经变化，请重新冻结 facts 并创建复核请求。", exit_code=1)
    reviewer_thread = _thread_id()
    if reviewer_thread in request.get("producer_threads", []):
        raise SdlcError("事实提取和独立复核必须由两个不同任务完成。", exit_code=1)
    receipt_id = secrets.token_hex(16)
    receipt = _signed(
        {
            "receipt_id": receipt_id,
            "request_id": request_id,
            "target_sha256": target_sha256,
            "review_sha256": review_sha256,
            "reviewer_thread_id": reviewer_thread,
            "challenge": request["challenge"],
            "consumed": False,
            "draft_id": request["draft_id"],
            "entry_scope": request["entry_scope"],
            "project_context_sha256": request["project_context_sha256"],
        },
        key,
    )
    registry["requests"][request_id] = _signed({**{k: v for k, v in request.items() if k != "signature"}, "status": "completed"}, key)
    registry["receipts"][receipt_id] = receipt
    _write(paths, registry)
    return receipt


def review_target_sha256(requirement: dict[str, Any], design: dict[str, Any], targets: dict[str, Any]) -> str:
    context_targets = {
        name: targets.get(name)
        for name in (
            "source_projection_sha256", "source_index_sha256", "requirement_source_sha256",
            "design_source_sha256", "questions_sha256", "decisions_sha256", "context_inputs_sha256",
        )
    }
    return fact_artifacts.canonical_sha256(
        {
            "targets": context_targets,
            "requirement_facts_sha256": fact_artifacts.artifact_sha256(requirement),
            "design_facts_sha256": fact_artifacts.artifact_sha256(design),
            "requirement_semantic_sha256": requirement.get("semantic_sha256"),
            "design_semantic_sha256": design.get("semantic_sha256"),
        }
    )


def trusted_receipt(
    paths,
    *,
    receipt_id: str,
    target_sha256: str,
    review_sha256: str,
    draft_id: str,
    entry_scope: str,
    allow_consumed: bool = False,
) -> dict[str, Any] | None:
    """按编号重新核对受管回执；事件或输入文件里的 trusted 字段不参与判断。"""

    try:
        key = _key(paths, create=False)
        receipt = _load(paths)["receipts"].get(receipt_id)
    except SdlcError:
        return None
    if not _valid_signed(receipt, key):
        return None
    if (
        receipt.get("target_sha256") != target_sha256
        or receipt.get("review_sha256") != review_sha256
        or receipt.get("draft_id") != draft_id.strip().upper()
        or receipt.get("entry_scope") != entry_scope
        or receipt.get("project_context_sha256") != project_context_sha256(paths)
        or (not allow_consumed and receipt.get("consumed") is not False)
    ):
        return None
    return {**deepcopy(receipt), "trusted": True}


def find_trusted_receipt(
    paths,
    *,
    target_sha256: str,
    review_sha256: str,
    draft_id: str = "FORMAL",
    entry_scope: str = "formal",
) -> dict[str, Any] | None:
    """只从项目受管登记表取回执，正式包内同名字段不会参与信任判断。"""

    try:
        key = _key(paths, create=False)
        receipts = _load(paths)["receipts"].values()
    except SdlcError:
        return None
    for receipt in receipts:
        if (
            _valid_signed(receipt, key)
            and receipt.get("target_sha256") == target_sha256
            and receipt.get("review_sha256") == review_sha256
            and receipt.get("consumed") is False
            and receipt.get("draft_id") == draft_id.strip().upper()
            and receipt.get("entry_scope") == entry_scope
            and receipt.get("project_context_sha256") == project_context_sha256(paths)
        ):
            return {**deepcopy(receipt), "trusted": True}
    return None


def consume_receipt(paths, *, receipt_id: str, requirement_id: str) -> None:
    key = _key(paths, create=False)
    registry = _load(paths)
    receipt = registry["receipts"].get(receipt_id)
    if not _valid_signed(receipt, key) or receipt.get("consumed") is not False:
        raise SdlcError("独立复核回执不存在、已损坏或已经用于正式建档。", exit_code=1)
    body = {name: value for name, value in receipt.items() if name != "signature"}
    body["consumed"] = True
    body["consumed_requirement_id"] = requirement_id
    registry["receipts"][receipt_id] = _signed(body, key)
    _write(paths, registry)


@dataclass(frozen=True)
class ReviewRequestOutcome:
    """创建请求时明确告诉调用方，是新请求、幂等重试还是复用已有通过结果。"""

    request: dict[str, Any]
    registration: dict[str, Any] | None
    reused: bool
    idempotent: bool


def _review_trust_dir(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "trust" / "reviews"


def _review_key_path(paths: ProjectPaths) -> Path:
    return _review_trust_dir(paths) / ".key"


def _review_registry_path(paths: ProjectPaths) -> Path:
    return _review_trust_dir(paths) / "registry.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _create_private_review_key(path: Path) -> bytes:
    """只在首次登记时创建项目私钥，内容不进入结果、日志和登记表。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_private_review_key(path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return key


def _read_private_review_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SdlcError("通用审核 HMAC 密钥缺失或类型不正确。", exit_code=1)
    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise SdlcError("通用审核 HMAC 密钥权限过宽。", exit_code=1)
        key = path.read_bytes()
    except OSError as exc:
        raise SdlcError("通用审核 HMAC 密钥无法读取。", exit_code=1) from exc
    if len(key) != 32:
        raise SdlcError("通用审核 HMAC 密钥长度不正确。", exit_code=1)
    return key


def _review_key(paths: ProjectPaths, *, create: bool) -> tuple[bytes, bool]:
    path = _review_key_path(paths)
    if path.exists() or path.is_symlink():
        return _read_private_review_key(path), False
    if not create:
        raise SdlcError("缺少通用审核 HMAC 密钥，可信登记不能读取或提交。", exit_code=1)
    return _create_private_review_key(path), True


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise SdlcError(f"通用审核登记包含重复字段：{name}。", exit_code=1)
        result[name] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"通用审核登记包含非标准数字：{value}。", exit_code=1)


def _empty_review_registry() -> dict[str, Any]:
    return {"schema": REVIEW_TRUST_SCHEMA, "requests": {}, "registrations": {}}


def _review_record_body(record: dict[str, Any]) -> dict[str, Any]:
    return {name: deepcopy(value) for name, value in record.items() if name != "signature"}


def _verify_review_registry(paths: ProjectPaths, registry: object, key: bytes) -> dict[str, Any]:
    validate_schema_document(registry, schema_name=REVIEW_TRUST_SCHEMA)
    assert isinstance(registry, dict)
    requests = registry["requests"]
    registrations = registry["registrations"]

    for review_id, record in requests.items():
        if not _valid_signed(record, key):
            raise SdlcError(f"审核请求登记已被改写：{review_id}。", exit_code=1)
        # 依赖快照属于请求登记的签名正文，HMAC只证明这份历史基线没有被改写。
        from codex_sdlc.core import dependency_graph

        dependency_graph.validate_dependency_snapshot(record["dependency_snapshot"])
        request = review_contract.validate_review_request(paths, record["request"], verify_files=False)
        if request["review_id"] != review_id:
            raise SdlcError(f"审核请求登记编号不一致：{review_id}。", exit_code=1)
        if record["request_sha256"] != fact_artifacts.canonical_sha256(request):
            raise SdlcError(f"审核请求登记哈希不一致：{review_id}。", exit_code=1)
        if record["input_fingerprint"] != review_contract.review_input_fingerprint(request):
            raise SdlcError(f"审核请求输入登记不一致：{review_id}。", exit_code=1)
        result_id = record["result_registration_id"]
        if (record["status"] == "pending" and result_id is not None) or (
            record["status"] == "completed" and result_id is None
        ):
            raise SdlcError(f"审核请求消费状态不完整：{review_id}。", exit_code=1)
        if result_id is not None and result_id not in registrations:
            raise SdlcError(f"审核请求找不到对应结果登记：{review_id}。", exit_code=1)

    for registration_id, registration in registrations.items():
        if not _valid_signed(registration, key):
            raise SdlcError(f"审核结果登记已被改写：{registration_id}。", exit_code=1)
        if registration["registration_id"] != registration_id:
            raise SdlcError(f"审核结果登记编号不一致：{registration_id}。", exit_code=1)
        result = registration["result"]
        request_record = requests.get(result.get("review_id")) if isinstance(result, dict) else None
        if not isinstance(request_record, dict):
            raise SdlcError(f"审核结果找不到对应请求：{registration_id}。", exit_code=1)
        request = request_record["request"]
        review_contract.validate_review_result(result, request)
        if registration["request_sha256"] != request_record["request_sha256"]:
            raise SdlcError(f"审核结果绑定的请求哈希不一致：{registration_id}。", exit_code=1)
        if registration["input_fingerprint"] != request_record["input_fingerprint"]:
            raise SdlcError(f"审核结果绑定的输入不一致：{registration_id}。", exit_code=1)
        if registration["challenge"] != request_record["challenge"]:
            raise SdlcError(f"审核结果的一次性挑战不一致：{registration_id}。", exit_code=1)
        if registration["result_sha256"] != fact_artifacts.canonical_sha256(result):
            raise SdlcError(f"审核结果登记哈希不一致：{registration_id}。", exit_code=1)
        if request_record["status"] != "completed" or request_record["result_registration_id"] != registration_id:
            raise SdlcError(f"审核请求和结果登记没有相互绑定：{registration_id}。", exit_code=1)
    return deepcopy(registry)


def _load_review_registry(paths: ProjectPaths, key: bytes) -> dict[str, Any]:
    path = _review_registry_path(paths)
    if not path.exists():
        return _empty_review_registry()
    if path.is_symlink() or not path.is_file():
        raise SdlcError("通用审核登记文件类型不正确。", exit_code=1)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("通用审核登记无法读取或不是有效 JSON。", exit_code=1) from exc
    return _verify_review_registry(paths, value, key)


def _write_review_registry(paths: ProjectPaths, registry: dict[str, Any], key: bytes) -> None:
    _verify_review_registry(paths, registry, key)
    path = _review_registry_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(fact_artifacts.canonical_json_bytes(registry) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _signed_request_record(
    request: dict[str, Any],
    dependency_snapshot: dict[str, Any],
    key: bytes,
) -> dict[str, Any]:
    return _signed(
        {
            "request": deepcopy(request),
            "request_sha256": fact_artifacts.canonical_sha256(request),
            "input_fingerprint": review_contract.review_input_fingerprint(request),
            "dependency_snapshot": deepcopy(dependency_snapshot),
            "challenge": secrets.token_hex(32),
            "status": "pending",
            "result_registration_id": None,
        },
        key,
    )


def register_trusted_review_request_locked(
    paths: ProjectPaths,
    *,
    request: dict[str, Any],
    dependency_snapshot: dict[str, Any],
    reusable_registration_id: str | None = None,
) -> ReviewRequestOutcome:
    """调用方已持有项目锁时登记请求，不能在这里再次取得同一项目锁。"""

    from codex_sdlc.core import dependency_graph

    request = review_contract.validate_review_request(paths, request)
    snapshot = dependency_graph.validate_dependency_snapshot(dependency_snapshot)
    if snapshot["target"] != {"stage": request["stage"], "owner_id": request["owner_id"]}:
        raise SdlcError("审核依赖快照与请求目标不一致。", exit_code=1)
    if snapshot["input_paths"] != request["input_paths"]:
        raise SdlcError("审核依赖快照与请求输入路径不一致。", exit_code=1)

    key, key_created = _review_key(paths, create=True)
    registry_existed = _review_registry_path(paths).exists()
    try:
        registry = _load_review_registry(paths, key)
        fingerprint = review_contract.review_input_fingerprint(request)
        existing = registry["requests"].get(request["review_id"])
        if existing is not None:
            existing_request = existing["request"]
            if existing["input_fingerprint"] != fingerprint:
                raise SdlcError("同一 review_id 已绑定其他审核请求。", exit_code=1)
            if existing["dependency_snapshot"] != snapshot:
                raise SdlcError("同一 review_id 已绑定其他审核依赖快照。", exit_code=1)
            registration = None
            if existing["result_registration_id"] is not None:
                registration = deepcopy(registry["registrations"][existing["result_registration_id"]])
                if (
                    registration["result"]["status"] == "passed"
                    and reusable_registration_id == registration["registration_id"]
                ):
                    return ReviewRequestOutcome(deepcopy(existing_request), registration, True, True)
            if existing_request["producer_run_id"] != request["producer_run_id"]:
                raise SdlcError("同一 review_id 已绑定其他生产任务。", exit_code=1)
            return ReviewRequestOutcome(deepcopy(existing_request), registration, False, True)

        reusable = None
        if reusable_registration_id:
            candidate = registry["registrations"].get(reusable_registration_id)
            if not isinstance(candidate, dict) or candidate["input_fingerprint"] != fingerprint:
                raise SdlcError("指定的审核复用登记与当前请求不一致。", exit_code=1)
            if candidate["result"]["status"] != "passed":
                raise SdlcError("指定的审核复用登记不是 passed。", exit_code=1)
            candidate_request = registry["requests"][candidate["result"]["review_id"]]["request"]
            reusable = (deepcopy(candidate_request), deepcopy(candidate))
        if reusable is not None:
            reused_request, registration = reusable
            return ReviewRequestOutcome(reused_request, registration, True, False)

        registry["requests"][request["review_id"]] = _signed_request_record(request, snapshot, key)
        _write_review_registry(paths, registry, key)
        return ReviewRequestOutcome(deepcopy(request), None, False, False)
    except Exception:
        if key_created and not registry_existed and not _review_registry_path(paths).exists():
            _review_key_path(paths).unlink(missing_ok=True)
        raise


def create_trusted_review_request(
    paths: ProjectPaths,
    *,
    review_id: str,
    stage: str,
    owner_id: str,
    input_paths: list[str | Path],
    required_checks: list[str] | tuple[str, ...] = (),
    created_at: str | None = None,
) -> ReviewRequestOutcome:
    """低层兼容入口也交给通用服务判断有效复用，避免出现第二套复用规则。"""

    from codex_sdlc.services import review_service

    result = review_service.create_review(
        paths,
        review_id=review_id,
        stage=stage,
        owner_id=owner_id,
        input_paths=input_paths,
        required_checks=required_checks,
        created_at=created_at,
    )
    registration = None
    if result["registration_id"] is not None:
        registration = trusted_review_registration(
            paths,
            registration_id=result["registration_id"],
        )
    return ReviewRequestOutcome(
        request=deepcopy(result["request"]),
        registration=registration,
        reused=result["action"] == "reused",
        idempotent=result["action"] == "idempotent",
    )


def submit_trusted_review_result_locked(
    paths: ProjectPaths,
    *,
    request_id: str,
    submission: dict[str, Any],
) -> dict[str, Any]:
    """调用方已持有项目锁时消费请求，不在这里嵌套项目锁。"""

    clean_request_id = str(request_id).strip().upper()
    key, _ = _review_key(paths, create=False)
    registry = _load_review_registry(paths, key)
    request_record = registry["requests"].get(clean_request_id)
    if not isinstance(request_record, dict):
        raise SdlcError("审核请求不存在。", exit_code=1)
    request = review_contract.validate_review_request(paths, request_record["request"])
    result = review_contract.capture_review_result(request, submission)
    result_sha256 = fact_artifacts.canonical_sha256(result)

    if request_record["status"] == "completed":
        registration = registry["registrations"].get(request_record["result_registration_id"])
        if isinstance(registration, dict) and registration["result_sha256"] == result_sha256:
            return deepcopy(registration)
        raise SdlcError("审核请求已经被其他结果消费。", exit_code=1)

    registration_id = secrets.token_hex(16)
    registration = _signed(
        {
            "registration_id": registration_id,
            "request_sha256": request_record["request_sha256"],
            "input_fingerprint": request_record["input_fingerprint"],
            "challenge": request_record["challenge"],
            "result": result,
            "result_sha256": result_sha256,
        },
        key,
    )
    completed_body = _review_record_body(request_record)
    completed_body["status"] = "completed"
    completed_body["result_registration_id"] = registration_id
    registry["requests"][clean_request_id] = _signed(completed_body, key)
    registry["registrations"][registration_id] = registration
    _write_review_registry(paths, registry, key)
    return deepcopy(registration)


def submit_trusted_review_result(
    paths: ProjectPaths,
    *,
    request_id: str,
    submission: dict[str, Any],
) -> dict[str, Any]:
    """消费一次性请求并登记结果；相同提交重试返回原登记，其他重放一律拒绝。"""

    with project_lock(paths):
        return submit_trusted_review_result_locked(
            paths,
            request_id=request_id,
            submission=submission,
        )


def load_review_registry(paths: ProjectPaths) -> dict[str, Any]:
    """读取并完整验证通用审核登记；HMAC 只用于发现登记内容是否被改写。"""

    key, _ = _review_key(paths, create=False)
    return _load_review_registry(paths, key)


def trusted_review_registration(paths: ProjectPaths, *, registration_id: str) -> dict[str, Any] | None:
    registry = load_review_registry(paths)
    registration = registry["registrations"].get(str(registration_id).strip().lower())
    return deepcopy(registration) if isinstance(registration, dict) else None


def find_reusable_passed_review(
    paths: ProjectPaths,
    *,
    stage: str,
    owner_id: str,
    input_paths: list[str | Path],
    required_checks: list[str] | tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """按完整输入集合查找当前仍有效且可推进的 passed。"""

    # 查询入口也重新读取受控文件，不能因为它只读就接受调用方自报哈希。
    input_hashes = review_contract.controlled_input_hashes(paths, input_paths)
    fingerprint = fact_artifacts.canonical_sha256(
        {
            "stage": str(stage).strip(),
            "owner_id": str(owner_id).strip().upper(),
            "input_hashes": deepcopy(input_hashes),
            "required_checks": sorted(required_checks),
        }
    )
    registry = load_review_registry(paths)
    from codex_sdlc.core import dependency_graph
    from codex_sdlc.services import review_service

    graph = dependency_graph.load_dependency_graph(paths)
    latest = review_service._latest_by_target(registry)
    for review_id, record in registry["requests"].items():
        if record["input_fingerprint"] != fingerprint:
            continue
        effective = review_service._effective_record(
            paths,
            registry,
            graph,
            review_id,
            latest_by_target=latest,
        )
        if effective["can_advance"] and effective["registration_id"]:
            return deepcopy(registry["registrations"][effective["registration_id"]])
    return None
