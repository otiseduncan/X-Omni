"""Navigator-only recovery for malformed model tool-call JSON.

Qwen occasionally copies a large slice of the current OEM procedure into the
``navigator_browse`` arguments and misses a closing quote. llama.cpp rejects
that completion with HTTP 500 before X Omni receives any tool call, so no
browser action has executed and one constrained retry is safe. Qwen can also
emit a nominal tool call whose arguments decode to an empty/missing action;
that is caught here before the browser loop sees it.

This wrapper is deliberately scoped to Navigator. It does not change generic
model/tool behavior and it does not choose the semantic action for X. The
repair turn only asks the same model to express its intended next action as one
small, schema-valid ``navigator_browse`` call.
"""

from __future__ import annotations

import json
import logging
from functools import wraps
from typing import Any

log = logging.getLogger("xomni.research_navigator_tool_repair")

_INSTALLED_ATTR = "__xomni_navigator_tool_json_repair_installed__"
_REPAIR_ATTR = "__xomni_navigator_tool_json_repair_client__"
_NAV_ACTIONS = frozenset({
    "observe", "observe_marks", "click", "type", "fill", "press", "back",
    "open", "scroll", "wait", "click_mark", "click_visual", "select_vehicle",
    "extract", "done",
})

_REPAIR_INSTRUCTION = (
    "Your previous Navigator tool call was malformed, so NO browser action executed. "
    "Reconsider the same current page and return exactly ONE navigator_browse tool "
    "call with valid JSON and only the fields required for that action. Never copy "
    "page/procedure text, quotations, explanations, or evidence into tool arguments. "
    "For extract, done, observe, observe_marks, or back, the arguments should contain "
    "only the action. If the latest observation says the bottom of the page has been "
    "reached and you judge that page to be the requested procedure, use "
    "{\"action\":\"extract\"}. Otherwise choose the one next browser action you "
    "actually need."
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


def _malformed_navigator_event(events: list[dict[str, Any]]) -> bool:
    """True only when a surfaced Navigator tool call cannot name a real action."""
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool_call":
            continue
        if event.get("name") != "navigator_browse":
            continue
        try:
            args = json.loads(str(event.get("arguments") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        if not isinstance(args, dict):
            return True
        action = str(args.get("action") or "").casefold()
        if action not in _NAV_ACTIONS:
            return True
    return False


class NavigatorToolRepairClient:
    """Delegate model client with one final, constrained Navigator repair turn."""

    def __init__(self, delegate: Any):
        self._delegate = delegate
        self.navigator_tool_json_repairs = 0
        setattr(self, _REPAIR_ATTR, True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def _repair(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any,
        max_tokens: int | None,
    ) -> list[dict[str, Any]]:
        self.navigator_tool_json_repairs += 1
        log.warning(
            "Navigator model turn produced malformed tool arguments; requesting one "
            "constrained repair turn."
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
        repaired = [
            event
            async for event in self._delegate.stream(
                repaired_messages,
                tools=tools,
                max_tokens=repair_max_tokens,
                tool_choice=forced,
            )
        ]
        if _malformed_navigator_event(repaired):
            raise RuntimeError(
                "Navigator repair turn still returned malformed tool arguments."
            )
        return repaired

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        max_tokens: int | None = None,
        *,
        tool_choice: Any = None,
    ):
        if not _is_navigator_toolset(tools):
            async for event in self._delegate.stream(
                messages,
                tools=tools,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
            ):
                yield event
            return

        # Buffer Navigator turns until their tool arguments are known-good.
        # No browser action is dispatched until these events are yielded to the
        # reasoning loop, so replacing a malformed event here cannot replay work.
        try:
            events = [
                event
                async for event in self._delegate.stream(
                    messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    tool_choice=tool_choice,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - narrowly recover parser rejection only
            if not _is_tool_json_parse_failure(exc):
                raise
            events = await self._repair(messages, tools=tools, max_tokens=max_tokens)
        else:
            if _malformed_navigator_event(events):
                events = await self._repair(messages, tools=tools, max_tokens=max_tokens)

        for event in events:
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
