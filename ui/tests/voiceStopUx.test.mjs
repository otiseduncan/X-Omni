import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
const voice = await readFile(new URL("../src/hooks/useVoice.js", import.meta.url), "utf8");

test("browser dictation uses one guarded recognition session that never self-restarts", () => {
  // The invariant this test exists for is the restart loop, not any specific
  // timing: one recognition object per session, finalized once by onend, and
  // never relaunched on a timer.
  assert.match(voice, /recognitionRef\.current !== recognition/);
  assert.match(voice, /recognition\.onend = \(\) => \{/);
  assert.doesNotMatch(voice, /BROWSER_RESTART_DELAY_MS/);
  assert.doesNotMatch(voice, /window\.setTimeout\(launch/);
  assert.match(voice, /event\.error === "no-speech"/);
});

test("dictation gives a hesitant recognizer room instead of submitting a fragment", () => {
  // 1800ms counted time since Chrome last emitted a result, not silence in
  // the room, so a weak far-field mic ended turns mid-sentence. Interim text
  // now buys the full window and only a final result shortens it.
  assert.match(voice, /const BROWSER_END_SILENCE_MS = 4500;/);
  assert.match(voice, /const BROWSER_FINAL_GRACE_MS = 2200;/);
  assert.match(
    voice,
    /newest\?\.isFinal \? BROWSER_FINAL_GRACE_MS : BROWSER_END_SILENCE_MS/
  );
  // Chrome's own endpointer outranks our timer while speech is in progress.
  assert.match(voice, /recognition\.onspeechstart = \(\) => \{/);
  assert.match(voice, /recognition\.onspeechend = \(\) => \{/);
});

test("continuous dictation is enabled everywhere except Android Chrome", () => {
  // Android's continuous mode emits cumulative final slots; desktop's is
  // correct, and without it Chrome stops at the first sentence boundary.
  assert.match(voice, /recognition\.continuous = !isAndroidChrome;/);
  assert.match(voice, /\/android\/i\.test\(navigator\.userAgent/);
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
