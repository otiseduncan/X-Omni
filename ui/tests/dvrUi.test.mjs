import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function dvrSource(name) {
  return readFile(new URL(`../dvr/${name}`, import.meta.url), "utf8");
}

async function mediaUrlValidator() {
  const source = await dvrSource("app.js");
  const start = source.indexOf("function sameOriginMediaUrl");
  const end = source.indexOf("\n}\n\nfunction showEmpty", start);
  assert.ok(start >= 0 && end > start, "same-origin validator must remain independently testable");
  const sandbox = {
    URL,
    window: {
      location: {
        href: "https://omega.example/dvr",
        origin: "https://omega.example",
      },
    },
  };
  vm.runInNewContext(source.slice(start, end + 2), sandbox, { filename: "dvr-media-url.js" });
  return sandbox.sameOriginMediaUrl;
}

async function liveSessionValidator() {
  const source = await dvrSource("app.js");
  const start = source.indexOf("function sameOriginMediaUrl");
  const end = source.indexOf("\n}\n\nfunction showEmpty", start);
  assert.ok(start >= 0 && end > start, "live-session validator must remain independently testable");
  const sandbox = {
    URL,
    encodeURIComponent,
    LIVE_MEDIA_PREFIXES: ["/dvr/api/live/sessions/"],
    window: {
      location: {
        href: "https://omega.example/dvr",
        origin: "https://omega.example",
      },
    },
  };
  vm.runInNewContext(source.slice(start, end + 2), sandbox, { filename: "dvr-live-session.js" });
  return sandbox.safeLiveSession;
}

async function segmentResolver() {
  const source = await dvrSource("app.js");
  const start = source.indexOf("function findSegmentForTime");
  const end = source.indexOf("\n}\n\nfunction nextSegmentAfter", start);
  assert.ok(start >= 0 && end > start, "segment resolver must remain independently testable");
  const sandbox = { state: { segments: [] } };
  vm.runInNewContext(source.slice(start, end + 2), sandbox, { filename: "dvr-segment-resolver.js" });
  return sandbox;
}

test("an overlapping segment boundary resolves forward, not back into the segment already playing", async () => {
  // Real archive data: consecutive segments can overlap by about a second
  // (the outgoing segment's real close time vs. the next one's start).
  // Picking the earlier segment at that exact instant re-seeks near the end
  // of the segment already loaded -- which never fires loadedmetadata or
  // error, so the auto-advance guard never clears. This reproduced an
  // exact, deterministic freeze at that timestamp on real hardware.
  const sandbox = await segmentResolver();
  const outgoing = {
    id: 379, complete: true,
    startedAt: new Date("2026-08-30T06:42:57Z"), endedAt: new Date("2026-08-30T06:47:58Z"),
  };
  const incoming = {
    id: 389, complete: true,
    startedAt: new Date("2026-08-30T06:47:57Z"), endedAt: new Date("2026-08-30T06:52:59Z"),
  };
  sandbox.state.segments = [outgoing, incoming];

  const resolved = sandbox.findSegmentForTime(incoming.startedAt);
  assert.equal(resolved.id, incoming.id);

  // Ordinary (non-overlapping) lookups are unaffected.
  assert.equal(sandbox.findSegmentForTime(new Date("2026-08-30T06:44:00Z")).id, outgoing.id);
  assert.equal(sandbox.findSegmentForTime(new Date("2026-08-30T06:50:00Z")).id, incoming.id);
});

async function advanceTargetFn() {
  const source = await dvrSource("app.js");
  const start = source.indexOf("function advanceTarget");
  const end = source.indexOf("\n}\n\nfunction advanceToNextSegment", start);
  assert.ok(start >= 0 && end > start, "advance-target picker must remain independently testable");
  const sandbox = {};
  vm.runInNewContext(source.slice(start, end + 2), sandbox, { filename: "dvr-advance-target.js" });
  return sandbox.advanceTarget;
}

test("auto-advance across an overlap continues from where playback was, not a visible rewind", async () => {
  // Reproduced live: it cleared the first (freeze) boundary, ran on at
  // speed, then visibly jumped backward at the *next* boundary -- landing
  // on the next segment's nominal start even though playback, at speed,
  // had already reached a moment at or after that start (the two segments
  // overlap there too). The seek target must never be earlier than where
  // playback actually already was.
  const advanceTarget = await advanceTargetFn();
  const nextStartedAt = new Date("2026-08-30T06:52:57Z");

  // Overlap: current position is already past the next segment's start.
  const pastStart = new Date("2026-08-30T06:52:58Z");
  assert.equal(advanceTarget(nextStartedAt, pastStart).getTime(), pastStart.getTime());

  // Gap: current position has not yet reached the next segment's start.
  const beforeStart = new Date("2026-08-30T06:52:55Z");
  assert.equal(advanceTarget(nextStartedAt, beforeStart).getTime(), nextStartedAt.getTime());

  // No known current position (e.g. a direct-source clip was playing).
  assert.equal(advanceTarget(nextStartedAt, null).getTime(), nextStartedAt.getTime());
});

test("DVR media URLs are same-origin and constrained to owned route families", async () => {
  const validate = await mediaUrlValidator();
  const snapshots = ["/api/camera-snapshots/"];
  const videos = ["/dvr/api/"];

  assert.equal(
    validate("/api/camera-snapshots/camera-motion-1.jpg", snapshots),
    "https://omega.example/api/camera-snapshots/camera-motion-1.jpg",
  );
  assert.equal(
    validate("/dvr/api/segments/7/video.mp4#ignored", videos),
    "https://omega.example/dvr/api/segments/7/video.mp4",
  );
  assert.equal(validate("https://attacker.example/image.jpg", snapshots), null);
  assert.equal(validate("//attacker.example/image.jpg", snapshots), null);
  assert.equal(validate("javascript:alert(1)", snapshots), null);
  assert.equal(validate("https://user:secret@omega.example/dvr/api/status", videos), null);
  assert.equal(validate("/api/generated-images/unowned.png", snapshots), null);
});

test("DVR renders API data through DOM text boundaries without innerHTML", async () => {
  const source = await dvrSource("app.js");
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
  assert.doesNotMatch(source, /bindImages/);
  assert.match(source, /document\.createElement\(/);
  assert.match(source, /\.textContent\s*=/);
  assert.match(source, /\.replaceChildren\(/);
  assert.match(source, /sameOriginMediaUrl\(item\.snapshotUrl, SNAPSHOT_MEDIA_PREFIXES\)/);
  assert.match(source, /positiveId\(segment\.id\)/);
  assert.match(source, /positiveId\(item\.id\)/);
});

test("standalone live watch accepts only its exact opaque same-origin stream", async () => {
  const validate = await liveSessionValidator();
  assert.deepEqual(
    { ...validate({
      session_id: "watch_session_12345678",
      stream_url: "/dvr/api/live/sessions/watch_session_12345678/stream.mjpg",
      label: "Driveway",
    }) },
    {
      session_id: "watch_session_12345678",
      stream_url: "https://omega.example/dvr/api/live/sessions/watch_session_12345678/stream.mjpg",
      label: "Driveway",
    },
  );
  assert.equal(validate({
    session_id: "short",
    stream_url: "/dvr/api/live/sessions/short/stream.mjpg",
  }), null);
  assert.equal(validate({
    session_id: "watch_session_12345678",
    stream_url: "/dvr/api/live/sessions/different_session/stream.mjpg",
  }), null);
  assert.equal(validate({
    session_id: "watch_session_12345678",
    stream_url: "https://attacker.example/dvr/api/live/sessions/watch_session_12345678/stream.mjpg",
  }), null);
  assert.equal(validate({
    session_id: "watch_session_12345678",
    stream_url: "/api/cameras/exterior/sessions/watch_session_12345678/stream.mjpg",
  }), null);
});

test("standalone live watch is a player mode, explicit, disconnectable, and cleaned on page exit", async () => {
  const [source, html] = await Promise.all([dvrSource("app.js"), dvrSource("index.html")]);
  assert.match(html, /id="modeLiveButton"[^>]*>Live</);
  assert.match(html, /id="modePlaybackButton"[^>]* class="mode-tab active"[^>]*>Playback</);
  assert.match(html, /id="liveFeed"[^>]*hidden/);
  assert.match(html, /id="startLiveButton"[^>]*>Start live view</);
  assert.match(html, /id="stopLiveButton"[^>]*hidden>Disconnect \/ log out</);
  assert.match(source, /request\("\/dvr\/api\/live\/sessions", \{ method: "POST", signal: controller\.signal \}\)/);
  assert.match(source, /liveFeed\.removeAttribute\("src"\)[\s\S]*method: "DELETE"/);
  assert.match(source, /pagehide[\s\S]*stopLiveWatch\(\{ keepalive: true, quiet: true \}\)/);
  assert.match(source, /state\.liveStartController\?\.abort\(\)/);
  assert.match(source, /state\.leaving \|\| state\.liveOperation !== operation[\s\S]*deleteLiveSession\(session, \{ keepalive: true \}\)/);
  assert.match(source, /liveFeed\.addEventListener\("error"[\s\S]*stopLiveWatch\(\{ quiet: true \}\)/);
  assert.doesNotMatch(source, /\nstartLiveWatch\(\);/);
  // Switching to live pauses playback; switching away stops any open session.
  assert.match(source, /function setMode\(mode\)[\s\S]*videoPlayer\.pause\(\)[\s\S]*stopLiveWatch\(\{ quiet: true \}\)/);
});

test("DVR status and viewer lifecycle expose the complete safe operator contract", async () => {
  const [source, html] = await Promise.all([dvrSource("app.js"), dvrSource("index.html")]);
  assert.match(source, /profile\.name/);
  assert.match(source, /Used \$\{formatBytes\(drive\.used_bytes\)\}/);
  assert.match(source, /Free \$\{formatBytes\(drive\.free_bytes\)\}/);
  assert.match(source, /Total \$\{formatBytes\(drive\.total_bytes\)\}/);
  assert.match(source, /viewer\.addEventListener\("cancel", closeViewer\)/);
  assert.match(source, /viewer\.addEventListener\("close", \(\) => imageViewer\.removeAttribute\("src"\)\)/);
  assert.match(html, /aria-labelledby="viewerTitle"/);
  assert.match(html, /aria-describedby="viewerMeta"/);
  assert.match(html, /Motion events with person and vehicle classification\./);
});

test("continuous timeline seeks by absolute time and hands off across segment boundaries", async () => {
  const [source, html] = await Promise.all([dvrSource("app.js"), dvrSource("index.html")]);
  assert.match(source, /function findSegmentForTime\(target\)/);
  assert.match(source, /async function seekAbsolute\(target/);
  assert.match(source, /function nextSegmentAfter\(segment\)/);
  // Auto-advance is guarded so a rapid loadedmetadata race cannot double-fire it.
  assert.match(source, /state\.player\.advancing/);
  assert.match(source, /videoPlayer\.addEventListener\("timeupdate"/);
  assert.match(source, /videoPlayer\.addEventListener\("ended"/);
  // A cold segment can take real seconds to prepare server-side; the next
  // one is pre-fetched during current playback so the boundary handoff
  // doesn't stall on it, and a failed/never-completing load must not leave
  // advancing stuck true forever (that previously required a page reload).
  assert.match(source, /function prefetchSegment\(segment\)/);
  assert.match(source, /prefetchSegment\(nextSegmentAfter\(segment\)\)/);
  assert.match(source, /function beginAdvancing\(\)/);
  assert.match(source, /videoPlayer\.addEventListener\("error", \(\) => \{[\s\S]*state\.player\.advancing = false/);
  // Auto-advance passes the already-known next segment directly instead of
  // re-deriving it from ambiguous boundary time math, and a manual seek
  // always clears any (possibly stuck) advancing flag rather than being
  // silently ignored by it.
  assert.match(source, /segmentHint: next, isAutoAdvance: true/);
  assert.match(source, /if \(!isAutoAdvance\) \{[\s\S]*state\.player\.advancing = false/);
  // A visible loading indicator distinguishes "preparing a cold segment" from
  // "broken", since a human can't tell those apart otherwise.
  assert.match(html, /id="playerLoading"[^>]*hidden/);
  assert.match(source, /playerLoading\.hidden = false/);
  assert.match(source, /playerLoading\.hidden = true/);
  // Required visible playback controls (spec: 10s/30s skip, prev/next event,
  // 0.5x-20x speed, fullscreen) -- keyboard shortcuts only supplement these.
  for (const id of [
    "playPauseButton", "back10Button", "forward10Button", "back30Button",
    "forward30Button", "prevEventButton", "nextEventButton", "speedSelect",
    "fullscreenButton",
  ]) {
    assert.match(source, new RegExp(`\\$\\("#${id}"\\)`), `${id} must be wired`);
  }
  for (const speed of ["0.5", "1", "2", "4", "8", "10", "15", "20"]) {
    assert.match(html, new RegExp(`<option value="${speed}"`));
  }
});

test("saved clips are marked, exported, and explicitly deleted -- never auto-pruned client-side", async () => {
  const [source, html] = await Promise.all([dvrSource("app.js"), dvrSource("index.html")]);
  assert.match(html, /id="markStartButton"/);
  assert.match(html, /id="markEndButton"/);
  assert.match(html, /id="saveClipButton"[^>]*disabled/);
  assert.match(source, /request\("\/dvr\/api\/clips\/export", \{/);
  assert.match(source, /method: "DELETE"/);
  assert.match(source, /window\.confirm\("Delete this saved clip\? This cannot be undone\."\)/);
});

test("DVR phone layouts retain readable grids and 44px touch controls", async () => {
  const css = await dvrSource("style.css");
  assert.match(css, /@media \(max-width:430px\)/);
  assert.match(css, /\.date-control input \{[^}]*min-height:44px/);
  assert.match(css, /\.tab \{[^}]*min-height:44px/);
  assert.match(css, /\.play-button \{[^}]*min-height:44px/);
  assert.match(css, /\.icon-button \{[^}]*width:44px; height:44px/);
  assert.match(css, /\.icon-btn \{[^}]*min-height:40px/);
  assert.match(css, /\.button, \.tab, \.icon-button/);
  assert.match(css, /\.event-grid \{ grid-template-columns:minmax\(0,1fr\); \}/);
  assert.match(css, /\.shot-grid \{ grid-template-columns:repeat\(2,minmax\(0,1fr\)\); \}/);
  assert.match(css, /\.recording-row \.play-button \{[^}]*grid-row:2 \/ span 2/);
});
