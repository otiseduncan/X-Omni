# Research evidence contract and ALLDATA sunset

Updated 2026-09-19.

## Why

Retrieval is not verification. A related page, a search hit, a cached claim, or a document for a neighboring year/system is not an answer merely because it was found.

Chat research and Calibration IQ procedure research therefore share one semantic evidence contract.

## One outcome, one evaluator

`core/services/research_evidence_contract.py` owns the operational research outcome:

| Outcome | Meaning |
| --- | --- |
| `SATISFIED` | Accepted evidence answers the objective for this vehicle and system. |
| `PARTIAL` | Accepted evidence answers part of it, but something required remains open. |
| `UNSATISFIED` | Nothing retrieved answers the objective. Related evidence does not count. |

`evaluate()` is shared by chat (`delegate_research`) and CIQ (`research_si` through `adas_si_research_source_cascade`).

- `deliverable="procedure"` uses the isolated, temperature-0 procedure reviewer. `ACCEPT` is SATISFIED, `ACCEPT_WITH_DEPENDENCIES` is PARTIAL, and every non-acceptance is UNSATISFIED.
- `deliverable="answer"` uses the independent answer reviewer. It identifies applicability and system, copies an exact source anchor, and only then decides whether the source answers the question.

Core validates structure, provenance, exact anchors where the deliverable supplies one, source/application identity, and internal consistency. It does not replace the model with automotive keyword rules.

## Source architecture

`X:\ADAS SI` is the durable automotive source memory.

The ADAS SI OCR/search index is derived retrieval acceleration. Automotive Knowledge (`knowledge.sqlite`) is a verified semantic cache of ADAS SI interpretations, not an independent automotive authority.

Chat research may reuse a verified cache record before rereading the PDF. If no applicable verified cache record settles the objective, ADAS SI remains the local source searched next, followed by public OEM web where allowed.

A cache miss never means the information is absent from ADAS SI.

## Symmetric semantic-cache learning

`core/services/research_knowledge_promotion.py` is the one trusted promotion boundary used by both production research paths.

### SATISFIED answer

A fact/requirement answer may enter the verified semantic cache only when:

- outcome is SATISFIED;
- the independent review is well formed;
- it says FULLY and SAME_SYSTEM;
- the source includes the exact vehicle/application;
- the source is an ADAS SI document;
- the exact answer anchor exists in Core's retrieved source text;
- year, make, and model are grounded and do not conflict with the library filing;
- the page is known;
- the local source remains inside the authoritative ADAS SI root and its SHA-256 matches.

### SATISFIED procedure

A CIQ or chat procedure may enter the same semantic cache only when:

- outcome is SATISFIED, not PARTIAL;
- the shared procedure review is well formed;
- decision is `ACCEPT`;
- classification is `ACTUAL_PROCEDURE`;
- objective match is `EXACT_MATCH`;
- the reviewer identifies the same physical unit;
- execution steps are present;
- the reviewer does not identify a different vehicle;
- the source is an identified ADAS SI document with known page and grounded YMM;
- a bounded exact source excerpt is retained with the cache record;
- the repository independently re-hashes the authoritative local file before accepting `verified`.

`research_si` now offers a SATISFIED procedure to this same gate before returning its normal result. Cache failure is deliberately non-fatal: ADAS SI remains the source and CIQ attachment work continues normally.

Model-directed candidate capture still cannot self-verify a claim.

## Cache retrieval

When a full Year/Make/Model is known, cache lookup still includes the research objective plus system and component. This keeps bm25 relevance ranking active instead of falling back to `updated_at DESC`, so a growing vehicle history does not bury the requested system outside the bounded review window.

Verified cache reads continue to re-check source integrity. If the underlying ADAS SI source changes, a historically verified cache record is no longer served as verified until the exact source is restored or the evidence is reviewed again.

## Technical research subject

Each `delegate_research` result updates `working_context.sections.technical_research` with the current vehicle, system, objective, outcome, accepted evidence, and unresolved questions. This is conversation-scoped working context for follow-ups, not another automotive evidence database.

Operator-visible research cards remain receipts/audit material. The active technical subject is the dedicated follow-up representation for the model.

## Grounding

`evidence_review` may use only accepted findings to establish vehicle facts. A source was searched only when the current result says it was searched.

## Inventory

`query_ciq kind=adas_si_library` reads the library inventory with `organize_root=false`. Vehicle counts come from `summary.vehicle_application_count`, never from search-result counts.

## ALLDATA sunset

ALLDATA remains preserved in source control but is not executable (`core/services/alldata_sunset.py`, `ALLDATA_SUNSET = True`).

- No production research path falls back to ALLDATA.
- Registry policy blocks sunset tools regardless of stale configuration entries.
- Licensed-browser launch, credentials, Navigator operations, and retired ALLDATA routes refuse before execution.
- `research_si` reports an ADAS SI miss honestly rather than silently using another provider.

Historical captured documents and verified provenance remain readable. Re-enabling ALLDATA is a deliberate future code/policy/deployment decision, not an automatic fallback.
