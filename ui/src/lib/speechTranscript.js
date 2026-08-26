function cleanText(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ");
}

function comparisonTokens(value) {
  return cleanText(value).toLowerCase().match(/[a-z0-9]+(?:'[a-z0-9]+)?/g) || [];
}

function isTokenPrefix(candidate, full) {
  return (
    candidate.length <= full.length &&
    candidate.every((token, index) => token === full[index])
  );
}

function transcriptFromResult(result) {
  return cleanText(result?.[0]?.transcript);
}

/**
 * Update the browser's indexed result slots. resultIndex identifies the first
 * changed slot; earlier final slots remain, changed hypotheses are replaced,
 * and a shorter result list removes a vanished interim tail.
 */
export function updateSpeechResultSlots(previous, event) {
  const results = Array.from(event?.results || []);
  const requestedStart = Number.isInteger(event?.resultIndex) ? event.resultIndex : 0;
  const start = Math.max(0, Math.min(requestedStart, results.length));
  const next = Array.from(previous || []).slice(0, results.length);

  for (let index = 0; index < results.length; index += 1) {
    if (index >= start || next[index] == null) {
      next[index] = transcriptFromResult(results[index]);
    }
  }
  return next;
}

/**
 * Render indexed Web Speech result slots. Slots are sequential by default and
 * are never globally deduplicated. Android's broken continuous mode can expose
 * an unmistakable cumulative ladder instead: three or more distinct lengths
 * where every slot is a prefix of one longest hypothesis. Only that shape is
 * collapsed to the longest original transcript.
 */
export function speechResultSlotsText(slots) {
  const populated = Array.from(slots || [], cleanText).filter(Boolean);
  if (!populated.length) return "";

  const candidates = populated.map((text, index) => ({
    text,
    index,
    tokens: comparisonTokens(text),
  }));
  const longest = candidates.reduce((best, candidate) => {
    if (candidate.tokens.length > best.tokens.length) return candidate;
    if (candidate.tokens.length === best.tokens.length && candidate.index > best.index) {
      return candidate;
    }
    return best;
  });
  const distinctLengths = new Set(candidates.map(({ tokens }) => tokens.length));
  const cumulativeLadder =
    distinctLengths.size >= 3 &&
    longest.tokens.length > 0 &&
    candidates.every(({ tokens }) => isTokenPrefix(tokens, longest.tokens));

  return cumulativeLadder ? longest.text : populated.join(" ");
}

/** Convenience renderer for a complete raw SpeechRecognitionResultList. */
export function speechRecognitionResultsText(results) {
  return speechResultSlotsText(
    Array.from(results || [], transcriptFromResult)
  );
}
