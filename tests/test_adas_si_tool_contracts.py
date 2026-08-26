from __future__ import annotations

import json

import pytest

from core.services.adas_si import AdasSI
from core.tools.registry import TOOL_SCHEMAS


def test_annotation_list_handler_exists_and_returns_bounded_records(tmp_path):
    source_root = tmp_path / "adas-si"
    source_root.mkdir()
    adas = AdasSI(source_root, tmp_path / "index.sqlite")
    written = adas.record_write(
        {"record_id": "camry-radar", "title": "Camry radar", "content": "Review."}
    )
    assert written["status"] == "success"

    invalid = adas.managed_root / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    result = adas.record_list({})
    assert result["status"] == "partial_success"
    assert result["count"] == 1
    assert result["invalid_count"] == 1
    assert result["records"][0]["record_id"] == "camry-radar"
    assert json.loads((adas.managed_root / "camry-radar.json").read_text())[
        "content"
    ] == "Review."


def test_adas_open_schema_matches_handler_argument_contract():
    params = TOOL_SCHEMAS["adas_si_open"]["parameters"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"relative_path", "query", "page"}
    assert "document" not in params["properties"]
    assert params["anyOf"] == [
        {"required": ["relative_path"]},
        {"required": ["query"]},
    ]


def test_adas_search_schema_requires_real_semantic_input(tmp_path):
    params = TOOL_SCHEMAS["adas_si_search"]["parameters"]
    assert params["anyOf"] == [
        {"required": ["vehicle"]},
        {"required": ["system"]},
        {"required": ["component"]},
        {"required": ["repair_event"]},
        {"required": ["requirement_type"]},
        {"required": ["question"]},
    ]
    assert params["properties"]["vehicle"]["minProperties"] == 1

    source_root = tmp_path / "adas-si"
    source_root.mkdir()
    adas = AdasSI(source_root, tmp_path / "index.sqlite")
    with pytest.raises(ValueError, match="supply at least one"):
        adas.model_search({"search_mode": "standard"})


def test_adas_open_handler_rejects_missing_path_and_query(tmp_path):
    source_root = tmp_path / "adas-si"
    source_root.mkdir()
    adas = AdasSI(source_root, tmp_path / "index.sqlite")
    with pytest.raises(ValueError, match="relative_path or query is required"):
        adas.open_document({})
