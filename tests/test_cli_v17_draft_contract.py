from __future__ import annotations

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

from codex_sdlc.core import atomic_import as atomic_import_module, draft_artifacts
from codex_sdlc.core.atomic_import import IMPORT_PACKAGE_SCHEMA, load_import_registry
from codex_sdlc.core.errors import SdlcError
from codex_sdlc.core.project import build_paths
from codex_sdlc.core.requirement_contract import REQUIREMENT_SPLIT_SCHEMA
from codex_sdlc.core.state import (
    derive_state,
    draft_requirement_coverage_markdown,
    draft_requirement_split_markdown,
    load_events,
)
from codex_sdlc.core.structured_contract import (
    canonical_json_text,
    contract_sha256,
    sha256_file,
)
from codex_sdlc.services.draft_service import DraftMutationService
from test_cli_v1 import SDLC_BIN, init_demo_repo, run_cli


FIXED_REQUIREMENT_FILES = (
    "requirement-split.v1.json",
    "requirement-coverage.v1.json",
    "需求拆分.md",
    "需求覆盖矩阵.md",
    "需求导入回执.json",
)


def create_draft_with_material(
    tmp_path: Path, *, draft_title: str = "课程访问需求"
) -> tuple[Path, object, dict[str, object]]:
    project = init_demo_repo(tmp_path)
    assert run_cli(["init-basic"], cwd=project).returncode == 0
    assert run_cli(["draft", "create", draft_title], cwd=project).returncode == 0
    source = project / "课程访问需求.md"
    source.write_text("用户登录后可以查看课程。\n", encoding="utf-8")
    added = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "requirement",
            "--title",
            "课程访问需求",
            "--file",
            source.name,
        ],
        cwd=project,
    )
    assert added.returncode == 0, added.stderr
    paths = build_paths(project)
    state = derive_state(paths)
    material = deepcopy(state["drafts"]["DRAFT-001"]["materials"][0])
    return project, paths, material


def start_barrier_cli(
    command: list[str], *, cwd: Path, stage: str
) -> tuple[subprocess.Popen[str], int, int]:
    """用两根继承管道让真实 CLI 在指定提交边界暂停，不依赖睡眠碰时序。"""

    ready_read, ready_write = os.pipe()
    continue_read, continue_write = os.pipe()
    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    env["CODEX_SDLC_REQUIREMENTS_BARRIER_STAGE"] = stage
    env["CODEX_SDLC_REQUIREMENTS_READY_FD"] = str(ready_write)
    env["CODEX_SDLC_REQUIREMENTS_CONTINUE_FD"] = str(continue_read)
    worker = subprocess.Popen(
        [str(SDLC_BIN), *command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(ready_write, continue_read),
    )
    os.close(ready_write)
    os.close(continue_read)
    readable, _, _ = select.select([ready_read], [], [], 30)
    if not readable:
        worker.kill()
        worker.communicate(timeout=30)
        os.close(ready_read)
        os.close(continue_write)
        raise AssertionError(f"真实 CLI 没有到达进程屏障：{stage}")
    assert os.read(ready_read, 1) == b"1"
    os.close(ready_read)
    return worker, continue_write, worker.pid


def release_barrier_cli(
    worker: subprocess.Popen[str], continue_write: int
) -> tuple[str, str]:
    os.write(continue_write, b"1")
    os.close(continue_write)
    return worker.communicate(timeout=30)


def source_reference(project: Path, material: dict[str, object]) -> dict[str, object]:
    archived = project / ".codex-sdlc" / "drafts" / "DRAFT-001" / str(material["stored_path"])
    return {
        "material_id": str(material["material_id"]),
        "reference": {
            "schema_version": "reference-locator.v1",
            "path": archived.relative_to(project).as_posix(),
            "sha256": sha256_file(archived),
            "locator": {"kind": "whole_file"},
        },
    }


def requirement_documents(
    project: Path,
    material: dict[str, object],
    *,
    suffix: str = "main",
    long_description: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    source_ref = source_reference(project, material)
    description = long_description or f"用户登录后可以查看课程，结构标识为 {suffix}。"
    split: dict[str, object] = {
        "schema_version": "requirement-split.v1",
        "draft_id": "DRAFT-001",
        "producer_run_id": os.environ.get("CODEX_THREAD_ID", "").strip() or "sdlc-draft",
        "title": f"课程访问需求 {suffix}",
        "background": "用户需要访问已经归档的课程。",
        "goal": "提供可以独立验收的课程访问结果。",
        "scope": ["登录状态下查看课程"],
        "out_of_scope": ["课程内容编辑"],
        "user_scenarios": ["用户登录后打开课程页"],
        "input_material_hashes": {str(material["material_id"]): str(material["sha256"])},
        "global_rules": [
            {
                "client_key": f"gr-{suffix}",
                "title": "统一登录状态",
                "description": "课程访问统一使用当前登录状态。",
                "type": "state",
                "applies_to": [f"@client:fr-{suffix}"],
                "source_refs": [deepcopy(source_ref)],
                "relations": [],
            }
        ],
        "functional_requirements": [
            {
                "client_key": f"fr-{suffix}",
                "title": f"查看课程 {suffix}",
                "description": description,
                "elements": ["课程标题", "课程内容"],
                "flow": ["用户登录", "打开课程页", "系统展示课程"],
                "facts": ["课程资料已经归档"],
                "rules": ["只有登录用户可以查看课程"],
                "constraints": ["不包含课程编辑"],
                "states_and_exceptions": ["登录失效时拒绝访问"],
                "acceptance_criteria": [
                    {
                        "client_key": f"ac-{suffix}",
                        "owner_fr_ref": f"@client:fr-{suffix}",
                        "operation": "使用已登录用户打开课程页",
                        "expected": "页面显示课程标题和内容",
                        "pass_standard": "标题和内容完整显示且没有访问错误",
                        "source_refs": [deepcopy(source_ref)],
                        "relations": [],
                    }
                ],
                "global_rule_refs": [f"@client:gr-{suffix}"],
                "source_refs": [deepcopy(source_ref)],
                "material_refs": [str(material["material_id"])],
                "depends_on": [],
                "out_of_scope": ["编辑课程"],
                "relations": [],
            }
        ],
        "open_questions": [],
    }
    coverage: dict[str, object] = {
        "schema_version": "requirement-coverage.v1",
        "draft_id": "DRAFT-001",
        "requirement_split_sha256": contract_sha256(
            split, schema_name=REQUIREMENT_SPLIT_SCHEMA
        ),
        "units": [
            {
                "client_key": f"src-rule-{suffix}",
                "source_ref": deepcopy(source_ref),
                "classification": "global_rule",
                "covered_by": [f"@client:gr-{suffix}"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
            {
                "client_key": f"src-requirement-{suffix}",
                "source_ref": deepcopy(source_ref),
                "classification": "requirement",
                "covered_by": [f"@client:fr-{suffix}"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
            {
                "client_key": f"src-acceptance-{suffix}",
                "source_ref": deepcopy(source_ref),
                "classification": "acceptance",
                "covered_by": [f"@client:ac-{suffix}"],
                "status": "covered",
                "reason": "",
                "decision_refs": [],
                "relations": [],
            },
        ],
    }
    return split, coverage


def write_documents(
    project: Path,
    split: dict[str, object],
    coverage: dict[str, object],
    *,
    suffix: str = "",
) -> tuple[Path, Path]:
    coverage["requirement_split_sha256"] = contract_sha256(
        split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    split_path = project / f"需求拆分{suffix}.json"
    coverage_path = project / f"需求覆盖{suffix}.json"
    split_path.write_text(json.dumps(split, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage_path.write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return split_path, coverage_path


def import_command(split_path: Path, coverage_path: Path) -> list[str]:
    return [
        "draft",
        "requirements",
        "DRAFT-001",
        "--split-file",
        split_path.name,
        "--coverage-file",
        coverage_path.name,
    ]


def import_first_real_dec_in_subprocess(project: Path) -> dict[str, object]:
    """从独立进程调用 T-002，证明普通决定文字没有占用 DEC 编号。"""

    package: dict[str, object] = {
        "schema": IMPORT_PACKAGE_SCHEMA,
        "package_key": "first-real-decision",
        "package_sha256": "0" * 64,
        "destination": ".codex-sdlc/drafts/DRAFT-001/需求/first-real-decision",
        "objects": [
            {"client_key": "decision-main", "id_prefix": "DEC", "depends_on": []}
        ],
        "files": [
            {
                "relative_path": "decision.json",
                "content": {"decision_id": "@client:decision-main"},
            }
        ],
    }
    package["package_sha256"] = contract_sha256(
        package, schema_name=IMPORT_PACKAGE_SCHEMA
    )
    package_path = project / "首个正式决定导入包.json"
    package_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    script = """
import json
import sys
from pathlib import Path
from codex_sdlc.core.atomic_import import atomic_import
from codex_sdlc.core.project import build_paths

root = Path(sys.argv[1])
package = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
print(json.dumps(atomic_import(build_paths(root), package).as_dict(), ensure_ascii=False))
""".strip()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(project), str(package_path)],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def requirement_events(paths) -> list[dict[str, object]]:
    return [
        event
        for event in load_events(paths)
        if event.get("event_type") == "structured_package_imported"
        and str(event.get("payload", {}).get("package_key") or "").startswith(
            "draft-requirements:"
        )
    ]


def fixed_requirement_paths(paths) -> list[Path]:
    directory = paths.draft_requirements_dir("DRAFT-001")
    return [directory / name for name in FIXED_REQUIREMENT_FILES]


def test_requirements_cli_imports_two_files_with_stable_ids_and_rebuildable_projections(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    long_description = "精确数字 1234567890、状态 READY、错误码 E-2048。\n" * 20000
    split, coverage = requirement_documents(
        project, material, long_description=long_description
    )
    split_path, coverage_path = write_documents(project, split, coverage)

    result = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert result.returncode == 0, result.stderr
    assert "FR-001" in result.stdout
    assert "GR-001" in result.stdout
    assert "AC-001" in result.stdout
    assert "SRC-001" in result.stdout
    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    mapping = draft["requirement_import"]["mapping"]
    assert mapping == {
        "ac-main": "AC-001",
        "fr-main": "FR-001",
        "gr-main": "GR-001",
        "src-acceptance-main": "SRC-001",
        "src-requirement-main": "SRC-002",
        "src-rule-main": "SRC-003",
    }
    formal_split = draft["requirement_split"]
    formal_coverage = draft["requirement_coverage"]
    assert formal_coverage["requirement_split_sha256"] == contract_sha256(
        formal_split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )
    assert formal_split["global_rules"][0]["applies_to"] == ["FR-001"]
    assert formal_split["functional_requirements"][0]["global_rule_refs"] == ["GR-001"]
    assert formal_split["functional_requirements"][0]["acceptance_criteria"][0][
        "owner_fr_ref"
    ] == "FR-001"
    assert {item["covered_by"][0] for item in formal_coverage["units"]} == {
        "FR-001",
        "GR-001",
        "AC-001",
    }
    assert "@client:" not in json.dumps(
        {"split": formal_split, "coverage": formal_coverage}, ensure_ascii=False
    )
    assert len(requirement_events(paths)) == 1
    registration = load_import_registry(paths)["packages"][0]
    assert len(load_import_registry(paths)["packages"]) == 1
    event = requirement_events(paths)[0]
    receipt = json.loads(
        (
            project
            / str(draft["requirement_import"]["destination"])
            / atomic_import_module.IMPORT_RECEIPT_NAME
        ).read_text(encoding="utf-8")
    )
    assert {
        draft["requirement_import"]["package_sha256"],
        registration["package_sha256"],
        event["payload"]["package_sha256"],
        receipt["package_sha256"],
    } == {draft["requirement_import"]["package_sha256"]}
    assert all(path.is_file() for path in fixed_requirement_paths(paths))
    assert long_description in (
        paths.draft_requirements_dir("DRAFT-001") / "需求拆分.md"
    ).read_text(encoding="utf-8")

    expected = {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)}
    for path in fixed_requirement_paths(paths):
        if path.suffix == ".md":
            path.write_text("被手工改坏\n", encoding="utf-8")
        elif path.name == "requirement-coverage.v1.json":
            drifted_coverage = json.loads(path.read_text(encoding="utf-8"))
            drifted_coverage["requirement_split_sha256"] = "e" * 64
            path.write_text(
                json.dumps(drifted_coverage, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        else:
            path.unlink()
    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)
    assert refreshed.returncode == 0, refreshed.stderr
    assert {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)} == expected


def test_same_requirement_package_retry_returns_same_mapping_without_new_state(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    first = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert first.returncode == 0, first.stderr
    before_events = paths.events_file.read_bytes()
    before_registry = paths.import_registry_file.read_bytes()
    before_projections = {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)}

    second = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert second.returncode == 0, second.stderr
    assert "重复提交：是" in second.stdout
    assert paths.events_file.read_bytes() == before_events
    assert paths.import_registry_file.read_bytes() == before_registry
    assert {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)} == before_projections
    assert derive_state(paths)["drafts"]["DRAFT-001"]["requirement_import"]["mapping"][
        "fr-main"
    ] == "FR-001"


def test_invalid_coverage_file_rejects_the_whole_pair_before_numbering(tmp_path: Path) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    coverage["unknown"] = True
    split_path, coverage_path = write_documents(project, split, coverage)
    before_events = paths.events_file.read_bytes()

    rejected = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert rejected.returncode != 0
    assert paths.events_file.read_bytes() == before_events
    assert not paths.import_registry_file.exists()
    assert not list(paths.draft_requirements_dir("DRAFT-001").glob("requirements-*"))

    coverage.pop("unknown")
    split_path, coverage_path = write_documents(project, split, coverage)
    accepted = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert accepted.returncode == 0, accepted.stderr
    assert "FR-001" in accepted.stdout


def test_plain_text_that_equals_formal_ids_cannot_validate_references_or_take_numbers(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(
        tmp_path, draft_title="FR-999"
    )
    decision = run_cli(
        ["draft", "decision", "DRAFT-001", "DEC-999"], cwd=project
    )
    assert decision.returncode == 0, decision.stderr
    markdown = run_cli(
        ["draft", "requirement", "DRAFT-001", "FR-999"], cwd=project
    )
    assert markdown.returncode == 0, markdown.stderr
    split, coverage = requirement_documents(project, material)
    split["title"] = "FR-999"
    split["functional_requirements"][0]["description"] = "FR-999"
    coverage["units"][0]["reason"] = "DEC-999"

    split["functional_requirements"][0]["depends_on"] = ["FR-999"]
    split_path, coverage_path = write_documents(
        project, split, coverage, suffix="-false-fr"
    )
    false_fr = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert false_fr.returncode != 0
    assert "FR-999" in false_fr.stderr and "不存在" in false_fr.stderr
    assert not requirement_events(paths)

    split["functional_requirements"][0]["depends_on"] = []
    false_decision_unit = deepcopy(coverage["units"][0])
    false_decision_unit.update(
        {
            "client_key": "src-false-decision",
            "classification": "other",
            "covered_by": [],
            "status": "excluded_by_decision",
            "reason": "DEC-999",
            "decision_refs": ["DEC-999"],
        }
    )
    coverage["units"].append(false_decision_unit)
    split_path, coverage_path = write_documents(
        project, split, coverage, suffix="-false-dec"
    )
    false_dec = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert false_dec.returncode != 0
    assert "DEC-999" in false_dec.stderr and "不存在" in false_dec.stderr
    assert not requirement_events(paths)

    coverage["units"].pop()
    split_path, coverage_path = write_documents(
        project, split, coverage, suffix="-first-real-fr"
    )
    accepted = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert accepted.returncode == 0, accepted.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["requirement_import"][
        "mapping"
    ]["fr-main"] == "FR-001"

    decision_result = import_first_real_dec_in_subprocess(project)
    assert decision_result["mapping"] == {"decision-main": "DEC-001"}


def test_input_hash_and_archived_source_drift_are_rejected_before_import(tmp_path: Path) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split["input_material_hashes"]["MAT-001"] = "f" * 64
    split_path, coverage_path = write_documents(project, split, coverage)

    hash_result = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert hash_result.returncode != 0
    assert "SHA-256 不一致" in hash_result.stderr
    assert not requirement_events(paths)

    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    archived = project / ".codex-sdlc" / "drafts" / "DRAFT-001" / str(material["stored_path"])
    archived.write_text("来源已经漂移。\n", encoding="utf-8")
    location_result = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert location_result.returncode != 0
    assert "哈希已经变化" in location_result.stderr or "sha256 不一致" in location_result.stderr
    assert not requirement_events(paths)


def test_locked_precommit_rejects_package_after_concurrent_material_replacement(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    stale_split, stale_coverage = requirement_documents(
        project, material, suffix="stale-material"
    )
    stale_paths = write_documents(
        project, stale_split, stale_coverage, suffix="-stale-material"
    )
    worker, continue_write, _ = start_barrier_cli(
        import_command(*stale_paths), cwd=project, stage="after_prevalidation"
    )

    replacement_source = project / "课程访问需求修订.md"
    replacement_source.write_text("用户登录后可以查看修订后的课程。\n", encoding="utf-8")
    replaced = run_cli(
        [
            "material",
            "DRAFT-001",
            "--type",
            "requirement",
            "--title",
            "课程访问需求修订",
            "--file",
            replacement_source.name,
            "--supersedes",
            str(material["material_id"]),
        ],
        cwd=project,
    )
    assert replaced.returncode == 0, replaced.stderr
    stdout, stderr = release_barrier_cli(worker, continue_write)

    assert worker.returncode != 0, (stdout, stderr)
    assert "当前活动文件" in stderr or "当前需求资料" in stderr
    assert not requirement_events(paths)
    assert not paths.import_registry_file.exists()
    assert not list(paths.draft_requirements_dir("DRAFT-001").glob("requirements-*"))
    assert not list(paths.import_transactions_dir.glob("*.json"))

    latest_draft = derive_state(paths)["drafts"]["DRAFT-001"]
    latest_material = next(
        item for item in latest_draft["materials"] if item["status"] != "archived"
    )
    fresh_split, fresh_coverage = requirement_documents(
        project, latest_material, suffix="fresh-material"
    )
    fresh_paths = write_documents(
        project, fresh_split, fresh_coverage, suffix="-fresh-material"
    )
    accepted = run_cli(import_command(*fresh_paths), cwd=project)
    assert accepted.returncode == 0, accepted.stderr
    assert derive_state(paths)["drafts"]["DRAFT-001"]["requirement_import"][
        "mapping"
    ]["fr-fresh-material"] == "FR-001"


def test_unresolved_coverage_is_preserved_and_blocks_later_review_without_auto_fill(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    unresolved = deepcopy(coverage["units"][0])
    unresolved.update(
        {
            "client_key": "src-needs-user",
            "classification": "other",
            "covered_by": [],
            "status": "needs_user",
            "reason": "等待用户确认课程过期后的行为",
        }
    )
    coverage["units"].append(unresolved)
    split_path, coverage_path = write_documents(project, split, coverage)

    imported = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert imported.returncode == 0, imported.stderr
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["requirement_import"]["review_blockers"] == [
        "src-needs-user:needs_user"
    ]
    assert next(
        item for item in draft["requirement_coverage"]["units"] if item["client_key"] == "src-needs-user"
    )["covered_by"] == []
    assert draft.get("review_request_id", "") == ""


def test_atomic_write_failure_leaves_no_requirement_event_file_or_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    before_events = paths.events_file.read_bytes()
    original_write = atomic_import_module._write_file_bytes
    calls = 0

    def fail_second_file(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("注入需求包写入失败")
        original_write(path, content)

    monkeypatch.setattr(atomic_import_module, "_write_file_bytes", fail_second_file)
    with pytest.raises(OSError, match="注入需求包写入失败"):
        DraftMutationService(paths, source="sdlc-draft").import_requirements(
            "DRAFT-001", split, coverage
        )

    assert paths.events_file.read_bytes() == before_events
    assert not paths.import_registry_file.exists()
    assert not list(paths.draft_requirements_dir("DRAFT-001").glob("requirements-*"))
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))

    monkeypatch.setattr(atomic_import_module, "_write_file_bytes", original_write)
    outcome = DraftMutationService(paths, source="sdlc-draft").import_requirements(
        "DRAFT-001", split, coverage
    )
    assert outcome.result.mapping["fr-main"] == "FR-001"


def test_projection_failure_rolls_back_fixed_files_and_same_package_retry_repairs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    original_replace = draft_artifacts._replace_projection

    def fail_coverage_projection(source: Path, target: Path) -> None:
        if target.name == "requirement-coverage.v1.json":
            raise OSError("注入覆盖投影提交失败")
        original_replace(source, target)

    monkeypatch.setattr(draft_artifacts, "_replace_projection", fail_coverage_projection)
    with pytest.raises(OSError, match="注入覆盖投影提交失败"):
        DraftMutationService(paths, source="sdlc-draft").import_requirements(
            "DRAFT-001", split, coverage
        )

    assert len(requirement_events(paths)) == 1
    assert len(load_import_registry(paths)["packages"]) == 1
    assert not any(path.exists() for path in fixed_requirement_paths(paths))

    monkeypatch.setattr(draft_artifacts, "_replace_projection", original_replace)
    outcome = DraftMutationService(paths, source="sdlc-draft").import_requirements(
        "DRAFT-001", split, coverage
    )
    assert outcome.result.duplicate is True
    assert outcome.result.mapping["fr-main"] == "FR-001"
    assert all(path.is_file() for path in fixed_requirement_paths(paths))


def test_source_and_formal_cross_hash_drift_block_refresh_without_overwriting_projections(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material)
    split_path, coverage_path = write_documents(project, split, coverage)
    imported = run_cli(import_command(split_path, coverage_path), cwd=project)
    assert imported.returncode == 0, imported.stderr
    before = {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)}
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    source_split = (
        project
        / str(draft["requirement_import"]["destination"])
        / "requirement-split.v1.json"
    )
    tampered = json.loads(source_split.read_text(encoding="utf-8"))
    tampered["title"] = "被手工改坏"
    source_split.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")

    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)

    assert refreshed.returncode != 0
    assert "完整哈希不一致" in refreshed.stderr
    assert {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)} == before
    source_split.write_text(
        canonical_json_text(draft["requirement_split"]), encoding="utf-8"
    )
    destination = project / str(draft["requirement_import"]["destination"])
    source_coverage_path = destination / "requirement-coverage.v1.json"
    source_coverage = json.loads(source_coverage_path.read_text(encoding="utf-8"))
    source_coverage["requirement_split_sha256"] = "f" * 64
    source_coverage_path.write_text(
        canonical_json_text(source_coverage), encoding="utf-8"
    )

    # 同时改写登记和事件里的整包哈希，保证失败确实来自正式双文件交叉核对，
    # 而不是先被普通目录完整性检查拦住。
    registry = load_import_registry(paths)
    registration = registry["packages"][0]
    registration["bundle_sha256"] = atomic_import_module._bundle_sha256(destination)
    paths.import_registry_file.write_text(
        canonical_json_text(registry), encoding="utf-8"
    )
    events = load_events(paths)
    event = next(
        item
        for item in events
        if item["event_id"] == registration["event_id"]
    )
    event["payload"]["bundle_sha256"] = registration["bundle_sha256"]
    paths.events_file.write_text(
        "".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            for item in events
        ),
        encoding="utf-8",
    )

    refreshed = run_cli(["draft", "refresh", "DRAFT-001"], cwd=project)

    assert refreshed.returncode != 0
    assert "正式拆分哈希与覆盖声明不一致" in refreshed.stderr
    assert {path.name: path.read_bytes() for path in fixed_requirement_paths(paths)} == before


def test_delayed_older_process_rebuilds_projection_from_latest_committed_package(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split_a, coverage_a = requirement_documents(project, material, suffix="race-a")
    paths_a = write_documents(project, split_a, coverage_a, suffix="-race-a")
    older, continue_write, _ = start_barrier_cli(
        import_command(*paths_a), cwd=project, stage="before_projection_lock"
    )

    split_b, coverage_b = requirement_documents(project, material, suffix="race-b")
    paths_b = write_documents(project, split_b, coverage_b, suffix="-race-b")
    newer = run_cli(import_command(*paths_b), cwd=project)
    assert newer.returncode == 0, newer.stderr
    older_stdout, older_stderr = release_barrier_cli(older, continue_write)
    assert older.returncode == 0, (older_stdout, older_stderr)

    state = derive_state(paths)
    draft = state["drafts"]["DRAFT-001"]
    latest_event = requirement_events(paths)[-1]
    assert draft["requirement_import"]["package_key"] == latest_event["payload"][
        "package_key"
    ]
    requirements_dir = paths.draft_requirements_dir("DRAFT-001")
    fixed_receipt = json.loads(
        (requirements_dir / "需求导入回执.json").read_text(encoding="utf-8")
    )
    fixed_split = json.loads(
        (requirements_dir / "requirement-split.v1.json").read_text(encoding="utf-8")
    )
    fixed_coverage = json.loads(
        (requirements_dir / "requirement-coverage.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixed_receipt == draft["requirement_import"]
    assert fixed_split == draft["requirement_split"]
    assert fixed_coverage == draft["requirement_coverage"]
    assert (requirements_dir / "需求拆分.md").read_text(
        encoding="utf-8"
    ) == draft_requirement_split_markdown(draft)
    assert (requirements_dir / "需求覆盖矩阵.md").read_text(
        encoding="utf-8"
    ) == draft_requirement_coverage_markdown(draft)
    assert fixed_coverage["requirement_split_sha256"] == contract_sha256(
        fixed_split, schema_name=REQUIREMENT_SPLIT_SCHEMA
    )

    split_c, coverage_c = requirement_documents(project, material, suffix="locked-c")
    paths_c = write_documents(project, split_c, coverage_c, suffix="-locked-c")
    first, continue_write, _ = start_barrier_cli(
        import_command(*paths_c), cwd=project, stage="inside_projection_lock"
    )
    with paths.lock_file.open("a+", encoding="utf-8") as lock_handle:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    split_d, coverage_d = requirement_documents(project, material, suffix="locked-d")
    paths_d = write_documents(project, split_d, coverage_d, suffix="-locked-d")
    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"
    second = subprocess.Popen(
        [str(SDLC_BIN), *import_command(*paths_d)],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    first_stdout, first_stderr = release_barrier_cli(first, continue_write)
    second_stdout, second_stderr = second.communicate(timeout=30)

    assert first.returncode == 0, (first_stdout, first_stderr)
    assert second.returncode == 0, (second_stdout, second_stderr)
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert draft["requirement_import"]["package_key"] == requirement_events(paths)[-1][
        "payload"
    ]["package_key"]
    fixed_receipt = json.loads(
        (
            paths.draft_requirements_dir("DRAFT-001") / "需求导入回执.json"
        ).read_text(encoding="utf-8")
    )
    assert fixed_receipt == draft["requirement_import"]


def test_two_real_cli_processes_allocate_unique_ids_and_keep_one_complete_latest_projection(
    tmp_path: Path,
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    commands: list[list[str]] = []
    for suffix in ("parallel-a", "parallel-b"):
        split, coverage = requirement_documents(project, material, suffix=suffix)
        split_path, coverage_path = write_documents(project, split, coverage, suffix=suffix)
        commands.append(import_command(split_path, coverage_path))
    env = os.environ.copy()
    env["CODEX_SDLC_DISABLE_AUTO_BACKUP"] = "1"

    workers = [
        subprocess.Popen(
            [str(SDLC_BIN), *command],
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for command in commands
    ]
    completed = [worker.communicate(timeout=30) for worker in workers]

    assert [worker.returncode for worker in workers] == [0, 0], completed
    registrations = [
        item
        for item in load_import_registry(paths)["packages"]
        if item["package_key"].startswith("draft-requirements:")
    ]
    assert len(registrations) == 2
    for prefix, expected in (("FR", {"FR-001", "FR-002"}), ("GR", {"GR-001", "GR-002"}), ("AC", {"AC-001", "AC-002"})):
        assert {
            formal_id
            for item in registrations
            for formal_id in item["mapping"].values()
            if formal_id.startswith(f"{prefix}-")
        } == expected
    source_ids = {
        formal_id
        for item in registrations
        for formal_id in item["mapping"].values()
        if formal_id.startswith("SRC-")
    }
    assert source_ids == {f"SRC-{index:03d}" for index in range(1, 7)}
    assert len(requirement_events(paths)) == 2
    draft = derive_state(paths)["drafts"]["DRAFT-001"]
    assert "@client:" not in json.dumps(draft["requirement_split"], ensure_ascii=False)
    assert all(path.is_file() for path in fixed_requirement_paths(paths))


@pytest.mark.parametrize(
    "stage",
    ["after_staging", "after_rename", "after_event_registration", "after_registration"],
)
def test_real_process_interruption_recovers_without_half_state_or_wrong_number(
    tmp_path: Path, stage: str
) -> None:
    project, paths, material = create_draft_with_material(tmp_path)
    split, coverage = requirement_documents(project, material, suffix=f"interrupt-{stage}")
    split_path, coverage_path = write_documents(project, split, coverage)

    interrupted = run_cli(
        import_command(split_path, coverage_path),
        cwd=project,
        extra_env={"CODEX_SDLC_REQUIREMENTS_INTERRUPT_AT": stage},
    )
    assert interrupted.returncode == 73, (interrupted.stdout, interrupted.stderr)

    recovered = run_cli(import_command(split_path, coverage_path), cwd=project)

    assert recovered.returncode == 0, recovered.stderr
    assert len(requirement_events(paths)) == 1
    registrations = load_import_registry(paths)["packages"]
    assert len(registrations) == 1
    assert registrations[0]["mapping"][f"fr-interrupt-{stage}"] == "FR-001"
    assert registrations[0]["mapping"][f"gr-interrupt-{stage}"] == "GR-001"
    assert registrations[0]["mapping"][f"ac-interrupt-{stage}"] == "AC-001"
    assert not list(paths.import_transactions_dir.glob("*.json"))
    assert not list((paths.import_transactions_dir / "staging").glob("*"))
    assert not list(paths.sdlc_dir.glob(".events.jsonl.*.tmp"))
    assert not list(paths.sdlc_dir.glob(".import-registry.json.*.tmp"))
    assert all(path.is_file() for path in fixed_requirement_paths(paths))
    state = derive_state(paths)
    assert state["drafts"]["DRAFT-001"]["requirement_import"]["mapping"] == registrations[0][
        "mapping"
    ]
