"""
Tests for the ADAS SI and Calibration IQ field tools.

Focus is on the invariants that matter when these run in a real shop:
OEM sources are never written, a scanned PDF is not reported as "no such
procedure", writes are gated and version-checked, and the service token
never leaks into a result that reaches model context or the audit log.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from core.services import adas_si as adas_mod
from core.services import calibration_iq as ciq
from core.tools.registry import TOOL_SCHEMAS


# ==========================================================================
# fixtures
# ==========================================================================

@dataclass
class FakeSettings:
    calibration_iq_base_url: str
    calibration_iq_project_path: Path


@pytest.fixture
def ciq_project(tmp_path: Path) -> Path:
    project = tmp_path / "calibration iq"
    project.mkdir()
    (project / ".env").write_text(
        "# comment line\n"
        "OTHER_KEY=ignored\n"
        'TOOL_SERVICE_TOKEN="quoted-secret-token"\n',
        encoding="utf-8",
    )
    return project


@pytest.fixture
def settings(ciq_project: Path) -> FakeSettings:
    return FakeSettings(
        calibration_iq_base_url="http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        calibration_iq_project_path=ciq_project,
    )


@pytest.fixture
def adas_root(tmp_path: Path) -> Path:
    root = tmp_path / "ADAS SI"
    root.mkdir()
    # Real-shaped filenames: identity lives here, not in the content.
    for name in (
        "2021 Ford F-150 AWD Front Camera Calibration.pdf",
        "2020 Subaru Forester Front Camera Calibration.pdf",
        "2019 Honda CR-V Radar Alignment.pdf",
    ):
        (root / name).write_bytes(b"%PDF-1.4\n% not a real pdf\n")
    return root


@pytest.fixture
def adas(adas_root: Path, tmp_path: Path) -> adas_mod.AdasSI:
    return adas_mod.AdasSI(adas_root, tmp_path / "cache" / "index.sqlite")


# ==========================================================================
# Calibration IQ -- credentials
# ==========================================================================

def test_token_is_unquoted(ciq_project: Path):
    """XV12 left the quotes on, producing a malformed Authorization header."""
    assert ciq._service_token(ciq_project) == "quoted-secret-token"


def test_missing_env_returns_empty_token(tmp_path: Path):
    assert ciq._service_token(tmp_path / "nope") == ""


@pytest.mark.asyncio
async def test_read_without_token_is_not_configured(tmp_path: Path):
    s = FakeSettings("http://127.0.0.1:8084/x", tmp_path)
    result = await ciq.read_repair_orders(s, {})
    assert result["status"] == "not_configured"
    assert result["items"] == []


@pytest.mark.asyncio
async def test_token_never_appears_in_results(settings, monkeypatch):
    """The service token must not reach model context or the audit log."""
    async def fake_get(self, url, **kw):
        return httpx.Response(200, json={"items": [], "count": 0, "returned_count": 0},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {})
    assert "quoted-secret-token" not in json.dumps(result, default=str)


# ==========================================================================
# Calibration IQ -- read
# ==========================================================================

@pytest.mark.asyncio
async def test_read_clamps_limit_and_allowlists_params(settings, monkeypatch):
    seen = {}

    async def fake_get(self, url, params=None, headers=None, **kw):
        seen["params"] = params
        seen["auth"] = headers.get("Authorization")
        return httpx.Response(200, json={"items": [], "count": 0, "returned_count": 0},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await ciq.read_repair_orders(
        settings, {"limit": 5000, "q": "F-150", "evil": "drop table", "shop": ""}
    )
    assert seen["params"]["limit"] == 100          # clamped
    assert seen["params"]["q"] == "F-150"
    assert "evil" not in seen["params"]            # not on the allow-list
    assert "shop" not in seen["params"]            # empty string dropped
    assert seen["auth"] == "Bearer quoted-secret-token"


@pytest.mark.asyncio
async def test_upstream_status_cannot_shadow_our_sentinel(settings, monkeypatch):
    """XV12 spread the body over the sentinel, so an upstream 'status'
    silently replaced 'verified'."""
    async def fake_get(self, url, **kw):
        return httpx.Response(
            200,
            json={"items": [], "count": 0, "returned_count": 0, "status": "totally-bogus"},
            request=httpx.Request("GET", url),
        )
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {})
    assert result["status"] == "verified"


@pytest.mark.asyncio
async def test_truncation_is_reported_honestly(settings, monkeypatch):
    async def fake_get(self, url, params=None, **kw):
        return httpx.Response(
            200,
            json={"items": [{"ro_number": f"RO{i}", "status": "New Arrival"}
                            for i in range(50)],
                  "count": 50, "returned_count": 50},
            request=httpx.Request("GET", url),
        )
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {})
    assert result["count"] == 50          # every match counted...
    assert result["shown_count"] == 20    # ...but only a page displayed
    assert result["truncated"] is True
    assert result["result_scope"] == "board_list_only"
    assert result["exact_ro_detail_included"] is False
    assert result["next_capability_for_one_ro_detail"] == "calibration_iq_ro"


@pytest.mark.asyncio
async def test_completed_work_is_not_active(settings, monkeypatch):
    """'Active' has to mean active: Calibration Complete and No Calibration
    Required are finished work and must not inflate the numbers Otis hears."""
    items = [
        {"ro_number": "1", "status": "New Arrival"},
        {"ro_number": "2", "status": "Waiting On Prerequisites"},
        {"ro_number": "3", "status": "Calibration Complete"},
        {"ro_number": "4", "status": "No Calibration Required"},
        {"ro_number": "5", "status": "Repair In Progress"},
    ]

    async def fake_get(self, url, params=None, **kw):
        return httpx.Response(200, json={"items": items, "count": 5},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await ciq.read_repair_orders(settings, {})
    assert result["count"] == 3
    assert result["active_count"] == 3
    assert result["completed_count"] == 2
    shown = {r["RO"] for r in result["rows"]}
    assert "3" not in shown and "4" not in shown

    everything = await ciq.read_repair_orders(settings, {"include_completed": True})
    assert everything["count"] == 5

    terminal = await ciq.read_repair_orders(settings, {"terminal_only": True})
    assert terminal["count"] == 2
    assert terminal["include_completed"] is True
    assert terminal["terminal_only"] is True
    assert terminal["scope"] == "terminal work only"
    assert {row["RO"] for row in terminal["rows"]} == {"3", "4"}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Calibration Complete", True),
        ("  CALIBRATION   COMPLETE  ", True),
        ("No Calibration Required", True),
        ("Calibration Incomplete", False),
        ("Not Complete", False),
        ("Calibration Complete - Pending QA", False),
        ("Closed Loop Diagnostic", False),
        ("Delivered to calibration bay", False),
    ],
)
def test_terminal_status_is_exact_after_normalization(status, expected):
    assert ciq.is_terminal({"status": status}) is expected


@pytest.mark.asyncio
async def test_summary_returns_counts_without_rows(settings, monkeypatch):
    items = [
        {"ro_number": "1", "status": "New Arrival", "phase": 5, "shop": {"name": "Macon"}},
        {"ro_number": "2", "status": "New Arrival", "phase": 5, "shop": {"name": "Macon"}},
        {"ro_number": "3", "status": "Waiting On Prerequisites", "phase": 5,
         "shop": {"name": "Perry"}},
        {"ro_number": "4", "status": "Calibration Complete", "phase": 5,
         "shop": {"name": "Macon"}},
    ]

    async def fake_get(self, url, params=None, **kw):
        return httpx.Response(200, json={"items": items, "count": 4},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await ciq.summarize_repair_orders(settings, {"phase": "5"})
    assert result["summary_only"] is True
    assert result["rows"] == []
    assert "items" not in result
    assert result["count"] == 3
    assert result["breakdown"]["by_status"]["New Arrival"] == 2
    assert result["breakdown"]["by_shop"]["Macon"] == 2


@pytest.mark.asyncio
async def test_nested_phase_is_rendered_as_a_scalar(settings, monkeypatch):
    item = {
        "ro_number": "1",
        "status": "New Arrival",
        "phase": {"name": "Phase 5", "number": 5},
    }

    async def fake_get(self, url, params=None, **kw):
        return httpx.Response(200, json={"items": [item], "count": 1},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {})
    assert result["rows"][0]["Phase"] == "Phase 5"
    assert result["breakdown"]["by_phase"] == {"Phase 5": 1}


@pytest.mark.asyncio
async def test_collection_pages_in_one_call(settings, monkeypatch):
    """One question, one tool call: the service walks offsets itself."""
    calls = []

    board = [{"ro_number": str(i), "status": "New Arrival"} for i in range(150)]

    async def fake_get(self, url, params=None, **kw):
        calls.append(dict(params or {}))
        offset = int((params or {}).get("offset") or 0)
        limit = int((params or {}).get("limit") or 100)
        return httpx.Response(
            200,
            json={"items": board[offset:offset + limit], "count": len(board)},
            request=httpx.Request("GET", url),
        )
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await ciq.summarize_repair_orders(settings, {})
    assert result["count"] == 150
    assert len(calls) == 2                      # 100 + 50, no third trip
    assert all(c.get("limit") == 100 for c in calls)


@pytest.mark.asyncio
async def test_upstream_short_pages_continue_to_authoritative_total(settings, monkeypatch):
    """An upstream 20-row cap must not turn 59 matches into a 20-row total."""
    calls = []
    board = [{"id": i, "ro_number": str(i), "status": "New Arrival"}
             for i in range(59)]

    async def fake_get(self, url, params=None, **kw):
        offset = int((params or {}).get("offset") or 0)
        calls.append(offset)
        return httpx.Response(
            200,
            json={"items": board[offset:offset + 20], "count": 59},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {})
    assert calls == [0, 20, 40]
    assert result["status"] == "verified"
    assert result["count"] == 59
    assert result["shown_count"] == 20
    assert result["truncated"] is True
    assert result["collection"]["raw_fetched_count"] == 59


@pytest.mark.asyncio
async def test_duplicate_rows_across_pages_do_not_inflate_total(settings, monkeypatch):
    calls = []
    pages = {
        0: [{"id": "1", "status": "New Arrival"},
            {"id": "2", "status": "New Arrival"}],
        2: [{"id": "2", "status": "New Arrival"},
            {"id": "3", "status": "New Arrival"}],
        4: [{"id": "4", "status": "New Arrival"}],
    }

    async def fake_get(self, url, params=None, **kw):
        offset = int((params or {}).get("offset") or 0)
        calls.append(offset)
        return httpx.Response(
            200,
            json={"items": pages.get(offset, []), "count": 4},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.read_repair_orders(settings, {"limit": 20})
    assert calls == [0, 2, 4]
    assert result["count"] == 4
    assert result["duplicate_count"] == 1
    assert [row["id"] for row in result["rows"]] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_collection_cap_never_reports_partial_count_as_verified(
    settings, monkeypatch,
):
    monkeypatch.setattr(ciq, "MAX_COLLECT", 3)
    board = [{"id": str(i), "status": "New Arrival"} for i in range(4)]

    async def fake_get(self, url, params=None, **kw):
        offset = int((params or {}).get("offset") or 0)
        limit = int((params or {}).get("limit") or 100)
        return httpx.Response(
            200,
            json={"items": board[offset:offset + limit], "count": 4},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.summarize_repair_orders(settings, {})
    assert result["status"] == "incomplete"
    assert result["count"] is None
    assert result["partial_count"] == 3
    assert result["collection_complete"] is False
    assert result["collection_capped"] is True


@pytest.mark.asyncio
async def test_early_empty_page_is_incomplete_not_a_false_total(settings, monkeypatch):
    calls = 0

    async def fake_get(self, url, params=None, **kw):
        nonlocal calls
        calls += 1
        items = ([{"id": "1", "status": "New Arrival"},
                  {"id": "2", "status": "New Arrival"}]
                 if calls == 1 else [])
        return httpx.Response(200, json={"items": items, "count": 3},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.summarize_repair_orders(settings, {})
    assert result["status"] == "incomplete"
    assert result["count"] is None
    assert result["partial_count"] == 2
    assert result["collection"]["completion_reason"] == "early_empty_page"


@pytest.mark.asyncio
async def test_empty_collection_is_a_verified_zero(settings, monkeypatch):
    async def fake_get(self, url, params=None, **kw):
        return httpx.Response(200, json={"items": [], "count": 0},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.summarize_repair_orders(settings, {})
    assert result["status"] == "verified"
    assert result["count"] == 0
    assert result["breakdown"] == {"by_status": {}, "by_phase": {}, "by_shop": {}}


@pytest.mark.asyncio
async def test_later_page_error_discards_partial_total_claim(settings, monkeypatch):
    calls = 0

    async def fake_get(self, url, params=None, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            items = [{"id": str(i), "status": "New Arrival"} for i in range(100)]
            return httpx.Response(200, json={"items": items, "count": 150},
                                  request=httpx.Request("GET", url))
        return httpx.Response(503, json={"detail": "unavailable"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    result = await ciq.summarize_repair_orders(settings, {})
    assert result["status"] == "error"
    assert result["count"] is None
    assert result["collection_complete"] is False
    assert result["collection"]["unique_count"] == 100


def test_calibration_iq_tool_schema_compatibility():
    summary = TOOL_SCHEMAS["calibration_iq_summary"]["parameters"]["properties"]
    listing = TOOL_SCHEMAS["calibration_iq_read"]["parameters"]["properties"]
    assert set(summary) == {
        "shop", "phase", "status", "insurance", "q", "include_completed",
        "terminal_only",
    }
    assert set(listing) == {
        "q", "shop", "insurance", "status", "phase", "limit", "include_completed",
        "terminal_only",
    }
    assert summary["include_completed"]["type"] == "boolean"
    assert summary["terminal_only"]["type"] == "boolean"
    assert listing["limit"]["type"] == "integer"


@pytest.mark.asyncio
async def test_offline_is_reported_not_raised(settings, monkeypatch):
    async def boom(self, url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    result = await ciq.read_repair_orders(settings, {})
    assert result["status"] == "offline"
    assert result["items"] == []


# ==========================================================================
# Calibration IQ -- write
# ==========================================================================

@pytest.mark.asyncio
async def test_unknown_operation_never_reaches_the_wire(settings, monkeypatch):
    async def fail(self, *a, **kw):
        raise AssertionError("must not send an unknown operation upstream")
    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    with pytest.raises(ValueError, match="Unsupported operation"):
        await ciq.mutate(settings, {"repair_order_id": "1", "operation": "drop_database",
                                    "arguments": {}})


@pytest.mark.asyncio
async def test_mutation_always_sends_an_idempotency_key(settings, monkeypatch):
    """The replay guard must hold even when the caller supplies no key."""
    seen = {}

    async def fake_post(self, url, json=None, headers=None, **kw):
        seen["key"] = headers.get("Idempotency-Key")
        seen["body"] = json
        return httpx.Response(200, json={"success": True, "receipt": {"status": "completed"}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    await ciq.mutate(settings, {"repair_order_id": "RO-9", "operation": "change_status",
                                "arguments": {"status": "done"}, "expected_version": 3})
    assert len(seen["key"]) >= 16
    assert seen["body"]["expected_version"] == 3
    assert seen["body"]["operation"] == "change_status"


@pytest.mark.asyncio
async def test_short_idempotency_key_rejected(settings):
    with pytest.raises(ValueError, match="at least 16"):
        await ciq.mutate(settings, {"repair_order_id": "1", "operation": "update_ro",
                                    "arguments": {}, "idempotency_key": "tooshort"})


@pytest.mark.asyncio
async def test_unconfirmed_mutation_is_not_called_success(settings, monkeypatch):
    """Never tell a tech the RO was updated when the service didn't confirm."""
    async def fake_post(self, url, **kw):
        return httpx.Response(200, json={"success": True, "receipt": {"status": "pending"}},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await ciq.mutate(settings, {"repair_order_id": "1", "operation": "update_ro",
                                         "arguments": {"note": "x"}})
    assert result["status"] == "partial_success"
    assert result["receipt"]["verified"] is False


@pytest.mark.asyncio
async def test_version_conflict_surfaces_as_conflict(settings, monkeypatch):
    async def fake_post(self, url, **kw):
        return httpx.Response(409, json={"detail": "version mismatch"},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await ciq.mutate(settings, {"repair_order_id": "1", "operation": "update_ro",
                                         "arguments": {}, "expected_version": 1})
    assert result["status"] == "conflict"
    assert result["executed"] is False


@pytest.mark.asyncio
async def test_offline_mutation_reports_nothing_changed(settings, monkeypatch):
    async def boom(self, *a, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    result = await ciq.mutate(settings, {"repair_order_id": "1", "operation": "update_ro",
                                         "arguments": {}})
    assert result["executed"] is False
    assert "Nothing was changed" in result["message"]


# ==========================================================================
# ADAS SI -- identity parsing
# ==========================================================================

def test_filename_identity_parsing(adas_root: Path):
    d = adas_mod.describe_document(
        adas_root, adas_root / "2021 Ford F-150 AWD Front Camera Calibration.pdf"
    )
    assert d["year"] == 2021
    assert d["make"] == "Ford"
    assert d["model"] == "F-150"
    assert d["drivetrain"] == "AWD"
    assert d["topic"] == "Front Camera"
    assert d["application_parsed"] is True
    assert d["parse_confidence"] == "high"


def test_identity_beats_content_in_ranking(adas: adas_mod.AdasSI):
    """Two docs share 'front camera calibration'; the queried vehicle must win."""
    matches = adas.inventory.matching_documents("2020 Subaru Forester front camera calibration")
    assert matches[0]["descriptor"]["make"] == "Subaru"


def test_inventory_groups_vehicle_applications(adas: adas_mod.AdasSI):
    snap = adas.inventory_read()
    assert snap["status"] == "success"
    assert snap["summary"]["document_count"] == 3
    makes = {a["make"] for a in snap["applications"]}
    assert {"Ford", "Subaru", "Honda"} <= makes


def test_missing_library_is_unavailable_not_crash(tmp_path: Path):
    a = adas_mod.AdasSI(tmp_path / "gone", tmp_path / "c.sqlite")
    assert a.available() is False
    assert a.inventory_read()["status"] == "unavailable"
    assert a.search({"query": "anything"})["status"] == "unavailable"


# ==========================================================================
# ADAS SI -- the honesty invariant
# ==========================================================================

def test_unreadable_pdf_is_not_reported_as_no_result(adas: adas_mod.AdasSI):
    """The fixtures are not real PDFs, so extraction fails. A strong filename
    match must still report the document -- telling a tech the procedure
    doesn't exist when it does is the worst failure mode here."""
    result = adas.search({"query": "2021 Ford F-150 AWD Front Camera Calibration"})
    assert result["exact_source_matched"] is True
    assert result["status"] == "partial_success"
    assert "do not treat this as the procedure being absent" in result["message"]
    assert result["matched_documents"][0]["make"] == "Ford"


def test_page_text_extraction_falls_back_to_pdfium_and_caches_result(
    tmp_path: Path, monkeypatch,
):
    source_root = tmp_path / "ADAS SI"
    source_root.mkdir()
    source = source_root / "2024 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF fixture")
    lifecycle: list[str] = []

    class BrokenPdfReader:
        def __init__(self, *_args, **_kwargs):
            raise ValueError("pypdf could not parse this valid PDFium document")

    class FakeTextPage:
        def __init__(self, text: str):
            self.text = text

        def get_text_range(self):
            return self.text

        def close(self):
            lifecycle.append("text_closed")

    class FakePage:
        def __init__(self, text: str):
            self.text = text

        def get_textpage(self):
            return FakeTextPage(self.text)

        def close(self):
            lifecycle.append("page_closed")

    class FakeDocument:
        texts = [
            "Forward recognition camera overview",
            "Perform the front camera calibration with the OEM target.",
        ]

        def __init__(self, path: str):
            assert path == str(source)
            lifecycle.append("document_opened")

        def __len__(self):
            return len(self.texts)

        def __getitem__(self, index: int):
            return FakePage(self.texts[index])

        def close(self):
            lifecycle.append("document_closed")

    class FakePdfium:
        PdfDocument = FakeDocument

    monkeypatch.setattr(adas_mod, "PdfReader", BrokenPdfReader)
    monkeypatch.setattr(adas_mod, "pdfium", FakePdfium)
    adas = adas_mod.AdasSI(source_root, tmp_path / "cache" / "index.sqlite")

    expected = [(1, FakeDocument.texts[0]), (2, FakeDocument.texts[1])]
    assert adas._pages(source) == expected
    assert lifecycle == [
        "document_opened",
        "text_closed", "page_closed",
        "text_closed", "page_closed",
        "document_closed",
    ]
    assert adas._pages(source) == expected
    assert lifecycle.count("document_opened") == 1


def test_page_cache_schema_version_change_invalidates_old_extracted_text(tmp_path: Path):
    source_root = tmp_path / "ADAS SI"
    source_root.mkdir()
    source = source_root / "2024 Toyota Camry Front Camera Calibration.pdf"
    source.write_bytes(b"%PDF fixture")
    cache_path = tmp_path / "cache" / "index.sqlite"
    cache_path.parent.mkdir(parents=True)
    with sqlite3.connect(cache_path) as db:
        db.executescript(
            "CREATE TABLE pages("
            "path TEXT, page INTEGER, text TEXT, source_mtime_ns INTEGER,"
            "PRIMARY KEY(path, page));"
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        )
        db.execute(
            "INSERT INTO pages(path, page, text, source_mtime_ns) VALUES(?,?,?,?)",
            (str(source), 1, "stale extractor output", source.stat().st_mtime_ns),
        )
        db.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )

    adas_mod.AdasSI(source_root, cache_path)

    with sqlite3.connect(cache_path) as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        version = db.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == adas_mod.CACHE_SCHEMA_VERSION


def test_search_requires_a_query(adas: adas_mod.AdasSI):
    with pytest.raises(ValueError, match="query is required"):
        adas.search({"query": "   "})


# ==========================================================================
# ADAS SI -- managed records never touch originals
# ==========================================================================

def test_record_write_creates_only_in_managed_dir(adas: adas_mod.AdasSI, adas_root: Path):
    before = {p.name: p.read_bytes() for p in adas_root.glob("*.pdf")}
    out = adas.record_write({"record_id": "f150-notes", "title": "Notes",
                             "content": "Target 1.2m from bumper."})
    assert out["status"] == "success"
    assert out["receipt"]["originals_modified"] is False
    written = Path(out["receipt"]["path"])
    assert written.parent.name == adas_mod.MANAGED_DIRNAME
    # every OEM source byte-identical
    after = {p.name: p.read_bytes() for p in adas_root.glob("*.pdf")}
    assert before == after


def test_record_id_is_sanitised(adas: adas_mod.AdasSI):
    out = adas.record_write({"record_id": "../../escape!", "content": "x"})
    path = Path(out["receipt"]["path"])
    assert path.parent.name == adas_mod.MANAGED_DIRNAME
    assert ".." not in path.name


def test_duplicate_record_write_refused(adas: adas_mod.AdasSI):
    adas.record_write({"record_id": "dup", "content": "first"})
    with pytest.raises(ValueError, match="already exists"):
        adas.record_write({"record_id": "dup", "content": "second"})


def test_modify_requires_matching_version(adas: adas_mod.AdasSI):
    adas.record_write({"record_id": "vt", "content": "v1"})
    with pytest.raises(ValueError, match="Version conflict"):
        adas.record_modify({"record_id": "vt", "expected_version": 99, "content": "v2"})
    ok = adas.record_modify({"record_id": "vt", "expected_version": 1, "content": "v2"})
    assert ok["record"]["version"] == 2
    assert ok["receipt"]["originals_modified"] is False


def test_modify_missing_record_is_no_result(adas: adas_mod.AdasSI):
    out = adas.record_modify({"record_id": "ghost", "expected_version": 1})
    assert out["status"] == "no_result"
    assert out["executed"] is False
