"""Bounded local video creation from verified generated images.

Two deliberately distinct modes share one content-addressed MP4 store:

``exact_source_animation``
    The original, non-generative fixed ``hover_pulse`` FFmpeg treatment.

``image_to_video``
    Genuine Wan2.2 TI2V-5B diffusion through an owned ComfyUI runtime.  The
    conversation model and ComfyUI run sequentially under the router's
    exclusive GPU lease.  This mode is source-conditioned, not pixel-exact and
    not a reusable 3D-mesh generator.

Both modes accept only a verified source SHA.  Callers can never supply a file
path, executable, filter graph, ComfyUI node graph, model filename, or output
location.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import httpx

from ..models.router import ExternalWorkloadStartError, ModelRouter, WorkerSwapError
from .image_generation import (
    ComfyUIProvider,
    ImageGenerationError,
    RuntimeHandle,
    _await_cleanup,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_PROCESS_OUTPUT_BYTES = 256 * 1024
_PROFILE = "hover_pulse"
_CODEC = "h264"
_PIXEL_FORMAT = "yuv420p"
_MIME_TYPE = "video/mp4"
_RENDER_KIND = "deterministic_exact_source_animation"
_PROCEDURAL_MODE = "exact_source_animation"
_I2V_MODE = "image_to_video"
_I2V_PROVIDER = "comfyui-wan2.2-ti2v-5b-local"
_I2V_RENDER_KIND = "generative_image_to_video"
_I2V_MODEL_ID = "Wan2.2-TI2V-5B"
_I2V_MODEL_FILE = "wan2.2_ti2v_5B_fp16.safetensors"
_I2V_TEXT_ENCODER_FILE = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
_I2V_VAE_FILE = "wan2.2_vae.safetensors"
_I2V_OFFICIAL_ASSETS = {
    _I2V_MODEL_FILE: {
        "bytes": 9_999_658_848,
        "sha256": "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
    },
    _I2V_TEXT_ENCODER_FILE: {
        "bytes": 6_735_906_897,
        "sha256": "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
    },
    _I2V_VAE_FILE: {
        "bytes": 1_409_400_960,
        "sha256": "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
    },
}
_I2V_WIDTH = 704
_I2V_HEIGHT = 704
_I2V_STEPS = 20
_I2V_CFG = 5.0
_I2V_SHIFT = 8.0
_I2V_NEGATIVE_PROMPT = (
    "oversaturated, overexposed, static, motionless, blurred details, subtitles, "
    "watermark, text, low quality, JPEG artifacts, deformed geometry, distorted "
    "object, duplicate subject, morphing, unstable background, camera shake"
)
_DEFAULT_I2V_PROMPT = (
    "The subject moves independently with convincing three-dimensional depth and "
    "subtle natural rotation while its defining design remains consistent. The "
    "background stays stable. Smooth cinematic motion, realistic lighting, no cuts."
)
_SAFE_COMFY_OUTPUT_FOLDER = PurePosixPath("xomni_i2v")


class VideoGenerationError(RuntimeError):
    """Raised when a video request cannot be proved safe and complete."""


class WanLifecycleError(VideoGenerationError):
    """A real Wan file exists, but cleanup or model restoration did not pass."""

    def __init__(
        self,
        message: str,
        *,
        temporary_path: Path,
        lifecycle: dict[str, Any],
        model_assets: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.temporary_path = temporary_path
        self.lifecycle = lifecycle
        self.model_assets = model_assets


class WanExecutionError(VideoGenerationError):
    """Wan produced no verified file; retain exact lifecycle and submit truth."""

    def __init__(
        self,
        message: str,
        *,
        lifecycle: dict[str, Any],
        generation: dict[str, Any],
        stage: str = "generative_execution",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.lifecycle = lifecycle
        self.generation = generation
        self.stage = stage
        self.retryable = retryable


class WanCancellationError(asyncio.CancelledError):
    """Cancellation completed with receipt-grade Wan cleanup evidence."""

    def __init__(
        self,
        *,
        lifecycle: dict[str, Any],
        generation: dict[str, Any],
    ) -> None:
        super().__init__("Wan2.2 generation was cancelled after execution started.")
        self.lifecycle = lifecycle
        self.generation = generation
        self.receipt_result: Optional[dict[str, Any]] = None


def _confined_path(root: Path, value: Any, *, label: str) -> Path:
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise VideoGenerationError(f"{label} must remain inside the X Omni root.") from exc
    if resolved == root:
        raise VideoGenerationError(f"{label} cannot be the X Omni root.")
    return resolved


def _positive_int(raw: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VideoGenerationError(
            f"{key} must be an integer from {minimum} through {maximum}."
        )
    return value


@dataclass(frozen=True)
class VideoGenerationConfig:
    enabled: bool
    provider: str
    ffmpeg_path: Path
    ffprobe_path: Path
    source_dir: Path
    output_dir: Path
    default_duration_seconds: int
    fps: int
    timeout_seconds: int
    max_source_bytes: int
    max_output_bytes: int
    i2v_enabled: bool
    i2v_generation_timeout_seconds: int

    @classmethod
    def from_file(cls, config_path: Path, app_root: Path) -> "VideoGenerationConfig":
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VideoGenerationError("Video generation configuration is unreadable.") from exc
        if not isinstance(raw, dict):
            raise VideoGenerationError("Video generation configuration must be an object.")

        allowed = {
            "enabled",
            "provider",
            "ffmpeg_path",
            "ffprobe_path",
            "source_dir",
            "output_dir",
            "default_duration_seconds",
            "fps",
            "timeout_seconds",
            "max_source_bytes",
            "max_output_bytes",
            "image_to_video",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise VideoGenerationError(
                f"Unknown video generation setting: {sorted(unknown)[0]}."
            )

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise VideoGenerationError("enabled must be a boolean.")
        provider = str(raw.get("provider") or "").strip()
        if provider != "ffmpeg-exact-local":
            raise VideoGenerationError("Unsupported video generation provider.")

        root = app_root.resolve()
        ffmpeg_path = Path(str(raw.get("ffmpeg_path") or "")).expanduser().resolve()
        ffprobe_path = Path(str(raw.get("ffprobe_path") or "")).expanduser().resolve()
        if not str(raw.get("ffmpeg_path") or "").strip():
            raise VideoGenerationError("ffmpeg_path is required.")
        if not str(raw.get("ffprobe_path") or "").strip():
            raise VideoGenerationError("ffprobe_path is required.")

        source_dir = _confined_path(
            root,
            raw.get("source_dir") or "data/generated-images",
            label="source_dir",
        )
        output_dir = _confined_path(
            root,
            raw.get("output_dir") or "data/generated-videos",
            label="output_dir",
        )
        if source_dir == output_dir:
            raise VideoGenerationError("source_dir and output_dir must be different.")

        fps = _positive_int(raw, "fps", 24, 24, 24)
        i2v_raw = raw.get("image_to_video") or {}
        if not isinstance(i2v_raw, dict):
            raise VideoGenerationError("image_to_video must be an object.")
        unknown_i2v = set(i2v_raw) - {"enabled", "generation_timeout_seconds"}
        if unknown_i2v:
            raise VideoGenerationError(
                f"Unknown image_to_video setting: {sorted(unknown_i2v)[0]}."
            )
        i2v_enabled = i2v_raw.get("enabled", False)
        if not isinstance(i2v_enabled, bool):
            raise VideoGenerationError("image_to_video.enabled must be a boolean.")
        return cls(
            enabled=enabled,
            provider=provider,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            source_dir=source_dir,
            output_dir=output_dir,
            default_duration_seconds=_positive_int(
                raw, "default_duration_seconds", 10, 2, 10
            ),
            fps=fps,
            timeout_seconds=_positive_int(raw, "timeout_seconds", 300, 30, 900),
            max_source_bytes=_positive_int(
                raw, "max_source_bytes", 64 * 1024 * 1024, 1024, 256 * 1024 * 1024
            ),
            max_output_bytes=_positive_int(
                raw, "max_output_bytes", 128 * 1024 * 1024, 1024, 512 * 1024 * 1024
            ),
            i2v_enabled=i2v_enabled,
            i2v_generation_timeout_seconds=_positive_int(
                i2v_raw,
                "generation_timeout_seconds",
                7200,
                300,
                14400,
            ),
        )


def generated_video_path(config: VideoGenerationConfig, filename: str) -> Path:
    """Resolve one exact lowercase content-addressed MP4 filename."""
    if not isinstance(filename, str) or not filename.endswith(".mp4"):
        raise VideoGenerationError("Generated video filename is invalid.")
    digest = filename[:-4]
    if _SHA256_RE.fullmatch(digest) is None:
        raise VideoGenerationError("Generated video filename is invalid.")
    path = config.output_dir / filename
    if path.is_symlink() or path.resolve().parent != config.output_dir.resolve():
        raise VideoGenerationError("Generated video path is invalid.")
    return path


def _hash_file(path: Path, maximum_bytes: int) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    size = 0
    prefix = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if not prefix:
                prefix = chunk[:16]
            size += len(chunk)
            if size > maximum_bytes:
                raise VideoGenerationError("Artifact exceeds the configured byte limit.")
            digest.update(chunk)
    if size <= 0:
        raise VideoGenerationError("Artifact is empty.")
    return digest.hexdigest(), size, prefix


def verify_generated_video_file(
    config: VideoGenerationConfig, path: Path, expected_digest: str
) -> tuple[int, str]:
    """Verify bounded MP4 container bytes and their content address."""
    if path.is_symlink() or not path.is_file():
        raise VideoGenerationError("Generated video is not a regular file.")
    digest, size, prefix = _hash_file(path, config.max_output_bytes)
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        raise VideoGenerationError("Generated video is not an MP4 container.")
    if digest != expected_digest:
        raise VideoGenerationError("Generated video content address does not match.")
    return size, digest


def _source_png(
    config: VideoGenerationConfig, source_sha256: str
) -> tuple[Path, int, int, int]:
    source = config.source_dir / f"{source_sha256}.png"
    if source.is_symlink() or source.resolve().parent != config.source_dir.resolve():
        raise VideoGenerationError("Source image path is invalid.")
    if not source.is_file():
        raise VideoGenerationError("The verified source image was not found.")
    digest, size, prefix = _hash_file(source, config.max_source_bytes)
    if digest != source_sha256 or prefix[:8] != _PNG_SIGNATURE:
        raise VideoGenerationError("Source image failed content-addressed PNG verification.")

    with source.open("rb") as stream:
        header = stream.read(33)
    if (
        len(header) < 33
        or int.from_bytes(header[8:12], "big") != 13
        or header[12:16] != b"IHDR"
    ):
        raise VideoGenerationError("Source image has an invalid PNG header.")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    bit_depth, color_type, compression, filtering, interlace = header[24:29]
    if (
        not 64 <= width <= 4096
        or not 64 <= height <= 4096
        or width % 2
        or height % 2
        or bit_depth not in {8, 16}
        or color_type not in {0, 2, 3, 4, 6}
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
    ):
        raise VideoGenerationError("Source PNG dimensions or format are unsupported.")
    return source, width, height, size


@dataclass(frozen=True)
class VideoRequest:
    source_sha256: str
    duration_seconds: int
    mode: str
    profile: Optional[str]
    prompt: Optional[str]
    seed: Optional[int]


def _validated_args(config: VideoGenerationConfig, args: Any) -> VideoRequest:
    if not isinstance(args, dict):
        raise VideoGenerationError("Video generation arguments must be an object.")
    allowed = {
        "source_sha256",
        "duration_seconds",
        "mode",
        "profile",
        "prompt",
        "seed",
    }
    unknown = set(args) - allowed
    if unknown:
        raise VideoGenerationError(f"Unsupported video argument: {sorted(unknown)[0]}.")
    source_sha256 = str(args.get("source_sha256") or "")
    if _SHA256_RE.fullmatch(source_sha256) is None:
        raise VideoGenerationError("source_sha256 must be a lowercase 64-character digest.")
    duration = args.get("duration_seconds", config.default_duration_seconds)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 2 <= duration <= 10:
        raise VideoGenerationError("duration_seconds must be an integer from 2 through 10.")
    mode = str(args.get("mode") or "")
    if mode not in {_PROCEDURAL_MODE, _I2V_MODE}:
        raise VideoGenerationError(
            "mode is required and must be exact_source_animation or image_to_video."
        )

    if mode == _PROCEDURAL_MODE:
        if "prompt" in args or "seed" in args:
            raise VideoGenerationError(
                "prompt and seed are available only in image_to_video mode."
            )
        profile = str(args.get("profile") or _PROFILE)
        if profile != _PROFILE:
            raise VideoGenerationError("Only the fixed hover_pulse profile is available.")
        return VideoRequest(
            source_sha256=source_sha256,
            duration_seconds=duration,
            mode=mode,
            profile=profile,
            prompt=None,
            seed=None,
        )

    if "profile" in args:
        raise VideoGenerationError(
            "profile is available only in exact_source_animation mode."
        )
    prompt = str(args.get("prompt") or _DEFAULT_I2V_PROMPT).strip()
    if (
        not prompt
        or len(prompt) > 2000
        or any(ord(char) < 32 and char not in "\n\t" for char in prompt)
    ):
        raise VideoGenerationError("Video prompt is invalid or exceeds 2,000 characters.")
    raw_seed = args.get("seed")
    seed = secrets.randbits(53) if raw_seed is None else raw_seed
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**53:
        raise VideoGenerationError(
            "Video seed must be a JavaScript-safe integer from 0 through 2^53-1."
        )
    return VideoRequest(
        source_sha256=source_sha256,
        duration_seconds=duration,
        mode=mode,
        profile=None,
        prompt=prompt,
        seed=seed,
    )


def _hover_pulse_filter(width: int, height: int) -> str:
    """Return the fixed, non-user-controllable procedural animation graph."""
    center_threshold = max(0, height // 2 - 2)
    selector = (
        f"gte(Y,{center_threshold})*gt(b(X,Y),1.05*r(X,Y))*"
        "gt(b(X,Y),1.03*g(X,Y))"
    )
    blue_pulse = "0.86+0.14*cos(2*PI*T/1.5)"  # range: 0.72 .. 1.00
    green_pulse = "0.91+0.09*cos(2*PI*T/1.5)"  # range: 0.82 .. 1.00
    return (
        f"scale={width + 16}:{height + 16}:flags=lanczos,"
        f"crop={width}:{height}:x='8+4*sin(2*PI*t/5)':y='8+6*sin(2*PI*t/4)',"
        "format=rgb24,"
        f"geq=r='r(X,Y)':g='if({selector},g(X,Y)*({green_pulse}),g(X,Y))':"
        f"b='if({selector},b(X,Y)*({blue_pulse}),b(X,Y))',"
        "format=yuv420p"
    )


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _read_bounded(stream: Optional[asyncio.StreamReader]) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_PROCESS_OUTPUT_BYTES:
            raise VideoGenerationError(
                "Video processor returned excessive diagnostic output."
            )
        chunks.append(chunk)


async def _run_process(
    args: list[str], *, timeout_seconds: int, capture_stdout: bool
) -> tuple[bytes, bytes]:
    """Run one exact argv without a shell and with bounded wall-clock time."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = await asyncio.create_subprocess_exec(*args, **kwargs)
    stdout_task = asyncio.create_task(_read_bounded(process.stdout))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    try:
        stdout, stderr, _return_code = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, wait_task),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        await asyncio.shield(_stop_process(process))
        raise VideoGenerationError("Video processing timed out.") from exc
    except asyncio.CancelledError:
        await asyncio.shield(_stop_process(process))
        raise
    except BaseException:
        await asyncio.shield(_stop_process(process))
        raise
    finally:
        for task in (stdout_task, stderr_task, wait_task):
            if not task.done():
                task.cancel()
    if process.returncode != 0:
        # FFmpeg diagnostics may include the absolute source/temp paths.  Do
        # not project those bytes into the approval receipt or public chat.
        raise VideoGenerationError("Video processor failed.")
    return stdout, stderr


def _ffmpeg_command(
    config: VideoGenerationConfig,
    source: Path,
    temporary_output: Path,
    *,
    width: int,
    height: int,
    duration_seconds: int,
) -> list[str]:
    frames = duration_seconds * config.fps
    return [
        str(config.ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(config.fps),
        "-i",
        str(source),
        "-vf",
        _hover_pulse_filter(width, height),
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-fs",
        str(config.max_output_bytes),
        str(temporary_output),
    ]


def _ffprobe_command(config: VideoGenerationConfig, path: Path) -> list[str]:
    return [
        str(config.ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        (
            "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,nb_frames,duration:"
            "format=format_name,duration,size"
        ),
        "-of",
        "json",
        str(path),
    ]


def _parse_number(value: Any, *, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise VideoGenerationError(f"ffprobe returned invalid {label}.") from exc
    if not math.isfinite(number) or number < 0:
        raise VideoGenerationError(f"ffprobe returned invalid {label}.")
    return number


def _verify_probe(
    payload: bytes,
    *,
    width: int,
    height: int,
    fps: int,
    frame_count: int,
    duration_seconds: int,
    actual_size: int,
) -> dict:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VideoGenerationError("ffprobe returned invalid JSON.") from exc
    streams = data.get("streams") if isinstance(data, dict) else None
    container = data.get("format") if isinstance(data, dict) else None
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(container, dict):
        raise VideoGenerationError("Video must contain exactly one stream.")
    stream = streams[0]
    if not isinstance(stream, dict) or stream.get("codec_type") != "video":
        raise VideoGenerationError("Video stream verification failed.")
    if stream.get("codec_name") != _CODEC:
        raise VideoGenerationError("Video codec verification failed.")
    if stream.get("pix_fmt") != _PIXEL_FORMAT:
        raise VideoGenerationError("Video pixel-format verification failed.")
    if stream.get("width") != width or stream.get("height") != height:
        raise VideoGenerationError("Video dimensions do not match the source image.")
    try:
        measured_fps = float(Fraction(str(stream.get("avg_frame_rate"))))
    except (ValueError, ZeroDivisionError) as exc:
        raise VideoGenerationError("Video frame rate verification failed.") from exc
    if abs(measured_fps - fps) > 0.01:
        raise VideoGenerationError("Video frame rate verification failed.")
    try:
        measured_frames = int(stream.get("nb_frames"))
    except (TypeError, ValueError) as exc:
        raise VideoGenerationError("Video frame-count verification failed.") from exc
    if measured_frames != frame_count:
        raise VideoGenerationError("Video frame-count verification failed.")
    measured_duration = _parse_number(container.get("duration"), label="duration")
    if abs(measured_duration - duration_seconds) > (1 / fps + 0.02):
        raise VideoGenerationError("Video duration verification failed.")
    stream_duration = _parse_number(stream.get("duration"), label="stream duration")
    if abs(stream_duration - duration_seconds) > (1 / fps + 0.02):
        raise VideoGenerationError("Video stream-duration verification failed.")
    try:
        probe_size = int(container.get("size"))
    except (TypeError, ValueError) as exc:
        raise VideoGenerationError("Video file-size verification failed.") from exc
    if probe_size != actual_size:
        raise VideoGenerationError("Video file-size verification failed.")
    if "mp4" not in str(container.get("format_name") or "").split(","):
        raise VideoGenerationError("Video container verification failed.")
    return {
        "codec": _CODEC,
        "pixel_format": _PIXEL_FORMAT,
        "width": width,
        "height": height,
        "fps": fps,
        "frame_count": frame_count,
        "duration_seconds": duration_seconds,
    }


class WanTI2VProvider:
    """Run one fixed, built-in-node Wan2.2 TI2V workflow under a GPU lease."""

    def __init__(
        self,
        config: VideoGenerationConfig,
        router: ModelRouter,
        runtime_provider: ComfyUIProvider,
    ) -> None:
        self.config = config
        self.router = router
        self.runtime_provider = runtime_provider
        self._lock = asyncio.Lock()
        self._asset_proof_cache: Optional[dict[str, dict[str, Any]]] = None

    @property
    def _comfy_root(self) -> Path:
        return self.runtime_provider.config.runtime_root / "ComfyUI"

    def _asset_paths(self) -> dict[str, Path]:
        model_root = self._comfy_root / "models"
        return {
            _I2V_MODEL_FILE: model_root / "diffusion_models" / _I2V_MODEL_FILE,
            _I2V_TEXT_ENCODER_FILE: (
                model_root / "text_encoders" / _I2V_TEXT_ENCODER_FILE
            ),
            _I2V_VAE_FILE: model_root / "vae" / _I2V_VAE_FILE,
        }

    @staticmethod
    def _verify_official_asset(path: Path, filename: str) -> dict[str, Any]:
        expected = _I2V_OFFICIAL_ASSETS[filename]
        try:
            if path.is_symlink() or not path.is_file():
                return {
                    "present": False,
                    "verified": False,
                    "bytes": None,
                    "sha256": None,
                    "identity": None,
                }
            before = path.stat()
            actual_size = before.st_size
            if actual_size != expected["bytes"]:
                return {
                    "present": True,
                    "verified": False,
                    "bytes": actual_size,
                    "sha256": None,
                    "identity": {
                        "size": before.st_size,
                        "mtime_ns": before.st_mtime_ns,
                        "device": before.st_dev,
                        "inode": before.st_ino,
                    },
                }
            digest = hashlib.sha256()
            measured = 0
            with path.open("rb") as stream:
                while chunk := stream.read(8 * 1024 * 1024):
                    measured += len(chunk)
                    if measured > expected["bytes"]:
                        break
                    digest.update(chunk)
            actual_digest = digest.hexdigest()
            after = path.stat()
            unchanged = (
                before.st_size == after.st_size
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_dev == after.st_dev
                and before.st_ino == after.st_ino
            )
            verified = (
                measured == expected["bytes"]
                and actual_digest == expected["sha256"]
                and unchanged
            )
            return {
                "present": True,
                "verified": verified,
                "bytes": actual_size,
                "sha256": actual_digest,
                "identity": {
                    "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns,
                    "device": after.st_dev,
                    "inode": after.st_ino,
                },
            }
        except OSError:
            return {
                "present": False,
                "verified": False,
                "bytes": None,
                "sha256": None,
                "identity": None,
            }

    async def _verified_asset_proof(self) -> dict[str, dict[str, Any]]:
        if self._asset_proof_is_current(self._asset_proof_cache):
            return self._asset_proof_cache
        proof: dict[str, dict[str, Any]] = {}
        for filename, path in self._asset_paths().items():
            measured = await asyncio.to_thread(
                self._verify_official_asset, path, filename
            )
            expected = _I2V_OFFICIAL_ASSETS[filename]
            proof[filename] = {
                **measured,
                "expected_bytes": expected["bytes"],
                "expected_sha256": expected["sha256"],
            }
        self._asset_proof_cache = proof
        return proof

    def _asset_proof_is_current(self, proof: Any) -> bool:
        if not isinstance(proof, dict) or set(proof) != set(_I2V_OFFICIAL_ASSETS):
            return False
        try:
            for filename, path in self._asset_paths().items():
                item = proof.get(filename)
                identity = item.get("identity") if isinstance(item, dict) else None
                expected = _I2V_OFFICIAL_ASSETS[filename]
                if not (
                    isinstance(item, dict)
                    and item.get("verified") is True
                    and item.get("bytes") == expected["bytes"]
                    and item.get("sha256") == expected["sha256"]
                    and isinstance(identity, dict)
                    and not path.is_symlink()
                    and path.is_file()
                ):
                    return False
                current = path.stat()
                if identity != {
                    "size": current.st_size,
                    "mtime_ns": current.st_mtime_ns,
                    "device": current.st_dev,
                    "inode": current.st_ino,
                }:
                    return False
            return True
        except OSError:
            return False

    async def status(self) -> dict[str, Any]:
        asset_state = await self._verified_asset_proof()
        missing = [
            name for name, proof in asset_state.items()
            if proof.get("present") is not True
        ]
        invalid = [
            name for name, proof in asset_state.items()
            if proof.get("present") is True and proof.get("verified") is not True
        ]
        base = {
            "enabled": self.config.i2v_enabled,
            "provider": _I2V_PROVIDER,
            "model_id": _I2V_MODEL_ID,
            "render_kind": _I2V_RENDER_KIND,
            "actual_generation": True,
            "source_conditioned": True,
            "source_preserved": False,
            "width": _I2V_WIDTH,
            "height": _I2V_HEIGHT,
            "fps": self.config.fps,
            "duration_seconds": {"minimum": 2, "maximum": 10},
            "fixed_workflow": True,
            "built_in_nodes_only": True,
            "model_assets": asset_state,
            "missing_assets": missing,
            "invalid_assets": invalid,
            "live_generation_proven": False,
            "requires_sequential_model_unload": True,
        }
        if not self.config.i2v_enabled:
            return {
                **base,
                "ok": False,
                "state": "disabled",
                "generation_available": False,
                "message": "Generative image-to-video is disabled.",
            }
        if missing or invalid:
            return {
                **base,
                "ok": False,
                "state": "model_assets_missing" if missing else "model_assets_invalid",
                "generation_available": False,
                "message": (
                    "Wan2.2 TI2V model assets are not installed."
                    if missing
                    else "Wan2.2 TI2V model assets failed official size and SHA-256 proof."
                ),
            }

        runtime = await self.runtime_provider.status()
        available = runtime.get("generation_available") is True
        return {
            **base,
            "ok": available,
            "state": (
                "configured_unverified"
                if available
                else "runtime_conflict_or_unavailable"
            ),
            "generation_available": available,
            "runtime_state": runtime.get("state"),
            "message": (
                "Wan2.2 TI2V is configured but has not yet completed a verified live run."
                if available
                else "The owned ComfyUI runtime is not available for Wan2.2 TI2V."
            ),
        }

    @staticmethod
    def workflow(
        *,
        prompt: str,
        seed: int,
        duration_seconds: int,
        staged_filename: str,
        request_id: str,
        fps: int,
    ) -> dict[str, Any]:
        """Return the fixed API graph derived from ComfyUI's official 5B template."""
        output_frames = duration_seconds * fps
        latent_frames = output_frames + 1  # Wan length must be 4n+1.
        return {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {
                    "unet_name": _I2V_MODEL_FILE,
                    "weight_dtype": "default",
                },
            },
            "2": {
                "class_type": "ModelSamplingSD3",
                "inputs": {"model": ["1", 0], "shift": _I2V_SHIFT},
            },
            "3": {
                "class_type": "CLIPLoader",
                "inputs": {
                    "clip_name": _I2V_TEXT_ENCODER_FILE,
                    "type": "wan",
                    "device": "default",
                },
            },
            "4": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": prompt, "clip": ["3", 0]},
            },
            "5": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": _I2V_NEGATIVE_PROMPT, "clip": ["3", 0]},
            },
            "6": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": _I2V_VAE_FILE},
            },
            "7": {
                "class_type": "LoadImage",
                "inputs": {"image": staged_filename},
            },
            "8": {
                "class_type": "Wan22ImageToVideoLatent",
                "inputs": {
                    "vae": ["6", 0],
                    "start_image": ["7", 0],
                    "width": _I2V_WIDTH,
                    "height": _I2V_HEIGHT,
                    "length": latent_frames,
                    "batch_size": 1,
                },
            },
            "9": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": _I2V_STEPS,
                    "cfg": _I2V_CFG,
                    "sampler_name": "uni_pc",
                    "scheduler": "simple",
                    "denoise": 1.0,
                    "model": ["2", 0],
                    "positive": ["4", 0],
                    "negative": ["5", 0],
                    "latent_image": ["8", 0],
                },
            },
            "10": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["9", 0], "vae": ["6", 0]},
            },
            "11": {
                "class_type": "ImageFromBatch",
                "inputs": {
                    "image": ["10", 0],
                    "batch_index": 0,
                    "length": output_frames,
                },
            },
            "12": {
                "class_type": "CreateVideo",
                "inputs": {"images": ["11", 0], "fps": float(fps)},
            },
            "13": {
                "class_type": "SaveVideo",
                "inputs": {
                    "video": ["12", 0],
                    "filename_prefix": f"{_SAFE_COMFY_OUTPUT_FOLDER}/{request_id}",
                    "format": "mp4",
                    "codec": "h264",
                },
            },
        }

    @staticmethod
    def _choices(payload: dict[str, Any], node: str, input_name: str) -> list[str]:
        try:
            raw = payload[node]["input"]["required"][input_name][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise VideoGenerationError(
                f"ComfyUI did not expose the required {node} input."
            ) from exc
        if not isinstance(raw, list):
            raise VideoGenerationError(
                f"ComfyUI returned invalid choices for {node}."
            )
        return [str(item) for item in raw]

    async def _live_workflow_proof(self) -> dict[str, Any]:
        required_nodes = (
            "UNETLoader",
            "ModelSamplingSD3",
            "CLIPLoader",
            "CLIPTextEncode",
            "VAELoader",
            "LoadImage",
            "Wan22ImageToVideoLatent",
            "KSampler",
            "VAEDecode",
            "ImageFromBatch",
            "CreateVideo",
            "SaveVideo",
        )
        objects: dict[str, Any] = {}
        async with self.runtime_provider._client(20) as client:
            for node in required_nodes:
                payload = await self.runtime_provider._bounded_json(
                    client, "GET", f"/object_info/{node}"
                )
                if not isinstance(payload.get(node), dict):
                    raise VideoGenerationError(
                        f"Required built-in ComfyUI node {node} is unavailable."
                    )
                objects[node] = payload[node]
        wrapped = {name: value for name, value in objects.items()}
        if _I2V_MODEL_FILE not in self._choices(wrapped, "UNETLoader", "unet_name"):
            raise VideoGenerationError("Wan2.2 TI2V is not visible to live ComfyUI.")
        if _I2V_TEXT_ENCODER_FILE not in self._choices(
            wrapped, "CLIPLoader", "clip_name"
        ):
            raise VideoGenerationError("The Wan text encoder is not visible to live ComfyUI.")
        if _I2V_VAE_FILE not in self._choices(wrapped, "VAELoader", "vae_name"):
            raise VideoGenerationError("The Wan2.2 VAE is not visible to live ComfyUI.")
        return {
            "built_in_nodes": list(required_nodes),
            "model_visible": True,
            "text_encoder_visible": True,
            "vae_visible": True,
        }

    def _stage_source(self, source: Path, source_sha256: str, request_id: str) -> Path:
        input_root = (self._comfy_root / "input").resolve()
        input_root.mkdir(parents=True, exist_ok=True)
        target = input_root / f"xomni_i2v_{request_id}_{source_sha256[:12]}.png"
        if target.resolve().parent != input_root or target.exists() or target.is_symlink():
            raise VideoGenerationError("ComfyUI source staging target is invalid.")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
                while chunk := input_stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.config.max_source_bytes:
                        raise VideoGenerationError("Source image exceeds the configured byte limit.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size <= 0 or digest.hexdigest() != source_sha256:
                raise VideoGenerationError("Staged ComfyUI source failed SHA-256 verification.")
            return target
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _safe_video_reference(output: Any, request_id: str) -> dict[str, str]:
        if not isinstance(output, dict):
            raise VideoGenerationError("ComfyUI returned an invalid video reference.")
        filename = str(output.get("filename") or "")
        subfolder = str(output.get("subfolder") or "").replace("\\", "/")
        output_type = str(output.get("type") or "")
        folder = PurePosixPath(subfolder)
        if (
            not filename
            or Path(filename).name != filename
            or not filename.startswith(f"{request_id}_")
            or not filename.casefold().endswith(".mp4")
            or "\x00" in filename
            or folder != _SAFE_COMFY_OUTPUT_FOLDER
            or folder.is_absolute()
            or ".." in folder.parts
            or output_type != "output"
        ):
            raise VideoGenerationError("ComfyUI returned an unsafe video reference.")
        return {"filename": filename, "subfolder": subfolder, "type": output_type}

    def _owned_output_path(self, reference: dict[str, str]) -> Path:
        output_root = (self._comfy_root / "output").resolve()
        candidate = (
            output_root / reference["subfolder"] / reference["filename"]
        ).resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError as exc:
            raise VideoGenerationError("ComfyUI video output escaped its owned root.") from exc
        if candidate.is_symlink():
            raise VideoGenerationError("ComfyUI video output cannot be a symlink.")
        return candidate

    async def _download_video(
        self, client: httpx.AsyncClient, reference: dict[str, str]
    ) -> Path:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".xomni-wan-", suffix=".mp4", dir=self.config.output_dir
        )
        os.close(descriptor)
        temporary = Path(name)
        size = 0
        try:
            with temporary.open("wb") as stream:
                async with client.stream("GET", "/view", params=reference) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if content_type.casefold() not in {"video/mp4", "application/octet-stream"}:
                        raise VideoGenerationError("ComfyUI output was not an MP4 video.")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.config.max_output_bytes:
                            raise VideoGenerationError("ComfyUI returned an oversized video.")
                        stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size <= 0:
                raise VideoGenerationError("ComfyUI returned an empty video.")
            _digest, measured, prefix = await asyncio.to_thread(
                _hash_file, temporary, self.config.max_output_bytes
            )
            if measured != size or len(prefix) < 12 or prefix[4:8] != b"ftyp":
                raise VideoGenerationError("ComfyUI returned an invalid MP4 container.")
            return temporary
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _cleanup_request_files(
        self,
        staged_source: Optional[Path],
        request_id: str,
    ) -> int:
        removed_outputs = 0
        if staged_source is not None:
            input_root = (self._comfy_root / "input").resolve()
            if staged_source.resolve().parent != input_root or staged_source.is_symlink():
                raise VideoGenerationError("Refusing unsafe ComfyUI source cleanup.")
            staged_source.unlink(missing_ok=True)

        request_folder = (self._comfy_root / "output" / str(_SAFE_COMFY_OUTPUT_FOLDER)).resolve()
        output_root = (self._comfy_root / "output").resolve()
        try:
            request_folder.relative_to(output_root)
        except ValueError as exc:
            raise VideoGenerationError("Refusing unsafe ComfyUI output cleanup.") from exc
        if not request_folder.is_dir():
            return removed_outputs
        for candidate in request_folder.iterdir():
            if not candidate.name.startswith(f"{request_id}_"):
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise VideoGenerationError("Refusing unsafe ComfyUI output cleanup target.")
            candidate.unlink()
            removed_outputs += 1
        return removed_outputs

    async def _execute_workflow(
        self,
        *,
        prompt: str,
        seed: int,
        duration_seconds: int,
        staged_source: Path,
        request_id: str,
        runtime: RuntimeHandle,
        generation: dict[str, Any],
    ) -> Path:
        await self._live_workflow_proof()
        client_id = str(uuid.uuid4())
        workflow = self.workflow(
            prompt=prompt,
            seed=seed,
            duration_seconds=duration_seconds,
            staged_filename=staged_source.name,
            request_id=request_id,
            fps=self.config.fps,
        )
        async with self.runtime_provider._client(
            self.config.i2v_generation_timeout_seconds
        ) as client:
            # POST /prompt is deliberately single-shot. If transport fails
            # after bytes may have reached ComfyUI, retrying could enqueue a
            # duplicate expensive job. Preserve that indeterminacy instead.
            generation["submit_state"] = "indeterminate"
            generation["may_have_generated"] = True
            queue_body = await self.runtime_provider._bounded_json(
                client,
                "POST",
                "/prompt",
                json_body={"prompt": workflow, "client_id": client_id},
            )
            prompt_id = str(queue_body.get("prompt_id") or "").strip()
            if not prompt_id or len(prompt_id) > 160:
                raise VideoGenerationError("ComfyUI returned no valid video prompt ID.")
            generation["submit_state"] = "accepted"
            generation["prompt_id_known"] = True
            runtime.active_prompt_id = prompt_id
            downloaded: Optional[Path] = None
            try:
                deadline = time.monotonic() + self.config.i2v_generation_timeout_seconds
                reference: Optional[dict[str, str]] = None
                while time.monotonic() < deadline:
                    history = await self.runtime_provider._bounded_json(
                        client, "GET", f"/history/{prompt_id}"
                    )
                    record = history.get(prompt_id) if isinstance(history, dict) else None
                    if isinstance(record, dict):
                        status = record.get("status") or {}
                        if isinstance(status, dict) and status.get("status_str") == "error":
                            raise VideoGenerationError(
                                "ComfyUI reported a Wan2.2 workflow execution error."
                            )
                        outputs = record.get("outputs") or {}
                        node_output = outputs.get("13") if isinstance(outputs, dict) else None
                        images = node_output.get("images") if isinstance(node_output, dict) else None
                        if isinstance(images, list) and len(images) == 1:
                            reference = self._safe_video_reference(images[0], request_id)
                            break
                    await asyncio.sleep(self.runtime_provider.poll_interval_s)
                if reference is None:
                    raise VideoGenerationError(
                        "Wan2.2 generation timed out before producing a video."
                    )
                downloaded = await self._download_video(client, reference)
                runtime.active_prompt_id = None
                return downloaded
            except BaseException:
                if downloaded is not None:
                    await _await_cleanup(
                        asyncio.to_thread(downloaded.unlink, missing_ok=True)
                    )
                try:
                    if runtime.active_prompt_id == prompt_id:
                        await _await_cleanup(
                            self.runtime_provider._cancel_prompt(prompt_id, runtime)
                        )
                        generation["prompt_delete_requested"] = True
                    runtime.active_prompt_id = None
                except Exception as cleanup_exc:
                    raise VideoGenerationError(
                        "Wan2.2 generation failed and its exact ComfyUI job could not be cancelled."
                    ) from cleanup_exc
                raise

    async def generate(
        self,
        *,
        source: Path,
        source_sha256: str,
        prompt: str,
        seed: int,
        duration_seconds: int,
        verified_asset_proof: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        staged_source: Optional[Path] = None
        temporary: Optional[Path] = None
        handle: Optional[RuntimeHandle] = None
        lease: dict[str, Any] = {}
        runtime_release_attempted = False
        runtime_released: Optional[bool] = None
        request_files_cleanup_attempted = False
        request_files_cleaned: Optional[bool] = None
        removed_outputs = 0
        generation = {
            "submit_state": "not_attempted",
            "prompt_id_known": False,
            "prompt_delete_requested": False,
            "prompt_cancelled": None,
            "may_have_generated": False,
            "may_have_surviving_output": False,
            "output_removed": None,
        }
        asset_proof = (
            verified_asset_proof
            if self._asset_proof_is_current(verified_asset_proof)
            else await self._verified_asset_proof()
        )
        if not all(item.get("verified") is True for item in asset_proof.values()):
            raise VideoGenerationError(
                "Wan2.2 TI2V model assets failed official size and SHA-256 proof."
            )
        body_error: Optional[BaseException] = None
        cleanup_error: Optional[BaseException] = None
        try:
            async with self._lock:
                async with self.router.external_workload_session("video_generation") as lease:
                    handle = await self.runtime_provider.ensure_runtime()
                    try:
                        staged_source = await asyncio.to_thread(
                            self._stage_source, source, source_sha256, request_id
                        )
                        temporary = await self._execute_workflow(
                            prompt=prompt,
                            seed=seed,
                            duration_seconds=duration_seconds,
                            staged_source=staged_source,
                            request_id=request_id,
                            runtime=handle,
                            generation=generation,
                        )
                    finally:
                        if handle is not None:
                            runtime_release_attempted = True
                            try:
                                await _await_cleanup(
                                    self.runtime_provider.release_runtime(handle)
                                )
                                runtime_released = True
                            except asyncio.CancelledError:
                                # _await_cleanup propagates cancellation only
                                # after the owned release task completed.
                                runtime_released = True
                                raise
                            except BaseException:
                                runtime_released = False
                                raise
        except BaseException as exc:
            body_error = exc
            if isinstance(exc, ExternalWorkloadStartError):
                lease = exc.lifecycle

        request_files_cleanup_attempted = True
        try:
            removed_outputs = await _await_cleanup(
                asyncio.to_thread(
                    self._cleanup_request_files, staged_source, request_id
                )
            )
            request_files_cleaned = True
        except asyncio.CancelledError as exc:
            # The scoped file cleanup completed before _await_cleanup
            # propagated caller cancellation.
            request_files_cleaned = True
            cleanup_error = exc
        except BaseException as exc:
            request_files_cleaned = False
            cleanup_error = exc

        model_stopped = lease.get("model_stopped") is True
        model_restore_required = lease.get("model_restore_required") is True or model_stopped
        raw_model_restored = lease.get("model_restored")
        lifecycle = {
            "mode": "sequential_exclusive",
            "previous_worker": lease.get("previous_worker"),
            "model_stop_attempted": lease.get("model_stop_attempted") is True,
            "model_stopped": model_stopped,
            "model_restore_required": model_restore_required,
            "model_restored": (
                raw_model_restored is True
                if model_restore_required
                else None
            ),
            "gpu_indices": lease.get("gpu_indices") or [],
            "external_runtime": (
                "cleanup_unverified"
                if handle is not None and runtime_released is not True
                else ("spawned" if handle is not None and handle.spawned else "not_started")
            ),
            "runtime_release_attempted": runtime_release_attempted,
            "runtime_released": runtime_released,
            "request_files_cleanup_attempted": request_files_cleanup_attempted,
            "request_files_cleaned": request_files_cleaned,
        }
        if generation["submit_state"] == "not_attempted":
            generation["may_have_surviving_output"] = False
        elif runtime_released is True and request_files_cleaned is True:
            generation["may_have_surviving_output"] = False
            if generation["submit_state"] == "accepted":
                generation["prompt_cancelled"] = True
            generation["output_removed"] = removed_outputs > 0
        else:
            generation["may_have_surviving_output"] = None
        compact_assets = {
            filename: {
                "verified": proof.get("verified") is True,
                "bytes": proof.get("bytes"),
                "sha256": proof.get("sha256"),
            }
            for filename, proof in asset_proof.items()
        }

        cancellation = next(
            (
                error
                for error in (body_error, cleanup_error)
                if isinstance(error, asyncio.CancelledError)
            ),
            None,
        )
        if cancellation is not None:
            if temporary is not None:
                await _await_cleanup(
                    asyncio.to_thread(temporary.unlink, missing_ok=True)
                )
            raise WanCancellationError(
                lifecycle=lifecycle,
                generation=generation,
            ) from cancellation

        failure = cleanup_error or body_error
        if temporary is not None and temporary.is_file() and (
            failure is not None or lifecycle["model_restored"] is not True
        ):
            if isinstance(failure, (VideoGenerationError, ImageGenerationError, WorkerSwapError)):
                message = str(failure)
            elif failure is not None:
                message = f"Wan2.2 lifecycle failed ({type(failure).__name__})."
            else:
                message = "The conversation model was not verified restored."
            raise WanLifecycleError(
                message,
                temporary_path=temporary,
                lifecycle=lifecycle,
                model_assets=compact_assets,
            ) from failure

        if failure is not None:
            if temporary is not None:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)
            if isinstance(failure, (VideoGenerationError, ImageGenerationError, WorkerSwapError)):
                message = str(failure)
            else:
                message = f"Wan2.2 generation failed ({type(failure).__name__})."
            raise WanExecutionError(
                message,
                lifecycle=lifecycle,
                generation=generation,
                stage=(
                    body_error.stage
                    if isinstance(body_error, ExternalWorkloadStartError)
                    else "generative_execution"
                ),
                retryable=(
                    body_error.retryable
                    if isinstance(body_error, ExternalWorkloadStartError)
                    else False
                ),
            ) from failure
        if temporary is None or not temporary.is_file():
            raise WanExecutionError(
                "Wan2.2 returned no completed local video.",
                lifecycle=lifecycle,
                generation=generation,
            )
        if lifecycle["model_restored"] is not True:
            # The branch above normally catches this. Keep the explicit guard
            # fail-closed if the temporary file changes between checks.
            raise WanLifecycleError(
                "The conversation model was not verified restored.",
                temporary_path=temporary,
                lifecycle=lifecycle,
                model_assets=compact_assets,
            )
        return {
            "temporary_path": temporary,
            "lifecycle": lifecycle,
            "model_assets": compact_assets,
        }


class VideoGenerationService:
    """Dispatch procedural and genuine source-conditioned video modes."""

    def __init__(
        self,
        config: VideoGenerationConfig,
        router: Optional[ModelRouter] = None,
        comfy_provider: Optional[ComfyUIProvider] = None,
        *,
        wan_provider: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.wan_provider = wan_provider
        if self.wan_provider is None and router is not None and comfy_provider is not None:
            self.wan_provider = WanTI2VProvider(config, router, comfy_provider)

    async def _i2v_status(self) -> dict[str, Any]:
        if self.wan_provider is not None:
            return await self.wan_provider.status()
        return {
            "ok": False,
            "state": "runtime_integration_unavailable",
            "generation_available": False,
            "enabled": self.config.i2v_enabled,
            "provider": _I2V_PROVIDER,
            "model_id": _I2V_MODEL_ID,
            "render_kind": _I2V_RENDER_KIND,
            "actual_generation": True,
            "source_conditioned": True,
            "source_preserved": False,
            "live_generation_proven": False,
            "message": "Wan2.2 TI2V is not connected to an owned ComfyUI runtime.",
        }

    async def status(self, _args: Optional[dict] = None) -> dict:
        ffmpeg_ready = self.config.ffmpeg_path.is_file()
        ffprobe_ready = self.config.ffprobe_path.is_file()
        source_ready = self.config.source_dir.is_dir()
        procedural_available = bool(
            self.config.enabled and ffmpeg_ready and ffprobe_ready and source_ready
        )
        i2v = await self._i2v_status()
        i2v_available = bool(
            self.config.enabled
            and ffprobe_ready
            and source_ready
            and i2v.get("generation_available") is True
        )
        return {
            "ok": procedural_available or i2v_available,
            "status": "available" if procedural_available or i2v_available else "unavailable",
            "state": (
                "configured_available"
                if procedural_available or i2v_available
                else "configured_unavailable"
            ),
            # Backward-compatible top-level field describes whether at least
            # one video mode can execute. The nested mode records are
            # authoritative about whether true diffusion is available.
            "generation_available": procedural_available or i2v_available,
            "true_generation_available": i2v_available,
            "provider": self.config.provider,
            "profile": _PROFILE,
            "duration_seconds": {"minimum": 2, "maximum": 10},
            "fps": self.config.fps,
            "actual_generation": i2v_available,
            "source_preserving": procedural_available,
            "ffmpeg_available": ffmpeg_ready,
            "ffprobe_available": ffprobe_ready,
            "source_store_available": source_ready,
            "modes": {
                _PROCEDURAL_MODE: {
                    "generation_available": procedural_available,
                    "provider": self.config.provider,
                    "render_kind": _RENDER_KIND,
                    "actual_generation": False,
                    "source_preserved": True,
                    "profile": _PROFILE,
                },
                _I2V_MODE: {
                    **i2v,
                    "ok": i2v_available,
                    "generation_available": i2v_available,
                },
            },
            "message": (
                "True Wan2.2 image-to-video and procedural source animation are available."
                if i2v_available and procedural_available
                else (
                    "Procedural source animation is available; true Wan2.2 image-to-video is not ready."
                    if procedural_available
                    else "Video generation dependencies are not all available."
                )
            ),
        }

    async def _verify_and_store(
        self,
        temporary: Path,
        *,
        width: int,
        height: int,
        duration_seconds: int,
    ) -> tuple[str, int, str, dict[str, Any]]:
        digest, size, prefix = await asyncio.to_thread(
            _hash_file, temporary, self.config.max_output_bytes
        )
        if len(prefix) < 12 or prefix[4:8] != b"ftyp":
            raise VideoGenerationError("Video generation did not produce an MP4 container.")
        probe_output, _ = await _run_process(
            _ffprobe_command(self.config, temporary),
            timeout_seconds=min(30, self.config.timeout_seconds),
            capture_stdout=True,
        )
        proof = _verify_probe(
            probe_output,
            width=width,
            height=height,
            fps=self.config.fps,
            frame_count=duration_seconds * self.config.fps,
            duration_seconds=duration_seconds,
            actual_size=size,
        )
        target = self.config.output_dir / f"{digest}.mp4"
        if target.exists():
            await asyncio.to_thread(
                verify_generated_video_file, self.config, target, digest
            )
            temporary.unlink(missing_ok=True)
        else:
            with temporary.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            await asyncio.to_thread(
                verify_generated_video_file, self.config, target, digest
            )
        return digest, size, f"/api/generated-videos/{digest}.mp4", proof

    async def _generate_procedural(
        self,
        request: VideoRequest,
        source: Path,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        if not self.config.ffmpeg_path.is_file() or not self.config.ffprobe_path.is_file():
            raise VideoGenerationError("The configured FFmpeg tools are unavailable.")
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".xomni-video-", suffix=".mp4", dir=self.config.output_dir
        )
        os.close(descriptor)
        temporary = Path(temp_name)
        try:
            await _run_process(
                _ffmpeg_command(
                    self.config,
                    source,
                    temporary,
                    width=width,
                    height=height,
                    duration_seconds=request.duration_seconds,
                ),
                timeout_seconds=self.config.timeout_seconds,
                capture_stdout=False,
            )
            digest, size, url, proof = await self._verify_and_store(
                temporary,
                width=width,
                height=height,
                duration_seconds=request.duration_seconds,
            )
            temporary = None
            return {
                "ok": True,
                "status": "completed",
                "executed": True,
                "success": True,
                "actual_video": True,
                "actual_generation": False,
                "verified": True,
                "source_preserved": True,
                "source_conditioned": False,
                "source_verified": True,
                "provider": self.config.provider,
                "render_kind": _RENDER_KIND,
                "mode": _PROCEDURAL_MODE,
                "profile": request.profile,
                "source_sha256": request.source_sha256,
                "video_url": url,
                "target": url,
                "mime_type": _MIME_TYPE,
                "sha256": digest,
                "bytes": size,
                "lifecycle": {
                    "mode": "bounded_cpu_subprocess",
                    "model_remained_available": True,
                },
                **proof,
                "message": (
                    "Created a verified procedural hover-and-pulse animation. "
                    "No generative video model was used."
                ),
            }
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _require_wan_temporary(self, temporary: Any) -> Path:
        if not isinstance(temporary, Path):
            raise VideoGenerationError("Wan2.2 returned no owned temporary video.")
        expected_parent = self.config.output_dir.resolve()
        if (
            temporary.is_symlink()
            or temporary.resolve().parent != expected_parent
            or not temporary.name.startswith(".xomni-wan-")
            or temporary.suffix.casefold() != ".mp4"
        ):
            raise VideoGenerationError("Wan2.2 returned an unsafe temporary video target.")
        return temporary

    @staticmethod
    def _require_wan_asset_proof(model_assets: Any) -> dict[str, Any]:
        if not isinstance(model_assets, dict) or set(model_assets) != set(
            _I2V_OFFICIAL_ASSETS
        ):
            raise VideoGenerationError("Wan2.2 returned invalid model-asset proof.")
        for filename, expected in _I2V_OFFICIAL_ASSETS.items():
            proof_item = model_assets.get(filename)
            if not (
                isinstance(proof_item, dict)
                and proof_item.get("verified") is True
                and proof_item.get("bytes") == expected["bytes"]
                and proof_item.get("sha256") == expected["sha256"]
            ):
                raise VideoGenerationError(
                    "Wan2.2 returned invalid official model-asset proof."
                )
        return model_assets

    async def _retain_failed_wan_result(
        self,
        request: VideoRequest,
        source_width: int,
        source_height: int,
        failure: WanLifecycleError,
    ) -> dict[str, Any]:
        temporary: Optional[Path] = self._require_wan_temporary(
            failure.temporary_path
        )
        try:
            model_assets = self._require_wan_asset_proof(failure.model_assets)
            digest, size, url, proof = await self._verify_and_store(
                temporary,
                width=_I2V_WIDTH,
                height=_I2V_HEIGHT,
                duration_seconds=request.duration_seconds,
            )
            temporary = None
            return {
                **self._failure(
                    request,
                    stage="model_restore_or_runtime_release",
                    executed=True,
                    source_verified=True,
                    message=(
                        "Wan2.2 produced and verified a video, but runtime cleanup or "
                        f"conversation-model restoration failed: {failure}"
                    ),
                ),
                "actual_video": True,
                "actual_generation": True,
                "verified": True,
                "source_preserved": False,
                "source_conditioned": True,
                "model_assets": model_assets,
                "seed": request.seed,
                "prompt_sha256": hashlib.sha256(
                    request.prompt.encode("utf-8")
                ).hexdigest(),
                "source_width": source_width,
                "source_height": source_height,
                "video_url": url,
                "target": url,
                "mime_type": _MIME_TYPE,
                "sha256": digest,
                "bytes": size,
                "lifecycle": failure.lifecycle,
                **proof,
            }
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _generate_i2v(
        self,
        request: VideoRequest,
        source: Path,
        source_width: int,
        source_height: int,
    ) -> dict[str, Any]:
        if not self.config.ffprobe_path.is_file():
            return self._failure(
                request,
                stage="preflight",
                executed=False,
                source_verified=True,
                message="The configured FFprobe tool is unavailable.",
            )
        readiness = await self._i2v_status()
        if readiness.get("generation_available") is not True:
            return {
                **self._failure(
                    request,
                    stage="preflight",
                    executed=False,
                    source_verified=True,
                    message=str(
                        readiness.get("message")
                        or "Wan2.2 TI2V is not ready."
                    ),
                ),
                "readiness": readiness,
            }
        if self.wan_provider is None:  # fail closed even if a malformed status says ready
            raise VideoGenerationError("Wan2.2 TI2V runtime integration is unavailable.")

        try:
            generated = await self.wan_provider.generate(
                source=source,
                source_sha256=request.source_sha256,
                prompt=request.prompt,
                seed=request.seed,
                duration_seconds=request.duration_seconds,
                verified_asset_proof=readiness.get("model_assets"),
            )
        except WanCancellationError as exc:
            exc.receipt_result = {
                **self._failure(
                    request,
                    stage="generative_execution",
                    executed=True,
                    source_verified=True,
                    message=str(exc),
                ),
                "execution_state": "cancelled",
                "may_have_executed": True,
                "lifecycle": exc.lifecycle,
                "generation": exc.generation,
            }
            raise
        except WanExecutionError as exc:
            return {
                **self._failure(
                    request,
                    stage=exc.stage,
                    executed=True,
                    source_verified=True,
                    message=str(exc),
                ),
                "retryable": exc.retryable,
                "lifecycle": exc.lifecycle,
                "generation": exc.generation,
            }
        except WanLifecycleError as exc:
            return await self._retain_failed_wan_result(
                request, source_width, source_height, exc
            )
        temporary = self._require_wan_temporary(generated.get("temporary_path"))
        try:
            lifecycle = generated.get("lifecycle") or {}
            if not (
                lifecycle.get("mode") == "sequential_exclusive"
                and lifecycle.get("model_stopped") is True
                and lifecycle.get("model_restored") is True
                and isinstance(lifecycle.get("gpu_indices"), list)
                and lifecycle.get("gpu_indices")
            ):
                raise VideoGenerationError(
                    "Wan2.2 did not prove conversation-model restoration."
                )
            model_assets = self._require_wan_asset_proof(
                generated.get("model_assets")
            )
            digest, size, url, proof = await self._verify_and_store(
                temporary,
                width=_I2V_WIDTH,
                height=_I2V_HEIGHT,
                duration_seconds=request.duration_seconds,
            )
            temporary = None
            return {
                "ok": True,
                "status": "completed",
                "executed": True,
                "success": True,
                "actual_video": True,
                "actual_generation": True,
                "verified": True,
                "source_preserved": False,
                "source_conditioned": True,
                "source_verified": True,
                "provider": _I2V_PROVIDER,
                "render_kind": _I2V_RENDER_KIND,
                "mode": _I2V_MODE,
                "model_id": _I2V_MODEL_ID,
                "model_assets": model_assets,
                "seed": request.seed,
                "prompt_sha256": hashlib.sha256(
                    request.prompt.encode("utf-8")
                ).hexdigest(),
                "source_sha256": request.source_sha256,
                "source_width": source_width,
                "source_height": source_height,
                "video_url": url,
                "target": url,
                "mime_type": _MIME_TYPE,
                "sha256": digest,
                "bytes": size,
                "lifecycle": lifecycle,
                **proof,
                "message": (
                    "Created a verified Wan2.2 generative image-to-video clip. "
                    "It is source-conditioned motion, not a reusable 3D mesh."
                ),
            }
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _failure(
        self,
        request: Optional[VideoRequest],
        *,
        stage: str,
        executed: bool,
        source_verified: bool,
        message: str,
    ) -> dict[str, Any]:
        mode = request.mode if request is not None else _PROCEDURAL_MODE
        generative = mode == _I2V_MODE
        return {
            "ok": False,
            "status": "failed",
            "executed": executed,
            "success": False,
            "actual_video": False,
            "actual_generation": False,
            "verified": False,
            "source_preserved": not generative,
            "source_conditioned": generative,
            "source_verified": source_verified,
            "provider": _I2V_PROVIDER if generative else self.config.provider,
            "render_kind": _I2V_RENDER_KIND if generative else _RENDER_KIND,
            "mode": mode,
            "profile": request.profile if request is not None else _PROFILE,
            "model_id": _I2V_MODEL_ID if generative else None,
            "source_sha256": request.source_sha256 if request is not None else None,
            "stage": stage,
            "message": message[:1000],
        }

    async def generate(self, args: Any) -> dict:
        request: Optional[VideoRequest] = None
        source_verified = False
        stage = "validation"
        try:
            if not self.config.enabled:
                raise VideoGenerationError("Video creation is disabled.")
            request = _validated_args(self.config, args)
            stage = "source_verification"
            source, width, height, _source_size = await asyncio.to_thread(
                _source_png, self.config, request.source_sha256
            )
            source_verified = True
            if request.mode == _I2V_MODE:
                # _generate_i2v returns its own executed:false preflight
                # failures. Any exception escaping it occurred after readiness
                # passed and therefore represents an attempted execution.
                stage = "generative_execution"
                return await self._generate_i2v(
                    request, source, source_width=width, source_height=height
                )
            stage = "procedural_render"
            return await self._generate_procedural(request, source, width, height)
        except asyncio.CancelledError:
            raise
        except (VideoGenerationError, ImageGenerationError, WorkerSwapError) as exc:
            return self._failure(
                request,
                stage=stage,
                executed=stage not in {"validation", "source_verification"},
                source_verified=source_verified,
                message=str(exc),
            )
        except (OSError, ValueError, httpx.HTTPError) as exc:
            return self._failure(
                request,
                stage=stage,
                executed=stage not in {"validation", "source_verification"},
                source_verified=source_verified,
                message=f"Video creation failed ({type(exc).__name__}).",
            )
