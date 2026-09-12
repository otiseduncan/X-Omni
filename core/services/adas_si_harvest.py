"""Background ADAS service-information harvest from ALLDATA.

Otis's need: every ADAS calibration procedure for a vehicle, filed in ADAS SI
as a PDF with provenance, ready to attach to an RO when it comes up. This runs
that acquisition outside any chat turn, the same way ``adas_map_sweep`` does,
so it survives a disconnect and never fills the model's context.

The route, established live on 2026-09-12 across six vehicles and five makes:

1. **Recent Vehicles** on ALLDATA's picker. One click for a vehicle already
   worked on, which also avoids the make taxonomy entirely -- ALLDATA files
   SUVs, trucks and vans under "<Make> Truck", so a Palisade is never under
   plain "Hyundai".
2. **ADAS Quick Reference**, in the ALLDATA Reference panel of every vehicle
   page. It is ALLDATA's own normalised ADAS table -- the same columns for
   every manufacturer -- listing each component, the systems it serves,
   whether post-repair calibration is required, static or dynamic, and the
   tools and targets needed. Each component name links into its procedures.
3. **Descend by judgement, not by a fixed path.** Depth differs per make and
   per component: Honda's radar aiming sits three levels under its component
   (component -> Programming and Relearning -> Millimeter Wave Radar Aiming),
   while a rear camera procedure stops one level up. A page with procedure
   links below it is an index to descend; a page with none is the document.
4. **Capture each leaf under its own task.** ScrapeX verifies a capture
   against its task's topic, so one task per document with the topic set to
   that document's title. Reusing a single task with a generic topic fails
   every gate -- "Millimeter Wave Radar Aiming" scores 1 against "adas
   calibration" and is refused.

ScrapeX owns the browser, the verification and the filing; this module owns
the route and the bookkeeping. Nothing here interprets language.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Optional

log = logging.getLogger("xomni.adas_si_harvest")

NAMESPACE = "adas_si_harvest"

PICKER_URL = "https://my.alldata.com/repair/#/select-vehicle"
QUICK_REFERENCE = "ADAS Quick Reference"

MAX_VEHICLES = 25
MAX_DEPTH = 3
# A page with nothing below it is a document however short: Honda's rear
# camera procedure is 742 characters, its LaneWatch programming 452. Length
# only separates a real page from an empty shell; having children is what
# separates an index from a document.
DOCUMENT_CHARS = 400
SETTLE_TRIES = 14

# Page furniture, and the help links inside the Quick Reference table.
CHROME = frozenset({
    "collision", "change", "library", "convert", "reference - collision",
    "community", "quote", "estimate", "vehicle", "alldata reference",
    "oem faq", "using this guide", "adas quick reference", "planner",
    "collision reference", "specifications quick reference", "search",
    "help & feedback", "bookmarks", "phone", "tech-assist",
})
# The RELATED INFORMATION block closing the Quick Reference: every ADAS row
# sits above it. On a component hub these same words are section headings the
# procedures live under, so this list applies to the Quick Reference only.
FOOTER_START = frozenset({
    "components", "fuse and fusible links", "grounds", "harness", "procedures",
    "service precautions", "technician safety information", "application and id",
    "mechanical (including torque)", "all new technical service bulletins",
    "all technical service bulletins", "by symptom", "collision repair bulletins",
    "general information bulletins", "recalls and campaigns", "locations",
    "superseded bulletins", "initial inspection and diagnostic overview",
    "specifications", "testing and inspection", "connector views",
    "diagrams", "parts and labor", "maintenance", "service and repair",
})
# Sections that hold calibration work, in every make's own wording.
PROCEDURE_WORDS = (
    "programming and relearning", "removal and replacement", "adjustment",
    "calibration", "aiming", "initialization", "alignment", "component tests",
    "removal and installation",
)


# Engine and body codes as ALLDATA prints them: L4-2.0L, V6-3.8L, (K20C2),
# (LX2), 1.6L.
_ENGINE_OR_CODE = re.compile(r"^\(|^[A-Za-z]\d+-\d|^\d+\.\d+L$")
# ADAS SI files by year/make/model with the plain make and the short model --
# 2020/Honda/Civic, 2026/Honda/CR-V, 2025/Kia/Carnival, 2016/Nissan/Rogue --
# while ALLDATA prints its own taxonomy and trim: "2025 Honda Truck CR-V 2WD
# L4-1.5L Turbo". Filing that verbatim scatters one vehicle across
# "Honda Truck/CR-V 2WD" and "Honda/CR-V", which is how a document stops being
# findable. The library's convention wins.
_BODY_OR_DRIVE = frozenset({
    "sedan", "coupe", "hatchback", "wagon", "convertible", "cabriolet",
    "fwd", "rwd", "awd", "4wd", "2wd", "4matic", "quattro", "hybrid",
})
_MAKE_ALIASES = {"nissan-datsun": "Nissan"}
# Makes ALLDATA prints as two words. Without these "2020 Mercedes Benz E 350"
# reads as a Mercedes named "Benz E".
_TWO_WORD_MAKES = {
    "mercedes benz": "Mercedes-Benz",
    "land rover": "Land Rover",
    "alfa romeo": "Alfa Romeo",
    "aston martin": "Aston Martin",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def page_links(observation: dict[str, Any], *, stop_at_footer: bool = False) -> list[tuple[str, str]]:
    """Link refs and names on one observed page, minus the page furniture."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for element in observation.get("elements") or []:
        if element.get("role") != "link":
            continue
        name = _clean(element.get("name"))
        folded = name.casefold()
        if stop_at_footer and folded in FOOTER_START:
            break
        if folded in CHROME or not (3 < len(name) < 80) or folded in seen:
            continue
        seen.add(folded)
        out.append((str(element.get("ref") or ""), name))
    return out


def find_link(observation: dict[str, Any], name: str) -> Optional[str]:
    wanted = _clean(name).casefold()
    for element in observation.get("elements") or []:
        if element.get("role") == "link" and _clean(element.get("name")).casefold() == wanted:
            return str(element.get("ref") or "")
    return None


def procedure_children(observation: dict[str, Any], parent: str) -> list[str]:
    """Names below this page that lead to calibration work."""
    parent_folded = _clean(parent).casefold()
    out: list[str] = []
    for _ref, name in page_links(observation):
        folded = name.casefold()
        if folded == parent_folded:
            continue
        if any(word in folded for word in PROCEDURE_WORDS) or parent_folded in folded:
            out.append(name)
    return out


def vehicle_target(label: str) -> dict[str, Any]:
    """The year/make/model ScrapeX verifies against, from ALLDATA's own label.

    ALLDATA files SUVs, trucks and vans under "<Make> Truck" and prints that
    in the label it shows -- "2021 Hyundai Truck Palisade AWD (LX2) V6-3.8L".
    Splitting on whitespace alone makes that a Hyundai named "Truck Palisade",
    which is not what the page says it is.
    """
    words = _clean(label).split()
    if not words:
        return {"year": _now().year, "make": "", "model": ""}
    year = int(words[0]) if words[0].isdigit() else _now().year
    rest = words[1:] if words[0].isdigit() else words
    if not rest:
        return {"year": year, "make": "", "model": ""}
    # "<Make> Truck" is ALLDATA's shelf, not the make: a Palisade is a
    # Hyundai. Drop the suffix for filing, and keep the library's spelling of
    # the make itself.
    pair = " ".join(rest[:2]).casefold() if len(rest) > 1 else ""
    if pair in _TWO_WORD_MAKES:
        make = _TWO_WORD_MAKES[pair]
        tail = rest[2:]
    elif len(rest) > 1 and rest[1].casefold() == "truck":
        make = rest[0]
        tail = rest[2:]
    else:
        make = rest[0]
        tail = rest[1:]
    make = _MAKE_ALIASES.get(make.casefold(), make)
    # "<Make> Truck" can also follow a two-word make.
    if tail and tail[0].casefold() == "truck":
        tail = tail[1:]
    # The label carries the engine and body code after the model, and taking a
    # fixed two words swallows them whenever the model is a single word:
    # "2022 Kia Niro L4-1.6L Hybrid" became the model "Niro L4-1.6L" and
    # ALLDATA would not confirm the vehicle, so nothing filed for it. Stop at
    # the first engine or code token instead.
    model: list[str] = []
    for token in tail[:3]:
        if _ENGINE_OR_CODE.match(token) or token.casefold() in _BODY_OR_DRIVE:
            break
        model.append(token)
        if len(model) == 2:
            break
    return {"year": year, "make": make, "model": " ".join(model)}


class AdasSiHarvestService:
    def __init__(
        self,
        settings: Any,
        store: Any = None,
        *,
        navigator: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
        capture: Optional[Callable[[str], Awaitable[dict[str, Any]]]] = None,
        notify: Optional[Callable[[str, str, str], Awaitable[Any]]] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.settings = settings
        self.store = store
        self.notify = notify
        self.sleep = sleep
        self.clock = clock
        self._runs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._start_lock = asyncio.Lock()
        if navigator is None or capture is None:
            from . import scrapex as scrapex_svc

            async def _default_navigator(body: dict[str, Any]) -> dict[str, Any]:
                return await scrapex_svc.navigator(self.settings, body)

            async def _default_capture(task_id: str) -> dict[str, Any]:
                return await scrapex_svc.navigator_capture(self.settings, task_id)

            self._navigator = navigator or _default_navigator
            self._capture = capture or _default_capture
        else:
            self._navigator = navigator
            self._capture = capture

    # ----------------------------------------------------------- browser
    async def _observe(self, task_id: str) -> dict[str, Any]:
        result = await self._navigator({"action": "observe", "task_id": task_id})
        return (result or {}).get("data") or {}

    async def _settle(self, task_id: str, tries: int = SETTLE_TRIES) -> dict[str, Any]:
        """Wait for a page to say something. ALLDATA answers before it lands."""
        observation: dict[str, Any] = {}
        for _ in range(max(1, tries)):
            observation = await self._observe(task_id)
            if _clean(observation.get("page_text")):
                return observation
            await self.sleep(1.0)
        return observation

    async def _open(self, task_id: str, url: str, settle: float = 2.5) -> dict[str, Any]:
        await self._navigator({"action": "open", "task_id": task_id, "url": url})
        await self.sleep(settle)
        return await self._settle(task_id)

    async def _click(self, task_id: str, ref: str, settle: float = 2.8) -> dict[str, Any]:
        acted = await self._navigator({"action": "click", "task_id": task_id, "ref": ref})
        if not (acted or {}).get("success"):
            return {}
        await self.sleep(settle)
        return await self._settle(task_id)

    async def _new_task(self, target: dict[str, Any], topic: str) -> Optional[str]:
        created = await self._navigator({
            "action": "create_task", "provider": "alldata",
            "target": target, "topic": topic[:180] or "adas calibration",
        })
        if not (created or {}).get("success"):
            return None
        return str(((created.get("data") or {}).get("id")) or "") or None

    # ----------------------------------------------------------- discovery
    async def discover(
        self,
        task_id: str,
        back_url: str,
        name: str,
        depth: int,
        found: list[dict[str, Any]],
        seen: set[str],
    ) -> None:
        """Open one link, then descend into it or record it as a document."""
        page = await self._open(task_id, back_url, settle=2.0)
        ref = find_link(page, name)
        if not ref:
            return
        here = await self._click(task_id, ref, settle=2.6)
        url = _clean(here.get("url"))
        if not url or url in seen:
            return
        seen.add(url)
        title = _clean(here.get("title")).replace(" - ALLDATA Collision", "").strip()
        text_length = len(str(here.get("page_text") or ""))

        children = procedure_children(here, name) if depth < MAX_DEPTH else []
        if children:
            for child in children:
                await self.discover(task_id, url, child, depth + 1, found, seen)
            return
        if text_length >= DOCUMENT_CHARS:
            found.append({"title": title or name, "url": url, "chars": text_length})

    async def documents_for(self, task_id: str, label: str) -> tuple[str, list[dict[str, Any]]]:
        """Every ADAS procedure document reachable for one Recent Vehicle."""
        page = await self._open(task_id, PICKER_URL, settle=3.0)
        tokens = [token for token in _clean(label).casefold().split() if token]
        ref = ""
        for element in page.get("elements") or []:
            name = _clean(element.get("name"))
            if len(name) > 8 and all(token in name.casefold() for token in tokens):
                ref = str(element.get("ref") or "")
                break
        if not ref:
            return "", []
        page = await self._click(task_id, ref, settle=3.0)
        vehicle = (
            _clean(page.get("title"))
            .replace("Vehicle Information - ", "")
            .replace(" - ALLDATA Collision", "")
            .strip()
        )
        quick_ref = find_link(page, QUICK_REFERENCE)
        if not quick_ref:
            return vehicle, []
        table = await self._click(task_id, quick_ref, settle=3.0)
        table_url = _clean(table.get("url"))
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ref, component in page_links(table, stop_at_footer=True):
            await self.discover(task_id, table_url, component, 1, found, seen)
        return vehicle, found

    # ----------------------------------------------------------- capture
    async def capture_document(
        self, target: dict[str, Any], document: dict[str, Any]
    ) -> dict[str, Any]:
        """One task per document, topic set to it, so verification can pass."""
        task_id = await self._new_task(target, document.get("title") or "")
        if not task_id:
            return {"status": "create_failed", "title": document.get("title")}
        page = await self._open(task_id, str(document.get("url") or ""), settle=4.0)
        if not _clean(page.get("page_text")):
            return {"status": "page_never_loaded", "title": document.get("title")}
        await self._navigator({"action": "extract", "task_id": task_id})
        proof = await self._navigator({"action": "verify", "task_id": task_id})
        evidence = (proof or {}).get("data") or {}
        if not evidence.get("verified"):
            return {
                "status": "unverified",
                "reason": _clean(evidence.get("reason"))[:160],
                "title": document.get("title"),
            }
        result = await self._capture(task_id)
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        return {
            "status": _clean(result.get("status")) or "unknown",
            "title": document.get("title"),
            "path": payload.get("relative_path"),
            "sha256": payload.get("sha256"),
        }

    # ----------------------------------------------------------- run
    async def _run(self, run_id: str, labels: list[str]) -> None:
        record = self._runs[run_id]
        try:
            for label in labels:
                task_id = await self._new_task(vehicle_target(label), "adas quick reference")
                if not task_id:
                    record["vehicles"].append(
                        {"requested": label, "vehicle": label, "found": 0, "filed": 0,
                         "documents": [], "error": "could not start a Navigator task"}
                    )
                    continue
                vehicle, documents = await self.documents_for(task_id, label)
                entry: dict[str, Any] = {
                    "requested": label,
                    "vehicle": vehicle or label,
                    "found": len(documents),
                    "filed": 0,
                    "documents": [],
                }
                record["vehicles"].append(entry)
                if not vehicle:
                    entry["error"] = "not in ALLDATA Recent Vehicles"
                    continue
                target = vehicle_target(vehicle)
                for document in documents:
                    outcome = await self.capture_document(target, document)
                    entry["documents"].append(outcome)
                    if outcome.get("status") in {"captured", "already_present"}:
                        entry["filed"] += 1
                        record["filed"] += 1
                record["completed"] += 1
            record["state"] = "finished"
        except asyncio.CancelledError:
            record["state"] = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            record["state"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
            log.exception("ADAS SI harvest failed")
        finally:
            record["finished_at"] = self.clock().isoformat()
            self._tasks.pop(run_id, None)
            if self.notify and record.get("state") == "finished":
                try:
                    await self.notify(
                        "ADAS SI harvest finished",
                        f"{record['filed']} document(s) filed for "
                        f"{record['completed']} vehicle(s).",
                        NAMESPACE,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("harvest notification failed", exc_info=True)

    async def start(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Begin a background harvest. Started is not finished."""
        args = dict(args or {})
        raw = args.get("vehicles")
        labels = [_clean(item) for item in raw if _clean(item)] if isinstance(raw, list) else []
        if not labels:
            return {
                "executed": False,
                "success": False,
                "reason": (
                    "vehicles is required: the ALLDATA Recent Vehicles labels to "
                    "harvest, for example '2021 Honda Civic'."
                ),
            }
        if len(labels) > MAX_VEHICLES:
            return {
                "executed": False,
                "success": False,
                "reason": f"At most {MAX_VEHICLES} vehicles per harvest; {len(labels)} were given.",
            }

        async with self._start_lock:
            if self.running():
                return {
                    "executed": False,
                    "success": False,
                    "reason": "An ADAS SI harvest is already running; wait for it to finish.",
                }
            run_id = uuid.uuid4().hex
            self._runs[run_id] = {
                "run_id": run_id,
                "state": "running",
                "requested": labels,
                "completed": 0,
                "filed": 0,
                "vehicles": [],
                "started_at": self.clock().isoformat(),
            }
            self._tasks[run_id] = asyncio.create_task(self._run(run_id, labels))

        return {
            "executed": True,
            "success": True,
            "run_id": run_id,
            "state": "running",
            "vehicles_requested": len(labels),
            "note": (
                "Running in the background; ask for adas_si_harvest_status. "
                "Started is not complete."
            ),
        }

    async def status(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        run_id = _clean(args.get("run_id"))
        if run_id:
            record = self._runs.get(run_id)
        else:
            record = max(
                self._runs.values(), key=lambda item: item.get("started_at") or "", default=None
            )
        if not record:
            return {"success": True, "state": "none", "reason": "No ADAS SI harvest has been run."}
        return {
            "success": True,
            "run_id": record.get("run_id"),
            "state": record.get("state"),
            "requested": len(record.get("requested") or []),
            "completed": record.get("completed"),
            "filed": record.get("filed"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "error": record.get("error"),
            "vehicles": [
                {
                    "vehicle": entry.get("vehicle"),
                    "found": entry.get("found"),
                    "filed": entry.get("filed"),
                    "error": entry.get("error"),
                }
                for entry in (record.get("vehicles") or [])
            ],
        }

    def running(self) -> bool:
        return any(not task.done() for task in self._tasks.values())

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("harvest task ended with an error during shutdown", exc_info=True)
        self._tasks.clear()
