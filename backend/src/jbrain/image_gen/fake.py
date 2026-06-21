"""In-memory ImageGen for tests — the only image-gen tests may call.

Returns a tiny but VALID 1x1 PNG (real magic bytes) so the blob store + serving
sniff path exercise the real code, and records the last spec for assertions. No
network, no ComfyUI (rule #5)."""

from __future__ import annotations

import base64

from jbrain.image_gen.comfyui import EditSpec, GenSpec, OnProgress

# A known-good 1x1 truecolor PNG — real \x89PNG\r\n\x1a\n magic + IHDR/IDAT/IEND.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)


class FakeImageGen:
    """Records the last call and returns a constant valid PNG."""

    def __init__(self) -> None:
        self.last_gen: GenSpec | None = None
        self.last_edit: EditSpec | None = None
        self.last_source: bytes | None = None
        # (step, total, preview) ticks the last call emitted — lets a test assert the
        # handler wired its progress callback through.
        self.progress: list[tuple[int, int, bytes | None]] = []

    def _emit(self, on_progress: OnProgress | None, steps: int) -> None:
        """Simulate a 50% then 100% tick (with a tiny preview) when a callback is given."""
        if on_progress is None:
            return
        for step in (max(steps // 2, 1), steps):
            self.progress.append((step, steps, _PNG_1X1))
            on_progress(step, steps, _PNG_1X1)

    async def generate(self, spec: GenSpec, on_progress: OnProgress | None = None) -> bytes:
        self.last_gen = spec
        self._emit(on_progress, spec.steps)
        return _PNG_1X1

    async def edit(
        self, spec: EditSpec, source: bytes, on_progress: OnProgress | None = None
    ) -> bytes:
        self.last_edit = spec
        self.last_source = source
        self._emit(on_progress, spec.steps)
        return _PNG_1X1
