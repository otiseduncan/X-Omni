from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.services.dvr_continuous_playback import (
    PlaybackPart,
    _ffconcat_text,
    _select_contiguous_rows,
)


def _row(row_id: int, start: str, end: str) -> dict:
    return {
        "id": row_id,
        "filename": f"segment-{row_id}.mkv",
        "started_at": start,
        "ended_at": end,
        "complete": 1,
    }


def test_contiguous_selection_trims_overlap_and_stops_at_real_gap() -> None:
    rows = [
        _row(1, "2026-08-30T06:40:00Z", "2026-08-30T06:45:01Z"),
        _row(2, "2026-08-30T06:45:00Z", "2026-08-30T06:50:00Z"),
        _row(3, "2026-08-30T06:50:00Z", "2026-08-30T06:55:00Z"),
        # A genuine two-minute recording gap must end this HTTP stream.
        _row(4, "2026-08-30T06:57:00Z", "2026-08-30T07:02:00Z"),
    ]
    start = datetime(2026, 8, 30, 6, 43, 30, tzinfo=timezone.utc)

    selected = _select_contiguous_rows(rows, start)

    assert [row["id"] for row, _inpoint in selected] == [1, 2, 3]
    assert selected[0][1] == 210.0
    assert selected[1][1] == 1.0
    assert selected[2][1] == 0.0


def test_ffconcat_declares_effective_duration_after_inpoint(tmp_path: Path) -> None:
    first = _row(10, "2026-08-30T06:40:00Z", "2026-08-30T06:45:01Z")
    second = _row(11, "2026-08-30T06:45:00Z", "2026-08-30T06:50:00Z")
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    parts = [
        PlaybackPart(first, a, 210.0),
        PlaybackPart(second, b, 1.0),
    ]

    text = _ffconcat_text(parts)

    assert "inpoint 210.000" in text
    assert "duration 91.000" in text
    assert "inpoint 1.000" in text
    assert "duration 299.000" in text


def test_selection_requires_start_to_be_inside_recorded_coverage() -> None:
    rows = [_row(1, "2026-08-30T06:40:00Z", "2026-08-30T06:45:00Z")]
    start = datetime(2026, 8, 30, 6, 46, 0, tzinfo=timezone.utc)
    assert _select_contiguous_rows(rows, start) == []
