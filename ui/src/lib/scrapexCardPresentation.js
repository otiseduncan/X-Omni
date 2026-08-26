function nonNegativeCount(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function countOfTotal(value, total) {
  const count = nonNegativeCount(value);
  if (count == null) return null;
  return total == null ? String(count) : `${count} of ${total}`;
}

export function scrapexCardPresentation(data) {
  const payload = data?.data;
  const objectPayload = payload && !Array.isArray(payload) ? payload : {};
  const readiness = objectPayload?.readiness || objectPayload?.batch?.readiness || {};
  const provenance = objectPayload?.provenance || {};
  const authenticationRequired = data?.authentication_required === true;
  const warning = authenticationRequired || data?.success === false;
  const batches = Array.isArray(payload)
    ? payload
    : Array.isArray(objectPayload?.batches)
      ? objectPayload.batches
      : [];
  const total = nonNegativeCount(readiness?.total);
  const readinessRows = [];

  if (total != null) {
    readinessRows.push({ key: "batch-size", label: "Batch vehicles", value: String(total) });
  }
  if (typeof readiness?.ready === "boolean") {
    readinessRows.push({
      key: "scope-ready",
      label: "ADAS Map + CIQ ready",
      value: readiness.ready ? "yes" : "no",
    });
  } else {
    const readyCount = countOfTotal(readiness?.ready, total);
    if (readyCount != null) {
      readinessRows.push({
        key: "scope-ready-count",
        label: "ADAS Map + CIQ ready",
        value: readyCount,
      });
    }
  }

  for (const [key, label] of [
    ["adas_map_complete", "ADAS Map complete"],
    ["ciq_reconciled", "CIQ reconciled"],
  ]) {
    const value = countOfTotal(readiness?.[key], total);
    if (value != null) readinessRows.push({ key, label, value });
  }
  for (const [key, label] of [
    ["adas_map_unresolved", "ADAS Map unresolved"],
    ["adas_map_attention", "Operator attention"],
  ]) {
    const value = nonNegativeCount(readiness?.[key]);
    if (value != null) readinessRows.push({ key, label, value: String(value) });
  }

  return {
    payload,
    objectPayload,
    readiness,
    readinessRows,
    provenance,
    authenticationRequired,
    warning,
    batches,
    completionLabel: data?.action === "open_authentication"
      ? "Authentication ready"
      : "ADAS Map + CIQ complete",
  };
}
