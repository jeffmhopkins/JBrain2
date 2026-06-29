"""In-memory ImageGen/MusicGen for tests — the only image/music-gen tests may call.

Returns a tiny but VALID 1x1 PNG (image) or minimal MP3 (music) — real magic bytes — so the
blob store + serving sniff path exercise the real code, and records the last spec for
assertions. No network, no ComfyUI (rule #5)."""

from __future__ import annotations

import base64
from collections.abc import Sequence

from jbrain.image_gen.comfyui import EditSpec, GenSpec, MusicSpec, OnProgress

# A known-good 1x1 truecolor PNG — real \x89PNG\r\n\x1a\n magic + IHDR/IDAT/IEND.
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# One MPEG-1 Layer III frame: sync 0xFFFB, 128 kbps, 44.1 kHz → a 417-byte frame. The 4-byte
# header then a zero-filled payload — a valid (silent) frame a sniffer/<audio> element accepts,
# the audio analogue of the 1x1 PNG above.
_MP3_SILENT = b"\xff\xfb\x90\x00" + b"\x00" * 413


def _png_with_dims(width: int, height: int) -> bytes:
    """A PNG whose IHDR declares (width, height) — the handlers read dims back from the
    header (not the pixels), so the fake's output carries the size a real render would."""
    return (
        _PNG_SIGNATURE
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


class FakeImageGen:
    """Records the last call and returns a PNG carrying the requested (or overridden)
    dimensions, so the handler's read-back-the-real-size path is exercised."""

    def __init__(self, out_dims: tuple[int, int] | None = None) -> None:
        # When set, generate/edit return a PNG of THESE dims regardless of the spec —
        # used to model an edit whose source-scaled output differs from the preset.
        self.out_dims = out_dims
        self.last_gen: GenSpec | None = None
        self.last_edit: EditSpec | None = None
        self.last_source: bytes | None = None
        # The full ordered image list of the last edit (primary first, then references) —
        # lets a multi-image test assert every source reached the adapter.
        self.last_sources: list[bytes] = []
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
        return _png_with_dims(*(self.out_dims or (spec.width, spec.height)))

    async def edit(
        self,
        spec: EditSpec,
        source: bytes,
        on_progress: OnProgress | None = None,
        *,
        extra_sources: Sequence[bytes] = (),
    ) -> bytes:
        self.last_edit = spec
        self.last_source = source
        self.last_sources = [source, *extra_sources]
        self._emit(on_progress, spec.steps)
        return _png_with_dims(*(self.out_dims or (spec.width, spec.height)))


class FakeMusicGen:
    """Records the last music call and returns minimal valid MP3 bytes. Audio sampling emits
    no preview frames, so the simulated ticks carry `None` (not a fake JPEG) — matching the
    real driver's audio path."""

    def __init__(self) -> None:
        self.last_spec: MusicSpec | None = None
        # (step, total, preview) ticks the last call emitted — preview is always None for audio.
        self.progress: list[tuple[int, int, bytes | None]] = []

    def _emit(self, on_progress: OnProgress | None, steps: int) -> None:
        if on_progress is None:
            return
        for step in (max(steps // 2, 1), steps):
            self.progress.append((step, steps, None))
            on_progress(step, steps, None)

    async def generate_music(self, spec: MusicSpec, on_progress: OnProgress | None = None) -> bytes:
        self.last_spec = spec
        self._emit(on_progress, spec.steps)
        return _MP3_SILENT
