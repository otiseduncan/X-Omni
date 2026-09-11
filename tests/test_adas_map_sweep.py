"""Background ADAS Map sweep: lifecycle, retry, sign-in, restart, and contracts.

The fake ScrapeX below behaves like ScrapeX's batch worker as read from
X:\\ScrapeX\\scrapex (exact batches of at most ten ROs, a background worker
that marks each item complete or an attention state, "paused" when sign-in
lapses, and start_batch as the idempotent way to resume). Calibration IQ is
faked as an inventory plus a per-RO presence check.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import ROOT, Settings
from core.main import build_app, configured_profile_catalog
from core.orchestrator.loop import artifacts_for_result, fit_messages_to_window
from core.services import adas_map_sweep as sweep_mod
from core.services.adas_map_sweep import (
    AdasMapSweepService,
    classify_outcome,
    summary_sentence,
)
from core.services.live_events import LiveEvents
from core.services.scrapex import adas_map_result_for_model
from core.state.db import Store
from core.tools import meta
from core.tools.registry import Registry


# ------------------------------------------------------------------ fakes


class FakeScrapeX:
    """Per RO, a list of terminal states: one per batch the RO appears in."""

    def __init__(self, outcomes: dict[str, list[str]], *, pause_first_run: bool = False):
        self.outcomes = outcomes
        self.appearances: dict[str, int] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.created: list[list[str]] = []
        self.started: list[str] = []
        self.signed = True
        self.opened = 0
        self.pause_first_run = pause_first_run
        self.final_state: dict[str, str] = {}

    async def ready(self):
        if self.signed:
            return None
        self.opened += 1
        return {
            "status": "authentication_required",
            "authentication_required": True,
            "requires_human": True,
            "message": "ADAS Map sign-in is required in the managed work browser.",
        }

    async def signed_in(self):
        return self.signed

    async def open_sign_in(self):
        self.opened += 1
        return {"success": self.signed, "verified": self.signed}

    async def create_batch(self, name, ro_numbers):
        if not self.signed:
            return {"status": "authentication_required", "success": False}
        assert len(ro_numbers) <= 10, "ScrapeX exact batches accept at most ten ROs"
        batch_id = f"batch{len(self.batches) + 1}"
        states = {}
        for ro in ro_numbers:
            index = self.appearances.get(ro, 0)
            self.appearances[ro] = index + 1
            sequence = self.outcomes.get(ro, ["adas_map_complete"])
            states[ro] = sequence[min(index, len(sequence) - 1)]
        self.batches[batch_id] = {
            "states": states,
            "worker": "created",
            "polls_since_start": 0,
            "pause_pending": self.pause_first_run and not self.batches,
        }
        self.created.append(list(ro_numbers))
        return {"success": True, "verified": True, "status": "queued", "data": {"id": batch_id}}

    async def start_batch(self, batch_id):
        if not self.signed:
            return {"status": "authentication_required", "success": False}
        batch = self.batches[batch_id]
        batch["worker"] = "running"
        batch["polls_since_start"] = 0
        self.started.append(batch_id)
        return {"success": True, "status": "running"}

    async def batch(self, batch_id):
        batch = self.batches[batch_id]
        batch["polls_since_start"] += 1
        if batch["pause_pending"] and batch["worker"] == "running":
            # Sign-in lapsed mid-run: the worker parks the batch as paused.
            batch["pause_pending"] = False
            batch["worker"] = "paused"
            self.signed = False
            return {
                "success": True,
                "batch_state": "paused",
                "batch_error": "ADAS Map is not open/authenticated in managed work Chrome.",
                "items": [
                    {"ro_number": ro, "adas_map_state": "pending", "complete": False, "finished": False}
                    for ro in batch["states"]
                ],
                "finished": False,
            }
        done = batch["worker"] == "running" and batch["polls_since_start"] >= 2
        items = []
        for ro, state in batch["states"].items():
            if done:
                self.final_state[ro] = state
            items.append(
                {
                    "ro_number": ro,
                    "adas_map_state": state if done else "searching_adas_map",
                    "complete": done and state == "adas_map_complete",
                    "finished": done,
                    "adas_map_last_error": (
                        "" if state == "adas_map_complete" else f"ADAS Map lookup returned '{state}'."
                    ),
                }
            )
        if done:
            batch["worker"] = "complete"
        return {
            "success": True,
            "batch_state": (
                "complete"
                if done
                else "running_adas_map"
                if batch["worker"] == "running"
                else batch["worker"]
            ),
            "items": items,
            "finished": done,
        }


def missing_rows(ros: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "repair_order_id": f"id-{ro}",
            "ro_number": ro,
            "vehicle": f"Vehicle {ro[-3:]}",
            "phase": "1",
            "adas_map_status": "not_found",
        }
        for ro in ros
    ]


class FakeCIQ:
    def __init__(self, scrapex: FakeScrapeX, missing: list[str]):
        self.scrapex = scrapex
        self.missing = list(missing)
        self.inventory_calls: list[dict[str, Any]] = []
        self.checked: list[str] = []

    async def inventory(self, scope):
        self.inventory_calls.append(dict(scope))
        if len(self.inventory_calls) == 1:
            rows = missing_rows(self.missing)
        else:
            rows = [
                row
                for row in missing_rows(self.missing)
                if self.scrapex.final_state.get(row["ro_number"]) != "adas_map_complete"
            ]
        return {
            "status": "verified",
            "verified": True,
            "queue_count": 40,
            "adas_map_present_count": 40 - len(rows),
            "adas_map_missing_count": len(rows),
            "adas_map_unverified_count": 0,
            "missing_repair_orders": rows,
        }

    async def ro_map_state(self, identifier):
        self.checked.append(identifier)
        ro = identifier.removeprefix("id-")
        present = self.scrapex.final_state.get(ro) == "adas_map_complete"
        return {"readable": True, "present": present, "status": "verified" if present else "not_found"}


async def no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def make_service(tmp_path: Path, scrapex: FakeScrapeX, ciq: FakeCIQ, **overrides):
    store = Store(tmp_path / "sweep.sqlite")
    conversation_id = store.create_conversation("morning maps")
    notified: list[tuple[str, str, str]] = []
    published: list[tuple[dict, str]] = []

    async def notify(user_id, title, body):
        notified.append((user_id, title, body))

    def publish(event, *, user_id):
        published.append((event, user_id))
        return 1

    service = AdasMapSweepService(
        SimpleNamespace(),
        store,
        scrapex=scrapex,
        inventory=ciq.inventory,
        ro_map_state=ciq.ro_map_state,
        notify=notify,
        publish=publish,
        sleep=overrides.pop("sleep", no_sleep),
        poll_seconds=0.0,
        **overrides,
    )
    context = {
        "conversation_id": conversation_id,
        "message_id": store.add_message(conversation_id, "user", "go get the missing adas maps"),
        "tool_call_id": "call-sweep",
        "user_id": "local-dev",
        "role": "owner",
    }
    return service, store, conversation_id, context, notified, published


async def run_to_completion(service: AdasMapSweepService, sweep_id: str) -> None:
    task = service._tasks.get(sweep_id)  # noqa: SLF001
    assert task is not None, "the sweep must be driven by a background task"
    await asyncio.wait_for(task, timeout=5)


# ------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_sweep_acquires_in_chunks_retries_needs_operator_once_and_reports_from_ciq(tmp_path):
    ros = [f"24007119{index:02d}" for index in range(12)]
    outcomes = {
        ros[1]: ["ro_not_found", "adas_map_complete"],  # never retried: not a retry state
        ros[2]: ["needs_operator", "adas_map_complete"],  # recovered by the retry pass
        ros[3]: ["view_not_found"],
        ros[4]: ["requirements_unparsed"],
        ros[11]: ["needs_operator", "needs_operator"],  # retried once, still stuck
    }
    scrapex = FakeScrapeX(outcomes)
    ciq = FakeCIQ(scrapex, ros)
    service, store, conversation_id, context, notified, published = make_service(tmp_path, scrapex, ciq)

    started = await service.start({"phases": ["1", "2", "3"], "__xomni_invocation": context})

    assert started["status"] == "running"
    assert started["executed"] is True and started["work_complete"] is False
    assert "Nothing is attached yet" in started["message"]
    assert started["target_count"] == 12
    assert started["scope"] == "phases 1-3"
    # The first batch exists and runs before the call returns.
    assert scrapex.created[0] == ros[:10]
    assert scrapex.started == ["batch1"]
    assert ciq.inventory_calls[0] == {"phases": ["1", "2", "3"], "shop": None}

    await run_to_completion(service, started["sweep_id"])

    # Chunks of ten, then one retry batch holding only the needs_operator ROs.
    assert scrapex.created == [ros[:10], ros[10:], [ros[2], ros[11]]]
    record = store.get_record(sweep_mod.NAMESPACE, started["sweep_id"], user_id="local-dev")
    assert record["state"] == "completed"
    by_ro = {target["ro_number"]: target for target in record["targets"]}
    assert by_ro[ros[0]]["outcome"] == "attached"
    assert by_ro[ros[1]]["outcome"] == "not_in_adas_map"
    assert by_ro[ros[2]]["outcome"] == "attached"
    assert by_ro[ros[3]]["outcome"] == "page_would_not_open"
    assert by_ro[ros[4]]["outcome"] == "requirements_unverified"
    assert by_ro[ros[11]]["outcome"] == "needs_a_look"
    assert record["result"]["counts"] == {
        "attached": 8,
        "not_in_adas_map": 1,
        "page_would_not_open": 1,
        "requirements_unverified": 1,
        "needs_a_look": 1,
    }
    assert record["result"]["retried"] == 2
    assert record["result"]["missing_after"] == 4
    # Every swept RO was re-checked in Calibration IQ by its exact id.
    assert sorted(ciq.checked) == sorted(f"id-{ro}" for ro in ros)

    # One result message with the card, a push, and a live event.
    messages = store.get_messages(conversation_id)
    posted = messages[-1]
    assert posted["role"] == "assistant"
    assert posted["content"].startswith("ADAS Map sweep for phases 1-3 is done: 8 of 12 attached.")
    card = posted["artifacts"][0]
    assert card["type"] == "adas_map_sweep"
    assert [group["outcome"] for group in card["data"]["groups"]] == [
        "attached",
        "not_in_adas_map",
        "page_would_not_open",
        "requirements_unverified",
        "needs_a_look",
    ]
    assert notified and notified[-1][1] == "ADAS Map sweep finished"
    assert published[-1][0]["type"] == "conversation_updated"
    assert published[-1][0]["conversation_id"] == conversation_id
    assert record["notified"] is True
    store.close()


@pytest.mark.asyncio
async def test_nothing_missing_starts_nothing(tmp_path):
    scrapex = FakeScrapeX({})
    ciq = FakeCIQ(scrapex, [])
    service, store, _cid, context, _n, _p = make_service(tmp_path, scrapex, ciq)

    result = await service.start({"__xomni_invocation": context})

    assert result["status"] == "nothing_missing"
    assert result["executed"] is False and result["work_complete"] is True
    assert "the active board" in result["message"]
    assert scrapex.created == []
    assert scrapex.opened == 0
    assert store.list_records(sweep_mod.NAMESPACE) == []
    store.close()


@pytest.mark.asyncio
async def test_nothing_missing_never_touches_scrapex_even_when_signed_out(tmp_path):
    scrapex = FakeScrapeX({})
    scrapex.signed = False
    ciq = FakeCIQ(scrapex, [])
    service, store, _cid, context, _n, _p = make_service(tmp_path, scrapex, ciq)

    result = await service.start({"__xomni_invocation": context})

    assert result["status"] == "nothing_missing"
    assert scrapex.opened == 0
    store.close()


@pytest.mark.asyncio
async def test_sign_in_required_at_start_creates_nothing(tmp_path):
    scrapex = FakeScrapeX({})
    scrapex.signed = False
    ciq = FakeCIQ(scrapex, ["2400711905"])
    service, store, _cid, context, _n, _p = make_service(tmp_path, scrapex, ciq)

    result = await service.start({"__xomni_invocation": context})

    assert result["status"] == "authentication_required"
    assert result["executed"] is False
    assert result["authentication_required"] is True
    assert scrapex.created == []
    # The inventory ran first (nothing to sign in for if nothing is missing);
    # with work pending, sign-in blocks before any batch or record exists.
    assert len(ciq.inventory_calls) == 1
    assert store.list_records(sweep_mod.NAMESPACE) == []
    store.close()


@pytest.mark.asyncio
async def test_second_start_while_running_reports_the_running_sweep(tmp_path):
    ros = [f"24007118{index:02d}" for index in range(3)]
    scrapex = FakeScrapeX({})
    ciq = FakeCIQ(scrapex, ros)
    pause = asyncio.Event()

    async def held_sleep(_seconds):
        await pause.wait()

    service, store, _cid, context, _n, _p = make_service(
        tmp_path, scrapex, ciq, sleep=held_sleep
    )
    first = await service.start({"__xomni_invocation": context})
    second = await service.start({"phases": ["5"], "__xomni_invocation": context})

    assert first["status"] == "running"
    assert second["status"] == "already_running"
    assert second["executed"] is False
    assert second["sweep_id"] == first["sweep_id"]
    assert len(scrapex.created) == 1
    pause.set()
    await run_to_completion(service, first["sweep_id"])
    store.close()


@pytest.mark.asyncio
async def test_sign_in_lapse_waits_notifies_once_and_continues_after_sign_in(tmp_path):
    ros = ["2400711899", "2400711896"]
    scrapex = FakeScrapeX({}, pause_first_run=True)
    ciq = FakeCIQ(scrapex, ros)
    sleeps: list[float] = []

    async def sleep_then_sign_in(seconds):
        sleeps.append(seconds)
        if seconds == sweep_mod.SIGN_IN_POLL_SECONDS:
            scrapex.signed = True  # Otis signs in while the sweep waits
        await asyncio.sleep(0)

    service, store, conversation_id, context, notified, _p = make_service(
        tmp_path, scrapex, ciq, sleep=sleep_then_sign_in
    )
    started = await service.start({"__xomni_invocation": context})
    await run_to_completion(service, started["sweep_id"])

    record = store.get_record(sweep_mod.NAMESPACE, started["sweep_id"], user_id="local-dev")
    assert record["state"] == "completed"
    assert record["result"]["counts"] == {"attached": 2}
    # One sign-in handoff and one "sign-in needed" push; then the paused batch
    # was restarted, never re-created.
    assert scrapex.opened == 1
    assert [title for _u, title, _b in notified] == [
        "ADAS Map sign-in needed",
        "ADAS Map sweep finished",
    ]
    assert scrapex.started == ["batch1", "batch1"]
    assert len(scrapex.created) == 1
    assert sweep_mod.SIGN_IN_POLL_SECONDS in sleeps
    store.close()


@pytest.mark.asyncio
async def test_unfinished_sweep_resumes_after_a_core_restart(tmp_path):
    ros = ["2400711880", "2400711897"]
    scrapex = FakeScrapeX({})
    ciq = FakeCIQ(scrapex, ros)
    pause = asyncio.Event()

    async def held_sleep(_seconds):
        await pause.wait()

    service, store, conversation_id, context, _n, _p = make_service(
        tmp_path, scrapex, ciq, sleep=held_sleep
    )
    started = await service.start({"__xomni_invocation": context})
    await service.shutdown()  # Core stops; ScrapeX keeps its batch
    assert store.get_record(sweep_mod.NAMESPACE, started["sweep_id"], user_id="local-dev")[
        "state"
    ] == "running"

    restarted = AdasMapSweepService(
        SimpleNamespace(),
        store,
        scrapex=scrapex,
        inventory=ciq.inventory,
        ro_map_state=ciq.ro_map_state,
        notify=lambda *_a: asyncio.sleep(0),
        publish=None,
        sleep=no_sleep,
        poll_seconds=0.0,
    )
    assert await restarted.resume() == 1
    await run_to_completion(restarted, started["sweep_id"])
    record = store.get_record(sweep_mod.NAMESPACE, started["sweep_id"], user_id="local-dev")
    assert record["state"] == "completed"
    assert record["result"]["counts"] == {"attached": 2}
    assert len(scrapex.created) == 1  # resumed polling, no duplicate batch
    assert store.get_messages(conversation_id)[-1]["artifacts"][0]["type"] == "adas_map_sweep"
    store.close()


@pytest.mark.asyncio
async def test_status_reports_no_sweep_progress_and_final_result(tmp_path):
    ros = ["2400711883"]
    scrapex = FakeScrapeX({})
    ciq = FakeCIQ(scrapex, ros)
    service, store, _cid, context, _n, _p = make_service(tmp_path, scrapex, ciq)

    empty = await service.status({"__xomni_invocation": context})
    assert empty["status"] == "no_sweep"

    started = await service.start({"__xomni_invocation": context})
    during = await service.status({"__xomni_invocation": context})
    assert during["status"] == "running"
    assert during["work_complete"] is False
    assert during["progress"]["total"] == 1
    assert "Nothing is final" in during["message"]

    await run_to_completion(service, started["sweep_id"])
    done = await service.status({"__xomni_invocation": context})
    assert done["status"] == "completed"
    assert done["attached_count"] == 1
    assert done["message"].startswith("ADAS Map sweep for the active board is done: 1 of 1 attached.")
    store.close()


def test_outcome_classification_lets_calibration_iq_decide_attachment():
    assert classify_outcome({"scrapex_state": "ro_not_found"}, {"present": True}) == "attached"
    assert classify_outcome({"scrapex_state": "adas_map_complete"}, {"present": False, "readable": True}) == "not_confirmed_in_ciq"
    assert classify_outcome({"scrapex_state": "ro_not_found"}, {"present": False, "readable": True}) == "not_in_adas_map"
    assert classify_outcome({"scrapex_state": "view_did_not_navigate"}, {"present": False}) == "page_would_not_open"
    assert classify_outcome({"scrapex_state": None}, {"present": False, "readable": True}) == "not_processed"
    assert classify_outcome({"scrapex_state": "ro_not_found"}, {"present": False, "readable": False}) == "ciq_unreadable"


def test_summary_sentence_is_built_from_structured_counts():
    record = {
        "scope_label": "phases 1-8",
        "targets": [{}] * 16,
        "result": {
            "target_count": 16,
            "counts": {"attached": 8, "not_in_adas_map": 5, "page_would_not_open": 2, "requirements_unverified": 1},
        },
    }
    assert summary_sentence(record) == (
        "ADAS Map sweep for phases 1-8 is done: 8 of 16 attached. Of the rest, 5 not in "
        "ADAS Map yet, 2 in ADAS Map but the page would not open, and 1 found but the "
        "requirements could not be verified."
    )


# ------------------------------------------------------ X-facing contracts


def _catalog_loaded() -> None:
    configured_profile_catalog(
        SimpleNamespace(tools_config="config/tools.yaml", tool_profile="adas_operator")
    )


@pytest.mark.asyncio
async def test_stage_action_sweep_invokes_the_concrete_sweep_with_core_owned_context(tmp_path):
    _catalog_loaded()
    store = Store(tmp_path / "registry-sweep.sqlite")
    conversation_id = store.create_conversation("sweep")
    message_id = store.add_message(conversation_id, "user", "go get the missing adas maps")
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    seen: list[dict[str, Any]] = []

    async def fake_sweep(args):
        seen.append(dict(args))
        return {"status": "running", "executed": True, "success": True, "sweep_id": "s1"}

    async def fake_status(args):
        seen.append(dict(args))
        return {"status": "running", "success": True}

    registry.register("adas_map_sweep", fake_sweep)
    registry.register("adas_map_sweep_status", fake_status)
    context = dict(
        message_id=message_id,
        conversation_id=conversation_id,
        tool_call_id="call-1",
        user_id="local-dev",
        role="owner",
    )

    result = await registry.invoke(
        "stage_action",
        {"operation": "sweep_adas_maps", "phases": ["1", "8"], "shop": "Macon"},
        **context,
    )
    assert result["stage"] == "started"
    assert result["executed_via"] == "adas_map_sweep"
    assert seen[0]["phases"] == ["1", "8"] and seen[0]["shop"] == "Macon"
    injected = seen[0]["__xomni_invocation"]
    assert injected["conversation_id"] == conversation_id
    assert injected["tool_call_id"] == "call-1"
    assert [card for card, _ in artifacts_for_result("stage_action", result)] == ["adas_map_sweep"]

    # A model-supplied context is dropped and replaced, never trusted.
    await registry.invoke(
        "query_ciq",
        {"kind": "adas_map_sweep"},
        **context,
    )
    assert seen[1]["__xomni_invocation"]["user_id"] == "local-dev"
    assert meta.expand_query_ciq({"kind": "adas_map_sweep"}) == ("adas_map_sweep_status", {})
    store.close()


def test_sweep_is_a_stage_action_operation_and_phases_follow_the_request():
    _catalog_loaded()
    schema = meta.stage_action_schema()
    assert "sweep_adas_maps" in schema["parameters"]["properties"]["operation"]["enum"]
    assert list(schema["parameters"]["properties"])[:2] == ["operation", "phases"]
    query_phases = meta.QUERY_CIQ_SCHEMA["parameters"]["properties"]["phases"]["description"]
    assert "omit to cover the whole active board" in query_phases


def test_build_app_registers_the_sweep_and_guards_competing_acquisitions(tmp_path):
    settings = Settings(
        root=tmp_path,
        host="127.0.0.1",
        port=8100,
        workers_config=ROOT / "config" / "workers.json",
        tools_config=ROOT / "config" / "tools.yaml",
        db_path=tmp_path / "state.sqlite",
        audio_tmp=tmp_path / "audio",
        auth_enabled=False,
        google_client_id="",
        google_client_secret="",
        public_origin="",
        session_ttl_days=30,
        session_secret="test-secret",
        vram_free_threshold_mib=15_000,
        gpu_index=0,
        context_tokens=32_768,
        max_response_tokens=128,
        temperature=0.1,
        adas_si_root=tmp_path / "adas-si",
        automotive_knowledge_db=tmp_path / "knowledge.sqlite",
    )
    app = build_app(settings)
    registry = app.state.registry
    store = app.state.store
    try:
        assert registry.is_implemented("adas_map_sweep")
        assert registry.is_implemented("adas_map_sweep_status")
        store.put_record(
            sweep_mod.NAMESPACE,
            "running-1",
            {"sweep_id": "running-1", "user_id": "local-dev", "state": "running", "scope_label": "phases 1-8"},
            user_id="local-dev",
        )
        handler = registry._handlers["scrapex_adas_map"]  # noqa: SLF001
        refused = asyncio.run(handler({"action": "acquire_exact", "ro_number": "2400711905"}))
        assert refused["status"] == "sweep_running"
        assert refused["executed"] is False
        assert "phases 1-8" in refused["message"]
    finally:
        store.close()


# ------------------------------------------------ context and projections


def test_acquisition_results_reach_the_model_compactly_and_truthfully():
    real_shape = {
        "service": "ScrapeX",
        "action": "acquire_exact",
        "status": "completed",
        "success": True,
        "executed": True,
        "verified": True,
        "work_complete": True,
        "requested_ro_number": "2400711899",
        "exact_batch_id": "ac83e39668e14bd2b6879e4d4335b196",
        "data": {
            "attempted": True,
            "completed": True,
            "status": "completed",
            "batch_id": "ac83e39668e14bd2b6879e4d4335b196",
            "ro_number": "2400711899",
            "item": {
                "ro_number": "2400711899",
                "year": 2024,
                "make": "Honda",
                "model": "Accord",
                "adas_map_state": "adas_map_complete",
                "ciq_adas_map_verified": 1,
                "adas_map_inspection_id": "6453507",
                "adas_map_calibrations_json": "x" * 9_000,
            },
            "readiness": {"total": 1, "ready": True, "blockers": ["y" * 3_000]},
            "provenance": {
                "requirements_proven": True,
                "requirements": [{"label": "Front radar"}, {"label": "Seat belt"}],
                "raw_result": {"z": "z" * 6_000},
                "ciq_reconciliation_state": "complete",
            },
        },
        "ciq_attachment": {"status": "verified", "attached": True, "research_state": "research_in_progress"},
        "local_report": {"verified": True, "relative_path": "ADAS Map/2400711899/2400711899 ADAS Map.pdf", "bytes": 206_851},
        "chat_document": {"url": "u" * 400},
        "message": "ADAS Map for RO 2400711899 is attached in Calibration IQ.",
    }
    projected = adas_map_result_for_model(real_shape)
    encoded = json.dumps(projected)
    assert len(encoded) < 1_500 < len(json.dumps(real_shape))
    assert projected["status"] == "completed" and projected["work_complete"] is True
    assert projected["ciq_attachment"]["attached"] is True
    assert projected["data"]["item"]["vehicle"] == "2024 Honda Accord"
    assert projected["data"]["provenance"]["requirements"] == ["Front radar", "Seat belt"]
    # Create results keep data.id, which mints same-turn batch evidence.
    create = {"service": "ScrapeX", "action": "create_exact_batch", "data": {"id": "b1"}}
    assert adas_map_result_for_model(create) is create


def test_context_guard_shrinks_tool_results_until_the_request_fits():
    messages = [
        {"role": "system", "content": "s" * 3_000},
        {"role": "user", "content": "go get them"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{}"}}]},
        *[
            {
                "role": "tool",
                "tool_call_id": f"c{index}",
                "content": json.dumps({"status": "ok", "rows": [{"n": i, "pad": "p" * 90} for i in range(120)]}),
            }
            for index in range(10)
        ],
    ]
    shrunk = fit_messages_to_window(messages, [], context_tokens=12_000, reserve_tokens=1_000)
    assert shrunk > 0
    for message in messages:
        if message["role"] == "tool":
            json.loads(message["content"])  # still valid JSON
    total = sum(
        int(len(m.get("content") or "") / (2.9 if m["role"] == "tool" else 3.5)) + 8
        for m in messages
    )
    assert total < 12_000
    # A request that already fits is left untouched.
    small = [{"role": "tool", "tool_call_id": "a", "content": "{\"ok\": true}"}]
    assert fit_messages_to_window(small, [], context_tokens=32_768, reserve_tokens=1_536) == 0


def test_live_events_reach_only_the_owning_users_sockets():
    events = LiveEvents()
    seen_a: list[dict] = []
    seen_b: list[dict] = []
    unsubscribe = events.subscribe("otis", seen_a.append)
    events.subscribe("tester", seen_b.append)
    assert events.publish({"type": "conversation_updated", "conversation_id": 7}, user_id="otis") == 1
    assert seen_a == [{"type": "conversation_updated", "conversation_id": 7}]
    assert seen_b == []
    unsubscribe()
    assert events.publish({"type": "conversation_updated"}, user_id="otis") == 0


def test_store_lists_records_newest_first_per_user(tmp_path):
    store = Store(tmp_path / "records.sqlite")
    store.put_record("ns", "a", {"n": 1})
    store.put_record("ns", "b", {"n": 2})
    store.put_record("other", "c", {"n": 3})
    rows = store.list_records("ns", user_id="local-dev")
    assert {row["id"] for row in rows} == {"a", "b"}
    assert all(row["user_id"] == "local-dev" for row in rows)
    assert {row["id"] for row in store.list_records("ns")} == {"a", "b"}
    store.close()


# ------------------------------------------------------ background context


def test_context_line_reports_running_and_recent_results_only():
    from datetime import timedelta

    running = {
        "state": "running",
        "scope_label": "phases 1-8",
        "started_at": "2026-09-11T09:20:00+00:00",
        "targets": [
            {"finished": True, "scrapex_state": "adas_map_complete"},
            {"finished": True, "scrapex_state": "ro_not_found"},
            {"finished": False, "scrapex_state": "pending"},
        ],
    }
    line = sweep_mod.context_line(running)
    assert "still running" in line
    assert "2 of 3 ROs processed" in line
    assert "ScrapeX reports 1 complete so far" in line
    assert "No final result exists yet" in line

    finished_at = sweep_mod._now() - timedelta(hours=1)  # noqa: SLF001
    done = {
        "state": "completed",
        "scope_label": "phases 1-8",
        "finished_at": finished_at.isoformat(),
        "targets": [{}] * 16,
        "result": {"target_count": 16, "counts": {"attached": 8, "not_in_adas_map": 8}},
    }
    assert "8 of 16 attached" in sweep_mod.context_line(done)
    stale = dict(done, finished_at=(sweep_mod._now() - timedelta(hours=30)).isoformat())  # noqa: SLF001
    assert sweep_mod.context_line(stale) is None
    assert sweep_mod.context_line({"state": "failed"}) is None


def test_background_line_rides_in_the_turn_context_not_the_static_prompt():
    from core.orchestrator import prompt

    class Router:
        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    line = "ADAS Map sweep for phases 1-8 is still running in the background."
    messages = prompt.build_messages(
        Router(),
        [{"role": "user", "content": "continue", "artifacts": []}],
        32_768,
        1_024,
        background=line,
    )
    assert "## Background work" not in messages[0]["content"]
    assert messages[-2]["content"].startswith("## Right now")
    assert "## Background work" in messages[-2]["content"]
    assert line in messages[-2]["content"]
    assert messages[-1] == {"role": "user", "content": "continue"}


@pytest.mark.asyncio
async def test_no_tool_review_carries_the_background_record():
    from core.orchestrator.loop import (
        NO_TOOL_SELF_CHECK_ACCEPT,
        model_owned_no_tool_self_check,
    )

    seen: dict[str, Any] = {}

    class Client:
        async def stream(self, messages, tools=None):
            seen["instruction"] = messages[-1]["content"]
            yield {"type": "content", "text": NO_TOOL_SELF_CHECK_ACCEPT}

    line = "ADAS Map sweep for phases 1-8 is still running; no final result exists yet."
    await model_owned_no_tool_self_check(
        Client(),
        [{"role": "user", "content": "continue"}],
        [],
        "All 16 maps are attached.",
        background=line,
    )
    assert seen["instruction"].startswith("Internal evidence check")
    assert line in seen["instruction"]
    assert "call query_ciq with kind=adas_map_sweep" in seen["instruction"]


@pytest.mark.asyncio
async def test_latest_context_line_reads_the_users_latest_sweep(tmp_path):
    store = Store(tmp_path / "ctx.sqlite")
    store.put_record(
        sweep_mod.NAMESPACE,
        "s1",
        {
            "sweep_id": "s1",
            "user_id": "local-dev",
            "state": "running",
            "scope_label": "the active board",
            "started_at": "2026-09-11T09:20:00+00:00",
            "targets": [{"finished": False}],
        },
        user_id="local-dev",
    )
    assert "still running" in sweep_mod.latest_context_line(store, "local-dev")
    assert sweep_mod.latest_context_line(SimpleNamespace(), "local-dev") is None
    store.close()


@pytest.mark.asyncio
async def test_rejected_draft_during_a_sweep_falls_back_to_cores_own_record(tmp_path):
    from core.orchestrator.loop import Orchestrator

    store = Store(tmp_path / "fallback.sqlite")
    conversation_id = store.create_conversation("continue")
    message_id = store.add_message(conversation_id, "user", "continue")
    store.put_record(
        sweep_mod.NAMESPACE,
        "s1",
        {
            "sweep_id": "s1",
            "user_id": "local-dev",
            "state": "running",
            "scope_label": "phases 1-8",
            "started_at": "2026-09-11T09:20:00+00:00",
            "targets": [{"finished": True, "scrapex_state": "adas_map_complete"}, {"finished": False}],
        },
        user_id="local-dev",
    )
    _catalog_loaded()
    registry = Registry("config/tools.yaml", store=store, profile="adas_operator")
    for name in ("delegate_research", "capability_search"):
        registry.register(name, lambda _args: {})

    class Client:
        supports_no_tool_self_check = True
        calls = 0

        async def stream(self, messages, tools=None, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "content", "text": "The sweep finished and all maps are attached."}
            else:
                yield {"type": "content", "text": "That draft is not supported."}

    router = SimpleNamespace(
        active_name="omni",
        active_config=lambda: SimpleNamespace(supports_vision=True, supports_audio=True),
    )
    events = [
        event
        async for event in Orchestrator(
            router,
            Client(),
            registry,
            store,
            SimpleNamespace(context_tokens=32_768, max_response_tokens=1_024),
        ).run_turn(
            conversation_id,
            "continue",
            approval_context={"session_id": "s", "user_id": "local-dev", "role": "owner", "message_id": message_id},
        )
    ]
    text = "".join(event["text"] for event in events if event.get("type") == "token")
    assert text.startswith("ADAS Map sweep for phases 1-8 is still running")
    assert "1 of 2 ROs processed" in text
    assert "all maps are attached" not in text
    store.close()
