"""Canonical storage policy and migration for the ADAS SI source library.

Operator rules:
- Service information is organized by Year / Make / Model.
- ADAS Map evidence is organized by repair order.

The migration is deliberately conservative. It moves a source only when its
identity can be proven from an existing artifact-catalog row, a structured
provenance sidecar, or the same filename descriptor ADAS SI already trusts.
Anything unresolved is left in place and reported rather than guessed.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

ADAS_MAP_DIRNAME = "ADAS Map"
_STORAGE_LOCK = threading.Lock()
_MIGRATED_ROOTS: set[str] = set()

_RO_RE = re.compile(r"^\d{6,20}$")
_ADAS_MAP_FILE_RE = re.compile(
    r"^(?P<ro>\d{6,20})\s+adas\s+map(?:\s+.*)?\.pdf$",
    re.IGNORECASE,
)
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._()&+\- ]+")


def safe_component(value: object, fallback: str, *, maximum: int = 120) -> str:
    cleaned = _SAFE_COMPONENT_RE.sub(" ", str(value or ""))
    cleaned = " ".join(cleaned.split()).strip(" .")
    return (cleaned[:maximum] or fallback).strip()


def normalize_vehicle_identity(value: object) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    raw_year = str(value.get("year") or "").strip()
    make = safe_component(value.get("make"), "", maximum=64)
    model = safe_component(
        value.get("model") or value.get("model_trim"),
        "",
        maximum=100,
    )
    if not re.fullmatch(r"(?:19|20)\d{2}", raw_year) or not make or not model:
        return None
    return {"year": int(raw_year), "make": make, "model": model}


def canonical_vehicle_identity(
    source_root: Path, path: Path
) -> Optional[dict[str, Any]]:
    try:
        parts = path.resolve().relative_to(Path(source_root).resolve()).parts
    except ValueError:
        return None
    if len(parts) < 4:
        return None
    if parts[0].casefold() == ADAS_MAP_DIRNAME.casefold():
        return None
    return normalize_vehicle_identity(
        {"year": parts[0], "make": parts[1], "model": parts[2]}
    )


def service_information_directory(
    source_root: Path,
    vehicle: dict[str, Any],
    *suffix: object,
) -> Path:
    identity = normalize_vehicle_identity(vehicle)
    if identity is None:
        raise ValueError("ADAS SI storage requires exact year, make, and model.")
    target = (
        Path(source_root).resolve()
        / str(identity["year"])
        / safe_component(identity["make"], "Unknown Make", maximum=64)
        / safe_component(identity["model"], "Unknown Model", maximum=100)
    )
    for raw in suffix:
        part = safe_component(raw, "", maximum=100)
        if part:
            target = target / part
    return target


def adas_map_directory(source_root: Path, ro_number: object) -> Path:
    ro = str(ro_number or "").strip()
    if not _RO_RE.fullmatch(ro):
        raise ValueError("ADAS Map storage requires a numeric repair-order number.")
    return Path(source_root).resolve() / ADAS_MAP_DIRNAME / ro


def adas_map_pdf_path(source_root: Path, ro_number: object) -> Path:
    ro = str(ro_number or "").strip()
    return adas_map_directory(source_root, ro) / f"{ro} ADAS Map.pdf"


def _sidecar_path(pdf_path: Path) -> Path:
    return pdf_path.with_name(pdf_path.stem + ".source.json")


def _read_sidecar(pdf_path: Path) -> dict[str, Any]:
    sidecar = _sidecar_path(pdf_path)
    if not sidecar.is_file():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _catalog_rows(cache_path: Path) -> dict[str, dict[str, Any]]:
    cache = Path(cache_path)
    if not cache.is_file():
        return {}
    try:
        with sqlite3.connect(cache) as db:
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "artifact_catalog" not in tables:
                return {}
            rows = db.execute(
                """
                SELECT path,artifact_kind,ro_number,year,make,model
                FROM artifact_catalog
                """
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {
        str(row[0]): {
            "artifact_kind": row[1],
            "ro_number": row[2],
            "year": row[3],
            "make": row[4],
            "model": row[5],
        }
        for row in rows
    }


def _vehicle_from_sidecar(sidecar: dict[str, Any]) -> Optional[dict[str, Any]]:
    candidates = (
        sidecar.get("vehicle"),
        sidecar.get("target"),
        sidecar.get("vehicle_identity"),
    )
    for candidate in candidates:
        identity = normalize_vehicle_identity(candidate)
        if identity is not None:
            return identity
    return None


def _vehicle_from_catalog(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    return normalize_vehicle_identity(
        {
            "year": row.get("year"),
            "make": row.get("make"),
            "model": row.get("model"),
        }
    )


def _ro_identity(
    path: Path,
    sidecar: dict[str, Any],
    catalog_row: dict[str, Any],
) -> Optional[str]:
    match = _ADAS_MAP_FILE_RE.fullmatch(path.name)
    if match:
        return match.group("ro")
    if str(catalog_row.get("artifact_kind") or "").casefold() == "adas_map_report":
        value = str(catalog_row.get("ro_number") or "").strip()
        if _RO_RE.fullmatch(value):
            return value
    for key in ("ro_number", "repair_order", "repair_order_number"):
        value = str(sidecar.get(key) or "").strip()
        if _RO_RE.fullmatch(value):
            return value
    return None


def _legacy_suffix(path: Path) -> tuple[str, ...]:
    parts = {part.casefold() for part in path.parts}
    if "adas quick reference" in parts:
        return ("ALLDATA", "ADAS Quick Reference")
    if "alldata" in parts:
        return ("ALLDATA",)
    if "public oem" in parts:
        return ("Public OEM",)
    return ()


def _unique_target(target: Path) -> Path:
    if not target.exists():
        return target
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _invalidate_derived_paths(cache_path: Path, moved: dict[str, str]) -> None:
    cache = Path(cache_path)
    if not cache.is_file() or not moved:
        return
    try:
        with sqlite3.connect(cache) as db:
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for old_path, new_path in moved.items():
                if "pages" in tables:
                    db.execute(
                        "DELETE FROM pages WHERE path IN (?,?)",
                        (old_path, new_path),
                    )
                if "artifact_catalog" in tables:
                    db.execute(
                        "DELETE FROM artifact_catalog WHERE path IN (?,?)",
                        (old_path, new_path),
                    )
    except sqlite3.Error:
        # Both tables are derived caches and can be rebuilt from the PDFs.
        return


def _remove_empty_parents(start: Path, root: Path) -> None:
    current = start
    root = root.resolve()
    while current != root:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def migrate_library(
    source_root: Path,
    cache_path: Path,
    descriptor_resolver: Callable[[Path, Path], dict[str, Any]],
) -> dict[str, Any]:
    root = Path(source_root).resolve()
    if not root.is_dir():
        return {
            "moved": 0,
            "unresolved": [],
            "paths": {},
            "policy": {
                "service_information": "<Year>/<Make>/<Model>",
                "adas_map": "ADAS Map/<RO>",
            },
        }

    catalog = _catalog_rows(cache_path)
    moved: dict[str, str] = {}
    unresolved: list[str] = []

    for source in sorted(root.rglob("*.pdf"), key=lambda p: str(p).casefold()):
        try:
            relative = source.resolve().relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            continue
        if relative.parts[0].casefold() in {
            "_xomni_managed",
            "_xomni_backups",
        }:
            continue
        if relative.parts[0].casefold() == ADAS_MAP_DIRNAME.casefold():
            continue
        if canonical_vehicle_identity(root, source) is not None:
            continue

        sidecar = _read_sidecar(source)
        catalog_row = catalog.get(str(source.resolve()), {})
        ro_number = _ro_identity(source, sidecar, catalog_row)

        if ro_number:
            destination = adas_map_pdf_path(root, ro_number)
        else:
            vehicle = _vehicle_from_catalog(catalog_row)
            if vehicle is None:
                vehicle = _vehicle_from_sidecar(sidecar)
            if vehicle is None:
                try:
                    descriptor = descriptor_resolver(root, source)
                except Exception:
                    descriptor = {}
                if isinstance(descriptor, dict) and descriptor.get("application_parsed"):
                    vehicle = normalize_vehicle_identity(descriptor)
            if vehicle is None:
                unresolved.append(relative.as_posix())
                continue
            destination = (
                service_information_directory(
                    root, vehicle, *_legacy_suffix(source)
                )
                / source.name
            )

        destination = _unique_target(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        old_abs = str(source.resolve())
        old_parent = source.parent
        shutil.move(str(source), str(destination))
        moved[old_abs] = str(destination.resolve())

        source_sidecar = _sidecar_path(source)
        if source_sidecar.is_file():
            destination_sidecar = _unique_target(_sidecar_path(destination))
            shutil.move(str(source_sidecar), str(destination_sidecar))

        manifest = old_parent / "quick-reference-manifest.json"
        if manifest.is_file():
            manifest_target = destination.parent / manifest.name
            if not manifest_target.exists():
                shutil.move(str(manifest), str(manifest_target))

        _remove_empty_parents(old_parent, root)

    _invalidate_derived_paths(cache_path, moved)
    return {
        "moved": len(moved),
        "unresolved": unresolved,
        "paths": moved,
        "policy": {
            "service_information": "<Year>/<Make>/<Model>",
            "adas_map": "ADAS Map/<RO>",
        },
    }


def migrate_library_once(
    source_root: Path,
    cache_path: Path,
    descriptor_resolver: Callable[[Path, Path], dict[str, Any]],
) -> dict[str, Any]:
    key = str(Path(source_root).resolve()).casefold()
    with _STORAGE_LOCK:
        if key in _MIGRATED_ROOTS:
            return {
                "moved": 0,
                "unresolved": [],
                "paths": {},
                "already_checked": True,
            }
        result = migrate_library(source_root, cache_path, descriptor_resolver)
        _MIGRATED_ROOTS.add(key)
        return result
