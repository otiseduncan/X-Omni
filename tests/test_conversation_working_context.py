from __future__ import annotations

import json

import pytest

from core.services.conversation_subjects import (
    subject_from_tool_result,
    track_active_subject_from_tool_result,
)
from core.state.db import Store


def _ciq_result(
    *,
    ro_id: str = "ro-1",
    ro_number: str = "2400911777",
    version: int = 8,
    calibration_label: str = "Front camera",
    many: bool = False,
) -> dict:
    count = 30 if many else 1
    calibrations = [
        {
            "id": f"cal-{index}",
            "name": f"{calibration_label} {index}",
            "status": "required",
            "determination": "required",
            "method": "static",
            "description": "x" * 800,
            "version": version,
            "access_token": "must-never-persist",
        }
        for index in range(count)
    ]
    blockers = [
        {
            "id": f"block-{index}",
            "title": f"Reassembly {index}",
            "status": "open",
            "description": "b" * 800,
            "version": version,
            "password": "must-never-persist",
        }
        for index in range(count)
    ]
    documents = [
        {
            "id": f"doc-{index}",
            "title": f"OEM procedure {index}",
            "document_type": "oem_procedure",
            "download_url": (
                f"/api/calibration-iq/documents/doc-{index}/download"
                "?repair_order_id=ro-1&token=must-never-persist"
            ),
            "sha256": f"{index:064x}",
        }
        for index in range(count)
    ]
    workspace = [
        {
            "kind": "file",
            "path": f"notes/case-{index}.txt",
            "download_url": (
                "/api/calibration-iq/workspace-file"
                f"?repair_order_id={ro_id}&path=notes%2Fcase-{index}.txt"
                "&secret=must-never-persist"
            ),
        }
        for index in range(count)
    ]
    return {
        "status": "verified",
        "repair_order": {
            "id": ro_id,
            "RO": ro_number,
            "Vehicle": "2024 Toyota Camry LE",
            "Status": "Research",
            "Shop": "Macon",
            "Phase": 6,
            "version": version,
        },
        "raw": {
            "repair_order": {
                "id": ro_id,
                "ro_number": ro_number,
                "year": 2024,
                "make": "Toyota",
                "model": "Camry",
                "trim": "LE",
                "vin": "4T1C11AK0RU000001",
                "version": version,
            },
            "shop": {"id": "shop-1", "name": "Macon"},
            "workflow": {"status": "RESEARCH", "phase": 6, "version": version},
            "calibrations": calibrations,
            "blockers": blockers,
            "research": {
                "state": "research_in_progress",
                "version": version,
                "documents": documents,
                "workspace": workspace,
            },
        },
    }


def _weekly_result() -> dict:
    return {
        "status": "partial_success",
        "mode": "week_readiness",
        "success": True,
        "verified": True,
        "readiness_complete": False,
        "queue_count": 2,
        "ready_count": 1,
        "exception_count": 1,
        "needs_si_count": 1,
        "si_unverified_count": 0,
        "adas_map_verified_count": 2,
        "queue_persistence_status": "persisted",
        "queue_persistence_verified": True,
        "phase_scope": ["5", "6", "7", "8"],
        "filters": {"include_completed": True, "shop": "Macon"},
        "repair_orders_total": 2,
        "repair_orders_shown": 2,
        "repair_orders_truncated": False,
        "repair_orders": [
            {
                "repair_order_id": "ro-1",
                "ro_number": "2400911777",
                "vehicle": "2024 Toyota Camry LE",
                "status": "ready",
                "ready": True,
                "coverage_status": "covered",
                "adas_map": {"status": "verified"},
            },
            {
                "repair_order_id": "ro-2",
                "ro_number": "2400911778",
                "vehicle": "2024 Toyota RAV4",
                "status": "si_missing",
                "ready": False,
                "coverage_status": "missing",
                "adas_map": {"status": "verified"},
                "missing_si": [{"calibration": "Blind spot monitor"}],
            },
        ],
    }


def _work_list_result() -> dict:
    return {
        "status": "verified",
        "scope": "active",
        "result_scope": "board_list_only",
        "exact_ro_detail_included": False,
        "count": 2,
        "shown_count": 2,
        "collection_complete": True,
        "truncated": False,
        "filters": {"phase": "6", "shop": "Macon", "include_completed": False},
        "rows": [
            {
                "id": "ro-1",
                "RO": "2400911777",
                "Vehicle": "2024 Toyota Camry LE",
                "Status": "Research",
                "Phase": 6,
                "Shop": "Macon",
                "version": 8,
            },
            {
                "id": "ro-2",
                "RO": "2400911778",
                "Vehicle": "2024 Toyota RAV4",
                "Status": "Calibration",
                "Phase": 6,
                "Shop": "Macon",
                "version": 3,
            },
        ],
    }


def _scrapex_result() -> dict:
    return {
        "service": "ScrapeX",
        "action": "batch_item",
        "status": "verified",
        "success": True,
        "executed": True,
        "verified": True,
        "data": {
            "batch_id": "batch-9",
            "batch_name": "Weekly",
            "batch_state": "paused",
            "item": {
                "id": "item-9",
                "batch_id": "batch-9",
                "ro_number": "2400911777",
                "adas_map_state": "adas_map_complete",
            },
            "provenance": {
                "contract_version": 3,
                "state": "adas_map_complete",
                "requirements_proven": True,
                "inspection_id": "inspection-9",
                "source_url": "https://opus.adasmap.com/details/9?token=private",
                "checked_at": "2026-08-25T20:00:00Z",
                "requirements": [{"calibration_type": "Blind Spot Monitor"}],
                "raw_result": {
                    "requirement_records": [
                        {
                            "label": "Blind Spot Monitor",
                            "source": "adas_map_required_list_item",
                            "source_context": "selected_required_modal",
                            "source_context_runtime_id": "42.9",
                            "cookie": "must-never-persist",
                        }
                    ]
                },
                "ciq_reconciliation_state": "complete",
                "ciq_reconciliation": {
                    "verified": True,
                    "snapshot_verified": True,
                    "receipt_count": 1,
                },
            },
        },
    }


def _knowledge_result(*, current: bool = True) -> dict:
    return {
        "status": "success",
        "source": "durable_automotive_knowledge",
        "records": [
            {
                "id": "knowledge-1",
                "version": 4,
                "application": {
                    "manufacturer": "Toyota",
                    "year_start": 2024,
                    "year_end": 2024,
                    "model": "Camry",
                    "trim": "LE",
                },
                "system": {"name": "Forward collision warning"},
                "component": {"name": "Front camera"},
                "repair_event": {"description": "Windshield replacement"},
                "requirement": {
                    "requirement_type": "calibration",
                    "text": "Perform camera aiming after windshield replacement.",
                    "calibration_type": "static",
                    "inspection_required": True,
                },
                "lifecycle": "verified",
                "source_integrity": {
                    "status": "current" if current else "stale",
                    "verified_read_allowed": current,
                },
                "evidence": [
                    {
                        "id": "evidence-1",
                        "page_start": 12,
                        "section": "Camera aiming",
                        "verification_effective": current,
                        "verification_status": "verified" if current else "unverified",
                        "source": {
                            "document_id": "toyota-camry-2024",
                            "source_name": "Toyota repair manual",
                            "content_sha256": "a" * 64,
                            "refresh_token": "must-never-persist",
                        },
                    }
                ],
            }
        ],
    }


def test_ciq_snapshot_adds_bounded_source_owned_sections_without_secrets(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    stored = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(many=True),
        tool_call_id="ciq-read-1",
    )
    assert stored is not None
    payload = stored["payload"]
    context = payload["working_context"]
    assert payload["context_schema_version"] == 1
    assert context["active_repair_order"]["repair_order_id"] == "ro-1"
    assert context["active_repair_order"]["ro_number"] == "2400911777"
    assert payload["current_calibration_detail_included"] is True
    assert "next_capability_for_current_ro_detail" not in payload
    assert set(context["sections"]) == {
        "calibrations",
        "blockers",
        "documents",
        "workspace",
    }
    for section in context["sections"].values():
        assert section["source_owner"] == "calibration_iq"
        assert section["authoritative"] is True
        assert section["observation"]["tool_call_id"] == "ciq-read-1"
        assert len(section["items"]) <= 8
    assert context["sections"]["calibrations"]["count"] == 30
    serialized = json.dumps(payload, sort_keys=True)
    assert len(serialized.encode("utf-8")) < 16_384
    assert "must-never-persist" not in serialized
    assert context["sections"]["calibrations"]["items_truncated"] is True


def test_provider_urls_and_malformed_scalar_fields_cannot_smuggle_secrets():
    result = _ciq_result()
    snapshot = result["raw"]
    snapshot["calibrations"][0]["name"] = {
        "label": "Front camera",
        "password": "nested-secret",
    }
    snapshot["calibrations"][0]["description"] = [
        "normal-looking value",
        {"refresh_token": "nested-secret"},
    ]
    snapshot["research"]["documents"][0]["download_url"] = (
        "/api/calibration-iq/documents/doc-0/download"
        "?token=relative-secret#fragment"
    )
    snapshot["research"]["workspace"][0]["download_url"] = (
        "//attacker.example/workspace?token=network-path-secret"
    )

    subject = subject_from_tool_result("calibration_iq_ro", result)
    assert subject is not None
    context = subject["working_context"]
    assert "context_truncated" not in context
    document = context["sections"]["documents"]["items"][0]
    workspace = context["sections"]["workspace"]["items"][0]
    assert document["download_url"] == (
        "/api/calibration-iq/documents/doc-0/download"
    )
    assert "download_url" not in workspace
    calibration = context["sections"]["calibrations"]["items"][0]
    assert "label" not in calibration
    assert "reason" not in calibration
    serialized = json.dumps(subject, sort_keys=True)
    for secret in (
        "relative-secret",
        "network-path-secret",
        "nested-secret",
        "refresh_token",
        "password",
    ):
        assert secret not in serialized


def test_weekly_context_never_replaces_active_ro_and_survives_exact_ro_change(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    weekly = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_work_prep",
        result=_weekly_result(),
        tool_call_id="weekly-1",
    )
    assert weekly is not None
    payload = weekly["payload"]
    assert payload["resource_id"] == "ro-1"
    assert payload["working_context"]["active_repair_order"]["repair_order_id"] == "ro-1"
    weekly_section = payload["working_context"]["sections"]["weekly"]
    assert weekly_section["source_owner"] == "calibration_iq_work_prep"
    assert weekly_section["queue_count"] == 2
    assert weekly_section["items"][0]["repair_order_id"] == "ro-2"
    assert weekly_section["observation"]["tool_call_id"] == "weekly-1"

    changed = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(
            ro_id="ro-2",
            ro_number="2400911778",
            version=9,
            calibration_label="Blind spot",
        ),
    )
    assert changed is not None
    payload = changed["payload"]
    assert payload["resource_id"] == "ro-2"
    assert payload["working_context"]["active_repair_order"]["repair_order_id"] == "ro-2"
    assert payload["working_context"]["sections"]["weekly"]["queue_count"] == 2
    assert (
        payload["working_context"]["sections"]["calibrations"]["items"][0]["label"]
        == "Blind spot 0"
    )


def test_verified_work_list_merges_beside_weekly_and_never_replaces_active_ro(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    work_list = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_read",
        result=_work_list_result(),
        tool_call_id="ciq-list-1",
    )
    assert work_list is not None
    payload = work_list["payload"]
    assert payload["resource_id"] == "ro-1"
    section = payload["working_context"]["sections"]["work_list"]
    assert section["source_owner"] == "calibration_iq"
    assert section["references_count"] == 2
    assert section["references"][1]["repair_order_id"] == "ro-2"
    assert section["observation"]["tool_call_id"] == "ciq-list-1"

    weekly = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_work_prep",
        result=_weekly_result(),
    )
    assert weekly is not None
    assert weekly["payload"]["resource_id"] == "ro-1"
    assert set(weekly["payload"]["working_context"]["sections"]) >= {
        "work_list",
        "weekly",
    }

    changed = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(
            ro_id="ro-2",
            ro_number="2400911778",
            version=9,
            calibration_label="Blind spot",
        ),
    )
    assert changed is not None
    context = changed["payload"]["working_context"]
    assert context["active_repair_order"]["repair_order_id"] == "ro-2"
    assert set(context["sections"]) >= {"work_list", "weekly", "calibrations"}


def test_weekly_only_subject_promotes_to_exact_ro_without_losing_work_list(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    weekly = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_work_prep",
        result=_weekly_result(),
    )
    assert weekly is not None
    assert weekly["payload"]["type"] == "calibration_iq.weekly_readiness"
    assert "active_repair_order" not in weekly["payload"]["working_context"]

    exact = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    assert exact is not None
    assert exact["payload"]["type"] == "calibration_iq.repair_order"
    assert exact["payload"]["working_context"]["sections"]["weekly"]["queue_count"] == 2


def test_tracker_cannot_read_or_update_another_users_conversation(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    with pytest.raises(ValueError, match="does not exist for this user"):
        track_active_subject_from_tool_result(
            store,
            conversation_id=conversation_id,
            tool_name="calibration_iq_work_prep",
            result=_weekly_result(),
            user_id="different-user",
        )
    stored = store.get_conversation_subject(conversation_id)
    assert stored["version"] == 1
    assert stored["payload"]["resource_id"] == "ro-1"


def test_scrapex_enriches_exact_ro_without_overwriting_ciq_sections(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    stored = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="scrapex_read",
        result=_scrapex_result(),
        tool_call_id="scrapex-item-1",
    )
    assert stored is not None
    payload = stored["payload"]
    assert payload["resource_id"] == "ro-1"
    assert payload["identity_source_owner"] == "calibration_iq"
    assert (
        payload["working_context"]["sections"]["calibrations"]["source_owner"]
        == "calibration_iq"
    )
    evidence = payload["working_context"]["evidence"]["scrapex_adas_map"]
    assert evidence["inspection_id"] == "inspection-9"
    assert evidence["requirements_proven"] is True
    assert evidence["ciq_reconciliation"]["verified"] is True
    assert evidence["source_url"] == "https://opus.adasmap.com/details/9"
    assert evidence["observation"]["tool_call_id"] == "scrapex-item-1"
    assert "must-never-persist" not in json.dumps(payload)

    malformed = _scrapex_result()
    malformed["verified"] = False
    assert (
        track_active_subject_from_tool_result(
            store,
            conversation_id=conversation_id,
            tool_name="scrapex_read",
            result=malformed,
        )
        is None
    )
    assert store.get_conversation_subject(conversation_id)["version"] == 2


def test_matching_adas_si_and_verified_knowledge_enrich_only_the_current_vehicle(
    tmp_path,
):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(),
    )
    adas = {
        "status": "success",
        "source": "ADAS SI",
        "exact_source_matched": True,
        "structured_query": {
            "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
            "system": "Forward camera",
            "search_mode": "calibration_requirements",
        },
        "matched_documents": [
            {
                "title": "2024 Toyota Camry camera calibration",
                "relative_path": "Toyota/Camry Camera.pdf",
                "url": "/api/adas-si/document?path=Toyota%2FCamry+Camera.pdf",
                "year": 2024,
                "make": "Toyota",
                "model": "Camry",
                "topic": "Front Camera",
                "source_match_score": 25,
            }
        ],
        "results": [],
    }
    stored = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="adas_si_search",
        result=adas,
        tool_call_id="adas-si-1",
    )
    assert stored is not None
    evidence = stored["payload"]["working_context"]["evidence"]["adas_si"]
    assert evidence["application_bound"] is True
    assert evidence["documents"][0]["title"].startswith("2024 Toyota Camry")

    mismatch = dict(adas)
    mismatch["structured_query"] = {
        "vehicle": {"year": 2024, "make": "Toyota", "model": "RAV4"}
    }
    assert (
        track_active_subject_from_tool_result(
            store,
            conversation_id=conversation_id,
            tool_name="adas_si_search",
            result=mismatch,
        )
        is None
    )

    knowledge = track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="automotive_knowledge_search",
        result=_knowledge_result(),
        tool_call_id="knowledge-1",
    )
    assert knowledge is not None
    durable = knowledge["payload"]["working_context"]["evidence"][
        "durable_automotive_knowledge"
    ]
    assert durable["verified_hashed_sources_only"] is True
    assert durable["records"][0]["id"] == "knowledge-1"
    assert durable["records"][0]["evidence"][0]["content_sha256"] == "a" * 64

    assert (
        track_active_subject_from_tool_result(
            store,
            conversation_id=conversation_id,
            tool_name="automotive_knowledge_search",
            result=_knowledge_result(current=False),
        )
        is None
    )
    assert store.get_conversation_subject(conversation_id)["version"] == 3


def test_stale_lower_version_ciq_snapshot_cannot_revert_active_context(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation()
    track_active_subject_from_tool_result(
        store,
        conversation_id=conversation_id,
        tool_name="calibration_iq_ro",
        result=_ciq_result(version=9, calibration_label="Current camera"),
    )
    assert (
        track_active_subject_from_tool_result(
            store,
            conversation_id=conversation_id,
            tool_name="calibration_iq_ro",
            result=_ciq_result(version=8, calibration_label="Stale camera"),
        )
        is None
    )
    stored = store.get_conversation_subject(conversation_id)
    assert stored["version"] == 1
    assert (
        stored["payload"]["working_context"]["sections"]["calibrations"]["items"][0][
            "label"
        ]
        == "Current camera 0"
    )


def test_ro_requirements_is_an_exact_authoritative_subject_not_a_text_inference():
    result = {
        "status": "success",
        "mode": "ro_requirements",
        "success": True,
        "verified": True,
        "snapshot_verified": True,
        "repair_order_id": "ro-55",
        "ro_number": "2400911755",
        "vehicle": "2024 Honda Accord",
        "calibration_requirements": [
            {
                "id": "cal-55",
                "label": "Front camera",
                "determination": "required",
                "method": "static",
            }
        ],
    }
    subject = subject_from_tool_result("calibration_iq_work_prep", result)
    assert subject is not None
    assert subject["resource_id"] == "ro-55"
    section = subject["working_context"]["sections"]["calibrations"]
    assert section["source_owner"] == "calibration_iq"
    assert section["items"][0]["label"] == "Front camera"
