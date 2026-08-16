import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("new capabilities render only as existing chat-stream artifacts", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");
  const links = await readFile(new URL("../src/lib/externalLinks.js", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");

  assert.match(app, /<main[\s\S]{0,160}className="stream"[\s\S]{0,160}ref=\{streamRef\}|<main[\s\S]{0,160}ref=\{streamRef\}[\s\S]{0,160}className="stream"/);
  assert.match(app, /return <Artifact artifact=\{item\.artifact\}/);
  assert.match(cards, /file_search:\s*FileSearchCard/);
  assert.match(cards, /web_research:\s*WebResearchCard/);
  assert.match(cards, /capabilities:\s*CapabilitiesCard/);
  assert.match(cards, /generated_image:\s*GeneratedImageCard/);
  assert.match(cards, /image_generation_status:\s*ImageGenerationStatusCard/);
  assert.match(cards, /className="generated-image"[\s\S]{0,240}width=\{data\.width\}[\s\S]{0,100}height=\{data\.height\}[\s\S]{0,100}loading="eager"/);
  assert.doesNotMatch(cards, /className="generated-image"[\s\S]{0,320}loading="lazy"/);
  assert.match(cards, /onError=\{\(\) => setImageLoadFailed\(true\)\}/);
  assert.match(cards, /imageLoadFailed \? "Generated image unavailable" : "Generated locally"/);
  assert.match(cards, /className="generated-image-load-error" role="alert"/);
  assert.match(cards, /No successful display is being claimed/);
  assert.match(cards, /imageUrl === `\/api\/generated-images\/\$\{digest\}\.png`/);
  assert.match(cards, /receiptState\(receipt\) === "succeeded"[\s\S]*?result\?\.lifecycle\?\.model_restored === true/);
  assert.match(cards, /<span>Camera vision<\/span>[\s\S]{0,100}supports_vision \? "yes" : "no"/);
  assert.doesNotMatch(cards, /<span>Image chat<\/span>[\s\S]{0,100}not available/);
  assert.match(app, /receiptMatchesArtifact\(lastExecutionReceiptRef\.current, liveArtifact\?\.type\)/);
  assert.match(app, /externalWorkload === "image_generation"/);
  assert.match(app, /generating image/);
  assert.match(app, /Omni is temporarily unloaded; Core will attempt to restore it/);
  assert.match(app, /success is shown only after that restoration is verified/);
  assert.match(cards, /safeExternalUrl\(source\.url\)/);
  assert.match(links, /parsed\.protocol[\s\S]*?parsed\.username[\s\S]*?isPrivateSourceHost/);
  assert.match(cards, /target="_blank"[\s\S]{0,120}rel="noopener noreferrer"[\s\S]{0,160}aria-label=/);
  assert.match(styles, /\.stream\s*\{[\s\S]*?overflow-y:\s*auto;/);
  assert.match(styles, /\.card\s*\{[\s\S]*?min-width:\s*0;/);
  assert.match(styles, /\.card-note\s*\{[\s\S]*?overflow-wrap:\s*anywhere;/);
  assert.match(styles, /\.generated-image\s*\{[\s\S]*?width:\s*100%;[\s\S]*?height:\s*auto;[\s\S]*?max-height:\s*min\(68vh, 720px\);/);
  assert.match(styles, /\.generated-image-load-error\s*\{[\s\S]*?border:/);
  assert.match(styles, /\.shell\s*\{[\s\S]*?minmax\(0, 1fr\)[\s\S]*?height:\s*100dvh;/);
  assert.doesNotMatch(cards, /role="dialog"|aria-modal|backdrop/);
});

test("direct cards always persist against an active conversation", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");

  assert.match(app, /let conversationId = conversationIdRef\.current;[\s\S]{0,120}conversationId == null[\s\S]{0,80}createConversation\(\)/);
  assert.match(app, /JSON\.stringify\(\{ conversation_id: conversationId \}\)/);
  assert.match(app, /<ToolRail[\s\S]{0,180}disabled=\{!historyReady \|\| creatingConversation \|\| thinking \|\| swapping\}/);
  assert.match(app, /artifact:\$\{payload\.message_id\}:0/);
});

test("tool rail counts only actions that are actually one-click runnable", async () => {
  const source = await readFile(new URL("../src/components/ToolRail.jsx", import.meta.url), "utf8");
  assert.match(source, /ONE_CLICK\.has\(t\.name\)/);
  assert.match(source, /\{oneClickCount\} quick/);
  assert.doesNotMatch(source, /\{runnable\} ready/);
});
