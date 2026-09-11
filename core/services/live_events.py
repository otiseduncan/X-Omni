"""In-process fan-out of small Core events to a user's connected chat sockets.

Background work (for example the ADAS Map sweep) finishes outside any chat
turn. When it adds a message to a conversation, it publishes one
``conversation_updated`` event here; each connected socket for that user
forwards it, and the UI re-reads the conversation. A phone that is not
connected gets the Web Push notification instead and reconciles on reconnect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional

log = logging.getLogger("xomni.live_events")

Subscriber = Callable[[dict[str, Any]], None]


class LiveEvents:
    def __init__(self) -> None:
        self._subscribers: list[tuple[Optional[str], Subscriber]] = []

    def subscribe(self, user_id: Optional[str], callback: Subscriber) -> Callable[[], None]:
        """Register one socket. Returns the matching unsubscribe function."""

        entry = (str(user_id) if user_id else None, callback)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(entry)
            except ValueError:
                pass

        return unsubscribe

    def publish(self, event: dict[str, Any], *, user_id: Optional[str]) -> int:
        """Deliver ``event`` to every socket of ``user_id``. Returns the count."""

        target = str(user_id) if user_id else None
        delivered = 0
        for owner, callback in list(self._subscribers):
            if target is not None and owner != target:
                continue
            try:
                callback(dict(event))
                delivered += 1
            except Exception:  # noqa: BLE001 - one dead socket must not stop the rest
                log.warning("live event delivery failed", exc_info=True)
        return delivered
