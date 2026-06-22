# GUI gate #2 — fish-model settings (Fish-ID Wave F5)

Three interactive mocks of the **fish-identification service in Settings → On-box
models** — where the F4 backend (`GET /api/settings/fish`: service status, the
catalog, and start/stop/free) is surfaced (`docs/PROCESS.md` GUI gate;
`docs/FISH_ID_PLAN.md` "Wave F5"). **Pick one**; the chosen mock becomes the binding
spec for the `FishServiceSection` in `LLMSettingsScreen.tsx`, and the other two are
retained here as the record (mirrors the `image-models/README` convention).

These mirror the image-model settings, which landed on the **unified "On-box models"
drawer** (`docs/mocks/image-models/`, variant B): one shared 128 GB memory bar with
LLM + image subsections. All three read the same real data — fishial reachable
(`/health`), the catalog (arch / species / on-disk / footprint estimate), and the
owner-only **start / stop / free** actions. Provisioning the weights stays the on-box
`fish-id-setup.sh` step; these surfaces are status + runtime control, not a downloader.

**The defining difference from the image/LLM models:** the fish model is **load → use
→ unload per identification** — it is *not* kept resident, and the gateway reports only
`reachable` + a transient `loaded` flag (no steady-state footprint). The three
variants differ mainly in how honestly they represent that.

| File | Direction | Idea | Trade-off |
|---|---|---|---|
| `fish-models-a-transient-row.html` | **Transient row** (recommended) | A third subsection in the unified drawer; a **dashed "transient" segment** fills the memory bar only *while identifying*, then frees. Service Start/Stop + Free; no persistent load/unload toggle. | Most honest to load/use/unload; one extra bar state to build. A "Simulate identify" button shows the pulse. |
| `fish-models-b-resident-row.html` | **Resident row** | Treat fish exactly like the image model — a solid teal segment + Start/Stop/Free row. Near-copy of `ImageServiceSection`. | Most consistent + least code; the solid segment overstates the steady-state footprint (fish isn't normally resident). |
| `fish-models-c-service-card.html` | **Service card** | A standalone appliance card: a footprint ring + big Start/Stop + a "loads on demand" note, set apart from the shared bar. | Clearest as a "device"; diverges most from the drawer styling and separates fish from the unified-budget picture. |

## Trade-offs

- **A** tells the truth about the memory model — the fish model only draws unified
  memory in a brief pulse per identification, so a transient (dashed) segment that
  appears and frees is the accurate picture. It costs one extra bar state and drops
  the load/unload toggle that doesn't apply.
- **B** is the lowest-risk build (a direct copy of the shipped image section) and the
  most visually consistent, but a steady solid segment implies the model sits resident
  like the LLM/image, which it doesn't.
- **C** reframes fish as its own appliance, which reads clearly, but it abandons the
  "one shared 128 GB budget" picture the image settings deliberately chose, and is the
  most new styling to build and maintain.

## Decision

_Pending owner selection._ Once chosen, this records the pick + rationale, the chosen
file becomes the binding spec for the `FishServiceSection` + its styles in
`frontend/src/screens/LLMSettingsScreen.tsx`, and the selection lands in
`docs/DESIGN.md` (the On-box models drawer entry).
