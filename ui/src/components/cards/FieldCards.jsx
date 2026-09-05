import { useState } from "react";
import {
  AlertTriangle,
  BookOpen,
  ClipboardList,
  ExternalLink,
  FileText,
  Layers,
  Wrench,
} from "lucide-react";
import {
  CALIBRATION_COLLECTION_WARNING,
  calibrationCountLabel,
  calibrationPhaseBreakdown,
  calibrationSummaryScopeLabel,
  calibrationStatusTone as statusTone,
} from "../../lib/calibrationIqPresentation.js";
import { scrapexCardPresentation } from "../../lib/scrapexCardPresentation.js";
import { ResearchProviderCard } from "./ResearchCards.jsx";

/*
 * Inline cards for Otis's two field systems.
 *
 * The point of these is that asking for something shows the real thing:
 * an ADAS SI request renders the actual PDF in the chat, and a repair
 * order request renders the actual board rows, not a prose description
 * of them.
 */

function Card({ icon: Icon, title, meta, children, tone }) {
  return (
    <div className={`card field-card${tone ? ` tone-${tone}` : ""}`}>
      <div className="card-head">
        <Icon size={14} />
        <span>{title}</span>
        {meta && <em className="field-meta">{meta}</em>}
      </div>
      {children}
    </div>
  );
}

/* ---------------- ADAS SI: the document itself ---------------- */

export function AdasDocumentCard({ data }) {
  const doc = data?.document;
  return (
    <Card icon={BookOpen} title={doc?.title || "ADAS SI"}>
      {doc ? (
        <DocumentViewer doc={doc} alternatives={data.alternatives} />
      ) : (
        <p className="card-note">{data?.message || "No document matched."}</p>
      )}
    </Card>
  );
}

/**
 * Inline page viewer.
 *
 * Pages are rendered server-side to PNG rather than embedded as a PDF.
 * Mobile browsers refuse to render PDFs in an iframe, and Otis is on a
 * phone in the field most of the time -- so the procedure has to appear
 * in the chat itself. It also means scanned documents display fine even
 * though no text can be extracted from them.
 *
 * The "Open PDF" link stays, because the real viewer is where print,
 * download and copy live.
 */
export function DocumentViewer({ doc, alternatives, matchedPages, compact = false }) {
  const total = doc.pages_total || null;
  const [page, setPage] = useState(doc.page || 1);
  const [zoom, setZoom] = useState(false);
  const [failed, setFailed] = useState(false);

  const v = doc.vehicle || {};
  const vehicle = [v.year, v.make, v.model, v.drivetrain, v.topic].filter(Boolean).join(" · ");
  const canRender = doc.renderable !== false && doc.page_url;
  const src = canRender
    ? `${doc.page_url}&page=${page}&width=${zoom ? 1800 : 1100}`
    : null;

  const go = (n) => {
    setFailed(false);
    setPage(() => Math.max(1, total ? Math.min(total, n) : n));
  };

  return (
    <>
      {vehicle && <p className="card-note field-vehicle">{vehicle}</p>}

      {matchedPages?.length > 1 && (
        <div className="field-actions" style={{ marginBottom: 6 }}>
          {matchedPages.map((n) => (
            <button
              key={n}
              type="button"
              className={n === page ? "is-active" : ""}
              onClick={() => go(n)}
            >
              p.{n}
            </button>
          ))}
        </div>
      )}

      {canRender && !failed ? (
        <div
          className={`page-view${zoom ? " is-zoomed" : ""}`}
          onClick={() => setZoom((z) => !z)}
          title={zoom ? "Tap to fit" : "Tap to zoom"}
        >
          <img
            src={src}
            alt={`${doc.title} page ${page}`}
            loading="lazy"
            onError={() => setFailed(true)}
          />
        </div>
      ) : (
        <p className="card-note">
          {failed
            ? "That page could not be rendered. Open the PDF directly."
            : "Inline page rendering is unavailable for this file."}
        </p>
      )}

      <div className="field-actions page-nav">
        <button onClick={() => go(page - 1)} disabled={page <= 1}>‹</button>
        <span className="field-page">{page}{total ? ` / ${total}` : ""}</span>
        <button onClick={() => go(page + 1)} disabled={!!total && page >= total}>›</button>
        {!compact && total > 1 && (
          <input
            className="page-scrub"
            type="range"
            min={1}
            max={total}
            value={page}
            onChange={(e) => go(Number(e.target.value))}
            aria-label="Page"
          />
        )}
        <a href={doc.url} target="_blank" rel="noreferrer" className="field-link">
          <ExternalLink size={12} /> Open PDF
        </a>
      </div>

      {alternatives?.length > 0 && (
        <details className="field-alts">
          <summary>{alternatives.length} other matching documents</summary>
          {alternatives.map((a) => (
            <a key={a.relative_path} href={a.url} target="_blank" rel="noreferrer">
              {a.title}
            </a>
          ))}
        </details>
      )}
    </>
  );
}

/* ---------------- ADAS SI: search hits ---------------- */

export function AdasResultsCard({ data }) {
  const results = (data?.results || []).filter((r) => r.excerpt);

  if (data?.status === "unavailable" || data?.status === "no_result") {
    return (
      <Card icon={BookOpen} title="ADAS SI" tone="warn">
        <p className="card-note">{data.message || "Nothing matched."}</p>
      </Card>
    );
  }

  // Matched the document but couldn't read it — a scanned PDF, not a miss.
  if (data?.status === "partial_success") {
    const first = data.matched_documents?.[0];
    // No text came out of it, but it still renders -- show the actual pages
    // rather than telling him to go open it somewhere else.
    return (
      <Card icon={AlertTriangle} title="ADAS SI · scanned document" tone="warn">
        <p className="card-note">
          No text could be extracted from this one (it is a scan, and there is no
          OCR) — so here are the pages themselves.
        </p>
        {first?.url && (
          <DocumentViewer
            doc={{
              title: first.title,
              url: first.url,
              page_url: first.url.replace("/document?", "/page?"),
              page: 1,
              pages_total: first.pages_total || null,
              renderable: true,
              vehicle: {
                year: first.year, make: first.make, model: first.model,
                drivetrain: first.drivetrain, topic: first.topic,
              },
            }}
          />
        )}
      </Card>
    );
  }

  // Group passages by document -- the same PDF hitting on several pages is
  // one thing to review, not several unrelated cards.
  const groups = [];
  const bySource = new Map();
  for (const r of results) {
    const key = r.source || r.title;
    let group = bySource.get(key);
    if (!group) {
      group = { key, title: r.title, url: r.url, vehicle: r.vehicle, hits: [] };
      bySource.set(key, group);
      groups.push(group);
    }
    group.hits.push(r);
  }

  return (
    <Card
      icon={BookOpen}
      title="ADAS SI"
      meta={`${groups.length} document${groups.length === 1 ? "" : "s"} · ${results.length} passages`}
    >
      {groups.map((g, i) => {
        const pages = g.hits.map((h) => h.page).sort((a, b) => a - b);
        // The extracted excerpt is often a mangled diagram/table dump --
        // real field reading is the actual page image, not that text.
        const doc = {
          title: g.title,
          url: g.url ? `${g.url}#page=${pages[0]}` : g.url,
          page_url: g.url ? g.url.replace("/document?", "/page?") : null,
          page: pages[0],
          renderable: true,
          vehicle: g.vehicle,
        };
        return (
          <details className="field-hit" key={g.key} open={i === 0}>
            <summary>
              <strong>{g.title}</strong>
              <span className="field-page">
                {pages.length > 1 ? `${pages.length} pages` : `p.${pages[0]}`}
              </span>
            </summary>
            <DocumentViewer doc={doc} matchedPages={pages} compact />
            <details className="field-alts" style={{ marginTop: 8 }}>
              <summary>Extracted text</summary>
              {g.hits.map((h, hi) => (
                <div key={`${h.page}-${hi}`} style={{ marginTop: hi ? 10 : 0 }}>
                  <span className="field-page">p.{h.page}</span>
                  <pre className="pre field-excerpt">{h.excerpt}</pre>
                </div>
              ))}
            </details>
          </details>
        );
      })}
    </Card>
  );
}

/* ---------------- ADAS SI: library coverage ---------------- */

export function AdasInventoryCard({ data }) {
  if (data?.status !== "success") {
    return (
      <Card icon={Layers} title="ADAS SI library" tone="warn">
        <p className="card-note">{data?.message || "Library unavailable."}</p>
      </Card>
    );
  }
  const s = data.summary || {};
  return (
    <Card icon={Layers} title="ADAS SI library" meta={`${s.document_count} documents`}>
      <div className="kv">
        <div><span>Documents</span><strong>{s.document_count}</strong></div>
        <div><span>Vehicles</span><strong>{s.vehicle_application_count}</strong></div>
        <div><span>Unparsed</span><strong>{s.unparsed_document_count}</strong></div>
      </div>
      <details className="field-alts" style={{ marginTop: 9 }}>
        <summary>Vehicle coverage</summary>
        <div className="field-apps">
          {(data.applications || []).map((a) => (
            <div key={`${a.year}-${a.make}-${a.model}`} className="field-row">
              <strong>{a.year} {a.make} {a.model}</strong>
              <span className="field-topics">
                {(a.topics || []).join(", ") || `${a.document_count} doc(s)`}
              </span>
            </div>
          ))}
        </div>
      </details>
    </Card>
  );
}

/* ---------------- Calibration IQ: the board ---------------- */

export function CalibrationRosCard({ data }) {
  if (data?.status !== "verified") {
    return (
      <Card
        icon={data?.status === "invalid_filter" ? AlertTriangle : ClipboardList}
        title={data?.status === "invalid_filter" ? "Calibration IQ · filter rejected" : "Calibration IQ"}
        tone="warn"
      >
        <p className="card-note">{data?.message || "Calibration IQ is unavailable."}</p>
        {data?.filters && (
          <pre className="pre" style={{ marginTop: 8 }}>
            {JSON.stringify(data.filters, null, 2)}
          </pre>
        )}
      </Card>
    );
  }

  const rows = data.rows || [];
  const f = data.filters || {};
  const totalLabel = calibrationCountLabel(data.count, data.collection_capped);
  const applied = Object.entries(f)
    .filter(([k]) => k !== "limit")
    .map(([k, v]) => `${k}: ${v}`)
    .join(" · ");

  return (
    <Card
      icon={ClipboardList}
      title="Calibration IQ"
      meta={
        data.truncated
          ? `${data.shown_count} of ${totalLabel}`
          : `${rows.length} repair order${rows.length === 1 ? "" : "s"}`
      }
    >
      {applied && <p className="card-note field-filters">Filtered — {applied}</p>}

      {data.collection_capped && (
        <p className="card-note ciq-incomplete" role="status">
          {CALIBRATION_COLLECTION_WARNING}
        </p>
      )}

      {rows.length === 0 ? (
        <p className="card-note">No repair orders matched that filter.</p>
      ) : (
        <div className="ro-list">
          {rows.map((r, i) => (
            <div className="ro-item" key={r.id || r.RO || i}>
              <div className="ro-line">
                <span className="ro-num">{r.RO || "—"}</span>
                <span className="ro-vehicle">{r.Vehicle || "—"}</span>
              </div>
              <div className="ro-line ro-sub">
                <span className={`ro-pill ${statusTone(r.Status)}`}>{r.Status || "—"}</span>
                {r.Shop && r.Shop !== "-" && <span className="ro-shop">{r.Shop}</span>}
                {r.Phase != null && <span className="ro-phase">Phase {r.Phase}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {data.truncated && (
        <p className="card-note">
          Showing {data.shown_count} of {totalLabel}. Narrow the filter or ask for a
          specific phase, shop, or status.
        </p>
      )}
    </Card>
  );
}

/** Human label for the filters that produced a result. */
function filterLabel(filters) {
  const parts = Object.entries(filters || {})
    .filter(([k]) => !["limit", "offset"].includes(k))
    .map(([k, v]) => (k === "phase" ? `Phase ${v}` : String(v)));
  return parts.join(" · ");
}

/* ---------------- Calibration IQ: counts, not rows ---------------- */

export function CalibrationSummaryCard({ data }) {
  if (data?.status !== "verified") {
    return (
      <Card icon={ClipboardList} title="Calibration IQ" tone="warn">
        <p className="card-note">{data?.message || "Calibration IQ is unavailable."}</p>
      </Card>
    );
  }

  const scope = filterLabel(data.filters);
  const byStatus = Object.entries(data.breakdown?.by_status || {});
  const byShop = Object.entries(data.breakdown?.by_shop || {});
  const byPhase = Object.entries(data.breakdown?.by_phase || {});
  const noFilterShop = !data.filters?.shop;
  const noFilterPhase = !data.filters?.phase;

  return (
    <Card icon={ClipboardList} title="Calibration IQ" meta={scope || "all shops"}>
      <div className="ciq-count">
        <strong>{data.count}{data.collection_capped ? "+" : ""}</strong>
        <span>{calibrationSummaryScopeLabel({
          terminalOnly: data.terminal_only,
          includeCompleted: data.include_completed,
          collectionCapped: data.collection_capped,
        })}</span>
      </div>

      {data.collection_capped && (
        <p className="card-note ciq-incomplete" role="status">
          {CALIBRATION_COLLECTION_WARNING}
        </p>
      )}

      {byStatus.length > 0 && (
        <div className="ciq-chips">
          {byStatus.map(([label, n]) => (
            <span className={`ro-pill ${statusTone(label)}`} key={label}>
              {n} {label}
            </span>
          ))}
        </div>
      )}

      {(noFilterShop && byShop.length > 1) && (
        <>
          <p className="rail-sub">By shop</p>
          <div className="ciq-chips">
            {byShop.map(([label, n]) => (
              <span className="ro-pill" key={label}>{n} {label}</span>
            ))}
          </div>
        </>
      )}

      {(noFilterPhase && byPhase.length > 1) && (
        <>
          <p className="rail-sub">By phase</p>
          <div className="ciq-chips">
            {byPhase.map(([label, n]) => (
              <span className="ro-pill" key={label}>
                {calibrationPhaseBreakdown(label, n)}
              </span>
            ))}
          </div>
        </>
      )}

      {!data.include_completed && data.completed_count > 0 && (
        <p className="card-note" style={{ marginTop: 9 }}>
          {data.completed_count} finished {data.completed_count === 1 ? "order" : "orders"} excluded.
        </p>
      )}
    </Card>
  );
}

/* ---------------- Calibration IQ: one repair order ---------------- */

function calibrationRoDocuments(data) {
  const raw = data?.raw || {};
  const candidates = [
    ...(Array.isArray(raw?.documents) ? raw.documents : []),
    ...(Array.isArray(raw?.research?.documents) ? raw.research.documents : []),
    ...(Array.isArray(raw?.research_case?.documents) ? raw.research_case.documents : []),
  ];
  const seen = new Set();
  return candidates.filter((doc) => {
    if (!doc || doc.archived_at) return false;
    const key = String(doc.id || doc.document_id || doc.source_uri || doc.source_name || "");
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function ciqDocumentDownloadUrl(doc) {
  const value = String(doc?.download_url || "");
  return value.startsWith("/api/calibration-iq/documents/") ? value : null;
}
export function CalibrationRoCard({ data }) {
  const ro = data?.repair_order;
  if (!ro) {
    return (
      <Card icon={ClipboardList} title="Repair order" tone="warn">
        <p className="card-note">{data?.message || "Repair order not found."}</p>
      </Card>
    );
  }
  const blockers = Array.isArray(ro.blockers) ? ro.blockers : [];
  const reqs = Array.isArray(ro.requirements) ? ro.requirements : [];
  const documents = calibrationRoDocuments(data);
  const humanize = (v) =>
    String(v || "").toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  const label = (x) => {
    if (typeof x === "string") return x;
    if (x?.calibration_type) {
      return x.determination ? `${x.calibration_type} — ${humanize(x.determination)}` : x.calibration_type;
    }
    return x?.name || x?.title || x?.description || x?.reason || "Untitled item";
  };

  return (
    <Card icon={ClipboardList} title={`RO ${ro.RO}`} meta={ro.Shop}>
      <p className="ro-vehicle-hero">{ro.Vehicle}</p>
      <div className="ro-line ro-sub" style={{ marginBottom: 10 }}>
        <span className={`ro-pill ${statusTone(ro.Status)}`}>{ro.Status}</span>
        {ro.Phase != null && <span className="ro-phase">Phase {ro.Phase}</span>}
      </div>

      <div className="kv">
        {ro.insurance && <div><span>Insurance</span><strong>{ro.insurance}</strong></div>}
        {ro.vin && <div><span>VIN</span><strong>{ro.vin}</strong></div>}
        {ro.arrival && <div><span>Arrival</span><strong>{String(ro.arrival).slice(0, 10)}</strong></div>}
        {ro.updated && <div><span>Updated</span><strong>{String(ro.updated).slice(0, 10)}</strong></div>}
        {ro.version != null && <div><span>Version</span><strong>{ro.version}</strong></div>}
      </div>

      {blockers.length > 0 && (
        <>
          <p className="rail-sub" style={{ marginTop: 10 }}>Blockers</p>
          {blockers.map((b, i) => (
            <div className="field-row" key={i}>
              <AlertTriangle size={13} />
              <span>{label(b)}</span>
            </div>
          ))}
        </>
      )}

      {reqs.length > 0 && (
        <>
          <p className="rail-sub" style={{ marginTop: 10 }}>Calibration requirements</p>
          {reqs.map((r, i) => (
            <div className="field-row" key={i}>
              <Wrench size={13} />
              <span>{label(r)}</span>
            </div>
          ))}
        </>
      )}

      {documents.length > 0 && (
        <>
          <p className="rail-sub" style={{ marginTop: 10 }}>Attached documents</p>
          {documents.map((doc, i) => {
            const href = ciqDocumentDownloadUrl(doc);
            const title = doc.title || doc.source_name || ("Document " + (i + 1));
            return (
              <div className="field-row" key={doc.id || doc.document_id || doc.source_uri || i}>
                <FileText size={13} />
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer" className="field-link">
                    {title}
                  </a>
                ) : (
                  <span>{title}</span>
                )}
                {doc.semantic_type && <span className="ro-pill">{humanize(doc.semantic_type)}</span>}
              </div>
            );
          })}
        </>
      )}
      {ro.notes && <p className="card-note" style={{ marginTop: 10 }}>{ro.notes}</p>}
    </Card>
  );
}

/* ---------------- Calibration IQ: write receipt ---------------- */

export function CalibrationReceiptCard({ data }) {
  const actions = Array.isArray(data?.receipts)
    ? data.receipts
    : Array.isArray(data?.actions)
      ? data.actions
      : Array.isArray(data?.results)
        ? data.results
        : [];
  const legacyVerified = data?.receipt?.verified === true;
  const actionVerified = actions.length > 0
    && actions.every((item) => (
      item?.success === true
      && item?.status === "completed"
      && (item?.verification?.verified === true || item?.verified === true || item?.receipt?.verified === true)
    ));
  const verified = data?.verified === true || actionVerified || legacyVerified;
  const partial = data?.partial === true || data?.status === "partial_success" || data?.status === "partial";
  const failed = data?.success === false || data?.status === "failed" || data?.status === "error";
  const tone = verified && !partial && !failed ? undefined : "warn";
  const title = verified && !partial && !failed
    ? "Calibration IQ — changes verified"
    : partial
      ? "Calibration IQ — partially completed"
      : "Calibration IQ — not confirmed";
  const actionLabel = (item) => item?.operation || item?.receipt?.operation || "operation";
  const targetLabel = (item) => (
    item?.resource_id
    || item?.resource?.id
    || item?.repair_order_id
    || item?.target_id
    || item?.receipt?.resource?.id
    || "—"
  );
  const isVerified = (item) => (
    item?.success === true
    && item?.status === "completed"
    && (item?.verification?.verified === true || item?.verified === true || item?.receipt?.verified === true)
  );
  const errorMessage = (item) => item?.error?.message || item?.message;
  return (
    <Card
      icon={Wrench}
      title={title}
      tone={tone}
    >
      {actions.length > 0 ? (
        <div className="ro-list ciq-operation-list">
          {actions.map((item, index) => (
            <div className="ro-item" key={item?.mutation_id || item?.receipt_id || item?.idempotency_key || index}>
              <div className="ro-line">
                <strong>{actionLabel(item)}</strong>
                <span className={`ro-pill ${isVerified(item) ? "done" : "warn"}`}>
                  {isVerified(item)
                    ? "verified"
                    : item?.status === "completed" || item?.executed
                      ? "not verified"
                      : "not executed"}
                </span>
              </div>
              <div className="ro-line ro-sub">
                <span>{targetLabel(item)}</span>
                {(item?.replayed || item?.duplicate) && <span>duplicate absorbed</span>}
              </div>
              {errorMessage(item) && (
                <p className="card-note ciq-operation-error">{errorMessage(item)}</p>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="kv">
          <div><span>Operation</span><strong>{data?.operation}</strong></div>
          <div><span>Repair order</span><strong>{data?.repair_order_id}</strong></div>
          <div><span>Verified</span><strong>{verified ? "yes" : "no"}</strong></div>
          {data?.duplicate && <div><span>Duplicate</span><strong>absorbed</strong></div>}
        </div>
      )}
      {data?.missing_documentation?.length > 0 && (
        <p className="card-note ciq-incomplete" style={{ marginTop: 8 }}>
          Missing documentation: {data.missing_documentation.join(", ")}
        </p>
      )}
      {data?.message && <p className="card-note" style={{ marginTop: 8 }}>{data.message}</p>}
    </Card>
  );
}

/* ---------------- Calibration IQ: service status ---------------- */

export function CalibrationStatusCard({ data }) {
  const ok = data?.status === "available";
  return (
    <Card icon={Wrench} title="Calibration IQ" tone={ok ? undefined : "warn"}>
      <div className="kv">
        <div><span>Service</span><strong>{data?.status}</strong></div>
        <div><span>Token</span><strong>{data?.token_present ? "present" : "missing"}</strong></div>
        {data?.http_status && <div><span>HTTP</span><strong>{data.http_status}</strong></div>}
      </div>
      {data?.message && <p className="card-note" style={{ marginTop: 8 }}>{data.message}</p>}
      {data?.env_keys_present?.length > 0 && (
        <details className="field-alts">
          <summary>Keys found in the project .env ({data.env_keys_present.length})</summary>
          <pre className="pre" style={{ marginTop: 6 }}>
            {data.env_keys_present.join("\n")}
          </pre>
        </details>
      )}
    </Card>
  );
}

/* ---------------- Calibration IQ: weekly readiness / work-prep audit ---------------- */

function coverageTone(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "COVERED") return "done";
  if (normalized === "MISSING") return "warn";
  return "";
}

function coverageLabel(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "COVERED") return "SI covered";
  if (normalized === "MISSING") return "SI missing";
  return "SI unverified";
}

function WorkPrepExceptionRow({ item }) {
  const missing = (item.missing_si || [])
    .map((m) => m?.calibration || m?.label)
    .filter(Boolean);
  return (
    <div className="ro-item" key={item.ro_number}>
      <div className="ro-line">
        <span className="ro-num">{item.ro_number || "—"}</span>
        <span className="ro-vehicle">{item.vehicle || "—"}</span>
      </div>
      <div className="ro-line ro-sub">
        <span className={`ro-pill ${coverageTone(item.coverage_status)}`}>
          {coverageLabel(item.coverage_status)}
        </span>
        {item.status === "adas_map_unverified" && (
          <span className="ro-shop">ADAS Map unverified</span>
        )}
        {item.status === "reconciliation_failed" && (
          <span className="ro-shop">reconciliation failed</span>
        )}
        {item.status === "ro_unavailable" && (
          <span className="ro-shop">RO detail unavailable</span>
        )}
      </div>
      {missing.length > 0 && (
        <p className="card-note" style={{ marginTop: 4 }}>
          Needs: {missing.join(", ")}
        </p>
      )}
    </div>
  );
}

/** phase_list shares calibration_iq_read's exact result shape. */
function WorkPrepPhaseListCard({ data }) {
  return <CalibrationRosCard data={data} />;
}

function WorkPrepQueueNextCard({ data }) {
  const ok = data?.success !== false;
  return (
    <Card icon={ClipboardList} title="Weekly readiness queue" tone={ok ? undefined : "warn"}>
      {typeof data?.done_count === "number" && (
        <div className="kv">
          <div>
            <span>Collected</span>
            <strong>{data.done_count} of {data.total_count}</strong>
          </div>
        </div>
      )}
      <p className="card-note" style={{ marginTop: 8 }}>
        {data?.message || "No update."}
      </p>
    </Card>
  );
}

function WorkPrepQueueItemRow({ item }) {
  const calibrations = item.category === "unverified"
    ? item.unverified_calibrations || []
    : item.missing_calibrations || [];
  return (
    <div className="ro-item" key={item.repair_order_id}>
      <div className="ro-line">
        <span className="ro-num">{item.ro_number || "—"}</span>
        <span className="ro-vehicle">{item.vehicle_label || "—"}</span>
      </div>
      <div className="ro-line ro-sub">
        <span className={`ro-pill ${item.category === "missing" ? "warn" : ""}`}>
          {item.category === "missing" ? "SI missing" : "SI unverified"}
        </span>
      </div>
      {calibrations.length > 0 && (
        <p className="card-note" style={{ marginTop: 4 }}>
          {item.category === "missing" ? "Needs" : "Unverified"}: {calibrations.join(", ")}
        </p>
      )}
    </div>
  );
}

/** Read-only replay of the persisted weekly-readiness SI queue -- no live
 * re-audit, so it always returns instantly with a real card. */
function WorkPrepQueueListCard({ data }) {
  if (["no_active_queue", "queue_stale", "context_missing"].includes(data?.status)) {
    return (
      <Card icon={ClipboardList} title="Weekly readiness queue" tone="warn">
        <p className="card-note">{data?.message || "No active weekly readiness queue."}</p>
      </Card>
    );
  }

  const items = data?.items || [];
  const missingCount = data?.missing_count ?? 0;
  const unverifiedCount = data?.unverified_count ?? 0;

  return (
    <Card
      icon={items.length === 0 ? ClipboardList : AlertTriangle}
      title="Weekly readiness queue"
      meta={`${items.length} pending`}
      tone={items.length === 0 ? undefined : "warn"}
    >
      {items.length === 0 ? (
        <p className="card-note">Every RO that needed SI has been collected.</p>
      ) : (
        <>
          <div className="ciq-chips" style={{ marginBottom: 9 }}>
            {missingCount > 0 && <span className="ro-pill warn">{missingCount} missing</span>}
            {unverifiedCount > 0 && <span className="ro-pill">{unverifiedCount} unverified</span>}
          </div>
          <div className="ro-list">
            {items.map((item, i) => (
              <WorkPrepQueueItemRow item={item} key={item.repair_order_id || i} />
            ))}
          </div>
        </>
      )}
      {typeof data?.done_count === "number" && data.done_count > 0 && (
        <p className="card-note" style={{ marginTop: 9 }}>
          {data.done_count} already collected this week.
        </p>
      )}
    </Card>
  );
}

function WorkPrepRoRequirementsCard({ data }) {
  const reqs = data?.calibration_requirements || [];
  const mapStatus = data?.adas_map?.status;
  return (
    <Card icon={ClipboardList} title={`RO ${data?.ro_number || "—"}`} meta={data?.vehicle}>
      <div className="kv">
        <div><span>ADAS Map</span><strong>{mapStatus || "unknown"}</strong></div>
      </div>
      {reqs.length > 0 ? (
        <>
          <p className="rail-sub" style={{ marginTop: 10 }}>Calibration requirements</p>
          {reqs.map((r, i) => (
            <div className="field-row" key={r.id || i}>
              <Wrench size={13} />
              <span>{r.label}{r.determination ? ` — ${r.determination}` : ""}</span>
            </div>
          ))}
        </>
      ) : (
        <p className="card-note">No saved calibration requirements.</p>
      )}
      {data?.message && <p className="card-note" style={{ marginTop: 8 }}>{data.message}</p>}
    </Card>
  );
}

/** week_readiness and phase_coverage share the same result shape. */
function WorkPrepReadinessCard({ data }) {
  if (data?.success === false || data?.status === "invalid_request" || data?.status === "context_missing") {
    return (
      <Card icon={AlertTriangle} title="Weekly readiness" tone="warn">
        <p className="card-note">{data?.message || "Calibration IQ work-prep is unavailable."}</p>
      </Card>
    );
  }

  const exceptionCount = data?.exception_count ?? 0;
  const queueCount = data?.queue_count ?? 0;
  const rows = data?.repair_orders || [];
  const exceptions = rows.filter((r) => r.ready !== true);
  const phaseScope = (data?.phase_scope || []).join("–");
  const title = data?.mode === "phase_coverage"
    ? `Phase ${data?.filters?.phase || ""} coverage`
    : "Weekly readiness";

  return (
    <Card
      icon={exceptionCount === 0 ? ClipboardList : AlertTriangle}
      title={title}
      meta={phaseScope ? `phase ${phaseScope}` : undefined}
      tone={exceptionCount === 0 ? undefined : "warn"}
    >
      <div className="ciq-count">
        <strong>{exceptionCount}</strong>
        <span>of {queueCount} not yet SI-ready</span>
      </div>

      <div className="kv" style={{ marginTop: 10 }}>
        <div>
          <span>ADAS Map</span>
          <strong>
            {data?.adas_map_verified_count ?? 0} verified · {data?.adas_map_missing_count ?? 0} missing · {data?.adas_map_unverified_count ?? 0} unverified
          </strong>
        </div>
        <div>
          <span>ADAS SI</span>
          <strong>
            {data?.si_covered_count ?? 0} covered · {data?.si_missing_count ?? 0} missing · {data?.si_unverified_count ?? 0} unverified
          </strong>
        </div>
        {data?.ciq_requirements_added_or_reactivated != null && (
          <div>
            <span>CIQ reconciliation</span>
            <strong>{data.ciq_requirements_added_or_reactivated} added/reactivated</strong>
          </div>
        )}
        {data?.alldata_queued_count > 0 && (
          <div><span>ALLDATA</span><strong>{data.alldata_queued_count} vehicle(s) queued</strong></div>
        )}
      </div>

      {exceptions.length > 0 && (
        <details className="field-alts" style={{ marginTop: 9 }} open>
          <summary>{exceptions.length} needing attention</summary>
          <div className="ro-list">
            {exceptions.map((item, i) => (
              <WorkPrepExceptionRow item={item} key={item.ro_number || i} />
            ))}
          </div>
        </details>
      )}

      {data?.repair_orders_truncated && (
        <p className="card-note" style={{ marginTop: 9 }}>
          Showing {data.repair_orders_shown} of {data.repair_orders_total}. Ask for a
          specific RO or phase to narrow it.
        </p>
      )}
    </Card>
  );
}

export function CalibrationWorkPrepCard({ data }) {
  switch (data?.mode) {
    case "phase_list":
      return <WorkPrepPhaseListCard data={data} />;
    case "queue_list":
      return <WorkPrepQueueListCard data={data} />;
    case "queue_next":
      return <WorkPrepQueueNextCard data={data} />;
    case "ro_requirements":
      return <WorkPrepRoRequirementsCard data={data} />;
    default:
      return <WorkPrepReadinessCard data={data} />;
  }
}

/* ---------------- ADAS SI: annotation records ---------------- */

export function AdasRecordsCard({ data }) {
  const records = data?.records || [];
  return (
    <Card icon={FileText} title="ADAS SI notes" meta={`${records.length}`}>
      {records.length === 0 ? (
        <p className="card-note">No annotation records yet.</p>
      ) : (
        records.map((r) => (
          <div className="field-row" key={r.record_id}>
            <FileText size={13} />
            <strong>{r.title || r.record_id}</strong>
            <span className="field-topics">v{r.version}</span>
          </div>
        ))
      )}
    </Card>
  );
}

export function AdasRecordCard({ data }) {
  const rec = data?.record || {};
  return (
    <Card icon={FileText} title={`Saved — ${rec.title || rec.record_id}`}>
      <div className="kv">
        <div><span>Record</span><strong>{rec.record_id}</strong></div>
        <div><span>Version</span><strong>{rec.version}</strong></div>
      </div>
      {data?.receipt?.backup_path && (
        <p className="card-note" style={{ marginTop: 8 }}>
          Previous version backed up.
        </p>
      )}
    </Card>
  );
}

/* ---------------- ScrapeX: bounded ADAS Map acquisition ---------------- */

export function ScrapeXCard({ data }) {
  const {
    objectPayload,
    readinessRows,
    provenance,
    authenticationRequired,
    warning,
    batches,
    completionLabel,
  } = scrapexCardPresentation(data);

  return (
    <Card
      icon={Layers}
      title="ScrapeX · ADAS Map"
      meta={data?.action || "status"}
      tone={warning ? "warn" : undefined}
    >
      <div className="kv">
        <div><span>State</span><strong>{data?.status || "unknown"}</strong></div>
        <div><span>Request executed</span><strong>{data?.executed === true ? "yes" : "no"}</strong></div>
        {data?.work_complete != null && (
          <div><span>{completionLabel}</span><strong>{data.work_complete ? "yes" : "no"}</strong></div>
        )}
        {(objectPayload?.id || objectPayload?.batch_id || objectPayload?.batch?.id) && (
          <div><span>Batch</span><strong>{objectPayload.id || objectPayload.batch_id || objectPayload.batch.id}</strong></div>
        )}
        {readinessRows.map((row) => (
          <div key={row.key}><span>{row.label}</span><strong>{row.value}</strong></div>
        ))}
        {data?.local_report?.verified === true && (
          <div><span>PDF saved</span><strong>yes</strong></div>
        )}
        {data?.ciq_attachment?.attached === true && (
          <div><span>Attached to RO</span><strong>yes</strong></div>
        )}
      </div>

      {data?.chat_document && data?.ciq_attachment?.attached === true && (
        <div style={{ marginTop: 10 }}>
          <DocumentViewer doc={data.chat_document} compact />
        </div>
      )}

      {data?.ciq_attachment?.download_url && (
        <div className="field-actions" style={{ marginTop: 8 }}>
          <a
            href={data.ciq_attachment.download_url}
            target="_blank"
            rel="noreferrer"
            className="field-link"
          >
            <ExternalLink size={12} /> Open attached ADAS Map
          </a>
        </div>
      )}

      {authenticationRequired && (        <p className="card-note ciq-incomplete" style={{ marginTop: 9 }}>
          Interactive ADAS Map sign-in is required in ScrapeX&apos;s managed work browser.
          Credentials never pass through chat or the model.
        </p>
      )}
      {data?.message && !authenticationRequired && (
        <p className="card-note" style={{ marginTop: 9 }}>{data.message}</p>
      )}
      {data?.error?.message && (
        <p className="card-note ciq-incomplete" style={{ marginTop: 9 }}>{data.error.message}</p>
      )}
      {batches.length > 0 && (
        <details className="field-alts" style={{ marginTop: 9 }}>
          <summary>{batches.length} ADAS Map batch{batches.length === 1 ? "" : "es"}</summary>
          <div className="ro-list">
            {batches.slice(0, 25).map((batch, index) => (
              <div className="ro-item" key={batch?.id || index}>
                <div className="ro-line">
                  <strong className="ro-num">{batch?.name || batch?.id || `Batch ${index + 1}`}</strong>
                  <span className="ro-pill">{batch?.state || batch?.status || "unknown"}</span>
                </div>
              </div>
            ))}
          </div>
        </details>
      )}
      {(provenance?.inspection_id || provenance?.source_url || provenance?.checked_at) && (
        <details className="field-alts" style={{ marginTop: 9 }} open>
          <summary>Source provenance</summary>
          <div className="kv" style={{ marginTop: 7 }}>
            {provenance.inspection_id && <div><span>Inspection</span><strong>{provenance.inspection_id}</strong></div>}
            {provenance.checked_at && <div><span>Checked</span><strong>{provenance.checked_at}</strong></div>}
            <div><span>Requirements proven</span><strong>{provenance.requirements_proven ? "yes" : "no"}</strong></div>
          </div>
          {provenance.source_url && <p className="card-note">{provenance.source_url}</p>}
        </details>
      )}
    </Card>
  );
}

/* ---------------- Durable automotive knowledge ---------------- */

function KnowledgeRecord({ record }) {
  const application = record?.application || {};
  const requirement = record?.requirement || {};
  const evidence = Array.isArray(record?.evidence) ? record.evidence : [];
  const years = application.year_start === application.year_end
    ? application.year_start
    : [application.year_start, application.year_end].filter(Boolean).join("–");
  const vehicle = [years, application.manufacturer, application.model, application.trim]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="research-source-row">
      <div className="research-source-line">
        <strong>{vehicle || record?.id || "Automotive knowledge"}</strong>
        <span className={`ro-pill ${record?.lifecycle === "verified" ? "ok" : "warn"}`}>
          {record?.lifecycle || "unknown"}
        </span>
      </div>
      <div className="research-source-detail">
        {record?.system?.name && <span>{record.system.name}</span>}
        {record?.component?.name && <span>{record.component.name}</span>}
        {requirement.calibration_type && <span>{requirement.calibration_type}</span>}
      </div>
      {requirement.text && <p className="card-note research-public-snippet">{requirement.text}</p>}
      {evidence.length > 0 && (
        <details className="research-evidence-group">
          <summary>{evidence.length} provenance source{evidence.length === 1 ? "" : "s"}</summary>
          {evidence.map((item, index) => (
            <div className="research-evidence-item" key={item?.id || index}>
              <div className="research-evidence-title">
                <strong>{item?.source?.source_name || item?.source?.document_id || "Source"}</strong>
                <span className={`ro-pill ${item?.verification_status === "verified" ? "ok" : "warn"}`}>
                  {item?.verification_status || "unverified"}
                </span>
              </div>
              <div className="research-source-detail">
                {item?.page_start != null && <span>page {item.page_start}</span>}
                {item?.section && <span>{item.section}</span>}
                {item?.source?.source_revision && <span>rev. {item.source.source_revision}</span>}
              </div>
              {item?.source?.source_uri && <p className="card-note">{item.source.source_uri}</p>}
            </div>
          ))}
        </details>
      )}
    </div>
  );
}

export function AutomotiveKnowledgeCard({ data }) {
  const records = Array.isArray(data?.records)
    ? data.records
    : data?.record
      ? [data.record]
      : [];
  const warning = data?.status === "no_result" || records.some((record) => record?.lifecycle !== "verified");

  return (
    <Card
      icon={BookOpen}
      title="Automotive knowledge"
      meta={`${records.length} record${records.length === 1 ? "" : "s"}`}
      tone={warning ? "warn" : undefined}
    >
      {records.length === 0 ? (
        <p className="card-note">
          {data?.message || "No matching durable knowledge was found in this source."}
        </p>
      ) : (
        <div className="research-source-ledger">
          {records.slice(0, 25).map((record, index) => (
            <KnowledgeRecord record={record} key={record?.id || index} />
          ))}
        </div>
      )}
      {data?.verification_deferred && (
        <p className="card-note ciq-incomplete" style={{ marginTop: 9 }}>
          Candidate evidence was preserved as unverified; trusted review is still required.
        </p>
      )}
    </Card>
  );
}

export const FIELD_CARDS = {
  adas_si_document: AdasDocumentCard,
  adas_si_results: AdasResultsCard,
  adas_si_inventory: AdasInventoryCard,
  adas_si_records: AdasRecordsCard,
  adas_si_record: AdasRecordCard,
  calibration_iq_ros: CalibrationRosCard,
  calibration_iq_summary: CalibrationSummaryCard,
  calibration_iq_ro: CalibrationRoCard,
  calibration_iq_receipt: CalibrationReceiptCard,
  calibration_iq_status: CalibrationStatusCard,
  calibration_iq_work_prep: CalibrationWorkPrepCard,
  research_provider: ResearchProviderCard,
  scrapex: ScrapeXCard,
  automotive_knowledge: AutomotiveKnowledgeCard,
};
