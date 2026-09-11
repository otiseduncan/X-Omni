"""Model-facing meta-tool surface.

The conversational model reasons about four permanent capabilities:

* ``query_ciq``          -- every Calibration IQ read, one contract
* ``delegate_research``  -- bounded multi-source research worker
* ``stage_action``       -- exact-resource read, staged contract, execution, receipt
* ``capability_search``  -- in-turn discovery of uncommon capabilities

Nothing here decides what the user meant.  ``query_ciq`` is a pure structural
expansion to the existing read handlers; ``stage_action`` plans a mutation from
the fresh exact-RO read the gateway just performed and the structured
arguments the model supplied.  Backend handlers, evidence binding, approvals,
receipts, and cards are unchanged: the gateway expands a meta call to the
concrete tool before any of them run.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

PERMANENT_TOOLS: tuple[str, ...] = (
    "query_ciq",
    "delegate_research",
    "stage_action",
    "capability_search",
)

# How many discovered tools one capability_search may unlock for the rest of
# the turn. Bounds the prompt growth a mid-turn discovery can cause.
MAX_UNLOCKED_TOOLS = 6

CIQ_STATUS_VALUES = (
    "NEW_ARRIVAL",
    "NEEDS_TECHNICIAN_REVIEW",
    "INITIAL_ASSESSMENT_COMPLETE",
    "REPAIR_IN_PROGRESS",
    "WAITING_ON_PREREQUISITES",
    "READY_FOR_TECHNICIAN_VERIFICATION",
    "CALIBRATION_READY",
    "CALIBRATION_IN_PROGRESS",
    "RETURNED_TO_SHOP",
    "CALIBRATION_COMPLETE",
    "ARCHIVED",
)

# kind -> (concrete tool, required fields, forwarded fields, fixed fields)
QUERY_CIQ_KINDS: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], dict[str, Any]]] = {
    "ro": (
        "calibration_iq_ro",
        ("repair_order_id",),
        ("repair_order_id", "shop"),
        {},
    ),
    "board_count": (
        "calibration_iq_summary",
        (),
        ("shop", "phase", "status", "insurance", "q"),
        {},
    ),
    "board_list": (
        "calibration_iq_read",
        (),
        ("shop", "phase", "status", "insurance", "q", "limit"),
        {},
    ),
    "phase_list": (
        "calibration_iq_work_prep",
        ("phase",),
        ("phase", "shop"),
        {"mode": "phase_list"},
    ),
    "ro_requirements": (
        "calibration_iq_work_prep",
        ("repair_order_id",),
        ("repair_order_id",),
        {"mode": "ro_requirements"},
    ),
    "adas_map_inventory": (
        "calibration_iq_work_prep",
        (),
        ("phases", "shop"),
        {"mode": "adas_map_inventory"},
    ),
    "status": ("calibration_iq_status", (), (), {}),
}

QUERY_CIQ_SCHEMA: dict[str, Any] = {
    "description": (
        "Read current Calibration IQ state; reads never change anything. kind=ro: "
        "exact read of one identified RO (workflow, phase, status, version, blockers, "
        "saved calibrations, research, documents); use it whenever one RO is known, "
        "including a current-state follow-up on the active subject. board_count / "
        "board_list: verified count or bounded rows for a shop/phase/status scope, "
        "finished work excluded unless include_completed. phase_list: one named "
        "phase. ro_requirements: one RO's governing ADAS Map requirements. "
        "adas_map_inventory: which active ROs have or lack an ADAS Map (never "
        "ScrapeX). status: service reachability. CIQ state is not OEM proof."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        # Property order is deliberate: llama.cpp's grammar emits properties in
        # declared order and cannot revisit an earlier one. Probed live on
        # 2026-09-11: the model reaches for repair_order_id first on an exact
        # read and for phase first on a board question, so shop must follow
        # both (shop-before-phase dropped the shop and pushed "Perry" into q
        # or invented a status; shop-before-repair_order_id dropped it on the
        # short RO form). The over-used status field comes last.
        "properties": {
            "kind": {"type": "string", "enum": list(QUERY_CIQ_KINDS)},
            "repair_order_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "RO as Otis named it this turn: full number, id, or 5-digit short "
                    "form (then also pass shop). Never the prior subject's."
                ),
            },
            "phase": {
                "type": "string",
                "pattern": "^[0-9]{1,2}$",
                "description": "Phase number as digits, e.g. '5' for 'phase five'; only when the request names a phase.",
            },
            "shop": {
                "type": "string",
                "description": (
                    "Shop named in the request (Perry, Macon, Warner Robins). Omit only "
                    "when no shop is named; that reads every shop. Required with a short "
                    "RO number."
                ),
            },
            "phases": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[0-9]{1,2}$"},
                "minItems": 1,
                "maxItems": 16,
                "uniqueItems": True,
                "description": "Explicit phase numbers (digits) for adas_map_inventory.",
            },
            "finished": {
                "type": "string",
                "enum": ["exclude", "include", "only"],
                "description": (
                    "Board scope for finished work. exclude (default): unfinished/active "
                    "work only, which is what an unqualified count or list asks for. "
                    "include: active plus finished. only: finished work alone."
                ),
            },
            "q": {"type": "string", "description": "Board search text (RO number, VIN)."},
            "insurance": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "status": {
                "type": "string",
                "enum": list(CIQ_STATUS_VALUES),
                "description": (
                    "Only when the request names this exact workflow status. Never add "
                    "one to mean active, open, unfinished, or in progress: finished work "
                    "is already excluded unless include_completed is true."
                ),
            },
        },
        "required": ["kind"],
    },
}


def expand_query_ciq(args: Any) -> tuple[str, dict[str, Any]]:
    """Map one ``query_ciq`` call to its concrete read tool and arguments."""

    if not isinstance(args, dict):
        raise ValueError("query_ciq arguments must be an object")
    kind = str(args.get("kind") or "").strip()
    if kind not in QUERY_CIQ_KINDS:
        raise ValueError(
            f"query_ciq kind must be one of {', '.join(QUERY_CIQ_KINDS)}"
        )
    tool, required, forwarded, fixed = QUERY_CIQ_KINDS[kind]
    missing = [
        field
        for field in required
        if args.get(field) in (None, "", [], {})
    ]
    if missing:
        raise ValueError(
            f"query_ciq kind={kind} requires {', '.join(missing)}"
        )
    concrete: dict[str, Any] = dict(fixed)
    for field in forwarded:
        value = args.get(field)
        if value in (None, "", [], {}):
            continue
        concrete[field] = value
    if kind in {"board_count", "board_list"}:
        finished = str(args.get("finished") or "exclude").strip()
        if finished == "include":
            concrete["include_completed"] = True
        elif finished == "only":
            concrete["terminal_only"] = True
    return tool, concrete


RESEARCH_SOURCES: tuple[str, ...] = (
    "adas_si",
    "automotive_knowledge",
    "alldata",
    "web",
)

DELEGATE_RESEARCH_SCHEMA: dict[str, Any] = {
    "description": (
        "Delegate one research objective to a bounded worker. Sources in order: "
        "local ADAS SI library, durable automotive knowledge, licensed ALLDATA "
        "(ScrapeX Navigator), public OEM web; stops at the first verified finding "
        "unless exhaustive. Returns provenance (document/page, record, URL, or "
        "ALLDATA task) with excerpts. Any vehicle, with or without an RO; give "
        "year/make/model when known. sources sets preference order; exclude_sources "
        "honors Otis's exclusions. Never changes Calibration IQ; preserve=true only "
        "captures verified external evidence into ADAS SI. A miss is only a miss in "
        "the sources checked."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "objective": {
                "type": "string",
                "minLength": 3,
                "maxLength": 600,
                "description": "The technical fact, procedure, or requirement to find.",
            },
            "vehicle": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "year": {"type": "integer", "minimum": 1900, "maximum": 2100},
                    "make": {"type": "string", "minLength": 1, "maxLength": 80},
                    "model": {"type": "string", "minLength": 1, "maxLength": 120},
                    "trim": {"type": "string", "minLength": 1, "maxLength": 80},
                },
            },
            "system": {"type": "string", "maxLength": 200},
            "component": {"type": "string", "maxLength": 200},
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": list(RESEARCH_SOURCES)},
                "uniqueItems": True,
                "description": "Preference order; omit for the default.",
            },
            "exclude_sources": {
                "type": "array",
                "items": {"type": "string", "enum": list(RESEARCH_SOURCES)},
                "uniqueItems": True,
            },
            "depth": {
                "type": "string",
                "enum": ["standard", "calibration_requirements", "repair_policy"],
                "description": "calibration_requirements scans whole documents for buried triggers/prerequisites.",
            },
            "exhaustive": {"type": "boolean"},
            "preserve": {"type": "boolean"},
        },
        "required": ["objective"],
    },
}


ADAS_MAP_STAGE_OPERATIONS: tuple[str, ...] = (
    "acquire_adas_map",
    "open_adas_map_authentication",
)


def _operator_branches(tool_name: str) -> list[dict[str, Any]]:
    from .registry import TOOL_SCHEMAS

    schema = TOOL_SCHEMAS.get(tool_name) or {}
    actions = ((schema.get("parameters") or {}).get("properties") or {}).get("actions") or {}
    branches = (actions.get("items") or {}).get("oneOf")
    return branches if isinstance(branches, list) else []


def stage_operations(*, allow_unscoped_creates: bool = False) -> tuple[str, ...]:
    from .registry import (
        CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS,
        CALIBRATION_IQ_ROUTINE_OPERATIONS,
        CALIBRATION_IQ_UNSCOPED_CREATE_OPERATIONS,
    )

    routine = [
        operation
        for operation in CALIBRATION_IQ_ROUTINE_OPERATIONS
        if allow_unscoped_creates
        or operation not in CALIBRATION_IQ_UNSCOPED_CREATE_OPERATIONS
    ]
    return (
        *routine,
        *CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS,
        *ADAS_MAP_STAGE_OPERATIONS,
    )


def stage_action_schema(*, allow_unscoped_creates: bool = False) -> dict[str, Any]:
    return {
        "description": (
            "The only way to change Calibration IQ or acquire an ADAS Map; only for a "
            "direct current-turn command. Reads the exact RO fresh first. Missing or "
            "stale ids, versions, or arguments return stage=staged with the current "
            "version, valid target ids, and the argument contract -- nothing changes; "
            "call again with those values to execute (stage=executed carries the "
            "receipt and final snapshot). delete_* operations raise Otis's approval "
            "card when executed; never ask for confirmation in prose instead. "
            "Whole-RO operations (close_ro, change_status with an explicitly named "
            "status, hold/resume/reopen, mark_no_calibration_required = the RO "
            "needs no calibration at all, add_*) take no target_id. One-child "
            "operations take target_id plus that child's version: delete_* "
            "removes one calibration/blocker/photo/prerequisite requirement, "
            "complete_/update_/reopen_* change one child's state. "
            "acquire_adas_map runs ScrapeX for that RO and attaches the document; "
            "open_adas_map_authentication opens the managed sign-in after a result "
            "reported authentication_required. Nothing continues automatically "
            "after sign-in; Otis asks again."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(stage_operations(allow_unscoped_creates=allow_unscoped_creates)),
                },
                "repair_order_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "RO as named this turn or from a fresh read: full number, id, or short form plus shop.",
                },
                "shop": {"type": "string"},
                "target_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact child id from the staged targets.",
                },
                "expected_version": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "RO operations: the RO's current version. Child operations "
                        "(target_id given): that target's own version from the staged "
                        "targets list, not the RO version."
                    ),
                },
                "arguments": {
                    "type": "object",
                    "description": "Operation fields per the staged argument contract.",
                },
                "source_scope": {
                    "type": "string",
                    "enum": ["active", "all", "terminal"],
                    "description": "acquire_adas_map only.",
                },
            },
            "required": ["operation"],
        },
    }


CAPABILITY_SEARCH_SCHEMA: dict[str, Any] = {
    "description": (
        "Find and unlock capabilities outside the permanent four: calendar, tasks, "
        "files, exterior camera and DVR footage, ADAS SI document display and "
        "inventory, knowledge capture, ScrapeX reads and status, service starts, "
        "worker status. Matches become callable for the rest of this turn. Omit "
        "query to list everything. Catalog presence is not execution proof."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "maxLength": 200,
                "description": "Words describing the needed capability, e.g. 'calendar', 'camera footage', 'open pdf'.",
            }
        },
        "required": [],
    },
}


def meta_tool_schemas(*, allow_unscoped_creates: bool = False) -> dict[str, dict[str, Any]]:
    return {
        "query_ciq": copy.deepcopy(QUERY_CIQ_SCHEMA),
        "delegate_research": copy.deepcopy(DELEGATE_RESEARCH_SCHEMA),
        "stage_action": stage_action_schema(allow_unscoped_creates=allow_unscoped_creates),
        "capability_search": copy.deepcopy(CAPABILITY_SEARCH_SCHEMA),
    }


def _branch_for_operation(tool_name: str, operation: str) -> Optional[dict[str, Any]]:
    for branch in _operator_branches(tool_name):
        enum = ((branch.get("properties") or {}).get("operation") or {}).get("enum")
        if isinstance(enum, list) and operation in enum:
            return branch
    return None


def _argument_errors(schema: Any, arguments: Any) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - lockfile pins jsonschema
        required = schema.get("required") or []
        return [
            f"arguments.{field} is required"
            for field in required
            if not isinstance(arguments, dict) or arguments.get(field) in (None, "")
        ]
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments if arguments is not None else {}),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        ("arguments." + ".".join(str(part) for part in error.absolute_path) + ": " if error.absolute_path else "arguments: ")
        + error.message
        for error in errors[:6]
    ]


def _repair_order_summary(read_result: dict[str, Any]) -> dict[str, Any]:
    repair_order = read_result.get("repair_order")
    repair_order = repair_order if isinstance(repair_order, dict) else {}
    raw = read_result.get("raw") if isinstance(read_result.get("raw"), dict) else {}
    raw_ro = raw.get("repair_order") if isinstance(raw.get("repair_order"), dict) else raw
    workflow = raw.get("workflow") if isinstance(raw.get("workflow"), dict) else {}
    summary = {
        "id": repair_order.get("id") or raw_ro.get("id"),
        "ro_number": (
            repair_order.get("RO")
            or repair_order.get("ro_number")
            or raw_ro.get("ro_number")
            or raw_ro.get("number")
        ),
        "status": workflow.get("status") or repair_order.get("Status") or raw_ro.get("status"),
        "phase": workflow.get("phase") or repair_order.get("Phase") or raw_ro.get("phase"),
        "version": repair_order.get("version") or raw_ro.get("version"),
        "shop": repair_order.get("Shop") or raw_ro.get("shop"),
        "vehicle": repair_order.get("Vehicle") or raw_ro.get("vehicle_description"),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", {})}


def plan_stage_action(
    args: dict[str, Any],
    read_result: Any,
    *,
    allow_unscoped_creates: bool = False,
) -> tuple[str, Any]:
    """Decide whether a stage_action call is complete enough to execute.

    Returns ``("staged", payload)`` when any id, version, target, or argument
    is missing or stale, or ``("execute", (concrete_tool, concrete_args))``
    when every binding matches the fresh exact-RO read.  Purely structural:
    the fresh read and the model's structured fields are the only inputs.
    """

    from .registry import (
        CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS,
        CALIBRATION_IQ_RO_REQUIRED_OPERATIONS,
        CALIBRATION_IQ_TARGET_REQUIRED_OPERATIONS,
        CALIBRATION_IQ_UNSCOPED_CREATE_OPERATIONS,
        CALIBRATION_IQ_VERSION_REQUIRED_OPERATIONS,
        _CALIBRATION_IQ_DESTRUCTIVE_TARGET_KINDS,
        _CALIBRATION_IQ_TARGET_OPERATION_KINDS,
        _calibration_iq_exact_binding,
        _calibration_iq_positive_version,
    )

    operation = str(args.get("operation") or "").strip()
    allowed = stage_operations(allow_unscoped_creates=allow_unscoped_creates)
    if operation not in allowed or operation in ADAS_MAP_STAGE_OPERATIONS:
        raise ValueError(f"{operation or 'operation'} is not a stageable Calibration IQ operation")
    binding = _calibration_iq_exact_binding(read_result)
    if binding is None:
        raise ValueError("stage_action requires a verified fresh exact-RO read")

    destructive = operation in CALIBRATION_IQ_DESTRUCTIVE_OPERATIONS
    concrete_tool = "calibration_iq_destructive" if destructive else "calibration_iq_operator"
    branch = _branch_for_operation(concrete_tool, operation)
    branch_properties = (branch or {}).get("properties") or {}
    arguments_schema = branch_properties.get("arguments") or {"type": "object"}
    contract = {
        "tool": concrete_tool,
        "description": (branch or {}).get("description"),
        "required": [
            field for field in ((branch or {}).get("required") or []) if field != "operation"
        ],
        "arguments": arguments_schema,
    }
    supplied_arguments = args.get("arguments")
    if supplied_arguments is None:
        supplied_arguments = {}
    if not isinstance(supplied_arguments, dict):
        supplied_arguments = {}
        argument_errors = ["arguments must be an object"]
    else:
        argument_errors = _argument_errors(arguments_schema, supplied_arguments)

    reasons: list[str] = list(argument_errors)
    summary = _repair_order_summary(read_result) if isinstance(read_result, dict) else {}
    staged: dict[str, Any] = {
        "stage": "staged",
        "executed": False,
        "mutated": False,
        "operation": operation,
        "tool": concrete_tool,
        "repair_order": summary,
        "repair_order_id": binding.repair_order_id,
        "expected_version": binding.expected_version,
        "argument_contract": contract,
    }
    if binding.research_expected_version is not None:
        staged["research_expected_version"] = binding.research_expected_version
    if destructive:
        staged["approval"] = (
            "Executing this operation raises Otis's approval card automatically; "
            "call stage_action again with the values below to raise it. Do not "
            "ask for confirmation in prose."
        )

    supplied_version = _calibration_iq_positive_version(args.get("expected_version"))
    target_id = str(args.get("target_id") or "").strip()
    action: dict[str, Any] = {"operation": operation, "arguments": supplied_arguments}

    if operation in CALIBRATION_IQ_UNSCOPED_CREATE_OPERATIONS:
        # Only reachable in the full profile; creates carry no binding.
        if reasons:
            staged["reasons"] = reasons
            return "staged", staged
        return "execute", (concrete_tool, {"actions": [action]})

    if destructive or operation in CALIBRATION_IQ_TARGET_REQUIRED_OPERATIONS:
        required_kind = (
            _CALIBRATION_IQ_DESTRUCTIVE_TARGET_KINDS.get(operation)
            if destructive
            else _CALIBRATION_IQ_TARGET_OPERATION_KINDS.get(operation)
        )
        candidates = [
            {
                "target_id": candidate_id,
                "kind": kind,
                "version": binding.target_version(candidate_id),
            }
            for candidate_id, kind in binding.target_kinds
            if required_kind is None or kind == required_kind
        ]
        staged["target_kind"] = required_kind
        staged["targets"] = candidates
        known_version = binding.target_version(target_id) if target_id else None
        if not target_id:
            reasons.append("target_id is required; choose one of targets")
        elif known_version is None or (
            required_kind is not None and binding.target_kind(target_id) != required_kind
        ):
            reasons.append("target_id is not a current child of this RO for that operation")
        elif supplied_version != known_version:
            reasons.append(
                f"expected_version must be the target's current version {known_version}"
            )
        if reasons:
            staged["reasons"] = reasons
            next_call: dict[str, Any] = {
                "operation": operation,
                "repair_order_id": binding.repair_order_id,
            }
            chosen = target_id if known_version is not None else (
                candidates[0]["target_id"] if len(candidates) == 1 else None
            )
            if chosen is not None:
                next_call["target_id"] = chosen
                next_call["expected_version"] = binding.target_version(chosen)
            else:
                next_call["target_id"] = "<one of targets>"
                next_call["expected_version"] = "<that target's version>"
            if supplied_arguments:
                next_call["arguments"] = supplied_arguments
            staged["next_call"] = next_call
            return "staged", staged
        action["target_id"] = target_id
        action["expected_version"] = known_version
        action["repair_order_id"] = binding.repair_order_id
        return "execute", (concrete_tool, {"actions": [action]})

    if operation in CALIBRATION_IQ_RO_REQUIRED_OPERATIONS:
        if target_id:
            # A child id on a whole-RO operation is the signature of a mixed-up
            # operation choice (e.g. mark_no_calibration_required for "remove
            # that calibration"). Never silently drop it: stage with the exact
            # one-child alternatives whose kind matches the supplied target.
            kind = binding.target_kind(target_id)
            alternatives = sorted(
                candidate
                for candidate, required_kind in _CALIBRATION_IQ_TARGET_OPERATION_KINDS.items()
                if kind is not None and required_kind == kind
            ) + sorted(
                candidate
                for candidate, required_kind in _CALIBRATION_IQ_DESTRUCTIVE_TARGET_KINDS.items()
                if kind is not None and required_kind == kind
            )
            reasons.append(
                f"{operation} applies to the whole RO and takes no target_id; "
                + (
                    f"for one {kind} use one of {alternatives}"
                    if alternatives
                    else "drop target_id if the whole RO is meant"
                )
            )
            staged["reasons"] = reasons
            staged["target_kind"] = kind
            staged["one_child_alternatives"] = alternatives
            return "staged", staged
        authoritative_version = (
            binding.research_expected_version
            if operation == "update_research"
            else binding.expected_version
        )
        if operation in CALIBRATION_IQ_VERSION_REQUIRED_OPERATIONS:
            if authoritative_version is None:
                reasons.append("the fresh read did not return a current version for this operation")
            elif supplied_version != authoritative_version:
                reasons.append(
                    f"expected_version must be the current version {authoritative_version}"
                )
        elif supplied_version is not None and supplied_version != binding.expected_version:
            reasons.append(
                f"expected_version does not match the current version {binding.expected_version}"
            )
        if reasons:
            staged["reasons"] = reasons
            next_call = {
                "operation": operation,
                "repair_order_id": binding.repair_order_id,
                "expected_version": authoritative_version,
            }
            if supplied_arguments:
                next_call["arguments"] = supplied_arguments
            staged["next_call"] = next_call
            return "staged", staged
        action["repair_order_id"] = binding.repair_order_id
        if operation in CALIBRATION_IQ_VERSION_REQUIRED_OPERATIONS:
            action["expected_version"] = authoritative_version
        return "execute", (concrete_tool, {"actions": [action]})

    raise ValueError(f"{operation} has no staged write contract")


def score_capability(query: str, name: str, description: str) -> int:
    """Rank a discoverable capability against a model-supplied query.

    Token overlap between the model's structured ``query`` field and the
    tool's own name/description.  The model chose to search; this only orders
    the catalog it asked to see.
    """

    tokens = [token for token in query.casefold().replace("_", " ").split() if len(token) > 1]
    if not tokens:
        return 0
    haystack = f"{name.replace('_', ' ')} {description}".casefold()
    name_words = set(name.casefold().split("_"))
    score = 0
    for token in tokens:
        if token in name_words:
            score += 3
        elif token in haystack:
            score += 1
    return score
