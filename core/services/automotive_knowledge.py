"""Durable, provenance-backed automotive knowledge.

This database is deliberately separate from both ``x_omni.sqlite`` (operator
and conversation state) and the ADAS SI ``index.sqlite`` (a rebuildable PDF
text/cache index).  Source documents remain authoritative where they already
live; this module stores structured claims and immutable pointers back to
those sources.

The repository owns persistence and lifecycle invariants.  The thin service
class exposes dict-in/dict-out methods suitable for Registry handlers without
granting a model authority to label unsupported inference as verified.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


KNOWLEDGE_SCHEMA_VERSION = 1
LIFECYCLES = ("discovered", "evidence_backed", "verified", "superseded")
REQUIREMENT_TYPES = (
    "calibration",
    "inspection",
    "calibration_and_inspection",
    "prerequisite",
    "procedure",
    "informational",
)
EXTRACTION_STATUSES = ("pending", "extracted", "failed")
VERIFICATION_STATUSES = ("unverified", "verified", "rejected")

_LIFECYCLE_RANK = {name: index for index, name in enumerate(LIFECYCLES)}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|authorization|cookie|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|session[_-]?token)",
    re.IGNORECASE,
)
_SEARCH_TOKEN_RE = re.compile(r"[\w][\w./+-]{1,63}", re.UNICODE)


class AutomotiveKnowledgeError(ValueError):
    """Invalid knowledge input or lifecycle transition."""


class AutomotiveKnowledgeConflict(AutomotiveKnowledgeError):
    """Optimistic-concurrency or supersession conflict."""


class AutomotiveKnowledgeMigrationError(RuntimeError):
    """The on-disk schema history does not match this build."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _clean(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split()).strip()
    if required and not text:
        raise AutomotiveKnowledgeError(f"{field} is required")
    if len(text) > limit:
        raise AutomotiveKnowledgeError(f"{field} exceeds {limit} characters")
    return text


def _key(value: Any) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).casefold().split()
    )


def _bounded_confidence(value: Any, *, field: str = "confidence") -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise AutomotiveKnowledgeError(f"{field} must be a number between 0 and 1")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AutomotiveKnowledgeError(
            f"{field} must be a number between 0 and 1"
        ) from exc
    if not 0.0 <= parsed <= 1.0:
        raise AutomotiveKnowledgeError(f"{field} must be between 0 and 1")
    return parsed


def _sanitize_metadata(value: Any, *, depth: int = 0) -> Any:
    """Keep useful provenance metadata while refusing credential-shaped data."""
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, child) in enumerate(value.items()):
            if index >= 64:
                result["_truncated"] = True
                break
            name = str(raw_key)[:120]
            if _SECRET_KEY_RE.search(name):
                continue
            result[name] = _sanitize_metadata(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        compact = [_sanitize_metadata(item, depth=depth + 1) for item in items[:64]]
        if len(items) > 64:
            compact.append({"_omitted_items": len(items) - 64})
        return compact
    if isinstance(value, str):
        return value[:2_000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2_000]


@dataclass(frozen=True)
class _Migration:
    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        material = f"{self.version}:{self.name}\n" + "\n-- statement --\n".join(
            self.statements
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


_MIGRATIONS = (
    _Migration(
        1,
        "initial_provenance_knowledge_graph",
        (
            """
            CREATE TABLE knowledge_applications (
                id              TEXT PRIMARY KEY,
                manufacturer    TEXT NOT NULL COLLATE NOCASE,
                year_start      INTEGER NOT NULL,
                year_end        INTEGER NOT NULL,
                model           TEXT NOT NULL COLLATE NOCASE,
                platform        TEXT,
                trim            TEXT,
                option_codes_json TEXT NOT NULL DEFAULT '[]',
                vin_pattern     TEXT,
                build_from      TEXT,
                build_to        TEXT,
                normalized_key  TEXT NOT NULL UNIQUE,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                CHECK (year_start BETWEEN 1900 AND 2200),
                CHECK (year_end BETWEEN year_start AND 2200)
            )
            """,
            """
            CREATE TABLE knowledge_systems (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                created_at      TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE knowledge_components (
                id              TEXT PRIMARY KEY,
                system_id       TEXT NOT NULL REFERENCES knowledge_systems(id) ON DELETE RESTRICT,
                name            TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                part_family     TEXT,
                created_at      TEXT NOT NULL,
                UNIQUE(system_id, normalized_name, part_family)
            )
            """,
            """
            CREATE TABLE knowledge_repair_events (
                id              TEXT PRIMARY KEY,
                event_type      TEXT NOT NULL,
                description     TEXT NOT NULL,
                normalized_key  TEXT NOT NULL UNIQUE,
                created_at      TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE knowledge_records (
                id                  TEXT PRIMARY KEY,
                application_id      TEXT NOT NULL REFERENCES knowledge_applications(id) ON DELETE RESTRICT,
                system_id           TEXT NOT NULL REFERENCES knowledge_systems(id) ON DELETE RESTRICT,
                component_id        TEXT REFERENCES knowledge_components(id) ON DELETE RESTRICT,
                repair_event_id     TEXT NOT NULL REFERENCES knowledge_repair_events(id) ON DELETE RESTRICT,
                requirement_type    TEXT NOT NULL CHECK (requirement_type IN (
                    'calibration','inspection','calibration_and_inspection',
                    'prerequisite','procedure','informational'
                )),
                requirement_text    TEXT NOT NULL,
                calibration_type    TEXT,
                inspection_required INTEGER NOT NULL DEFAULT 0 CHECK (inspection_required IN (0,1)),
                procedure_summary   TEXT,
                applicability_notes TEXT,
                lifecycle           TEXT NOT NULL CHECK (lifecycle IN (
                    'discovered','evidence_backed','verified','superseded'
                )),
                confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                superseded_by       TEXT REFERENCES knowledge_records(id) ON DELETE RESTRICT,
                fingerprint         TEXT NOT NULL UNIQUE,
                version             INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                created_by          TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                CHECK (
                    (lifecycle = 'superseded' AND superseded_by IS NOT NULL)
                    OR (lifecycle <> 'superseded' AND superseded_by IS NULL)
                )
            )
            """,
            """
            CREATE TABLE knowledge_sources (
                id              TEXT PRIMARY KEY,
                source_type     TEXT NOT NULL,
                document_id     TEXT,
                source_name     TEXT NOT NULL,
                source_uri      TEXT,
                local_path      TEXT,
                content_sha256  TEXT,
                retrieved_at    TEXT NOT NULL,
                source_revision TEXT,
                source_date     TEXT,
                authoritative   INTEGER NOT NULL DEFAULT 0 CHECK (authoritative IN (0,1)),
                content_validation_status TEXT NOT NULL CHECK (content_validation_status IN (
                    'not_configured','no_local_copy','not_found',
                    'path_outside_roots','hash_mismatch','content_hash_verified'
                )),
                content_validated_at TEXT,
                metadata_json   TEXT NOT NULL DEFAULT '{}',
                fingerprint     TEXT NOT NULL UNIQUE,
                created_at      TEXT NOT NULL,
                CHECK (document_id IS NOT NULL OR source_uri IS NOT NULL OR local_path IS NOT NULL)
            )
            """,
            """
            CREATE TABLE knowledge_evidence (
                id                  TEXT PRIMARY KEY,
                record_id           TEXT NOT NULL REFERENCES knowledge_records(id) ON DELETE RESTRICT,
                source_id           TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE RESTRICT,
                page_start          INTEGER,
                page_end            INTEGER,
                section             TEXT,
                excerpt             TEXT,
                extraction_status   TEXT NOT NULL CHECK (extraction_status IN (
                    'pending','extracted','failed'
                )),
                verification_status TEXT NOT NULL CHECK (verification_status IN (
                    'unverified','verified','rejected'
                )),
                confidence          REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                claim_sha256        TEXT NOT NULL,
                source_tool_call_id TEXT,
                source_receipt_id   TEXT,
                verified_by         TEXT,
                verified_at         TEXT,
                created_at          TEXT NOT NULL,
                UNIQUE(record_id, source_id, page_start, page_end, section, claim_sha256),
                CHECK (page_start IS NULL OR page_start >= 1),
                CHECK (page_end IS NULL OR (page_start IS NOT NULL AND page_end >= page_start)),
                CHECK (
                    verification_status <> 'verified'
                    OR (verified_by IS NOT NULL AND verified_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE knowledge_prerequisites (
                id          TEXT PRIMARY KEY,
                record_id   TEXT NOT NULL REFERENCES knowledge_records(id) ON DELETE RESTRICT,
                sequence    INTEGER NOT NULL DEFAULT 0,
                kind        TEXT NOT NULL,
                description TEXT NOT NULL,
                UNIQUE(record_id, sequence, kind, description)
            )
            """,
            """
            CREATE TABLE knowledge_procedures (
                id                   TEXT PRIMARY KEY,
                record_id            TEXT NOT NULL REFERENCES knowledge_records(id) ON DELETE RESTRICT,
                title                TEXT NOT NULL,
                procedure_identifier TEXT,
                summary              TEXT,
                UNIQUE(record_id, title, procedure_identifier)
            )
            """,
            """
            CREATE TABLE knowledge_record_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id      TEXT NOT NULL REFERENCES knowledge_records(id) ON DELETE RESTRICT,
                from_lifecycle TEXT,
                to_lifecycle   TEXT NOT NULL,
                actor          TEXT NOT NULL,
                detail_json    TEXT NOT NULL DEFAULT '{}',
                created_at     TEXT NOT NULL
            )
            """,
            """
            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                record_id UNINDEXED,
                search_text,
                tokenize='unicode61 remove_diacritics 2'
            )
            """,
            "CREATE INDEX ix_knowledge_application_vehicle ON knowledge_applications(manufacturer, model, year_start, year_end)",
            "CREATE INDEX ix_knowledge_record_lifecycle ON knowledge_records(lifecycle, updated_at DESC)",
            "CREATE INDEX ix_knowledge_record_dimensions ON knowledge_records(application_id, system_id, component_id, repair_event_id)",
            "CREATE INDEX ix_knowledge_evidence_record ON knowledge_evidence(record_id, verification_status)",
            "CREATE INDEX ix_knowledge_source_document ON knowledge_sources(source_type, document_id)",
        ),
    ),
)


class AutomotiveKnowledgeRepository:
    """SQLite repository with explicit migrations and lifecycle checks."""

    def __init__(
        self,
        path: str | Path,
        *,
        authoritative_roots: Iterable[str | Path] = (),
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.authoritative_roots = tuple(
            Path(raw).resolve()
            for raw in authoritative_roots
            if str(raw).strip()
        )
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=10,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._apply_migrations()
        except Exception:
            self.conn.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def _apply_migrations(self) -> None:
        with self._transaction() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    checksum    TEXT NOT NULL,
                    applied_at  TEXT NOT NULL
                )
                """
            )
            for migration in _MIGRATIONS:
                current = db.execute(
                    "SELECT name,checksum FROM knowledge_schema_migrations WHERE version=?",
                    (migration.version,),
                ).fetchone()
                if current:
                    if (
                        str(current["name"]) != migration.name
                        or str(current["checksum"]) != migration.checksum
                    ):
                        raise AutomotiveKnowledgeMigrationError(
                            f"Knowledge migration {migration.version} does not match its recorded checksum."
                        )
                    continue
                for statement in migration.statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO knowledge_schema_migrations(version,name,checksum,applied_at) "
                    "VALUES(?,?,?,?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        _now_iso(),
                    ),
                )

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM knowledge_schema_migrations"
            ).fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _application(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AutomotiveKnowledgeError("application must be an object")
        manufacturer = _clean(
            raw.get("manufacturer") or raw.get("make"),
            field="application.manufacturer",
            limit=120,
            required=True,
        )
        model = _clean(
            raw.get("model"), field="application.model", limit=160, required=True
        )
        year = raw.get("year")
        year_start = raw.get("year_start", raw.get("year_from", year))
        year_end = raw.get("year_end", raw.get("year_to", year_start))
        try:
            year_start = int(year_start)
            year_end = int(year_end)
        except (TypeError, ValueError) as exc:
            raise AutomotiveKnowledgeError(
                "application requires a numeric year or year range"
            ) from exc
        if not (1900 <= year_start <= year_end <= 2200):
            raise AutomotiveKnowledgeError("application year range is invalid")
        options_raw = raw.get("option_codes") or raw.get("options") or []
        if isinstance(options_raw, str):
            options_raw = [options_raw]
        if not isinstance(options_raw, (list, tuple, set)):
            raise AutomotiveKnowledgeError("application.option_codes must be a list")
        options = sorted(
            {
                value
                for item in options_raw
                if (value := _clean(item, field="application.option_code", limit=80))
            },
            key=str.casefold,
        )
        application = {
            "manufacturer": manufacturer,
            "year_start": year_start,
            "year_end": year_end,
            "model": model,
            "platform": _clean(raw.get("platform"), field="application.platform", limit=120),
            "trim": _clean(raw.get("trim"), field="application.trim", limit=160),
            "option_codes": options,
            "vin_pattern": _clean(
                raw.get("vin_pattern") or raw.get("vin_applicability"),
                field="application.vin_pattern",
                limit=160,
            ),
            "build_from": _clean(raw.get("build_from"), field="application.build_from", limit=80),
            "build_to": _clean(raw.get("build_to"), field="application.build_to", limit=80),
        }
        application["normalized_key"] = _canonical_json(
            {
                key: (_key(value) if isinstance(value, str) else value)
                for key, value in application.items()
            }
        )
        application["id"] = _stable_id("app", application["normalized_key"])
        return application

    @staticmethod
    def _named_dimension(raw: Any, *, field: str, required: bool = True) -> dict[str, str]:
        if isinstance(raw, dict):
            name = raw.get("name") or raw.get("label")
            family = raw.get("part_family")
        else:
            name = raw
            family = None
        text = _clean(name, field=field, limit=200, required=required)
        return {
            "name": text,
            "normalized_name": _key(text),
            "part_family": _clean(family, field=f"{field}.part_family", limit=120),
        }

    @staticmethod
    def _repair_event(raw: Any) -> dict[str, str]:
        if isinstance(raw, dict):
            event_type = raw.get("event_type") or raw.get("type") or "repair_event"
            description = raw.get("description") or raw.get("trigger") or raw.get("text")
        else:
            event_type = "repair_event"
            description = raw
        event = {
            "event_type": _clean(
                event_type, field="repair_event.event_type", limit=100, required=True
            ),
            "description": _clean(
                description,
                field="repair_event.description",
                limit=1_000,
                required=True,
            ),
        }
        event["normalized_key"] = _canonical_json(
            {"type": _key(event["event_type"]), "description": _key(event["description"])}
        )
        event["id"] = _stable_id("evt", event["normalized_key"])
        return event

    @staticmethod
    def _requirement(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AutomotiveKnowledgeError("requirement must be an object")
        requirement_type = _key(raw.get("requirement_type") or raw.get("type"))
        if requirement_type not in REQUIREMENT_TYPES:
            raise AutomotiveKnowledgeError(
                "requirement.requirement_type must be one of: "
                + ", ".join(REQUIREMENT_TYPES)
            )
        inspection = raw.get("inspection_required", False)
        if not isinstance(inspection, bool):
            raise AutomotiveKnowledgeError("requirement.inspection_required must be boolean")
        return {
            "requirement_type": requirement_type,
            "requirement_text": _clean(
                raw.get("requirement_text") or raw.get("text") or raw.get("requirement"),
                field="requirement.text",
                limit=4_000,
                required=True,
            ),
            "calibration_type": _clean(
                raw.get("calibration_type"),
                field="requirement.calibration_type",
                limit=240,
            ),
            "inspection_required": inspection,
            "procedure_summary": _clean(
                raw.get("procedure_summary"),
                field="requirement.procedure_summary",
                limit=8_000,
            ),
            "applicability_notes": _clean(
                raw.get("applicability_notes"),
                field="requirement.applicability_notes",
                limit=4_000,
            ),
        }

    @staticmethod
    def _prerequisites(raw: Any) -> list[dict[str, Any]]:
        if raw in (None, ""):
            return []
        if not isinstance(raw, list):
            raise AutomotiveKnowledgeError("prerequisites must be a list")
        result = []
        for index, item in enumerate(raw):
            if isinstance(item, dict):
                kind = item.get("kind") or "prerequisite"
                description = item.get("description") or item.get("text")
                sequence = item.get("sequence", index)
            else:
                kind, description, sequence = "prerequisite", item, index
            try:
                sequence = int(sequence)
            except (TypeError, ValueError) as exc:
                raise AutomotiveKnowledgeError("prerequisite.sequence must be integer") from exc
            result.append(
                {
                    "sequence": max(0, sequence),
                    "kind": _clean(kind, field="prerequisite.kind", limit=120, required=True),
                    "description": _clean(
                        description,
                        field="prerequisite.description",
                        limit=2_000,
                        required=True,
                    ),
                }
            )
        return result

    @staticmethod
    def _procedures(raw: Any) -> list[dict[str, str]]:
        if raw in (None, ""):
            return []
        if not isinstance(raw, list):
            raise AutomotiveKnowledgeError("procedures must be a list")
        result = []
        for item in raw:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name")
                identifier = item.get("procedure_identifier") or item.get("reference")
                summary = item.get("summary")
            else:
                title, identifier, summary = item, None, None
            result.append(
                {
                    "title": _clean(title, field="procedure.title", limit=300, required=True),
                    "procedure_identifier": _clean(
                        identifier, field="procedure.identifier", limit=240
                    ),
                    "summary": _clean(summary, field="procedure.summary", limit=4_000),
                }
            )
        return result

    @staticmethod
    def _source(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AutomotiveKnowledgeError("evidence.source must be an object")
        source_type = _clean(
            raw.get("source_type") or raw.get("type") or raw.get("kind"),
            field="source.source_type",
            limit=100,
            required=True,
        )
        source = {
            "source_type": source_type,
            "document_id": _clean(
                raw.get("document_id") or raw.get("source_document_id"),
                field="source.document_id",
                limit=300,
            ),
            "source_name": _clean(
                raw.get("source_name") or raw.get("name") or raw.get("title"),
                field="source.source_name",
                limit=500,
                required=True,
            ),
            "source_uri": _clean(
                raw.get("source_uri") or raw.get("uri") or raw.get("url"),
                field="source.source_uri",
                limit=2_000,
            ),
            "local_path": _clean(
                raw.get("local_path") or raw.get("location") or raw.get("relative_path"),
                field="source.local_path",
                limit=1_000,
            ),
            "content_sha256": _key(raw.get("content_sha256") or raw.get("sha256")),
            "retrieved_at": _clean(
                raw.get("retrieved_at") or _now_iso(),
                field="source.retrieved_at",
                limit=80,
                required=True,
            ),
            "source_revision": _clean(
                raw.get("source_revision") or raw.get("revision"),
                field="source.source_revision",
                limit=200,
            ),
            "source_date": _clean(
                raw.get("source_date"), field="source.source_date", limit=80
            ),
            "authoritative": bool(raw.get("authoritative") is True),
            "metadata": _sanitize_metadata(raw.get("metadata") or {}),
        }
        if not (source["document_id"] or source["source_uri"] or source["local_path"]):
            raise AutomotiveKnowledgeError(
                "source requires document_id, source_uri, or local_path"
            )
        if source["content_sha256"] and not _SHA256_RE.fullmatch(
            source["content_sha256"]
        ):
            raise AutomotiveKnowledgeError("source.content_sha256 must be a SHA-256 hex digest")
        fingerprint = {
            key: source[key]
            for key in (
                "source_type",
                "document_id",
                "source_uri",
                "local_path",
                "content_sha256",
                "source_revision",
            )
        }
        source["fingerprint"] = hashlib.sha256(
            _canonical_json(fingerprint).encode("utf-8")
        ).hexdigest()
        source["id"] = f"src_{source['fingerprint'][:32]}"
        return source

    @staticmethod
    def _hash_file(path: Path) -> str:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise AutomotiveKnowledgeError(
                "authoritative source changed while its content hash was being verified"
            )
        return digest.hexdigest()

    def _source_content_validation(self, source: dict[str, Any]) -> dict[str, Any]:
        """Return a fresh, root-confined local-source integrity verdict.

        A verdict is deliberately never inferred from the value persisted in
        SQLite.  Callers use this at capture, every positive trust transition,
        and every read of historically verified evidence.
        """
        verdict: dict[str, Any] = {
            "status": "not_configured",
            "validated_at": None,
        }
        local_path = str(source.get("local_path") or "").strip()
        if not local_path:
            verdict["status"] = "no_local_copy"
            return verdict
        if not self.authoritative_roots:
            return verdict

        raw = Path(local_path)
        candidates = [raw] if raw.is_absolute() else [
            root / raw for root in self.authoritative_roots
        ]
        inside_candidates: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if any(
                resolved == root or root in resolved.parents
                for root in self.authoritative_roots
            ):
                inside_candidates.append(resolved)
        if not inside_candidates:
            verdict["status"] = "path_outside_roots"
            return verdict
        existing = next((item for item in inside_candidates if item.is_file()), None)
        if existing is None:
            verdict["status"] = "not_found"
            return verdict
        expected = str(source.get("content_sha256") or "")
        verdict["validated_at"] = _now_iso()
        if not _SHA256_RE.fullmatch(expected):
            verdict["status"] = "hash_mismatch"
            return verdict
        try:
            actual = self._hash_file(existing)
        except FileNotFoundError:
            verdict["status"] = "not_found"
            return verdict
        except AutomotiveKnowledgeError:
            verdict["status"] = "hash_mismatch"
            return verdict
        except OSError:
            verdict["status"] = "hash_mismatch"
            return verdict
        verdict["status"] = (
            "content_hash_verified" if actual == expected else "hash_mismatch"
        )
        return verdict

    def _validate_source_content(self, source: dict[str, Any]) -> None:
        """Attach a fresh local-source integrity verdict to source input."""
        verdict = self._source_content_validation(source)
        source["content_validation_status"] = verdict["status"]
        source["content_validated_at"] = verdict["validated_at"]

    def _evidence(self, raw: Any, *, actor: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise AutomotiveKnowledgeError("evidence entries must be objects")
        source = self._source(raw.get("source"))
        self._validate_source_content(source)
        extraction = _key(raw.get("extraction_status") or "pending")
        verification = _key(raw.get("verification_status") or "unverified")
        if extraction not in EXTRACTION_STATUSES:
            raise AutomotiveKnowledgeError(
                "evidence.extraction_status must be one of: "
                + ", ".join(EXTRACTION_STATUSES)
            )
        if verification not in VERIFICATION_STATUSES:
            raise AutomotiveKnowledgeError(
                "evidence.verification_status must be one of: "
                + ", ".join(VERIFICATION_STATUSES)
            )
        page_start = raw.get("page_start", raw.get("page"))
        page_end = raw.get("page_end", page_start)
        try:
            page_start = int(page_start) if page_start not in (None, "") else None
            page_end = int(page_end) if page_end not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise AutomotiveKnowledgeError("evidence page references must be integers") from exc
        if page_start is not None and page_start < 1:
            raise AutomotiveKnowledgeError("evidence.page_start must be at least 1")
        if page_end is not None and (page_start is None or page_end < page_start):
            raise AutomotiveKnowledgeError("evidence.page_end is invalid")
        section = _clean(raw.get("section"), field="evidence.section", limit=500)
        excerpt = _clean(raw.get("excerpt"), field="evidence.excerpt", limit=4_000)
        verified_by = _clean(
            raw.get("verified_by") or (actor if verification == "verified" else ""),
            field="evidence.verified_by",
            limit=200,
        )
        verified_at = _clean(
            raw.get("verified_at") or (_now_iso() if verification == "verified" else ""),
            field="evidence.verified_at",
            limit=80,
        )
        if verification == "verified" and (not verified_by or not verified_at):
            raise AutomotiveKnowledgeError(
                "verified evidence requires verified_by and verified_at"
            )
        claim_material = {
            "source": source["fingerprint"],
            "page_start": page_start,
            "page_end": page_end,
            "section": section,
            "excerpt": excerpt,
        }
        return {
            "source": source,
            "page_start": page_start,
            "page_end": page_end,
            "section": section,
            "excerpt": excerpt,
            "extraction_status": extraction,
            "verification_status": verification,
            "confidence": _bounded_confidence(
                raw.get("confidence"), field="evidence.confidence"
            ),
            "claim_sha256": hashlib.sha256(
                _canonical_json(claim_material).encode("utf-8")
            ).hexdigest(),
            "source_tool_call_id": _clean(
                raw.get("source_tool_call_id"),
                field="evidence.source_tool_call_id",
                limit=240,
            ),
            "source_receipt_id": _clean(
                raw.get("source_receipt_id"),
                field="evidence.source_receipt_id",
                limit=240,
            ),
            "verified_by": verified_by,
            "verified_at": verified_at,
        }

    @staticmethod
    def _validate_target_lifecycle(
        lifecycle: str, evidence: Iterable[dict[str, Any]]
    ) -> None:
        rows = list(evidence)
        if not rows:
            raise AutomotiveKnowledgeError(
                "durable automotive knowledge requires at least one provenance source"
            )
        if lifecycle == "discovered":
            return
        located_extracted = [
            row
            for row in rows
            if row["extraction_status"] == "extracted"
            and (row.get("page_start") is not None or row.get("section"))
        ]
        extracted = [
            row
            for row in located_extracted
            if not (
                row["source"].get("authoritative") is True
                and row["source"].get("local_path")
                and row["source"].get("content_validation_status")
                != "content_hash_verified"
            )
        ]
        if not extracted:
            if located_extracted:
                raise AutomotiveKnowledgeError(
                    "evidence_backed authoritative evidence requires a current matching local source hash"
                )
            raise AutomotiveKnowledgeError(
                "evidence_backed knowledge requires extracted evidence with a page or section locator"
            )
        if lifecycle == "evidence_backed":
            return
        verified = [
            row
            for row in extracted
            if row["verification_status"] == "verified"
            and row["source"]["authoritative"] is True
            and _SHA256_RE.fullmatch(row["source"].get("content_sha256") or "")
            and row["source"].get("content_validation_status")
            == "content_hash_verified"
        ]
        if lifecycle == "verified" and not verified:
            raise AutomotiveKnowledgeError(
                "verified knowledge requires verified, located evidence from an authoritative source whose local content hash was validated"
            )
        if lifecycle == "superseded":
            raise AutomotiveKnowledgeError(
                "create a replacement record, then supersede the old record explicitly"
            )

    @staticmethod
    def _insert_application(db: sqlite3.Connection, item: dict[str, Any], now: str) -> str:
        db.execute(
            """
            INSERT OR IGNORE INTO knowledge_applications(
                id,manufacturer,year_start,year_end,model,platform,trim,
                option_codes_json,vin_pattern,build_from,build_to,normalized_key,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item["id"],
                item["manufacturer"],
                item["year_start"],
                item["year_end"],
                item["model"],
                item["platform"] or None,
                item["trim"] or None,
                _canonical_json(item["option_codes"]),
                item["vin_pattern"] or None,
                item["build_from"] or None,
                item["build_to"] or None,
                item["normalized_key"],
                now,
                now,
            ),
        )
        return item["id"]

    @staticmethod
    def _insert_system(db: sqlite3.Connection, item: dict[str, str], now: str) -> str:
        system_id = _stable_id("sys", item["normalized_name"])
        db.execute(
            "INSERT OR IGNORE INTO knowledge_systems(id,name,normalized_name,created_at) VALUES(?,?,?,?)",
            (system_id, item["name"], item["normalized_name"], now),
        )
        return system_id

    @staticmethod
    def _insert_component(
        db: sqlite3.Connection,
        system_id: str,
        item: Optional[dict[str, str]],
        now: str,
    ) -> Optional[str]:
        if not item or not item["name"]:
            return None
        identity = {
            "system_id": system_id,
            "name": item["normalized_name"],
            "part_family": _key(item["part_family"]),
        }
        component_id = _stable_id("cmp", identity)
        db.execute(
            """
            INSERT OR IGNORE INTO knowledge_components(
                id,system_id,name,normalized_name,part_family,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                component_id,
                system_id,
                item["name"],
                item["normalized_name"],
                item["part_family"] or None,
                now,
            ),
        )
        return component_id

    @staticmethod
    def _insert_event(db: sqlite3.Connection, item: dict[str, str], now: str) -> str:
        db.execute(
            """
            INSERT OR IGNORE INTO knowledge_repair_events(
                id,event_type,description,normalized_key,created_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                item["id"],
                item["event_type"],
                item["description"],
                item["normalized_key"],
                now,
            ),
        )
        return item["id"]

    @staticmethod
    def _insert_source(db: sqlite3.Connection, source: dict[str, Any], now: str) -> str:
        db.execute(
            """
            INSERT INTO knowledge_sources(
                id,source_type,document_id,source_name,source_uri,local_path,
                content_sha256,retrieved_at,source_revision,source_date,
                authoritative,content_validation_status,content_validated_at,
                metadata_json,fingerprint,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                content_validation_status=excluded.content_validation_status,
                content_validated_at=excluded.content_validated_at
            """,
            (
                source["id"],
                source["source_type"],
                source["document_id"] or None,
                source["source_name"],
                source["source_uri"] or None,
                source["local_path"] or None,
                source["content_sha256"] or None,
                source["retrieved_at"],
                source["source_revision"] or None,
                source["source_date"] or None,
                int(source["authoritative"]),
                source["content_validation_status"],
                source["content_validated_at"],
                _canonical_json(source["metadata"]),
                source["fingerprint"],
                now,
            ),
        )
        return source["id"]

    @classmethod
    def _insert_evidence(
        cls,
        db: sqlite3.Connection,
        record_id: str,
        evidence: dict[str, Any],
        now: str,
    ) -> bool:
        source_id = cls._insert_source(db, evidence["source"], now)
        identity = {
            "record_id": record_id,
            "source_id": source_id,
            "page_start": evidence["page_start"],
            "page_end": evidence["page_end"],
            "section": evidence["section"],
            "claim_sha256": evidence["claim_sha256"],
        }
        evidence_id = _stable_id("evd", identity)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO knowledge_evidence(
                id,record_id,source_id,page_start,page_end,section,excerpt,
                extraction_status,verification_status,confidence,claim_sha256,
                source_tool_call_id,source_receipt_id,verified_by,verified_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                evidence_id,
                record_id,
                source_id,
                evidence["page_start"],
                evidence["page_end"],
                evidence["section"] or None,
                evidence["excerpt"] or None,
                evidence["extraction_status"],
                evidence["verification_status"],
                evidence["confidence"],
                evidence["claim_sha256"],
                evidence["source_tool_call_id"] or None,
                evidence["source_receipt_id"] or None,
                evidence["verified_by"] or None,
                evidence["verified_at"] or None,
                now,
            ),
        )
        return cursor.rowcount > 0

    @staticmethod
    def _evidence_rows_locked(
        db: sqlite3.Connection, record_id: str
    ) -> list[sqlite3.Row]:
        return db.execute(
            """
            SELECT e.*,s.source_type,s.document_id,s.source_name,s.source_uri,
                   s.local_path,s.content_sha256,s.retrieved_at,s.source_revision,
                   s.source_date,s.authoritative,s.content_validation_status,
                   s.content_validated_at,s.metadata_json
            FROM knowledge_evidence e
            JOIN knowledge_sources s ON s.id=e.source_id
            WHERE e.record_id=?
            ORDER BY e.created_at,e.id
            """,
            (record_id,),
        ).fetchall()

    def _rehash_record_sources_locked(
        self, db: sqlite3.Connection, record_id: str
    ) -> list[sqlite3.Row]:
        """Rehash every persisted local source used by a record.

        This method is called from the same immediate transaction as a trust
        transition, so a cached capture-time status can never authorize review
        or promotion.  Successful verdicts are persisted with their new check
        time.  A failed transition rolls the transaction back and is still
        reported from the freshly computed verdict, never the cached value.
        """
        source_rows = db.execute(
            """
            SELECT DISTINCT s.id,s.local_path,s.content_sha256
            FROM knowledge_sources s
            JOIN knowledge_evidence e ON e.source_id=s.id
            WHERE e.record_id=?
            """,
            (record_id,),
        ).fetchall()
        for row in source_rows:
            verdict = self._source_content_validation(
                {
                    "local_path": row["local_path"],
                    "content_sha256": row["content_sha256"],
                }
            )
            db.execute(
                """
                UPDATE knowledge_sources
                SET content_validation_status=?,content_validated_at=?
                WHERE id=?
                """,
                (verdict["status"], verdict["validated_at"], row["id"]),
            )
        return self._evidence_rows_locked(db, record_id)

    @staticmethod
    def _project_lifecycle_evidence(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        projected = []
        for row in rows:
            projected.append(
                {
                    "page_start": row["page_start"],
                    "section": row["section"],
                    "extraction_status": row["extraction_status"],
                    "verification_status": row["verification_status"],
                    "source": {
                        "authoritative": bool(row["authoritative"]),
                        "local_path": str(row["local_path"] or ""),
                        "content_sha256": str(row["content_sha256"] or ""),
                        "content_validation_status": row[
                            "content_validation_status"
                        ],
                    },
                }
            )
        return projected

    def _validate_persisted_lifecycle_locked(
        self, db: sqlite3.Connection, record_id: str, lifecycle: str
    ) -> None:
        rows = self._rehash_record_sources_locked(db, record_id)
        self._validate_target_lifecycle(
            lifecycle, self._project_lifecycle_evidence(rows)
        )

    @staticmethod
    def _record_search_text(
        application: dict[str, Any],
        system: dict[str, str],
        component: Optional[dict[str, str]],
        event: dict[str, str],
        requirement: dict[str, Any],
        prerequisites: list[dict[str, Any]],
        procedures: list[dict[str, str]],
    ) -> str:
        values: list[str] = [
            application["manufacturer"],
            str(application["year_start"]),
            str(application["year_end"]),
            application["model"],
            application["platform"],
            application["trim"],
            " ".join(application["option_codes"]),
            system["name"],
            component["name"] if component else "",
            component["part_family"] if component else "",
            event["event_type"],
            event["description"],
            requirement["requirement_type"],
            requirement["requirement_text"],
            requirement["calibration_type"],
            requirement["procedure_summary"],
            requirement["applicability_notes"],
        ]
        values.extend(item["description"] for item in prerequisites)
        values.extend(
            f"{item['title']} {item['procedure_identifier']} {item['summary']}"
            for item in procedures
        )
        return "\n".join(value for value in values if value)[:50_000]

    def _transition_locked(
        self,
        db: sqlite3.Connection,
        record_id: str,
        target: str,
        actor: str,
        *,
        detail: Optional[dict[str, Any]] = None,
    ) -> int:
        row = db.execute(
            "SELECT lifecycle,version FROM knowledge_records WHERE id=?", (record_id,)
        ).fetchone()
        if not row:
            raise AutomotiveKnowledgeError("knowledge record does not exist")
        current = str(row["lifecycle"])
        if target == current:
            return int(row["version"])
        if target == "superseded":
            raise AutomotiveKnowledgeError("use supersede() for that transition")
        expected_next = LIFECYCLES[_LIFECYCLE_RANK[current] + 1]
        if expected_next != target:
            raise AutomotiveKnowledgeError(
                f"knowledge lifecycle must advance one step from {current} to {expected_next}"
            )
        self._validate_persisted_lifecycle_locked(db, record_id, target)
        version = int(row["version"]) + 1
        now = _now_iso()
        db.execute(
            "UPDATE knowledge_records SET lifecycle=?,version=?,updated_at=? WHERE id=?",
            (target, version, now, record_id),
        )
        db.execute(
            """
            INSERT INTO knowledge_record_events(
                record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                record_id,
                current,
                target,
                actor,
                _canonical_json(detail or {}),
                now,
            ),
        )
        return version

    def create_record(self, payload: dict[str, Any], *, actor: str = "operator") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AutomotiveKnowledgeError("knowledge payload must be an object")
        actor = _clean(actor, field="actor", limit=200, required=True)
        application = self._application(payload.get("application"))
        system = self._named_dimension(payload.get("system"), field="system")
        component_raw = payload.get("component")
        component = (
            self._named_dimension(component_raw, field="component", required=False)
            if component_raw not in (None, "", {})
            else None
        )
        event = self._repair_event(
            payload.get("repair_event") or payload.get("event") or payload.get("trigger")
        )
        requirement = self._requirement(payload.get("requirement"))
        prerequisites = self._prerequisites(payload.get("prerequisites"))
        procedures = self._procedures(payload.get("procedures"))
        raw_evidence = payload.get("evidence")
        if not isinstance(raw_evidence, list):
            raise AutomotiveKnowledgeError("evidence must be a list")
        evidence = [self._evidence(item, actor=actor) for item in raw_evidence]
        lifecycle = _key(payload.get("lifecycle") or "discovered")
        if lifecycle not in LIFECYCLES:
            raise AutomotiveKnowledgeError(
                "lifecycle must be one of: " + ", ".join(LIFECYCLES)
            )
        self._validate_target_lifecycle(lifecycle, evidence)
        confidence = _bounded_confidence(payload.get("confidence"))

        fingerprint_payload = {
            "application": application["normalized_key"],
            "system": system["normalized_name"],
            "component": component or None,
            "event": event["normalized_key"],
            "requirement": requirement,
            "prerequisites": prerequisites,
            "procedures": procedures,
        }
        fingerprint = hashlib.sha256(
            _canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()
        record_id = f"akr_{fingerprint[:32]}"
        now = _now_iso()

        with self._transaction() as db:
            application_id = self._insert_application(db, application, now)
            system_id = self._insert_system(db, system, now)
            component_id = self._insert_component(db, system_id, component, now)
            event_id = self._insert_event(db, event, now)
            existing = db.execute(
                "SELECT id,lifecycle,version FROM knowledge_records WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            created = existing is None
            previous_version = int(existing["version"]) if existing else 0
            if created:
                db.execute(
                    """
                    INSERT INTO knowledge_records(
                        id,application_id,system_id,component_id,repair_event_id,
                        requirement_type,requirement_text,calibration_type,
                        inspection_required,procedure_summary,applicability_notes,
                        lifecycle,confidence,superseded_by,fingerprint,version,
                        created_by,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,1,?,?,?)
                    """,
                    (
                        record_id,
                        application_id,
                        system_id,
                        component_id,
                        event_id,
                        requirement["requirement_type"],
                        requirement["requirement_text"],
                        requirement["calibration_type"] or None,
                        int(requirement["inspection_required"]),
                        requirement["procedure_summary"] or None,
                        requirement["applicability_notes"] or None,
                        "discovered",
                        confidence,
                        fingerprint,
                        actor,
                        now,
                        now,
                    ),
                )
                db.execute(
                    """
                    INSERT INTO knowledge_record_events(
                        record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
                    ) VALUES(?,NULL,'discovered',?,?,?)
                    """,
                    (record_id, actor, _canonical_json({"created": True}), now),
                )
                search_text = self._record_search_text(
                    application,
                    system,
                    component,
                    event,
                    requirement,
                    prerequisites,
                    procedures,
                )
                db.execute(
                    "INSERT INTO knowledge_fts(record_id,search_text) VALUES(?,?)",
                    (record_id, search_text),
                )
            else:
                record_id = str(existing["id"])

            for item in prerequisites:
                item_id = _stable_id("pre", {"record_id": record_id, **item})
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_prerequisites(id,record_id,sequence,kind,description) VALUES(?,?,?,?,?)",
                    (
                        item_id,
                        record_id,
                        item["sequence"],
                        item["kind"],
                        item["description"],
                    ),
                )
            for item in procedures:
                item_id = _stable_id("pro", {"record_id": record_id, **item})
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_procedures(id,record_id,title,procedure_identifier,summary) VALUES(?,?,?,?,?)",
                    (
                        item_id,
                        record_id,
                        item["title"],
                        item["procedure_identifier"] or None,
                        item["summary"] or None,
                    ),
                )
            evidence_added = sum(
                1 for item in evidence if self._insert_evidence(db, record_id, item, now)
            )

            current = str(
                db.execute(
                    "SELECT lifecycle FROM knowledge_records WHERE id=?", (record_id,)
                ).fetchone()[0]
            )
            if current == "superseded":
                raise AutomotiveKnowledgeConflict(
                    "an exact superseded record cannot be silently revived"
                )
            while _LIFECYCLE_RANK[current] < _LIFECYCLE_RANK[lifecycle]:
                target = LIFECYCLES[_LIFECYCLE_RANK[current] + 1]
                self._transition_locked(
                    db,
                    record_id,
                    target,
                    actor,
                    detail={"source": "create_record"},
                )
                current = target
            if evidence_added and not created:
                db.execute(
                    "UPDATE knowledge_records SET version=version+1,updated_at=? WHERE id=?",
                    (now, record_id),
                )
                db.execute(
                    """
                    INSERT INTO knowledge_record_events(
                        record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        current,
                        current,
                        actor,
                        _canonical_json({"evidence_added": evidence_added}),
                        now,
                    ),
                )

        record = self.get(record_id)
        assert record is not None
        return {
            "created": created,
            "evidence_added": evidence_added,
            "changed": int(record["version"]) != previous_version,
            "record": record,
        }

    def add_evidence(
        self,
        record_id: str,
        raw_evidence: dict[str, Any],
        *,
        expected_version: int,
        actor: str = "operator",
    ) -> dict[str, Any]:
        record_id = _clean(record_id, field="record_id", limit=100, required=True)
        actor = _clean(actor, field="actor", limit=200, required=True)
        evidence = self._evidence(raw_evidence, actor=actor)
        with self._transaction() as db:
            row = db.execute(
                "SELECT lifecycle,version FROM knowledge_records WHERE id=?", (record_id,)
            ).fetchone()
            if not row:
                raise AutomotiveKnowledgeError("knowledge record does not exist")
            if int(row["version"]) != int(expected_version):
                raise AutomotiveKnowledgeConflict(
                    f"version conflict: record is at {row['version']}"
                )
            if str(row["lifecycle"]) == "superseded":
                raise AutomotiveKnowledgeConflict("superseded knowledge is immutable")
            now = _now_iso()
            added = self._insert_evidence(db, record_id, evidence, now)
            if added:
                db.execute(
                    "UPDATE knowledge_records SET version=version+1,updated_at=? WHERE id=?",
                    (now, record_id),
                )
                db.execute(
                    """
                    INSERT INTO knowledge_record_events(
                        record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        row["lifecycle"],
                        row["lifecycle"],
                        actor,
                        _canonical_json({"evidence_added": 1}),
                        now,
                    ),
                )
        record = self.get(record_id)
        assert record is not None
        return {"added": added, "record": record}

    def review_evidence(
        self,
        record_id: str,
        evidence_id: str,
        *,
        extraction_status: str,
        verification_status: str,
        expected_version: int,
        actor: str = "operator",
    ) -> dict[str, Any]:
        """Apply an explicit, audited review to already-preserved evidence.

        Verification is monotonic: a verified or rejected claim cannot be
        silently moved back to unverified or flipped to the opposite terminal
        result. Extraction may be retried after a failure, but extracted data
        cannot be demoted to pending. A verified outcome is accepted only when
        the persisted source itself satisfies the authoritative/hash/locator
        contract; model-provided status text alone is insufficient.
        """
        record_id = _clean(record_id, field="record_id", limit=100, required=True)
        evidence_id = _clean(evidence_id, field="evidence_id", limit=100, required=True)
        actor = _clean(actor, field="actor", limit=200, required=True)
        extraction_status = _key(extraction_status)
        verification_status = _key(verification_status)
        if extraction_status not in EXTRACTION_STATUSES:
            raise AutomotiveKnowledgeError(
                "extraction_status must be one of: " + ", ".join(EXTRACTION_STATUSES)
            )
        if verification_status not in VERIFICATION_STATUSES:
            raise AutomotiveKnowledgeError(
                "verification_status must be one of: " + ", ".join(VERIFICATION_STATUSES)
            )

        with self._transaction() as db:
            row = db.execute(
                "SELECT lifecycle,version FROM knowledge_records WHERE id=?",
                (record_id,),
            ).fetchone()
            evidence = db.execute(
                """
                SELECT e.*,s.authoritative,s.content_sha256,
                       s.content_validation_status
                FROM knowledge_evidence e
                JOIN knowledge_sources s ON s.id=e.source_id
                WHERE e.id=? AND e.record_id=?
                """,
                (evidence_id, record_id),
            ).fetchone()
            if not row or not evidence:
                raise AutomotiveKnowledgeError("knowledge record or evidence does not exist")
            if int(row["version"]) != int(expected_version):
                raise AutomotiveKnowledgeConflict(
                    f"version conflict: record is at {row['version']}"
                )
            if str(row["lifecycle"]) == "superseded":
                raise AutomotiveKnowledgeConflict("superseded knowledge is immutable")

            # Never authorize a review from capture-time source metadata.  The
            # configured local file is hashed again inside this transaction.
            fresh_evidence = {
                str(item["id"]): item
                for item in self._rehash_record_sources_locked(db, record_id)
            }
            evidence = fresh_evidence[evidence_id]

            current_extraction = str(evidence["extraction_status"])
            current_verification = str(evidence["verification_status"])
            if current_extraction == "extracted" and extraction_status != "extracted":
                raise AutomotiveKnowledgeError("extracted evidence cannot be demoted")
            if current_verification in {"verified", "rejected"} and (
                verification_status != current_verification
            ):
                raise AutomotiveKnowledgeError(
                    "a terminal evidence review cannot be reversed in place"
                )
            if verification_status == "verified":
                if not (
                    extraction_status == "extracted"
                    and (evidence["page_start"] is not None or evidence["section"])
                    and bool(evidence["authoritative"])
                    and _SHA256_RE.fullmatch(str(evidence["content_sha256"] or ""))
                    and evidence["content_validation_status"]
                    == "content_hash_verified"
                ):
                    raise AutomotiveKnowledgeError(
                        "verified evidence requires extracted, located content from an authoritative hashed source"
                    )
            elif (
                extraction_status == "extracted"
                and bool(evidence["authoritative"])
                and evidence["local_path"]
                and evidence["content_validation_status"]
                != "content_hash_verified"
            ):
                raise AutomotiveKnowledgeError(
                    "extracted authoritative evidence requires a current matching local source hash"
                )

            changed = (
                extraction_status != current_extraction
                or verification_status != current_verification
            )
            if changed:
                now = _now_iso()
                db.execute(
                    """
                    UPDATE knowledge_evidence
                    SET extraction_status=?,verification_status=?,
                        verified_by=?,verified_at=?
                    WHERE id=?
                    """,
                    (
                        extraction_status,
                        verification_status,
                        actor if verification_status == "verified" else None,
                        now if verification_status == "verified" else None,
                        evidence_id,
                    ),
                )
                db.execute(
                    "UPDATE knowledge_records SET version=version+1,updated_at=? WHERE id=?",
                    (now, record_id),
                )
                db.execute(
                    """
                    INSERT INTO knowledge_record_events(
                        record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        record_id,
                        row["lifecycle"],
                        row["lifecycle"],
                        actor,
                        _canonical_json(
                            {
                                "evidence_id": evidence_id,
                                "extraction_status": {
                                    "from": current_extraction,
                                    "to": extraction_status,
                                },
                                "verification_status": {
                                    "from": current_verification,
                                    "to": verification_status,
                                },
                            }
                        ),
                        now,
                    ),
                )
        record = self.get(record_id)
        assert record is not None
        return {"changed": changed, "record": record}

    def promote(
        self,
        record_id: str,
        target: str,
        *,
        expected_version: int,
        actor: str = "operator",
    ) -> dict[str, Any]:
        record_id = _clean(record_id, field="record_id", limit=100, required=True)
        actor = _clean(actor, field="actor", limit=200, required=True)
        target = _key(target)
        if target not in {"evidence_backed", "verified"}:
            raise AutomotiveKnowledgeError(
                "promotion target must be evidence_backed or verified"
            )
        with self._transaction() as db:
            row = db.execute(
                "SELECT lifecycle,version FROM knowledge_records WHERE id=?", (record_id,)
            ).fetchone()
            if not row:
                raise AutomotiveKnowledgeError("knowledge record does not exist")
            if int(row["version"]) != int(expected_version):
                raise AutomotiveKnowledgeConflict(
                    f"version conflict: record is at {row['version']}"
                )
            current = str(row["lifecycle"])
            if current == "superseded":
                raise AutomotiveKnowledgeConflict("superseded knowledge is immutable")
            if _LIFECYCLE_RANK[target] < _LIFECYCLE_RANK[current]:
                raise AutomotiveKnowledgeError("knowledge lifecycle cannot move backward")
            while current != target:
                next_status = LIFECYCLES[_LIFECYCLE_RANK[current] + 1]
                self._transition_locked(
                    db,
                    record_id,
                    next_status,
                    actor,
                    detail={"source": "explicit_promotion"},
                )
                current = next_status
        record = self.get(record_id)
        assert record is not None
        return record

    def supersede(
        self,
        record_id: str,
        replacement_id: str,
        *,
        expected_version: int,
        actor: str = "operator",
    ) -> dict[str, Any]:
        record_id = _clean(record_id, field="record_id", limit=100, required=True)
        replacement_id = _clean(
            replacement_id, field="replacement_id", limit=100, required=True
        )
        actor = _clean(actor, field="actor", limit=200, required=True)
        if record_id == replacement_id:
            raise AutomotiveKnowledgeError("a record cannot supersede itself")
        with self._transaction() as db:
            row = db.execute(
                "SELECT lifecycle,version FROM knowledge_records WHERE id=?", (record_id,)
            ).fetchone()
            replacement = db.execute(
                "SELECT lifecycle FROM knowledge_records WHERE id=?", (replacement_id,)
            ).fetchone()
            if not row or not replacement:
                raise AutomotiveKnowledgeError("knowledge record or replacement does not exist")
            if int(row["version"]) != int(expected_version):
                raise AutomotiveKnowledgeConflict(
                    f"version conflict: record is at {row['version']}"
                )
            if str(row["lifecycle"]) == "superseded":
                raise AutomotiveKnowledgeConflict("knowledge record is already superseded")
            if str(replacement["lifecycle"]) != "verified":
                raise AutomotiveKnowledgeError(
                    "replacement knowledge must be verified before supersession"
                )
            # Supersession depends on the replacement still being effectively
            # verified now, not merely having reached that lifecycle earlier.
            self._validate_persisted_lifecycle_locked(
                db, replacement_id, "verified"
            )
            now = _now_iso()
            db.execute(
                """
                UPDATE knowledge_records
                SET lifecycle='superseded',superseded_by=?,version=version+1,updated_at=?
                WHERE id=?
                """,
                (replacement_id, now, record_id),
            )
            db.execute(
                """
                INSERT INTO knowledge_record_events(
                    record_id,from_lifecycle,to_lifecycle,actor,detail_json,created_at
                ) VALUES(?,?,'superseded',?,?,?)
                """,
                (
                    record_id,
                    row["lifecycle"],
                    actor,
                    _canonical_json({"superseded_by": replacement_id}),
                    now,
                ),
            )
        record = self.get(record_id)
        assert record is not None
        return record

    def _source_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        stored_status = str(row["content_validation_status"])
        current = self._source_content_validation(
            {
                "local_path": row["local_path"],
                "content_sha256": row["content_sha256"],
            }
        )
        return {
            "id": row["source_id"],
            "source_type": row["source_type"],
            "document_id": row["document_id"],
            "source_name": row["source_name"],
            "source_uri": row["source_uri"],
            "local_path": row["local_path"],
            "content_sha256": row["content_sha256"],
            "retrieved_at": row["retrieved_at"],
            "source_revision": row["source_revision"],
            "source_date": row["source_date"],
            "authoritative": bool(row["authoritative"]),
            "content_validation_status": current["status"],
            "content_validated_at": current["validated_at"],
            "stored_content_validation_status": stored_status,
            "integrity_current": current["status"] == "content_hash_verified",
            "metadata": metadata,
        }

    def _record_dict_locked(self, row: sqlite3.Row) -> dict[str, Any]:
        record_id = str(row["id"])
        prerequisites = [
            dict(item)
            for item in self.conn.execute(
                "SELECT sequence,kind,description FROM knowledge_prerequisites WHERE record_id=? ORDER BY sequence,id",
                (record_id,),
            ).fetchall()
        ]
        procedures = [
            dict(item)
            for item in self.conn.execute(
                "SELECT title,procedure_identifier,summary FROM knowledge_procedures WHERE record_id=? ORDER BY id",
                (record_id,),
            ).fetchall()
        ]
        evidence_rows = self._evidence_rows_locked(self.conn, record_id)
        evidence = []
        stored_verified_evidence_count = 0
        current_verified_evidence_count = 0
        stale_evidence_ids: list[str] = []
        for item in evidence_rows:
            source = self._source_dict(item)
            stored_verification_status = str(item["verification_status"])
            current_verification_status = stored_verification_status
            verification_effective = False
            if stored_verification_status == "verified":
                stored_verified_evidence_count += 1
                verification_effective = bool(
                    item["extraction_status"] == "extracted"
                    and (item["page_start"] is not None or item["section"])
                    and item["authoritative"]
                    and source["integrity_current"]
                )
                if verification_effective:
                    current_verified_evidence_count += 1
                else:
                    current_verification_status = "unverified"
                    stale_evidence_ids.append(str(item["id"]))
            evidence.append(
                {
                    "id": item["id"],
                    "page_start": item["page_start"],
                    "page_end": item["page_end"],
                    "section": item["section"],
                    "excerpt": item["excerpt"],
                    "extraction_status": item["extraction_status"],
                    "verification_status": current_verification_status,
                    "stored_verification_status": stored_verification_status,
                    "verification_effective": verification_effective,
                    "confidence": item["confidence"],
                    "claim_sha256": item["claim_sha256"],
                    "source_tool_call_id": item["source_tool_call_id"],
                    "source_receipt_id": item["source_receipt_id"],
                    "verified_by": item["verified_by"],
                    "verified_at": item["verified_at"],
                    "source": source,
                }
            )
        stored_lifecycle = str(row["lifecycle"])
        lifecycle = stored_lifecycle
        integrity_status = "not_applicable"
        if stored_lifecycle == "verified":
            if current_verified_evidence_count:
                integrity_status = "current"
            else:
                # Preserve the historical lifecycle in stored_lifecycle while
                # refusing to expose drifted content as currently verified.
                lifecycle = "evidence_backed"
                integrity_status = "stale"
        elif stored_verified_evidence_count:
            integrity_status = (
                "current" if current_verified_evidence_count else "stale"
            )
        return {
            "id": record_id,
            "application": {
                "id": row["application_id"],
                "manufacturer": row["manufacturer"],
                "year_start": row["year_start"],
                "year_end": row["year_end"],
                "model": row["model"],
                "platform": row["platform"],
                "trim": row["trim"],
                "option_codes": json.loads(row["option_codes_json"] or "[]"),
                "vin_pattern": row["vin_pattern"],
                "build_from": row["build_from"],
                "build_to": row["build_to"],
            },
            "system": {"id": row["system_id"], "name": row["system_name"]},
            "component": (
                {
                    "id": row["component_id"],
                    "name": row["component_name"],
                    "part_family": row["part_family"],
                }
                if row["component_id"]
                else None
            ),
            "repair_event": {
                "id": row["repair_event_id"],
                "event_type": row["event_type"],
                "description": row["event_description"],
            },
            "requirement": {
                "requirement_type": row["requirement_type"],
                "text": row["requirement_text"],
                "calibration_type": row["calibration_type"],
                "inspection_required": bool(row["inspection_required"]),
                "procedure_summary": row["procedure_summary"],
                "applicability_notes": row["applicability_notes"],
            },
            "prerequisites": prerequisites,
            "procedures": procedures,
            "lifecycle": lifecycle,
            "stored_lifecycle": stored_lifecycle,
            "source_integrity": {
                "status": integrity_status,
                "verified_evidence_count": stored_verified_evidence_count,
                "current_verified_evidence_count": current_verified_evidence_count,
                "stale_evidence_ids": stale_evidence_ids,
                "verified_read_allowed": not (
                    stored_lifecycle == "verified" and integrity_status == "stale"
                ),
            },
            "confidence": row["confidence"],
            "superseded_by": row["superseded_by"],
            "version": row["version"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "evidence": evidence,
        }

    @staticmethod
    def _record_select() -> str:
        return """
            SELECT r.*,a.manufacturer,a.year_start,a.year_end,a.model,a.platform,
                   a.trim,a.option_codes_json,a.vin_pattern,a.build_from,a.build_to,
                   s.name AS system_name,c.name AS component_name,c.part_family,
                   e.event_type,e.description AS event_description
            FROM knowledge_records r
            JOIN knowledge_applications a ON a.id=r.application_id
            JOIN knowledge_systems s ON s.id=r.system_id
            LEFT JOIN knowledge_components c ON c.id=r.component_id
            JOIN knowledge_repair_events e ON e.id=r.repair_event_id
        """

    def get(self, record_id: str) -> Optional[dict[str, Any]]:
        record_id = _clean(record_id, field="record_id", limit=100, required=True)
        with self._lock:
            row = self.conn.execute(
                self._record_select() + " WHERE r.id=?", (record_id,)
            ).fetchone()
            return self._record_dict_locked(row) if row else None

    @staticmethod
    def _fts_query(value: Any) -> str:
        tokens = _SEARCH_TOKEN_RE.findall(
            unicodedata.normalize("NFKC", str(value or ""))
        )[:16]
        return " AND ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"*' for token in tokens
        )

    def search(self, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        args = dict(args or {})
        clauses: list[str] = []
        params: list[Any] = []
        joins = ""
        query = self._fts_query(args.get("query"))
        if args.get("query") and not query:
            return {
                "status": "no_result",
                "records": [],
                "count": 0,
                "source": "durable_automotive_knowledge",
                "message": "The search text did not contain usable terms.",
            }
        if query:
            # FTS5's MATCH and bm25() operands must be the virtual-table name;
            # SQLite does not consistently accept a table alias here.
            joins += " JOIN knowledge_fts ON knowledge_fts.record_id=r.id"
            clauses.append("knowledge_fts MATCH ?")
            params.append(query)

        for arg_name, column in (
            ("manufacturer", "a.manufacturer"),
            ("make", "a.manufacturer"),
            ("model", "a.model"),
            ("platform", "a.platform"),
            ("trim", "a.trim"),
            ("system", "s.name"),
            ("component", "c.name"),
            ("event_type", "e.event_type"),
            ("requirement_type", "r.requirement_type"),
            ("calibration_type", "r.calibration_type"),
        ):
            value = args.get(arg_name)
            if value not in (None, ""):
                clauses.append(f"{column} = ? COLLATE NOCASE")
                params.append(_clean(value, field=arg_name, limit=240, required=True))
        if args.get("event") not in (None, ""):
            clauses.append("e.description LIKE ? ESCAPE '\\'")
            value = _clean(args["event"], field="event", limit=500, required=True)
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if args.get("year") not in (None, ""):
            try:
                year = int(args["year"])
            except (TypeError, ValueError) as exc:
                raise AutomotiveKnowledgeError("year must be an integer") from exc
            clauses.append("a.year_start <= ? AND a.year_end >= ?")
            params.extend((year, year))

        lifecycles_raw = args.get("lifecycles") or args.get("lifecycle") or ["verified"]
        if isinstance(lifecycles_raw, str):
            lifecycles_raw = [lifecycles_raw]
        if not isinstance(lifecycles_raw, list) or not lifecycles_raw:
            raise AutomotiveKnowledgeError("lifecycles must be a non-empty list")
        lifecycles = [_key(item) for item in lifecycles_raw]
        if any(item not in LIFECYCLES for item in lifecycles):
            raise AutomotiveKnowledgeError("an unknown lifecycle was requested")
        if not args.get("include_superseded"):
            lifecycles = [item for item in lifecycles if item != "superseded"]
        if not lifecycles:
            return {
                "status": "no_result",
                "records": [],
                "count": 0,
                "source": "durable_automotive_knowledge",
                "message": "No non-superseded lifecycle was requested.",
            }
        placeholders = ",".join("?" for _ in lifecycles)
        clauses.append(f"r.lifecycle IN ({placeholders})")
        params.extend(lifecycles)

        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError) as exc:
            raise AutomotiveKnowledgeError("limit must be an integer") from exc
        limit = max(1, min(limit, 50))
        order = (
            "bm25(knowledge_fts), r.updated_at DESC"
            if query
            else "r.updated_at DESC, r.id"
        )
        sql = self._record_select() + joins
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order}"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params))
            records: list[dict[str, Any]] = []
            stale_verified_excluded = 0
            for row in rows:
                record = self._record_dict_locked(row)
                if record["lifecycle"] not in lifecycles:
                    if (
                        record["stored_lifecycle"] == "verified"
                        and record["source_integrity"]["status"] == "stale"
                    ):
                        stale_verified_excluded += 1
                    continue
                records.append(record)
                if len(records) >= limit:
                    break
        return {
            "status": "success" if records else "no_result",
            "records": records,
            "count": len(records),
            "stale_verified_excluded": stale_verified_excluded,
            "source": "durable_automotive_knowledge",
            "message": None
            if records
            else (
                "Matching records were excluded because their authoritative local source no longer matches its verified hash."
                if stale_verified_excluded
                else
                "No matching durable knowledge record was found in the requested scope. "
                "This source miss does not establish that the information does not exist."
            ),
            "evidence_contract": {
                "verified_requires_authoritative_hashed_source": True,
                "unsupported_inference_is_not_verified": True,
                "no_result_is_source_bounded": True,
                "superseded_excluded_by_default": True,
            },
        }

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class AutomotiveKnowledgeService:
    """Model-facing facade that cannot self-assert claim verification.

    Direct repository methods are the trusted-import/reviewer boundary. Calls
    through this facade may capture source-located candidate knowledge, but
    evidence is always stored unverified and a requested ``verified`` lifecycle
    is deferred until deterministic review invokes the repository API.
    """

    def __init__(self, repository: AutomotiveKnowledgeRepository):
        self.repository = repository

    def search(self, args: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        return self.repository.search(args)

    def read(self, args: dict[str, Any]) -> dict[str, Any]:
        record_id = str((args or {}).get("record_id") or "").strip()
        if not record_id:
            raise AutomotiveKnowledgeError("record_id is required")
        record = self.repository.get(record_id)
        stale_source = bool(
            record
            and record.get("stored_lifecycle") == "verified"
            and record.get("source_integrity", {}).get("status") == "stale"
        )
        return {
            "status": "stale_source" if stale_source else "success" if record else "no_result",
            "record": record,
            "source": "durable_automotive_knowledge",
            "verified": bool(record and record.get("lifecycle") == "verified"),
            "message": (
                "The record was historically verified, but its authoritative local source is now missing or does not match the verified hash. It is exposed only as evidence-backed until an administrator imports and reviews current source evidence."
                if stale_source
                else None
                if record
                else "No durable knowledge record has that id."
            ),
        }

    def store(
        self, args: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise AutomotiveKnowledgeError("knowledge payload must be an object")
        payload = deepcopy(args)
        requested_lifecycle = _key(payload.get("lifecycle") or "discovered")
        if requested_lifecycle not in {"discovered", "evidence_backed", "verified"}:
            raise AutomotiveKnowledgeError(
                "model-facing capture lifecycle must be discovered, evidence_backed, or verified"
            )
        raw_evidence = payload.get("evidence")
        if not isinstance(raw_evidence, list):
            raise AutomotiveKnowledgeError("evidence must be a list")
        has_extracted_locator = False
        for item in raw_evidence:
            if not isinstance(item, dict):
                raise AutomotiveKnowledgeError("evidence entries must be objects")
            item["verification_status"] = "unverified"
            item.pop("verified_by", None)
            item.pop("verified_at", None)
            extraction = _key(item.get("extraction_status") or "pending")
            has_locator = item.get("page") not in (None, "") or item.get(
                "page_start"
            ) not in (None, "") or bool(str(item.get("section") or "").strip())
            has_extracted_locator = has_extracted_locator or (
                extraction == "extracted" and has_locator
            )
        stored_lifecycle = (
            "evidence_backed"
            if requested_lifecycle in {"evidence_backed", "verified"}
            and has_extracted_locator
            else "discovered"
        )
        payload["lifecycle"] = stored_lifecycle
        outcome = self.repository.create_record(payload, actor=actor)
        record = outcome["record"]
        return {
            "status": "success",
            "executed": outcome["changed"],
            "success": True,
            "created": outcome["created"],
            "evidence_added": outcome["evidence_added"],
            "record": record,
            "verified": False,
            "requested_lifecycle": requested_lifecycle,
            "stored_lifecycle": stored_lifecycle,
            "verification_deferred": requested_lifecycle == "verified",
            "message": (
                "Candidate knowledge was preserved, but claim verification was deferred to the trusted review boundary."
                if requested_lifecycle == "verified"
                else None
            ),
        }

    def add_evidence(
        self, args: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        evidence = deepcopy(args.get("evidence"))
        if not isinstance(evidence, dict):
            raise AutomotiveKnowledgeError("evidence must be an object")
        evidence["verification_status"] = "unverified"
        evidence.pop("verified_by", None)
        evidence.pop("verified_at", None)
        outcome = self.repository.add_evidence(
            str(args.get("record_id") or ""),
            evidence,
            expected_version=int(args.get("expected_version") or 0),
            actor=actor,
        )
        return {
            "status": "success",
            "executed": outcome["added"],
            "success": True,
            **outcome,
        }

    def review_evidence(
        self, args: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        if _key(args.get("verification_status") or "unverified") != "unverified":
            raise AutomotiveKnowledgeError(
                "model-facing evidence review cannot assert verified or rejected status; use the trusted repository review boundary"
            )
        outcome = self.repository.review_evidence(
            str(args.get("record_id") or ""),
            str(args.get("evidence_id") or ""),
            extraction_status=str(args.get("extraction_status") or ""),
            verification_status="unverified",
            expected_version=int(args.get("expected_version") or 0),
            actor=actor,
        )
        return {
            "status": "success",
            "executed": outcome["changed"],
            "success": True,
            **outcome,
        }

    def promote(
        self, args: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        record = self.repository.promote(
            str(args.get("record_id") or ""),
            str(args.get("lifecycle") or ""),
            expected_version=int(args.get("expected_version") or 0),
            actor=actor,
        )
        return {
            "status": "success",
            "executed": True,
            "success": True,
            "verified": record["lifecycle"] == "verified",
            "record": record,
        }

    def supersede(
        self, args: dict[str, Any], *, actor: str = "operator"
    ) -> dict[str, Any]:
        record = self.repository.supersede(
            str(args.get("record_id") or ""),
            str(args.get("replacement_id") or ""),
            expected_version=int(args.get("expected_version") or 0),
            actor=actor,
        )
        return {
            "status": "success",
            "executed": True,
            "success": True,
            "record": record,
        }


__all__ = [
    "AutomotiveKnowledgeConflict",
    "AutomotiveKnowledgeError",
    "AutomotiveKnowledgeMigrationError",
    "AutomotiveKnowledgeRepository",
    "AutomotiveKnowledgeService",
    "KNOWLEDGE_SCHEMA_VERSION",
]
