# RapidOCR — deterministic CPU OCR: sidecar, cross-validation, jerv + sandbox tools

> **Status:** In progress · **Last verified:** 2026-08-11 · **Waves:** R0✅ R1✅ R2✅ R3✅ R4✅ R5◻️ (R1–R4 all shipped on-branch; CI covers the client/pipeline/tools/bridge with fakes + a localhost stub and real-Postgres for the store-both round trip. R5 = on-box sign-off against the live RapidOCR sidecar — image + PDF quality, idle load/unload timing — plus a decision on whether to build the `rapidocr` image in CI, deliberately left out for now to keep the image job light.)

> Reconciled with the root `CLAUDE.md` non-negotiables: the sidecar is reached only
> through a pinned-URL client on the api (no model-supplied host); OCR text is written
> as `attachment_extracts` cache rows on an RLS-scoped session (no new table → no new RLS
> test; a migration only if we persist an agreement score, §R2); the vision LLM path is
> unchanged (still through the adapter). The health-safety confidence cap on OCR-derived
> facts is **preserved regardless of engine** (§R2) — deterministic OCR raises *trust in
> the verbatim string*, never a fact's auto-supersede power.

Add **RapidOCR** (PP-OCR via ONNX Runtime, CPU-only) as an on-box service, use it to
**cross-validate the VLM text extraction** (deterministic transcription vs. the model's
reading), and expose a **direct OCR tool** to both jerv and the jcode sandbox — a verbatim
"read the text in this image" that never hallucinates, distinct from the semantic
`analyze_image`/`vision.ocr` path.

---

## 1. Why

The box's OCR is **VLM-based** today (`backend/src/jbrain/ingest/ocr.py` → the `vision.ocr`
route). That's great for messy/complex captures but it's a *model reading* — it can
hallucinate text, which is exactly why OCR-derived facts are confidence-capped at 0.7. A
deterministic engine gives us three things a VLM can't:

1. **A hallucination check** — when RapidOCR and the VLM agree on a string, we trust the
   verbatim text; when they diverge, we have a signal.
2. **A trustworthy verbatim citation** — exact transcription for quoting/searching.
3. **A cheap, swap-free tool** — RapidOCR runs in its own container and never evicts the
   coder or a jerv model (unlike a VLM OCR call, which cold-swaps the box's one big model).

## 2. Decisions (owner, 2026-07-31)

| Decision | Choice |
|---|---|
| Container | **Standalone `rapidocr` service** (internal sidecar, mirrors `searxng`/`reader`) |
| Pipeline role | **Store both rows** — keep the VLM `ocr` row, add a RapidOCR row; downstream picks (§R2) |
| Availability | **Stock always-up container**, but the **engine lazy-loads + idle-unloads** (§R1) so idle RAM is just the process baseline |
| Sandbox tool gate | **Always-on** — OCR of a local file touches no internet, so no gate (like `git`) |
| jerv tool | **Yes** — a direct `ocr` tool, separate from `analyze_image` |

**Footprint (lazy-loaded, not always-resident).** The container is stock/always-*up*, but
the OCR **engine lazy-loads on the first `/ocr` and unloads after an idle TTL** — mirroring
the LLM gateway's residency eviction, far cheaper here (§R1). So idle RAM is just the
process baseline (~150–200 MB: Python + onnxruntime + opencv), not the loaded inference
sessions; the first OCR after a quiet spell pays a ~1–2 s cold start. The weights are tiny
(~15 MB PP-OCR models) — the runtime baseline dominates, which is *why* unloading the model
reclaims only the variable part; true zero-when-idle would need scale-to-zero or in-process,
both rejected as over-engineering at this size (§6). Separate from the LLM residency budget
(not a swap model), so it never fights the coder; `mem_limit` bounds it.

## 3. Architecture — where it slots

```
ingest OCR job ─┐                          jerv `ocr` tool ─┐
sandbox `ocr` ──┤─→ RapidOcrClient (api) ─→ rapidocr sidecar (POST /ocr, internal net)
                └─→ (PDF? rasterize via pdf_page_images first, one image per page)
```

The sidecar is **image-only** (one image in → text + lines + score out); every PDF is
rasterized by the *caller* (reusing `jbrain.ingest.imageprep.pdf_page_images`), so the
service stays a dumb, stateless OCR box. The `api` is the sole caller (pinned
`rapidocr_url`), exactly like `SearxngClient`.

## 4. Waves

### R1 — the `rapidocr` sidecar + `RapidOcrClient`
- **Service** (`deploy/rapidocr/`, `deploy/Dockerfile.rapidocr`): `python:3.12-slim` +
  `rapidocr-onnxruntime`, `onnxruntime`, `opencv-python-headless`, `pillow`, `fastapi`,
  `uvicorn`. One route: `POST /ocr` (base64 or multipart image) → `{text, lines:[{text,
  box, score}], mean_score}`. Internal network, no published port, **no profile** (stock).
  `mem_limit` ~1 GB. Compose comment mirrors `searxng`'s.
- **Lazy engine (load/unload on demand).** The RapidOCR engine instantiates on the first
  `/ocr` and is freed after `RAPIDOCR_IDLE_TTL_SECONDS` (default 300) of no calls, so idle
  RAM is the process baseline, not the loaded sessions — the JBrain residency idiom, applied
  to a ~15 MB engine. A per-process lock serializes (re)load so concurrent calls don't
  double-instantiate; the container **health check must not touch the engine** (or it would
  defeat the idle-unload). Cold start after idle is ~1–2 s, folded into that first call.
- **Config**: `rapidocr_url: str = "http://rapidocr:8000"` (empty ⇒ degrade to VLM-only,
  same fail-open-to-old-behavior as `reader_url`).
- **Client** `jbrain/vision/rapidocr.py` (`RapidOcrClient`, mirroring `SearxngClient`):
  pinned base URL, injectable `httpx` transport, `async def ocr(data: bytes, media_type)
  -> OcrResult`; raises `OcrServiceError` on non-2xx/unreachable. App-lifetime singleton on
  `app.state.rapidocr`.
- **Tests**: fake transport — success, empty result, non-2xx → error, unreachable → error.
- No `dev-setup.sh` change (the sidecar is a container; tests fake the transport).

### R2 — parallel cross-validation in the ingest OCR pipeline
- In `OcrPipeline.ocr_attachment`, when `rapidocr` is configured, run
  `RapidOcrClient.ocr(image)` **in parallel** with the `vision.ocr` call (`asyncio.gather`);
  for PDFs, OCR each rasterized page alongside `ocr_pdf_pages`.
- **Store both** `attachment_extracts` rows, distinguished by `tool`:
  - VLM row — `kind="ocr"`, `tool="local:<vlm>"` (unchanged).
  - RapidOCR row — `kind="ocr"`, `tool="rapidocr"`.
- **Downstream selection (the deferred "downstream decides"):** the chunker
  (`ingest.extract.image_segments`) must not double-count the same text into facts.
  **Recommended default:** when both rows exist, the **RapidOCR row is the chunked/citeable
  transcription** (deterministic, verbatim) and the VLM row is retained for comparison but
  not independently chunked. One line of chunker selection (`prefer tool="rapidocr"`), no
  schema change.
- **Divergence signal:** compute normalized text agreement and record it via `flow_trace`
  (no schema change). *Open sub-decision:* if we want the agreement score persisted for a
  review surface, add a nullable `provenance`/`agreement` column to `attachment_extracts`
  (one migration + touch the RLS-covered table) — **out of scope for R2 unless a review UI
  needs it.**
- **Health-safety invariant preserved:** OCR-derived facts stay confidence-capped
  regardless of engine. Deterministic corroboration raises *trust in the string*, never a
  fact's auto-supersede power (the capped-at-0.7 rule that stops a low-confidence health
  numeric from silently overwriting).
- **Fallback:** `rapidocr` off/unreachable ⇒ today's VLM-only path (log + degrade).
- **Tests**: both rows persisted; high-agreement vs. divergent; degraded (service off);
  the confidence cap holds; PDF per-page path.

### R3 — jerv `ocr` tool (direct, deterministic)
- `build_ocr_handlers(client, blobs)` → an `ocr` tool: `arguments={attachment_id}` (a chat
  image the owner shared), resolved to bytes via the blob store exactly as
  `analyze_video`/`analyze_image` do, then `RapidOcrClient.ocr(...)`. Returns the **verbatim
  transcription** with an image citation source. On-box read of an owner attachment (no
  `web` permission — like `analyze_image`).
- Distinct from `analyze_image`: this is "read the exact text," no VLM, no interpretation,
  no model swap. jerv steering (`ASSISTANT.md`) says which to reach for.
- Registered in `readtools.build_registry` alongside the image handlers; degrades to a
  clear "OCR service unavailable" when `rapidocr` is off.
- PDFs: images first; a PDF attachment (rasterize→OCR pages) is a small follow-on.
- **Tests**: returns text; missing attachment; service-off message; registry wiring.

### R4 — sandbox `ocr` shell tool (grok / claude)
- `POST /api/jcode/llm/v1/ocr` bridge (shared-token auth) → `RapidOcrClient`; accepts the
  posted image bytes; rasterizes a PDF in the bridge (reuse `pdf_page_images`).
- `ocr <image|pdf>` helper on PATH in the jcode image (Python stdlib, like `web-search`):
  reads the local file, POSTs it, prints the text. **Always-on — no `JCODE_INTERNET_*`
  gate** (it's an offline, local read).
- **Tests**: helper request shape (localhost stub) + no-file usage error; bridge endpoint
  (fake client, auth reject, service-off).

### R5 — on-box sign-off (pending)
Validate against the live sidecar on the box: OCR quality on real captures (dark-mode
terminals, receipts, scanned PDFs) vs. the VLM, the idle load/unload timing under
`RAPIDOCR_IDLE_TTL_SECONDS`, and the cross-check agreement distribution in the logs. Decide
whether to add `rapidocr` to the CI image-build matrix (left out for now — the onnxruntime/
opencv build is heavy and the logic is covered by fakes).

## 5. Docs to reconcile on merge
`docs/reference/ANALYSIS.md` (the OCR pipeline now cross-validates + stores both rows),
`docs/reference/ASSISTANT.md` (the new jerv `ocr` tool + the sandbox tool + when to prefer
it over `analyze_image`), `docs/reference/SERVICES.md` (the new `rapidocr` service), the
`rapidocr` compose comment, and this plan's status block as each wave lands (archive on
`Shipped`, per `docs/DOC_LIFECYCLE.md`).

## 6. Out of scope (named, not silently dropped)
- Persisting the agreement score to a review surface (needs the R2 migration above).
- PDF OCR in the jerv tool (R3 is image-first).
- Non-English language packs (RapidOCR ships multilingual PP-OCR models; wiring a language
  selector is a follow-on).
- Replacing the VLM `vision.caption` (description) path — untouched; only OCR is dual-engine.
- **Zero-when-idle** beyond the lazy-unload: **scale-to-zero** (an on-demand container
  activator) and **in-process OCR in the api** both reclaim the ~150–200 MB baseline that
  lazy-unload leaves resident, but one adds an activator and the other folds opencv/
  onnxruntime into the api (OCR then competing with request handling). Rejected as
  over-engineering for a ~200 MB sidecar — revisit only if box RAM gets tight.
