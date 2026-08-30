"""FastAPI router for the standalone DVR GUI, backed entirely by MediaMTX.

Owner-auth-gated, loopback-only. There is no FFmpeg here and no per-segment
serving: live view is a WHEP/HLS URL the browser negotiates directly against
MediaMTX, and historical playback plus clip export both come from
MediaMTX's own Playback API (server-side stitched, zero X Omni transcoding).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from . import mediamtx_dvr as mediamtx_dvr_svc
from .mediamtx_client import MediaMTXClient, MediaMTXError, PATH_LIVE

log = logging.getLogger("xomni.dvr_routes")

_SAFE_CAMERA_SNAPSHOT_FILENAME_RE = re.compile(r"^\d{9,11}-(?:interval|motion)(?:-\d+)?\.jpg$")


def create_router(
    *,
    require_session,
    client: MediaMTXClient,
    dvr: "mediamtx_dvr_svc.MediaMTXDVR",
    store,
    ui_root: Path,
    camera_snapshot_dir: Path,
) -> APIRouter:
    router = APIRouter(prefix="/dvr", tags=["dvr"])

    async def require_owner(session: dict = Depends(require_session)) -> dict:
        if session.get("role") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    def _parse_range(since_raw: str, until_raw: str) -> tuple[datetime, datetime]:
        since_dt = mediamtx_dvr_svc._parse_iso(since_raw)
        until_dt = mediamtx_dvr_svc._parse_iso(until_raw)
        if since_dt is None or until_dt is None or until_dt <= since_dt:
            raise HTTPException(400, "since/until must be valid, ordered timestamps.")
        return since_dt, until_dt

    @router.get("")
    @router.get("/")
    async def index(_session: dict = Depends(require_owner)):
        path = ui_root / "index.html"
        if not path.is_file():
            raise HTTPException(404, "DVR UI is not installed.")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @router.get("/app.js")
    async def app_js(_session: dict = Depends(require_owner)):
        path = ui_root / "app.js"
        if not path.is_file():
            raise HTTPException(404, "DVR UI is not installed.")
        return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @router.get("/style.css")
    async def style_css(_session: dict = Depends(require_owner)):
        path = ui_root / "style.css"
        if not path.is_file():
            raise HTTPException(404, "DVR UI is not installed.")
        return FileResponse(path, media_type="text/css", headers={"Cache-Control": "no-store"})

    @router.get("/api/status")
    async def status(_session: dict = Depends(require_owner)) -> dict[str, Any]:
        dvr_status = await dvr.status()
        try:
            live_info = await client.path_status(PATH_LIVE)
        except MediaMTXError:
            live_info = None
        last_motion_at = None
        try:
            last_event = store.get_last_camera_event(trigger="motion")
            last_motion_at = last_event.get("captured_at") if last_event else None
        except Exception:
            log.info("could not read last motion event for DVR status", exc_info=True)
        return {
            "ok": bool(dvr_status.get("ok")),
            "recording": bool(dvr_status.get("recording")),
            "drive": dvr_status.get("drive"),
            "last_error": dvr_status.get("last_error"),
            "camera_ready": bool(live_info and live_info.get("ready")),
            "last_motion_at": last_motion_at,
            "whep_url": client.whep_url(PATH_LIVE),
            "hls_url": client.hls_playlist_url(PATH_LIVE),
        }

    @router.get("/api/recordings")
    async def recordings(
        since: str = Query(..., min_length=10, max_length=40),
        until: str = Query(..., min_length=10, max_length=40),
        _session: dict = Depends(require_owner),
    ) -> dict[str, Any]:
        since_dt, until_dt = _parse_range(since, until)
        rows = await dvr.list_segments(
            since=mediamtx_dvr_svc._iso(since_dt),
            until=mediamtx_dvr_svc._iso(until_dt),
            limit=500,
        )
        return {"items": rows, "count": len(rows)}

    @router.get("/api/events")
    async def events(
        since: str = Query(..., min_length=10, max_length=40),
        until: str = Query(..., min_length=10, max_length=40),
        _session: dict = Depends(require_owner),
    ) -> dict[str, Any]:
        since_dt, until_dt = _parse_range(since, until)
        try:
            rows = store.list_camera_events(
                since=since_dt.strftime("%Y-%m-%d %H:%M:%S"),
                until=until_dt.strftime("%Y-%m-%d %H:%M:%S"),
                limit=500,
            )
        except Exception:
            log.warning("could not list camera events for DVR UI", exc_info=True)
            rows = []
        motion_rows = []
        for row in rows:
            if row.get("trigger") != "motion":
                continue
            row = dict(row)
            filename = row.get("snapshot_filename")
            if filename:
                row["snapshot_url"] = f"/dvr/api/camera-snapshots/{filename}"
            motion_rows.append(row)
        return {"items": motion_rows, "count": len(motion_rows)}

    @router.get("/api/camera-snapshots/{filename}")
    async def camera_snapshot_image(filename: str, _session: dict = Depends(require_owner)):
        if not _SAFE_CAMERA_SNAPSHOT_FILENAME_RE.match(filename):
            raise HTTPException(404, "Camera snapshot not found.")
        if not store.camera_snapshot_is_tracked(filename):
            raise HTTPException(404, "Camera snapshot not found.")
        path = camera_snapshot_dir / filename
        if not path.is_file():
            raise HTTPException(404, "Camera snapshot not found.")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Content-Disposition": f'inline; filename="{filename}"', "Cache-Control": "private, max-age=604800"},
        )

    @router.get("/api/clip")
    async def clip(
        since: str = Query(..., min_length=10, max_length=40),
        until: str = Query(..., min_length=10, max_length=40),
        _session: dict = Depends(require_owner),
    ):
        since_dt, until_dt = _parse_range(since, until)
        cache_name = f"range-{int(since_dt.timestamp())}-{int(until_dt.timestamp())}"
        try:
            path = await dvr.range_clip(since_dt, until_dt, cache_name=cache_name)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except mediamtx_dvr_svc.PlaybackPreparationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @router.get("/api/events/{burst_id}/clip")
    async def event_clip(burst_id: int, _session: dict = Depends(require_owner)):
        try:
            path = await dvr.event_clip(store, burst_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except mediamtx_dvr_svc.PlaybackPreparationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @router.post("/api/clips/export")
    async def export_clip(
        payload: dict = Body(...),
        _session: dict = Depends(require_owner),
    ) -> dict[str, Any]:
        since_dt, until_dt = _parse_range(str(payload.get("since") or ""), str(payload.get("until") or ""))
        title = str(payload.get("title") or "").strip()
        name = f"export-{int(since_dt.timestamp())}-{int(until_dt.timestamp())}"
        if title:
            name = f"{name}-{title}"
        try:
            path = await dvr.export_clip(since_dt, until_dt, name=name)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except mediamtx_dvr_svc.PlaybackPreparationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "filename": path.name, "clip_url": f"/dvr/api/clips-saved/{path.name}"}

    @router.get("/api/clips-saved")
    async def clips_saved(_session: dict = Depends(require_owner)) -> dict[str, Any]:
        items = dvr.list_saved_clips()
        return {"items": items, "count": len(items)}

    @router.get("/api/clips-saved/{filename}")
    async def clip_saved_video(filename: str, _session: dict = Depends(require_owner)):
        path = dvr.saved_clip_path(filename)
        if path is None:
            raise HTTPException(404, "Clip not found.")
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @router.delete("/api/clips-saved/{filename}")
    async def delete_clip_saved(filename: str, _session: dict = Depends(require_owner)) -> dict[str, Any]:
        if not dvr.delete_saved_clip(filename):
            raise HTTPException(404, "Clip not found.")
        return {"ok": True}

    @router.get("/api/healthz")
    async def healthz() -> dict[str, Any]:
        return await dvr.status()

    return router
