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
pet state (name/mood/drives/position/speech in the safe `general` domain), so the
display stays DB-free and the pet is never exposed publicly. Until the api is up (or
the drives tick has created the pet) the wall shows *"waiting for the pet…"* and
retries. On a box without the api, the neural wall is unaffected — only `/pet` needs
it.

**Sound.** When the pet speaks, `/pet` reads its speech bubble aloud with the same
on-box piper `/tts` endpoint the neural wall uses (`/tts/voices` picks the voice —
`en_US-amy-medium` if installed). A display tab can't autoplay audio, so a one-time
**🔊 tap for sound** button (bottom-right) unlocks it and primes the OS audio sink;
after that the pet just talks. No piper voices on the box → the button never appears
and the pet stays silent — unlike the neural wall's read-aloud, this is *not* gated on
the `brain_read_aloud` setting, since the pet is its own surface.

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
[`piper`](https://github.com/OHF-Voice/piper1-gpl): `serve.py` exposes `GET /tts?voice=<model>&text=…`
(returns a WAV) and `GET /tts/voices` (the installed models), and the page plays the clip through an
`<audio>` element — keeping the browser's flaky Web Speech engine (speech-dispatcher cold start,
silent first-word drops) out of the path entirely.

Two independent voices — **Joe** reads prompts and **Amy** reads answers by default — each an enable
checkbox + a picker over the installed piper models (add more and they show up automatically); both
persist in `localStorage`. Markdown is stripped before speaking.

**The whole reply, not an excerpt.** The page splits a reply into sentence-sized clips and plays them
back-to-back through one queue: the first clip renders while the rest queue, so speech starts fast, the
*entire* answer is read, and no single giant piper render risks the timeout. Only the first clip of a
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

`piper` **and the default Joe/Amy voice models** ship **baked into the server-brain image**
(`deploy/Dockerfile.server-brain`, at `/opt/piper-voices` — outside the read-only `/app` bind mount
that would otherwise shadow them). So there is **nothing to provision and no env var to set**: the
feature is driven entirely by one Settings toggle.

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
(baked defaults, default `/opt/piper-voices`), `BRAIN_PIPER_LEAD_MS`. Text is passed to piper on
**stdin** (never a shell arg) and the `voice` param is validated against the installed set, so there
is no command-injection or path-traversal surface.

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
