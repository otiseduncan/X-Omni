from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from core.models.router import LiveProof, ModelRouter, ProcessRecord, WorkerConfig, WorkerSwapError
from core.orchestrator.loop import artifact_type_for_tool
from core.services import image_generation as images
from core.services.image_generation import (
    ComfyProcessRecord,
    ComfyUIProvider,
    ImageGenerationConfig,
    ImageGenerationError,
    ImageGenerationService,
    RuntimeHandle,
)
from core.state.db import Store
from core.tools.registry import TOOL_SCHEMAS, Registry


def test_image_tool_schema_avoids_llama_cpp_multifield_maxlength_grammar_bug():
    parameters = TOOL_SCHEMAS["image_generate"]["parameters"]
    assert set(parameters["properties"]) == {"prompt", "width", "height", "seed"}
    assert "maxLength" not in parameters["properties"]["prompt"]
    assert parameters["required"] == ["prompt"]


def test_adult_artistic_nude_is_not_blanket_blocked() -> None:
    prompt = "An artistic nude figure study of an adult model in a classical painting style"
    validated, width, height, seed = ImageGenerationService._validated_args({
        "prompt": prompt,
        "width": 512,
        "height": 512,
        "seed": 7,
    })
    assert (validated, width, height, seed) == (prompt, 512, 512, 7)


@pytest.mark.parametrize(
    "prompt",
    [
        "Sexualized nude portrait of an underage child",
        "A nonconsensual sexual assault scene involving adults",
    ],
)
def test_high_risk_image_boundaries_remain_narrow(prompt: str) -> None:
    with pytest.raises(ImageGenerationError, match="high-risk content boundary"):
        ImageGenerationService._validated_args({"prompt": prompt})


def test_image_generation_remains_approval_gated() -> None:
    assert Registry("config/tools.yaml").tier("image_generate") == "confirm_required"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + kind + payload + crc.to_bytes(4, "big")


def _png(width: int = 512, height: int = 512) -> bytes:
    header = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 2, 0, 0, 0])
    )
    row = b"\x00" + (b"\x22\x66\xaa" * width)
    pixels = zlib.compress(row * height, level=9)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", pixels) + _chunk(b"IEND", b"")


def _config(tmp_path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> ImageGenerationConfig:
    runtime = tmp_path / "comfy"
    (runtime / "python_embeded").mkdir(parents=True)
    (runtime / "ComfyUI" / "models" / "checkpoints").mkdir(parents=True)
    (runtime / "python_embeded" / "python.exe").write_bytes(b"fixture")
    (runtime / "ComfyUI" / "main.py").write_text("# fixture", encoding="utf-8")
    checkpoint = "fixture.safetensors"
    (runtime / "ComfyUI" / "models" / "checkpoints" / checkpoint).write_bytes(b"weights")
    config_path = tmp_path / "image.json"
    config_path.write_text(
        json.dumps({
            "enabled": True,
            "provider": "comfyui",
            "host": "127.0.0.1",
            "port": 18188,
            "runtime_root": str(runtime),
            "checkpoint": checkpoint,
            "output_dir": "data/generated-images",
            "default_width": 512,
            "default_height": 512,
            "startup_timeout_seconds": 30,
            "generation_timeout_seconds": 30,
            "shutdown_timeout_seconds": 5,
            "max_output_bytes": max_bytes,
        }),
        encoding="utf-8",
    )
    return ImageGenerationConfig.from_file(config_path, tmp_path)


def _record(config: ImageGenerationConfig, pid: int = 321) -> ComfyProcessRecord:
    return ComfyProcessRecord(
        pid=pid,
        executable=config.python_executable,
        command_line=config.command,
        cwd=config.runtime_root,
        started_at=1234.5,
    )


def _success_result(digest: str = "a" * 64) -> dict:
    url = f"/api/generated-images/{digest}.png"
    return {
        "ok": True,
        "status": "completed",
        "executed": True,
        "success": True,
        "actual_generation": True,
        "verified": True,
        "provider": "comfyui-sdxl-local",
        "image_url": url,
        "target": url,
        "mime_type": "image/png",
        "sha256": digest,
        "bytes": 1234,
        "width": 512,
        "height": 512,
        "seed": 7,
        "prompt": "a blue workshop",
        "lifecycle": {
            "mode": "sequential_exclusive",
            "model_stopped": True,
            "model_restored": True,
            "gpu_indices": [0, 1],
        },
    }


@pytest.mark.asyncio
async def test_non_disruptive_status_reports_configured_stopped_without_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ComfyUIProvider(_config(tmp_path))
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: None)
    monkeypatch.setattr(
        provider, "_spawn", lambda: (_ for _ in ()).throw(AssertionError("must not start"))
    )

    status = await provider.status()

    assert status["state"] == "configured_stopped"
    assert status["generation_available"] is True
    assert status["live"] is False
    assert status["requires_sequential_model_unload"] is True


@pytest.mark.asyncio
async def test_preexisting_exact_comfy_runtime_is_proved_but_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.22.0"}, "devices": []})
        if request.url.path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(200, json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[config.checkpoint]]}}}})
        raise AssertionError(request.url.path)

    provider = ComfyUIProvider(config, transport=httpx.MockTransport(transport))
    record = _record(config)
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: record.pid)
    monkeypatch.setattr(provider, "_require_exact", lambda *_args: record)

    status = await provider.status()
    assert status["healthy"] is True
    assert status["exact_identity"] is True
    assert status["generation_available"] is False
    assert status["error"] == "preexisting_runtime_not_owned_by_request"
    with pytest.raises(ImageGenerationError, match="will not adopt"):
        await provider.ensure_runtime()


@pytest.mark.asyncio
async def test_provider_persists_only_verified_content_addressed_png(tmp_path: Path) -> None:
    config = _config(tmp_path)
    png = _png()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "owned-job"})
        if request.url.path == "/history/owned-job":
            return httpx.Response(200, json={
                "owned-job": {
                    "status": {"status_str": "success"},
                    "outputs": {"9": {"images": [{
                        "filename": "xomni.png", "subfolder": "", "type": "output",
                    }]}},
                }
            })
        if request.url.path == "/view":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=png)
        raise AssertionError(request.url.path)

    provider = ComfyUIProvider(config, transport=httpx.MockTransport(transport), poll_interval_s=0.01)
    runtime = RuntimeHandle(record=_record(config), spawned=True)
    result = await provider.generate(
        "a blue workshop", width=512, height=512, seed=7, runtime=runtime
    )

    assert result["verified"] is True
    assert result["actual_generation"] is True
    assert result["image_url"] == f"/api/generated-images/{result['sha256']}.png"
    target = config.output_dir / f"{result['sha256']}.png"
    assert target.read_bytes() == png
    assert runtime.active_prompt_id is None


@pytest.mark.asyncio
async def test_cancellation_during_atomic_store_waits_for_bounded_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    png = _png()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "store-job"})
        if request.url.path == "/history/store-job":
            return httpx.Response(200, json={
                "store-job": {"outputs": {"9": {"images": [{
                    "filename": "store.png", "subfolder": "", "type": "output",
                }]}}}
            })
        if request.url.path == "/view":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=png)
        raise AssertionError(request.url.path)

    provider = ComfyUIProvider(config, transport=httpx.MockTransport(transport), poll_interval_s=0.01)
    runtime = RuntimeHandle(record=_record(config), spawned=True)
    store_started = threading.Event()
    allow_store = threading.Event()
    stored = threading.Event()
    real_store = provider._atomic_store

    def blocking_store(content: bytes, digest: str) -> Path:
        store_started.set()
        assert allow_store.wait(timeout=5)
        target = real_store(content, digest)
        stored.set()
        return target

    monkeypatch.setattr(provider, "_atomic_store", blocking_store)
    task = asyncio.create_task(provider.generate(
        "store safely", width=512, height=512, seed=9, runtime=runtime
    ))
    await asyncio.to_thread(store_started.wait, 5)
    task.cancel()
    allow_store.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stored.is_set()


@pytest.mark.asyncio
async def test_view_stream_stops_at_byte_cap_and_deletes_only_owned_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, max_bytes=1024)
    requests: list[tuple[str, str]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "bounded-job"})
        if request.url.path == "/history/bounded-job":
            return httpx.Response(200, json={
                "bounded-job": {"outputs": {"9": {"images": [{
                    "filename": "large.png", "subfolder": "", "type": "output",
                }]}}}
            })
        if request.url.path == "/view":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"x" * 1025
            )
        if request.url.path == "/queue":
            assert json.loads(request.content) == {"delete": ["bounded-job"]}
            return httpx.Response(200)
        raise AssertionError(request.url.path)

    provider = ComfyUIProvider(config, transport=httpx.MockTransport(transport), poll_interval_s=0.01)
    record = _record(config)
    runtime = RuntimeHandle(record=record, spawned=True)
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: record.pid)
    monkeypatch.setattr(provider, "_require_exact", lambda *_args: record)

    with pytest.raises(ImageGenerationError, match="oversized"):
        await provider.generate(
            "large output", width=512, height=512, seed=8, runtime=runtime
        )
    assert ("POST", "/queue") in requests
    assert ("POST", "/interrupt") not in requests
    assert list(config.output_dir.glob("*.png")) == [] if config.output_dir.exists() else True


def test_existing_content_address_target_is_stat_bounded_before_read(tmp_path: Path) -> None:
    config = _config(tmp_path, max_bytes=1024)
    provider = ComfyUIProvider(config)
    content = b"small"
    digest = hashlib.sha256(content).hexdigest()
    config.output_dir.mkdir(parents=True)
    (config.output_dir / f"{digest}.png").write_bytes(b"x" * 1025)

    with pytest.raises(ImageGenerationError, match="Existing generated-image target"):
        provider._atomic_store(content, digest)


@pytest.mark.asyncio
async def test_json_response_limit_applies_to_status_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    record = _record(config)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, content=b'{"padding":"' + b"x" * 80 + b'"}')
        raise AssertionError(request.url.path)

    provider = ComfyUIProvider(config, transport=httpx.MockTransport(transport))
    provider.max_json_response_bytes = 32
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: record.pid)
    monkeypatch.setattr(provider, "_require_exact", lambda *_args: record)
    status = await provider.status()
    assert status["generation_available"] is False
    assert status["state"] == "runtime_conflict_or_unhealthy"


class _FakeRouter:
    def __init__(self, *, restore_error: bool = False) -> None:
        self.events: list[str] = []
        self.restore_error = restore_error

    def status(self) -> dict:
        return {
            "active_worker": "omni", "active_inferences": 0,
            "external_workload": None, "expected": {"gpu_indices": [0, 1]},
        }

    @asynccontextmanager
    async def external_workload_session(self, name: str):
        assert name == "image_generation"
        lease = {
            "previous_worker": "omni", "model_stopped": True,
            "model_restored": False, "gpu_indices": [0, 1],
        }
        self.events.append("model_stopped")
        try:
            yield lease
        finally:
            self.events.append("model_restore")
            if self.restore_error:
                raise WorkerSwapError("fixture restore failed")
            lease["model_restored"] = True


class _FakeProvider:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.events: list[str] = []
        self.handle = RuntimeHandle(
            record=ComfyProcessRecord(1, Path("python.exe"), (), Path("."), 1.0),
            spawned=True,
        )

    async def status(self) -> dict:
        return {"generation_available": True, "state": "configured_stopped"}

    async def ensure_runtime(self) -> RuntimeHandle:
        self.events.append("runtime_started")
        return self.handle

    async def generate(self, *_args, **_kwargs) -> dict:
        self.events.append("generated")
        return dict(self.result)

    async def release_runtime(self, _handle: RuntimeHandle) -> None:
        self.events.append("runtime_released")


@pytest.mark.asyncio
async def test_service_sequences_model_comfy_cleanup_and_restore() -> None:
    router = _FakeRouter()
    provider = _FakeProvider(_success_result())
    service = ImageGenerationService(router, provider)  # type: ignore[arg-type]
    result = await service.generate({"prompt": "a blue workshop", "width": 512, "height": 512})

    assert result["success"] is True
    assert result["lifecycle"]["model_restored"] is True
    assert router.events == ["model_stopped", "model_restore"]
    assert provider.events == ["runtime_started", "generated", "runtime_released"]
    assert Registry._approved_result_error("image_generate", result) is None
    assert artifact_type_for_tool("image_generate", result) == "generated_image"


@pytest.mark.asyncio
async def test_verified_file_truth_survives_restore_failure_without_success_card() -> None:
    router = _FakeRouter(restore_error=True)
    provider = _FakeProvider(_success_result())
    service = ImageGenerationService(router, provider)  # type: ignore[arg-type]
    result = await service.generate({"prompt": "a blue workshop", "width": 512, "height": 512})

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["actual_generation"] is True
    assert result["verified"] is True
    assert result["sha256"] == "a" * 64
    assert result["lifecycle"]["model_restored"] is False
    assert Registry._approved_result_error("image_generate", result)
    assert artifact_type_for_tool("image_generate", result) == "image_generation_status"


@pytest.mark.asyncio
async def test_generation_cancellation_releases_runtime_and_restores_model() -> None:
    router = _FakeRouter()
    provider = _FakeProvider(_success_result())
    generation_started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_generate(*_args, **_kwargs):
        provider.events.append("generated_started")
        generation_started.set()
        await never.wait()

    provider.generate = blocked_generate  # type: ignore[method-assign]
    service = ImageGenerationService(router, provider)  # type: ignore[arg-type]
    task = asyncio.create_task(service.generate({"prompt": "cancel me", "width": 512, "height": 512}))
    await generation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.events == ["runtime_started", "generated_started", "runtime_released"]
    assert router.events == ["model_stopped", "model_restore"]


@pytest.mark.asyncio
async def test_cancellation_during_release_waits_for_cleanup_then_restores() -> None:
    router = _FakeRouter()
    provider = _FakeProvider(_success_result())
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def blocked_release(_handle: RuntimeHandle) -> None:
        provider.events.append("runtime_release_started")
        release_started.set()
        await allow_release.wait()
        provider.events.append("runtime_released")

    provider.release_runtime = blocked_release  # type: ignore[method-assign]
    service = ImageGenerationService(router, provider)  # type: ignore[arg-type]
    task = asyncio.create_task(service.generate({"prompt": "cancel release", "width": 512, "height": 512}))
    await release_started.wait()
    task.cancel()
    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.events[-2:] == ["runtime_release_started", "runtime_released"]
    assert router.events == ["model_stopped", "model_restore"]


@pytest.mark.asyncio
async def test_startup_reconciliation_stops_only_state_backed_exact_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    provider = ComfyUIProvider(config)
    record = _record(config)
    provider._write_spawn_state(record)
    stopped: list[int] = []
    monkeypatch.setattr(images.psutil, "pid_exists", lambda pid: pid == record.pid)
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: record.pid)
    monkeypatch.setattr(provider, "_require_exact", lambda *_args: record)
    monkeypatch.setattr(
        provider, "_terminate_owned_record", lambda owned: stopped.append(owned.pid)
    )

    result = await provider.reconcile_startup()

    assert result["state"] == "owned_orphan_stopped"
    assert stopped == [record.pid]
    assert not config.state_path.exists()


@pytest.mark.asyncio
async def test_startup_reconciliation_never_stops_untracked_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ComfyUIProvider(_config(tmp_path))
    monkeypatch.setattr(provider, "_find_listener_pid", lambda: 999)
    monkeypatch.setattr(
        provider,
        "_terminate_owned_record",
        lambda _record: (_ for _ in ()).throw(AssertionError("must not stop")),
    )
    with pytest.raises(ImageGenerationError, match="untracked pid 999"):
        await provider.reconcile_startup()


@pytest.mark.asyncio
async def test_router_runs_external_reconciliation_before_default_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = WorkerConfig(
        name="omni", alias="omni", executable=tmp_path / "llama.exe",
        model_path=tmp_path / "model.gguf", host="127.0.0.1", port=18130,
        context_tokens=32768, gpu_indices=(0, 1),
    )
    router = ModelRouter(
        {"omni": cfg}, "omni", lifecycle_lock_path=tmp_path / "startup.lock"
    )
    events: list[str] = []

    async def reconcile() -> dict:
        events.append("reconciled")
        return {"state": "owned_orphan_stopped", "reconciled": True}

    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: None)
    monkeypatch.setattr(router, "_wait_vram_free", lambda _cfg: events.append("gpu_free") or 0.0)
    monkeypatch.setattr(
        router, "_spawn",
        lambda worker: (
            events.append("spawned")
            or ProcessRecord(40, worker.executable, (str(worker.executable),), 40.0)
        ),
    )
    monkeypatch.setattr(
        router, "_wait_healthy",
        lambda worker, *_args: (
            0.0, LiveProof((worker.alias,), worker.context_tokens), (0, 1)
        ),
    )

    result = await router.start_default(pre_start=reconcile)

    assert events[:3] == ["reconciled", "gpu_free", "spawned"]
    assert result["external_reconciliation"]["state"] == "owned_orphan_stopped"


@pytest.mark.asyncio
async def test_cancellation_while_spawn_finishes_cleans_exact_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    provider = ComfyUIProvider(config)
    record = _record(config)
    spawn_started = threading.Event()
    finish_spawn = threading.Event()
    cleaned: list[int] = []

    class Process:
        pass

    def spawn():
        spawn_started.set()
        assert finish_spawn.wait(timeout=5)
        return Process(), record

    async def cleanup(handle: RuntimeHandle) -> None:
        cleaned.append(handle.record.pid)

    monkeypatch.setattr(provider, "_find_listener_pid", lambda: None)
    monkeypatch.setattr(provider, "_spawn", spawn)
    monkeypatch.setattr(provider, "_terminate_spawned", cleanup)

    task = asyncio.create_task(provider.ensure_runtime())
    await asyncio.to_thread(spawn_started.wait, 5)
    task.cancel()
    finish_spawn.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [record.pid]


@pytest.mark.asyncio
async def test_router_cancellation_waits_for_stop_then_restores_previous_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = WorkerConfig(
        name="omni", alias="omni", executable=tmp_path / "llama.exe",
        model_path=tmp_path / "model.gguf", host="127.0.0.1", port=18131,
        context_tokens=32768, gpu_indices=(0, 1),
    )
    router = ModelRouter(
        {"omni": cfg}, "omni", lifecycle_lock_path=tmp_path / "lifecycle.lock"
    )
    router.active_name = "omni"
    router.active_pid = 10
    router.active_started_at = 10.0
    stop_started = threading.Event()
    allow_stop = threading.Event()

    def stop() -> WorkerConfig:
        stop_started.set()
        assert allow_stop.wait(timeout=5)
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        return cfg

    monkeypatch.setattr(router, "_stop_active_worker", stop)
    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: None)
    monkeypatch.setattr(router, "_wait_vram_free", lambda _cfg: 0.0)
    monkeypatch.setattr(
        router, "_spawn",
        lambda worker: ProcessRecord(20, worker.executable, (str(worker.executable),), 20.0),
    )
    monkeypatch.setattr(
        router, "_wait_healthy",
        lambda worker, *_args: (
            0.0, LiveProof((worker.alias,), worker.context_tokens), (0, 1)
        ),
    )

    async def operation() -> None:
        async with router.external_workload_session("image_generation"):
            raise AssertionError("cancelled hand-off must not reach the workload")

    async def scenario() -> None:
        task = asyncio.create_task(operation())
        await asyncio.to_thread(stop_started.wait, 5)
        task.cancel()
        allow_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    await scenario()
    assert router.active_name == "omni"
    assert router.active_pid == 20
    assert router.swapping is False
    assert router.external_workload is None


@pytest.mark.asyncio
async def test_router_cancellation_during_restore_waits_for_verified_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = WorkerConfig(
        name="omni", alias="omni", executable=tmp_path / "llama.exe",
        model_path=tmp_path / "model.gguf", host="127.0.0.1", port=18132,
        context_tokens=32768, gpu_indices=(0, 1),
    )
    router = ModelRouter(
        {"omni": cfg}, "omni", lifecycle_lock_path=tmp_path / "restore.lock"
    )
    router.active_name = "omni"
    router.active_pid = 10
    router.active_started_at = 10.0
    restore_started = threading.Event()
    allow_restore = threading.Event()

    def stop() -> WorkerConfig:
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        return cfg

    def spawn(worker: WorkerConfig) -> ProcessRecord:
        restore_started.set()
        assert allow_restore.wait(timeout=5)
        return ProcessRecord(30, worker.executable, (str(worker.executable),), 30.0)

    monkeypatch.setattr(router, "_stop_active_worker", stop)
    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: None)
    monkeypatch.setattr(router, "_wait_vram_free", lambda _cfg: 0.0)
    monkeypatch.setattr(router, "_spawn", spawn)
    monkeypatch.setattr(
        router, "_wait_healthy",
        lambda worker, *_args: (
            0.0, LiveProof((worker.alias,), worker.context_tokens), (0, 1)
        ),
    )

    async def operation() -> None:
        async with router.external_workload_session("image_generation"):
            pass

    task = asyncio.create_task(operation())
    await asyncio.to_thread(restore_started.wait, 5)
    task.cancel()
    allow_restore.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert router.active_name == "omni"
    assert router.active_pid == 30
    assert router.swapping is False


@pytest.mark.asyncio
async def test_cancelled_approved_execution_gets_durable_terminal_receipt(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "tools.yaml"
    policy.write_text(
        "tools:\n  image_generate:\n    tier: confirm_required\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(policy, store=store)
    started = asyncio.Event()
    never = asyncio.Event()

    async def handler(_args: dict) -> dict:
        started.set()
        await never.wait()
        return _success_result()

    registry.register("image_generate", handler)
    conversation_id = store.create_conversation("cancel")
    message_id = store.add_message(conversation_id, "user", "generate")
    approval_id = store.create_approval(
        "image_generate",
        "Generate image",
        {"name": "image_generate", "args": {"prompt": "blue workshop"}},
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="image-call",
    )

    task = asyncio.create_task(registry.resolve_approval(
        approval_id,
        True,
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = store.approval_snapshot(approval_id)
    assert snapshot["approval"]["status"] == "failed"
    assert snapshot["receipt"]["status"] == "failed"
    assert snapshot["receipt"]["success"] is False
    assert snapshot["receipt"]["result"]["execution_state"] == "cancelled"
    assert snapshot["receipt"]["result"]["may_have_executed"] is True
    store.close()


@pytest.mark.asyncio
async def test_cancelled_executing_status_closes_receipt_before_handler_runs(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "tools.yaml"
    policy.write_text(
        "tools:\n  image_generate:\n    tier: confirm_required\n",
        encoding="utf-8",
    )
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(policy, store=store)
    handler_calls = 0
    status_started = asyncio.Event()
    never = asyncio.Event()

    async def handler(_args: dict) -> dict:
        nonlocal handler_calls
        handler_calls += 1
        return _success_result()

    async def on_status(_status: str) -> None:
        status_started.set()
        await never.wait()

    registry.register("image_generate", handler)
    conversation_id = store.create_conversation("cancel before handler")
    message_id = store.add_message(conversation_id, "user", "generate")
    approval_id = store.create_approval(
        "image_generate",
        "Generate image",
        {"name": "image_generate", "args": {"prompt": "blue workshop"}},
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="image-call",
    )

    task = asyncio.create_task(registry.resolve_approval(
        approval_id,
        True,
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        on_status=on_status,
    ))
    await status_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    snapshot = store.approval_snapshot(approval_id)
    assert handler_calls == 0
    assert snapshot["approval"]["status"] == "failed"
    assert snapshot["receipt"]["executed"] is False
    assert snapshot["receipt"]["success"] is False
    assert snapshot["receipt"]["result"]["execution_state"] == "cancelled"
    assert snapshot["receipt"]["result"]["may_have_executed"] is False
    store.close()
