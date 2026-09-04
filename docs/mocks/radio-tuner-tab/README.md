# Radio Tuner tab — mock round (preview)

**Preview artifacts for chat** — not yet committed to the JBrain2 repo.
Intended eventual home: `docs/mocks/radio-tuner-tab/`.

Three interactive rivals for the Radio launcher’s **Tuner** tab (full-screen surface under
`Tuner | APRS | Recordings`). Open each HTML file offline in a browser.

## Options

| File | Option | Idea |
|------|--------|------|
| `a-controls-column.html` | **A** | Controls-first vertical stack. Closest to `SdrTunerControls`. No waterfall (tiny stub). |
| `b-spectrum-stage.html` | **B** | Spectrum/waterfall owns upper ~45%; controls docked below. Click stage to nudge freq. |
| `c-dual-radio-cards.html` | **C** | Listen + APRS cards. 1-dongle contention on Listen; 2-dongle both live. |

## Harness states (all three)

1. **Idle** — no listen session; freq/mode + Listen
2. **Listening** — full tuner controls + instrument tape + LIVE/PAUSED
3. **APRS contention (1 dongle)** — hold + Release & listen handoff
4. **2-dongle both live** — listen usable while APRS logs

Theme toggle (dark/light) on every file. Record is ghosted “Coming next”.

## Decision

**TBD** — owner picks A/B/C before any app implementation (PROCESS GUI gate).
