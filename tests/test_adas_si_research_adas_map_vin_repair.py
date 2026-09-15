from types import SimpleNamespace

import pytest

from core.services import adas_si_research as research
from core.services import adas_si_research_adas_map_vin_repair as vin_repair
from core.services import calibration_iq, scrapex


RO = "2400911761"
VIN = "3TMCZ5AN0PM123456"
VIN_2 = "3TMCZ5AN0PM654321"


def _read(*, vin=None, adas_map=True):
    documents = []
    if adas_map:
        documents.append(
            {
                "id": "doc-map",
                "title": f"{RO} ADAS Map",
                "source_name": f"{RO} ADAS Map.pdf",
                "document_type": "adas_map_report",
                "semantic_type": "ADAS_MAP_REPORT",
            }
        )
    return {
        "status": "verified",
        "repair_order": {
            "id": "ro-1",
            "RO": RO,
            "vin": vin,
            "year": 2023,
            "make": "Toyota",
            "model": "Tacoma",
        },
        "vehicle": {
            "vin": vin,
            "year": 2023,
            "make": "Toyota",
            "model": "Tacoma",
        },
        "raw": {
            "repair_order": {
                "id": "ro-1",
                "ro_number": RO,
                "vin": vin,
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
            },
            "vehicle": {
                "vin": vin,
                "year": 2023,
                "make": "Toyota",
                "model": "Tacoma",
            },
            "research": {"documents": documents},
            "calibrations": [
                {
                    "id": "cal-camera",
                    "calibration_type": "Forward Recognition Camera",
                    "determination": "REQUIRED",
                }
            ],
        },
    }


def _operator_snapshot(*, vin=None, documents=None):
    return {
        "repair_order": {
            "id": "ro-1",
            "ro_number": RO,
            "version": 39,
            "vin": vin,
        },
        "vehicle": {"vin": vin},
        "research": {"documents": list(documents or [])},
    }


@pytest.mark.asyncio
async def test_missing_ciq_vin_is_repaired_from_proven_adas_map(monkeypatch):
    reads = [_read(vin=None), _read(vin=VIN)]
    calls = []

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {
            "requested_count": 1,
            "repaired_count": 1,
            "results": [{"ro_number": RO, "status": "repaired", "vin": VIN}],
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})

    assert research.vin_from_read(result) == VIN
    assert calls == [
        (
            "POST",
            "/api/adas-map/repair-proven-vins",
            {"ro_numbers": [RO]},
        )
    ]


@pytest.mark.asyncio
async def test_missing_vin_does_not_depend_on_ciq_document_projection(monkeypatch):
    """The proof service, not CIQ's current document JSON shape, owns eligibility."""
    reads = [_read(vin=None, adas_map=False), _read(vin=VIN, adas_map=False)]
    calls = []

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {
            "requested_count": 1,
            "repaired_count": 1,
            "results": [{"ro_number": RO, "status": "repaired", "vin": VIN}],
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})

    assert research.vin_from_read(result) == VIN
    assert calls == [
        (
            "POST",
            "/api/adas-map/repair-proven-vins",
            {"ro_numbers": [RO]},
        )
    ]


@pytest.mark.asyncio
async def test_attached_adas_map_repairs_vin_when_scrapex_history_is_gone(monkeypatch):
    """RO 2400911761 regression: the attached report itself is the fallback proof."""
    reads = [_read(vin=None), _read(vin=VIN)]
    operator_calls = []
    document = {
        "id": "doc-map",
        "title": f"ADAS Map Report - RO {RO}",
        "source_name": f"{RO} ADAS Map.pdf",
        "document_type": "adas_map_report",
        "semantic_type": "ADAS_MAP_REPORT",
    }

    async def get_ro(_settings, _args):
        return reads.pop(0)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, _method, _path, **_kwargs):
        return {
            "requested_count": 1,
            "repaired_count": 0,
            "results": [{"ro_number": RO, "status": "unverified"}],
        }

    async def resolve_snapshot(_settings, identifier):
        assert identifier == RO
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "repair_order_id": "ro-1",
            "snapshot": _operator_snapshot(vin=None, documents=[document]),
        }

    async def fetch_document(_settings, document_id):
        assert document_id == "doc-map"
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "content": b"%PDF-1.4 test-map",
            "sha256": "a" * 64,
        }

    async def execute(_settings, adas, args):
        assert adas is None
        operator_calls.append(args)
        return {"status": "verified", "success": True, "verified": True}

    async def final_snapshot(_settings, repair_order_id):
        assert repair_order_id == "ro-1"
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "snapshot": _operator_snapshot(vin=VIN, documents=[document]),
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)
    monkeypatch.setattr(calibration_iq, "operator_resolve_snapshot", resolve_snapshot)
    monkeypatch.setattr(calibration_iq, "fetch_operator_document", fetch_document)
    monkeypatch.setattr(calibration_iq, "operator_execute", execute)
    monkeypatch.setattr(calibration_iq, "operator_snapshot", final_snapshot)
    monkeypatch.setattr(
        vin_repair,
        "_extract_pdf_text",
        lambda _raw, _filename: f"Repair Order: {RO}\nVIN: {VIN}\n",
    )

    context = {
        "conversation_id": 7,
        "message_id": 91,
        "tool_call_id": "research-si-call",
        "user_id": "owner",
        "role": "owner",
    }
    token = vin_repair._INVOCATION_CONTEXT.set(context)
    try:
        reader = research.default_ro_reader(SimpleNamespace())
        result = await reader({"repair_order_id": RO})
    finally:
        vin_repair._INVOCATION_CONTEXT.reset(token)

    assert research.vin_from_read(result) == VIN
    assert len(operator_calls) == 1
    action = operator_calls[0]["actions"][0]
    assert action == {
        "operation": "update_ro",
        "repair_order_id": "ro-1",
        "expected_version": 39,
        "arguments": {"vin": VIN},
    }
    invocation = operator_calls[0][calibration_iq._INVOCATION_CONTEXT_KEY]
    assert invocation["conversation_id"] == 7
    assert invocation["message_id"] == 91
    assert "adas-map-vin" in invocation["tool_call_id"]


@pytest.mark.asyncio
async def test_conflicting_attached_adas_maps_never_choose_a_vin(monkeypatch):
    documents = [
        {
            "id": "doc-a",
            "title": f"{RO} ADAS Map",
            "source_name": f"{RO} ADAS Map A.pdf",
            "semantic_type": "ADAS_MAP_REPORT",
        },
        {
            "id": "doc-b",
            "title": f"{RO} ADAS Map",
            "source_name": f"{RO} ADAS Map B.pdf",
            "semantic_type": "ADAS_MAP_REPORT",
        },
    ]

    async def fetch_document(_settings, document_id):
        return {
            "status": "verified",
            "success": True,
            "verified": True,
            "content": document_id.encode("ascii"),
            "sha256": ("a" if document_id == "doc-a" else "b") * 64,
        }

    def extract(raw, _filename):
        return (
            f"RO {RO}\nVIN: {VIN}"
            if raw == b"doc-a"
            else f"RO {RO}\nVIN: {VIN_2}"
        )

    monkeypatch.setattr(calibration_iq, "fetch_operator_document", fetch_document)
    monkeypatch.setattr(vin_repair, "_extract_pdf_text", extract)

    result = await vin_repair._attached_map_vin(
        SimpleNamespace(),
        _operator_snapshot(vin=None, documents=documents),
        RO,
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_vin_without_scrapex_proof_stays_blocked(monkeypatch):
    calls = []

    async def get_ro(_settings, _args):
        return _read(vin=None, adas_map=False)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {
            "requested_count": 1,
            "repaired_count": 0,
            "results": [{"ro_number": RO, "status": "unverified"}],
        }

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})

    assert research.vin_from_read(result) == ""
    assert calls == [
        (
            "POST",
            "/api/adas-map/repair-proven-vins",
            {"ro_numbers": [RO]},
        )
    ]


@pytest.mark.asyncio
async def test_existing_valid_vin_never_calls_backfill(monkeypatch):
    async def get_ro(_settings, _args):
        return _read(vin=VIN)

    async def should_not_start(_settings):
        raise AssertionError("ScrapeX repair must not run when CIQ already has a VIN")

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", should_not_start)

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})
    assert research.vin_from_read(result) == VIN


@pytest.mark.asyncio
async def test_unverified_scrapex_backfill_without_chat_context_leaves_guard_closed(
    monkeypatch,
):
    async def get_ro(_settings, _args):
        return _read(vin=None)

    async def start_native(_settings):
        return {"success": True, "verified": True}

    async def request(_settings, _method, _path, **_kwargs):
        return {
            "requested_count": 1,
            "repaired_count": 0,
            "results": [{"ro_number": RO, "status": "unverified"}],
        }

    async def should_not_resolve(_settings, _identifier):
        raise AssertionError(
            "Attached-map mutation requires the research_si chat invocation context"
        )

    monkeypatch.setattr(calibration_iq, "get_repair_order", get_ro)
    monkeypatch.setattr(scrapex, "start_native", start_native)
    monkeypatch.setattr(scrapex, "_request", request)
    monkeypatch.setattr(
        calibration_iq, "operator_resolve_snapshot", should_not_resolve
    )

    reader = research.default_ro_reader(SimpleNamespace())
    result = await reader({"repair_order_id": RO})
    assert research.vin_from_read(result) == ""
