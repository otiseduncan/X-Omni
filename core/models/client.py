"""
X Omni -- OpenAI-compatible streaming client for llama.cpp server.

Always resolves the base URL and alias from the router at call time
rather than caching them, so a swap that happens between turns is picked
up automatically with no reconnect logic anywhere upstream.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

import httpx

from .router import ModelRouter, WorkerSwapError

log = logging.getLogger("xomni.client")


class ModelError(RuntimeError):
    pass


class ModelClient:
    # Production turns support one bounded same-model evidence review before
    # Core accepts an initial prose-only draft. Lightweight test clients opt in
    # explicitly so their scripted call counts remain deterministic.
    supports_no_tool_self_check = True

    def __init__(self, router: ModelRouter, temperature: float = 0.4,
                 max_tokens: int = 1536):
        self.router = router
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _active(self):
        cfg = self.router.active_config()
        if cfg is None:
            raise ModelError("No model worker is active.")
        return cfg

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        max_tokens: Optional[int] = None,
        *,
        tool_choice: Optional[str | dict[str, Any]] = None,
    ) -> AsyncIterator[dict]:
        """Stream a turn, recovering once if the worker has died.

        A bare httpx ConnectError surfaces to the user as "All connection
        attempts failed", which says nothing useful. If the worker port is
        dead we relaunch it once and retry -- and only retry when no tokens
        have been emitted yet, so a mid-stream failure can't duplicate
        output the user already saw.
        """
        emitted = False
        try:
            async with self.router.inference_session():
                async for event in self._stream_once(
                    messages,
                    tools,
                    max_tokens,
                    tool_choice=tool_choice,
                ):
                    emitted = True
                    yield event
            return
        except (httpx.ConnectError, WorkerSwapError):
            if emitted:
                raise ModelError(
                    "Lost the connection to the model worker mid-reply. "
                    "It likely crashed -- check its console window."
                )
            log.warning("Worker unreachable; attempting recovery.")
        except httpx.ReadTimeout:
            raise ModelError(
                "The model worker accepted the request but never responded. "
                "It may be stuck loading or out of VRAM."
            )

        try:
            await self.router.recover()
        except Exception as exc:  # noqa: BLE001
            raise ModelError(
                f"The model worker is not running and could not be restarted: {exc}"
            ) from exc

        try:
            async with self.router.inference_session():
                async for event in self._stream_once(
                    messages,
                    tools,
                    max_tokens,
                    tool_choice=tool_choice,
                ):
                    yield event
        except (httpx.ConnectError, WorkerSwapError) as exc:
            raise ModelError(
                "The verified model worker did not accept the request after recovery."
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ModelError(
                "The recovered model worker accepted the request but never responded."
            ) from exc

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        max_tokens: Optional[int] = None,
        *,
        tool_choice: Optional[str | dict[str, Any]] = None,
    ) -> AsyncIterator[dict]:
        """Yields {"type": "content", "text": ...} as tokens arrive, then
        {"type": "tool_call", ...} for any accumulated calls once the
        stream closes. llama.cpp emits tool-call fragments across many
        chunks, so they're assembled by index before being surfaced."""
        cfg = self._active()
        payload: dict[str, Any] = {
            "model": cfg.alias,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        elif tool_choice is not None:
            raise ValueError("tool_choice requires at least one advertised tool")

        pending: dict[int, dict] = {}
        timeout = httpx.Timeout(15.0, read=600.0, write=60.0, pool=15.0)

        # trust_env=False -- a stray HTTP_PROXY/ALL_PROXY must never
        # hijack a 127.0.0.1 request.
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream("POST", f"{cfg.base_url}/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:600]
                    raise ModelError(f"Worker returned HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    text = delta.get("content")
                    if text:
                        yield {"type": "content", "text": str(text)}
                    for call in delta.get("tool_calls") or []:
                        idx = int(call.get("index", 0))
                        acc = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        acc["id"] += str(call.get("id") or "")
                        fn = call.get("function") or {}
                        acc["name"] += str(fn.get("name") or "")
                        acc["arguments"] += str(fn.get("arguments") or "")

        for idx in sorted(pending):
            call = pending[idx]
            if call["name"]:
                yield {"type": "tool_call", **call}

    async def complete(self, messages: list[dict], max_tokens: int = 320,
                       temperature: float = 0.1) -> str:
        try:
            async with self.router.inference_session():
                return await self._complete_once(messages, max_tokens, temperature)
        except (httpx.ConnectError, WorkerSwapError):
            log.warning("Worker unreachable during completion; attempting recovery.")
        except httpx.ReadTimeout as exc:
            raise ModelError(
                "The model worker accepted the request but never responded."
            ) from exc

        try:
            await self.router.recover()
            async with self.router.inference_session():
                return await self._complete_once(messages, max_tokens, temperature)
        except (httpx.ConnectError, httpx.ReadTimeout, WorkerSwapError) as exc:
            raise ModelError(
                "The verified model worker did not complete the request after recovery."
            ) from exc

    async def _complete_once(
        self, messages: list[dict], max_tokens: int, temperature: float
    ) -> str:
        cfg = self._active()
        payload = {
            "model": cfg.alias,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        timeout = httpx.Timeout(15.0, read=300.0, write=60.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(f"{cfg.base_url}/chat/completions", json=payload)
            if resp.status_code != 200:
                body = resp.text[:600]
                raise ModelError(f"Worker returned HTTP {resp.status_code}: {body}")
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ModelError("Worker returned a malformed completion response.") from exc
            return str(content).strip()

    async def transcribe(self, messages: list[dict], max_tokens: int = 512) -> str:
        """Audio understanding goes through the same chat endpoint -- Omni
        takes input_audio content parts natively. Caller is responsible for
        having ensured an audio-capable worker is active."""
        return await self.complete(messages, max_tokens=max_tokens, temperature=0.0)
