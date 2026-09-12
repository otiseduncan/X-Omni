import { AlertCircle, FileSpreadsheet, FileText, Image as ImageIcon, Loader2, Paperclip, X } from "lucide-react";

import { humanSize, kindLabel } from "../lib/attachments.js";

/* Files staged in the composer, above the text box.

   Each file is uploaded and read the moment it is chosen, not when the
   message is sent, so a PDF that X could not make sense of is visible here
   before the message goes out rather than after. */

const KIND_ICONS = {
  image: ImageIcon,
  pdf: FileText,
  text: FileText,
  docx: FileText,
  xlsx: FileSpreadsheet,
};

function Icon({ kind }) {
  const Glyph = KIND_ICONS[kind] || Paperclip;
  return <Glyph size={14} />;
}

function statusLine(entry) {
  if (entry.status === "uploading") {
    return entry.kind === "image"
      ? "X is reading the image…"
      : "reading…";
  }
  if (entry.status === "error") return entry.error;
  const parts = [kindLabel(entry.kind), humanSize(entry.bytes)];
  if (entry.pageCount) parts.splice(1, 0, `${entry.pageCount} page(s)`);
  if (entry.truncated) parts.push("truncated");
  return parts.join(" · ");
}

export default function AttachmentTray({ entries, onRemove, onRetry }) {
  if (!entries.length) return null;

  return (
    <div className="attach-tray" aria-label="Files attached to this message">
      {entries.map((entry) => (
        <div
          key={entry.key}
          className={`attach-chip${entry.status === "error" ? " is-error" : ""}`}
          title={entry.note || entry.filename}
        >
          <span className="attach-chip-icon">
            {entry.status === "uploading" ? (
              <Loader2 size={14} className="spin" />
            ) : entry.status === "error" ? (
              <AlertCircle size={14} />
            ) : (
              <Icon kind={entry.kind} />
            )}
          </span>

          <span className="attach-chip-body">
            <span className="attach-chip-name">{entry.filename}</span>
            <span className="attach-chip-meta">{statusLine(entry)}</span>
          </span>

          {entry.status === "error" && onRetry && (
            <button
              type="button"
              className="attach-chip-action"
              onClick={() => onRetry(entry.key)}
            >
              retry
            </button>
          )}

          <button
            type="button"
            className="attach-chip-remove"
            onClick={() => onRemove(entry.key)}
            aria-label={`Remove ${entry.filename}`}
            title="Remove"
          >
            <X size={13} />
          </button>
        </div>
      ))}
    </div>
  );
}
