from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

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


def _jpeg_bytes() -> bytes:
    # A compact fully-decodable JPEG fixture without involving a camera or FFmpeg.
    encoded = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(encoded, format="JPEG")
    return encoded.getvalue()


class _PlaybackReader:
    async def read(self, _size):
        return b""


class _CompletedPlaybackProcess:
    def __init__(self):
        self.stderr = _PlaybackReader()
        self.returncode = 0

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_footage_analysis_extracts_bounded_chronological_samples_from_immutable_segments(
    tmp_path: Path,
):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    source = recordings / "20260830T060000000000Z-000000.mkv"
    source.write_bytes(b"immutable-dvr-source")
    stat = source.stat()
    observed_commands: list[tuple] = []

    async def factory(*args, **_kwargs):
        observed_commands.append(args)
        Path(args[-1]).write_bytes(_jpeg_bytes())
        return _CompletedPlaybackProcess()

    camera = SimpleNamespace(_require_ffmpeg=lambda: tmp_path / "ffmpeg.exe")
    dvr = camera_dvr.CameraDVR(
        camera, root=tmp_path, required_drive=None, process_factory=factory
    )
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
    until = since + timedelta(minutes=1)
    row = {
        "id": 1,
        "filename": source.name,
        "started_at": since.isoformat().replace("+00:00", "Z"),
        "ended_at": (since + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "bytes": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "codec": "HEVC",
        "width": 2304,
        "height": 1296,
        "complete": 1,
        "probed": 1,
    }

    async def no_index(*, force=False):
        return None

    async def rows(**_kwargs):
        return [row]

    dvr._index_segments = no_index
    dvr.list_segments = rows
    result = await dvr.footage_analysis_samples(since, until, sample_count=8)

    assert result["sample_count"] == 8
    assert result["sampled_at"][0] == "2026-08-30T06:00:00Z"
    assert result["sampled_at"][-1] == "2026-08-30T06:01:00Z"
    assert result["source_segments"] == [{
        "id": 1,
        "started_at": "2026-08-30T06:00:00Z",
        "ended_at": "2026-08-30T06:05:00Z",
        "codec": "HEVC",
        "width": 2304,
        "height": 1296,
    }]
    assert result["contact_sheet"].startswith(b"\xff\xd8\xff")
    assert len(observed_commands) == 8
    assert all("-nostdin" in command and "-xerror" in command for command in observed_commands)


@pytest.mark.asyncio
async def test_footage_analysis_rejects_a_broad_interval_before_touching_dvr_storage(tmp_path: Path):
    dvr = _dvr(tmp_path)
    since = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="three-minute"):
        await dvr.footage_analysis_samples(
            since,
            since + timedelta(seconds=camera_dvr.MAX_FOOTAGE_ANALYSIS_DURATION_SECONDS + 1),
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


def test_onvif_fast_pull_cycle_is_paced_but_long_poll_is_not():
    assert camera_dvr.CameraDVR._pull_cycle_delay(10.0, now=10.1) == pytest.approx(0.9)
    assert camera_dvr.CameraDVR._pull_cycle_delay(10.0, now=11.2) == 0.0


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
    unknown = camera_dvr.CameraDVR._playback_codec_args(None)
    assert "libx264" in hevc
    assert "libx264" in unknown
    assert not any("cuda" in value.casefold() or "nvenc" in value.casefold() for value in hevc)
    # A full-resolution software transcode measured ~30s/segment on real
    # hardware -- long enough to read as playback stopping. Downscaled,
    # it measured ~5.6s; that is what actually keeps this off the GPU
    # (already saturated by the model) while staying human-patience-fast.
    assert "-vf" in hevc
    assert hevc[hevc.index("-vf") + 1].startswith("scale=")


def test_segment_probe_reads_actual_bitstream_metadata_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    segment = tmp_path / "recording.mkv"
    segment.write_bytes(b"video")
    camera = SimpleNamespace(_require_ffmpeg=lambda: ffmpeg)
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path / "dvr", required_drive=None)
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"streams":[{"codec_name":"hevc","width":2304,"height":1296}]}',
        )

    monkeypatch.setattr(camera_dvr.subprocess, "run", run)

    assert dvr._probe_segment_sync(segment) == ("HEVC", 2304, 1296)
    assert observed["command"][-1] == str(segment)
    assert "-select_streams" in observed["command"]
    assert not any(
        token in " ".join(observed["command"]).casefold()
        for token in ("cuda", "nvenc", "libx264")
    )
    assert observed["kwargs"]["timeout"] == camera_dvr.SEGMENT_PROBE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_completed_segment_probe_corrects_advertised_codec_and_rebuilds_stale_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr._ensure_storage_sync()
    session = "20260829T120000000000Z"
    dvr._current_session_prefix = session
    first = dvr.recordings_dir / f"{session}-000000.mkv"
    first.write_bytes(b"first")
    dvr._process = SimpleNamespace(returncode=None)
    dvr._profile = camera_dvr.RecordingProfile(
        "main", "mainStream", "H264", 2304, 1296
    )

    await dvr._index_segments(force=True)
    initial = (await dvr.list_segments(limit=10))[0]
    stale = dvr.playback_dir / f"segment-{initial['id']}.mp4"
    stale.write_bytes(b"stale-remux")
    (dvr.recordings_dir / f"{session}-000001.mkv").write_bytes(b"second")
    monkeypatch.setattr(
        dvr, "_probe_segment_sync", lambda _path: ("HEVC", 2304, 1296)
    )

    await dvr._index_segments(force=True)
    rows = sorted(await dvr.list_segments(limit=10), key=lambda row: row["started_at"])

    assert rows[0]["complete"] == 1
    assert rows[0]["probed"] == 1
    assert rows[0]["codec"] == "HEVC"
    assert rows[1]["complete"] == 0
    assert rows[1]["probed"] == 0
    assert rows[1]["codec"] is None
    assert dvr._profile is not None and dvr._profile.encoding == "H264"
    assert dvr._actual_profile is not None and dvr._actual_profile.encoding == "HEVC"
    assert stale.read_bytes() == b"stale-remux"

    observed = {}

    async def write_playback(target, args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        target.write_bytes(b"fresh-h264")
        return target

    monkeypatch.setattr(dvr, "_write_playback_target", write_playback)
    result = await dvr.segment_playback(rows[0]["id"])

    assert result.read_bytes() == b"fresh-h264"
    assert "libx264" in observed["args"]
    assert observed["kwargs"]["artifact_kind"] == "segment"
    assert observed["kwargs"]["source_key"] == dvr._segment_source_key(rows[0])


@pytest.mark.asyncio
async def test_segment_playback_reindexes_changed_source_before_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    source = dvr.recordings_dir / "20260829-120000.mkv"
    source.write_bytes(b"original-h264")
    probe_codec = {"value": "H264"}
    monkeypatch.setattr(
        dvr,
        "_probe_segment_sync",
        lambda _path: (probe_codec["value"], 1920, 1080),
    )
    await dvr._index_segments(force=True)
    original = (await dvr.list_segments(limit=1))[0]
    target = dvr.playback_dir / f"segment-{original['id']}.mp4"
    target.write_bytes(b"cached-old")
    dvr._register_artifact_sync(
        target.name, "segment", dvr._segment_source_key(original)
    )
    probe_codec["value"] = "HEVC"
    source.write_bytes(b"replacement-hevc-with-new-size")
    observed = {}

    async def rebuild(target_path, args, **kwargs):
        observed["args"] = args
        target_path.write_bytes(b"rebuilt-h264")
        return target_path

    monkeypatch.setattr(dvr, "_write_playback_target", rebuild)
    result = await dvr.segment_playback(original["id"])

    refreshed = await dvr.get_segment(original["id"])
    assert refreshed is not None and refreshed["codec"] == "HEVC"
    assert result.read_bytes() == b"rebuilt-h264"
    assert "libx264" in observed["args"]


@pytest.mark.asyncio
async def test_range_playback_reindexes_changed_source_before_cache_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    source = dvr.recordings_dir / "20260829-120000.mkv"
    source.write_bytes(b"original-h264")
    probe_codec = {"value": "H264"}
    monkeypatch.setattr(
        dvr,
        "_probe_segment_sync",
        lambda _path: (probe_codec["value"], 1920, 1080),
    )
    await dvr._index_segments(force=True)
    original = (await dvr.list_segments(limit=1))[0]
    since = datetime.fromisoformat(original["started_at"].replace("Z", "+00:00"))
    until = since + timedelta(seconds=10)
    target = dvr.playback_dir / "range-test.mp4"
    target.write_bytes(b"cached-old")
    old_source_key = dvr._validate_range_coverage([original], since, until)
    dvr._register_artifact_sync(target.name, "range", old_source_key)

    probe_codec["value"] = "HEVC"
    source.write_bytes(b"replacement-hevc-with-new-size")
    observed = {}

    async def rebuild(target_path, args, **kwargs):
        observed["args"] = args
        target_path.write_bytes(b"rebuilt-h264")
        return target_path

    monkeypatch.setattr(dvr, "_write_playback_target", rebuild)
    result = await dvr.range_clip(since, until, cache_name="range-test")

    refreshed = (await dvr.list_segments(limit=1))[0]
    assert refreshed["codec"] == "HEVC"
    assert dvr._validate_range_coverage([refreshed], since, until) != old_source_key
    assert result.read_bytes() == b"rebuilt-h264"
    assert "libx264" in observed["args"]


def test_legacy_index_migration_preserves_rows_but_clears_guessed_metadata(tmp_path: Path):
    root = tmp_path / "dvr"
    root.mkdir()
    db = root / "dvr.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                bytes INTEGER NOT NULL DEFAULT 0,
                codec TEXT,
                width INTEGER,
                height INTEGER,
                complete INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO segments
                (id, filename, started_at, ended_at, bytes, codec, width, height,
                 complete, indexed_at)
            VALUES (7, '20260829-120000.mkv', '2026-08-29T12:00:00Z',
                    '2026-08-29T12:05:00Z', 10, 'H264', 1920, 1080, 1,
                    '2026-08-29T12:05:00Z')
            """
        )
        conn.commit()
    finally:
        conn.close()

    dvr = camera_dvr.CameraDVR(SimpleNamespace(), root=root, required_drive=None)
    dvr._ensure_storage_sync()
    migrated = dvr._conn()
    try:
        row = dict(migrated.execute("SELECT * FROM segments WHERE id=7").fetchone())
    finally:
        migrated.close()

    assert row["filename"] == "20260829-120000.mkv"
    assert row["probed"] == 0
    assert row["codec"] is None
    assert row["width"] is None
    assert row["height"] is None
    assert row["source_mtime_ns"] == 0


@pytest.mark.asyncio
async def test_new_recorder_without_a_current_file_does_not_reopen_old_footage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    old = dvr.recordings_dir / "20260829T110000000000Z-000000.mkv"
    old.write_bytes(b"old")
    dvr._process = SimpleNamespace(returncode=None)
    dvr._current_session_prefix = "20260829T120000000000Z"
    dvr._profile = camera_dvr.RecordingProfile(
        "main", "mainStream", "H264", 2304, 1296
    )
    monkeypatch.setattr(
        dvr, "_probe_segment_sync", lambda _path: ("HEVC", 2304, 1296)
    )

    await dvr._index_segments(force=True)
    row = (await dvr.list_segments(limit=1))[0]

    assert row["complete"] == 1
    assert row["probed"] == 1
    assert dvr._actual_profile is None


@pytest.mark.asyncio
async def test_future_dated_archive_never_hides_current_session_active_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    current_session = "20260829T120000000000Z"
    current = dvr.recordings_dir / f"{current_session}-000000.mkv"
    future_archive = dvr.recordings_dir / "20260829T130000000000Z-000000.mkv"
    current.write_bytes(b"active")
    future_archive.write_bytes(b"restored-future")
    dvr._process = SimpleNamespace(returncode=None)
    dvr._current_session_prefix = current_session
    monkeypatch.setattr(
        dvr, "_probe_segment_sync", lambda _path: ("HEVC", 2304, 1296)
    )

    await dvr._index_segments(force=True)
    rows = {row["filename"]: row for row in await dvr.list_segments(limit=10)}

    assert rows[current.name]["complete"] == 0
    assert rows[current.name]["probed"] == 0
    assert rows[future_archive.name]["complete"] == 1
    assert rows[future_archive.name]["probed"] == 1


@pytest.mark.asyncio
async def test_segment_probe_backfill_is_bounded_newest_first(tmp_path: Path, monkeypatch):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    for minute in range(6):
        (dvr.recordings_dir / f"20260829-{120000 + minute * 100:06d}.mkv").write_bytes(
            f"segment-{minute}".encode()
        )
    calls = []

    def probe(path):
        calls.append(path.name)
        return ("H264", 1920, 1080)

    monkeypatch.setattr(dvr, "_probe_segment_sync", probe)

    await dvr._index_segments(force=True)
    assert len(calls) == camera_dvr.MAX_SEGMENT_PROBES_PER_INDEX
    assert set(calls) == {
        "20260829-120500.mkv",
        "20260829-120400.mkv",
        "20260829-120300.mkv",
        "20260829-120200.mkv",
    }

    await dvr._index_segments(force=True)
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_changed_segment_discards_probe_result_and_retries_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    segment = dvr.recordings_dir / "20260829-120000.mkv"
    segment.write_bytes(b"before")

    def racing_probe(path):
        path.write_bytes(b"changed-after-probe-start")
        return ("HEVC", 2304, 1296)

    monkeypatch.setattr(dvr, "_probe_segment_sync", racing_probe)
    await dvr._index_segments(force=True)
    row = (await dvr.list_segments(limit=1))[0]

    assert row["complete"] == 1
    assert row["probed"] == 0
    assert row["codec"] is None


@pytest.mark.asyncio
async def test_ffprobe_never_holds_database_lock(tmp_path: Path, monkeypatch):
    dvr = _dvr(tmp_path)
    dvr.recordings_dir.mkdir(parents=True)
    (dvr.recordings_dir / "20260829-120000.mkv").write_bytes(b"complete")
    started = threading.Event()
    release = threading.Event()

    def blocked_probe(_path):
        started.set()
        release.wait(timeout=2)
        return ("H264", 1920, 1080)

    monkeypatch.setattr(dvr, "_probe_segment_sync", blocked_probe)
    task = asyncio.create_task(dvr._index_segments(force=True))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.wait_for(dvr._db_lock.acquire(), timeout=0.2)
    dvr._db_lock.release()
    release.set()
    await task


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
    session = "20260829T120000000000Z"
    (dvr.recordings_dir / f"{session}-000000.mkv").write_bytes(b"open")
    dvr._process = SimpleNamespace(returncode=None)
    dvr._current_session_prefix = session
    await dvr._index_segments(force=True)
    segment = (await dvr.list_segments(limit=1))[0]

    with pytest.raises(FileNotFoundError, match="still being written"):
        await dvr.segment_playback(segment["id"])


@pytest.mark.asyncio
async def test_event_clip_retries_exact_burst_when_only_padding_crosses_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dvr = _dvr(tmp_path)
    events = [
        {"captured_at": "2026-08-29 22:04:53"},
        {"captured_at": "2026-08-29 22:05:12"},
    ]
    store = SimpleNamespace(list_camera_events_by_burst=lambda _burst_id: events)
    calls = []

    async def range_clip(since, until, *, cache_name):
        calls.append((since, until, cache_name))
        if len(calls) == 1:
            raise FileNotFoundError("padding crossed a recorder restart")
        return tmp_path / "motion-23.mp4"

    monkeypatch.setattr(dvr, "range_clip", range_clip)

    result = await dvr.event_clip(store, 23)

    assert result.name == "motion-23.mp4"
    assert len(calls) == 2
    assert calls[0][0].isoformat() == "2026-08-29T22:04:43+00:00"
    assert calls[0][1].isoformat() == "2026-08-29T22:05:32+00:00"
    assert calls[1][0].isoformat() == "2026-08-29T22:04:53+00:00"
    assert calls[1][1].isoformat() == "2026-08-29T22:05:12+00:00"
    assert calls[1][2] == "motion-23"


@pytest.mark.asyncio
async def test_retention_clears_cache_then_deletes_oldest_complete_only(tmp_path: Path):
    dvr = _dvr(tmp_path, reserve_bytes=256 * 1024 * 1024)
    dvr.recordings_dir.mkdir(parents=True)
    session = "20260829T120000000000Z"
    oldest = dvr.recordings_dir / f"{session}-000000.mkv"
    newest = dvr.recordings_dir / f"{session}-000001.mkv"
    oldest.write_bytes(b"old")
    newest.write_bytes(b"active")
    dvr.playback_dir.mkdir(parents=True)
    cached = dvr.playback_dir / "motion-1.mp4"
    cached.write_bytes(b"cache")
    dvr._process = SimpleNamespace(returncode=None)
    dvr._current_session_prefix = session
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
    client = TestClient(app)
    assert client.get("/dvr").status_code == 403
    assert client.post("/dvr/api/live/sessions").status_code == 403
    assert client.get(
        "/dvr/api/live/sessions/watch_session_12345678/stream.mjpg"
    ).status_code == 403
    assert client.delete(
        "/dvr/api/live/sessions/watch_session_12345678"
    ).status_code == 403


def test_dvr_trailing_slash_is_the_owner_dvr_not_chat_spa(tmp_path: Path):
    app, _dvr_instance = _router_app(tmp_path, {"role": "owner"})
    response = TestClient(app).get("/dvr/")
    assert response.status_code == 200
    assert "X DVR" in response.text


def test_dvr_live_watch_is_owner_bound_exact_origin_and_conversation_free(
    tmp_path: Path,
):
    calls = []

    class WatchCamera:
        ffmpeg_path = None

        async def create_watch_session(self, *, owner_id):
            calls.append(("create", owner_id))
            return {
                "ok": True,
                "status": "ready",
                "session_id": "watch_session_12345678",
                "stream_url": "/api/unused",
                "label": "Driveway",
                "expires_at": "2026-08-30T00:00:00Z",
            }

        async def stream(self, *, session_id, owner_id):
            calls.append(("stream", session_id, owner_id))

            async def chunks():
                yield b"--xomni\r\nContent-Type: image/jpeg\r\n\r\nframe\r\n"

            return chunks()

        async def delete_session(self, *, session_id, owner_id):
            calls.append(("delete", session_id, owner_id))
            return {"ok": True, "status": "stopped", "session_id": session_id}

    class Store:
        def __init__(self):
            self.audits = []

        def audit(self, event, payload):
            self.audits.append((event, payload))

    async def require_session():
        return {"role": "owner", "token_hash": "owner-token"}

    camera = WatchCamera()
    dvr = camera_dvr.CameraDVR(camera, root=tmp_path / "dvr", required_drive=None)
    store = Store()
    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
        local_origin="http://omega.test",
        public_origin="",
    )
    app = FastAPI()
    app.include_router(camera_dvr.create_router(settings, store, require_session, dvr))
    client = TestClient(app)

    assert client.post("/dvr/api/live/sessions").status_code == 403
    assert client.post(
        "/dvr/api/live/sessions", headers={"Origin": "https://attacker.test"}
    ).status_code == 403

    created = client.post(
        "/dvr/api/live/sessions", headers={"Origin": "http://omega.test"}
    )
    assert created.status_code == 200
    assert created.json() == {
        "ok": True,
        "status": "ready",
        "session_id": "watch_session_12345678",
        "stream_url": "/dvr/api/live/sessions/watch_session_12345678/stream.mjpg",
        "label": "Driveway",
        "expires_at": "2026-08-30T00:00:00Z",
        "streaming": False,
    }
    assert "conversation_id" not in created.json()
    assert not {
        "host", "username", "password", "rtsp_uri", "stream_uri"
    }.intersection(created.json())

    streamed = client.get(created.json()["stream_url"])
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert "no-store" in streamed.headers["cache-control"]
    assert streamed.headers["x-content-type-options"] == "nosniff"
    stopped = client.delete(
        "/dvr/api/live/sessions/watch_session_12345678",
        headers={"Origin": "http://omega.test"},
    )
    assert stopped.status_code == 200
    assert calls == [
        ("create", "session:owner-token"),
        ("stream", "watch_session_12345678", "session:owner-token"),
        ("delete", "watch_session_12345678", "session:owner-token"),
    ]
    assert [event for event, _payload in store.audits] == [
        "standalone_dvr_live_session_started",
        "standalone_dvr_live_session_stopped",
    ]


def test_dvr_camera_auth_failure_does_not_log_out_the_owner(tmp_path: Path):
    class RejectedCamera:
        ffmpeg_path = None

        async def create_watch_session(self, *, owner_id):
            raise camera_dvr.exterior_camera_svc.ExteriorCameraAuthError(
                "camera rejected credentials"
            )

    async def require_session():
        return {"role": "owner", "token_hash": "owner-token"}

    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
        local_origin="http://omega.test",
        public_origin="",
    )
    dvr = camera_dvr.CameraDVR(
        RejectedCamera(), root=tmp_path / "dvr", required_drive=None
    )
    app = FastAPI()
    app.include_router(
        camera_dvr.create_router(settings, SimpleNamespace(), require_session, dvr)
    )

    response = TestClient(app).post(
        "/dvr/api/live/sessions", headers={"Origin": "http://omega.test"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Exterior camera credentials were rejected."


def test_dvr_live_start_disconnect_releases_pending_session_without_audit(
    tmp_path: Path,
):
    calls = []

    class WatchCamera:
        ffmpeg_path = None

        async def create_watch_session(self, *, owner_id):
            calls.append(("create", owner_id))
            return {
                "session_id": "watch_session_12345678",
                "status": "ready",
                "label": "Driveway",
            }

        async def delete_session(self, *, session_id, owner_id):
            calls.append(("delete", session_id, owner_id))
            return {"ok": True, "status": "stopped"}

    class Store:
        def __init__(self):
            self.audits = []

        def audit(self, event, payload):
            self.audits.append((event, payload))

    async def require_session():
        return {"role": "owner", "token_hash": "owner-token"}

    store = Store()
    settings = SimpleNamespace(
        root=Path(__file__).resolve().parents[1],
        camera_snapshot_dir=tmp_path / "snapshots",
        local_origin="http://omega.test",
        public_origin="",
    )
    dvr = camera_dvr.CameraDVR(
        WatchCamera(), root=tmp_path / "dvr", required_drive=None
    )
    app = FastAPI()
    app.include_router(camera_dvr.create_router(settings, store, require_session, dvr))
    inbound = iter(
        [
            {"type": "http.request", "body": b"", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    outbound = []

    async def receive():
        return next(inbound)

    async def send(message):
        outbound.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/dvr/api/live/sessions",
        "raw_path": b"/dvr/api/live/sessions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"origin", b"http://omega.test"), (b"content-length", b"0")],
        "client": ("127.0.0.1", 41000),
        "server": ("omega.test", 80),
    }

    asyncio.run(app(scope, receive, send))

    start = next(message for message in outbound if message["type"] == "http.response.start")
    assert start["status"] == 499
    assert calls == [
        ("create", "session:owner-token"),
        ("delete", "watch_session_12345678", "session:owner-token"),
    ]
    assert store.audits == []


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
