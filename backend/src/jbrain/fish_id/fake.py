"""In-memory FishIdentifier for tests — the only fish-id impl tests may call.

Records the last call and returns a canned ranked result, so the handler's
top-species / view / unload path is exercised with no network and no GPU (rule #5)."""

from __future__ import annotations

from jbrain.fish_id.client import DEFAULT_TOP_K, FishIdentifier, FishMatch, FishResult

# A plausible ranked default (a confident top match plus a runner-up) so a handler
# test sees both the verdict and the "also considered" path.
_DEFAULT = FishResult(
    (
        FishMatch(species="Zebrasoma flavescens", common_name="Yellow tang", score=0.92),
        FishMatch(species="Acanthurus coeruleus", common_name="Blue tang", score=0.05),
    )
)


class FakeFishIdentifier(FishIdentifier):
    """Returns a fixed (or injected) result and records the last image + top_k."""

    def __init__(self, result: FishResult = _DEFAULT) -> None:
        self.result = result
        self.last_image: bytes | None = None
        self.last_top_k: int | None = None
        self.calls = 0

    async def identify(self, image: bytes, top_k: int = DEFAULT_TOP_K) -> FishResult:
        self.calls += 1
        self.last_image = image
        self.last_top_k = top_k
        return self.result
