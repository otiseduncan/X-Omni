"""get_shared_instance memoizes AdasSI by (source_root, cache_path).

calibration_iq_work_prep.py used to construct a fresh AdasSI (a full
filesystem walk plus a cache schema check) on every single tool
invocation instead of once at process startup, duplicating the instance
main.py already built for the same underlying store. get_shared_instance
is the fix: one AdasSI per distinct (source_root, cache_path) pair.
"""

from __future__ import annotations

from pathlib import Path

from core.services.adas_si import AdasSI, get_shared_instance


def test_shared_instance_is_identical_for_the_same_paths(tmp_path: Path) -> None:
    root = tmp_path / "ADAS SI"
    root.mkdir()
    cache = tmp_path / "cache" / "index.sqlite"

    first = get_shared_instance(root, cache)
    second = get_shared_instance(root, cache)

    assert first is second
    assert isinstance(first, AdasSI)


def test_shared_instance_differs_for_different_paths(tmp_path: Path) -> None:
    root_a = tmp_path / "ADAS SI A"
    root_a.mkdir()
    root_b = tmp_path / "ADAS SI B"
    root_b.mkdir()
    cache_a = tmp_path / "cache-a" / "index.sqlite"
    cache_b = tmp_path / "cache-b" / "index.sqlite"

    instance_a = get_shared_instance(root_a, cache_a)
    instance_b = get_shared_instance(root_b, cache_b)

    assert instance_a is not instance_b
