"""Runtime preflight for ScrapeX Navigator task creation.

Navigator task creation is the first mutating call in an SI-research session.  If
ScrapeX is not already listening, issuing that POST first produces an
indeterminate mutation result and the model never receives a task id.  X Omni
already owns a revision-aware ``scrapex.start_native`` lifecycle, so use that
before the first task is created instead of requiring a separate manually kept
PowerShell process.

This module changes no navigation or semantic decisions.  It only ensures the
mechanical ScrapeX service is available before task creation.
"""

from __future__ import annotations

from typing import Any

_INSTALLED_ATTR = "__xomni_navigator_runtime_preflight_installed__"


def _startup_failure(startup: Any) -> dict[str, Any]:
    detail = startup if isinstance(startup, dict) else {}
    message = str(
        detail.get("message")
        or detail.get("detail")
        or (detail.get("error") or {}).get("message")
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


def install(module: Any) -> None:
    """Ensure ScrapeX is healthy before every Navigator ``create_task`` call."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original_navigator = module.navigator

    async def navigator_with_runtime_preflight(settings: Any, args: dict[str, Any]):
        if isinstance(args, dict) and args.get("action") == "create_task":
            startup = await module.start_native(settings)
            if not isinstance(startup, dict) or startup.get("success") is not True:
                return _startup_failure(startup)
        return await original_navigator(settings, args)

    module.navigator = navigator_with_runtime_preflight
    setattr(module, _INSTALLED_ATTR, True)
