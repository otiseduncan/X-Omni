from __future__ import annotations

import sqlite3
from pathlib import Path

from core.services import adas_ocr as ocr


class FakeAdasSI:
    def __init__(self, root: Path):
        self.source_root = root
        self.cache_path = root / "cache" / "index.sqlite"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        self.source = root / "2024 Toyota Camry Front Camera Calibration.pdf"
        self.source.write_bytes(b"%PDF fake")
        self.render_calls: list[int] = []

    def _pages(self, _path: Path):
        return [
            (1, ""),
            (2, "Perform the front camera calibration using the specified OEM target."),
        ]

    def render_page(self, _path: Path, page: int, width: int = 1100) -> bytes:
        self.render_calls.append(page)
        return b"fake-png"

    def resolve_relative(self, _relative: str) -> Path:
        return self.source

    def search(self, _args: dict):
        pages = self._pages(self.source)
        text = pages[0][1]
        if text:
            return {
                "status": "success",
                "results": [
                    {
                        "relative_path": self.source.name,
                        "page": 1,
                        "excerpt": text,
                    }
                ],
                "message": None,
            }
        return {
            "status": "partial_success",
            "results": [],
            "message": "there is no OCR",
        }

    def open_document(self, _args: dict):
        return {
            "status": "success",
            "document": {
                "relative_path": self.source.name,
                "page": 1,
                "url": "/api/adas-si/document?path=x",
            },
        }


ocr.install_class(FakeAdasSI)


def _good_ocr(_png: bytes):
    return {
        "text": (
            "Toyota front camera calibration procedure\n"
            "Place the OEM target at the specified distance."
        ),
        "confidence": 0.96,
        "line_count": 2,
        "rotation": 0,
        "engine": "rapidocr-onnxruntime",
        "engine_version": "test",
        "pipeline_version": ocr.OCR_PIPELINE_VERSION,
    }


def test_scan_only_page_is_ocrd_while_good_native_page_is_preserved(tmp_path, monkeypatch):
    calls = []

    def fake_ocr(png: bytes):
        calls.append(png)
        return _good_ocr(png)

    monkeypatch.setattr(ocr, "_ocr_png", fake_ocr)
    adas = FakeAdasSI(tmp_path)

    pages = adas._pages(adas.source)

    assert "Toyota front camera calibration procedure" in pages[0][1]
    assert pages[1][1] == "Perform the front camera calibration using the specified OEM target."
    assert adas.render_calls == [1]
    assert len(calls) == 1


def test_ocr_page_cache_prevents_duplicate_ocr_work(tmp_path, monkeypatch):
    calls = 0

    def fake_ocr(png: bytes):
        nonlocal calls
        calls += 1
        return _good_ocr(png)

    monkeypatch.setattr(ocr, "_ocr_png", fake_ocr)
    adas = FakeAdasSI(tmp_path)

    first = adas._pages(adas.source)
    second = adas._pages(adas.source)

    assert first == second
    assert calls == 1
    metadata = adas.page_text_metadata(adas.source, 1)
    assert metadata["method"] == "ocr"
    assert metadata["source_is_original_pdf"] is True


def test_existing_search_transparently_receives_ocr_text_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_ocr_png", _good_ocr)
    adas = FakeAdasSI(tmp_path)

    result = adas.search({"query": "Toyota camera calibration"})

    assert result["status"] == "success"
    assert "Toyota front camera calibration" in result["results"][0]["excerpt"]
    assert result["results"][0]["text_extraction"]["method"] == "ocr"
    assert result["results"][0]["text_extraction"]["source_is_original_pdf"] is True
    assert "ocr" in result


def test_open_document_gives_x_page_text_while_preserving_original_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_ocr_png", _good_ocr)
    adas = FakeAdasSI(tmp_path)

    result = adas.open_document({"document": "Camry camera", "page": 1})
    document = result["document"]

    assert result["status"] == "success"
    assert document["readable_by_x"] is True
    assert "OEM target" in document["page_text"]
    assert document["text_extraction"]["method"] == "ocr"
    assert document["text_extraction"]["source_is_original_pdf"] is True
    assert document["url"].startswith("/api/adas-si/document")


def test_failed_ocr_keeps_honest_partial_success_semantics(tmp_path, monkeypatch):
    def failed_ocr(_png: bytes):
        raise RuntimeError("OCR engine unavailable")

    monkeypatch.setattr(ocr, "_ocr_png", failed_ocr)
    adas = FakeAdasSI(tmp_path)

    result = adas.search({"query": "Toyota camera calibration"})

    assert result["status"] == "partial_success"
    assert "native extraction and local OCR did not produce usable page text" in result["message"]
    assert "do not treat this as the procedure being absent" in result["message"]


def test_layout_text_preserves_rows_and_columns():
    txts = ("Target", "1500 mm", "Height", "900 mm")
    boxes = [
        [[0, 0], [80, 0], [80, 20], [0, 20]],
        [[200, 1], [300, 1], [300, 21], [200, 21]],
        [[0, 50], [80, 50], [80, 70], [0, 70]],
        [[200, 51], [300, 51], [300, 71], [200, 71]],
    ]

    text = ocr._layout_text(txts, boxes)

    assert text.splitlines() == ["Target    1500 mm", "Height    900 mm"]
