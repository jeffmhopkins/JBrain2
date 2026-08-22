# Owner debug console (assistant access for live prompt iteration)

> **Status:** Living · **Last verified:** 2026-08-20

A way to let an external assistant (e.g. a Claude Code session) reach a **running**
JBrain box to iterate on prompts against the local model, run read-only SQL, read
logs, and switch LLM routing — live, without a redeploy. Built for the owner's own
**test** box: it trades the domain firewalls for convenience, so turn it on only
where you're comfortable letting the assistant read everything.

It is **off by default** and adds no surface until you enable it.

> Driving it from a session? The assistant-facing runbook —
> requesting a token, saving it, and the `scripts/debug-connect.sh` commands — is
> `docs/runbooks/DEBUG_ACCESS_SESSION_GUIDE.md`.

## The shape

```
PWA (owner) ──mint──▶ capability token  ──hand off──▶  assistant
                                                          │  Authorization: Bearer <key>
                                                          ▼
                                              https://<your-host>/api/debug/*
```

1. In **Settings → Debug access (Claude)** the owner mints a token: a label and a
   lifetime (1h / 24h / 7d / 30d). The server returns a single self-contained
   **payload** — `base64url(JSON{ v, u: server-url, k: key })`, the same idea as the
   OwnTracks pairing payload — shown **once**.
2. The owner copies that payload and hands it to the assistant — or opens the
   **web console** (below) with one tap. It encodes both *where* to connect (the
   public host) and *how* (the bearer key), so nothing else is needed.
3. The assistant calls `/api/debug/*` with `Authorization: Bearer <key>`.
4. The owner can **revoke** (permanent) or **suspend** (reversible) any token from
   the same screen; every token also **expires** on its own.

### Token lifecycle: active · suspended · revoked · expired

- **Revoke** is permanent (`revoked_at`); **expiry** lapses on its own
  (`expires_at`). **Suspend** sits between them: `suspended_at` freezes a token so
  it stops authenticating, and **resume** clears it (migration `0087`).
- A suspended token **cannot un-suspend itself** — it can no longer authenticate
  the surface — so **resume is owner-only** (the PWA token list). The console (or
  the owner) can *enter* suspension; only the owner leaves it. That asymmetry is
  deliberate.
- **Side effect while a token is live:** the api pushes a `tts_debug` flag to the
  tts-stt renderer each turn, switching on its verbose per-clip TTS trace (voice
  as received, resolved `--speaker`, byte count, elapsed ms — read via `GET
  /logs/tts-stt`). It follows token liveness (the same active-set predicate as
  auth), so it clears on the next turn once the token lapses, is suspended, or is
  revoked. Diagnostics-only: the trace carries no owner text.

> **Reading `local_gateway.unannounced_load`.** It means *the client that logged it* did not
> load that model — NOT that the load skipped the residency budget. Two benign cases produce
> it, and both were mistaken for a bypass on 2026-08-21:
>
> - `first_poll=true` — a fresh client reports every already-resident model, because it had no
>   prior view. The worker logged two models at the same millisecond this way, both loaded by
>   the api minutes earlier.
> - a different `client` id — the api and the worker each hold their own client, so each
>   reports the other's legitimate, guarded loads.
>
> A load this client has in flight no longer reports itself (it used to; `load_cache_swept`
> and `unannounced_load` for the same model, same client, three seconds apart). Treat a line
> as a real bypass only when `first_poll=false` **and** no other process was loading — confirm
> against `box_events` (a guarded load leaves a `model_load` span; a true bypass leaves none)
> and against llama-swap's own request log via `upstream-logs`.

## Streamed turns, buffered on the box

`POST /api/debug/complete` takes `stream: true` (`scripts/debug-connect.sh complete --stream`),
which runs the turn through the **streaming** adapter path — the one a real chat turn takes —
instead of the one-shot one. It returns `ttft_ms` and a timestamped frame per streamed part.

Two reasons it exists:

- **The two paths are different code.** Anything that only happens while a turn streams — the
  prefill fraction, first-token latency, the reasoning channel arriving separately from the
  answer — is invisible to a console that can only call `complete`. A probe that cannot be
  exercised cannot be verified, which is how the prefill capture shipped and reported nothing
  from the console.
- **It survives the proxy.** The frames are buffered on the box and returned when the turn
  ends, never streamed over the wire. A long turn held open through a Cloudflare Tunnel dies
  at its request timeout — measured, a 204 s model load returns `error code: 524` while the
  load itself carries on fine server-side. For a call expected to outlast the timeout
  entirely, `POST /complete-async` + `GET /jobs/{id}` is the same trick with the polling made
  explicit.

`ttft_ms` is the reading to look at: a long first gap is prefill, an even cadence after it is
generation, and the two have different causes and different fixes.

## Launch-flag experiments (no terminal)

These routes exist so a llama-server **launch flag** can be tried, measured and reverted from the
console, instead of needing a catalog edit, a release and an Ops → Update per iteration:

- `PUT /api/debug/llm/local-models/{id}/extra-args` — set or clear extra flags for one model
  (re-stamps the gateway config and unloads it, so the next request relaunches with them).
  Only flags on `llm_settings.EXTRA_ARG_FLAGS` are accepted: llama-server **refuses to start**
  on an unknown flag, so an unrestricted argv here could make a model permanently unloadable
  from a box with no shell. Clearing is the same call with `{"args": []}` — and clearing does
  not need the model to be loadable, so a bad *value* is always recoverable. The list covers
  `--swa-full`, `-b`/`-ub`, the four speculative-decoding knobs
  (`--spec-type`, `--spec-draft-n-max`, `--spec-draft-n-min`, `--spec-draft-p-min`), the image
  pair (`--image-min-tokens`, `--image-max-tokens`), `--load-mode`/`-lm` (which SUPERSEDES the
  hardcoded `--no-mmap` rather than duplicating it — `auto|none|mmap|mlock|mmap+mlock|dio`, the
  lever for testing whether the weights need to be resident twice at all), and the cache pair
  (`--ctx-checkpoints`,
  `--cache-reuse`) — those
  because their right values are empirical and hardware-specific, so without a live path a
  single tuning iteration would cost a catalog edit, a release and an Ops → Update. Setting
  `--spec-type` here also pins the model to one slot: the config generator derives that from
  the flags it is about to write, so an operator flag gets the same clamp a catalog flag does.
  Also on the list: `-ngl` and `-fa` (the "is it the GPU?" bisect — fewer offloaded layers or
  flash attention off, when a model emits garbage or dies on this iGPU) and
  `--reasoning-format` (for `<think>` leaking into `content`, or an empty reasoning channel,
  after a llama.cpp rebuild on master). `--no-mmap` is deliberately absent and cannot be added:
  llama.cpp has no positive `--mmap`, so an entry could not undo the flag we already pass — it
  would be a silent no-op, which is worse than an absent one.
  Also on the list for prefill work: **`-lv`** (llama-server verbosity — `-lv 4` is TRC, the only
  place `created context checkpoint` / `restored context checkpoint` / `forcing full prompt
  re-processing` appear, and without them a checkpoint sweep cannot tell a wrong count from
  nothing ever being restored), **`--checkpoint-min-step`** (default 8192, so a ~24k prompt gets
  only ~3 checkpoints by SPACING however high the count goes) and **`--cache-ram`** (default 8192
  **`--cache-ram` costs HOST memory, and the default 8 GiB is now budgeted** as
  `local_catalog.CACHE_RAM_GB` in every model's resident footprint. It was not, on the
  reasoning that it "does not touch the GTT budget" — true of `gpu_guard`, and irrelevant to
  `residency`, which is a host-RAM budget. Up to 8 GiB per resident model was therefore
  invisible to the evictor, on the box whose failure mode is running out of exactly this. An
  OVERRIDE is still unbudgeted (it rides `extra_server_args`, which the cost model does not
  parse), which is why the flag is bounded to 0..32 GiB rather than left open — raise it and
  the evictor under-counts that model by the difference.
  **`-fa 0` is refused (422) on a model carrying a vision projector.** Turning flash attention
  off swaps the CLIP attention workspace from the linear branch to the quadratic one — ~0.47 GB
  to ~16 GB — and `_vision_resident_gb` hardcodes the linear figure, so the residency budget
  would under-reserve by ~15.5 GB. The allocation lands on the first full-resolution image,
  after the load guard passed and after the watchdog stopped watching, which is the
  unrecoverable hang. The bisect stays available on text-only models; budgeting it properly
  (threading the served `-fa` into `footprint_gb`) is what would lift the refusal.
  **`--ctx-checkpoints` is the one exception to "a bad value is always recoverable"**, so it is
  bounded to `0..32` server-side — 32 being llama.cpp's OWN default, so a sweep can reach it (an
  earlier 0..8 bound put the one value most worth trying out of reach). A checkpoint on a hybrid
  is a full copy of the recurrent state (~150 MiB for Qwen3.8), device-resident and per slot.
  `footprint_gb` budgets it at the SERVED count (2), not at whatever you set here — so everything
  above that is unbudgeted on a box whose documented failure mode is an unrecoverable host hang.

  ⚠️ **A cold prime cannot be measured through this console.** The proxy in front of the box
  returns 524 around 100 s and `/upstream/…` has a hard 180 s ceiling, while a cold prefill of the
  agent prefix takes ~101 s at 32k and ~220 s at 262k. The client gives up while the server keeps
  working, and on a one-slot model the next request queues behind a task that may already have
  been dropped. Read cold behaviour from `gateway-logs` and `/metrics` deltas instead. And note
  `n_prompt_tokens_cache` in `slots` is **zeroed on slot release**, so it reads 0 after any
  completed request regardless of reuse — use `llamacpp:prompt_tokens_cached_total` from the
  metrics route, or `timings.cache_n` in a completion response body.
- `GET /api/debug/llm/local-models/{id}/metrics` (`debug-connect.sh spec-metrics`) now reports
  **`prompt_tokens_cached_total`, `prompt_tokens_total` and `cache_hit_rate`** beside the
  speculation figures. These are the authoritative prompt-reuse signal. Do NOT use
  `n_prompt_tokens_cache` from `slots` for this: llama.cpp zeroes a slot's stats on release, so
  it reads 0 after any completed request whether reuse was total or nonexistent — the same class
  of trap as the dead `speculative.types` field. All of these are lifetime totals, so delta them
  around a single request. A warm prime on this box measured 32,485 of 32,489 tokens reused.
- `POST /api/debug/complete` takes an optional `sampling` object (`{"temperature": 0.1,
  "min_p": 0.0}`), merged over the model's catalog defaults exactly as a prompt's
  `config: sampling:` block is. This is the ONLY way to vary sampling from the box: it is
  catalog-static per model with no settings key and no endpoint, so repetition loops, a model
  that will not stop, or malformed tool-call blocks could not otherwise be A/B'd against a live
  model. Per-request and unpersisted — no reload, and nothing outlives the call. A mistyped key
  is a 422, never a silent drop.
- `GET /api/debug/llm` reports `local_llm_timeout_s`, the client-side ceiling on a local call.
  REPORTED, not settable (it is env-only). It is here because it masquerades as a hung model: a
  cold prefill at a large window can exceed it and the turn fails as a client timeout with
  nothing saying the model was still working.
- `PUT /api/debug/llm/local-models/{id}/context-window` — the served `-c`. On this surface
  because window and KV are one decision: `--swa-full` doubles a model's KV and halving the
  window pays for it exactly.
- `GET /api/debug/llm/local-models/{id}/props` — `build_info` (the only build identity available
  over HTTP, and this box rebuilds llama.cpp on master by default), real `n_ctx`, `total_slots`.
- `POST /api/debug/llm/local-models/{id}/prime` — run the real jerv prime and return
  `elapsed_ms`, the measurement instrument for any prefill experiment.

  ⚠️ **A prime EVICTS to fit, exactly like a load, and what it evicts does not come back on
  its own.** Both operator warms admit through the same evict-to-fit path; neither records a
  restore, because nothing on the debug-console path fires one (a restore is driven by a
  finished agent turn or by code-mode power-off). That is deliberate — a recorded-but-undrained
  displacement would pile up across an experiment and then reload all at once during whatever
  chat turn came next, evicting the model you had just primed. So check `GET …/local-models`
  before a run, and reload what the experiment displaced when you are done.

> **A 200 from `restore` does not mean the prefill was skipped.** On a sliding-window model
> (gpt-oss) llama-server can accept a restore and then discard it, logging `forcing full prompt
> re-processing`. Always pair a restore with `POST …/prime` and compare `elapsed_ms` against a
> known-cold prefill, and read `GET /api/debug/logs/local-llm`. The timing and the log are the
> honest signals; the HTTP status is not.

## Auth model

The token is a `capability_token` **principal** — the third, previously-dormant
principal kind alongside `owner` and `device_key`. It follows the same isolation
rule as every other credential: a **physically distinct, kind-filtered lookup**
(`find_active_capability_by_key_hash`), so a debug token can never authenticate on
the owner-cookie or device paths, and an owner/device key can never authenticate
here. On top of revocation it enforces an **`expires_at`** and stamps
**`last_used_at`** on each hit (migration `0086`), plus a reversible
**`suspended_at`** pause (migration `0087`).

Two gates protect the surface, both fail-closed:

- **Feature flag** `JBRAIN_DEBUG_ACCESS_ENABLED` (default `false`). When off, the
  `/api/debug/*` router is **not mounted** (a 404 — no oracle that it exists) and
  minting is refused (409). The owner management routes (`/api/settings/debug-tokens`)
  exist either way so a token can still be listed / suspended / resumed / revoked.
- **The bearer key**: a live, unrevoked, **unsuspended**, unexpired
  `capability_token` or 401.

## What the token can do (`/api/debug/*`)

| Route | Purpose |
|-------|---------|
| `GET /whoami` | Token label, kind, and the fixed scope set (`llm.complete`, `sql.read`, `logs.read`, `llm.routing`, `host.read`, `host.metrics`, `web.fetch`). |
| `GET /version` | The git revision the **running server** was built from — `git_sha`, `git_describe`, and `build_time`, baked into the image at build time (`deploy/update-inner.sh` → Dockerfile ARG/ENV), plus `started_at` (this process's boot time). Answers "is the merge I just made actually deployed?" without guessing; `started_at` behind `build_time` means the new image built but the container wasn't recreated. `"unknown"` on a plain local build. |
| `GET /version/history` | The recorded **timeline of deployed versions**, newest first (`app.deploy_history` — the app appends one row on boot whenever its baked `git_sha` changes, so a plain restart adds nothing). Each row is `{git_sha, git_describe, build_time, deployed_at}`; the interval `[deployed_at, next row)` is when that build was live, so an *older* timestamped record (a research run, an ingest) can be tied to the build that produced it. Owner-only read. |
| `POST /complete` | Run one `system` + `user_text` prompt through the **LLM adapter** (non-negotiable #1 — never a provider SDK) against whatever model is currently routed; returns the text/parsed JSON, token usage, and the **resolved provider:model**. Route by a known `task` (so the live per-task override applies) or a raw `strength` tier. Synchronous — fine for quick calls. |
| `POST /complete-async` → `GET /jobs/{id}` | Same completion, but as a **background job**: submit returns a `job_id` at once; poll `/jobs/{id}` until `done`. For a slow model (a long, high-effort local extraction takes minutes) this avoids holding a request open past a proxy's timeout — e.g. the Cloudflare Tunnel's ~100s edge limit. In-memory + best-effort (a restart drops in-flight jobs). |
| `POST /vision` | Run one vision task (`vision.ocr` / `vision.caption`) over an **on-box attachment** (by id) through the **LLM adapter**, optionally with a candidate `system` prompt — the image-layer twin of `/complete` for iterating the OCR/caption prompts against the real vision model. Image bytes load via the storage abstraction (non-negotiable #2); the attachment lookup runs in the same read-only owner context as `/sql`. Reuses the `llm.complete` scope (vision IS a completion). |
| `POST /grounding` | Ask the routed vision model to **locate** something in an on-box attachment (`target`, in plain words) and report the boxes it returns. Exists because the coordinate base is a per-MODEL fact that nothing upstream documents for this checkpoint — the Qwen3-VL cookbook divides by 1000, the Qwen3-VL docs site describes a 0–1 range, and the Qwen3.8 model card says nothing — and guessing is unsafe: a wrong base yields a confident box around the *wrong thing*. So the response renders the **same reply under both bases** (`as_norm_1000` / `as_norm_1`, in original-image pixels): whichever set frames the object is the model's real convention, and that value gets pinned in `agent/grounding.py`. `inferred` is what the magnitude heuristic guessed, `pinned` what the table currently holds (`null` = unqualified model, which the canvas tools refuse to use). `width`/`height` are **EXIF-corrected** — the axes the model actually saw. `downscale: true` re-runs it through the ingest downscale to compare. Reuses the `llm.complete` scope. |
| `POST /sql` | One **read-only** statement. Runs under an owner RLS context (full read) inside a `SET TRANSACTION READ ONLY` transaction, so it can read anything yet write nothing; a single-statement read-verb guard rejects obvious misuse with a clean 400. Rows capped + JSON-coerced. |
| `POST /fetch` | Run a URL through jerv's **WebFetcher** — the same `direct → reader → solver → tavily` escalation the agent uses — and return the extracted page (title, one text window, total chars, link count), or a 400 carrying the recoverable fetch error. The one route that drives the live web-fetch path end to end, so bot-challenge detection and the recovery fallbacks can be verified against a real walled URL after a deploy. **`tier`** names the leg that actually served the page (`direct` = nothing had to be recovered; `reader`/`solver`/`tavily` = that tier saved it), so confirming an escalation is one call rather than a hunt through `GET /logs/api`. **`js_shell`** is true when NO tier could render a JavaScript app — an empty `text` with `js_shell: true` is an *unread* page, not an empty one (`GET /logs/api` still carries `web.js_shell_unrecovered` / `web.challenge_blocked` for the blow-by-blow). A `"tier": "tavily"` body field forces ONLY the hosted Tavily Extract tier — the same probe the Settings **Test key** button uses, so a freshly-entered key can be verified from a handed-over token. |
| `POST /solve` | Run a URL through **ONLY the challenge solver** (byparr), skipping the direct+reader legs — so the stealth browser can be exercised in isolation against a walled URL (Reuters/WSJ/…) without a doomed direct fetch first. Same output shape as `/fetch`; a 400 distinguishes *solver unconfigured* from *byparr ran but still challenged / empty* (a real solve miss — pair with `GET /logs/byparr`). Shares the `web.fetch` scope. |
| `POST /fetch` `tier=tavily` | Force **ONLY the hosted Tavily Extract tier** (`scripts/debug-connect.sh tavily <url>`). A 400 distinguishes *the tier unwired* (no `JBRAIN_TAVILY_URL`) from *disabled / keyless / a genuine miss* (bad key or Tavily error) — so after a deploy the owner can confirm the Tavily key works against a real walled URL with a handed-over token, no PWA needed. Shares the `web.fetch` scope. |
| `GET /client-vitals` | What the **browser** last reported about the top-bar vitals stream: frames seen, opens, errors, reopen counts, `readyState`, `sinceLastFrameMs`, the number of samples the browser is holding, and `clockOffsetMs` (how far the box's clock sits from the browser's, as estimated from the frames — samples are placed on the graph with that difference taken out, so a large figure here is a fact about the clocks, not a fault). The one read that separates *the box never sent a frame* from *the browser never received one* — states that need different fixes and are indistinguishable from the box, because a connection the browser declines to open (its per-origin cap, say) leaves no server-side trace at all. `sinceLastFrameMs` is the number that matters: the route emits one a second, so anything above a few thousand means the meter is blind however healthy the socket claims to be. `{"reported": false}` means nobody has opened the vitals detail since this process started, **not** that the meter is broken. Populated by the PWA beaconing every 15s while that screen is open. |
| `GET /logs/{service}` | Tail a container's logs, proxied to the supervisor (the single owner of docker access), mirroring the owner ops surface. |
| `GET /llm/gateway-logs` | Tail **llama-swap's** buffered log — swap decisions, health checks, and the slot lifecycle, where a slot is acquired on a request and **released** when its generation ends. The read that answers whether a Stop/disconnect halts decoding or the engine runs on. It does **not** carry llama.cpp's own output (that's the next row). 502 if the gateway is unreachable. |
| `GET /llm/upstream-logs` | Tail **llama-server's** own stdout, which `gateway-logs` cannot show: the slot lifecycle, per-request prompt-eval throughput, context-checkpoint evictions, and a failed load's reason. Reads the history burst llama-swap replays on `/logs/stream/{stream}`; `stream` defaults to `upstream` (all models interleaved) or takes a served model id to isolate one. **It does not carry a load's per-buffer memory breakdown** — verified 2026-08-19: the model loader prints nothing at the default verbosity 3, so a load reads as a ~1.4 s silent gap here *and* in the `local-llm` container log. Use the `local_gateway.footprint_measured` event (the device delta) for a load's memory. An empty body means the engine has printed nothing since llama-swap started — usually no load since boot, not a fault. |
| `POST /llm/drop-page-cache` | Reclaim the page-cache copy of on-box model weights (`?models=a,b` for specific catalog ids, omit for all). The box serves with `--no-mmap`, so every load leaves the weights resident **twice** — GTT plus the page cache the read filled — and unloading frees only the GTT copy. Since `host_metrics` counts page cache as **used**, that residue shrinks the admission budget for every later load: measured 2026-08-19, 29.19 GiB of stale `gpt-oss-120b` cache left host pages free at 86.2 GB and got `qwen3-coder-next-q8` (needs ~95.5) refused for want of 15.3 GB nothing was using. Safe while models are resident — `POSIX_FADV_DONTNEED` drops clean cache only, never the GTT copy being served from, and weights are read-only. A null `freed_gb` means `cachestat(2)` could not measure the drop (blocked by the container's seccomp profile on this box), **not** that nothing was freed. Previously this needed host shell (`deploy/update-inner.sh`'s global `drop_caches`), which the owner does not have. |
| `GET /host/metrics` | The host's live hardware telemetry, proxied from the supervisor (the only container that reads `/sys`): GPU busy %, APU package power, load average, memory/swap/disk, fan RPM, per-container memory — plus **`gpu_mem`** (amdgpu GTT/VRAM used, the iGPU's slice of unified RAM that no process RSS shows), a **`mem_breakdown`** of key `/proc/meminfo` lines, and cumulative **`net`** / **`disk_io`** byte counters. The console's one physical read — pair it with a turn to watch the GPU gauge across a Stop, or read `gpu_mem.gtt_used_bytes` with no model loaded to spot device memory a teardown failed to release. |
| `GET /host` | Live host memory/swap/disk/load + **per-container RSS** and **raw per-process RSS** (both biggest first), proxied from the supervisor. Attributes the unified-memory total down to individual processes — the per-process list (via `docker top`) tells the 120B's `llama-server` from the vision model's, since the `local-llm` container runs a separate process per loaded model. Answers "what is using the box's RAM" the read-only meter can only total. (For the iGPU/GTT share the process list can't show, read `gpu_mem` on `/host/metrics`.) |
| `GET /update/status` | The last system-update one-shot's state + log tail (git pull + rebuild + model sync), proxied from the supervisor — the console's window into an update that runs outside the compose project. |
| `GET /provision/status` | The last local-model **download** one-shot's state + log tail (the PWA "Download" action, `deploy/local-models-sync.sh`). The verbose per-model weight-pull output — resolved repo, include globs, and the hf failure reason (404 / auth / disk / network) — so the console can answer *why* a model download failed. |
| `GET/PUT /llm` | Read or **switch** which model serves each task — live, no restart. Shares validation with the owner settings screen. |
| `POST /llm/local-models/{id}/load\|unload` | Warm or evict a local model on the gateway. |
| `POST /suspend-self` | **Pause** the presenting token (the console's Suspend button). Owner resumes it later from the PWA. |
| `POST /revoke-self` | **Kill** the presenting token (the console's Revoke button). Permanent. |

The two `*-self` routes are the only writes a token can make to its **own** grant,
and both only ever *weaken* it (de-escalate), never extend it — so they need no
owner authority. There are **no** data-write or owner-management routes on this
surface, and it is rate-limit/audit-logged like the rest of the API.

The owner-side counterparts live on the management surface (owner-cookie gated):
`DELETE /api/settings/debug-tokens/{id}` (revoke) and
`POST /api/settings/debug-tokens/{id}/suspend|resume`.

`GET /api/debug/activity?after=<seq>` returns a live ring of recent `/api/debug/*`
calls — verb, route, status, derived kind, and a short **detail** (the SQL text,
the prompt, the routing change, truncated) that each handler stashes on
`request.state`. So the console shows *what* ran, not just the route, including
commands an external assistant issues. The surface is owner-token-gated and the
owner already has full read, so echoing their own commands back is intentional.

## The web console

`/debug-console.html` (opened from **Settings → Debug access** via **Open
console**, or by pasting a payload) is a standalone, **token-authed** page — not
part of the cookie-authed PWA. Two-pane UI: a **live activity** feed on the left
(it polls `/api/debug/activity`, so an assistant's commands stream in as they run,
not just this tab's), output on the right, and **Suspend** / **Revoke** top-right
as the token's own kill switch. It is a separate Vite entry, precached by the
service worker like `/dash`.

Two properties make it work across the public/LAN split:

- **Same-origin API calls.** The console calls the API with *relative* paths, so
  it always targets the host that served the page — never the token's embedded
  host. That is what lets a LAN-only console (served over `jbrain.local`) drive the
  box even though the token it carries points an external assistant at the public
  host. The token supplies only the bearer **key**; its `u` host is for off-box
  clients.
- **Cached connection.** The key is saved to `localStorage`, so a refresh
  auto-reconnects (and the fragment is stripped from the address bar on load). It
  is cleared on **Revoke**. A suspended token still 401s until the owner resumes it
  in the PWA, after which a reload reconnects.

### Public token, LAN-only console

By design the token defaults to the **public** host (so a handed-off token reaches
the box from the internet), while the console **page** is **LAN-only** (it must not
be exposed publicly):

- `JBRAIN_PUBLIC_BASE_URL` (e.g. `https://your-tunnel-host`) is embedded in every
  minted payload, even when minted from the LAN PWA, so an external assistant
  connects over the public host. Empty falls back to the mint origin.
- The console page is served only on the LAN site; the public site **404s**
  `/debug-console*` (its shared `/assets/*` carry no secrets, and `/api/debug/*`
  stays reachable for the token). So the human UI requires LAN access
  (`jbrain enable-lan`); a remote assistant still uses the `/api/debug/*` routes
  directly (or `scripts/debug-connect.sh`).

## Security posture (and the deliberate trade)

- Off by default; no surface, no minting until enabled.
- 256-bit key, stored SHA-256-hashed; revocable; time-boxed; usage-stamped.
- Kind-filtered lookup → no confused-deputy across principal kinds.
- SQL is read-only at the **transaction** level, not just by string inspection.
- **The trade:** read-only SQL runs under an owner context, so it bypasses the
  health/finance/location domain firewalls, and `GET /logs` can surface logged
  content. That means a holder of a live token can read **personal data**, and it
  leaves the box to wherever the assistant runs. This is intended for a **test**
  box. Keep tokens short-lived and revoke when done. On a box with real personal
  data, leave `JBRAIN_DEBUG_ACCESS_ENABLED=false`.

## Reachability

The assistant reaches the box at the payload's host — normally the public
Cloudflare Tunnel hostname (`docs/runbooks/CLOUDFLARE_TUNNEL.md`). This only works if the
assistant's network egress can reach that host; an isolated sandbox may not be
able to, in which case the token is fine but the connection won't establish.

## Enabling it

Set in `/opt/jbrain2/.env`:

```
DEBUG_ACCESS_ENABLED=true
# So a handed-off token reaches the box from the internet even when minted from
# the LAN PWA (the LAN console ignores this and calls same-origin):
PUBLIC_BASE_URL=https://your-tunnel-host
```

then `sudo jbrain up` (**not** `jbrain restart`). A `.env` change is only injected
when the container is **recreated**: `docker compose restart` reuses the existing
container with its old environment, so the flag wouldn't take. `jbrain up` (or
`down` + `up`) recreates and picks it up. Mint a token in **Settings → Debug
access (Claude)**, hand off the payload (or **Open console**), and revoke it when
the session is done. Set it back to `false` (and `jbrain up`) to remove the
surface entirely.
