from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.services.dvr_continuous_playback import (
    PlaybackPart,
    _ffconcat_text,
    _is_expected_successor,
    _select_contiguous_rows,
)


def _row(row_id: int, start: str, end: str, *, filename: str | None = None) -> dict:
    return {
        "id": row_id,
        "filename": filename or f"20260830-{row_id:06d}.mkv",
        "started_at": start,
        "ended_at": end,
        "complete": 1,
    }


def _session_name(ordinal: int) -> str:
    return f"20260830T064000123456Z-{ordinal:06d}.mkv"


def test_contiguous_selection_trims_overlap_and_stops_at_real_legacy_gap() -> None:
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


def test_same_recorder_session_ignores_mtime_end_jitter() -> None:
    rows = [
        _row(
            10,
            "2026-08-30T06:40:00Z",
            "2026-08-30T06:44:58Z",
            filename=_session_name(0),
        ),
        _row(
            11,
            "2026-08-30T06:45:00Z",
            "2026-08-30T06:49:58Z",
            filename=_session_name(1),
        ),
        _row(
            12,
            "2026-08-30T06:50:00Z",
            "2026-08-30T06:55:01Z",
            filename=_session_name(2),
        ),
    ]
    start = datetime(2026, 8, 30, 6, 42, 0, tzinfo=timezone.utc)

    selected = _select_contiguous_rows(rows, start)

    assert [row["id"] for row, _inpoint in selected] == [10, 11, 12]
    assert _is_expected_successor(rows[0], rows[1]) is True
    assert _is_expected_successor(rows[1], rows[2]) is True


def test_missing_ordinal_is_a_real_break_even_if_wall_clock_is_close() -> None:
    first = _row(
        20,
        "2026-08-30T06:40:00Z",
        "2026-08-30T06:45:00Z",
        filename=_session_name(0),
    )
    skipped = _row(
        22,
        "2026-08-30T06:50:00Z",
        "2026-08-30T06:55:00Z",
        filename=_session_name(2),
    )
    assert _is_expected_successor(first, skipped) is False


def test_ffconcat_uses_media_timing_not_sqlite_close_time(tmp_path: Path) -> None:
    first = _row(10, "2026-08-30T06:40:00Z", "2026-08-30T06:44:58Z")
    second = _row(11, "2026-08-30T06:45:00Z", "2026-08-30T06:49:58Z")
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    parts = [
        PlaybackPart(first, a, 120.0),
        PlaybackPart(second, b, 0.0),
    ]

    text = _ffconcat_text(parts)

    assert "inpoint 120.000" in text
    # Never manufacture ffconcat duration values from mtime-derived ended_at;
    # FFmpeg should use the actual media packet duration instead.
    assert "duration " not in text


def test_selection_requires_start_to_be_inside_recorded_coverage() -> None:
    rows = [_row(1, "2026-08-30T06:40:00Z", "2026-08-30T06:45:00Z")]
    start = datetime(2026, 8, 30, 6, 46, 0, tzinfo=timezone.utc)
    assert _select_contiguous_rows(rows, start) == []
