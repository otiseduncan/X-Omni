from __future__ import annotations

import json
from functools import wraps
from typing import Any

_INSTALLED_ATTR = "__xomni_objective_match_guard_installed__"
_TOOL = "report_objective_match"
_MATCHES = (
    "EXACT_MATCH",
    "SAME_COMPONENT_WRONG_PROCEDURE",
    "DIFFERENT_COMPONENT",
    "DIFFERENT_SENSOR_FAMILY",
    "UNCERTAIN",
)
_SCHEMA = {
    "type": "function",
    "function": {
        "name": _TOOL,
        "description": (
            "Judge only whether this real procedure satisfies the requested ADAS "
            "system/component/operation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "match": {"type": "string", "enum": list(_MATCHES)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string", "minLength": 1, "maxLength": 700},
            },
            "required": ["match", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}


def _tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") == "tool_call"
    ]


async def _judge(
    *,
    client: Any,
    objective: dict[str, Any],
    vehicle: dict[str, Any],
    candidate: dict[str, Any],
    provider: str,
) -> dict[str, Any]:
    packet = {
        "objective": objective.get("objective"),
        "system": objective.get("system"),
        "component": objective.get("component"),
        "vehicle": {
            key: vehicle.get(key)
            for key in ("year", "make", "model", "trim", "vin")
            if vehicle.get(key) not in (None, "")
        },
        "provider": provider,
        "candidate_title": candidate.get("title"),
        "candidate_breadcrumb": candidate.get("breadcrumb") or [],
        "candidate_text": str(candidate.get("text") or "")[:14_000],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are an independent ADAS objective-match critic. The candidate has "
                "already been judged to be a real procedure; your only job is to compare "
                "what that procedure actually performs with the requested system/component/"
                "operation. EXACT_MATCH means it performs the requested objective. "
                "SAME_COMPONENT_WRONG_PROCEDURE means the sensor/component is right but this "
                "is a different operation/article. DIFFERENT_COMPONENT means it is another "
                "component in the same broad ADAS area. DIFFERENT_SENSOR_FAMILY means camera "
                "vs radar or another clearly different sensor family. UNCERTAIN means the "
                "evidence is insufficient. Never infer from the objective alone; judge the "
                "candidate evidence."
            ),
        },
        {"role": "user", "content": json.dumps(packet, default=str)},
    ]
    try:
        events = [
            event
            async for event in client.stream(
                messages,
                tools=[_SCHEMA],
                max_tokens=260,
                tool_choice="required",
            )
        ]
        calls = _tool_calls(events)
        if not calls:
            raise ValueError("objective-match reviewer returned no tool call")
        raw = json.loads(calls[0].get("arguments") or "{}")
        match = str(raw.get("match") or "")
        confidence = float(raw.get("confidence"))
        reason = " ".join(str(raw.get("reason") or "").split())[:700]
        if match not in _MATCHES or not 0 <= confidence <= 1 or not reason:
            raise ValueError("objective-match reviewer returned malformed fields")
        return {
            "match": match,
            "confidence": round(confidence, 3),
            "reason": reason,
            "malformed": False,
        }
    except Exception as exc:  # noqa: BLE001 - fail closed, never accept on review failure
        return {
            "match": "UNCERTAIN",
            "confidence": 0.0,
            "reason": (
                f"Objective-match review failed: {type(exc).__name__}: {exc}"
            )[:700],
            "malformed": True,
        }


def install(review_module: Any, navigator_module: Any) -> None:
    """Add an explicit component/operation match verdict and recovery policy."""
    if getattr(review_module, _INSTALLED_ATTR, False):
        navigator_module.review_candidate = review_module.review_candidate
        return

    original = review_module.review_candidate
    original_instruction = navigator_module._next_instruction_for_review

    @wraps(original)
    async def review_candidate_with_objective_match(*args: Any, **kwargs: Any):
        verdict = await original(*args, **kwargs)
        if not isinstance(verdict, dict):
            return verdict
        objective = (
            kwargs.get("objective")
            if isinstance(kwargs.get("objective"), dict)
            else {}
        )
        # Supporting-document tasks answer a different question and already
        # carry an explicit dependency_context. Do not compare them to the
        # primary component objective again.
        if objective.get("dependency_context"):
            return verdict
        classification = str(verdict.get("classification") or "")
        if classification not in {
            "ACTUAL_PROCEDURE",
            "REQUIRED_SUPPORTING_PROCEDURE",
        }:
            return verdict

        client = kwargs.get("client")
        candidate = (
            kwargs.get("candidate")
            if isinstance(kwargs.get("candidate"), dict)
            else {}
        )
        vehicle = (
            kwargs.get("vehicle")
            if isinstance(kwargs.get("vehicle"), dict)
            else {}
        )
        provider = str(kwargs.get("provider") or "alldata")
        match = await _judge(
            client=client,
            objective=objective,
            vehicle=vehicle,
            candidate=candidate,
            provider=provider,
        )
        out = dict(verdict)
        out["objective_match"] = match
        category = match["match"]
        if category == "EXACT_MATCH":
            # A finer reviewer can confirm an otherwise acceptable result, but
            # it may not override an earlier hard veto such as radar-vs-camera.
            return out

        current_decision = str(out.get("decision") or "")
        out.setdefault("original_decision", current_decision)
        if category == "UNCERTAIN":
            # Preserve a stronger already-proven negative decision from the
            # coarse system-family guard. The second reviewer adds precision;
            # it never weakens an established mismatch back to ambiguity.
            if current_decision not in {"CONTINUE_SEARCH", "REJECT"}:
                out["decision"] = "UNCERTAIN"
        else:
            out["decision"] = "CONTINUE_SEARCH"
        inconsistent = list(out.get("inconsistent") or [])
        inconsistent.append(f"objective_match={category}: {match['reason']}")
        out["inconsistent"] = inconsistent
        return out

    @wraps(original_instruction)
    def next_instruction_with_objective_match(review: dict[str, Any]) -> str:
        match = review.get("objective_match") if isinstance(review, dict) else None
        category = (
            str((match or {}).get("match") or "")
            if isinstance(match, dict)
            else ""
        )
        reason = (
            " ".join(str((match or {}).get("reason") or "").split())[:360]
            if isinstance(match, dict)
            else ""
        )
        if category in {"DIFFERENT_COMPONENT", "DIFFERENT_SENSOR_FAMILY"}:
            return (
                f"Independent objective-match review says {category}: {reason} "
                "Do not extract this page again and do not keep drilling in this rejected "
                "component branch. Backtrack using only the live rendered controls until "
                "alternative ADAS components/systems are visible, then choose the requested "
                "component from that observed state."
            )
        if category == "SAME_COMPONENT_WRONG_PROCEDURE":
            return (
                "Independent objective-match review says SAME_COMPONENT_WRONG_PROCEDURE: "
                f"{reason} Stay within this same component/system area and look for a "
                "different procedure/article that performs the requested operation. Do not "
                "jump to another sensor family, and do not extract this page again."
            )
        return original_instruction(review)

    review_module.review_candidate = review_candidate_with_objective_match
    navigator_module.review_candidate = review_module.review_candidate
    navigator_module._next_instruction_for_review = next_instruction_with_objective_match
    setattr(review_module, _INSTALLED_ATTR, True)
