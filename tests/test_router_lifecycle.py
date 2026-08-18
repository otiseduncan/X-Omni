from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest

from core.models.router import (
    ExternalWorkloadStartError,
    LiveProof,
    ModelRouter,
    ProcessRecord,
    WorkerConfig,
    WorkerProcessExitedError,
    WorkerSwapError,
    _CrossProcessFileLock,
    load_workers,
)


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_listener(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"fixture listener on port {port} did not start")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _fixture_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _write_fixture_worker(tmp_path: Path) -> Path:
    package = tmp_path / "fixture_llama"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        """
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--alias', required=True)
parser.add_argument('--host', required=True)
parser.add_argument('--port', required=True, type=int)
parser.add_argument('-c', required=True, type=int)
args, _ = parser.parse_known_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/v1/models':
            self.send_response(404)
            self.end_headers()
            return
        alias = os.environ.get('FIXTURE_ALIAS', args.alias)
        context = int(os.environ.get('FIXTURE_CONTEXT', str(args.c)))
        body = json.dumps({'data': [{'id': alias, 'meta': {'n_ctx': context}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass

ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )
    return package


def _config(tmp_path: Path, name: str = "omni", port: int | None = None) -> WorkerConfig:
    # ``python -m fixture_llama`` gives the fixture process the same exact
    # command-line shape that llama.cpp uses (executable, -m model, flags).
    # Tests chdir to tmp_path, where this package exists.
    model_package = Path("fixture_llama")
    return WorkerConfig(
        name=name,
        alias=f"fixture-{name}",
        # A venv's sys.executable can be a redirector while Process.exe()
        # truthfully reports the base interpreter. Use the inspected executable
        # on both sides of this process-identity fixture.
        executable=Path(psutil.Process(os.getpid()).exe()),
        model_path=model_package,
        host="127.0.0.1",
        port=port or _free_port(),
        context_tokens=32768,
        extra_args=("--no-webui",),
        gpu_indices=(0, 1),
        gpu_free_thresholds_mib=((0, 15000), (1, 7500)),
    )


def _router(tmp_path: Path, cfg: WorkerConfig, **kwargs) -> ModelRouter:
    return ModelRouter(
        {cfg.name: cfg},
        cfg.name,
        health_timeout_s=2,
        vram_timeout_s=0.2,
        lifecycle_lock_timeout_s=0.2,
        lifecycle_lock_path=tmp_path / f"lifecycle-{cfg.port}.lock",
        **kwargs,
    )


def _launch_exact(cfg: WorkerConfig, env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [str(cfg.executable), *cfg.build_args()],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_fixture_creation_flags(),
    )


def _fake_gpu_proof(monkeypatch: pytest.MonkeyPatch, router: ModelRouter) -> None:
    monkeypatch.setattr(
        router,
        "_gpu_indices_for_pid",
        lambda _pid: {0, 1},
    )
    monkeypatch.setattr(router, "_wait_vram_free", lambda _cfg: 0.0)


def test_repository_workers_use_dedicated_port_and_both_gpus() -> None:
    configs, default = load_workers(ROOT / "config" / "workers.json")
    assert default == "omni"
    assert {cfg.port for cfg in configs.values()} == {8131}
    assert all(cfg.gpu_indices == (0, 1) for cfg in configs.values())
    # GPU1 drives the Windows desktop and has ~7040 MiB free at the clean
    # no-model baseline. 6500 still decisively distinguishes that state from
    # the loaded worker (~380 MiB free) without making release impossible.
    assert all(cfg.gpu_thresholds == {0: 15000, 1: 6500} for cfg in configs.values())


def test_exact_listener_is_adopted_only_after_live_alias_context_gpu_and_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    process = _launch_exact(cfg)
    try:
        _wait_for_listener(cfg.port)
        result = asyncio.run(router.start_default())
        assert result["adopted"] is True
        assert result["pid"] == process.pid
        assert result["context_tokens"] == 32768
        assert result["gpu_indices"] == [0, 1]
        assert router.active_started_at == pytest.approx(psutil.Process(process.pid).create_time())

        health = asyncio.run(router.health())
        assert health["ready"] is True
        assert health["process_identity_ok"] is True
        assert health["process_start_time_ok"] is True
        assert health["alias_ok"] is True
        assert health["context_ok"] is True
        assert health["gpu_ok"] is True

        asyncio.run(router.shutdown())
        process.wait(timeout=5)
    finally:
        _terminate(process)


def test_foreign_listener_is_never_adopted_or_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(cfg.port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_fixture_creation_flags(),
    )
    try:
        _wait_for_listener(cfg.port)
        with pytest.raises(WorkerSwapError, match="will not reuse or stop"):
            asyncio.run(router.start_default())
        assert process.poll() is None
    finally:
        _terminate(process)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"FIXTURE_ALIAS": "wrong-alias"}, "alias/context"),
        ({"FIXTURE_CONTEXT": "4096"}, "alias/context"),
    ],
)
def test_exact_process_with_wrong_live_contract_is_not_adopted_or_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, str],
    message: str,
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    env = os.environ.copy()
    env.update(override)
    process = _launch_exact(cfg, env=env)
    try:
        _wait_for_listener(cfg.port)
        with pytest.raises(WorkerSwapError, match=message):
            asyncio.run(router.start_default())
        assert process.poll() is None
    finally:
        _terminate(process)


def test_start_time_mismatch_prevents_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    process = _launch_exact(cfg)
    try:
        _wait_for_listener(cfg.port)
        asyncio.run(router.start_default())
        router.active_started_at = float(router.active_started_at) - 10
        with pytest.raises(WorkerSwapError, match="start_time_mismatch"):
            router._stop_active_worker()
        assert process.poll() is None
    finally:
        _terminate(process)


def test_stop_retries_transient_model_catalog_timeout_with_identity_reproof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    process = _launch_exact(cfg)
    try:
        _wait_for_listener(cfg.port)
        asyncio.run(router.start_default())
        original_proof = router._model_proof
        attempts = 0

        def transient_proof(worker: WorkerConfig) -> LiveProof:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectTimeout("transient loopback timeout")
            return original_proof(worker)

        monkeypatch.setattr(router, "_model_proof", transient_proof)
        stopped = router._stop_active_worker()

        assert stopped == cfg
        assert attempts == 2
        assert process.poll() is not None
    finally:
        _terminate(process)


def test_exhausted_stop_proof_keeps_original_worker_and_pre_stop_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    process = _launch_exact(cfg)
    try:
        _wait_for_listener(cfg.port)
        asyncio.run(router.start_default())
        attempts = 0

        def unavailable_proof(_worker: WorkerConfig) -> LiveProof:
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectTimeout("persistent loopback timeout")

        monkeypatch.setattr(router, "_model_proof", unavailable_proof)
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)

        async def scenario() -> ExternalWorkloadStartError:
            with pytest.raises(ExternalWorkloadStartError) as captured:
                async with router.external_workload_session("video_generation"):
                    raise AssertionError("failed hand-off must never yield")
            return captured.value

        failure = asyncio.run(scenario())

        assert attempts == 3
        assert process.poll() is None
        assert router.active_pid == process.pid
        assert failure.lifecycle["model_stop_attempted"] is True
        assert failure.lifecycle["model_stopped"] is False
        assert failure.lifecycle["model_restore_required"] is False
        assert failure.lifecycle["model_restored"] is None
        assert failure.stage == "model_stop_readiness"
        assert failure.retryable is True
        assert "Wan" not in str(failure)
    finally:
        _terminate(process)


def test_stop_retry_refuses_listener_owner_change_without_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    pid = 101
    started_at = 1000.0
    record = ProcessRecord(pid, cfg.executable, (str(cfg.executable),), started_at)
    listeners = iter((pid, pid, 202))
    model_calls = 0
    signal_attempted = False

    monkeypatch.setattr(router, "_require_identity", lambda *_args: record)
    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: next(listeners))

    def transient_proof(_worker: WorkerConfig) -> LiveProof:
        nonlocal model_calls
        model_calls += 1
        raise httpx.ConnectTimeout("transient loopback timeout")

    def forbidden_process(_pid: int):
        nonlocal signal_attempted
        signal_attempted = True
        raise AssertionError("replacement ownership must prevent any signal")

    monkeypatch.setattr(router, "_model_proof", transient_proof)
    monkeypatch.setattr("core.models.router.psutil.Process", forbidden_process)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    with pytest.raises(WorkerSwapError, match="ownership changed"):
        router._terminate_exact(
            cfg, pid, started_at, require_live_proof=True
        )

    assert model_calls == 1
    assert signal_attempted is False


def test_external_workload_restores_after_post_stop_vram_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    router.active_name = cfg.name
    router.active_pid = 101
    router.active_started_at = 1000.0
    restored = False

    def stop_then_fail() -> WorkerConfig:
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        raise WorkerSwapError("fixture VRAM release failed after stop")

    async def restore(_cfg: WorkerConfig, lease: dict) -> None:
        nonlocal restored
        restored = True
        lease["model_restored"] = True

    monkeypatch.setattr(router, "_stop_active_worker", stop_then_fail)
    monkeypatch.setattr(router, "_restore_external_worker", restore)

    async def scenario() -> ExternalWorkloadStartError:
        with pytest.raises(ExternalWorkloadStartError) as captured:
            async with router.external_workload_session("video_generation"):
                raise AssertionError("failed hand-off must never yield")
        return captured.value

    failure = asyncio.run(scenario())

    assert restored is True
    assert failure.lifecycle["model_stop_attempted"] is True
    assert failure.lifecycle["model_stopped"] is True
    assert failure.lifecycle["model_restore_required"] is True
    assert failure.lifecycle["model_restored"] is True


def test_external_workload_restore_retries_one_exact_startup_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    records = [
        ProcessRecord(201, cfg.executable, (str(cfg.executable),), 2000.0),
        ProcessRecord(202, cfg.executable, (str(cfg.executable),), 2001.0),
    ]
    spawned: list[int] = []
    cleaned: list[int] = []

    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: None)
    monkeypatch.setattr(router, "_wait_vram_free", lambda _cfg: 0.0)

    def spawn(_cfg: WorkerConfig) -> ProcessRecord:
        record = records[len(spawned)]
        spawned.append(record.pid)
        return record

    def wait_healthy(_cfg, pid, _started_at):
        if pid == 201:
            raise WorkerProcessExitedError("Process pid 201 no longer exists")
        return 12.5, LiveProof((cfg.alias,), cfg.context_tokens), (0, 1)

    monkeypatch.setattr(router, "_spawn", spawn)
    monkeypatch.setattr(router, "_wait_healthy", wait_healthy)
    monkeypatch.setattr(
        router,
        "_cleanup_failed_spawn",
        lambda _cfg, record: cleaned.append(record.pid),
    )
    lease: dict = {}

    asyncio.run(router._restore_external_worker(cfg, lease))

    assert spawned == [201, 202]
    assert cleaned == [201]
    assert router.active_name == cfg.name
    assert router.active_pid == 202
    assert lease["model_restored"] is True
    assert lease["restore"]["attempts"] == 2


def test_external_workload_retains_stopped_truth_when_restore_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    router.active_name = cfg.name
    router.active_pid = 101
    router.active_started_at = 1000.0

    def stop_then_fail() -> WorkerConfig:
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        raise WorkerSwapError("fixture VRAM release failed after stop")

    async def restore_failure(_cfg: WorkerConfig, _lease: dict) -> None:
        raise WorkerSwapError(r"Model file not found: X:\private\secret.gguf")

    monkeypatch.setattr(router, "_stop_active_worker", stop_then_fail)
    monkeypatch.setattr(router, "_restore_external_worker", restore_failure)

    async def scenario() -> ExternalWorkloadStartError:
        with pytest.raises(ExternalWorkloadStartError) as captured:
            async with router.external_workload_session("video_generation"):
                raise AssertionError("failed hand-off must never yield")
        return captured.value

    failure = asyncio.run(scenario())

    assert failure.lifecycle["model_stop_attempted"] is True
    assert failure.lifecycle["model_stopped"] is True
    assert failure.lifecycle["model_restore_required"] is True
    assert failure.lifecycle["model_restored"] is False
    assert "X:\\private" not in str(failure)
    assert "secret.gguf" not in str(failure)


def test_pre_yield_cancellation_with_restore_failure_keeps_lifecycle_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    router.active_name = cfg.name
    router.active_pid = 101
    router.active_started_at = 1000.0
    stop_started = threading.Event()
    allow_stop = threading.Event()

    def delayed_stop() -> WorkerConfig:
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        stop_started.set()
        assert allow_stop.wait(timeout=5)
        return cfg

    async def restore_failure(_cfg: WorkerConfig, _lease: dict) -> None:
        raise WorkerSwapError("fixture restore failed")

    monkeypatch.setattr(router, "_stop_active_worker", delayed_stop)
    monkeypatch.setattr(router, "_restore_external_worker", restore_failure)

    async def enter_workload() -> None:
        async with router.external_workload_session("video_generation"):
            raise AssertionError("cancelled hand-off must never yield")

    async def scenario() -> ExternalWorkloadStartError:
        task = asyncio.create_task(enter_workload())
        assert await asyncio.to_thread(stop_started.wait, 5)
        task.cancel()
        allow_stop.set()
        with pytest.raises(ExternalWorkloadStartError) as captured:
            await task
        return captured.value

    failure = asyncio.run(scenario())

    assert failure.lifecycle["model_stop_attempted"] is True
    assert failure.lifecycle["model_stopped"] is True
    assert failure.lifecycle["model_restore_required"] is True
    assert failure.lifecycle["model_restored"] is False


def test_readiness_failure_cleans_up_only_exact_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    _fake_gpu_proof(monkeypatch, router)
    spawned: list[ProcessRecord] = []
    original_spawn = router._spawn

    def capture_spawn(worker: WorkerConfig) -> ProcessRecord:
        record = original_spawn(worker)
        spawned.append(record)
        return record

    monkeypatch.setattr(router, "_spawn", capture_spawn)
    monkeypatch.setattr(
        router,
        "_wait_healthy",
        lambda *_args: (_ for _ in ()).throw(WorkerSwapError("fixture readiness failed")),
    )
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0, raising=False)

    with pytest.raises(WorkerSwapError, match="fixture readiness failed"):
        asyncio.run(router.start_default())
    assert len(spawned) == 1
    assert not psutil.pid_exists(spawned[0].pid)


def test_inference_lease_blocks_swap_until_generation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    port = _free_port()
    omni = _config(tmp_path, "omni", port)
    coder = _config(tmp_path, "coder", port)
    router = ModelRouter(
        {"omni": omni, "coder": coder},
        "omni",
        lifecycle_lock_path=tmp_path / "coordination.lock",
    )
    router.active_name = "omni"
    router.active_pid = 101
    router.active_started_at = 1000.0
    events: list[str] = []

    def stop_active() -> WorkerConfig:
        events.append("stopped")
        router.active_name = None
        router.active_pid = None
        router.active_started_at = None
        return omni

    monkeypatch.setattr(router, "_stop_active_worker", stop_active)
    monkeypatch.setattr(router, "_find_pid_on_port", lambda _port: None)
    monkeypatch.setattr(
        router,
        "_spawn",
        lambda cfg: ProcessRecord(202, cfg.executable, (str(cfg.executable),), 2000.0),
    )
    monkeypatch.setattr(
        router,
        "_wait_healthy",
        lambda cfg, *_args: (0.0, LiveProof((cfg.alias,), cfg.context_tokens), (0, 1)),
    )

    async def scenario() -> dict:
        async with router.inference_session():
            task = asyncio.create_task(router.swap_to("coder"))
            for _ in range(100):
                if router._coordinator.lifecycle_waiters:
                    break
                await asyncio.sleep(0)
            assert router._coordinator.lifecycle_waiters == 1
            assert events == []
            assert not task.done()
        return await task

    result = asyncio.run(scenario())
    assert events == ["stopped"]
    assert result["swapped"] is True
    assert router.active_name == "coder"


def test_vram_release_requires_every_configured_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_worker(tmp_path)
    cfg = _config(tmp_path)
    router = _router(tmp_path, cfg)
    snapshots = iter(
        [
            {0: (16000, 16311), 1: (7000, 8151)},
            {0: (16000, 16311), 1: (7000, 8151)},
            {0: (16000, 16311), 1: (7600, 8151)},
        ]
    )
    calls: list[dict[int, tuple[int, int]]] = []

    def snapshot() -> dict[int, tuple[int, int]]:
        value = next(snapshots)
        calls.append(value)
        return value

    monkeypatch.setattr(router, "_gpu_memory_mib", snapshot)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    router._wait_vram_free(cfg)
    assert len(calls) == 3
    assert calls[1][0][0] >= 15000  # GPU0 was free, but GPU1 still blocked release.
    assert calls[1][1][0] < 7500


def test_cross_process_lifecycle_lock_rejects_concurrent_owner(tmp_path: Path) -> None:
    path = tmp_path / "cross-process.lock"
    first = _CrossProcessFileLock(path)
    second = _CrossProcessFileLock(path)
    first.acquire(0.1)
    try:
        with pytest.raises(WorkerSwapError, match="Another X Omni process"):
            second.acquire(0.1)
    finally:
        second.release()
        first.release()
