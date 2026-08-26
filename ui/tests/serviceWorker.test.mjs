import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("service worker bypasses range and media requests before static caching", async () => {
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  assert.match(source, /x-omni-shell-2026-08-26-12/);
  assert.match(source, /request\.headers\.has\("range"\)/);
  assert.match(source, /request\.destination === "audio"/);
  assert.match(source, /request\.destination === "video"/);
  assert.match(source, /url\.pathname\.startsWith\("\/api\/"\)/);

  const rangeBypass = source.indexOf('request.headers.has("range")');
  const staticInterception = source.indexOf("event.respondWith(staticResponse(request))");
  assert.ok(rangeBypass >= 0 && rangeBypass < staticInterception);
});

test("the standalone app actively installs a fresh worker and reloads on activation", async () => {
  const source = await readFile(new URL("../src/main.jsx", import.meta.url), "utf8");
  assert.match(source, /register\("\/sw\.js", \{ updateViaCache: "none" \}\)/);
  assert.match(source, /registration\.update\(\)/);
  assert.match(source, /addEventListener\("controllerchange"/);
  assert.match(source, /reloadingForServiceWorker/);
  assert.match(source, /window\.location\.reload\(\)/);
});
