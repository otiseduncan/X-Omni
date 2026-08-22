from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import research_conversation, research_policy_depth, research_workflow


class _Page:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class _Reader:
    def __init__(self, *_args, **_kwargs):
        self.pages = [
            _Page("ordinary collision repair page"),
            _Page("ordinary repair procedures"),
            _Page("welding information"),
            _Page("structural repair guidance"),
            _Page(
                "TOYOTA & LEXUS APPROVED REPAIR METHODS. METHODS NOT APPROVED: "
                "Installing Aftermarket and Recycled Parts."
            ),
        ]


@pytest.mark.asyncio
async def test_deep_pdf_policy_reader_finds_later_page(monkeypatch):
    async def fake_fetch(_url):
        return "https://www.toyotapartsandservice.com/policy.pdf", b"%PDF fake", "application/pdf"

    monkeypatch.setattr(research_policy_depth.research_capture, "_bounded_public_fetch", fake_fetch)
    monkeypatch.setattr(research_policy_depth, "PdfReader", _Reader)

    findings = await research_policy_depth._read_policy_source(
        {
            "title": "Toyota Collision Pros",
            "url": "https://www.toyotapartsandservice.com/policy.pdf",
        },
        "Toyota",
    )
    assert findings
    assert findings[0]["page"] == 5
    assert findings[0]["authority"] == "official_manufacturer"
    assert "recycled" in findings[0]["excerpt"].casefold()
    assert "not approved" in findings[0]["excerpt"].casefold()


def test_conversation_distillation_keeps_policy_finding_and_hides_debug_bulk():
    result = {
        "query": "Toyota recycled blind spot module",
        "requested_manufacturer": "Toyota",
        "source_ledger": [
            {"source": "ADAS SI", "verified": True, "result_count": 4},
            {"source": "ALLDATA", "verified": False, "reason": "No field"},
            {"source": "Public OEM web", "verified": True, "result_count": 8},
        ],
        "adas_si": {"hits": [{"title": "Toyota BSM", "page": 2, "excerpt": "calibration"}]},
        "alldata": {"verified": False, "reason": "No field"},
        "public_oem": {
            "policy_findings": [{
                "title": "Toyota Collision Pros",
                "url": "https://www.toyotapartsandservice.com/policy.pdf",
                "page": 5,
                "excerpt": "METHODS NOT APPROVED Installing Aftermarket and Recycled Parts",
                "authority": "official_manufacturer",
            }],
            "sources": [],
        },
        "captures": [],
    }
    distilled = research_conversation._distill(result)
    assert distilled["policy_findings"][0]["page"] == 5
    assert "source_ledger" in distilled
    assert "policy_findings" in distilled


@pytest.mark.asyncio
async def test_conversational_synthesis_is_short_and_does_not_append_source_ledger():
    class Client:
        async def complete(self, messages, max_tokens, temperature):
            assert max_tokens == 420
            assert "Do not list the research process" in messages[0]["content"]
            return (
                "Toyota's collision-repair guidance does not approve installing aftermarket or recycled parts. "
                "The strongest support is Toyota Collision Pros' approved-repair-methods sidebar on page 5, "
                "which places aftermarket and recycled parts under methods not approved. "
                "That is stronger policy evidence than inferring approval from a BSM calibration procedure."
            )

    result = {
        "query": "Toyota recycled blind spot module",
        "requested_manufacturer": "Toyota",
        "source_ledger": [],
        "adas_si": {"hits": []},
        "alldata": {},
        "public_oem": {
            "policy_findings": [{
                "title": "Toyota Collision Pros",
                "url": "https://www.toyotapartsandservice.com/policy.pdf",
                "page": 5,
                "excerpt": "METHODS NOT APPROVED Installing Aftermarket and Recycled Parts",
                "authority": "official_manufacturer",
            }],
            "sources": [],
        },
        "captures": [],
    }
    answer = await research_conversation.conversational_synthesize(
        SimpleNamespace(client=Client()),
        "Does Toyota allow a recycled BSM module?",
        result,
    )
    assert len(answer) < 900
    assert "source ledger" not in answer.casefold()
    assert "recycled" in answer.casefold()
    assert "page 5" in answer.casefold()
