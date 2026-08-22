import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("ALLDATA credentials are collected inside chat without browser secret storage", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/ResearchCards.jsx", import.meta.url),
    "utf8"
  );
  const fields = await readFile(
    new URL("../src/components/cards/FieldCards.jsx", import.meta.url),
    "utf8"
  );

  assert.match(fields, /research_provider:\s*ResearchProviderCard/);
  assert.match(cards, /ALLDATA research access/);
  assert.match(cards, /ALLDATA username/);
  assert.match(cards, /type="password"/);
  assert.match(cards, /autoComplete="current-password"/);
  assert.match(cards, /\/api\/research\/providers\/alldata\/credentials/);
  assert.match(cards, /Windows Credential Manager/);
  assert.match(cards, /not put in chat, browser storage, or model context/);

  const accessBlock = cards.slice(
    cards.indexOf("function AccessCard"),
    cards.indexOf("export function ResearchProviderCard")
  );
  assert.doesNotMatch(accessBlock, /localStorage|sessionStorage|indexedDB/);
  assert.doesNotMatch(accessBlock, /setItems|conversation|push\(/);

  const clearIndex = accessBlock.indexOf('passwordRef.current.value = ""');
  const fetchIndex = accessBlock.indexOf('fetch("/api/research/providers/alldata/credentials"');
  assert.ok(
    clearIndex >= 0 && fetchIndex >= 0 && clearIndex < fetchIndex,
    "password input must be cleared before awaiting the credential request"
  );
});

test("ALLDATA human authentication controls are mobile inline and same-origin", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/ResearchCards.jsx", import.meta.url),
    "utf8"
  );

  assert.match(cards, /\/api\/research\/providers\/alldata\/sessions/);
  assert.match(cards, /Tap the browser image to click/);
  assert.match(cards, /works from your phone/);
  assert.match(cards, /autocomplete="one-time-code"/i);
  assert.match(cards, /action:\s*"click"/);
  assert.match(cards, /action:\s*"type"/);
  assert.match(cards, /action:\s*"press",\s*key:\s*"Enter"/);
  assert.match(cards, /action:\s*"scroll",\s*dy:\s*700/);
  assert.match(cards, /\* 1280/);
  assert.match(cards, /\* 900/);
  assert.doesNotMatch(cards, /window\.open|target="_blank"[^>]*ALLDATA/);
  assert.doesNotMatch(cards, /data:image\/jpeg;base64|FileReader/);
});

test("research card exposes public OEM evidence and permanent ADAS capture results", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/ResearchCards.jsx", import.meta.url),
    "utf8"
  );

  assert.match(cards, /Post-collision web research/);
  assert.match(cards, /OEM, insurer, and legal\/regulatory requirements are separate authorities/);
  assert.match(cards, /Research source saved/);
  assert.match(cards, /readable_pages/);
  assert.match(cards, /relative_path/);
});
