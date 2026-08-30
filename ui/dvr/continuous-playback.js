/*
 * Continuous X DVR playback adapter.
 *
 * app.js owns the operator UI. This layer changes only the timeline media
 * transport: selecting a time starts one fragmented-MP4 stream at that absolute
 * instant. The DVR backend walks the underlying five-minute MKVs, so physical
 * archive boundaries never replace <video>.src during ordinary playback.
 */

function continuousCoveringSegment(target) {
  const playable = state.segments
    .filter((row) => row.complete && target >= row.startedAt && target < row.endedAt)
    .sort((a, b) => b.startedAt - a.startedAt);
  return playable[0] || null;
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

  // Normal DVR playback should look like a DVR, not a file-preparation tool.
  // Keep the legacy diagnostic badge available to old direct-file paths, but
  // never flash "Loading next recording" for the continuous transport. The
  // video simply remains on its current/black frame until metadata is ready.
  playerLoading.hidden = true;

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
  videoPlayer.src = `/dvr/api/playback/continuous.mp4?${params.toString()}`;
  videoPlayer.load();
}

seekAbsolute = continuousSeekAbsolute;

advanceToNextSegment = function continuousAdvanceBookkeeping(_segment, currentAbs) {
  if (state.player.directSource || !(currentAbs instanceof Date)) return;
  const covering = continuousCoveringSegment(currentAbs);
  if (covering) state.player.currentSegmentId = covering.id;
  // No source replacement. FFmpeg already carries the physical next segment
  // inside this one browser response.
};

prefetchSegment = function continuousPrefetchNoop() {};

// Defense in depth: app.js retains the old loading element for historical
// direct-file playback. Continuous timeline playback never needs it.
videoPlayer.addEventListener("loadstart", () => {
  if (!state.player.directSource) playerLoading.hidden = true;
});
