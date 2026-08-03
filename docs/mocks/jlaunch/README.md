# Math launcher (jlaunch) — GUI-gate mocks

Three interactive variants of the **job screen** the `Math` launcher tile opens — the
decision-critical surface: start/stop/kill, the live terminal, and the finished-run
headline + public sharelink. Per `docs/reference/PROCESS.md`'s GUI gate, the owner picks
one before the frontend is built; the chosen mock becomes the binding spec for
`frontend/src/screens/JlaunchScreen.tsx` (see `docs/plans/JLAUNCH_PLAN.md`, W4).

All three share the design-system tokens and simulate a run (click **Start**): phases
advance, the terminal streams, then the result + sharelink appear. Stop/Kill end it.

| Variant | Idea | Trade-off |
|---|---|---|
| `a-console-first.html` | The **live terminal is the hero** (big xterm); controls in a slim toolbar, phase chips above. | Most "watch the shell" — closest to a real terminal; progress is a thin strip. |
| `b-dashboard.html` | A **progress ring + phase checklist** is the hero; the terminal is a drawer. | Best glanceable status for a multi-hour run from a phone; terminal is one tap away. |
| `c-tabbed.html` | **Overview · Terminal · Result** tabs, matching the jcode session screen. | Most consistent with jcode (one family); each surface gets full room, but needs a tap to switch. |

The public results page (`/results/{token}`) — headline block + machine specs + download —
is represented inline in each as the "Result / share" section; the standalone shell-less
page is built to match the chosen variant's result styling.
