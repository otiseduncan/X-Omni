"""Model-driven ALLDATA navigation over ScrapeX's Navigator HTTP API.

Sibling to research_alldata_agent.py, not a replacement for it -- same
turn-by-turn loop/message-bookkeeping shape (a model turn -> a tool call ->
an environment result -> back to the model), but every action is a
contract-validated HTTP call through core.services.scrapex.navigator(...)
against ScrapeX's own persistent-profile browser, session, navigation
graph, and action-budget/loop-detection, instead of an in-process
Playwright page driven directly by this process. ScrapeX owns browser
mechanics; this loop remains the only reasoning layer, exactly as it was
for the old path -- only where the "browser" lives has changed.

Feature-flagged off by default via Settings.alldata_navigator_enabled; the
old research_alldata_agent.py stays the default path until this one has
proven itself against a real ALLDATA acceptance case. See the ScrapeX
Navigator architecture plan.

Truthfulness never depends on the model's own narration of what it did.
After the loop ends (the model stops calling tools, sends "done", or the
turn budget runs out), a deterministic epilogue calls ScrapeX's own verify
action -- the single authority on evaluate_navigation_claim -- so "verified"
means the same thing regardless of how many turns the model actually used.
This loop never recomputes browser semantics itself; it only checks the
*shape* of what ScrapeX's contract-validated client returns.
"""

from __future__ import annotations

import base64
import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from . import scrapex as scrapex_svc

log = logging.getLogger("xomni.research_navigator_agent")

# The active X model is bound only for the duration of one Registry handler
# invocation. This lets a composite operator capability delegate a bounded
# browser-navigation subtask back to the same model without global state,
# keyword routing, or a second model process.
_ACTIVE_MODEL_CLIENT: ContextVar[Any | None] = ContextVar(
    "xomni_active_navigator_model_client",
    default=None,
)


def bind_model_client(client: Any):
    return _ACTIVE_MODEL_CLIENT.set(client)


def reset_model_client(token: Any) -> None:
    _ACTIVE_MODEL_CLIENT.reset(token)


def current_model_client() -> Any | None:
    return _ACTIVE_MODEL_CLIENT.get()


MAX_MODEL_TURNS = 40
_NAV_ACTIONS = ("observe", "click", "fill", "press", "back", "open", "extract", "done")
# Bounded at the element level, not by an outer character truncation --
# confirmed live against real ALLDATA search results (500+ entries): a flat
# json.dumps(...)[:N] cap cut the elements array off mid-object, so the
# model picked a ref from an incomplete list and selected the wrong
# vehicle. Capping the list itself keeps the fed-back JSON always complete.
MAX_ELEMENTS_FOR_MODEL = 120
_TOOL_RESULT_CHAR_BACKSTOP = 24_000

NAVIGATOR_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "navigator_browse",
        "description": (
            "Operate the real, already-authenticated ALLDATA browser session one bounded "
            "action at a time. Reason from the current rendered page and structured element "
            "map; do not assume a fixed ALLDATA hierarchy or scripted drill-down sequence. "
            "Every click/fill/press targets an exact 'ref' copied verbatim from the latest "
            "observation -- never invent a ref, role, label, selector, or coordinate. After "
            "each action the browser is re-observed, so choose the next action from the new "
            "state rather than predicting what a page should contain. Maintain the exact "
            "requested vehicle as a hard evidence requirement, explore/backtrack as needed, "
            "and call extract only when actual procedure content is on screen rather than a "
            "menu or results list. Call done when the requested evidence has been extracted "
            "or when the observed site state shows the goal is not reachable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_NAV_ACTIONS)},
                "ref": {
                    "type": "string",
                    "description": (
                        "The exact element ref from the most recent observation -- required "
                        "by, and only used by, click/fill/press."
                    ),
                },
                "text": {"type": "string", "description": "Text to type -- only used by fill."},
                "key": {
                    "type": "string",
                    "description": "A keyboard key name such as Enter or Tab -- only used by press.",
                },
                "url": {
                    "type": "string",
                    "description": "Only used by open; must stay on this provider's own domain.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _target_label(target: dict[str, Any]) -> str:
    parts = [
        str(target.get(key)).strip()
        for key in ("year", "make", "model", "trim")
        if target.get(key) not in (None, "")
    ]
    label = " ".join(part for part in parts if part)
    return label or "the requested vehicle"


def _system_prompt(target: dict[str, Any], topic: str) -> str:
    label = _target_label(target)
    return (
        "You are operating a licensed ALLDATA Repair/Collision browser session for a "
        "collision repair technician, through a bounded Navigator action interface. The "
        "session is already authenticated. Your very first tool call has already been "
        "answered with an initial observation of the current page -- read it before acting. "
        f"Find the exact OEM procedure for:\nVehicle: {label}\nTopic: {topic}\n\n"
        "Do not substitute a different model, trim, or year, and do not answer from general "
        "knowledge -- only from what you actually observe. You are the navigation reasoner: "
        "choose the next browser action from the live page state instead of following a fixed "
        "menu script or keyword router. A task-bound annotated screenshot accompanies each "
        "observation when available; labels such as [e12] on the image are the same exact refs "
        "listed in the structured observation. Use pixels to understand layout, grouping, "
        "selected state, menus, and drill-down context, but act only by an observed ref. The "
        "browser will be re-observed after each executed action, so choose one action at a time "
        "and then reassess. Your final claim is independently checked against the real page. "
        "If a tool call returns an error, adapt to the observed state rather than repeating it. "
        "If the exact vehicle/topic cannot be found after reasonable exploration, call 'done' "
        "and say so plainly instead of guessing."
    )


def _validate_args(action: str, args: dict[str, Any]) -> Optional[str]:
    if action in ("click", "fill", "press") and not str(args.get("ref") or "").strip():
        return (
            f"{action} requires a non-empty 'ref' copied verbatim from the most recent "
            "observation's elements list."
        )
    if action == "fill" and not str(args.get("text") or "").strip():
        return "fill requires a non-empty 'text' field with the value to type."
    if action == "press" and not str(args.get("key") or "").strip():
        return "press requires a non-empty 'key', e.g. Enter."
    if action == "open" and not str(args.get("url") or "").strip():
        return "open requires a non-empty 'url'."
    return None


def _extract_content(events: list[dict[str, Any]]) -> str:
    return "".join(str(event.get("text") or "") for event in events if event.get("type") == "content")


def _extract_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == "tool_call"]


def _observation_summary(navigator_result: dict[str, Any]) -> dict[str, Any]:
    """The bounded, model-facing view of one navigator() call's result.

    Deliberately smaller than the full contract-validated payload -- the
    model needs url/title/elements/warnings to decide its next action, not
    ScrapeX's internal status bookkeeping.
    """
    data = navigator_result.get("data") if isinstance(navigator_result, dict) else None
    if not isinstance(data, dict):
        return {"error": (navigator_result or {}).get("error") if isinstance(navigator_result, dict) else "no_data"}
    elements = data.get("elements")
    elements = elements[:MAX_ELEMENTS_FOR_MODEL] if isinstance(elements, list) else elements
    truncated = isinstance(data.get("elements"), list) and len(data["elements"]) > MAX_ELEMENTS_FOR_MODEL
    summary: dict[str, Any] = {
        "url": data.get("url"),
        "title": data.get("title"),
        "elements": elements,
        "loop_warning": data.get("loop_warning"),
        "backtrack_available": data.get("backtrack_available"),
        "repeated_action_warning": data.get("repeated_action_warning"),
    }
    if truncated:
        summary["elements_truncated"] = (
            f"Only the first {MAX_ELEMENTS_FOR_MODEL} of "
            f"{len(data['elements'])} elements are shown. If what you need "
            "isn't here, narrow the search (e.g. add the trim) rather than "
            "guessing a ref that isn't in this list."
        )
    return summary


async def _task_screenshot(
    settings: Any, task_id: str
) -> Optional[tuple[bytes, str]]:
    """Best-effort visual observation; pixels never become the truth gate."""
    try:
        return await scrapex_svc.navigator_screenshot(settings, task_id)
    except Exception as exc:  # noqa: BLE001 - vision supplements the DOM contract
        log.debug("Navigator screenshot unavailable for %s: %s", task_id, exc)
        return None


def _visual_observation_content(
    heading: str,
    summary: dict[str, Any],
    screenshot: Optional[tuple[bytes, str]],
) -> str | list[dict[str, Any]]:
    text = (
        f"{heading}\n\nStructured observation: "
        f"{json.dumps(summary, default=str)[:_TOOL_RESULT_CHAR_BACKSTOP]}"
    )
    if screenshot is None:
        return text
    raw, mime = screenshot
    encoded = base64.b64encode(raw).decode("ascii")
    return [
        {
            "type": "text",
            "text": (
                text
                + "\n\nThe attached image is this same task's current rendered viewport. "
                "Its [eN] overlays correspond to the exact refs above. Use the image to "
                "understand what a human sees, but execute browser actions only by ref."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
    ]


async def run_navigator_search(
    *,
    client: Any,
    settings: Any,
    provider: str,
    target: dict[str, Any],
    topic: str,
    max_turns: int = MAX_MODEL_TURNS,
    action_budget: Optional[int] = None,
) -> dict[str, Any]:
    create_body: dict[str, Any] = {
        "action": "create_task",
        "provider": provider,
        "target": target,
        "topic": topic,
    }
    if action_budget is not None:
        create_body["action_budget"] = action_budget
    created = await scrapex_svc.navigator(settings, create_body)
    if not (created.get("success") and created.get("verified")):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "reason": (
                "Could not start a Navigator task: "
                f"{(created.get('error') or {}).get('message') or created.get('status')}"
            ),
            "create_task_result": created,
        }
    task_id = str(created["data"]["id"])

    initial_observation = await scrapex_svc.navigator(
        settings, {"action": "observe", "task_id": task_id}
    )
    if not initial_observation.get("success"):
        return {
            "attempted": True,
            "searched": False,
            "verified": False,
            "task_id": task_id,
            "reason": (
                "Could not observe the initial Navigator page: "
                f"{(initial_observation.get('error') or {}).get('message') or initial_observation.get('status')}"
            ),
        }

    initial_summary = _observation_summary(initial_observation)
    initial_screenshot = await _task_screenshot(settings, task_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(target, topic)},
        {
            "role": "user",
            "content": _visual_observation_content(
                f"Find the ALLDATA procedure for {_target_label(target)}: {topic}.",
                initial_summary,
                initial_screenshot,
            ),
        },
    ]
    trace: list[dict[str, Any]] = [{"turn": -1, "action": "observe", "result": _observation_summary(initial_observation)}]
    stopped_reason = "model_finished"
    last_failed_call: Optional[tuple[str, tuple[tuple[str, Any], ...]]] = None
    repeated_failure_count = 0
    model_called_done = False

    for turn in range(max_turns):
        try:
            events = [
                event
                async for event in client.stream(
                    messages, tools=[NAVIGATOR_AGENT_TOOL_SCHEMA], max_tokens=500
                )
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
                "function": {"name": "navigator_browse", "arguments": call.get("arguments") or "{}"},
            }
            for index, call in enumerate(calls)
        ]
        messages.append({"role": "assistant", "content": content or None, "tool_calls": wire_calls})

        turn_hit_repeat_limit = False
        latest_visual_summary: Optional[dict[str, Any]] = None
        for call_index, (call, wire_call) in enumerate(zip(calls, wire_calls)):
            try:
                args = json.loads(call.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            action = str(args.get("action") or "").casefold()
            validation_error = _validate_args(action, args) if action in _NAV_ACTIONS else None
            dispatched = False

            if call_index > 0:
                result: dict[str, Any] = {
                    "error": (
                        "Only the first browser action from this model turn was executed. "
                        "Inspect the refreshed observation before choosing another action."
                    )
                }
            elif action not in _NAV_ACTIONS:
                result = {
                    "error": f"'{action or '(missing)'}' is not a navigator action available to this loop."
                }
            elif validation_error:
                result = {"error": validation_error}
            else:
                dispatch_args = {"action": action, "task_id": task_id}
                if action == "click":
                    dispatch_args["ref"] = args.get("ref")
                elif action == "fill":
                    dispatch_args["ref"] = args.get("ref")
                    dispatch_args["text"] = args.get("text")
                elif action == "press":
                    dispatch_args["ref"] = args.get("ref")
                    dispatch_args["key"] = args.get("key")
                elif action == "open":
                    dispatch_args["url"] = args.get("url")
                dispatched = True
                navigator_result = await scrapex_svc.navigator(settings, dispatch_args)
                if navigator_result.get("success"):
                    result = _observation_summary(navigator_result)
                    if action != "done":
                        latest_visual_summary = result
                else:
                    result = {
                        "error": (navigator_result.get("error") or {}).get("message")
                        or f"navigator {action} failed: {navigator_result.get('status')}"
                    }

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
                        f"call and got the exact same error again. Re-read the live state and "
                        f"change the action or arguments. {call_error}"
                    ),
                }
            if call_error and repeated_failure_count >= 3:
                turn_hit_repeat_limit = True

            if action == "done" and dispatched and not call_error:
                model_called_done = True

            trace.append({
                "turn": turn,
                "action": action,
                "args": {k: v for k, v in args.items() if k != "action"},
                "error": call_error,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": wire_call["id"],
                "content": json.dumps(result, default=str)[:_TOOL_RESULT_CHAR_BACKSTOP],
            })

        if latest_visual_summary is not None and not model_called_done:
            current_screenshot = await _task_screenshot(settings, task_id)
            messages.append(
                {
                    "role": "user",
                    "content": _visual_observation_content(
                        "Current rendered browser state after the executed action. "
                        "Reason from this new state and choose the next action yourself.",
                        latest_visual_summary,
                        current_screenshot,
                    ),
                }
            )

        if model_called_done:
            stopped_reason = "model_done"
            break
        if turn_hit_repeat_limit:
            stopped_reason = "repeated_tool_error"
            break
    else:
        stopped_reason = "turn_budget_exhausted"

    # Deterministic epilogue -- ScrapeX's own verify action is the single
    # authority on evaluate_navigation_claim; this loop never recomputes
    # browser semantics or text-matching itself.
    verification = await scrapex_svc.navigator(settings, {"action": "verify", "task_id": task_id})
    proof = verification.get("data") if isinstance(verification.get("data"), dict) else {}
    verified = bool(proof.get("verified"))

    evidence_result = await scrapex_svc.navigator(
        settings, {"action": "get_evidence", "task_id": task_id}
    )
    evidence = (
        evidence_result.get("data")
        if isinstance(evidence_result.get("data"), dict)
        else {}
    )

    capture_result: dict[str, Any] | None = None
    captured = False
    if verified:
        # ScrapeX owns the provider browser and therefore owns the final
        # verified-page capture. X never re-opens the page in another profile
        # and never reconstructs the path from model narration.
        capture_result = await scrapex_svc.navigator_capture(settings, task_id)
        captured = bool(
            capture_result.get("success") is True
            and capture_result.get("verified") is True
            and capture_result.get("work_complete") is True
        )

    return {
        "attempted": True,
        "searched": len(trace) > 1,
        "verified": verified,
        "captured": captured,
        "capture": capture_result,
        "verification_reason": proof.get("reason"),
        "verification": proof,
        "task_id": task_id,
        "provider": provider,
        "target": target,
        "topic": topic,
        "agent_trace": trace,
        "agent_stopped_reason": stopped_reason,
        "source_url": evidence.get("source_url"),
        "extracted_text": (evidence.get("extracted_text") or "")[:20_000],
        "provenance": {
            "provider": provider,
            "licensed_session": True,
            "workflow": "model_navigator_agent",
        },
    }
