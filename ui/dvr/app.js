const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = { segments: [], bursts: [], snapshots: [] };
const datePicker = $("#datePicker");
const viewer = $("#viewer");
const videoPlayer = $("#videoPlayer");
const imageViewer = $("#imageViewer");

function localDateString(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
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
  if (!value) return "—";
  const normalized = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

async function request(url) {
  const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
  if (response.status === 401) {
    window.location.href = "/";
    throw new Error("Sign in required");
  }
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function renderStatus(status) {
  const badge = $("#recordingBadge");
  badge.textContent = status.recording ? "● Recording" : "Recorder offline";
  badge.className = `badge ${status.recording ? "live" : "error"}`;
  const drive = status.drive || {};
  $("#storageText").textContent = Number.isFinite(drive.free_bytes)
    ? `${formatBytes(drive.free_bytes)} free of ${formatBytes(drive.total_bytes)}` : "E:\\ unavailable";
  const profile = status.profile;
  $("#profileText").textContent = profile
    ? `${profile.width}×${profile.height} · ${profile.codec}` : "Waiting for camera";
  $("#motionText").textContent = status.onvif_motion_events ? "Connected" : "Reconnecting";
  const usedPct = Number.isFinite(drive.used_bytes) && Number.isFinite(drive.total_bytes) && drive.total_bytes
    ? Math.max(0, Math.min(100, drive.used_bytes / drive.total_bytes * 100)) : 0;
  $("#storageFill").style.width = `${usedPct}%`;
  $("#statusNote").textContent = status.last_error
    ? status.last_error
    : "Continuous stream-copy recording · oldest completed footage is overwritten automatically when E:\\ fills.";
}

function renderRecordings(items) {
  state.segments = items;
  $("#recordingCount").textContent = `${items.length} segments`;
  const list = $("#recordingsList");
  if (!items.length) { list.className = "recording-list empty-state"; list.textContent = "No recordings for this date."; return; }
  list.className = "recording-list";
  list.innerHTML = items.map((item) => `
    <article class="recording-row">
      <div><div class="recording-time">${formatTime(item.started_at)} – ${formatTime(item.ended_at)}</div><div class="recording-meta">${item.complete ? "Completed" : "Recording now"}</div></div>
      <div class="recording-meta">${item.width || "—"}×${item.height || "—"} · ${item.codec || "video"}</div>
      <div class="recording-meta">${formatBytes(item.bytes)}</div>
      <button class="play-button" data-segment="${item.id}" ${item.complete ? "" : "disabled"}>Play</button>
    </article>`).join("");
  $$('[data-segment]').forEach((button) => button.addEventListener("click", () => {
    const item = items.find((row) => String(row.id) === button.dataset.segment);
    openVideo(`/dvr/api/segments/${button.dataset.segment}/video.mp4`, "Continuous recording", item ? `${formatTime(item.started_at)} – ${formatTime(item.ended_at)}` : "");
  }));
}

function tagsFor(item) {
  const tags = [];
  if (item.person_detected) tags.push('<span class="tag person">Person</span>');
  if (item.vehicle_detected) tags.push('<span class="tag vehicle">Vehicle</span>');
  if (!tags.length) tags.push('<span class="tag">Motion</span>');
  return tags.join("");
}

function renderEvents(items) {
  state.bursts = items;
  $("#eventCount").textContent = `${items.length} events`;
  const list = $("#eventsList");
  if (!items.length) { list.className = "event-grid empty-state"; list.textContent = "No motion events for this date."; return; }
  list.className = "event-grid";
  list.innerHTML = items.map((item) => `
    <article class="event-card">
      ${item.snapshot_url ? `<img class="event-thumb" src="${item.snapshot_url}" alt="Motion event snapshot" data-image="${item.snapshot_url}" />` : '<div class="event-thumb"></div>'}
      <div class="event-body">
        <div class="event-top"><span class="event-time">${formatTime(item.started_at)} – ${formatTime(item.ended_at)}</span><span class="recording-meta">${item.frame_count} frames</span></div>
        <div class="event-caption">${item.caption || "Motion detected"}</div>
        <div class="event-top"><div class="tags">${tagsFor(item)}</div><button class="play-button" data-burst="${item.burst_id}">Play footage</button></div>
      </div>
    </article>`).join("");
  $$('[data-burst]').forEach((button) => button.addEventListener("click", () => {
    const item = items.find((row) => String(row.burst_id) === button.dataset.burst);
    openVideo(`/dvr/api/events/${button.dataset.burst}/video.mp4`, "Motion footage", item ? item.caption || formatTime(item.started_at) : "");
  }));
  bindImages();
}

function renderScreenshots(items) {
  state.snapshots = items;
  $("#snapshotCount").textContent = `${items.length} images`;
  const list = $("#screenshotsList");
  if (!items.length) { list.className = "shot-grid empty-state"; list.textContent = "No screenshots for this date."; return; }
  list.className = "shot-grid";
  list.innerHTML = items.map((item) => `
    <article class="shot" data-image="${item.snapshot_url}" data-title="${item.caption || (item.trigger === "motion" ? "Motion snapshot" : "Baseline snapshot")}">
      <img src="${item.snapshot_url}" alt="Exterior camera snapshot" loading="lazy" />
      <div class="shot-overlay">${formatTime(item.captured_at)} · ${item.trigger}</div>
    </article>`).join("");
  bindImages();
}

function bindImages() {
  $$('[data-image]').forEach((element) => element.addEventListener("click", () => {
    openImage(element.dataset.image, element.dataset.title || "Camera snapshot", "");
  }));
}

function openVideo(src, title, meta) {
  viewer.className = "viewer video-mode";
  $("#viewerTitle").textContent = title;
  $("#viewerMeta").textContent = meta || "";
  imageViewer.removeAttribute("src");
  videoPlayer.src = src;
  viewer.showModal();
  videoPlayer.play().catch(() => {});
}

function openImage(src, title, meta) {
  viewer.className = "viewer image-mode";
  $("#viewerTitle").textContent = title;
  $("#viewerMeta").textContent = meta || "";
  videoPlayer.pause();
  videoPlayer.removeAttribute("src");
  imageViewer.src = src;
  viewer.showModal();
}

function closeViewer() {
  videoPlayer.pause();
  videoPlayer.removeAttribute("src");
  imageViewer.removeAttribute("src");
  viewer.close();
}

async function refresh() {
  const day = encodeURIComponent(datePicker.value || localDateString());
  $("#refreshButton").disabled = true;
  try {
    const [status, segments, events] = await Promise.all([
      request("/dvr/api/status"),
      request(`/dvr/api/segments?date=${day}`),
      request(`/dvr/api/events?date=${day}`),
    ]);
    renderStatus(status);
    renderRecordings(segments.items || []);
    renderEvents(events.bursts || []);
    renderScreenshots(events.snapshots || []);
  } catch (error) {
    $("#statusNote").textContent = error.message || "DVR data could not be loaded.";
  } finally {
    $("#refreshButton").disabled = false;
  }
}

datePicker.value = localDateString();
$("#refreshButton").addEventListener("click", refresh);
datePicker.addEventListener("change", refresh);
$("#closeViewer").addEventListener("click", closeViewer);
viewer.addEventListener("click", (event) => { if (event.target === viewer) closeViewer(); });
$$('.tab').forEach((tab) => tab.addEventListener("click", () => {
  $$('.tab').forEach((item) => item.classList.toggle("active", item === tab));
  $$('.view').forEach((view) => view.classList.toggle("active", view.id === `${tab.dataset.view}View`));
}));
refresh();
