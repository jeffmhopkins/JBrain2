"""Run a jmolt night on demand, in seconds, against a recorded platform.

The binding constraint on this system has never been a shortage of design ideas — it is that
a change costs a night and returns one sample. Two pre-registered probe studies could not
reach the behaviour under study; a shipped fix (showing jmolt its own post titles) turned out
not to work; a trajectory-replay harness failed its own validation. This is S1 of
docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, and nothing else in that plan is built before it.

What makes a measurement taken here worth anything is that almost none of the night is
simulated. The runner is the production `JmoltNightRunner`; the loop, the sittings, the
prologue, the write tools, the outbox, the guards, the caps, the pacing and the action ledger
are all the shipped code on the shipped path. Three things are swapped:

- **The platform** — `SimMoltbookClient`, holding no credential and no transport.
- **The clock** — driven by the caller, so the hour is exercised without waiting an hour.
- **The principal** — a synthetic id, so the scratchpad, outbox and ledger a sim night reads
  and writes are its own. Every jmolt query except `OutboxRepo.due` is principal-scoped, and
  `due` is fenced by the `sim` column (migration 0180), so a simulated write cannot reach the
  real Moltbook and cannot appear in the owner's digest as something jmolt did.

The settings store is wrapped rather than faked: reads fall through to the real box, so a sim
night sees the real advisory note and timezone unless an arm deliberately overrides them, and
writes are caught in memory so a sim cannot stamp the live night's deadline.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.jmolt_night import JmoltNightRunner, jmolt_run_context
from jbrain.agent.jmolt_sim_client import SimCorpus, SimMoltbookClient, SimWrite
from jbrain.agent.jmolt_sweep import JmoltSweep
from jbrain.agent.moltbooktools import build_moltbook_handlers
from jbrain.agent.moltbookwritetools import build_moltbook_write_handlers
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo, LedgerRow, OutboxRepo, OutboxRow

log = structlog.get_logger(__name__)

# A sim principal is a namespace, not an identity. It is a real `app.principals` row because
# `agent_sessions.principal_id` carries a foreign key to it, but it is minted as a
# `capability_token` rather than an `owner`: jmolt's data anchor is "the oldest owner
# principal" (jmolt_owner.py), and a simulator that could ever become that anchor would be a
# simulator that could re-home the real jmolt's entire history. Its `key_hash` is a random
# value nothing hashes to, so it authenticates nothing; RLS is satisfied by the session's
# declared `principal_kind`, which `_sim_owner_ctx` sets, not by this row.
SIM_PRINCIPAL_LABEL = "jmolt simulator night"
_SIM_KEY_PREFIX = "sim-night-not-a-key:"


async def _mint_sim_principal(maker: async_sessionmaker[AsyncSession]) -> str:
    """A fresh principal for one sim night. Fresh per night, not per arm: two nights of the
    same arm must not see each other's scratchpad, or the second is measuring the first's
    memory rather than the arm."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash, label)"
                " VALUES (cast(:id AS uuid), 'capability_token', :kh, :label)"
            ),
            {"id": pid, "kh": f"{_SIM_KEY_PREFIX}{uuid.uuid4()}", "label": SIM_PRINCIPAL_LABEL},
        )
    return pid


async def purge_sim_nights(maker: async_sessionmaker[AsyncSession]) -> int:
    """Delete every simulated night's rows and principals. Returns principals removed.

    A sim night is disposable by design — one principal per night — so a harness run of
    twenty nights per arm leaves rows behind. This is how they go, and it is reachable
    without a shell because the owner has none (CLAUDE.md, non-negotiable 10)."""
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        ids = [
            str(r[0])
            for r in (
                await s.execute(
                    text("SELECT id FROM app.principals WHERE label = :label"),
                    {"label": SIM_PRINCIPAL_LABEL},
                )
            ).all()
        ]
        if not ids:
            return 0
        for table in (
            "jmolt_outbox",
            "jmolt_action_ledger",
            "jmolt_scratch",
            "jmolt_scratch_archive",
        ):
            await s.execute(
                text(f"DELETE FROM app.{table} WHERE principal_id = ANY(:ids)"), {"ids": ids}
            )
        await s.execute(
            text("DELETE FROM app.agent_sessions WHERE principal_id = ANY(cast(:ids AS uuid[]))"),
            {"ids": ids},
        )
        await s.execute(
            text("DELETE FROM app.principals WHERE id = ANY(cast(:ids AS uuid[]))"), {"ids": ids}
        )
    return len(ids)


@dataclass
class SimSpec:
    """One night's starting world, and the arm's configuration.

    `scratch` is jmolt's own memory as of that night — without it a sim night under a fresh
    principal would find an empty scratchpad and run the FIRST-NIGHT bootstrap prologue,
    which is a different system from the one under study.
    """

    corpus: SimCorpus
    scratch: dict[str, str] = field(default_factory=dict)
    advisory: str | None = None
    handle: str | None = None
    autonomy: bool = True
    timezone: str = "UTC"
    # Seconds the clock jumps per read. The sittings loop reads it once per iteration, so
    # this sets how many sittings the simulated hour affords — the real constraint on a live
    # night is the model's latency, and this is the knob that stands in for it.
    clock_step_s: float = 240.0
    label: str = ""


@dataclass
class SimNight:
    """What one simulated night did. Everything here is a record, not a narration: the
    believed writes come from the client, the staged rows from the outbox, the actions from
    the ledger — never from anything the model said about itself."""

    principal_id: str
    session_id: str
    started_at: datetime
    writes: list[SimWrite] = field(default_factory=list)
    outbox: list[OutboxRow] = field(default_factory=list)
    ledger: list[LedgerRow] = field(default_factory=list)
    scratch_after: dict[str, str] = field(default_factory=dict)
    label: str = ""
    error: str = ""

    @property
    def posts(self) -> list[OutboxRow]:
        return [r for r in self.outbox if r.kind == "post"]

    @property
    def comments(self) -> list[OutboxRow]:
        return [r for r in self.outbox if r.kind == "comment"]


class _SimSettings:
    """The real settings store with the night's writes caught and the arm's reads pinned.

    Delegation is by `__getattr__` on purpose: the night reads a small, enumerable set of
    settings today, but a future wave adding one must not silently get a fake default — it
    gets the real box's value until someone decides otherwise.
    """

    def __init__(self, inner: Any, spec: SimSpec) -> None:
        self._inner = inner
        self._spec = spec
        self.captured: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("set_"):

            async def _sink(_ctx: Any, *args: Any, **kw: Any) -> None:
                self.captured[name] = args[0] if args else kw
                return None

            return _sink
        return getattr(self._inner, name)

    async def owner_timezone(self, _ctx: Any) -> str:
        return self._spec.timezone

    async def moltbook_advisory_note(self, ctx: Any) -> str:
        if self._spec.advisory is not None:
            return self._spec.advisory
        return str(await self._inner.moltbook_advisory_note(ctx) or "")

    async def moltbook_handle(self, ctx: Any) -> str:
        if self._spec.handle is not None:
            return self._spec.handle
        return str(await self._inner.moltbook_handle(ctx) or "")

    async def moltbook_autonomy(self, _ctx: Any) -> bool:
        return self._spec.autonomy

    async def moltbook_killed(self, _ctx: Any) -> bool:
        # A sim night is never killed by the live switch: an arm that silently ran zero
        # sittings because the owner had paused writing would score as perfect restraint.
        return False


def _stepped_clock(step_s: float, start: datetime) -> Callable[[], datetime]:
    """A clock the simulated night drives. The sittings loop reads it once per iteration, so
    a step is one sitting's worth of the hour."""
    state = {"t": start}

    def _now() -> datetime:
        cur = state["t"]
        state["t"] = cur + timedelta(seconds=step_s)
        return cur

    return _now


class JmoltSimulator:
    """Runs sim nights. Holds the pieces a night needs that are not per-night."""

    def __init__(
        self,
        *,
        maker: async_sessionmaker[AsyncSession],
        registry: ToolRegistry,
        settings_store: Any,
        executor_factory: Callable[[ToolRegistry], Any],
        session_repos: Callable[[], tuple[Any, Any, Any]],
        router: Any = None,
    ) -> None:
        self._maker = maker
        self._registry = registry
        self._settings = settings_store
        self._executor_factory = executor_factory
        self._session_repos = session_repos
        # Only reached if the platform hands back a verification challenge; the simulator's
        # never does. Present so the shipped publish path is wired exactly as it ships.
        self._router = router

    async def run(self, spec: SimSpec, *, at: datetime | None = None) -> SimNight:
        """One simulated night, end to end. Never raises: a night that died is a data point
        (and the arm's error rate is itself a score), not a broken harness."""
        pid = await _mint_sim_principal(self._maker)
        started = at or datetime.now(UTC)
        client = SimMoltbookClient(spec.corpus, clock=_stepped_clock(0.0, started))
        settings = _SimSettings(self._settings, spec)
        await self._seed_scratch(pid, spec)

        # The simulator's own sweep, over the transport-less client and pinned to this
        # night's principal. It is what makes a staged write actually GO OUT and become
        # visible to jmolt's own next read — the loop whose absence produced the
        # seventeen-comment night — through the shipped publish path rather than a second
        # implementation of it. It publishes only sim rows; the live sweep only real ones.
        sweep = JmoltSweep(
            maker=self._maker,
            client=client,
            router=self._router,
            settings_store=settings,
            sim=True,
            principal_id=pid,
        )

        async def _publish_now(row_id: str) -> tuple[str, str]:
            return await sweep.publish_row_now(row_id)

        # The production tool builders, over the simulated client. `sim=True` marks every
        # staged row so the live sweep cannot see it — the write tools, guards, caps,
        # pacing and ledger are otherwise the shipped ones, which is the point.
        overrides = dict(build_moltbook_handlers(client))
        overrides.update(
            build_moltbook_write_handlers(self._maker, settings, publish_now=_publish_now, sim=True)
        )
        sessions, runlog, transcript = self._session_repos()
        runner = JmoltNightRunner(
            sessions=sessions,
            runlog=runlog,
            transcript=transcript,
            executor=self._executor_factory(self._registry.with_handlers(overrides)),
            settings_store=settings,
            maker=self._maker,
            clock=_stepped_clock(spec.clock_step_s, started),
            # No night hold: a simulated night must not reserve the box's real GPU.
            served_model_loader=None,
        )
        night = SimNight(
            principal_id=pid, session_id="", started_at=started, label=spec.label, writes=[]
        )
        try:
            night.session_id = await runner.run(_sim_owner_ctx(pid))
        except Exception as exc:  # noqa: BLE001 — a dead night is a data point, not a crash
            log.warning("jmolt_sim.night_failed", principal_id=pid, exc_info=True)
            night.error = f"{type(exc).__name__}: {exc}"
        night.writes = list(client.writes)
        await self._collect(night, started)
        return night

    async def _seed_scratch(self, pid: str, spec: SimSpec) -> None:
        """Give the night jmolt's memory as of the night being reproduced. An empty
        scratchpad is not a neutral starting point — it triggers the first-night bootstrap."""
        if not spec.scratch:
            return
        repo = JmoltScratchRepo()
        async with scoped_session(self._maker, jmolt_run_context(pid)) as s:
            for filename, content in spec.scratch.items():
                await repo.write(s, pid, filename=filename, content=content)

    async def _collect(self, night: SimNight, since: datetime) -> None:
        """The night's record, read back from the same tables the observer and the digest
        read. Deliberately not from the transcript: what the model said it did is the thing
        under study, not the source of truth about it."""
        pid = night.principal_id
        async with scoped_session(self._maker, _sim_owner_ctx(pid)) as s:
            night.outbox = await OutboxRepo().list_by_status(
                s, pid, ("queued", "released", "published", "failed", "discarded")
            )
            night.ledger = await ActionLedgerRepo().since(s, pid, since=since)
            night.scratch_after = await _scratch_files(s, pid)


async def _scratch_files(session: AsyncSession, pid: str) -> dict[str, str]:
    """The scratchpad as it stands, contents included — `list_files` carries sizes only, and
    what a night WROTE into its files is half of what a night is scored on."""
    repo = JmoltScratchRepo()
    out: dict[str, str] = {}
    for f in await repo.list_files(session, pid):
        out[f.filename] = await repo.read(session, pid, f.filename) or ""
    return out


def _sim_owner_ctx(pid: str) -> SessionContext:
    """The owner context a sim night runs under. `principal_kind='owner'` is what satisfies
    `is_owner()` in the RLS policies; the synthetic principal id is what keeps the rows out
    of the real jmolt's world."""
    return SessionContext(principal_id=pid, principal_kind="owner")
