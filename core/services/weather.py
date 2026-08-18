"""
X Omni -- weather (Open-Meteo).

No API key, no OAuth, no account. Location is saved once (place name,
US ZIP, or lat/lon) and the forecast is cached so a flaky connection
degrades to stale-but-honest rather than an empty card.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

SETTINGS_NS = "weather"
LOCATION_ID = "location"
CACHE_ID = "forecast_cache"
CACHE_FRESH_HOURS = 6
US_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy",
    45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _condition(code: Any) -> str:
    try:
        return CODES.get(int(code), "unsettled")
    except (TypeError, ValueError):
        return "unsettled"


def _wind_label(mph: Any) -> str:
    try:
        v = float(mph)
    except (TypeError, ValueError):
        return "unknown"
    return "light" if v < 8 else "breezy" if v < 18 else "windy"


def resolve_zip(zip_code: str) -> dict:
    m = US_ZIP.search(zip_code)
    if not m:
        raise ValueError("A five-digit US ZIP code is required.")
    z = m.group(1)
    with httpx.Client(timeout=12) as c:
        r = c.get(f"https://api.zippopotam.us/us/{z}")
        if r.status_code == 404:
            raise ValueError(f"Could not find ZIP {z}.")
        r.raise_for_status()
    places = r.json().get("places") or []
    if not places:
        raise ValueError(f"Could not find ZIP {z}.")
    p = places[0]
    return {
        "name": f"{p.get('place name')}, {p.get('state abbreviation', '')} {z}".strip(),
        "latitude": float(p["latitude"]), "longitude": float(p["longitude"]),
    }


def geocode(name: str) -> dict:
    with httpx.Client(timeout=12) as c:
        r = c.get("https://geocoding-api.open-meteo.com/v1/search",
                  params={"name": name, "count": 1, "language": "en", "format": "json"})
        r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise ValueError(f"Could not find location '{name}'.")
    f = results[0]
    parts = [f.get("name"), f.get("admin1"), f.get("country_code")]
    return {
        "name": ", ".join(p for p in parts if p),
        "latitude": float(f["latitude"]), "longitude": float(f["longitude"]),
    }


def save_location(store, payload: dict, *, user_id: str = "local-dev") -> dict:
    name = str(payload.get("name") or "").strip()
    lat, lon = payload.get("latitude"), payload.get("longitude")
    if lat is not None and lon is not None:
        loc = {"name": name or "Saved location",
               "latitude": float(lat), "longitude": float(lon)}
    elif payload.get("zip"):
        loc = resolve_zip(str(payload["zip"]))
    elif name and US_ZIP.fullmatch(name.strip()):
        loc = resolve_zip(name)
    elif name:
        loc = geocode(name)
    else:
        raise ValueError("Provide a place name, a US ZIP, or latitude/longitude.")
    store.put_record(SETTINGS_NS, LOCATION_ID, loc, user_id=user_id)
    store.put_record(SETTINGS_NS, CACHE_ID, {}, user_id=user_id)  # invalidate stale cache
    return loc


def get_location(store, *, user_id: str = "local-dev") -> Optional[dict]:
    return store.get_record(SETTINGS_NS, LOCATION_ID, user_id=user_id)


def _rows(payload: dict) -> list[dict]:
    daily = payload.get("daily") or {}

    def at(key: str, i: int):
        vals = daily.get(key) or []
        return vals[i] if i < len(vals) else None

    out = []
    for i, date in enumerate(daily.get("time") or []):
        out.append({
            "date": date,
            "high_f": at("temperature_2m_max", i),
            "low_f": at("temperature_2m_min", i),
            "rain_chance": at("precipitation_probability_max", i),
            "wind_mph": at("wind_speed_10m_max", i),
            "wind": _wind_label(at("wind_speed_10m_max", i)),
            "condition": _condition(at("weather_code", i)),
        })
    return out


def summarize(day: dict, location: Optional[dict]) -> str:
    name = (location or {}).get("name", "Local")
    hi, lo = day.get("high_f"), day.get("low_f")
    temp = f"{round(hi)}/{round(lo)}F" if hi is not None and lo is not None else "--"
    rain = day.get("rain_chance")
    rain_txt = f", {round(rain)}% rain" if rain is not None else ""
    return f"{name}: {temp}, {day.get('condition', 'unsettled')}{rain_txt}."


def fetch(store, *, user_id: str = "local-dev") -> dict:
    location = get_location(store, user_id=user_id)
    if not location:
        return {
            "ok": False, "status": "not_configured",
            "summary": "No weather location set yet.",
            "location": None, "current": None, "forecast": [],
            "next_step": "Tell X Omni your city or ZIP code.",
        }
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0)) as c:
            r = c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": location["latitude"], "longitude": location["longitude"],
                "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m,apparent_temperature",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code,wind_speed_10m_max",
                "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                "precipitation_unit": "inch", "timezone": "auto", "forecast_days": 7,
            })
            r.raise_for_status()
        payload = r.json()
        cur = payload.get("current") or {}
        current = {
            "temperature_f": cur.get("temperature_2m"),
            "feels_like_f": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "condition": _condition(cur.get("weather_code")),
            "wind_mph": cur.get("wind_speed_10m"),
            "wind": _wind_label(cur.get("wind_speed_10m")),
        }
        rows = _rows(payload)
        result = {
            "ok": True, "status": "live", "location": location,
            "current": current, "forecast": rows,
            "summary": summarize(rows[0] if rows else current, location),
            "updated_at": _now(),
        }
        store.put_record(SETTINGS_NS, CACHE_ID, result, user_id=user_id)
        return result
    except Exception as exc:  # noqa: BLE001
        cached = store.get_record(SETTINGS_NS, CACHE_ID, user_id=user_id) or {}
        if cached.get("forecast"):
            cached = dict(cached)
            cached["status"] = "cached"
            cached["summary"] = f"{cached.get('summary', '')} (cached; live refresh failed)"
            return cached
        return {
            "ok": False, "status": "failed", "location": location,
            "current": None, "forecast": [],
            "summary": f"Weather lookup failed: {exc}",
        }
