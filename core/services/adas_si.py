"""
X Omni -- ADAS SI source library.

Search, display, and maintain the ADAS SI calibration library.

This is Otis's own calibration knowledge base, not third-party reference
material, so X has full read/write/modify access to it. Where XV12 kept
originals strictly immutable, the rail here protects against accidents
rather than intent: every write is approval-gated, and any file that gets
overwritten is copied into `_xomni_backups/` first, so an edit to an
authoritative document is always reversible.

Structured annotations still live separately in `_xomni_managed/`, which
keeps Otis's notes distinguishable from OEM source material.

How search works, and why it isn't just full-text matching: vehicle
identity lives in the *filename* ("2021 Ford F-150 AWD Front Camera
Calibration.pdf"), while the procedure lives in the page text. Matching
on content alone returns the right words in the wrong vehicle's manual,
so document identity is scored first and dominates the ranking. Page
text is then scored with procedure-specific term weighting.

Extracted page text is cached in SQLite keyed on the file's mtime, so a
re-search is fast and an edited source re-extracts automatically.

Known limitation kept from XV12: pypdf is preferred for embedded text and
PDFium is the runtime fallback, but neither performs OCR. A scanned image-only
PDF therefore yields no text. That case is reported honestly as
`partial_success` -- "the document exists and matched, but no text could be
extracted" -- rather than as "no result", because telling a technician a
procedure doesn't exist when it does is the worst possible failure here.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import adas_storage

log = logging.getLogger("xomni.adas_si")

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - surfaced at call time instead
    PdfReader = None

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - page images degrade to the PDF link
    pdfium = None

# PDFium's C library is not safe for concurrent use across threads. A search
# runs several adas.search() calls in parallel (one per calibration
# requirement, via asyncio.to_thread), and the chat UI opens several page
# images at once -- both paths land here from different threads at the same
# time. Without this lock, concurrent PdfDocument access crashes the native
# library instead of raising a catchable Python exception, which is why a
# page could fail to render with no error ever reaching the log.
_PDFIUM_LOCK = threading.Lock()

MANAGED_DIRNAME = "_xomni_managed"
BACKUP_DIRNAME = "_xomni_backups"
MAX_PAGE_CHARS = 250_000
MAX_RESULTS = 8
MAX_MATCHED_DOCS = 5
EXCERPT_LEAD = 350
EXCERPT_LEN = 1800
CACHE_SCHEMA_VERSION = "3"

IGNORE_TOKENS = {
    "the", "for", "show", "find", "display", "procedure", "calibration",
    "calibrate", "system", "specific", "specifically", "please", "look",
    "lookup", "need", "want", "vehicle", "model", "how", "what", "with",
}

# Domain term weights. A page that says "calibration" is far more likely
# to be the procedure than one that merely mentions the component.
MARKER_WEIGHTS = (
    ("calibrat", 8), ("alignment", 7), ("adjustment", 6), ("azimuth", 3),
    ("elevation", 3), ("service function", 3), ("align ", 5), ("adjust ", 4),
    ("procedure", 4), ("target", 3), ("diagnostic", 2), ("scan tool", 2),
    ("learn", 2), ("aim", 2), ("horizontal", 2), ("vertical", 2),
)

KNOWN_MAKES = sorted(
    [
        "Acura", "Alfa Romeo", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet",
        "Chrysler", "Dodge", "Fiat", "Ford", "Genesis", "GMC", "Honda", "Hyundai",
        "Infiniti", "Jaguar", "Jeep", "Kia", "Land Rover", "Lexus", "Lincoln",
        "Mazda", "Mercedes-Benz", "Mercury", "Mini", "Mitsubishi", "Nissan",
        "Polestar", "Pontiac", "Porsche", "Ram", "Rivian", "Saab", "Subaru",
        "Tesla", "Toyota", "Volkswagen", "Volvo",
    ],
    key=len,
    reverse=True,
)
MAKE_ALIASES = {"chevy": "Chevrolet", "mercedes benz": "Mercedes-Benz", "vw": "Volkswagen"}
MODEL_MAKE_HINTS = {"forester": "Subaru", "outback": "Subaru", "f-150": "Ford"}
BODY_PREFIXES = {"truck", "car", "suv", "crossover", "van"}

YEAR_RE = re.compile(r"^((?:19|20)\d{2})\s+(.+)$")
DRIVETRAIN_RE = re.compile(r"\b(AWD|FWD|RWD|4WD|2WD|4X4)\b", re.IGNORECASE)
PLATFORM_RE = re.compile(r"\(([^()]{1,24})\)")
# ADAS abbreviations such as BSM/BSD/IPMA/CCM are topic markers, not part of a
# vehicle model.  Recognizing them here keeps filenames such as
# "2021 Jeep Cherokee BSM Calibration.pdf" indexed as model=\"Cherokee\".
#
# Filenames abbreviate inconsistently ("Prk Sens", "prk assit", "Millimeterwave
# radar"). An unrecognized topic word doesn't just fail to score a topic bonus
# -- it falls through to the "no boundary found" branch below, which folds the
# whole remainder (topic word included) into the model, breaking the model
# match for every other bonus too. Every real variant seen in the library
# needs an entry here, or that one document silently loses to less relevant
# ones that happen to use a spelled-out word already on this list.
TOPIC_RE = re.compile(
    r"\b(front camera|forward camera|forward facing camera|rear camera|rearview camera|"
    r"surround view|360|"
    r"blind spot|bsm|bsd|eyesight|ipma|ccm|acc|lkas|adaptive cruise|lane keep|lane departure|"
    r"parking sensor|prk sens|parking assist|prk assit|prk asst|park assist|"
    r"millimeter\s*wave\s*radar|mm[\s-]?wave radar|"
    r"night vision|radar|lidar|windshield|calibration|alignment|abs|airbag)\b",
    re.IGNORECASE,
)

# Canonical labels for topic variants whose literal filename text would never
# appear in a spelled-out query (e.g. a technician asking for "parking sensor
# calibration" will never type "prk sens"). Only entries whose raw captured
# text needs normalizing are listed; everything else keeps its title-cased
# capture as before.
TOPIC_CANONICAL = {
    "Prk Sens": "Parking Sensor",
    "Prk Assit": "Park Assist",
    "Prk Asst": "Park Assist",
    "Parking Assist": "Park Assist",
    "Millimeterwave Radar": "Radar",
    "Millimeter Wave Radar": "Radar",
    "Mm Wave Radar": "Radar",
    "Mm-Wave Radar": "Radar",
    "Rearview Camera": "Rear Camera",
    "360": "360 System",
    "Acc": "Adaptive Cruise",
    "Lkas": "Lane Keep",
}
SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ==========================================================================
# document identity, parsed from filenames
# ==========================================================================

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("_", " ")).strip()


def _canonical_model(text: str) -> str:
    out = re.sub(r"\bF\s?(\d{3})\b", r"F-\1", text, flags=re.IGNORECASE)
    words = []
    for word in out.split():
        if word.isupper() and not any(c.isdigit() for c in word) and len(word) > 3:
            words.append(word.title())
        else:
            words.append(word)
    return " ".join(words)


def _split_make(body: str) -> tuple[Optional[str], str]:
    folded = body.casefold()
    for alias, canonical in MAKE_ALIASES.items():
        if folded.startswith(alias + " "):
            return canonical, body[len(alias):].strip()
    for make in KNOWN_MAKES:
        if folded.startswith(make.casefold() + " "):
            return make, body[len(make):].strip()
    for hint, make in MODEL_MAKE_HINTS.items():
        if hint in folded:
            return make, body
    parts = body.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, body


def describe_document(source_root: Path, path: Path) -> dict[str, Any]:
    """Vehicle identity from the filename. Contents are never opened here."""
    title = _clean(path.stem)
    try:
        relative = str(path.relative_to(source_root))
    except ValueError:
        relative = path.name
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    descriptor: dict[str, Any] = {
        "title": title, "relative_path": relative, "size_bytes": size,
        "year": None, "make": None, "model": None, "drivetrain": None,
        "platform_code": None, "topic": None,
        "application_parsed": False, "parse_confidence": "none",
    }
    path_identity = adas_storage.canonical_vehicle_identity(source_root, path)

    def _finish() -> dict[str, Any]:
        # The Year/Make/Model directory is the canonical identity contract for
        # newly captured SI. Filenames remain useful for topic parsing only.
        if path_identity is not None:
            descriptor["year"] = path_identity["year"]
            descriptor["make"] = path_identity["make"]
            descriptor["model"] = path_identity["model"]
            descriptor["application_parsed"] = True
            descriptor["parse_confidence"] = "path"
        return descriptor

    match = YEAR_RE.match(title)
    if not match:
        return _finish()
    descriptor["year"] = int(match.group(1))
    body = match.group(2).strip()

    make, remainder = _split_make(body)
    descriptor["make"] = make
    if not make or not remainder:
        descriptor["parse_confidence"] = "low"
        return _finish()

    words = remainder.split()
    while words and words[0].casefold() in BODY_PREFIXES:
        words.pop(0)
    remainder = " ".join(words)

    topic_m = TOPIC_RE.search(remainder)
    drive_m = DRIVETRAIN_RE.search(remainder)
    plat_m = PLATFORM_RE.search(remainder)
    if topic_m:
        raw_topic = _clean(topic_m.group(1)).title()
        descriptor["topic"] = TOPIC_CANONICAL.get(raw_topic, raw_topic)
    if drive_m:
        descriptor["drivetrain"] = drive_m.group(1).upper()
    if plat_m:
        descriptor["platform_code"] = _clean(plat_m.group(1))

    bounds = [m.start() for m in (topic_m, drive_m, plat_m) if m]
    model = remainder[: min(bounds)].strip() if bounds else remainder.strip()
    model = _canonical_model(_clean(model))
    if model:
        descriptor["model"] = model
        descriptor["application_parsed"] = True
        descriptor["parse_confidence"] = (
            "high" if (descriptor["topic"] or descriptor["drivetrain"]
                       or descriptor["platform_code"]) else "medium"
        )
    else:
        descriptor["parse_confidence"] = "low"
    return _finish()


class SourceInventory:
    """Enumerates the PDF library. Caches the walk, unlike XV12 which
    re-walked the whole tree on every single call."""

    def __init__(self, source_root: Path):
        self.source_root = Path(source_root).resolve()
        self._cache: Optional[list[dict]] = None
        self._cache_key: Optional[tuple] = None

    def available(self) -> bool:
        return self.source_root.is_dir()

    def _walk(self) -> list[Path]:
        if not self.available():
            return []
        return sorted(self.source_root.rglob("*.pdf"), key=lambda p: str(p).casefold())

    def documents(self) -> list[dict]:
        paths = self._walk()
        key = tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in paths)
        if self._cache is not None and key == self._cache_key:
            return self._cache
        docs = []
        for p in paths:
            d = describe_document(self.source_root, p)
            d["_path"] = p
            docs.append(d)
        self._cache, self._cache_key = docs, key
        return docs

    def matching_documents(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        """Score documents by identity match. Returns {score, path, descriptor}."""
        tokens = [
            t for t in re.findall(r"[a-z0-9\-]+", query.casefold())
            if len(t) > 1 and t not in IGNORE_TOKENS
        ]
        folded_query = query.casefold()
        # A repair-order number is long and unique enough that finding it
        # verbatim in a filename is unambiguous identity -- unlike an ADAS
        # SI procedure PDF, an ADAS Map coverage report is filed under its RO
        # number ("2400911731 ADAS Map.pdf") rather than a vehicle
        # description, so it never parses a year/make/model and would
        # otherwise never outscore an unrelated document that merely shares
        # a few common words.
        ro_number_tokens = [t for t in tokens if t.isdigit() and len(t) >= 6]
        scored = []
        for doc in self.documents():
            title_folded = doc["title"].casefold()
            score = sum(1 for t in tokens if t in title_folded)
            if doc.get("year") and str(doc["year"]) in folded_query:
                score += 4
            if doc.get("make") and doc["make"].casefold() in folded_query:
                score += 4
            if doc.get("model") and doc["model"].casefold() in folded_query:
                score += 7
            if doc.get("topic") and doc["topic"].casefold() in folded_query:
                score += 6
            if doc.get("drivetrain") and doc["drivetrain"].casefold() in folded_query:
                score += 2
            if any(ro_token in title_folded for ro_token in ro_number_tokens):
                score += 15
            if title_folded and title_folded in folded_query:
                score += 12
            if score > 0:
                scored.append({"score": score, "path": doc["_path"],
                               "descriptor": {k: v for k, v in doc.items() if k != "_path"}})
        scored.sort(key=lambda i: (-i["score"], i["descriptor"]["title"].casefold()))
        return scored[: max(1, min(limit, 25))]

    def snapshot(self) -> dict[str, Any]:
        if not self.available():
            return {
                "status": "unavailable", "authoritative_path": str(self.source_root),
                "documents": [], "applications": [],
                "summary": {"document_count": 0, "vehicle_application_count": 0},
                "message": "The ADAS SI source library is not reachable at "
                           f"{self.source_root}.",
            }
        docs = [{k: v for k, v in d.items() if k != "_path"} for d in self.documents()]
        parsed = [d for d in docs if d["application_parsed"]]
        unparsed = [d["title"] for d in docs if not d["application_parsed"]]

        groups: dict[tuple, list[dict]] = {}
        for d in parsed:
            groups.setdefault((d["year"], d["make"], d["model"]), []).append(d)

        applications = []
        for (year, make, model), supporting in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1].casefold(), kv[0][2].casefold())
        ):
            applications.append({
                "year": year, "make": make, "model": model,
                "document_count": len(supporting),
                "drivetrains": sorted({d["drivetrain"] for d in supporting if d["drivetrain"]}),
                "platform_codes": sorted({d["platform_code"] for d in supporting if d["platform_code"]}),
                "topics": sorted({d["topic"] for d in supporting if d["topic"]}, key=str.casefold),
                "source_documents": [d["title"] for d in supporting],
            })

        # Field order matters here, not just content: a downstream transport
        # bound (loop.py's model-facing tool feed) can truncate a large
        # payload. Small, safety-critical fields (the evidence contract, the
        # per-vehicle rollup) are ordered before the large per-document list
        # so they survive even when "documents" itself gets cut.
        return {
            "status": "success",
            "authoritative_path": str(self.source_root),
            "summary": {
                "document_count": len(docs),
                "vehicle_application_count": len(applications),
                "parsed_document_count": len(docs) - len(unparsed),
                "unparsed_document_count": len(unparsed),
            },
            "evidence_contract": {
                "authoritative_records_only": True,
                "do_not_infer_records_from_counts": True,
            },
            "applications": applications,
            "unparsed_documents": unparsed,
            "documents": docs,
        }


# ==========================================================================
# page text cache + search
# ==========================================================================

_shared_instances: dict[tuple[str, str], "AdasSI"] = {}
_shared_instances_lock = threading.Lock()


def get_shared_instance(source_root: Path, cache_path: Path) -> "AdasSI":
    """Return one memoized `AdasSI` per (source_root, cache_path) pair.

    Building an `AdasSI` walks the entire source tree and opens a schema
    check against the cache. Callers that would otherwise construct a fresh
    instance per tool invocation (rather than once at process startup)
    should go through this factory instead, so the walk only happens once.
    """

    key = (str(Path(source_root).resolve()), str(Path(cache_path).resolve()))
    with _shared_instances_lock:
        instance = _shared_instances.get(key)
        if instance is None:
            instance = AdasSI(source_root, cache_path)
            _shared_instances[key] = instance
        return instance


class AdasSI:
    def __init__(self, source_root: Path, cache_path: Path):
        self.source_root = Path(source_root).resolve()
        self.cache_path = Path(cache_path).resolve()
        self.storage_migration = (
            adas_storage.migrate_library_once(
                self.source_root,
                self.cache_path,
                describe_document,
            )
            if adas_storage.is_authoritative_runtime_root(self.source_root)
            else {
                "moved": 0,
                "unresolved": [],
                "paths": {},
                "skipped_non_authoritative_root": True,
            }
        )
        self.managed_root = self.source_root / MANAGED_DIRNAME
        self.inventory = SourceInventory(self.source_root)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS pages("
                "  path TEXT, page INTEGER, text TEXT, source_mtime_ns INTEGER,"
                "  PRIMARY KEY(path, page));"
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
            )
            current = db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if current is None or str(current[0]) != CACHE_SCHEMA_VERSION:
                db.execute("DELETE FROM pages")
            db.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (CACHE_SCHEMA_VERSION,),
            )

    def available(self) -> bool:
        return self.inventory.available()

    # ---------- page extraction ----------

    def _pages(self, path: Path) -> list[tuple[int, str]]:
        """Cached page text, keyed on mtime so an edited source re-extracts."""
        mtime = path.stat().st_mtime_ns
        with sqlite3.connect(self.cache_path) as db:
            cached = db.execute(
                "SELECT page, text FROM pages WHERE path=? AND source_mtime_ns=? ORDER BY page",
                (str(path), mtime),
            ).fetchall()
        if cached:
            return [(int(p), str(t)) for p, t in cached]

        pages: Optional[list[tuple[int, str]]] = None
        if PdfReader is not None:
            try:
                reader = PdfReader(str(path), strict=False)
                pages = [
                    (n, (page.extract_text() or "")[:MAX_PAGE_CHARS])
                    for n, page in enumerate(reader.pages, 1)
                ]
            except Exception as exc:  # noqa: BLE001 - PDFium is the supported fallback
                if pdfium is None:
                    raise
                log.warning(
                    "ADAS SI: pypdf extraction failed for %s; trying PDFium: %s",
                    path.name,
                    type(exc).__name__,
                )

        # PDFium is already the supported Windows runtime used for inline page
        # rendering. Use it when pypdf is unavailable, raises, or yields no
        # embedded text. If pypdf successfully identified a scan and PDFium
        # itself fails, retain the honest empty-page result instead of turning
        # a displayable matched document into an unreadable error.
        needs_pdfium = pages is None or not any(text.strip() for _, text in pages)
        if needs_pdfium and pdfium is not None:
            pypdf_pages = pages
            document = None
            try:
                with _PDFIUM_LOCK:
                    document = pdfium.PdfDocument(str(path))
                    pdfium_pages: list[tuple[int, str]] = []
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
                        pdfium_pages.append((index + 1, text[:MAX_PAGE_CHARS]))
                pages = pdfium_pages
            except Exception as exc:  # noqa: BLE001 - preserve a valid pypdf scan result
                if pypdf_pages is None:
                    raise
                pages = pypdf_pages
                log.warning(
                    "ADAS SI: PDFium fallback failed for %s: %s",
                    path.name,
                    type(exc).__name__,
                )
            finally:
                if document is not None:
                    document.close()
        if pages is None:
            raise RuntimeError(
                "Neither pypdf nor pypdfium2 is installed; cannot read ADAS SI PDFs."
            )
        with sqlite3.connect(self.cache_path) as db:
            db.execute("DELETE FROM pages WHERE path=?", (str(path),))
            db.executemany(
                "INSERT INTO pages(path, page, text, source_mtime_ns) VALUES(?,?,?,?)",
                [(str(path), n, t, mtime) for n, t in pages],
            )
        return pages

    @staticmethod
    def _marker_score(text: str) -> int:
        folded = text.casefold()
        return sum(weight for term, weight in MARKER_WEIGHTS if term in folded)

    # ---------- public tools ----------

    def inventory_read(self, _args: dict | None = None) -> dict[str, Any]:
        snap = self.inventory.snapshot()
        snap["managed_path"] = str(self.managed_root)
        snap["cache_path"] = str(self.cache_path)
        if snap.get("status") != "success":
            return snap

        # Rebuild in explicit key order rather than appending: the classifier
        # summary is exactly the kind of small, high-value fact that a
        # downstream transport-size truncation must not be allowed to cut
        # before it reaches the model, so it belongs ahead of the large
        # "documents" list, not after it.
        ordered: dict[str, Any] = {}
        for key in ("status", "authoritative_path", "summary", "evidence_contract"):
            if key in snap:
                ordered[key] = snap[key]
        ordered["artifact_kind_summary"] = self._artifact_kind_summary()
        for key, value in snap.items():
            if key not in ordered:
                ordered[key] = value
        return ordered

    def _artifact_kind_summary(self) -> dict[str, Any]:
        """Deterministic ADAS Map vs. OE-service-information counts.

        Deferred import: adas_artifact_catalog imports from this module, so
        importing it at module scope here would be circular. A classifier
        failure (e.g. the ScrapeX/pypdf runtime is unavailable) must not take
        down the whole inventory read -- it degrades to an honest status
        instead of a hard error on a read-only reporting tool.
        """
        try:
            from . import adas_artifact_catalog
        except ImportError as exc:
            return {"status": "unavailable", "message": f"{type(exc).__name__}: {exc}"}
        try:
            catalog = adas_artifact_catalog.AdasArtifactCatalog(
                self.source_root, self.cache_path
            )
            return catalog.artifact_kind_summary()
        except Exception as exc:  # noqa: BLE001 - never break inventory_read on this
            return {"status": "unavailable", "message": f"{type(exc).__name__}: {exc}"}

    def model_search(self, args: dict) -> dict[str, Any]:
        """Search from model-supplied structured automotive semantics.

        The conversation model decides the vehicle, repair event, component,
        and evidence depth.  This adapter only validates those structured
        decisions and serializes them for the existing local document index;
        it never classifies the user's original wording or chooses a route.
        """

        if not isinstance(args, dict):
            raise ValueError("structured ADAS SI search arguments are required")
        allowed = {
            "vehicle",
            "system",
            "component",
            "repair_event",
            "requirement_type",
            "question",
            "search_mode",
        }
        unknown = sorted(str(key) for key in args if key not in allowed)
        if unknown:
            raise ValueError(f"unsupported ADAS SI search fields: {', '.join(unknown)}")

        vehicle = args.get("vehicle") or {}
        if not isinstance(vehicle, dict):
            raise ValueError("vehicle must be an object")
        vehicle_allowed = {"year", "make", "model", "trim", "platform"}
        vehicle_unknown = sorted(
            str(key) for key in vehicle if key not in vehicle_allowed
        )
        if vehicle_unknown:
            raise ValueError(
                f"unsupported vehicle fields: {', '.join(vehicle_unknown)}"
            )

        structured: dict[str, Any] = {}
        query_parts: list[str] = []
        raw_year = vehicle.get("year")
        if raw_year not in (None, ""):
            if isinstance(raw_year, bool):
                raise ValueError("vehicle.year must be a four-digit year")
            try:
                year = int(raw_year)
            except (TypeError, ValueError) as exc:
                raise ValueError("vehicle.year must be a four-digit year") from exc
            if year < 1900 or year > 2100:
                raise ValueError("vehicle.year must be between 1900 and 2100")
            structured.setdefault("vehicle", {})["year"] = year
            query_parts.append(str(year))

        for key in ("make", "model", "trim", "platform"):
            raw_value = vehicle.get(key)
            if raw_value in (None, ""):
                continue
            value = " ".join(str(raw_value).split()).strip()
            if not value or len(value) > 160:
                raise ValueError(f"vehicle.{key} must be 1 through 160 characters")
            structured.setdefault("vehicle", {})[key] = value
            query_parts.append(value)

        for key in (
            "system",
            "component",
            "repair_event",
            "requirement_type",
            "question",
        ):
            raw_value = args.get(key)
            if raw_value in (None, ""):
                continue
            value = " ".join(str(raw_value).split()).strip()
            if not value or len(value) > 500:
                raise ValueError(f"{key} must be 1 through 500 characters")
            structured[key] = value
            query_parts.append(value)

        mode = str(args.get("search_mode") or "standard").strip()
        if mode not in {"standard", "calibration_requirements"}:
            raise ValueError(
                "search_mode must be standard or calibration_requirements"
            )
        structured["search_mode"] = mode
        if not query_parts:
            raise ValueError(
                "supply at least one vehicle, system, component, repair event, "
                "requirement type, or question field"
            )

        result = self.search(
            {"query": " ".join(query_parts)[:2_000], "search_mode": mode}
        )
        if isinstance(result, dict):
            result = dict(result)
            result["structured_query"] = structured
        return result

    def search(self, args: dict) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")

        if not self.available():
            return {
                "status": "unavailable", "query": query, "results": [],
                "matched_documents": [], "source": "ADAS SI",
                "message": f"The ADAS SI source library is not reachable at {self.source_root}.",
            }

        candidates = self.inventory.matching_documents(query)
        if not candidates:
            return {
                "status": "no_result", "query": query, "results": [],
                "matched_documents": [], "source": "ADAS SI",
                "message": "No ADAS SI document matched that vehicle or topic.",
            }

        content_tokens = [
            t for t in re.findall(r"[a-z0-9\-]+", query.casefold())
            if len(t) > 2 and t not in IGNORE_TOKENS
        ]

        results: list[dict] = []
        strongest = candidates[0]
        exact_source_matched = int(strongest.get("score") or 0) >= 10

        for candidate in candidates:
            path: Path = candidate["path"]
            descriptor = candidate["descriptor"]
            filename_score = int(candidate["score"])
            try:
                pages = self._pages(path)
            except Exception as exc:  # noqa: BLE001 - one bad PDF must not sink the search
                log.warning("ADAS SI: could not read %s: %s", path.name, exc)
                results.append({
                    "source": path.name, "status": "unreadable",
                    "error": type(exc).__name__, "source_match_score": filename_score,
                    "match_score": 0,
                })
                continue

            for page_number, text in pages:
                if not text:
                    continue
                folded = text.casefold()
                lexical = sum(min(folded.count(t), 3) for t in content_tokens)
                marker = self._marker_score(text)
                score = (lexical * 4) + marker
                if filename_score >= 10 and marker > 0:
                    score += min(filename_score // 3, 8)
                elif filename_score >= 10 and lexical > 0:
                    score += min(filename_score // 4, 6)

                threshold = 2 if filename_score >= 10 else max(
                    2, min(4, max(1, len(content_tokens)) // 2)
                )
                if score < threshold:
                    continue

                positions = [folded.find(t) for t in content_tokens if folded.find(t) >= 0]
                start = max(0, (min(positions) if positions else 0) - EXCERPT_LEAD)
                relative = self.relative_of(path)
                results.append({
                    "source": path.name,
                    "title": path.stem,
                    "page": page_number,
                    "relative_path": relative,
                    "url": f"/api/adas-si/document?path={quote(relative)}",
                    "excerpt": text[start:start + EXCERPT_LEN].strip(),
                    "match_score": score,
                    "source_match_score": filename_score,
                    "vehicle": {
                        k: descriptor[k]
                        for k in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                        if descriptor.get(k) is not None
                    },
                })

        # Identity first, then content: the right words in the wrong
        # vehicle's manual are worse than useless in the field.
        results.sort(
            key=lambda i: (int(i.get("source_match_score", 0)), int(i.get("match_score", 0))),
            reverse=True,
        )
        results = results[:MAX_RESULTS]
        has_text_hit = any(r.get("excerpt") for r in results)

        matched_documents = [
            {
                "title": c["descriptor"]["title"],
                "source": c["path"].name,
                "relative_path": self.relative_of(c["path"]),
                "url": f"/api/adas-si/document?path={quote(self.relative_of(c['path']))}",
                "pages_total": self.page_count(c["path"]),
                "source_match_score": c["score"],
                **{k: c["descriptor"].get(k)
                   for k in ("year", "make", "model", "drivetrain", "platform_code", "topic")},
            }
            for c in candidates[:MAX_MATCHED_DOCS]
        ]

        if has_text_hit:
            status_value, message = "success", None
        elif exact_source_matched:
            status_value = "partial_success"
            message = (
                "The document was matched but no text could be extracted from it — it is "
                "most likely a scanned PDF, and there is no OCR. Open the source directly; "
                "do not treat this as the procedure being absent."
            )
        else:
            status_value, message = "no_result", "No page text matched that query."

        return {
            "status": status_value,
            "query": query,
            "results": results,
            "matched_documents": matched_documents,
            "exact_source_matched": exact_source_matched,
            "source": "ADAS SI",
            "message": message,
            "evidence_contract": {
                "authoritative_records_only": True,
                "specific_facts_traceable_to_results": True,
                "do_not_infer_missing_records": True,
                "matched_source_is_not_a_no_result": True,
            },
        }

    # ---------- managed annotations (never touch OEM originals) ----------

    def _managed_path(self, record_id: str) -> Path:
        safe = SAFE_ID_RE.sub("", str(record_id))
        if not safe:
            raise ValueError("record_id is required and must be alphanumeric")
        self.managed_root.mkdir(parents=True, exist_ok=True)
        return self.managed_root / f"{safe}.json"

    def record_list(self, _args: Optional[dict] = None) -> dict[str, Any]:
        """List bounded operator annotations without touching OEM sources."""
        if not self.available():
            return {
                "status": "unavailable",
                "records": [],
                "message": "The ADAS SI source library is not reachable.",
            }
        if not self.managed_root.is_dir():
            return {"status": "success", "records": [], "count": 0}

        records: list[dict[str, Any]] = []
        invalid_count = 0
        for path in sorted(self.managed_root.glob("*.json"))[:500]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                invalid_count += 1
                continue
            if not isinstance(payload, dict):
                invalid_count += 1
                continue
            records.append(
                {
                    key: payload.get(key)
                    for key in (
                        "record_id",
                        "title",
                        "content",
                        "version",
                        "created_by",
                        "updated_by",
                        "updated_at",
                    )
                    if payload.get(key) is not None
                }
            )
        return {
            "status": "success" if invalid_count == 0 else "partial_success",
            "records": records,
            "count": len(records),
            "invalid_count": invalid_count,
        }

    def record_write(self, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
        if not self.available():
            return {"status": "unavailable", "executed": False,
                    "message": "The ADAS SI source library is not reachable."}
        target = self._managed_path(args.get("record_id"))
        if target.exists():
            raise ValueError(
                f"Managed record '{target.stem}' already exists. Use adas_si_record_modify."
            )
        payload = {
            "record_id": target.stem,
            "title": str(args.get("title") or target.stem),
            "content": str(args.get("content") or ""),
            "created_by": (user or {}).get("google_sub") or "operator",
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {
            "status": "success", "executed": True,
            "receipt": {
                "operation": "write", "path": str(target), "version": 1,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "originals_modified": False,
            },
            "record": payload,
        }

    def record_modify(self, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
        if not self.available():
            return {"status": "unavailable", "executed": False,
                    "message": "The ADAS SI source library is not reachable."}
        target = self._managed_path(args.get("record_id"))
        if not target.is_file():
            return {"status": "no_result", "executed": False,
                    "record_id": target.stem,
                    "message": "No managed record with that id."}
        payload = json.loads(target.read_text(encoding="utf-8"))
        expected = int(args.get("expected_version") or 0)
        if int(payload.get("version", 0)) != expected:
            raise ValueError(
                f"Version conflict: record is at version {payload.get('version')}, "
                f"you supplied expected_version={expected}. Re-read it first."
            )
        payload.update({
            "title": args.get("title", payload.get("title")),
            "content": args.get("content", payload.get("content")),
            "updated_by": (user or {}).get("google_sub") or "operator",
            "version": expected + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {
            "status": "success", "executed": True,
            "receipt": {
                "operation": "modify", "path": str(target), "version": payload["version"],
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "originals_modified": False,
            },
            "record": payload,
        }

    # ---------- rendering pages as images ----------

    def page_count(self, path: Path) -> Optional[int]:
        if pdfium is None or path.suffix.lower() != ".pdf":
            return None
        try:
            with _PDFIUM_LOCK:
                doc = pdfium.PdfDocument(str(path))
                try:
                    return len(doc)
                finally:
                    doc.close()
        except Exception:  # noqa: BLE001 - a broken PDF still lists, just without a count
            return None

    def render_page(self, path: Path, page: int, width: int = 1100) -> bytes:
        """Render one page to PNG.

        Inline page images are the display path rather than an embedded PDF
        viewer: mobile browsers refuse to render PDFs in an iframe, and Otis
        is on a phone in the field most of the time. It also makes scanned
        documents viewable -- a scan has no extractable text but rasterises
        perfectly.

        Results are cached on disk keyed by path, mtime, page and width, so
        paging through a document is fast and an edited source re-renders.
        """
        if pdfium is None:
            raise RuntimeError("pypdfium2 is not installed; cannot render PDF pages.")
        width = max(320, min(int(width or 1100), 2200))
        page = max(1, int(page or 1))

        mtime = path.stat().st_mtime_ns
        key = hashlib.sha256(f"{path}|{mtime}|{page}|{width}".encode()).hexdigest()[:32]
        cache_dir = self.cache_path.parent / "pages"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / f"{key}.png"
        if cached.is_file():
            return cached.read_bytes()

        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(str(path))
            try:
                if page > len(doc):
                    raise ValueError(f"Page {page} is past the end ({len(doc)} pages).")
                pdf_page = doc[page - 1]
                page_width = pdf_page.get_size()[0] or 612
                image = pdf_page.render(scale=width / page_width).to_pil()
            finally:
                doc.close()

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        try:
            cached.write_bytes(data)
        except OSError:
            pass
        return data

    # ---------- pulling a document up in chat ----------

    def resolve_relative(self, relative: str) -> Path:
        candidate = (self.source_root / str(relative)).resolve()
        try:
            candidate.relative_to(self.source_root)
        except ValueError as exc:
            raise ValueError("ADAS SI document path escapes the source library.") from exc
        if not candidate.is_file() or candidate.suffix.lower() != ".pdf":
            raise ValueError("ADAS SI document does not exist.")
        return candidate

    def relative_of(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.source_root)).replace("\\", "/")

    def open_document(self, args: dict) -> dict[str, Any]:
        relative = str(args.get("relative_path") or "").strip()
        page = max(1, int(args.get("page") or 1))
        if not relative:
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("relative_path or query is required")
            matches = self.inventory.matching_documents(query, limit=4)
            if not matches:
                return {"status": "no_result", "document": None,
                        "message": "No ADAS SI document matched that request."}
            chosen = matches[0]
            path = chosen["path"]
            alternatives = matches[1:]
        else:
            path = self.resolve_relative(relative)
            alternatives = []

        total = self.page_count(path)
        if total is not None:
            page = min(page, max(1, total))
        descriptor = describe_document(self.source_root, path)
        rel = self.relative_of(path)
        doc = {
            "title": descriptor["title"],
            "relative_path": rel,
            "url": f"/api/adas-si/document?path={quote(rel)}",
            "page_url": f"/api/adas-si/page?path={quote(rel)}",
            "page": page,
            "pages_total": total,
            "renderable": pdfium is not None,
            "vehicle": {
                k: descriptor[k]
                for k in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                if descriptor.get(k) is not None
            },
            "authoritative_path": str(path),
        }
        return {
            "status": "success", "document": doc,
            "alternatives": [
                {
                    "title": a["descriptor"]["title"],
                    "relative_path": self.relative_of(a["path"]),
                    "url": f"/api/adas-si/document?path={quote(self.relative_of(a['path']))}",
                }
                for a in alternatives
            ],
            "source": "ADAS SI",
        }

    # ---------- direct file operations ----------

    def _backup(self, path: Path) -> Optional[Path]:
        """Back up an existing original before overwrite/delete."""
        if not path.exists():
            return None
        rel = path.resolve().relative_to(self.source_root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.source_root / BACKUP_DIRNAME / f"{stamp}-{rel.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        return target

    def write_file(self, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
        relative = str(args.get("relative_path") or "").strip().replace("\\", "/")
        if not relative:
            raise ValueError("relative_path is required")
        target = (self.source_root / relative).resolve()
        try:
            target.relative_to(self.source_root)
        except ValueError as exc:
            raise ValueError("ADAS SI write path escapes the source library") from exc
        if target.suffix.lower() != ".pdf":
            raise ValueError("ADAS SI write only accepts PDF files")
        content = args.get("content")
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, (bytes, bytearray)):
            data = bytes(content)
        else:
            raise ValueError("content must be bytes or string")
        backup = self._backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.inventory._cache = None
        return {
            "status": "success", "executed": True,
            "receipt": {
                "operation": "write", "relative_path": self.relative_of(target),
                "bytes": len(data), "backup": self.relative_of(backup) if backup else None,
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        }

    def delete_file(self, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
        path = self.resolve_relative(args.get("relative_path"))
        backup = self._backup(path)
        path.unlink()
        self.inventory._cache = None
        return {
            "status": "success", "executed": True,
            "receipt": {
                "operation": "delete", "relative_path": str(args.get("relative_path")),
                "backup": self.relative_of(backup) if backup else None,
            },
        }
