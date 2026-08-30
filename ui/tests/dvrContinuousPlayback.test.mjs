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
  assert.match(js, /Intentionally no source replacement/);
  assert.match(js, /prefetchSegment = function continuousPrefetchNoop/);
});
