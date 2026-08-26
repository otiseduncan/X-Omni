import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const voice = await readFile(new URL("../src/hooks/useVoice.js", import.meta.url), "utf8");

test("browser dictation uses one guarded single-utterance recognition session", () => {
  assert.match(voice, /const BROWSER_END_SILENCE_MS = 1800;/);
  assert.match(voice, /recognition\.continuous = false;/);
  assert.match(voice, /recognitionRef\.current !== recognition/);
  assert.match(voice, /recognition\.onend = \(\) => \{/);
  assert.doesNotMatch(voice, /BROWSER_RESTART_DELAY_MS/);
  assert.doesNotMatch(voice, /window\.setTimeout\(launch/);
  assert.match(voice, /event\.error === "no-speech"/);
});

test("browser dictation rebuilds Android hypotheses instead of appending each result event", () => {
  assert.match(voice, /updateSpeechResultSlots\(session\.resultSlots, event\)/);
  assert.match(voice, /speechResultSlotsText\(session\.resultSlots\)/);
  assert.match(voice, /browserSessionRef\.current !== session/);
  assert.match(voice, /recognitionRef\.current !== recognition/);
  assert.match(voice, /browserSessionRef\.current \|\| recognitionRef\.current/);
  assert.doesNotMatch(voice, /finalRef/);
});

test("local microphone capture requests speech-friendly browser processing", () => {
  assert.match(voice, /channelCount: 1/);
  assert.match(voice, /echoCancellation: true/);
  assert.match(voice, /noiseSuppression: true/);
  assert.match(voice, /autoGainControl: true/);
  assert.match(voice, /rec\.start\(250\)/);
});

test("composer turns Send into a real Stop control while a response is active", () => {
  assert.match(app, /const responseActive = thinking \|\| Boolean\(streaming\) \|\| Boolean\(activeTool\);/);
  assert.match(app, /type: "stop"/);
  assert.match(app, /onClick=\{responseActive \? stopResponse : \(\) => sendMessage\(\)\}/);
  assert.match(app, /aria-label=\{responseActive \? "Stop response" : "Send message"\}/);
  assert.match(app, /<Square size=\{16\} fill="currentColor" \/>/);
  assert.match(app, /case "cancelled":/);
});
