# JShare — publishing files at their own public links

> **Status:** Living · **Last verified:** 2026-10-18

JShare is where the owner publishes a file for other people: a picture, a sound, a video,
an HTML page, a zip holding a self-contained website, or any other file. Each upload gets
its own link, `https://<public host>/s/<slug>/`, that anyone can open without signing in.
Everything is done from the PWA (**launcher → JShare**): add a file, copy or share its link,
rename it, delete it. There is no host step.

## What a link serves

| Upload | Kind | What a visitor gets |
|---|---|---|
| `.png .jpg .gif .webp .avif .svg …` | image | The picture, in the browser tab. |
| `.mp3 .m4a .wav .ogg .flac …` | audio | The browser's own player. |
| `.mp4 .mov .webm …` | video | The browser's own player, with seeking (range requests). |
| `.html .htm` | html | The page. |
| `.zip` with an `index.html` | site | The site: `index.html` at the link, every other file at its path under it. |
| anything else (a `.zip` without `index.html` too) | file | A download. |

A zipped site's `index.html` may sit at the zip's root or in a single top folder (zipping a
`dist/` folder is fine). Its links must be **relative** (`app.css`, `img/a.png`): a link that
starts with `/` points at the box's app, not the site. A request for a folder serves its
`index.html`. Files inside a zipped site are served whole, without range requests, so a video
in a site plays but can't seek, and iPhones may refuse it; share a video on its own instead. Limits: 4 GB per upload, and a zip may hold at most 20,000 files and 4 GB
unpacked. Uploading over the Cloudflare tunnel is also capped by Cloudflare itself (100 MB
per request on the free plan); upload bigger files from the LAN site.

Deleting an item takes its link down at once: responses carry `Cache-Control: private`, so
Cloudflare's edge keeps no copy. The bytes are freed unless something else on the box (a note
attachment with identical content, another item) still holds them (`blob_refs.py`). That check and the delete are not atomic, so deleting an item while uploading the
identical bytes elsewhere at the same moment can still lose the file; the blob store has no
refcount yet.

## Who can do what

- **Upload, list, rename, delete** (`/api/jshare/items`): the owner only (`OwnerDep`). An
  upload is the raw file as the request body (name and title in the query), streamed straight
  into the blob store, so the size and free-space checks hold before the bytes land.
- **Read** (`/s/<slug>/…`): anyone with the link. The slug is 128 random bits, so links can't
  be guessed or enumerated. It is stored as itself rather than hashed, so the owner can copy a
  link again later; it grants nothing but that one item. Reads are rate-limited per client
  and marked `noindex` and `no-referrer`.
- In Postgres, a visitor runs under `jshare_context(slug)`, a non-owner context whose only
  grant is the one `app.jshare_items` row with that slug (migration `0252_jshare_items.py`).

A link names the box's public host whichever site the owner is using: `PUBLIC_BASE_URL` when
it is set, else `https://` + the installer's domain (`JBRAIN_DOMAIN`, passed to the api as
`JBRAIN_PUBLIC_DOMAIN`), else the PWA's own origin. Only the public site serves `/s/`, so a
link naming a LAN or owner-only name would not open for anyone.

## Why shared code can't touch the app

A shared site or HTML page runs its own scripts, and it is served from the app's own origin.
Four layers keep that script away from the owner's session:

1. **Sandbox.** Every `/s/` response carries `Content-Security-Policy: sandbox …` without
   `allow-same-origin`, so the browser gives the page an opaque origin: no access to the
   app's cookies, storage or service worker. `api/jshare_public.py` sets it; the real-Caddy
   test checks it survives the proxy.
2. **No cookie.** Caddy strips the `Cookie` header from `/s/*` before the api, and the route
   reads none.
3. **Cross-site to the api.** From an opaque origin the app's `/api` is cross-site: the `Lax`
   session cookie isn't sent on its requests, and `SameSiteGuard` refuses an `Origin: null`
   request that carries one anyway.
4. **Public site only.** `/s/*` is routed on the public site alone; the LAN and owner sites
   never serve it.

What the sandbox does not change is the address bar: a shared page still runs on the app's
own host, and in the installed PWA a link opens without one. So the sandbox leaves out
`allow-modals` (no `prompt()` asking for a passphrase on the app's host), and publishing a
third-party site you don't trust is still publishing it under your domain's name. A separate
share domain would remove both; the slug is already a single DNS label so it could name one.

The cost of the sandbox is that a shared site can't use `localStorage`, cookies or a
service worker of its own (they throw in an opaque origin). Its own fetches of its own files
work: responses carry `Access-Control-Allow-Origin: *`. The installed PWA skips its SPA
fallback for `/s/` (`navigateFallbackDenylist` in `frontend/vite.config.ts`), so the owner's
own phone opens links like everyone else's. The app's `frame-ancestors 'self'` policy rides on
these responses too, so other sites can't embed a share in a frame; link to it instead.
