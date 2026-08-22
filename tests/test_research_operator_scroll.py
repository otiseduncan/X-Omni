"""Field report: scrolling a year/make/model picker in the inline ALLDATA
browser did nothing. A real mouse wheel scrolls whatever is under the
cursor, not the page as a whole -- a picker popup commonly renders in a
different spot than the field that opened it, so a scroll fired at a stale
cursor position (left over from an earlier, unrelated tap) can land outside
the popup entirely and never move it. human_action's scroll branch must move
the cursor to the tapped point before scrolling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.services import research_operator


class _FakeLocator:
    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return False


class _FakeMouse:
    def __init__(self):
        self.calls: list[tuple] = []

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))

    async def click(self, x, y):
        self.calls.append(("click", x, y))


class _FakePage:
    def __init__(self, *, url: str = "https://my.alldata.com/repair/#/vehicle-select", title: str = "ALLDATA Collision"):
        self.url = url
        self._title = title
        self.mouse = _FakeMouse()

    async def title(self):
        return self._title

    def locator(self, _selector):
        return _FakeLocator()


def _browser(tmp_path: Path, page: _FakePage) -> research_operator.LicensedBrowser:
    browser = research_operator.LicensedBrowser(tmp_path, adas=None)
    browser._page = page
    browser._session_id = "session-1"
    return browser


@pytest.mark.asyncio
async def test_scroll_with_coordinates_moves_the_cursor_there_first(tmp_path: Path):
    page = _FakePage()
    browser = _browser(tmp_path, page)

    await browser.human_action("session-1", {"action": "scroll", "dy": 700, "x": 300, "y": 500})

    assert page.mouse.calls == [("move", 300.0, 500.0), ("wheel", 0, 700.0)]


@pytest.mark.asyncio
async def test_scroll_without_coordinates_still_scrolls_in_place(tmp_path: Path):
    # Backward compatible: an older client that never sends x/y must keep
    # working exactly as before -- just no cursor repositioning.
    page = _FakePage()
    browser = _browser(tmp_path, page)

    await browser.human_action("session-1", {"action": "scroll", "dy": -700})

    assert page.mouse.calls == [("wheel", 0, -700.0)]


@pytest.mark.asyncio
async def test_scroll_coordinates_are_bounded_to_the_screenshot_viewport(tmp_path: Path):
    page = _FakePage()
    browser = _browser(tmp_path, page)

    await browser.human_action("session-1", {"action": "scroll", "dy": 700, "x": 99999, "y": -50})

    assert page.mouse.calls == [
        ("move", float(research_operator.SCREENSHOT_WIDTH), 0.0),
        ("wheel", 0, 700.0),
    ]
