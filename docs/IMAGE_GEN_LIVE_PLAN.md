# Live image generation — progressive preview + stop (Wave G7)

Extends the image-gen feature so a generation shows **live progressive previews**
(updating at ~25% step intervals) and can be **stopped mid-render** from the chat.
With on-box Qwen-Image taking ~3.5 min for a 20-step render, a blind 3.5-minute
wait with no cancel is the worst part of the current experience — this fixes it.
Binds on `docs/DEVELOPMENT.md`, `docs/PROCESS.md`, `docs/DESIGN.md`, and the
`CLAUDE.md` non-negotiables. Builds on Waves G1–G5.

## The problem (grounded)
`generate_image`/`edit_image` run as a **blocking tool call inside the agent turn**
(`agent/imagegentools.py` → `await imagegen.generate(spec)`), returning a
`generated_image` view only when the tool finishes. The driver
(`image_gen/comfyui.py`) POSTs `/prompt` then polls `/history` for the final PNG.
There is no path for intermediate previews or a mid-render stop.

## What ComfyUI gives us
- **Progress + previews** over its `/ws` WebSocket: `progress` messages
  (`value`=current step, `max`=total) and binary `b_preview` frames (preview JPEGs)
  during sampling. Previews require the server started with a preview method.
- **Stop**: `POST /interrupt` cancels the running prompt (single-GPU box → one job
  at a time, so a plain interrupt is unambiguous).

## Architecture (Path A — extend the turn SSE)
Chosen over a new WebSocket side-channel: the agent turn is **already SSE** with
typed `ChatEvent`s the frontend decodes via `applyEvent`, so previews ride the
existing stream as a new event type — no new socket, no turn-id coordination, and
the existing `AbortController` already tears the stream down.

1. **Driver → WebSocket** (`image_gen/comfyui.py`): a new `generate`/`edit` path
   that POSTs `/prompt`, then consumes `/ws?clientId=…` for `progress` + `b_preview`
   frames and the final `executed` output, calling an injected
   `on_progress(step, total, preview_jpeg | None)` callback. Keeps the HTTP-poll
   path as the fallback when no callback is supplied (and for the fake in tests).
   Previews are emitted at the **25/50/75/100%** step boundaries (throttled, not
   every step — keeps the SSE light).
2. **A progress sink on `ToolContext`** (`agent/loop.py`): an optional
   `emit_progress` the loop wires to the turn's SSE yield. The image tool calls it;
   tools that don't, behave exactly as today.
3. **`ToolProgressEvent`** (`agent/contracts.py`): `{tool_call_id, step, total,
   preview}` where `preview` is a **base64 JPEG data URI the backend authors** for
   the ephemeral frame (invariant #9 forbids the *model* authoring a URL; this is
   app-authored, like the `<img>` src the view component builds). Previews are
   throwaway — they are NOT written to the blob store or `generated_images`; only
   the FINAL image takes the existing blob-store + row path, unchanged.
4. **Stop** (`api/image_gen` or extend `image_settings`): owner-only
   `POST …/interrupt` → ComfyUI `/interrupt`. The in-flight `/ws` await ends, the
   tool returns a clean "generation stopped" result, and jerv's turn continues
   (no whole-turn abort). A stopped render writes no image row.
5. **Service preview method** (`docker-compose.yml`): start the `comfyui` service
   with `--preview-method auto` (or `taesd` for sharper previews) so `b_preview`
   frames are emitted. One-line compose change + a re-up.

## Frontend (GUI gate first)
- **GUI gate (mandatory, before code):** three interactive mocks of the in-chat
  **generating** state (evolving preview + % + Stop) → owner picks; the choice lands
  in `docs/mocks/`.
- **Stream handling** (`agent/transcript.ts`, `agent/types.ts`): handle
  `ToolProgressEvent` — hold the latest preview + step on the streaming tool
  activity; the chosen mock dictates the render. The final `generated_image` view
  replaces the preview on completion.
- **Stop control** (`api/client.ts` + the chat view): a Stop button on the
  generating surface calls the interrupt endpoint.

## Dependency note (rule, per PROCESS)
The driver needs a **client** WebSocket (the existing `live.py` is server-side
FastAPI WS). `websockets` is almost certainly already present (uvicorn[standard]
depends on it) — to confirm; if a new dep is unavoidable it's flagged here, not a
stop. No new dep on the frontend (the preview is a data URI on the existing SSE).

## Non-negotiables check
1. **LLM adapter** — n/a (image service); the progress callback carries no model
   call.
2. **Storage** — the FINAL image still rides `blob_store` + a `generated_images`
   row (rule 2/3); ephemeral preview frames are never persisted (in-memory SSE
   only), so they need no store.
3. **RLS / owner** — the interrupt + any new endpoint are **owner-only**; the
   generation row stays RLS-scoped as today. A stopped render writes nothing.
4–6. Comments why-not-what; tests with code (the WS driver against a fake ws
   server / scripted frames, the progress event end-to-end, the interrupt endpoint;
   security paths 100%); Conventional Commits; per-wave PR; CI green.
7. Previews + final image remain **chat artifacts** — never notes/RAG.
8. `dev-setup.sh` unchanged (no dev bootstrap step); the compose preview-method
   flag is the only setup change, carried with the code.

## Waves
- **G7a — backend live pipeline:** WS-consuming driver + `on_progress`,
  `ToolProgressEvent` + the `ToolContext` sink + loop wiring, the interrupt
  endpoint, the compose preview flag. Tests. (No UI yet — the events just flow.)
- **G7b — the UI:** the GUI gate, then the in-chat generating component + Stop,
  consuming the events from G7a. Mock-mode fixtures + tests.

## Open risks
- **Preview cadence vs cost:** 25/50/75% frames are tiny (latent previews), but
  confirm the data-URI size stays modest on the SSE; throttle harder if needed.
- **`taesd` vs `auto` preview quality** — tune on-box (auto is cheap/low-fidelity;
  taesd is sharper but loads a small decoder). Default `auto`, revisit.
- **Interrupt granularity** — fine for a single-user box; revisit if concurrent
  generations ever exist.
