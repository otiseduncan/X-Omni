import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  cameraFailureMessage,
  captureCameraJpeg,
  captureCameraVideoJpeg,
  encodeCameraPromptHeader,
  fitCameraFrame,
  safeCameraObservationArtifact,
  safeExteriorCameraSession,
  startCameraPreview,
  stopCameraPreview,
} from "../src/lib/cameraCapture.js";

test("camera prompt header is UTF-8 base64url without padding", () => {
  const encoded = encodeCameraPromptHeader("What is on Otis’s bench? 🔧");
  assert.match(encoded, /^[A-Za-z0-9_-]+$/);
  assert.doesNotMatch(encoded, /=/);
  const standard = encoded.replace(/-/g, "+").replace(/_/g, "/");
  const padded = standard + "=".repeat((4 - (standard.length % 4)) % 4);
  assert.equal(
    new TextDecoder().decode(Uint8Array.from(atob(padded), (char) => char.charCodeAt(0))),
    "What is on Otis’s bench? 🔧"
  );
});
import { timelineFromHistory } from "../src/lib/conversationTimeline.js";

test("one explicit camera capture produces a bounded JPEG and always stops every track", async () => {
  const calls = { constraints: null, draw: null, paused: false, stopped: 0, toDataUrl: 0 };
  const tracks = [
    { stop: () => { calls.stopped += 1; } },
    { stop: () => { calls.stopped += 1; } },
  ];
  const stream = { getTracks: () => tracks };
  const video = {
    videoWidth: 1920,
    videoHeight: 1080,
    srcObject: null,
    muted: false,
    playsInline: false,
    play: async () => {},
    pause: () => { calls.paused = true; },
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({
      drawImage: (...args) => { calls.draw = args; },
    }),
    toBlob: (callback, type, quality) => {
      assert.equal(type, "image/jpeg");
      assert.equal(quality, 0.82);
      callback(new Blob([new Uint8Array(128)], { type: "image/jpeg" }));
    },
    toDataURL: () => { calls.toDataUrl += 1; },
  };
  const documentRef = {
    createElement: (tag) => (tag === "video" ? video : canvas),
  };
  const mediaDevices = {
    getUserMedia: async (constraints) => {
      calls.constraints = constraints;
      return stream;
    },
  };
  const stages = [];

  const frame = await captureCameraJpeg({
    mediaDevices,
    documentRef,
    onStage: (stage) => stages.push(stage),
  });

  assert.deepEqual(calls.constraints, { video: true, audio: false });
  assert.deepEqual(stages, ["requesting_permission", "live", "capturing"]);
  assert.equal(frame.width, 1280);
  assert.equal(frame.height, 720);
  assert.equal(frame.blob.type, "image/jpeg");
  assert.equal(calls.draw[3], 1280);
  assert.equal(calls.draw[4], 720);
  assert.equal(calls.toDataUrl, 0);
  assert.equal(calls.stopped, 2);
  assert.equal(calls.paused, true);
  assert.equal(video.srcObject, null);
});

test("live preview stays open across explicit frame analysis and stop clears every track", async () => {
  const calls = { draw: 0, paused: 0, stopped: 0 };
  const tracks = [
    { stop: () => { calls.stopped += 1; } },
    { stop: () => { calls.stopped += 1; } },
  ];
  const stream = { getTracks: () => tracks };
  const video = {
    videoWidth: 1920,
    videoHeight: 1080,
    srcObject: null,
    muted: false,
    playsInline: false,
    play: async () => {},
    pause: () => { calls.paused += 1; },
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage: () => { calls.draw += 1; } }),
    toBlob: (callback) => callback(new Blob([new Uint8Array(256)], { type: "image/jpeg" })),
  };
  const stages = [];

  const opened = await startCameraPreview({
    video,
    mediaDevices: {
      getUserMedia: async (constraints) => {
        assert.deepEqual(constraints, { video: true, audio: false });
        return stream;
      },
    },
    onStage: (stage) => stages.push(stage),
  });
  const frame = await captureCameraVideoJpeg({
    video,
    documentRef: { createElement: () => canvas },
    onStage: (stage) => stages.push(stage),
  });

  assert.equal(opened, stream);
  assert.equal(video.srcObject, stream);
  assert.equal(frame.blob.type, "image/jpeg");
  assert.equal(frame.width, 1280);
  assert.equal(frame.height, 720);
  assert.equal(calls.draw, 1);
  assert.equal(calls.stopped, 0, "analysis must leave the live stream running");
  assert.deepEqual(stages, ["requesting_permission", "live", "capturing"]);

  stopCameraPreview({ video, stream });
  assert.equal(video.srcObject, null);
  assert.equal(calls.paused, 1);
  assert.equal(calls.stopped, 2);
});

test("exterior camera sessions accept only opaque IDs and same-origin stream URLs", () => {
  const locationRef = { origin: "http://127.0.0.1:8100" };
  assert.deepEqual(
    safeExteriorCameraSession({
      session_id: "exterior_12345678",
      stream_url: "http://127.0.0.1:8100/api/cameras/exterior/sessions/exterior_12345678/stream?view=live",
      status: "connected",
      label: "Driveway",
    }, locationRef),
    {
      session_id: "exterior_12345678",
      stream_url: "/api/cameras/exterior/sessions/exterior_12345678/stream?view=live",
      status: "connected",
      label: "Driveway",
    }
  );
  assert.throws(
    () => safeExteriorCameraSession({
      session_id: "short",
      stream_url: "/api/cameras/exterior/stream",
    }, locationRef),
    /invalid exterior camera session/
  );
  assert.throws(
    () => safeExteriorCameraSession({
      session_id: "exterior_12345678",
      stream_url: "https://example.com/camera.mjpeg",
    }, locationRef),
    /unsafe exterior camera stream URL/
  );
});

test("a stream is stoppable immediately while preview frame readiness is still pending", async () => {
  let stopped = false;
  let releaseFrame;
  const frameReady = new Promise((resolve) => { releaseFrame = resolve; });
  const track = { stop: () => { stopped = true; } };
  const stream = { getTracks: () => [track] };
  const video = {
    videoWidth: 0,
    videoHeight: 0,
    muted: false,
    playsInline: false,
    srcObject: null,
    play: async () => {},
    pause: () => {},
    addEventListener: (_name, callback) => { frameReady.then(callback); },
    removeEventListener: () => {},
  };
  let acquired = null;
  const starting = startCameraPreview({
    video,
    mediaDevices: { getUserMedia: async () => stream },
    onStream: (value) => { acquired = value; },
  });

  await Promise.resolve();
  await Promise.resolve();
  assert.equal(acquired, stream);
  stopCameraPreview({ video, stream: acquired });
  assert.equal(stopped, true);
  assert.equal(video.srcObject, null);

  // Let the pending helper unwind; it may stop the already-stopped track
  // again, but it must not retain or resurrect the MediaStream.
  video.videoWidth = 640;
  video.videoHeight = 480;
  releaseFrame();
  await starting;
  assert.equal(video.srcObject, null);
});

test("camera tracks are stopped when frame preparation fails", async () => {
  let stopped = false;
  const video = {
    videoWidth: 640,
    videoHeight: 480,
    srcObject: null,
    play: async () => {},
    pause: () => {},
  };
  const documentRef = {
    createElement: (tag) => tag === "video"
      ? video
      : { getContext: () => null, width: 0, height: 0 },
  };
  const mediaDevices = {
    getUserMedia: async () => ({ getTracks: () => [{ stop: () => { stopped = true; } }] }),
  };

  await assert.rejects(
    captureCameraJpeg({ mediaDevices, documentRef }),
    /could not prepare/
  );
  assert.equal(stopped, true);
  assert.equal(video.srcObject, null);
});

test("camera observation state keeps only bounded answer and provenance", () => {
  const artifact = safeCameraObservationArtifact({
    type: "camera_observation",
    data: {
      ok: true,
      description: "A blue mug is on the desk.",
      prompt: "What is visible?",
      source: "browser_camera_still",
      mime: "image/jpeg",
      image: "raw bytes",
      base64: "secret",
      data_url: "data:image/jpeg;base64,secret",
      media: { width: 1920, height: 1080, bytes: 1234, sha256: "a".repeat(64) },
      arbitrary_secret: "do not retain",
    },
  });

  assert.equal(artifact.data.description, "A blue mug is on the desk.");
  assert.equal(artifact.data.source, "browser_camera_still");
  assert.equal(artifact.data.media_type, "image/jpeg");
  assert.equal(artifact.data.width, 1920);
  assert.equal("image" in artifact.data, false);
  assert.equal("base64" in artifact.data, false);
  assert.equal("data_url" in artifact.data, false);
  assert.equal("arbitrary_secret" in artifact.data, false);

  const exterior = safeCameraObservationArtifact({
    type: "camera_observation",
    data: {
      description: "The exterior gate is closed.",
      source: "exterior_camera_still",
      camera_source_id: "exterior",
      camera_label: "Driveway",
      capture_transport: "mjpeg",
    },
  });
  assert.equal(exterior.data.source, "exterior_camera_still");
  assert.equal(exterior.data.camera_source_id, "exterior");
  assert.equal(exterior.data.camera_label, "Driveway");
  assert.equal(exterior.data.capture_transport, "mjpeg");

  const timeline = timelineFromHistory([{
    id: 7,
    role: "assistant",
    content: "",
    artifacts: [{
      type: "camera_observation",
      data: {
        description: "A blue mug is on the desk.",
        source: "browser_camera_still",
        mime: "image/jpeg",
        data_url: "data:image/jpeg;base64,secret",
      },
    }],
  }]);
  assert.equal(timeline.length, 1);
  assert.equal(timeline[0].artifact.data.description, "A blue mug is on the desk.");
  assert.equal("data_url" in timeline[0].artifact.data, false);
});

test("camera dimensions and browser failures have deterministic truthful copy", () => {
  assert.deepEqual(fitCameraFrame(720, 1280), { width: 720, height: 1280 });
  assert.deepEqual(fitCameraFrame(4000, 2000), { width: 1280, height: 640 });
  assert.throws(() => fitCameraFrame(0, 10), /usable video frame/);
  assert.match(cameraFailureMessage({ name: "NotAllowedError" }), /permission was not granted/);
  assert.match(cameraFailureMessage({ name: "NotFoundError" }), /No camera was found/);
  assert.match(cameraFailureMessage({ name: "AbortError" }), /was not described/);
  assert.match(cameraFailureMessage({ name: "TypeError", message: "Failed to fetch" }), /Could not reach X Omni Core/);
});

test("camera capability stays inline, explicitly controlled, and reconciles one durable artifact", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");
  const capture = await readFile(new URL("../src/lib/cameraCapture.js", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");

  assert.match(cards, /camera_request:\s*CameraRequestCard/);
  assert.match(cards, /camera_observation:\s*CameraObservationCard/);
  assert.match(cards, /onClick=\{startLiveCamera\}[\s\S]*?Start live camera/);
  assert.match(cards, /<video[\s\S]*?muted[\s\S]*?playsInline[\s\S]*?aria-label="Live camera preview"/);
  assert.match(cards, /onClick=\{analyzeCurrentFrame\}[\s\S]*?Analyze current frame/);
  assert.match(cards, /onClick=\{stopLiveCamera\}[\s\S]*?Stop camera/);
  assert.match(cards, /onStream: \(acquiredStream\)[\s\S]*?streamRef\.current = acquiredStream/);
  assert.match(cards, /useEffect\(\(\) => \(\) => \{[\s\S]*?stopCameraPreview/);
  assert.match(cards, /<strong>Camera observation<\/strong>[\s\S]*?compactDescription/);
  assert.match(cards, /Nothing is captured or sent until you start it and choose Analyze current frame/);
  assert.match(app, /"X-XOmni-Conversation-ID": String\(conversationId\)/);
  assert.match(app, /"X-XOmni-Camera-Prompt-B64": encodeCameraPromptHeader\(prompt\)/);
  assert.match(app, /headers\["Content-Type"\] = frame\.blob\.type/);
  assert.match(app, /request\.body = frame\.blob/);
  assert.doesNotMatch(app, /new FormData\(|form\.set\(/);
  assert.match(app, /fetch\("\/api\/vision\/analyze"/);
  assert.match(app, /payload\.message_id \? `artifact:\$\{payload\.message_id\}:0`/);
  assert.match(app, /await reconcile\(\)/);
  assert.match(capture, /getUserMedia\(\{ video: true, audio: false \}\)/);
  assert.match(capture, /export async function captureCameraVideoJpeg/);
  assert.match(capture, /export function stopCameraPreview/);
  assert.match(capture, /for \(const track of stream\?\.getTracks[\s\S]*?track\.stop\(\)/);
  assert.match(capture, /finally\s*\{[\s\S]*?stopCameraPreview/);
  assert.doesNotMatch(`${app}\n${cards}\n${capture}`, /toDataURL|FileReader|data:image\/jpeg;base64/);
  assert.doesNotMatch(`${app}\n${cards}\n${capture}`, /setInterval|requestAnimationFrame/);
  assert.doesNotMatch(cards, /role="dialog"|aria-modal|backdrop/);
  assert.match(styles, /\.camera-action\s*\{[\s\S]*?min-height:\s*44px/);
});
