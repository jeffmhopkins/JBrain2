"""Reset the note corpus, the graph it derives, the wiki, and the fact projections — again.

A REPEAT of 0202, at the owner's word, on the same scope. 0202 cleared the box so the
rewritten producer could start from nothing; the two days since have been spent fixing
what that producer got wrong (the tautologies of #1404, the pre-seeded handles of #1407,
the predicate drift of #1408, the persona leak of #1409). The rows written before those
fixes are the evidence he keeps being shown — `Boss --hasName--> Boss`, Susan's six
competing coat predicates — and they cannot be corrected in place, because the thing that
was wrong was the producer, not any single row.

**Authorised by the owner** — "Again, I'm looking only for the [notes] and the wiki and
the entities. Not the tasks. Not my conversation. History outside of notes", which is the
same scope he set for 0202 in almost the same words ("the notes, and the wiki, and the
database for facts. Not the tasks, task history, conversation history"). Appointments ride
along as they did then, under "You are good to completely reset the appointments also":
they are a projection of facts, and nothing else would take a calendar row whose note is
gone. They measured ZERO on the box before this shipped, so that clause costs him nothing
this time.

**The scope was re-derived, not assumed.** 0202's `_WIPE` is reproduced verbatim below,
but only after asking the LIVE schema which tables outside the set still hold a foreign
key into it. The answer is the same three 0202 documented — `list_items.source_note_id`
(ON DELETE SET NULL, so the shopping list outlives the note that spawned it),
`agent_episode_refs` and `place_share` (both CASCADE) — and no table added since
references the corpus at all. `media_analysis_results` and `external_source_chunks` look
like they might and do not: the first keys on an agent SESSION (his chat, which stays),
the second on researched web sources, which are not notes.

Same reasons as 0202 for everything else: DELETE rather than `TRUNCATE ... CASCADE`,
because TRUNCATE is structural and would empty those three keeper tables regardless of
what their keys declare; children before parents throughout, with `facts` before
`temporal_tokens` because `facts.temporal_token_id` is NO ACTION; no sequences reset,
because these tables key on `gen_random_uuid()`.

`canonical_predicates` goes again, and comes back on its own: every one of the 121 rows on
the box reads `origin = 'seed'`, and the worker re-seeds the shipped registry (`embed.py`)
at startup — which the update that runs this migration performs anyway. What it does clear
is anything the old corpus taught the registry, which is the point.

It rides `Ops -> Update` like 0202, which quiesces api and worker before running
`migrate`, so nothing writes into these tables while it runs and the owner needs no shell
(CLAUDE.md #10).

`downgrade()` refuses, for 0202's reason: deleting data is not reversible, and a downgrade
that pretended otherwise would be a lie in the one place someone reads under pressure. The
recoverable copy is the backup taken before pressing Update — Data -> Backup -> "Back up
everything" in the PWA, or `debug-connect.sh backup` with a token.

Revision ID: 0205
Revises: 0204
Create Date: 2026-09-16
"""

from alembic import op

revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None


#: Children before parents. Grouped as the owner reasons about them, not as the schema
#: happens to be laid out. Identical to 0202's list, re-checked against the live schema.
_WIPE = (
    # The wiki: a projection of facts, which are a projection of notes.
    # `wiki_citations.chunk_id` is NOT NULL against a chunk table being emptied, so the
    # wiki could not survive this even if it were not in scope by the owner's word.
    "wiki_talk_posts",
    "wiki_talk_topics",
    "wiki_citations",
    "wiki_links",
    "wiki_sections",
    "wiki_revisions",
    "wiki_index",
    "wiki_articles",
    "wiki_source_exclusions",
    # Fact projections. All of them measured empty this time, appointments included.
    "encounter_diagnoses",
    "encounter_providers",
    "encounters",
    "lab_results",
    "appointment_locations",
    "appointments",
    "geofence_state",
    "place_geofence",
    # The graph.
    "entity_mentions",
    "entity_aliases",
    "entity_distinctions",
    # `facts` BEFORE `temporal_tokens`: `facts.temporal_token_id` is NO ACTION, so the
    # token is the parent here even though a token reads like a detail of the fact.
    # `test_the_wipe_order_satisfies_every_blocking_constraint` derives this from the
    # live schema for BOTH migrations rather than trusting the reading.
    "facts",
    "temporal_tokens",
    "entities",
    # The predicate registry: tier-1 vocabulary, re-seeded by the worker on the restart
    # this same update performs.
    "predicate_aliases",
    "canonical_predicates",
    # Cards and rebuild history, both keyed on rows above.
    "review_items",
    "graph_rebuild_runs",
    # The note conversation and its transcript. The `agent_sessions` row behind each is
    # taken separately below, because finding it requires this table to still exist.
    "note_conversation_tool_calls",
    "note_conversations",
    # The corpus itself.
    "note_analysis",
    "chunks",
    "attachment_extracts",
    "attachments",
    "note_clarifications",
    "notes",
)


def upgrade() -> None:
    # FIRST, while `note_conversations` still exists to name them: the `agent_sessions`
    # rows that ARE note threads. `note_conversations.session_id` FKs `agent_sessions.id`
    # ON DELETE CASCADE, so deleting the session takes the thread with it. This is the
    # one place "conversation history" needs splitting: the owner keeps his CHAT, and a
    # note's ingestion thread is not chat — it belongs to the note being deleted.
    op.execute(
        "DELETE FROM app.agent_sessions WHERE id IN (SELECT session_id FROM app.note_conversations)"
    )

    for table in _WIPE:
        op.execute(f"DELETE FROM app.{table}")

    # Queued or running work whose subject is about to stop existing. `app.jobs` is KEPT
    # as history — a `done` row records something that happened — but an undispatched
    # `integrate_note` names a note that will be gone, and the worker would fail it on
    # every poll. Measured zero on the box; a correct rule regardless, since a job could
    # be enqueued between the backup and the deploy.
    op.execute(
        "DELETE FROM app.jobs WHERE kind IN ('integrate_note', 'note_extract')"
        " AND status IN ('queued', 'running')"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0205 deleted the note corpus, the graph, the wiki and the fact projections."
        " There is no downgrade: the rows are gone. Restore from the backup taken before"
        " the update that ran this (PWA: Data -> Backup -> Restore)."
    )
