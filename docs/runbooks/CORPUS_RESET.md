# Resetting the note corpus

> **Status:** Living · **Last verified:** 2026-09-16

Wiping the notes, the graph they derive, the wiki and the fact projections, while
keeping everything that is not derived from a note. The owner has asked for this
twice while the ingestion producer was being rewritten, and will ask again: a
producer that was wrong writes rows that cannot be corrected in place, because
what was wrong was the producer, not any one row.

This is the destructive procedure in this repo. It needs no shell (CLAUDE.md #10)
and no direct database access — but it does need a merged PR, because the wipe
rides the migration one-shot.

## What "the corpus" means

The owner's scope, in his words both times: **the notes, the wiki, and the
entities/facts** — *not* the tasks, *not* his chat, *not* history that lives
outside notes. Appointments ride along, under "You are good to completely reset
the appointments also": they are a projection of facts, and nothing else would
take a calendar row whose note is gone.

The authoritative list is `_WIPE` in each reset migration
(`backend/migrations/versions/*_reset_note_corpus*.py`). Three tables that stay
reach INTO that set, and they are why the migration uses `DELETE` and not
`TRUNCATE ... CASCADE` — TRUNCATE is structural and would empty them regardless
of what their keys declare:

| Kept table | Key | On delete |
|---|---|---|
| `list_items.source_note_id` | the shopping list | **SET NULL** — a list outlives the note that spawned it |
| `agent_episode_refs` | a memory's citation | CASCADE |
| `place_share` | a share of a place entity | CASCADE |

His CHAT stays; a note's **ingestion thread** does not. That split is the one
statement that must run *first*, while `note_conversations` still exists to name
the `agent_sessions` rows behind those threads.

## The procedure

1. **Back up, on the deployed build, and wait for it to finish.** PWA:
   **Data → Backup → "Back up everything"**. With a token:
   `scripts/debug-connect.sh backup`, then poll `backup-status` until it reports
   a finished export. This is the only way back — the migration's `downgrade()`
   refuses rather than pretending otherwise.
2. **Write the migration**, copying the previous reset's `_WIPE` verbatim and
   bumping the revision. Re-derive the scope rather than assuming it: ask the
   live schema which tables outside the set hold a foreign key into it (see
   `test_no_kept_table_can_block_the_wipe` for the query). A new feature that FKs
   `notes` and forgets `ondelete` would abort the whole update, on the box only.
3. **Add it to `_RESETS`** in `backend/tests/integration/test_reset_corpus_pg.py`.
   Every test there runs against every reset. This is not ceremony: CI runs
   migrations against an EMPTY schema, where a wrong statement order never trips
   a foreign key and an over-broad delete deletes nothing. A reset is only ever
   exercised with rows in the tables on the owner's box, once, irreversibly.
4. **Merge.** `deploy/update-inner.sh` resets to `origin/main`, so merging is
   what makes the wipe deployable.
5. **Run it:** PWA **Ops → Update**, or `scripts/debug-connect.sh update`. The
   updater quiesces api and worker before `migrate`, so nothing writes into these
   tables while the delete runs.
6. **Verify**, with `scripts/debug-connect.sh sql`: the wiped tables read zero,
   and the keepers (his lists, his chat sessions, his tasks) do not.

## What comes back on its own

`canonical_predicates` is emptied and re-seeded: the shipped tier-1 registry is
written from `embed.py` at worker startup, which the same update performs. What
the reset actually clears there is anything the old corpus taught the registry,
which is the point. A reset that measured every row as `origin = 'seed'`
beforehand has lost nothing it cannot rebuild.
