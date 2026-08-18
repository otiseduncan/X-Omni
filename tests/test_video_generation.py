from __future__ import annotations

import hashlib
import json
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from core.api.routes import create_router
from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import Orchestrator, artifact_type_for_tool
from core.services import video_generation as videos
from core.services.video_generation import (
    VideoGenerationConfig,
    VideoGenerationError,
    VideoGenerationService,
)
from core.state.db import Store as StateStore
from core.tools.registry import TOOL_SCHEMAS, Registry


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + kind + payload + checksum.to_bytes(4, "big")


def _png(width: int = 64, height: int = 64) -> bytes:
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 2, 0, 0, 0])
    )
    row = b"\x00" + (b"\x22\x66\xaa" * width)
    pixels = zlib.compress(row * height, level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + (b"verified-video" * 16)


def _config(
    tmp_path: Path,
    *,
    max_output_bytes: int = 1024 * 1024,
    i2v_enabled: bool = False,
) -> VideoGenerationConfig:
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    ffmpeg = tools / "ffmpeg.exe"
    ffprobe = tools / "ffprobe.exe"
    ffmpeg.write_bytes(b"fixture")
    ffprobe.write_bytes(b"fixture")
    (tmp_path / "data" / "generated-images").mkdir(parents=True)
    config_path = tmp_path / "video.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "provider": "ffmpeg-exact-local",
                "ffmpeg_path": str(ffmpeg),
                "ffprobe_path": str(ffprobe),
                "source_dir": "data/generated-images",
                "output_dir": "data/generated-videos",
                "default_duration_seconds": 10,
                "fps": 24,
                "timeout_seconds": 60,
                "max_source_bytes": 1024 * 1024,
                "max_output_bytes": max_output_bytes,
                "image_to_video": {
                    "enabled": i2v_enabled,
                    "generation_timeout_seconds": 600,
                },
            }
        ),
        encoding="utf-8",
    )
    return VideoGenerationConfig.from_file(config_path, tmp_path)


def _source(config: VideoGenerationConfig, content: bytes | None = None) -> str:
    payload = content or _png()
    digest = hashlib.sha256(payload).hexdigest()
    (config.source_dir / f"{digest}.png").write_bytes(payload)
    return digest


def _success_result(digest: str = "a" * 64, source_digest: str = "b" * 64) -> dict:
    url = f"/api/generated-videos/{digest}.mp4"
    return {
        "ok": True,
        "status": "completed",
        "executed": True,
        "success": True,
        "actual_video": True,
        "actual_generation": False,
        "verified": True,
        "source_verified": True,
        "source_preserved": True,
        "source_conditioned": False,
        "provider": "ffmpeg-exact-local",
        "render_kind": "deterministic_exact_source_animation",
        "mode": "exact_source_animation",
        "profile": "hover_pulse",
        "source_sha256": source_digest,
        "video_url": url,
        "target": url,
        "mime_type": "video/mp4",
        "sha256": digest,
        "bytes": 1234,
        "codec": "h264",
        "pixel_format": "yuv420p",
        "width": 1024,
        "height": 1024,
        "fps": 24,
        "frame_count": 240,
        "duration_seconds": 10,
        "lifecycle": {
            "mode": "bounded_cpu_subprocess",
            "model_remained_available": True,
        },
    }


def _wan_asset_proof() -> dict:
    return {
        filename: {
            "verified": True,
            "bytes": expected["bytes"],
            "sha256": expected["sha256"],
        }
        for filename, expected in videos._I2V_OFFICIAL_ASSETS.items()
    }


def _probe_payload(
    size: int,
    *,
    width: int = 704,
    height: int = 704,
    duration: int = 10,
) -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": width,
                    "height": height,
                    "avg_frame_rate": "24/1",
                    "nb_frames": str(duration * 24),
                    "duration": f"{duration}.000000",
                }
            ],
            "format": {
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "duration": f"{duration}.000000",
                "size": str(size),
            },
        }
    ).encode()


def test_video_tool_schema_requires_explicit_mode_and_bounds_both_paths() -> None:
    parameters = TOOL_SCHEMAS["video_generate"]["parameters"]
    assert set(parameters["properties"]) == {
        "source_sha256",
        "duration_seconds",
        "mode",
        "profile",
        "prompt",
        "seed",
    }
    assert parameters["required"] == ["source_sha256", "mode"]
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["profile"]["enum"] == ["hover_pulse"]
    assert parameters["properties"]["mode"]["enum"] == [
        "image_to_video",
        "exact_source_animation",
    ]
    # Core enforces the exact digest, prompt, and JS-safe seed bounds. Those
    # keywords stay out of the advertised schema because llama.cpp b9906
    # cannot compile them alongside the sibling numeric fields.
    assert "maximum" not in parameters["properties"]["seed"]
    assert "pattern" not in parameters["properties"]["source_sha256"]
    assert "maxLength" not in parameters["properties"]["prompt"]


def test_wan_workflow_uses_only_fixed_builtin_nodes_and_slices_241_to_240() -> None:
    workflow = videos.WanTI2VProvider.workflow(
        prompt="the orb rotates with real depth",
        seed=123,
        duration_seconds=10,
        staged_filename="xomni_i2v_fixture.png",
        request_id="a" * 32,
        fps=24,
    )

    assert {node["class_type"] for node in workflow.values()} == {
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
    }
    assert workflow["8"]["inputs"] == {
        "vae": ["6", 0],
        "start_image": ["7", 0],
        "width": 704,
        "height": 704,
        "length": 241,
        "batch_size": 1,
    }
    assert workflow["11"]["inputs"]["length"] == 240
    assert workflow["12"]["inputs"] == {"images": ["11", 0], "fps": 24.0}
    assert workflow["9"]["inputs"]["steps"] == 20
    assert workflow["9"]["inputs"]["cfg"] == 5.0
    assert workflow["9"]["inputs"]["sampler_name"] == "uni_pc"
    assert workflow["13"]["inputs"]["format"] == "mp4"
    assert workflow["13"]["inputs"]["codec"] == "h264"


def test_wan_asset_proof_rejects_correct_size_with_wrong_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = videos._I2V_MODEL_FILE
    payload = b"not-official"
    monkeypatch.setitem(
        videos._I2V_OFFICIAL_ASSETS,
        filename,
        {"bytes": len(payload), "sha256": "0" * 64},
    )
    path = tmp_path / filename
    path.write_bytes(payload)

    proof = videos.WanTI2VProvider._verify_official_asset(path, filename)

    assert proof["present"] is True
    assert proof["bytes"] == len(payload)
    assert proof["verified"] is False
    assert proof["sha256"] == hashlib.sha256(payload).hexdigest()


def test_wan_video_reference_requires_exact_history_output_shape() -> None:
    request_id = "b" * 32
    valid = {
        "filename": f"{request_id}_00001_.mp4",
        "subfolder": "xomni_i2v",
        "type": "output",
    }
    assert videos.WanTI2VProvider._safe_video_reference(valid, request_id) == valid
    for invalid in (
        {**valid, "filename": "other_00001_.mp4"},
        {**valid, "filename": f"../{request_id}_00001_.mp4"},
        {**valid, "subfolder": "../private"},
        {**valid, "type": "temp"},
        {**valid, "filename": f"{request_id}_00001_.webm"},
    ):
        with pytest.raises(VideoGenerationError, match="unsafe video reference"):
            videos.WanTI2VProvider._safe_video_reference(invalid, request_id)


@pytest.mark.asyncio
async def test_wan_history_accepts_only_node_13_saved_mp4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    request_id = "c" * 32
    output = {
        "filename": f"{request_id}_00001_.mp4",
        "subfolder": "xomni_i2v",
        "type": "output",
    }
    temporary = config.output_dir / ".xomni-wan-history.mp4"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class RuntimeProvider:
        poll_interval_s = 0.001
        config = SimpleNamespace(
            runtime_root=tmp_path / "comfy",
            generation_timeout_s=600,
        )

        def _client(self, _timeout=None):
            return Client()

        async def _bounded_json(self, _client, method, path, **_kwargs):
            assert method in {"POST", "GET"}
            if path == "/prompt":
                return {"prompt_id": "prompt-1"}
            assert path == "/history/prompt-1"
            return {
                "prompt-1": {
                    "status": {"status_str": "success"},
                    "outputs": {"13": {"images": [output], "animated": [True]}},
                }
            }

        async def _cancel_prompt(self, *_args):
            raise AssertionError("successful history must not cancel")

    provider = videos.WanTI2VProvider(
        config, SimpleNamespace(), RuntimeProvider()
    )

    async def live_proof():
        return {"model_visible": True}

    async def download(_client, reference):
        assert reference == output
        config.output_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(_mp4())
        return temporary

    monkeypatch.setattr(provider, "_live_workflow_proof", live_proof)
    monkeypatch.setattr(provider, "_download_video", download)
    runtime = SimpleNamespace(active_prompt_id=None)

    result = await provider._execute_workflow(
        prompt="orb moves",
        seed=1,
        duration_seconds=10,
        staged_source=tmp_path / "xomni.png",
        request_id=request_id,
        runtime=runtime,
        generation={
            "submit_state": "not_attempted",
            "prompt_id_known": False,
            "prompt_delete_requested": False,
            "prompt_cancelled": None,
            "may_have_generated": False,
            "may_have_surviving_output": False,
            "output_removed": None,
        },
    )

    assert result == temporary
    assert runtime.active_prompt_id is None


@pytest.mark.asyncio
async def test_wan_pre_stop_failure_retains_not_submitted_lifecycle_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    lifecycle = {
        "mode": "sequential_exclusive",
        "previous_worker": "omni",
        "model_stop_attempted": True,
        "model_stopped": False,
        "model_restore_required": False,
        "model_restored": None,
        "gpu_indices": [0, 1],
    }

    class Router:
        @asynccontextmanager
        async def external_workload_session(self, _name):
            raise videos.ExternalWorkloadStartError(
                "Omni readiness could not be re-proved before the external model hand-off.",
                lifecycle=lifecycle,
                stage="model_stop_readiness",
                retryable=True,
            )
            yield  # pragma: no cover - required async-generator shape

    class RuntimeProvider:
        config = SimpleNamespace(runtime_root=tmp_path / "comfy")

        async def ensure_runtime(self):
            raise AssertionError("ComfyUI must not start before the model hand-off")

    provider = videos.WanTI2VProvider(config, Router(), RuntimeProvider())
    monkeypatch.setattr(provider, "_asset_proof_is_current", lambda _proof: True)

    with pytest.raises(videos.WanExecutionError) as captured:
        await provider.generate(
            source=config.source_dir / f"{source_digest}.png",
            source_sha256=source_digest,
            prompt="orb rotates",
            seed=4,
            duration_seconds=10,
            verified_asset_proof=_wan_asset_proof(),
        )

    failure = captured.value
    assert failure.lifecycle == {
        **lifecycle,
        "external_runtime": "not_started",
        "runtime_release_attempted": False,
        "runtime_released": None,
        "request_files_cleanup_attempted": True,
        "request_files_cleaned": True,
    }
    assert failure.generation == {
        "submit_state": "not_attempted",
        "prompt_id_known": False,
        "prompt_delete_requested": False,
        "prompt_cancelled": None,
        "may_have_generated": False,
        "may_have_surviving_output": False,
        "output_removed": None,
    }
    assert failure.stage == "model_stop_readiness"
    assert failure.retryable is True


@pytest.mark.asyncio
async def test_wan_prompt_connect_timeout_is_single_shot_and_retains_indeterminacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    prompt_posts = 0

    class Router:
        @asynccontextmanager
        async def external_workload_session(self, _name):
            lease = {
                "previous_worker": "omni",
                "gpu_indices": [0, 1],
                "model_stop_attempted": True,
                "model_stopped": True,
                "model_restore_required": True,
                "model_restored": False,
            }
            try:
                yield lease
            finally:
                lease["model_restored"] = True

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class RuntimeProvider:
        poll_interval_s = 0.001
        config = SimpleNamespace(runtime_root=tmp_path / "comfy")
        released = False

        async def ensure_runtime(self):
            return SimpleNamespace(spawned=True, active_prompt_id=None)

        async def release_runtime(self, _handle):
            self.released = True

        def _client(self, _timeout=None):
            return Client()

        async def _bounded_json(self, _client, method, path, **_kwargs):
            nonlocal prompt_posts
            assert method == "POST"
            assert path == "/prompt"
            prompt_posts += 1
            raise httpx.ConnectTimeout("indeterminate prompt response")

        async def _cancel_prompt(self, *_args):
            raise AssertionError("an unknown prompt id cannot be cancelled by id")

    runtime = RuntimeProvider()
    provider = videos.WanTI2VProvider(config, Router(), runtime)
    monkeypatch.setattr(provider, "_asset_proof_is_current", lambda _proof: True)

    async def live_proof():
        return {"model_visible": True}

    monkeypatch.setattr(provider, "_live_workflow_proof", live_proof)

    with pytest.raises(videos.WanExecutionError) as captured:
        await provider.generate(
            source=config.source_dir / f"{source_digest}.png",
            source_sha256=source_digest,
            prompt="orb rotates",
            seed=4,
            duration_seconds=10,
            verified_asset_proof=_wan_asset_proof(),
        )

    failure = captured.value
    assert prompt_posts == 1
    assert runtime.released is True
    assert failure.lifecycle["model_stopped"] is True
    assert failure.lifecycle["model_restored"] is True
    assert failure.lifecycle["runtime_released"] is True
    assert failure.lifecycle["request_files_cleaned"] is True
    assert failure.generation["submit_state"] == "indeterminate"
    assert failure.generation["prompt_id_known"] is False
    assert failure.generation["may_have_generated"] is True
    assert failure.generation["may_have_surviving_output"] is False
    assert failure.generation["prompt_cancelled"] is None


@pytest.mark.asyncio
async def test_real_wan_provider_success_proves_restore_and_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)

    class Router:
        restored = False

        @asynccontextmanager
        async def external_workload_session(self, name):
            assert name == "video_generation"
            lease = {
                "previous_worker": "omni",
                "gpu_indices": [0, 1],
                "model_stopped": True,
                "model_restored": False,
            }
            try:
                yield lease
            finally:
                lease["model_restored"] = True
                self.restored = True

    class RuntimeProvider:
        config = SimpleNamespace(runtime_root=tmp_path / "comfy")
        released = False

        async def ensure_runtime(self):
            return SimpleNamespace(spawned=True, active_prompt_id=None)

        async def release_runtime(self, _handle):
            self.released = True

    router = Router()
    runtime = RuntimeProvider()
    provider = videos.WanTI2VProvider(config, router, runtime)
    monkeypatch.setattr(provider, "_asset_proof_is_current", lambda _proof: True)

    async def execute(**_kwargs):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = config.output_dir / ".xomni-wan-provider-success.mp4"
        temporary.write_bytes(_mp4())
        return temporary

    monkeypatch.setattr(provider, "_execute_workflow", execute)
    result = await provider.generate(
        source=config.source_dir / f"{source_digest}.png",
        source_sha256=source_digest,
        prompt="orb rotates",
        seed=4,
        duration_seconds=10,
        verified_asset_proof=_wan_asset_proof(),
    )

    assert result["temporary_path"].is_file()
    assert result["lifecycle"]["model_restored"] is True
    assert result["model_assets"] == _wan_asset_proof()
    assert runtime.released is True
    assert router.restored is True
    assert not list((tmp_path / "comfy" / "ComfyUI" / "input").glob("xomni_i2v_*.png"))
    result["temporary_path"].unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["release", "restore", "cleanup"])
async def test_real_wan_provider_preserves_completed_temp_on_lifecycle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)

    class Router:
        @asynccontextmanager
        async def external_workload_session(self, _name):
            lease = {
                "previous_worker": "omni",
                "gpu_indices": [0, 1],
                "model_stopped": True,
                "model_restored": False,
            }
            try:
                yield lease
            finally:
                if failure_kind == "restore":
                    raise videos.WorkerSwapError("restore failed")
                lease["model_restored"] = True

    class RuntimeProvider:
        config = SimpleNamespace(runtime_root=tmp_path / "comfy")

        async def ensure_runtime(self):
            return SimpleNamespace(spawned=True, active_prompt_id=None)

        async def release_runtime(self, _handle):
            if failure_kind == "release":
                raise videos.ImageGenerationError("release failed")

    provider = videos.WanTI2VProvider(config, Router(), RuntimeProvider())
    monkeypatch.setattr(provider, "_asset_proof_is_current", lambda _proof: True)

    async def execute(**_kwargs):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = config.output_dir / f".xomni-wan-{failure_kind}.mp4"
        temporary.write_bytes(_mp4())
        return temporary

    monkeypatch.setattr(provider, "_execute_workflow", execute)
    if failure_kind == "cleanup":
        def cleanup_failure(*_args):
            raise VideoGenerationError("owned cleanup failed")
        monkeypatch.setattr(provider, "_cleanup_request_files", cleanup_failure)

    with pytest.raises(videos.WanLifecycleError) as captured:
        await provider.generate(
            source=config.source_dir / f"{source_digest}.png",
            source_sha256=source_digest,
            prompt="orb rotates",
            seed=4,
            duration_seconds=10,
            verified_asset_proof=_wan_asset_proof(),
        )

    failure = captured.value
    assert failure.temporary_path.is_file()
    assert failure.lifecycle["model_restored"] is (failure_kind != "restore")
    assert failure.model_assets == _wan_asset_proof()
    failure.temporary_path.unlink()


@pytest.mark.asyncio
async def test_real_wan_provider_cancellation_after_output_restores_and_deletes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    release_started = videos.asyncio.Event()

    class Router:
        restored = False

        @asynccontextmanager
        async def external_workload_session(self, _name):
            lease = {
                "previous_worker": "omni",
                "gpu_indices": [0, 1],
                "model_stopped": True,
                "model_restored": False,
            }
            try:
                yield lease
            finally:
                lease["model_restored"] = True
                self.restored = True

    class RuntimeProvider:
        config = SimpleNamespace(runtime_root=tmp_path / "comfy")

        async def ensure_runtime(self):
            return SimpleNamespace(spawned=True, active_prompt_id=None)

        async def release_runtime(self, _handle):
            release_started.set()
            await videos.asyncio.sleep(0.05)

    router = Router()
    provider = videos.WanTI2VProvider(config, router, RuntimeProvider())
    monkeypatch.setattr(provider, "_asset_proof_is_current", lambda _proof: True)

    async def execute(**_kwargs):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = config.output_dir / ".xomni-wan-cancel.mp4"
        temporary.write_bytes(_mp4())
        return temporary

    monkeypatch.setattr(provider, "_execute_workflow", execute)
    task = videos.asyncio.create_task(
        provider.generate(
            source=config.source_dir / f"{source_digest}.png",
            source_sha256=source_digest,
            prompt="orb rotates",
            seed=4,
            duration_seconds=10,
            verified_asset_proof=_wan_asset_proof(),
        )
    )
    await videos.asyncio.wait_for(release_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(videos.WanCancellationError) as captured:
        await task

    assert router.restored is True
    assert captured.value.lifecycle["model_restored"] is True
    assert captured.value.lifecycle["runtime_released"] is True
    assert captured.value.lifecycle["request_files_cleaned"] is True
    assert not list(config.output_dir.glob(".xomni-wan-*.mp4"))
    assert not list((tmp_path / "comfy" / "ComfyUI" / "input").glob("xomni_i2v_*.png"))


def test_config_confines_artifact_stores_to_app_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    raw = json.loads((tmp_path / "video.json").read_text(encoding="utf-8"))
    raw["output_dir"] = str(tmp_path.parent / "escape")
    (tmp_path / "video.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(VideoGenerationError, match="inside the X Omni root"):
        VideoGenerationConfig.from_file(tmp_path / "video.json", tmp_path)
    assert config.provider == "ffmpeg-exact-local"


@pytest.mark.asyncio
async def test_status_is_non_disruptive_and_reports_exact_dependencies(tmp_path: Path) -> None:
    service = VideoGenerationService(_config(tmp_path))

    status = await service.status({})

    assert status["generation_available"] is True
    assert status["actual_generation"] is False
    assert status["profile"] == "hover_pulse"
    assert status["source_preserving"] is True


@pytest.mark.asyncio
async def test_generate_uses_fixed_cpu_command_and_returns_strict_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_digest = _source(config)
    mp4 = _mp4()
    seen_commands: list[list[str]] = []

    async def fake_run(args, *, timeout_seconds, capture_stdout):
        seen_commands.append(list(args))
        if Path(args[0]) == config.ffmpeg_path:
            assert capture_stdout is False
            Path(args[-1]).write_bytes(mp4)
            return b"", b""
        assert Path(args[0]) == config.ffprobe_path
        assert capture_stdout is True
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv420p",
                        "width": 64,
                        "height": 64,
                        "avg_frame_rate": "24/1",
                        "nb_frames": "72",
                        "duration": "3.000000",
                    }
                ],
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "3.000000",
                    "size": str(len(mp4)),
                },
            }
        ).encode(), b""

    monkeypatch.setattr(videos, "_run_process", fake_run)
    result = await VideoGenerationService(config).generate(
        {
            "source_sha256": source_digest,
            "duration_seconds": 3,
            "mode": "exact_source_animation",
            "profile": "hover_pulse",
        }
    )

    assert result["ok"] is True, result["message"]
    assert result["actual_video"] is True
    assert result["actual_generation"] is False
    assert result["source_verified"] is True
    assert result["provider"] == "ffmpeg-exact-local"
    assert result["render_kind"] == "deterministic_exact_source_animation"
    assert result["mode"] == "exact_source_animation"
    assert result["codec"] == "h264"
    assert result["pixel_format"] == "yuv420p"
    assert result["frame_count"] == 72
    assert result["lifecycle"] == {
        "mode": "bounded_cpu_subprocess",
        "model_remained_available": True,
    }
    assert (config.output_dir / f"{result['sha256']}.mp4").read_bytes() == mp4

    command = seen_commands[0]
    assert command[0] == str(config.ffmpeg_path)
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-fs") + 1] == str(config.max_output_bytes)
    assert command[command.index("-frames:v") + 1] == "72"
    assert "hover_pulse" not in " ".join(command)
    assert source_digest in command[command.index("-i") + 1]


@pytest.mark.asyncio
async def test_i2v_success_is_true_generation_with_restore_and_asset_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    mp4 = _mp4()
    seen_processes: list[Path] = []

    class WanProvider:
        async def status(self):
            return {
                "ok": True,
                "generation_available": True,
                "state": "configured_unverified",
            }

        async def generate(self, **kwargs):
            assert kwargs["source_sha256"] == source_digest
            assert kwargs["prompt"] == "the orb rotates with real depth"
            assert kwargs["seed"] == 77
            temporary = config.output_dir / ".xomni-wan-success.mp4"
            config.output_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(mp4)
            return {
                "temporary_path": temporary,
                "model_assets": _wan_asset_proof(),
                "lifecycle": {
                    "mode": "sequential_exclusive",
                    "model_stopped": True,
                    "model_restored": True,
                    "gpu_indices": [0, 1],
                    "previous_worker": "omni",
                    "external_runtime": "spawned",
                },
            }

    async def fake_run(args, *, timeout_seconds, capture_stdout):
        del timeout_seconds
        seen_processes.append(Path(args[0]))
        assert Path(args[0]) == config.ffprobe_path
        assert capture_stdout is True
        return _probe_payload(len(mp4)), b""

    monkeypatch.setattr(videos, "_run_process", fake_run)
    result = await VideoGenerationService(
        config, wan_provider=WanProvider()
    ).generate(
        {
            "source_sha256": source_digest,
            "mode": "image_to_video",
            "duration_seconds": 10,
            "prompt": "the orb rotates with real depth",
            "seed": 77,
        }
    )

    assert result["ok"] is True, result["message"]
    assert result["actual_generation"] is True
    assert result["source_conditioned"] is True
    assert result["source_preserved"] is False
    assert result["provider"] == "comfyui-wan2.2-ti2v-5b-local"
    assert result["render_kind"] == "generative_image_to_video"
    assert result["mode"] == "image_to_video"
    assert result["model_assets"] == _wan_asset_proof()
    assert result["lifecycle"]["model_restored"] is True
    assert result["width"] == result["height"] == 704
    assert result["frame_count"] == 240
    assert result["duration_seconds"] == 10
    assert seen_processes == [config.ffprobe_path]
    assert Registry._approved_result_error("video_generate", result) is None
    assert artifact_type_for_tool("video_generate", result) == "generated_video"

    tampered = json.loads(json.dumps(result))
    tampered["model_assets"][videos._I2V_MODEL_FILE]["sha256"] = "0" * 64
    assert Registry._approved_result_error("video_generate", tampered)
    assert artifact_type_for_tool("video_generate", tampered) == "video_generation_status"


@pytest.mark.asyncio
async def test_i2v_runtime_failure_is_executed_and_never_falls_back_to_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)

    class WanProvider:
        async def status(self):
            return {"ok": True, "generation_available": True}

        async def generate(self, **_kwargs):
            raise VideoGenerationError("Wan2.2 execution failed.")

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("I2V failure must never run procedural FFmpeg")

    monkeypatch.setattr(videos, "_run_process", must_not_run)
    result = await VideoGenerationService(
        config, wan_provider=WanProvider()
    ).generate(
        {
            "source_sha256": source_digest,
            "mode": "image_to_video",
            "duration_seconds": 10,
        }
    )

    assert result["ok"] is False
    assert result["executed"] is True
    assert result["actual_video"] is False
    assert result["actual_generation"] is False
    assert result["mode"] == "image_to_video"
    assert result["provider"] == "comfyui-wan2.2-ti2v-5b-local"
    assert "Wan2.2 execution failed" in result["message"]


@pytest.mark.asyncio
async def test_i2v_structured_execution_failure_reaches_terminal_result(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    lifecycle = {
        "mode": "sequential_exclusive",
        "previous_worker": "omni",
        "model_stop_attempted": True,
        "model_stopped": False,
        "model_restore_required": False,
        "model_restored": None,
        "gpu_indices": [0, 1],
        "external_runtime": "not_started",
        "runtime_release_attempted": False,
        "runtime_released": None,
        "request_files_cleanup_attempted": True,
        "request_files_cleaned": True,
    }
    generation = {
        "submit_state": "not_attempted",
        "prompt_id_known": False,
        "prompt_delete_requested": False,
        "prompt_cancelled": None,
        "may_have_generated": False,
        "may_have_surviving_output": False,
        "output_removed": None,
    }

    class WanProvider:
        async def status(self):
            return {"ok": True, "generation_available": True}

        async def generate(self, **_kwargs):
            raise videos.WanExecutionError(
                "Omni readiness could not be re-proved before the external model hand-off.",
                lifecycle=lifecycle,
                generation=generation,
                stage="model_stop_readiness",
                retryable=True,
            )

    result = await VideoGenerationService(
        config, wan_provider=WanProvider()
    ).generate(
        {
            "source_sha256": source_digest,
            "mode": "image_to_video",
            "duration_seconds": 10,
        }
    )

    assert result["ok"] is False
    assert result["executed"] is True
    assert result["stage"] == "model_stop_readiness"
    assert result["retryable"] is True
    assert "Wan" not in result["message"]
    assert result["lifecycle"] == lifecycle
    assert result["generation"] == generation


@pytest.mark.asyncio
async def test_cancelled_wan_execution_persists_structured_cleanup_receipt(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    lifecycle = {
        "mode": "sequential_exclusive",
        "previous_worker": "omni",
        "model_stop_attempted": True,
        "model_stopped": True,
        "model_restore_required": True,
        "model_restored": True,
        "gpu_indices": [0, 1],
        "external_runtime": "spawned",
        "runtime_release_attempted": True,
        "runtime_released": True,
        "request_files_cleanup_attempted": True,
        "request_files_cleaned": True,
    }
    generation = {
        "submit_state": "accepted",
        "prompt_id_known": True,
        "prompt_delete_requested": True,
        "prompt_cancelled": True,
        "may_have_generated": True,
        "may_have_surviving_output": False,
        "output_removed": False,
    }

    class WanProvider:
        async def status(self):
            return {"ok": True, "generation_available": True}

        async def generate(self, **_kwargs):
            raise videos.WanCancellationError(
                lifecycle=lifecycle,
                generation=generation,
            )

    service = VideoGenerationService(config, wan_provider=WanProvider())
    policy = tmp_path / "tools.yaml"
    policy.write_text(
        "tools:\n  video_generate:\n    tier: confirm_required\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.sqlite")
    registry = Registry(policy, store=store)
    registry.register("video_generate", service.generate)
    conversation_id = store.create_conversation("cancelled Wan")
    message_id = store.add_message(conversation_id, "user", "generate video")
    approval_id = store.create_approval(
        "video_generate",
        "Generate video",
        {
            "name": "video_generate",
            "args": {
                "source_sha256": source_digest,
                "mode": "image_to_video",
                "duration_seconds": 10,
            },
        },
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="video-call",
    )

    with pytest.raises(videos.asyncio.CancelledError):
        await registry.resolve_approval(
            approval_id,
            True,
            conversation_id=conversation_id,
            session_id="session-a",
            user_id="owner-a",
        )

    snapshot = store.approval_snapshot(approval_id)
    result = snapshot["receipt"]["result"]
    assert result["execution_state"] == "cancelled"
    assert result["may_have_executed"] is True
    assert result["lifecycle"] == lifecycle
    assert result["generation"] == generation
    public = registry.public_approval(
        snapshot["approval"], receipt=snapshot["receipt"]
    )
    assert public["execution_state"] == "cancelled"
    assert public["may_have_executed"] is True
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_restored", [False, True])
async def test_post_output_restore_or_release_failure_retains_verified_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_restored: bool,
) -> None:
    config = _config(tmp_path, i2v_enabled=True)
    source_digest = _source(config)
    mp4 = _mp4()

    class WanProvider:
        async def status(self):
            return {"ok": True, "generation_available": True}

        async def generate(self, **_kwargs):
            temporary = config.output_dir / ".xomni-wan-partial.mp4"
            config.output_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(mp4)
            raise videos.WanLifecycleError(
                "model restore failed" if not model_restored else "runtime release failed",
                temporary_path=temporary,
                lifecycle={
                    "mode": "sequential_exclusive",
                    "model_stopped": True,
                    "model_restored": model_restored,
                    "gpu_indices": [0, 1],
                    "previous_worker": "omni",
                    "external_runtime": "spawned",
                },
                model_assets=_wan_asset_proof(),
            )

    async def fake_run(args, *, timeout_seconds, capture_stdout):
        del timeout_seconds
        assert Path(args[0]) == config.ffprobe_path
        assert capture_stdout is True
        return _probe_payload(len(mp4)), b""

    monkeypatch.setattr(videos, "_run_process", fake_run)
    result = await VideoGenerationService(
        config, wan_provider=WanProvider()
    ).generate(
        {
            "source_sha256": source_digest,
            "mode": "image_to_video",
            "duration_seconds": 10,
            "seed": 9,
        }
    )

    assert result["ok"] is False
    assert result["success"] is False
    assert result["executed"] is True
    assert result["actual_video"] is True
    assert result["actual_generation"] is True
    assert result["verified"] is True
    assert result["lifecycle"]["model_restored"] is model_restored
    assert result["sha256"] == hashlib.sha256(mp4).hexdigest()
    assert (config.output_dir / f"{result['sha256']}.mp4").read_bytes() == mp4
    assert Registry._approved_result_error("video_generate", result)
    assert artifact_type_for_tool("video_generate", result) == "video_generation_status"


@pytest.mark.asyncio
async def test_invalid_source_or_user_command_fields_never_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_digest = _source(config)

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("subprocess must not start")

    monkeypatch.setattr(videos, "_run_process", must_not_run)
    service = VideoGenerationService(config)
    for args in (
        {"source_sha256": source_digest},
        {"source_sha256": "A" * 64, "mode": "exact_source_animation"},
        {"source_sha256": source_digest, "mode": "exact_source_animation", "filter": "movie=secret.txt"},
        {"source_sha256": source_digest, "mode": "exact_source_animation", "duration_seconds": 11},
        {"source_sha256": source_digest, "mode": "exact_source_animation", "profile": "user_filter"},
    ):
        result = await service.generate(args)
        assert result["ok"] is False
        assert result["executed"] is False
        assert result["actual_video"] is False


@pytest.mark.asyncio
async def test_oserror_is_projected_without_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_digest = _source(config)

    async def fail_with_path(*_args, **_kwargs):
        raise OSError(f"access denied: {tmp_path / 'private-source.png'}")

    monkeypatch.setattr(videos, "_run_process", fail_with_path)
    result = await VideoGenerationService(config).generate(
        {"source_sha256": source_digest, "mode": "exact_source_animation", "duration_seconds": 2}
    )

    assert result["ok"] is False
    assert result["executed"] is True
    assert result["message"] == "Video creation failed (OSError)."
    assert str(tmp_path) not in result["message"]


@pytest.mark.asyncio
async def test_cancellation_waits_for_owned_process_termination_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_digest = _source(config)
    spawned = videos.asyncio.Event()
    finished = videos.asyncio.Event()

    class BlockingStream:
        async def read(self, _size):
            await finished.wait()
            return b""

    class OwnedProcess:
        returncode = None
        stdout = None
        stderr = BlockingStream()
        terminated = False
        killed = False
        termination_waited = False

        async def wait(self):
            await finished.wait()
            self.returncode = 0
            if self.terminated:
                self.termination_waited = True
            return 0

        def terminate(self):
            self.terminated = True
            finished.set()

        def kill(self):
            self.killed = True
            finished.set()

    process = OwnedProcess()

    async def fake_spawn(*_args, **_kwargs):
        spawned.set()
        return process

    monkeypatch.setattr(videos.asyncio, "create_subprocess_exec", fake_spawn)
    task = videos.asyncio.create_task(
        VideoGenerationService(config).generate(
                {"source_sha256": source_digest, "mode": "exact_source_animation", "duration_seconds": 2}
        )
    )
    await videos.asyncio.wait_for(spawned.wait(), timeout=2)
    assert list(config.output_dir.glob(".xomni-video-*.mp4"))

    task.cancel()
    with pytest.raises(videos.asyncio.CancelledError):
        await task

    assert process.terminated is True
    assert process.killed is False
    assert process.termination_waited is True
    assert list(config.output_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_probe_leaves_no_final_or_temporary_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source_digest = _source(config)
    mp4 = _mp4()

    async def fake_run(args, *, timeout_seconds, capture_stdout):
        if Path(args[0]) == config.ffmpeg_path:
            Path(args[-1]).write_bytes(mp4)
            return b"", b""
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "pix_fmt": "yuv444p",
                        "width": 64,
                        "height": 64,
                        "avg_frame_rate": "24/1",
                        "nb_frames": "48",
                        "duration": "2.0",
                    }
                ],
                "format": {
                    "format_name": "mp4",
                    "duration": "2.0",
                    "size": str(len(mp4)),
                },
            }
        ).encode(), b""

    monkeypatch.setattr(videos, "_run_process", fake_run)
    result = await VideoGenerationService(config).generate(
        {"source_sha256": source_digest, "mode": "exact_source_animation", "duration_seconds": 2}
    )

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["actual_video"] is False
    assert "pixel-format" in result["message"]
    assert list(config.output_dir.iterdir()) == []


def test_probe_rejects_nan_and_divergent_stream_duration() -> None:
    base = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 64,
                "height": 64,
                "avg_frame_rate": "24/1",
                "nb_frames": "48",
                "duration": "2.0",
            }
        ],
        "format": {"format_name": "mp4", "duration": "2.0", "size": "100"},
    }
    bad_nan = json.loads(json.dumps(base))
    bad_nan["format"]["duration"] = "nan"
    bad_stream = json.loads(json.dumps(base))
    bad_stream["streams"][0]["duration"] = "1.0"

    for payload in (bad_nan, bad_stream):
        with pytest.raises(VideoGenerationError):
            videos._verify_probe(
                json.dumps(payload).encode(),
                width=64,
                height=64,
                fps=24,
                frame_count=48,
                duration_seconds=2,
                actual_size=100,
            )


def test_registry_and_artifact_mapping_fail_closed_on_near_truth() -> None:
    result = _success_result()
    assert Registry._approved_result_error("video_generate", result) is None
    assert artifact_type_for_tool("video_generate", result) == "generated_video"

    variants = [
        {**result, "actual_video": 1},
        {**result, "actual_generation": 0},
        {**result, "source_preserved": 1},
        {**result, "fps": 24.0},
        {**result, "frame_count": 240.0},
        {**result, "bytes": True},
        {**result, "pixel_format": "yuv444p"},
        {
            **result,
            "lifecycle": {
                "mode": "bounded_cpu_subprocess",
                "model_remained_available": 1,
            },
        },
    ]
    for candidate in variants:
        assert Registry._approved_result_error("video_generate", candidate)
        assert artifact_type_for_tool("video_generate", candidate) == "video_generation_status"


def _route_app(config: VideoGenerationConfig, *, authorized: bool = True) -> FastAPI:
    async def require_session():
        if not authorized:
            raise HTTPException(401, "Sign in required")
        return {"token_hash": "owner"}

    app = FastAPI()
    app.include_router(
        create_router(
            SimpleNamespace(local_origin="http://127.0.0.1:8100", public_origin=""),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(policy={}, roots=[], _handlers={}),
            require_session,
            video_config=config,
        )
    )
    return app


@pytest.mark.asyncio
async def test_generated_video_route_is_authenticated_integrity_checked_and_range_capable(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.output_dir.mkdir(parents=True)
    payload = _mp4()
    digest = hashlib.sha256(payload).hexdigest()
    (config.output_dir / f"{digest}.mp4").write_bytes(payload)
    app = _route_app(config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        full = await client.get(f"/api/generated-videos/{digest}.mp4")
        partial = await client.get(
            f"/api/generated-videos/{digest}.mp4", headers={"Range": "bytes=4-15"}
        )
        suffix = await client.get(
            f"/api/generated-videos/{digest}.mp4", headers={"Range": "bytes=-7"}
        )
        multi = await client.get(
            f"/api/generated-videos/{digest}.mp4", headers={"Range": "bytes=0-1,4-5"}
        )
        huge = await client.get(
            f"/api/generated-videos/{digest}.mp4",
            headers={"Range": f"bytes={'9' * 10000}-"},
        )

    assert full.status_code == 200
    assert full.content == payload
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["etag"] == f'"{digest}"'
    assert partial.status_code == 206
    assert partial.content == payload[4:16]
    assert partial.headers["content-range"] == f"bytes 4-15/{len(payload)}"
    assert suffix.status_code == 206
    assert suffix.content == payload[-7:]
    for rejected in (multi, huge):
        assert rejected.status_code == 416
        assert rejected.headers["content-range"] == f"bytes */{len(payload)}"

    unauthorized_app = _route_app(config, authorized=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unauthorized_app), base_url="http://test"
    ) as client:
        unauthorized = await client.get(f"/api/generated-videos/{digest}.mp4")
    assert unauthorized.status_code == 401

    (config.output_dir / f"{digest}.mp4").write_bytes(payload + b"tampered")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        tampered = await client.get(f"/api/generated-videos/{digest}.mp4")
    assert tampered.status_code == 404


@pytest.mark.asyncio
async def test_approved_video_success_is_persisted_then_uses_fixed_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _success_result()
    receipt = {
        "id": "receipt-1",
        "approval_id": "approval-1",
        "tool_name": "video_generate",
        "status": "succeeded",
        "executed": True,
        "success": True,
        "result": dict(result),
    }

    class Store:
        persisted = False
        saved = None

        def get_messages(self, _conversation_id):
            return []

        def add_message(self, *args, **kwargs):
            self.persisted = True
            self.saved = (args, kwargs)
            return 77

        def touch_conversation(self, *_args, **_kwargs):
            return None

    class Client:
        async def stream(self, *_args, **_kwargs):
            raise AssertionError("verified video success must not call the model")
            yield  # pragma: no cover

    store = Store()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    orchestrator = Orchestrator(
        SimpleNamespace(active_name="omni"),
        Client(),
        SimpleNamespace(model_tools=lambda: []),
        store,
        SimpleNamespace(context_tokens=32000, max_response_tokens=1000),
    )
    approved = {
        "name": "video_generate",
        "args": {
            "source_sha256": result["source_sha256"],
            "mode": "exact_source_animation",
        },
        "result": result,
        "receipt": receipt,
        "call_id": "video-call-1",
    }
    stream = orchestrator._run(1, "Animate that image", approved, None)

    first = await anext(stream)
    assert store.persisted is True
    events = [first]
    events.extend([event async for event in stream])

    summary = "The verified procedural source animation is ready in the chat card."
    assert [event for event in events if event.get("type") == "token"] == [
        {"type": "token", "text": summary}
    ]
    assert all("http" not in str(event.get("text") or "") for event in events)
    assert events[-1]["type"] == "done"
    saved_args, saved_kwargs = store.saved
    assert saved_args[2] == summary
    assert [item["type"] for item in saved_kwargs["artifacts"]] == [
        "execution_receipt",
        "generated_video",
    ]
