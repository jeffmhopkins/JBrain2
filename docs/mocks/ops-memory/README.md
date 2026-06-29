# Ops · System-memory card mockups

Three interactive takes on an **expandable "System memory" card** for the Ops
screen, each driven by `GET /api/debug/host` (the supervisor-proxied per-process
RSS readout: host totals + per-container memory + per-process RSS via
`docker top`). All three are self-contained HTML — open in a browser, everything
is clickable, numbers are faked to mirror the real box (the two `llama-server`
models dominate, with a ComfyUI render co-resident).

Every direction has the three required parts: an **expandable card**, a
**Refresh** control (re-polls, animates), a **table**, and a **graph**.

| File | Graph | Idea |
|------|-------|------|
| `ops-memory-a-stacked-table.html` | stacked bar + donut | Glance line (94% + colored stack); expand to a **sortable per-process table** with sparkbars; toggle a composition **donut**. The conservative, information-dense default. |
| `ops-memory-b-treemap.html` | treemap | Expand to a **treemap** where each process is a block **sized by RSS** — the 120B visually owns the box. Toggle to a table for exact figures. The most legible "where did the RAM go". |
| `ops-memory-c-timeline.html` | stacked area over time | Leads with **history** — a scrubable stacked-area of the top consumers; legend chips mute series; the table shows each process **now vs 30m ago** to catch a leak. Best for "is it growing / what spiked". |

Shared design notes:
- Tokens lifted from `docs/mocks/ops-redesign/` (same dark surface, group colors:
  violet = AI/models, steel = core, teal = code mode, amber = infra).
- Every direction shows an **honest "kernel & page cache" slice** — the host
  total minus what `docker stats`/`docker top` can attribute to a container.
- Processes are grouped under their container, with the two co-resident
  `llama-server` models broken out (the whole reason per-process beats
  per-container here).

Pick one (or a hybrid) and it becomes a real card in `OpsScreen` reading the
new `/api/debug/host` route. Backend + supervisor + debug-activity logging for
that route already shipped on this branch.
