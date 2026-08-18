const CACHE_NAME = "x-omni-shell-2026-08-16-11";
const SHELL_URLS = [
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
];

async function installShell() {
  const cache = await caches.open(CACHE_NAME);
  const indexResponse = await fetch(new Request("/", { cache: "reload" }));
  if (!indexResponse.ok) throw new Error(`app shell HTTP ${indexResponse.status}`);
  await cache.put("/", indexResponse.clone());

  const html = await indexResponse.text();
  const discovered = [...html.matchAll(/(?:src|href)="(\/[^"?#]+)"/g)]
    .map((match) => match[1])
    .filter((path) => !path.startsWith("/api/") && path !== "/sw.js");
  await Promise.allSettled([...new Set([...SHELL_URLS, ...discovered])].map((path) => cache.add(path)));
}

self.addEventListener("install", (event) => {
  event.waitUntil(installShell().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

async function navigationResponse(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response.ok) await cache.put("/", response.clone());
    return response;
  } catch {
    return (await cache.match("/")) || Response.error();
  }
}

async function staticResponse(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok && response.type === "basic") {
    const cache = await caches.open(CACHE_NAME);
    await cache.put(request, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  // Browsers stream media with Range requests. Their 206 responses cannot be
  // stored by Cache.put(), so leave all range/audio/video traffic on the
  // browser's native network and HTTP-cache path.
  if (
    request.headers.has("range") ||
    request.destination === "audio" ||
    request.destination === "video"
  ) return;
  if (
    url.pathname === "/api" ||
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/ws/") ||
    url.pathname === "/sw.js"
  ) return;

  if (request.mode === "navigate") {
    event.respondWith(navigationResponse(request));
    return;
  }
  event.respondWith(staticResponse(request));
});
