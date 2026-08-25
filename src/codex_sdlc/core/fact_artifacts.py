from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from codex_sdlc.core import fact_schema, structured_contract
from codex_sdlc.core.errors import SdlcError


# 这些名称仍被 facts 活动调用方使用，但计算规则只保留在 structured_contract 中。
canonical_json_bytes = structured_contract.canonical_json_bytes
canonical_json_text = structured_contract.canonical_json_text
sha256_bytes = structured_contract.sha256_bytes
canonical_sha256 = structured_contract.canonical_sha256


def artifact_sha256(document: dict[str, Any]) -> str:
    schema_name = document.get("schema")
    if not isinstance(schema_name, str) or not schema_name.strip():
        raise SdlcError("产物哈希必须提供合法的 schema。")
    return structured_contract.contract_sha256(document, schema_name=schema_name)


def with_artifact_hash(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result["artifact_sha256"] = artifact_sha256(result)
    return result


def build_formal_source_projection(business: dict[str, Any]) -> dict[str, Any]:
    issues = fact_schema.formal_business_issues(business)
    if issues:
        raise ValueError("；".join(issues))
    return {
        "schema": fact_schema.FORMAL_SOURCE_PROJECTION_SCHEMA,
        "business": {name: deepcopy(business[name]) for name in fact_schema.FORMAL_BUSINESS_FIELDS},
    }


def build_draft_source_projection(
    requirement: str,
    design: str,
    questions: str,
    decisions: str,
) -> dict[str, Any]:
    return {
        "schema": fact_schema.DRAFT_SOURCE_PROJECTION_SCHEMA,
        "requirement": requirement,
        "design": design,
        "questions": questions,
        "decisions": decisions,
    }


def source_projection_sha256(source: dict[str, Any]) -> str:
    return canonical_sha256(source)


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _leaf_values(value: object, pointer: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        # JSON 对象没有业务顺序；按键排序后，写盘再读取不会改变 source unit 编号。
        for key in sorted(value):
            child = value[key]
            yield from _leaf_values(child, f"{pointer}/{_json_pointer_escape(str(key))}")
        return
    if isinstance(value, list):
        if not value:
            yield pointer, []
            return
        for index, child in enumerate(value):
            yield from _leaf_values(child, f"{pointer}/{index}")
        return
    yield pointer, value


def _quote(value: object) -> str:
    return value if isinstance(value, str) else canonical_json_bytes(value).decode("utf-8")


_FORMAL_MACHINE_FIELDS = {
    "id", "status", "task_id", "type", "material_refs", "requirement_ids", "acceptance_ids",
}


def _formal_classification(pointer: str, value: object) -> str:
    """只按 formal.v3 的确定性字段合同排除机器脚手架，不判断开放式业务语义。"""

    terminal = pointer.rsplit("/", 1)[-1]
    if value in (None, "") or value == [] or value == {}:
        return "structural"
    if any(f"/{field}/" in pointer or pointer.endswith(f"/{field}") for field in _FORMAL_MACHINE_FIELDS):
        return "structural"
    if any(pointer.startswith(f"/{group}/") for group in ("functional_requirements", "acceptance_criteria", "test_cases")) and (
        "/title" in pointer or "/summary" in pointer
    ):
        # 条目标题和摘要由同条目的说明、操作、预期等业务叶子生成，属于导航字段，不重复制造事实。
        return "structural"
    if any(pointer.startswith(f"/{group}/") for group in ("acceptance_criteria", "test_cases")) and "/description" in pointer:
        # 验收和测试的 description 是操作或预期的展示摘要，正式业务叶子由同条目的明确字段保存。
        return "structural"
    if pointer == "/description" and isinstance(value, str) and "\n## " in value:
        # DRAFT 物化包的 description 是完整 Markdown 镜像，业务叶子已经在后续正式字段中逐项保存。
        return "structural"
    return "content"


def formal_unit_allowed_categories(unit: dict[str, Any]) -> set[str]:
    """formal JSON Pointer 决定可绑定的事实类别，防止权限、接口等结构字段互相错绑。"""

    pointer = str(unit.get("json_pointer") or "")
    if unit.get("classification") == "structural":
        return set()
    if pointer.startswith("/design/"):
        field = pointer.split("/", 3)[2] if len(pointer.split("/")) > 2 else ""
        return {
            "data_flow": {"data_implementation"},
            "data_structures": {"data_implementation"},
            "error_handling": {"error_handling"},
            "interfaces": {"interface_implementation"},
            "modules": {"module"},
            "out_of_scope": {"requirement_coverage"},
            "permissions_security": {"permission_enforcement"},
            "requirement_coverage": {"requirement_coverage"},
            "risks": {"risk"},
            "state_flow": {"state_implementation"},
            "summary": {"module"},
            "technical_goal": {"module"},
            "test_strategy": {"test_strategy"},
            "title": {"module"},
        }.get(field, set())
    top = pointer.split("/", 2)[1] if pointer.startswith("/") else ""
    direct = {
        "title": {"goal"},
        "description": {"goal"},
        "background": {"goal"},
        "goal": {"goal"},
        "user_scenarios": {"actor_scenario"},
        "scope": {"scope"},
        "out_of_scope": {"out_of_scope"},
        "business_rules": {"business_rule"},
        "permission_rules": {"permission"},
        # 这个正式字段的合同名称就是“数据和状态规则”。同一句可以同时说明状态迁移和落库动作，
        # 因此只开放这两个有明确边界的类别；权限、接口、错误等仍必须引用各自专用字段。
        "data_state_rules": {"state_transition", "data_change"},
        "interface_scope": {"interface", "page_behavior"},
        "exception_rules": {"error"},
        "test_focus": {"test_case"},
        "open_questions": {"business_rule"},
        "decisions": {"business_rule"},
        "acceptance_criteria": {"acceptance"},
        "test_cases": {"test_case"},
    }
    if top in direct:
        return direct[top]
    if top != "functional_requirements":
        return set()
    field = pointer.split("/")[3] if len(pointer.split("/")) > 3 else ""
    return {
        "boundaries": {"out_of_scope"},
        "data_changes": {"data_change"},
        "exceptions": {"error"},
        "permissions": {"permission"},
        "description": {"business_rule"},
        "inputs": {"business_rule"},
        "outputs": {"business_rule"},
        "rules": {"business_rule"},
        "summary": {"business_rule"},
        "title": {"business_rule"},
        "triggers": {"business_rule"},
    }.get(field, set())


def _formal_units(source: dict[str, Any]) -> list[dict[str, Any]]:
    business = source["business"]
    projection_hash = source_projection_sha256(source)
    units: list[dict[str, Any]] = []
    counters = {"requirement": 0, "design": 0}
    for field in fact_schema.FORMAL_BUSINESS_FIELDS:
        value = business[field]
        owner = "design" if field == "design" else "requirement"
        prefix = "DU" if owner == "design" else "RU"
        for child_pointer, child in _leaf_values(value, f"/{_json_pointer_escape(field)}"):
            counters[owner] += 1
            quote = _quote(child)
            units.append(
                {
                    "unit_id": f"{prefix}-{counters[owner]:04d}",
                    "owner": owner,
                    "document_id": "formal-package",
                    "source_projection_sha256": projection_hash,
                    "anchor_kind": "json_pointer",
                    "json_pointer": child_pointer,
                    "section_key": field,
                    "quote": quote,
                    "quote_sha256": sha256_bytes(quote.encode("utf-8")),
                    "classification": _formal_classification(child_pointer, child),
                }
            )
    return units


def _markdown_units(source: dict[str, Any], draft_id: str) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    mapping = (("requirement", "RU"), ("design", "DU"), ("questions", "QU"), ("decisions", "CU"))
    filenames = {"requirement": "requirement.draft.md", "design": "design.draft.md", "questions": "questions.md", "decisions": "decisions.md"}
    for owner, prefix in mapping:
        text = str(source.get(owner, ""))
        document_hash = sha256_bytes(text.encode("utf-8"))
        counter = 0
        current_section = owner
        current_detail = ""
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            quote = raw_line.strip()
            structural = not quote or quote.startswith("#") or set(quote) <= {"-", "|", ":", " "}
            if quote.startswith("#"):
                current_section = quote.lstrip("#").strip() or owner
                current_detail = ""
            body = quote[2:].strip() if quote.startswith("- ") else quote
            if owner in {"questions", "decisions"} and body.startswith("草稿标题："):
                structural = True
            if owner == "requirement" and current_section in {"未确认问题", "待确认问题"}:
                # 问题状态只由 questions.md 对应的结构化 questions 合同提供。
                # requirement.draft.md 里的同名章节只是展示副本，任何正文都不能再变成事实或建档阻塞项。
                structural = True
            section_code = current_section.split(" ", 1)[0]
            detail_aliases = {
                "规则": "规则", "输入": "输入", "输出": "输出", "触发条件": "触发条件",
                "保存数据": "保存数据", "权限": "权限", "异常": "异常", "异常回退": "异常",
                "错误处理": "异常", "边界": "边界", "操作": "操作", "预期": "预期",
                "通过标准": "通过标准",
            }
            detail_labels = {f"{label}：" for label in detail_aliases}
            line_detail = ""
            if owner == "requirement" and section_code.startswith(("FR-", "AC-", "TC-")):
                if body in detail_labels:
                    current_detail = detail_aliases[body[:-1]]
                    structural = True
                elif "：" in body and body.split("：", 1)[0] in detail_aliases:
                    # FR/AC/TC 的同行字段名也是生成器合同的一部分。保留正文为内容，
                    # 但把它放进明确的局部字段槽，后续物化才能按“异常”“权限”等字段逐项对应。
                    current_detail = detail_aliases[body.split("：", 1)[0]]
                    line_detail = current_detail
                elif raw_line[:1].isspace() and quote.startswith("- ") and current_detail:
                    # 嵌套列表继承固定字段标签，避免把边界、异常或权限子项降成通用规则。
                    pass
                else:
                    current_detail = ""
            if owner == "requirement" and (
                (section_code.startswith("FR-") and body.startswith("验收关联："))
                or (section_code.startswith("AC-") and body.startswith(("覆盖需求：", "关联需求：")))
                or (
                    section_code.startswith("TC-")
                    and body.startswith(("覆盖验收：", "关联验收：", "覆盖需求：", "类型："))
                )
            ):
                # 这些行由 DRAFT 固定模板生成，只保存追溯编号或测试类型，不是独立业务事实。
                structural = True
            counter += 1
            units.append(
                {
                    "unit_id": f"{prefix}-{counter:04d}",
                    "owner": owner if owner in {"requirement", "design"} else "requirement",
                    "document_id": f"{draft_id}:{owner}",
                    "relative_path": f".codex-sdlc/drafts/{draft_id}/{filenames[owner]}",
                    "document_sha256": document_hash,
                    "anchor_kind": "markdown_lines",
                    "line_start": line_number,
                    "line_end": line_number,
                    "section_key": (
                        f"{current_section}/{line_detail or current_detail}"
                        if (line_detail or (current_detail and body not in detail_labels))
                        else current_section
                    ),
                    "quote": quote,
                    "quote_sha256": sha256_bytes(quote.encode("utf-8")),
                    "classification": "structural" if structural else "content",
                }
            )
    return units


def build_source_index(source: dict[str, Any], *, source_kind: str, draft_id: str = "") -> dict[str, Any]:
    if source_kind not in {"formal", "draft"}:
        raise ValueError("source_kind 只能是 formal 或 draft。")
    clean_draft_id = draft_id.strip().upper()
    if source_kind == "draft" and not clean_draft_id:
        raise ValueError("DRAFT 来源索引必须提供 draft_id。")
    units = _formal_units(source) if source_kind == "formal" else _markdown_units(source, clean_draft_id)
    documents: list[dict[str, Any]] = []
    if source_kind == "formal":
        documents.append({"document_id": "formal-package", "sha256": source_projection_sha256(source)})
    else:
        filenames = {"requirement": "requirement.draft.md", "design": "design.draft.md", "questions": "questions.md", "decisions": "decisions.md"}
        for name in ("requirement", "design", "questions", "decisions"):
            raw = str(source.get(name, "")).encode("utf-8")
            documents.append(
                {
                    "document_id": f"{clean_draft_id}:{name}",
                    "relative_path": f".codex-sdlc/drafts/{clean_draft_id}/{filenames[name]}",
                    "sha256": sha256_bytes(raw),
                }
            )
    return with_artifact_hash(
        {
            "schema": fact_schema.SOURCE_INDEX_SCHEMA,
            "source_kind": source_kind,
            **({"draft_id": clean_draft_id} if source_kind == "draft" else {}),
            "source_projection_sha256": source_projection_sha256(source),
            "normalization": "utf-8-preserve-source-and-canonical-json-pointer-v1",
            "documents": documents,
            "units": units,
        }
    )


def _source_parts(source: dict[str, Any]) -> dict[str, Any]:
    if source.get("schema") == fact_schema.FORMAL_SOURCE_PROJECTION_SCHEMA:
        business = source["business"]
        requirement = {key: value for key, value in business.items() if key != "design"}
        design = business["design"]
        questions = business["open_questions"]
        decisions = business["decisions"]
    else:
        requirement = source["requirement"]
        design = source["design"]
        questions = source["questions"]
        decisions = source["decisions"]
    return {
        "requirement_source_sha256": canonical_sha256(requirement),
        "design_source_sha256": canonical_sha256(design),
        "questions_sha256": canonical_sha256(questions),
        "decisions_sha256": canonical_sha256(decisions),
    }


def build_context_targets(source: dict[str, Any], index: dict[str, Any]) -> dict[str, str]:
    targets = {
        "source_projection_sha256": source_projection_sha256(source),
        "source_index_sha256": artifact_sha256(index),
        **_source_parts(source),
    }
    context = {"schema": fact_schema.REVIEW_INPUTS_SCHEMA, **targets}
    targets["context_inputs_sha256"] = canonical_sha256(context)
    return targets


def semantic_sha256(semantic: dict[str, Any]) -> str:
    payload = deepcopy(semantic)
    for fact in payload.get("facts", []):
        if isinstance(fact, dict):
            fact.pop("source_refs", None)
            fact.pop("decision_refs", None)
    return canonical_sha256(payload)


def _refs(index: dict[str, Any], unit_ids: set[str]) -> list[dict[str, Any]]:
    return [deepcopy(unit) for unit in index.get("units", []) if unit.get("unit_id") in unit_ids]


def build_fact_artifact(
    owner: str,
    semantic: dict[str, Any],
    targets: dict[str, str],
    index: dict[str, Any],
    *,
    producer: dict[str, Any] | None = None,
    coverage: list[dict[str, Any]] | None = None,
    draft_id: str = "",
) -> dict[str, Any]:
    unit_ids = {
        str(unit_id)
        for fact in semantic.get("facts", [])
        if isinstance(fact, dict)
        for unit_id in fact.get("source_refs", [])
    }
    referenced = _refs(index, unit_ids)
    binding_name = "formal_refs" if index.get("source_kind") == "formal" else "origin_refs"
    if coverage is None:
        fact_by_unit: dict[str, list[str]] = {}
        for fact in semantic.get("facts", []):
            if not isinstance(fact, dict):
                continue
            for unit_id in fact.get("source_refs", []):
                fact_by_unit.setdefault(str(unit_id), []).append(str(fact.get("fact_id", "")))
        coverage = []
        for unit in index.get("units", []):
            if unit.get("owner") != owner:
                continue
            unit_id = str(unit["unit_id"])
            coverage.append(
                {"unit_id": unit_id, "fact_ids": fact_by_unit.get(unit_id, []), "status": "covered" if unit_id in fact_by_unit else "non_contractual", "reason": "模型已标记该单元不形成独立合同事实。" if unit_id not in fact_by_unit else ""}
            )
    document = {
        "schema": fact_schema.REQUIREMENT_FACTS_SCHEMA if owner == "requirement" else fact_schema.DESIGN_FACTS_SCHEMA,
        "semantic": deepcopy(semantic),
        "semantic_sha256": semantic_sha256(semantic),
        "context_targets": deepcopy(targets),
        "bindings": {"origin_refs": [], "formal_refs": [], binding_name: referenced},
        "producer": deepcopy(producer or {"kind": "model", "actor_id": "primary-extractor", "run_id": "primary-extractor-run"}),
        "coverage": deepcopy(coverage),
        **({"draft_id": draft_id.strip().upper()} if draft_id.strip() else {}),
    }
    return with_artifact_hash(document)


def build_review_artifact(
    requirement: dict[str, Any],
    design: dict[str, Any],
    targets: dict[str, str],
    *,
    status: str,
    issues: list[dict[str, Any]],
    reviewer: dict[str, Any] | None = None,
    non_contractual_approvals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    review_targets = {
        **deepcopy(targets),
        "requirement_facts_sha256": artifact_sha256(requirement),
        "design_facts_sha256": artifact_sha256(design),
        "requirement_semantic_sha256": requirement.get("semantic_sha256", ""),
        "design_semantic_sha256": design.get("semantic_sha256", ""),
    }
    return with_artifact_hash(
        {
            "schema": fact_schema.MODEL_REVIEW_SCHEMA,
            "review_id": "MR-0001",
            "targets": review_targets,
            "reviewer": deepcopy(reviewer or {"role": "independent_checker", "actor_id": "independent-checker", "run_id": "independent-review-run"}),
            "status": status,
            "coverage_status": "complete" if status == "passed" and not issues else "incomplete",
            "issues": deepcopy(issues),
            "non_contractual_approvals": deepcopy(non_contractual_approvals or []),
        }
    )


def build_fact_manifest(
    source: dict[str, Any],
    index: dict[str, Any],
    requirement: dict[str, Any],
    design: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": fact_schema.FACT_BUNDLE_SCHEMA,
        "source_projection_schema": source.get("schema"),
        "source_projection_sha256": source_projection_sha256(source),
        "source_index_file": "source-index.json",
        "source_index_sha256": artifact_sha256(index),
        "requirement_facts_file": "requirement.facts.json",
        "design_facts_file": "design.facts.json",
        "model_review_file": "model-review.json",
        "requirement_facts_sha256": artifact_sha256(requirement),
        "design_facts_sha256": artifact_sha256(design),
        "model_review_sha256": artifact_sha256(review),
    }


def materialize_fact_artifact(fact: dict[str, Any], formal_index: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(fact)
    source_ids = {
        str(unit_id)
        for item in result.get("semantic", {}).get("facts", [])
        if isinstance(item, dict)
        for unit_id in item.get("source_refs", [])
    }
    result.setdefault("bindings", {})["formal_refs"] = _refs(formal_index, source_ids)
    result["artifact_sha256"] = artifact_sha256(result)
    return result


def _decision_text(unit: dict[str, Any]) -> str:
    text = str(unit.get("quote") or "").strip()
    return text[2:].strip() if text.startswith("- ") else text


def decision_ref_mapping(origin_index: dict[str, Any], formal_index: dict[str, Any]) -> dict[str, str]:
    """按决定正文建立 DRAFT CU 编号到正式 JSON Pointer 编号的唯一映射。"""

    origin_by_text: dict[str, list[str]] = {}
    formal_by_text: dict[str, list[str]] = {}
    for unit in origin_index.get("units", []):
        if (
            isinstance(unit, dict)
            and str(unit.get("unit_id") or "").startswith("CU-")
            and str(unit.get("document_id") or "").endswith(":decisions")
            and unit.get("classification") != "structural"
        ):
            origin_by_text.setdefault(_decision_text(unit), []).append(str(unit["unit_id"]))
    for unit in formal_index.get("units", []):
        if (
            isinstance(unit, dict)
            and unit.get("section_key") == "decisions"
            and unit.get("classification") != "structural"
        ):
            formal_by_text.setdefault(_decision_text(unit), []).append(str(unit["unit_id"]))
    mapping: dict[str, str] = {}
    for text, origin_ids in origin_by_text.items():
        formal_ids = formal_by_text.get(text, [])
        # 相同决定出现多次时无法证明引用的是哪一次，必须让模型先合并或澄清。
        if len(origin_ids) != 1 or len(formal_ids) != 1:
            continue
        mapping[origin_ids[0]] = formal_ids[0]
    return mapping


def materialize_semantic_decision_refs(
    semantic: dict[str, Any],
    origin_index: dict[str, Any],
    formal_index: dict[str, Any],
) -> dict[str, Any]:
    """只重绑来源编号，不改变业务语义摘要。丢失或歧义引用直接拒绝。"""

    document = materialize_fact_document_decision_refs(
        {"semantic": semantic, "coverage": []}, origin_index, formal_index
    )
    return document["semantic"]


def materialize_fact_document_decision_refs(
    document: dict[str, Any],
    origin_index: dict[str, Any],
    formal_index: dict[str, Any],
) -> dict[str, Any]:
    """统一重绑事实与 coverage 的决定编号，任何位置都不能遗留 DRAFT 的 CU 编号。"""

    result = deepcopy(document)
    mapping = decision_ref_mapping(origin_index, formal_index)
    locations = [
        ("事实", result.get("semantic", {}).get("facts", [])),
        ("coverage", result.get("coverage", [])),
    ]
    for label, items in locations:
        for item in items:
            if not isinstance(item, dict):
                continue
            refs = item.get("decision_refs", [])
            if not isinstance(refs, list):
                raise ValueError(f"{label} decision_refs 必须是数组。")
            missing = [str(ref) for ref in refs if str(ref) not in mapping]
            if missing:
                raise ValueError(
                    f"{label}决定引用无法唯一映射到正式 decisions：" + "、".join(missing)
                )
            item["decision_refs"] = [mapping[str(ref)] for ref in refs]
    return result


def materialize_review_approvals(
    review: dict[str, Any],
    origin_index: dict[str, Any],
    formal_index: dict[str, Any],
    *,
    unit_mapping: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    mapping = decision_ref_mapping(origin_index, formal_index)
    approvals = deepcopy(review.get("non_contractual_approvals", []))
    for approval in approvals:
        refs = approval.get("decision_refs", []) if isinstance(approval, dict) else []
        missing = [str(ref) for ref in refs if str(ref) not in mapping]
        if missing:
            raise ValueError("非合同批准的决定引用无法唯一物化：" + "、".join(missing))
        approval["decision_refs"] = [mapping[str(ref)] for ref in refs]
        if unit_mapping is not None:
            origin_unit_id = str(approval.get("unit_id") or "")
            if origin_unit_id not in unit_mapping:
                raise ValueError("非合同批准的内容单元无法唯一物化：" + origin_unit_id)
            approval["unit_id"] = unit_mapping[origin_unit_id]
    return approvals


def package_formal_v3(business: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    formal = {"formal_contract_version": "formal.v3", **deepcopy(business), "fact_bundle": deepcopy(bundle["manifest"])}
    return {
        "formal": formal,
        "source_projection": deepcopy(bundle["source"]),
        "source_index": deepcopy(bundle["index"]),
        "requirement_facts": deepcopy(bundle["requirement"]),
        "design_facts": deepcopy(bundle["design"]),
        "model_review": deepcopy(bundle["review"]),
    }


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json_text(document), encoding="utf-8")


def write_verified_bundle(root: Path, business: dict[str, Any], bundle: dict[str, Any]) -> None:
    current = root / "current"
    original = root / "original"
    effective = root / "effective"
    for directory in (current, original, effective):
        write_json(directory / "source-projection.json", bundle["source"])
        write_json(directory / "source-index.json", bundle["index"])
        write_json(directory / "requirement.facts.json", bundle["requirement"])
        write_json(directory / "design.facts.json", bundle["design"])
        write_json(directory / "model-review.json", bundle["review"])
        if directory != current:
            write_json(directory / "fact-bundle.json", bundle["manifest"])
        if isinstance(bundle.get("review_receipt"), dict):
            write_json(directory / "review-receipt.json", bundle["review_receipt"])
    write_json(current / "fact-bundle.current.json", bundle["manifest"])
    write_json(current / "formal.current.json", {"formal_contract_version": "formal.v3", **business, "fact_bundle": bundle["manifest"]})
    if isinstance(bundle.get("origin_index"), dict):
        origin = original / "origin"
        origin_source = bundle.get("origin_source")
        if isinstance(origin_source, dict):
            filenames = {
                "requirement": "requirement.draft.md",
                "design": "design.draft.md",
                "questions": "questions.md",
                "decisions": "decisions.md",
            }
            for name, filename in filenames.items():
                (origin / filename).parent.mkdir(parents=True, exist_ok=True)
                (origin / filename).write_text(str(origin_source.get(name, "")), encoding="utf-8")
        write_json(origin / "source-index.json", bundle["origin_index"])
        if isinstance(bundle.get("origin_requirement"), dict):
            write_json(origin / "requirement.facts.json", bundle["origin_requirement"])
        if isinstance(bundle.get("origin_design"), dict):
            write_json(origin / "design.facts.json", bundle["origin_design"])
        if isinstance(bundle.get("origin_review"), dict):
            write_json(origin / "model-review.json", bundle["origin_review"])


def build_formal_integrity_index(root: Path, formal_bytes: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    """外部索引只列不可变区域，索引自身不进入哈希，避免形成循环依赖。"""

    protected: dict[str, str] = {}
    for directory_name in ("original", "versions"):
        directory = root / directory_name
        if directory.exists():
            for path in sorted(item for item in directory.rglob("*") if item.is_file()):
                protected[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    for name in ("fact-bundle.current.json", "fact-bundle.json"):
        path = root / "current" / name
        if path.exists():
            protected[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
    return {
        "schema": "sdlc.formal-integrity.v2",
        "full_file_sha256": sha256_bytes(formal_bytes),
        "fact_bundle": deepcopy(manifest),
        "protected_files": protected,
    }
