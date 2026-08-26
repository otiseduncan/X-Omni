from __future__ import annotations

from pathlib import Path

import pytest

from core.config import ROOT, Settings
from core.main import build_app
from core.tools.registry import NeedsApproval, TOOL_SCHEMAS


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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


def _candidate_record() -> dict:
    return {
        "application": {"year": 2020, "manufacturer": "Toyota", "model": "Camry"},
        "system": "Forward collision warning",
        "component": "Front radar sensor",
        "repair_event": {
            "event_type": "collision_repair",
            "description": "Front radar bracket replaced",
        },
        "requirement": {
            "type": "calibration",
            "text": "Candidate requirement pending trusted review.",
        },
        "lifecycle": "verified",
        "evidence": [
            {
                "source": {
                    "type": "scrapex_adas_map",
                    "document_id": "inspection-1",
                    "name": "ADAS Map inspection",
                    "authoritative": True,
                    "sha256": "a" * 64,
                },
                "page": 1,
                "section": "Calibration requirements",
                "excerpt": "Candidate extracted evidence.",
                "extraction_status": "extracted",
                "verification_status": "verified",
            }
        ],
    }


def _one_of_object_schema_accepts(schema: dict, payload: dict) -> bool:
    matches = 0
    for branch in schema["oneOf"]:
        properties = set(branch.get("properties") or {})
        required = set(branch.get("required") or [])
        if required <= set(payload) and (
            branch.get("additionalProperties") is not False
            or set(payload) <= properties
        ):
            matches += 1
    return matches == 1


def test_knowledge_search_schema_requires_complete_application_scope() -> None:
    schema = TOOL_SCHEMAS["automotive_knowledge_search"]["parameters"]

    assert _one_of_object_schema_accepts(schema, {})
    assert _one_of_object_schema_accepts(
        schema,
        {"query": "camera calibration procedures", "system": "ADAS"},
    )
    assert _one_of_object_schema_accepts(
        schema,
        {
            "year": 2024,
            "manufacturer": "Toyota",
            "model": "Camry",
            "event": "windshield replacement",
            "calibration_type": "dynamic",
        },
    )
    assert not _one_of_object_schema_accepts(
        schema,
        {"query": "Camry", "event": "windshield replacement"},
    )
    assert not _one_of_object_schema_accepts(
        schema,
        {"requirement_type": "calibration"},
    )
    assert not _one_of_object_schema_accepts(
        schema,
        {"year": 2024, "manufacturer": "Toyota", "event_type": "repair"},
    )


@pytest.mark.asyncio
async def test_knowledge_tools_are_statically_advertised_and_safely_tiered(tmp_path):
    app = build_app(_settings(tmp_path))
    registry = app.state.registry
    try:
        advertised = {
            item["function"]["name"] for item in registry.model_tools("owner")
        }
        assert {
            "automotive_knowledge_search",
            "automotive_knowledge_read",
            "automotive_knowledge_capture",
        } <= advertised
        assert "automotive_knowledge_lifecycle" not in advertised
        assert registry.profile_allows_tool("automotive_knowledge_lifecycle") is False
        assert "automotive_knowledge_review" not in advertised
        assert registry.tier("automotive_knowledge_search") == "read_only"
        assert registry.tier("automotive_knowledge_capture") == "operator_authorized"
        assert registry.tier("automotive_knowledge_lifecycle") == "confirm_required"
        search_branches = TOOL_SCHEMAS["automotive_knowledge_search"][
            "parameters"
        ]["oneOf"]
        assert all(branch["additionalProperties"] is False for branch in search_branches)
        assert search_branches[1]["required"] == ["year", "manufacturer", "model"]
        capture_conditions = TOOL_SCHEMAS["automotive_knowledge_capture"][
            "parameters"
        ]["allOf"]
        assert capture_conditions == [
            {
                "if": {
                    "properties": {"action": {"const": "capture"}},
                    "required": ["action"],
                },
                "then": {"required": ["record"]},
            },
            {
                "if": {
                    "properties": {"action": {"const": "add_evidence"}},
                    "required": ["action"],
                },
                "then": {
                    "required": ["record_id", "expected_version", "evidence"]
                },
            },
        ]
        lifecycle_conditions = TOOL_SCHEMAS["automotive_knowledge_lifecycle"][
            "parameters"
        ]["allOf"]
        assert lifecycle_conditions == [
            {
                "if": {
                    "properties": {"action": {"const": "promote"}},
                    "required": ["action"],
                },
                "then": {"required": ["lifecycle"]},
            },
            {
                "if": {
                    "properties": {"action": {"const": "supersede"}},
                    "required": ["action"],
                },
                "then": {"required": ["replacement_id"]},
            },
        ]

        result = await registry.invoke(
            "automotive_knowledge_capture",
            {"action": "capture", "record": _candidate_record()},
        )
        assert result["success"] is True
        assert result["verified"] is False
        assert result["requested_lifecycle"] == "verified"
        assert result["stored_lifecycle"] == "evidence_backed"
        assert result["record"]["evidence"][0]["verification_status"] == "unverified"

        with pytest.raises(NeedsApproval):
            await registry.invoke(
                "automotive_knowledge_lifecycle",
                {
                    "action": "promote",
                    "record_id": result["record"]["id"],
                    "expected_version": result["record"]["version"],
                    "lifecycle": "verified",
                },
            )
    finally:
        app.state.automotive_knowledge.repository.close()
        app.state.store.close()
