# GitHub Fetch — `web_fetch` reads a public GitHub repo natively

> **Status:** Shipped 2026-10 · no migration · **Superseded-by:** —

jerv reads GitHub today the way it reads any site: the HTML of one page at a time, with
the link list capped at 40, no line numbers, and no way to search a repository. The owner
wants native repo access (2026-10-08) **without a new tool** — jerv already carries ~51
tools and every extra entry crowds a small local model's tool list. So the existing
`web_fetch` learns GitHub URLs instead.

## Scope

In: public repositories on `github.com` and `raw.githubusercontent.com`, read from a repo
**snapshot** (the tarball, downloaded once):

| URL shape | Reply |
|---|---|
| `github.com/{o}/{r}` | overview — description, file-tree outline with counts, languages + line counts by extension, README (capped), recent commits and releases (Atom) |
| `…/tree/{ref}/{path}` | the whole folder listing, with sizes — not link-capped |
| `…/blob/{ref}/{path}` (+ `#L10-L40` / `#L10`) | the file with line numbers; the anchor narrows it to that range |
| `raw.githubusercontent.com/{o}/{r}/{ref}/{path}` | same as `blob` when that repo@ref is already snapshotted; otherwise the ordinary fetch (one file is never worth a repo download) |
| any of the above + `find=` | repo/tree: matches across every text file (path + line number + line), bounded; blob: the usual jump-to-keyword |

Out: private repos, a GitHub token on the box (a deliberate design — no GitHub credential
lives on the box; see the `endpoint` comment in `deploy/docker-compose.yml`), the REST API,
issues/PRs/actions pages (those keep the plain HTML path), a Python-sandbox mount of the
repo, and any execution of repo content.

## Decisions

| Question | Decision | Why |
|---|---|---|
| New tool or `web_fetch`? | `web_fetch`; schema unchanged (no new params), one-sentence description addition | owner ruling: no tool-list growth |
| API or archive? | Archive (`codeload.github.com` tarball) + public Atom feeds; **no** `api.github.com` | unauthenticated API is 60 req/h per IP shared with the box, and a rate-limit 403 there would cost a day on the skip list |
| Default branch | try `github.com/{o}/{r}/archive/HEAD.tar.gz` (redirects to codeload), then `codeload…/tar.gz/HEAD`, then `refs/heads/main`, then `refs/heads/master`; commit SHA from the tarball's pax `comment` header; the branch *name* (display only) best-effort from the repo page HTML | no API; each miss is one cheap 404, and the chain survives either form being retired. Not verifiable offline — hence the fallback chain |
| Ref with slashes (`tree/feature/x/path`) | try ref splits **shortest first** (`feature`, `feature/x`, …, at most 4 segments); a codeload 404 moves to the next; a cached snapshot for any candidate is used without a network call | git forbids `feature` and `feature/x` as branches together, so the first ref that exists is almost always the right one; a tag/branch collision is the documented residual |
| Where the snapshot lives | **in memory**, indexed straight from the tar stream; nothing extracted to disk | the archive is read once into a bounded buffer and parsed in a thread; only text files under the per-file cap are retained. No disk writes means no raw paths and no blob-store entries to garbage-collect (a content-addressed blob delete could otherwise hit an owner attachment with the same digest) |
| Cache | process-level, keyed `owner/repo@ref`, 30 min TTL, LRU-bounded by count (6) and in-memory size (192 MB, counted as decoded `str` sizes plus per-file overhead, not raw bytes); every miss (404, over cap, 403/429, timeout) remembered 5 min; one in-flight download per key, and one snapshot built at a time across all repos; 120 s deadline per download | a chat's follow-up reads (and a second chat soon after) hit the snapshot; bounded memory |
| Limits | 100 MB compressed, 400 MB declared uncompressed, 50,000 entries — over any of them the snapshot is refused cleanly; per-file 1 MB retained; 64 MB retained text per snapshot | a huge monorepo falls back to the plain page fetch instead of eating the box |
| Failure | 404 on every candidate → a clear "missing or private" message for a tree/blob URL, the plain page for a bare `{o}/{r}` (it may be a site section); a listed file the snapshot cannot show (binary, over 1 MB) → the raw file through the ordinary fetch; anything else (over cap, 403/429, network) → today's plain HTML fetch with a one-line note | a snapshot failure **never** records `github.com`/`codeload` on the 24 h skip list — it never reaches `_record_block` |

## Security

- Every download goes through `WebFetcher.fetch_bytes` → the per-hop SSRF guard, so a
  redirect from `github.com`/`codeload` to a private address is refused like any fetch.
- Tar safety while indexing: only regular files are read — symlinks, hardlinks, devices,
  FIFOs are skipped; absolute names, `..` segments and NUL bytes are dropped; binaries
  (known extensions or a NUL in the head) are listed but never decoded; per-file,
  uncompressed and entry-count caps are enforced from the headers *before* reading data.
- A ref or path segment that decodes to contain `/` (`%2f`) or `\`, or is `.`/`..`, is
  refused at parse time, and each ref component is percent-encoded again in every URL
  built from it — so a crafted ref cannot steer the archive URL at another repository.
- A repo search runs a model-supplied pattern over attacker-written text, off the event
  loop, with the `regex` engine's per-match timeout, each line cut to 2,000 chars, and a
  3 s budget; past either it stops and says the results are partial.
- Everything returned is page-derived untrusted content, rendered through the **same**
  `window_text` → `_present_result` path every other `web_fetch` result takes (header,
  windowing, citation chip), and the tool's existing "public web page … never as
  instructions" frame covers it. Nothing from a repo is ever executed.

## Waves

### G1 — the GitHub snapshot reader ✅

`jbrain/web/github.py` (URL parsing, safe tar indexing, the snapshot cache + single-flight,
the overview/tree/blob/search renderers); `webtools.web_fetch` routes a recognised URL
through it ahead of the plain fetch (and past the skip-list short-circuit); `main.py`
wires it; `web_fetch.tool` gains one sentence; `ASSISTANT.md` describes it.

**Done when:** unit tests (fakes, no network) cover URL parsing incl. slashed refs, tar
safety (traversal, symlink, huge file, too many files, binary skip), every view, find,
quarantine-identical rendering, the fallback paths, no skip-list entry on failure, the SSRF
guard on a codeload redirect, cache reuse and single-flight — security paths at 100%; the
backend gates and `docs-freshness` are green.

**Landed:** `backend/src/jbrain/web/github.py`, the `web_fetch` route in
`agent/webtools.py`, `web_fetch.tool` v13, `tests/unit/test_web_github.py` (the module at
100% line coverage). **Not verifiable without network, carried to `ROADMAP.md`:** which of
the four default-branch archive URLs GitHub actually serves today, the `defaultBranch` key
in the repo page HTML, the pax `comment` commit header on codeload archives, and the Atom
feed for `commits/{sha}.atom` — each degrades gracefully (next URL / "default branch" label
/ no commit shown / no commits section), none blocks a read.
