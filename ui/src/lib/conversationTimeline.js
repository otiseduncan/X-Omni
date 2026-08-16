import { safeCameraObservationArtifact } from "./cameraCapture.js";

const APPROVAL_STATES = new Set([
  "pending",
  "deciding",
  "approved",
  "executing",
  "succeeded",
  "failed",
  "denied",
  "expired",
  "indeterminate",
]);

function text(value) {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  return value ? [value] : [];
}

function approvalId(value) {
  return value?.approval_id || value?.approvalId || value?.id || null;
}

function websiteDigest(value) {
  const digest = typeof value === "string" ? value.trim().toLowerCase() : "";
  return /^[a-f0-9]{64}$/.test(digest) ? digest : "";
}

/**
 * Return revision metadata only for a complete successful website artifact.
 * Failed, blocked, timed-out, and malformed attempts deliberately return null
 * so they remain visible beside the last usable revision.
 */
export function websiteRevision(item) {
  if (item?.kind !== "artifact" || item?.artifact?.type !== "website_preview") return null;
  const data = item.artifact.data;
  if (!data || data.ok !== true || typeof data.html !== "string" || !data.html.trim()) return null;
  const status = text(data.status);
  if (["blocked", "error", "failed", "timeout", "timed_out", "unavailable"].includes(status)) {
    return null;
  }
  const sha256 = websiteDigest(data.sha256);
  if (!sha256) return null;
  const rawLineage = data.website_id || data.lineage_id || data.site_id;
  const lineage = typeof rawLineage === "string" ? rawLineage.trim().slice(0, 200) : "";
  const parentSha256 = websiteDigest(
    data.parent_sha256 || data.parent_hash || data.supersedes_sha256
  );
  return { lineage, parentSha256, sha256 };
}

function supersededWebsitePosition(items, incoming) {
  const revision = websiteRevision(incoming);
  if (!revision || (!revision.lineage && !revision.parentSha256)) return -1;

  for (let index = items.length - 1; index >= 0; index -= 1) {
    const current = websiteRevision(items[index]);
    if (!current) continue;
    const lineageMatches = Boolean(
      revision.lineage && current.lineage && revision.lineage === current.lineage
    );
    const parentMatches = Boolean(
      revision.parentSha256 && revision.parentSha256 === current.sha256
    );
    // If a parent is supplied it must be the currently visible revision. This
    // prevents a delayed/stale branch from silently replacing a newer card.
    if ((lineageMatches && (!revision.parentSha256 || parentMatches)) || parentMatches) {
      return index;
    }
  }
  return -1;
}

/** Append a live item while letting a proved website revision update its
 * existing chat card. The previous React key is retained intentionally. */
export function upsertTimelineItem(items, incoming) {
  const sameKey = items.findIndex((item) => item.key === incoming.key);
  if (sameKey >= 0) {
    const next = [...items];
    next[sameKey] = { ...items[sameKey], ...incoming };
    return next;
  }

  const superseded = supersededWebsitePosition(items, incoming);
  if (superseded < 0) return [...items, incoming];
  const next = [...items];
  next[superseded] = { ...incoming, key: items[superseded].key };
  return next;
}

export function coalesceWebsiteRevisions(items) {
  return items.reduce((current, item) => upsertTimelineItem(current, item), []);
}

/**
 * A successful write is shown only when the durable receipt says all three
 * things: it reached the success terminal, it executed, and it succeeded.
 * Approval alone and a plain tool result are deliberately insufficient.
 */
export function receiptState(receipt) {
  if (!receipt || typeof receipt !== "object") return null;

  const status = text(receipt.status || receipt.outcome);
  if (status === "denied") return "denied";
  if (status === "expired") return "expired";
  if (["failed", "error", "blocked", "cancelled"].includes(status)) return "failed";
  if (
    ["succeeded", "success", "completed"].includes(status) &&
    receipt.executed === true &&
    receipt.success === true
  ) {
    return "succeeded";
  }
  if (status === "executing" || status === "running") return "executing";
  if (status === "approved") return "approved";
  return null;
}

/**
 * Keep the strict execution-proof predicate above while still giving an
 * inconsistent terminal receipt an honest UI state. A claimed success with
 * missing/contradictory proof must never fall back to a pending approval.
 */
export function receiptDisplayState(receipt) {
  if (
    receipt?.execution_state === "indeterminate" ||
    receipt?.may_have_executed === true ||
    receipt?.result?.execution_state === "indeterminate" ||
    receipt?.result?.may_have_executed === true
  ) {
    return "indeterminate";
  }
  const verified = receiptState(receipt);
  if (verified) return verified;
  const status = text(receipt?.status || receipt?.outcome);
  if (["succeeded", "success", "completed"].includes(status)) return "indeterminate";
  return null;
}

/** Convert a live stored-artifact-shaped receipt into the same update used by
 * approval_receipt WebSocket events. */
export function receiptUpdateFromArtifact(artifact) {
  if (!artifact || !["execution_receipt", "approval_receipt"].includes(artifact.type)) {
    return null;
  }
  const receipt = artifact.data || artifact.receipt;
  const id = receipt?.approval_id || receipt?.approvalId || null;
  if (!receipt || !id) return null;
  return {
    id,
    receipt,
    status: receiptDisplayState(receipt) || "indeterminate",
  };
}

export function receiptToolForArtifact(type) {
  if (type === "shell_result") return "run_powershell";
  if (type === "generated_image") return "image_generate";
  return null;
}

export function receiptMatchesArtifact(receipt, type) {
  const tool = receiptToolForArtifact(type);
  return Boolean(tool && receipt?.tool_name === tool);
}

export function normaliseApproval(raw = {}) {
  const receipt = raw.receipt || null;
  const fromReceipt = receiptDisplayState(receipt);
  const requested = text(raw.status || raw.state) || "pending";
  // A naked "succeeded" state is not execution proof. Wait for its receipt.
  const status = fromReceipt ||
    (requested === "succeeded" ? "executing" : APPROVAL_STATES.has(requested) ? requested : "pending");

  return {
    id: approvalId(raw) || approvalId(receipt),
    tool: raw.tool || raw.tool_name || receipt?.tool_name || "approved action",
    summary: raw.summary || receipt?.summary || raw.tool || receipt?.tool_name || "Approved action",
    args: raw.args || raw.arguments || {},
    action_digest: raw.action_digest || receipt?.action_digest || null,
    idempotency_key: raw.idempotency_key || receipt?.idempotency_key || null,
    execution_state: raw.execution_state || receipt?.execution_state || receipt?.result?.execution_state || null,
    may_have_executed: raw.may_have_executed ?? receipt?.may_have_executed ?? receipt?.result?.may_have_executed ?? false,
    outcome_message: raw.outcome_message || receipt?.outcome_message || receipt?.result?.message || null,
    status,
    receipt,
  };
}

function messageItems(message) {
  if (!message || typeof message !== "object") return [];
  const id = message.id ?? message.message_id ?? `unknown-${message.created_at || ""}`;
  const role = text(message.role);
  const out = [];

  if (["user", "assistant", "system"].includes(role) && String(message.content || "").trim()) {
    out.push({
      kind: role,
      key: `message:${id}`,
      text: String(message.content),
      worker: message.worker_used || message.worker || null,
      server: true,
    });
  }

  asArray(message.approval || message.approvals).forEach((raw) => {
    const approval = normaliseApproval(raw);
    if (approval.id) {
      out.push({
        kind: "approval",
        key: `approval:${approval.id}`,
        approval,
        status: approval.status,
        receipt: approval.receipt,
        server: true,
      });
    }
  });

  const artifacts = asArray(message.artifacts);
  const executionReceipts = artifacts
    .map(receiptUpdateFromArtifact)
    .map((update) => update?.receipt)
    .filter(Boolean);

  function matchingArtifactReceipt(type) {
    return executionReceipts.find((receipt) => receiptMatchesArtifact(receipt, type)) || null;
  }

  artifacts.forEach((artifact, index) => {
    if (!artifact || typeof artifact !== "object") return;
    if (artifact.type === "approval_request" || artifact.type === "approval") {
      const approval = normaliseApproval(artifact.data || artifact.approval || artifact);
      if (approval.id) {
        out.push({
          kind: "approval",
          key: `approval:${approval.id}`,
          approval,
          status: approval.status,
          receipt: approval.receipt,
          server: true,
        });
      }
      return;
    }
    if (artifact.type === "execution_receipt" || artifact.type === "approval_receipt") {
      const receipt = artifact.data || artifact.receipt;
      const idFromReceipt = approvalId(receipt);
      if (idFromReceipt) {
        out.push({
          kind: "approval_receipt",
          key: `approval-receipt:${idFromReceipt}`,
          approvalId: idFromReceipt,
          receipt,
          server: true,
        });
      }
      return;
    }
    // Artifact rendering is registry-based. Unknown types safely render null.
    let safeArtifact = artifact;
    if (artifact.type === "camera_observation") {
      try {
        safeArtifact = safeCameraObservationArtifact(artifact);
      } catch {
        safeArtifact = {
          type: "camera_observation",
          data: {
            ok: false,
            description: "The stored camera observation could not be displayed safely.",
          },
        };
      }
    }
    const matchingReceipt = matchingArtifactReceipt(safeArtifact.type);
    out.push({
      kind: "artifact",
      key: `artifact:${id}:${index}`,
      artifact: matchingReceipt
        ? { ...safeArtifact, receipt: matchingReceipt }
        : safeArtifact,
      server: true,
    });
  });

  return out;
}

function combineApproval(existing, incoming) {
  return {
    ...existing,
    ...incoming,
    approval: {
      ...(existing.approval || {}),
      ...(incoming.approval || {}),
      args: incoming.approval?.args || existing.approval?.args || {},
    },
    receipt: incoming.receipt || existing.receipt || null,
  };
}

/** Convert stored messages and their artifacts into the same timeline shape
 * used by live WebSocket events. Approval receipts are folded back into the
 * original approval card instead of becoming duplicate cards. */
export function timelineFromHistory(payload) {
  const messages = Array.isArray(payload) ? payload : asArray(payload?.messages);
  const rawItems = messages.flatMap(messageItems);
  asArray(payload?.approvals).forEach((raw) => {
    const approval = normaliseApproval(raw);
    if (approval.id) {
      rawItems.push({
        kind: "approval",
        key: `approval:${approval.id}`,
        approval,
        status: approval.status,
        receipt: approval.receipt,
        server: true,
      });
    }
  });
  asArray(payload?.receipts).forEach((receipt) => {
    const id = approvalId(receipt);
    if (id) rawItems.push({ kind: "approval_receipt", approvalId: id, receipt });
  });

  const out = [];
  const positions = new Map();
  const deferredReceipts = new Map();

  rawItems.forEach((item) => {
    if (item.kind === "approval_receipt") {
      deferredReceipts.set(item.approvalId, item.receipt);
      const position = positions.get(`approval:${item.approvalId}`);
      if (position != null) {
        const current = out[position];
        const state = receiptDisplayState(item.receipt) || current.status;
        out[position] = combineApproval(current, {
          status: state,
          receipt: item.receipt,
          approval: normaliseApproval({ ...current.approval, receipt: item.receipt }),
        });
      }
      return;
    }

    const position = positions.get(item.key);
    if (position != null) {
      out[position] = item.kind === "approval" ? combineApproval(out[position], item) : item;
      return;
    }

    if (item.kind === "approval") {
      const id = item.approval.id;
      const receipt = deferredReceipts.get(id);
      if (receipt) {
        item = combineApproval(item, {
          status: receiptDisplayState(receipt) || item.status,
          receipt,
          approval: normaliseApproval({ ...item.approval, receipt }),
        });
      }
    }

    positions.set(item.key, out.length);
    out.push(item);
  });

  // A legacy or partial history may contain the receipt without its request.
  // Show it as a truthful, read-only action card rather than silently losing it.
  deferredReceipts.forEach((receipt, id) => {
    if (positions.has(`approval:${id}`)) return;
    const approval = normaliseApproval({ id, receipt });
    positions.set(`approval:${id}`, out.length);
    out.push({
      kind: "approval",
      key: `approval:${id}`,
      approval,
      status: approval.status,
      receipt,
      server: true,
    });
  });

  return coalesceWebsiteRevisions(out);
}

function signature(item) {
  if (!item) return "";
  if (["user", "assistant", "system"].includes(item.kind)) {
    return `${item.kind}|${String(item.text || "").trim()}|${item.worker || ""}`;
  }
  if (item.kind === "artifact") {
    try {
      return `artifact|${JSON.stringify(item.artifact)}`;
    } catch {
      return "";
    }
  }
  return "";
}

/** Merge a fresh server history with live-only UI entries. Stable IDs win;
 * semantic matching removes optimistic user/artifact duplicates. */
export function mergeTimelines(authoritative, live) {
  const merged = [...authoritative];
  const positions = new Map(merged.map((item, index) => [item.key, index]));
  const signatures = new Set(merged.map(signature).filter(Boolean));

  live.forEach((item) => {
    const position = positions.get(item.key);
    if (position != null) {
      if (item.kind === "approval") {
        // Preserve richer live request details while accepting server truth.
        merged[position] = combineApproval(item, merged[position]);
      }
      return;
    }
    const itemSignature = signature(item);
    if (itemSignature && signatures.has(itemSignature)) return;
    positions.set(item.key, merged.length);
    if (itemSignature) signatures.add(itemSignature);
    merged.push(item);
  });

  return coalesceWebsiteRevisions(merged);
}

export function updateApproval(items, id, update) {
  let found = false;
  const next = items.map((item) => {
    if (item.kind !== "approval" || item.approval?.id !== id) return item;
    found = true;
    const receipt = update.receipt || item.receipt || null;
    const requested = text(update.status);
    const safeStatus = receiptDisplayState(receipt) ||
      (requested === "succeeded" ? item.status || "executing" : requested || item.status);
    return combineApproval(item, {
      ...update,
      status: safeStatus,
      receipt,
      approval: normaliseApproval({
        ...item.approval,
        ...(update.approval || {}),
        ...update,
        receipt,
      }),
    });
  });

  if (found || !update.receipt) return next;
  const approval = normaliseApproval({ id, receipt: update.receipt });
  return [
    ...next,
    {
      kind: "approval",
      key: `approval:${id}`,
      approval,
      status: approval.status,
      receipt: update.receipt,
    },
  ];
}
