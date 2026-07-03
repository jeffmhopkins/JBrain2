"""The wiki-editorial WRITE tools (docs/plans/PHASE6_WIKI_PLAN.md §4): the sanctioned levers the agent
operates on the owner's behalf in Talk. The wiki stays machine-written — these never edit prose;
they file the inputs the builder re-derives from.

- file_correction: mint an owner-authored correction note (provenance=owner_correction) and drive
  ingestion — its surface-attested facts force-supersede + pin the conflicting head (Wave A+), and
  the rebuilt article reflects the correction. The owner "out-argues the wiki."
- add_source_exclusion: suppress a note as a source for an article (or globally) and queue a
  rebuild so the article is re-derived without it. Not deletion, not retraction — just un-sourced.
- request_rebuild: queue a full re-derive of one article on demand.

These run only inside the owner's agent session (no capability token reaches the agent pre-P7),
which is the gate on these privileged writes. Each returns a deferred-job chip, not prose.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import UnknownDomain
from jbrain.queue import JobEnqueuer


def build_wiki_write_handlers(
    notes: SqlNotesRepo, jobs: JobEnqueuer, maker: async_sessionmaker[AsyncSession]
) -> dict[str, ToolHandler]:
    async def file_correction_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        body = str(arguments.get("body", "")).strip()
        domain = str(arguments.get("domain", "")).strip()
        if not body or not domain:
            return ToolOutput("file_correction needs the correction text and its domain.")
        article_id = str(arguments.get("article_id", "")).strip()
        rev = arguments.get("revision_id")
        try:
            note, created = await notes.create_note(
                ctx.session,
                client_id=f"correction-{uuid.uuid4().hex}",
                domain=domain,
                destination=None,
                body=body,
                provenance="owner_correction",
                source_ref=f"wiki:{article_id}" if article_id else None,
                wiki_revision_id=uuid.UUID(str(rev)) if rev else None,
            )
        except UnknownDomain:
            return ToolOutput(f"'{domain}' is not a known domain.")
        except ValueError:
            return ToolOutput("revision_id must be a valid id.")
        if created:
            await jobs.enqueue(ctx.session, "ingest_note", {"note_id": note.id})
        return ToolOutput(
            "Filed your correction as a note. It will out-argue the conflicting fact and the "
            "article will be rebuilt from the corrected graph — the wiki stays machine-written."
        )

    async def request_rebuild_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        article_id = str(arguments.get("article_id", "")).strip()
        if not article_id:
            return ToolOutput("request_rebuild needs an article_id.")
        await jobs.enqueue(ctx.session, "wiki_rebuild", {"target": article_id})
        return ToolOutput("Queued a full rebuild of that article.")

    async def add_source_exclusion_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        note_id = str(arguments.get("note_id", "")).strip()
        domain = str(arguments.get("domain", "")).strip()
        if not note_id or not domain:
            return ToolOutput("add_source_exclusion needs a note_id and its domain.")
        article_id = str(arguments.get("article_id", "")).strip() or None
        reason = str(arguments.get("reason", "")).strip() or None
        try:
            note_uuid = str(uuid.UUID(note_id))
            article_uuid = str(uuid.UUID(article_id)) if article_id else None
        except ValueError:
            return ToolOutput("note_id/article_id must be valid ids.")
        # Owner + domain-scoped write (RLS); article_id NULL = a global exclusion.
        async with scoped_session(maker, ctx.session) as session:
            await session.execute(
                text(
                    "INSERT INTO app.wiki_source_exclusions (note_id, article_id, reason,"
                    " domain_code) VALUES (:n, :a, :r, :d)"
                ),
                {"n": note_uuid, "a": article_uuid, "r": reason, "d": domain},
            )
            await session.commit()
        # Re-derive the affected article (or every article if the exclusion is global).
        await jobs.enqueue(ctx.session, "wiki_rebuild", {"target": article_id or "all"})
        scope = "this article" if article_id else "every article"
        return ToolOutput(f"Excluded that note as a source for {scope}; queued a rebuild.")

    return {
        "file_correction": file_correction_tool,
        "request_rebuild": request_rebuild_tool,
        "add_source_exclusion": add_source_exclusion_tool,
    }
