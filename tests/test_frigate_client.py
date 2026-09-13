"""The Frigate client's contract with an NVR on another machine.

Two things are load-bearing and are tested as such: a secret must never
leave this process by any path a human or a log could read, and every
failure mode of a network peer that can be asleep must arrive as a
distinguishable structured state rather than an exception nobody handled.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.services.frigate_client import (
    MAX_IMAGE_BYTES,
    FrigateAuthError,
    FrigateCameraNotFound,
    FrigateClient,
    FrigateInvalidRequest,
    FrigateNoRecording,
    FrigateNotConfigured,
    FrigateProtocolError,
    FrigateState,
    FrigateUnavailable,
    normalize_base_url,
    validate_camera_name,
)
from core.services.windows_secrets import SecretStore

PASSWORD = "correct-horse-battery-staple"
TOKEN = "jwt.header.payload"


def _secret_store(tmp_path, *, username="x-omni", password=PASSWORD):
    """A DPAPI-shaped store whose sealing is reversible in-process."""
    store = SecretStore(
        tmp_path / "frigate.bin",
        entropy=b"test",
        description="test",
        protect=lambda raw: b"sealed:" + raw,
        unprotect=lambda raw: raw.removeprefix(b"sealed:"),
    )
    if username is not None:
        store.save({"username": username, "password": password})
    return store


def _client(tmp_path, handler, *, store=None, **kwargs):
    return FrigateClient(
        base_url=kwargs.pop("base_url", "https://frigate.example:8971"),
        camera=kwargs.pop("camera", "exterior"),
        credential_path=tmp_path / "unused.bin",
        transport=httpx.MockTransport(handler),
        store=store if store is not None else _secret_store(tmp_path),
        **kwargs,
    )


def _login_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    if body.get("password") != PASSWORD:
        return httpx.Response(401, json={"message": "unauthorized"})
    return httpx.Response(200, json={"access_token": TOKEN})


# ------------------------------------------------------------ configuration


def test_a_base_url_is_reduced_to_a_bare_origin():
    assert normalize_base_url("https://192.168.1.201:8971/") == "https://192.168.1.201:8971"
    assert normalize_base_url("192.168.1.201:8971") == "https://192.168.1.201:8971"
    assert normalize_base_url("https://frigate.local") == "https://frigate.local:8971"


def test_the_unauthenticated_port_is_rejected_not_merely_discouraged():
    with pytest.raises(FrigateInvalidRequest, match="authenticated API"):
        normalize_base_url("http://frigate.local:5000")


def test_another_route_to_frigate_needs_no_code_change():
    """The endpoint must be able to move without editing this module.

    A reverse proxy or a Tailscale name terminates TLS on 443, so refusing
    every port but 8971 would turn "change one setting" into "change the
    code". Only the unauthenticated API is off-limits.
    """
    # A bare host means Frigate's own port, because that is the common case.
    assert normalize_base_url("https://frigate.example.com") == "https://frigate.example.com:8971"
    # A proxy or Tailscale name terminating TLS on 443 is stated explicitly
    # and accepted -- one setting, no code change.
    assert (
        normalize_base_url("https://omega-frigate.tail1234.ts.net:443")
        == "https://omega-frigate.tail1234.ts.net:443"
    )
    assert normalize_base_url("https://192.168.1.201:8971") == "https://192.168.1.201:8971"


def test_a_base_url_carrying_credentials_is_refused():
    # A password in a URL is a password in every log that ever prints it.
    with pytest.raises(FrigateInvalidRequest):
        normalize_base_url("https://user:secret@frigate.local:8971")


def test_a_camera_name_must_be_a_safe_path_segment():
    assert validate_camera_name("exterior") == "exterior"
    for hostile in ("../etc", "a/b", "back door", "x" * 100, ""):
        with pytest.raises(FrigateInvalidRequest):
            validate_camera_name(hostile)


def test_a_misconfigured_client_still_constructs_and_fails_closed(tmp_path):
    # Construction must never raise: X has to start whatever Frigate's state is.
    client = FrigateClient(
        base_url="not a url at all",
        camera="exterior",
        credential_path=tmp_path / "frigate.bin",
    )
    assert client.configured() is False
    assert "configuration_error" in client.configuration_summary()


# ------------------------------------------------------------------ secrets


def test_no_secret_appears_in_repr_summary_or_logs(tmp_path, caplog):
    store = _secret_store(tmp_path)
    client = _client(tmp_path, lambda request: httpx.Response(500), store=store)
    with caplog.at_level(logging.DEBUG):
        rendered = " ".join(
            [repr(client), repr(store), json.dumps(client.configuration_summary())]
        )
    assert PASSWORD not in rendered
    assert PASSWORD not in caplog.text
    # Presence is reportable; the value is not.
    assert client.configuration_summary()["credential_registered"] is True


def test_frigate_http_never_inherits_machine_proxy_settings(tmp_path):
    client = _client(tmp_path, lambda request: httpx.Response(200, json={}))
    transport_client = client._client(timeout_seconds=1)
    try:
        assert transport_client._trust_env is False
    finally:
        import asyncio

        asyncio.run(transport_client.aclose())


@pytest.mark.asyncio
async def test_no_secret_appears_in_any_exception_raised_outward(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "unauthorized"})

    client = _client(tmp_path, handler)
    with pytest.raises(FrigateAuthError) as raised:
        await client.stats()
    assert PASSWORD not in str(raised.value)
    assert TOKEN not in str(raised.value)


@pytest.mark.asyncio
async def test_an_error_message_names_a_path_but_never_a_query_string(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(500)

    client = _client(tmp_path, handler)
    with pytest.raises(FrigateUnavailable) as raised:
        await client.review(since=datetime.now(timezone.utc))
    message = str(raised.value)
    assert "/api/review" in message
    assert "cameras=" not in message and "?" not in message


# --------------------------------------------------------------- connection


@pytest.mark.asyncio
async def test_an_authenticated_call_logs_in_once_and_reuses_the_session(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            return _login_response(request)
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"service": {"version": "0.15.0"}})

    client = _client(tmp_path, handler)
    await client.stats()
    await client.stats()
    # One login, two reads: repeated calls must not re-authenticate.
    assert calls.count("/api/login") == 1
    assert calls.count("/api/stats") == 2


@pytest.mark.asyncio
async def test_a_wrong_password_is_an_auth_error_not_an_outage(tmp_path):
    client = _client(tmp_path, _login_response, store=_secret_store(tmp_path, password="wrong"))
    with pytest.raises(FrigateAuthError) as raised:
        await client.stats()
    assert raised.value.state == FrigateState.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_an_unreachable_frigate_is_distinguishable_from_a_refusal(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is unreachable")

    client = _client(tmp_path, handler)
    with pytest.raises(FrigateUnavailable) as raised:
        await client.stats()
    assert raised.value.state == FrigateState.UNAVAILABLE
    assert raised.value.state != FrigateState.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        raise httpx.ReadTimeout("too slow")

    client = _client(tmp_path, handler)
    with pytest.raises(FrigateUnavailable):
        await client.stats()


@pytest.mark.asyncio
async def test_a_configured_endpoint_with_no_credential_requires_authentication(tmp_path):
    empty = SecretStore(
        tmp_path / "absent.bin", entropy=b"t", description="t",
        protect=lambda raw: raw, unprotect=lambda raw: raw,
    )
    client = _client(tmp_path, _login_response, store=empty)
    assert client.configured() is True
    with pytest.raises(FrigateAuthError) as raised:
        await client.stats()
    assert raised.value.state == FrigateState.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_verified_registration_rolls_back_a_bad_replacement(tmp_path):
    store = _secret_store(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return _login_response(request)

    client = _client(tmp_path, handler, store=store)
    with pytest.raises(FrigateAuthError):
        await client.save_credential_verified(username="x-omni", password="wrong")
    restored = store.load()
    assert restored["username"] == "x-omni"
    assert restored["password"] == PASSWORD


@pytest.mark.asyncio
async def test_verified_registration_proves_login_and_logical_camera(tmp_path):
    empty = _secret_store(tmp_path, username="", password="")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"cameras": {"exterior": {}}})
        if request.url.path == "/api/stats":
            return httpx.Response(
                200,
                json={"cameras": {"exterior": {"pid": 7, "camera_fps": 5.0}}},
            )
        raise AssertionError(request.url.path)

    client = _client(tmp_path, handler, store=empty)
    result = await client.save_credential_verified(
        username="x-omni", password=PASSWORD
    )
    assert result["health"]["state"] == FrigateState.AVAILABLE
    assert result["health"]["camera_running"] is True
    assert empty.configured() is True


@pytest.mark.asyncio
async def test_an_expired_session_is_renewed_exactly_once(tmp_path):
    calls: list[str] = []
    rejected = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            return _login_response(request)
        # The first read after the session went stale is refused; the retry
        # with a fresh token succeeds.
        if rejected["count"] == 0:
            rejected["count"] += 1
            return httpx.Response(401)
        return httpx.Response(200, json={"service": {"version": "0.15.0"}})

    client = _client(tmp_path, handler)
    assert await client.stats() == {"service": {"version": "0.15.0"}}
    assert calls.count("/api/login") == 2


@pytest.mark.asyncio
async def test_a_changed_password_fails_cleanly_instead_of_looping(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(401)  # the account changed underneath us

    client = _client(tmp_path, handler)
    with pytest.raises(FrigateAuthError):
        await client.stats()
    # Exactly one retry, then a clean refusal -- never an endless login loop.
    assert calls.count("/api/login") == 2


# ------------------------------------------------------------------- health


@pytest.mark.asyncio
async def test_health_requires_the_camera_to_exist_and_to_be_running(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"cameras": {"exterior": {}, "garage": {}}})
        return httpx.Response(
            200,
            json={
                "service": {"version": "0.15.0"},
                "cameras": {"exterior": {"pid": 42, "camera_fps": 5.0}},
            },
        )

    health = await _client(tmp_path, handler).health()
    assert health["state"] == FrigateState.AVAILABLE
    assert health["camera_configured"] is True
    assert health["camera_running"] is True
    assert health["cameras"] == ["exterior", "garage"]


@pytest.mark.asyncio
async def test_a_reachable_frigate_without_that_camera_is_camera_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"cameras": {"garage": {}}})
        return httpx.Response(200, json={})

    health = await _client(tmp_path, handler).health()
    assert health["state"] == FrigateState.CAMERA_UNAVAILABLE
    assert health["camera_configured"] is False


@pytest.mark.asyncio
async def test_a_configured_camera_with_no_live_capture_is_not_called_healthy(tmp_path):
    # Answering on 8971 is not evidence that the camera works.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"cameras": {"exterior": {}}})
        return httpx.Response(200, json={"cameras": {"exterior": {"pid": 0, "camera_fps": 0}}})

    health = await _client(tmp_path, handler).health()
    assert health["state"] == FrigateState.CAMERA_UNAVAILABLE
    assert health["camera_running"] is False


# -------------------------------------------------------------------- media


@pytest.mark.asyncio
async def test_the_latest_frame_comes_back_as_bounded_jpeg_bytes(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        assert request.url.path == "/api/exterior/latest.jpg"
        return httpx.Response(200, content=b"\xff\xd8\xffjpeg-bytes")

    assert await _client(tmp_path, handler).latest_frame() == b"\xff\xd8\xffjpeg-bytes"


@pytest.mark.asyncio
async def test_an_oversized_image_is_refused_rather_than_buffered(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(200, content=b"x" * (MAX_IMAGE_BYTES + 1))

    with pytest.raises(FrigateProtocolError):
        await _client(tmp_path, handler).latest_frame()


@pytest.mark.asyncio
async def test_a_camera_with_no_current_frame_is_camera_unavailable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(404)

    with pytest.raises(FrigateCameraNotFound):
        await _client(tmp_path, handler).latest_frame()


@pytest.mark.asyncio
async def test_a_clip_is_requested_by_epoch_range(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        seen.append(request.url.path)
        return httpx.Response(200, content=b"mp4-bytes")

    since = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    client = _client(tmp_path, handler)
    assert await client.clip_bytes(since, since + timedelta(seconds=60)) == b"mp4-bytes"
    start = int(since.timestamp())
    assert seen == [f"/api/exterior/start/{start}/end/{start + 60}/clip.mp4"]


@pytest.mark.asyncio
async def test_an_excessive_time_range_is_refused_before_any_request(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        raise AssertionError("an over-long range must never reach Frigate")

    since = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    client = _client(tmp_path, handler, max_clip_seconds=300)
    with pytest.raises(FrigateInvalidRequest):
        await client.clip_bytes(since, since + timedelta(hours=2))


@pytest.mark.asyncio
async def test_a_missing_recording_is_its_own_state(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(404)

    since = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(FrigateNoRecording) as raised:
        await _client(tmp_path, handler).clip_bytes(since, since + timedelta(seconds=30))
    assert raised.value.state == FrigateState.NO_RECORDING


# ------------------------------------------------------------------- review


@pytest.mark.asyncio
async def test_review_records_are_requested_for_this_camera_only(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        captured.update(dict(request.url.params))
        return httpx.Response(200, json=[{"id": "r1", "severity": "alert"}])

    since = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    rows = await _client(tmp_path, handler).review(
        since=since, until=since + timedelta(hours=1), severity="alert"
    )
    assert rows == [{"id": "r1", "severity": "alert"}]
    assert captured["cameras"] == "exterior"
    assert captured["severity"] == "alert"
    assert "after" in captured and "before" in captured


@pytest.mark.asyncio
async def test_an_empty_review_history_is_an_empty_list_not_an_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(200, json=[])

    assert await _client(tmp_path, handler).review() == []


@pytest.mark.asyncio
async def test_a_malformed_frigate_response_is_a_protocol_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(FrigateProtocolError):
        await _client(tmp_path, handler).review()


@pytest.mark.asyncio
async def test_review_json_of_the_wrong_shape_is_refused(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        return httpx.Response(200, json={"unexpected": "object"})

    with pytest.raises(FrigateProtocolError):
        await _client(tmp_path, handler).review()


@pytest.mark.asyncio
async def test_a_hostile_event_id_never_reaches_a_url(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return _login_response(request)
        raise AssertionError("a rejected event id must never be requested")

    client = _client(tmp_path, handler)
    for hostile in ("../../etc/passwd", "a b", "", "x" * 200):
        with pytest.raises(FrigateInvalidRequest):
            await client.event_snapshot(hostile)


@pytest.mark.asyncio
async def test_saving_a_credential_invalidates_the_cached_session(tmp_path):
    logins: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            logins.append(1)
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(200, json={"cameras": {}})

    client = _client(tmp_path, handler)
    await client.config()
    client.save_credential(username="x-omni", password="a-new-password")
    await client.config()
    # The new password must be used immediately, not after the old token ages out.
    assert len(logins) == 2
