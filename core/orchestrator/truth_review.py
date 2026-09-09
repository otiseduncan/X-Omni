"""Model-based truth review for Calibration IQ mutation turns.

X's model owns interpretation, tool choice, and final wording. Deterministic
Core owns authorization, execution, receipts, and rereads. This module is the
boundary between them for turns that actually changed state: after a mutation
has executed and its authoritative evidence is in the turn, a bounded second
model pass judges whether the candidate answer materially contradicts or
exceeds that evidence.

It replaced deterministic terminal summaries that used to overwrite the
model's response on every Calibration IQ turn. Those summaries kept X truthful
by force -- and made "How many need SI?" answer with a multi-section audit
report. The repair rule is: reason broadly, act safely, answer narrowly,
expand when asked.

Deliberate properties of every model call made from here:

* ``tools=None`` -- the reviewer and the regeneration pass cannot select or
  re-execute anything; language correction is not action retry. Tool-call
  events are ignored even if a model emits one.
* Review context is ephemeral. Nothing appended for review, regeneration, or
  synthesis reaches the persisted transcript or the next turn's history.
* The deterministic layer validates only the *shape* of the reviewer's JSON.
  Semantic judgment stays in the model; there is no phrase parsing here and
  none may be added.
* One regeneration at most, then a tiny fail-closed line chosen from
  structural verification state -- never a rebuilt audit report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("xomni.truth_review")

# Bounded so a hostile or rambling reviewer cannot flood the regeneration
# prompt; these are shape limits, not interpretation.
MAX_REVIEW_ITEMS = 6
MAX_REVIEW_ITEM_CHARS = 300
MAX_GUIDANCE_CHARS = 500

TRUTH_REVIEW_INSTRUCTION = """Internal truth review; this is not a new user request. Judge only whether the assistant's last message contains a material factual or execution claim that contradicts or exceeds the authoritative tool results in this conversation. A short answer is valid when the evidence supports it; do not require receipts, ids, versions, counts, timestamps, or audit detail unless omitting them makes the message materially false. Never infer success the evidence does not state: authentication required, conflict, partial, indeterminate, may-have-executed, failed, and unverified are not success. Do not rewrite the answer and do not choose tools. Reply with exactly one JSON object and nothing else:
{"valid": true or false, "unsupported_claims": ["..."], "contradictions": ["..."], "correction_guidance": "..."}"""

REGENERATION_INSTRUCTION = """Internal correction; this is not a new user request. Your previous answer was inconsistent with the authoritative evidence:
{problems}
Guidance: {guidance}
Rewrite the final answer to Otis so it is fully consistent with the authoritative tool results in this conversation. Be concise. Do not repeat or re-execute any action; no tools are available."""

SYNTHESIS_INSTRUCTION = """Internal synthesis; this is not a new user request. Compose the final concise answer to Otis's request from this authoritative execution evidence:
{evidence}
Report exactly what completed, failed, awaits approval, or could not be verified; claim nothing beyond the evidence. No tools are available."""

# The tiny fail-closed vocabulary. This is the one place deterministic
# user-facing prose is allowed on this path, and it must stay tiny: the full
# structured evidence is still on the turn's result card and in the store.
FAIL_CLOSED_TEXTS = {
    "verified": "The requested Calibration IQ action completed and was verified.",
    "failed": "That Calibration IQ action failed and no change was verified.",
    "indeterminate": "I couldn't verify that Calibration IQ action's outcome.",
}


@dataclass(frozen=True)
class TruthReview:
    valid: bool
    unsupported_claims: tuple[str, ...]
    contradictions: tuple[str, ...]
    correction_guidance: str


def fail_closed_text(state: str) -> str:
    """Minimal safe line for a mutation turn whose wording could not be verified."""
    return FAIL_CLOSED_TEXTS.get(state, FAIL_CLOSED_TEXTS["indeterminate"])


def _bounded_str_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = tuple(
        str(item)[:MAX_REVIEW_ITEM_CHARS]
        for item in value[:MAX_REVIEW_ITEMS]
        if isinstance(item, str) and item.strip()
    )
    return items


def parse_truth_review(text: str) -> Optional[TruthReview]:
    """Validate the reviewer's JSON shape. Shape only -- never semantics.

    Local models occasionally wrap JSON in a code fence or a sentence; taking
    the outermost brace span is mechanical format handling, not language
    interpretation. Anything that does not parse into the contract is treated
    as reviewer-unavailable so the caller fails closed.
    """
    raw = str(text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("valid"), bool):
        return None
    return TruthReview(
        valid=data["valid"],
        unsupported_claims=_bounded_str_items(data.get("unsupported_claims")),
        contradictions=_bounded_str_items(data.get("contradictions")),
        correction_guidance=str(data.get("correction_guidance") or "")[
            :MAX_GUIDANCE_CHARS
        ],
    )


async def _no_tool_completion(client: Any, messages: list[dict[str, Any]]) -> str:
    """One bounded model call that structurally cannot execute anything.

    ``tools=None`` means the request advertises no tools, so the server-side
    model has nothing to call; if a scripted test double emits a tool_call
    event anyway it is ignored. This guarantee is what makes language repair
    safe after a mutation has already executed.
    """
    text = ""
    async for event in client.stream(messages, tools=None):
        if event.get("type") == "content":
            text += str(event.get("text") or "")
    return text


async def review_candidate(
    client: Any, base_messages: list[dict[str, Any]], candidate: str
) -> Optional[TruthReview]:
    """Run the truth review once. None means the reviewer was unavailable."""
    review_messages = [
        *base_messages,
        {"role": "assistant", "content": candidate},
        {"role": "user", "content": TRUTH_REVIEW_INSTRUCTION},
    ]
    try:
        raw = await _no_tool_completion(client, review_messages)
    except Exception:  # noqa: BLE001 - any transport/model failure fails closed
        log.warning("Truth review call failed; failing closed.", exc_info=True)
        return None
    return parse_truth_review(raw)


async def regenerate_candidate(
    client: Any,
    base_messages: list[dict[str, Any]],
    candidate: str,
    review: TruthReview,
) -> str:
    """One language-only regeneration. Never re-executes; returns '' on failure."""
    problems = [*review.contradictions, *review.unsupported_claims]
    problem_text = "\n".join(f"- {item}" for item in problems[:MAX_REVIEW_ITEMS])
    if not problem_text:
        problem_text = "- The answer was not supported by the evidence."
    instruction = REGENERATION_INSTRUCTION.format(
        problems=problem_text,
        guidance=review.correction_guidance or "State only what the evidence proves.",
    )
    regen_messages = [
        *base_messages,
        {"role": "assistant", "content": candidate},
        {"role": "user", "content": instruction},
    ]
    try:
        return await _no_tool_completion(client, regen_messages)
    except Exception:  # noqa: BLE001
        log.warning("Truth-review regeneration failed.", exc_info=True)
        return ""


async def synthesize_candidate(
    client: Any, base_messages: list[dict[str, Any]], evidence_json: str
) -> str:
    """Compose a candidate when the turn ended without one (e.g. approval pause).

    The evidence is embedded explicitly because on abnormal exits the message
    history may not yet contain the completed results. Returns '' on failure so
    the caller falls back closed.
    """
    instruction = SYNTHESIS_INSTRUCTION.format(evidence=evidence_json)
    synth_messages = [*base_messages, {"role": "user", "content": instruction}]
    try:
        return await _no_tool_completion(client, synth_messages)
    except Exception:  # noqa: BLE001
        log.warning("Truth-review synthesis failed.", exc_info=True)
        return ""
