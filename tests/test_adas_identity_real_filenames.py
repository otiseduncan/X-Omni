from __future__ import annotations

from pathlib import Path

from core.services import adas_identity_guard
from core.services import adas_si as adas_mod


def _doc(root: Path, name: str) -> dict:
    path = root / name
    path.write_bytes(b"pdf")
    return {**adas_mod.describe_document(root, path), "_path": path}


def test_make_fallback_understands_compact_real_world_filenames(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    root.mkdir()

    hyundai = _doc(root, "HyundaiPalisade(2020-25)BlindSpotMonitorCalibration.pdf")
    toyota = _doc(root, "2023-2026 Toyota Highlander BSM.pdf")
    lexus = _doc(root, "2022 Lexus ES 350 FWD parking assist monitor.pdf")

    assert adas_identity_guard.descriptor_make(hyundai, adas_mod) == "Hyundai"
    assert adas_identity_guard.descriptor_make(toyota, adas_mod) == "Toyota"
    assert adas_identity_guard.descriptor_make(lexus, adas_mod) == "Lexus"


def test_explicit_toyota_filter_drops_hyundai_and_lexus_even_with_compact_filename(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    docs = [
        _doc(root, "HyundaiPalisade(2020-25)BlindSpotMonitorCalibration.pdf"),
        _doc(root, "2023-2026 Toyota Highlander BSM.pdf"),
        _doc(root, "2022 Lexus ES 350 FWD parking assist monitor.pdf"),
    ]

    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(inventory, "documents", lambda: docs)
    results = inventory.matching_documents(
        "Toyota recycled blind spot monitor module",
        limit=8,
    )

    assert results
    assert [
        adas_identity_guard.descriptor_make(item["descriptor"], adas_mod)
        for item in results
    ] == ["Toyota"]
    assert all("Hyundai" not in item["descriptor"]["title"] for item in results)
    assert all("Lexus" not in item["descriptor"]["title"] for item in results)
