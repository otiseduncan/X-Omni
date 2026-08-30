const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = {
  mode: "playback", // "live" | "playback"
  day: null,
  dayStart: null,
  dayEnd: null,
  segments: [],   // {id, startedAt, endedAt, complete, bytes, codec, width, height}
  events: [],     // {burstId, startedAt, endedAt, caption, person, vehicle, snapshotUrl}
  snapshots: [],
  clips: [],
  liveSession: null,
  liveStartController: null,
  liveOperation: 0,
  leaving: false,
  player: {
    currentSegmentId: null,
    anchorAbsolute: null,   // Date matching video.currentTime === 0 for the loaded source
    pendingOffset: null,
    autoplayAfterLoad: false,
    advancing: false,
    advanceWatchdog: null,
    prefetchedSegmentId: null,
    speed: 1,
    directSource: false,    // true when a saved clip (not a timeline segment) is loaded
  },
  clipMark: { start: null, end: null },
};

const datePicker = $("#datePicker");
const viewer = $("#viewer");
const imageViewer = $("#imageViewer");
const videoPlayer = $("#videoPlayer");
const liveFeed = $("#liveFeed");
const liveStage = $("#liveStage");
const liveActions = $("#liveActions");
const playbackStage = $("#playbackStage");
const playerEmpty = $("#playerEmpty");
const playerControls = $("#playerControls");
const playerTimeEl = $("#playerTime");
const timelineEl = $("#timeline");
const timelineCoverageEl = $("#timelineCoverage");
const timelineMarkersEl = $("#timelineMarkers");
const timelineSelectionEl = $("#timelineSelection");
const timelinePlayheadEl = $("#timelinePlayhead");

const SNAPSHOT_MEDIA_PREFIXES = ["/api/camera-snapshots/"];
const DVR_MEDIA_PREFIXES = ["/dvr/api/"];
const LIVE_MEDIA_PREFIXES = ["/dvr/api/live/sessions/"];

function makeElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function positiveId(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function sameOriginMediaUrl(value, allowedPrefixes) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw, window.location.href);
    if (
      url.origin !== window.location.origin ||
      url.username ||
      url.password ||
      !allowedPrefixes.some((prefix) => url.pathname.startsWith(prefix))
    ) return null;
    url.hash = "";
    return url.href;
  } catch {
    return null;
  }
}

function safeLiveSession(payload) {
  const sessionId = String(payload?.session_id || "").trim();
  if (!/^[A-Za-z0-9_-]{8,160}$/.test(sessionId)) return null;
  const streamUrl = sameOriginMediaUrl(payload?.stream_url, LIVE_MEDIA_PREFIXES);
  if (!streamUrl) return null;
  const resolved = new URL(streamUrl);
  const expectedPath = `/dvr/api/live/sessions/${encodeURIComponent(sessionId)}/stream.mjpg`;
  if (resolved.pathname !== expectedPath || resolved.search) return null;
  return {
    session_id: sessionId,
    stream_url: streamUrl,
    label: String(payload?.label || "Exterior camera").slice(0, 80),
  };
}

function showEmpty(container, baseClass, message) {
  container.className = `${baseClass} empty-state`;
  container.replaceChildren();
  container.textContent = message;
}

function localDateString(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localDayBounds(dayText) {
  const [year, month, day] = dayText.split("-").map(Number);
  return {
    start: new Date(year, month - 1, day, 0, 0, 0),
    end: new Date(year, month - 1, day + 1, 0, 0, 0),
  };
}

function sqliteUtc(date) {
  return date.toISOString().slice(0, 19).replace("T", " ");
}

function parseServerTime(value) {
  if (!value) return null;
  const normalized = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${String(value).replace(" ", "T")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let size = n;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index >= 3 ? 1 : 0)} ${units[index]}`;
}

function formatTime(value) {
  const date = value instanceof Date ? value : parseServerTime(value);
  if (!date) return "—";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function formatClock(date) {
  if (!date) return "—";
  return date.toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

async function request(url, options = {}) {
  const response = await fetch(url, { ...options, credentials: "same-origin", cache: "no-store" });
  if (response.status === 401) {
    window.location.href = "/";
    throw new Error("Sign in required");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload?.detail === "string" ? payload.detail.slice(0, 300) : "";
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

/* ------------------------------ status ------------------------------ */

function renderStatus(status) {
  const badge = $("#recordingBadge");
  badge.textContent = status.recording ? "● Recording" : "Recorder offline";
  badge.className = `badge ${status.recording ? "live" : "error"}`;
  const drive = status.drive || {};
  const driveAvailable = [drive.used_bytes, drive.free_bytes, drive.total_bytes]
    .every((value) => Number.isFinite(value));
  $("#storageText").textContent = driveAvailable
    ? `Used ${formatBytes(drive.used_bytes)} · Free ${formatBytes(drive.free_bytes)} · Total ${formatBytes(drive.total_bytes)}`
    : "E:\\ unavailable";
  const profile = status.profile;
  if (profile) {
    const dimensions = Number.isFinite(profile.width) && Number.isFinite(profile.height)
      ? `${profile.width}×${profile.height}` : null;
    $("#profileText").textContent = [profile.name, dimensions, profile.codec]
      .filter(Boolean).join(" · ") || "Waiting for camera";
  } else if (status.advertised_profile) {
    const advertised = status.advertised_profile;
    const dimensions = Number.isFinite(advertised.width) && Number.isFinite(advertised.height)
      ? `${advertised.width}×${advertised.height}` : null;
    const claim = [dimensions, advertised.codec].filter(Boolean).join(" · ");
    $("#profileText").textContent = [
      advertised.name, "bitstream metadata pending", claim ? `advertised ${claim}` : null,
    ].filter(Boolean).join(" · ");
  } else {
    $("#profileText").textContent = "Waiting for camera";
  }
  $("#motionText").textContent = status.onvif_motion_events ? "Connected" : "Reconnecting";
  const usedPct = Number.isFinite(drive.used_bytes) && Number.isFinite(drive.total_bytes) && drive.total_bytes
    ? Math.max(0, Math.min(100, drive.used_bytes / drive.total_bytes * 100)) : 0;
  $("#storageFill").style.width = `${usedPct}%`;
  $("#statusNote").textContent = status.last_error
    ? status.last_error
    : "Continuous stream-copy recording · oldest completed footage is overwritten automatically when E:\\ fills.";
}

async function pollStatus() {
  try {
    renderStatus(await request("/dvr/api/status"));
  } catch (error) {
    $("#statusNote").textContent = error.message || "DVR status could not be loaded.";
  }
}

/* ------------------------------ live view ------------------------------ */

function renderLiveWatch(stage, message = "") {
  const badge = $("#liveFeedBadge");
  const start = $("#startLiveButton");
  const stop = $("#stopLiveButton");
  const placeholder = $("#livePlaceholder");
  const hasSession = Boolean(state.liveSession);
  const visible = hasSession && ["connecting", "live", "error"].includes(stage);

  liveFeed.hidden = !visible;
  placeholder.hidden = visible;
  badge.hidden = stage !== "live";
  start.hidden = hasSession;
  start.disabled = stage === "starting" || stage === "stopping";
  start.textContent = stage === "starting" ? "Starting…" : "Start live view";
  stop.hidden = !hasSession;
  stop.disabled = stage === "stopping";
  $("#liveWatchStatus").textContent = message || (
    stage === "live"
      ? "Live exterior feed is visible. DVR recording continues in the background."
      : "Start the feed when you want to watch outside. Continuous DVR recording stays independent."
  );
}

async function startLiveWatch() {
  if (state.liveSession) return;
  const operation = state.liveOperation + 1;
  state.liveOperation = operation;
  const controller = new AbortController();
  state.liveStartController = controller;
  renderLiveWatch("starting", "Opening an Owner-only camera session…");
  try {
    const payload = await request("/dvr/api/live/sessions", { method: "POST", signal: controller.signal });
    const session = safeLiveSession(payload);
    if (!session) throw new Error("The DVR service returned an invalid live camera session.");
    if (state.leaving || state.liveOperation !== operation) {
      await deleteLiveSession(session, { keepalive: true });
      return;
    }
    state.liveSession = session;
    renderLiveWatch("connecting", `Connecting to ${session.label}…`);
    liveFeed.src = session.stream_url;
  } catch (error) {
    if (state.leaving || state.liveOperation !== operation) return;
    state.liveSession = null;
    liveFeed.removeAttribute("src");
    renderLiveWatch("error", error.message || "The live exterior feed could not be started.");
  } finally {
    if (state.liveOperation === operation) state.liveStartController = null;
  }
}

async function deleteLiveSession(session, { keepalive = false } = {}) {
  return fetch(`/dvr/api/live/sessions/${encodeURIComponent(session.session_id)}`, {
    method: "DELETE", credentials: "same-origin", cache: "no-store", keepalive,
  });
}

async function stopLiveWatch({ keepalive = false, quiet = false } = {}) {
  state.liveOperation += 1;
  state.liveStartController?.abort();
  state.liveStartController = null;
  const session = state.liveSession;
  state.liveSession = null;
  liveFeed.removeAttribute("src");
  renderLiveWatch("stopping", quiet ? "" : "Closing the live camera session…");
  if (!session) { renderLiveWatch("idle"); return; }
  try {
    const response = await deleteLiveSession(session, { keepalive });
    if (!response.ok && response.status !== 404 && !keepalive) {
      throw new Error(`Disconnect could not be confirmed (${response.status}).`);
    }
    if (!quiet) renderLiveWatch("idle", "Live view is off. Continuous DVR recording is still active.");
  } catch (error) {
    if (!quiet) renderLiveWatch("error", error.message || "The DVR service could not confirm camera logout.");
  }
}

function setMode(mode) {
  state.mode = mode;
  $("#modeLiveButton").classList.toggle("active", mode === "live");
  $("#modePlaybackButton").classList.toggle("active", mode === "playback");
  liveStage.hidden = mode !== "live";
  liveActions.hidden = mode !== "live";
  playbackStage.hidden = mode !== "playback";
  playerControls.hidden = mode !== "playback";
  if (mode === "live") {
    videoPlayer.pause();
  } else if (state.liveSession) {
    stopLiveWatch({ quiet: true });
  }
}

/* ------------------------------ timeline data ------------------------------ */

async function loadTimeline(day) {
  const bounds = localDayBounds(day);
  state.day = day;
  state.dayStart = bounds.start;
  state.dayEnd = bounds.end;
  const dateParam = encodeURIComponent(day);
  const eventQuery = new URLSearchParams({ since: sqliteUtc(bounds.start), until: sqliteUtc(bounds.end) }).toString();
  const [status, segmentsResp, eventsResp, clipsResp] = await Promise.all([
    request("/dvr/api/status"),
    request(`/dvr/api/segments?date=${dateParam}`),
    request(`/dvr/api/events?${eventQuery}`),
    request("/dvr/api/clips-saved").catch(() => ({ items: [] })),
  ]);
  renderStatus(status);
  state.segments = (segmentsResp.items || []).map((row) => ({
    id: row.id,
    startedAt: parseServerTime(row.started_at),
    endedAt: parseServerTime(row.ended_at),
    complete: Boolean(row.complete),
    bytes: row.bytes,
    codec: row.codec,
    width: row.width,
    height: row.height,
    probed: Boolean(row.probed),
  })).filter((row) => row.startedAt && row.endedAt);
  state.events = (eventsResp.bursts || []).map((row) => ({
    burstId: row.burst_id,
    startedAt: parseServerTime(row.started_at),
    endedAt: parseServerTime(row.ended_at),
    caption: row.caption,
    person: Boolean(row.person_detected),
    vehicle: Boolean(row.vehicle_detected),
    frameCount: row.frame_count,
    snapshotUrl: row.snapshot_url,
  })).filter((row) => row.startedAt);
  state.snapshots = eventsResp.snapshots || [];
  state.clips = clipsResp.items || [];
  renderTimeline();
  renderRecordings();
  renderEvents();
  renderScreenshots();
  renderClips();
}

function renderTimeline() {
  timelineCoverageEl.replaceChildren();
  timelineMarkersEl.replaceChildren();
  if (!state.dayStart || !state.dayEnd) return;
  const span = state.dayEnd - state.dayStart;
  const fraction = (date) => Math.min(1, Math.max(0, (date - state.dayStart) / span));
  const coverageFragment = document.createDocumentFragment();
  state.segments.forEach((segment) => {
    const left = fraction(segment.startedAt) * 100;
    const width = Math.max(0.15, (fraction(segment.endedAt) - fraction(segment.startedAt)) * 100);
    const span2 = makeElement("span", segment.complete ? "" : "recording-now");
    span2.style.left = `${left}%`;
    span2.style.width = `${width}%`;
    coverageFragment.append(span2);
  });
  timelineCoverageEl.append(coverageFragment);

  const markersFragment = document.createDocumentFragment();
  state.events.forEach((event) => {
    const left = fraction(event.startedAt) * 100;
    const kind = event.person && event.vehicle ? "both" : event.person ? "person" : event.vehicle ? "vehicle" : "motion";
    const marker = makeElement("span", kind);
    marker.style.left = `${left}%`;
    markersFragment.append(marker);
  });
  timelineMarkersEl.append(markersFragment);
  renderClipSelection();
}

function timelineFraction(date) {
  if (!state.dayStart || !state.dayEnd) return 0;
  return Math.min(1, Math.max(0, (date - state.dayStart) / (state.dayEnd - state.dayStart)));
}

function updatePlayheadUI(date) {
  if (!date || date < state.dayStart || date > state.dayEnd) {
    timelinePlayheadEl.hidden = true;
    return;
  }
  timelinePlayheadEl.hidden = false;
  timelinePlayheadEl.style.left = `${timelineFraction(date) * 100}%`;
}

function renderClipSelection() {
  const { start, end } = state.clipMark;
  if (!start || !end) { timelineSelectionEl.hidden = true; return; }
  const lo = start < end ? start : end;
  const hi = start < end ? end : start;
  const left = timelineFraction(lo) * 100;
  const width = Math.max(0.15, (timelineFraction(hi) - timelineFraction(lo)) * 100);
  timelineSelectionEl.hidden = false;
  timelineSelectionEl.style.left = `${left}%`;
  timelineSelectionEl.style.width = `${width}%`;
}

timelineEl.addEventListener("click", (event) => {
  if (!state.dayStart || !state.dayEnd) return;
  const rect = timelineEl.getBoundingClientRect();
  const fraction = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  const target = new Date(state.dayStart.getTime() + fraction * (state.dayEnd - state.dayStart));
  setMode("playback");
  seekAbsolute(target, { autoplay: true });
});
timelineEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  const current = currentAbsoluteTime() || state.dayStart;
  seekAbsolute(current, { autoplay: true });
});

/* ------------------------------ player engine ------------------------------ */

function currentAbsoluteTime() {
  if (state.player.directSource || state.player.anchorAbsolute == null) return null;
  return new Date(state.player.anchorAbsolute.getTime() + (videoPlayer.currentTime || 0) * 1000);
}

function findSegmentForTime(target) {
  const playable = state.segments.filter((row) => row.complete).sort((a, b) => a.startedAt - b.startedAt);
  const covering = playable.filter((row) => target >= row.startedAt && target < row.endedAt);
  if (covering.length) {
    // Consecutive archive segments can overlap by about a second at their
    // boundary (the outgoing segment's real close time vs. the next one's
    // start). Picking the earlier one there re-seeks near the end of the
    // segment already loaded instead of advancing -- since that never
    // triggers a new load, neither loadedmetadata nor error ever fires,
    // which was a real, exactly-reproducible freeze at that timestamp.
    return covering.reduce((latest, row) => (row.startedAt > latest.startedAt ? row : latest));
  }
  const after = playable.filter((row) => row.startedAt > target).sort((a, b) => a.startedAt - b.startedAt)[0];
  if (after) return after;
  return playable[playable.length - 1] || null;
}

function nextSegmentAfter(segment) {
  const playable = state.segments.filter((row) => row.complete).sort((a, b) => a.startedAt - b.startedAt);
  const index = playable.findIndex((row) => row.id === segment.id);
  return index >= 0 ? playable[index + 1] || null : null;
}

function setPlayerEmpty(message) {
  playerEmpty.hidden = message === null;
  if (message) playerEmpty.textContent = message;
}

function prefetchSegment(segment) {
  // A cold segment can take real seconds to remux/transcode server-side.
  // Warming the next one in the background, as soon as the current one
  // starts, hides that behind normal playback time instead of stalling
  // the handoff at the boundary -- the earlier this starts, the more
  // margin it has, which matters most at high playback speeds.
  const segmentId = segment && positiveId(segment.id);
  if (segmentId === null || segmentId === undefined) return;
  if (state.player.prefetchedSegmentId === segmentId) return;
  state.player.prefetchedSegmentId = segmentId;
  fetch(`/dvr/api/segments/${segmentId}/video.mp4`, {
    credentials: "same-origin", cache: "no-store",
  }).catch(() => {});
}

function clearAdvanceWatchdog() {
  if (state.player.advanceWatchdog != null) {
    clearTimeout(state.player.advanceWatchdog);
    state.player.advanceWatchdog = null;
  }
}

function beginAdvancing() {
  state.player.advancing = true;
  clearAdvanceWatchdog();
  // Defense in depth: if neither loadedmetadata nor error ever fires (a
  // silently dropped connection, a backgrounded tab), advancing must not
  // stay stuck forever -- that previously required reloading the page to
  // recover, since it silently blocked every future auto-advance too.
  state.player.advanceWatchdog = setTimeout(() => {
    state.player.advancing = false;
    state.player.advanceWatchdog = null;
    setPlayerEmpty("The next recording is taking longer than expected. Try skip or the timeline.");
  }, 60000);
}

async function seekAbsolute(target, { autoplay = true } = {}) {
  const segment = findSegmentForTime(target);
  const segmentId = segment && positiveId(segment.id);
  if (!segment || segmentId === null) {
    setPlayerEmpty("No recording covers that time.");
    return;
  }
  state.player.directSource = false;
  const clamped = target < segment.startedAt
    ? segment.startedAt
    : target >= segment.endedAt
      ? new Date(segment.endedAt.getTime() - 500)
      : target;
  const offsetSeconds = Math.max(0, (clamped - segment.startedAt) / 1000);
  updatePlayheadUI(clamped);
  updatePlayerTimeUI(clamped);
  if (state.player.currentSegmentId !== segment.id) {
    setPlayerEmpty(null);
    state.player.currentSegmentId = segment.id;
    state.player.anchorAbsolute = segment.startedAt;
    state.player.pendingOffset = offsetSeconds;
    state.player.autoplayAfterLoad = autoplay;
    videoPlayer.src = `/dvr/api/segments/${segmentId}/video.mp4`;
    videoPlayer.load();
    prefetchSegment(nextSegmentAfter(segment));
  } else {
    videoPlayer.currentTime = offsetSeconds;
    if (autoplay) videoPlayer.play().catch(() => {});
    else videoPlayer.pause();
  }
}

function loadDirectSource(url, label) {
  const mediaUrl = sameOriginMediaUrl(url, DVR_MEDIA_PREFIXES);
  if (!mediaUrl) return;
  setMode("playback");
  setPlayerEmpty(null);
  state.player.directSource = true;
  state.player.currentSegmentId = null;
  state.player.anchorAbsolute = null;
  state.player.pendingOffset = null;
  state.player.autoplayAfterLoad = false;
  timelinePlayheadEl.hidden = true;
  playerTimeEl.textContent = label || "";
  videoPlayer.src = mediaUrl;
  videoPlayer.load();
  videoPlayer.play().catch(() => {});
}

function updatePlayerTimeUI(date) {
  playerTimeEl.textContent = date ? formatClock(date) : "—";
}

videoPlayer.addEventListener("loadedmetadata", () => {
  if (state.player.pendingOffset != null) {
    videoPlayer.currentTime = Math.min(state.player.pendingOffset, Math.max(0, videoPlayer.duration - 0.1));
    state.player.pendingOffset = null;
  }
  videoPlayer.playbackRate = state.player.speed;
  if (state.player.autoplayAfterLoad) {
    videoPlayer.play().catch(() => {});
    state.player.autoplayAfterLoad = false;
  }
  state.player.advancing = false;
  clearAdvanceWatchdog();
  updatePlayPauseUI();
});
videoPlayer.addEventListener("error", () => {
  // A failed load must never leave advancing stuck true -- that silently
  // disabled every future auto-advance until the page was reloaded, which
  // is exactly what made one slow/failed segment look like the whole
  // player had broken.
  state.player.advancing = false;
  clearAdvanceWatchdog();
  if (videoPlayer.getAttribute("src")) setPlayerEmpty("This recording could not be played.");
});
videoPlayer.addEventListener("play", updatePlayPauseUI);
videoPlayer.addEventListener("pause", updatePlayPauseUI);
videoPlayer.addEventListener("timeupdate", () => {
  const abs = currentAbsoluteTime();
  if (!abs) return;
  updatePlayerTimeUI(abs);
  updatePlayheadUI(abs);
  const segment = state.segments.find((row) => row.id === state.player.currentSegmentId);
  if (!segment || state.player.advancing || videoPlayer.paused) return;
  if (abs >= new Date(segment.endedAt.getTime() - 250)) {
    advanceToNextSegment(segment, abs);
  }
});
videoPlayer.addEventListener("ended", () => {
  const segment = state.segments.find((row) => row.id === state.player.currentSegmentId);
  if (!segment || state.player.advancing) return;
  advanceToNextSegment(segment, currentAbsoluteTime());
});

function advanceTarget(nextStartedAt, currentAbs) {
  // Consecutive segments can overlap by a second or two (see
  // findSegmentForTime); seeking to nextStartedAt unconditionally would
  // visibly rewind into content already shown whenever that overlap exists.
  // Continuing from wherever playback actually was (never earlier than the
  // next segment's start, so a real gap still jumps forward correctly) keeps
  // the boundary seamless either way.
  return currentAbs && currentAbs.getTime() > nextStartedAt.getTime() ? currentAbs : nextStartedAt;
}

function advanceToNextSegment(segment, currentAbs) {
  const next = nextSegmentAfter(segment);
  if (!next) return;
  const target = advanceTarget(next.startedAt, currentAbs);
  beginAdvancing();
  seekAbsolute(target, { autoplay: true });
}

function updatePlayPauseUI() {
  const button = $("#playPauseButton");
  const playing = !videoPlayer.paused && !videoPlayer.ended && videoPlayer.currentSrc;
  button.textContent = playing ? "⏸" : "▶";
  button.setAttribute("aria-label", playing ? "Pause" : "Play");
}

function skip(deltaSeconds) {
  const abs = currentAbsoluteTime();
  if (!abs) return;
  const autoplay = !videoPlayer.paused;
  seekAbsolute(new Date(abs.getTime() + deltaSeconds * 1000), { autoplay });
}

function jumpToEvent(direction) {
  if (!state.events.length) return;
  const current = currentAbsoluteTime();
  const sorted = state.events.slice().sort((a, b) => a.startedAt - b.startedAt);
  let target = null;
  if (direction === "next") {
    target = sorted.find((event) => !current || event.startedAt > current);
  } else {
    for (let index = sorted.length - 1; index >= 0; index -= 1) {
      if (!current || sorted[index].startedAt < current) { target = sorted[index]; break; }
    }
  }
  if (target) { setMode("playback"); seekAbsolute(target.startedAt, { autoplay: true }); }
}

$("#playPauseButton").addEventListener("click", () => {
  if (!videoPlayer.currentSrc) return;
  if (videoPlayer.paused) videoPlayer.play().catch(() => {});
  else videoPlayer.pause();
});
$("#back10Button").addEventListener("click", () => skip(-10));
$("#forward10Button").addEventListener("click", () => skip(10));
$("#back30Button").addEventListener("click", () => skip(-30));
$("#forward30Button").addEventListener("click", () => skip(30));
$("#prevEventButton").addEventListener("click", () => jumpToEvent("prev"));
$("#nextEventButton").addEventListener("click", () => jumpToEvent("next"));
$("#speedSelect").addEventListener("change", (event) => {
  state.player.speed = Number(event.target.value) || 1;
  videoPlayer.playbackRate = state.player.speed;
});
$("#fullscreenButton").addEventListener("click", () => {
  const target = state.mode === "live" ? liveStage : playbackStage;
  (target.requestFullscreen ? target : document.documentElement).requestFullscreen?.().catch(() => {});
});
document.addEventListener("keydown", (event) => {
  if (state.mode !== "playback" || !videoPlayer.currentSrc) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  // Frame-step (approximate, ~1/30s) only while paused -- a supplement to the
  // visible controls, not a replacement for them.
  if (videoPlayer.paused && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
    event.preventDefault();
    videoPlayer.currentTime = Math.max(0, videoPlayer.currentTime + (event.key === "ArrowRight" ? 1 : -1) / 30);
  } else if (event.key === " ") {
    event.preventDefault();
    if (videoPlayer.paused) videoPlayer.play().catch(() => {}); else videoPlayer.pause();
  }
});

/* ------------------------------ browsing lists ------------------------------ */

function appendTags(container, item) {
  if (item.person) container.append(makeElement("span", "tag person", "Person"));
  if (item.vehicle) container.append(makeElement("span", "tag vehicle", "Vehicle"));
  if (!container.childElementCount) container.append(makeElement("span", "tag", "Motion"));
}

function renderRecordings() {
  const items = state.segments;
  $("#recordingCount").textContent = `${items.length} segments`;
  const list = $("#recordingsList");
  if (!items.length) { showEmpty(list, "recording-list", "No recordings for this date."); return; }
  list.className = "recording-list";
  const fragment = document.createDocumentFragment();
  items.slice().sort((a, b) => b.startedAt - a.startedAt).forEach((item) => {
    const article = makeElement("article", "recording-row");
    const timing = makeElement("div", "recording-timing");
    timing.append(
      makeElement("div", "recording-time", `${formatTime(item.startedAt)} – ${formatTime(item.endedAt)}`),
      makeElement("div", "recording-meta", item.complete ? "Completed" : "Recording now"),
    );
    const dimensions = Number.isFinite(item.width) && Number.isFinite(item.height) ? `${item.width}×${item.height}` : "—×—";
    const profile = makeElement("div", "recording-meta recording-profile", item.probed ? `${dimensions} · ${item.codec}` : "bitstream metadata pending");
    const size = makeElement("div", "recording-meta recording-size", formatBytes(item.bytes));
    const button = makeElement("button", "play-button", "Play");
    button.type = "button";
    button.disabled = !item.complete;
    button.setAttribute("aria-label", `Play recording from ${formatTime(item.startedAt)}`);
    if (!button.disabled) {
      button.addEventListener("click", () => { setMode("playback"); seekAbsolute(item.startedAt, { autoplay: true }); });
    }
    article.append(timing, profile, size, button);
    fragment.append(article);
  });
  list.replaceChildren(fragment);
}

function renderEvents() {
  const items = state.events;
  $("#eventCount").textContent = `${items.length} events`;
  const list = $("#eventsList");
  if (!items.length) { showEmpty(list, "event-grid", "No motion events for this date."); return; }
  list.className = "event-grid";
  const fragment = document.createDocumentFragment();
  items.slice().sort((a, b) => b.startedAt - a.startedAt).forEach((item) => {
    const article = makeElement("article", "event-card");
    const snapshotUrl = sameOriginMediaUrl(item.snapshotUrl, SNAPSHOT_MEDIA_PREFIXES);
    if (snapshotUrl) {
      const thumbButton = makeElement("button", "event-thumb-button");
      thumbButton.type = "button";
      thumbButton.setAttribute("aria-label", `Enlarge motion snapshot from ${formatTime(item.startedAt)}`);
      const image = makeElement("img", "event-thumb");
      image.src = snapshotUrl;
      image.alt = "Motion event snapshot";
      image.loading = "lazy";
      thumbButton.append(image);
      thumbButton.addEventListener("click", () => openImage(snapshotUrl, "Motion event snapshot", formatTime(item.startedAt)));
      article.append(thumbButton);
    } else {
      article.append(makeElement("div", "event-thumb event-thumb-missing", "Snapshot unavailable"));
    }
    const body = makeElement("div", "event-body");
    const top = makeElement("div", "event-top");
    const frameCount = Number.isFinite(item.frameCount) ? Math.max(0, Math.trunc(item.frameCount)) : 0;
    top.append(
      makeElement("span", "event-time", `${formatTime(item.startedAt)} – ${formatTime(item.endedAt)}`),
      makeElement("span", "recording-meta", `${frameCount} frames`),
    );
    const caption = makeElement("div", "event-caption", item.caption || "Motion detected");
    const actions = makeElement("div", "event-top event-actions");
    const tags = makeElement("div", "tags");
    appendTags(tags, item);
    const play = makeElement("button", "play-button", "Play footage");
    play.type = "button";
    play.addEventListener("click", () => { setMode("playback"); seekAbsolute(item.startedAt, { autoplay: true }); });
    actions.append(tags, play);
    body.append(top, caption, actions);
    article.append(body);
    fragment.append(article);
  });
  list.replaceChildren(fragment);
}

function renderScreenshots() {
  const items = state.snapshots;
  $("#snapshotCount").textContent = `${items.length} images`;
  const list = $("#screenshotsList");
  if (!items.length) { showEmpty(list, "shot-grid", "No screenshots for this date."); return; }
  list.className = "shot-grid";
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const snapshotUrl = sameOriginMediaUrl(item.snapshot_url, SNAPSHOT_MEDIA_PREFIXES);
    const title = item.caption || (item.trigger === "motion" ? "Motion snapshot" : "Baseline snapshot");
    if (!snapshotUrl) {
      fragment.append(makeElement("div", "shot shot-unavailable", "Snapshot unavailable"));
      return;
    }
    const wrap = makeElement("div", "shot-wrap");
    const shot = makeElement("button", "shot");
    shot.type = "button";
    shot.setAttribute("aria-label", `Enlarge ${title} from ${formatTime(item.captured_at)}`);
    const image = makeElement("img");
    image.src = snapshotUrl;
    image.alt = "Exterior camera snapshot";
    image.loading = "lazy";
    const trigger = item.trigger === "motion" ? "motion" : "baseline";
    shot.append(image, makeElement("div", "shot-overlay", `${formatTime(item.captured_at)} · ${trigger}`));
    shot.addEventListener("click", () => openImage(snapshotUrl, title, formatTime(item.captured_at)));
    wrap.append(shot);
    const capturedAt = parseServerTime(item.captured_at);
    if (capturedAt) {
      const play = makeElement("button", "play-button shot-play", "Play footage");
      play.type = "button";
      play.addEventListener("click", () => { setMode("playback"); seekAbsolute(capturedAt, { autoplay: true }); });
      wrap.append(play);
    }
    fragment.append(wrap);
  });
  list.replaceChildren(fragment);
}

function renderClips() {
  const items = state.clips;
  $("#clipCount").textContent = `${items.length} clips`;
  const list = $("#clipsList");
  if (!items.length) { showEmpty(list, "recording-list", "No saved clips yet. Mark a start and end, then Save clip."); return; }
  list.className = "recording-list";
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const article = makeElement("article", "recording-row");
    const startedAt = parseServerTime(item.started_at);
    const endedAt = parseServerTime(item.ended_at);
    const timing = makeElement("div", "recording-timing");
    timing.append(
      makeElement("div", "recording-time", item.title || (startedAt ? formatClock(startedAt) : "Saved clip")),
      makeElement("div", "recording-meta", `${formatTime(startedAt)} – ${formatTime(endedAt)}`),
    );
    const dateMeta = makeElement("div", "recording-meta recording-profile", startedAt ? formatClock(startedAt) : "");
    const size = makeElement("div", "recording-meta recording-size", formatBytes(item.bytes));
    const actions = makeElement("div");
    const play = makeElement("button", "play-button", "Play");
    play.type = "button";
    play.addEventListener("click", () => loadDirectSource(item.video_url, item.title || formatClock(startedAt)));
    const del = makeElement("button", "play-button", "Delete");
    del.type = "button";
    del.style.marginLeft = "6px";
    del.addEventListener("click", async () => {
      const clipId = positiveId(item.id);
      if (clipId === null) return;
      if (!window.confirm("Delete this saved clip? This cannot be undone.")) return;
      try {
        await request(`/dvr/api/clips-saved/${clipId}`, { method: "DELETE" });
        state.clips = state.clips.filter((row) => row.id !== item.id);
        renderClips();
      } catch (error) {
        $("#clipMarkerStatus").textContent = error.message || "Clip could not be deleted.";
      }
    });
    actions.append(play, del);
    article.append(timing, dateMeta, size, actions);
    fragment.append(article);
  });
  list.replaceChildren(fragment);
}

/* ------------------------------ clip marking ------------------------------ */

function renderClipMarkerStatus() {
  const { start, end } = state.clipMark;
  const status = $("#clipMarkerStatus");
  $("#saveClipButton").disabled = !start || !end;
  if (!start && !end) { status.textContent = ""; return; }
  status.textContent = [
    start ? `Start ${formatClock(start)}` : "Start not set",
    end ? `End ${formatClock(end)}` : "End not set",
  ].join(" · ");
  renderClipSelection();
}

$("#markStartButton").addEventListener("click", () => {
  const abs = currentAbsoluteTime();
  if (!abs) { $("#clipMarkerStatus").textContent = "Play a recording first."; return; }
  state.clipMark.start = abs;
  renderClipMarkerStatus();
});
$("#markEndButton").addEventListener("click", () => {
  const abs = currentAbsoluteTime();
  if (!abs) { $("#clipMarkerStatus").textContent = "Play a recording first."; return; }
  state.clipMark.end = abs;
  renderClipMarkerStatus();
});
$("#saveClipButton").addEventListener("click", async () => {
  const { start, end } = state.clipMark;
  if (!start || !end) return;
  const since = start < end ? start : end;
  const until = start < end ? end : start;
  const button = $("#saveClipButton");
  button.disabled = true;
  $("#clipMarkerStatus").textContent = "Saving clip…";
  try {
    const title = $("#clipTitleInput").value.trim();
    await request("/dvr/api/clips/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ since: since.toISOString(), until: until.toISOString(), title: title || null }),
    });
    state.clipMark = { start: null, end: null };
    $("#clipTitleInput").value = "";
    $("#clipMarkerStatus").textContent = "Clip saved.";
    renderClipSelection();
    const clipsResp = await request("/dvr/api/clips-saved");
    state.clips = clipsResp.items || [];
    renderClips();
  } catch (error) {
    $("#clipMarkerStatus").textContent = error.message || "Clip could not be saved.";
  } finally {
    button.disabled = !(state.clipMark.start && state.clipMark.end);
  }
});

/* ------------------------------ viewer (screenshots) ------------------------------ */

function openImage(src, title, meta) {
  const mediaUrl = sameOriginMediaUrl(src, SNAPSHOT_MEDIA_PREFIXES);
  if (!mediaUrl) { $("#statusNote").textContent = "Snapshot URL was rejected."; return; }
  viewer.className = "viewer image-mode";
  $("#viewerTitle").textContent = title;
  $("#viewerMeta").textContent = meta || "";
  imageViewer.src = mediaUrl;
  if (!viewer.open) viewer.showModal();
}
function closeViewer() {
  if (viewer.open) viewer.close();
  imageViewer.removeAttribute("src");
}

/* ------------------------------ wiring ------------------------------ */

async function refresh() {
  const selectedDay = datePicker.value || localDateString();
  $("#refreshButton").disabled = true;
  try {
    await loadTimeline(selectedDay);
  } catch (error) {
    $("#statusNote").textContent = error.message || "DVR data could not be loaded.";
  } finally {
    $("#refreshButton").disabled = false;
  }
}

datePicker.value = localDateString();
datePicker.addEventListener("change", refresh);
$("#refreshButton").addEventListener("click", refresh);
$("#modeLiveButton").addEventListener("click", () => setMode("live"));
$("#modePlaybackButton").addEventListener("click", () => setMode("playback"));
$("#startLiveButton").addEventListener("click", startLiveWatch);
$("#stopLiveButton").addEventListener("click", () => stopLiveWatch());
liveFeed.addEventListener("load", () => { if (state.liveSession) renderLiveWatch("live"); });
liveFeed.addEventListener("error", () => {
  if (state.liveSession) {
    stopLiveWatch({ quiet: true }).finally(() => renderLiveWatch("error", "The live feed ended. You can start a new watch session."));
  }
});
$("#closeViewer").addEventListener("click", closeViewer);
viewer.addEventListener("click", (event) => { if (event.target === viewer) closeViewer(); });
viewer.addEventListener("cancel", closeViewer);
viewer.addEventListener("close", () => imageViewer.removeAttribute("src"));
window.addEventListener("pagehide", () => {
  state.leaving = true;
  state.liveStartController?.abort();
  stopLiveWatch({ keepalive: true, quiet: true });
});
window.addEventListener("pageshow", () => {
  state.leaving = false;
  if (!state.liveSession) renderLiveWatch("idle");
});
$$(".tab").forEach((tab) => tab.addEventListener("click", () => {
  $$(".tab").forEach((item) => item.classList.toggle("active", item === tab));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `${tab.dataset.view}View`));
}));

renderLiveWatch("idle");
setMode("playback");
setPlayerEmpty("Pick a time on the timeline, or a recording, event, or clip below.");
refresh();
setInterval(pollStatus, 10000);
