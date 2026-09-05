"""Read-only source discovery plus a local metadata/text catalog for ADAS SI.

The ADAS SI PDFs remain authoritative and immutable.  This module only writes
derived rows into X Omni's existing ADAS SI SQLite cache.  ScrapeX is opened
with SQLite's read-only immutable URI contract; its database is never migrated
or otherwise modified here.

The catalog deliberately distinguishes an ADAS Map requirement report from OE
service information.  A report can establish which calibrations govern an RO,
but it is procedure coverage only when its own OE Service Information section
contains the requested system.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from .adas_si import KNOWN_MAKES, describe_document

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - normalized as an unreadable artifact
    PdfReader = None

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - normalized as an unreadable artifact
    pdfium = None


CATALOG_SCHEMA_VERSION = "7"  # ScrapeX ADAS Map contract v3 -- forces a full re-scan
SCRAPEX_ADAS_MAP_CONTRACT_VERSION = 3
SCRAPEX_DB_ENV = "XOMNI_SCRAPEX_DB_PATH"
DEFAULT_SCRAPEX_DB = Path(r"X:\ScrapeX\data\scrapex.sqlite3")

DISCOVERY_VERIFIED = "verified"
DISCOVERY_NOT_FOUND = "not_found"
DISCOVERY_UNVERIFIED = "unverified"
DISCOVERY_AMBIGUOUS = "ambiguous"

COVERED = "COVERED"
MISSING = "MISSING"
UNVERIFIED = "UNVERIFIED"

_CANONICAL_MAP_EVIDENCE_STATES = {
    "requirements_captured",
    "needs_operator",
    "adas_map_complete",
}

_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
_RO_FILE_RE = re.compile(r"^(?P<ro>\d{6,})\s+adas\s+map$", re.IGNORECASE)
_SAFE_REQUIREMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 /&()+,.'\-]{1,158}$")
_PROCEDURE_MARKERS = (
    "service information",
    "service procedure",
    "calibration procedure",
    "initialization procedure",
    "adjustment procedure",
    "relearn procedure",
    "target placement",
    "scan tool",
    "oem procedure",
    "oe procedure",
)

_FAMILY_LABELS = {
    "occupant_classification": "Occupant Classification System",
    "front_camera": "Front/Windshield Camera",
    "seat_belt": "Seat Belt",
    "blind_spot": "Blind Spot Monitor",
    "front_radar": "Front Radar",
    "steering_angle": "Steering Angle Sensor",
    "rear_camera": "Rear Camera",
    "surround_camera": "Surround View Camera",
    "parking_assist": "Parking Assist Sensor",
}

# These are intentionally bounded.  In particular, short acronyms are matched
# as complete normalized tokens, never as substrings of unrelated words.
_ALIAS_FAMILIES: dict[str, tuple[str, ...]] = {
    "occupant_classification": (
        "occupant classification system",
        "occupant classification",
        "seat weight sensor",
        "seat weight",
        "passenger presence system",
        "passenger presence",
        "ocs",
    ),
    "front_camera": (
        "windshield camera",
        "front camera",
        "front view camera",
        "forward camera",
        "forward facing camera",
        "ipma",
        "mono camera",
        "monocamera",
    ),
    "seat_belt": ("seat belt", "seatbelt"),
    "blind_spot": ("blind spot monitor", "blind spot", "bsm", "bsd"),
    "front_radar": ("front radar", "forward radar", "millimeter wave radar"),
    "steering_angle": ("steering angle sensor", "steering angle", "sas"),
    "rear_camera": ("rear camera", "rear view camera", "reverse camera"),
    "surround_camera": (
        "surround view camera",
        "surround view",
        "around view camera",
        "around view",
        "360 camera",
    ),
    "parking_assist": (
        "parking assist sensor",
        "parking sensor",
        "park assist",
        "ultrasonic sensor",
        "sonar sensor",
    ),
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_LIGATURE_REPLACEMENTS = {
    # "ti" has no ToUnicode mapping at all in these reports' embedded font:
    # pypdf renders the glyph as a literal NUL, pypdfium2 (the fallback
    # path) renders the same glyph as U+FFFE. Either way "Estimate" comes
    # out "Es<gap>mate", "Identified" comes out "Iden<gap>fied", "Static
    # Calibration" comes out "Sta<gap>c Calibra<gap>on" -- confirmed live
    # against a real report, and it silently broke every downstream regex
    # expecting those exact words, so a real, confirmed-correct-vehicle
    # report parsed to zero requirements even though the calibration was
    # genuinely printed on the page.
    "\x00": "ti",
    "￾": "ti",
    # These, by contrast, resolve correctly to real Unicode ligature code
    # points (also confirmed live in the same document -- "Identified"
    # came through as "Iden" + U+FB01 + "ed") -- just not to the plain
    # ASCII letter pairs the parser's regexes are written against.
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "ft",
    "ﬆ": "st",
}


def _normalize_ligature_artifacts(text: str) -> str:
    """Undo font-decoding gaps observed live in real ADAS Map PDFs where a
    ligature glyph (only ever "ti"/"fi" confirmed so far) doesn't come
    through as its plain letter pair -- see _LIGATURE_REPLACEMENTS."""
    for artifact, letters in _LIGATURE_REPLACEMENTS.items():
        text = text.replace(artifact, letters)
    return text


def _fold(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _compact(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _valid_vin(value: object) -> bool:
    return bool(re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", str(value or "").strip().upper()))


def _contains_phrase(text: object, phrase: object) -> bool:
    haystack = f" {_fold(text)} "
    needle = _fold(phrase)
    return bool(needle) and f" {needle} " in haystack


def _requirement_family(label: object) -> str:
    folded = _fold(label)
    for family, aliases in _ALIAS_FAMILIES.items():
        if any(_contains_phrase(folded, alias) for alias in aliases):
            return family
    return f"literal:{folded}" if folded else ""


def _aliases_for(label: object) -> tuple[str, ...]:
    family = _requirement_family(label)
    if family in _ALIAS_FAMILIES:
        return _ALIAS_FAMILIES[family]
    literal = family.removeprefix("literal:")
    return (literal,) if literal else ()


def _has_alias(text: object, label: object) -> bool:
    return any(_contains_phrase(text, alias) for alias in _aliases_for(label))


def _safe_requirement(value: object) -> Optional[str]:
    label = " ".join(str(value or "").split())
    if not label or not _SAFE_REQUIREMENT_RE.fullmatch(label):
        return None
    if _fold(label) in {
        "calibration",
        "calibration requirements",
        "required calibrations",
        "service information",
        "oe service information",
        "none",
        "not required",
    }:
        return None
    return label


def _clean_canonical_requirements(raw: object) -> list[dict[str, str]]:
    """Accept only current structured ADAS Map modal provenance.

    Legacy string arrays are intentionally ignored; those rows were populated
    before the contract could distinguish a requirement from surrounding UI.
    """

    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("source") or "").strip().casefold()
            != "adas_map_required_list_item"
        ):
            continue
        if (
            str(item.get("source_context") or "").strip().casefold()
            != "selected_required_modal"
        ):
            continue
        classes = str(item.get("source_control_class") or "").casefold().split()
        if "custom-link" not in classes:
            continue
        runtime_id = str(item.get("source_context_runtime_id") or "").strip()
        if not runtime_id:
            continue
        label = _safe_requirement(
            item.get("label") or item.get("calibration_type") or item.get("name")
        )
        if not label:
            continue
        family = _requirement_family(label)
        key = family or _fold(label)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "label": label,
                "family": family,
                "source": "adas_map_required_list_item",
                "source_context": "selected_required_modal",
                "source_context_runtime_id": runtime_id,
            }
        )
    return cleaned


def _json_load(value: object, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback


def _label_value(text: str, labels: Iterable[str]) -> Optional[str]:
    alternatives = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)(?:^|\s)(?:{alternatives})\s*(?:[:#]|\s+-\s)\s*"
        rf"(?P<value>[^\r\n]+)",
        text,
    )
    if not match:
        return None
    value = match.group("value").replace("\x00", "")
    # PDF print snapshots sometimes flatten several visual fields onto one text
    # line.  Stop at the next known field instead of absorbing unrelated data
    # into Make/Model/Inspection values.
    value = re.split(
        r"(?i)\s+(?=(?:last updated|estimate source|repair order|ro number|vin|"
        r"model year|year|vehicle make|make|vehicle model|model|inspection id|"
        r"inspection number)\s*(?:[:#]|\s+-\s))",
        value,
        maxsplit=1,
    )[0]
    return " ".join(value.split()) or None


def _section(text: str, heading: re.Pattern[str]) -> str:
    lines = text.splitlines()
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if heading.search(line):
            start = index + 1
            break
    if start is None:
        return ""
    selected: list[str] = []
    stop = re.compile(
        r"(?i)^\s*(?:end\s+(?:of\s+)?|vehicle information|repair order|inspection "
        r"(?:summary|details)|photos?|notes?|disclaimer|technician)\b"
    )
    for line in lines[start : start + 120]:
        if stop.search(line) and selected:
            break
        selected.append(line)
    return "\n".join(selected).strip()


def _governing_section(text: str) -> str:
    lines = text.splitlines()
    summary_heading = re.compile(
        r"(?i)\b(?:identified\s+adas\s+related\s+services|"
        r"following\s+calibrations?/initializations?\s+have\s+been\s+identified)\b"
    )
    start = next(
        (index + 1 for index, line in enumerate(lines) if summary_heading.search(line)),
        None,
    )
    if start is not None:
        selected: list[str] = []
        for line in lines[start : start + 80]:
            if re.search(r"(?i)^\s*scanning\s+requirements?\b", line):
                break
            selected.append(line)
        bounded = "\n".join(selected).strip()
        if bounded:
            return bounded
    return _section(
        text,
        re.compile(
            r"(?i)\b(?:required calibrations?|calibration requirements?|"
            r"required systems?|required calibration details)\b"
        ),
    )


def _oe_section(text: str) -> str:
    return _section(
        text,
        re.compile(
            r"(?i)\b(?:(?:oe|oem)\s+service information|service information\s+"
            r"(?:requirements?|links?|procedures?))\b"
        ),
    )


def _families_in(text: str) -> list[str]:
    return [
        family
        for family, aliases in _ALIAS_FAMILIES.items()
        if any(_contains_phrase(text, alias) for alias in aliases)
    ]


def _requirements_from_section(text: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    # U+FFFD (the generic Unicode replacement character): confirmed live --
    # this document's actual bullet glyph has no ToUnicode mapping either,
    # so it comes through as the same "unknown character" placeholder every
    # PDF text extractor falls back to, not one of the literal bullet
    # characters below. Without it, the requirement line under this bullet
    # (a genuinely required calibration, printed on the page) silently
    # never matched at all.
    # The PDF report's own bullet phrasing is "ADAS L3: Static Calibration -
    # A of ICC / Distance Sensor" -- the method/tier description prefixed
    # onto the actual component name with " of ", not the bare component
    # name the live ADAS Map modal itself shows. Confirmed live: without
    # stripping this prefix, _safe_requirement's own colon-rejecting filter
    # (correctly strict against section-header-looking text) discarded the
    # entire line, silently losing a real, genuinely-required calibration.
    _method_prefix_re = re.compile(
        r"(?i)^adas\s+l\d+\s*:\s*.+?\bof\s+(?P<component>.+)$"
    )

    for raw in re.findall(r"(?m)^\s*[•●▪*�-]\s*(?P<label>[^\r\n]{2,160})", text):
        label = re.sub(r"(?i)\s+estimate\s+lines?.*$", "", raw).strip(" .;:-")
        prefix_match = _method_prefix_re.match(label)
        if prefix_match:
            label = prefix_match.group("component").strip(" .;:-")
        safe = _safe_requirement(label)
        if not safe:
            continue
        family = _requirement_family(safe)
        key = family or _fold(safe)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "label": _FAMILY_LABELS.get(family, safe),
                "family": family,
                "source": "physical_adas_map_requirement_section",
            }
        )
    if result:
        return result
    for family in _families_in(text):
        result.append(
            {
                "label": _FAMILY_LABELS.get(family, family.replace("_", " ").title()),
                "family": family,
                "source": "physical_adas_map_requirement_section",
            }
        )
    return result


def _descriptor_vehicle(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Remove bounded system suffixes that the base filename parser predates."""

    model = " ".join(str(descriptor.get("model") or "").split())
    earliest: Optional[int] = None
    aliases = [alias for values in _ALIAS_FAMILIES.values() for alias in values]
    for alias in sorted(aliases, key=len, reverse=True):
        words = [re.escape(word) for word in alias.split()]
        match = re.search(
            r"(?i)(?<![a-z0-9])" + r"[\s_-]+".join(words) + r"(?![a-z0-9])",
            model,
        )
        if match and match.start() > 0:
            earliest = (
                match.start() if earliest is None else min(earliest, match.start())
            )
    if earliest is not None:
        model = model[:earliest].strip(" -_")
    return _vehicle(
        descriptor.get("year"),
        descriptor.get("make"),
        model,
        None,
    )


def _parse_report_text(text: str, filename_ro: Optional[str]) -> dict[str, Any]:
    # ADAS Map's print PDFs encode the common "ti" glyph as a NUL in several
    # headings (Inspection, Estimate, Calibration, Information) and retain
    # Unicode presentation ligatures such as ﬁ.  Repair only that observed,
    # deterministic extraction artifact before matching structured labels.
    text = unicodedata.normalize("NFKC", text.replace("\x00", "ti"))
    ro_value = _label_value(text, ("Repair Order", "RO Number", "RO"))
    ro_match = re.search(r"\d{6,}", ro_value or "")
    ro_number = ro_match.group(0) if ro_match else None
    if (
        ro_number is None
        and filename_ro
        and re.search(rf"(?<!\d){re.escape(filename_ro)}(?!\d)", text)
    ):
        ro_number = filename_ro

    vin_match = _VIN_RE.search(text.upper())
    year_raw = _label_value(text, ("Model Year", "Year"))
    year_match = re.search(r"(?:19|20)\d{2}", year_raw or "")
    make = _label_value(text, ("Vehicle Make", "Make"))
    if not make:
        folded = text.casefold()
        make = next(
            (
                known
                for known in KNOWN_MAKES
                if re.search(
                    rf"(?<![a-z]){re.escape(known.casefold())}(?![a-z])", folded
                )
            ),
            None,
        )
    model = _label_value(text, ("Vehicle Model", "Model"))
    inspection = _label_value(
        text, ("Inspection ID", "Inspection Number", "Inspection")
    )
    inspection_match = re.search(r"[A-Za-z0-9_-]{3,}", inspection or "")
    governing = _governing_section(text)
    oe = _oe_section(text)
    explicit_no_calibration = bool(
        re.search(
            r"(?i)\b(?:no calibrations?(?: (?:is|are))? required|no calibration required)\b",
            governing or text,
        )
    )
    return {
        "ro_number": ro_number,
        "vin": vin_match.group(0).upper() if vin_match else None,
        "year": int(year_match.group(0)) if year_match else None,
        "make": make,
        "model": model,
        "inspection_id": inspection_match.group(0) if inspection_match else None,
        "requirements": _requirements_from_section(governing),
        "explicit_no_calibration": explicit_no_calibration,
        "oe_requirement_families": _families_in(oe),
        "oe_section_text": oe,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ro_uri(path: Path) -> str:
    value = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{value}?mode=ro&immutable=1"


def _configuration(value: object) -> Optional[str]:
    if isinstance(value, dict):
        for key in (
            "adas_map_model_configuration",
            "model_configuration",
            "configuration",
            "label",
            "name",
        ):
            candidate = " ".join(str(value.get(key) or "").split()).strip()
            if candidate:
                return candidate
        return None
    return " ".join(str(value or "").split()).strip() or None


def _vehicle(
    year: object,
    make: object,
    model: object,
    trim: object = None,
    configuration: object = None,
) -> dict[str, Any]:
    try:
        parsed_year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        parsed_year = None
    return {
        "year": parsed_year,
        "make": " ".join(str(make or "").split()) or None,
        "model": " ".join(str(model or "").split()) or None,
        "trim": " ".join(str(trim or "").split()) or None,
        "configuration": _configuration(configuration),
    }


def _vehicle_complete(value: dict[str, Any]) -> bool:
    return bool(value.get("year") and value.get("make") and value.get("model"))


def _vehicle_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not (
        int(left.get("year") or 0) == int(right.get("year") or 0)
        and _compact(left.get("make")) == _compact(right.get("make"))
        and _compact(left.get("model")) == _compact(right.get("model"))
    ):
        return False
    for key in ("trim", "configuration"):
        left_value = _compact(left.get(key))
        right_value = _compact(right.get(key))
        if left_value and right_value and left_value != right_value:
            return False
    return True


def _vehicle_optional_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return any(
        (left_value := _compact(left.get(key)))
        and (right_value := _compact(right.get(key)))
        and left_value != right_value
        for key in ("trim", "configuration")
    )


@dataclass(frozen=True)
class DiscoveryQuery:
    ro_number: Optional[str] = None
    ciq_ro_id: Optional[str] = None
    vin: Optional[str] = None
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    configuration: Optional[str] = None
    inspection_id: Optional[str] = None

    def validate(self) -> None:
        if self.vin and not _valid_vin(self.vin):
            raise ValueError("vin must be a valid 17-character VIN")
        vehicle_parts = (self.year, self.make, self.model)
        if any(value not in (None, "") for value in vehicle_parts) and not all(
            value not in (None, "") for value in vehicle_parts
        ):
            raise ValueError("exact vehicle discovery requires year, make, and model")
        if not any(
            (
                self.ro_number,
                self.ciq_ro_id,
                self.vin,
                self.inspection_id,
                all(vehicle_parts),
            )
        ):
            raise ValueError(
                "exact RO, CIQ ID, VIN, inspection ID, or complete vehicle identity is required"
            )

    @property
    def vehicle(self) -> Optional[dict[str, Any]]:
        if self.year and self.make and self.model:
            return _vehicle(
                self.year,
                self.make,
                self.model,
                self.trim,
                self.configuration,
            )
        return None


class AdasArtifactCatalog:
    """Incremental physical-artifact index and exact provenance resolver."""

    def __init__(
        self,
        source_root: Path,
        cache_path: Path,
        scrapex_db_path: Optional[Path] = None,
    ) -> None:
        self.source_root = Path(source_root).resolve()
        self.cache_path = Path(cache_path).resolve()
        configured = os.getenv(SCRAPEX_DB_ENV)
        self.scrapex_db_path = Path(
            scrapex_db_path or configured or DEFAULT_SCRAPEX_DB
        ).resolve()

    # ------------------------------------------------------------------
    # Incremental physical index
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> bool:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS pages("
            " path TEXT, page INTEGER, text TEXT, source_mtime_ns INTEGER,"
            " PRIMARY KEY(path, page));"
            "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS artifact_catalog("
            " path TEXT PRIMARY KEY, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,"
            " size_bytes INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL,"
            " page_count INTEGER NOT NULL, readable INTEGER NOT NULL, present INTEGER NOT NULL,"
            " sidecar_mtime_ns INTEGER NOT NULL DEFAULT 0,"
            " artifact_kind TEXT NOT NULL, ro_number TEXT, ciq_ro_id TEXT, vin TEXT,"
            " year INTEGER, make TEXT, model TEXT, trim TEXT, configuration TEXT, inspection_id TEXT,"
            " identity_verified INTEGER NOT NULL, provenance_verified INTEGER NOT NULL,"
            " explicit_no_calibration INTEGER NOT NULL DEFAULT 0,"
            " requirements_json TEXT NOT NULL, oe_requirement_families_json TEXT NOT NULL,"
            " oe_section_text TEXT NOT NULL, text_content TEXT NOT NULL,"
            " source_json TEXT NOT NULL, parse_error TEXT, indexed_at TEXT NOT NULL);"
            "CREATE INDEX IF NOT EXISTS ix_artifact_catalog_ro ON artifact_catalog(ro_number);"
            "CREATE INDEX IF NOT EXISTS ix_artifact_catalog_ciq ON artifact_catalog(ciq_ro_id);"
            "CREATE INDEX IF NOT EXISTS ix_artifact_catalog_vin ON artifact_catalog(vin);"
        )
        existing_columns = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(artifact_catalog)").fetchall()
        }
        if "sidecar_mtime_ns" not in existing_columns:
            db.execute(
                "ALTER TABLE artifact_catalog ADD COLUMN sidecar_mtime_ns INTEGER NOT NULL DEFAULT 0"
            )
        if "explicit_no_calibration" not in existing_columns:
            db.execute(
                "ALTER TABLE artifact_catalog ADD COLUMN explicit_no_calibration INTEGER NOT NULL DEFAULT 0"
            )
        if "configuration" not in existing_columns:
            db.execute("ALTER TABLE artifact_catalog ADD COLUMN configuration TEXT")
        current = db.execute(
            "SELECT value FROM meta WHERE key='artifact_catalog_schema_version'"
        ).fetchone()
        reindex_all = current is None or str(current[0]) != CATALOG_SCHEMA_VERSION
        db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('artifact_catalog_schema_version',?)",
            (CATALOG_SCHEMA_VERSION,),
        )
        return reindex_all

    def _read_pdf_pages(self, path: Path) -> list[tuple[int, str]]:
        pages: Optional[list[tuple[int, str]]] = None
        if PdfReader is not None:
            try:
                reader = PdfReader(str(path), strict=False)
                pages = [
                    (number, str(page.extract_text() or "")[:250_000])
                    for number, page in enumerate(reader.pages, 1)
                ]
            except Exception:  # noqa: BLE001 - PDFium is the supported fallback
                if pdfium is None:
                    raise

        needs_pdfium = pages is None or not any(text.strip() for _, text in pages)
        if needs_pdfium and pdfium is not None:
            pypdf_pages = pages
            document = None
            try:
                document = pdfium.PdfDocument(str(path))
                extracted: list[tuple[int, str]] = []
                for index in range(len(document)):
                    page = document[index]
                    try:
                        text_page = page.get_textpage()
                        try:
                            text = text_page.get_text_range() or ""
                        finally:
                            text_page.close()
                    finally:
                        page.close()
                    extracted.append((index + 1, text[:250_000]))
                pages = extracted
            except Exception:  # noqa: BLE001 - preserve an honest pypdf scan result
                if pypdf_pages is None:
                    raise
                pages = pypdf_pages
            finally:
                if document is not None:
                    document.close()
        if pages is None:
            raise RuntimeError(
                "Neither pypdf nor pypdfium2 is installed; cannot read ADAS SI PDFs."
            )
        return [(number, _normalize_ligature_artifacts(text)) for number, text in pages]

    @staticmethod
    def _cached_pages(
        db: sqlite3.Connection, path: Path, mtime_ns: int
    ) -> list[tuple[int, str]]:
        rows = db.execute(
            "SELECT page,text FROM pages WHERE path=? AND source_mtime_ns=? ORDER BY page",
            (str(path), int(mtime_ns)),
        ).fetchall()
        if not rows:
            return []
        pages = [(int(row[0]), str(row[1] or "")) for row in rows]
        if any(text.strip() for _, text in pages):
            # Cached rows were written by whatever extraction ran when they
            # were first scanned -- confirmed live: 2,607 pages already
            # cached from before the ligature-artifact fix existed, so
            # reading them back verbatim would keep serving the same
            # corrupted text forever regardless of the fix. Normalize on
            # every read instead of requiring a full cache invalidation.
            return [(number, _normalize_ligature_artifacts(text)) for number, text in pages]
        # Existing OCR is part of the same cache.  Reuse it when native page
        # rows are present but empty; never initialize or run OCR implicitly.
        has_ocr = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ocr_pages'"
        ).fetchone()
        if not has_ocr:
            # A prior extractor may have cached only blank native-text pages.
            # Treat that as a cache miss when an artifact row is being
            # reprocessed (for example after an extractor contract bump) so
            # the supported PDFium fallback gets one chance to recover text.
            return []
        ocr_rows = db.execute(
            "SELECT page,text FROM ocr_pages WHERE path=? AND source_mtime_ns=? ORDER BY page",
            (str(path), int(mtime_ns)),
        ).fetchall()
        ocr = {int(row[0]): str(row[1] or "") for row in ocr_rows}
        merged = [(page, ocr.get(page) or text) for page, text in pages]
        if not any(text.strip() for _, text in merged):
            return []
        return [(number, _normalize_ligature_artifacts(text)) for number, text in merged]

    @staticmethod
    def _sidecar(path: Path, digest: str) -> tuple[dict[str, Any], bool, Optional[str]]:
        sidecar_path = path.with_name(f"{path.stem}.source.json")
        if not sidecar_path.is_file():
            return {}, False, None
        try:
            value = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            return {}, False, f"sidecar_{type(exc).__name__}"
        if not isinstance(value, dict):
            return {}, False, "sidecar_not_object"
        declared = str(value.get("saved_pdf_sha256") or "").strip().casefold()
        verified = bool(declared and declared == digest.casefold())
        error = "sidecar_hash_missing_or_mismatch" if not verified else None
        return value, verified, error

    def _index_one(
        self, db: sqlite3.Connection, path: Path, existing: Optional[sqlite3.Row]
    ) -> str:
        stat = path.stat()
        sidecar_path = path.with_name(f"{path.stem}.source.json")
        sidecar_mtime_ns = (
            sidecar_path.stat().st_mtime_ns if sidecar_path.is_file() else 0
        )
        if (
            existing is not None
            and int(existing["size_bytes"]) == stat.st_size
            and int(existing["source_mtime_ns"]) == stat.st_mtime_ns
            and int(existing["sidecar_mtime_ns"]) == sidecar_mtime_ns
            and int(existing["present"]) == 1
        ):
            return "unchanged"

        digest = _file_sha256(path)
        pages = self._cached_pages(db, path, stat.st_mtime_ns)
        extraction_error: Optional[str] = None
        if not pages:
            try:
                pages = self._read_pdf_pages(path)
            except Exception as exc:  # noqa: BLE001 - recorded as unverified
                pages = []
                extraction_error = f"{type(exc).__name__}: {exc}"
            if pages:
                db.execute("DELETE FROM pages WHERE path=?", (str(path),))
                db.executemany(
                    "INSERT OR REPLACE INTO pages(path,page,text,source_mtime_ns) VALUES(?,?,?,?)",
                    [(str(path), page, text, stat.st_mtime_ns) for page, text in pages],
                )
        text = "\n".join(value for _, value in pages)
        readable = bool(text.strip())

        descriptor = describe_document(self.source_root, path)
        file_match = _RO_FILE_RE.fullmatch(path.stem.strip())
        filename_ro = file_match.group("ro") if file_match else None
        # Parse every indexed document, not only the historical
        # ``<RO> adas map.pdf`` filename.  Older captures frequently kept a
        # portal-generated name; their embedded RO/VIN/inspection fields are
        # still authoritative evidence when the full report structure proves
        # the document is an ADAS Map result.
        report = _parse_report_text(text, filename_ro)
        sidecar, sidecar_verified, sidecar_error = self._sidecar(path, digest)
        trusted_sidecar = sidecar if sidecar_verified else {}
        sidecar_vehicle = (
            trusted_sidecar.get("vehicle")
            if isinstance(trusted_sidecar.get("vehicle"), dict)
            else {}
        )

        ro_number = (
            str(
                trusted_sidecar.get("ro_number")
                or trusted_sidecar.get("ro")
                or report.get("ro_number")
                or filename_ro
                or ""
            ).strip()
            or None
        )
        ciq_ro_id = (
            str(
                trusted_sidecar.get("ciq_ro_id") or trusted_sidecar.get("ro_id") or ""
            ).strip()
            or None
        )
        vin = (
            str(
                trusted_sidecar.get("vin")
                or sidecar_vehicle.get("vin")
                or report.get("vin")
                or ""
            )
            .strip()
            .upper()
            or None
        )
        descriptor_vehicle = _descriptor_vehicle(descriptor)
        vehicle = _vehicle(
            trusted_sidecar.get("year")
            or sidecar_vehicle.get("year")
            or report.get("year")
            or descriptor_vehicle.get("year"),
            trusted_sidecar.get("make")
            or sidecar_vehicle.get("make")
            or report.get("make")
            or descriptor_vehicle.get("make"),
            trusted_sidecar.get("model")
            or sidecar_vehicle.get("model")
            or report.get("model")
            or descriptor_vehicle.get("model"),
            trusted_sidecar.get("trim") or sidecar_vehicle.get("trim"),
            trusted_sidecar.get("configuration")
            or trusted_sidecar.get("model_configuration")
            or sidecar_vehicle.get("configuration")
            or sidecar_vehicle.get("model_configuration"),
        )
        inspection_id = (
            str(
                trusted_sidecar.get("inspection_id")
                or report.get("inspection_id")
                or ""
            ).strip()
            or None
        )
        requirements = report.get("requirements") if isinstance(report, dict) else []
        explicit_no_calibration = bool(
            isinstance(report, dict) and report.get("explicit_no_calibration") is True
        )
        content_map_identity = bool(
            re.search(r"(?i)\badas\s+map\b", text)
            and report.get("ro_number")
            and _valid_vin(vin)
            and _vehicle_complete(vehicle)
            and inspection_id
            and bool(requirements) != explicit_no_calibration
        )
        declared_kind = str(trusted_sidecar.get("artifact_kind") or "").casefold()
        if filename_ro or "adas_map" in declared_kind or content_map_identity:
            artifact_kind = "adas_map_report"
        else:
            artifact_kind = "service_information"

        identity_verified = bool(
            (filename_ro and ro_number == filename_ro and _valid_vin(vin))
            or content_map_identity
            or (
                _vehicle_complete(vehicle)
                and (sidecar_verified or descriptor.get("application_parsed"))
            )
        )
        oe_families = (
            report.get("oe_requirement_families") if isinstance(report, dict) else []
        )
        oe_text = (
            str(report.get("oe_section_text") or "") if isinstance(report, dict) else ""
        )
        errors = [value for value in (extraction_error, sidecar_error) if value]
        source = {
            "kind": "physical_pdf",
            "sidecar_present": bool(sidecar),
            "sidecar_verified": sidecar_verified,
            "provider": str(sidecar.get("provider") or "").strip() or None,
            "source_url": str(sidecar.get("source_url") or "").strip() or None,
        }
        db.execute(
            """INSERT OR REPLACE INTO artifact_catalog(
               path,relative_path,sha256,size_bytes,source_mtime_ns,page_count,
               readable,present,sidecar_mtime_ns,artifact_kind,ro_number,ciq_ro_id,vin,year,make,model,trim,configuration,
               inspection_id,identity_verified,provenance_verified,explicit_no_calibration,requirements_json,
               oe_requirement_families_json,oe_section_text,text_content,source_json,
               parse_error,indexed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(path),
                str(path.relative_to(self.source_root)).replace("\\", "/"),
                digest,
                stat.st_size,
                stat.st_mtime_ns,
                len(pages),
                int(readable),
                1,
                sidecar_mtime_ns,
                artifact_kind,
                ro_number,
                ciq_ro_id,
                vin if _valid_vin(vin) else None,
                vehicle.get("year"),
                vehicle.get("make"),
                vehicle.get("model"),
                vehicle.get("trim"),
                vehicle.get("configuration"),
                inspection_id,
                int(identity_verified),
                int(sidecar_verified),
                int(explicit_no_calibration),
                json.dumps(requirements or [], sort_keys=True),
                json.dumps(oe_families or [], sort_keys=True),
                oe_text,
                text,
                json.dumps(source, sort_keys=True),
                "; ".join(errors) or None,
                _utcnow(),
            ),
        )
        return "updated" if existing is not None else "added"

    def reconcile_index(self, *, max_seconds: Optional[float] = None) -> dict[str, Any]:
        """Reconcile physical PDFs into the derived cache; never alter sources.

        ``max_seconds`` bounds wall-clock time for an interactive caller: PDF
        text extraction and hashing for a never-before-seen document is slow
        (seconds per file), so an unbounded scan of a cold cache can run for
        minutes. When the budget runs out mid-scan, already-processed rows
        are committed and returned as a truthful partial result -- never as
        a silent hang or a false "complete" count. A warm cache (everything
        already indexed and unchanged) finishes almost immediately regardless
        of the budget, since unchanged files are a cheap stat-only check.
        """

        summary: dict[str, Any] = {
            "status": "success",
            "scan_complete": True,
            "physical_pdf_count": 0,
            "added": 0,
            "updated": 0,
            "unchanged": 0,
            "missing": 0,
            "unreadable": 0,
            "errors": [],
        }
        if not self.source_root.is_dir():
            return {
                **summary,
                "status": "unavailable",
                "scan_complete": False,
                "errors": ["ADAS SI source root is unavailable."],
            }
        paths = sorted(
            self.source_root.rglob("*.pdf"), key=lambda item: str(item).casefold()
        )
        summary["physical_pdf_count"] = len(paths)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        scanned = 0
        with closing(sqlite3.connect(self.cache_path)) as db:
            db.row_factory = sqlite3.Row
            reindex_all = self._ensure_schema(db)
            current_paths = {str(path) for path in paths}
            for path in paths:
                if deadline is not None and time.monotonic() >= deadline:
                    summary["scan_complete"] = False
                    summary["deadline_exceeded"] = True
                    break
                try:
                    existing = None
                    if not reindex_all:
                        existing = db.execute(
                            "SELECT size_bytes,source_mtime_ns,sidecar_mtime_ns,present "
                            "FROM artifact_catalog WHERE path=?",
                            (str(path),),
                        ).fetchone()
                    outcome = self._index_one(db, path, existing)
                    summary[outcome] += 1
                except Exception as exc:  # noqa: BLE001 - one source must not hide the rest
                    summary["scan_complete"] = False
                    summary["errors"].append(
                        {
                            "relative_path": str(path.relative_to(self.source_root)),
                            "error": type(exc).__name__,
                        }
                    )
                scanned += 1
            summary["scanned_this_call"] = scanned
            summary["unscanned_remaining"] = len(paths) - scanned
            rows = db.execute(
                "SELECT path FROM artifact_catalog WHERE present=1"
            ).fetchall()
            stale = [str(row[0]) for row in rows if str(row[0]) not in current_paths]
            if stale:
                db.executemany(
                    "UPDATE artifact_catalog SET present=0,indexed_at=? WHERE path=?",
                    [(_utcnow(), path) for path in stale],
                )
            summary["missing"] = len(stale)
            summary["unreadable"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM artifact_catalog WHERE present=1 AND readable=0"
                ).fetchone()[0]
            )
            db.commit()
        if summary["errors"]:
            summary["status"] = "partial_success"
        return summary

    def artifact_kind_summary(self, *, max_seconds: float = 4.0) -> dict[str, Any]:
        """Reconcile the physical index, then report counts by artifact kind.

        This is the deterministic answer to "how many ADAS Map reports/OE
        service documents are in the library" -- ``artifact_kind`` is decided
        by ``_index_one`` from filename identity, a verified sidecar, or a
        structural content match (never from a raw document count). A warm
        cache (the common case once anything -- this call, or the weekly
        Calibration IQ work-prep pass -- has indexed the library before)
        costs one cheap directory walk. ``max_seconds`` bounds a cold-cache
        first run so an interactive chat request can never hang for the
        minutes a from-scratch PDF text/hash pass over the whole library can
        take; an incomplete pass is reported honestly, not silently padded.
        """
        reconcile = self.reconcile_index(max_seconds=max_seconds)
        by_kind: dict[str, dict[str, int]] = {}
        with closing(sqlite3.connect(self.cache_path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT artifact_kind,"
                " COUNT(*) AS n,"
                " SUM(CASE WHEN identity_verified=1 THEN 1 ELSE 0 END) AS identity_verified_n"
                " FROM artifact_catalog WHERE present=1 GROUP BY artifact_kind"
            ).fetchall()
        for row in rows:
            by_kind[str(row["artifact_kind"])] = {
                "count": int(row["n"]),
                "identity_verified_count": int(row["identity_verified_n"] or 0),
            }
        classified_count = sum(item["count"] for item in by_kind.values())
        physical_pdf_count = int(reconcile.get("physical_pdf_count") or 0)
        scan_complete = bool(reconcile.get("scan_complete"))
        counts_are_final = bool(
            scan_complete and classified_count >= physical_pdf_count
        )
        return {
            "status": reconcile.get("status", "success"),
            "scan_complete": scan_complete,
            "counts_are_final": counts_are_final,
            "physical_pdf_count": physical_pdf_count,
            "classified_count": classified_count,
            "unscanned_remaining": int(reconcile.get("unscanned_remaining") or 0),
            "by_artifact_kind": by_kind,
            "unreadable_count": int(reconcile.get("unreadable") or 0),
            "reconcile_errors": reconcile.get("errors") or [],
            "evidence_contract": {
                "artifact_kind_is_a_content_classification_not_a_guess": True,
                "unreadable_documents_are_not_excluded_from_physical_pdf_count": True,
                "when_counts_are_final_is_false_the_by_artifact_kind_totals_are_"
                "a_partial_scan_and_must_be_reported_as_partial": True,
            },
        }

    # ------------------------------------------------------------------
    # ScrapeX canonical provenance (strictly read-only)
    # ------------------------------------------------------------------

    def _scrapex_rows(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        if not self.scrapex_db_path.is_file():
            return [], None
        db: Optional[sqlite3.Connection] = None
        try:
            db = sqlite3.connect(_ro_uri(self.scrapex_db_path), uri=True)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(items)").fetchall()
            }
            required = {
                "id",
                "ro_id",
                "ro_number",
                "vin",
                "year",
                "make",
                "model",
                "trim",
                "adas_map_contract_version",
                "adas_map_state",
                "adas_map_inspection_id",
                "adas_map_source_url",
                "adas_map_requirements_json",
                "adas_map_requirements_proven",
                "adas_map_raw_result_json",
                "ciq_reconciliation_state",
                "ciq_reconciliation_json",
            }
            if not required.issubset(columns):
                return [], "scrapex_schema_incomplete"
            rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id,ro_id,ro_number,vin,year,make,model,trim,
                       adas_map_contract_version,adas_map_state,adas_map_inspection_id,
                       adas_map_source_url,adas_map_requirements_json,
                       adas_map_requirements_proven,adas_map_raw_result_json,
                       ciq_reconciliation_state,
                       ciq_reconciliation_json,
                       adas_map_checked_at
                       FROM items"""
                ).fetchall()
            ]
            return rows, None
        except sqlite3.Error as exc:
            return [], f"scrapex_read_{type(exc).__name__}"
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _query_matches(record: dict[str, Any], query: DiscoveryQuery) -> bool:
        if query.ro_number and _fold(record.get("ro_number")) != _fold(query.ro_number):
            return False
        if (
            query.ciq_ro_id
            and str(record.get("ciq_ro_id") or record.get("ro_id") or "").casefold()
            != str(query.ciq_ro_id).casefold()
        ):
            return False
        if query.vin and str(record.get("vin") or "").upper() != str(query.vin).upper():
            return False
        if query.inspection_id and _fold(record.get("inspection_id")) != _fold(
            query.inspection_id
        ):
            return False
        if query.vehicle and not _vehicle_equal(
            record.get("vehicle") or {}, query.vehicle
        ):
            return False
        return True

    def _canonical_candidates(
        self, query: DiscoveryQuery
    ) -> tuple[list[dict[str, Any]], Optional[str]]:
        rows, read_error = self._scrapex_rows()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            # Poisoned legacy rows are not candidates for governing truth.
            if (
                int(row.get("adas_map_contract_version") or 0)
                != SCRAPEX_ADAS_MAP_CONTRACT_VERSION
            ):
                continue
            record = {
                "item_id": str(row.get("id") or ""),
                "ro_number": str(row.get("ro_number") or "").strip() or None,
                "ciq_ro_id": str(row.get("ro_id") or "").strip() or None,
                "ro_id": str(row.get("ro_id") or "").strip() or None,
                "vin": str(row.get("vin") or "").strip().upper() or None,
                "vehicle": _vehicle(
                    row.get("year"), row.get("make"), row.get("model"), row.get("trim")
                ),
                "inspection_id": str(row.get("adas_map_inspection_id") or "").strip()
                or None,
            }
            if not self._query_matches(record, query):
                continue

            requirements_raw = _json_load(row.get("adas_map_requirements_json"), [])
            requirements = _clean_canonical_requirements(requirements_raw)
            raw_result = _json_load(row.get("adas_map_raw_result_json"), {})
            raw_records = (
                raw_result.get("requirement_records")
                if isinstance(raw_result, dict)
                else None
            )
            raw_requirements = _clean_canonical_requirements(raw_records)
            reconciliation = _json_load(row.get("ciq_reconciliation_json"), {})
            explicit_none = bool(
                isinstance(raw_result, dict)
                and raw_result.get("explicit_no_calibration") is True
            )
            errors: list[str] = []
            map_state = str(row.get("adas_map_state") or "")
            if map_state not in _CANONICAL_MAP_EVIDENCE_STATES:
                errors.append("adas_map_evidence_state_invalid")
            if row.get("adas_map_requirements_proven") not in (1, True):
                errors.append("requirements_not_proven")
            if not isinstance(raw_result, dict):
                errors.append("raw_result_missing")
                raw_result = {}
            if (
                raw_result.get("success") is not True
                or str(raw_result.get("status") or "") != "complete"
            ):
                errors.append("raw_result_not_complete")
            if raw_result.get("requirements_proven") is not True:
                errors.append("raw_requirements_not_proven")
            modal_runtime_id = str(raw_result.get("modal_runtime_id") or "").strip()
            if not (
                raw_result.get("row_binding_confirmed") is True
                and raw_result.get("modal_inspection_confirmed") is True
                and raw_result.get("required_region_confirmed") is True
                and modal_runtime_id
            ):
                errors.append("map_structure_not_proven")
            if not explicit_none:
                fully_proven_records = bool(
                    isinstance(raw_records, list)
                    and raw_records
                    and all(
                        isinstance(value, dict)
                        and str(value.get("source") or "").strip().casefold()
                        == "adas_map_required_list_item"
                        and str(value.get("source_context") or "").strip().casefold()
                        == "selected_required_modal"
                        and "custom-link"
                        in str(value.get("source_control_class") or "")
                        .casefold()
                        .split()
                        and str(value.get("source_context_runtime_id") or "").strip()
                        == modal_runtime_id
                        and _safe_requirement(
                            value.get("label")
                            or value.get("calibration_type")
                            or value.get("name")
                        )
                        is not None
                        for value in raw_records
                    )
                )
                if not fully_proven_records:
                    errors.append("raw_requirement_provenance_invalid")
            elif raw_records or requirements_raw:
                errors.append("explicit_none_conflicts_with_requirements")
            if not requirements and not explicit_none:
                errors.append("structured_requirements_missing")
            db_requirement_keys = {
                (value["family"], value["source_context_runtime_id"])
                for value in requirements
            }
            raw_requirement_keys = {
                (value["family"], value["source_context_runtime_id"])
                for value in raw_requirements
            }
            if not explicit_none and db_requirement_keys != raw_requirement_keys:
                errors.append("stored_requirements_do_not_match_raw_result")
            if (
                not record["ro_number"]
                or not record["ciq_ro_id"]
                or not record["inspection_id"]
            ):
                errors.append("canonical_identity_incomplete")
            if not _valid_vin(record["vin"]) or not _vehicle_complete(
                record["vehicle"]
            ):
                errors.append("vehicle_identity_incomplete")
            source_url = str(row.get("adas_map_source_url") or "").strip()
            if not source_url:
                errors.append("source_url_missing")
            raw_inspection = str(raw_result.get("inspection_id") or "").strip()
            raw_source_url = str(
                raw_result.get("source_url") or raw_result.get("details_url") or ""
            ).strip()
            raw_ro_number = str(raw_result.get("ro_number") or "").strip()
            raw_ciq_ro_id = str(raw_result.get("ciq_ro_id") or "").strip()
            raw_vin = str(raw_result.get("vin") or "").strip().upper()
            raw_vehicle = (
                raw_result.get("vehicle")
                if isinstance(raw_result.get("vehicle"), dict)
                else {}
            )
            raw_configuration = _configuration(
                raw_vehicle.get("configuration")
                or raw_vehicle.get("model_configuration")
                or raw_result.get("model_configuration")
            )
            if raw_inspection != str(record["inspection_id"] or ""):
                errors.append("inspection_mismatch")
            if raw_source_url != source_url:
                errors.append("source_url_mismatch")
            if raw_ro_number != str(record["ro_number"] or ""):
                errors.append("ro_number_mismatch")
            if raw_ciq_ro_id != str(record["ciq_ro_id"] or ""):
                errors.append("ciq_ro_id_mismatch")
            if raw_vin != str(record["vin"] or "") or not _valid_vin(raw_vin):
                errors.append("vin_mismatch")
            if not _vehicle_complete(raw_vehicle) or not _vehicle_equal(
                raw_vehicle, record["vehicle"]
            ):
                errors.append("vehicle_identity_mismatch")
            raw_trim = _compact(raw_vehicle.get("trim"))
            stored_trim = _compact(record["vehicle"].get("trim"))
            if raw_trim and stored_trim and raw_trim != stored_trim:
                errors.append("vehicle_trim_mismatch")
            record["vehicle"]["configuration"] = raw_configuration
            attachment = (
                reconciliation.get("adas_map_attachment")
                if isinstance(reconciliation, dict)
                else None
            )
            ciq_verified = bool(
                isinstance(reconciliation, dict)
                and reconciliation.get("verified") is True
                and reconciliation.get("snapshot_verified") is True
                and isinstance(attachment, dict)
                and attachment.get("attached") is True
                and str(attachment.get("semantic_type") or "").strip().casefold()
                == "adas_map_report"
                and attachment.get("document_id")
            )
            if not ciq_verified:
                errors.append("ciq_adas_map_attachment_unverified")
            record.update(
                {
                    "requirements": requirements,
                    "explicit_no_calibration": explicit_none,
                    "verified": not errors,
                    "errors": errors,
                    "checked_at": str(row.get("adas_map_checked_at") or ""),
                    # Family-only, deliberately excluding source_context_runtime_id:
                    # that id is a per-scrape-session anti-spoofing nonce (proves
                    # every requirement in *this* record came from one live DOM
                    # modal), not a stable identity -- it differs on every session
                    # by design, so it cannot be used to compare records from
                    # different scrape sessions against each other.
                    "requirement_key_set": frozenset(
                        value["family"] for value in requirements
                    ),
                    "sources": [
                        {
                            "kind": "scrapex_canonical_v3",
                            "item_id": record["item_id"],
                            "inspection_id": record["inspection_id"],
                            "source_url": source_url or None,
                            "adas_map_state": map_state,
                            "requirements_proven": row.get(
                                "adas_map_requirements_proven"
                            )
                            in (1, True),
                            "ciq_reconciliation_state": str(
                                row.get("ciq_reconciliation_state") or ""
                            )
                            or None,
                            "ciq_reconciliation_verified": ciq_verified,
                            "ciq_receipt_count": (
                                reconciliation.get("receipt_count")
                                if isinstance(reconciliation, dict)
                                and isinstance(reconciliation.get("receipt_count"), int)
                                and not isinstance(
                                    reconciliation.get("receipt_count"), bool
                                )
                                else 0
                            ),
                        }
                    ],
                }
            )
            candidates.append(record)
        return candidates, read_error

    # ------------------------------------------------------------------
    # Discovery and requirement coverage
    # ------------------------------------------------------------------

    def _artifacts(self) -> list[dict[str, Any]]:
        if not self.cache_path.is_file():
            return []
        with closing(sqlite3.connect(self.cache_path)) as db:
            db.row_factory = sqlite3.Row
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_catalog'"
            ).fetchone()
            if not has_table:
                return []
            rows = [
                dict(row)
                for row in db.execute("SELECT * FROM artifact_catalog WHERE present=1")
            ]
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    **row,
                    "readable": bool(row.get("readable")),
                    "identity_verified": bool(row.get("identity_verified")),
                    "provenance_verified": bool(row.get("provenance_verified")),
                    "explicit_no_calibration": bool(row.get("explicit_no_calibration")),
                    "vehicle": _vehicle(
                        row.get("year"),
                        row.get("make"),
                        row.get("model"),
                        row.get("trim"),
                        row.get("configuration"),
                    ),
                    "requirements": _json_load(row.get("requirements_json"), []),
                    "oe_requirement_families": _json_load(
                        row.get("oe_requirement_families_json"), []
                    ),
                    "source": _json_load(row.get("source_json"), {}),
                }
            )
        return result

    @staticmethod
    def _artifact_source(row: dict[str, Any]) -> dict[str, Any]:
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        return {
            "kind": "physical_pdf",
            "artifact_kind": row.get("artifact_kind"),
            "relative_path": row.get("relative_path"),
            "sha256": row.get("sha256"),
            "readable": bool(row.get("readable")),
            "identity_verified": bool(row.get("identity_verified")),
            "sidecar_present": bool(source.get("sidecar_present")),
            "sidecar_verified": bool(source.get("sidecar_verified")),
            "provider": source.get("provider"),
            "source_url": source.get("source_url"),
        }

    @staticmethod
    def _artifact_matches(row: dict[str, Any], query: DiscoveryQuery) -> bool:
        record = {
            "ro_number": row.get("ro_number"),
            "ciq_ro_id": row.get("ciq_ro_id"),
            "vin": row.get("vin"),
            "vehicle": row.get("vehicle"),
            "inspection_id": row.get("inspection_id"),
        }
        return AdasArtifactCatalog._query_matches(record, query)

    @staticmethod
    def _artifact_conflicts(record: dict[str, Any], row: dict[str, Any]) -> bool:
        if (
            record.get("ro_number")
            and row.get("ro_number")
            and _fold(record["ro_number"]) != _fold(row["ro_number"])
        ):
            return True
        if (
            record.get("ciq_ro_id")
            and row.get("ciq_ro_id")
            and str(record["ciq_ro_id"]).casefold() != str(row["ciq_ro_id"]).casefold()
        ):
            return True
        if (
            record.get("inspection_id")
            and row.get("inspection_id")
            and _fold(record["inspection_id"]) != _fold(row["inspection_id"])
        ):
            return True
        if record.get("vin") and row.get("vin"):
            # A matching valid VIN is stronger than lossy model/configuration
            # text extracted from a printed report.  A VIN disagreement is a
            # hard conflict; an agreement binds the physical report exactly,
            # while explicit trim/configuration contradictions remain unsafe.
            if str(record["vin"]).upper() != str(row["vin"]).upper():
                return True
            return _vehicle_optional_conflict(
                record.get("vehicle") or {}, row.get("vehicle") or {}
            )
        if _vehicle_complete(record.get("vehicle") or {}) and _vehicle_complete(
            row.get("vehicle") or {}
        ):
            return not _vehicle_equal(record["vehicle"], row["vehicle"])
        return False

    @staticmethod
    def _fallback_record(row: dict[str, Any]) -> dict[str, Any]:
        explicit_none = row.get("explicit_no_calibration") is True
        requirements = row.get("requirements") or []
        requirements_proven = bool(requirements) != explicit_none
        verified = bool(
            row.get("artifact_kind") == "adas_map_report"
            and row.get("readable")
            and row.get("identity_verified")
            and row.get("ro_number")
            and _valid_vin(row.get("vin"))
            and _vehicle_complete(row.get("vehicle") or {})
            and row.get("inspection_id")
            and requirements_proven
        )
        errors: list[str] = []
        if not row.get("readable"):
            errors.append("artifact_unreadable")
        if not row.get("identity_verified") or not _vehicle_complete(
            row.get("vehicle") or {}
        ):
            errors.append("physical_identity_unverified")
        if not row.get("inspection_id"):
            errors.append("inspection_id_missing")
        if not requirements and not explicit_none:
            errors.append("governing_requirements_missing")
        if requirements and explicit_none:
            errors.append("explicit_none_conflicts_with_requirements")
        return {
            "ro_number": row.get("ro_number"),
            "ciq_ro_id": row.get("ciq_ro_id"),
            "vin": row.get("vin"),
            "vehicle": row.get("vehicle"),
            "inspection_id": row.get("inspection_id"),
            "requirements": requirements,
            "explicit_no_calibration": explicit_none,
            "verified": verified,
            "errors": errors,
            "sources": [AdasArtifactCatalog._artifact_source(row)],
        }

    def discover(
        self,
        *,
        ro_number: Optional[str] = None,
        ciq_ro_id: Optional[str] = None,
        vin: Optional[str] = None,
        year: Optional[int] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
        trim: Optional[str] = None,
        configuration: Optional[str] = None,
        inspection_id: Optional[str] = None,
    ) -> dict[str, Any]:
        query = DiscoveryQuery(
            ro_number=ro_number,
            ciq_ro_id=ciq_ro_id,
            vin=vin,
            year=year,
            make=make,
            model=model,
            trim=trim,
            configuration=configuration,
            inspection_id=inspection_id,
        )
        query.validate()
        index = self.reconcile_index()
        artifacts = self._artifacts()
        canonical, scrapex_error = self._canonical_candidates(query)

        if len(canonical) > 1:
            # Repeat successful scrapes of the same RO (ScrapeX re-verifies on
            # its own schedule) legitimately produce more than one fully
            # proven canonical row. That is corroborating evidence, not a
            # conflict -- only disagreement in substance (a different VIN or
            # a different requirement set) is a real identity conflict.
            # Unverified rows never resolve the pick; they just don't count.
            #
            # Requiring every verified row ever taken to agree is too strict:
            # ADAS Map genuinely adds requirements over time (a later scrape
            # legitimately finds more than an earlier one did), so an old,
            # now-superseded row would permanently block an otherwise-settled
            # RO. The safe resolution is corroboration, not unanimity: trust
            # the most recent verified signature only if some OTHER verified
            # row -- taken at a different time -- independently produced the
            # exact same signature. A lone, uncorroborated latest read stays
            # ambiguous rather than being trusted on its own.
            verified_candidates = [c for c in canonical if c.get("verified") is True]

            def _signature(c: dict[str, Any]) -> tuple[Any, Any, Any]:
                return (c.get("vin"), c.get("explicit_no_calibration"), c.get("requirement_key_set"))

            resolved: Optional[dict[str, Any]] = None
            if verified_candidates:
                by_recency = sorted(
                    verified_candidates, key=lambda c: c.get("checked_at") or "", reverse=True
                )
                latest = by_recency[0]
                latest_signature = _signature(latest)
                corroborated = any(
                    c is not latest and _signature(c) == latest_signature
                    for c in verified_candidates
                )
                if corroborated:
                    resolved = latest
            if resolved is not None:
                canonical = [resolved]
            else:
                return {
                    "status": DISCOVERY_AMBIGUOUS,
                    "query": query.__dict__,
                    "record": None,
                    "match_count": len(canonical),
                    "index": index,
                    "reason": (
                        "Multiple canonical ScrapeX v1 rows match the exact identity "
                        "and disagree on vehicle identity or requirements."
                        if verified_candidates
                        else "Multiple canonical ScrapeX v1 rows match the exact identity, "
                        "but none passed full provenance verification."
                    ),
                }

        if canonical:
            record = canonical[0]
            related = [
                row
                for row in artifacts
                if (
                    record.get("ro_number")
                    and _fold(row.get("ro_number")) == _fold(record.get("ro_number"))
                )
                or (
                    record.get("vin")
                    and str(row.get("vin") or "").upper()
                    == str(record.get("vin")).upper()
                )
                or (
                    record.get("ciq_ro_id")
                    and str(row.get("ciq_ro_id") or "").casefold()
                    == str(record.get("ciq_ro_id")).casefold()
                )
                or (
                    record.get("inspection_id")
                    and _fold(row.get("inspection_id"))
                    == _fold(record.get("inspection_id"))
                )
            ]
            if any(self._artifact_conflicts(record, row) for row in related):
                return {
                    "status": DISCOVERY_AMBIGUOUS,
                    "query": query.__dict__,
                    "record": None,
                    "match_count": 1 + len(related),
                    "index": index,
                    "reason": "A physical artifact conflicts with canonical ScrapeX identity.",
                }
            record["sources"].extend(self._artifact_source(row) for row in related)
            unreadable = [row for row in related if not row.get("readable")]
            if not record.get("verified") or unreadable:
                reason = (
                    "An exact associated physical artifact is unreadable."
                    if unreadable
                    else "The matching ScrapeX v1 row failed canonical provenance checks."
                )
                return {
                    "status": DISCOVERY_UNVERIFIED,
                    "query": query.__dict__,
                    "record": record,
                    "match_count": 1,
                    "index": index,
                    "reason": reason,
                }
            return {
                "status": DISCOVERY_VERIFIED,
                "query": query.__dict__,
                "record": record,
                "match_count": 1,
                "index": index,
                "reason": None,
            }

        physical = [row for row in artifacts if self._artifact_matches(row, query)]
        if query.ro_number:
            physical = [
                row for row in physical if row.get("artifact_kind") == "adas_map_report"
            ]
        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in physical:
            vehicle = row.get("vehicle") if isinstance(row.get("vehicle"), dict) else {}
            duplicate_key = (
                str(row.get("sha256") or "").casefold(),
                _fold(row.get("ro_number")),
                str(row.get("ciq_ro_id") or "").casefold(),
                str(row.get("vin") or "").upper(),
                int(vehicle.get("year") or 0),
                _compact(vehicle.get("make")),
                _compact(vehicle.get("model")),
                _compact(vehicle.get("trim")),
                _compact(vehicle.get("configuration")),
                _fold(row.get("inspection_id")),
                str(row.get("artifact_kind") or ""),
                bool(row.get("explicit_no_calibration")),
                json.dumps(row.get("requirements") or [], sort_keys=True),
            )
            deduplicated.setdefault(duplicate_key, row)
        physical = list(deduplicated.values())
        if len(physical) > 1:
            identities = {
                (
                    str(row.get("ro_number") or "").casefold(),
                    str(row.get("vin") or "").upper(),
                    int((row.get("vehicle") or {}).get("year") or 0),
                    _compact((row.get("vehicle") or {}).get("make")),
                    _compact((row.get("vehicle") or {}).get("model")),
                    _fold(row.get("inspection_id")),
                )
                for row in physical
            }
            if len(identities) > 1 or any(
                row.get("artifact_kind") == "adas_map_report" for row in physical
            ):
                return {
                    "status": DISCOVERY_AMBIGUOUS,
                    "query": query.__dict__,
                    "record": None,
                    "match_count": len(physical),
                    "index": index,
                    "reason": "Multiple physical artifacts match with non-unique provenance.",
                }
        if physical:
            if physical[0].get("artifact_kind") == "adas_map_report":
                record = self._fallback_record(physical[0])
            else:
                first = physical[0]
                verified = all(
                    row.get("readable") and row.get("identity_verified")
                    for row in physical
                )
                record = {
                    "ro_number": first.get("ro_number"),
                    "ciq_ro_id": first.get("ciq_ro_id"),
                    "vin": first.get("vin"),
                    "vehicle": first.get("vehicle"),
                    "inspection_id": first.get("inspection_id"),
                    "requirements": [],
                    "explicit_no_calibration": False,
                    "verified": verified,
                    "errors": [] if verified else ["physical_artifact_unverified"],
                    "sources": [self._artifact_source(row) for row in physical],
                }
            return {
                "status": DISCOVERY_VERIFIED
                if record["verified"]
                else DISCOVERY_UNVERIFIED,
                "query": query.__dict__,
                "record": record,
                "match_count": len(physical),
                "index": index,
                "reason": None
                if record["verified"]
                else "Physical artifact parsing or identity is incomplete.",
            }

        if scrapex_error or not index.get("scan_complete"):
            return {
                "status": DISCOVERY_UNVERIFIED,
                "query": query.__dict__,
                "record": None,
                "match_count": 0,
                "index": index,
                "reason": scrapex_error or "Physical artifact scan was incomplete.",
            }
        return {
            "status": DISCOVERY_NOT_FOUND,
            "query": query.__dict__,
            "record": None,
            "match_count": 0,
            "index": index,
            "reason": "Complete exact scan found no matching canonical or physical artifact.",
        }

    @staticmethod
    def _procedure_supports(row: dict[str, Any], requirement: str) -> bool:
        if not row.get("readable"):
            return False
        if row.get("artifact_kind") == "adas_map_report":
            family = _requirement_family(requirement)
            if family in set(row.get("oe_requirement_families") or []):
                return True
            oe_text = str(row.get("oe_section_text") or "")
            return bool(oe_text and _has_alias(oe_text, requirement))
        text = str(row.get("text_content") or "")
        return _has_alias(text, requirement) and any(
            _contains_phrase(text, marker) for marker in _PROCEDURE_MARKERS
        )

    def requirement_coverage(
        self,
        requirements: Iterable[str],
        *,
        ro_number: Optional[str] = None,
        ciq_ro_id: Optional[str] = None,
        vin: Optional[str] = None,
        year: Optional[int] = None,
        make: Optional[str] = None,
        model: Optional[str] = None,
        trim: Optional[str] = None,
        configuration: Optional[str] = None,
        inspection_id: Optional[str] = None,
    ) -> dict[str, Any]:
        labels = [
            label for value in requirements if (label := _safe_requirement(value))
        ]
        discovery = self.discover(
            ro_number=ro_number,
            ciq_ro_id=ciq_ro_id,
            vin=vin,
            year=year,
            make=make,
            model=model,
            trim=trim,
            configuration=configuration,
            inspection_id=inspection_id,
        )
        if discovery["status"] in {DISCOVERY_AMBIGUOUS, DISCOVERY_UNVERIFIED}:
            return {
                "status": UNVERIFIED,
                "discovery_status": discovery["status"],
                "requirements": [
                    {
                        "requirement": label,
                        "family": _requirement_family(label),
                        "state": UNVERIFIED,
                        "sources": [],
                    }
                    for label in labels
                ],
                "reason": discovery.get("reason"),
            }
        if discovery["status"] == DISCOVERY_NOT_FOUND:
            return {
                "status": MISSING,
                "discovery_status": DISCOVERY_NOT_FOUND,
                "requirements": [
                    {
                        "requirement": label,
                        "family": _requirement_family(label),
                        "state": MISSING,
                        "sources": [],
                    }
                    for label in labels
                ],
                "reason": "Complete exact scan found no matching artifact.",
            }

        record = discovery.get("record") or {}
        identity = (
            record.get("vehicle") if isinstance(record.get("vehicle"), dict) else None
        )
        query = DiscoveryQuery(
            ro_number=record.get("ro_number") or ro_number,
            ciq_ro_id=record.get("ciq_ro_id") or ciq_ro_id,
            vin=record.get("vin") or vin,
            year=(identity or {}).get("year") or year,
            make=(identity or {}).get("make") or make,
            model=(identity or {}).get("model") or model,
            trim=(identity or {}).get("trim") or trim,
            configuration=(identity or {}).get("configuration") or configuration,
            inspection_id=record.get("inspection_id") or inspection_id,
        )
        artifacts = self._artifacts()
        relevant: list[dict[str, Any]] = []
        conflicting: list[dict[str, Any]] = []
        for row in artifacts:
            row_ro = str(row.get("ro_number") or "").strip()
            row_ciq_id = str(row.get("ciq_ro_id") or "").strip()
            row_vin = str(row.get("vin") or "").strip().upper()
            row_inspection_id = str(row.get("inspection_id") or "").strip()
            ro_match = bool(
                query.ro_number and row_ro and _fold(row_ro) == _fold(query.ro_number)
            )
            ciq_match = bool(
                query.ciq_ro_id
                and row_ciq_id
                and row_ciq_id.casefold() == str(query.ciq_ro_id).casefold()
            )
            vin_match = bool(
                query.vin and row_vin and row_vin == str(query.vin).upper()
            )
            inspection_match = bool(
                query.inspection_id
                and row_inspection_id
                and _fold(row_inspection_id) == _fold(query.inspection_id)
            )
            vehicle_match = bool(
                query.vehicle
                and _vehicle_complete(row.get("vehicle") or {})
                and _vehicle_equal(row.get("vehicle") or {}, query.vehicle)
            )
            strong_match = ro_match or ciq_match or vin_match or inspection_match
            strong_conflict = bool(
                strong_match
                and (
                    (
                        query.ro_number
                        and row_ro
                        and _fold(row_ro) != _fold(query.ro_number)
                    )
                    or (
                        query.ciq_ro_id
                        and row_ciq_id
                        and row_ciq_id.casefold() != str(query.ciq_ro_id).casefold()
                    )
                    or (query.vin and row_vin and row_vin != str(query.vin).upper())
                    or (
                        query.inspection_id
                        and row_inspection_id
                        and _fold(row_inspection_id) != _fold(query.inspection_id)
                    )
                    or (
                        query.vehicle
                        and _vehicle_complete(row.get("vehicle") or {})
                        and (
                            _vehicle_optional_conflict(
                                row.get("vehicle") or {}, query.vehicle
                            )
                            or (not vin_match and not vehicle_match)
                        )
                    )
                )
            )
            if strong_conflict:
                conflicting.append(row)
                continue
            if strong_match:
                relevant.append(row)
                continue
            # Vehicle-only historical SI may participate when its optional
            # identity is compatible.  A document carrying another explicit
            # RO, CIQ ID, or VIN is for that other vehicle and cannot satisfy
            # this one merely because the base model text matches.
            explicit_other_identity = bool(
                (query.ro_number and row_ro)
                or (query.ciq_ro_id and row_ciq_id)
                or (query.vin and row_vin)
                or (query.inspection_id and row_inspection_id)
            )
            if vehicle_match and not explicit_other_identity:
                relevant.append(row)

        if conflicting:
            return {
                "status": UNVERIFIED,
                "discovery_status": discovery["status"],
                "requirements": [
                    {
                        "requirement": label,
                        "family": _requirement_family(label),
                        "state": UNVERIFIED,
                        "sources": [],
                    }
                    for label in labels
                ],
                "reason": "An artifact bound by RO, CIQ ID, or VIN has contradictory identity metadata.",
            }

        if any(not row.get("readable") for row in relevant):
            return {
                "status": UNVERIFIED,
                "discovery_status": discovery["status"],
                "requirements": [
                    {
                        "requirement": label,
                        "family": _requirement_family(label),
                        "state": UNVERIFIED,
                        "sources": [],
                    }
                    for label in labels
                ],
                "reason": "At least one exact-identity artifact is unreadable.",
            }

        outcomes: list[dict[str, Any]] = []
        scan_complete = (
            isinstance(discovery.get("index"), dict)
            and discovery["index"].get("scan_complete") is True
        )
        for label in labels:
            supporting = [
                row for row in relevant if self._procedure_supports(row, label)
            ]
            state = COVERED if supporting else MISSING if scan_complete else UNVERIFIED
            outcomes.append(
                {
                    "requirement": label,
                    "family": _requirement_family(label),
                    "state": state,
                    "sources": [self._artifact_source(row) for row in supporting],
                }
            )
        if all(row["state"] == COVERED for row in outcomes):
            overall = COVERED
        elif any(row["state"] == MISSING for row in outcomes):
            overall = MISSING
        else:
            overall = UNVERIFIED
        return {
            "status": overall,
            "discovery_status": discovery["status"],
            "requirements": outcomes,
            "reason": (
                None
                if overall == COVERED
                else "Complete exact scan found no qualifying OE procedure for one or more requirements."
                if overall == MISSING
                else "The physical artifact scan was incomplete, so absent procedure support is unverified."
            ),
        }


__all__ = [
    "AdasArtifactCatalog",
    "COVERED",
    "MISSING",
    "UNVERIFIED",
    "DISCOVERY_VERIFIED",
    "DISCOVERY_NOT_FOUND",
    "DISCOVERY_UNVERIFIED",
    "DISCOVERY_AMBIGUOUS",
]
