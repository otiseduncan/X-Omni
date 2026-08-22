"""Vehicle-aware ALLDATA Repair/Collision research automation.

ALLDATA's professional workflow is vehicle-first: enter Repair/Collision, select
the vehicle, then search vehicle information. The first integration only looked
for a generic search box on the portal home page, so an authenticated session
could still fail with "no searchable field".

This layer uses semantic labels/placeholders instead of brittle CSS classes. It
never bypasses authentication, CAPTCHA, subscription controls, or other access
boundaries; those remain handled by the existing mobile human-auth handoff.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Optional

from . import research_operator as ro
from . import research_workflow

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False

_YEAR_RE = re.compile(r"(?<!\d)(?P<year>20\d{2}|\d{2})(?!\d)")
_MAKES = (
    "Jeep", "Subaru", "Toyota", "Lexus", "Honda", "Acura", "Ford", "Lincoln",
    "Chevrolet", "GMC", "Cadillac", "Buick", "Nissan", "Infiniti", "Hyundai",
    "Kia", "Genesis", "BMW", "Mercedes-Benz", "Mercedes", "Volkswagen", "VW",
    "Audi", "Dodge", "Ram", "Chrysler", "Mazda", "Mitsubishi", "Volvo", "Porsche",
)
_TOPIC_BOUNDARY_RE = re.compile(
    r"\b(?:bsm|blind\s+spot|eyesight|adas|calibrat\w*|recalibrat\w*|aim\w*|"
    r"alignment|camera|radar|sensor|module|procedure|adjustment|beam\s+axis|"
    r"replacement|replace|collision|repair|position\s+statement)\b",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"\b(?:i|we|need|needs|the|a|an|for|on|of|to|get|me|please|procedure|"
    r"information|info|check|find|research|look|up|same|vehicle|car|this|that)\b",
    re.IGNORECASE,
)


def _normalize_year(raw: str) -> str:
    value = int(raw)
    if value < 100:
        value += 2000 if value <= 69 else 1900
    return str(value)


def vehicle_from_query(query: object) -> dict[str, Any]:
    text = " ".join(str(query or "").split()).strip()
    if not text:
        return {}

    year_match = _YEAR_RE.search(text)
    year = _normalize_year(year_match.group("year")) if year_match else ""

    make = ""
    try:
        from . import adas_si as adas_mod
        make = research_workflow.requested_make(text, adas_mod) or ""
    except Exception:
        pass
    if not make:
        folded = text.casefold()
        for candidate in _MAKES:
            if re.search(rf"(?<![a-z0-9]){re.escape(candidate.casefold())}(?![a-z0-9])", folded):
                make = "Volkswagen" if candidate == "VW" else (
                    "Mercedes-Benz" if candidate == "Mercedes" else candidate
                )
                break

    model_trim = ""
    if make:
        if make == "Volkswagen":
            make_match = re.search(r"\b(?:Volkswagen|VW)\b", text, re.IGNORECASE)
        elif make == "Mercedes-Benz":
            make_match = re.search(r"\b(?:Mercedes(?:-Benz)?)\b", text, re.IGNORECASE)
        else:
            make_match = re.search(rf"\b{re.escape(make)}\b", text, re.IGNORECASE)
        if make_match:
            tail = text[make_match.end():].strip(" ,:-")
            boundary = _TOPIC_BOUNDARY_RE.search(tail)
            if boundary:
                tail = tail[:boundary.start()]
            words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", tail)[:6]
            model_trim = " ".join(words).strip()

    label = " ".join(part for part in (year, make, model_trim) if part).strip()
    return {
        "year": year or None,
        "make": make or None,
        "model_trim": model_trim or None,
        "label": label or None,
    }


def topic_from_query(query: object, vehicle: Optional[dict[str, Any]] = None) -> str:
    text = " ".join(str(query or "").split()).strip()
    if not text:
        return ""
    vehicle = vehicle or vehicle_from_query(text)
    topic = text
    for token in sorted(str(vehicle.get("label") or "").split(), key=len, reverse=True):
        topic = re.sub(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
            " ",
            topic,
            flags=re.IGNORECASE,
        )
    topic = re.sub(r"\bBSM\b", "Blind Spot Monitor", topic, flags=re.IGNORECASE)
    topic = re.sub(r"\bBSD\b", "Blind Spot Detection", topic, flags=re.IGNORECASE)
    topic = _FILLER_RE.sub(" ", topic)
    topic = " ".join(topic.split()).strip(" ,.-")
    if re.search(r"blind\s+spot", topic, re.IGNORECASE) and not re.search(
        r"calibrat|adjust|aim", topic, re.IGNORECASE
    ):
        topic += " calibration"
    return topic[:220] or "calibration"


def topic_variants(topic: str) -> list[str]:
    base = " ".join(str(topic or "").split()).strip()
    output: list[str] = []

    def add(value: str) -> None:
        value = " ".join(value.split()).strip()
        if value and value.casefold() not in {item.casefold() for item in output}:
            output.append(value[:220])

    add(base)
    if re.search(r"blind\s+spot", base, re.IGNORECASE):
        add("Blind Spot Monitor calibration")
        add("Blind Spot Detection calibration")
        add("Blind Spot Monitor sensor adjustment")
        add("Blind Spot Module calibration")
    elif re.search(r"eyesight", base, re.IGNORECASE):
        add("EyeSight calibration")
        add("EyeSight camera adjustment")
    elif re.search(r"camera", base, re.IGNORECASE):
        add(base + " calibration")
    elif re.search(r"radar", base, re.IGNORECASE):
        add(base + " adjustment")
    return output[:5]


async def _visible(locator: Any, timeout: int = 400) -> bool:
    try:
        return bool(await locator.is_visible(timeout=timeout))
    except Exception:
        return False


async def _first_visible(page: Any, selectors: tuple[str, ...], timeout: int = 350) -> Any | None:
    frames = [page, *list(getattr(page, "frames", []) or [])]
    seen: set[int] = set()
    for frame in frames:
        if id(frame) in seen:
            continue
        seen.add(id(frame))
        for selector in selectors:
            try:
                loc = frame.locator(selector).first
            except Exception:
                continue
            if await _visible(loc, timeout=timeout):
                return loc
    return None


async def _click_named(page: Any, patterns: tuple[str, ...]) -> str | None:
    for raw in patterns:
        pattern = re.compile(raw, re.IGNORECASE)
        for role in ("link", "button", "option"):
            try:
                loc = page.get_by_role(role, name=pattern).first
            except Exception:
                continue
            if not await _visible(loc, timeout=350):
                continue
            try:
                label = " ".join((await loc.inner_text()).split()).strip()[:200]
            except Exception:
                label = raw
            try:
                await loc.click(timeout=4_000)
                await asyncio.sleep(0.6)
                return label
            except Exception:
                continue
        try:
            loc = page.get_by_text(pattern, exact=False).first
        except Exception:
            continue
        if await _visible(loc, timeout=350):
            try:
                label = " ".join((await loc.inner_text()).split()).strip()[:200]
                await loc.click(timeout=4_000)
                await asyncio.sleep(0.6)
                return label
            except Exception:
                continue
    return None


async def _enter_product(page: Any) -> bool:
    if await _first_visible(page, (
        "input[placeholder*='Year' i]",
        "input[placeholder*='VIN' i]",
        "input[aria-label*='YYME' i]",
        "input[placeholder*='Search' i]",
    )) is not None:
        return True
    clicked = await _click_named(page, (
        r"^ALLDATA\s+Repair$",
        r"^Repair$",
        r"Repair\s*/\s*Collision",
        r"^ALLDATA\s+Collision$",
        r"^Collision$",
    ))
    if clicked:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        return True
    return False


async def _vehicle_box(page: Any) -> Any | None:
    selectors = (
        "input[placeholder*='Year, Make, Model' i]",
        "input[placeholder*='Year' i][placeholder*='Make' i]",
        "input[placeholder*='VIN' i]",
        "input[aria-label*='Year, Make, Model' i]",
        "input[aria-label*='YYME' i]",
        "input[name*='yyme' i]",
        "input[id*='yyme' i]",
        "input[name*='vehicle' i]",
        "input[id*='vehicle' i]",
    )
    box = await _first_visible(page, selectors, timeout=500)
    if box is not None:
        return box
    await _click_named(page, (r"Change\s+Vehicle", r"Select\s+Vehicle", r"Choose\s+Vehicle"))
    return await _first_visible(page, selectors, timeout=700)


async def _click_vehicle_result(page: Any, vehicle: dict[str, Any]) -> str | None:
    year = str(vehicle.get("year") or "")
    make = str(vehicle.get("make") or "")
    model_trim = str(vehicle.get("model_trim") or "")
    must = [part.casefold() for part in (year, make) if part]
    preferred = [part.casefold() for part in model_trim.split() if len(part) >= 3]
    try:
        elements = page.locator("a, button, [role='option'], [role='link'], [role='row']")
        count = min(await elements.count(), 240)
    except Exception:
        return None

    candidates: list[tuple[int, int, Any, str]] = []
    for index in range(count):
        loc = elements.nth(index)
        if not await _visible(loc, timeout=120):
            continue
        try:
            text = " ".join((await loc.inner_text()).split()).strip()
        except Exception:
            continue
        if not 4 <= len(text) <= 350:
            continue
        folded = text.casefold()
        if must and not all(token in folded for token in must):
            continue
        score = sum(4 for token in must if token in folded)
        score += sum(3 for token in preferred if token in folded)
        candidates.append((score, -len(text), loc, text))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _score, _length, loc, text = candidates[0]
    try:
        await loc.click(timeout=5_000)
        await asyncio.sleep(0.8)
        return text[:300]
    except Exception:
        return None


async def _select_vehicle(page: Any, vehicle: dict[str, Any]) -> dict[str, Any]:
    label = str(vehicle.get("label") or "").strip()
    if not label:
        return {
            "selected": False,
            "reason": "The research query did not contain enough vehicle identity for ALLDATA vehicle selection.",
        }

    try:
        body = " ".join((await page.locator("body").inner_text(timeout=5_000)).split()).casefold()
    except Exception:
        body = ""
    identity_tokens = [
        str(vehicle.get(key) or "").casefold()
        for key in ("year", "make")
        if vehicle.get(key)
    ]
    model_token = next(
        (token.casefold() for token in str(vehicle.get("model_trim") or "").split() if len(token) >= 3),
        "",
    )
    if identity_tokens and all(token in body for token in identity_tokens) and (
        not model_token or model_token in body
    ):
        return {"selected": True, "vehicle_query": label, "already_selected": True}

    box = await _vehicle_box(page)
    if box is None:
        return {
            "selected": False,
            "vehicle_query": label,
            "reason": "ALLDATA vehicle selection field was not found.",
        }
    try:
        await box.fill(label)
        await asyncio.sleep(1.0)
    except Exception as exc:
        return {
            "selected": False,
            "vehicle_query": label,
            "reason": f"ALLDATA vehicle entry failed: {type(exc).__name__}.",
        }

    selected_text = await _click_vehicle_result(page, vehicle)
    if not selected_text:
        try:
            await box.press("Enter")
            await asyncio.sleep(0.8)
        except Exception:
            pass
        try:
            current = " ".join((await page.locator("body").inner_text(timeout=5_000)).split()).casefold()
        except Exception:
            current = ""
        if not all(token in current for token in identity_tokens):
            return {
                "selected": False,
                "vehicle_query": label,
                "reason": "ALLDATA did not expose a selectable result for the requested vehicle.",
            }
        selected_text = label
    return {"selected": True, "vehicle_query": label, "selected_result": selected_text}


async def _information_box(page: Any) -> Any | None:
    selectors = (
        "input[placeholder*='Search' i]:not([placeholder*='VIN' i]):not([placeholder*='Year' i])",
        "input[aria-label*='Search' i]:not([aria-label*='VIN' i]):not([aria-label*='Year' i])",
        "input[name*='search' i]",
        "input[id*='search' i]",
    )
    box = await _first_visible(page, selectors, timeout=500)
    if box is not None:
        return box
    await _click_named(page, (
        r"Vehicle\s+Information\s+Search",
        r"^Search$",
        r"Information\s+Search",
    ))
    return await _first_visible(page, selectors, timeout=700)


def _research_tokens(topic: str) -> list[str]:
    ignored = {
        "calibration", "procedure", "adjustment", "system", "monitor", "sensor",
        "the", "and", "for",
    }
    tokens = [
        token
        for token in re.findall(r"[a-z0-9-]+", topic.casefold())
        if len(token) >= 3 and token not in ignored
    ]
    if "calibration" in topic.casefold():
        tokens.append("calibration")
    if "adjustment" in topic.casefold():
        tokens.append("adjustment")
    return list(dict.fromkeys(tokens))[:16]


async def _click_best_result(page: Any, topic: str) -> tuple[str | None, int]:
    tokens = _research_tokens(topic)
    try:
        elements = page.locator("a, button, [role='link'], [role='option'], [role='treeitem']")
        count = min(await elements.count(), 320)
    except Exception:
        return None, 0

    choices: list[tuple[int, int, Any, str]] = []
    for index in range(count):
        loc = elements.nth(index)
        if not await _visible(loc, timeout=100):
            continue
        try:
            text = " ".join((await loc.inner_text()).split()).strip()
        except Exception:
            continue
        if not 4 <= len(text) <= 450:
            continue
        folded = text.casefold()
        score = sum(4 for token in tokens if token in folded)
        if "calibrat" in folded or "adjust" in folded or "aim" in folded:
            score += 4
        if "blind spot" in folded and "blind spot" in topic.casefold():
            score += 8
        if "service and repair" in folded or "procedure" in folded:
            score += 2
        if score > 0:
            choices.append((score, -len(text), loc, text))

    if not choices:
        return None, 0
    choices.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, _length, loc, text = choices[0]
    if score < 4:
        return None, score
    try:
        await loc.click(timeout=5_000)
        await asyncio.sleep(0.8)
        return text[:350], score
    except Exception:
        return None, score


async def search_alldata_vehicle_first(browser: Any, query: str) -> dict[str, Any]:
    state = await browser.start(auto_login=True)
    page = browser._page  # noqa: SLF001 - provider automation owned by this service
    if page is None:
        return {"attempted": True, "searched": False, "verified": False, "reason": "No active ALLDATA page."}
    if not state.get("authenticated"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "human_action_required": True,
            "reason": "ALLDATA requires a human authentication step before research can continue.",
            "status": state,
        }

    vehicle = vehicle_from_query(query)
    topic = topic_from_query(query, vehicle)
    entered = await _enter_product(page)
    if not entered:
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "vehicle": vehicle,
            "topic": topic,
            "reason": "X could not enter the ALLDATA Repair/Collision vehicle workflow from the authenticated portal.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
            "title": (await page.title())[:300],
        }

    vehicle_state = await _select_vehicle(page, vehicle)
    if not vehicle_state.get("selected"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "vehicle": vehicle,
            "topic": topic,
            "vehicle_selection": vehicle_state,
            "reason": vehicle_state.get("reason") or "ALLDATA vehicle selection was not verified.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
            "title": (await page.title())[:300],
        }

    search_box = await _information_box(page)
    if search_box is None:
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "vehicle": vehicle,
            "topic": topic,
            "vehicle_selection": vehicle_state,
            "reason": "The vehicle was selected, but ALLDATA Vehicle Information Search was not found.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
            "title": (await page.title())[:300],
        }

    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    for variant in topic_variants(topic):
        try:
            await search_box.fill(variant)
            await search_box.press("Enter")
            await asyncio.sleep(0.8)
            result_title, result_score = await _click_best_result(page, variant)
            try:
                body = str(await page.locator("body").inner_text(timeout=8_000) or "")
            except Exception:
                body = ""
            matched = [token for token in _research_tokens(variant) if token in body.casefold()]
            relevance = (result_score or 0) + len(matched)
            attempt = {
                "query": variant,
                "result_title": result_title,
                "result_score": result_score,
                "matched_terms": matched[:12],
                "relevance_score": relevance,
                "url": str(page.url)[: ro.MAX_URL_CHARS],
                "title": (await page.title())[:300],
                "page_text": body[:20_000],
            }
            attempts.append(attempt)
            if best is None or relevance > int(best.get("relevance_score") or 0):
                best = attempt
            if result_title and relevance >= 8:
                break
            search_box = await _information_box(page) or search_box
        except Exception as exc:
            attempts.append({"query": variant, "error": f"{type(exc).__name__}: {exc}"})
            try:
                search_box = await _information_box(page) or search_box
            except Exception:
                pass

    if best is None:
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "vehicle": vehicle,
            "topic": topic,
            "vehicle_selection": vehicle_state,
            "attempts": attempts,
            "reason": "ALLDATA vehicle research did not produce a readable search result.",
            "url": str(page.url)[: ro.MAX_URL_CHARS],
        }

    return {
        "attempted": True,
        "searched": True,
        "verified": bool(ro._is_alldata_url(page.url)),
        "query_submitted": True,
        "query": best.get("query"),
        "vehicle": vehicle,
        "topic": topic,
        "vehicle_selection": vehicle_state,
        "attempts": attempts,
        "url": best.get("url"),
        "title": best.get("title"),
        "result_title": best.get("result_title"),
        "matched_terms": best.get("matched_terms") or [],
        "relevance_score": int(best.get("relevance_score") or 0),
        "page_text": best.get("page_text") or "",
        "provenance": {
            "provider": ro.PROVIDER_LABEL,
            "licensed_session": True,
            "vehicle_selected": True,
            "query_submitted": True,
            "workflow": "vehicle_first_information_search",
        },
    }


def install() -> None:
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = research_workflow.search_alldata
        if not getattr(previous, "_xomni_alldata_vehicle_first", False):
            search_alldata_vehicle_first._xomni_alldata_vehicle_first = True  # type: ignore[attr-defined]
            research_workflow.search_alldata = search_alldata_vehicle_first
        _INSTALLED = True
