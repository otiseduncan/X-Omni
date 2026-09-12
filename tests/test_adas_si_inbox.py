from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.services import adas_si
from core.services.adas_si_classifier import classify_root_pdf


def test_root_classifier_keeps_multi_vehicle_reference_out_of_vehicle_tree() -> None:
    result = classify_root_pdf(
        Path("capture.pdf"),
        [
            (
                1,
                "Calibration Requirements\nHonda / Acura Front Radar\n"
                "Honda / Acura Front Windshield Camera\nAll models",
            )
        ],
    )

    assert result["storage_class"] == "reference"
    assert result["group"] == "Honda Acura"
    assert result["title"] == "Honda Acura ADAS Calibration Requirements"
    assert "vehicle" not in result


def test_root_classifier_extracts_adas_map_repair_order() -> None:
    result = classify_root_pdf(
        Path("anonymous.pdf"),
        [
            (
                1,
                "ADAS MAP - Estimate Analysis\nRO Number: 2400612271\n"
                "Year: 2026\nMake: Hyundai\nModel: Tucson SE FWD",
            )
        ],
    )

    assert result["storage_class"] == "adas_map_report"
    assert result["ro_number"] == "2400612271"
    assert result["vehicle"] == {
        "year": "2026",
        "make": "Hyundai",
        "model": "Tucson SE FWD",
    }


def test_inventory_discovers_files_added_after_start_and_files_them(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ADAS SI"
    root.mkdir()
    cache = tmp_path / "cache" / "index.sqlite"
    monkeypatch.setenv("XOMNI_ADAS_SI_ROOT", str(root))
    service = adas_si.AdasSI(root, cache)

    map_pdf = root / "anonymous.pdf"
    reference_pdf = root / "sheet.pdf"
    unknown_pdf = root / "mystery.pdf"
    for path in (map_pdf, reference_pdf, unknown_pdf):
        path.write_bytes(b"%PDF-1.4 placeholder")

    texts = {
        "anonymous.pdf": (
            "ADAS MAP - Estimate Analysis\nRO Number: 2400612271\n"
            "Year: 2026\nMake: Hyundai\nModel: Tucson SE FWD"
        ),
        "sheet.pdf": (
            "Calibration Requirements\nHonda Acura Front Radar\n"
            "Honda Acura Front Windshield Camera"
        ),
        "mystery.pdf": "Unidentified document",
    }
    monkeypatch.setattr(service, "_pages", lambda path: [(1, texts[path.name])])

    refresh = service.refresh_library(organize_root=True)

    assert refresh["moved_count"] == 3
    assert refresh["classified_count"] == 2
    assert refresh["review_required_count"] == 1
    assert (root / "ADAS Map" / "2400612271" / "2400612271 ADAS Map.pdf").is_file()
    assert (
        root
        / "Reference"
        / "Calibration Requirements"
        / "Honda Acura"
        / "Honda Acura ADAS Calibration Requirements.pdf"
    ).is_file()
    assert (root / "Needs Review" / "mystery.pdf").is_file()
    assert list(root.glob("*.pdf")) == []

    inventory = service.inventory_read(
        {
            "added_since": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "added_before": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
    )
    recent = inventory["recent_additions"]
    assert recent["count"] == 3
    assert {item["storage_class"] for item in recent["documents"]} == {
        "adas_map_report",
        "reference",
        "needs_review",
    }
    assert inventory["summary"]["root_drop_count"] == 0
    assert inventory["summary"]["needs_review_document_count"] == 1


def test_inventory_time_window_requires_offset(tmp_path) -> None:
    root = tmp_path / "ADAS SI"
    root.mkdir()
    service = adas_si.AdasSI(root, tmp_path / "index.sqlite")

    try:
        service.inventory_read({"added_since": "2026-09-12T00:00:00"})
    except ValueError as exc:
        assert "UTC offset" in str(exc)
    else:
        raise AssertionError("naive arrival boundary was accepted")
