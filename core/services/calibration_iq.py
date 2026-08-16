"""
X Omni -- Calibration IQ.

Full read/write access to the field repair-order system, over its
versioned tool API. Ported from XV12's adapter with its safety rails
kept intact, because those rails are what make write access safe rather
than what blocks it:

  * idempotency key on every mutation -- a replayed request is absorbed
    by the service instead of applying twice
  * expected_version optimistic concurrency -- a stale edit is rejected
    with 409 rather than silently clobbering someone else's change
  * operation allow-list -- unknown operations never reach the wire
  * receipt verification -- success is only claimed when the service
    confirms the mutation completed

Three XV12 bugs are deliberately not carried over; see the comments on
_service_token, read_repair_orders, and _merge_counts.

The service token is read from the Calibration IQ project's own .env and
never enters model context, the database, or the audit log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("xomni.calibration_iq")

MUTATION_OPERATIONS = {
    "change_status": "Change a repair order's status",
    "update_ro": "Update repair order fields",
    "update_blocker": "Add, update, or clear a blocker",
    "update_requirement": "Update a calibration requirement",
}

READ_PARAMS = ("q", "shop", "insurance", "status", "phase", "limit", "offset")

# Terminal statuses proved by the current Calibration IQ contract.  Match the
# normalized label exactly: substring rules make active values such as
# "Calibration Incomplete" look finished and silently corrupt every count.
# Add another value only after Calibration IQ itself proves that exact status is
# terminal.
TERMINAL_STATUSES = frozenset({
    "calibration complete",
    "no calibration required",
})

READ_TIMEOUT = 20.0
MUTATE_TIMEOUT = 25.0
HEALTH_TIMEOUT = 4.0
MAX_ITEMS = 20

# Server-side paging bounds. The whole board is ~200 records, so one question
# can collect every match instead of the model manually walking offsets and
# emitting a card per batch.
PAGE_SIZE = 100
MAX_COLLECT = 400
MAX_PAGE_REQUESTS = 100


class CalibrationIQUnavailable(RuntimeError):
    pass


def _unquote(value: str) -> str:
    """XV12's env parser left quotes in the value, which silently produced a
    malformed Authorization header. Strip a single matched pair."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
        return v[1:-1]
    return v


# Candidate names for the service token, in priority order. Different
# Calibration IQ revisions have spelled it differently, and reporting
# "missing" when the value is present under another name wastes real time.
TOKEN_KEYS = (
    "TOOL_SERVICE_TOKEN",
    "TOOLS_SERVICE_TOKEN",
    "TOOL_API_TOKEN",
    "SERVICE_TOKEN",
    "API_TOKEN",
    "XV12_CALIBRATION_IQ_ACCESS_TOKEN",
    "CALIBRATION_IQ_ACCESS_TOKEN",
)


def _read_env_file(project_path: Path) -> dict[str, str]:
    env_path = Path(project_path) / ".env"
    if not env_path.is_file():
        return {}
    try:
        raw = env_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("Could not read Calibration IQ .env: %s", type(exc).__name__)
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = _unquote(value)
    return values


def _service_token(project_path: Path) -> str:
    """Find the service token in <project>/.env.

    An explicit XOMNI_CALIBRATION_IQ_TOKEN in X Omni's own environment wins,
    so the token can be supplied without touching the Calibration IQ project.
    Returns "" when absent. The value is never logged.
    """
    override = os.getenv("XOMNI_CALIBRATION_IQ_TOKEN", "").strip()
    if override:
        return _unquote(override)
    values = _read_env_file(project_path)
    for key in TOKEN_KEYS:
        if values.get(key):
            return values[key]
    return ""


def token_key_names(project_path: Path) -> list[str]:
    """Key names present in the project .env -- names only, never values.
    Lets the status card say 'the file is there but the token is called
    something else' instead of a bare 'missing'."""
    return sorted(_read_env_file(project_path).keys())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------

async def health(settings) -> dict[str, Any]:
    """A 401 still proves the service is up -- it just means our token was
    not accepted, which is a credential problem, not an outage."""
    base = await resolve_base(settings)
    if not base:
        return {"status": "not_configured", "configured": False}
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT, trust_env=False) as client:
            resp = await client.get(f"{base}/health")
    except httpx.HTTPError as exc:
        return {"status": "offline", "configured": True,
                "error": type(exc).__name__, "base_url": base}
    reachable = resp.status_code in {200, 401}
    return {
        "status": "available" if reachable else "degraded",
        "configured": True,
        "http_status": resp.status_code,
        "token_present": bool(_service_token(settings.calibration_iq_project_path)),
        "base_url": base,
    }


def _candidate_bases(base: str) -> list[str]:
    """Alternate spellings of the same address to probe when the configured
    one refuses a connection.

    On Windows `localhost` usually resolves to ::1 first. A service bound
    only to the IPv6 loopback answers a browser hitting localhost while
    refusing 127.0.0.1 outright -- which looks exactly like "the service is
    down" from here, even though it is plainly running.
    """
    out = [base]
    for a, b in (("127.0.0.1", "localhost"), ("localhost", "127.0.0.1"),
                 ("127.0.0.1", "[::1]"), ("localhost", "[::1]")):
        if a in base:
            alt = base.replace(a, b)
            if alt not in out:
                out.append(alt)
    return out


# Once a spelling of the address is proven to work, remember it. Caddy here
# binds 127.0.0.1 only, so `localhost` -- which Windows resolves to ::1 first
# -- is refused outright even though the service is plainly running.
_RESOLVED_BASE: dict[str, str] = {}


async def resolve_base(settings) -> str:
    """Return a base URL that actually answers, preferring the configured one."""
    configured = settings.calibration_iq_base_url
    cached = _RESOLVED_BASE.get(configured)
    if cached:
        return cached
    for candidate in _candidate_bases(configured):
        try:
            async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                resp = await client.get(f"{candidate}/health")
            if resp.status_code in {200, 401}:
                if candidate != configured:
                    log.warning(
                        "Calibration IQ answered at %s, not the configured %s -- using it.",
                        candidate, configured,
                    )
                _RESOLVED_BASE[configured] = candidate
                return candidate
        except httpx.HTTPError:
            continue
    return configured


async def probe(settings) -> dict[str, Any]:
    """Find out what is actually answering, and where.

    Reports each candidate address and the bare service root separately, so
    'the port is open but the tool API path is wrong' is distinguishable
    from 'nothing is listening'.
    """
    base = settings.calibration_iq_base_url
    findings = []
    for candidate in _candidate_bases(base):
        for suffix, label in ((("/health"), "tool-api"), ("", "service-root")):
            url = f"{candidate}{suffix}" if suffix else candidate.split("/api/")[0]
            try:
                async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
                    resp = await client.get(url)
                findings.append({"url": url, "kind": label, "reachable": True,
                                 "http_status": resp.status_code})
            except httpx.HTTPError as exc:
                findings.append({"url": url, "kind": label, "reachable": False,
                                 "error": type(exc).__name__})
    working = [f for f in findings if f["reachable"]]
    return {"configured_base": base, "findings": findings,
            "any_reachable": bool(working)}


async def status(settings, _args: dict | None = None) -> dict[str, Any]:
    """Tool-facing status: is Calibration IQ reachable and authorized."""
    result = await health(settings)
    token_ok = result.get("token_present")

    if result["status"] == "available" and not token_ok:
        env_path = Path(settings.calibration_iq_project_path) / ".env"
        keys = token_key_names(settings.calibration_iq_project_path)
        result["status"] = "unauthorized"
        result["env_path"] = str(env_path)
        # Names only, never values -- but knowing the file exists and what it
        # calls things is the difference between a two-minute fix and an hour.
        result["env_keys_present"] = keys
        if not env_path.is_file():
            result["message"] = (
                f"Calibration IQ is running, but no .env was found at {env_path}. "
                "Either point XOMNI_CALIBRATION_IQ_PROJECT_PATH at the project, or set "
                "XOMNI_CALIBRATION_IQ_TOKEN directly in X Omni's config/.env.local."
            )
        elif keys:
            result["message"] = (
                f"Calibration IQ is running and {env_path} exists, but none of the "
                f"expected token names are set. Keys present: {', '.join(keys)}. "
                f"Expected one of: {', '.join(TOKEN_KEYS[:4])}. "
                "Set XOMNI_CALIBRATION_IQ_TOKEN in X Omni's config/.env.local to the "
                "right value, or rename the key."
            )
        else:
            result["message"] = (
                f"Calibration IQ is running but {env_path} has no readable keys."
            )
        return result

    if result["status"] != "offline":
        return result

    # Offline is the answer most likely to be wrong, so dig before reporting it.
    detail = await probe(settings)
    result["probe"] = detail
    reachable = [f for f in detail["findings"] if f["reachable"]]
    if reachable:
        alt = next((f for f in reachable if f["kind"] == "tool-api"), reachable[0])
        result["status"] = "misconfigured"
        result["message"] = (
            f"Calibration IQ is running, but not at the configured address. "
            f"{alt['url']} answered with HTTP {alt.get('http_status')}. "
            f"Set XOMNI_CALIBRATION_IQ_BASE_URL in config/.env.local to match."
        )
        result["suggested_base_url"] = alt["url"].removesuffix("/health")
    else:
        result["message"] = (
            f"Nothing answered at {detail['configured_base']} on any loopback "
            "spelling (127.0.0.1, localhost, ::1). If the web UI is up on that "
            "port, the tool API is probably served under a different path."
        )
    return result


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def _dig(item: dict, *paths, default=None):
    """Pull a value that different Calibration IQ revisions nest differently.

    The vehicle column came back blank because year/make/model are not always
    top-level -- they can sit under `vehicle`, or arrive pre-joined as a
    description string.
    """
    for path in paths:
        cursor: Any = item
        for part in path.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
            if cursor is None:
                break
        if cursor not in (None, "", [], {}):
            return cursor
    return default


def _vehicle_label(item: dict) -> str:
    pre = _dig(item, "vehicle_description", "vehicle_display", "vehicle.description",
               "vehicle_name", "description")
    if isinstance(pre, str) and pre.strip():
        return pre.strip()
    parts = [
        _dig(item, "year", "vehicle.year", "vehicle_year"),
        _dig(item, "make", "vehicle.make", "vehicle_make"),
        _dig(item, "model", "vehicle.model", "vehicle_model"),
        _dig(item, "trim", "vehicle.trim", "vehicle_trim"),
    ]
    label = " ".join(str(p) for p in parts if p not in (None, ""))
    if label:
        return label
    vin = _dig(item, "vin", "vehicle.vin", "vehicle_vin")
    return f"VIN {str(vin)[-8:]}" if vin else "-"


def _shop_label(item: dict) -> str:
    shop = _dig(item, "shop.name", "shop_name", "shop", "location.name", "location")
    if isinstance(shop, dict):
        shop = shop.get("name")
    return str(shop) if shop else "-"


def _status_of(item: dict) -> str:
    return str(_dig(item, "display_status", "status_display", "status", default="")).strip()


def _normalized_label(value: Any) -> str:
    """Normalize a categorical label without weakening it to a substring."""
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def is_terminal(item: dict) -> bool:
    """True when the repair order is finished and no longer active work."""
    return _normalized_label(_status_of(item)) in TERMINAL_STATUSES


def _phase_of(item: dict) -> Any:
    """Return a display-safe scalar phase across known response shapes."""
    phase = _dig(
        item,
        "phase_name",
        "phase.name",
        "phase.number",
        "phase.value",
        "phase",
    )
    if isinstance(phase, dict):
        phase = _dig(
            phase,
            "name",
            "number",
            "value",
            "phase",
            "id",
        )
    if phase in (None, "", [], {}):
        return None
    if isinstance(phase, (str, int, float)):
        return phase
    return str(phase)


def _record_identity(item: dict) -> str:
    """Stable identity for de-duplicating records repeated across pages."""
    # The repair-order number is the stable business identity even when one
    # response revision omits/renames its internal database id.
    for path in ("ro_number", "roNumber", "number", "ro"):
        value = _dig(item, path)
        if value not in (None, ""):
            return f"ro:{str(value).strip().casefold()}"
    for path in ("id", "repair_order_id", "uuid"):
        value = _dig(item, path)
        if value not in (None, ""):
            return f"id:{str(value).strip().casefold()}"
    encoded = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return f"row:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _breakdown(items: list[dict]) -> dict[str, Any]:
    """Counts by status, phase and shop. This is what a spoken answer needs --
    'fifteen, nine new arrival, five waiting' rather than fifteen rows."""
    def tally(fn):
        out: dict[str, int] = {}
        for item in items:
            key = fn(item) or "unspecified"
            out[str(key)] = out.get(str(key), 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "by_status": tally(_status_of),
        "by_phase": tally(_phase_of),
        "by_shop": tally(_shop_label),
    }


def _ro_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        rows.append({
            "RO": _dig(item, "ro_number", "roNumber", "number", "ro", default="-"),
            "Vehicle": _vehicle_label(item),
            "Status": _dig(item, "display_status", "status_display", "status", default="-"),
            "Shop": _shop_label(item),
            "Phase": _phase_of(item),
            "id": _dig(item, "id", "repair_order_id", "uuid"),
        })
    return rows


async def _collect(
    base: str,
    token: str,
    params: dict,
) -> tuple[list[dict], dict[str, Any], Optional[dict]]:
    """Page through the collection until every match is gathered.

    The model used to walk offsets itself, which cost three tool calls and
    emitted three separate cards for one question. One question should be
    one call.

    Offset advances by the raw batch length because the upstream service may
    cap a requested 100-row page at 20. Records are de-duplicated separately,
    so an overlapping page cannot inflate the result. A result is complete
    only when the unique collection reaches the upstream total (or an empty
    response proves the end when no total is supplied).

    Returns (items, collection_metadata, error_result). error_result is None
    when transport and response parsing succeeded; metadata still distinguishes
    a complete collection from a safety-cap/early-end partial collection.
    """
    collected: list[dict] = []
    seen: set[str] = set()
    offset = int(params.get("offset") or 0)
    upstream_total: Optional[int] = None
    raw_fetched = 0
    duplicate_count = 0
    pages_fetched = 0
    completion_reason = "not_started"
    complete = False

    def metadata() -> dict[str, Any]:
        return {
            "complete": complete,
            "completion_reason": completion_reason,
            "upstream_total": upstream_total,
            "raw_fetched_count": raw_fetched,
            "unique_count": len(collected),
            "duplicate_count": duplicate_count,
            "pages_fetched": pages_fetched,
            "collection_capped": completion_reason in {"item_cap", "page_cap"},
        }

    async with httpx.AsyncClient(timeout=READ_TIMEOUT, trust_env=False) as client:
        while pages_fetched < MAX_PAGE_REQUESTS:
            if len(collected) >= MAX_COLLECT:
                completion_reason = "item_cap"
                break

            remaining = MAX_COLLECT - len(collected)
            page = {**params, "limit": min(PAGE_SIZE, remaining), "offset": offset}
            try:
                resp = await client.get(f"{base}/collection/ros", params=page,
                                        headers=_auth(token))
            except httpx.HTTPError as exc:
                completion_reason = "transport_error"
                return collected, metadata(), {
                    "status": "offline",
                    "error": type(exc).__name__,
                    "message": f"Calibration IQ is not reachable at {base}.",
                }

            if resp.status_code == 401:
                completion_reason = "authentication_failed"
                return collected, metadata(), {
                    "status": "authentication_failed",
                    "message": "Calibration IQ rejected the service token.",
                }
            if resp.status_code == 422:
                try:
                    detail = resp.json().get("detail")
                except ValueError:
                    detail = resp.text[:400]
                completion_reason = "invalid_filter"
                return collected, metadata(), {
                    "status": "invalid_filter", "http_status": 422,
                    "detail": detail, "filters": params,
                    "message": (
                        "Calibration IQ rejected those filter values (HTTP 422). "
                        f"It objected to: {detail}. Valid filters are "
                        f"{', '.join(READ_PARAMS)} -- drop the unsupported one and retry."
                    )}
            if resp.status_code >= 400:
                completion_reason = "upstream_error"
                return collected, metadata(), {
                    "status": "error",
                    "http_status": resp.status_code,
                    "message": f"Calibration IQ returned HTTP {resp.status_code}.",
                }
            try:
                body = resp.json()
            except ValueError:
                completion_reason = "invalid_json"
                return collected, metadata(), {
                    "status": "error",
                    "message": "Calibration IQ returned a non-JSON response.",
                }
            if not isinstance(body, dict):
                completion_reason = "invalid_response"
                return collected, metadata(), {
                    "status": "error",
                    "message": "Calibration IQ returned an invalid collection response.",
                }

            raw_items = body.get("items") or []
            if not isinstance(raw_items, list) or any(
                not isinstance(item, dict) for item in raw_items
            ):
                completion_reason = "invalid_response"
                return collected, metadata(), {
                    "status": "error",
                    "message": "Calibration IQ returned malformed repair-order rows.",
                }

            reported_total = body.get("count")
            if reported_total not in (None, ""):
                try:
                    parsed_total = int(reported_total)
                except (TypeError, ValueError):
                    completion_reason = "invalid_response"
                    return collected, metadata(), {
                        "status": "error",
                        "message": "Calibration IQ returned an invalid total count.",
                    }
                if parsed_total < 0:
                    completion_reason = "invalid_response"
                    return collected, metadata(), {
                        "status": "error",
                        "message": "Calibration IQ returned an invalid total count.",
                    }
                upstream_total = max(upstream_total or 0, parsed_total)

            batch = list(raw_items)
            if upstream_total == 0 and batch:
                completion_reason = "inconsistent_total"
                return collected, metadata(), {
                    "status": "error",
                    "message": (
                        "Calibration IQ returned rows with an authoritative total of zero."
                    ),
                }
            pages_fetched += 1
            raw_fetched += len(batch)
            hit_item_cap = False
            for item in batch:
                identity = _record_identity(item)
                if identity in seen:
                    duplicate_count += 1
                    continue
                if len(collected) >= MAX_COLLECT:
                    hit_item_cap = True
                    break
                seen.add(identity)
                collected.append(item)

            offset += len(batch)

            if upstream_total is not None and len(collected) >= upstream_total:
                complete = True
                completion_reason = "upstream_total_reached"
                break
            if hit_item_cap or len(collected) >= MAX_COLLECT:
                completion_reason = "item_cap"
                break
            if not batch:
                complete = upstream_total in (None, 0) or len(collected) >= upstream_total
                completion_reason = "empty_page" if complete else "early_empty_page"
                break
        else:
            completion_reason = "page_cap"

    return collected, metadata(), None


async def query_repair_orders(settings, args: dict) -> dict[str, Any]:
    """Shared query path for both the list and the summary tools.

    Excludes finished work unless explicitly asked. 'Active' has to mean
    active: Calibration Complete and No Calibration Required are terminal,
    and counting them made every number Otis heard too high.
    """
    base = await resolve_base(settings)
    token = _service_token(settings.calibration_iq_project_path)
    if not token:
        return {"status": "not_configured", "items": [], "rows": [], "count": 0,
                "message": "Calibration IQ service token is not configured."}

    terminal_only = bool(args.get("terminal_only"))
    include_completed = bool(args.get("include_completed")) or terminal_only
    params = {k: args[k] for k in READ_PARAMS
              if k not in {"limit", "offset"} and args.get(k) not in (None, "")}

    collected, collection, error = await _collect(base, token, params)
    if error:
        return {
            **error,
            "items": [],
            "rows": [],
            "count": None,
            "filters": error.get("filters", params),
            "collection": collection,
            "collection_complete": False,
            "collection_capped": collection["collection_capped"],
        }

    completed = [i for i in collected if is_terminal(i)]
    active = [i for i in collected if not is_terminal(i)]
    matched = completed if terminal_only else (collected if include_completed else active)
    scope = (
        "terminal work only"
        if terminal_only
        else ("all repair orders" if include_completed else "active work only")
    )

    if not collection["complete"]:
        return {
            "status": "incomplete",
            "items": [],
            "rows": [],
            "count": None,
            "partial_count": len(matched),
            "partial_active_count": len(active),
            "partial_completed_count": len(completed),
            "include_completed": include_completed,
            "terminal_only": terminal_only,
            "scope": scope,
            "filters": params,
            "collection": collection,
            "collection_complete": False,
            "collection_capped": collection["collection_capped"],
            "message": (
                "Calibration IQ did not provide the complete matching collection "
                f"({collection['completion_reason']}). A partial count was not reported "
                "as the total."
            ),
            "evidence": {
                "source": "calibration_iq_authenticated_api",
                "read_only": True,
            },
        }

    return {
        "status": "verified",
        "items": matched,
        "count": len(matched),
        "active_count": len(active),
        "completed_count": len(completed),
        "include_completed": include_completed,
        "terminal_only": terminal_only,
        "scope": scope,
        "filters": params,
        "breakdown": _breakdown(matched),
        "collection": collection,
        "collection_complete": True,
        "collection_capped": False,
        "upstream_total": collection["upstream_total"],
        "duplicate_count": collection["duplicate_count"],
        "evidence": {"source": "calibration_iq_authenticated_api", "read_only": True},
    }


async def summarize_repair_orders(settings, args: dict) -> dict[str, Any]:
    """Counts only -- no rows.

    This is the default shape for "how many" questions. Otis is usually
    listening over Bluetooth in the field, not looking at a screen, so the
    answer needs to be a number and a short breakdown he can hear.
    """
    result = await query_repair_orders(settings, args)
    if result.get("status") != "verified":
        return result
    result.pop("items", None)
    result["rows"] = []
    result["summary_only"] = True
    return result


async def read_repair_orders(settings, args: dict) -> dict[str, Any]:
    """The list. Returns counts plus a bounded set of rows to display."""
    result = await query_repair_orders(settings, args)
    if result.get("status") != "verified":
        return result

    items = result.pop("items", [])
    limit = min(max(int(args.get("limit") or MAX_ITEMS), 1), 100)
    shown = items[:limit]
    result["rows"] = _ro_rows(shown)
    result["shown_count"] = len(shown)
    # Count and shown_count are deliberately distinct so a capped list can
    # never be mistaken for the whole answer.
    result["truncated"] = len(items) > len(shown)
    return result


async def get_repair_order(settings, args: dict) -> dict[str, Any]:
    """Pull one repair order up in full.

    Tries the detail endpoint first, then falls back to a collection search
    on the RO number -- different revisions expose one or the other, and a
    technician asking for "RO 2400911724" should not care which.
    """
    base = await resolve_base(settings)
    token = _service_token(settings.calibration_iq_project_path)
    if not token:
        return {"status": "not_configured", "repair_order": None,
                "message": "Calibration IQ service token is not configured."}

    ident = str(args.get("repair_order_id") or args.get("ro_number") or "").strip()
    if not ident:
        raise ValueError("repair_order_id (or ro_number) is required")

    item: Optional[dict] = None
    tried: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT, trust_env=False) as client:
            url = f"{base}/ros/{ident}"
            tried.append(url)
            resp = await client.get(url, headers=_auth(token))
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    item = body.get("item") or body.get("repair_order") or body
                except ValueError:
                    item = None

            if item is None:
                # Fall back to searching the board for that RO number.
                url = f"{base}/collection/ros"
                tried.append(f"{url}?q={ident}")
                resp = await client.get(url, params={"q": ident, "limit": 5},
                                        headers=_auth(token))
                if resp.status_code == 200:
                    try:
                        found = list(resp.json().get("items") or [])
                    except ValueError:
                        found = []
                    exact = [
                        f for f in found
                        if str(_dig(f, "ro_number", "roNumber", "number", default="")).strip() == ident
                    ]
                    item = (exact or found or [None])[0]
    except httpx.HTTPError as exc:
        return {"status": "offline", "repair_order": None, "error": type(exc).__name__,
                "message": f"Calibration IQ is not reachable at {base}."}

    if not item:
        return {"status": "no_result", "repair_order": None, "query": ident,
                "tried": tried,
                "message": f"No repair order matched '{ident}'."}

    summary = _ro_rows([item])[0]
    return {
        "status": "verified",
        "repair_order": {
            **summary,
            "insurance": _dig(item, "insurance.name", "insurance_name", "insurance"),
            "vin": _dig(item, "vin", "vehicle.vin", "vehicle_vin"),
            "arrival": _dig(item, "arrival_date", "arrival", "arrived_at"),
            "created": _dig(item, "created_at", "created"),
            "updated": _dig(item, "updated_at", "last_modified", "modified_at"),
            "version": _dig(item, "version", "revision", "_version"),
            "blockers": _dig(item, "blockers", "blocking", default=[]),
            "requirements": _dig(item, "calibration_requirements", "requirements", default=[]),
            "notes": _dig(item, "notes", "note"),
        },
        # Everything the service returned, so nothing is hidden from the
        # detail card just because this mapper didn't anticipate a field.
        "raw": item,
        "evidence": {"source": "calibration_iq_authenticated_api", "read_only": True},
    }


# --------------------------------------------------------------------------
# write / mutate
# --------------------------------------------------------------------------

def mutation_summary(args: dict) -> str:
    """Human-readable line for the approval card."""
    op = str(args.get("operation") or "?")
    ro = str(args.get("repair_order_id") or "?")
    inner = args.get("arguments") or {}
    detail = ", ".join(f"{k}={v}" for k, v in list(inner.items())[:4]) if inner else ""
    label = MUTATION_OPERATIONS.get(op, op)
    return f"Calibration IQ — {label} on RO {ro}{f' ({detail})' if detail else ''}"


async def mutate(settings, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
    """POST /ros/{id}/mutations.

    The idempotency key is the replay guard: if the same key arrives twice,
    Calibration IQ returns the original result with duplicate=true rather
    than applying the change again. One is generated when the caller does
    not supply one, so a retry at any layer above is still safe.
    """
    base = await resolve_base(settings)
    token = _service_token(settings.calibration_iq_project_path)
    if not token:
        return {"status": "not_configured", "executed": False,
                "message": "Calibration IQ service token is not configured."}

    ro_id = str(args.get("repair_order_id") or "").strip()
    if not ro_id:
        raise ValueError("repair_order_id is required")

    operation = str(args.get("operation") or "").strip()
    if operation not in MUTATION_OPERATIONS:
        raise ValueError(
            f"Unsupported operation '{operation}'. "
            f"Allowed: {', '.join(sorted(MUTATION_OPERATIONS))}"
        )

    key = str(args.get("idempotency_key") or "").strip() or f"xomni-{uuid.uuid4().hex}"
    if len(key) < 16:
        raise ValueError("idempotency_key must be at least 16 characters")

    correlation = str(args.get("correlation_id") or "").strip() or f"xomni-{uuid.uuid4().hex[:16]}"
    body = {
        "operation": operation,
        "arguments": dict(args.get("arguments") or {}),
        "expected_version": int(args.get("expected_version") or 0),
        "correlation_id": correlation,
    }

    try:
        async with httpx.AsyncClient(timeout=MUTATE_TIMEOUT, trust_env=False) as client:
            resp = await client.post(
                f"{base}/ros/{ro_id}/mutations",
                json=body,
                headers={**_auth(token), "Idempotency-Key": key},
            )
    except httpx.HTTPError as exc:
        return {"status": "offline", "executed": False, "error": type(exc).__name__,
                "message": f"Calibration IQ is not reachable at {base}. Nothing was changed."}

    if resp.status_code == 403:
        return {"status": "permission_denied", "executed": False, "upstream_status": 403,
                "message": "Calibration IQ refused this change for this token."}
    if resp.status_code == 409:
        detail = None
        try:
            detail = resp.json().get("detail")
        except ValueError:
            pass
        return {"status": "conflict", "executed": False, "conflict": True, "detail": detail,
                "message": "The repair order changed since it was read. Re-read it and retry "
                           "with the current expected_version."}
    if resp.status_code >= 400:
        return {"status": "error", "executed": False, "http_status": resp.status_code,
                "message": f"Calibration IQ returned HTTP {resp.status_code}. Nothing was changed."}

    try:
        payload = resp.json()
    except ValueError:
        return {"status": "error", "executed": False,
                "message": "Calibration IQ returned a non-JSON response."}

    receipt = dict(payload.get("receipt") or {})
    verified = bool(payload.get("success") and receipt.get("status") == "completed")
    receipt.update({
        "operation": operation,
        "target": ro_id,
        "requested_change": body,
        "idempotency_key": key,
        "authenticated_user": (user or {}).get("google_sub") or (user or {}).get("id") or "operator",
        "verified": verified,
    })

    return {
        # Never claim success the service did not confirm.
        "status": "success" if verified else "partial_success",
        "executed": True,
        "duplicate": bool(payload.get("duplicate")),
        "operation": operation,
        "repair_order_id": ro_id,
        "receipt": receipt,
        "message": (
            "Change confirmed by Calibration IQ."
            if verified
            else "Calibration IQ accepted the request but did not confirm completion. "
                 "Verify the repair order before treating this as done."
        ),
    }
