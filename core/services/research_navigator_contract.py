"""The structural execution contract of one Navigator research objective.

X owns navigation meaning: which page is relevant, which link to follow, what
the manufacturer calls a system. This module owns none of that. It holds the
small set of *structural* facts the Navigator loop enforces around X's
choices, in one place, as plain functions the loop calls directly:

* where a live ALLDATA route sits -- a vehicle/component landing page or an
  article/guid document route -- read from the URL's shape, never its words;
* when ``done`` is refused, because nothing has been submitted for review from
  a page X deliberately navigated into;
* when a downward scroll is refused, because the observation already says the
  page has no more content below;
* which rendered page states this task has already shown X, so a return to one
  is recognised as a cycle rather than progress (A -> B -> A);
* which candidate pages this *objective* has already had reviewed and not
  accepted, so the same page is never re-submitted, and a fresh task knows
  where not to go back to.

These used to be five separate wrappers installed at import time around the
Navigator module and the ScrapeX client -- a cycle guard, a completion guard,
a bottom-of-page guard, a vehicle anchor, and a dependency budget extension --
each rebinding functions another had already rebound. The rules are unchanged
in spirit; they are now data and functions with no install order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# A task may reply in prose once and be reminded that the Navigator acts only
# through its tool; a second prose reply in a row ends the task explicitly.
PROSE_REMINDERS_ALLOWED = 1

# How much a return to an already-seen page state costs the stall budget. A
# cycle is at least as costly as a repeated failed action, so four ignored
# cycle warnings are enough to reach the stall limit.
REVISIT_STALL_COST = 2

# A primary objective whose first task ended without an accepted procedure may
# continue in a fresh, VIN-anchored task while its turn budget allows. These
# bound that by resources, not by navigation depth.
MAX_PRIMARY_ATTEMPTS = 3
MIN_TURNS_FOR_ANOTHER_ATTEMPT = 8

# Ways a primary task can end that another attempt with memory can improve on.
# Anything else -- authentication, a task that never started, the model worker
# failing, or the budget itself running out -- is not helped by trying again.
RECOVERABLE_STOPS = frozenset(
    {
        "stalled",
        "model_done",
        "model_finished",
        "repeated_no_effect",
        "repeated_tool_error",
    }
)


# ------------------------------------------------------------------ vehicle


def exact_vin(target: Any) -> str:
    """The target's VIN when it is a real 17-character VIN, else ''."""
    if not isinstance(target, dict):
        return ""
    vin = "".join(str(target.get("vin") or "").split()).upper()
    return vin if _VIN_RE.fullmatch(vin) else ""


def managed_runtime(settings: Any) -> bool:
    """Whether X drives the managed ScrapeX runtime (as opposed to a test harness)."""
    value = getattr(settings, "scrapex_project_path", None)
    return value is not None and bool(str(value).strip())


def must_anchor_vehicle(settings: Any, target: Any, role: str) -> bool:
    """Whether this task must reselect its exact VIN before X may navigate.

    ALLDATA keeps browser state between tasks, so a new primary objective must
    never inherit the previous vehicle or article because the page looks
    usable. A dependency task deliberately keeps the already-verified vehicle
    and page so a supporting document can be followed from where the primary
    procedure named it. There is no year/make/model fallback: without an exact
    VIN there is nothing to anchor to.
    """
    return role == "primary" and managed_runtime(settings) and bool(exact_vin(target))


# ------------------------------------------------------------------- routes


def _route_text(url: object) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.casefold()
    return f"{parsed.path}#{parsed.fragment}".casefold()


def is_routing_page(url: object) -> bool:
    """An ALLDATA vehicle/component landing route: a place to navigate from."""
    route = _route_text(url)
    if not route or "/article/" in route or "/guid/" in route:
        return False
    return "/vehicle/" in route and "/component/" in route and "/filter/nofilter" in route


def is_document_route(url: object) -> bool:
    """An ALLDATA article/guid route. The URL alone does not say what it holds."""
    route = _route_text(url)
    return bool(route and ("/article/" in route or "/guid/" in route))


# ------------------------------------------------------------- refusals


def _refusal(action: str, code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "error": message,
        "refused": {"action": action, "code": code, **detail},
    }


def done_refusal(url: object, *, candidate_submitted: bool) -> Optional[dict[str, Any]]:
    """Why ``done`` may not end this task now, or None when it may.

    ``done`` is how X says the procedure cannot be reached. It must not stand in
    for submitting a page X deliberately navigated into. Once a candidate has
    been submitted from this task, the reviewer has had its say and X may end
    it -- that is an explicit, diagnosable failure, not a silent one.
    """
    if candidate_submitted:
        return None
    if is_routing_page(url):
        return _refusal(
            "done",
            "premature_done_on_routing_page",
            "Completion refused: this is an ALLDATA vehicle/component landing page, not "
            "procedure evidence, and nothing has been submitted for review from this task. "
            "Reaching the right component is not completion. Open the plausible child "
            "procedure or article, read it, and extract it for independent review.",
            url=str(url or ""),
        )
    if is_document_route(url):
        return _refusal(
            "done",
            "premature_done_on_untested_article",
            "Completion refused: this is an ALLDATA article/guid route and nothing has been "
            "submitted for review from this task. An article route can be an index, a "
            "section, an introduction, or the procedure itself; its URL does not decide "
            "that. If the page carries the executable steps, read it fully and extract it. "
            "If it links deeper to the procedure, follow that link.",
            url=str(url or ""),
        )
    return None


def bottom_scroll_refusal(summary: Optional[dict[str, Any]], delta_y: Any) -> Optional[dict[str, Any]]:
    """Refuse a downward scroll the latest observation says cannot reveal anything."""
    if isinstance(delta_y, bool) or not isinstance(delta_y, int) or delta_y <= 0:
        return None
    position = (summary or {}).get("scroll_position")
    if not isinstance(position, dict) or position.get("at_page_bottom") is not True:
        return None
    return _refusal(
        "scroll",
        "scroll_at_page_bottom",
        "The current observation is already at the bottom of this page; another downward "
        "scroll cannot reveal anything. Decide from the page you have read: extract it for "
        "independent review if it could be the requested procedure, or leave it and keep "
        "searching if it clearly is not.",
    )


def prose_reminder() -> str:
    return (
        "You replied in prose without a navigator_browse action, so no browser action "
        "executed and the task is still open. A prose reply does not end research. Choose "
        "your next action from the latest observation: keep navigating, extract a fully read "
        "page that could be the procedure, or call done only if you have genuinely explored "
        "and the procedure is not reachable."
    )


# ------------------------------------------------------------------ memory


@dataclass
class TaskMemory:
    """Which rendered page states one Navigator task has shown X.

    Returning to an identical rendered state -- through any path -- is a
    revisit, not progress. The same observation is read more than once by the
    loop, so it is judged once by its observation id.
    """

    seen_page_states: set[str] = field(default_factory=set)
    judged: dict[str, bool] = field(default_factory=dict)
    revisit_count: int = 0

    def record(self, observation_id: Any, page_state: str) -> bool:
        key = str(observation_id or "").strip()
        if not key:
            return False
        if key in self.judged:
            return self.judged[key]
        revisit = page_state in self.seen_page_states
        self.judged[key] = revisit
        self.seen_page_states.add(page_state)
        if revisit:
            self.revisit_count += 1
        return revisit


def cycle_warning(revisit_count: int) -> str:
    return (
        "NAVIGATION CYCLE: this exact rendered page state was already shown earlier in this "
        "task. Returning here is not progress. Do not repeat the route that led back here; "
        "choose a genuinely different approach from the live controls -- a search with the "
        "manufacturer's own name for the system, a different component, or, if ALLDATA's "
        "ADAS Quick Reference is visible in THIS observation, that index. "
        f"Revisits so far: {revisit_count}."
    )


def _candidate_key(url: Any, text_sha256: Any) -> str:
    return f"{str(url or '').strip()}|{str(text_sha256 or '').strip()}"


@dataclass
class ReviewedCandidates:
    """Every candidate one research objective has had reviewed, across its tasks.

    Keyed by the page's URL and the hash of the text that was reviewed, so the
    same page is recognised even from a later task, while a page that has since
    loaded more text is a genuinely new candidate. Only the reviewer's verdicts
    are remembered; nothing here judges a page.
    """

    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def prior(self, url: Any, text_sha256: Any) -> Optional[dict[str, Any]]:
        return self.verdicts.get(_candidate_key(url, text_sha256))

    def remember(self, url: Any, text_sha256: Any, title: Any, verdict: dict[str, Any]) -> None:
        if not str(url or "").strip():
            return
        self.verdicts[_candidate_key(url, text_sha256)] = {
            "url": str(url),
            "title": " ".join(str(title or "").split())[:200],
            "decision": verdict.get("decision"),
            "classification": verdict.get("classification"),
            "objective_match": verdict.get("objective_match"),
            "reason": " ".join(str(verdict.get("evidence_summary") or "").split())[:240],
            "verdict": verdict,
        }

    def not_accepted(self) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.verdicts.values()
            if entry.get("decision") not in {"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"}
        ]

    def briefing(self, limit: int = 8) -> str:
        """What a fresh task needs to know about pages already turned down."""
        rows = self.not_accepted()[-limit:]
        if not rows:
            return ""
        lines = [
            f"- \"{row['title'] or row['url']}\" -- {row['decision']}"
            + (f" ({row['objective_match']})" if row.get("objective_match") else "")
            + (f": {row['reason']}" if row.get("reason") else "")
            for row in rows
        ]
        return (
            "Pages already reviewed for this objective and NOT accepted. Do not submit them "
            "again; the reviewer's reasons say where the procedure is not:\n" + "\n".join(lines)
        )


def repeated_candidate_verdict(prior: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's earlier verdict, re-issued for a page submitted again."""
    verdict = dict(prior.get("verdict") or {})
    verdict["repeated_candidate"] = True
    verdict["evidence_summary"] = (
        "This exact page was already reviewed for this objective and was not accepted "
        f"({prior.get('decision')}). Submitting it again cannot change that. Earlier reason: "
        f"{prior.get('reason') or 'none recorded'}"
    )[:1200]
    return verdict


def retry_goal_note(attempt: int, previous: dict[str, Any], reviewed: ReviewedCandidates) -> str:
    """The briefing a fresh primary attempt starts from."""
    stop = str(previous.get("agent_stopped_reason") or "unknown")
    parts = [
        f"This is attempt {attempt} at this objective. The previous attempt ended without an "
        f"accepted procedure (stopped: {stop}). The browser has been re-anchored to the exact "
        "vehicle. Take a different route than before -- for example ALLDATA's search with the "
        "manufacturer's own name for the system, or the ADAS Quick Reference if it is visible "
        "-- rather than repeating the same menus."
    ]
    briefing = reviewed.briefing()
    if briefing:
        parts.append(briefing)
    return "\n\n".join(parts)
