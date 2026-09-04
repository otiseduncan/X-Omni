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

import json
import logging
from typing import Any, Optional

from . import scrapex as scrapex_svc

log = logging.getLogger("xomni.research_navigator_agent")

MAX_MODEL_TURNS = 18
_NAV_ACTIONS = ("observe", "click", "fill", "press", "back", "open", "extract", "done")

NAVIGATOR_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "navigator_browse",
        "description": (
            "Operate a real, already-authenticated ALLDATA browser session one bounded "
            "action at a time. observe re-reads the current page without acting -- call it "
            "if you suspect the page changed (e.g. after a lazily-loaded menu) and want a "
            "fresh look before deciding what to do next. Every click/fill/press targets an "
            "element by its exact 'ref' copied verbatim from the most recent observe/action "
            "result's elements list -- never guess a ref, and never invent a role, name, or "
            "CSS selector; if the ref you want isn't in the latest list, call observe again. "
            "Workflow: (1) fill the vehicle search box with the requested vehicle and click "
            "the matching result to select it -- this is mandatory before any destination can "
            "count as relevant to it, (2) navigate toward the requested calibration/procedure "
            "topic, using 'back' to retreat out of any wrong branch, (3) once the actual "
            "procedure content -- not a menu or search-results listing -- is on screen, call "
            "extract. Call 'done' once you have extracted the relevant procedure, or once you "
            "are confident this exact vehicle/topic is not reachable -- do not keep acting once "
            "you already have enough information to answer."
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
        "knowledge -- only from what you actually observe in a tool result. Your final answer "
        "is independently checked against the real page afterward, so a claim that isn't backed "
        "by what the tools actually showed you will be discarded rather than trusted. If a tool "
        "call returns an error, read it and change what you send -- do not repeat the exact same "
        "call. If you cannot find this exact vehicle/topic after a reasonable number of steps, "
        "call 'done' and say so plainly instead of guessing."
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
    return {
        "url": data.get("url"),
        "title": data.get("title"),
        "elements": data.get("elements"),
        "loop_warning": data.get("loop_warning"),
        "backtrack_available": data.get("backtrack_available"),
        "repeated_action_warning": data.get("repeated_action_warning"),
    }


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

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(target, topic)},
        {
            "role": "user",
            "content": (
                f"Find the ALLDATA procedure for {_target_label(target)}: {topic}.\n\n"
                f"Initial observation: {json.dumps(_observation_summary(initial_observation), default=str)[:4_000]}"
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
                navigator_result = await scrapex_svc.navigator(settings, dispatch_args)
                if navigator_result.get("success"):
                    result = _observation_summary(navigator_result)
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
                        f"call and got the exact same error again. Re-read the error and change "
                        f"the arguments -- do not resend this call unchanged. {call_error}"
                    ),
                }
            if call_error and repeated_failure_count >= 3:
                turn_hit_repeat_limit = True

            if action == "done":
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
                "content": json.dumps(result, default=str)[:6_000],
            })

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

    evidence_result = await scrapex_svc.navigator(settings, {"action": "get_evidence", "task_id": task_id})
    evidence = evidence_result.get("data") if isinstance(evidence_result.get("data"), dict) else {}

    return {
        "attempted": True,
        "searched": len(trace) > 1,
        "verified": verified,
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
