# JBrain2 — Report share links

> **Status:** Shipped 2026-07 · migration 0150 · **Superseded-by:** —

Public, revocable, no-login share links for the owner's research reports. The owner mints a
link that targets **one report** or **one folder** (`report_groups`); anyone with the link
reads it at `/share/<token>`; the owner revokes it to kill it. No expiry — revoke is the only
control. Built as a single vertical slice (one branch), GUI gate honoured (variant B chosen).

## Owner decisions (GUI gate + clarifications)

- **Target** — report *or* folder, chosen at mint. Folder membership is **dynamic** (a report
  later filed into a shared folder becomes visible; the folder *name* is snapshotted to the
  link label so `report_groups` stays owner-private).
- **Lifetime** — no expiry; revoke only.
- **Link style** — plain token-in-URL (`/share/<token>`), no cookie/redeem. The owner accepted
  the token appearing in logs/history; the Referer and search-index vectors are closed in code.
- **GUI** — variant **B** (share sheet + branded public page): `docs/mocks/research-share/`.
- **Library reports** — warn + confirm at mint (not a hard block); `source_mode` surfaced on
  the report listing so the Share sheet warns before publishing a notes-derived report, and the
  public payload strips non-URL (note-derived) citations.

## Security — the capability is enforced in RLS, not app code

The read runs under `research_share_context` (`db/session.py`): a **non-owner, empty-scope**
principal carrying `auth_context='research_share'` and, once the token resolves, `principal_id
= <link id>` as the pin. Empty domain scope is load-bearing — an `external` scope would make the
permissive `research_reports_domain` policy match every report (and the video corpus), turning
the grant into a no-op. The only grant a visitor gets is `research_reports_share` (migration
0150): a row-scoped `SELECT` policy that joins the pinned, non-revoked link to the report id (or
its folder's `group_id`). The link id is compared **as text** (never casting the GUC to uuid),
because a `SELECT` policy is also evaluated on `INSERT … RETURNING` rows and Postgres
constant-folds a `::uuid` cast — which would throw on the `worker` system context. Revocation
is re-checked in the policy, so a revoked link fails closed at the data layer. The view-count
bump runs under the owner `SYSTEM_CTX`; the visitor has no write policy anywhere. Public
responses set `Referrer-Policy: no-referrer` + `X-Robots-Tag: noindex` and are rate-limited.

## What shipped

- **Migration 0150** — `research_share_links` (token hash, target, FKs with `ON DELETE
  CASCADE`, exactly-one-target CHECK) + owner/public-read policies + the `research_reports_share`
  row-scoped policy.
- **Backend** — `db/session.py: research_share_context`; `external/research_shares.py`
  (mint/list/revoke/resolve/read + source sanitisation); scoped corpus readers
  (`research_corpus.fetch_report_scoped` / `list_reports_scoped`); owner routes on the
  research-library router; the public, rate-limited `api/research_share.py`.
- **Frontend** — `research/share.ts` (parse + URL); shell-less `ResearchShareApp` mounted in
  `main.tsx` before the login gate; exported `ReportDetailBody` reused verbatim; a `ShareSheet`
  on the report ⋯ and folder headers (`ResearchScreen`); client methods + types.
- **Tests** — `test_research_share_rls.py` (RLS isolation: pinned reads only its target;
  external corpus / owner tables / other links invisible; dynamic membership; revoke; write- and
  no-pin denial) and `test_research_share_api.py` (public read, folder index, revoke → 404,
  headers, sanitisation, owner-gating); `research/share.test.ts`.

## Follow-ups carried forward

- The label snapshot goes stale on a folder rename (cosmetic; documented in the mock README).
- The public read reuses the OwnTracks `TokenBucket` (per-IP, in-memory, unbounded map). Fine at
  personal scale; if abuse on the internet-facing route becomes a concern, bound the map and key
  off a trusted forwarded-for. (Independent-review note; not a confidentiality issue.)

## Independent review (post-build)

Three independent reviewers (security red-team, backend, frontend) reviewed the diff. No
exploit found — the RLS design holds, including the NULL-`group_id` edge (a report link never
exposes ungrouped reports), verified and tested. Fixes applied from the review: folder links now
compute `library_warning` at mint and in the list (a folder containing a notes-derived report is
flagged, not just report links); the report mint requires a second tap to publish a
notes-derived report (warn **+ confirm**); `_public_sources` projects citations to `{url,title}`
explicitly (allowlist, not passthrough); a non-uuid `report_id` is rejected at mint; a failed
member fetch keeps the folder index; added tests for the report-link `/reports/{id}` path, the
429 rate-limit, and the folder library-warning; added a `ResearchShareApp` component test.
