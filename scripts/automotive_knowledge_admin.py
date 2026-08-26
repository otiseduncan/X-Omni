"""Local administrator CLI for durable automotive knowledge review.

This script is intentionally not registered as a model tool.  Candidate
imports pass through the same forced-unverified facade as model captures;
only an operator invoking this local CLI can review evidence as verified or
promote a reviewed record.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.automotive_knowledge import (  # noqa: E402
    AutomotiveKnowledgeError,
    AutomotiveKnowledgeRepository,
    AutomotiveKnowledgeService,
)


DEFAULT_DB = (
    ROOT
    / "data"
    / "capabilities"
    / "automotive_knowledge"
    / "knowledge.sqlite"
)
MAX_IMPORT_BYTES = 2 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import, inspect, review, and promote durable automotive knowledge "
            "from the local trusted administrator boundary."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("XOMNI_AUTOMOTIVE_KNOWLEDGE_DB", str(DEFAULT_DB))),
        help="Knowledge SQLite path (defaults to X Omni's configured knowledge DB).",
    )
    parser.add_argument(
        "--authoritative-root",
        action="append",
        type=Path,
        dest="authoritative_roots",
        help=(
            "Root containing authoritative local source files. Repeat for multiple "
            "roots. Defaults to XOMNI_ADAS_SI_ROOT or X:\\ADAS SI."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    import_command = commands.add_parser(
        "import-candidate",
        help="Import a JSON candidate while forcing all evidence to unverified.",
    )
    import_command.add_argument("--input", type=Path, required=True)
    import_command.add_argument("--actor", required=True)

    read_command = commands.add_parser(
        "read", help="Read one record with a fresh source-integrity verdict."
    )
    read_command.add_argument("--record-id", required=True)

    review_command = commands.add_parser(
        "review-evidence",
        help="Apply an audited evidence review after rehashing its local source.",
    )
    review_command.add_argument("--record-id", required=True)
    review_command.add_argument("--evidence-id", required=True)
    review_command.add_argument("--expected-version", type=int, required=True)
    review_command.add_argument(
        "--extraction-status",
        choices=("pending", "extracted", "failed"),
        required=True,
    )
    review_command.add_argument(
        "--verification-status",
        choices=("unverified", "verified", "rejected"),
        required=True,
    )
    review_command.add_argument("--actor", required=True)

    promote_command = commands.add_parser(
        "promote",
        help="Promote a reviewed record after freshly rehashing every local source.",
    )
    promote_command.add_argument("--record-id", required=True)
    promote_command.add_argument("--expected-version", type=int, required=True)
    promote_command.add_argument(
        "--target", choices=("evidence_backed", "verified"), required=True
    )
    promote_command.add_argument("--actor", required=True)
    return parser


def _actor(value: str) -> str:
    actor = " ".join(str(value or "").split()).strip()
    if not actor:
        raise AutomotiveKnowledgeError("actor is required")
    return actor


def _read_candidate(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise AutomotiveKnowledgeError("candidate input file is not readable") from exc
    if size > MAX_IMPORT_BYTES:
        raise AutomotiveKnowledgeError(
            f"candidate input exceeds {MAX_IMPORT_BYTES} bytes"
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutomotiveKnowledgeError(
            "candidate input must be a readable UTF-8 JSON object"
        ) from exc
    if not isinstance(payload, dict):
        raise AutomotiveKnowledgeError("candidate input must contain one JSON object")
    return payload


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    roots = args.authoritative_roots or [
        Path(os.getenv("XOMNI_ADAS_SI_ROOT", r"X:\ADAS SI"))
    ]
    repository = AutomotiveKnowledgeRepository(
        args.db.resolve(), authoritative_roots=roots
    )
    try:
        if args.command == "import-candidate":
            service = AutomotiveKnowledgeService(repository)
            return service.store(
                _read_candidate(args.input), actor=_actor(args.actor)
            )
        if args.command == "read":
            record = repository.get(args.record_id)
            return {
                "status": "success" if record else "no_result",
                "record": record,
            }
        if args.command == "review-evidence":
            return repository.review_evidence(
                args.record_id,
                args.evidence_id,
                extraction_status=args.extraction_status,
                verification_status=args.verification_status,
                expected_version=args.expected_version,
                actor=_actor(args.actor),
            )
        if args.command == "promote":
            record = repository.promote(
                args.record_id,
                args.target,
                expected_version=args.expected_version,
                actor=_actor(args.actor),
            )
            return {"status": "success", "record": record}
        raise AutomotiveKnowledgeError("unsupported administrator command")
    finally:
        repository.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except AutomotiveKnowledgeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
