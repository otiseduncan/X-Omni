"""Compatibility hook for the retired calibration intent pre-router."""

from __future__ import annotations

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    _INSTALLED = True
