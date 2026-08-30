from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from core.services import mediamtx_client as mediamtx_client_svc


def _client(handler) -> mediamtx_client_svc.MediaMTXClient:
    return mediamtx_client_svc.MediaMTXClient(transport=httpx.MockTransport(handler))


def test_loopback_validation_rejects_a_public_control_address():
    with pytest.raises(mediamtx_client_svc.MediaMTXInvalidRequest):
        mediamtx_client_svc.MediaMTXClient(control_base_url="http://93.184.216.34:9997")


def test_loopback_validation_accepts_localhost_and_private_addresses():
    client = mediamtx_client_svc.MediaMTXClient(
        control_base_url="http://localhost:9997",
        playback_base_url="http://192.168.1.5:9996",
    )
    assert client.control_base_url == "http://localhost:9997"
    assert client.playback_base_url == "http://192.168.1.5:9996"


def test_url_builders_are_pure_and_never_touch_the_network():
    client = mediamtx_client_svc.MediaMTXClient()
    assert client.hls_playlist_url("exterior_sub") == "http://127.0.0.1:8888/exterior_sub/index.m3u8"
    assert client.whep_url("exterior_sub") == "http://127.0.0.1:8889/exterior_sub/whep"
    assert client.rtsp_url("exterior") == "rtsp://127.0.0.1:8554/exterior"


@pytest.mark.asyncio
async def test_path_status_returns_none_on_404_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/paths/get/exterior"
        return httpx.Response(404)

    client = _client(handler)
    assert await client.path_status("exterior") is None


@pytest.mark.asyncio
async def test_path_status_raises_unavailable_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)
    with pytest.raises(mediamtx_client_svc.MediaMTXUnavailable):
        await client.path_status("exterior")


@pytest.mark.asyncio
async def test_camera_online_requires_both_source_and_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"source": {"type": "rtspSource"}, "ready": True})

    client = _client(handler)
    assert await client.camera_online("exterior") is True


@pytest.mark.asyncio
async def test_camera_online_false_when_source_present_but_not_ready():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"source": {"type": "rtspSource"}, "ready": False})

    client = _client(handler)
    assert await client.camera_online("exterior") is False


@pytest.mark.asyncio
async def test_list_recordings_parses_and_sorts_spans_chronologically():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/list"
        assert request.url.params["path"] == "exterior"
        return httpx.Response(
            200,
            json=[
                {"start": "2026-08-30T09:00:00Z", "duration": 60.0},
                {"start": "2026-08-30T08:00:00Z", "duration": 30.0},
                {"start": "not-a-time", "duration": 10.0},
                {"start": "2026-08-30T09:30:00Z", "duration": -5.0},
            ],
        )

    client = _client(handler)
    spans = await client.list_recordings(
        "exterior",
        datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc),
    )
    assert [s.started_at.hour for s in spans] == [8, 9]
    assert spans[1].ended_at == spans[1].started_at.replace(hour=9, minute=1)


@pytest.mark.asyncio
async def test_list_recordings_rejects_until_before_since_without_a_network_call():
    client = _client(lambda request: pytest.fail("no request should be made"))
    with pytest.raises(mediamtx_client_svc.MediaMTXInvalidRequest):
        await client.list_recordings(
            "exterior",
            datetime(2026, 8, 30, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 30, 8, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_fetch_clip_bytes_rejects_a_duration_over_the_cap():
    client = _client(lambda request: pytest.fail("no request should be made"))
    with pytest.raises(mediamtx_client_svc.MediaMTXInvalidRequest):
        await client.fetch_clip_bytes(
            "exterior", datetime.now(timezone.utc), mediamtx_client_svc.MAX_CLIP_DURATION_SECONDS + 1
        )


@pytest.mark.asyncio
async def test_fetch_clip_bytes_returns_the_body_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/get"
        assert request.url.params["format"] == "mp4"
        return httpx.Response(200, content=b"fake-mp4-bytes")

    client = _client(handler)
    data = await client.fetch_clip_bytes("exterior", datetime.now(timezone.utc), 30.0)
    assert data == b"fake-mp4-bytes"


@pytest.mark.asyncio
async def test_fetch_clip_bytes_raises_not_found_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = _client(handler)
    with pytest.raises(mediamtx_client_svc.MediaMTXNotFound):
        await client.fetch_clip_bytes("exterior", datetime.now(timezone.utc), 30.0)


@pytest.mark.asyncio
async def test_fetch_clip_bytes_rejects_an_oversized_response():
    oversized = b"x" * (mediamtx_client_svc.MAX_CLIP_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    client = _client(handler)
    with pytest.raises(mediamtx_client_svc.MediaMTXUnavailable):
        await client.fetch_clip_bytes("exterior", datetime.now(timezone.utc), 30.0)
