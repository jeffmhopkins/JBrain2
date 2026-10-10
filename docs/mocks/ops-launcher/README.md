# GUI mock — Ops launcher layout

> **Status:** Living · **Last verified:** 2026-10-10

`ops-launcher.html` is the clickable mock the owner reviewed for the Ops cleanup and the
reference for `frontend/src/screens/OpsScreen.tsx`. Open it in a browser; the numbers are
samples. The reasoning lives in `docs/reference/DESIGN.md` ("Ops screen", "Ops Engine page",
"Ops Host tile").

Two rounds, both 2026-10-10:

1. **Layout.** Live vitals, the one Update and the History graphs stay open on top; every
   other section moves behind a launcher-style tile that pushes its own page, the same
   pattern as the Settings grid. The update's engine switches (*Track newest llama.cpp*,
   *Fast Qwen loads*), the Standard / Flash-Next switch and the per-service Rebuild buttons
   are removed.
2. **Services.** A service stopped on purpose (opt-in, or the engine not chosen) reads grey
   **off** and never counts against its group, the banner or the tile. The services are
   re-sorted by purpose — Core, Models, Assistant tools, Devices, Apps, One-shot jobs —
   retiring AI - Optional, Infra, Display and the catch-all Other. The prompt-cache settings
   stay on the Engine page.

Supersedes `../ops-redesign/` (the stack of collapsible cards) and, for the engine, the
switch in `../engine-switch/`.
