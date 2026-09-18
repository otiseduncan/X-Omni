# Research evidence contract and ALLDATA sunset (2026-09-17)

## Why

Retrieval had become verification. `delegate_research` marked a source
"verified" whenever it returned findings (`status in {"success",
"partial_success"} and findings`), so a related bumper removal page could stand
as the answer to a calibration question, and a later Kia K4 document could read
as evidence for a 2021 K4. Calibration IQ research (`research_si`) had a strong
isolated reviewer; ordinary chat had none.

## One outcome, one evaluator

`core/services/research_evidence_contract.py` owns the only operational research
outcome:

| Outcome | Meaning |
| --- | --- |
| `SATISFIED` | Accepted evidence answers the objective for this vehicle and system, anchored to exact source text. |
| `PARTIAL` | Accepted evidence answers part of it; something the answer depends on is open (`ACCEPT_WITH_DEPENDENCIES`, a stage the source does not cover, unstated applicability). |
| `UNSATISFIED` | Nothing retrieved answers it, including a related document about the right part. |

`evaluate()` is the shared evaluator. Chat (`delegate_research`) and CIQ
(`research_si` through `adas_si_research_source_cascade`) both call it:

- `deliverable="procedure"` goes to `research_semantic_review` (the existing
  isolated, temperature-0 procedure review). `ACCEPT` is SATISFIED,
  `ACCEPT_WITH_DEPENDENCIES` is PARTIAL, and everything else is UNSATISFIED.
- `deliverable="answer"` goes to a forced `report_evidence_answer_review` call.
  The reviewer names the question, reads what the source says it covers,
  copies the exact anchor text, and only then judges `answers_objective`.

Core checks only shape and consistency. The anchor must really be in the
retrieved text. A structured Year/Make/Model filing that disagrees with the
request vetoes an acceptance. No model, a model error, prose instead of the
tool, or a malformed verdict all end UNSATISFIED.

## Chat research

`delegate_research` searches durable knowledge first, then the ADAS SI library,
then the public OEM web. It sends each candidate to the evaluator (at most 3
per source and 6 per call) and stops at the first SATISFIED source. The result
carries `outcome`, per-finding `accepted` and `evaluation`, and `unresolved`.
`verified` is true only for SATISFIED.

## Durable learning

`core/services/research_knowledge_promotion.py` is the trusted promotion path.
The model-facing facade still cannot self-verify. A research answer becomes
`verified` knowledge only when all of these hold:

- the outcome is SATISFIED for an answer;
- the review is well formed and says FULLY, SAME_SYSTEM, and that the source
  includes this vehicle;
- the source is an ADAS SI library document;
- the anchor is in the text Core retrieved;
- year, make, and model are known and the library's filing agrees;
- the page is known;
- the repository's own re-hash of the file matches.

A later question about the same vehicle is answered from that record without
searching or reviewing the library again. If the file changes on disk, the
record stops being served as verified.

## Technical research subject

Each `delegate_research` result updates
`working_context.sections.technical_research` on the conversation subject. It
records the objective, vehicle, system, deliverable, outcome, what accepted
evidence established (with the anchor), what was retrieved but not accepted,
and what is still open. It joins an active RO without replacing it and
survives an RO change. It is rendered in its own bounded prompt section,
"Active technical research", so follow-ups resolve against it.

## Grounding

`evidence_review` reads only what accepted findings establish. Its check flags
`draft_states_vehicle_facts_no_accepted_evidence_establishes`, and a flagged
draft is rewritten without tools.

## Inventory

`query_ciq kind=adas_si_library` reads the library inventory with
`organize_root=false`, so it files nothing. Vehicle counts come from
`summary.vehicle_application_count`, never from search counts.

## ALLDATA sunset

ALLDATA is preserved in source control and is not executable
(`core/services/alldata_sunset.py`, `ALLDATA_SUNSET = True`):

- No source order, schema, prompt, or profile names it, and
  `capability_search` cannot find it.
- `Registry.tier()` returns `blocked` for every tool in `SUNSET_TOOLS`,
  whatever `config/tools.yaml` says.
- The licensed browser launch, the Credential Manager read and write, every
  ScrapeX Navigator call (task, observe, act, signals, screenshot, capture),
  `run_navigator_search`, the vehicle-first and agent searches, and the Quick
  Reference collectors all raise `AlldataSunset` before touching anything.
- The ALLDATA HTTP routes are not installed. The UI's historical access card
  renders a retired notice.
- `research_si` has no ALLDATA fallback. Work-prep `ro_si_acquire` and
  `queue_next` return a sunset result.
- `alldata_navigator_enabled` is `False` and is no longer read from the
  environment.

Historical captures and verified claims keep their ALLDATA provenance.
Re-enabling ALLDATA takes a deliberate code change to the constant, the policy,
the schemas, and the source order, followed by a redeploy.

The retired runtime's tests are kept and skipped with an explicit reason.
`tests/test_alldata_sunset.py` proves every guard.
