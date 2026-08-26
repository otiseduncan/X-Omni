# Durable automotive knowledge administration

The model-facing tools can capture candidates, but they always force evidence
to `unverified`. Evidence review is intentionally available only through the
repository-owned local administrator CLI; it is not registered in the model's
tool catalog.

Run these commands from the X Omni repository root. Identify the human or
import job in every mutating command with `--actor`; do not put credentials in
candidate JSON or actor names.

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

The default database is
`data/capabilities/automotive_knowledge/knowledge.sqlite`. The default
authoritative source root is `XOMNI_ADAS_SI_ROOT`, or `X:\ADAS SI` when the
environment variable is absent. Use global `--db` or repeat global
`--authoritative-root` before the subcommand when operating on another bounded
repository-owned store or source root.

Every positive review and each lifecycle promotion reopens and hashes the
configured authoritative local file. A missing file, a path outside the
configured roots, or a content mismatch fails closed. A successful earlier
hash is never treated as a permanent trust grant.

Verified records also receive a fresh integrity check when read. If all
historically verified evidence has gone stale, exact reads return the record as
effectively `evidence_backed`, retain `stored_lifecycle: verified` for audit
history, and report `source_integrity.status: stale`. Default verified searches
exclude it. Restoring the exact hash-matching source makes the record readable
as verified again without rewriting its history.
