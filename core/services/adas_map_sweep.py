"""Background ADAS Map sweep: get every missing map in a scope, then report.

Otis's daily routine: after uploading new ROs, find every active RO that lacks
an ADAS Map, acquire and attach them, and report what could not be done. The
conversational model starts this with one ``stage_action`` call
(``sweep_adas_maps``); everything after that runs here, outside any chat turn,
so it survives the phone disconnecting and never fills the model's context.

Flow (all structural, no language interpretation):

1. Calibration IQ ADAS Map inventory for the requested scope (phases/shop,
   default the whole active board) -> the exact missing ROs.
2. ScrapeX exact batches of at most ten ROs, run one after another by
   ScrapeX's own background worker (the same per-item code that acquires the
   PDF, attaches it in Calibration IQ, and reconciles requirements).
3. One automatic retry batch for ROs that ended in ``needs_operator``; on
   2026-09-11 both such ROs succeeded on a second try, while every other
   failure state repeated identically.
4. Each swept RO is re-read in Calibration IQ. Only CIQ decides "attached".
5. One chat message with a result card in the originating conversation, a
   Web Push notification, and a live ``conversation_updated`` event.

Sweeps are persisted in ``state_records`` (namespace ``adas_map_sweep``) and
resumed after a Core restart; ScrapeX keeps processing a started batch on its
own meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Optional

log = logging.getLogger("xomni.adas_map_sweep")

NAMESPACE = "adas_map_sweep"
INVOCATION_KEY = "__xomni_invocation"

CHUNK_LIMIT = 10  # ScrapeX /api/batches/from-ciq/exact accepts at most ten ROs
MAX_TARGETS = 60
POLL_SECONDS = 15.0
STALL_SECONDS = 300.0
MAX_BATCH_RESTARTS = 3
MAX_READ_FAILURES = 20
SIGN_IN_WAIT_SECONDS = 1800.0
SIGN_IN_POLL_SECONDS = 30.0
MAX_RUN_SECONDS = 4 * 3600.0
SECONDS_PER_RO_ESTIMATE = 35

ACTIVE_STATES = frozenset({"running", "waiting_for_sign_in"})
RETRY_STATES = frozenset({"needs_operator"})
WORKER_RUNNING_STATES = frozenset({"running_adas_map", "pausing"})

OUTCOME_ORDER = (
    "attached",
    "not_in_adas_map",
    "page_would_not_open",
    "requirements_unverified",
    "needs_a_look",
    "not_confirmed_in_ciq",
    "ciq_unreadable",
    "not_processed",
)
OUTCOME_LABELS = {
    "attached": "Attached",
    "not_in_adas_map": "Not in ADAS Map yet",
    "page_would_not_open": "In ADAS Map, but the page would not open",
    "requirements_unverified": "Found, but requirements could not be verified",
    "needs_a_look": "Needs a look",
    "not_confirmed_in_ciq": "ScrapeX finished, but Calibration IQ does not show it",
    "ciq_unreadable": "Could not be re-read in Calibration IQ",
    "not_processed": "Not processed",
}
_OUTCOME_PHRASES = {
    "not_in_adas_map": "not in ADAS Map yet",
    "page_would_not_open": "in ADAS Map but the page would not open",
    "requirements_unverified": "found but the requirements could not be verified",
    "needs_a_look": "need a look",
    "not_confirmed_in_ciq": "finished in ScrapeX but not showing in Calibration IQ",
    "ciq_unreadable": "could not be re-read in Calibration IQ",
    "not_processed": "not processed",
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


def _clean_text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _phases(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else [value]
    phases: list[str] = []
    for item in items:
        text = _clean_text(item, 4)
        if not text.isdigit():
            raise ValueError("phases must be phase numbers, for example ['1', '2']")
        if text not in phases:
            phases.append(str(int(text)))
    return sorted(phases, key=int)[:16]


def _scope_label(phases: list[str], shop: str) -> str:
    if not phases:
        label = "the active board"
    elif len(phases) == 1:
        label = f"phase {phases[0]}"
    else:
        numbers = [int(value) for value in phases]
        contiguous = numbers == list(range(numbers[0], numbers[-1] + 1))
        label = (
            f"phases {numbers[0]}-{numbers[-1]}"
            if contiguous
            else "phases " + ", ".join(phases)
        )
    return f"{label} in {shop}" if shop else label


def classify_outcome(target: dict[str, Any], ciq_check: dict[str, Any]) -> str:
    """Structural outcome for one swept RO. Calibration IQ decides attachment."""

    if ciq_check.get("present") is True:
        return "attached"
    if ciq_check.get("readable") is False:
        return "ciq_unreadable"
    state = str(target.get("scrapex_state") or "")
    if state == "ro_not_found":
        return "not_in_adas_map"
    if state in {"view_not_found", "view_did_not_navigate"}:
        return "page_would_not_open"
    if state == "requirements_unparsed":
        return "requirements_unverified"
    if state == "adas_map_complete":
        return "not_confirmed_in_ciq"
    if state in {"needs_operator", "failed", "login_required", "ambiguous_ro", "vin_missing"}:
        return "needs_a_look"
    return "not_processed"


def summary_sentence(record: dict[str, Any]) -> str:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    total = int(result.get("target_count") or len(record.get("targets") or []))
    attached = int(counts.get("attached") or 0)
    scope = record.get("scope_label") or "the active board"
    lead = f"ADAS Map sweep for {scope} is done: {attached} of {total} attached."
    rest = [
        f"{int(counts[key])} {_OUTCOME_PHRASES[key]}"
        for key in OUTCOME_ORDER[1:]
        if int(counts.get(key) or 0)
    ]
    if not rest:
        return lead
    if len(rest) == 1:
        tail = rest[0]
    else:
        tail = ", ".join(rest[:-1]) + ", and " + rest[-1]
    return f"{lead} Of the rest, {tail}."


CONTEXT_RECENT_HOURS = 12.0
CONTEXT_MAX_CHARS = 500


def _clock_label(value: Any) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "earlier"
    return parsed.astimezone().strftime("%I:%M %p").lstrip("0")


def context_line(
    record: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    for_model: bool = True,
) -> Optional[str]:
    """One structured sentence of background-work truth for X's turn context.

    Present while a sweep runs and for a while after it finishes, so "continue"
    or "how'd the maps go" is answered from Core's own record instead of from
    the model's guess. Returns None when there is nothing current to say.
    """

    state = str(record.get("state") or "")
    scope = record.get("scope_label") or "the active board"
    targets = record.get("targets") or []
    if state in ACTIVE_STATES:
        finished = sum(1 for target in targets if target.get("finished"))
        complete = sum(
            1 for target in targets if target.get("scrapex_state") == "adas_map_complete"
        )
        if state == "waiting_for_sign_in":
            line = (
                f"ADAS Map sweep for {scope} (started {_clock_label(record.get('started_at'))}) "
                "is paused waiting for ADAS Map sign-in in the work browser; it continues "
                "on its own after sign-in. No final result exists yet."
            )
        else:
            line = (
                f"ADAS Map sweep for {scope} is still running in the background "
                f"(started {_clock_label(record.get('started_at'))}): {finished} of "
                f"{len(targets)} ROs processed, ScrapeX reports {complete} complete so far. "
                "No final result exists yet; results post to the chat when it finishes."
            )
            if for_model:
                line += (
                    " For its latest progress read query_ciq kind=adas_map_sweep; never "
                    "report an outcome it has not returned."
                )
        return line[:CONTEXT_MAX_CHARS]
    if state == "completed":
        finished_at = _parse_iso(record.get("finished_at"))
        reference = now or _now()
        if finished_at is None or (reference - finished_at).total_seconds() > CONTEXT_RECENT_HOURS * 3600:
            return None
        return (
            f"Finished at {_clock_label(record.get('finished_at'))}. " + summary_sentence(record)
        )[:CONTEXT_MAX_CHARS]
    return None


def latest_context_line(
    store: Any, user_id: Optional[str], *, for_model: bool = True
) -> Optional[str]:
    """Context line for the user's latest sweep, or None. Never raises.

    ``for_model=False`` returns the same structured status without the
    model-directed reading instruction, for text shown to Otis.
    """

    try:
        rows = store.list_records(NAMESPACE, user_id=user_id, limit=1)
    except Exception:  # noqa: BLE001 - lightweight test stores have no records
        return None
    if not rows or not isinstance(rows[0].get("payload"), dict):
        return None
    try:
        return context_line(rows[0]["payload"], for_model=for_model)
    except Exception:  # noqa: BLE001
        log.warning("could not render sweep context", exc_info=True)
        return None


# ---------------------------------------------------------------- defaults


class ScrapeXSweepClient:
    """The production ScrapeX calls the sweep needs, all contract-validated."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ready(self) -> Optional[dict[str, Any]]:
        from . import scrapex

        return await scrapex.adas_map_ready(self.settings)

    async def signed_in(self) -> Optional[bool]:
        from . import scrapex

        return (await scrapex.adas_map_signed_in(self.settings)).get("signed_in")

    async def open_sign_in(self) -> dict[str, Any]:
        from . import scrapex

        return await scrapex.adas_map(self.settings, {"action": "open_authentication"})

    async def create_batch(self, name: str, ro_numbers: list[str]) -> dict[str, Any]:
        from . import scrapex

        return await scrapex.adas_map(
            self.settings,
            {
                "action": "create_exact_batch",
                "name": name,
                "ro_numbers": ro_numbers,
                "source_scope": "all",
            },
        )

    async def start_batch(self, batch_id: str) -> dict[str, Any]:
        from . import scrapex

        return await scrapex.adas_map(
            self.settings, {"action": "start_batch", "batch_id": batch_id}
        )

    async def batch(self, batch_id: str) -> dict[str, Any]:
        from . import scrapex

        return await scrapex.adas_map_batch(self.settings, batch_id)


def default_inventory(settings: Any) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def inventory(scope: dict[str, Any]) -> dict[str, Any]:
        from . import calibration_iq_work_prep as work_prep

        args: dict[str, Any] = {"mode": "adas_map_inventory"}
        if scope.get("phases"):
            args["phases"] = list(scope["phases"])
        if scope.get("shop"):
            args["shop"] = scope["shop"]
        return await work_prep.handle(settings, None, args)

    return inventory


def default_ro_map_state(settings: Any) -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def ro_map_state(identifier: str) -> dict[str, Any]:
        from . import calibration_iq_work_prep as work_prep

        envelope = await work_prep._load_ro_snapshot(settings, identifier)  # noqa: SLF001
        if envelope.get("status") != "verified" or not isinstance(
            envelope.get("snapshot"), dict
        ):
            return {
                "readable": False,
                "present": False,
                "status": "unreadable",
                "message": _clean_text(envelope.get("message"), 300),
            }
        info = work_prep.extract_adas_map(envelope["snapshot"])
        status = str(info.get("status") or "not_found")
        return {
            "readable": True,
            "present": status in {"verified", "present_unparsed"},
            "status": status,
        }

    return ro_map_state


# ----------------------------------------------------------------- service


class AdasMapSweepService:
    def __init__(
        self,
        settings: Any,
        store: Any,
        *,
        scrapex: Any = None,
        inventory: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
        ro_map_state: Optional[Callable[[str], Awaitable[dict[str, Any]]]] = None,
        notify: Optional[Callable[[str, str, str], Awaitable[Any]]] = None,
        publish: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], datetime] = _now,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.store = store
        self.scrapex = scrapex or ScrapeXSweepClient(settings)
        self.inventory = inventory or default_inventory(settings)
        self.ro_map_state = ro_map_state or default_ro_map_state(settings)
        self.notify = notify or self._default_notify
        self.publish = publish
        self.sleep = sleep
        self.clock = clock
        self.poll_seconds = poll_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------ records

    def _save(self, record: dict[str, Any]) -> dict[str, Any]:
        record["updated_at"] = _iso(self.clock())
        self.store.put_record(
            NAMESPACE, record["sweep_id"], record, user_id=record["user_id"]
        )
        return record

    def _load(self, user_id: str, sweep_id: str) -> Optional[dict[str, Any]]:
        return self.store.get_record(NAMESPACE, sweep_id, user_id=user_id)

    def _records(self, user_id: Optional[str]) -> list[dict[str, Any]]:
        try:
            rows = self.store.list_records(NAMESPACE, user_id=user_id, limit=50)
        except AttributeError:
            return []
        return [row["payload"] for row in rows if isinstance(row.get("payload"), dict)]

    def running_sweep(self, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        for record in self._records(user_id):
            if record.get("state") in ACTIVE_STATES:
                return record
        return None

    def has_running(self) -> bool:
        return self.running_sweep(None) is not None

    # ----------------------------------------------------------- handlers

    async def start(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handler for the concrete ``adas_map_sweep`` tool (via stage_action)."""

        payload = dict(args or {})
        context = payload.pop(INVOCATION_KEY, None)
        if not isinstance(context, dict) or not context.get("conversation_id"):
            return self._not_started(
                "context_missing",
                "The sweep must be started from a conversation; nothing was started.",
            )
        try:
            phases = _phases(payload.get("phases"))
        except ValueError as exc:
            return self._not_started("invalid_scope", str(exc))
        shop = _clean_text(payload.get("shop"), 60)
        user_id = str(context.get("user_id") or "local-dev")
        scope = {"phases": phases, "shop": shop or None}
        label = _scope_label(phases, shop)

        async with self._start_lock:
            running = self.running_sweep(user_id)
            if running is not None:
                self._ensure_driver(running)
                view = self.public_view(running)
                view.update(
                    {
                        "status": "already_running",
                        "executed": False,
                        "message": (
                            f"An ADAS Map sweep for {running.get('scope_label')} is already "
                            "running; nothing new was started. Its results will be posted "
                            "in its conversation when it finishes."
                        ),
                    }
                )
                return view

            inventory = await self.inventory(scope)
            if not (
                isinstance(inventory, dict)
                and inventory.get("verified") is True
                and isinstance(inventory.get("missing_repair_orders"), list)
            ):
                return self._not_started(
                    "inventory_unverified",
                    _clean_text(
                        (inventory or {}).get("message")
                        if isinstance(inventory, dict)
                        else "",
                        300,
                    )
                    or "The Calibration IQ ADAS Map inventory could not be verified; "
                    "nothing was started.",
                )

            counts = {
                "queue_count": int(inventory.get("queue_count") or 0),
                "present_count": int(inventory.get("adas_map_present_count") or 0),
                "missing_count": int(inventory.get("adas_map_missing_count") or 0),
                "unverified_count": int(inventory.get("adas_map_unverified_count") or 0),
            }
            missing = [
                row
                for row in inventory["missing_repair_orders"]
                if isinstance(row, dict) and _clean_text(row.get("ro_number"), 40)
            ]
            if not missing:
                return {
                    "service": "X Omni",
                    "action": "adas_map_sweep",
                    "status": "nothing_missing",
                    "success": True,
                    "executed": False,
                    "verified": True,
                    "work_complete": True,
                    "scope": label,
                    "inventory": counts,
                    "message": (
                        f"Calibration IQ shows no active RO missing an ADAS Map in {label}; "
                        "nothing needed to be acquired."
                    ),
                }

            # ScrapeX readiness only matters once there is work: "nothing
            # missing" never starts ScrapeX or opens the sign-in window.
            not_ready = await self.scrapex.ready()
            if not_ready is not None:
                return self._not_started(
                    str(not_ready.get("status") or "scrapex_not_ready"),
                    str(
                        not_ready.get("message")
                        or "ScrapeX is not ready for ADAS Map work; nothing was started."
                    ),
                    authentication_required=bool(not_ready.get("authentication_required")),
                    requires_human=bool(not_ready.get("requires_human")),
                )

            started = self.clock()
            targets = [
                {
                    "ro_number": _clean_text(row.get("ro_number"), 40),
                    "repair_order_id": _clean_text(row.get("repair_order_id"), 80) or None,
                    "vehicle": _clean_text(row.get("vehicle"), 120),
                    "phase": _clean_text(row.get("phase"), 8),
                    "scrapex_state": None,
                    "last_error": "",
                    "passes": 0,
                    "outcome": None,
                }
                for row in missing[:MAX_TARGETS]
            ]
            ro_numbers = [target["ro_number"] for target in targets]
            record: dict[str, Any] = {
                "sweep_id": uuid.uuid4().hex[:16],
                "user_id": user_id,
                "conversation_id": int(context["conversation_id"]),
                "message_id": context.get("message_id"),
                "tool_call_id": context.get("tool_call_id"),
                "scope": scope,
                "scope_label": label,
                "state": "running",
                "started_at": _iso(started),
                "updated_at": _iso(started),
                "finished_at": None,
                "inventory": counts,
                "truncated_count": max(0, len(missing) - len(targets)),
                "targets": targets,
                "retry_pass_done": False,
                "chunks": [
                    {
                        "index": index,
                        "pass": 1,
                        "ro_numbers": ro_numbers[offset : offset + CHUNK_LIMIT],
                        "batch_id": None,
                        "state": "pending",
                        "restarts": 0,
                        "read_failures": 0,
                        "last_progress_at": None,
                    }
                    for index, offset in enumerate(range(0, len(ro_numbers), CHUNK_LIMIT))
                ],
                "sign_in_wait_started_at": None,
                "result": None,
                "notified": False,
                "error": None,
            }
            self._save(record)

            launched = await self._launch_chunk(record, record["chunks"][0])
            if launched != "running":
                record["state"] = "failed"
                record["finished_at"] = _iso(self.clock())
                record["error"] = record.get("error") or "The first ScrapeX batch did not start."
                self._save(record)
                return self._not_started(
                    "authentication_required" if launched == "sign_in" else "batch_not_started",
                    (
                        "ADAS Map sign-in is required before the sweep can start; the "
                        "sign-in window was opened and nothing was acquired."
                        if launched == "sign_in"
                        else f"ScrapeX did not start the batch: {record['error']} Nothing was acquired."
                    ),
                    authentication_required=launched == "sign_in",
                    requires_human=launched == "sign_in",
                )
            self._save(record)
            self._ensure_driver(record)

        minutes = max(1, math.ceil(len(targets) * SECONDS_PER_RO_ESTIMATE / 60))
        view = self.public_view(record)
        view.update(
            {
                "status": "running",
                "executed": True,
                "verified": True,
                "estimated_minutes": minutes,
                "message": (
                    f"Started the ADAS Map sweep for {label}: {len(targets)} ROs are missing "
                    "a map. ScrapeX is acquiring and attaching them in the background, and "
                    f"the results will be posted in this chat in about {minutes} minutes. "
                    "Nothing is attached yet."
                    + (
                        f" {record['truncated_count']} more missing ROs exceed this sweep's "
                        f"limit of {MAX_TARGETS} and were not included."
                        if record["truncated_count"]
                        else ""
                    )
                ),
            }
        )
        return view

    async def status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Handler for ``adas_map_sweep_status`` (query_ciq kind=adas_map_sweep)."""

        payload = dict(args or {})
        context = payload.pop(INVOCATION_KEY, None)
        user_id = str((context or {}).get("user_id") or "local-dev")
        records = self._records(user_id)
        if not records:
            return {
                "service": "X Omni",
                "action": "adas_map_sweep_status",
                "status": "no_sweep",
                "success": True,
                "executed": True,
                "verified": True,
                "message": "No ADAS Map sweep has been started yet.",
            }
        record = records[0]
        if record.get("state") in ACTIVE_STATES:
            self._ensure_driver(record)
        view = self.public_view(record)
        view["action"] = "adas_map_sweep_status"
        return view

    # ------------------------------------------------------------ lifecycle

    async def resume(self) -> int:
        """Resume unfinished sweeps after a Core restart. Returns the count."""

        resumed = 0
        for record in self._records(None):
            if record.get("state") in ACTIVE_STATES:
                self._ensure_driver(record)
                resumed += 1
            elif record.get("state") == "completed" and not record.get("notified"):
                try:
                    await self._post_result(record)
                except Exception:  # noqa: BLE001
                    log.exception("could not post a completed sweep after restart")
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
        sweep_id = record["sweep_id"]
        task = self._tasks.get(sweep_id)
        if task is not None and not task.done():
            return
        self._tasks[sweep_id] = asyncio.create_task(
            self._drive(sweep_id, record["user_id"]),
            name=f"adas-map-sweep-{sweep_id}",
        )

    # -------------------------------------------------------------- driver

    async def _launch_chunk(self, record: dict[str, Any], chunk: dict[str, Any]) -> str:
        """Create and start one ScrapeX exact batch. Returns running/sign_in/failed."""

        created = await self.scrapex.create_batch(
            f"ADAS Map sweep {record['sweep_id']} part {chunk['index'] + 1}",
            list(chunk["ro_numbers"]),
        )
        if created.get("status") == "authentication_required":
            return "sign_in"
        data = created.get("data") if isinstance(created.get("data"), dict) else {}
        batch_id = _clean_text(data.get("id"), 80)
        if not (created.get("success") is True and batch_id):
            record["error"] = _clean_text(
                created.get("message")
                or (created.get("error") or {}).get("message")
                or created.get("status"),
                300,
            )
            return "failed"
        chunk["batch_id"] = batch_id
        started = await self.scrapex.start_batch(batch_id)
        if started.get("status") == "authentication_required":
            chunk["state"] = "created"
            return "sign_in"
        if started.get("success") is not True:
            record["error"] = _clean_text(
                started.get("message")
                or (started.get("error") or {}).get("message")
                or started.get("status"),
                300,
            )
            chunk["state"] = "created"
            return "failed"
        chunk["state"] = "running"
        chunk["last_progress_at"] = _iso(self.clock())
        for target in record["targets"]:
            if target["ro_number"] in chunk["ro_numbers"]:
                target["passes"] = int(target.get("passes") or 0) + 1
        return "running"

    def _apply_items(self, record: dict[str, Any], chunk: dict[str, Any], items: list[dict]) -> bool:
        by_ro = {target["ro_number"]: target for target in record["targets"]}
        changed = False
        for item in items:
            target = by_ro.get(item.get("ro_number"))
            if target is None or item.get("ro_number") not in chunk["ro_numbers"]:
                continue
            state = item.get("adas_map_state") or "pending"
            if item.get("complete"):
                state = "adas_map_complete"
            if state != target.get("scrapex_state") or item.get("finished") != target.get("finished"):
                changed = True
            target["scrapex_state"] = state
            target["finished"] = bool(item.get("finished"))
            error = _clean_text(item.get("adas_map_last_error"), 300)
            if error:
                target["last_error"] = error
        return changed

    async def _enter_sign_in_wait(self, record: dict[str, Any]) -> None:
        if record.get("state") == "waiting_for_sign_in":
            return
        record["state"] = "waiting_for_sign_in"
        record["sign_in_wait_started_at"] = _iso(self.clock())
        self._save(record)
        try:
            await self.scrapex.open_sign_in()
        except Exception:  # noqa: BLE001
            log.warning("could not open the ADAS Map sign-in window", exc_info=True)
        await self._safe_notify(
            record["user_id"],
            "ADAS Map sign-in needed",
            f"The ADAS Map sweep for {record.get('scope_label')} is paused until you sign "
            "in to ADAS Map in the work browser. It continues on its own after sign-in.",
        )

    async def _drive(self, sweep_id: str, user_id: str) -> None:
        try:
            while True:
                record = self._load(user_id, sweep_id)
                if record is None or record.get("state") not in ACTIVE_STATES:
                    return
                started = _parse_iso(record.get("started_at")) or self.clock()
                if (self.clock() - started).total_seconds() > MAX_RUN_SECONDS:
                    record["error"] = "The sweep exceeded its time limit."
                    await self._finalize(record)
                    return

                if record["state"] == "waiting_for_sign_in":
                    signed_in = await self.scrapex.signed_in()
                    if signed_in:
                        record["state"] = "running"
                        record["sign_in_wait_started_at"] = None
                        self._save(record)
                        continue
                    waited_from = _parse_iso(record.get("sign_in_wait_started_at")) or self.clock()
                    if (self.clock() - waited_from).total_seconds() > SIGN_IN_WAIT_SECONDS:
                        record["error"] = "ADAS Map sign-in was not completed in time."
                        await self._finalize(record)
                        return
                    await self.sleep(SIGN_IN_POLL_SECONDS)
                    continue

                chunk = next(
                    (item for item in record["chunks"] if item["state"] != "done"),
                    None,
                )
                if chunk is None:
                    if not record.get("retry_pass_done"):
                        record["retry_pass_done"] = True
                        retry = [
                            target["ro_number"]
                            for target in record["targets"]
                            if target.get("scrapex_state") in RETRY_STATES
                        ]
                        base = len(record["chunks"])
                        for offset in range(0, len(retry), CHUNK_LIMIT):
                            record["chunks"].append(
                                {
                                    "index": base + offset // CHUNK_LIMIT,
                                    "pass": 2,
                                    "ro_numbers": retry[offset : offset + CHUNK_LIMIT],
                                    "batch_id": None,
                                    "state": "pending",
                                    "restarts": 0,
                                    "read_failures": 0,
                                    "last_progress_at": None,
                                }
                            )
                        self._save(record)
                        continue
                    await self._finalize(record)
                    return

                if chunk["state"] == "pending" or not chunk.get("batch_id"):
                    launched = await self._launch_chunk(record, chunk)
                    if launched == "sign_in":
                        await self._enter_sign_in_wait(record)
                        continue
                    if launched == "failed":
                        chunk["state"] = "done"
                        chunk["failed"] = True
                    self._save(record)
                    continue

                if chunk["state"] == "created":
                    started_result = await self.scrapex.start_batch(chunk["batch_id"])
                    if started_result.get("status") == "authentication_required":
                        await self._enter_sign_in_wait(record)
                        continue
                    if started_result.get("success") is True:
                        chunk["state"] = "running"
                        chunk["last_progress_at"] = _iso(self.clock())
                    else:
                        chunk["restarts"] = int(chunk.get("restarts") or 0) + 1
                        if chunk["restarts"] > MAX_BATCH_RESTARTS:
                            chunk["state"] = "done"
                            chunk["failed"] = True
                    self._save(record)
                    if chunk["state"] != "running":
                        await self.sleep(self.poll_seconds)
                    continue

                batch = await self.scrapex.batch(chunk["batch_id"])
                if batch.get("success") is not True:
                    chunk["read_failures"] = int(chunk.get("read_failures") or 0) + 1
                    if chunk["read_failures"] >= MAX_READ_FAILURES:
                        chunk["state"] = "done"
                        chunk["failed"] = True
                    self._save(record)
                    await self.sleep(self.poll_seconds)
                    continue
                chunk["read_failures"] = 0
                if self._apply_items(record, chunk, batch.get("items") or []):
                    chunk["last_progress_at"] = _iso(self.clock())
                if batch.get("finished"):
                    chunk["state"] = "done"
                    self._save(record)
                    continue

                progress_at = _parse_iso(chunk.get("last_progress_at")) or self.clock()
                stalled = (self.clock() - progress_at).total_seconds() > STALL_SECONDS
                if batch.get("batch_state") in WORKER_RUNNING_STATES and not stalled:
                    self._save(record)
                    await self.sleep(self.poll_seconds)
                    continue

                # The worker stopped (paused, restarted, or stalled) with ROs
                # still unfinished. start_batch is idempotent: it reports
                # already_running when the worker is genuinely busy.
                signed_in = await self.scrapex.signed_in()
                if signed_in is False:
                    await self._enter_sign_in_wait(record)
                    continue
                chunk["restarts"] = int(chunk.get("restarts") or 0) + 1
                if chunk["restarts"] > MAX_BATCH_RESTARTS:
                    chunk["state"] = "done"
                    chunk["failed"] = True
                    self._save(record)
                    continue
                restarted = await self.scrapex.start_batch(chunk["batch_id"])
                if restarted.get("status") == "authentication_required":
                    await self._enter_sign_in_wait(record)
                    continue
                chunk["last_progress_at"] = _iso(self.clock())
                self._save(record)
                await self.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leave a sweep silently stuck
            log.exception("ADAS Map sweep %s failed", sweep_id)
            record = self._load(user_id, sweep_id)
            if record is not None and record.get("state") in ACTIVE_STATES:
                record["error"] = f"{type(exc).__name__}: {exc}"[:300]
                try:
                    await self._finalize(record)
                except Exception:  # noqa: BLE001
                    log.exception("could not finalize failed sweep %s", sweep_id)
        finally:
            task = self._tasks.get(sweep_id)
            if task is not None and task is asyncio.current_task():
                self._tasks.pop(sweep_id, None)

    async def _finalize(self, record: dict[str, Any]) -> None:
        semaphore = asyncio.Semaphore(6)

        async def check(target: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            async with semaphore:
                try:
                    state = await self.ro_map_state(
                        target.get("repair_order_id") or target["ro_number"]
                    )
                except Exception as exc:  # noqa: BLE001
                    state = {"readable": False, "present": False, "message": str(exc)[:200]}
                return target, state

        checks = await asyncio.gather(*(check(target) for target in record["targets"]))
        counts = {key: 0 for key in OUTCOME_ORDER}
        for target, state in checks:
            outcome = classify_outcome(target, state)
            target["outcome"] = outcome
            target["ciq_map_status"] = state.get("status")
            counts[outcome] += 1

        missing_after: Optional[int] = None
        try:
            after = await self.inventory(record.get("scope") or {})
            if isinstance(after, dict) and after.get("verified") is True:
                missing_after = int(after.get("adas_map_missing_count") or 0)
        except Exception:  # noqa: BLE001
            log.warning("post-sweep inventory failed", exc_info=True)

        record["result"] = {
            "target_count": len(record["targets"]),
            "counts": {key: value for key, value in counts.items() if value},
            "missing_after": missing_after,
            "retried": sum(
                1 for target in record["targets"] if int(target.get("passes") or 0) > 1
            ),
        }
        record["state"] = "completed"
        record["finished_at"] = _iso(self.clock())
        self._save(record)
        await self._post_result(record)

    # --------------------------------------------------------------- output

    def public_view(self, record: dict[str, Any]) -> dict[str, Any]:
        targets = record.get("targets") or []
        state = str(record.get("state") or "")
        completed = state == "completed"
        view: dict[str, Any] = {
            "service": "X Omni",
            "action": "adas_map_sweep",
            "sweep_id": record.get("sweep_id"),
            "status": state,
            "success": state != "failed",
            "executed": True,
            "verified": True,
            "work_complete": completed,
            "scope": record.get("scope_label"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "target_count": len(targets),
            "inventory": record.get("inventory"),
        }
        if completed:
            result = record.get("result") or {}
            groups = []
            for outcome in OUTCOME_ORDER:
                members = [target for target in targets if target.get("outcome") == outcome]
                if not members:
                    continue
                groups.append(
                    {
                        "outcome": outcome,
                        "label": OUTCOME_LABELS[outcome],
                        "count": len(members),
                        "ros": [
                            {
                                "ro_number": target["ro_number"],
                                "vehicle": target.get("vehicle"),
                                "phase": target.get("phase"),
                                **(
                                    {"reason": target["last_error"]}
                                    if outcome != "attached" and target.get("last_error")
                                    else {}
                                ),
                            }
                            for target in members
                        ],
                    }
                )
            view.update(
                {
                    "attached_count": int((result.get("counts") or {}).get("attached") or 0),
                    "counts": result.get("counts") or {},
                    "groups": groups,
                    "missing_after": result.get("missing_after"),
                    "retried_count": result.get("retried") or 0,
                    "message": summary_sentence(record)
                    + (
                        f" Calibration IQ now shows {result['missing_after']} missing in "
                        f"{record.get('scope_label')}."
                        if isinstance(result.get("missing_after"), int)
                        else ""
                    ),
                }
            )
            if record.get("error"):
                view["note"] = record["error"]
            return view

        finished = sum(1 for target in targets if target.get("finished"))
        complete_so_far = sum(
            1 for target in targets if target.get("scrapex_state") == "adas_map_complete"
        )
        view.update(
            {
                "progress": {"finished": finished, "total": len(targets)},
                "scrapex_complete_so_far": complete_so_far,
                "ros": [
                    {
                        "ro_number": target["ro_number"],
                        "vehicle": target.get("vehicle"),
                        "phase": target.get("phase"),
                        "scrapex_state": target.get("scrapex_state") or "pending",
                    }
                    for target in targets
                ],
                "message": (
                    f"The ADAS Map sweep for {record.get('scope_label')} is waiting for ADAS "
                    "Map sign-in in the work browser; it continues on its own after sign-in."
                    if state == "waiting_for_sign_in"
                    else (
                        f"The ADAS Map sweep for {record.get('scope_label')} failed: "
                        f"{record.get('error') or 'unknown error'}."
                        if state == "failed"
                        else (
                            f"The ADAS Map sweep for {record.get('scope_label')} is running: "
                            f"{finished} of {len(targets)} ROs processed, ScrapeX reports "
                            f"{complete_so_far} complete so far. Nothing is final until it "
                            "finishes and each RO is re-checked in Calibration IQ. Reading "
                            "this again now will not show more progress; the result posts "
                            "to this chat when it finishes."
                        )
                    )
                ),
            }
        )
        return view

    def _not_started(
        self,
        status: str,
        message: str,
        *,
        authentication_required: bool = False,
        requires_human: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "service": "X Omni",
            "action": "adas_map_sweep",
            "status": status,
            "success": False,
            "executed": False,
            "verified": False,
            "work_complete": False,
            "message": message,
        }
        if authentication_required:
            result["authentication_required"] = True
        if requires_human:
            result["requires_human"] = True
        return result

    async def _default_notify(self, user_id: str, title: str, body: str) -> Any:
        from . import push_notifications

        return await push_notifications.send_push_async(
            self.store, self.settings, user_id, title, body
        )

    async def _safe_notify(self, user_id: str, title: str, body: str) -> None:
        try:
            await self.notify(user_id, title, body)
        except Exception:  # noqa: BLE001 - notification is best effort
            log.warning("sweep push notification failed", exc_info=True)

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
                artifacts=[{"type": "adas_map_sweep", "data": view}],
            )
        except Exception:  # noqa: BLE001
            log.exception("could not post sweep result message")
        await self._safe_notify(record["user_id"], "ADAS Map sweep finished", text)
        if self.publish is not None:
            try:
                self.publish(
                    {
                        "type": "conversation_updated",
                        "conversation_id": int(record["conversation_id"]),
                        "message_id": message_id,
                        "reason": "adas_map_sweep",
                    },
                    user_id=record["user_id"],
                )
            except Exception:  # noqa: BLE001
                log.warning("sweep live event failed", exc_info=True)
        record["notified"] = True
        record["result_message_id"] = message_id
        self._save(record)
