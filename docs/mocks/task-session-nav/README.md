# Task → session navigation — mock review

Three interactive mockups for the Tasks screen, exploring two linked needs:

1. **One tap to the latest session.** Today you must expand a card, scan the
   "Recent runs → sessions" list, compare timestamps to find the newest, then
   tap it. We want the latest run's session reachable in a single tap from the
   collapsed card.
2. **At-a-glance recognition of unviewed results.** A launcher-level badge
   already counts runs since Tasks was last opened (one global `seenAt`
   timestamp, `TASKS_SEEN_KEY`), but *inside* the screen nothing tells you
   which task has a fresh result you haven't read. The mockups introduce a
   per-task **unviewed** signal (steel = info/notification, deliberately
   distinct from the green health dot already on the card).

All three share the same chrome, tokens, and sample data (Email overview /
News both unviewed; Weekly finance sweep already viewed as the control) so the
directions compare cleanly. Each opens in a browser with zero network.

| File | Direction | Thesis |
|---|---|---|
| `a-inline-latest-line.html` | **A — inline latest-run line** | Smallest change: the collapsed card grows one always-visible tappable line (latest run summary + `N turns · ago ›`) that opens its session in one tap. Unviewed = a steel pip on the name + a steel hairline on the line; opening clears it. |
| `b-unread-inbox.html` | **B — unread-inbox reframe** | Treat results as an inbox: a `New · All` segmented control with count pills, unread tasks sorted to top with a preview line; tapping an unread card body opens its session. "Mark all read" clears the lot. |
| `c-result-band.html` | **C — two-zone card + result band** | Restructure the card into a config header + a docked, full-width **result band** (a mini session row). The band is the primary CTA; unviewed gets a steel left-edge bar + "NEW" pill, relaxing once read. |

Reflects the live data shape: each run carries `session_id`, `summary`,
`step_count` (turns), `started_at`, `status` (`TaskRun` in
`frontend/src/api/client.ts`). The chosen pattern and reasoning get written
into `docs/DESIGN.md` per the binding mock-first UI process.
