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

## Deployment (auto-started, auto-updated)

It runs as the `server-brain` service in `deploy/docker-compose.yml` — a default
profile service on a stock `python:3.12-slim` image, so the standard deploy flow
owns its lifecycle:

- **`jbrain update`** brings it up and keeps it current via `docker compose up
  -d`. No rebuild: `serve.py` re-reads `index.html` from the bind mount on every
  request, so a git reset of `src/` serves the new page immediately. (A change to
  `serve.py` itself takes effect on the container's next restart.)
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
`{"kind": "llm_output", "text": …}` when the answer settles (each excerpt-truncated).
The page streams the prompt IN along a steel tendril and the answer OUT along a green
one — the characters ride the tendril path — then blooms a fade-out popup of the answer.
The same toggle also lets a web tool's **search query** (cyan) / **fetched URL** (amber)
stream out along its tendril; with the toggle off those stay content-free markers.

**This is the one place the display carries owner data.** Everything else here is host
vitals + content-free markers, which is why it's safe unauthenticated on a trusted LAN.
Turning this on puts your prompt and answer text on that unauthenticated surface, so
enable it **only when the display is the box's own monitor** — bind it to the box with
`BRAIN_HOST_BIND=127.0.0.1` (compose) so nothing on the LAN can read it. The switch is
read live per turn, so flipping it off stops the text immediately (no redeploy).
