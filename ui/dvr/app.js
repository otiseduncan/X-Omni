const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const state = { segments: [], bursts: [], snapshots: [] };
const datePicker = $("#datePicker");
const viewer = $("#viewer");
const videoPlayer = $("#videoPlayer");
const imageViewer = $("#imageViewer");
const SNAPSHOT_MEDIA_PREFIXES = ["/api/camera-snapshots/"];
const DVR_MEDIA_PREFIXES = ["/dvr/api/", "/api/camera-clips/"];

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

function dayBounds(dayText) {
  const [year, month, day] = dayText.split("-").map(Number);
  const start = new Date(year, month - 1, day, 0, 0, 0);
  const end = new Date(year, month - 1, day + 1, 0, 0, 0);
  const sqliteUtc = (date) => date.toISOString().slice(0, 19).replace("T", " ");
  return { since: sqliteUtc(start), until: sqliteUtc(end) };
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
      advertised.name,
      "bitstream metadata pending",
      claim ? `advertised ${claim}` : null,
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

function renderRecordings(items) {
  state.segments = items;
  $("#recordingCount").textContent = `${items.length} segments`;
  const list = $("#recordingsList");
  if (!items.length) { showEmpty(list, "recording-list", "No recordings for this date."); return; }
  list.className = "recording-list";
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const article = makeElement("article", "recording-row");
    const timing = makeElement("div", "recording-timing");
    timing.append(
      makeElement("div", "recording-time", `${formatTime(item.started_at)} – ${formatTime(item.ended_at)}`),
      makeElement("div", "recording-meta", item.complete ? "Completed" : "Recording now"),
    );
    const dimensions = Number.isFinite(item.width) && Number.isFinite(item.height)
      ? `${item.width}×${item.height}` : "—×—";
    const profile = makeElement(
      "div", "recording-meta recording-profile",
      item.probed ? `${dimensions} · ${item.codec}` : "bitstream metadata pending",
    );
    const size = makeElement("div", "recording-meta recording-size", formatBytes(item.bytes));
    const button = makeElement("button", "play-button", "Play");
    button.type = "button";
    const segmentId = positiveId(item.id);
    button.disabled = !item.complete || segmentId === null;
    button.setAttribute("aria-label", `Play recording from ${formatTime(item.started_at)}`);
    if (!button.disabled) {
      button.addEventListener("click", () => openVideo(
        `/dvr/api/segments/${segmentId}/video.mp4`,
        "Continuous recording",
        `${formatTime(item.started_at)} – ${formatTime(item.ended_at)}`,
      ));
    }
    article.append(timing, profile, size, button);
    fragment.append(article);
  });
  list.replaceChildren(fragment);
}

function appendTags(container, item) {
  if (item.person_detected) container.append(makeElement("span", "tag person", "Person"));
  if (item.vehicle_detected) container.append(makeElement("span", "tag vehicle", "Vehicle"));
  if (!container.childElementCount) container.append(makeElement("span", "tag", "Motion"));
}

function renderEvents(items) {
  state.bursts = items;
  $("#eventCount").textContent = `${items.length} events`;
  const list = $("#eventsList");
  if (!items.length) { showEmpty(list, "event-grid", "No motion events for this date."); return; }
  list.className = "event-grid";
  const fragment = document.createDocumentFragment();
  items.forEach((item) => {
    const article = makeElement("article", "event-card");
    const snapshotUrl = sameOriginMediaUrl(item.snapshot_url, SNAPSHOT_MEDIA_PREFIXES);
    if (snapshotUrl) {
      const thumbButton = makeElement("button", "event-thumb-button");
      thumbButton.type = "button";
      thumbButton.setAttribute("aria-label", `Enlarge motion snapshot from ${formatTime(item.started_at)}`);
      const image = makeElement("img", "event-thumb");
      image.src = snapshotUrl;
      image.alt = "Motion event snapshot";
      image.loading = "lazy";
      thumbButton.append(image);
      thumbButton.addEventListener("click", () => openImage(snapshotUrl, "Motion event snapshot", formatTime(item.started_at)));
      article.append(thumbButton);
    } else {
      article.append(makeElement("div", "event-thumb event-thumb-missing", "Snapshot unavailable"));
    }

    const body = makeElement("div", "event-body");
    const top = makeElement("div", "event-top");
    const frameCount = Number.isFinite(item.frame_count) ? Math.max(0, Math.trunc(item.frame_count)) : 0;
    top.append(
      makeElement("span", "event-time", `${formatTime(item.started_at)} – ${formatTime(item.ended_at)}`),
      makeElement("span", "recording-meta", `${frameCount} frames`),
    );
    const caption = makeElement("div", "event-caption", item.caption || "Motion detected");
    const actions = makeElement("div", "event-top event-actions");
    const tags = makeElement("div", "tags");
    appendTags(tags, item);
    const play = makeElement("button", "play-button", "Play footage");
    play.type = "button";
    const burstId = positiveId(item.burst_id);
    play.disabled = burstId === null;
    if (!play.disabled) {
      play.addEventListener("click", () => openVideo(
        `/dvr/api/events/${burstId}/video.mp4`,
        "Motion footage",
        item.caption || formatTime(item.started_at),
      ));
    }
    actions.append(tags, play);
    body.append(top, caption, actions);
    article.append(body);
    fragment.append(article);
  });
  list.replaceChildren(fragment);
}

function renderScreenshots(items) {
  state.snapshots = items;
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
    fragment.append(shot);
  });
  list.replaceChildren(fragment);
}

function openVideo(src, title, meta) {
  const mediaUrl = sameOriginMediaUrl(src, DVR_MEDIA_PREFIXES);
  if (!mediaUrl) {
    $("#statusNote").textContent = "Playback URL was rejected.";
    return;
  }
  cleanupViewerMedia();
  viewer.className = "viewer video-mode";
  $("#viewerTitle").textContent = title;
  $("#viewerMeta").textContent = meta || "";
  videoPlayer.src = mediaUrl;
  if (!viewer.open) viewer.showModal();
  videoPlayer.play().catch(() => {});
}

function openImage(src, title, meta) {
  const mediaUrl = sameOriginMediaUrl(src, SNAPSHOT_MEDIA_PREFIXES);
  if (!mediaUrl) {
    $("#statusNote").textContent = "Snapshot URL was rejected.";
    return;
  }
  cleanupViewerMedia();
  viewer.className = "viewer image-mode";
  $("#viewerTitle").textContent = title;
  $("#viewerMeta").textContent = meta || "";
  imageViewer.src = mediaUrl;
  if (!viewer.open) viewer.showModal();
}

function cleanupViewerMedia() {
  videoPlayer.pause();
  videoPlayer.removeAttribute("src");
  videoPlayer.load();
  imageViewer.removeAttribute("src");
}

function closeViewer() {
  if (viewer.open) viewer.close();
  else cleanupViewerMedia();
}

async function refresh() {
  const selectedDay = datePicker.value || localDateString();
  $("#refreshButton").disabled = true;
  try {
    const day = encodeURIComponent(selectedDay);
    const bounds = dayBounds(selectedDay);
    const eventQuery = new URLSearchParams(bounds).toString();
    const [status, segments, events] = await Promise.all([
      request("/dvr/api/status"),
      request(`/dvr/api/segments?date=${day}`),
      request(`/dvr/api/events?${eventQuery}`),
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
viewer.addEventListener("cancel", cleanupViewerMedia);
viewer.addEventListener("close", cleanupViewerMedia);
videoPlayer.addEventListener("error", () => {
  if (videoPlayer.getAttribute("src")) $("#viewerMeta").textContent = "Playback could not be loaded.";
});
window.addEventListener("pagehide", cleanupViewerMedia);
$$('.tab').forEach((tab) => tab.addEventListener("click", () => {
  $$('.tab').forEach((item) => item.classList.toggle("active", item === tab));
  $$('.view').forEach((view) => view.classList.toggle("active", view.id === `${tab.dataset.view}View`));
}));
refresh();
