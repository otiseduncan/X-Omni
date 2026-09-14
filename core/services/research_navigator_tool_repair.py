"""Navigator-only recovery for malformed model tool-call JSON.

Qwen occasionally copies a large slice of the current OEM procedure into the
``navigator_browse`` arguments and misses a closing quote. llama.cpp rejects
that completion with HTTP 500 before X Omni receives any tool call, so no
browser action has executed and one constrained retry is safe.

This wrapper is deliberately scoped to Navigator. It does not change generic
model/tool behavior and it does not choose the semantic action for X. The
repair turn only asks the same model to express its intended next action as one
small, schema-valid ``navigator_browse`` call.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

log = logging.getLogger("xomni.research_navigator_tool_repair")

_INSTALLED_ATTR = "__xomni_navigator_tool_json_repair_installed__"
_REPAIR_ATTR = "__xomni_navigator_tool_json_repair_client__"

_REPAIR_INSTRUCTION = (
    "Your previous Navigator tool call could not be parsed as JSON, so NO browser "
    "action executed. Reconsider the same current page and return exactly ONE "
    "navigator_browse tool call with valid JSON and only the fields required for "
    "that action. Never copy page/procedure text, quotations, explanations, or "
    "evidence into tool arguments. For extract, done, observe, observe_marks, or "
    "back, the arguments should contain only the action. If the latest observation "
    "says the bottom of the page has been reached and you judge that page to be the "
    "requested procedure, use {\"action\":\"extract\"}. Otherwise choose the one "
    "next browser action you actually need."
)


def _is_tool_json_parse_failure(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return (
        "http 500" in text
        and "tool call" in text
        and "json" in text
        and ("parse" in text or "argument" in text)
    )


def _is_navigator_toolset(tools: Any) -> bool:
    if not isinstance(tools, list) or len(tools) != 1:
        return False
    try:
        return tools[0]["function"]["name"] == "navigator_browse"
    except (KeyError, TypeError):
        return False


class NavigatorToolRepairClient:
    """Delegate model client with one final, constrained Navigator repair turn."""

    def __init__(self, delegate: Any):
        self._delegate = delegate
        self.navigator_tool_json_repairs = 0
        setattr(self, _REPAIR_ATTR, True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        max_tokens: int | None = None,
        *,
        tool_choice: Any = None,
    ):
        emitted = False
        try:
            async for event in self._delegate.stream(
                messages,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
            ):
                emitted = True
                yield event
            return
        except Exception as exc:  # noqa: BLE001 - preserve delegate error unless narrowly repairable
            if emitted or not _is_navigator_toolset(tools) or not _is_tool_json_parse_failure(exc):
                raise

            self.navigator_tool_json_repairs += 1
            log.warning(
                "Navigator model turn produced malformed tool JSON after normal retry; "
                "requesting one constrained repair turn."
            )
            repaired_messages = list(messages) + [
                {"role": "user", "content": _REPAIR_INSTRUCTION}
            ]
            forced = {
                "type": "function",
                "function": {"name": "navigator_browse"},
            }
            # Navigator actions are tiny. Keeping the repair generation bounded
            # prevents another accidental dump of the OEM article into arguments.
            repair_max_tokens = min(int(max_tokens or 192), 192)
            async for event in self._delegate.stream(
                repaired_messages,
                tools=tools,
                max_tokens=repair_max_tokens,
                tool_choice=forced,
            ):
                yield event


def install(module: Any) -> None:
    """Wrap only ``run_navigator_search`` clients; preserve its public signature."""
    if getattr(module, _INSTALLED_ATTR, False):
        return

    original = module.run_navigator_search

    @wraps(original)
    async def run_navigator_search_with_tool_repair(*args: Any, **kwargs: Any):
        client = kwargs.get("client")
        if client is not None and not getattr(client, _REPAIR_ATTR, False):
            kwargs["client"] = NavigatorToolRepairClient(client)
        return await original(*args, **kwargs)

    module.run_navigator_search = run_navigator_search_with_tool_repair
    setattr(module, _INSTALLED_ATTR, True)
