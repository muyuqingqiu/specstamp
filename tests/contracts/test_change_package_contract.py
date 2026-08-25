from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
for import_path in (SRC_ROOT, TESTS_ROOT):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from codex_sdlc.core.change_workspace import (
    BASE_VERSION_PATHS,
    INTERRUPT_AFTER_PACKAGE_EVENT_APPEND,
    INTERRUPT_AFTER_PACKAGE_PUBLISH,
    INTERRUPT_BEFORE_PACKAGE_PUBLISH,
)
from codex_sdlc.core.change_contract import _project_reference_index
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.id_allocator import allocate_stable_ids
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.reference_locator import validate_reference
from codex_sdlc.core.state import load_events
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
    sha256_file,
    validate_schema_document,
)
from codex_sdlc.services.change_service import (
    add_change_material,
    create_change_workspace,
    submit_change_package,
)
from test_change_workspace_contract import _minimal_formal_project


def _locator(path: str, digest: str) -> dict[str, object]:
    return {
        "schema_version": "reference-locator.v1",
        "path": path,
        "sha256": digest,
        "locator": {"kind": "whole_file"},
    }


def _source_ref(locator: dict[str, object]) -> dict[str, object]:
    return {"material_id": "MAT-001", "reference": locator}


def _criterion(client_key: str, owner: str, locator: dict[str, object]) -> dict[str, object]:
    return {
        "client_key": client_key,
        "owner_fr_ref": owner,
        "operation": f"执行 {client_key}",
        "expected": f"得到 {client_key}",
        "pass_standard": f"{client_key} 结果完全一致",
        "source_refs": [_source_ref(locator)],
        "relations": [],
    }


def _functional_requirement(
    client_key: str,
    acceptance: list[dict[str, object]],
    locator: dict[str, object],
) -> dict[str, object]:
    return {
        "client_key": client_key,
        "title": f"功能 {client_key}",
        "description": f"完成 {client_key}",
        "elements": ["输入", "输出"],
        "flow": ["提交", "处理"],
        "facts": ["已有正式基础"],
        "rules": ["结果必须确定"],
        "constraints": ["不能猜测"],
        "states_and_exceptions": ["失败时不写入"],
        "acceptance_criteria": acceptance,
        "global_rule_refs": [],
        "source_refs": [_source_ref(locator)],
        "material_refs": ["MAT-001"],
        "depends_on": [],
        "out_of_scope": ["不处理部署"],
        "relations": [],
    }


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(document), encoding="utf-8")


def _formal_project(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    project, requirement_dir = _minimal_formal_project(tmp_path)
    source = project / "资料.json"
    source.write_text('{"标题":"正式来源"}\n', encoding="utf-8")
    source_digest = sha256_file(source)
    locator = _locator("资料.json", source_digest)
    ac = {"id": "AC-001", **_criterion("existing-ac", "FR-001", locator)}
    fr = {"id": "FR-001", **_functional_requirement("existing-fr", [ac], locator)}
    requirement = {
        "schema_version": "requirement-current.v1",
        "requirement_id": "REQ-001",
        "source_draft_id": "DRAFT-001",
        "version": "requirement.v1",
        "is_current": True,
        "title": "订单审批",
        "background": "订单需要审批",
        "goal": "审批结果稳定",
        "scope": ["订单审批"],
        "out_of_scope": ["支付"],
        "user_scenarios": ["提交订单"],
        "global_rules": [],
        "functional_requirements": [fr],
        "open_questions": [],
    }
    design = {
        "schema_version": "design-current.v1",
        "requirement_id": "REQ-001",
        "source_draft_id": "DRAFT-001",
        "version": "design.v1",
        "is_current": True,
        "artifacts": [
            {
                "artifact_id": "PAGE-001",
                "artifact_type": "design_artifact",
                "archive_path": "资料.json",
                "sha256": source_digest,
                "document": {
                    "artifact_id": "PAGE-001",
                    "schema_version": "design-artifact.v1",
                    "type": "page",
                    "requirement_refs": ["FR-001"],
                    "global_rule_refs": [],
                    "material_refs": ["MAT-001"],
                    "depends_on": [],
                    "content": {
                        "pages": [
                            {
                                "page_id": "PG-001",
                                "name": "订单页",
                                "route": "/orders",
                                "navigation_refs": [],
                                "elements": [
                                    {
                                        "element_id": "EL-001",
                                        "name": "订单列表",
                                        "data_source_refs": [],
                                    }
                                ],
                                "states": {
                                    "initial": "等待加载",
                                    "loading": "正在加载",
                                    "empty": "暂无订单",
                                    "ready": "订单已显示",
                                    "error": "加载失败",
                                    "forbidden": "无权查看",
                                },
                                "layout": "单列布局",
                                "interactions": ["查看订单"],
                                "responsive": ["窄屏纵向排列"],
                                "ui_material_refs": [],
                            }
                        ]
                    },
                    "open_questions": [],
                },
            }
        ],
    }
    test_matrix = {
        "schema_version": "test-matrix-current.v1",
        "requirement_id": "REQ-001",
        "source_draft_id": "DRAFT-001",
        "version": "test-matrix.v1",
        "is_current": True,
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "requirement_id": "FR-001",
                **{key: value for key, value in ac.items() if key != "id"},
            }
        ],
    }
    reference_index = {
        "schema_version": "reference-index.v1",
        "requirement_id": "REQ-001",
        "entries": {
            "AC-001": locator,
            "FR-001": locator,
            "MAT-001": locator,
            "PAGE-001": locator,
        },
    }
    task_plan = {
        "schema_version": "task-plan.v2",
        "requirement_id": "REQ-001",
        "producer_run_id": "run-base",
        "input_hashes": {"formal": source_digest},
        "tasks": ["T-001"],
        "dependencies": [],
        "mapping": {"base-task": "T-001"},
    }
    documents = {
        "requirement": requirement,
        "design": design,
        "test_matrix": test_matrix,
        "reference_index": reference_index,
        "task_plan": task_plan,
    }
    for name, suffix in BASE_VERSION_PATHS.items():
        _write_json(requirement_dir / suffix, documents[name])
    result = create_change_workspace(
        build_paths(project),
        requirement_id="REQ-001",
        request_key="t032-contract",
    )
    status = json.loads((project / result.workspace_path / "status.json").read_text(encoding="utf-8"))
    return project, requirement_dir, status


def _source_contracts(project: Path, status: dict[str, object]) -> dict[str, dict[str, object]]:
    requirement_dir = project / ".codex-sdlc/requirements/REQ-001-订单审批"
    bases = {
        name: json.loads((requirement_dir / suffix).read_text(encoding="utf-8"))
        for name, suffix in BASE_VERSION_PATHS.items()
    }
    locator = bases["reference_index"]["entries"]["MAT-001"]
    new_ac = _criterion("new-ac", "@client:new-fr", locator)
    new_fr = _functional_requirement("new-fr", [new_ac], locator)
    package = {
        "schema_version": "change-package.v1",
        "requirement_id": "REQ-001",
        "change_id": "CHG-001",
        "producer_run_id": "run-t032",
        "reason": "增加订单重试功能",
        "base_versions": status["base_versions"],
        "source_refs": ["MAT-001"],
        "requirement_operations": [
            {"operation": "add", "client_key": "new-fr", "next_value": new_fr, "source_refs": ["MAT-001"]}
        ],
        "global_rule_operations": [],
        "acceptance_operations": [
            {"operation": "add", "client_key": "new-ac", "next_value": new_ac, "source_refs": ["MAT-001"]}
        ],
        "design_operations": [],
        "material_operations": [],
        "task_impacts": {"restore": [], "add": [], "close": [], "unaffected": [{"task_id": "T-001", "basis_refs": ["FR-001"]}]},
        "review_impacts": [
            {"stage": "requirement_split", "reason_refs": ["FR-001"]}
        ],
        "open_questions": [],
    }
    rewritten_package = deepcopy(package)
    rewritten_package["requirement_operations"][0]["next_value"]["acceptance_criteria"][0]["owner_fr_ref"] = "FR-002"
    rewritten_package["acceptance_operations"][0]["next_value"]["owner_fr_ref"] = "FR-002"
    package_bytes = canonical_json_text(rewritten_package).encode("utf-8")
    package_hash = sha256_bytes(package_bytes)
    package_path = f"{status['workspace_path']}/change-package.v1.json"

    projected_requirement = deepcopy(bases["requirement"])
    projected_requirement["version"] = "requirement.v2"
    projected_requirement["is_current"] = False
    formal_ac = {"id": "AC-002", **deepcopy(new_ac)}
    formal_ac["owner_fr_ref"] = "FR-002"
    formal_fr = {"id": "FR-002", **deepcopy(new_fr)}
    formal_fr["acceptance_criteria"] = [formal_ac]
    projected_requirement["functional_requirements"].append(formal_fr)

    projected_design = deepcopy(bases["design"])
    projected_design["version"] = "design.v2"
    projected_design["is_current"] = False
    projected_test = deepcopy(bases["test_matrix"])
    projected_test["version"] = "test-matrix.v2"
    projected_test["is_current"] = False
    projected_test["acceptance_criteria"].append(
        {
            "id": "AC-002",
            "requirement_id": "FR-002",
            **{key: value for key, value in formal_ac.items() if key != "id"},
        }
    )
    projected_reference = deepcopy(bases["reference_index"])
    package_locator = _locator(package_path, package_hash)
    projected_reference["entries"] = {
        **projected_reference["entries"],
        "AC-002": package_locator,
        "FR-002": package_locator,
    }
    projected_reference["entries"] = {
        key: projected_reference["entries"][key] for key in sorted(projected_reference["entries"])
    }
    projected_task = deepcopy(bases["task_plan"])
    projected_task["producer_run_id"] = "run-t032"
    projected_task["input_hashes"] = {
        **projected_task["input_hashes"],
        "change_package": canonical_sha256(rewritten_package),
        **{
            f"base_{name}": status["base_versions"][name]["sha256"]
            for name in BASE_VERSION_PATHS
        },
    }
    contents = {
        "projected-requirement.v2.json": projected_requirement,
        "projected-design.v2.json": projected_design,
        "projected-test-matrix.v2.json": projected_test,
        "projected-reference-index.v2.json": projected_reference,
        "projected-task-plan.v2.json": projected_task,
    }
    result = {"change-package.v1.json": package}
    base_name_by_file = {
        "projected-requirement.v2.json": "requirement",
        "projected-design.v2.json": "design",
        "projected-test-matrix.v2.json": "test_matrix",
        "projected-reference-index.v2.json": "reference_index",
        "projected-task-plan.v2.json": "task_plan",
    }
    for filename, content in contents.items():
        result[filename] = {
            "schema_version": filename.removesuffix(".json"),
            "requirement_id": "REQ-001",
            "change_id": "CHG-001",
            "base": status["base_versions"][base_name_by_file[filename]],
            "content": content,
            "content_sha256": canonical_sha256(content),
        }
    return result


def _write_sources(project: Path, documents: dict[str, dict[str, object]]) -> dict[str, str]:
    root = project / "输入"
    paths: dict[str, str] = {}
    for filename, document in documents.items():
        target = root / filename
        _write_json(target, document)
        paths[filename] = target.relative_to(project).as_posix()
    return paths


def _submit(project: Path, paths: dict[str, str], *, hook=None):
    return submit_change_package(
        build_paths(project),
        requirement_id="REQ-001",
        change_id="CHG-001",
        package_path=paths["change-package.v1.json"],
        projected_paths={key: value for key, value in paths.items() if key != "change-package.v1.json"},
        interruption_hook=hook,
    )


def test_six_schemas_reject_alias_extra_field_and_wrong_content_hash(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    documents = _source_contracts(project, status)
    validate_schema_document(documents["change-package.v1.json"], schema_name="change-package.v1")
    alias = deepcopy(documents["change-package.v1.json"])
    operation = alias["requirement_operations"][0]
    operation["base_sha256"] = "a" * 64
    with pytest.raises(SdlcError, match="Schema 校验失败"):
        validate_schema_document(alias, schema_name="change-package.v1")
    wrong = deepcopy(documents["projected-requirement.v2.json"])
    wrong["extra"] = True
    with pytest.raises(SdlcError, match="未知字段"):
        validate_schema_document(wrong, schema_name="projected-requirement.v2")


def test_ac_numbering_follows_fr_nested_order_not_client_key_order(tmp_path: Path) -> None:
    from codex_sdlc.core.change_contract import _allocation_objects

    project, _requirement_dir, status = _formal_project(tmp_path)
    package = _source_contracts(project, status)["change-package.v1.json"]
    locator = package["requirement_operations"][0]["next_value"]["source_refs"][0]["reference"]
    second = _criterion("a-ac", "@client:new-fr", locator)
    first = _criterion("b-ac", "@client:new-fr", locator)
    package["requirement_operations"][0]["next_value"]["acceptance_criteria"] = [first, second]
    package["acceptance_operations"] = [
        {"operation": "add", "client_key": "b-ac", "next_value": first, "source_refs": ["MAT-001"]},
        {"operation": "add", "client_key": "a-ac", "next_value": second, "source_refs": ["MAT-001"]},
    ]
    mapping = allocate_stable_ids(
        _allocation_objects(package),
        existing_ids={"FR-001", "AC-001"},
    )
    assert mapping["b-ac"] == "AC-002"
    assert mapping["a-ac"] == "AC-003"


def test_submit_allocates_fr_and_ac_once_and_keeps_formal_bases_unchanged(tmp_path: Path) -> None:
    project, requirement_dir, status = _formal_project(tmp_path)
    protected_before = {
        suffix: (requirement_dir / suffix).read_bytes() for suffix in BASE_VERSION_PATHS.values()
    }
    status_path = project / status["workspace_path"] / "status.json"
    status_before = status_path.read_bytes()
    paths = _write_sources(project, _source_contracts(project, status))

    first = _submit(project, paths)
    duplicate = _submit(project, paths)

    assert first.id_mapping == {"new-ac": "AC-002", "new-fr": "FR-002"}
    assert duplicate.duplicate is True
    assert duplicate.projected_event_id == first.projected_event_id
    events = [item for item in load_events(build_paths(project)) if item["event_type"] == "change_package_projected"]
    assert len(events) == 1
    workspace = project / status["workspace_path"]
    assert all((workspace / name).is_file() for name in first.committed_files_sha256)
    assert status_path.read_bytes() == status_before
    assert {
        suffix: (requirement_dir / suffix).read_bytes() for suffix in BASE_VERSION_PATHS.values()
    } == protected_before
    assert not (workspace / ".projection-transactions").exists()
    assert not (workspace / ".projection-staging").exists()


def test_file_material_add_uses_existing_locator_fields_without_expanding_schema(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    material_file = project / "资料补充.md"
    material_file.write_text("# 资料补充\n", encoding="utf-8")
    material = add_change_material(
        build_paths(project),
        requirement_id="REQ-001",
        change_id="CHG-001",
        material_type="requirement",
        file_path="资料补充.md",
    )
    documents = _source_contracts(project, status)
    package = documents["change-package.v1.json"]
    digest = sha256_file(material_file)
    package["material_operations"] = [
        {
            "operation": "add",
            "client_key": "new-mat",
            "source_material_id": material.material_id,
            "workspace_path": f"原始资料/{material.material_id}",
            "sha256": digest,
            "version_evidence": {"kind": "local_snapshot", "sha256": digest},
            "source_refs": [material.material_id],
        }
    ]
    rewritten_package = deepcopy(package)
    rewritten_package["requirement_operations"][0]["next_value"]["acceptance_criteria"][0]["owner_fr_ref"] = "FR-002"
    rewritten_package["acceptance_operations"][0]["next_value"]["owner_fr_ref"] = "FR-002"
    package_hash = sha256_bytes(canonical_json_text(rewritten_package).encode("utf-8"))
    reference = documents["projected-reference-index.v2.json"]
    for stable_id in ("AC-002", "FR-002"):
        reference["content"]["entries"][stable_id]["sha256"] = package_hash
    reference["content"]["entries"]["MAT-002"] = _locator(
        f"{status['workspace_path']}/原始资料/{material.material_id}",
        digest,
    )
    reference["content"]["entries"] = {
        key: reference["content"]["entries"][key]
        for key in sorted(reference["content"]["entries"])
    }
    reference["content_sha256"] = canonical_sha256(reference["content"])
    task = documents["projected-task-plan.v2.json"]
    task["content"]["input_hashes"]["change_package"] = canonical_sha256(rewritten_package)
    task["content_sha256"] = canonical_sha256(task["content"])

    result = _submit(project, _write_sources(project, documents))
    assert result.id_mapping["new-mat"] == "MAT-002"
    committed = json.loads(
        (project / status["workspace_path"] / "projected-reference-index.v2.json").read_text(encoding="utf-8")
    )
    assert set(committed["content"]["entries"]["MAT-002"]) == {
        "schema_version", "path", "sha256", "locator"
    }


def test_change_material_must_be_consumed_and_mat_relink_uses_exact_references(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    material_file = project / "待消费资料.md"
    material_file.write_text("# 待消费资料\n", encoding="utf-8")
    add_change_material(
        build_paths(project), requirement_id="REQ-001", change_id="CHG-001",
        material_type="requirement", file_path="待消费资料.md",
    )
    documents = _source_contracts(project, status)
    with pytest.raises(SdlcError, match="必须各自由一个 add 或 replace"):
        _submit(project, _write_sources(project, documents))

    clean_root = tmp_path / "重连"
    clean_root.mkdir()
    clean_project, _requirement_dir, clean_status = _formal_project(clean_root)
    relink_documents = _source_contracts(clean_project, clean_status)
    base_reference = json.loads(
        (clean_project / clean_status["base_versions"]["reference_index"]["path"]).read_text(encoding="utf-8")
    )["entries"]["MAT-001"]
    relink_documents["change-package.v1.json"]["material_operations"] = [{
        "operation": "relink", "target_id": "MAT-001",
        "base_revision_sha256": canonical_sha256(base_reference),
        "references": {}, "source_refs": ["MAT-001"],
    }]
    with pytest.raises(SdlcError, match="MAT relink.references"):
        _submit(clean_project, _write_sources(clean_project, relink_documents))


def test_deprecated_material_keeps_locator_and_persists_lifecycle(tmp_path: Path) -> None:
    """资料废弃后保留稳定编号和定位，同时把正式生命周期写入预计引用。"""

    material = tmp_path / "original/材料.md"
    material.parent.mkdir()
    material.write_text("保留原定位\n", encoding="utf-8")
    locator = _locator("original/材料.md", sha256_file(material))
    base = {
        "schema_version": "reference-index.v1",
        "requirement_id": "REQ-001",
        "entries": {"MAT-002": locator},
    }
    package = {
        "change_id": "CHG-001",
        "material_operations": [
            {
                "operation": "deprecate",
                "target_id": "MAT-002",
                "base_revision_sha256": canonical_sha256(locator),
                "reason": "当前资料由正式替代资料接替",
                "replacement_refs": ["MAT-001"],
            }
        ],
    }

    projected = _project_reference_index(
        base,
        package,
        {},
        package_relative_path=".codex-sdlc/requirements/REQ-001/changes/CHG-001/change-package.v1.json",
        package_sha256="2" * 64,
        manifest={"materials": []},
        manifest_relative_path=".codex-sdlc/requirements/REQ-001/changes/CHG-001/change-material-manifest.v1.json",
        manifest_sha256=None,
        changed_ids=set(),
    )

    deprecated = projected["entries"]["MAT-002"]
    assert {
        key: deprecated[key]
        for key in ("schema_version", "path", "sha256", "locator")
    } == locator
    assert deprecated["lifecycle"] == {
        "status": "deprecated",
        "change_id": "CHG-001",
        "reason": "当前资料由正式替代资料接替",
        "replacement_refs": ["MAT-001"],
    }
    assert validate_reference(tmp_path, deprecated).path == material


def test_reason_that_looks_like_formal_id_is_not_an_explicit_reference(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    documents = _source_contracts(project, status)
    package = documents["change-package.v1.json"]
    package["reason"] = "FR-999"
    rewritten = deepcopy(package)
    rewritten["requirement_operations"][0]["next_value"]["acceptance_criteria"][0]["owner_fr_ref"] = "FR-002"
    rewritten["acceptance_operations"][0]["next_value"]["owner_fr_ref"] = "FR-002"
    package_hash = sha256_bytes(canonical_json_text(rewritten).encode("utf-8"))
    reference = documents["projected-reference-index.v2.json"]
    for stable_id in ("AC-002", "FR-002"):
        reference["content"]["entries"][stable_id]["sha256"] = package_hash
    reference["content_sha256"] = canonical_sha256(reference["content"])
    task = documents["projected-task-plan.v2.json"]
    task["content"]["input_hashes"]["change_package"] = canonical_sha256(rewritten)
    task["content_sha256"] = canonical_sha256(task["content"])

    result = _submit(project, _write_sources(project, documents))
    assert result.duplicate is False


def test_active_task_cannot_be_closed_by_change_package(tmp_path: Path) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    paths = build_paths(project)
    events = load_events(paths)
    events.append({
        "event_id": "EVT-20260722-999998",
        "event_type": "task_created",
        "project_path": str(project),
        "requirement_id": "REQ-001",
        "task_id": "T-001",
        "created_at": "2026-07-22T20:30:00+08:00",
        "source": "t032-regression",
        "summary": "创建活动任务",
        "payload": {"title": "活动任务", "summary": "活动任务", "status": "doing"},
    })
    paths.events_file.write_text(
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in events),
        encoding="utf-8",
    )
    documents = _source_contracts(project, status)
    documents["change-package.v1.json"]["task_impacts"] = {
        "restore": [],
        "add": [],
        "close": [{"task_id": "T-001", "reason": "错误关闭", "replacement_refs": []}],
        "unaffected": [],
    }
    # close 不改变预计任务计划结构，来源内容本身仍与计算结果相同。
    with pytest.raises(SdlcError, match="未开始|活动|close"):
        _submit(project, _write_sources(project, documents))


@pytest.mark.parametrize(
    "stage",
    [
        INTERRUPT_BEFORE_PACKAGE_PUBLISH,
        INTERRUPT_AFTER_PACKAGE_PUBLISH,
        INTERRUPT_AFTER_PACKAGE_EVENT_APPEND,
    ],
)
def test_three_interruptions_retry_to_one_complete_result(tmp_path: Path, stage: str) -> None:
    project, _requirement_dir, status = _formal_project(tmp_path)
    paths = _write_sources(project, _source_contracts(project, status))

    def interrupt(current: str) -> None:
        if current == stage:
            raise SdlcError(f"测试中断：{stage}")

    with pytest.raises(SdlcError, match="测试中断"):
        _submit(project, paths, hook=interrupt)
    result = _submit(project, paths)
    workspace = project / status["workspace_path"]
    events = [item for item in load_events(build_paths(project)) if item["event_type"] == "change_package_projected"]
    assert len(events) == 1
    assert all((workspace / name).is_file() for name in result.committed_files_sha256)
    assert not (workspace / ".projection-transactions").exists()
    assert not (workspace / ".projection-staging").exists()
