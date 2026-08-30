"""X DVR -- independent continuous-recording service entrypoint.

    cd "X:\\X Omni"
    python -m core.dvr_service

This process owns the exterior camera's continuous RTSP recording, ONVIF
motion-subscription health used by its own retention/status reporting, the
E:\\XOmni-DVR archive, and the standalone DVR operator GUI/API. It is
deliberately independent of X Omni Core: starting, stopping, or restarting
Core (or its model worker) never starts, stops, or restarts this process,
and closing the DVR GUI in a browser never touches recording either. Core
talks to this service as a client (see
`core.services.camera_dvr_client.DVRServiceClient`); it does not run any of
this itself.

Binds loopback only, exactly like Core -- remote reach, if wanted, is
Tailscale's job (see scripts/tailscale-serve.ps1), not this process binding
wider than 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .api import auth as auth_api
from .config import Settings
from .services import camera_dvr as camera_dvr_svc
from .services import camera_security as camera_security_svc
from .services import exterior_camera as exterior_camera_svc
from .state.db import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-18s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xomni.dvr")


def build_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path)
    exterior_camera = exterior_camera_svc.ExteriorCameraService(settings.root)
    camera_dvr = camera_security_svc.XiongmaiDVR(exterior_camera)
    require_session = auth_api.make_require_session(settings, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("X DVR recording E:\\XOmni-DVR independent of X Omni Core.")
        recorder_task = asyncio.create_task(camera_dvr.run_forever())
        try:
            yield
        finally:
            log.info("Shutting down X DVR recorder...")
            camera_dvr.stop()
            recorder_task.cancel()
            await asyncio.gather(recorder_task, return_exceptions=True)
            await camera_dvr.shutdown()
            try:
                await exterior_camera.shutdown()
            finally:
                store.close()

    app = FastAPI(
        title="X DVR",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.camera_dvr = camera_dvr

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self'; media-src 'self' blob:; "
            "worker-src 'self' blob:; form-action 'self'"
        )
        if request.url.path.startswith(("/dvr/api/",)):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(
        camera_dvr_svc.create_router(
            settings,
            store,
            require_session,
            camera_dvr,
            internal_token=settings.internal_dvr_token,
            extra_allowed_origins=(settings.dvr_local_origin,),
        )
    )

    @app.get("/healthz")
    async def healthz():
        status = await camera_dvr.status()
        return JSONResponse({"ok": True, "service": "dvr", "status": status})

    @app.get("/")
    async def root():
        return RedirectResponse(url="/dvr")

    return app


def main() -> None:
    settings = Settings.load()
    app = build_app(settings)
    log.info("X DVR -> http://127.0.0.1:%d/dvr", settings.dvr_port)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=settings.dvr_port,
        log_level="info",
        server_header=False,
    )


if __name__ == "__main__":
    sys.exit(main())
