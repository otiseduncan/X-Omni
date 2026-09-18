"""ALLDATA is sunset: preserved in source control, not executable.

X has no active way to reach ALLDATA. The adapters, the Navigator, the
licensed browser, the credential vault, historical captures, provenance, and
old verified durable claims all stay in the repository and in ADAS SI; what
is gone is every path that could run them:

* no research source order, delegate schema, or model prompt names ALLDATA;
* no ALLDATA tool is model-visible or discoverable through capability_search,
  in any profile, and the tool policy blocks each one (fail closed);
* no Calibration IQ research falls back to it;
* every runtime entry point that could open the licensed browser, read the
  saved ALLDATA credential, or ask ScrapeX to drive an ALLDATA Navigator task
  calls :func:`refuse` first, so a stray caller, a retry, or a test double
  that reaches one gets :class:`AlldataSunset` instead of a session.

Re-enabling ALLDATA is a deliberate code change -- this constant, the tool
policy, the schemas, and the source order -- followed by a redeploy. No
environment variable, setting, or model argument can turn it back on.
"""

from __future__ import annotations

from typing import Any, NoReturn

ALLDATA_SUNSET = True

# Model-facing capabilities that existed only to reach ALLDATA. They stay
# configured (and ``blocked``) so the gateway fails closed on a direct call.
SUNSET_TOOLS: frozenset[str] = frozenset(
    {
        "alldata_service_information",
        "collision_research",
        "research_provider_setup",
        "scrapex_navigator",
        "service_information_research",
        "adas_si_harvest",
        "adas_si_harvest_status",
    }
)

SUNSET_MESSAGE = (
    "ALLDATA is retired in X: it is kept for reference but is not available, so "
    "nothing was opened, signed in to, or searched there."
)


class AlldataSunset(RuntimeError):
    """Raised by every ALLDATA runtime entry point."""

    def __init__(self, entry_point: str):
        self.entry_point = entry_point
        super().__init__(f"{SUNSET_MESSAGE} (blocked at {entry_point})")


def refuse(entry_point: str) -> NoReturn:
    """Stop an ALLDATA runtime path before it touches a browser, credential, or ScrapeX."""

    raise AlldataSunset(entry_point)


def sunset_result(entry_point: str, **extra: Any) -> dict[str, Any]:
    """The structured result a tool-shaped ALLDATA path returns instead of running."""

    return {
        "status": "unavailable",
        "success": False,
        "executed": False,
        "verified": False,
        "provider": "alldata",
        "sunset": True,
        "blocked_at": entry_point,
        "authentication_required": False,
        "requires_human": False,
        "message": SUNSET_MESSAGE,
        **extra,
    }


def is_sunset_tool(name: Any) -> bool:
    return str(name or "") in SUNSET_TOOLS


__all__ = [
    "ALLDATA_SUNSET",
    "AlldataSunset",
    "SUNSET_MESSAGE",
    "SUNSET_TOOLS",
    "is_sunset_tool",
    "refuse",
    "sunset_result",
]
