"""Post-upgrade smoke test for the on-box model gateway.

Runs after the gateway is rebuilt on a NEWER llama.cpp base — the opt-in
`LOCAL_LLM_AUTO_UPDATE` path in `deploy/update-inner.sh`, which floats the
gateway on the rolling `:vulkan-radv` tag so a freshly-released model's
architecture is supported without a manual digest bump. Tracking master
reintroduces the risk the pinned digest exists to avoid (a `:vulkan-radv` build
once shipped a gpt-oss harmony tool-call grammar regression that crashed
tool-carrying turns), so the update path only KEEPS the new build if this smoke
test passes; on failure it rolls the gateway back to the pinned, known-good
base. That makes "track newest" safe: a bad upstream build never leaves the box
unable to serve.

Deliberately narrow — it proves the new binary can LOAD a model and survive a
tool-carrying turn, not that quality is unchanged:
  1. Load the smallest installed tool-capable model. A build that can't parse an
     architecture (the Nemotron-3.5 / hybrid-Mamba failure mode) crashes
     llama-server at load, which surfaces as a `LocalGatewayError` here.
  2. If gpt-oss is installed, run one tool-carrying probe against it — the exact
     surface the past regression broke.

It talks to the gateway's readiness surface (`LocalGatewayClient` — model load +
a discarded probe generation), NOT the LLM adapter: these are readiness probes,
the same seam `LocalGatewayClient.load`'s warm-up uses, deliberately outside the
adapter (see `local_gateway.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGatewayError

# gpt-oss is the served model whose tool-call path a rolling llama.cpp build
# regressed before (a harmony grammar segfault over the tool union). Probe it with
# a tool ONLY when it is installed; other boxes skip straight to the load check.
TOOL_PROBE_MODEL_ID = "gpt-oss-120b"


class SmokeGateway(Protocol):
    """The readiness surface the smoke test drives — a subset of LocalGatewayClient
    (plus `tool_probe`), so the concrete client and an in-memory fake both satisfy it."""

    async def load(self, served_model: str) -> None: ...

    async def tool_probe(self, served_model: str) -> None: ...


async def run_smoketest(
    local_models: Sequence[str], gateway: SmokeGateway
) -> tuple[bool, list[str]]:
    """Smoke-test the gateway's current build against the installed model set.

    Returns ``(ok, messages)``: ``ok`` True means the build loaded a model (and, when
    gpt-oss is installed, survived a tool-carrying turn) and is safe to keep; False is
    the update path's signal to roll back to the pinned base. ``messages`` is a short
    human-readable trace for the update log. Never raises — a gateway failure is a
    smoke FAILURE (return False), not an exception, so the caller's rollback always runs.
    """
    messages: list[str] = []

    installed = [m for m in local_catalog.selected(local_models) if m.supports_tools]
    if not installed:
        messages.append("no installed tool-capable models to smoke-test — treating as pass")
        return True, messages

    # Cheapest possible load: a build that can't run at all fails here without paying
    # to read tens of GB of a large model's weights.
    smallest = min(installed, key=lambda m: m.size_gb)
    try:
        await gateway.load(smallest.served_model)
        messages.append(f"load OK — {smallest.id} ({smallest.served_model})")
    except LocalGatewayError as exc:
        messages.append(f"load FAILED — {smallest.id} ({smallest.served_model}): {exc}")
        return False, messages

    probe = local_catalog.get(TOOL_PROBE_MODEL_ID)
    if probe is not None and TOOL_PROBE_MODEL_ID in set(local_models):
        try:
            await gateway.tool_probe(probe.served_model)
            messages.append(f"tool-call probe OK — {probe.id}")
        except LocalGatewayError as exc:
            messages.append(f"tool-call probe FAILED — {probe.id}: {exc}")
            return False, messages

    return True, messages
