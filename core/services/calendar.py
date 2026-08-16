"""
X Omni -- Google Calendar.

Reads are direct. Writes are gated: create_event() is only reachable
after the capability gateway has an approved approval record, which the
operator grants from a card in chat.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from . import google_auth

log = logging.getLogger("xomni.calendar")

API = "https://www.googleapis.com/calendar/v3"


class CalendarUnavailable(RuntimeError):
    pass


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_event_datetime(value: str, field: str) -> datetime:
    """Parse an event timestamp and make local wall-clock input unambiguous.

    Google accepts an RFC3339 timestamp containing an explicit UTC offset.  We
    intentionally send that instead of Windows' display timezone names (for
    example ``Eastern Daylight Time``), which are not valid IANA identifiers.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Could not parse {field} time '{value}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz() or timezone.utc)
    return parsed


def _fmt_event(raw: dict) -> dict:
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    all_day = "date" in start
    return {
        "id": raw.get("id"),
        "title": raw.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": all_day,
        "location": raw.get("location"),
        "description": (raw.get("description") or "")[:500],
        "link": raw.get("htmlLink"),
        "status": raw.get("status"),
    }


async def _request(settings, store, method: str, path: str, **kwargs) -> dict:
    token = await google_auth.valid_access_token(settings, store)
    if not token:
        raise CalendarUnavailable(
            "Google Calendar is not connected. Sign in with Google to link it."
        )
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.request(
            method, f"{API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
    if resp.status_code == 401:
        raise CalendarUnavailable("Google rejected the stored credentials. Re-authorize.")
    if resp.status_code >= 400:
        raise CalendarUnavailable(f"Google Calendar returned HTTP {resp.status_code}.")
    return resp.json()


async def upcoming(settings, store, days: int = 7) -> dict:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, min(int(days or 7), 60)))
    data = await _request(
        settings, store, "GET", "/calendars/primary/events",
        params={
            "timeMin": now.isoformat().replace("+00:00", "Z"),
            "timeMax": end.isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 50,
        },
    )
    events = [_fmt_event(e) for e in data.get("items", [])
              if e.get("status") != "cancelled"]
    today = datetime.now(_local_tz()).date().isoformat()
    return {
        "ok": True,
        "days": days,
        "today": today,
        "events": events,
        "today_events": [e for e in events if str(e.get("start", "")).startswith(today)],
        "count": len(events),
    }


async def create_event(settings, store, args: dict) -> dict:
    title = str(args.get("title") or "").strip()
    start = str(args.get("start") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not start:
        raise ValueError("start is required")

    start_at = _parse_event_datetime(start, "start")
    end_input = str(args.get("end") or "").strip()
    end_at = (
        _parse_event_datetime(end_input, "end")
        if end_input
        else start_at + timedelta(hours=1)
    )
    if end_at <= start_at:
        raise ValueError("end must be after start")

    start_rfc3339 = start_at.isoformat()
    end_rfc3339 = end_at.isoformat()
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": start_rfc3339},
        "end": {"dateTime": end_rfc3339},
    }
    if args.get("location"):
        body["location"] = str(args["location"])
    if args.get("description"):
        body["description"] = str(args["description"])

    created = await _request(settings, store, "POST",
                             "/calendars/primary/events", json=body)
    store.audit("calendar_event_created",
                {"id": created.get("id"), "title": title, "start": start_rfc3339})
    return {"ok": True, "created": True, "event": _fmt_event(created)}


async def status(settings, store) -> dict:
    row = store.get_google_token()
    if not row or not row.get("access_token"):
        return {"connected": False, "reason": "not_authorized"}
    try:
        token = await google_auth.valid_access_token(settings, store)
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "reason": str(exc)}
    return {
        "connected": bool(token),
        "account": row.get("account_email"),
        "scope": row.get("scope"),
    }
