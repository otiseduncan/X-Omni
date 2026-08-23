import { useCallback, useEffect, useRef, useState } from "react";
import {
  Cpu,
  KeyRound,
  Loader2,
  LogIn,
  Mic,
  Plus,
  Send,
  Settings2,
  Square,
  Volume2,
  VolumeX,
  Wrench,
} from "lucide-react";

import AuthPanel from "./components/AuthPanel.jsx";
import Avatar from "./components/Avatar.jsx";
import ApprovalCard from "./components/ApprovalCard.jsx";
import DashboardRail from "./components/DashboardRail.jsx";
import VoicePanel from "./components/VoicePanel.jsx";
import ToolRail from "./components/ToolRail.jsx";
import Artifact from "./components/cards/Cards.jsx";
import { useChatSocket } from "./hooks/useChatSocket.js";
import { useConversationContinuity } from "./hooks/useConversationContinuity.js";
import { useVoice } from "./hooks/useVoice.js";
import {
  cameraFailureMessage,
  captureCameraJpeg,
  captureCameraVideoJpeg,
  encodeCameraPromptHeader,
  safeCameraObservationArtifact,
  safeExteriorCameraSession,
} from "./lib/cameraCapture.js";
import {
  receiptMatchesArtifact,
  receiptState,
  receiptUpdateFromArtifact,
  terminalMediaWorkload,
  updateApproval,
} from "./lib/conversationTimeline.js";
import { settledWorkerHealth } from "./lib/workerState.js";
import "./styles/theme.css";
import "./styles/app.css";
import "./styles/field-cards.css";

const AUTH_ERRORS = {
  not_owner: "That Google account isn't the owner of this X Omni instance.",
  no_refresh_token:
    "Google didn't return a refresh token. Revoke X Omni's access in your Google account settings, then sign in again.",
  exchange_failed: "Google sign-in failed during the token exchange.",
  invalid_state: "Sign-in session expired. Try again.",
  no_subject: "Google didn't return an account identifier.",
  email_not_verified: "Google must provide a verified email address.",
  identity_mismatch:
    "Use the same email for Google that Tailscale authenticated for this enrollment.",
  google_identity_conflict: "Google returned conflicting identity information.",
  enrollment_conflict:
    "This Google or Tailscale identity is already linked. Ask the Owner to review the tester record.",
  tailscale_identity_missing: "Return through the private Tailscale Serve URL and try again.",
  tailscale_identity_changed: "The Tailscale identity changed during sign-in. Start again.",
};

async function exteriorCameraPayload(response, fallbackMessage) {
  if (response.status === 204) return {};
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload?.detail === "string"
      ? payload.detail
      : typeof payload?.message === "string"
        ? payload.message
        : fallbackMessage;
    throw new Error(String(detail || fallbackMessage).slice(0, 500));
  }
  return payload && typeof payload === "object" ? payload : {};
}

export function isNearChatBottom(element, threshold = 72) {
  if (!element) return true;
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function SignIn({ error, auth, onOpenSetup }) {
  const notConfigured = auth && auth.auth_enabled && !auth.google_configured;

  if (notConfigured) {
    return (
      <div className="signin">
        <div className="signin-card">
          <div className="brand-mark">X</div>
          <h1>X Omni</h1>
          <p>
            Sign-in is required, but Google OAuth hasn&apos;t been set up yet —
            so there&apos;s nothing to sign in with.
          </p>
          <button className="google-btn" type="button" onClick={onOpenSetup}>
            <KeyRound size={17} />
            Set up Google Auth locally
          </button>
          <p className="signin-setup-note">
            Open this page at <code>127.0.0.1:8100</code> on Omega to save the
            OAuth credentials.
          </p>
        </div>
      </div>
    );
  }

  const remoteDenied = auth?.access_path === "denied";
  const remoteUninvited = auth?.access_path === "tailscale" && !auth?.remote_authorized;
  if (remoteDenied || remoteUninvited) {
    return (
      <div className="signin">
        <div className="signin-card">
          <div className="brand-mark">X</div>
          <h1>X Omni</h1>
          <p>
            {remoteDenied
              ? "Private remote access requires an authenticated Tailscale Serve identity."
              : "Your Tailscale identity reached X Omni, but it is not authorized for an X profile."}
          </p>
          <div className="signin-error">
            {auth?.remote_access_error || "Ask the X Omni Owner to authorize this exact email first."}
          </div>
        </div>
      </div>
    );
  }

  const isRemote = auth?.access_path === "tailscale";

  return (
    <div className="signin">
      <div className="signin-card">
        <div className="brand-mark">X</div>
        <h1>X Omni</h1>
        <p>
          {isRemote
            ? `Tailscale verified ${auth?.tailscale_identity}. Sign in with that same Google email to ${auth?.enrollment_status === "pending" ? "create" : "open"} your X Omni profile.`
            : "Local operator assistant on Omega. Sign in with the owner Google account to continue."}
        </p>
        <a className="google-btn" href="/api/auth/login">
          <LogIn size={17} />
          Sign in with Google
        </a>
        {!auth?.owner_bound && (
          <button className="signin-secondary" type="button" onClick={onOpenSetup}>
            <KeyRound size={15} />
            Google Auth is ready
          </button>
        )}
        {error && <div className="signin-error">{AUTH_ERRORS[error] || error}</div>}
      </div>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState(null);
  const authorised = Boolean(auth && (auth.signed_in || !auth.auth_enabled));
  const continuity = useConversationContinuity({ enabled: authorised });
  const {
    items,
    setItems,
    push,
    conversationIdRef,
    adoptConversation,
    ready: historyReady,
    restoring,
    reconcile,
    createConversation,
  } = continuity;
  const [streaming, setStreaming] = useState("");
  const [thinking, setThinking] = useState(false);
  const [activeTool, setActiveTool] = useState(null);
  const [worker, setWorker] = useState(null);
  const [swapping, setSwapping] = useState(false);
  const [swapTarget, setSwapTarget] = useState(null);
  const [externalWorkload, setExternalWorkload] = useState(null);
  const [lastSwapSeconds, setLastSwapSeconds] = useState(null);
  const [speaking, setSpeaking] = useState(false);
  const [ttsOn, setTtsOn] = useState(false);
  const [draft, setDraft] = useState("");
  const [interim, setInterim] = useState("");
  const [voicePanelOpen, setVoicePanelOpen] = useState(false);
  const [accountPanelOpen, setAccountPanelOpen] = useState(false);
  const [creatingConversation, setCreatingConversation] = useState(false);

  const streamRef = useRef(null);
  const followStreamRef = useRef(true);
  const streamingRef = useRef("");
  const lastExecutionReceiptRef = useRef(null);
  const cameraCaptureActiveRef = useRef(false);
  const textareaRef = useRef(null);
  const ttsOnRef = useRef(false);
  ttsOnRef.current = ttsOn;

  const authError = new URLSearchParams(window.location.search).get("auth_error");

  const refreshAuth = useCallback(async () => {
    const response = await fetch("/api/auth/status", {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Auth status HTTP ${response.status}`);
    const status = await response.json();
    setAuth(status);
    return status;
  }, []);

  const handleLoggedOut = useCallback(async () => {
    try {
      await refreshAuth();
    } catch {
      setAuth((current) => ({ ...(current || {}), signed_in: false }));
    }
  }, [refreshAuth]);

  useEffect(() => {
    refreshAuth().catch(() =>
      setAuth({ signed_in: false, auth_enabled: true, core_unreachable: true })
    );
  }, [refreshAuth]);

  const refreshSettledWorkerState = useCallback(async () => {
    try {
      const response = await fetch("/healthz", {
        credentials: "include",
        cache: "no-store",
      });
      const truth = settledWorkerHealth(await response.json().catch(() => null));
      if (!response.ok || !truth) return false;
      setWorker(truth.worker);
      setSwapping(false);
      setSwapTarget(null);
      setExternalWorkload(null);
      return true;
    } catch {
      return false;
    }
  }, []);

  const voice = useVoice({
    onTranscript: (text) => {
      setInterim("");
      sendMessage(text);
    },
    onInterim: setInterim,
    onSpeakingChange: setSpeaking,
    onError: (message) => {
      setInterim("");
      push({ kind: "system", text: message });
    },
  });

  const handleEvent = useCallback(
    (event) => {
      switch (event.type) {
        case "unauthorized":
          setAuth((a) => ({ ...(a || {}), signed_in: false }));
          break;

        case "worker_state":
          setWorker(event.active_worker);
          setSwapping(event.swapping);
          setExternalWorkload(event.external_workload || null);
          setSwapTarget(event.external_workload ? null : event.swapping_to || null);
          if (event.last_swap_seconds) setLastSwapSeconds(event.last_swap_seconds);
          break;

        case "swap_complete":
          setSwapping(false);
          setSwapTarget(null);
          setExternalWorkload(null);
          setWorker(event.worker);
          if (event.swapped) {
            setLastSwapSeconds(event.total_swap_s);
            push({
              kind: "system",
              text: `Switched ${event.from || "—"} → ${event.worker} in ${event.total_swap_s}s`,
            });
          }
          break;

        case "conversation":
          adoptConversation(event.conversation_id);
          break;

        case "thinking":
          setThinking(true);
          lastExecutionReceiptRef.current = null;
          streamingRef.current = "";
          setStreaming("");
          break;

        case "token":
          streamingRef.current += event.text;
          setStreaming(streamingRef.current);
          break;

        case "tool_start":
          setActiveTool(event.name);
          break;

        case "tool_result":
          setActiveTool(null);
          if (event.receipt?.approval_id) {
            lastExecutionReceiptRef.current = event.receipt;
          }
          if (terminalMediaWorkload(event.receipt)) {
            void refreshSettledWorkerState();
          }
          break;

        case "artifact": {
          const receiptUpdate = receiptUpdateFromArtifact(event.artifact);
          if (receiptUpdate) {
            lastExecutionReceiptRef.current = receiptUpdate.receipt;
            setItems((previous) =>
              updateApproval(previous, receiptUpdate.id, receiptUpdate)
            );
            if (terminalMediaWorkload(receiptUpdate.receipt)) {
              void refreshSettledWorkerState();
            }
            break;
          }
          let liveArtifact = event.artifact;
          if (event.artifact?.type === "camera_observation") {
            try {
              liveArtifact = safeCameraObservationArtifact(event.artifact);
            } catch {
              liveArtifact = {
                type: "camera_observation",
                data: {
                  ok: false,
                  description: "The camera observation could not be displayed safely.",
                },
              };
            }
          }
          const matchingReceipt =
            receiptMatchesArtifact(lastExecutionReceiptRef.current, liveArtifact?.type)
              ? lastExecutionReceiptRef.current
              : null;
          push({
            kind: "artifact",
            artifact: matchingReceipt
              ? { ...liveArtifact, receipt: matchingReceipt }
              : liveArtifact,
          });
          if ([
            "shell_result",
            "generated_image",
            "image_generation_status",
            "generated_video",
            "video_generation_status",
          ].includes(liveArtifact?.type)) {
            lastExecutionReceiptRef.current = null;
          }
          break;
        }

        case "approval":
          setThinking(false);
          setActiveTool(null);
          if (streamingRef.current.trim()) {
            push({ kind: "assistant", text: streamingRef.current, worker });
            streamingRef.current = "";
            setStreaming("");
          }
          push({
            kind: "approval",
            key: `approval:${event.approval.id}`,
            approval: event.approval,
            status: event.approval.status || "pending",
            receipt: event.approval.receipt || null,
          });
          break;

        case "approval_status":
          setItems((previous) =>
            updateApproval(previous, event.id, { status: event.status })
          );
          if (event.status === "executing") setThinking(true);
          break;

        case "approval_receipt": {
          const terminal = receiptState(event.receipt);
          setItems((previous) =>
            updateApproval(previous, event.id, {
              status: terminal || event.receipt?.status,
              receipt: event.receipt,
            })
          );
          if (terminal && terminal !== "succeeded") {
            setThinking(false);
            setActiveTool(null);
          }
          if (terminalMediaWorkload(event.receipt)) {
            void refreshSettledWorkerState();
          }
          break;
        }

        case "approval_resolved":
          setItems((prev) =>
            updateApproval(prev, event.id, {
              status: event.status || (event.approved ? "approved" : "denied"),
              receipt: event.receipt || null,
            })
          );
          if (event.approved && !event.receipt) setThinking(true);
          if (!event.approved) setThinking(false);
          break;

        case "done": {
          setThinking(false);
          setActiveTool(null);
          const text = streamingRef.current;
          streamingRef.current = "";
          lastExecutionReceiptRef.current = null;
          setStreaming("");
          if (text.trim()) {
            push({
              kind: "assistant",
              key: event.message_id ? `message:${event.message_id}` : undefined,
              text,
              worker: event.worker,
            });
            if (ttsOnRef.current) voice.speak(text);
          }
          window.setTimeout(() => reconcile(), 0);
          break;
        }

        case "cancelled": {
          setThinking(false);
          setActiveTool(null);
          const text = streamingRef.current;
          streamingRef.current = "";
          lastExecutionReceiptRef.current = null;
          setStreaming("");
          if (text.trim()) {
            push({
              kind: "assistant",
              key: event.message_id ? `message:${event.message_id}` : undefined,
              text,
              worker: event.worker || worker,
            });
          }
          window.setTimeout(() => reconcile(), 0);
          break;
        }

        case "error":
          setThinking(false);
          setActiveTool(null);
          if (streamingRef.current.trim()) {
            push({ kind: "assistant", text: streamingRef.current, worker });
            streamingRef.current = "";
            setStreaming("");
          }
          push({ kind: "error", text: event.message });
          break;

        default:
          break;
      }
    },
    [adoptConversation, push, reconcile, refreshSettledWorkerState, setItems, voice, worker]
  );

  const socketEnabled = authorised && historyReady;
  const { connected, send, attempts, rejected, connectionEpoch } = useChatSocket({
    onEvent: handleEvent,
    enabled: socketEnabled,
  });

  useEffect(() => {
    if (!connected || connectionEpoch < 1) return;
    let active = true;
    void (async () => {
      await reconcile();
      if (active) await refreshSettledWorkerState();
    })();
    return () => {
      active = false;
    };
  }, [connected, connectionEpoch, reconcile, refreshSettledWorkerState]);

  useEffect(() => {
    if (connected || connectionEpoch < 1) return;
    setThinking(false);
    setActiveTool(null);
    streamingRef.current = "";
    setStreaming("");
  }, [connected, connectionEpoch]);

  const linkLabel = connected
    ? "connected"
    : restoring
      ? "restoring…"
    : rejected
      ? "sign-in required"
      : attempts > 3
        ? "core offline"
        : "reconnecting…";

  useEffect(() => {
    const el = streamRef.current;
    if (el && followStreamRef.current) el.scrollTop = el.scrollHeight;
  }, [items, streaming, thinking, activeTool]);

  function sendMessage(text) {
    const body = String(text ?? draft).trim();
    if (!body || thinking || swapping) return;
    const ok = send({
      type: "message",
      conversation_id: conversationIdRef.current,
      text: body,
    });
    if (!ok) {
      push({ kind: "error", text: "Not connected to X Omni Core." });
      return;
    }
    followStreamRef.current = true;
    push({ kind: "user", text: body });
    setDraft("");
    setThinking(true);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  function stopResponse() {
    const sent = send({
      type: "stop",
      conversation_id: conversationIdRef.current,
    });
    if (!sent) {
      push({ kind: "error", text: "Stop was not sent because Core is disconnected." });
    }
  }

  function requestSwap(target) {
    if (swapping) return;
    const sent = send({ type: "swap", worker: target });
    if (!sent) {
      push({ kind: "error", text: "Model switch was not sent because Core is disconnected." });
      return;
    }
    setSwapping(true);
    setSwapTarget(target);
  }

  function decideApproval(id, approved) {
    const sent = send({
      type: "approve",
      approval_id: id,
      approved,
      conversation_id: conversationIdRef.current,
    });
    if (!sent) {
      push({ kind: "error", text: "Approval was not sent because Core is disconnected." });
      return;
    }
    setItems((previous) => updateApproval(previous, id, { status: "deciding" }));
  }

  async function runToolDirect(name) {
    try {
      let conversationId = conversationIdRef.current;
      if (conversationId == null) conversationId = await createConversation();

      const resp = await fetch(`/api/tools/${name}/run`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: conversationId }),
      });
      const payload = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        push({ kind: "error", text: payload.detail || `${name} failed.` });
        return;
      }
      if (payload.artifact) {
        push({
          kind: "artifact",
          key: payload.message_id ? `artifact:${payload.message_id}:0` : undefined,
          artifact: payload.artifact,
        });
      }
    } catch (err) {
      push({ kind: "error", text: `Could not run ${name}: ${err.message || err}` });
    }
  }

  const getExteriorCameraStatus = useCallback(async () => {
    const response = await fetch("/api/cameras/exterior", {
      method: "GET",
      credentials: "include",
      cache: "no-store",
    });
    return exteriorCameraPayload(response, "Could not check the exterior camera setup.");
  }, []);

  const configureExteriorCamera = useCallback(async ({ label, host, username, password }) => {
    const response = await fetch("/api/cameras/exterior/configure", {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, host, username, password }),
    });
    return exteriorCameraPayload(response, "Could not save the exterior camera setup.");
  }, []);

  const startExteriorCamera = useCallback(async () => {
    const conversationId = conversationIdRef.current;
    if (conversationId == null) {
      throw new Error("This exterior camera request is not attached to an active conversation.");
    }
    const response = await fetch("/api/cameras/exterior/sessions", {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId }),
    });
    const payload = await exteriorCameraPayload(response, "Could not start the exterior camera feed.");
    return safeExteriorCameraSession(payload, window.location);
  }, [conversationIdRef]);

  const stopExteriorCamera = useCallback(async (sessionId, { keepalive = false } = {}) => {
    const safeSessionId = String(sessionId || "").trim();
    if (!/^[A-Za-z0-9_-]{8,160}$/.test(safeSessionId)) {
      throw new Error("The exterior camera session is invalid.");
    }
    const response = await fetch(
      `/api/cameras/exterior/sessions/${encodeURIComponent(safeSessionId)}`,
      {
        method: "DELETE",
        credentials: "include",
        cache: "no-store",
        keepalive: Boolean(keepalive),
      }
    );
    await exteriorCameraPayload(response, "Could not confirm exterior camera logout.");
  }, []);

  async function captureAndAnalyzeCamera(data, onStage = () => {}, source = {}) {
    if (cameraCaptureActiveRef.current) {
      throw new Error("Another camera capture is already in progress.");
    }
    const conversationId = conversationIdRef.current;
    if (conversationId == null) {
      throw new Error("This camera request is not attached to an active conversation.");
    }

    cameraCaptureActiveRef.current = true;
    let timeout = null;
    try {
      const isExterior = source?.cameraSourceId === "exterior";
      const exteriorSessionId = String(source?.cameraSessionId || "").trim();
      let frame = null;
      if (isExterior) {
        if (!/^[A-Za-z0-9_-]{8,160}$/.test(exteriorSessionId)) {
          throw new Error("The exterior camera session is invalid or no longer active.");
        }
      } else {
        frame = source?.video
          ? await captureCameraVideoJpeg({ video: source.video, onStage })
          : await captureCameraJpeg({ onStage });
      }
      onStage("analyzing");

      const prompt = String(
        data?.prompt || "Describe what is visible in this camera frame."
      );

      const controller = new AbortController();
      timeout = window.setTimeout(() => controller.abort(), 120_000);
      const headers = {
        "X-XOmni-Conversation-ID": String(conversationId),
        "X-XOmni-Camera-Prompt-B64": encodeCameraPromptHeader(prompt),
      };
      const request = {
        method: "POST",
        credentials: "include",
        headers,
        signal: controller.signal,
      };
      if (isExterior) {
        headers["X-XOmni-Camera-Source-ID"] = "exterior";
        headers["X-XOmni-Camera-Session-ID"] = exteriorSessionId;
      } else {
        headers["Content-Type"] = frame.blob.type;
        request.body = frame.blob;
      }
      const response = await fetch("/api/vision/analyze", request);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof payload.detail === "string"
          ? payload.detail
          : typeof payload.message === "string"
            ? payload.message
            : `Camera analysis failed (HTTP ${response.status}).`;
        throw new Error(detail);
      }

      const artifact = safeCameraObservationArtifact(payload.artifact);
      if (conversationIdRef.current === conversationId) {
        push({
          kind: "artifact",
          key: payload.message_id ? `artifact:${payload.message_id}:0` : undefined,
          artifact,
        });
        await reconcile();
      }
      return artifact.data;
    } catch (error) {
      const safeError = new Error(cameraFailureMessage(error));
      safeError.name = error?.name || safeError.name;
      throw safeError;
    } finally {
      if (timeout != null) window.clearTimeout(timeout);
      cameraCaptureActiveRef.current = false;
    }
  }

  async function newConversation() {
    if (creatingConversation || thinking || swapping) return;
    setCreatingConversation(true);
    try {
      await createConversation();
      streamingRef.current = "";
      setStreaming("");
      setThinking(false);
      setActiveTool(null);
    } catch (error) {
      push({ kind: "error", text: `Could not create a conversation: ${error.message}` });
    } finally {
      setCreatingConversation(false);
    }
  }

  if (!auth) return <div className="signin" />;
  if (auth.core_unreachable) {
    return (
      <div className="signin">
        <div className="signin-card">
          <div className="brand-mark">X</div>
          <h1>X Omni</h1>
          <p>
            Can&apos;t reach X Omni Core. Check that it&apos;s running, then
            reload.
          </p>
          <div className="signin-error">
            Start it with <code>.\\scripts\\start.ps1</code>
          </div>
        </div>
      </div>
    );
  }
  if (auth.auth_enabled && !auth.signed_in)
    return (
      <>
        <SignIn
          error={authError}
          auth={auth}
          onOpenSetup={() => setAccountPanelOpen(true)}
        />
        {accountPanelOpen && (
          <AuthPanel
            auth={auth}
            onClose={() => setAccountPanelOpen(false)}
            onLoggedOut={handleLoggedOut}
          />
        )}
      </>
    );

  const avatarState = swapping
    ? "swapping"
    : voice.recording
      ? "listening"
      : speaking
        ? "speaking"
        : thinking || streaming
          ? "thinking"
          : "idle";

  const otherWorker = worker === "coder" ? "omni" : "coder";
  const imageWorkload = externalWorkload === "image_generation";
  const videoWorkload = externalWorkload === "video_generation";
  const mediaWorkload = imageWorkload || videoWorkload;
  const workloadLabel = videoWorkload ? "rendering video" : "generating image";
  const responseActive = thinking || Boolean(streaming) || Boolean(activeTool);

  return (
    <div className="shell">
      {voicePanelOpen && (
        <VoicePanel voice={voice} onClose={() => setVoicePanelOpen(false)} />
      )}
      {accountPanelOpen && (
        <AuthPanel
          auth={auth}
          onClose={() => setAccountPanelOpen(false)}
          onLoggedOut={handleLoggedOut}
        />
      )}

      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">X</div>
          <div className="brand-text">
            <strong>X Omni</strong>
            <span aria-live="polite">{linkLabel}</span>
          </div>
        </div>

        <div className="topbar-actions">
          <button
            className={`worker-pill${swapping ? " is-swapping" : ""}`}
            onClick={() => requestSwap(otherWorker)}
            disabled={swapping || thinking || !connected}
            title={mediaWorkload ? (videoWorkload ? "Rendering local video; the conversation model may temporarily unload, and any unload must be verified restored" : "Generating image; success requires verified Omni restoration") : swapping ? "Switching model…" : `Switch to ${otherWorker} (~15-20s)`}
            aria-label={mediaWorkload ? (videoWorkload ? "Rendering local video; the conversation model may temporarily unload, and any unload must be verified restored" : "Generating image locally; success requires verified model restoration") : swapping ? `Switching model to ${swapTarget}` : `Switch model to ${otherWorker}`}
          >
            <span className={`dot ${swapping ? "warn" : worker ? "" : "off"}`} />
            <span className="worker-label">
              {mediaWorkload ? workloadLabel : swapping ? `→ ${swapTarget || "model"}` : worker || "no worker"}
            </span>
          </button>

          <button
            className="icon-btn"
            onClick={() => {
              if (speaking) voice.stopSpeaking();
              setTtsOn((v) => !v);
            }}
            title={ttsOn ? "Spoken replies on" : "Spoken replies off"}
            aria-label={ttsOn ? "Turn spoken replies off" : "Turn spoken replies on"}
            aria-pressed={ttsOn}
          >
            {ttsOn ? <Volume2 size={17} /> : <VolumeX size={17} />}
          </button>

          <button
            className="icon-btn"
            onClick={() => setVoicePanelOpen(true)}
            title="Voice settings"
            aria-label="Open voice settings"
          >
            <Settings2 size={17} />
          </button>

          <button
            className="icon-btn"
            onClick={newConversation}
            title="New conversation"
            aria-label="Start a new conversation"
            disabled={creatingConversation || thinking || swapping || !connected}
          >
            {creatingConversation ? <Loader2 size={17} className="spin" /> : <Plus size={17} />}
          </button>

          <button
            className="icon-btn account-btn"
            onClick={() => setAccountPanelOpen(true)}
            title={auth.auth_enabled && auth.signed_in ? "Account" : "Set up Google Auth"}
            aria-label={
              auth.auth_enabled && auth.signed_in
                ? "Open account and sign out"
                : "Set up Google Auth"
            }
          >
            <KeyRound size={17} />
          </button>
        </div>
      </header>

      <aside className="side">
        <Avatar
          state={avatarState}
          worker={worker}
          swapTarget={swapTarget}
          externalWorkload={externalWorkload}
          swapSeconds={lastSwapSeconds}
        />
        <DashboardRail>
          <ToolRail
            onRun={runToolDirect}
            disabled={!historyReady || creatingConversation || thinking || swapping}
          />
        </DashboardRail>
      </aside>

      <main
        className="stream"
        ref={streamRef}
        onScroll={(event) => {
          followStreamRef.current = isNearChatBottom(event.currentTarget);
        }}
      >
        {items.length === 0 && !streaming && (
          <div className="empty-state">
            {restoring ? (
              <>Restoring the latest conversation…</>
            ) : (
              <>
                Ask X anything.
                <br />
                <code>/coder</code> switches to the coding specialist,{" "}
                <code>/omni</code> switches back.
              </>
            )}
          </div>
        )}

        {items.map((item) => {
          if (item.kind === "user")
            return (
              <div className="msg user" key={item.key}>
                {item.text}
              </div>
            );
          if (item.kind === "assistant")
            return (
              <div className="msg assistant" key={item.key}>
                {item.text}
                {item.worker && <div className="msg-meta">via {item.worker}</div>}
              </div>
            );
          if (item.kind === "system")
            return (
              <div className="msg system" key={item.key}>
                {item.text}
              </div>
            );
          if (item.kind === "error")
            return (
              <div className="msg error" key={item.key}>
                {item.text}
              </div>
            );
          if (item.kind === "artifact")
            return <Artifact artifact={item.artifact}
                key={item.key}
                onCameraCapture={captureAndAnalyzeCamera}
                onExteriorCameraStatus={getExteriorCameraStatus}
                onExteriorCameraConfigure={configureExteriorCamera}
                onExteriorCameraStart={startExteriorCamera}
                onExteriorCameraStop={stopExteriorCamera}
              />;
          if (item.kind === "approval")
            return (
              <ApprovalCard
                key={item.key}
                approval={item.approval}
                status={item.status}
                receipt={item.receipt}
                onDecide={decideApproval}
                disabled={!connected}
              />
            );
          return null;
        })}

        {activeTool && (
          <div className="tool-chip">
            <Wrench size={13} className="spin" />
            {activeTool.replace(/_/g, " ")}
          </div>
        )}

        {streaming && (
          <div className="msg assistant">
            {streaming}
            <span className="caret">▍</span>
          </div>
        )}

        {thinking && !streaming && !activeTool && (
          <div className="tool-chip">
            <Loader2 size={13} className="spin" />
            thinking
          </div>
        )}

        {swapping && (
          <div className="msg system">
            <Cpu size={12} style={{ verticalAlign: "-2px", marginRight: 5 }} />
            {imageWorkload ? (
              <>Generating the image locally. Omni is temporarily unloaded; Core will attempt to restore it, and success is shown only after that restoration is verified.</>
            ) : videoWorkload ? (
              <>Rendering the video locally. Depending on the selected mode, the conversation model may temporarily unload. If it unloads, success is shown only after the required runtime and model-restoration proofs pass.</>
            ) : (
              <>Switching to {swapTarget || "the requested model"}. This takes 15–20 seconds — the model process fully restarts, but your conversation is kept.</>
            )}
          </div>
        )}
      </main>

      <div>
        <div className="composer">
          <button
            className={`mic-btn${voice.recording ? " recording" : ""}`}
            onClick={voice.toggle}
            disabled={voice.busy || thinking || swapping || !connected || restoring}
            aria-label={voice.recording ? "Stop speech input" : "Start speech input"}
            title={
              voice.supported
                ? voice.recording
                  ? "Stop listening"
                  : `Speak (${voice.sttMode === "browser" ? "Chrome/Google" : "local Omni"})`
                : "Microphone needs HTTPS or localhost"
            }
          >
            {voice.busy ? <Loader2 size={18} className="spin" /> : <Mic size={18} />}
          </button>

          <textarea
            ref={textareaRef}
            className={voice.recording ? "listening" : ""}
            value={voice.recording && interim ? interim : draft}
            rows={1}
            placeholder={
              voice.recording
                ? "Listening…"
                : swapping
                  ? "Switching model…"
                  : "Message X…"
            }
            disabled={swapping || voice.recording}
            aria-label="Message X Omni"
            onChange={(e) => {
              setDraft(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 152)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!responseActive) sendMessage();
              }
            }}
          />

          <button
            className={`send-btn${responseActive ? " is-stop" : ""}`}
            onClick={responseActive ? stopResponse : () => sendMessage()}
            disabled={responseActive
              ? !connected
              : !draft.trim() || swapping || !connected || restoring}
            aria-label={responseActive ? "Stop response" : "Send message"}
            title={responseActive ? "Stop response" : "Send message"}
          >
            {responseActive
              ? <Square size={16} fill="currentColor" />
              : <Send size={18} />}
          </button>
        </div>
      </div>
    </div>
  );
}
