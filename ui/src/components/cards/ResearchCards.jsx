import { useEffect, useRef, useState } from "react";
import {
  ExternalLink,
  Globe2,
  Loader2,
  LogIn,
  RefreshCw,
  Save,
} from "lucide-react";

function Card({ title, children, tone = "" }) {
  return (
    <div className={`card field-card research-provider-card${tone ? ` tone-${tone}` : ""}`}>
      <div className="card-head">
        <Globe2 size={14} />
        <span>{title}</span>
      </div>
      {children}
    </div>
  );
}

async function payload(response, fallback) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body?.detail === "string"
      ? body.detail
      : typeof body?.message === "string"
        ? body.message
        : fallback;
    throw new Error(String(detail || fallback).slice(0, 500));
  }
  return body && typeof body === "object" ? body : {};
}

function SourceLedger({ rows }) {
  return (
    <div className="research-source-ledger" aria-label="Research source verification">
      {(rows || []).map((row, index) => {
        const verified = row?.verified === true;
        const searched = row?.searched === true;
        const state = verified ? "searched · verified" : searched ? "searched · not verified" : "not searched";
        const count = Number.isFinite(Number(row?.result_count)) ? Number(row.result_count) : null;
        return (
          <div className={`research-source-row${verified ? " is-verified" : " is-warning"}`} key={`${row?.source || "source"}-${index}`}>
            <div className="research-source-line">
              <strong>{row?.source || "Source"}</strong>
              <span className={`ro-pill ${verified ? "ok" : "warn"}`}>{state}</span>
            </div>
            <div className="research-source-detail">
              {row?.query_submitted === true && <span>query submitted</span>}
              {count != null && <span>{count} result{count === 1 ? "" : "s"}</span>}
              {row?.requested_make && <span>make: {row.requested_make}</span>}
            </div>
            {row?.reason && <p className="card-note research-source-reason">{row.reason}</p>}
          </div>
        );
      })}
    </div>
  );
}

function FullResearchResult({ data }) {
  const ledger = Array.isArray(data?.source_ledger) ? data.source_ledger : [];
  const adasHits = Array.isArray(data?.adas_si?.hits) ? data.adas_si.hits : [];
  const publicSources = Array.isArray(data?.public_oem?.sources) ? data.public_oem.sources : [];
  const publicReads = Array.isArray(data?.public_oem?.read_results) ? data.public_oem.read_results : [];
  const captures = Array.isArray(data?.captures) ? data.captures : [];
  const allData = data?.alldata && typeof data.alldata === "object" ? data.alldata : {};
  const externalVerified = data?.external_search_verified === true;

  return (
    <Card
      title={externalVerified ? "Post-collision research · source verified" : "Post-collision research · incomplete"}
      tone={externalVerified ? "" : "warn"}
    >
      <p className="card-note research-query">
        {data?.requested_manufacturer && <strong>{data.requested_manufacturer} · </strong>}
        {data?.query || "Research request"}
      </p>

      <p className="rail-sub research-section-title">What X actually searched</p>
      <SourceLedger rows={ledger} />

      {adasHits.length > 0 && (
        <details className="research-evidence-group" open>
          <summary>ADAS SI evidence · {adasHits.length}</summary>
          {adasHits.map((hit, index) => (
            <div className="research-evidence-item" key={`${hit?.title || "adas"}-${hit?.page || index}`}>
              <div className="research-evidence-title">
                <strong>{hit?.title || "ADAS SI result"}</strong>
                {hit?.page != null && <span className="field-page">p.{hit.page}</span>}
              </div>
              {hit?.vehicle?.make && (
                <p className="card-note field-vehicle">
                  {[hit.vehicle.year, hit.vehicle.make, hit.vehicle.model, hit.vehicle.topic].filter(Boolean).join(" · ")}
                </p>
              )}
              <pre className="pre field-excerpt research-readable-text">{String(hit?.excerpt || "").slice(0, 5000)}</pre>
            </div>
          ))}
        </details>
      )}

      <details className="research-evidence-group" open={allData?.verified === true}>
        <summary>ALLDATA evidence</summary>
        {allData?.verified === true ? (
          <>
            <div className="kv research-evidence-kv">
              <div><span>Query submitted</span><strong>{allData?.query_submitted ? "yes" : "no"}</strong></div>
              {allData?.title && <div><span>Page</span><strong>{allData.title}</strong></div>}
              {allData?.relevance_score != null && <div><span>Matched terms</span><strong>{allData.relevance_score}</strong></div>}
            </div>
            {allData?.page_text && (
              <pre className="pre field-excerpt research-readable-text">{String(allData.page_text).slice(0, 10000)}</pre>
            )}
          </>
        ) : (
          <p className="card-note ciq-incomplete">
            ALLDATA was not verified as searched{allData?.reason ? ` — ${allData.reason}` : "."}
          </p>
        )}
      </details>

      <details className="research-evidence-group" open={publicSources.length > 0}>
        <summary>Public OEM / manufacturer sources · {publicSources.length}</summary>
        {publicSources.length === 0 ? (
          <p className="card-note">No public OEM source result was returned.</p>
        ) : (
          <div className="ro-list research-public-list">
            {publicSources.map((source, index) => (
              <div className="ro-item" key={`${source?.url || source?.title}-${index}`}>
                <div className="research-evidence-title"><strong>{source?.title || source?.url}</strong></div>
                {source?.snippet && <p className="card-note research-public-snippet">{source.snippet}</p>}
                {source?.url && (
                  <a className="field-link research-source-link" href={source.url} target="_blank" rel="noreferrer">
                    <ExternalLink size={12} /> Open source
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
        {publicReads.map((read, index) => read?.page_text ? (
          <details className="research-read-result" key={`${read?.url || "read"}-${index}`}>
            <summary>{read?.title || read?.url || "Read source"}</summary>
            <pre className="pre field-excerpt research-readable-text">{String(read.page_text).slice(0, 9000)}</pre>
          </details>
        ) : null)}
      </details>

      {captures.length > 0 && (
        <details className="research-evidence-group">
          <summary>Saved to ADAS SI · {captures.length}</summary>
          {captures.map((item, index) => (
            <div className="research-capture-row" key={`${item?.relative_path || item?.url || index}`}>
              <span className={`ro-pill ${item?.status === "success" ? "ok" : "warn"}`}>{item?.source || "source"}</span>
              <span>{item?.relative_path || item?.url || item?.error || "Capture result"}</span>
            </div>
          ))}
        </details>
      )}

      <p className="card-note research-authority-note">
        {data?.authority_note || "OEM/manufacturer, insurer, and legal/regulatory requirements are separate authorities."}
      </p>
    </Card>
  );
}

function ExternalResults({ data }) {
  if (data?.action === "public_read") {
    return (
      <Card title="Public OEM / collision source">
        {data?.title && <strong>{data.title}</strong>}
        {data?.url && (
          <p style={{ margin: "8px 0" }}>
            <a className="field-link" href={data.url} target="_blank" rel="noreferrer">
              <ExternalLink size={12} /> Open source
            </a>
          </p>
        )}
        <pre className="pre field-excerpt research-readable-text">
          {String(data?.page_text || data?.message || "No readable source text was returned.").slice(0, 12000)}
        </pre>
      </Card>
    );
  }

  const sources = Array.isArray(data?.sources) ? data.sources : [];
  return (
    <Card title="Post-collision web research" tone={sources.length ? "" : "warn"}>
      {sources.length === 0 ? (
        <p className="card-note">{data?.summary || "No public source matched."}</p>
      ) : (
        <div className="ro-list">
          {sources.map((source, index) => (
            <div className="ro-item" key={`${source.url || source.title}-${index}`}>
              <div className="ro-line"><strong>{source.title || source.url}</strong></div>
              {source.snippet && <p className="card-note">{source.snippet}</p>}
              {source.url && (
                <a className="field-link" href={source.url} target="_blank" rel="noreferrer">
                  <ExternalLink size={12} /> Open source
                </a>
              )}
            </div>
          ))}
        </div>
      )}
      <p className="card-note" style={{ marginTop: 8 }}>
        OEM, insurer, and legal/regulatory requirements are separate authorities.
      </p>
    </Card>
  );
}

function CaptureResult({ data }) {
  return (
    <Card title="Research source saved" tone={data?.status === "success" ? "" : "warn"}>
      <div className="kv">
        <div><span>Provider</span><strong>{data?.provider || data?.source || "research source"}</strong></div>
        <div><span>Saved</span><strong>{data?.saved ? "yes" : "no"}</strong></div>
        {data?.pages != null && <div><span>Pages</span><strong>{data.pages}</strong></div>}
        {data?.readable_pages != null && <div><span>Readable</span><strong>{data.readable_pages}</strong></div>}
      </div>
      {data?.relative_path && <p className="card-note" style={{ marginTop: 8 }}>{data.relative_path}</p>}
    </Card>
  );
}

function ExtractResult({ data }) {
  return (
    <Card title="ALLDATA research page">
      <div className="kv">
        <div><span>Authenticated</span><strong>{data?.authenticated ? "yes" : "not verified"}</strong></div>
        {data?.title && <div><span>Page</span><strong>{data.title}</strong></div>}
      </div>
      <pre className="pre field-excerpt research-readable-text">
        {String(data?.page_text || "No page text returned.").slice(0, 12000)}
      </pre>
    </Card>
  );
}

function AccessCard({ data }) {
  const userId = useRef(`alldata-user-${Math.random().toString(36).slice(2)}`);
  const passId = useRef(`alldata-pass-${Math.random().toString(36).slice(2)}`);
  const passwordRef = useRef(null);
  const [username, setUsername] = useState(data?.credential?.username || "");
  const [configured, setConfigured] = useState(data?.credential?.configured === true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [starting, setStarting] = useState(false);
  const [message, setMessage] = useState("");
  const [session, setSession] = useState(data?.browser_active && data?.session_id ? data : null);
  const [shotNonce, setShotNonce] = useState(Date.now());
  const [manualText, setManualText] = useState("");

  async function refreshStatus() {
    const response = await fetch("/api/research/providers/alldata/status", { credentials: "include", cache: "no-store" });
    const status = await payload(response, "Could not check ALLDATA access.");
    setConfigured(status?.credential?.configured === true);
    if (status?.credential?.username) setUsername(status.credential.username);
    if (status?.browser_active && status?.session_id) setSession(status);
    return status;
  }

  useEffect(() => {
    let active = true;
    refreshStatus().catch((error) => active && setMessage(error.message)).finally(() => active && setLoading(false));
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!session?.session_id) return undefined;
    const timer = window.setInterval(() => setShotNonce(Date.now()), 1800);
    return () => window.clearInterval(timer);
  }, [session?.session_id]);

  async function saveCredentials(event) {
    event.preventDefault();
    if (saving) return;
    let password = passwordRef.current?.value || "";
    if (passwordRef.current) passwordRef.current.value = "";
    if (!username.trim() || !password) {
      setMessage("Enter your ALLDATA username and password.");
      return;
    }
    setSaving(true);
    setMessage("");
    try {
      const pending = fetch("/api/research/providers/alldata/credentials", {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      password = "";
      const saved = await payload(await pending, "Could not save ALLDATA credentials.");
      setConfigured(saved.configured === true);
      setUsername(saved.username || username.trim());
      setMessage(saved.configured ? "ALLDATA credentials saved securely." : "Credentials were not saved.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      password = "";
      if (passwordRef.current) passwordRef.current.value = "";
      setSaving(false);
    }
  }

  async function startBrowser() {
    if (starting) return;
    setStarting(true);
    setMessage("");
    try {
      const response = await fetch("/api/research/providers/alldata/sessions", {
        method: "POST", credentials: "include", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const next = await payload(response, "Could not start the ALLDATA browser.");
      setSession(next);
      setShotNonce(Date.now());
      setMessage(next.authenticated
        ? "ALLDATA is authenticated. X can continue the research."
        : "ALLDATA needs a login or human authentication step. Use the browser below.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setStarting(false);
    }
  }

  async function browserAction(action) {
    if (!session?.session_id) return;
    const response = await fetch(`/api/research/providers/alldata/sessions/${encodeURIComponent(session.session_id)}/action`, {
      method: "POST", credentials: "include", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(action),
    });
    const next = await payload(response, "Browser action failed.");
    setSession(next);
    setShotNonce(Date.now());
  }

  async function sendManualText() {
    const text = manualText;
    setManualText("");
    if (!text) return;
    try { await browserAction({ action: "type", text }); }
    catch (error) { setMessage(error.message); }
  }

  const sessionId = session?.session_id;
  const screenshot = sessionId
    ? `/api/research/providers/alldata/sessions/${encodeURIComponent(sessionId)}/screenshot?t=${shotNonce}`
    : "";

  return (
    <Card title="ALLDATA research access">
      <p className="card-note">
        This is X&apos;s licensed research login. The password goes directly to Core and Windows Credential Manager; it is not put in chat, browser storage, or model context.
      </p>
      {loading ? (
        <p className="card-note"><Loader2 size={13} className="spin" /> Checking ALLDATA setup…</p>
      ) : !configured ? (
        <form onSubmit={saveCredentials} style={{ marginTop: 10 }}>
          <label className="exterior-camera-field" htmlFor={userId.current}>
            <span>ALLDATA username</span>
            <input id={userId.current} value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck="false" maxLength={320} required />
          </label>
          <label className="exterior-camera-field" htmlFor={passId.current}>
            <span>ALLDATA password</span>
            <input id={passId.current} ref={passwordRef} type="password" autoComplete="current-password" required />
          </label>
          <button className="camera-action" type="submit" disabled={saving} style={{ marginTop: 10 }}>
            {saving ? <Loader2 size={15} className="spin" /> : <Save size={15} />}
            {saving ? "Saving…" : "Save ALLDATA credentials"}
          </button>
        </form>
      ) : (
        <>
          <div className="kv" style={{ marginTop: 9 }}>
            <div><span>Credential vault</span><strong>Windows Credential Manager</strong></div>
            <div><span>Username</span><strong>{username || "saved"}</strong></div>
            <div><span>Password</span><strong>stored · hidden from X</strong></div>
          </div>
          <button className="camera-action" type="button" onClick={startBrowser} disabled={starting} style={{ marginTop: 10 }}>
            {starting ? <Loader2 size={15} className="spin" /> : <LogIn size={15} />}
            {starting ? "Opening ALLDATA…" : sessionId ? "Resume ALLDATA browser" : "Open ALLDATA browser"}
          </button>
        </>
      )}
      {message && <p className="card-note" role="status" style={{ marginTop: 8 }}>{message}</p>}
      {sessionId && (
        <div style={{ marginTop: 12 }}>
          <div className="ro-line ro-sub" style={{ marginBottom: 6 }}>
            <span className={`ro-pill ${session?.authenticated ? "done" : "warn"}`}>{session?.authenticated ? "authenticated" : "human step"}</span>
            <span>{session?.title || "ALLDATA"}</span>
          </div>
          <p className="card-note">Tap the browser image to click. This works from your phone; use the text box for MFA, verification codes, or other human-only input.</p>
          <img
            src={screenshot}
            alt="Live ALLDATA browser session"
            style={{ width: "100%", display: "block", borderRadius: 8, marginTop: 8, cursor: "crosshair" }}
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              const x = ((event.clientX - rect.left) / rect.width) * 1280;
              const y = ((event.clientY - rect.top) / rect.height) * 900;
              browserAction({ action: "click", x, y }).catch((error) => setMessage(error.message));
            }}
          />
          <input value={manualText} onChange={(event) => setManualText(event.target.value)} placeholder="Type into the focused ALLDATA field" autoComplete="one-time-code" style={{ width: "100%", boxSizing: "border-box", marginTop: 8 }} />
          <div className="field-actions" style={{ marginTop: 8, flexWrap: "wrap" }}>
            <button type="button" onClick={sendManualText}>Type</button>
            <button type="button" onClick={() => browserAction({ action: "press", key: "Tab" }).catch((e) => setMessage(e.message))}>Tab</button>
            <button type="button" onClick={() => browserAction({ action: "press", key: "Enter" }).catch((e) => setMessage(e.message))}>Enter</button>
            <button type="button" onClick={() => browserAction({ action: "scroll", dy: 700 }).catch((e) => setMessage(e.message))}>Scroll ↓</button>
            <button type="button" onClick={() => browserAction({ action: "scroll", dy: -700 }).catch((e) => setMessage(e.message))}>Scroll ↑</button>
            <button type="button" onClick={() => setShotNonce(Date.now())}><RefreshCw size={12} /> Refresh</button>
          </div>
        </div>
      )}
      <p className="card-note" style={{ marginTop: 10 }}>X performs targeted research only. It does not bypass CAPTCHA, access controls, subscription limits, or other provider boundaries.</p>
    </Card>
  );
}

export function ResearchProviderCard({ data }) {
  if (data?.action === "full_research") return <FullResearchResult data={data} />;
  if (data?.action === "public_search" || data?.action === "public_read") return <ExternalResults data={data} />;
  if (data?.action === "capture_to_adas" || data?.action === "public_capture") return <CaptureResult data={data} />;
  if (data?.action === "extract" || data?.action === "snapshot") return <ExtractResult data={data} />;
  return <AccessCard data={data} />;
}
