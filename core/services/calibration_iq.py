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
_service_token, read_repair_orders, and the page-merging logic in _collect
(the pagination/dedup guard that replaced the old _merge_counts helper).

The service token is read from the Calibration IQ project's own .env and
never enters model context, the database, or the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

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

OPERATOR_TIMEOUT = 60.0
OPERATOR_MAX_ACTIONS = 50
MAX_OPERATOR_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_OPERATOR_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024
MAX_OPERATOR_PHOTO_BYTES = 32 * 1024 * 1024
MAX_OPERATOR_ERROR_BYTES = 64 * 1024
_INVOCATION_CONTEXT_KEY = "__xomni_invocation"
ROUTINE_OPERATOR_OPERATIONS = frozenset({
    "create_ro",
    "update_ro",
    "change_status",
    "hold_ro",
    "resume_ro",
    "close_ro",
    "reopen_ro",
    "undo_status",
    "add_note",
    "update_note",
    "add_calibration",
    "update_calibration",
    "complete_calibration",
    "reopen_calibration",
    "mark_no_calibration_required",
    "reopen_calibration_review",
    "add_blocker",
    "update_blocker",
    "resolve_blocker",
    "reopen_blocker",
    "add_prerequisite",
    "update_prerequisite",
    "complete_prerequisite",
    "verify_prerequisite",
    "reject_prerequisite",
    "reopen_prerequisite",
    "update_research",
    "research_ro",
    "ensure_case_workspace",
    "create_folder",
    "rename_entry",
    "move_entry",
    "copy_entry",
    "create_file",
    "archive_entry",
    "restore_entry",
    "import_document",
    "update_document",
    "link_document",
    "unlink_document",
    "replace_document",
    "archive_document",
    "restore_document",
    "import_photo",
    "update_photo",
    "update_location",
    "create_location",
    "annotate_domo",
    "create_assessment",
    "update_assessment",
    "publish_assessment",
})
DESTRUCTIVE_OPERATOR_OPERATIONS = frozenset({
    "delete_calibration",
    "delete_blocker",
    "delete_photo",
    "delete_prerequisite",
})
# These backend actions require a confirmation bit in addition to X's
# approval-gateway decision. Inject it only inside the confirmation-gated
# handler; the routine tool cannot reach this path, and the older delete
# operations reject unknown fields under strict preflight.
BACKEND_EXPLICIT_CONFIRM_OPERATIONS = frozenset({
    "delete_photo",
    "delete_prerequisite",
})
CALIBRATION_MUTATION_OPERATIONS = frozenset({
    "add_calibration",
    "update_calibration",
    "complete_calibration",
    "reopen_calibration",
    "mark_no_calibration_required",
    "reopen_calibration_review",
})
_OPERATOR_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CONTENT_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_UNSAFE_BROWSER_CONTENT_TYPES = frozenset({
    "application/javascript",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/html",
    "text/javascript",
})


class CalibrationIQUnavailable(RuntimeError):
    pass


class CalibrationIQOperatorInput(ValueError):
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


def _title_case_status(raw: Any) -> str:
    """Best-effort human label from a SCREAMING_SNAKE_CASE status enum.

    Calibration IQ's collection response pairs every raw status with its own
    display_status (NEW_ARRIVAL / "New Arrival", CALIBRATION_COMPLETE /
    "Calibration Complete", etc.) -- confirmed 1:1 against the live service.
    This is only a fallback for shapes that omit the pretty label entirely
    (the operator snapshot's repair_order object has no display_status at
    all), so a raw enum is never shown, or matched against TERMINAL_STATUSES,
    verbatim.
    """
    text = str(raw or "").strip()
    return text.replace("_", " ").title() if text else ""


def _status_of(item: dict) -> str:
    label = _dig(item, "display_status", "status_display", default="")
    if label:
        return str(label).strip()
    return _title_case_status(_dig(item, "status", default=""))


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
            "Status": _status_of(item) or "-",
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

    Uses the first-class operator snapshot when available so history,
    research, managed documents, evidence and actors stay in session context.
    Both the snapshot route and the legacy detail route are keyed by
    Calibration IQ's internal id (a strict primary-key lookup server-side),
    not the human-facing RO number Otis actually uses in conversation --
    confirmed against the live service, where both 404 for a bare RO number.
    When neither identifier-shaped route resolves, the collection search
    below is used to find the exact matching row, and its id is used for one
    more snapshot request rather than settling for the collection row's
    thinner shape. That row has no vin, activity, audit, prerequisites,
    assessments, or photos -- silently returning it instead of retrying with
    the resolved id would look like a complete, verified answer while
    quietly dropping most of the detail Otis asked for.
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
    raw_detail: Optional[dict] = None
    tried: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=READ_TIMEOUT, trust_env=False) as client:

            async def _try_snapshot(candidate: str) -> bool:
                nonlocal item, raw_detail
                snapshot_url = f"{base}/operator/ros/{quote(candidate, safe='')}/snapshot"
                tried.append(snapshot_url)
                resp = await client.get(snapshot_url, headers=_auth(token))
                if resp.status_code != 200:
                    return False
                try:
                    snapshot_body = resp.json()
                except ValueError:
                    return False
                detail = _operator_snapshot_body(snapshot_body)
                ro = (detail or {}).get("repair_order")
                if not isinstance(ro, dict):
                    return False
                built = dict(ro)
                if isinstance((detail or {}).get("shop"), dict):
                    built["shop"] = detail["shop"]
                workflow = (detail or {}).get("workflow")
                if isinstance(workflow, dict) and workflow.get("status"):
                    workflow_status = workflow["status"]
                    if _normalized_label(built.get("status")) != _normalized_label(
                        workflow_status
                    ):
                        # workflow is the fresher source here, but it carries
                        # only the raw enum, not a pretty label -- show that
                        # honestly rather than keep a display_status that
                        # described the status this is now overriding.
                        built["display_status"] = None
                    built["status"] = workflow_status
                item = built
                raw_detail = detail
                return True

            await _try_snapshot(ident)

            url = f"{base}/ros/{ident}"
            if item is None:
                tried.append(url)
                resp = await client.get(url, headers=_auth(token))
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        # Current legacy route wraps detail as {ro: ...}; older
                        # revisions used item/repair_order. Never mistake the
                        # wrapper itself for the RO record.
                        item = (
                            body.get("snapshot")
                            or body.get("ro")
                            or body.get("item")
                            or body.get("repair_order")
                            or body
                        )
                        raw_detail = item if isinstance(item, dict) else None
                    except (AttributeError, ValueError):
                        item = None

            if item is None:
                # Neither identifier-shaped route recognized `ident` -- it is
                # most likely the human RO number. Resolve it via search, then
                # retry the rich snapshot with the id search proves is the
                # exact match, instead of settling for the collection row.
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
                    match = (exact or found or [None])[0]
                    resolved_id = (
                        str(_dig(match, "id", "repair_order_id", "uuid") or "").strip()
                        if isinstance(match, dict)
                        else ""
                    )
                    if not (
                        resolved_id
                        and resolved_id != ident
                        and await _try_snapshot(resolved_id)
                    ):
                        item = match
                        raw_detail = match if isinstance(match, dict) else None
    except httpx.HTTPError as exc:
        return {"status": "offline", "repair_order": None, "error": type(exc).__name__,
                "message": f"Calibration IQ is not reachable at {base}."}

    if not item:
        return {"status": "no_result", "repair_order": None, "query": ident,
                "tried": tried,
                "message": f"No repair order matched '{ident}'."}

    summary = _ro_rows([item])[0]
    detail = raw_detail or item
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
            "blockers": _dig(detail, "blockers", "blocking", default=[]),
            "requirements": _dig(
                detail, "calibrations", "calibration_requirements", "requirements", default=[]
            ),
            "notes": _dig(detail, "notes", "note"),
        },
        # Everything the service returned, so nothing is hidden from the
        # detail card just because this mapper didn't anticipate a field.
        "raw": _map_document_urls(detail),
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


# --------------------------------------------------------------------------
# first-class operator contract
# --------------------------------------------------------------------------

def _operator_error(
    code: str,
    message: str,
    *,
    http_status: Optional[int] = None,
    retryable: bool = False,
    details: Optional[dict[str, Any]] = None,
    executed: bool = False,
    may_have_executed: bool = False,
    indeterminate: bool = False,
) -> dict[str, Any]:
    error = {
        "code": str(code or "operation_failed"),
        "message": str(message or "Calibration IQ operator action failed."),
        "category": str(code or "operation_failed"),
        "retryable": bool(retryable),
        "details": dict(details or {}),
    }
    result = {
        "status": error["code"],
        "executed": bool(executed),
        "success": False,
        "verified": False,
        "partial": False,
        "http_status": http_status,
        "error": error,
        "message": error["message"],
    }
    if may_have_executed:
        result["may_have_executed"] = True
    if indeterminate:
        result["indeterminate"] = True
    return result


def _response_error(
    resp: httpx.Response,
    *,
    executed: bool = False,
    may_have_executed: bool = False,
) -> dict[str, Any]:
    mapping = {
        400: ("invalid_input", False),
        401: ("unauthorized", False),
        403: ("permission_denied", False),
        404: ("not_found", False),
        409: ("conflict", True),
        412: ("prerequisite_missing", False),
        422: ("invalid_input", False),
        429: ("temporary_service_failure", True),
    }
    code, retryable = mapping.get(
        resp.status_code,
        ("temporary_service_failure", True)
        if resp.status_code >= 500
        else ("operation_failed", False),
    )
    details: dict[str, Any] = {}
    message = f"Calibration IQ returned HTTP {resp.status_code}."
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        candidate = body.get("error") or body.get("detail")
        if isinstance(candidate, dict):
            upstream_code = str(candidate.get("code") or "").strip()
            if upstream_code:
                code = upstream_code
            message = str(candidate.get("message") or candidate.get("detail") or message)
            details = dict(candidate.get("details") or {})
            retryable = bool(candidate.get("retryable", retryable))
        elif candidate:
            message = str(candidate)
        elif body.get("message"):
            message = str(body["message"])
    return _operator_error(
        code,
        message,
        http_status=resp.status_code,
        retryable=retryable,
        details=details,
        executed=executed,
        may_have_executed=may_have_executed,
        indeterminate=may_have_executed,
    )


def _operator_credentials(settings) -> tuple[str, str] | dict[str, Any]:
    token = _service_token(settings.calibration_iq_project_path)
    if not token:
        return _operator_error(
            "not_configured",
            "Calibration IQ service token is not configured. Nothing was changed.",
        )
    return settings.calibration_iq_base_url, token


def _operator_snapshot_body(body: Any) -> Optional[dict[str, Any]]:
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("snapshot"), dict):
        return dict(body["snapshot"])
    # The live operator endpoint returns the snapshot directly and one of its
    # top-level fields is itself named repair_order. Do not unwrap that field
    # and discard research, documents, activity, audit, actors, and workflow.
    if any(
        key in body
        for key in (
            "workflow", "calibrations", "blockers", "research", "activity",
            "audit", "actors", "domo_comparison", "vehicle", "shop",
        )
    ):
        return dict(body)
    for key in ("repair_order", "ro", "item"):
        if isinstance(body.get(key), dict):
            return dict(body[key])
    return dict(body)


def _authoritative_repair_order_id(snapshot: Any) -> Optional[str]:
    """Return the internal repair-order id proved by an operator snapshot."""
    if not isinstance(snapshot, dict):
        return None
    value = _nested(
        snapshot,
        "repair_order.id",
        "repair_order.repair_order_id",
        "repair_order.uuid",
        "id",
        "repair_order_id",
        "uuid",
    )
    ident = str(value or "").strip()
    return ident if _OPERATOR_RESOURCE_ID_RE.fullmatch(ident) else None


def _repair_order_number_key(value: Any) -> str:
    """Match Calibration IQ's whitespace-insensitive RO-number identity."""
    return "".join(str(value or "").split()).casefold()


def _normalized_operator_arguments(
    operation: str, arguments: Any
) -> dict[str, Any]:
    """Normalize only known model aliases without weakening strict preflight."""
    normalized = dict(arguments or {})
    aliases: tuple[str, tuple[str, ...]] | None = None
    if operation == "add_note":
        aliases = ("body", ("note", "text", "content"))
    elif operation == "create_folder":
        aliases = ("path", ("folder_name", "name"))
    if aliases is None:
        return normalized

    canonical, alternate_names = aliases
    present_names = [
        name for name in (canonical, *alternate_names) if name in normalized
    ]
    if present_names:
        reference_name = present_names[0]
        reference_value = normalized[reference_name]
        conflicts = [
            name
            for name in present_names[1:]
            if type(normalized[name]) is not type(reference_value)
            or normalized[name] != reference_value
        ]
        if conflicts:
            supplied = ", ".join(present_names)
            raise CalibrationIQOperatorInput(
                f"{operation} received conflicting argument aliases ({supplied}). "
                f"Supply one unambiguous {canonical} value."
            )
        # Only synthesize the canonical field when it was absent. Equal aliases
        # beside an explicit canonical value are harmless but still removed so
        # Calibration IQ's strict unknown-field preflight remains authoritative.
        if canonical not in normalized:
            normalized[canonical] = reference_value
    for name in alternate_names:
        normalized.pop(name, None)
    return normalized


def _normalized_operator_action(action: dict[str, Any]) -> dict[str, Any]:
    operation = str(action.get("operation") or "").strip()
    normalized = {
        key: action[key]
        for key in (
            "operation",
            "repair_order_id",
            "target_id",
            "expected_version",
            "arguments",
        )
        if key in action and action[key] is not None
    }
    normalized["operation"] = operation
    if "arguments" in normalized:
        normalized["arguments"] = _normalized_operator_arguments(
            operation, normalized["arguments"]
        )
    return normalized


def _document_proxy_url(document_id: Any) -> Optional[str]:
    value = str(document_id or "").strip()
    if not _OPERATOR_RESOURCE_ID_RE.fullmatch(value):
        return None
    return f"/api/calibration-iq/documents/{quote(value, safe='')}/download"


def _photo_proxy_url(photo_id: Any, variant: str) -> Optional[str]:
    value = str(photo_id or "").strip()
    normalized_variant = str(variant or "").strip().casefold()
    if (
        not _OPERATOR_RESOURCE_ID_RE.fullmatch(value)
        or normalized_variant not in {"download", "thumbnail"}
    ):
        return None
    return (
        f"/api/calibration-iq/photos/{quote(value, safe='')}/{normalized_variant}"
    )


def _workspace_file_proxy_url(repair_order_id: Any, path: Any) -> Optional[str]:
    ro_id = str(repair_order_id or "").strip()
    if not _OPERATOR_RESOURCE_ID_RE.fullmatch(ro_id):
        return None
    try:
        relative_path = _workspace_relative_path(path, field="path")
    except CalibrationIQOperatorInput:
        return None
    if not relative_path:
        return None
    return "/api/calibration-iq/workspace-file?" + urlencode({
        "repair_order_id": ro_id,
        "path": relative_path,
    })


def _single_query_value(query: dict[str, list[str]], key: str) -> Optional[str]:
    values = query.get(key)
    if not isinstance(values, list) or len(values) != 1:
        return None
    value = str(values[0] or "")
    return value if value else None


def _operator_proxy_url(raw_url: Any) -> Optional[str]:
    """Map only an explicit, exact backend/X operator URL to its public proxy."""
    raw = str(raw_url or "").strip()
    if not raw or "\r" in raw or "\n" in raw or len(raw) > 4000:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    path = parsed.path.rstrip("/")

    document_match = re.search(
        r"(?:^|/)operator/documents/([^/]+)/download$", path
    ) or re.fullmatch(
        r"/api/calibration-iq/documents/([^/]+)/download", path
    )
    if document_match and not parsed.query and not parsed.fragment:
        return _document_proxy_url(unquote(document_match.group(1)))

    photo_match = re.search(
        r"(?:^|/)operator/photos/([^/]+)/(download|thumbnail)$", path
    ) or re.fullmatch(
        r"/api/calibration-iq/photos/([^/]+)/(download|thumbnail)", path
    )
    if photo_match and not parsed.query and not parsed.fragment:
        return _photo_proxy_url(
            unquote(photo_match.group(1)), photo_match.group(2)
        )

    workspace_match = re.search(
        r"(?:^|/)operator/ros/([^/]+)/files$", path
    )
    if workspace_match and not parsed.fragment:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"path"}:
            return None
        return _workspace_file_proxy_url(
            unquote(workspace_match.group(1)), _single_query_value(query, "path")
        )

    if path == "/api/calibration-iq/workspace-file" and not parsed.fragment:
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"repair_order_id", "path"}:
            return None
        return _workspace_file_proxy_url(
            _single_query_value(query, "repair_order_id"),
            _single_query_value(query, "path"),
        )
    return None


def _is_internal_url(raw_url: Any) -> bool:
    try:
        parsed = urlsplit(str(raw_url or "").strip())
    except ValueError:
        return True
    return (parsed.hostname or "").casefold() in {"127.0.0.1", "localhost", "::1"}


def _map_document_urls(value: Any, *, parent_key: str = "") -> Any:
    """Recursively replace genuine operator byte URLs without inventing links."""
    if isinstance(value, list):
        return [_map_document_urls(item, parent_key=parent_key) for item in value]
    if not isinstance(value, dict):
        return value
    mapped = {
        key: _map_document_urls(item, parent_key=str(key))
        for key, item in value.items()
    }
    for key, raw_url in list(mapped.items()):
        if key != "url" and not str(key).endswith("_url"):
            continue
        if not isinstance(raw_url, str):
            continue
        proxy = _operator_proxy_url(raw_url)
        if proxy:
            mapped[key] = proxy
        elif _is_internal_url(raw_url):
            mapped.pop(key, None)
    return mapped


async def _operator_snapshot_request(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    repair_order_id: str,
) -> dict[str, Any]:
    ident = str(repair_order_id or "").strip()
    if not ident:
        return _operator_error("invalid_input", "repair_order_id is required.")
    try:
        resp = await client.get(
            f"{base}/operator/ros/{quote(ident, safe='')}/snapshot",
            headers=_auth(token),
        )
    except httpx.HTTPError as exc:
        return _operator_error(
            "temporary_service_failure",
            "Calibration IQ is not reachable. Nothing was changed.",
            retryable=True,
            details={"exception": type(exc).__name__},
        )
    if resp.status_code >= 400:
        return _response_error(resp)
    try:
        body = resp.json()
    except ValueError:
        return _operator_error(
            "invalid_response", "Calibration IQ returned a non-JSON snapshot."
        )
    snapshot = _operator_snapshot_body(body)
    if snapshot is None:
        return _operator_error(
            "invalid_response", "Calibration IQ returned an invalid snapshot."
        )
    return {
        "status": "verified",
        "executed": False,
        "success": True,
        "verified": True,
        "repair_order_id": ident,
        "snapshot": _map_document_urls(snapshot),
    }


async def _resolve_operator_repair_order(
    client: httpx.AsyncClient,
    base: str,
    token: str,
    identifier: Any,
) -> dict[str, Any]:
    """Resolve an internal id or business RO number to one proved internal id."""
    ident = str(identifier or "").strip()
    if not ident:
        return _operator_error("invalid_input", "repair_order_id is required.")

    direct = await _operator_snapshot_request(client, base, token, ident)
    if direct.get("status") == "verified":
        authoritative_id = _authoritative_repair_order_id(direct.get("snapshot"))
        if authoritative_id is None:
            return _operator_error(
                "invalid_response",
                "Calibration IQ's operator snapshot did not prove a repair-order id. "
                "Nothing was changed.",
            )
        return {
            "status": "verified",
            "repair_order_id": authoritative_id,
            "snapshot": direct["snapshot"],
            "resolved_from": ident,
        }
    if direct.get("http_status") != 404:
        return direct

    # Snapshot routes require the internal id. Search every source state by the
    # business number, collect the complete result set, and accept exactly one
    # authoritative id. A partial or changing collection can never authorize a
    # mutation because it cannot disprove an ambiguous same-number RO.
    query_value = "".join(ident.split()) or ident
    identifier_key = _repair_order_number_key(ident)
    offset = 0
    expected_total: Optional[int] = None
    exact_matches: dict[str, dict[str, Any]] = {}
    for _page_number in range(MAX_PAGE_REQUESTS):
        try:
            response = await client.get(
                f"{base}/collection/ros",
                params={
                    "q": query_value,
                    "source_scope": "all",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
                headers=_auth(token),
            )
        except httpx.HTTPError as exc:
            return _operator_error(
                "temporary_service_failure",
                "Calibration IQ repair-order resolution is unavailable. Nothing was changed.",
                retryable=True,
                details={"exception": type(exc).__name__},
            )
        if response.status_code >= 400:
            return _response_error(response)
        try:
            body = response.json()
        except ValueError:
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned non-JSON repair-order resolution data. "
                "Nothing was changed.",
            )
        if not isinstance(body, dict):
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned invalid repair-order resolution data. "
                "Nothing was changed.",
            )
        rows = body.get("items")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned malformed repair-order resolution rows. "
                "Nothing was changed.",
            )
        try:
            total = int(body.get("count"))
        except (TypeError, ValueError):
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned an invalid repair-order resolution count. "
                "Nothing was changed.",
            )
        if total < 0 or (expected_total is not None and total != expected_total):
            return _operator_error(
                "conflict",
                "Calibration IQ's repair-order collection changed during resolution. "
                "Nothing was changed; retry from a fresh read.",
                retryable=True,
            )
        expected_total = total
        if total > MAX_COLLECT:
            return _operator_error(
                "resolution_incomplete",
                "Calibration IQ returned too many possible repair orders to prove a unique "
                "target. Nothing was changed.",
                details={"identifier": ident, "matching_collection_count": total},
            )

        for row in rows:
            ro_number = _dig(row, "ro_number", "roNumber", "number", "ro")
            if _repair_order_number_key(ro_number) != identifier_key:
                continue
            row_id = str(_dig(row, "id", "repair_order_id", "uuid") or "").strip()
            if not _OPERATOR_RESOURCE_ID_RE.fullmatch(row_id):
                return _operator_error(
                    "invalid_response",
                    "Calibration IQ matched a repair order without a valid internal id. "
                    "Nothing was changed.",
                )
            exact_matches[row_id] = row

        offset += len(rows)
        if offset >= total:
            break
        if not rows:
            return _operator_error(
                "resolution_incomplete",
                "Calibration IQ ended repair-order resolution before the authoritative "
                "collection was complete. Nothing was changed.",
            )
    else:
        return _operator_error(
            "resolution_incomplete",
            "Calibration IQ repair-order resolution reached its paging safety limit. "
            "Nothing was changed.",
        )

    if not exact_matches:
        return _operator_error(
            "not_found",
            f"No Calibration IQ repair order exactly matched {ident}. Nothing was changed.",
            http_status=404,
            details={"identifier": ident, "exact_match_count": 0},
        )
    if len(exact_matches) != 1:
        return _operator_error(
            "ambiguous_identifier",
            f"Calibration IQ found multiple repair orders numbered {ident}. Nothing was changed.",
            details={"identifier": ident, "exact_match_count": len(exact_matches)},
        )

    authoritative_id = next(iter(exact_matches))
    snapshot_result = await _operator_snapshot_request(
        client, base, token, authoritative_id
    )
    if snapshot_result.get("status") != "verified":
        return snapshot_result
    snapshot_id = _authoritative_repair_order_id(snapshot_result.get("snapshot"))
    if snapshot_id != authoritative_id:
        return _operator_error(
            "invalid_response",
            "Calibration IQ's repair-order collection and operator snapshot disagreed. "
            "Nothing was changed.",
            details={"identifier": ident},
        )
    return {
        "status": "verified",
        "repair_order_id": authoritative_id,
        "snapshot": snapshot_result["snapshot"],
        "resolved_from": ident,
    }


async def operator_capabilities(settings) -> dict[str, Any]:
    credentials = _operator_credentials(settings)
    if isinstance(credentials, dict):
        return credentials
    _, token = credentials
    base = await resolve_base(settings)
    try:
        async with httpx.AsyncClient(
            timeout=READ_TIMEOUT, trust_env=False, follow_redirects=False
        ) as client:
            resp = await client.get(f"{base}/operator/capabilities", headers=_auth(token))
    except httpx.HTTPError as exc:
        return _operator_error(
            "temporary_service_failure",
            "Calibration IQ operator capabilities are unavailable.",
            retryable=True,
            details={"exception": type(exc).__name__},
        )
    if resp.status_code >= 400:
        return _response_error(resp)
    try:
        body = resp.json()
    except ValueError:
        return _operator_error(
            "invalid_response", "Calibration IQ returned non-JSON operator capabilities."
        )
    return {
        "status": "verified",
        "executed": False,
        "success": True,
        "verified": True,
        "capabilities": body,
    }


async def operator_snapshot(settings, repair_order_id: str) -> dict[str, Any]:
    credentials = _operator_credentials(settings)
    if isinstance(credentials, dict):
        return credentials
    _, token = credentials
    base = await resolve_base(settings)
    async with httpx.AsyncClient(
        timeout=READ_TIMEOUT, trust_env=False, follow_redirects=False
    ) as client:
        return await _operator_snapshot_request(client, base, token, repair_order_id)


async def operator_resolve_snapshot(
    settings, repair_order_identifier: str
) -> dict[str, Any]:
    """Resolve an internal id or exact business RO number to one operator snapshot.

    The operator snapshot endpoint accepts only Calibration IQ's internal id.  Field
    workflows commonly start with the business RO number, so callers must not fall
    back to the legacy collection summary (which omits optimistic versions and can
    be ambiguous).  This public read wrapper reuses the same exhaustive, exact-match
    resolver that protects operator mutations and returns only an authoritative
    snapshot/id pair.
    """
    credentials = _operator_credentials(settings)
    if isinstance(credentials, dict):
        return credentials
    _, token = credentials
    base = await resolve_base(settings)
    async with httpx.AsyncClient(
        timeout=READ_TIMEOUT, trust_env=False, follow_redirects=False
    ) as client:
        resolved = await _resolve_operator_repair_order(
            client, base, token, repair_order_identifier
        )
    if resolved.get("status") != "verified":
        return resolved
    return {
        "status": "verified",
        "executed": False,
        "success": True,
        "verified": True,
        "repair_order_id": resolved["repair_order_id"],
        "resolved_from": resolved.get("resolved_from"),
        "snapshot": resolved["snapshot"],
    }


def _capability_operations(body: Any) -> set[str]:
    if not isinstance(body, dict):
        return set()
    policy = body.get("policy")
    raw: Any
    if isinstance(policy, dict):
        raw = [
            *(policy.get("routine") or []),
            *(policy.get("destructive") or []),
        ]
    else:
        # Compatibility with pre-operator development fixtures only. The
        # current contract stores permission names in `capabilities`, not
        # operation names, so never interpret that field as an allow-list.
        raw = body.get("operations") or body.get("items") or []
    if isinstance(raw, dict):
        raw = [dict(value, operation=key) if isinstance(value, dict) else key
               for key, value in raw.items()]
    operations = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                operations.add(item)
            elif isinstance(item, dict):
                name = item.get("operation") or item.get("name") or item.get("id")
                if name:
                    operations.add(str(name))
    return operations


def _stable_action_ids(
    context: dict[str, Any], occurrence_ordinal: int, action: dict[str, Any],
    *, action_index: Optional[int] = None,
) -> tuple[str, str]:
    # Idempotency represents one user intent, not one model attempt. The model
    # may reissue an identical batch later in the same turn with a new tool
    # call id; the persisted user-message id remains stable and absorbs it.
    canonical_action = json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
    idempotency_seed = json.dumps(
        {
            "conversation_id": context.get("conversation_id"),
            "message_id": context.get("message_id"),
            # A retry may emit the same action alone or in a different order.
            # Its identity therefore depends on the occurrence number among
            # identical canonical actions, not its absolute batch position.
            "occurrence_ordinal": occurrence_ordinal,
            "action": canonical_action,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_digest = hashlib.sha256(idempotency_seed.encode("utf-8")).hexdigest()
    correlation_seed = json.dumps(
        {
            "conversation_id": context.get("conversation_id"),
            "message_id": context.get("message_id"),
            "tool_call_id": context.get("tool_call_id"),
            "action_index": (
                occurrence_ordinal if action_index is None else action_index
            ),
            "idempotency": idempotency_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    correlation_digest = hashlib.sha256(correlation_seed.encode("utf-8")).hexdigest()
    return f"xomni-{idempotency_digest}", f"x-{correlation_digest[:64]}"


def _nested(value: Any, *paths: str, default: Any = None) -> Any:
    for path in paths:
        current = value
        found = True
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found and current not in (None, ""):
            return current
    return default


def _research_vehicle_label(snapshot: dict[str, Any]) -> str:
    vehicle = _nested(snapshot, "vehicle", "repair_order.vehicle", "ro.vehicle", default={})
    vehicle = vehicle if isinstance(vehicle, dict) else {}
    parts = [
        _nested(vehicle, "year", default=_nested(snapshot, "year", "vehicle_year")),
        _nested(vehicle, "make", default=_nested(snapshot, "make", "vehicle_make")),
        _nested(vehicle, "model", default=_nested(snapshot, "model", "vehicle_model")),
        _nested(vehicle, "trim", default=_nested(snapshot, "trim", "vehicle_trim")),
    ]
    return " ".join(str(part).strip() for part in parts if str(part or "").strip())


def _research_calibrations(snapshot: dict[str, Any], arguments: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[Any] = []
    for path in (
        "calibration_items",
        "calibrations",
        "calibration_requirements",
        "requirements",
        "repair_order.calibration_items",
        "repair_order.calibrations",
        "research.calibration_items",
        "research.required_calibrations",
        "required_calibrations",
    ):
        value = _nested(snapshot, path)
        if isinstance(value, list):
            candidates.extend(value)
    explicit = arguments.get("calibrations")
    if isinstance(explicit, list):
        candidates.extend(explicit)

    specs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str):
            item_id, label, query_value = "", item, ""
            state = ""
        elif isinstance(item, dict):
            item_id = str(
                item.get("id") or item.get("calibration_item_id")
                or item.get("requirement_id") or ""
            ).strip()
            label = str(
                item.get("name") or item.get("label") or item.get("calibration_type")
                or item.get("system") or item.get("description") or item.get("type") or item_id
            ).strip()
            query_value = str(item.get("query") or "").strip()
            state = str(
                item.get("determination") or item.get("disposition")
                or item.get("state") or item.get("status") or ""
            ).casefold()
        else:
            continue
        if state in {
            "not_required", "removed", "removed_after_review", "deleted", "archived"
        }:
            continue
        key = item_id or label.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        specs.append({"id": item_id, "label": label or item_id, "query": query_value})

    explicit_ids = arguments.get("calibration_ids") or arguments.get("calibration_item_ids")
    if isinstance(explicit_ids, list):
        by_id = {item["id"]: item for item in specs if item["id"]}
        for raw_id in explicit_ids:
            item_id = str(raw_id or "").strip()
            if item_id and item_id not in by_id:
                specs.append({"id": item_id, "label": item_id, "query": ""})
    return specs


def _research_queries(
    vehicle: str, specs: list[dict[str, str]], arguments: dict[str, Any], ro_number: str = ""
) -> list[dict[str, str]]:
    explicit_queries = arguments.get("queries")
    global_query = str(arguments.get("query") or "").strip()
    output: list[dict[str, str]] = []
    if isinstance(explicit_queries, list) and explicit_queries:
        for index, raw in enumerate(explicit_queries):
            if isinstance(raw, dict):
                query_value = str(raw.get("query") or "").strip()
                calibration_id = str(raw.get("calibration_id") or "").strip()
                label = str(raw.get("label") or calibration_id).strip()
            else:
                query_value = str(raw or "").strip()
                spec = specs[index] if index < len(specs) else {}
                calibration_id = str(spec.get("id") or "")
                label = str(spec.get("label") or query_value)
            if query_value:
                output.append({"id": calibration_id, "label": label, "query": query_value})
        return output
    if specs:
        for spec in specs:
            query_value = spec["query"] or " ".join(
                part for part in (vehicle, spec["label"], global_query) if part
            )
            output.append({**spec, "query": query_value.strip()})
    else:
        query_value = global_query or " ".join(
            part for part in (vehicle, "OEM calibration procedures") if part
        )
        output.append({"id": "", "label": "vehicle calibration research", "query": query_value})
    # ADAS Map coverage reports in this library are filed under the repair
    # order's own number ("2400911731 ADAS Map.pdf"), not under a vehicle
    # description, so none of the calibration-type queries above can ever
    # find one -- they need a query built from the RO number itself.
    if ro_number:
        output.append({"id": "", "label": "ADAS Map", "query": f"{ro_number} ADAS Map"})
    return output


def _research_requirement_key(item: dict[str, Any]) -> str:
    """Return the stable identity used to prove coverage of a required calibration."""
    item_id = str(item.get("id") or item.get("calibration_id") or "").strip()
    if item_id:
        return f"id:{item_id}"
    label = str(item.get("label") or item.get("calibration") or "").strip()
    normalized_label = " ".join(label.casefold().split())
    return f"label:{normalized_label}" if normalized_label else ""


def _workspace_relative_path(raw: Any, *, field: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    if not value:
        return ""
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part or "\x00" in part for part in path.parts)
    ):
        raise CalibrationIQOperatorInput(
            f"{field} must be a normalized relative path inside the Calibration IQ case workspace."
        )
    return path.as_posix()


def _existing_research_documents(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in ("documents", "research.documents", "research_case.documents"):
        items = _nested(snapshot, path)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("archived_at"):
                continue
            document_id = str(item.get("id") or item.get("document_id") or "").strip()
            key = document_id or json.dumps(item, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
    return output


def _matching_existing_document(
    existing: list[dict[str, Any]], *, source_uri: str, source_name: str
) -> Optional[dict[str, Any]]:
    uri_key = source_uri.strip().casefold()
    name_key = source_name.strip().casefold()
    for item in existing:
        if str(item.get("source_uri") or "").strip().casefold() == uri_key:
            return item
    for item in existing:
        stored = str(
            item.get("storage_relative_path") or item.get("storage_key") or ""
        ).strip().replace("\\", "/").casefold()
        if stored and PurePosixPath(stored).name.casefold() == name_key:
            return item
    return None


def _existing_document_version(
    document: dict[str, Any], *, operation: str
) -> int:
    """Return the authoritative concurrency token for an existing document."""
    document_id = str(
        document.get("id") or document.get("document_id") or ""
    ).strip()
    version = document.get("version")
    if (
        not document_id
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
    ):
        raise CalibrationIQOperatorInput(
            "The authoritative Calibration IQ snapshot did not provide a valid "
            f"positive document version required for {operation}. Nothing was changed."
        )
    return version


async def _expand_research_action(
    adas: Any,
    action: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ro_id = str(action.get("repair_order_id") or "").strip()
    arguments = dict(action.get("arguments") or {})
    vehicle = _research_vehicle_label(snapshot)
    specs = _research_calibrations(snapshot, arguments)
    ro_number = str(
        _dig(
            snapshot,
            "repair_order.ro_number", "repair_order.number",
            "ro_number", "roNumber", "number", "ro",
        ) or ""
    ).strip()
    queries = _research_queries(vehicle, specs, arguments, ro_number=ro_number)
    search_results = await asyncio.gather(*[
        asyncio.to_thread(adas.search, {"query": item["query"]})
        for item in queries
    ])

    documents: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for spec, search in zip(queries, search_results):
        search = search if isinstance(search, dict) else {}
        hits = [
            hit for hit in (search.get("results") or [])
            if isinstance(hit, dict)
            and hit.get("excerpt")
            and isinstance(hit.get("page"), int)
            and int(hit.get("source_match_score") or 0) >= 10
        ]
        exact_source_matched = search.get("exact_source_matched") is True
        supported = False
        finding_docs = []
        for matched in search.get("matched_documents") or []:
            if not isinstance(matched, dict) or int(matched.get("source_match_score") or 0) < 10:
                continue
            relative = str(matched.get("relative_path") or "").strip()
            if not relative:
                continue
            doc_hits = [hit for hit in hits if str(hit.get("relative_path") or "") == relative]
            try:
                source_path = adas.resolve_relative(relative)
            except (OSError, ValueError):
                continue
            document_supported = bool(exact_source_matched and doc_hits)
            supported = supported or document_supported
            record = documents.setdefault(relative, {
                "relative_path": relative,
                "source_path": str(source_path),
                "source_name": source_path.name,
                "title": str(matched.get("title") or source_path.stem),
                "pages": set(),
                "calibration_item_ids": set(),
                "queries": set(),
                "validated": False,
                "is_adas_map": False,
            })
            record["pages"].update(int(hit["page"]) for hit in doc_hits)
            record["queries"].add(spec["query"])
            if spec.get("label") == "ADAS Map":
                record["is_adas_map"] = True
            if document_supported:
                record["validated"] = True
                if spec.get("id"):
                    record["calibration_item_ids"].add(spec["id"])
            finding_docs.append({
                "title": record["title"],
                "source": record["source_name"],
                "relative_path": relative,
                "pages": sorted(int(hit["page"]) for hit in doc_hits),
            })
        findings.append({
            "calibration_id": spec.get("id") or None,
            "calibration": spec.get("label"),
            "query": spec["query"],
            "status": str(search.get("status") or "invalid_response"),
            "supported": supported,
            "documents": finding_docs,
            "missing_reason": None if supported else (
                search.get("message")
                or "No safely resolved exact OEM source with a matching procedure page was available."
            ),
        })

    expanded: list[dict[str, Any]] = [{
        "operation": "ensure_case_workspace",
        "repair_order_id": ro_id,
        "arguments": {},
    }]
    imported = []
    already_present = []
    destination_path = _workspace_relative_path(
        arguments.get("destination_path"), field="destination_path"
    )
    destination_folder = _workspace_relative_path(
        arguments.get("destination_folder"), field="destination_folder"
    )
    if destination_path and destination_folder:
        raise CalibrationIQOperatorInput(
            "Use destination_path for one exact file or destination_folder for a document set, not both."
        )
    if destination_path and len(documents) > 1:
        raise CalibrationIQOperatorInput(
            "destination_path is an exact file path and can only be used when research matches one document; "
            "use destination_folder for multiple OEM PDFs."
        )
    existing_documents = _existing_research_documents(snapshot)
    for record in documents.values():
        pages = sorted(record["pages"])
        calibration_ids = sorted(record["calibration_item_ids"])
        canonical_source_uri = f"adas-si:///{quote(record['relative_path'])}"
        default_document_type = "adas_map_report" if record.get("is_adas_map") else "oem_procedure"
        import_arguments: dict[str, Any] = {
            "source_path": record["source_path"],
            "document_type": str(arguments.get("document_type") or default_document_type),
            "title": record["title"],
            "source_uri": canonical_source_uri,
            "source_name": record["source_name"],
            "page_references": [f"p. {page}" for page in pages],
            "citation": (
                f"{record['source_name']}, "
                + (", ".join(f"p. {page}" for page in pages) if pages else "source matched; page not extracted")
            ),
            "notes": "ADAS SI queries: " + "; ".join(sorted(record["queries"])),
            "status": "validated" if record["validated"] else "candidate",
            "calibration_item_ids": calibration_ids,
        }
        if destination_path:
            import_arguments["destination_path"] = destination_path
        elif destination_folder:
            import_arguments["destination_path"] = _workspace_relative_path(
                f"{destination_folder}/{record['source_name']}",
                field="destination_folder",
            )

        existing = _matching_existing_document(
            existing_documents,
            source_uri=canonical_source_uri,
            source_name=record["source_name"],
        )
        if existing is not None:
            document_id = str(existing.get("id") or existing.get("document_id") or "").strip()
            existing_ids = {
                str(item) for item in (existing.get("calibration_item_ids") or []) if item
            }
            missing_ids = sorted(set(calibration_ids) - existing_ids)
            existing_pages = {str(item) for item in (existing.get("page_references") or [])}
            desired_pages = set(import_arguments["page_references"])
            needs_metadata_update = bool(
                document_id
                and (
                    (
                        record["validated"]
                        and str(existing.get("status") or "").strip().casefold()
                        != "validated"
                    )
                    or not desired_pages.issubset(existing_pages)
                )
            )
            if needs_metadata_update:
                changes: dict[str, Any] = {
                    "status": import_arguments["status"],
                    "page_references": sorted(existing_pages | desired_pages),
                    "citation": import_arguments["citation"],
                    "notes": import_arguments["notes"],
                }
                # A metadata refresh and missing evidence links must be one
                # optimistic-concurrency mutation. Emitting update_document
                # followed by link_document would require guessing the first
                # action's output version inside the same backend batch.
                if missing_ids:
                    changes["calibration_item_ids"] = sorted(
                        existing_ids | set(calibration_ids)
                    )
                expanded.append({
                    "operation": "update_document",
                    "target_id": document_id,
                    "expected_version": _existing_document_version(
                        existing, operation="update_document"
                    ),
                    "arguments": changes,
                })
            elif document_id and missing_ids:
                expanded.append({
                    "operation": "link_document",
                    "target_id": document_id,
                    "expected_version": _existing_document_version(
                        existing, operation="link_document"
                    ),
                    "arguments": {"calibration_item_ids": missing_ids},
                })
            already_present.append({
                "document_id": document_id or None,
                "title": record["title"],
                "source": record["source_name"],
                "source_uri": canonical_source_uri,
                "calibration_item_ids": sorted(existing_ids | set(calibration_ids)),
                "new_links_requested": missing_ids,
                "metadata_update_requested": needs_metadata_update,
                "download_url": _document_proxy_url(document_id),
            })
            continue
        expanded.append({
            "operation": "import_document",
            "repair_order_id": ro_id,
            "arguments": import_arguments,
        })
        imported.append({
            "title": record["title"],
            "source": record["source_name"],
            "relative_path": record["relative_path"],
            "pages": pages,
            "calibration_item_ids": calibration_ids,
            "status": import_arguments["status"],
        })

    requested_complete = arguments.get("complete_research") is True
    required_by_key = {
        key: spec
        for spec in specs
        if (key := _research_requirement_key(spec))
    }
    supported_keys = {
        key
        for finding in findings
        if finding["supported"] and (key := _research_requirement_key(finding))
    }
    eligible_complete = bool(required_by_key) and set(required_by_key).issubset(supported_keys)
    missing = [
        {
            "calibration_id": finding["calibration_id"],
            "calibration": finding["calibration"],
            "reason": finding["missing_reason"],
        }
        for finding in findings if not finding["supported"]
    ]
    reported_missing_keys = {
        key
        for finding in findings
        if not finding["supported"] and (key := _research_requirement_key(finding))
    }
    for key, spec in required_by_key.items():
        if key in supported_keys or key in reported_missing_keys:
            continue
        missing.append({
            "calibration_id": spec.get("id") or None,
            "calibration": spec.get("label"),
            "reason": "No supported OEM evidence query covered this required calibration.",
        })
    already_complete = _research_state(snapshot) == "research_complete"
    if requested_complete and eligible_complete and not already_complete:
        summary = str(arguments.get("summary") or "").strip() or (
            f"X verified OEM evidence for {len(required_by_key)} required calibration(s)."
        )
        update_arguments: dict[str, Any] = {
            "state": "research_complete",
            "summary": summary,
            "reason": str(arguments.get("reason") or "X source-backed research completion"),
        }
        research_version = _nested(snapshot, "research.version", "research_case.version")
        # Calibration IQ's optimistic-concurrency check reads expected_version
        # off the action itself, not out of arguments (the backend overwrites
        # any "version" placed there with its own freshly observed value
        # before dispatch, so a copy inside arguments is inert). research_ro
        # is a composite the caller issues without ever having fetched the
        # research case directly, so nothing else would ever populate this --
        # it has to come from the pre-fetched snapshot here, or Calibration
        # IQ rejects every completion with "expected_version is required".
        #
        # It also has to account for everything queued ahead of it in this
        # same batch: ensure_case_workspace, and every import_document /
        # update_document / link_document above all touch the same
        # ResearchCase row and each bump its version by exactly one as a side
        # effect, so by the time this action actually dispatches the real
        # version is the pre-batch snapshot plus one per prior queued action
        # -- not the pre-batch snapshot value itself.
        expected_version = (
            research_version + len(expanded)
            if isinstance(research_version, int) and not isinstance(research_version, bool)
            else action.get("expected_version")
        )
        expanded.append({
            "operation": "update_research",
            "repair_order_id": ro_id,
            "expected_version": expected_version,
            "arguments": update_arguments,
        })

    report = {
        "repair_order_id": ro_id,
        "vehicle": vehicle or None,
        "required_calibrations": [
            {"id": spec.get("id") or None, "label": spec.get("label")}
            for spec in required_by_key.values()
        ],
        "findings": findings,
        "documents_prepared": imported,
        "already_present": already_present,
        "missing_documents": missing,
        "research_complete_requested": requested_complete,
        "research_complete_eligible": eligible_complete,
        "research_complete_action_added": requested_complete and eligible_complete and not already_complete,
        "research_complete_was_already_set": already_complete,
        "research_complete_already_verified": False,
        "completion_withheld": requested_complete and not eligible_complete,
    }
    return expanded, report


def _receipt_verified(receipt: Any) -> bool:
    if not isinstance(receipt, dict):
        return False
    verification = receipt.get("verification")
    return bool(
        receipt.get("status") == "completed"
        and receipt.get("success") is True
        and isinstance(verification, dict)
        and verification.get("verified") is True
    )


def _research_state(snapshot: Any) -> str:
    if not isinstance(snapshot, dict):
        return ""
    return str(_nested(
        snapshot,
        "research.state",
        "research_case.state",
        "research_state",
        "repair_order.research_state",
        default="",
    )).casefold()


def _verify_persisted_research_evidence(
    snapshot: Any,
    required_calibrations: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    """Prove final-state managed OEM evidence for every required calibration."""
    if not isinstance(snapshot, dict) or not isinstance(required_calibrations, list):
        return False, [{
            "calibration_id": None,
            "calibration": "required calibration",
            "reason": "The authoritative final research snapshot was unavailable.",
        }]
    documents = _existing_research_documents(snapshot)
    evidence_documents: list[dict[str, Any]] = []
    for document in documents:
        document_id = str(document.get("id") or document.get("document_id") or "").strip()
        storage_path = str(
            document.get("storage_relative_path") or document.get("storage_key") or ""
        ).strip().replace("\\", "/")
        file_size = document.get("file_size")
        sha256 = str(document.get("sha256") or "").strip().casefold()
        page_references = document.get("page_references")
        download_url = str(document.get("download_url") or "").strip()
        if (
            document.get("archived_at") not in (None, "")
            or str(document.get("status") or "").casefold() != "validated"
            or not document_id
            or not storage_path
            or storage_path.casefold().startswith(".archive/")
            or isinstance(file_size, bool)
            or not isinstance(file_size, int)
            or file_size <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or not str(document.get("source_uri") or "").strip()
            or not str(document.get("source_name") or "").strip()
            or not isinstance(page_references, list)
            or not any(str(item or "").strip() for item in page_references)
            or not str(document.get("citation") or "").strip()
            or download_url != _document_proxy_url(document_id)
        ):
            continue
        evidence_documents.append(document)

    missing: list[dict[str, Any]] = []
    for required in required_calibrations:
        if not isinstance(required, dict):
            continue
        calibration_id = str(required.get("id") or "").strip()
        label = str(required.get("label") or calibration_id or "required calibration").strip()
        linked = bool(calibration_id) and any(
            calibration_id in {
                str(item).strip()
                for item in (document.get("calibration_item_ids") or [])
                if str(item or "").strip()
            }
            for document in evidence_documents
        )
        if not linked:
            missing.append({
                "calibration_id": calibration_id or None,
                "calibration": label,
                "reason": (
                    "The final snapshot did not contain an active validated managed document "
                    "with this calibration link, OEM provenance, page citation, stored-file "
                    "integrity, and verified download URL."
                ),
            })
    return bool(required_calibrations) and not missing, missing


async def operator_execute(
    settings,
    adas: Any,
    args: dict[str, Any],
    *,
    destructive: bool = False,
) -> dict[str, Any]:
    """Execute one exact-once operator batch, then reread authoritative RO state."""
    context = args.get(_INVOCATION_CONTEXT_KEY)
    if (
        not isinstance(context, dict)
        or not context.get("conversation_id")
        or isinstance(context.get("message_id"), bool)
        or not isinstance(context.get("message_id"), int)
        or context.get("message_id") <= 0
        or not context.get("tool_call_id")
    ):
        return _operator_error(
            "unauthorized_context",
            "Calibration IQ operator identity is missing. Nothing was changed.",
        )
    raw_actions = args.get("actions")
    if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= OPERATOR_MAX_ACTIONS:
        return _operator_error(
            "invalid_input", f"actions must contain 1-{OPERATOR_MAX_ACTIONS} items."
        )
    for index, action in enumerate(raw_actions):
        if not isinstance(action, dict):
            return _operator_error("invalid_input", f"actions[{index}] must be an object.")
        operation = str(action.get("operation") or "").strip()
        if not operation:
            return _operator_error("invalid_input", f"actions[{index}].operation is required.")
        if operation.startswith("hard_delete_"):
            return _operator_error(
                "invalid_operation",
                f"{operation} is not an advertised Calibration IQ operator operation.",
            )
        if operation not in (
            ROUTINE_OPERATOR_OPERATIONS | DESTRUCTIVE_OPERATOR_OPERATIONS
        ):
            return _operator_error(
                "invalid_operation",
                f"{operation} is not an X-approved Calibration IQ operator operation.",
            )
        destructive_operation = operation in DESTRUCTIVE_OPERATOR_OPERATIONS
        if destructive != destructive_operation:
            message = (
                f"{operation} requires calibration_iq_destructive and Owner confirmation."
                if destructive_operation
                else f"{operation} is not one of Calibration IQ's destructive operations."
            )
            return _operator_error(
                "approval_required" if destructive_operation else "invalid_operation", message
            )
        inner = action.get("arguments")
        if inner is not None and not isinstance(inner, dict):
            return _operator_error(
                "invalid_input", f"actions[{index}].arguments must be an object."
            )
        requested_research_state = re.sub(
            r"[\s-]+", "_", str((inner or {}).get("state") or "").strip().casefold()
        )
        if operation == "update_research" and requested_research_state in {
            "complete", "completed", "research_complete", "research_completed"
        }:
            return _operator_error(
                "prerequisite_missing",
                "Research completion must use research_ro with complete_research=true so OEM evidence is verified first.",
            )

    normalized_actions: list[dict[str, Any]] = []
    for index, action in enumerate(raw_actions):
        try:
            normalized_actions.append(_normalized_operator_action(action))
        except CalibrationIQOperatorInput as exc:
            return _operator_error("invalid_input", f"actions[{index}]: {exc}")

    supplied_research_ids = {
        str(action.get("repair_order_id") or "").strip().casefold()
        for action in normalized_actions
        if action.get("operation") == "research_ro"
    }
    supplied_calibration_ids = {
        str(action.get("repair_order_id") or "").strip().casefold()
        for action in normalized_actions
        if action.get("operation") in CALIBRATION_MUTATION_OPERATIONS
    }
    supplied_mixed_ids = sorted(
        item for item in supplied_research_ids & supplied_calibration_ids if item
    )
    if supplied_mixed_ids:
        return _operator_error(
            "prerequisite_missing",
            "Calibration mutations and research_ro for the same repair order require "
            "sequential calibration_iq_operator calls in this same user turn: apply and "
            "verify the calibration change first, then use its generated id in research_ro. "
            "Nothing was run.",
            details={"repair_order_ids": supplied_mixed_ids},
        )

    credentials = _operator_credentials(settings)
    if isinstance(credentials, dict):
        return credentials
    _, token = credentials
    base = await resolve_base(settings)
    research_reports: list[dict[str, Any]] = []
    pre_snapshots: dict[str, dict[str, Any]] = {}
    expanded_actions: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=OPERATOR_TIMEOUT, trust_env=False, follow_redirects=False
    ) as client:
        try:
            capability_resp = await client.get(
                f"{base}/operator/capabilities", headers=_auth(token)
            )
        except httpx.HTTPError as exc:
            return _operator_error(
                "temporary_service_failure",
                "Calibration IQ operator capabilities are unavailable. Nothing was changed.",
                retryable=True,
                details={"exception": type(exc).__name__},
            )
        if capability_resp.status_code >= 400:
            return _response_error(capability_resp)
        try:
            capability_body = capability_resp.json()
        except ValueError:
            return _operator_error(
                "invalid_response", "Calibration IQ returned non-JSON operator capabilities."
            )
        supported_operations = _capability_operations(capability_body)
        if not supported_operations:
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned no valid operator operation policy. Nothing was changed.",
            )

        resolution_cache: dict[str, dict[str, Any]] = {}
        resolved_actions: list[dict[str, Any]] = []
        for action in normalized_actions:
            action_copy = dict(action)
            if action_copy.get("repair_order_id") is not None:
                supplied_id = str(action_copy.get("repair_order_id") or "").strip()
                if not supplied_id:
                    return _operator_error(
                        "invalid_input", "repair_order_id cannot be empty."
                    )
                cache_key = _repair_order_number_key(supplied_id)
                resolution = resolution_cache.get(cache_key)
                if resolution is None:
                    resolution = await _resolve_operator_repair_order(
                        client, base, token, supplied_id
                    )
                    if resolution.get("status") != "verified":
                        return resolution
                    resolution_cache[cache_key] = resolution
                    resolution_cache[
                        str(resolution["repair_order_id"]).casefold()
                    ] = resolution
                action_copy["repair_order_id"] = resolution["repair_order_id"]
                pre_snapshots[
                    str(resolution["repair_order_id"])
                ] = resolution["snapshot"]
            if destructive and action_copy["operation"] in BACKEND_EXPLICIT_CONFIRM_OPERATIONS:
                confirmed_arguments = dict(action_copy.get("arguments") or {})
                confirmed_arguments["confirm"] = True
                action_copy["arguments"] = confirmed_arguments
            resolved_actions.append(action_copy)

        research_ro_ids = {
            str(action.get("repair_order_id") or "").strip()
            for action in resolved_actions
            if str(action.get("operation") or "").strip() == "research_ro"
        }
        calibration_mutation_ids = {
            str(action.get("repair_order_id") or "").strip()
            for action in resolved_actions
            if str(action.get("operation") or "").strip()
            in CALIBRATION_MUTATION_OPERATIONS
        }
        mixed_ro_ids = sorted(
            item for item in research_ro_ids & calibration_mutation_ids if item
        )
        if mixed_ro_ids:
            return _operator_error(
                "prerequisite_missing",
                "Calibration mutations and research_ro for the same repair order require "
                "sequential calibration_iq_operator calls in this same user turn: apply and "
                "verify the calibration change first, then use its generated id in research_ro. "
                "Nothing was run.",
                details={"repair_order_ids": mixed_ro_ids},
            )

        for action_copy in resolved_actions:
            if action_copy["operation"] != "research_ro":
                expanded_actions.append(action_copy)
                continue
            if destructive:
                return _operator_error("invalid_operation", "research_ro is not destructive.")
            ro_id = str(action_copy.get("repair_order_id") or "").strip()
            if not ro_id:
                return _operator_error("invalid_input", "research_ro requires repair_order_id.")
            snapshot = dict(pre_snapshots[ro_id])
            try:
                research_actions, report = await _expand_research_action(
                    adas, action_copy, snapshot
                )
            except CalibrationIQOperatorInput as exc:
                return _operator_error("invalid_input", str(exc))
            except Exception as exc:  # noqa: BLE001 - normalize source/index failures
                return _operator_error(
                    "research_source_unavailable",
                    "ADAS SI research could not be completed. Nothing was changed.",
                    retryable=isinstance(exc, OSError),
                    details={"exception": type(exc).__name__},
                )
            expanded_actions.extend(research_actions)
            research_reports.append(report)

        if len(expanded_actions) > OPERATOR_MAX_ACTIONS:
            return _operator_error(
                "invalid_input",
                f"Composite request expanded to {len(expanded_actions)} actions; maximum is {OPERATOR_MAX_ACTIONS}.",
            )
        unsupported = sorted({
            str(action["operation"])
            for action in expanded_actions
            if str(action["operation"]) not in supported_operations
        })
        if unsupported:
            return _operator_error(
                "invalid_operation",
                "Calibration IQ does not advertise these operator operations: "
                + ", ".join(unsupported),
                details={"unsupported_operations": unsupported},
            )

        backend_actions = []
        canonical_occurrences: dict[str, int] = {}
        for index, action in enumerate(expanded_actions):
            operation = str(action["operation"])
            canonical_action = json.dumps(
                action, sort_keys=True, separators=(",", ":"), default=str
            )
            occurrence_ordinal = canonical_occurrences.get(canonical_action, 0)
            canonical_occurrences[canonical_action] = occurrence_ordinal + 1
            idempotency_key, correlation_id = _stable_action_ids(
                context,
                occurrence_ordinal,
                action,
                action_index=index,
            )
            prepared: dict[str, Any] = {
                "idempotency_key": idempotency_key,
                "correlation_id": correlation_id,
                "operation": operation,
                "arguments": dict(action.get("arguments") or {}),
            }
            for key in ("repair_order_id", "target_id", "expected_version"):
                if action.get(key) is not None:
                    prepared[key] = action[key]
            backend_actions.append(prepared)

        principal = str(context.get("user_id") or "local-dev")
        delegation: dict[str, Any] = {
            "on_behalf_of": principal[:160],
            "reason": (
                "X operator request bound to conversation "
                f"{context.get('conversation_id')} and tool call {context.get('tool_call_id')}"
            )[:1000],
            "channel": "x",
        }
        request_body = {
            "actions": backend_actions,
            # research_ro deliberately finishes with update_research. A prior
            # import/update/link failure must stop the batch before that state
            # transition regardless of a model-provided preference.
            "continue_on_error": (
                False
                if research_reports
                else bool(args.get("continue_on_error", False))
            ),
            "delegation": delegation,
        }
        try:
            response = await client.post(
                f"{base}/operator/actions", json=request_body, headers=_auth(token)
            )
        except httpx.HTTPError as exc:
            return _operator_error(
                "temporary_service_failure",
                "Calibration IQ did not return an action receipt; the request may have reached the service, so authoritative state must be reread.",
                retryable=True,
                details={"exception": type(exc).__name__},
                may_have_executed=True,
                indeterminate=True,
            )
        if response.status_code >= 400:
            return _response_error(
                response,
                may_have_executed=response.status_code >= 500,
            )
        try:
            payload = response.json()
        except ValueError:
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned a non-JSON operator result; do not assume anything changed.",
                executed=True,
            )
        if not isinstance(payload, dict):
            return _operator_error(
                "invalid_response", "Calibration IQ returned an invalid operator result.", executed=True
            )

        receipts = [
            _map_document_urls(item)
            for item in (payload.get("receipts") or [])
            if isinstance(item, dict)
        ]
        affected_ro_ids = {
            str(action.get("repair_order_id") or "").strip()
            for action in backend_actions if action.get("repair_order_id")
        }
        affected_ro_ids.update(
            str(receipt.get("repair_order_id") or "").strip()
            for receipt in receipts if receipt.get("repair_order_id")
        )
        final_snapshots: dict[str, Any] = {}
        snapshots_verified = True
        for ro_id in sorted(item for item in affected_ro_ids if item):
            reread = await _operator_snapshot_request(client, base, token, ro_id)
            final_snapshots[ro_id] = reread
            if reread.get("status") != "verified":
                snapshots_verified = False

    completed_research_verified = True
    for report in research_reports:
        if not (
            report.get("research_complete_requested")
            and report.get("research_complete_eligible")
        ):
            continue
        reread = final_snapshots.get(str(report.get("repair_order_id"))) or {}
        final_snapshot = reread.get("snapshot")
        state_verified = _research_state(final_snapshot) == "research_complete"
        final_required_calibrations = [
            {"id": spec.get("id") or None, "label": spec.get("label")}
            for spec in (
                _research_calibrations(final_snapshot, {})
                if isinstance(final_snapshot, dict)
                else []
            )
        ]
        report["final_required_calibrations"] = final_required_calibrations
        evidence_verified, evidence_missing = _verify_persisted_research_evidence(
            final_snapshot,
            final_required_calibrations,
        )
        completion_verified = state_verified and evidence_verified
        report["research_state_verified"] = state_verified
        report["persisted_evidence_verified"] = evidence_verified
        report["persisted_evidence_missing"] = evidence_missing
        report["research_complete_verified"] = completion_verified
        if report.get("research_complete_was_already_set"):
            report["research_complete_already_verified"] = completion_verified
        if not completion_verified:
            completed_research_verified = False
            known_missing = {
                _research_requirement_key(item)
                for item in (report.get("missing_documents") or [])
                if isinstance(item, dict)
            }
            report.setdefault("missing_documents", []).extend(
                item for item in evidence_missing
                if _research_requirement_key(item) not in known_missing
            )

    research_request_fulfilled = not any(
        report.get("completion_withheld") is True for report in research_reports
    )
    missing_documentation = [
        str(item.get("calibration") or item.get("calibration_id") or "required calibration")
        for report in research_reports
        for item in (report.get("missing_documents") or [])
        if isinstance(item, dict)
    ]

    requested_count = int(payload.get("requested_count") or len(backend_actions))
    processed_count = int(payload.get("processed_count") or len(receipts))
    receipt_truth = bool(receipts) and len(receipts) == processed_count and all(
        _receipt_verified(receipt) for receipt in receipts
    )
    success = bool(
        payload.get("success") is True
        and payload.get("partial") is not True
        and requested_count == len(backend_actions)
        and processed_count == requested_count
        and receipt_truth
        and snapshots_verified
        and completed_research_verified
        and research_request_fulfilled
    )
    executed = processed_count > 0 or bool(receipts)
    first_error = next(
        (
            receipt.get("error") for receipt in receipts
            if isinstance(receipt.get("error"), dict)
        ),
        None,
    )
    verified_success_count = sum(
        1 for receipt in receipts if _receipt_verified(receipt)
    )
    partial = bool(
        verified_success_count > 0
        and (
            payload.get("partial") is True
            or verified_success_count < requested_count
            or processed_count < requested_count
        )
    )
    result = {
        "status": "success" if success else ("partial_success" if partial else "failed"),
        "executed": executed,
        "success": success,
        "verified": success,
        "partial": partial,
        "requested_count": requested_count,
        "processed_count": processed_count,
        "stopped_on_error": bool(payload.get("stopped_on_error")),
        "receipts": receipts,
        "final_snapshots": final_snapshots,
        "research": research_reports,
        "missing_documentation": missing_documentation,
        "message": (
            "Calibration IQ confirmed every action and the authoritative final state."
            if success
            else "Calibration IQ did not verify the complete requested outcome. Review the failed receipt and final snapshot."
        ),
    }
    if first_error:
        result["error"] = first_error
    return result


def _validated_operator_content_type(raw: Any, *, resource_kind: str) -> Optional[str]:
    value = str(raw or "").strip()
    if not value or "\r" in value or "\n" in value or len(value) > 200:
        return None
    base_type = value.split(";", 1)[0].strip().casefold()
    if (
        not _CONTENT_TYPE_RE.fullmatch(base_type)
        or base_type in _UNSAFE_BROWSER_CONTENT_TYPES
    ):
        return None
    if resource_kind == "photo" and not base_type.startswith("image/"):
        return None
    if resource_kind == "document" and base_type == "application/json":
        return None
    return base_type


async def _bounded_operator_error(response: httpx.Response) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_OPERATOR_ERROR_BYTES:
            return _operator_error(
                "invalid_response",
                "Calibration IQ returned an oversized operator error response.",
                http_status=response.status_code,
            )
        chunks.append(chunk)
    buffered = httpx.Response(
        response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
    )
    return _response_error(buffered)


async def _fetch_verified_operator_file(
    settings,
    endpoint: str,
    *,
    resource_kind: str,
    resource_label: str,
    max_bytes: int,
    too_large_code: str,
    fallback_filename: str,
) -> dict[str, Any]:
    """Fetch one bounded operator file and bind its bytes to backend integrity proof."""
    credentials = _operator_credentials(settings)
    if isinstance(credentials, dict):
        return credentials
    _, token = credentials
    base = await resolve_base(settings)
    try:
        async with httpx.AsyncClient(
            timeout=OPERATOR_TIMEOUT, trust_env=False, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                f"{base}{endpoint}",
                headers={**_auth(token), "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code >= 400:
                    return await _bounded_operator_error(response)
                content_type = _validated_operator_content_type(
                    response.headers.get("content-type"), resource_kind=resource_kind
                )
                if content_type is None:
                    return _operator_error(
                        "invalid_response",
                        f"Calibration IQ returned an invalid {resource_label} content type.",
                    )
                advertised = response.headers.get("content-length")
                verified_length = response.headers.get("x-content-length-verified")
                advertised_digest = str(
                    response.headers.get("x-content-sha256") or ""
                ).strip().casefold()
                try:
                    advertised_size = int(str(advertised))
                    verified_size = int(str(verified_length))
                except (TypeError, ValueError):
                    return _operator_error(
                        "invalid_response",
                        f"Calibration IQ did not provide valid verified {resource_label} length headers.",
                    )
                if advertised_size != verified_size or advertised_size < 0:
                    return _operator_error(
                        "invalid_response",
                        f"Calibration IQ {resource_label} length proof did not match its response length.",
                    )
                if advertised_size > max_bytes:
                    return _operator_error(
                        too_large_code,
                        f"Calibration IQ {resource_label} exceeds the {max_bytes}-byte proxy limit.",
                    )
                if not re.fullmatch(r"[0-9a-f]{64}", advertised_digest):
                    return _operator_error(
                        "invalid_response",
                        f"Calibration IQ did not provide a valid {resource_label} SHA-256 proof.",
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return _operator_error(
                            too_large_code,
                            f"Calibration IQ {resource_label} exceeds the {max_bytes}-byte proxy limit.",
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                actual_digest = hashlib.sha256(data).hexdigest()
                if len(data) != verified_size or actual_digest != advertised_digest:
                    return _operator_error(
                        "invalid_response",
                        f"Calibration IQ {resource_label} failed integrity verification.",
                        details={
                            "length_matches": len(data) == verified_size,
                            "sha256_matches": actual_digest == advertised_digest,
                        },
                    )
                disposition = str(response.headers.get("content-disposition") or "")
    except httpx.HTTPError as exc:
        return _operator_error(
            "temporary_service_failure",
            f"Calibration IQ {resource_label} download is unavailable.",
            retryable=True,
            details={"exception": type(exc).__name__},
        )
    if "\r" in disposition or "\n" in disposition or len(disposition) > 1000:
        disposition = ""
    return {
        "status": "verified",
        "executed": False,
        "success": True,
        "verified": True,
        "content": data,
        "content_type": content_type,
        "content_disposition": disposition or f'attachment; filename="{fallback_filename}"',
        "content_length": len(data),
        "sha256": actual_digest,
    }


async def fetch_operator_document(settings, document_id: str) -> dict[str, Any]:
    """Fetch exactly one verified managed document without exposing the service token."""
    ident = str(document_id or "").strip()
    if not _OPERATOR_RESOURCE_ID_RE.fullmatch(ident):
        return _operator_error("invalid_input", "Invalid Calibration IQ document id.")
    result = await _fetch_verified_operator_file(
        settings,
        f"/operator/documents/{quote(ident, safe='')}/download",
        resource_kind="document",
        resource_label="managed document",
        max_bytes=MAX_OPERATOR_DOCUMENT_BYTES,
        too_large_code="document_too_large",
        fallback_filename=f"calibration-iq-{ident}",
    )
    if result.get("status") == "verified":
        result["document_id"] = ident
    return result


async def fetch_operator_workspace_file(
    settings, repair_order_id: str, path: str
) -> dict[str, Any]:
    """Fetch one path-confined file from an RO's managed case workspace."""
    ro_id = str(repair_order_id or "").strip()
    if not _OPERATOR_RESOURCE_ID_RE.fullmatch(ro_id):
        return _operator_error("invalid_input", "Invalid Calibration IQ repair order id.")
    try:
        relative_path = _workspace_relative_path(path, field="path")
    except CalibrationIQOperatorInput as exc:
        return _operator_error("invalid_input", str(exc))
    if not relative_path:
        return _operator_error("invalid_input", "A managed workspace file path is required.")
    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+", "_", PurePosixPath(relative_path).name
    ).strip("._") or "workspace-file"
    result = await _fetch_verified_operator_file(
        settings,
        "/operator/ros/"
        f"{quote(ro_id, safe='')}/files?{urlencode({'path': relative_path})}",
        resource_kind="workspace",
        resource_label="managed workspace file",
        max_bytes=MAX_OPERATOR_WORKSPACE_FILE_BYTES,
        too_large_code="workspace_file_too_large",
        fallback_filename=safe_name,
    )
    if result.get("status") == "verified":
        result.update({"repair_order_id": ro_id, "path": relative_path})
    return result


async def fetch_operator_photo(
    settings, photo_id: str, variant: str
) -> dict[str, Any]:
    """Fetch a verified photo original or thumbnail from Calibration IQ."""
    ident = str(photo_id or "").strip()
    normalized_variant = str(variant or "").strip().casefold()
    if not _OPERATOR_RESOURCE_ID_RE.fullmatch(ident):
        return _operator_error("invalid_input", "Invalid Calibration IQ photo id.")
    if normalized_variant not in {"download", "thumbnail"}:
        return _operator_error(
            "invalid_input", "Photo variant must be download or thumbnail."
        )
    result = await _fetch_verified_operator_file(
        settings,
        f"/operator/photos/{quote(ident, safe='')}/{normalized_variant}",
        resource_kind="photo",
        resource_label=f"photo {normalized_variant}",
        max_bytes=MAX_OPERATOR_PHOTO_BYTES,
        too_large_code="photo_too_large",
        fallback_filename=f"calibration-iq-photo-{ident}",
    )
    if result.get("status") == "verified":
        result.update({"photo_id": ident, "variant": normalized_variant})
    return result
