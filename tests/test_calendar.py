from __future__ import annotations

from datetime import datetime

import pytest

from core.services import calendar


class AuditStore:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def audit(self, action: str, details: dict) -> None:
        self.entries.append((action, details))


@pytest.mark.asyncio
async def test_create_event_uses_portable_offset_not_windows_timezone(monkeypatch):
    captured: dict = {}

    async def fake_request(_settings, _store, method, path, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        body = kwargs["json"]
        return {
            "id": "evt-1",
            "summary": body["summary"],
            "start": body["start"],
            "end": body["end"],
            "status": "confirmed",
        }

    monkeypatch.setattr(calendar, "_request", fake_request)
    store = AuditStore()
    result = await calendar.create_event(
        object(), store, {"title": "Road test", "start": "2026-08-17T09:00:00"}
    )

    body = captured["json"]
    assert captured["method"] == "POST"
    assert captured["path"] == "/calendars/primary/events"
    assert "timeZone" not in body["start"]
    assert "timeZone" not in body["end"]
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert start.utcoffset() is not None
    assert end - start == calendar.timedelta(hours=1)
    assert result["created"] is True
    assert store.entries[0][0] == "calendar_event_created"


@pytest.mark.asyncio
async def test_create_event_rejects_non_forward_interval(monkeypatch):
    async def should_not_request(*_args, **_kwargs):
        raise AssertionError("Google must not be called for an invalid interval")

    monkeypatch.setattr(calendar, "_request", should_not_request)
    with pytest.raises(ValueError, match="end must be after start"):
        await calendar.create_event(
            object(),
            AuditStore(),
            {
                "title": "Invalid",
                "start": "2026-08-17T10:00:00-04:00",
                "end": "2026-08-17T09:00:00-04:00",
            },
        )
