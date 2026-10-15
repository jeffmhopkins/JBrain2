// The "Send to panel" share target (vite.config.ts `share_target`), imported into the
// generated service worker. A photo shared from another app arrives as a multipart POST to
// /panel/share, which nothing on the box answers: the worker keeps the photo and any words in
// a cache and sends the app to /panel/send?shared=1, where the composer offers them
// (PanelNoticeComposer.tsx, SHARE_CACHE). One share at a time: a new one replaces the last.
//
// ANY PAGE CAN POST HERE, not only the share sheet: a form on another site auto-submitted at
// this URL reaches this worker too (review of #1756). So a POST another site visibly started is
// dropped here, and what is kept is never put in the draft unasked: the composer shows it and
// waits for "Use it", and forgets one older than a couple of minutes.

const SHARE_CACHE = "panel-share";

/** True when the request shows it came from another site. The share sheet's own navigation is
 *  browser-initiated: no referrer, and `Sec-Fetch-Site: none` where the browser exposes it. */
function crossSite(request) {
  const site = request.headers.get("Sec-Fetch-Site");
  if (site && site !== "none" && site !== "same-origin") return true;
  if (request.referrer && request.referrer !== "about:client") {
    try {
      if (new URL(request.referrer).origin !== self.location.origin) return true;
    } catch {
      return true;
    }
  }
  return false;
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "POST" || url.pathname !== "/panel/share") return;
  event.respondWith(
    (async () => {
      await caches.delete(SHARE_CACHE);
      if (crossSite(event.request)) return Response.redirect("/panel/send", 303);
      try {
        const form = await event.request.formData();
        const cache = await caches.open(SHARE_CACHE);
        const at = String(Date.now());
        const photo = form.get("photo");
        if (photo && typeof photo !== "string") {
          await cache.put(
            "/panel/shared-photo",
            new Response(photo, {
              headers: { "Content-Type": photo.type || "image/jpeg", "X-Shared-At": at },
            }),
          );
        }
        // Apps put a shared line in title or text, rarely both; a bare link is not words.
        const words = ["title", "text"]
          .map((k) => form.get(k))
          .filter((v) => typeof v === "string" && v.trim() && !/^https?:\/\//.test(v.trim()))
          .join(" ")
          .trim();
        if (words) {
          await cache.put(
            "/panel/shared-text",
            new Response(words, { headers: { "X-Shared-At": at } }),
          );
        }
      } catch {
        // An unreadable share still opens the composer, which offers the picker.
      }
      return Response.redirect("/panel/send?shared=1", 303);
    })(),
  );
});
