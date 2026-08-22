"""Acquisition into ADAS SI is the highest-consequence step in the research
pipeline -- it becomes the answer for every future query about a vehicle.
_capture_to_adas must independently re-confirm the claimed vehicle against the
live page before saving, regardless of which navigation path (deterministic
or model-driven agent) produced that page.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.services import research_operator


class _EmptyLoc:
    def __init__(self):
        self.first = self

    async def is_visible(self, timeout=None):  # noqa: ARG002
        return False


class _FakePage:
    def __init__(self, *, url: str, title: str):
        self.url = url
        self._title = title

    def get_by_text(self, *_args, **_kwargs):
        return _EmptyLoc()

    async def title(self):
        return self._title

    async def pdf(self, **_kwargs):
        return b"%PDF-fake"


class _FakeAdas:
    def __init__(self, root: Path):
        self.source_root = root
        self.inventory = SimpleNamespace(_cache=None)

    def available(self):
        return True

    def _pages(self, _path):
        return [(1, "some text")]

    def relative_of(self, path: Path):
        return str(path)


def _browser(tmp_path: Path, page: _FakePage) -> research_operator.LicensedBrowser:
    browser = research_operator.LicensedBrowser(tmp_path, adas=_FakeAdas(tmp_path))

    async def fake_start(auto_login=True):  # noqa: ARG001
        return {"authenticated": True}

    browser.start = fake_start  # type: ignore[assignment]
    browser._page = page
    return browser


@pytest.mark.asyncio
async def test_capture_is_refused_when_the_page_does_not_confirm_the_claimed_vehicle(tmp_path: Path):
    # The exact reported failure mode: a page that never actually confirmed a
    # vehicle selection, with a capture call claiming one anyway.
    page = _FakePage(
        url="https://my.alldata.com/repair/#/vehicle-select",
        title="ALLDATA Collision - Home",
    )
    browser = _browser(tmp_path, page)

    with pytest.raises(ValueError, match="Refusing to preserve"):
        await browser._capture_to_adas({"vehicle": "2018 Ford F-350", "topic": "Camera calibration"})


@pytest.mark.asyncio
async def test_capture_succeeds_when_the_page_confirms_the_claimed_vehicle(tmp_path: Path):
    page = _FakePage(
        url="https://my.alldata.com/repair/#/vehicle/2018-ford-f350",
        title="2018 Ford F-350 - ALLDATA",
    )
    browser = _browser(tmp_path, page)

    result = await browser._capture_to_adas({"vehicle": "2018 Ford F-350", "topic": "Camera calibration"})

    assert result["status"] == "success"
    assert result["saved"] is True


@pytest.mark.asyncio
async def test_capture_without_a_vehicle_argument_is_not_gated(tmp_path: Path):
    """A capture that doesn't claim to be about a specific vehicle (e.g. a
    general policy page) isn't subject to the vehicle-identity gate at all."""
    page = _FakePage(
        url="https://my.alldata.com/repair/#/policy",
        title="ALLDATA Collision - Home",
    )
    browser = _browser(tmp_path, page)

    result = await browser._capture_to_adas({"topic": "General collision policy"})

    assert result["status"] == "success"
