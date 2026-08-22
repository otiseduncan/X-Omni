import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const cardsUrl = new URL("../src/components/cards/ResearchCards.jsx", import.meta.url);

test("full collision research stays collapsed under the conversational answer", async () => {
  const cards = await readFile(cardsUrl, "utf8");
  const start = cards.indexOf("function FullResearchResult");
  const end = cards.indexOf("function ExternalResults");
  const block = cards.slice(start, end);

  assert.match(block, /title="Research details"/);
  assert.match(block, /research-compact-details/);
  assert.match(block, /Research details ·/);
  assert.match(block, /Key manufacturer policy findings/);
  assert.doesNotMatch(block, /research-compact-details"\s+open/);
  assert.doesNotMatch(block, /research-evidence-group"\s+open/);
});
