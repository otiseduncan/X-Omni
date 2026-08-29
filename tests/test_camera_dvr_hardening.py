from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import camera_dvr
from core.services import camera_security


def _dvr(tmp_path: Path, **kwargs) -> camera_dvr.CameraDVR:
    return camera_dvr.CameraDVR(
        SimpleNamespace(), root=tmp_path, required_drive=None, **kwargs
    )


@pytest.mark.skipif(os.name != "nt", reason="E: drive enforcement is Windows-specific")
def test_missing_required_e_drive_never_falls_back_to_another_drive(tmp_path: Path):
    dvr = camera_dvr.CameraDVR(SimpleNamespace(), root=tmp_path)

    with pytest.raises(RuntimeError, match=r"E:"):
        dvr._ensure_storage_sync()

    assert not dvr.recordings_dir.exists()
    assert not dvr.playback_dir.exists()


def test_xiongmai_dynamic_pullpoint_path_remains_narrowly_host_pinned():
    assert camera_security.XiongmaiDVR._pinned_subscription_url(
        "http://192.0.2.200:8899/event_service/0",
        host="192.168.50.25",
    ) == "http://192.168.50.25:8899/event_service/0"

    with pytest.raises(Exception, match="subscription address was invalid"):
        camera_security.XiongmaiDVR._pinned_subscription_url(
            "http://192.0.2.200:8899/admin/0",
            host="192.168.50.25",
        )


def test_range_coverage_rejects_active_tail_and_outage_and_fingerprints_sources():
    start = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    first = {
        "id": 1,
        "filename": "20260829-120000.mkv",
        "started_at": "2026-08-29T12:00:00Z",
        "ended_at": "2026-08-29T12:05:00Z",
        "bytes": 100,
        "codec": "H264",
    }

    with pytest.raises(FileNotFoundError, match="end time"):
        camera_dvr.CameraDVR._validate_range_coverage(
            [first], start, datetime(2026, 8, 29, 12, 6, tzinfo=timezone.utc)
        )

    after_gap = {
        **first,
        "id": 2,
        "filename": "20260829-120600.mkv",
        "started_at": "2026-08-29T12:06:00Z",
        "ended_at": "2026-08-29T12:11:00Z",
    }
    with pytest.raises(FileNotFoundError, match="gap"):
        camera_dvr.CameraDVR._validate_range_coverage(
            [first, after_gap],
            datetime(2026, 8, 29, 12, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 29, 12, 8, tzinfo=timezone.utc),
        )

    contiguous = {**after_gap, "started_at": "2026-08-29T12:05:00Z"}
    fingerprint = camera_dvr.CameraDVR._validate_range_coverage(
        [first, contiguous],
        datetime(2026, 8, 29, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 12, 8, tzinfo=timezone.utc),
    )
    changed = camera_dvr.CameraDVR._validate_range_coverage(
        [first, {**contiguous, "bytes": 101}],
        datetime(2026, 8, 29, 12, 2, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 12, 8, tzinfo=timezone.utc),
    )
    assert len(fingerprint) == 64
    assert changed != fingerprint


@pytest.mark.asyncio
async def test_range_playback_is_bounded_before_archive_or_scratch_access(tmp_path: Path):
    dvr = _dvr(tmp_path)
    start = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="30 minutes"):
        await dvr.range_clip(
            start,
            start + timedelta(minutes=31),
            cache_name="range-too-large",
        )

    assert not dvr.playback_dir.exists()


@pytest.mark.asyncio
async def test_xiongmai_subscription_termination_uses_onvif_events_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = camera_security.XiongmaiDVR(
        SimpleNamespace(), root=tmp_path, required_drive=None
    )
    event_url = "http://192.168.50.25:8899/onvif/event_service"
    credentials = SimpleNamespace(host="192.168.50.25")
    observed_tags: list[str] = []

    async def discover(_client, _credentials):
        return event_url

    async def post_event(
        _client, *, credentials, url, operation, body_builder=None
    ):
        assert url == event_url
        assert operation == "CreatePullPointSubscription"
        operation_element = ET.Element(f"{{{camera_dvr._EVENTS_NS}}}{operation}")
        assert body_builder is not None
        body_builder(operation_element)
        observed_tags.extend(child.tag for child in operation_element)
        body = ET.Element("Body")
        response = ET.SubElement(
            body,
            f"{{{camera_dvr._EVENTS_NS}}}CreatePullPointSubscriptionResponse",
        )
        ET.SubElement(response, "CurrentTime").text = "2026-08-29T12:00:00Z"
        ET.SubElement(response, "TerminationTime").text = "2026-08-29T12:10:00Z"
        return body

    monkeypatch.setattr(dvr, "_discover_event_url", discover)
    monkeypatch.setattr(dvr, "_post_event", post_event)

    assert await dvr._create_subscription(object(), credentials) == event_url
    assert observed_tags == [
        f"{{{camera_dvr._EVENTS_NS}}}InitialTerminationTime"
    ]


@pytest.mark.asyncio
async def test_subscription_renew_and_unsubscribe_use_ws_notification_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    credentials = SimpleNamespace(host="192.168.50.25")
    calls: list[tuple[str, str, list[str]]] = []

    async def post_event(
        _client,
        *,
        credentials,
        url,
        operation,
        body_builder=None,
        namespace=camera_dvr._EVENTS_NS,
        allow_empty_response=False,
    ):
        operation_element = ET.Element(f"{{{namespace}}}{operation}")
        if body_builder is not None:
            body_builder(operation_element)
        calls.append(
            (operation, namespace, [child.tag for child in operation_element])
        )
        body = ET.Element("Body")
        response_namespace = (
            camera_dvr._WSN_NS
            if operation in {"Renew", "Unsubscribe"}
            else camera_dvr._EVENTS_NS
        )
        response = ET.SubElement(
            body, f"{{{response_namespace}}}{operation}Response"
        )
        if operation == "Renew":
            ET.SubElement(response, "CurrentTime").text = "2026-08-29T12:00:00Z"
            ET.SubElement(response, "TerminationTime").text = "2026-08-29T12:10:00Z"
        return body

    monkeypatch.setattr(dvr, "_post_event", post_event)
    url = "http://192.168.50.25:8899/event_service/0"

    await dvr._renew_subscription(object(), credentials, url)
    await dvr._unsubscribe_subscription(object(), credentials, url)

    assert calls == [
        (
            "Renew",
            camera_dvr._WSN_NS,
            [f"{{{camera_dvr._WSN_NS}}}TerminationTime"],
        ),
        ("Unsubscribe", camera_dvr._WSN_NS, []),
    ]
    assert dvr._subscription_renew_at > 0


@pytest.mark.asyncio
async def test_xiongmai_empty_renew_ack_is_accepted_but_empty_pull_is_not(
    tmp_path: Path
):
    dvr = _dvr(tmp_path)
    credentials = SimpleNamespace(
        host="192.168.50.25", username="owner", password="secret"
    )

    def handler(_request):
        return camera_dvr.httpx.Response(200, content=b"")

    async with camera_dvr.httpx.AsyncClient(
        transport=camera_dvr.httpx.MockTransport(handler)
    ) as client:
        await dvr._renew_subscription(
            client,
            credentials,
            "http://192.168.50.25:8899/event_service/0",
        )
        assert dvr._subscription_renew_at > camera_dvr.time.monotonic()

        with pytest.raises(Exception, match="ONVIF response was invalid"):
            await dvr._post_event(
                client,
                credentials=credentials,
                url="http://192.168.50.25:8899/event_service/0",
                operation="PullMessages",
            )


def test_empty_pull_response_never_qualifies_as_onvif_authority(tmp_path: Path):
    dvr = _dvr(tmp_path)

    with pytest.raises(Exception, match="event response was invalid"):
        dvr._event_response(ET.Element("Body"), "PullMessagesResponse")

    assert dvr.events_healthy is False


@pytest.mark.parametrize(
    "body",
    [
        ET.fromstring(
            f"""
            <Body xmlns:tev="{camera_dvr._EVENTS_NS}" xmlns:s="http://www.w3.org/2003/05/soap-envelope">
              <tev:PullMessagesResponse><s:Fault /></tev:PullMessagesResponse>
            </Body>
            """
        ),
        ET.fromstring(
            """
            <Body xmlns:wrong="urn:not-onvif">
              <wrong:PullMessagesResponse />
            </Body>
            """
        ),
    ],
)
def test_faulted_or_wrong_namespace_pull_never_grants_authority(
    tmp_path: Path, body: ET.Element
):
    dvr = _dvr(tmp_path)

    with pytest.raises(Exception):
        dvr._event_response(body, "PullMessagesResponse")

    assert dvr.events_healthy is False


def test_pull_response_requires_valid_camera_lease_times(tmp_path: Path):
    dvr = _dvr(tmp_path)
    body = ET.Element("Body")
    response = ET.SubElement(
        body, f"{{{camera_dvr._EVENTS_NS}}}PullMessagesResponse"
    )

    with pytest.raises(Exception, match="event lease was invalid"):
        dvr._set_subscription_renewal(response, require_times=True)

    assert dvr.events_healthy is False


def test_profile_uses_hevc_main_instead_of_low_quality_h264_substream():
    profiles = [
        SimpleNamespace(
            token="sub", name="Sub", encoding="H264", width=640, height=360, ordinal=0
        ),
        SimpleNamespace(
            token="main", name="Main HEVC", encoding="H265", width=3840, height=2160, ordinal=1
        ),
    ]

    selected = camera_dvr.CameraDVR._select_recording_profile(profiles)

    assert selected.token == "main"
    assert selected.encoding == "H265"


def test_hevc_playback_is_cpu_transcoded_but_h264_is_remuxed():
    assert camera_dvr.CameraDVR._playback_codec_args("H264") == ["-c:v", "copy"]
    hevc = camera_dvr.CameraDVR._playback_codec_args("HEVC")
    assert "libx264" in hevc
    assert not any("cuda" in value.casefold() or "nvenc" in value.casefold() for value in hevc)


def test_recorder_error_classification_never_projects_rtsp_uri():
    secret = b"rtsp://camera/user=x_password=DVR_SECRET_SENTINEL_channel=0"
    public = camera_dvr.CameraDVR._safe_recorder_failure(
        b"Could not open input " + secret
    )

    assert "rtsp://" not in public
    assert "DVR_SECRET_SENTINEL" not in public
    assert public == "DVR recorder lost the camera stream and is retrying."


def test_new_segment_names_are_utc_unique_sessions_and_use_stream_copy(tmp_path: Path):
    camera = SimpleNamespace(_base_args=lambda: ["ffmpeg", "-i", "pipe:0"])
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path, required_drive=None)

    args = dvr._record_args()

    output = Path(args[-1]).name
    assert camera_dvr._SEGMENT_RE.fullmatch(output.replace("%06d", "000000"))
    assert "%" in output
    assert "-strftime" not in args
    assert args[args.index("-c:v") + 1] == "copy"
    assert not any("cuda" in value.casefold() or "nvenc" in value.casefold() for value in args)


@pytest.mark.asyncio
async def test_active_segment_is_not_available_for_cached_playback(tmp_path: Path):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    (dvr.recordings_dir / "20260829-120000.mkv").write_bytes(b"open")
    dvr._process = SimpleNamespace(returncode=None)
    await dvr._index_segments(force=True)
    segment = (await dvr.list_segments(limit=1))[0]

    with pytest.raises(FileNotFoundError, match="still being written"):
        await dvr.segment_playback(segment["id"])


@pytest.mark.asyncio
async def test_retention_clears_cache_then_deletes_oldest_complete_only(tmp_path: Path):
    dvr = _dvr(tmp_path, reserve_bytes=256 * 1024 * 1024)
    dvr.recordings_dir.mkdir(parents=True)
    oldest = dvr.recordings_dir / "20260829-120000.mkv"
    newest = dvr.recordings_dir / "20260829-120500.mkv"
    oldest.write_bytes(b"old")
    newest.write_bytes(b"active")
    dvr.playback_dir.mkdir(parents=True)
    cached = dvr.playback_dir / "motion-1.mp4"
    cached.write_bytes(b"cache")
    dvr._process = SimpleNamespace(returncode=None)
    await dvr._index_segments(force=True)

    usages = iter(
        [
            SimpleNamespace(total=1000, used=999, free=1),
            SimpleNamespace(total=1000, used=999, free=1),
            SimpleNamespace(total=1000, used=1, free=999_999_999),
        ]
    )
    dvr._disk_usage_sync = lambda: next(usages)

    await dvr._prune()

    assert not cached.exists()
    assert not oldest.exists()
    assert newest.exists()
    rows = await dvr.list_segments(limit=10)
    assert [row["filename"] for row in rows] == [newest.name]
    assert rows[0]["complete"] == 0


@pytest.mark.asyncio
async def test_retention_discards_tampered_index_row_without_deleting_outside_root(tmp_path: Path):
    dvr = _dvr(tmp_path, reserve_bytes=256 * 1024 * 1024)
    dvr._ensure_storage_sync()
    victim = tmp_path / "victim.mkv"
    victim.write_bytes(b"preserve")
    conn = sqlite3.connect(dvr.db_path)
    try:
        conn.execute(
            """
            INSERT INTO segments
                (filename, started_at, ended_at, bytes, complete, indexed_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            ("../victim.mkv", "2000-01-01T00:00:00Z", "2000-01-01T00:05:00Z", 8, "now"),
        )
        conn.commit()
    finally:
        conn.close()
    usages = iter(
        [
            SimpleNamespace(total=1000, used=999, free=1),
            SimpleNamespace(total=1000, used=999, free=1),
            SimpleNamespace(total=1000, used=1, free=999_999_999),
        ]
    )
    dvr._disk_usage_sync = lambda: next(usages)

    await dvr._prune()

    assert victim.read_bytes() == b"preserve"
    conn = sqlite3.connect(dvr.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_index_refresh_is_throttled_between_api_reads(tmp_path: Path, monkeypatch):
    dvr = _dvr(tmp_path)
    calls = 0

    async def indexed():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(dvr, "_index_segments_locked", indexed)
    await dvr._index_segments()
    await dvr._index_segments()
    assert calls == 1


class _FailingWriter:
    def write(self, _value):
        raise BrokenPipeError("fixture")

    async def drain(self):
        return None

    def close(self):
        return None


class _EmptyReader:
    async def read(self, _size):
        return b""


class _OwnedProcess:
    def __init__(self):
        self.stdin = _FailingWriter()
        self.stderr = _EmptyReader()
        self.returncode = None
        self.terminated = False
        self.pid = 123

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


@pytest.mark.asyncio
async def test_failed_manifest_pipe_write_reaps_owned_recorder_child(tmp_path: Path, monkeypatch):
    process = _OwnedProcess()

    async def factory(*_args, **_kwargs):
        return process

    dvr = camera_dvr.CameraDVR(
        SimpleNamespace(),
        root=tmp_path,
        required_drive=None,
        process_factory=factory,
    )

    async def manifest():
        return b"ffconcat version 1.0\n", camera_dvr.RecordingProfile(
            "main", "Main", "H264", 1920, 1080
        )

    monkeypatch.setattr(dvr, "_recording_manifest", manifest)
    monkeypatch.setattr(dvr, "_record_args", lambda: ["ffmpeg"])

    with pytest.raises(BrokenPipeError):
        await dvr._start_recorder()

    assert process.terminated is True
    assert dvr._process is None


class _StubbornProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self):
        await asyncio.Event().wait()

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_stubborn_recorder_stop_is_bounded_and_retains_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    process = _StubbornProcess()
    dvr._process = process
    dvr._wait_task = asyncio.create_task(process.wait())
    monkeypatch.setattr(camera_dvr, "PROCESS_STOP_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(dvr._stop_recorder(), timeout=0.2)

    assert process.terminated is True
    assert process.killed is True
    assert dvr._process is process
    assert dvr._wait_task is not None
    assert "ownership was retained" in str(dvr._last_error)
    dvr._wait_task.cancel()
    await asyncio.gather(dvr._wait_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_e_error_remains_visible_while_recorder_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    attempts = 0

    async def no_index(*, force=False):
        return None

    async def no_prune():
        return None

    async def start():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("DVR drive E: is not available.")
        dvr._stopped = True

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(dvr, "_index_segments", no_index)
    monkeypatch.setattr(dvr, "_prune", no_prune)
    monkeypatch.setattr(dvr, "_start_recorder", start)
    monkeypatch.setattr(camera_dvr.asyncio, "sleep", no_delay)

    await dvr.run_forever()

    assert attempts == 2
    assert dvr._last_error == "DVR drive E: is not available."


def _router_app(tmp_path: Path, session: dict):
    async def require_session():
        return session

    camera = SimpleNamespace(ffmpeg_path=None)
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path / "dvr", required_drive=None)
    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
    )
    store = SimpleNamespace()
    app = FastAPI()
    app.include_router(camera_dvr.create_router(settings, store, require_session, dvr))
    return app, dvr


@pytest.mark.parametrize("session", [{}, {"role": "test_user"}])
def test_dvr_router_fails_closed_for_non_owner_roles(tmp_path: Path, session: dict):
    app, _dvr_instance = _router_app(tmp_path, session)
    response = TestClient(app).get("/dvr")
    assert response.status_code == 403


def test_dvr_trailing_slash_is_the_owner_dvr_not_chat_spa(tmp_path: Path):
    app, _dvr_instance = _router_app(tmp_path, {"role": "owner"})
    response = TestClient(app).get("/dvr/")
    assert response.status_code == 200
    assert "X Omni DVR" in response.text


def test_dvr_rejects_untracked_cache_and_out_of_range_ids(tmp_path: Path):
    app, dvr = _router_app(tmp_path, {"role": "owner"})
    dvr._ensure_storage_sync()
    rogue = dvr.playback_dir / "motion-999.mp4"
    rogue.write_bytes(b"rogue")
    client = TestClient(app)

    assert client.get("/dvr/api/clips/motion-999.mp4").status_code == 404
    assert client.get(f"/dvr/api/segments/{2**80}/video.mp4").status_code == 404


def test_segment_video_is_served_inline(tmp_path: Path):
    app, dvr = _router_app(tmp_path, {"role": "owner"})
    prepared = tmp_path / "prepared.mp4"
    prepared.write_bytes(b"video")

    async def segment_playback(_segment_id: int):
        return prepared

    dvr.segment_playback = segment_playback
    response = TestClient(app).get("/dvr/api/segments/1/video.mp4")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline;")


def test_standalone_event_video_falls_back_to_tracked_historical_timelapse(
    tmp_path: Path, monkeypatch
):
    clip_dir = tmp_path / "snapshots" / camera_dvr.camera_monitoring_svc.CLIP_SUBDIR
    clip_dir.mkdir(parents=True)
    historical = clip_dir / "motion-7.mp4"
    historical.write_bytes(b"historical")

    class Store:
        @staticmethod
        def list_camera_events_by_burst(_burst_id):
            return [{"id": 70, "burst_id": 7}]

        @staticmethod
        def camera_clip_is_tracked(filename):
            return filename == historical.name

    async def require_session():
        return {"role": "owner"}

    async def no_continuous(_store, _burst_id):
        raise FileNotFoundError("fixture")

    async def legacy_clip(_store, _settings, _ffmpeg, _args):
        return {"ok": True, "clip_url": f"/api/camera-clips/{historical.name}"}

    camera = SimpleNamespace(ffmpeg_path=None)
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path / "dvr", required_drive=None)
    dvr.event_clip = no_continuous
    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
    )
    monkeypatch.setattr(camera_dvr.camera_monitoring_svc, "camera_motion_clip", legacy_clip)
    app = FastAPI()
    app.include_router(
        camera_dvr.create_router(settings, Store(), require_session, dvr)
    )

    response = TestClient(app).get("/dvr/api/events/7/video.mp4")

    assert response.status_code == 200
    assert response.content == b"historical"
    assert response.headers["content-disposition"].startswith("inline;")
