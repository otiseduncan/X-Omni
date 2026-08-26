from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "core" / "services"
LOOP = ROOT / "core" / "orchestrator" / "loop.py"

SEMANTIC_ROUTING_PATHS = (
    LOOP,
    SERVICES / "calibration_iq_work_prep.py",
    SERVICES / "research_auto_acquire.py",
    SERVICES / "research_calibration_route.py",
    SERVICES / "research_task_continuity.py",
    SERVICES / "research_workflow.py",
)

_CONVERSATIONAL_ARGUMENTS = {
    "message",
    "request_text",
    "user_message",
    "user_prompt",
    "user_text",
    "utterance",
}
_ROUTING_VERBS = {
    "choose",
    "classify",
    "detect",
    "dispatch",
    "infer",
    "pick",
    "route",
    "select",
}
_ROUTING_OBJECTS = {"capability", "intent", "request", "route", "tool"}
_TEXT_MATCH_METHODS = {
    "endswith",
    "findall",
    "finditer",
    "fullmatch",
    "match",
    "search",
    "startswith",
}
_REGEX_MATCH_METHODS = {"findall", "finditer", "fullmatch", "match", "search"}


def _attribute_path(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _declared_tool_names() -> set[str]:
    """Read top-level tool keys without importing production configuration."""

    names: set[str] = set()
    in_tools = False
    config_text = (ROOT / "config" / "tools.yaml").read_text(encoding="utf-8")
    for line in config_text.splitlines():
        if line == "tools:":
            in_tools = True
            continue
        if not in_tools:
            continue
        if line and not line.startswith((" ", "#")):
            break
        if line.startswith("  ") and not line.startswith("    "):
            candidate = line.strip()
            if candidate.endswith(":"):
                names.add(candidate[:-1])
    return names


def _function_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef):
    """Walk one function without attributing nested-function code to its parent."""

    stack = list(reversed(function.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for item in target.elts for name in _target_names(item)}
    return set()


def _depends_on(node: ast.AST | None, names: set[str]) -> bool:
    if node is None:
        return False
    return any(isinstance(item, ast.Name) and item.id in names for item in ast.walk(node))


def _tainted_text_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    args = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    tainted = {arg.arg for arg in args if arg.arg in _CONVERSATIONAL_ARGUMENTS}
    tokens = set(function.name.casefold().split("_"))
    if tokens & _ROUTING_VERBS and tokens & _ROUTING_OBJECTS:
        tainted.update(arg.arg for arg in args if arg.arg in {"prompt", "query", "text"})

    assignments = [
        node
        for node in _function_nodes(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
    ]
    changed = True
    while changed:
        changed = False
        for assignment in assignments:
            value = assignment.value
            if not _depends_on(value, tainted):
                continue
            if isinstance(assignment, ast.Assign):
                targets = {
                    name for target in assignment.targets for name in _target_names(target)
                }
            else:
                targets = _target_names(assignment.target)
            new_names = targets - tainted
            if new_names:
                tainted.update(new_names)
                changed = True
    return tainted


def _routes_or_constructs_tool(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    tool_names: set[str],
) -> bool:
    tokens = set(function.name.casefold().split("_"))
    if tokens & _ROUTING_VERBS and tokens & _ROUTING_OBJECTS:
        return True

    for node in _function_nodes(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in tool_names:
                return True
        if not isinstance(node, ast.Call):
            continue
        called = _attribute_path(node.func).casefold()
        if called.endswith((".invoke", "._execute", ".execute_tool")):
            return True
    return False


def _literal_text(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in ast.walk(node)
    )


def _explicit_ui_protocol_match(node: ast.AST) -> bool:
    """Permit exact slash-command parsing, which is an explicit UI protocol."""

    literals = [
        item.value.strip()
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]
    return bool(literals) and all(
        literal.startswith(("/", "^/")) for literal in literals
    )


def _semantic_text_match(node: ast.AST, tainted: set[str]) -> str | None:
    if _explicit_ui_protocol_match(node):
        return None
    if isinstance(node, ast.Compare):
        operands = (node.left, *node.comparators)
        if any(_depends_on(operand, tainted) for operand in operands):
            if any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops):
                return "string membership"
            if _literal_text(node) and any(
                isinstance(operator, (ast.Eq, ast.NotEq)) for operator in node.ops
            ):
                return "literal comparison"

    if isinstance(node, ast.Call):
        called = _attribute_path(node.func).casefold()
        method = called.rsplit(".", 1)[-1]
        if method not in _TEXT_MATCH_METHODS:
            return None
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        inputs = [*node.args, *(keyword.value for keyword in node.keywords)]
        if receiver is not None:
            inputs.append(receiver)
        if any(_depends_on(value, tainted) for value in inputs) and (
            method in _REGEX_MATCH_METHODS or _literal_text(node)
        ):
            return f"{method} text match"
    return None


def _semantic_routing_violations(
    source: str,
    *,
    filename: str,
    tool_names: set[str],
) -> list[str]:
    """Find casual phrase/regex routers, not generic parsing or validation."""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = _tainted_text_names(function)
        if not tainted or not _routes_or_constructs_tool(function, tool_names):
            continue
        for node in _function_nodes(function):
            match_kind = _semantic_text_match(node, tainted)
            if match_kind:
                violations.append(
                    f"{filename}:{node.lineno}:{function.name}: {match_kind}"
                )
    return sorted(set(violations))


def test_production_orchestration_has_no_casual_semantic_tool_router() -> None:
    tool_names = _declared_tool_names()
    violations: list[str] = []
    for path in SEMANTIC_ROUTING_PATHS:
        violations.extend(
            _semantic_routing_violations(
                path.read_text(encoding="utf-8"),
                filename=str(path.relative_to(ROOT)),
                tool_names=tool_names,
            )
        )

    assert violations == [], (
        "Conversational meaning must come from model tool selection. Production "
        "orchestration may parse structured protocols and validate results, but "
        f"must not choose tools with user-text phrase/regex rules: {violations}"
    )


def test_semantic_router_guard_rejects_count_membership_choose_tool() -> None:
    source = '''
def choose_tool(message):
    normalized = message.casefold()
    if "count" in normalized:
        return "calibration_iq_summary"
    return None
'''

    violations = _semantic_routing_violations(
        source,
        filename="synthetic_orchestrator.py",
        tool_names={"calibration_iq_summary"},
    )

    assert any("choose_tool: string membership" in item for item in violations)


def test_semantic_router_guard_rejects_regex_tool_selection() -> None:
    source = '''
import re

def route_request(user_message):
    if re.search(r"\\bhow many\\b", user_message.casefold()):
        return "calibration_iq_summary"
    return None
'''

    violations = _semantic_routing_violations(
        source,
        filename="synthetic_orchestrator.py",
        tool_names={"calibration_iq_summary"},
    )

    assert any("route_request: search text match" in item for item in violations)


def test_semantic_router_guard_allows_protocol_regex_parsing() -> None:
    source = '''
import re

RECEIPT_DIGEST = re.compile(r"[0-9a-f]{64}")

def parse_execution_receipt(payload):
    return RECEIPT_DIGEST.fullmatch(str(payload["digest"])) is not None

def select_tool(message):
    if re.fullmatch(r"/(?:coder|omni)", message):
        return "switch_worker"
    return None
'''

    assert _semantic_routing_violations(
        source,
        filename="synthetic_protocol.py",
        tool_names={"calibration_iq_summary"},
    ) == []


def test_service_modules_do_not_wrap_or_replace_the_turn_loop() -> None:
    violations: list[str] = []
    for path in SERVICES.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if _attribute_path(node).endswith("Orchestrator._run"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == [], (
        "Service modules must register capabilities behind the gateway, not "
        f"read, wrap, or replace Orchestrator._run: {violations}"
    )


def test_retired_conversational_prerouters_are_not_production_functions() -> None:
    banned_by_file = {
        LOOP: {
            "deterministic_read_tool",
            "calibration_iq_read_request",
            "calibration_iq_research_request",
            "website_update_intent",
            "website_generation_intent",
            "image_generation_request",
            "web_research_request",
        },
        SERVICES / "calibration_iq_work_prep.py": {"classify_request"},
        SERVICES / "research_workflow.py": {
            "full_research_request",
            "preserve_requested",
        },
        SERVICES / "research_task_continuity.py": {
            "looks_like_continuation",
            "merge_active_task",
        },
        SERVICES / "research_auto_acquire.py": {
            "acquisition_candidate",
            "full_research_with_acquisition",
        },
    }

    violations: list[str] = []
    for path, banned in banned_by_file.items():
        found = sorted(_defined_functions(path) & banned)
        if found:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(found)}")

    assert violations == [], (
        "Conversational meaning and optional persistence belong to model tool "
        f"selection, not production classifiers: {violations}"
    )


def test_service_execution_does_not_call_query_intent_classifiers() -> None:
    violations: list[str] = []
    for path in SERVICES.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _attribute_path(node.func)
            if called.endswith("calibration_intent"):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == [], (
        "Source depth must come from an explicit structured mode, not a query "
        f"intent classifier: {violations}"
    )


def test_research_installers_are_registration_only() -> None:
    paths = (
        SERVICES / "research_setup.py",
        SERVICES / "research_workflow.py",
        SERVICES / "calibration_iq_work_prep.py",
        SERVICES / "research_task_continuity.py",
    )
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        install = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "install"
        )
        names = {node.id for node in ast.walk(install) if isinstance(node, ast.Name)}
        forbidden = sorted(names & {"user_message", "history", "classify_request"})
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(forbidden)}")

    assert violations == [], (
        "Capability installers may register schemas, handlers, policy, and cards; "
        f"they may not route conversational text: {violations}"
    )


def test_fixed_source_composite_installer_is_compatibility_only() -> None:
    path = SERVICES / "research_workflow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    install = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "install"
    )
    referenced = {
        node.id for node in ast.walk(install) if isinstance(node, ast.Name)
    }
    called_attributes = {
        _attribute_path(node.func)
        for node in ast.walk(install)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "registry_mod" not in referenced
    assert not any(name.endswith(".register") for name in called_attributes)
