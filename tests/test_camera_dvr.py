from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from core.services import camera_dvr


def test_recording_profile_prefers_highest_resolution_h264():
    profiles = [
        SimpleNamespace(token="sub", name="Sub", encoding="H264", width=640, height=360, ordinal=1),
        SimpleNamespace(token="main", name="Main", encoding="H264", width=2560, height=1440, ordinal=0),
        SimpleNamespace(token="hevc", name="Main H265", encoding="H265", width=3840, height=2160, ordinal=2),
    ]
    selected = camera_dvr.CameraDVR._select_recording_profile(profiles)
    assert selected.token == "main"
    assert selected.encoding == "H264"
    assert (selected.width, selected.height) == (2560, 1440)


def test_recording_profile_uses_h265_when_h264_is_unavailable():
    profiles = [
        SimpleNamespace(token="a", name="Sub", encoding="H265", width=640, height=360, ordinal=1),
        SimpleNamespace(token="b", name="Main", encoding="HEVC", width=1920, height=1080, ordinal=0),
    ]
    selected = camera_dvr.CameraDVR._select_recording_profile(profiles)
    assert selected.token == "b"
    assert selected.width == 1920


def _notification(topic: str, name: str, value: str) -> ET.Element:
    body = ET.Element("Body")
    notification = ET.SubElement(body, "NotificationMessage")
    ET.SubElement(notification, "Topic").text = topic
    message = ET.SubElement(notification, "Message")
    ET.SubElement(message, "SimpleItem", {"Name": name, "Value": value})
    return body


def test_motion_parser_accepts_xiongmai_style_motion_states():
    assert camera_dvr.CameraDVR.motion_states_from_body(
        _notification("tns1:VideoSource/MotionAlarm", "State", "true")
    ) == [True]
    assert camera_dvr.CameraDVR.motion_states_from_body(
        _notification("tns1:RuleEngine/CellMotionDetector/Motion", "IsMotion", "false")
    ) == [False]


def test_motion_parser_ignores_unrelated_boolean_events():
    assert camera_dvr.CameraDVR.motion_states_from_body(
        _notification("tns1:Device/Relay", "State", "true")
    ) == []


@pytest.mark.asyncio
async def test_segment_index_marks_only_newest_active_segment_open(tmp_path: Path):
    fake_camera = SimpleNamespace()
    dvr = camera_dvr.CameraDVR(fake_camera, root=tmp_path, reserve_bytes=256 * 1024 * 1024)
    dvr.recordings_dir.mkdir(parents=True)
    (dvr.recordings_dir / "20260829-120000.mkv").write_bytes(b"a" * 100)
    (dvr.recordings_dir / "20260829-120500.mkv").write_bytes(b"b" * 200)
    dvr._process = SimpleNamespace(returncode=None)
    dvr._profile = camera_dvr.RecordingProfile("x", "Main", "H264", 1920, 1080)

    await dvr._index_segments()
    rows = sorted(await dvr.list_segments(limit=10), key=lambda row: row["started_at"])
    assert len(rows) == 2
    assert rows[0]["complete"] == 1
    assert rows[1]["complete"] == 0
    assert rows[0]["codec"] == "H264"


def test_dvr_default_root_is_dedicated_e_drive_folder():
    assert str(camera_dvr.DEFAULT_DVR_ROOT).replace("\\", "/") == "E:/XOmni-DVR"
