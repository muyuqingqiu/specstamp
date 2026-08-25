from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_sdlc.core.artifact_index import (
    ARTIFACT_INDEX_PATH,
    artifact_index_bytes,
    build_artifact_index_document,
    formal_manifest_entries,
    validate_artifact_index_document,
)
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.formal_manifest_contract import (
    build_document_first_formal_package,
    validate_formal_package_contract,
)
from codex_sdlc.core.project import ProjectPaths
from codex_sdlc.core.state import derive_state, refresh_materialized_state
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    canonical_sha256,
    sha256_bytes,
)
from test_cli_v1 import run_cli
from test_design_artifact_contract import (
    _artifact,
    _import_artifact,
    _write_artifact,
)
from test_design_plan_contract import (
    _import as _import_plan,
    _module,
    _plan,
    _write_plan,
)
from test_design_reference_contract import (
    create_confirmed_design_project,
    import_reference,
    write_design_reference,
)
from test_design_summary_contract import (
    _import_summary,
    _summary,
    _write_summary,
)
from test_integrated_design_review_flow import (
    _create as _create_design_review,
    _project_with_summary,
    _submit as _submit_design_review,
)


def _append_unique_extra(package: dict[str, object]) -> None:
    extra = deepcopy(package["artifact_manifest"][0])
    extra.update(
        {
            "artifact_id": "ART-999999999999999",
            "business_id": "MAT-999",
            "source_path": "原始资料/MAT-999_额外资料.md",
            "archive_path": "original/原始资料/MAT-999_额外资料.md",
        }
    )
    package["artifact_manifest"].append(extra)


def _ready_workspace(root: Path) -> tuple[ProjectPaths, dict[str, object], dict[str, object]]:
    paths = ProjectPaths(root)
    draft_dir = paths.draft_dir("DRAFT-001")
    for relative in ("原始资料", "需求", "设计", "质检", ".staging"):
        (draft_dir / relative).mkdir(parents=True, exist_ok=True)

    material = "# 需求原文\n\n导出筛选后的订单。\n".encode("utf-8")
    material_path = draft_dir / "原始资料/MAT-001_需求说明.md"
    material_path.write_bytes(material)
    split = b'{"schema_version":"requirement-split.v1"}'
    summary = b'{"schema_version":"design-summary.v1"}'
    documents = {
        "需求/requirement-split.v1.json": split,
        "需求/需求拆分.md": "# 阅读投影\n".encode("utf-8"),
        "设计/design-summary.v1.json": summary,
        "设计/总体设计说明.md": "# 总体设计阅读投影\n".encode("utf-8"),
    }
    for source_path, content in documents.items():
        target = draft_dir / source_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    review_inputs: dict[str, str] = {}
    for review_id, stage, schema, file_name in (
        (
            "REV-001",
            "requirement_split",
            "sdlc.requirement-review-input.v1",
            "需求审核输入.json",
        ),
        (
            "REV-002",
            "integrated_design",
            "sdlc.integrated-design-review-input.v1",
            "整体设计审核输入.json",
        ),
    ):
        body = {
            "schema": schema,
            "stage": stage,
            "owner_id": "DRAFT-001",
            "review_id": review_id,
        }
        content = canonical_json_text(
            {**body, "snapshot_sha256": canonical_sha256(body)}
        ).encode("utf-8")
        relative = f"质检/{file_name}"
        (draft_dir / relative).write_bytes(content)
        project_relative = (
            paths.draft_dir("DRAFT-001").relative_to(paths.root) / relative
        ).as_posix()
        review_inputs[review_id] = project_relative
        documents[relative] = content
    # 状态、缓存和暂存都是真实文件，用来证明索引不会把目录扫描结果当成业务清单。
    (draft_dir / "status.json").write_text('{"status":"start_ready"}', encoding="utf-8")
    (draft_dir / ".staging/temporary.json").write_text("{}", encoding="utf-8")
    (draft_dir / "model-review.json").write_text("{}", encoding="utf-8")

    draft: dict[str, object] = {
        "draft_id": "DRAFT-001",
        "status": "start_ready",
        "assessment": {"can_start": True},
        "materials": [
            {
                "material_id": "MAT-001",
                "source_kind": "file",
                "stored_path": "原始资料/MAT-001_需求说明.md",
                "sha256": sha256_bytes(material),
                "status": "active",
            }
        ],
        "artifact_records": [],
        "requirement_split": {"schema_version": "requirement-split.v1"},
        "requirement_confirmations": [],
        "design_references": [],
        "design_artifacts": [],
        "design_summaries": [],
        "_requirement_confirmation_state": {
            "status": "ready",
            "can_advance": True,
            "current_confirmation": {"confirmation_id": "RCF-001"},
        },
        "_requirement_review_state": {
            "status": "ready",
            "can_advance": True,
            "reviews": [
                {
                    "review_id": "REV-001",
                    "stage": "requirement_split",
                    "owner_id": "DRAFT-001",
                    "is_current": True,
                    "effective_status": "passed",
                    "can_advance": True,
                    "request_status": "completed",
                    "input_hashes": {
                        review_inputs["REV-001"]: sha256_bytes(
                            documents["质检/需求审核输入.json"]
                        )
                    },
                }
            ],
        },
        "_integrated_design_review_state": {
            "status": "ready",
            "can_advance": True,
            "reviews": [
                {
                    "review_id": "REV-002",
                    "stage": "integrated_design",
                    "owner_id": "DRAFT-001",
                    "is_current": True,
                    "effective_status": "passed",
                    "can_advance": True,
                    "request_status": "completed",
                    "input_hashes": {
                        review_inputs["REV-002"]: sha256_bytes(
                            documents["质检/整体设计审核输入.json"]
                        )
                    },
                }
            ],
        },
    }
    index = build_artifact_index_document(
        paths,
        draft,
        events=[],
        documents=documents,
    )
    (draft_dir / ARTIFACT_INDEX_PATH).write_bytes(artifact_index_bytes(index))
    state = {
        "drafts": {"DRAFT-001": draft},
        "events": [],
        "requirements": {},
    }
    return paths, state, index


def _formal_generation_evidence(
    paths: ProjectPaths,
    state: dict[str, object],
) -> dict[str, object]:
    """把所有可观察写入放进一个快照，确保拒绝测试不只检查单个输出文件。"""

    requirement_ids = sorted(
        str(key)
        for key in state.get("requirements", {})
        if str(key).startswith("REQ-")
    )
    next_number = (
        max(int(item.split("-", 1)[1]) for item in requirement_ids) + 1
        if requirement_ids
        else 1
    )
    return {
        "files": {
            path.relative_to(paths.sdlc_dir).as_posix(): path.read_bytes()
            for path in paths.sdlc_dir.rglob("*")
            if path.is_file()
        },
        "next_requirement_id": f"REQ-{next_number:03d}",
        "draft": deepcopy(state["drafts"]["DRAFT-001"]),
        "requirements_dir_exists": paths.requirements_dir.exists(),
        "staging_files": sorted(
            path.relative_to(paths.draft_staging_dir("DRAFT-001")).as_posix()
            for path in paths.draft_staging_dir("DRAFT-001").rglob("*")
            if path.is_file()
        ),
    }


def test_revision_uses_only_current_formal_inputs_and_can_rebuild() -> None:
    with TemporaryDirectory(prefix="t015-manifest-") as directory:
        root = Path(directory)
        paths, state, first = _ready_workspace(root)
        draft = state["drafts"]["DRAFT-001"]
        draft_dir = paths.draft_dir("DRAFT-001")

        artifact_ids = {
            item["source_path"]: item["artifact_id"]
            for item in first["artifacts"]
        }
        assert {
            item["source_path"]
            for item in first["artifacts"]
            if item["include_in_formal"]
        } == {
            "原始资料/MAT-001_需求说明.md",
            "需求/requirement-split.v1.json",
            "设计/design-summary.v1.json",
            "质检/需求审核输入.json",
            "质检/整体设计审核输入.json",
        }
        assert not {
            "status.json",
            "model-review.json",
            ".staging/temporary.json",
            ARTIFACT_INDEX_PATH,
        } & {item["source_path"] for item in first["artifacts"]}

        (draft_dir / "status.json").write_text('{"status":"changed"}', encoding="utf-8")
        (draft_dir / ".staging/temporary.json").write_text('{"changed":true}', encoding="utf-8")
        (draft_dir / "需求/需求拆分.md").write_text("# 展示改写\n", encoding="utf-8")
        second = build_artifact_index_document(
            paths,
            draft,
            events=[],
            documents={},
        )
        assert second["draft_revision_sha256"] == first["draft_revision_sha256"]
        assert {
            item["source_path"]: item["artifact_id"]
            for item in second["artifacts"]
        } == artifact_ids

        split_path = draft_dir / "需求/requirement-split.v1.json"
        split_path.write_text(
            '{"schema_version":"requirement-split.v1","changed":true}',
            encoding="utf-8",
        )
        third = build_artifact_index_document(paths, draft, events=[], documents={})
        assert third["draft_revision_sha256"] != first["draft_revision_sha256"]

        paths.draft_artifact_index_file("DRAFT-001").unlink()
        paths.draft_artifact_index_file("DRAFT-001").write_bytes(
            artifact_index_bytes(third)
        )
        rebuilt = json.loads(
            paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8")
        )
        assert rebuilt == third


@pytest.mark.parametrize(
    "mutate",
    [
        lambda package: package["artifact_manifest"].pop(),
        lambda package: package["artifact_manifest"].append(
            deepcopy(package["artifact_manifest"][0])
        ),
        _append_unique_extra,
        lambda package: package["artifact_manifest"][1].update(
            {"source_path": package["artifact_manifest"][0]["source_path"]}
        ),
        lambda package: package["artifact_manifest"][1].update(
            {"archive_path": package["artifact_manifest"][0]["archive_path"]}
        ),
        lambda package: package["artifact_manifest"][0].update(
            {"source_path": "../越界.json"}
        ),
        lambda package: package["artifact_manifest"][0].update(
            {"sha256": "0" * 64}
        ),
        lambda package: package.update({"source_revision_sha256": "0" * 64}),
        lambda package: package.update({"source_draft_id": "DRAFT-999"}),
        lambda package: package["artifact_manifest"][0].update(
            {"review_relations": {"applies_to": [], "depends_on_business_ids": []}}
        ),
    ],
)
def test_manifest_rejects_non_equivalent_or_unsafe_inputs(mutate) -> None:
    with TemporaryDirectory(prefix="t015-reject-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        package = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )
        before = {
            path.relative_to(paths.sdlc_dir).as_posix(): path.read_bytes()
            for path in paths.sdlc_dir.rglob("*")
            if path.is_file()
        }
        mutate(package)
        with pytest.raises(SdlcError):
            validate_formal_package_contract(paths, package, state=state)
        after = {
            path.relative_to(paths.sdlc_dir).as_posix(): path.read_bytes()
            for path in paths.sdlc_dir.rglob("*")
            if path.is_file()
        }
        assert after == before
        assert not paths.requirements_dir.exists()


def test_formal_manifest_projection_is_canonical_and_has_no_index_cycle() -> None:
    with TemporaryDirectory(prefix="t015-canonical-") as directory:
        paths, _state, index = _ready_workspace(Path(directory))
        manifest = formal_manifest_entries(index)
        assert manifest == sorted(manifest, key=lambda item: item["source_path"])
        assert ARTIFACT_INDEX_PATH not in {item["source_path"] for item in manifest}
        expected = canonical_sha256(
            {"draft_id": "DRAFT-001", "artifact_manifest": manifest}
        )
        assert index["draft_revision_sha256"] == expected


def test_formal_generation_rejects_review_input_drift_with_cached_inputs() -> None:
    with TemporaryDirectory(prefix="t015-generate-drift-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        target = paths.draft_dir("DRAFT-001") / "质检/整体设计审核输入.json"
        target.write_bytes(target.read_bytes() + b" ")
        before = _formal_generation_evidence(paths, state)

        with pytest.raises(SdlcError, match="哈希与真实文件不一致"):
            build_document_first_formal_package(
                paths,
                "DRAFT-001",
                state=state,
                artifact_index=index,
            )

        assert _formal_generation_evidence(paths, state) == before


def test_formal_generation_rejects_persisted_index_missing_current_artifact() -> None:
    with TemporaryDirectory(prefix="t015-generate-stale-index-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        expected = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )
        stale = deepcopy(index)
        stale["artifacts"] = [
            item
            for item in stale["artifacts"]
            if item["source_path"] != "原始资料/MAT-001_需求说明.md"
        ]
        stale["draft_revision_sha256"] = canonical_sha256(
            {
                "draft_id": "DRAFT-001",
                "artifact_manifest": formal_manifest_entries(stale),
            }
        )
        stale_body = {
            key: deepcopy(value)
            for key, value in stale.items()
            if key != "index_sha256"
        }
        stale["index_sha256"] = canonical_sha256(stale_body)
        stale = validate_artifact_index_document(stale)
        paths.draft_artifact_index_file("DRAFT-001").write_bytes(
            artifact_index_bytes(stale)
        )
        before = _formal_generation_evidence(paths, state)

        with pytest.raises(SdlcError, match="当前事件和真实受管产物"):
            build_document_first_formal_package(
                paths,
                "DRAFT-001",
                state=state,
                artifact_index=stale,
            )

        assert _formal_generation_evidence(paths, state) == before
        paths.draft_artifact_index_file("DRAFT-001").write_bytes(
            artifact_index_bytes(index)
        )
        assert build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        ) == expected


def _persisted_index(paths) -> dict[str, object]:
    document = json.loads(
        paths.draft_artifact_index_file("DRAFT-001").read_text(encoding="utf-8")
    )
    return validate_artifact_index_document(document)


def _assert_stable_artifacts(
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    previous = {
        item["source_path"]: item["artifact_id"]
        for item in before["artifacts"]
    }
    current = {
        item["source_path"]: item["artifact_id"]
        for item in after["artifacts"]
    }
    assert current.items() >= previous.items()


def test_real_stages_always_write_current_index_and_keep_art_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths, _source, _material = create_confirmed_design_project(
        tmp_path,
        monkeypatch,
        long=False,
    )
    requirement_index = _persisted_index(paths)

    assert import_reference(
        project, write_design_reference(project)
    ).returncode == 0
    assert run_cli(
        ["design-reference-confirm", "DRAFT-001", "DES-001"],
        cwd=project,
    ).returncode == 0
    (project / "AGENTS.md").write_text("# 项目规则\n", encoding="utf-8")
    (project / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n',
        encoding="utf-8",
    )
    (project / "src").mkdir(exist_ok=True)
    (project / "src/app.py").write_text(
        "# 真实代码证据用于让计划合同绑定当前项目。\nVALUE = 1\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "合同测试"],
        cwd=project,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "建立逐阶段索引夹具"],
        cwd=project,
        check=True,
    )

    modules = [
        _module("data-main", "data"),
        _module("api-main", "api", depends_on=["@client:data-main"]),
        _module("page-main", "page", depends_on=["@client:api-main"]),
        _module("component-main", "component"),
        _module("security-main", "security"),
    ]
    planned = _import_plan(project, _write_plan(project, _plan(modules)))
    assert planned.returncode == 0, planned.stderr
    plan_index = _persisted_index(paths)
    _assert_stable_artifacts(requirement_index, plan_index)
    assert plan_index["draft_revision_sha256"] != requirement_index[
        "draft_revision_sha256"
    ]

    module_documents = [
        _artifact("DATA-001", "data"),
        _artifact("API-001", "api", depends_on=["DATA-001"]),
        _artifact("PAGE-001", "page", depends_on=["API-001"]),
        _artifact("COMP-001", "component"),
        _artifact("SAFE-001", "security"),
    ]
    previous = plan_index
    for number, document in enumerate(module_documents, start=1):
        imported = _import_artifact(
            project,
            _write_artifact(project, document, f"阶段模块-{number}.json"),
        )
        assert imported.returncode == 0, imported.stderr
        current = _persisted_index(paths)
        _assert_stable_artifacts(previous, current)
        assert current["draft_revision_sha256"] != previous[
            "draft_revision_sha256"
        ]
        previous = current

    imported_summary = _import_summary(
        project,
        _write_summary(project, _summary(), "阶段总体设计.json"),
    )
    assert imported_summary.returncode == 0, imported_summary.stderr
    summary_index = _persisted_index(paths)
    _assert_stable_artifacts(previous, summary_index)
    assert summary_index["draft_revision_sha256"] != previous[
        "draft_revision_sha256"
    ]


def test_real_current_review_inputs_enter_index_manifest_and_replace_stale_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, paths = _project_with_summary(tmp_path, monkeypatch)
    passed = _create_design_review(paths, monkeypatch)
    _submit_design_review(paths, passed["request"], monkeypatch)
    refresh_materialized_state(paths)

    first = _persisted_index(paths)
    first_reviews = [
        item
        for item in first["artifacts"]
        if item["artifact_type"] in {
            "requirement_review_input",
            "integrated_design_review_input",
        }
        and item["include_in_formal"]
    ]
    assert {
        (item["artifact_type"], item["business_id"])
        for item in first_reviews
    } == {
        ("requirement_review_input", "REV-001"),
        ("integrated_design_review_input", "REV-002"),
    }
    package = build_document_first_formal_package(
        paths,
        "DRAFT-001",
        state=derive_state(paths),
        artifact_index=first,
    )
    assert build_document_first_formal_package(
        paths,
        "DRAFT-001",
    ) == package
    assert {
        (item["artifact_type"], item["business_id"])
        for item in package["artifact_manifest"]
        if item["artifact_type"].endswith("_review_input")
    } == {
        ("requirement_review_input", "REV-001"),
        ("integrated_design_review_input", "REV-002"),
    }

    review_source = next(
        item["source_path"]
        for item in first_reviews
        if item["artifact_type"] == "integrated_design_review_input"
    )
    review_target = paths.draft_dir("DRAFT-001") / review_source
    original_review_bytes = review_target.read_bytes()
    cached_state = derive_state(paths)
    review_target.write_bytes(original_review_bytes + b" ")
    before_drift_rejection = _formal_generation_evidence(paths, cached_state)
    with pytest.raises(SdlcError):
        build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=cached_state,
            artifact_index=first,
        )
    assert _formal_generation_evidence(paths, cached_state) == before_drift_rejection
    review_target.write_bytes(original_review_bytes)

    for link_kind in ("parent", "file"):
        source: Path
        target: Path
        if link_kind == "parent":
            source = paths.draft_dir("DRAFT-001") / "需求"
            target = paths.draft_dir("DRAFT-001") / "真实需求"
            source.rename(target)
            source.symlink_to(target, target_is_directory=True)
        else:
            source = (
                paths.draft_dir("DRAFT-001")
                / "需求/requirement-split.v1.json"
            )
            target = paths.draft_dir("DRAFT-001") / "需求/真实需求拆分.json"
            source.rename(target)
            source.symlink_to(target)
        try:
            before_link_rejection = _formal_generation_evidence(
                paths,
                cached_state,
            )
            with pytest.raises(SdlcError):
                build_document_first_formal_package(
                    paths,
                    "DRAFT-001",
                    state=cached_state,
                    artifact_index=first,
                )
            assert (
                _formal_generation_evidence(paths, cached_state)
                == before_link_rejection
            )
        finally:
            source.unlink()
            target.rename(source)

    revised = _summary()
    revised["common_objects"][0]["definition"]["contract"] = (
        "用户实体统一由数据模块提供，接口层不能重复定义。"
    )
    revised["affected_modules"] = ["API-001", "DATA-001"]
    changed = _import_summary(
        project,
        _write_summary(project, revised, "审核换轮次总体设计.json"),
    )
    assert changed.returncode == 0, changed.stderr
    stale_index = _persisted_index(paths)
    assert not [
        item
        for item in stale_index["artifacts"]
        if item["artifact_type"] == "integrated_design_review_input"
        and item["include_in_formal"]
    ]

    current = _create_design_review(
        paths,
        monkeypatch,
        producer="审核换轮次生产任务",
    )
    _submit_design_review(paths, current["request"], monkeypatch)
    refresh_materialized_state(paths)
    replaced = _persisted_index(paths)
    integrated = [
        item
        for item in replaced["artifacts"]
        if item["artifact_type"] == "integrated_design_review_input"
        and item["include_in_formal"]
    ]
    assert [item["business_id"] for item in integrated] == ["REV-003"]
    assert all("REV-002" != item["business_id"] for item in replaced["artifacts"])


@pytest.mark.parametrize("target_kind", ["project_inside", "project_outside"])
def test_draft_root_symlink_is_rejected_by_index_and_formal(
    target_kind: str,
) -> None:
    with TemporaryDirectory(prefix="t015-root-link-") as directory:
        root = Path(directory)
        paths, state, index = _ready_workspace(root)
        package = build_document_first_formal_package(
            paths,
            "DRAFT-001",
            state=state,
            artifact_index=index,
        )
        draft_root = paths.draft_dir("DRAFT-001")
        if target_kind == "project_inside":
            target_root = paths.drafts_dir / "真实DRAFT"
            draft_root.rename(target_root)
            draft_root.symlink_to(target_root, target_is_directory=True)
            outside_context = None
        else:
            outside_context = TemporaryDirectory(prefix="t015-outside-draft-")
            target_root = Path(outside_context.name) / "真实DRAFT"
            draft_root.rename(target_root)
            draft_root.symlink_to(target_root, target_is_directory=True)
        before = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        try:
            with pytest.raises(SdlcError, match="DRAFT 根目录不能是符号链接"):
                build_artifact_index_document(
                    paths,
                    state["drafts"]["DRAFT-001"],
                    events=[],
                    documents={},
                )
            with pytest.raises(SdlcError, match="DRAFT 根目录不能是符号链接"):
                build_document_first_formal_package(
                    paths,
                    "DRAFT-001",
                    state=state,
                    artifact_index=index,
                )
            with pytest.raises(SdlcError, match="DRAFT 根目录不能是符号链接"):
                validate_formal_package_contract(
                    paths,
                    package,
                    state=state,
                )
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            assert after == before
        finally:
            if outside_context is not None:
                outside_context.cleanup()


@pytest.mark.parametrize("link_kind", ["parent", "file"])
def test_internal_parent_or_target_symlink_remains_rejected(link_kind: str) -> None:
    with TemporaryDirectory(prefix="t015-child-link-") as directory:
        paths, state, index = _ready_workspace(Path(directory))
        draft_root = paths.draft_dir("DRAFT-001")
        if link_kind == "parent":
            source = draft_root / "需求"
            target = draft_root / "真实需求"
            source.rename(target)
            source.symlink_to(target, target_is_directory=True)
        else:
            source = draft_root / "需求/requirement-split.v1.json"
            target = draft_root / "需求/真实需求拆分.json"
            source.rename(target)
            source.symlink_to(target)
        before = _formal_generation_evidence(paths, state)
        with pytest.raises(SdlcError, match="不能经过符号链接"):
            build_artifact_index_document(
                paths,
                state["drafts"]["DRAFT-001"],
                events=[],
                documents={},
            )
        with pytest.raises(SdlcError, match="不能经过符号链接"):
            build_document_first_formal_package(
                paths,
                "DRAFT-001",
                state=state,
                artifact_index=index,
            )
        assert _formal_generation_evidence(paths, state) == before
