"""Persistent weekly-readiness missing-SI queue.

A "make sure we're prepared for the week" run already identifies exactly
which active Calibration IQ ROs are missing ADAS SI coverage. Working
through that list one vehicle at a time means the operator repeating the
RO number or vehicle every turn -- pick a vehicle in ALLDATA, say "collect
the ADAS info for RO 12345", repeat 53 times.

This module remembers that missing-SI list per conversation so a short
"next" can walk it: X resolves whatever vehicle is currently selected in
ALLDATA against the remaining queue, collects for it if it matches, marks
it complete, and reports which vehicle to pull up next.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# A queue is considered stale, and no longer eligible for "next" to resume,
# once this much wall-clock time has passed without an update. Working a
# real 53-car queue can reasonably span days in the same conversation, so
# this is time-based rather than turn-count-based -- unlike the short
# ALLDATA-login continuity window, which only needs to survive a few turns.
STALE_AFTER_SECONDS = 7 * 24 * 3600

# A queue-next attempt is persisted as ``running`` before the external
# ALLDATA/Calibration IQ boundary is crossed.  If the process stops after
# that write, no worker remains to produce a terminal state.  Thirty minutes
# is deliberately longer than the bounded collector's normal work while
# still ensuring an interrupted row becomes actionable again without a human
# editing the JSON file.
RUNNING_STALE_AFTER_SECONDS = 30 * 60
_INTERRUPTED_ATTEMPT_MESSAGE = (
    "The prior collection attempt remained running beyond the 30-minute "
    "recovery window and was made retryable."
)

# A normal weekly run is about fifty ROs.  Keep enough headroom for a busy
# week while making both the on-disk state and tool responses predictably
# bounded if an upstream service returns malformed or duplicate data.
MAX_QUEUE_ITEMS = 100
MAX_STORED_CONVERSATIONS = 256

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AUTHENTICATION_REQUIRED = "authentication_required"
STATUS_RETRYABLE = "retryable"
STATUS_COMPLETED = "completed"
STATUS_BLOCKED = "blocked"

LIFECYCLE_STATUSES = frozenset(
    {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_AUTHENTICATION_REQUIRED,
        STATUS_RETRYABLE,
        STATUS_COMPLETED,
        STATUS_BLOCKED,
    }
)
UNRESOLVED_STATUSES = frozenset(LIFECYCLE_STATUSES - {STATUS_COMPLETED})
FAILURE_STATUSES = frozenset(
    {STATUS_AUTHENTICATION_REQUIRED, STATUS_RETRYABLE, STATUS_BLOCKED}
)

# Persisted queues from the pre-lifecycle implementation remain usable. A
# legacy failed row is deliberately retained as blocked instead of silently
# disappearing from the next/list views.
_LEGACY_STATUS_MAP = {
    "pending": STATUS_QUEUED,
    "complete": STATUS_COMPLETED,
    "failed": STATUS_BLOCKED,
}


def normalize_status(value: Any) -> str:
    raw = str(value or STATUS_QUEUED).strip().casefold()
    normalized = _LEGACY_STATUS_MAP.get(raw, raw)
    return normalized if normalized in LIFECYCLE_STATUSES else STATUS_BLOCKED


@dataclass
class WeeklyQueueItem:
    repair_order_id: str
    ro_number: str = ""
    vehicle_label: str = ""
    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model_trim: Optional[str] = None
    missing_calibrations: list[str] = field(default_factory=list)
    # Calibrations whose SI coverage could not be proven either way -- not
    # the same claim as missing_calibrations (a confirmed gap). Otis chose to
    # have both categories walked and listed together: an unverified result
    # is still something to go check in the field, even though it isn't yet
    # a proven miss.
    unverified_calibrations: list[str] = field(default_factory=list)
    category: str = "missing"  # "missing" | "unverified"
    status: str = STATUS_QUEUED
    attempts: int = 0
    last_error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status_changed_at: float = field(default_factory=time.time)
    last_attempt_at: Optional[float] = None
    completed_at: Optional[float] = None

    def __post_init__(self) -> None:
        self.status = normalize_status(self.status)
        self.attempts = max(0, int(self.attempts or 0))
        self.last_error = str(self.last_error or "")[:2000]

    def transition(
        self,
        status: str,
        *,
        error: Any = None,
        now: Optional[float] = None,
        begin_attempt: bool = False,
    ) -> None:
        """Persist lifecycle truth without interpreting conversational text."""

        target = normalize_status(status)
        current = time.time() if now is None else float(now)
        if begin_attempt:
            self.attempts += 1
            self.last_attempt_at = current
        if target != self.status:
            self.status_changed_at = current
        self.status = target
        self.updated_at = current
        if error is not None:
            if isinstance(error, str):
                rendered = error
            else:
                try:
                    rendered = json.dumps(error, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    rendered = str(error)
            self.last_error = rendered[:2000]
        elif target in {STATUS_RUNNING, STATUS_COMPLETED}:
            self.last_error = ""
        self.completed_at = current if target == STATUS_COMPLETED else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeeklyQueueItem":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass introspection
        values = {k: v for k, v in data.items() if k in known}
        values["status"] = normalize_status(values.get("status"))
        return cls(**values)


@dataclass
class WeeklyQueue:
    conversation_id: str
    items: list[WeeklyQueueItem] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def is_stale(self, *, now: Optional[float] = None, max_seconds: float = STALE_AFTER_SECONDS) -> bool:
        current = now if now is not None else time.time()
        return (current - self.updated_at) > max_seconds

    def with_statuses(self, statuses: set[str] | frozenset[str]) -> list[WeeklyQueueItem]:
        normalized = {normalize_status(status) for status in statuses}
        return [item for item in self.items if item.status in normalized]

    def unresolved(self) -> list[WeeklyQueueItem]:
        """All unfinished rows, including human and terminal blockers."""

        return self.with_statuses(UNRESOLVED_STATUSES)

    def failures(self) -> list[WeeklyQueueItem]:
        """Rows that could not finish, retained for structured reporting."""

        return self.with_statuses(FAILURE_STATUSES)

    def actionable(self) -> list[WeeklyQueueItem]:
        """Rows that queue-next may attempt after checking live auth state."""

        return self.with_statuses(
            {STATUS_QUEUED, STATUS_RETRYABLE, STATUS_AUTHENTICATION_REQUIRED}
        )

    def pending(self) -> list[WeeklyQueueItem]:
        """Backward-compatible name for queue-next's actionable rows."""

        return self.actionable()

    def completed(self) -> list[WeeklyQueueItem]:
        return self.with_statuses({STATUS_COMPLETED})

    def recover_stale_running(
        self,
        *,
        now: Optional[float] = None,
        max_seconds: float = RUNNING_STALE_AFTER_SECONDS,
    ) -> int:
        """Make abandoned in-flight rows actionable without losing attempts.

        ``status_changed_at`` is the authoritative start of the persisted
        running state.  ``last_attempt_at`` and ``updated_at`` are fallbacks
        for older records.  A row at exactly the boundary is retained; it is
        recovered only after the full bounded window has elapsed.
        """

        current = time.time() if now is None else float(now)
        recovered = 0
        for item in self.items:
            if item.status != STATUS_RUNNING:
                continue
            timestamps = [
                value
                for value in (
                    item.status_changed_at,
                    item.last_attempt_at,
                    item.updated_at,
                )
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            started_at = max(timestamps) if timestamps else self.updated_at
            if current - float(started_at or 0.0) <= max_seconds:
                continue
            item.transition(
                STATUS_RETRYABLE,
                error=_INTERRUPTED_ATTEMPT_MESSAGE,
                now=current,
            )
            recovered += 1
        return recovered

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "items": [item.to_dict() for item in self.items],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeeklyQueue":
        raw_items = data.get("items")
        item_pairs = [
            (item, WeeklyQueueItem.from_dict(item))
            for item in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(item, dict)
        ][:MAX_QUEUE_ITEMS]
        queue = cls(
            conversation_id=str(data.get("conversation_id") or ""),
            items=[item for _, item in item_pairs],
            updated_at=float(data.get("updated_at") or 0.0),
        )
        # Old records had only a queue timestamp. Preserve that provenance
        # instead of pretending their item timestamps were created now.
        for raw_item, item in item_pairs:
            for field_name in ("created_at", "updated_at", "status_changed_at"):
                if field_name not in raw_item:
                    setattr(item, field_name, queue.updated_at)
        return queue


class WeeklyQueueStore:
    """Thread-safe, JSON-backed per-conversation queue store.

    Mirrors research_task.ResearchTaskStore -- one small file, one lock, no
    new infrastructure dependency.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read_all(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def get(self, conversation_id: str) -> Optional[WeeklyQueue]:
        with self._lock:
            data = self._read_all()
            raw = data.get(str(conversation_id))
            if not isinstance(raw, dict):
                return None
            try:
                queue = WeeklyQueue.from_dict(raw)
            except Exception:
                return None
            if queue.recover_stale_running():
                # Persist recovery in the same lock-protected read/replace
                # transaction. Preserve the queue's original freshness: an
                # automatic crash recovery must not make a seven-day-old
                # queue eligible for execution again.
                data[str(conversation_id)] = queue.to_dict()
                self._write_all(data)
            return queue

    def save(self, queue: WeeklyQueue) -> None:
        if not str(queue.conversation_id).strip():
            raise ValueError("Weekly queue requires a conversation id.")
        if len(queue.items) > MAX_QUEUE_ITEMS:
            raise ValueError(f"Weekly queue is limited to {MAX_QUEUE_ITEMS} items.")
        queue.updated_at = time.time()
        with self._lock:
            data = self._read_all()
            data[str(queue.conversation_id)] = queue.to_dict()
            if len(data) > MAX_STORED_CONVERSATIONS:
                oldest = sorted(
                    (key for key in data if key != str(queue.conversation_id)),
                    key=lambda key: float((data.get(key) or {}).get("updated_at") or 0.0),
                )
                for key in oldest[: len(data) - MAX_STORED_CONVERSATIONS]:
                    data.pop(key, None)
            self._write_all(data)

    def clear(self, conversation_id: str) -> None:
        with self._lock:
            data = self._read_all()
            if str(conversation_id) in data:
                del data[str(conversation_id)]
                self._write_all(data)


_STORE: Optional[WeeklyQueueStore] = None
_STORE_LOCK = threading.Lock()


def get_store(root: Path) -> WeeklyQueueStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = WeeklyQueueStore(Path(root) / "data" / "weekly_readiness_queue.json")
        return _STORE
