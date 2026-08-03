// Service worker: makes the dashboard installable and shells offline.
// The app shell is network-first (so a self-update is reflected on the next
// load, with the cache as an offline fallback); everything dynamic (/api,
// /share, SSE) is network-only so status, the live terminal, and the artifact
// are never stale.
const CACHE = "es1e12-shell-v2";
const SHELL = [
  "./", "index.html", "style.css", "app.js",
  "manifest.webmanifest", "icon-192.png", "icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Never intercept live data — let it hit the network directly.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/share/")) return;
  // Network-first: fresh shell when online (picks up updates), cache when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
