import assert from "node:assert/strict";
import test from "node:test";

import { correctDomainVocabulary } from "../src/lib/speechDomainCorrections.js";

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
