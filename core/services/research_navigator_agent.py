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

ScrapeX Navigator is the production ALLDATA browser path. The model owns
perception and navigation; ScrapeX owns the isolated provider session,
bounded action execution, and verification.

Truthfulness never depends on the model's own narration of what it did.
After the loop ends (the model stops calling tools, sends "done", or the
turn budget runs out), ScrapeX's verify action is the single authority on
evaluate_navigation_claim, so "verified" means the same thing regardless
of how many turns the model actually used.
This loop never recomputes browser semantics itself; it only checks the
*shape* of what ScrapeX's contract-validated client returns.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from . import scrapex as scrapex_svc

log = logging.getLogger("xomni.research_navigator_agent")

# The active X model is bound only for the duration of one Registry handler
# invocation. This lets a composite operator capability delegate a bounded
# browser-navigation subtask back to the same model without global state
# or a second model process.
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
_NAV_ACTIONS = ("observe", "click", "fill", "press", "back", "open", "scroll", "wait", "extract", "done")
# Bounded at the element level, not by an outer character truncation --
# confirmed live against real ALLDATA search results (500+ entries): a flat
# json.dumps(...)[:N] cap cut the elements array off mid-object, so the
# model picked a ref from an incomplete list and selected the wrong
# vehicle. Capping the list itself keeps the fed-back JSON always complete.
MAX_ELEMENTS_FOR_MODEL = 120
_TOOL_RESULT_CHAR_BACKSTOP = 24_000
_INITIAL_OBSERVE_ATTEMPTS = 5
_INITIAL_OBSERVE_DELAY_SECONDS = 0.45
_FAILED_ACTION_OBSERVE_ATTEMPTS = 4
_FAILED_ACTION_OBSERVE_DELAY_SECONDS = 0.35

# --- Transcript budget ----------------------------------------------------
#
# This loop's own transcript is the only thing competing for the local
# worker's 32K context, and it used to grow without bound: every turn
# appended the same observation twice -- once as the tool result, once as
# the visual user message -- plus a fresh screenshot, and nothing was ever
# dropped.
#
# Measured live on 2026-09-12 against the real ALLDATA vehicle picker, a
# single observation tokenized at 3.1k-5.7k tokens on the worker itself, so
# four browser actions reached ~32.6k tokens of text before one image was
# counted, and llama-server rejected the fifth model call with HTTP 400.
# Every Navigator task ScrapeX has ever recorded died that way -- 2 to 5
# actions in, all of them still on the vehicle picker, none ever reaching
# the service-information tree that actually needs the reasoning.
#
# So only the newest observation is carried in full; superseded ones
# collapse to a one-line digest of what was done and where it led. That is
# not only a size win: a ref from a superseded observation is stale, and
# ScrapeX rejects it, so keeping old element maps in context does nothing
# but offer the model refs it must not use.
#
# Two live measurements, not one: the ref-dense vehicle-picker JSON runs
# ~2.2 characters per token (opaque refs like "f8e397" tokenize badly),
# while an ALLDATA procedure article -- mostly prose -- runs ~3.4. A single
# 2.2 calibrated only on the picker over-charged the article by 60%: on
# 2026-09-12 a 35k-character article page was estimated at 15.9k tokens
# when the worker actually reported 10.2k, so the backstop below fired and
# stripped the screenshots from turn 13 onward while 22k of context sat
# unused -- losing the visual channel exactly where a long procedure needed
# it. 2.5 sits under both measurements, so it still over-estimates prose
# while no longer inventing pressure that is not there.
_CHARS_PER_TOKEN_ESTIMATE = 2.5
# One annotated JPEG viewport, charged as a flat estimate rather than
# measured; it is a backstop input, not an accounting record.
_IMAGE_TOKEN_ESTIMATE = 1_200
# The real ceiling is the worker's 32,768, and one observation plus one
# image measured 4.1k-10.2k live across 21 turns. At the most pessimistic
# measured ratio this budget still lands near 27k with the image, system
# prompt, tool schema and generation counted -- inside the ceiling with
# margin, and high enough that a long procedure page no longer costs the
# screenshots.
_TRANSCRIPT_TOKEN_BUDGET = 22_000
_DIGEST_CHAR_CAP = 260
# Three in a row is reading; more than that is drifting.
_SCROLL_NUDGE_AFTER = 3
_TRUNCATION_NOTICE = (
    "\n\n[This observation was cut to fit the model's context. What is shown is "
    "complete up to the cut; if what you need is missing, narrow the page with a "
    "search or a more specific menu rather than guessing a ref that is not listed.]"
)

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
            "menu or results list. Every extract is immediately checked by ScrapeX; if the "
            "candidate is rejected, use the returned verification gates/reason to backtrack "
            "and explore a different branch. Call done only when the observed site state "
            "shows the exact goal is not reachable."
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
                "delta_y": {
                    "type": "integer",
                    "minimum": -1600,
                    "maximum": 1600,
                    "description": "Only used by scroll; positive scrolls down, negative scrolls up.",
                },
                "milliseconds": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 2500,
                    "description": "Only used by wait for bounded client-rendered content settling.",
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
        "choose the next browser action from the live page state and reassess after every turn. "
        "A task-bound annotated screenshot accompanies each "
        "observation when available; labels such as [e12] on the image are the same exact refs "
        "listed in the structured observation. Use pixels to understand layout, grouping, "
        "selected state, menus, and drill-down context, but act only by an observed ref. The "
        "browser will be re-observed after each executed action, so choose one action at a time "
        "and then reassess. Do not require exact article-title wording: OEMs may express the same "
        "intent as calibration, aiming, alignment, adjustment, initialization, relearn, setup, "
        "registration, learn, or zero-point procedures, and system names also vary. Use the live "
        "page context to reason semantically while preserving the exact requested vehicle/system. "
        "Your final claim is independently checked against the real page. After extract, read the "
        "verification feedback; if it is rejected, correct course instead of declaring success. "
        "If a tool call returns an error, adapt to the observed state rather than repeating it. "
        "On ALLDATA's vehicle picker, prefer its full-vehicle search box (for example, "
        "'Search by Year, Make, Model, Engine, or VIN') when that box is visible. Fill it "
        "with the exact requested year, make, and model and let ALLDATA resolve its own "
        "make taxonomy; do not invent or hardcode make aliases to drive separate dropdowns. "
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
    if action == "scroll":
        delta_y = args.get("delta_y")
        if (
            isinstance(delta_y, bool)
            or not isinstance(delta_y, int)
            or not -1600 <= delta_y <= 1600
        ):
            return "scroll requires integer 'delta_y' from -1600 to 1600."
    if action == "wait":
        milliseconds = args.get("milliseconds")
        if (
            isinstance(milliseconds, bool)
            or not isinstance(milliseconds, int)
            or not 100 <= milliseconds <= 2500
        ):
            return "wait requires integer 'milliseconds' from 100 to 2500."
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
        "breadcrumb": data.get("breadcrumb"),
        # ScrapeX bounds this to 8k chars while building the observation.
        # This is the semantic content X was previously missing when it had
        # only labels/refs and a screenshot to reason from.
        "page_text": str(data.get("page_text") or ""),
        # ...and whether that bound actually cut anything, plus where the
        # viewport sits in the document. A long OEM procedure runs past 8k
        # and its calibration specifications sit at the bottom, so "the text
        # ends here" and "the page ends here" have to be distinguishable.
        # Confirmed live on 2026-09-12: the Palisade front-radar article came
        # back cut mid-word at exactly 8000 chars and read as complete.
        "page_text_truncated": data.get("page_text_truncated"),
        "page_text_total_chars": data.get("page_text_total_chars"),
        "scroll_position": data.get("scroll_position"),
        "elements": elements,
        "loop_warning": data.get("loop_warning"),
        "backtrack_available": data.get("backtrack_available"),
        "repeated_action_warning": data.get("repeated_action_warning"),
    }
    if truncated:
        summary["elements_truncated"] = (
            f"Only the first {MAX_ELEMENTS_FOR_MODEL} of "
            f"{len(data['elements'])} elements are shown. Never guess a ref "
            "that isn't in this list. If what you need isn't here, scroll to "
            "bring more of this page into the list, or narrow the page (a "
            "search, or a more specific menu)."
        )

    # Turn the raw signals into the one sentence that matters. Without this
    # the model read a procedure cut mid-word at 8000 characters as the whole
    # procedure, and clicked around the part it could see instead of scrolling
    # to the specifications at the bottom.
    position = summary.get("scroll_position") or {}
    more_below = bool(position) and not position.get("at_page_bottom")
    if summary.get("page_text_truncated") or more_below:
        parts = []
        if summary.get("page_text_truncated"):
            total = summary.get("page_text_total_chars")
            parts.append(
                "The page text above is CUT SHORT"
                + (f" -- this page holds about {total} characters" if total else "")
                + " and the rest is not shown here."
            )
        if more_below:
            parts.append("The viewport is not at the bottom of the page.")
        parts.append(
            "Content you are looking for may be further down -- OEM procedures "
            "put specifications, target dimensions and distances near the end. "
            "Scroll to bring more into view, and call extract the moment what "
            "you need is on screen. Do NOT try to reach the bottom: this page "
            "loads more as you scroll, so its end moves and scrolling alone "
            "never finishes."
        )
        summary["page_continues"] = " ".join(parts)
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


def _observation_fingerprint(summary: dict[str, Any]) -> str:
    """A stable identity for one rendered page state.

    Used only to tell the model, factually, whether its last action moved
    the page. It compares what the model was actually shown -- url, title,
    and the ref/role/name of every listed element -- and makes no judgement
    about what the page means or what should be clicked next.
    """
    elements = summary.get("elements")
    listed = (
        [
            f"{item.get('ref')}:{item.get('role')}:{item.get('name')}"
            for item in elements
            if isinstance(item, dict)
        ]
        if isinstance(elements, list)
        else []
    )
    return json.dumps(
        [summary.get("url"), summary.get("title"), listed], default=str, sort_keys=True
    )


def _observation_ready(summary: dict[str, Any]) -> bool:
    """Whether ScrapeX has exposed enough rendered state for a real action.

    A Navigator task can be created while its provider page is still settling.
    The live ALLDATA race returned only the title ``ALLDATA`` with no text or
    elements; sending that to the model produced invented refs until the turn
    budget expired. A content page may legitimately have no controls, so page
    text or a breadcrumb also establishes readiness.
    """

    elements = summary.get("elements")
    return bool(
        (isinstance(elements, list) and elements)
        or str(summary.get("page_text") or "").strip()
        or summary.get("breadcrumb")
    )


async def _observe_until_ready(
    settings: Any,
    task_id: str,
    *,
    attempts: int,
    delay_seconds: float,
    previous_fingerprint: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Return the newest usable observation after bounded page-settle polling."""

    latest_result: dict[str, Any] = {}
    latest_summary: dict[str, Any] = {}
    for attempt in range(1, max(1, attempts) + 1):
        latest_result = await scrapex_svc.navigator(
            settings, {"action": "observe", "task_id": task_id}
        )
        if not latest_result.get("success"):
            return latest_result, _observation_summary(latest_result), attempt
        latest_summary = _observation_summary(latest_result)
        ready = _observation_ready(latest_summary)
        changed = (
            previous_fingerprint is None
            or _observation_fingerprint(latest_summary) != previous_fingerprint
        )
        if ready and changed:
            return latest_result, latest_summary, attempt
        if attempt < max(1, attempts):
            await asyncio.sleep(max(0.0, delay_seconds))
    return latest_result, latest_summary, max(1, attempts)


def _navigator_failure_message(result: dict[str, Any], action: str) -> str:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    message = str(error.get("message") or f"navigator {action} failed: {result.get('status')}")
    detail = result.get("detail")
    if detail not in (None, ""):
        try:
            detail_text = json.dumps(detail, default=str, sort_keys=True)
        except (TypeError, ValueError):
            detail_text = str(detail)
        if detail_text and detail_text not in message:
            message = f"{message} Detail: {detail_text}"
    return message


def _can_refresh_after_failure(result: dict[str, Any]) -> bool:
    """True only when ScrapeX definitively rejected the requested action."""

    return bool(
        result.get("success") is False
        and result.get("executed") is False
        and result.get("may_have_executed") is not True
        and (
            result.get("http_status") in {409, 422}
            or result.get("status") in {"conflict", "invalid_request"}
        )
    )


def _action_digest(
    ordinal: int,
    action: str,
    args: dict[str, Any],
    summary: Optional[dict[str, Any]],
    *,
    unchanged: bool = False,
) -> str:
    """The one line that stands in for a superseded full observation.

    This is the loop's navigation memory once the old element maps are
    dropped: what was tried, in order, and where each attempt landed.
    Without it, collapsing the transcript would leave the model free to
    re-try an action it has already watched do nothing. A failed action
    needs no digest -- its error stays in its own tool receipt, which is
    small and never collapsed.
    """
    detail = " ".join(
        f"{key}={value}"
        for key, value in sorted(args.items())
        if key != "action" and value not in (None, "")
    )
    head = " ".join(part for part in (f"[action {ordinal}]", action, detail) if part)
    where = ""
    if summary:
        title = str(summary.get("title") or "")
        where = " ".join(
            part
            for part in (str(summary.get("url") or ""), f'"{title}"' if title else "")
            if part
        )
    outcome = "page unchanged" if unchanged else "new page state"
    return f"{head} -> {outcome}: {where}".rstrip()[:_DIGEST_CHAR_CAP]


def _tool_receipt(
    result: Any,
    *,
    action: str = "",
    args: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """What the tool role carries now that the observation lives in one place.

    The full observation rides in exactly one message per turn -- the visual
    user message -- so this is an execution receipt, not a second copy of
    it. Everything the model must act on rather than merely see is kept
    verbatim: the error text, and ScrapeX's post-extract verification
    verdict with its instruction.
    """
    if not isinstance(result, dict):
        return {"error": "The navigator returned no usable result."}
    if result.get("error"):
        return {"error": result["error"]}
    # Name the action that just happened and say it is finished. On an SPA
    # the url and title frequently do not move when a click opens a panel or
    # a menu, and a receipt that only echoed those read as "nothing
    # happened": across every live run on 2026-09-12 the model re-issued
    # almost every click it had just made successfully, spending about half
    # of each budget on duplicates. The refreshed page arrives in the very
    # next message, so the receipt's job is to close the action, not to
    # describe the page.
    performed = " ".join(
        part
        for part in (
            action,
            str((args or {}).get("ref") or ""),
        )
        if part
    ).strip()
    receipt: dict[str, Any] = {
        "executed": True,
        "completed_action": performed or action or "action",
        "do_not_repeat": (
            f"'{performed}' has already been carried out. Do not send it again. "
            "The refreshed page state follows in the next message -- read it and "
            "choose your NEXT action from it."
        ),
        "url": result.get("url"),
        "title": result.get("title"),
    }
    for key in (
        "verification_after_extract",
        "next_instruction",
        "loop_warning",
        "repeated_action_warning",
        "elements_truncated",
    ):
        if result.get(key) is not None:
            receipt[key] = result[key]
    return receipt


def _estimated_tokens(content: Any) -> int:
    """Cheap local size estimate for one message's content.

    Deliberately not a call to the worker's tokenizer: this runs before
    every model turn and only has to be right enough to keep the backstop
    below from firing late.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return int(len(content) / _CHARS_PER_TOKEN_ESTIMATE)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                total += _IMAGE_TOKEN_ESTIMATE
            else:
                total += int(len(str(part.get("text") or "")) / _CHARS_PER_TOKEN_ESTIMATE)
        return total
    return int(len(str(content)) / _CHARS_PER_TOKEN_ESTIMATE)


def _estimated_message_tokens(message: dict[str, Any]) -> int:
    """Size of one whole message, tool-call arguments included.

    Counting only "content" would leave the assistant turns uncounted, and
    those carry the tool-call arguments -- small individually, but a blind
    spot that grows with every turn is exactly what this budget exists to
    stop having.
    """
    total = _estimated_tokens(message.get("content"))
    calls = message.get("tool_calls")
    if calls:
        total += _estimated_tokens(json.dumps(calls, default=str))
    return total


def _collapse_superseded_observations(
    messages: list[dict[str, Any]],
    slots: list[int],
    digests: dict[int, str],
) -> None:
    """Replace every observation but the newest with its one-line digest.

    Idempotent, and safe to run before every model turn: each slot is
    rewritten from the digest recorded when that observation arrived, so
    running it again changes nothing.
    """
    for index in slots[:-1]:
        messages[index]["content"] = digests.get(index) or "[superseded observation]"


def _strip_images(messages: list[dict[str, Any]]) -> bool:
    stripped = False
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kept = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "image_url")
        ]
        if len(kept) != len(content):
            message["content"] = kept
            stripped = True
    return stripped


def _enforce_transcript_budget(messages: list[dict[str, Any]]) -> bool:
    """Backstop for a page large enough to blow the budget on its own.

    With superseded observations collapsed this should not fire -- one
    observation plus one image measured well inside the budget on the real
    ALLDATA pages. It exists because the alternative to firing is an HTTP
    400 from the worker that ends the task outright, and a degraded turn is
    worth more than no turn. The image goes first, since the element map is
    the only thing an action can be built from; only then is observation
    text cut, with the cut declared in place so the model knows not to
    trust the list as complete.
    """

    def total() -> int:
        return sum(_estimated_message_tokens(message) for message in messages)

    if total() <= _TRANSCRIPT_TOKEN_BUDGET:
        return False

    degraded = _strip_images(messages)
    if total() <= _TRANSCRIPT_TOKEN_BUDGET:
        return degraded

    for message in reversed(messages):
        content = message.get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    break
        if text is None or len(text) < 2_000:
            continue
        over_tokens = total() - _TRANSCRIPT_TOKEN_BUDGET
        keep_chars = max(
            1_500, len(text) - int(over_tokens * _CHARS_PER_TOKEN_ESTIMATE) - 400
        )
        if keep_chars >= len(text):
            continue
        trimmed = text[:keep_chars] + _TRUNCATION_NOTICE
        if isinstance(content, str):
            message["content"] = trimmed
        else:
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = trimmed
                    break
        degraded = True
        if total() <= _TRANSCRIPT_TOKEN_BUDGET:
            break

    return degraded


async def run_navigator_search(
    *,
    client: Any,
    settings: Any,
    provider: str,
    target: dict[str, Any],
    topic: str,
    max_turns: int = MAX_MODEL_TURNS,
    action_budget: Optional[int] = None,
    capture: bool = False,
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

    (
        initial_observation,
        initial_summary,
        initial_observe_attempts,
    ) = await _observe_until_ready(
        settings,
        task_id,
        attempts=_INITIAL_OBSERVE_ATTEMPTS,
        delay_seconds=_INITIAL_OBSERVE_DELAY_SECONDS,
    )
    if not initial_observation.get("success"):
        if initial_observation.get("status") == "authentication_required":
            return {
                "status": "authentication_required",
                "provider": provider,
                "attempted": True,
                "searched": False,
                "verified": False,
                "captured": False,
                "requires_human": True,
                "task_id": task_id,
                "reason": str(
                    initial_observation.get("message")
                    or "ALLDATA requires interactive authentication."
                ),
                "navigator": initial_observation,
            }
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

    if not _observation_ready(initial_summary):
        return {
            "status": "initial_page_not_ready",
            "provider": provider,
            "attempted": True,
            "searched": False,
            "verified": False,
            "captured": False,
            "task_id": task_id,
            "initial_observe_attempts": initial_observe_attempts,
            "reason": (
                "The Navigator task started, but its provider page did not expose "
                "any readable text, breadcrumb, or actionable elements before the "
                "bounded readiness window ended. No browser action was attempted."
            ),
            "navigator": initial_observation,
        }

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
    trace: list[dict[str, Any]] = [
        {
            "turn": -1,
            "action": "observe",
            "attempts": initial_observe_attempts,
            "result": initial_summary,
        }
    ]
    stopped_reason = "model_finished"
    last_failed_call: Optional[tuple[str, tuple[tuple[str, Any], ...]]] = None
    repeated_failure_count = 0
    model_called_done = False
    candidate_verified = False

    # Which messages hold a full observation, and the one line each collapses
    # to once a newer one arrives. The opening message carries the goal as
    # well as the first page, so its digest restates the goal -- the system
    # prompt holds the authoritative copy either way.
    observation_slots: list[int] = [len(messages) - 1]
    slot_digests: dict[int, str] = {
        len(messages) - 1: (
            f"[initial page] goal: {_target_label(target)} -- {topic} -> "
            f"{initial_summary.get('url') or ''}"
        )[:_DIGEST_CHAR_CAP]
    }
    previous_fingerprint = _observation_fingerprint(initial_summary)
    action_ordinal = 0
    context_degraded = False
    # A working scroll on a lazily-loaded procedure is its own trap: the live
    # Palisade article grew from 15,676px to 18,116px while being scrolled, so
    # "keep going until the bottom" spent all 40 turns and never extracted.
    # Reading is what scrolling is for; this counts how long it has been since
    # any reading happened.
    consecutive_scrolls = 0

    for turn in range(max_turns):
        # Bound the transcript before every model call, not after the worker
        # has already refused one. Only the newest observation stays whole;
        # refs in the older ones are stale and ScrapeX would reject them.
        _collapse_superseded_observations(messages, observation_slots, slot_digests)
        if _enforce_transcript_budget(messages):
            context_degraded = True
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
        latest_action_args: Optional[tuple[str, dict[str, Any]]] = None
        latest_visual_is_failure_refresh = False
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
                elif action == "scroll":
                    dispatch_args["delta_y"] = args.get("delta_y")
                elif action == "wait":
                    dispatch_args["milliseconds"] = args.get("milliseconds")
                dispatched = True
                action_ordinal += 1
                consecutive_scrolls = consecutive_scrolls + 1 if action == "scroll" else 0
                navigator_result = await scrapex_svc.navigator(settings, dispatch_args)
                if navigator_result.get("success"):
                    result = _observation_summary(navigator_result)
                    if action != "done":
                        latest_visual_summary = result
                        latest_action_args = (
                            action,
                            {key: value for key, value in args.items() if key != "action"},
                        )

                    # Close the reasoning loop at the moment X proposes a
                    # candidate procedure. ScrapeX remains the truth authority:
                    # if the candidate fails any vehicle/subject/leaf/content
                    # gate, feed that proof straight back to X while the live
                    # page and navigation history are still available.
                    if action == "extract":
                        candidate_check = await scrapex_svc.navigator(
                            settings, {"action": "verify", "task_id": task_id}
                        )
                        candidate_proof = (
                            candidate_check.get("data")
                            if isinstance(candidate_check.get("data"), dict)
                            else {}
                        )
                        candidate_verified = bool(candidate_proof.get("verified"))
                        result["verification_after_extract"] = {
                            "verified": candidate_verified,
                            "reason": candidate_proof.get("reason"),
                            "vehicle_verified": candidate_proof.get("vehicle_verified"),
                            "subject_verified": candidate_proof.get("subject_verified"),
                            "procedure_leaf_verified": candidate_proof.get("procedure_leaf_verified"),
                            "content_extracted": candidate_proof.get("content_extracted"),
                            "matched_terms": candidate_proof.get("matched_terms"),
                        }
                        result["next_instruction"] = (
                            "Candidate verified. Stop browsing; verified evidence has been reached."
                            if candidate_verified
                            else (
                                "Candidate rejected by ScrapeX verification. Use the failed gates/reason "
                                "and current page state to backtrack or choose a different branch; do not "
                                "repeat extract on the same unchanged page."
                            )
                        )
                        trace.append(
                            {
                                "turn": turn,
                                "action": "verify_after_extract",
                                "verified": candidate_verified,
                                "reason": candidate_proof.get("reason"),
                            }
                        )
                else:
                    result = {
                        "error": _navigator_failure_message(navigator_result, action)
                    }
                    # A 409/422 is a definitive rejection: the requested
                    # action did not execute. The page can still have changed
                    # while ALLDATA was settling, which makes every ref from
                    # the prior observation stale. Re-observe and show that
                    # fresh state instead of letting one rejection consume
                    # the remaining model turns. Indeterminate failures never
                    # enter this path.
                    if _can_refresh_after_failure(navigator_result):
                        (
                            refreshed,
                            refreshed_summary,
                            refresh_attempts,
                        ) = await _observe_until_ready(
                            settings,
                            task_id,
                            attempts=_FAILED_ACTION_OBSERVE_ATTEMPTS,
                            delay_seconds=_FAILED_ACTION_OBSERVE_DELAY_SECONDS,
                            previous_fingerprint=previous_fingerprint,
                        )
                        if refreshed.get("success") and _observation_ready(
                            refreshed_summary
                        ):
                            latest_visual_summary = refreshed_summary
                            latest_action_args = (
                                f"observe after rejected {action}",
                                {"attempts": refresh_attempts},
                            )
                            latest_visual_is_failure_refresh = True
                            result["fresh_observation_supplied"] = True
                            result["refresh_attempts"] = refresh_attempts

            call_error = (result or {}).get("error") if isinstance(result, dict) else None
            # A rejected extract is a repeat worth catching. The action itself
            # succeeds -- only ScrapeX's verification refuses it -- so without
            # this the repeat guard never sees it: the live run on 2026-09-12
            # submitted the SAME extract 40 times, was refused 40 times, and
            # burned the whole budget. Counting it here routes it through the
            # same escalation as any other repeated mistake, while leaving the
            # verification feedback in the result untouched.
            if (
                not call_error
                and action == "extract"
                and dispatched
                and isinstance(result, dict)
                and result.get("verification_after_extract") is not None
                and not candidate_verified
            ):
                call_error = str(
                    (result.get("verification_after_extract") or {}).get("reason")
                    or "This candidate was rejected by verification."
                )
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
            # A receipt, not a second copy of the observation: the full
            # page state rides in the visual user message appended below, so
            # sending it here too was doubling the cost of every turn.
            messages.append({
                "role": "tool",
                "tool_call_id": wire_call["id"],
                "content": json.dumps(
                    _tool_receipt(
                        result,
                        action=action,
                        args={k: v for k, v in args.items() if k != "action"},
                    ),
                    default=str,
                )[:_TOOL_RESULT_CHAR_BACKSTOP],
            })

        if latest_visual_summary is not None and not model_called_done:
            current_screenshot = await _task_screenshot(settings, task_id)
            fingerprint = _observation_fingerprint(latest_visual_summary)
            unchanged = fingerprint == previous_fingerprint
            previous_fingerprint = fingerprint
            heading = (
                (
                    "The requested browser action was rejected and did not execute. "
                    "This is a fresh observation of the current rendered page. Use only "
                    "refs from this state and choose the next action yourself."
                )
                if latest_visual_is_failure_refresh
                else (
                    "Current rendered browser state after the executed action. "
                    "Reason from this new state and choose the next action yourself."
                )
            )
            if consecutive_scrolls >= _SCROLL_NUDGE_AFTER:
                heading += (
                    f" You have now scrolled {consecutive_scrolls} times in a row "
                    "without extracting anything. Scrolling is for reading, and "
                    "this page keeps loading more, so it has no end to reach. If "
                    "the procedure content you were sent for is on screen, call "
                    "extract NOW. If this page is the wrong one, go back and "
                    "choose a different branch."
                )
            if unchanged:
                # Stated as an observed fact, not a hint about what to click.
                # Three identical scrolls in a row on the live vehicle picker
                # (2026-09-12) is what this exists to stop: ScrapeX's own
                # loop warning covers its navigation graph, but an action that
                # leaves the rendered page byte-identical never reached it.
                heading += (
                    " That action left the page exactly as it was: same URL, same "
                    "title, same elements. Repeating it will not change anything -- "
                    "act on a different element, or go back."
                )
            messages.append(
                {
                    "role": "user",
                    "content": _visual_observation_content(
                        heading,
                        latest_visual_summary,
                        current_screenshot,
                    ),
                }
            )
            slot = len(messages) - 1
            observation_slots.append(slot)
            digest_action, digest_args = latest_action_args or ("", {})
            slot_digests[slot] = _action_digest(
                action_ordinal,
                digest_action,
                digest_args,
                latest_visual_summary,
                unchanged=unchanged,
            )

        if candidate_verified:
            stopped_reason = "verified_after_extract"
            break
        if model_called_done:
            stopped_reason = "model_done"
            break
        if turn_hit_repeat_limit:
            stopped_reason = "repeated_tool_error"
            break
    else:
        stopped_reason = "turn_budget_exhausted"

    # ScrapeX's own verify action is the single authority on
    # evaluate_navigation_claim; this loop never recomputes browser semantics
    # or text-matching itself.
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
    if verified and capture:
        # Persistence is a separate structured choice from research. ScrapeX
        # owns the provider browser and therefore owns the final verified-page
        # capture when the calling workflow explicitly requests preservation.
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
        "browser_actions_observed": len(observation_slots) - 1,
        "context_degraded": context_degraded,
        "source_url": evidence.get("source_url"),
        "extracted_text": (evidence.get("extracted_text") or "")[:20_000],
        "provenance": {
            "provider": provider,
            "licensed_session": True,
            "workflow": "model_navigator_agent",
        },
    }
