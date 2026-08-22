import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { toSpeechText } from "../src/lib/speechText.js";


test("speech text strips markdown emphasis instead of saying asterisks", () => {
  const spoken = toSpeechText(
    "**Evidence Summary:** Toyota **does not approve** recycled parts."
  );

  assert.equal(spoken, "Evidence Summary: Toyota does not approve recycled parts.");
  assert.equal(spoken.includes("*"), false);
});


test("speech text keeps link labels but does not read raw URLs", () => {
  const spoken = toSpeechText(
    "See [Toyota Collision Pros](https://example.com/really/long/path) for the source."
  );

  assert.equal(spoken, "See Toyota Collision Pros for the source.");
  assert.equal(spoken.includes("http"), false);
});


test("voice hook sanitizes assistant text before SpeechSynthesisUtterance", async () => {
  const hook = await readFile(new URL("../src/hooks/useVoice.js", import.meta.url), "utf8");

  assert.match(hook, /import \{ toSpeechText \} from "\.\.\/lib\/speechText\.js"/);
  assert.match(hook, /const spoken = toSpeechText\(text\)/);
  assert.match(hook, /SpeechSynthesisUtterance\(spoken\.slice\(0, 4000\)\)/);
  assert.doesNotMatch(hook, /SpeechSynthesisUtterance\(text\.slice\(0, 4000\)\)/);
});
