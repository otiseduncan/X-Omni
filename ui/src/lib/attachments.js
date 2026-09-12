/* Operator file attachments -- upload, presentation, and message splitting.

   Core stores one chat message containing both what Otis typed and the text
   X read out of each attached file. The model needs that combined message;
   a reader does not want 6,000 characters of extracted PDF inside a chat
   bubble. splitAttachmentBlocks() separates the two again for display. */

export const MAX_ATTACHMENTS_PER_MESSAGE = 8;
export const MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024;

const KIND_LABELS = {
  image: "image",
  pdf: "PDF",
  text: "text file",
  docx: "Word document",
  xlsx: "Excel workbook",
};

/* Only the block header is matched, and only at the start of a line, so
   an attachment marker typed inside an ordinary message cannot split it. */
const ATTACHMENT_BLOCK = /^\[Attachment: .+? -- .+?, id \d+\]$/;

export function kindLabel(kind) {
  return KIND_LABELS[kind] || "file";
}

export function humanSize(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** Client-side guard so an obviously oversized file never leaves the browser. */
export function tooLarge(file) {
  return Number(file?.size || 0) > MAX_ATTACHMENT_BYTES;
}

/**
 * Upload one file and return the stored attachment record.
 *
 * Core reads the file during this request -- a PDF is extracted, an image is
 * transcribed by the vision worker -- so this can legitimately take a while.
 * The caller shows per-file progress rather than blocking the composer.
 */
export async function uploadAttachment(file) {
  const body = new FormData();
  body.append("file", file, file.name);

  const response = await fetch("/api/attachments", {
    method: "POST",
    credentials: "include",
    body,
  });

  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(
      payload.detail || payload.message || `Upload failed (HTTP ${response.status})`
    );
  }
  return payload;
}

/** Read further into an attachment -- the same text X can page through. */
export async function fetchAttachmentText(attachmentId, offset = 0) {
  const query = new URLSearchParams({ offset: String(offset) });
  const response = await fetch(
    `/api/attachments/${encodeURIComponent(attachmentId)}/text?${query}`,
    { credentials: "include", cache: "no-store" }
  );
  if (!response.ok) throw new Error(`Could not read the attachment (HTTP ${response.status})`);
  return response.json();
}

export function attachmentDownloadUrl(attachmentId, { download = false } = {}) {
  const query = download ? "?download=true" : "";
  return `/api/attachments/${encodeURIComponent(attachmentId)}${query}`;
}

/**
 * Split a stored user message into what Otis typed and what X read from each
 * file, so the chat bubble shows the message and keeps the extracted text
 * behind a toggle.
 *
 * Falls back to treating everything as typed text when no block header is
 * found, which keeps every pre-attachment message rendering exactly as before.
 */
export function splitAttachmentBlocks(text) {
  const body = String(text ?? "");
  const lines = body.split("\n");
  const starts = [];
  lines.forEach((line, index) => {
    if (ATTACHMENT_BLOCK.test(line.trim())) starts.push(index);
  });
  if (!starts.length) return { text: body, blocks: [] };

  const blocks = starts.map((start, position) => {
    const end = position + 1 < starts.length ? starts[position + 1] : lines.length;
    const [header, ...rest] = lines.slice(start, end);
    const match = header.trim().match(/^\[Attachment: (.+?) -- (.+?), id (\d+)\]$/);
    return {
      id: match ? Number(match[3]) : null,
      filename: match ? match[1] : "attachment",
      descriptor: match ? match[2] : "",
      body: rest.join("\n").trim(),
    };
  });

  const typed = lines.slice(0, starts[0]).join("\n").trim();
  const preamble = "(The operator attached the following with no message text.)";
  return {
    text: typed === preamble ? "" : typed,
    blocks,
  };
}
