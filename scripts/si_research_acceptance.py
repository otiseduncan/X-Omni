r"""Live ADAS service-information research acceptance harness.

Runs deliberately varied real SI research objectives through the model-driven
ScrapeX Navigator against the live ALLDATA session and the live Qwen worker,
and records what actually happened: verification, semantic review, browser
actions, model rounds, repeated/stale actions, context tokens, wall time, and
the evidence reached. It exists so the current Navigator can be measured
before a change and re-measured after it with the same cases.

Nothing here judges relevance. The report carries the evidence (title, URL,
text head, review result when present) for a person to judge and records the
mechanical measures the runtime can prove. By default the command exits nonzero
unless every selected case has an exact VIN, X's accepted semantic review, a
complete dependency set, extracted evidence, and (with ``--capture``) a saved
capture. ``--allow-incomplete`` is only for diagnostic/baseline recording.

Usage::

    .venv\Scripts\python.exe scripts\si_research_acceptance.py ^
        --cases scripts\si_research_cases.json --out data\acceptance\phase-b.json ^
        --label phase-b --capture --vin-from-ciq [--max-turns 40] [--only case-id]

Requires the local worker, ScrapeX, and an authenticated ALLDATA Navigator
profile. A case that cannot run reports its blocker; nothing is invented.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MeasuredModelClient:
    """Non-streaming completions over the worker, recording each call's cost."""

    supports_no_tool_self_check = True

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        timeout: float = 300.0,
        temperature: float = 0.1,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.calls: list[dict[str, Any]] = []

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(
            timeout=httpx.Timeout(15.0, read=self.timeout, write=60.0, pool=15.0),
            trust_env=False,
        ) as client:
            response = client.post(f"{self.endpoint}/chat/completions", json=payload)
        if response.status_code != 200:
            raise RuntimeError(
                f"worker returned HTTP {response.status_code}: {response.text[:800]}"
            )
        return response.json()

    async def stream(self, messages, tools=None, max_tokens=None, *, tool_choice=None):
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or 640,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        started = time.perf_counter()
        raw = await asyncio.to_thread(self._post, payload)
        elapsed = time.perf_counter() - started
        message = raw["choices"][0]["message"]
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        timings = raw.get("timings") if isinstance(raw.get("timings"), dict) else {}
        images = sum(
            1
            for item in messages
            if isinstance(item.get("content"), list)
            for part in item["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        )
        self.calls.append(
            {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": timings.get("cache_n"),
                "prompt_ms": timings.get("prompt_ms"),
                "predicted_ms": timings.get("predicted_ms"),
                "wall_s": round(elapsed, 2),
                "messages": len(messages),
                "images": images,
                "tools": [item["function"]["name"] for item in (tools or [])],
            }
        )
        content = message.get("content")
        if isinstance(content, str) and content:
            yield {"type": "content", "text": content}
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            yield {
                "type": "tool_call",
                "id": raw_call.get("id") or "",
                "name": function.get("name") or "",
                "arguments": function.get("arguments") or "{}",
            }
        yield {"type": "usage", "usage": usage, "timings": timings}

    async def complete(self, messages, max_tokens: int = 320, temperature: float = 0.1) -> str:
        raw = await asyncio.to_thread(
            self._post,
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        return str(raw["choices"][0]["message"].get("content") or "").strip()


def worker_target() -> tuple[str, str]:
    raw = json.loads((ROOT / "config" / "workers.json").read_text(encoding="utf-8"))
    name = str(raw["default_worker"])
    config = raw["workers"][name]
    endpoint = os.getenv("XOMNI_MODEL_BASE_URL") or (
        f"http://{config.get('host', '127.0.0.1')}:{int(config['port'])}/v1"
    )
    return endpoint, os.getenv("XOMNI_MODEL_ALIAS") or str(config["alias"])


_NON_ACTION_TRACE = {None, "verify_after_extract", "semantic_review", "dependency"}


def _trace_measures(trace: list[dict[str, Any]]) -> dict[str, Any]:
    actions = [
        item
        for item in trace
        if (
            (isinstance(item.get("turn"), int) and item.get("turn", -1) >= 0)
            or item.get("mechanical_preflight") is True
        )
        and item.get("action") not in _NON_ACTION_TRACE
    ]
    errors = [str(item.get("error") or "") for item in actions if item.get("error")]
    stale = sum(
        1
        for text in errors
        if "no longer resolves" in text or "stale" in text.casefold()
    )
    missing_ref = sum(
        1
        for text in errors
        if "not a ref" in text
        or "requires a non-empty 'ref'" in text
        or "unknown_ref" in text
    )
    repeated = 0
    previous = None
    for item in actions:
        signature = (
            item.get("action"),
            json.dumps(item.get("args"), sort_keys=True, default=str),
        )
        if signature == previous:
            repeated += 1
        previous = signature
    rounds = len({item["turn"] for item in actions})
    kinds: dict[str, int] = {}
    for item in actions:
        kinds[str(item.get("action"))] = kinds.get(str(item.get("action")), 0) + 1
    return {
        "model_rounds": rounds,
        "browser_actions": sum(1 for item in actions if not item.get("error")),
        "action_kinds": kinds,
        "errors": len(errors),
        "repeated_actions": repeated,
        "stale_target_failures": stale,
        "missing_ref_failures": missing_ref,
        "visual_actions": sum(
            kinds.get(kind, 0) for kind in ("click_visual", "click_mark", "observe_marks")
        ),
    }


async def run_case(
    case: dict[str, Any],
    *,
    settings: Any,
    endpoint: str,
    model: str,
    capture: bool,
    max_turns: int,
) -> dict[str, Any]:
    from core.services import research_navigator_agent as agent

    client = MeasuredModelClient(endpoint, model)
    target = {"year": case["year"], "make": case["make"], "model": case["model"]}
    if case.get("trim"):
        target["trim"] = case["trim"]
    if case.get("vin"):
        target["vin"] = case["vin"]
    kwargs: dict[str, Any] = dict(
        client=client,
        settings=settings,
        provider="alldata",
        target=target,
        topic=case["topic"],
        max_turns=max_turns,
        capture=capture,
    )
    signature = inspect.signature(agent.run_navigator_search)
    if "objective" in signature.parameters:
        kwargs["objective"] = {
            "objective": case["topic"],
            "system": case.get("system"),
            "component": case.get("component"),
            "repair_order": case.get("ro"),
        }
    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    preflight_error = str(case.get("_preflight_error") or "").strip()
    if preflight_error:
        result = {}
        error = f"PreflightError: {preflight_error}"
    else:
        try:
            result = await agent.run_navigator_search(**kwargs)
            error = None
        except Exception as exc:  # noqa: BLE001 - the harness records, never hides
            result = {}
            error = f"{type(exc).__name__}: {exc}"
    wall = time.perf_counter() - started
    trace = result.get("agent_trace") or []
    measures = _trace_measures(trace)
    prompt_tokens = [call.get("prompt_tokens") or 0 for call in client.calls]
    evidence_text = str(result.get("extracted_text") or "")
    documents = result.get("documents") or []
    review = result.get("semantic_review") or result.get("review")
    receipt = result.get("research_receipt")
    return {
        "case": case,
        "started_at": started_at,
        "wall_s": round(wall, 1),
        "error": error,
        "status": result.get("status"),
        "verified": result.get("verified"),
        "complete": result.get("complete"),
        "stopped_reason": result.get("agent_stopped_reason"),
        "verification_reason": result.get("verification_reason"),
        "captured": result.get("captured"),
        "context_degraded": result.get("context_degraded"),
        "task_id": result.get("task_id"),
        "task_ids": result.get("task_ids"),
        "source_url": result.get("source_url"),
        "evidence_title": result.get("evidence_title")
        or (result.get("verification") or {}).get("title"),
        "evidence_chars": len(evidence_text),
        "evidence_head": evidence_text[:700],
        "evidence_tail": evidence_text[-400:] if len(evidence_text) > 700 else "",
        "semantic_review": review,
        "documents": documents,
        "dependencies": result.get("dependencies"),
        "receipt": receipt,
        "model_calls": len(client.calls),
        "prompt_tokens_max": max(prompt_tokens) if prompt_tokens else 0,
        "prompt_tokens_total": sum(prompt_tokens),
        "model_wall_s": round(sum(call.get("wall_s") or 0 for call in client.calls), 1),
        "images_sent": sum(call.get("images") or 0 for call in client.calls),
        **measures,
        "visited_urls": sorted(
            {
                str((item.get("result") or {}).get("url"))
                for item in trace
                if isinstance(item.get("result"), dict)
                and (item.get("result") or {}).get("url")
            }
        )[:40],
        "trace_tail": trace[-12:],
    }


def acceptance_failures(
    item: dict[str, Any], *, capture: bool, require_vin: bool
) -> list[str]:
    """Strict, non-semantic definition of a completed acceptance case.

    This never re-judges the procedure. It requires X's independent semantic
    acceptance plus complete mechanical/capture evidence, leaving the case's
    explicit ``expect`` text visible for the senior human accuracy review.
    """
    failures: list[str] = []
    case = item.get("case") if isinstance(item.get("case"), dict) else {}
    if item.get("error"):
        failures.append(str(item["error"]))
    if require_vin and not str(case.get("vin") or "").strip():
        failures.append("exact VIN was not resolved from Calibration IQ")
    if item.get("verified") is not True:
        failures.append("X did not accept a mechanically verified procedure")
    if item.get("complete") is not True:
        failures.append("the procedure/dependency set is incomplete")
    review = item.get("semantic_review") if isinstance(item.get("semantic_review"), dict) else {}
    accepted_decisions = {"ACCEPT", "ACCEPT_WITH_DEPENDENCIES"}
    documents = item.get("documents") if isinstance(item.get("documents"), list) else []
    review_accepted = review.get("decision") in accepted_decisions or any(
        isinstance(document, dict)
        and document.get("accepted") is True
        and document.get("decision") in accepted_decisions
        for document in documents
    )
    if not review_accepted:
        failures.append("independent semantic review did not accept the candidate")
    if not str(item.get("evidence_title") or "").strip():
        failures.append("no evidence title was returned")
    if int(item.get("evidence_chars") or 0) <= 0:
        failures.append("no extracted evidence text was returned")
    if capture and item.get("captured") is not True:
        failures.append("accepted evidence was not captured")
    return list(dict.fromkeys(failures))


def markdown_summary(report: dict[str, Any]) -> str:
    lines = [
        f"# SI research acceptance: {report['label']}",
        "",
        f"Run at {report['run_at']} against {report['worker']} with capture={report['capture']}.",
        "",
        f"Operational result: **{report['passed_cases']} / {report['total_cases']} passed**.",
        "Procedure accuracy still requires comparing each evidence set with the explicit expected result below.",
        "",
        "| case | pass | verified | stop | rounds | actions | repeats | stale | ctx max | wall s | evidence title |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report["results"]:
        case = item["case"]
        review = item.get("semantic_review") or {}
        decision = review.get("decision") if isinstance(review, dict) else ""
        verified = f"{item.get('verified')}"
        if decision:
            verified += f" / {decision}"
        lines.append(
            f"| {case['id']} | {item.get('acceptance_pass')} | {verified} | "
            f"{item.get('stopped_reason') or item.get('error') or '-'} | {item.get('model_rounds')} | "
            f"{item.get('browser_actions')} | {item.get('repeated_actions')} | "
            f"{item.get('stale_target_failures')} | {item.get('prompt_tokens_max')} | "
            f"{item.get('wall_s')} | {(item.get('evidence_title') or '')[:60]} |"
        )
    lines.extend(["", "## Accuracy review", ""])
    for item in report["results"]:
        case = item["case"]
        failures = item.get("acceptance_failures") or []
        lines.extend(
            [
                f"### {case['id']}",
                "",
                f"- Expected: {case.get('expect') or 'not specified'}",
                f"- Retrieved: {item.get('evidence_title') or 'none'}",
                f"- Operational failures: {'; '.join(failures) if failures else 'none'}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="run")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Diagnostic mode: write the report but return success even when cases fail.",
    )
    parser.add_argument(
        "--vin-from-ciq",
        action="store_true",
        help="Fill each case's VIN from its repair order in Calibration IQ (exact identity only).",
    )
    args = parser.parse_args()

    from core.config import Settings

    settings = Settings.load()
    endpoint, model = worker_target()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case["id"] in wanted]
    if args.vin_from_ciq:
        from core.services import adas_si_research, calibration_iq

        for case in cases:
            if case.get("vin") or not case.get("ro"):
                continue
            try:
                read = await calibration_iq.get_repair_order(settings, {"repair_order_id": case["ro"]})
            except Exception as exc:  # noqa: BLE001
                case["_preflight_error"] = f"VIN lookup failed ({type(exc).__name__})"
                print(f"[{args.label}] {case['id']}: {case['_preflight_error']}", flush=True)
                continue
            vin = adas_si_research.vin_from_read(read) if isinstance(read, dict) else ""
            if vin:
                case["vin"] = vin
                print(f"[{args.label}] {case['id']}: VIN {vin} from Calibration IQ", flush=True)
            else:
                case["_preflight_error"] = "Calibration IQ did not return a valid 17-character VIN"
                print(f"[{args.label}] {case['id']}: {case['_preflight_error']}", flush=True)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "label": args.label,
        "run_at": datetime.now(UTC).isoformat(),
        "worker": f"{endpoint} ({model})",
        "capture": args.capture,
        "max_turns": args.max_turns,
        "results": [],
        "total_cases": len(cases),
        "passed_cases": 0,
    }
    for case in cases:
        print(
            f"[{args.label}] running {case['id']}: {case['year']} {case['make']} "
            f"{case['model']} -- {case['topic']}",
            flush=True,
        )
        item = await run_case(
            case,
            settings=settings,
            endpoint=endpoint,
            model=model,
            capture=args.capture,
            max_turns=args.max_turns,
        )
        item["acceptance_failures"] = acceptance_failures(
            item, capture=args.capture, require_vin=args.vin_from_ciq
        )
        item["acceptance_pass"] = not item["acceptance_failures"]
        report["results"].append(item)
        report["passed_cases"] = sum(
            1 for result in report["results"] if result.get("acceptance_pass") is True
        )
        print(
            f"[{args.label}] {case['id']}: verified={item.get('verified')} "
            f"stop={item.get('stopped_reason') or item.get('error')} "
            f"rounds={item.get('model_rounds')} actions={item.get('browser_actions')} "
            f"ctx_max={item.get('prompt_tokens_max')} wall={item.get('wall_s')}s "
            f"title={item.get('evidence_title')}",
            flush=True,
        )
        out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        out.with_suffix(".md").write_text(markdown_summary(report), encoding="utf-8")
    all_passed = report["passed_cases"] == report["total_cases"] and report["total_cases"] > 0
    return 0 if all_passed or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
