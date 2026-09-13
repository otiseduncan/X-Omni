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


def test_filing_follows_the_library_not_alldata_taxonomy():
    """ADAS SI files year/plain-make/short-model; ALLDATA prints neither.

    The library already holds 2020/Honda/Civic, 2026/Honda/CR-V,
    2025/Kia/Carnival, 2016/Nissan/Rogue. ALLDATA prints "2025 Honda Truck
    CR-V 2WD L4-1.5L Turbo", and filing that verbatim scatters one vehicle
    across "Honda Truck/CR-V 2WD" and "Honda/CR-V" -- which is how a document
    stops being findable.
    """
    assert harvest.vehicle_target("2025 Honda Truck CR-V 2WD L4-1.5L Turbo (L15BE)") == {
        "year": 2025, "make": "Honda", "model": "CR-V",
    }
    assert harvest.vehicle_target("2021 Hyundai Truck Palisade AWD (LX2) V6-3.8L") == {
        "year": 2021, "make": "Hyundai", "model": "Palisade",
    }
    # ALLDATA's own spelling of the make is not the library's.
    assert harvest.vehicle_target("2024 Nissan-Datsun Truck Kicks FWD L4-1.6L (HR16DE)") == {
        "year": 2024, "make": "Nissan", "model": "Kicks",
    }
    assert harvest.vehicle_target("2020 Mercedes Benz E 350 4MATIC Sedan (213.084)") == {
        "year": 2020, "make": "Mercedes-Benz", "model": "E 350",
    }


def test_a_single_word_model_does_not_swallow_the_engine_code():
    """"Niro L4-1.6L" is not a model, and ALLDATA will not confirm it.

    In the first ten-vehicle batch the Kia Niro and the Ford Mustang each
    found seven documents and filed none, both refused with "ALLDATA vehicle
    selection was not confirmed".
    """
    assert harvest.vehicle_target("2022 Kia Niro L4-1.6L Hybrid") == {
        "year": 2022, "make": "Kia", "model": "Niro",
    }
    assert harvest.vehicle_target("2025 Ford Mustang L4-2.3L Turbo") == {
        "year": 2025, "make": "Ford", "model": "Mustang",
    }


def test_two_word_models_survive():
    """Trimming trim must not trim the model: ES 350 and F-150 are names."""
    assert harvest.vehicle_target("2019 Lexus ES 350")["model"] == "ES 350"
    assert harvest.vehicle_target("2019 Ford F-150")["model"] == "F-150"


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
    assert "vehicles or phases is required" in empty["reason"]

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
    assert status["vehicles"][0]["error"] == "ALLDATA does not list this vehicle"
    assert status["filed"] == 0


class _CascadeNavigator(_FakeNavigator):
    """A picker that behaves like ALLDATA's: three dependent comboboxes.

    The Palisade is only reachable under "Hyundai Truck"; under plain
    "Hyundai" the model list holds cars alone, exactly as the live site does.
    """

    CARS = ["Accent", "Elantra (CN7) VIN KMH", "IONIQ", "Sonata (DN8)"]
    TRUCKS = ["Palisade", "Santa Fe", "Tucson"]

    def __init__(self):
        super().__init__()
        self.year = None
        self.make = None
        self.model = None
        self.open_list = None
        self.picked: list[str] = []

    def _picker(self):
        boxes = [
            _element("cb-year", self.year or "Year", "combobox"),
            _element("cb-make", self.make or "Make", "combobox"),
            _element("cb-model", self.model or "Model", "combobox"),
        ]
        options = []
        if self.open_list == "year":
            options = [_element(f"opt-{y}", str(y), "option") for y in (2023, 2022, 2021)]
        elif self.open_list == "make":
            options = [
                _element("opt-hyundai", "Hyundai", "option"),
                _element("opt-hyundai-truck", "Hyundai Truck", "option"),
            ]
        elif self.open_list == "model":
            names = self.TRUCKS if self.make == "Hyundai Truck" else self.CARS
            options = [_element(f"opt-{n}", n, "option") for n in names]
        return {
            "url": harvest.PICKER_URL, "title": "ALLDATA Collision - Home",
            "page_text": "Select Vehicle", "elements": boxes + options,
        }

    VEHICLE = _page(
        "https://my.alldata.com/repair/#/vehicle/62400",
        "Vehicle Information - 2021 Hyundai Truck Palisade AWD (LX2) V6-3.8L - ALLDATA Collision",
        ["ADAS Quick Reference"],
    )

    async def __call__(self, body):
        action = body.get("action")
        if action == "create_task":
            self.topics.append(str(body.get("topic") or ""))
            return {"success": True, "data": {"id": f"task-{len(self.topics)}"}}
        if action == "open" and str(body.get("url")) == harvest.PICKER_URL:
            self.year = self.make = self.model = None
            self.open_list = None
            self.current = self._picker()
            return {"success": True, "data": self.current}
        if action == "observe":
            return {"success": True, "data": self.current}
        if action == "click":
            ref = str(body.get("ref") or "")
            if ref.startswith("cb-"):
                which = ref.split("-", 1)[1]
                self.open_list = None if self.open_list == which else which
                self.current = self._picker()
                return {"success": True, "data": self.current}
            if ref.startswith("opt-"):
                value = ref[4:]
                if self.open_list == "year":
                    self.year = value
                elif self.open_list == "make":
                    self.make = "Hyundai Truck" if value == "hyundai-truck" else "Hyundai"
                elif self.open_list == "model":
                    self.model = value
                    self.picked.append(value)
                    if self.make == "Hyundai Truck":
                        self.current = self.VEHICLE
                        self.open_list = None
                        return {"success": True, "data": self.current}
                self.open_list = None
                self.current = self._picker()
                return {"success": True, "data": self.current}
            return await super().__call__(body)
        return await super().__call__(body)


@pytest.mark.asyncio
async def test_the_cascade_finds_a_vehicle_recent_vehicles_never_saw():
    navigator = _CascadeNavigator()
    service = _service(navigator, [])
    chosen = await service.select_vehicle(
        "task-1", {"year": 2021, "make": "Hyundai", "model": "Palisade"}
    )
    assert chosen["selected"] is True
    assert chosen["vehicle"].startswith("2021 Hyundai Truck Palisade")


@pytest.mark.asyncio
async def test_the_make_list_answers_the_taxonomy_question():
    """A Palisade is not under "Hyundai"; the picker itself says where it is.

    Asking for it under the plain make returns a list of cars. Rather than
    guessing at "<Make> Truck", the cascade tries the other make the list
    actually offers and reads its models.
    """
    navigator = _CascadeNavigator()
    service = _service(navigator, [])
    chosen = await service.select_vehicle(
        "task-1", {"year": 2021, "make": "Hyundai", "model": "Palisade"}
    )
    assert chosen["make_used"] == "Hyundai Truck"
    # It must not have settled for a car that merely contains the letters.
    assert navigator.picked == ["Palisade"]


@pytest.mark.asyncio
async def test_a_vehicle_on_no_shelf_is_reported_not_guessed():
    navigator = _CascadeNavigator()
    service = _service(navigator, [])
    chosen = await service.select_vehicle(
        "task-1", {"year": 2021, "make": "Hyundai", "model": "Veloster N"}
    )
    assert chosen["selected"] is False
    assert "Hyundai Truck" in chosen["reason"]


def test_option_matching_prefers_an_anchored_name():
    """"Accord" must not match "Accord Coupe" ahead of "Accord Sedan"...

    ...and must never match something that merely contains the word. ALLDATA
    lists "Elantra (CN7) VIN KMH" beside "Elantra (CN7A) VIN 5NP", so a loose
    match picks a different car.
    """
    best = harvest.AdasSiHarvestService._best_option
    options = [("a", "Accord Coupe"), ("b", "Accord"), ("c", "Accord Sedan")]
    assert best(options, "Accord") == ("b", "Accord")
    assert best([("a", "Accord Coupe"), ("c", "Accord Sedan")], "Accord Sedan") == (
        "c", "Accord Sedan",
    )
    assert best([("a", "Elantra (CN7) VIN KMH")], "Sonata") is None


class _PhaseNavigator(_CascadeNavigator):
    """Cascade picker plus a Calibration IQ board behind it."""


@pytest.mark.asyncio
async def test_phases_take_their_vehicles_from_the_board(monkeypatch):
    """One capture serves every RO on the same vehicle.

    Calibration IQ names a vehicle with its trim attached and truncated by the
    board's column -- "2023 Honda Accord Sedan EX w/Continuously Variable
    Transmissi" -- and several ROs share one vehicle.
    """
    rows = {
        "1": [
            {"RO": "111", "Vehicle": "2021 Hyundai Palisade SEL w/Convenience Pkg"},
            {"RO": "222", "Vehicle": "2021 Hyundai Palisade Limited AWD"},
        ],
        "3": [{"RO": "333", "Vehicle": "2023 Honda Accord Sedan EX w/Continuously Variable"}],
    }

    async def fake_read(_settings, filters):
        return {"status": "verified", "rows": rows.get(str(filters.get("phase")), [])}

    from core.services import calibration_iq
    monkeypatch.setattr(calibration_iq, "read_repair_orders", fake_read)

    service = _service(_PhaseNavigator(), [])
    found = await service.vehicles_in_phases(["1", "3"])
    labels = sorted(item["label"] for item in found)
    assert labels == ["2021 Hyundai Palisade", "2023 Honda Accord"]
    palisade = next(i for i in found if i["label"] == "2021 Hyundai Palisade")
    # Two ROs, one vehicle, one capture -- and the ROs it serves are recorded.
    assert palisade["repair_orders"] == ["111", "222"]


@pytest.mark.asyncio
async def test_a_phase_sweep_reports_which_repair_orders_it_served(monkeypatch):
    async def fake_read(_settings, filters):
        if str(filters.get("phase")) != "1":
            return {"status": "verified", "rows": []}
        return {"status": "verified",
                "rows": [{"RO": "111", "Vehicle": "2021 Hyundai Palisade SEL"}]}

    from core.services import calibration_iq
    monkeypatch.setattr(calibration_iq, "read_repair_orders", fake_read)

    navigator = _PhaseNavigator()
    service = _service(navigator, [])
    started = await service.start({"phases": ["1"]})
    assert started["executed"] is True
    await service._tasks[started["run_id"]]

    status = await service.status({"run_id": started["run_id"]})
    assert status["phases"] == ["1"]
    assert status["vehicles_from_phases"] == 1
    assert status["vehicles"][0]["repair_orders"] == ["111"]


@pytest.mark.asyncio
async def test_vehicles_and_phases_are_not_both_accepted():
    service = _service(_CascadeNavigator(), [])
    both = await service.start({"vehicles": ["2021 Honda Civic"], "phases": ["1"]})
    assert both["executed"] is False
    assert "not both" in both["reason"]


class _KiaNavigator(_FakeNavigator):
    """A 2025 Kia K4's radar branch, at the sizes the live site returned.

    The hub is small and the calibration below it is large, and the
    calibration closes with a cross-reference back to Removal and Replacement
    -- which is what made every real procedure look like an index.
    """

    HUB = _page(
        "https://my.alldata.com/repair/#/vehicle/1/component/radar",
        "Collision Avoidance Sensor - ALLDATA Collision",
        ["Front Radar (ADAS) - Adjustment", "Removal and Replacement"],
        text="h" * 540,
    )
    ADJUSTMENT = _page(
        "https://my.alldata.com/repair/#/article/1/radar/adjustment",
        "Front Radar (ADAS) - Adjustment (Collision Avoidance Sensor) - ALLDATA Collision",
        ["Removal and Replacement"],          # the cross-reference at its foot
        text="Install the reflector. " * 260,  # ~5,700 characters
    )
    REMOVAL = _page(
        "https://my.alldata.com/repair/#/article/1/radar/removal",
        "Removal and Replacement (Collision Avoidance Sensor) - ALLDATA Collision",
        [], text="r" * 900,
    )

    async def __call__(self, body):
        action = body.get("action")
        if action == "click":
            ref = str(body.get("ref") or "")
            name = ""
            for element in self.current["elements"]:
                if element["ref"] == ref:
                    name = element["name"]
                    break
            route = {
                "Front Radar (ADAS) - Adjustment": self.ADJUSTMENT,
                "Removal and Replacement": self.REMOVAL,
            }
            if name in route:
                self.current = route[name]
                return {"success": True, "data": self.current}
        if action == "open":
            for page in (self.HUB, self.ADJUSTMENT, self.REMOVAL):
                if page["url"] == str(body.get("url")):
                    self.current = page
                    return {"success": True, "data": self.current}
        return await super().__call__(body)


@pytest.mark.asyncio
async def test_a_calibration_that_links_onward_is_still_captured():
    """The failure Otis watched: found the page, then walked past it.

    "Front Radar (ADAS) - Adjustment" carries 5,768 characters of procedure
    and one cross-reference at its foot. Treating any page with links below it
    as an index skipped it, and the same rule skipped "Wide Angle Camera
    (ADAS) - Adjustment" at 8,000 characters and "Front Camera (ADAS) -
    Adjustment" at 4,390.
    """
    navigator = _KiaNavigator()
    navigator.current = _KiaNavigator.HUB
    service = _service(navigator, [])
    found: list = []
    await service.discover("task-1", _KiaNavigator.HUB["url"],
                           "Front Radar (ADAS) - Adjustment", 1, found, set())
    titles = [item["title"] for item in found]
    assert any("Front Radar (ADAS) - Adjustment" in t for t in titles), titles
    # and it still follows the cross-reference
    assert any("Removal and Replacement" in t for t in titles), titles


@pytest.mark.asyncio
async def test_a_small_hub_is_still_only_an_index():
    """540 characters of links is a hub, not a procedure."""
    navigator = _KiaNavigator()
    navigator.current = _KiaNavigator.HUB
    service = _service(navigator, [])
    found: list = []
    await service.discover("task-1", _KiaNavigator.HUB["url"],
                           "Removal and Replacement", 1, found, set())
    assert all("Collision Avoidance Sensor - ALLDATA" not in item["title"] for item in found)

