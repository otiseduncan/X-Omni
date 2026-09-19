# Automotive memory architecture

**Status:** binding thesis (2026-09-19)

This document is the single source of architectural truth for how X remembers
automotive facts. It supersedes any local implication, comment, or temporary
workaround that treats Automotive Knowledge as an independent second brain.

## The rule

**One durable automotive memory. One retrieval path. One semantic authority.**

Everything else is an index, a cache, or temporary working context.

```
X:\ADAS SI  (PDFs / charts / SI)
    │
    │  THE durable automotive source memory
    │  Authoritative. Immutable except by explicit, approval-gated edit.
    ▼
Search / OCR index (index.sqlite)
    │
    │  Pure acceleration — disposable and rebuildable
    ▼
Automotive Knowledge (knowledge.sqlite)
    │
    │  Verified semantic index / cache OF ADAS SI
    │  Never an independent source of truth
    │  Answers only: “Have I already produced a verified interpretation
    │  of this exact claim from the library?”
    ▼
Technical research subject + research_findings cards
    │
    │  Temporary working-memory pointers for the current conversation
    │  Not another evidence database
    ▼
X (the model)
    │
    │  One interpretation / synthesis layer
    │  Never invents durable facts; only synthesizes accepted evidence
```

## Role definitions

### 1. ADAS SI — sole durable source memory

- The physical library under `X:\ADAS SI` (or `XOMNI_ADAS_SI_ROOT`) is the
  only place automotive procedure and requirement facts permanently live.
- PDFs, charts, and SI documents remain authoritative. No derived store may
  contradict or replace them.
- The SQLite search/OCR index is derived data. It may be deleted and rebuilt
  at any time. It accelerates retrieval; it does not own meaning.

### 2. Automotive Knowledge — verified semantic cache, not a second brain

- Stores structured, provenance-backed claims that have already been extracted
  from ADAS SI and accepted by the shared semantic evaluator under the
  promotion gates.
- Exists solely so X can avoid re-reading and re-evaluating a PDF when a
  verified interpretation of the exact claim already exists.
- Lifecycle states (`discovered` → `evidence_backed` → `verified` →
  `superseded`) and source-hash integrity checks exist to keep the cache
  honest with respect to the library. They do not grant the cache independent
  authority.
- Population paths must be symmetric. Any research path that produces a
  SATISFIED, anchored answer from ADAS SI (chat or Calibration IQ) is eligible
  for promotion when the existing gates hold. Procedure-oriented work must not
  be permanently excluded from durable recall.
- Retrieval must use the dimensions the store already supports: system,
  component, requirement type, calibration type, etc. When a vehicle is known,
  the query must still carry the research objective / system / component so
  ranking is by relevance, not merely by `updated_at`.

### 3. Technical research subject and research_findings — temporary context

- `working_context.sections.technical_research` is a conversation-scoped
  pointer: current objective, vehicle, system, outcome, accepted anchors,
  unresolved items.
- Persisted `research_findings` cards are receipts for the operator and for
  later prompt context. They are not a third durable evidence store.
- The prompt may need to remind the model that the active objective and system
  supersede older topics. That reminder is a symptom of overlapping context;
  it is not a feature to expand.

### 4. Semantic authority

- Final judgment of whether retrieved text answers the objective for this
  vehicle and system belongs to the shared evaluator
  (`research_evidence_contract.evaluate`).
- Deterministic layers (topic aliases, identity guards, artifact-catalog
  mappings, ranking bonuses) may only produce candidate sets and accelerate
  search. They must not decide “this requirement is covered” or “this claim
  is verified.”
- Manufacturer-wide reference charts (bumper matrices, multi-model tables)
  are first-class library citizens. The data model and retrieval path must
  accommodate them without a growing stack of special-case bypasses.

## What is currently wrong (and why it produces the hiccups)

These are implementation drifts away from the thesis above. They are listed so
the cleanup has a clear target list; they are not the desired end state.

1. Automotive Knowledge and ADAS SI are treated as peer research sources
   instead of source + derived cache.
2. CIQ / procedure research and chat research do not populate durable semantic
   recall the same way. Promotion currently requires `deliverable == "answer"`
   and is wired only into `delegate_research`.
3. When a full year/make/model is known, the knowledge query drops system and
   component and falls back to recency ordering. Only three candidates are
   reviewed per source. Relevant verified memory can therefore be present and
   still invisible.
4. Manufacturer reference charts do not fit a pure YMM filing model, producing
   classifier exceptions, identity-guard bypasses, and ranking bonuses.
5. The same research result survives both as an active technical subject and as
   persisted evidence cards, increasing the chance of competing context.
6. Several deterministic automotive-classification layers still sit around the
   single model-owned semantic evaluator.

These defects are sufficient to produce the operator experience:
“X literally has this information. Why doesn’t she remember it?”

## Cleanup principle

Do not patch the individual hiccups one at a time. That is how the current
layering accumulated. Prefer one coherent pass that restores:

- ADAS SI as the only durable source
- Automotive Knowledge as a verified cache of that source
- one retrieval path that always carries system/component when known
- one semantic evaluator as the sole authority on meaning
- temporary conversation context that does not compete with either of the above

When in doubt, ask: “Does this layer own memory, retrieval, or meaning?”
If the answer is yes and the layer is not ADAS SI or the shared evaluator,
move the ownership back.
