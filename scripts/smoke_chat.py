"""Small end-to-end WebSocket smoke check for a running local X Omni Core."""

from __future__ import annotations

import argparse
import asyncio
import json

import websockets


async def run(uri: str, origin: str, expected: str, timeout: float) -> dict:
    reply_parts: list[str] = []
    event_types: list[str] = []
    conversation_id = None
    message_id = None
    async with websockets.connect(
        uri,
        origin=origin,
        proxy=None,
        open_timeout=10,
        ping_timeout=20,
    ) as socket:
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))
        event_types.append(str(first.get("type")))
        if first.get("type") == "unauthorized":
            raise RuntimeError(first.get("message") or "WebSocket was unauthorized")
        await socket.send(json.dumps({
            "type": "message",
            "conversation_id": None,
            "text": f"Reply with exactly {expected} and nothing else.",
        }))
        while True:
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))
            kind = str(event.get("type"))
            event_types.append(kind)
            if kind == "conversation":
                conversation_id = event.get("conversation_id")
            elif kind == "token":
                reply_parts.append(str(event.get("text") or ""))
            elif kind in {"error", "unauthorized"}:
                raise RuntimeError(event.get("message") or kind)
            elif kind == "done":
                message_id = event.get("message_id")
                break

    reply = "".join(reply_parts).strip()
    if reply != expected:
        raise RuntimeError(f"Unexpected model reply: {reply!r}")
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "reply": reply,
        "event_types": event_types,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="ws://127.0.0.1:8100/ws/chat")
    parser.add_argument("--origin", default="http://127.0.0.1:8100")
    parser.add_argument("--expected", default="LIVE_OK_8131")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(
        run(args.uri, args.origin, args.expected, args.timeout)
    ), indent=2))


if __name__ == "__main__":
    main()
