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
- **The creature** is `app/v3/next.html` (1.5 MB; five inline `<script>` blocks and a few
  inline `on*=` handlers) plus `app/v3/*.js` modules, the root walk/spin policy runners, and
  `relay-transport.js`. Its own memory ("soul") lives in the phone's localStorage.
- **The body link.** The Pico dials *out* over TLS to a Cloudflare Worker relay
  (`relay_chip.py` `HOST = "growbot-relay.growbot.workers.dev"`, port 443, path
  `/d/<code>`); the phone joins the same path. Envelopes are tiny JSON (`attach`, `pose`,
  `act`, `routine`, `stop` → chip; `hello`, `hb`, `ack` ← chip; the relay itself emits
  `status`, `alive`, `hb_ack`). An agent side channel `/d/<code>-agent` carries `menu`, `obs`,
  `verb`, `ack`. The relay worker is not published, but the protocol is fully specified in
  `relay_chip.py` and `relay-transport.js` ("the ONE source of truth"). MicroPython's
  `ssl.wrap_socket` there sends SNI and does not verify the certificate. Upstream's relay is
  unauthenticated: the pairing code alone drives the legs.
- **The server is not published.** The app calls `/api/config` (model lineup), `/api/device`
  (device token, credits), `/api/models`, `/api/llm?target=openrouter` and
  `/api/llm-stream?target=openrouter` (chat, SSE), `/api/llm?target=groq-audio|deepinfra-audio`
  (Whisper STT, multipart, `verbose_json`), `/api/fish-tts`, `/api/journal`, `/api/episodes`,
  `/api/gallery`, `/api/ev` + `/api/hit` (analytics), `/api/feedback`, `/api/redeem`,
  `/api/transfer`.
- **There is already a "local brain door"** (`llmCfg()` in next.html, localStorage
  `pb_llm_url`/`pb_llm_model`/`pb_llm_key`/`pb_llm_vision`): any OpenAI-style server takes the
  whole think lane, and with `pb_llm_vision=1` camera frames go to it as `image_url` parts.
  It does nothing for the relay, STT, diary or analytics. We do not lean on it: the shim (§2)
  answers `/api/llm` itself, so the stock code path runs against the box.
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
  patches/         the reviewed diffs we apply (relay URL, inline scripts moved to files, no analytics)
  vendor/          the third-party assets, pinned by SHA-256
  firmware/        relay_chip.py et al. as flashed: box address + device key from a generated secrets.py
  server/          serve.py: static app + /api shim + relay
  sync.sh          re-snapshot upstream at a new commit and re-apply patches/ (fails loudly on a conflict)
  README.md        the sidecar's Living doc (what it is, env, security)
```

The pieces that by nature live elsewhere stay where the repo keeps them: the compose service
in `deploy/docker-compose.yml`, its `Dockerfile.growbot`, a Caddy site in
`proxy-lan-conf.sh`, a new api router for the model lane, a `CALLERS` entry in
`internal_auth.py`, the board entry in the endpoint flasher, the Ops listing in
`OpsScreen.tsx`, the CI path filter, and this plan.

**The `growbot` service** follows `wall`: a `python:3.12-slim` image (plus one pip-installed
websocket library, added to `scripts/dev-setup.sh` per rule 8), `growbot/` bind-mounted
read-only, so Ops → Update ships page changes with no rebuild. Default-on (no compose profile),
since a profile would need a `.env` edit (rule 10). It joins `edge` (so Caddy can reach it) and
`internal` (for `api` and `tts-stt`), and publishes no port of its own.

**Its own token, its own volume.** The api mints `growbot.token` into a dedicated
`growbot_secrets` volume, the way `enroll_secrets` is split out (`internal_auth.py`), rather
than mounting the shared `internal_secrets` volume that holds every caller's token.

**Its own origin over the box's TLS, with no new name.** The app uses root-absolute paths
(`/api/…`, `/v3/…`), which collide with the PWA's, and the phone grants camera and mic only to
a secure page. So `proxy-lan-conf.sh` writes a second site, `https://jbrain.local:8444`, with
the same `tls internal` CA, and the `proxy` service publishes 8444 (8443 is the unpublished owner mTLS site and stays so). A different port is a
different origin for script and storage, but browsers do not scope cookies by port, so the
owner's `__Host-` session cookie would still be sent there: the Caddy site strips the incoming
`Cookie` header down to growbot's own, distinctly named cookie, so the api session never
reaches vendored code. Reusing `jbrain.local`
needs no new mDNS alias, which would otherwise only be installed by a host update
(`update-inner.sh` runs `lan-setup.sh` only under `HOST_UPDATE`) — a terminal dependency.

**Access.** The LAN is not the boundary: anyone on it could otherwise drive the servos.
- Phone: the owner opens GrowBot from the PWA, which mints a short-lived ticket through the
  api; `growbot` exchanges it for its own session cookie on the `:8444` origin. No ticket, no
  page, no relay.
- Robot: the chip side of the relay presents the device key minted at flash time (§3); the
  relay checks it against the api. Reflashing revokes the old key.

**No egress, enforced twice.**
- Browser: every response carries a CSP of `default-src 'self'; script-src 'self'
  'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src
  'self' blob:; worker-src 'self' blob:; connect-src 'self'; font-src 'self'`. `patches/`
  moves next.html's inline scripts and `on*=` handlers into files so `script-src` needs no
  `'unsafe-inline'`; `'wasm-unsafe-eval'` is for ONNX Runtime and MediaPipe. With that header
  the page cannot reach a non-box host even if a patch misses a URL. A CI test loads the
  patched app in headless Chromium and fails on any request to a host other than the box, and
  on any CSP violation report.
- Server: the shim never calls out. It talks to `api` and `tts-stt` by compose name only.

**The `/api` shim** (`growbot/server/serve.py`):

| upstream call | on the box |
|---|---|
| `/api/config`, `/api/models` | the box's settings (§2b): model per lane (`fast`/`active`/`dream`), vision, voice |
| `/api/llm[-stream]?target=openrouter` | a new api router (`api/growbot_llm.py`), residency-aware with its own slot role, authenticated by `growbot.token` — the `jcode_llm.py` shape, not its routes or token. Never the llama-swap gateway directly (compose's "freeze the host" rule) |
| `/api/llm?target=*-audio` | `tts-stt` whisper `/v1/audio/transcriptions`, its `verbose_json` normalised to the Groq shape upstream reads (`segments[].no_speech_prob`, `avg_logprob`), since whisper.cpp builds differ |
| `/api/fish-tts` | `tts-stt` Kokoro `/tts` (WAV). This is the creature's only voice (§2a) |
| `/api/device`, `/api/redeem`, `/api/transfer` | one owned device, no metering |
| `/api/journal`, `/api/episodes`, `/api/gallery`, `/api/ev`, `/api/hit`, `/api/feedback` | 204, stored nowhere (the analytics and feedback scripts are also patched out). These are upstream's server-side telemetry; the creature's memory stays on the phone |

### 2a. Speech is the box's, every word

The owner's ask (2026-11-10): *"make sure we're using our tts stt containers for everything."*

- **STT** upstream is already server-side only: every clip goes to `/api/llm?target=*-audio`,
  and the app never uses the browser's Web Speech API (no `SpeechRecognition` anywhere). The
  shim sends it to `tts-stt` whisper, so all transcription is the box's. Silero VAD runs in the
  page, but it only detects speech and never transcribes.
- **TTS** upstream has three engines (`vtEngine`): `robot` (meSpeak, in the page), `device`
  (`speechSynthesis`, the OS or browser voice, which on Chrome can be a Google network voice),
  and `fish` (`/api/fish-tts`). `patches/` makes `fish`, answered by Kokoro, the default and
  only engine, removes `device` and `robot` from the picker, and maps upstream's fish voice
  settings (`vtFish` `{key, voice, model}`) to a Kokoro voice id, so no word is spoken by
  anything but `tts-stt`. `speechSynthesis.speak` also appears in `agent-gateway.js` and
  `tester/custom.html`; the patch covers every served file, and `tester/` pages that only
  exist for upstream's cloud setup are not served. If Kokoro is down, the creature shows the words instead of speaking
  them, with no silent fallback to another voice.
- A test scans every served file and asserts there is no reachable `speechSynthesis.speak` or
  meSpeak call, and that every speak goes to `/api/fish-tts`.

### 2b. Settings live on the box, and the PWA owns them

The owner's ask (2026-11-10): *"make sure the pwa can change the growbot settings, not just
through the … growbot phone app."* Upstream keeps every setting in the phone's localStorage
(`pb2_voice`, `gb_brain`, `gb_effort`, `gb_low_power`, `gb_walk_cal`, `pb_llm_*`, …), so a
change made on one phone is invisible everywhere else.

- **The pattern is `panel_settings.py`'s** (the desk panel's settings from the PWA: asked for
  here, taken by the device, reported back): a `growbot_settings` table, one row per robot,
  holding what the phone app last reported and the queue of what the owner asked for, under RLS
  with its isolation test (rule 3).
- **The PWA** gets a GrowBot card (Settings, beside the panels) showing the settings as the app
  last reported them. A change made there is queued and reads "Waiting for GrowBot" until the
  app reports the new value, then "On GrowBot". The card covers the model per lane, vision
  on/off, the Kokoro voice, speed and pitch (`tts_server.py` takes per-request `speed` and
  `pitch`), effort, low-power, the motion duty budget and the leg calibration.
- **The phone app is the reporter.** Through the shim, on a `growbot.token`-authed api route on
  an RLS-scoped session (with its auth test, security paths at 100%), it picks up the queue at
  load and every few seconds while open, applies it, and reports its full settings back. A
  change made in the app's own panels is simply its next report, so the PWA shows it. A queued
  owner ask is re-applied on the next pickup until a report matches it, so the PWA's change
  wins over an older one made on the phone.
- **Applying.** Upstream reads most settings into variables once at start-up (`vtEngine` from
  `pb2_voice`, for instance), so writing localStorage under a running page changes nothing.
  `patches/` adds a small apply hook that sets both the localStorage key and the live variable
  for each setting it can change in place (voice, speed, pitch, effort, low-power, duty budget).
  For the rest (the model lineup, leg calibration) the app reloads itself the next time the
  creature is idle, which is safe because its memory is in localStorage. The card says which
  kind each setting is.
- The model list the PWA offers is the box's catalog filtered to installed models, with
  vision-capable ones marked.

**Data.** G1–G3 store nothing on the box but the settings row (§2b) and the robot's device key (§3). Camera frames and transcripts pass
through to the model and STT and are not kept. If the diary comes onto the box (G4) it goes
through the api into Postgres under RLS with an isolation test (rule 3), and is treated as
sensitive: it names people the camera saw.

**The relay** is the same process, at `wss://jbrain.local:8444/d/<code>` and
`/d/<code>-agent`, reimplementing the envelopes above plus the key check.

**Models.** The default lineup names a vision-capable catalog model so visual memory works:
`qwen3-vl-30b` or `qwen3.8-27b` on the standard engine, `qwen3.8-flash-next` when Flash-Next
is the engine. Upstream's measured budget is ~600–900 prompt tokens and ~10–25 reply tokens
per turn, so latency is dominated by the image prefill on vision turns only. STT needs the box's
whisper model provisioned; without it the shim reports STT off and the creature's typed and
talk-button paths still work.

## 3. Flashing — jpanel's path, a Pico board

The Pico is plugged into the **box's** USB, and the owner flashes it from **PWA → Endpoints →
Flash**, as the panels are. A blank RP2350 enumerates as its USB bootloader with no button,
so the first flash needs no BOOTSEL press.

1. `picotool load` the pinned MicroPython UF2 (`RPI_PICO2_W-20260406-v1.28.0.uf2`), supplied
   by the api from the repo like panel firmware (the endpoint sidecar never fetches), and
   reboot.
2. Over the board's USB serial (`/dev/ttyACM*`, cgroup 166, already allowed), `mpremote cp`
   the firmware set with a generated `secrets.py`: the box's Wi-Fi list, the box's LAN IP and
   port 8444 to dial, `jbrain.local` as the TLS server name (so SNI matches the Caddy site;
   MicroPython has no mDNS resolver), and a fresh device key. A changed box IP means a
   re-provision over USB, so the box's DHCP lease should be reserved.
3. Read `PAIRING CODE: gb-…` from the console and show it in the PWA with the link that opens
   the creature for that code.

A reflash of a board already running MicroPython uses `mpremote` alone (or
`machine.bootloader()` then `picotool` for a new MicroPython).

The endpoint sidecar gains `mpremote` (pip) and `picotool` (apt or a source build; RP2350 needs
picotool 2.x), in `Dockerfile.endpoint` and `dev-setup.sh`. `picotool` talks raw USB, which needs
`c 189:* rmw` in `device_cgroup_rules`: that opens every USB device node to the container, so it
is a security-reviewed change. The sidecar stays on the `internal: true` `panel` network.

## 4. Waves

- **G1 — the box serves the creature.** No new GUI: the upstream app as it is. `growbot/` with
  `upstream/` + `patches/` + `vendor/` + `sync.sh`; `serve.py` (static, CSP, shim, relay with
  key check, ticket exchange); the compose service, the `:8444` Caddy site (and a test that 8443 stays unpublished), `growbot_secrets`,
  `api/growbot_llm.py`, the ticket mint, the `CALLERS` entry. Tests: shim and relay by path,
  the api router and its auth (security paths at 100%), the Caddy site via the existing
  real-Caddy tests, the headless no-egress test. Proven with the stock body firmware hand-edited
  to the box.
- **G2 — the PWA flashes the robot.** GUI gate first (three mocks, `PROCESS.md`), covering the
  Endpoints board, pairing code, the open-GrowBot entry and the Ops listing. Then Pico 2 W as an
  endpoint board: `picotool` + `mpremote`, the generated `secrets.py`, device key mint/revoke.
- **G3 — settings from the PWA.** GUI gate first (the PWA GrowBot card). The
  `growbot_settings` table, its RLS and isolation test, the owner routes, the app's pickup and
  report route, and the apply hook with idle reload (§2b).
- **G4 — the diary.** GUI gate first. The diary via the api into Postgres under RLS, readable in
  the PWA; optionally offered to JBrain as a note, owner-confirmed, never automatic.

## 5. What only the owner can do

Build the body and put lithium AAs in; plug the Pico into the box's USB once; open GrowBot from
the PWA on the phone and accept the box's certificate (or have the root installed — the open gap
named in `../runbooks/LOCAL_ACCESS.md`). Everything else is PWA-operable.

## 6. Open questions

- Whether the owner's phone browser already trusts the box's root, or G1 also needs the
  root-download page that `LOCAL_ACCESS.md` names as a gap.
- Which local engine is usually up, which fixes the default vision model.
- Upstream moves fast (dated build tags in every comment). `sync.sh` keeps the patch set
  small enough to re-apply; a sync is its own PR.
