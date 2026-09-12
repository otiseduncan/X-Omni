"""Tests for the background ADAS service-information harvest.

These drive a fake ScrapeX navigator rather than a browser, and cover the
things that actually went wrong while establishing this route live against
ALLDATA on 2026-09-12.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.services import adas_si_harvest as harvest


def _element(ref: str, name: str, role: str = "link") -> dict[str, Any]:
    return {"ref": ref, "role": role, "name": name, "expanded": None}


def _page(url: str, title: str, names: list[str], text: str = "x" * 900) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "page_text": text,
        "elements": [_element(f"e{i}", n) for i, n in enumerate(names, 1)],
    }


class _FakeNavigator:
    """A small ALLDATA shaped like the real one, three levels deep."""

    PICKER = _page(
        harvest.PICKER_URL, "ALLDATA Collision - Home",
        ["2021 Honda Civic Sedan", "2014 Nissan-Datsun Truck Rogue 4WD"],
    )
    VEHICLE = _page(
        "https://my.alldata.com/repair/#/vehicle/62139",
        "Vehicle Information - 2021 Honda Civic Sedan L4-2.0L (K20C2) - ALLDATA Collision",
        ["ADAS Quick Reference", "Collision Reference", "Specifications Quick Reference"],
    )
    QUICK = _page(
        "https://my.alldata.com/repair/#/article/62139/component/1",
        "ADAS Systems, Locations, and Calibrations - ALLDATA Collision",
        # The RELATED INFORMATION block follows the ADAS rows, as on the real page.
        ["Millimeter Wave Radar", "Rearview Camera", "Components", "Diagrams", "Grounds"],
    )
    HUB = _page(
        "https://my.alldata.com/repair/#/vehicle/62139/component/872",
        "Collision Avoidance Sensor - ALLDATA Collision",
        # "Diagrams" and "Parts and Labor" are section headings here, not a footer.
        ["Diagrams", "Connector Views", "Parts and Labor",
         "Removal and Replacement", "Programming and Relearning"],
        text="y" * 500,
    )
    PROGRAMMING = _page(
        "https://my.alldata.com/repair/#/vehicle/62139/component/872/itype/387",
        "Programming and Relearning - ALLDATA Collision",
        ["Millimeter Wave Radar Aiming"],
        text="z" * 450,
    )
    AIMING = _page(
        "https://my.alldata.com/repair/#/article/62139/component/872/nonstandard/13641",
        "Millimeter Wave Radar Aiming (Collision Avoidance Sensor) - ALLDATA Collision",
        [],
        text="Install the reflector exactly 2.5 m (8.2 ft) away. " * 60,
    )
    REMOVAL = _page(
        "https://my.alldata.com/repair/#/article/62139/component/872/nonstandard/99",
        "Removal and Replacement (Collision Avoidance Sensor) - ALLDATA Collision",
        [], text="w" * 800,
    )
    CAMERA = _page(
        "https://my.alldata.com/repair/#/vehicle/62139/component/900",
        "Rear Vision Camera - ALLDATA Collision", [], text="v" * 742,
    )

    def __init__(self, *, verified: bool = True):
        self.current = self.PICKER
        self.calls: list[dict[str, Any]] = []
        self.topics: list[str] = []
        self.captures: list[str] = []
        self.verified = verified

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(body))
        action = body.get("action")
        if action == "create_task":
            self.topics.append(str(body.get("topic") or ""))
            return {"success": True, "data": {"id": f"task-{len(self.topics)}"}}
        if action == "open":
            url = str(body.get("url") or "")
            for page in (self.PICKER, self.VEHICLE, self.QUICK, self.HUB,
                         self.PROGRAMMING, self.AIMING, self.REMOVAL, self.CAMERA):
                if page["url"] == url:
                    self.current = page
                    break
            return {"success": True, "data": self.current}
        if action == "observe":
            return {"success": True, "data": self.current}
        if action == "click":
            ref = str(body.get("ref") or "")
            name = ""
            for element in self.current["elements"]:
                if element["ref"] == ref:
                    name = element["name"]
                    break
            route = {
                "2021 Honda Civic Sedan": self.VEHICLE,
                "ADAS Quick Reference": self.QUICK,
                "Millimeter Wave Radar": self.HUB,
                "Rearview Camera": self.CAMERA,
                "Programming and Relearning": self.PROGRAMMING,
                "Removal and Replacement": self.REMOVAL,
                "Millimeter Wave Radar Aiming": self.AIMING,
            }
            if name in route:
                self.current = route[name]
                return {"success": True, "data": self.current}
            return {"success": False, "error": {"message": f"no route for {name!r}"}}
        if action in {"extract", "scroll", "wait", "press", "fill", "back", "done"}:
            return {"success": True, "data": self.current}
        if action == "verify":
            return {
                "success": True,
                "data": {"verified": self.verified,
                         "reason": None if self.verified else "Relevance score 1 is below 2."},
            }
        raise AssertionError(f"unexpected action {action!r}")


async def _no_sleep(_seconds: float) -> None:
    return None


def _service(navigator: _FakeNavigator, captures: list[str]) -> harvest.AdasSiHarvestService:
    async def capture(task_id: str) -> dict[str, Any]:
        captures.append(task_id)
        return {
            "status": "captured",
            "data": {"relative_path": f"2021/Honda/Civic Sedan/doc-{len(captures)}.pdf",
                     "sha256": "f" * 64},
        }

    return harvest.AdasSiHarvestService(
        settings=object(), navigator=navigator, capture=capture, sleep=_no_sleep
    )


def test_alldata_files_suvs_under_make_truck():
    """A Palisade is never a Hyundai; ALLDATA calls it a Hyundai Truck."""
    assert harvest.vehicle_target("2021 Hyundai Truck Palisade AWD (LX2) V6-3.8L") == {
        "year": 2021, "make": "Hyundai Truck", "model": "Palisade AWD",
    }
    assert harvest.vehicle_target("2021 Honda Civic Sedan L4-2.0L (K20C2)") == {
        "year": 2021, "make": "Honda", "model": "Civic Sedan",
    }


def test_a_single_word_model_does_not_swallow_the_engine_code():
    """"Niro L4-1.6L" is not a model, and ALLDATA will not confirm it.

    A fixed two-word model works for "Civic Sedan" and "CTS Sedan" and fails
    for every one-word model. In the first ten-vehicle batch the Kia Niro and
    the Ford Mustang each found seven documents and filed none, both refused
    with "ALLDATA vehicle selection was not confirmed".
    """
    assert harvest.vehicle_target("2022 Kia Niro L4-1.6L Hybrid") == {
        "year": 2022, "make": "Kia", "model": "Niro",
    }
    assert harvest.vehicle_target("2025 Ford Mustang L4-2.3L Turbo") == {
        "year": 2025, "make": "Ford", "model": "Mustang",
    }
    # A drive layout is part of the model, not an engine code.
    assert harvest.vehicle_target("2024 Nissan-Datsun Truck Kicks FWD L4-1.6L (HR16DE)") == {
        "year": 2024, "make": "Nissan-Datsun Truck", "model": "Kicks FWD",
    }


def test_quick_reference_rows_stop_at_related_information():
    """The ADAS rows sit above a block of site-wide links."""
    names = [name for _ref, name in harvest.page_links(_FakeNavigator.QUICK, stop_at_footer=True)]
    assert names == ["Millimeter Wave Radar", "Rearview Camera"]


def test_a_component_hub_is_not_read_as_a_footer():
    """"Diagrams" and "Parts and Labor" are sections on a hub, not the end of it.

    Applying the Quick Reference's footer rule here stopped the walk before
    "Programming and Relearning" -- which is where the calibration lives -- and
    every component came back with nothing.
    """
    names = [name for _ref, name in harvest.page_links(_FakeNavigator.HUB)]
    assert "Programming and Relearning" in names
    assert "Removal and Replacement" in names


@pytest.mark.asyncio
async def test_harvest_descends_to_the_leaf_and_files_it():
    navigator = _FakeNavigator()
    captures: list[str] = []
    service = _service(navigator, captures)

    result = await service.start({"vehicles": ["2021 Honda Civic"]})
    assert result["executed"] is True
    await service._tasks[result["run_id"]]

    status = await service.status({"run_id": result["run_id"]})
    assert status["state"] == "finished"
    assert status["filed"] == status["vehicles"][0]["found"] > 0
    assert status["vehicles"][0]["vehicle"] == "2021 Honda Civic Sedan L4-2.0L (K20C2)"

    titles = [
        document["title"]
        for entry in service._runs[result["run_id"]]["vehicles"]
        for document in entry["documents"]
    ]
    # The aiming procedure sits three levels below its component; reaching it
    # is the whole point of descending rather than assuming a fixed depth.
    assert any("Millimeter Wave Radar Aiming" in title for title in titles)


@pytest.mark.asyncio
async def test_each_document_is_captured_under_its_own_topic():
    """Verification scores a capture against its task's topic.

    A single task with a generic topic failed every gate live -- "Millimeter
    Wave Radar Aiming" scores 1 against "adas calibration" and is refused --
    so each document gets a task named after itself.
    """
    navigator = _FakeNavigator()
    captures: list[str] = []
    service = _service(navigator, captures)
    result = await service.start({"vehicles": ["2021 Honda Civic"]})
    await service._tasks[result["run_id"]]

    document_topics = [topic for topic in navigator.topics if topic != "adas quick reference"]
    assert document_topics, navigator.topics
    assert any("Millimeter Wave Radar Aiming" in topic for topic in document_topics)
    assert len(set(document_topics)) == len(document_topics)


@pytest.mark.asyncio
async def test_an_unverified_document_is_not_filed():
    navigator = _FakeNavigator(verified=False)
    captures: list[str] = []
    service = _service(navigator, captures)
    result = await service.start({"vehicles": ["2021 Honda Civic"]})
    await service._tasks[result["run_id"]]

    status = await service.status({"run_id": result["run_id"]})
    assert status["filed"] == 0
    assert captures == []


@pytest.mark.asyncio
async def test_start_requires_vehicles_and_refuses_a_second_run():
    navigator = _FakeNavigator()
    service = _service(navigator, [])
    empty = await service.start({})
    assert empty["executed"] is False
    assert "vehicles is required" in empty["reason"]

    too_many = await service.start({"vehicles": [f"2021 Car {n}" for n in range(30)]})
    assert too_many["executed"] is False
    assert "At most" in too_many["reason"]


@pytest.mark.asyncio
async def test_status_reports_a_vehicle_missing_from_recent_vehicles():
    navigator = _FakeNavigator()
    service = _service(navigator, [])
    result = await service.start({"vehicles": ["1998 Something Unknown"]})
    await service._tasks[result["run_id"]]

    status = await service.status({"run_id": result["run_id"]})
    assert status["vehicles"][0]["error"] == "not in ALLDATA Recent Vehicles"
    assert status["filed"] == 0
