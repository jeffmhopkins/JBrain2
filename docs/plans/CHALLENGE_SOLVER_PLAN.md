# Challenge Solver — get web_fetch past the bot walls

> **Status:** In progress · **Last verified:** 2026-08-05 · **Waves:** S1✅ S2◻️

Deep-research runs were quietly losing their most authoritative sources. A
Cloudflare-class bot wall (`floridapolitics.com`, a candidate's own site) answers
`web_fetch` with a **challenge interstitial** — "Update Browser Required", "Just a
moment…" — instead of the article. The plain headless `reader` fallback renders that
challenge in a browser and hands it back as a clean 200, so the pipeline ingested the
junk as real content and **cited it** (observed on-box as the "Update Browser Required"
`floridapolitics.com` source). Search itself was healthy; the loss was entirely at the
per-page fetch step.

The box's egress is already a **residential IP** (the Cloudflare Tunnel is inbound
only), so the lever most scrapers pay for is already in hand — the remaining gap is the
browser/TLS fingerprint, which a stealth browser targets directly.

## Design — a third fetch tier, gated behind honest detection

`web_fetch` becomes a three-tier escalation, each tier a no-op miss (`None`) when it
can't help, so escalation is just "try the next one":

1. **direct** — a browser-headers httpx GET (unchanged).
2. **reader** — the stock headless-Chromium renderer for a JS shell / soft bot-wall
   (unchanged), now with challenge detection on its output.
3. **solver** — OPT-IN. A stealth browser (Byparr, Camoufox-backed) behind a
   FlareSolverr-shape `POST /v1` API that clears the JS/managed challenge and returns
   the solved HTML, extracted exactly like a direct fetch.

**Challenge detection (`_is_challenge_page`, `backend/src/jbrain/web/fetch.py`).** The
load-bearing correctness fix, independent of the solver: content-based detection of a
challenge interstitial, tuned hard for precision (canonical title/body strings fire at
any length; weaker markers are gated on a short page and/or a marker combo, so a real
article that merely mentions Cloudflare is never flagged). A detected challenge is a
**blocked fetch** — the direct path raises (status None, so the next tier is still
tried), the reader/solver paths return `None`; it never becomes a `WebSource`, so junk
is never cited. A distinct `web.challenge_blocked` log (with `via=direct|reader|solver`)
makes a block diagnosable instead of hiding in a generic fetch failure.

**Solver sidecar (`byparr`, `deploy/docker-compose.yml`, `solver` profile).** The
heaviest web sidecar (a full stealth browser per solve), so unlike searxng/reader it is
**off by default**: `JBRAIN_SOLVER_URL` empty ⇒ the tier is absent and a walled page is
an honest block, exactly as before. Enable with `SOLVER_URL=http://byparr:8191` + the
`solver` profile. Owner-pinned and never model-supplied, like the reader; only the public
target URL travels in the request body. A solve that is *itself* still challenged stays a
blocked fetch, so the solver never launders junk either.

**Debug fetch route (`POST /api/debug/fetch`, scope `web.fetch`).** The debug console had
no way to exercise the live fetch path — the first investigation of this bug couldn't
fetch a URL through the box. The route runs a URL through the real `WebFetcher` (the same
direct→reader→solver escalation) and returns the extracted page or the recoverable error,
so the detection + solver can be verified against a real walled URL after a deploy (paired
with `logs api` to see which tier served via `web.solver_used` / `web.challenge_blocked`).

## Waves

- **S1 ✅ (this PR)** — challenge detection at the direct + reader seams; the opt-in
  solver tier (`_fetch_via_solver`, the `_recover` escalation helper, config +
  compose + dev-setup wiring); the `POST /api/debug/fetch` route + `web.fetch` scope +
  `debug-connect.sh fetch`. Full unit coverage (`test_web.py`, `test_debug_api.py`),
  web fetch faked via `MockTransport` (no network in tests).
- **S2 ◻️** — live validation on the owner's box after `SOLVER_URL` + the `solver`
  profile are enabled: confirm a known-walled URL (`floridapolitics.com`) now returns
  real content via `web.solver_used`, tune `maxTimeout`/`mem_limit` against real solves,
  and decide whether to cache `cf_clearance` per host to skip repeat solves.

## Security posture and the honest ceiling

The solver reuses the reader's trust model: owner-pinned base URL, SSRF-guarded public
target, on `internal` with no owner data, reached only by jerv (no KB/owner context).
Detection is a **correctness** fix (stop citing junk); the solver is a **retrieval**
upgrade. Neither defeats an interactive Turnstile/CAPTCHA — those sites stay blocked, and
detection keeps them an honest block rather than a fake citation. Open-source solvers lag
Cloudflare's updates, so treat the solver as best-effort and pin the image for a deploy.
