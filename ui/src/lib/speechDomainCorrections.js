/**
 * Chrome's generic speech recognizer has no vocabulary for this shop's
 * jargon and substitutes the nearest common word it knows instead. Observed
 * live: "ADAS" heard as "ass" ("check it ass SI", "check that ass SI").
 * Phrase patterns run first so a known multi-word mishearing wins over the
 * single-word fallback below it. Add newly observed mishearings here as
 * they're spotted rather than guessing ahead of real evidence.
 */
const DOMAIN_TERM_CORRECTIONS = [
  { pattern: /\bass\s+si\b/gi, replacement: "ADAS SI" },
  { pattern: /\bass\s+map\b/gi, replacement: "ADAS Map" },
  { pattern: /\bass\b/gi, replacement: "ADAS" },
];

export function correctDomainVocabulary(text) {
  return DOMAIN_TERM_CORRECTIONS.reduce(
    (value, { pattern, replacement }) => value.replace(pattern, replacement),
    String(text || "")
  );
}
