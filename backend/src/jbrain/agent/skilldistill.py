"""The `skill_distill` engine action (Loop 2, Wave 2; docs/LOOP2_SKILL_LEARNING_PLAN.md).

Nightly, budget-gated: select successful agent runs (≥2 tool calls), distill each into a sanitized,
parameterized **shadow** skill via the LLM router, and stage an owner `skill-promotion` Proposal —
so nothing becomes `active` (and thus retrievable) without the owner reviewing the playbook (the
MVP's trust+promotion gate; no auto-promotion). Writes shadow skills only, so it is inert in the
turn loop until the owner enacts a promotion.

Fail-closed: refuses before spending when the kill-switch is on or the self-improvement budget is
gone (`SelfImprovementGate`); a run is processed at most once (a `started_at` high-water mark in
settings); the distilled playbook is data, never world-facts (the prompt + parameterization), and a
candidate too close to an existing same-domain skill is skipped (embedding dedup).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.agent.skills import SkillsRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import EmbedClient
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.router import LlmRouter
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

log = structlog.get_logger()

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "skill_distill.prompt").render()
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "description", "body", "reusable"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "body": {"type": "string"},
        "reusable": {"type": "boolean"},
    },
}

# Most-restrictive single domain for a skill (non-neg #5, fail-closed): a multi-scope source run
# tags its skill at the most-sensitive scope, never split. Mirrors the analysis sensitivity order.
_SENSITIVITY = {"general": 0, "location": 1, "finance": 2, "health": 3}

# How many runs to distill per nightly sweep, and the per-run token estimate checked up front.
_BATCH = 5
_PER_RUN_ESTIMATE = 8_000
# A candidate whose nearest same-domain skill is closer than this cosine distance is a duplicate.
_DEDUP_DISTANCE = 0.05
# Settings key for the high-water mark (a row, not a migration). The cursor is a COMPOSITE
# (started_at, run_id), not a bare timestamp: two runs that share a `started_at` would otherwise let
# the second be silently skipped once the first advanced the mark. Stored as {"ts", "id"}.
_HWM_KEY = "skill_distill:after"
_EPOCH = "1970-01-01T00:00:00+00:00"
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_HWM_DEFAULT = {"ts": _EPOCH, "id": _ZERO_UUID}

SKILL_DISTILL_SPEC = ActionSpec(
    name="skill_distill",
    version=1,
    handler="skill_distill",
    domain_optional=True,
    mutating=True,  # writes shadow skills + stages proposals
    cost_class="expensive",
    dedup_key_expr=None,
    description="Distill skills from successful agent runs into owner-reviewed shadow skills.",
)


@dataclass(frozen=True)
class _Candidate:
    run_id: str
    started_at: str
    tool_names: tuple[str, ...]
    prose: str
    session_scopes: tuple[str, ...]


def _domain_of(scopes: tuple[str, ...]) -> str:
    """The skill's single domain: the most-sensitive scope the source run held (fail-closed)."""
    return max(scopes, key=lambda s: _SENSITIVITY.get(s, 0)) if scopes else "general"


def _parse_hwm(raw: Any) -> tuple[str, str]:
    """The stored composite cursor as (ts, id). A dict is the current form; a bare string is the
    legacy ts-only form (id floored to zero); anything else floors to the epoch."""
    if isinstance(raw, dict):
        return str(raw.get("ts") or _EPOCH), str(raw.get("id") or _ZERO_UUID)
    return (raw, _ZERO_UUID) if isinstance(raw, str) else (_EPOCH, _ZERO_UUID)


async def _fetch_candidates(
    session: AsyncSession, *, after_ts: str, after_id: str, limit: int
) -> list[_Candidate]:
    rows = (
        await session.execute(
            text(
                "SELECT r.id::text AS run_id, r.started_at,"
                " array_agg(rs.name ORDER BY rs.idx)"
                "   FILTER (WHERE rs.kind = 'tool') AS tool_names,"
                " ses.domain_scopes AS scopes,"
                " (SELECT string_agg(t.content, E'\n' ORDER BY t.seq) FROM app.agent_turns t"
                "   WHERE t.run_id = r.id AND t.role = 'assistant') AS prose"
                " FROM app.runs r"
                " JOIN app.run_steps rs ON rs.run_id = r.id"
                " JOIN app.agent_sessions ses ON ses.id = r.session_id"
                " WHERE r.kind = 'agent' AND r.status = 'done' AND r.stop_reason = 'end_turn'"
                "   AND (r.started_at, r.id) > (:after_ts, :after_id)"
                " GROUP BY r.id, r.started_at, ses.domain_scopes"
                " HAVING count(*) FILTER (WHERE rs.kind = 'tool') >= 2"
                " ORDER BY r.started_at, r.id LIMIT :limit"
            ),
            # Bind typed objects, not strings: the row comparison pins each param to the column's
            # type for asyncpg, so a `cast(... AS timestamptz)` is ignored and a str is rejected.
            {
                "after_ts": datetime.fromisoformat(after_ts),
                "after_id": uuid.UUID(after_id),
                "limit": limit,
            },
        )
    ).all()
    out: list[_Candidate] = []
    for r in rows:
        if r.tool_names and r.prose:
            out.append(
                _Candidate(
                    run_id=r.run_id,
                    started_at=r.started_at.isoformat(),
                    tool_names=tuple(r.tool_names),
                    prose=r.prose,
                    session_scopes=tuple(r.scopes or ()),
                )
            )
    return out


async def _owner_principal_id(session: AsyncSession) -> str:
    """The live owner principal uuid — the FK a system-staged proposal is attributed to (SYSTEM_CTX
    is owner-kind, so RLS lets it read app.principals). Mirrors analysis/persist.py."""
    pid = (
        await session.execute(
            text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
        )
    ).scalar_one()
    return str(pid)


class SkillDistillAction:
    """gate → fetch candidates → distill → shadow skill + owner proposal → charge + mark."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        router: LlmRouter,
        embedder: EmbedClient,
        embedding_model: str,
        settings: SqlSettingsStore,
        skills: SkillsRepo,
        proposals: ProposalRepo,
        ctx: SessionContext = SYSTEM_CTX,
    ):
        self._maker = maker
        self._router = router
        self._embedder = embedder
        self._model = embedding_model
        self._settings = settings
        self._skills = skills
        self._proposals = proposals
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        decision = await self._gate.check(self._ctx, estimated_tokens=_BATCH * _PER_RUN_ESTIMATE)
        if not decision.allowed:
            raise PermanentJobError(f"skill_distill refused: {decision.reason}")

        after_ts, after_id = _parse_hwm(await self._settings.get(self._ctx, _HWM_KEY, _HWM_DEFAULT))
        async with scoped_session(self._maker, self._ctx) as session:
            candidates = await _fetch_candidates(
                session, after_ts=after_ts, after_id=after_id, limit=_BATCH
            )
            owner_pid = await _owner_principal_id(session) if candidates else ""

        spent = 0
        high_water = {"ts": after_ts, "id": after_id}
        for cand in candidates:
            try:
                spent += await self._distill_one(cand, owner_pid)
            except Exception:  # noqa: BLE001 — one bad candidate must not abort the sweep
                log.warning("skill_distill_candidate_failed", run_id=cand.run_id)
            # Advance the composite cursor past it regardless (don't re-attempt a bad run);
            # candidates are ordered by (started_at, id) so the last one is the new mark.
            high_water = {"ts": cand.started_at, "id": cand.run_id}

        await self._settings.upsert(self._ctx, _HWM_KEY, high_water)
        if spent:
            await self._gate.record_spend(self._ctx, tokens=spent)

    async def _distill_one(self, cand: _Candidate, owner_pid: str) -> int:
        """Distill one candidate; return tokens spent. Drops a non-reusable or duplicate result."""
        user_text = (
            "Tool sequence (in order):\n"
            + "\n".join(f"- {name}" for name in cand.tool_names)
            + f"\n\nAssistant prose from the run:\n{cand.prose}"
        )
        # Resolve via the prompt's strength tier (an internal task, not surfaced in the routing UI).
        result = await self._router.complete(
            "skill.distill",
            system=_PROMPT,
            user_text=user_text,
            json_schema=_SCHEMA,
            strength="high",
        )
        tokens = result.usage.input_tokens + result.usage.output_tokens
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        if not parsed.get("reusable") or not str(parsed.get("body", "")).strip():
            return tokens
        name = str(parsed.get("name", "")).strip() or "untitled skill"
        description = str(parsed.get("description", "")).strip()
        body = str(parsed.get("body", "")).strip()
        domain = _domain_of(cand.session_scopes)

        embedding = (await self._embedder.embed([f"{description}\n{body}"]))[0]
        near = await self._skills.nearest_distance(self._ctx, domain, embedding)
        if near is not None and near < _DEDUP_DISTANCE:
            return tokens  # a near-duplicate already exists in this domain

        skill_id = await self._skills.create(
            self._ctx,
            name=name,
            description=description,
            body=body,
            domain_code=domain,
            status="shadow",
            embedding=embedding,
            embedding_model=self._model,
        )
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="skill_promote",
            label=f"Promote skill: {name}",
            preview={
                "skill_id": skill_id,
                "name": name,
                "description": description,
                "body": body,
                "domain": domain,
            },
        )
        await self._proposals.stage(
            self._ctx,
            principal_id=owner_pid,
            spec=ProposalSpec(
                kind="skill-promotion",
                domain=domain,
                title=f"Promote skill: {name}",
                nodes=[node],
                provenance={"source": "skill_distill", "run_id": cand.run_id},
            ),
        )
        return tokens


def skill_distill_handler(
    maker: async_sessionmaker[AsyncSession],
    *,
    router: LlmRouter,
    embedder: EmbedClient,
    embedding_model: str,
) -> Any:
    """Worker dispatch entry for `skill_distill` (payload-only Handler)."""
    action = SkillDistillAction(
        maker,
        router=router,
        embedder=embedder,
        embedding_model=embedding_model,
        settings=SqlSettingsStore(maker),
        skills=SkillsRepo(maker),
        proposals=ProposalRepo(maker),
    )
    return action.run
