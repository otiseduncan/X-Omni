"""Compatibility hook for the retired regex research-route guard.

The model selects research capabilities from their structured schemas. Source
and repair-order safety remain enforced by the tool gateway and handlers.
"""

from __future__ import annotations

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    _INSTALLED = True
