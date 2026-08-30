/*
 * Continuous X DVR playback adapter.
 *
 * app.js owns the operator UI.  This small layer changes only the timeline
 * media transport: selecting a time starts one fragmented-MP4 stream at that
 * absolute instant.  The DVR backend walks the underlying five-minute MKVs.
 * Consequently five-minute archive boundaries never replace <video>.src and
 * cannot reset, rewind, or strand the browser's auto-advance state.
 *
 * app.js is deliberately loaded as a classic script before this file so its
 * global lexical state/functions are shared here. Saved clips and Live View
 * keep their original direct-source behavior.
 */

function continuousCoveringSegment(target) {
  const playable = state.segments
    .filter((row) => row.complete && target >= row.startedAt && target < row.endedAt)
    .sort((a, b) => b.startedAt - a.startedAt);
  return playable[0] || null;
}

async function continuousSeekAbsolute(target, { autoplay = true } = {}) {
  if (!(target instanceof Date) || Number.isNaN(target.getTime())) return;

  // Deliberate operator navigation always wins over any stale transition state
  // left by the legacy segment handoff machinery.
  state.player.advancing = false;
  clearAdvanceWatchdog();

  let segment = continuousCoveringSegment(target);
  if (!segment) {
    // Preserve the existing convenient behavior for clicks immediately before
    // recorded coverage: move to the next completed recording, but never hide
    // a gap while continuous playback is already underway.
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
  playerLoading.hidden = false;

  state.player.directSource = false;
  state.player.currentSegmentId = segment.id;
  // The continuous endpoint starts its output exactly at `clamped`, so video
  // time zero maps directly to this absolute wall-clock instant.
  state.player.anchorAbsolute = clamped;
  state.player.pendingOffset = 0;
  state.player.autoplayAfterLoad = autoplay;
  state.player.prefetchedSegmentId = null;

  updatePlayheadUI(clamped);
  updatePlayerTimeUI(clamped);

  const params = new URLSearchParams({ start: clamped.toISOString() });
  // A fresh URL also guarantees a cancelled/previous streaming response can
  // never be reused as the source for a new human seek.
  params.set("request", String(Date.now()));
  videoPlayer.src = `/dvr/api/playback/continuous.mp4?${params.toString()}`;
  videoPlayer.load();
}

// Rebind the function used by timeline clicks, recording rows, event jumps,
// skip controls, and keyboard navigation. User-directed seeking starts a new
// continuous stream; ordinary playback never changes source at archive edges.
seekAbsolute = continuousSeekAbsolute;

advanceToNextSegment = function continuousAdvanceBookkeeping(_segment, currentAbs) {
  if (state.player.directSource || !(currentAbs instanceof Date)) return;
  const covering = continuousCoveringSegment(currentAbs);
  if (covering) {
    state.player.currentSegmentId = covering.id;
  }
  // Intentionally no source replacement. The fragmented MP4 already contains
  // the following physical recording. If there is a real archive gap, FFmpeg
  // ends this response and the normal `ended` state is truthful.
};

// Per-file prefetching was required only by the old source-swap architecture.
// Keep the binding harmless because app.js may still call it from code paths
// that are retained for historical/saved media compatibility.
prefetchSegment = function continuousPrefetchNoop() {};
