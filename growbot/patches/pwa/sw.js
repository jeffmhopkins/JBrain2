/* GrowBot on the box: the installed app's service worker. It caches nothing, because the
   creature is the box's (models, voice, the robot relay) and a stale copy would only pretend
   to be alive. Its one job is a page that says so when the phone cannot reach the box,
   instead of the browser's own error, which in a full-screen app has no way back. */
"use strict";

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

var OFFLINE =
  '<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">' +
  "<title>GrowBot</title>" +
  '<body style="background:#0b0b1a;color:#e6edf3;font:18px system-ui;padding:24px">' +
  "<p>GrowBot can't reach the box. Check this phone is on the house Wi-Fi.</p>" +
  '<p><a href="/start" style="color:#7fd89a">Try again</a></p>';

self.addEventListener("fetch", function (event) {
  // Only page loads: the app's own calls and the relay's socket fail the way the page expects.
  if (event.request.mode !== "navigate") return;
  event.respondWith(
    fetch(event.request).catch(function () {
      return new Response(OFFLINE, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    })
  );
});
