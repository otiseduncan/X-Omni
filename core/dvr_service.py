"""X DVR -- standalone operator GUI entrypoint.

    cd "X:\\X Omni"
    python -m core.dvr_service

This process owns nothing but the browser-facing DVR GUI/API. Continuous
recording, the RTSP connection to the camera, live delivery, and recorded
playback all live in MediaMTX (X:\\MediaMTX), an independently-managed
process started by scripts/launch-mediamtx.ps1 -- not by this service, not
by X Omni Core. Starting, stopping, or restarting this GUI process never
touches recording, and closing it in a browser never does either.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from .api import auth as auth_api
from .config import Settings
from .services import mediamtx_dvr as mediamtx_dvr_svc
from .services import mediamtx_dvr_routes
from .services.exterior_camera import ExteriorCameraService
from .services.mediamtx_client import MediaMTXClient, PATH_MAIN
from .state.db import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-18s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("xomni.dvr")


def build_app(settings: Settings) -> FastAPI:
    store = Store(settings.db_path)
    exterior_camera = ExteriorCameraService(settings.root)
    client = MediaMTXClient(
        control_base_url=settings.mediamtx_control_base_url,
        playback_base_url=settings.mediamtx_playback_base_url,
        hls_base_url=settings.mediamtx_hls_base_url,
        webrtc_base_url=settings.mediamtx_webrtc_base_url,
        rtsp_base_url=settings.mediamtx_rtsp_base_url,
    )
    dvr = mediamtx_dvr_svc.MediaMTXDVR(
        client,
        path=PATH_MAIN,
        ffmpeg_path=exterior_camera.ffmpeg_path,
        recordings_root=settings.mediamtx_recordings_root,
        clips_dir=settings.mediamtx_clips_root / "_cache",
        saved_clips_dir=settings.mediamtx_clips_root / "saved",
    )
    dvr_auth_settings = dataclasses.replace(settings, port=settings.dvr_port)
    require_session = auth_api.make_require_session(dvr_auth_settings, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info(
            "X DVR GUI serving MediaMTX-backed playback -- recording is owned "
            "independently by MediaMTX (%s), not this process.",
            settings.mediamtx_control_base_url,
        )
        try:
            yield
        finally:
            log.info("Shutting down X DVR GUI...")
            try:
                await exterior_camera.shutdown()
            finally:
                store.close()

    app = FastAPI(
        title="X DVR",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.mediamtx_client = client
    app.state.mediamtx_dvr = dvr

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
            f"connect-src 'self' {settings.mediamtx_webrtc_base_url} {settings.mediamtx_hls_base_url}; "
            f"media-src 'self' blob: {settings.mediamtx_hls_base_url}; "
            "worker-src 'self' blob:; form-action 'self'"
        )
        if request.url.path.startswith("/dvr/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(
        mediamtx_dvr_routes.create_router(
            require_session=require_session,
            client=client,
            dvr=dvr,
            store=store,
            ui_root=Path(settings.root) / "ui" / "dvr",
            camera_snapshot_dir=Path(settings.camera_snapshot_dir),
        )
    )

    @app.get("/healthz")
    async def healthz():
        status = await dvr.status()
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
