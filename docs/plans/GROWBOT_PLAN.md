# GrowBot on the box — a LAN-only brain for the desk robot

> **Status:** In progress · **Last verified:** 2026-11-13 · **Waves:** G1✅ G2✅ (built; first real flash pending) G3◻️ G4✅ (G4a✅ G4b✅ G4c✅ G4d✅) G5◻️ · personas stand apart from the robot body (owner, 2026-11-10)

The owner's ask (2026-11-10): *"I want to run it similar to my local jpanel and jwall … I
want everything in this repo under a sub dir"*, after *"I don't want to send data beyond my
local network, and want more control over the settings."* The robot is a GrowBot
(growbot.dev) kit: a Raspberry Pi Pico 2 W on a Waveshare Pico Servo Driver with two MG90S
servos, whose "creature" brain is a web app on a phone. Stock, every thought, word, leg
command and diary line leaves the house. This plan brings all of it onto the box.

**Away from home (owner, 2026-11-11).** Asked whether a persona could open from the PWA at work,
the owner chose an away-from-home address knowing Cloudflare would carry its traffic, as it does
the PWA's: `growbot.<host>` through the tunnel, behind the same ticket and session. The robot
still dials in on the LAN. See growbot/README.md "Away from home".

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
  patches/         upstream.json (the pinned upstream commit + every vendored asset's URL and
                   SHA-256), apply.py (exact, counted replacements that fail loudly), growbot-box.js
  site/            the served app, BUILT by apply.py and committed: upstream app/ patched, plus
                   the vendored assets at their upstream URL paths and csp.json
  firmware/        upstream's MicroPython, patched to dial the box with its key (also built)
  server/          serve.py: static app + /api shim + relay
  sync.sh          fetch upstream at the pin and re-run apply.py; a sync is its own PR
  LICENSE, UPSTREAM  upstream's licence and the pin, carried with the copy
  README.md        the sidecar's Living doc (what it is, env, security)
```

The pieces that by nature live elsewhere stay where the repo keeps them: the compose service
in `deploy/docker-compose.yml`, its `Dockerfile.growbot`, a Caddy site in
`proxy-lan-conf.sh`, a new api router for the model lane, its own `growbot` caller in
`internal_auth.py` (deliberately not in `CALLERS`: its token lives alone in `growbot_secrets`), the board entry in the endpoint flasher, the Ops listing in
`OpsScreen.tsx`, the CI path filter, and this plan.

**The `growbot` service** follows `wall`: a bare `python:3.12-slim` image with a stdlib-only
server (HTTP/1.1 and RFC 6455 written in-house, no new dependency), `growbot/` bind-mounted
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
  'wasm-unsafe-eval' <hashes> 'unsafe-hashes'; style-src 'self' 'unsafe-inline'; img-src 'self'
  data: blob:; media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self' blob:;
  font-src 'self' data:; form-action 'self'; object-src 'none'; base-uri 'self';
  frame-ancestors 'self'`. Rather than rewriting upstream's inline code, `apply.py` hashes every
  inline script and inline handler into `site/csp.json`, so `script-src` never needs
  `'unsafe-inline'`, and the build refuses a handler shape it cannot hash.
  `'wasm-unsafe-eval'` is for ONNX Runtime and MediaPipe; `blob:` in `connect-src` is for the
  page's own iOS audio path. With that header the page cannot reach a non-box host even if a
  patch misses a URL. CI has no browser, so CI scans the built site for any runtime load of an
  absolute URL or a third-party origin; the wave's own verification loads it in real Chromium
  and checks for zero off-box requests and zero CSP violations.
- Server: the shim never calls out. It talks to `api` and `tts-stt` by compose name only.

**The `/api` shim** (`growbot/server/serve.py`):

| upstream call | on the box |
|---|---|
| `/api/config`, `/api/models` | the box's settings (§2b): model per lane (`fast`/`active`/`dream`), vision, voice |
| `/api/llm[-stream]?target=openrouter` | a new api router (`api/growbot_llm.py`), residency-aware with its own slot role, authenticated by `growbot.token` — the `jcode_llm.py` shape, not its routes or token. Never the llama-swap gateway directly (compose's "freeze the host" rule) |
| `/api/llm?target=*-audio` | `tts-stt` whisper `/v1/audio/transcriptions`, its `verbose_json` normalised to the Groq shape upstream reads (`segments[].no_speech_prob`, `avg_logprob`), since whisper.cpp builds differ |
| `/api/fish-tts` | `tts-stt` Kokoro `/tts` (WAV). This is the creature's only voice (§2a) |
| `/api/device`, `/api/redeem`, `/api/transfer` | one owned device, no metering |
| `/api/journal`, `/api/episodes`, `/api/gallery`, `/api/ev`, `/api/hit`, `/api/feedback` | 204, stored nowhere (a `DELETE` of journal or episodes answers `{"ok":true}`, which the app's erase flow reads). The feedback widget is not served; `analytics.js` is, because the page calls it unguarded, but it only posts to `/api/ev`, which goes nowhere. The creature's memory stays on the phone until G4 |

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

### 2a′. The faces: lantern by default, the panels' ostrich among them

The owner (2026-11-10) did not like upstream's face and chose the panels' ostrich from three
mocks (`docs/mocks/growbot-face/`; binding spec in `docs/reference/DESIGN.md` "GrowBot — the
creature's face"). `patches/growbot-face.js` draws it at full resolution over upstream's pixel
sky from the same state upstream drew its own eyes from, so the brain still sets only
`emotion`; the first new brain verb is `coat`, which recolours it. Since 2026-11-11 the face is
each persona's own (migration 0259, `growbot_personas.face`), lantern by default (owner): picked
on the persona's *Manage* tab, read by the phone with the soul and changed at its next poll.
The fifth face, the ember (migration 0264), is a single glowing lens for the owner's adult
persona, Dave (owner, 2026-11-12), whose light moves with the measured loudness of its voice.

The second brain verb is `eye` (owner, 2026-11-12: switch between the front and back camera
when the phone is off the body): `front` or `back`, heeded only on a turn that answers something
the person said, since upstream retired a free `look` verb after the model flipped lenses
constantly. The lens is upstream's `gb_eye` key, which the persona's settings record (§2b) keeps
on the box; the owner picks it on the persona's *Manage* tab (*Camera*), and the change reaching
the page as `gb-box-prefs` flips the camera where it stands.

### 2b. Settings live on the box, and the PWA owns them

The owner's ask (2026-11-10): *"make sure the pwa can change the growbot settings, not just
through the … growbot phone app."* Upstream keeps every setting in the phone's localStorage
(`pb2_voice`, `gb_brain`, `gb_effort`, `gb_low_power`, `gb_walk_cal`, `pb_llm_*`, …), so a
change made on one phone is invisible everywhere else.

- **The pattern is `panel_settings.py`'s** (the desk panel's settings from the PWA: asked for
  here, taken by the device, reported back): a `growbot_settings` table, one row for the house's
  robot (there is one body; personas carry only the soul, §2c, so model and walk settings
  stay with the robot and apply to every persona, while the voice became each persona's own,
  like its face, on 2026-11-12 at the owner's ask, picked on its Manage tab; only the phone
  riding the body reports into it or takes its queue, so a persona open on a phone alone can never overwrite the leg
  calibration), holding what the phone app last reported and the queue of what the owner asked for, under RLS
  with its isolation test (rule 3).
- **The PWA** gets a GrowBot card (Settings, beside the panels) showing the settings as the app
  last reported them. A change made there is queued and reads "Waiting for GrowBot" until the
  app reports the new value, then "On GrowBot". The card covers the model per lane, vision
  on/off, the voice's speed and pitch (`tts_server.py` takes per-request `speed` and
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
  creature is idle, which is safe because its memory is in localStorage and IndexedDB. The card says which
  kind each setting is.
- The model list the PWA offers is the box's catalog filtered to installed models, with
  vision-capable ones marked.
- **The leg calibration landed first, and simpler** (owner, 2026-11-12: *"we also need to be
  able to trim the legs … anything they could do in the original application"*). Upstream
  reads `gb_walk_cal` at every move, so it needs no queue, report or reload: the box is the
  only writer (`app.growbot_robots.cal`, migration 0261, set from the Body tab's Legs pane,
  `api/growbot_body.py`) and `growbot-box.js` copies it into each page, the box winning key by
  key. The pane's check moves go chip-direct (`POST /internal/robots/act`). See
  growbot/README.md "Leg calibration".
- **The rest of the creature's keys followed, per persona** (owner, 2026-11-12: *"the cell
  phone should just be a display, not a data storage device"*). Rather than the report-and-queue
  table above, every localStorage key that is the creature's (coat colour, games guide, volume,
  speech speed, camera, low power, remembered `?flags`; `growbot-box.js` `KEYS`) lives in
  `growbot_personas.prefs` (migration 0263): pulled into the page before it runs, each change
  sent back as a per-key patch, and an owner's `PATCH …/prefs` taken where the creature stands
  on the next poll. Model and effort pickers are cloud-only and are being removed from the
  page rather than synced. See growbot/README.md "Settings".

### 2c. The creature lives on the box, as profiles the owner can switch

The owner's ask (2026-11-10): *"by living on the server, we can use other phones if we need
to"*, then, on moving the creature's memory too: *"Yes. Along with bot profiles we can change."*

Upstream keeps the whole creature on one phone: `v3/soul.js` (soul_format 3) holds it in **one
localStorage key, `pb2_soul`**: the memory surfaces (identity, the append-once bond line,
scratch, log), long-arc goals, and the swappable config regions (persona, dream, harness, body,
meta, expression). Visual memory (saved views of people, objects, handwriting) is in
**IndexedDB** (`v3/visual-memory.js`). Upstream already treats a soul as a portable document
(the MIND tab downloads `growbot-soul-snapshot.json`, and its soul gallery serves souls;
`/api/transfer` moves only device credits, not memories), and `prompts.js` appends its
child-safety floor after any loaded persona, so swapping a soul never removes the rails.

- **A profile is a soul, and it stands apart from the body.** The box stores profiles: each a
  `pb2_soul` document plus its visual memories and a name. A profile is not tied to a robot. The
  owner (2026-11-10): *"I plan on only having one physical robot that may be a couple different
  personas, and I want to be able to open the persona without the actual robot body."* So a
  profile is added on the GrowBot screen with no board present, and each profile has its own
  *Open* that runs it on the phone alone. The owner, next: *"assume that we'll only ever have one
  robot buddy but multiple personas that I can [load] up on a phone, and then when I hit connect to
  body it would actually connect to that body, or it can live alone on the phone app."* So there
  is one body, and *Connect to body* joins the house's one robot; whichever persona connected
  last is the one the body wears. Then: *"the primary jbrain pwa should be able to pair a robot
  creature to a body. But the creature app on a given phone should not have the ability to pair
  from that app to the body; that is reserved only for the primary jbrain application."* So
  *Connect to body* is on the JBrain PWA's persona list, never in the creature. The box enforces
  it: the PWA's ticket names the robot, the `gb_session` it buys carries that code under its
  HMAC, and the sidecar refuses any relay socket for another code, so a code typed into the
  creature's own pairing box opens nothing (built with the installable app, ahead of G4). Until then
  (or with no robot paired) the persona lives on the phone alone. The owner can keep several (a
  different persona, a fresh creature, a saved copy before an experiment).
- **Storage.** Through the api into Postgres under RLS with its isolation test (rule 3).
  Visual memories are camera-derived images of people, so their blobs go through the storage
  abstraction (rule 2) under their own namespace. It is not a new firewall domain (the domain
  set in `agent/contracts.py` stays as it is); instead the profile tables are owner-principal-only
  under RLS, a phone reads and writes only the persona it was opened as (the `gb_session` from
  that persona's ticket names it, under its own RLS context), the `growbot` sidecar's
  `growbot.token` routes read only which persona and phone ride the body (the robot itself holds
  only its device key, §3, and learns nothing of personas), and profiles are never readable by share or member sessions, agent tools, RAG
  or wiki ingest. Each of those has an isolation test.
- **Checkpoints and seeds (owner, 2026-11-10): *"from the jbrain pwa review all the saved
  files, the soul file etc. And memories. And then if I want I can create a set point and I can
  use that as a restore point. And then we can make different personas that we can flash not
  just the default."*** The PWA shows each persona's soul and its visual memories. The creature writes its memory
  surfaces and goals itself, and the owner may correct each of them too (owner, 2026-11-12:
  *"basically all these should be editable by me"*): who it is, the bond line, right now (state
  and rules), wants (its own name and up to four wants) and the diary, each capped and cleaned
  where `soul.js` caps it, checkpointed first, and the identity and bond line stamped
  `identityAt` so the phone's merge keeps them; its dreams go on from the correction. The config regions (persona, expression, meta,
  dream, harness, body) are editable (owner, 2026-11-10: *"a way to edit personas files and
  make backups"*), each save checkpointed first and passed through the same
  `sanitizeSoulCfg` and size cap as an import. A
  *checkpoint* is a named, dated copy of a persona's soul and visual memories, kept on the box;
  *Restore* puts one back as the persona (after a confirm, and after saving the current state as
  a checkpoint first, so a restore is itself undoable). A *seed* is a persona's starting
  personality (upstream's persona and expression config regions, written in the PWA or taken from
  a checkpoint); *New persona* starts from the default seed or any saved one. Seeds and
  checkpoints pass the same `sanitizeSoulCfg` and size cap as an import, and sit under the same
  owner-only RLS as profiles.
- **Imports are untrusted.** An imported soul passes upstream's `sanitizeSoulCfg` and a size cap;
  visual-memory blobs are type- and size-checked.
- **Sync.** The phone app is a cache of the persona it was opened as. The patched-in loader pulls
  it at load (so any phone opened from the PWA becomes that persona) and pushes changes back as the
  soul is written (the fast loop and dreams both write through `saveSoul`), debounced, with a
  version so a stale phone cannot overwrite a newer soul. Two things move separately. A persona
  runs on one phone at a time: opening it on a second phone hands the persona over, and the first
  phone shows that it has moved. The body has one rider at a time: *Connect to body* on a phone
  takes the robot from whichever phone held it, and that phone's persona carries on alone on the
  phone, showing that the body has gone. The box keeps the body's rider (which persona, which
  phone) and the routes that set it, each with its auth test. What a profile carries: `pb2_soul` and the visual memories (upstream namespaces those
  by `vmNs()`: `pb2_sid`, else `gb_vm_ns`; the loader pins it to the profile id). What it does not: upstream's per-device keys kept
  outside the soul on purpose (`pb_device`, `pb_or_*`) stay with the phone, and on the box
  `/api/device` answers them for any phone.
- **Settled at the G4 gate (owner picked mock B, three tabs, 2026-11-10;
  `docs/mocks/growbot-personas/README.md` "What the owner's ask changes in the plan").**
  - A PWA edit (or restore, reset, import) is a newer soul: it bumps the box's version, so the
    open phone's next push is refused and the phone reloads the box's copy when idle, never
    overwriting it with its cache.
  - The body's rider can be set from the PWA (B's *Who wears Fish*): for a persona open on
    another phone, the box moves the rider and that phone takes the legs at its next check,
    without anyone touching it. Only the owner's session can do this; a phone's own session
    can take the body only by being opened wearing it.
  - The soul's `harness` and `body` regions apply while the persona is alone; while it wears
    the body, the robot's own settings (§2b) win.
  - "Launch a persona from a link" is the one-time, two-minute code 2 as a link (*Copy code 2
    as a link instead*). No standing per-persona link: it would be a credential. The installed
    app's `/start` reopens the persona and body its session was opened as.
  - A phone holds one persona (upstream's single `pb2_soul`): opening another there closes the
    first, which stays on the box.
  - Delete takes the persona's checkpoints with it, and the armed tap says how many.
  - Export is the soul alone; visual memories (faces) stay on the box. A checkpoint keeps them.
- **The PWA** lists the profiles on the GrowBot screen (the launcher tile), each with *Open* (on the phone alone) and
  *Connect to body*. It can add one, duplicate, rename,
  reset to a fresh creature (upstream's own seed), export and import a soul file, and delete
  (confirmed, since it erases memories). To change persona on a phone, open another one. The G4
  GUI gate rewrites DESIGN.md's GrowBot screen for this (a persona list with *Open* on each, in
  place of today's per-robot *Open GrowBot* and *Open GrowBot without a robot*).

**Data.** Until G4, the box stores nothing but the settings row (§2b) and the robot's device key (§3). Camera frames
pass through to the model and are not kept. The creature's memory (G4, §2c) and the diary (G5) go
through the api into Postgres under RLS with an isolation test (rule 3), and are treated as
sensitive: they name people the camera saw.

**Call log.** The owner asked (2026-11-12) for every prompt and reply kept, so the creature's
slowness and its answers can be analysed: `app.growbot_calls` (migration 0260, owner-only RLS,
kept a week). The model lane (`api/growbot_llm.py`) writes an `llm` row per turn after it
ends: the request as sent to the engine with each picture replaced by its media type and
base64 size (never the pixels), the reply rebuilt from the stream (words, thinking, tool
calls), the persona and robot the sidecar names from the phone's session, the engine's token
counts, and where the time went (`lock_ms`, `admit_ms`, `first_byte_ms`, `first_token_ms`, the
engine's own `engine_prefill_ms` / `engine_decode_ms`). The sidecar posts an `stt` row (the
transcript, the clip's length, whisper's time) and a `tts` row (the line spoken, Kokoro's time)
to `POST /internal/growbot/calls` on a one-worker pool after the phone has its answer, dropping
rows past 32 waiting rather than queueing without end. A failed write never fails a turn. Read
with the debug API: `GET /api/debug/growbot/calls` and `…/calls/summary`, which splits picture
turns from text ones (`../runbooks/DEBUG_ACCESS.md`).

**Speed work (owner, 2026-11-12: "shorten response time while keeping good results").** A warm
creature turn re-reads everything after its ~7k-token system message (~1,800 tokens text,
~3,150 with three frames, at ~310 tokens/s), and a turn whose system text changed or whose
cache was lost re-reads all of it (~21 s). The plan, reviewed by an independent agent: (0)
instruments first: each `llm` row carries its system message's length and hash, the summary
splits out cold turns with their reason, and `POST /api/debug/growbot/replay` runs an edited
logged request through the lane so a layout is tried on real turns before the page ships it;
(1) the fixed rules the page sends every turn (gait library, picture-memory contract, the
fixed self-description) move into the cached system message, every state-dependent sentence
staying live under a RIGHT NOW line (apply.py `FIXED_PATCHES`; done); (2) the picture floor and frame count measured live against quality; (3) the
system prefix saved to disk and restored after a restart (`jbrain.growbot.prefix`, done: after a
cold turn, once the creature is quiet 20 s, the lane primes its slot with the system message and
a one-character turn and saves it through the jerv prompt cache's per-role store, so only the
system text reaches disk; a turn after 5 minutes' quiet asks the store to restore it into the
slot if the slot is empty; only on a model with a role pool, where the creature has a slot of
its own), and creature priority (done: each turn stamps
`growbot_awake_until`, and while it has not lapsed, 3 minutes after the last turn, the worker
pauses its background loop as it does for the code-mode hold, for at most 20 minutes at a
stretch, then stamps nothing for 10 so the worker can catch up; Settings → Devices →
*GrowBot priority* turns it off);
(4) history made reusable. Thinking stays off. Found by the replay check: the page replayed the
creature's past replies as bare words, and the model copied that, answering about half its turns
in plain text cut after a word or two; past replies now replay as `{"say":...}` (apply.py
`HISTORY_PATCHES`), and the same turns then came back whole and well-formed. Round 2, from the
owner's first live session on that layout: the automatic reply-language line, which rewrites
itself once speech settles a language, moved from the cached system message to the live block
(it cost one ~28 s full re-read per session), and a spoken turn sends 1 camera frame unless the
scene, the carried phone or the body is moving (3 then), ~2 s less prefill a turn
(`ROUND2_PATCHES`). Speech is transcribed in pieces while the person talks (`STREAM_PATCHES`): after 4 s of
audio, each short pause or a full buffer sends what is held and listening goes on, and the end
of speech sends only the last piece; the creature gets one turn with the pieces joined. This also
ends upstream's 12 s cap, which silently dropped the rest of a longer message. An ordinary quiet beat now waits 90 s after the last activity
(upstream: 8 s), so the creature never cuts in just after the person stops talking
(`QUIET_PATCHES`). What that beat says is upstream's live 11-11 text, ported from growbot.dev
since GitHub's last snapshot (owner, 2026-11-12): "this time is yours: you may reach for one of
your wants with your own body" in place of "you are awake together", so a creature left alone
explores instead of waiting. It is a persona setting, *When it's quiet* on the Manage tab, on by
default (`growbot_personas.explore`), not upstream's `?quiet` URL switch, because the phone is
only a display. Upstream's matching "quiet settle", which thins the beats the longer nobody
acts, is left out: it saves cloud credits, and a local beat costs none (`QUIET_TEXT_PATCHES`).
The words ride the turn's own message, never the cached prefix.

**Reading the turn while the person talks (owner, 2026-11-13).** Once someone has been talking
0.6 s, the page sends the box the coming turn minus their words (the history, the live block
and the camera frames), ending in a placeholder message and asking for one token, and repeats it
at most every 5 s while they go on; the spoken turn then sends those same messages byte for byte
with the words as a message of their own, so the engine resumes after the frames instead of
re-reading them. The engine keeps a resumable checkpoint only at the start of a request's last
message, which is why the words are split off. Measured with replays of logged turns: prefill
7.8 s → 1.9 s, and the split layout's replies matched the old one's. A warm-up is used once, by
the next spoken turn, under 12 s old, and only if the history, system text, eye, state block
(scratchpad, goal and language lines, and self-stats compared without their numbers) and
picture-memory line are unchanged and its newest frame is under 10 s old; the turn then sees and
remembers the warm-up's frames, and its labels and self-stats numbers are up to 12 s stale. What
it could not know (a recall, an imitation guide) rides with the words, and a turn that wants
more frames than it holds ("look at this") is built as before. A refused or failed warm-up
(an HTTP error or an error in its body) is never used. Warm-ups log as `llm_warmup` in the call
summary and the brain log, do not stamp the creature awake (a television can start one), and are
refused off a slot pool, where one could load a model, which turns them off for the visit. An
autonomous thought waits only while the utterance that started the warm-up is open, and after
three utterances in a row that never became a turn the page stops warming until someone speaks
(`WARM_PATCHES`). Nothing reaches the model while the sleep screen is up: the dream a new visit
owes used to run there, before the wake tap, and now waits for it and 30 s more, so the creature
does not greet and fall asleep (`WAKE_GATE_PATCHES`).

**Talk style (owner, 2026-11-12).** Upstream's creature is written for small children: ~18-word
lines, its last 4 exchanges and 5 diary lines in view, and an adult could not hold a conversation
with it. A persona's talk style (its `gb_talk` setting, picked on JBrain's persona page under
Manage) is Kid, upstream's creature unchanged, or Grown-up: replies of up to ~45 words, its last
12 exchanges replayed (the ring now keeps 16), and a standing ask to log what the person tells
about their life as they say it. Older memory stays out of the turn: the owner wants it looked
up when needed, not carried every turn. The page reads the setting every turn; the
grown-up block sits in the cached system prefix, so a switch costs one cold turn. For every
persona, the dream's "one tiny thing to try" is tried once rather than every turn, and the
person's name is no longer a tag on most lines (`TALK_PATCHES`).

**Facts and recall (owner, 2026-11-12).** Stored memory stays out of the turn; the creature looks
it up. A `fact` reply field files what the person tells about their life in a fact book
(`soul.facts`, newest 400, merged across tabs like the diary); grown-up personas are asked to use
it as they hear things. When asked about something it cannot see, the creature answers with
`recall` (a few search words) and a short thinking-back line; the page searches the fact book,
the whole diary, bright moments, standing rules and this session's conversation, and a follow-up
thought answers with what it found. This session's conversation lives in page memory only: raw
past sessions are never searched, only what was logged, filed or dreamed from them. One search a
turn, only on a turn that answers someone, never from the follow-up, at most one per 15 s; if the
person speaks meanwhile or the creature cannot think, the find
rides their turn (`RECALL_PATCHES`). While it searches, its line only says it is thinking back, never an answer or "I don't know"; and what it found stays in front of it for the next 90 s of its person's turns, since its answer is often talked over and a re-ask must not meet "I don't remember" (found on the box, 2026-11-12).

**Its name.** Upstream's seed says "I am Sprout" and the creature's own name (`goals.selfName`) is set only when its person confirms one, so a persona named Dave woke up as Sprout. A box creature born fresh (a new, reset or seeded persona) now starts out with its persona's name; one already awake keeps its own, which the owner edits under Soul as "Calls itself" (`NAME_PATCHES`, owner 2026-11-12).

**Sleep (owner, 2026-11-12).** Upstream dreamed every 12 replies, so a long conversation was
broken by dreams. Now a dream comes when the person lets the creature sleep (a `sleep` reply
field), after 5 quiet minutes with the page open (the visit ended), or at the first lull of a
new visit when the last one was never dreamed, since a closed page cannot dream. A long visit (60
replies, or 20 and half an hour awake) makes it sleepy: its turn says so, and it asks once, at a
natural pause; its permission to sleep lasts three minutes and outranks the dream cooldown. The
conversation since it last slept (newest 80 exchanges) rides the soul, so a dream after a reload
or on the next open still sees it; that dream writes a session summary to the diary and up to 5
facts to the fact book, then clears it, so the dream is the visit's long-term memory. Leave to sleep also ends any seek or mission under way, which would otherwise hold the dream back, and "go to sleep" or "go to bed" is leave to sleep, never a place to seek (found on the box, 2026-11-12). The moon button still dreams on demand (`SLEEP_PATCHES`).

**Brain log.** The owner asked (2026-11-12) for a brain log he can hand upstream's developer:
pick 1, 3 or 6 hours and download one zip with what debugging needs. Upstream's own "brain
log" is the creature's diary (`flushLog` in next.html posts a rolling window of its inputs,
goals and actions to `/api/journal` every 8 s, human speech already hashed by `redactQ`), so
the sidecar now forwards each snapshot's rows to `POST /internal/growbot/journal` on the call
log's worker (its soul peek and join key stay behind) and answers `{"ok":true}`; a watch-id
registration is still upstream's cloud link and is kept nowhere. `app.growbot_journal`
(migration 0262, owner-only RLS, kept a week) keeps each row once, keyed by diary id, row
time and a hash of the row, since every snapshot resends rows it sent before. The PWA's
Body tab *Share a brain log…* (DESIGN.md "GrowBot — the brain log") posts to `POST
/api/growbot/brain-log` and gets `growbot-brain-log-<stamp>-<N>h.zip`: `calls.jsonl` (every
call-log row in the window, oldest first, a long system prompt written once under `prompts/`),
`summary.json` (the call log's latency summary), `diary.jsonl` and `diary-pages.json` (the
diary rows and each page load's header), `box.json` (the personas and robots in the window,
numbered, and the models that answered), `manifest.json` and a README for the developer. It
leaves the house, so it carries no ids or secrets of the box's: personas, robots and diaries
are numbered, codes and ids inside text are swapped for those numbers, and bearer-shaped
tokens are cut. Redacted (the default) applies upstream's own rule with upstream's own djb2
`h8`, so a hashed line joins to the diary's: quoted spans become `«h8·Nw»`, a transcription
and the model's reasoning are hashed whole, the creature's words stay. A `.local` LAN name
makes upstream's page count as staging and write its diary raw, so the box hashes the diary's
speech fields (`txt`, `words`, … `SPEECH_KEYS`) whole itself, whatever the page did. Names
read `[name]` everywhere, in either mode: the ones the owner lists (kept on his phone, never on
the box) plus the ones the box already holds (each soul's `goals.owner`, the people named in
visual memories, persona and robot names). The sidecar keeps at most one snapshot per phone
every 2 s, an export builds off the event loop one at a time (409 while one runs), and a zip
stops taking rows past 96 MB, with `cut` in its manifest. A debug token
reads the same zip, always redacted, at `GET /api/debug/growbot/brain-log?hours=`.

**The relay** is the same process, at `wss://jbrain.local:8444/d/<code>` and
`/d/<code>-agent`, reimplementing the envelopes above plus the key check.

**Models.** The default lineup names a vision-capable catalog model so visual memory works:
`qwen3-vl-30b` or `qwen3.8-27b` on the standard engine, `qwen3.8-flash-next` when Flash-Next
is the engine. Upstream's measured budget is ~600–900 prompt tokens and ~10–25 reply tokens
per turn, so latency is dominated by the image prefill on vision turns only. Measured on the
box (2026-11-12): the served 2048-token picture floor blew each 320 px frame up to ~2,100
tokens, three frames a picture turn, ~33 s of prefill against ~5 s for a text turn. The lane
now asks for its own floor per request (`CREATURE_IMAGE_MIN_TOKENS` = 256, read by Flash-Next's
patched engine, `deploy/apply-llama-image-floor.sh`), so note photos keep 2048. STT needs the box's
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

**The clock.** MicroPython's mbedTLS checks a certificate's validity dates against the RTC,
which boots in the past, and Caddy's `tls internal` leaf certificates live hours, so a robot
with no internet would fail `CERT_REQUIRED` as "not yet valid". The `growbot` service answers
SNTP (RFC 4330 server mode: stateless, only a well-formed 48-byte client request, a same-size
reply) on UDP `8123`, published on the LAN; `secrets.py` carries `GROWBOT_NTP_PORT`. The
firmware sets `machine.RTC` from it before every verified dial (three tries, the watchdog
fed), falls back to `ntptime` against the Wi-Fi gateway, and refuses to dial with a clock
before 2026, printing `handshake: FAIL clock` (fail code `clock_unset`). A rekey refuses a
robot without exactly one string `GROWBOT_CA` line (`needs_reflash`), since its new key would
dial unverified.

The endpoint sidecar gains `mpremote` (pip) and `picotool` (apt or a source build; RP2350 needs
picotool 2.x), in `Dockerfile.endpoint` and `dev-setup.sh`. `picotool` talks raw USB, which needs
`c 189:* rmw` in `device_cgroup_rules`: that opens every USB device node to the container, so it
is a security-reviewed change. The sidecar stays on the `internal: true` `panel` network.

## 4. Waves

- **G1 — the box serves the creature.** No new GUI: the upstream app as it is. `growbot/` with
  `site/` + `firmware/` (built) + `patches/` + `server/` + `sync.sh`; `serve.py` (static, CSP, shim, relay with
  key check, ticket exchange); the compose service, the `:8444` Caddy site (and a test that 8443 stays unpublished), `growbot_secrets`,
  `api/growbot_llm.py`, the ticket mint, the `growbot` caller and its `growbot_lane` network. Tests: shim and relay by path,
  the api router and its auth (security paths at 100%), the Caddy site via the existing
  real-Caddy tests, the headless no-egress test. Proven with the stock body firmware hand-edited
  to the box. ✅ Built: until G3's per-lane settings the box picks one creature model (the
  serving engine's first installed vision model, else its first) and ignores the name the page
  sends, so no lane can cold-swap the box; a turn that may swap takes jcode's swap lock.
- **Installing the creature (owner, 2026-11-10): *"can you verify that the app will be able to
  be installed as a pwa on a Android device?"*** Done between G2 and G3: a manifest, icons and a
  service worker that caches nothing, a `/start` that reopens the creature on its robot, and a
  30-day session keyed from `growbot.token` so the installed app outlasts a night and a restart,
  with *Sign out every phone* on the GrowBot screen to end them all (`growbot/README.md`
  "Installing it on a phone").
- **G2 — the PWA flashes the robot.** GUI gate first (three mocks, `PROCESS.md`), covering the
  Endpoints board, pairing code, the open-GrowBot entry and the Ops listing. Then Pico 2 W as an
  endpoint board: `picotool` + `mpremote`, the generated `secrets.py`, device key mint/revoke.
  ✅ Built: flashing lives on the PWA's GrowBot screen (`api/growbot_flash.py`, one in-process
  job with load / copy / join phases), and the Endpoints board points a Pico there. The endpoint
  sidecar's `pico.py` drives `picotool` 2.1.1 (Debian trixie's package) and `mpremote` 1.28.0,
  with `c 189:* rmw` for raw USB; picotool is handed only the bus/address of a sysfs-verified
  2e8a:000f. The UF2 is vendored by `apply.py` into `growbot/firmware/` with `SHA256SUMS`, and
  the api reads it off a read-only mount. The key is minted only after the board has said which
  robot it is; a robot is paired once the relay stamps its `last_seen_at` (migration 0255 adds
  `name`, `revoked_at`, `last_seen_at`, and a NULL `key_hash` for a revoke). Measured, not
  guessed: the box's LAN IPv4 comes from AdGuard's `dns_addresses` (else the owner types it); the
  CA is written as base64 DER because `wifi_store` reads no bytes literal; a reset comes before
  every copy because a running robot's 8 s hardware watchdog would cut a copy off. The box's
  root is now a PWA download (`GET /api/lan/root.crt`), closing §6's first question. The robot
  sets its clock from the box's SNTP before a verified dial (§3). Not yet run on hardware: the
  first real flash is pending.
- **G3 — settings from the PWA.** GUI gate first (the PWA GrowBot card). The
  `growbot_settings` table, its RLS and isolation test, the owner routes, the app's pickup and
  report route, and the apply hook with idle reload (§2b).
- **G4 — the creature on the box, and profiles.** GUI gate passed (owner, 2026-11-10: mock B, three tabs,
  `docs/mocks/growbot-personas/b-three-tabs.html`; binding spec
  in `docs/reference/DESIGN.md` "GrowBot — the desk robot's screen"). Built in four PRs:
  - **G4a — personas on the box** ✅ (#1815). The persona and checkpoint tables under owner-only RLS with
    their isolation tests; the owner and debug routes (list, add from a seed, rename,
    duplicate, edit a config region, export, import, reset, delete, checkpoint save, list and
    restore); the sidecar's `growbot.token` routes that pull and push the soul of the persona a
    phone was opened as, with the version and holder checks; tickets that name a persona and
    whether it wears the body, and the body's rider, settable from the PWA's Body tab for a
    persona already open on another phone (B's *Who wears Fish*), with its own auth test.
  - **G4b — the phone follows the box** ✅. The `gb_session` carries the persona and the
    phone's id; the shim's soul routes; the relay lets only the rider's phone drive a body (a G2
    phone only while no persona rides it) and drops one that stops riding; the loader patch
    (pull before the page loads, push as the soul is written, an owner's edit and a hand-over
    noticed within a poll, the *it has moved* and *it was deleted* pages, and the creature's own
    pairing box answering that pairing is JBrain's). See `growbot/README.md` "Personas".
  - **G4c — the screen** ✅. The Personas, Body and Phones tabs, the launch sheet, *Who wears
    Fish*, the hand-over banners, *On another phone* with a persona and the body, and the
    persona page with its tabs (soul and editor, checkpoints, manage). "This phone" is learned
    from a launch here that lands (the creature's phone id is another origin's), and the
    launcher tile says who wears the body. Since 2026-11-12 the Body tab also moves the robot
    (owner: *"if the body is turned on … call the policies directly to have it try and do
    movements from my pwa commands"*): a remote laid out like upstream's Policy Remote
    (`tester/policy.html`) sends each move to the creature on the rider's phone over the
    side channel, since the policies run on that phone's motion sensors (README "Moves from
    JBrain").
  - **G4d — visual memories.** ✅ Migration 0258 (`growbot_memories`, owner-only under RLS
    with its isolation test); the pictures in the blob store's own `growbot-memories`
    namespace, deleted only once no memory or checkpoint names them. The phone syncs record by
    record against what both sides last agreed on (`growbot-box.js`), with apply.py pinning
    `vmNs()` to the persona and holding the creature's store until the box's records are in. A
    checkpoint keeps them and a restore brings them back; reset and import clear them (their
    checkpoint keeps them); duplicate copies them; export leaves them out. The persona page's
    *Memories* tab is the grid with an armed delete and a viewer.
- **Upstream policy watch (owner, 2026-11-12): *"occasionally the grow bot official repo will
  post up new policies … a way for the jbrain pwa to check"*.** ✅ The api checks upstream's
  commits feed every 12 hours and, once `main` has moved off the pin, lists the changed policy
  files with one GitHub compare call. It notifies the owner once per new upstream head that brings
  any, and shows them on the Body tab's *Walks and spins* card (GUI gate passed: mock A,
  `docs/mocks/growbot-policies/a-body-card.html`). Bringing them in is still a sync PR, and the
  card says so. See `growbot/README.md` "Upstream and patches".
- **G5 — the diary.** GUI gate first. The diary via the api into Postgres under RLS, readable in
  the PWA; optionally offered to JBrain as a note, owner-confirmed, never automatic. Its store
  landed early with the brain log (§2 "Brain log", migration 0262); reading it in the PWA and
  the note remain.

## 5. What only the owner can do

Build the body and put lithium AAs in; plug the Pico into the box's USB once; open GrowBot from
the PWA on the phone and accept the box's certificate (or have the root installed — the open gap
named in `../runbooks/LOCAL_ACCESS.md`). Everything else is PWA-operable.

## 6. Open questions

- Whether the owner's phone browser already trusts the box's root, or G1 also needs the
  root-download page that `LOCAL_ACCESS.md` names as a gap.
- Which local engine is usually up, which fixes the default vision model.
- Upstream moves fast (dated build tags in every comment). `sync.sh` keeps the patch set
  small enough to re-apply; a sync is its own PR. The box now says when one is due (the
  upstream policy watch, §4).
