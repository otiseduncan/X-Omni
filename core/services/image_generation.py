"""Owned, sequential ComfyUI image generation for the existing chat stream.

Qwen3-Omni understands multimodal input; it is not an image synthesizer.  This
module therefore treats ComfyUI as a separate heavy GPU workload.  Generation
may run only inside ``ModelRouter.external_workload_session`` so the live model
and ComfyUI are never assumed to fit concurrently on Omega's two GPUs.

The provider never trusts a port alone.  Reuse requires the exact executable,
full command line, working directory, process start time, listener PID, live
ComfyUI version, and configured checkpoint proof.  Cleanup stops only the
``Popen`` instance created by this provider.  A pre-existing exact runtime is
unloaded through its API and left running.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import httpx
import psutil

from ..models.router import ModelRouter, WorkerSwapError


NEGATIVE_PROMPT = (
    "low quality, blurry, soft focus, distorted, malformed anatomy, duplicate "
    "subject, duplicate face, extra limbs, extra fingers, bad hands, noisy, "
    "oversaturated, watermark, signature, logo, caption, text artifacts"
)
POSITIVE_ENHANCER = (
    "high quality, detailed, cinematic lighting, polished composition, natural "
    "materials, professional image"
)

_MINOR_RE = re.compile(r"\b(child|children|kid|minor|underage|teenager|schoolgirl|schoolboy)\b", re.I)
_SEXUAL_RE = re.compile(r"\b(nude|naked|sex|sexual|porn|erotic|fetish|genitals?)\b", re.I)
_SEXUAL_VIOLENCE_RE = re.compile(r"\b(rape|raped|raping|sexual assault|nonconsensual)\b", re.I)
_SAFE_IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{64}\.png$")


class ImageGenerationError(RuntimeError):
    """A bounded, operator-safe image-generation failure."""


async def _finish_despite_cancellation(awaitable) -> tuple[Any, bool]:
    """Return an ownership mutation's result after any caller cancellation."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _await_cleanup(awaitable) -> Any:
    """Complete exact runtime cleanup before propagating cancellation."""
    result, cancelled = await _finish_despite_cancellation(awaitable)
    if cancelled:
        raise asyncio.CancelledError
    return result


@dataclass(frozen=True)
class ImageGenerationConfig:
    enabled: bool
    provider: str
    host: str
    port: int
    runtime_root: Path
    python_executable: Path
    main_script: Path
    checkpoint: str
    checkpoint_path: Path
    output_dir: Path
    state_path: Path
    default_width: int
    default_height: int
    startup_timeout_s: float
    generation_timeout_s: float
    shutdown_timeout_s: float
    max_output_bytes: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def command(self) -> tuple[str, ...]:
        return (
            str(self.python_executable),
            "-s",
            str(self.main_script),
            "--windows-standalone-build",
            "--listen",
            self.host,
            "--port",
            str(self.port),
            "--disable-auto-launch",
        )

    @classmethod
    def from_file(cls, path: Path, app_root: Path) -> "ImageGenerationConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        runtime_root = Path(str(raw.get("runtime_root") or "")).resolve()
        output_value = Path(str(raw.get("output_dir") or "data/generated-images"))
        output_dir = (
            output_value.resolve()
            if output_value.is_absolute()
            else (Path(app_root).resolve() / output_value).resolve()
        )
        app_root = Path(app_root).resolve()
        if output_dir != app_root and app_root not in output_dir.parents:
            raise ImageGenerationError("Generated-image output must stay inside X Omni")

        config = cls(
            enabled=bool(raw.get("enabled", False)),
            provider=str(raw.get("provider") or "").strip(),
            host=str(raw.get("host") or "").strip(),
            port=int(raw.get("port") or 0),
            runtime_root=runtime_root,
            python_executable=runtime_root / "python_embeded" / "python.exe",
            main_script=runtime_root / "ComfyUI" / "main.py",
            checkpoint=str(raw.get("checkpoint") or "").strip(),
            checkpoint_path=(
                runtime_root
                / "ComfyUI"
                / "models"
                / "checkpoints"
                / str(raw.get("checkpoint") or "").strip()
            ),
            output_dir=output_dir,
            state_path=app_root / "data" / "runtime" / "image-generation-runtime.json",
            default_width=int(raw.get("default_width") or 1024),
            default_height=int(raw.get("default_height") or 1024),
            startup_timeout_s=float(raw.get("startup_timeout_seconds") or 180),
            generation_timeout_s=float(raw.get("generation_timeout_seconds") or 300),
            shutdown_timeout_s=float(raw.get("shutdown_timeout_seconds") or 20),
            max_output_bytes=int(raw.get("max_output_bytes") or 64 * 1024 * 1024),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.provider != "comfyui":
            raise ImageGenerationError("Only the owned ComfyUI image provider is supported")
        if self.host not in {"127.0.0.1", "localhost"}:
            raise ImageGenerationError("ComfyUI must bind to loopback")
        if not 1 <= self.port <= 65535:
            raise ImageGenerationError("ComfyUI port is invalid")
        for dimension in (self.default_width, self.default_height):
            if not 512 <= dimension <= 1024 or dimension % 64:
                raise ImageGenerationError(
                    "Default image dimensions must be 512-1024 and multiples of 64"
                )
        if not 30 <= self.startup_timeout_s <= 600:
            raise ImageGenerationError("ComfyUI startup timeout is invalid")
        if not 30 <= self.generation_timeout_s <= 1800:
            raise ImageGenerationError("ComfyUI generation timeout is invalid")
        if not 5 <= self.shutdown_timeout_s <= 120:
            raise ImageGenerationError("ComfyUI shutdown timeout is invalid")
        if not 1024 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ImageGenerationError("Generated-image byte limit is invalid")


@dataclass(frozen=True)
class ComfyProcessRecord:
    pid: int
    executable: Path
    command_line: tuple[str, ...]
    cwd: Path
    started_at: float


@dataclass
class RuntimeHandle:
    record: ComfyProcessRecord
    spawned: bool
    process: Optional[subprocess.Popen] = None
    active_prompt_id: Optional[str] = None


class ComfyUIProvider:
    provider_id = "comfyui-sdxl-local"
    max_json_response_bytes = 8 * 1024 * 1024

    def __init__(
        self,
        config: ImageGenerationConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.75,
    ) -> None:
        self.config = config
        self.transport = transport
        self.poll_interval_s = max(0.01, float(poll_interval_s))

    def _client(self, timeout_s: Optional[float] = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=timeout_s or self.config.generation_timeout_s,
            transport=self.transport,
            trust_env=False,
        )

    async def _bounded_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        content = bytearray()
        async with client.stream(method, path, json=json_body) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > self.max_json_response_bytes:
                    raise ImageGenerationError(
                        "ComfyUI returned an oversized JSON response"
                    )
                content.extend(chunk)
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ImageGenerationError("ComfyUI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ImageGenerationError("ComfyUI JSON response was not an object")
        return payload

    @staticmethod
    def _path_key(path: Path | str) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _find_listener_pid(self) -> Optional[int]:
        try:
            pids = {
                int(item.pid)
                for item in psutil.net_connections(kind="tcp")
                if item.laddr
                and item.laddr.port == self.config.port
                and item.status == psutil.CONN_LISTEN
                and item.pid
            }
        except psutil.AccessDenied as exc:
            raise ImageGenerationError(
                "Access denied while proving ComfyUI listener ownership"
            ) from exc
        if len(pids) > 1:
            raise ImageGenerationError(
                f"Multiple processes listen on ComfyUI port {self.config.port}"
            )
        return next(iter(pids), None)

    def _inspect_process(self, pid: int) -> ComfyProcessRecord:
        try:
            process = psutil.Process(pid)
            record = ComfyProcessRecord(
                pid=pid,
                executable=Path(process.exe()),
                command_line=tuple(process.cmdline()),
                cwd=Path(process.cwd()),
                started_at=float(process.create_time()),
            )
        except psutil.NoSuchProcess as exc:
            raise ImageGenerationError(f"ComfyUI process pid {pid} no longer exists") from exc
        except psutil.AccessDenied as exc:
            raise ImageGenerationError(
                f"Access denied while proving ComfyUI process pid {pid}"
            ) from exc
        if not record.command_line or record.started_at <= 0:
            raise ImageGenerationError("ComfyUI process identity is incomplete")
        return record

    def _identity_issues(
        self,
        record: ComfyProcessRecord,
        expected_started_at: Optional[float] = None,
    ) -> list[str]:
        issues: list[str] = []
        expected = self.config.command
        if self._path_key(record.executable) != self._path_key(self.config.python_executable):
            issues.append("executable_mismatch")
        if len(record.command_line) != len(expected):
            issues.append("command_line_mismatch")
        else:
            for index, (actual, wanted) in enumerate(
                zip(record.command_line, expected, strict=True)
            ):
                equal = (
                    self._path_key(actual) == self._path_key(wanted)
                    if index in {0, 2}
                    else actual == wanted
                )
                if not equal:
                    issues.append("command_line_mismatch")
                    break
        if self._path_key(record.cwd) != self._path_key(self.config.runtime_root):
            issues.append("working_directory_mismatch")
        if (
            expected_started_at is not None
            and abs(record.started_at - expected_started_at) >= 0.01
        ):
            issues.append("start_time_mismatch")
        return sorted(set(issues))

    def _require_exact(
        self, pid: int, expected_started_at: Optional[float] = None
    ) -> ComfyProcessRecord:
        record = self._inspect_process(pid)
        issues = self._identity_issues(record, expected_started_at)
        if issues:
            raise ImageGenerationError(
                f"Port {self.config.port} is not the configured ComfyUI runtime "
                f"({', '.join(issues)}); no process was adopted or stopped"
            )
        return record

    async def _live_proof(self) -> dict[str, Any]:
        async with self._client(5) as client:
            stats = await self._bounded_json(client, "GET", "/system_stats")
            version = str((stats.get("system") or {}).get("comfyui_version") or "").strip()
            if not version:
                raise ImageGenerationError("ComfyUI did not report its runtime version")

            object_info = await self._bounded_json(
                client, "GET", "/object_info/CheckpointLoaderSimple"
            )
            choices = (
                (((object_info.get("CheckpointLoaderSimple") or {}).get("input") or {})
                 .get("required") or {})
                .get("ckpt_name")
                or [[]]
            )
            available = choices[0] if isinstance(choices, list) and choices else []
            if self.config.checkpoint not in available:
                raise ImageGenerationError(
                    "Configured image checkpoint is not available in the live ComfyUI runtime"
                )
            devices = stats.get("devices") or []
            return {
                "comfyui_version": version,
                "checkpoint": self.config.checkpoint,
                "checkpoint_available": True,
                "devices": [
                    str(item.get("name") or "")
                    for item in devices
                    if isinstance(item, dict) and item.get("name")
                ][:4],
            }

    def _runtime_files(self) -> dict[str, bool]:
        return {
            "root_present": self.config.runtime_root.is_dir(),
            "python_present": self.config.python_executable.is_file(),
            "main_present": self.config.main_script.is_file(),
            "checkpoint_present": self.config.checkpoint_path.is_file(),
        }

    def _command_fingerprint(self) -> str:
        encoded = json.dumps(
            list(self.config.command), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _state_payload(self, record: ComfyProcessRecord) -> dict[str, Any]:
        return {
            "version": 1,
            "owner": "x-omni-image-generation",
            "provider": self.provider_id,
            "pid": record.pid,
            "process_started_at": record.started_at,
            "port": self.config.port,
            "command_sha256": self._command_fingerprint(),
            "recorded_at": time.time(),
        }

    def _write_spawn_state(self, record: ComfyProcessRecord) -> None:
        payload = self._state_payload(record)
        self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".image-runtime-",
                suffix=".tmp",
                dir=self.config.state_path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config.state_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_spawn_state(self) -> Optional[dict[str, Any]]:
        path = self.config.state_path
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > 16 * 1024:
            raise ImageGenerationError("ComfyUI ownership state is invalid")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ImageGenerationError("ComfyUI ownership state is unreadable") from exc
        if not isinstance(payload, dict):
            raise ImageGenerationError("ComfyUI ownership state is invalid")
        expected = {
            "version": 1,
            "owner": "x-omni-image-generation",
            "provider": self.provider_id,
            "port": self.config.port,
            "command_sha256": self._command_fingerprint(),
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ImageGenerationError(
                "ComfyUI ownership state does not match this X Omni runtime"
            )
        pid = payload.get("pid")
        started_at = payload.get("process_started_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or float(started_at) <= 0
        ):
            raise ImageGenerationError("ComfyUI ownership state has invalid process identity")
        return payload

    def _clear_spawn_state(self, record: ComfyProcessRecord) -> None:
        payload = self._read_spawn_state()
        if payload is None:
            return
        if (
            payload.get("pid") != record.pid
            or abs(float(payload.get("process_started_at")) - record.started_at) >= 0.01
        ):
            raise ImageGenerationError(
                "ComfyUI ownership state changed; it was not removed"
            )
        self.config.state_path.unlink(missing_ok=True)

    def _terminate_owned_record(self, record: ComfyProcessRecord) -> None:
        exact = self._require_exact(record.pid, record.started_at)
        listener = self._find_listener_pid()
        if listener is not None and listener != exact.pid:
            raise ImageGenerationError(
                f"Foreign pid {listener} owns ComfyUI port {self.config.port}; "
                "the recorded process was not stopped"
            )
        try:
            process = psutil.Process(exact.pid)
            if abs(float(process.create_time()) - record.started_at) >= 0.01:
                raise ImageGenerationError(
                    "Recorded ComfyUI process start time changed before cleanup"
                )
            process.terminate()
            try:
                process.wait(timeout=self.config.shutdown_timeout_s)
            except psutil.TimeoutExpired:
                if abs(float(process.create_time()) - record.started_at) >= 0.01:
                    raise ImageGenerationError(
                        "Recorded ComfyUI process start time changed before force cleanup"
                    )
                process.kill()
                process.wait(timeout=self.config.shutdown_timeout_s)
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied as exc:
            raise ImageGenerationError(
                "Access denied cleaning up the exact X Omni ComfyUI process"
            ) from exc

    async def reconcile_startup(self) -> dict[str, Any]:
        """Remove only a state-backed exact orphan before llama.cpp starts."""
        payload = await asyncio.to_thread(self._read_spawn_state)
        listener = await asyncio.to_thread(self._find_listener_pid)
        if payload is None:
            if listener is not None:
                raise ImageGenerationError(
                    f"ComfyUI port {self.config.port} is owned by untracked pid {listener}; "
                    "X Omni will not stop or reuse it during startup"
                )
            return {"state": "clean", "reconciled": False}

        pid = int(payload["pid"])
        started_at = float(payload["process_started_at"])
        if not psutil.pid_exists(pid):
            # No process can be targeted; removing this exact stale marker is safe.
            self.config.state_path.unlink(missing_ok=True)
            if listener is not None:
                raise ImageGenerationError(
                    f"Stale X Omni ComfyUI state was removed, but untracked pid "
                    f"{listener} owns port {self.config.port}"
                )
            return {"state": "stale_state_removed", "reconciled": True}

        record = await asyncio.to_thread(self._require_exact, pid, started_at)
        if listener is not None and listener != pid:
            raise ImageGenerationError(
                f"Tracked ComfyUI pid {pid} is not the listener on port "
                f"{self.config.port}; no process was stopped"
            )
        await asyncio.to_thread(self._terminate_owned_record, record)
        await asyncio.to_thread(self._clear_spawn_state, record)
        return {
            "state": "owned_orphan_stopped",
            "reconciled": True,
            "pid": pid,
            "process_started_at": started_at,
            "listener_was_present": listener == pid,
        }

    async def status(self) -> dict[str, Any]:
        """Non-disruptive configured/live/identity status; never auto-starts."""
        files = self._runtime_files()
        configured = self.config.enabled and all(files.values())
        base = {
            "ok": False,
            "provider": self.provider_id,
            "enabled": self.config.enabled,
            "configured": configured,
            "live": False,
            "healthy": False,
            "exact_identity": False,
            "checkpoint": self.config.checkpoint,
            "checkpoint_available": False,
            "listener_pid": None,
            "runtime": files,
            "coexistence_supported": False,
            "requires_sequential_model_unload": True,
            "managed_lifecycle": True,
            "generation_available": False,
            "owned_state_present": self.config.state_path.is_file(),
        }
        if not self.config.enabled:
            return {**base, "state": "disabled", "error": "provider_disabled"}
        if not configured:
            return {**base, "state": "not_configured", "error": "runtime_asset_missing"}
        try:
            pid = await asyncio.to_thread(self._find_listener_pid)
            base["listener_pid"] = pid
            if pid is None:
                return {
                    **base,
                    "ok": True,
                    "state": "configured_stopped",
                    "generation_available": True,
                    "error": None,
                }
            record = await asyncio.to_thread(self._require_exact, pid)
            proof = await self._live_proof()
            return {
                **base,
                "ok": True,
                "state": "healthy_exact_runtime_not_adopted",
                "live": True,
                "healthy": True,
                "exact_identity": True,
                "process_started_at": record.started_at,
                "checkpoint_available": True,
                # Exact process identity is not proof that its queued work is
                # ours. X Omni therefore never adopts a pre-existing runtime.
                "generation_available": False,
                "comfyui_version": proof["comfyui_version"],
                "devices": proof["devices"],
                "error": "preexisting_runtime_not_owned_by_request",
            }
        except (ImageGenerationError, httpx.HTTPError, ValueError, psutil.Error) as exc:
            return {
                **base,
                "state": "runtime_conflict_or_unhealthy",
                "error": type(exc).__name__,
            }

    def _spawn(self) -> tuple[subprocess.Popen, ComfyProcessRecord]:
        if not all(self._runtime_files().values()):
            raise ImageGenerationError("Configured ComfyUI runtime assets are missing")
        existing = self._find_listener_pid()
        if existing is not None:
            raise ImageGenerationError(
                f"ComfyUI port {self.config.port} became occupied by pid {existing}"
            )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            list(self.config.command),
            cwd=str(self.config.runtime_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            record = self._inspect_process(process.pid)
            issues = self._identity_issues(record)
            if issues:
                raise ImageGenerationError(
                    f"Spawned ComfyUI failed identity checks: {', '.join(issues)}"
                )
            self._write_spawn_state(record)
            return process, record
        except Exception:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.config.shutdown_timeout_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.config.shutdown_timeout_s)
            raise

    async def _wait_ready(
        self, process: subprocess.Popen, record: ComfyProcessRecord
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.startup_timeout_s
        last_error = "listener_not_ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ImageGenerationError(
                    f"ComfyUI exited during startup with code {process.returncode}"
                )
            await asyncio.to_thread(self._require_exact, record.pid, record.started_at)
            listener = await asyncio.to_thread(self._find_listener_pid)
            if listener is not None and listener != record.pid:
                raise ImageGenerationError(
                    f"Foreign pid {listener} took ComfyUI port {self.config.port}"
                )
            if listener == record.pid:
                try:
                    return await self._live_proof()
                except (ImageGenerationError, httpx.HTTPError, ValueError) as exc:
                    last_error = type(exc).__name__
            await asyncio.sleep(self.poll_interval_s)
        raise ImageGenerationError(
            f"ComfyUI did not satisfy identity and checkpoint readiness within "
            f"{self.config.startup_timeout_s:.0f}s (last: {last_error})"
        )

    async def _terminate_spawned(self, handle: RuntimeHandle) -> None:
        process = handle.process
        if not handle.spawned or process is None:
            return
        if process.poll() is not None:
            await asyncio.to_thread(self._clear_spawn_state, handle.record)
            return
        await asyncio.to_thread(
            self._require_exact, handle.record.pid, handle.record.started_at
        )
        # Popen retains the exact Windows process handle; the start-time check
        # above additionally prevents a recycled PID from passing inspection.
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, self.config.shutdown_timeout_s)
        except subprocess.TimeoutExpired:
            await asyncio.to_thread(
                self._require_exact, handle.record.pid, handle.record.started_at
            )
            process.kill()
            await asyncio.to_thread(process.wait, self.config.shutdown_timeout_s)
        await asyncio.to_thread(self._clear_spawn_state, handle.record)

    async def ensure_runtime(self) -> RuntimeHandle:
        listener = await asyncio.to_thread(self._find_listener_pid)
        if listener is not None:
            await asyncio.to_thread(self._require_exact, listener)
            await self._live_proof()
            raise ImageGenerationError(
                "A pre-existing exact ComfyUI runtime is live, but X Omni will not "
                "adopt its process or queued work"
            )

        spawned, spawn_cancelled = await _finish_despite_cancellation(
            asyncio.to_thread(self._spawn)
        )
        process, record = spawned
        handle = RuntimeHandle(record=record, spawned=True, process=process)
        if spawn_cancelled:
            await _await_cleanup(self._terminate_spawned(handle))
            raise asyncio.CancelledError
        try:
            await self._wait_ready(process, record)
            return handle
        except BaseException:
            await _await_cleanup(self._terminate_spawned(handle))
            raise

    async def release_runtime(self, handle: RuntimeHandle) -> None:
        if handle.spawned:
            await self._terminate_spawned(handle)
            return
        raise ImageGenerationError(
            "Refusing to release a ComfyUI runtime that this request did not spawn"
        )

    @staticmethod
    def workflow(
        prompt: str, checkpoint: str, width: int, height: int, seed: int
    ) -> dict[str, Any]:
        return {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": 28,
                    "cfg": 7,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": checkpoint},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": width, "height": height, "batch_size": 1},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": f"{prompt}, {POSITIVE_ENHANCER}",
                    "clip": ["4", 1],
                },
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": NEGATIVE_PROMPT, "clip": ["4", 1]},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "xomni_generated", "images": ["8", 0]},
            },
        }

    @staticmethod
    def _validate_png(content: bytes) -> tuple[int, int]:
        if len(content) < 45 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ImageGenerationError("ComfyUI returned invalid PNG data")
        offset = 8
        width = height = 0
        saw_iend = False
        chunks = 0
        while offset + 12 <= len(content):
            chunks += 1
            if chunks > 100_000:
                raise ImageGenerationError("ComfyUI PNG contains too many chunks")
            length = int.from_bytes(content[offset : offset + 4], "big")
            kind = content[offset + 4 : offset + 8]
            data_start = offset + 8
            data_end = data_start + length
            crc_end = data_end + 4
            if length > len(content) or crc_end > len(content):
                raise ImageGenerationError("ComfyUI PNG is truncated")
            expected_crc = int.from_bytes(content[data_end:crc_end], "big")
            actual_crc = zlib.crc32(kind)
            actual_crc = zlib.crc32(content[data_start:data_end], actual_crc) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise ImageGenerationError("ComfyUI PNG failed its integrity check")
            if chunks == 1:
                if kind != b"IHDR" or length != 13:
                    raise ImageGenerationError("ComfyUI PNG has no valid IHDR")
                width = int.from_bytes(content[data_start : data_start + 4], "big")
                height = int.from_bytes(content[data_start + 4 : data_start + 8], "big")
            if kind == b"IEND":
                if length != 0 or crc_end != len(content):
                    raise ImageGenerationError("ComfyUI PNG has an invalid end marker")
                saw_iend = True
                break
            offset = crc_end
        if not saw_iend or width <= 0 or height <= 0:
            raise ImageGenerationError("ComfyUI PNG is incomplete")
        return width, height

    @staticmethod
    def _safe_output_reference(output: dict[str, Any]) -> dict[str, str]:
        filename = str(output.get("filename") or "")
        subfolder = str(output.get("subfolder") or "").replace("\\", "/")
        output_type = str(output.get("type") or "output")
        if not filename or Path(filename).name != filename or "\x00" in filename:
            raise ImageGenerationError("ComfyUI returned an unsafe output filename")
        folder = PurePosixPath(subfolder)
        if folder.is_absolute() or ".." in folder.parts:
            raise ImageGenerationError("ComfyUI returned an unsafe output subfolder")
        if output_type != "output":
            raise ImageGenerationError("ComfyUI did not return a saved output image")
        return {"filename": filename, "subfolder": subfolder, "type": output_type}

    def _atomic_store(self, content: bytes, digest: str) -> Path:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.output_dir / f"{digest}.png"
        if target.exists():
            existing_size = target.stat().st_size
            if existing_size <= 0 or existing_size > self.config.max_output_bytes:
                raise ImageGenerationError("Existing generated-image target is invalid")
            existing_hash = hashlib.sha256()
            with target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    existing_hash.update(chunk)
            if existing_hash.hexdigest() != digest:
                raise ImageGenerationError("Generated-image content-address collision")
            return target
        temporary: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".image-", suffix=".tmp",
                dir=self.config.output_dir, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _verify_stored(self, target: Path, digest: str, expected_size: int) -> bool:
        if not target.is_file():
            return False
        size = target.stat().st_size
        if size != expected_size or size <= 0 or size > self.config.max_output_bytes:
            return False
        actual = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
        return actual.hexdigest() == digest

    async def _cancel_prompt(self, prompt_id: str, handle: RuntimeHandle) -> None:
        """Remove only this request's exact prompt from its owned runtime."""
        if not handle.spawned:
            raise ImageGenerationError(
                "X Omni will not cancel work in a runtime it did not spawn"
            )
        listener = await asyncio.to_thread(self._find_listener_pid)
        if listener != handle.record.pid:
            raise ImageGenerationError(
                "ComfyUI listener changed while cancelling the failed image job"
            )
        await asyncio.to_thread(
            self._require_exact, handle.record.pid, handle.record.started_at
        )
        async with self._client(10) as client:
            async with client.stream(
                "POST", "/queue", json={"delete": [prompt_id]}
            ) as removed:
                removed.raise_for_status()
        # A running job may not be deletable. The exact spawned process is
        # terminated immediately by release_runtime, which is the scoped
        # cancellation boundary; no global /interrupt is sent.

    async def generate(
        self,
        prompt: str,
        *,
        width: int,
        height: int,
        seed: int,
        runtime: RuntimeHandle,
    ) -> dict[str, Any]:
        client_id = str(uuid.uuid4())
        workflow = self.workflow(prompt, self.config.checkpoint, width, height, seed)
        async with self._client() as client:
            queue_body = await self._bounded_json(
                client,
                "POST",
                "/prompt",
                json_body={"prompt": workflow, "client_id": client_id},
            )
            prompt_id = str((queue_body or {}).get("prompt_id") or "").strip()
            if not prompt_id or len(prompt_id) > 160:
                raise ImageGenerationError("ComfyUI returned no valid prompt ID")
            runtime.active_prompt_id = prompt_id

            try:
                deadline = time.monotonic() + self.config.generation_timeout_s
                output: Optional[dict[str, Any]] = None
                while time.monotonic() < deadline:
                    history = await self._bounded_json(
                        client, "GET", f"/history/{prompt_id}"
                    )
                    record = history.get(prompt_id) if isinstance(history, dict) else None
                    if isinstance(record, dict):
                        status = record.get("status") or {}
                        if status.get("status_str") == "error":
                            raise ImageGenerationError(
                                "ComfyUI reported a workflow execution error"
                            )
                        for node in (record.get("outputs") or {}).values():
                            images = node.get("images") if isinstance(node, dict) else None
                            if (
                                isinstance(images, list)
                                and images
                                and isinstance(images[0], dict)
                            ):
                                output = images[0]
                                break
                    if output is not None:
                        break
                    await asyncio.sleep(self.poll_interval_s)
                if output is None:
                    raise ImageGenerationError(
                        "ComfyUI generation timed out before producing an image"
                    )

                reference = self._safe_output_reference(output)
                content = bytearray()
                async with client.stream("GET", "/view", params=reference) as image_response:
                    image_response.raise_for_status()
                    content_type = image_response.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0]
                    if content_type.casefold() != "image/png":
                        raise ImageGenerationError("ComfyUI output was not a PNG image")
                    async for chunk in image_response.aiter_bytes():
                        if len(content) + len(chunk) > self.config.max_output_bytes:
                            raise ImageGenerationError(
                                "ComfyUI returned an oversized image"
                            )
                        content.extend(chunk)
                if not content:
                    raise ImageGenerationError("ComfyUI returned an empty image")
                runtime.active_prompt_id = None
            except Exception:
                try:
                    await self._cancel_prompt(prompt_id, runtime)
                    runtime.active_prompt_id = None
                except Exception as cleanup_exc:
                    raise ImageGenerationError(
                        "Image generation failed and its ComfyUI job could not be cancelled"
                    ) from cleanup_exc
                raise

        content_bytes = bytes(content)
        actual_width, actual_height = self._validate_png(content_bytes)
        if (actual_width, actual_height) != (width, height):
            raise ImageGenerationError(
                "ComfyUI output dimensions did not match the approved request"
            )
        digest = hashlib.sha256(content_bytes).hexdigest()
        target, store_cancelled = await _finish_despite_cancellation(
            asyncio.to_thread(self._atomic_store, content_bytes, digest)
        )
        stored_ok, verify_cancelled = await _finish_despite_cancellation(
            asyncio.to_thread(
                self._verify_stored, target, digest, len(content_bytes)
            )
        )
        if not stored_ok:
            raise ImageGenerationError("Persisted generated image failed SHA-256 verification")
        if store_cancelled or verify_cancelled:
            raise asyncio.CancelledError

        return {
            "ok": True,
            "status": "completed",
            "executed": True,
            "success": True,
            "actual_generation": True,
            "verified": True,
            "provider": self.provider_id,
            "image_url": f"/api/generated-images/{digest}.png",
            "mime_type": "image/png",
            "sha256": digest,
            "bytes": len(content_bytes),
            "width": actual_width,
            "height": actual_height,
            "seed": seed,
            "checkpoint": self.config.checkpoint,
            "steps": 28,
            "cfg": 7,
            "sampler": "euler",
            "scheduler": "normal",
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_id": prompt_id,
            "target": f"/api/generated-images/{digest}.png",
        }


class ImageGenerationService:
    def __init__(self, router: ModelRouter, provider: ComfyUIProvider) -> None:
        self.router = router
        self.provider = provider
        self._lock = asyncio.Lock()

    async def reconcile_startup(self) -> dict[str, Any]:
        return await self.provider.reconcile_startup()

    @staticmethod
    def _validated_args(args: dict) -> tuple[str, int, int, int]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ImageGenerationError("An image prompt is required")
        if len(prompt) > 2000 or any(ord(char) < 32 and char not in "\n\t" for char in prompt):
            raise ImageGenerationError("Image prompt is invalid or exceeds 2,000 characters")
        if _SEXUAL_VIOLENCE_RE.search(prompt) or (
            _MINOR_RE.search(prompt) and _SEXUAL_RE.search(prompt)
        ):
            raise ImageGenerationError(
                "This image request is blocked by the local high-risk content boundary"
            )

        width = int(args.get("width") or 1024)
        height = int(args.get("height") or 1024)
        for dimension in (width, height):
            if not 512 <= dimension <= 1024 or dimension % 64:
                raise ImageGenerationError(
                    "Image dimensions must be 512-1024 and multiples of 64"
                )
        raw_seed = args.get("seed")
        seed = secrets.randbits(63) if raw_seed is None else int(raw_seed)
        if isinstance(raw_seed, bool) or not 0 <= seed < 2**63:
            raise ImageGenerationError("Image seed must be between 0 and 2^63-1")
        return prompt, width, height, seed

    async def status(self, _args: Optional[dict] = None) -> dict[str, Any]:
        provider = await self.provider.status()
        router_state = self.router.status()
        return {
            **provider,
            "lifecycle": {
                "mode": "sequential_exclusive",
                "active_worker": router_state.get("active_worker"),
                "active_inferences": router_state.get("active_inferences", 0),
                "external_workload": router_state.get("external_workload"),
                "gpu_indices": ((router_state.get("expected") or {}).get("gpu_indices") or []),
                "model_restore_required": True,
            },
        }

    @staticmethod
    def _failure(stage: str, exc: Exception) -> dict[str, Any]:
        if isinstance(exc, (ImageGenerationError, WorkerSwapError)):
            message = str(exc)
        elif isinstance(exc, httpx.HTTPError):
            message = f"ComfyUI request failed ({type(exc).__name__})"
        else:
            message = f"Image generation failed ({type(exc).__name__})"
        return {
            "ok": False,
            "status": "failed",
            "executed": stage not in {"validation", "preflight"},
            "success": False,
            "actual_generation": False,
            "verified": False,
            "provider": ComfyUIProvider.provider_id,
            "stage": stage,
            "message": message[:1000],
            "coexistence_supported": False,
            "lifecycle_mode": "sequential_exclusive",
        }

    async def generate(self, args: dict) -> dict[str, Any]:
        try:
            prompt, width, height, seed = self._validated_args(args)
        except (TypeError, ValueError, ImageGenerationError) as exc:
            return self._failure("validation", exc)

        preflight = await self.provider.status()
        if not preflight.get("generation_available"):
            return {
                **self._failure(
                    "preflight",
                    ImageGenerationError(
                        "The configured ComfyUI runtime is unavailable or its listener identity is unverified"
                    ),
                ),
                "readiness": preflight,
            }

        stage = "scheduler"
        handle: Optional[RuntimeHandle] = None
        lease: dict[str, Any] = {}
        generated_result: Optional[dict[str, Any]] = None
        try:
            async with self._lock:
                async with self.router.external_workload_session("image_generation") as lease:
                    stage = "runtime_start"
                    handle = await self.provider.ensure_runtime()
                    try:
                        stage = "generation"
                        result = await self.provider.generate(
                            prompt, width=width, height=height, seed=seed,
                            runtime=handle,
                        )
                        generated_result = dict(result)
                    finally:
                        stage = "runtime_release"
                        if handle is not None:
                            await _await_cleanup(self.provider.release_runtime(handle))
                    stage = "model_restore"
                stage = "model_restore"
            result["lifecycle"] = {
                "mode": "sequential_exclusive",
                "previous_worker": lease.get("previous_worker"),
                "model_stopped": lease.get("model_stopped") is True,
                "model_restored": lease.get("model_restored") is True,
                "gpu_indices": lease.get("gpu_indices") or [],
                "external_runtime": "spawned" if handle and handle.spawned else "exact_reuse",
            }
            if not result["lifecycle"]["model_restored"]:
                raise ImageGenerationError("The prior model was not verified restored")
            return result
        except Exception as exc:  # model/runtime failures become receipt-classified results
            failure = self._failure(stage, exc)
            if generated_result is None:
                return failure
            # Preserve evidence that a verified file really was produced while
            # refusing to call the overall action successful when runtime
            # cleanup or model restoration failed. The orchestrator renders a
            # status card, never a generated-image success card, in this state.
            lifecycle = {
                "mode": "sequential_exclusive",
                "previous_worker": lease.get("previous_worker"),
                "model_stopped": lease.get("model_stopped") is True,
                "model_restored": lease.get("model_restored") is True,
                "gpu_indices": lease.get("gpu_indices") or [],
                "external_runtime": "spawned" if handle and handle.spawned else "unowned",
            }
            return {
                **generated_result,
                **failure,
                "executed": True,
                "actual_generation": True,
                "verified": True,
                "lifecycle": lifecycle,
            }


def generated_image_path(config: ImageGenerationConfig, filename: str) -> Path:
    """Resolve only content-addressed PNG names inside the owned artifact root."""
    if not _SAFE_IMAGE_NAME_RE.fullmatch(str(filename or "")):
        raise ImageGenerationError("Invalid generated-image name")
    candidate = (config.output_dir / filename).resolve()
    if candidate.parent != config.output_dir.resolve():
        raise ImageGenerationError("Generated-image path escaped its owned root")
    return candidate
