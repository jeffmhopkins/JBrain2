# server-brain — neural wall display

A dark, glowing neural-network animation of the JBrain2 host's live status, for
the otherwise-blank terminal/monitor on the box. The brain fires in travelling
flood-ping cascades driven by real host vitals:

| Visual | Signal | Source |
| --- | --- | --- |
| **Neural activity** (cascade rate/brightness) | GPU utilisation | amdgpu `gpu_busy_percent` |
| **Density** (how full/present the web looks) | RAM in use | `/proc/meminfo` |
| **Bloom / heat** (white-hot glow, gold tint) | APU power draw | amdgpu `power1_average` |
| global mood tint (ok/warn/crit) | health | derived from GPU/RAM/temp/load |

`index.html` is fully self-contained (three.js is vendored inline — no network).
`serve.py` reads the vitals from `/proc` and `/sys` and serves both the page and
its telemetry. Stdlib only, no dependencies, no build step.

## JPet wall view (`/pet`)

A second view — the **family wall pet** (`docs/archive/JPET_PLAN.md`). The `🤖 Pet`
link (top-left of the neural wall) opens **`http://<host>.local:8800/pet`**
(`pet.html`, a self-contained WebGL Tron room). It is **display only** — the kids
drive the pet from the **phone Control screen** in the PWA; this wall just shows it.

Data path: `pet.html` polls `GET /pet/state` on this server once a second, and
`serve.py` proxies that to the on-box api's **internal-only** `GET /internal/pet`
(`BRAIN_API_URL`, default `http://api:8000`; reachable only on the docker `internal`
network — Caddy never routes `/internal` off-box). The snapshot is a content-free
pet state (name/mood/position/current-command/objects in the safe `general` domain), so
the display stays DB-free and the pet is never exposed publicly. Until the api is up (or
the ensure loop has created the pet) the wall shows *"waiting for the pet…"* and retries.
On a box without the api, the neural wall is unaffected — only `/pet` needs it.

**The wall runs the pet's brain (v3, `docs/archive/JPET_V3_PLAN.md`).** `pet.html` is a
continuous 60fps simulation: an **autonomy engine** keeps the pet fluidly doing things on
its own — strolling between the room's spots, looking around, little wiggles/spins/nods,
playing its instruments — chosen by constrained randomness so it never repeats and never
stands idle (and it heads to bed on its own at night; see the interactive room below).
Motion is **damped-spring** (eased accel/decel, speed-scaled turning) with always-on idle
micro-motion, so nothing snaps or freezes. The pet is drawn in **solid wireframe** (dark
occluding faces behind the neon edges — not X-ray). There are **no meters** — mood reads
from behaviour, the way a real pet does. A server **command** (a phone button, or talking
to it) arrives via `/pet/state` as a bounded action `script` and plays as a brief
*interrupt*; when it finishes the pet resumes its own life. It renders the props (ball, bed,
toy box, food bowl, ball pit, light switch) from `objects`.

**Interactive room (wall-owned).** The wall runs a light physics layer the kids play with by
**clicking** (and, on the box's own keyboard, by pressing keys): a **ball** with real vertical
physics — a kick sends it **arcing**, and it bounces off the walls and furniture, scatters the
loose building **blocks** and knocks the pet's **statue** flying; the pet **avoids solid
furniture / the statue / the instruments** (bounding-box collisions) and shoves loose blocks it
walks into. The pet builds one of **ten+ statue shapes** in a **random spot** each time. Clicking
the **TV** changes its channel (**off → three animated cartoons**); clicking the **light switch**
cycles a **dimmer** (off → dim → bright) that changes the room's luminosity — genuinely lighting
it **at night** (daylight lights it by day; the phone `lights` command steps the same dimmer).
There's a **drum kit** and a wall-hung **guitar** to play alongside the **synth**; the synth and
guitar both plink out **kids' songs** (Twinkle, Itsy Bitsy Spider, Hot Cross Buns, Frère Jacques,
Old MacDonald…) — the guitar **plucked**, an octave down — from their fixed play spots. The pet
**puts itself to bed**: at night with the **light off and the TV off** it curls up asleep and
**stays** there, only getting up when day breaks or a **light / the TV** comes on. A
distance-phased gait keeps the walk smooth, and it lingers in each activity (durations run long). Keyboard: **hold an arrow** to walk the pet around until
you let go; the bottom row (**Z–M**) plays the **synth** and the home row (**A–L**) the **drums**
— play either and the pet ambles over to the *other* to **jam along**, holding the groove (the
kit knows several patterns and switches it up every four bars) until you stop; **space** picks up the
nearest **block** and throws it the way the pet faces (press again to let go).

**Sound.** When the pet speaks, `/pet` reads its speech bubble aloud with the same on-box
piper `/tts` endpoint the neural wall uses (`/tts/voices` picks the voice — `en_US-amy-medium`
if installed), and each action fires a short, volume-capped WebAudio cue (a chirp to jump, a
wobble to wiggle, a robot beep, …). A display tab can't autoplay audio, so a one-time **🔊 tap
for sound** button (bottom-right) unlocks it and primes the OS audio sink; after that the pet
just plays. No piper voices on the box → the button never appears and speech stays silent
(the WebAudio cues still play once unlocked) — unlike the neural wall's read-aloud, this is
*not* gated on the `brain_read_aloud` setting, since the pet is its own surface.

## Deployment (auto-started, auto-updated)

It runs as the `server-brain` service in `deploy/docker-compose.yml` — a default
profile service on a thin `python:3.12-slim` + `piper` image
(`deploy/Dockerfile.server-brain`; piper + the baked default voices power the
toggle-gated read-aloud below), so the standard deploy flow owns its lifecycle:

- **`jbrain update`** brings it up and keeps it current via `docker compose up
  -d`. The page still hot-reloads with no rebuild: `serve.py` re-reads `index.html`
  from the bind mount on every request, so a git reset of `src/` serves the new page
  immediately. (A change to `serve.py`, or a piper/base bump, takes effect on the
  container's next rebuild + restart, which `jbrain update` does.)
- It needs **no GPU device and no extra mounts** — Docker already exposes the
  host's `/sys` (read-only) and non-namespaced `/proc` to every container, which
  is exactly where the amdgpu and meminfo vitals live.
- It is published on **its own LAN port (`8800`)**, deliberately *not* behind
  Caddy, so the unauthenticated surface never shares the authed app's origin or
  session cookie.

Open the box's monitor (or any LAN browser) full-screen / kiosk at
**`http://<host>.local:8800/`**.

### Deploy config (compose `.env`, all optional)

| Var | Default | Meaning |
| --- | --- | --- |
| `BRAIN_HOST_BIND` | `0.0.0.0` | Host bind for the published port. Set `127.0.0.1` to serve only the box's own monitor. |
| `BRAIN_POWER_MAX_W` | `90` | APU TDP ceiling, used to normalise power → heat. |
| `BRAIN_NET_MAX_BPS` | `12500000` | Network throughput ceiling (bytes/s, ~100 Mbit) for the net-in/out rim aura. |
| `BRAIN_DISK_MAX_BPS` | `500000000` | Disk-read throughput ceiling (bytes/s, ~500 MB) for the disk-read rim aura. |
| `BRAIN_EVENTS_FILE` | unset | Path to a JSONL file the agent appends web-tool events to (see below) → reach-out tendrils. |

## Security

**There is no authentication.** It exposes only non-sensitive host vitals (GPU
busy %, RAM, power, load) — no database, no user data, nothing behind the RLS
firewalls. That makes it safe to serve unauthenticated **on a trusted LAN**.
Never port-forward it to the public internet. Bind it to your LAN only
(`BRAIN_HOST`), and don't reverse-proxy it past your network edge.

## Run

```bash
# live, from the host vitals (needs amdgpu on this box):
python3 deploy/server-brain/serve.py
#  -> http://0.0.0.0:8800/  — reachable on the LAN at http://<host>.local:8800/

# preview without amdgpu (synthetic wandering values):
BRAIN_DEMO=1 python3 deploy/server-brain/serve.py
```

Then point the wall terminal's browser at `http://<host>.local:8800/` (full-screen
/ kiosk). `<host>` is the box's mDNS name; binding `0.0.0.0` makes it answer on
every interface, so `.local` resolves via avahi like any other host service.

If the page can't reach `/stats` (opened standalone, or the service is down) it
falls back to a built-in demo animation and shows a `— demo · no telemetry —`
badge, so the display is never a dead black screen.

## Config (environment)

| Var | Default | Meaning |
| --- | --- | --- |
| `BRAIN_HOST` | `0.0.0.0` | Bind interface — set a LAN IP to pin one NIC |
| `BRAIN_PORT` | `8800` | Port |
| `BRAIN_API_URL` | `http://api:8000` | On-box api base for the `/pet` view's read-only snapshot (`/internal/pet`) |
| `BRAIN_POWER_MAX_W` | `90` | APU TDP ceiling, used to normalise power → heat |
| `BRAIN_DEMO` | unset | `1` = synthetic values (no amdgpu needed) |

The `python3 serve.py` commands above are for **local dev / preview**; on the
deployed box the compose service (above) runs it for you.

## Signals wired

`serve.py`'s `snapshot()` returns the full `ServerBrain` contract (see
`frontend/demos/server-status-brain/CONTRACT.md`). Wired to real host data:

- **GPU util** (`gpu_busy_percent`) → neural activity / cascade routing
- **RAM + VRAM** (`/proc/meminfo` + amdgpu `mem_info_vram_used`) → active density
- **APU power** (`power1_average`) → bloom heat
- **Net in / out** (`/proc/net/dev` rx/tx deltas) → blue / coral rim aura
- **Disk read** (`/proc/diskstats` sector deltas) → violet rim aura

Still quiet (zeros): `llm`, `api`, `db` — fill them in `snapshot()` when you want
them (tokens/sec from the inference server, qps from `pg_stat_database`), no page
changes needed.

### Web search / fetch tendrils

Wired and **active by default**: the JBrain2 agent (`jbrain.agent.brainevents`)
POSTs a tiny `{"kind": "web_search"|"web_fetch"}` to **`POST /event`** here each
time jerv runs a web tool — compose points `JBRAIN_BRAIN_EVENTS_URL` at this
service on the internal network. `serve.py` queues it and drains it into `/stats`
`events`, and the page fires a cyan (search) or amber (fetch) tendril per event.
Best-effort, on-box, no owner data; a failure never touches the agent's turn.

Alternative source: set `BRAIN_EVENTS_FILE` to a JSONL path and append one event
object per line (`{"kind": "web_search"}`); the reader tails it and re-syncs if it
is truncated/rotated.

### LLM prompt / answer tendrils (opt-in — carries owner text)

When the owner turns on **Settings → Stream LLM to wall display** (the
`brain_llm_stream` app setting, **off by default**), each jerv chat turn POSTs its real
text to `POST /event`: `{"kind": "llm_input", "text": …}` when the turn starts and
`{"kind": "llm_output", "text": …}` when the answer settles (the whole reply, bounded at 4000 chars).
The page streams the prompt IN from off the left edge along a steel tendril that lands on
an inner neuron, and the answer OUT to the right along a green one — the characters ride the
tendril path as clean prose (markdown syntax stripped, the marquee capped for legibility) — then
blooms a popup that renders the full message as Markdown (the same markup as the jerv chat:
headings, bold/italic, code, lists, quotes, links, tables), slowly scrolling if it's too tall to
fit, and (when read-aloud is on) speaks the whole thing.
The same toggle also lets a web tool's **search query** (cyan) / **fetched URL** (amber)
stream out along its tendril; with the toggle off those stay content-free markers.

### Running-workflow / task popups

A workflow or task in flight can POST `{"kind": "task_start", "text": name}` to raise a held
teal popup naming what's running, and `{"kind": "task_stop", "text": name}` (same name) to
retire it when it finishes. Several hold at once, so a couple of concurrent workflows each get
their own card — a quiet twin of the prompt popup.

### Read aloud (optional TTS — server-side piper)

The **Read aloud** panel (bottom-right) reads turns aloud. Speech is rendered **on the box** by
[`piper`](https://github.com/OHF-Voice/piper1-gpl): `serve.py` exposes `GET /tts?voice=<id>&text=…`
(returns a WAV) and `GET /tts/voices` (the installed voice ids), and the page plays the clip through an
`<audio>` element — keeping the browser's flaky Web Speech engine (speech-dispatcher cold start,
silent first-word drops) out of the path entirely.

Two independent voices — **Joe** reads prompts and **Amy** reads answers by default — each an enable
checkbox + a picker over the installed voice ids (add more and they show up automatically); both
persist in `localStorage`. Markdown is stripped before speaking.

**Voice ids and speakers.** A single-speaker model is one voice, its id the file stem
(`en_US-amy-medium`). A **multi-speaker** model (e.g. `en_US-libritts_r-medium`, which carries
hundreds of speakers) contributes one voice per *curated* speaker — id `"<stem>#<speaker>"`, e.g.
`en_US-libritts_r-medium#3922` (a second, female agent voice). Curation lives in `CURATED_SPEAKERS`
in `serve.py` (keyed by model stem → speaker names from the model's `.onnx.json` `speaker_id_map`); an
uncurated multi-speaker model falls back to its default speaker so it stays usable. `serve.py` resolves
the id's speaker index and passes it to piper as `--speaker`.

**The PWA reads aloud too.** The in-chat read-aloud (per-turn play button) can render through this
same piper, reached from the PWA over the authenticated api proxy `GET /api/brain/tts` /
`GET /api/brain/voices` (the api → this on-box service, internal network only). **Settings → Read-aloud
voice** picks the engine (`brain_read_aloud_engine`): **piper** (the voice — any id above, speakers
included, chosen via `brain_answer_voice`, which is also the wall's answer voice; a *play sample*
button auditions it) with an automatic fall back to the **device's native (Web Speech) voice** when
this box is unreachable **or a clip fails to render**, or **native** to always use the device voice.
A silent fall back can look like "the wrong voice" (the native default is often male), so a failed
render is logged — `docker logs server-brain` shows a `[tts] render failed …` line naming the cause
(a timeout points at `BRAIN_PIPER_TIMEOUT_S`; a non-zero exit at a bad/corrupt model).

**The whole reply, not an excerpt.** The page splits a reply into sentence-sized clips and plays them
back-to-back through one queue: the first clip renders while the rest queue, so speech starts fast, the
*entire* answer is read, and no single giant piper render risks the per-clip timeout
(`BRAIN_PIPER_TIMEOUT_S`, default 60 s — piper cold-loads the model on every clip, so a big
multi-speaker voice on a busy box needs headroom, else only that voice times out into the native
fall back). Only the first clip of a
turn carries the silence pad — continuation clips request `?lead=0` so the sentences run together
instead of gapping between each.

**No clipped first word.** Linux audio suspends the output sink after a few seconds of silence, and the
cold resume on the next clip swallows its start — the one durable read-aloud gotcha. The page fights it
on three fronts: (1) while a voice is enabled it runs a **permanently-silent WebAudio keep-alive** that
holds one live stream on the sink, so the sink never goes idle (suspend keys on stream *presence*, not
level, so it's truly silent); (2) it **primes** the `<audio>`→sink path with one silent clip
(`GET /tts/silence`) the moment read-aloud activates, so the very first utterance after a fresh load
isn't clipped by the sink's cold start; (3) as a last backstop, `serve.py` prepends a short lead of
silence to the first clip (`BRAIN_PIPER_LEAD_MS`, default 400 ms).

`piper` **and the default voice models** (Joe, Amy, and the multi-speaker `libritts_r` — its curated
speaker 3922 is a second female agent voice) ship **baked into the server-brain image**
(`deploy/Dockerfile.server-brain`, at `/opt/piper-voices` — outside the read-only `/app` bind mount
that would otherwise shadow them). So there is **nothing to provision and no env var to set**: the
feature is driven entirely by one Settings toggle. A **new baked voice lands on the next `jbrain update`**:
`update-inner.sh` runs `docker compose build` (which re-bakes this image — a changed voice tuple
invalidates the fetch layer's cache) and then `up -d` (which recreates the container from the new
image). A container **restart alone does not re-bake** — restarting reuses the existing image, so use
`jbrain update` (or a manual `docker compose build server-brain && docker compose up -d server-brain`)
for a voice bump, not the Ops "restart all".

**One switch — the toggle.** The voice panel shows only when the owner turns on **Settings → Read
wall display aloud** (the `brain_read_aloud` app setting, **off by default** — the runtime companion
to *Stream LLM to wall display*). The app pushes the setting to this service as a held flag
(`{"kind": "read_aloud", "on": …}` to `POST /event`, surfaced in `/stats.read_aloud`) on the toggle
and again each chat turn, so flipping it shows/hides the panel live with no redeploy; the display is
ephemeral, so it stays off until the next push after a restart. Like *Stream LLM*, it only speaks the
streamed prompt/answer text, so enable it only for a localhost-bound / box-monitor-only display.

A stock `jbrain update` rebuilds the image (which re-bakes the voices), so read-aloud is ready the
moment you flip the toggle — no `.env` edit, no download step.

Add more voices by dropping `<name>.onnx` + `<name>.onnx.json` in the mounted `voices/` dir (scanned
alongside the baked defaults; a dropped-in name overrides a baked one) — grab English voices from the
[piper voices list](https://github.com/OHF-Voice/piper1-gpl/blob/main/VOICES.md). For run-on-host dev
(`python3 serve.py` directly, no image), `bash deploy/server-brain/install-tts.sh` installs piper +
the models into `voices/`. Env knobs (all optional): `BRAIN_PIPER_BIN` (default `piper`),
`BRAIN_PIPER_VOICES_DIR` (mounted extras, default `/app/voices`), `BRAIN_PIPER_BAKED_VOICES_DIR`
(baked defaults, default `/opt/piper-voices`), `BRAIN_PIPER_LEAD_MS`, and `BRAIN_PIPER_TIMEOUT_S`
(per-clip render cap, default 60 s). Text is passed to piper on
**stdin** (never a shell arg) and the `voice` param is validated against the installed set, so there
is no command-injection or path-traversal surface.

**Verbose TTS tracing follows a debug session — no env flag.** While an owner-authorized
debug-console token is live (minted in **Settings → Debug tokens**), the api pushes a
`tts_debug` flag to the display each turn (latched like `read_aloud`), switching on a
per-clip trace: each render logs the voice AS RECEIVED, the model + resolved `--speaker`,
byte count and elapsed ms — so you can confirm the box rendered the requested voice rather
than the PWA falling back to its native voice. It clears automatically when the token
lapses or is revoked. (Failures always log, debug session or not.) Read the trace via
`docker logs server-brain` or the console's `GET /api/debug/logs/server-brain`.

**Autoplay:** the enable checkbox is the user gesture Firefox needs to allow `<audio>` playback for
the session. On a gesture-free kiosk, also set `media.autoplay.default = 0` in `about:config` (or a
site permission for the localhost origin) so clips play without an interaction.

**Sink suspend (guaranteed fix):** the silent WebAudio keep-alive above stops the sink suspending from
inside the browser, which is enough on a normal desktop. If a box power-manages its audio harder and
still clips the first syllable after a long idle, stop the sink suspending one level down — WirePlumber
(Ubuntu 22.04+) `session.suspend-timeout-seconds = 0`, or comment out
`load-module module-suspend-on-idle` in classic PulseAudio. That removes the cold resume entirely, so
the keep-alive and the lead pad become belt-and-suspenders.

**This is the one place the display carries owner data.** Everything else here is host
vitals + content-free markers, which is why it's safe unauthenticated on a trusted LAN.
Turning this on puts your prompt and answer text on that unauthenticated surface, so
enable it **only when the display is the box's own monitor** — bind it to the box with
`BRAIN_HOST_BIND=127.0.0.1` (compose) so nothing on the LAN can read it. The switch is
read live per turn, so flipping it off stops the text immediately (no redeploy).
