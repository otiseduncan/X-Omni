"""X DVR -- independent continuous-recording service entrypoint.

    cd "X:\\X Omni"
    python -m core.dvr_service

This process owns the exterior camera's continuous RTSP recording, ONVIF
motion-subscription health used by its own retention/status reporting, the
E:\\XOmni-DVR archive, and the standalone DVR operator GUI/API. It is
deliberately independent of X Omni Core: starting, stopping, or restarting
Core (or its model worker) never starts, stops, or restarts this process,
and closing the DVR GUI in a browser never touches recording either.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .api import auth as auth_api
from .config import Settings
from .services import camera_dvr as camera_dvr_svc
from .services import camera_security as camera_security_svc
from .services import dvr_continuous_playback as continuous_playback_svc
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
    dvr_auth_settings = dataclasses.replace(settings, port=settings.dvr_port)
    require_session = auth_api.make_require_session(dvr_auth_settings, store)

    async def _track_motion_health() -> None:
        while not camera_dvr._stopped:
            try:
                async for _active in camera_dvr.motion_states():
                    if camera_dvr._stopped:
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("X DVR motion-health consumer failed; retrying", exc_info=True)
            if camera_dvr._stopped:
                return
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("X DVR recording E:\\XOmni-DVR independent of X Omni Core.")
        recorder_task = asyncio.create_task(camera_dvr.run_forever())
        motion_health_task = asyncio.create_task(_track_motion_health())
        try:
            yield
        finally:
            log.info("Shutting down X DVR recorder...")
            camera_dvr.stop()
            recorder_task.cancel()
            motion_health_task.cancel()
            await asyncio.gather(recorder_task, motion_health_task, return_exceptions=True)
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
        continuous_playback_svc.create_router(require_session, camera_dvr)
    )

    ui_root = Path(settings.root) / "ui" / "dvr"

    @app.get("/dvr/continuous-playback.js")
    async def dvr_continuous_js():
        path = ui_root / "continuous-playback.js"
        if not path.is_file():
            return JSONResponse(
                {"detail": "DVR playback adapter is not installed."},
                status_code=404,
            )
        return FileResponse(
            path,
            media_type="text/javascript",
            headers={"Cache-Control": "no-store"},
        )

    async def _require_owner(session: dict = Depends(require_session)) -> dict:
        if session.get("role") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    def _owner_session_id(session: dict) -> str:
        token_hash = str(session.get("token_hash") or "").strip()
        if token_hash:
            return f"session:{token_hash}"
        return f"local:{session.get('google_sub') or 'local-dev'}"

    @app.delete("/dvr/api/live/reset")
    async def reset_owner_live_session(session: dict = Depends(_require_owner)):
        """Clear an orphaned standalone Live View session for this Owner.

        A browser reload can lose the opaque live session id before its
        keepalive DELETE reaches the service. Without this bounded reset the
        invisible pending session blocks Start Live until its five-minute TTL.
        It never touches the continuous recorder or historical playback.
        """
        owner_id = _owner_session_id(session)
        async with exterior_camera._lock:
            await exterior_camera._expire_locked()
            current = exterior_camera._session
            if current is None:
                return {"ok": True, "stopped": False}
            if not secrets.compare_digest(current.owner_id, owner_id):
                raise HTTPException(403, "The active camera session belongs to another Owner.")
            live_session_id = current.session_id
        try:
            await exterior_camera.delete_session(
                session_id=live_session_id,
                owner_id=owner_id,
            )
        except exterior_camera_svc.ExteriorCameraSessionNotFound:
            return {"ok": True, "stopped": False}
        return {"ok": True, "stopped": True}

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
