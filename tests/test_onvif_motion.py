from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from core.services import exterior_camera as exterior_camera_svc
from core.services import onvif_motion


def _notification_body(*notifications: tuple[str, list[tuple[str, str]]]) -> ET.Element:
    body = ET.Element("Body")
    for topic, items in notifications:
        notification = ET.SubElement(body, "NotificationMessage")
        ET.SubElement(notification, "Topic").text = topic
        message = ET.SubElement(notification, "Message")
        for name, value in items:
            ET.SubElement(message, "SimpleItem", {"Name": name, "Value": value})
    return body


def test_motion_parser_preserves_batch_order_and_ignores_channel_values():
    body = _notification_body(
        ("tns1:VideoSource/MotionAlarm", [("Channel", "1"), ("State", "false")]),
        ("tns1:VideoAnalytics/Vehicle", [("Channel", "1"), ("State", "true")]),
        ("tns1:VideoSource/MotionAlarm", [("State", "idle")]),
    )

    assert onvif_motion.OnvifMotionWatcher.motion_states_from_body(body) == [False, True, False]


def test_motion_parser_ignores_unrelated_topics_and_names():
    unrelated = _notification_body(
        ("tns1:Device/Relay", [("Channel", "1"), ("State", "true")]),
        ("tns1:Device/CardReader", [("State", "true")]),
    )
    assert onvif_motion.OnvifMotionWatcher.motion_states_from_body(unrelated) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("trigger", True),
        ("triggered", True),
        ("true", True),
        ("1", True),
        ("clear", False),
        ("cleared", False),
        ("false", False),
        ("0", False),
        ("unrecognized", None),
        ("", None),
    ],
)
def test_parse_bool_covers_xiongmai_and_standard_onvif_vocabularies(value, expected):
    assert onvif_motion.OnvifMotionWatcher._parse_bool(value) is expected


def test_pinned_subscription_url_accepts_a_standard_onvif_path():
    url = onvif_motion.OnvifMotionWatcher._pinned_subscription_url(
        "http://192.0.2.200:8899/onvif/event_service/0", host="192.168.50.25"
    )
    assert url == "http://192.168.50.25:8899/onvif/event_service/0"


def test_pinned_subscription_url_accepts_the_xiongmai_pullpoint_path_shape():
    url = onvif_motion.OnvifMotionWatcher._pinned_subscription_url(
        "http://192.0.2.200:8899/event_service/0", host="192.168.50.25"
    )
    assert url == "http://192.168.50.25:8899/event_service/0"


def test_pinned_subscription_url_discards_the_advertised_host_not_just_the_port():
    # The camera-advertised address is untrusted; only its path/query survive
    # -- the netloc is always rebuilt from the host X Omni actually dialed.
    url = onvif_motion.OnvifMotionWatcher._pinned_subscription_url(
        "http://attacker.example:8899/onvif/event_service", host="192.168.50.25"
    )
    assert url == "http://192.168.50.25:8899/onvif/event_service"


@pytest.mark.parametrize(
    "malicious",
    [
        "https://192.0.2.200:8899/onvif/event_service",
        "http://user:pass@192.0.2.200:8899/onvif/event_service",
        "http://192.0.2.200:9999/onvif/event_service",
        "http://192.0.2.200:8899/not-onvif-shaped",
    ],
)
def test_pinned_subscription_url_rejects_a_bad_scheme_credentials_port_or_path(malicious):
    with pytest.raises(exterior_camera_svc.ExteriorCameraUnavailable):
        onvif_motion.OnvifMotionWatcher._pinned_subscription_url(malicious, host="192.168.50.25")


def test_pull_cycle_delay_caps_a_camera_that_ignores_the_long_poll_timeout():
    # A pull that returned instantly must still be throttled to the minimum
    # cycle so a misbehaving camera subscription can never become a busy loop.
    delay = onvif_motion.OnvifMotionWatcher._pull_cycle_delay(100.0, now=100.0)
    assert delay == pytest.approx(onvif_motion.ONVIF_MIN_PULL_CYCLE_SECONDS)


def test_pull_cycle_delay_is_zero_once_the_camera_already_took_long_enough():
    delay = onvif_motion.OnvifMotionWatcher._pull_cycle_delay(
        100.0, now=100.0 + onvif_motion.ONVIF_MIN_PULL_CYCLE_SECONDS + 1
    )
    assert delay == 0.0


def test_watcher_starts_unhealthy_and_exposes_mark_unhealthy():
    camera = object()
    watcher = onvif_motion.OnvifMotionWatcher(camera)
    assert watcher.events_healthy is False
    watcher._events_healthy = True
    watcher.mark_events_unhealthy()
    assert watcher.events_healthy is False
