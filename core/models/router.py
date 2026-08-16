"""X Omni model lifecycle ownership, readiness, and swap coordination.

The model worker is disposable; the Core is not. This module treats the port,
process, command line, model catalog, context size, process start time, and GPU
attachment as one ownership contract. A listener that does not satisfy the
complete contract is foreign and is never adopted or stopped.

The target machine can hold only one 30B worker at a time. ``swap_to`` waits
for every inference lease to finish, stops only the exact process previously
proved healthy, waits for every configured GPU to release its VRAM, and then
starts the requested worker. Callers must hold ``inference_session()`` around
each complete model HTTP attempt; recovery must happen after that lease exits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
import psutil

log = logging.getLogger("xomni.router")


class WorkerSwapError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    alias: str
    executable: Path
    model_path: Path
    host: str
    port: int
    context_tokens: int
    ngl: int = 99
    parallel: int = 1
    mmproj: Optional[Path] = None
    no_mmproj_offload: bool = False
    supports_vision: bool = False
    supports_audio: bool = False
    description: str = ""
    extra_args: tuple[str, ...] = ()
    gpu_indices: tuple[int, ...] = (0, 1)
    gpu_free_thresholds_mib: tuple[tuple[int, int], ...] = ()

    def build_args(self) -> list[str]:
        args = [
            "-m", str(self.model_path),
            "--alias", self.alias,
            "--host", self.host,
            "--port", str(self.port),
            "-c", str(self.context_tokens),
            "-ngl", str(self.ngl),
            "--parallel", str(self.parallel),
        ]
        if self.mmproj:
            args += ["--mmproj", str(self.mmproj)]
        if self.no_mmproj_offload:
            args.append("--no-mmproj-offload")
        args += list(self.extra_args)
        return args

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def gpu_thresholds(self) -> dict[int, int]:
        return dict(self.gpu_free_thresholds_mib)


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    executable: Path
    command_line: tuple[str, ...]
    started_at: float


@dataclass(frozen=True)
class LiveProof:
    models: tuple[str, ...]
    context_tokens: Optional[int]

    def matches(self, cfg: WorkerConfig) -> bool:
        return cfg.alias in self.models and self.context_tokens == cfg.context_tokens


def load_workers(path: Path) -> tuple[dict[str, WorkerConfig], str]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    configs: dict[str, WorkerConfig] = {}
    for name, c in raw["workers"].items():
        executable = Path(c["executable"])
        model_path = Path(c["model_path"])
        if not executable.exists():
            log.warning("Worker '%s': executable not found at %s", name, executable)
        if not model_path.exists():
            log.warning("Worker '%s': model not found at %s", name, model_path)
        thresholds = tuple(
            sorted(
                (int(index), int(value))
                for index, value in c.get("gpu_free_thresholds_mib", {}).items()
            )
        )
        configs[name] = WorkerConfig(
            name=name,
            alias=c["alias"],
            executable=executable,
            model_path=model_path,
            host=c.get("host", "127.0.0.1"),
            port=int(c["port"]),
            context_tokens=int(c["context_tokens"]),
            ngl=int(c.get("ngl", 99)),
            parallel=int(c.get("parallel", 1)),
            mmproj=Path(c["mmproj"]) if c.get("mmproj") else None,
            no_mmproj_offload=bool(c.get("no_mmproj_offload", False)),
            supports_vision=bool(c.get("supports_vision", False)),
            supports_audio=bool(c.get("supports_audio", False)),
            description=c.get("description", ""),
            extra_args=tuple(str(arg) for arg in c.get("extra_args", [])),
            gpu_indices=tuple(int(i) for i in c.get("gpu_indices", (0, 1))),
            gpu_free_thresholds_mib=thresholds,
        )

    default_worker = str(raw["default_worker"])
    if default_worker not in configs:
        raise WorkerSwapError(f"Default worker '{default_worker}' is not configured")
    ports = {cfg.port for cfg in configs.values()}
    if len(ports) != 1:
        raise WorkerSwapError(
            "All mutually exclusive X Omni workers must share one dedicated model port"
        )
    for cfg in configs.values():
        if cfg.host not in {"127.0.0.1", "localhost", "::1"}:
            raise WorkerSwapError(
                f"Worker '{cfg.name}' must bind loopback, not {cfg.host!r}"
            )
        if not cfg.gpu_indices:
            raise WorkerSwapError(f"Worker '{cfg.name}' has no configured GPU indices")
    return configs, default_worker


class _CrossProcessFileLock:
    """Dependency-free cross-process lifecycle lock.

    On Windows, ``msvcrt.locking`` maps to a kernel byte-range lock. It is an
    equivalent safety primitive to the XV12 named launcher mutex while also
    remaining testable on non-Windows development hosts.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self, timeout_s: float) -> None:
        if self._fd is not None:
            raise RuntimeError("cross-process lifecycle lock is already held")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"0")
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise WorkerSwapError(
                        "Another X Omni process is still changing the model lifecycle"
                    )
                time.sleep(0.1)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class _InferenceCoordinator:
    """Async shared inference leases and an exclusive lifecycle lease."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self.active_inferences = 0
        self.lifecycle_waiters = 0
        self.lifecycle_active = False

    @asynccontextmanager
    async def inference(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self.lifecycle_active and self.lifecycle_waiters == 0
            )
            self.active_inferences += 1
        try:
            yield
        finally:
            async with self._condition:
                self.active_inferences -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def lifecycle(self) -> AsyncIterator[None]:
        async with self._condition:
            self.lifecycle_waiters += 1
            try:
                await self._condition.wait_for(
                    lambda: not self.lifecycle_active and self.active_inferences == 0
                )
                self.lifecycle_active = True
            finally:
                self.lifecycle_waiters -= 1
        try:
            yield
        finally:
            async with self._condition:
                self.lifecycle_active = False
                self._condition.notify_all()


class ModelRouter:
    """Own exactly one verified llama-server process at a time."""

    def __init__(
        self,
        configs: dict[str, WorkerConfig],
        default_worker: str,
        vram_free_threshold_mib: int = 15000,
        gpu_index: int = 0,
        health_timeout_s: float = 180.0,
        vram_timeout_s: float = 45.0,
        lifecycle_lock_timeout_s: Optional[float] = None,
        lifecycle_lock_path: Optional[Path] = None,
    ):
        if default_worker not in configs:
            raise WorkerSwapError(f"Default worker '{default_worker}' is not configured")
        self.configs = configs
        self.default_worker = default_worker
        # Retain compatibility with the existing environment knobs. Per-worker
        # thresholds add every other GPU to the release contract.
        self.vram_free_threshold_mib = vram_free_threshold_mib
        self.gpu_index = gpu_index
        self.health_timeout_s = health_timeout_s
        self.vram_timeout_s = vram_timeout_s
        self.lifecycle_lock_timeout_s = lifecycle_lock_timeout_s or (
            health_timeout_s + vram_timeout_s + 30.0
        )

        ports = sorted({cfg.port for cfg in configs.values()})
        lock_name = "-".join(str(port) for port in ports)
        lock_path = lifecycle_lock_path or (
            Path(tempfile.gettempdir()) / f"xomni-model-{lock_name}.lock"
        )
        self._process_lock = _CrossProcessFileLock(lock_path)
        self._lock = asyncio.Lock()
        self._coordinator = _InferenceCoordinator()

        self.active_name: Optional[str] = None
        self.active_pid: Optional[int] = None
        self.active_started_at: Optional[float] = None
        self.swapping: bool = False
        self.external_workload: Optional[str] = None
        self.last_swap_seconds: Optional[float] = None
        self._listeners: list = []

    @classmethod
    def from_config(cls, path: Path, **kwargs) -> "ModelRouter":
        configs, default_worker = load_workers(path)
        return cls(configs, default_worker, **kwargs)

    # ---------- observers and coordination ----------

    def subscribe(self, callback) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, event: dict) -> None:
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:  # noqa: BLE001
                log.exception("worker state listener failed")

    @asynccontextmanager
    async def inference_session(self) -> AsyncIterator[None]:
        """Prevent a swap from terminating the worker during one HTTP attempt.

        The client must release this lease before invoking ``recover()``.
        Multiple reads may coexist; a waiting lifecycle operation blocks new
        reads so a requested model swap cannot starve indefinitely.
        """
        async with self._coordinator.inference():
            if self.active_config() is None:
                raise WorkerSwapError("No model worker is active")
            yield

    @staticmethod
    async def _await_lifecycle_completion(awaitable) -> tuple[object, bool]:
        """Finish an ownership mutation even if its caller is cancelled.

        ``asyncio.to_thread`` work does not stop when the awaiting task is
        cancelled. Shielding and then waiting to completion prevents a lock,
        process stop, or process restore from continuing after its ownership
        scope has already been released. The caller re-raises cancellation
        only after the mutation reaches a known state.
        """
        task = asyncio.create_task(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled

    @asynccontextmanager
    async def _lifecycle_session(self) -> AsyncIterator[None]:
        async with self._lock:
            async with self._coordinator.lifecycle():
                _, acquire_cancelled = await self._await_lifecycle_completion(
                    asyncio.to_thread(
                        self._process_lock.acquire, self.lifecycle_lock_timeout_s
                    )
                )
                if acquire_cancelled:
                    await self._await_lifecycle_completion(
                        asyncio.to_thread(self._process_lock.release)
                    )
                    raise asyncio.CancelledError
                try:
                    yield
                finally:
                    _, release_cancelled = await self._await_lifecycle_completion(
                        asyncio.to_thread(self._process_lock.release)
                    )
                    if release_cancelled:
                        raise asyncio.CancelledError

    async def _restore_external_worker(
        self, previous_cfg: WorkerConfig, lease: dict
    ) -> None:
        restore_record: Optional[ProcessRecord] = None
        try:
            listener = await asyncio.to_thread(
                self._find_pid_on_port, previous_cfg.port
            )
            if listener is not None:
                raise WorkerSwapError(
                    f"Cannot restore '{previous_cfg.name}': foreign or unmanaged "
                    f"pid {listener} owns model port {previous_cfg.port}"
                )
            await asyncio.to_thread(self._wait_vram_free, previous_cfg)
            restore_record = await asyncio.to_thread(self._spawn, previous_cfg)
            health_s, proof, gpu_indices = await asyncio.to_thread(
                self._wait_healthy,
                previous_cfg,
                restore_record.pid,
                restore_record.started_at,
            )
            self._set_active(previous_cfg, restore_record)
            lease["model_restored"] = True
            lease["restore"] = {
                "worker": previous_cfg.name,
                "pid": restore_record.pid,
                "process_started_at": restore_record.started_at,
                "health_wait_s": round(health_s, 1),
                "alias": previous_cfg.alias,
                "context_tokens": proof.context_tokens,
                "gpu_indices": list(gpu_indices),
            }
        except BaseException:
            if restore_record is not None:
                await asyncio.to_thread(
                    self._cleanup_failed_spawn, previous_cfg, restore_record
                )
            raise

    @asynccontextmanager
    async def external_workload_session(self, name: str) -> AsyncIterator[dict]:
        """Temporarily yield both GPUs to one explicitly managed workload.

        The exclusive lifecycle lease waits for active model HTTP attempts and
        blocks new ones. The verified current worker is then stopped and every
        configured GPU must satisfy the release threshold before the caller may
        start an external runtime. On exit, the caller must already have stopped
        or unloaded that runtime; this method proves GPU release again and
        restores the exact worker that was active before the hand-off.

        This is intentionally a narrow scheduling primitive, not permission to
        adopt or terminate an arbitrary process. External-runtime identity and
        cleanup remain the caller's responsibility.
        """
        workload = str(name or "").strip()
        if not workload or len(workload) > 80:
            raise WorkerSwapError("External workload name is missing or invalid")

        async with self._lifecycle_session():
            previous_cfg = self.active_config()
            if previous_cfg is None:
                raise WorkerSwapError(
                    "A verified model worker must be active before an external GPU workload"
                )

            started = time.monotonic()
            lease = {
                "workload": workload,
                "previous_worker": previous_cfg.name,
                "gpu_indices": list(previous_cfg.gpu_indices),
                "model_stopped": False,
                "model_restored": False,
            }
            body_error: Optional[BaseException] = None
            self.swapping = True
            self.external_workload = workload
            self._notify({**self.status(), "external_workload": workload})
            try:
                stopped, stop_cancelled = await self._await_lifecycle_completion(
                    asyncio.to_thread(self._stop_active_worker)
                )
                if stopped is None:
                    raise WorkerSwapError("Verified model worker disappeared before hand-off")
                lease["model_stopped"] = True
                if stop_cancelled:
                    raise asyncio.CancelledError
                yield lease
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                restore_error: Optional[BaseException] = None
                restore_cancelled = False
                if lease["model_stopped"]:
                    try:
                        _, restore_cancelled = await self._await_lifecycle_completion(
                            self._restore_external_worker(previous_cfg, lease)
                        )
                    except BaseException as exc:
                        restore_error = exc

                self.last_swap_seconds = round(time.monotonic() - started, 1)
                self.external_workload = None
                self.swapping = False
                self._notify(self.status())
                if restore_error is not None:
                    message = (
                        f"External workload '{workload}' ended, but verified worker "
                        f"'{previous_cfg.name}' could not be restored: {restore_error}"
                    )
                    if body_error is not None:
                        message += f" (the workload also failed: {type(body_error).__name__})"
                    raise WorkerSwapError(message) from restore_error
                if restore_cancelled:
                    raise asyncio.CancelledError

    # ---------- exact process identity ----------

    @staticmethod
    def _path_key(path: Path | str) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def _inspect_process(self, pid: int) -> ProcessRecord:
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe())
            command_line = tuple(process.cmdline())
            started_at = float(process.create_time())
        except psutil.NoSuchProcess as exc:
            raise WorkerSwapError(f"Process pid {pid} no longer exists") from exc
        except psutil.AccessDenied as exc:
            raise WorkerSwapError(
                f"Access denied inspecting listener pid {pid}; ownership is unverified"
            ) from exc
        if not command_line or started_at <= 0:
            raise WorkerSwapError(f"Process pid {pid} has incomplete identity metadata")
        return ProcessRecord(pid, executable, command_line, started_at)

    @staticmethod
    def _path_argument_indices(command: tuple[str, ...]) -> set[int]:
        result = {0}
        for flag in ("-m", "--mmproj"):
            try:
                result.add(command.index(flag) + 1)
            except ValueError:
                pass
        return result

    def _identity_issues(
        self,
        record: ProcessRecord,
        cfg: WorkerConfig,
        expected_started_at: Optional[float] = None,
    ) -> list[str]:
        issues: list[str] = []
        if self._path_key(record.executable) != self._path_key(cfg.executable):
            issues.append("executable_mismatch")
        expected_command = (str(cfg.executable), *cfg.build_args())
        if len(record.command_line) != len(expected_command):
            issues.append("command_line_mismatch")
        else:
            path_indices = self._path_argument_indices(expected_command)
            for index, (actual, expected) in enumerate(
                zip(record.command_line, expected_command, strict=True)
            ):
                if index in path_indices:
                    equal = self._path_key(actual) == self._path_key(expected)
                else:
                    equal = actual == expected
                if not equal:
                    issues.append("command_line_mismatch")
                    break
        if expected_started_at is not None and abs(record.started_at - expected_started_at) >= 0.01:
            issues.append("start_time_mismatch")
        return sorted(set(issues))

    def _find_pid_on_port(self, port: int) -> Optional[int]:
        try:
            connections = psutil.net_connections(kind="tcp")
        except psutil.AccessDenied as exc:
            raise WorkerSwapError(
                "Access denied enumerating TCP listeners; port ownership is unverified"
            ) from exc
        for connection in connections:
            if (
                connection.laddr
                and connection.laddr.port == port
                and connection.status == psutil.CONN_LISTEN
                and connection.pid
            ):
                return int(connection.pid)
        return None

    def _require_identity(
        self,
        pid: int,
        cfg: WorkerConfig,
        expected_started_at: Optional[float] = None,
    ) -> ProcessRecord:
        record = self._inspect_process(pid)
        issues = self._identity_issues(record, cfg, expected_started_at)
        if issues:
            raise WorkerSwapError(
                f"Foreign or wrong runtime on model port {cfg.port}: pid {pid}; "
                f"identity checks failed: {', '.join(issues)}. X Omni will not reuse or stop it."
            )
        return record

    # ---------- live model and GPU proof ----------

    def _model_proof(self, cfg: WorkerConfig) -> LiveProof:
        response = httpx.get(f"{cfg.base_url}/models", timeout=3, trust_env=False)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise WorkerSwapError("Model catalog returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise WorkerSwapError("Model catalog response is missing a data list")
        entries = payload["data"]
        if not all(isinstance(item, dict) for item in entries):
            raise WorkerSwapError("Model catalog contains an invalid entry")
        models = tuple(str(item.get("id")) for item in entries)
        matching = next((item for item in entries if str(item.get("id")) == cfg.alias), None)
        raw_context = ((matching or {}).get("meta") or {}).get("n_ctx")
        try:
            context = int(raw_context) if raw_context is not None else None
        except (TypeError, ValueError):
            context = None
        return LiveProof(models=models, context_tokens=context)

    def _gpu_memory_mib(self) -> dict[int, tuple[int, int]]:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
        result: dict[int, tuple[int, int]] = {}
        for line in output.strip().splitlines():
            index, free, total = (part.strip() for part in line.split(","))
            result[int(index)] = (int(free), int(total))
        return result

    def _gpu_indices_for_pid(self, pid: int) -> set[int]:
        # ``--query-compute-apps`` loses the correct PID on this Windows WDDM
        # machine. pmon reports the actual llama-server PID on both GPUs.
        output = subprocess.check_output(["nvidia-smi", "pmon", "-c", "1"], text=True)
        indices: set[int] = set()
        for line in output.splitlines():
            fields = line.split()
            if not fields or fields[0].startswith("#") or len(fields) < 2:
                continue
            try:
                index, process_id = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            if process_id == pid:
                indices.add(index)
        return indices

    def _required_gpu_thresholds(self, cfg: WorkerConfig) -> dict[int, int]:
        thresholds = cfg.gpu_thresholds
        thresholds[self.gpu_index] = self.vram_free_threshold_mib
        snapshot = self._gpu_memory_mib()
        missing = sorted(set(cfg.gpu_indices) - set(snapshot))
        if missing:
            raise WorkerSwapError(f"Configured GPUs are not present: {missing}")
        for index in cfg.gpu_indices:
            if index not in thresholds:
                # Conservative fallback for old worker files: require 90% of
                # each card to be free, while still checking every card.
                thresholds[index] = int(snapshot[index][1] * 0.90)
        return thresholds

    def _wait_vram_free(self, cfg: WorkerConfig) -> float:
        start = time.monotonic()
        thresholds = self._required_gpu_thresholds(cfg)
        last: dict[int, tuple[int, int]] = {}
        while True:
            snapshot = self._gpu_memory_mib()
            last = {index: snapshot[index] for index in cfg.gpu_indices}
            if all(last[index][0] >= thresholds[index] for index in cfg.gpu_indices):
                summary = ", ".join(
                    f"GPU{index}={last[index][0]} MiB" for index in cfg.gpu_indices
                )
                elapsed = time.monotonic() - start
                log.info(
                    "VRAM released on every configured GPU (%s) after %.1fs",
                    summary,
                    elapsed,
                )
                return elapsed
            if time.monotonic() - start > self.vram_timeout_s:
                detail = ", ".join(
                    f"GPU{index} free={last[index][0]} required={thresholds[index]} MiB"
                    for index in cfg.gpu_indices
                )
                raise WorkerSwapError(
                    f"VRAM did not release on every configured GPU within "
                    f"{self.vram_timeout_s}s ({detail})"
                )
            time.sleep(0.5)

    def _wait_healthy(
        self,
        cfg: WorkerConfig,
        expected_pid: int,
        expected_started_at: float,
    ) -> tuple[float, LiveProof, tuple[int, ...]]:
        start = time.monotonic()
        last_error = "listener_not_ready"
        while True:
            self._require_identity(expected_pid, cfg, expected_started_at)
            listener_pid = self._find_pid_on_port(cfg.port)
            if listener_pid is not None and listener_pid != expected_pid:
                raise WorkerSwapError(
                    f"Port {cfg.port} became owned by foreign pid {listener_pid}; "
                    "X Omni will not stop it"
                )
            if listener_pid == expected_pid:
                self._require_identity(listener_pid, cfg, expected_started_at)
                try:
                    proof = self._model_proof(cfg)
                    if not proof.matches(cfg):
                        raise WorkerSwapError(
                            f"Worker '{cfg.name}' reported alias/context "
                            f"{list(proof.models)}/{proof.context_tokens}; expected "
                            f"{cfg.alias}/{cfg.context_tokens}"
                        )
                    gpu_indices = tuple(sorted(self._gpu_indices_for_pid(expected_pid)))
                    missing_gpus = sorted(set(cfg.gpu_indices) - set(gpu_indices))
                    if not missing_gpus:
                        return time.monotonic() - start, proof, gpu_indices
                    last_error = f"worker_not_attached_to_gpus_{missing_gpus}"
                except httpx.HTTPError as exc:
                    last_error = type(exc).__name__
            if time.monotonic() - start > self.health_timeout_s:
                raise WorkerSwapError(
                    f"'{cfg.name}' did not satisfy process, alias, context, and GPU "
                    f"readiness within {self.health_timeout_s}s (last: {last_error})"
                )
            time.sleep(0.75)

    # ---------- start, stop, and failure cleanup ----------

    def _spawn(self, cfg: WorkerConfig) -> ProcessRecord:
        if not cfg.executable.exists():
            raise WorkerSwapError(f"llama-server not found: {cfg.executable}")
        if not cfg.model_path.exists():
            raise WorkerSwapError(f"Model file not found: {cfg.model_path}")
        existing = self._find_pid_on_port(cfg.port)
        if existing is not None:
            raise WorkerSwapError(
                f"Model port {cfg.port} is already held by pid {existing}; X Omni will not replace it"
            )
        args = [str(cfg.executable), *cfg.build_args()]
        log.info("Starting verified worker '%s' on dedicated port %d", cfg.name, cfg.port)
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        process = subprocess.Popen(args, creationflags=creationflags)
        try:
            record = self._inspect_process(process.pid)
            issues = self._identity_issues(record, cfg)
            if issues:
                raise WorkerSwapError(
                    f"Spawned pid {process.pid} failed identity checks: {', '.join(issues)}"
                )
            return record
        except Exception:
            # Popen is an exact OS handle for this spawn, so this cannot target
            # a recycled PID even if psutil inspection failed.
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            raise

    def _terminate_exact(
        self,
        cfg: WorkerConfig,
        pid: int,
        started_at: float,
        *,
        require_live_proof: bool,
    ) -> bool:
        try:
            record = self._require_identity(pid, cfg, started_at)
        except WorkerSwapError as exc:
            if not psutil.pid_exists(pid):
                return False
            raise exc

        listener_pid = self._find_pid_on_port(cfg.port)
        if listener_pid is not None and listener_pid != pid:
            raise WorkerSwapError(
                f"Foreign pid {listener_pid} owns model port {cfg.port}; X Omni will not stop it"
            )
        if listener_pid == pid and require_live_proof:
            proof = self._model_proof(cfg)
            if not proof.matches(cfg):
                raise WorkerSwapError(f"Refusing to stop pid {pid}: live alias/context proof failed")

        try:
            process = psutil.Process(record.pid)
            # Recheck the start time immediately before sending a signal.
            if abs(float(process.create_time()) - started_at) >= 0.01:
                raise WorkerSwapError(f"Refusing to stop pid {pid}: process start time changed")
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                # Still the same process record; a force kill remains scoped.
                if abs(float(process.create_time()) - started_at) >= 0.01:
                    raise WorkerSwapError(f"Refusing to kill pid {pid}: process start time changed")
                process.kill()
                process.wait(timeout=10)
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied as exc:
            raise WorkerSwapError(f"Access denied stopping verified worker pid {pid}") from exc

    def _cleanup_failed_spawn(self, cfg: WorkerConfig, record: ProcessRecord) -> None:
        try:
            self._terminate_exact(cfg, record.pid, record.started_at, require_live_proof=False)
            self._wait_vram_free(cfg)
        except Exception:  # noqa: BLE001
            log.exception(
                "Could not clean up exact failed spawn pid %d; no other process was targeted",
                record.pid,
            )

    def _stop_active_worker(self) -> Optional[WorkerConfig]:
        if self.active_name is None or self.active_pid is None or self.active_started_at is None:
            return None
        cfg = self.configs[self.active_name]
        self._terminate_exact(
            cfg,
            self.active_pid,
            self.active_started_at,
            require_live_proof=True,
        )
        # Once the exact process is gone it is no longer truthful to publish it
        # as active, even if CUDA teardown subsequently exceeds its deadline.
        self.active_name = None
        self.active_pid = None
        self.active_started_at = None
        # A process that already exited can leave CUDA teardown in progress.
        # The next worker must not start until every configured card is free.
        self._wait_vram_free(cfg)
        return cfg

    def _set_active(self, cfg: WorkerConfig, record: ProcessRecord) -> None:
        self.active_name = cfg.name
        self.active_pid = record.pid
        self.active_started_at = record.started_at

    # ---------- async public lifecycle API ----------

    async def start_default(self, pre_start=None) -> dict:
        cfg = self.configs[self.default_worker]
        async with self._lifecycle_session():
            reconciliation = None
            if pre_start is not None:
                try:
                    candidate = pre_start()
                    if hasattr(candidate, "__await__"):
                        reconciliation, cancelled = await self._await_lifecycle_completion(
                            candidate
                        )
                        if cancelled:
                            raise asyncio.CancelledError
                    else:
                        reconciliation = candidate
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise WorkerSwapError(
                        f"External GPU runtime reconciliation failed before model startup: {exc}"
                    ) from exc
            existing = await asyncio.to_thread(self._find_pid_on_port, cfg.port)
            if existing is not None:
                record = await asyncio.to_thread(self._require_identity, existing, cfg)
                elapsed, proof, gpu_indices = await asyncio.to_thread(
                    self._wait_healthy, cfg, record.pid, record.started_at
                )
                self._set_active(cfg, record)
                self._notify(self.status())
                result = {
                    "worker": cfg.name,
                    "adopted": True,
                    "pid": record.pid,
                    "process_started_at": record.started_at,
                    "startup_s": round(elapsed, 1),
                    "alias": cfg.alias,
                    "context_tokens": proof.context_tokens,
                    "gpu_indices": list(gpu_indices),
                }
                if reconciliation is not None:
                    result["external_reconciliation"] = reconciliation
                return result

            self.swapping = True
            self._notify(self.status())
            record: Optional[ProcessRecord] = None
            try:
                # A model may have crashed without releasing its CUDA context.
                await asyncio.to_thread(self._wait_vram_free, cfg)
                record = await asyncio.to_thread(self._spawn, cfg)
                elapsed, proof, gpu_indices = await asyncio.to_thread(
                    self._wait_healthy, cfg, record.pid, record.started_at
                )
                self._set_active(cfg, record)
                result = {
                    "worker": cfg.name,
                    "adopted": False,
                    "pid": record.pid,
                    "process_started_at": record.started_at,
                    "startup_s": round(elapsed, 1),
                    "alias": cfg.alias,
                    "context_tokens": proof.context_tokens,
                    "gpu_indices": list(gpu_indices),
                }
                if reconciliation is not None:
                    result["external_reconciliation"] = reconciliation
                return result
            except Exception:
                if record is not None:
                    await asyncio.to_thread(self._cleanup_failed_spawn, cfg, record)
                self.active_name = None
                self.active_pid = None
                self.active_started_at = None
                raise
            finally:
                self.swapping = False
                self._notify(self.status())

    async def swap_to(self, name: str) -> dict:
        if name not in self.configs:
            raise WorkerSwapError(f"Unknown worker '{name}'. Known: {list(self.configs)}")
        async with self._lifecycle_session():
            if self.active_name == name:
                health = await self.health()
                if not health.get("ready"):
                    raise WorkerSwapError(
                        f"Worker '{name}' is recorded active but failed live ownership/readiness"
                    )
                return {"worker": name, "swapped": False, "reason": "already_active"}

            started = time.monotonic()
            previous = self.active_name
            self.swapping = True
            self._notify({**self.status(), "swapping_to": name})
            record: Optional[ProcessRecord] = None
            cfg = self.configs[name]
            try:
                await asyncio.to_thread(self._stop_active_worker)
                listener = await asyncio.to_thread(self._find_pid_on_port, cfg.port)
                if listener is not None:
                    raise WorkerSwapError(
                        f"Foreign or unmanaged pid {listener} owns model port {cfg.port}; "
                        "X Omni will not stop it"
                    )
                record = await asyncio.to_thread(self._spawn, cfg)
                health_s, proof, gpu_indices = await asyncio.to_thread(
                    self._wait_healthy, cfg, record.pid, record.started_at
                )
                self._set_active(cfg, record)
                self.last_swap_seconds = round(time.monotonic() - started, 1)
                return {
                    "worker": name,
                    "from": previous,
                    "swapped": True,
                    "pid": record.pid,
                    "process_started_at": record.started_at,
                    "total_swap_s": self.last_swap_seconds,
                    "health_wait_s": round(health_s, 1),
                    "alias": cfg.alias,
                    "context_tokens": proof.context_tokens,
                    "gpu_indices": list(gpu_indices),
                }
            except Exception:
                if record is not None:
                    await asyncio.to_thread(self._cleanup_failed_spawn, cfg, record)
                raise
            finally:
                self.swapping = False
                self._notify(self.status())

    async def recover(self) -> Optional[dict]:
        """Relaunch a dead active worker without touching a foreign listener."""
        name = self.active_name or self.default_worker
        cfg = self.configs[name]
        async with self._lifecycle_session():
            listener_pid = await asyncio.to_thread(self._find_pid_on_port, cfg.port)
            if listener_pid is not None:
                # A listener is not evidence of recovery. Prove its exact
                # process and model contract or surface the conflict.
                expected_start = self.active_started_at if listener_pid == self.active_pid else None
                record = await asyncio.to_thread(
                    self._require_identity, listener_pid, cfg, expected_start
                )
                await asyncio.to_thread(
                    self._wait_healthy, cfg, record.pid, record.started_at
                )
                if listener_pid == self.active_pid:
                    return None
                raise WorkerSwapError(
                    f"Verified but unmanaged worker pid {listener_pid} is already on port {cfg.port}"
                )

            self.swapping = True
            self._notify({**self.status(), "recovering": True})
            record: Optional[ProcessRecord] = None
            try:
                # If an old active PID still exists without a listener, only
                # its exact identity/start record may be cleaned up.
                if self.active_pid is not None and self.active_started_at is not None:
                    await asyncio.to_thread(
                        self._terminate_exact,
                        cfg,
                        self.active_pid,
                        self.active_started_at,
                        require_live_proof=False,
                    )
                self.active_name = None
                self.active_pid = None
                self.active_started_at = None
                await asyncio.to_thread(self._wait_vram_free, cfg)
                record = await asyncio.to_thread(self._spawn, cfg)
                elapsed, proof, gpu_indices = await asyncio.to_thread(
                    self._wait_healthy, cfg, record.pid, record.started_at
                )
                self._set_active(cfg, record)
                return {
                    "worker": name,
                    "recovered": True,
                    "pid": record.pid,
                    "process_started_at": record.started_at,
                    "startup_s": round(elapsed, 1),
                    "alias": cfg.alias,
                    "context_tokens": proof.context_tokens,
                    "gpu_indices": list(gpu_indices),
                }
            except Exception:
                if record is not None:
                    await asyncio.to_thread(self._cleanup_failed_spawn, cfg, record)
                raise
            finally:
                self.swapping = False
                self._notify(self.status())

    async def ensure_capability(
        self, *, vision: bool = False, audio: bool = False
    ) -> Optional[dict]:
        cfg = self.active_config()
        if cfg and (not vision or cfg.supports_vision) and (not audio or cfg.supports_audio):
            return None
        for name, candidate in self.configs.items():
            if (not vision or candidate.supports_vision) and (not audio or candidate.supports_audio):
                return await self.swap_to(name)
        need = " and ".join(
            label
            for label, required in (("vision", vision), ("audio", audio))
            if required
        )
        raise WorkerSwapError(f"No configured worker supports {need}.")

    # ---------- truthful introspection ----------

    def active_config(self) -> Optional[WorkerConfig]:
        return self.configs.get(self.active_name) if self.active_name else None

    def supports_vision(self) -> bool:
        cfg = self.active_config()
        return bool(cfg and cfg.supports_vision)

    def supports_audio(self) -> bool:
        cfg = self.active_config()
        return bool(cfg and cfg.supports_audio)

    def status(self) -> dict:
        cfg = self.active_config()
        state = (
            "external_workload"
            if self.external_workload
            else "swapping"
            if self.swapping
            else "active_unverified"
            if cfg
            else "stopped"
        )
        return {
            "type": "worker_state",
            "state": state,
            "active_worker": self.active_name,
            "active_pid": self.active_pid,
            "active_process_started_at": self.active_started_at,
            "swapping": self.swapping,
            "external_workload": self.external_workload,
            "active_inferences": self._coordinator.active_inferences,
            "lifecycle_waiters": self._coordinator.lifecycle_waiters,
            "supports_vision": bool(cfg and cfg.supports_vision),
            "supports_audio": bool(cfg and cfg.supports_audio),
            "last_swap_seconds": self.last_swap_seconds,
            "expected": None
            if cfg is None
            else {
                "alias": cfg.alias,
                "port": cfg.port,
                "context_tokens": cfg.context_tokens,
                "gpu_indices": list(cfg.gpu_indices),
            },
            "available": [
                {
                    "name": name,
                    "alias": candidate.alias,
                    "description": candidate.description,
                    "vision": candidate.supports_vision,
                    "audio": candidate.supports_audio,
                    "port": candidate.port,
                    "context_tokens": candidate.context_tokens,
                    "gpu_indices": list(candidate.gpu_indices),
                }
                for name, candidate in self.configs.items()
            ],
        }

    async def health(self) -> dict:
        cfg = self.active_config()
        if cfg is None:
            default = self.configs[self.default_worker]
            listener = await asyncio.to_thread(self._find_pid_on_port, default.port)
            return {
                "reachable": False,
                "ready": False,
                "state": (
                    "external_workload"
                    if self.external_workload
                    else "foreign_or_unmanaged_listener"
                    if listener
                    else "stopped"
                ),
                "error": (
                    "external_workload_active"
                    if self.external_workload
                    else "no_active_worker"
                ),
                "external_workload": self.external_workload,
                "listener_pid": listener,
                "expected_alias": default.alias,
                "expected_context_tokens": default.context_tokens,
                "expected_port": default.port,
            }

        result = {
            "reachable": False,
            "ready": False,
            "state": "unhealthy",
            "worker": cfg.name,
            "expected_alias": cfg.alias,
            "expected_context_tokens": cfg.context_tokens,
            "expected_port": cfg.port,
            "expected_gpu_indices": list(cfg.gpu_indices),
            "listener_pid": None,
            "process_identity_ok": False,
            "process_start_time_ok": False,
            "alias_ok": False,
            "context_ok": False,
            "gpu_ok": False,
            "models": [],
            "context_tokens": None,
            "gpu_indices": [],
            "issues": [],
        }
        try:
            listener = await asyncio.to_thread(self._find_pid_on_port, cfg.port)
            result["listener_pid"] = listener
            if listener is None:
                result["issues"].append("listener_missing")
                return result
            if listener != self.active_pid:
                result["issues"].append("listener_pid_mismatch")
                return result
            record = await asyncio.to_thread(self._inspect_process, listener)
            issues = self._identity_issues(record, cfg, self.active_started_at)
            result["issues"].extend(issues)
            result["process_identity_ok"] = not any(
                issue in {"executable_mismatch", "command_line_mismatch"}
                for issue in issues
            )
            result["process_start_time_ok"] = "start_time_mismatch" not in issues
            if issues:
                return result

            try:
                proof = await asyncio.to_thread(self._model_proof, cfg)
                result["reachable"] = True
                result["models"] = list(proof.models)
                result["context_tokens"] = proof.context_tokens
                result["alias_ok"] = cfg.alias in proof.models
                result["context_ok"] = proof.context_tokens == cfg.context_tokens
            except httpx.HTTPError as exc:
                result["issues"].append(type(exc).__name__)
                return result

            gpu_indices = await asyncio.to_thread(self._gpu_indices_for_pid, listener)
            result["gpu_indices"] = sorted(gpu_indices)
            result["gpu_ok"] = set(cfg.gpu_indices).issubset(gpu_indices)
            if not result["alias_ok"]:
                result["issues"].append("alias_mismatch")
            if not result["context_ok"]:
                result["issues"].append("context_mismatch")
            if not result["gpu_ok"]:
                result["issues"].append("gpu_attachment_mismatch")
            result["ready"] = all(
                result[key]
                for key in (
                    "reachable",
                    "process_identity_ok",
                    "process_start_time_ok",
                    "alias_ok",
                    "context_ok",
                    "gpu_ok",
                )
            )
            result["state"] = "healthy" if result["ready"] else "wrong_runtime"
            return result
        except (WorkerSwapError, psutil.Error, subprocess.SubprocessError, OSError) as exc:
            result["issues"].append(type(exc).__name__)
            result["error"] = type(exc).__name__
            return result

    async def shutdown(self) -> None:
        """Wait for active inference, then stop only the exact verified worker."""
        try:
            async with self._lifecycle_session():
                await asyncio.to_thread(self._stop_active_worker)
        except Exception:  # noqa: BLE001
            log.exception("verified worker shutdown failed; no foreign process was targeted")
        finally:
            self._notify(self.status())
