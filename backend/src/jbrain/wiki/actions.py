"""The four wiki workflow actions (docs/PHASE6_WIKI_PLAN.md §3b), as in-code ActionSpecs.

Like `EVAL_RUN_SPEC` / `PURGE_ACTION` / the reconcilers, these live in the in-code registry
only — NOT in the `app.actions` seed (the seed-lockstep test pins the shipped six). They are
composed into the worker/api registry at boot, and a migration seeds their pipelines/schedules/
manual triggers (referencing them by name). The handlers run the `WikiBuilder` (Wave C2a:
sourcing + write + index, with the deterministic `StubRewriter`; the LLM rewrite + grounding +
the token budget swap in at Wave C2b).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.embed import EmbedClient
from jbrain.wiki.builder import Rewriter, StubRewriter, WikiBuilder
from jbrain.workflow.registry import ActionSpec

Handler = Callable[[dict[str, Any]], Awaitable[None]]

WIKI_REFRESH_SPEC = ActionSpec(
    name="wiki_refresh",
    version=1,
    handler="wiki_refresh",
    domain_optional=True,
    mutating=True,
    cost_class="expensive",
    dedup_key_expr=None,
    description="Rebuild dirty entities' articles (dirty-bit driven).",
)
WIKI_REBUILD_SPEC = ActionSpec(
    name="wiki_rebuild",
    version=1,
    handler="wiki_rebuild",
    domain_optional=True,
    mutating=True,
    cost_class="expensive",
    dedup_key_expr=None,
    description="Full re-derive of one article (or all).",
)
WIKI_REINDEX_SPEC = ActionSpec(
    name="wiki_reindex",
    version=1,
    handler="wiki_reindex",
    domain_optional=True,
    mutating=True,
    cost_class="standard",
    dedup_key_expr=None,
    description="Re-embed wiki section summaries.",
)
WIKI_PRUNE_SPEC = ActionSpec(
    name="wiki_prune",
    version=1,
    handler="wiki_prune",
    domain_optional=True,
    mutating=True,
    cost_class="cheap",
    dedup_key_expr=None,
    description="Archive orphaned wiki articles.",
)

WIKI_SPECS = (WIKI_REFRESH_SPEC, WIKI_REBUILD_SPEC, WIKI_REINDEX_SPEC, WIKI_PRUNE_SPEC)


def wiki_handlers(
    maker: async_sessionmaker[AsyncSession],
    *,
    embed: EmbedClient,
    embedding_model: str,
    rewriter: Rewriter | None = None,
) -> dict[str, Handler]:
    """The {handler_key: callable} dispatch entries for the four wiki actions. `rewriter`
    defaults to the deterministic stub (C2a); C2b injects the LLM rewriter here."""
    builder = WikiBuilder(
        maker, embed=embed, rewriter=rewriter or StubRewriter(), embedding_model=embedding_model
    )

    async def refresh(_payload: dict[str, Any]) -> None:
        await builder.refresh()

    async def rebuild(payload: dict[str, Any]) -> None:
        await builder.rebuild(str(payload.get("target", "all")))

    async def reindex(_payload: dict[str, Any]) -> None:
        await builder.reindex()

    async def prune(_payload: dict[str, Any]) -> None:
        await builder.prune()

    return {
        "wiki_refresh": refresh,
        "wiki_rebuild": rebuild,
        "wiki_reindex": reindex,
        "wiki_prune": prune,
    }
