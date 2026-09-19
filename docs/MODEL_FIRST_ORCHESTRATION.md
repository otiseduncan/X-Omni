# Model-first conversational orchestration

**Current production contract — updated 2026-09-19.**

Historical experiments and retired ALLDATA/Navigator behavior are intentionally not documented as current architecture here. Git history preserves them.

## Core rule

The model owns ordinary meaning. Deterministic code owns security, execution truth, provenance, and bounded persistence.

Production turn flow:

1. Core loads persisted conversation history and trusted structured working context.
2. The model receives four permanent capabilities: `query_ciq`, `delegate_research`, `stage_action`, and `capability_search`.
3. The model answers or emits structured tool calls.
4. The gateway expands meta calls, authenticates, authorizes, validates, executes, verifies, audits, and returns bounded results.
5. Every result—including a typed miss—returns to the model. The model decides whether to answer, refine, or use another permitted capability within the round budget.

The turn loop must not inspect ordinary user prose with deterministic keyword routers to decide meaning. Capability implementations may validate their explicit structured arguments and mechanically inspect retrieved evidence.

## Permanent surface

### `query_ciq`

One read contract for Calibration IQ:

- exact RO read;
- board count/list;
- one phase;
- RO requirements;
- ADAS Map inventory;
- ADAS Map sweep status;
- SI research status;
- ADAS SI library inventory counts;
- Calibration IQ service status.

A CIQ read establishes current workflow state. It is not OEM proof of a technical requirement.

### `delegate_research`

One automotive research path for ad-hoc technical questions.

Internally it:

1. checks the verified Automotive Knowledge semantic cache for an applicable source-backed interpretation;
2. searches the authoritative ADAS SI source library when the cache does not settle the objective;
3. may search public OEM web when needed and not excluded;
4. sends every candidate through the shared semantic evidence evaluator.

Automotive Knowledge is **not** a model-selectable peer source and its maintenance tools are not in the normal `adas_operator` profile. The model may prefer/exclude ADAS SI or web; cache reuse remains an internal optimization.

Research ends with exactly one evidence outcome:

- `SATISFIED`
- `PARTIAL`
- `UNSATISFIED`

Retrieval alone never establishes a fact.

`delegate_research` never writes to Calibration IQ.

### `stage_action`

The only normal path that changes Calibration IQ or starts RO-bound acquisition work.

It performs a fresh exact-RO read where required, binds resource ids/versions, validates the operation contract, then either returns a staged request or executes through the concrete protected tool. Destructive work surfaces the real approval flow.

Special production operations:

- `acquire_adas_map` — one named RO;
- `sweep_adas_maps` — a phase/shop/board scope in background;
- `research_si` — Calibration IQ-bound service-information research and attachment in background.

### `capability_search`

Discovers uncommon daily capabilities such as calendar, tasks, files, cameras/footage, ADAS SI document display, ScrapeX ADAS Map reads/status, service starts, and worker status.

Maintenance-only semantic-cache tools stay in the `full` profile and are not discoverable to normal field chat.

## Automotive source memory

See `AUTOMOTIVE_MEMORY_ARCHITECTURE.md` for the binding memory design.

In short:

- `X:\ADAS SI` is the sole durable automotive source memory.
- ADAS SI `index.sqlite` is rebuildable OCR/search acceleration.
- Automotive Knowledge `knowledge.sqlite` is a verified semantic cache derived from ADAS SI.
- `working_context.sections.technical_research` is temporary conversation follow-up memory.
- the shared evidence evaluator is the semantic authority.

Chat and CIQ research both use the same trusted promotion boundary. SATISFIED facts and SATISFIED actual procedures may enter the cache only when their ADAS SI provenance/application/source integrity pass the promotion contract. Cache failure never invalidates the source document or blocks CIQ work.

## Calibration IQ SI research

`stage_action operation=research_si` starts the production RO-bound SI workflow:

1. Calibration IQ supplies the exact RO, VIN/vehicle identity, and active calibration requirements.
2. Each requirement searches the shared ADAS SI library.
3. Candidate documents are reviewed by `research_evidence_contract.evaluate(..., deliverable="procedure")`.
4. Only accepted actual procedures may satisfy the objective; related R&I, descriptions, diagnostics, or neighboring-system pages do not.
5. SATISFIED/eligible procedures are offered to the shared semantic-cache promotion gate.
6. Accepted procedure documents attach to the correct Calibration IQ calibration item through the operator path.
7. A fresh Calibration IQ reread decides attachment truth.
8. The background job posts one result card and status record.

There is **no ALLDATA fallback**. A library miss remains a truthful unresolved/missing result until a future supported source path is intentionally added.

## ADAS Map acquisition

ScrapeX is the mechanical ADAS Map acquisition worker only. It does not own automotive meaning and it does not retrieve SI through ALLDATA.

For a scope sweep:

1. Calibration IQ inventory identifies missing maps.
2. ScrapeX processes exact ROs in bounded batches.
3. Per-RO Calibration IQ rereads decide attachment truth.
4. The sweep persists status and posts its result back to the originating conversation.

Authentication boundaries apply only to the managed ADAS Map work browser.

## Evidence contract

`research_evidence_contract.evaluate()` is shared by ad-hoc chat research and CIQ procedure research.

Deterministic code may enforce:

- exact application/identity constraints;
- file/path/hash provenance;
- evidence shape/internal consistency;
- bounded review budgets;
- source availability and execution truth.

It must not replace semantic review with automotive keyword rules.

The model may explain evidence, but general knowledge does not override accepted vehicle-specific OEM/ADAS SI evidence without an explicit reason the evidence does not apply.

## Conversation continuity

The active conversation subject is advisory structured memory, never a routing gate.

- An active RO helps resolve follow-up references but mutable RO state must be reread before current-state claims.
- Active technical research carries the current vehicle/system/objective and accepted evidence for technical follow-ups.
- `research_findings` cards remain persisted UI/audit receipts but are deliberately excluded from generic stored-artifact prompt replay; otherwise the same research exists twice in model context and older findings can compete with the active technical subject.
- A new explicitly named RO/vehicle/system supersedes the relevant prior subject fields.

No active subject forces `tool_choice` or deterministic control flow.

## Tool-result and prompt boundaries

- All tool output is bounded before entering model context.
- Large evidence keeps useful line structure rather than becoming flattened JSON where tables lose meaning.
- Stored cards are compacted/redacted; unsafe bodies, approval receipts, execution receipts, and duplicate research findings are omitted from future prompt replay.
- The static system prompt stays cache-stable; time, active subject, background work, and compact stored artifacts live in the volatile turn-context message.

## Execution truth

A claim that work ran or changed state requires a matching current-turn result/receipt.

- queued/started is not complete;
- authentication required means nothing started;
- partial/failed/indeterminate is not success;
- mutations are idempotency/version bound;
- destructive actions use the approval system;
- current Calibration IQ state is established by fresh reads, not conversation memory.

## ALLDATA sunset

ALLDATA is preserved in source control for history but is not executable production behavior.

`core.services.alldata_sunset` and gateway policy block the retired tool family. Production ADAS SI research does not open ALLDATA, launch its browser, access credentials, or silently fall back to it.

## Adding or changing a capability

Before adding a layer, identify which responsibility it owns:

- user meaning → model;
- durable automotive source memory → ADAS SI;
- semantic judgment → shared evaluator/model;
- security/execution/provenance → deterministic Core;
- derived acceleration → index/cache only;
- conversation continuity → bounded active subject.

Do not add a second system that independently owns the same responsibility.

Add structured schemas and focused regression tests for execution/provenance/contracts. Do not encode natural-language intent routing or automotive semantic verdicts as a growing set of deterministic phrase tables.
