# Model-first conversational orchestration

## Contract

The production conversation path is:

1. Core loads persisted conversation history and trusted structured context.
2. The model receives the **permanent tool surface**: `query_ciq`,
   `delegate_research`, `stage_action`, and `capability_search`.
3. The model either answers or emits one or more structured tool calls.
4. The deterministic tool gateway expands a meta call to its concrete handler,
   then authenticates, authorizes, validates, executes, verifies, audits, and
   returns bounded tool results.
5. Every result, including an evidence miss such as `status: no_result`, is fed
   back to the model. The model decides whether to answer, refine the call, or
   try another capability within the bounded tool-round limit.

The model owns ordinary conversational meaning: intent, entity resolution,
pronoun resolution, source selection, source escalation, tool ordering, and
whether an optional persistence action should be requested. Deterministic code
owns security and truth: authentication, authorization, schemas, allowlists,
path confinement, approvals, exact-once execution, receipts, authoritative
rereads, provenance, audit, bounded serialization, and terminal media safety.

## The permanent surface

| Tool | Owns | Expands to |
|---|---|---|
| `query_ciq` | every Calibration IQ read: one RO, board count/list, phase list, RO requirements, ADAS Map inventory, sweep progress, service status | `calibration_iq_ro`, `_summary`, `_read`, `_work_prep`, `_status` (pure structural expansion in `Registry.invoke` and the loop) |
| `delegate_research` | one research objective over local ADAS SI, durable knowledge, licensed ALLDATA (ScrapeX Navigator, model-driven inside the call), public OEM web; provenance-bearing findings; any vehicle, RO or not; never writes CIQ | `core/services/research_delegate.py` |
| `stage_action` | the only write path: fresh exact-RO read, then `stage=staged` (current version, valid targets, argument contract, exact `next_call`) or `stage=executed` (receipt + final snapshot); one-RO ADAS Map acquisition; `sweep_adas_maps` for a scope | `calibration_iq_operator`, `calibration_iq_destructive` (approval-gated), `scrapex_adas_map`, all through `Registry.invoke` with the exact-RO write binding |
| `capability_search` | ranks the profile's discoverable tools against a structured query and unlocks matches for the rest of the turn | `core/tools/builtin/system.py::make_capability_search` |

Everything else in the `adas_operator` profile (calendar, tasks, files,
cameras/DVR, ADAS SI browsing, knowledge capture, ScrapeX reads, service starts)
is *discoverable*: advertised only on later rounds of a turn in which
`capability_search` matched it. The raw Calibration IQ read and write tools are
not in the profile at all; the full maintenance profile still advertises them.

Measured on the live Qwen3-Omni worker on 2026-09-11 through the real chat
template, after the ADAS Map sweep contract: the static system prompt is 1,009
tokens and the permanent catalog about 2,511, against ~1,450 + ~12,300 for the
old 33-tool reserve.

## Background work: the ADAS Map sweep

Getting every missing ADAS Map is a daily routine, and it does not fit inside a
chat turn: each acquisition takes 15-45 s, each full ScrapeX result cost the
model ~3,200 tokens, and a chat turn is cancelled when the phone's socket drops.
So `stage_action` operation `sweep_adas_maps` (scope: named phases and/or
shop, default the whole active board) starts `core/services/adas_map_sweep.py`
and returns at once:

1. Calibration IQ `adas_map_inventory` for the scope gives the exact missing
   ROs (nothing missing: return without touching ScrapeX).
2. ScrapeX readiness and sign-in are proved; exact batches of at most ten ROs
   (ScrapeX's limit) run one after another on ScrapeX's own background worker,
   the same per-item code that saves the PDF, attaches it in Calibration IQ,
   and reconciles requirements.
3. ROs that ended in `needs_operator` get one retry batch (both such ROs
   succeeded on a second try on 2026-09-11; every other failure state
   repeated identically, so those are reported, not retried).
4. Each swept RO is re-read in Calibration IQ; only CIQ decides "attached".
5. Core posts one message with an `adas_map_sweep` card to the originating
   conversation, sends a Web Push, and publishes `conversation_updated` so an
   open chat re-reads (`core/services/live_events.py`).

Sweeps persist in `state_records` (namespace `adas_map_sweep`), resume after a
Core restart, wait for sign-in if it lapses mid-sweep (one push, the window
opened once, then status-only polling), and refuse a competing single-RO
acquisition while they own the ADAS Map browser. `query_ciq` kind
`adas_map_sweep` reads progress or the result.

While a sweep runs, and for 12 hours after it finishes, the turn context
carries a **Background work** line built from Core's own record. The no-tool
review leads with that record and asks whether the draft reports an outcome it
does not contain; if the review rejects a draft without choosing a read, the
fail-closed answer is the record's own status sentence. Live acceptance showed
why: without this, "how'd the maps go?" produced "all 16 attached" five times
out of five while the sweep was still running.

## Turn-loop rules added 2026-09-11

- **No silent drops.** Calls beyond the eight-per-round limit stay in the
  assistant message and each gets a `not_run` result, so the model asks again.
- **Context guard.** Before every model call, this turn's tool results are
  shrunk structurally, largest first, until the request fits the window
  (JSON results are budgeted at 2.9 chars/token, measured).
- **Compact acquisition results.** `acquire_exact`/`process_one` results reach
  the model as a compact projection (status, identity, CIQ attachment truth,
  requirement labels); the full payload still goes to the card and store.
- **Repeat-only rounds end tool use.** A round whose calls were all served
  from the same-turn read cache goes straight to the final answer.
- **Mutations invalidate the read cache.** After any non-read tool runs, a
  re-read in the same turn returns fresh state, never the cached pre-mutation
  result.

## Prompt layout and the prefix cache

`build_messages` returns `[static system, older history..., turn context, newest
user message]`. The static system message never changes between turns; the
Qwen chat template renders the tool catalog right after it, so the catalog and
the earlier history stay in the worker's prefix cache. The clock, the active
conversation subject, and stored chat artifacts live in the *turn context*
message placed after the history, so their churn re-evaluates only the tail.
Before this layout every turn re-prefilled the whole catalog and history
(measured 2.5-6.7 s of prompt processing per turn); after it, ~0.1 s.

Per-turn cost is read from llama.cpp's own `timings` on the final streamed
chunk (`cache_n`, `prompt_n`, `prompt_ms`, `predicted_n`, `predicted_ms`), logged
as `turn metrics`, and attached to the `done` event.

## Production invariants

- `core.orchestrator.loop.Orchestrator._run` is the only production turn loop.
  Service modules must not monkey-patch or wrap it.
- The turn loop must not inspect user text with regexes, keyword tables, phrase
  lists, or fixed-priority classifiers to choose a tool or construct its args.
- The active conversation subject is advisory memory. It is rendered as data
  in the turn context and never changes control flow: the no-tool review is
  never run with a forced `tool_choice`, and nothing clears or coerces the
  model because a subject exists.
- A capability may parse and validate its explicitly supplied structured fields
  and may inspect retrieved documents, DOM state, receipts, or authoritative
  service responses during execution.
- A capability may return a stable typed miss. A miss is evidence, not a
  terminal routing decision.
- Multiple model tool calls in one round and sequential calls across rounds are
  supported. Tool results remain in model context for later calls.
- Explicit UI protocol commands, such as `/coder` and `/omni`, may remain
  deterministic because they are commands rather than conversational intent.

## Safeguards that remain deterministic

- Registry role/policy checks and blocked, confirmation-required, and
  operator-authorized tiers, applied to the *concrete* tool a meta call
  expands to.
- Approval binding to conversation, message, user, role, tool call, arguments,
  expiry, and one-time execution. An approval raised inside `stage_action`
  is recorded for the concrete protected tool (`calibration_iq_destructive`),
  never for the wrapper.
- Calibration IQ operation allowlists, idempotency and concurrency guards,
  matching execution receipts, and authoritative final rereads. `stage_action`
  reads the exact RO fresh inside the same call and binds ids and versions to
  that read before the concrete write runs.
- Pre-tool narration sealing, bounded tool-result serialization, deduplication,
  artifact persistence, and false-capability-denial checks.
- The bounded model truth review for turns that executed a mutation
  (`core/orchestrator/truth_review.py`), and the model-owned, unforced no-tool
  review for zero-tool drafts (`model_owned_no_tool_self_check`).
- Receipt-bound terminal image/video completion.

## Adding a capability

Expose one structured schema with a clear description and implement the handler
behind the deterministic gateway. Prefer typed modes and fields over a second
natural-language mini-router inside the handler. Decide whether it belongs in
the permanent surface (rare; costs every turn), the discoverable set (the
default), or the full maintenance profile only. Add execution tests for policy,
validation, receipts, and failure semantics, plus a live acceptance scenario in
`tests/test_model_first_live_acceptance.py` that asserts the expanded capability
and structured arguments across varied natural language. Do not assert exact
prose.

The static architecture test in
`tests/test_model_first_orchestration_architecture.py` prevents service-level
turn-loop wrapping, reintroduction of retired pre-router symbols, forced
`tool_choice`, and casual user-text phrase/regex tool selection in the known
production orchestration layers (including `core/tools/meta.py` and
`core/services/research_delegate.py`).

Durable automotive candidate capture is model-accessible but verification is
not. The local import, evidence-review, lifecycle-promotion, fresh-source-hash,
and stale-read procedure is documented in
[`AUTOMOTIVE_KNOWLEDGE_ADMIN.md`](AUTOMOTIVE_KNOWLEDGE_ADMIN.md).
