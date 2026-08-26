import assert from "node:assert/strict";
import test from "node:test";

import {
  speechResultSlotsText,
  speechRecognitionResultsText,
  updateSpeechResultSlots,
} from "../src/lib/speechTranscript.js";


function result(transcript, isFinal = false) {
  const value = [{ transcript }];
  value.isFinal = isFinal;
  return value;
}


test("cumulative Android events rebuild one current hypothesis", () => {
  const events = [
    [result("do you", true)],
    [result("do you show", true)],
    [result("do you show the front", true)],
    [result("do you show the front windshield camera", true)],
  ];
  const previews = events.map(speechRecognitionResultsText);

  assert.deepEqual(previews, [
    "do you",
    "do you show",
    "do you show the front",
    "do you show the front windshield camera",
  ]);
  assert.equal(previews.at(-1), "do you show the front windshield camera");
});


test("the photographed Android cumulative slot ladder collapses once", () => {
  const text = speechRecognitionResultsText([
    result("how many", true),
    result("how many", true),
    result("how many cars", true),
    result("how many cars and", true),
    result("How many cars and Phase 5", true),
    result("how many cars and Phase 5 6 7 and 8 need", true),
  ]);

  assert.equal(text, "how many cars and Phase 5 6 7 and 8 need");
});


test("a changing result slot is rebuilt instead of appended across events", () => {
  const first = speechRecognitionResultsText([result("do you", true)]);
  const second = speechRecognitionResultsText([
    result("do you show the front windshield camera calibration", true),
  ]);

  assert.equal(first, "do you");
  assert.equal(second, "do you show the front windshield camera calibration");
  assert.equal(second.includes("do you do you"), false);
});


test("resultIndex replaces changed slots and truncates a vanished interim tail", () => {
  let slots = updateSpeechResultSlots([], {
    resultIndex: 0,
    results: [result("show me", true), result("the windshield", false)],
  });
  assert.deepEqual(slots, ["show me", "the windshield"]);

  slots = updateSpeechResultSlots(slots, {
    resultIndex: 1,
    results: [result("show me", true), result("the windshield camera", true)],
  });
  assert.deepEqual(slots, ["show me", "the windshield camera"]);
  assert.equal(speechResultSlotsText(slots), "show me the windshield camera");

  slots = updateSpeechResultSlots(slots, {
    resultIndex: 1,
    results: [result("show me", true)],
  });
  assert.deepEqual(slots, ["show me"]);
});


test("the complete indexed snapshot retains earlier finals and removes a vanished tail", () => {
  const withInterimTail = speechRecognitionResultsText([
    result("show me", true),
    result("the windshield", false),
  ]);
  const withFinalTail = speechRecognitionResultsText([
    result("show me", true),
    result("the windshield camera", true),
  ]);
  const withoutTail = speechRecognitionResultsText([result("show me", true)]);

  assert.equal(withInterimTail, "show me the windshield");
  assert.equal(withFinalTail, "show me the windshield camera");
  assert.equal(withoutTail, "show me");
});


test("sequential Web Speech result slots preserve intentional repetition", () => {
  assert.equal(
    speechRecognitionResultsText([
      result("to be or not", true),
      result("to be", true),
    ]),
    "to be or not to be"
  );
  assert.equal(
    speechRecognitionResultsText([
      result("I had", true),
      result("had enough", true),
    ]),
    "I had had enough"
  );
  assert.equal(
    speechRecognitionResultsText([result("very"), result("very")]),
    "very very"
  );
  assert.equal(
    speechRecognitionResultsText([
      result("I", true),
      result("I really", true),
      result("meant something else", true),
    ]),
    "I I really meant something else"
  );
});
