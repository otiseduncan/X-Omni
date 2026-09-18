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

Three things the loop keeps strictly apart:

* **Observation-bound action.** Every ref action names the observation it
  was chosen from; a control the accessibility tree does not expose can be
  reached through a numbered mark (``observe_marks`` then ``click_mark``) or,
  last, a point on the screenshot (``click_visual``). ScrapeX proves the
  target is still what was seen, or refuses; a refusal comes back here as a
  fresh observation, never as a substituted click.
* **Semantic review in a clean context.** When the model marks a page and
  ScrapeX's mechanical gates pass (vehicle selected, navigation happened,
  leaf reached, text extracted), the page's evidence goes to an independent
  reviewer that has never seen this loop's transcript
  (``research_semantic_review``). Only its structured acceptance makes a
  candidate count, and only then is anything captured.
* **Execution truth.** ScrapeX's verify action is the single authority on
  the mechanical claim, capture is the single authority on what was filed,
  and the research receipt records ids, URLs, decisions, and hashes -- not
  the model's narration of what it did.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

from . import alldata_sunset
from . import research_navigator_contract as contract
from . import scrapex as scrapex_svc
from .research_navigator_tool_repair import NavigatorToolRepairClient
from .research_semantic_review import accepted as review_accepted
from .research_semantic_review import review_candidate

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


# One Navigator run at a time: ScrapeX drives one provider browser, and two
# loops interleaving actions on it would each be acting from the other's
# page. A caller that finds the lock held gets a structured "busy" result
# instead of a wait that outlives a chat turn.
NAVIGATOR_LOCK = asyncio.Lock()

MAX_MODEL_TURNS = 40
# Dependencies a single objective may pursue, and the share of the turn
# budget each may use. These are resource limits, not a depth rule: a
# procedure whose prerequisite has its own prerequisite is followed as long
# as the budget holds, and running out is reported as incompleteness.
MAX_DEPENDENCIES = 3
DEPENDENCY_TURN_SHARE = 0.5
# Turns added per document a primary procedure requires, and the ceiling on
# the whole objective once they are added. The primary search keeps its full
# budget; required documents get capacity of their own afterwards.
DEPENDENCY_TURNS_EACH = 12
MAX_OBJECTIVE_TURNS = 76
# Non-progress accounting. Progress is a new page, a new candidate, a new
# dependency, a capture; non-progress is the same page again, a repeated or
# failed action, a stale-target refusal. Each non-progress turn costs one
# point (a repeated identical failure two), each progress turn refunds one,
# and the task stops as stalled at the limit. The hard turn ceiling above
# still applies regardless.
STALL_LIMIT = 8
_NAV_ACTIONS = (
    "observe", "observe_marks", "click", "type", "fill", "press", "back", "open",
    "scroll", "wait", "click_mark", "click_visual", "select_vehicle", "extract", "done",
)
_REF_ACTIONS = frozenset({"click", "fill", "type", "press"})
_OBSERVATION_BOUND_ACTIONS = frozenset({"click_mark", "click_visual"})
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
# Actions that ask the page to become a different page. ALLDATA answers
# the act call before the navigation lands, so the observation returned
# with it can still describe the page the action was leaving.
_PAGE_CHANGING_ACTIONS = frozenset(
    {"click", "press", "open", "back", "click_mark", "click_visual", "select_vehicle"}
)
_SETTLE_OBSERVE_ATTEMPTS = 4
_SETTLE_OBSERVE_DELAY_SECONDS = 0.35
_FAILED_ACTION_OBSERVE_DELAY_SECONDS = 0.35
# ScrapeX codes that mean "the target you named is no longer what you saw".
_STALE_CODES = ("stale_observation", "stale_target", "stale_visual_target", "stale_ref", "visual_frame_missing")

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
# The longest ALLDATA procedure measured here reaches its bottom in about
# ten scrolls, so a run longer than this is no longer heading anywhere.
_SCROLL_NUDGE_AFTER = 16
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
            "Prefer refs: every click/type/fill/press targets an exact 'ref' copied verbatim "
            "from the latest observation -- never invent a ref, role, label, or selector. "
            "When you can SEE a control in the screenshot but no ref reaches it, call "
            "observe_marks: the runtime numbers such controls [m21], [m22]... and you may "
            "click_mark one. Only if neither works, click_visual with x_norm/y_norm as "
            "fractions of the screenshot's width and height. Every action is bound to the "
            "observation you chose it from; if the page changed you get a fresh observation "
            "instead, so choose again from that. After each action the browser is "
            "re-observed. Keep the exact requested vehicle as a hard requirement; "
            "select_vehicle with the VIN selects it exactly when one is given. extract submits "
            "the current, fully read page as a candidate: it is not a claim that the page is "
            "right, it is checked mechanically by ScrapeX and judged by an independent "
            "reviewer, and you get both verdicts back. Once the observation reports the bottom "
            "of the page, a downward scroll is refused -- extract a plausible page or leave it. "
            "done is refused on a component landing page or an article page from which nothing "
            "has been submitted; call it only when you have genuinely explored and the "
            "procedure is not reachable. Always act through this tool: a prose reply does not "
            "end the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_NAV_ACTIONS)},
                "ref": {
                    "type": "string",
                    "description": (
                        "The exact element ref from the most recent observation -- required "
                        "by, and only used by, click/type/fill/press."
                    ),
                },
                "text": {"type": "string", "description": "Text to type -- only used by type/fill."},
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
                    "description": (
                        "Only used by scroll; positive scrolls down, negative scrolls up. "
                        "Do not use a positive value after at_page_bottom=true."
                    ),
                },
                "milliseconds": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 2500,
                    "description": "Only used by wait for bounded client-rendered content settling.",
                },
                "mark": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Only used by click_mark: a mark number from the latest observe_marks result.",
                },
                "x_norm": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Only used by click_visual: horizontal position as a fraction of the screenshot width.",
                },
                "y_norm": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Only used by click_visual: vertical position as a fraction of the screenshot height.",
                },
                "vin": {
                    "type": "string",
                    "description": "Only used by select_vehicle: the exact 17-character VIN the repair order carries.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

# Non-binding provider notes: what this provider has been observed to do.
# They are offered as context and may be ignored; nothing here routes.
#
# These describe how the provider's site is built, never what a procedure is
# called. An earlier version listed title words ("Programming and Relearning,
# Initialization, Learn...") and the live Tacoma BSM runs followed that list
# straight into a generic Programming and Relearning menu and submitted a
# "Repair Instruction - Initialization" page four times; Toyota's actual
# procedure is titled "Operation Check". Naming is the model's job, from the
# manufacturer's own terms, so no vocabulary lives here.
PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "alldata": (
        "Recent Vehicles on the picker reaches a vehicle worked on before in one click.",
        "ALLDATA files SUVs, trucks and vans under a '<Make> Truck' make; a Palisade may be "
        "under 'Hyundai Truck' rather than 'Hyundai'.",
        "Every vehicle page has an 'ADAS Quick Reference' in its Reference panel: a table of "
        "ADAS components with links into each component's procedures.",
        "Nissan/Infiniti front distance-sensor (ICC radar) material has been filed under "
        "Cruise Control.",
        "The vehicle picker is https://my.alldata.com/repair/#/select-vehicle (open it "
        "to change vehicle); its search box ignores programmatic fills but reacts to "
        "typed keystrokes, and a typed VIN resolves the exact vehicle on its own.",
        "Search boxes on ALLDATA pages respond to typed keystrokes: type the text, then "
        "press Enter.",
        "A component's own procedures are in that component's subsections -- Testing and "
        "Inspection (Programming and Relearning is inside it), Service and Repair, Removal "
        "and Replacement. The vehicle-wide Testing and Inspection lists only general items.",
        "A Parts and Labor page lists labor operations and times, never procedure steps. "
        "An operation named there is the manufacturer's name for the work: search for "
        "that name, or open the component's Testing and Inspection, instead of clicking on "
        "the labor page.",
    ),
}


async def _target_already_selected(
    settings: Any, provider: str, target: dict[str, Any]
) -> Optional[bool]:
    """Whether the live provider session is on this exact vehicle, or None.

    Best effort: a provider without the read, an unreachable service, or a
    malformed answer all mean "not known", never "not selected".
    """
    reader = getattr(scrapex_svc, "navigator_current_target_signal", None)
    if reader is None:
        return None
    try:
        result = await reader(settings, provider, target)
    except Exception as exc:  # noqa: BLE001 - a missing fact is not a failure
        log.debug("target signal unavailable for %s: %s", provider, exc)
        return None
    if not isinstance(result, dict) or result.get("success") is not True:
        return None
    selected = (result.get("data") or {}).get("selected")
    return bool(selected) if isinstance(selected, bool) else None


def _vehicle_selection_note(selected: Optional[bool], target: dict[str, Any]) -> str:
    if selected is True:
        return " The provider session already has this exact vehicle selected."
    if selected is False:
        note = (
            " The provider session does NOT currently have this vehicle selected; the page "
            "below belongs to whatever vehicle was open last."
        )
        if target.get("vin"):
            note += " select_vehicle with the VIN above selects it exactly."
        return note
    return ""


def _target_label(target: dict[str, Any]) -> str:
    parts = [
        str(target.get(key)).strip()
        for key in ("year", "make", "model", "trim")
        if target.get(key) not in (None, "")
    ]
    label = " ".join(part for part in parts if part)
    return label or "the requested vehicle"


def _system_prompt(
    target: dict[str, Any],
    topic: str,
    objective: Optional[dict[str, Any]] = None,
    provider: str = "alldata",
    goal_note: str = "",
) -> str:
    label = _target_label(target)
    objective = objective or {}
    requirement = str(
        objective.get("requirement_label") or objective.get("system") or ""
    ).strip()
    component = str(objective.get("component") or "").strip()
    vin_line = (
        f"VIN: {target.get('vin')} -- the exact vehicle; select_vehicle with it selects that vehicle.\n"
        if target.get("vin")
        else ""
    )
    requirement_line = (
        f"Requirement (the shop's own label, from Calibration IQ): {requirement}\n"
        if requirement
        else ""
    )
    component_line = f"Component noted by the shop: {component}\n" if component else ""
    hints = PROVIDER_HINTS.get(provider, ())
    hint_block = (
        "\n\nPROVIDER NOTES (how this site is built; non-binding, verify on the live page):\n- "
        + "\n- ".join(hints)
        if hints
        else ""
    )
    goal_block = f"\n\n{goal_note}" if goal_note else ""
    return (
        "You are operating a licensed ALLDATA Repair/Collision browser session for a "
        "collision repair technician, through a bounded Navigator action interface. The "
        "session is already authenticated. Your very first tool call has already been "
        "answered with an observation of the current page -- read it before acting.\n\n"
        f"TASK\nVehicle: {label}\n{vin_line}{requirement_line}{component_line}Goal: {topic}\n\n"
        "TERMINOLOGY\n"
        "A requirement label is the repair shop's own wording. It is not ALLDATA's term and "
        "usually not the manufacturer's. Before you search or choose a menu, work out which "
        "system and component this manufacturer means by it, and what the manufacturer calls "
        "the service procedure a technician performs for that system after a repair or "
        "replacement. Search and navigate with the manufacturer's and ALLDATA's own names, and "
        "switch to the names the live pages show for this vehicle as soon as you see them. "
        "Use the shop's label as a search term only when it is also what the manufacturer "
        "calls the system.\n\n"
        "HOW TO FIND IT\n"
        "- When a search box is available for the selected vehicle, use it: type the "
        "manufacturer's name for the system or component and open results that belong to it.\n"
        "- ALLDATA's ADAS Quick Reference, when visible on this exact vehicle's page, is a "
        "manufacturer-native index of ADAS components with links into their procedures. It is "
        "often a better entry point than generic service menus. It is an option, not a route "
        "you must take.\n"
        "- A component page, a category list, or an article index is somewhere to keep going "
        "from, not an answer. Reaching the right system name is not completion.\n\n"
        "JUDGING A PAGE\n"
        "Manufacturer titles mislead in both directions. A page titled 'Operation Check', "
        "'Inspection', or 'Confirmation' can be the executable procedure, and a page titled "
        "'Initialization' can be unrelated generic material. Judge a page by what it actually "
        "has you do for this vehicle and system -- setup, tools, targets or reflectors, "
        "distances or angles, scan-tool steps, completion criteria -- not by whether its title "
        "repeats a word you were sent to find. A procedure page may never use the word "
        "'calibration' at all. When a page could be the procedure, read all of it: scroll to "
        "the bottom, then extract it.\n\n"
        "CANDIDATES\n"
        "extract submits the fully read current page as a CANDIDATE. It is not a claim that "
        "the page is right. ScrapeX checks it mechanically and an independent reviewer who "
        "sees only the evidence judges it; you get both verdicts back. If it is accepted, stop. "
        "If the reviewer names a required document, that is pursued next. If it is not "
        "accepted, the reviewer says why and which direction to take -- continue from there. "
        "Never submit a page that has already been reviewed and not accepted.\n\n"
        "COMPLETION\n"
        "done is refused on a component landing page, and on an article page, until you have "
        "submitted something for review from this task. Call done only when you have genuinely "
        "explored and the procedure is not reachable, and say plainly what you tried. A prose "
        "reply never ends the task -- always act through navigator_browse.\n\n"
        "MOVEMENT\n"
        "Changing pages is not automatically progress. If you return to a page state you have "
        "already seen, that is a cycle: change approach instead of drilling the same menus "
        "again. After the bottom of a page is reached, a downward scroll is refused; decide "
        "whether to extract the page or leave it.\n\n"
        "VEHICLE AND ACTIONS\n"
        "Do not substitute a different model, trim, or year, and do not answer from general "
        "knowledge -- only from what you observe. On ALLDATA's vehicle picker, prefer its "
        "full-vehicle search box (for example, 'Search by Year, Make, Model, Engine, or VIN') "
        "and let ALLDATA resolve its own make taxonomy; do not invent or hardcode make aliases "
        "to drive separate dropdowns. A task-bound annotated screenshot accompanies each "
        "observation when available; labels such as [e12] on it are the exact refs in the "
        "structured observation, and [m21]-style labels are marks you asked for. Use the "
        "pixels to understand layout, grouping, selected state, and menus, but act by ref first, "
        "by mark when a visible control has no ref, and by click_visual only as a last resort. "
        "Choose one action at a time; the browser is re-observed after each one. If an action "
        "returns an error, adapt to the observed state rather than repeating it."
        + hint_block
        + goal_block
    )


def _validate_args(action: str, args: dict[str, Any]) -> Optional[str]:
    if action in _REF_ACTIONS and not str(args.get("ref") or "").strip():
        return (
            f"{action} requires a non-empty 'ref' copied verbatim from the most recent "
            "observation's elements list."
        )
    if action in {"fill", "type"} and not str(args.get("text") or "").strip():
        return f"{action} requires a non-empty 'text' field with the value to type."
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
    if action == "click_mark":
        mark = args.get("mark")
        if isinstance(mark, bool) or not isinstance(mark, int) or mark < 1:
            return "click_mark requires integer 'mark' from the latest observe_marks result."
    if action == "click_visual":
        for key in ("x_norm", "y_norm"):
            value = args.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                return "click_visual requires 'x_norm' and 'y_norm' between 0 and 1."
    if action == "select_vehicle" and not str(args.get("vin") or "").strip():
        return "select_vehicle requires the 'vin' the repair order carries."
    return None


def _extract_content(events: list[dict[str, Any]]) -> str:
    return "".join(str(event.get("text") or "") for event in events if event.get("type") == "content")


def _extract_tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if event.get("type") == "tool_call"]


def _extract_usage(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for event in events:
        if event.get("type") == "usage":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            timings = event.get("timings") if isinstance(event.get("timings"), dict) else {}
            return {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": timings.get("cache_n"),
                "prompt_ms": timings.get("prompt_ms"),
                "predicted_ms": timings.get("predicted_ms"),
            }
    return None


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
    if isinstance(elements, list):
        # Boxes are for the runtime's binding checks and the screenshot
        # labels; the model relates refs to the picture through the labels.
        elements = [
            {key: value for key, value in item.items() if key != "box"} if isinstance(item, dict) else item
            for item in elements[:MAX_ELEMENTS_FOR_MODEL]
        ]
    truncated = isinstance(data.get("elements"), list) and len(data["elements"]) > MAX_ELEMENTS_FOR_MODEL
    summary: dict[str, Any] = {
        "observation_id": data.get("observation_id"),
        "url": data.get("url"),
        "title": data.get("title"),
        "breadcrumb": data.get("breadcrumb"),
        "viewport": data.get("viewport"),
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
    if data.get("marks"):
        summary["marks"] = data["marks"]
    if isinstance(data.get("controls_without_refs"), int) and data["controls_without_refs"] > 0:
        summary["controls_without_refs"] = data["controls_without_refs"]
    if isinstance(data.get("action_target"), dict):
        summary["action_target"] = data["action_target"]
    if data.get("action_detail"):
        summary["action_detail"] = data["action_detail"]
    if isinstance(data.get("extract"), dict):
        summary["extract"] = data["extract"]
    summary = {key: value for key, value in summary.items() if key != "observation_id" or value}
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
    if position and position.get("at_page_bottom"):
        summary["page_bottom_reached"] = (
            "You are at the BOTTOM of this page; there is nothing further down. "
            "If this page is the procedure for the requested vehicle and system, "
            "it is the source -- call extract now."
        )
        summary["bottom_decision_contract"] = {
            "scroll_down_allowed": False,
            "decision_required": True,
            "extract_is_candidate_submission": True,
            "instruction": (
                "The whole page has been reached. If it could plausibly be the requested "
                "procedure, extract it for independent review -- extract is not a claim that it "
                "is right. If it clearly is not, leave it and keep searching. Do not scroll down "
                "again, and do not come back to it without submitting it."
            ),
        }
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
            "SCROLL TO THE BOTTOM OF THIS PAGE. OEM procedures put the "
            "specifications -- target distances, reflector positions, "
            "clearances, dimensions -- at the very END, after the preparation "
            "steps. This page loads more as you scroll, so it grows as you go, "
            "but it does end: keep scrolling until you are told you are at the "
            "bottom. On this provider that takes roughly ten scrolls of 1600. "
            "Do not leave the page, and do not conclude it lacks what you need, "
            "before you have seen the bottom of it."
        )
        summary["page_continues"] = " ".join(parts)
    return summary


async def _task_screenshot(
    settings: Any, task_id: str, observation_id: Optional[str] = None
) -> Optional[tuple[bytes, str]]:
    """Best-effort visual observation; pixels never become the truth gate.

    Bound to the observation it accompanies: ScrapeX refuses a still for a
    superseded observation and echoes the bound id, which is what makes a
    later click_visual checkable against exactly this frame.
    """
    try:
        if observation_id:
            return await scrapex_svc.navigator_screenshot(settings, task_id, observation_id)
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
                "Its [eN] overlays correspond to the exact refs above, and any [mN] overlays "
                "to the marks listed. Use the image to understand what a human sees; act by "
                "ref, then mark, then click_visual only when nothing else reaches the control."
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


def _page_state(summary: dict[str, Any]) -> str:
    """Page identity plus where the viewport sits.

    A scroll that moves down a long procedure leaves url, title and the
    element list identical, so the fingerprint alone would call it an action
    that did nothing. Reading is what scrolling is for; position is part of
    the state.
    """
    position = summary.get("scroll_position") or {}
    return json.dumps(
        [_observation_fingerprint(summary), position.get("scroll_y"), position.get("scroll_height")],
        default=str,
    )


# Actions whose whole point is to change the page. Repeating one that leaves
# the page exactly as it was is the 2026-09-13 baseline's dominant waste: the
# same ref clicked 39 times, no error each time, the whole budget gone.
_EFFECT_EXPECTED_ACTIONS = frozenset(
    {"click", "click_mark", "click_visual", "fill", "type", "press", "back", "open", "scroll", "select_vehicle"}
)
# How many identical no-effect repeats before the loop says so, and before it
# stops. Two is a statement of fact; three is a task going nowhere.
_NO_EFFECT_NOTICE_AT = 2
_NO_EFFECT_LIMIT = 3


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


def _failure_code(result: dict[str, Any]) -> str:
    """The ScrapeX-side code of a refusal, when it carried one."""
    detail = result.get("detail")
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"])
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    text = json.dumps([error.get("message"), detail], default=str)
    for code in _STALE_CODES:
        if code in text:
            return code
    return str(error.get("code") or result.get("status") or "")


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
        if key not in {"action", "observation_id"} and value not in (None, "")
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
    verbatim: the error text, ScrapeX's post-extract verification verdict,
    the reviewer's verdict, and the instruction that follows from them.
    """
    if not isinstance(result, dict):
        return {"error": "The navigator returned no usable result."}
    if result.get("error"):
        receipt = {"error": result["error"]}
        if result.get("fallback_hint"):
            receipt["fallback_hint"] = result["fallback_hint"]
        return receipt
    # Name the action that just happened and say it is finished. On an SPA
    # the url and title frequently do not move when a click opens a panel or
    # a menu, and a receipt that only echoed those read as "nothing
    # happened": across every live run on 2026-09-12 the model re-issued
    # almost every click it had just made successfully, spending about half
    # of each budget on duplicates. The refreshed page arrives in the very
    # next message, so the receipt's job is to close the action, not to
    # describe the page.
    target_word = ""
    if args:
        for key in ("ref", "mark", "vin"):
            if args.get(key) not in (None, ""):
                target_word = str(args[key])
                break
    performed = " ".join(part for part in (action, target_word) if part).strip()
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
        "semantic_review",
        "no_effect_repeat",
        "next_instruction",
        "action_target",
        "action_detail",
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


# --------------------------------------------------------------- budgets


@dataclass
class _Budget:
    """Turns and progress accounting shared by one objective's tasks."""

    max_turns: int
    turns_used: int = 0
    stall_points: int = 0
    dependency_slots: int = MAX_DEPENDENCIES
    progress_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def turns_left(self) -> int:
        return max(0, self.max_turns - self.turns_used)

    def note(self, kind: str, *, progress: bool, cost: int = 1, **detail: Any) -> None:
        if progress:
            self.stall_points = max(0, self.stall_points - 1)
        else:
            self.stall_points += cost
        self.progress_events.append({"kind": kind, "progress": progress, **detail})

    @property
    def stalled(self) -> bool:
        return self.stall_points >= STALL_LIMIT


def _candidate_from_evidence(evidence: dict[str, Any], summary: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The evidence packet the reviewer sees: what the page itself said."""
    summary = summary or {}
    return {
        "title": evidence.get("title") or summary.get("title"),
        "url": evidence.get("source_url") or summary.get("url"),
        "breadcrumb": evidence.get("breadcrumb") or summary.get("breadcrumb") or [],
        "text": str(evidence.get("extracted_text") or summary.get("page_text") or ""),
        "text_truncated": bool(evidence.get("extracted_text_truncated") or summary.get("page_text_truncated")),
        "referenced_links": list(evidence.get("referenced_links") or []),
        "observation_id": evidence.get("observation_id") or summary.get("observation_id"),
        "text_sha256": evidence.get("extracted_text_sha256"),
    }


def _next_instruction_for_review(review: dict[str, Any]) -> str:
    decision = review.get("decision")
    summary = str(review.get("evidence_summary") or "")[:400]
    match = str(review.get("objective_match") or "")
    # A real page for the wrong target says which way to move, not only that
    # this page is wrong: the reviewer's own objective match separates "right
    # component, wrong article" from "wrong component or sensor altogether".
    if decision == "CONTINUE_SEARCH" and match in {"DIFFERENT_COMPONENT", "DIFFERENT_SENSOR_FAMILY"}:
        return (
            f"Independent review: this is a real page, but it performs a {match.replace('_', ' ').lower()}"
            f" rather than the requested system. Reviewer: {summary} Do not extract this page "
            "again and do not keep drilling in this branch. Back out using the live controls "
            "until other systems or components are visible, then choose the requested one."
        )
    if decision == "CONTINUE_SEARCH" and match == "SAME_COMPONENT_WRONG_PROCEDURE":
        return (
            "Independent review: right component, but this article does not perform the "
            f"requested operation. Reviewer: {summary} Stay with this component and look for a "
            "different article under it that does. Do not extract this page again."
        )
    if decision == "ACCEPT":
        return "Independent review ACCEPTED this page as the procedure. Stop browsing; the evidence has been reached."
    if decision == "ACCEPT_WITH_DEPENDENCIES":
        names = ", ".join(item.get("title", "") for item in review.get("dependencies") or [])
        return (
            "Independent review ACCEPTED this page and requires these documents as well: "
            f"{names}. Stop browsing this task; they are pursued next."
        )
    if decision == "FOLLOW_DEPENDENCY":
        names = ", ".join(item.get("title", "") for item in review.get("dependencies") or [])
        return (
            "Independent review: this page is not the procedure, but it names the document "
            f"that is: {names}. Stop browsing this task; that document is pursued next."
        )
    if decision == "CONTINUE_SEARCH":
        return (
            "Independent review: related, but NOT the requested procedure. "
            f"Reviewer: {summary} Keep searching from the current page; do not extract this page again."
        )
    if decision == "REJECT":
        return (
            "Independent review REJECTED this page as the wrong kind of document or wrong vehicle. "
            f"Reviewer: {summary} Go back and choose a different branch; do not extract this page again."
        )
    return (
        "Independent review could not tell whether this page is the procedure. "
        f"Reviewer: {summary} If more of the page exists, scroll it into view and extract again; "
        "otherwise keep searching."
    )


# --------------------------------------------------------------- one task


async def _run_task(
    *,
    client: Any,
    settings: Any,
    provider: str,
    target: dict[str, Any],
    topic: str,
    objective: dict[str, Any],
    budget: _Budget,
    action_budget: Optional[int],
    capture: bool,
    review: bool,
    reviewer: Any,
    goal_note: str = "",
    role: str = "primary",
    reviewed: Optional[contract.ReviewedCandidates] = None,
) -> dict[str, Any]:
    """Drive one ScrapeX task to a verdict and return everything that happened."""

    started = time.perf_counter()
    # Candidates this objective has already had reviewed, shared with every
    # other task of the same objective; and the page states this one task has
    # shown X. Both are plain data owned by this call, not import-time state.
    reviewed = reviewed if reviewed is not None else contract.ReviewedCandidates()
    memory = contract.TaskMemory()
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
            "role": role,
            "topic": topic,
            "attempted": True,
            "searched": False,
            "verified": False,
            "accepted": False,
            "captured": False,
            "reason": (
                "Could not start a Navigator task: "
                f"{(created.get('error') or {}).get('message') or created.get('status')}"
            ),
            "create_task_result": created,
            "agent_trace": [],
            "agent_stopped_reason": "task_not_created",
            "stats": {},
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
                "role": role,
                "topic": topic,
                "status": "authentication_required",
                "provider": provider,
                "attempted": True,
                "searched": False,
                "verified": False,
                "accepted": False,
                "captured": False,
                "requires_human": True,
                "task_id": task_id,
                "reason": str(
                    initial_observation.get("message")
                    or "ALLDATA requires interactive authentication."
                ),
                "navigator": initial_observation,
                "agent_trace": [],
                "agent_stopped_reason": "authentication_required",
                "stats": {},
            }
        return {
            "role": role,
            "topic": topic,
            "attempted": True,
            "searched": False,
            "verified": False,
            "accepted": False,
            "captured": False,
            "task_id": task_id,
            "reason": (
                "Could not observe the initial Navigator page: "
                f"{(initial_observation.get('error') or {}).get('message') or initial_observation.get('status')}"
            ),
            "agent_trace": [],
            "agent_stopped_reason": "initial_observe_failed",
            "stats": {},
        }

    if not _observation_ready(initial_summary):
        return {
            "role": role,
            "topic": topic,
            "status": "initial_page_not_ready",
            "provider": provider,
            "attempted": True,
            "searched": False,
            "verified": False,
            "accepted": False,
            "captured": False,
            "task_id": task_id,
            "initial_observe_attempts": initial_observe_attempts,
            "reason": (
                "The Navigator task started, but its provider page did not expose "
                "any readable text, breadcrumb, or actionable elements before the "
                "bounded readiness window ended. No browser action was attempted."
            ),
            "navigator": initial_observation,
            "agent_trace": [],
            "agent_stopped_reason": "initial_page_not_ready",
            "stats": {},
        }

    first_summary = initial_summary
    first_observation_id: Optional[str] = initial_summary.get("observation_id")
    current_observation_id = first_observation_id
    # Whether the provider session already has the requested vehicle family
    # selected is a mechanical fact the provider can answer before any turn
    # is spent. When CIQ supplied a VIN, selection itself is mechanical too:
    # use ScrapeX's exact-VIN fast path before asking the model to interpret
    # the page. The live 2023 Accord run otherwise ignored the VIN, saw two
    # standard/hybrid choices, and eventually clicked an unrelated 2024
    # recent vehicle. Powertrain variants remain equivalent once year/make/
    # model match; a different year or VIN is never silently accepted.
    #
    # A new primary task in the managed runtime always reselects its exact VIN,
    # even when the page already looks like the right vehicle: ALLDATA keeps
    # browser state between tasks, and a new objective must not start inside
    # the previous objective's article. A dependency task keeps the verified
    # vehicle and page, so a supporting document is followed from where the
    # primary procedure named it.
    if contract.must_anchor_vehicle(settings, target, role):
        initial_vehicle_selected: Optional[bool] = False
    else:
        initial_vehicle_selected = await _target_already_selected(settings, provider, target)
    vehicle_selected_before_preflight = initial_vehicle_selected
    preflight_selection: Optional[dict[str, Any]] = None
    vin = "".join(str(target.get("vin") or "").split()).upper()
    if vin and initial_vehicle_selected is not True:
        selection_result = await scrapex_svc.navigator(
            settings,
            {"action": "select_vehicle", "task_id": task_id, "vin": vin},
        )
        selection_summary = _observation_summary(selection_result)
        selection_target = selection_summary.get("action_target")
        selection_claimed = bool(
            isinstance(selection_target, dict)
            and selection_target.get("selected") is True
            and str(selection_target.get("vin") or "").upper() == vin
        )
        if selection_result.get("success") and _observation_ready(selection_summary):
            initial_summary = selection_summary
            current_observation_id = selection_summary.get("observation_id")
        refreshed_selection = await _target_already_selected(settings, provider, target)
        if refreshed_selection is not None:
            initial_vehicle_selected = refreshed_selection
        elif selection_claimed:
            # ScrapeX's fast path now proves the exact VIN in the rendered
            # vehicle header before it emits selected=True, so that receipt is
            # sufficient when the separate current-target read is unavailable.
            initial_vehicle_selected = True
        preflight_selection = {
            "turn": -1,
            "action": "select_vehicle",
            "args": {"vin": vin},
            "mechanical_preflight": True,
            "observation_id": current_observation_id,
            "selected": initial_vehicle_selected,
            "error": (
                None
                if (
                    selection_result.get("success")
                    and selection_claimed
                    and initial_vehicle_selected is not False
                )
                else str(
                    selection_summary.get("action_detail")
                    or (
                        "The exact VIN was selected, but its rendered year/make/model "
                        "did not match the requested vehicle."
                        if selection_claimed and initial_vehicle_selected is False
                        else _navigator_failure_message(selection_result, "select_vehicle")
                    )
                )
            ),
            "result": selection_summary,
        }
    initial_screenshot = await _task_screenshot(settings, task_id, current_observation_id)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": _system_prompt(target, topic, objective, provider, goal_note),
        },
        {
            "role": "user",
            "content": _visual_observation_content(
                f"Find the ALLDATA procedure for {_target_label(target)}: {topic}."
                + _vehicle_selection_note(initial_vehicle_selected, target)
                + (f"\n\n{reviewed.briefing()}" if reviewed.briefing() else ""),
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
            "observation_id": first_observation_id,
            "vehicle_already_selected": vehicle_selected_before_preflight,
            "result": first_summary,
        }
    ]
    if preflight_selection is not None:
        trace.append(preflight_selection)
    stopped_reason = "model_finished"
    last_failed_call: Optional[tuple[str, tuple[tuple[str, Any], ...]]] = None
    repeated_failure_count = 0
    last_no_effect_call: Optional[tuple[str, tuple[tuple[str, Any], ...]]] = None
    no_effect_count = 0
    model_called_done = False
    candidate_verified = False
    accepted_review: Optional[dict[str, Any]] = None
    latest_review: Optional[dict[str, Any]] = None
    reviews: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    model_calls = 0
    prompt_tokens: list[int] = []
    stale_rejections = 0
    visited_urls: list[str] = []
    observation_ids: list[str] = []
    for observation_id in (first_observation_id, current_observation_id):
        if observation_id and observation_id not in observation_ids:
            observation_ids.append(observation_id)
    for summary in (first_summary, initial_summary):
        if summary.get("url") and str(summary["url"]) not in visited_urls:
            visited_urls.append(str(summary["url"]))

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
    previous_page_state = _page_state(initial_summary)
    memory.record(initial_summary.get("observation_id"), previous_page_state)
    # The observation X is currently acting from. The completion and
    # end-of-page contracts are judged against exactly this page.
    latest_observed_summary: dict[str, Any] = initial_summary
    candidate_submitted = False
    prose_reminders = 0
    action_ordinal = 1 if preflight_selection is not None else 0
    context_degraded = False
    # A working scroll on a lazily-loaded procedure is its own trap: the live
    # Palisade article grew from 15,676px to 18,116px while being scrolled, so
    # "keep going until the bottom" spent all 40 turns and never extracted.
    # Reading is what scrolling is for; this counts how long it has been since
    # any reading happened.
    consecutive_scrolls = 0

    while budget.turns_left > 0:
        budget.turns_used += 1
        turn = budget.turns_used - 1
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
        model_calls += 1
        usage = _extract_usage(events)
        if usage and isinstance(usage.get("prompt_tokens"), int):
            prompt_tokens.append(int(usage["prompt_tokens"]))

        content = _extract_content(events)
        calls = _extract_tool_calls(events)
        if not calls:
            # A prose reply is not a way to end research. The completion
            # contract refuses `done` on untested pages, and a reply with no
            # action used to walk straight past it: the Tacoma BSM dependency
            # tasks ended at zero browser actions this way. X is reminded once
            # that the task is still open; a second prose reply in a row ends
            # the task, explicitly, as model_finished.
            if prose_reminders < contract.PROSE_REMINDERS_ALLOWED and budget.turns_left > 0:
                prose_reminders += 1
                if content:
                    messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": contract.prose_reminder()})
                trace.append({"turn": turn, "action": "prose_reply_refused", "reminder": prose_reminders})
                budget.note("prose_reply", progress=False, turn=turn, task_id=task_id)
                continue
            stopped_reason = "model_finished"
            break
        prose_reminders = 0

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
        turn_progress: Optional[bool] = None
        turn_progress_kind = ""
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
            elif action == "done" and (
                refusal := contract.done_refusal(
                    latest_observed_summary.get("url"), candidate_submitted=candidate_submitted
                )
            ):
                # Refused before anything reaches ScrapeX: the page X is on was
                # deliberately navigated into and has not been tested.
                result = refusal
            elif action == "scroll" and (
                refusal := contract.bottom_scroll_refusal(latest_observed_summary, args.get("delta_y"))
            ):
                result = refusal
            else:
                dispatch_args: dict[str, Any] = {"action": action, "task_id": task_id}
                if action == "observe_marks":
                    dispatch_args = {"action": "observe", "task_id": task_id, "marks": True}
                elif action in _REF_ACTIONS:
                    dispatch_args["ref"] = args.get("ref")
                    if action in {"fill", "type"}:
                        dispatch_args["text"] = args.get("text")
                    elif action == "press":
                        dispatch_args["key"] = args.get("key")
                    if current_observation_id:
                        dispatch_args["observation_id"] = current_observation_id
                elif action == "open":
                    dispatch_args["url"] = args.get("url")
                elif action == "scroll":
                    dispatch_args["delta_y"] = args.get("delta_y")
                elif action == "wait":
                    dispatch_args["milliseconds"] = args.get("milliseconds")
                elif action == "click_mark":
                    dispatch_args["mark"] = args.get("mark")
                    dispatch_args["observation_id"] = current_observation_id or "obs_unknown"
                elif action == "click_visual":
                    dispatch_args["x_norm"] = float(args.get("x_norm"))
                    dispatch_args["y_norm"] = float(args.get("y_norm"))
                    dispatch_args["observation_id"] = current_observation_id or "obs_unknown"
                elif action == "select_vehicle":
                    dispatch_args["vin"] = args.get("vin")
                dispatched = True
                action_ordinal += 1
                consecutive_scrolls = consecutive_scrolls + 1 if action == "scroll" else 0
                navigator_result = await scrapex_svc.navigator(settings, dispatch_args)
                if navigator_result.get("success"):
                    result = _observation_summary(navigator_result)
                    # Let the page finish becoming itself before showing it.
                    # Measured live on 2026-09-12: a click that navigated from
                    # ALLDATA Home to Collision Home came back with an
                    # observation still describing Home -- 26 elements, old
                    # title -- and the very next action, a bare wait, showed
                    # the new page. So the model saw "I clicked and nothing
                    # changed", clicked the same ref again, and by then the
                    # page really had moved, making the ref unresolvable. That
                    # 409 alternation cost roughly half of every turn budget.
                    # Only the suspicious case is worth waiting on: if this
                    # observation already shows a different page, the action
                    # landed and there is nothing to wait for. An observation
                    # identical to the one the model just acted from is the
                    # one that might have been read mid-transition, so poll
                    # for a short while and adopt a changed page if one
                    # arrives. If nothing changes, the action genuinely did
                    # not move the page and the original result stands.
                    if (
                        action in _PAGE_CHANGING_ACTIONS
                        and _observation_fingerprint(result) == previous_fingerprint
                    ):
                        (
                            settled,
                            settled_summary,
                            settle_attempts,
                        ) = await _observe_until_ready(
                            settings,
                            task_id,
                            attempts=_SETTLE_OBSERVE_ATTEMPTS,
                            delay_seconds=_SETTLE_OBSERVE_DELAY_SECONDS,
                            previous_fingerprint=previous_fingerprint,
                        )
                        if (
                            settled.get("success")
                            and _observation_ready(settled_summary)
                            and _observation_fingerprint(settled_summary) != previous_fingerprint
                        ):
                            result = settled_summary
                            result["settled_after_observations"] = settle_attempts
                    if action != "done":
                        latest_visual_summary = result
                        latest_action_args = (
                            action,
                            {key: value for key, value in args.items() if key != "action"},
                        )

                    # Close the reasoning loop at the moment X proposes a
                    # candidate procedure. ScrapeX remains the truth authority
                    # on the mechanical claim; the independent reviewer on the
                    # semantic one. Both verdicts go straight back to X while
                    # the live page and navigation history are still available.
                    if action == "extract":
                        # X has put a page up for review from this task, so the
                        # completion contract no longer holds `done` back.
                        candidate_submitted = True
                        candidate_check = await scrapex_svc.navigator(
                            settings, {"action": "verify", "task_id": task_id}
                        )
                        candidate_proof = (
                            candidate_check.get("data")
                            if isinstance(candidate_check.get("data"), dict)
                            else {}
                        )
                        mechanically_verified = bool(candidate_proof.get("verified"))
                        result["verification_after_extract"] = {
                            "verified": mechanically_verified,
                            "reason": candidate_proof.get("reason"),
                            "vehicle_verified": candidate_proof.get("vehicle_verified"),
                            "navigation_performed": candidate_proof.get("navigation_performed"),
                            "candidate_extracted": candidate_proof.get("candidate_extracted"),
                            "content_extracted": candidate_proof.get("content_extracted"),
                        }
                        trace.append(
                            {
                                "turn": turn,
                                "action": "verify_after_extract",
                                "verified": mechanically_verified,
                                "reason": candidate_proof.get("reason"),
                            }
                        )
                        candidate_record: dict[str, Any] = {
                            "task_id": task_id,
                            "title": result.get("title"),
                            "url": result.get("url"),
                            "observation_id": result.get("observation_id"),
                            "mechanically_verified": mechanically_verified,
                            "mechanical_reason": candidate_proof.get("reason"),
                        }
                        if not mechanically_verified:
                            result["next_instruction"] = (
                                "Candidate rejected by ScrapeX verification. Use the failed gates/reason "
                                "and current page state to backtrack or choose a different branch; do not "
                                "repeat extract on the same unchanged page."
                            )
                        elif not review:
                            candidate_verified = True
                            result["next_instruction"] = (
                                "Candidate verified. Stop browsing; verified evidence has been reached."
                            )
                        else:
                            evidence_result = await scrapex_svc.navigator(
                                settings, {"action": "get_evidence", "task_id": task_id}
                            )
                            evidence = (
                                evidence_result.get("data")
                                if isinstance(evidence_result.get("data"), dict)
                                else {}
                            )
                            candidate = _candidate_from_evidence(evidence, result)
                            # The same page with the same text has already been
                            # reviewed for this objective and turned down --
                            # possibly by an earlier task. Asking again cannot
                            # change the answer; it only spends a review. The
                            # live BSM run extracted one "Initialization" page
                            # four times in a row and paid for four reviews.
                            prior = reviewed.prior(candidate.get("url"), candidate.get("text_sha256"))
                            if prior is not None and not review_accepted(prior.get("verdict")):
                                verdict = contract.repeated_candidate_verdict(prior)
                            else:
                                review_screenshot = await _task_screenshot(
                                    settings, task_id, result.get("observation_id")
                                )
                                verdict = await reviewer(
                                    client=client,
                                    objective=objective,
                                    vehicle=target,
                                    candidate=candidate,
                                    provider=provider,
                                    screenshot=review_screenshot,
                                )
                                verdict = verdict if isinstance(verdict, dict) else {"decision": "UNCERTAIN", "malformed": True}
                                reviewed.remember(
                                    candidate.get("url"),
                                    candidate.get("text_sha256"),
                                    candidate.get("title"),
                                    verdict,
                                )
                            latest_review = verdict
                            reviews.append(verdict)
                            candidate_record["review"] = verdict
                            candidate_record["text_sha256"] = candidate.get("text_sha256")
                            result["semantic_review"] = {
                                key: verdict.get(key)
                                for key in (
                                    "classification",
                                    "procedure_type",
                                    "objective_match",
                                    "decision",
                                    "confidence",
                                    "evidence",
                                    "dependencies",
                                    "evidence_summary",
                                    "inconsistent",
                                    "malformed",
                                )
                            }
                            result["next_instruction"] = _next_instruction_for_review(verdict)
                            trace.append(
                                {
                                    "turn": turn,
                                    "action": "semantic_review",
                                    "decision": verdict.get("decision"),
                                    "classification": verdict.get("classification"),
                                    "objective_match": verdict.get("objective_match"),
                                    "confidence": verdict.get("confidence"),
                                    "malformed": verdict.get("malformed"),
                                    "repeated_candidate": verdict.get("repeated_candidate") is True,
                                    "dependencies": verdict.get("dependencies"),
                                }
                            )
                            if review_accepted(verdict):
                                candidate_verified = True
                                accepted_review = verdict
                            elif verdict.get("decision") == "FOLLOW_DEPENDENCY" and verdict.get("dependencies"):
                                # Not this page: the reviewer named the document
                                # that is. This task ends; that document is a
                                # dependency task of its own.
                                accepted_review = None
                                latest_review = verdict
                                model_called_done = True
                        candidates.append(candidate_record)
                else:
                    result = {
                        "error": _navigator_failure_message(navigator_result, action)
                    }
                    code = _failure_code(navigator_result)
                    if code in _STALE_CODES:
                        stale_rejections += 1
                        result["stale_rejection"] = code
                    if action in _REF_ACTIONS and code in {"unknown_ref", "stale_ref", "stale_target"}:
                        result["fallback_hint"] = (
                            "If you can see the control in the screenshot but no listed ref reaches "
                            "it, call observe_marks and then click_mark; use click_visual only if no "
                            "mark is offered for it."
                        )
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
            # succeeds -- only verification or review refuses it -- so without
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
                and not model_called_done
            ):
                call_error = str(
                    (result.get("semantic_review") or {}).get("evidence_summary")
                    if result.get("semantic_review")
                    else (result.get("verification_after_extract") or {}).get("reason")
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

            # An action that executed cleanly and left the page exactly as it
            # was, sent again unchanged, is a fact worth stating plainly. This
            # says only what was observed -- same url, same title, same
            # elements, same viewport -- and never what the model should have
            # meant instead.
            if (
                call_index == 0
                and dispatched
                and not call_error
                and action in _EFFECT_EXPECTED_ACTIONS
                and isinstance(result, dict)
                and not result.get("error")
            ):
                if _page_state(result) == previous_page_state:
                    if call_signature == last_no_effect_call:
                        no_effect_count += 1
                    else:
                        no_effect_count = 1
                    last_no_effect_call = call_signature
                    if no_effect_count >= _NO_EFFECT_NOTICE_AT:
                        result["no_effect_repeat"] = (
                            f"You have now sent this exact action {no_effect_count} times and the page "
                            "has not changed at all: same URL, same title, same elements, same scroll "
                            "position. Sending it again will do the same. Act on a different element, "
                            "go back, or reach the content another way."
                        )
                    if no_effect_count >= _NO_EFFECT_LIMIT:
                        turn_hit_repeat_limit = True
                        stopped_reason = "repeated_no_effect"
                else:
                    no_effect_count = 0
                    last_no_effect_call = None

            if action == "done" and dispatched and not call_error:
                model_called_done = True

            if call_index == 0:
                if call_error:
                    turn_progress = False
                    turn_progress_kind = (
                        "repeated_failure" if repeated_failure_count >= 2 else
                        ("stale_rejection" if result.get("stale_rejection") else "failed_action")
                    )
                elif action == "extract":
                    turn_progress = True
                    turn_progress_kind = "candidate"

            trace.append({
                "turn": turn,
                "action": action,
                "args": {k: v for k, v in args.items() if k != "action"},
                "observation_id": current_observation_id,
                "error": call_error,
                "refused": ((result.get("refused") or {}).get("code") if isinstance(result, dict) else None),
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
            next_observation_id = latest_visual_summary.get("observation_id")
            current_screenshot = await _task_screenshot(settings, task_id, next_observation_id)
            fingerprint = _observation_fingerprint(latest_visual_summary)
            page_state = _page_state(latest_visual_summary)
            # "Unchanged" means the reader is exactly where it was, viewport
            # included: scrolling into the rest of a long procedure is the job,
            # not drift. The bare fingerprint still decides whether a click
            # needs waiting out, since that asks a different question -- has
            # this become a different page yet?
            unchanged = page_state == previous_page_state
            previous_fingerprint = fingerprint
            previous_page_state = page_state
            latest_observed_summary = latest_visual_summary
            # A return to a state this task already showed X -- through any
            # route, not only the previous page -- is a cycle. Comparing with
            # the previous page alone let A -> B -> A menu ping-pong look like
            # steady progress and keep refunding the stall budget.
            revisit = (not unchanged) and memory.record(next_observation_id, page_state)
            if next_observation_id:
                current_observation_id = next_observation_id
                observation_ids.append(next_observation_id)
            url = str(latest_visual_summary.get("url") or "")
            new_url = bool(url) and url not in visited_urls
            if new_url:
                visited_urls.append(url)
            if turn_progress is None:
                if revisit:
                    turn_progress = False
                    turn_progress_kind = "revisited_page_state"
                else:
                    turn_progress = not unchanged
                    turn_progress_kind = (
                        "new_url" if new_url else ("new_page_state" if not unchanged else "page_unchanged")
                    )
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
            at_bottom = bool(
                (latest_visual_summary.get("scroll_position") or {}).get("at_page_bottom")
            )
            # Scrolling toward the bottom is the job, so it is only drift once
            # the bottom is actually reached, or once it has run well past the
            # ten-or-so scrolls this provider's longest procedures need.
            if consecutive_scrolls >= 1 and at_bottom:
                heading += (
                    " You have reached the bottom of this page. If it is the "
                    "procedure for the requested vehicle and system, call extract "
                    "NOW -- do not keep scrolling, and do not go looking elsewhere."
                )
            elif consecutive_scrolls >= _SCROLL_NUDGE_AFTER:
                heading += (
                    f" You have scrolled {consecutive_scrolls} times without "
                    "reaching the bottom or extracting anything. If the content "
                    "you were sent for is on screen, call extract NOW; otherwise "
                    "go back and choose a different branch."
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
            if revisit:
                heading += " " + contract.cycle_warning(memory.revisit_count)
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

        if turn_progress is not None:
            budget.note(
                turn_progress_kind or ("progress" if turn_progress else "no_progress"),
                progress=turn_progress,
                cost=(
                    2
                    if turn_progress_kind == "repeated_failure"
                    else contract.REVISIT_STALL_COST
                    if turn_progress_kind == "revisited_page_state"
                    else 1
                ),
                turn=turn,
                task_id=task_id,
            )

        if candidate_verified:
            stopped_reason = "verified_after_extract"
            break
        if model_called_done:
            stopped_reason = "model_done"
            break
        if turn_hit_repeat_limit:
            if stopped_reason != "repeated_no_effect":
                stopped_reason = "repeated_tool_error"
            break
        if budget.stalled:
            stopped_reason = "stalled"
            break
    else:
        stopped_reason = "turn_budget_exhausted"
    if budget.turns_left <= 0 and stopped_reason == "model_finished" and not candidate_verified:
        stopped_reason = "turn_budget_exhausted"

    # ScrapeX's own verify action is the single authority on
    # evaluate_navigation_claim; this loop never recomputes browser semantics
    # or text-matching itself.
    verification = await scrapex_svc.navigator(settings, {"action": "verify", "task_id": task_id})
    proof = verification.get("data") if isinstance(verification.get("data"), dict) else {}
    mechanically_verified = bool(proof.get("verified"))

    evidence_result = await scrapex_svc.navigator(
        settings, {"action": "get_evidence", "task_id": task_id}
    )
    evidence = (
        evidence_result.get("data")
        if isinstance(evidence_result.get("data"), dict)
        else {}
    )

    accepted = bool(mechanically_verified and (accepted_review is not None or not review))
    capture_result: dict[str, Any] | None = None
    captured = False
    if accepted and capture:
        # Persistence is a separate structured choice from research. ScrapeX
        # owns the provider browser and therefore owns the final verified-page
        # capture when the calling workflow explicitly requests preservation.
        # The reviewer's verdict rides along as data in the provenance.
        if accepted_review is not None:
            capture_result = await scrapex_svc.navigator_capture(
                settings, task_id, semantic_review=accepted_review, objective=objective
            )
        else:
            capture_result = await scrapex_svc.navigator_capture(settings, task_id)
        captured = bool(
            capture_result.get("success") is True
            and capture_result.get("verified") is True
            and capture_result.get("work_complete") is True
        )

    if not mechanically_verified:
        reason = proof.get("reason")
    elif review and accepted_review is None:
        reason = (
            "Semantic review did not accept a candidate: "
            + str((latest_review or {}).get("evidence_summary") or (latest_review or {}).get("decision") or "no candidate reviewed")
        )
    else:
        reason = None

    capture_data = capture_result.get("data") if isinstance(capture_result, dict) and isinstance(capture_result.get("data"), dict) else {}
    return {
        "role": role,
        "topic": topic,
        "goal_note": goal_note or None,
        "attempted": True,
        "searched": len(trace) > 1,
        "mechanically_verified": mechanically_verified,
        "verified": accepted,
        "accepted": accepted,
        "captured": captured,
        "capture": capture_result,
        "artifact": {
            "relative_path": capture_data.get("relative_path"),
            "sha256": capture_data.get("sha256"),
            "text_sidecar": capture_data.get("text_sidecar"),
            "source_sidecar": capture_data.get("source_sidecar"),
            "title": capture_data.get("title"),
            "already_present": capture_data.get("already_present"),
        } if captured else None,
        "verification_reason": reason,
        "verification": proof,
        "review": accepted_review or latest_review,
        "reviews": reviews,
        "candidates": candidates,
        "task_id": task_id,
        "provider": provider,
        "target": target,
        "agent_trace": trace,
        "agent_stopped_reason": stopped_reason,
        "browser_actions_observed": len(observation_slots) - 1,
        "context_degraded": context_degraded,
        "source_url": evidence.get("source_url"),
        "title": evidence.get("title") or proof.get("title"),
        "extracted_text": (evidence.get("extracted_text") or "")[:20_000],
        "extracted_text_sha256": evidence.get("extracted_text_sha256"),
        "stats": {
            "vehicle_already_selected": vehicle_selected_before_preflight,
            "vehicle_selected_after_preflight": initial_vehicle_selected,
            "model_calls": model_calls,
            "browser_actions": action_ordinal,
            "stale_rejections": stale_rejections,
            "page_state_revisits": memory.revisit_count,
            "candidate_submitted": candidate_submitted,
            "prompt_tokens_max": max(prompt_tokens) if prompt_tokens else None,
            "prompt_tokens_total": sum(prompt_tokens) if prompt_tokens else None,
            "observation_ids": observation_ids,
            "visited_urls": visited_urls,
            "wall_s": round(time.perf_counter() - started, 1),
        },
    }


# --------------------------------------------------------------- objective


def _objective_record(objective: Optional[dict[str, Any]], topic: str, target: dict[str, Any]) -> dict[str, Any]:
    record = dict(objective or {})
    record.setdefault("objective", topic)
    record["vehicle"] = {
        key: target.get(key) for key in ("year", "make", "model", "trim", "vin") if target.get(key) not in (None, "")
    }
    return record


def _receipt(
    *,
    objective: dict[str, Any],
    target: dict[str, Any],
    provider: str,
    tasks: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
    budget: _Budget,
    status: str,
    incomplete_reasons: list[str],
    wall_s: float,
) -> dict[str, Any]:
    """Structured history of one research objective, for diagnosis without logs.

    Actions, observation ids, visited URLs, candidates, reviewer decisions,
    dependencies, stale-action refusals, artifacts and hashes, final status,
    and why it is incomplete. Summaries of what happened, never the model's
    hidden reasoning.
    """
    actions: list[dict[str, Any]] = []
    observation_ids: list[str] = []
    visited: list[str] = []
    candidates: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    stale = 0
    model_calls = 0
    browser_actions = 0
    prompt_max = 0
    prompt_total = 0
    for task in tasks:
        stats = task.get("stats") or {}
        stale += int(stats.get("stale_rejections") or 0)
        model_calls += int(stats.get("model_calls") or 0)
        browser_actions += int(stats.get("browser_actions") or 0)
        prompt_max = max(prompt_max, int(stats.get("prompt_tokens_max") or 0))
        prompt_total += int(stats.get("prompt_tokens_total") or 0)
        observation_ids.extend(stats.get("observation_ids") or [])
        for url in stats.get("visited_urls") or []:
            if url not in visited:
                visited.append(url)
        for item in task.get("agent_trace") or []:
            mechanical_preflight = item.get("mechanical_preflight") is True
            if (
                (not isinstance(item.get("turn"), int) or item.get("turn") < 0)
                and not mechanical_preflight
            ):
                continue
            if item.get("action") in {"verify_after_extract", "semantic_review"}:
                continue
            actions.append(
                {
                    "task_id": task.get("task_id"),
                    "turn": item.get("turn"),
                    "action": item.get("action"),
                    "args": item.get("args"),
                    "observation_id": item.get("observation_id"),
                    "mechanical_preflight": mechanical_preflight,
                    "error": (str(item.get("error"))[:200] if item.get("error") else None),
                }
            )
        for candidate in task.get("candidates") or []:
            candidates.append({key: value for key, value in candidate.items() if key != "review"})
            verdict = candidate.get("review")
            if isinstance(verdict, dict):
                decisions.append(
                    {
                        "task_id": task.get("task_id"),
                        "title": candidate.get("title"),
                        "url": candidate.get("url"),
                        "decision": verdict.get("decision"),
                        "classification": verdict.get("classification"),
                        "confidence": verdict.get("confidence"),
                        "malformed": verdict.get("malformed"),
                        "inconsistent": verdict.get("inconsistent"),
                        "evidence_summary": verdict.get("evidence_summary"),
                    }
                )
        if task.get("captured") and task.get("artifact"):
            artifacts.append({"task_id": task.get("task_id"), **task["artifact"]})
    return {
        "objective": objective,
        "vehicle": target,
        "provider": provider,
        "task_ids": [task.get("task_id") for task in tasks if task.get("task_id")],
        "actions": actions,
        "observation_ids": observation_ids,
        "visited_urls": visited,
        "candidates": candidates,
        "critic_decisions": decisions,
        "dependencies": dependencies,
        "stale_action_rejections": stale,
        # Objective-level decisions the loop took on X's behalf: a fresh
        # primary attempt, or turns added for required dependencies.
        "objective_events": [
            event
            for event in budget.progress_events
            if event.get("kind") in {"primary_attempt_restarted", "dependency_capacity_added"}
        ],
        "page_state_revisits": sum(
            1 for event in budget.progress_events if event.get("kind") == "revisited_page_state"
        ),
        "artifacts": artifacts,
        "final_status": status,
        "incomplete_reasons": incomplete_reasons,
        "metrics": {
            "model_calls": model_calls,
            "browser_actions": browser_actions,
            "turns_used": budget.turns_used,
            "turns_allowed": budget.max_turns,
            "stall_points": budget.stall_points,
            "prompt_tokens_max": prompt_max or None,
            "prompt_tokens_total": prompt_total or None,
            "wall_s": round(wall_s, 1),
        },
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
    capture: bool = False,
    objective: Optional[dict[str, Any]] = None,
    review: bool = True,
    reviewer: Any = None,
    max_dependencies: int = MAX_DEPENDENCIES,
) -> dict[str, Any]:
    """Research one objective: the primary document, then what it requires.

    The model navigates; ScrapeX proves the mechanics; the independent
    reviewer judges the evidence; capture files what was accepted. When the
    reviewer names documents the objective needs, each is pursued as its own
    ScrapeX task inside the same turn budget, and running out of budget is
    reported as incompleteness rather than hidden.
    """
    alldata_sunset.refuse("research_navigator_agent.run_navigator_search")
    if NAVIGATOR_LOCK.locked():
        return {
            "status": "navigator_busy",
            "attempted": False,
            "searched": False,
            "verified": False,
            "captured": False,
            "provider": provider,
            "target": target,
            "topic": topic,
            "reason": (
                "Another research run is using the provider browser right now; "
                "nothing was started for this objective."
            ),
        }
    # Qwen occasionally emits malformed Navigator tool JSON; the repair client
    # gives that one constrained retry. It is part of this entry point, not a
    # wrapper installed around it.
    if client is not None and not isinstance(client, NavigatorToolRepairClient):
        client = NavigatorToolRepairClient(client)
    async with NAVIGATOR_LOCK:
        return await _run_objective(
            client=client,
            settings=settings,
            provider=provider,
            target=target,
            topic=topic,
            max_turns=max_turns,
            action_budget=action_budget,
            capture=capture,
            objective=objective,
            review=review,
            reviewer=reviewer,
            max_dependencies=max_dependencies,
        )


async def _run_objective(
    *,
    client: Any,
    settings: Any,
    provider: str,
    target: dict[str, Any],
    topic: str,
    max_turns: int,
    action_budget: Optional[int],
    capture: bool,
    objective: Optional[dict[str, Any]],
    review: bool,
    reviewer: Any,
    max_dependencies: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    objective_record = _objective_record(objective, topic, target)
    reviewer = reviewer or review_candidate
    budget = _Budget(max_turns=max(1, int(max_turns)), dependency_slots=max(0, int(max_dependencies)))
    tasks: list[dict[str, Any]] = []
    dependencies: list[dict[str, Any]] = []
    incomplete: list[str] = []
    # Every candidate this objective has had reviewed, across all of its
    # primary attempts. Dependency tasks keep their own: a page that is wrong
    # for the primary objective can be exactly the supporting document a
    # dependency task was created to fetch.
    reviewed = contract.ReviewedCandidates()

    # The primary search. One task used to be all an objective got: when X
    # stalled on a menu or quit, the objective was simply unverified, and the
    # only recovery was re-running the whole job. A primary attempt that ends
    # without an accepted procedure for a reason another attempt can improve
    # on continues in a fresh, VIN-anchored task that knows which pages were
    # already turned down -- bounded by the objective's turn budget and an
    # attempt limit, never by navigation depth.
    primary_attempts = 0
    goal_note = ""
    while True:
        primary_attempts += 1
        primary = await _run_task(
            client=client,
            settings=settings,
            provider=provider,
            target=target,
            topic=topic,
            objective=objective_record,
            budget=budget,
            action_budget=action_budget,
            capture=capture,
            review=review,
            reviewer=reviewer,
            goal_note=goal_note,
            role="primary",
            reviewed=reviewed,
        )
        tasks.append(primary)
        primary_review = primary.get("review") if isinstance(primary.get("review"), dict) else {}
        if primary.get("accepted"):
            break
        if primary_review.get("decision") == "FOLLOW_DEPENDENCY" and primary_review.get("dependencies"):
            break
        stop = str(primary.get("agent_stopped_reason") or "")
        if (
            stop not in contract.RECOVERABLE_STOPS
            or primary_attempts >= contract.MAX_PRIMARY_ATTEMPTS
            or budget.turns_left < contract.MIN_TURNS_FOR_ANOTHER_ATTEMPT
        ):
            break
        budget.stall_points = 0
        budget.progress_events.append(
            {
                "kind": "primary_attempt_restarted",
                "progress": True,
                "attempt": primary_attempts + 1,
                "previous_stop": stop,
                "reviewed_not_accepted": len(reviewed.not_accepted()),
            }
        )
        goal_note = contract.retry_goal_note(primary_attempts + 1, primary, reviewed)

    # A primary procedure that says it needs other documents gets turns for
    # them on top of its own; the primary search never gave any up.
    primary_review = primary.get("review") if isinstance(primary.get("review"), dict) else {}
    if primary_review.get("decision") in {"ACCEPT_WITH_DEPENDENCIES", "FOLLOW_DEPENDENCY"}:
        required = min(
            budget.dependency_slots,
            len(
                {
                    " ".join(str(item.get("title") or "").casefold().split())
                    for item in primary_review.get("dependencies") or []
                    if isinstance(item, dict) and str(item.get("title") or "").strip()
                }
            ),
        )
        extended = min(MAX_OBJECTIVE_TURNS, budget.max_turns + DEPENDENCY_TURNS_EACH * required)
        if required > 0 and extended > budget.max_turns:
            budget.progress_events.append(
                {
                    "kind": "dependency_capacity_added",
                    "progress": True,
                    "primary_turn_limit": budget.max_turns,
                    "objective_turn_limit": extended,
                    "required_dependencies": required,
                    "dependency_turns_each": DEPENDENCY_TURNS_EACH,
                }
            )
            budget.max_turns = extended

    if len(tasks) == 1 and (
        not primary.get("task_id")
        or primary.get("agent_stopped_reason")
        in {"task_not_created", "authentication_required", "initial_observe_failed", "initial_page_not_ready"}
    ):
        result = dict(primary)
        result.pop("role", None)
        result["status"] = result.get("status") or "unverified"
        result["documents"] = []
        result["dependencies"] = []
        result["task_ids"] = [primary["task_id"]] if primary.get("task_id") else []
        result["research_receipt"] = _receipt(
            objective=objective_record, target=target, provider=provider, tasks=tasks,
            dependencies=[], budget=budget, status=result["status"],
            incomplete_reasons=[str(primary.get("reason") or "")], wall_s=time.perf_counter() - started,
        )
        result.setdefault("topic", topic)
        result["provenance"] = {"provider": provider, "licensed_session": True, "workflow": "model_navigator_agent"}
        return result

    # Dependency queue: what the reviewer said the objective needs, why, and
    # what came of pursuing it. Bounded by slots and by the shared turn
    # budget; a limit reached is reported, never papered over.
    def enqueue(verdict: Optional[dict[str, Any]], origin: dict[str, Any]) -> None:
        # Only a verdict that says the objective *needs* another document
        # opens a dependency. A plain ACCEPT that also lists documents is
        # complete on its own; those are recorded as noted, never pursued.
        decision = str((verdict or {}).get("decision") or "")
        required = decision in {"ACCEPT_WITH_DEPENDENCIES", "FOLLOW_DEPENDENCY"}
        for item in (verdict or {}).get("dependencies") or []:
            title = str(item.get("title") or "").strip()
            if not title or any(dep["title"].casefold() == title.casefold() for dep in dependencies):
                continue
            dependencies.append(
                {
                    "title": title,
                    "reason": str(item.get("reason") or "").strip(),
                    "quote": str(item.get("quote") or "").strip() or None,
                    "originating_document": origin.get("title"),
                    "originating_url": origin.get("url"),
                    "originating_task_id": origin.get("task_id"),
                    "status": "pending" if required else "noted",
                    "resolved_artifact": None,
                    "task_id": None,
                }
            )

    enqueue(primary.get("review"), primary)
    dependency_turns_each = max(4, int(budget.max_turns * DEPENDENCY_TURN_SHARE / max(1, budget.dependency_slots or 1)))
    index = 0
    while index < len(dependencies):
        dependency = dependencies[index]
        index += 1
        if dependency["status"] != "pending":
            continue
        if budget.dependency_slots <= 0:
            dependency["status"] = "not_pursued"
            dependency["reason_not_pursued"] = f"dependency limit of {max_dependencies} reached"
            incomplete.append(f"dependency '{dependency['title']}' not pursued: dependency limit reached")
            continue
        if budget.turns_left < 3:
            dependency["status"] = "not_pursued"
            dependency["reason_not_pursued"] = "turn budget exhausted"
            incomplete.append(f"dependency '{dependency['title']}' not pursued: turn budget exhausted")
            continue
        budget.dependency_slots -= 1
        sub_budget = _Budget(
            max_turns=min(budget.turns_left, dependency_turns_each),
            dependency_slots=0,
        )
        dependency_objective = dict(objective_record)
        dependency_objective["dependency_context"] = (
            f"Required document '{dependency['title']}' for the objective; reason: "
            f"{dependency['reason']} (referenced from '{dependency.get('originating_document') or 'the accepted procedure'}')."
        )
        goal_note = (
            "This task pursues a REQUIRED SUPPORTING DOCUMENT named by the independent reviewer "
            f"of the primary procedure: '{dependency['title']}'. Why the objective needs it: "
            f"{dependency['reason']}. Find that exact document for the same vehicle and extract it."
        )
        dependency["status"] = "in_progress"
        outcome = await _run_task(
            client=client,
            settings=settings,
            provider=provider,
            target=target,
            topic=dependency["title"],
            objective=dependency_objective,
            budget=sub_budget,
            action_budget=action_budget,
            capture=capture,
            review=review,
            reviewer=reviewer,
            goal_note=goal_note,
            role="dependency",
        )
        budget.turns_used += sub_budget.turns_used
        budget.progress_events.extend(sub_budget.progress_events)
        tasks.append(outcome)
        dependency["task_id"] = outcome.get("task_id")
        dependency_review = outcome.get("review") if isinstance(outcome.get("review"), dict) else {}
        if outcome.get("accepted"):
            dependency["status"] = "resolved"
            dependency["resolved_artifact"] = outcome.get("artifact") if outcome.get("captured") else None
            dependency["resolved_url"] = outcome.get("source_url")
            dependency["resolved_title"] = outcome.get("title")
            enqueue(outcome.get("review"), outcome)
        elif (
            outcome.get("mechanically_verified")
            and dependency_review.get("decision") == "REJECT"
            and dependency_review.get("malformed") is not True
        ):
            # The reviewer reached the named document and, seeing it, judged
            # it the wrong kind of document for the objective. Its own later
            # verdict overrides its earlier naming: the objective does not
            # need it. Recorded, never counted as missing.
            dependency["status"] = "dismissed"
            dependency["reason_dismissed"] = str(dependency_review.get("evidence_summary") or "")
            dependency["resolved_url"] = outcome.get("source_url")
            dependency["resolved_title"] = outcome.get("title")
        else:
            dependency["status"] = "unresolved"
            dependency["reason_unresolved"] = str(outcome.get("verification_reason") or outcome.get("reason") or outcome.get("agent_stopped_reason") or "")
            incomplete.append(
                f"dependency '{dependency['title']}' unresolved: {dependency['reason_unresolved'] or 'not found'}"
            )

    documents = [
        {
            "role": task.get("role"),
            "topic": task.get("topic"),
            "task_id": task.get("task_id"),
            "title": task.get("title"),
            "url": task.get("source_url"),
            "accepted": bool(task.get("accepted")),
            "mechanically_verified": bool(task.get("mechanically_verified")),
            "classification": (task.get("review") or {}).get("classification"),
            "procedure_type": (task.get("review") or {}).get("procedure_type"),
            "decision": (task.get("review") or {}).get("decision"),
            "confidence": (task.get("review") or {}).get("confidence"),
            "captured": bool(task.get("captured")),
            "artifact": task.get("artifact"),
            "extracted_text_sha256": task.get("extracted_text_sha256"),
            "stopped_reason": task.get("agent_stopped_reason"),
        }
        for task in tasks
    ]
    accepted_docs = [doc for doc in documents if doc["accepted"]]
    procedure_found = any(
        doc["accepted"] and (not review or doc.get("classification") == "ACTUAL_PROCEDURE")
        for doc in documents
    )
    primary_review = primary.get("review") or {}
    if primary.get("accepted"):
        verified = True
    elif primary_review.get("decision") == "FOLLOW_DEPENDENCY":
        verified = procedure_found
    else:
        verified = False
    unresolved = [dep for dep in dependencies if dep["status"] in {"unresolved", "not_pursued", "pending"}]
    # A later attempt that never reached review must not erase what the
    # reviewer said about an earlier attempt's candidate.
    reviewed_primary = next(
        (
            task
            for task in reversed(tasks)
            if task.get("role") == "primary" and isinstance(task.get("review"), dict) and task.get("review")
        ),
        primary,
    )
    if not verified:
        status = "unverified"
        if primary.get("agent_stopped_reason") == "stalled":
            incomplete.append("navigation stalled without progress")
        if primary.get("agent_stopped_reason") == "turn_budget_exhausted":
            incomplete.append("turn budget exhausted before a procedure was accepted")
        if review and reviewed_primary.get("mechanically_verified") and not reviewed_primary.get("accepted"):
            incomplete.append(str(reviewed_primary.get("verification_reason") or "semantic review did not accept the candidate"))
        if review and (reviewed_primary.get("review") or {}).get("decision") == "UNCERTAIN":
            status = "uncertain"
        # A failure has to say what happened: how each attempt ended and
        # which pages were actually reviewed and turned down, and why.
        stops = [
            str(task.get("agent_stopped_reason") or "unknown")
            for task in tasks
            if task.get("role") == "primary"
        ]
        incomplete.append(
            f"{primary_attempts} primary attempt(s) ended without an accepted procedure "
            f"(stopped: {', '.join(stops)})"
        )
        for entry in reviewed.not_accepted()[:5]:
            incomplete.append(
                f"reviewed and not accepted: \"{entry['title'] or entry['url']}\" -- "
                f"{entry['decision']}"
                + (f": {entry['reason']}" if entry.get("reason") else "")
            )
    elif unresolved:
        status = "incomplete"
    else:
        status = "verified"
    if verified and capture and any(doc["accepted"] and not doc["captured"] for doc in documents):
        status = "incomplete"
        incomplete.append("an accepted document was not captured")

    accepted_primary = next((task for task in tasks if task.get("accepted")), None) or primary
    review_out = primary.get("review") if verified else reviewed_primary.get("review")
    result: dict[str, Any] = {
        "status": status,
        "attempted": True,
        "searched": bool(primary.get("searched")),
        "primary_attempts": primary_attempts,
        "verified": verified,
        "complete": status == "verified",
        "mechanically_verified": bool(accepted_primary.get("mechanically_verified")),
        "captured": bool(accepted_primary.get("captured")),
        "capture": accepted_primary.get("capture"),
        "verification_reason": primary.get("verification_reason") or reviewed_primary.get("verification_reason"),
        "verification": accepted_primary.get("verification"),
        "semantic_review": review_out,
        "task_id": primary.get("task_id"),
        "task_ids": [task.get("task_id") for task in tasks if task.get("task_id")],
        "provider": provider,
        "target": target,
        "topic": topic,
        "objective": objective_record,
        "agent_trace": [item for task in tasks for item in (task.get("agent_trace") or [])],
        "agent_stopped_reason": primary.get("agent_stopped_reason"),
        "browser_actions_observed": sum(int(task.get("browser_actions_observed") or 0) for task in tasks),
        "context_degraded": any(task.get("context_degraded") for task in tasks),
        "source_url": accepted_primary.get("source_url"),
        "evidence_title": accepted_primary.get("title"),
        "extracted_text": accepted_primary.get("extracted_text") or "",
        "extracted_text_sha256": accepted_primary.get("extracted_text_sha256"),
        "documents": documents,
        "accepted_documents": accepted_docs,
        "dependencies": dependencies,
        "incomplete_reasons": incomplete,
        "provenance": {
            "provider": provider,
            "licensed_session": True,
            "workflow": "model_navigator_agent",
            "semantic_review": bool(review),
        },
    }
    if primary.get("requires_human"):
        result["requires_human"] = True
    result["research_receipt"] = _receipt(
        objective=objective_record,
        target=target,
        provider=provider,
        tasks=tasks,
        dependencies=dependencies,
        budget=budget,
        status=status,
        incomplete_reasons=incomplete,
        wall_s=time.perf_counter() - started,
    )
    return result
