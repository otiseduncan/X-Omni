import asyncio
import base64
import hashlib
import io
import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from core.api.routes import create_router
from core.orchestrator import prompt as prompt_module
from core.orchestrator.loop import (
    ARTIFACT_FOR_TOOL,
    Orchestrator,
)
from core.services import camera
from core.services import exterior_camera
from core.tools.registry import Registry, TOOL_SCHEMAS


class _Reader:
    def __init__(self, chunks=()):
        self._chunks = deque(chunks or (b"",))

    async def read(self, _size=-1):
        await asyncio.sleep(0)
        return self._chunks.popleft() if self._chunks else b""


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, value):
        self.data.extend(value)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.sleep(0)


class _BlockingReader:
    def __init__(self, started):
        self.started = started

    async def read(self, _size=-1):
        self.started.set()
        await asyncio.Event().wait()


class _FakeProcess:
    _next_pid = 40_000

    def __init__(
        self,
        *,
        stdout=(b"--xomni\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n",),
        stderr=(b"",),
        returncode=None,
    ):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.started_at = float(self.pid)
        self.stdin = _Writer()
        self.stdout = _Reader(stdout)
        self.stderr = _Reader(stderr)
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self._stopped = asyncio.Event()
        if returncode is not None:
            self._stopped.set()

    async def wait(self):
        await self._stopped.wait()
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self._stopped.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._stopped.set()


def _frame() -> camera.CameraFrame:
    raw = b"bounded-probe-frame"
    return camera.CameraFrame(
        raw=raw,
        mime="image/jpeg",
        width=640,
        height=360,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _ffmpeg(tmp_path: Path) -> Path:
    path = tmp_path / "ffmpeg.exe"
    path.write_bytes(b"test fixture; never executed")
    return path


def _install_successful_probe(monkeypatch, service, seen=None):
    async def probe(credentials):
        if seen is not None:
            seen.append(credentials)
        return _frame()

    monkeypatch.setattr(service, "_probe_frame", probe)
    _install_stream_resolver(monkeypatch, service)


def _install_stream_resolver(monkeypatch, service, seen=None):
    async def resolve(credentials):
        if seen is not None:
            seen.append(credentials)
        return (
            f"rtsp://{credentials.host}:554/"
            "user=fixture_password=opaque_channel=0_stream=1&onvif=0.sdp?real_stream"
        )

    monkeypatch.setattr(service, "_resolve_stream_uri", resolve)


def _local_name(element):
    return str(element.tag).rsplit("}", 1)[-1]


def _soap_body(operation_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        f"<s:Body>{operation_xml}</s:Body></s:Envelope>"
    ).encode("utf-8")


def _profiles_response() -> bytes:
    def profile(token, encoding, width, height):
        return (
            f'<trt:Profiles token="{token}">'
            f"<tt:Name>{token}</tt:Name>"
            "<trt:VideoEncoderConfiguration>"
            f"<tt:Encoding>{encoding}</tt:Encoding>"
            "<tt:Resolution>"
            f"<tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height>"
            "</tt:Resolution>"
            "</trt:VideoEncoderConfiguration>"
            "</trt:Profiles>"
        )

    return _soap_body(
        "<trt:GetProfilesResponse>"
        + profile("mainStream", "H264", 2304, 1296)
        + profile("subStream", "H264", 704, 576)
        + profile("snapStream", "JPEG", 704, 576)
        + "</trt:GetProfilesResponse>"
    )


def _stream_uri_response(uri: str) -> bytes:
    escaped = uri.replace("&", "&amp;")
    return _soap_body(
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        f"<tt:Uri>{escaped}</tt:Uri>"
        "<tt:InvalidAfterConnect>true</tt:InvalidAfterConnect>"
        "<tt:InvalidAfterReboot>true</tt:InvalidAfterReboot>"
        "<tt:Timeout>PT60S</tt:Timeout>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )


def test_configuration_uses_injected_dpapi_and_never_projects_password(
    tmp_path, monkeypatch, caplog
):
    password = "camera-password-that-must-not-leak"
    protected_blob = b"opaque-dpapi-test-envelope"
    protected_inputs = []
    unprotected_inputs = []

    def protect(raw: bytes) -> bytes:
        protected_inputs.append(raw)
        return protected_blob

    def unprotect(raw: bytes) -> bytes:
        unprotected_inputs.append(raw)
        assert raw == protected_blob
        return password.encode("utf-8")

    credential_path = tmp_path / "private" / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=protect,
        unprotect=unprotect,
    )
    seen_credentials = []
    _install_successful_probe(monkeypatch, service, seen_credentials)

    async def scenario():
        result = await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password=password,
        )
        session = await service.create_session(
            conversation_id=17, owner_id="session:owner-a"
        )
        await service.delete_session(
            session_id=session["session_id"], owner_id="session:owner-a"
        )
        return result

    result = asyncio.run(scenario())
    record_text = credential_path.read_text(encoding="utf-8")
    record = json.loads(record_text)
    public_text = json.dumps(
        {"configure": result, "status": service.status()}, sort_keys=True
    )

    assert protected_inputs == [password.encode("utf-8")]
    assert unprotected_inputs == [protected_blob]
    assert record["version"] == 1
    assert record["password_dpapi"]
    assert password not in record_text
    assert password not in public_text
    assert password not in repr(seen_credentials)
    assert "rtsp://" not in record_text
    assert password not in caplog.text
    assert result["verified"] is True
    assert result["probe"] == {
        "mime": "image/jpeg",
        "bytes": len(_frame().raw),
        "width": 640,
        "height": 360,
    }
    assert service.status()["password_stored"] is True


def test_bad_camera_auth_keeps_uri_off_argv_and_never_persists_credentials(
    tmp_path, monkeypatch, caplog
):
    password = "camera-secret-401"
    calls = []

    async def process_factory(*args, **kwargs):
        process = _FakeProcess(
            stdout=(b"",),
            stderr=(
                b"401 Unauthorized opening rtsp://camera-admin:"
                + password.encode("ascii")
                + b"@192.168.1.73/live\n",
            ),
            returncode=1,
        )
        calls.append((tuple(args), kwargs, process))
        return process

    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"must-not-be-called-when-probe-fails",
        unprotect=lambda raw: raw,
        process_factory=process_factory,
    )
    _install_stream_resolver(monkeypatch, service)

    async def scenario():
        with pytest.raises(exterior_camera.ExteriorCameraAuthError) as raised:
            await service.configure(
                label="Driveway",
                host="192.168.1.73",
                username="camera-admin",
                password=password,
            )
        return str(raised.value)

    safe_error = asyncio.run(scenario())
    assert safe_error == "Exterior camera credentials were rejected."
    assert password not in safe_error
    assert password not in caplog.text
    assert not credential_path.exists()
    assert len(calls) == 1

    argv, kwargs, process = calls[0]
    argv_text = " ".join(argv)
    manifest = bytes(process.stdin.data)
    assert argv[argv.index("-i") + 1] == "pipe:0"
    assert "rtsp://" not in argv_text
    assert "camera-admin" not in argv_text
    assert password not in argv_text
    assert manifest.startswith(b"ffconcat version 1.0\nfile 'rtsp://")
    assert b"192.168.1.73" in manifest
    assert b"fixture_password=opaque" in manifest
    assert b"camera-admin" not in manifest
    assert password.encode("ascii") not in manifest
    assert manifest.count(b"\nfile ") == 1
    assert manifest.count(b"\noption rtsp_transport tcp\n") == 1
    assert process.stdin.closed is True
    assert kwargs["stdin"] == asyncio.subprocess.PIPE
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


def test_onvif_wsse_selects_h264_substream_pins_authority_and_keeps_token_off_argv(
    tmp_path, caplog
):
    login_password = "login<&password-must-not-leak"
    media_token = "opaque-media-token-must-not-leak"
    advertised_uri = (
        "rtsp://192.168.1.226:554/"
        f"user=media_password={media_token}_channel=0_stream=1&onvif=0.sdp"
        "?real_stream"
    )
    requests = []
    spawned = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "http://192.168.1.10:8899/onvif/media_service"
        assert login_password.encode("utf-8") not in request.content
        root = ET.fromstring(request.content)
        body = next(item for item in root if _local_name(item) == "Body")
        operation = next(iter(body))
        operation_name = _local_name(operation)
        token = next(item for item in root.iter() if _local_name(item) == "UsernameToken")
        username = next(item for item in token if _local_name(item) == "Username")
        password = next(item for item in token if _local_name(item) == "Password")
        nonce = next(item for item in token if _local_name(item) == "Nonce")
        created = next(item for item in token if _local_name(item) == "Created")
        assert username.text == "operator<&"
        raw_nonce = base64.b64decode(nonce.text, validate=True)
        expected_digest = base64.b64encode(
            hashlib.sha1(
                raw_nonce
                + created.text.encode("utf-8")
                + login_password.encode("utf-8")
            ).digest()
        ).decode("ascii")
        assert password.text == expected_digest
        datetime.fromisoformat(created.text.replace("Z", "+00:00"))
        requests.append((operation_name, raw_nonce, created.text, request.content))
        if operation_name == "GetProfiles":
            content = _profiles_response()
        else:
            profile_token = next(
                item for item in operation if _local_name(item) == "ProfileToken"
            )
            assert profile_token.text == "subStream"
            content = _stream_uri_response(advertised_uri)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            content=content,
        )

    async def process_factory(*args, **kwargs):
        process = _FakeProcess(stdout=(_jpeg(96, 64), b""), returncode=0)
        spawned.append((tuple(args), kwargs, process))
        return process

    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda _raw: b"opaque-dpapi-envelope",
        unprotect=lambda _raw: login_password.encode("utf-8"),
        process_factory=process_factory,
        onvif_transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        service.configure(
            label="Exterior",
            host="192.168.1.10",
            username="operator<&",
            password=login_password,
        )
    )

    assert result["verified"] is True
    assert [request[0] for request in requests] == ["GetProfiles", "GetStreamUri"]
    assert requests[0][1] != requests[1][1]
    record_text = credential_path.read_text(encoding="utf-8")
    assert json.loads(record_text)["host"] == "192.168.1.10"
    assert media_token not in record_text
    assert login_password not in record_text
    assert media_token not in json.dumps(result)
    assert media_token not in caplog.text
    assert len(spawned) == 1
    argv, kwargs, process = spawned[0]
    argv_text = " ".join(argv)
    manifest = bytes(process.stdin.data)
    assert "-rtsp_transport" not in argv
    assert argv[argv.index("-protocol_whitelist") + 1] == "file,pipe,tcp,rtsp"
    assert "rtsp://" not in argv_text
    assert media_token not in argv_text
    assert manifest.count(b"\nfile ") == 1
    assert manifest.count(b"\noption rtsp_transport tcp\n") == 1
    assert b"rtsp://192.168.1.10:554/" in manifest
    assert b"192.168.1.226" not in manifest
    assert media_token.encode("ascii") in manifest
    assert login_password.encode("utf-8") not in manifest
    assert kwargs["stdin"] == asyncio.subprocess.PIPE


@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://user:pass@192.168.1.226:554/user=x_password=y_channel=0_stream=1&onvif=0.sdp?real_stream",
        "rtsp://192.168.1.226:80/user=x_password=y_channel=0_stream=1&onvif=0.sdp?real_stream",
        "rtsp://192.168.1.226:554/user=x_password=y_channel=0_stream=1&onvif=0.sdp?real_stream#fragment",
        "rtsp://192.168.1.226:554/user=x_password=y_channel=0_stream=1&onvif=0.sdp?wrong",
        "rtsp://192.168.1.226:554/user=x_password=y_channel=0_stream=1&onvif=0.sdp?real_stream\nfile 'x'",
        "rtsp://192.168.1.226:554/user=x_password=y\\z_channel=0_stream=1&onvif=0.sdp?real_stream",
        "rtsp://192.168.1.226:554/user=x_password=bad%Q1_channel=0_stream=1&onvif=0.sdp?real_stream",
        " rtsp://192.168.1.226:554/user=x_password=y_channel=0_stream=1&onvif=0.sdp?real_stream",
    ],
)
def test_onvif_stream_uri_rejects_untrusted_authority_or_concat_syntax(uri):
    with pytest.raises(exterior_camera.ExteriorCameraUnavailable):
        exterior_camera._rtsp_stream_uri(uri, host="192.168.1.10")


def test_onvif_redirect_is_not_followed_and_xml_boundary_rejects_utf16_dtd(
    tmp_path,
):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        onvif_transport=httpx.MockTransport(handler),
    )
    credentials = exterior_camera._Credentials(
        label="Exterior",
        host="192.168.1.10",
        username="operator",
        password="secret",
    )
    with pytest.raises(exterior_camera.ExteriorCameraUnavailable):
        asyncio.run(service._resolve_stream_uri(credentials))
    assert seen == ["http://192.168.1.10:8899/onvif/media_service"]

    utf16 = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE x [<!ENTITY boom "boom">]>'
        '<Envelope><Body>&boom;</Body></Envelope>'
    ).encode("utf-16")
    with pytest.raises(exterior_camera.ExteriorCameraUnavailable):
        exterior_camera._parse_onvif_xml(utf16)


def test_onvif_profile_caps_codec_order_and_duplicate_uri_boundary():
    body = exterior_camera._parse_onvif_xml(_profiles_response())
    selected = exterior_camera.ExteriorCameraService._select_profile(
        exterior_camera.ExteriorCameraService._profiles_from_response(body)
    )
    assert selected.name == "subStream"
    assert selected.encoding == "H264"

    h265 = _soap_body(
        '<trt:GetProfilesResponse><trt:Profiles token="mainStream">'
        "<tt:Name>mainStream</tt:Name><trt:VideoEncoderConfiguration>"
        "<tt:Encoding>H265</tt:Encoding><tt:Resolution>"
        "<tt:Width>1280</tt:Width><tt:Height>720</tt:Height>"
        "</tt:Resolution></trt:VideoEncoderConfiguration>"
        "</trt:Profiles></trt:GetProfilesResponse>"
    )
    h265_body = exterior_camera._parse_onvif_xml(h265)
    fallback = exterior_camera.ExteriorCameraService._select_profile(
        exterior_camera.ExteriorCameraService._profiles_from_response(h265_body)
    )
    assert fallback.encoding == "H265"

    duplicate_uri = _soap_body(
        "<trt:GetStreamUriResponse><trt:MediaUri>"
        "<tt:Uri>first</tt:Uri><tt:Uri>second</tt:Uri>"
        "</trt:MediaUri></trt:GetStreamUriResponse>"
    )
    duplicate_body = exterior_camera._parse_onvif_xml(duplicate_uri)
    with pytest.raises(exterior_camera.ExteriorCameraUnavailable):
        exterior_camera.ExteriorCameraService._stream_uri_from_response(
            duplicate_body, host="192.168.1.10"
        )


def test_one_owner_bound_session_expires_and_stops_only_its_exact_process(
    tmp_path, monkeypatch
):
    password = b"stored-password"
    protected_blob = b"ciphertext"
    now = [100.0]
    spawned = []
    foreign = _FakeProcess()

    async def process_factory(*args, **kwargs):
        process = _FakeProcess()
        spawned.append((tuple(args), kwargs, process))
        return process

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: protected_blob,
        unprotect=lambda raw: password,
        session_ttl_seconds=5,
        clock=lambda: now[0],
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password=password.decode("ascii"),
        )
        first = await service.create_session(
            conversation_id=5, owner_id="session:owner-a"
        )
        with pytest.raises(exterior_camera.ExteriorCameraConflict):
            await service.create_session(
                conversation_id=6, owner_id="session:owner-b"
            )
        with pytest.raises(exterior_camera.ExteriorCameraSessionNotFound):
            await service.stream(
                session_id=first["session_id"], owner_id="session:owner-b"
            )

        first_stream = await service.stream(
            session_id=first["session_id"], owner_id="session:owner-a"
        )
        assert service.status()["status"] == "streaming"
        with pytest.raises(exterior_camera.ExteriorCameraConflict):
            await service.stream(
                session_id=first["session_id"], owner_id="session:owner-a"
            )
        with pytest.raises(exterior_camera.ExteriorCameraSessionNotFound):
            await service.delete_session(
                session_id=first["session_id"], owner_id="session:owner-b"
            )

        now[0] += 6
        second = await service.create_session(
            conversation_id=7, owner_id="session:owner-b"
        )
        await first_stream.aclose()
        second_stream = await service.stream(
            session_id=second["session_id"], owner_id="session:owner-b"
        )
        await service.shutdown()
        await second_stream.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first["session_id"] != second["session_id"]
    assert first["stream_url"].endswith(
        f"/{first['session_id']}/stream.mjpg"
    )
    assert len(spawned) == 2
    first_process = spawned[0][2]
    second_process = spawned[1][2]
    assert first_process.terminate_calls == 1
    assert second_process.terminate_calls == 1
    assert first_process.kill_calls == second_process.kill_calls == 0
    assert foreign.terminate_calls == foreign.kill_calls == 0
    assert service.status()["streaming"] is False


def test_stream_disconnect_cleans_exact_child_and_releases_single_session(
    tmp_path, monkeypatch
):
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess(
            stdout=(b"--xomni\r\nContent-Type: image/jpeg\r\n\r\none\r\n", b"")
        )
        processes.append(process)
        return process

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )
        first = await service.create_session(
            conversation_id=9, owner_id="session:owner"
        )
        stream = await service.stream(
            session_id=first["session_id"], owner_id="session:owner"
        )
        chunk = await anext(stream)
        await stream.aclose()
        replacement = await service.create_session(
            conversation_id=9, owner_id="session:owner"
        )
        await service.delete_session(
            session_id=replacement["session_id"], owner_id="session:owner"
        )
        return chunk

    chunk = asyncio.run(scenario())
    assert chunk.startswith(b"--xomni")
    assert len(processes) == 1
    assert processes[0].terminate_calls == 1
    assert processes[0].kill_calls == 0


def test_cancelled_configure_waits_for_spawn_handle_then_cleans_exact_child(
    tmp_path, monkeypatch
):
    allocated = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess()
        processes.append(process)
        allocated.set()
        await release.wait()
        return process

    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_stream_resolver(monkeypatch, service)

    async def scenario():
        configure = asyncio.create_task(
            service.configure(
                label="Driveway",
                host="192.168.1.73",
                username="camera-admin",
                password="password",
            )
        )
        await allocated.wait()
        configure.cancel()
        await asyncio.sleep(0)
        assert not configure.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await configure

    asyncio.run(scenario())
    assert len(processes) == 1
    assert processes[0].terminate_calls == 1
    assert processes[0].kill_calls == 0
    assert not credential_path.exists()
    assert service._session is None


def test_cancelled_stream_spawn_cleans_exact_child_and_discards_pending_session(
    tmp_path, monkeypatch
):
    allocated = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess()
        processes.append(process)
        allocated.set()
        await release.wait()
        return process

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )
        session = await service.create_session(
            conversation_id=9, owner_id="session:owner"
        )
        opening = asyncio.create_task(
            service.stream(
                session_id=session["session_id"], owner_id="session:owner"
            )
        )
        await allocated.wait()
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening

    asyncio.run(scenario())
    assert len(processes) == 1
    assert processes[0].terminate_calls == 1
    assert processes[0].kill_calls == 0
    assert service._session is None


def test_cancelled_empty_stream_wait_cleans_child_and_discards_session(
    tmp_path, monkeypatch
):
    wait_started = asyncio.Event()
    processes = []

    class WaitingProcess(_FakeProcess):
        async def wait(self):
            wait_started.set()
            return await super().wait()

    async def process_factory(*_args, **_kwargs):
        process = WaitingProcess(stdout=(b"",), stderr=(b"",))
        processes.append(process)
        return process

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )
        session = await service.create_session(
            conversation_id=9, owner_id="session:owner"
        )
        opening = asyncio.create_task(
            service.stream(
                session_id=session["session_id"], owner_id="session:owner"
            )
        )
        await wait_started.wait()
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening

    asyncio.run(scenario())
    assert len(processes) == 1
    assert processes[0].terminate_calls == 1
    assert processes[0].kill_calls == 0
    assert service._session is None


def test_cancelled_probe_stderr_wait_does_not_swallow_cancel_or_store_password(
    tmp_path, monkeypatch
):
    stderr_started = asyncio.Event()
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess(stdout=(_jpeg(), b""), returncode=0)
        process.stderr = _BlockingReader(stderr_started)
        processes.append(process)
        return process

    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_stream_resolver(monkeypatch, service)

    async def scenario():
        configuring = asyncio.create_task(
            service.configure(
                label="Driveway",
                host="192.168.1.73",
                username="camera-admin",
                password="password",
            )
        )
        await stderr_started.wait()
        configuring.cancel()
        with pytest.raises(asyncio.CancelledError):
            await configuring

    asyncio.run(scenario())
    assert len(processes) == 1
    assert not credential_path.exists()
    assert service._session is None


def test_cancelled_credential_commit_waits_for_background_write_to_finish(
    tmp_path, monkeypatch
):
    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()
    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
    )
    _install_successful_probe(monkeypatch, service)
    original_write = service._write_record

    def blocking_write(credentials):
        write_started.set()
        try:
            assert release_write.wait(timeout=5)
            original_write(credentials)
        finally:
            write_finished.set()

    monkeypatch.setattr(service, "_write_record", blocking_write)

    async def scenario():
        configuring = asyncio.create_task(
            service.configure(
                label="Driveway",
                host="192.168.1.73",
                username="camera-admin",
                password="password",
            )
        )
        while not write_started.is_set():
            await asyncio.sleep(0.001)
        configuring.cancel()
        await asyncio.sleep(0)
        cancellation_escaped_before_write_finished = configuring.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await configuring
        while not write_finished.is_set():
            await asyncio.sleep(0.001)
        return cancellation_escaped_before_write_finished

    escaped_early = asyncio.run(scenario())
    assert escaped_early is False
    assert credential_path.is_file()


def test_consumed_stream_bytes_renew_active_lease_but_pending_session_expires(
    tmp_path, monkeypatch
):
    now = [100.0]
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess(
            stdout=(
                b"--xomni\r\nContent-Type: image/jpeg\r\n\r\none\r\n",
                b"--xomni\r\nContent-Type: image/jpeg\r\n\r\ntwo\r\n",
                b"",
            )
        )
        processes.append(process)
        return process

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        session_ttl_seconds=5,
        clock=lambda: now[0],
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )

        pending = await service.create_session(
            conversation_id=11, owner_id="session:owner"
        )
        now[0] = 106.0
        replacement = await service.create_session(
            conversation_id=11, owner_id="session:owner"
        )
        assert replacement["session_id"] != pending["session_id"]
        await service.delete_session(
            session_id=replacement["session_id"], owner_id="session:owner"
        )

        active = await service.create_session(
            conversation_id=11, owner_id="session:owner"
        )
        stream = await service.stream(
            session_id=active["session_id"], owner_id="session:owner"
        )
        original_expiry = service._session.expires_monotonic
        now[0] = original_expiry - 1
        first = await anext(stream)
        renewed_expiry = service._session.expires_monotonic
        assert renewed_expiry > original_expiry

        now[0] = original_expiry + 1
        second = await anext(stream)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    assert first.endswith(b"one\r\n")
    assert second.endswith(b"two\r\n")
    assert len(processes) == 1
    assert processes[0].terminate_calls == 1


def test_current_frame_is_latest_proxied_jpeg_and_bound_to_session_owner_conversation(
    tmp_path, monkeypatch
):
    first_jpeg = _jpeg(40, 30)
    latest_jpeg = _jpeg(64, 48)
    first_chunk = (
        b"--xomni\r\nContent-Type: image/jpeg\r\n\r\n"
        + first_jpeg
        + b"\r\n"
    )
    latest_chunk = (
        b"--xomni\r\nContent-Type: image/jpeg\r\n\r\n"
        + latest_jpeg
        + b"\r\n"
    )
    processes = []

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess(stdout=(first_chunk, latest_chunk, b""))
        processes.append(process)
        return process

    credential_path = tmp_path / "exterior-camera.json"
    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        credential_path=credential_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )
        session = await service.create_session(
            conversation_id=7, owner_id="session:owner-a"
        )
        with pytest.raises(exterior_camera.ExteriorCameraFrameUnavailable):
            await service.current_frame(
                session_id=session["session_id"],
                owner_id="session:owner-a",
                conversation_id=7,
            )

        stream = await service.stream(
            session_id=session["session_id"], owner_id="session:owner-a"
        )
        with pytest.raises(exterior_camera.ExteriorCameraFrameUnavailable):
            await service.current_frame(
                session_id=session["session_id"],
                owner_id="session:owner-a",
                conversation_id=7,
            )
        for values in (
            {
                "session_id": "wrong_session_123",
                "owner_id": "session:owner-a",
                "conversation_id": 7,
            },
            {
                "session_id": session["session_id"],
                "owner_id": "session:owner-b",
                "conversation_id": 7,
            },
            {
                "session_id": session["session_id"],
                "owner_id": "session:owner-a",
                "conversation_id": 8,
            },
        ):
            with pytest.raises(exterior_camera.ExteriorCameraSessionNotFound):
                await service.current_frame(**values)

        assert await anext(stream) == first_chunk
        first = await service.current_frame(
            session_id=session["session_id"],
            owner_id="session:owner-a",
            conversation_id=7,
        )
        assert await anext(stream) == latest_chunk
        latest = await service.current_frame(
            session_id=session["session_id"],
            owner_id="session:owner-a",
            conversation_id=7,
        )
        await stream.aclose()
        return first, latest

    first, latest = asyncio.run(scenario())
    assert first.raw == first_jpeg
    assert (first.width, first.height) == (40, 30)
    assert latest.raw == latest_jpeg
    assert (latest.width, latest.height) == (64, 48)
    assert latest.sha256 == hashlib.sha256(latest_jpeg).hexdigest()
    assert service._session is None
    assert processes[0].terminate_calls == 1
    persisted = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert first_jpeg not in persisted
    assert latest_jpeg not in persisted


def test_stalled_consumer_watchdog_stops_exact_process_and_clears_frame(
    tmp_path, monkeypatch
):
    now = [100.0]
    wake_watchdog = asyncio.Event()
    processes = []
    jpeg = _jpeg(48, 32)
    chunk = b"--xomni\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    class ImmediateReader:
        def __init__(self, chunks):
            self.chunks = deque(chunks)

        async def read(self, _size=-1):
            return self.chunks.popleft() if self.chunks else b""

    class ImmediateWriter(_Writer):
        async def drain(self):
            return None

        async def wait_closed(self):
            return None

    async def process_factory(*_args, **_kwargs):
        process = _FakeProcess()
        process.stdin = ImmediateWriter()
        process.stdout = ImmediateReader((chunk, b""))
        process.stderr = ImmediateReader((b"",))
        processes.append(process)
        return process

    async def controlled_sleep(_delay):
        await wake_watchdog.wait()

    service = exterior_camera.ExteriorCameraService(
        tmp_path,
        ffmpeg_path=_ffmpeg(tmp_path),
        protect=lambda raw: b"ciphertext",
        unprotect=lambda raw: b"password",
        session_ttl_seconds=5,
        clock=lambda: now[0],
        sleep=controlled_sleep,
        process_factory=process_factory,
    )
    _install_successful_probe(monkeypatch, service)

    async def scenario():
        await service.configure(
            label="Driveway",
            host="192.168.1.73",
            username="camera-admin",
            password="password",
        )
        session_payload = await service.create_session(
            conversation_id=7, owner_id="session:owner"
        )
        session = service._session
        watchdog = session.watchdog_task
        stream = await service.stream(
            session_id=session_payload["session_id"], owner_id="session:owner"
        )
        assert await anext(stream) == chunk
        assert session.current_frame == jpeg
        renewed_expiry = session.expires_monotonic

        # Do not resume the generator: this is the ASGI-backpressure state that
        # used to bypass all inline TTL checks.
        now[0] = renewed_expiry + 1
        wake_watchdog.set()
        await watchdog
        assert service._session is None
        assert session.current_frame is None
        assert processes[0].terminate_calls == 1
        assert processes[0].kill_calls == 0
        await stream.aclose()

    asyncio.run(scenario())


class _ApiStore:
    def __init__(self):
        self.audits = []
        self.saved = []

    @staticmethod
    def conversation_exists(conversation_id):
        return conversation_id == 7

    def audit(self, event, detail):
        self.audits.append((event, detail))

    def add_message(self, conversation_id, role, content, **kwargs):
        self.saved.append((conversation_id, role, content, kwargs))
        return 91


class _ApiRouter:
    active_name = "omni"

    @staticmethod
    def supports_vision():
        return True


class _ApiRegistry:
    policy = {}
    roots = []
    _handlers = {}

    @staticmethod
    def tier(_name):
        return "blocked"

    @staticmethod
    def public_approval(record, receipt=None):
        return record


class _ApiExteriorCamera:
    def __init__(self):
        self.calls = []

    @staticmethod
    def status():
        return {
            "ok": True,
            "configured": True,
            "status": "configured",
            "label": "Driveway",
            "host": "192.168.1.73",
            "username": "camera-admin",
            "password_stored": True,
            "runtime_available": True,
            "streaming": False,
        }

    async def configure(self, **values):
        self.calls.append(("configure", values))
        return {**self.status(), "verified": True}

    async def create_session(self, *, conversation_id, owner_id):
        self.calls.append(("create", conversation_id, owner_id))
        return {
            "ok": True,
            "status": "ready",
            "session_id": "opaque_session_123456",
            "stream_url": (
                "/api/cameras/exterior/sessions/opaque_session_123456/stream.mjpg"
            ),
            "label": "Driveway",
            "conversation_id": conversation_id,
            "expires_at": "2026-08-16T12:00:00+00:00",
            "streaming": False,
        }

    async def stream(self, *, session_id, owner_id):
        self.calls.append(("stream", session_id, owner_id))

        async def chunks():
            yield b"--xomni\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n"

        return chunks()

    async def delete_session(self, *, session_id, owner_id):
        self.calls.append(("delete", session_id, owner_id))
        return {
            "ok": True,
            "status": "stopped",
            "session_id": session_id,
            "streaming": False,
        }

    async def current_frame(self, *, session_id, owner_id, conversation_id):
        self.calls.append(("frame", session_id, owner_id, conversation_id))
        if session_id != "opaque_session_123456":
            raise exterior_camera.ExteriorCameraSessionNotFound(
                "Exterior camera session was not found or has expired."
            )
        return _frame()

    @staticmethod
    def source_metadata():
        return {
            "source": "exterior_camera_still",
            "camera_source_id": "exterior",
            "camera_label": "Driveway",
        }


def _api_app(service, store=None):
    store = store or _ApiStore()

    async def require_session(request: Request):
        if request.headers.get("x-test-owner") != "yes":
            raise HTTPException(401, "Not signed in.")
        return {"google_sub": "owner-a", "token_hash": "owner-token-hash"}

    settings = SimpleNamespace(
        local_origin="http://127.0.0.1:8100",
        public_origin="https://omega.example.ts.net",
    )
    app = FastAPI()
    app.include_router(
        create_router(
            settings,
            store,
            _ApiRouter(),
            _ApiRegistry(),
            require_session,
            exterior_camera=service,
        )
    )
    return app, store


def test_exterior_camera_api_requires_owner_exact_origin_and_safe_stream_status():
    secret = "api-camera-secret"
    service = _ApiExteriorCamera()
    app, store = _api_app(service)
    client = TestClient(app)
    owner = {"X-Test-Owner": "yes"}
    local = {**owner, "Origin": "http://127.0.0.1:8100"}

    assert client.get("/api/cameras/exterior").status_code == 401
    status = client.get("/api/cameras/exterior", headers=owner)
    assert status.status_code == 200
    assert status.json()["configured"] is True

    missing_origin = client.post(
        "/api/cameras/exterior/configure",
        headers=owner,
        json={
            "label": "Driveway",
            "host": "192.168.1.73",
            "username": "camera-admin",
            "password": secret,
        },
    )
    assert missing_origin.status_code == 403
    cross_origin = client.post(
        "/api/cameras/exterior/configure",
        headers={**owner, "Origin": "https://attacker.example"},
        json={
            "label": "Driveway",
            "host": "192.168.1.73",
            "username": "camera-admin",
            "password": secret,
        },
    )
    assert cross_origin.status_code == 403
    assert service.calls == []

    oversized_secret = "oversized-secret-marker"
    oversized_body = json.dumps({
        "label": "Driveway",
        "host": "192.168.1.73",
        "username": "camera-admin",
        "password": oversized_secret + ("x" * 20_000),
    })
    oversized = client.post(
        "/api/cameras/exterior/configure",
        headers={**local, "Content-Type": "application/json"},
        content=oversized_body,
    )
    assert oversized.status_code == 413
    assert oversized_secret not in oversized.text
    assert service.calls == []

    configured = client.post(
        "/api/cameras/exterior/configure",
        headers=local,
        json={
            "label": "Driveway",
            "host": "192.168.1.73",
            "username": "camera-admin",
            "password": secret,
        },
    )
    assert configured.status_code == 200
    assert secret not in configured.text
    assert secret not in json.dumps(store.audits)
    assert service.calls[-1][1]["password"] == secret

    denied_session = client.post(
        "/api/cameras/exterior/sessions",
        headers={**owner, "Origin": "https://attacker.example"},
        json={"conversation_id": 7},
    )
    assert denied_session.status_code == 403
    created = client.post(
        "/api/cameras/exterior/sessions",
        headers=local,
        json={"conversation_id": 7},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["session_id"] == "opaque_session_123456"
    assert payload["stream_url"].startswith("/api/cameras/exterior/sessions/")
    assert "rtsp://" not in created.text
    assert ("create", 7, "session:owner-token-hash") in service.calls

    assert client.get(payload["stream_url"]).status_code == 401
    streamed = client.get(payload["stream_url"], headers=owner)
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith(
        "multipart/x-mixed-replace; boundary=xomni"
    )
    assert streamed.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert streamed.content.startswith(b"--xomni")

    denied_stop = client.delete(
        f"/api/cameras/exterior/sessions/{payload['session_id']}",
        headers={**owner, "Origin": "https://attacker.example"},
    )
    assert denied_stop.status_code == 403
    stopped = client.delete(
        f"/api/cameras/exterior/sessions/{payload['session_id']}",
        headers=local,
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"


def test_exterior_camera_tool_is_read_only_inline_and_starts_nothing():
    service = _ApiExteriorCamera()
    handler = exterior_camera.make_exterior_camera_request(service)
    normal_registry = Registry("config/tools.yaml")
    normal_registry.register("exterior_camera_request", handler)
    assert "exterior_camera_request" in {
        item["function"]["name"] for item in normal_registry.model_tools()
    }

    registry = Registry("config/tools.yaml", profile="full")
    registry.register("exterior_camera_request", handler)
    advertised = {
        item["function"]["name"]: item["function"]
        for item in registry.model_tools()
    }

    result = handler({"prompt": "Check whether a package is at the garage."})
    assert registry.tier("exterior_camera_request") == "read_only"
    assert "exterior_camera_request" in advertised
    assert ARTIFACT_FOR_TOOL["exterior_camera_request"] == "exterior_camera_request"
    assert result["status"] == "awaiting_stream_start"
    assert result["configured"] is True
    assert result["stream_started"] is False
    assert result["capture_received"] is False
    assert result["camera_source_id"] == "exterior"
    assert result["prompt"] == "Check whether a package is at the garage."
    assert service.calls == []
    assert "password" not in json.dumps(result).casefold()
    assert "rtsp://" not in json.dumps(result)

    class OmniRouter:
        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    system = prompt_module.system_prompt(OmniRouter())
    assert "exterior_camera_request" not in system
    schema_description = TOOL_SCHEMAS["exterior_camera_request"]["description"]
    assert "Doesn't start a stream" in schema_description


def test_exterior_camera_observation_is_model_selected_inline():
    request_text = (
        "Look through the exterior camera and tell me what you can see "
        "in the current frame."
    )
    service = _ApiExteriorCamera()
    invocations = []
    real_handler = exterior_camera.make_exterior_camera_request(service)

    def handler(args):
        invocations.append(dict(args))
        return real_handler(args)

    registry = Registry("config/tools.yaml", profile="full")
    registry.register("exterior_camera_request", handler)

    class Store:
        def __init__(self):
            self.saved = []
            self.titles = []

        @staticmethod
        def get_messages(_conversation_id):
            return [{"role": "user", "content": request_text}]

        def add_message(self, conversation_id, role, content, **kwargs):
            self.saved.append((conversation_id, role, content, kwargs))
            return 91

        def touch_conversation(self, conversation_id, title):
            self.titles.append((conversation_id, title))

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    class Client:
        def __init__(self):
            self.calls = []

        async def stream(self, messages, tools=None):
            self.calls.append((messages, tools))
            if len(self.calls) == 1:
                yield {
                    "type": "tool_call",
                    "id": "model-exterior-camera",
                    "name": "exterior_camera_request",
                    "arguments": json.dumps({"prompt": request_text}),
                }
                return
            yield {
                "type": "content",
                "text": "The exterior camera controls are ready in chat.",
            }

    store = Store()
    client = Client()
    orchestrator = Orchestrator(
        Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )

    async def collect():
        return [
            event
            async for event in orchestrator.run_turn(
                7,
                request_text,
            )
        ]

    events = asyncio.run(collect())
    assert invocations == [{"prompt": request_text}]
    assert [
        event["type"] for event in events[:3]
    ] == ["tool_start", "tool_result", "artifact"]
    artifact = events[2]["artifact"]
    assert artifact["type"] == "exterior_camera_request"
    assert artifact["data"]["configured"] is True
    assert artifact["data"]["status"] == "awaiting_stream_start"
    assert not any(event["type"] == "approval" for event in events)
    assert len(client.calls) == 2
    _initial_messages, model_tools = client.calls[0]
    assert any(
        item.get("function", {}).get("name") == "exterior_camera_request"
        for item in model_tools
    )
    model_messages, _model_tools = client.calls[1]
    synthetic = next(message for message in model_messages if message.get("tool_calls"))
    assert synthetic["tool_calls"][0]["function"]["name"] == "exterior_camera_request"
    tool_result = next(message for message in model_messages if message["role"] == "tool")
    assert tool_result["name"] == "exterior_camera_request"
    assert json.loads(tool_result["content"])["configured"] is True
    assert store.saved[0][2] == "The exterior camera controls are ready in chat."


def _jpeg(width=32, height=24) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(output, format="JPEG")
    return output.getvalue()


def _camera_headers(prompt="What is outside?"):
    encoded = base64.urlsafe_b64encode(prompt.encode("utf-8")).decode("ascii")
    return {
        "X-Test-Owner": "yes",
        "Origin": "http://127.0.0.1:8100",
        "Content-Type": "image/jpeg",
        "X-XOmni-Conversation-ID": "7",
        "X-XOmni-Camera-Prompt-B64": encoded.rstrip("="),
        "X-XOmni-Camera-Source-ID": "exterior",
        "X-XOmni-Camera-Session-ID": "opaque_session_123456",
        # This untrusted value must never override the stored service label.
        "X-XOmni-Camera-Label": "Attacker supplied label",
    }


def test_exterior_vision_provenance_uses_server_source_id_and_stored_label(monkeypatch):
    import core.models.client as model_client_module

    class FakeModelClient:
        seen = []

        def __init__(self, _router, **_kwargs):
            pass

        async def complete(self, messages, **_kwargs):
            self.seen.append((messages, _kwargs))
            return "A closed garage door is visible."

    monkeypatch.setattr(model_client_module, "ModelClient", FakeModelClient)
    service = _ApiExteriorCamera()
    app, store = _api_app(service)
    client = TestClient(app)
    invalid_headers = _camera_headers()
    invalid_headers["X-XOmni-Camera-Source-ID"] = "arbitrary-camera"
    invalid = client.post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=invalid_headers,
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "Unknown camera source identifier."
    assert store.saved == []

    missing_session_headers = _camera_headers()
    missing_session_headers.pop("X-XOmni-Camera-Session-ID")
    missing_session = client.post(
        "/api/vision/analyze",
        content=b"",
        headers=missing_session_headers,
    )
    assert missing_session.status_code == 400
    assert missing_session.json()["detail"] == "X-XOmni-Camera-Session-ID is required."
    assert store.saved == []

    uploaded_body = client.post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=_camera_headers(),
    )
    assert uploaded_body.status_code == 400
    assert uploaded_body.json()["detail"] == (
        "Exterior camera analysis does not accept an uploaded image body."
    )
    assert store.saved == []

    response = client.post(
        "/api/vision/analyze",
        content=b"",
        headers=_camera_headers(),
    )

    assert response.status_code == 200
    artifact = response.json()["artifact"]
    assert artifact["type"] == "camera_observation"
    assert artifact["data"]["source"] == "exterior_camera_still"
    assert artifact["data"]["camera_source_id"] == "exterior"
    assert artifact["data"]["camera_label"] == "Driveway"
    assert artifact["data"]["capture_transport"] == "server_mjpeg_frame"
    assert "Attacker supplied label" not in response.text
    assert ("frame", "opaque_session_123456", "session:owner-token-hash", 7) in service.calls
    model_messages, _options = FakeModelClient.seen[0]
    expected_url = "data:image/jpeg;base64," + base64.b64encode(_frame().raw).decode("ascii")
    assert model_messages[1]["content"][1]["image_url"]["url"] == expected_url
    saved_artifact = store.saved[0][3]["artifacts"][0]
    assert saved_artifact["data"]["camera_label"] == "Driveway"
    assert saved_artifact["data"]["capture_transport"] == "server_mjpeg_frame"
    persisted_projection = json.dumps(store.saved) + json.dumps(store.audits) + response.text
    assert expected_url not in persisted_projection
    assert base64.b64encode(_frame().raw).decode("ascii") not in persisted_projection
    analyzed = next(
        detail for event, detail in store.audits if event == "camera_frame_analyzed"
    )
    assert analyzed["source"] == "exterior_camera_still"
    assert analyzed["camera_source_id"] == "exterior"
    assert analyzed["camera_label"] == "Driveway"
    assert analyzed["capture_transport"] == "server_mjpeg_frame"
