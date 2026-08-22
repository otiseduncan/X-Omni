"""Make collision-research audit status reflect verified source execution."""

from __future__ import annotations

import threading
from typing import Any, Optional

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        from ..tools import registry as registry_mod

        previous = registry_mod.Registry._approved_result_error
        if not getattr(previous, "_xomni_research_truth", False):
            def approved_result_error(name: str, result: Any) -> Optional[str]:
                if name == "collision_research" and isinstance(result, dict):
                    action = str(result.get("action") or "").casefold()
                    if action == "full_research":
                        status = str(result.get("status") or "").casefold()
                        if status == "failed" or result.get("external_search_verified") is not True:
                            return str(
                                result.get("error")
                                or "Post-collision research did not verify any external source search."
                            )
                        # partial_success is truthful execution: at least one
                        # external lane was verified, while the source ledger
                        # carries the incomplete lane explicitly.
                        return None
                return previous(name, result)

            approved_result_error._xomni_research_truth = True  # type: ignore[attr-defined]
            registry_mod.Registry._approved_result_error = staticmethod(approved_result_error)

        _INSTALLED = True
