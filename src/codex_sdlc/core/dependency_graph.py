from __future__ import annotations

from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from codex_sdlc.core import review_contract
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import ProjectPaths, project_lock
from codex_sdlc.core.structured_contract import canonical_json_bytes, canonical_sha256


DEPENDENCY_GRAPH_SCHEMA = "sdlc.review-dependency-graph.v1"
DEPENDENCY_SNAPSHOT_SCHEMA = "sdlc.review-dependency-snapshot.v1"
_OWNER_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]{3,}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TEMP_PATTERN = re.compile(r"^\.dependency-graph\.json\.[a-z0-9_]{8}\.tmp$")


def dependency_graph_path(paths: ProjectPaths) -> Path:
    return paths.sdlc_dir / "trust" / "reviews" / "dependency-graph.json"


def _empty_graph() -> dict[str, Any]:
    body = {"schema": DEPENDENCY_GRAPH_SCHEMA, "artifacts": {}}
    return {**body, "graph_sha256": canonical_sha256(body)}


def _graph_body(graph: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": graph.get("schema"), "artifacts": deepcopy(graph.get("artifacts"))}


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SdlcError(f"审核依赖图包含重复字段：{key}。", exit_code=1)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise SdlcError(f"审核依赖图包含非标准数字：{value}。", exit_code=1)


def _normalize_target(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"stage", "owner_id"}:
        raise SdlcError("applies_to 必须只包含 stage 和 owner_id。", exit_code=1)
    stage = str(value["stage"]).strip()
    owner_id = str(value["owner_id"]).strip().upper()
    if stage not in review_contract.REVIEW_STAGES:
        raise SdlcError(f"applies_to 的审核阶段不受支持：{stage}。", exit_code=1)
    if _OWNER_ID_PATTERN.fullmatch(owner_id) is None:
        raise SdlcError(f"applies_to 的 owner_id 格式不正确：{owner_id}。", exit_code=1)
    return {"stage": stage, "owner_id": owner_id}


def _normalize_relation_structure(
    value: object,
    *,
    path: str,
    require_canonical: bool,
) -> dict[str, Any]:
    """写入和读取共用同一份纯结构合同，不在这里读取文件。"""

    if not isinstance(value, dict) or set(value) != {"applies_to", "depends_on"}:
        raise SdlcError(f"审核依赖记录关系结构不正确：{path}。", exit_code=1)
    if not isinstance(value["applies_to"], list) or not isinstance(value["depends_on"], list):
        raise SdlcError(f"审核依赖记录关系格式不正确：{path}。", exit_code=1)
    applies_to = [_normalize_target(item) for item in value["applies_to"]]
    canonical_targets = sorted(applies_to, key=lambda item: (item["stage"], item["owner_id"]))
    if len({(item["stage"], item["owner_id"]) for item in applies_to}) != len(applies_to):
        raise SdlcError(f"{path} 的 applies_to 不能重复。", exit_code=1)

    depends_on: list[str] = []
    for item in value["depends_on"]:
        if not isinstance(item, str):
            raise SdlcError(f"{path} 的 depends_on 必须是路径字符串。", exit_code=1)
        depends_on.append(review_contract.normalize_input_path(item))
    canonical_dependencies = sorted(depends_on)
    if len(set(depends_on)) != len(depends_on):
        raise SdlcError(f"{path} 的 depends_on 不能重复。", exit_code=1)
    if path in depends_on:
        raise SdlcError(f"{path} 的 depends_on 不能指向自身。", exit_code=1)
    if require_canonical and (
        applies_to != canonical_targets or depends_on != canonical_dependencies
    ):
        raise SdlcError(f"{path} 的显式依赖关系没有按规范排序。", exit_code=1)
    return {"applies_to": canonical_targets, "depends_on": canonical_dependencies}


def _normalize_record(paths: ProjectPaths, value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "applies_to", "depends_on"}:
        raise SdlcError("依赖记录必须只包含 path、applies_to 和 depends_on。", exit_code=1)
    if not isinstance(value["path"], str):
        raise SdlcError("依赖记录的 path 必须是字符串。", exit_code=1)
    path = review_contract.normalize_input_path(value["path"])
    # 先用受控读取确认调用方给的是当前项目内普通文件，不能接受词法链接。
    review_contract.controlled_input_hashes(paths, [path])
    relations = _normalize_relation_structure(
        {"applies_to": value["applies_to"], "depends_on": value["depends_on"]},
        path=path,
        require_canonical=False,
    )
    for dependency in relations["depends_on"]:
        review_contract.controlled_input_hashes(paths, [dependency])
    return {"path": path, **relations}


def _validate_graph(graph: object) -> dict[str, Any]:
    if not isinstance(graph, dict) or set(graph) != {"schema", "artifacts", "graph_sha256"}:
        raise SdlcError("审核依赖图结构不完整。", exit_code=1)
    if graph["schema"] != DEPENDENCY_GRAPH_SCHEMA or not isinstance(graph["artifacts"], dict):
        raise SdlcError("审核依赖图版本或 artifacts 格式不正确。", exit_code=1)
    if graph["graph_sha256"] != canonical_sha256(_graph_body(graph)):
        raise SdlcError("审核依赖图已被改写。", exit_code=1)
    artifact_paths = list(graph["artifacts"])
    if artifact_paths != sorted(artifact_paths):
        raise SdlcError("审核依赖图 artifacts 没有按路径排序。", exit_code=1)
    for raw_path, record in graph["artifacts"].items():
        path = review_contract.normalize_input_path(raw_path)
        if path != raw_path or not isinstance(record, dict):
            raise SdlcError("审核依赖图包含无效产物记录。", exit_code=1)
        if set(record) != {"path", "sha256", "applies_to", "depends_on"} or record["path"] != path:
            raise SdlcError(f"审核依赖记录结构或路径不一致：{path}。", exit_code=1)
        if _SHA256_PATTERN.fullmatch(str(record["sha256"])) is None:
            raise SdlcError(f"审核依赖记录缺少有效文件哈希：{path}。", exit_code=1)
        _normalize_relation_structure(
            {"applies_to": record["applies_to"], "depends_on": record["depends_on"]},
            path=path,
            require_canonical=True,
        )
    known_paths = set(graph["artifacts"])
    for path, record in graph["artifacts"].items():
        missing = set(record["depends_on"]) - known_paths
        if missing:
            raise SdlcError(
                f"{path} 的 depends_on 指向未登记文件：{', '.join(sorted(missing))}。",
                exit_code=1,
            )
    return deepcopy(graph)


def load_dependency_graph(paths: ProjectPaths) -> dict[str, Any]:
    path = dependency_graph_path(paths)
    if not path.exists():
        return _empty_graph()
    if path.is_symlink() or not path.is_file():
        raise SdlcError("审核依赖图文件类型不正确。", exit_code=1)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except SdlcError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SdlcError("审核依赖图无法读取或不是有效 JSON。", exit_code=1) from exc
    return _validate_graph(value)


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


def recover_dependency_graph_storage_locked(paths: ProjectPaths) -> None:
    """调用方已持有项目锁时，只清理由本模块创建的临时文件。"""

    directory = dependency_graph_path(paths).parent
    if not directory.exists():
        return
    removed = False
    for candidate in directory.iterdir():
        if not _TEMP_PATTERN.fullmatch(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate.unlink()
        removed = True
    if removed:
        _fsync_directory(directory)


def recover_dependency_graph_storage(paths: ProjectPaths) -> None:
    with project_lock(paths):
        recover_dependency_graph_storage_locked(paths)


def _write_graph(paths: ProjectPaths, graph: dict[str, Any]) -> None:
    _validate_graph(graph)
    path = dependency_graph_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".dependency-graph.json.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(graph) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def register_dependency_records(
    paths: ProjectPaths,
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """登记当前显式关系；历史审核基线存放在签名请求中，不会被这里覆盖。"""

    raw_records = [deepcopy(dict(item)) for item in records]
    if not raw_records:
        raise SdlcError("至少需要一条审核依赖记录。", exit_code=1)
    with project_lock(paths):
        recover_dependency_graph_storage_locked(paths)
        graph = load_dependency_graph(paths)
        normalized = [_normalize_record(paths, item) for item in raw_records]
        if len({item["path"] for item in normalized}) != len(normalized):
            raise SdlcError("同一文件不能在一次依赖登记中重复出现。", exit_code=1)
        artifacts = deepcopy(graph["artifacts"])
        for item in normalized:
            sha256 = review_contract.controlled_input_hashes(paths, [item["path"]])[item["path"]]
            artifacts[item["path"]] = {**item, "sha256": sha256}
        artifacts = {path: artifacts[path] for path in sorted(artifacts)}
        known_paths = set(artifacts)
        for path, record in artifacts.items():
            missing = set(record["depends_on"]) - known_paths
            if missing:
                raise SdlcError(
                    f"{path} 的 depends_on 指向未登记文件：{', '.join(sorted(missing))}。",
                    exit_code=1,
                )
        body = {"schema": DEPENDENCY_GRAPH_SCHEMA, "artifacts": artifacts}
        updated = {**body, "graph_sha256": canonical_sha256(body)}
        _write_graph(paths, updated)
        return deepcopy(updated)


def _snapshot_body(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": snapshot.get("schema"),
        "target": deepcopy(snapshot.get("target")),
        "input_paths": deepcopy(snapshot.get("input_paths")),
        "artifacts": deepcopy(snapshot.get("artifacts")),
    }


def validate_dependency_snapshot(snapshot: object) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema",
        "target",
        "input_paths",
        "artifacts",
        "closure_sha256",
    }:
        raise SdlcError("审核依赖快照结构不完整。", exit_code=1)
    if snapshot["schema"] != DEPENDENCY_SNAPSHOT_SCHEMA:
        raise SdlcError("审核依赖快照版本不正确。", exit_code=1)
    target = _normalize_target(snapshot["target"])
    if target != snapshot["target"]:
        raise SdlcError("审核依赖快照目标不规范。", exit_code=1)
    if not isinstance(snapshot["input_paths"], list):
        raise SdlcError("审核依赖快照输入路径格式不正确。", exit_code=1)
    input_paths = [review_contract.normalize_input_path(item) for item in snapshot["input_paths"]]
    if input_paths != sorted(input_paths) or len(set(input_paths)) != len(input_paths):
        raise SdlcError("审核依赖快照输入路径必须唯一并按路径排序。", exit_code=1)
    artifacts = snapshot["artifacts"]
    if not isinstance(artifacts, dict) or list(artifacts) != sorted(artifacts):
        raise SdlcError("审核依赖快照 artifacts 格式或顺序不正确。", exit_code=1)
    for raw_path, record in artifacts.items():
        path = review_contract.normalize_input_path(raw_path)
        if path != raw_path or not isinstance(record, dict):
            raise SdlcError("审核依赖快照包含无效产物记录。", exit_code=1)
        if set(record) != {"path", "sha256", "applies_to", "depends_on"} or record["path"] != path:
            raise SdlcError(f"审核依赖快照记录结构或路径不一致：{path}。", exit_code=1)
        if _SHA256_PATTERN.fullmatch(str(record["sha256"])) is None:
            raise SdlcError(f"审核依赖快照缺少有效文件哈希：{path}。", exit_code=1)
        _normalize_relation_structure(
            {"applies_to": record["applies_to"], "depends_on": record["depends_on"]},
            path=path,
            require_canonical=True,
        )
    known_paths = set(artifacts)
    for path, record in artifacts.items():
        missing = set(record["depends_on"]) - known_paths
        if missing:
            raise SdlcError(
                f"审核依赖快照中的 {path} 缺少依赖记录：{', '.join(sorted(missing))}。",
                exit_code=1,
            )
    if snapshot["closure_sha256"] != canonical_sha256(_snapshot_body(snapshot)):
        raise SdlcError("审核依赖快照摘要不一致。", exit_code=1)
    return deepcopy(snapshot)


def _closure_paths(request: Mapping[str, Any], graph: Mapping[str, Any]) -> set[str]:
    artifacts = graph["artifacts"]
    target = {"stage": request["stage"], "owner_id": request["owner_id"]}
    selected = {path for path in request["input_paths"] if path in artifacts}
    selected.update(
        path for path, record in artifacts.items() if target in record.get("applies_to", [])
    )
    pending = list(selected)
    while pending:
        path = pending.pop()
        for dependency in artifacts[path]["depends_on"]:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def build_dependency_snapshot(
    paths: ProjectPaths,
    request: Mapping[str, Any],
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    """调用方在项目锁内读取闭包文件并建立不可变审核基线。"""

    validated_graph = _validate_graph(graph)
    selected = _closure_paths(request, validated_graph)
    artifacts: dict[str, Any] = {}
    for path in sorted(selected):
        record = validated_graph["artifacts"][path]
        current_hash = review_contract.controlled_input_hashes(paths, [path])[path]
        artifacts[path] = {
            "path": path,
            "sha256": current_hash,
            "applies_to": deepcopy(record["applies_to"]),
            "depends_on": deepcopy(record["depends_on"]),
        }
    body = {
        "schema": DEPENDENCY_SNAPSHOT_SCHEMA,
        "target": {"stage": request["stage"], "owner_id": request["owner_id"]},
        "input_paths": list(request["input_paths"]),
        "artifacts": artifacts,
    }
    return validate_dependency_snapshot({**body, "closure_sha256": canonical_sha256(body)})


def _current_hash(paths: ProjectPaths, relative_path: str) -> str | None:
    try:
        return review_contract.controlled_input_hashes(paths, [relative_path])[relative_path]
    except SdlcError:
        return None


def review_staleness(
    paths: ProjectPaths,
    request: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    graph: Mapping[str, Any],
) -> dict[str, Any]:
    """统一按签名历史快照、真实文件和当前显式关系计算有效性。"""

    baseline = validate_dependency_snapshot(snapshot)
    current_graph = _validate_graph(graph)
    historical = baseline["artifacts"]
    changed = {
        path for path, record in historical.items() if _current_hash(paths, path) != record["sha256"]
    }
    direct_changed = {
        path
        for path, expected_hash in request.get("input_hashes", {}).items()
        if _current_hash(paths, path) != expected_hash
    }

    # 关系本身也是快照的一部分；删除、增加或改写闭包关系都必须让旧审核失效。
    try:
        current_snapshot = build_dependency_snapshot(paths, request, current_graph)
        relation_changed = {
            path
            for path in set(historical) | set(current_snapshot["artifacts"])
            if (
                historical.get(path, {}).get("applies_to"),
                historical.get(path, {}).get("depends_on"),
            )
            != (
                current_snapshot["artifacts"].get(path, {}).get("applies_to"),
                current_snapshot["artifacts"].get(path, {}).get("depends_on"),
            )
        }
    except SdlcError:
        relation_changed = set(historical) | _closure_paths(request, current_graph)

    invalid = set(changed) | set(relation_changed)
    dependency_sources: dict[str, set[str]] = {}
    updated = True
    while updated:
        updated = False
        for path, record in historical.items():
            sources = set(record["depends_on"]) & invalid
            if sources and path not in invalid:
                invalid.add(path)
                dependency_sources[path] = sources
                updated = True
    invalid.update(direct_changed)
    request_inputs = set(request.get("input_hashes", {}))
    target = {"stage": request.get("stage"), "owner_id": request.get("owner_id")}
    applies_paths = {
        path for path in invalid if path in historical and target in historical[path]["applies_to"]
    }
    affected_inputs = request_inputs & invalid

    reasons: list[dict[str, Any]] = []
    for path in sorted(direct_changed):
        reasons.append({"kind": "file_hash_changed", "path": path, "source_paths": [path]})
    for path in sorted(affected_inputs - direct_changed):
        reasons.append(
            {
                "kind": "depends_on_changed",
                "path": path,
                "source_paths": sorted(dependency_sources.get(path, changed | relation_changed)),
            }
        )
    for path in sorted(applies_paths - affected_inputs):
        kind = "dependency_relation_changed" if path in relation_changed and path not in changed else "applies_to_changed"
        reasons.append({"kind": kind, "path": path, "source_paths": [path]})
    for path in sorted(relation_changed - affected_inputs - applies_paths):
        reasons.append({"kind": "dependency_relation_changed", "path": path, "source_paths": [path]})
    return {
        "stale": bool(reasons),
        "changed_files": sorted(changed | direct_changed | relation_changed),
        "invalid_files": sorted(invalid),
        "reasons": reasons,
    }


def required_change_review_stages(package: Mapping[str, Any]) -> list[str]:
    """只按结构化操作计算最低审核范围，不从变更说明猜业务影响。"""

    required: set[str] = set()
    if any(
        isinstance(package.get(field), list) and bool(package.get(field))
        for field in (
            "requirement_operations",
            "global_rule_operations",
            "acceptance_operations",
        )
    ):
        # 需求事实变化会继续影响整体设计和任务计划，三个固定审核都要重做。
        required.update(review_contract.REVIEW_STAGES)
    if isinstance(package.get("design_operations"), list) and package.get(
        "design_operations"
    ):
        required.update({"integrated_design", "task_plan"})
    task_impacts = package.get("task_impacts")
    if isinstance(task_impacts, Mapping) and any(
        isinstance(task_impacts.get(field), list) and bool(task_impacts.get(field))
        for field in ("restore", "add", "close")
    ):
        required.add("task_plan")
    return [stage for stage in review_contract.REVIEW_STAGES if stage in required]


def prove_task_unaffected(
    task: Mapping[str, Any],
    *,
    basis_refs: Iterable[str],
    base_reference_index: Mapping[str, Any],
    projected_reference_index: Mapping[str, Any],
) -> dict[str, Any]:
    """用任务显式引用和两版定位哈希证明活动轮次不受当前 CHG 影响。"""

    declared = sorted({str(item).strip() for item in basis_refs if str(item).strip()})
    task_refs = sorted(
        {
            str(item).strip()
            for field in (
                "requirement_refs",
                "global_rule_refs",
                "acceptance_refs",
                "design_refs",
                "material_refs",
            )
            for item in (
                task.get(field) if isinstance(task.get(field), list) else []
            )
            if str(item).strip()
        }
    )
    if not task_refs:
        raise SdlcError(
            f"任务 {task.get('task_id')} 没有可核对的正式引用，不能证明 unaffected。",
            exit_code=1,
        )
    if not declared or not set(declared).issubset(task_refs):
        extra = sorted(set(declared) - set(task_refs))
        detail = "依据不能为空" if not declared else "多出 " + "、".join(extra)
        raise SdlcError(
            f"任务 {task.get('task_id')} 的 unaffected 依据不属于真实引用（{detail}）。",
            exit_code=1,
        )
    base_entries = base_reference_index.get("entries")
    projected_entries = projected_reference_index.get("entries")
    if not isinstance(base_entries, Mapping) or not isinstance(projected_entries, Mapping):
        raise SdlcError("基础或预计引用索引缺少 entries，不能证明 unaffected。", exit_code=1)
    evidence: dict[str, Any] = {}
    for reference in task_refs:
        before = base_entries.get(reference)
        after = projected_entries.get(reference)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise SdlcError(
                f"任务 {task.get('task_id')} 的 unaffected 依据无法定位：{reference}。",
                exit_code=1,
            )
        before_sha256 = canonical_sha256(before)
        after_sha256 = canonical_sha256(after)
        if before_sha256 != after_sha256:
            raise SdlcError(
                f"任务 {task.get('task_id')} 的引用 {reference} 在预计版本中已经变化，不能声明 unaffected。",
                exit_code=1,
            )
        evidence[reference] = {
            "base_sha256": before_sha256,
            "projected_sha256": after_sha256,
        }
    return {
        "task_id": str(task.get("task_id") or ""),
        "basis_refs": declared,
        "covered_task_refs": task_refs,
        "evidence": evidence,
        "proof_sha256": canonical_sha256(
            {
                "task_id": str(task.get("task_id") or ""),
                "basis_refs": declared,
                "evidence": evidence,
            }
        ),
    }


__all__ = [
    "DEPENDENCY_GRAPH_SCHEMA",
    "DEPENDENCY_SNAPSHOT_SCHEMA",
    "build_dependency_snapshot",
    "dependency_graph_path",
    "load_dependency_graph",
    "recover_dependency_graph_storage",
    "recover_dependency_graph_storage_locked",
    "register_dependency_records",
    "prove_task_unaffected",
    "required_change_review_stages",
    "review_staleness",
    "validate_dependency_snapshot",
]
