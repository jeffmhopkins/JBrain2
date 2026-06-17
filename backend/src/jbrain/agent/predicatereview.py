"""The `predicate_review` engine action (Loop 3a, Wave 2; docs/LOOP3_PREDICATE_CANON_PLAN.md).

The agent becomes a *proposing* reviewer of the `new_predicate` cards the canonicalization pass
files: nightly, it batches open cards and stages an owner `predicate-canon` Proposal whose leaves
each carry a card's suggested resolution. Nothing is applied until the owner approves — the leaf
then runs the SHIPPED `resolve_review` (map_to_existing / accept_as_new), reusing all its committed
logic (fact rewrite, mint, the Wave-1 durable alias, the consolidate event). Owner review is the
trust gate; there is no auto-resolution (the MVP posture, mirroring Loop 2).

Embedding-only: the suggested resolution comes from the card's existing embedding-neighbor
suggestions (computed when the card was filed), so the action spends no tokens — the LLM
name-shortlist (§3.1a/§7) is deferred. Fail-closed behind the self-improvement kill-switch.
Idempotent: a card already carried by a live `predicate-canon` proposal is skipped, so a re-run
never double-proposes. One proposal per domain keeps the firewall (a card carries its note's
domain; predicates are global but the proposal is domain-scoped like every other).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.analysis.predicates import _PRED_WEAK
from jbrain.db.session import SessionContext, scoped_session
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

log = structlog.get_logger()

# How many open cards to propose per nightly sweep (owner-review load + proposal size bound).
_BATCH = 20
# A card whose top embedding-neighbor is at least this similar is proposed as a map onto it;
# below this, the agent proposes minting the raw predicate as new. The WEAK band — the cards CARRY
# these suggestions precisely because they are right for the clean drifts (docs §5a).
_MAP_THRESHOLD = _PRED_WEAK

PREDICATE_REVIEW_SPEC = ActionSpec(
    name="predicate_review",
    version=1,
    handler="predicate_review",
    domain_optional=True,
    mutating=True,  # stages proposals
    cost_class="cheap",  # embedding-only (suggestions already on the card); no LLM call
    dedup_key_expr=None,
    description="Propose owner-reviewed resolutions for open new_predicate cards.",
)


@dataclass(frozen=True)
class _Card:
    card_id: str
    domain: str
    predicate: str
    action: str  # "map_to_existing" | "accept_as_new"
    canonical: str | None  # the map target (None for accept_as_new)


def _proposed_resolution(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Pick the suggested resolution from a card's embedding neighbors: map onto the top neighbor
    when it clears the band, else mint the raw predicate as new. The owner approves either way."""
    suggestions = payload.get("suggestions") or []
    if suggestions and isinstance(suggestions[0], (list, tuple)) and len(suggestions[0]) >= 2:
        name, sim = suggestions[0][0], suggestions[0][1]
        if isinstance(name, str) and isinstance(sim, (int, float)) and sim >= _MAP_THRESHOLD:
            return "map_to_existing", name
    return "accept_as_new", None


async def _open_cards(session: AsyncSession, *, limit: int) -> list[_Card]:
    """Open `new_predicate` cards not already carried by a live `predicate-canon` proposal
    (idempotency: a re-run never double-proposes the same card)."""
    rows = (
        await session.execute(
            text(
                "SELECT id::text AS card_id, domain_code, payload FROM app.review_items r"
                " WHERE r.kind = 'new_predicate' AND r.status = 'open'"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM app.proposal_nodes n JOIN app.proposals p"
                "       ON p.id = n.proposal_id"
                "     WHERE p.kind = 'predicate-canon' AND p.status IN ('staged', 'approved')"
                "       AND n.preview->>'card_id' = r.id::text)"
                " ORDER BY r.created_at LIMIT :limit"
            ),
            {"limit": limit},
        )
    ).all()
    out: list[_Card] = []
    for r in rows:
        payload = r.payload if isinstance(r.payload, dict) else {}
        predicate = str(payload.get("predicate", "")).strip()
        if not predicate:
            continue
        action, canonical = _proposed_resolution(payload)
        out.append(_Card(r.card_id, r.domain_code, predicate, action, canonical))
    return out


async def _owner_principal_id(session: AsyncSession) -> str:
    pid = (
        await session.execute(
            text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
        )
    ).scalar_one()
    return str(pid)


class PredicateReviewAction:
    """gate → fetch open cards → one owner `predicate-canon` proposal per domain."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        settings: SqlSettingsStore,
        proposals: ProposalRepo,
        ctx: SessionContext = SYSTEM_CTX,
    ):
        self._maker = maker
        self._settings = settings
        self._proposals = proposals
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        # Embedding-only (suggestions are already on the card), so estimated_tokens=0 — only the
        # kill-switch / exhausted-budget terms gate; nothing is charged.
        decision = await self._gate.check(self._ctx, estimated_tokens=0)
        if not decision.allowed:
            raise PermanentJobError(f"predicate_review refused: {decision.reason}")

        async with scoped_session(self._maker, self._ctx) as session:
            cards = await _open_cards(session, limit=_BATCH)
            owner_pid = await _owner_principal_id(session) if cards else ""

        by_domain: dict[str, list[_Card]] = defaultdict(list)
        for card in cards:
            by_domain[card.domain].append(card)

        for domain, domain_cards in by_domain.items():
            nodes = [
                NodeSpec(
                    id=str(uuid.uuid4()),
                    type="leaf",
                    op="predicate_resolve",
                    label=(
                        f"Map '{c.predicate}' → {c.canonical}"
                        if c.action == "map_to_existing"
                        else f"Mint predicate '{c.predicate}'"
                    ),
                    preview={
                        "card_id": c.card_id,
                        "action": c.action,
                        "canonical_name": c.canonical,
                        "predicate": c.predicate,
                    },
                )
                for c in domain_cards
            ]
            await self._proposals.stage(
                self._ctx,
                principal_id=owner_pid,
                spec=ProposalSpec(
                    kind="predicate-canon",
                    domain=domain,
                    title=f"Review {len(nodes)} predicate card(s) in {domain}",
                    nodes=nodes,
                    provenance={"source": "predicate_review"},
                ),
            )
        if cards:
            log.info("predicate_review_staged", cards=len(cards), domains=len(by_domain))


def predicate_review_handler(maker: async_sessionmaker[AsyncSession]) -> Any:
    """Worker dispatch entry for `predicate_review` (payload-only Handler)."""
    action = PredicateReviewAction(
        maker,
        settings=SqlSettingsStore(maker),
        proposals=ProposalRepo(maker),
    )
    return action.run
