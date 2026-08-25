from __future__ import annotations

import ast
from pathlib import Path

from codex_sdlc.commands.plan_cmd import is_fix_task
from codex_sdlc.commands.task_cmd import task_is_ui_report
from codex_sdlc.core.state import task_regression_scope
from codex_sdlc.core.task_pack import task_kind
from codex_sdlc.services.start_service import usable_test_commands


ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "codex_sdlc"

FORBIDDEN_FUNCTIONS = {
    "draft_consistency_issues",
    "facts_from_markdown",
    "facts_from_package",
    "missing_facts",
    "structured_permission_facts",
    "structured_interface_facts",
    "structured_state_facts",
    "structured_error_facts",
    "structured_data_facts",
    "review_draft_before_start",
    "start_package_from_draft",
    "find_design_conflicts",
    "looks_like_clear_bug_fix",
    "looks_like_requirement_change",
    "looks_like_check_task",
    "task_requires_feedback_trace",
    "task_report_matches",
    "task_change_report_formal_issues",
    "score_lesson_for_text",
    "infer_lesson_level",
    "classify_test_case",
    "is_process_acceptance_item",
    "generate_tasks_from_requirement",
    "candidate_from_text",
    "detect_impacted_tasks",
    "agent_rule_snippets",
    "inferred_task_files",
    "is_generic_check_text",
    "is_manual_verification_summary",
    "verification_summaries_from_event",
    "explicit_material_task_ids",
    "material_type_from_argument",
    "material_source_from_argument",
    "normalize_task_id",
}

FREE_TEXT_FIELDS = {
    "title",
    "summary",
    "goal",
    "feedback",
    "description",
    "note",
    "content",
    "text",
    "requirement_body",
    "design_body",
}
TEXT_WRAPPERS = {"str", "clean_text", "sanitize_runtime_text", "clean_commit_text"}
TEXT_METHODS = {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}
TEXT_PREDICATE_METHODS = {"startswith", "endswith"}
REGEX_PREDICATES = {"search", "match", "fullmatch", "findall", "finditer"}
FEEDBACK_CONTRACT_FIELDS = {"feedback_contract_version", "feedback_state"}
FEEDBACK_CONTRACT_DEFAULTS = {"feedback.v1", "none", "structured"}

# 这些位置只识别用户明确选择的枚举、Markdown 固定标题或命令字面量，不判断开放式中文的含义。
ALLOWED_CJK_MEMBERSHIP = {
    ("src/codex_sdlc/commands/docs_cmd.py", "docs_next_recommendation"),
    ("src/codex_sdlc/core/draft_contract.py", "explicit_permission_field_issues"),
    ("src/codex_sdlc/core/fact_artifacts.py", "_markdown_units"),
    ("src/codex_sdlc/core/state.py", "inspect_materialized_state"),
    ("src/codex_sdlc/core/state.py", "build_task_model_advice"),
    # 这两处只对结构化状态生成的固定展示文字做去重或分流，不读取业务正文决定合同状态。
    ("src/codex_sdlc/core/state.py", "task_acceptance_feedback_lines"),
    ("src/codex_sdlc/core/state.py", "task_plan_review_action"),
    ("src/codex_sdlc/core/task_pack_contract.py", "_section_group"),
    ("src/codex_sdlc/core/task_pack_contract.py", "compact_task_pack_markdown"),
}
ALLOWED_CJK_PREFIX = {
    ("src/codex_sdlc/commands/design_cmd.py", "parse_design_task_impacts"),
    ("src/codex_sdlc/core/draft_contract.py", "explicit_permission_field_issues"),
    ("src/codex_sdlc/core/fact_artifacts.py", "_markdown_units"),
    ("src/codex_sdlc/core/state.py", "extract_current_next_snapshot"),
    ("src/codex_sdlc/core/state.py", "sanitize_task_pack_markdown"),
    ("src/codex_sdlc/services/design_service.py", "build_design_draft_body"),
    ("src/codex_sdlc/services/draft_service.py", "_body"),
}
ALLOWED_CJK_REGEX = {
    ("src/codex_sdlc/core/markdown_contract.py", "extract_public_ids"),
    ("src/codex_sdlc/core/state.py", "slugify_text"),
    ("src/codex_sdlc/core/state.py", "build_design_title"),
    ("src/codex_sdlc/core/state.py", "split_readable_sentence_lines"),
    ("src/codex_sdlc/services/design_service.py", "_has_markdown_heading"),
    ("src/codex_sdlc/services/draft_service.py", "_has_heading"),
}


def production_python_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _container_strings(node: ast.AST) -> list[str]:
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        items = node.elts
    elif isinstance(node, ast.Dict):
        items = [*node.keys, *node.values]
    else:
        return []
    return [item.value for item in items if isinstance(item, ast.Constant) and isinstance(item.value, str)]


class _ScopedCollector(ast.NodeVisitor):
    """收集一个函数自己的节点，不把内层函数的局部变量混进来。"""

    def __init__(self, root: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.root = root
        self.nodes: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes.append(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _function_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    collector = _ScopedCollector(function)
    collector.visit(function)
    return collector.nodes


def _reads_free_text_field(node: ast.AST, tainted_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tainted_names
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value in FREE_TEXT_FIELDS
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in FREE_TEXT_FIELDS
        ):
            return True
        if isinstance(node.func, ast.Name) and node.func.id in TEXT_WRAPPERS:
            return any(_reads_free_text_field(arg, tainted_names) for arg in node.args)
        if isinstance(node.func, ast.Attribute) and node.func.attr in TEXT_METHODS:
            return _reads_free_text_field(node.func.value, tainted_names)
        # 任意包装函数只要接收了开放文本，返回值就继续按开放文本追踪；不能靠改包装函数名洗掉来源。
        return any(_reads_free_text_field(arg, tainted_names) for arg in node.args) or any(
            _reads_free_text_field(keyword.value, tainted_names) for keyword in node.keywords
        )
    if isinstance(node, ast.BoolOp):
        return any(_reads_free_text_field(value, tainted_names) for value in node.values)
    if isinstance(node, ast.IfExp):
        return _reads_free_text_field(node.body, tainted_names) or _reads_free_text_field(node.orelse, tainted_names)
    if isinstance(node, ast.BinOp):
        return _reads_free_text_field(node.left, tainted_names) or _reads_free_text_field(node.right, tainted_names)
    return False


def _tainted_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    nodes: list[ast.AST],
) -> set[str]:
    # summary、note 这类函数参数本身就是开放式文本，不能只在读取字典字段时才追踪。
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    result = {argument.arg for argument in arguments if argument.arg in FREE_TEXT_FIELDS}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not _reads_free_text_field(node.value, result):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return result


def _cjk_tables(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            values = _container_strings(node.value)
            is_table = any(_contains_cjk(value) for value in values)
            if isinstance(node.value, ast.Name) and node.value.id in result:
                is_table = True
            if not is_table:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return result


def _cjk_iteration_names(nodes: list[ast.AST], table_names: set[str]) -> set[str]:
    """追踪 `term in TERMS` 生成器里的 term，避免生成器改写绕过词表检查。"""

    result: set[str] = set()
    for node in nodes:
        generators: list[ast.comprehension] = []
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            generators = node.generators
        elif isinstance(node, ast.DictComp):
            generators = node.generators
        for generator in generators:
            if isinstance(generator.iter, ast.Name) and generator.iter.id in table_names:
                if isinstance(generator.target, ast.Name):
                    result.add(generator.target.id)
    return result


def _comparison_uses_cjk_table(
    node: ast.Compare,
    table_names: set[str],
    cjk_iteration_names: set[str],
) -> bool:
    if not any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops):
        return False
    expressions = [node.left, *node.comparators]
    if any(
        isinstance(item, ast.Name) and item.id in table_names | cjk_iteration_names
        for item in expressions
    ):
        return True
    if any(
        isinstance(item, ast.Constant)
        and isinstance(item.value, str)
        and _contains_cjk(item.value)
        for item in expressions
    ):
        return True
    return any(
        any(_contains_cjk(value) for value in _container_strings(item))
        for item in expressions
    )


def _argument_uses_cjk_literal(node: ast.AST, table_names: set[str]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _contains_cjk(node.value)
    if isinstance(node, ast.Name):
        return node.id in table_names
    return any(_contains_cjk(value) for value in _container_strings(node))


def _display_placeholder_tables(tree: ast.AST) -> set[str]:
    result: set[str] = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            values = _container_strings(node.value)
            is_placeholder = any(value == "无" or value.startswith("暂无") for value in values)
            if isinstance(node.value, ast.Name) and node.value.id in result:
                is_placeholder = True
            if not is_placeholder:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return result


def _display_placeholder_comparison(node: ast.Compare, placeholder_tables: set[str]) -> bool:
    """展示用的“暂无…”不能再被读回来充当空值或状态。"""

    expressions = [node.left, *node.comparators]
    values = [value for expression in expressions for value in _container_strings(expression)]
    values.extend(
        expression.value
        for expression in expressions
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str)
    )
    return any(value == "无" or value.startswith("暂无") for value in values) or any(
        isinstance(expression, ast.Name) and expression.id in placeholder_tables
        for expression in expressions
    )


def _regex_aliases(tree: ast.AST) -> tuple[set[str], set[str], set[str]]:
    module_aliases = {"re"}
    predicate_aliases: set[str] = set()
    compile_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "re":
                    module_aliases.add(item.asname or item.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "re":
            for item in node.names:
                if item.name in REGEX_PREDICATES:
                    predicate_aliases.add(item.asname or item.name)
                elif item.name == "compile":
                    compile_aliases.add(item.asname or item.name)
    return module_aliases, predicate_aliases, compile_aliases


def _compiled_regex_names(
    tree: ast.AST,
    module_aliases: set[str],
    compile_aliases: set[str],
) -> set[str]:
    result: set[str] = set()
    changed = True
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            compiled = (
                isinstance(value, ast.Call)
                and (
                    (
                        isinstance(value.func, ast.Attribute)
                        and value.func.attr == "compile"
                        and isinstance(value.func.value, ast.Name)
                        and value.func.value.id in module_aliases
                    )
                    or (isinstance(value.func, ast.Name) and value.func.id in compile_aliases)
                )
                and bool(value.args)
                and _argument_uses_cjk_literal(value.args[0], set())
            )
            alias = isinstance(value, ast.Name) and value.id in result
            if not compiled and not alias:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return result


def _old_task_kind_fallback(node: ast.AST) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    fields = {
        argument.value
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "get"
        and item.args
        for argument in item.args[:1]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    return {"task_kind", "kind"}.issubset(fields)


def _function_reads_both_task_kind_fields(nodes: list[ast.AST]) -> bool:
    fields = {
        argument.value
        for node in nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        for argument in node.args[:1]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    return {"task_kind", "kind"}.issubset(fields)


def _reads_named_field(node: ast.AST, field: str) -> bool:
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "get"
        and item.args
        and isinstance(item.args[0], ast.Constant)
        and item.args[0].value == field
        for item in ast.walk(node)
    )


def _question_text_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    nodes: list[ast.AST],
) -> tuple[set[str], set[str]]:
    """区分问题集合和单条问题文字，精确成员选择可以通过，两个文字互相包含必须拦截。"""

    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    collections = {
        argument.arg
        for argument in arguments
        if argument.arg == "questions" or argument.arg.endswith("_questions")
    }
    scalars: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            is_collection = _reads_named_field(value, "questions") or (
                isinstance(value, ast.Name) and value.id in collections
            )
            is_scalar = any(
                isinstance(item, ast.Name) and item.id in scalars
                for item in ast.walk(value)
            )
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if is_collection and target.id not in collections:
                    collections.add(target.id)
                    changed = True
                elif is_scalar and target.id not in scalars:
                    scalars.add(target.id)
                    changed = True
    for node in nodes:
        generators: list[ast.comprehension] = []
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            generators = node.generators
        for generator in generators:
            reads_questions = _reads_named_field(generator.iter, "questions") or (
                isinstance(generator.iter, ast.Name) and generator.iter.id in collections
            )
            if reads_questions and isinstance(generator.target, ast.Name):
                scalars.add(generator.target.id)
        if isinstance(node, (ast.For, ast.AsyncFor)):
            reads_questions = _reads_named_field(node.iter, "questions") or (
                isinstance(node.iter, ast.Name) and node.iter.id in collections
            )
            if reads_questions and isinstance(node.target, ast.Name):
                scalars.add(node.target.id)
    # 先识别迭代出来的单条问题，再继续追踪它经过包装函数或字符串方法后的别名。
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not any(isinstance(item, ast.Name) and item.id in scalars for item in ast.walk(node.value)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in scalars:
                    scalars.add(target.id)
                    changed = True
    return collections, scalars


def _question_fragment_comparison(node: ast.AST, collections: set[str], scalars: set[str]) -> bool:
    if not collections or not isinstance(node, ast.Compare):
        return False
    for operator, comparator in zip(node.ops, node.comparators):
        if not isinstance(operator, (ast.In, ast.NotIn)):
            continue
        if not isinstance(node.left, ast.Name) or not isinstance(comparator, ast.Name):
            continue
        if node.left.id in collections or comparator.id in collections:
            continue
        if node.left.id in scalars or comparator.id in scalars:
            return True
    return False


def _feedback_field_aliases(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    nodes: list[ast.AST],
) -> dict[str, str]:
    """追踪反馈合同字段别名，避免先赋值再补默认值绕过检查。"""

    aliases = {
        argument.arg: argument.arg
        for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        if argument.arg in FEEDBACK_CONTRACT_FIELDS
    }
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            field = next(
                (candidate for candidate in FEEDBACK_CONTRACT_FIELDS if _reads_named_field(node.value, candidate)),
                "",
            )
            if not field and isinstance(node.value, ast.Name):
                field = aliases.get(node.value.id, "")
            if not field:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != field:
                    aliases[target.id] = field
                    changed = True
    return aliases


def _feedback_default_names(tree: ast.AST) -> set[str]:
    """追踪反馈有效默认值常量及别名，避免把字面量搬到模块常量后绕过检查。"""

    result: set[str] = set()
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            is_default = (
                isinstance(value, ast.Constant) and value.value in FEEDBACK_CONTRACT_DEFAULTS
            ) or (isinstance(value, ast.Name) and value.id in result)
            if not is_default:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return result


def _has_feedback_default(node: ast.AST, default_names: set[str]) -> bool:
    return any(
        (isinstance(item, ast.Constant) and item.value in FEEDBACK_CONTRACT_DEFAULTS)
        or (isinstance(item, ast.Name) and item.id in default_names)
        for item in ast.walk(node)
    )


def _feedback_contract_auto_default(
    node: ast.AST,
    aliases: dict[str, str],
    default_names: set[str],
) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"get", "setdefault"} and node.args:
            field = node.args[0]
            if (
                isinstance(field, ast.Constant)
                and field.value in FEEDBACK_CONTRACT_FIELDS
                and len(node.args) > 1
                and _has_feedback_default(node.args[1], default_names)
            ):
                return True
    if not isinstance(node, (ast.BoolOp, ast.IfExp)):
        return False
    values = node.values if isinstance(node, ast.BoolOp) else [node.test, node.body, node.orelse]
    # 只把“字段值或别名直接参与兜底表达式”判成自动补齐，合同版本比较和状态校验不能误报。
    reads_field = any(
        (isinstance(value, ast.Name) and value.id in aliases)
        or (isinstance(value, ast.Call) and any(_reads_named_field(value, field) for field in FEEDBACK_CONTRACT_FIELDS))
        for value in values
    )
    has_valid_default = _has_feedback_default(node, default_names)
    return reads_field and has_valid_default


def semantic_violations(source: str, relative_path: str) -> list[str]:
    """找出把自由文本当状态、任务类型或空值合同的代码，不依赖函数名和常量名。"""

    tree = ast.parse(source, filename=relative_path)
    table_names = _cjk_tables(tree)
    placeholder_tables = _display_placeholder_tables(tree)
    regex_module_aliases, regex_predicate_aliases, regex_compile_aliases = _regex_aliases(tree)
    compiled_regex_names = _compiled_regex_names(tree, regex_module_aliases, regex_compile_aliases)
    feedback_default_names = _feedback_default_names(tree)
    violations: list[str] = []
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        nodes = _function_nodes(function)
        tainted = _tainted_names(function, nodes)
        cjk_iteration_names = _cjk_iteration_names(nodes, table_names)
        question_collections, question_scalars = _question_text_names(function, nodes)
        feedback_aliases = _feedback_field_aliases(function, nodes)
        if _function_reads_both_task_kind_fields(nodes):
            first = next(node for node in nodes if isinstance(node, ast.Call))
            violations.append(f"{relative_path}:{first.lineno} {function.name} 同时读取 task_kind 和旧 kind")
        for node in nodes:
            if _old_task_kind_fallback(node):
                violations.append(f"{relative_path}:{node.lineno} {function.name} 回退读取旧任务 kind")
            if _question_fragment_comparison(node, question_collections, question_scalars):
                violations.append(f"{relative_path}:{node.lineno} {function.name} 用片段包含关系选择自然语言问题")
            if _feedback_contract_auto_default(node, feedback_aliases, feedback_default_names):
                violations.append(f"{relative_path}:{node.lineno} {function.name} 在反馈合同缺失时自动补有效状态")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in TEXT_PREDICATE_METHODS
                and (
                    _reads_free_text_field(node.func.value, tainted)
                    or any(_argument_uses_cjk_literal(argument, table_names) for argument in node.args)
                )
            ):
                if (relative_path, function.name) not in ALLOWED_CJK_PREFIX:
                    violations.append(f"{relative_path}:{node.lineno} {function.name} 对自由文本调用 {node.func.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in regex_module_aliases
                and node.func.attr in REGEX_PREDICATES
                and (
                    any(_reads_free_text_field(argument, tainted) for argument in node.args)
                    or (node.args and _argument_uses_cjk_literal(node.args[0], table_names))
                )
            ):
                if (relative_path, function.name) not in ALLOWED_CJK_REGEX:
                    violations.append(f"{relative_path}:{node.lineno} {function.name} 用正则判断自由文本")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in regex_predicate_aliases
                and (
                    any(_reads_free_text_field(argument, tainted) for argument in node.args)
                    or (node.args and _argument_uses_cjk_literal(node.args[0], table_names))
                )
            ):
                if (relative_path, function.name) not in ALLOWED_CJK_REGEX:
                    violations.append(f"{relative_path}:{node.lineno} {function.name} 用正则别名判断自由文本")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in compiled_regex_names
                and node.func.attr in REGEX_PREDICATES
                and any(_reads_free_text_field(argument, tainted) for argument in node.args)
            ):
                if (relative_path, function.name) not in ALLOWED_CJK_REGEX:
                    violations.append(f"{relative_path}:{node.lineno} {function.name} 用编译正则判断自由文本")
            if (
                isinstance(node, ast.Call)
                and not isinstance(node.func, ast.Attribute)
                and any(_reads_free_text_field(argument, tainted) for argument in node.args)
                and any(_argument_uses_cjk_literal(argument, table_names) for argument in node.args)
            ):
                violations.append(f"{relative_path}:{node.lineno} {function.name} 经包装函数判断自由文本")
            if isinstance(node, ast.Compare) and _comparison_uses_cjk_table(
                node,
                table_names,
                cjk_iteration_names,
            ):
                if (relative_path, function.name) not in ALLOWED_CJK_MEMBERSHIP:
                    violations.append(f"{relative_path}:{node.lineno} {function.name} 用中文词表做成员判断")
            if isinstance(node, ast.Compare) and _display_placeholder_comparison(node, placeholder_tables):
                violations.append(f"{relative_path}:{node.lineno} {function.name} 回读中文展示空值")
    return list(dict.fromkeys(violations))


def test_production_has_no_legacy_free_text_fact_parser() -> None:
    assert not (SOURCE_ROOT / "core" / "fact_contract.py").exists()


def test_production_has_no_forbidden_semantic_functions_or_word_tables() -> None:
    violations: list[str] = []
    for path in production_python_files():
        relative_path = str(path.relative_to(ROOT))
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FORBIDDEN_FUNCTIONS:
                violations.append(f"{relative_path}:{node.lineno} 函数 {node.name}")
        violations.extend(semantic_violations(source, relative_path))
    assert not violations, "生产代码仍包含自然语言判断入口：\n" + "\n".join(violations)


def test_structured_contract_and_reference_locator_only_check_explicit_structure() -> None:
    """新公共模块必须持续经过同一套生产代码扫描，不能在公共层夹带自然语言判断。"""

    for relative_path in (
        "core/structured_contract.py",
        "core/reference_locator.py",
    ):
        path = SOURCE_ROOT / relative_path
        source = path.read_text(encoding="utf-8")
        assert semantic_violations(source, f"src/codex_sdlc/{relative_path}") == []


def test_detector_cannot_be_bypassed_by_renaming_function_or_word_table() -> None:
    source = '''
import re
WORDS = {"修复", "排错"}
def arbitrary_name(task):
    message = str(task.get("summary") or "")
    return message.startswith("修复") or message in WORDS or bool(re.search("排错", message))
'''
    violations = semantic_violations(source, "src/codex_sdlc/example.py")
    assert any("startswith" in item for item in violations)
    assert any("中文词表" in item for item in violations)
    assert any("正则" in item for item in violations)


def test_detector_allows_explicit_structure_and_exact_machine_markers() -> None:
    source = '''
def arbitrary_name(task, value):
    return (task.get("task_kind") == "fix", value == "__PENDING__")
def select_exact_question(questions, needle):
    return needle in questions
'''
    assert semantic_violations(source, "src/codex_sdlc/example.py") == []


def test_detector_blocks_generators_wrappers_dicts_regex_aliases_and_old_kind_fallback() -> None:
    samples = {
        "生成器": '''
TERMS = {"修复", "排错"}
def classify(summary):
    return any(term in summary for term in TERMS)
''',
        "包装函数": '''
def includes(value, term):
    return term in value
def classify(summary):
    return includes(summary, "修复")
''',
        "包装函数返回值": '''
def normalize(value):
    return value.strip()
def classify(summary):
    message = normalize(summary)
    return message.startswith("修复")
''',
        "字典词表": '''
WORDS = {"修复": "fix", "复查": "audit"}
def classify(summary):
    return summary in WORDS
''',
        "正则别名": '''
import re as alias
def classify(summary):
    return bool(alias.search("修复", summary))
''',
        "编译正则": '''
import re as alias
PATTERN = alias.compile("修复")
def classify(summary):
    return bool(PATTERN.search(summary))
''',
        "导入编译正则": '''
from re import compile as make_pattern
PATTERN = make_pattern("修复")
def classify(summary):
    return bool(PATTERN.search(summary))
''',
        "旧类型回退": '''
def classify(task):
    return task.get("task_kind") or task.get("kind") or "generic"
''',
        "展示空值回读": '''
def classify(lessons):
    return lessons != ["暂无需求级经验"]
''',
        "展示空值别名": '''
EMPTY_LESSONS = ["暂无需求级经验"]
DISPLAY_EMPTY = EMPTY_LESSONS
def classify(lessons):
    return lessons != DISPLAY_EMPTY
''',
        "问题片段匹配": '''
def choose(draft, needle):
    questions = [str(item) for item in draft.get("questions", [])]
    selector = needle.strip()
    return [item for item in questions if selector in item or item in selector]
''',
        "反馈合同缺失自动补齐": '''
def normalize(item):
    version = item.get("feedback_contract_version") or "feedback.v1"
    state = item.get("feedback_state") or "none"
    return version, state
''',
        "反馈合同别名自动补齐": '''
DEFAULT_VERSION = "feedback.v1"
EMPTY_STATE = "none"
VERSION_ALIAS = DEFAULT_VERSION
def normalize(item):
    version = item.get("feedback_contract_version")
    state = item.get("feedback_state")
    return version or VERSION_ALIAS, state or EMPTY_STATE
''',
    }
    for name, source in samples.items():
        assert semantic_violations(source, "src/codex_sdlc/example.py"), f"{name}没有被架构门禁识别"


def test_key_flows_only_use_explicit_structures() -> None:
    task = (SOURCE_ROOT / "commands" / "task_cmd.py").read_text(encoding="utf-8")
    pack = (SOURCE_ROOT / "core" / "task_pack_contract.py").read_text(encoding="utf-8")
    material = (SOURCE_ROOT / "commands" / "material_cmd.py").read_text(encoding="utf-8")
    codegraph = (SOURCE_ROOT / "core" / "codegraph_context.py").read_text(encoding="utf-8")
    assets = (SOURCE_ROOT / "core" / "codex_assets.py").read_text(encoding="utf-8")

    assert "FORMAL_REPORT_RESULT_WORDS" not in task
    assert "NEGATIVE_RESULT_PATTERN" not in task
    assert "CHANGE_ID_PATTERN.findall(task_text)" not in pack
    assert "supersedes_rules" not in pack
    assert "for raw in re.findall" not in material
    assert "CHINESE_SEARCH_PHRASES" not in codegraph
    assert "FEEDBACK_KEYWORDS" not in assets
    assert "negative_phrases" not in assets


def test_internal_audit_fix_and_closeout_kinds_stay_explicit() -> None:
    assert task_kind({"task_kind": "audit"}) == "audit"
    assert task_kind({"task_kind": "fix"}) == "fix"
    assert task_kind({"task_kind": "closeout"}) == "closeout"
    assert is_fix_task({"task_kind": "fix", "title": "普通任务", "summary": "普通摘要"}) is True
    assert is_fix_task({"kind": "fix", "title": "普通任务", "summary": "普通摘要"}) is False
    assert is_fix_task({"task_kind": "generic", "title": "修复 T-001", "summary": "修复已完成任务"}) is False
    assert task_regression_scope({"task_kind": "generic", "source_task_id": "FIX-T-001"}) == [
        "按本任务涉及文件和用户可见行为做局部回归"
    ]
    assert task_regression_scope({"task_kind": "fix", "source_task_id": "FIX-T-001"}) == ["回归修复来源：T-001"]
    assert task_kind({"kind": "fix"}) == "generic"
    assert task_is_ui_report({"kind": "visual"}) is False
    assert task_is_ui_report({"task_kind": "ui"}) is True


def test_command_lists_do_not_infer_manual_mode_from_command_text() -> None:
    command = "请手动执行 pytest -q"
    assert usable_test_commands({"project": {"test_commands": [command]}}) == [command]
