import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function serviceWorkerFetchListener() {
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  const listeners = new Map();
  const sandbox = {
    URL,
    fetch: async () => {
      throw new Error("a network-only request must not be intercepted by the worker");
    },
    caches: {
      open: async () => ({ add: async () => {}, match: async () => null, put: async () => {} }),
      match: async () => null,
      keys: async () => [],
      delete: async () => true,
    },
    self: {
      location: { origin: "https://omega.example" },
      addEventListener: (name, handler) => listeners.set(name, handler),
      skipWaiting: async () => {},
      clients: { claim: async () => {}, matchAll: async () => [], openWindow: async () => {} },
      registration: { showNotification: async () => {} },
    },
  };
  vm.runInNewContext(source, sandbox, { filename: "sw.js" });
  return listeners.get("fetch");
}

test("service worker bypasses range and media requests before static caching", async () => {
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  assert.match(source, /x-omni-shell-2026-08-29-17/);
  assert.match(source, /request\.headers\.has\("range"\)/);
  assert.match(source, /request\.destination === "audio"/);
  assert.match(source, /request\.destination === "video"/);
  assert.match(source, /url\.pathname\.startsWith\("\/api\/"\)/);

  const rangeBypass = source.indexOf('request.headers.has("range")');
  const staticInterception = source.indexOf("event.respondWith(staticResponse(request))");
  assert.ok(rangeBypass >= 0 && rangeBypass < staticInterception);
});

test("service worker leaves every DVR path network-only", async () => {
  const listener = await serviceWorkerFetchListener();
  assert.equal(typeof listener, "function");

  for (const pathname of [
    "/dvr",
    "/dvr/",
    "/dvr/app.js",
    "/dvr/api/status",
    "/dvr/api/events/7/video.mp4",
  ]) {
    let intercepted = false;
    listener({
      request: {
        method: "GET",
        url: `https://omega.example${pathname}`,
        headers: { has: () => false },
        destination: "",
        mode: pathname === "/dvr" || pathname === "/dvr/" ? "navigate" : "cors",
      },
      respondWith: () => { intercepted = true; },
    });
    assert.equal(intercepted, false, `${pathname} must use the native network path`);
  }
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
