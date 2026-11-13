# growbot — the desk robot's brain on the box

> **Status:** Living · **Last verified:** 2026-11-13

The GrowBot (growbot.dev) is a Raspberry Pi Pico 2 W robot whose "creature" is a web app on a
phone. Stock, it thinks, hears, speaks and relays its leg commands through upstream's cloud.
Here all of it runs on the box: this directory holds the app, the server that serves it, and
(later waves) the firmware we flash. The plan, and why each piece is shaped as it is, is
[`docs/plans/GROWBOT_PLAN.md`](../docs/plans/GROWBOT_PLAN.md).

## Layout

| Path | What |
| --- | --- |
| `server/serve.py` | The sidecar: static app, `/api` shim, robot relay, ticket exchange. Stdlib only. |
| `site/` | The web root it serves (built from upstream + patches; committed, so the box needs no build). |
| `site/csp.json` | Hashes of the inline scripts the patched app still carries, read once at start for the CSP. |
| `firmware/` | What PWA flashing writes to a robot: the pinned MicroPython UF2, the patched robot firmware, and `SHA256SUMS` over both (built). |

## Upstream and patches

`site/` and `firmware/` are built, never hand-edited. `patches/upstream.json` pins the
britcruise9/GrowBot commit and every third-party asset (ONNX Runtime Web, Silero VAD,
MediaPipe tasks-vision and its BlazeFace model) by canonical URL and SHA-256; each matched the
copy growbot.dev serves byte for byte when pinned. MediaPipe's non-SIMD wasm fallback is left
out (every phone browser the creature supports has SIMD), and so is meSpeak.
`patches/apply.py` copies upstream's `app/` (minus the Custom-agent tab, the superseded face
page and the feedback widget), applies exact replacements that each must match a stated number
of times, adds `growbot-box.js`, writes the CSP hashes, and patches the firmware to dial the box
with its key, pinning the box CA when `secrets.py` carries one (`GROWBOT_CA`, DER as a
base64 string, since `wifi_store`'s restricted parser reads no bytes literal). A verified
dial first sets the clock, because mbedTLS checks the certificate's dates against an RTC that
boots in the past and the box's leaf certificates live hours: SNTP from `serve.py` on
`GROWBOT_NTP_PORT` (UDP 8123, published on the LAN), three tries feeding the watchdog, then
the Wi-Fi gateway's NTP; a clock still before 2026 refuses to dial and prints
`handshake: FAIL clock`, which the flasher reports as `clock_unset`. rp2's `time` epoch is
1970 (most ports' is 2000), so the patch reads it from `time.gmtime(0)`. It also
vendors MicroPython (`upstream.json` `micropython`: `RPI_PICO2_W-20260406-v1.28.0.uf2`,
MIT, pinned by SHA-256 like every asset) into `firmware/` and writes `firmware/SHA256SUMS`
over the UF2 and the five files a flash copies. It builds in a staging
directory, so a patch that stops matching fails with its name and leaves the committed tree
alone.

To move to a new upstream: change the pin in `upstream.json`, run `./sync.sh`, and review the
diff in its own PR.

The box watches for that moment itself (`backend/src/jbrain/growbot/upstream.py`). Every 12
hours the api reads upstream's commits Atom feed through the shared SSRF-guarded fetcher. When
`main` has moved off the pin, one unauthenticated GitHub `compare` call lists the changed files;
a compare already answered for that pin and head is never asked again. The policy files (JSON
under `app/policies/`, `app/policy*.json` and `policy/`) are kept in the `growbot_upstream`
setting. The owner gets one notification per new upstream head that brings any, and the PWA's
Body tab shows them on its *Walks and spins* card with *Check now* (at most once a minute).
`GET`/`POST /api/debug/growbot/upstream[/check]` read and run the same check with a debug
token. The api reads the pin off a read-only mount of `patches/` (`growbot_patches_dir`). `backend/tests/unit/test_growbot_site.py` holds the promises on the built
bytes. Upstream's licence (PolyForm Noncommercial 1.0.0) is `LICENSE`; the vendored projects'
licence texts ship beside their files. The GrowBot name and logo are upstream's trademarks.

## How it runs

The compose service `growbot` (`deploy/docker-compose.yml`, `deploy/Dockerfile.growbot`) is a
bare `python:3.12-slim` running `python3 /app/server/serve.py`, with this directory bind-mounted
read-only at `/app`, so **Ops → Update** ships a page or server change with no rebuild. It is
default-on (no compose profile), publishes no port, and sits on `edge` (Caddy reaches it) and
`growbot_lane`, an egress-free network only `api` and `tts-stt` share, so it has no route to the
database, the supervisor or the model gateway.

Caddy's LAN site `https://jbrain.local:8444` (rendered by `deploy/proxy-lan-conf.sh` from
`JBRAIN_LAN_ADDR`, same `tls internal` CA as the LAN app) is the way in at home. It is its own
origin because upstream's root-absolute paths (`/api/…`, `/v3/…`) collide with the PWA's.

### Away from home

So the PWA opens a persona from anywhere (owner, 2026-11-11), the creature is also served on
`https://growbot.<host>` through the Cloudflare tunnel, where `<host>` is
`JBRAIN_JCODE_PREVIEW_BASE_HOST`: `deploy/proxy-preview-conf.sh` renders it beside the
previews' `http://*.<host>` site, so it rides their wildcard tunnel route and DNS record and
needs no Cloudflare change (and exists exactly when they do: tunnel mode, base host set). Its
certificate is Cloudflare's, so a phone installs nothing. It is the `:8444` block with three
doors shut: `/setup.html`, `/setup.js` and `/box-root.crt` are for a phone at home, and a
cookieless `/d/` (a robot dialing the relay, never a phone) 404s, since a robot dials in on the
LAN. Caddy only sniffs for a cookie named `gb_session`, so the sidecar is the real gate: the away
site stamps `X-Growbot-Away` on every request (`:8444` strips it), and a relay socket wearing it
without a valid session is 403, keeping the robots' unauthenticated budget and their key guesses
on the LAN. `/open` spends one ticket per client at a time and 16 in all, since its backoff
counts a failure only once the api answers; away clients are keyed by address, an IPv6 one by
its /64. A signed-in phone's `/d/` passes, so a
persona opened away still wears its body at home through the relay. Cloudflare sees this site's
traffic (chat, voice, camera frames), as it does the PWA's. One thing the LAN origin never had:
the jcode previews (`<slug>-preview.<host>`, the owner's own sandboxed dev apps) are same-site
with it, so a hostile preview the phone opened could toss a `Domain=<host>` `gb_session` that
signs the phone out there, or spend `/open`'s backoff. Neither reaches a session, the relay or a
response (exact-Origin checks, `frame-ancestors 'self'`, no CORS); a `__Host-` cookie name would
close the first, at the cost of signing every phone out once.

The api picks the link: a ticket minted from the PWA on the LAN site opens on `:8444`, one
minted from anywhere else on the away name (`growbot.opens_away`; `GET /api/growbot/status`
says which as `away`, and the PWA skips the box-certificate sheet when it is true). The robot's
own phone asks for `where: "lan"`, because its setup page follows only `:8444`'s links. A
phone's session is per origin, so the first open away is a fresh sign-in on that phone. Behind
the tunnel Caddy's peer is the connector, so the sidecar takes the client from Cloudflare's
`Cf-Connecting-IP` when, and only when, Caddy's `X-Real-IP` is `cloudflared` (as the api's
`client_ip.py` does): otherwise every away client would share one backoff bucket.

| Env | Default | Meaning |
| --- | --- | --- |
| `GROWBOT_PORT` | `8810` | Listen port (plain HTTP, behind Caddy). |
| `GROWBOT_SITE` | `/app/site` | Web root. |
| `GROWBOT_API_URL` | `http://api:8000` | The api: ticket redeem, robot verify, the model lane. |
| `GROWBOT_TTS_URL` | `http://tts-stt:8801` | Kokoro `/tts`. |
| `GROWBOT_STT_URL` | `http://tts-stt:8080/v1` | whisper `/audio/transcriptions`. |
| `GROWBOT_TOKEN_FILE` | `/run/jbrain-growbot/growbot.token` | Bearer for the api's `/internal/growbot/*`; re-read per call. |
| `GROWBOT_DEFAULT_MODEL` | `qwen3-vl-30b` | Lineup fallback while the api lists no models. |
| `GROWBOT_TRUSTED_PROXY` | `proxy` | The one peer whose `X-Real-IP` is believed (Caddy's compose name, re-resolved every minute). |
| `GROWBOT_TRUSTED_TUNNEL` | `cloudflared` | The connector: when Caddy's `X-Real-IP` is it, `Cf-Connecting-IP` names the client. Unresolvable (no tunnel) trusts nothing. |

## Flashing a robot (wave G2)

The owner plugs the robot's Pico 2 W into the box's USB and adds it from the PWA's GrowBot
screen (`backend/src/jbrain/api/growbot_flash.py`). The endpoint sidecar
(`deploy/endpoint/pico.py`) does the device work: `picotool` writes the UF2 to a board in
its RP2350 bootloader (2e8a:000f, no button needed on a blank one; `mpremote bootloader`
first if it is running), `mpremote` reads its pairing code, then copies `relay_chip.py` as
`main.py` with `act_engine.py`, `PicoRobotics.py`, `wifi_store.py`, `wifi_recovery.py` and a
generated `secrets.py` (`backend/src/jbrain/growbot/provision.py`: the box's LAN IPv4 from
AdGuard, port 8444, the LAN name as SNI, the minted key, the box root, and the Wi-Fi list in
the managed `# BEGIN GROWBOT WIFI V1` block). The key is minted only after the board says
which robot it is. A robot counts as paired once the relay verifies its hello, which stamps
its `last_seen_at`. A rekey swaps the one `GROWBOT_KEY` line in place; a revoke nulls the key
and drops the robot off the relay.

## Installing it on a phone

Chrome on Android offers *Install app* for the creature: `v3/next.html` links
`/manifest.webmanifest` (full screen, start page `/start`, icons under `/icons/`), and
`growbot-box.js` registers `/sw.js`, which caches nothing and only shows a "can't reach the box"
page when the phone is off the Wi-Fi. These four files are the only ones served without a
session, because Chrome fetches the manifest and icons with no cookie. `/start` sends a
signed-in phone back into the creature on its session's robot, and a phone whose session
has lapsed to `/setup.html`. Chrome
installs only from a site it trusts, so the phone needs the box's root certificate installed
(the GrowBot screen's *Download the box's certificate*), not a warning clicked through. On an
iPhone a home-screen app keeps its own storage and cookies apart from Safari's, so it is
another phone to the box: open the persona there (a fresh code) and Safari's copy shows that it
has moved.

## Setting up the robot's phone

That phone is signed in to nothing, so JBrain's GrowBot screen shows two codes under *Set up
another phone*. The first, scanned with the camera app, opens `/setup.html` on this site: a
*Download the box's certificate* button (`/box-root.crt`, the public half of Caddy's internal
CA, read from the api's `/internal/growbot/lan-root` and served only here on the LAN) and a
scanner (`setup.js`, Chrome's built-in `BarcodeDetector`). The second is a one-time ticket for
the robot chosen there (or none); the scanner follows only this origin's `/open?ticket=` link,
so a code from anywhere else opens nothing. A lapsed installed app's `/start` lands on the same
page, so getting it back is one scan.

## Security

- **Opening it.** The owner mints a one-time ticket through the api (`POST /api/growbot/ticket`;
  until G2 adds the PWA entry, through the owner debug API's copy). `GET /open?ticket=…[&id=<code>]` redeems it and sets `gb_session` (HttpOnly, Secure,
  SameSite=Strict, 30 days, renewed at most daily by any page load or call), then answers a page that moves on to
  the creature by itself. Not a 302: the PWA's link may be a cross-site navigation (the PWA reached by its public name), and Chrome
  withholds a Strict cookie for the rest of a cross-site redirect chain, so a redirect landed
  the phone on the creature signed out. The cookie carries, under its HMAC, the robot code the api says the ticket was minted for (the
  PWA names it at mint, migration 0256; the link's `id` is never trusted), and the relay
  lets that phone join that robot alone: pairing is JBrain's, and a code typed into the
  creature's own pairing box is refused (`403 pair from JBrain`). The key is derived from
  `growbot.token` and `growbot-session.token`: nothing is stored, a restart keeps phones signed
  in, and *Sign out every phone* (the GrowBot screen, or the debug API's
  `POST /growbot/sessions/reset`) re-mints the second file, which signs every phone out at once. Every other route answers 401 without it, decided from
  the request head: without a session nothing but a body-less `/open`, `/start` or one of the
  installed app's own files (`PUBLIC_PATHS`: manifest, worker, icons, setup page, voice shim) is read, and the 401 closes the connection, so an anonymous client never
  costs the process an upload. Requests that act must also come from this origin, since the PWA
  on `:443` is the same *site* and SameSite alone would let its requests carry the cookie: a
  non-GET `/api` call with a foreign `Origin` is 403 (one with no `Origin` is allowed, as a
  same-origin fetch may omit it and a cross-origin one cannot), and the phone side of the relay
  must send this origin's `Origin` or is 403.
- **The owner's session never arrives.** Browsers send cookies regardless of port, so the PWA's
  `__Host-jbrain_session` would reach `:8444`. Caddy rebuilds the `Cookie` header to hold
  `gb_session` alone, or drops it, and never routes `/internal*`.
- **No egress.** Every response carries a CSP of `'self'` for every fetch directive (`blob:` and
  `data:` where the app needs them, `'wasm-unsafe-eval'` for ONNX Runtime and MediaPipe, and
  the inline-script hashes from `site/csp.json`, never `'unsafe-inline'` for scripts), with
  `frame-ancestors 'self'`, `base-uri 'self'`, `form-action 'self'` and `object-src 'none'`,
  plus `nosniff`, `no-referrer` and a Permissions-Policy granting camera, microphone and the
  motion sensors to this origin. The server calls only the three URLs above, with environment
  proxies and redirects disabled. A chat message keeps only `text` parts and inline
  `data:image/…;base64,` images (no image by URL, video, audio or any other part type), so no
  message can have the model's engine fetch anything.
- **The api's routes.** `GET /internal/robots` (`{robots: [{code, since, creature}]}`, the
  robots on the relay now, and whether a creature page listens on each one's side channel),
  `POST /internal/robots/drop` (`{code}`: close a revoked robot's socket, 1008) and
  `POST /internal/robots/move` (below) take `Authorization: Bearer <growbot.token>`, compared in constant time; without it
  the request is 401 before any body is read. Caddy's site 404s `/internal*`, so they are
  reachable only from a container on `edge` or `growbot_lane` that holds the token.
- **The robot.** The relay's chip side has no browser and no cookie: its first frame must be a
  `hello` whose `key` the api verifies for that pairing code, or the socket is closed (1008). The
  key is stripped from the hello before phones see it and is never logged. A robot flashed
  without `BOX_CA` in `secrets.py` dials TLS unverified, so anyone who can impersonate the box on
  the LAN could take its key and pose as that robot (never anything more); PWA flashing
  always writes the CA, and refuses to flash at all when the box's root cannot be read.
- **Limits.** 16 KiB of request head, 16 MiB of body (`Content-Length` up to 10 digits, chunk
  sizes up to 8 hex digits, checked before any `100 Continue`), 512 connections, 256 KiB per
  WebSocket message, 256 sockets, 8 phones (or agents) per code. Cookieless sockets (robots
  until their hello is verified) have their own budget of 16, and a verified robot leaves it.
  Failed `/open` tickets and failed robot hellos are counted per client (Caddy's `X-Real-IP`,
  believed only from the proxy, whose last good address is kept if a lookup fails): 8 in 5
  minutes and that client gets 429 until the window passes. Tickets and hellos keep separate
  counts, and so do cross-site `/open` navigations, so a page the owner visits cannot lock out
  the PWA's own link; an `/open` that is not a top-level navigation (`Sec-Fetch-Dest` other than
  `document`, except `empty` with `Sec-Fetch-Mode: navigate`, the installed app's service worker
  passing the link on; a frame is refused) is 403 and never counted, and a cookieless socket that sends an `Origin` (a
  browser, never the chip) is 403. The counts are only per client while Docker hands Caddy the
  real source address; behind a userland proxy every client shares one count. Socket slots are taken at the
  admission check, so a burst cannot overshoot them. Outbound calls run on pools apart from
  static serving: 8 threads for brain, Kokoro and whisper calls, 2 for the model list, and 2
  for ticket redeem and robot verify, so slow brain calls never queue the doors. A pairing code's channel exists only while an admitted socket
  is on it. Static paths are percent-decoded once; dot segments, dotfiles, backslashes, NULs
  and anything resolving outside the root are 404, and there is no directory listing.

## The `/api` shim

What the upstream app calls, and what answers on the box:

| Route | Answer |
| --- | --- |
| `/api/config` | `{fast, active, dream, fallback}`: one installed model, vision-capable first. |
| `/api/models` | `{simple, all}`: the box's installed models from the api, all free to run. |
| `/api/llm?target=openrouter`, `/api/llm-stream?target=openrouter` | The api's `/internal/growbot/llm/v1/chat/completions`, body reduced to the engine fields; an unknown model becomes the default. The stream is passed through as SSE as it arrives. |
| `/api/llm?target=groq-audio\|deepinfra-audio` | whisper, rebuilt as `model=whisper`, `response_format=verbose_json`, and normalised so `text` and every segment's `no_speech_prob` and `avg_logprob` exist (a missing probability reads as speech). 503 when whisper is down, never 429, with `error` saying which: not running, over 60 s, or the HTTP status it answered. |
| `/api/fish-tts` | Kokoro `GET /tts` with the text, a `kokoro-*` voice (else Kokoro's default), speed and pitch; WAV back. 503 when down, never 401/402, which upstream latches as a dead voice. |
| `/api/device` | One owned device, nothing metered. |
| `/api/redeem`, `/api/transfer` | 410: no credits on the box. |
| `DELETE /api/journal`, `DELETE /api/episodes` | 200 `{"ok":true}` (the app's erase flow reads JSON); nothing is stored to erase. |
| `/api/gallery?…&download=1` | 404: no soul or policy gallery on the box. |
| `POST /api/journal` (a diary snapshot: `sid` and `log`) | its rows to the api's `POST /internal/growbot/journal` off the page's path (soul peek and join key left behind), kept a week for the brain log (`../docs/plans/GROWBOT_PLAN.md` §2 "Brain log"); 200 `{"ok":true}`. A watch-id registration (`op`) or anything else is 204, stored nowhere. |
| `/api/journal`, `episodes`, `gallery`, `ev`, `hit`, `feedback` | otherwise 204, stored nowhere. |
| `/api/box/cal?id=<code>` | The box's own route: `{cal}`, the robot's leg calibration as the PWA saved it (the api's `/internal/growbot/robots/<code>/cal`, cached 2 s), or `{cal: null}` when never saved, the api cannot say, or the robot is not the one this phone drives (its ticket's, or the one its persona wears), since `gb_walk_cal` is one key per phone. |

When the box fails the creature, it says why. `growbot-box.js` keeps the reason a speech call
failed (the sidecar's `error`, or how long the page waited before it gave up), and the page's
"my ears are down" and "brain error" lines read it (`gbBoxEars`, `gbBoxBrainWhy`), with the box
model's own words from an error frame in the stream. A clip the creature's checks drop names the
check ("…didn't catch that: the speech model heard no words"). A spoken turn waits 90 s instead
of upstream's 30: the box reads a creature's ~8k-token prompt at a few hundred tokens a second.

## The relay

`wss://jbrain.local:8444/d/<code>` reimplements upstream's relay (`relay_chip.py` and
`relay-transport.js` define the envelopes). A socket with a valid session is a phone; one
without must be the chip (above). Phone frames go to the chip, chip frames go to that code's
phones. The relay sends phones `{"t":"status","awake":0|1}` when they attach and when the chip
comes or goes, and `{"t":"alive","quiet_ms":N}` every 30 s while a heartbeating chip is attached
(`quiet_ms` is the time since its last message, heartbeats included and pongs not). It answers the chip's `{"t":"hb"}` with
`{"t":"hb_ack"}`, pings every socket every 20 s, and drops any socket silent for 45 s (a robot
that loses power never closes its socket). A second chip on a code replaces the first.

The api's `POST /internal/robots/act` `{code, frame}` puts one PWA move on a code's chip, past
any phone riding it, as upstream's tester does: an `act` of at most 12 whole-degree keyframes
(0–180°, 50–2000 ms each), the `wiggle` routine, or a `stop`, rebuilt from the checked fields
with a rid above any a phone counts to. `{sent: false}` when no chip is on the code.

`/d/<code>-agent` is upstream's side channel for an external agent: the creature page joins with
`hello` and agents with `attach`, both behind the session cookie, and frames are bridged both
ways. It never meets the robot's channel.

**Moves from JBrain.** The PWA's Body tab drives the robot through this side channel. The walk
and spin policies read the rider phone's motion sensors, so only the creature page can run
them: `growbot-box.js` joins `/d/<code>-agent` with `hello` while the page wears a robot and is
visible (the rider check keeps it to the rider), and leaves when hidden, since a hidden page
stops every move. `POST /internal/robots/move` `{code, move, secs}` (`move` one of `forward`,
`back`, `spin_left`, `spin_right`, `wiggle`, `bow`, `sway`, `stop`; `secs` 0 or 1-15) sends
`{"t":"box_move", rid, move, secs}` to that page, which runs it through its own `runGait` or
`moveLegs` with this phone's walk calibration and answers `{"t":"box_ack", rid, ok, state,
why}` at once: what it started, or its own words for why not. The route answers `{ok, state,
why}`, 409 `no_creature` when no page listens and 504 `no_answer` after 3 s; a `box_ack` that
arrives while no move waits is bridged to the agents like any other frame.

## Leg calibration (the Body tab's Legs pane)

Upstream keeps a robot's calibration (`gb_walk_cal`: the by-eye trims `L_OFF`/`R_OFF`, the
left/right `SWAP`, the leg signs `L_SIGN`/`R_SIGN`, the phone's `IMU_SIGN` and the walk
`gain`) in the localStorage of whichever phone set it in its tester. Here it belongs to the
body: the PWA's Body tab saves it on the robot's row (`app.growbot_robots.cal`, migration 0261,
`api/growbot_body.py`), and `growbot-box.js` copies it into `gb_walk_cal` on every page that
loads the shim, synchronously at load (a tester page reads it once) and every 5 s while
visible. The box holds only the keys set from the PWA, and those win one by one; every other
key (and everything, for a robot never calibrated there) stays the phone's own. So once a key
is set from the PWA, that is where it is changed: upstream's tester still edits the phone's
copy, and the next poll puts the box's value back. Upstream reads the calibration at each move (`calNow()`, and
relay-transport reads `SWAP` per frame), so a change lands on the next move with no reload. The
PWA's check moves (which leg, a trim, a sweep) go chip-direct through the relay, the box
applying a saved swap on the wire.

## Personas (wave G4b)

A persona's soul lives on the box (plan §2c; the api's `jbrain.growbot.personas`). A ticket
minted for a persona redeems to the persona and the phone that now holds it, and the session
becomes `<expiry>.<nonce>.<robot>.<persona>.<phone>.<hmac>` (G2's four-part form is still read,
and opens the creature with its soul on the phone, as before). The phone id is the api's
`growbot_phones` id: `/open` sends the one this browser's session already has, so a phone stays
the same phone across tickets, with a kind-of-phone label from its User-Agent (never the string).
A cross-site `/open` (the PWA reached by its public name) arrives without the Strict cookie, so it
spends nothing and only bounces: its page refreshes to the same link plus `hop=1`, which comes
back same-site with the cookie and counts its tries in the cross-site bucket. So a link from
another site that carries a live ticket opens with the phone's own session; only the owner
mints tickets, and each opens once.

| Route | Answer |
| --- | --- |
| `GET /api/box/persona` | The api's `/internal/growbot/soul/pull` for this session's persona and phone: `{version, soul, wearing, persona}`. |
| `GET /api/box/persona/state` | `/soul/state`, the poll: `{version, wearing, persona}`. |
| `POST /api/box/persona` | `{persona, base, soul}` to `/soul/push`: `{version}`. A `persona` that is not the session's is 409 `other` (a write from the persona the phone held before). The body is capped at 1.1 MB before it is read. |

All three answer 204 for a G2 session; 409 `moved` (another phone holds it), 409 `newer` (the
owner changed it), 404 `gone`, 413 and 400 are the api's; anything else is a 503 the page
retries. They run on their own two threads, and the relay's rider checks on two more, apart
from the doors and the brain.

`growbot-box.js` does the rest on `/v3/next.html`, before any page code: a synchronous pull
writes `pb2_soul` (the one key soul.js keeps the whole creature in), and the page moves to
`?id=<robot>&mode=legs` for the robot the persona wears, or drops `id` (and `gb_walk_id`) when it
wears none. Every write of `pb2_soul` is pushed 1.5 s after the last, against the version it came
from, and when the page is hidden; `gb_box_soul` keeps `{persona, version, dirty}`, so a write
the box never got is sent on the next load, before the pull, and only into the persona it came
from; a box that does not answer keeps it (the page runs on it) until the box takes it or says
it never will. While a persona has the key, the phone's own creature (a G2 session's) waits in
`gb_phone_soul` and comes back when a G2 session opens. A poll every 10 s notices an owner's edit (the page restarts on it once nobody has
touched it for 20 s; a write racing it is refused as `newer` and the owner's wins), a hand-over
(the page moves on or off the robot) and a move or deletion, which stop the page and say so.
The creature's own pairing box (a `prompt` behind Pair and Reconnect) answers that robots are
paired from JBrain.

A persona also wears its own face (`growbot_personas.face`, lantern by default): the pull and
the poll carry it, `growbot-box.js` hands it to `growbot-face.js` before the face loads, and a
`gb-box-state` event with a new one changes the face where the creature stands.
Every face's mouth moves while its voice is actually sounding, whatever the mood: the face asks
the players (the Kokoro line on Web Audio or `fishAud`, a song, the device synth) rather than
upstream's `speakingUntil`, which is the ear's queue-time estimate and both outlived and fell
short of real lines. The face also measures the line's loudness in 20 ms steps so the mouth
keeps time with the words: on Web Audio (the iPhone's Kokoro route) from the line's decoded
buffer at the context's clock, and on `fishAud` or the box voice's own element
(`window.__gbBoxAudio`) by fetching the `blob:` back and decoding it offline; it never routes
the element through Web Audio, which a suspended iOS context would mute. The ember face
(Dave's lens) shows that loudness as its light.

It speaks in its own voice the same way (`growbot_personas.voice`, migration 0265; `""` is the
box's default): `growbot-box.js` keeps it in `__gbBoxVoice` from the state read and every poll,
and both speech paths (the page's fish lane and the `speechSynthesis` stand-in) send it, ahead
of any voice the phone kept, so a new pick is the next line's voice.

Whether it explores when it's quiet is the persona's too (`growbot_personas.explore`, migration
0266, on by default): `growbot-box.js` keeps it in `__gbBoxExplore`, and the page's ordinary
quiet beat reads it each time (`apply.py` `QUIET_TEXT_PATCHES`), so only an explicit `false`
brings back upstream's old "quiet company" words. No URL switch or phone storage holds it.

### Settings (migration 0263)

The phone is only a display (owner, 2026-11-12). What upstream keeps outside the soul, each in a
localStorage key of its own, is the persona's too when it is the creature's: its coat colour,
its games guide, volume and speech speed, which camera it looks through (`gb_eye`), how it
talks (`gb_talk`: kid, or grown-up), low power,
visual memory on or off, *keep tracking me* per robot, whether it keeps a brain log and its
diary id, and every remembered `?flag` (`getup`, `ftdial`, `fishwa`, `asfix`,
`judge0`, `bodysnd`, `listen`, `stt`, `rest`, `legslew`). The list is `KEYS` and `PREFIXES` in
`growbot-box.js`; the box keeps them in `growbot_personas.prefs` and bounds only their shape
(64 keys, 16 KB each, 64 KB in all). What stays on the phone is about the phone, or a secret: its tilt
calibration (`gb_up_vec`), the consent it gave, upstream's tokens and keys (a fish.audio key in
`pb2_voicetest` among them), robots' Wi-Fi passwords (`gb_travel_cards_v1`), the open tab, the
sync's own bookkeeping, and the diary's rolling window (its rows are on the box already). A
duplicate copies the settings but not the diary id, so the two keep separate diaries.

| Route | Answer |
| --- | --- |
| `GET /api/box/prefs` | `/internal/growbot/prefs/pull`: `{version, prefs, adopt, persona}`, `prefs` null until a phone first sent its own or the owner set one; `adopt` true while only the owner has. |
| `POST /api/box/prefs` | `{persona, set, drop}` to `/prefs/push`: `{version}`. Capped at 320 KB before it is read; 409 `other` as the soul's. |

At load, after the soul, a synchronous pull replaces this phone's copy of those keys with the
box's (the phone's own wait in `gb_phone_prefs` for a G2 session, as the soul's do). A persona
with none yet takes the first phone's own, never another persona's (an owner edit made before
any phone sent its own is marked, and the pull's `adopt` tells the first phone to lay its own
under the owner's rather than drop them; the box holds to that even for a phone that pulled before the owner's edit landed, keeping only its keys the owner had not set, and that phone pulls again when its write shows the version moved). A change (a `setItem`, a
`removeItem`, or upstream's `localStorage.key = v`, which a 2 s check catches) goes back 1.5 s
later, and when the page hides, as a patch of only the keys that changed, so the owner's edit
of another key stands. `gb_box_prefs` keeps `{persona, dirty, base, version}`: a change the box
never got is sent on the next load, before the pull. The poll's `prefs` version tells a running
page the owner changed them (`PATCH /api/growbot/personas/{id}/prefs {set, drop}`, also on the
debug API): the page writes them where it stands and fires `gb-box-prefs` `{changed}`, which the
camera listens for.

### Fast turns

The box's model is a hybrid: llama.cpp reuses a cached prompt only from a context checkpoint,
and it leaves one at each message boundary. Upstream built the creature's system message with
its state, clock, diary, move status and self-stats about 2k tokens in, so every turn diverged
before any checkpoint and re-read all ~8k tokens (~20 s on the box). `apply.py`'s
`SPEED_PATCHES` keep the system message to what stays put (constitution, identity, output
schema, the movement guide's fixed contracts) and move the moving parts (wants, state, clock,
diary, latest move, seek and stance status, gait list, self-stats) to the head of the turn's
own message (`gbBoxLive` in `growbot-box.js`), so a turn resumes at the system's end and reads
only its history and its moment. The creature's slot (`slot_roles`, GrowBot) never spills to
another slot, where that checkpoint does not exist, and is freed only after every background
slot. The cost: a second phone (or persona) open at once waits behind the first and replaces
its cached prompt, so two creatures talking together are each slow.

While the person is still talking, the page sends that turn minus their words, a one-token
warm-up the sidecar passes on as `X-Growbot-Warm`, so the engine has read the history and the
camera frames by the time the transcript lands; the turn then carries the words as its own last
message and only those are left to read (`WARM_PATCHES`, `docs/plans/GROWBOT_PLAN.md` §2).

Speech is the same lesson as the panels': whisper encodes a fixed 30 s window, so a 3 s clip
cost 10-30 s on the box and outran upstream's 8 s / 12 s transcriber leashes every time (the
mic never produced text). `serve.py` sizes `audio_ctx` to a WAV clip's own length (the room
panels' 9.5 s to 2.3 s cut), and `STT_PATCHES` drop the cloud leashes and the hand-off to a
backstop lane, which only re-sent the same clip to the same whisper.

### What it is doing (the status strip)

So the owner can see from the screen alone what the creature is doing, and above all when it
takes a picture (owner, 2026-11-12), `growbot-status.js` keeps a strip of chips at the top-left
of `/v3/next.html`: photo, looking (a model call carrying a picture), thinking, hearing (an
utterance being recorded), transcribing, voicing (Kokoro), speaking, moving (relay leg frames),
and, with nothing under way, listening or idle. Each chip runs a clock; a finished one shrinks to
its icon and final time and lingers 5 s. Whatever is under way always sits at the left edge,
newest first, with finished chips after it, most recent first; on the wake gate the strip drops
below the GrowBot logo bar instead of covering it. A photo also flashes the
screen's edge. It watches from outside rather than patching each call site: it wraps `fetch`
(the box's `/api/llm`, `/api/llm-stream` and `/api/fish-tts`, timed until the body is read, so a
streamed reply counts as thinking until its last token) and the relay socket's `send` (a frame
repeating the last one, as a held pose does every 200 ms, is not movement), and polls upstream's
`vadOn`, `recording`, `listenMode` and `speakingUntil`. The photo is the moment a model call
carries a picture: the camera itself encodes a frame into its ring every 2.5 s whether or not
anything looks, so an encode would flash constantly. It loads right after the face and before any page script
so every call goes through it; `?status=off` leaves the page untouched.

### The eyes follow a face

The pupils follow the person in front of the phone, with no model call (owner, 2026-11-12).
`growbot-eyes.js` turns a face box into a point on the screen and hands it to the face
(`gbFace.see`), which eases the pupils and the whole eye toward it. When the box is more than
1.3 s old, the eyes settle back. A touch's glance outranks it, and so do thinking, looking,
resting and sleep. The box comes from the BlazeFace detector that the leg tracker already
ships. While the tracker is on, it passes every box it keeps (`apply.py` hooks both of its
result paths). While it is off (no robot paired, a person said stop, or the camera or detector
failed it), `growbot-eyes.js` runs its own copy of the detector in a worker on the creature's
open front camera. The two never run at once, because a second GPU worker could make the
tracker miss its 350 ms frame deadline, and that drops leg tracking to the main thread for
good. So when the tracker isn't following, it now looks 4 times a second, not once, while a
face was seen in the last 5 s. While the body moves, the tracker sends no frames, so the eyes
hold their last look for 1.3 s and then settle. The eyes' own copy runs 8 times a second (4 in
low power, or twice a second after 5 s with no face), and it closes after 30 s without a
camera. It uses the main-thread detector only where no worker can run at all. A failed worker
retries after 30 s and then 120 s, then gives up, logging an `eyes_detector` journal row each
time. Frames stay in the browser, and nothing runs while the camera is off or facing the back.

### What was said stays on screen

Talking over the creature used to lose what it was saying: a new reply overwrote the one
caption (owner, 2026-11-13). `growbot-talklog.js` keeps both sides of the conversation and
shows them two ways. A fading trail above the caption holds the last three lines from the
past 4 minutes, older ones dimmer, the person's on the right. Tapping a line opens it in
place. A log drawer, opened from the trail's *log* pill or a swipe up on the trail, holds the
whole week with a search box. In both, the part of a reply the person never heard is dimmed
behind a *✂ you cut in* marker, with *hear the rest* to have it spoken.

What was heard comes from upstream's own sentence queue (`TALKLOG_PATCHES` in `apply.py`).
Each streamed sentence is queued with the caption offset it ends at. `_chainNext` starting it
marks it playing, and the next `_chainNext` marks it finished. Every path that drops the
queue reports first, and what was playing or still queued is marked cut: `silence()` (barge-in,
a new reply over an old one, pause), `_ctxHush`, and `speakNow` when a whole line takes the
voice over a stream. A settled caption that turned a dash into a comma stays the same line. A sentence cut mid-play counts as unheard. So does caption text the page
held back while the person talked (`_yieldNow`). A muted creature is caption-only by choice,
so nothing is dimmed. The person's lines come from the one input funnel (`sendTxtFrom`), typed
or spoken. Body sounds are left out. The lines stay on the phone, in localStorage per persona
(`gb_talk_log:<persona>`, 300 lines, a week), and are never sent anywhere. The drawer's
*clear* removes them. A tab keeps writing to the persona it loaded, even after another tab
moves the phone to a new one. Taps on the trail and the drawer are never a pet or a
push-to-talk hold. `?talklog=off` leaves the page as upstream drew it.

### The bottom bar and the wake gate

`CHROME_PATCHES` fit upstream's chrome to a box-run creature (owner, 2026-11-12). The bottom bar
(brain, pause, mute and the type box) never fades: upstream hid it 9 s after the last touch.
When the keyboard opens, Android Chrome shrinks the page to the space above it
(`interactive-widget=resizes-content`) instead of panning it up, so the face redraws smaller and
the status strip stays on screen; iOS ignores that key and still pans.
The wake gate drops upstream's store card under the wake button ("adopted, i'm yours", the
members area and a soul download, or *adopt me · $20*): the box answers `/api/device` with a
pass, there is no store, and the soul lives on the box.

### The brain panel and text size

The creature's Settings tab is scrubbed of what a box-run creature has no use for (owner,
2026-11-11): the brain dial (the box picks the model), credits, *move me*, *your own key*, the
soul backup row (the box holds the soul; personas and their checkpoints are the backup), the
cloud logs link, the old full panel and *Advanced*'s placeholders. `SCRUB_PATCHES` hide them
behind `window.__gbBox`, which `growbot-box.js` sets, so the page without the shim keeps
upstream's panel. What stays: the body, *follow my face*, the eye, the voice and low power. The money-code badge, a
saved credits tab and the full panel's key and credits tabs are closed off the same way, and
*My thoughts* loses its cloud logs link.

The rest went the same way (owner, 2026-11-12: *"remove all the stuff that doesn't apply to our
implementation"*): *My thoughts* loses its soul download (the box keeps the soul; JBrain exports
it), and its *Visual memories* no longer say "on this device"; the full panel's MIND tab loses
the soul download, *move him to a new phone*, the cloud watch link and the cloud notes card
(whose delete erased nothing here). What those rooms left on a phone (`gb_brain`,
`gb_free_pick`, `gb_effort`, `pb_byok`, `pb_llm_*`, `pb_energy_out`, `pb2_erase`,
`gb_nx_watch`) is dropped by `growbot-box.js` before the page reads it, so no model pick, key or
custom endpoint steers a box creature. The cloud notes card was also the brain log's only
switch, so an old opt-out (`gb_next_log` / `pb2_journal` "0") is lifted too: the brain log goes
to the box and is always kept.

Upstream sets the spoken line at 11px and the brain panel at 11-12.5px; `growbot-box.js` raises
them to phone sizes. A phone in desktop-site mode (Safari's *Request Desktop Website*, Chrome's
*Desktop site*) ignores the viewport tag and lays the page out ~980px wide; the shim detects that
(a touch screen whose window is over 1.25x its screen) and zooms the controls back, leaving the
face's canvas, which sizes itself to the window.

### Visual memories (wave G4d)

The saved views upstream keeps in IndexedDB (`v3/visual-memory.js`) live on the box with the
persona (the api's `growbot_memories`, pictures in the blob store's `growbot-memories`
namespace). apply.py makes three small changes to that file: `vmNs()` reads the tab's
`window.__gbBoxVmNs` first, `vmOpen()` waits on `window.gbBoxMemReady`, and every read-write transaction calls
`window.gbBoxMemChanged()`.

| Route | Answer |
| --- | --- |
| `GET /api/box/memories` | `/internal/growbot/memories/list`: `{version, memories}`, each record's fields and its views' times, never a picture. |
| `GET /api/box/memories/view?id=&i=` | `/memories/view`: `{j}`, one view's JPEG as base64 (as IndexedDB keeps it). |
| `POST /api/box/memories` | `{persona, memory}` to `/memories/keep`: the record whole, capped at 2.5 MB before it is read. 400 is a record the box will never keep, 413 a persona at its 500. |
| `POST /api/box/memories/forget` | `{persona, ids}` to `/memories/forget`. |

They answer like the soul's routes (204 for a G2 session, 409 `other` for another persona's
write, the api's 400/404/409/413, else 503). `growbot-box.js` pins the tab's namespace to
`gb-<persona>` (never shared through localStorage, so a tab left on the persona the phone held
before cannot save into the new one), merges the box's records in before the creature opens
its store (each record lands as soon as its pictures are in; one it cannot fetch waits for the
next load and never causes a reload), and sends the creature's writes 2 s after the last,
backing off to 5 minutes while the box will not answer. Each side's change wins against
`known` (what both last agreed on, in `gb_box_vm/<persona>`); a record the phone lacks is
forgotten on the box only when the phone has unsent writes (`dirty`, a count shared by its
tabs), otherwise the box's copy comes back. An owner's change (the poll's `memories` moved)
reloads the page when idle, unless this load already merged that version (what it could not
fetch waits for the next load). Moved, deleted or a G2 session: every persona's faces leave the
phone, except, for a G2 answer (which may be a lapsed session), while some are unsent. Other
personas' records leave the phone at load.

The body has one rider. A phone's relay socket asks the api's `/internal/growbot/body/<code>`
who wears that robot: a persona session joins only if its persona wears it from this phone, a
G2 session only its ticket's robot while no persona wears it, and an api that cannot say is a
503 the transport retries; a session refused eight times in five minutes gets 429. Every 10 s
each phone's socket, and its agent side channel, is asked again, and one that no longer rides
(a hand-over, a *Who wears* pick) is closed with 1008; a failed lookup keeps it on, so an api
blip never stops a walking robot.

## Tests

The backend suite loads `server/serve.py` by path (`backend/tests/unit/test_growbot_server.py`:
static, CSP, cookie gate, `/open`, persona sessions, each shim route against fake api/tts/stt
servers, the relay, its rider check and WebSocket framing) and runs the Caddy site in a real Caddy
(`backend/tests/unit/test_growbot_caddy.py`, `test_lan_proxy.py`). CI's backend path filter
includes `growbot/**`.
