"""What jmolt has already claimed, across nights — the gate's memory.

Backs the claim gate (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2). It exists because of a
measurement, not a guess: on the live box, six posts across 2026-08-28 and 2026-08-29 asserted
one claim, and the sequence crossed the night boundary. A gate holding only tonight's claims
would let the first restatement of every night through, every night.

Embeddings are stored, not recomputed. A night that re-embedded its own history would pay for
the same vectors nightly and — worse — would be at the mercy of the embedder still being up:
the failure mode of "cannot embed history" must not be "the gate lets everything through".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.agent.jmolt_claim import Claim

# How many prior claims a night loads. The whole point is crossing night boundaries, so this
# reaches back further than one night — but not without bound: the gate compares the candidate
# against every one of them, and an unbounded history makes the guard the cost.
DEFAULT_RECALL = 60


@dataclass(frozen=True)
class StoredClaim:
    claim: Claim
    embedding: list[float]
    object_embedding: list[float]
    published: bool
    at: datetime


def _vec(raw: Any) -> list[float]:
    """pgvector comes back as a string like '[0.1,0.2]' on a plain text query."""
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        return [float(x) for x in raw.strip().strip("[]").split(",") if x.strip()]
    return []


class ClaimStore:
    async def record(
        self,
        session: AsyncSession,
        principal_id: str,
        claim: Claim,
        *,
        embedding: list[float],
        object_embedding: list[float],
        published: bool,
        outbox_id: str | None = None,
    ) -> str:
        """Write one claim as said (or as judged and refused).

        A refused claim is kept deliberately. A refusal that keeps recurring is the strongest
        available signal that a threshold is wrong, and it is invisible if only the writes that
        got through are stored."""
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.jmolt_claim"
                    " (principal_id, subject, predicate, object, citations,"
                    "  embedding, object_embedding, outbox_id, published)"
                    " VALUES (:pid, :s, :p, :o, :cites, :emb, :oemb,"
                    "         cast(:oid AS uuid), :pub)"
                    " RETURNING id"
                ),
                {
                    "pid": principal_id,
                    "s": claim.subject,
                    "p": claim.predicate,
                    "o": claim.object,
                    "cites": sorted(claim.citations),
                    "emb": str(list(embedding)),
                    "oemb": str(list(object_embedding)),
                    "oid": outbox_id,
                    "pub": published,
                },
            )
        ).scalar_one()
        return str(row)

    async def recent(
        self, session: AsyncSession, principal_id: str, *, limit: int = DEFAULT_RECALL
    ) -> list[StoredClaim]:
        """The claims a night loads its gate with — PUBLISHED ones only.

        A refused claim was never said, so repeating it is not a repetition; holding a draft
        against a night that never sent it would make the gate stricter every time it
        refused, which is the runaway the one-retry cap exists to prevent."""
        rows = (
            await session.execute(
                text(
                    "SELECT subject, predicate, object, citations,"
                    "       embedding::text AS emb, object_embedding::text AS oemb,"
                    "       published, at"
                    " FROM app.jmolt_claim"
                    " WHERE principal_id = :pid AND published"
                    " ORDER BY at DESC, seq DESC LIMIT :lim"
                ),
                {"pid": principal_id, "lim": limit},
            )
        ).all()
        return [
            StoredClaim(
                claim=Claim(
                    subject=r.subject,
                    predicate=r.predicate,
                    object=r.object,
                    citations=frozenset(r.citations or ()),
                ),
                embedding=_vec(r.emb),
                object_embedding=_vec(r.oemb),
                published=r.published,
                at=r.at,
            )
            for r in rows
        ]

    async def refusal_counts(
        self, session: AsyncSession, principal_id: str, *, since: datetime
    ) -> int:
        """How many candidates the gate refused since `since`. For the observer and the
        scorer — never shown to jmolt, which would make the gate a score to beat."""
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM app.jmolt_claim"
                        " WHERE principal_id = :pid AND NOT published AND at >= :since"
                    ),
                    {"pid": principal_id, "since": since},
                )
            ).scalar_one()
        )
