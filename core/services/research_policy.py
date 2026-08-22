"""Keep installed research handlers subordinate to the declared tool policy.

Feature modules register handlers dynamically to avoid invasive edits to the
large core registry, but registration must never become authorization. This
wrapper restores the gateway's invariant: if either research capability is
removed from ``config/tools.yaml``, its runtime policy entry is removed too and
Registry.tier() returns ``blocked``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import yaml

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_RESEARCH_TOOLS = frozenset({"research_provider_setup", "collision_research"})


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        previous_init = registry_mod.Registry.__init__
        if not getattr(previous_init, "_xomni_research_policy_guard", False):
            def registry_init(self, policy_path, *args, **kwargs):
                declared: set[str] = set()
                try:
                    raw = yaml.safe_load(Path(policy_path).read_text(encoding="utf-8")) or {}
                    configured = raw.get("tools") or {}
                    if isinstance(configured, dict):
                        declared = {str(name) for name in configured}
                except (OSError, ValueError, yaml.YAMLError):
                    # The core Registry owns authoritative parsing/error behavior.
                    # This guard only decides whether dynamic research policy may remain.
                    declared = set()

                previous_init(self, policy_path, *args, **kwargs)
                for name in _RESEARCH_TOOLS - declared:
                    self.policy.pop(name, None)

            registry_init._xomni_research_policy_guard = True  # type: ignore[attr-defined]
            registry_mod.Registry.__init__ = registry_init

        _INSTALLED = True
