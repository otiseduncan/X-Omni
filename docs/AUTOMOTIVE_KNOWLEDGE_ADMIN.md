# Automotive semantic-cache administration

`data/capabilities/automotive_knowledge/knowledge.sqlite` is **not** X's durable automotive source library. `X:\ADAS SI` is the source memory; this database is a provenance-backed semantic cache of interpretations derived from that library.

Normal production learning is handled by the trusted research promotion path. This CLI exists for maintenance, review, import, lifecycle repair, and inspection of the cache.

The model-facing capture tool can preserve candidates, but it always forces model-directed evidence to `unverified`. Manual evidence review/lifecycle administration is intentionally available only through this repository-owned local CLI; it is not a way for model inference to self-verify a claim.

Run commands from the X Omni repository root. Identify the human or import job in every mutating command with `--actor`; do not put credentials in candidate JSON or actor names.

```powershell
python scripts/automotive_knowledge_admin.py import-candidate `
  --input C:\local\candidate.json --actor "Otis"

python scripts/automotive_knowledge_admin.py review-evidence `
  --record-id akr_... --evidence-id evd_... --expected-version 2 `
  --extraction-status extracted --verification-status verified --actor "Otis"

python scripts/automotive_knowledge_admin.py promote `
  --record-id akr_... --expected-version 3 --target verified --actor "Otis"

python scripts/automotive_knowledge_admin.py read --record-id akr_...
```

The default cache database is `data/capabilities/automotive_knowledge/knowledge.sqlite`. The authoritative source root is `XOMNI_ADAS_SI_ROOT`, or `X:\ADAS SI` when the environment variable is absent. Use global `--db` or repeat `--authoritative-root` before the subcommand when maintaining another bounded repository-owned cache/source root.

Every positive review and lifecycle promotion reopens and hashes the configured authoritative local source. A missing file, a path outside the configured ADAS SI roots, or a content mismatch fails closed. A successful earlier hash is never treated as a permanent trust grant.

Verified records also receive a fresh integrity check when read. If all historically verified evidence has gone stale, exact reads return the record as effectively `evidence_backed`, retain `stored_lifecycle: verified` for audit history, and report `source_integrity.status: stale`. Default verified searches exclude it. Restoring the exact hash-matching ADAS SI source makes the record readable as verified again without rewriting its history.

Deleting/rebuilding this cache must never delete the underlying ADAS SI documents. A cache miss means only "no reusable verified interpretation is stored here"; it does not mean X lacks the source information.
