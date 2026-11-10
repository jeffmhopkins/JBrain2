# growbot — the desk robot's brain on the box

> **Status:** Living · **Last verified:** 2026-11-10

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
diff in its own PR. `backend/tests/unit/test_growbot_site.py` holds the promises on the built
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
`JBRAIN_LAN_ADDR`, same `tls internal` CA as the LAN app) is the only way in. It is its own
origin because upstream's root-absolute paths (`/api/…`, `/v3/…`) collide with the PWA's.

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
has lapsed to a page pointing at JBrain, whose GrowBot screen mints the next ticket. Chrome
installs only from a site it trusts, so the phone needs the box's root certificate installed
(the GrowBot screen's *Download the box's certificate*), not a warning clicked through. On an
iPhone a home-screen app keeps its own storage apart from Safari's, so it starts a fresh creature
until G4 moves the creature onto the box.

## Security

- **Opening it.** The owner mints a one-time ticket through the api (`POST /api/growbot/ticket`;
  until G2 adds the PWA entry, through the owner debug API's copy). `GET /open?ticket=…[&id=<code>]` redeems it and sets `gb_session` (HttpOnly, Secure,
  SameSite=Strict, 30 days, renewed at most daily by any page load or call), then redirects to
  the creature. The cookie carries the robot code the ticket named under its HMAC, and the relay
  lets that phone join that robot alone: pairing is JBrain's, and a code typed into the
  creature's own pairing box is refused (`403 pair from JBrain`). The key is derived from
  `growbot.token` and `growbot-session.token`: nothing is stored, a restart keeps phones signed
  in, and *Sign out every phone* (the GrowBot screen, or the debug API's
  `POST /growbot/sessions/reset`) re-mints the second file, which signs every phone out at once. Every other route answers 401 without it, decided from
  the request head: without a session nothing but a body-less `/open`, `/start` or one of the
  installed app's four files is read, and the 401 closes the connection, so an anonymous client never
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
- **The api's routes.** `GET /internal/robots` (`{robots: [{code, since}]}`, the robots on
  the relay now) and `POST /internal/robots/drop` (`{code}`: close a revoked robot's socket,
  1008) take `Authorization: Bearer <growbot.token>`, compared in constant time; without it
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
  `document`) is 403 and never counted, and a cookieless socket that sends an `Origin` (a
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
| `/api/llm?target=groq-audio\|deepinfra-audio` | whisper, rebuilt as `model=whisper`, `response_format=verbose_json`, and normalised so `text` and every segment's `no_speech_prob` and `avg_logprob` exist (a missing probability reads as speech). 503 when whisper is down, never 429. |
| `/api/fish-tts` | Kokoro `GET /tts` with the text, a `kokoro-*` voice (else Kokoro's default), speed and pitch; WAV back. 503 when down, never 401/402, which upstream latches as a dead voice. |
| `/api/device` | One owned device, nothing metered. |
| `/api/redeem`, `/api/transfer` | 410: no credits on the box. |
| `DELETE /api/journal`, `DELETE /api/episodes` | 200 `{"ok":true}` (the app's erase flow reads JSON); nothing is stored to erase. |
| `/api/gallery?…&download=1` | 404: no soul or policy gallery on the box. |
| `/api/journal`, `episodes`, `gallery`, `ev`, `hit`, `feedback` | otherwise 204, stored nowhere. |

## The relay

`wss://jbrain.local:8444/d/<code>` reimplements upstream's relay (`relay_chip.py` and
`relay-transport.js` define the envelopes). A socket with a valid session is a phone; one
without must be the chip (above). Phone frames go to the chip, chip frames go to that code's
phones. The relay sends phones `{"t":"status","awake":0|1}` when they attach and when the chip
comes or goes, and `{"t":"alive","quiet_ms":N}` every 30 s while a heartbeating chip is attached
(`quiet_ms` is the time since its last message, heartbeats included and pongs not). It answers the chip's `{"t":"hb"}` with
`{"t":"hb_ack"}`, pings every socket every 20 s, and drops any socket silent for 45 s (a robot
that loses power never closes its socket). A second chip on a code replaces the first.

`/d/<code>-agent` is upstream's side channel for an external agent: the creature page joins with
`hello` and agents with `attach`, both behind the session cookie, and frames are bridged both
ways. It never meets the robot's channel.

## Tests

The backend suite loads `server/serve.py` by path (`backend/tests/unit/test_growbot_server.py`:
static, CSP, cookie gate, `/open`, each shim route against fake api/tts/stt servers, the relay
and WebSocket framing) and runs the Caddy site in a real Caddy
(`backend/tests/unit/test_growbot_caddy.py`, `test_lan_proxy.py`). CI's backend path filter
includes `growbot/**`.
