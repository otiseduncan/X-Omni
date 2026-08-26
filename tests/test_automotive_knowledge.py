from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.services.automotive_knowledge import (
    KNOWLEDGE_SCHEMA_VERSION,
    AutomotiveKnowledgeConflict,
    AutomotiveKnowledgeError,
    AutomotiveKnowledgeMigrationError,
    AutomotiveKnowledgeRepository,
    AutomotiveKnowledgeService,
)


_SOURCE_BYTES = b"bounded authoritative repair manual fixture\n"
_SOURCE_SHA256 = hashlib.sha256(_SOURCE_BYTES).hexdigest()
_SOURCE_RELATIVE_PATH = r"Toyota\2020 Camry Repair Manual.pdf"


def _repo(tmp_path) -> AutomotiveKnowledgeRepository:
    source_root = tmp_path / "adas-si"
    source = source_root / _SOURCE_RELATIVE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_SOURCE_BYTES)
    return AutomotiveKnowledgeRepository(
        tmp_path / "knowledge.sqlite",
        authoritative_roots=[source_root],
    )


def _source_path(tmp_path: Path) -> Path:
    return tmp_path / "adas-si" / _SOURCE_RELATIVE_PATH


def _evidence(
    *,
    digest: str | None = _SOURCE_SHA256,
    authoritative: bool = True,
    extraction_status: str = "extracted",
    verification_status: str = "verified",
    page: int | None = 12,
    section: str | None = "Millimeter wave radar sensor adjustment",
    document_id: str = "toyota-rm-2020-camry",
) -> dict:
    source = {
        "type": "adas_si",
        "document_id": document_id,
        "name": "Toyota Repair Manual",
        "local_path": _SOURCE_RELATIVE_PATH,
        "authoritative": authoritative,
        "metadata": {
            "edition": "2020",
            "api_key": "must-never-persist",
            "nested": {"refresh_token": "also-secret", "region": "US"},
        },
    }
    if digest is not None:
        source["sha256"] = digest
    return {
        "source": source,
        "page": page,
        "section": section,
        "excerpt": "Calibration is required after radar sensor removal.",
        "extraction_status": extraction_status,
        "verification_status": verification_status,
        "source_tool_call_id": "adas-search-1",
    }


def _payload(
    *,
    lifecycle: str = "verified",
    evidence: list[dict] | None = None,
    requirement_text: str = "Perform static radar calibration after sensor removal.",
) -> dict:
    return {
        "application": {
            "year": 2020,
            "make": "Toyota",
            "model": "Camry",
            "trim": "LE",
            "option_codes": ["TSS-P"],
        },
        "system": {"name": "Forward collision warning"},
        "component": {"name": "Front radar sensor", "part_family": "radar"},
        "repair_event": {
            "event_type": "collision_repair",
            "description": "Front bumper and radar sensor removed",
        },
        "requirement": {
            "type": "calibration",
            "text": requirement_text,
            "calibration_type": "static",
            "inspection_required": True,
        },
        "prerequisites": [
            {"kind": "setup", "description": "Vehicle on a level floor"},
        ],
        "procedures": [
            {
                "title": "Radar sensor aiming",
                "reference": "RM1000001",
                "summary": "Follow the OEM target placement procedure.",
            }
        ],
        "lifecycle": lifecycle,
        "confidence": 0.98,
        "evidence": evidence if evidence is not None else [_evidence()],
    }


def test_schema_migrations_are_versioned_idempotent_and_checksum_guarded(tmp_path):
    path = tmp_path / "knowledge.sqlite"
    repo = AutomotiveKnowledgeRepository(path)
    assert repo.schema_version == KNOWLEDGE_SCHEMA_VERSION == 1
    tables = {
        row[0]
        for row in repo.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert {
        "knowledge_schema_migrations",
        "knowledge_records",
        "knowledge_sources",
        "knowledge_evidence",
        "knowledge_record_events",
        "knowledge_fts",
    } <= tables
    assert repo.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    repo.close()

    reopened = AutomotiveKnowledgeRepository(path)
    assert reopened.schema_version == 1
    assert reopened.conn.execute(
        "SELECT COUNT(*) FROM knowledge_schema_migrations"
    ).fetchone()[0] == 1
    reopened.conn.execute(
        "UPDATE knowledge_schema_migrations SET checksum='corrupt' WHERE version=1"
    )
    reopened.conn.commit()
    reopened.close()

    with pytest.raises(AutomotiveKnowledgeMigrationError, match="checksum"):
        AutomotiveKnowledgeRepository(path)


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(authoritative=False),
        _evidence(digest=None),
        _evidence(extraction_status="pending"),
        _evidence(verification_status="unverified"),
        _evidence(page=None, section=None),
    ],
)
def test_verified_status_rejects_unsupported_or_unlocated_evidence(tmp_path, evidence):
    repo = _repo(tmp_path)
    with pytest.raises(AutomotiveKnowledgeError, match="verified|evidence_backed"):
        repo.create_record(_payload(evidence=[evidence]))


def test_discovered_and_evidence_backed_are_distinct_from_verified(tmp_path):
    repo = _repo(tmp_path)
    discovered = repo.create_record(
        _payload(
            lifecycle="discovered",
            evidence=[
                _evidence(
                    digest=None,
                    authoritative=False,
                    extraction_status="pending",
                    verification_status="unverified",
                    page=None,
                    section=None,
                )
            ],
        )
    )["record"]
    assert discovered["lifecycle"] == "discovered"

    backed = repo.create_record(
        _payload(
            lifecycle="evidence_backed",
            requirement_text="Inspect radar bracket after bumper removal.",
            evidence=[
                _evidence(
                    digest=None,
                    authoritative=False,
                    verification_status="unverified",
                    document_id="collision-note-1",
                )
            ],
        )
    )["record"]
    assert backed["lifecycle"] == "evidence_backed"
    assert repo.search({})["status"] == "no_result"  # verified is the safe default


def test_verified_record_is_idempotent_searchable_and_provenance_preserving(tmp_path):
    repo = _repo(tmp_path)
    first = repo.create_record(_payload(), actor="adas_import")
    record = first["record"]
    assert first["created"] is True
    assert record["lifecycle"] == "verified"
    assert record["version"] == 3  # discovered -> backed -> verified
    assert record["evidence"][0]["page_start"] == 12
    assert record["evidence"][0]["section"].startswith("Millimeter")
    assert record["evidence"][0]["source"]["content_sha256"] == _SOURCE_SHA256
    assert (
        record["evidence"][0]["source"]["content_validation_status"]
        == "content_hash_verified"
    )
    assert record["evidence"][0]["source"]["metadata"] == {
        "edition": "2020",
        "nested": {"region": "US"},
    }

    repeated = repo.create_record(_payload(), actor="adas_import")
    assert repeated["created"] is False
    assert repeated["evidence_added"] == 0
    assert repeated["record"]["id"] == record["id"]
    assert repeated["record"]["version"] == record["version"]

    result = repo.search(
        {
            "query": "radar calibration",
            "year": 2020,
            "manufacturer": "Toyota",
            "model": "Camry",
            "system": "Forward collision warning",
            "event_type": "collision_repair",
        }
    )
    assert result["status"] == "success"
    assert [item["id"] for item in result["records"]] == [record["id"]]
    assert result["evidence_contract"]["unsupported_inference_is_not_verified"] is True


def test_explicit_evidence_and_promotions_enforce_cas_and_status_rules(tmp_path):
    repo = _repo(tmp_path)
    record = repo.create_record(
        _payload(
            lifecycle="discovered",
            evidence=[
                _evidence(
                    extraction_status="pending",
                    verification_status="unverified",
                    document_id="observation-1",
                )
            ],
        )
    )["record"]

    with pytest.raises(AutomotiveKnowledgeError, match="evidence_backed"):
        repo.promote(
            record["id"],
            "evidence_backed",
            expected_version=record["version"],
        )

    reviewed = repo.review_evidence(
        record["id"],
        record["evidence"][0]["id"],
        extraction_status="extracted",
        verification_status="verified",
        expected_version=record["version"],
        actor="reviewer",
    )["record"]
    assert reviewed["version"] == 2
    assert reviewed["evidence"][0]["verification_status"] == "verified"
    with pytest.raises(AutomotiveKnowledgeConflict, match="version conflict"):
        repo.promote(record["id"], "verified", expected_version=1)

    verified = repo.promote(
        record["id"],
        "verified",
        expected_version=reviewed["version"],
        actor="reviewer",
    )
    assert verified["lifecycle"] == "verified"
    assert verified["version"] == 4


def test_supersession_preserves_history_and_is_excluded_by_default(tmp_path):
    repo = _repo(tmp_path)
    old = repo.create_record(_payload())["record"]
    replacement = repo.create_record(
        _payload(requirement_text="Perform dynamic radar calibration after sensor removal.")
    )["record"]

    superseded = repo.supersede(
        old["id"],
        replacement["id"],
        expected_version=old["version"],
        actor="oem_revision_import",
    )
    assert superseded["lifecycle"] == "superseded"
    assert superseded["superseded_by"] == replacement["id"]
    assert superseded["evidence"][0]["source"]["document_id"] == "toyota-rm-2020-camry"

    default = repo.search({"query": "radar calibration"})
    assert [item["id"] for item in default["records"]] == [replacement["id"]]
    history = repo.search(
        {
            "query": "radar calibration",
            "lifecycles": ["verified", "superseded"],
            "include_superseded": True,
        }
    )
    assert {item["id"] for item in history["records"]} == {
        old["id"],
        replacement["id"],
    }


def test_no_result_is_explicitly_source_bounded(tmp_path):
    repo = _repo(tmp_path)
    repo.create_record(_payload())
    result = repo.search({"year": 2025, "manufacturer": "Ford"})
    assert result["status"] == "no_result"
    assert "requested scope" in result["message"]
    assert "does not establish" in result["message"]


def test_model_facing_capture_cannot_self_assert_verified_claims(tmp_path):
    repo = _repo(tmp_path)
    service = AutomotiveKnowledgeService(repo)
    result = service.store(_payload(lifecycle="verified"), actor="model_tool")
    assert result["status"] == "success"
    assert result["requested_lifecycle"] == "verified"
    assert result["stored_lifecycle"] == "evidence_backed"
    assert result["verification_deferred"] is True
    assert result["verified"] is False
    assert result["record"]["lifecycle"] == "evidence_backed"
    assert result["record"]["evidence"][0]["verification_status"] == "unverified"
    assert repo.search({})["status"] == "no_result"

    with pytest.raises(AutomotiveKnowledgeError, match="cannot assert"):
        service.review_evidence(
            {
                "record_id": result["record"]["id"],
                "evidence_id": result["record"]["evidence"][0]["id"],
                "extraction_status": "extracted",
                "verification_status": "verified",
                "expected_version": result["record"]["version"],
            }
        )


@pytest.mark.parametrize("drift", ["modified", "missing"])
def test_review_rehashes_source_and_fails_closed_after_capture(tmp_path, drift):
    repo = _repo(tmp_path)
    record = repo.create_record(
        _payload(
            lifecycle="discovered",
            evidence=[
                _evidence(
                    extraction_status="pending",
                    verification_status="unverified",
                )
            ],
        )
    )["record"]
    source = _source_path(tmp_path)
    if drift == "modified":
        source.write_bytes(b"different authoritative content\n")
    else:
        source.unlink()

    with pytest.raises(AutomotiveKnowledgeError, match="authoritative|verified"):
        repo.review_evidence(
            record["id"],
            record["evidence"][0]["id"],
            extraction_status="extracted",
            verification_status="verified",
            expected_version=record["version"],
            actor="trusted_reviewer",
        )

    unchanged = repo.get(record["id"])
    assert unchanged is not None
    assert unchanged["version"] == record["version"]
    assert unchanged["evidence"][0]["verification_status"] == "unverified"


@pytest.mark.parametrize("drift", ["modified", "missing"])
def test_promotion_rehashes_source_and_fails_closed_after_review(tmp_path, drift):
    repo = _repo(tmp_path)
    record = repo.create_record(
        _payload(
            lifecycle="discovered",
            evidence=[
                _evidence(
                    extraction_status="pending",
                    verification_status="unverified",
                )
            ],
        )
    )["record"]
    reviewed = repo.review_evidence(
        record["id"],
        record["evidence"][0]["id"],
        extraction_status="extracted",
        verification_status="verified",
        expected_version=record["version"],
        actor="trusted_reviewer",
    )["record"]
    backed = repo.promote(
        record["id"],
        "evidence_backed",
        expected_version=reviewed["version"],
        actor="trusted_reviewer",
    )
    source = _source_path(tmp_path)
    if drift == "modified":
        source.write_bytes(b"revision changed without a new evidence import\n")
    else:
        source.unlink()

    with pytest.raises(AutomotiveKnowledgeError, match="current matching local source hash"):
        repo.promote(
            record["id"],
            "verified",
            expected_version=backed["version"],
            actor="trusted_reviewer",
        )

    unchanged = repo.get(record["id"])
    assert unchanged is not None
    assert unchanged["stored_lifecycle"] == "evidence_backed"
    assert unchanged["version"] == backed["version"]


@pytest.mark.parametrize("drift", ["modified", "missing"])
def test_verified_reads_fail_closed_when_authoritative_source_goes_stale(
    tmp_path, drift
):
    repo = _repo(tmp_path)
    record = repo.create_record(_payload())["record"]
    source = _source_path(tmp_path)
    if drift == "modified":
        source.write_bytes(b"unreviewed source revision\n")
        expected_status = "hash_mismatch"
    else:
        source.unlink()
        expected_status = "not_found"

    stale = repo.get(record["id"])
    assert stale is not None
    assert stale["stored_lifecycle"] == "verified"
    assert stale["lifecycle"] == "evidence_backed"
    assert stale["source_integrity"] == {
        "status": "stale",
        "verified_evidence_count": 1,
        "current_verified_evidence_count": 0,
        "stale_evidence_ids": [record["evidence"][0]["id"]],
        "verified_read_allowed": False,
    }
    evidence = stale["evidence"][0]
    assert evidence["stored_verification_status"] == "verified"
    assert evidence["verification_status"] == "unverified"
    assert evidence["verification_effective"] is False
    assert evidence["source"]["content_validation_status"] == expected_status
    assert evidence["source"]["stored_content_validation_status"] == (
        "content_hash_verified"
    )

    default_search = repo.search({"manufacturer": "Toyota", "model": "Camry"})
    assert default_search["status"] == "no_result"
    assert default_search["stale_verified_excluded"] == 1
    assert "excluded" in default_search["message"]

    service_read = AutomotiveKnowledgeService(repo).read({"record_id": record["id"]})
    assert service_read["status"] == "stale_source"
    assert service_read["verified"] is False

    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_SOURCE_BYTES)
    restored = repo.get(record["id"])
    assert restored is not None
    assert restored["lifecycle"] == "verified"
    assert restored["source_integrity"]["status"] == "current"
    assert repo.search({"manufacturer": "Toyota", "model": "Camry"})[
        "status"
    ] == "success"


def _run_admin(tmp_path: Path, source_root: Path, *arguments: str) -> dict:
    script = Path(__file__).resolve().parents[1] / "scripts" / "automotive_knowledge_admin.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(tmp_path / "admin-knowledge.sqlite"),
            "--authoritative-root",
            str(source_root),
            *arguments,
        ],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def test_local_admin_cli_import_review_and_promotion_are_operational(tmp_path):
    source_root = tmp_path / "admin-sources"
    source = source_root / _SOURCE_RELATIVE_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_SOURCE_BYTES)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_payload()), encoding="utf-8")

    imported = _run_admin(
        tmp_path,
        source_root,
        "import-candidate",
        "--input",
        str(candidate),
        "--actor",
        "test importer",
    )
    assert imported["requested_lifecycle"] == "verified"
    assert imported["stored_lifecycle"] == "evidence_backed"
    assert imported["record"]["evidence"][0]["verification_status"] == "unverified"

    record = imported["record"]
    reviewed = _run_admin(
        tmp_path,
        source_root,
        "review-evidence",
        "--record-id",
        record["id"],
        "--evidence-id",
        record["evidence"][0]["id"],
        "--expected-version",
        str(record["version"]),
        "--extraction-status",
        "extracted",
        "--verification-status",
        "verified",
        "--actor",
        "test reviewer",
    )["record"]
    assert reviewed["evidence"][0]["verification_status"] == "verified"

    promoted = _run_admin(
        tmp_path,
        source_root,
        "promote",
        "--record-id",
        record["id"],
        "--expected-version",
        str(reviewed["version"]),
        "--target",
        "verified",
        "--actor",
        "test reviewer",
    )["record"]
    assert promoted["lifecycle"] == "verified"
    assert promoted["source_integrity"]["status"] == "current"

    read_back = _run_admin(
        tmp_path,
        source_root,
        "read",
        "--record-id",
        record["id"],
    )
    assert read_back["status"] == "success"
    assert read_back["record"]["lifecycle"] == "verified"
