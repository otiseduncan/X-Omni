r"""Opt-in, non-destructive acceptance checks against the configured Qwen worker.

The suite sends real OpenAI-compatible chat requests to the local model, but
every business tool result is supplied by this file.  It therefore exercises
the model's semantic tool selection and continuation behavior without reading
or mutating Calibration IQ, ScrapeX, ADAS SI, ALLDATA, or the knowledge store.

Ordinary pytest runs skip this module.  Run it explicitly with either::

    $env:XOMNI_RUN_LIVE_MODEL_ACCEPTANCE = "1"
    .venv\Scripts\python.exe -m pytest -q tests\test_model_first_live_acceptance.py -s

or::

    .venv\Scripts\python.exe tests\test_model_first_live_acceptance.py

Scenario expectations are structured data.  No language classifier or regular
expression decides which tool is expected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
RUN_ENV = "XOMNI_RUN_LIVE_MODEL_ACCEPTANCE"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _object_schema(
    properties: dict[str, Any], required: Iterable[str] = ()
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


# Business tools come directly from the configured production ADAS profile;
# only acceptance_report below is test-only instrumentation.

REPORT_SCHEMA = {
    "description": (
        "Test-only terminal report. Call exactly once after all needed business tool "
        "results have been examined. It records semantic decisions without depending "
        "on exact prose."
    ),
    "parameters": _object_schema(
        {
            "outcome": {
                "type": "string",
                "enum": [
                    "answered",
                    "approval_required",
                    "blocked",
                    "indeterminate",
                    "no_authoritative_answer",
                ],
                "description": (
                    "Terminal semantic outcome. A tool result with "
                    "authentication_required=true or requires_human=true is always the "
                    "blocked human-authentication boundary, never approval_required. "
                    "approval_required is only for an explicit Owner approval result."
                    " A mutation result that may have executed but lacks receipt and "
                    "authoritative-reread verification is indeterminate."
                ),
            },
            "authoritative_answer_found": {"type": "boolean"},
            "execution_state": {
                "type": "string",
                "enum": [
                    "not_requested",
                    "verified",
                    "approval_required",
                    "not_confirmed",
                ],
                "description": (
                    "State only of a requested business mutation. Precedence: when the user "
                    "requested acquisition or processing and authentication_required blocks "
                    "that requested mutation, choose not_confirmed even if its final tool "
                    "result says executed=false or an earlier setup step succeeded. Otherwise "
                    "choose not_requested whenever no write, deletion, or acquisition was "
                    "requested or called, even when a read result has status=verified. "
                    "calibration_iq_ro, calibration_iq_read, searches, and research lookups "
                    "are reads and therefore remain not_requested. Choose verified only after "
                    "the requested mutation completes with a verified execution receipt."
                ),
            },
            "used_active_subject": {"type": "boolean"},
            "subject_resource_id": {"type": "string"},
            "requested_credentials": {
                "type": "boolean",
                "description": (
                    "True only if the assistant itself asked the user to provide a password, "
                    "token, cookie, or other secret in chat. A user's instruction not to ask "
                    "for a password and a tool result saying credentials stay outside model "
                    "context both require false."
                ),
            },
            "sources_checked": {
                "type": "array",
                "description": (
                    "Every source whose business tool returned a result this turn, including "
                    "intermediate reads and bounded misses. Include calibration_iq whenever "
                    "calibration_iq_ro, calibration_iq_read, or calibration_iq_summary was "
                    "called, even when a later source supplied the final answer."
                ),
                "items": {
                    "type": "string",
                    "enum": [
                        "capability_catalog",
                        "calibration_iq",
                        "durable_knowledge",
                        "adas_si",
                        "scrapex_adas_map",
                        "alldata",
                    ],
                },
            },
            "observed_evidence_ids": {
                "type": "array",
                "description": (
                    "Evidence or source-resource identifiers decisive for the terminal outcome. "
                    "Include final source hits, misses, blocked results, mutation receipts, and "
                    "production-observable batch/item/provenance ids when a service emits no "
                    "evidence_id. An intermediate setup/list id may be omitted when the next "
                    "matched tool call demonstrably copied and consumed its opaque id."
                ),
                "items": {"type": "string"},
            },
            "summary": {"type": "string"},
        },
        (
            "outcome",
            "authoritative_answer_found",
            "execution_state",
            "used_active_subject",
            "subject_resource_id",
            "requested_credentials",
            "sources_checked",
            "observed_evidence_ids",
            "summary",
        ),
    ),
}

CLOSE_FINAL_TRUTH_AUDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "acceptance_close_truth_audit",
        "description": (
            "Test-only semantic audit of whether final prose mentions child calibration "
            "state that was absent from a verified close_ro snapshot."
        ),
        "parameters": _object_schema(
            {
                "mentions_child_calibration_state": {"type": "boolean"},
                "unsupported_child_calibration_claim": {"type": "boolean"},
                "rationale": {"type": "string"},
            },
            (
                "mentions_child_calibration_state",
                "unsupported_child_calibration_claim",
                "rationale",
            ),
        ),
    },
}


def _production_profile_tools() -> list[dict[str, Any]]:
    """Read the real ADAS profile catalog without constructing live handlers."""
    from core.config import Settings
    from core.main import configured_profile_catalog

    settings = Settings.load()
    return configured_profile_catalog(
        settings,
        role="owner",
        profile="adas_operator",
    )


def _production_system_prompt() -> str:
    from core.orchestrator.prompt import system_prompt

    router = SimpleNamespace(
        active_config=lambda: SimpleNamespace(
            supports_vision=True,
            supports_audio=True,
        )
    )
    return system_prompt(router)


ACCEPTANCE_HARNESS_BOUNDARY = """This is a non-destructive model-level acceptance
run. Advertised business tools have their real production schemas, but this harness
returns production-shaped fixture results and never invokes their handlers. Treat each
fixture exactly like an authoritative tool result. When the test-only acceptance_report
tool becomes available, call it once with the semantic outcome before composing the final
user response. Do not mention the harness to the user."""


def _tool_list() -> list[dict[str, Any]]:
    report = {
        "type": "function",
        "function": {
            "name": "acceptance_report",
            "description": REPORT_SCHEMA["description"],
            "parameters": REPORT_SCHEMA["parameters"],
        },
    }
    return [*_production_profile_tools(), report]


@dataclass(frozen=True)
class WorkerTarget:
    endpoint: str
    model: str


def configured_worker_target() -> WorkerTarget:
    override_endpoint = os.getenv("XOMNI_MODEL_BASE_URL")
    override_model = os.getenv("XOMNI_MODEL_ALIAS")
    raw = json.loads((ROOT / "config" / "workers.json").read_text(encoding="utf-8"))
    default_name = str(raw["default_worker"])
    config = raw["workers"][default_name]
    endpoint = (
        override_endpoint
        or f"http://{config.get('host', '127.0.0.1')}:{int(config['port'])}/v1"
    )
    return WorkerTarget(endpoint.rstrip("/"), override_model or str(config["alias"]))


def worker_is_healthy(target: WorkerTarget, timeout: float = 3.0) -> bool:
    health_url = target.endpoint.removesuffix("/v1") + "/health"
    try:
        response = httpx.get(health_url, timeout=timeout, trust_env=False)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError, TypeError):
        return False


def _subset(actual: Any, expected: Any, path: str = "arguments") -> None:
    """Assert a structured subset; this deliberately performs no text classification."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path} must be an object, got {actual!r}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key} is missing from {actual!r}"
            _subset(actual[key], value, f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path} must be an array, got {actual!r}"
        assert len(actual) >= len(expected), f"{path} has too few items: {actual!r}"
        for index, value in enumerate(expected):
            _subset(actual[index], value, f"{path}[{index}]")
        return
    assert actual == expected, f"{path}: expected {expected!r}, got {actual!r}"


ArgValidator = Callable[[dict[str, Any]], None]


def _repository_knowledge_scope(arguments: dict[str, Any]) -> None:
    allowed = {
        "query",
        "system",
        "component",
        "lifecycles",
        "include_superseded",
        "limit",
    }
    assert set(arguments) <= allowed, arguments
    semantic_terms = [arguments.get(key) for key in ("query", "system", "component")]
    assert any(isinstance(value, str) and value.strip() for value in semantic_terms), (
        arguments
    )
    lifecycles = arguments.get("lifecycles")
    if lifecycles is not None:
        assert isinstance(lifecycles, list) and lifecycles, arguments
        assert set(lifecycles) <= {
            "discovered",
            "evidence_backed",
            "verified",
            "superseded",
        }, arguments
        assert "verified" in lifecycles, arguments
    assert arguments.get("include_superseded", False) is False, arguments
    limit = arguments.get("limit")
    if limit is not None:
        assert isinstance(limit, int) and not isinstance(limit, bool), arguments
        assert 1 <= limit <= 50, arguments


def _structured_knowledge_search(arguments: dict[str, Any]) -> None:
    if not {"year", "manufacturer", "model"} & set(arguments):
        _repository_knowledge_scope(arguments)
        return
    _subset(
        arguments,
        {"year": 2023, "manufacturer": "Chevrolet", "model": "Tahoe"},
    )
    repair_scope = (
        arguments.get("event") or arguments.get("event_type") or arguments.get("query")
    )
    assert isinstance(repair_scope, str) and repair_scope.strip(), arguments


def _normalized_adas_query(arguments: dict[str, Any]) -> dict[str, Any]:
    """Mirror the model-facing query echo without invoking the ADAS SI handler."""

    structured: dict[str, Any] = {}
    vehicle = arguments.get("vehicle")
    if isinstance(vehicle, dict):
        normalized_vehicle: dict[str, Any] = {}
        for key in ("year", "make", "model", "trim", "platform"):
            value = vehicle.get(key)
            if value in (None, ""):
                continue
            normalized_vehicle[key] = (
                value if key == "year" else " ".join(str(value).split())
            )
        if normalized_vehicle:
            structured["vehicle"] = normalized_vehicle
    for key in (
        "system",
        "component",
        "repair_event",
        "requirement_type",
        "question",
    ):
        value = arguments.get(key)
        if value not in (None, ""):
            structured[key] = " ".join(str(value).split())
    structured["search_mode"] = arguments.get("search_mode") or "standard"
    return structured


def _production_adas_scope(
    *,
    year: int,
    make: str,
    model: str,
    repair_events: str | tuple[str, ...] | None,
    technical_scopes: str | tuple[str, ...],
) -> ArgValidator:
    accepted_events = (
        repair_events
        if isinstance(repair_events, tuple)
        else (() if repair_events is None else (repair_events,))
    )
    accepted_scopes = (
        technical_scopes if isinstance(technical_scopes, tuple) else (technical_scopes,)
    )
    event_keys = {value.strip().casefold() for value in accepted_events}
    scope_keys = {value.strip().casefold() for value in accepted_scopes}

    def validate(arguments: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        from core.tools.registry import TOOL_SCHEMAS

        schema = TOOL_SCHEMAS["adas_si_search"]["parameters"]
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        assert not errors, errors[0].message if errors else arguments

        vehicle = arguments.get("vehicle")
        assert isinstance(vehicle, dict), arguments
        assert vehicle.get("year") == year, arguments
        assert str(vehicle.get("make") or "").strip().casefold() == make.casefold(), (
            arguments
        )
        assert str(vehicle.get("model") or "").strip().casefold() == model.casefold(), (
            arguments
        )

        repair_event = arguments.get("repair_event")
        if repair_event is not None:
            assert isinstance(repair_event, str) and repair_event.strip(), arguments
            if event_keys:
                assert repair_event.strip().casefold() in event_keys, arguments

        supplied_scopes = [arguments.get(key) for key in ("system", "component")]
        assert any(
            isinstance(value, str)
            and value.strip()
            and value.strip().casefold() in scope_keys
            for value in supplied_scopes
        ), arguments

        requirement_type = arguments.get("requirement_type")
        if requirement_type is not None:
            assert isinstance(requirement_type, str) and requirement_type.strip(), (
                arguments
            )
        question = arguments.get("question")
        if question is None:
            assert isinstance(repair_event, str) and repair_event.strip(), arguments
            assert isinstance(requirement_type, str) and requirement_type.strip(), (
                arguments
            )
        else:
            assert isinstance(question, str) and question.strip(), arguments
        search_mode = arguments.get("search_mode")
        if search_mode is not None:
            assert search_mode in {"standard", "calibration_requirements"}, arguments

    return validate


_structured_adas_search = _production_adas_scope(
    year=2023,
    make="Chevrolet",
    model="Tahoe",
    repair_events="windshield replacement",
    technical_scopes=(
        "ADAS",
        "forward camera",
        "forward-facing camera",
        "camera",
    ),
)

_structured_adas_procedure_search = _structured_adas_search

_five_turn_adas_procedure_scope = _production_adas_scope(
    year=2023,
    make="Chevrolet",
    model="Tahoe",
    repair_events=None,
    technical_scopes=(
        "ADAS",
        "forward camera",
        "forward-facing camera",
        "camera",
    ),
)


def _summary_scope(arguments: dict[str, Any]) -> None:
    _subset(arguments, {"shop": "Perry", "phase": "5"})
    assert arguments.get("include_completed", False) is False, arguments


def _macon_summary_scope(arguments: dict[str, Any]) -> None:
    assert arguments.get("shop") == "Macon", arguments
    assert arguments.get("include_completed", False) is False, arguments
    assert not arguments.get("phase"), arguments


def _alldata_scope(arguments: dict[str, Any]) -> None:
    from jsonschema import Draft202012Validator

    from core.services.research_alldata_navigation import vehicle_from_query

    tool = next(
        item
        for item in _production_profile_tools()
        if item["function"]["name"] == "collision_research"
    )
    errors = sorted(
        Draft202012Validator(tool["function"]["parameters"]).iter_errors(arguments),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, errors[0].message if errors else arguments
    assert arguments.get("action") == "alldata_vehicle_research", arguments

    vehicle_label = " ".join(str(arguments.get("vehicle") or "").split()).strip()
    parsed_vehicle = vehicle_from_query(vehicle_label) if vehicle_label else {}
    effective_year = arguments.get("vehicle_year") or parsed_vehicle.get("year")
    effective_make = arguments.get("vehicle_make") or parsed_vehicle.get("make")
    effective_model = arguments.get("vehicle_model") or parsed_vehicle.get("model_trim")
    assert str(effective_year or "").strip() == "2023", arguments
    assert str(effective_make or "").strip().casefold() == "chevrolet", arguments
    assert str(effective_model or "").strip().casefold() == "tahoe", arguments
    assert isinstance(arguments.get("topic"), str) and arguments["topic"].strip(), (
        arguments
    )


def _destructive_scope(arguments: dict[str, Any]) -> None:
    _subset(
        arguments,
        {
            "actions": [
                {
                    "operation": "delete_blocker",
                    "target_id": "blk-9",
                    "expected_version": 12,
                }
            ]
        },
    )
    repair_order_id = arguments["actions"][0].get("repair_order_id")
    if repair_order_id is not None:
        assert repair_order_id in {"ro-uuid-17", "2400911724"}, arguments


def _exact_ro_scope(*accepted_ids: str) -> ArgValidator:
    accepted = frozenset(accepted_ids)

    def validate(arguments: dict[str, Any]) -> None:
        assert arguments.get("repair_order_id") in accepted, arguments

    return validate


def _board_scope(*, shop: str) -> ArgValidator:
    def validate(arguments: dict[str, Any]) -> None:
        assert arguments.get("shop") == shop, arguments
        assert arguments.get("include_completed", False) is False, arguments
        assert not arguments.get("status"), arguments

    return validate


def _week_readiness_scope(arguments: dict[str, Any]) -> None:
    assert arguments.get("mode") == "week_readiness", arguments
    assert not arguments.get("repair_order_id"), arguments
    assert not arguments.get("phase"), arguments


def _phase_list_scope(arguments: dict[str, Any]) -> None:
    from jsonschema import Draft202012Validator

    from core.tools.registry import TOOL_SCHEMAS

    schema = TOOL_SCHEMAS["calibration_iq_work_prep"]["parameters"]
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, errors[0].message if errors else arguments
    _subset(arguments, {"mode": "phase_list", "phase": "5", "shop": "Perry"})
    assert not arguments.get("repair_order_id"), arguments
    assert not arguments.get("coverage_focus"), arguments


def _ro_requirements_scope(*accepted_ids: str) -> ArgValidator:
    accepted = frozenset(accepted_ids)

    def validate(arguments: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        from core.tools.registry import TOOL_SCHEMAS

        schema = TOOL_SCHEMAS["calibration_iq_work_prep"]["parameters"]
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        assert not errors, errors[0].message if errors else arguments
        assert arguments.get("mode") == "ro_requirements", arguments
        assert arguments.get("repair_order_id") in accepted, arguments
        assert not arguments.get("phase"), arguments
        assert not arguments.get("shop"), arguments

    return validate


def _close_ro_scope(*accepted_ids: str, expected_version: int) -> ArgValidator:
    accepted = frozenset(accepted_ids)

    def validate(arguments: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        from core.tools.registry import TOOL_SCHEMAS

        schema = TOOL_SCHEMAS["calibration_iq_operator"]["parameters"]
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        assert not errors, errors[0].message if errors else arguments
        actions = arguments.get("actions")
        assert isinstance(actions, list) and len(actions) == 1, arguments
        action = actions[0]
        assert action.get("operation") == "close_ro", arguments
        assert action.get("repair_order_id") in accepted, arguments
        assert action.get("expected_version") == expected_version, arguments

    return validate


def _field_adas_scope(
    *,
    year: int,
    make: str,
    model: str,
    repair_event: str | tuple[str, ...],
    component: str | tuple[str, ...],
) -> ArgValidator:
    return _production_adas_scope(
        year=year,
        make=make,
        model=model,
        repair_events=repair_event,
        technical_scopes=component,
    )


def _field_knowledge_scope(
    *,
    year: int,
    make: str,
    model: str,
    event: str | tuple[str, ...],
    component: str,
) -> ArgValidator:
    def validate(arguments: dict[str, Any]) -> None:
        if not {"year", "manufacturer", "model"} & set(arguments):
            _repository_knowledge_scope(arguments)
            return
        _subset(
            arguments,
            {"year": year, "manufacturer": make, "model": model},
        )
        selected_event = arguments.get("event") or arguments.get("event_type")
        accepted_events = frozenset(event) if isinstance(event, tuple) else {event}
        assert selected_event in accepted_events, arguments
        selected_component = arguments.get("component") or arguments.get("system")
        assert selected_component == component, arguments

    return validate


def _adas_open_scope(*, relative_path: str, page: int) -> ArgValidator:
    def validate(arguments: dict[str, Any]) -> None:
        _subset(arguments, {"relative_path": relative_path, "page": page})

    return validate


def _research_ro_scope(*, ro_id: str, expected_version: int) -> ArgValidator:
    def validate(arguments: dict[str, Any]) -> None:
        actions = arguments.get("actions")
        assert isinstance(actions, list) and len(actions) == 1, arguments
        _subset(
            actions[0],
            {
                "operation": "research_ro",
                "repair_order_id": ro_id,
            },
            "arguments.actions[0]",
        )
        supplied_version = actions[0].get("expected_version")
        if supplied_version is not None:
            assert supplied_version == expected_version, arguments

    return validate


@dataclass(frozen=True)
class CallExpectation:
    name: str
    result: dict[str, Any]
    subset: dict[str, Any] = field(default_factory=dict)
    validator: ArgValidator | None = None

    def check(self, arguments: dict[str, Any]) -> None:
        _subset(arguments, self.subset)
        if self.validator:
            self.validator(arguments)

    def result_for(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(self.result)
        if self.name == "adas_si_search":
            # Production AdasSI.model_search echoes the normalized structured
            # query on every dict result, including a source-bounded miss.
            result["structured_query"] = _normalized_adas_query(arguments)
        return result


@dataclass(frozen=True)
class ReportExpectation:
    subset: dict[str, Any]
    required_sources: frozenset[str] = frozenset()
    required_evidence_ids: frozenset[str] = frozenset()
    path_requirements: tuple[
        tuple[tuple[str, ...], frozenset[str], frozenset[str]], ...
    ] = ()

    def check(
        self, report: dict[str, Any], *, call_path: tuple[str, ...] = ()
    ) -> None:
        _subset(report, self.subset, "acceptance_report")
        required_sources = self.required_sources
        required_evidence_ids = self.required_evidence_ids
        for expected_path, path_sources, path_evidence_ids in self.path_requirements:
            if call_path == expected_path:
                required_sources = path_sources
                required_evidence_ids = path_evidence_ids
                break
        actual_sources = set(report.get("sources_checked") or [])
        assert required_sources <= actual_sources, (
            f"acceptance_report.sources_checked missing "
            f"{sorted(required_sources - actual_sources)!r}: {report!r}"
        )
        actual_ids = set(report.get("observed_evidence_ids") or [])
        assert required_evidence_ids <= actual_ids, (
            f"acceptance_report.observed_evidence_ids missing "
            f"{sorted(required_evidence_ids - actual_ids)!r}: {report!r}"
        )


@dataclass(frozen=True)
class Turn:
    user: str
    calls: tuple[CallExpectation, ...]
    report: ReportExpectation
    alternative_calls: tuple[tuple[CallExpectation, ...], ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    turns: tuple[Turn, ...]
    initial_subject: dict[str, Any] | None = None
    category: str = "existing_model_first"
    negative_truths: frozenset[str] = frozenset()


# These identifiers are metadata, not language classifiers.  They make the
# acceptance contract reviewable without deriving expectations from the user's
# wording.  Each field scenario below declares its category and any negative
# truth boundary it proves alongside its structured tool path.
FIELD_ACCEPTANCE_CATEGORIES = frozenset(
    {
        "exact_ro",
        "current_calibration_state",
        "technical_evidence",
        "document_request",
        "follow_up_reference",
        "operational_mutation",
        "shop_work",
        "weekly_work",
        "natural_technician_language",
        "ambiguous_reference",
        "explicit_context_switch",
        "multi_step",
    }
)
NEGATIVE_TRUTH_CONTRACTS = frozenset(
    {
        "no_invented_data",
        "no_stale_context_as_current_ro_state",
        "no_adas_si_as_current_ciq_state",
        "no_ciq_state_as_oem_proof",
        "no_invented_batch_ids",
        "no_false_acquisition_success",
        "no_unreceipted_mutation_success",
        "no_hidden_zero_run_mixed_batch",
        "no_magic_wording",
        "no_phrase_routing",
    }
)
NEGATIVE_TRUTH_DESCRIPTIONS = {
    "no_invented_data": "No fact or record absent from the observed fixture results is claimed.",
    "no_stale_context_as_current_ro_state": (
        "Durable context is not presented as a fresh mutable RO snapshot."
    ),
    "no_adas_si_as_current_ciq_state": (
        "ADAS SI evidence is not presented as the currently saved CIQ calibration state."
    ),
    "no_ciq_state_as_oem_proof": (
        "A CIQ workflow or requirement is not presented as proof of an OEM rule."
    ),
    "no_invented_batch_ids": "Every opaque ScrapeX id came from an observed fixture result.",
    "no_false_acquisition_success": (
        "Acquisition is not called successful unless verified complete by its result."
    ),
    "no_unreceipted_mutation_success": (
        "A mutation is not called successful without a verified receipt and agreeing reread."
    ),
    "no_hidden_zero_run_mixed_batch": (
        "If one mixed Calibration IQ batch was rejected before execution and a later "
        "research-only call succeeded, the response explicitly says the mixed batch ran "
        "nothing and limits verified success to the later research attachment; it never "
        "claims an add_calibration action succeeded."
    ),
    "no_magic_wording": "The answer does not require or teach a special command phrase.",
    "no_phrase_routing": (
        "The response does not claim or imply that a wording classifier selected the path."
    ),
}

LIVE_FAILURE_CLASSIFICATION = {
    "capability_read_write_boundary": "production_rejected_mutation_instead_of_capability_read",
    "durable_subject_and_close_ro_mapping": (
        "production_rejected_missing_identity_and_version"
    ),
    "destructive_write_stops_for_approval": "harness_optional_ro_context",
    "existing_evidence_escalates_without_inventing_answer": (
        "harness_repository_wide_knowledge_scope"
    ),
    "durable_knowledge_supplies_missing_local_answer": (
        "harness_repository_wide_knowledge_scope"
    ),
    "field_why_radar_calibration_needs_oem_evidence": (
        "harness_valid_adas_query_shape"
    ),
    "field_weekly_work_readiness": "production_rejected_calendar_ambiguity",
    "field_multi_step_toyota_procedures_to_case": (
        "harness_valid_adas_query_and_multistep_path"
    ),
    "negative_close_indeterminate_is_not_success": (
        "production_rejected_missing_identity_and_version"
    ),
    "close_paraphrase": "production_rejected_missing_identity_and_version",
}

FINAL_TRUTH_AUDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "acceptance_final_truth_audit",
        "description": (
            "Test-only semantic audit of the final user-facing response against declared "
            "negative truth contracts and observed fixture results."
        ),
        "parameters": _object_schema(
            {
                "unsupported_claim": {"type": "boolean"},
                "violated_contracts": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": sorted(NEGATIVE_TRUTH_CONTRACTS),
                    },
                    "uniqueItems": True,
                },
                "rationale": {"type": "string"},
            },
            ("unsupported_claim", "violated_contracts", "rationale"),
        ),
    },
}


SUBJECT_PAYLOAD = {
    "type": "calibration_iq.repair_order",
    "resource_id": "ro-uuid-17",
    "repair_order_id": "ro-uuid-17",
    "ro_number": "2400911724",
    "subject_scope": "identity_and_workflow_context_only",
    "current_calibration_detail_included": False,
    "next_capability_for_current_ro_detail": "calibration_iq_ro",
    "repair_order": {
        "id": "ro-uuid-17",
        "ro_number": "2400911724",
        "status": "calibration_in_progress",
        "phase": 6,
        "version": 12,
    },
    "vehicle": {
        "year": 2023,
        "make": "Chevrolet",
        "model": "Tahoe",
        "vin": "1GNSKTEST00000017",
    },
    "shop": {"id": "shop-perry", "name": "Perry"},
}
SUBJECT = {
    "version": 1,
    "updated_at": "2026-08-25T20:00:00+00:00",
    "source_tool_name": "calibration_iq_ro",
    "payload": SUBJECT_PAYLOAD,
}

RO_RESULT = {
    "status": "verified",
    "evidence_id": "ro-snapshot-12",
    "repair_order": {
        "id": "ro-uuid-17",
        "RO": "2400911724",
        "Vehicle": "2023 Chevrolet Tahoe",
        "Shop": "Perry",
        "Phase": 6,
        "version": 12,
        "Status": "calibration_in_progress",
    },
    "raw": {
        "repair_order": {
            "id": "ro-uuid-17",
            "ro_number": "2400911724",
            "year": 2023,
            "make": "Chevrolet",
            "model": "Tahoe",
            "vin": "1GNSKTEST00000017",
            "version": 12,
        },
        "shop": {"id": "shop-perry", "name": "Perry"},
        "workflow": {
            "status": "calibration_in_progress",
            "phase": 6,
            "version": 12,
        },
    },
}


def _operator_result(
    *,
    ro_id: str = "ro-uuid-17",
    ro_number: str = "2400911724",
    version: int = 13,
    evidence_id: str = "close-receipt-13",
    operation: str = "close_ro",
    status: str = "complete",
    make: str = "Chevrolet",
    model: str = "Tahoe",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "success",
        "executed": True,
        "success": True,
        "verified": True,
        "partial": False,
        "requested_count": 1,
        "processed_count": 1,
        "stopped_on_error": False,
        "evidence_id": evidence_id,
        "receipts": [
            {
                "operation": operation,
                "repair_order_id": ro_id,
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            }
        ],
        "final_snapshots": {
            ro_id: {
                "status": "verified",
                "snapshot": {
                    "repair_order": {
                        "id": ro_id,
                        "ro_number": ro_number,
                        "year": 2023,
                        "make": make,
                        "model": model,
                        "vin": "1GNSKTEST00000017",
                        "version": version,
                    },
                    "shop": {"id": "shop-perry", "name": "Perry"},
                    "workflow": {
                        "status": status,
                        "phase": 8 if status == "complete" else 6,
                        "version": version,
                    },
                },
            }
        },
    }
    if operation == "close_ro":
        result.update(
            verified_effect_scope="repair_order_workflow_closure",
            child_calibration_state_included=False,
            child_calibration_completion_proven=False,
        )
    if operation == "research_ro":
        snapshot = result["final_snapshots"][ro_id]["snapshot"]
        snapshot["calibrations"] = [
            {
                "id": "cal-fcm-1",
                "system": "forward camera",
                "status": "required",
                "document_ids": ["doc-fcm-procedure-1"],
            }
        ]
        snapshot["documents"] = [
            {
                "id": "doc-fcm-procedure-1",
                "title": "Forward Camera Learn Procedure",
                "page_references": [9],
                "status": "validated",
            }
        ]
        result["research_reports"] = [
            {
                "repair_order_id": ro_id,
                "documents_prepared": ["doc-fcm-procedure-1"],
                "missing_documentation": [],
                "complete_research_requested": False,
            }
        ]
    return result


def _ro_result_for(
    *,
    ro_id: str,
    ro_number: str,
    version: int,
    evidence_id: str,
    year: int = 2023,
    make: str = "Chevrolet",
    model: str = "Tahoe",
    shop: str = "Perry",
    vin: str = "1GNSKTEST00000017",
) -> dict[str, Any]:
    result = json.loads(json.dumps(RO_RESULT))
    result["evidence_id"] = evidence_id
    result["repair_order"].update(
        id=ro_id,
        RO=ro_number,
        Vehicle=f"{year} {make} {model}",
        Shop=shop,
        version=version,
    )
    result["raw"]["repair_order"].update(
        id=ro_id,
        ro_number=ro_number,
        year=year,
        make=make,
        model=model,
        vin=vin,
        version=version,
    )
    result["raw"]["shop"].update(name=shop)
    result["raw"]["workflow"]["version"] = version
    return result


def _subject_for(
    *,
    ro_id: str,
    ro_number: str,
    version: int,
    year: int,
    make: str,
    model: str,
    shop: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "2026-08-26T12:00:00+00:00",
        "source_tool_name": "calibration_iq_ro",
        "payload": {
            "type": "calibration_iq.repair_order",
            "resource_id": ro_id,
            "repair_order_id": ro_id,
            "ro_number": ro_number,
            "subject_scope": "identity_and_workflow_context_only",
            "current_calibration_detail_included": False,
            "next_capability_for_current_ro_detail": "calibration_iq_ro",
            "repair_order": {
                "id": ro_id,
                "ro_number": ro_number,
                "status": "calibration_in_progress",
                "phase": 6,
                "version": version,
            },
            "vehicle": {"year": year, "make": make, "model": model},
            "shop": {"name": shop},
        },
    }


class _FixtureSubjectStore:
    """Isolated store adapter that exercises the production subject merge hook."""

    def __init__(self, subject: dict[str, Any] | None) -> None:
        self._row = deepcopy(subject)

    def get_conversation_subject(
        self,
        conversation_id: int,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        del conversation_id, user_id
        return deepcopy(self._row)

    def set_conversation_subject(
        self,
        conversation_id: int,
        subject: dict[str, Any],
        *,
        source_tool_name: str,
        source_tool_call_id: str | None = None,
        source_message_id: int | None = None,
        user_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        del conversation_id, user_id
        current_version = int((self._row or {}).get("version") or 0)
        if expected_version is not None and expected_version != current_version:
            from core.state.db import ConversationSubjectConflict

            raise ConversationSubjectConflict(
                "fixture subject changed before the production merge committed"
            )
        next_version = current_version + 1
        self._row = {
            "version": next_version,
            "updated_at": "2026-08-26T20:00:00+00:00",
            "source_tool_name": source_tool_name,
            "source_tool_call_id": source_tool_call_id,
            "source_message_id": source_message_id,
            "payload": deepcopy(subject),
        }
        return deepcopy(self._row)


CALIBRATION_RO_RESULT = json.loads(json.dumps(RO_RESULT))
CALIBRATION_RO_RESULT.update(evidence_id="ro-calibrations-12")
CALIBRATION_RO_RESULT["raw"]["calibrations"] = [
    {
        "id": "cal-fcm-1",
        "system": "forward camera",
        "calibration_type": "static/dynamic aiming",
        "status": "required",
        "evidence": {"source": "OEM procedure", "verified": True},
    }
]

PARAPHRASE_SUBJECT = json.loads(json.dumps(SUBJECT))
PARAPHRASE_SUBJECT["payload"].update(
    resource_id="ro-uuid-1478",
    repair_order_id="ro-uuid-1478",
    ro_number="2400611478",
)
PARAPHRASE_SUBJECT["payload"]["repair_order"].update(
    id="ro-uuid-1478",
    ro_number="2400611478",
)
PARAPHRASE_RO_RESULT = _ro_result_for(
    ro_id="ro-uuid-1478",
    ro_number="2400611478",
    version=12,
    evidence_id="ro-calibrations-1478",
)
PARAPHRASE_RO_RESULT["raw"]["calibrations"] = json.loads(
    json.dumps(CALIBRATION_RO_RESULT["raw"]["calibrations"])
)
RO_NO_PROCEDURE_RESULT = _ro_result_for(
    ro_id="ro-uuid-1478",
    ro_number="2400611478",
    version=12,
    evidence_id="five-turn-ciq-procedure-miss",
)
RO_NO_PROCEDURE_RESULT["raw"].update(
    documents=[],
    research={"documents": [], "procedure_evidence_found": False},
)

NISSAN_SUBJECT = _subject_for(
    ro_id="ro-nissan-1667",
    ro_number="2400911667",
    version=21,
    year=2023,
    make="Nissan",
    model="Rogue",
    shop="Macon",
)
NISSAN_RO_RESULT = _ro_result_for(
    ro_id="ro-nissan-1667",
    ro_number="2400911667",
    version=21,
    evidence_id="ciq-nissan-current-21",
    year=2023,
    make="Nissan",
    model="Rogue",
    shop="Macon",
    vin="5N1AT3TEST00001667",
)
NISSAN_RO_RESULT["raw"].update(
    calibrations=[
        {
            "id": "cal-radar-nissan-1",
            "system": "front radar",
            "calibration_type": "radar aiming",
            "status": "required",
            "prerequisites": ["wheel alignment complete"],
        }
    ],
    blockers=[
        {
            "id": "blk-alignment-nissan-1",
            "type": "wheel_alignment",
            "status": "open",
        }
    ],
    repair_events=[
        {"type": "front bumper replacement", "status": "bumper reinstalled"},
        {"type": "wheel alignment", "status": "not completed"},
    ],
    documents=[],
)

# This document-follow-up models a recent prior exact-RO result, not merely the
# identity-only subject used by the other standalone field scenarios. Build its
# compact authoritative calibration context through the production tracker.
NISSAN_PROCEDURE_CONTEXT_RESULT = deepcopy(NISSAN_RO_RESULT)
NISSAN_PROCEDURE_CONTEXT_RESULT["evidence_id"] = (
    "ciq-nissan-procedure-prior-context-21"
)
for _omitted_procedure_context_key in ("blockers", "documents", "repair_events"):
    NISSAN_PROCEDURE_CONTEXT_RESULT["raw"].pop(
        _omitted_procedure_context_key,
        None,
    )

def _tracked_prior_exact_ro_subject(
    subject: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    from core.services.conversation_subjects import (
        track_active_subject_from_tool_result,
    )

    tracked = track_active_subject_from_tool_result(
        _FixtureSubjectStore(subject),
        conversation_id=1,
        tool_name="calibration_iq_ro",
        result=result,
        tool_call_id="prior-ciq-nissan-procedure-context",
    )
    assert tracked is not None
    return tracked


NISSAN_PROCEDURE_SUBJECT = _tracked_prior_exact_ro_subject(
    NISSAN_SUBJECT,
    NISSAN_PROCEDURE_CONTEXT_RESULT,
)

TOYOTA_SUBJECT = _subject_for(
    ro_id="ro-toyota-1478",
    ro_number="2400611478",
    version=7,
    year=2024,
    make="Toyota",
    model="Camry",
    shop="Perry",
)
TOYOTA_RO_RESULT = _ro_result_for(
    ro_id="ro-toyota-1478",
    ro_number="2400611478",
    version=7,
    evidence_id="ciq-toyota-current-7",
    year=2024,
    make="Toyota",
    model="Camry",
    shop="Perry",
    vin="4T1C11AKTEST001478",
)
TOYOTA_RO_RESULT["raw"].update(
    calibrations=[
        {
            "id": "cal-camera-toyota-1",
            "system": "forward camera",
            "calibration_type": "forward recognition camera adjustment",
            "status": "required",
        }
    ],
    repair_events=[{"type": "windshield replacement", "status": "completed"}],
    documents=[],
    research={"documents": [], "procedure_evidence_found": False},
)


def _exact_ro_lookup_call(
    *,
    ro_id: str,
    ro_number: str,
    evidence_id: str,
    vehicle: str,
    shop: str,
) -> CallExpectation:
    def exact_query(arguments: dict[str, Any]) -> None:
        assert arguments.get("q") == ro_number, arguments
        assert arguments.get("include_completed", False) is False, arguments

    return CallExpectation(
        "calibration_iq_read",
        {
            "status": "verified",
            "verified": True,
            "evidence_id": evidence_id,
            "query": ro_number,
            "count": 1,
            "result_scope": "board_list_only",
            "exact_ro_detail_included": False,
            "next_capability_for_one_ro_detail": "calibration_iq_ro",
            "rows": [
                {
                    "id": ro_id,
                    "RO": ro_number,
                    "Vehicle": vehicle,
                    "Shop": shop,
                    "Status": "calibration_in_progress",
                }
            ],
            "collection_complete": True,
        },
        validator=exact_query,
    )


def _lookup_then_exact_ro(
    ro_call: CallExpectation,
    *,
    ro_id: str,
    ro_number: str,
    evidence_id: str,
    vehicle: str,
    shop: str,
) -> tuple[CallExpectation, ...]:
    assert ro_call.name == "calibration_iq_ro"
    return (
        _exact_ro_lookup_call(
            ro_id=ro_id,
            ro_number=ro_number,
            evidence_id=evidence_id,
            vehicle=vehicle,
            shop=shop,
        ),
        CallExpectation(
            "calibration_iq_ro",
            ro_call.result,
            validator=_exact_ro_scope(ro_id, ro_number),
        ),
    )


def _five_turn_list_call() -> CallExpectation:
    def scope(arguments: dict[str, Any]) -> None:
        assert arguments.get("q") == "2400611478", arguments
        assert arguments.get("include_completed", False) is False, arguments

    return CallExpectation(
        "calibration_iq_read",
        {
            "status": "verified",
            "evidence_id": "five-turn-list-detour",
            "count": 1,
            "result_scope": "board_list_only",
            "exact_ro_detail_included": False,
            "next_capability_for_one_ro_detail": "calibration_iq_ro",
            "rows": [
                {
                    "id": "ro-uuid-1478",
                    "RO": "2400611478",
                    "Status": "calibration_in_progress",
                }
            ],
            "collection_complete": True,
        },
        validator=scope,
    )


def _five_turn_ro_procedure_call() -> CallExpectation:
    def exact_subject(arguments: dict[str, Any]) -> None:
        assert arguments.get("repair_order_id") in {"ro-uuid-1478", "2400611478"}, (
            arguments
        )

    return CallExpectation(
        "calibration_iq_ro",
        RO_NO_PROCEDURE_RESULT,
        validator=exact_subject,
    )


def _five_turn_knowledge_call() -> CallExpectation:
    return CallExpectation(
        "automotive_knowledge_search",
        {
            "status": "no_result",
            "source": "durable_automotive_knowledge",
            "source_bounded": True,
            "evidence_id": "five-turn-knowledge-miss",
            "records": [],
        },
        validator=_structured_knowledge_search,
    )


def _five_turn_adas_procedure_call() -> CallExpectation:
    relative_path = "Chevrolet/Tahoe/2023/ADAS/Forward Camera Learn Procedure.pdf"
    return CallExpectation(
        "adas_si_search",
        {
            "status": "verified",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": "five-turn-oem-procedure",
            "results": [
                {
                    "document": "Forward Camera Learn Procedure",
                    "relative_path": relative_path,
                    "page": 9,
                    "finding": "OEM forward camera aiming procedure.",
                    "vehicle": {
                        "year": 2023,
                        "make": "Chevrolet",
                        "model": "Tahoe",
                    },
                }
            ],
        },
        validator=_five_turn_adas_procedure_scope,
    )


def _five_turn_procedure_open_call() -> CallExpectation:
    relative_path = "Chevrolet/Tahoe/2023/ADAS/Forward Camera Learn Procedure.pdf"
    return CallExpectation(
        "adas_si_open",
        {
            "status": "verified",
            "source": "adas_si",
            "evidence_id": "five-turn-oem-procedure-open",
            "relative_path": relative_path,
            "page": 9,
            "displayed": True,
            "document": {
                "title": "Forward Camera Learn Procedure",
                "relative_path": relative_path,
                "page": 9,
                "vehicle": {
                    "year": 2023,
                    "make": "Chevrolet",
                    "model": "Tahoe",
                },
            },
        },
        {"relative_path": relative_path, "page": 9},
        validator=_adas_open_scope(relative_path=relative_path, page=9),
    )


def _five_turn_adas_calibration_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_search",
        {
            "status": "verified",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": "five-turn-calibration-answer",
            "results": [
                {
                    "document": "Forward Camera Learn Procedure",
                    "page": 9,
                    "finding": "Forward camera aiming is required.",
                }
            ],
        },
        validator=_structured_adas_search,
    )


def _five_turn_calibration_ro_call() -> CallExpectation:
    def exact_subject(arguments: dict[str, Any]) -> None:
        assert arguments.get("repair_order_id") in {"ro-uuid-1478", "2400611478"}, (
            arguments
        )

    return CallExpectation(
        "calibration_iq_ro",
        PARAPHRASE_RO_RESULT,
        validator=exact_subject,
    )


def _five_turn_research_attach_result() -> dict[str, Any]:
    ro_id = "ro-uuid-1478"
    calibration_id = "cal-fcm-1"
    document_id = "doc-five-turn-fcm-procedure-1"
    relative_path = "Chevrolet/Tahoe/2023/ADAS/Forward Camera Learn Procedure.pdf"
    snapshot = deepcopy(PARAPHRASE_RO_RESULT["raw"])
    snapshot["documents"] = [
        {
            "id": document_id,
            "version": 1,
            "title": "Forward Camera Learn Procedure",
            "document_type": "oem_procedure",
            "status": "validated",
            "source_uri": f"adas-si:///{relative_path}",
            "source_name": "Forward Camera Learn Procedure.pdf",
            "storage_relative_path": "OEM Procedures/Forward Camera Learn Procedure.pdf",
            "page_references": ["p. 9"],
            "calibration_item_ids": [calibration_id],
        }
    ]
    snapshot["research"] = {
        "state": "research_in_progress",
        "version": 3,
        "documents": deepcopy(snapshot["documents"]),
    }
    research = {
        "repair_order_id": ro_id,
        "vehicle": "2023 Chevrolet Tahoe",
        "required_calibrations": [
            {"id": calibration_id, "label": "static/dynamic aiming"}
        ],
        "findings": [
            {
                "calibration_id": calibration_id,
                "calibration": "static/dynamic aiming",
                "query": "2023 Chevrolet Tahoe forward camera aiming",
                "status": "verified",
                "supported": True,
                "documents": [
                    {
                        "title": "Forward Camera Learn Procedure",
                        "source": "Forward Camera Learn Procedure.pdf",
                        "relative_path": relative_path,
                        "pages": [9],
                    }
                ],
                "missing_reason": None,
            }
        ],
        "documents_prepared": [
            {
                "title": "Forward Camera Learn Procedure",
                "source": "Forward Camera Learn Procedure.pdf",
                "relative_path": relative_path,
                "pages": [9],
                "calibration_item_ids": [calibration_id],
                "status": "validated",
            }
        ],
        "already_present": [],
        "missing_documents": [],
        "research_complete_requested": False,
        "research_complete_eligible": True,
        "research_complete_action_added": False,
        "research_complete_was_already_set": False,
        "research_complete_already_verified": False,
        "completion_withheld": False,
    }
    return {
        "status": "success",
        "executed": True,
        "success": True,
        "verified": True,
        "partial": False,
        "requested_count": 2,
        "processed_count": 2,
        "stopped_on_error": False,
        "receipts": [
            {
                "mutation_id": "mut-five-turn-workspace-1",
                "operation": "ensure_case_workspace",
                "repair_order_id": ro_id,
                "resource_type": "case_workspace",
                "resource_id": "case-workspace-five-turn-1478",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            },
            {
                "mutation_id": "mut-five-turn-document-import-1",
                "operation": "import_document",
                "repair_order_id": ro_id,
                "resource_type": "document",
                "resource_id": document_id,
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            },
        ],
        "final_snapshots": {ro_id: {"status": "verified", "snapshot": snapshot}},
        "research": [research],
        "missing_documentation": [],
        "message": (
            "Calibration IQ confirmed every action and the authoritative final state."
        ),
    }


def _five_turn_research_attach_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_operator",
        _five_turn_research_attach_result(),
        validator=_research_ro_scope(ro_id="ro-uuid-1478", expected_version=12),
    )


def _existing_ciq_ro_call() -> CallExpectation:
    result = json.loads(json.dumps(RO_RESULT))
    result["evidence_id"] = "existing-chain-ciq-ro"
    result["raw"].update(calibrations=[], documents=[], research={"documents": []})
    return CallExpectation(
        "calibration_iq_ro",
        result,
        {"repair_order_id": "ro-uuid-17"},
    )


def _destructive_refresh_call() -> CallExpectation:
    result = json.loads(json.dumps(RO_RESULT))
    result["evidence_id"] = "ciq-pre-destructive-refresh-12"
    result["raw"]["blockers"] = [
        {
            "id": "blk-9",
            "version": 12,
            "status": "open",
            "type": "workflow",
        }
    ]
    return CallExpectation(
        "calibration_iq_ro",
        result,
        {"repair_order_id": "ro-uuid-17"},
    )


def _destructive_delete_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_destructive",
        {
            "status": "approval_required",
            "success": False,
            "attempted": False,
            "executed": False,
            "verified": False,
            "evidence_id": "approval-boundary-blk-9",
            "approval": {
                "operation": "delete_blocker",
                "target_id": "blk-9",
            },
        },
        {
            "actions": [
                {
                    "operation": "delete_blocker",
                    "target_id": "blk-9",
                    "expected_version": 12,
                }
            ]
        },
        validator=_destructive_scope,
    )


def _calibration_iq_status_call() -> CallExpectation:
    def no_arguments(arguments: dict[str, Any]) -> None:
        assert arguments == {}, arguments

    return CallExpectation(
        "calibration_iq_status",
        {
            "status": "success",
            "service": "calibration_iq",
            "configured": True,
            "reachable": True,
            "capability_catalog_required_for_policy": True,
        },
        validator=no_arguments,
    )


def _scrapex_status_call() -> CallExpectation:
    def no_arguments(arguments: dict[str, Any]) -> None:
        assert arguments == {}, arguments

    return CallExpectation(
        "scrapex_status",
        {
            "status": "success",
            "service": "ScrapeX",
            "reachable": True,
            "calibration_iq_reachable": True,
            "managed_browser": {
                "authentication_state": "unknown_until_acquisition",
                "human_sign_in_supported": True,
            },
        },
        validator=no_arguments,
    )


def _alldata_provider_ready_call() -> CallExpectation:
    def no_arguments(arguments: dict[str, Any]) -> None:
        assert arguments == {}, arguments

    return CallExpectation(
        "research_provider_setup",
        {
            "status": "success",
            "provider": "licensed_alldata",
            "configured": True,
            "authenticated": True,
            "setup_in_chat": True,
            "credential_secret_in_model_context": False,
        },
        validator=no_arguments,
    )


def _capability_catalog_call() -> CallExpectation:
    return CallExpectation(
        "assistant_capabilities_read",
        {
            "status": "success",
            "evidence_id": "capability-catalog-live-1",
            "resources": {
                "calibration_iq": {
                    "read": True,
                    "routine_write": "operator_authorized",
                    "destructive_write": "approval_required",
                }
            },
        },
    )


def _perry_phase_list_call() -> CallExpectation:
    vehicles = (
        "2023 Chevrolet Tahoe",
        "2024 Toyota Camry",
        "2023 Nissan Rogue",
        "2024 Honda CR-V",
        "2022 Ford F-150",
        "2024 Subaru Outback",
        "2023 Hyundai Tucson",
    )
    rows = [
        {
            "RO": f"24005{index:04d}",
            "Vehicle": vehicle,
            "Status": "calibration_in_progress",
            "Shop": "Perry",
            "Phase": 5,
            "id": f"ro-perry-phase5-{index}",
        }
        for index, vehicle in enumerate(vehicles, start=1)
    ]
    return CallExpectation(
        "calibration_iq_work_prep",
        {
            "mode": "phase_list",
            "status": "verified",
            "count": 7,
            "active_count": 7,
            "completed_count": 0,
            "include_completed": False,
            "terminal_only": False,
            "scope": "active work only",
            "filters": {"phase": "5", "limit": 100, "shop": "Perry"},
            "breakdown": {
                "by_status": {"calibration_in_progress": 7},
                "by_phase": {"5": 7},
                "by_shop": {"Perry": 7},
            },
            "collection": {
                "complete": True,
                "collection_capped": False,
                "upstream_total": 7,
                "duplicate_count": 0,
            },
            "collection_complete": True,
            "collection_capped": False,
            "upstream_total": 7,
            "duplicate_count": 0,
            "evidence": {
                "source": "calibration_iq_authenticated_api",
                "read_only": True,
            },
            "rows": rows,
            "shown_count": 7,
            "truncated": False,
            "result_scope": "board_list_only",
            "exact_ro_detail_included": False,
            "next_capability_for_one_ro_detail": "calibration_iq_ro",
        },
        validator=_phase_list_scope,
    )


def _alldata_research_call() -> CallExpectation:
    return CallExpectation(
        "collision_research",
        {
            "status": "success",
            "verified": True,
            "source": "licensed_alldata",
            "evidence_id": "alldata-evidence-tahoe-1",
            "citations": [
                {
                    "title": "Forward camera service procedure",
                    "section": "Calibration prerequisites",
                }
            ],
        },
        validator=_alldata_scope,
    )


def _macon_current_summary_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_summary",
        {
            "status": "success",
            "verified": True,
            "evidence_id": "ciq-macon-active-current",
            "scope": {"shop": "Macon", "include_completed": False},
            "count": 2,
            "result_scope": "aggregate_only",
            "repair_order_rows_included": False,
        },
        validator=_macon_summary_scope,
    )


def _durable_knowledge_hit_call() -> CallExpectation:
    return CallExpectation(
        "automotive_knowledge_search",
        {
            "status": "verified",
            "source": "durable_automotive_knowledge",
            "evidence_id": "knowledge-hit-tahoe-camera-1",
            "records": [
                {
                    "id": "knowledge-tahoe-camera-1",
                    "lifecycle": "verified",
                    "source_integrity": {
                        "status": "current",
                        "verified_read_allowed": True,
                    },
                    "application": {
                        "year_start": 2023,
                        "year_end": 2023,
                        "manufacturer": "Chevrolet",
                        "model": "Tahoe",
                    },
                    "finding": (
                        "Forward camera aiming is required after windshield replacement."
                    ),
                    "source": {
                        "name": "Chevrolet service procedure",
                        "page": 14,
                    },
                }
            ],
        },
        validator=_structured_knowledge_search,
    )


def _existing_knowledge_miss_call() -> CallExpectation:
    return CallExpectation(
        "automotive_knowledge_search",
        {
            "status": "no_result",
            "source": "durable_automotive_knowledge",
            "source_bounded": True,
            "evidence_id": "knowledge-miss-tahoe-1",
            "records": [],
        },
        validator=_structured_knowledge_search,
    )


def _existing_adas_miss_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_search",
        {
            "status": "no_result",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": "adas-si-miss-tahoe-1",
            "results": [],
        },
        validator=_structured_adas_search,
    )


def _existing_adas_retry_miss_call(
    *, search_mode: str, requirement_type: str
) -> CallExpectation:
    assert search_mode in {"standard", "calibration_requirements"}
    requirement_key = requirement_type.strip().casefold()

    def validate(arguments: dict[str, Any]) -> None:
        _structured_adas_search(arguments)
        vehicle = arguments.get("vehicle")
        assert isinstance(vehicle, dict), arguments
        assert vehicle.get("year") == 2023, arguments
        assert str(vehicle.get("make") or "").strip().casefold() == "chevrolet", (
            arguments
        )
        assert str(vehicle.get("model") or "").strip().casefold() == "tahoe", (
            arguments
        )
        assert str(arguments.get("system") or "").strip().casefold() == (
            "forward camera"
        ), arguments
        assert str(arguments.get("repair_event") or "").strip().casefold() == (
            "windshield replacement"
        ), arguments
        assert str(arguments.get("requirement_type") or "").strip().casefold() == (
            requirement_key
        ), arguments
        supplied_mode = arguments.get("search_mode") or "standard"
        assert supplied_mode == search_mode, arguments

    evidence_id = (
        "adas-si-miss-tahoe-1"
        if search_mode == "standard"
        else "adas-si-deep-miss-tahoe-2"
    )
    return CallExpectation(
        "adas_si_search",
        {
            "status": "no_result",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": evidence_id,
            "results": [],
        },
        validator=validate,
    )


def _tahoe_adas_hit_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_search",
        {
            "status": "verified",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": "adas-si-hit-tahoe-camera-1",
            "exact_source_matched": True,
            "structured_query": {
                "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
                "component": "forward camera",
                "repair_event": "windshield replacement",
                "search_mode": "calibration_requirements",
            },
            "results": [
                {
                    "document": "Forward Camera Learn Procedure",
                    "page": 9,
                    "finding": "Calibrate after windshield removal or replacement.",
                    "vehicle": {
                        "year": 2023,
                        "make": "Chevrolet",
                        "model": "Tahoe",
                    },
                }
            ],
        },
        validator=_structured_adas_procedure_search,
    )


def _existing_scrapex_list_call() -> CallExpectation:
    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "list_batches",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "source": "scrapex_adas_map",
            "source_bounded": True,
            "data": {
                "batches": [
                    {
                        "id": "batch-week-2026-08-25",
                        "name": "Weekly phase 5-8",
                        "state": "paused",
                    }
                ]
            },
        },
        {"action": "list_batches"},
    )


def _existing_scrapex_preview_call() -> CallExpectation:
    def active_queue_preview(arguments: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        from core.services.scrapex import SCRAPEX_READ_SCHEMA

        errors = sorted(
            Draft202012Validator(
                SCRAPEX_READ_SCHEMA["parameters"]
            ).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        assert not errors, errors[0].message if errors else arguments
        _subset(
            arguments,
            {
                "action": "preview_ciq_queue",
                "phases": ["6"],
                "shop": "Perry",
            },
        )
        assert arguments.get("source_scope", "active") == "active", arguments

    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "preview_ciq_queue",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "data": {
                "count": 1,
                "phases": ["6"],
                "shop": "Perry",
                "source_scope": "active",
                "vehicles": [
                    {
                        "ro_number": "2400911724",
                        "phase": "6",
                        "shop": "Perry",
                    }
                ],
            },
        },
        {
            "action": "preview_ciq_queue",
            "phases": ["6"],
            "shop": "Perry",
        },
        validator=active_queue_preview,
    )


def _existing_scrapex_summary_call() -> CallExpectation:
    observed_batch_id = _existing_scrapex_list_call().result["data"]["batches"][0]["id"]
    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "batch_summary",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "data": {
                "batch_id": observed_batch_id,
                "name": "Weekly phase 5-8",
                "state": "paused",
                "item_count": 1,
                "completed_count": 0,
            },
        },
        {"action": "batch_summary", "batch_id": observed_batch_id},
    )


def _existing_scrapex_exceptions_call() -> CallExpectation:
    observed_batch_id = _existing_scrapex_list_call().result["data"]["batches"][0][
        "id"
    ]
    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "batch_exceptions",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "data": {
                "batch_id": observed_batch_id,
                "count": 0,
                "exceptions": [],
            },
        },
        {"action": "batch_exceptions", "batch_id": observed_batch_id},
    )


def _existing_scrapex_item_miss_call() -> CallExpectation:
    return CallExpectation(
        "scrapex_read",
        {
            "status": "no_result",
            "source": "scrapex_adas_map",
            "source_bounded": True,
            "data": {
                "batch_id": "batch-week-2026-08-25",
                "ro_number": "2400911724",
                "item": None,
            },
            "item": None,
        },
        {
            "action": "batch_item",
            "batch_id": "batch-week-2026-08-25",
            "ro_number": "2400911724",
        },
    )


def _auth_scrapex_list_call() -> CallExpectation:
    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "list_batches",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "source": "scrapex_adas_map",
            "source_bounded": True,
            "data": {"batches": []},
        },
        {"action": "list_batches"},
    )


def _auth_scrapex_create_call() -> CallExpectation:
    return CallExpectation(
        "scrapex_adas_map",
        {
            "service": "ScrapeX",
            "action": "create_exact_batch",
            "status": "queued",
            "success": True,
            "executed": True,
            "verified": True,
            "work_complete": False,
            "data": {"id": "batch-auth-boundary-1"},
            "message": (
                "Batch created. Copy data.id exactly as batch_id for process_one."
            ),
        },
        {"action": "create_exact_batch", "ro_numbers": ["2400911724"]},
    )


def _auth_scrapex_process_call() -> CallExpectation:
    created_batch_id = _auth_scrapex_create_call().result["data"]["id"]

    def copies_created_batch(arguments: dict[str, Any]) -> None:
        _subset(
            arguments,
            {"action": "process_one", "ro_number": "2400911724"},
        )
        assert arguments.get("batch_id") == created_batch_id, arguments

    return CallExpectation(
        "scrapex_adas_map",
        {
            "service": "ScrapeX",
            "action": "process_one",
            "status": "authentication_required",
            "authentication_required": True,
            "requires_human": True,
            "success": False,
            "executed": False,
            "verified": False,
            "work_complete": False,
            "message": (
                "Processing did not run. Managed browser sign-in is required before it can run."
            ),
            "authentication": {
                "mode": "managed_browser_human_sign_in",
                "credential_in_model_context": False,
            },
        },
        validator=copies_created_batch,
    )


def _acquisition_valid_list_detour_call() -> CallExpectation:
    def valid_list(arguments: dict[str, Any]) -> None:
        assert arguments == {"action": "list_batches"}, arguments

    return CallExpectation(
        "scrapex_read",
        {
            "service": "ScrapeX",
            "action": "list_batches",
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "source": "scrapex_adas_map",
            "source_bounded": True,
            "data": {"batches": []},
            "message": (
                "No existing batch id is available. This read did not acquire or process "
                "anything; use create_exact_batch for the requested current acquisition."
            ),
        },
        {"action": "list_batches"},
        validator=valid_list,
    )


def _acquisition_create_call() -> CallExpectation:
    return CallExpectation(
        "scrapex_adas_map",
        {
            "service": "ScrapeX",
            "action": "create_exact_batch",
            "status": "queued",
            "success": True,
            "executed": True,
            "verified": True,
            "work_complete": False,
            "data": {"id": "batch-exact-2"},
            "message": (
                "Batch created. For the next process_one call, copy data.id exactly "
                "as batch_id."
            ),
        },
        {
            "action": "create_exact_batch",
            "ro_numbers": ["2400911724"],
        },
    )


def _acquisition_process_call() -> CallExpectation:
    created_batch_id = _acquisition_create_call().result["data"]["id"]

    def copies_created_batch(arguments: dict[str, Any]) -> None:
        _subset(
            arguments,
            {"action": "process_one", "ro_number": "2400911724"},
        )
        assert arguments.get("batch_id") == created_batch_id, arguments

    return CallExpectation(
        "scrapex_adas_map",
        {
            "service": "ScrapeX",
            "action": "process_one",
            "status": "completed",
            "success": True,
            "executed": True,
            "verified": True,
            "work_complete": True,
            "data": {
                "batch_id": "batch-exact-2",
                "ro_number": "2400911724",
                "attempted": True,
                "completed": True,
                "item": {"ro_number": "2400911724", "status": "completed"},
                "provenance": {"source": "ADAS Map", "verified": True},
            },
        },
        validator=copies_created_batch,
    )


def _nissan_ro_call(*, evidence_id: str = "ciq-nissan-current-21") -> CallExpectation:
    result = json.loads(json.dumps(NISSAN_RO_RESULT))
    result["evidence_id"] = evidence_id
    return CallExpectation(
        "calibration_iq_ro",
        result,
        validator=_exact_ro_scope("ro-nissan-1667", "2400911667"),
    )


def _nissan_alignment_work_prep_call() -> CallExpectation:
    inspection_id = "adas-map-nissan-alignment-verified-1"
    requirement = {
        "label": "radar aiming",
        "system": "front radar",
        "method": "STATIC",
        "prerequisites": [
            "Complete wheel alignment and verify thrust angle before radar aiming."
        ],
        "actionable_before_alignment": [
            "Verify the radar sensor and mounting bracket are installed and undamaged.",
            "Stage the OEM aiming procedure, work area, and static target equipment.",
        ],
    }
    return CallExpectation(
        "calibration_iq_work_prep",
        {
            "status": "success",
            "mode": "ro_requirements",
            "executed": False,
            "success": True,
            "verified": True,
            "snapshot_verified": True,
            "repair_order_id": "ro-nissan-1667",
            "ro_number": "2400911667",
            "vehicle": "2023 Nissan Rogue",
            "calibration_requirements": [
                {
                    "id": "cal-radar-nissan-1",
                    "label": "radar aiming",
                    "determination": "REQUIRED",
                    "method": "STATIC",
                }
            ],
            "adas_map": {
                "status": "verified",
                "discovery_status": "verified",
                "governing_source": "ADAS Map",
                "requirements": [requirement],
                "sources": [
                    {
                        "kind": "scrapex_adas_map",
                        "inspection_id": inspection_id,
                        "item_id": "adas-map-nissan-radar-requirement-1",
                    }
                ],
                "requirement_count": 1,
                "explicit_no_calibration": False,
                "inspection_id": inspection_id,
                "vin": "5N1AT3TEST00001667",
                "vehicle": {
                    "year": 2023,
                    "make": "Nissan",
                    "model": "Rogue",
                },
                "artifact_index": 0,
                "reason": None,
                "identity_conflicts": [],
            },
            "reconciliation_actions": [],
            "reconciliation": None,
            "reconciliation_issues": [],
        },
        validator=_ro_requirements_scope("ro-nissan-1667", "2400911667"),
    )


def _nissan_knowledge_miss_call(
    *, event: str | tuple[str, ...], evidence_id: str
) -> CallExpectation:
    return CallExpectation(
        "automotive_knowledge_search",
        {
            "status": "no_result",
            "source": "durable_automotive_knowledge",
            "source_bounded": True,
            "evidence_id": evidence_id,
            "records": [],
        },
        validator=_field_knowledge_scope(
            year=2023,
            make="Nissan",
            model="Rogue",
            event=event,
            component="front radar",
        ),
    )


def _nissan_radar_evidence_call(
    *,
    event: str | tuple[str, ...],
    evidence_id: str,
    finding: str,
    page: int,
    explicit_radar_scopes: tuple[str, ...] = ("front radar", "radar"),
) -> CallExpectation:
    structured_scope = _field_adas_scope(
        year=2023,
        make="Nissan",
        model="Rogue",
        repair_event=event,
        component=("front radar", "radar", "ADAS"),
    )

    def explicit_radar_scope(arguments: dict[str, Any]) -> None:
        structured_scope(arguments)
        component = str(arguments.get("component") or "").strip().casefold()
        system = str(arguments.get("system") or "").strip().casefold()
        explicit_scope = component or system
        assert explicit_scope in {
            value.strip().casefold() for value in explicit_radar_scopes
        }, arguments

    return CallExpectation(
        "adas_si_search",
        {
            "status": "verified",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": evidence_id,
            "exact_source_matched": True,
            "structured_query": {
                "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
                "component": "front radar",
                "repair_event": (event[0] if isinstance(event, tuple) else event),
                "search_mode": "calibration_requirements",
            },
            "results": [
                {
                    "document": "2023 Rogue Front Radar Aiming",
                    "relative_path": "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf",
                    "page": page,
                    "finding": finding,
                    "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
                }
            ],
        },
        validator=explicit_radar_scope,
    )


def _nissan_scoped_evidence_call(
    *,
    event: str | tuple[str, ...],
    evidence_id: str,
    finding: str,
    page: int,
    explicit_radar_scopes: tuple[str, ...] = ("front radar", "radar"),
) -> CallExpectation:
    expectation = _nissan_radar_evidence_call(
        event=event,
        evidence_id=evidence_id,
        finding=finding,
        page=page,
        explicit_radar_scopes=explicit_radar_scopes,
    )
    structured_scope = expectation.validator
    assert structured_scope is not None
    radar_scope_keys = {
        value.strip().casefold() for value in explicit_radar_scopes
    }

    def explicit_structured_scope(arguments: dict[str, Any]) -> None:
        structured_scope(arguments)
        requirement = arguments.get("requirement_type")
        assert isinstance(requirement, str) and requirement.strip(), arguments
        question = arguments.get("question")
        assert isinstance(question, str) and question.strip(), arguments
        supplied_scopes = [
            str(arguments.get(key) or "").strip().casefold()
            for key in ("system", "component")
            if arguments.get(key) not in (None, "")
        ]
        assert supplied_scopes, arguments
        assert all(scope in radar_scope_keys for scope in supplied_scopes), arguments

    return CallExpectation(
        expectation.name,
        expectation.result,
        expectation.subset,
        validator=explicit_structured_scope,
    )


def _nissan_alignment_evidence_call() -> CallExpectation:
    return _nissan_scoped_evidence_call(
        event=(
            "wheel alignment incomplete",
            "wheel alignment",
            "front bumper replacement",
        ),
        evidence_id="adas-si-nissan-alignment-prereq-p20",
        finding=(
            "Complete wheel alignment and verify thrust angle before front radar aiming."
        ),
        page=20,
    )


def _nissan_ambiguous_procedure_evidence_call() -> CallExpectation:
    return _nissan_scoped_evidence_call(
        event="front bumper replacement",
        evidence_id="adas-si-nissan-ambiguous-procedure-p18",
        finding="Front radar aiming procedure and prerequisites.",
        page=18,
        explicit_radar_scopes=("front radar", "radar", "radar aiming"),
    )


def _nissan_procedure_search_call(
    *, repair_events: tuple[str, ...]
) -> CallExpectation:
    expectation = _nissan_radar_evidence_call(
        event=repair_events,
        evidence_id="adas-si-nissan-radar-procedure-p18",
        finding="Front radar aiming procedure and prerequisites.",
        page=18,
    )
    structured_scope = expectation.validator
    assert structured_scope is not None

    def procedure_scope(arguments: dict[str, Any]) -> None:
        structured_scope(arguments)
        requirement = str(arguments.get("requirement_type") or "").strip().casefold()
        assert requirement in {
            "procedure",
            "oem procedure",
            "calibration",
            "calibration procedure",
        }, arguments
        question = arguments.get("question")
        assert isinstance(question, str) and question.strip(), arguments

    return CallExpectation(
        expectation.name,
        expectation.result,
        expectation.subset,
        validator=procedure_scope,
    )


def _nissan_subject_procedure_search_call() -> CallExpectation:
    return _nissan_procedure_search_call(
        repair_events=("calibration", "radar aiming")
    )


def _nissan_refreshed_procedure_search_call() -> CallExpectation:
    return _nissan_procedure_search_call(
        repair_events=("front bumper replacement", "calibration", "radar aiming")
    )


def _nissan_procedure_open_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_open",
        {
            "status": "verified",
            "source": "adas_si",
            "evidence_id": "adas-open-nissan-radar-p18",
            "relative_path": "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf",
            "page": 18,
            "displayed": True,
            "document": {
                "title": "2023 Rogue Front Radar Aiming",
                "relative_path": "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf",
                "page": 18,
                "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
            },
        },
        validator=_adas_open_scope(
            relative_path="Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf",
            page=18,
        ),
    )


def _toyota_ro_call(*, evidence_id: str = "ciq-toyota-current-7") -> CallExpectation:
    result = json.loads(json.dumps(TOYOTA_RO_RESULT))
    result["evidence_id"] = evidence_id
    return CallExpectation(
        "calibration_iq_ro",
        result,
        validator=_exact_ro_scope("ro-toyota-1478", "2400611478"),
    )


def _toyota_procedure_search_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_search",
        {
            "status": "verified",
            "source": "adas_si",
            "source_bounded": True,
            "evidence_id": "adas-si-toyota-camera-p34",
            "exact_source_matched": True,
            "structured_query": {
                "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
                "component": "forward camera",
                "repair_event": "windshield replacement",
                "search_mode": "calibration_requirements",
            },
            "results": [
                {
                    "document": "2024 Camry Forward Recognition Camera Adjustment",
                    "relative_path": (
                        "Toyota/Camry/2024/ADAS/Forward Recognition Camera Adjustment.pdf"
                    ),
                    "page": 34,
                    "finding": (
                        "Forward recognition camera adjustment is required after "
                        "windshield replacement."
                    ),
                    "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
                }
            ],
        },
        validator=_field_adas_scope(
            year=2024,
            make="Toyota",
            model="Camry",
            repair_event="windshield replacement",
            component=("forward camera", "camera"),
        ),
    )


def _toyota_procedure_open_call() -> CallExpectation:
    return CallExpectation(
        "adas_si_open",
        {
            "status": "verified",
            "source": "adas_si",
            "evidence_id": "adas-open-toyota-camera-p34",
            "relative_path": (
                "Toyota/Camry/2024/ADAS/Forward Recognition Camera Adjustment.pdf"
            ),
            "page": 34,
            "displayed": True,
            "document": {
                "title": "2024 Camry Forward Recognition Camera Adjustment",
                "relative_path": (
                    "Toyota/Camry/2024/ADAS/Forward Recognition Camera Adjustment.pdf"
                ),
                "page": 34,
                "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
            },
        },
        validator=_adas_open_scope(
            relative_path=(
                "Toyota/Camry/2024/ADAS/Forward Recognition Camera Adjustment.pdf"
            ),
            page=34,
        ),
    )


def _toyota_research_attach_result() -> dict[str, Any]:
    ro_id = "ro-toyota-1478"
    calibration_id = "cal-camera-toyota-1"
    document_id = "doc-toyota-camera-oem-1"
    relative_path = (
        "Toyota/Camry/2024/ADAS/Forward Recognition Camera Adjustment.pdf"
    )
    snapshot = deepcopy(TOYOTA_RO_RESULT["raw"])
    snapshot["documents"] = [
        {
            "id": document_id,
            "version": 1,
            "title": "2024 Camry Forward Recognition Camera Adjustment",
            "document_type": "oem_procedure",
            "status": "validated",
            "source_uri": f"adas-si:///{relative_path}",
            "source_name": "Forward Recognition Camera Adjustment.pdf",
            "storage_relative_path": (
                "OEM Procedures/Forward Recognition Camera Adjustment.pdf"
            ),
            "page_references": ["p. 34"],
            "calibration_item_ids": [calibration_id],
        }
    ]
    snapshot["research"] = {
        "state": "research_in_progress",
        "version": 3,
        "documents": deepcopy(snapshot["documents"]),
    }
    research = {
        "repair_order_id": ro_id,
        "vehicle": "2024 Toyota Camry",
        "required_calibrations": [
            {
                "id": calibration_id,
                "label": "forward recognition camera adjustment",
            }
        ],
        "findings": [
            {
                "calibration_id": calibration_id,
                "calibration": "forward recognition camera adjustment",
                "query": (
                    "2024 Toyota Camry forward recognition camera adjustment"
                ),
                "status": "verified",
                "supported": True,
                "documents": [
                    {
                        "title": "2024 Camry Forward Recognition Camera Adjustment",
                        "source": "Forward Recognition Camera Adjustment.pdf",
                        "relative_path": relative_path,
                        "pages": [34],
                    }
                ],
                "missing_reason": None,
            }
        ],
        "documents_prepared": [
            {
                "title": "2024 Camry Forward Recognition Camera Adjustment",
                "source": "Forward Recognition Camera Adjustment.pdf",
                "relative_path": relative_path,
                "pages": [34],
                "calibration_item_ids": [calibration_id],
                "status": "validated",
            }
        ],
        "already_present": [],
        "missing_documents": [],
        "research_complete_requested": False,
        "research_complete_eligible": True,
        "research_complete_action_added": False,
        "research_complete_was_already_set": False,
        "research_complete_already_verified": False,
        "completion_withheld": False,
    }
    return {
        "status": "success",
        "executed": True,
        "success": True,
        "verified": True,
        "partial": False,
        "requested_count": 2,
        "processed_count": 2,
        "stopped_on_error": False,
        "receipts": [
            {
                "mutation_id": "mut-toyota-workspace-1",
                "operation": "ensure_case_workspace",
                "repair_order_id": ro_id,
                "resource_type": "case_workspace",
                "resource_id": "case-workspace-toyota-1478",
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            },
            {
                "mutation_id": "mut-toyota-document-import-1",
                "operation": "import_document",
                "repair_order_id": ro_id,
                "resource_type": "document",
                "resource_id": document_id,
                "status": "completed",
                "success": True,
                "verification": {"verified": True},
            },
        ],
        "final_snapshots": {
            ro_id: {"status": "verified", "snapshot": snapshot}
        },
        "research": [research],
        "missing_documentation": [],
        "message": (
            "Calibration IQ confirmed every action and the authoritative final state."
        ),
    }


def _toyota_research_attach_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_operator",
        _toyota_research_attach_result(),
        validator=_research_ro_scope(ro_id="ro-toyota-1478", expected_version=7),
    )


def _toyota_mixed_live_arguments() -> dict[str, Any]:
    return {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": "ro-toyota-1478",
                "arguments": {
                    "calibration_ids": ["cal-camera-toyota-1"],
                    "query": (
                        "2024 Toyota Camry forward recognition camera adjustment "
                        "after windshield replacement"
                    ),
                    "destination_folder": "OEM Procedures",
                    "complete_research": False,
                },
            },
            {
                "operation": "add_calibration",
                "repair_order_id": "ro-toyota-1478",
                "arguments": {
                    "calibration_type": (
                        "forward recognition camera adjustment"
                    ),
                    "determination": "REQUIRED",
                    "method": "STATIC",
                },
            },
        ]
    }


def _toyota_mixed_research_calibration_call() -> CallExpectation:
    def mixed_same_ro_scope(arguments: dict[str, Any]) -> None:
        from jsonschema import Draft202012Validator

        from core.tools.registry import TOOL_SCHEMAS

        errors = list(
            Draft202012Validator(
                TOOL_SCHEMAS["calibration_iq_operator"]["parameters"]
            ).iter_errors(arguments)
        )
        assert not errors, errors[0].message if errors else arguments
        actions = arguments.get("actions")
        assert isinstance(actions, list) and len(actions) == 2, arguments
        assert {action.get("operation") for action in actions} == {
            "research_ro",
            "add_calibration",
        }, arguments
        assert {
            str(action.get("repair_order_id") or "").strip() for action in actions
        } == {"ro-toyota-1478"}, arguments

    message = (
        "Calibration mutations and research_ro for the same repair order require "
        "sequential calibration_iq_operator calls in this same user turn: apply and "
        "verify the calibration change first, then use its generated id in research_ro. "
        "Nothing was run."
    )
    return CallExpectation(
        "calibration_iq_operator",
        {
            "status": "prerequisite_missing",
            "executed": False,
            "success": False,
            "verified": False,
            "partial": False,
            "http_status": None,
            "error": {
                "code": "prerequisite_missing",
                "message": message,
                "category": "prerequisite_missing",
                "retryable": False,
                "details": {"repair_order_ids": ["ro-toyota-1478"]},
            },
            "message": message,
        },
        validator=mixed_same_ro_scope,
    )


def _toyota_mixed_recovery_ro_call() -> CallExpectation:
    return _toyota_ro_call(evidence_id="ciq-toyota-mixed-recovery-current-7")


def _toyota_existing_calibration_research_call() -> CallExpectation:
    def existing_calibration_scope(arguments: dict[str, Any]) -> None:
        _research_ro_scope(ro_id="ro-toyota-1478", expected_version=7)(arguments)
        action_arguments = arguments["actions"][0].get("arguments")
        if isinstance(action_arguments, dict) and "calibration_ids" in action_arguments:
            assert action_arguments["calibration_ids"] == ["cal-camera-toyota-1"], (
                arguments
            )

    return CallExpectation(
        "calibration_iq_operator",
        _toyota_research_attach_result(),
        validator=existing_calibration_scope,
    )


def _toyota_verified_close_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_operator",
        _operator_result(
            ro_id="ro-toyota-1478",
            ro_number="2400611478",
            version=8,
            evidence_id="ciq-toyota-close-receipt-8",
            make="Toyota",
            model="Camry",
        ),
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "ro-toyota-1478",
                    "expected_version": 7,
                }
            ]
        },
    )


def _change_status_complete_call() -> CallExpectation:
    def exact_completed_status(arguments: dict[str, Any]) -> None:
        actions = arguments.get("actions")
        assert isinstance(actions, list) and len(actions) == 1, arguments
        _subset(
            actions[0],
            {
                "operation": "change_status",
                "repair_order_id": "ro-uuid-17",
                "expected_version": 12,
            },
            "arguments.actions[0]",
        )
        operation_arguments = actions[0].get("arguments")
        assert isinstance(operation_arguments, dict), arguments
        assert operation_arguments.get("status") in {
            "complete",
            "calibration complete",
        }, arguments

    result = _operator_result(
        evidence_id="close-receipt-13",
        operation="change_status",
        status="complete",
    )
    result.update(
        verified_effect_scope="repair_order_workflow_status_completion",
        child_calibration_state_included=False,
        child_calibration_completion_proven=False,
    )
    return CallExpectation(
        "calibration_iq_operator",
        result,
        validator=exact_completed_status,
    )


def _toyota_indeterminate_close_call() -> CallExpectation:
    return CallExpectation(
        "calibration_iq_operator",
        {
            "status": "indeterminate",
            "success": False,
            "verified": False,
            "partial": True,
            "may_have_executed": True,
            "retryable": False,
            "evidence_id": "ciq-close-indeterminate-7",
            "receipts": [
                {
                    "operation": "close_ro",
                    "status": "indeterminate",
                    "verification": {"verified": False},
                }
            ],
            "final_snapshots": {},
        },
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "ro-toyota-1478",
                    "expected_version": 7,
                }
            ]
        },
    )


def _close_paraphrase_call(index: int) -> CallExpectation:
    return CallExpectation(
        "calibration_iq_operator",
        _operator_result(
            ro_id="ro-uuid-1478",
            ro_number="2400611478",
            version=13,
            evidence_id=f"close-paraphrase-receipt-{index + 1}",
        ),
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "ro-uuid-1478",
                    "expected_version": 12,
                }
            ]
        },
    )


def _close_paraphrase_refresh_call(index: int) -> CallExpectation:
    result = json.loads(json.dumps(PARAPHRASE_RO_RESULT))
    result["evidence_id"] = f"close-paraphrase-refresh-{index + 1}"
    return CallExpectation(
        "calibration_iq_ro",
        result,
        validator=_exact_ro_scope("ro-uuid-1478", "2400611478"),
    )


def _report(
    outcome: str,
    *,
    found: bool,
    execution: str = "not_requested",
    used_subject: bool = False,
    subject_id: str = "",
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "authoritative_answer_found": found,
        "execution_state": execution,
        "used_active_subject": used_subject,
        "subject_resource_id": subject_id,
        "requested_credentials": False,
    }


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "capability_read_write_boundary",
        (
            Turn(
                "Are you actually able to change Calibration IQ records, or can you only look at them? Check what is connected before answering.",
                (_capability_catalog_call(),),
                ReportExpectation(
                    _report("answered", found=True),
                    frozenset({"capability_catalog"}),
                    frozenset({"capability-catalog-live-1"}),
                ),
                alternative_calls=(
                    (
                        _calibration_iq_status_call(),
                        _capability_catalog_call(),
                    ),
                ),
            ),
        ),
    ),
    Scenario(
        "count_paraphrase_unfinished_workload",
        (
            Turn(
                "What's the unfinished workload at Perry in phase five? I need the size, not individual vehicles.",
                (
                    CallExpectation(
                        "calibration_iq_summary",
                        {
                            "status": "success",
                            "verified": True,
                            "evidence_id": "ciq-summary-perry-p5-a",
                            "scope": {
                                "shop": "Perry",
                                "phase": "5",
                                "include_completed": False,
                            },
                            "count": 7,
                        },
                        validator=_summary_scope,
                    ),
                ),
                ReportExpectation(
                    _report("answered", found=True),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-summary-perry-p5-a"}),
                    path_requirements=(
                        (
                            ("calibration_iq_work_prep",),
                            frozenset({"calibration_iq"}),
                            frozenset(),
                        ),
                    ),
                ),
                alternative_calls=((_perry_phase_list_call(),),),
            ),
        ),
    ),
    Scenario(
        "count_paraphrase_total_still_in_play",
        (
            Turn(
                "For Perry phase 5, give me the total still in play and leave out the finished repair orders.",
                (
                    CallExpectation(
                        "calibration_iq_summary",
                        {
                            "status": "success",
                            "verified": True,
                            "evidence_id": "ciq-summary-perry-p5-b",
                            "scope": {
                                "shop": "Perry",
                                "phase": "5",
                                "include_completed": False,
                            },
                            "count": 7,
                        },
                        validator=_summary_scope,
                    ),
                ),
                ReportExpectation(
                    _report("answered", found=True),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-summary-perry-p5-b"}),
                ),
            ),
        ),
    ),
    Scenario(
        "durable_subject_and_close_ro_mapping",
        (
            Turn(
                "Pull up repair order 2400911724.",
                (
                    CallExpectation(
                        "calibration_iq_ro",
                        RO_RESULT,
                        {"repair_order_id": "2400911724"},
                    ),
                ),
                ReportExpectation(
                    {
                        "outcome": "answered",
                        "authoritative_answer_found": True,
                        "used_active_subject": False,
                        "requested_credentials": False,
                    },
                    frozenset({"calibration_iq"}),
                    frozenset({"ro-snapshot-12"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        CallExpectation(
                            "calibration_iq_ro",
                            RO_RESULT,
                            {"repair_order_id": "2400911724"},
                        ),
                        ro_id="ro-uuid-17",
                        ro_number="2400911724",
                        evidence_id="lookup-ro-2400911724",
                        vehicle="2023 Chevrolet Tahoe",
                        shop="Perry",
                    ),
                ),
            ),
            Turn(
                "The work on that vehicle is all wrapped up. Mark the repair order finished.",
                (
                    _existing_ciq_ro_call(),
                    CallExpectation(
                        "calibration_iq_operator",
                        _operator_result(),
                        {
                            "actions": [
                                {
                                    "operation": "close_ro",
                                    "repair_order_id": "ro-uuid-17",
                                    "expected_version": 12,
                                }
                            ]
                        },
                    ),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"close-receipt-13"}),
                ),
                alternative_calls=(
                    (
                        _existing_ciq_ro_call(),
                        _change_status_complete_call(),
                    ),
                ),
            ),
        ),
        negative_truths=frozenset(
            {"no_invented_data", "no_unreceipted_mutation_success"}
        ),
    ),
    Scenario(
        "explicit_new_resource_overrides_stale_subject",
        (
            Turn(
                "Pull up repair order 2400999000 instead.",
                (
                    CallExpectation(
                        "calibration_iq_ro",
                        _ro_result_for(
                            ro_id="ro-uuid-99",
                            ro_number="2400999000",
                            version=4,
                            evidence_id="ro-snapshot-b-4",
                            make="Ford",
                            model="F-150",
                        ),
                        {"repair_order_id": "2400999000"},
                    ),
                ),
                ReportExpectation(
                    {
                        "outcome": "answered",
                        "authoritative_answer_found": True,
                        "subject_resource_id": "ro-uuid-99",
                        "requested_credentials": False,
                    },
                    frozenset({"calibration_iq"}),
                    frozenset({"ro-snapshot-b-4"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        CallExpectation(
                            "calibration_iq_ro",
                            _ro_result_for(
                                ro_id="ro-uuid-99",
                                ro_number="2400999000",
                                version=4,
                                evidence_id="ro-snapshot-b-4",
                                make="Ford",
                                model="F-150",
                            ),
                            {"repair_order_id": "2400999000"},
                        ),
                        ro_id="ro-uuid-99",
                        ro_number="2400999000",
                        evidence_id="lookup-ro-2400999000",
                        vehicle="2023 Ford F-150",
                        shop="Perry",
                    ),
                ),
            ),
            Turn(
                "Close it out.",
                (
                    CallExpectation(
                        "calibration_iq_ro",
                        _ro_result_for(
                            ro_id="ro-uuid-99",
                            ro_number="2400999000",
                            version=4,
                            evidence_id="ro-snapshot-b-preclose-4",
                            make="Ford",
                            model="F-150",
                        ),
                        validator=_exact_ro_scope("ro-uuid-99", "2400999000"),
                    ),
                    CallExpectation(
                        "calibration_iq_operator",
                        _operator_result(
                            ro_id="ro-uuid-99",
                            ro_number="2400999000",
                            version=5,
                            evidence_id="close-receipt-b-5",
                            make="Ford",
                            model="F-150",
                        ),
                        validator=_close_ro_scope(
                            "ro-uuid-99",
                            "2400999000",
                            expected_version=4,
                        ),
                    ),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-uuid-99",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"close-receipt-b-5"}),
                ),
            ),
        ),
        initial_subject=SUBJECT,
    ),
    Scenario(
        "destructive_write_stops_for_approval",
        (
            Turn(
                "Remove blocker blk-9 from this repair order.",
                (
                    _destructive_refresh_call(),
                    _destructive_delete_call(),
                ),
                ReportExpectation(
                    {
                        "outcome": "approval_required",
                        "authoritative_answer_found": False,
                        "used_active_subject": True,
                        "subject_resource_id": "ro-uuid-17",
                        "requested_credentials": False,
                    },
                    frozenset({"calibration_iq"}),
                    frozenset({"approval-boundary-blk-9"}),
                ),
            ),
        ),
        initial_subject=SUBJECT,
    ),
    Scenario(
        "existing_evidence_escalates_without_inventing_answer",
        (
            Turn(
                "Using this vehicle, exhaust the durable knowledge, local service-information, and existing ADAS Map evidence to establish whether forward-camera calibration is required after the windshield was replaced. Retrieve the exact per-RO item, but do not start new acquisition. If those sources do not establish it, say so.",
                (
                    CallExpectation(
                        "automotive_knowledge_search",
                        {
                            "status": "no_result",
                            "source": "durable_automotive_knowledge",
                            "source_bounded": True,
                            "evidence_id": "knowledge-miss-tahoe-1",
                            "records": [],
                        },
                        validator=_structured_knowledge_search,
                    ),
                    CallExpectation(
                        "adas_si_search",
                        {
                            "status": "no_result",
                            "source": "adas_si",
                            "source_bounded": True,
                            "evidence_id": "adas-si-miss-tahoe-1",
                            "results": [],
                        },
                        validator=_structured_adas_search,
                    ),
                    _existing_scrapex_list_call(),
                    _existing_scrapex_item_miss_call(),
                ),
                ReportExpectation(
                    _report(
                        "no_authoritative_answer",
                        found=False,
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"durable_knowledge", "adas_si", "scrapex_adas_map"}),
                    frozenset(
                        {
                            "knowledge-miss-tahoe-1",
                            "adas-si-miss-tahoe-1",
                            "batch-week-2026-08-25",
                        }
                    ),
                    path_requirements=(
                        (
                            (
                                "calibration_iq_ro",
                                "adas_si_search",
                                "adas_si_search",
                                "automotive_knowledge_search",
                                "scrapex_read",
                                "scrapex_read",
                            ),
                            frozenset(
                                {
                                    "durable_knowledge",
                                    "adas_si",
                                    "scrapex_adas_map",
                                }
                            ),
                            frozenset(
                                {
                                    "knowledge-miss-tahoe-1",
                                    "adas-si-miss-tahoe-1",
                                    "adas-si-deep-miss-tahoe-2",
                                    "batch-week-2026-08-25",
                                }
                            ),
                        ),
                    ),
                ),
                alternative_calls=(
                    (
                        _existing_ciq_ro_call(),
                        _existing_adas_retry_miss_call(
                            search_mode="standard",
                            requirement_type="Calibration Required",
                        ),
                        _existing_adas_retry_miss_call(
                            search_mode="calibration_requirements",
                            requirement_type="Calibration Required",
                        ),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_knowledge_miss_call(),
                        _existing_adas_miss_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_knowledge_miss_call(),
                        _existing_adas_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_knowledge_miss_call(),
                        _existing_adas_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_summary_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_knowledge_miss_call(),
                        _existing_adas_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_summary_call(),
                        _existing_scrapex_exceptions_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_summary_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_ciq_ro_call(),
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_summary_call(),
                        _existing_scrapex_exceptions_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                    (
                        _existing_adas_miss_call(),
                        _existing_knowledge_miss_call(),
                        _existing_scrapex_preview_call(),
                        _existing_ciq_ro_call(),
                        _existing_scrapex_list_call(),
                        _existing_scrapex_item_miss_call(),
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
        category="negative_source_truth",
        negative_truths=frozenset({"no_invented_data", "no_invented_batch_ids"}),
    ),
    Scenario(
        "scrapex_authentication_boundary",
        (
            Turn(
                "Acquire current ADAS Map evidence for this repair order. If the managed browser needs sign-in, stop at that boundary and do not ask me for a password here.",
                (
                    _auth_scrapex_create_call(),
                    _auth_scrapex_process_call(),
                ),
                ReportExpectation(
                    _report(
                        "blocked",
                        found=False,
                        execution="not_confirmed",
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"scrapex_adas_map"}),
                    frozenset({"batch-auth-boundary-1"}),
                ),
                alternative_calls=(
                    (
                        _existing_scrapex_preview_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                    (
                        _existing_scrapex_preview_call(),
                        _scrapex_status_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                    (
                        _auth_scrapex_list_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                    (
                        _scrapex_status_call(),
                        _existing_scrapex_preview_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                    (
                        _scrapex_status_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                    (
                        _scrapex_status_call(),
                        _auth_scrapex_list_call(),
                        _auth_scrapex_create_call(),
                        _auth_scrapex_process_call(),
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
        category="acquisition_auth_boundary",
        negative_truths=frozenset({"no_false_acquisition_success"}),
    ),
    Scenario(
        "licensed_alldata_is_not_scrapex",
        (
            Turn(
                "Use the licensed ALLDATA provider to research collision-repair procedures for this Tahoe's forward camera after windshield replacement.",
                (_alldata_research_call(),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"alldata"}),
                    frozenset({"alldata-evidence-tahoe-1"}),
                ),
                alternative_calls=(
                    (
                        _alldata_provider_ready_call(),
                        _alldata_research_call(),
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
    ),
)

ESCALATION_SCENARIOS = (
    Scenario(
        "durable_knowledge_supplies_missing_local_answer",
        (
            Turn(
                "What forward-camera calibration is required for this RO after its windshield replacement? Check our verified durable automotive knowledge after the RO before any acquisition.",
                (_durable_knowledge_hit_call(),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"durable_knowledge"}),
                    frozenset({"knowledge-hit-tahoe-camera-1"}),
                ),
                alternative_calls=(
                    (_existing_ciq_ro_call(), _durable_knowledge_hit_call()),
                ),
            ),
        ),
        initial_subject=SUBJECT,
    ),
    Scenario(
        "adas_si_supplies_answer_after_durable_miss",
        (
            Turn(
                "Establish the forward-camera requirement for this RO after windshield replacement. After the RO, check verified durable automotive knowledge first, then ADAS SI if that source has no result.",
                (
                    _existing_ciq_ro_call(),
                    CallExpectation(
                        "automotive_knowledge_search",
                        {
                            "status": "no_result",
                            "source": "durable_automotive_knowledge",
                            "source_bounded": True,
                            "evidence_id": "knowledge-miss-before-adas-1",
                            "records": [],
                        },
                        validator=_structured_knowledge_search,
                    ),
                    _tahoe_adas_hit_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"durable_knowledge", "adas_si"}),
                    frozenset(
                        {
                            "adas-si-hit-tahoe-camera-1",
                        }
                    ),
                ),
                alternative_calls=(
                    (
                        CallExpectation(
                            "automotive_knowledge_search",
                            {
                                "status": "no_result",
                                "source": "durable_automotive_knowledge",
                                "source_bounded": True,
                                "evidence_id": "knowledge-miss-before-adas-1",
                                "records": [],
                            },
                            validator=_structured_knowledge_search,
                        ),
                        _tahoe_adas_hit_call(),
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
    ),
    Scenario(
        "scrapex_acquisition_completes_with_provenance",
        (
            Turn(
                "Acquire current ADAS Map evidence for this active repair order and process just this one.",
                (
                    _acquisition_create_call(),
                    _acquisition_process_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-uuid-17",
                    ),
                    frozenset({"scrapex_adas_map"}),
                    frozenset({"batch-exact-2"}),
                ),
                alternative_calls=(
                    (
                        _existing_scrapex_preview_call(),
                        _acquisition_create_call(),
                        _acquisition_process_call(),
                    ),
                    (
                        _existing_scrapex_preview_call(),
                        _scrapex_status_call(),
                        _acquisition_create_call(),
                        _acquisition_process_call(),
                    ),
                    (
                        _acquisition_valid_list_detour_call(),
                        _acquisition_create_call(),
                        _acquisition_process_call(),
                    ),
                    (
                        _scrapex_status_call(),
                        _acquisition_create_call(),
                        _acquisition_process_call(),
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
        category="acquisition",
        negative_truths=frozenset(
            {"no_invented_batch_ids", "no_false_acquisition_success"}
        ),
    ),
    Scenario(
        "five_turn_subject_research_attach_close",
        (
            Turn(
                "Pull up RO 2400611478.",
                (
                    CallExpectation(
                        "calibration_iq_ro",
                        _ro_result_for(
                            ro_id="ro-uuid-1478",
                            ro_number="2400611478",
                            version=12,
                            evidence_id="five-turn-ro-12",
                        ),
                        {"repair_order_id": "2400611478"},
                    ),
                ),
                ReportExpectation(
                    {
                        "outcome": "answered",
                        "authoritative_answer_found": True,
                        "used_active_subject": False,
                        "requested_credentials": False,
                    },
                    frozenset({"calibration_iq"}),
                    frozenset({"five-turn-ro-12"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        CallExpectation(
                            "calibration_iq_ro",
                            _ro_result_for(
                                ro_id="ro-uuid-1478",
                                ro_number="2400611478",
                                version=12,
                                evidence_id="five-turn-ro-12",
                            ),
                            {"repair_order_id": "2400611478"},
                        ),
                        ro_id="ro-uuid-1478",
                        ro_number="2400611478",
                        evidence_id="lookup-five-turn-ro-1478",
                        vehicle="2023 Chevrolet Tahoe",
                        shop="Perry",
                    ),
                ),
            ),
            Turn(
                "What calibrations does it need?",
                (_five_turn_calibration_ro_call(),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-uuid-1478",
                    ),
                ),
                alternative_calls=(
                    (_five_turn_adas_calibration_call(),),
                    (
                        _five_turn_list_call(),
                        _five_turn_calibration_ro_call(),
                    ),
                    (_five_turn_list_call(), _five_turn_adas_calibration_call()),
                ),
            ),
            Turn(
                "Show me the OEM procedure.",
                (
                    _five_turn_ro_procedure_call(),
                    _five_turn_knowledge_call(),
                    _five_turn_adas_procedure_call(),
                    _five_turn_procedure_open_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-uuid-1478",
                    ),
                    frozenset({"adas_si"}),
                    frozenset(
                        {
                            "five-turn-oem-procedure",
                            "five-turn-oem-procedure-open",
                        }
                    ),
                ),
                alternative_calls=(
                    (
                        _five_turn_ro_procedure_call(),
                        _five_turn_adas_procedure_call(),
                        _five_turn_procedure_open_call(),
                    ),
                    (
                        _five_turn_list_call(),
                        _five_turn_ro_procedure_call(),
                        _five_turn_knowledge_call(),
                        _five_turn_adas_procedure_call(),
                        _five_turn_procedure_open_call(),
                    ),
                    (
                        _five_turn_list_call(),
                        _five_turn_ro_procedure_call(),
                        _five_turn_adas_procedure_call(),
                        _five_turn_procedure_open_call(),
                    ),
                    (
                        _five_turn_list_call(),
                        _five_turn_adas_procedure_call(),
                        _five_turn_procedure_open_call(),
                    ),
                    (
                        _five_turn_adas_procedure_call(),
                        _five_turn_procedure_open_call(),
                    ),
                ),
            ),
            Turn(
                "Put that in the case.",
                (
                    _five_turn_calibration_ro_call(),
                    _five_turn_research_attach_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-uuid-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"doc-five-turn-fcm-procedure-1"}),
                ),
            ),
            Turn(
                "Close it out.",
                (
                    CallExpectation(
                        "calibration_iq_ro",
                        _ro_result_for(
                            ro_id="ro-uuid-1478",
                            ro_number="2400611478",
                            version=13,
                            evidence_id="five-turn-preclose-ro-13",
                        ),
                        {"repair_order_id": "ro-uuid-1478"},
                    ),
                    CallExpectation(
                        "calibration_iq_operator",
                        _operator_result(
                            ro_id="ro-uuid-1478",
                            ro_number="2400611478",
                            version=14,
                            evidence_id="five-turn-close-receipt",
                        ),
                        {
                            "actions": [
                                {
                                    "operation": "close_ro",
                                    "repair_order_id": "ro-uuid-1478",
                                    "expected_version": 13,
                                }
                            ]
                        },
                    ),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-uuid-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"five-turn-close-receipt"}),
                ),
            ),
        ),
    ),
)


FIELD_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "field_exact_ro_2400611478",
        (
            Turn(
                "Pull up RO 2400611478.",
                (_toyota_ro_call(),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=False,
                        subject_id="ro-toyota-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-toyota-current-7"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        _toyota_ro_call(),
                        ro_id="ro-toyota-1478",
                        ro_number="2400611478",
                        evidence_id="lookup-field-toyota-1478",
                        vehicle="2024 Toyota Camry",
                        shop="Perry",
                    ),
                ),
            ),
        ),
        category="exact_ro",
        negative_truths=frozenset({"no_invented_data", "no_magic_wording"}),
    ),
    Scenario(
        "field_current_calibrations_refreshes_exact_ro",
        (
            Turn(
                "What calibrations does this one need?",
                (_toyota_ro_call(evidence_id="ciq-toyota-current-calibrations-7"),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-toyota-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-toyota-current-calibrations-7"}),
                ),
            ),
        ),
        initial_subject=TOYOTA_SUBJECT,
        category="current_calibration_state",
        negative_truths=frozenset(
            {
                "no_stale_context_as_current_ro_state",
                "no_adas_si_as_current_ciq_state",
                "no_phrase_routing",
            }
        ),
    ),
    Scenario(
        "field_why_radar_calibration_needs_oem_evidence",
        (
            Turn(
                "Why does it need the radar calibration?",
                (
                    _nissan_ro_call(evidence_id="ciq-nissan-radar-question-21"),
                    _nissan_knowledge_miss_call(
                        event=("front bumper replacement", "Collision repair"),
                        evidence_id="knowledge-nissan-radar-miss",
                    ),
                    _nissan_radar_evidence_call(
                        event=("front bumper replacement", "Collision repair"),
                        evidence_id="adas-si-nissan-radar-trigger-p12",
                        finding=(
                            "Front radar aiming is required when the radar sensor is "
                            "removed during front bumper service."
                        ),
                        page=12,
                    ),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"adas_si"}),
                    frozenset(
                        {
                            "adas-si-nissan-radar-trigger-p12",
                        }
                    ),
                ),
                alternative_calls=(
                    (
                        _nissan_ro_call(evidence_id="ciq-nissan-radar-question-21"),
                        _nissan_radar_evidence_call(
                            event=("front bumper replacement", "Collision repair"),
                            evidence_id="adas-si-nissan-radar-trigger-p12",
                            finding=(
                                "Front radar aiming is required when the radar sensor is "
                                "removed during front bumper service."
                            ),
                            page=12,
                        ),
                    ),
                    (
                        _nissan_radar_evidence_call(
                            event=("front bumper replacement", "Collision repair"),
                            evidence_id="adas-si-nissan-radar-trigger-p12",
                            finding=(
                                "Front radar aiming is required when the radar sensor is "
                                "removed during front bumper service."
                            ),
                            page=12,
                        ),
                    ),
                ),
            ),
        ),
        initial_subject=NISSAN_SUBJECT,
        category="technical_evidence",
        negative_truths=frozenset(
            {
                "no_invented_data",
                "no_ciq_state_as_oem_proof",
                "no_adas_si_as_current_ciq_state",
            }
        ),
    ),
    Scenario(
        "field_show_procedure_opens_source_document",
        (
            Turn(
                "Show me the procedure.",
                (
                    _nissan_ro_call(evidence_id="ciq-nissan-procedure-context-21"),
                    _nissan_refreshed_procedure_search_call(),
                    _nissan_procedure_open_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"adas_si"}),
                    frozenset({"adas-open-nissan-radar-p18"}),
                ),
                alternative_calls=(
                    (
                        _nissan_subject_procedure_search_call(),
                        _nissan_procedure_open_call(),
                    ),
                    (
                        _nissan_subject_procedure_search_call(),
                        _nissan_ro_call(evidence_id="ciq-nissan-procedure-context-21"),
                        _nissan_procedure_open_call(),
                    ),
                ),
            ),
        ),
        initial_subject=NISSAN_PROCEDURE_SUBJECT,
        category="document_request",
        negative_truths=frozenset({"no_invented_data"}),
    ),
    Scenario(
        "field_follow_up_anything_else_refreshes_subject",
        (
            Turn(
                "Pull up RO 2400911667.",
                (_nissan_ro_call(evidence_id="ciq-nissan-followup-start-21"),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=False,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-nissan-followup-start-21"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        _nissan_ro_call(evidence_id="ciq-nissan-followup-start-21"),
                        ro_id="ro-nissan-1667",
                        ro_number="2400911667",
                        evidence_id="lookup-nissan-followup-start",
                        vehicle="2023 Nissan Rogue",
                        shop="Macon",
                    ),
                ),
            ),
            Turn(
                "Anything else on this car?",
                (_nissan_ro_call(evidence_id="ciq-nissan-followup-current-21"),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-nissan-followup-current-21"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        _nissan_ro_call(evidence_id="ciq-nissan-followup-current-21"),
                        ro_id="ro-nissan-1667",
                        ro_number="2400911667",
                        evidence_id="lookup-nissan-followup-current",
                        vehicle="2023 Nissan Rogue",
                        shop="Macon",
                    ),
                ),
            ),
        ),
        category="follow_up_reference",
        negative_truths=frozenset({"no_stale_context_as_current_ro_state"}),
    ),
    Scenario(
        "field_close_it_out_requires_verified_receipt",
        (
            Turn(
                "All right, we're done with this one. Close it out.",
                (
                    _toyota_ro_call(evidence_id="ciq-toyota-preclose-refresh-7"),
                    _toyota_verified_close_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-toyota-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-toyota-close-receipt-8"}),
                ),
            ),
        ),
        initial_subject=TOYOTA_SUBJECT,
        category="operational_mutation",
        negative_truths=frozenset({"no_unreceipted_mutation_success"}),
    ),
    Scenario(
        "field_shop_work_waiting_in_macon",
        (
            Turn(
                "What's still waiting in Macon?",
                (
                    CallExpectation(
                        "calibration_iq_read",
                        {
                            "status": "verified",
                            "verified": True,
                            "evidence_id": "ciq-macon-active-current",
                            "result_scope": "board_list_only",
                            "exact_ro_detail_included": False,
                            "filters": {"shop": "Macon", "include_completed": False},
                            "count": 2,
                            "rows": [
                                {
                                    "id": "ro-nissan-1667",
                                    "RO": "2400911667",
                                    "Vehicle": "2023 Nissan Rogue",
                                    "Status": "calibration_in_progress",
                                },
                                {
                                    "id": "ro-honda-1702",
                                    "RO": "2400911702",
                                    "Vehicle": "2024 Honda CR-V",
                                    "Status": "research",
                                },
                            ],
                            "collection_complete": True,
                        },
                        validator=_board_scope(shop="Macon"),
                    ),
                ),
                ReportExpectation(
                    _report("answered", found=True),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-macon-active-current"}),
                ),
                alternative_calls=((_macon_current_summary_call(),),),
            ),
        ),
        category="shop_work",
        negative_truths=frozenset({"no_invented_data"}),
    ),
    Scenario(
        "field_weekly_work_readiness",
        (
            Turn(
                "What do I have coming up this week?",
                (
                    CallExpectation(
                        "calibration_iq_work_prep",
                        {
                            "status": "verified",
                            "verified": True,
                            "mode": "week_readiness",
                            "evidence_id": "weekly-readiness-2026-08-26",
                            "requested_count": 3,
                            "processed_count": 3,
                            "mutation_count": 0,
                            "sources_checked": [
                                "calibration_iq",
                                "scrapex_adas_map",
                                "adas_si",
                            ],
                            "repair_orders": [
                                {
                                    "ro_number": "2400911667",
                                    "vehicle": "2023 Nissan Rogue",
                                    "shop": "Macon",
                                    "readiness": "blocked",
                                    "blockers": ["wheel alignment incomplete"],
                                },
                                {
                                    "ro_number": "2400611478",
                                    "vehicle": "2024 Toyota Camry",
                                    "shop": "Perry",
                                    "readiness": "ready",
                                },
                            ],
                        },
                        validator=_week_readiness_scope,
                    ),
                ),
                ReportExpectation(
                    _report("answered", found=True),
                    frozenset({"calibration_iq", "scrapex_adas_map", "adas_si"}),
                    frozenset({"weekly-readiness-2026-08-26"}),
                ),
            ),
        ),
        category="weekly_work",
        negative_truths=frozenset({"no_invented_data"}),
    ),
    Scenario(
        "field_nissan_alignment_technician_language",
        (
            Turn(
                "That Nissan we were talking about earlier, bumper's back on but alignment ain't done yet. Anything I can knock out before alignment?",
                (
                    _nissan_ro_call(evidence_id="ciq-nissan-alignment-current-21"),
                    _nissan_knowledge_miss_call(
                        event=("wheel alignment incomplete", "wheel alignment"),
                        evidence_id="knowledge-nissan-alignment-miss",
                    ),
                    _nissan_alignment_evidence_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq", "adas_si"}),
                    frozenset(
                        {
                            "ciq-nissan-alignment-current-21",
                            "adas-si-nissan-alignment-prereq-p20",
                        }
                    ),
                    path_requirements=(
                        (
                            ("calibration_iq_work_prep",),
                            frozenset({"calibration_iq", "scrapex_adas_map"}),
                            frozenset({"adas-map-nissan-alignment-verified-1"}),
                        ),
                    ),
                ),
                alternative_calls=(
                    (
                        _nissan_ro_call(evidence_id="ciq-nissan-alignment-current-21"),
                        _nissan_alignment_evidence_call(),
                    ),
                    (_nissan_alignment_work_prep_call(),),
                ),
            ),
        ),
        initial_subject=NISSAN_SUBJECT,
        category="natural_technician_language",
        negative_truths=frozenset(
            {
                "no_ciq_state_as_oem_proof",
                "no_invented_data",
                "no_unreceipted_mutation_success",
                "no_magic_wording",
                "no_phrase_routing",
            }
        ),
    ),
    Scenario(
        "field_ambiguous_procedure_uses_active_subject",
        (
            Turn(
                "Pull the procedure for that one.",
                (
                    _nissan_ro_call(evidence_id="ciq-nissan-ambiguous-procedure-21"),
                    _nissan_ambiguous_procedure_evidence_call(),
                    _nissan_procedure_open_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq", "adas_si"}),
                    frozenset(
                        {
                            "ciq-nissan-ambiguous-procedure-21",
                            "adas-si-nissan-ambiguous-procedure-p18",
                            "adas-open-nissan-radar-p18",
                        }
                    ),
                ),
            ),
        ),
        initial_subject=NISSAN_SUBJECT,
        category="ambiguous_reference",
        negative_truths=frozenset({"no_invented_data"}),
    ),
    Scenario(
        "field_explicit_context_switch_2400911667",
        (
            Turn(
                "Forget that one for a minute. Pull up RO 2400911667.",
                (_nissan_ro_call(evidence_id="ciq-nissan-explicit-switch-21"),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=False,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-nissan-explicit-switch-21"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        _nissan_ro_call(evidence_id="ciq-nissan-explicit-switch-21"),
                        ro_id="ro-nissan-1667",
                        ro_number="2400911667",
                        evidence_id="lookup-nissan-explicit-switch",
                        vehicle="2023 Nissan Rogue",
                        shop="Macon",
                    ),
                ),
            ),
            Turn(
                "What does this one need right now?",
                (_nissan_ro_call(evidence_id="ciq-nissan-after-switch-current-21"),),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        used_subject=True,
                        subject_id="ro-nissan-1667",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-nissan-after-switch-current-21"}),
                ),
                alternative_calls=(
                    _lookup_then_exact_ro(
                        _nissan_ro_call(
                            evidence_id="ciq-nissan-after-switch-current-21"
                        ),
                        ro_id="ro-nissan-1667",
                        ro_number="2400911667",
                        evidence_id="lookup-nissan-after-switch",
                        vehicle="2023 Nissan Rogue",
                        shop="Macon",
                    ),
                ),
            ),
        ),
        initial_subject=SUBJECT,
        category="explicit_context_switch",
        negative_truths=frozenset(
            {"no_stale_context_as_current_ro_state", "no_adas_si_as_current_ciq_state"}
        ),
    ),
    Scenario(
        "field_multi_step_toyota_procedures_to_case",
        (
            Turn(
                "Find out what calibrations this Toyota needs, get the OEM procedures, and put them in the case folder.",
                (
                    _toyota_ro_call(evidence_id="ciq-toyota-multistep-current-7"),
                    _toyota_procedure_search_call(),
                    _toyota_research_attach_call(),
                ),
                ReportExpectation(
                    _report(
                        "answered",
                        found=True,
                        execution="verified",
                        used_subject=True,
                        subject_id="ro-toyota-1478",
                    ),
                    frozenset({"calibration_iq", "adas_si"}),
                    frozenset(
                        {
                            "ciq-toyota-multistep-current-7",
                            "adas-si-toyota-camera-p34",
                            "mut-toyota-workspace-1",
                            "mut-toyota-document-import-1",
                        }
                    ),
                ),
                alternative_calls=(
                    (
                        _toyota_ro_call(evidence_id="ciq-toyota-multistep-current-7"),
                        _toyota_procedure_search_call(),
                        _toyota_procedure_open_call(),
                        _toyota_research_attach_call(),
                    ),
                    (
                        _toyota_ro_call(evidence_id="ciq-toyota-multistep-current-7"),
                        _toyota_research_attach_call(),
                    ),
                    (
                        _toyota_ro_call(evidence_id="ciq-toyota-multistep-current-7"),
                        _toyota_procedure_search_call(),
                        _toyota_mixed_research_calibration_call(),
                        _toyota_mixed_recovery_ro_call(),
                        _toyota_existing_calibration_research_call(),
                    ),
                    (
                        _toyota_ro_call(evidence_id="ciq-toyota-multistep-current-7"),
                        _toyota_procedure_search_call(),
                        _toyota_procedure_open_call(),
                        _toyota_mixed_research_calibration_call(),
                        _toyota_mixed_recovery_ro_call(),
                        _toyota_existing_calibration_research_call(),
                    ),
                ),
            ),
        ),
        initial_subject=TOYOTA_SUBJECT,
        category="multi_step",
        negative_truths=frozenset(
            {
                "no_ciq_state_as_oem_proof",
                "no_unreceipted_mutation_success",
                "no_hidden_zero_run_mixed_batch",
            }
        ),
    ),
    Scenario(
        "negative_close_indeterminate_is_not_success",
        (
            Turn(
                "Close this one out.",
                (
                    _toyota_ro_call(evidence_id="ciq-toyota-indeterminate-preclose-7"),
                    _toyota_indeterminate_close_call(),
                ),
                ReportExpectation(
                    _report(
                        "indeterminate",
                        found=False,
                        execution="not_confirmed",
                        used_subject=True,
                        subject_id="ro-toyota-1478",
                    ),
                    frozenset({"calibration_iq"}),
                    frozenset({"ciq-close-indeterminate-7"}),
                ),
            ),
        ),
        initial_subject=TOYOTA_SUBJECT,
        category="negative_mutation_truth",
        negative_truths=frozenset({"no_unreceipted_mutation_success"}),
    ),
)

CLOSE_PARAPHRASES = (
    "Close RO 2400611478.",
    "Close out 2400611478.",
    "We're done with that one.",
    "Take this RO off the board.",
    "Finish 2400611478.",
    "That repair order is complete. Close it.",
)
CALIBRATION_PARAPHRASES = (
    "What calibrations does this car need?",
    "What needs calibrated?",
    "What ADAS work is on this one?",
    "Show me the calibrations required for this RO.",
    "What else does this car need besides the radar?",
    "What are we doing calibration-wise?",
)

SCENARIOS = (
    *SCENARIOS,
    *ESCALATION_SCENARIOS,
    *FIELD_SCENARIOS,
    *(
        Scenario(
            f"close_paraphrase_{index + 1}",
            (
                Turn(
                    phrase,
                    (
                        _close_paraphrase_refresh_call(index),
                        _close_paraphrase_call(index),
                    ),
                    ReportExpectation(
                        _report(
                            "answered",
                            found=True,
                            execution="verified",
                            used_subject=True,
                            subject_id="ro-uuid-1478",
                        ),
                        frozenset({"calibration_iq"}),
                        frozenset({f"close-paraphrase-receipt-{index + 1}"}),
                    ),
                ),
            ),
            initial_subject=PARAPHRASE_SUBJECT,
        )
        for index, phrase in enumerate(CLOSE_PARAPHRASES)
    ),
    *(
        Scenario(
            f"calibration_paraphrase_{index + 1}",
            (
                Turn(
                    phrase,
                    (_five_turn_calibration_ro_call(),),
                    ReportExpectation(
                        _report(
                            "answered",
                            found=True,
                            used_subject=True,
                            subject_id="ro-uuid-1478",
                        ),
                        frozenset({"calibration_iq"}),
                        frozenset({"ro-calibrations-1478"}),
                    ),
                    alternative_calls=(
                        (
                            _five_turn_list_call(),
                            _five_turn_calibration_ro_call(),
                        ),
                    ),
                ),
            ),
            initial_subject=PARAPHRASE_SUBJECT,
        )
        for index, phrase in enumerate(CALIBRATION_PARAPHRASES)
    ),
)


class ModelProtocolError(AssertionError):
    pass


@dataclass
class TurnResult:
    report: dict[str, Any]
    raw_report: dict[str, Any]
    calls: list[dict[str, Any]]
    assistant_content: list[str]
    final_content: str
    elapsed_seconds: float


def _verified_close_without_child_calibration_state(
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for observation in observations:
        if observation.get("name") != "calibration_iq_operator":
            continue
        arguments = observation.get("arguments")
        actions = arguments.get("actions") if isinstance(arguments, dict) else None
        if not isinstance(actions, list):
            continue
        completion_operations: set[str] = set()
        for action in actions:
            if not isinstance(action, dict):
                continue
            operation = action.get("operation")
            if operation == "close_ro":
                completion_operations.add(operation)
            elif operation == "change_status":
                operation_arguments = action.get("arguments")
                status = (
                    operation_arguments.get("status")
                    if isinstance(operation_arguments, dict)
                    else None
                )
                if status in {"complete", "calibration complete"}:
                    completion_operations.add(operation)
        if not completion_operations:
            continue
        result = observation.get("result")
        if not isinstance(result, dict) or not (
            result.get("status") == "success"
            and result.get("success") is True
            and result.get("verified") is True
        ):
            continue
        receipts = result.get("receipts")
        if not isinstance(receipts, list) or not any(
            isinstance(receipt, dict)
            and receipt.get("operation") in completion_operations
            and receipt.get("status") == "completed"
            and isinstance(receipt.get("verification"), dict)
            and receipt["verification"].get("verified") is True
            for receipt in receipts
        ):
            continue
        snapshots = result.get("final_snapshots")
        if not isinstance(snapshots, dict) or not snapshots:
            continue
        child_state_present = any(
            isinstance(entry, dict)
            and isinstance(entry.get("snapshot"), dict)
            and "calibrations" in entry["snapshot"]
            for entry in snapshots.values()
        )
        if not child_state_present:
            return result
    return None


def _canonicalize_scrapex_report_execution(
    report: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Correct only the test-only enum from a fully proven ScrapeX result."""
    canonical_state: str | None = None
    for observation in observations:
        if observation.get("name") != "scrapex_adas_map":
            continue
        arguments = observation.get("arguments")
        result = observation.get("result")
        if not isinstance(arguments, dict) or not isinstance(result, dict):
            continue
        action = arguments.get("action")
        if action != "process_one" or result.get("action") != action:
            continue
        if (
            result.get("status") == "authentication_required"
            and result.get("authentication_required") is True
            and result.get("requires_human") is True
            and result.get("success") is False
            and result.get("executed") is False
            and result.get("verified") is False
            and result.get("work_complete") is False
        ):
            canonical_state = "not_confirmed"
            continue
        data = result.get("data")
        provenance = data.get("provenance") if isinstance(data, dict) else None
        if (
            result.get("status") == "completed"
            and result.get("success") is True
            and result.get("executed") is True
            and result.get("verified") is True
            and result.get("work_complete") is True
            and isinstance(data, dict)
            and data.get("attempted") is True
            and data.get("completed") is True
            and data.get("batch_id") == arguments.get("batch_id")
            and data.get("ro_number") == arguments.get("ro_number")
            and isinstance(provenance, dict)
            and provenance.get("verified") is True
        ):
            canonical_state = "verified"
    if canonical_state is None or report.get("execution_state") == canonical_state:
        return report
    normalized = deepcopy(report)
    normalized["execution_state"] = canonical_state
    return normalized


def _calibration_iq_operator_observation_is_verified(
    observation: dict[str, Any],
) -> bool:
    if observation.get("name") != "calibration_iq_operator":
        return False
    arguments = observation.get("arguments")
    result = observation.get("result")
    if not isinstance(arguments, dict) or not isinstance(result, dict):
        return False

    from core.orchestrator.loop import (
        _calibration_iq_operator_payload,
        calibration_iq_operator_result_is_verified,
    )

    payload = _calibration_iq_operator_payload(result)
    if not (
        calibration_iq_operator_result_is_verified(payload)
        and payload.get("executed") is True
    ):
        return False
    actions = arguments.get("actions")
    receipts = payload.get("receipts")
    snapshots = payload.get("final_snapshots")
    if not (
        isinstance(actions, list)
        and actions
        and isinstance(receipts, list)
        and isinstance(snapshots, dict)
        and snapshots
    ):
        return False

    research_reports = payload.get("research")
    research_reports = research_reports if isinstance(research_reports, list) else []
    research_receipt_operations = {
        "ensure_case_workspace",
        "import_document",
        "update_document",
        "link_document",
        "update_research",
    }
    for action in actions:
        if not isinstance(action, dict):
            return False
        operation = action.get("operation")
        if not isinstance(operation, str):
            return False
        action_ro = str(action.get("repair_order_id") or "").strip()
        matching_receipts = [
            receipt
            for receipt in receipts
            if isinstance(receipt, dict)
            and (
                receipt.get("operation") == operation
                or (
                    operation == "research_ro"
                    and receipt.get("operation") in research_receipt_operations
                )
            )
            and (
                not action_ro
                or not receipt.get("repair_order_id")
                or str(receipt.get("repair_order_id")).strip() == action_ro
            )
        ]
        if not matching_receipts:
            return False
        repair_order_id = action_ro or str(
            matching_receipts[0].get("repair_order_id") or ""
        ).strip()
        if not repair_order_id:
            return False
        if operation == "research_ro" and not any(
            isinstance(report, dict)
            and str(report.get("repair_order_id") or "").strip() == repair_order_id
            and report.get("completion_withheld") is not True
            for report in research_reports
        ):
            return False
        final = snapshots.get(repair_order_id)
        snapshot = final.get("snapshot") if isinstance(final, dict) else None
        if not (
            isinstance(final, dict)
            and final.get("status") == "verified"
            and isinstance(snapshot, dict)
        ):
            return False
        nested_ro = snapshot.get("repair_order")
        snapshot_ids = {
            str(value).strip()
            for value in (
                snapshot.get("id"),
                snapshot.get("repair_order_id"),
                nested_ro.get("id") if isinstance(nested_ro, dict) else None,
                (
                    nested_ro.get("repair_order_id")
                    if isinstance(nested_ro, dict)
                    else None
                ),
            )
            if value
        }
        if repair_order_id not in snapshot_ids:
            return False
    return True


def _calibration_iq_chain_has_receiptless_incomplete_attempt(
    observations: list[dict[str, Any]],
) -> bool:
    from core.orchestrator.loop import (
        _calibration_iq_operator_payload,
        calibration_iq_operator_result_is_verified,
    )

    for observation in observations:
        if observation.get("name") != "calibration_iq_operator":
            continue
        payload = _calibration_iq_operator_payload(observation.get("result"))
        receipts = payload.get("receipts")
        if (
            not calibration_iq_operator_result_is_verified(payload)
            and not (isinstance(receipts, list) and receipts)
            and payload.get("executed") is not True
        ):
            return True
    return False


def _mixed_zero_run_research_recovery(
    observations: list[dict[str, Any]],
) -> bool:
    rejected = False
    later_research_verified = False
    for observation in observations:
        if observation.get("name") != "calibration_iq_operator":
            continue
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        if (
            result.get("status") == "prerequisite_missing"
            and result.get("executed") is False
            and result.get("verified") is False
            and not result.get("receipts")
        ):
            rejected = True
            continue
        arguments = observation.get("arguments")
        actions = arguments.get("actions") if isinstance(arguments, dict) else None
        if rejected and isinstance(actions, list) and any(
            isinstance(action, dict) and action.get("operation") == "research_ro"
            for action in actions
        ):
            later_research_verified = _calibration_iq_operator_observation_is_verified(
                observation
            )
    return rejected and later_research_verified


def _canonicalize_calibration_iq_report_execution(
    report: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Promote only the test enum backed by the production terminal guard."""

    if not any(
        _calibration_iq_operator_observation_is_verified(observation)
        for observation in observations
    ):
        return report
    canonical_state = (
        "not_confirmed"
        if _calibration_iq_chain_has_receiptless_incomplete_attempt(observations)
        else "verified"
    )
    if report.get("execution_state") == canonical_state:
        return report
    normalized = deepcopy(report)
    normalized["execution_state"] = canonical_state
    return normalized


def _canonicalize_indeterminate_report_answer_truth(
    report: dict[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Correct one ambiguous test-only boolean without changing receipt truth."""

    if not (
        report.get("outcome") == "indeterminate"
        and report.get("execution_state") == "not_confirmed"
        and report.get("authoritative_answer_found") is True
    ):
        return report
    if any(
        _calibration_iq_operator_observation_is_verified(observation)
        for observation in observations
    ):
        return report

    for observation in observations:
        if observation.get("name") != "calibration_iq_operator":
            continue
        result = observation.get("result")
        if not isinstance(result, dict):
            continue
        receipts = result.get("receipts")
        receipt_truth = receipts if isinstance(receipts, list) else []
        if (
            result.get("status") == "indeterminate"
            and result.get("success") is False
            and result.get("verified") is False
            and result.get("may_have_executed") is True
            and result.get("retryable") is False
            and not result.get("final_snapshots")
            and receipt_truth
            and all(
                isinstance(receipt, dict)
                and receipt.get("status") == "indeterminate"
                and isinstance(receipt.get("verification"), dict)
                and receipt["verification"].get("verified") is False
                for receipt in receipt_truth
            )
        ):
            normalized = deepcopy(report)
            normalized["authoritative_answer_found"] = False
            return normalized
    return report


def _canonicalize_active_subject_usage(
    report: dict[str, Any],
    observations: list[dict[str, Any]],
    active_subject: dict[str, Any] | None,
) -> dict[str, Any]:
    """Correct only same-resource instrumentation from trusted structured context."""

    if report.get("used_active_subject") is True or not isinstance(active_subject, dict):
        return report
    payload = active_subject.get("payload")
    if not isinstance(payload, dict):
        return report
    nested_subject_ro = payload.get("repair_order")
    subject_ids = {
        str(value).strip()
        for value in (
            payload.get("resource_id"),
            payload.get("repair_order_id"),
            payload.get("ro_number"),
            (
                nested_subject_ro.get("id")
                if isinstance(nested_subject_ro, dict)
                else None
            ),
            (
                nested_subject_ro.get("ro_number")
                if isinstance(nested_subject_ro, dict)
                else None
            ),
        )
        if str(value or "").strip()
    }
    if not subject_ids:
        return report

    for observation in observations:
        if observation.get("name") != "calibration_iq_ro":
            continue
        arguments = observation.get("arguments")
        requested_id = (
            str(arguments.get("repair_order_id") or "").strip()
            if isinstance(arguments, dict)
            else ""
        )
        result = observation.get("result")
        raw = result.get("raw") if isinstance(result, dict) else None
        result_ro = raw.get("repair_order") if isinstance(raw, dict) else None
        result_ids = {
            str(value).strip()
            for value in (
                result_ro.get("id") if isinstance(result_ro, dict) else None,
                (
                    result_ro.get("ro_number")
                    if isinstance(result_ro, dict)
                    else None
                ),
            )
            if str(value or "").strip()
        }
        if (
            requested_id in subject_ids
            and isinstance(result, dict)
            and result.get("status") == "verified"
            and bool(result_ids & subject_ids)
        ):
            normalized = deepcopy(report)
            normalized["used_active_subject"] = True
            return normalized
    return report


class LiveQwenHarness:
    def __init__(self, target: WorkerTarget, *, timeout: float = 300.0) -> None:
        self.target = target
        self.timeout = timeout
        by_name = {item["function"]["name"]: item for item in _tool_list()}
        # Preserve the actual configured ADAS profile catalog and its production
        # order.  The report tool is exposed separately only after business
        # selection completes; it is assertion instrumentation, not a capability.
        self.business_tools = [
            item
            for item in _production_profile_tools()
            if item["function"]["name"] != "acceptance_report"
        ]
        self.report_tools = [by_name["acceptance_report"]]

    def _business_tools_for_evidence(
        self,
        calibration_iq_evidence: Any,
        scrapex_evidence: Any = None,
    ) -> list[dict[str, Any]]:
        from core.tools.registry import (
            calibration_iq_catalog_for_turn,
            scrapex_catalog_for_turn,
        )

        catalog = calibration_iq_catalog_for_turn(
            self.business_tools,
            calibration_iq_evidence,
        )
        return scrapex_catalog_for_turn(catalog, scrapex_evidence)

    @staticmethod
    def _assert_advertised_call_schema(
        name: str,
        arguments: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> None:
        from jsonschema import Draft202012Validator

        tool = next(
            (
                item
                for item in tools
                if (item.get("function") or {}).get("name") == name
            ),
            None,
        )
        if tool is None:
            raise ModelProtocolError(
                f"model called unadvertised staged tool {name!r}; no fixture result "
                "was exposed"
            )
        schema = (tool.get("function") or {}).get("parameters") or {}
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if errors:
            raise ModelProtocolError(
                f"{name} arguments were outside the advertised staged schema; "
                f"no fixture result was exposed: {errors[0].message}"
            )

    @staticmethod
    def _staged_binding_block(
        name: str,
        arguments: dict[str, Any],
        evidence: Any,
        *,
        conversation_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        from core.tools.registry import (
            ToolBlocked,
            validate_calibration_iq_write_binding,
        )

        try:
            validate_calibration_iq_write_binding(
                name,
                arguments,
                evidence,
                conversation_id=conversation_id,
                message_id=message_id,
            )
        except ToolBlocked as exc:
            return {"status": "blocked", "message": str(exc)}
        return None

    @staticmethod
    def _scrapex_binding_block(
        name: str,
        arguments: dict[str, Any],
        evidence: Any,
        *,
        conversation_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        from core.tools.registry import ToolBlocked, validate_scrapex_batch_binding

        try:
            validate_scrapex_batch_binding(
                name,
                arguments,
                evidence,
                conversation_id=conversation_id,
                message_id=message_id,
            )
        except ToolBlocked as exc:
            return {"status": "blocked", "message": str(exc)}
        return None

    def _completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        force_tool: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.target.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 640,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            if force_tool and tool_choice is not None:
                raise ValueError("force_tool and tool_choice are mutually exclusive")
            payload["tool_choice"] = (
                {"type": "function", "function": {"name": force_tool}}
                if force_tool
                else (tool_choice or "auto")
            )
        with httpx.Client(
            timeout=httpx.Timeout(15.0, read=self.timeout, write=60.0, pool=15.0),
            trust_env=False,
        ) as client:
            response = client.post(
                f"{self.target.endpoint}/chat/completions",
                json=payload,
            )
        if response.status_code != 200:
            raise ModelProtocolError(
                f"worker returned HTTP {response.status_code}: {response.text[:1000]}"
            )
        try:
            return response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelProtocolError(
                f"malformed worker response: {response.text[:1000]}"
            ) from exc

    def _model_owned_no_tool_self_check(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        draft: str,
        *,
        require_tool: bool,
    ) -> Any:
        from core.orchestrator.loop import model_owned_no_tool_self_check

        harness = self

        class CompletionStreamAdapter:
            supports_no_tool_self_check = True

            async def stream(
                self,
                review_messages: list[dict[str, Any]],
                *,
                tools: list[dict[str, Any]],
                tool_choice: str | dict[str, Any] | None = None,
            ) -> Any:
                response = harness._completion(
                    review_messages,
                    tools,
                    tool_choice=tool_choice,
                )
                content = response.get("content")
                if isinstance(content, str) and content:
                    yield {"type": "content", "text": content}
                for raw_call in response.get("tool_calls") or []:
                    function = raw_call.get("function") or {}
                    yield {
                        "type": "tool_call",
                        "id": raw_call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "{}",
                    }

        return asyncio.run(
            model_owned_no_tool_self_check(
                CompletionStreamAdapter(),
                messages,
                tools,
                draft,
                require_tool=require_tool,
            )
        )

    @staticmethod
    def _active_subject_message(
        subject: dict[str, Any] | None,
    ) -> dict[str, str] | None:
        from core.orchestrator import prompt as production_prompt

        content = production_prompt._active_subject_context(  # noqa: SLF001
            subject,
            production_prompt.ACTIVE_SUBJECT_CONTEXT_MAX_CHARS,
        )
        return {"role": "system", "content": content} if content else None

    def _assert_close_final_truth(
        self,
        *,
        final_content: str,
        close_result: dict[str, Any],
    ) -> None:
        audit = self._completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a semantic acceptance auditor, not the user-facing assistant. "
                        "The verified whole-RO completion result below proves only repair-order/"
                        "workflow completion. Its final snapshot contains no child calibrations "
                        "field. "
                        "Determine whether the assistant response makes any claim about child "
                        "calibration state, including whether calibration work is required, "
                        "pending, remaining, performed, finished, completed, or absent. A bare "
                        "product name such as Calibration IQ is not itself a child-state claim. "
                        "Call acceptance_close_truth_audit exactly once."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "verified_close_result": close_result,
                            "assistant_response": final_content,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            [CLOSE_FINAL_TRUTH_AUDIT_TOOL],
            "acceptance_close_truth_audit",
        )
        raw_calls = audit.get("tool_calls") or []
        if len(raw_calls) != 1:
            raise ModelProtocolError(
                "close final semantic audit did not emit exactly one structured result"
            )
        function = raw_calls[0].get("function") or {}
        if function.get("name") != "acceptance_close_truth_audit":
            raise ModelProtocolError(
                "close final semantic audit emitted the wrong tool"
            )
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError) as exc:
            raise ModelProtocolError(
                "close final semantic audit emitted invalid JSON"
            ) from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("close final semantic audit was not an object")
        if (
            arguments.get("mentions_child_calibration_state") is not False
            or arguments.get("unsupported_child_calibration_claim") is not False
        ):
            raise ModelProtocolError(
                "close final response mentioned unsupported child calibration state: "
                + str(arguments.get("rationale") or arguments)
            )

    def _assert_declared_negative_truths(
        self,
        *,
        final_content: str,
        report: dict[str, Any],
        raw_report: dict[str, Any],
        observations: list[dict[str, Any]],
        contracts: frozenset[str],
    ) -> None:
        if not contracts:
            return
        audit = self._completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a semantic acceptance auditor, not the user-facing "
                        "assistant. Compare the final response with the observed tool calls, "
                        "fixture results, structured terminal report, and declared truth "
                        "contracts. Evaluate meaning, not wording. Call "
                        "acceptance_final_truth_audit exactly once. Mark unsupported_claim "
                        "true and list every violated contract if the response exceeds the "
                        "observed evidence or execution state."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "contracts": {
                                name: NEGATIVE_TRUTH_DESCRIPTIONS[name]
                                for name in sorted(contracts)
                            },
                            "observations": observations,
                            "raw_terminal_report": raw_report,
                            "terminal_report": report,
                            "assistant_response": final_content,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            [FINAL_TRUTH_AUDIT_TOOL],
            "acceptance_final_truth_audit",
        )
        raw_calls = audit.get("tool_calls") or []
        if len(raw_calls) != 1:
            raise ModelProtocolError(
                "final truth audit did not emit exactly one structured result"
            )
        function = raw_calls[0].get("function") or {}
        if function.get("name") != "acceptance_final_truth_audit":
            raise ModelProtocolError("final truth audit emitted the wrong tool")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError) as exc:
            raise ModelProtocolError("final truth audit emitted invalid JSON") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError("final truth audit was not an object")
        violations = set(arguments.get("violated_contracts") or [])
        if arguments.get("unsupported_claim") is not False or violations:
            raise ModelProtocolError(
                "final response violated declared truth contract(s): "
                + str(arguments.get("rationale") or sorted(violations) or arguments)
            )

    def run_turn(
        self,
        turn: Turn,
        *,
        subject: dict[str, Any] | None,
        prior_dialogue: list[dict[str, str]],
        negative_truths: frozenset[str] = frozenset(),
    ) -> tuple[TurnResult, dict[str, Any] | None, list[dict[str, str]]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _production_system_prompt()},
            {"role": "system", "content": ACCEPTANCE_HARNESS_BOUNDARY},
        ]
        subject_message = self._active_subject_message(subject)
        if subject_message:
            messages.append(subject_message)
        messages.extend(prior_dialogue)
        messages.append({"role": "user", "content": turn.user})

        call_index = 0
        possible_paths = [turn.calls, *turn.alternative_calls]
        calls: list[dict[str, Any]] = []
        business_observations: list[dict[str, Any]] = []
        content_fragments: list[str] = []
        started = time.perf_counter()
        report: dict[str, Any] | None = None
        raw_report: dict[str, Any] | None = None
        report_tool_call_id: str | None = None
        next_subject = subject
        subject_store = _FixtureSubjectStore(subject)
        turn_conversation_id = 1
        turn_message_id = max(1, (len(prior_dialogue) // 2) + 1)
        calibration_iq_evidence: Any = None
        scrapex_evidence: Any = None
        report_instruction_added = False

        max_business_calls = max(len(path) for path in possible_paths)
        for _step in range(max_business_calls + 3):
            round_business_tools = self._business_tools_for_evidence(
                calibration_iq_evidence,
                scrapex_evidence,
            )
            advertised_business_names = {
                item["function"]["name"] for item in round_business_tools
            }
            call_calibration_iq_evidence = calibration_iq_evidence
            next_calibration_iq_evidence = calibration_iq_evidence
            # Production freezes authorization evidence for the whole model
            # batch. Results visible during this batch accumulate separately
            # and are promoted only before the next model round.
            call_scrapex_evidence = scrapex_evidence
            next_scrapex_evidence = scrapex_evidence
            report_phase = bool(possible_paths) and all(
                call_index == len(path) for path in possible_paths
            )
            if report_phase and not report_instruction_added:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Acceptance instrumentation phase: call acceptance_report now "
                            "with the structured semantic outcome. Do not answer in prose. "
                            "requested_credentials describes only whether you asked the user "
                            "to send a secret; the user's own mention of a password does not count. "
                            "used_active_subject is true only when an active-subject system context "
                            "existed at the beginning of this turn and was used; an RO first read "
                            "during this turn becomes durable for later turns and does not count yet. "
                            "sources_checked must include the source of every business result: in "
                            "particular, any calibration_iq_ro result requires calibration_iq even "
                            "when a later knowledge or ADAS result supplied the answer. "
                            "observed_evidence_ids must include evidence or source-resource ids "
                            "decisive for the terminal outcome. Include final hits, misses, blocked "
                            "results, mutation receipts, and batch/item/provenance ids when the "
                            "service emits no evidence_id. An intermediate setup/list id may be "
                            "omitted when the next matched call demonstrably copied and consumed "
                            "its opaque id. "
                            "execution_state is the mutation state, not the evidence-verification "
                            "state: if this turn only called reads such as calibration_iq_ro, use "
                            "not_requested even though the read result says status=verified. "
                            "But this read rule never overrides an acquisition/process mutation "
                            "that the user requested and authentication_required blocked: that "
                            "case is not_confirmed even when the blocked result says executed=false. "
                            "For outcome precedence, authentication_required=true or "
                            "requires_human=true always means outcome=blocked, never "
                            "approval_required; approval_required is reserved for an explicit "
                            "Owner approval result."
                        ),
                    }
                )
                report_instruction_added = True
            assistant = self._completion(
                messages,
                self.report_tools if report_phase else round_business_tools,
                "acceptance_report" if report_phase else None,
            )
            content = assistant.get("content")
            raw_calls = assistant.get("tool_calls") or []
            if not raw_calls and not report_phase and call_index == 0:
                self_check = self._model_owned_no_tool_self_check(
                    messages,
                    round_business_tools,
                    str(content or ""),
                    require_tool=subject is not None,
                )
                if self_check.tool_calls:
                    content = ""
                    raw_calls = [
                        {
                            "id": call.get("id"),
                            "type": "function",
                            "function": {
                                "name": call.get("name"),
                                "arguments": call.get("arguments") or "{}",
                            },
                        }
                        for call in self_check.tool_calls
                    ]
                elif self_check.accept_draft:
                    raise ModelProtocolError(
                        "production no-tool self-check accepted a draft that did not "
                        "satisfy the declared business path"
                    )
                else:
                    raise ModelProtocolError(
                        "production no-tool self-check failed closed before the declared "
                        "business path"
                    )
            if not raw_calls:
                raise ModelProtocolError(
                    f"model stopped without acceptance_report after {call_index}/"
                    f"{max_business_calls} business calls; content={content!r}"
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": raw_calls,
                }
            )
            for raw_call in raw_calls:
                function = raw_call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError) as exc:
                    raise ModelProtocolError(
                        f"{name or 'unnamed tool'} emitted invalid JSON arguments"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelProtocolError(
                        f"{name} arguments were not an object: {arguments!r}"
                    )
                calls.append({"name": name, "arguments": arguments})

                if name == "acceptance_report":
                    if not report_phase:
                        raise ModelProtocolError(
                            "acceptance_report was unavailable during business selection"
                        )
                    if len(raw_calls) != 1:
                        raise ModelProtocolError(
                            "acceptance_report must be a terminal single call"
                        )
                    if not possible_paths or not all(
                        call_index == len(path) for path in possible_paths
                    ):
                        raise ModelProtocolError(
                            f"acceptance_report arrived after {call_index}/"
                            f"{max_business_calls} possible business calls"
                        )
                    raw_report = deepcopy(arguments)
                    arguments = _canonicalize_scrapex_report_execution(
                        raw_report,
                        business_observations,
                    )
                    arguments = _canonicalize_calibration_iq_report_execution(
                        arguments,
                        business_observations,
                    )
                    arguments = _canonicalize_indeterminate_report_answer_truth(
                        arguments,
                        business_observations,
                    )
                    arguments = _canonicalize_active_subject_usage(
                        arguments,
                        business_observations,
                        subject,
                    )
                    report_expectation = turn.report
                    if (
                        arguments.get("execution_state") == "not_confirmed"
                        and turn.report.subset.get("execution_state") == "verified"
                        and _calibration_iq_chain_has_receiptless_incomplete_attempt(
                            business_observations
                        )
                    ):
                        conservative_subset = deepcopy(turn.report.subset)
                        conservative_subset["execution_state"] = "not_confirmed"
                        report_expectation = ReportExpectation(
                            conservative_subset,
                            turn.report.required_sources,
                            turn.report.required_evidence_ids,
                            turn.report.path_requirements,
                        )
                    report_expectation.check(
                        arguments,
                        call_path=tuple(
                            str(observation.get("name") or "")
                            for observation in business_observations
                        ),
                    )
                    report = arguments
                    report_tool_call_id = raw_call.get("id") or "acceptance-report"
                    break

                if report_phase:
                    raise ModelProtocolError(
                        f"unexpected extra business call {name} with {arguments!r}"
                    )
                if name not in advertised_business_names:
                    raise ModelProtocolError(
                        f"model called unadvertised staged tool {name!r}; no fixture result "
                        "was exposed"
                    )
                self._assert_advertised_call_schema(
                    name,
                    arguments,
                    round_business_tools,
                )
                binding_block = None
                from core.tools.registry import (
                    CALIBRATION_IQ_STAGED_WRITE_TOOLS,
                    SCRAPEX_STAGED_TOOLS,
                )

                if name in CALIBRATION_IQ_STAGED_WRITE_TOOLS:
                    binding_block = self._staged_binding_block(
                        name,
                        arguments,
                        call_calibration_iq_evidence,
                        conversation_id=turn_conversation_id,
                        message_id=turn_message_id,
                    )
                    # Production consumes the same-turn unlock on the first
                    # write attempt, including one rejected by the binding gate.
                    call_calibration_iq_evidence = None
                    next_calibration_iq_evidence = None
                if name in SCRAPEX_STAGED_TOOLS:
                    binding_block = self._scrapex_binding_block(
                        name,
                        arguments,
                        call_scrapex_evidence,
                        conversation_id=turn_conversation_id,
                        message_id=turn_message_id,
                    )
                name_candidates = [
                    path
                    for path in possible_paths
                    if call_index < len(path) and path[call_index].name == name
                ]
                if not name_candidates:
                    expected_names = sorted(
                        {
                            path[call_index].name
                            for path in possible_paths
                            if call_index < len(path)
                        }
                    )
                    raise ModelProtocolError(
                        f"business call {call_index + 1}: expected one of {expected_names}, "
                        f"got {name} with {arguments!r}"
                    )
                valid_candidates: list[tuple[CallExpectation, ...]] = []
                validation_errors: list[str] = []
                for path in name_candidates:
                    expectation = path[call_index]
                    try:
                        expectation.check(arguments)
                        expects_binding_block = (
                            expectation.result.get("status") == "blocked"
                        )
                        if binding_block is not None and not expects_binding_block:
                            raise AssertionError(binding_block["message"])
                        if binding_block is None and expects_binding_block:
                            raise AssertionError(
                                "expected the production staged-write gate to block"
                            )
                    except AssertionError as exc:
                        validation_errors.append(str(exc))
                    else:
                        valid_candidates.append(path)
                if not valid_candidates:
                    raise ModelProtocolError(
                        f"business call {call_index + 1} arguments did not match any "
                        f"accepted {name} path: {'; '.join(validation_errors)}; "
                        f"arguments={arguments!r}"
                    )
                possible_paths = valid_candidates
                expectation = possible_paths[0][call_index]
                call_index += 1
                result = binding_block or expectation.result_for(arguments)
                business_observations.append(
                    {"name": name, "arguments": arguments, "result": result}
                )
                # Exercise the production merge hook against an isolated test
                # store instead of replacing context from a fixture-invented field.
                from core.services.conversation_subjects import (
                    track_active_subject_from_tool_result,
                )

                tracked = track_active_subject_from_tool_result(
                    subject_store,
                    conversation_id=turn_conversation_id,
                    tool_name=name,
                    result=result,
                    tool_call_id=raw_call.get("id") or f"fixture-{call_index}",
                )
                if tracked is not None:
                    next_subject = tracked
                if name == "calibration_iq_ro":
                    from core.tools.registry import calibration_iq_evidence_from_result

                    next_calibration_iq_evidence = calibration_iq_evidence_from_result(
                        name,
                        result,
                        conversation_id=turn_conversation_id,
                        message_id=turn_message_id,
                        source_tool_call_id=(
                            raw_call.get("id") or f"fixture-{call_index}"
                        ),
                        previous=next_calibration_iq_evidence,
                    )
                if name in SCRAPEX_STAGED_TOOLS:
                    from core.orchestrator.loop import tool_result_visible_to_model
                    from core.tools.registry import (
                        scrapex_apply_new_quarantine,
                        scrapex_evidence_from_result,
                    )

                    next_scrapex_evidence = scrapex_evidence_from_result(
                        name,
                        arguments,
                        tool_result_visible_to_model(name, result),
                        conversation_id=turn_conversation_id,
                        message_id=turn_message_id,
                        source_tool_call_id=(
                            raw_call.get("id") or f"fixture-{call_index}"
                        ),
                        previous=next_scrapex_evidence,
                    )
                    call_scrapex_evidence = scrapex_apply_new_quarantine(
                        call_scrapex_evidence,
                        next_scrapex_evidence,
                    )
                from core.orchestrator.loop import tool_result_json_for_model

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": raw_call.get("id") or f"fixture-{call_index}",
                        "name": name,
                        "content": tool_result_json_for_model(name, result),
                    }
                )
            calibration_iq_evidence = next_calibration_iq_evidence
            scrapex_evidence = next_scrapex_evidence
            if report is not None:
                break

        if report is None:
            raise ModelProtocolError(
                "model exceeded the bounded loop without acceptance_report"
            )
        if raw_report is None:
            raise ModelProtocolError(
                "acceptance_report raw instrumentation was not retained"
            )
        if not possible_paths or not all(
            call_index == len(path) for path in possible_paths
        ):
            raise ModelProtocolError(
                f"model completed only {call_index}/{max_business_calls} possible business calls"
            )

        # Exercise the real no-tools conversational synthesis path after the
        # structured semantic assertions. The report is test instrumentation;
        # the next response is what a user-facing turn would actually sound like.
        messages.append(
            {
                "role": "tool",
                "tool_call_id": report_tool_call_id or "acceptance-report",
                "name": "acceptance_report",
                "content": json.dumps(
                    {"accepted": True, "semantic_outcome": report},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
        outcome = str(report.get("outcome") or "")
        execution_state = str(report.get("execution_state") or "")
        close_without_child_state = _verified_close_without_child_calibration_state(
            business_observations
        )
        mixed_research_recovery = _mixed_zero_run_research_recovery(
            business_observations
        )
        if mixed_research_recovery:
            terminal_truth = (
                "The earlier mixed research_ro plus add_calibration batch was rejected "
                "before execution and ran nothing. A later exact reread proved the existing "
                "calibration, and only the subsequent research attachment was verified by its "
                "expanded receipts and final snapshot. State all three facts explicitly; do "
                "not claim that add_calibration succeeded or call the whole chain an "
                "unqualified success."
            )
        elif outcome == "approval_required":
            terminal_truth = (
                "The requested mutation was not attempted and did not execute. State plainly "
                "that approval is required; do not describe it as attempted, initiated, started, "
                "executed, changed, removed, or completed."
            )
        elif outcome == "blocked":
            authentication_boundary = any(
                observation["result"].get("authentication_required") is True
                or observation["result"].get("requires_human") is True
                for observation in business_observations
            )
            if authentication_boundary:
                terminal_truth = (
                    "The requested work is blocked and not confirmed. Describe the human or "
                    "authentication boundary without claiming execution or requesting a secret. "
                    "If acquisition or processing returned executed=false, do not use attempted, "
                    "initiated, started, or executed for that requested work. State only that an "
                    "earlier verified batch-creation result created a batch and that processing "
                    "did not run because authentication is required. Lead with: 'A batch was "
                    "created, but processing did not run because managed-browser sign-in is "
                    "required.' Do not characterize the overall request as an attempt."
                )
            else:
                terminal_truth = (
                    "The requested write was blocked by the structured binding gate and nothing "
                    "ran. State that the exact RO must be refreshed before another write attempt. "
                    "Do not claim that the mutation was attempted, initiated, executed, changed, "
                    "removed, or completed."
                )
        elif outcome == "indeterminate":
            terminal_truth = (
                "The requested mutation is indeterminate and not verified. State that it may "
                "have executed and needs an authoritative reread before any retry. Do not "
                "claim success, failure, completion, or safe retry."
            )
        elif outcome == "no_authoritative_answer":
            terminal_truth = (
                "No authoritative source established the answer. State that limitation and "
                "do not infer a yes/no technical requirement."
            )
        elif execution_state == "verified":
            if close_without_child_state is not None:
                terminal_truth = (
                    "The verified whole-RO completion result contains repair_order and workflow "
                    "fields but no child calibrations state. Limit the response to the returned "
                    "RO/workflow completion facts (for example status, phase, version, and "
                    "active-board removal). "
                    "Do not mention child calibration state at all: do not say calibration work "
                    "is required, pending, remaining, performed, finished, completed, or absent."
                )
            else:
                terminal_truth = (
                    "A mutation may be described as complete only to the exact extent established "
                    "by the verified receipt and final snapshot above."
                )
        else:
            terminal_truth = (
                "Describe only facts established by the read results above."
            )
        messages.append(
            {
                "role": "system",
                "content": (
                    "Now give the user a concise conversational answer. Ground it only in the "
                    "business tool results above; preserve no-result, approval, authentication, "
                    "and receipt truth. Do not mention this acceptance instrumentation. "
                    + terminal_truth
                ),
            }
        )
        final_assistant = self._completion(messages, [])
        if final_assistant.get("tool_calls"):
            raise ModelProtocolError(
                "final conversational synthesis emitted an unexpected tool call"
            )
        final_content = str(final_assistant.get("content") or "").strip()
        if not final_content:
            raise ModelProtocolError("final conversational synthesis was empty")
        if close_without_child_state is not None:
            self._assert_close_final_truth(
                final_content=final_content,
                close_result=close_without_child_state,
            )
        self._assert_declared_negative_truths(
            final_content=final_content,
            report=report,
            raw_report=raw_report,
            observations=business_observations,
            contracts=negative_truths,
        )
        content_fragments.append(final_content)

        elapsed = time.perf_counter() - started
        updated_dialogue = [
            *prior_dialogue,
            {"role": "user", "content": turn.user},
            {"role": "assistant", "content": final_content},
        ]
        return (
            TurnResult(
                report,
                raw_report,
                calls,
                content_fragments,
                final_content,
                elapsed,
            ),
            next_subject,
            updated_dialogue,
        )

    def run_scenario(self, scenario: Scenario) -> list[TurnResult]:
        subject = scenario.initial_subject
        dialogue: list[dict[str, str]] = []
        results: list[TurnResult] = []
        for index, turn in enumerate(scenario.turns):
            try:
                result, subject, dialogue = self.run_turn(
                    turn,
                    subject=subject,
                    prior_dialogue=dialogue,
                    negative_truths=scenario.negative_truths,
                )
            except Exception as exc:
                raise ModelProtocolError(
                    f"turn {index + 1} ({turn.user!r}): {exc}"
                ) from exc
            results.append(result)
        return results


def run_suite(
    target: WorkerTarget,
    *,
    scenario_names: set[str] | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    selected = [
        scenario
        for scenario in SCENARIOS
        if not scenario_names or scenario.name in scenario_names
    ]
    if scenario_names:
        missing = scenario_names - {scenario.name for scenario in selected}
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(missing))}")

    harness = LiveQwenHarness(target, timeout=timeout)
    suite_started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    failures = 0
    for scenario in selected:
        started = time.perf_counter()
        try:
            turn_results = harness.run_scenario(scenario)
        except Exception as exc:  # noqa: BLE001 - acceptance report must retain each failure
            failures += 1
            rows.append(
                {
                    "scenario": scenario.name,
                    "category": scenario.category,
                    "negative_truths": sorted(scenario.negative_truths),
                    "status": "failed",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            rows.append(
                {
                    "scenario": scenario.name,
                    "category": scenario.category,
                    "negative_truths": sorted(scenario.negative_truths),
                    "status": "passed",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "turns": [
                        {
                            "calls": [call["name"] for call in result.calls],
                            "report": {
                                key: result.report[key]
                                for key in (
                                    "outcome",
                                    "authoritative_answer_found",
                                    "execution_state",
                                    "used_active_subject",
                                    "requested_credentials",
                                )
                            },
                            "raw_report": result.raw_report,
                            "final_response": result.final_content,
                            "elapsed_seconds": round(result.elapsed_seconds, 3),
                        }
                        for result in turn_results
                    ],
                }
            )

    return {
        "worker": {"endpoint": target.endpoint, "model": target.model},
        "isolation": {
            "business_handlers_invoked": False,
            "business_results": "in_process_fixtures",
            "network_target": target.endpoint,
        },
        "summary": {
            "scenarios": len(selected),
            "passed": len(selected) - failures,
            "failed": failures,
            "elapsed_seconds": round(time.perf_counter() - suite_started, 3),
        },
        "results": rows,
    }


def run_production_catalog_smoke(
    target: WorkerTarget, *, timeout: float = 300.0
) -> dict[str, Any]:
    """Prove one semantic choice with the production prompt and initial catalog.

    The scenario suite and this smoke check both load the configured ADAS
    profile schemas in production registry order and the real system prompt.
    The initial turn catalog applies the production staged-write filter, and no
    business handler is invoked.
    """
    from core.orchestrator.prompt import system_prompt

    from core.tools.registry import calibration_iq_catalog_for_turn

    tools = calibration_iq_catalog_for_turn(_production_profile_tools(), None)
    router = SimpleNamespace(
        active_config=lambda: SimpleNamespace(supports_vision=True, supports_audio=True)
    )
    harness = LiveQwenHarness(target, timeout=timeout)
    started = time.perf_counter()
    response = harness._completion(
        [
            {"role": "system", "content": system_prompt(router)},
            {
                "role": "user",
                "content": (
                    "Give me the number of unfinished vehicles in phase five. "
                    "I need the aggregate, not the repair-order rows."
                ),
            },
        ],
        tools,
    )
    raw_calls = response.get("tool_calls") or []
    calls = [
        {
            "name": item.get("function", {}).get("name"),
            "arguments": json.loads(item.get("function", {}).get("arguments") or "{}"),
        }
        for item in raw_calls
        if isinstance(item, dict)
    ]
    aggregate_reads = [
        call
        for call in calls
        if call["name"] in {"calibration_iq_summary", "calibration_iq_work_prep"}
    ]
    if len(aggregate_reads) != 1:
        raise ModelProtocolError(
            "full production catalog did not select exactly one bounded aggregate read: "
            + json.dumps(calls, ensure_ascii=False)
        )
    selected = aggregate_reads[0]
    arguments = selected["arguments"]
    if str(arguments.get("phase") or "").strip() != "5":
        raise ModelProtocolError(
            "full production catalog aggregate read did not preserve phase 5: "
            + json.dumps(arguments, ensure_ascii=False)
        )
    if (
        selected["name"] == "calibration_iq_work_prep"
        and arguments.get("mode") != "phase_list"
    ):
        raise ModelProtocolError(
            "full production catalog work-prep read was not phase_list: "
            + json.dumps(arguments, ensure_ascii=False)
        )
    forbidden = {
        "calibration_iq_update",
        "calibration_iq_operator",
        "calibration_iq_destructive",
    }
    if any(call["name"] in forbidden for call in calls):
        raise ModelProtocolError(
            "read-only aggregate request selected a write tool: "
            + json.dumps(calls, ensure_ascii=False)
        )
    return {
        "status": "passed",
        "tool_count": len(tools),
        "selected_calls": calls,
        "business_handlers_invoked": False,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def test_field_acceptance_contract_is_structurally_complete() -> None:
    categories = {scenario.category for scenario in FIELD_SCENARIOS}
    declared_truths = set().union(*(scenario.negative_truths for scenario in SCENARIOS))

    assert FIELD_ACCEPTANCE_CATEGORIES <= categories
    assert NEGATIVE_TRUTH_CONTRACTS <= declared_truths
    assert len({scenario.name for scenario in SCENARIOS}) == len(SCENARIOS)
    assert len(FIELD_SCENARIOS) == 13


def test_field_acceptance_expected_tool_paths_are_declared_not_inferred() -> None:
    expected_paths = {
        "field_exact_ro_2400611478": [("calibration_iq_ro",)],
        "field_current_calibrations_refreshes_exact_ro": [("calibration_iq_ro",)],
        "field_why_radar_calibration_needs_oem_evidence": [
            (
                "calibration_iq_ro",
                "automotive_knowledge_search",
                "adas_si_search",
            )
        ],
        "field_show_procedure_opens_source_document": [
            ("calibration_iq_ro", "adas_si_search", "adas_si_open")
        ],
        "field_follow_up_anything_else_refreshes_subject": [
            ("calibration_iq_ro",),
            ("calibration_iq_ro",),
        ],
        "field_close_it_out_requires_verified_receipt": [
            ("calibration_iq_ro", "calibration_iq_operator")
        ],
        "field_shop_work_waiting_in_macon": [("calibration_iq_read",)],
        "field_weekly_work_readiness": [("calibration_iq_work_prep",)],
        "field_nissan_alignment_technician_language": [
            (
                "calibration_iq_ro",
                "automotive_knowledge_search",
                "adas_si_search",
            )
        ],
        "field_ambiguous_procedure_uses_active_subject": [
            ("calibration_iq_ro", "adas_si_search", "adas_si_open")
        ],
        "field_explicit_context_switch_2400911667": [
            ("calibration_iq_ro",),
            ("calibration_iq_ro",),
        ],
        "field_multi_step_toyota_procedures_to_case": [
            ("calibration_iq_ro", "adas_si_search", "calibration_iq_operator")
        ],
        "negative_close_indeterminate_is_not_success": [
            ("calibration_iq_ro", "calibration_iq_operator")
        ],
    }
    scenarios = {scenario.name: scenario for scenario in FIELD_SCENARIOS}

    assert set(scenarios) == set(expected_paths)
    for name, paths in expected_paths.items():
        actual = [
            tuple(call.name for call in turn.calls) for turn in scenarios[name].turns
        ]
        assert actual == paths


def test_production_safe_live_alternatives_are_structurally_declared() -> None:
    scenarios = {scenario.name: scenario for scenario in SCENARIOS}
    expected = {
        ("count_paraphrase_unfinished_workload", 0): {
            ("calibration_iq_work_prep",)
        },
        ("capability_read_write_boundary", 0): {
            ("calibration_iq_status", "assistant_capabilities_read")
        },
        ("durable_subject_and_close_ro_mapping", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("explicit_new_resource_overrides_stale_subject", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("existing_evidence_escalates_without_inventing_answer", 0): {
            (
                "calibration_iq_ro",
                "adas_si_search",
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "calibration_iq_ro",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "automotive_knowledge_search",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
                "scrapex_read",
            ),
        },
        ("scrapex_authentication_boundary", 0): {
            ("scrapex_status", "scrapex_adas_map", "scrapex_adas_map"),
            ("scrapex_read", "scrapex_adas_map", "scrapex_adas_map"),
            (
                "scrapex_read",
                "scrapex_status",
                "scrapex_adas_map",
                "scrapex_adas_map",
            ),
            (
                "scrapex_status",
                "scrapex_read",
                "scrapex_adas_map",
                "scrapex_adas_map",
            ),
        },
        ("licensed_alldata_is_not_scrapex", 0): {
            ("research_provider_setup", "collision_research")
        },
        ("adas_si_supplies_answer_after_durable_miss", 0): {
            ("automotive_knowledge_search", "adas_si_search")
        },
        ("five_turn_subject_research_attach_close", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("five_turn_subject_research_attach_close", 2): {
            ("adas_si_search", "adas_si_open")
        },
        ("scrapex_acquisition_completes_with_provenance", 0): {
            ("scrapex_read", "scrapex_adas_map", "scrapex_adas_map"),
            (
                "scrapex_read",
                "scrapex_status",
                "scrapex_adas_map",
                "scrapex_adas_map",
            ),
        },
        ("field_exact_ro_2400611478", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("field_why_radar_calibration_needs_oem_evidence", 0): {("adas_si_search",)},
        ("field_show_procedure_opens_source_document", 0): {
            ("adas_si_search", "adas_si_open"),
            ("adas_si_search", "calibration_iq_ro", "adas_si_open"),
        },
        ("field_follow_up_anything_else_refreshes_subject", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("field_shop_work_waiting_in_macon", 0): {("calibration_iq_summary",)},
        ("field_nissan_alignment_technician_language", 0): {
            ("calibration_iq_work_prep",)
        },
        ("field_explicit_context_switch_2400911667", 0): {
            ("calibration_iq_read", "calibration_iq_ro")
        },
        ("field_multi_step_toyota_procedures_to_case", 0): {
            (
                "calibration_iq_ro",
                "adas_si_search",
                "adas_si_open",
                "calibration_iq_operator",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "calibration_iq_operator",
                "calibration_iq_ro",
                "calibration_iq_operator",
            ),
            (
                "calibration_iq_ro",
                "adas_si_search",
                "adas_si_open",
                "calibration_iq_operator",
                "calibration_iq_ro",
                "calibration_iq_operator",
            ),
        },
    }

    for (scenario_name, turn_index), required_paths in expected.items():
        actual_paths = {
            tuple(call.name for call in path)
            for path in scenarios[scenario_name].turns[turn_index].alternative_calls
        }
        assert required_paths <= actual_paths


def test_exact_ro_resolution_accepts_observed_id_or_displayed_number() -> None:
    ro_call = _toyota_ro_call()
    lookup, resolved = _lookup_then_exact_ro(
        ro_call,
        ro_id="ro-toyota-1478",
        ro_number="2400611478",
        evidence_id="lookup-test",
        vehicle="2024 Toyota Camry",
        shop="Perry",
    )

    lookup.check({"q": "2400611478"})
    observed_id = lookup.result["rows"][0]["id"]
    resolved.check({"repair_order_id": observed_id})
    resolved.check({"repair_order_id": "2400611478"})
    with pytest.raises(AssertionError):
        resolved.check({"repair_order_id": "2400611499"})


def test_explicit_new_resource_close_reread_accepts_exact_id_or_number_only() -> None:
    from core.tools.registry import (
        ToolBlocked,
        calibration_iq_evidence_from_result,
        validate_calibration_iq_write_binding,
    )

    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "explicit_new_resource_overrides_stale_subject"
    )
    reread, close = scenario.turns[1].calls
    reread.check({"repair_order_id": "ro-uuid-99"})
    reread.check({"repair_order_id": "2400999000"})
    with pytest.raises(AssertionError):
        reread.check({"repair_order_id": "2400911724"})

    close.check(
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "ro-uuid-99",
                    "expected_version": 4,
                }
            ]
        }
    )
    displayed_arguments = {
        "actions": [
            {
                "operation": "close_ro",
                "repair_order_id": "2400999000",
                "expected_version": 4,
                "arguments": {
                    "disposition": "completed",
                    "reason": "All required work is complete.",
                },
            }
        ]
    }
    close.check(displayed_arguments)

    evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        reread.result,
        conversation_id=71,
        message_id=2,
        source_tool_call_id="explicit-new-resource-refresh",
    )
    assert evidence is not None
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        displayed_arguments,
        evidence,
        conversation_id=71,
        message_id=2,
    )
    for invalid_arguments in (
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "2400911724",
                    "expected_version": 4,
                }
            ]
        },
        {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "2400999000",
                    "expected_version": 3,
                }
            ]
        },
    ):
        with pytest.raises(AssertionError):
            close.check(invalid_arguments)
        with pytest.raises(ToolBlocked):
            validate_calibration_iq_write_binding(
                "calibration_iq_operator",
                invalid_arguments,
                evidence,
                conversation_id=71,
                message_id=2,
            )


def test_perry_phase_list_is_a_bounded_read_only_aggregate_alternative() -> None:
    scenario = next(
        item for item in SCENARIOS if item.name == "count_paraphrase_unfinished_workload"
    )
    turn = scenario.turns[0]
    path = next(
        candidate
        for candidate in turn.alternative_calls
        if tuple(call.name for call in candidate) == ("calibration_iq_work_prep",)
    )
    call = path[0]
    arguments = {"mode": "phase_list", "phase": "5", "shop": "Perry"}
    call.check(arguments)
    result = call.result
    assert result["status"] == "verified"
    assert result["count"] == 7
    assert result["evidence"]["read_only"] is True
    assert result["collection_complete"] is True
    assert result["shown_count"] == len(result["rows"]) == 7
    assert result["result_scope"] == "board_list_only"
    assert "executed" not in result
    assert "receipts" not in result

    turn.report.check(
        {
            **turn.report.subset,
            "sources_checked": ["calibration_iq"],
            "observed_evidence_ids": [],
            "summary": "Seven unfinished Perry phase-five repair orders.",
        },
        call_path=("calibration_iq_work_prep",),
    )
    for invalid in (
        {**arguments, "phase": "6"},
        {**arguments, "shop": "Macon"},
        {**arguments, "repair_order_id": "2400611478"},
    ):
        with pytest.raises(AssertionError):
            call.check(invalid)


def test_close_paraphrase_exact_read_accepts_bound_id_or_displayed_number() -> None:
    scenario = next(
        item for item in SCENARIOS if item.name == "close_paraphrase_5"
    )
    reread = scenario.turns[0].calls[0]
    reread.check({"repair_order_id": "ro-uuid-1478"})
    reread.check({"repair_order_id": "2400611478"})
    with pytest.raises(AssertionError):
        reread.check({"repair_order_id": "2400611499"})


@pytest.mark.asyncio
async def test_alldata_vehicle_string_is_schema_and_handler_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema import validate

    from core.services import research_navigator_agent, research_operator

    arguments = {
        "action": "alldata_vehicle_research",
        "vehicle": "2023 Chevrolet Tahoe",
        "topic": "forward camera after windshield replacement",
    }
    tool = next(
        item
        for item in _production_profile_tools()
        if item["function"]["name"] == "collision_research"
    )
    validate(instance=arguments, schema=tool["function"]["parameters"])

    forwarded: dict[str, Any] = {}
    sentinel = object()

    async def capture_search(
        *,
        client: Any,
        provider: str,
        target: dict[str, Any],
        topic: str,
        capture: bool = False,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert client is sentinel
        assert provider == "alldata"
        assert capture is False
        forwarded.update(vehicle=target, topic=topic)
        return {"status": "invalid_vehicle", "verified": False, "captured": False}

    monkeypatch.setattr(research_navigator_agent, "current_model_client", lambda: sentinel)
    monkeypatch.setattr(
        research_navigator_agent,
        "run_navigator_search",
        capture_search,
    )
    browser = research_operator.LicensedBrowser(ROOT)
    await browser.operator_action(arguments)
    assert forwarded == {
        "vehicle": {
            "year": "2023",
            "make": "Chevrolet",
            "model": "Tahoe",
            "trim": None,
        },
        "topic": "forward camera after windshield replacement",
    }
    _alldata_research_call().check(arguments)
    with pytest.raises(AssertionError):
        _alldata_research_call().check(
            {**arguments, "vehicle": "2023 Chevrolet Suburban"}
        )
    with pytest.raises(AssertionError):
        _alldata_research_call().check({**arguments, "topic": ""})
    _alldata_research_call().check(
        {
            "action": "alldata_vehicle_research",
            "vehicle_year": 2023,
            "vehicle_make": "Chevrolet",
            "vehicle_model": "Tahoe",
            "topic": "forward camera after windshield replacement",
        }
    )


def test_field_adas_scope_uses_structured_identity_and_optional_search_mode() -> None:
    validator = _field_adas_scope(
        year=2023,
        make="Nissan",
        model="Rogue",
        repair_event="wheel alignment",
        component=("front radar", "radar", "ADAS"),
    )
    bounded = {
        "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
        "system": "ADAS",
        "requirement_type": "Calibration Requirement",
        "question": "What calibration procedure applies after this event?",
    }
    validator(bounded)
    radar_bounded = {**bounded, "component": "Radar"}
    _nissan_radar_evidence_call(
        event="wheel alignment",
        evidence_id="validator-nissan-radar",
        finding="Front radar aiming is required after wheel alignment.",
        page=18,
    ).check(radar_bounded)
    validator({**bounded, "repair_event": "Wheel Alignment"})
    validator({**bounded, "search_mode": "standard"})
    validator({**bounded, "search_mode": "calibration_requirements"})

    with pytest.raises(AssertionError):
        validator({**bounded, "repair_event": "windshield replacement"})
    with pytest.raises(AssertionError):
        validator({**bounded, "search_mode": "unsupported"})
    with pytest.raises(AssertionError):
        validator({**bounded, "question": ""})
    with pytest.raises(AssertionError):
        validator(
            {
                **bounded,
                "vehicle": {"year": 2024, "make": "Nissan", "model": "Rogue"},
            }
        )
    with pytest.raises(AssertionError):
        validator({**bounded, "system": ""})


def test_show_procedure_subject_has_authoritative_radar_context_only() -> None:
    from jsonschema import validate

    from core.tools.registry import TOOL_SCHEMAS

    scenario = next(
        item
        for item in FIELD_SCENARIOS
        if item.name == "field_show_procedure_opens_source_document"
    )
    subject = scenario.initial_subject
    assert subject == NISSAN_PROCEDURE_SUBJECT
    assert NISSAN_SUBJECT["payload"]["current_calibration_detail_included"] is False
    payload = subject["payload"]
    assert payload["current_calibration_detail_included"] is True
    calibrations = payload["working_context"]["sections"]["calibrations"]
    assert calibrations["source_owner"] == "calibration_iq"
    assert calibrations["authoritative"] is True
    assert calibrations["items"] == [
        {
            "id": "cal-radar-nissan-1",
            "label": "radar aiming",
            "status": "required",
        }
    ]
    assert subject["source_tool_name"] == "calibration_iq_ro"
    subject_message = LiveQwenHarness._active_subject_message(subject)
    assert subject_message is not None
    assert "detail_omitted" not in subject_message["content"]
    assert '"label":"radar aiming"' in subject_message["content"]

    direct = next(
        path
        for path in scenario.turns[0].alternative_calls
        if tuple(call.name for call in path) == ("adas_si_search", "adas_si_open")
    )
    radar_search = direct[0]
    assert radar_search.result["results"][0]["relative_path"] == (
        "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf"
    )
    assert radar_search.result["structured_query"]["repair_event"] == "calibration"
    assert direct[1].result["relative_path"] == (
        "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf"
    )
    radar_arguments = {
        "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
        "system": "ADAS",
        "component": "Radar",
        "repair_event": "Calibration",
        "requirement_type": "calibration",
        "question": "Show the required radar procedure after the collision.",
    }
    radar_search.check(radar_arguments)
    radar_search.check(
        {
            **{key: value for key, value in radar_arguments.items() if key != "component"},
            "system": "Radar",
            "repair_event": "Radar Aiming",
            "requirement_type": "procedure",
        }
    )

    front_bumper_arguments = {
        **radar_arguments,
        "repair_event": "Front Bumper Replacement",
        "requirement_type": "OEM procedure",
    }
    pre_read_paths = (
        direct,
        next(
            path
            for path in scenario.turns[0].alternative_calls
            if tuple(call.name for call in path)
            == ("adas_si_search", "calibration_iq_ro", "adas_si_open")
        ),
    )
    for path in pre_read_paths:
        with pytest.raises(AssertionError):
            path[0].check(front_bumper_arguments)

    refreshed = scenario.turns[0].calls
    assert tuple(call.name for call in refreshed) == (
        "calibration_iq_ro",
        "adas_si_search",
        "adas_si_open",
    )
    refreshed_search = refreshed[1]
    refreshed_search.check(front_bumper_arguments)
    assert refreshed_search.result["structured_query"]["repair_event"] == (
        "front bumper replacement"
    )
    assert refreshed_search.result["results"][0]["relative_path"] == (
        "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf"
    )

    camera_guess = {
        **radar_arguments,
        "component": "Camera",
        "repair_event": "Calibration",
        "question": "Show me the camera calibration procedure.",
    }
    validate(
        instance=camera_guess,
        schema=TOOL_SCHEMAS["adas_si_search"]["parameters"],
    )
    with pytest.raises(AssertionError):
        radar_search.check(camera_guess)
    with pytest.raises(AssertionError):
        radar_search.check({**radar_arguments, "component": "Camera"})
    with pytest.raises(AssertionError):
        radar_search.check(
            {
                key: value
                for key, value in radar_arguments.items()
                if key != "component"
            }
        )
    with pytest.raises(AssertionError):
        radar_search.check(
            {
                **radar_arguments,
                "vehicle": {"year": 2023, "make": "Nissan", "model": "Altima"},
            }
        )
    with pytest.raises(AssertionError):
        radar_search.check({**radar_arguments, "repair_event": "windshield replacement"})
    with pytest.raises(AssertionError):
        radar_search.check({**radar_arguments, "requirement_type": "inspection"})
    with pytest.raises(AssertionError):
        radar_search.check({**radar_arguments, "question": ""})


def test_show_procedure_report_uses_opened_document_as_decisive_evidence() -> None:
    scenario = next(
        item
        for item in FIELD_SCENARIOS
        if item.name == "field_show_procedure_opens_source_document"
    )
    turn = scenario.turns[0]
    paths = (turn.calls, *turn.alternative_calls)
    for path in paths:
        names = tuple(call.name for call in path)
        assert "adas_si_search" in names
        assert "adas_si_open" in names
        assert names.index("adas_si_search") < names.index("adas_si_open")
        search = path[names.index("adas_si_search")]
        opened = path[names.index("adas_si_open")]
        assert search.result["status"] == "verified"
        assert search.result["evidence_id"] == "adas-si-nissan-radar-procedure-p18"
        assert search.result["results"][0]["relative_path"] == (
            "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf"
        )
        assert opened.result["evidence_id"] == "adas-open-nissan-radar-p18"
        opened.check(
            {
                "relative_path": "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf",
                "page": 18,
            }
        )
        with pytest.raises(AssertionError):
            opened.check(
                {
                    "relative_path": "Nissan/Rogue/2023/ADAS/Camera Calibration.pdf",
                    "page": 18,
                }
            )

    report = {
        **turn.report.subset,
        "sources_checked": ["adas_si"],
        "observed_evidence_ids": ["adas-open-nissan-radar-p18"],
    }
    turn.report.check(report)
    for invalid_ids in ([], ["adas-si-nissan-radar-procedure-p18"]):
        with pytest.raises(AssertionError):
            turn.report.check({**report, "observed_evidence_ids": invalid_ids})


def test_adas_scope_accepts_exact_event_and_requirement_without_question() -> None:
    bounded = {
        "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
        "system": "Forward Camera",
        "repair_event": "Windshield Replacement",
        "requirement_type": "Calibration Requirement",
    }
    _structured_adas_search(bounded)
    _existing_adas_miss_call().check(bounded)

    for invalid in (
        {key: value for key, value in bounded.items() if key != "repair_event"},
        {key: value for key, value in bounded.items() if key != "requirement_type"},
        {**bounded, "question": ""},
        {
            **bounded,
            "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Suburban"},
        },
        {**bounded, "system": ""},
    ):
        with pytest.raises(AssertionError):
            _structured_adas_search(invalid)


def test_existing_evidence_allows_one_structurally_deeper_adas_retry_only() -> None:
    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "existing_evidence_escalates_without_inventing_answer"
    )
    turn = scenario.turns[0]
    call_path = (
        "calibration_iq_ro",
        "adas_si_search",
        "adas_si_search",
        "automotive_knowledge_search",
        "scrapex_read",
        "scrapex_read",
    )
    path = next(
        candidate
        for candidate in turn.alternative_calls
        if tuple(call.name for call in candidate) == call_path
    )
    standard, deep = path[1:3]
    structured_scope = {
        "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
        "system": "Forward Camera",
        "repair_event": "Windshield Replacement",
        "requirement_type": "Calibration Required",
    }
    standard.check(structured_scope)
    standard.check({**structured_scope, "search_mode": "standard"})
    deep_arguments = {
        **structured_scope,
        "question": "Is forward camera calibration required?",
        "search_mode": "calibration_requirements",
    }
    deep.check(deep_arguments)

    standard_result = standard.result_for(structured_scope)
    deep_result = deep.result_for(deep_arguments)
    assert standard_result["structured_query"]["search_mode"] == "standard"
    assert deep_result["structured_query"]["search_mode"] == (
        "calibration_requirements"
    )
    assert standard_result["evidence_id"] != deep_result["evidence_id"]

    # The first slot cannot be another unchanged deep attempt, and the second
    # slot cannot repeat the standard depth.
    with pytest.raises(AssertionError):
        standard.check(deep_arguments)
    with pytest.raises(AssertionError):
        deep.check({**structured_scope, "search_mode": "standard"})
    for changed_scope in (
        {**deep_arguments, "repair_event": "Collision Repair"},
        {**deep_arguments, "system": "Radar"},
        {**deep_arguments, "requirement_type": "Procedure"},
        {
            **deep_arguments,
            "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Suburban"},
        },
    ):
        with pytest.raises(AssertionError):
            deep.check(changed_scope)

    report = {
        **turn.report.subset,
        "sources_checked": ["calibration_iq", "adas_si", "durable_knowledge", "scrapex_adas_map"],
        "observed_evidence_ids": [
            "knowledge-miss-tahoe-1",
            "adas-si-miss-tahoe-1",
            "adas-si-deep-miss-tahoe-2",
            "batch-week-2026-08-25",
        ],
        "summary": "Both bounded ADAS SI depths and the remaining sources had no result.",
    }
    turn.report.check(report, call_path=call_path)
    report["observed_evidence_ids"].remove("adas-si-deep-miss-tahoe-2")
    with pytest.raises(AssertionError):
        turn.report.check(report, call_path=call_path)


def test_nissan_ro_requirements_alternative_is_verified_read_only_work_prep() -> None:
    scenario = next(
        item
        for item in FIELD_SCENARIOS
        if item.name == "field_nissan_alignment_technician_language"
    )
    turn = scenario.turns[0]
    path = next(
        candidate
        for candidate in turn.alternative_calls
        if tuple(call.name for call in candidate) == ("calibration_iq_work_prep",)
    )
    call = path[0]
    arguments = {
        "mode": "ro_requirements",
        "repair_order_id": "ro-nissan-1667",
    }
    call.check(arguments)
    result = call.result
    assert result["status"] == "success"
    assert result["verified"] is True
    assert result["snapshot_verified"] is True
    assert result["executed"] is False
    assert result["reconciliation_actions"] == []
    assert result["reconciliation"] is None
    assert "receipts" not in result
    assert result["adas_map"]["status"] == "verified"
    requirement = result["adas_map"]["requirements"][0]
    assert requirement["prerequisites"]
    assert requirement["actionable_before_alignment"]
    assert turn.report.subset["execution_state"] == "not_requested"
    assert "no_unreceipted_mutation_success" in scenario.negative_truths

    report = {
        **_report(
            "answered",
            found=True,
            used_subject=True,
            subject_id="ro-nissan-1667",
        ),
        "sources_checked": ["calibration_iq", "scrapex_adas_map"],
        "observed_evidence_ids": ["adas-map-nissan-alignment-verified-1"],
        "summary": "The verified read shows what can be staged before alignment.",
    }
    turn.report.check(report, call_path=("calibration_iq_work_prep",))
    with pytest.raises(AssertionError):
        turn.report.check(
            {**report, "execution_state": "verified"},
            call_path=("calibration_iq_work_prep",),
        )


def test_nissan_calibration_requirement_is_structural_not_question_classified() -> None:
    scenarios = {scenario.name: scenario for scenario in FIELD_SCENARIOS}
    alignment_turn = scenarios[
        "field_nissan_alignment_technician_language"
    ].turns[0]
    alignment_path = next(
        path
        for path in alignment_turn.alternative_calls
        if tuple(call.name for call in path)
        == ("calibration_iq_ro", "adas_si_search")
    )
    alignment = alignment_path[1]
    alignment_arguments = {
        "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
        "system": "front radar",
        "component": "radar",
        "repair_event": "front bumper replacement",
        "requirement_type": "calibration",
        "question": (
            "What are the prerequisites for front radar calibration after bumper "
            "replacement?"
        ),
    }
    alignment.check(alignment_arguments)
    alignment.check(
        {
            **alignment_arguments,
            "requirement_type": "OEM calibration guidance",
            "question": "Which OEM steps apply ahead of the aiming task?",
        }
    )
    alignment_result = alignment.result_for(alignment_arguments)
    assert alignment_result["structured_query"]["repair_event"] == (
        "front bumper replacement"
    )
    assert alignment_result["evidence_id"] == (
        "adas-si-nissan-alignment-prereq-p20"
    )

    ambiguous_turn = scenarios[
        "field_ambiguous_procedure_uses_active_subject"
    ].turns[0]
    ambiguous = ambiguous_turn.calls[1]
    procedure_arguments = {
        "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
        "system": "front radar",
        "component": "radar aiming",
        "repair_event": "front bumper replacement",
        "requirement_type": "calibration",
        "question": (
            "What is the required procedure for front radar calibration after bumper "
            "replacement?"
        ),
    }
    ambiguous.check(procedure_arguments)
    ambiguous.check(
        {
            **procedure_arguments,
            "requirement_type": "OEM service information",
            "question": "Please locate the exact OEM steps for this radar task.",
        }
    )
    procedure_result = ambiguous.result_for(procedure_arguments)
    assert procedure_result["structured_query"]["component"] == "radar aiming"
    assert procedure_result["evidence_id"] == (
        "adas-si-nissan-ambiguous-procedure-p18"
    )
    assert ambiguous_turn.calls[2].result["relative_path"] == (
        "Nissan/Rogue/2023/ADAS/Front Radar Aiming.pdf"
    )

    for expectation, arguments in (
        (alignment, alignment_arguments),
        (ambiguous, procedure_arguments),
    ):
        with pytest.raises(AssertionError):
            expectation.check({**arguments, "question": ""})
        with pytest.raises(AssertionError):
            expectation.check(
                {key: value for key, value in arguments.items() if key != "question"}
            )
        with pytest.raises(AssertionError):
            expectation.check({**arguments, "requirement_type": ""})
        with pytest.raises(AssertionError):
            expectation.check(
                {
                    key: value
                    for key, value in arguments.items()
                    if key != "requirement_type"
                }
            )
        with pytest.raises(AssertionError):
            expectation.check(
                {
                    **arguments,
                    "vehicle": {"year": 2023, "make": "Nissan", "model": "Altima"},
                }
            )
        with pytest.raises(AssertionError):
            expectation.check({**arguments, "component": "Camera"})
        with pytest.raises(AssertionError):
            expectation.check({**arguments, "system": "ADAS"})
        with pytest.raises(AssertionError):
            expectation.check({**arguments, "repair_event": "windshield replacement"})


def test_live_failure_classification_keeps_production_truth_failures_rejected() -> None:
    assert len(LIVE_FAILURE_CLASSIFICATION) == 10
    protected = {
        "capability_read_write_boundary",
        "durable_subject_and_close_ro_mapping",
        "field_weekly_work_readiness",
        "negative_close_indeterminate_is_not_success",
        "close_paraphrase",
    }
    assert all(
        LIVE_FAILURE_CLASSIFICATION[name].startswith("production_rejected")
        for name in protected
    )
    harness_only = set(LIVE_FAILURE_CLASSIFICATION) - protected
    assert all(
        LIVE_FAILURE_CLASSIFICATION[name].startswith("harness_")
        for name in harness_only
    )


def test_repository_wide_knowledge_scope_matches_production_schema_branch() -> None:
    repository_query = {
        "query": "verified service evidence",
        "system": "ADAS",
        "component": "Radar",
        "lifecycles": ["verified"],
        "limit": 10,
    }
    _existing_knowledge_miss_call().check(repository_query)
    _durable_knowledge_hit_call().check(repository_query)
    _nissan_knowledge_miss_call(
        event="front bumper replacement",
        evidence_id="knowledge-repository-test",
    ).check(repository_query)

    with pytest.raises(AssertionError):
        _existing_knowledge_miss_call().check(
            {**repository_query, "lifecycles": ["discovered"]}
        )
    with pytest.raises(AssertionError):
        _existing_knowledge_miss_call().check({"limit": 10})


def test_live_adas_shapes_preserve_exact_vehicle_and_declared_technical_scope() -> None:
    nissan = _nissan_radar_evidence_call(
        event=("front bumper replacement", "Collision repair"),
        evidence_id="adas-live-shape-nissan",
        finding="Radar aiming is required.",
        page=12,
    )
    nissan.check(
        {
            "vehicle": {"year": 2023, "make": "Nissan", "model": "Rogue"},
            "system": "ADAS",
            "component": "Radar",
            "repair_event": "Collision repair",
            "requirement_type": "Calibration",
            "question": "Why is radar aiming required?",
            "search_mode": "calibration_requirements",
        }
    )
    toyota = _toyota_procedure_search_call()
    toyota.check(
        {
            "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
            "system": "ADAS",
            "component": "Forward Camera",
            "repair_event": "Windshield Replacement",
            "requirement_type": "Calibration Procedure",
            "question": "Find the OEM forward-camera procedure.",
        }
    )
    with pytest.raises(AssertionError):
        nissan.check(
            {
                "vehicle": {"year": 2023, "make": "Nissan", "model": "Altima"},
                "component": "Radar",
            }
        )


def test_generic_adas_validator_accepts_production_shapes_and_handler_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema import validate

    from core.services.adas_si import AdasSI
    from core.tools.registry import TOOL_SCHEMAS

    existing_evidence_arguments = {
        "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
        "system": "Forward Camera",
        "repair_event": "Windshield Replacement",
        "requirement_type": "Calibration Requirement",
        "question": (
            "Is forward camera calibration required after windshield replacement?"
        ),
    }
    _existing_adas_miss_call().check(existing_evidence_arguments)

    arguments = {
        "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
        "system": "forward camera",
        "component": "forward recognition camera adjustment",
        "repair_event": "windshield replacement",
        "requirement_type": "calibration requirements",
        "question": (
            "Locate every applicable OEM procedure and page for this camera after the "
            "windshield work."
        ),
    }
    validate(instance=arguments, schema=TOOL_SCHEMAS["adas_si_search"]["parameters"])

    observed_search: dict[str, Any] = {}
    service = AdasSI(tmp_path / "adas-source", tmp_path / "adas-cache.sqlite3")

    def fixture_search(search: dict[str, Any]) -> dict[str, Any]:
        observed_search.update(search)
        return {"status": "verified", "results": []}

    monkeypatch.setattr(service, "search", fixture_search)
    result = service.model_search(arguments)

    expectation = _toyota_procedure_search_call()
    expectation.check(arguments)
    paraphrased = deepcopy(arguments)
    paraphrased["question"] = "Which source-backed procedure applies here?"
    expectation.check(paraphrased)
    assert observed_search["search_mode"] == "standard"
    fixture_result = expectation.result_for(arguments)
    assert result["structured_query"] == fixture_result["structured_query"]
    assert result["structured_query"]["component"] == (
        "forward recognition camera adjustment"
    )
    assert fixture_result["results"][0]["vehicle"] == {
        "year": 2024,
        "make": "Toyota",
        "model": "Camry",
    }

    wrong_vehicle = deepcopy(arguments)
    wrong_vehicle["vehicle"]["model"] = "Corolla"
    with pytest.raises(AssertionError):
        expectation.check(wrong_vehicle)


def test_adas_si_supplies_accepts_exact_vehicle_with_generic_structured_scope() -> None:
    from jsonschema import validate

    from core.tools.registry import TOOL_SCHEMAS

    arguments = {
        "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
        "system": "ADAS",
        "component": "forward-facing camera",
        "repair_event": "Windshield Replacement",
        "requirement_type": "Calibration Requirement",
        "question": "Which source-backed camera requirement applies to this repair?",
    }
    validate(instance=arguments, schema=TOOL_SCHEMAS["adas_si_search"]["parameters"])
    _tahoe_adas_hit_call().check(arguments)

    wrong_vehicle = deepcopy(arguments)
    wrong_vehicle["vehicle"]["model"] = "Suburban"
    with pytest.raises(AssertionError):
        _tahoe_adas_hit_call().check(wrong_vehicle)

    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "adas_si_supplies_answer_after_durable_miss"
    )
    report = {
        **_report(
            "answered",
            found=True,
            used_subject=True,
            subject_id="ro-uuid-17",
        ),
        "sources_checked": ["durable_knowledge", "adas_si"],
        "observed_evidence_ids": ["adas-si-hit-tahoe-camera-1"],
        "summary": "ADAS SI supplied the decisive verified result.",
    }
    scenario.turns[0].report.check(report)
    assert "knowledge-miss-before-adas-1" not in (
        scenario.turns[0].report.required_evidence_ids
    )


def test_existing_evidence_preview_is_safe_but_must_continue_to_exact_batch_item() -> (
    None
):
    from jsonschema import validate

    from core.orchestrator.loop import MAX_TOOL_ROUNDS, tool_result_visible_to_model
    from core.services.scrapex import SCRAPEX_READ_SCHEMA
    from core.tools.registry import (
        ToolBlocked,
        scrapex_evidence_from_result,
        validate_scrapex_batch_binding,
    )

    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "existing_evidence_escalates_without_inventing_answer"
    )
    preview_paths = [
        path
        for path in scenario.turns[0].alternative_calls
        if any(
            call.name == "scrapex_read"
            and call.subset.get("action") == "preview_ciq_queue"
            for call in path
        )
    ]
    assert len(preview_paths) == 8
    summary_paths = 0
    exception_paths = 0
    late_ro_paths = 0
    for path in preview_paths:
        preview_index = next(
            index
            for index, call in enumerate(path)
            if call.name == "scrapex_read"
            and call.subset.get("action") == "preview_ciq_queue"
        )
        post_preview = path[preview_index:]
        intervening_reads = [
            call for call in post_preview[1:] if call.name != "scrapex_read"
        ]
        assert all(call.name == "calibration_iq_ro" for call in intervening_reads)
        if intervening_reads:
            late_ro_paths += 1
            assert len(intervening_reads) == 1
            intervening_reads[0].check({"repair_order_id": "ro-uuid-17"})
        scrapex_tail = tuple(
            call for call in post_preview if call.name == "scrapex_read"
        )
        actions = [call.subset["action"] for call in scrapex_tail]
        assert actions[:2] == ["preview_ciq_queue", "list_batches"]
        assert actions[-1] == "batch_item"
        assert actions[2:-1] in (
            [],
            ["batch_summary"],
            ["batch_summary", "batch_exceptions"],
        )
        preview, listed = scrapex_tail[:2]
        item = scrapex_tail[-1]
        validate(
            instance=preview.subset,
            schema=SCRAPEX_READ_SCHEMA["parameters"],
        )
        assert preview.result == _existing_scrapex_preview_call().result
        assert "evidence_id" not in preview.result
        observed_batch_id = listed.result["data"]["batches"][0]["id"]
        if "batch_summary" in actions[2:-1]:
            summary_paths += 1
            summary = scrapex_tail[2]
            validate(
                instance=summary.subset,
                schema=SCRAPEX_READ_SCHEMA["parameters"],
            )
            assert summary.subset["batch_id"] == observed_batch_id
            assert summary.result["data"]["batch_id"] == observed_batch_id
        if "batch_exceptions" in actions[2:-1]:
            exception_paths += 1
            exceptions = scrapex_tail[3]
            validate(
                instance=exceptions.subset,
                schema=SCRAPEX_READ_SCHEMA["parameters"],
            )
            assert exceptions.subset["batch_id"] == observed_batch_id
            assert exceptions.result["data"]["batch_id"] == observed_batch_id
        assert item.subset["batch_id"] == observed_batch_id
        assert item.subset["ro_number"] == "2400911724"

    report = scenario.turns[0].report
    assert "batch-week-2026-08-25" in report.required_evidence_ids
    assert all(path[-1].subset.get("action") == "batch_item" for path in preview_paths)
    assert summary_paths == 4
    assert exception_paths == 2
    assert late_ro_paths == 1

    direct_path_names = (
        "adas_si_search",
        "automotive_knowledge_search",
        "scrapex_read",
        "scrapex_read",
        "scrapex_read",
    )
    direct_path = next(
        path
        for path in preview_paths
        if tuple(call.name for call in path) == direct_path_names
    )
    assert len(direct_path) == 5 <= MAX_TOOL_ROUNDS
    preview, listed, item = direct_path[-3:]
    assert preview.subset["action"] == "preview_ciq_queue"
    assert listed.subset == {"action": "list_batches"}
    assert item.subset["action"] == "batch_item"

    late_refresh_path_names = (
        "adas_si_search",
        "automotive_knowledge_search",
        "scrapex_read",
        "calibration_iq_ro",
        "scrapex_read",
        "scrapex_read",
    )
    late_refresh_path = next(
        path
        for path in preview_paths
        if tuple(call.name for call in path) == late_refresh_path_names
    )
    assert len(late_refresh_path) == 6 == MAX_TOOL_ROUNDS
    late_preview, late_ro, late_list, late_item = late_refresh_path[-4:]
    assert late_preview.subset["action"] == "preview_ciq_queue"
    assert "evidence_id" not in late_preview.result
    late_ro.check({"repair_order_id": "ro-uuid-17"})
    with pytest.raises(AssertionError):
        late_ro.check({"repair_order_id": "invented-ro-id"})
    late_batch_id = late_list.result["data"]["batches"][0]["id"]
    assert late_list.subset == {"action": "list_batches"}
    assert late_item.subset == {
        "action": "batch_item",
        "batch_id": late_batch_id,
        "ro_number": "2400911724",
    }
    assert (
        scrapex_evidence_from_result(
            late_preview.name,
            late_preview.subset,
            tool_result_visible_to_model(late_preview.name, late_preview.result),
            conversation_id=91,
            message_id=1,
            source_tool_call_id="late-preview",
        )
        is None
    )
    late_list_evidence = scrapex_evidence_from_result(
        late_list.name,
        late_list.subset,
        tool_result_visible_to_model(late_list.name, late_list.result),
        conversation_id=91,
        message_id=1,
        source_tool_call_id="late-list",
    )
    assert late_list_evidence is not None
    assert late_list_evidence.batch_ids == (late_batch_id,)
    validate_scrapex_batch_binding(
        late_item.name,
        late_item.subset,
        late_list_evidence,
        conversation_id=91,
        message_id=1,
    )
    late_report = {
        **scenario.turns[0].report.subset,
        "sources_checked": [
            "calibration_iq",
            "adas_si",
            "durable_knowledge",
            "scrapex_adas_map",
        ],
        "observed_evidence_ids": [
            "existing-chain-ciq-ro",
            "knowledge-miss-tahoe-1",
            "adas-si-miss-tahoe-1",
            late_batch_id,
        ],
    }
    scenario.turns[0].report.check(
        late_report,
        call_path=late_refresh_path_names,
    )
    assert late_report["outcome"] == "no_authoritative_answer"

    conversation_id = 91
    message_id = 1
    assert (
        scrapex_evidence_from_result(
            preview.name,
            preview.subset,
            tool_result_visible_to_model(preview.name, preview.result),
            conversation_id=conversation_id,
            message_id=message_id,
            source_tool_call_id="direct-preview",
        )
        is None
    )
    listed_evidence = scrapex_evidence_from_result(
        listed.name,
        listed.subset,
        tool_result_visible_to_model(listed.name, listed.result),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="direct-list",
    )
    assert listed_evidence is not None
    observed_batch_id = listed.result["data"]["batches"][0]["id"]
    assert listed_evidence.batch_ids == (observed_batch_id,)
    validate_scrapex_batch_binding(
        item.name,
        item.subset,
        listed_evidence,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    with pytest.raises(ToolBlocked, match="copied verbatim"):
        validate_scrapex_batch_binding(
            item.name,
            {**item.subset, "batch_id": "invented-batch-id"},
            listed_evidence,
            conversation_id=conversation_id,
            message_id=message_id,
        )


def test_scrapex_preview_is_safe_extra_read_but_never_completes_acquisition() -> None:
    from jsonschema import validate

    from core.services.scrapex import SCRAPEX_READ_SCHEMA

    scenarios = {scenario.name: scenario for scenario in SCENARIOS}
    for name in (
        "scrapex_authentication_boundary",
        "scrapex_acquisition_completes_with_provenance",
    ):
        turn = scenarios[name].turns[0]
        preview_paths = [
            path
            for path in turn.alternative_calls
            if any(
                call.name == "scrapex_read"
                and call.subset.get("action") == "preview_ciq_queue"
                for call in path
            )
        ]
        assert len(preview_paths) == (
            3 if name == "scrapex_authentication_boundary" else 2
        )
        for path in preview_paths:
            preview = next(
                call
                for call in path
                if call.name == "scrapex_read"
                and call.subset.get("action") == "preview_ciq_queue"
            )
            validate(
                instance=preview.subset,
                schema=SCRAPEX_READ_SCHEMA["parameters"],
            )
            assert preview.result["status"] == "verified"
            assert "evidence_id" not in preview.result
            assert [call.name for call in path[-2:]] == [
                "scrapex_adas_map",
                "scrapex_adas_map",
            ]
            create, process = path[-2:]
            assert create.subset["action"] == "create_exact_batch"
            process.check(
                {
                    "action": "process_one",
                    "batch_id": create.result["data"]["id"],
                    "ro_number": "2400911724",
                }
            )
            if process.result.get("status") == "completed":
                assert process.result["data"]["batch_id"] == create.result["data"]["id"]
            else:
                assert process.result["status"] == "authentication_required"
                assert process.result["executed"] is False
        assert turn.report.required_evidence_ids == frozenset(
            {preview_paths[0][-2].result["data"]["id"]}
        )


@pytest.mark.asyncio
async def test_scrapex_preview_omitted_scope_defaults_to_active_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.services import scrapex

    observed_body: dict[str, Any] = {}

    async def capture_request(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        observed_body.update(kwargs.get("body") or {})
        return {
            "count": 0,
            "phases": ["6"],
            "shop": "Perry",
            "source_scope": observed_body.get("source_scope"),
            "vehicles": [],
        }

    monkeypatch.setattr(scrapex, "_request", capture_request)
    # Keep the read hermetic: without this, a developer machine with a real
    # local ScrapeX checkout would trigger revision-aware native startup.
    monkeypatch.setattr(scrapex, "_project_revision", lambda _project: None)
    omitted_scope = {
        "action": "preview_ciq_queue",
        "phases": ["6"],
        "shop": "Perry",
    }
    expectation = _existing_scrapex_preview_call()
    expectation.check(omitted_scope)
    expectation.check({**omitted_scope, "source_scope": "active"})

    result = await scrapex.read(SimpleNamespace(), omitted_scope)
    assert observed_body == {
        "phases": ["6"],
        "shop": "Perry",
        "source_scope": "active",
    }
    assert result["service"] == "ScrapeX"
    assert result["action"] == "preview_ciq_queue"
    assert result["verified"] is True
    assert result["data"]["source_scope"] == "active"

    for invalid in (
        {**omitted_scope, "source_scope": "all"},
        {**omitted_scope, "phases": ["5"]},
        {key: value for key, value in omitted_scope.items() if key != "shop"},
    ):
        with pytest.raises(AssertionError):
            expectation.check(invalid)


@pytest.mark.asyncio
async def test_scrapex_preview_with_ro_number_cannot_replace_batch_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.services import scrapex

    monkeypatch.setattr(scrapex, "_project_revision", lambda _project: None)
    result = await scrapex.read(
        SimpleNamespace(),
        {
            "action": "preview_ciq_queue",
            "ro_number": "2400911724",
        },
    )
    assert result["status"] == "invalid_request"
    assert result["executed"] is False
    assert result["verified"] is False
    assert "unsupported argument" in result["error"]["message"].casefold()

    with pytest.raises(AssertionError):
        _existing_scrapex_list_call().check(
            {
                "action": "preview_ciq_queue",
                "ro_number": "2400911724",
            }
        )


def test_five_turn_fresh_durable_subject_can_search_then_open_without_ro_refresh() -> (
    None
):
    from jsonschema import validate

    from core.tools.registry import TOOL_SCHEMAS

    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "five_turn_subject_research_attach_close"
    )
    turn = scenario.turns[2]
    direct = next(
        path
        for path in turn.alternative_calls
        if tuple(call.name for call in path) == ("adas_si_search", "adas_si_open")
    )
    search, opened = direct
    search.check(
        {
            "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
            "system": "ADAS",
            "component": "forward camera",
            "repair_event": "Calibration",
            "requirement_type": "OEM procedure",
            "question": "Show the source-backed camera calibration procedure.",
        }
    )
    validate(
        instance=opened.subset,
        schema=TOOL_SCHEMAS["adas_si_open"]["parameters"],
    )
    assert opened.subset["relative_path"] == search.result["results"][0][
        "relative_path"
    ]
    assert opened.subset["page"] == search.result["results"][0]["page"]
    assert turn.report.required_evidence_ids == frozenset(
        {"five-turn-oem-procedure", "five-turn-oem-procedure-open"}
    )


def test_five_turn_research_attach_uses_receipt_truth_to_correct_raw_enum() -> None:
    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "five_turn_subject_research_attach_close"
    )
    turn = scenario.turns[3]
    operator = turn.calls[1]
    result = operator.result
    mutation_ids = {
        receipt["mutation_id"] for receipt in result.get("receipts") or []
    }
    assert mutation_ids == {
        "mut-five-turn-workspace-1",
        "mut-five-turn-document-import-1",
    }
    assert "evidence_id" not in result
    assert "research_reports" not in result
    assert turn.report.required_sources == frozenset({"calibration_iq"})
    assert turn.report.required_evidence_ids == frozenset(
        {"doc-five-turn-fcm-procedure-1"}
    )

    raw_report = {"outcome": "answered", "execution_state": "not_requested"}
    observation = {
        "name": operator.name,
        "arguments": {
            "actions": [
                {
                    "operation": "research_ro",
                    "repair_order_id": "ro-uuid-1478",
                }
            ]
        },
        "result": result,
    }
    normalized = _canonicalize_calibration_iq_report_execution(
        raw_report, [observation]
    )
    assert normalized["execution_state"] == "verified"
    assert raw_report["execution_state"] == "not_requested"

    unverified = deepcopy(observation)
    unverified["result"]["receipts"][0]["verification"]["verified"] = False
    assert (
        _canonicalize_calibration_iq_report_execution(raw_report, [unverified])
        is raw_report
    )


def test_explicit_displayed_ro_number_is_valid_direct_exact_read_shape() -> None:
    from jsonschema import validate

    from core.tools.registry import TOOL_SCHEMAS

    arguments = {"repair_order_id": "2400911667"}
    validate(
        instance=arguments,
        schema=TOOL_SCHEMAS["calibration_iq_ro"]["parameters"],
    )
    _nissan_ro_call(evidence_id="direct-number-test").check(arguments)
    assert "displayed RO number" in TOOL_SCHEMAS["calibration_iq_ro"][
        "parameters"
    ]["properties"]["repair_order_id"]["description"]

    with pytest.raises(AssertionError):
        _nissan_ro_call().check({"repair_order_id": "2400911777"})


def test_research_ro_is_exact_ro_bound_but_intentionally_unversioned() -> None:
    from jsonschema import validate

    from core.tools.registry import (
        ToolBlocked,
        calibration_iq_evidence_from_result,
        validate_calibration_iq_write_binding,
    )

    omitted_version = {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": "ro-toyota-1478",
            }
        ]
    }
    expectation = _toyota_research_attach_call()
    expectation.check(omitted_version)

    operator = next(
        item
        for item in _production_profile_tools()
        if item["function"]["name"] == "calibration_iq_operator"
    )
    validate(
        instance=omitted_version,
        schema=operator["function"]["parameters"],
    )

    exact_result = _toyota_ro_call().result
    evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        exact_result,
        conversation_id=71,
        message_id=3,
        source_tool_call_id="toyota-research-refresh",
    )
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        omitted_version,
        evidence,
        conversation_id=71,
        message_id=3,
    )

    exact_optional_version = deepcopy(omitted_version)
    exact_optional_version["actions"][0]["expected_version"] = 7
    expectation.check(exact_optional_version)
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        exact_optional_version,
        evidence,
        conversation_id=71,
        message_id=3,
    )

    stale_optional_version = deepcopy(omitted_version)
    stale_optional_version["actions"][0]["expected_version"] = 6
    with pytest.raises(ToolBlocked, match="optional version"):
        validate_calibration_iq_write_binding(
            "calibration_iq_operator",
            stale_optional_version,
            evidence,
            conversation_id=71,
            message_id=3,
        )


def test_mixed_research_calibration_rejects_zero_run_then_recovers_existing_child() -> (
    None
):
    from jsonschema import validate

    from core.orchestrator.loop import (
        MAX_TOOL_ROUNDS,
        calibration_iq_operator_terminal_summary,
    )
    from core.tools.registry import (
        TOOL_SCHEMAS,
        calibration_iq_evidence_from_result,
        validate_calibration_iq_write_binding,
    )

    mixed = _toyota_mixed_research_calibration_call()
    mixed_arguments = _toyota_mixed_live_arguments()
    mixed.check(mixed_arguments)
    validate(
        instance=mixed_arguments,
        schema=TOOL_SCHEMAS["calibration_iq_operator"]["parameters"],
    )
    research_action = next(
        action
        for action in mixed_arguments["actions"]
        if action["operation"] == "research_ro"
    )
    assert research_action["arguments"]["calibration_ids"] == [
        "cal-camera-toyota-1"
    ]
    assert research_action["arguments"]["destination_folder"] == "OEM Procedures"
    assert "documents" not in research_action["arguments"]
    first_evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        _toyota_ro_call().result,
        conversation_id=81,
        message_id=9,
        source_tool_call_id="toyota-mixed-initial-read",
    )
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        mixed_arguments,
        first_evidence,
        conversation_id=81,
        message_id=9,
    )
    assert mixed.result["status"] == "prerequisite_missing"
    assert mixed.result["executed"] is False
    assert mixed.result["error"]["details"]["repair_order_ids"] == [
        "ro-toyota-1478"
    ]
    for absent in (
        "requested_count",
        "processed_count",
        "receipts",
        "final_snapshots",
        "research",
        "evidence_id",
    ):
        assert absent not in mixed.result

    production_shape = deepcopy(mixed_arguments)
    add_action = next(
        action
        for action in production_shape["actions"]
        if action["operation"] == "add_calibration"
    )
    assert "system" not in add_action["arguments"]
    assert add_action["arguments"]["determination"] == "REQUIRED"
    assert add_action["arguments"]["method"] == "STATIC"
    mixed.check(production_shape)
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        production_shape,
        first_evidence,
        conversation_id=81,
        message_id=9,
    )

    reread = _toyota_mixed_recovery_ro_call()
    assert any(
        item.get("id") == "cal-camera-toyota-1"
        for item in reread.result["raw"]["calibrations"]
    )
    second_evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        reread.result,
        conversation_id=81,
        message_id=9,
        source_tool_call_id="toyota-mixed-recovery-reread",
    )
    research_arguments = {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": "ro-toyota-1478",
                "arguments": {"calibration_ids": ["cal-camera-toyota-1"]},
            }
        ]
    }
    recovery = _toyota_existing_calibration_research_call()
    recovery.check(research_arguments)
    validate(
        instance=research_arguments,
        schema=TOOL_SCHEMAS["calibration_iq_operator"]["parameters"],
    )
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        research_arguments,
        second_evidence,
        conversation_id=81,
        message_id=9,
    )
    assert "evidence_id" not in recovery.result
    assert {receipt["operation"] for receipt in recovery.result["receipts"]} == {
        "ensure_case_workspace",
        "import_document",
    }
    assert {
        receipt["mutation_id"] for receipt in recovery.result["receipts"]
    } == {
        "mut-toyota-workspace-1",
        "mut-toyota-document-import-1",
    }
    document = recovery.result["final_snapshots"]["ro-toyota-1478"]["snapshot"][
        "documents"
    ][0]
    assert document["id"] == "doc-toyota-camera-oem-1"
    assert document["calibration_item_ids"] == ["cal-camera-toyota-1"]
    assert recovery.result["research"][0]["missing_documents"] == []

    scenario = next(
        item
        for item in FIELD_SCENARIOS
        if item.name == "field_multi_step_toyota_procedures_to_case"
    )
    assert scenario.turns[0].report.required_evidence_ids == frozenset(
        {
            "ciq-toyota-multistep-current-7",
            "adas-si-toyota-camera-p34",
            "mut-toyota-workspace-1",
            "mut-toyota-document-import-1",
        }
    )
    assert (
        "doc-toyota-camera-oem-1"
        not in scenario.turns[0].report.required_evidence_ids
    )
    recovery_path = next(
        path
        for path in scenario.turns[0].alternative_calls
        if tuple(call.name for call in path)
        == (
            "calibration_iq_ro",
            "adas_si_search",
            "calibration_iq_operator",
            "calibration_iq_ro",
            "calibration_iq_operator",
        )
    )
    assert len(recovery_path) <= MAX_TOOL_ROUNDS
    assert "no_hidden_zero_run_mixed_batch" in scenario.negative_truths

    observations = [
        {
            "name": "calibration_iq_operator",
            "arguments": mixed_arguments,
            "result": mixed.result,
        },
        {
            "name": "calibration_iq_operator",
            "arguments": research_arguments,
            "result": recovery.result,
        },
    ]
    assert _mixed_zero_run_research_recovery(observations) is True
    assert "only partially verified" in calibration_iq_operator_terminal_summary(
        [mixed.result, recovery.result]
    ).casefold()
    canonical = _canonicalize_calibration_iq_report_execution(
        {"execution_state": "verified"}, observations
    )
    assert canonical["execution_state"] == "not_confirmed"


def test_destructive_child_target_does_not_require_optional_ro_context() -> None:
    expectation = _destructive_delete_call()
    action = {
        "operation": "delete_blocker",
        "target_id": "blk-9",
        "expected_version": 12,
    }
    expectation.check({"actions": [action]})
    with pytest.raises(AssertionError):
        expectation.check({"actions": [{**action, "repair_order_id": "different-ro"}]})


def test_change_status_is_close_equivalent_only_with_identity_version_and_receipt() -> (
    None
):
    expectation = _change_status_complete_call()
    arguments = {
        "actions": [
            {
                "operation": "change_status",
                "repair_order_id": "ro-uuid-17",
                "expected_version": 12,
                "arguments": {"status": "complete"},
            }
        ]
    }
    expectation.check(arguments)
    with pytest.raises(AssertionError):
        expectation.check(
            {
                "actions": [
                    {
                        "operation": "change_status",
                        "arguments": {"status": "complete"},
                    }
                ]
            }
        )
    observation = {
        "name": "calibration_iq_operator",
        "arguments": arguments,
        "result": expectation.result,
    }
    assert (
        _verified_close_without_child_calibration_state([observation])
        is expectation.result
    )


def test_scrapex_process_copies_data_id_not_synthetic_evidence() -> None:
    for create, process in (
        (_auth_scrapex_create_call(), _auth_scrapex_process_call()),
        (_acquisition_create_call(), _acquisition_process_call()),
    ):
        assert "evidence_id" not in create.result
        batch_id = create.result["data"]["id"]
        process.check(
            {
                "action": "process_one",
                "batch_id": batch_id,
                "ro_number": "2400911724",
            }
        )
        with pytest.raises(AssertionError):
            process.check(
                {
                    "action": "process_one",
                    "batch_id": "receipt-or-call-id-is-not-a-batch",
                    "ro_number": "2400911724",
                }
            )


def test_refresh_before_close_preserves_exact_version_dependency() -> None:
    scenarios = {scenario.name: scenario for scenario in SCENARIOS}
    names = {
        "field_close_it_out_requires_verified_receipt",
        "negative_close_indeterminate_is_not_success",
        *(f"close_paraphrase_{index}" for index in range(1, 7)),
    }
    for name in names:
        refresh, close = scenarios[name].turns[0].calls
        snapshot = refresh.result["raw"]["repair_order"]
        action = close.subset["actions"][0]
        assert action["repair_order_id"] == snapshot["id"]
        assert action["expected_version"] == snapshot["version"]


def test_shop_summary_alternative_is_aggregate_only() -> None:
    result = _macon_current_summary_call().result
    assert result["count"] == 2
    assert result["result_scope"] == "aggregate_only"
    assert result["repair_order_rows_included"] is False
    assert "rows" not in result


def test_scrapex_report_enum_is_canonicalized_only_from_terminal_contract() -> None:
    base = {
        "outcome": "answered",
        "execution_state": "not_requested",
    }
    success = {
        "name": "scrapex_adas_map",
        "arguments": {
            "action": "process_one",
            "batch_id": "batch-exact-2",
            "ro_number": "2400911724",
        },
        "result": _acquisition_process_call().result,
    }
    assert (
        _canonicalize_scrapex_report_execution(base, [success])["execution_state"]
        == "verified"
    )

    blocked = {
        "name": "scrapex_adas_map",
        "arguments": {
            "action": "process_one",
            "batch_id": "batch-auth-boundary-1",
            "ro_number": "2400911724",
        },
        "result": _auth_scrapex_process_call().result,
    }
    assert (
        _canonicalize_scrapex_report_execution(base, [blocked])["execution_state"]
        == "not_confirmed"
    )

    malformed = deepcopy(success)
    malformed["result"]["data"]["batch_id"] = "different-batch"
    assert _canonicalize_scrapex_report_execution(base, [malformed]) is base


def test_ciq_report_enum_is_canonicalized_only_from_terminal_receipt_truth() -> None:
    base = {"outcome": "answered", "execution_state": "not_requested"}
    observation = {
        "name": "calibration_iq_operator",
        "arguments": {
            "actions": [
                {
                    "operation": "research_ro",
                    "repair_order_id": "ro-toyota-1478",
                }
            ]
        },
        "result": _toyota_research_attach_call().result,
    }

    normalized = _canonicalize_calibration_iq_report_execution(base, [observation])
    assert normalized["execution_state"] == "verified"
    assert normalized is not base
    assert base["execution_state"] == "not_requested"


def test_ciq_report_enum_never_promotes_partial_indeterminate_or_unverified() -> None:
    base = {"outcome": "answered", "execution_state": "not_requested"}
    arguments = {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": "ro-toyota-1478",
            }
        ]
    }
    verified = _toyota_research_attach_call().result

    partial = deepcopy(verified)
    partial.update(
        status="partial_success",
        success=False,
        verified=False,
        partial=True,
    )
    indeterminate = deepcopy(verified)
    indeterminate.update(
        status="indeterminate",
        success=False,
        verified=False,
        partial=True,
    )
    unverified_receipt = deepcopy(verified)
    unverified_receipt["receipts"][0]["verification"]["verified"] = False
    missing_snapshot = deepcopy(verified)
    missing_snapshot["final_snapshots"] = {}
    mismatched_snapshot = deepcopy(verified)
    final = mismatched_snapshot["final_snapshots"].pop("ro-toyota-1478")
    mismatched_snapshot["final_snapshots"]["different-ro"] = final

    for result in (
        partial,
        indeterminate,
        unverified_receipt,
        missing_snapshot,
        mismatched_snapshot,
    ):
        observation = {
            "name": "calibration_iq_operator",
            "arguments": arguments,
            "result": result,
        }
        assert (
            _canonicalize_calibration_iq_report_execution(base, [observation]) is base
        )


def test_indeterminate_report_answer_boolean_canonicalizes_only_strict_truth() -> None:
    raw_report = {
        "outcome": "indeterminate",
        "authoritative_answer_found": True,
        "execution_state": "not_confirmed",
        "used_active_subject": True,
        "subject_resource_id": "ro-toyota-1478",
        "requested_credentials": False,
        "sources_checked": ["calibration_iq"],
        "observed_evidence_ids": ["ciq-close-indeterminate-7"],
        "summary": "The close may have executed, but it was not verified.",
    }
    observation = {
        "name": "calibration_iq_operator",
        "arguments": _toyota_indeterminate_close_call().subset,
        "result": _toyota_indeterminate_close_call().result,
    }
    normalized = _canonicalize_indeterminate_report_answer_truth(
        raw_report,
        [observation],
    )
    assert normalized["authoritative_answer_found"] is False
    assert normalized["outcome"] == "indeterminate"
    assert normalized["execution_state"] == "not_confirmed"
    assert raw_report["authoritative_answer_found"] is True

    scenario = next(
        item
        for item in SCENARIOS
        if item.name == "negative_close_indeterminate_is_not_success"
    )
    scenario.turns[0].report.check(
        normalized,
        call_path=("calibration_iq_ro", "calibration_iq_operator"),
    )

    for noncanonical_report, noncanonical_result in (
        ({**raw_report, "outcome": "answered"}, observation["result"]),
        ({**raw_report, "execution_state": "verified"}, observation["result"]),
        (
            raw_report,
            {
                **observation["result"],
                "status": "partial_success",
                "may_have_executed": False,
            },
        ),
        (
            raw_report,
            {
                **observation["result"],
                "final_snapshots": {"ro-toyota-1478": {"status": "verified"}},
            },
        ),
    ):
        candidate_observation = {**observation, "result": noncanonical_result}
        assert (
            _canonicalize_indeterminate_report_answer_truth(
                noncanonical_report,
                [candidate_observation],
            )
            is noncanonical_report
        )


def test_active_subject_usage_canonicalizes_only_matching_exact_resource() -> None:
    raw_report = {
        "used_active_subject": False,
        "subject_resource_id": "ro-nissan-1667",
    }
    matching = {
        "name": "calibration_iq_ro",
        "arguments": {"repair_order_id": "2400911667"},
        "result": _nissan_ro_call(evidence_id="subject-usage-match").result,
    }
    normalized = _canonicalize_active_subject_usage(
        raw_report,
        [matching],
        NISSAN_SUBJECT,
    )
    assert normalized["used_active_subject"] is True
    assert normalized is not raw_report
    assert raw_report["used_active_subject"] is False

    different = {
        "name": "calibration_iq_ro",
        "arguments": {"repair_order_id": "ro-toyota-1478"},
        "result": _toyota_ro_call(evidence_id="subject-usage-different").result,
    }
    assert (
        _canonicalize_active_subject_usage(
            raw_report,
            [different],
            NISSAN_SUBJECT,
        )
        is raw_report
    )
    untrusted = deepcopy(NISSAN_SUBJECT)
    untrusted["payload"]["resource_id"] = ""
    untrusted["payload"]["repair_order_id"] = ""
    untrusted["payload"]["ro_number"] = ""
    untrusted["payload"]["repair_order"] = {}
    assert (
        _canonicalize_active_subject_usage(raw_report, [matching], untrusted)
        is raw_report
    )


def test_field_acceptance_fixtures_never_hold_live_handlers() -> None:
    advertised = {item["function"]["name"] for item in _production_profile_tools()}
    for scenario in FIELD_SCENARIOS:
        for turn in scenario.turns:
            for path in (turn.calls, *turn.alternative_calls):
                assert path
                for expectation in path:
                    assert isinstance(expectation.result, dict)
                    assert expectation.name != "acceptance_report"
                    assert expectation.name in advertised


def test_live_harness_advertises_exact_production_adas_profile() -> None:
    production = _production_profile_tools()
    harness = LiveQwenHarness(WorkerTarget("http://fixture.invalid/v1", "fixture"))

    assert harness.business_tools == production
    assert [item["function"]["name"] for item in harness.report_tools] == [
        "acceptance_report"
    ]


def test_staged_catalog_unlocks_only_from_context_bound_exact_ro_evidence() -> None:
    from core.tools.registry import (
        CALIBRATION_IQ_STAGED_WRITE_TOOLS,
        calibration_iq_evidence_from_result,
    )

    harness = LiveQwenHarness(WorkerTarget("http://fixture.invalid/v1", "fixture"))
    initial_names = {
        item["function"]["name"] for item in harness._business_tools_for_evidence(None)
    }
    assert CALIBRATION_IQ_STAGED_WRITE_TOOLS.isdisjoint(initial_names)

    evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        _existing_ciq_ro_call().result,
        conversation_id=41,
        message_id=7,
        source_tool_call_id="fixture-exact-ro",
    )
    assert evidence is not None
    assert evidence.conversation_id == 41
    assert evidence.message_id == 7
    assert evidence.source_tool_call_ids == ("fixture-exact-ro",)
    unlocked_names = {
        item["function"]["name"]
        for item in harness._business_tools_for_evidence(evidence)
    }
    assert CALIBRATION_IQ_STAGED_WRITE_TOOLS <= unlocked_names

    inconsistent = deepcopy(_existing_ciq_ro_call().result)
    inconsistent["raw"]["repair_order"]["id"] = "different-ro"
    assert (
        calibration_iq_evidence_from_result(
            "calibration_iq_ro",
            inconsistent,
            conversation_id=41,
            message_id=7,
            source_tool_call_id="fixture-inconsistent-ro",
        )
        is None
    )
    assert (
        calibration_iq_evidence_from_result(
            "calibration_iq_read",
            _existing_ciq_ro_call().result,
            conversation_id=41,
            message_id=7,
            source_tool_call_id="fixture-wrong-tool",
        )
        is None
    )


def test_staged_binding_requires_exact_identity_version_target_and_turn() -> None:
    from core.tools.registry import (
        ToolBlocked,
        calibration_iq_evidence_from_result,
        validate_calibration_iq_write_binding,
    )

    close_evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        _existing_ciq_ro_call().result,
        conversation_id=51,
        message_id=9,
        source_tool_call_id="fixture-close-refresh",
    )
    close_arguments = {
        "actions": [
            {
                "operation": "close_ro",
                "repair_order_id": "ro-uuid-17",
                "expected_version": 12,
            }
        ]
    }
    validate_calibration_iq_write_binding(
        "calibration_iq_operator",
        close_arguments,
        close_evidence,
        conversation_id=51,
        message_id=9,
    )
    with pytest.raises(ToolBlocked, match="expected_version"):
        validate_calibration_iq_write_binding(
            "calibration_iq_operator",
            {
                "actions": [
                    {
                        **close_arguments["actions"][0],
                        "expected_version": 11,
                    }
                ]
            },
            close_evidence,
            conversation_id=51,
            message_id=9,
        )
    with pytest.raises(ToolBlocked, match="different conversation turn"):
        validate_calibration_iq_write_binding(
            "calibration_iq_operator",
            close_arguments,
            close_evidence,
            conversation_id=51,
            message_id=10,
        )

    destructive_evidence = calibration_iq_evidence_from_result(
        "calibration_iq_ro",
        _destructive_refresh_call().result,
        conversation_id=51,
        message_id=10,
        source_tool_call_id="fixture-destructive-refresh",
    )
    destructive_arguments = {
        "actions": [
            {
                "operation": "delete_blocker",
                "target_id": "blk-9",
                "expected_version": 12,
            }
        ]
    }
    validate_calibration_iq_write_binding(
        "calibration_iq_destructive",
        destructive_arguments,
        destructive_evidence,
        conversation_id=51,
        message_id=10,
    )
    with pytest.raises(ToolBlocked, match="target/version"):
        validate_calibration_iq_write_binding(
            "calibration_iq_destructive",
            {
                "actions": [
                    {
                        **destructive_arguments["actions"][0],
                        "target_id": "invented-blocker",
                    }
                ]
            },
            destructive_evidence,
            conversation_id=51,
            message_id=10,
        )


def test_every_staged_mutation_path_has_authoritative_exact_ro_before_write() -> None:
    from core.tools.registry import (
        CALIBRATION_IQ_STAGED_WRITE_TOOLS,
        calibration_iq_evidence_from_result,
    )

    staged_paths = 0
    for scenario in SCENARIOS:
        for turn in scenario.turns:
            for path in (turn.calls, *turn.alternative_calls):
                for index, expectation in enumerate(path):
                    if expectation.name not in CALIBRATION_IQ_STAGED_WRITE_TOOLS:
                        continue
                    staged_paths += 1
                    exact_reads = [
                        item
                        for item in path[:index]
                        if item.name == "calibration_iq_ro"
                    ]
                    assert exact_reads, (scenario.name, [item.name for item in path])
                    evidence = calibration_iq_evidence_from_result(
                        "calibration_iq_ro",
                        exact_reads[-1].result,
                        conversation_id=61,
                        message_id=staged_paths,
                        source_tool_call_id=f"fixture-read-{staged_paths}",
                    )
                    assert evidence is not None, scenario.name

                    receipt_operations = {
                        receipt.get("operation")
                        for receipt in expectation.result.get("receipts", [])
                        if isinstance(receipt, dict)
                    }
                    closes_or_deletes = (
                        expectation.name == "calibration_iq_destructive"
                        or bool({"close_ro", "change_status"} & receipt_operations)
                    )
                    if closes_or_deletes:
                        assert path[index - 1].name == "calibration_iq_ro", (
                            scenario.name,
                            [item.name for item in path],
                        )
    assert staged_paths == 21


def test_unadvertised_initial_write_never_receives_a_fixture_result() -> None:
    completion_calls: list[dict[str, Any]] = []

    class ScriptedUnadvertisedWriteHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            completion_calls.append(
                {
                    "messages": deepcopy(messages),
                    "tool_names": [item["function"]["name"] for item in tools],
                    "force_tool": force_tool,
                    "tool_choice": tool_choice,
                }
            )
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "unadvertised-close",
                        "type": "function",
                        "function": {
                            "name": "calibration_iq_operator",
                            "arguments": json.dumps(_close_paraphrase_call(0).subset),
                        },
                    }
                ],
            }

    turn = Turn(
        "Close the current repair order.",
        (_close_paraphrase_call(0),),
        ReportExpectation(_report("answered", found=True, execution="verified")),
    )
    harness = ScriptedUnadvertisedWriteHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    with pytest.raises(ModelProtocolError, match="unadvertised staged tool"):
        harness.run_turn(turn, subject=SUBJECT, prior_dialogue=[])

    assert len(completion_calls) == 1
    assert "calibration_iq_operator" not in completion_calls[0]["tool_names"]
    assert not any(
        message.get("role") == "tool" for message in completion_calls[0]["messages"]
    )


def test_blocked_staged_attempt_uses_production_result_consumes_and_relocks() -> None:
    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    exact_read = _existing_ciq_ro_call()
    stale_arguments = {
        "actions": [
            {
                "operation": "close_ro",
                "repair_order_id": "ro-uuid-17",
                "expected_version": 11,
            }
        ]
    }
    blocked_write = CallExpectation(
        "calibration_iq_operator",
        {
            "status": "blocked",
            "fixture_sentinel": "must-never-be-fed-to-the-model",
        },
        stale_arguments,
    )
    note_arguments = {
        "actions": [
            {
                "operation": "add_note",
                "repair_order_id": "ro-uuid-17",
                "arguments": {"body": "Verified after a fresh exact reread."},
            }
        ]
    }
    verified_note_result = _operator_result(
        operation="add_note",
        evidence_id="fixture-note-after-refresh",
    )
    verified_note_result["receipts"][0].update(
        mutation_id="mut-fixture-note-after-refresh",
        resource_type="note",
        resource_id="note-fixture-after-refresh",
    )
    verified_note_result["final_snapshots"]["ro-uuid-17"]["snapshot"]["notes"] = [
        {
            "id": "note-fixture-after-refresh",
            "text": "Verified after a fresh exact reread.",
        }
    ]
    verified_note = CallExpectation(
        "calibration_iq_operator",
        verified_note_result,
        note_arguments,
    )
    report = {
        **_report(
            "answered",
            found=True,
            execution="verified",
            used_subject=True,
            subject_id="ro-uuid-17",
        ),
        "sources_checked": ["calibration_iq_ro", "calibration_iq_operator"],
        "observed_evidence_ids": [
            "existing-chain-ciq-ro",
            "fixture-note-after-refresh",
        ],
        "summary": "The exact reread re-enabled one verified note write.",
    }
    responses = iter(
        [
            tool_call(
                "exact-read-one",
                "calibration_iq_ro",
                {"repair_order_id": "ro-uuid-17"},
            ),
            tool_call(
                "stale-write",
                "calibration_iq_operator",
                stale_arguments,
            ),
            tool_call(
                "exact-read-two",
                "calibration_iq_ro",
                {"repair_order_id": "ro-uuid-17"},
            ),
            tool_call(
                "verified-write",
                "calibration_iq_operator",
                note_arguments,
            ),
            tool_call("terminal-report", "acceptance_report", report),
            {"content": "The stale write ran nothing; the later note alone was verified."},
        ]
    )
    completion_calls: list[dict[str, Any]] = []

    class ScriptedRelockHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            completion_calls.append(
                {
                    "messages": deepcopy(messages),
                    "tool_names": [item["function"]["name"] for item in tools],
                    "force_tool": force_tool,
                    "tool_choice": tool_choice,
                }
            )
            return next(responses)

    turn = Turn(
        "Refresh the exact RO, reject stale binding, then add a note after a new exact read.",
        (exact_read, blocked_write, exact_read, verified_note),
        ReportExpectation(
            _report(
                "answered",
                found=True,
                execution="verified",
                used_subject=True,
                subject_id="ro-uuid-17",
            ),
            frozenset({"calibration_iq_ro", "calibration_iq_operator"}),
            frozenset({"existing-chain-ciq-ro", "fixture-note-after-refresh"}),
        ),
    )
    harness = ScriptedRelockHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    result, _, _ = harness.run_turn(turn, subject=SUBJECT, prior_dialogue=[])

    assert "calibration_iq_operator" not in completion_calls[0]["tool_names"]
    assert "calibration_iq_operator" in completion_calls[1]["tool_names"]
    assert "calibration_iq_operator" not in completion_calls[2]["tool_names"]
    assert "calibration_iq_operator" in completion_calls[3]["tool_names"]
    operator_results = [
        json.loads(message["content"])
        for message in completion_calls[4]["messages"]
        if message.get("role") == "tool"
        and message.get("name") == "calibration_iq_operator"
    ]
    assert operator_results[0]["status"] == "blocked"
    assert "nothing was run" in operator_results[0]["message"].casefold()
    assert "fixture_sentinel" not in operator_results[0]
    assert operator_results[1]["status"] == "success"
    assert result.report["execution_state"] == "not_confirmed"


def test_live_harness_uses_production_no_tool_self_check_and_discards_draft() -> None:
    from core.orchestrator.loop import NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE

    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    unsupported_draft = "RO 2400911667 is ready now."
    report = {
        **_report(
            "answered",
            found=True,
            used_subject=False,
            subject_id="ro-nissan-1667",
        ),
        "sources_checked": ["calibration_iq"],
        "observed_evidence_ids": ["ciq-self-check-nissan-21"],
        "summary": "The exact current RO detail supplied the answer.",
    }
    responses = iter(
        [
            {"content": unsupported_draft, "tool_calls": []},
            tool_call(
                "self-check-exact-ro",
                "calibration_iq_ro",
                {"repair_order_id": "2400911667"},
            ),
            tool_call("self-check-report", "acceptance_report", report),
            {"content": "I pulled the current Nissan RO detail.", "tool_calls": []},
        ]
    )
    completion_calls: list[dict[str, Any]] = []

    class ScriptedSelfCheckHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            completion_calls.append(
                {
                    "messages": deepcopy(messages),
                    "tool_names": [item["function"]["name"] for item in tools],
                    "force_tool": force_tool,
                    "tool_choice": tool_choice,
                }
            )
            return next(responses)

    exact = _nissan_ro_call(evidence_id="ciq-self-check-nissan-21")
    turn = Turn(
        "Pull up RO 2400911667 and tell me its current state.",
        (exact,),
        ReportExpectation(
            _report(
                "answered",
                found=True,
                used_subject=False,
                subject_id="ro-nissan-1667",
            ),
            frozenset({"calibration_iq"}),
            frozenset({"ciq-self-check-nissan-21"}),
        ),
    )
    harness = ScriptedSelfCheckHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    result, _, _ = harness.run_turn(turn, subject=SUBJECT, prior_dialogue=[])

    assert completion_calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": unsupported_draft,
    }
    assert completion_calls[1]["messages"][-1] == {
        "role": "user",
        "content": NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE,
    }
    assert completion_calls[1]["tool_names"] == completion_calls[0]["tool_names"]
    assert completion_calls[1]["tool_choice"] == "required"
    persisted_contents = [
        message.get("content") for message in completion_calls[2]["messages"]
    ]
    assert unsupported_draft not in persisted_contents
    assert NO_TOOL_SELF_CHECK_REQUIRED_MESSAGE not in persisted_contents
    assert [call["name"] for call in result.calls] == [
        "calibration_iq_ro",
        "acceptance_report",
    ]


def test_harness_retains_raw_report_when_ciq_terminal_truth_corrects_enum() -> None:
    def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }
            ],
        }

    adas_arguments = {
        "vehicle": {"year": 2024, "make": "Toyota", "model": "Camry"},
        "system": "forward camera",
        "component": "forward recognition camera adjustment",
        "repair_event": "windshield replacement",
        "requirement_type": "calibration requirements",
        "question": "Which OEM procedures and pages apply to this repair?",
    }
    operator_arguments = {
        "actions": [
            {
                "operation": "research_ro",
                "repair_order_id": "ro-toyota-1478",
            }
        ]
    }
    raw_report = {
        **_report(
            "answered",
            found=True,
            execution="not_requested",
            used_subject=True,
            subject_id="ro-toyota-1478",
        ),
        "sources_checked": ["calibration_iq", "adas_si"],
        "observed_evidence_ids": [
            "ciq-toyota-multistep-current-7",
            "adas-si-toyota-camera-p34",
            "mut-toyota-workspace-1",
            "mut-toyota-document-import-1",
        ],
        "summary": "The verified operator receipt and final snapshot prove the update.",
    }
    responses = iter(
        [
            tool_call(
                "toyota-exact-ro",
                "calibration_iq_ro",
                {"repair_order_id": "ro-toyota-1478"},
            ),
            tool_call("toyota-adas", "adas_si_search", adas_arguments),
            tool_call("toyota-research", "calibration_iq_operator", operator_arguments),
            tool_call("toyota-report", "acceptance_report", raw_report),
            {"content": "The OEM evidence was verified and added to the case."},
        ]
    )

    class ScriptedCiqReportHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del messages, tools, force_tool, tool_choice
            return next(responses)

    scenario = next(
        item
        for item in FIELD_SCENARIOS
        if item.name == "field_multi_step_toyota_procedures_to_case"
    )
    harness = ScriptedCiqReportHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    result, _, _ = harness.run_turn(
        scenario.turns[0],
        subject=scenario.initial_subject,
        prior_dialogue=[],
    )

    assert result.raw_report["execution_state"] == "not_requested"
    assert result.report["execution_state"] == "verified"
    assert result.raw_report == raw_report


def test_live_harness_uses_production_working_context_merge() -> None:
    from core.services.conversation_subjects import (
        track_active_subject_from_tool_result,
    )

    store = _FixtureSubjectStore(NISSAN_SUBJECT)
    refreshed = track_active_subject_from_tool_result(
        store,
        conversation_id=1,
        tool_name="calibration_iq_ro",
        result=_nissan_ro_call().result,
        tool_call_id="fixture-ciq-ro",
    )
    assert refreshed is not None

    weekly_scenario = next(
        scenario
        for scenario in FIELD_SCENARIOS
        if scenario.name == "field_weekly_work_readiness"
    )
    weekly = track_active_subject_from_tool_result(
        store,
        conversation_id=1,
        tool_name="calibration_iq_work_prep",
        result=weekly_scenario.turns[0].calls[0].result,
        tool_call_id="fixture-weekly",
    )
    assert weekly is not None
    assert weekly["payload"]["resource_id"] == "ro-nissan-1667"
    assert (
        weekly["payload"]["working_context"]["sections"]["weekly"]["source_owner"]
        == "calibration_iq_work_prep"
    )

    evidence = _nissan_radar_evidence_call(
        event="wheel alignment",
        evidence_id="fixture-adas-si",
        finding="Radar aiming follows alignment.",
        page=18,
    )
    enriched = track_active_subject_from_tool_result(
        store,
        conversation_id=1,
        tool_name="adas_si_search",
        result=evidence.result,
        tool_call_id="fixture-adas-si-call",
    )
    assert enriched is not None
    payload = enriched["payload"]
    assert payload["resource_id"] == "ro-nissan-1667"
    assert payload["working_context"]["sections"]["weekly"]["items_count"] == 2
    adas_evidence = payload["working_context"]["evidence"]["adas_si"]
    assert adas_evidence["application_bound"] is True
    assert adas_evidence["documents"][0]["page"] == 18
    assert adas_evidence["observation"]["tool_call_id"] == "fixture-adas-si-call"


def test_procedure_validator_accepts_production_requirement_scope() -> None:
    _structured_adas_procedure_search(
        {
            "vehicle": {"year": 2023, "make": "Chevrolet", "model": "Tahoe"},
            "repair_event": "windshield replacement",
            "system": "forward camera",
            "requirement_type": "calibration trigger",
            "question": "Which OEM procedure applies after this windshield event?",
        }
    )


def test_auth_process_fixture_exposes_production_human_boundary() -> None:
    result = _auth_scrapex_process_call().result
    assert result["status"] == "authentication_required"
    assert result["authentication_required"] is True
    assert result["requires_human"] is True
    assert result["work_complete"] is False
    assert result["executed"] is False
    assert result["message"]


def test_scrapex_read_schemas_require_observed_verbatim_batch_ids() -> None:
    from core.services.scrapex import SCRAPEX_ADAS_MAP_SCHEMA, SCRAPEX_READ_SCHEMA

    read_contract = json.dumps(SCRAPEX_READ_SCHEMA, ensure_ascii=False).casefold()
    assert "verbatim" in read_contract
    assert "placeholder" in read_contract
    assert "guess" in read_contract

    read_variants = {
        variant["properties"]["action"]["const"]: variant
        for variant in SCRAPEX_READ_SCHEMA["parameters"]["oneOf"]
    }
    assert set(read_variants) == {
        "list_batches",
        "batch_summary",
        "batch_exceptions",
        "batch_item",
        "preview_ciq_queue",
    }
    assert read_variants["list_batches"]["required"] == ["action"]
    assert set(read_variants["batch_item"]["required"]) == {
        "action",
        "batch_id",
        "ro_number",
    }
    preview = read_variants["preview_ciq_queue"]
    assert set(preview["required"]) == {"action", "phases"}
    assert "ro_number" not in preview["properties"]
    assert preview["additionalProperties"] is False

    acquisition_contract = json.dumps(
        SCRAPEX_ADAS_MAP_SCHEMA, ensure_ascii=False
    ).casefold()
    assert "process_one requires an observed exact batch_id" in acquisition_contract
    assert "create_exact_batch" in acquisition_contract
    acquisition_variants = {
        variant["properties"]["action"]["const"]: variant
        for variant in SCRAPEX_ADAS_MAP_SCHEMA["parameters"]["oneOf"]
    }
    assert set(acquisition_variants) == {
        "open_authentication",
        "acquire_exact",
        "create_exact_batch",
        "create_phase_batch",
        "process_one",
        "start_batch",
        "pause_batch",
    }
    assert all(
        variant["additionalProperties"] is False
        for variant in acquisition_variants.values()
    )
    assert set(acquisition_variants["process_one"]["required"]) == {
        "action",
        "batch_id",
        "ro_number",
    }


def test_scrapex_initial_catalog_has_no_id_bound_fixture_path() -> None:
    harness = LiveQwenHarness(WorkerTarget("http://fixture.invalid/v1", "fixture"))
    initial_tools = harness._business_tools_for_evidence(None, None)

    def actions_for(name: str) -> set[str]:
        tool = next(
            item for item in initial_tools if item["function"]["name"] == name
        )
        return {
            branch["properties"]["action"]["const"]
            for branch in tool["function"]["parameters"]["oneOf"]
        }

    assert actions_for("scrapex_read") == {
        "list_batches",
        "preview_ciq_queue",
    }
    assert actions_for("scrapex_adas_map") == {
        "open_authentication",
        "acquire_exact",
        "create_exact_batch",
        "create_phase_batch",
    }
    with pytest.raises(ModelProtocolError, match="advertised staged schema"):
        harness._assert_advertised_call_schema(
            "scrapex_read",
            {
                "action": "batch_item",
                "batch_id": "invented-batch-id",
                "ro_number": "2400911724",
            },
            initial_tools,
        )

    id_free = {
        "list_batches",
        "preview_ciq_queue",
        "open_authentication",
        "acquire_exact",
        "create_exact_batch",
        "create_phase_batch",
    }
    for scenario in SCENARIOS:
        for turn in scenario.turns:
            for path in (turn.calls, *turn.alternative_calls):
                first_scrapex = next(
                    (
                        call
                        for call in path
                        if call.name in {"scrapex_read", "scrapex_adas_map"}
                    ),
                    None,
                )
                if first_scrapex is not None:
                    assert first_scrapex.subset.get("action") in id_free, (
                        scenario.name,
                        first_scrapex,
                    )


def test_scrapex_batch_ids_unlock_only_after_next_model_round() -> None:
    from core.orchestrator.loop import tool_result_visible_to_model
    from core.tools.registry import (
        ToolBlocked,
        scrapex_apply_new_quarantine,
        scrapex_catalog_for_turn,
        scrapex_evidence_from_result,
        validate_scrapex_batch_binding,
    )

    conversation_id = 81
    message_id = 4

    def success(action: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "service": "ScrapeX",
            "action": action,
            "status": "verified",
            "success": True,
            "executed": True,
            "verified": True,
            "data": data,
        }

    old_args = {"action": "list_batches"}
    old_result = success("list_batches", {"batches": [{"id": "batch-old"}]})
    round_evidence = scrapex_evidence_from_result(
        "scrapex_read",
        old_args,
        tool_result_visible_to_model("scrapex_read", old_result),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="old-list",
    )
    assert round_evidence is not None and round_evidence.verified

    # Existing evidence exposes the bound branches, but a sibling list/create
    # result is accumulated separately and cannot authorize another call from
    # the same model response.
    unlocked_catalog = scrapex_catalog_for_turn(
        LiveQwenHarness(
            WorkerTarget("http://fixture.invalid/v1", "fixture")
        ).business_tools,
        round_evidence,
    )
    assert "batch_id" in json.dumps(unlocked_catalog)

    listed_args = {"action": "list_batches"}
    listed_result = success(
        "list_batches",
        {"batches": [{"id": "batch-from-list"}]},
    )
    after_list = scrapex_evidence_from_result(
        "scrapex_read",
        listed_args,
        tool_result_visible_to_model("scrapex_read", listed_result),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="new-list",
        previous=round_evidence,
    )
    assert scrapex_apply_new_quarantine(round_evidence, after_list) == round_evidence
    item_args = {
        "action": "batch_item",
        "batch_id": "batch-from-list",
        "ro_number": "2400911724",
    }
    with pytest.raises(ToolBlocked, match="copied verbatim"):
        validate_scrapex_batch_binding(
            "scrapex_read",
            item_args,
            round_evidence,
            conversation_id=conversation_id,
            message_id=message_id,
        )
    validate_scrapex_batch_binding(
        "scrapex_read",
        item_args,
        after_list,
        conversation_id=conversation_id,
        message_id=message_id,
    )

    created_args = {
        "action": "create_exact_batch",
        "ro_numbers": ["2400911724"],
    }
    created_result = success("create_exact_batch", {"id": "batch-from-create"})
    after_create = scrapex_evidence_from_result(
        "scrapex_adas_map",
        created_args,
        tool_result_visible_to_model("scrapex_adas_map", created_result),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="new-create",
        previous=round_evidence,
    )
    assert scrapex_apply_new_quarantine(round_evidence, after_create) == (
        round_evidence
    )
    process_args = {
        "action": "process_one",
        "batch_id": "batch-from-create",
        "ro_number": "2400911724",
    }
    with pytest.raises(ToolBlocked, match="copied verbatim"):
        validate_scrapex_batch_binding(
            "scrapex_adas_map",
            process_args,
            round_evidence,
            conversation_id=conversation_id,
            message_id=message_id,
        )
    validate_scrapex_batch_binding(
        "scrapex_adas_map",
        process_args,
        after_create,
        conversation_id=conversation_id,
        message_id=message_id,
    )


def test_bounded_scrapex_fixtures_match_exact_model_visible_serialization() -> None:
    from core.orchestrator.loop import (
        tool_result_json_for_model,
        tool_result_visible_to_model,
    )

    for expectation in (
        _existing_scrapex_list_call(),
        _acquisition_create_call(),
        _acquisition_process_call(),
    ):
        serialized = tool_result_json_for_model(
            expectation.name,
            expectation.result,
        )
        assert json.loads(serialized) == expectation.result
        assert tool_result_visible_to_model(
            expectation.name,
            expectation.result,
        ) == json.loads(serialized)


def test_scrapex_stale_and_quarantined_batch_ids_remain_blocked() -> None:
    from core.orchestrator.loop import tool_result_visible_to_model
    from core.tools.registry import (
        ToolBlocked,
        scrapex_apply_new_quarantine,
        scrapex_evidence_from_result,
        validate_scrapex_batch_binding,
    )

    conversation_id = 82
    message_id = 5
    list_args = {"action": "list_batches"}
    list_result = {
        "service": "ScrapeX",
        "action": "list_batches",
        "status": "verified",
        "success": True,
        "executed": True,
        "verified": True,
        "data": {
            "batches": [
                {"id": "batch-quarantined"},
                {"id": "batch-still-safe"},
            ]
        },
    }
    evidence = scrapex_evidence_from_result(
        "scrapex_read",
        list_args,
        tool_result_visible_to_model("scrapex_read", list_result),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="quarantine-list",
    )
    assert evidence is not None and evidence.verified
    process_args = {
        "action": "process_one",
        "batch_id": "batch-quarantined",
        "ro_number": "2400911724",
    }
    with pytest.raises(ToolBlocked, match="different conversation turn"):
        validate_scrapex_batch_binding(
            "scrapex_adas_map",
            process_args,
            evidence,
            conversation_id=conversation_id,
            message_id=message_id + 1,
        )

    indeterminate = {
        "service": "ScrapeX",
        "action": "process_one",
        "status": "indeterminate",
        "success": False,
        "executed": False,
        "verified": False,
        "may_have_executed": True,
        "indeterminate": True,
    }
    quarantined = scrapex_evidence_from_result(
        "scrapex_adas_map",
        process_args,
        tool_result_visible_to_model("scrapex_adas_map", indeterminate),
        conversation_id=conversation_id,
        message_id=message_id,
        source_tool_call_id="indeterminate-process",
        previous=evidence,
    )
    assert quarantined is not None and quarantined.verified
    assert "batch-quarantined" in quarantined.quarantined_batch_ids
    assert "batch-still-safe" in quarantined.batch_ids
    sibling_evidence = scrapex_apply_new_quarantine(evidence, quarantined)
    assert sibling_evidence is not None
    assert sibling_evidence.batch_ids == ("batch-still-safe",)
    assert sibling_evidence.quarantined_batch_ids == ("batch-quarantined",)
    with pytest.raises(ToolBlocked, match="automatic retry is forbidden"):
        validate_scrapex_batch_binding(
            "scrapex_adas_map",
            process_args,
            sibling_evidence,
            conversation_id=conversation_id,
            message_id=message_id,
        )
    validate_scrapex_batch_binding(
        "scrapex_adas_map",
        {"action": "start_batch", "batch_id": "batch-still-safe"},
        sibling_evidence,
        conversation_id=conversation_id,
        message_id=message_id,
    )


def test_verified_close_without_child_calibration_state_is_selected_for_audit() -> None:
    close_result = _operator_result()
    observation = {
        "name": "calibration_iq_operator",
        "arguments": {
            "actions": [
                {
                    "operation": "close_ro",
                    "repair_order_id": "ro-uuid-17",
                    "expected_version": 12,
                }
            ]
        },
        "result": close_result,
    }

    assert (
        _verified_close_without_child_calibration_state([observation]) is close_result
    )

    close_result["final_snapshots"]["ro-uuid-17"]["snapshot"]["calibrations"] = []
    assert _verified_close_without_child_calibration_state([observation]) is None


def test_close_final_semantic_audit_rejects_unsupported_child_state_claim() -> None:
    class ScriptedCloseAuditHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del messages, tools, tool_choice
            assert force_tool == "acceptance_close_truth_audit"
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "close-truth-audit",
                        "type": "function",
                        "function": {
                            "name": "acceptance_close_truth_audit",
                            "arguments": json.dumps(
                                {
                                    "mentions_child_calibration_state": True,
                                    "unsupported_child_calibration_claim": True,
                                    "rationale": (
                                        "The response claims there is no pending child "
                                        "calibration work."
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }

    harness = ScriptedCloseAuditHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    with pytest.raises(ModelProtocolError, match="unsupported child calibration state"):
        harness._assert_close_final_truth(
            final_content="The RO is closed and no calibration work remains.",
            close_result=_operator_result(),
        )


def test_declared_negative_truth_audit_rejects_unverified_mutation_success() -> None:
    class ScriptedTruthAuditHarness(LiveQwenHarness):
        def _completion(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            force_tool: str | None = None,
            tool_choice: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            del messages, tools, tool_choice
            assert force_tool == "acceptance_final_truth_audit"
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "truth-audit",
                        "type": "function",
                        "function": {
                            "name": "acceptance_final_truth_audit",
                            "arguments": json.dumps(
                                {
                                    "unsupported_claim": True,
                                    "violated_contracts": [
                                        "no_unreceipted_mutation_success"
                                    ],
                                    "rationale": (
                                        "The response calls an indeterminate close complete."
                                    ),
                                }
                            ),
                        },
                    }
                ],
            }

    harness = ScriptedTruthAuditHarness(
        WorkerTarget("http://fixture.invalid/v1", "fixture")
    )
    with pytest.raises(ModelProtocolError, match="violated declared truth"):
        harness._assert_declared_negative_truths(
            final_content="The repair order is closed.",
            report={"outcome": "indeterminate", "execution_state": "not_confirmed"},
            raw_report={
                "outcome": "indeterminate",
                "execution_state": "not_confirmed",
            },
            observations=[
                {
                    "name": "calibration_iq_operator",
                    "arguments": {"actions": [{"operation": "close_ro"}]},
                    "result": {
                        "status": "indeterminate",
                        "verified": False,
                        "may_have_executed": True,
                    },
                }
            ],
            contracts=frozenset({"no_unreceipted_mutation_success"}),
        )


def test_live_qwen_model_first_conversational_acceptance() -> None:
    if os.getenv(RUN_ENV, "").strip().casefold() not in {"1", "true", "yes", "on"}:
        pytest.skip(f"set {RUN_ENV}=1 to run the local Qwen acceptance suite")
    target = configured_worker_target()
    if not worker_is_healthy(target):
        pytest.skip(f"configured local worker is not healthy at {target.endpoint}")
    report = run_suite(target)
    report["production_catalog"] = run_production_catalog_smoke(target)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    failures = [row for row in report["results"] if row["status"] == "failed"]
    assert not failures, json.dumps(failures, indent=2, ensure_ascii=False)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", help="OpenAI-compatible /v1 base URL")
    parser.add_argument("--model", help="Configured model alias")
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="Run one named scenario; repeat to select several",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    configured = configured_worker_target()
    target = WorkerTarget(
        (args.endpoint or configured.endpoint).rstrip("/"),
        args.model or configured.model,
    )
    if not worker_is_healthy(target):
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "configured local worker is not healthy",
                    "worker": {"endpoint": target.endpoint, "model": target.model},
                },
                indent=2,
            )
        )
        return 2

    report = run_suite(
        target,
        scenario_names=set(args.scenarios or []) or None,
        timeout=args.timeout,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
