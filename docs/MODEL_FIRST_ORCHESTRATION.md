# Model-first conversational orchestration

## Contract

The production conversation path is:

1. Core loads persisted conversation history and trusted structured context.
2. The model receives the available tool schemas.
3. The model either answers or emits one or more structured tool calls.
4. The deterministic tool gateway authenticates, authorizes, validates, executes,
   verifies, audits, and returns bounded tool results.
5. Every result, including an evidence miss such as `status: no_result`, is fed
   back to the model. The model decides whether to answer, refine the call, or
   try another capability within the bounded tool-round limit.

The model owns ordinary conversational meaning: intent, entity resolution,
pronoun resolution, source selection, source escalation, tool ordering, and
whether an optional persistence action should be requested. Deterministic code
owns security and truth: authentication, authorization, schemas, allowlists,
path confinement, approvals, exact-once execution, receipts, authoritative
rereads, provenance, audit, bounded serialization, and terminal media safety.

## Production invariants

- `core.orchestrator.loop.Orchestrator._run` is the only production turn loop.
  Service modules must not monkey-patch or wrap it.
- The turn loop must not inspect user text with regexes, keyword tables, phrase
  lists, or fixed-priority classifiers to choose a tool or construct its args.
- A capability may parse and validate its explicitly supplied structured fields
  and may inspect retrieved documents, DOM state, receipts, or authoritative
  service responses during execution.
- A capability may return a stable typed miss. A miss is evidence, not a
  terminal routing decision.
- Multiple model tool calls in one round and sequential calls across rounds are
  supported. Tool results remain in model context for later calls.
- Explicit UI protocol commands, such as `/coder` and `/omni`, may remain
  deterministic because they are commands rather than conversational intent.
- Active-subject continuity is trusted structured context. Core must not rewrite
  a new user message by regex or prepend a guessed prior subject.

## Safeguards that remain deterministic

The model-first rule does not weaken execution controls. Keep these boundaries:

- Registry role/policy checks and blocked, confirmation-required, and
  operator-authorized tiers.
- Approval binding to conversation, message, user, role, tool call, arguments,
  expiry, and one-time execution.
- Calibration IQ operation allowlists, idempotency and concurrency guards,
  matching execution receipts, and authoritative final rereads.
- Pre-tool narration sealing, bounded tool-result serialization, deduplication,
  artifact persistence, and false-capability-denial checks.
- Receipt-derived operator summaries where model prose could otherwise claim an
  unverified mutation.
- Receipt-bound terminal image/video completion. A completed media result must
  not return to a model loop that can implicitly launch another media tool.

## Adding a capability

Expose one structured schema with a clear description and implement the handler
behind the deterministic gateway. Prefer typed modes and fields over a second
natural-language mini-router inside the handler. Add execution tests for policy,
validation, receipts, and failure semantics, plus model acceptance cases that
assert the selected capability and structured arguments across varied natural
language. Do not assert exact prose.

The static architecture test in
`tests/test_model_first_orchestration_architecture.py` prevents service-level
turn-loop wrapping, reintroduction of retired pre-router symbols, and casual
user-text phrase/regex tool selection in the known production orchestration
layers. It remains intentionally scoped so structured protocol, receipt, and
source parsing are not banned.

Durable automotive candidate capture is model-accessible but verification is
not. The local import, evidence-review, lifecycle-promotion, fresh-source-hash,
and stale-read procedure is documented in
[`AUTOMOTIVE_KNOWLEDGE_ADMIN.md`](AUTOMOTIVE_KNOWLEDGE_ADMIN.md).
