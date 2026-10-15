// The "Send to panel" share target (vite.config.ts `share_target`), imported into the
// generated service worker. A photo shared from another app arrives as a multipart POST to
// /panel/share, which nothing on the box answers: the worker keeps the photo and any words in
// a cache and sends the app to /panel/send?shared=1, where the composer picks them up
// (PanelNoticeComposer.tsx, SHARE_CACHE). One share at a time: a new one replaces the last.

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "POST" || url.pathname !== "/panel/share") return;
  event.respondWith(
    (async () => {
      try {
        const form = await event.request.formData();
        await caches.delete("panel-share");
        const cache = await caches.open("panel-share");
        const photo = form.get("photo");
        if (photo && typeof photo !== "string") {
          await cache.put(
            "/panel/shared-photo",
            new Response(photo, { headers: { "Content-Type": photo.type || "image/jpeg" } }),
          );
        }
        // Apps put a shared line in title or text, rarely both; a bare link is not words.
        const words = ["title", "text"]
          .map((k) => form.get(k))
          .filter((v) => typeof v === "string" && v.trim() && !/^https?:\/\//.test(v.trim()))
          .join(" ")
          .trim();
        if (words) await cache.put("/panel/shared-text", new Response(words));
      } catch {
        // An unreadable share still opens the composer, which says so and offers the picker.
      }
      return Response.redirect("/panel/send?shared=1", 303);
    })(),
  );
});
