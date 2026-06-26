"""Action preconditions: a gate the worker evaluates before running a job.

An `ActionSpec.precondition` names one of these. Before the worker runs the action's
handler it evaluates the named check; if the check is NOT met the job is deferred (a
fixed retry, no attempt burned — see `queue.defer`) rather than run. This is the
engine seam for "only run when X holds" without a handler having to fail-and-retry its
way there.

The one precondition shipped today is `model_already_loaded`: it keeps the inbox-
triage sweep from forcing a local model swap. When triage is routed to a local model,
the sweep should run only when that model is ALREADY resident in the llama-swap
gateway — otherwise the first classify call would load it and evict whatever the owner
is actively using (a code model, an image-editing session). A cloud route has nothing
to load or evict, so it is always met.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from jbrain.llm.local_gateway import LocalGateway
from jbrain.llm.router import LlmRouter

# How long a deferred job waits before the worker re-evaluates its precondition. Fixed
# (not the queue's exponential failure backoff): a precondition miss is "not yet", not
# a failure, so it retries on a steady cadence and can wait indefinitely. Five minutes
# is short enough to pick the model up soon after it loads, long enough not to spin.
RETRY_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class PreconditionResult:
    """Whether the gate is satisfied, plus a short reason when it is not (surfaced on
    the run's progress note and the deferred job's diagnostic)."""

    met: bool
    reason: str = ""


# A nullary check the worker awaits before running a job: met → run, unmet → defer.
Precondition = Callable[[], Awaitable[PreconditionResult]]


def model_already_loaded(
    router: LlmRouter, gateway: LocalGateway, *, task: str, strength: str | None = None
) -> Precondition:
    """A precondition that is met only when the model `task` resolves to is ALREADY
    resident in the local gateway — so a scheduled sweep never triggers a model swap.

    Resolves the LIVE route (folding in the operator's per-task override) exactly as the
    handler will, then:
      - a non-local route is always met — a cloud model loads nothing on the box, and
        the router already folds a `local:` override back to cloud when local hosting is
        off, so a disabled gateway reads as "nothing to gate" rather than "blocked";
      - a local route is met only when its served-model name is in `gateway.running()`
        (the same served name the router hands the gateway, and that `/running` reports).

    `gateway.running()` returns an empty set on any error, so an unreachable gateway
    reads as "not loaded" and the job defers rather than blindly forcing a load.
    """

    async def check() -> PreconditionResult:
        provider, model = await router.effective_spec(task, strength)
        if provider != "local":
            return PreconditionResult(met=True)
        if model in await gateway.running():
            return PreconditionResult(met=True)
        return PreconditionResult(met=False, reason=f"local model {model!r} not loaded")

    return check
