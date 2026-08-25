from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from codex_sdlc.core import fact_artifacts, fact_schema


@dataclass(frozen=True)
class GateResult:
    passed: bool
    code: str
    message: str


@dataclass(frozen=True)
class ReviewFreshness:
    status: str
    reason: str


@dataclass(frozen=True)
class VerifiedFactBundle:
    source: dict[str, Any]
    index: dict[str, Any]
    requirement: dict[str, Any]
    design: dict[str, Any]
    review: dict[str, Any]
    manifest: dict[str, Any]
    origin_index: dict[str, Any] | None = None
    origin_semantic_sha256: dict[str, str] | None = None
    origin_requirement: dict[str, Any] | None = None
    origin_design: dict[str, Any] | None = None
    review_receipt: dict[str, Any] | None = None
    origin_source: dict[str, Any] | None = None
    origin_review: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "index": self.index,
            "requirement": self.requirement,
            "design": self.design,
            "review": self.review,
            "manifest": self.manifest,
            "origin_index": self.origin_index,
            "origin_semantic_sha256": self.origin_semantic_sha256,
            "origin_requirement": self.origin_requirement,
            "origin_design": self.origin_design,
            "review_receipt": self.review_receipt,
            "origin_source": self.origin_source,
            "origin_review": self.origin_review,
        }


def _failed(code: str, message: str) -> GateResult:
    return GateResult(False, code, message)


def _current_targets(bundle: dict[str, Any]) -> dict[str, str] | None:
    source = bundle.get("source")
    index = bundle.get("index")
    if not isinstance(source, dict) or not isinstance(index, dict):
        return None
    try:
        rebuilt_index = fact_artifacts.build_source_index(
            source,
            source_kind=str(index.get("source_kind")),
            draft_id=str(index.get("draft_id") or ""),
        )
        if fact_artifacts.artifact_sha256(rebuilt_index) != fact_artifacts.artifact_sha256(index):
            return None
        return fact_artifacts.build_context_targets(source, index)
    except (KeyError, TypeError, ValueError):
        return None


def review_freshness(bundle: dict[str, Any]) -> ReviewFreshness:
    targets = _current_targets(bundle)
    review = bundle.get("review")
    requirement = bundle.get("requirement")
    design = bundle.get("design")
    if targets is None or not isinstance(review, dict) or not isinstance(requirement, dict) or not isinstance(design, dict):
        return ReviewFreshness("stale", "来源内容或索引已经变化。")
    expected = {
        **targets,
        "requirement_facts_sha256": fact_artifacts.artifact_sha256(requirement),
        "design_facts_sha256": fact_artifacts.artifact_sha256(design),
        "requirement_semantic_sha256": requirement.get("semantic_sha256", ""),
        "design_semantic_sha256": design.get("semantic_sha256", ""),
    }
    if review.get("targets") != expected:
        return ReviewFreshness("stale", "原文、问题、决定、索引或事实已经变化，请重新复核。")
    return ReviewFreshness(str(review.get("status") or "needs_review"), "复核目标与当前输入一致。")


def _artifact_valid(document: dict[str, Any]) -> bool:
    return document.get("artifact_sha256") == fact_artifacts.artifact_sha256(document)


def _reference_issue(
    fact: dict[str, Any],
    index: dict[str, Any],
    *,
    entry_kind: str,
    origin_index: dict[str, Any] | None,
) -> tuple[str, str] | None:
    formal_units = {str(item.get("unit_id")): item for item in index.get("units", []) if isinstance(item, dict)}
    origin_units = {
        str(item.get("unit_id")): item
        for item in (origin_index or {}).get("units", [])
        if isinstance(item, dict)
    }
    bindings = fact.get("bindings", {})
    if not isinstance(bindings, dict):
        return "来源引用结构无效。"
    required = "origin_refs" if entry_kind == "draft" else "formal_refs"
    refs = bindings.get(required, [])
    # 正式物化时允许同时保留 origin refs；无 DRAFT 来源时只要求正式引用。
    if not refs:
        return f"缺少 {required}。"
    if entry_kind == "formal" and origin_index is None and bindings.get("origin_refs"):
        return "无 DRAFT 来源的正式事实不能声明 origin_refs。"
    if entry_kind == "formal" and origin_index is not None and not bindings.get("origin_refs"):
        return "带 DRAFT 来源的正式事实必须保留 origin_refs。"
    markdown_fields = {
        "unit_id", "document_id", "relative_path", "document_sha256", "anchor_kind",
        "line_start", "line_end", "section_key", "quote", "quote_sha256", "owner", "classification",
    }
    formal_fields = {
        "unit_id", "document_id", "source_projection_sha256", "anchor_kind", "json_pointer",
        "section_key", "quote", "quote_sha256", "owner", "classification",
    }
    for binding_name, expected_units in (("origin_refs", origin_units), ("formal_refs", formal_units)):
      for ref in bindings.get(binding_name, []):
        if not isinstance(ref, dict):
            return "来源引用必须是对象。"
        unit = expected_units.get(str(ref.get("unit_id")))
        if unit is None:
            return "来源引用指向不存在的内容单元。"
        expected_fields = markdown_fields if binding_name == "origin_refs" else formal_fields
        # 引用就是规范内容单元的完整快照。缺字段、额外字段和类型变化都不能靠重算外层哈希掩盖。
        if set(ref) != expected_fields or set(unit) != expected_fields:
            return "来源引用字段与规范内容单元不一致。"
        if any(type(ref[name]) is not type(unit[name]) or ref[name] != unit[name] for name in expected_fields):
            return "来源引用与规范内容单元不一致。"
    bound_ids = {
        str(ref.get("unit_id"))
        for name in ("origin_refs", "formal_refs")
        for ref in bindings.get(name, [])
        if isinstance(ref, dict)
    }
    semantic_ids = {
        str(unit_id)
        for item in fact.get("semantic", {}).get("facts", [])
        if isinstance(item, dict)
        for unit_id in item.get("source_refs", [])
    }
    if not semantic_ids <= bound_ids:
        return "事实 source_refs 没有对应的有效来源绑定。"
    return None


def _coverage_complete(fact: dict[str, Any], index: dict[str, Any], owner: str) -> bool:
    expected = {
        str(item.get("unit_id"))
        for item in index.get("units", [])
        if isinstance(item, dict) and item.get("owner") == owner and item.get("classification") != "structural"
    }
    coverage = fact.get("coverage", [])
    actual = {str(item.get("unit_id")) for item in coverage if isinstance(item, dict) and item.get("status") in {"covered", "non_contractual", "ambiguous"}}
    return expected <= actual


def validate_fact_artifact_references(
    document: dict[str, Any],
    index: dict[str, Any],
    *,
    owner: str,
    entry_kind: str,
    origin_index: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> str | None:
    """供单份 facts 写入和完整 FactGate 共用同一套锚点、覆盖校验。"""

    reference_issue = _reference_issue(document, index, entry_kind=entry_kind, origin_index=origin_index)
    if reference_issue:
        return ("invalid_source_ref", reference_issue)
    if not _coverage_complete(document, index, owner):
        return ("coverage_gap", "存在没有覆盖记录的原文单元。")
    owner_units = {
        str(item.get("unit_id"))
        for item in index.get("units", [])
        if isinstance(item, dict) and item.get("owner") == owner
    }
    facts_by_id = {
        str(item.get("fact_id")): item
        for item in document.get("semantic", {}).get("facts", [])
        if isinstance(item, dict)
    }
    if entry_kind == "formal":
        units_by_id = {
            str(item.get("unit_id")): item
            for item in index.get("units", [])
            if isinstance(item, dict)
        }
        for fact in facts_by_id.values():
            category = str(fact.get("category") or "")
            for unit_id in fact.get("source_refs", []):
                unit = units_by_id.get(str(unit_id))
                if unit is None:
                    continue
                allowed = fact_artifacts.formal_unit_allowed_categories(unit)
                if not allowed or category not in allowed:
                    return (
                        "invalid_source_ref",
                        f"正式来源字段不能绑定到 {category} 事实，请按 JSON 字段含义重新生成来源引用。",
                    )
    fact_ids = set(facts_by_id)
    seen_units: set[str] = set()
    decision_units = {
        str(item.get("unit_id"))
        for item in index.get("units", [])
        if isinstance(item, dict) and (str(item.get("unit_id", "")).startswith("CU-") or item.get("section_key") == "decisions")
    }
    approvals = {
        str(item.get("approval_id")): item
        for item in (review or {}).get("non_contractual_approvals", [])
        if isinstance(item, dict)
    }
    for coverage in document.get("coverage", []):
        unit_id = str(coverage.get("unit_id") or "")
        if unit_id in seen_units:
            return ("coverage_gap", "覆盖记录不能重复同一内容单元。")
        seen_units.add(unit_id)
        if coverage.get("unit_id") not in owner_units:
            return ("invalid_source_ref", "覆盖记录引用了不存在或 owner 不匹配的内容单元。")
        covered_facts = {str(item) for item in coverage.get("fact_ids", [])}
        if not covered_facts <= fact_ids:
            return ("coverage_gap", "覆盖记录引用了不存在的事实编号。")
        if coverage.get("status") == "covered" and not covered_facts:
            return ("coverage_gap", "标记为 covered 的内容单元没有关联事实。")
        if coverage.get("status") == "covered" and any(
            unit_id not in facts_by_id[fact_id].get("source_refs", []) for fact_id in covered_facts
        ):
            return ("coverage_gap", "覆盖记录关联的事实没有引用该内容单元。")
        if coverage.get("status") == "ambiguous":
            return ("ambiguity_unresolved", "内容单元仍标记为歧义，不能通过正式门禁。")
        if coverage.get("status") == "non_contractual" and covered_facts:
            return ("coverage_gap", "non_contractual 内容单元不能同时关联合同事实。")
        unit = next((item for item in index.get("units", []) if item.get("unit_id") == coverage.get("unit_id")), None)
        if coverage.get("status") == "non_contractual" and isinstance(unit, dict) and unit.get("classification") != "structural":
            decision_refs = {str(item) for item in coverage.get("decision_refs", [])}
            approval_refs = {str(item) for item in coverage.get("approval_refs", [])}
            if decision_refs and not decision_refs <= decision_units:
                return ("invalid_source_ref", "non_contractual 引用了不存在的用户决定。")
            valid_approvals = {
                approval_id
                for approval_id in approval_refs
                if approval_id in approvals
                and approvals[approval_id].get("unit_id") == unit_id
                and approvals[approval_id].get("owner") == owner
                and bool(approvals[approval_id].get("decision_refs"))
                and len(approvals[approval_id].get("decision_refs", []))
                == len(set(str(item) for item in approvals[approval_id].get("decision_refs", [])))
                and set(str(item) for item in approvals[approval_id].get("decision_refs", [])) <= decision_units
            }
            if not decision_refs and (not approval_refs or valid_approvals != approval_refs):
                return (
                    "untrusted_non_contractual",
                    "业务内容不能用通用理由排除；请关联真实用户决定或逐项可信批准。",
                )
    return None


class FactGate:
    @staticmethod
    def verify(bundle: dict[str, Any], *, entry_kind: str) -> GateResult:
        if entry_kind not in {"draft", "formal"}:
            return _failed("entry_contract_invalid", "事实门禁入口类型无效。")
        missing_codes = {
            "source": "missing_source_projection",
            "index": "missing_source_index",
            "requirement": "missing_requirement_facts",
            "design": "missing_design_facts",
            "review": "missing_model_review",
            "manifest": "missing_fact_manifest",
        }
        for name, code in missing_codes.items():
            if not isinstance(bundle.get(name), dict):
                return _failed(code, f"缺少 {name} 产物。")

        requirement = bundle["requirement"]
        design = bundle["design"]
        review = bundle["review"]
        index = bundle["index"]
        requirement_ids = {
            str(item.get("fact_id")) for item in requirement.get("semantic", {}).get("facts", []) if isinstance(item, dict)
        }
        if fact_schema.fact_document_issues(requirement, owner="requirement") or fact_schema.fact_document_issues(
            design, owner="design", requirement_fact_ids=requirement_ids
        ) or fact_schema.review_document_issues(review):
            return _failed("schema_invalid", "事实或复核文件未通过 schema 校验。")
        for document in (index, requirement, design, review):
            if not _artifact_valid(document):
                return _failed("artifact_hash_mismatch", "产物内容与 artifact hash 不一致。")
        for document in (requirement, design):
            if document.get("semantic_sha256") != fact_artifacts.semantic_sha256(document["semantic"]):
                return _failed("semantic_hash_mismatch", "事实语义摘要与内容不一致。")

        if review.get("issues"):
            issue = review["issues"][0]
            return _failed(str(issue.get("type") or "needs_review"), str(issue.get("message") or "模型复核发现问题。"))
        freshness = review_freshness(bundle)
        if freshness.status == "stale":
            return _failed("stale", freshness.reason)
        if freshness.status != "passed":
            return _failed(freshness.status, "模型复核尚未通过。")
        if review.get("coverage_status") != "complete":
            return _failed("coverage_gap", "模型复核没有确认原文覆盖完整。")
        # producer/reviewer 由模型文件提供，只用于说明。独立性只看 CLI 捕获的运行环境任务标识和受信回执。

        origin = bundle.get("origin_semantic_sha256")
        if isinstance(origin, dict) and (
            origin.get("requirement") != requirement.get("semantic_sha256")
            or origin.get("design") != design.get("semantic_sha256")
        ):
            return _failed("entry_contract_mismatch", "DRAFT 与正式物化事实的语义摘要不一致。")

        for owner, document in (("requirement", requirement), ("design", design)):
            ref_issue = validate_fact_artifact_references(
                document,
                index,
                owner=owner,
                entry_kind=entry_kind,
                origin_index=bundle.get("origin_index") if isinstance(bundle.get("origin_index"), dict) else None,
                review=review,
            )
            if ref_issue:
                return _failed(*ref_issue)
            for fact in document["semantic"].get("facts", []):
                decision_units = {
                    str(item.get("unit_id"))
                    for item in index.get("units", [])
                    if isinstance(item, dict) and (str(item.get("unit_id", "")).startswith("CU-") or item.get("section_key") == "decisions")
                }
                decision_refs = {str(item) for item in fact.get("decision_refs", [])}
                if not decision_refs <= decision_units:
                    return _failed("invalid_source_ref", "事实 decision_refs 指向不存在的用户决定。")
                if fact.get("certainty") == "inferred" and not decision_refs:
                    return _failed("ambiguity_unresolved", "推断事实尚未得到用户决定确认。")
                if fact.get("certainty") == "ambiguous" and not fact.get("ambiguity_id"):
                    return _failed("ambiguity_unresolved", "歧义事实没有关联 ambiguity_id。")
            if document["semantic"].get("ambiguities"):
                return _failed("ambiguity_unresolved", "事实文件仍包含没有解决的业务歧义。")

        receipt = bundle.get("review_receipt")
        from codex_sdlc.core import fact_review_trust

        expected_target = fact_review_trust.review_target_sha256(requirement, design, review.get("targets", {}))
        if not isinstance(receipt, dict) or receipt.get("trusted") is not True:
            return _failed("missing_review_receipt", "缺少 CLI 签发的独立复核回执。")
        if receipt.get("target_sha256") != expected_target or receipt.get("review_sha256") != fact_artifacts.artifact_sha256(review):
            return _failed("invalid_review_receipt", "独立复核回执与当前 facts 或复核内容不一致。")

        high_risk = {
            str(item.get("fact_id"))
            for item in requirement["semantic"].get("facts", [])
            if isinstance(item, dict)
            and item.get("category") in {"permission", "interface", "state_transition", "error", "data_change", "out_of_scope"}
        }
        requirement_by_id = {str(item["fact_id"]): item for item in requirement["semantic"].get("facts", []) if isinstance(item, dict)}
        design_by_id = {str(item["fact_id"]): item for item in design["semantic"].get("facts", []) if isinstance(item, dict)}
        compatible = {
            "permission": {"permission_enforcement"},
            "interface": {"interface_implementation"},
            "state_transition": {"state_implementation"},
            "error": {"error_handling"},
            "data_change": {"data_implementation"},
            "out_of_scope": {"requirement_coverage"},
        }
        covered: set[str] = set()
        for relation in design["semantic"].get("relations", []):
            if not isinstance(relation, dict):
                continue
            requirement_fact = requirement_by_id.get(str(relation.get("requirement_fact_id")))
            design_fact = design_by_id.get(str(relation.get("design_fact_id")))
            if requirement_fact is None or design_fact is None:
                return _failed("requirement_design_conflict", "技术关系引用了不存在的当前事实。")
            allowed = compatible.get(str(requirement_fact.get("category")))
            if allowed is not None and relation.get("type") != "implements":
                return _failed("requirement_design_conflict", "高风险需求事实必须使用 implements 技术关系。")
            if allowed is not None and design_fact.get("category") not in allowed:
                return _failed("requirement_design_conflict", "高风险需求事实关联了错误类别的技术事实。")
            covered.add(str(requirement_fact["fact_id"]))
        if not high_risk <= covered:
            return _failed("requirement_design_conflict", "高风险需求事实没有逐条关联技术事实。")

        expected_manifest = fact_artifacts.build_fact_manifest(bundle["source"], index, requirement, design, review)
        if bundle["manifest"] != expected_manifest:
            return _failed("manifest_mismatch", "事实清单与实际产物不一致。")
        return GateResult(True, "passed", "事实层检查通过。")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_saved_bundle_integrity(requirement_root: Path) -> list[str]:
    issues: list[str] = []
    snapshots: dict[str, dict[str, bytes]] = {}
    integrity_path = requirement_root / "formal-integrity.json"
    original_formal_path = requirement_root / "original" / "formal.v3.json"
    integrity: dict[str, Any] | None = None
    if not integrity_path.exists() or not original_formal_path.exists():
        issues.append("缺少 formal.v3 原始输入或外部完整性索引")
    else:
        try:
            integrity = _load(integrity_path)
            original_bytes = original_formal_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append(f"formal.v3 原始输入或完整性索引无法解析：{exc}")
        else:
            if integrity.get("schema") != "sdlc.formal-integrity.v2" or integrity.get("full_file_sha256") != fact_artifacts.sha256_bytes(original_bytes):
                issues.append("formal.v3 原始输入完整文件哈希不一致")
            protected = integrity.get("protected_files")
            if not isinstance(protected, dict) or not all(isinstance(name, str) and isinstance(digest, str) for name, digest in protected.items()):
                issues.append("外部完整性索引的受保护文件清单无效")
            else:
                actual_paths: set[str] = set()
                for protected_dir in (requirement_root / "original", requirement_root / "versions"):
                    if protected_dir.exists():
                        actual_paths.update(
                            path.relative_to(requirement_root).as_posix()
                            for path in protected_dir.rglob("*") if path.is_file()
                        )
                for name in ("fact-bundle.current.json", "fact-bundle.json"):
                    path = requirement_root / "current" / name
                    if path.exists():
                        actual_paths.add(path.relative_to(requirement_root).as_posix())
                # 受控 change 可以新增 requirement.v2 等版本文件；已有版本仍按哈希冻结。
                # original 和 current 权威 manifest 不允许出现清单外文件。
                actual_fixed = {name for name in actual_paths if not name.startswith("versions/")}
                protected_fixed = {name for name in protected if not name.startswith("versions/")}
                if actual_fixed != protected_fixed:
                    issues.append("受保护文件有缺失或新增")
                for relative_path, expected_hash in protected.items():
                    path = requirement_root / relative_path
                    if not path.exists():
                        issues.append(f"受保护文件缺失：{relative_path}")
                    elif fact_artifacts.sha256_bytes(path.read_bytes()) != expected_hash:
                        issues.append(f"受保护文件内容漂移：{relative_path}")
    for directory_name in ("original", "effective", "current"):
        directory = requirement_root / directory_name
        manifest_path = directory / ("fact-bundle.current.json" if directory_name == "current" else "fact-bundle.json")
        if not manifest_path.exists() and directory_name == "current":
            manifest_path = directory / "fact-bundle.json"
        required = {
            "source": directory / "source-projection.json",
            "index": directory / "source-index.json",
            "requirement": directory / "requirement.facts.json",
            "design": directory / "design.facts.json",
            "review": directory / "model-review.json",
            "manifest": manifest_path,
        }
        receipt_path = directory / "review-receipt.json"
        if receipt_path.exists():
            required["review_receipt"] = receipt_path
        missing = [path.name for path in required.values() if not path.exists()]
        if missing:
            issues.append(f"{directory_name} 缺少事实产物：{', '.join(missing)}")
            continue
        snapshots[directory_name] = {name: path.read_bytes() for name, path in required.items()}
        try:
            bundle = {name: _load(path) for name, path in required.items()}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            issues.append(f"{directory_name} 事实产物无法解析：{exc}")
            continue
        origin_dir = requirement_root / "original" / "origin"
        origin_index_path = origin_dir / "source-index.json"
        origin_requirement_path = origin_dir / "requirement.facts.json"
        origin_design_path = origin_dir / "design.facts.json"
        origin_review_path = origin_dir / "model-review.json"
        if origin_index_path.exists():
            bundle["origin_index"] = _load(origin_index_path)
            if origin_requirement_path.exists() and origin_design_path.exists():
                origin_requirement = _load(origin_requirement_path)
                origin_design = _load(origin_design_path)
                bundle["origin_semantic_sha256"] = {
                    "requirement": str(origin_requirement.get("semantic_sha256") or ""),
                    "design": str(origin_design.get("semantic_sha256") or ""),
                }
        if "review_receipt" in bundle:
            bundle["review_receipt"] = {**bundle["review_receipt"], "trusted": True}
        if origin_index_path.exists():
            origin_text_paths = {
                "requirement": origin_dir / "requirement.draft.md",
                "design": origin_dir / "design.draft.md",
                "questions": origin_dir / "questions.md",
                "decisions": origin_dir / "decisions.md",
            }
            if not all(path.exists() for path in origin_text_paths.values()) or not origin_review_path.exists():
                issues.append("original/origin 缺少冻结的四份输入或模型复核")
            else:
                origin_source = fact_artifacts.build_draft_source_projection(
                    *(origin_text_paths[name].read_text(encoding="utf-8") for name in ("requirement", "design", "questions", "decisions"))
                )
                rebuilt_origin_index = fact_artifacts.build_source_index(
                    origin_source,
                    source_kind="draft",
                    draft_id=str(bundle["origin_index"].get("draft_id") or ""),
                )
                if rebuilt_origin_index != bundle["origin_index"]:
                    issues.append("original/origin 的来源索引不能由冻结原文重建")
        result = FactGate.verify(bundle, entry_kind="formal")
        if not result.passed:
            issues.append(f"{directory_name} manifest 或事实产物漂移：{result.message}")
        if directory_name == "original" and isinstance(integrity, dict) and integrity.get("fact_bundle") != bundle["manifest"]:
            issues.append("外部完整性索引与 original 事实清单不一致")
        if directory_name == "current":
            formal_current = directory / "formal.current.json"
            if not formal_current.exists():
                issues.append("current 缺少事实层绑定的 formal.current.json")
            else:
                try:
                    formal = _load(formal_current)
                    rebuilt_source = fact_artifacts.build_formal_source_projection(formal)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    issues.append(f"current formal.current.json 无法重建来源投影：{exc}")
                else:
                    if rebuilt_source != bundle["source"] or formal.get("fact_bundle") != bundle["manifest"]:
                        issues.append("current formal.current.json 与来源投影或事实清单不一致")
    # 三个目录是同一次已验证写入的只读副本。即使有人把单个目录的整条哈希链一起重算，
    # 副本间差异仍必须被 doctor-deep 发现，不能把局部自洽误判为未漂移。
    if all(name in snapshots for name in ("original", "effective", "current")):
        for artifact_name in ("source", "index", "requirement", "design", "review", "manifest", "review_receipt"):
            if not all(artifact_name in snapshots[name] for name in ("original", "effective", "current")):
                continue
            values = {snapshots[name][artifact_name] for name in ("original", "effective", "current")}
            if len(values) != 1:
                issues.append(f"original/effective/current 的 {artifact_name} 内容漂移")
    return issues
