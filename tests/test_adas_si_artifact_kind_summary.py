"""Regression coverage for adas_si_inventory's artifact-kind classification.

Live field trace: asked "how many ADAS Map reports do you see in ADAS SI,"
X reported the ADAS SI service's parsed_document_count (a filename-identity
metric with no relationship to document category) as if it were a count of
ADAS Map reports. core/services/adas_artifact_catalog.py already implements
a tested, content-aware classifier (artifact_kind in
{"adas_map_report", "service_information"}) for a different consumer
(calibration_iq_work_prep's weekly RO pipeline); it was never wired to the
conversational adas_si_inventory tool. This test covers that wiring, plus
the field-ordering fix that keeps the classification summary ahead of the
large "documents" list so a downstream size truncation can never cut it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.services.adas_artifact_catalog import AdasArtifactCatalog
from core.services.adas_si import AdasSI


def _pdf(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"%PDF-fixture")
    return path


def test_inventory_read_reports_classified_counts_ahead_of_the_document_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ADAS SI"
    root.mkdir()

    _pdf(root, "2400911695 adas map.pdf")
    _pdf(root, "2020 Toyota Camry Front Camera.pdf")

    texts = {
        "2400911695 adas map.pdf": (
            "ADAS Map Report\nRepair Order: 2400911695\nInspection ID: 5949644\n"
            "VIN: 1HGCM82633A004352\nYear: 2020\nMake: Toyota\nModel: Camry\n"
            "Required Calibrations\nOccupant Classification System\n"
            "End Required Calibrations\n"
        ),
        "2020 Toyota Camry Front Camera.pdf": (
            "OE Service Information\nFront camera calibration procedure.\n"
        ),
    }

    def fake_read_pdf_pages(self, path):  # noqa: ANN001 - test double
        return [(1, texts.get(path.name, ""))]

    monkeypatch.setattr(AdasArtifactCatalog, "_read_pdf_pages", fake_read_pdf_pages)

    adas = AdasSI(root, tmp_path / "index.sqlite")
    result = adas.inventory_read({})

    keys = list(result.keys())
    assert keys.index("artifact_kind_summary") < keys.index("documents")
    assert keys.index("evidence_contract") < keys.index("documents")

    summary = result["artifact_kind_summary"]
    assert summary["counts_are_final"] is True
    assert summary["physical_pdf_count"] == 2
    assert summary["by_artifact_kind"]["adas_map_report"]["count"] == 1
    assert summary["by_artifact_kind"]["service_information"]["count"] == 1
    # This is the specific number the model must never derive from the
    # unrelated "parsed_document_count" filename-identity metric instead.
    assert result["summary"]["document_count"] == 2


def test_inventory_read_degrades_honestly_when_classifier_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken classifier (e.g. no pypdf/pdfium runtime) must not take down
    the whole read-only inventory response -- it degrades to an honest
    "unavailable" status for that one field instead of a hard error."""
    root = tmp_path / "ADAS SI"
    root.mkdir()
    _pdf(root, "2020 Toyota Camry Front Camera.pdf")

    def broken(self):  # noqa: ANN001 - test double
        raise RuntimeError("classifier runtime unavailable")

    monkeypatch.setattr(AdasArtifactCatalog, "artifact_kind_summary", broken)

    adas = AdasSI(root, tmp_path / "index.sqlite")
    result = adas.inventory_read({})

    assert result["status"] == "success"
    assert result["artifact_kind_summary"]["status"] == "unavailable"
    assert "classifier runtime unavailable" in result["artifact_kind_summary"]["message"]
    assert result["summary"]["document_count"] == 1
