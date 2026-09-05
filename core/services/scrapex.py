"""Bounded, loopback-only client for the local ScrapeX ADAS Map worker.

The model chooses these operations through structured tool arguments.  This
module deliberately contains no language/keyword router and accepts neither a
URL nor credentials from tool input.  ScrapeX remains the owner of browser
state and CIQ reconciliation; X Omni only invokes its published loopback API
and reports the returned state without turning "started" into "completed".
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8125"
BASE_URL_ENV = "XOMNI_SCRAPEX_BASE_URL"
DEFAULT_PROJECT_PATH = Path(r"X:\ScrapeX")
STATUS_TIMEOUT = 5.0
READ_TIMEOUT = 20.0
OPERATOR_TIMEOUT = 180.0
NATIVE_START_TIMEOUT_S = 90.0
NATIVE_START_POLL_INTERVAL_S = 1.0
MAX_NATIVE_START_LOG_CHARS = 4000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_BATCH_ID_CHARS = 80
MAX_RO_CHARS = 80
MAX_NAME_CHARS = 180
MAX_SHOP_CHARS = 180
MAX_PHASE_CHARS = 40
_INVOCATION_CONTEXT_KEY = "__xomni_invocation"
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|secret|access[_-]?token|refresh[_-]?token|"
    r"service[_-]?token|api[_-]?key)\s*[=:]\s*[^\s,&;]+"
)

READ_ACTIONS = frozenset(
    {
        "list_batches",
        "batch_summary",
        "batch_exceptions",
        "batch_item",
        "preview_ciq_queue",
    }
)
ADAS_MAP_ACTIONS = frozenset(
    {
        "open_authentication",
        "create_exact_batch",
        "create_phase_batch",
        "process_one",
        "start_batch",
        "pause_batch",
    }
)
SOURCE_SCOPES = frozenset({"active", "all", "terminal"})

# --- ScrapeX Navigator: bounded browser observation/action turns -----------
#
# Unlike ADAS Map (one deterministic worker call per action), a Navigator
# task is driven turn-by-turn by this process's own model loop, one browser
# action per model turn, against ScrapeX's session/graph/action-budget/
# verification machinery. This client never authors a CSS selector or role
# guess -- every click/fill/press targets an opaque ``ref`` copied verbatim
# from the most recent observation's element list, exactly like batch_id
# for ADAS Map. Staged behind Settings.alldata_navigator_enabled; see the
# ScrapeX Navigator architecture plan.
NAVIGATOR_PROVIDERS = frozenset({"alldata"})
NAVIGATOR_META_ACTIONS = frozenset({"create_task", "observe", "verify", "get_evidence"})
NAVIGATOR_ACT_KINDS = frozenset({"click", "fill", "press", "back", "open", "extract", "done"})
NAVIGATOR_ACTIONS = NAVIGATOR_META_ACTIONS | NAVIGATOR_ACT_KINDS
MAX_TASK_ID_CHARS = 80
MAX_TOPIC_CHARS = 400
MAX_TARGET_FIELD_CHARS = 120
MAX_VIN_CHARS = 32
MAX_REF_CHARS = 40
MAX_FILL_TEXT_CHARS = 400
MAX_KEY_CHARS = 40
MAX_NAV_URL_CHARS = 2048
MAX_NAV_SCREENSHOT_BYTES = 4 * 1024 * 1024


SCRAPEX_STATUS_SCHEMA: dict[str, Any] = {
    "description": (
        "Safe, non-mutating provider preflight. Check the local ScrapeX ADAS Map "
        "worker, Calibration IQ dependency, and managed-browser authentication "
        "state. It may run before acquisition or provider setup and opens nothing."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

SCRAPEX_READ_SCHEMA: dict[str, Any] = {
    "description": (
        "Read ScrapeX batches, exact-RO ADAS Map evidence, exceptions, or a "
        "non-mutating CIQ queue preview. For a create result, batch_id is exactly "
        "result.data.id, never evidence_id. Existing-evidence reads begin with "
        "list_batches when no id is known; new acquisition uses "
        "scrapex_adas_map.create_exact_batch instead."
    ),
    "parameters": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "list_batches",
                        "description": (
                            "Discover stored ScrapeX batches containing existing ADAS "
                            "Map evidence when no exact batch id is known."
                        ),
                    }
                },
                "required": ["action"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "const": "batch_summary"},
                    "batch_id": {
                        "type": "string",
                        "maxLength": MAX_BATCH_ID_CHARS,
                        "description": (
                            "Exact opaque id copied verbatim from a returned batch "
                            "object. For create results copy result.data.id; never use "
                            "evidence_id, a placeholder, or a derived or guessed value."
                        ),
                    },
                },
                "required": ["action", "batch_id"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "const": "batch_exceptions"},
                    "batch_id": {
                        "type": "string",
                        "maxLength": MAX_BATCH_ID_CHARS,
                        "description": (
                            "Exact opaque id copied verbatim from a returned batch "
                            "object. For create results copy result.data.id; never use "
                            "evidence_id, a placeholder, or a derived or guessed value."
                        ),
                    },
                },
                "required": ["action", "batch_id"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "const": "batch_item"},
                    "batch_id": {
                        "type": "string",
                        "maxLength": MAX_BATCH_ID_CHARS,
                        "description": (
                            "Exact opaque id copied verbatim from a returned batch "
                            "object. For create results copy result.data.id; never use "
                            "evidence_id, a placeholder, or a derived or guessed value."
                        ),
                    },
                    "ro_number": {"type": "string", "maxLength": MAX_RO_CHARS},
                },
                "required": ["action", "batch_id", "ro_number"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "preview_ciq_queue",
                        "description": (
                            "Non-mutating view of Calibration IQ candidate work selected "
                            "by phases, shop, and source scope. This is not stored ADAS Map "
                            "evidence and does not provide an existing ScrapeX batch or "
                            "batch item; list_batches discovers existing evidence."
                        ),
                    },
                    "phases": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": MAX_PHASE_CHARS},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "shop": {"type": "string", "maxLength": MAX_SHOP_CHARS},
                    "source_scope": {
                        "type": "string",
                        "enum": sorted(SOURCE_SCOPES),
                    },
                },
                "required": ["action", "phases"],
            },
        ],
    },
}

SCRAPEX_ADAS_MAP_SCHEMA: dict[str, Any] = {
    "description": (
        "Run bounded ScrapeX ADAS Map actions. process_one requires an observed exact "
        "batch_id. After create_exact_batch or create_phase_batch, copy "
        "result.data.id exactly; never copy evidence_id. open_authentication is a "
        "parameterless browser-opening human/provider handoff used after status reports "
        "authentication_required or when the user explicitly requests provider setup. "
        "Queued or started is not completed."
    ),
    "parameters": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "open_authentication",
                        "description": "Open the managed provider sign-in handoff.",
                    },
                },
                "required": ["action"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "create_exact_batch",
                        "description": "Create one bounded batch for exact RO numbers.",
                    },
                    "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
                    "ro_numbers": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": MAX_RO_CHARS},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "source_scope": {
                        "type": "string",
                        "enum": sorted(SOURCE_SCOPES),
                    },
                },
                "required": ["action", "ro_numbers"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "create_phase_batch",
                        "description": "Create one bounded batch for exact CIQ phases.",
                    },
                    "name": {"type": "string", "maxLength": MAX_NAME_CHARS},
                    "phases": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": MAX_PHASE_CHARS},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "shop": {"type": "string", "maxLength": MAX_SHOP_CHARS},
                    "source_scope": {
                        "type": "string",
                        "enum": sorted(SOURCE_SCOPES),
                    },
                },
                "required": ["action", "phases"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "process_one",
                        "description": "Process one RO in an observed exact batch.",
                    },
                    "batch_id": {
                        "type": "string",
                        "maxLength": MAX_BATCH_ID_CHARS,
                        "description": (
                            "Exact id copied verbatim from a verified same-turn "
                            "ScrapeX result; never guess or use evidence_id."
                        ),
                    },
                    "ro_number": {"type": "string", "maxLength": MAX_RO_CHARS},
                },
                "required": ["action", "batch_id", "ro_number"],
            },
            *[
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "const": action,
                            "description": f"{label} an observed exact batch.",
                        },
                        "batch_id": {
                            "type": "string",
                            "maxLength": MAX_BATCH_ID_CHARS,
                            "description": (
                                "Exact id copied verbatim from a verified same-turn "
                                "ScrapeX result; never guess or use evidence_id."
                            ),
                        },
                    },
                    "required": ["action", "batch_id"],
                }
                for action, label in (
                    ("start_batch", "Start"),
                    ("pause_batch", "Pause"),
                )
            ],
        ],
    },
}

SCRAPEX_START_NATIVE_SCHEMA: dict[str, Any] = {
    "description": (
        "Start ScrapeX's local server if unreachable, then verify it "
        "answers. Safe even if already running."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

_NAVIGATOR_TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "The vehicle/subject this task must stay bound to. Every verification "
        "check re-derives its own proof from the live page; nothing here is "
        "trusted as already selected."
    ),
    "properties": {
        "year": {"type": "integer", "minimum": 1900, "maximum": 2100},
        "make": {"type": "string", "maxLength": MAX_TARGET_FIELD_CHARS},
        "model": {"type": "string", "maxLength": MAX_TARGET_FIELD_CHARS},
        "trim": {"type": "string", "maxLength": MAX_TARGET_FIELD_CHARS},
        "vin": {"type": "string", "maxLength": MAX_VIN_CHARS},
    },
}
_NAVIGATOR_TASK_ID_PROPERTY: dict[str, Any] = {
    "type": "string",
    "maxLength": MAX_TASK_ID_CHARS,
    "description": (
        "Exact opaque id copied verbatim from a verified same-turn "
        "scrapex_navigator create_task or observe/act result; never guess."
    ),
}
_NAVIGATOR_REF_PROPERTY: dict[str, Any] = {
    "type": "string",
    "maxLength": MAX_REF_CHARS,
    "description": (
        "Exact opaque element ref copied verbatim from the most recent "
        "observe/act result's elements list -- never a role, name, or CSS "
        "selector the model authors itself. A ref from an older observation "
        "may no longer resolve; re-observe if so."
    ),
}

SCRAPEX_NAVIGATOR_SCHEMA: dict[str, Any] = {
    "description": (
        "Drive one bounded ScrapeX Navigator browser turn for dynamic "
        "service-information sites (e.g. ALLDATA) whose procedure content is "
        "many clicks deep and varies by vehicle. create_task starts a new "
        "session-scoped task; every other action requires its exact task_id. "
        "observe/act both return the current page's element list -- always "
        "act using a ref from the most recent one, never an older observation. "
        "click/fill/press/back/open/extract are ordinary navigation steps; "
        "done ends the task and verify computes the deterministic proof of "
        "whether real, on-topic procedure content was actually reached and "
        "extracted for the requested target. A page changing after an action "
        "is expected -- always re-observe before acting again."
    ),
    "parameters": {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string", "const": "create_task"},
                    "provider": {
                        "type": "string",
                        "enum": sorted(NAVIGATOR_PROVIDERS),
                    },
                    "target": _NAVIGATOR_TARGET_SCHEMA,
                    "topic": {
                        "type": "string",
                        "maxLength": MAX_TOPIC_CHARS,
                        "description": "The calibration/procedure topic being researched.",
                    },
                    "action_budget": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 80,
                        "description": "Optional cap on browser actions for this task; ScrapeX defaults it.",
                    },
                },
                "required": ["action", "provider", "target", "topic"],
            },
            *[
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string", "const": meta_action},
                        "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                    },
                    "required": ["action", "task_id"],
                }
                for meta_action in ("observe", "verify", "get_evidence")
            ],
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "click",
                        "description": "Click one element by its observed ref.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                    "ref": _NAVIGATOR_REF_PROPERTY,
                },
                "required": ["action", "task_id", "ref"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "fill",
                        "description": "Type text into one observed field by its ref.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                    "ref": _NAVIGATOR_REF_PROPERTY,
                    "text": {"type": "string", "maxLength": MAX_FILL_TEXT_CHARS},
                },
                "required": ["action", "task_id", "ref", "text"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "press",
                        "description": "Press one key while one observed element by its ref is focused.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                    "ref": _NAVIGATOR_REF_PROPERTY,
                    "key": {"type": "string", "maxLength": MAX_KEY_CHARS},
                },
                "required": ["action", "task_id", "ref", "key"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "back",
                        "description": "Navigate back one page.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                },
                "required": ["action", "task_id"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "open",
                        "description": "Navigate directly to a URL within this provider's own domain.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                    "url": {"type": "string", "maxLength": MAX_NAV_URL_CHARS},
                },
                "required": ["action", "task_id", "url"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "extract",
                        "description": (
                            "Mark the current page as the procedure leaf whose content "
                            "should be captured as evidence. Only call this once the "
                            "actual procedure content -- not a menu or search-results "
                            "listing -- is on screen."
                        ),
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                },
                "required": ["action", "task_id"],
            },
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "const": "done",
                        "description": "End this task's browser turns before calling verify.",
                    },
                    "task_id": _NAVIGATOR_TASK_ID_PROPERTY,
                },
                "required": ["action", "task_id"],
            },
        ],
    },
}

SCRAPEX_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "scrapex_status": SCRAPEX_STATUS_SCHEMA,
    "scrapex_read": SCRAPEX_READ_SCHEMA,
    "scrapex_adas_map": SCRAPEX_ADAS_MAP_SCHEMA,
    "scrapex_start_native": SCRAPEX_START_NATIVE_SCHEMA,
    "scrapex_navigator": SCRAPEX_NAVIGATOR_SCHEMA,
}


class ScrapeXInput(ValueError):
    """Structured tool input is invalid."""


class ScrapeXConfiguration(ValueError):
    """The configured service boundary is unsafe or invalid."""


class ScrapeXTransport(RuntimeError):
    def __init__(self, code: str, message: str, *, indeterminate: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.indeterminate = indeterminate


class ScrapeXRemote(RuntimeError):
    def __init__(self, status_code: int, detail: Any, *, may_mutate: bool = False):
        super().__init__(f"ScrapeX returned HTTP {status_code}.")
        self.status_code = status_code
        self.detail = detail
        self.may_mutate = may_mutate


class ScrapeXContract(RuntimeError):
    """A successful HTTP response did not prove the requested resource/action."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _clean_args(args: dict[str, Any] | None) -> dict[str, Any]:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ScrapeXInput("Tool arguments must be an object.")
    return {key: value for key, value in args.items() if key != _INVOCATION_CONTEXT_KEY}


def _expect_keys(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(args) - allowed)
    if unknown:
        raise ScrapeXInput(f"Unsupported argument(s): {', '.join(unknown)}.")


def _text(
    value: Any,
    field: str,
    *,
    maximum: int,
    required: bool = True,
) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ScrapeXInput(f"{field} must be a string.")
    result = value.strip()
    if not result and required:
        raise ScrapeXInput(f"{field} is required.")
    if not result:
        return None
    if len(result) > maximum or any(ord(ch) < 32 or ord(ch) == 127 for ch in result):
        raise ScrapeXInput(f"{field} is invalid or too long.")
    return result


def _batch_id(args: dict[str, Any]) -> str:
    value = _text(args.get("batch_id"), "batch_id", maximum=MAX_BATCH_ID_CHARS)
    assert value is not None
    if not _RESOURCE_ID_RE.fullmatch(value):
        raise ScrapeXInput("batch_id must be a bounded ScrapeX identifier.")
    return value


def _ro_number(value: Any, field: str = "ro_number") -> str:
    result = _text(value, field, maximum=MAX_RO_CHARS)
    assert result is not None
    return result


def _string_list(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    maximum_chars: int,
) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
        raise ScrapeXInput(f"{field} must contain 1 to {maximum_items} values.")
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _text(raw, field, maximum=maximum_chars)
        assert item is not None
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            output.append(item)
    return output


def _source_scope(args: dict[str, Any], default: str) -> str:
    raw = args.get("source_scope", default)
    if not isinstance(raw, str) or raw not in SOURCE_SCOPES:
        raise ScrapeXInput("source_scope must be active, all, or terminal.")
    return raw


def _base_url(settings: Any) -> str:
    configured = getattr(settings, "scrapex_base_url", None)
    raw = str(configured or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ScrapeXConfiguration("ScrapeX base URL has an invalid port.") from exc
    if parsed.scheme != "http" or not parsed.hostname:
        raise ScrapeXConfiguration("ScrapeX must use a literal loopback HTTP URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ScrapeXConfiguration("ScrapeX base URL cannot contain credentials or URL parameters.")
    if parsed.path not in {"", "/"}:
        raise ScrapeXConfiguration("ScrapeX base URL cannot contain an API path.")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ScrapeXConfiguration("ScrapeX host must be a literal loopback address.") from exc
    if not address.is_loopback:
        raise ScrapeXConfiguration("ScrapeX is restricted to the local loopback interface.")
    if port is None:
        raise ScrapeXConfiguration("ScrapeX base URL must include its explicit loopback port.")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return f"http://{host}:{port}"


def _sensitive_key(value: Any) -> bool:
    folded = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return folded in {
        "password",
        "secret",
        "accesstoken",
        "refreshtoken",
        "servicetoken",
        "authorization",
        "cookie",
        "setcookie",
        "apikey",
        "credential",
        "credentials",
    }


def _safe_text(value: str) -> str:
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _sensitive_key(key)
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) <= MAX_RESPONSE_BYTES and (
            (stripped.startswith("{") and stripped.endswith("}"))
            or (stripped.startswith("[") and stripped.endswith("]"))
        ):
            try:
                embedded = json.loads(stripped)
            except (TypeError, ValueError):
                pass
            else:
                if isinstance(embedded, (dict, list)):
                    return json.dumps(
                        _sanitize(embedded),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
        return _safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(str(value))


def _decode_response(response: httpx.Response, *, may_mutate: bool) -> Any:
    ambiguous_success = may_mutate and 200 <= response.status_code < 300
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise ScrapeXTransport(
            "response_too_large",
            "ScrapeX returned too much data.",
            indeterminate=ambiguous_success,
        )
    if not content:
        return {}
    try:
        return _sanitize(response.json())
    except (ValueError, json.JSONDecodeError) as exc:
        raise ScrapeXTransport(
            "invalid_response",
            "ScrapeX returned invalid JSON.",
            indeterminate=ambiguous_success,
        ) from exc


async def _request(
    settings: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float,
    may_mutate: bool,
) -> Any:
    base_url = _base_url(settings)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "X-Omni/ScrapeX-adapter"},
        ) as client:
            response = await client.request(method, path, json=body)
    except httpx.TimeoutException as exc:
        raise ScrapeXTransport(
            "timeout",
            "ScrapeX did not respond before the operation timeout.",
            indeterminate=may_mutate,
        ) from exc
    except httpx.HTTPError as exc:
        raise ScrapeXTransport(
            "unavailable",
            "ScrapeX is unavailable on its local loopback endpoint.",
            indeterminate=may_mutate,
        ) from exc
    payload = _decode_response(response, may_mutate=may_mutate)
    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        raise ScrapeXRemote(
            response.status_code,
            detail,
            may_mutate=may_mutate,
        )
    return payload


def _failure(action: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "service": "ScrapeX",
        "action": action,
        "status": code,
        "success": False,
        "executed": False,
        "verified": False,
        "error": {"code": code, "message": _safe_text(message)},
    }
    result.update(_sanitize(extra))
    return result


def _input_failure(action: str, exc: Exception) -> dict[str, Any]:
    return _failure(action, "invalid_request", str(exc))


def _detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, sort_keys=True)
    except (TypeError, ValueError):
        return str(detail)


def _remote_failure(action: str, exc: ScrapeXRemote) -> dict[str, Any]:
    detail = _sanitize(exc.detail)
    lowered = _detail_text(detail).casefold()
    if exc.status_code == 409 and "adas map" in lowered and (
        "not authenticated" in lowered or "login" in lowered
    ):
        return _authentication_required(action, {"detail": detail}, executed=False)
    # A server/proxy error can be returned after the remote side committed a
    # POST.  The adapter has no request-id lookup that could safely disprove
    # execution, so fail closed and forbid an automatic retry.  Validation,
    # auth, not-found, conflict, and rate-limit responses are definitive
    # rejections and remain ordinary failures below.
    definitive_rejections = {400, 401, 403, 404, 409, 422, 429}
    if exc.may_mutate and exc.status_code not in definitive_rejections:
        return _failure(
            action,
            "indeterminate",
            f"ScrapeX returned HTTP {exc.status_code} after a mutation request; "
            "execution could not be disproved.",
            http_status=exc.status_code,
            detail=detail,
            may_have_executed=True,
            indeterminate=True,
            retryable=False,
        )
    if exc.status_code == 404:
        code = "not_found"
    elif exc.status_code in {400, 422}:
        code = "invalid_request"
    elif exc.status_code == 409:
        code = "conflict"
    elif exc.status_code in {502, 503, 504}:
        code = "dependency_unavailable"
    else:
        code = "service_error"
    return _failure(
        action,
        code,
        f"ScrapeX returned HTTP {exc.status_code}.",
        http_status=exc.status_code,
        detail=detail,
    )


def _transport_failure(action: str, exc: ScrapeXTransport) -> dict[str, Any]:
    status = "indeterminate" if exc.indeterminate else exc.code
    result = _failure(
        action,
        status,
        exc.message,
        may_have_executed=exc.indeterminate,
        indeterminate=exc.indeterminate,
        retryable=(
            not exc.indeterminate and exc.code in {"timeout", "unavailable"}
        ),
    )
    result["error"]["transport_code"] = exc.code
    return result


def _configuration_failure(action: str, exc: ScrapeXConfiguration) -> dict[str, Any]:
    return _failure(action, "configuration_error", str(exc))


def _contract_failure(
    action: str,
    exc: ScrapeXContract,
    *,
    may_mutate: bool,
) -> dict[str, Any]:
    if may_mutate:
        result = _failure(
            action,
            "indeterminate",
            "ScrapeX returned a 2xx response that did not prove the requested "
            "resource and operation contract.",
            may_have_executed=True,
            indeterminate=True,
            retryable=False,
        )
    else:
        result = _failure(
            action,
            "invalid_response",
            "ScrapeX returned data for a different or malformed resource.",
            may_have_executed=False,
            indeterminate=False,
            retryable=False,
        )
    result["error"]["contract_code"] = exc.code
    result["error"]["contract_message"] = _safe_text(exc.message)
    return result


def _success(
    action: str,
    data: Any,
    *,
    status: str = "verified",
    executed: bool = True,
    verified: bool = True,
    work_complete: bool | None = None,
    success: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "service": "ScrapeX",
        "action": action,
        "status": status,
        "success": success,
        "executed": executed,
        "verified": verified,
        "data": _sanitize(data),
    }
    if work_complete is not None:
        result["work_complete"] = work_complete
    return result


def _authentication_required(
    action: str,
    authentication: Any,
    *,
    executed: bool,
) -> dict[str, Any]:
    return {
        "service": "ScrapeX",
        "action": action,
        "status": "authentication_required",
        "success": False,
        "executed": executed,
        "verified": False,
        "work_complete": False,
        "authentication_required": True,
        "requires_human": True,
        "authentication": _sanitize(authentication),
        "message": (
            "ADAS Map needs interactive sign-in in ScrapeX's managed work Chrome "
            "window. No credential is requested or returned through the model."
        ),
    }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _provenance(item: dict[str, Any]) -> dict[str, Any]:
    """Expose the canonical evidence fields without reinterpreting their truth."""
    raw_result = _json_object(item.get("adas_map_raw_result_json"))
    reconciliation = item.get("ciq_reconciliation")
    if not isinstance(reconciliation, dict):
        reconciliation = _json_object(item.get("ciq_reconciliation_json"))
    requirements = item.get("adas_map_requirements")
    if not isinstance(requirements, list):
        requirements = _json_list(item.get("adas_map_requirements_json"))
    return _sanitize(
        {
            "contract_version": item.get("adas_map_contract_version"),
            "state": item.get("adas_map_state"),
            "requirements_proven": bool(item.get("adas_map_requirements_proven")),
            "inspection_id": item.get("adas_map_inspection_id"),
            "source_url": item.get("adas_map_source_url"),
            "checked_at": item.get("adas_map_checked_at"),
            "requirements": requirements,
            "raw_result": raw_result,
            "ciq_reconciliation_state": item.get("ciq_reconciliation_state"),
            "ciq_reconciliation": reconciliation,
        }
    )


def _contract_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScrapeXContract("malformed_payload", f"{label} must be an object.")
    return value


def _contract_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ScrapeXContract("malformed_identifier", f"{label} must be a string.")
    result = value.strip()
    if not _RESOURCE_ID_RE.fullmatch(result):
        raise ScrapeXContract(
            "malformed_identifier",
            f"{label} is not a bounded ScrapeX identifier.",
        )
    return result


def _contract_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ScrapeXContract("malformed_text", f"{label} must be a string.")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in result)
    ):
        raise ScrapeXContract("malformed_text", f"{label} is empty or invalid.")
    return result


def _contract_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ScrapeXContract("malformed_list", f"{label} must be a non-empty list.")
    output: list[str] = []
    for item in value:
        output.append(_contract_text(item, label, maximum=MAX_RO_CHARS))
    return output


def _returned_batch(payload: Any, *, expected_batch_id: str | None = None) -> tuple[dict[str, Any], str]:
    batch = _contract_mapping(payload, "batch")
    batch_id = _contract_identifier(batch.get("id"), "batch.id")
    if expected_batch_id is not None and batch_id != expected_batch_id:
        raise ScrapeXContract(
            "batch_mismatch",
            "ScrapeX returned a different batch than the one requested.",
        )
    return batch, batch_id


def _returned_item(
    payload: Any,
    *,
    expected_batch_id: str,
    expected_ro_number: str | None = None,
) -> dict[str, Any]:
    item = _contract_mapping(payload, "item")
    _contract_identifier(item.get("id"), "item.id")
    returned_batch_id = _contract_identifier(item.get("batch_id"), "item.batch_id")
    if returned_batch_id != expected_batch_id:
        raise ScrapeXContract(
            "item_batch_mismatch",
            "ScrapeX returned an item from a different batch.",
        )
    if expected_ro_number is not None:
        returned_ro = _contract_text(
            item.get("ro_number"), "item.ro_number", maximum=MAX_RO_CHARS
        )
        if returned_ro != expected_ro_number:
            raise ScrapeXContract(
                "item_ro_mismatch",
                "ScrapeX returned an item for a different repair order.",
            )
    return item


def _returned_batch_items(batch: dict[str, Any], batch_id: str) -> list[dict[str, Any]]:
    values = batch.get("items")
    if not isinstance(values, list) or not values:
        raise ScrapeXContract(
            "batch_items_missing",
            "ScrapeX did not return the created batch items.",
        )
    return [
        _returned_item(value, expected_batch_id=batch_id)
        for value in values
    ]


def _returned_readiness(payload: Any) -> dict[str, Any]:
    readiness = _contract_mapping(payload, "readiness")
    if type(readiness.get("ready")) is not bool:
        raise ScrapeXContract(
            "readiness_state_missing",
            "ScrapeX omitted the authoritative readiness state.",
        )
    total = readiness.get("total")
    if total is not None and (
        isinstance(total, bool) or not isinstance(total, int) or total < 0
    ):
        raise ScrapeXContract(
            "readiness_total_invalid",
            "ScrapeX returned an invalid readiness total.",
        )
    return readiness


def _validate_exact_batch_contract(
    payload: Any,
    *,
    requested_ro_numbers: list[str],
    source_scope: str,
) -> dict[str, Any]:
    batch, batch_id = _returned_batch(payload)
    returned_requested = _contract_string_list(
        batch.get("requested_ro_numbers"), "requested_ro_numbers"
    )
    if returned_requested != requested_ro_numbers:
        raise ScrapeXContract(
            "requested_ro_contract_mismatch",
            "ScrapeX did not echo the exact requested RO contract.",
        )
    if batch.get("source_scope") != source_scope:
        raise ScrapeXContract(
            "source_scope_mismatch",
            "ScrapeX returned a different source scope than requested.",
        )
    items = _returned_batch_items(batch, batch_id)
    returned_ros = [
        _contract_text(item.get("ro_number"), "item.ro_number", maximum=MAX_RO_CHARS)
        for item in items
    ]
    if returned_ros != requested_ro_numbers:
        raise ScrapeXContract(
            "created_ro_contract_mismatch",
            "The created batch items do not exactly match the requested ROs.",
        )
    readiness = _returned_readiness(batch.get("readiness"))
    if readiness.get("total") != len(items):
        raise ScrapeXContract(
            "created_batch_total_mismatch",
            "ScrapeX readiness does not match the exact created batch items.",
        )
    return batch


def _validate_phase_batch_contract(
    payload: Any,
    *,
    requested_phases: list[str],
    requested_shop: str | None,
    source_scope: str,
) -> dict[str, Any]:
    batch, batch_id = _returned_batch(payload)
    returned_phases = _contract_string_list(batch.get("phases"), "phases")
    if returned_phases != requested_phases:
        raise ScrapeXContract(
            "requested_phase_contract_mismatch",
            "ScrapeX did not echo the exact requested phase contract.",
        )
    returned_shop = batch.get("shop")
    if returned_shop is not None:
        returned_shop = _contract_text(returned_shop, "shop", maximum=MAX_SHOP_CHARS)
    if returned_shop != requested_shop:
        raise ScrapeXContract(
            "requested_shop_contract_mismatch",
            "ScrapeX returned a different shop scope than requested.",
        )
    if batch.get("source_scope") != source_scope:
        raise ScrapeXContract(
            "source_scope_mismatch",
            "ScrapeX returned a different source scope than requested.",
        )
    items = _returned_batch_items(batch, batch_id)
    readiness = _returned_readiness(batch.get("readiness"))
    if readiness.get("total") != len(items):
        raise ScrapeXContract(
            "created_batch_total_mismatch",
            "ScrapeX readiness does not match the created phase batch items.",
        )
    return batch


def _validate_authentication_contract(payload: Any) -> dict[str, Any]:
    status_payload = _contract_mapping(payload, "authentication status")
    if type(status_payload.get("active")) is not bool:  # bool, not truthy coercion
        raise ScrapeXContract(
            "authentication_state_missing",
            "ScrapeX omitted the managed-browser active state.",
        )
    if type(status_payload.get("authenticated")) is not bool:
        raise ScrapeXContract(
            "authentication_state_missing",
            "ScrapeX omitted the managed-browser authentication state.",
        )
    if status_payload["authenticated"] and not status_payload["active"]:
        raise ScrapeXContract(
            "authentication_state_conflict",
            "ScrapeX reported authentication without an active managed browser.",
        )
    return status_payload


def _validate_completed_provenance(
    item: dict[str, Any],
    *,
    expected_ro_number: str,
) -> dict[str, Any]:
    provenance = _provenance(item)
    if provenance.get("contract_version") != 1:
        raise ScrapeXContract(
            "provenance_contract_mismatch",
            "Completed ADAS Map work did not return canonical contract version 1.",
        )
    if provenance.get("state") != "adas_map_complete":
        raise ScrapeXContract(
            "provenance_state_mismatch",
            "Completed ADAS Map work did not return the canonical complete state.",
        )
    if item.get("adas_map_requirements_proven") not in (1, True):
        raise ScrapeXContract(
            "requirements_not_proven",
            "Completed ADAS Map work did not prove its requirements.",
        )
    inspection_id = _contract_text(
        provenance.get("inspection_id"), "inspection_id", maximum=160
    )
    source_url = _contract_text(
        provenance.get("source_url"), "source_url", maximum=2048
    )
    if provenance.get("ciq_reconciliation_state") != "complete":
        raise ScrapeXContract(
            "ciq_reconciliation_incomplete",
            "Completed ADAS Map work was not reconciled to Calibration IQ.",
        )
    reconciliation = _contract_mapping(
        provenance.get("ciq_reconciliation"), "ciq_reconciliation"
    )
    if not (
        reconciliation.get("verified") is True
        and reconciliation.get("snapshot_verified") is True
    ):
        raise ScrapeXContract(
            "ciq_reconciliation_unverified",
            "Calibration IQ reconciliation was not authoritatively verified.",
        )
    raw_result = _contract_mapping(provenance.get("raw_result"), "raw_result")
    if not (
        raw_result.get("success") is True
        and raw_result.get("status") == "complete"
        and raw_result.get("requirements_proven") is True
        and raw_result.get("row_binding_confirmed") is True
        and raw_result.get("modal_inspection_confirmed") is True
        and raw_result.get("required_region_confirmed") is True
    ):
        raise ScrapeXContract(
            "raw_provenance_unverified",
            "The raw ADAS Map evidence did not prove its selected row and modal.",
        )
    modal_runtime_id = _contract_text(
        raw_result.get("modal_runtime_id"),
        "raw_result.modal_runtime_id",
        maximum=160,
    )
    explicit_none = raw_result.get("explicit_no_calibration")
    if type(explicit_none) is not bool:
        raise ScrapeXContract(
            "explicit_none_state_missing",
            "The raw ADAS Map evidence omitted its explicit-none state.",
        )
    requirement_records = raw_result.get("requirement_records")
    if not isinstance(requirement_records, list):
        raise ScrapeXContract(
            "requirement_provenance_missing",
            "The raw ADAS Map evidence omitted structured requirement records.",
        )
    if explicit_none:
        if requirement_records:
            raise ScrapeXContract(
                "explicit_none_conflict",
                "Explicit no-calibration evidence also returned requirements.",
            )
    elif not requirement_records:
        raise ScrapeXContract(
            "requirement_provenance_missing",
            "Completed ADAS Map evidence did not include requirement provenance.",
        )
    else:
        for raw_record in requirement_records:
            record = _contract_mapping(raw_record, "requirement_record")
            control_classes = str(record.get("source_control_class") or "").split()
            if not (
                record.get("source") == "adas_map_required_list_item"
                and record.get("source_context") == "selected_required_modal"
                and record.get("source_context_runtime_id") == modal_runtime_id
                and "custom-link" in control_classes
            ):
                raise ScrapeXContract(
                    "requirement_provenance_mismatch",
                    "A requirement record was not bound to the selected ADAS Map modal.",
                )
    raw_ro = _contract_text(raw_result.get("ro_number"), "raw_result.ro_number", maximum=MAX_RO_CHARS)
    raw_inspection = _contract_text(
        raw_result.get("inspection_id"), "raw_result.inspection_id", maximum=160
    )
    raw_source = raw_result.get("source_url") or raw_result.get("details_url")
    raw_source = _contract_text(raw_source, "raw_result.source_url", maximum=2048)
    if raw_ro != expected_ro_number:
        raise ScrapeXContract(
            "raw_ro_mismatch",
            "The raw ADAS Map evidence belongs to a different repair order.",
        )
    if raw_inspection != inspection_id or raw_source != source_url:
        raise ScrapeXContract(
            "raw_provenance_mismatch",
            "The returned canonical item and raw ADAS Map provenance disagree.",
        )
    return provenance


def _validate_process_one_contract(
    payload: Any,
    *,
    expected_batch_id: str,
    expected_ro_number: str,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    result = _contract_mapping(payload, "process-one result")
    if result.get("attempted") is not True:
        raise ScrapeXContract(
            "attempt_not_proven",
            "ScrapeX did not prove that process-one was attempted.",
        )
    if type(result.get("completed")) is not bool:
        raise ScrapeXContract(
            "completion_state_missing",
            "ScrapeX omitted the process-one completion state.",
        )
    if result.get("batch_id") != expected_batch_id:
        raise ScrapeXContract(
            "batch_mismatch",
            "ScrapeX returned process-one state for a different batch.",
        )
    if result.get("ro_number") != expected_ro_number:
        raise ScrapeXContract(
            "ro_mismatch",
            "ScrapeX returned process-one state for a different repair order.",
        )
    item = _returned_item(
        result.get("item"),
        expected_batch_id=expected_batch_id,
        expected_ro_number=expected_ro_number,
    )
    _returned_readiness(result.get("readiness"))
    completed = result["completed"]
    if completed:
        if result.get("status") != "completed":
            raise ScrapeXContract(
                "completion_status_mismatch",
                "ScrapeX marked process-one complete without its completed status.",
            )
        provenance = _validate_completed_provenance(
            item,
            expected_ro_number=expected_ro_number,
        )
    else:
        _contract_text(result.get("status"), "status", maximum=120)
        provenance = _provenance(item)
    return result, completed, provenance


def _validate_start_contract(
    payload: Any,
    *,
    expected_batch_id: str,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    result = _contract_mapping(payload, "start result")
    if result.get("stage") != "adas_map":
        raise ScrapeXContract(
            "stage_mismatch",
            "ScrapeX returned a start result for a different stage.",
        )
    batch, _ = _returned_batch(
        result.get("batch"), expected_batch_id=expected_batch_id
    )
    _returned_readiness(batch.get("readiness"))
    started = result.get("started")
    already_running = result.get("already_running", False)
    if type(started) is not bool or type(already_running) is not bool:
        raise ScrapeXContract(
            "start_state_missing",
            "ScrapeX omitted its start/already-running state.",
        )
    if not (started or already_running) or (started and already_running):
        raise ScrapeXContract(
            "start_state_conflict",
            "ScrapeX did not return one verified start outcome.",
        )
    return result, batch, started, already_running


def _validate_pause_contract(payload: Any, *, expected_batch_id: str) -> dict[str, Any]:
    result = _contract_mapping(payload, "pause result")
    if result.get("paused") is not True or result.get("stage") != "adas_map":
        raise ScrapeXContract(
            "pause_state_mismatch",
            "ScrapeX did not return a verified ADAS Map pause state.",
        )
    batch, _ = _returned_batch(
        result.get("batch"), expected_batch_id=expected_batch_id
    )
    _returned_readiness(batch.get("readiness"))
    return result


def _navigator_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScrapeXInput("target must be an object.")
    allowed = {"year", "make", "model", "trim", "vin"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ScrapeXInput(f"Unsupported target field(s): {', '.join(unknown)}.")
    target: dict[str, Any] = {}
    year = value.get("year")
    if year is not None:
        if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
            raise ScrapeXInput("target.year must be an integer year.")
        target["year"] = year
    for field, maximum in (
        ("make", MAX_TARGET_FIELD_CHARS),
        ("model", MAX_TARGET_FIELD_CHARS),
        ("trim", MAX_TARGET_FIELD_CHARS),
        ("vin", MAX_VIN_CHARS),
    ):
        raw = value.get(field)
        if raw is not None:
            target[field] = _text(raw, f"target.{field}", maximum=maximum)
    return target


def _navigator_task_id(args: dict[str, Any]) -> str:
    value = _text(args.get("task_id"), "task_id", maximum=MAX_TASK_ID_CHARS)
    assert value is not None
    if not _RESOURCE_ID_RE.fullmatch(value):
        raise ScrapeXInput("task_id must be a bounded ScrapeX identifier.")
    return value


def _navigator_ref(value: Any) -> str:
    result = _text(value, "ref", maximum=MAX_REF_CHARS)
    assert result is not None
    return result


def _validate_navigator_task_contract(
    payload: Any,
    *,
    expected_provider: str | None = None,
    expected_target: dict[str, Any] | None = None,
    expected_topic: str | None = None,
) -> tuple[dict[str, Any], str]:
    task = _contract_mapping(payload, "navigator task")
    task_id = _contract_identifier(task.get("id") or task.get("task_id"), "task.id")
    if expected_provider is not None and task.get("provider") != expected_provider:
        raise ScrapeXContract(
            "navigator_provider_mismatch",
            "ScrapeX returned a navigator task for a different provider.",
        )
    if expected_target is not None and task.get("target") != expected_target:
        raise ScrapeXContract(
            "navigator_target_mismatch",
            "ScrapeX did not echo the exact requested navigator target.",
        )
    if expected_topic is not None and task.get("topic") != expected_topic:
        raise ScrapeXContract(
            "navigator_topic_mismatch",
            "ScrapeX did not echo the exact requested navigator topic.",
        )
    return task, task_id


def _validate_navigator_observation_contract(payload: Any) -> dict[str, Any]:
    observation = _contract_mapping(payload, "navigator observation")
    if not isinstance(observation.get("url"), str):
        raise ScrapeXContract(
            "navigator_observation_malformed", "ScrapeX omitted the observation URL."
        )
    elements = observation.get("elements")
    if not isinstance(elements, list):
        raise ScrapeXContract(
            "navigator_observation_malformed",
            "ScrapeX omitted the observation element list.",
        )
    for raw_element in elements:
        element = _contract_mapping(raw_element, "observation element")
        if not isinstance(element.get("ref"), str) or not element["ref"]:
            raise ScrapeXContract(
                "navigator_observation_malformed",
                "An observation element is missing its ref.",
            )
        if not isinstance(element.get("role"), str) or not isinstance(
            element.get("name"), str
        ):
            raise ScrapeXContract(
                "navigator_observation_malformed",
                "An observation element is missing its role or name.",
            )
    return observation


def _validate_navigator_verification_contract(payload: Any) -> dict[str, Any]:
    proof = _contract_mapping(payload, "navigator verification")
    for key in (
        "vehicle_verified",
        "subject_verified",
        "procedure_leaf_verified",
        "content_extracted",
        "verified",
    ):
        if type(proof.get(key)) is not bool:
            raise ScrapeXContract(
                "navigator_verification_malformed",
                f"ScrapeX omitted the {key} verification gate.",
            )
    if proof.get("provider") is not None and not isinstance(proof.get("provider"), str):
        raise ScrapeXContract(
            "navigator_verification_malformed",
            "ScrapeX returned a malformed verification provider.",
        )
    return proof


def _validate_navigator_page_signals_contract(
    payload: Any, *, expected_provider: str
) -> dict[str, Any]:
    status_payload = _contract_mapping(payload, "navigator page signals")
    if status_payload.get("provider") != expected_provider:
        raise ScrapeXContract(
            "navigator_provider_mismatch",
            "ScrapeX returned page signals for a different provider.",
        )
    if type(status_payload.get("authenticated")) is not bool:
        raise ScrapeXContract(
            "navigator_authentication_state_missing",
            "ScrapeX omitted the Navigator authentication state.",
        )
    signals = status_payload.get("signals")
    if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
        raise ScrapeXContract(
            "navigator_signals_malformed",
            "ScrapeX returned a malformed page-signals list.",
        )
    return status_payload


def _validate_navigator_evidence_contract(
    payload: Any, *, expected_task_id: str
) -> dict[str, Any]:
    evidence = _contract_mapping(payload, "navigator evidence")
    if evidence.get("task_id") != expected_task_id:
        raise ScrapeXContract(
            "navigator_task_mismatch",
            "ScrapeX returned navigator evidence for a different task.",
        )
    if type(evidence.get("verified")) is not bool:
        raise ScrapeXContract(
            "navigator_verification_malformed",
            "ScrapeX omitted the evidence verified state.",
        )
    return evidence


async def status(settings: Any, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return service/dependency state without launching a browser or work."""
    try:
        clean = _clean_args(args)
        _expect_keys(clean, set())
        data = await _request(
            settings,
            "GET",
            "/api/health",
            timeout=STATUS_TIMEOUT,
            may_mutate=False,
        )
    except ScrapeXInput as exc:
        return _input_failure("status", exc)
    except ScrapeXConfiguration as exc:
        return _configuration_failure("status", exc)
    except ScrapeXRemote as exc:
        return _remote_failure("status", exc)
    except ScrapeXTransport as exc:
        return _transport_failure("status", exc)

    adas_map = data.get("adas_map") if isinstance(data, dict) else None
    ciq = data.get("ciq") if isinstance(data, dict) else None
    if isinstance(adas_map, dict) and adas_map.get("ok") is False:
        result = _success(
            "status",
            data,
            status="dependency_unavailable",
            success=False,
        )
        result["ready"] = False
        return result
    authenticated = bool(isinstance(adas_map, dict) and adas_map.get("authenticated"))
    ciq_ready = bool(isinstance(ciq, dict) and ciq.get("authorized"))
    ready = bool(isinstance(data, dict) and data.get("ok") and authenticated and ciq_ready)
    if not authenticated:
        result = _authentication_required("status", adas_map or {}, executed=True)
        result["data"] = _sanitize(data)
        result["verified"] = True  # the read verified that authentication is absent
        result["ready"] = False
        return result
    result = _success(
        "status",
        data,
        status="ready" if ready else "dependency_unavailable",
        success=ready,
    )
    result["ready"] = ready
    return result


def _tail_native_start_log(log_path: Path) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-MAX_NATIVE_START_LOG_CHARS:]


def _windows_powershell_exe() -> str:
    """The exact Windows PowerShell path launch-x-omni.ps1 itself uses to
    spawn Core, rather than a bare "powershell" that depends on this
    process's own PATH resolving it -- Core's child-process environment is
    not guaranteed to match an interactive shell's."""
    exact = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exact) if exact.is_file() else "powershell"


async def start_native(settings: Any) -> dict[str, Any]:
    """Start ScrapeX's local server if it is not already answering
    /api/health, then verify with a fresh probe -- the spawned process
    merely existing is never treated as proof it is actually serving."""
    try:
        await _request(
            settings, "GET", "/api/health", timeout=STATUS_TIMEOUT, may_mutate=False
        )
    except ScrapeXConfiguration as exc:
        return _configuration_failure("start_native", exc)
    except ScrapeXTransport:
        pass  # not reachable yet -- fall through to actually starting it
    else:
        return _success("start_native", None, status="already_healthy", verified=True)

    try:
        project_path = getattr(settings, "scrapex_project_path", None) or DEFAULT_PROJECT_PATH
        script = Path(project_path) / "scripts" / "start.ps1"
        if not script.is_file():
            return _failure(
                "start_native", "configuration_error",
                f"Native launcher not found at {script}.",
            )
        log_dir = Path(settings.root) / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "scrapex_native_start.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [
                    _windows_powershell_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                ],
                cwd=str(project_path),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError as exc:
        return _failure("start_native", "spawn_failed", f"Could not launch ScrapeX: {exc}")

    deadline = time.monotonic() + NATIVE_START_TIMEOUT_S
    healthy = False
    exited_early = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            exited_early = True
            break
        try:
            await _request(
                settings, "GET", "/api/health", timeout=STATUS_TIMEOUT, may_mutate=False
            )
        except ScrapeXTransport:
            await asyncio.sleep(NATIVE_START_POLL_INTERVAL_S)
            continue
        healthy = True
        break

    if not healthy:
        detail = _tail_native_start_log(log_path)
        message = (
            f"ScrapeX exited before becoming healthy (code {process.returncode})."
            if exited_early
            else "ScrapeX did not become healthy before the startup timeout."
        )
        return _failure(
            "start_native", "failed", message,
            detail=detail,
            exit_code=process.returncode if exited_early else None,
        )

    return _success("start_native", None, status="healthy", executed=True, verified=True)


async def read(settings: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Read bounded ScrapeX state selected entirely by structured arguments."""
    action = "read"
    try:
        clean = _clean_args(args)
        action_value = _text(clean.get("action"), "action", maximum=40)
        assert action_value is not None
        action = action_value
        if action not in READ_ACTIONS:
            raise ScrapeXInput(f"Unsupported ScrapeX read action: {action}.")

        if action == "list_batches":
            _expect_keys(clean, {"action"})
            data = await _request(
                settings, "GET", "/api/batches", timeout=READ_TIMEOUT, may_mutate=False
            )
            return _success(action, data)

        if action == "preview_ciq_queue":
            _expect_keys(clean, {"action", "phases", "shop", "source_scope"})
            phases = _string_list(
                clean.get("phases"),
                "phases",
                maximum_items=10,
                maximum_chars=MAX_PHASE_CHARS,
            )
            shop = _text(
                clean.get("shop"), "shop", maximum=MAX_SHOP_CHARS, required=False
            )
            body = {
                "phases": phases,
                "shop": shop,
                "source_scope": _source_scope(clean, "active"),
            }
            data = await _request(
                settings,
                "POST",
                "/api/ciq/preview",
                body=body,
                timeout=READ_TIMEOUT,
                may_mutate=False,
            )
            return _success(action, data)

        _expect_keys(clean, {"action", "batch_id", "ro_number"})
        batch_id = _batch_id(clean)
        encoded_batch = quote(batch_id, safe="")
        if action == "batch_summary":
            if "ro_number" in clean:
                raise ScrapeXInput("ro_number is not used by batch_summary.")
            path = f"/api/batches/{encoded_batch}/summary"
            data = await _request(
                settings, "GET", path, timeout=READ_TIMEOUT, may_mutate=False
            )
            return _success(action, data)
        if action == "batch_exceptions":
            if "ro_number" in clean:
                raise ScrapeXInput("ro_number is not used by batch_exceptions.")
            path = f"/api/batches/{encoded_batch}/exceptions"
            data = await _request(
                settings, "GET", path, timeout=READ_TIMEOUT, may_mutate=False
            )
            return _success(action, data)

        ro_number = _ro_number(clean.get("ro_number"))
        data = await _request(
            settings,
            "GET",
            f"/api/batches/{encoded_batch}",
            timeout=READ_TIMEOUT,
            may_mutate=False,
        )
        batch, _ = _returned_batch(data, expected_batch_id=batch_id)
        items = batch.get("items")
        matches = [
            item
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
            and str(item.get("ro_number") or "").strip() == ro_number
        ]
        if not matches:
            return _failure(action, "not_found", f"RO {ro_number} is not in this batch.")
        if len(matches) != 1:
            return _failure(action, "conflict", f"RO {ro_number} is not unique in this batch.")
        item = matches[0]
        return _success(
            action,
            {
                "batch_id": batch_id,
                "batch_name": batch.get("name"),
                "batch_state": batch.get("state"),
                "readiness": batch.get("readiness"),
                "item": item,
                "provenance": _provenance(item),
            },
        )
    except ScrapeXInput as exc:
        return _input_failure(action, exc)
    except ScrapeXConfiguration as exc:
        return _configuration_failure(action, exc)
    except ScrapeXRemote as exc:
        return _remote_failure(action, exc)
    except ScrapeXTransport as exc:
        return _transport_failure(action, exc)
    except ScrapeXContract as exc:
        return _contract_failure(action, exc, may_mutate=False)


async def _authentication_status(settings: Any, action: str) -> dict[str, Any] | None:
    data = await _request(
        settings,
        "GET",
        "/api/adas-map/status",
        timeout=STATUS_TIMEOUT,
        may_mutate=False,
    )
    try:
        data = _validate_authentication_contract(data)
    except ScrapeXContract as exc:
        return _contract_failure(action, exc, may_mutate=False)
    if not data["authenticated"]:
        return _authentication_required(action, data, executed=False)
    return None


async def adas_map(settings: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Perform one explicitly selected ScrapeX ADAS Map control operation."""
    action = "adas_map"
    try:
        clean = _clean_args(args)
        action_value = _text(clean.get("action"), "action", maximum=40)
        assert action_value is not None
        action = action_value
        if action not in ADAS_MAP_ACTIONS:
            raise ScrapeXInput(f"Unsupported ScrapeX ADAS Map action: {action}.")

        if action == "open_authentication":
            _expect_keys(clean, {"action"})
            data = await _request(
                settings,
                "POST",
                "/api/adas-map/open",
                body={},
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            data = _validate_authentication_contract(data)
            if not data["authenticated"]:
                return _authentication_required(action, data, executed=True)
            return _success(action, data, status="verified", work_complete=True)

        if action == "create_exact_batch":
            _expect_keys(clean, {"action", "name", "ro_numbers", "source_scope"})
            name = _text(
                clean.get("name", "ScrapeX staged acceptance"),
                "name",
                maximum=MAX_NAME_CHARS,
            )
            ro_numbers = _string_list(
                clean.get("ro_numbers"),
                "ro_numbers",
                maximum_items=10,
                maximum_chars=MAX_RO_CHARS,
            )
            body = {
                "name": name,
                "ro_numbers": ro_numbers,
                "source_scope": _source_scope(clean, "all"),
            }
            data = await _request(
                settings,
                "POST",
                "/api/batches/from-ciq/exact",
                body=body,
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            data = _validate_exact_batch_contract(
                data,
                requested_ro_numbers=ro_numbers,
                source_scope=body["source_scope"],
            )
            readiness = data.get("readiness")
            complete = bool(isinstance(readiness, dict) and readiness.get("ready"))
            return _success(
                action,
                data,
                status="completed" if complete else "queued",
                work_complete=complete,
            )

        if action == "create_phase_batch":
            _expect_keys(
                clean, {"action", "name", "phases", "shop", "source_scope"}
            )
            name = _text(
                clean.get("name", "Calibration IQ weekly queue"),
                "name",
                maximum=MAX_NAME_CHARS,
            )
            phases = _string_list(
                clean.get("phases"),
                "phases",
                maximum_items=10,
                maximum_chars=MAX_PHASE_CHARS,
            )
            shop = _text(
                clean.get("shop"), "shop", maximum=MAX_SHOP_CHARS, required=False
            )
            body = {
                "name": name,
                "phases": phases,
                "shop": shop,
                "source_scope": _source_scope(clean, "active"),
            }
            data = await _request(
                settings,
                "POST",
                "/api/batches/from-ciq",
                body=body,
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            data = _validate_phase_batch_contract(
                data,
                requested_phases=phases,
                requested_shop=shop,
                source_scope=body["source_scope"],
            )
            readiness = data.get("readiness")
            complete = bool(isinstance(readiness, dict) and readiness.get("ready"))
            return _success(
                action,
                data,
                status="completed" if complete else "queued",
                work_complete=complete,
            )

        if action in {"process_one", "start_batch"}:
            allowed = {"action", "batch_id"}
            if action == "process_one":
                allowed.add("ro_number")
            _expect_keys(clean, allowed)
            batch_id = _batch_id(clean)
            ro_number = (
                _ro_number(clean.get("ro_number")) if action == "process_one" else None
            )
            authentication = await _authentication_status(settings, action)
            if authentication is not None:
                return authentication
        else:
            _expect_keys(clean, {"action", "batch_id"})
            batch_id = _batch_id(clean)
            ro_number = None

        encoded_batch = quote(batch_id, safe="")
        if action == "process_one":
            assert ro_number is not None
            path = (
                f"/api/batches/{encoded_batch}/adas-map/process-one/"
                f"{quote(ro_number, safe='')}"
            )
            data = await _request(
                settings,
                "POST",
                path,
                body={},
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            data, completed, provenance = _validate_process_one_contract(
                data,
                expected_batch_id=batch_id,
                expected_ro_number=ro_number,
            )
            operation_status = (
                "completed"
                if completed
                else str(data["status"])
            )
            data = {**data, "provenance": provenance}
            return _success(
                action,
                data,
                status=operation_status,
                verified=completed,
                work_complete=completed,
                success=completed,
            )

        if action == "start_batch":
            data = await _request(
                settings,
                "POST",
                f"/api/batches/{encoded_batch}/adas-map/start",
                body={},
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            data, batch, started, already_running = _validate_start_contract(
                data,
                expected_batch_id=batch_id,
            )
            readiness = batch.get("readiness")
            complete = bool(isinstance(readiness, dict) and readiness.get("ready"))
            return _success(
                action,
                data,
                status="completed" if complete else "running",
                # The bounded start request itself was executed and the service
                # authoritatively confirmed either a new or already-running task.
                executed=started or already_running,
                verified=started or already_running,
                work_complete=complete,
                success=started or already_running,
            )

        data = await _request(
            settings,
            "POST",
            f"/api/batches/{encoded_batch}/adas-map/pause",
            body={},
            timeout=OPERATOR_TIMEOUT,
            may_mutate=True,
        )
        data = _validate_pause_contract(data, expected_batch_id=batch_id)
        paused = True
        return _success(
            action,
            data,
            status="paused" if paused else "indeterminate",
            executed=paused,
            verified=paused,
            work_complete=False,
            success=paused,
        )
    except ScrapeXInput as exc:
        return _input_failure(action, exc)
    except ScrapeXConfiguration as exc:
        return _configuration_failure(action, exc)
    except ScrapeXRemote as exc:
        return _remote_failure(action, exc)
    except ScrapeXTransport as exc:
        return _transport_failure(action, exc)
    except ScrapeXContract as exc:
        return _contract_failure(action, exc, may_mutate=True)


async def navigator_current_page_signals(settings: Any, provider: str) -> dict[str, Any]:
    """Bounded, generic "what's currently on screen" read -- no task, no
    model turns, no specific candidate vehicle. For the small number of
    non-agentic callers (Calibration IQ work-prep matching) that need to
    check many candidate rows against whatever vehicle is currently
    selected in an already-authenticated Navigator session.
    """
    action = "current_page_signals"
    try:
        provider_value = _text(provider, "provider", maximum=40)
        assert provider_value is not None
        if provider_value not in NAVIGATOR_PROVIDERS:
            raise ScrapeXInput(f"Unsupported navigator provider: {provider_value}.")
        data = await _request(
            settings,
            "GET",
            f"/api/navigator/providers/{quote(provider_value, safe='')}/current-page-signals",
            timeout=READ_TIMEOUT,
            may_mutate=False,
        )
        status_payload = _validate_navigator_page_signals_contract(
            data, expected_provider=provider_value
        )
        return _success(action, status_payload, status="read", verified=True)
    except ScrapeXInput as exc:
        return _input_failure(action, exc)
    except ScrapeXConfiguration as exc:
        return _configuration_failure(action, exc)
    except ScrapeXRemote as exc:
        return _remote_failure(action, exc)
    except ScrapeXTransport as exc:
        return _transport_failure(action, exc)
    except ScrapeXContract as exc:
        return _contract_failure(action, exc, may_mutate=False)



async def navigator_screenshot(settings: Any, task_id: str) -> tuple[bytes, str]:
    """Fetch one task-bound Navigator still for transient multimodal reasoning.

    This is intentionally not a registered model tool. Raw image bytes never
    enter a tool result or durable conversation artifact; the caller may feed
    the validated still directly to the active local vision worker for the
    current browser turn only.
    """
    task_value = _text(task_id, "task_id", maximum=MAX_TASK_ID_CHARS)
    assert task_value is not None
    if not _RESOURCE_ID_RE.fullmatch(task_value):
        raise ScrapeXInput("task_id must be a bounded ScrapeX identifier.")

    base_url = _base_url(settings)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=READ_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
            headers={
                "Accept": "image/jpeg",
                "User-Agent": "X-Omni/ScrapeX-navigator-vision",
            },
        ) as client:
            response = await client.get(
                f"/api/navigator/tasks/{quote(task_value, safe='')}/screenshot"
            )
    except httpx.TimeoutException as exc:
        raise ScrapeXTransport(
            "timeout", "ScrapeX screenshot did not arrive before the timeout."
        ) from exc
    except httpx.HTTPError as exc:
        raise ScrapeXTransport(
            "unavailable", "ScrapeX is unavailable on its local loopback endpoint."
        ) from exc

    if response.status_code >= 400:
        try:
            payload = response.json()
            detail = payload.get("detail") if isinstance(payload, dict) else payload
        except ValueError:
            detail = response.text[:600]
        raise ScrapeXRemote(response.status_code, detail, may_mutate=False)

    content = response.content
    if not content or len(content) > MAX_NAV_SCREENSHOT_BYTES:
        raise ScrapeXContract(
            "navigator_screenshot_invalid",
            "ScrapeX returned an empty or oversized Navigator screenshot.",
        )
    media_type = (
        str(response.headers.get("content-type") or "")
        .partition(";")[0]
        .strip()
        .casefold()
    )
    if media_type != "image/jpeg" or not content.startswith(b"\xff\xd8\xff"):
        raise ScrapeXContract(
            "navigator_screenshot_invalid",
            "ScrapeX returned a Navigator screenshot with an invalid image contract.",
        )
    echoed_task = str(response.headers.get("x-scrapex-task-id") or "").strip()
    if echoed_task != task_value:
        raise ScrapeXContract(
            "navigator_task_mismatch",
            "ScrapeX returned a screenshot for a different Navigator task.",
        )
    return content, media_type


_NAVIGATOR_ALLOWED_KEYS: dict[str, set[str]] = {
    "create_task": {"action", "provider", "target", "topic", "action_budget"},
    "observe": {"action", "task_id"},
    "verify": {"action", "task_id"},
    "get_evidence": {"action", "task_id"},
    "back": {"action", "task_id"},
    "extract": {"action", "task_id"},
    "done": {"action", "task_id"},
    "click": {"action", "task_id", "ref"},
    "fill": {"action", "task_id", "ref", "text"},
    "press": {"action", "task_id", "ref", "key"},
    "open": {"action", "task_id", "url"},
}


async def navigator(settings: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Drive one bounded ScrapeX Navigator browser turn per model call.

    ``create_task`` is id-free. Every other action requires an exact
    ``task_id`` copied verbatim from a verified same-turn result -- the
    caller's registry-level evidence gate enforces that binding before this
    function is ever invoked; this function's own contract validation only
    proves that ScrapeX's response is well-formed, never that the id was
    legitimately obtained.
    """
    action = "navigator"
    try:
        clean = _clean_args(args)
        action_value = _text(clean.get("action"), "action", maximum=40)
        assert action_value is not None
        action = action_value
        if action not in NAVIGATOR_ACTIONS:
            raise ScrapeXInput(f"Unsupported ScrapeX navigator action: {action}.")
        _expect_keys(clean, _NAVIGATOR_ALLOWED_KEYS[action])

        if action == "create_task":
            provider = _text(clean.get("provider"), "provider", maximum=40)
            assert provider is not None
            if provider not in NAVIGATOR_PROVIDERS:
                raise ScrapeXInput(f"Unsupported navigator provider: {provider}.")
            target = _navigator_target(clean.get("target"))
            topic = _text(clean.get("topic"), "topic", maximum=MAX_TOPIC_CHARS)
            assert topic is not None
            action_budget = clean.get("action_budget")
            if action_budget is not None and (
                isinstance(action_budget, bool)
                or not isinstance(action_budget, int)
                or not 1 <= action_budget <= 80
            ):
                raise ScrapeXInput("action_budget must be an integer from 1 to 80.")
            body: dict[str, Any] = {"provider": provider, "target": target, "topic": topic}
            if action_budget is not None:
                body["action_budget"] = action_budget
            data = await _request(
                settings,
                "POST",
                "/api/navigator/tasks",
                body=body,
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            task, _task_id = _validate_navigator_task_contract(
                data,
                expected_provider=provider,
                expected_target=target,
                expected_topic=topic,
            )
            return _success(action, task, status="created", work_complete=False)

        task_id = _navigator_task_id(clean)
        encoded_task = quote(task_id, safe="")

        if action == "observe":
            data = await _request(
                settings,
                "POST",
                f"/api/navigator/tasks/{encoded_task}/observe",
                body={},
                timeout=OPERATOR_TIMEOUT,
                may_mutate=True,
            )
            observation = _validate_navigator_observation_contract(data)
            return _success(action, observation, status="observed")

        if action == "verify":
            data = await _request(
                settings,
                "POST",
                f"/api/navigator/tasks/{encoded_task}/verify",
                body={},
                timeout=OPERATOR_TIMEOUT,
                may_mutate=False,
            )
            proof = _validate_navigator_verification_contract(data)
            verified = proof["verified"]
            return _success(
                action,
                proof,
                status="verified" if verified else "unverified",
                verified=verified,
                work_complete=verified,
                success=verified,
            )

        if action == "get_evidence":
            data = await _request(
                settings,
                "GET",
                f"/api/navigator/tasks/{encoded_task}/evidence",
                timeout=READ_TIMEOUT,
                may_mutate=False,
            )
            evidence = _validate_navigator_evidence_contract(
                data, expected_task_id=task_id
            )
            return _success(action, evidence, status="read", verified=True)

        # click/fill/press/back/open/extract/done all drive the same bounded
        # /act endpoint; ScrapeX's own executor is the sole authority on
        # whether the ref/url is valid and the action kind is legal.
        body = {"action": action}
        if action == "click":
            body["ref"] = _navigator_ref(clean.get("ref"))
        elif action == "fill":
            body["ref"] = _navigator_ref(clean.get("ref"))
            text = _text(clean.get("text"), "text", maximum=MAX_FILL_TEXT_CHARS)
            assert text is not None
            body["text"] = text
        elif action == "press":
            body["ref"] = _navigator_ref(clean.get("ref"))
            key = _text(clean.get("key"), "key", maximum=MAX_KEY_CHARS)
            assert key is not None
            body["key"] = key
        elif action == "open":
            url = _text(clean.get("url"), "url", maximum=MAX_NAV_URL_CHARS)
            assert url is not None
            body["url"] = url
        data = await _request(
            settings,
            "POST",
            f"/api/navigator/tasks/{encoded_task}/act",
            body=body,
            timeout=OPERATOR_TIMEOUT,
            may_mutate=True,
        )
        observation = _validate_navigator_observation_contract(data)
        return _success(
            action,
            observation,
            status="acted",
            work_complete=(action == "done"),
        )
    except ScrapeXInput as exc:
        return _input_failure(action, exc)
    except ScrapeXConfiguration as exc:
        return _configuration_failure(action, exc)
    except ScrapeXRemote as exc:
        return _remote_failure(action, exc)
    except ScrapeXTransport as exc:
        return _transport_failure(action, exc)
    except ScrapeXContract as exc:
        return _contract_failure(
            action, exc, may_mutate=(action not in {"verify", "get_evidence"})
        )
