import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("successful execution and research details are collapsed native stream disclosures", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const approval = await readFile(new URL("../src/components/ApprovalCard.jsx", import.meta.url), "utf8");
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");

  assert.match(cards, /<details className=\{`card inline-disclosure research-disclosure/);
  assert.match(cards, /<summary[\s\S]{0,180}className="disclosure-summary"[\s\S]{0,240}aria-label=/);
  assert.match(cards, /if \(verifiedSuccess && !truncated\)[\s\S]*?<details className="card inline-disclosure shell-disclosure is-success">/);
  const successfulShell = cards.slice(cards.indexOf("if (verifiedSuccess"), cards.indexOf('let state = "indeterminate"'));
  assert.match(successfulShell, /<strong>PowerShell details<\/strong>/);
  assert.doesNotMatch(successfulShell.slice(0, successfulShell.indexOf("<div className=\"disclosure-body\">")), /PowerShell succeeded|Exit 0|receiptId/);
  assert.match(approval, /status === "succeeded" && receipt[\s\S]*?<details className="card approval approval-succeeded inline-disclosure receipt-disclosure">/);
  const approvalSummary = approval.slice(approval.indexOf("status === \"succeeded\""), approval.indexOf("</summary>", approval.indexOf("status === \"succeeded\"")));
  assert.match(approvalSummary, /<strong>Action completed<\/strong>/);
  assert.doesNotMatch(approvalSummary, /receiptMeta|receiptId|completedLabel/);
  const researchSummary = cards.slice(cards.indexOf("<summary", cards.indexOf("research-disclosure")), cards.indexOf("</summary>", cards.indexOf("research-disclosure")));
  assert.doesNotMatch(researchSummary, /queriedAt|External/);
  assert.doesNotMatch(`${cards}\n${approval}`, /<details[^>]*\sopen(?:=|\s|>)/);
  assert.match(app, /receiptUpdateFromArtifact\(event\.artifact\)/);
  assert.match(app, /receiptMatchesArtifact\(lastExecutionReceiptRef\.current, liveArtifact\?\.type\)/);
  assert.match(app, /\["shell_result", "generated_image", "image_generation_status"\]\.includes/);

  assert.match(styles, /\.disclosure-summary\s*\{[\s\S]*?minmax\(0, 1fr\)[\s\S]*?min-height:\s*44px;/);
  assert.match(styles, /\.disclosure-summary:focus-visible\s*\{/);
  assert.match(styles, /\.pre\s*\{[\s\S]*?max-height:\s*none;[\s\S]*?overflow:\s*visible;/);
  assert.doesNotMatch(`${cards}\n${approval}`, /role="dialog"|aria-modal|backdrop/);
});

test("failure, uncertainty, truncation, and research warnings stay visible in disclosure summaries or expanded cards", async () => {
  const approval = await readFile(new URL("../src/components/ApprovalCard.jsx", import.meta.url), "utf8");
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");

  assert.match(cards, /PowerShell failed · exit \$\{exitCode\}/);
  assert.match(cards, /PowerShell timed out · outcome indeterminate/);
  assert.match(cards, /PowerShell completed · output truncated/);
  assert.match(cards, /No matching successful execution receipt/);
  assert.doesNotMatch(cards, /exit \$\{data\?\.exit_code\}/);
  assert.match(approval, /Outcome unknown; the action may have executed and was not run again/);
  assert.match(approval, /role=\{\["failed", "indeterminate"\]\.includes\(status\) \? "alert" : "status"\}/);
  assert.match(cards, /No reliable sources returned/);
  assert.match(cards, /degradedProviders\.length[\s\S]*?provider warning/);
  assert.match(cards, /unsafe link blocked/);
  assert.match(cards, /Array\.isArray\(data\?\.results\)/);
  assert.match(cards, /source\.snippet \|\| source\.excerpt/);
});
