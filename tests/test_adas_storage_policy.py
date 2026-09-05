from __future__ import annotations

import sqlite3
from pathlib import Path

from core.services import adas_si
from core.services import adas_storage


def _cache_with_rows(cache: Path, pdf: Path, *, kind: str, ro: str | None = None):
    cache.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache) as db:
        db.execute(
            "CREATE TABLE pages(path TEXT,page INTEGER,text TEXT,source_mtime_ns INTEGER)"
        )
        db.execute(
            """
            CREATE TABLE artifact_catalog(
                path TEXT PRIMARY KEY,
                artifact_kind TEXT,
                ro_number TEXT,
                year INTEGER,
                make TEXT,
                model TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO pages VALUES(?,?,?,?)",
            (str(pdf.resolve()), 1, "cached", 1),
        )
        db.execute(
            "INSERT INTO artifact_catalog VALUES(?,?,?,?,?,?)",
            (
                str(pdf.resolve()),
                kind,
                ro,
                2023 if kind != "adas_map_report" else None,
                "Toyota" if kind != "adas_map_report" else None,
                "Camry" if kind != "adas_map_report" else None,
            ),
        )


def test_service_information_migrates_to_year_make_model_and_invalidates_cache(
    tmp_path: Path,
):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    old = root / "Acquired" / "ALLDATA" / "2023 Toyota Camry"
    old.mkdir(parents=True)
    pdf = old / "Front Camera Calibration.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    cache = tmp_path / "index.sqlite"
    _cache_with_rows(cache, pdf, kind="service_information")

    result = adas_storage.migrate_library(root, cache, adas_si.describe_document)

    expected = (
        root
        / "2023"
        / "Toyota"
        / "Camry"
        / "ALLDATA"
        / "Front Camera Calibration.pdf"
    )
    assert result["moved"] == 1
    assert expected.is_file()
    assert not pdf.exists()
    with sqlite3.connect(cache) as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifact_catalog").fetchone()[0] == 0


def test_adas_map_migrates_to_repair_order_folder(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    pdf = root / "2400911731 adas map.pdf"
    pdf.write_bytes(b"%PDF-1.4 map")
    cache = tmp_path / "empty.sqlite"

    result = adas_storage.migrate_library(root, cache, adas_si.describe_document)

    expected = (
        root
        / "ADAS Map"
        / "2400911731"
        / "2400911731 ADAS Map.pdf"
    )
    assert result["moved"] == 1
    assert expected.is_file()
    assert not pdf.exists()


def test_canonical_path_supplies_vehicle_identity_when_filename_has_no_vehicle(
    tmp_path: Path,
):
    root = tmp_path / "ADAS SI"
    pdf = root / "2024" / "Ford" / "F-150" / "ALLDATA" / "Front Camera Procedure.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")

    descriptor = adas_si.describe_document(root, pdf)

    assert descriptor["application_parsed"] is True
    assert descriptor["parse_confidence"] == "path"
    assert descriptor["year"] == 2024
    assert descriptor["make"] == "Ford"
    assert descriptor["model"] == "F-150"


def test_storage_directory_rule_is_year_make_model(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    target = adas_storage.service_information_directory(
        root,
        {"year": 2022, "make": "Honda", "model": "CR-V"},
        "ALLDATA",
    )
    assert target == root.resolve() / "2022" / "Honda" / "CR-V" / "ALLDATA"


def test_adas_map_inside_old_vehicle_tree_is_still_moved_by_ro(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    old = root / "2023" / "Toyota" / "Camry"
    old.mkdir(parents=True)
    pdf = old / "2400911777 adas map.pdf"
    pdf.write_bytes(b"%PDF-1.4 map")
    cache = tmp_path / "empty2.sqlite"

    result = adas_storage.migrate_library(root, cache, adas_si.describe_document)

    expected = (
        root
        / "ADAS Map"
        / "2400911777"
        / "2400911777 ADAS Map.pdf"
    )
    assert result["moved"] == 1
    assert expected.is_file()
    assert not pdf.exists()


def test_runtime_auto_migration_is_scoped_to_configured_authoritative_root(
    tmp_path: Path,
    monkeypatch,
):
    live = tmp_path / "Live ADAS SI"
    scratch = tmp_path / "Scratch ADAS SI"
    live.mkdir()
    scratch.mkdir()
    monkeypatch.setenv("XOMNI_ADAS_SI_ROOT", str(live))

    assert adas_storage.is_authoritative_runtime_root(live) is True
    assert adas_storage.is_authoritative_runtime_root(scratch) is False
