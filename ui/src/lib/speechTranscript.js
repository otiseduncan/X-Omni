function cleanText(value) {
  return String(value || "")
    .trim()
    .replace(/\s+/g, " ");
}

/**
 * Rebuild the current Web Speech result list instead of appending events.
 * Result slots are sequential speech and are deliberately not deduplicated:
 * two slots containing "very" must remain "very very".
 */
export function speechRecognitionResultsText(results) {
  return Array.from(results || [], (result) => cleanText(result?.[0]?.transcript))
    .filter(Boolean)
    .join(" ");
}
