# Math launcher live view — GUI-gate mocks

How a running job's **live folder** (progress, a 3D replay of the latest checkpoint, the
latest weights) shows on the job screen, and where the finished result downloads. Per
`docs/reference/PROCESS.md`'s GUI gate. Each mock simulates a GrowBot training run: tap
**Fast-forward training** and **Finish the run**.

| Variant | Idea | Trade-off |
|---|---|---|
| `a-overview-card.html` | A **Live card** at the top of Overview; the replay opens in a new tab; Result gains a Download button. | Smallest change, fits the existing tabs. One tap out to see the walk. |
| `b-live-tab.html` | A fourth **Live** tab, opened first while training. | Room for charts later; a fourth tab is tight on a phone. |
| `c-inline-replay.html` | The 3D replay **plays inside the card**. | Most glanceable; runs a 3D scene in the Launcher. |

Built: **A** (recommended; the owner can switch). `frontend/src/screens/JlaunchScreen.tsx`
(`LiveCard`) follows it.
