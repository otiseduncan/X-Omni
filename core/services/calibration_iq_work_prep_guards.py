"""Narrow safety and compatibility corrections for Calibration IQ work prep.

These guards keep the bridge production-scoped, preserve ADAS Map authority,
accept observed field dictation, and let an inline ALLDATA card survive a Core
restart without hammering a dead in-memory browser session id.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from . import calibration_iq_work_prep as prep
from . import research_operator


_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _strict_adas_map_marker(value: Any, path: tuple[str, ...]) -> bool:
    if any(prep._ADAS_MAP_MARKER_RE.search(part.replace("_", " ")) for part in path):  # noqa: SLF001
        return True
    if not isinstance(value, dict):
        return False
    for key in (
        "provider",
        "source",
        "source_name",
        "title",
        "document_type",
        "name",
        "label",
    ):
        raw = value.get(key)
        if isinstance(raw, str) and prep._ADAS_MAP_MARKER_RE.search(raw):  # noqa: SLF001
            return True
    return False


def _safe_phase(text: object):
    """Parse numeric or spoken phase values without eager fallback evaluation."""
    match = prep._PHASE_RE.search(str(text or ""))  # noqa: SLF001
    if not match:
        return None
    token = match.group("phase").casefold()
    if token in prep._PHASE_WORDS:  # noqa: SLF001
        return prep._PHASE_WORDS[token]  # noqa: SLF001
    try:
        return str(int(token))
    except ValueError:
        return None


def _policy_declares_work_prep(policy_path: object) -> bool:
    """Only production policies that explicitly declare the tool may expose it."""
    try:
        raw = yaml.safe_load(Path(str(policy_path)).read_text(encoding="utf-8")) or {}
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return False
    tools = raw.get("tools") if isinstance(raw, dict) else None
    return isinstance(tools, dict) and prep.TOOL_NAME in tools


async def _restore_browser_session_if_stale(browser: Any, session_id: str) -> None:
    """Rehydrate a rendered inline ALLDATA card after a Core process restart.

    The browser profile is persistent but the in-memory session id is not. A
    stale card is owner-authenticated by the HTTP route already; only adopt its
    original UUID-shaped id when there is no competing live browser session.
    """
    requested = str(session_id or "").strip().casefold()
    if not _SESSION_ID_RE.fullmatch(requested):
        return
    if browser._page is not None or browser._session_id is not None:  # noqa: SLF001
        return
    await browser.start(auto_login=True)
    if browser._page is not None:  # noqa: SLF001
        browser._session_id = requested  # noqa: SLF001


def install() -> None:
    # Speech-to-text has produced "8 oz quick reference" and "8 ass quick
    # reference" in real field turns (both mishearings of "ADAS"). Keep this
    # deliberately specific to Quick Reference; it must not make arbitrary
    # "8 oz"/"8 ass" utterances enter the licensed research lane.
    prep._QUICK_REFERENCE_RE = re.compile(  # noqa: SLF001
        r"\b(?:adas|ados|a\s*d\s*a\s*s|8\s*oz|8\s*ass)\s+quick\s+reference\b|"
        r"\bquick\s+reference\b.{0,60}\b(?:adas|ados|a\s*d\s*a\s*s)\b",
        re.IGNORECASE | re.DOTALL,
    )
    prep._node_has_adas_map_marker = _strict_adas_map_marker  # noqa: SLF001
    prep._phase = _safe_phase  # noqa: SLF001

    # Work prep used to inject itself into every Registry instance. That made a
    # tiny isolated test policy advertise a production tool it never declared.
    # Keep the existing handler wrapper, but remove it from registries whose
    # source policy does not explicitly opt in.
    from ..tools import registry as registry_mod

    previous_init = registry_mod.Registry.__init__
    if not getattr(previous_init, "_xomni_work_prep_policy_guard", False):
        def registry_init(self, *args, **kwargs):
            policy_path = args[0] if args else kwargs.get("policy_path")
            declared = _policy_declares_work_prep(policy_path)
            previous_init(self, *args, **kwargs)
            if not declared:
                self.policy.pop(prep.TOOL_NAME, None)
                self._handlers.pop(prep.TOOL_NAME, None)  # noqa: SLF001

        registry_init._xomni_work_prep_policy_guard = True  # type: ignore[attr-defined]
        registry_mod.Registry.__init__ = registry_init

    # A Core restart invalidates only the in-memory browser session id; the
    # persistent ALLDATA Chrome profile remains. Rehydrate that session lazily
    # from the existing inline card instead of returning 404 every ~1.8 seconds.
    previous_screenshot = research_operator.LicensedBrowser.screenshot
    if not getattr(previous_screenshot, "_xomni_stale_session_resume", False):
        async def screenshot(self, session_id: str):
            await _restore_browser_session_if_stale(self, session_id)
            return await previous_screenshot(self, session_id)

        screenshot._xomni_stale_session_resume = True  # type: ignore[attr-defined]
        research_operator.LicensedBrowser.screenshot = screenshot

    previous_human_action = research_operator.LicensedBrowser.human_action
    if not getattr(previous_human_action, "_xomni_stale_session_resume", False):
        async def human_action(self, session_id: str, payload: dict[str, Any]):
            await _restore_browser_session_if_stale(self, session_id)
            return await previous_human_action(self, session_id, payload)

        human_action._xomni_stale_session_resume = True  # type: ignore[attr-defined]
        research_operator.LicensedBrowser.human_action = human_action
