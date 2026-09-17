import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  artifactNeedsAttention,
  artifactPresentation,
  evidenceSummary,
  presentTimeline,
  spokenReply,
} from "../src/lib/responsePresentation.js";
import { timelineFromHistory } from "../src/lib/conversationTimeline.js";

const OCR_ROWS = "Carnival | 01F | 01F | 0F | 11 | OFF | N/A\nForte (BD) | N/A | N/A | OFF | OF | OF | ON | ON";
const findings = {
  type: "research_findings",
  presentation: "evidence",
  data: { status: "success", verified: true, findings: [{ title: "Hyundai Kia Genesis Front Radar Bumper Requirements", excerpt: OCR_ROWS }] },
};

test("Core's stamp decides presentation, with the same type fallback for older messages", () => {
  assert.equal(artifactPresentation(findings), "evidence");
  assert.equal(artifactPresentation({ type: "research_findings" }), "evidence");
  assert.equal(artifactPresentation({ type: "calibration_iq_summary" }), "evidence");
  assert.equal(artifactPresentation({ type: "a_new_tool_card" }), "evidence");
  assert.equal(artifactPresentation({ type: "generated_image" }), "primary");
  assert.equal(artifactPresentation({ type: "approval_request" }), "primary");
  assert.equal(artifactPresentation({ type: "research_findings", presentation: "primary" }), "primary");
});

test("a restored reply keeps its answer and folds its evidence into one collapsed group", () => {
  const items = timelineFromHistory([
    { id: 1, role: "user", content: "what about Kia front bumpers during radar calibration", artifacts: [] },
    {
      id: 2,
      role: "assistant",
      content: "Off for the bracket inspection, back on before you calibrate.",
      worker_used: "omni",
      artifacts: [findings, { type: "calibration_iq_summary", data: { status: "success", count: 7 } }],
    },
  ]);
  const presented = presentTimeline(items);

  assert.deepEqual(presented.map((item) => item.kind), ["user", "assistant"]);
  const reply = presented[1];
  assert.equal(reply.text, "Off for the bracket inspection, back on before you calibrate.");
  assert.equal(reply.evidence.length, 2);
  assert.ok(!reply.text.includes("Carnival"), "raw extraction never becomes reply text");
  assert.equal(reply.evidence[0].artifact.data.findings[0].excerpt, OCR_ROWS, "evidence is preserved whole");
});

test("live order (cards first, reply at done) attaches the same way", () => {
  const presented = presentTimeline([
    { kind: "user", key: "u", text: "how many cars need SI?" },
    { kind: "artifact", key: "a1", artifact: { type: "calibration_iq_summary", presentation: "evidence", data: { count: 7 } } },
    { kind: "assistant", key: "m", text: "Seven cars need SI." },
  ]);
  assert.deepEqual(presented.map((item) => item.kind), ["user", "assistant"]);
  assert.equal(presented[1].text, "Seven cars need SI.");
  assert.equal(presented[1].evidence.length, 1);
});

test("primary deliverables stay in the stream and in-progress work still shows as a disclosure", () => {
  const presented = presentTimeline([
    { kind: "user", key: "u", text: "make the image" },
    { kind: "artifact", key: "img", artifact: { type: "generated_image", presentation: "primary", data: {} } },
    { kind: "artifact", key: "r1", artifact: { type: "research_findings", presentation: "evidence", data: {} } },
  ]);
  assert.deepEqual(presented.map((item) => item.kind), ["user", "artifact", "evidence"]);
  assert.equal(presented[2].evidence[0].key, "r1");
});

test("the disclosure summary flags failed or unverified evidence", () => {
  assert.equal(artifactNeedsAttention({ data: { status: "blocked" } }), true);
  assert.equal(artifactNeedsAttention({ data: { verified: false } }), true);
  assert.equal(artifactNeedsAttention(findings), false);
  assert.deepEqual(
    evidenceSummary([{ artifact: findings }, { artifact: { data: { status: "authentication_required" } } }]),
    { count: 2, attention: true, label: "2 items" },
  );
});

test("voice reads Core's spoken answer, never the evidence", () => {
  const done = {
    type: "done",
    artifacts: [findings],
    response: { assistant_text: "**Off** for inspection.", spoken_text: "Off for inspection." },
  };
  assert.equal(spokenReply(done, "**Off** for inspection."), "Off for inspection.");
  assert.ok(!spokenReply(done, "").includes("Carnival"));
  assert.equal(spokenReply({ type: "done" }, "Streamed answer."), "Streamed answer.");
});

test("the evidence control is collapsed by default, subtle, and wired into the reply and voice", async () => {
  const component = await readFile(new URL("../src/components/EvidenceDisclosure.jsx", import.meta.url), "utf8");
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");

  assert.match(component, /<details className=\{`evidence-disclosure/);
  assert.doesNotMatch(component, /<details[^>]*\sopen(?:=|\s|>)/);
  assert.match(component, /Sources &amp; work/);
  assert.match(app, /presentedItems\.map\(/);
  assert.match(app, /voice\.speak\(spokenReply\(event, text\)\)/);
  assert.doesNotMatch(app, /voice\.speak\(text\)/);
  assert.match(styles, /\.evidence-summary\s*\{[\s\S]*?min-height:\s*44px;[\s\S]*?font-size:\s*0\.72rem;/);
});
