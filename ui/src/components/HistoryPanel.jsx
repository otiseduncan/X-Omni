import { useCallback, useEffect, useState } from "react";
import { Check, Download, FileJson, FileText, Loader2, MessageSquare, X } from "lucide-react";

/* Past conversations.

   Before this panel, the UI could only restore the most recent conversation,
   so a conversation that went wrong could be abandoned but never revisited.
   Everything here is about getting back in: reopen a thread and keep working,
   or export the whole exchange and hand it to someone else for examination. */

function when(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  // SQLite stores naive UTC; make that explicit before the browser localises.
  const parsed = new Date(text.includes("T") ? text : `${text.replace(" ", "T")}Z`);
  if (Number.isNaN(parsed.getTime())) return text;
  const now = new Date();
  const sameDay = parsed.toDateString() === now.toDateString();
  return sameDay
    ? parsed.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : parsed.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

function title(conversation) {
  const given = String(conversation.title || "").trim();
  return given || `Conversation ${conversation.id}`;
}

export default function HistoryPanel({ open, activeId, onOpenConversation, onClose }) {
  const [conversations, setConversations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(null);
  const [copied, setCopied] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/conversations", {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      setConversations(Array.isArray(payload) ? payload : payload.conversations || []);
    } catch (problem) {
      setError(`Could not load past conversations: ${problem.message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const exportUrl = (id, format) =>
    `/api/conversations/${id}/export?format=${format}&download=true`;

  /* Copying the Markdown transcript is the fast path for the actual use
     case: paste the whole exchange into another assistant and ask about it. */
  const copyTranscript = useCallback(async (id) => {
    setExporting(`copy-${id}`);
    setError("");
    try {
      const response = await fetch(
        `/api/conversations/${id}/export?format=markdown&download=false`,
        { credentials: "include", cache: "no-store" }
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await navigator.clipboard.writeText(await response.text());
      setCopied(id);
      setTimeout(() => setCopied((current) => (current === id ? null : current)), 2000);
    } catch (problem) {
      setError(
        `Could not copy the transcript: ${problem.message}. Use Markdown to download it instead.`
      );
    } finally {
      setExporting(null);
    }
  }, []);

  if (!open) return null;

  return (
    <div
      className="panel-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <section
        className="history-panel"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Conversation history"
      >
        <header className="history-head">
          <h2>
            <MessageSquare size={15} /> Conversations
          </h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close history">
            <X size={16} />
          </button>
        </header>

        <p className="history-hint">
          Reopen any past conversation to keep working in it, or export the whole
          exchange — messages, files, and the tool calls that ran — to hand to
          another assistant.
        </p>

        {error && <div className="history-error">{error}</div>}

        {loading && (
          <div className="history-loading">
            <Loader2 size={14} className="spin" /> Loading…
          </div>
        )}

        {!loading && !conversations.length && !error && (
          <div className="history-empty">No conversations yet.</div>
        )}

        <ul className="history-list">
          {conversations.map((conversation) => {
            const isActive = Number(conversation.id) === Number(activeId);
            return (
              <li
                key={conversation.id}
                className={`history-row${isActive ? " is-active" : ""}`}
              >
                <button
                  type="button"
                  className="history-open"
                  onClick={() => onOpenConversation(conversation.id)}
                  title={isActive ? "Already open" : "Open this conversation"}
                >
                  <span className="history-title">{title(conversation)}</span>
                  <span className="history-meta">
                    #{conversation.id}
                    {conversation.updated_at ? ` · ${when(conversation.updated_at)}` : ""}
                    {isActive ? " · open" : ""}
                  </span>
                </button>

                <div className="history-actions">
                  <button
                    type="button"
                    className="history-action"
                    onClick={() => copyTranscript(conversation.id)}
                    disabled={exporting === `copy-${conversation.id}`}
                    title="Copy the whole transcript to the clipboard"
                  >
                    {exporting === `copy-${conversation.id}` ? (
                      <Loader2 size={13} className="spin" />
                    ) : copied === conversation.id ? (
                      <Check size={13} />
                    ) : (
                      <Download size={13} />
                    )}
                    {copied === conversation.id ? "copied" : "copy"}
                  </button>

                  <a
                    className="history-action"
                    href={exportUrl(conversation.id, "markdown")}
                    title="Download as Markdown"
                  >
                    <FileText size={13} /> md
                  </a>

                  <a
                    className="history-action"
                    href={exportUrl(conversation.id, "json")}
                    title="Download as JSON"
                  >
                    <FileJson size={13} /> json
                  </a>
                </div>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}
