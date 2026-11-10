# GrowBot on the box — a LAN-only brain for the desk robot

> **Status:** Scheduled · **Last verified:** 2026-11-10 · **Waves:** G1◻️ G2◻️ G3◻️ G4◻️

The owner's ask (2026-11-10): *"I want to run it similar to my local jpanel and jwall … I
want everything in this repo under a sub dir"*, after *"I don't want to send data beyond my
local network, and want more control over the settings."* The robot is a GrowBot
(growbot.dev) kit: a Raspberry Pi Pico 2 W on a Waveshare Pico Servo Driver with two MG90S
servos, whose "creature" brain is a web app on a phone. Stock, every thought, word, leg
command and diary line leaves the house. This plan brings all of it onto the box.

## 1. What upstream is, measured

Read from `github.com/britcruise9/GrowBot` (the `app/` folder is a byte-for-byte copy of the
browser code growbot.dev serves, snapshot of private commit `17e8689c`, 2026-10-13) and the
body kit zip.

- **Licence.** PolyForm Noncommercial 1.0.0 for code, CC BY-NC 4.0 for docs. A personal,
  noncommercial copy in this repo is permitted; the licence text and any `Required Notice:`
  lines travel with it. The GrowBot name and logo are trademarks, so the copy is never
  republished as a product.
- **The creature** is `app/v3/next.html` (1.5 MB, one inline script) plus `app/v3/*.js`
  modules, the root walk/spin policy runners, and `relay-transport.js`.
- **The body link.** The Pico dials *out* over TLS to a Cloudflare Worker relay
  (`relay_chip.py` `HOST = "growbot-relay.growbot.workers.dev"`, port 443, path
  `/d/<code>`); the phone joins the same path. Envelopes are tiny JSON (`attach`, `pose`,
  `act`, `routine`, `stop` → chip; `hello`, `hb`, `ack` ← chip; the relay itself emits
  `status`, `alive`, `hb_ack`). An agent side channel `/d/<code>-agent` carries `menu`, `obs`,
  `verb`, `ack`. The relay worker is not published, but the protocol is fully specified in
  `relay_chip.py` and `relay-transport.js` ("the ONE source of truth"). MicroPython's
  `ssl.wrap_socket` there sends SNI and does not verify the certificate.
- **The server is not published.** The app calls `/api/config` (model lineup), `/api/device`
  (device token, credits), `/api/models`, `/api/llm?target=openrouter` and
  `/api/llm-stream?target=openrouter` (chat, SSE), `/api/llm?target=groq-audio|deepinfra-audio`
  (Whisper STT, multipart, `verbose_json`), `/api/fish-tts`, `/api/journal`, `/api/episodes`,
  `/api/gallery`, `/api/ev` + `/api/hit` (analytics), `/api/feedback`, `/api/redeem`,
  `/api/transfer`.
- **There is already a "local brain door"** (`llmCfg()` in next.html, localStorage
  `pb_llm_url`/`pb_llm_model`/`pb_llm_key`/`pb_llm_vision`): any OpenAI-style server takes the
  whole think lane, and with `pb_llm_vision=1` camera frames go to it as `image_url` parts.
  It does nothing for the relay, STT, diary or analytics, and the page enforces a tunnel for
  a LAN address. We do not lean on it: the shim (§2) answers `/api/llm` itself, so the stock
  code path runs against the box.
- **Vision uses the model.** Visual memory (`v3/visual-memory.js`) sends saved views and the
  current frame to the LLM to name, recall and compare people, objects and handwriting. Face
  follow and presence are on-phone (MediaPipe BlazeFace).
- **Third-party assets** served beside the code and not in the repo: MediaPipe tasks-vision
  0.10.18 (Apache-2.0), ONNX Runtime Web 1.19.2 (MIT), Silero VAD (MIT), meSpeak 2.0.7 (GPL).

## 2. Shape on the box — wall's pattern, jpanel's flashing

Everything GrowBot lives under one top-level directory, `growbot/`:

```
growbot/
  upstream/        pristine copy of britcruise9/GrowBot app/ + firmware/ at a pinned commit, LICENSE
  patches/         the small, reviewed diffs we apply (relay URL, CSP-clean asset paths, no analytics)
  vendor/          the third-party assets, pinned by SHA-256
  firmware/        relay_chip.py et al. as flashed: HOST → jbrain.local, device key, Wi-Fi from the box
  server/          serve.py: static app + /api shim + relay (stdlib + one websocket lib)
  sync.sh          re-snapshot upstream at a new commit and re-apply patches/ (fails loudly on a conflict)
  README.md        the sidecar's Living doc (what it is, env, security)
```

The pieces that by nature live elsewhere stay where the repo keeps them: the compose service
in `deploy/docker-compose.yml`, its `Dockerfile.growbot`, a Caddy site in
`proxy-lan-conf.sh`, the Ops listing in `OpsScreen.tsx`, the board entry in the endpoint
flasher, a `CALLERS` entry in `internal_auth.py`, the CI path filter, and this plan.

**The `growbot` service** follows `wall`: a bare Python image, `growbot/` bind-mounted
read-only, so Ops → Update ships page changes with no rebuild. Default-on (no compose profile),
since a profile would need a `.env` edit (rule 10). It holds no secrets beyond its scoped
token and touches no RLS table.

**Its own origin over the box's TLS.** The app uses root-absolute paths (`/api/…`, `/v3/…`),
which collide with the PWA's, and the phone grants camera and mic only to a secure page. So
Caddy serves a second LAN name, `https://growbot.local`, with the same `tls internal` CA as
`jbrain.local`, published by `avahi_alias.py` beside `jbrain.local`. Its own origin also keeps
it off the authed app's cookie, as the wall's own port does.

**No egress, enforced twice.**
- Browser: every response carries `Content-Security-Policy: default-src 'self'` (plus
  `connect-src 'self' wss://growbot.local`, `media-src 'self' blob:`, `img-src 'self' data:
  blob:`, `worker-src 'self' blob:`). With that header the page cannot reach a non-box host
  even if a patch misses a URL. A CI test loads the patched app in headless Chromium and fails
  on any request to a host other than the box.
- Server: the shim never calls out. It talks to `api` and `tts-stt` by compose name only.

**The `/api` shim** (`growbot/server/serve.py`):

| upstream call | on the box |
|---|---|
| `/api/config`, `/api/models` | a static lineup naming the box's models (`fast`/`active`/`dream`), owner-tunable from a settings file |
| `/api/llm[-stream]?target=openrouter` | the api's residency-aware local-LLM proxy (the `jcode_llm.py` precedent) with a `growbot` bearer, never the llama-swap gateway directly (the compose:523 rule) |
| `/api/llm?target=*-audio` | `tts-stt` whisper `/v1/audio/transcriptions`, `verbose_json` passed through |
| `/api/fish-tts` | `tts-stt` Kokoro `/tts` (WAV), so the creature can have a real voice with no account |
| `/api/device`, `/api/redeem`, `/api/transfer` | one owned device, no metering |
| `/api/journal`, `/api/episodes`, `/api/gallery` | stored on the box in a volume, readable by the owner |
| `/api/ev`, `/api/hit`, `/api/feedback` | dropped (204) and the scripts patched out |

**The relay** is the same process, at `wss://growbot.local/d/<code>` and `/d/<code>-agent`,
reimplementing the envelopes above. Unlike upstream it is not open: the chip side presents the
device key minted at flash time, and the phone side is the LAN origin only.

**Models.** The think lane goes to whichever engine is up; the default lineup names a
vision-capable catalog model (`qwen3-vl-30b`, or `qwen3.8-27b` on the Flash-Next engine) so
visual memory works. Upstream's measured budget is ~600–900 prompt tokens and ~10–25 reply
tokens per turn, so latency is dominated by the image prefill on vision turns only.

## 3. Flashing — jpanel's path, a Pico board

The Pico is plugged into the **box's** USB, and the owner flashes it from **PWA → Endpoints →
Flash**, as the panels are. A blank RP2350 enumerates as its USB bootloader with no button,
so the first flash needs no BOOTSEL press.

1. `picotool load` the pinned MicroPython UF2 (`RPI_PICO2_W-20260406-v1.28.0.uf2`) and reboot.
2. Over the board's USB serial (`/dev/ttyACM*`, cgroup 166, already allowed), `mpremote cp`
   the firmware set with a generated `secrets.py`: the box's Wi-Fi list, `HOST =
   "growbot.local"` (with the box's IP as a fallback, since MicroPython has no mDNS
   resolver), and a fresh device key. Reflashing revokes the old key.
3. Read `PAIRING CODE: gb-…` from the console and show it in the PWA with a link that opens
   `https://growbot.local/v3/next.html?id=<code>&mode=legs`.

A reflash of a board already running MicroPython uses `mpremote` alone (or
`machine.bootloader()` then `picotool` for a new MicroPython). The endpoint sidecar gains
`picotool` and `mpremote`; it stays on the `internal: true` `panel` network.

## 4. Waves

- **G1 — the box serves the creature.** `growbot/` with `upstream/` + `patches/` + `vendor/`
  + `sync.sh`; `serve.py` (static, CSP, shim for config/device/llm/stt/journal, relay); the
  compose service, Caddy site, avahi alias; unit tests for the shim and relay by path, the
  headless no-egress test, the `CALLERS` entry and its auth test. Proven with the stock body
  firmware, `HOST` edited to the box.
- **G2 — the PWA flashes the robot.** Pico 2 W as an endpoint board: `picotool` + `mpremote`
  in the endpoint sidecar, the generated `secrets.py`, device key mint/revoke, the pairing code
  and link in the PWA.
- **G3 — settings and voice.** A small settings page on `growbot.local` (models per lane,
  vision on/off, Kokoro voice, motion duty budget) backed by the shim's settings file; Kokoro
  for speech.
- **G4 — the owner's things.** The diary readable in the PWA; Ops listing and health; an
  optional bridge that offers a day's diary to JBrain as a note (owner-confirmed, never
  automatic).

## 5. What only the owner can do

Build the body and put lithium AAs in; plug the Pico into the box's USB once; open the link on
the phone and accept the box's certificate (or have the root installed — the open gap named in
`../runbooks/LOCAL_ACCESS.md`). Everything else is PWA-operable.

## 6. Open questions

- Whether the owner's phone browser already trusts the box's root, or G1 also needs the
  root-download page that `LOCAL_ACCESS.md` names as a gap.
- Which local engine is usually up, which fixes the default vision model.
- Upstream moves fast (dated build tags in every comment). `sync.sh` keeps the patch set
  small enough to re-apply; a sync is its own PR.
