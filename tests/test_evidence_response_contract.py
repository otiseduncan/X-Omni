"""Evidence is interpreted by X, kept for audit, and never becomes the answer.

These run the production orchestrator with a scripted model so the
architecture is proven deterministically: what reaches the model, what the
response contract separates, what voice may read, and how the model-owned
evidence review is wired. Whether the live model actually reasons well from
the evidence is judged in ``test_evidence_interpretation_live.py``; nothing
here asserts canned answer wording.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evidence_fixtures as fx  # noqa: E402
from core.orchestrator import evidence_review as review_mod  # noqa: E402
from core.orchestrator import prompt as prompt_mod  # noqa: E402
from core.orchestrator import response_contract as contract  # noqa: E402
from core.orchestrator.loop import Orchestrator, tool_result_json_for_model  # noqa: E402
from core.services import adas_ocr  # noqa: E402
from core.services import research_delegate  # noqa: E402
from core.services.adas_si import page_excerpt  # noqa: E402
from core.state.db import Store  # noqa: E402

KIA_QUESTION = "what can you tell me about Kia front bumpers during the long range radar calibration"


# --------------------------------------------------------------- harness


class _Router:
    active_name = "omni"

    @staticmethod
    def active_config():
        return SimpleNamespace(supports_vision=True, supports_audio=True)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"fixture {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _Registry:
    def __init__(self, results: dict[str, Any]) -> None:
        self.results = results
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def model_tools(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [_tool(name) for name in self.results]

    @staticmethod
    def tier(_name: str) -> str:
        return "read_only"

    async def invoke(self, name: str, args: dict[str, Any], **_kwargs: Any) -> Any:
        self.invocations.append((name, dict(args)))
        return self.results[name]


class _ScriptedModel:
    """Plays the conversation model, the evidence reader, and the checker."""

    supports_no_tool_self_check = False
    supports_evidence_review = True

    def __init__(
        self,
        *,
        tool_calls: list[tuple[str, dict[str, Any]]],
        draft: str,
        reading: Any = None,
        check: Any = None,
        revision: str = "",
    ) -> None:
        self.tool_calls = list(tool_calls)
        self.draft = draft
        self.reading = reading
        self.check = check
        self.revision = revision
        self.requests: list[dict[str, Any]] = []

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        *,
        tool_choice: Any = None,
        temperature: Optional[float] = None,
    ):
        names = [item["function"]["name"] for item in tools or []]
        self.requests.append(
            {
                "messages": json.loads(json.dumps(messages, default=str)),
                "tools": names,
                "tool_choice": tool_choice,
                "temperature": temperature,
            }
        )
        if review_mod.READING_TOOL_NAME in names:
            if self.reading is not None:
                yield {"type": "tool_call", "id": "r", "name": review_mod.READING_TOOL_NAME,
                       "arguments": json.dumps(self.reading)}
            return
        if review_mod.CHECK_TOOL_NAME in names:
            if self.check is not None:
                yield {"type": "tool_call", "id": "c", "name": review_mod.CHECK_TOOL_NAME,
                       "arguments": json.dumps(self.check)}
            return
        if not tools:
            yield {"type": "content", "text": self.revision}
            return
        if self.tool_calls:
            name, args = self.tool_calls.pop(0)
            yield {"type": "tool_call", "id": f"call-{name}", "name": name, "arguments": json.dumps(args)}
            return
        yield {"type": "content", "text": self.draft}


def _run_turn(tmp_path: Path, client: _ScriptedModel, registry: _Registry, question: str):
    store = Store(tmp_path / "turn.sqlite")
    conversation_id = store.create_conversation("evidence")
    message_id = store.add_message(conversation_id, "user", question)
    orchestrator = Orchestrator(
        _Router(),
        client,
        registry,
        store,
        SimpleNamespace(context_tokens=32_768, max_response_tokens=1_024),
    )

    async def drive() -> list[dict[str, Any]]:
        return [
            event
            async for event in orchestrator.run_turn(
                conversation_id,
                question,
                approval_context={
                    "session_id": "s",
                    "user_id": "local-dev",
                    "role": "owner",
                    "message_id": message_id,
                },
            )
        ]

    events = asyncio.run(drive())
    persisted = store.get_messages(conversation_id)[-1]
    store.close()
    return events, persisted


def _kia_research_result(*hits: dict[str, Any]) -> dict[str, Any]:
    handler = research_delegate.make_delegate_research(
        None,
        adas_search=lambda _query: {"status": "success", "results": list(hits)},
        knowledge_search=lambda _query: {"status": "no_result", "records": []},
    )
    return asyncio.run(
        handler({"objective": "Kia front bumper during front radar calibration", "vehicle": {"make": "Kia"}})
    )


def _chart_rows() -> list[str]:
    return fx.raw_row_lines(fx.bumper_chart_pages()["pages"][fx.CHART_TITLE])


def _done(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(event for event in events if event["type"] == "done")


STAGED_READING = {
    "otis_asked_to_see_source_text": False,
    "evidence_says": [
        {
            "source_text": "1. Remove the front bumper cover.",
            "finding": "The bumper cover comes off to inspect the radar bracket.",
            "stage": "inspection",
            "applies_to": "2023 Kia Sportage",
        },
        {
            "source_text": "- The front bumper cover is installed and all fasteners are tightened.",
            "finding": "The bumper cover is back on before calibration.",
            "stage": "calibration",
            "applies_to": "2023 Kia Sportage",
        },
    ],
}


def _check(*positions: str, **flags: bool) -> dict[str, Any]:
    return {
        "draft_claims": ["claim"],
        "findings": [{"number": index, "draft": value} for index, value in enumerate(positions, 1)],
        "problems": ["problem"] if "contradicts" in positions or any(flags.values()) else [],
        "draft_contradicts_evidence": flags.get("contradicts", False),
        "draft_uses_general_knowledge_over_evidence": flags.get("general", False),
        "draft_credits_the_source_with_claims_it_does_not_make": flags.get("attribution", False),
        "draft_flattens_stage_dependent_requirements": flags.get("flattens", False),
        "draft_pastes_raw_extraction_or_tool_status": flags.get("raw", False),
    }


# ------------------------------------------------ evidence reaches the model


def test_real_chart_rows_reach_the_model_with_their_structure() -> None:
    """The live regression: X received a flattened excerpt without any Kia rows."""

    legacy = fx.bumper_chart_pages()["legacy_flattened_excerpt"]
    assert "Carnival" not in legacy and "\n" not in legacy

    hit = fx.chart_search_hit(["kia", "front", "bumper", "radar"])
    result = _kia_research_result(hit)
    model_view = tool_result_json_for_model("delegate_research", result)

    for row in ("Carnival", "Cadenza", "Forte (BD)"):
        assert row in model_view
    header = next(line for line in model_view.splitlines() if line.startswith("2027"))
    assert "2026 | 2025" in header
    kia_row = next(line for line in model_view.splitlines() if "Forte (BD) |" in line)
    assert kia_row.count("|") >= 10, "table rows keep their columns"
    # Readable text, not an escaped JSON string, and no request echo for the
    # model to mistake for evidence.
    assert "\\n" not in model_view
    assert '"objective"' not in model_view and '"vehicle"' not in model_view
    assert "OCR confidence" in model_view


def test_page_excerpt_keeps_header_and_matching_rows_of_a_long_table() -> None:
    text = fx.bumper_chart_pages()["pages"][fx.CHART_TITLE]
    assert len(text) > 4_000
    excerpt, truncated = page_excerpt(text, ["kia", "bumper"])
    assert truncated is True
    lines = excerpt.splitlines()
    assert any(line.startswith("2027") for line in lines), "column header survives"
    assert any("Carnival" in line for line in lines)
    assert any("Forte (BDm)" in line for line in lines)


def test_short_pages_are_returned_whole() -> None:
    excerpt, truncated = page_excerpt(fx.STAGED_PROCEDURE_TEXT, ["bumper"])
    assert excerpt == fx.STAGED_PROCEDURE_TEXT.strip()
    assert truncated is False


def test_small_content_region_is_detected_for_legible_ocr() -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    def png(box: tuple[int, int, int, int]) -> bytes:
        image = Image.new("RGB", (1000, 1300), "white")
        ImageDraw.Draw(image).rectangle(box, fill="black")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    screenshot = adas_ocr.sparse_content_region(png((30, 30, 970, 330)))
    assert screenshot is not None
    left, top, right, bottom = screenshot
    assert top < 0.03 and bottom < 0.3 and right > 0.95
    assert adas_ocr.sparse_content_region(png((20, 20, 980, 1280))) is None
    assert adas_ocr.sparse_content_region(b"not a png") is None


def test_cached_full_page_read_of_a_screenshot_is_upgraded_once(tmp_path, monkeypatch) -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1000, 1300), "white")
    ImageDraw.Draw(image).rectangle((30, 30, 970, 330), fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    page_png = buffer.getvalue()

    class Library:
        def __init__(self) -> None:
            self.cache_path = tmp_path / "index.sqlite"
            import sqlite3

            with sqlite3.connect(self.cache_path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            self.source = tmp_path / "chart.pdf"
            self.source.write_bytes(b"%PDF")

        def _pages(self, _path):
            return [(1, "")]

        def render_page(self, _path, _page, width=1100):
            return page_png

        def search(self, _args):
            return {}

        def open_document(self, _args):
            return {}

    adas_ocr.install_class(Library)
    library = Library()
    mtime = library.source.stat().st_mtime_ns
    adas_ocr._store_page(  # an existing full-page read from before the region pass
        library,
        library.source,
        1,
        mtime,
        {"text": "garbled full page read of a small table", "confidence": 0.9},
        "success",
    )
    region_reads: list[tuple] = []

    def fake_region(path, page, region):
        region_reads.append(region)
        return b"region-png"

    monkeypatch.setattr(adas_ocr, "_render_region_png", fake_region)
    monkeypatch.setattr(
        adas_ocr,
        "_ocr_png",
        lambda png: {"text": "Carnival    OFF    OFF    N/A\nForte (BD)    ON    ON", "confidence": 0.93},
    )

    first = library._pages(library.source)
    second = library._pages(library.source)

    assert "Carnival" in first[0][1]
    assert first == second
    assert len(region_reads) == 1
    metadata = library.page_text_metadata(library.source, 1)
    assert metadata["pipeline_version"] == adas_ocr.OCR_REGION_PIPELINE_VERSION


# ------------------------------------ Tests 3-5: machine data stays evidence


def test_raw_extraction_is_preserved_as_collapsed_evidence_not_answer(tmp_path) -> None:
    result = _kia_research_result(fx.chart_search_hit(["kia", "bumper"]))
    answer = (
        "On the Kia chart the front bumper is marked off for most listed years on the "
        "Carnival and Cadenza, and it varies by year on the Forte."
    )
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Kia bumper"})],
        draft=answer,
        reading={
            "otis_asked_to_see_source_text": False,
            "evidence_says": [
                {"source_text": "Cadenza | OFF", "finding": "Bumper off on Cadenza.",
                 "stage": "not_stated", "applies_to": "Kia Cadenza"}
            ],
        },
        check=_check("agrees"),
    )
    events, persisted = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), KIA_QUESTION)
    done = _done(events)
    response = done["response"]

    # The model received the rows (it can reason from them) ...
    tool_message = next(
        m for m in client.requests[1]["messages"] if m.get("role") == "tool"
    )
    assert "Forte (BD) |" in tool_message["content"]

    # ... the reply is exactly X's answer, and voice reads only that answer.
    assert response["assistant_text"] == answer
    assert response["spoken_text"] == answer
    streamed = "".join(event["text"] for event in events if event["type"] == "token")
    assert streamed == answer
    for row in _chart_rows():
        assert row not in response["assistant_text"]
        assert row not in response["spoken_text"]

    # Test 4: evidence is attached, structured, collapsed by default.
    assert response["evidence_presentation"] == {
        "default_state": "collapsed",
        "count": 1,
        "attention": False,
    }
    assert response["evidence"][0]["type"] == "research_findings"
    assert response["sources"][0]["title"] == fx.CHART_TITLE
    assert response["sources"][0]["page"] == 1

    # The raw extraction is preserved for audit on the card and in the store.
    card = next(a for a in done["artifacts"] if a["type"] == "research_findings")
    assert card["presentation"] == "evidence"
    assert "Carnival" in card["data"]["findings"][0]["excerpt"]
    stored = next(a for a in persisted["artifacts"] if a["type"] == "research_findings")
    assert stored["presentation"] == "evidence"
    assert stored["data"]["findings"][0]["excerpt"] == card["data"]["findings"][0]["excerpt"]
    assert persisted["content"] == answer


def test_spoken_text_is_the_answer_without_formatting_links_tables_or_code() -> None:
    answer = (
        "**Bumper off** for the inspection, back on for calibration.\n"
        "See [the chart](/api/adas-si/document?path=x.pdf) or https://example.test/x.\n"
        "| model | 2024 |\n| --- | --- |\n| Carnival | OFF |\n"
        "```json\n{\"status\": \"success\"}\n```\n"
        "- Tie each step to its stage."
    )
    spoken = contract.spoken_text(answer)
    assert "Bumper off for the inspection, back on for calibration." in spoken
    assert "the chart" in spoken
    for fragment in ("**", "/api/", "https://", "| Carnival", "{", "```", "- Tie"):
        assert fragment not in spoken
    assert "Tie each step to its stage." in spoken

    huge_artifacts = [{"type": "research_findings", "data": {"findings": [{"excerpt": "N/A | OFF " * 500}]}}]
    response = contract.build_response("Short answer.", huge_artifacts)
    assert response["spoken_text"] == "Short answer."


def test_deliverables_stay_primary_and_unknown_cards_default_to_evidence() -> None:
    for card in ("approval_request", "generated_image", "generated_video", "website_preview",
                 "camera_request", "weather", "adas_si_document"):
        assert contract.artifact_presentation(card) == "primary"
    for card in ("research_findings", "adas_si_results", "calibration_iq_summary",
                 "calibration_iq_receipt", "scrapex", "execution_receipt", "capabilities",
                 "a_future_tool_card"):
        assert contract.artifact_presentation(card) == "evidence"


# -------------------------------------------- Test 7: ordinary CIQ answers


def test_calibration_iq_count_answers_first_with_collapsed_supporting_evidence(tmp_path) -> None:
    summary = {"status": "success", "verified": True, "count": 7, "scope": {"shop": "Macon"}}
    client = _ScriptedModel(
        tool_calls=[("calibration_iq_summary", {"shop": "Macon"})],
        draft="Seven cars in Macon still need SI.",
    )
    events, _persisted = _run_turn(
        tmp_path, client, _Registry({"calibration_iq_summary": summary}), "how many cars need SI?"
    )
    response = _done(events)["response"]

    assert response["assistant_text"] == "Seven cars in Macon still need SI."
    assert [item["type"] for item in response["evidence"]] == ["calibration_iq_summary"]
    assert response["evidence_presentation"]["default_state"] == "collapsed"
    assert response["primary"] == []
    # A Calibration IQ read is not technical evidence to interpret; no review ran.
    assert all(review_mod.READING_TOOL_NAME not in r["tools"] for r in client.requests)


# ---------------------------------------------- Test 8: failures read naturally


def test_source_failure_stays_visible_as_attention_evidence_not_a_status_dump(tmp_path) -> None:
    failed = {
        "status": "blocked",
        "verified": False,
        "source_ledger": [{"source": "adas_si", "attempted": True, "status": "error"}],
        "findings": [],
        "authentication_required": True,
        "message": "ALLDATA requires interactive sign-in; no verified finding yet.",
    }
    answer = (
        "I couldn't pull that procedure: the service library didn't answer and ALLDATA "
        "needs you to sign in first."
    )
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Telluride camera"})],
        draft=answer,
        reading={
            "otis_asked_to_see_source_text": False,
            "evidence_says": [
                {"source_text": "ALLDATA requires interactive sign-in", "finding": "Nothing was retrieved.",
                 "stage": "not_stated", "applies_to": "2022 Kia Telluride"}
            ],
        },
        check=_check("agrees"),
    )
    events, _ = _run_turn(tmp_path, client, _Registry({"delegate_research": failed}), "telluride camera procedure?")
    response = _done(events)["response"]

    assert response["assistant_text"] == answer
    assert response["evidence"][0]["attention"] is True
    assert response["evidence_presentation"]["attention"] is True
    for fragment in ("source_ledger", "authentication_required", "{", "blocked"):
        assert fragment not in response["assistant_text"]


# ------------------------------ Tests 1-2: the model-owned evidence review


def test_stage_flattening_draft_is_rewritten_from_the_models_own_reading(tmp_path) -> None:
    result = _kia_research_result(fx.staged_procedure_hit(), fx.chart_search_hit(["kia", "bumper"]))
    flattened = "The front bumper must be installed for the whole radar calibration."
    staged = (
        "The bumper comes off for the radar bracket inspection, then goes back on before "
        "you calibrate."
    )
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Kia bumper radar"})],
        draft=flattened,
        reading=STAGED_READING,
        check=_check("contradicts", "agrees", flattens=True),
        revision=staged,
    )
    events, persisted = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), KIA_QUESTION)
    done = _done(events)

    streamed = "".join(event["text"] for event in events if event["type"] == "token")
    assert streamed == staged, "the flattened draft never reaches Otis"
    assert done["response"]["assistant_text"] == staged
    assert persisted["content"] == staged
    assert done["metrics"]["evidence_review"]["revised"] is True
    assert done["metrics"]["evidence_review"]["stages"] == ["calibration", "inspection"]

    reading, check, revision = client.requests[2], client.requests[3], client.requests[4]
    # The reader sees the evidence compactly and never the draft.
    reading_text = json.dumps(reading["messages"])
    assert reading["tools"] == [review_mod.READING_TOOL_NAME]
    assert reading["tool_choice"]["function"]["name"] == review_mod.READING_TOOL_NAME
    assert reading["temperature"] == 0
    assert flattened not in reading_text
    assert "Remove the front bumper cover" in reading_text
    assert len(reading["messages"]) == 2
    # The checker compares the draft with that reading.
    assert check["tools"] == [review_mod.CHECK_TOOL_NAME]
    assert flattened in json.dumps(check["messages"])
    # The rewrite cannot call tools, carries the staged reading, and does not
    # show the model its rejected draft.
    assert revision["tools"] == []
    revision_text = json.dumps(revision["messages"])
    assert "[inspection]" in revision_text and "[calibration]" in revision_text
    assert flattened not in revision_text


def test_authoritative_evidence_contradiction_triggers_rewrite(tmp_path) -> None:
    result = _kia_research_result(fx.dynamic_only_hit())
    generic = "Set the static corner reflector 1 m behind the bumper and run the aiming routine."
    follows = "No static calibration on this one: clear codes, then drive it straight over 30 km/h for 10 minutes."
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Tucson BCW"})],
        draft=generic,
        reading={
            "otis_asked_to_see_source_text": False,
            "evidence_says": [
                {"source_text": "no static calibration is performed and no target or reflector is used.",
                 "finding": "No static calibration or target.", "stage": "calibration",
                 "applies_to": "2024 Hyundai Tucson"}
            ],
        },
        check=_check("contradicts", general=True),
        revision=follows,
    )
    events, _ = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), "tucson blind spot calibration?")
    assert _done(events)["response"]["assistant_text"] == follows


def test_sound_draft_is_released_unchanged(tmp_path) -> None:
    result = _kia_research_result(fx.staged_procedure_hit())
    draft = "Bumper off to inspect the bracket, back on and tight before the calibration."
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Kia"})],
        draft=draft,
        reading=STAGED_READING,
        check=_check("agrees", "agrees"),
        revision="SHOULD NOT BE USED",
    )
    events, _ = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), KIA_QUESTION)
    done = _done(events)
    assert done["response"]["assistant_text"] == draft
    assert done["metrics"]["evidence_review"] == {
        "reviewed": True,
        "revised": False,
        "needs_revision": False,
        "findings": 2,
        "contradicted": 0,
        "stages": ["calibration", "inspection"],
    }
    assert all(request["tools"] for request in client.requests), "no rewrite call was made"


@pytest.mark.parametrize(
    "reading, check",
    [
        (None, None),
        ({"evidence_says": []}, None),
        (STAGED_READING, {"findings": "not a list"}),
    ],
)
def test_unavailable_or_malformed_review_releases_the_draft(tmp_path, reading, check) -> None:
    result = _kia_research_result(fx.staged_procedure_hit())
    draft = "Bumper off for inspection, on for calibration."
    client = _ScriptedModel(
        tool_calls=[("delegate_research", {"objective": "Kia"})],
        draft=draft,
        reading=reading,
        check=check,
        revision="SHOULD NOT BE USED",
    )
    events, _ = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), KIA_QUESTION)
    assert _done(events)["response"]["assistant_text"] == draft


def test_clients_that_do_not_opt_in_get_no_review(tmp_path) -> None:
    result = _kia_research_result(fx.staged_procedure_hit())
    client = _ScriptedModel(tool_calls=[("delegate_research", {"objective": "Kia"})], draft="Answer.")
    client.supports_evidence_review = False
    events, _ = _run_turn(tmp_path, client, _Registry({"delegate_research": result}), KIA_QUESTION)
    assert _done(events)["response"]["assistant_text"] == "Answer."
    assert len(client.requests) == 2


# --------------------------- Test 6: evidence stays reachable when requested


def test_explicit_source_request_may_quote_raw_text_and_evidence_stays_whole() -> None:
    asked = review_mod.parse_reading({**STAGED_READING, "otis_asked_to_see_source_text": True})
    not_asked = review_mod.parse_reading(STAGED_READING)
    raw_only = review_mod.parse_check(_check("agrees", "agrees", raw=True), 2)

    assert review_mod.needs_revision(asked, raw_only) is False
    assert review_mod.needs_revision(not_asked, raw_only) is True

    captured: dict[str, Any] = {}

    class Client:
        async def stream(self, messages, tools=None):
            captured["messages"] = messages
            yield {"type": "content", "text": "quoted"}

    contradiction = review_mod.parse_check(_check("contradicts", "agrees"), 2)
    asyncio.run(review_mod.revise_answer(Client(), [{"role": "user", "content": "q"}], asked, contradiction))
    assert review_mod.RAW_RULE_SHOWN in captured["messages"][-1]["content"]

    # The conversation contract permits showing the source when Otis asks.
    section = prompt_mod.EVIDENCE_AND_CONVERSATION.casefold()
    assert "unless otis asks to see them" in section


def test_static_prompt_carries_precedence_stage_and_conversation_principles() -> None:
    sections = prompt_mod.system_prompt_sections(_Router())
    text = sections["evidence_and_conversation"]
    folded = text.casefold()
    assert folded.index("oem service information") < folded.index("calibration iq job context")
    assert folded.index("calibration iq job context") < folded.index("general knowledge")
    assert "never overrides it" in folded
    assert "stage" in folded and "never invent a stage" in folded
    assert "never paste raw ocr" in folded
    # No vehicle-specific answer is written into the prompt.
    for make in ("kia", "hyundai", "genesis", "toyota"):
        assert make not in folded


def test_evidence_review_selects_by_capability_not_wording() -> None:
    assert review_mod.turn_used_evidence({"delegate_research"})
    assert review_mod.turn_used_evidence({"query_ciq", "adas_si_open"})
    assert not review_mod.turn_used_evidence({"calibration_iq_summary", "stage_action"})
    assert not review_mod.turn_used_evidence(set())


def test_ui_primary_card_list_matches_core() -> None:
    source = (Path(__file__).resolve().parents[1] / "ui" / "src" / "lib" / "responsePresentation.js").read_text(
        encoding="utf-8"
    )
    block = source[source.index("PRIMARY_ARTIFACT_TYPES") : source.index("]);")]
    ui_types = {part.strip().strip('"') for part in block.split("[", 1)[1].split(",") if part.strip()}
    assert ui_types == set(contract.PRIMARY_ARTIFACT_TYPES)
