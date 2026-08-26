from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.services import (
    research_conversation,
    research_operator,
    research_policy_depth,
    research_workflow,
)


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


@pytest.mark.asyncio
async def test_collision_research_explicit_depth_uses_bounded_deep_reader(
    monkeypatch, tmp_path
):
    captured = {}

    async def deep(query, make, *, source_depth):
        captured.update({"query": query, "make": make, "source_depth": source_depth})
        return {
            "verified": True,
            "sources": [{"url": "https://oem.example/calibration.pdf"}],
            "calibration_findings": [{"page": 7, "matched_term": "must calibrate"}],
            "deep_read_metrics": {
                "full_pdf_pages_inspected": 9,
                "same_host_links_read": 1,
            },
        }

    monkeypatch.setattr(research_policy_depth, "deep_search_public_oem", deep)
    browser = research_operator.LicensedBrowser(tmp_path)
    result = await browser.operator_action(
        {
            "action": "public_search",
            "query": "windshield camera calibration",
            "manufacturer": "Toyota",
            "source_depth": "calibration_requirements",
        }
    )

    assert captured == {
        "query": "windshield camera calibration",
        "make": "Toyota",
        "source_depth": "calibration_requirements",
    }
    assert result["status"] == "success"
    assert result["action"] == "public_search"
    assert result["source_depth"] == "calibration_requirements"
    assert result["calibration_findings"][0]["page"] == 7


@pytest.mark.asyncio
async def test_nonstandard_workflow_depth_enters_standard_discovery_once(monkeypatch):
    calls = {"search": 0, "read": 0, "fetch": 0}

    async def public_search(_args):
        calls["search"] += 1
        return {
            "query": "camera calibration",
            "external_network": True,
            "source_bounded": True,
            "providers": ["fixture"],
            "sources": [
                {
                    "title": "Toyota calibration requirements",
                    "url": "https://toyota.example/calibration",
                    "snippet": "OEM collision camera calibration requirement",
                }
            ],
        }

    async def public_read(args):
        calls["read"] += 1
        return {
            "url": args["url"],
            "title": "Toyota calibration requirements",
            "content_type": "text/html",
            "page_text": "The forward camera must be calibrated after windshield replacement.",
        }

    async def bounded_fetch(url):
        calls["fetch"] += 1
        return (
            url,
            b"<html><body>The forward camera must be calibrated after windshield replacement.</body></html>",
            "text/html",
        )

    monkeypatch.setattr(research_operator, "public_search", public_search)
    monkeypatch.setattr(research_operator, "public_read", public_read)
    monkeypatch.setattr(
        research_policy_depth.research_capture,
        "_bounded_public_fetch",
        bounded_fetch,
    )

    result = await research_workflow.search_public_oem(
        "forward camera after windshield replacement",
        "Toyota",
        source_depth="calibration_requirements",
    )

    assert result["deep_calibration_read"] is True
    assert result["calibration_findings"]
    assert calls["search"] >= 1
    assert calls["read"] == 1
    assert calls["fetch"] >= 1


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
