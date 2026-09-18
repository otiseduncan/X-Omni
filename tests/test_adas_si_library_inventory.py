"""How many vehicles ADAS SI represents comes from its authoritative inventory.

"How many vehicles are represented in ADAS SI?" is answered by
``summary.vehicle_application_count`` from the library's own inventory,
reached from the permanent surface as ``query_ciq kind=adas_si_library`` --
a read-only read that files nothing -- and never from a search result count.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.services import research_delegate
from core.services.adas_si import AdasSI
from core.tools import meta
from core.tools.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


def test_query_ciq_reaches_the_library_inventory_without_filing_anything() -> None:
    tool, args = meta.expand_query_ciq({"kind": "adas_si_library"})
    assert tool == "adas_si_inventory"
    assert args == {"organize_root": False}
    description = meta.QUERY_CIQ_SCHEMA["description"]
    assert "adas_si_library" in description and "never from search results" in description
    assert "adas_si_library" in meta.QUERY_CIQ_SCHEMA["parameters"]["properties"]["kind"]["enum"]


def test_the_vehicle_count_is_the_inventory_application_count(tmp_path: Path) -> None:
    library = tmp_path / "ADAS SI"
    for name in (
        "2020 Toyota Camry Front Camera.pdf",
        "2020 Toyota Camry Blind Spot Monitor.pdf",
        "2021 Ford F-150 AWD Front Camera.pdf",
        "2025 Kia K4 Blind Spot Monitor.pdf",
    ):
        (library / name).parent.mkdir(parents=True, exist_ok=True)
        (library / name).write_bytes(b"%PDF-fixture")
    root_drop = library / "2025 Kia K4 Blind Spot Monitor.pdf"
    adas = AdasSI(library, tmp_path / "index.sqlite")

    registry = Registry(ROOT / "config" / "tools.yaml")
    registry.register("adas_si_inventory", lambda args: adas.inventory_read(args))
    result = asyncio.run(registry.invoke("query_ciq", {"kind": "adas_si_library"}))

    summary = result["summary"]
    # Two Camry documents are one vehicle application; four documents, three vehicles.
    assert summary["document_count"] == 4
    assert summary["vehicle_application_count"] == 3
    # A count read never files a root drop.
    assert root_drop.is_file()
    assert result["storage_refresh"]["moved_count"] == 0
    assert result["storage_refresh"]["organize_root"] is False


def test_research_results_say_their_counts_are_not_inventory() -> None:
    assert "Result counts are not library inventory" in research_delegate.READING_GUIDE
