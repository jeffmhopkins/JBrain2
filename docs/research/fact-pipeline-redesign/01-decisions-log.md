# Decisions log — binding constraints added during the design effort

Decisions the user has made mid-process. These are BINDING on all subsequent
synthesis, revision, and red-team work (they override anything in 00-framing.md
or a spec revision that contradicts them).

## D1 — A complete DB reset is acceptable (no in-place legacy migration)

The user does not need the existing fact corpus migrated in place. The cutover to
the new model is a **clean rebuild**: drop the derived graph (facts / entities /
review / op-log) and **re-ingest all retained notes from scratch** under the new
contract. Notes remain the sources of truth and are kept; only the derived layer
is rebuilt.

**Implications:**
- The one-time "existing-corpus migration mapping" (Round-1 migration finding M7:
  recovering cardinality intent, `value_identity`, and the `valid_to=NULL` bound
  ambiguity from today's rows) is **OUT OF SCOPE** — there is nothing to migrate;
  the new contract produces these fields natively on re-ingest.
- This **removes** a class of risk (lossy legacy mapping) and lets the storage /
  contract design be fully greenfield with no back-compat to today's `facts` table.
- **Still in scope:** FUTURE contract-version re-analysis migration AFTER launch —
  i.e. when a later contract bump re-analyzes notes, human edits (the op overlay)
  and pinned/human-touched facts must survive (Round-1 migration M2/M3). The
  human-op overlay and pinned-protection design remain required; only the *initial*
  legacy cutover is a clean wipe.
- Disposition: reclassify M7 from "must fix" to "out of scope (clean rebuild, D1)";
  keep M2/M3 in scope.
