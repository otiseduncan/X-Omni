export const CAMERA_MAX_DIMENSION = 1280;
export const CAMERA_MAX_JPEG_BYTES = 4 * 1024 * 1024;

export function encodeCameraPromptHeader(value) {
  const bytes = new TextEncoder().encode(String(value || ""));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export function fitCameraFrame(width, height, maxDimension = CAMERA_MAX_DIMENSION) {
  const sourceWidth = Number(width);
  const sourceHeight = Number(height);
  if (!Number.isFinite(sourceWidth) || !Number.isFinite(sourceHeight) || sourceWidth < 1 || sourceHeight < 1) {
    throw new Error("The camera did not provide a usable video frame.");
  }
  const scale = Math.min(1, maxDimension / Math.max(sourceWidth, sourceHeight));
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
  };
}

function waitForVideoFrame(video, timeoutMs = 10_000) {
  if (video.videoWidth > 0 && video.videoHeight > 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let timer;
    const cleanup = () => {
      globalThis.clearTimeout(timer);
      video.removeEventListener("loadedmetadata", ready);
      video.removeEventListener("canplay", ready);
      video.removeEventListener("error", failed);
    };
    const ready = () => {
      if (video.videoWidth < 1 || video.videoHeight < 1) return;
      cleanup();
      resolve();
    };
    const failed = () => {
      cleanup();
      reject(new Error("The camera stream could not produce a frame."));
    };
    timer = globalThis.setTimeout(() => {
      cleanup();
      reject(new Error("The camera did not produce a frame in time."));
    }, timeoutMs);
    video.addEventListener("loadedmetadata", ready);
    video.addEventListener("canplay", ready);
    video.addEventListener("error", failed);
  });
}

function canvasJpeg(canvas, quality = 0.82) {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) reject(new Error("The browser could not encode the camera frame."));
        else resolve(blob);
      },
      "image/jpeg",
      quality
    );
  });
}

function stopTracks(stream) {
  for (const track of stream?.getTracks?.() || []) {
    try {
      track.stop();
    } catch {
      // Continue stopping the remaining tracks.
    }
  }
}

/**
 * Start a user-visible camera preview. Call this only from an explicit user
 * action. The returned MediaStream is runtime-only and must be passed to
 * stopCameraPreview when the operator stops the camera or the card unmounts.
 */
export async function startCameraPreview({
  video,
  mediaDevices = globalThis.navigator?.mediaDevices,
  onStage = () => {},
  onStream = () => {},
} = {}) {
  if (!mediaDevices?.getUserMedia || !video) {
    const error = new Error("Camera capture is not supported by this browser.");
    error.code = "camera_unsupported";
    throw error;
  }

  let stream = null;
  try {
    onStage("requesting_permission");
    stream = await mediaDevices.getUserMedia({ video: true, audio: false });
    // Transfer ownership immediately—before play()/frame readiness can wait—
    // so an operator pressing Stop during startup can end every track now.
    if (onStream(stream) === false) {
      const error = new Error("Camera startup was cancelled before the preview became live.");
      error.name = "AbortError";
      throw error;
    }
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    await video.play();
    await waitForVideoFrame(video);
    onStage("live");
    return stream;
  } catch (error) {
    if (video.srcObject === stream) video.srcObject = null;
    stopTracks(stream);
    throw error;
  }
}

/** Stop every track and detach the live preview without retaining a frame. */
export function stopCameraPreview({ video, stream } = {}) {
  if (video) {
    try {
      video.pause();
    } catch {
      // Some test/browser implementations do not expose a working pause.
    }
    video.srcObject = null;
  }
  stopTracks(stream);
}

/**
 * Encode exactly the current video frame as a bounded JPEG. This does not stop
 * or replace the video's MediaStream, so an operator-started preview stays live.
 */
export async function captureCameraVideoJpeg({
  video,
  documentRef = globalThis.document,
  onStage = () => {},
} = {}) {
  if (!video || !documentRef?.createElement) {
    const error = new Error("Camera capture is not supported by this browser.");
    error.code = "camera_unsupported";
    throw error;
  }

  onStage("capturing");
  await waitForVideoFrame(video);
  const dimensions = fitCameraFrame(video.videoWidth, video.videoHeight);
  const canvas = documentRef.createElement("canvas");
  canvas.width = dimensions.width;
  canvas.height = dimensions.height;
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("The browser could not prepare the camera frame.");
  context.drawImage(video, 0, 0, dimensions.width, dimensions.height);

  const blob = await canvasJpeg(canvas);
  if (blob.type !== "image/jpeg") throw new Error("The browser returned an unsupported camera format.");
  if (blob.size > CAMERA_MAX_JPEG_BYTES) {
    throw new Error("The captured frame is too large to analyze safely.");
  }
  return { blob, ...dimensions };
}

/** Accept only an opaque session and same-origin stream URL returned by Core. */
export function safeExteriorCameraSession(payload, locationRef = globalThis.location) {
  const sessionId = String(payload?.session_id || "").trim();
  const rawUrl = String(payload?.stream_url || "").trim();
  const origin = String(locationRef?.origin || "").trim();
  if (!/^[A-Za-z0-9_-]{8,160}$/.test(sessionId)) {
    throw new Error("Core returned an invalid exterior camera session.");
  }
  if (!rawUrl || !origin) {
    throw new Error("Core did not return an exterior camera stream URL.");
  }
  let streamUrl;
  try {
    const resolved = new URL(rawUrl, `${origin}/`);
    if (resolved.origin !== origin || !["http:", "https:"].includes(resolved.protocol)) {
      throw new Error("cross-origin");
    }
    streamUrl = `${resolved.pathname}${resolved.search}`;
  } catch {
    throw new Error("Core returned an unsafe exterior camera stream URL.");
  }
  return {
    session_id: sessionId,
    stream_url: streamUrl,
    status: boundedText(payload?.status, 80),
    label: boundedText(payload?.label, 80) || "Exterior camera",
  };
}

/**
 * Capture one bounded JPEG. The caller must invoke this from an explicit user
 * action. The Blob stays in this function's return value only long enough for
 * upload; no data URL or raw frame enters chat state or durable history.
 */
export async function captureCameraJpeg({
  mediaDevices = globalThis.navigator?.mediaDevices,
  documentRef = globalThis.document,
  onStage = () => {},
} = {}) {
  if (!mediaDevices?.getUserMedia || !documentRef?.createElement) {
    const error = new Error("Camera capture is not supported by this browser.");
    error.code = "camera_unsupported";
    throw error;
  }

  let stream = null;
  let video = null;
  try {
    video = documentRef.createElement("video");
    stream = await startCameraPreview({ video, mediaDevices, onStage });
    return await captureCameraVideoJpeg({ video, documentRef, onStage });
  } finally {
    stopCameraPreview({ video, stream });
  }
}

export function cameraFailureMessage(error) {
  const name = String(error?.name || "");
  const code = String(error?.code || "");
  if (code === "camera_unsupported") return "Camera capture is not supported in this browser.";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    return "Camera permission was not granted. Use camera again and allow access when the browser asks.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "No camera was found on this device.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "The camera is unavailable or already in use by another application.";
  }
  if (name === "SecurityError") return "Camera access is blocked for this page.";
  if (name === "AbortError") return "Camera analysis timed out. The frame was not described.";
  if (name === "TypeError" && /fetch|network|load failed/i.test(String(error?.message || ""))) {
    return "Could not reach X Omni Core for camera analysis. The frame was not described.";
  }
  const message = String(error?.message || "").trim();
  return message || "Camera capture failed before X Omni could describe a frame.";
}

function boundedText(value, limit) {
  if (typeof value !== "string") return "";
  const text = value.trim();
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}\u2026`;
}

/** Only a bounded, explicit projection of server truth may enter UI state. */
export function safeCameraObservationArtifact(artifact) {
  if (artifact?.type !== "camera_observation" || !artifact.data || typeof artifact.data !== "object") {
    throw new Error("Core did not return a camera observation.");
  }
  const raw = artifact.data;
  const description = boundedText(raw.description || raw.observation || raw.text, 12_000);
  if (!description) throw new Error("The vision model returned no camera description.");

  const media = raw.media && typeof raw.media === "object" ? raw.media : {};
  const provenance = raw.provenance && typeof raw.provenance === "object" ? raw.provenance : {};
  const number = (value) => (Number.isFinite(Number(value)) ? Number(value) : undefined);
  const safeData = {
    ok: raw.ok !== false,
    description,
    prompt: boundedText(raw.prompt, 1_000),
    source: ["camera", "browser_camera_still", "exterior_camera_still"].includes(raw.source)
      ? raw.source
      : "camera",
    camera_source_id: boundedText(raw.camera_source_id || provenance.camera_source_id, 80),
    camera_label: boundedText(raw.camera_label || provenance.camera_label, 160),
    capture_transport: boundedText(raw.capture_transport || provenance.capture_transport, 80),
    media_type: boundedText(raw.media_type || raw.mime || media.media_type || media.mime_type, 64),
    bytes: number(raw.bytes ?? media.bytes),
    width: number(raw.width ?? media.width),
    height: number(raw.height ?? media.height),
    sha256: boundedText(raw.sha256 || media.sha256, 64),
    model: boundedText(raw.model || raw.model_alias || provenance.model || provenance.model_alias, 160),
    worker: boundedText(raw.worker || provenance.worker, 80),
    analyzed_at: boundedText(raw.analyzed_at || raw.created_at || provenance.analyzed_at, 80),
  };
  return {
    type: "camera_observation",
    data: Object.fromEntries(Object.entries(safeData).filter(([, value]) => value !== undefined && value !== "")),
  };
}
