"""Receipt-backed live approval smoke check against a running local Core."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path

import websockets


async def receive(socket, timeout: float) -> dict:
    return json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))


async def run(uri: str, origin: str, target: Path, content: str, timeout: float) -> dict:
    conversation_id = None
    approval = None
    first_turn_done = False
    first_receipt = None
    first_resolved = None
    replay_resolved = None
    event_types: list[str] = []

    async with websockets.connect(
        uri, origin=origin, proxy=None, open_timeout=10, ping_timeout=20
    ) as socket:
        event_types.append(str((await receive(socket, timeout)).get("type")))
        prompt = (
            f"Use write_file to create {target} with the exact content {content}. "
            "Do not claim completion without calling the tool."
        )
        await socket.send(json.dumps({
            "type": "message", "conversation_id": None, "text": prompt,
        }))

        while not first_turn_done:
            event = await receive(socket, timeout)
            kind = str(event.get("type"))
            event_types.append(kind)
            if kind == "conversation":
                conversation_id = int(event["conversation_id"])
            elif kind == "approval":
                approval = event["approval"]
            elif kind in {"error", "unauthorized"}:
                raise RuntimeError(event.get("message") or kind)
            elif kind == "done":
                first_turn_done = True

        if not conversation_id or not approval:
            raise RuntimeError("Model did not produce a bound approval request")
        args = approval.get("args") or {}
        requested_target = Path(str(args.get("path") or "")).resolve()
        if approval.get("tool") != "write_file":
            raise RuntimeError(f"Refusing unexpected tool {approval.get('tool')!r}")
        if requested_target != target.resolve() or str(args.get("content")) != content:
            raise RuntimeError("Refusing approval because the model changed the target action")

        await socket.send(json.dumps({
            "type": "approve",
            "approval_id": approval["id"],
            "approved": True,
            "conversation_id": conversation_id,
        }))
        continuation_done = False
        while not continuation_done:
            event = await receive(socket, timeout)
            kind = str(event.get("type"))
            event_types.append(kind)
            if kind == "approval_receipt":
                first_receipt = event.get("receipt")
            elif kind == "approval_resolved":
                first_resolved = event
            elif kind in {"error", "unauthorized"}:
                raise RuntimeError(event.get("message") or kind)
            elif kind == "done":
                continuation_done = True

        await socket.send(json.dumps({
            "type": "approve",
            "approval_id": approval["id"],
            "approved": True,
            "conversation_id": conversation_id,
        }))
        while replay_resolved is None:
            event = await receive(socket, timeout)
            kind = str(event.get("type"))
            event_types.append(kind)
            if kind == "approval_resolved":
                replay_resolved = event
            elif kind in {"error", "unauthorized"}:
                raise RuntimeError(event.get("message") or kind)

    if target.read_text(encoding="utf-8") != content:
        raise RuntimeError("Approved file content does not match")
    if not first_receipt or not first_resolved:
        raise RuntimeError("Terminal receipt/resolution was not delivered")
    if not first_receipt.get("executed") or not first_receipt.get("success"):
        raise RuntimeError("First terminal receipt does not prove successful execution")
    replay_receipt = (replay_resolved or {}).get("receipt") or {}
    if not replay_resolved.get("replayed"):
        raise RuntimeError("Duplicate approval was not labeled as a replay")
    if replay_receipt.get("receipt_id") != first_receipt.get("receipt_id"):
        raise RuntimeError("Duplicate approval returned different receipt evidence")

    return {
        "ok": True,
        "conversation_id": conversation_id,
        "approval_id": approval["id"],
        "receipt_id": first_receipt["receipt_id"],
        "executed": first_receipt["executed"],
        "success": first_receipt["success"],
        "replay": replay_resolved["replayed"],
        "target": str(target),
        "event_types": event_types,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="ws://127.0.0.1:8100/ws/chat")
    parser.add_argument("--origin", default="http://127.0.0.1:8100")
    parser.add_argument("--content", default="APPROVAL_ONCE_OK")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--target")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = (
        Path(args.target).resolve()
        if args.target
        else root / "data" / f"live-approval-smoke-{datetime.now():%Y%m%d-%H%M%S}.txt"
    )
    print(json.dumps(asyncio.run(
        run(args.uri, args.origin, target, args.content, args.timeout)
    ), indent=2))


if __name__ == "__main__":
    main()
