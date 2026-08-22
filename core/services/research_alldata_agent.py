"""Model-driven ALLDATA navigation.

ALLDATA search used to be one deterministic Python function that clicked
through a fixed sequence of selectors -- effective until ALLDATA's layout
drifted, at which point the only fix available was another selector. That
function (research_alldata_navigation.search_alldata_vehicle_first) still
exists as a fallback, but the primary path now gives the model real
turn-by-turn control over the same collision_research tool actions
(snapshot/goto/click_text/fill/press/extract) already available to it in
ordinary conversation, observing the real page after each action and
deciding what to do next -- so a portal layout change is something the model
can read and adapt to, not something that requires a new wrapper file.

Truthfulness never depends on the model's own narration of what it did.
After the loop ends (or is cut off by the turn budget), a deterministic
epilogue independently re-reads the live page and scores it through the same
research_verification.evaluate_alldata_claim predicate the deterministic
search path uses, so "verified" means the same thing regardless of which
navigation path produced the result.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import research_alldata_navigation as nav
from . import research_operator as ro
from . import research_verification

log = logging.getLogger("xomni.research_alldata_agent")

MAX_AGENT_TURNS = 7
_NAV_ACTIONS = ("snapshot", "goto", "click_text", "fill", "press", "extract")

AGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "collision_research",
        "description": (
            "Operate the ALLDATA Repair/Collision browser session, which is ALREADY "
            "authenticated and already sitting inside my.alldata.com -- never goto a fresh "
            "URL as your first move; call snapshot first to see the real page you're already "
            "on. ALLDATA's workflow is vehicle-first: (1) snapshot to see what's currently on "
            "screen, (2) fill the Year/Make/Model or VIN box with the requested vehicle and "
            "click_text the matching result to select that exact vehicle -- this is mandatory "
            "before any search result can count as relevant to it, (3) once selected, fill the "
            "Vehicle Information Search box with the calibration/reset topic and press Enter, "
            "(4) click_text the most relevant result, (5) extract to read the procedure text. "
            "Stop calling tools and answer in plain text once you have extracted the relevant "
            "procedure, or once you are confident ALLDATA does not have it for this exact "
            "vehicle -- do not keep calling tools once you have enough information to answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_NAV_ACTIONS)},
                "text": {
                    "type": "string",
                    "description": (
                        "The ONLY field that carries a value to type or click. For fill: the "
                        "text to type into the field named by 'selector'. For click_text: the "
                        "visible link/button text to click. Never put this value in 'key' -- "
                        "'key' is unrelated and only used by the press action."
                    ),
                },
                "selector": {"type": "string", "description": "CSS selector -- required by, and only used by, fill."},
                "url": {"type": "string", "description": "Only used by goto; must stay on an alldata.com URL."},
                "key": {
                    "type": "string",
                    "description": (
                        "A keyboard key name, e.g. Enter -- ONLY used by the press action. Never "
                        "used by fill; fill's value goes in 'text', not here."
                    ),
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _system_prompt(vehicle: dict[str, Any], topic: str) -> str:
    label = str(vehicle.get("label") or "").strip() or "the requested vehicle"
    return (
        "You are operating a licensed ALLDATA Repair/Collision browser session for a "
        "collision repair technician. The browser session is ALREADY authenticated and "
        "already positioned inside my.alldata.com -- call snapshot as your very first action "
        "to see the real page you're on. Do not call goto to a fresh URL (e.g. alldata.com or "
        "www.alldata.com) as your first move; that leaves the authenticated portal and lands "
        "on the public marketing site instead. Find the exact OEM procedure for:\n"
        f"Vehicle: {label}\n"
        f"Topic: {topic}\n\n"
        "Do not substitute a different model, trim, or year, and do not answer from general "
        "knowledge -- only from what you actually observe in a tool result. Your final answer "
        "is independently checked against the real page state afterward, so a claim that isn't "
        "backed by what the tools actually showed you will be discarded rather than trusted. "
        "If a tool call returns an error, read it and change what you send -- do not repeat the "
        "exact same call. If you cannot find this exact vehicle/topic after a reasonable number "
        "of steps, say so plainly instead of guessing."
    )


def _validate_args(action: str, args: dict[str, Any]) -> Optional[str]:
    """Catch a malformed call before it even reaches the browser, with a
    correction specific enough for the model to actually fix it -- reproduced
    live: the model called fill with {"key": "..."} instead of {"text": "..."}
    and repeated that exact mistake for the rest of its turn budget against a
    generic backend ValueError that didn't spell out the fix."""
    if action == "fill":
        if not str(args.get("selector") or "").strip():
            return "fill requires a non-empty 'selector' naming the field to type into."
        if not str(args.get("text") or "").strip():
            extra = " You sent 'key' instead of 'text' -- 'key' is not read by fill." if args.get("key") else ""
            return "fill requires a non-empty 'text' field with the value to type." + extra
    elif action == "click_text":
        if not str(args.get("text") or "").strip():
            return "click_text requires a non-empty 'text' field with the visible link/button text to click."
    elif action == "goto":
        if not str(args.get("url") or "").strip():
            return "goto requires a non-empty 'url'."
    elif action == "press":
        if not str(args.get("key") or "").strip():
            return "press requires a non-empty 'key', e.g. Enter."
    return None


def _extract_content(events: list[dict[str, Any]]) -> str:
    return "".join(str(event.get("text") or "") for event in events if event.get("type") == "content")


def _extract_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == "tool_call"]


async def run_agent_search(
    *,
    client: Any,
    browser: Any,
    vehicle: dict[str, Any],
    topic: str,
    max_turns: int = MAX_AGENT_TURNS,
) -> dict[str, Any]:
    state = await browser.start(auto_login=True)
    if not state.get("authenticated"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "human_action_required": True,
            "reason": "ALLDATA requires a human authentication step before research can continue.",
            "status": state,
        }

    if not vehicle.get("label"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": "The research query did not contain enough vehicle identity to drive ALLDATA navigation.",
        }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(vehicle, topic)},
        {
            "role": "user",
            "content": f"Find the ALLDATA procedure for {vehicle.get('label')}: {topic}.",
        },
    ]
    trace: list[dict[str, Any]] = []
    query_submitted = False
    last_extract_text = ""
    stopped_reason = "model_finished"
    last_failed_call: Optional[tuple[str, tuple[tuple[str, Any], ...]]] = None
    repeated_failure_count = 0

    for turn in range(max_turns):
        try:
            events = [
                event async for event in client.stream(messages, tools=[AGENT_TOOL_SCHEMA], max_tokens=500)
            ]
        except Exception as exc:  # noqa: BLE001
            trace.append({"turn": turn, "error": f"model call failed: {type(exc).__name__}: {exc}"})
            stopped_reason = "model_error"
            break

        content = _extract_content(events)
        calls = _extract_tool_calls(events)
        if not calls:
            stopped_reason = "model_finished"
            break

        wire_calls = [
            {
                "id": call.get("id") or f"call_{turn}_{index}",
                "type": "function",
                "function": {"name": "collision_research", "arguments": call.get("arguments") or "{}"},
            }
            for index, call in enumerate(calls)
        ]
        messages.append({"role": "assistant", "content": content or None, "tool_calls": wire_calls})

        turn_hit_repeat_limit = False
        for call, wire_call in zip(calls, wire_calls):
            try:
                args = json.loads(call.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            action = str(args.get("action") or "").casefold()
            validation_error = _validate_args(action, args) if action in _NAV_ACTIONS else None
            if action not in _NAV_ACTIONS:
                result: dict[str, Any] = {
                    "error": f"'{action or '(missing)'}' is not a navigation action available to this loop."
                }
            elif validation_error:
                # Caught before ever reaching the browser: a specific, fixable
                # correction beats the backend's generic "selector and text are
                # required" -- reproduced live, the model repeated that exact
                # malformed call for its entire remaining turn budget without
                # ever correcting the mistake.
                result = {"error": validation_error}
            else:
                try:
                    result = await browser.operator_action({**args, "action": action})
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}"}

            call_error = (result or {}).get("error") if isinstance(result, dict) else None
            call_signature = (action, tuple(sorted((k, v) for k, v in args.items() if k != "action")))
            if call_error and call_signature == last_failed_call:
                repeated_failure_count += 1
            else:
                repeated_failure_count = 1 if call_error else 0
            last_failed_call = call_signature if call_error else None
            if call_error and repeated_failure_count >= 2:
                result = {
                    **result,
                    "error": (
                        f"REPEATED MISTAKE ({repeated_failure_count}x): you sent the exact same "
                        f"call and got the exact same error again. Re-read the error and change "
                        f"the arguments -- do not resend this call unchanged. {call_error}"
                    ),
                }
            if call_error and repeated_failure_count >= 3:
                turn_hit_repeat_limit = True

            if action == "fill" and "search" in str(args.get("selector") or "").casefold():
                query_submitted = True
            if action == "extract" and isinstance(result, dict):
                last_extract_text = str(result.get("page_text") or "") or last_extract_text

            trace.append({
                "turn": turn,
                "action": action,
                "args": {k: v for k, v in args.items() if k != "action"},
                "url": (result or {}).get("url") if isinstance(result, dict) else None,
                "title": (result or {}).get("title") if isinstance(result, dict) else None,
                "error": (result or {}).get("error") if isinstance(result, dict) else None,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": wire_call["id"],
                "content": json.dumps(result, default=str)[:6_000],
            })

        if turn_hit_repeat_limit:
            # The model sent the identical failing call three times in a row
            # even after a corrective hint -- reproduced live, it burned an
            # entire 7-turn budget on one malformed fill call. No point
            # spending the rest of the budget on a mistake it isn't
            # correcting; stop and let the epilogue report honestly.
            stopped_reason = "repeated_tool_error"
            break
    else:
        stopped_reason = "turn_budget_exhausted"

    # Deterministic epilogue -- independently re-read the live page rather
    # than trusting anything the model said in `messages`. This is the same
    # research_verification predicate the deterministic search path uses, so
    # "verified" carries one meaning regardless of which path produced it.
    page = getattr(browser, "_page", None)
    if page is None:
        return {
            "attempted": True,
            "searched": bool(trace),
            "verified": False,
            "reason": "No active ALLDATA page remained after the agent loop.",
            "agent_trace": trace,
            "agent_stopped_reason": stopped_reason,
        }

    try:
        body = str(await page.locator("body").inner_text(timeout=8_000) or "")
    except Exception:
        body = ""
    if not body:
        body = last_extract_text

    current_label = await nav._current_vehicle_label(page)  # noqa: SLF001 - same-package epilogue check
    vehicle_state = {
        "selected": await nav._confirms_identity(current_label, vehicle),  # noqa: SLF001
        "confirmed_via": current_label[:200] if current_label else None,
    }
    if not vehicle_state["selected"]:
        vehicle_state["reason"] = (
            "The agent's session did not end with a bounded signal confirming the requested "
            "vehicle was actually selected."
        )

    matched_terms = [token for token in nav._research_tokens(topic) if token in body.casefold()]  # noqa: SLF001
    claim = research_verification.evaluate_alldata_claim(
        vehicle=vehicle,
        vehicle_state=vehicle_state,
        query_submitted=query_submitted,
        matched_terms=matched_terms,
        relevance_score=len(matched_terms),
        result_page_text=body,
    )

    try:
        title = str(await page.title() or "")
    except Exception:
        title = ""

    return {
        "attempted": True,
        "searched": bool(trace),
        "verified": claim["verified"],
        "verification_reason": claim["reason"],
        "query_submitted": query_submitted,
        "vehicle": vehicle,
        "topic": topic,
        "vehicle_selection": vehicle_state,
        "agent_trace": trace,
        "agent_stopped_reason": stopped_reason,
        "url": str(page.url)[: ro.MAX_URL_CHARS],
        "title": title[:300],
        "matched_terms": matched_terms,
        "relevance_score": len(matched_terms),
        "page_text": body[:20_000],
        "provenance": {
            "provider": ro.PROVIDER_LABEL,
            "licensed_session": True,
            "workflow": "model_agent_navigation",
        },
    }
