/*
 * Continuous X DVR playback adapter.
 *
 * app.js owns the operator UI. This layer changes only timeline-media
 * transport and, critically, keeps historical playback isolated from Live
 * View. Switching to Live tears down the browser media request AND asks the
 * DVR service to terminate its archive-playback FFmpeg worker before opening
 * another RTSP camera session.
 */

const CONTINUOUS_STALL_RECOVERY_MS = 15000;
let continuousStallTimer = null;
let continuousRequestGeneration = 0;

function continuousCoveringSegment(target) {
  const playable = state.segments
    .filter((row) => row.complete && target >= row.startedAt && target < row.endedAt)
    .sort((a, b) => b.startedAt - a.startedAt);
  return playable[0] || null;
}

function clearContinuousStallTimer() {
  if (continuousStallTimer !== null) {
    clearTimeout(continuousStallTimer);
    continuousStallTimer = null;
  }
}

function cancelContinuousMediaRequest() {
  clearContinuousStallTimer();
  continuousRequestGeneration += 1;
  videoPlayer.pause();
  videoPlayer.removeAttribute("src");
  videoPlayer.load();
}

async function stopHistoricalPlaybackOnServer() {
  try {
    await fetch("/dvr/api/playback/active", {
      method: "DELETE",
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (_error) {
    // Browser teardown still closes the HTTP response. The backend also has a
    // no-progress watchdog, so a failed best-effort control request must never
    // prevent the operator from attempting Live View.
  }
}

function resetHistoricalPlayerState() {
  state.player.advancing = false;
  clearAdvanceWatchdog();
  state.player.currentSegmentId = null;
  state.player.anchorAbsolute = null;
  state.player.pendingOffset = null;
  state.player.autoplayAfterLoad = false;
  state.player.prefetchedSegmentId = null;
  timelinePlayheadEl.hidden = true;
  playerLoading.hidden = true;
  updatePlayPauseUI();
}

function armContinuousStallRecovery() {
  if (state.player.directSource || !videoPlayer.currentSrc) return;
  clearContinuousStallTimer();
  const generation = continuousRequestGeneration;
  continuousStallTimer = setTimeout(() => {
    if (generation !== continuousRequestGeneration || state.player.directSource) return;
    const when = currentAbsoluteTime();
    cancelContinuousMediaRequest();
    void stopHistoricalPlaybackOnServer();
    resetHistoricalPlayerState();
    setPlayerEmpty(
      when
        ? `Playback stalled near ${formatClock(when)}. Pick another point on the timeline to continue.`
        : "Playback stalled. Pick another point on the timeline to continue."
    );
  }, CONTINUOUS_STALL_RECOVERY_MS);
}

async function continuousSeekAbsolute(target, { autoplay = true } = {}) {
  if (!(target instanceof Date) || Number.isNaN(target.getTime())) return;

  state.player.advancing = false;
  clearAdvanceWatchdog();

  let segment = continuousCoveringSegment(target);
  if (!segment) {
    const after = state.segments
      .filter((row) => row.complete && row.startedAt > target)
      .sort((a, b) => a.startedAt - b.startedAt)[0];
    segment = after || null;
  }
  const segmentId = segment && positiveId(segment.id);
  if (!segment || segmentId === null) {
    setPlayerEmpty("No recording covers that time.");
    return;
  }

  const clamped = target < segment.startedAt ? segment.startedAt : target;
  setMode("playback");
  setPlayerEmpty(null);
  playerLoading.hidden = true;

  // Do not stack historical transcodes while scrubbing. Shut the browser side
  // immediately; the new backend playback request itself also supersedes any
  // older server worker through the single-owner playback guard.
  cancelContinuousMediaRequest();

  state.player.directSource = false;
  state.player.currentSegmentId = segment.id;
  state.player.anchorAbsolute = clamped;
  state.player.pendingOffset = 0;
  state.player.autoplayAfterLoad = autoplay;
  state.player.prefetchedSegmentId = null;

  updatePlayheadUI(clamped);
  updatePlayerTimeUI(clamped);

  const params = new URLSearchParams({ start: clamped.toISOString() });
  params.set("request", String(Date.now()));
  continuousRequestGeneration += 1;
  videoPlayer.src = `/dvr/api/playback/continuous.mp4?${params.toString()}`;
  videoPlayer.load();
}

seekAbsolute = continuousSeekAbsolute;

advanceToNextSegment = function continuousAdvanceBookkeeping(_segment, currentAbs) {
  if (state.player.directSource || !(currentAbs instanceof Date)) return;
  const covering = continuousCoveringSegment(currentAbs);
  if (covering) state.player.currentSegmentId = covering.id;
};

prefetchSegment = function continuousPrefetchNoop() {};

// setMode's Live tab is an immediate media-lifecycle boundary. The base UI only
// paused the hidden <video>, which left its HTTP/FFmpeg playback request alive.
const baseSetMode = setMode;
setMode = function isolatedDvrMode(mode) {
  if (mode === "live" && !state.player.directSource) {
    cancelContinuousMediaRequest();
    resetHistoricalPlayerState();
    void stopHistoricalPlaybackOnServer();
  }
  return baseSetMode(mode);
};

// app.js registered the original startLiveWatch function object directly as
// the button listener before this adapter loaded. Remove that exact reference
// and replace it with a guarded start that waits for archive playback teardown.
const baseStartLiveWatch = startLiveWatch;
const startLiveButton = $("#startLiveButton");
startLiveButton.removeEventListener("click", baseStartLiveWatch);
async function startLiveAfterPlaybackStops() {
  cancelContinuousMediaRequest();
  resetHistoricalPlayerState();
  await stopHistoricalPlaybackOnServer();
  return baseStartLiveWatch();
}
startLiveButton.addEventListener("click", startLiveAfterPlaybackStops);

videoPlayer.addEventListener("loadstart", () => {
  if (!state.player.directSource) playerLoading.hidden = true;
});
videoPlayer.addEventListener("waiting", armContinuousStallRecovery);
videoPlayer.addEventListener("stalled", armContinuousStallRecovery);
for (const eventName of ["playing", "canplay", "progress", "timeupdate", "loadeddata"]) {
  videoPlayer.addEventListener(eventName, () => {
    if (!state.player.directSource) clearContinuousStallTimer();
  });
}
videoPlayer.addEventListener("error", () => {
  if (!state.player.directSource) clearContinuousStallTimer();
});
window.addEventListener("beforeunload", () => {
  cancelContinuousMediaRequest();
  void stopHistoricalPlaybackOnServer();
});
