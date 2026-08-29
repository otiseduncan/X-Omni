"""
X Omni -- HTTP routes (everything except the chat WebSocket).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, SecretStr, ValidationError

from ..models.router import WorkerSwapError
from ..services import camera as camera_svc
from ..services import calendar as calendar_svc
from ..services import calibration_iq as calibration_iq_svc
from ..services import exterior_camera as exterior_camera_svc
from ..services.image_generation import ImageGenerationError, generated_image_path
from ..services.video_generation import (
    VideoGenerationError,
    generated_video_path,
    verify_generated_video_file,
)
from ..services import weather as weather_svc

log = logging.getLogger("xomni.routes")

MAX_EXTERIOR_CAMERA_CONFIG_BYTES = 16 * 1024
_SAFE_CAMERA_SNAPSHOT_FILENAME_RE = re.compile(r"^\d{9,11}-(?:interval|motion)(?:-\d+)?\.jpg$")

_CALIBRATION_IQ_PROXY_STATUS = {
    "invalid_input": 400,
    "unauthorized": 401,
    "permission_denied": 403,
    "not_found": 404,
    "document_too_large": 413,
    "workspace_file_too_large": 413,
    "photo_too_large": 413,
    "invalid_response": 502,
    "temporary_service_failure": 503,
    "not_configured": 503,
}


def _calibration_iq_proxy_response(
    result: dict, *, resource_kind: str
) -> StreamingResponse:
    if (
        result.get("status") != "verified"
        or result.get("success") is not True
        or result.get("verified") is not True
    ):
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        code = str(error.get("code") or result.get("status") or "operation_failed")
        raise HTTPException(
            _CALIBRATION_IQ_PROXY_STATUS.get(code, 502),
            {"code": code, "message": str(error.get("message") or result.get("message"))},
        )
    data = result.get("content")
    expected_length = result.get("content_length")
    expected_sha256 = str(result.get("sha256") or "").strip().casefold()
    content_type = calibration_iq_svc._validated_operator_content_type(
        result.get("content_type"), resource_kind=resource_kind
    )
    if (
        not isinstance(data, bytes)
        or isinstance(expected_length, bool)
        or not isinstance(expected_length, int)
        or expected_length < 0
        or len(data) != expected_length
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or hashlib.sha256(data).hexdigest() != expected_sha256
        or content_type is None
    ):
        raise HTTPException(
            502,
            {
                "code": "invalid_response",
                "message": "Calibration IQ proxy data failed local integrity validation.",
            },
        )
    disposition = str(result.get("content_disposition") or "attachment")
    if "\r" in disposition or "\n" in disposition or len(disposition) > 1000:
        disposition = "attachment"
    return StreamingResponse(
        iter([data]),
        media_type=content_type,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(expected_length),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Content-SHA256": expected_sha256,
            "X-Content-Length-Verified": str(expected_length),
        },
    )


class SwapRequest(BaseModel):
    worker: str


class LocationRequest(BaseModel):
    name: Optional[str] = None
    zip: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class ApprovalDecision(BaseModel):
    approved: bool


class DirectToolRequest(BaseModel):
    conversation_id: int


class ExteriorCameraConfigureRequest(BaseModel):
    label: str
    host: str
    username: str
    password: SecretStr


class ExteriorCameraSessionRequest(BaseModel):
    conversation_id: int


class PushSubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


def create_router(
    settings,
    store,
    router_,
    registry,
    require_session,
    *,
    image_config=None,
    video_config=None,
    exterior_camera=None,
    adas=None,
) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["core"], dependencies=[Depends(require_session)])

    def public_approval(record: dict, receipt: Optional[dict] = None) -> dict:
        """Expose action evidence without leaking session/principal bindings."""
        return registry.public_approval(record, receipt=receipt)

    def session_id(session: dict) -> str:
        token_hash = str(session.get("token_hash") or "").strip()
        if token_hash:
            return f"session:{token_hash}"
        return f"local:{session.get('google_sub') or 'local-dev'}"

    def user_id(session: dict) -> str:
        return str(session.get("user_id") or "local-dev")

    async def require_owner(session: dict = Depends(require_session)) -> dict:
        if session.get("role", "owner") != "owner":
            raise HTTPException(403, "Owner authorization is required.")
        return session

    def require_conversation(conversation_id: int, session: dict) -> None:
        if not hasattr(store, "conversation_exists"):
            return
        try:
            exists = store.conversation_exists(conversation_id, user_id=user_id(session))
        except TypeError:
            # Small route-unit-test stores predate scoped ownership. Production
            # Store always implements the keyword boundary above.
            exists = store.conversation_exists(conversation_id)
        if not exists:
            raise HTTPException(404, "Conversation does not exist.")

    def require_exact_origin(request: Request, message: str) -> None:
        origin = str(request.headers.get("origin") or "").strip().rstrip("/")
        allowed_origins = {
            str(getattr(settings, "local_origin", "") or "").rstrip("/"),
            str(getattr(settings, "public_origin", "") or "").rstrip("/"),
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        allowed_origins.discard("")
        if not origin or origin not in allowed_origins:
            raise HTTPException(403, message)

    def exterior_camera_http_error(exc: BaseException) -> HTTPException:
        if isinstance(exc, exterior_camera_svc.ExteriorCameraAuthError):
            return HTTPException(401, "Exterior camera credentials were rejected.")
        if isinstance(exc, exterior_camera_svc.ExteriorCameraSessionNotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, exterior_camera_svc.ExteriorCameraFrameUnavailable):
            return HTTPException(409, str(exc))
        if isinstance(
            exc,
            (
                exterior_camera_svc.ExteriorCameraNotConfigured,
                exterior_camera_svc.ExteriorCameraConflict,
            ),
        ):
            return HTTPException(409, str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(400, str(exc))
        return HTTPException(503, "Exterior camera is unavailable.")

    # ---------- system ----------

    @api.get("/status")
    async def status(session: dict = Depends(require_session)):
        health = await router_.health()
        weather_loc = weather_svc.get_location(store, user_id=user_id(session))
        calendar_status = (
            await calendar_svc.status(settings, store)
            if session.get("role", "owner") == "owner"
            else {"connected": False, "reason": "owner_only"}
        )
        return {
            "worker": router_.status(),
            "model_health": health,
            "calendar": calendar_status,
            "weather_location": weather_loc,
            "auth_enabled": settings.auth_enabled,
        }

    @api.get("/tools")
    async def list_tools(session: dict = Depends(require_session)):
        """The capability surface, exactly as policy defines it -- including
        the blocked entries, so the UI can show what X deliberately cannot
        do rather than quietly omitting it."""
        from ..tools.registry import TOOL_SCHEMAS
        items = []
        for name in sorted(set(TOOL_SCHEMAS) | set(registry.policy)):
            if hasattr(registry, "role_allows_tool") and not registry.role_allows_tool(
                str(session.get("role") or "owner"), name
            ):
                continue
            entry = registry.policy.get(name) or {}
            items.append({
                "name": name,
                "tier": registry.tier(name),
                "description": entry.get("description")
                or TOOL_SCHEMAS.get(name, {}).get("description", ""),
                "implemented": name in registry._handlers,  # noqa: SLF001
            })
        return {
            "tools": items,
            "roots": [str(r) for r in registry.roots]
            if session.get("role", "owner") == "owner"
            else [],
        }

    @api.post("/tools/{tool_name}/run")
    async def run_tool(
        tool_name: str,
        request: DirectToolRequest,
        session: dict = Depends(require_session),
    ):
        """Fire a no-argument read-only tool straight from the UI.

        Deliberately refuses anything above read_only: approval-gated
        actions must go through the chat approval card so there is always
        a conversational record of what was authorized and why.
        """
        if registry.tier(tool_name) != "read_only":
            raise HTTPException(
                403,
                "Only read-only tools can be run directly. Ask X to use this one "
                "so it goes through the approval flow.",
            )
        try:
            require_conversation(request.conversation_id, session)
            invoke_context = {"conversation_id": request.conversation_id}
            if hasattr(registry, "role_allows_tool"):
                invoke_context.update({
                    "user_id": user_id(session),
                    "role": str(session.get("role") or "owner"),
                })
            result = await registry.invoke(tool_name, {}, **invoke_context)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"{type(exc).__name__}: {exc}")
        card_type = {
            "get_weather": "weather",
            "get_calendar": "calendar",
            "list_tasks": "tasks",
            "system_status": "system_status",
            "assistant_capabilities_read": "capabilities",
            "image_generation_status": "image_generation_status",
            "video_generation_status": "video_generation_status",
            "exterior_camera_request": "exterior_camera_request",
        }.get(tool_name)
        artifact = {"type": card_type, "data": result} if card_type else None
        message_id = None
        if artifact:
            try:
                message_id = store.add_message(
                    request.conversation_id,
                    "assistant",
                    "",
                    worker_used=router_.active_name,
                    artifacts=[artifact],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not persist direct tool artifact: %s", type(exc).__name__)
                raise HTTPException(400, "Conversation does not exist or cannot accept artifacts.") from exc
        success = not (isinstance(result, dict) and result.get("ok") is False)
        return {
            "ok": success,
            "executed": True,
            "success": success,
            "tool": tool_name,
            "result": result,
            "artifact": artifact,
            "message_id": message_id,
        }

    @api.post("/worker/swap")
    async def swap(req: SwapRequest, _session: dict = Depends(require_owner)):
        try:
            result = await router_.swap_to(req.worker)
        except WorkerSwapError as exc:
            raise HTTPException(503, str(exc))
        store.upsert_worker_state(
            req.worker, router_.configs[req.worker].port, router_.active_pid, "active"
        )
        store.audit("worker_swap", result)
        return result

    # ---------- conversations ----------

    @api.get("/conversations")
    async def conversations(session: dict = Depends(require_session)):
        return store.list_conversations(user_id=user_id(session))

    @api.post("/conversations")
    async def new_conversation(session: dict = Depends(require_session)):
        return {"id": store.create_conversation(user_id=user_id(session))}

    @api.get("/conversations/{conversation_id}/messages")
    async def messages(conversation_id: int, session: dict = Depends(require_session)):
        require_conversation(conversation_id, session)
        return store.get_messages(conversation_id, user_id=user_id(session))

    @api.get("/generated-images/{filename}")
    async def generated_image(filename: str, session: dict = Depends(require_session)):
        """Serve only a verified content-addressed image to the Owner session."""
        if image_config is None:
            raise HTTPException(404, "Image generation is not configured.")
        try:
            path = generated_image_path(image_config, filename)
        except ImageGenerationError as exc:
            raise HTTPException(404, "Generated image not found.") from exc
        if not path.is_file():
            raise HTTPException(404, "Generated image not found.")
        if hasattr(store, "artifact_belongs_to_user") and not store.artifact_belongs_to_user(
            filename, user_id(session)
        ):
            raise HTTPException(404, "Generated image not found.")

        def verify() -> bool:
            size = path.stat().st_size
            if size <= 0 or size > image_config.max_output_bytes:
                return False
            digest = hashlib.sha256()
            first = b""
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    if not first:
                        first = chunk[:8]
                    digest.update(chunk)
            expected = filename.removesuffix(".png")
            return first == b"\x89PNG\r\n\x1a\n" and digest.hexdigest() == expected

        if not await asyncio.to_thread(verify):
            raise HTTPException(404, "Generated image failed integrity verification.")
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )

    @api.get("/camera-snapshots/{filename}")
    async def camera_snapshot_image(filename: str, _session: dict = Depends(require_owner)):
        """Serve only a snapshot this app actually wrote and logged, to the
        Owner only -- exterior-camera imagery is surveillance data, same
        access level as the live stream itself."""
        if not _SAFE_CAMERA_SNAPSHOT_FILENAME_RE.match(filename):
            raise HTTPException(404, "Camera snapshot not found.")
        if not store.camera_snapshot_is_tracked(filename):
            raise HTTPException(404, "Camera snapshot not found.")
        path = Path(settings.camera_snapshot_dir) / filename
        if not path.is_file():
            raise HTTPException(404, "Camera snapshot not found.")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "private, max-age=86400",
            },
        )

    @api.get("/generated-videos/{filename}")
    async def generated_video(
        filename: str, request: Request, session: dict = Depends(require_session),
    ):
        """Serve one verified content-addressed MP4 with single-range support."""
        if video_config is None:
            raise HTTPException(404, "Video animation is not configured.")
        try:
            path = generated_video_path(video_config, filename)
        except VideoGenerationError as exc:
            raise HTTPException(404, "Generated video not found.") from exc
        if not path.is_file():
            raise HTTPException(404, "Generated video not found.")
        if hasattr(store, "artifact_belongs_to_user") and not store.artifact_belongs_to_user(
            filename, user_id(session)
        ):
            raise HTTPException(404, "Generated video not found.")

        expected_digest = filename.removesuffix(".mp4")
        try:
            size, digest = await asyncio.to_thread(
                verify_generated_video_file,
                video_config,
                path,
                expected_digest,
            )
        except (OSError, VideoGenerationError) as exc:
            raise HTTPException(
                404, "Generated video failed integrity verification."
            ) from exc

        common_headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{filename}"',
            "ETag": f'"{digest}"',
        }
        range_header = str(request.headers.get("range") or "").strip()
        if not range_header:
            return FileResponse(
                path,
                media_type="video/mp4",
                headers=common_headers,
            )

        match = (
            re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", range_header)
            if len(range_header) <= 48
            else None
        )
        if match is None or (not match.group(1) and not match.group(2)):
            raise HTTPException(
                416,
                "Only one valid byte range is supported.",
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{size}",
                },
            )
        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start >= size or end < start:
                raise HTTPException(
                    416,
                    "Requested byte range is not satisfiable.",
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{size}",
                    },
                )
            end = min(end, size - 1)
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise HTTPException(
                    416,
                    "Requested byte range is not satisfiable.",
                    headers={
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes */{size}",
                    },
                )
            suffix_length = min(suffix_length, size)
            start = size - suffix_length
            end = size - 1

        length = end - start + 1

        def stream_range():
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            stream_range(),
            status_code=206,
            media_type="video/mp4",
            headers={
                **common_headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )

    # ---------- ADAS SI documents ----------

    @api.get("/adas-si/document")
    async def adas_si_document(
        path: str, download: bool = False, _session: dict = Depends(require_owner),
    ):
        """Stream a document out of the ADAS SI library for inline display.

        The service resolves and confines the path to the library root, so a
        crafted value cannot reach the rest of the disk. Served inline by
        default so the browser PDF viewer renders it inside the chat card;
        a #page= fragment on the URL jumps to the matching page.
        """
        if adas is None or not adas.available():
            raise HTTPException(404, "The ADAS SI library is not available.")
        try:
            resolved = adas.resolve_relative(path)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

        media = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".txt": "text/plain; charset=utf-8",
            ".json": "application/json",
        }.get(resolved.suffix.lower(), "application/octet-stream")

        disposition = "attachment" if download else "inline"
        safe_name = resolved.name.replace('"', "")
        return FileResponse(
            resolved,
            media_type=media,
            headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
        )

    @api.get("/calibration-iq/documents/{document_id}/download")
    async def calibration_iq_document_download(
        document_id: str,
        _session: dict = Depends(require_owner),
    ):
        """Authenticated same-origin proxy for Calibration IQ managed files.

        The browser never receives the internal service URL or bearer token,
        and Core buffers no more than the service adapter's fixed size limit.
        """
        result = await calibration_iq_svc.fetch_operator_document(settings, document_id)
        return _calibration_iq_proxy_response(result, resource_kind="document")

    @api.get("/calibration-iq/workspace-file")
    async def calibration_iq_workspace_file(
        repair_order_id: str,
        path: str,
        _session: dict = Depends(require_owner),
    ):
        """Authenticated proxy for one path-confined managed RO workspace file."""
        result = await calibration_iq_svc.fetch_operator_workspace_file(
            settings, repair_order_id, path
        )
        return _calibration_iq_proxy_response(result, resource_kind="workspace")

    @api.get("/calibration-iq/photos/{photo_id}/{variant}")
    async def calibration_iq_photo(
        photo_id: str,
        variant: str,
        _session: dict = Depends(require_owner),
    ):
        """Authenticated proxy for a verified Calibration IQ photo or thumbnail."""
        result = await calibration_iq_svc.fetch_operator_photo(
            settings, photo_id, variant
        )
        return _calibration_iq_proxy_response(result, resource_kind="photo")

    @api.get("/adas-si/page")
    async def adas_si_page(
        path: str, page: int = 1, width: int = 1100,
        _session: dict = Depends(require_owner),
    ):
        """Render one page of an ADAS SI document as a PNG.

        This is the inline display path. Mobile browsers will not render a
        PDF inside an iframe, and Otis works from a phone in the field, so
        pages are rasterised server-side and shown as images. It also makes
        scanned documents readable in chat -- a scan has no extractable text
        but renders fine.
        """
        if adas is None or not adas.available():
            raise HTTPException(404, "The ADAS SI library is not available.")
        try:
            resolved = adas.resolve_relative(path)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            png = await asyncio.to_thread(adas.render_page, resolved, page, width)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.warning("ADAS SI page render failed for %s p%s: %s", path, page, exc)
            raise HTTPException(502, f"Could not render that page: {type(exc).__name__}")
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    # ---------- weather ----------

    @api.get("/weather")
    async def weather(session: dict = Depends(require_session)):
        return weather_svc.fetch(store, user_id=user_id(session))

    @api.post("/weather/location")
    async def set_location(
        req: LocationRequest, session: dict = Depends(require_session),
    ):
        try:
            loc = weather_svc.save_location(
                store, req.model_dump(exclude_none=True), user_id=user_id(session)
            )
        except ValueError as exc:
            # Bad or unresolvable input -- the caller can fix this.
            raise HTTPException(400, str(exc))
        except httpx.HTTPError as exc:
            # Geocoding is a network call. Failing it with a bare 500 tells
            # the operator nothing; name the actual cause.
            log.warning("geocoding failed: %s", exc)
            raise HTTPException(
                503,
                "Could not reach the geocoding service to look that up. "
                "Check the network and try again, or give exact coordinates.",
            )
        return {
            "ok": True, "location": loc,
            "forecast": weather_svc.fetch(store, user_id=user_id(session)),
        }

    # ---------- calendar ----------

    @api.get("/calendar")
    async def calendar(days: int = 7, _session: dict = Depends(require_owner)):
        try:
            return await calendar_svc.upcoming(settings, store, days)
        except calendar_svc.CalendarUnavailable as exc:
            return {"ok": False, "connected": False, "message": str(exc), "events": []}

    # ---------- approvals ----------

    @api.get("/approvals/{approval_id}")
    async def approval(
        approval_id: str,
        session: dict = Depends(require_session),
    ):
        snapshot = store.approval_snapshot(approval_id)
        if not snapshot:
            raise HTTPException(404, "Unknown approval.")
        record = snapshot["approval"]
        if (
            record.get("session_id") != session_id(session)
            or record.get("user_id") != user_id(session)
        ):
            raise HTTPException(403, "Approval belongs to a different session or user.")
        return {
            "approval": public_approval(record, snapshot["receipt"]),
            "receipt": snapshot["receipt"],
        }

    @api.post("/approvals/{approval_id}")
    async def decide(
        approval_id: str,
        decision: ApprovalDecision,
        session: dict = Depends(require_session),
    ):
        record = store.get_approval(approval_id)
        if not record:
            raise HTTPException(404, "Unknown approval.")
        try:
            outcome = await registry.resolve_approval(
                approval_id,
                decision.approved,
                conversation_id=int(record["conversation_id"]),
                session_id=session_id(session),
                user_id=user_id(session),
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "approval": public_approval(outcome["approval"], outcome.get("receipt")),
            "receipt": outcome.get("receipt"),
            "replayed": outcome["replayed"],
        }

    # ---------- local exterior camera ----------

    @api.get("/cameras/exterior")
    async def exterior_camera_status(_session: dict = Depends(require_owner)):
        if exterior_camera is None:
            return {
                "ok": False,
                "configured": False,
                "status": "unavailable",
                "runtime_available": False,
                "streaming": False,
                "message": "Exterior camera connector is not available.",
            }
        return exterior_camera.status()

    @api.post("/cameras/exterior/configure")
    async def configure_exterior_camera(
        request: Request,
        _session: dict = Depends(require_owner),
    ):
        require_exact_origin(
            request, "Exterior camera configuration requires the exact X Omni origin."
        )
        if exterior_camera is None:
            raise HTTPException(503, "Exterior camera connector is not available.")
        content_type = str(request.headers.get("content-type") or "").partition(";")[0]
        if content_type.strip().casefold() != "application/json":
            raise HTTPException(415, "Exterior camera configuration requires JSON.")
        if str(request.headers.get("content-encoding") or "").strip().casefold() not in {
            "",
            "identity",
        }:
            raise HTTPException(415, "Encoded camera configuration is not accepted.")
        content_length = str(request.headers.get("content-length") or "").strip()
        if content_length:
            try:
                if int(content_length) < 0:
                    raise ValueError
                if int(content_length) > MAX_EXTERIOR_CAMERA_CONFIG_BYTES:
                    raise HTTPException(413, "Exterior camera configuration is too large.")
            except ValueError:
                raise HTTPException(400, "Invalid configuration Content-Length.") from None
        body = bytearray()
        async for chunk in request.stream():
            if not chunk:
                continue
            if len(body) + len(chunk) > MAX_EXTERIOR_CAMERA_CONFIG_BYTES:
                body.clear()
                raise HTTPException(413, "Exterior camera configuration is too large.")
            body.extend(chunk)
        try:
            configuration = ExteriorCameraConfigureRequest.model_validate_json(bytes(body))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Exterior camera configuration is invalid.") from None
        finally:
            body.clear()
        try:
            result = await exterior_camera.configure(
                label=configuration.label,
                host=configuration.host,
                username=configuration.username,
                password=configuration.password.get_secret_value(),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        except ValueError as exc:
            raise exterior_camera_http_error(exc) from exc
        store.audit(
            "exterior_camera_configured",
            {
                "label": result.get("label"),
                "host": result.get("host"),
                "username": result.get("username"),
                "verified": result.get("verified") is True,
            },
        )
        return result

    @api.post("/cameras/exterior/sessions")
    async def create_exterior_camera_session(
        request: Request,
        body: ExteriorCameraSessionRequest,
        session: dict = Depends(require_owner),
    ):
        require_exact_origin(
            request, "Exterior camera sessions require the exact X Omni origin."
        )
        if exterior_camera is None:
            raise HTTPException(503, "Exterior camera connector is not available.")
        if body.conversation_id <= 0:
            raise HTTPException(400, "conversation_id must be a positive integer.")
        require_conversation(body.conversation_id, session)
        try:
            result = await exterior_camera.create_session(
                conversation_id=body.conversation_id,
                owner_id=session_id(session),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        except ValueError as exc:
            raise exterior_camera_http_error(exc) from exc
        store.audit(
            "exterior_camera_session_created",
            {
                "conversation_id": body.conversation_id,
                "label": result.get("label"),
                "expires_at": result.get("expires_at"),
                "streaming": False,
            },
        )
        return result

    @api.get("/cameras/exterior/sessions/{camera_session_id}/stream.mjpg")
    async def exterior_camera_stream(
        camera_session_id: str,
        session: dict = Depends(require_owner),
    ):
        if exterior_camera is None:
            raise HTTPException(503, "Exterior camera connector is not available.")
        try:
            iterator = await exterior_camera.stream(
                session_id=camera_session_id,
                owner_id=session_id(session),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        return StreamingResponse(
            iterator,
            media_type="multipart/x-mixed-replace; boundary=xomni",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @api.delete("/cameras/exterior/sessions/{camera_session_id}")
    async def delete_exterior_camera_session(
        camera_session_id: str,
        request: Request,
        session: dict = Depends(require_owner),
    ):
        require_exact_origin(
            request, "Stopping an exterior camera session requires the exact X Omni origin."
        )
        if exterior_camera is None:
            raise HTTPException(503, "Exterior camera connector is not available.")
        try:
            result = await exterior_camera.delete_session(
                session_id=camera_session_id,
                owner_id=session_id(session),
            )
        except exterior_camera_svc.ExteriorCameraError as exc:
            raise exterior_camera_http_error(exc) from exc
        store.audit("exterior_camera_session_stopped", {"streaming": False})
        return result

    # ---------- push notifications ----------

    @api.get("/push/public-key")
    async def push_public_key(_session: dict = Depends(require_session)):
        return {"key": settings.vapid_public_key}

    @api.post("/push/subscribe")
    async def push_subscribe(
        body: PushSubscribeRequest, session: dict = Depends(require_session),
    ):
        endpoint = body.endpoint.strip()
        p256dh = body.p256dh.strip()
        auth = body.auth.strip()
        if not (endpoint.startswith("https://") and p256dh and auth):
            raise HTTPException(400, "A valid push subscription is required.")
        if len(endpoint) > 2048 or len(p256dh) > 512 or len(auth) > 512:
            raise HTTPException(400, "Push subscription fields exceed the allowed length.")
        store.add_push_subscription(
            user_id=user_id(session), endpoint=endpoint, p256dh_key=p256dh, auth_key=auth,
        )
        return {"ok": True}

    @api.post("/push/unsubscribe")
    async def push_unsubscribe(
        body: PushUnsubscribeRequest, _session: dict = Depends(require_session),
    ):
        store.remove_push_subscription(body.endpoint.strip())
        return {"ok": True}

    # ---------- camera still -> native Omni vision ----------

    @api.post("/vision/analyze")
    async def analyze_camera_frame(
        request: Request,
        auth_session: dict = Depends(require_session),
    ):
        """Analyze one explicitly submitted still without persisting bytes.

        Browser-camera stills arrive as a bounded raw body. Exterior-camera
        analysis accepts no uploaded image: it resolves the latest JPEG that
        Core actually proxied from the exact active Owner-bound MJPEG session.
        """
        require_exact_origin(request, "Camera frames must come from the X Omni origin.")

        requested_camera_source = str(
            request.headers.get("x-xomni-camera-source-id") or ""
        ).strip()
        if requested_camera_source not in {"", "exterior"}:
            raise HTTPException(400, "Unknown camera source identifier.")
        is_exterior = requested_camera_source == "exterior"

        raw_conversation_id = str(
            request.headers.get("x-xomni-conversation-id") or ""
        ).strip()
        try:
            conversation_id = int(raw_conversation_id)
            if conversation_id <= 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(
                400, "X-XOmni-Conversation-ID must be a positive integer."
            ) from exc
        require_conversation(conversation_id, auth_session)
        if is_exterior and auth_session.get("role", "owner") != "owner":
            raise HTTPException(403, "Exterior camera access requires Owner authorization.")
        try:
            bounded_prompt = camera_svc.decode_camera_prompt_header(
                request.headers.get("x-xomni-camera-prompt-b64")
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        content_encoding = str(request.headers.get("content-encoding") or "").casefold()
        if content_encoding not in {"", "identity"}:
            raise HTTPException(415, "Encoded camera request bodies are not accepted.")
        content_length = str(request.headers.get("content-length") or "").strip()
        announced_length: int | None = None
        if content_length:
            try:
                announced_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "Invalid camera frame Content-Length.") from exc
            if announced_length < 0:
                raise HTTPException(400, "Invalid camera frame Content-Length.")

        camera_provenance: dict[str, str]
        if is_exterior:
            if exterior_camera is None:
                raise HTTPException(409, "Exterior camera is not configured.")
            camera_session_id = str(
                request.headers.get("x-xomni-camera-session-id") or ""
            ).strip()
            if not 8 <= len(camera_session_id) <= 160:
                raise HTTPException(400, "X-XOmni-Camera-Session-ID is required.")
            if announced_length not in {None, 0}:
                raise HTTPException(
                    400,
                    "Exterior camera analysis does not accept an uploaded image body.",
                )
            async for chunk in request.stream():
                if chunk:
                    raise HTTPException(
                        400,
                        "Exterior camera analysis does not accept an uploaded image body.",
                    )
            try:
                frame = await exterior_camera.current_frame(
                    session_id=camera_session_id,
                    owner_id=session_id(auth_session),
                    conversation_id=conversation_id,
                )
                camera_provenance = {
                    **exterior_camera.source_metadata(),
                    "capture_transport": "server_mjpeg_frame",
                }
            except exterior_camera_svc.ExteriorCameraError as exc:
                raise exterior_camera_http_error(exc) from exc
        else:
            if request.headers.get("x-xomni-camera-session-id"):
                raise HTTPException(
                    400, "Camera session identifiers require the exterior source."
                )
            if (
                announced_length is not None
                and announced_length > camera_svc.MAX_CAMERA_FRAME_BYTES
            ):
                raise HTTPException(
                    413,
                    f"Camera frame exceeds the "
                    f"{camera_svc.MAX_CAMERA_FRAME_BYTES // (1024 * 1024)} MiB limit.",
                )
            chunks = bytearray()
            async for chunk in request.stream():
                if not chunk:
                    continue
                if len(chunks) + len(chunk) > camera_svc.MAX_CAMERA_FRAME_BYTES:
                    raise HTTPException(
                        413,
                        f"Camera frame exceeds the "
                        f"{camera_svc.MAX_CAMERA_FRAME_BYTES // (1024 * 1024)} MiB limit.",
                    )
                chunks.extend(chunk)
            raw = bytes(chunks)
            try:
                frame = camera_svc.validate_camera_frame(
                    raw, request.headers.get("content-type") or ""
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            camera_provenance = {"source": "browser_camera_still"}

        swap_info = None
        if not router_.supports_vision():
            try:
                swap_info = await router_.ensure_capability(vision=True)
            except WorkerSwapError as exc:
                raise HTTPException(503, str(exc)) from exc
        if not router_.supports_vision():
            raise HTTPException(503, "The active model worker does not support vision.")

        def audit_failure(exc: BaseException) -> None:
            try:
                store.audit("camera_frame_analysis_failed", {
                    "conversation_id": conversation_id,
                    "mime": frame.mime,
                    "bytes": frame.byte_count,
                    "sha256": frame.sha256,
                    "width": frame.width,
                    "height": frame.height,
                    "source": camera_provenance["source"],
                    "camera_source_id": camera_provenance.get("camera_source_id"),
                    "camera_label": camera_provenance.get("camera_label"),
                    "capture_transport": camera_provenance.get("capture_transport"),
                    "error_type": type(exc).__name__,
                })
            except Exception:  # noqa: BLE001
                log.warning("Could not persist camera failure audit metadata.")

        try:
            description = await camera_svc.caption_frame(router_, frame, bounded_prompt)
        except asyncio.TimeoutError as exc:
            log.warning(
                "camera frame analysis exceeded %.0fs",
                camera_svc.VISION_TIMEOUT_SECONDS,
            )
            audit_failure(exc)
            raise HTTPException(
                504,
                "Camera analysis timed out before an observation was saved.",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("camera frame analysis failed")
            audit_failure(exc)
            raise HTTPException(
                502, f"Camera analysis failed: {type(exc).__name__}."
            ) from exc

        artifact = camera_svc.observation_artifact(
            frame,
            description,
            source=camera_provenance["source"],
            camera_source_id=camera_provenance.get("camera_source_id"),
            camera_label=camera_provenance.get("camera_label"),
            capture_transport=camera_provenance.get("capture_transport"),
        )
        try:
            message_id = store.add_message(
                conversation_id,
                "assistant",
                "",
                worker_used=router_.active_name,
                artifacts=[artifact],
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("could not persist camera observation")
            raise HTTPException(409, "Camera observation could not be saved.") from exc

        store.audit("camera_frame_analyzed", {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "mime": frame.mime,
            "bytes": frame.byte_count,
            "sha256": frame.sha256,
            "width": frame.width,
            "height": frame.height,
            "worker": router_.active_name,
            "swapped": swap_info is not None,
            "source": camera_provenance["source"],
            "camera_source_id": camera_provenance.get("camera_source_id"),
            "camera_label": camera_provenance.get("camera_label"),
            "capture_transport": camera_provenance.get("capture_transport"),
            "raw_frame_persisted": False,
        })
        return {
            "ok": True,
            "artifact": artifact,
            "message_id": message_id,
            "worker": router_.active_name,
            "swapped": swap_info,
        }

    # ---------- voice ----------

    @api.post("/voice/transcribe")
    async def transcribe(audio: UploadFile = File(...)):
        """Browser-recorded audio -> Omni's native audio understanding.

        Deliberately not the Web Speech API: support is inconsistent on iOS
        and some platforms route audio to a cloud speech service, which
        defeats a local assistant. Omni's audio path was validated on this
        hardware (code/color/action test, 3/3).

        Requires an audio-capable worker. Rather than fail, this swaps to
        one -- the caller is told it happened and what it cost, because a
        silent 15-20s pause is indistinguishable from a hang.
        """
        raw = await audio.read()
        if not raw:
            raise HTTPException(400, "Empty audio upload.")
        if len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413, "Audio too large (25MB limit).")

        swap_info = None
        if not router_.supports_audio():
            try:
                swap_info = await router_.ensure_capability(audio=True)
            except WorkerSwapError as exc:
                raise HTTPException(503, str(exc))

        fmt = "wav"
        ctype = (audio.content_type or "").lower()
        if "webm" in ctype:
            fmt = "webm"
        elif "ogg" in ctype:
            fmt = "ogg"
        elif "mp4" in ctype or "m4a" in ctype:
            fmt = "mp4"
        elif "mpeg" in ctype or "mp3" in ctype:
            fmt = "mp3"

        encoded = base64.b64encode(raw).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Transcribe this audio exactly. Reply with only the "
                         "transcription text and nothing else."},
                {"type": "input_audio",
                 "input_audio": {"data": encoded, "format": fmt}},
            ],
        }]

        from ..models.client import ModelClient  # local import avoids a cycle
        client = ModelClient(router_, temperature=0.0)
        try:
            text = await client.transcribe(messages)
        except Exception as exc:  # noqa: BLE001
            log.exception("transcription failed")
            raise HTTPException(502, f"Transcription failed: {type(exc).__name__}")

        store.audit("voice_transcribed", {"bytes": len(raw), "format": fmt})
        return {"ok": True, "text": text, "worker": router_.active_name,
                "swapped": swap_info}

    return api
