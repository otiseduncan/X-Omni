from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import research_alldata_quick_reference as quick
from core.tools import registry as registry_mod


def _pdf_bytes(seed: bytes = b"A") -> bytes:
    return b"%PDF-1.4\n" + seed * 2200


def test_canonical_alldata_url_drops_query_but_keeps_spa_article_route():
    value = quick._canonical_alldata_url(
        "https://my.alldata.com/repair/?tracking=1#/article/65779/guid/abc/"
    )
    assert value == "https://my.alldata.com/repair/#/article/65779/guid/abc"
    assert quick._article_id(value) == "65779"


def test_bounded_vehicle_identity_normalizes_model_punctuation():
    assert quick._identity_matches_text(
        "2018 Ford Truck F350 4WD V8-6.7L Diesel",
        {"year": "2018", "make": "Ford", "model_trim": "F-350"},
    ) is True


def test_bounded_vehicle_identity_accepts_known_alldata_make_aliases():
    assert quick._identity_matches_text(
        "2018 Chevy Truck Tahoe 4WD V8-5.3L",
        {"year": "2018", "make": "Chevrolet", "model_trim": "Tahoe"},
    ) is True
    assert quick._identity_matches_text(
        "2021 Nissan-Datsun Versa Sedan L4-1.6L",
        {"year": "2021", "make": "Nissan", "model_trim": "Versa"},
    ) is True


def test_bounded_vehicle_identity_still_rejects_wrong_model():
    assert quick._identity_matches_text(
        "2018 Ford Truck F150 4WD",
        {"year": "2018", "make": "Ford", "model_trim": "F-350"},
    ) is False


def test_quick_reference_link_filter_accepts_procedure_and_rejects_navigation():
    procedure = "https://my.alldata.com/repair/#/article/65779/guid/abc"
    assert quick._link_score("Forward Facing Camera Calibration", procedure) >= 3
    assert quick._link_score("Home", "https://my.alldata.com/repair/#/home") == 0
    assert quick._link_score("Help & Feedback", "https://my.alldata.com/repair/#/help") == 0
    assert quick._link_score("Camera Calibration", "https://example.com/article/1") == 0


def test_inter_document_delay_is_deliberately_low_rate():
    assert quick.MIN_INTER_DOCUMENT_DELAY_SECONDS >= 1.25
    assert quick.MAX_QUICK_REFERENCE_LINKS <= 40


def test_dedupe_index_covers_sidecar_url_and_whole_library_hash(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    acquired = root / "Acquired" / "ALLDATA" / "Ford" / "2018 Ford F-350" / "ADAS Quick Reference"
    acquired.mkdir(parents=True)
    existing = acquired / "2018 Ford F-350 Camera Calibration article-123.pdf"
    existing.write_bytes(_pdf_bytes(b"Z"))
    sidecar = acquired / "2018 Ford F-350 Camera Calibration article-123.source.json"
    canonical = "https://my.alldata.com/repair/#/article/123"
    sidecar.write_text(
        json.dumps(
            {
                "provider": "ALLDATA",
                "source_url": canonical,
                "canonical_source_url": canonical,
                "vehicle": "2018 Ford F-350",
                "title": "Camera Calibration",
                "saved_pdf_sha256": hashlib.sha256(existing.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    # A legacy PDF elsewhere in ADAS SI has no sidecar; content hash must still
    # keep it from being duplicated.
    legacy = root / "2018 Ford F-350 Radar Calibration.pdf"
    legacy.write_bytes(_pdf_bytes(b"R"))

    index = quick._load_dedupe_index(root, "2018 Ford F-350")
    assert canonical in index["urls"]
    assert hashlib.sha256(existing.read_bytes()).hexdigest() in index["hashes"]
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() in index["hashes"]


class _Body:
    def __init__(self, text: str):
        self.text = text

    async def inner_text(self, timeout=None):  # noqa: ARG002
        return self.text


class _Page:
    def __init__(self, url: str, pdf: bytes):
        self.url = url
        self._pdf = pdf
        self.goto_calls = []
        self.pdf_calls = 0

    async def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.url = url

    async def title(self):
        return "Camera Calibration - ALLDATA Collision"

    def locator(self, selector):
        assert selector == "body"
        return _Body("2018 Ford F-350 forward facing camera calibration procedure steps and prerequisites " * 5)

    async def pdf(self, **_kwargs):
        self.pdf_calls += 1
        return self._pdf


class _Inventory:
    def __init__(self):
        self._cache = None


class _Adas:
    def __init__(self, root: Path):
        self.source_root = root
        self.inventory = _Inventory()
        self.last_path = None

    def relative_of(self, path: Path):
        return str(path.relative_to(self.source_root)).replace("\\", "/")

    def _pages(self, path: Path):
        self.last_path = path
        return [(1, "2018 Ford F-350 camera calibration procedure readable OCR text")]

    def search(self, _args):
        assert self.last_path is not None
        return {
            "status": "success",
            "matched_documents": [{"relative_path": self.relative_of(self.last_path)}],
            "results": [],
        }


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_capture_skips_identical_hash_without_creating_second_file(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    pdf = _pdf_bytes(b"D")
    existing = root / "existing.pdf"
    existing.write_bytes(pdf)
    digest = hashlib.sha256(pdf).hexdigest()
    dedupe = {
        "urls": {},
        "hashes": {digest: {"sidecar": None, "pdf": existing, "data": {}}},
        "title_keys": {},
    }
    monkeypatch.setattr(
        quick,
        "_selected_vehicle_signal",
        lambda *_args, **_kwargs: _async_value({"verified": True, "label": "2018 Ford F-350"}),
    )
    adas = _Adas(root)
    page = _Page("https://my.alldata.com/repair/#/article/123", pdf)
    result = await quick._capture_one(
        page=page,
        adas=adas,
        vehicle={"year": "2018", "make": "Ford", "model_trim": "F-350", "label": "2018 Ford F-350"},
        link={"title": "Forward Facing Camera Calibration", "url": page.url},
        quick_reference_url="https://my.alldata.com/repair/#/quick-reference",
        dedupe=dedupe,
    )
    assert result["status"] == "duplicate_skipped"
    assert result["duplicate_reason"] == "identical_pdf_sha256"
    assert list((root / "Acquired").rglob("*.pdf")) == []


@pytest.mark.asyncio
async def test_known_canonical_article_is_skipped_before_navigation_or_render(tmp_path: Path):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    existing = root / "existing.pdf"
    existing.write_bytes(_pdf_bytes(b"O"))
    canonical = "https://my.alldata.com/repair/#/article/123"
    dedupe = {
        "urls": {canonical: {"sidecar": None, "pdf": existing, "data": {}}},
        "hashes": {},
        "title_keys": {},
    }
    page = _Page("https://my.alldata.com/repair/#/quick-reference", _pdf_bytes(b"N"))
    adas = _Adas(root)
    result = await quick._capture_one(
        page=page,
        adas=adas,
        vehicle={"year": "2018", "make": "Ford", "model_trim": "F-350", "label": "2018 Ford F-350"},
        link={"title": "Forward Facing Camera Calibration", "url": canonical},
        quick_reference_url=page.url,
        dedupe=dedupe,
    )
    assert result["status"] == "duplicate_skipped"
    assert result["duplicate_reason"] == "canonical_source_url_already_present"
    assert result["existing_relative_path"] == "existing.pdf"
    assert page.goto_calls == []
    assert page.pdf_calls == 0


@pytest.mark.asyncio
async def test_new_procedure_is_captured_ocr_readable_and_search_retrievable(tmp_path: Path, monkeypatch):
    root = tmp_path / "ADAS SI"
    root.mkdir()
    async def selected(*_args, **_kwargs):
        return {"verified": True, "label": "2018 Ford F-350", "source": "test"}
    monkeypatch.setattr(quick, "_selected_vehicle_signal", selected)

    page = _Page("https://my.alldata.com/repair/#/quick-reference", _pdf_bytes(b"N"))
    adas = _Adas(root)
    dedupe = {"urls": {}, "hashes": {}, "title_keys": {}}
    result = await quick._capture_one(
        page=page,
        adas=adas,
        vehicle={"year": "2018", "make": "Ford", "model_trim": "F-350", "label": "2018 Ford F-350"},
        link={
            "title": "Forward Facing Camera Calibration",
            "url": "https://my.alldata.com/repair/#/article/123",
        },
        quick_reference_url="https://my.alldata.com/repair/#/quick-reference",
        dedupe=dedupe,
    )
    assert result["status"] == "captured"
    assert result["retrieval_verified"] is True
    assert result["readable_pages"] == 1
    assert Path(root / result["relative_path"]).is_file()
    assert Path(root / result["source_sidecar"]).is_file()


@pytest.mark.asyncio
async def test_collector_stops_before_quick_reference_when_selected_vehicle_mismatches(
    tmp_path: Path, monkeypatch
):
    async def get_ro(_settings, _args):
        return {"status": "verified", "raw": {"vehicle": {}}, "repair_order": {"requirements": []}}

    monkeypatch.setattr(quick.calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(
        quick.calibration_iq,
        "_research_vehicle_label",
        lambda _snapshot: "2018 Ford F-350",
    )

    class Browser:
        def __init__(self):
            self._page = object()

        async def start(self, auto_login=False):  # noqa: ARG002
            return {"authenticated": True}

    browser = Browser()
    monkeypatch.setattr(quick.ro, "get_browser", lambda *_args, **_kwargs: browser)

    async def mismatch(_page, _vehicle):
        return {
            "verified": False,
            "label": "2024 Ford Maverick",
            "reason": "Select the exact CIQ vehicle before collection.",
        }

    monkeypatch.setattr(quick, "_selected_vehicle_signal", mismatch)
    opened = False

    async def should_not_open(_page):
        nonlocal opened
        opened = True
        return {"opened": True}

    monkeypatch.setattr(quick, "_open_quick_reference", should_not_open)
    settings = SimpleNamespace(root=tmp_path)
    adas = SimpleNamespace(source_root=tmp_path / "ADAS SI")

    result = await quick.collect_for_calibration_iq_ro(
        settings, adas, {"repair_order_id": "2400012345"}
    )
    assert result["status"] == "vehicle_selection_required"
    assert result["verified"] is False
    assert opened is False


def test_collision_research_schema_advertises_ciq_quick_reference_collector():
    schema = registry_mod.TOOL_SCHEMAS["collision_research"]
    props = schema["parameters"]["properties"]
    assert "collect_alldata_quick_reference" in props["action"]["enum"]
    assert "repair_order_id" in props
    assert "max_documents" in props
