"""Unit tests for the bounded model truth-review machinery.

The deterministic layer here validates only JSON *shape* and enforces the
no-tools boundary; every semantic judgment belongs to the model. These tests
pin both properties.
"""

from __future__ import annotations

import json

import pytest

from core.orchestrator import truth_review as tr


# ---------------------------------------------------------------------------
# parse: shape validation only
# ---------------------------------------------------------------------------


def test_parse_accepts_plain_contract_json() -> None:
    review = tr.parse_truth_review(
        json.dumps(
            {
                "valid": False,
                "unsupported_claims": ["Says closed; snapshot shows OPEN."],
                "contradictions": ["Receipt is not verified."],
                "correction_guidance": "State that the close could not be verified.",
            }
        )
    )
    assert review is not None
    assert review.valid is False
    assert review.unsupported_claims == ("Says closed; snapshot shows OPEN.",)
    assert review.contradictions == ("Receipt is not verified.",)
    assert review.correction_guidance.startswith("State that")


def test_parse_tolerates_code_fences_and_prose_wrapping() -> None:
    wrapped = 'Here is my check:\n```json\n{"valid": true, "unsupported_claims": [], "contradictions": [], "correction_guidance": ""}\n```\nDone.'
    review = tr.parse_truth_review(wrapped)
    assert review is not None and review.valid is True


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "It looks fine to me.",
        '{"valid": "yes"}',  # non-boolean verdict
        '{"unsupported_claims": []}',  # missing verdict
        "{not json",
    ],
)
def test_parse_rejects_anything_outside_the_contract(raw: str) -> None:
    assert tr.parse_truth_review(raw) is None


def test_parse_bounds_item_counts_and_lengths() -> None:
    review = tr.parse_truth_review(
        json.dumps(
            {
                "valid": False,
                "unsupported_claims": ["x" * 10_000] * 50,
                "contradictions": [],
                "correction_guidance": "g" * 10_000,
            }
        )
    )
    assert review is not None
    assert len(review.unsupported_claims) == tr.MAX_REVIEW_ITEMS
    assert all(
        len(item) <= tr.MAX_REVIEW_ITEM_CHARS for item in review.unsupported_claims
    )
    assert len(review.correction_guidance) <= tr.MAX_GUIDANCE_CHARS


# ---------------------------------------------------------------------------
# fail-closed vocabulary stays tiny
# ---------------------------------------------------------------------------


def test_fail_closed_texts_are_short_and_never_audit_reports() -> None:
    for state in ("verified", "failed", "indeterminate", "anything-else"):
        text = tr.fail_closed_text(state)
        assert 0 < len(text) < 120
        assert "receipt" not in text.casefold()
    assert tr.fail_closed_text("unknown") == tr.fail_closed_text("indeterminate")


# ---------------------------------------------------------------------------
# the no-tools boundary
# ---------------------------------------------------------------------------


class _ToolHappyClient:
    """A model that tries to call a tool during review anyway."""

    def __init__(self) -> None:
        self.tools_seen: list[object] = []

    async def stream(self, _messages, tools=None):
        self.tools_seen.append(tools)
        yield {"type": "tool_call", "id": "sneaky", "name": "calibration_iq_operator"}
        yield {
            "type": "content",
            "text": json.dumps(
                {
                    "valid": True,
                    "unsupported_claims": [],
                    "contradictions": [],
                    "correction_guidance": "",
                }
            ),
        }


@pytest.mark.asyncio
async def test_review_call_advertises_no_tools_and_ignores_tool_call_events() -> None:
    client = _ToolHappyClient()
    review = await tr.review_candidate(client, [], "Done.")
    assert client.tools_seen == [None]
    assert review is not None and review.valid is True


@pytest.mark.asyncio
async def test_regeneration_and_synthesis_also_advertise_no_tools() -> None:
    client = _ToolHappyClient()
    review = tr.TruthReview(
        valid=False,
        unsupported_claims=("overclaim",),
        contradictions=(),
        correction_guidance="",
    )
    await tr.regenerate_candidate(client, [], "Done.", review)
    await tr.synthesize_candidate(client, [], "{}")
    assert client.tools_seen == [None, None]


class _BrokenClient:
    async def stream(self, _messages, tools=None):
        raise RuntimeError("worker died")
        yield  # pragma: no cover - makes this an async generator

@pytest.mark.asyncio
async def test_transport_failures_fail_closed_not_open() -> None:
    review = await tr.review_candidate(_BrokenClient(), [], "Done.")
    assert review is None
    regenerated = await tr.regenerate_candidate(
        _BrokenClient(),
        [],
        "Done.",
        tr.TruthReview(False, (), (), ""),
    )
    assert regenerated == ""
    synthesized = await tr.synthesize_candidate(_BrokenClient(), [], "{}")
    assert synthesized == ""


def test_review_prompts_forbid_rewriting_and_tool_choice() -> None:
    text = tr.TRUTH_REVIEW_INSTRUCTION.casefold()
    assert "do not rewrite" in text
    assert "do not choose tools" in text
    assert "short answer is valid" in text
    regen = tr.REGENERATION_INSTRUCTION.casefold()
    assert "do not repeat or re-execute" in regen


def test_reviewer_is_a_truth_gate_not_a_style_gate() -> None:
    """Correction guidance must not be able to bloat the regenerated answer.

    The reviewer judges truth only. If its guidance were free to demand
    receipts or a fuller breakdown, the regenerated answer would grow an audit
    tail and then pass review -- reintroducing the very verbosity this work
    removed, one layer later.
    """
    text = tr.TRUTH_REVIEW_INSTRUCTION.casefold()
    assert "brevity is never a defect" in text
    assert "extra detail is never required" in text
    assert "never ask for receipts, ids, versions, timestamps, or a fuller breakdown" in text
    assert "one short sentence" in text


def test_reviewer_may_accept_a_failure_reason_the_evidence_carries() -> None:
    """Observed live: a truthful reason was rejected as an overreach.

    Evidence held error code 'conflict' / 'Version changed.', and the
    candidate "could not be closed due to a version conflict" was still
    rejected -- costing the answer its specificity and falling back to the
    generic line. Restating what the evidence contains is supported.
    """
    text = tr.TRUTH_REVIEW_INSTRUCTION.casefold()
    assert "restating a failure reason the evidence itself carries" in text
    assert "is supported, not an overreach" in text
