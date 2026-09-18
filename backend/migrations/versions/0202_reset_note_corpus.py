"""Reset the note corpus, the graph it derives, the wiki, and the fact projections.

R5 of docs/plans/AGENT_INGEST_REWRITE.md. The rewrite replaced the note producer
outright: every fact on the box was written by a chain that no longer exists (R4), under
identity rules the conversation does not share. Migrating those rows forward was never
the plan — the owner asked for a clean start, so the first note the new system reads is
genuinely the first note it has ever seen.

**Authorised by the owner, twice, in his own words** — "the notes, and the wiki, and the
database for facts. Not the tasks, task history, conversation history", then, asked
specifically about the calendar rows that derive from notes, "You are good to completely
reset the appointments also". It rides `Ops -> Update`, which runs
`docker compose run --rm migrate` after quiescing api and worker
(`deploy/update-inner.sh`), so nothing writes into these tables while it runs and the
owner needs no shell (CLAUDE.md #10).

**DELETE, not `TRUNCATE ... CASCADE`.** The plan recommended TRUNCATE; it is wrong here,
and measuring the live box is what showed it. `TRUNCATE ... CASCADE` is STRUCTURAL: it
truncates every table holding a foreign key into the named set, whatever those keys say
should happen and whatever rows they hold. Three KEPT tables reference this set —
`list_items.source_note_id` (ON DELETE **SET NULL**), `agent_episode_refs` and
`place_share` (ON DELETE CASCADE). A TRUNCATE would therefore have destroyed the owner's
shopping list, which the plan explicitly keeps and whose FK exists precisely so a list
outlives the note that spawned it. DELETE honours the declared actions instead: the list
item survives with its provenance blanked, and a memory ref or a share of something that
no longer exists goes, which is what CASCADE on those columns is for.

DELETE is also cheap at this size — the corpus measured tens of notes, hundreds of facts —
so the only thing TRUNCATE bought was a foot-gun. No sequences are reset because these
tables key on `gen_random_uuid()`, not on serials.

Order is children-before-parents throughout, so no statement leans on a cascade to be
correct. That is not a claim to take on trust — the first draft got it wrong
(`temporal_tokens` before `facts`) and would have aborted every `Ops -> Update` on this
release, because CI runs migrations against an EMPTY schema where a wrong order cannot
fail and an over-broad delete deletes nothing. So `test_reset_corpus_pg.py` seeds real
referential data, and derives the blocking-constraint graph from the schema to check this
list is a valid order — rather than re-asserting a reading of it.

`downgrade()` refuses. Deleting data is not reversible, and a downgrade that pretended
otherwise would be a lie in the one place someone reads under pressure. The recoverable
copy is the backup taken before pressing Update: Data -> Backup -> "Back up
everything" in the PWA, or `debug-connect.sh backup` with a token.

Revision ID: 0202
Revises: 0201
Create Date: 2026-09-14
"""

from alembic import op

revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None


#: Children before parents. Grouped as the owner reasons about them, not as the schema
#: happens to be laid out, so this list can be read against the plan's §6 table.
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
    # Fact projections. Lab results, encounters and geofences measured empty on the box;
    # appointments did not, and the owner was asked about those specifically.
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
    # token is the parent here even though a token reads like a detail of the fact. The
    # first draft had them the other way round and aborted the whole migration on any
    # dated fact — which is most of them. `test_the_wipe_order_satisfies_every_blocking
    # _constraint` now derives this from the live schema rather than trusting the reading.
    "facts",
    "temporal_tokens",
    "entities",
    # The predicate registry: tier-1 vocabulary learned from a corpus that is going.
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

    # Queued or running work for producers R4 deleted. `app.jobs` is KEPT as history —
    # a `done` row records something that happened — but an undispatched one names a
    # handler that no longer exists, and the worker would fail it on every poll forever
    # (`worker.py`, unknown kind). Measured zero on the box; a correct rule regardless,
    # since a job could be enqueued between the Export and the deploy.
    op.execute(
        "DELETE FROM app.jobs WHERE kind IN ('integrate_note', 'note_extract')"
        " AND status IN ('queued', 'running')"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0202 deleted the note corpus, the graph, the wiki and the fact projections."
        " There is no downgrade: the rows are gone. Restore from the backup taken before"
        " the update that ran this (PWA: Data -> Backup -> Restore)."
    )
