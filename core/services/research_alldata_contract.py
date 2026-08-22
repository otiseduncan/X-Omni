"""Harden ALLDATA Repair/Collision navigation against portal/UI variations.

ALLDATA's documented workflow is vehicle-first:
Portal -> Repair/Collision -> Change/New Vehicle -> YYME/VIN -> Vehicle Information Search.
The generic browser operator may land on the portal home page where buttons or
selectors vary.  This layer preserves the authenticated session but falls back to
the canonical Repair URL and broadens semantic controls without bypassing any
provider access boundary.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any

from . import research_alldata_navigation as nav
from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_REPAIR_HOME = "https://my.alldata.com/repair/#/"


async def _frames(page: Any) -> list[Any]:
    values = [page]
    try:
        values.extend(list(getattr(page, "frames", []) or []))
    except Exception:
        pass
    output: list[Any] = []
    seen: set[int] = set()
    for value in values:
        if id(value) in seen:
            continue
        seen.add(id(value))
        output.append(value)
    return output


async def _click_any(page: Any, names: tuple[str, ...]) -> str | None:
    for frame in await _frames(page):
        for raw in names:
            pattern = re.compile(raw, re.IGNORECASE)
            for role in ("button", "link", "option", "menuitem", "tab"):
                try:
                    locator = frame.get_by_role(role, name=pattern).first
                    if not await locator.is_visible(timeout=350):
                        continue
                    label = ""
                    try:
                        label = " ".join((await locator.inner_text()).split()).strip()
                    except Exception:
                        pass
                    await locator.click(timeout=4_000)
                    await asyncio.sleep(0.7)
                    return label or raw
                except Exception:
                    continue
            try:
                locator = frame.get_by_text(pattern, exact=False).first
                if not await locator.is_visible(timeout=350):
                    continue
                label = " ".join((await locator.inner_text()).split()).strip()
                await locator.click(timeout=4_000)
                await asyncio.sleep(0.7)
                return label or raw
            except Exception:
                continue
    return None


async def _direct_repair(page: Any) -> bool:
    try:
        current = str(page.url or "")
    except Exception:
        current = ""
    if "/repair/" in current.casefold():
        return True
    try:
        await page.goto(_REPAIR_HOME, wait_until="domcontentloaded", timeout=15_000)
        await asyncio.sleep(1.0)
        return "/repair/" in str(page.url or "").casefold()
    except Exception:
        return False


async def hardened_enter_product(page: Any) -> bool:
    # Keep the normal semantic discovery first.
    try:
        if await _PREVIOUS_ENTER(page):
            return True
    except Exception:
        pass

    # Portal button names have changed over time; try a broader documented set.
    clicked = await _click_any(page, (
        r"^Repair\s*/\s*Collision$",
        r"^Repair\s+and\s+Collision$",
        r"^Repair\s*&\s*Collision$",
        r"^ALLDATA\s+Repair$",
        r"^Repair$",
        r"^Collision$",
    ))
    if clicked:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        if "/repair/" in str(page.url or "").casefold():
            return True

    # Canonical direct entry retains the existing authenticated browser profile.
    return await _direct_repair(page)


async def hardened_vehicle_box(page: Any) -> Any | None:
    selectors = (
        "input[placeholder*='Year, Make, Model' i]",
        "input[placeholder*='Year' i][placeholder*='Make' i]",
        "input[placeholder*='vehicle' i]",
        "input[placeholder*='YYME' i]",
        "input[placeholder*='YMME' i]",
        "input[placeholder*='VIN' i]",
        "input[aria-label*='Year, Make, Model' i]",
        "input[aria-label*='vehicle' i]",
        "input[aria-label*='YYME' i]",
        "input[aria-label*='YMME' i]",
        "input[aria-label*='VIN' i]",
        "input[name*='yyme' i]",
        "input[name*='ymme' i]",
        "input[id*='yyme' i]",
        "input[id*='ymme' i]",
        "input[name*='vehicle' i]",
        "input[id*='vehicle' i]",
    )

    try:
        existing = await _PREVIOUS_VEHICLE_BOX(page)
        if existing is not None:
            return existing
    except Exception:
        pass

    # ALLDATA documents both Change vehicle and New Vehicle entry points.
    for names in (
        (r"Change\s+vehicle", r"Change\s+Vehicle", r"Select\s+Vehicle", r"Choose\s+Vehicle"),
        (r"^New\s+Vehicle$", r"Add\s+Vehicle", r"Select\s+New\s+Vehicle"),
    ):
        await _click_any(page, names)
        box = await nav._first_visible(page, selectors, timeout=850)  # noqa: SLF001
        if box is not None:
            return box

    # If the current page drifted back to the portal, re-enter Repair and retry.
    if await _direct_repair(page):
        await _click_any(page, (r"Change\s+vehicle", r"^New\s+Vehicle$", r"Select\s+Vehicle"))
        return await nav._first_visible(page, selectors, timeout=900)  # noqa: SLF001
    return None


async def hardened_information_box(page: Any) -> Any | None:
    try:
        box = await _PREVIOUS_INFORMATION_BOX(page)
        if box is not None:
            return box
    except Exception:
        pass

    selectors = (
        "input[placeholder*='Search vehicle information' i]",
        "input[aria-label*='Search vehicle information' i]",
        "input[placeholder*='vehicle information' i]",
        "input[aria-label*='vehicle information' i]",
        "input[placeholder*='Search' i]:not([placeholder*='VIN' i]):not([placeholder*='Year' i]):not([placeholder*='vehicle' i])",
        "input[aria-label*='Search' i]:not([aria-label*='VIN' i]):not([aria-label*='Year' i])",
    )
    await _click_any(page, (
        r"Vehicle\s+Information\s+Search",
        r"Search\s+Vehicle\s+Information",
        r"Information\s+Search",
        r"^Search$",
    ))
    return await nav._first_visible(page, selectors, timeout=900)  # noqa: SLF001


def install() -> None:
    global _INSTALLED, _PREVIOUS_ENTER, _PREVIOUS_VEHICLE_BOX, _PREVIOUS_INFORMATION_BOX
    with _INSTALL_LOCK:
        if _INSTALLED:
            return

        _PREVIOUS_ENTER = nav._enter_product  # noqa: SLF001
        _PREVIOUS_VEHICLE_BOX = nav._vehicle_box  # noqa: SLF001
        _PREVIOUS_INFORMATION_BOX = nav._information_box  # noqa: SLF001
        nav._enter_product = hardened_enter_product  # noqa: SLF001
        nav._vehicle_box = hardened_vehicle_box  # noqa: SLF001
        nav._information_box = hardened_information_box  # noqa: SLF001

        # Ensure the already-installed workflow still points to the vehicle-first
        # search function that now uses the hardened globals above.
        research_workflow.search_alldata = nav.search_alldata_vehicle_first
        _INSTALLED = True


_PREVIOUS_ENTER = nav._enter_product  # noqa: SLF001
_PREVIOUS_VEHICLE_BOX = nav._vehicle_box  # noqa: SLF001
_PREVIOUS_INFORMATION_BOX = nav._information_box  # noqa: SLF001
