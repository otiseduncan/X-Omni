"""Bounded webcam-frame validation and native Omni vision messages.

The browser owns camera permission and capture.  Core accepts one in-memory
still frame, validates it as an actual supported image, and builds the local
``image_url`` content part understood by the active llama.cpp Omni worker.
Raw image bytes and data URLs are never returned for persistence.
"""

from __future__ import annotations

import base64
import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_CAMERA_FRAME_BYTES = 4 * 1024 * 1024
MAX_CAMERA_PIXELS = 16_000_000
MAX_CAMERA_EDGE = 8_192
MAX_CAMERA_PROMPT_CHARS = 1_000
MAX_CAMERA_PROMPT_HEADER_CHARS = 8_192
VISION_COMPLETION_TOKENS = 700
VISION_TIMEOUT_SECONDS = 100.0

_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}


@dataclass(frozen=True)
class CameraFrame:
    raw: bytes
    mime: str
    width: int
    height: int
    sha256: str

    @property
    def byte_count(self) -> int:
        return len(self.raw)


def camera_prompt(value: object) -> str:
    prompt = str(value or "").strip()
    if not prompt:
        prompt = "Describe what is visible in this camera frame."
    if len(prompt) > MAX_CAMERA_PROMPT_CHARS:
        raise ValueError(
            f"Camera request exceeds the {MAX_CAMERA_PROMPT_CHARS:,}-character limit."
        )
    return prompt


def decode_camera_prompt_header(value: object) -> str:
    """Decode a UTF-8 prompt from a bounded URL-safe base64 header.

    Browser header values cannot reliably carry arbitrary Unicode.  Encoding
    the prompt also keeps it out of request URLs and access logs.
    """

    encoded = str(value or "").strip()
    if not encoded:
        raise ValueError("X-XOmni-Camera-Prompt-B64 is required.")
    if len(encoded) > MAX_CAMERA_PROMPT_HEADER_CHARS:
        raise ValueError("Encoded camera prompt header is too large.")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        decoded = raw.decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Camera prompt header is not valid URL-safe base64 UTF-8.") from exc
    return camera_prompt(decoded)


def make_camera_request():
    """Return a model tool that requests UI capture without opening a camera."""

    def camera_request(args: dict) -> dict:
        prompt = camera_prompt(args.get("prompt"))
        return {
            "ok": True,
            "status": "awaiting_capture",
            "prompt": prompt,
            "camera_opened": False,
            "capture_received": False,
            "message": (
                "Camera access has not started. Use the inline controls to start the "
                "live preview, analyze a current frame, and stop the camera."
            ),
        }

    return camera_request


def _magic_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_camera_frame(raw: bytes, declared_mime: str) -> CameraFrame:
    """Fully decode one bounded JPEG/PNG/WebP and return safe metadata."""

    if not raw:
        raise ValueError("Empty camera frame.")
    if len(raw) > MAX_CAMERA_FRAME_BYTES:
        raise ValueError(
            f"Camera frame exceeds the {MAX_CAMERA_FRAME_BYTES // (1024 * 1024)} MiB limit."
        )

    declared = str(declared_mime or "").partition(";")[0].strip().casefold()
    magic_mime = _magic_mime(raw)
    if magic_mime is None:
        raise ValueError("Camera frame must be a JPEG, PNG, or WebP image.")
    if declared not in set(_FORMAT_TO_MIME.values()):
        raise ValueError("Camera frame MIME type must be image/jpeg, image/png, or image/webp.")
    if declared != magic_mime:
        raise ValueError("Camera frame MIME type does not match its file signature.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                detected_mime = _FORMAT_TO_MIME.get(str(image.format or "").upper())
                width, height = image.size
                if detected_mime != magic_mime:
                    raise ValueError("Camera frame decoder format does not match its signature.")
                if width <= 0 or height <= 0:
                    raise ValueError("Camera frame has invalid dimensions.")
                if width > MAX_CAMERA_EDGE or height > MAX_CAMERA_EDGE:
                    raise ValueError(
                        f"Camera frame edge exceeds the {MAX_CAMERA_EDGE:,}-pixel limit."
                    )
                if width * height > MAX_CAMERA_PIXELS:
                    raise ValueError(
                        f"Camera frame exceeds the {MAX_CAMERA_PIXELS:,}-pixel limit."
                    )
                if int(getattr(image, "n_frames", 1)) != 1:
                    raise ValueError("Camera upload must contain exactly one still frame.")
                # Loading forces actual pixel decoding.  Header-only spoofed or
                # truncated inputs fail here before reaching the model worker.
                image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("Camera frame could not be decoded safely.") from exc

    return CameraFrame(
        raw=raw,
        mime=magic_mime,
        width=int(width),
        height=int(height),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def vision_messages(frame: CameraFrame, prompt: str) -> list[dict]:
    encoded = base64.b64encode(frame.raw).decode("ascii")
    return [
        {
            "role": "system",
            "content": (
                "Analyze exactly one operator-provided still camera frame. Describe only "
                "what the pixels support. Do not imply that you opened the camera, watched "
                "a live feed, identified a person, or observed anything outside this frame. "
                "If the image is unclear, say so plainly."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": camera_prompt(prompt)},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{frame.mime};base64,{encoded}"},
                },
            ],
        },
    ]


def observation_artifact(
    frame: CameraFrame,
    description: str,
    *,
    source: str = "browser_camera_still",
    camera_source_id: str | None = None,
    camera_label: str | None = None,
    capture_transport: str | None = None,
) -> dict:
    """Build metadata-only provenance for one explicitly submitted still.

    The route maps the sole supported remote source identifier to the stored
    service label.  No client-provided label or URL reaches this function.
    """

    if source not in {"browser_camera_still", "exterior_camera_still"}:
        raise ValueError("Unknown camera observation source.")
    provenance: dict[str, object] = {"source": source}
    if source == "exterior_camera_still":
        if camera_source_id != "exterior":
            raise ValueError("Exterior camera observation source is invalid.")
        label = str(camera_label or "").strip()
        if not label or len(label) > 80:
            raise ValueError("Exterior camera observation label is invalid.")
        provenance.update(
            camera_source_id="exterior",
            camera_label=label,
            capture_transport=(
                "server_mjpeg_frame"
                if capture_transport == "server_mjpeg_frame"
                else None
            ),
        )
        if provenance["capture_transport"] is None:
            raise ValueError("Exterior camera capture transport is invalid.")
    return {
        "type": "camera_observation",
        "data": {
            "ok": True,
            "status": "analyzed",
            **provenance,
            "mime": frame.mime,
            "bytes": frame.byte_count,
            "sha256": frame.sha256,
            "width": frame.width,
            "height": frame.height,
            "description": str(description or "").strip(),
            "raw_frame_persisted": False,
            "live_feed": False,
        },
    }
