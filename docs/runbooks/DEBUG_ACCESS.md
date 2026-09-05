# Owner debug console (assistant access for live prompt iteration)

> **Status:** Living · **Last verified:** 2026-09-05

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
  nothing ever being restored) and **`--checkpoint-min-step`** (default 8192, so a ~24k prompt
  gets only ~3 checkpoints by SPACING however high the count goes). And the KV-cache
  quantisation flags, in both spellings (**`-ctk`/`-ctv`**, **`--cache-type-k`/
  `--cache-type-v`**): the largest live lever on KV demand here (q8_0 halves an f16 cache),
  allowlisted rather than simply switched on because the quality cost is empirical and untested
  against this box's models.
  **`--cache-ram` is deliberately NOT on the list.** The gateway serves `-cram 0` and
  `local_catalog.CACHE_RAM_GB` is 0.0 to match — the flag and the budget term are one
  decision — so an operator turning the cache back on from here would serve up to 32 GiB of
  host RAM the residency budget believes does not exist, under-reserving on the one path this
  box has hard-locked on (`llm_settings.EXTRA_ARG_FLAGS` has the full rationale).
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
  is a full copy of the recurrent state — measured **275–284 MiB** each for the hybrid 27B, and
  held in **host RAM**, not device memory (GTT unchanged going 2 → 16), though on unified
  memory it still comes out of the one pool the budget counts — kept per slot.
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

  ⚠️ **A prime EVICTS to fit, exactly like a load, and nothing on this path schedules a
  restore.** One exception, and it cuts the other way: if the victim is the **primary local
  chat model** and the auto-restore toggle is ON, `WarmKeeper` finds it cold within 60 s and
  reloads it through `ensure_room` — which can evict the model you just primed and corrupt the
  measurement mid-experiment. Check the toggle before a run: with it OFF (its state on this box
  at the time of writing) nothing reloads behind you, which is what you want while measuring. If it takes code mode's reserved model, it says so: grep
  `residency.evicted_held_model` in `GET /api/debug/logs/api`, and the vitals row names it as
  code mode's. The load is allowed to win — you pressed it — but a code session that suddenly
  lost its model is explained by that line, not by the box misbehaving. Both operator warms admit through the same evict-to-fit path; neither records a
  restore, because nothing on the debug-console path fires one (a restore is driven by a
  finished agent turn or by code-mode power-off). That is deliberate — a recorded-but-undrained
  displacement would pile up across an experiment and then reload all at once during whatever
  chat turn came next, evicting the model you had just primed. So check `GET …/local-models`
  before a run, and reload what the experiment displaced when you are done.

> **Historical (v1): there is no `restore` route any more.** The v1 disk KV-slot cache exposed
> one; the v2 prompt cache is app-internal — the load-time warm restores the KV file itself
> before priming, with no console route to drive it. The v1 lesson still generalises: a 200
> from a restore-shaped call does not mean the prefill was skipped (llama-server could accept
> a restore and then discard it, logging `forcing full prompt re-processing`), so measure with
> `POST …/prime` `elapsed_ms` against a known-cold prefill and read the logs — timing and log
> are the honest signals, the HTTP status is not.

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
| `GET /whoami` | Token label, kind, and the fixed scope set (`llm.complete`, `sql.read`, `logs.read`, `llm.routing`, `llm.gateway`, `host.read`, `host.metrics`, `web.fetch`). `llm.gateway` is the gateway surface — load/unload, the served `-c`/`-np`, launch flags via the allowlist, props/slots/metrics, prime. |
| `GET /version` | The git revision the **running server** was built from — `git_sha`, `git_describe`, and `build_time`, baked into the image at build time (`deploy/update-inner.sh` → Dockerfile ARG/ENV), plus `started_at` (this process's boot time). Answers "is the merge I just made actually deployed?" without guessing; `started_at` behind `build_time` means the new image built but the container wasn't recreated. `"unknown"` on a plain local build. |
| `GET /version/history` | The recorded **timeline of deployed versions**, newest first (`app.deploy_history` — the app appends one row on boot whenever its baked `git_sha` changes, so a plain restart adds nothing). Each row is `{git_sha, git_describe, build_time, deployed_at}`; the interval `[deployed_at, next row)` is when that build was live, so an *older* timestamped record (a research run, an ingest) can be tied to the build that produced it. Owner-only read. |
| `POST /complete` | Run one `system` + `user_text` prompt through the **LLM adapter** (non-negotiable #1 — never a provider SDK) against whatever model is currently routed; returns the text/parsed JSON, token usage, and the **resolved provider:model**. Route by a known `task` (so the live per-task override applies) or a raw `strength` tier. Synchronous — fine for quick calls. |
| `POST /complete-async` → `GET /jobs/{id}` | Same completion, but as a **background job**: submit returns a `job_id` at once; poll `/jobs/{id}` until `done`. For a slow model (a long, high-effort local extraction takes minutes) this avoids holding a request open past a proxy's timeout — e.g. the Cloudflare Tunnel's ~100s edge limit. In-memory + best-effort (a restart drops in-flight jobs). |
| `POST /vision` | Run one vision task (`vision.ocr` / `vision.caption`) over an **on-box attachment** (by id) through the **LLM adapter**, optionally with a candidate `system` prompt — the image-layer twin of `/complete` for iterating the OCR/caption prompts against the real vision model. Image bytes load via the storage abstraction (non-negotiable #2); the attachment lookup runs in the same read-only owner context as `/sql`. Reuses the `llm.complete` scope (vision IS a completion). |
| `POST /grounding` | Ask the routed vision model to **locate** something in an on-box attachment (`target`, in plain words) and report the boxes it returns. Exists because the coordinate base is a per-MODEL fact that nothing upstream documents for this checkpoint — the Qwen3-VL cookbook divides by 1000, the Qwen3-VL docs site describes a 0–1 range, and the Qwen3.8 model card says nothing — and guessing is unsafe: a wrong base yields a confident box around the *wrong thing*. So the response renders the **same reply under both bases** (`as_norm_1000` / `as_norm_1`, in original-image pixels): whichever set frames the object is the model's real convention, and that value gets pinned in `agent/grounding.py`. `inferred` is what the magnitude heuristic guessed, `pinned` what the table currently holds (`null` = unqualified model, which the canvas tools refuse to use). `width`/`height` are **EXIF-corrected** — the axes the model actually saw. `downscale: true` re-runs it through the ingest downscale to compare. Reuses the `llm.complete` scope. |
| `POST /tool-probe` | Run one completion with a chosen set of the agent's **real registered tool schemas** (by name, plus optional raw schemas) attached — **no handler runs**. Probes whether a routed model emits well-formed tool calls against the live schemas; unknown tool names are a 400. Reuses the `llm.complete` scope. |
| `POST /sql` | One **read-only** statement. Runs under an owner RLS context (full read) inside a `SET TRANSACTION READ ONLY` transaction, so it can read anything yet write nothing; a single-statement read-verb guard rejects obvious misuse with a clean 400. Rows capped + JSON-coerced. |
| `POST /fetch` | Run a URL through jerv's **WebFetcher** — the same `direct → reader → solver → tavily` escalation the agent uses — and return the extracted page (title, one text window, total chars, link count), or a 400 carrying the recoverable fetch error. The one route that drives the live web-fetch path end to end, so bot-challenge detection and the recovery fallbacks can be verified against a real walled URL after a deploy. **`tier`** names the leg that actually served the page (`direct` = nothing had to be recovered; `reader`/`solver`/`tavily` = that tier saved it), so confirming an escalation is one call rather than a hunt through `GET /logs/api`. **`js_shell`** is true when NO tier could render a JavaScript app — an empty `text` with `js_shell: true` is an *unread* page, not an empty one (`GET /logs/api` still carries `web.js_shell_unrecovered` / `web.challenge_blocked` for the blow-by-blow). A `"tier": "tavily"` body field forces ONLY the hosted Tavily Extract tier — the same probe the Settings **Test key** button uses, so a freshly-entered key can be verified from a handed-over token. |
| `POST /solve` | Run a URL through **ONLY the challenge solver** (byparr), skipping the direct+reader legs — so the stealth browser can be exercised in isolation against a walled URL (Reuters/WSJ/…) without a doomed direct fetch first. Same output shape as `/fetch`; a 400 distinguishes *solver unconfigured* from *byparr ran but still challenged / empty* (a real solve miss — pair with `GET /logs/byparr`). Shares the `web.fetch` scope. |
| `POST /fetch` `tier=tavily` | Force **ONLY the hosted Tavily Extract tier** (`scripts/debug-connect.sh tavily <url>`). A 400 distinguishes *the tier unwired* (no `JBRAIN_TAVILY_URL`) from *disabled / keyless / a genuine miss* (bad key or Tavily error) — so after a deploy the owner can confirm the Tavily key works against a real walled URL with a handed-over token, no PWA needed. Shares the `web.fetch` scope. |
| `POST /sdr/sweep` → `GET /jobs/{id}` | Sweep a band with `rtl_power` and report what was busy in it — a **background job**, because a five-minute sweep will not survive the tunnel's ~100s edge limit. Returns a waterfall PNG (base64), the occupancy table, and the sweep's own noise floor. **This TAKES A RADIO** as a real lease with the omnibox icon and Release. Since `APRS_CONTROL_PLAN.md` P0b the refusal is PER RADIO, so on a two-dongle box a sweep runs while APRS logs on the other one — it is refused only when it would open the radio something else holds, and the 409 names that radio. A measuring instrument rather than an agent tool, and deliberately so: a detector's thresholds have to be calibrated against THIS box's noise floor and spur pattern — per-box facts nothing documents, the same reason `/grounding` exists for a vision model's coordinate base — and guessing them ships a detector that reports the tuner's own artifacts as stations. **`occupancy` is a fraction of the window, not a peak in dB**: a one-off burst and a channel busy half the hour have the same peak. **`steady`** carries bins that never went quiet — a spur and a carrier held all window are the same measurement, so they are reported rather than dropped. `steady` compares each bin to its NEIGHBOURS' floor, not the sweep's, because "high" is a local claim — band-edge rolloff, spurs and each retune block's own noise all vary across a sweep. **Its threshold is calibrated against FM broadcast** (carriers up 24/7, so no keyed-up radio needed): a real transmitter clears its local floor by **+11 to +24 dB**, noise sits at p50 0.09 dB, and a quiet 2m band's worst bin reached +4.8 — so at 6 dB the detector finds all 13 stations on 88-108 and reports nothing on a silent band, with 3 dB of margin to the first false positive. Raising it loses real stations rather than sharpening anything. `steady: []` on a quiet band is therefore the right answer, not a miss. **`uncovered`** lists the spans the sweep did NOT measure — a 144-148 run once came back missing 145.872-146.206, a 342 kHz hole across live repeater channels, with nothing saying so; that was the reducer's own block-trimming and the same sweep now returns 1026 contiguous bins with `uncovered: []`. Silence is only evidence where the receiver was listening. **`include_csv=true`** returns rtl_power's own numbers: off by default because it is megabytes, but calibrating the detector against its own summary is circular — and calibrating against the returned PNG is worse, since the waterfall palette is stretched between the 20th and 99.5th percentile of the data, so brightness ratios off it mean nothing in dB. One wrong "1.76x" in this file came from exactly that. `channel_khz` says **how wide a signal is here**, and two things need the answer: it groups adjacent bins into one signal (by adjacency rather than snapping to a grid, because real band plans are not anchored at 0 Hz) AND it sizes the neighbourhood `steady` judges a bin against. 15 for 2m, 25 for 70cm/airband/marine, 200 for FM broadcast, thousands for a cellular carrier. **Off the narrowband bands it is not optional**: unfolded, a 200 kHz signal in a 5 kHz sweep reads as forty stations; unsized, the default 400 kHz neighbourhood sits *inside* a wide carrier, so the baseline is computed from the signal itself and the carrier hides. Zero leaves the bins alone and takes the narrowband default. **`revisit_s`** is the scale `occupancy` is a fraction of, measured off rtl_power's own timestamps: 5% of a 1 s revisit is a few seconds of traffic, 5% of a 60 s one is most of an hour. It is reported rather than assumed because it depends on how many retune hops the span needs — measured 1.0 s at both one hop and two on this box, so rtl_power retunes *within* its interval rather than multiplying it, but a span needing hundreds of hops cannot hold that. Zero means the clock could not answer. **A sweep is capped at a 60 MHz span** (`MAX_SWEEP_SPAN_HZ`), so the tuner's 24-1766 MHz range is never one call — surveying it is at least 29 sweeps, and a wider request is refused by the sidecar rather than trimmed. `bin_khz` is a hint that also coarsens with the span, because rtl_power picks a power-of-2 FFT per hop: 5 kHz asked over 1.4 MHz gave 2.7 kHz, 100 kHz asked over 60 MHz gave 85 kHz. `409` means another caller holds the radio this sweep would open, and says which; `400` below 24 MHz means **shortwave cannot be swept** — the radio reaches HF by bypassing its tuner and `rtl_power` hardcodes the other ADC branch, so it would tune something and measure nothing. Listening there works (`tune 10.0 --mode am`); surveying never will. `503` means this box has no radio (`sdr_url` unset). Poll `GET /jobs/{id}` until `status` is `done`; `png_base64` is a PNG, and `rows`/`csv_chars` are what separate a genuinely quiet band from a sweep that never ran. CLI: `scripts/debug-connect.sh sweep 144 148 --seconds 300 --channel-khz 15`, which submits and polls for you (`--no-wait` returns the job id instead). The CLI writes the PNG — and, with `--csv`, the raw numbers — to files under `--out DIR` and prints their paths in place of the blobs, so the reading is readable. |
> **Read 4xx, not 5xx.** MEASURED 2026-09-05: a route that answered `502` with a
> `detail` naming exactly what went wrong reached the console as the edge's own
> `error code: 502` page, the sentence gone. 4xx bodies survive the trip intact (a 400,
> 404, 409 or 422 arrives with its JSON); 5xx bodies do not. Two consequences worth
> designing around. A debug route that wants to TELL the owner something must answer
> `4xx`, or `200` carrying a verdict — never a 5xx with the reason in it, which is a
> sentence written to a channel that drops it. And when a 5xx does happen, the reason is
> in the container log rather than the response: the sidecar prints every refusal it
> returns (`logs sdr`), which is the one channel that survives both.

| `POST /sdr/reset?serial=…` | **Re-enumerate one dongle — the software equivalent of unplugging it.** An RTL-SDR left with transfers pending (an unclean teardown, a brown-out) can stay on the bus and stop answering descriptor reads: librtlsdr then enumerates it with blank strings, every `-d <serial>` lookup fails, and the radio looks absent while the USB scan still lists it. **Nothing else clears that** — not a container restart, not a rebuild, not an update — so before this the only fix was a person walking to the box, which is not a fix when nobody is there. One source of that unclean teardown was **this box's own capture route**, fixed 2026-09-04: it recorded with `subprocess.run(timeout=…)`, whose timeout branch is the normal path here (rtl_fm streams until stopped) and whose timeout handling is `process.kill()` — SIGKILL, which never runs the handler that cancels the USB transfer and closes the device. Captures now SIGTERM first. A dongle already in that state still needs this reset; new ones should stop arriving. Issues `USBDEVFS_RESET` on the device node, a port reset that makes the kernel re-enumerate exactly as a re-plug would. The node is resolved HERE from the supervisor's sysfs scan and never taken from the caller: sysfs answers from what the kernel cached at enumeration, so it still names the very device whose live reads have failed — and resolving by serial also means a reset cannot be aimed at something that is not an SDR. Refused with a 409 while a session holds that radio (the lease, so a reset cannot run under a decode); the OTHER dongle is untouched, so APRS keeps logging through it. The node number CHANGES as a result — a reset device returns at the next free address — so re-read the scan rather than reusing one. `404` means no radio by that serial in the scan. Same thing as the PWA's **Radios → the radio → Reset this radio**; the twin exists because a capability token is on a physically distinct path from the owner routes, and the situation this was written for was precisely a broken dongle with nobody home. CLI: `scripts/debug-connect.sh sdr-reset <serial>`. |
| `GET /client-vitals` | What the **browser** last reported about the top-bar vitals stream: frames seen, opens, errors, reopen counts, `readyState`, `sinceLastFrameMs`, the number of samples the browser is holding, and `clockOffsetMs` (how far the box's clock sits from the browser's, as estimated from the frames — samples are placed on the graph with that difference taken out, so a large figure here is a fact about the clocks, not a fault). The one read that separates *the box never sent a frame* from *the browser never received one* — states that need different fixes and are indistinguishable from the box, because a connection the browser declines to open (its per-origin cap, say) leaves no server-side trace at all. `sinceLastFrameMs` is the number that matters: the route emits one a second, so anything above a few thousand means the meter is blind however healthy the socket claims to be. `{"reported": false}` means nobody has opened the vitals detail since this process started, **not** that the meter is broken. Populated by the PWA beaconing every 15s while that screen is open. |
| `GET /logs/{service}` | Tail a container's logs, proxied to the supervisor (the single owner of docker access), mirroring the owner ops surface. |
| `GET /jcode/logs` | All **code-mode** logs in one pull — the jcode control server and its model gateway, each tailed and labeled. A not-running service is noted, not fatal, so it works mid-bring-up. |
| `GET /llm/gateway-logs` | Tail **llama-swap's** buffered log — swap decisions, health checks, and the slot lifecycle, where a slot is acquired on a request and **released** when its generation ends. The read that answers whether a Stop/disconnect halts decoding or the engine runs on. It does **not** carry llama.cpp's own output (that's the next row). 502 if the gateway is unreachable. |
| `GET /llm/upstream-logs` | Tail **llama-server's** own stdout, which `gateway-logs` cannot show: the slot lifecycle, per-request prompt-eval throughput, context-checkpoint evictions, and a failed load's reason. Reads the history burst llama-swap replays on `/logs/stream/{stream}`; `stream` defaults to `upstream` (all models interleaved) or takes a served model id to isolate one. **It does not carry a load's per-buffer memory breakdown** — verified 2026-08-19: the model loader prints nothing at the default verbosity 3, so a load reads as a ~1.4 s silent gap here *and* in the `local-llm` container log. Use the `local_gateway.footprint_measured` event (the device delta) for a load's memory. An empty body means the engine has printed nothing since llama-swap started — usually no load since boot, not a fault. |
| `POST /llm/drop-page-cache` | Reclaim the page-cache copy of on-box model weights (`?models=a,b` for specific catalog ids, omit for all). The box serves with `--no-mmap`, so every load leaves the weights resident **twice** — GTT plus the page cache the read filled — and unloading frees only the GTT copy. Since `host_metrics` counts page cache as **used**, that residue shrinks the admission budget for every later load: measured 2026-08-19, 29.19 GiB of stale `gpt-oss-120b` cache left host pages free at 86.2 GB and got `qwen3-coder-next-q8` (needs ~95.5) refused for want of 15.3 GB nothing was using. Safe while models are resident — `POSIX_FADV_DONTNEED` drops clean cache only, never the GTT copy being served from, and weights are read-only. A null `freed_gb` means `cachestat(2)` could not measure the drop (blocked by the container's seccomp profile on this box), **not** that nothing was freed. Previously this needed host shell (`deploy/update-inner.sh`'s global `drop_caches`), which the owner does not have. |
| `GET /host/metrics` | The host's live hardware telemetry, proxied from the supervisor (the only container that reads `/sys`): GPU busy %, APU package power, load average, memory/swap/disk, fan RPM, per-container memory — plus **`gpu_mem`** (amdgpu GTT/VRAM used, the iGPU's slice of unified RAM that no process RSS shows), a **`mem_breakdown`** of key `/proc/meminfo` lines, and cumulative **`net`** / **`disk_io`** byte counters. The console's one physical read — pair it with a turn to watch the GPU gauge across a Stop, or read `gpu_mem.gtt_used_bytes` with no model loaded to spot device memory a teardown failed to release. |
| `GET /host` | Live host memory/swap/disk/load + **per-container RSS** and **raw per-process RSS** (both biggest first), proxied from the supervisor. Attributes the unified-memory total down to individual processes — the per-process list (via `docker top`) tells the 120B's `llama-server` from the vision model's, since the `local-llm` container runs a separate process per loaded model. Answers "what is using the box's RAM" the read-only meter can only total. (For the iGPU/GTT share the process list can't show, read `gpu_mem` on `/host/metrics`.) |
| `GET /sdr` | **Is the USB software-defined radio there, what exactly is it called, and is anything holding it?** Proxied from the supervisor, which enumerates `/sys/bus/usb/devices/`. Deliberately the cheapest possible probe: naming a USB device is a **sysfs read**, so this needs no device passthrough, no privileges, and no `sdr` container — it answers "will this work?" *before* any of that exists. Returns a verdict, not a dump: `found`, `ready`, a one-line `summary`, and a `next_step`. The distinction that matters is **found-but-not-ready** — a dongle claimed by the kernel's DVB-T driver (`dvb_usb_rtl28xxu`) is the expected first result on a stock Ubuntu box, and the verdict names the blacklist rather than leaving you to know it. `sysfs_readable: false` means the supervisor cannot see `/sys` at all, which is a different fault from "no dongle" and would otherwise be indistinguishable from it. `usb_device_count` proves the scan ran even when nothing SDR-shaped turned up, and **`devices` carries the whole bus** — the not-found verdict asks you to identify the dongle by its `usb_id`, which needs the list in the same response since the console has no other window onto USB. Console: pick **sdr — probe the USB radio**. See `../plans/SDR_RADIO_PLAN.md` (S0a). |
| `POST /sdr/capture` | **Tune the radio, record a few seconds, and run it through whisper.** The end-to-end proof for the SDR plan's S0b-ii gate, driven from the console so it needs no terminal. Query params only — `frequency_mhz`, `seconds`, `mode` (`fm`/`nfm`/`wbfm`/`am`/`usb`/`lsb`), `gain`, `transcribe` — **never a URL or a host**, and the frequency is bounded to the radio's real range both here and in the sidecar — **0.1-1766 MHz, which is TWO signal paths**: above 24 MHz through the tuner, below it straight into the ADC with the tuner powered down, so there is no gain control there at all. The `stream.py` SSRF guard is neither used nor widened. Read **`audio_peak`/`heard_something` before the transcript**: a dead antenna, a mistuned frequency and a working capture of silence all return audio of the same length, and only the level tells them apart — whisper will confabulate words over noise, so a transcript from a silent capture is an artefact, not a reception. `409` means another caller holds the radio this capture would open, and says which; `503` means this box has no radio (`sdr_url` unset). Console: pick **tune — capture + transcribe FM** and enter a frequency. CLI: `scripts/debug-connect.sh tune 99.3 --mode wbfm`, or `tune 10.0 --mode am` for shortwave. **Measured 2026-09-04** on a desk whip: the direct-sampling path enables correctly (`Enabled direct sampling mode, input 2/Q`) and medium wave reads FLAT at 0.09-0.12 across 900 kHz — no station structure, which is the antenna rather than the radio. A whip is electrically tiny against MW's 300 m wavelength. `audio_peak` is named for what it measures (F9): the loudest sample of the DEMODULATED AUDIO, **not a signal level**, and not the same quantity as the spectrum path's true dBFS per bin. It is not even comparable across the radio's own two signal paths — idle airband AM reads 0.21 because the tuner's AGC amplifies its own noise, while HF has no gain stage to do that. |
| `POST /sdr/soapy-probe` | **Does this radio really behave the way the I/Q spectrum engine assumes?** F0 of `../plans/SDR_IQ_SPECTRUM_PLAN.md`, run from the console because the owner has no terminal to run `SoapySDRUtil` from (CLAUDE.md #10). It drives the sidecar's `radio.py` against a real dongle and returns a **verdict, not a dump**: `ok`, a one-line `summary`, and a `findings` list naming any claim that did not hold, with the evidence under it. Seven claims, each one the engine is already written against — SoapySDR enumerates the dongles; **`serial=` opens the one it names** (everything per-radio here depends on that); **`direct_samp=2` reads back as 2**, which is the whole shortwave story; **`setFrequency` and `setSampleRate` work on a LIVE stream with no rebuild**, which is why pan and zoom stop blanking; **`SOAPY_SDR_OVERFLOW` is really reported** under a second of induced backpressure, where `rtl_sdr` dropped samples silently; the **achieved rate** comes back off librtlsdr's 28.8 MHz divider unchanged; and — the one nothing else can answer — **`bufflen` actually took**. That last one is measured as a callback period on purpose: librtlsdr replaces a bad `bufflen` **silently** with 262,144 bytes (the 1.9 fps ceiling the engine exists to remove) and `getStreamMTU` reports the value that was *asked for*, so neither the call succeeding nor the MTU agreeing proves anything. It also captures one frame through the real FFT and reports the strongest bin against the frame's own median — **excluding the centre bin**, because every direct-conversion receiver puts a DC offset spike exactly at the tuned frequency and a peak there is the receiver looking at itself (`dc_db` reports it separately). A frame whose median has fallen to the zero-magnitude floor is reported as **`dead`**, a finding rather than a pass: a receiver with no noise floor is not receiving. **Read `settled` before `dead`.** Measured 2026-09-05: a single frame taken shortly after the direct-sampling branch was switched reported `dead` at 5.0, 7.15 and 10.0 MHz — while `spectrum-probe`, streaming on the SAME dongle at the same frequency and rate, found 40 m perfectly alive at a -51.5 dBFS floor. Both cannot be true of the radio, so the single frame was the weaker reading; the probe now takes ten and judges the last, and `settled` says the first was empty when a later one was not. An earlier version of this row concluded from that single frame that this box's HF input was dead — it is not, and that conclusion reached the owner before the engine existed to contradict it. **Measured on the fixed probe**: 7.2125 MHz answers `dead: false`, `settled: true`, a -51.4 dBFS floor with a peak 5.9 dB over it — matching the streaming engine on the same dongle to within a dB — while 99.3 MHz answers `settled: false` with a station 24.7 dB up. So the settle is real and specific to the direct-sampling branch, and `ok: false` on such a run is the probe saying *read me carefully*, not the radio failing. It also **measures the retune settle**, which is the number a hopping sweep is made of: the samples in a hop are microseconds and the discard after each retune is milliseconds, so `SETTLE_S` alone decides whether a wide band redraws twice a second or twenty times. It was chosen and never measured. The method is a discard of zero and a stopwatch made of sample indices — tune away, tune back with the timed discard switched off, read one continuous block, and find where its slice-by-slice level stops differing from the settled level by more than that level's own deviation. Read `retune_settle.settle_ms` against `configured_ms`: **too SHORT is a correctness fault** (a hop then carries the previous hop's samples and draws them at a frequency they are not on), **too long is only a speed one** — but it is the one that decides whether a wide band is watchable. A disagreement smaller than `window_us` is inside the method's own resolution and is not reported. **Ask it on the path the sweep uses, or the answer is about a different radio**: the defaults (`--mhz 10.0`, `--rate 256000`) open the DIRECT-SAMPLING branch, where the tuner is bypassed and a retune is nearly a no-op, and make a slice 2 ms wide; measured 2026-09-05 they returned `0.0 ms`, which was true and irrelevant. Use `--mhz 100.1 --rate 2400000`. **And read `saturated` before `settle_ms`**: a settle equal to `span_ms` is the stopwatch running out, not the radio settling — the first version of this spanned 80 ms and reported `worst_ms: 80.0`, which was the span. When `saturated` is non-zero the true settle is *at least* `span_ms` and is otherwise unknown. **Measured 2026-09-05 on the tuner path: 61.2 ms median, 132.1 ms worst, against 50 ms configured** — too short, so on a hopped band the 0.43 ms a hop actually keeps is typically all stale and each hop draws its neighbour's spectrum. It also reports `hop_cost`: `setFrequency`, the bare flush and the read timed apart, with barriers timed at 0, 50 and 150 ms. Read `hop_cost.bound` — `real-time` means every millisecond discarded comes off every hop and the fix is a shallower USB pipeline, `memcpy` means the stale samples are already captured and the settle can cover the worst case for free. It is a reading, not a finding: both answers describe a healthy radio. **Measured 2026-09-05**: `setFrequency` 32.0 ms, flush 0.02 ms, discard 49.5 ms at a 50 ms setting, read 0.21 ms — `real-time`, so the flush really does empty the pipeline and what arrives after it is live. It also reports `gain`, and raises a finding when the tuner's gain is **automatic** — which it is, because nothing in this engine has ever set it: a waterfall whose gain moves has a dB scale that means nothing row to row, and every hop seam becomes a gain step drawn as if the band had changed. `retune_settle_fixed_gain` asks the settle again with the gain nailed down (and hands it back), because what the stopwatch watches is a level and a gain loop looked a better candidate than a PLL. **It was not**: measured 2026-09-05 the tuner is already MANUAL, and fixing the gain moved the median only 60.8 ms to 51.4. The same run found the gain sitting at **0.0 dB, the bottom of a 0-49.6 dB range** — a driver default nothing here ever overrode, which is its own finding and costs every weak signal. `settle_after_flush` is the control that follows: the same stopwatch with the disturbance being `activateStream` and NO frequency change. A transient that survives that belongs to the flush, not the tuner. **It did not**: measured 2026-09-05 a flush with no frequency change disturbs the output for **0.0 ms on every trial**, against 59.7 ms median and 131.2 ms worst for a whole hop. So the settle is the TUNER's and it is real — not USB backlog, not the gain, not the flush. `SETTLE_S` is now 0.15 and FIXED. An adaptive discard was tried and reverted the same day: it stopped when the level stopped moving, which cannot tell settled from steadily WRONG — a discard has no reference level, because the new frequency's level is the thing it is waiting for. **Verified**: `settle_after_barrier` reports 0.0 ms median / 7.7 ms worst left behind. The cost is real and shows up as `spectrum-probe --section fm-broadcast` at **0.33 fps**, so the probe also runs a `queue_ladder`: the settle at each candidate USB queue depth, each argument tried alone. The buffer period is 10.27 ms and the driver queues 15 of them — 154 ms — against a settle of 61 median / 134 worst on BOTH dongles, which is a queue depth rather than a PLL. **It was**: measured 2026-09-05, `buffers=4` takes the worst case from 113 ms to 20 and `buffers=2` to 10, while `asyncBuffs` moves nothing — so the engine sets `QUEUE_BUFFERS = 4` and `SETTLE_S` is 0.03. Read `queue_ladder.rungs` if a hop ever looks stale again. **TAKES A RADIO** for a few seconds through the same lease as everything else, so it is refused with a `409` naming the job that holds it and released in a `finally`; `serial` picks which dongle (the point of the probe on a two-dongle box) and defaults to whatever the resolver gives a general job. `404` means no radio by that serial in the USB scan; `400` refuses 14.4-24 MHz, which is reachable by neither signal path and would measure the frequency it folds onto instead. CLI: `scripts/debug-connect.sh soapy-probe --serial 09022796`, then again for the other dongle. |
| `POST /sdr/spectrum-probe` | **Did the engine swap actually work on this radio?** F6's twin of `soapy-probe`, and it exists for the same reason: a live spectrum is an owner route behind a websocket, so before this the only way to know whether F6 worked on real hardware was for the owner to open the Radio tab and look — and an owner with no terminal cannot be the test harness for their own box (CLAUDE.md #10). "It renders" is not a measurement of a frame rate or a bin width anyway. It runs the **real** decision rather than a copy of it: `_span` picks the range, the width and the one-hop capture exactly as `POST /api/sdr/spectrum` does, so a bug in that choice shows up here instead of hiding behind a probe that chose differently. It then starts a genuine spectrum session, watches it for a few seconds and gives the radio back. Three claims it can fail, each invisible from anywhere else. **Which engine ran** — the sidecar drops to `rtl_power` at RUNTIME when a radio will not open, so `rtl_power` answering a request that named a capture means the fallback fired, and a silent downgrade is a waterfall quietly at a tenth of the rate it claims. **Whether `bin_hz` on the wire is exactly `rate / bins`** — a frame declaring a width the transform never used is the one failure nothing downstream can see, because the PWA draws bin `i` at `start + i * bin_hz` and believes it. **What the frame rate really is** — `rtl_power` clamps its interval to `>= 1s` in its own C, so anything above ~1.5 fps IS that ceiling being gone, and no static reading proves it. A frame whose every bin sits at the zero-magnitude floor is reported as the antenna or the input rather than as the engine's failure. **TAKES A RADIO** for those seconds through the same lease as everything else: a `409` names the holder, and the session is released even when the probe fails, because the owner has no terminal to free a radio a diagnostic walked away from. CLI: `scripts/debug-connect.sh spectrum-probe --section 2m-ssb`, or `--from`/`--to` for a hand-typed range. |
| `POST /sdr/listen-probe` | **Does the numpy demodulator really work on this radio?** The twin of `spectrum-probe` one layer over: that one retires the claims the spectrum engine is written against, this retires the ones `deploy/sdr/demod.py` is. Both exist for the same reason — listening is an owner route and the audio is an MP3 stream, so before this the only way to know whether the demodulator worked on real hardware was for the owner to press play and listen, and an owner with no terminal cannot be the test harness for their own box (CLAUDE.md #10). It starts a genuine listening session, holds it for a few seconds and gives the radio back, returning a **verdict, not a dump**: `ok`, a one-line `summary`, and `findings` naming any claim that did not hold. Four of them, none provable against the synthetic signals `test_sdr_demod.py` uses. **Which engine ran** — the listen pipeline drops to `rtl_fm` at RUNTIME when a radio will not open for our own samples, which is right and whose silence is not: on `rtl_fm` there is no tuning view at all and nothing else says why. **Whether the station is where the offset tuning says** — the radio sits `LISTEN_OFFSET_HZ` above it and the mixer takes that back out, so `view.strongest_offset_hz` is the number that catches the two being out of step, which on a narrowband channel is silence from code that reads correctly in both places. It is measured as the **midpoint of the 6 dB shoulders**, the same rule `frontend/src/sdrTuning.ts` draws with and deliberately not the argmax: an FM carrier's top is flat, so the loudest bin is wherever the noise peaked across a plateau a hundred bins wide, which on a 32 kHz row is an answer a third of the view out. **Whether the audio is a signal rather than silence or a rail** — a peak at 0.0 is a chain not connected and a peak at 1.0 is one clipping, and both look like "it ran" from outside; the peak is sampled ALONGSIDE the frames rather than once at the end, since a channel loud for four seconds and quiet for the fifth would otherwise read as silent. **How many USB buffers the driver threw away** — `overflows` is the one measurement no fake radio can produce, and the one worth re-reading now that the same stream has to run continuously for audio rather than in frames for a picture: on a waterfall a dropped buffer is one row slightly wrong, on audio it is an audible click. **TAKES A RADIO** for those seconds through the same lease as everything else — a `409` names the holder, and the session is released in a `finally` because the owner has no terminal to free a radio a diagnostic walked away from. **MEASURED 2026-09-05, the first captures through the numpy demodulator.** The engine came back `iq` on every run with **zero USB overflows** across ~30 s of continuous 2.4 MS/s capture at `QUEUE_BUFFERS = 4` — the risk no fake radio could exercise — at 10.0-10.1 fps, with the offset tuning landing the station within **48 Hz** on a 93.75 Hz bin. It also caught two defects, both invisible without hardware. **The listening path never set the tuner gain**: `radio.Radio.open` does not touch it and `_start_iq_listen` did not either, so a session ran at whatever librtlsdr left the dongle at (0.0 dB, the bottom of 0-49.6) while `rtl_fm` hands the tuner to its own AGC — 162.550, NOAA weather transmitting continuously, read -64.5 dBFS. With AGC restored it reads **-47.3, +17 dB**, dead on frequency. **And the probe's own clipping test was wrong**: `peak >= 0.999` on FM is not a clipping test at all, because a discriminator turns every burst of noise that momentarily overpowers the carrier into a full-scale impulse, so one sample in 1600 pins the peak while the rest is a good voice. `clipped_fraction_max` replaced it, and its threshold is measured rather than guessed: 162.550 at 14 dB SNR clips 1.0-1.25% with RMS 0.40, while 96.5 / 99.3 / 104.1 on wbfm at **31-40 dB SNR clip 0.0%** with peaks at 0.37-0.42 — so the weak-signal clicks are the noise and the deviation scaling is right. It did not move with the tuner gain either (1.00% on AGC, 1.19% at 40 dB), because a discriminator is blind to amplitude — which is also why `audio_peak` can never answer a question about signal strength and `view.snr_db` is the number that can. CLI: `scripts/debug-connect.sh listen-probe --mhz 146.94`, `--mode wbfm --mhz 96.5` for the broadcast dial, or `--gain 40` to hold the tuner still and compare two runs. |
| `POST /refresh` | **Pull `main` and rebuild ONE service** — the fast path, and the reason it exists is a measured cost rather than impatience. `/update` pulls and then rebuilds the world: backup, quiesce, every image, every model unloaded, about **ten minutes**. That is the right price for shipping and the wrong one for asking the radio a question — and the sdr sidecar is pure Python behind an apt-only image, so a one-line change to a measurement cost a full system update to try. On 2026-09-05 answering one question about the tuner's retune settle took five of those cycles, each ten minutes, and the owner has no terminal to shortcut it with (CLAUDE.md #10), so the shortcut has to be a route. **It takes no ref, and that is the same security property `/update` has**: the inner script resets the source mirror to its *tracked upstream*, so a token can ask for what a merged PR already put on `main` and nothing else — a ref parameter here would turn a capability token into remote code execution. `service` is validated by the supervisor against the live compose service set before it reaches a shell-quoted command. **It is a PARTIAL deploy, deliberately**: the mirror moves to `main` for every service while only the named one is rebuilt, so the api can be running older code than `src` describes — and `/version` reports the **api's** build, so it will not show the change. It also does not refresh the host helper files, so a `docker-compose.yml` or Dockerfile-path change still needs the full update. Use it to iterate, `/update` to ship. Rebuilding recreates the container, so anything the service held — an sdr lease, a live spectrum — ends. `409` while another one-shot runs (it pulls the shared mirror, so it must not race an update over it); `404` for an unknown service. CLI: `scripts/debug-connect.sh refresh sdr`, then `refresh-status`. |
| `POST /update` | **Deploy: pull `main`, rebuild, restart** — the PWA's Ops → Update button, reachable with a token. Until this existed the console could *watch* an update and not *cause* one, so every deploy waited on the owner being at the PWA; that is a real cost when the thing being deployed is the only way to answer a question about the hardware, which is the shape of every probe-and-fix cycle. **It takes no ref, and that is the security property, not an omission**: the supervisor builds whatever `main` is, so a token can only ask for what a merged PR already put there — it cannot choose the code it deploys. Widening this to an arbitrary branch or sha would turn a capability token into remote code execution on the box, and no operational convenience buys that. What it *does* grant is worth stating plainly: anyone holding a live token can restart this box's services and roll it to current `main`. `DebugDep` is uniform — there is no per-token scope on this surface — so it reaches **every token ever minted**, not only ones issued afterwards; the mitigation is the one this surface already has, in that tokens are revocable, time-boxed and listed for the owner, so the answer to "who holds one" is to revoke rather than to reason about it. `409` while an update is already running (the supervisor's own mutual exclusion over one-shots — two updates racing one working tree is how a box ends up serving a half-built image). Afterwards read **`/version`**, not just `/update/status`: a restart and a rebuild look identical from outside and only `git_sha` tells them apart — MEASURED 2026-09-05, an update that restarted containers without rebuilding reported a fresh `started_at` against a `build_time` from before the merge. CLI: `scripts/debug-connect.sh update`, then `update-status`. |
| `GET /update/status` | The last system-update one-shot's state + log tail (git pull + rebuild + model sync), proxied from the supervisor — the console's window into an update that runs outside the compose project. |
| `GET /provision/status` | The last local-model **download** one-shot's state + log tail (the PWA "Download" action, `deploy/local-models-sync.sh`). The verbose per-model weight-pull output — resolved repo, include globs, and the hf failure reason (404 / auth / disk / network) — so the console can answer *why* a model download failed. |
| `GET/PUT /llm` | Read or **switch** which model serves each task — live, no restart. Shares validation with the owner settings screen. |
| `POST /llm/local-models/{id}/load\|unload` | Warm or evict a local model on the gateway. |
| `GET /llm/local-models/{id}/slots` | llama-server's `/slots` for one **resident** model — per-slot state, and on a speculative build the `speculative` object that says whether drafting is actually running (which `/props`'s dead `speculative.types` field cannot answer). |
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

> ⚠️ This is a **first-time host step** — a `.env` edit plus `jbrain up` on the box's shell,
> which the owner cannot run remotely (CLAUDE.md non-negotiable #10). The terminal dependency
> here is a known gap to design out (a PWA-side enable would need its own authorization
> story); until then, enabling needs someone at the host.

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
