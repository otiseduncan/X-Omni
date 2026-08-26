"""Regression coverage for get_repair_order's RO-number resolution bug.

Live reproduction against the real Calibration IQ service: both
/operator/ros/{id}/snapshot and /ros/{id} do a strict internal-id primary-key
lookup server-side (confirmed straight from the backend's
get_repair_order_or_404 -- db.get(RepairOrder, repair_order_id)), so asking
for a repair order by its human-facing number -- the only way Otis actually
refers to one -- 404s on both. The code silently fell back to the plain
collection search, which lacks vin, activity, audit, prerequisites,
assessments, and photos, while still reporting status: "verified" with no
indication anything was skipped.

The fix: after the collection search resolves the exact matching row, use
its internal id for one more snapshot request instead of settling for the
thinner row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from core.services import calibration_iq as ciq


@dataclass
class FakeSettings:
    calibration_iq_base_url: str
    calibration_iq_project_path: Path


@pytest.fixture
def settings(tmp_path: Path) -> FakeSettings:
    project = tmp_path / "calibration iq"
    project.mkdir()
    (project / ".env").write_text("TOOL_SERVICE_TOKEN=test-service-token\n", encoding="utf-8")
    return FakeSettings(
        "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
        project,
    )


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        ciq.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def resolved(_settings):
        return _settings.calibration_iq_base_url

    monkeypatch.setattr(ciq, "resolve_base", resolved)


@pytest.mark.asyncio
async def test_ro_number_resolves_to_id_then_retries_the_rich_snapshot(
    settings, monkeypatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/operator/ros/2400612490/snapshot"):
            return httpx.Response(404, json={"detail": "not_found"})
        if path.endswith("/ros/2400612490"):
            return httpx.Response(404, json={"detail": "not_found"})
        if path.endswith("/collection/ros"):
            assert dict(request.url.params)["q"] == "2400612490"
            return httpx.Response(200, json={"items": [
                {"id": "real-uuid-1", "ro_number": "2400612490",
                 "display_status": "New Arrival", "status": "NEW_ARRIVAL"},
            ]})
        if path.endswith("/operator/ros/real-uuid-1/snapshot"):
            return httpx.Response(200, json={
                "repair_order": {
                    "id": "real-uuid-1", "ro_number": "2400612490",
                    "status": "NEW_ARRIVAL", "vin": "3FTTW8E34PRA72116",
                    "year": 2023, "make": "Ford", "model": "Maverick",
                },
                "shop": {"id": "shop-1", "name": "Perry"},
                "workflow": {"status": "NEW_ARRIVAL", "version": 5},
                "activity": [{"id": "activity-1", "description": "Intake"}],
                "audit": [{"id": "audit-1", "action": "created"}],
                "prerequisites": [{"id": "prereq-1", "title": "Fully assembled"}],
            })
        raise AssertionError(f"unexpected request: {path}")

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "2400612490"})

    assert result["status"] == "verified"
    assert result["repair_order"]["vin"] == "3FTTW8E34PRA72116"
    assert result["repair_order"]["Status"] == "New Arrival"
    # The rich snapshot fields must be present -- this is exactly what the
    # collection-row fallback cannot provide.
    assert "activity" in result["raw"]
    assert "audit" in result["raw"]
    assert "prerequisites" in result["raw"]
    # Exactly the two failed identifier-shaped attempts, the search, then
    # one resolved-id retry -- not a loop, not a third blind guess.
    assert calls == [
        "/api/v1/tools/v1/calibration-iq/operator/ros/2400612490/snapshot",
        "/api/v1/tools/v1/calibration-iq/ros/2400612490",
        "/api/v1/tools/v1/calibration-iq/collection/ros",
        "/api/v1/tools/v1/calibration-iq/operator/ros/real-uuid-1/snapshot",
    ]


@pytest.mark.asyncio
async def test_uuid_identifier_never_touches_collection_search(
    settings, monkeypatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.url.path.endswith("/operator/ros/real-uuid-1/snapshot")
        return httpx.Response(200, json={
            "repair_order": {"id": "real-uuid-1", "ro_number": "2400612490",
                              "status": "NEW_ARRIVAL"},
            "workflow": {"status": "NEW_ARRIVAL", "version": 5},
        })

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "real-uuid-1"})

    assert result["status"] == "verified"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_collection_match_without_a_usable_id_fails_closed(
    settings, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/snapshot") or path.endswith("/ros/2400612490"):
            return httpx.Response(404, json={"detail": "not_found"})
        if path.endswith("/collection/ros"):
            return httpx.Response(200, json={"items": [
                {"ro_number": "2400612490", "display_status": "New Arrival"},
            ]})
        raise AssertionError(f"unexpected request: {path}")

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "2400612490"})

    assert result["status"] == "conflict"
    assert result["repair_order"] is None
    assert "authoritative id" in result["message"]


@pytest.mark.asyncio
async def test_fuzzy_collection_row_is_not_accepted_as_the_requested_ro(
    settings, monkeypatch
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/snapshot") or path.endswith("/ros/2400612490"):
            return httpx.Response(404, json={"detail": "not_found"})
        if path.endswith("/collection/ros"):
            return httpx.Response(200, json={"items": [
                {"id": "wrong-uuid", "ro_number": "2400612499"},
            ]})
        raise AssertionError(f"unexpected request: {path}")

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "2400612490"})

    assert result["status"] == "no_result"
    assert result["repair_order"] is None
    assert not any("wrong-uuid" in path for path in calls)


@pytest.mark.asyncio
async def test_duplicate_exact_collection_rows_are_ambiguous(
    settings, monkeypatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/snapshot") or path.endswith("/ros/2400612490"):
            return httpx.Response(404, json={"detail": "not_found"})
        if path.endswith("/collection/ros"):
            return httpx.Response(200, json={"items": [
                {"id": "uuid-a", "ro_number": "2400612490"},
                {"id": "uuid-b", "ro_number": "2400612490"},
            ]})
        raise AssertionError(f"unexpected request: {path}")

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "2400612490"})

    assert result["status"] == "conflict"
    assert result["repair_order"] is None
    assert "more than one" in result["message"].casefold()


@pytest.mark.asyncio
async def test_workflow_status_disagreeing_with_repair_order_clears_stale_label(
    settings, monkeypatch
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "repair_order": {
                "id": "ro-1", "ro_number": "2400911777",
                "status": "NEW_ARRIVAL", "display_status": "New Arrival",
            },
            "workflow": {"status": "CALIBRATION_READY", "version": 9},
        })

    _install_transport(monkeypatch, handler)
    result = await ciq.get_repair_order(settings, {"repair_order_id": "ro-1"})

    # The stale "New Arrival" label (describing the pre-workflow-update
    # status) must not survive; the fresher raw enum is shown formatted
    # instead of a display_status that now describes the wrong state.
    assert result["repair_order"]["Status"] == "Calibration Ready"
