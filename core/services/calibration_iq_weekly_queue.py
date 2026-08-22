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


@dataclass
class WeeklyQueueItem:
    repair_order_id: str
    ro_number: str = ""
    vehicle_label: str = ""
    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model_trim: Optional[str] = None
    missing_calibrations: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | complete | failed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeeklyQueueItem":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass introspection
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class WeeklyQueue:
    conversation_id: str
    items: list[WeeklyQueueItem] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def is_stale(self, *, now: Optional[float] = None, max_seconds: float = STALE_AFTER_SECONDS) -> bool:
        current = now if now is not None else time.time()
        return (current - self.updated_at) > max_seconds

    def pending(self) -> list[WeeklyQueueItem]:
        return [item for item in self.items if item.status == "pending"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "items": [item.to_dict() for item in self.items],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeeklyQueue":
        raw_items = data.get("items")
        items = [
            WeeklyQueueItem.from_dict(item)
            for item in (raw_items if isinstance(raw_items, list) else [])
            if isinstance(item, dict)
        ]
        return cls(
            conversation_id=str(data.get("conversation_id") or ""),
            items=items,
            updated_at=float(data.get("updated_at") or 0.0),
        )


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
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, conversation_id: str) -> Optional[WeeklyQueue]:
        with self._lock:
            raw = self._read_all().get(str(conversation_id))
            if not isinstance(raw, dict):
                return None
            try:
                return WeeklyQueue.from_dict(raw)
            except Exception:
                return None

    def save(self, queue: WeeklyQueue) -> None:
        queue.updated_at = time.time()
        with self._lock:
            data = self._read_all()
            data[str(queue.conversation_id)] = queue.to_dict()
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
