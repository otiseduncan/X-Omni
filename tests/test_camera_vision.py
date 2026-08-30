import asyncio
import base64
import io
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from core.api.routes import create_router
from core.orchestrator import prompt as prompt_module
from core.orchestrator.loop import ARTIFACT_FOR_TOOL
from core.services import camera
from core.tools.registry import Registry, TOOL_SCHEMAS


def _jpeg(width=32, height=24, color=(12, 34, 56)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="JPEG")
    return output.getvalue()


def _camera_headers(prompt="What is here?", origin="http://127.0.0.1:8100"):
    encoded_prompt = base64.urlsafe_b64encode(prompt.encode("utf-8")).decode("ascii")
    return {
        "Origin": origin,
        "Content-Type": "image/jpeg",
        "X-XOmni-Conversation-ID": "7",
        "X-XOmni-Camera-Prompt-B64": encoded_prompt.rstrip("="),
    }


def test_camera_request_is_truthful_and_does_not_claim_capture():
    handler = camera.make_camera_request()
    result = handler({"prompt": "Tell me what is on my workbench."})
    assert result == {
        "ok": True,
        "status": "awaiting_capture",
        "prompt": "Tell me what is on my workbench.",
        "camera_opened": False,
        "capture_received": False,
        "message": (
            "Camera access has not started. Use the inline controls to start the "
            "live preview, analyze a current frame, and stop the camera."
        ),
    }


def test_camera_request_is_available_in_the_default_and_full_profile_without_prompt_routing():
    normal_registry = Registry("config/tools.yaml")
    normal_registry.register("camera_request", camera.make_camera_request())
    normal_advertised = {
        item["function"]["name"] for item in normal_registry.model_tools()
    }
    assert "camera_request" in normal_advertised

    registry = Registry("config/tools.yaml", profile="full")
    registry.register("camera_request", camera.make_camera_request())
    advertised = {
        item["function"]["name"]: item["function"]
        for item in registry.model_tools()
    }
    assert registry.tier("camera_request") == "read_only"
    assert "camera_request" in advertised
    assert ARTIFACT_FOR_TOOL["camera_request"] == "camera_request"

    class OmniRouter:
        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    system = prompt_module.system_prompt(OmniRouter())
    assert "camera_request" not in system
    schema_description = TOOL_SCHEMAS["camera_request"]["description"]
    assert "operator" in schema_description
    assert "Doesn't open the camera" in schema_description


def test_camera_frame_is_fully_decoded_and_metadata_only_artifact_is_built():
    raw = _jpeg(40, 30)
    frame = camera.validate_camera_frame(raw, "image/jpeg")
    assert (frame.width, frame.height, frame.mime) == (40, 30, "image/jpeg")
    assert frame.byte_count == len(raw)
    assert len(frame.sha256) == 64

    messages = camera.vision_messages(frame, "Describe this frame.")
    url = messages[1]["content"][1]["image_url"]["url"]
    assert url == "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")

    artifact = camera.observation_artifact(frame, "A dark blue test image.")
    assert artifact["data"]["raw_frame_persisted"] is False
    assert artifact["data"]["description"] == "A dark blue test image."
    assert "base64" not in str(artifact)
    assert url not in str(artifact)


def test_vision_messages_accepts_a_long_system_authored_prompt():
    # camera_prompt()'s MAX_CAMERA_PROMPT_CHARS bounds raw operator/model text
    # at the point it is first accepted (already enforced by every call site
    # that takes user input). vision_messages must not re-apply that same
    # bound to its own already-finalized prompt string -- a real regression:
    # camera_security's structured temporal-evidence instructions
    # (_FOOTAGE_ANALYSIS_PROMPT) are System-authored and legitimately longer
    # than any single operator request should be.
    raw = _jpeg(40, 30)
    frame = camera.validate_camera_frame(raw, "image/jpeg")
    long_prompt = "Reply in exactly these lines:\n" + ("PERSON: yes, no, or uncertain\n" * 40)
    assert len(long_prompt) > camera.MAX_CAMERA_PROMPT_CHARS
    messages = camera.vision_messages(frame, long_prompt)
    assert messages[1]["content"][0]["text"] == long_prompt.strip()

    assert camera.vision_messages(frame, "")[1]["content"][0]["text"] == (
        "Describe what is visible in this camera frame."
    )


@pytest.mark.parametrize(
    ("raw", "mime", "message"),
    [
        (b"not an image", "image/jpeg", "must be a JPEG"),
        (_jpeg(), "image/png", "does not match"),
        (_jpeg()[:24], "image/jpeg", "decoded safely"),
    ],
)
def test_camera_frame_rejects_untrusted_or_mismatched_images(raw, mime, message):
    with pytest.raises(ValueError, match=message):
        camera.validate_camera_frame(raw, mime)


def test_camera_frame_limits_bytes_and_prompt(monkeypatch):
    monkeypatch.setattr(camera, "MAX_CAMERA_FRAME_BYTES", 8)
    with pytest.raises(ValueError, match="exceeds"):
        camera.validate_camera_frame(_jpeg(), "image/jpeg")
    with pytest.raises(ValueError, match="character limit"):
        camera.camera_prompt("x" * (camera.MAX_CAMERA_PROMPT_CHARS + 1))
    prompt = "¿Qué ves aquí? 🔧"
    encoded = base64.urlsafe_b64encode(prompt.encode("utf-8")).decode("ascii").rstrip("=")
    assert camera.decode_camera_prompt_header(encoded) == prompt


class _Store:
    def __init__(self, exists=True):
        self.exists = exists
        self.saved = []
        self.audits = []

    def conversation_exists(self, conversation_id):
        return self.exists and conversation_id == 7

    def add_message(self, conversation_id, role, content, **kwargs):
        self.saved.append((conversation_id, role, content, kwargs))
        return 91

    def audit(self, event, detail):
        self.audits.append((event, detail))


class _Router:
    def __init__(self, vision=True):
        self.vision = vision
        self.active_name = "omni" if vision else "coder"
        self.ensure_calls = []

    def supports_vision(self):
        return self.vision

    async def ensure_capability(self, **kwargs):
        self.ensure_calls.append(kwargs)
        self.vision = True
        self.active_name = "omni"
        return {"from": "coder", "to": "omni", "startup_s": 17.2}


class _Registry:
    policy = {}
    roots = []
    _handlers = {}

    @staticmethod
    def tier(_name):
        return "blocked"

    @staticmethod
    def public_approval(record, receipt=None):
        return record


class _FakeModelClient:
    seen = []

    def __init__(self, router, **kwargs):
        self.router = router

    async def complete(self, messages, **kwargs):
        self.seen.append((messages, kwargs))
        return "A blue rectangle is visible in the captured frame."


def _app(store, router):
    async def session():
        return {"google_sub": "owner"}

    settings = SimpleNamespace(
        local_origin="http://127.0.0.1:8100",
        public_origin="",
    )
    app = FastAPI()
    app.include_router(create_router(settings, store, router, _Registry(), session))
    return app


def test_camera_analysis_route_uses_native_image_part_and_persists_metadata_only(monkeypatch):
    import core.models.client as model_client_module

    _FakeModelClient.seen = []
    monkeypatch.setattr(model_client_module, "ModelClient", _FakeModelClient)
    store = _Store()
    router = _Router(vision=False)
    response = TestClient(_app(store, router)).post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=_camera_headers(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["message_id"] == 91
    assert payload["artifact"]["type"] == "camera_observation"
    assert payload["artifact"]["data"]["description"].startswith("A blue")
    assert router.ensure_calls == [{"vision": True}]

    model_messages, options = _FakeModelClient.seen[0]
    assert model_messages[1]["content"][1]["type"] == "image_url"
    assert options["temperature"] == 0.0
    assert store.saved[0][0:3] == (
        7,
        "assistant",
        "",
    )
    saved_text = str(store.saved) + str(store.audits) + str(payload)
    assert "data:image" not in saved_text
    assert base64.b64encode(_jpeg()).decode("ascii") not in saved_text
    assert store.audits[0][0] == "camera_frame_analyzed"


def test_camera_analysis_route_rejects_unknown_conversation_and_cross_origin(monkeypatch):
    import core.models.client as model_client_module

    monkeypatch.setattr(model_client_module, "ModelClient", _FakeModelClient)
    store = _Store(exists=False)
    client = TestClient(_app(store, _Router()))
    unknown = client.post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=_camera_headers(),
    )
    assert unknown.status_code == 404

    store.exists = True
    missing_origin = client.post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers={key: value for key, value in _camera_headers().items() if key != "Origin"},
    )
    assert missing_origin.status_code == 403

    cross_origin = client.post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=_camera_headers(origin="https://evil.example"),
    )
    assert cross_origin.status_code == 403


def test_raw_camera_route_accepts_over_one_mib_without_multipart_spooling(monkeypatch):
    import core.models.client as model_client_module
    import starlette.formparsers as formparsers

    _FakeModelClient.seen = []
    monkeypatch.setattr(model_client_module, "ModelClient", _FakeModelClient)

    def no_spool(*_args, **_kwargs):
        raise AssertionError("multipart temporary-file spooling must not run")

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", no_spool)
    output = io.BytesIO()
    Image.frombytes("RGB", (1024, 1024), os.urandom(1024 * 1024 * 3)).save(
        output, format="JPEG", quality=95, subsampling=0
    )
    raw = output.getvalue()
    assert 1024 * 1024 < len(raw) < camera.MAX_CAMERA_FRAME_BYTES

    store = _Store()
    response = TestClient(_app(store, _Router())).post(
        "/api/vision/analyze",
        content=raw,
        headers=_camera_headers("Describe this large still."),
    )
    assert response.status_code == 200
    assert response.json()["artifact"]["data"]["bytes"] == len(raw)
    assert store.saved[0][3]["artifacts"][0]["data"]["raw_frame_persisted"] is False


def test_raw_camera_route_rejects_body_over_four_mib_before_inference(monkeypatch):
    import core.models.client as model_client_module

    _FakeModelClient.seen = []
    monkeypatch.setattr(model_client_module, "ModelClient", _FakeModelClient)
    store = _Store()
    response = TestClient(_app(store, _Router())).post(
        "/api/vision/analyze",
        content=b"x" * (camera.MAX_CAMERA_FRAME_BYTES + 1),
        headers=_camera_headers(),
    )
    assert response.status_code == 413
    assert store.saved == []
    assert _FakeModelClient.seen == []


def test_camera_model_timeout_persists_no_observation(monkeypatch):
    import core.models.client as model_client_module

    class SlowModelClient:
        def __init__(self, _router, **_kwargs):
            pass

        async def complete(self, _messages, **_kwargs):
            await asyncio.sleep(1)
            return "This must never be persisted."

    monkeypatch.setattr(model_client_module, "ModelClient", SlowModelClient)
    monkeypatch.setattr(camera, "VISION_TIMEOUT_SECONDS", 0.01)
    store = _Store()
    response = TestClient(_app(store, _Router())).post(
        "/api/vision/analyze",
        content=_jpeg(),
        headers=_camera_headers(),
    )
    assert response.status_code == 504
    assert response.json()["detail"] == (
        "Camera analysis timed out before an observation was saved."
    )
    assert store.saved == []
    assert [event for event, _detail in store.audits] == [
        "camera_frame_analysis_failed"
    ]
    assert store.audits[0][1]["error_type"] == "TimeoutError"
