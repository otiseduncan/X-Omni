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

Known limitation kept from XV12: text extraction is pypdf only, no OCR.
A scanned image-only PDF yields no text. That case is reported honestly
as `partial_success` -- "the document exists and matched, but no text
could be extracted" -- rather than as "no result", because telling a
technician a procedure doesn't exist when it does is the worst possible
failure here.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

log = logging.getLogger("xomni.adas_si")

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - surfaced at call time instead
    PdfReader = None

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - page images degrade to the PDF link
    pdfium = None

MANAGED_DIRNAME = "_xomni_managed"
BACKUP_DIRNAME = "_xomni_backups"
MAX_PAGE_CHARS = 250_000
MAX_RESULTS = 8
MAX_MATCHED_DOCS = 5
EXCERPT_LEAD = 350
EXCERPT_LEN = 1800

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
TOPIC_RE = re.compile(
    r"\b(front camera|rear camera|surround view|blind spot|adaptive cruise|"
    r"lane keep|lane departure|park assist|night vision|radar|lidar|"
    r"windshield|calibration|alignment|abs|airbag)\b",
    re.IGNORECASE,
)
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

    match = YEAR_RE.match(title)
    if not match:
        return descriptor
    descriptor["year"] = int(match.group(1))
    body = match.group(2).strip()

    make, remainder = _split_make(body)
    descriptor["make"] = make
    if not make or not remainder:
        descriptor["parse_confidence"] = "low"
        return descriptor

    words = remainder.split()
    while words and words[0].casefold() in BODY_PREFIXES:
        words.pop(0)
    remainder = " ".join(words)

    topic_m = TOPIC_RE.search(remainder)
    drive_m = DRIVETRAIN_RE.search(remainder)
    plat_m = PLATFORM_RE.search(remainder)
    if topic_m:
        descriptor["topic"] = _clean(topic_m.group(1)).title()
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
    return descriptor


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

        return {
            "status": "success",
            "authoritative_path": str(self.source_root),
            "summary": {
                "document_count": len(docs),
                "vehicle_application_count": len(applications),
                "parsed_document_count": len(docs) - len(unparsed),
                "unparsed_document_count": len(unparsed),
            },
            "documents": docs,
            "applications": applications,
            "unparsed_documents": unparsed,
            "evidence_contract": {
                "authoritative_records_only": True,
                "do_not_infer_records_from_counts": True,
            },
        }


# ==========================================================================
# page text cache + search
# ==========================================================================

class AdasSI:
    def __init__(self, source_root: Path, cache_path: Path):
        self.source_root = Path(source_root).resolve()
        self.cache_path = Path(cache_path).resolve()
        self.managed_root = self.source_root / MANAGED_DIRNAME
        self.inventory = SourceInventory(self.source_root)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS pages("
                "  path TEXT, page INTEGER, text TEXT, source_mtime_ns INTEGER,"
                "  PRIMARY KEY(path, page));"
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
                "INSERT OR REPLACE INTO meta VALUES('schema_version','1');"
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

        if PdfReader is None:
            raise RuntimeError("pypdf is not installed; cannot read ADAS SI PDFs.")
        reader = PdfReader(str(path), strict=False)
        pages = [
            (n, (page.extract_text() or "")[:MAX_PAGE_CHARS])
            for n, page in enumerate(reader.pages, 1)
        ]
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
        return snap

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

    def relative_of(self, path: Path) -> str:
        """POSIX-style path relative to the library root, for use in a URL."""
        try:
            return path.resolve().relative_to(self.source_root).as_posix()
        except ValueError:
            return path.name

    def resolve_relative(self, relative: str) -> Path:
        """Inverse of relative_of, confined to the library. Used by the HTTP
        route that streams a document, so a crafted path cannot escape."""
        candidate = (self.source_root / str(relative or "").strip()).resolve()
        if candidate != self.source_root and self.source_root not in candidate.parents:
            raise ValueError("Path is outside the ADAS SI library.")
        if not candidate.is_file():
            raise ValueError("No such ADAS SI document.")
        return candidate

    def _document_ref(self, path: Path, descriptor: dict, page: int = 1) -> dict:
        relative = self.relative_of(path)
        return {
            "title": descriptor.get("title") or path.stem,
            "source": path.name,
            "relative_path": relative,
            # The UI renders this in an inline PDF frame; #page= jumps to the hit.
            "url": f"/api/adas-si/document?path={quote(relative)}",
            # Page images are what actually render in chat; the PDF url above
            # stays available for print/download/copy in a real viewer.
            "page_url": f"/api/adas-si/page?path={quote(relative)}",
            "page": int(page or 1),
            "vehicle": {
                k: descriptor.get(k)
                for k in ("year", "make", "model", "drivetrain", "platform_code", "topic")
                if descriptor.get(k) is not None
            },
        }

    def open_document(self, args: dict) -> dict[str, Any]:
        """Resolve a document by name or description and return it for
        inline display. This is what 'pull up the F-150 front camera doc'
        should call -- it shows the real PDF rather than describing it."""
        if not self.available():
            return {"status": "unavailable", "document": None,
                    "message": f"The ADAS SI library is not reachable at {self.source_root}."}

        query = str(args.get("document") or args.get("query") or "").strip()
        if not query:
            raise ValueError("document is required")

        matches = self.inventory.matching_documents(query, limit=5)
        if not matches:
            return {"status": "no_result", "document": None, "query": query,
                    "message": "No ADAS SI document matched that."}

        best = matches[0]
        page = int(args.get("page") or 1)
        ref = self._document_ref(best["path"], best["descriptor"], page)

        pages_total = self.page_count(best["path"])
        if pages_total is None:
            try:
                pages_total = len(self._pages(best["path"]))
            except Exception:  # noqa: BLE001 - a scanned/broken PDF still displays
                pages_total = None

        return {
            "status": "success",
            "query": query,
            "document": {
                **ref,
                "pages_total": pages_total,
                "renderable": pdfium is not None and best["path"].suffix.lower() == ".pdf",
            },
            "alternatives": [
                self._document_ref(m["path"], m["descriptor"]) for m in matches[1:4]
            ],
        }

    # ---------- direct writes into the library ----------

    def _resolve_in_library(self, raw: str) -> Path:
        """Resolve a path and confine it to the ADAS SI tree.

        Full write access inside the library, nothing outside it -- '..'
        cannot be used to reach the rest of the disk.
        """
        candidate = Path(str(raw or "").strip())
        if not candidate.is_absolute():
            candidate = self.source_root / candidate
        resolved = candidate.resolve()
        if resolved != self.source_root and self.source_root not in resolved.parents:
            raise ValueError(
                f"Path is outside the ADAS SI library ({self.source_root}): {resolved}"
            )
        return resolved

    def _backup(self, path: Path) -> Optional[str]:
        """Copy an existing file aside before it is overwritten.

        This is the one rail kept on library writes. It does not block the
        write -- it makes the write reversible, so a bad edit to an
        authoritative document is recoverable rather than final.
        """
        if not path.is_file():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_dir = self.source_root / BACKUP_DIRNAME
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"{path.stem}.{stamp}{path.suffix}.bak"
        target.write_bytes(path.read_bytes())
        return str(target)

    def file_write(self, args: dict, user: Optional[dict] = None) -> dict[str, Any]:
        """Create or overwrite a file inside the ADAS SI library."""
        if not self.available():
            return {"status": "unavailable", "executed": False,
                    "message": f"The ADAS SI library is not reachable at {self.source_root}."}

        path = self._resolve_in_library(args.get("path"))
        content = args.get("content")
        if content is None:
            raise ValueError("content is required")

        existed = path.is_file()
        backup = self._backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = str(content).encode("utf-8")
        path.write_bytes(data)

        # A changed file must not be served from stale extracted text.
        if path.suffix.lower() == ".pdf":
            try:
                with sqlite3.connect(self.cache_path) as db:
                    db.execute("DELETE FROM pages WHERE path=?", (str(path),))
            except sqlite3.Error:
                pass
        self.inventory._cache = None  # force a re-walk on next search

        return {
            "status": "success",
            "executed": True,
            "receipt": {
                "operation": "overwrite" if existed else "create",
                "path": str(path),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "backup_path": backup,
                "overwrote_existing": existed,
            },
            "message": (
                f"Overwrote {path.name}. Previous version saved to {backup}."
                if backup else f"Created {path.name}."
            ),
        }

    def record_list(self, _args: dict | None = None) -> dict[str, Any]:
        if not self.managed_root.is_dir():
            return {"status": "success", "records": [], "managed_path": str(self.managed_root)}
        records = []
        for f in sorted(self.managed_root.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                records.append({
                    "record_id": data.get("record_id", f.stem),
                    "title": data.get("title"),
                    "version": data.get("version"),
                    "updated_at": data.get("updated_at"),
                })
            except (OSError, ValueError):
                continue
        return {"status": "success", "records": records,
                "managed_path": str(self.managed_root)}
