"""
X Omni -- chat WebSocket.

Client sends:
    {"type":"message","conversation_id":N|null,"text":"..."}
    {"type":"approve","approval_id":"...","approved":true}
    {"type":"swap","worker":"omni"|"coder"}

Server sends the orchestrator's event stream plus worker_state pushes,
so the avatar can react to a swap it didn't initiate.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models.router import WorkerSwapError
from ..orchestrator.loop import Orchestrator
from .auth import session_from_websocket

log = logging.getLogger("xomni.chat")

SWAP_COMMANDS = {
    "/coder": "coder",
    "/omni": "omni",
}


def create_router(settings, store, router_, client, registry) -> APIRouter:
    ws_router = APIRouter()
    orchestrator = Orchestrator(router_, client, registry, store, settings)

    @ws_router.websocket("/ws/chat")
    async def chat_socket(websocket: WebSocket):
        # Accept BEFORE any rejection. Closing a websocket before accept()
        # produces a bare HTTP handshake failure -- the browser cannot read
        # the close code or reason, so the client can't tell "not signed in"
        # apart from "server is down" and retries forever. Accept, say why,
        # then close.
        await websocket.accept()

        session = session_from_websocket(settings, store, websocket)
        if session is None:
            await websocket.send_json({
                "type": "unauthorized",
                "message": "Not signed in. Sign in with Google, or set "
                           "XOMNI_AUTH_ENABLED=0 for local use.",
            })
            await websocket.close(code=4401)
            return

        loop = asyncio.get_running_loop()

        # Push router state changes (including swaps triggered elsewhere)
        # straight to this client.
        def on_worker_event(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(_safe_send(event), loop)

        async def _safe_send(payload: dict) -> None:
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 - client vanished mid-send
                pass

        router_.subscribe(on_worker_event)
        try:
            await websocket.send_json(router_.status())

            while True:
                data = await websocket.receive_json()
                kind = data.get("type")

                if kind == "swap":
                    await _handle_swap(websocket, router_, store, data.get("worker"))
                    continue

                if kind == "approve":
                    await _handle_approval(
                        websocket, store, orchestrator, registry, session, data
                    )
                    continue

                if kind != "message":
                    continue

                text = str(data.get("text") or "").strip()
                if not text:
                    continue

                conversation_id = data.get("conversation_id")
                if not conversation_id:
                    conversation_id = store.create_conversation()
                    await websocket.send_json(
                        {"type": "conversation", "conversation_id": conversation_id}
                    )

                # Manual routing override. Deterministic on purpose -- the
                # model does not get to decide when to spend 15-20 seconds.
                lowered = text.lower()
                for command, worker in SWAP_COMMANDS.items():
                    if lowered.startswith(command):
                        text = text[len(command):].strip()
                        if router_.active_name != worker:
                            await _handle_swap(websocket, router_, store, worker)
                        if not text:
                            await websocket.send_json(
                                {"type": "done", "message_id": None,
                                 "worker": router_.active_name, "artifacts": []}
                            )
                            break
                else:
                    command = None
                if command and not text:
                    continue

                user_message_id = store.add_message(conversation_id, "user", text)
                await websocket.send_json({"type": "thinking"})

                async for event in orchestrator.run_turn(
                    conversation_id,
                    text,
                    approval_context={
                        "session_id": _session_id(session),
                        "user_id": str(session.get("google_sub") or "local-dev"),
                        "message_id": user_message_id,
                    },
                ):
                    await websocket.send_json(event)

        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("chat socket error")
            await _safe_send({"type": "error", "message": "Chat connection failed."})
        finally:
            router_.unsubscribe(on_worker_event)

    async def _handle_swap(websocket, router_, store, worker) -> None:
        worker = str(worker or "").strip().lower()
        if worker not in router_.configs:
            await websocket.send_json(
                {"type": "error", "message": f"Unknown worker '{worker}'."}
            )
            return
        try:
            result = await router_.swap_to(worker)
        except WorkerSwapError as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            return
        store.audit("worker_swap", result)
        await websocket.send_json({"type": "swap_complete", **result})

    def _session_id(session: dict) -> str:
        token_hash = str(session.get("token_hash") or "").strip()
        if token_hash:
            return f"session:{token_hash}"
        return f"local:{session.get('google_sub') or 'local-dev'}"

    async def _handle_approval(
        websocket, store, orchestrator, registry, session, data
    ) -> None:
        approval_id = data.get("approval_id")
        record = store.get_approval(approval_id) if approval_id else None
        if not record:
            await websocket.send_json({"type": "error", "message": "Unknown approval."})
            return
        approved = bool(data.get("approved"))
        supplied_conversation = data.get("conversation_id")
        try:
            supplied_conversation = (
                int(supplied_conversation) if supplied_conversation is not None else None
            )
            outcome = await registry.resolve_approval(
                approval_id,
                approved,
                conversation_id=supplied_conversation,
                session_id=_session_id(session),
                user_id=str(session.get("google_sub") or "local-dev"),
                on_status=lambda status: websocket.send_json(
                    {"type": "approval_status", "id": approval_id, "status": status}
                ),
            )
        except (KeyError, PermissionError, ValueError) as exc:
            await websocket.send_json({"type": "error", "message": str(exc)})
            return

        current = outcome["approval"]
        status = current["status"]
        receipt = outcome.get("receipt")
        await websocket.send_json(
            {"type": "approval_status", "id": approval_id, "status": status}
        )
        if receipt is None:
            # Another decision already owns execution. Do not imply completion
            # and, most importantly, do not invoke the tool a second time.
            return

        await websocket.send_json(
            {"type": "approval_receipt", "id": approval_id, "receipt": receipt}
        )
        execution_authorized = status in {"succeeded", "failed"}
        await websocket.send_json({
            "type": "approval_resolved",
            "id": approval_id,
            "approved": execution_authorized,
            "status": status,
            "executed": receipt["executed"],
            "success": receipt["success"],
            "receipt": receipt,
            "replayed": outcome["replayed"],
        })

        # Only the CAS winner synthesizes a continuation. A retry receives the
        # durable receipt but cannot duplicate either execution or chat output.
        if not outcome["claimed"] or status not in {"succeeded", "failed"}:
            return
        conversation_id = int(current["conversation_id"])
        await websocket.send_json({"type": "thinking"})
        async for event in orchestrator.run_turn(
            conversation_id,
            "",
            approved_tool={
                "name": current["tool_name"],
                "args": current["args"],
                "result": receipt["result"],
                "receipt": receipt,
                "call_id": current["tool_call_id"],
            },
            approval_context={
                "session_id": _session_id(session),
                "user_id": str(session.get("google_sub") or "local-dev"),
                "message_id": int(current["message_id"]),
            },
        ):
            await websocket.send_json(event)

    return ws_router
