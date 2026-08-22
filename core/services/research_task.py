"""Persistent research-task continuity, replacing per-phrase follow-up matchers.

Previously, "check ALLDATA for it" and "show me the exact procedure" each
needed their own hand-enumerated regex of phrasings (research_followup.py,
research_procedure_followup.py) because nothing remembered what "it" was.
That doesn't scale -- every new way a technician phrases the same follow-up
needs another pattern.

Instead, a ResearchTask records the vehicle/subject/goal/status of the most
recent research request per conversation. A follow-up is recognized generically:
if the new message doesn't name its own (different) vehicle, the active task's
vehicle/subject is merged in ahead of it, regardless of exact phrasing. Vehicle
identity is a hard, parseable fact (research_alldata_navigation.vehicle_from_query
already extracts it); conversational phrasing is not something worth enumerating.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# A task is considered stale, and no longer eligible for silent follow-up
# merging, once this many user turns have passed without an update.
STALE_AFTER_TURNS = 6


@dataclass
class ResearchTask:
    conversation_id: str
    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model_trim: Optional[str] = None
    vehicle_label: Optional[str] = None
    subject: str = ""
    goal: str = "Obtain the exact OEM procedure."
    local_status: str = "unknown"  # missing | found | unknown
    alldata_status: str = "not_started"  # not_started | vehicle_selection_required | searched_unverified | verified
    alldata_evidence_url: Optional[str] = None
    oem_web_status: str = "not_started"
    oem_web_evidence_url: Optional[str] = None
    acquisition_status: str = "pending"  # pending | captured
    updated_at: float = field(default_factory=time.time)
    turn_count_at_update: int = 0

    def is_stale(self, current_turn_count: int, *, max_turns: int = STALE_AFTER_TURNS) -> bool:
        return (current_turn_count - self.turn_count_at_update) > max_turns

    def subject_line(self) -> str:
        parts = [part for part in (self.vehicle_label, self.subject) if part]
        return " ".join(parts).strip() or self.subject or "the prior research request"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchTask":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass introspection
        return cls(**{k: v for k, v in data.items() if k in known})


class ResearchTaskStore:
    """Thread-safe, JSON-backed per-conversation task store.

    Mirrors the singleton pattern already used for the licensed browser
    (research_operator.get_browser) -- one small file, one lock, no new
    infrastructure dependency.
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

    def get(self, conversation_id: str) -> Optional[ResearchTask]:
        with self._lock:
            raw = self._read_all().get(str(conversation_id))
            if not isinstance(raw, dict):
                return None
            try:
                return ResearchTask.from_dict(raw)
            except Exception:
                return None

    def save(self, task: ResearchTask) -> None:
        with self._lock:
            data = self._read_all()
            data[str(task.conversation_id)] = task.to_dict()
            self._write_all(data)

    def clear(self, conversation_id: str) -> None:
        with self._lock:
            data = self._read_all()
            if str(conversation_id) in data:
                del data[str(conversation_id)]
                self._write_all(data)


_STORE: Optional[ResearchTaskStore] = None
_STORE_LOCK = threading.Lock()


def get_store(root: Path) -> ResearchTaskStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ResearchTaskStore(Path(root) / "data" / "research_tasks.json")
        return _STORE
