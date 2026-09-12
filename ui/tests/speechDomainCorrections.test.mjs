import assert from "node:assert/strict";
import test from "node:test";

import {
  correctDomainVocabulary,
  domainTermScore,
} from "../src/lib/speechDomainCorrections.js";

test("phrase mishearings resolve to the two-word term", () => {
  assert.equal(
    correctDomainVocabulary("check it ass SI and see if you can find any"),
    "check it ADAS SI and see if you can find any"
  );
  assert.equal(
    correctDomainVocabulary("check that ass SI and scrapex"),
    "check that ADAS SI and scrapex"
  );
  assert.equal(
    correctDomainVocabulary("pull the ass map report"),
    "pull the ADAS Map report"
  );
});

test("a standalone mishearing falls back to the single-word term", () => {
  assert.equal(
    correctDomainVocabulary("does it need an ass calibration"),
    "does it need an ADAS calibration"
  );
});

test("matching is case-insensitive and preserves the rest of the sentence", () => {
  assert.equal(correctDomainVocabulary("ASS SI report"), "ADAS SI report");
  assert.equal(correctDomainVocabulary("Ass"), "ADAS");
});

test("empty and non-matching input pass through unchanged", () => {
  assert.equal(correctDomainVocabulary(""), "");
  assert.equal(correctDomainVocabulary(null), "");
  assert.equal(
    correctDomainVocabulary("how many vehicles are in phase 6"),
    "how many vehicles are in phase 6"
  );
});

test("the observed 'a dash map' mishearing becomes ADAS Map without touching dash cams", () => {
  assert.equal(
    correctDomainVocabulary("how many Ro's in calibration IQ are missing a dash map"),
    "how many Ro's in calibration IQ are missing ADAS Map",
  );
  assert.equal(correctDomainVocabulary("check the a dash SI library"), "check the ADAS SI library");
  assert.equal(correctDomainVocabulary("pull a dash cam clip"), "pull a dash cam clip");
});

test("mishearings observed on the desktop far-field mic are corrected", () => {
  // Every string here was produced verbatim by Chrome during one dictation
  // session on 2026-09-12.
  assert.equal(
    correctDomainVocabulary("how many carbs and phases one through eight need ADAS Map"),
    "how many cars in phases one through eight need ADAS Map",
  );
  assert.equal(
    correctDomainVocabulary("reports and attached them to the arrows"),
    "reports and attached them to the ROs",
  );
  assert.equal(
    correctDomainVocabulary("get the missing a bath mat reports and attached to the roof"),
    "get the missing ADAS Map reports and attached to the ROs",
  );
  // Seen from the local Omni engine on the same vocabulary.
  assert.equal(
    correctDomainVocabulary("run a sweep of dash maps"),
    "run a sweep ADAS Maps",
  );
  assert.equal(correctDomainVocabulary("need a Das map"), "need ADAS Map");
});

test("words that are also legitimate shop language survive uncorrected", () => {
  // Each of these shares a word with a correction above; the corrections are
  // anchored to the phrase they were observed in so ordinary usage is safe.
  assert.equal(
    correctDomainVocabulary("the roof rail was damaged"),
    "the roof rail was damaged",
  );
  assert.equal(
    correctDomainVocabulary("replace the carbs on the old truck"),
    "replace the carbs on the old truck",
  );
  assert.equal(
    correctDomainVocabulary("draw arrows on the diagram"),
    "draw arrows on the diagram",
  );
});

test("domain term scoring ranks a shop-correct transcript above a generic one", () => {
  assert.ok(
    domainTermScore("attach them to the ROs") >
      domainTermScore("attach them to the arrows"),
  );
  assert.equal(domainTermScore(""), 0);
  assert.equal(domainTermScore(null), 0);
});
