"""Model-owned evidence review for answers built on retrieved technical evidence.

X owns meaning. When a turn retrieved technical evidence -- ADAS SI documents,
research findings, ALLDATA or ScrapeX extractions, knowledge records, web
research -- its draft answer is withheld until the same model has checked it:

1. **Reading.** In a compact context -- the question, the last few
   conversational messages, and this turn's tool results, *without the draft*
   and without the long system prompt and tool catalog -- the model records
   what the evidence establishes for Otis's question, one finding at a time,
   each tagged with the procedure stage the evidence itself ties it to
   (``not_stated`` when it names none) and what it applies to.
2. **Check.** In a small separate call, the model compares the draft with that
   reading, finding by finding, and says whether the draft puts general
   knowledge where the evidence speaks, collapses stage-dependent requirements
   into one rule, or pastes raw extraction Otis did not ask to see.
3. **Revision, only if needed.** The model writes the answer once more from its
   own reading, with no tools available and without the rejected draft in view.

Core validates only the *shape* of each structured result and releases the
draft unchanged when the model finds it sound or when a review step is
unavailable. Every semantic judgment is the model's; there is no phrase
matching, and no answer text is authored here.

Why (2026-09-17, conversation 257): X retrieved the Hyundai/Kia/Genesis front
radar bumper chart and answered from general knowledge that the bumper "must
be in place" during calibration. Live acceptance after the evidence and prompt
fixes still reproduced that override on the real chart in about half of runs,
and a single review call that could see the draft summarized *the draft* as
the evidence and approved it. Even with the draft removed, a reading taken in
the full 30K-token turn context invented requirements the chart does not
contain, while the same model reading the same chart in a compact context
read it correctly. Hence both the draft and the long context stay out of view.

Both review steps are forced function calls: forced structured judgements are
markedly more reliable than prose instructions on the local 30B worker, and
llama.cpp's grammar emits schema properties in declared order, so each reason
is generated before the judgement that depends on it.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("xomni.evidence_review")

# Tools whose results are technical evidence X must interpret. Structural:
# selected by capability name, never by the wording of the request.
EVIDENCE_TOOLS = frozenset(
    {
        "adas_si_open",
        "adas_si_search",
        "alldata_service_information",
        "automotive_knowledge_read",
        "automotive_knowledge_search",
        "collision_research",
        "delegate_research",
        "scrapex_read",
        "service_information_research",
        "web_research_current",
    }
)

STAGES = (
    "prerequisite",
    "inspection",
    "setup",
    "calibration",
    "verification",
    "whole_procedure",
    "not_stated",
)
DRAFT_POSITIONS = ("agrees", "contradicts", "not_mentioned")

MAX_READINGS = 8
EVIDENCE_CONTEXT_MAX_CHARS = 40_000
RECENT_CONVERSATION_MESSAGES = 4
RECENT_MESSAGE_MAX_CHARS = 1_200
# Fields in which a tool echoes the request X sent rather than returning
# evidence. Live, X searched with an invented "Sorento SX" and the reader then
# judged the chart against that vehicle instead of Otis's question.
QUERY_ECHO_KEYS = frozenset(
    {"depth", "objective", "query", "source_order", "structured_query", "vehicle"}
)
MAX_PROBLEMS = 4
MAX_ITEM_CHARS = 300
# Room for a full reading; a structured result cut off mid-JSON is invalid and
# would silently release the unchecked draft.
READING_MAX_TOKENS = 1_200
CHECK_MAX_TOKENS = 700

READING_TOOL_NAME = "evidence_reading"
CHECK_TOOL_NAME = "draft_evidence_check"

READING_TOOL = {
    "type": "function",
    "function": {
        "name": READING_TOOL_NAME,
        "description": "Internal record of what the evidence retrieved in this turn establishes.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "otis_asked_to_see_source_text": {
                    "type": "boolean",
                    "description": "True only if Otis asked to see the source, raw text, or tool detail.",
                },
                "evidence_says": {
                    "type": "array",
                    "maxItems": MAX_READINGS,
                    "description": (
                        "What the retrieved tool results establish for Otis's question, in your "
                        "own words: one short sentence per requirement or finding, grouping rows "
                        "that say the same thing. Include anything that could not be retrieved."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source_text": {
                                "type": "string",
                                "description": (
                                    "The exact line or lines of retrieved text this finding "
                                    "rests on, copied verbatim, including a table's header "
                                    "when the finding comes from a row."
                                ),
                            },
                            "finding": {
                                "type": "string",
                                "description": "What that text establishes, in plain words.",
                            },
                            "stage": {
                                "type": "string",
                                "enum": list(STAGES),
                                "description": (
                                    "The procedure stage the evidence itself ties this to; "
                                    "not_stated when the evidence names no stage."
                                ),
                            },
                            "applies_to": {
                                "type": "string",
                                "description": "Vehicles, models, or years it covers, as the evidence states.",
                            },
                        },
                        "required": ["source_text", "finding", "stage", "applies_to"],
                    },
                },
            },
            "required": ["otis_asked_to_see_source_text", "evidence_says"],
        },
    },
}

CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": CHECK_TOOL_NAME,
        "description": "Internal comparison of a withheld draft answer with the evidence reading.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            # Declared order is generation order: the draft's own claims are
            # written down before they are compared, and the problems before
            # the flags. Live, a check that compared first copied each finding
            # into "what the draft says", and one that flagged before listing
            # problems named real contradictions under all-false flags.
            "properties": {
                "draft_claims": {
                    "type": "array",
                    "maxItems": MAX_READINGS,
                    "items": {"type": "string"},
                    "description": (
                        "The specific requirements or facts the withheld draft itself states, "
                        "taken from the draft only, one per item."
                    ),
                },
                "findings": {
                    "type": "array",
                    "maxItems": MAX_READINGS,
                    "description": "One entry per numbered evidence finding, in order.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "number": {"type": "integer"},
                            "draft": {"type": "string", "enum": list(DRAFT_POSITIONS)},
                        },
                        "required": ["number", "draft"],
                    },
                },
                "problems": {
                    "type": "array",
                    "maxItems": MAX_PROBLEMS,
                    "items": {"type": "string"},
                    "description": (
                        "Each way the draft's claims conflict with or go beyond the evidence "
                        "reading, one short sentence each; empty when there are none."
                    ),
                },
                "draft_contradicts_evidence": {"type": "boolean"},
                "draft_uses_general_knowledge_over_evidence": {"type": "boolean"},
                "draft_credits_the_source_with_claims_it_does_not_make": {"type": "boolean"},
                "draft_flattens_stage_dependent_requirements": {"type": "boolean"},
                "draft_pastes_raw_extraction_or_tool_status": {"type": "boolean"},
            },
            "required": [
                "draft_claims",
                "findings",
                "problems",
                "draft_contradicts_evidence",
                "draft_uses_general_knowledge_over_evidence",
                "draft_credits_the_source_with_claims_it_does_not_make",
                "draft_flattens_stage_dependent_requirements",
                "draft_pastes_raw_extraction_or_tool_status",
            ],
        },
    },
}

READING_SYSTEM = (
    "You read retrieved automotive service evidence for an ADAS technician's assistant. "
    "Record what the retrieved tool results actually establish for Otis's question -- "
    "from those results only, never from general knowledge. For each finding, copy the "
    "exact text it rests on first, then say only what that text shows; do not reinterpret "
    "a table value into a different action. Read tables row by row against their header "
    "row and the document's title, and note rows that are unreadable. Tag each finding with "
    "the procedure stage the evidence itself ties it to (not_stated when it names none; "
    "never invent a stage). Answer for Otis's question as he asked it; the search request "
    "that produced the results is not evidence. If a source failed or found nothing, "
    "record that. Call evidence_reading exactly once."
)

CHECK_SYSTEM = (
    "You compare a withheld draft answer with a numbered reading of the evidence it must "
    "follow. First list the specific claims the draft itself makes, from the draft only. "
    "Then, for each finding, say whether the draft's claims agree with it, contradict it, "
    "or do not mention it, and list every problem. Finally say whether the draft "
    "states general knowledge where the evidence says otherwise or is silent, presents "
    "requirements as coming from the source that the reading does not contain, collapses "
    "requirements that differ between procedure stages into one rule, or pastes raw OCR, "
    "table rows, JSON, receipts, or tool status. Judge meaning, not wording; a short draft "
    "or one that adds explanation consistent with the evidence is fine. Call "
    "draft_evidence_check exactly once."
)

REVISION_INSTRUCTION = (
    "Internal revision; this is not a new user request. Your previous draft did not follow "
    "the evidence retrieved in this turn, and it was not shown to Otis.\n"
    "What the evidence establishes, by stage:\n{readings}\n"
    "Problems with the draft:\n{problems}\n"
    "Answer Otis now from what the evidence means: answer his question first, as one "
    "technician to another; tie each requirement to the stage the evidence gives it; say "
    "plainly where the evidence is unclear, incomplete, or covers other vehicles; {raw_rule} "
    "No tools are available, and do not mention this review."
)
RAW_RULE_HIDDEN = "do not paste raw extraction, table rows, JSON, or tool status."
RAW_RULE_SHOWN = "he asked to see the source, so quote the relevant source text exactly."


@dataclass(frozen=True)
class EvidenceReading:
    source_text: str
    finding: str
    stage: str
    applies_to: str


@dataclass(frozen=True)
class EvidenceReadingResult:
    asked_for_source: bool
    readings: tuple[EvidenceReading, ...]


@dataclass(frozen=True)
class DraftCheck:
    positions: tuple[str, ...]
    contradicts: bool
    general_knowledge_override: bool
    unsupported_attribution: bool
    flattens_stages: bool
    raw_extraction: bool
    problems: tuple[str, ...]


def needs_revision(reading: EvidenceReadingResult, check: DraftCheck) -> bool:
    return bool(
        "contradicts" in check.positions
        or check.contradicts
        or check.general_knowledge_override
        or check.unsupported_attribution
        or check.flattens_stages
        or (check.raw_extraction and not reading.asked_for_source)
    )


def turn_used_evidence(executed_tool_names: set[str]) -> bool:
    return bool(executed_tool_names & EVIDENCE_TOOLS)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())[:MAX_ITEM_CHARS]


def _arguments(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def parse_reading(arguments: Any) -> Optional[EvidenceReadingResult]:
    """Validate the reading's shape. Shape only -- never its meaning."""

    data = _arguments(arguments)
    if data is None or not isinstance(data.get("otis_asked_to_see_source_text"), bool):
        return None
    if not isinstance(data.get("evidence_says"), list):
        return None
    readings = []
    for item in data["evidence_says"]:
        if not isinstance(item, dict) or not _text(item.get("finding")):
            continue
        stage = str(item.get("stage") or "not_stated")
        readings.append(
            EvidenceReading(
                source_text=_text(item.get("source_text")),
                finding=_text(item.get("finding")),
                stage=stage if stage in STAGES else "not_stated",
                applies_to=_text(item.get("applies_to")),
            )
        )
    return EvidenceReadingResult(
        asked_for_source=data["otis_asked_to_see_source_text"],
        readings=tuple(readings[:MAX_READINGS]),
    )


def parse_check(arguments: Any, finding_count: int) -> Optional[DraftCheck]:
    """Validate the check's shape. Shape only -- never its meaning."""

    data = _arguments(arguments)
    if data is None:
        return None
    flags = (
        "draft_contradicts_evidence",
        "draft_uses_general_knowledge_over_evidence",
        "draft_credits_the_source_with_claims_it_does_not_make",
        "draft_flattens_stage_dependent_requirements",
        "draft_pastes_raw_extraction_or_tool_status",
    )
    if not all(isinstance(data.get(flag), bool) for flag in flags):
        return None
    if not isinstance(data.get("findings"), list):
        return None
    positions = ["not_mentioned"] * finding_count
    for item in data["findings"]:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        position = item.get("draft")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and 1 <= number <= finding_count
            and position in DRAFT_POSITIONS
        ):
            positions[number - 1] = position
    return DraftCheck(
        positions=tuple(positions),
        contradicts=data[flags[0]],
        general_knowledge_override=data[flags[1]],
        unsupported_attribution=data[flags[2]],
        flattens_stages=data[flags[3]],
        raw_extraction=data[flags[4]],
        problems=tuple(_text(item) for item in (data.get("problems") or []) if _text(item))[
            :MAX_PROBLEMS
        ],
    )


def _stream_kwargs(client: Any, **kwargs: Any) -> dict[str, Any]:
    """Pass only the keyword arguments this client's stream accepts."""

    try:
        parameters = inspect.signature(client.stream).parameters
    except (TypeError, ValueError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in parameters}


async def _forced_call(
    client: Any,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    max_tokens: int,
) -> Any:
    name = tool["function"]["name"]
    arguments: Any = None
    async for event in client.stream(
        messages,
        tools=[tool],
        **_stream_kwargs(
            client,
            max_tokens=max_tokens,
            tool_choice={"type": "function", "function": {"name": name}},
            temperature=0,
        ),
    ):
        if event.get("type") == "tool_call" and event.get("name") == name and arguments is None:
            arguments = event.get("arguments")
    return arguments


def _readable_result(content: Any) -> str:
    """Tool content as the reader sees it: text stays text, JSON loses request echoes."""

    raw = str(content or "")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if key not in QUERY_ECHO_KEYS}
    return json.dumps(value, ensure_ascii=False, default=str)


def evidence_context(messages: list[dict[str, Any]]) -> Optional[str]:
    """The question, recent conversation, and this turn's tool results only.

    Plain text, not a JSON envelope: JSON would escape the line breaks that
    make a table readable.
    """

    last_user = max(
        (index for index, message in enumerate(messages) if message.get("role") == "user"),
        default=-1,
    )
    if last_user < 0:
        return None
    results = []
    used = 0
    for message in messages[last_user + 1 :]:
        if message.get("role") != "tool":
            continue
        content = _readable_result(message.get("content"))
        name = message.get("name") or "tool"
        if used + len(content) > EVIDENCE_CONTEXT_MAX_CHARS:
            results.append(f"[{name}] (omitted for length)")
            continue
        used += len(content)
        results.append(f"[{name}]\n{content}")
    if not results:
        return None
    recent = [
        f"{message['role']}: {str(message.get('content') or '')[:RECENT_MESSAGE_MAX_CHARS]}"
        for message in messages[:last_user]
        if message.get("role") in {"user", "assistant"}
        and str(message.get("content") or "").strip()
        and not message.get("tool_calls")
    ][-RECENT_CONVERSATION_MESSAGES:]
    sections = []
    if recent:
        sections.append("Recent conversation:\n" + "\n".join(recent))
    sections.append("Otis's question:\n" + str(messages[last_user].get("content") or "")[:2_000])
    sections.append("Retrieved tool results:\n\n" + "\n\n".join(results))
    return "\n\n".join(sections)


async def read_evidence(
    client: Any, messages: list[dict[str, Any]]
) -> Optional[EvidenceReadingResult]:
    """Step 1: the model reads the turn's evidence, compactly, with no draft in view."""

    context = evidence_context(messages)
    if context is None:
        return None
    try:
        arguments = await _forced_call(
            client,
            [
                {"role": "system", "content": READING_SYSTEM},
                {"role": "user", "content": context},
            ],
            READING_TOOL,
            READING_MAX_TOKENS,
        )
    except Exception:  # noqa: BLE001 - an unavailable review releases the draft as before
        log.warning("Evidence reading failed; releasing the draft.", exc_info=True)
        return None
    return parse_reading(arguments)


def _numbered(reading: EvidenceReadingResult) -> list[dict[str, Any]]:
    return [
        {
            "number": index,
            "finding": item.finding,
            "stage": item.stage,
            "applies_to": item.applies_to,
        }
        for index, item in enumerate(reading.readings, start=1)
    ]


async def check_draft(
    client: Any, question: str, reading: EvidenceReadingResult, draft: str
) -> Optional[DraftCheck]:
    """Step 2: a small, separate comparison of the draft with that reading."""

    payload = {
        "otis_question": question,
        "withheld_draft": draft,
        "evidence_reading": _numbered(reading),
    }
    try:
        arguments = await _forced_call(
            client,
            [
                {"role": "system", "content": CHECK_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            CHECK_TOOL,
            CHECK_MAX_TOKENS,
        )
    except Exception:  # noqa: BLE001
        log.warning("Draft evidence check failed; releasing the draft.", exc_info=True)
        return None
    return parse_check(arguments, len(reading.readings))


async def revise_answer(
    client: Any,
    messages: list[dict[str, Any]],
    reading: EvidenceReadingResult,
    check: DraftCheck,
) -> str:
    """Step 3: one tool-less answer from the model's own reading. '' on failure."""

    readings = "\n".join(
        f"- [{item.stage}] {item.finding}"
        + (f" (applies to: {item.applies_to})" if item.applies_to else "")
        + (f' -- source: "{item.source_text}"' if item.source_text else "")
        for item in reading.readings
    )
    problems = "\n".join(f"- {problem}" for problem in check.problems) or (
        "- The draft did not follow the retrieved evidence."
    )
    instruction = REVISION_INSTRUCTION.format(
        readings=readings,
        problems=problems,
        raw_rule=RAW_RULE_SHOWN if reading.asked_for_source else RAW_RULE_HIDDEN,
    )
    text = ""
    try:
        async for event in client.stream(
            [*messages, {"role": "user", "content": instruction}], tools=None
        ):
            if event.get("type") == "content":
                text += str(event.get("text") or "")
    except Exception:  # noqa: BLE001
        log.warning("Evidence revision failed; releasing the draft.", exc_info=True)
        return ""
    return text.strip()


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"][:2_000]
    return ""


async def reviewed_answer(
    client: Any, messages: list[dict[str, Any]], draft: str
) -> tuple[str, dict[str, Any]]:
    """Return the answer to release and a compact record of the review."""

    record: dict[str, Any] = {"reviewed": False, "revised": False}
    if not str(draft or "").strip():
        return draft, record
    reading = await read_evidence(client, messages)
    if reading is None or not reading.readings:
        # Nothing established to hold the draft against.
        return draft, record
    check = await check_draft(client, _latest_user_text(messages), reading, draft)
    if check is None:
        return draft, record
    revise = needs_revision(reading, check)
    record.update(
        {
            "reviewed": True,
            "needs_revision": revise,
            "findings": len(reading.readings),
            "contradicted": check.positions.count("contradicts"),
            "stages": sorted({item.stage for item in reading.readings}),
        }
    )
    if not revise:
        return draft, record
    revised = await revise_answer(client, messages, reading, check)
    if not revised:
        return draft, record
    record["revised"] = True
    return revised, record
