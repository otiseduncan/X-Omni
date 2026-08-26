from __future__ import annotations

from pathlib import Path

import pytest

from core.services import adas_si, research_conversation, research_policy_depth


class _Page:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self):
        return self._text


class _SixPageReader:
    def __init__(self, *_args, **_kwargs):
        self.pages = [
            _Page("Subaru collision repair introduction."),
            _Page("EyeSight component overview and service precautions."),
            _Page("Windshield and camera service information."),
            _Page("General repair information."),
            _Page("IMPORTANT: EyeSight calibration is required after all collisions."),
            _Page("End of document."),
        ]


def test_local_adas_scan_reads_every_page_and_finds_buried_collision_rule(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    subaru_path = root / "2024 Subaru Outback EyeSight Calibration.pdf"
    toyota_path = root / "2024 Toyota Highlander BSM.pdf"
    subaru_path.write_bytes(b"pdf")
    toyota_path.write_bytes(b"pdf")

    subaru_doc = {**adas_si.describe_document(root, subaru_path), "_path": subaru_path}
    toyota_doc = {**adas_si.describe_document(root, toyota_path), "_path": toyota_path}
    service = adas_si.AdasSI(root, tmp_path / "cache" / "index.sqlite")

    monkeypatch.setattr(service.inventory, "documents", lambda: [subaru_doc, toyota_doc])

    def matching_documents(query, limit=8):
        assert "Subaru" in query
        return [{
            "score": 14,
            "path": subaru_path,
            "descriptor": {k: v for k, v in subaru_doc.items() if k != "_path"},
        }]

    monkeypatch.setattr(service.inventory, "matching_documents", matching_documents)

    pages = [
        (1, "Subaru EyeSight overview."),
        (2, "Camera mounting information."),
        (3, "General collision repair notes."),
        (4, "Inspection procedure."),
        (5, "IMPORTANT: EyeSight calibration is required after all collisions."),
        (6, "End of service document."),
    ]
    monkeypatch.setattr(service, "_pages", lambda path: pages if path == subaru_path else [(1, "Toyota")])

    result = service.search({
        "query": "2024 Subaru EyeSight calibration after collision",
        "search_mode": "calibration_requirements",
    })
    scan = result["calibration_deep_scan"]
    assert scan["enabled"] is True
    assert scan["scanned_full_documents"] is True
    assert scan["uses_native_and_ocr_text"] is True
    assert scan["pages_scanned"] == 6
    assert result["deep_calibration_findings"]
    assert result["deep_calibration_findings"][0]["page"] == 5
    assert "required after all collisions" in result["deep_calibration_findings"][0]["excerpt"].casefold()
    assert all(
        (item.get("vehicle") or {}).get("make") in {None, "Subaru"}
        for item in result["results"]
    )


@pytest.mark.asyncio
async def test_public_pdf_deep_reader_does_not_stop_before_late_calibration_tagline(monkeypatch):
    async def fake_fetch(_url):
        return "https://techinfo.subaru.com/eyesight.pdf", b"%PDF fake", "application/pdf"

    monkeypatch.setattr(research_policy_depth.research_capture, "_bounded_public_fetch", fake_fetch)
    monkeypatch.setattr(research_policy_depth, "PdfReader", _SixPageReader)

    findings, reads, pages, links = await research_policy_depth._read_source_url(
        "https://techinfo.subaru.com/eyesight.pdf",
        title="Subaru EyeSight Collision Repair",
        make="Subaru",
        calibration_mode=True,
        follow_same_host_links=False,
    )

    assert pages == 6
    assert links == 0
    assert reads and reads[0]["deep_read"] is True
    buried = [item for item in findings if item.get("page") == 5]
    assert buried
    assert buried[0]["finding_kind"] == "calibration_requirement"
    assert "required after all collisions" in buried[0]["excerpt"].casefold()


def test_calibration_web_discovery_explicitly_searches_any_and_all_collision_language():
    queries = research_policy_depth._focused_queries(
        "Subaru EyeSight calibration requirements",
        "Subaru",
        calibration_mode=True,
        policy_mode=False,
    )
    folded = "\n".join(queries).casefold()
    assert "after any collision" in folded
    assert "after all collisions" in folded
    assert "position statement" in folded
    assert "replacement removal repair" in folded


def test_deep_html_navigation_only_follows_relevant_same_host_links():
    document = """
    <a href='/collision/eyesight-calibration'>EyeSight Calibration After Collision</a>
    <a href='/owners/warranty'>Warranty</a>
    <a href='https://evil.example/calibration'>Calibration</a>
    <a href='/technical/adas-position-statement'>ADAS Position Statement</a>
    """
    links = research_policy_depth._same_host_deep_links(
        document,
        "https://techinfo.subaru.com/collision/index.html",
    )
    urls = [item[0] for item in links]
    assert "https://techinfo.subaru.com/collision/eyesight-calibration" in urls
    assert "https://techinfo.subaru.com/technical/adas-position-statement" in urls
    assert not any("evil.example" in item for item in urls)
    assert not any("warranty" in item for item in urls)


def test_conversation_distillation_keeps_deep_calibration_finding_without_dumping_research():
    result = {
        "query": "Does Subaru require EyeSight calibration after a collision?",
        "requested_manufacturer": "Subaru",
        "source_ledger": [],
        "adas_si": {"hits": []},
        "alldata": {},
        "public_oem": {
            "calibration_findings": [{
                "title": "Subaru EyeSight Collision Repair",
                "url": "https://techinfo.subaru.com/eyesight.pdf",
                "page": 5,
                "excerpt": "EyeSight calibration is required after all collisions.",
                "authority": "official_manufacturer",
                "finding_kind": "calibration_requirement",
            }],
            "deep_calibration_read": True,
            "deep_read_metrics": {"full_pdf_pages_inspected": 6},
            "sources": [],
        },
        "captures": [],
    }
    distilled = research_conversation._distill(result)
    assert distilled["calibration_findings"][0]["page"] == 5
    assert distilled["deep_read"]["calibration"] is True
    assert distilled["deep_read"]["metrics"]["full_pdf_pages_inspected"] == 6
