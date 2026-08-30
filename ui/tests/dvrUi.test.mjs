import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function dvrSource(name) {
  return readFile(new URL(`../dvr/${name}`, import.meta.url), "utf8");
}

function extract(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `could not find ${JSON.stringify(startMarker)}`);
  const end = source.indexOf(endMarker, start);
  assert.ok(end > start, `could not find ${JSON.stringify(endMarker)} after ${JSON.stringify(startMarker)}`);
  return source.slice(start, end);
}

async function pureHelpers() {
  const source = await dvrSource("app.js");
  const body = extract(source, "function pad2", "/* ---------- status ---------- */");
  const sandbox = {};
  vm.runInNewContext(body, sandbox, { filename: "dvr-pure-helpers.js" });
  return sandbox;
}

test("localDayBounds returns midnight-to-midnight for the given local date, not UTC", async () => {
  const { localDayBounds } = await pureHelpers();
  const [start, end] = localDayBounds(new Date(2026, 7, 30, 14, 22, 9));
  assert.equal(start.getHours(), 0);
  assert.equal(start.getDate(), 30);
  assert.equal(end.getTime() - start.getTime(), 24 * 3600 * 1000);
});

test("formatDateInput zero-pads month and day for the date input control", async () => {
  const { formatDateInput } = await pureHelpers();
  assert.equal(formatDateInput(new Date(2026, 0, 5)), "2026-01-05");
});

test("formatBytes scales through binary units and handles null", async () => {
  const { formatBytes } = await pureHelpers();
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(5 * 1024 * 1024 * 1024), "5.0 GB");
});

test("app.js never assigns untrusted content through innerHTML, only clears with an empty string", async () => {
  const source = await dvrSource("app.js");
  const assignments = [...source.matchAll(/\.innerHTML\s*=\s*(.+?);/g)].map((m) => m[1].trim());
  assert.ok(assignments.length > 0, "expected at least one innerHTML clear in app.js");
  for (const rhs of assignments) {
    assert.equal(rhs, '""', `innerHTML must only ever be cleared, found: ${rhs}`);
  }
});

test("standalone DVR no longer loads the retired continuous-playback adapter", async () => {
  const html = await dvrSource("index.html");
  assert.doesNotMatch(html, /continuous-playback\.js/);
  assert.match(html, /<script src="\/dvr\/app\.js"><\/script>/);
});

test("live view uses a real <video> element for WHEP, not an <img> MJPEG stream", async () => {
  const html = await dvrSource("index.html");
  assert.match(html, /<video id="liveVideo"[^>]*autoplay[^>]*muted[^>]*playsinline[^>]*><\/video>/);
  assert.doesNotMatch(html, /id="liveFeed"/);
});

test("every element id app.js looks up by getElementById exists in index.html", async () => {
  const jsSource = await dvrSource("app.js");
  const html = await dvrSource("index.html");
  const idListMatch = jsSource.match(/for \(const id of \[([\s\S]*?)\]\) \{\s*\n\s*els\[id\]/);
  assert.ok(idListMatch, "expected app.js's els id-list initializer to be findable");
  const ids = [...idListMatch[1].matchAll(/"([A-Za-z0-9_]+)"/g)].map((m) => m[1]);
  assert.ok(ids.length > 20, "expected a substantial list of DOM ids");
  for (const id of ids) {
    assert.match(html, new RegExp(`id="${id}"`), `index.html is missing id="${id}" that app.js looks up`);
  }
});

test("WHEP negotiation posts an SDP offer and applies the returned SDP answer", async () => {
  const source = await dvrSource("app.js");
  const body = extract(source, "function waitIceGatheringComplete", "function setMode(mode) {");
  const posted = [];
  class FakeTransceiver {}
  class FakePeerConnection {
    constructor() {
      this.iceGatheringState = "complete";
      this.localDescription = { sdp: "v=0\r\n...offer..." };
    }
    addTransceiver() {}
    createOffer() { return Promise.resolve({}); }
    setLocalDescription() { return Promise.resolve(); }
    setRemoteDescription(desc) { this.remoteDescription = desc; return Promise.resolve(); }
    addEventListener() {}
    removeEventListener() {}
    close() {}
  }
  const els = {
    liveVideo: { hidden: true, srcObject: null },
    livePlaceholder: { hidden: false },
    liveFeedBadge: { hidden: true },
    liveWatchStatus: { textContent: "" },
    startLiveButton: { disabled: false, hidden: false },
    stopLiveButton: { hidden: true },
  };
  const sandbox = {
    els,
    state: { status: { whep_url: "http://127.0.0.1:8889/exterior_sub/whep" }, whep: null },
    RTCPeerConnection: FakePeerConnection,
    MediaStream: class { addTrack() {} },
    URL,
    setTimeout,
    fetch: async (url, options) => {
      posted.push({ url, options });
      return {
        ok: true,
        text: async () => "v=0\r\n...answer...",
        headers: { get: () => null },
      };
    },
    console,
  };
  vm.runInNewContext(body, sandbox, { filename: "dvr-whep.js" });
  await sandbox.startLive();

  assert.equal(posted.length, 1);
  assert.equal(posted[0].url, "http://127.0.0.1:8889/exterior_sub/whep");
  assert.equal(posted[0].options.method, "POST");
  assert.equal(posted[0].options.headers["Content-Type"], "application/sdp");
  assert.equal(els.liveWatchStatus.textContent, "Live.");
  assert.equal(els.startLiveButton.hidden, true);
  assert.equal(els.stopLiveButton.hidden, false);
});
