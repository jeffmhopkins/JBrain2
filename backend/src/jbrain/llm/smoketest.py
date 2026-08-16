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

Both steps are gated on the box having ROOM for the weights (see
`LOAD_HEADROOM_GB`): step 2 loads gpt-oss-120b, ~60 GB on the reference box, and
a load with no headroom is what hard-froze this hardware.

It talks to the gateway's readiness surface (`LocalGatewayClient` — model load +
a discarded probe generation), NOT the LLM adapter: these are readiness probes,
the same seam `LocalGatewayClient.load`'s warm-up uses, deliberately outside the
adapter (see `local_gateway.py`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from jbrain.llm import local_catalog
from jbrain.llm.local_gateway import LocalGatewayError

if TYPE_CHECKING:
    from collections.abc import Sequence

# gpt-oss is the served model whose tool-call path a rolling llama.cpp build
# regressed before (a harmony grammar segfault over the tool union). Probe it with
# a tool ONLY when it is installed; other boxes skip straight to the load check.
TOOL_PROBE_MODEL_ID = "gpt-oss-120b"

# Kernel telemetry, not stored data: read directly rather than through the storage
# abstraction, the same way vitals sampling reads the amdgpu sysfs attributes.
MEMINFO_PATH = Path("/proc/meminfo")

# Free RAM a load needs BEYOND the model's own resident cost, in GB.
#
# On a unified-memory box (Strix Halo) a model's device buffers are pinned out of
# system RAM through the amdgpu GTT, and the driver asks for them with
# __GFP_RETRY_MAYFAIL — which tells the kernel to reclaim hard and then FAIL the
# allocation rather than invoke the OOM killer. So an over-tight load kills nothing,
# logs nothing to the app, and instead livelocks the box in reclaim: a hard freeze
# down to the USB keyboard, needing a power cycle. That happened repeatedly during
# updates, and the kernel trace named it precisely — llama-server failing an order:0
# (single 4 KB page) allocation inside amdgpu_ttm_tt_populate.
#
# So this test refuses a load it cannot afford. Rather than bet the host on a
# verification step, a box without the room keeps the pinned, known-good base — the
# same outcome as a failed smoke test, which is the conservative one.
#
# The number is the app's OWN steady-state floor, not a smaller one invented here. Every
# ordinary turn loads through `jbrain.llm.residency`, which keeps
# `local_llm_free_ram_fraction` (0.15) of RAM free — ~19.5 GB on this 130 GB box. It would
# be indefensible for the update path, the one that has actually frozen the hardware, to
# be the single loosest load path on the machine. A test pins the two together.
LOAD_HEADROOM_GB = 20.0


def mem_available_gb(meminfo_path: Path = MEMINFO_PATH) -> float | None:
    """Host MemAvailable in GB, or None when it can't be read.

    /proc/meminfo is not namespaced, so this is the HOST's number even from inside
    the api container the update runs this in.
    """
    try:
        text = meminfo_path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("MemAvailable:"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1]) / (1024 * 1024)
    return None


def _resident_cost_gb(model: local_catalog.LocalModel) -> float:
    """What holding `model` actually costs in unified memory, not just its weights.

    Weights alone understate it: the KV cache is a real, non-swappable allocation
    (`local_catalog.footprint_gb`), and on a 60 GB model it is several more GB. Counted
    at the catalog's 128k reference window with one slot — the same headline number the
    settings memory meter shows. A box serving a wider window or a second slot pays more
    than this, which is part of what LOAD_HEADROOM_GB is absorbing.
    """
    return model.size_gb + model.kv_gb_per_128k


async def _room_for(
    model: local_catalog.LocalModel,
    gateway: SmokeGateway,
    meminfo: Path,
    messages: list[str],
) -> bool:
    """True when this model can be loaded without leaving the box on the edge.

    A model the gateway ALREADY holds is free: gating it would refuse a load that
    allocates nothing. That is not a corner case — it is the common one. The keep-warm
    prime in the running api loads the chat model on its own, and on a box whose only
    tool-capable model is gpt-oss the smoke test's own first step loads the very model
    the tool probe then wants, so a weights-only check would refuse every update forever.
    """
    try:
        resident = await gateway.running()
    except Exception:  # noqa: BLE001 — an unreadable roster must gate, not crash
        resident = set()
    if model.served_model in resident:
        messages.append(f"{model.id} already resident — no allocation to gate")
        return True

    available_gb = mem_available_gb(meminfo)
    # An unknown MemAvailable proceeds with a note rather than blocking: on any real
    # deployment /proc/meminfo is readable, so "unknown" means an environment odd enough
    # that refusing every load would be the wrong default.
    if available_gb is None:
        return True
    cost = _resident_cost_gb(model)
    if available_gb >= cost + LOAD_HEADROOM_GB:
        return True
    messages.append(
        f"NOT ENOUGH MEMORY to load {model.id} safely — {available_gb:.0f} GB available, "
        f"{cost + LOAD_HEADROOM_GB:.0f} GB needed ({cost:.0f} GB resident + "
        f"{LOAD_HEADROOM_GB:.0f} GB headroom). REFUSED — the build was not tested, and "
        f"the gateway is being kept on its pinned base."
    )
    return False


class SmokeGateway(Protocol):
    """The readiness surface the smoke test drives — a subset of LocalGatewayClient
    (plus `tool_probe`), so the concrete client and an in-memory fake both satisfy it."""

    async def load(self, served_model: str) -> None: ...

    async def tool_probe(self, served_model: str) -> None: ...

    async def running(self) -> set[str]: ...


async def run_smoketest(
    local_models: Sequence[str],
    gateway: SmokeGateway,
    *,
    meminfo_path: Path | None = None,
) -> tuple[bool, list[str]]:
    """Smoke-test the gateway's current build against the installed model set.

    Returns ``(ok, messages)``: ``ok`` True means the build loaded a model (and, when
    gpt-oss is installed, survived a tool-carrying turn) and is safe to keep; False is
    the update path's signal to roll back to the pinned base. ``messages`` is a short
    human-readable trace for the update log. Never raises — a gateway failure is a
    smoke FAILURE (return False), not an exception, so the caller's rollback always runs.

    ``meminfo_path`` is injectable for tests only; None reads the real host. The
    free-memory reading is only as honest as the caller made it — the update drops the
    page cache first, because MemAvailable counts reclaimable cache as free and
    reclaiming it is the very thing that livelocks this hardware.
    """
    messages: list[str] = []
    meminfo = meminfo_path if meminfo_path is not None else MEMINFO_PATH

    installed = [m for m in local_catalog.selected(local_models) if m.supports_tools]
    if not installed:
        messages.append("no installed tool-capable models to smoke-test — treating as pass")
        return True, messages

    available = mem_available_gb(meminfo)
    if available is None:
        messages.append("MemAvailable unreadable — proceeding without the headroom check")
    else:
        messages.append(f"{available:.0f} GB available before the load")

    # Cheapest possible load: a build that can't run at all fails here without paying
    # to read tens of GB of a large model's weights.
    smallest = min(installed, key=lambda m: m.size_gb)
    if not await _room_for(smallest, gateway, meminfo, messages):
        return False, messages
    try:
        await gateway.load(smallest.served_model)
        messages.append(f"load OK — {smallest.id} ({smallest.served_model})")
    except LocalGatewayError as exc:
        messages.append(f"load FAILED — {smallest.id} ({smallest.served_model}): {exc}")
        return False, messages

    probe = local_catalog.get(TOOL_PROBE_MODEL_ID)
    if probe is not None and TOOL_PROBE_MODEL_ID in set(local_models):
        # The expensive one — gpt-oss-120b is ~60 GB, and it is THIS load, not the cheap
        # one above, that the kernel trace caught freezing the box. `_room_for` re-reads
        # MemAvailable rather than reusing the number above, because the first load just
        # took its own weights out of the total.
        if not await _room_for(probe, gateway, meminfo, messages):
            return False, messages
        try:
            await gateway.tool_probe(probe.served_model)
            messages.append(f"tool-call probe OK — {probe.id}")
        except LocalGatewayError as exc:
            messages.append(f"tool-call probe FAILED — {probe.id}: {exc}")
            return False, messages

    return True, messages
