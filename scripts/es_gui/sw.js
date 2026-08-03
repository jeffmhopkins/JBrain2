// Service worker: makes the dashboard installable and shells offline.
// The app shell is cache-first; everything dynamic (/api, /share, SSE) is
// network-only so status, the live terminal, and the artifact are never stale.
const CACHE = "es1e12-shell-v1";
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
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
