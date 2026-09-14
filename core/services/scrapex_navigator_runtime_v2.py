"""Contract-preserving runtime preflight for ScrapeX Navigator task creation.

Production X Omni owns a local ScrapeX checkout and may start it when Navigator
research needs it.  The preflight must never run before request validation: an
unsupported provider or malformed create request is still a pure adapter error
and must not touch the runtime.  Likewise, lightweight/hermetic callers that do
not declare a managed ScrapeX project keep the original adapter behaviour.
"""

from __future__ import annotations

from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_runtime_preflight_v2_installed__"


def _startup_failure(startup: Any) -> dict[str, Any]:
    detail = startup if isinstance(startup, dict) else {}
    error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
    message = str(
        detail.get("message")
        or detail.get("detail")
        or error.get("message")
        or "ScrapeX could not be started for Navigator research."
    )
    return {
        "service": "ScrapeX",
        "action": "create_task",
        "status": "runtime_unavailable",
        "success": False,
        "executed": False,
        "verified": False,
        "error": {"code": "runtime_unavailable", "message": message},
        "startup": detail,
    }


def _managed_project(settings: Any) -> bool:
    """Only production-style settings opt into native runtime ownership."""
    value = getattr(settings, "scrapex_project_path", None)
    return value is not None and str(value).strip() != ""


def _valid_create_task(module: Any, args: Any) -> bool:
    """Mirror only the create-task shape needed before a lifecycle side effect.

    The canonical adapter still performs the authoritative validation.  This
    gate merely proves enough to decide whether starting a local process is
    allowed before delegating to that adapter.
    """
    if not isinstance(args, dict) or args.get("action") != "create_task":
        return False

    provider = args.get("provider")
    providers = getattr(module, "NAVIGATOR_PROVIDERS", frozenset({"alldata"}))
    if not isinstance(provider, str) or provider not in providers:
        return False

    target = args.get("target")
    if not isinstance(target, dict):
        return False
    allowed_target = {"year", "make", "model", "trim", "vin"}
    if set(target) - allowed_target:
        return False
    year = target.get("year")
    if year is not None and (
        isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100
    ):
        return False
    for field in ("make", "model", "trim", "vin"):
        value = target.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return False

    topic = args.get("topic")
    max_topic = int(getattr(module, "MAX_TOPIC_CHARS", 400))
    if not isinstance(topic, str) or not topic.strip() or len(topic.strip()) > max_topic:
        return False

    budget = args.get("action_budget")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 80
    ):
        return False

    return True


def install(module: Any) -> None:
    """Auto-start managed ScrapeX only for a validated Navigator create request."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_navigator = module.navigator

    async def navigator_with_runtime_preflight(settings: Any, args: dict[str, Any]):
        if (
            _managed_project(settings)
            and _valid_create_task(module, args)
        ):
            startup = await module.start_native(settings)
            if not isinstance(startup, dict) or startup.get("success") is not True:
                return _startup_failure(startup)
        return await original_navigator(settings, args)

    module.navigator = navigator_with_runtime_preflight
    setattr(module, _INSTALLED_ATTR, True)
