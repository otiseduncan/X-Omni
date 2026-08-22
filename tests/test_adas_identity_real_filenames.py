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


def test_policy_words_after_make_are_not_mistaken_for_model_name():
    assert adas_identity_guard.explicit_model(
        "Toyota recycled blind spot monitor module",
        "Toyota",
        adas_mod,
    ) is None


def test_adas_system_name_after_make_is_not_mistaken_for_model_name():
    assert adas_identity_guard.explicit_model(
        "2024 Subaru EyeSight calibration after collision",
        "Subaru",
        adas_mod,
    ) is None


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


def test_2024_ford_transit_does_not_accept_maverick_or_f150(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    docs = [
        _doc(root, "2024 Ford Truck Maverick FWD Front Camera Calibration.pdf"),
        _doc(root, "2024 Ford Transit Front Camera Calibration.pdf"),
        _doc(root, "2024 Ford F-150 Front Camera Calibration.pdf"),
    ]
    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(inventory, "documents", lambda: docs)

    results = inventory.matching_documents(
        "I need the forward facing calibration procedure for a 24 Ford Transit",
        limit=8,
    )

    assert results
    assert [item["descriptor"]["model"] for item in results] == ["Transit"]


def test_transit_request_returns_no_same_make_substitute_when_transit_is_absent(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    docs = [
        _doc(root, "2024 Ford Truck Maverick FWD Front Camera Calibration.pdf"),
        _doc(root, "2024 Ford F-150 Front Camera Calibration.pdf"),
    ]
    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(inventory, "documents", lambda: docs)

    results = inventory.matching_documents(
        "2024 Ford Transit forward facing camera calibration",
        limit=8,
    )
    assert results == []


def test_cherokee_trim_can_match_base_cherokee_document(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    docs = [
        _doc(root, "2021 Jeep Cherokee BSM Calibration.pdf"),
        _doc(root, "2021 Jeep Grand Cherokee BSM Calibration.pdf"),
    ]
    inventory = adas_mod.SourceInventory(root)
    monkeypatch.setattr(inventory, "documents", lambda: docs)

    results = inventory.matching_documents(
        "2021 Jeep Cherokee Latitude Luxe BSM calibration procedure",
        limit=8,
    )
    assert results
    assert [item["descriptor"]["model"] for item in results] == ["Cherokee"]
