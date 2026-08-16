function htmlText(value) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("No generated HTML is available.");
  }
  return value;
}

const WEBSITE_VIEW_STORAGE_PREFIX = "xomni.websiteView:";

function browserStorage(explicitStorage) {
  if (explicitStorage !== undefined) return explicitStorage;
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

/**
 * Return the durable identity for one exact website artifact. Current Core
 * artifacts expose a content hash; the artifact-id branches keep this ready
 * for a future revision-aware backend without guessing from a mutable title.
 */
export function websiteArtifactIdentity(data) {
  const explicit = data?.website_id || data?.lineage_id || data?.artifact_id || data?.preview?.artifact_id;
  if (typeof explicit === "string" && explicit.trim()) {
    return `id:${encodeURIComponent(explicit.trim().slice(0, 200))}`;
  }
  const sha256 = typeof data?.sha256 === "string" ? data.sha256.trim().toLowerCase() : "";
  return /^[a-f0-9]{64}$/.test(sha256) ? `sha256:${sha256}` : "";
}

export function websiteViewStorageKey(data) {
  const identity = websiteArtifactIdentity(data);
  return identity ? `${WEBSITE_VIEW_STORAGE_PREFIX}${identity}` : "";
}

/** Restore only the display choice. Generated HTML and titles never enter
 * browser storage; persisted conversation history remains authoritative. */
export function restoredWebsiteView(data, { storage } = {}) {
  const key = websiteViewStorageKey(data);
  if (!key) return "code";
  try {
    const target = browserStorage(storage);
    const stored = target?.getItem(key);
    if (stored === "preview" || stored === "code") return stored;

    // The first revision-aware update can add a lineage ID to an older
    // hash-only artifact. Inherit that parent card's display choice once so
    // replacing the card does not unexpectedly flip Preview back to Code.
    const parentSha256 = String(
      data?.parent_sha256 || data?.parent_hash || data?.supersedes_sha256 || ""
    ).trim().toLowerCase();
    if (/^[a-f0-9]{64}$/.test(parentSha256)) {
      const inherited = target?.getItem(
        `${WEBSITE_VIEW_STORAGE_PREFIX}sha256:${parentSha256}`
      );
      if (inherited === "preview" || inherited === "code") return inherited;
    }
    return "code";
  } catch {
    return "code";
  }
}

export function persistWebsiteView(data, view, { storage } = {}) {
  const normalized = view === "preview" ? "preview" : "code";
  const key = websiteViewStorageKey(data);
  if (!key) return normalized;
  try {
    browserStorage(storage)?.setItem(key, normalized);
  } catch {
    // Storage can be disabled; the mounted card still keeps local state.
  }
  return normalized;
}

export function websiteHtmlFilename(title) {
  const stem = String(title || "generated-website")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "");
  return `${stem || "generated-website"}.html`;
}

export async function copyGeneratedHtml(
  html,
  { clipboard = globalThis.navigator?.clipboard } = {}
) {
  const content = htmlText(html);
  if (!clipboard?.writeText) {
    throw new Error("Clipboard access is not available in this browser.");
  }
  await clipboard.writeText(content);
  return content.length;
}

/**
 * Download an exact generated HTML buffer without opening a new view. The
 * object URL is short-lived and revoked after the synthetic link click.
 */
export function downloadGeneratedHtml(
  html,
  title,
  {
    documentRef = globalThis.document,
    urlApi = globalThis.URL,
    BlobCtor = globalThis.Blob,
    deferCleanup = (callback) => globalThis.setTimeout(callback, 0),
  } = {}
) {
  const content = htmlText(html);
  if (!documentRef?.createElement || !documentRef?.body || !urlApi?.createObjectURL || !BlobCtor) {
    throw new Error("HTML download is not available in this browser.");
  }

  const blob = new BlobCtor([content], { type: "text/html;charset=utf-8" });
  const objectUrl = urlApi.createObjectURL(blob);
  let anchor = null;

  try {
    anchor = documentRef.createElement("a");
    anchor.href = objectUrl;
    anchor.download = websiteHtmlFilename(title);
    anchor.rel = "noopener";
    anchor.hidden = true;
    documentRef.body.appendChild(anchor);
    anchor.click();
  } finally {
    try {
      anchor?.remove?.();
    } finally {
      deferCleanup(() => urlApi.revokeObjectURL(objectUrl));
    }
  }

  return { filename: anchor?.download || websiteHtmlFilename(title), bytes: blob.size };
}
