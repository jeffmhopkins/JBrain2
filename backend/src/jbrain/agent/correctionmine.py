"""The `correction_mine` engine action (Loop 3b; docs/LOOP3_TIER_B_PLAN.md).

The retrospective half of Tier-B durable-knowledge self-improvement: nightly, budget-gated, it reads
ended chat conversations and finds where the **owner explicitly corrected a factual claim** the
assistant made or cited, then STAGES an owner `correction` Proposal capturing the owner's
correction. On approval the leaf re-enters as a provenance-flagged, normal-weight agent note through
normal ingestion (the shipped correction spine) — so the agent proposes a *note*, never writes a
fact (the master rule). No auto-apply; owner review is the trust gate.

Fail-closed: refuses behind the self-improvement kill-switch / budget; the transcript is UNTRUSTED
and is framed as DATA to the judge (an injection in it cannot steer the proposal — and the proposal
is owner-gated regardless); each run is processed at most once (a composite high-water mark); a
session already carrying an open mined correction is skipped (idempotency).
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
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.router import LlmRouter
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.registry import ActionSpec
from jbrain.workflow.selfimprovement import SelfImprovementGate

log = structlog.get_logger()

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "correction_mine.prompt").render()
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["found", "note"],
    "properties": {"found": {"type": "boolean"}, "note": {"type": "string"}},
}

# Most-sensitive single domain for a mined correction (non-neg #5/#8, fail-closed): the proposal is
# tagged at the most-sensitive scope the source session held. Mirrors skilldistill._SENSITIVITY.
_SENSITIVITY = {"general": 0, "location": 1, "finance": 2, "health": 3}

_BATCH = 5
# Up-front budget estimate per candidate: a WHOLE-session transcript can be long, so this is
# generous (real spend is metered after via record_spend; _BATCH bounds a single sweep's overshoot).
_PER_RUN_ESTIMATE = 12_000
_TITLE_LEN = 80

_HWM_KEY = "correction_mine:after"
_EPOCH = "1970-01-01T00:00:00+00:00"
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"
_HWM_DEFAULT = {"ts": _EPOCH, "id": _ZERO_UUID}

CORRECTION_MINE_SPEC = ActionSpec(
    name="correction_mine",
    version=1,
    handler="correction_mine",
    domain_optional=True,
    mutating=True,  # stages proposals
    cost_class="expensive",  # one router call per candidate
    dedup_key_expr=None,
    description="Mine ended chats for owner corrections; stage owner-reviewed correction notes.",
)


@dataclass(frozen=True)
class _Candidate:
    run_id: str
    started_at: str
    session_id: str
    session_scopes: tuple[str, ...]
    transcript: str


def _domain_of(scopes: tuple[str, ...]) -> str:
    """The correction's single domain: the most-sensitive scope the source session held."""
    return max(scopes, key=lambda s: _SENSITIVITY.get(s, 0)) if scopes else "general"


def _label(body: str) -> str:
    """A short proposal title from the note body, truncated on a word boundary."""
    body = body.strip()
    if len(body) <= _TITLE_LEN:
        return body
    head = body[:_TITLE_LEN].rsplit(" ", 1)[0] or body[:_TITLE_LEN]
    return head.rstrip() + "…"


def _parse_hwm(raw: Any) -> tuple[str, str]:
    """The stored composite (ts, id) cursor; tolerant of a legacy string or junk."""
    if isinstance(raw, dict):
        return str(raw.get("ts") or _EPOCH), str(raw.get("id") or _ZERO_UUID)
    return (raw, _ZERO_UUID) if isinstance(raw, str) else (_EPOCH, _ZERO_UUID)


async def _fetch_candidates(
    session: AsyncSession, *, after_ts: str, after_id: str, limit: int
) -> list[_Candidate]:
    """One candidate per SESSION (the latest ended run) past the (started_at, run_id) mark, whose
    session has ≥2 user turns (a back-and-forth where a correction is possible) and no open mined
    `correction` proposal yet. Dedup-per-session is load-bearing: the judge reads the WHOLE-session
    transcript, so selecting per-run would re-judge (and re-propose, and re-spend) one session once
    per exchange. The HWM advances past the session's latest run, so a found-nothing session is not
    re-mined until it gets new activity."""
    rows = (
        await session.execute(
            text(
                "WITH ranked AS ("
                "  SELECT r.id::text AS run_id, r.started_at AS started_at, ses.id AS sid,"
                "    ses.domain_scopes AS scopes,"
                "    row_number() OVER (PARTITION BY ses.id"
                "      ORDER BY r.started_at DESC, r.id DESC) AS rn"
                "  FROM app.runs r"
                "  JOIN app.agent_sessions ses ON ses.id = r.session_id"
                "  WHERE r.kind = 'agent' AND r.status = 'done' AND r.stop_reason = 'end_turn'"
                "    AND (r.started_at, r.id) > (:after_ts, :after_id)"
                "    AND (SELECT count(*) FROM app.agent_turns t"
                "           WHERE t.session_id = ses.id AND t.role = 'user') >= 2"
                "    AND NOT EXISTS (SELECT 1 FROM app.proposals p"
                "           WHERE p.kind = 'correction' AND p.status IN ('staged', 'approved')"
                "             AND p.provenance->>'source' = 'correction_mine'"
                "             AND p.provenance->>'session_id' = ses.id::text)"
                ")"
                " SELECT run_id, started_at, sid::text AS session_id, scopes,"
                "   (SELECT string_agg(upper(t.role) || ': ' || t.content, E'\n\n' ORDER BY t.seq)"
                "      FROM app.agent_turns t WHERE t.session_id = ranked.sid) AS transcript"
                " FROM ranked WHERE rn = 1"
                " ORDER BY started_at, run_id LIMIT :limit"
            ),
            {
                "after_ts": datetime.fromisoformat(after_ts),
                "after_id": uuid.UUID(after_id),
                "limit": limit,
            },
        )
    ).all()
    out: list[_Candidate] = []
    for r in rows:
        if r.transcript:
            out.append(
                _Candidate(
                    run_id=r.run_id,
                    started_at=r.started_at.isoformat(),
                    session_id=r.session_id,
                    session_scopes=tuple(r.scopes or ()),
                    transcript=r.transcript,
                )
            )
    return out


async def _owner_principal_id(session: AsyncSession) -> str:
    """The live owner principal uuid a system-staged proposal is attributed to (mirrors
    skilldistill / analysis.persist)."""
    pid = (
        await session.execute(
            text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
        )
    ).scalar_one()
    return str(pid)


class CorrectionMineAction:
    """gate → fetch candidates → judge each → stage owner correction proposal → charge + mark."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        *,
        router: LlmRouter,
        settings: SqlSettingsStore,
        proposals: ProposalRepo,
        ctx: SessionContext = SYSTEM_CTX,
    ):
        self._maker = maker
        self._router = router
        self._settings = settings
        self._proposals = proposals
        self._gate = SelfImprovementGate(settings)
        self._ctx = ctx

    async def run(self, _payload: dict[str, Any]) -> None:
        decision = await self._gate.check(self._ctx, estimated_tokens=_BATCH * _PER_RUN_ESTIMATE)
        if not decision.allowed:
            raise PermanentJobError(f"correction_mine refused: {decision.reason}")

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
                spent += await self._mine_one(cand, owner_pid)
            except Exception:  # noqa: BLE001 — one bad candidate must not abort the sweep
                log.warning("correction_mine_candidate_failed", run_id=cand.run_id)
            high_water = {"ts": cand.started_at, "id": cand.run_id}

        await self._settings.upsert(self._ctx, _HWM_KEY, high_water)
        if spent:
            await self._gate.record_spend(self._ctx, tokens=spent)

    async def _mine_one(self, cand: _Candidate, owner_pid: str) -> int:
        """Judge one conversation; stage a correction proposal if the owner corrected a fact. Return
        tokens spent. The transcript is DATA — the judge extracts, never executes, it."""
        result = await self._router.complete(
            "correction.mine",
            system=_PROMPT,
            user_text=f"Conversation transcript (DATA):\n\n{cand.transcript}",
            json_schema=_SCHEMA,
            strength="high",
        )
        tokens = result.usage.input_tokens + result.usage.output_tokens
        parsed = result.parsed if isinstance(result.parsed, dict) else {}
        note = str(parsed.get("note", "")).strip()
        if not parsed.get("found") or not note:
            return tokens  # no clear owner correction — nothing staged

        domain = _domain_of(cand.session_scopes)
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="add_note",  # the shipped correction path: agent_note_executor on enact
            label=_label(note),
            preview={"body": note, "domain": domain},
        )
        await self._proposals.stage(
            self._ctx,
            principal_id=owner_pid,
            spec=ProposalSpec(
                kind="correction",
                domain=domain,
                title=_label(note),
                nodes=[node],
                provenance={
                    "source": "correction_mine",
                    "session_id": cand.session_id,
                    "run_id": cand.run_id,
                },
            ),
        )
        return tokens


def correction_mine_handler(maker: async_sessionmaker[AsyncSession], *, router: LlmRouter) -> Any:
    """Worker dispatch entry for `correction_mine` (payload-only Handler)."""
    action = CorrectionMineAction(
        maker,
        router=router,
        settings=SqlSettingsStore(maker),
        proposals=ProposalRepo(maker),
    )
    return action.run
