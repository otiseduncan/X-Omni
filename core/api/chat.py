"""
X Omni -- chat WebSocket.

Client sends:
    {"type":"message","conversation_id":N|null,"text":"..."}
    {"type":"stop","conversation_id":N|null}
    {"type":"approve","approval_id":"...","approved":true}
    {"type":"swap","worker":"omni"|"coder"}

Server sends the orchestrator's event stream plus worker_state pushes,
so the avatar can react to a swap it didn't initiate.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..models.router import WorkerSwapError
from ..orchestrator.loop import Orchestrator
from .auth import session_from_websocket

log = logging.getLogger("xomni.chat")

SWAP_COMMANDS = {
    "/coder": "coder",
    "/omni": "omni",
}

SendJson = Callable[[dict], Awaitable[None]]


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
        send_lock = asyncio.Lock()
        active_task: asyncio.Task | None = None
        active_kind: str | None = None

        async def _safe_send(payload: dict) -> None:
            try:
                async with send_lock:
                    await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 - client vanished mid-send
                pass

        # Push router state changes (including swaps triggered elsewhere)
        # straight to this client.
        def on_worker_event(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(_safe_send(event), loop)

        def _task_running() -> bool:
            return active_task is not None and not active_task.done()

        def _clear_active(task: asyncio.Task) -> None:
            nonlocal active_task, active_kind
            if active_task is task:
                active_task = None
                active_kind = None

        def _start_active(coro, kind: str) -> None:
            nonlocal active_task, active_kind
            task = asyncio.create_task(coro)
            active_task = task
            active_kind = kind
            task.add_done_callback(_clear_active)

        async def _cancel_active() -> bool:
            task = active_task
            kind = active_kind
            if task is None or task.done():
                await _safe_send({"type": "cancelled", "active": False})
                return False
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # The task itself emits the authoritative cancellation event after
            # it has closed the active model/tool coroutine and persisted any
            # partial assistant text.
            log.info("cancelled active chat workload: %s", kind or "unknown")
            return True

        async def _run_message_turn(
            conversation_id: int,
            text: str,
            user_message_id: int,
        ) -> None:
            partial = ""
            try:
                async for event in orchestrator.run_turn(
                    conversation_id,
                    text,
                    approval_context={
                        "session_id": _session_id(session),
                        "user_id": str(session.get("user_id") or "local-dev"),
                        "role": str(session.get("role") or "owner"),
                        "message_id": user_message_id,
                    },
                ):
                    if event.get("type") == "token":
                        partial += str(event.get("text") or "")
                    await _safe_send(event)
            except asyncio.CancelledError:
                partial_message_id = None
                clean_partial = partial.strip()
                if clean_partial:
                    try:
                        partial_message_id = store.add_message(
                            conversation_id,
                            "assistant",
                            clean_partial,
                            worker_used=router_.active_name,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("could not persist cancelled partial response")
                await _safe_send({
                    "type": "cancelled",
                    "active": True,
                    "workload": "response",
                    "conversation_id": conversation_id,
                    "message_id": partial_message_id,
                    "partial_saved": bool(partial_message_id),
                    "worker": router_.active_name,
                })
                raise
            except Exception:  # noqa: BLE001
                log.exception("chat turn failed")
                await _safe_send({"type": "error", "message": "Chat turn failed."})

        async def _run_approval(data: dict) -> None:
            try:
                await _handle_approval(
                    _safe_send, store, orchestrator, registry, session, data
                )
            except asyncio.CancelledError:
                await _safe_send({
                    "type": "cancelled",
                    "active": True,
                    "workload": "approval",
                    "conversation_id": data.get("conversation_id"),
                    "partial_saved": False,
                })
                raise
            except Exception:  # noqa: BLE001
                log.exception("approval continuation failed")
                await _safe_send({
                    "type": "error",
                    "message": "Approval execution or continuation failed.",
                })

        router_.subscribe(on_worker_event)
        try:
            await _safe_send(router_.status())

            while True:
                data = await websocket.receive_json()
                kind = data.get("type")

                if kind == "stop":
                    await _cancel_active()
                    continue

                if kind == "swap":
                    if _task_running():
                        await _safe_send({
                            "type": "error",
                            "message": "Stop the current response before switching models.",
                        })
                        continue
                    if session.get("role") != "owner":
                        await _safe_send({
                            "type": "error",
                            "message": "Model switching requires Owner authorization.",
                        })
                        continue
                    await _handle_swap(_safe_send, router_, store, data.get("worker"))
                    continue

                if kind == "approve":
                    if _task_running():
                        await _safe_send({
                            "type": "error",
                            "message": "X is already working on the current turn.",
                        })
                        continue
                    _start_active(_run_approval(data), "approval")
                    continue

                if kind != "message":
                    continue

                if _task_running():
                    await _safe_send({
                        "type": "error",
                        "message": "X is already responding. Stop the current response first.",
                    })
                    continue

                text = str(data.get("text") or "").strip()
                if not text:
                    continue

                conversation_id = data.get("conversation_id")
                if not conversation_id:
                    conversation_id = store.create_conversation(
                        user_id=str(session.get("user_id") or "local-dev")
                    )
                    await _safe_send(
                        {"type": "conversation", "conversation_id": conversation_id}
                    )
                else:
                    try:
                        conversation_id = int(conversation_id)
                    except (TypeError, ValueError):
                        await _safe_send({
                            "type": "error", "message": "Invalid conversation identifier."
                        })
                        continue
                    if not store.conversation_exists(
                        conversation_id,
                        user_id=str(session.get("user_id") or "local-dev"),
                    ):
                        await _safe_send({
                            "type": "error", "message": "Conversation does not exist."
                        })
                        continue

                # Manual routing override. Deterministic on purpose -- the
                # model does not get to decide when to spend 15-20 seconds.
                lowered = text.lower()
                if (
                    session.get("role") != "owner"
                    and any(lowered.startswith(command) for command in SWAP_COMMANDS)
                ):
                    await _safe_send({
                        "type": "error",
                        "message": "Model switching requires Owner authorization.",
                    })
                    continue
                for command, worker in SWAP_COMMANDS.items():
                    if lowered.startswith(command):
                        text = text[len(command):].strip()
                        if router_.active_name != worker:
                            await _handle_swap(_safe_send, router_, store, worker)
                        if not text:
                            await _safe_send(
                                {"type": "done", "message_id": None,
                                 "worker": router_.active_name, "artifacts": []}
                            )
                            break
                else:
                    command = None
                if command and not text:
                    continue

                user_message_id = store.add_message(conversation_id, "user", text)
                await _safe_send({"type": "thinking"})
                _start_active(
                    _run_message_turn(conversation_id, text, user_message_id),
                    "response",
                )

        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("chat socket error")
            await _safe_send({"type": "error", "message": "Chat connection failed."})
        finally:
            if _task_running():
                active_task.cancel()
                with suppress(asyncio.CancelledError):
                    await active_task
            router_.unsubscribe(on_worker_event)

    async def _handle_swap(send_json: SendJson, router_, store, worker) -> None:
        worker = str(worker or "").strip().lower()
        if worker not in router_.configs:
            await send_json(
                {"type": "error", "message": f"Unknown worker '{worker}'."}
            )
            return
        try:
            result = await router_.swap_to(worker)
        except WorkerSwapError as exc:
            await send_json({"type": "error", "message": str(exc)})
            return
        store.audit("worker_swap", result)
        await send_json({"type": "swap_complete", **result})

    def _session_id(session: dict) -> str:
        token_hash = str(session.get("token_hash") or "").strip()
        if token_hash:
            return f"session:{token_hash}"
        return f"local:{session.get('google_sub') or 'local-dev'}"

    async def _handle_approval(
        send_json: SendJson, store, orchestrator, registry, session, data
    ) -> None:
        approval_id = data.get("approval_id")
        record = store.get_approval(approval_id) if approval_id else None
        if not record:
            await send_json({"type": "error", "message": "Unknown approval."})
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
                user_id=str(session.get("user_id") or "local-dev"),
                on_status=lambda status: send_json(
                    {"type": "approval_status", "id": approval_id, "status": status}
                ),
            )
        except (KeyError, PermissionError, ValueError) as exc:
            await send_json({"type": "error", "message": str(exc)})
            return

        current = outcome["approval"]
        status = current["status"]
        receipt = outcome.get("receipt")
        await send_json(
            {"type": "approval_status", "id": approval_id, "status": status}
        )
        if receipt is None:
            # Another decision already owns execution. Do not imply completion
            # and, most importantly, do not invoke the tool a second time.
            return

        await send_json(
            {"type": "approval_receipt", "id": approval_id, "receipt": receipt}
        )
        execution_authorized = status in {"succeeded", "failed"}
        await send_json({
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
        await send_json({"type": "thinking"})
        async for event in orchestrator.run_turn(
            conversation_id,
            "",
            approved_tool={
                "name": current["tool_name"],
                "args": registry.log_args(current["tool_name"], current["args"]),
                "result": receipt["result"],
                "receipt": receipt,
                "call_id": current["tool_call_id"],
            },
            approval_context={
                "session_id": _session_id(session),
                "user_id": str(session.get("user_id") or "local-dev"),
                "role": str(session.get("role") or "owner"),
                "message_id": int(current["message_id"]),
            },
        ):
            await send_json(event)

    return ws_router
