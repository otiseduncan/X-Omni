const DONE_STATUS_LABELS = new Set([
  "calibration complete",
  "no calibration required",
]);

export const CALIBRATION_COLLECTION_WARNING =
  "Collection limit reached. This count may be incomplete.";

export function calibrationStatusTone(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (DONE_STATUS_LABELS.has(normalized)) return "done";
  if (normalized.includes("ready")) return "ok";
  if (normalized.includes("progress") || normalized.includes("calibrat")) return "active";
  if (
    normalized.includes("hold") ||
    normalized.includes("waiting") ||
    normalized.includes("block")
  ) return "warn";
  return "";
}

export function calibrationPhaseBreakdown(label, count) {
  const normalized = String(label ?? "").trim();
  if (!normalized || normalized.toLowerCase() === "unspecified") {
    return `${count} no phase`;
  }
  const phase = /^phase(?:\s|$)/i.test(normalized) ? normalized : `Phase ${normalized}`;
  return `${count} in ${phase}`;
}

export function calibrationCountLabel(count, collectionCapped = false) {
  return collectionCapped ? `at least ${count}` : String(count);
}

export function calibrationSummaryScopeLabel({
  terminalOnly = false,
  includeCompleted = false,
  collectionCapped = false,
} = {}) {
  const scope = terminalOnly
    ? "finished repair orders"
    : includeCompleted
      ? "repair orders"
      : "active repair orders";
  return collectionCapped ? `${scope} counted` : scope;
}
