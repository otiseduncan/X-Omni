"""Background service-information research: scheduling without deciding.

The job reads exact identities from Calibration IQ, hands each requirement
to the model-driven Navigator, attaches what the independent review accepted
through the operator path, and reports per objective from Calibration IQ's
own reread. Fakes stand in for Calibration IQ, the Navigator, and the
attachment path; nothing here touches a browser.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.orchestrator.loop import artifacts_for_result
from core.services import adas_si_research as research_mod
from core.services.adas_si_research import (
    AdasSiResearchService,
    classify_objective,
    objectives_for,
    target_from_read,
)
from core.state.db import Store
from core.tools import meta
from core.tools.registry import Registry


def _ro_read(ro_number: str = "2400711902", *, calibrations: list[dict[str, Any]] | None = None, vin: str = "KNAF24A28S5000001") -> dict[str, Any]:
    return {
        "status": "verified",
        "repair_order": {"RO": ro_number, "id": f"id-{ro_number}", "Phase": 1, "Shop": "Warner Robins"},
        "raw": {
            "id": f"id-{ro_number}",
            "ro_number": ro_number,
            "vin": vin,
            "vehicle": {"year": 2025, "make": "Kia", "model": "K4 LX FWD"},
            "calibrations": calibrations
            if calibrations is not None
            else [
                {"id": "cal-radar", "title": "Front Radar Sensor - SCC / AEB / FCW", "determination": "REQUIRED"},
                {"id": "cal-done", "title": "Rear Camera", "determination": "NOT_REQUIRED"},
            ],
        },
    }


async def _no_sleep(_seconds: float) -> None:
    return None


class _FakeNavigator:
    def __init__(self, outcomes: dict[str, dict[str, Any]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.outcomes = outcomes or {}

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        topic = kwargs["topic"]
        default = {
            "status": "verified", "verified": True, "complete": True, "captured": True,
            "evidence_title": "Front Radar (ADAS) - Adjustment", "source_url": "https://my.alldata.com/a",
            "semantic_review": {"classification": "ACTUAL_PROCEDURE", "decision": "ACCEPT", "confidence": 0.9, "evidence_summary": "aiming steps"},
            "documents": [{"role": "primary", "title": "Front Radar (ADAS) - Adjustment", "url": "https://my.alldata.com/a", "accepted": True, "captured": True, "artifact": {"relative_path": "2025/Kia/K4/Front Radar (ADAS) - Adjustment ALLDATA 20260913-1.pdf", "sha256": "f" * 64, "title": "Front Radar (ADAS) - Adjustment"}, "task_id": "t1"}],
            "dependencies": [], "incomplete_reasons": [], "task_ids": ["t1"],
            "research_receipt": {"task_ids": ["t1"], "visited_urls": ["https://my.alldata.com/a"], "candidates": [], "critic_decisions": [{"decision": "ACCEPT"}], "dependencies": [], "stale_action_rejections": 0, "artifacts": [{"sha256": "f" * 64}], "final_status": "verified", "incomplete_reasons": [], "metrics": {"model_calls": 5}, "actions": [1, 2, 3], "observation_ids": ["obs_1"]},
        }
        for key, outcome in self.outcomes.items():
            if key in topic:
                return outcome
        return default


class _FakeAttach:
    def __init__(self, attached: bool = True):
        self.attached = attached
        self.calls: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, objective, document, context):
        self.calls.append((objective, document, context))
        return {"attached": self.attached, "status": "attached" if self.attached else "not_confirmed", "document_id": "doc-1", "document_status": "validated"}


def _service(tmp_path: Path, *, navigator=None, attach=None, reads=None, board=None):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation("research")
    message_id = store.add_message(conversation_id, "user", "research the k4")
    posted: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    async def ro_reader(args):
        key = str(args.get("repair_order_id"))
        if reads is not None:
            return reads.get(key) or {"status": "not_found", "message": f"no RO {key}"}
        return _ro_read(key if key.startswith("24") else "2400711902")

    async def board_reader(args):
        return board or {"status": "verified", "rows": []}

    async def notify(user_id, title, body):
        posted.append({"user_id": user_id, "title": title, "body": body})

    service = AdasSiResearchService(
        SimpleNamespace(),
        store,
        client=object(),
        router=SimpleNamespace(supports_vision=lambda: True),
        navigator_search=navigator or _FakeNavigator(),
        ro_reader=ro_reader,
        board_reader=board_reader,
        attach=attach or _FakeAttach(),
        notify=notify,
        publish=lambda event, user_id=None: published.append({**event, "user_id": user_id}),
        sleep=_no_sleep,
    )
    context = {"conversation_id": conversation_id, "message_id": message_id, "tool_call_id": "call-1", "user_id": "local-dev", "role": "owner"}
    return service, store, context, posted, published


async def _finish(service: AdasSiResearchService) -> None:
    for task in list(service._tasks.values()):  # noqa: SLF001
        await task


# ---------------------------------------------------------- identity


def test_target_identity_comes_from_calibration_iq_only():
    target = target_from_read(_ro_read())
    assert target["ro_number"] == "2400711902"
    assert target["repair_order_id"] == "id-2400711902"
    assert target["vehicle"] == {"year": 2025, "make": "Kia", "model": "K4 LX FWD"}
    assert target["vin"] == "KNAF24A28S5000001"
    assert [item["title"] for item in target["calibrations"]] == ["Front Radar Sensor - SCC / AEB / FCW"]
    assert target_from_read({"status": "not_found"}) is None
    assert research_mod.normalize_make("VW") == "Volkswagen"
    assert research_mod.normalize_make("BENZ") == "Mercedes-Benz"
    assert research_mod.normalize_model("Santa Fe Limited") == "Santa Fe Limited"
    assert research_mod.normalize_model("Santa Fe Limited", "Limited") == "Santa Fe"
    assert research_mod.normalize_model("Grand Cherokee L", "Limited") == "Grand Cherokee L"


def test_target_preserves_multiword_model_and_separate_trim():
    read = _ro_read()
    read["raw"]["vehicle"].update({"model": "Santa Fe Limited", "trim": "Limited"})
    target = target_from_read(read)
    assert target["vehicle"] == {
        "year": 2025,
        "make": "Kia",
        "model": "Santa Fe",
        "trim": "Limited",
    }
    assert target["vehicle_label"] == "2025 Kia Santa Fe Limited"


def test_objectives_are_one_per_requirement_and_carry_no_navigation_decisions():
    target = target_from_read(_ro_read())
    objectives = objectives_for(target)
    assert len(objectives) == 1
    objective = objectives[0]
    assert objective["calibration_id"] == "cal-radar"
    assert objective["calibration_title"] == "Front Radar Sensor - SCC / AEB / FCW"
    assert objective["vin"] == "KNAF24A28S5000001"
    assert objective["topic"].startswith("Front Radar Sensor - SCC / AEB / FCW")
    # No URL, branch, menu, or keyword appears in an objective: X decides those.
    assert not any(key in objective for key in ("url", "branch", "route", "keywords", "path"))
    named = objectives_for(target, systems=["Blind Spot Monitor"])
    assert [item["calibration_title"] for item in named] == ["Blind Spot Monitor"]
    assert named[0]["calibration_id"] is None


# --------------------------------------------------------------- job


@pytest.mark.asyncio
async def test_job_researches_each_requirement_attaches_accepted_documents_and_reports(tmp_path):
    navigator = _FakeNavigator()
    attach = _FakeAttach()
    service, store, context, posted, published = _service(tmp_path, navigator=navigator, attach=attach)

    started = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    assert started["status"] == "running" and started["executed"] is True
    assert started["objective_count"] == 1
    await _finish(service)

    status = await service.status({research_mod.INVOCATION_KEY: context})
    assert status["status"] == "completed"
    assert status["attached_count"] == 1
    row = status["objectives"][0]
    assert row["outcome"] == "attached"
    assert row["title"] == "Front Radar (ADAS) - Adjustment"
    assert row["review"]["decision"] == "ACCEPT"
    assert row["attachments"][0]["attached"] is True

    # The Navigator was given the exact identity and the requirement, nothing more.
    call = navigator.calls[0]
    assert call["target"] == {
        "year": 2025,
        "make": "Kia",
        "model": "K4 LX FWD",
        "vin": "KNAF24A28S5000001",
    }
    assert call["capture"] is True
    assert call["objective"]["repair_order"] == "2400711902"
    assert call["objective"]["calibration_item_id"] == "cal-radar"
    assert call["provider"] == "alldata"

    # Attachment bound the document to the calibration item under the job's identity.
    objective, document, attach_context = attach.calls[0]
    assert objective["calibration_id"] == "cal-radar"
    assert document["artifact"]["sha256"] == "f" * 64
    assert attach_context["conversation_id"] == context["conversation_id"]
    assert attach_context["tool_call_id"] == "call-1"

    # Result posted to the conversation, pushed, and published.
    messages = store.get_messages(context["conversation_id"])
    assert messages[-1]["artifacts"][0]["type"] == "adas_si_research"
    assert posted and "finished" in posted[0]["title"]
    assert published[0]["type"] == "conversation_updated"
    # The receipt is kept with the job for diagnosis without logs.
    record = store.get_record(research_mod.NAMESPACE, started["job_id"], user_id="local-dev")
    assert record["objectives"][0]["result"]["receipt"]["final_status"] == "verified"
    assert record["objectives"][0]["result"]["receipt"]["action_count"] == 3
    store.close()


@pytest.mark.asyncio
async def test_unaccepted_incomplete_and_unattached_outcomes_are_reported_truthfully(tmp_path):
    reads = {
        "2400711902": _ro_read("2400711902", calibrations=[
            {"id": "c1", "title": "Front Radar", "determination": "REQUIRED"},
            {"id": "c2", "title": "Blind Spot Monitor", "determination": "REQUIRED"},
            {"id": "c3", "title": "Steering Angle Sensor", "determination": "REQUIRED"},
            {"id": "c4", "title": "Occupant Classification", "determination": "REQUIRED"},
        ]),
    }
    navigator = _FakeNavigator({
        "Blind Spot": {"status": "unverified", "verified": False, "captured": False, "reason": "Semantic review did not accept a candidate: removal only.", "documents": [], "dependencies": [], "incomplete_reasons": ["semantic review did not accept"], "task_ids": ["t2"]},
        "Steering": {"status": "incomplete", "verified": True, "complete": False, "captured": True, "evidence_title": "SAS Calibration", "source_url": "https://my.alldata.com/s", "semantic_review": {"decision": "ACCEPT_WITH_DEPENDENCIES", "classification": "ACTUAL_PROCEDURE", "confidence": 0.8}, "documents": [{"role": "primary", "title": "SAS Calibration", "url": "https://my.alldata.com/s", "accepted": True, "captured": True, "artifact": {"relative_path": "2025/Kia/K4/SAS.pdf", "sha256": "e" * 64}, "task_id": "t3"}], "dependencies": [{"title": "Wheel Alignment", "reason": "needed first", "status": "unresolved"}], "incomplete_reasons": ["dependency 'Wheel Alignment' unresolved: not found"], "task_ids": ["t3", "t4"]},
        "Occupant": {"status": "authentication_required", "verified": False, "requires_human": True, "reason": "sign in", "documents": [], "dependencies": [], "incomplete_reasons": [], "task_ids": []},
    })
    service, store, context, _posted, _published = _service(tmp_path, navigator=navigator, reads=reads)
    started = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    assert started["objective_count"] == 4
    await _finish(service)
    status = await service.status({research_mod.INVOCATION_KEY: context})
    outcomes = {row["calibration"]: row["outcome"] for row in status["objectives"]}
    assert outcomes == {
        "Front Radar": "attached",
        "Blind Spot Monitor": "not_found",
        "Steering Angle Sensor": "incomplete",
        "Occupant Classification": "blocked",
    }
    steering = next(row for row in status["objectives"] if row["calibration"] == "Steering Angle Sensor")
    assert steering["dependencies"][0]["status"] == "unresolved"
    assert "Wheel Alignment" in " ".join(steering["incomplete_reasons"])
    assert status["counts"] == {"attached": 1, "not_found": 1, "incomplete": 1, "blocked": 1}
    assert "1 of 4 procedure(s) filed and attached" in status["message"]
    store.close()


@pytest.mark.asyncio
async def test_attachment_not_confirmed_by_calibration_iq_is_never_reported_attached(tmp_path):
    service, store, context, _posted, _published = _service(tmp_path, attach=_FakeAttach(attached=False))
    started = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    await _finish(service)
    status = await service.status({research_mod.INVOCATION_KEY: context})
    assert status["objectives"][0]["outcome"] == "captured_not_attached"
    assert status["attached_count"] == 0
    del started
    store.close()


@pytest.mark.asyncio
async def test_start_needs_a_conversation_targets_and_requirements(tmp_path):
    service, store, context, _posted, _published = _service(tmp_path, reads={"2400711902": _ro_read(calibrations=[])})
    missing_context = await service.start({"repair_order_id": "2400711902"})
    assert missing_context["status"] == "context_missing" and missing_context["executed"] is False
    no_requirements = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    assert no_requirements["status"] == "no_requirements"
    assert "Attach the ADAS Map first" in no_requirements["message"]
    unknown = await service.start({"repair_order_id": "2400700000", research_mod.INVOCATION_KEY: context})
    assert unknown["status"] == "no_targets"
    store.close()


@pytest.mark.asyncio
async def test_phase_scope_takes_targets_from_the_board_and_a_second_start_reports_the_running_job(tmp_path):
    board = {"status": "verified", "rows": [{"id": "id-2400711902", "RO": "2400711902"}, {"id": "id-2400711902", "RO": "2400711902"}]}

    class _SlowNavigator(_FakeNavigator):
        async def __call__(self, **kwargs):
            await asyncio.sleep(0.05)
            return await super().__call__(**kwargs)

    reads = {"id-2400711902": _ro_read("2400711902")}
    service, store, context, _posted, _published = _service(tmp_path, navigator=_SlowNavigator(), reads=reads, board=board)
    started = await service.start({"phases": ["1"], "shop": "Warner Robins", research_mod.INVOCATION_KEY: context})
    assert started["status"] == "running"
    assert started["scope"] == "phase 1 in Warner Robins"
    assert started["target_count"] == 1
    again = await service.start({"phases": ["1"], research_mod.INVOCATION_KEY: context})
    assert again["status"] == "already_running" and again["executed"] is False
    await _finish(service)
    store.close()


@pytest.mark.asyncio
async def test_unfinished_job_resumes_after_a_core_restart(tmp_path):
    service, store, context, _posted, _published = _service(tmp_path)
    started = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    await service.shutdown()
    record = store.get_record(research_mod.NAMESPACE, started["job_id"], user_id="local-dev")
    record["objectives"][0]["status"] = "pending"
    record["state"] = "running"
    store.put_record(research_mod.NAMESPACE, started["job_id"], record, user_id="local-dev")

    restarted, _store2, _context, _p, _pub = _service(tmp_path)
    restarted.store = store
    resumed = await restarted.resume()
    assert resumed == 1
    await _finish(restarted)
    final = store.get_record(research_mod.NAMESPACE, started["job_id"], user_id="local-dev")
    assert final["state"] == "completed"
    store.close()


@pytest.mark.asyncio
async def test_model_unavailable_is_recorded_not_hidden(tmp_path):
    service, store, context, _posted, _published = _service(tmp_path)
    service.router = SimpleNamespace(supports_vision=lambda: False)
    started = await service.start({"repair_order_id": "2400711902", research_mod.INVOCATION_KEY: context})
    await _finish(service)
    status = await service.status({research_mod.INVOCATION_KEY: context})
    assert status["objectives"][0]["outcome"] == "model_unavailable"
    del started
    store.close()


def test_outcome_classification_lets_calibration_iq_decide_attachment():
    verified = {"status": "finished", "result": {"verified": True, "complete": True, "captured": True}, "attachments": [{"attached": True}]}
    assert classify_objective(verified) == "attached"
    unconfirmed = {"status": "finished", "result": {"verified": True, "complete": True, "captured": True}, "attachments": [{"attached": False}]}
    assert classify_objective(unconfirmed) == "captured_not_attached"
    uncertain = {"status": "finished", "result": {"verified": False, "status": "uncertain"}}
    assert classify_objective(uncertain) == "uncertain"
    assert classify_objective({"status": "pending"}) == "not_run"


# ------------------------------------------------------ gateway wiring


@pytest.mark.asyncio
async def test_stage_action_research_si_invokes_the_job_with_core_owned_context(tmp_path):
    store = Store(tmp_path / "registry.sqlite")
    conversation_id = store.create_conversation("research")
    message_id = store.add_message(conversation_id, "user", "get the procedures for 11902")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    seen: list[dict[str, Any]] = []

    async def fake_start(args):
        seen.append(dict(args))
        return {"status": "running", "executed": True, "success": True, "job_id": "j1"}

    async def fake_status(args):
        seen.append(dict(args))
        return {"status": "running", "success": True}

    registry.register("adas_si_research", fake_start)
    registry.register("adas_si_research_status", fake_status)
    context = dict(message_id=message_id, conversation_id=conversation_id, tool_call_id="call-9", user_id="local-dev", role="owner")
    result = await registry.invoke(
        "stage_action",
        {"operation": "research_si", "repair_order_id": "11902", "shop": "Warner Robins", "arguments": {"systems": ["Front Radar"]}},
        **context,
    )
    assert result["stage"] == "started" and result["executed_via"] == "adas_si_research"
    assert seen[0]["repair_order_id"] == "11902" and seen[0]["shop"] == "Warner Robins"
    assert seen[0]["systems"] == ["Front Radar"]
    assert seen[0]["__xomni_invocation"]["conversation_id"] == conversation_id
    assert [card for card, _ in artifacts_for_result("stage_action", result)] == ["adas_si_research"]
    await registry.invoke("query_ciq", {"kind": "adas_si_research"}, **context)
    assert seen[1]["__xomni_invocation"]["user_id"] == "local-dev"
    assert meta.expand_query_ciq({"kind": "adas_si_research"}) == ("adas_si_research_status", {})
    assert "research_si" in meta.stage_action_schema()["parameters"]["properties"]["operation"]["enum"]
    store.close()


def test_context_line_reports_running_and_recent_research_only():
    running = {"state": "running", "scope_label": "RO 2400711902", "started_at": "2026-09-13T10:00:00+00:00", "objectives": [{"status": "finished", "outcome": "attached"}, {"status": "pending"}]}
    line = research_mod.context_line(running)
    assert "still running" in line and "1 of 2" in line and "query_ciq kind=adas_si_research" in line
    assert "query_ciq" not in research_mod.context_line(running, for_model=False)
    finished = {"state": "completed", "scope_label": "RO 2400711902", "finished_at": research_mod._iso(research_mod._now()), "targets": [{}], "objectives": [{"status": "finished", "outcome": "attached"}]}
    assert "1 of 1 procedure(s) filed and attached" in research_mod.context_line(finished)
    stale = dict(finished, finished_at="2020-01-01T00:00:00+00:00")
    assert research_mod.context_line(stale) is None
