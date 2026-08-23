"""Regression coverage for the tool-result-to-model truncation bug.

Live field trace: adas_si_inventory's real result for a 137-document library
serializes to ~46,000 characters. The previous feed path did
``json.dumps(result)[:12000]``, a flat byte-level cut that landed inside the
"documents" list and silently deleted the trailing "applications" and
"evidence_contract" fields -- including the explicit
"do_not_infer_records_from_counts" guardrail the ADAS SI service was written
to provide. The model was left with a corrupted JSON fragment and one intact
number (parsed_document_count), and reported that number as an "ADAS Map
report" count -- a evidence-integrity bug, not primarily a reasoning bug.

_bounded_tool_result_json must always return valid, parseable JSON, must
never drop a scalar/dict field to make room, and should shrink the largest
list-valued field first, in preference to slicing the raw string.
"""

from __future__ import annotations

import json

from core.orchestrator.loop import _bounded_tool_result_json


def test_small_result_passes_through_unchanged() -> None:
    value = {"status": "success", "count": 3}
    assert _bounded_tool_result_json(value) == json.dumps(value, default=str)


def test_oversized_list_field_is_shrunk_not_raw_sliced() -> None:
    value = {
        "status": "success",
        "evidence_contract": {"do_not_infer_records_from_counts": True},
        "summary": {"document_count": 500},
        "documents": [{"title": f"doc-{i}", "body": "x" * 200} for i in range(500)],
    }
    encoded = _bounded_tool_result_json(value, max_chars=5000)

    assert len(encoded) <= 5000 + 200  # small overshoot only from the last kept item
    parsed = json.loads(encoded)  # must be valid, complete JSON -- never truncated mid-object
    assert parsed["evidence_contract"] == {"do_not_infer_records_from_counts": True}
    assert parsed["summary"] == {"document_count": 500}
    assert parsed["documents_omitted_count"] > 0
    assert len(parsed["documents"]) + parsed["documents_omitted_count"] == 500


def test_scalar_and_dict_fields_are_never_touched_to_make_room() -> None:
    value = {
        "status": "success",
        "authoritative_path": "X:\\ADAS SI",
        "small_list": list(range(3)),
        "big_list": [{"n": i, "pad": "y" * 300} for i in range(200)],
    }
    encoded = _bounded_tool_result_json(value, max_chars=4000)
    parsed = json.loads(encoded)

    assert parsed["status"] == "success"
    assert parsed["authoritative_path"] == "X:\\ADAS SI"
    assert parsed["small_list"] == [0, 1, 2]
    assert len(parsed["big_list"]) < 200


def test_result_with_no_shrinkable_list_falls_back_to_raw_cut() -> None:
    value = {"status": "success", "blob": "z" * 20000}
    encoded = _bounded_tool_result_json(value, max_chars=5000)
    assert len(encoded) == 5000
