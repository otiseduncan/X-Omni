import { useEffect, useId, useRef, useState } from "react";
import {
  CalendarDays,
  Camera,
  CheckSquare,
  ChevronDown,
  CloudSun,
  Code2,
  Copy,
  Cpu,
  Download,
  Eye,
  FileText,
  Film,
  FolderOpen,
  Globe2,
  LayoutTemplate,
  Plus,
  Search,
  Sparkles,
  Square,
  Terminal,
} from "lucide-react";

import {
  cameraFailureMessage,
  startCameraPreview,
  stopCameraPreview,
} from "../../lib/cameraCapture.js";
import { receiptState } from "../../lib/conversationTimeline.js";
import { safeExternalUrl } from "../../lib/externalLinks.js";
import {
  copyGeneratedHtml,
  downloadGeneratedHtml,
  persistWebsiteView,
  restoredWebsiteView,
  websiteArtifactIdentity,
} from "../../lib/websiteArtifact.js";
import { verifiedVideoMedia, videoFailureDisclosure } from "../../lib/videoArtifact.js";
import { FIELD_CARDS } from "./FieldCards.jsx";

/* Inline chat cards. Everything X Omni surfaces renders here, inside the
   conversation, rather than in a separate dashboard panel. */

function Card({ icon: Icon, title, className = "", children }) {
  return (
    <div className={`card${className ? ` ${className}` : ""}`}>
      <div className="card-head">
        <Icon size={14} />
        <span>{title}</span>
      </div>
      {children}
    </div>
  );
}

function fmtTime(iso, allDay) {
  if (!iso) return "";
  if (allDay) return "all day";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function fmtDay(iso) {
  try {
    return new Date(`${iso}T12:00:00`).toLocaleDateString([], { weekday: "short" });
  } catch {
    return iso;
  }
}

function displayText(value, fallback = "") {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function WeatherCard({ data }) {
  if (!data?.ok) {
    return (
      <Card icon={CloudSun} title="Weather">
        <p className="card-note">{data?.summary || data?.next_step || "Weather unavailable."}</p>
      </Card>
    );
  }
  const cur = data.current || {};
  const days = (data.forecast || []).slice(0, 7);
  return (
    <Card icon={CloudSun} title={data.status === "cached" ? "Weather · cached" : "Weather"}>
      <div className="wx-now">
        <div className="wx-temp">
          {cur.temperature_f != null ? `${Math.round(cur.temperature_f)}°` : "--"}
        </div>
        <div>
          <div className="wx-sub">{cur.condition || "—"}</div>
          <div className="wx-loc">
            {data.location?.name}
            {cur.feels_like_f != null && ` · feels ${Math.round(cur.feels_like_f)}°`}
            {cur.wind && ` · ${cur.wind}`}
          </div>
        </div>
      </div>
      {days.length > 0 && (
        <div className="wx-days">
          {days.map((d) => (
            <div className="wx-day" key={d.date}>
              <span>{fmtDay(d.date)}</span>
              <strong>{d.high_f != null ? Math.round(d.high_f) : "--"}°</strong>
              <small>{d.rain_chance != null ? `${Math.round(d.rain_chance)}%` : ""}</small>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function CalendarCard({ data }) {
  if (!data?.ok) {
    return (
      <Card icon={CalendarDays} title="Calendar">
        <p className="card-note">{data?.message || "Calendar unavailable."}</p>
      </Card>
    );
  }
  const events = data.events || [];
  return (
    <Card icon={CalendarDays} title={`Calendar · next ${data.days || 7} days`}>
      {events.length === 0 ? (
        <p className="card-note">Nothing scheduled.</p>
      ) : (
        <div className="cal-list">
          {events.slice(0, 12).map((e) => {
            const isToday = String(e.start || "").startsWith(data.today);
            return (
              <div className={`cal-item${isToday ? " today" : ""}`} key={e.id}>
                <div className="cal-when">
                  {isToday ? fmtTime(e.start, e.all_day) : fmtDay(String(e.start).slice(0, 10))}
                </div>
                <div>
                  <div className="cal-title">{e.title}</div>
                  {e.location && <span className="cal-where">{e.location}</span>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function TasksCard({ data }) {
  const tasks = data?.tasks || [];
  return (
    <Card icon={CheckSquare} title="Tasks">
      {tasks.length === 0 ? (
        <p className="card-note">No tasks.</p>
      ) : (
        tasks.slice(0, 15).map((t) => (
          <div className="list-row" key={t.id}>
            <CheckSquare size={13} />
            <span>{t.title}</span>
            {t.due_at && <span className="due">{String(t.due_at).slice(0, 10)}</span>}
          </div>
        ))
      )}
    </Card>
  );
}

function TaskAddedCard({ data }) {
  return (
    <Card icon={Plus} title="Task added">
      <div className="list-row">
        <CheckSquare size={13} />
        <span>{data?.title}</span>
        {data?.due_at && <span className="due">{String(data.due_at).slice(0, 10)}</span>}
      </div>
    </Card>
  );
}

function TaskUpdatedCard({ data }) {
  return (
    <Card icon={CheckSquare} title="Task updated">
      <div className="list-row">
        <CheckSquare size={13} />
        <span>{data?.title || `Task ${data?.id}`}</span>
        <span className="due">{String(data?.status || "").replace(/_/g, " ")}</span>
      </div>
    </Card>
  );
}

function SystemStatusCard({ data }) {
  const gpus = data?.gpus || [];
  return (
    <Card icon={Cpu} title="System">
      <div className="kv">
        <div>
          <span>Worker</span>
          <strong>{data?.active_worker || "none"}</strong>
        </div>
        <div>
          <span>Camera vision</span>
          <strong>{data?.supports_vision ? "yes" : "no"}</strong>
        </div>
        <div>
          <span>Audio</span>
          <strong>{data?.supports_audio ? "yes" : "no"}</strong>
        </div>
        {gpus.map((g) =>
          g.error ? (
            <div key="err">
              <span>GPU</span>
              <strong>{g.error}</strong>
            </div>
          ) : (
            <div key={g.index}>
              <span>GPU{g.index} free</span>
              <strong>
                {g.free_mib} / {g.total_mib} MiB
              </strong>
            </div>
          )
        )}
      </div>
    </Card>
  );
}

function DirectoryCard({ data }) {
  const entries = data?.entries || [];
  return (
    <Card icon={FolderOpen} title={data?.path || "Directory"}>
      {entries.slice(0, 40).map((e) => (
        <div className="list-row" key={e.name}>
          {e.is_dir ? <FolderOpen size={13} /> : <FileText size={13} />}
          <span>{e.name}</span>
          {e.bytes != null && <span className="due">{e.bytes} B</span>}
        </div>
      ))}
      {entries.length > 40 && <p className="card-note">+{entries.length - 40} more</p>}
    </Card>
  );
}

function FileCard({ data }) {
  return (
    <Card icon={FileText} title={data?.path || "File"}>
      <pre className="pre">{data?.content?.slice(0, 4000)}</pre>
      {data?.truncated && <p className="card-note">Truncated.</p>}
    </Card>
  );
}

function FileSearchCard({ data }) {
  const matches = data?.matches || [];
  return (
    <Card icon={Search} title={`File search · ${matches.length} match${matches.length === 1 ? "" : "es"}`}>
      <p className="card-note">
        <strong>{data?.query}</strong> in {data?.path}
      </p>
      <div className="search-results">
        {matches.length === 0 ? (
          <p className="card-note">No literal match found in {data?.scanned_files || 0} scanned files.</p>
        ) : (
          matches.slice(0, 100).map((match, index) => (
            <div className="search-hit" key={`${match.path}:${match.line}:${index}`}>
              <div className="search-hit-path">{match.path}:{match.line}</div>
              <pre>{match.text || "(blank line)"}</pre>
            </div>
          ))
        )}
      </div>
      <p className="card-note">
        {data?.scanned_files || 0} files scanned
        {data?.skipped_protected_paths ? ` · ${data.skipped_protected_paths} protected paths skipped` : ""}
        {data?.truncated ? " · result limit reached" : ""}
      </p>
    </Card>
  );
}

function WebResearchCard({ data }) {
  const rawSources = Array.isArray(data?.sources)
    ? data.sources
    : Array.isArray(data?.results)
      ? data.results
      : [];
  const sources = rawSources
    .filter((source) => source && typeof source === "object")
    .map((source) => ({
      index: displayText(source.index),
      provider: displayText(source.provider, "web"),
      title: displayText(source.title),
      url: displayText(source.url),
      snippet: displayText(source.snippet || source.excerpt),
      published_at: displayText(source.published_at),
    }));
  const providers = Array.isArray(data?.providers)
    ? data.providers
      .filter((provider) => provider && typeof provider === "object")
      .map((provider) => ({
        provider: displayText(provider.provider, "provider"),
        status: displayText(provider.status, "unknown"),
        results: Number.isFinite(Number(provider.results)) ? Number(provider.results) : 0,
      }))
    : [];
  const degradedProviders = providers.filter((provider) => provider.status !== "healthy");
  const sourceRows = sources.map((source, index) => ({
    source,
    index,
    href: safeExternalUrl(source.url),
  }));
  const blockedLinks = sourceRows.filter(({ source, href }) => source.url && !href).length;
  const query = displayText(data?.query);
  const queriedAt = displayText(data?.queried_at, "time unavailable");
  const compactQuery = query.length > 96 ? `${query.slice(0, 93)}…` : query;
  const warning = data?.ok === false || data?.status === "warning" || sources.length === 0 ||
    degradedProviders.length > 0 || blockedLinks > 0;
  const summaryTitle = sources.length === 0
    ? "Web research · warning"
    : `Web research · ${sources.length} source${sources.length === 1 ? "" : "s"}`;
  const warningLabel = sources.length === 0
    ? "No reliable sources returned"
    : [
      degradedProviders.length
        ? `${degradedProviders.length} provider warning${degradedProviders.length === 1 ? "" : "s"}`
        : "",
      blockedLinks ? `${blockedLinks} unsafe link blocked` : "",
    ].filter(Boolean).join(" · ");
  return (
    <details className={`card inline-disclosure research-disclosure${warning ? " is-warning" : ""}`}>
      <summary
        className="disclosure-summary"
        aria-label={`${summaryTitle}. ${warningLabel ? `${warningLabel}. ` : ""}Query: ${query || "unavailable"}.`}
      >
        <Globe2 size={14} aria-hidden="true" />
        <span className="disclosure-copy">
          <strong>{summaryTitle}</strong>
          <small className="disclosure-query">{compactQuery || "Query unavailable"}</small>
          {warningLabel && <small className="disclosure-warning-copy">{warningLabel}</small>}
        </span>
        <ChevronDown className="disclosure-chevron" size={15} aria-hidden="true" />
      </summary>

      <div className="disclosure-body">
        {sources.length === 0 ? (
          <p className="disclosure-warning" role="status">
            No source result was returned. This does not prove that nothing happened.
          </p>
        ) : (
          <div className="research-sources">
            {sourceRows.map(({ source, index, href }) => {
              const label = source.title || source.url || "Untitled source";
              const snippet = source.snippet || source.excerpt;
              return (
                <article className="research-source" key={source.url || `${source.title}:${index}`}>
                  <div className="research-source-index">[{source.index || index + 1}]</div>
                  <div>
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={`${label} (opens in a new tab)`}
                      >
                        {label}
                      </a>
                    ) : (
                      <>
                        <span className="research-source-title">{label}</span>
                        {source.url && <span className="blocked-link">Unsafe or local source link blocked.</span>}
                      </>
                    )}
                    <div className="research-source-meta">
                      {source.provider || "web"}{source.published_at ? ` · ${source.published_at}` : ""}
                    </div>
                    {snippet && <p>{snippet}</p>}
                  </div>
                </article>
              );
            })}
          </div>
        )}

        {providers.length > 0 && (
          <dl className="research-diagnostics" aria-label="Search provider diagnostics">
            {providers.map((provider, index) => (
              <div key={`${provider.provider || "provider"}:${index}`}>
                <dt>{String(provider.provider || "provider").replace(/_/g, " ")}</dt>
                <dd className={provider.status === "healthy" ? "is-healthy" : "is-degraded"}>
                  {provider.status || "unknown"} · {provider.results ?? 0} result{provider.results === 1 ? "" : "s"}
                </dd>
              </div>
            ))}
          </dl>
        )}

        <p className="card-note research-evidence-note">
          External search · {queriedAt} · source excerpts are untrusted evidence
        </p>
      </div>
    </details>
  );
}

function CapabilitiesCard({ data }) {
  const tools = data?.tools || [];
  const unavailable = data?.not_wired || [];
  return (
    <Card icon={Sparkles} title="Capabilities · live catalog">
      <p className="card-note">
        Active worker: <strong>{data?.active_worker || "none"}</strong>. Catalog presence is not execution proof.
      </p>
      <div className="capability-list">
        {tools.map((tool) => (
          <div className="capability-row" key={tool.name}>
            <span>{tool.name.replace(/_/g, " ")}</span>
            <strong>{tool.status === "approval_required" ? "approval" : "ready"}</strong>
          </div>
        ))}
      </div>
      {unavailable.length > 0 && (
        <details className="capability-limits">
          <summary>Known limits</summary>
          {unavailable.map((item) => (
            <p key={item.name}><strong>{item.name}:</strong> {item.reason}</p>
          ))}
        </details>
      )}
    </Card>
  );
}

function FileWrittenCard({ data }) {
  return (
    <Card icon={FileText} title="File written">
      <div className="kv">
        <div>
          <span>Path</span>
          <strong>{data?.path}</strong>
        </div>
        <div>
          <span>Bytes</span>
          <strong>{data?.bytes}</strong>
        </div>
        <div>
          <span>Overwrote</span>
          <strong>{data?.overwrote_existing ? "yes" : "no"}</strong>
        </div>
      </div>
    </Card>
  );
}

function shellReceiptMatches(data, receipt) {
  if (
    receiptState(receipt) !== "succeeded" ||
    receipt?.tool_name !== "run_powershell" ||
    !receipt.result ||
    typeof receipt.result !== "object"
  ) {
    return false;
  }
  return ["command", "exit_code", "timed_out", "stdout_bytes", "stderr_bytes"].every(
    (key) => (receipt.result[key] ?? null) === (data?.[key] ?? null)
  );
}

function ShellDetail({ data }) {
  const output = data?.stdout || data?.preview || "(no output)";
  return (
    <>
      {data?.command && (
        <p className="shell-command">
          <span>Command</span>
          <code>{data.command}</code>
        </p>
      )}
      {(data?.stdout_truncated || data?.stderr_truncated || data?.truncated) && (
        <p className="shell-truncation" role="status">
          Displayed output is only the captured tail.
          {data?.stdout_truncated ? ` Stdout total: ${data.stdout_bytes ?? "unknown"} bytes.` : ""}
          {data?.stderr_truncated ? ` Stderr total: ${data.stderr_bytes ?? "unknown"} bytes.` : ""}
          {data?.truncated && data?.original_bytes ? ` Result total: ${data.original_bytes} bytes.` : ""}
        </p>
      )}
      <pre className="pre shell-output">
        {output}
        {data?.stderr ? `\n--- stderr ---\n${data.stderr}` : ""}
      </pre>
    </>
  );
}

function ShellCard({ data, receipt }) {
  const exitCode = Number.isInteger(data?.exit_code) ? data.exit_code : null;
  const timedOut = data?.timed_out === true;
  const truncated = Boolean(data?.stdout_truncated || data?.stderr_truncated || data?.truncated);
  const verifiedSuccess = !timedOut && exitCode === 0 && shellReceiptMatches(data, receipt);

  if (verifiedSuccess && !truncated) {
    return (
      <details className="card inline-disclosure shell-disclosure is-success">
        <summary
          className="disclosure-summary"
          aria-label="PowerShell execution details."
        >
          <Terminal size={14} aria-hidden="true" />
          <span className="disclosure-copy">
            <strong>PowerShell details</strong>
          </span>
          <ChevronDown className="disclosure-chevron" size={15} aria-hidden="true" />
        </summary>
        <div className="disclosure-body">
          <p className="shell-state">Verified execution · exit code {exitCode}</p>
          <ShellDetail data={data} />
        </div>
      </details>
    );
  }

  let state = "indeterminate";
  let title = "PowerShell outcome indeterminate";
  let message = "No matching successful execution receipt is attached to this result.";
  if (timedOut) {
    title = "PowerShell timed out · outcome indeterminate";
    message = "The process was stopped after the timeout. Do not assume the requested command completed.";
  } else if (exitCode != null && exitCode !== 0) {
    state = "failed";
    title = `PowerShell failed · exit ${exitCode}`;
    message = `The command returned a nonzero exit code (${exitCode}).`;
  } else if (truncated && exitCode === 0 && shellReceiptMatches(data, receipt)) {
    state = "warning";
    title = "PowerShell completed · output truncated";
    message = "Execution is receipt-verified, but only the tail of the bounded output is available.";
  }

  return (
    <Card icon={Terminal} title={title} className={`shell-result shell-${state}`}>
      <p className="shell-state" role={state === "failed" || state === "indeterminate" ? "alert" : "status"}>
        {message}
      </p>
      <ShellDetail data={data} />
    </Card>
  );
}

function CalendarEventCreatedCard({ data }) {
  const e = data?.event || {};
  return (
    <Card icon={CalendarDays} title="Event created">
      <div className="cal-item">
        <div className="cal-when">{fmtTime(e.start, e.all_day)}</div>
        <div>
          <div className="cal-title">{e.title}</div>
          {e.location && <span className="cal-where">{e.location}</span>}
        </div>
      </div>
    </Card>
  );
}

const CAMERA_STAGE_COPY = {
  requesting_permission: "Waiting for browser camera permission…",
  capturing: "Capturing the current frame…",
  analyzing: "Analyzing the captured frame…",
};

const EXTERIOR_CAMERA_DEFAULTS = {
  label: "Exterior camera",
  host: "192.168.1.10",
  username: "admin",
};

const EXTERIOR_CAMERA_STAGE_COPY = {
  checking: "Checking the exterior camera setup…",
  saving: "Saving the exterior camera setup…",
  starting: "Signing in to the exterior camera and starting its live feed…",
  analyzing: "Asking Core to analyze the current proxied exterior camera frame…",
  disconnecting: "Disconnecting from the exterior camera…",
};

function exteriorCameraText(value, fallback, limit = 200) {
  const text = displayText(value, fallback).trim();
  if (!text) return fallback;
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

function CameraRequestCard({ data, onCameraCapture }) {
  const [stage, setStage] = useState("idle");
  const [error, setError] = useState("");
  const [live, setLive] = useState(false);
  const [analyzed, setAnalyzed] = useState(false);
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const cameraSessionRef = useRef(0);
  const prompt = displayText(data?.prompt, "Describe what is visible in this camera frame.");
  const busy = stage === "capturing" || stage === "analyzing";
  const starting = stage === "requesting_permission";
  const previewVisible = live || starting;

  useEffect(() => () => {
    cameraSessionRef.current += 1;
    const stream = streamRef.current;
    streamRef.current = null;
    stopCameraPreview({ video: videoRef.current, stream });
  }, []);

  async function startLiveCamera() {
    if (live || starting || busy) return;
    const session = cameraSessionRef.current + 1;
    cameraSessionRef.current = session;
    setError("");
    setAnalyzed(false);
    setStage("requesting_permission");

    let stream = null;
    try {
      stream = await startCameraPreview({
        video: videoRef.current,
        onStage: (nextStage) => {
          if (cameraSessionRef.current === session) setStage(nextStage);
        },
        onStream: (acquiredStream) => {
          if (cameraSessionRef.current !== session) return false;
          streamRef.current = acquiredStream;
          return true;
        },
      });
      if (cameraSessionRef.current !== session || !videoRef.current) {
        stopCameraPreview({ video: videoRef.current, stream });
        return;
      }
      streamRef.current = stream;
      for (const track of stream.getVideoTracks?.() || []) {
        track.addEventListener?.("ended", () => {
          if (cameraSessionRef.current !== session || streamRef.current !== stream) return;
          streamRef.current = null;
          stopCameraPreview({ video: videoRef.current, stream });
          setLive(false);
          setStage("stopped");
        }, { once: true });
      }
      setLive(true);
      setStage("live");
    } catch (cameraError) {
      if (cameraSessionRef.current !== session) return;
      setLive(false);
      setStage("error");
      setError(cameraFailureMessage(cameraError));
    }
  }

  function stopLiveCamera() {
    cameraSessionRef.current += 1;
    const stream = streamRef.current;
    streamRef.current = null;
    stopCameraPreview({ video: videoRef.current, stream });
    setLive(false);
    setStage("stopped");
    setError("");
  }

  async function analyzeCurrentFrame() {
    if (busy || !live || !streamRef.current) return;
    setError("");
    if (typeof onCameraCapture !== "function") {
      setError("Camera capture is not available in this chat session.");
      return;
    }
    try {
      await onCameraCapture(
        { ...data, prompt },
        setStage,
        { video: videoRef.current }
      );
      setAnalyzed(true);
      setStage(streamRef.current ? "live" : "stopped");
    } catch (captureError) {
      setError(displayText(captureError?.message, "Camera capture failed before a frame was described."));
      setStage(streamRef.current ? "live" : "error");
    }
  }

  return (
    <Card icon={Camera} title="Camera requested" className="camera-request">
      <p className="camera-prompt">{prompt}</p>
      <div className="camera-preview" hidden={!previewVisible}>
        <video
          ref={(node) => {
            // Retain the detached node through effect cleanup so unmount can
            // clear srcObject as well as stop every MediaStream track.
            if (node) videoRef.current = node;
          }}
          className="camera-live-video"
          muted
          playsInline
          autoPlay
          aria-label="Live camera preview"
        />
        {live && <span className="camera-live-badge" aria-hidden="true">Live</span>}
      </div>
      <div className="camera-controls" role="group" aria-label="Live camera controls">
        {!live && (
          <button
            type="button"
            className="camera-action"
            onClick={startLiveCamera}
            disabled={starting || busy}
            aria-label="Start live camera"
          >
            <Camera size={16} aria-hidden="true" />
            {starting ? "Starting camera…" : "Start live camera"}
          </button>
        )}
        {previewVisible && (
          <>
            <button
              type="button"
              className="camera-action"
              onClick={analyzeCurrentFrame}
              disabled={!live || busy}
              aria-label="Analyze current camera frame"
            >
              <Camera size={16} aria-hidden="true" />
              {busy ? "Analyzing frame…" : "Analyze current frame"}
            </button>
            <button
              type="button"
              className="camera-action is-secondary"
              onClick={stopLiveCamera}
              aria-label="Stop live camera"
            >
              <Square size={15} aria-hidden="true" />
              Stop camera
            </button>
          </>
        )}
      </div>
      {(stage === "idle" || stage === "stopped") && (
        <p className="card-note camera-state" role="status">
          Camera is off. Nothing is captured or sent until you start it and choose Analyze current frame.
        </p>
      )}
      {CAMERA_STAGE_COPY[stage] && (
        <p className="card-note camera-state" role="status">{CAMERA_STAGE_COPY[stage]}</p>
      )}
      {stage === "live" && (
        <p className={`card-note camera-state${analyzed ? " is-complete" : ""}`} role="status">
          {analyzed
            ? "Frame analyzed and added to this chat. Live camera remains on."
            : "Live camera is on. No frame is sent until you choose Analyze current frame."}
        </p>
      )}
      {error && (
        <p className="camera-error" role="alert">{error}</p>
      )}
    </Card>
  );
}

function ExteriorCameraRequestCard({
  data,
  onCameraCapture,
  onExteriorCameraStatus,
  onExteriorCameraConfigure,
  onExteriorCameraStart,
  onExteriorCameraStop,
}) {
  const labelId = useId();
  const hostId = useId();
  const usernameId = useId();
  const passwordId = useId();
  const [configurationKnown, setConfigurationKnown] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [label, setLabel] = useState(EXTERIOR_CAMERA_DEFAULTS.label);
  const [host, setHost] = useState(EXTERIOR_CAMERA_DEFAULTS.host);
  const [username, setUsername] = useState(EXTERIOR_CAMERA_DEFAULTS.username);
  const [stage, setStage] = useState("checking");
  const [error, setError] = useState("");
  const [session, setSession] = useState(null);
  const [frameReady, setFrameReady] = useState(false);
  const [streamFailed, setStreamFailed] = useState(false);
  const [analyzed, setAnalyzed] = useState(false);
  const passwordRef = useRef(null);
  const imageRef = useRef(null);
  const sessionRef = useRef(null);
  const stopCallbackRef = useRef(onExteriorCameraStop);
  const operationRef = useRef(0);
  const mountedRef = useRef(true);
  const prompt = exteriorCameraText(
    data?.prompt,
    "Describe what is visible in this exterior camera frame.",
    1_000
  );
  const busy = ["checking", "saving", "starting", "capturing", "analyzing", "disconnecting"]
    .includes(stage);
  const live = Boolean(session?.session_id && session?.stream_url);

  stopCallbackRef.current = onExteriorCameraStop;

  useEffect(() => {
    let active = true;
    mountedRef.current = true;
    setStage("checking");
    setError("");

    Promise.resolve()
      .then(() => {
        if (typeof onExteriorCameraStatus !== "function") {
          throw new Error("Exterior camera setup is not available in this chat session.");
        }
        return onExteriorCameraStatus();
      })
      .then((payload) => {
        if (!active) return;
        const details = payload?.camera || payload?.configuration || payload || {};
        const isConfigured = payload?.configured === true
          || details?.configured === true
          || ["configured", "ready"].includes(String(payload?.status || details?.status || "").toLowerCase());
        setLabel(exteriorCameraText(details?.label, EXTERIOR_CAMERA_DEFAULTS.label, 80));
        setHost(exteriorCameraText(details?.host, EXTERIOR_CAMERA_DEFAULTS.host, 255));
        setUsername(exteriorCameraText(details?.username, EXTERIOR_CAMERA_DEFAULTS.username, 160));
        setConfigured(isConfigured);
        setConfigurationKnown(true);
        setStage("ready");
      })
      .catch((statusError) => {
        if (!active) return;
        setConfigurationKnown(true);
        setConfigured(false);
        setStage("error");
        setError(exteriorCameraText(
          statusError?.message,
          "X Omni could not check the exterior camera setup."
        ));
      });

    return () => {
      active = false;
      mountedRef.current = false;
      operationRef.current += 1;
      const activeSession = sessionRef.current;
      sessionRef.current = null;
      imageRef.current?.removeAttribute?.("src");
      if (activeSession?.session_id && typeof stopCallbackRef.current === "function") {
        Promise.resolve(
          stopCallbackRef.current(activeSession.session_id, { keepalive: true })
        ).catch(() => {});
      }
    };
  }, [onExteriorCameraStatus]);

  async function saveExteriorCamera(event) {
    event.preventDefault();
    if (stage === "saving") return;
    if (typeof onExteriorCameraConfigure !== "function") {
      setError("Exterior camera setup is not available in this chat session.");
      return;
    }

    let credential = passwordRef.current?.value || "";
    if (passwordRef.current) passwordRef.current.value = "";
    if (!credential) {
      setError("Enter the exterior camera password to finish setup.");
      passwordRef.current?.focus?.();
      return;
    }

    const operation = operationRef.current + 1;
    operationRef.current = operation;
    setError("");
    setStage("saving");
    try {
      const pending = onExteriorCameraConfigure({
        label: label.trim(),
        host: host.trim(),
        username: username.trim(),
        password: credential,
      });
      // The credential has already left the DOM and is cleared from the local
      // variable as soon as the callback has synchronously built its request.
      credential = "";
      const payload = await pending;
      if (!mountedRef.current || operationRef.current !== operation) return;
      const details = payload?.camera || payload?.configuration || payload || {};
      setLabel(exteriorCameraText(details?.label, label.trim(), 80));
      setHost(exteriorCameraText(details?.host, host.trim(), 255));
      setUsername(exteriorCameraText(details?.username, username.trim(), 160));
      setConfigured(true);
      setConfigurationKnown(true);
      setStage("ready");
    } catch (setupError) {
      if (!mountedRef.current || operationRef.current !== operation) return;
      setStage("error");
      setError(exteriorCameraText(
        setupError?.message,
        "X Omni could not save the exterior camera setup."
      ));
    } finally {
      credential = "";
      if (passwordRef.current) passwordRef.current.value = "";
    }
  }

  async function startExteriorCamera() {
    if (busy || live) return;
    if (typeof onExteriorCameraStart !== "function") {
      setError("Exterior camera streaming is not available in this chat session.");
      return;
    }
    const operation = operationRef.current + 1;
    operationRef.current = operation;
    setError("");
    setFrameReady(false);
    setStreamFailed(false);
    setAnalyzed(false);
    setStage("starting");
    try {
      const nextSession = await onExteriorCameraStart();
      if (!mountedRef.current || operationRef.current !== operation) {
        if (nextSession?.session_id && typeof stopCallbackRef.current === "function") {
          Promise.resolve(
            stopCallbackRef.current(nextSession.session_id, { keepalive: true })
          ).catch(() => {});
        }
        return;
      }
      sessionRef.current = nextSession;
      setSession(nextSession);
      setLabel(exteriorCameraText(nextSession?.label, label, 80));
      setStage("live");
    } catch (startError) {
      if (!mountedRef.current || operationRef.current !== operation) return;
      setStage("error");
      setError(exteriorCameraText(
        startError?.message,
        "X Omni could not start the exterior camera feed."
      ));
    }
  }

  async function analyzeExteriorFrame() {
    if (busy || !live || streamFailed || !frameReady) return;
    if (typeof onCameraCapture !== "function") {
      setError("Camera analysis is not available in this chat session.");
      return;
    }
    const operation = operationRef.current + 1;
    operationRef.current = operation;
    setError("");
    try {
      await onCameraCapture(
        { ...data, prompt },
        (nextStage) => {
          if (mountedRef.current && operationRef.current === operation) setStage(nextStage);
        },
        {
          cameraSourceId: "exterior",
          cameraSessionId: sessionRef.current?.session_id,
        }
      );
      if (!mountedRef.current || operationRef.current !== operation) return;
      setAnalyzed(true);
      setStage(sessionRef.current ? "live" : "ready");
    } catch (captureError) {
      if (!mountedRef.current || operationRef.current !== operation) return;
      setError(exteriorCameraText(
        captureError?.message,
        "Exterior camera capture failed before a frame was described."
      ));
      setStage(sessionRef.current ? "live" : "error");
    }
  }

  async function disconnectExteriorCamera() {
    if (stage === "disconnecting") return;
    const activeSession = sessionRef.current;
    operationRef.current += 1;
    sessionRef.current = null;
    imageRef.current?.removeAttribute?.("src");
    setSession(null);
    setFrameReady(false);
    setStreamFailed(false);
    setAnalyzed(false);
    setError("");
    if (!activeSession?.session_id) {
      setStage("ready");
      return;
    }
    setStage("disconnecting");
    try {
      if (typeof onExteriorCameraStop !== "function") {
        throw new Error("Exterior camera disconnect is not available in this chat session.");
      }
      await onExteriorCameraStop(activeSession.session_id);
      if (mountedRef.current) setStage("ready");
    } catch (stopError) {
      if (!mountedRef.current) return;
      setStage("error");
      setError(exteriorCameraText(
        stopError?.message,
        "The live feed was closed here, but Core could not confirm camera logout."
      ));
    }
  }

  return (
    <Card icon={Camera} title="Exterior camera" className="camera-request exterior-camera-request">
      {!configurationKnown && (
        <p className="card-note camera-state" role="status" aria-live="polite">
          {EXTERIOR_CAMERA_STAGE_COPY.checking}
        </p>
      )}

      {configurationKnown && !configured && (
        <form className="exterior-camera-setup" onSubmit={saveExteriorCamera} aria-label="Exterior camera setup">
          <p className="camera-prompt">
            Connect the exterior camera once. Its password is sent directly to Core and is never kept in chat or browser storage.
          </p>
          <div className="exterior-camera-fields">
            <label className="exterior-camera-field" htmlFor={labelId}>
              <span>Camera label</span>
              <input
                id={labelId}
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                required
                maxLength={80}
                autoComplete="off"
              />
            </label>
            <label className="exterior-camera-field" htmlFor={hostId}>
              <span>Camera address</span>
              <input
                id={hostId}
                value={host}
                onChange={(event) => setHost(event.target.value)}
                required
                maxLength={255}
                inputMode="url"
                autoCapitalize="none"
                spellCheck="false"
                autoComplete="off"
              />
            </label>
            <label className="exterior-camera-field" htmlFor={usernameId}>
              <span>Username</span>
              <input
                id={usernameId}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
                maxLength={160}
                autoCapitalize="none"
                spellCheck="false"
                autoComplete="username"
              />
            </label>
            <label className="exterior-camera-field" htmlFor={passwordId}>
              <span>Password</span>
              <input
                id={passwordId}
                ref={passwordRef}
                type="password"
                required
                autoComplete="off"
                data-1p-ignore="true"
                data-lpignore="true"
              />
            </label>
          </div>
          <button
            type="submit"
            className="camera-action"
            disabled={stage === "saving"}
            aria-label="Save exterior camera setup"
          >
            <Camera size={16} aria-hidden="true" />
            {stage === "saving" ? "Saving camera…" : "Save camera setup"}
          </button>
          <p className="card-note exterior-camera-private-note">
            X Omni does not place the password, raw frames, or base64 image data in conversation history.
          </p>
        </form>
      )}

      {configured && (
        <>
          <p className="camera-prompt">
            {label} is configured at <span className="exterior-camera-host">{host}</span>. The feed connects only when you start it.
          </p>
          {live && (
            <div className="camera-preview exterior-camera-preview">
              {!streamFailed ? (
                <img
                  ref={imageRef}
                  className="exterior-camera-live-image"
                  src={session.stream_url}
                  alt={`${label} live feed`}
                  onLoad={() => {
                    if (!sessionRef.current) return;
                    setFrameReady(true);
                    setStreamFailed(false);
                    setStage((current) => ["capturing", "analyzing", "disconnecting"].includes(current)
                      ? current
                      : "live");
                  }}
                  onError={() => {
                    if (!sessionRef.current) return;
                    setFrameReady(false);
                    setStreamFailed(true);
                    setStage("error");
                    setError("The exterior camera feed could not load. Disconnect, then try again.");
                  }}
                />
              ) : (
                <div className="exterior-camera-stream-error" role="img" aria-label="Exterior camera feed unavailable">
                  Live feed unavailable
                </div>
              )}
              {frameReady && !streamFailed && (
                <span className="camera-live-badge" aria-hidden="true">Live</span>
              )}
            </div>
          )}
          <div className="camera-controls" role="group" aria-label="Exterior camera controls">
            {!live && (
              <button
                type="button"
                className="camera-action"
                onClick={startExteriorCamera}
                disabled={busy}
                aria-label="Start exterior camera live feed"
              >
                <Camera size={16} aria-hidden="true" />
                {stage === "starting" ? "Starting live feed…" : "Start live feed"}
              </button>
            )}
            {live && (
              <>
                <button
                  type="button"
                  className="camera-action"
                  onClick={analyzeExteriorFrame}
                  disabled={busy || streamFailed || !frameReady}
                  aria-label="Analyze current exterior camera frame"
                >
                  <Camera size={16} aria-hidden="true" />
                  {stage === "analyzing"
                    ? "Analyzing frame…"
                    : "Analyze current frame"}
                </button>
                <button
                  type="button"
                  className="camera-action is-secondary"
                  onClick={disconnectExteriorCamera}
                  disabled={stage === "disconnecting"}
                  aria-label="Disconnect and log out of exterior camera"
                >
                  <Square size={15} aria-hidden="true" />
                  {stage === "disconnecting" ? "Disconnecting…" : "Disconnect / log out"}
                </button>
              </>
            )}
          </div>
          {!live && stage === "ready" && (
            <p className="card-note camera-state" role="status">
              Camera is configured and offline. Start the live feed when you want to view it.
            </p>
          )}
          {live && stage === "live" && (
            <p className={`card-note camera-state${analyzed ? " is-complete" : ""}`} role="status" aria-live="polite">
              {analyzed
                ? "Frame analyzed and added to this chat. The exterior feed remains live."
                : frameReady
                  ? "Live exterior feed is visible. No frame is analyzed until you choose Analyze current frame."
                  : "Live exterior feed is connecting. Analyze becomes available after a frame is visible, then asks Core to inspect one current proxied frame."}
            </p>
          )}
        </>
      )}

      {EXTERIOR_CAMERA_STAGE_COPY[stage] && stage !== "checking" && (
        <p className="card-note camera-state" role="status" aria-live="polite">
          {EXTERIOR_CAMERA_STAGE_COPY[stage]}
        </p>
      )}
      {error && <p className="camera-error" role="alert">{error}</p>}
    </Card>
  );
}

function CameraObservationCard({ data }) {
  const description = displayText(
    data?.description,
    "The vision model returned no camera description."
  );
  const compactDescription = description.length > 240
    ? `${description.slice(0, 239)}…`
    : description;
  const dimensions = data?.width && data?.height ? `${data.width} × ${data.height}` : "";
  const provenance = [
    ["Prompt", displayText(data?.prompt)],
    ["Source", displayText(data?.source).replace(/_/g, " ")],
    ["Camera", displayText(data?.camera_label)],
    ["Camera source", displayText(data?.camera_source_id)],
    ["Transport", displayText(data?.capture_transport)],
    ["Dimensions", dimensions],
    ["Media", displayText(data?.media_type)],
    ["Bytes", Number.isFinite(Number(data?.bytes)) ? Number(data.bytes).toLocaleString() : ""],
    ["Model", displayText(data?.model)],
    ["Worker", displayText(data?.worker)],
    ["Analyzed", displayText(data?.analyzed_at)],
    ["Frame SHA-256", displayText(data?.sha256)],
  ].filter(([, value]) => value !== "");

  return (
    <details className={`card inline-disclosure camera-observation${data?.ok === false ? " is-warning" : ""}`}>
      <summary
        className="disclosure-summary"
        aria-label={`Camera observation. ${compactDescription}`}
      >
        <Camera size={14} aria-hidden="true" />
        <span className="disclosure-copy">
          <strong>Camera observation</strong>
          <small className="camera-answer">{compactDescription}</small>
        </span>
        <ChevronDown className="disclosure-chevron" size={15} aria-hidden="true" />
      </summary>
      <div className="disclosure-body camera-observation-body">
        {description !== compactDescription && <p className="camera-description">{description}</p>}
        {provenance.length > 0 && (
          <dl className="camera-provenance" aria-label="Camera observation details">
            {provenance.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>
    </details>
  );
}

function imageReceiptMatches(data, receipt) {
  const digest = displayText(data?.sha256);
  const imageUrl = displayText(data?.image_url);
  const result = receipt?.result || {};
  return (
    receiptState(receipt) === "succeeded" &&
    receipt?.tool_name === "image_generate" &&
    /^[0-9a-f]{64}$/.test(digest) &&
    imageUrl === `/api/generated-images/${digest}.png` &&
    data?.target === imageUrl &&
    data?.status === "completed" &&
    data?.verified === true &&
    data?.actual_generation === true &&
    result?.sha256 === digest &&
    result?.image_url === imageUrl &&
    result?.target === imageUrl &&
    result?.verified === true &&
    result?.lifecycle?.model_restored === true
  );
}

function GeneratedImageCard({ data, receipt }) {
  const [imageLoadFailed, setImageLoadFailed] = useState(false);
  useEffect(() => setImageLoadFailed(false), [data?.image_url]);

  if (!imageReceiptMatches(data, receipt)) {
    return (
      <Card icon={Sparkles} title="Generated image unverified" className="image-generation-warning">
        <p className="card-note" role="alert">
          No matching successful execution receipt and verified local file are attached. The image is not displayed.
        </p>
      </Card>
    );
  }
  const prompt = displayText(data?.prompt, "Locally generated image");
  return (
    <figure className={`card generated-image-card${imageLoadFailed ? " image-generation-warning" : ""}`}>
      <div className="card-head">
        <Sparkles size={14} aria-hidden="true" />
        <span>{imageLoadFailed ? "Generated image unavailable" : "Generated locally"}</span>
      </div>
      {imageLoadFailed ? (
        <div className="generated-image-load-error" role="alert">
          <strong>The verified image could not be displayed.</strong>
          <span>The browser failed to load the same-origin image file. No successful display is being claimed.</span>
        </div>
      ) : (
        <img
          className="generated-image"
          src={data.image_url}
          alt={prompt.slice(0, 240)}
          width={data.width}
          height={data.height}
          loading="eager"
          referrerPolicy="no-referrer"
          onError={() => setImageLoadFailed(true)}
        />
      )}
      <figcaption>
        <p className="generated-image-prompt">{prompt}</p>
        <p className="card-note">
          {data.width} × {data.height} · seed {data.seed} · {displayText(data.provider, "local provider")}
        </p>
        <a className="generated-image-download" href={data.image_url} download>
          Download verified PNG
        </a>
      </figcaption>
    </figure>
  );
}

function cameraEventTime(capturedAt) {
  if (!capturedAt) return "";
  // SQLite datetime('now') is UTC with a space separator; force UTC parsing.
  return fmtTime(`${String(capturedAt).replace(" ", "T")}Z`);
}

function CameraEventHistoryCard({ data }) {
  const items = Array.isArray(data?.items) ? data.items : [];
  if (!data?.ok || !items.length) {
    return (
      <Card icon={Camera} title="Camera history">
        <p className="card-note">No stored camera snapshots in that range.</p>
      </Card>
    );
  }
  return (
    <Card icon={Camera} title={`Camera history — ${data.shown_count} of ${data.total_count}`}>
      <div className="camera-history-grid">
        {items.map((item) => (
          <figure className="camera-history-item" key={item.id}>
            {item.snapshot_url && (
              <img
                className="camera-history-thumb"
                src={item.snapshot_url}
                alt={displayText(item.caption, `${item.trigger} snapshot`)}
                loading="lazy"
                referrerPolicy="no-referrer"
              />
            )}
            <figcaption>
              <span className="camera-history-time">{cameraEventTime(item.captured_at)}</span>
              <span className={`camera-history-trigger is-${item.trigger}`}>{item.trigger}</span>
              {item.caption && <p className="card-note">{item.caption}</p>}
            </figcaption>
          </figure>
        ))}
      </div>
      {data.truncated && (
        <p className="card-note">
          Showing the {data.shown_count} most recent of {data.total_count} total.
        </p>
      )}
    </Card>
  );
}

function CameraSnapshotCard({ data }) {
  if (!data?.ok) {
    return (
      <Card icon={Camera} title="Camera snapshot" className="image-generation-warning">
        <p className="card-note" role="alert">
          {displayText(data?.error, "This snapshot could not be analyzed.")}
        </p>
      </Card>
    );
  }
  const badges = [
    data.person_detected ? "person" : null,
    data.vehicle_detected ? "vehicle" : null,
    data.cached ? "cached" : null,
  ].filter(Boolean);
  return (
    <figure className="card camera-snapshot-card">
      <div className="card-head">
        <Camera size={14} aria-hidden="true" />
        <span>{data.trigger === "motion" ? "Motion event" : "Camera snapshot"}</span>
      </div>
      {data.snapshot_url && (
        <img
          className="camera-snapshot-image"
          src={data.snapshot_url}
          alt={displayText(data.caption, "Camera snapshot")}
          loading="eager"
          referrerPolicy="no-referrer"
        />
      )}
      <figcaption>
        <p className="camera-snapshot-caption">
          {displayText(data.caption, "No description available.")}
        </p>
        <p className="card-note">
          {cameraEventTime(data.captured_at)}
          {badges.length ? ` · ${badges.join(" · ")}` : ""}
        </p>
      </figcaption>
    </figure>
  );
}

function CameraMotionClipCard({ data }) {
  if (!data?.ok) {
    return (
      <Card icon={Camera} title="Motion clip" className="image-generation-warning">
        <p className="card-note" role="alert">
          {displayText(data?.error, "This motion event's clip could not be assembled.")}
        </p>
      </Card>
    );
  }
  return (
    <figure className="card camera-snapshot-card">
      <div className="card-head">
        <Camera size={14} aria-hidden="true" />
        <span>Motion event clip</span>
      </div>
      {data.clip_url && (
        <video
          className="camera-motion-clip-video"
          src={data.clip_url}
          controls
          playsInline
          preload="metadata"
          aria-label={displayText(data.caption, "Motion event clip")}
        />
      )}
      <figcaption>
        <p className="camera-snapshot-caption">
          {displayText(data.caption, "No description available.")}
        </p>
        <p className="card-note">
          {data.started_at_local} – {data.ended_at_local} · {data.frame_count} frames
          {data.cached ? " · cached" : ""}
        </p>
      </figcaption>
    </figure>
  );
}

function ImageGenerationStatusCard({ data }) {
  const successful = data?.status === "completed" && data?.verified === true;
  const configured = data?.generation_available === true;
  const title = successful
    ? "Image generated"
    : configured
      ? "Image generation configured"
      : "Image generation unavailable";
  return (
    <Card icon={Sparkles} title={title} className={configured ? "" : "image-generation-warning"}>
      <p className="card-note" role={configured ? "status" : "alert"}>
        {displayText(
          data?.message,
          data?.state === "configured_stopped"
            ? "ComfyUI is configured and stopped. Generation runs sequentially after approval, then restores Omni."
            : "The local image runtime is not currently safe to schedule."
        )}
      </p>
      {data?.checkpoint && <p className="card-note">Checkpoint · {displayText(data.checkpoint)}</p>}
    </Card>
  );
}

function GeneratedVideoCard({ data, receipt }) {
  const [videoLoadFailed, setVideoLoadFailed] = useState(false);
  const media = verifiedVideoMedia(data, receipt);
  useEffect(() => setVideoLoadFailed(false), [data?.video_url]);

  if (!media) {
    return (
      <Card icon={Film} title="Video result unverified" className="video-generation-warning">
        <p className="card-note" role="alert">
          No matching successful video-generation receipt and verified local MP4 are attached. The video is not displayed.
        </p>
      </Card>
    );
  }

  const generative = data.mode === "image_to_video";
  const description = generative
    ? "Source-conditioned Wan 2.2 image-to-video clip"
    : "Hover-and-pulse animation of the verified source image";
  const availableTitle = generative
    ? "AI-generated source-conditioned video"
    : "Procedural source animation";
  const unavailableTitle = generative
    ? "AI-generated video unavailable"
    : "Procedural animation unavailable";
  const dimensions = data?.width && data?.height ? `${data.width} × ${data.height}` : "";
  const duration = Number.isFinite(Number(data?.duration_seconds))
    ? `${Number(data.duration_seconds).toLocaleString()} seconds`
    : "";
  const details = [dimensions, duration, data?.fps ? `${data.fps} fps` : ""].filter(Boolean).join(" · ");

  return (
    <figure className={`card generated-video-card${generative ? " is-generative" : ""}${videoLoadFailed ? " video-generation-warning" : ""}`}>
      <div className="card-head">
        <Film size={14} aria-hidden="true" />
        <span>{videoLoadFailed ? unavailableTitle : availableTitle}</span>
      </div>
      <p className="card-note video-generation-boundary">
        {generative
          ? "Wan 2.2 generated new source-conditioned motion with apparent 3D/depth movement. This is not a reusable 3D mesh and is not pixel-exact to the source image."
          : "Deterministic local motion and light effects applied to the exact source image. This is not generative video."}
      </p>
      {videoLoadFailed ? (
        <div className="generated-video-load-error" role="alert">
          <strong>The verified MP4 could not be played.</strong>
          <span>The browser failed to load the same-origin video file. Playback and download are hidden; no successful display is being claimed.</span>
        </div>
      ) : (
        <video
          className="generated-video"
          src={media.src}
          poster={media.poster}
          controls
          playsInline
          preload="metadata"
          aria-label={`${generative ? "AI-generated source-conditioned video" : "Procedural animation"}: ${description}`}
          onError={() => setVideoLoadFailed(true)}
        />
      )}
      <figcaption>
        <p className="generated-video-prompt">{description}</p>
        {details && (
          <p className="card-note">
            {details} · H.264 MP4 · {generative ? displayText(data.model_id) : displayText(data.provider)}
          </p>
        )}
        {!videoLoadFailed && (
          <a className="generated-video-download" href={media.src} download={media.filename}>
            <Download size={14} aria-hidden="true" />
            Download verified MP4
          </a>
        )}
      </figcaption>
    </figure>
  );
}

function VideoGenerationStatusCard({ data }) {
  const failure = videoFailureDisclosure(data);
  const hasModeRecords = data?.modes && typeof data.modes === "object";
  const proceduralAvailable = hasModeRecords
    ? data.modes?.exact_source_animation?.generation_available === true
    : data?.exact_source_animation_available === true || data?.generation_available === true;
  const generativeAvailable = hasModeRecords
    ? data.modes?.image_to_video?.generation_available === true
    : data?.image_to_video_available === true || data?.true_generation_available === true;
  const title = failure?.title || (
    generativeAvailable && proceduralAvailable
      ? "Generative video and procedural animation available"
      : generativeAvailable
        ? "Generative video available"
        : proceduralAvailable
          ? "Procedural video animation available"
          : "Video generation unavailable"
  );
  const warning = Boolean(failure) || (!proceduralAvailable && !generativeAvailable);

  return (
    <Card icon={Film} title={title} className={warning ? "video-generation-warning" : ""}>
      <p className="card-note" role={warning ? "alert" : "status"}>
        {failure?.message || displayText(
          data?.message,
          generativeAvailable
            ? "X Omni can create genuine source-conditioned Wan 2.2 video after approval."
            : proceduralAvailable
              ? "X Omni can create a deterministic MP4 from an exact source image after approval."
              : "No verified local video renderer is currently available."
        )}
      </p>
      {!failure && generativeAvailable && (
        <p className="card-note video-generation-boundary">
          Wan 2.2 creates new source-conditioned motion with apparent 3D/depth movement.
          It does not create a reusable 3D mesh and is not pixel-exact to the source image.
        </p>
      )}
      {!failure && proceduralAvailable && (
        <p className="card-note video-generation-boundary">
          Procedural mode applies deterministic hover-and-pulse effects and is not AI-generated image-to-video.
        </p>
      )}
    </Card>
  );
}

function WebsitePreviewCard({ data }) {
  const artifactIdentity = websiteArtifactIdentity(data);
  const [view, setView] = useState(() => restoredWebsiteView(data));
  const [actionNotice, setActionNotice] = useState(null);
  const panelId = useId();
  const title = displayText(data?.title, "Generated website");
  const html = displayText(data?.html);

  useEffect(() => {
    setView(restoredWebsiteView(data));
    setActionNotice(null);
  }, [artifactIdentity]);

  if (!data?.ok || !html) {
    return (
      <Card icon={LayoutTemplate} title="Website preview unavailable" className="website-warning">
        <p className="card-note" role="alert">
          {displayText(data?.message, "The model did not produce a usable website preview.")}
        </p>
      </Card>
    );
  }

  async function copyCode() {
    try {
      await copyGeneratedHtml(html);
      setActionNotice({ kind: "success", text: "HTML copied to the clipboard." });
    } catch (error) {
      setActionNotice({
        kind: "error",
        text: displayText(error?.message, "The generated HTML could not be copied."),
      });
    }
  }

  function downloadHtml() {
    try {
      const download = downloadGeneratedHtml(html, title);
      setActionNotice({ kind: "success", text: `Download requested for ${download.filename}.` });
    } catch (error) {
      setActionNotice({
        kind: "error",
        text: displayText(error?.message, "The generated HTML could not be downloaded."),
      });
    }
  }

  const showingPreview = view === "preview";

  function changeView(nextView) {
    setView(persistWebsiteView(data, nextView));
    setActionNotice(null);
  }

  return (
    <section className="card website-card" aria-label={`Generated website: ${title}`}>
      <div className="card-head website-card-head">
        <LayoutTemplate size={14} aria-hidden="true" />
        <span>Website · {title}</span>
      </div>
      <p className="card-note website-boundary">Buffered in chat · not written or deployed</p>
      <div className="website-body">
        <div className="website-toolbar" role="group" aria-label="Generated website actions">
          <button
            type="button"
            className="website-action is-view-toggle"
            onClick={() => {
              changeView(showingPreview ? "code" : "preview");
            }}
            aria-controls={panelId}
            aria-pressed={showingPreview}
            aria-label={showingPreview ? "Show generated HTML code" : "Show rendered website preview"}
          >
            {showingPreview
              ? <Code2 size={15} aria-hidden="true" />
              : <Eye size={15} aria-hidden="true" />}
            {showingPreview ? "Code" : "Preview"}
          </button>
          <button
            type="button"
            className="website-action"
            onClick={copyCode}
            aria-label="Copy generated HTML code"
          >
            <Copy size={15} aria-hidden="true" />
            Copy code
          </button>
          <button
            type="button"
            className="website-action"
            onClick={downloadHtml}
            aria-label="Download generated website as HTML"
          >
            <Download size={15} aria-hidden="true" />
            Download HTML
          </button>
        </div>
        {showingPreview ? (
          <div
            id={panelId}
            className="website-view website-preview-view"
            role="region"
            aria-label={`Rendered preview of ${title}`}
          >
            <iframe
              className="website-frame"
              srcDoc={html}
              sandbox=""
              referrerPolicy="no-referrer"
              title={`${title} generated website preview`}
            />
          </div>
        ) : (
          <div
            id={panelId}
            className="website-view website-code-view"
            role="region"
            aria-label={`HTML code for ${title}`}
          >
            <pre className="pre"><code>{html}</code></pre>
          </div>
        )}
        <p className="card-note website-policy">
          Sandboxed static preview. Network, scripts, forms, and navigation are disabled.
          {" "}Page-initiated downloads inside the sandbox are disabled.
        </p>
        {actionNotice && (
          <p
            className={`card-note website-action-notice is-${actionNotice.kind}`}
            role={actionNotice.kind === "error" ? "alert" : "status"}
            aria-live={actionNotice.kind === "error" ? "assertive" : "polite"}
          >
            {actionNotice.text}
          </p>
        )}
      </div>
    </section>
  );
}

const REGISTRY = {
  weather: WeatherCard,
  calendar: CalendarCard,
  tasks: TasksCard,
  task_added: TaskAddedCard,
  task_updated: TaskUpdatedCard,
  system_status: SystemStatusCard,
  directory: DirectoryCard,
  file: FileCard,
  file_search: FileSearchCard,
  web_research: WebResearchCard,
  capabilities: CapabilitiesCard,
  file_written: FileWrittenCard,
  shell_result: ShellCard,
  calendar_event_created: CalendarEventCreatedCard,
  camera_request: CameraRequestCard,
  exterior_camera_request: ExteriorCameraRequestCard,
  camera_observation: CameraObservationCard,
  camera_event_history: CameraEventHistoryCard,
  camera_snapshot: CameraSnapshotCard,
  camera_motion_clip: CameraMotionClipCard,
  generated_image: GeneratedImageCard,
  image_generation_status: ImageGenerationStatusCard,
  generated_video: GeneratedVideoCard,
  video_generation_status: VideoGenerationStatusCard,
  website_preview: WebsitePreviewCard,
  // Otis's field systems: ADAS SI documents and Calibration IQ repair orders.
  ...FIELD_CARDS,
};

export default function Artifact({
  artifact,
  onCameraCapture,
  onExteriorCameraStatus,
  onExteriorCameraConfigure,
  onExteriorCameraStart,
  onExteriorCameraStop,
}) {
  const Component = REGISTRY[artifact?.type];
  if (!Component) return null;
  return (
    <Component
      data={artifact.data}
      receipt={artifact.receipt}
      onCameraCapture={onCameraCapture}
      onExteriorCameraStatus={onExteriorCameraStatus}
      onExteriorCameraConfigure={onExteriorCameraConfigure}
      onExteriorCameraStart={onExteriorCameraStart}
      onExteriorCameraStop={onExteriorCameraStop}
    />
  );
}
