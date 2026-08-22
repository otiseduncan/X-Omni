"""Deterministic chat entrypoint for licensed research setup.

Saying "set up AllData", "add my AllData credentials", or equivalent must open
the secure inline card. This read-only entrypoint is separate from the broader
operator-authorized collision_research tool so deterministic routing does not
weaken the policy tier of browser/capture operations.
"""

from __future__ import annotations

import re
import threading
from typing import Any

_SETUP_RE = re.compile(
    r"\b(?:set\s*up|setup|configure|add|save|enter|update|change|login|log\s*in)\b"
    r".{0,80}\b(?:all\s*data|alldata)\b|"
    r"\b(?:all\s*data|alldata)\b.{0,80}\b(?:credential|credentials|password|login|log\s*in|setup|configure)\b",
    re.IGNORECASE | re.DOTALL,
)
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod
        from . import research_operator

        registry_mod.TOOL_SCHEMAS.setdefault(
            "research_provider_setup",
            {
                "description": (
                    "Open the secure ALLDATA credential and mobile authentication card in chat. "
                    "Use when the owner asks to set up, save, enter, update, or log in to ALLDATA. "
                    "The credential itself is collected by the UI and never enters model context."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        )

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_research_setup", False):
            def registry_init(self, *args, **kwargs):
                previous_init(self, *args, **kwargs)
                self.policy.setdefault(
                    "research_provider_setup",
                    {
                        "tier": "read_only",
                        "description": "Open secure ALLDATA credential setup in chat.",
                    },
                )

                async def handler(_args: dict[str, Any]):
                    from ..config import Settings
                    from . import adas_si as adas_si_mod
                    settings = Settings.load()
                    adas = adas_si_mod.AdasSI(
                        settings.adas_si_root,
                        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
                    )
                    browser = research_operator.get_browser(settings.root, adas=adas)
                    status = await browser.status()
                    status.update({
                        "action": "setup",
                        "setup_in_chat": True,
                        "credential_secret_in_model_context": False,
                    })
                    return status

                self.register("research_provider_setup", handler)

            registry_init._xomni_research_setup = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        try:
            from ..orchestrator import loop as loop_mod
            loop_mod.ARTIFACT_FOR_TOOL["research_provider_setup"] = "research_provider"
            previous_route = loop_mod.deterministic_read_tool
            if not getattr(previous_route, "_xomni_research_setup", False):
                def deterministic_read_tool(user_message: object):
                    text = str(user_message or "").strip()
                    if text and _SETUP_RE.search(text):
                        return "research_provider_setup"
                    return previous_route(user_message)

                deterministic_read_tool._xomni_research_setup = True  # type: ignore[attr-defined]
                loop_mod.deterministic_read_tool = deterministic_read_tool
        except Exception:  # noqa: BLE001 - loop may load after isolated service tests
            pass

        _INSTALLED = True
