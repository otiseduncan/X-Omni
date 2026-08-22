"""Compatibility boundary for optional authenticated-research HTTP routes.

Many existing route-unit tests deliberately construct tiny settings objects with
only the attributes required by the route under test.  The authenticated
research browser is different: it owns a persistent protected browser profile
under ``<X root>/data/credentials`` and therefore requires the real X Omni
application root.

Production ``Settings`` always has ``root``.  Test/minimal settings objects do
not, and adding unrelated research routes to those routers would both violate
their fixture contract and create persistent state during tests.  Keep the
feature optional at the router boundary instead of forcing every historical
fixture to pretend it has a production root.
"""

from __future__ import annotations

from typing import Any

from . import research_operator


def install() -> None:
    """Make research HTTP route installation conditional on a real app root."""
    original = research_operator.install_http_routes
    if getattr(original, "_xomni_root_compatible", False):
        return

    def compatible_install_http_routes(
        router: Any,
        settings: Any,
        require_session: Any,
        *,
        adas: Any | None = None,
    ) -> None:
        if getattr(settings, "root", None) is None:
            return
        original(router, settings, require_session, adas=adas)

    compatible_install_http_routes._xomni_root_compatible = True  # type: ignore[attr-defined]
    research_operator.install_http_routes = compatible_install_http_routes
