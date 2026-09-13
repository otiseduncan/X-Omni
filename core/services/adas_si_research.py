"""Background ADAS service-information research: X navigates, Core keeps score.

Otis's need is unchanged from the scripted harvester this replaces: every ADAS
calibration procedure a repair order needs, filed in ADAS SI with provenance
and attached to the RO in Calibration IQ, acquired outside any chat turn so it
survives a disconnect and never fills the model's context.

What changed is who decides. This job knows only what a scheduler may know:

* the exact vehicle and VIN, read fresh from Calibration IQ;
* the RO and the calibration requirements Calibration IQ lists for it;
* the provider (ALLDATA through ScrapeX's Navigator) and the destination
  (ADAS SI, then the RO's research case);
* budgets, status, receipts.

It turns each requirement into one structured research objective and hands it
to the model-driven Navigator (``research_navigator_agent.run_navigator_search``).
X chooses where to go inside ALLDATA, what to click, what is a procedure, and
what else is required; an independent semantic review judges every candidate;
ScrapeX proves the mechanics, captures, and hashes. Nothing in this module
inspects a page, a title, or a link name.

Attachment goes through the existing Calibration IQ operator path
(``ensure_case_workspace`` + ``import_document`` bound to the calibration item
the objective was researched for) with a fresh authoritative reread deciding
"attached". Research state is never marked complete here; that remains a
separate, evidence-gated operation.

Jobs persist in ``state_records`` (namespace ``adas_si_research``) and resume
after a Core restart; the research receipt of every objective is kept with the
job so a failure can be diagnosed without reading server logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Optional
from urllib.parse import quote

log = logging.getLogger("xomni.adas_si_research")

NAMESPACE = "adas_si_research"
INVOCATION_KEY = "__xomni_invocation"
MAX_TARGETS = 25
MAX_OBJECTIVES = 60
MAX_PHASES = 12
BOARD_ROW_LIMIT = 100
MAX_RUN_SECONDS = 6 * 3600.0
MODEL_WAIT_SECONDS = 600.0
MODEL_POLL_SECONDS = 20.0
OBJECTIVE_TURNS = 40
ACTIVE_STATES = frozenset({"running"})
CONTEXT_MAX_CHARS = 900
CONTEXT_RECENT_HOURS = 12
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_VIN_IN_TEXT = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
# Identity normalization only: the exact make Calibration IQ stores, spelled
# the way ScrapeX's vehicle-identity check and the library spell it.
_MAKE_SPELLINGS = {
    "vw": "Volkswagen",
    "benz": "Mercedes-Benz",
    "mercedes benz": "Mercedes-Benz",
    "mercedes": "Mercedes-Benz",
    "chevy": "Chevrolet",
}
# Requirement dispositions Calibration IQ treats as work to do.
_ACTIVE_DETERMINATIONS = frozenset({"required", "likely_required", "needs_research"})

OUTCOME_ORDER = (
    "attached",
    "captured_not_attached",
    "found_not_captured",
    "incomplete",
    "not_found",
    "uncertain",
    "blocked",
    "model_unavailable",
    "not_run",
)
OUTCOME_LABELS = {
    "attached": "Procedure filed and attached in Calibration IQ",
    "captured_not_attached": "Procedure filed in ADAS SI; Calibration IQ attachment not confirmed",
    "found_not_captured": "Procedure accepted but not captured",
    "incomplete": "Procedure found; a required document is still missing",
    "not_found": "No procedure accepted",
    "uncertain": "Reviewer could not confirm a procedure",
    "blocked": "Blocked (sign-in or browser busy)",
    "model_unavailable": "The X model was not available",
    "not_run": "Not run",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _clean(value: Any, limit: int = 160) -> str:
    return " ".join(str(value or "").split())[:limit]


def _dig(item: Any, *paths: str) -> Any:
    for path in paths:
        current = item
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
                break
        if current not in (None, "", [], {}):
            return current
    return None


def _phases(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    phases: list[str] = []
    for item in items:
        text = _clean(item, 4)
        if not text.isdigit():
            raise ValueError("phases must be phase numbers, for example ['1', '2']")
        if text not in phases:
            phases.append(str(int(text)))
    return sorted(phases, key=int)[:MAX_PHASES]


def normalize_make(value: Any) -> str:
    text = _clean(value, 60)
    return _MAKE_SPELLINGS.get(text.casefold(), text)


def normalize_model(value: Any, trim: Any = None) -> str:
    """Preserve the complete model identity Calibration IQ supplied.

    A multiword model is not a model plus trim: ``Santa Fe``, ``Grand
    Cherokee``, and ``Range Rover`` must remain intact. When CIQ supplies a
    separate trim field and also appends that exact trim to the model string,
    remove only that explicit suffix. ScrapeX receives the trim separately.
    """
    model = _clean(value, 120)
    trim_text = _clean(trim, 80)
    if not model or not trim_text:
        return model
    suffix = f" {trim_text}"
    if model.casefold().endswith(suffix.casefold()):
        return model[: -len(suffix)].strip()
    return model


def vin_from_read(read: dict[str, Any]) -> str:
    raw = read.get("raw") if isinstance(read.get("raw"), dict) else {}
    candidate = _dig(raw, "vin", "vehicle.vin", "repair_order.vin", "repair_order.vehicle.vin")
    text = _clean(candidate, 32).upper()
    if _VIN_RE.match(text):
        return text
    import json as _json

    match = _VIN_IN_TEXT.search(_json.dumps(raw, default=str))
    return match.group(0) if match else ""


def target_from_read(read: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The exact identity one Calibration IQ read carries, and nothing more."""
    if not isinstance(read, dict) or read.get("status") != "verified":
        return None
    raw = read.get("raw") if isinstance(read.get("raw"), dict) else {}
    repair_order = read.get("repair_order") if isinstance(read.get("repair_order"), dict) else {}
    ro_number = _clean(
        _dig(repair_order, "RO", "ro_number") or _dig(raw, "ro_number", "repair_order.ro_number", "number"), 40
    )
    ro_id = _clean(_dig(repair_order, "id") or _dig(raw, "id", "repair_order.id"), 80)
    vehicle_raw = _dig(raw, "vehicle", "repair_order.vehicle") or {}
    if not isinstance(vehicle_raw, dict):
        vehicle_raw = {}
    year = vehicle_raw.get("year") or _dig(raw, "year", "repair_order.year")
    try:
        year_value = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year_value = None
    make = normalize_make(vehicle_raw.get("make") or _dig(raw, "make", "repair_order.make"))
    trim = _clean(vehicle_raw.get("trim") or _dig(raw, "trim", "repair_order.trim"), 80)
    model = normalize_model(
        vehicle_raw.get("model") or _dig(raw, "model", "repair_order.model"),
        trim,
    )
    calibrations: list[dict[str, Any]] = []
    for item in _dig(raw, "calibrations", "calibration_items", "repair_order.calibrations") or []:
        if not isinstance(item, dict):
            continue
        determination = _clean(item.get("determination") or item.get("status"), 40).casefold()
        if determination and determination not in _ACTIVE_DETERMINATIONS:
            continue
        title = _clean(item.get("title") or item.get("name") or item.get("calibration_type"), 160)
        if not title:
            continue
        calibrations.append(
            {
                "id": _clean(item.get("id"), 80) or None,
                "title": title,
                "determination": determination or None,
                "version": item.get("version"),
            }
        )
    if not ro_number or not ro_id or not year_value or not make or not model:
        return None
    vehicle = {"year": year_value, "make": make, "model": model}
    if trim:
        vehicle["trim"] = trim
    return {
        "ro_number": ro_number,
        "repair_order_id": ro_id,
        "vehicle": vehicle,
        "vehicle_label": " ".join(str(part) for part in (year_value, make, model, trim) if part),
        "vin": vin_from_read(read),
        "phase": _dig(repair_order, "Phase") or _dig(raw, "phase", "workflow.phase"),
        "shop": _clean(_dig(repair_order, "Shop") or _dig(raw, "shop.name", "shop"), 60) or None,
        "calibrations": calibrations,
    }


def objectives_for(target: dict[str, Any], *, systems: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """One research objective per calibration requirement (or per named system).

    The objective text is the requirement as Calibration IQ names it, plus
    the research goal. It is the *question*; the model decides how ALLDATA
    answers it. When the caller names systems explicitly, those are the
    objectives instead.
    """
    objectives: list[dict[str, Any]] = []
    if systems:
        sources = [{"id": None, "title": _clean(system, 160)} for system in systems if _clean(system, 160)]
    else:
        sources = list(target.get("calibrations") or [])
    for calibration in sources:
        title = calibration.get("title")
        if not title:
            continue
        objective_id = hashlib.sha1(
            f"{target['ro_number']}|{calibration.get('id') or ''}|{title}".encode("utf-8")
        ).hexdigest()[:12]
        objectives.append(
            {
                "objective_id": objective_id,
                "ro_number": target["ro_number"],
                "repair_order_id": target["repair_order_id"],
                "calibration_id": calibration.get("id"),
                "calibration_title": title,
                "system": title,
                "topic": f"{title} calibration / aiming / initialization procedure",
                "vehicle": dict(target["vehicle"]),
                "vehicle_label": target.get("vehicle_label"),
                "vin": target.get("vin") or "",
                "status": "pending",
            }
        )
    return objectives


def classify_objective(objective: dict[str, Any]) -> str:
    """Structural outcome of one objective; Calibration IQ decides attachment."""
    result = objective.get("result") or {}
    if objective.get("status") in {"pending", "in_progress"}:
        return "not_run"
    if objective.get("status") == "model_unavailable":
        return "model_unavailable"
    if result.get("status") in {"authentication_required", "navigator_busy"} or result.get("requires_human"):
        return "blocked"
    if not result.get("verified"):
        return "uncertain" if result.get("status") == "uncertain" else "not_found"
    attachments = objective.get("attachments") or []
    if attachments and all(item.get("attached") for item in attachments):
        return "attached" if result.get("complete") else "incomplete"
    if result.get("captured"):
        # Filed in ADAS SI, but Calibration IQ does not show it on the RO --
        # whether the attachment failed, was refused, or was never attempted.
        return "captured_not_attached"
    return "found_not_captured"


def summary_sentence(record: dict[str, Any]) -> str:
    objectives = record.get("objectives") or []
    counts: dict[str, int] = {}
    for objective in objectives:
        outcome = objective.get("outcome") or classify_objective(objective)
        counts[outcome] = counts.get(outcome, 0) + 1
    targets = record.get("targets") or []
    parts = [
        f"Service-information research for {record.get('scope_label') or 'the requested work'} finished: "
        f"{counts.get('attached', 0)} of {len(objectives)} procedure(s) filed and attached across "
        f"{len(targets)} repair order(s)."
    ]
    rest = [
        f"{count} {OUTCOME_LABELS.get(outcome, outcome).casefold()}"
        for outcome, count in counts.items()
        if outcome != "attached"
    ]
    if rest:
        parts.append("Also: " + "; ".join(rest) + ".")
    return " ".join(parts)


def _clock_label(value: Any) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "earlier"
    return parsed.astimezone().strftime("%I:%M %p").lstrip("0")


def context_line(record: dict[str, Any], *, now: Optional[datetime] = None, for_model: bool = True) -> Optional[str]:
    """One sentence of background-work truth for X's turn context."""
    state = str(record.get("state") or "")
    scope = record.get("scope_label") or "the requested work"
    objectives = record.get("objectives") or []
    if state in ACTIVE_STATES:
        finished = sum(1 for item in objectives if item.get("status") not in {"pending", "in_progress"})
        attached = sum(1 for item in objectives if item.get("outcome") == "attached")
        line = (
            f"Service-information research for {scope} is still running in the background "
            f"(started {_clock_label(record.get('started_at'))}): {finished} of {len(objectives)} "
            f"procedure objectives finished, {attached} attached so far. No final result exists yet; "
            "results post to the chat when it finishes."
        )
        if for_model:
            line += (
                " For its latest progress read query_ciq kind=adas_si_research; never report an "
                "outcome it has not returned."
            )
        return line[:CONTEXT_MAX_CHARS]
    if state == "completed":
        finished_at = _parse_iso(record.get("finished_at"))
        reference = now or _now()
        if finished_at is None or (reference - finished_at).total_seconds() > CONTEXT_RECENT_HOURS * 3600:
            return None
        return (f"Finished at {_clock_label(record.get('finished_at'))}. " + summary_sentence(record))[:CONTEXT_MAX_CHARS]
    return None


def latest_context_line(
    store: Any,
    user_id: Optional[str],
    *,
    for_model: bool = True,
    conversation_id: Optional[int] = None,
    include_completed: bool = True,
) -> Optional[str]:
    try:
        rows = store.list_records(NAMESPACE, user_id=user_id, limit=50 if conversation_id is not None else 1)
    except Exception:  # noqa: BLE001 - lightweight test stores have no records
        return None
    record = None
    for row in rows or []:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        if conversation_id is not None:
            try:
                if int(payload.get("conversation_id")) != int(conversation_id):
                    continue
            except (TypeError, ValueError):
                continue
        if not include_completed and str(payload.get("state") or "") == "completed":
            continue
        record = payload
        break
    if record is None:
        return None
    try:
        return context_line(record, for_model=for_model)
    except Exception:  # noqa: BLE001
        log.warning("could not render research context", exc_info=True)
        return None


# ---------------------------------------------------------------- defaults


def default_ro_reader(settings: Any) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def read(args: dict[str, Any]) -> dict[str, Any]:
        from . import calibration_iq

        return await calibration_iq.get_repair_order(settings, args)

    return read


def default_board_reader(settings: Any) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def read(args: dict[str, Any]) -> dict[str, Any]:
        from . import calibration_iq

        return await calibration_iq.read_repair_orders(settings, args)

    return read


def default_attach(settings: Any, adas: Any) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Attach one captured document to its RO through the operator path.

    ``ensure_case_workspace`` then ``import_document`` bound to the exact
    calibration item the objective was researched for, under the job's own
    invocation identity (exact-once, receipted), followed by an authoritative
    reread that alone decides "attached". Research state is left alone.
    """

    async def attach(
        objective: dict[str, Any], document: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        from . import calibration_iq

        artifact = document.get("artifact") or {}
        relative = str(artifact.get("relative_path") or "").strip().replace("\\", "/")
        if not relative:
            return {"attached": False, "status": "no_artifact"}
        try:
            source_path = adas.resolve_relative(relative)
        except Exception as exc:  # noqa: BLE001
            return {"attached": False, "status": "artifact_unresolvable", "error": f"{type(exc).__name__}: {exc}"[:200]}
        source_uri = f"adas-si:///{quote(relative)}"
        ro_id = str(objective.get("repair_order_id") or "")
        before = await calibration_iq.get_repair_order(settings, {"repair_order_id": ro_id})
        if before.get("status") != "verified" or not isinstance(before.get("raw"), dict):
            return {"attached": False, "status": "ro_unreadable", "message": before.get("message")}
        existing = calibration_iq._existing_research_documents(before["raw"])  # noqa: SLF001
        already = next(
            (item for item in existing if str(item.get("source_uri") or "").strip().casefold() == source_uri.casefold()),
            None,
        )
        if already is not None:
            return {
                "attached": True,
                "status": "already_attached",
                "document_id": already.get("id") or already.get("document_id"),
                "source_uri": source_uri,
            }
        review = document.get("review") if isinstance(document.get("review"), dict) else {}
        status = (
            "validated"
            if review.get("decision") == "ACCEPT" and float(review.get("confidence") or 0) >= 0.8
            else "candidate"
        )
        arguments: dict[str, Any] = {
            "source_path": str(source_path),
            "document_type": "oem_procedure",
            "semantic_type": "OEM_PROCEDURE",
            "evidence_role": "PROCEDURE",
            "title": _clean(document.get("title") or artifact.get("title") or objective.get("calibration_title"), 255),
            "source_uri": source_uri,
            "source_name": relative.rsplit("/", 1)[-1][:255],
            "page_references": [],
            "citation": _clean(f"ALLDATA, {document.get('url') or ''}", 500),
            "notes": _clean(
                "Filed by X's service-information research. Reviewer: "
                f"{review.get('classification') or ''} / {review.get('decision') or ''} "
                f"({review.get('confidence')}). {review.get('evidence_summary') or ''}",
                1500,
            ),
            "status": status,
        }
        if objective.get("calibration_id"):
            arguments["calibration_item_ids"] = [str(objective["calibration_id"])]
        actions = [
            {"operation": "ensure_case_workspace", "repair_order_id": ro_id, "arguments": {}},
            {"operation": "import_document", "repair_order_id": ro_id, "arguments": arguments},
        ]
        digest = str(artifact.get("sha256") or "")[:12]
        invocation = dict(context)
        invocation["tool_call_id"] = f"{context.get('tool_call_id') or 'adas_si_research'}:{objective.get('objective_id')}:{digest}"
        result = await calibration_iq.operator_execute(
            settings, adas, {"actions": actions, INVOCATION_KEY: invocation}
        )
        after = await calibration_iq.get_repair_order(settings, {"repair_order_id": ro_id})
        attached_document = None
        if after.get("status") == "verified" and isinstance(after.get("raw"), dict):
            for item in calibration_iq._existing_research_documents(after["raw"]):  # noqa: SLF001
                if str(item.get("source_uri") or "").strip().casefold() == source_uri.casefold():
                    attached_document = item
                    break
        return {
            "attached": attached_document is not None,
            "status": "attached" if attached_document is not None else "not_confirmed",
            "document_id": (attached_document or {}).get("id") or (attached_document or {}).get("document_id"),
            "document_status": status,
            "source_uri": source_uri,
            "receipt_status": result.get("status") if isinstance(result, dict) else None,
            "receipt_message": _clean((result or {}).get("message") if isinstance(result, dict) else "", 300) or None,
            "calibration_item_id": objective.get("calibration_id"),
        }

    return attach


# ----------------------------------------------------------------- service


class AdasSiResearchService:
    def __init__(
        self,
        settings: Any,
        store: Any,
        *,
        client: Any = None,
        router: Any = None,
        adas: Any = None,
        navigator_search: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
        ro_reader: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
        board_reader: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
        attach: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
        notify: Optional[Callable[[str, str, str], Awaitable[Any]]] = None,
        publish: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = _now,
        objective_turns: int = OBJECTIVE_TURNS,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.router = router
        self.adas = adas
        self.navigator_search = navigator_search
        self.ro_reader = ro_reader or default_ro_reader(settings)
        self.board_reader = board_reader or default_board_reader(settings)
        self.attach = attach or (default_attach(settings, adas) if adas is not None else None)
        self.notify = notify or self._default_notify
        self.publish = publish
        self.sleep = sleep
        self.clock = clock
        self.objective_turns = objective_turns
        self._tasks: dict[str, asyncio.Task] = {}
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------ records

    def _save(self, record: dict[str, Any]) -> dict[str, Any]:
        record["updated_at"] = _iso(self.clock())
        self.store.put_record(NAMESPACE, record["job_id"], record, user_id=record["user_id"])
        return record

    def _load(self, user_id: str, job_id: str) -> Optional[dict[str, Any]]:
        return self.store.get_record(NAMESPACE, job_id, user_id=user_id)

    def _records(self, user_id: Optional[str]) -> list[dict[str, Any]]:
        try:
            rows = self.store.list_records(NAMESPACE, user_id=user_id, limit=50)
        except AttributeError:
            return []
        return [row["payload"] for row in rows if isinstance(row.get("payload"), dict)]

    def running_job(self, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        for record in self._records(user_id):
            if record.get("state") in ACTIVE_STATES:
                return record
        return None

    # ----------------------------------------------------------- handlers

    def _not_started(self, status: str, message: str, **extra: Any) -> dict[str, Any]:
        return {
            "service": "X Omni",
            "action": "adas_si_research",
            "status": status,
            "success": False,
            "executed": False,
            "verified": False,
            "work_complete": False,
            "message": message,
            **extra,
        }

    async def _resolve_targets(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], str]:
        """Exact targets from Calibration IQ for the requested scope."""
        problems: list[str] = []
        targets: list[dict[str, Any]] = []
        identifiers: list[str] = []
        for key in ("repair_order_id", "ro_number"):
            value = _clean(payload.get(key), 40)
            if value and value not in identifiers:
                identifiers.append(value)
        for value in payload.get("ro_numbers") or []:
            text = _clean(value, 40)
            if text and text not in identifiers:
                identifiers.append(text)
        phases = _phases(payload.get("phases"))
        shop = _clean(payload.get("shop"), 60)
        if identifiers:
            label = ", ".join(f"RO {item}" for item in identifiers[:5])
        elif phases:
            label = ("phase " + phases[0] if len(phases) == 1 else "phases " + ", ".join(phases)) + (f" in {shop}" if shop else "")
        else:
            label = "the active board" + (f" in {shop}" if shop else "")
        if not identifiers:
            filters: dict[str, Any] = {"limit": BOARD_ROW_LIMIT}
            if shop:
                filters["shop"] = shop
            phase_list = phases or [None]
            for phase in phase_list:
                query = dict(filters)
                if phase is not None:
                    query["phase"] = phase
                board = await self.board_reader(query)
                if not isinstance(board, dict) or board.get("status") != "verified":
                    problems.append(f"Calibration IQ board read failed for {label}: {(board or {}).get('message') if isinstance(board, dict) else 'no result'}")
                    continue
                for row in board.get("rows") or []:
                    identifier = _clean(row.get("id") or row.get("RO"), 80)
                    if identifier and identifier not in identifiers:
                        identifiers.append(identifier)
        for identifier in identifiers[:MAX_TARGETS]:
            read_args: dict[str, Any] = {"repair_order_id": identifier}
            if shop and len(identifier) == 5:
                read_args["shop"] = shop
            try:
                read = await self.ro_reader(read_args)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"RO {identifier}: read failed ({type(exc).__name__})")
                continue
            target = target_from_read(read)
            if target is None:
                problems.append(f"RO {identifier}: {(read or {}).get('message') if isinstance(read, dict) else 'not readable'}")
                continue
            if any(item["ro_number"] == target["ro_number"] for item in targets):
                continue
            targets.append(target)
        if len(identifiers) > MAX_TARGETS:
            problems.append(f"{len(identifiers)} repair orders matched; the first {MAX_TARGETS} were taken.")
        return targets, problems, label

    async def start(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handler for the concrete ``adas_si_research`` tool (via stage_action)."""
        payload = dict(args or {})
        context = payload.pop(INVOCATION_KEY, None)
        if not isinstance(context, dict) or not context.get("conversation_id"):
            return self._not_started("context_missing", "The research must be started from a conversation; nothing was started.")
        try:
            _phases(payload.get("phases"))
        except ValueError as exc:
            return self._not_started("invalid_scope", str(exc))
        systems = [
            _clean(item, 160) for item in (payload.get("systems") or []) if _clean(item, 160)
        ][:12] if isinstance(payload.get("systems"), list) else []
        user_id = str(context.get("user_id") or "local-dev")
        if self.client is None and self.navigator_search is None:
            return self._not_started("model_unavailable", "No model client is wired for background research; nothing was started.")

        async with self._start_lock:
            running = self.running_job(user_id)
            if running is not None:
                self._ensure_driver(running)
                view = self.public_view(running)
                view.update({
                    "status": "already_running",
                    "executed": False,
                    "message": (
                        f"Service-information research for {running.get('scope_label')} is already "
                        "running; nothing new was started. Its results post to its conversation when it finishes."
                    ),
                })
                return view
            targets, problems, label = await self._resolve_targets(payload)
            if not targets:
                return self._not_started(
                    "no_targets",
                    "No readable repair order with an exact vehicle was found for "
                    f"{label}; nothing was started." + (" " + "; ".join(problems[:4]) if problems else ""),
                    problems=problems,
                )
            objectives: list[dict[str, Any]] = []
            for target in targets:
                objectives.extend(objectives_for(target, systems=systems or None))
            objectives = objectives[:MAX_OBJECTIVES]
            if not objectives:
                return self._not_started(
                    "no_requirements",
                    f"Calibration IQ lists no active calibration requirement for {label}; "
                    "without a requirement there is no procedure to research. Attach the ADAS "
                    "Map first, or name the systems to research.",
                    targets=[{"ro_number": item["ro_number"], "vehicle": item["vehicle_label"]} for item in targets],
                )
            job_id = uuid.uuid4().hex[:16]
            record = {
                "job_id": job_id,
                "user_id": user_id,
                "conversation_id": int(context["conversation_id"]),
                "message_id": context.get("message_id"),
                "tool_call_id": context.get("tool_call_id"),
                "role": context.get("role"),
                "state": "running",
                "scope_label": label,
                "systems": systems,
                "targets": [
                    {key: value for key, value in target.items() if key != "calibrations"}
                    | {"calibration_count": len(target.get("calibrations") or [])}
                    for target in targets
                ],
                "objectives": objectives,
                "problems": problems,
                "started_at": _iso(self.clock()),
                "finished_at": None,
                "notified": False,
            }
            self._save(record)
            self._ensure_driver(record)
        view = self.public_view(record)
        view["status"] = "running"
        view["message"] = (
            f"Started service-information research for {label}: {len(objectives)} procedure "
            f"objective(s) across {len(targets)} repair order(s). X navigates ALLDATA for each, an "
            "independent review judges every candidate, accepted procedures are filed in ADAS SI "
            "and attached to the RO. Results post to this chat when it finishes; started is not complete."
        )
        return view

    async def status(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = dict(args or {})
        context = payload.pop(INVOCATION_KEY, None)
        user_id = str((context or {}).get("user_id") or "local-dev")
        records = self._records(user_id)
        if not records:
            return {
                "service": "X Omni",
                "action": "adas_si_research_status",
                "status": "no_job",
                "success": True,
                "executed": True,
                "verified": True,
                "message": "No service-information research has been started yet.",
            }
        record = records[0]
        if record.get("state") in ACTIVE_STATES:
            self._ensure_driver(record)
        view = self.public_view(record)
        view["action"] = "adas_si_research_status"
        return view

    async def resume(self) -> int:
        resumed = 0
        for record in self._records(None):
            if record.get("state") in ACTIVE_STATES:
                self._ensure_driver(record)
                resumed += 1
            elif record.get("state") == "completed" and not record.get("notified"):
                try:
                    await self._post_result(record)
                except Exception:  # noqa: BLE001
                    log.exception("could not post a completed research job after restart")
        return resumed

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    def _ensure_driver(self, record: dict[str, Any]) -> None:
        job_id = record["job_id"]
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            return
        self._tasks[job_id] = asyncio.create_task(
            self._drive(job_id, record["user_id"]), name=f"adas-si-research-{job_id}"
        )

    # -------------------------------------------------------------- driver

    def _model_ready(self) -> bool:
        if self.navigator_search is not None and self.client is None:
            return True
        if self.router is None:
            return self.client is not None
        try:
            return bool(self.router.supports_vision())
        except Exception:  # noqa: BLE001
            return False

    async def _wait_for_model(self) -> bool:
        waited = 0.0
        while not self._model_ready():
            if waited >= MODEL_WAIT_SECONDS:
                return False
            await self.sleep(MODEL_POLL_SECONDS)
            waited += MODEL_POLL_SECONDS
        return True

    async def _research(self, objective: dict[str, Any]) -> dict[str, Any]:
        from . import research_navigator_agent as nav_agent

        search = self.navigator_search or nav_agent.run_navigator_search
        target = dict(objective["vehicle"])
        if objective.get("vin"):
            target["vin"] = objective["vin"]
        return await search(
            client=self.client,
            settings=self.settings,
            provider="alldata",
            target=target,
            topic=objective["topic"],
            max_turns=self.objective_turns,
            capture=True,
            objective={
                "objective": objective["topic"],
                "system": objective.get("system"),
                "component": objective.get("calibration_title"),
                "repair_order": objective.get("ro_number"),
                "calibration_item_id": objective.get("calibration_id"),
            },
        )

    @staticmethod
    def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
        receipt = result.get("research_receipt") if isinstance(result.get("research_receipt"), dict) else {}
        review = result.get("semantic_review") if isinstance(result.get("semantic_review"), dict) else {}
        return {
            "status": result.get("status"),
            "verified": bool(result.get("verified")),
            "complete": result.get("complete"),
            "captured": bool(result.get("captured")),
            "requires_human": bool(result.get("requires_human")),
            "reason": _clean(result.get("reason") or result.get("verification_reason"), 300) or None,
            "title": _clean(result.get("evidence_title"), 200) or None,
            "source_url": result.get("source_url"),
            "review": {key: review.get(key) for key in ("classification", "procedure_type", "decision", "confidence", "evidence_summary") if review.get(key) is not None},
            "documents": [
                {key: document.get(key) for key in ("role", "title", "url", "accepted", "classification", "decision", "captured", "artifact", "task_id")}
                for document in (result.get("documents") or []) if isinstance(document, dict)
            ][:8],
            "dependencies": [
                {key: dependency.get(key) for key in ("title", "reason", "status", "resolved_artifact")}
                for dependency in (result.get("dependencies") or []) if isinstance(dependency, dict)
            ][:8],
            "incomplete_reasons": list(result.get("incomplete_reasons") or [])[:6],
            "task_ids": list(result.get("task_ids") or []),
            "receipt": {
                "task_ids": receipt.get("task_ids"),
                "visited_urls": receipt.get("visited_urls"),
                "candidates": receipt.get("candidates"),
                "critic_decisions": receipt.get("critic_decisions"),
                "dependencies": receipt.get("dependencies"),
                "stale_action_rejections": receipt.get("stale_action_rejections"),
                "artifacts": receipt.get("artifacts"),
                "final_status": receipt.get("final_status"),
                "incomplete_reasons": receipt.get("incomplete_reasons"),
                "metrics": receipt.get("metrics"),
                "action_count": len(receipt.get("actions") or []),
                "observation_count": len(receipt.get("observation_ids") or []),
            } if receipt else None,
        }

    async def _drive(self, job_id: str, user_id: str) -> None:
        try:
            while True:
                record = self._load(user_id, job_id)
                if record is None or record.get("state") not in ACTIVE_STATES:
                    return
                started = _parse_iso(record.get("started_at")) or self.clock()
                if (self.clock() - started).total_seconds() > MAX_RUN_SECONDS:
                    record["error"] = "The research job exceeded its time limit."
                    await self._finalize(record)
                    return
                objective = next((item for item in record["objectives"] if item.get("status") == "pending"), None)
                if objective is None:
                    await self._finalize(record)
                    return
                objective["status"] = "in_progress"
                objective["started_at"] = _iso(self.clock())
                self._save(record)
                if not await self._wait_for_model():
                    objective["status"] = "model_unavailable"
                    objective["outcome"] = "model_unavailable"
                    objective["finished_at"] = _iso(self.clock())
                    self._save(record)
                    continue
                try:
                    result = await self._research(objective)
                except Exception as exc:  # noqa: BLE001 - one objective failing is a fact to record
                    log.exception("research objective %s failed", objective.get("objective_id"))
                    result = {"status": "error", "verified": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
                result = result if isinstance(result, dict) else {"status": "error", "verified": False}
                objective["result"] = self._compact_result(result)
                objective["attachments"] = []
                if result.get("verified") and objective.get("repair_order_id") and self.attach is not None:
                    context = {
                        "conversation_id": record["conversation_id"],
                        "message_id": record.get("message_id"),
                        "tool_call_id": record.get("tool_call_id"),
                        "user_id": record["user_id"],
                        "role": record.get("role") or "owner",
                    }
                    for document in result.get("documents") or []:
                        if not isinstance(document, dict) or not document.get("accepted") or not document.get("captured"):
                            continue
                        document_record = dict(document)
                        document_record["review"] = next(
                            (task_review for task_review in [result.get("semantic_review")] if document.get("role") == "primary"),
                            None,
                        ) or {}
                        try:
                            attachment = await self.attach(objective, document_record, context)
                        except Exception as exc:  # noqa: BLE001
                            log.exception("attachment failed for %s", objective.get("objective_id"))
                            attachment = {"attached": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:200]}
                        attachment = attachment if isinstance(attachment, dict) else {"attached": False}
                        attachment["title"] = document.get("title")
                        attachment["role"] = document.get("role")
                        objective["attachments"].append(attachment)
                objective["status"] = "finished"
                objective["outcome"] = classify_objective(objective)
                objective["finished_at"] = _iso(self.clock())
                self._save(record)
                # Between objectives, re-read the record: a shutdown or a
                # restart may have changed it under us.
                await self.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("research job %s failed", job_id)
            record = self._load(user_id, job_id)
            if record is not None:
                record["error"] = "The research job stopped on an internal error."
                try:
                    await self._finalize(record)
                except Exception:  # noqa: BLE001
                    log.exception("could not finalize research job %s", job_id)

    async def _finalize(self, record: dict[str, Any]) -> None:
        for objective in record.get("objectives") or []:
            if objective.get("status") in {"pending", "in_progress"}:
                objective["status"] = "not_run"
                objective["outcome"] = "not_run"
            elif not objective.get("outcome"):
                objective["outcome"] = classify_objective(objective)
        counts: dict[str, int] = {}
        for objective in record.get("objectives") or []:
            outcome = objective.get("outcome") or "not_run"
            counts[outcome] = counts.get(outcome, 0) + 1
        record["state"] = "completed"
        record["finished_at"] = _iso(self.clock())
        record["result"] = {"counts": counts}
        self._save(record)
        await self._post_result(record)

    # ------------------------------------------------------------- views

    def public_view(self, record: dict[str, Any]) -> dict[str, Any]:
        objectives = record.get("objectives") or []
        state = str(record.get("state") or "")
        completed = state == "completed"
        rows = []
        for objective in objectives:
            result = objective.get("result") or {}
            rows.append(
                {
                    "objective_id": objective.get("objective_id"),
                    "ro_number": objective.get("ro_number"),
                    "vehicle": objective.get("vehicle_label"),
                    "calibration": objective.get("calibration_title"),
                    "status": objective.get("status"),
                    "outcome": objective.get("outcome") or classify_objective(objective),
                    "outcome_label": OUTCOME_LABELS.get(objective.get("outcome") or classify_objective(objective)),
                    "title": result.get("title"),
                    "source_url": result.get("source_url"),
                    "review": result.get("review"),
                    "documents": result.get("documents"),
                    "dependencies": result.get("dependencies"),
                    "incomplete_reasons": result.get("incomplete_reasons"),
                    "attachments": objective.get("attachments"),
                    "reason": result.get("reason"),
                    "task_ids": result.get("task_ids"),
                    "metrics": (result.get("receipt") or {}).get("metrics") if result.get("receipt") else None,
                }
            )
        finished = sum(1 for item in objectives if item.get("status") not in {"pending", "in_progress"})
        attached = sum(1 for item in rows if item.get("outcome") == "attached")
        view: dict[str, Any] = {
            "service": "X Omni",
            "action": "adas_si_research",
            "job_id": record.get("job_id"),
            "status": state,
            "success": state != "failed",
            "executed": True,
            "verified": True,
            "work_complete": completed,
            "scope": record.get("scope_label"),
            "systems": record.get("systems") or None,
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "target_count": len(record.get("targets") or []),
            "targets": record.get("targets") or [],
            "objective_count": len(objectives),
            "progress": {"finished": finished, "total": len(objectives), "attached": attached},
            "objectives": rows,
            "problems": record.get("problems") or [],
        }
        if completed:
            counts = (record.get("result") or {}).get("counts") or {}
            view["counts"] = counts
            view["attached_count"] = int(counts.get("attached") or 0)
            view["groups"] = [
                {
                    "outcome": outcome,
                    "label": OUTCOME_LABELS[outcome],
                    "count": sum(1 for row in rows if row["outcome"] == outcome),
                    "objectives": [row for row in rows if row["outcome"] == outcome],
                }
                for outcome in OUTCOME_ORDER
                if any(row["outcome"] == outcome for row in rows)
            ]
            view["message"] = summary_sentence(record)
            if record.get("error"):
                view["note"] = record["error"]
        else:
            view["message"] = (
                f"Service-information research for {record.get('scope_label')} is running: "
                f"{finished} of {len(objectives)} procedure objectives finished, {attached} attached so far. "
                "Nothing is final until it finishes and each attachment is confirmed in Calibration IQ; "
                "the result posts to this chat when it is done."
            )
        return view

    async def _default_notify(self, user_id: str, title: str, body: str) -> Any:
        from . import push_notifications

        return await push_notifications.send_push_async(self.store, self.settings, user_id, title, body)

    async def _safe_notify(self, user_id: str, title: str, body: str) -> None:
        try:
            await self.notify(user_id, title, body)
        except Exception:  # noqa: BLE001
            log.warning("research push notification failed", exc_info=True)

    async def _post_result(self, record: dict[str, Any]) -> None:
        view = self.public_view(record)
        text = view.get("message") or summary_sentence(record)
        message_id = None
        try:
            message_id = self.store.add_message(
                int(record["conversation_id"]),
                "assistant",
                text,
                worker_used="core",
                artifacts=[{"type": "adas_si_research", "data": view}],
            )
        except Exception:  # noqa: BLE001
            log.exception("could not post research result message")
        await self._safe_notify(record["user_id"], "Service-information research finished", text)
        if self.publish is not None:
            try:
                self.publish(
                    {
                        "type": "conversation_updated",
                        "conversation_id": int(record["conversation_id"]),
                        "message_id": message_id,
                        "reason": "adas_si_research",
                    },
                    user_id=record["user_id"],
                )
            except Exception:  # noqa: BLE001
                log.warning("research live event failed", exc_info=True)
        record["notified"] = True
        record["result_message_id"] = message_id
        self._save(record)
