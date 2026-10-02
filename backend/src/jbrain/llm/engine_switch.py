"""The local engine switch — ONE orchestration behind the owner route and the debug route.

docs/plans/FLASH_NEXT_ENGINE_PLAN.md F3a. Standard (`local-llm`) or Flash-Next (`flash-next`),
never both up (§4d: their footprints together freeze a 128 GB box). The order is fixed and
every step is checked before the next:

  draining  close local admission across the api and the worker (jbrain.llm.drain), then wait
            for in-flight local calls to finish — bounded, then proceed
  stopping  unload the running gateway's models through the client (so the ledger and the
            vitals surface stay honest), stop every OTHER engine, wait until the supervisor
            REPORTS it stopped, wait for device memory to settle
  starting  start the target and wait until it reports running; record it as EFFECTIVE (it is
            what is up — the re-stamp and residency must follow it from here)
  loading   load the model the smoke test runs on
  smoke     a text completion, a tool-call probe, and an image probe on a vision model
  done      persist the target as DESIRED too, reopen admission

The whole run holds the supervisor's switch hold (`/engine-switch/hold`), so nothing else —
the Ops toggle, a restart, jcode's power-on, an update — can start an engine or a one-shot
under it, and the api refuses its own engine-affecting routes while `busy`.

Any failure after the target was touched: stop it, wait until it is CONFIRMED down, restart
the previous engine and record it as effective (`rolled_back`). Whatever cannot be confirmed,
the switch ends by reading what is actually up and recording THAT as effective — one engine
up: that one; none up: it says NO local engine is up; both up: it restores nothing and says
so, because starting anything beside them is the freeze. Admission is reopened on every exit.
Each switch ends as a box event.

It runs as a background task: a Flash-Next load alone outlives an HTTP request and the
Cloudflare 100 s limit, so the POST answers 202 and the status is polled. The status is held
in memory AND written to the settings store at each stage, so a poll from another process or
after a restart still reads the last result.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from jbrain import box_events
from jbrain.llm import drain, local_catalog
from jbrain.llm import engine as engines

log = structlog.get_logger()

DRAINING = "draining"
STOPPING = "stopping"
STARTING = "starting"
LOADING = "loading"
SMOKE = "smoke"
DONE = "done"
ROLLED_BACK = "rolled_back"
FAILED = "failed"
# The owner cancelled while it was still draining — before anything was stopped.
CANCELLED = "cancelled"
TERMINAL = frozenset({DONE, ROLLED_BACK, FAILED, CANCELLED})


class SwitchRefused(Exception):
    """Refused before anything was touched — the route's 4xx/5xx."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class SupervisorError(Exception):
    """The supervisor could not be read or refused a toggle unexpectedly."""


class HoldRefused(Exception):
    """The supervisor would not grant the switch hold (another switch, a one-shot)."""


class UnloadFailed(Exception):
    def __init__(self, served: str, cause: Exception, released: list[str]) -> None:
        super().__init__(f"{served}: {cause}")
        self.served = served
        self.cause = cause
        self.released = released


class Supervisor(Protocol):
    async def states(self) -> dict[str, str]: ...

    async def oneshot(self) -> str | None: ...

    async def toggle(self, action: str, service: str, switch_id: str | None = None) -> int: ...

    # Take or renew the hold; False when the supervisor predates it (the api's own lock
    # then is the only guard). Raises HoldRefused / SupervisorError.
    async def hold(self, switch_id: str, ttl_s: float) -> bool: ...

    async def release(self, switch_id: str) -> None: ...


class SwitchGateway(Protocol):
    async def running_states(self) -> dict[str, str] | None: ...

    async def unload(self, served_model: str) -> None: ...

    async def load(self, served_model: str) -> None: ...

    async def slots(self, served_model: str) -> list[dict[str, object]]: ...

    async def tool_probe(self, served_model: str) -> None: ...

    async def text_probe(self, served_model: str) -> str: ...

    async def image_probe(self, served_model: str) -> str: ...


class SwitchStore(Protocol):
    async def llm_local_engine(self, ctx: Any) -> engines.Engine: ...

    async def set_llm_local_engine(self, ctx: Any, engine: engines.Engine) -> Any: ...

    async def llm_local_engine_effective(self, ctx: Any) -> engines.Engine: ...

    async def set_llm_local_engine_effective(
        self, ctx: Any, engine: engines.Engine, *, reason: str | None = None
    ) -> Any: ...

    async def set_llm_local_admission(self, ctx: Any, row: dict[str, Any]) -> None: ...

    async def llm_local_engine_switch(self, ctx: Any) -> dict[str, Any] | None: ...

    async def set_llm_local_engine_switch(self, ctx: Any, status: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Timings:
    # In-flight local calls get this long to finish before the engine is stopped under them.
    drain_s: float = 60.0
    # A stop or start must show in the supervisor's /status within this. A stop that does not
    # is re-issued once and waited for again before the switch gives up on it.
    settle_s: float = 120.0
    # Device memory must stop moving within this after the stop, or the start goes ahead.
    memory_settle_s: float = 60.0
    memory_settled_gb: float = 0.5
    poll_s: float = 2.0
    # The admission row's and the supervisor hold's deadline, re-extended at every stage, so a
    # switch never outlives it while a crashed one cannot hold the box for long.
    admission_ttl_s: float = 30 * 60.0
    # After closing admission: other processes may trust a cached "open" for one gate TTL;
    # this is that with margin for a call that read it just before the write.
    gate_wait_s: float = 3.0
    # How long another process may keep routing by a cached effective engine
    # (`engines.ACTIVE_ENGINE_TTL_S`); admission reopens only once that has passed.
    engine_cache_s: float = engines.ACTIVE_ENGINE_TTL_S


@dataclass
class SwitchDeps:
    supervisor: Supervisor
    gateway: SwitchGateway | None
    store: SwitchStore
    ctx: Any
    # Installed catalog ids (`settings.local_models`).
    local_models: Sequence[str]
    # Why a switch should wait (a workflow run, the nightly window), or None. Raising is a
    # refusal too: an unreadable schedule must not be read as a quiet box.
    quiet_guard: Callable[[], Awaitable[str | None]] | None = None
    # Device (GTT) memory in use, GB, or None when unreadable.
    memory_used_gb: Callable[[], Awaitable[float | None]] | None = None
    # The served name `agent.turn` runs on locally (the router's, remapped), or None.
    primary_model: Callable[[], Awaitable[str | None]] | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def smoke_model(
    engine: engines.Engine, local_models: Sequence[str], primary: str | None
) -> local_catalog.LocalModel | None:
    """The model a switch loads and smoke-tests on `engine`: its sole model (Flash-Next), else
    the model the chat turn runs on when it belongs to this engine and is installed (the
    keeper would load it next anyway), else the smallest installed tool-capable one."""
    installed = [
        m
        for m in local_catalog.selected(local_models)
        if engines.parse(m.engine) == engine and m.supports_tools
    ]
    sole = local_catalog.sole_model(engine)
    if sole is not None:
        return sole if sole.id in local_models else None
    if primary is not None:
        for m in installed:
            if m.served_model == primary:
                return m
    return min(installed, key=lambda m: m.size_gb) if installed else None


class EngineSwitcher:
    """One per api process (app.state). Holds the in-flight switch and its lock."""

    def __init__(
        self,
        timings: Timings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.timings = timings or Timings()
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._status: dict[str, Any] | None = None
        self._effective_written_at: float | None = None
        # Whether this run closed admission (so each stage write re-extends its deadline).
        self._admission_closed = False
        # Set by `cancel` while the run is draining; read before anything is stopped.
        self._cancel_requested = False

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def status(self, deps: SwitchDeps) -> dict[str, Any] | None:
        """The in-flight switch, else the last one recorded (this process or any other)."""
        if self._status is not None and self._status["stage"] not in TERMINAL:
            return dict(self._status)
        stored: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            stored = await deps.store.llm_local_engine_switch(deps.ctx)
        return stored or (dict(self._status) if self._status is not None else None)

    def cancel(self) -> dict[str, Any]:
        """Ask the in-flight switch to stop — only while it is DRAINING, when nothing has been
        unloaded or stopped yet, so cancelling costs nothing but the pause. The run notices at
        its next drain poll, reopens admission and ends `cancelled`. SwitchRefused (409) at any
        other moment: past draining, an engine is already being stopped, and the way back from
        there is the rollback, not an abort."""
        status = self._status
        if not self.busy or status is None or status["stage"] != DRAINING:
            stage = status["stage"] if status is not None and self.busy else "none"
            raise SwitchRefused(
                409,
                f"only a switch that is still draining can be cancelled (stage: {stage}); "
                "once it is stopping engines it runs to done or rolls back",
            )
        self._cancel_requested = True
        status["notes"].append("cancel requested")
        return dict(status)

    async def wait(self) -> None:
        """Until the in-flight switch ends (tests, shutdown)."""
        task = self._task
        if task is not None:
            await asyncio.shield(task)

    async def begin(
        self,
        deps: SwitchDeps,
        target: engines.Engine,
        *,
        source: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Refuse (SwitchRefused) or start the switch in the background and return its status.
        Switching to the engine already up, with the other down, only re-persists the setting
        and returns a `done` status without a job."""
        if self._lock.locked():
            raise SwitchRefused(409, "an engine switch is already in progress")
        await self._lock.acquire()
        try:
            status, previous, up = await self._preflight(deps, target, source=source, force=force)
            self._status = status
            await self._save(deps, status)
        except BaseException:
            self._lock.release()
            raise
        if status["stage"] == DONE:
            self._lock.release()
            return dict(status)
        self._task = asyncio.create_task(self._run(deps, status, previous, up))
        self._task.add_done_callback(_log_task_failure)
        return dict(status)

    async def _preflight(
        self, deps: SwitchDeps, target: engines.Engine, *, source: str, force: bool
    ) -> tuple[dict[str, Any], engines.Engine, list[engines.Engine]]:
        try:
            busy = await deps.supervisor.oneshot()
        except SupervisorError as exc:
            raise SwitchRefused(502, f"supervisor unreachable: {exc}") from exc
        if busy is not None:
            raise SwitchRefused(
                409, f"a supervisor one-shot ({busy}) is running; switch once it has finished"
            )
        if not force and deps.quiet_guard is not None:
            try:
                reason = await deps.quiet_guard()
            except Exception as exc:  # noqa: BLE001 — fail CLOSED, but say how to override
                raise SwitchRefused(
                    409,
                    f"could not check the nightly window or running workflows ({exc}); "
                    "send force: true to switch anyway.",
                ) from exc
            if reason is not None:
                raise SwitchRefused(
                    409,
                    f"not now: {reason}. Switching stops every local model for minutes; "
                    "send force: true to switch anyway.",
                )
        try:
            states = await deps.supervisor.states()
        except SupervisorError as exc:
            raise SwitchRefused(502, f"supervisor unreachable: {exc}") from exc
        service = engines.SERVICE[target]
        if service not in states:
            raise SwitchRefused(
                409,
                f"the {target} engine is not provisioned (no `{service}` container); "
                "Ops → Update creates it once its weights are installed. Nothing was changed.",
            )
        sole = local_catalog.sole_model(target)
        if sole is not None and sole.id not in deps.local_models:
            raise SwitchRefused(
                409,
                f"the {engines.LABEL[target]} weights are not installed — install "
                f"{sole.id} under On-box models first. Nothing was changed.",
            )
        previous = await deps.store.llm_local_engine_effective(deps.ctx)
        up: list[engines.Engine] = [
            e for e in engines.ENGINES if engines.holds_memory(states.get(engines.SERVICE[e], ""))
        ]
        status: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "source": source,
            "target": target,
            "previous": previous,
            "force": force,
            "stage": DRAINING,
            "reason": None,
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "ended_at": None,
            "stages": [{"stage": DRAINING, "at": _now_iso()}],
            "model": None,
            "smoke": [],
            "notes": [],
        }
        if target in up and all(e == target for e in up):
            await deps.store.set_llm_local_engine(deps.ctx, target)
            await deps.store.set_llm_local_engine_effective(deps.ctx, target)
            status.update(
                stage=DONE, ended_at=_now_iso(), stages=[{"stage": DONE, "at": _now_iso()}]
            )
            status["notes"].append(f"{target} was already the only engine up; setting re-persisted")
            return status, previous, up
        # Last: the hold is what the run keeps, so take it only once nothing else refuses.
        try:
            held = await deps.supervisor.hold(status["id"], self.timings.admission_ttl_s)
        except HoldRefused as exc:
            raise SwitchRefused(409, f"the supervisor refused the switch: {exc}") from exc
        except SupervisorError as exc:
            raise SwitchRefused(502, f"supervisor unreachable: {exc}") from exc
        if not held:
            status["notes"].append(
                "this supervisor predates the switch hold — only the api's own routes are "
                "refused while it runs"
            )
        return status, previous, up

    async def _save(self, deps: SwitchDeps, status: dict[str, Any]) -> None:
        status["updated_at"] = _now_iso()
        with contextlib.suppress(Exception):
            await deps.store.set_llm_local_engine_switch(deps.ctx, dict(status))

    async def _stage(self, deps: SwitchDeps, status: dict[str, Any], stage: str) -> None:
        status["stage"] = stage
        status["stages"].append({"stage": stage, "at": _now_iso()})
        if stage not in TERMINAL:
            await self._extend(deps, status)
        await self._save(deps, status)

    async def _extend(self, deps: SwitchDeps, status: dict[str, Any]) -> None:
        """Push the admission row's and the supervisor hold's deadlines out again, so a long
        switch never has admission reopen (or the hold lapse) in the middle of it."""
        ttl = self.timings.admission_ttl_s
        if self._admission_closed:
            row = drain.closed_row(f"switching to {status['target']}", ttl_s=ttl, now=self._wall())
            with contextlib.suppress(Exception):
                await deps.store.set_llm_local_admission(deps.ctx, row)
        with contextlib.suppress(Exception):
            await deps.supervisor.hold(status["id"], ttl)

    async def _run(
        self,
        deps: SwitchDeps,
        status: dict[str, Any],
        previous: engines.Engine,
        up: list[engines.Engine],
    ) -> None:
        target: engines.Engine = status["target"]
        self._effective_written_at = None
        self._cancel_requested = False
        outcome, reason = FAILED, "the switch did not finish"
        try:
            try:
                outcome, reason = await self._switch(deps, status, previous, up)
            except Exception as exc:  # noqa: BLE001 — a switch never leaves its status open
                log.exception("engine_switch.crashed", target=target)
                outcome, reason = FAILED, f"the switch crashed: {exc!r}"
            finally:
                await self._await_engine_caches()
                await self._open_admission(deps)
                with contextlib.suppress(Exception):
                    await deps.supervisor.release(status["id"])
            status["reason"] = reason
            status["ended_at"] = _now_iso()
            await self._stage(deps, status, outcome)
            try:
                await box_events.record(
                    box_events.ENGINE_SWITCH,
                    target,
                    detail=(
                        f"switched from {previous} to {target}"
                        if outcome == DONE
                        else f"{outcome.replace('_', ' ')}: {reason}"
                    ),
                    status="ok" if outcome in (DONE, CANCELLED) else "failed",
                )
            except Exception:  # noqa: BLE001 — narration never decides the outcome
                log.warning("engine_switch.box_event_failed", exc_info=True)
        finally:
            log.info(
                "engine_switch.ended",
                target=target,
                previous=previous,
                outcome=outcome,
                reason=reason,
            )
            self._lock.release()

    async def _switch(
        self,
        deps: SwitchDeps,
        status: dict[str, Any],
        previous: engines.Engine,
        up: list[engines.Engine],
    ) -> tuple[str, str | None]:
        target: engines.Engine = status["target"]
        service = engines.SERVICE[target]
        status["was_up"] = list(up)
        await self._drain(deps, status, target)
        # The last moment a cancel is honoured: nothing below this line is undone by one.
        if self._cancel_requested:
            return CANCELLED, f"cancelled while draining — nothing was stopped; {previous} serves"

        await self._stage(deps, status, STOPPING)
        try:
            await self._unload_resident(deps, f"switching the local engine to {target}")
        except UnloadFailed as exc:
            released = ", ".join(exc.released) or "none"
            return FAILED, (
                f"could not unload {exc.served} ({exc.cause}); no engine was stopped and "
                f"{previous} still serves, but these were already unloaded and reload on "
                f"their next use: {released}"
            )
        others: list[engines.Engine] = [e for e in up if e != target]
        # Put back ONE engine on failure — the effective one if it was up, else the first that
        # was. Restoring two would recreate the very state this exists to prevent.
        restore: engines.Engine | None = (
            previous if previous in others else (others[0] if others else None)
        )
        for engine in others:
            other = engines.SERVICE[engine]
            if not await self._stop_confirmed(deps, other):
                return await self._settle(
                    deps, f"{other} did not stop; {service} was NOT started", restore, status
                )
        await self._settle_memory(deps, status)

        await self._stage(deps, status, STARTING)
        if target not in up:
            busy = await self._oneshot_now(deps)
            if busy is not None:
                return await self._settle(
                    deps, f"a {busy} one-shot started; {service} was NOT started", restore, status
                )
            try:
                code = await deps.supervisor.toggle("start", service, status["id"])
            except SupervisorError as exc:
                return await self._rollback(
                    deps, target, restore, f"{service} did not start ({exc})", status
                )
            if code == 404:
                return await self._rollback(
                    deps,
                    target,
                    restore,
                    f"the {target} engine is not provisioned (404 on start)",
                    status,
                )
        if not await self._wait_for(deps, service, up=True):
            return await self._rollback(
                deps, target, restore, f"{service} did not report running", status
            )
        # It is what is up now: residency's engine gate and the config re-stamp before the load
        # below must follow the target, not the engine just stopped.
        await self._set_effective(deps, target)

        await self._stage(deps, status, LOADING)
        primary: str | None = None
        if deps.primary_model is not None:
            with contextlib.suppress(Exception):
                primary = await deps.primary_model()
        model = smoke_model(target, deps.local_models, primary)
        if model is None:
            status["notes"].append(f"no installed tool-capable {target} model — smoke test skipped")
        elif deps.gateway is None:
            status["notes"].append("no gateway client — smoke test skipped")
        else:
            status["model"] = model.served_model
            try:
                with box_events.because(f"smoke-testing the {engines.LABEL[target]} engine"):
                    await deps.gateway.load(model.served_model)
            except Exception as exc:  # noqa: BLE001 — every load failure is a rollback
                return await self._rollback(
                    deps, target, restore, f"{model.served_model} did not load ({exc})", status
                )
            await self._stage(deps, status, SMOKE)
            failure = await self._smoke(deps.gateway, model, status)
            if failure is not None:
                return await self._rollback(
                    deps, target, restore, f"smoke test failed: {failure}", status
                )

        await deps.store.set_llm_local_engine(deps.ctx, target)
        await self._set_effective(deps, target)
        return DONE, None

    async def _smoke(
        self, gateway: SwitchGateway, model: local_catalog.LocalModel, status: dict[str, Any]
    ) -> str | None:
        """Run the probes; the first failure's description, or None when all passed."""
        probes: list[tuple[str, Callable[[str], Awaitable[object]]]] = [
            ("text", gateway.text_probe),
            ("tool", gateway.tool_probe),
        ]
        if model.supports_vision:
            probes.append(("image", gateway.image_probe))
        for name, probe in probes:
            try:
                said = await probe(model.served_model)
            except Exception as exc:  # noqa: BLE001 — a probe failure is the verdict
                status["smoke"].append({"probe": name, "ok": False, "detail": str(exc)[:200]})
                return f"{name} probe: {exc}"
            detail = said if isinstance(said, str) else "ok"
            status["smoke"].append({"probe": name, "ok": True, "detail": detail})
        return None

    async def _rollback(
        self,
        deps: SwitchDeps,
        target: engines.Engine,
        restore: engines.Engine | None,
        why: str,
        status: dict[str, Any],
    ) -> tuple[str, str]:
        """Stop the target and restart `restore`, but only once the target is CONFIRMED down."""
        service = engines.SERVICE[target]
        # Through the client first, best-effort, so the ledger does not keep charging for a
        # model the stop is about to free (a half-loaded one included).
        with contextlib.suppress(Exception):
            await self._unload_resident(deps, f"rolling back the switch to {target}")
        if not await self._stop_confirmed(deps, service):
            return await self._settle(
                deps,
                f"{why}; {service} could not be confirmed stopped, so nothing was restored "
                "(starting the previous engine beside it could freeze the box)",
                None,
                status,
            )
        return await self._settle(deps, why, restore, status)

    async def _settle(
        self,
        deps: SwitchDeps,
        why: str,
        restore: engines.Engine | None,
        status: dict[str, Any],
    ) -> tuple[str, str]:
        """End a failed switch on what is ACTUALLY up, never on a stale belief.

        One engine up: record it as effective. Both up: restore nothing (that is the freeze
        state) and say so. None up: start `restore` if there is one and it may start (no
        one-shot), confirm it, record it — `rolled_back`; otherwise record the previous engine
        as effective (what the next update brings back) and say NO local engine is up."""
        try:
            states = await deps.supervisor.states()
        except SupervisorError:
            states = None
        if states is None:
            return FAILED, f"{why}; the supervisor could not be read, so what is up is unknown"
        up: list[engines.Engine] = [
            e for e in engines.ENGINES if engines.holds_memory(states.get(engines.SERVICE[e], ""))
        ]
        if len(up) == 1:
            await self._set_effective(deps, up[0], why)
            return FAILED, f"{why}; {up[0]} is serving"
        if len(up) > 1:
            return FAILED, (
                f"{why}; BOTH engines are up — nothing was started; switch again to stop one"
            )
        fallback: engines.Engine = restore or status["previous"]
        if restore is None and not status.get("was_up"):
            return ROLLED_BACK, f"{why}; no engine was running before, so none was put back"
        if restore is not None:
            busy = await self._oneshot_now(deps)
            started = False
            if busy is None:
                try:
                    code = await deps.supervisor.toggle(
                        "start", engines.SERVICE[restore], status["id"]
                    )
                    started = code == 202 and await self._wait_for(
                        deps, engines.SERVICE[restore], up=True
                    )
                except SupervisorError:
                    started = False
            if started:
                await self._set_effective(deps, restore, why)
                return ROLLED_BACK, f"{why}; {restore} was put back"
            why = f"{why}; {restore} could NOT be put back" + (
                f" (a {busy} one-shot is running)" if busy else ""
            )
        await self._set_effective(deps, fallback, f"{why}; no local engine is up")
        status["no_engine_up"] = True
        return FAILED, (
            f"{why}. NO local engine is up — local models are unavailable until you switch "
            f"again (or the next Ops → Update brings {fallback} back)"
        )

    async def _stop_confirmed(self, deps: SwitchDeps, service: str) -> bool:
        """Stop `service` and wait until /status reports it down; re-issue the stop once if it
        does not show. True when confirmed (or the container does not exist)."""
        for _ in range(2):
            try:
                if await deps.supervisor.toggle("stop", service) == 404:
                    return True
            except SupervisorError:
                pass
            if await self._wait_for(deps, service, up=False):
                return True
        return False

    async def _oneshot_now(self, deps: SwitchDeps) -> str | None:
        """A one-shot that started since preflight (an older supervisor without the hold
        cannot refuse it). Unreadable counts as busy: a start under an update is the freeze."""
        try:
            return await deps.supervisor.oneshot()
        except SupervisorError:
            return "unknown (supervisor unreadable)"

    async def _drain(self, deps: SwitchDeps, status: dict[str, Any], target: str) -> None:
        """Close admission, let other processes see it, then wait for the gateway to go idle."""
        row = drain.closed_row(
            f"switching to {target}", ttl_s=self.timings.admission_ttl_s, now=self._wall()
        )
        await deps.store.set_llm_local_admission(deps.ctx, row)
        self._admission_closed = True
        drain.invalidate_cached()
        await self._sleep(self.timings.gate_wait_s)
        deadline = self._clock() + self.timings.drain_s
        while not self._cancel_requested:
            busy = await self._in_flight(deps.gateway)
            if not busy:
                return
            if self._clock() >= deadline:
                status["notes"].append(
                    f"drain timed out after {self.timings.drain_s:.0f} s with "
                    f"{', '.join(busy)} still busy; proceeding"
                )
                return
            await self._sleep(self.timings.poll_s)

    async def _in_flight(self, gateway: SwitchGateway | None) -> list[str]:
        """Models the gateway is still working for — loading, or a slot mid-request. It sees the
        calls of EVERY process (api, worker, jcode), which no in-process counter could. A slot
        read that fails counts as busy: guessing "idle" is what cuts a call."""
        if gateway is None:
            return []
        states = await gateway.running_states()
        if not states:
            return []
        busy: list[str] = []
        for served, state in sorted(states.items()):
            if state and state != "ready":
                busy.append(served)
                continue
            try:
                slots = await gateway.slots(served)
            except Exception:  # noqa: BLE001
                busy.append(served)
                continue
            if any(isinstance(s, dict) and s.get("is_processing") for s in slots):
                busy.append(served)
        return busy

    async def _unload_resident(self, deps: SwitchDeps, why: str) -> None:
        """Unload through the client — the one chokepoint that discharges the reservation
        ledger and narrates to box events. An unreachable gateway holds nothing. A failure
        names the model and what was already released before it."""
        if deps.gateway is None:
            return
        states = await deps.gateway.running_states()
        released: list[str] = []
        with box_events.because(why):
            for served in sorted(states or {}):
                try:
                    await deps.gateway.unload(served)
                except Exception as exc:  # noqa: BLE001 — reported with what was released
                    raise UnloadFailed(served, exc, released) from exc
                released.append(served)

    async def _wait_for(self, deps: SwitchDeps, service: str, *, up: bool) -> bool:
        """Poll /status until `service` is up (or down). False on timeout or an unreadable
        supervisor — "returned" and "reported" are different claims and only the second
        licenses the next step."""
        deadline = self._clock() + self.timings.settle_s
        while True:
            try:
                state = (await deps.supervisor.states()).get(service, "missing")
            except SupervisorError:
                state = None
            if state is not None and engines.holds_memory(state) == up:
                return True
            if self._clock() >= deadline:
                return False
            await self._sleep(self.timings.poll_s)

    async def _settle_memory(self, deps: SwitchDeps, status: dict[str, Any]) -> None:
        """Wait until device memory stops falling after the stop, so the target's start is
        budgeted against memory that is actually free. Bounded; unreadable skips it."""
        if deps.memory_used_gb is None:
            return
        deadline = self._clock() + self.timings.memory_settle_s
        last = await deps.memory_used_gb()
        while last is not None:
            await self._sleep(self.timings.poll_s)
            now = await deps.memory_used_gb()
            if now is None or abs(now - last) < self.timings.memory_settled_gb:
                return
            if self._clock() >= deadline:
                status["notes"].append("device memory was still moving when the start went ahead")
                return
            last = now

    async def _set_effective(
        self, deps: SwitchDeps, engine: engines.Engine, reason: str | None = None
    ) -> None:
        await deps.store.set_llm_local_engine_effective(deps.ctx, engine, reason=reason)
        self._effective_written_at = self._clock()

    async def _await_engine_caches(self) -> None:
        """Hold admission closed until every process's cached effective engine has expired, so
        the first call after reopening is routed by the engine that is actually up — the
        worker sees a write here only through its ActiveEngine TTL."""
        if self._effective_written_at is None:
            return
        left = self._effective_written_at + self.timings.engine_cache_s - self._clock()
        if left > 0:
            await self._sleep(left)

    async def _open_admission(self, deps: SwitchDeps) -> None:
        self._admission_closed = False
        try:
            await deps.store.set_llm_local_admission(deps.ctx, dict(drain.OPEN_ROW))
        except Exception:  # noqa: BLE001 — the row's deadline reopens it if this write fails
            log.warning("engine_switch.reopen_failed", exc_info=True)
        drain.invalidate_cached()


def _log_task_failure(task: asyncio.Task[None]) -> None:
    """Observe the background task's outcome, so nothing it raised is silently dropped."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("engine_switch.task_failed", error=repr(exc))


async def reset_after_restart(store: SwitchStore, ctx: Any) -> None:
    """Boot: a switch cannot survive its process, so reopen admission and mark an unfinished
    switch as interrupted. Best-effort — the admission row's deadline is the backstop."""
    with contextlib.suppress(Exception):
        await store.set_llm_local_admission(ctx, dict(drain.OPEN_ROW))
    with contextlib.suppress(Exception):
        status = await store.llm_local_engine_switch(ctx)
        if status is not None and status.get("stage") not in TERMINAL:
            status.update(
                stage=FAILED,
                reason="interrupted: the api restarted mid-switch; read the engine state and "
                "switch again if it is not the one you want",
                ended_at=_now_iso(),
                updated_at=_now_iso(),
            )
            await store.set_llm_local_engine_switch(ctx, status)
