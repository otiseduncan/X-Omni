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

test("DVR media URLs are same-origin and constrained to owned route families", async () => {
  const validate = await mediaUrlValidator();
  const snapshots = ["/api/camera-snapshots/"];
  const videos = ["/dvr/api/", "/api/camera-clips/"];

  assert.equal(
    validate("/api/camera-snapshots/camera-motion-1.jpg", snapshots),
    "https://omega.example/api/camera-snapshots/camera-motion-1.jpg",
  );
  assert.equal(
    validate("/dvr/api/events/7/video.mp4#ignored", videos),
    "https://omega.example/dvr/api/events/7/video.mp4",
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
  assert.match(source, /sameOriginMediaUrl\(item\.snapshot_url, SNAPSHOT_MEDIA_PREFIXES\)/);
  assert.match(source, /positiveId\(item\.id\)/);
  assert.match(source, /positiveId\(item\.burst_id\)/);
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

test("standalone live watch is explicit, disconnectable, and cleaned on page exit", async () => {
  const [source, html, css] = await Promise.all([
    dvrSource("app.js"), dvrSource("index.html"), dvrSource("style.css"),
  ]);
  assert.match(html, /data-view="live"[^>]*>Live watch</);
  assert.match(html, /id="liveView"/);
  assert.match(html, /id="liveFeed"[^>]*hidden/);
  assert.match(html, /id="startLiveButton"[^>]*>Start live view</);
  assert.match(html, /id="stopLiveButton"[^>]*hidden>Disconnect \/ log out</);
  assert.match(source, /request\("\/dvr\/api\/live\/sessions", \{[\s\S]*method: "POST"[\s\S]*signal: controller\.signal/);
  assert.match(source, /liveFeed\.removeAttribute\("src"\)[\s\S]*method: "DELETE"/);
  assert.match(source, /pagehide[\s\S]*stopLiveWatch\(\{ keepalive: true, quiet: true \}\)/);
  assert.match(source, /state\.liveStartController\?\.abort\(\)/);
  assert.match(source, /state\.leaving \|\| state\.liveOperation !== operation[\s\S]*deleteLiveSession\(session, \{ keepalive: true \}\)/);
  assert.match(source, /liveFeed\.addEventListener\("error"[\s\S]*stopLiveWatch\(\{ quiet: true \}\)/);
  assert.doesNotMatch(source, /\nstartLiveWatch\(\);/);
  assert.match(css, /\.live-surface \{[^}]*aspect-ratio:16\/9/);
  assert.match(css, /\.live-surface img\[hidden\] \{ display:none; \}/);
  assert.match(css, /\.live-actions \.button \{ flex:1; \}/);
});

test("DVR status and viewer lifecycle expose the complete safe operator contract", async () => {
  const [source, html] = await Promise.all([dvrSource("app.js"), dvrSource("index.html")]);
  assert.match(source, /profile\.name/);
  assert.match(source, /Used \$\{formatBytes\(drive\.used_bytes\)\}/);
  assert.match(source, /Free \$\{formatBytes\(drive\.free_bytes\)\}/);
  assert.match(source, /Total \$\{formatBytes\(drive\.total_bytes\)\}/);
  assert.match(source, /viewer\.addEventListener\("cancel", cleanupViewerMedia\)/);
  assert.match(source, /viewer\.addEventListener\("close", cleanupViewerMedia\)/);
  assert.match(source, /window\.addEventListener\("pagehide", \(\) => \{[\s\S]*cleanupViewerMedia\(\)/);
  assert.match(source, /videoPlayer\.removeAttribute\("src"\)[\s\S]*videoPlayer\.load\(\)/);
  assert.match(html, /aria-labelledby="viewerTitle"/);
  assert.match(html, /aria-describedby="viewerMeta"/);
  assert.match(html, /Motion events with person and vehicle classification\./);
  assert.doesNotMatch(html, /ONVIF-triggered events/);
});

test("DVR phone layouts retain readable grids and 44px touch controls", async () => {
  const css = await dvrSource("style.css");
  assert.match(css, /@media \(max-width:430px\)/);
  assert.match(css, /\.date-control input \{[^}]*min-height:44px/);
  assert.match(css, /\.tab \{[^}]*min-height:44px/);
  assert.match(css, /\.play-button \{[^}]*min-height:44px/);
  assert.match(css, /\.icon-button \{[^}]*width:44px; height:44px/);
  assert.match(css, /\.button, \.tab, \.icon-button/);
  assert.match(css, /\.event-grid \{ grid-template-columns:minmax\(0,1fr\); \}/);
  assert.match(css, /\.shot-grid \{ grid-template-columns:repeat\(2,minmax\(0,1fr\)\); \}/);
  assert.match(css, /\.recording-row \.play-button \{[^}]*grid-row:2 \/ span 2/);
});
