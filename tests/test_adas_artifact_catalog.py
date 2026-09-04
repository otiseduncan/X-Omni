from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from core.services import adas_artifact_catalog as catalog_mod
from core.services.adas_artifact_catalog import (
    AdasArtifactCatalog,
    COVERED,
    DISCOVERY_AMBIGUOUS,
    DISCOVERY_UNVERIFIED,
    DISCOVERY_VERIFIED,
    MISSING,
    UNVERIFIED,
)


VIN = "1HGCM82633A004352"


def test_live_print_text_corruption_still_parses_bounded_map_and_oe_sections():
    text = """
ADAS MAP - Es\x00mate Analysis
RO Number: 2400000001 Inspec\x00on Number: 5940000
Year: 2020 Insurance: Example
Make: Toyota Last Updated: today
Model: Camry XLE Es\x00mate Source: CCC
VIN: 1HGCM82633A004352 Es\x00mator: Example
Iden\x00ﬁed ADAS Related Services to be Performed
Based on the es\x00mate, the following calibra\x00ons/ini\x00ializa\x00ons have been iden\x00ﬁed:
    • Seat Weight Sensor of Occupant Classi\x00ﬁca\x00on System
Es\x00mate Lines 1, 2
    • Seat Belt Inspec\x00on of Seat Belt
Es\x00mate Lines 1, 2
Scanning Requirements
Required Calibra\x00on Details
Occupant Detec\x00on
OE Service Informa\x00on:
Perform Zero Point Calibra\x00on and Sensi\x00vity Check for the Seat Weight Sensor.
Seat Belt Inspec\x00on
OE Service Informa\x00on:
Inspect the seat belt a\x00er a collision.
"""

    parsed = catalog_mod._parse_report_text(text, "2400000001")  # noqa: SLF001

    assert parsed["ro_number"] == "2400000001"
    assert parsed["inspection_id"] == "5940000"
    assert parsed["vin"] == VIN
    assert [item["family"] for item in parsed["requirements"]] == [
        "occupant_classification",
        "seat_belt",
    ]
    assert set(parsed["oe_requirement_families"]) >= {
        "occupant_classification",
        "seat_belt",
    }


def test_catalog_pdf_extraction_uses_supported_pdfium_fallback(tmp_path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    source = root / "portal-generated-name.pdf"
    source.write_bytes(b"%PDF fixture")
    lifecycle: list[str] = []

    class BrokenPdfReader:
        def __init__(self, *_args, **_kwargs):
            raise ValueError("pypdf rejected a PDFium-readable document")

    class FakeTextPage:
        def get_text_range(self):
            return "Inspection ID: 5949644"

        def close(self):
            lifecycle.append("text_closed")

    class FakePage:
        def get_textpage(self):
            return FakeTextPage()

        def close(self):
            lifecycle.append("page_closed")

    class FakeDocument:
        def __init__(self, path):
            assert path == str(source)
            lifecycle.append("document_opened")

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return FakePage()

        def close(self):
            lifecycle.append("document_closed")

    class FakePdfium:
        PdfDocument = FakeDocument

    monkeypatch.setattr(catalog_mod, "PdfReader", BrokenPdfReader)
    monkeypatch.setattr(catalog_mod, "pdfium", FakePdfium)
    catalog = AdasArtifactCatalog(root, tmp_path / "index.sqlite")

    assert catalog._read_pdf_pages(source) == [(1, "Inspection ID: 5949644")]
    assert lifecycle == [
        "document_opened",
        "text_closed",
        "page_closed",
        "document_closed",
    ]


def test_catalog_version_upgrade_reprocesses_blank_cached_pages_with_pdfium(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    source = root / "2020 Toyota Camry Front Camera.pdf"
    source.write_bytes(b"%PDF fixture")
    cache = tmp_path / "index.sqlite"

    class BlankPage:
        def extract_text(self):
            return ""

    class BlankPdfReader:
        def __init__(self, *_args, **_kwargs):
            self.pages = [BlankPage()]

    monkeypatch.setattr(catalog_mod, "PdfReader", BlankPdfReader)
    monkeypatch.setattr(catalog_mod, "pdfium", None)
    catalog = AdasArtifactCatalog(root, cache, tmp_path / "missing-scrapex.sqlite")

    first = catalog.reconcile_index()
    assert first["unreadable"] == 1
    with sqlite3.connect(cache) as db:
        db.execute(
            "UPDATE meta SET value='4' WHERE key='artifact_catalog_schema_version'"
        )

    class FakeTextPage:
        def get_text_range(self):
            return "OEM service information. Front camera calibration procedure."

        def close(self):
            pass

    class FakePage:
        def get_textpage(self):
            return FakeTextPage()

        def close(self):
            pass

    class FakeDocument:
        def __init__(self, _path):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, _index):
            return FakePage()

        def close(self):
            pass

    class FakePdfium:
        PdfDocument = FakeDocument

    monkeypatch.setattr(catalog_mod, "pdfium", FakePdfium)

    upgraded = catalog.reconcile_index()

    assert upgraded["scan_complete"] is True
    assert upgraded["unreadable"] == 0
    with sqlite3.connect(cache) as db:
        row = db.execute(
            "SELECT readable,text_content FROM artifact_catalog WHERE path=?",
            (str(source),),
        ).fetchone()
        version = db.execute(
            "SELECT value FROM meta WHERE key='artifact_catalog_schema_version'"
        ).fetchone()[0]
    assert row == (
        1,
        "OEM service information. Front camera calibration procedure.",
    )
    assert version == catalog_mod.CATALOG_SCHEMA_VERSION


class TextCatalog(AdasArtifactCatalog):
    def __init__(self, *args, texts: dict[str, str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.texts = texts if texts is not None else {}

    def _read_pdf_pages(self, path: Path):
        text = self.texts.get(path.name)
        return [] if text is None else [(1, text)]


def _pdf(root: Path, relative: str, text: str, texts: dict[str, str]) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("%PDF-audit-" + relative).encode())
    texts[path.name] = text
    return path


def _report_text(
    ro: str,
    *,
    vin: str = VIN,
    year: int = 2020,
    make: str = "Toyota",
    model: str = "Camry",
    requirements: tuple[str, ...] = ("Occupant Classification System",),
    oe_requirements: tuple[str, ...] = (),
) -> str:
    lines = [
        "ADAS Map Report",
        f"Repair Order: {ro}",
        "Inspection ID: 5949644",
        f"VIN: {vin}",
        f"Year: {year}",
        f"Make: {make}",
        f"Model: {model}",
        "Required Calibrations",
        *requirements,
        "End Required Calibrations",
    ]
    if oe_requirements:
        lines.extend(
            [
                "OE Service Information",
                *(f"{label} - OEM service procedure" for label in oe_requirements),
                "End OE Service Information",
            ]
        )
    return "\n".join(lines)


def _create_scrapex(path: Path, rows: list[dict]) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE items(
               id TEXT, ro_id TEXT, ro_number TEXT, vin TEXT, year INTEGER,
               make TEXT, model TEXT, trim TEXT, adas_map_contract_version INTEGER,
               adas_map_state TEXT, adas_map_inspection_id TEXT,
               adas_map_source_url TEXT, adas_map_requirements_json TEXT,
               adas_map_requirements_proven INTEGER, adas_map_raw_result_json TEXT,
               ciq_reconciliation_state TEXT, ciq_reconciliation_json TEXT,
               adas_map_checked_at TEXT)"""
        )
        for row in rows:
            columns = list(row)
            db.execute(
                f"INSERT INTO items({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )


def _canonical_row(
    *,
    item_id: str = "canonical-1",
    ro: str = "2400911695",
    requirement: str | None = "Occupant Classification System",
    explicit_no_calibration: bool = False,
    map_state: str = "adas_map_complete",
    ciq_state: str = "complete",
    ciq_verified: bool = True,
    raw_overrides: dict | None = None,
) -> dict:
    ro_id = "ciq-ro-1"
    inspection = "5949644"
    requirements = (
        [
            {
                "label": requirement,
                "source": "adas_map_required_list_item",
                "source_context": "selected_required_modal",
                "source_control_class": "btn btn-link custom-link",
                "source_context_runtime_id": "modal-1",
            }
        ]
        if requirement
        else []
    )
    reconciliation = {
        "verified": ciq_verified,
        "snapshot_verified": ciq_verified,
        "repair_order_id": ro_id,
        "inspection_id": inspection,
        "receipt_count": 1 if ciq_verified else 0,
        "explicit_no_calibration": explicit_no_calibration,
    }
    raw_result = {
        "success": True,
        "status": "complete",
        "ciq_ro_id": ro_id,
        "ro_number": ro,
        "vin": VIN,
        "vehicle": {"year": 2020, "make": "Toyota", "model": "Camry", "trim": "LE"},
        "inspection_id": inspection,
        "source_url": "https://adas-map.invalid/exact-inspection",
        "requirements_proven": True,
        "row_binding_confirmed": True,
        "modal_inspection_confirmed": True,
        "required_region_confirmed": True,
        "modal_runtime_id": "modal-1",
        "requirement_records": requirements,
        "explicit_no_calibration": explicit_no_calibration,
    }
    raw_result.update(raw_overrides or {})
    return {
        "id": item_id,
        "ro_id": ro_id,
        "ro_number": ro,
        "vin": VIN,
        "year": 2020,
        "make": "Toyota",
        "model": "Camry",
        "trim": "LE",
        "adas_map_contract_version": 1,
        "adas_map_state": map_state,
        "adas_map_inspection_id": inspection,
        "adas_map_source_url": "https://adas-map.invalid/exact-inspection",
        "adas_map_requirements_json": json.dumps(requirements),
        "adas_map_requirements_proven": 1,
        "adas_map_raw_result_json": json.dumps(raw_result),
        "ciq_reconciliation_state": ciq_state,
        "ciq_reconciliation_json": json.dumps(reconciliation),
        "adas_map_checked_at": "2026-01-01T00:00:00+00:00",
    }


def _catalog(tmp_path: Path, *, rows: list[dict] | None = None, texts=None):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    scrapex = tmp_path / "scrapex.sqlite3"
    if rows is not None:
        _create_scrapex(scrapex, rows)
    return (
        TextCatalog(root, tmp_path / "index.sqlite", scrapex, texts=texts),
        root,
        scrapex,
    )


def test_canonical_v1_provenance_wins_and_legacy_strings_are_ignored(tmp_path: Path):
    canonical = _canonical_row()
    legacy = {
        **canonical,
        "id": "legacy-poisoned",
        "adas_map_contract_version": 0,
        "adas_map_state": "complete",
        "adas_map_requirements_json": json.dumps(
            ["Create Calibration", "Required", "Seat Belt"]
        ),
        "adas_map_requirements_proven": 0,
        "ciq_reconciliation_state": "pending",
    }
    catalog, _root, _scrapex = _catalog(tmp_path, rows=[legacy, canonical])

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["record"]["inspection_id"] == "5949644"
    assert [row["label"] for row in result["record"]["requirements"]] == [
        "Occupant Classification System"
    ]
    assert result["record"]["sources"] == [
        {
            "kind": "scrapex_canonical_v1",
            "item_id": "canonical-1",
            "inspection_id": "5949644",
            "source_url": "https://adas-map.invalid/exact-inspection",
            "adas_map_state": "adas_map_complete",
            "requirements_proven": True,
            "ciq_reconciliation_state": "complete",
            "ciq_reconciliation_verified": True,
            "ciq_receipt_count": 1,
        }
    ]


def test_inspection_id_is_an_exact_discovery_and_coverage_identity(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(
        tmp_path,
        rows=[_canonical_row()],
        texts=texts,
    )
    _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text(
            "2400911695",
            requirements=("Occupant Classification System",),
            oe_requirements=("Seat weight sensor",),
        ),
        texts,
    )

    discovery = catalog.discover(inspection_id="5949644")
    coverage = catalog.requirement_coverage(
        ["Occupant Classification System"], inspection_id="5949644"
    )

    assert discovery["status"] == DISCOVERY_VERIFIED
    assert discovery["record"]["ro_number"] == "2400911695"
    assert coverage["status"] == COVERED
    assert coverage["requirements"][0]["state"] == COVERED


def test_physical_only_noncanonical_report_resolves_by_indexed_inspection_id(
    tmp_path: Path,
):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    _pdf(
        root,
        "Historical/Estimate Analysis.pdf",
        _report_text(
            "2400911695",
            requirements=("Occupant Classification System",),
            oe_requirements=("Seat weight sensor",),
        ),
        texts,
    )

    discovery = catalog.discover(inspection_id="5949644")
    coverage = catalog.requirement_coverage(["OCS"], inspection_id="5949644")

    assert discovery["status"] == DISCOVERY_VERIFIED
    assert discovery["record"]["ro_number"] == "2400911695"
    assert discovery["record"]["requirements"][0]["family"] == "occupant_classification"
    assert coverage["status"] == COVERED


def test_byte_identical_exact_ro_reports_are_deduplicated_by_hash(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    report_text = _report_text("2400911695")
    original = _pdf(root, "2400911695 adas map.pdf", report_text, texts)
    duplicate = root / "Duplicates" / "Estimate Analysis.pdf"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(original.read_bytes())
    texts[duplicate.name] = report_text

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["match_count"] == 1
    assert result["index"]["physical_pdf_count"] == 2


def test_canonical_map_proof_is_verified_while_ciq_reconciliation_is_pending(
    tmp_path: Path,
):
    row = _canonical_row(
        map_state="needs_operator",
        ciq_state="needs_operator",
        ciq_verified=False,
    )
    catalog, _root, _scrapex = _catalog(tmp_path, rows=[row])

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["record"]["explicit_no_calibration"] is False
    source = result["record"]["sources"][0]
    assert source["adas_map_state"] == "needs_operator"
    assert source["ciq_reconciliation_state"] == "needs_operator"
    assert source["ciq_reconciliation_verified"] is False


def test_canonical_map_fails_closed_when_raw_modal_proof_is_missing(tmp_path: Path):
    row = _canonical_row(raw_overrides={"modal_inspection_confirmed": False})
    catalog, _root, _scrapex = _catalog(tmp_path, rows=[row])

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_UNVERIFIED
    assert "map_structure_not_proven" in result["record"]["errors"]


def test_canonical_explicit_no_calibration_is_public_and_verified(tmp_path: Path):
    row = _canonical_row(requirement=None, explicit_no_calibration=True)
    catalog, _root, _scrapex = _catalog(tmp_path, rows=[row])

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["record"]["requirements"] == []
    assert result["record"]["explicit_no_calibration"] is True


def test_discovery_reconciles_a_stale_unindexed_physical_report(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    report = _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text("2400911695"),
        texts,
    )

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["index"]["added"] == 1
    assert result["record"]["vin"] == VIN
    assert result["record"]["vehicle"] == {
        "year": 2020,
        "make": "Toyota",
        "model": "Camry",
        "trim": None,
        "configuration": None,
    }
    with sqlite3.connect(catalog.cache_path) as db:
        row = db.execute(
            "SELECT sha256,artifact_kind,ro_number FROM artifact_catalog"
        ).fetchone()
    assert row == (
        hashlib.sha256(report.read_bytes()).hexdigest(),
        "adas_map_report",
        "2400911695",
    )


def test_exact_ro_physical_fallback_can_prove_no_calibration(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text("2400911695", requirements=("No calibration required",)),
        texts,
    )

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_VERIFIED
    assert result["record"]["requirements"] == []
    assert result["record"]["explicit_no_calibration"] is True


def test_exact_ro_physical_fallback_fails_ambiguous_for_two_identities(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    _pdf(
        root,
        "one/2400911695 adas map.pdf",
        _report_text("2400911695", vin=VIN, model="Camry"),
        texts,
    )
    _pdf(
        root,
        "two/2400911695 adas map.pdf",
        _report_text("2400911695", vin="5TDZK3DC3GS724909", model="Sienna"),
        texts,
    )

    result = catalog.discover(ro_number="2400911695")

    assert result["status"] == DISCOVERY_AMBIGUOUS
    assert result["match_count"] == 2


def test_bounded_ocs_and_front_camera_aliases_cover_exact_vehicle(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    _pdf(
        root,
        "2020 Toyota Camry Occupant Classification.pdf",
        "Toyota OEM service information. Seat weight sensor initialization procedure.",
        texts,
    )
    _pdf(
        root,
        "2020 Toyota Camry Windshield Camera.pdf",
        "Toyota OEM service information. IPMA mono camera calibration procedure.",
        texts,
    )

    result = catalog.requirement_coverage(
        ["OCS", "Front Camera"],
        year=2020,
        make="Toyota",
        model="Camry",
    )

    assert result["status"] == COVERED
    assert [row["family"] for row in result["requirements"]] == [
        "occupant_classification",
        "front_camera",
    ]
    assert {row["state"] for row in result["requirements"]} == {COVERED}


def test_map_report_presence_alone_is_not_service_information_coverage(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(
        tmp_path,
        rows=[_canonical_row()],
        texts=texts,
    )
    _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text(
            "2400911695",
            model="Camry LE configuration text",
            requirements=("Occupant Classification System",),
        ),
        texts,
    )

    result = catalog.requirement_coverage(
        ["Occupant Classification System"], ro_number="2400911695"
    )

    assert result["status"] == MISSING
    assert result["requirements"][0]["state"] == MISSING


def test_verified_map_with_incomplete_physical_scan_is_unverified_not_missing(
    tmp_path: Path,
    monkeypatch,
):
    catalog, _root, _scrapex = _catalog(tmp_path, rows=[_canonical_row()])
    monkeypatch.setattr(
        catalog,
        "reconcile_index",
        lambda: {
            "status": "partial_success",
            "scan_complete": False,
            "errors": [{"path": "unreadable.pdf", "error": "read failed"}],
        },
    )
    monkeypatch.setattr(catalog, "_artifacts", lambda: [])

    result = catalog.requirement_coverage(
        ["Occupant Classification System"], ro_number="2400911695"
    )

    assert result["status"] == UNVERIFIED
    assert result["discovery_status"] == DISCOVERY_VERIFIED
    assert result["requirements"][0]["state"] == UNVERIFIED
    assert "scan was incomplete" in result["reason"]


def test_map_report_exact_oe_section_can_support_coverage(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text(
            "2400911695",
            requirements=("Occupant Classification System",),
            oe_requirements=("Seat weight sensor",),
        ),
        texts,
    )

    result = catalog.requirement_coverage(["OCS"], ro_number="2400911695")

    assert result["status"] == COVERED
    assert result["requirements"][0]["sources"][0]["artifact_kind"] == "adas_map_report"


def test_same_model_artifact_with_conflicting_vin_cannot_supply_coverage(
    tmp_path: Path,
):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(
        tmp_path,
        rows=[_canonical_row()],
        texts=texts,
    )
    path = _pdf(
        root,
        "2020 Toyota Camry OCS.pdf",
        "Toyota OEM service information. Seat weight sensor initialization procedure.",
        texts,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.stem}.source.json").write_text(
        json.dumps(
            {
                "provider": "Public OEM",
                "artifact_kind": "service_information",
                "saved_pdf_sha256": digest,
                "vin": "5TDZK3DC3GS724909",
                "vehicle": {
                    "year": 2020,
                    "make": "Toyota",
                    "model": "Camry",
                    "trim": "XSE",
                    "configuration": "AWD",
                },
            }
        ),
        encoding="utf-8",
    )

    result = catalog.requirement_coverage(
        ["Occupant Classification System"],
        ro_number="2400911695",
        ciq_ro_id="ciq-ro-1",
        vin=VIN,
    )

    assert result["status"] == MISSING
    assert result["requirements"][0]["state"] == MISSING


def test_same_ro_and_vin_with_conflicting_trim_is_unverified(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(
        tmp_path,
        rows=[_canonical_row()],
        texts=texts,
    )
    path = _pdf(
        root,
        "2400911695 adas map.pdf",
        _report_text("2400911695"),
        texts,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.stem}.source.json").write_text(
        json.dumps(
            {
                "provider": "ADAS Map",
                "artifact_kind": "adas_map_report",
                "saved_pdf_sha256": digest,
                "ro_number": "2400911695",
                "ciq_ro_id": "ciq-ro-1",
                "vin": VIN,
                "vehicle": {
                    "year": 2020,
                    "make": "Toyota",
                    "model": "Camry",
                    "trim": "XSE",
                },
            }
        ),
        encoding="utf-8",
    )

    result = catalog.discover(
        ro_number="2400911695",
        ciq_ro_id="ciq-ro-1",
        vin=VIN,
    )

    assert result["status"] == DISCOVERY_AMBIGUOUS


def test_unreadable_exact_identity_artifact_is_unverified(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    path = root / "2020 Toyota Camry Front Camera.pdf"
    path.write_bytes(b"%PDF-unreadable-audit")

    discovery = catalog.discover(year=2020, make="Toyota", model="Camry")
    coverage = catalog.requirement_coverage(
        ["Front Camera"], year=2020, make="Toyota", model="Camry"
    )

    assert discovery["status"] == DISCOVERY_UNVERIFIED
    assert coverage["status"] == UNVERIFIED


def test_complete_negative_scan_is_missing_not_unverified(tmp_path: Path):
    catalog, _root, _scrapex = _catalog(tmp_path)

    result = catalog.requirement_coverage(
        ["Front Camera"], year=2020, make="Toyota", model="Camry"
    )

    assert result["status"] == MISSING
    assert result["requirements"][0]["state"] == MISSING


def test_hash_matching_sidecar_supplies_verified_physical_provenance(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    path = _pdf(
        root,
        "Acquired/reference.pdf",
        "OEM service information. IPMA front camera calibration procedure.",
        texts,
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.stem}.source.json").write_text(
        json.dumps(
            {
                "provider": "Public OEM",
                "artifact_kind": "service_information",
                "saved_pdf_sha256": digest,
                "source_url": "https://oem.invalid/procedure",
                "vehicle": {"year": 2020, "make": "Toyota", "model": "Camry"},
            }
        ),
        encoding="utf-8",
    )

    result = catalog.discover(year=2020, make="Toyota", model="Camry")

    assert result["status"] == DISCOVERY_VERIFIED
    source = result["record"]["sources"][0]
    assert source["sidecar_present"] is True
    assert source["sidecar_verified"] is True
    assert source["provider"] == "Public OEM"


def test_new_sidecar_reindexes_an_unchanged_pdf(tmp_path: Path):
    texts: dict[str, str] = {}
    catalog, root, _scrapex = _catalog(tmp_path, texts=texts)
    path = _pdf(
        root,
        "Acquired/reference.pdf",
        "OEM service information. IPMA front camera calibration procedure.",
        texts,
    )
    first = catalog.reconcile_index()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(f"{path.stem}.source.json").write_text(
        json.dumps(
            {
                "provider": "Public OEM",
                "artifact_kind": "service_information",
                "saved_pdf_sha256": digest,
                "source_url": "https://oem.invalid/procedure",
                "vehicle": {"year": 2020, "make": "Toyota", "model": "Camry"},
            }
        ),
        encoding="utf-8",
    )

    result = catalog.discover(year=2020, make="Toyota", model="Camry")

    assert first["added"] == 1
    assert result["index"]["updated"] == 1
    assert result["status"] == DISCOVERY_VERIFIED
    assert result["record"]["sources"][0]["sidecar_verified"] is True
