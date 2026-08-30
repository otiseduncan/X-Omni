"use strict";

/* X DVR -- standalone operator GUI, backed entirely by MediaMTX.
 * Live view negotiates WebRTC (WHEP) directly against MediaMTX; historical
 * playback and clip export both come from MediaMTX's own Playback API
 * (server-side stitched, zero transcoding here). This file has no module
 * imports/exports -- it is loaded as a classic script.
 */

const SCRUB_WINDOW_SECONDS = 8 * 60;

const state = {
  mode: "playback",
  day: new Date(),
  status: null,
  recordings: [],
  events: [],
  activeView: "recordings",
  clip: null, // { since: Date, until: Date }
  markStart: null,
  markEnd: null,
  whep: null, // { pc, location }
};

const els = {};
for (const id of [
  "recordingBadge", "storageText", "profileText", "motionText", "storageFill", "statusNote",
  "modeLiveButton", "modePlaybackButton", "playerTime",
  "liveStage", "liveVideo", "livePlaceholder", "liveFeedBadge", "liveActions",
  "startLiveButton", "stopLiveButton", "liveWatchStatus",
  "playbackStage", "videoPlayer", "playerEmpty", "playerLoading", "playerControls",
  "playPauseButton", "back30Button", "back10Button", "prevEventButton", "nextEventButton",
  "forward10Button", "forward30Button", "speedSelect", "fullscreenButton",
  "datePicker", "refreshButton", "markStartButton", "markEndButton", "clipTitleInput",
  "saveClipButton", "clipMarkerStatus",
  "timeline", "timelineCoverage", "timelineMarkers", "timelineSelection", "timelinePlayhead",
  "recordingCount", "recordingsList", "eventCount", "eventsList", "clipCount", "clipsList",
  "viewer", "viewerTitle", "viewerMeta", "closeViewer", "imageViewer",
]) {
  els[id] = document.getElementById(id);
}

function pad2(n) { return String(n).padStart(2, "0"); }

function localDayBounds(date) {
  const start = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 0, 0, 0, 0);
  const end = new Date(start.getTime() + 24 * 3600 * 1000);
  return [start, end];
}

function formatDateInput(date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function formatClock(date) {
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function formatBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

async function request(url, options) {
  const response = await fetch(url, { ...options, credentials: "same-origin", cache: "no-store" });
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail || "";
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response;
}

/* ---------- status ---------- */

async function refreshStatus() {
  try {
    const status = await request("/dvr/api/status");
    state.status = status;
    renderStatus(status);
  } catch (err) {
    els.recordingBadge.textContent = "Status unavailable";
    els.recordingBadge.className = "badge error";
    els.statusNote.textContent = String(err.message || err);
  }
}

function renderStatus(status) {
  if (status.recording) {
    els.recordingBadge.textContent = "Recording";
    els.recordingBadge.className = "badge live";
  } else {
    els.recordingBadge.textContent = "Not recording";
    els.recordingBadge.className = "badge error";
  }
  const drive = status.drive || {};
  if (drive.total_bytes) {
    const used = drive.used_bytes || 0;
    const pct = Math.min(100, (used / drive.total_bytes) * 100);
    els.storageFill.style.width = `${pct.toFixed(1)}%`;
    els.storageText.textContent = `${formatBytes(drive.used_bytes)} / ${formatBytes(drive.total_bytes)}`;
  } else {
    els.storageText.textContent = "—";
  }
  els.profileText.textContent = status.camera_ready ? "Connected" : "Offline";
  if (status.last_motion_at) {
    const raw = String(status.last_motion_at).replace(" ", "T");
    const parsed = new Date(raw.endsWith("Z") ? raw : `${raw}Z`);
    els.motionText.textContent = Number.isNaN(parsed.getTime()) ? status.last_motion_at : parsed.toLocaleString();
  } else {
    els.motionText.textContent = "None recorded";
  }
  els.statusNote.textContent = status.last_error || "";
}

/* ---------- WHEP live view ---------- */

function waitIceGatheringComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const onChange = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", onChange);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", onChange);
    setTimeout(resolve, 3000);
  });
}

async function startLive() {
  if (!state.status || !state.status.whep_url) {
    els.liveWatchStatus.textContent = "Live view is not configured.";
    return;
  }
  els.startLiveButton.disabled = true;
  els.liveWatchStatus.textContent = "Connecting…";
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  const remoteStream = new MediaStream();
  pc.ontrack = (event) => {
    remoteStream.addTrack(event.track);
    els.liveVideo.srcObject = remoteStream;
    els.liveVideo.hidden = false;
    els.livePlaceholder.hidden = true;
    els.liveFeedBadge.hidden = false;
  };
  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIceGatheringComplete(pc);
    const response = await fetch(state.status.whep_url, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription.sdp,
    });
    if (!response.ok) throw new Error(`MediaMTX rejected the live-view offer (${response.status}).`);
    const answerSdp = await response.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    const location = response.headers.get("Location");
    state.whep = { pc, location: location ? new URL(location, state.status.whep_url).toString() : null };
    els.liveWatchStatus.textContent = "Live.";
    els.startLiveButton.hidden = true;
    els.stopLiveButton.hidden = false;
  } catch (err) {
    pc.close();
    els.liveWatchStatus.textContent = `Live view failed: ${err.message || err}`;
  } finally {
    els.startLiveButton.disabled = false;
  }
}

async function stopLive() {
  const whep = state.whep;
  state.whep = null;
  if (!whep) return;
  try {
    whep.pc.close();
  } catch {
    /* ignore */
  }
  if (whep.location) {
    try {
      await fetch(whep.location, { method: "DELETE" });
    } catch {
      /* best effort */
    }
  }
  els.liveVideo.hidden = true;
  els.liveVideo.srcObject = null;
  els.livePlaceholder.hidden = false;
  els.liveFeedBadge.hidden = true;
  els.startLiveButton.hidden = false;
  els.stopLiveButton.hidden = true;
  els.liveWatchStatus.textContent = "Start the feed when you want to watch outside. Continuous DVR recording stays independent.";
}

function setMode(mode) {
  state.mode = mode;
  const live = mode === "live";
  els.modeLiveButton.classList.toggle("active", live);
  els.modePlaybackButton.classList.toggle("active", !live);
  els.liveStage.hidden = !live;
  els.liveActions.hidden = !live;
  els.playbackStage.hidden = live;
  els.playerControls.style.display = live ? "none" : "";
  if (!live) stopLive();
}

/* ---------- playback ---------- */

function setLoading(isLoading) {
  els.playerLoading.hidden = !isLoading;
}

function loadClip(since, until, { autoplay = true, seekTo = null } = {}) {
  setLoading(true);
  els.playerEmpty.hidden = true;
  const url = `/dvr/api/clip?since=${encodeURIComponent(since.toISOString())}&until=${encodeURIComponent(until.toISOString())}`;
  els.videoPlayer.src = url;
  state.clip = { since, until };
  const onReady = () => {
    setLoading(false);
    if (seekTo != null) {
      els.videoPlayer.currentTime = Math.max(0, Math.min(seekTo, els.videoPlayer.duration || seekTo));
    }
    if (autoplay) els.videoPlayer.play().catch(() => {});
    els.videoPlayer.removeEventListener("loadedmetadata", onReady);
  };
  const onError = () => {
    setLoading(false);
    els.playerEmpty.hidden = false;
    els.playerEmpty.textContent = "That time range has no continuous recording.";
    els.videoPlayer.removeEventListener("error", onError);
  };
  els.videoPlayer.addEventListener("loadedmetadata", onReady, { once: true });
  els.videoPlayer.addEventListener("error", onError, { once: true });
  updatePlayhead();
}

function seekToInstant(target) {
  const [dayStart, dayEnd] = localDayBounds(state.day);
  let since = new Date(target.getTime() - (SCRUB_WINDOW_SECONDS / 2) * 1000);
  let until = new Date(target.getTime() + (SCRUB_WINDOW_SECONDS / 2) * 1000);
  if (since < dayStart) since = dayStart;
  if (until > dayEnd) until = dayEnd;
  const offset = (target.getTime() - since.getTime()) / 1000;
  loadClip(since, until, { seekTo: offset });
}

function currentAbsoluteTime() {
  if (!state.clip) return null;
  return new Date(state.clip.since.getTime() + els.videoPlayer.currentTime * 1000);
}

function updatePlayerTime() {
  const absolute = currentAbsoluteTime();
  els.playerTime.textContent = absolute ? absolute.toLocaleString() : "—";
}

/* ---------- timeline ---------- */

function renderTimeline() {
  const [dayStart, dayEnd] = localDayBounds(state.day);
  const totalMs = dayEnd - dayStart;
  els.timelineCoverage.innerHTML = "";
  els.timelineMarkers.innerHTML = "";

  for (const row of state.recordings) {
    const start = new Date(row.started_at);
    const end = new Date(row.ended_at);
    const left = Math.max(0, (start - dayStart) / totalMs) * 100;
    const width = Math.max(0.15, (Math.min(end, dayEnd) - Math.max(start, dayStart)) / totalMs) * 100;
    const span = document.createElement("span");
    span.style.left = `${left}%`;
    span.style.width = `${width}%`;
    els.timelineCoverage.append(span);
  }

  for (const event of state.events) {
    const capturedAt = event._capturedAt;
    if (!capturedAt) continue;
    const left = Math.max(0, Math.min(100, ((capturedAt - dayStart) / totalMs) * 100));
    const marker = document.createElement("span");
    const person = !!event.person_detected;
    const vehicle = !!event.vehicle_detected;
    marker.className = person && vehicle ? "both" : person ? "person" : vehicle ? "vehicle" : "motion";
    marker.style.left = `${left}%`;
    marker.title = capturedAt.toLocaleTimeString();
    marker.addEventListener("click", (ev) => {
      ev.stopPropagation();
      seekToInstant(capturedAt);
    });
    els.timelineMarkers.append(marker);
  }
  updatePlayhead();
}

function updatePlayhead() {
  const [dayStart, dayEnd] = localDayBounds(state.day);
  const totalMs = dayEnd - dayStart;
  const absolute = currentAbsoluteTime();
  if (!absolute || absolute < dayStart || absolute > dayEnd) {
    els.timelinePlayhead.hidden = true;
    return;
  }
  const left = ((absolute - dayStart) / totalMs) * 100;
  els.timelinePlayhead.style.left = `${left}%`;
  els.timelinePlayhead.hidden = false;
}

els.timeline.addEventListener("click", (ev) => {
  const rect = els.timeline.getBoundingClientRect();
  const fraction = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
  const [dayStart] = localDayBounds(state.day);
  const target = new Date(dayStart.getTime() + fraction * 24 * 3600 * 1000);
  seekToInstant(target);
});

/* ---------- recordings / events lists ---------- */

function makeElement(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

function renderRecordings() {
  els.recordingCount.textContent = state.recordings.length ? `${state.recordings.length}` : "";
  els.recordingsList.classList.toggle("empty-state", state.recordings.length === 0);
  els.recordingsList.innerHTML = "";
  if (!state.recordings.length) {
    els.recordingsList.textContent = "No recordings for this date.";
    return;
  }
  for (const row of state.recordings) {
    const start = new Date(row.started_at);
    const end = new Date(row.ended_at);
    const item = makeElement("div", "recording-row");
    item.append(makeElement("span", "recording-time", `${formatClock(start)} – ${formatClock(end)}`));
    item.append(makeElement("span", "recording-meta", `${Math.round(row.duration_seconds)}s`));
    item.append(makeElement("span", "recording-meta", row.complete ? "complete" : "in progress"));
    const button = makeElement("button", "play-button", "Play");
    button.type = "button";
    button.addEventListener("click", () => {
      setMode("playback");
      loadClip(start, end);
    });
    item.append(button);
    els.recordingsList.append(item);
  }
}

function renderEvents() {
  els.eventCount.textContent = state.events.length ? `${state.events.length}` : "";
  els.eventsList.classList.toggle("empty-state", state.events.length === 0);
  els.eventsList.innerHTML = "";
  if (!state.events.length) {
    els.eventsList.textContent = "No motion events for this date.";
    return;
  }
  for (const event of state.events) {
    const article = makeElement("div", "event-card");
    if (event.snapshot_url) {
      const thumbButton = makeElement("button", "event-thumb-button");
      thumbButton.type = "button";
      const image = makeElement("img", "event-thumb");
      image.src = event.snapshot_url;
      image.alt = "Motion snapshot";
      thumbButton.append(image);
      thumbButton.addEventListener("click", () => openImage(event.snapshot_url, "Motion snapshot", event._capturedAt ? formatClock(event._capturedAt) : ""));
      article.append(thumbButton);
    } else {
      article.append(makeElement("div", "event-thumb event-thumb-missing", "Snapshot unavailable"));
    }
    const body = makeElement("div", "event-body");
    const top = makeElement("div", "event-top");
    top.append(makeElement("span", "event-time", event._capturedAt ? formatClock(event._capturedAt) : "—"));
    const playButton = makeElement("button", "play-button", "Play");
    playButton.type = "button";
    playButton.addEventListener("click", async () => {
      setMode("playback");
      setLoading(true);
      try {
        const response = await request(`/dvr/api/events/${event.burst_id}/clip`);
        const blobUrl = URL.createObjectURL(await response.blob());
        els.videoPlayer.src = blobUrl;
        state.clip = null;
        els.playerEmpty.hidden = true;
        els.videoPlayer.play().catch(() => {});
      } catch (err) {
        els.playerEmpty.hidden = false;
        els.playerEmpty.textContent = String(err.message || err);
      } finally {
        setLoading(false);
      }
    });
    top.append(playButton);
    body.append(top);
    body.append(makeElement("div", "event-caption", event.caption || "No caption recorded."));
    const tags = makeElement("div", "tags");
    if (event.person_detected) tags.append(makeElement("span", "tag person", "Person"));
    if (event.vehicle_detected) tags.append(makeElement("span", "tag vehicle", "Vehicle"));
    body.append(tags);
    article.append(body);
    els.eventsList.append(article);
  }
}

function renderClipsSaved() {
  request("/dvr/api/clips-saved").then((payload) => {
    const items = payload.items || [];
    els.clipCount.textContent = items.length ? `${items.length}` : "";
    els.clipsList.classList.toggle("empty-state", items.length === 0);
    els.clipsList.innerHTML = "";
    if (!items.length) {
      els.clipsList.textContent = "No saved clips yet.";
      return;
    }
    for (const item of items) {
      const row = makeElement("div", "recording-row");
      row.append(makeElement("span", "recording-time", item.filename));
      row.append(makeElement("span", "recording-meta", formatBytes(item.bytes)));
      row.append(makeElement("span", "recording-meta", new Date(item.created_at).toLocaleString()));
      const playButton = makeElement("button", "play-button", "Play");
      playButton.type = "button";
      playButton.addEventListener("click", () => {
        setMode("playback");
        setLoading(true);
        els.playerEmpty.hidden = true;
        state.clip = null;
        els.videoPlayer.src = `/dvr/api/clips-saved/${encodeURIComponent(item.filename)}`;
        els.videoPlayer.addEventListener("loadedmetadata", () => setLoading(false), { once: true });
        els.videoPlayer.play().catch(() => {});
      });
      row.append(playButton);
      const deleteButton = makeElement("button", "play-button", "Delete");
      deleteButton.type = "button";
      deleteButton.addEventListener("click", async () => {
        if (!confirm(`Delete ${item.filename}?`)) return;
        await request(`/dvr/api/clips-saved/${encodeURIComponent(item.filename)}`, { method: "DELETE" });
        renderClipsSaved();
      });
      row.append(deleteButton);
      els.clipsList.append(row);
    }
  }).catch((err) => {
    els.clipsList.textContent = String(err.message || err);
  });
}

/* ---------- image viewer ---------- */

function openImage(src, title, meta) {
  els.viewerTitle.textContent = title;
  els.viewerMeta.textContent = meta || "";
  els.imageViewer.src = src;
  els.viewer.classList.add("image-mode");
  els.viewer.showModal();
}

els.closeViewer.addEventListener("click", () => els.viewer.close());

/* ---------- load day ---------- */

async function loadDay() {
  const [dayStart, dayEnd] = localDayBounds(state.day);
  const sinceParam = encodeURIComponent(dayStart.toISOString());
  const untilParam = encodeURIComponent(dayEnd.toISOString());
  try {
    const [recordingsPayload, eventsPayload] = await Promise.all([
      request(`/dvr/api/recordings?since=${sinceParam}&until=${untilParam}`),
      request(`/dvr/api/events?since=${sinceParam}&until=${untilParam}`),
    ]);
    state.recordings = recordingsPayload.items || [];
    state.events = (eventsPayload.items || []).map((row) => {
      const raw = String(row.captured_at || "").replace(" ", "T");
      const capturedAt = new Date(raw.endsWith("Z") ? raw : `${raw}Z`);
      return { ...row, _capturedAt: Number.isNaN(capturedAt.getTime()) ? null : capturedAt };
    });
  } catch (err) {
    state.recordings = [];
    state.events = [];
    els.recordingsList.textContent = String(err.message || err);
  }
  renderRecordings();
  renderEvents();
  renderTimeline();
}

/* ---------- clip marking / export ---------- */

function updateSaveClipEnabled() {
  els.saveClipButton.disabled = !(state.markStart && state.markEnd && state.markEnd > state.markStart);
}

els.markStartButton.addEventListener("click", () => {
  const absolute = currentAbsoluteTime();
  if (!absolute) return;
  state.markStart = absolute;
  els.clipMarkerStatus.textContent = `Start: ${absolute.toLocaleTimeString()}`;
  updateSaveClipEnabled();
});

els.markEndButton.addEventListener("click", () => {
  const absolute = currentAbsoluteTime();
  if (!absolute) return;
  state.markEnd = absolute;
  els.clipMarkerStatus.textContent = `${state.markStart ? `Start: ${state.markStart.toLocaleTimeString()} — ` : ""}End: ${absolute.toLocaleTimeString()}`;
  updateSaveClipEnabled();
});

els.saveClipButton.addEventListener("click", async () => {
  if (!state.markStart || !state.markEnd) return;
  els.saveClipButton.disabled = true;
  els.clipMarkerStatus.textContent = "Saving…";
  try {
    const payload = await request("/dvr/api/clips/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        since: state.markStart.toISOString(),
        until: state.markEnd.toISOString(),
        title: els.clipTitleInput.value.trim(),
      }),
    });
    els.clipMarkerStatus.textContent = `Saved ${payload.filename}.`;
    state.markStart = null;
    state.markEnd = null;
    els.clipTitleInput.value = "";
    updateSaveClipEnabled();
    if (state.activeView === "clips") renderClipsSaved();
  } catch (err) {
    els.clipMarkerStatus.textContent = String(err.message || err);
    updateSaveClipEnabled();
  }
});

/* ---------- player controls ---------- */

els.playPauseButton.addEventListener("click", () => {
  if (els.videoPlayer.paused) els.videoPlayer.play().catch(() => {});
  else els.videoPlayer.pause();
});
els.videoPlayer.addEventListener("play", () => { els.playPauseButton.textContent = "⏸"; });
els.videoPlayer.addEventListener("pause", () => { els.playPauseButton.textContent = "▶"; });
els.videoPlayer.addEventListener("timeupdate", () => { updatePlayerTime(); updatePlayhead(); });

function seekBy(deltaSeconds) {
  const duration = els.videoPlayer.duration || 0;
  const next = els.videoPlayer.currentTime + deltaSeconds;
  if ((next < 0 || next > duration) && state.clip) {
    const absolute = currentAbsoluteTime();
    if (absolute) {
      seekToInstant(new Date(absolute.getTime() + deltaSeconds * 1000));
      return;
    }
  }
  els.videoPlayer.currentTime = Math.max(0, Math.min(duration, next));
}
els.back30Button.addEventListener("click", () => seekBy(-30));
els.back10Button.addEventListener("click", () => seekBy(-10));
els.forward10Button.addEventListener("click", () => seekBy(10));
els.forward30Button.addEventListener("click", () => seekBy(30));

function jumpEvent(direction) {
  const absolute = currentAbsoluteTime();
  const candidates = state.events
    .filter((event) => event._capturedAt)
    .sort((a, b) => a._capturedAt - b._capturedAt);
  if (!candidates.length) return;
  let target = null;
  if (!absolute) {
    target = direction > 0 ? candidates[0] : candidates[candidates.length - 1];
  } else if (direction > 0) {
    target = candidates.find((event) => event._capturedAt > absolute) || null;
  } else {
    target = [...candidates].reverse().find((event) => event._capturedAt < absolute) || null;
  }
  if (target) seekToInstant(target._capturedAt);
}
els.prevEventButton.addEventListener("click", () => jumpEvent(-1));
els.nextEventButton.addEventListener("click", () => jumpEvent(1));

els.speedSelect.addEventListener("change", () => {
  els.videoPlayer.playbackRate = parseFloat(els.speedSelect.value) || 1;
});

els.fullscreenButton.addEventListener("click", () => {
  const target = state.mode === "live" ? els.liveStage : els.playbackStage;
  if (document.fullscreenElement) document.exitFullscreen();
  else target.requestFullscreen().catch(() => {});
});

/* ---------- mode / tabs / date ---------- */

els.modeLiveButton.addEventListener("click", () => setMode("live"));
els.modePlaybackButton.addEventListener("click", () => setMode("playback"));
els.startLiveButton.addEventListener("click", startLive);
els.stopLiveButton.addEventListener("click", stopLive);

for (const tab of document.querySelectorAll(".tabs .tab")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tabs .tab")) other.classList.remove("active");
    tab.classList.add("active");
    const view = tab.dataset.view;
    state.activeView = view;
    for (const section of document.querySelectorAll(".view")) section.classList.remove("active");
    document.getElementById(`${view}View`).classList.add("active");
    if (view === "clips") renderClipsSaved();
  });
}

els.datePicker.addEventListener("change", () => {
  const [year, month, day] = els.datePicker.value.split("-").map(Number);
  if (!year || !month || !day) return;
  state.day = new Date(year, month - 1, day);
  loadDay();
});

els.refreshButton.addEventListener("click", () => {
  refreshStatus();
  loadDay();
});

/* ---------- init ---------- */

(function init() {
  els.datePicker.value = formatDateInput(state.day);
  setMode("playback");
  refreshStatus();
  loadDay();
  setInterval(refreshStatus, 30000);
})();
