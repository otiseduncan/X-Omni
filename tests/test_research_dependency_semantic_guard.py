from types import SimpleNamespace

from core.services import research_dependency_semantic_guard as guard


def test_dependency_guard_adds_normal_path_contract_once():
    module = SimpleNamespace(REVIEW_SYSTEM_PROMPT="base reviewer prompt")

    guard.install(module)
    first = module.REVIEW_SYSTEM_PROMPT
    guard.install(module)

    assert module.REVIEW_SYSTEM_PROMPT == first
    assert "NORMAL PATH" in first
    assert "Conditional fault-handling and repair branches are not dependencies" in first
    assert "DTC check/clear" in first
    assert "removal" in first
    assert "installation" in first
    assert "CONTINUE_SEARCH or REJECT rather than FOLLOW_DEPENDENCY" in first
    assert "ordinary successful procedure" in first


def test_dependency_guard_does_not_add_semantic_code_or_routing_contract():
    module = SimpleNamespace(REVIEW_SYSTEM_PROMPT="base")
    guard.install(module)

    # The installer changes only the review prompt. It does not wrap the
    # reviewer, classify titles, inspect browser state, or manufacture a
    # dependency decision in Python.
    assert set(vars(module)) == {
        "REVIEW_SYSTEM_PROMPT",
        guard._INSTALLED_ATTR,  # noqa: SLF001 - contract-level test
    }
