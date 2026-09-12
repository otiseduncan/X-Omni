/**
 * Chrome's generic speech recognizer has no vocabulary for this shop's
 * jargon and substitutes the nearest common word it knows instead. Observed
 * live: "ADAS" heard as "ass" ("check it ass SI", "check that ass SI").
 * Phrase patterns run first so a known multi-word mishearing wins over the
 * single-word fallback below it. Add newly observed mishearings here as
 * they're spotted rather than guessing ahead of real evidence.
 */
const DOMAIN_TERM_CORRECTIONS = [
  // Observed 2026-09-11: "missing a dash map" for "missing ADAS Map". Only the
  // two-word phrase is corrected; a bare "a dash" (a dash cam) is left alone.
  { pattern: /\ba\s+dash\s+map\b/gi, replacement: "ADAS Map" },
  { pattern: /\ba\s+dash\s+si\b/gi, replacement: "ADAS SI" },
  { pattern: /\bass\s+si\b/gi, replacement: "ADAS SI" },
  { pattern: /\bass\s+map\b/gi, replacement: "ADAS Map" },
  { pattern: /\bass\b/gi, replacement: "ADAS" },

  // Observed 2026-09-12, all from one dictation session on the desktop's
  // far-field mic. Each is scoped to the phrase it was actually seen in
  // rather than the bare word, because every one of these misheard words is
  // also a legitimate word in a collision shop: a car has a roof, "carbs"
  // could be carburettors, and an arrow is an arrow.
  //
  // "get the missing a bath mat reports" -> ADAS Map
  { pattern: /\ba\s+bath\s+mat\b/gi, replacement: "ADAS Map" },
  // "run a sweep of dash maps" / "need a Das map" -- seen from the local
  // engine on the same vocabulary.
  { pattern: /\bof\s+dash\s+maps\b/gi, replacement: "ADAS Maps" },
  { pattern: /\ba\s+das\s+map\b/gi, replacement: "ADAS Map" },
  // "how many carbs and phases one through eight" -> "cars in phases".
  // Anchored on the following "phase" so ordinary carburettor talk survives.
  {
    pattern: /\bcarbs\s+(?:and|in)\s+phase/gi,
    replacement: "cars in phase",
  },
  { pattern: /\band\s+phases\s+(one|two|\d)\b/gi, replacement: "in phases $1" },
  // "attach them to the arrows" / "attached to the roof" -> ROs. Both are
  // anchored to "the", so a literal roof or arrow is only rewritten when it
  // is the object of an attach-to-the phrase.
  { pattern: /\bthe\s+arrows\b/gi, replacement: "the ROs" },
  {
    pattern: /\battach(ed|ing)?\s+((?:it|them)\s+)?to\s+the\s+roof\b/gi,
    replacement: (_m, tense = "", obj = "") =>
      `attach${tense || ""} ${obj}to the ROs`.replace(/\s+/g, " "),
  },
];

/**
 * Shop terms used only to rank Chrome's alternative hypotheses against each
 * other. Chrome returns up to maxAlternatives transcripts ordered by its own
 * generic-English confidence; when a lower-ranked one spells a shop term that
 * the top one missed, it is the better transcript for this app.
 */
const DOMAIN_TERMS = [
  /\badas\b/i,
  /\badas\s+map\b/i,
  /\badas\s+si\b/i,
  /\bros?\b/i,
  /\brepair\s+orders?\b/i,
  /\bcalibration\s+iq\b/i,
  /\bscrapex\b/i,
  /\bphase\s*\d/i,
  /\bvin\b/i,
  /\bcalibration\b/i,
  /\bwindshield\b/i,
  /\bradar\b/i,
];

/** How many distinct shop terms a candidate transcript spells correctly. */
export function domainTermScore(text) {
  const value = String(text || "");
  return DOMAIN_TERMS.reduce(
    (score, pattern) => (pattern.test(value) ? score + 1 : score),
    0
  );
}

export function correctDomainVocabulary(text) {
  return DOMAIN_TERM_CORRECTIONS.reduce(
    (value, { pattern, replacement }) => value.replace(pattern, replacement),
    String(text || "")
  );
}
