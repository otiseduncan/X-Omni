import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const cardsUrl = new URL("../src/components/cards/ResearchCards.jsx", import.meta.url);
const fieldCssUrl = new URL("../src/styles/field-cards.css", import.meta.url);

test("composite collision research visibly proves each searched source", async () => {
  const cards = await readFile(cardsUrl, "utf8");

  assert.match(cards, /function FullResearchResult/);
  assert.match(cards, /What X actually searched/);
  assert.match(cards, /research-source-ledger/);
  assert.match(cards, /searched · verified/);
  assert.match(cards, /not searched/);
  assert.match(cards, /ALLDATA was not verified as searched/);
  assert.match(cards, /Public OEM \/ manufacturer sources/);
  assert.match(cards, /data\?\.action === "full_research"/);
});

test("ADAS and research excerpts cannot collapse or overlay on narrow cards", async () => {
  const css = await readFile(fieldCssUrl, "utf8");

  assert.match(css, /\.field-hit\s*\{[\s\S]*?min-width:\s*0[\s\S]*?overflow:\s*hidden/);
  assert.match(css, /\.field-hit summary\s*\{[\s\S]*?flex-wrap:\s*wrap[\s\S]*?line-height:\s*1\.35/);
  assert.match(css, /\.field-excerpt,[\s\S]*?\.research-readable-text\s*\{[\s\S]*?display:\s*block/);
  assert.match(css, /line-height:\s*1\.5\s*!important/);
  assert.match(css, /white-space:\s*pre-wrap\s*!important/);
  assert.match(css, /overflow-wrap:\s*anywhere/);
  assert.match(css, /font-variant-ligatures:\s*none/);
});
