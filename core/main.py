"""
X Omni -- entrypoint.

    cd "X:\\X Omni"
    python -m core.main

Starts the default model worker (Omni), then serves the API and the
built UI on 127.0.0.1:8100. Remote reach is Tailscale's job -- Core
never binds beyond loopback.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import auth as auth_api
from .api import chat as chat_api
from .api import routes as core_routes
from .config import Settings
from .models.client import ModelClient
from .models.router import ModelRouter, WorkerSwapError
from .services import adas_si as adas_si_svc
from .services import calibration_iq as ciq_svc
from .services import camera as camera_svc
from .services import calendar as calendar_svc
from .services import exterior_camera as exterior_camera_svc
from .services import image_generation as image_svc
from .services import research as research_svc
from .services import video_generation as video_svc
from .services import website as website_svc
from .services import weather as weather_svc
from .state.db import Store
from .tools.builtin import system as builtin
from .tools.registry import Registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-18s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xomni")


def build_app(settings: Settings) -> FastAPI:
    settings.audio_tmp.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path)
    router = ModelRouter.from_config(
        settings.workers_config,
        vram_free_threshold_mib=settings.vram_free_threshold_mib,
        gpu_index=settings.gpu_index,
    )
    client = ModelClient(
        router,
        temperature=settings.temperature,
        max_tokens=settings.max_response_tokens,
    )
    registry = Registry(settings.tools_config, store=store)
    exterior_camera = exterior_camera_svc.ExteriorCameraService(settings.root)
    image_config = None
    image_generation = None
    try:
        image_config = image_svc.ImageGenerationConfig.from_file(
            settings.root / "config" / "image_generation.json", settings.root
        )
        image_provider = image_svc.ComfyUIProvider(image_config)
        image_generation = image_svc.ImageGenerationService(router, image_provider)
    except (OSError, ValueError, image_svc.ImageGenerationError) as exc:
        log.warning("Image generation is not configured: %s", type(exc).__name__)
    video_config = None
    video_generation = None
    try:
        video_config = video_svc.VideoGenerationConfig.from_file(
            settings.root / "config" / "video_generation.json", settings.root
        )
        video_generation = video_svc.VideoGenerationService(
            video_config,
            router=router,
            comfy_provider=image_provider if image_generation is not None else None,
        )
    except (OSError, ValueError, video_svc.VideoGenerationError) as exc:
        log.warning("Video animation is not configured: %s", type(exc).__name__)

    # --- wire tool handlers ---
    registry.register("read_file", builtin.make_read_file(registry))
    registry.register("list_directory", builtin.make_list_directory(registry))
    registry.register("search_files", builtin.make_search_files(registry))
    registry.register("write_file", builtin.make_write_file(registry))
    registry.register("system_status", builtin.make_system_status(router))
    registry.register(
        "assistant_capabilities_read",
        builtin.make_assistant_capabilities(router, registry),
    )
    registry.register("web_research_current", research_svc.search_current)
    registry.register(
        "website_preview_generate",
        website_svc.make_website_preview(client, store),
    )
    registry.register("camera_request", camera_svc.make_camera_request())
    registry.register(
        "exterior_camera_request",
        exterior_camera_svc.make_exterior_camera_request(exterior_camera),
    )
    if image_generation is not None:
        registry.register("image_generation_status", image_generation.status)
        registry.register("image_generate", image_generation.generate)
    if video_generation is not None:
        registry.register("video_generation_status", video_generation.status)
        registry.register("video_generate", video_generation.generate)
    registry.register("run_powershell", builtin.make_run_powershell())

    list_tasks, add_task, update_task_status = builtin.make_task_tools(store)
    registry.register("list_tasks", list_tasks)
    registry.register("add_task", add_task)
    registry.register("update_task_status", update_task_status)

    registry.register(
        "get_weather",
        lambda args: weather_svc.fetch(
            store, user_id=str(args.get("__xomni_user_id") or "local-dev")
        ),
    )

    async def get_calendar(args: dict) -> dict:
        try:
            return await calendar_svc.upcoming(settings, store, int(args.get("days") or 7))
        except calendar_svc.CalendarUnavailable as exc:
            return {"ok": False, "connected": False, "message": str(exc), "events": []}

    async def create_calendar_event(args: dict) -> dict:
        return await calendar_svc.create_event(settings, store, args)

    registry.register("get_calendar", get_calendar)
    registry.register("create_calendar_event", create_calendar_event)

    # --- field tools: ADAS SI + Calibration IQ ---
    # Both degrade to an honest "unavailable" rather than failing to start,
    # so Core still boots when the library or the service is offline.
    adas = adas_si_svc.AdasSI(
        settings.adas_si_root,
        settings.root / "data" / "capabilities" / "adas_si" / "index.sqlite",
    )
    if not adas.available():
        log.warning("ADAS SI library not found at %s -- tools will report unavailable.",
                    settings.adas_si_root)

    registry.register("adas_si_search", lambda a: adas.search(a))
    registry.register("adas_si_open", lambda a: adas.open_document(a))
    registry.register("adas_si_inventory", lambda a: adas.inventory_read(a))
    registry.register("adas_si_records", lambda a: adas.record_list(a))
    registry.register("adas_si_file_write", lambda a: adas.file_write(a))
    registry.register("adas_si_record_write", lambda a: adas.record_write(a))
    registry.register("adas_si_record_modify", lambda a: adas.record_modify(a))

    async def calibration_iq_status(_args: dict) -> dict:
        return await ciq_svc.status(settings)

    async def calibration_iq_summary(args: dict) -> dict:
        return await ciq_svc.summarize_repair_orders(settings, args)

    async def calibration_iq_read(args: dict) -> dict:
        return await ciq_svc.read_repair_orders(settings, args)

    async def calibration_iq_ro(args: dict) -> dict:
        return await ciq_svc.get_repair_order(settings, args)

    async def calibration_iq_update(args: dict) -> dict:
        return await ciq_svc.mutate(settings, args)

    registry.register("calibration_iq_status", calibration_iq_status)
    registry.register("calibration_iq_summary", calibration_iq_summary)
    registry.register("calibration_iq_read", calibration_iq_read)
    registry.register("calibration_iq_ro", calibration_iq_ro)
    registry.register("calibration_iq_update", calibration_iq_update)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("Starting default worker '%s' (cold start takes ~15-20s)...",
                 router.default_worker)
        try:
            result = await router.start_default(
                pre_start=(
                    image_generation.reconcile_startup
                    if image_generation is not None
                    else None
                )
            )
            cfg = router.configs[router.active_name]
            store.upsert_worker_state(router.active_name, cfg.port, router.active_pid, "active")
            store.audit("worker_started", result)
            log.info("Worker '%s' ready. %s", router.active_name,
                     "(adopted existing process)" if result.get("adopted") else
                     f"(started in {result.get('startup_s')}s)")
        except WorkerSwapError as exc:
            # Serve anyway -- the UI can show the failure and let Otis retry,
            # which beats an opaque crash at startup.
            log.error("Could not start default worker: %s", exc)
            store.audit("worker_start_failed", {"error": str(exc)})
        try:
            yield
        finally:
            log.info("Shutting down; stopping camera and model workers...")
            try:
                await exterior_camera.shutdown()
            finally:
                try:
                    await router.shutdown()
                finally:
                    store.close()

    app = FastAPI(
        title="X Omni",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.router = router
    app.state.client = client
    app.state.registry = registry
    app.state.exterior_camera = exterior_camera
    app.state.image_generation = image_generation
    app.state.image_generation_config = image_config
    app.state.video_generation = video_generation
    app.state.video_generation_config = video_config

    # Vite dev server runs on 5173 during development. Production serves the
    # built UI from this same origin, where CORS is irrelevant.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(self), geolocation=(), microphone=(self), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.rainviewer.com "
            "https://basemaps.cartocdn.com; "
            "connect-src 'self' ws: wss: https://api.rainviewer.com "
            "https://*.rainviewer.com; "
            "media-src 'self' blob:; worker-src 'self' blob:; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/healthz")):
            response.headers["Cache-Control"] = "no-store"
        return response

    require_session = auth_api.make_require_session(settings, store)
    app.include_router(auth_api.create_router(settings, store))
    app.include_router(
        core_routes.create_router(
            settings, store, router, registry, require_session,
            image_config=image_config,
            video_config=video_config,
            exterior_camera=exterior_camera,
            adas=adas,
        )
    )
    app.include_router(chat_api.create_router(settings, store, router, client, registry))

    @app.get("/healthz")
    async def healthz():
        model_health = await router.health()
        ready = bool(model_health.get("ready")) and not router.swapping
        payload = {
            "ok": ready,
            "core": "running",
            "worker": router.active_name,
            "swapping": router.swapping,
            "model": model_health,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    # --- static UI (served only if it's been built) ---
    dist = settings.root / "ui" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str):
            # Never let the SPA fallback swallow an unmatched API path --
            # that turns a 404 into a confusing page of HTML.
            if full_path.startswith(("api/", "ws/")):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/")
        async def no_ui():
            return JSONResponse({
                "detail": "UI is not built yet.",
                "fix": "cd ui && npm install && npm run build",
                "api": "/api/status",
            })

    return app


def main() -> None:
    settings = Settings.load()

    if not settings.auth_enabled:
        log.warning("=" * 68)
        log.warning("AUTH IS DISABLED (XOMNI_AUTH_ENABLED=0).")
        log.warning("Fine for local development on Omega.")
        log.warning("Do NOT leave this off while Tailscale serve is running --")
        log.warning("proxied remote traffic arrives over loopback and would be")
        log.warning("indistinguishable from you sitting at the machine.")
        log.warning("=" * 68)
    elif not settings.google_configured:
        log.warning("Auth is enabled but Google OAuth is not configured. "
                    "Set XOMNI_GOOGLE_CLIENT_ID / XOMNI_GOOGLE_CLIENT_SECRET "
                    "in config/.env.local, or set XOMNI_AUTH_ENABLED=0 for local dev.")

    app = build_app(settings)
    log.info("X Omni Core -> http://127.0.0.1:%d", settings.port)
    if settings.public_origin:
        log.info("Remote (Tailscale) -> %s", settings.public_origin)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        server_header=False,
    )


if __name__ == "__main__":
    sys.exit(main())
