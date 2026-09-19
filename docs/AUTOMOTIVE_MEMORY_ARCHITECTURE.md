# Automotive memory architecture

**Status:** binding production architecture (2026-09-19)

This document is the source of architectural truth for how X remembers automotive facts. It supersedes comments or temporary workarounds that treat Automotive Knowledge as an independent second brain.

## The rule

**One durable automotive memory. One research path. One semantic authority.**

Everything else is an index, cache, receipt, or temporary working context.

```text
X:\ADAS SI  (PDFs / charts / SI)
    │
    │  THE durable automotive source memory
    │  Authoritative source material
    ▼
Search / OCR index (index.sqlite)
    │
    │  Disposable/rebuildable retrieval acceleration
    ▼
Automotive Knowledge (knowledge.sqlite)
    │
    │  Verified semantic cache OF ADAS SI
    │  Never an independent source of truth
    ▼
Technical research subject
    │
    │  Conversation-scoped follow-up context
    ▼
X + shared evidence evaluator
    │
    │  One interpretation / synthesis authority
```

## 1. ADAS SI — sole durable source memory

- The physical library under `X:\ADAS SI` (or `XOMNI_ADAS_SI_ROOT`) is the permanent automotive source library.
- PDFs, charts, manufacturer matrices, and SI documents remain authoritative. No derived store may contradict or replace them.
- `data/capabilities/adas_si/index.sqlite` is derived OCR/search data. It may be rebuilt without losing automotive knowledge.
- Manufacturer-wide requirement charts are first-class ADAS SI documents even when they do not map to one Year/Make/Model application.

## 2. Automotive Knowledge — semantic cache, not another brain

`knowledge.sqlite` exists to avoid repeatedly reading and interpreting the same source when an exact, source-backed interpretation has already been established.

A verified cache record must retain provenance to ADAS SI and pass fresh source-integrity checks. If its source file changes or disappears, the record stops being served as verified. The PDF remains the authority.

The cache answers one question:

> Have we already established a verified interpretation of this exact automotive claim or procedure from ADAS SI?

It does **not** answer:

> Does X know this?

ADAS SI answers that.

### Population is now symmetric

Both production research paths use the same trusted promotion boundary:

- chat `delegate_research`: SATISFIED fact/requirement answers from ADAS SI may enter the cache;
- Calibration IQ `stage_action research_si`: SATISFIED actual procedures from ADAS SI may enter the same cache.

Procedure promotion is not allowed merely because a page was retrieved. The shared semantic review must establish an actual procedure, exact objective match, same unit, execution steps present, exact vehicle compatibility, and a complete `ACCEPT` outcome. The local ADAS SI file is re-hashed before the cache accepts it.

Cache write failure never blocks or changes the research/attachment result. The source PDF is still the memory.

### Cache identity comes from evidence, not the question

A cache record must not become unique merely because a technician phrased the same question differently or because a generic procedure was first encountered on a particular repair order.

The fingerprint-defining fields therefore use:

- grounded Year/Make/Model application;
- system/component;
- ADAS SI source document + page;
- exact accepted source anchor for fact answers, or accepted source procedure identity for procedures;
- stable procedure classification where applicable.

The original research question, RO-specific VIN, and conversational trim are retained only in evidence metadata for audit. They do not define the reusable cache record. This prevents a generic 2025 Kia K4 procedure from becoming one memory per VIN or one memory per paraphrased question.

### Retrieval is relevance-aware

When Year/Make/Model is known, semantic-cache lookup still carries the research objective plus system and component. That forces relevance ranking rather than `updated_at` ordering and prevents a growing vehicle history from hiding the requested BSM/radar/camera record outside the review budget.

## 3. Search/index code may narrow candidates, not decide meaning

Deterministic code may:

- parse/normalize identity;
- classify a file broadly for storage;
- OCR/index text;
- rank candidate documents;
- enforce source hash, path, provenance, and application constraints.

It must not independently decide that a candidate answers the automotive question. `research_evidence_contract.evaluate()` is the shared semantic authority for chat and CIQ research.

This is the same boundary used elsewhere in X Omni: mechanical systems find and prove evidence; X interprets what that evidence means.

## 4. Conversation memory is not automotive source memory

`working_context.sections.technical_research` carries the current vehicle/system/objective and accepted findings so normal follow-ups can resolve references without starting over.

`research_findings` and `adas_si_research` cards are operator-visible receipts. They may persist in chat history for audit and UI presentation, but they do not become another source of automotive truth. The active technical subject is the dedicated model-facing follow-up representation.

## 5. Current implementation invariants

As of 2026-09-19:

1. **ADAS SI remains the only durable automotive source library.**
2. **Automotive Knowledge is a source-backed semantic cache.** Its verified records still re-hash ADAS SI on trust-sensitive reads/transitions.
3. **Cache identity is source/evidence based, not question/VIN based.** RO-specific context stays provenance-only unless a future source contract explicitly proves narrower applicability.
4. **Knowledge recall uses YMM + system/component + objective relevance**, rather than YMM + recency alone.
5. **Chat and CIQ procedure research share one semantic evaluator.**
6. **Chat and CIQ SATISFIED ADAS SI research share one trusted cache-promotion gate.**
7. **PARTIAL/UNSATISFIED work is not promoted as verified memory.**
8. **Cache failure is non-fatal.** It can never make valid ADAS SI or CIQ work fail.
9. **Reference charts remain ADAS SI source documents.** They do not need to be forced into a fake single-vehicle application to be durable memory.

## 6. What not to rebuild

Do not add another persistent store to solve a recall problem. Do not make ScrapeX, the ADAS SI index, Calibration IQ, conversation cards, or the semantic cache a second automotive authority.

Before adding a layer, ask:

> Does this layer own memory, retrieval, or meaning?

- Durable source memory belongs to **ADAS SI**.
- Semantic judgment belongs to the **shared evaluator / X**.
- Derived stores may accelerate those responsibilities but may not compete with them.

That is the boundary that keeps X Omni from drifting back into the dual-layer/spaghetti architecture this cleanup was intended to remove.
