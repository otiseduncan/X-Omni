import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("service worker bypasses range and media requests before static caching", async () => {
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  assert.match(source, /x-omni-shell-2026-08-29-13/);
  assert.match(source, /request\.headers\.has\("range"\)/);
  assert.match(source, /request\.destination === "audio"/);
  assert.match(source, /request\.destination === "video"/);
  assert.match(source, /url\.pathname\.startsWith\("\/api\/"\)/);

  const rangeBypass = source.indexOf('request.headers.has("range")');
  const staticInterception = source.indexOf("event.respondWith(staticResponse(request))");
  assert.ok(rangeBypass >= 0 && rangeBypass < staticInterception);
});

test("service worker shows a notification on push and focuses/opens the app on click", async () => {
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  assert.match(source, /addEventListener\("push",/);
  assert.match(source, /self\.registration\.showNotification\(/);
  assert.match(source, /addEventListener\("notificationclick",/);
  assert.match(source, /event\.notification\.close\(\)/);
  assert.match(source, /clients\.openWindow/);

  const pushListener = source.indexOf('addEventListener("push"');
  const showNotification = source.indexOf("showNotification(");
  assert.ok(pushListener >= 0 && pushListener < showNotification);
});

test("the standalone app actively installs a fresh worker and reloads on activation", async () => {
  const source = await readFile(new URL("../src/main.jsx", import.meta.url), "utf8");
  assert.match(source, /register\("\/sw\.js", \{ updateViaCache: "none" \}\)/);
  assert.match(source, /registration\.update\(\)/);
  assert.match(source, /addEventListener\("controllerchange"/);
  assert.match(source, /reloadingForServiceWorker/);
  assert.match(source, /window\.location\.reload\(\)/);
});
