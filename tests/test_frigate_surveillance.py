"""The camera capabilities X offers, and how they behave when Frigate is not there.

The tool names in here are the ones the model already knows. What these
tests protect is that their contract survived the move to Frigate, and that
an unreachable recorder produces an honest structured state instead of
either a crash or -- much worse -- a confident answer about what is
happening outside.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.services import camera_security, frigate_surveillance
from core.services.frigate_client import (
    FrigateAuthError,
    FrigateCameraNotFound,
    FrigateNoRecording,
    FrigateNotConfigured,
    FrigateState,
    FrigateUnavailable,
)

NOW = datetime(2026, 9, 13, 14, 30, tzinfo=timezone.utc)


class FakeClient:
    """Stands in for Frigate with whatever behaviour a test needs."""

    def __init__(self, **behaviour):
        self.base_url = "https://frigate.example:8971"
        self.camera = "exterior"
        self._behaviour = behaviour
        self.calls: list[str] = []

    def configured(self) -> bool:
        return self._behaviour.get("configured", True)

    def configuration_summary(self) -> dict:
        return {"base_url": self.base_url, "camera": self.camera}

    def _result(self, name, default=None):
        self.calls.append(name)
        value = self._behaviour.get(name, default)
        if isinstance(value, Exception):
            raise value
        return value

    async def health(self):
        return self._result(
            "health",
            {
                "state": FrigateState.AVAILABLE,
                "camera": "exterior",
                "camera_configured": True,
                "camera_running": True,
                "cameras": ["exterior"],
                "version": "0.15.0",
                "detail": None,
            },
        )

    async def review(self, **kwargs):
        return self._result("review", [])

    async def recording_spans(self, since, until):
        return self._result("recording_spans", [])

    async def clip_bytes(self, since, until):
        return self._result("clip_bytes", b"mp4")

    async def latest_frame(self, height=None):
        return self._result("latest_frame", b"\xff\xd8\xffjpeg")

    async def event_snapshot(self, event_id):
        return self._result("event_snapshot", b"\xff\xd8\xffjpeg")


def _surveillance(**behaviour):
    return frigate_surveillance.FrigateSurveillance(
        FakeClient(**behaviour), ffmpeg_path="ffmpeg"
    )


def _review_record(identifier="r1", start=NOW, seconds=45, objects=("person",)):
    return {
        "id": identifier,
        "severity": "alert",
        "start_time": start.timestamp(),
        "end_time": (start + timedelta(seconds=seconds)).timestamp(),
        "has_been_reviewed": False,
        "data": {"objects": list(objects), "zones": ["driveway"], "detections": ["d1"]},
    }


class FakeRouter:
    def __init__(self, caption: str):
        self.caption = caption

    def supports_vision(self) -> bool:
        return True

    async def ensure_capability(self, **_kwargs):
        return None


# ------------------------------------------------------------------ status


@pytest.mark.asyncio
async def test_status_reports_available_when_frigate_and_the_camera_are_live():
    status = await _surveillance().status()
    assert status["ok"] is True
    assert status["state"] == FrigateState.AVAILABLE
    # X never records; "recording" reports what Frigate is doing.
    assert status["recording"] is True


@pytest.mark.asyncio
async def test_status_never_raises_when_frigate_is_unreachable():
    status = await _surveillance(health=FrigateUnavailable("host is down")).status()
    assert status["ok"] is False
    assert status["state"] == FrigateState.UNAVAILABLE
    assert status["recording"] is False


@pytest.mark.asyncio
async def test_status_distinguishes_a_refused_credential_from_an_outage():
    status = await _surveillance(health=FrigateAuthError("rejected")).status()
    assert status["state"] == FrigateState.AUTHENTICATION_REQUIRED


@pytest.mark.asyncio
async def test_status_reports_not_configured_before_any_setup():
    status = await _surveillance(configured=False).status()
    assert status["state"] == FrigateState.NOT_CONFIGURED


# ------------------------------------------------------------ event history


@pytest.mark.asyncio
async def test_event_history_maps_frigate_review_into_the_existing_item_shape():
    service = _surveillance(review=[_review_record()])
    result = await camera_security.camera_event_history({}, surveillance=service)
    assert result["ok"] is True
    item = result["items"][0]
    # The fields the history card and the model already rely on.
    for key in (
        "id", "captured_at", "captured_at_local", "trigger",
        "person_detected", "vehicle_detected", "snapshot_url",
    ):
        assert key in item, key
    assert item["person_detected"] is True
    assert item["vehicle_detected"] is False
    assert item["snapshot_url"].startswith("/api/camera/event-snapshot.jpg?")
    assert result["frigate_url"] == "https://frigate.example:8971"


@pytest.mark.asyncio
async def test_a_vehicle_label_becomes_a_vehicle_detection():
    service = _surveillance(review=[_review_record(objects=("car",))])
    result = await camera_security.camera_event_history({}, surveillance=service)
    assert result["items"][0]["vehicle_detected"] is True
    assert result["items"][0]["person_detected"] is False


@pytest.mark.asyncio
async def test_an_empty_history_says_no_detections_without_implying_nothing_happened():
    result = await camera_security.camera_event_history({}, surveillance=_surveillance())
    assert result["ok"] is True
    assert result["items"] == []
    assert result["state"] == FrigateState.NO_EVENT_DATA
    # The distinction that keeps this from being a false reassurance.
    assert "not the same as nothing having happened" in result["note"]


@pytest.mark.asyncio
async def test_event_history_reports_an_outage_instead_of_an_empty_list():
    service = _surveillance(review=FrigateUnavailable("asleep"))
    result = await camera_security.camera_event_history({}, surveillance=service)
    assert result["ok"] is False
    assert result["state"] == FrigateState.UNAVAILABLE
    assert result["items"] == []
    # An outage must never read as "there were no detections".
    assert "no detections" not in result["error"].casefold()


@pytest.mark.asyncio
async def test_event_history_can_include_bounded_recording_coverage():
    service = _surveillance(
        review=[_review_record()],
        recording_spans=[
            {
                "started_at": NOW,
                "ended_at": NOW + timedelta(minutes=10),
                "duration_seconds": 600.0,
            }
        ],
    )
    result = await camera_security.camera_event_history(
        {"include_recordings": True}, surveillance=service
    )
    assert result["recordings"][0]["duration_seconds"] == 600.0


# ---------------------------------------------------------------- playback


@pytest.mark.asyncio
async def test_playback_returns_a_url_only_for_footage_frigate_actually_holds():
    service = _surveillance(
        review=[_review_record()],
        recording_spans=[
            {
                "started_at": NOW - timedelta(minutes=1),
                "ended_at": NOW + timedelta(minutes=5),
                "duration_seconds": 360.0,
            }
        ],
    )
    result = await camera_security.camera_motion_clip({}, surveillance=service)
    assert result["ok"] is True
    assert result["clip_url"].startswith("/api/camera/footage.mp4?since=")
    assert result["partial"] is False


@pytest.mark.asyncio
async def test_a_range_with_no_recording_says_so_rather_than_linking_nothing():
    service = _surveillance(review=[_review_record()], recording_spans=[])
    result = await camera_security.camera_motion_clip({}, surveillance=service)
    assert result["ok"] is False
    assert result["state"] == FrigateState.NO_RECORDING
    assert "clip_url" not in result


@pytest.mark.asyncio
async def test_partial_coverage_is_reported_as_partial():
    service = _surveillance(
        recording_spans=[
            {
                "started_at": NOW + timedelta(seconds=30),
                "ended_at": NOW + timedelta(seconds=90),
                "duration_seconds": 60.0,
            }
        ]
    )
    result = await camera_security.camera_motion_clip(
        {
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(seconds=120)).isoformat(),
        },
        surveillance=service,
    )
    assert result["ok"] is True
    assert result["partial"] is True


@pytest.mark.asyncio
async def test_an_over_long_playback_range_is_refused_with_its_limit():
    result = await camera_security.camera_motion_clip(
        {
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(hours=3)).isoformat(),
        },
        surveillance=_surveillance(),
    )
    assert result["ok"] is False
    assert result["state"] == FrigateState.INVALID_REQUEST
    assert "minutes" in result["error"]


@pytest.mark.asyncio
async def test_half_a_time_range_is_an_invalid_request():
    result = await camera_security.camera_motion_clip(
        {"since": NOW.isoformat()}, surveillance=_surveillance()
    )
    assert result["ok"] is False
    assert result["state"] == FrigateState.INVALID_REQUEST


# ---------------------------------------------------------------- analysis


@pytest.mark.asyncio
async def test_footage_analysis_is_refused_when_the_window_is_too_long():
    result = await camera_security.camera_footage_analyze(
        FakeRouter(""),
        {
            "analysis": True,
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(hours=1)).isoformat(),
        },
        surveillance=_surveillance(),
    )
    assert result["ok"] is False
    assert result["analysis_status"] == "range_too_broad"


@pytest.mark.asyncio
async def test_footage_analysis_reports_an_outage_without_concluding_anything(monkeypatch):
    service = _surveillance(clip_bytes=FrigateUnavailable("asleep"))
    result = await camera_security.camera_footage_analyze(
        FakeRouter(""),
        {
            "analysis": True,
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(seconds=60)).isoformat(),
        },
        surveillance=service,
    )
    assert result["ok"] is False
    assert result["analysis_status"] == FrigateState.UNAVAILABLE
    # No person/vehicle verdict may be present when nothing was looked at.
    assert "person_detected" not in result
    assert "vehicle_detected" not in result


@pytest.mark.asyncio
async def test_a_complete_analysis_keeps_its_evidence_contract(monkeypatch):
    async def fake_samples(self, since, until, *, sample_count=None):
        return {
            "analyzed_started_at": "2026-09-13T14:30:00Z",
            "analyzed_ended_at": "2026-09-13T14:31:00Z",
            "sample_count": 8,
            "sampled_at": ["2026-09-13T14:30:00Z"],
            "contact_sheet": b"\xff\xd8\xffsheet",
            "source_segments": ["exterior"],
        }

    monkeypatch.setattr(
        frigate_surveillance.FrigateSurveillance, "analysis_samples", fake_samples
    )

    class Frame:
        sha256 = "abc123"

    monkeypatch.setattr(
        camera_security.camera_svc, "validate_camera_frame", lambda raw, mime: Frame()
    )

    async def fake_caption(router, frame, prompt):
        return (
            "PERSON: yes\nVEHICLE: no\nVEHICLE_MOVEMENT: not_observed\n"
            "PERSON_INTERACTION: observed\nSUFFICIENCY: sufficient\n"
            "DESCRIPTION: Someone walks up the driveway.\n"
            "EVIDENCE: A figure appears in frame three and reaches the door by frame six."
        )

    monkeypatch.setattr(camera_security.camera_svc, "caption_frame", fake_caption)

    service = _surveillance(
        recording_spans=[
            {
                "started_at": NOW,
                "ended_at": NOW + timedelta(minutes=2),
                "duration_seconds": 120.0,
            }
        ]
    )
    result = await camera_security.camera_footage_analyze(
        FakeRouter(""),
        {
            "analysis": True,
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(seconds=60)).isoformat(),
        },
        surveillance=service,
    )
    assert result["ok"] is True
    assert result["analysis_status"] == "sufficient"
    assert result["person_detected"] is True
    assert result["vehicle_detected"] is False
    # A negative observation never becomes a boolean "it did not happen".
    assert result["vehicle_movement_observed"] is None
    assert result["person_interaction_observed"] is True
    assert result["contact_sheet_sha256"] == "abc123"
    assert result["clip_url"].startswith("/api/camera/footage.mp4?")


@pytest.mark.asyncio
async def test_an_unstructured_vision_reply_yields_no_conclusion(monkeypatch):
    async def fake_samples(self, since, until, *, sample_count=None):
        return {
            "analyzed_started_at": "x",
            "analyzed_ended_at": "y",
            "sample_count": 8,
            "sampled_at": [],
            "contact_sheet": b"sheet",
            "source_segments": ["exterior"],
        }

    monkeypatch.setattr(
        frigate_surveillance.FrigateSurveillance, "analysis_samples", fake_samples
    )

    class Frame:
        sha256 = "abc"

    monkeypatch.setattr(
        camera_security.camera_svc, "validate_camera_frame", lambda raw, mime: Frame()
    )

    async def fake_caption(router, frame, prompt):
        return "Looks like somebody was out there, probably."

    monkeypatch.setattr(camera_security.camera_svc, "caption_frame", fake_caption)

    result = await camera_security.camera_footage_analyze(
        FakeRouter(""),
        {
            "analysis": True,
            "since": NOW.isoformat(),
            "until": (NOW + timedelta(seconds=60)).isoformat(),
        },
        surveillance=_surveillance(),
    )
    assert result["ok"] is False
    assert result["analysis_status"] == "unstructured_vision_result"


# ----------------------------------------------------------------- stills


@pytest.mark.asyncio
async def test_looking_at_the_camera_uses_frigates_current_frame(monkeypatch):
    class Frame:
        sha256 = "def456"

    monkeypatch.setattr(
        camera_security.camera_svc, "validate_camera_frame", lambda raw, mime: Frame()
    )

    async def fake_caption(router, frame, prompt):
        return "PERSON: no\nVEHICLE: yes\nDESCRIPTION: A pickup truck sits in the driveway."

    monkeypatch.setattr(camera_security.camera_svc, "caption_frame", fake_caption)

    service = _surveillance()
    result = await camera_security.exterior_camera_look(
        FakeRouter(""), {}, surveillance=service
    )
    assert result["ok"] is True
    assert result["vehicle_detected"] is True
    assert result["person_detected"] is False
    assert result["camera_source_id"] == "exterior"
    assert result["live_frame_url"] == "/api/camera/latest.jpg"
    assert "latest_frame" in service.client.calls


@pytest.mark.asyncio
async def test_looking_at_an_unreachable_camera_never_describes_a_scene():
    service = _surveillance(latest_frame=FrigateUnavailable("host is asleep"))
    result = await camera_security.exterior_camera_look(
        FakeRouter(""), {}, surveillance=service
    )
    assert result["ok"] is False
    assert result["state"] == FrigateState.UNAVAILABLE
    # The one thing that must never happen: an invented observation.
    assert "caption" not in result
    assert "description" not in result


@pytest.mark.asyncio
async def test_a_snapshot_for_a_missing_event_is_reported_not_invented():
    service = _surveillance(review=[], event_snapshot=FrigateNoRecording("gone"))
    result = await camera_security.camera_snapshot_analyze(
        FakeRouter(""), {"event_id": "missing"}, surveillance=service
    )
    assert result["ok"] is False
    assert "caption" not in result


# ----------------------------------------------------------------- wording


def test_every_structured_state_has_an_honest_sentence():
    for state in (
        FrigateState.UNAVAILABLE,
        FrigateState.AUTHENTICATION_REQUIRED,
        FrigateState.NOT_CONFIGURED,
        FrigateState.CAMERA_UNAVAILABLE,
        FrigateState.NO_RECORDING,
        FrigateState.NO_EVENT_DATA,
    ):
        message = frigate_surveillance.state_message(state)
        assert message and message[0].isupper() and message.endswith(".")


def test_the_footage_url_carries_its_own_range():
    url = frigate_surveillance.footage_url(NOW, NOW + timedelta(seconds=60))
    assert url.startswith("/api/camera/footage.mp4?")
    assert "since=2026-09-13T14%3A30%3A00Z" in url
    assert "until=2026-09-13T14%3A31%3A00Z" in url
