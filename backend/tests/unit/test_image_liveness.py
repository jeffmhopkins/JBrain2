"""Unit tests for ImageGenLiveness (image_gen/liveness.py).

The cache decides which image tools a jerv turn may see. We pin: optimistic before
the first probe, hides only the GEN pair when ComfyUI is down (analyze_image stays),
caches within the TTL (one probe), re-probes after it, and — hysteresis — hides only
after a SUSTAINED outage, because flipping the tool array reshapes the prompt's
leading tokens and costs a full KV re-prefill (observed ~170 s on qwen, 2026-08-24).
Fake clock + call-counting gateway: no real HTTP, no wall-clock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jbrain.image_gen.liveness import ImageGenLiveness


@dataclass
class _Status:
    reachable: bool


class _FakeGateway:
    def __init__(self, reachable: bool):
        self.reachable = reachable
        self.calls = 0

    async def status(self) -> _Status:
        self.calls += 1
        return _Status(self.reachable)


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_hides_only_the_gen_pair_after_a_sustained_outage() -> None:
    gw = _FakeGateway(reachable=False)
    clock = _Clock()
    live = ImageGenLiveness(gw, ttl_s=30.0, hide_after_s=600.0, clock=clock)

    async def scenario() -> None:
        # Freshly down: inside the hysteresis window the prompt stays stable.
        assert await live.hidden_tools() == frozenset()
        # Still down past the window: NOW the pair hides.
        clock.t += 601.0
        hidden = await live.hidden_tools()
        assert hidden == {"generate_image", "edit_image"}
        assert "analyze_image" not in hidden  # a read that degrades to OCR — always kept

    asyncio.run(scenario())


def test_a_flap_never_reshapes_the_prompt() -> None:
    """Down for one probe, back up on the next: the tools never hid, so the rendered
    tool array — and every KV prefix built on it — stayed byte-stable throughout."""
    gw = _FakeGateway(reachable=False)
    clock = _Clock()
    live = ImageGenLiveness(gw, ttl_s=30.0, hide_after_s=600.0, clock=clock)

    async def scenario() -> None:
        assert await live.hidden_tools() == frozenset()  # down, inside the window
        clock.t += 31.0
        gw.reachable = True
        assert await live.hidden_tools() == frozenset()  # recovered — down-run reset
        clock.t += 601.0
        assert await live.hidden_tools() == frozenset()  # reachable: never hides

    asyncio.run(scenario())


def test_hides_nothing_when_reachable() -> None:
    live = ImageGenLiveness(_FakeGateway(reachable=True), ttl_s=30.0, clock=_Clock())
    assert asyncio.run(live.hidden_tools()) == frozenset()


def test_caches_within_ttl_then_reprobes_after_it() -> None:
    gw = _FakeGateway(reachable=True)
    clock = _Clock()
    live = ImageGenLiveness(gw, ttl_s=30.0, clock=clock)

    async def scenario() -> None:
        await live.hidden_tools()
        await live.hidden_tools()
        assert gw.calls == 1  # second call is inside the TTL — no re-probe
        clock.t += 31.0
        gw.reachable = False
        hidden = await live.hidden_tools()
        assert gw.calls == 2  # TTL elapsed → one fresh probe
        # Just-down: the hysteresis window holds the tools visible; the down-run
        # started at this probe.
        assert hidden == frozenset()
        clock.t += 601.0
        assert await live.hidden_tools() == {"generate_image", "edit_image"}

    asyncio.run(scenario())


def test_is_optimistic_before_the_first_probe_resolves() -> None:
    # A brand-new cache reports reachable (hides nothing) until a probe has run — a
    # slow first probe must never hide a healthy server. Verified via the internal flag.
    live = ImageGenLiveness(_FakeGateway(reachable=False), clock=_Clock())
    assert live._reachable is True  # noqa: SLF001 — asserting the documented cold-start posture
