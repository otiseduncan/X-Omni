import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const dvrRoot = path.resolve(here, "../dvr");

async function source(name) {
  return fs.readFile(path.join(dvrRoot, name), "utf8");
}

test("standalone DVR uses continuous playback adapter after the base UI", async () => {
  const html = await source("index.html");
  assert.match(html, /<script src="\/dvr\/app\.js"><\/script>/);
  assert.match(html, /<script src="\/dvr\/continuous-playback\.js"><\/script>/);
  assert.ok(
    html.indexOf('/dvr/app.js') < html.indexOf('/dvr/continuous-playback.js'),
    "continuous adapter must load after app.js",
  );
  assert.doesNotMatch(html, /type="module" src="\/dvr\/app\.js"/);
});

test("continuous adapter replaces five-minute source swapping", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /seekAbsolute = continuousSeekAbsolute/);
  assert.match(js, /\/dvr\/api\/playback\/continuous\.mp4/);
  assert.match(js, /advanceToNextSegment = function continuousAdvanceBookkeeping/);
  assert.match(js, /prefetchSegment = function continuousPrefetchNoop/);
});

test("continuous playback does not flash segment-loading UI", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /playerLoading\.hidden = true/);
  assert.doesNotMatch(js, /playerLoading\.hidden = false/);
});

test("a new DVR seek explicitly tears down the prior media request", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /function cancelContinuousMediaRequest\(\)/);
  assert.match(js, /videoPlayer\.pause\(\)/);
  assert.match(js, /videoPlayer\.removeAttribute\("src"\)/);
  assert.match(js, /videoPlayer\.load\(\)/);
  assert.match(js, /cancelContinuousMediaRequest\(\)[\s\S]*videoPlayer\.src = `\/dvr\/api\/playback\/continuous\.mp4/);
});

test("a prolonged media stall releases playback without reloading the DVR page", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /CONTINUOUS_STALL_RECOVERY_MS = 15000/);
  assert.match(js, /function armContinuousStallRecovery\(\)/);
  assert.match(js, /addEventListener\("waiting", armContinuousStallRecovery\)/);
  assert.match(js, /addEventListener\("stalled", armContinuousStallRecovery\)/);
  assert.match(js, /Playback stalled near/);
  assert.doesNotMatch(js, /window\.location\.reload/);
});

test("Live View hard-stops historical playback before opening the camera", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /\/dvr\/api\/playback\/active/);
  assert.match(js, /method: "DELETE"/);
  assert.match(js, /mode === "live"[\s\S]*stopHistoricalPlaybackOnServer/);
  assert.match(js, /removeEventListener\("click", baseStartLiveWatch\)/);
  assert.match(js, /await stopHistoricalPlaybackOnServer\(\)/);
});

test("Live View clears an orphaned server camera session before a fresh start", async () => {
  const js = await source("continuous-playback.js");
  assert.match(js, /function resetOrphanedLiveSession\(\)/);
  assert.match(js, /\/dvr\/api\/live\/reset/);
  assert.match(js, /await resetOrphanedLiveSession\(\)[\s\S]*return baseStartLiveWatch\(\)/);
});

test("hard refresh never runs media load or cleanup fetch from beforeunload", async () => {
  const js = await source("continuous-playback.js");
  assert.doesNotMatch(js, /addEventListener\("beforeunload"/);
  assert.match(js, /addEventListener\("pagehide"/);
  assert.match(js, /AbortController/);
});
