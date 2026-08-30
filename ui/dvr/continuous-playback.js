/*
 * Continuous X DVR playback adapter.
 *
 * app.js owns the operator UI. This layer changes only the timeline media
 * transport: selecting a time starts one fragmented-MP4 stream at that absolute
 * instant. The DVR backend walks the underlying five-minute MKVs, so physical
 * archive boundaries never replace <video>.src during ordinary playback.
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
  // Merely assigning a new src does not guarantee Chrome immediately closes
  // the old streaming HTTP response. Pause, detach, and load the empty element
  // first so the DVR service sees a real disconnect and can retire its FFmpeg
  // worker before a new seek starts.
  videoPlayer.pause();
  videoPlayer.removeAttribute("src");
  videoPlayer.load();
}

function armContinuousStallRecovery() {
  if (state.player.directSource || !videoPlayer.currentSrc) return;
  clearContinuousStallTimer();
  const generation = continuousRequestGeneration;
  continuousStallTimer = setTimeout(() => {
    if (generation !== continuousRequestGeneration || state.player.directSource) return;
    const when = currentAbsoluteTime();
    cancelContinuousMediaRequest();
    state.player.currentSegmentId = null;
    state.player.anchorAbsolute = null;
    state.player.pendingOffset = null;
    state.player.autoplayAfterLoad = false;
    timelinePlayheadEl.hidden = true;
    playerLoading.hidden = true;
    setPlayerEmpty(
      when
        ? `Playback stalled near ${formatClock(when)}. Pick another point on the timeline to continue.`
        : "Playback stalled. Pick another point on the timeline to continue."
    );
    updatePlayPauseUI();
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

  // Explicitly close the prior HTTP media request before opening another one.
  // This prevents stale FFmpeg workers from piling up when an operator scrubs
  // or seeks away from a damaged segment.
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

// Normal buffering should be invisible. A prolonged stall, however, must
// release the media request so the entire DVR console cannot become hostage to
// one bad recording.
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
window.addEventListener("beforeunload", cancelContinuousMediaRequest);
