import json
from pathlib import Path

import httpx

from scripts import si_research_acceptance as acceptance


def _item(**overrides):
    item = {
        "case": {"id": "case-1", "vin": "1HGCV1F30PA000001"},
        "error": None,
        "verified": True,
        "complete": True,
        "semantic_review": {"decision": "ACCEPT"},
        "evidence_title": "Millimeter Wave Radar Aiming",
        "evidence_chars": 1200,
        "captured": True,
    }
    item.update(overrides)
    return item


def _response(status: int, text: str) -> httpx.Response:
    return httpx.Response(
        status,
        text=text,
        request=httpx.Request("POST", "http://worker/v1/chat/completions"),
    )


def test_strict_acceptance_requires_complete_reviewed_captured_exact_vin_case():
    assert acceptance.acceptance_failures(_item(), capture=True, require_vin=True) == []


def test_strict_acceptance_reports_every_missing_proof_without_judging_terms():
    failures = acceptance.acceptance_failures(
        _item(
            case={"id": "case-1"},
            verified=False,
            complete=False,
            semantic_review={"decision": "FOLLOW_DEPENDENCY"},
            evidence_title=None,
            evidence_chars=0,
            captured=False,
        ),
        capture=True,
        require_vin=True,
    )
    assert "exact VIN was not resolved from Calibration IQ" in failures
    assert "X did not accept a mechanically verified procedure" in failures
    assert "the procedure/dependency set is incomplete" in failures
    assert "independent semantic review did not accept the candidate" in failures
    assert "no evidence title was returned" in failures
    assert "no extracted evidence text was returned" in failures
    assert "accepted evidence was not captured" in failures


def test_followed_dependency_passes_when_the_dependency_document_was_accepted():
    item = _item(
        semantic_review={"decision": "FOLLOW_DEPENDENCY"},
        documents=[{"accepted": True, "decision": "ACCEPT", "captured": True}],
    )
    assert acceptance.acceptance_failures(item, capture=True, require_vin=True) == []


def test_phase_b_retries_only_known_llamacpp_tool_json_500():
    assert acceptance._retryable_tool_json_response(
        _response(500, "Failed to parse tool call arguments as JSON")
    ) is True
    assert acceptance._retryable_tool_json_response(
        _response(500, "internal allocation failure")
    ) is False
    assert acceptance._retryable_tool_json_response(
        _response(400, "Failed to parse tool call arguments as JSON")
    ) is False


def test_phase_b_set_is_exactly_ten_named_real_ro_cases():
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "scripts" / "si_research_cases.json").read_text(encoding="utf-8"))
    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == 10
    for case in cases:
        assert all(case.get(field) for field in ("id", "year", "make", "model", "ro", "topic", "expect"))
