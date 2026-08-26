from __future__ import annotations

import json
from pathlib import Path

from core.services import research_auto_acquire, research_workflow


def test_existing_capture_deduplicates_by_alldata_url(tmp_path: Path):
    folder = tmp_path / "Acquired" / "ALLDATA"
    folder.mkdir(parents=True)
    pdf = folder / "2024 Ford Transit Camera.pdf"
    sidecar = folder / "2024 Ford Transit Camera.source.json"
    pdf.write_bytes(b"pdf")
    sidecar.write_text(
        json.dumps({"url": "https://my.alldata.com/repair/#/article/123"}),
        encoding="utf-8",
    )

    found = research_auto_acquire._existing_capture(
        folder, "https://my.alldata.com/repair/#/article/123"
    )
    assert found is not None
    assert found["pdf"] == pdf
    assert found["sidecar"] == sidecar


def test_auto_acquire_install_preserves_explicit_model_owned_persistence():
    research_auto_acquire.install()
    assert research_workflow.full_research.__module__ == "core.services.research_workflow"
    assert not getattr(research_workflow.full_research, "_xomni_auto_acquire", False)
