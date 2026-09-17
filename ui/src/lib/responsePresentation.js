/**
 * Presentation of one assistant reply: X's answer, then its evidence.
 *
 * Core stamps every card with `presentation` ("primary" or "evidence"; see
 * core/orchestrator/response_contract.py). Primary cards are the deliverable
 * or need action and stay in the stream. Evidence cards -- sources, OCR and
 * table extractions, Calibration IQ reads and receipts, ScrapeX results,
 * statuses -- attach to the reply they support inside one collapsed
 * "Sources & work" disclosure. Nothing is dropped: every card still renders
 * when the disclosure is opened.
 *
 * Stored messages from before the stamp fall back to the same type list Core
 * uses; a backend test keeps the two lists identical.
 */

export const PRIMARY_ARTIFACT_TYPES = new Set([
  "adas_si_document",
  "approval",
  "approval_request",
  "calendar",
  "camera_event_history",
  "camera_footage_analysis",
  "camera_motion_clip",
  "camera_observation",
  "camera_request",
  "camera_snapshot",
  "exterior_camera_request",
  "generated_image",
  "generated_video",
  "image_generation_status",
  "shell_result",
  "tasks",
  "video_generation_status",
  "weather",
  "website_preview",
]);

const ATTENTION_STATUSES = new Set([
  "authentication_required",
  "blocked",
  "error",
  "failed",
  "indeterminate",
  "not_executed",
  "partial",
  "partial_success",
  "read_failed",
  "unavailable",
  "unverified_result",
]);

export function artifactPresentation(artifact) {
  if (artifact?.presentation === "primary" || artifact?.presentation === "evidence") {
    return artifact.presentation;
  }
  const type = typeof artifact?.type === "string" ? artifact.type.trim().toLowerCase() : "";
  return PRIMARY_ARTIFACT_TYPES.has(type) ? "primary" : "evidence";
}

export function isEvidenceItem(item) {
  return item?.kind === "artifact" && artifactPresentation(item.artifact) === "evidence";
}

/** A failed, partial, blocked, or unverified record must not look settled. */
export function artifactNeedsAttention(artifact) {
  const data = artifact?.data;
  if (!data || typeof data !== "object") return false;
  for (const key of ["status", "stage", "outcome"]) {
    const value = typeof data[key] === "string" ? data[key].trim().toLowerCase() : "";
    if (ATTENTION_STATUSES.has(value)) return true;
  }
  return data.success === false || data.verified === false;
}

export function evidenceSummary(evidence) {
  const list = Array.isArray(evidence) ? evidence : [];
  const count = list.length;
  const attention = list.some((item) => artifactNeedsAttention(item?.artifact));
  const label = `${count} ${count === 1 ? "item" : "items"}`;
  return { count, attention, label };
}

/**
 * Attach each run of evidence cards to the reply it belongs to.
 *
 * Live, a turn's cards arrive before its reply; a restored message lists its
 * reply before its cards. A run therefore attaches to the reply directly
 * before it, else the reply directly after it. A run with no adjacent reply
 * (the turn is still working, or paused for approval) becomes its own
 * collapsed disclosure so the work is never hidden entirely.
 */
export function presentTimeline(items) {
  const source = Array.isArray(items) ? items : [];
  const out = [];
  let index = 0;
  while (index < source.length) {
    const item = source[index];
    if (!isEvidenceItem(item)) {
      out.push(item);
      index += 1;
      continue;
    }
    const run = [];
    while (index < source.length && isEvidenceItem(source[index])) {
      run.push(source[index]);
      index += 1;
    }
    const previous = out[out.length - 1];
    const next = source[index];
    if (previous?.kind === "assistant") {
      out[out.length - 1] = {
        ...previous,
        evidence: [...(previous.evidence || []), ...run],
      };
    } else if (next?.kind === "assistant") {
      out.push({ ...next, evidence: [...run, ...(next.evidence || [])] });
      index += 1;
    } else {
      out.push({ kind: "evidence", key: `evidence:${run[0].key}`, evidence: run });
    }
  }
  return out;
}

/** Voice reads the conversational answer only, never the evidence. */
export function spokenReply(doneEvent, streamedText) {
  const spoken = doneEvent?.response?.spoken_text;
  return typeof spoken === "string" ? spoken : String(streamedText || "");
}
