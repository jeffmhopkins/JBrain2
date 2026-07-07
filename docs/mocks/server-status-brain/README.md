# Server-brain status wall — mock round (3 variants)

> **Decided: Variant A (Synaptic Cortex).** Shipped as `deploy/wall/index.html`,
> the server-status wall display wired to live host vitals. B/C are kept below as
> the record.

Three directions for the ambient server-status visualization that runs on the
wall display — each animating the same host vitals (GPU/RAM/VRAM/power/net/disk)
plus reach-out tendrils for web/LLM tool activity. All three implement the shared
`window.ServerBrain` data contract, now recorded next to its implementation at
`deploy/wall/CONTRACT.md` (`serve.py`'s `snapshot()` returns it; see
`deploy/wall/README.md`).

## The variants

| | File | Direction |
|---|---|---|
| **A** | `variant-a-cortex.html` | **Synaptic Cortex** — 3D neural bloom, in-shader additive glow. The shipped wall. |
| **B** | `variant-b-mycelium.html` | **Mycelium** — organic filament network growing/pruning with load. |
| **C** | `variant-c-hud.html` | **HUD** — instrument-panel gauges + rim auras, most legible-at-a-glance. |
