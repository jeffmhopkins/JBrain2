# Research report expiry — GUI mocks (W4)

Three interactive mocks for surfacing a report's opt-in TTL in the Research Library and
letting the owner **keep** a report (clear its expiry). Backs `REPORT_EXPIRY_PLAN.md` W4
and the `docs/reference/DESIGN.md` GUI gate (three mocks, owner chooses, the chosen one is
the binding spec). Open each in a browser; all three are self-contained and theme-toggleable.

The data model is already shipped (W1–W3): a report may carry `expires_at` (NULL = keep
forever). These mocks only differ in **how the countdown is shown** and **how Keep is
reached** — the behavior (a temporary report auto-deletes on its date; Keep clears the TTL)
is identical across all three.

| Mock | Countdown | Keep affordance | Feel |
| --- | --- | --- | --- |
| `a-quiet-footer.html` | a muted amber clause appended to the existing footer line ("· expires in 6 days"), rose in the last day | **Keep this report** in the existing ⋯ action sheet | understated; least new chrome, most consistent with the current restrained library |
| `b-pill-and-pin.html` | an amber pill in the chip row ("6d left"), rose in the last day | a **Keep** pin button on the card — one tap, no sheet | prominent, fast; a persistent button on every ephemeral card |
| `c-urgency-strip.html` | a full-width tinted strip on the card foot, escalating amber→rose | inline **Keep it** link in the strip | expressive; the strip visually separates ephemeral from permanent reports |

## Chosen: **A — quiet footer** (binding spec)

Rationale (owner authorized proceeding without a blocking review):
- The Research Library is a **read-only, restrained, amber-accented** surface; per-report
  actions already live in the ONE consolidated ⋯ sheet (view / rename / move / share / copy
  / download / delete). Adding **Keep this report** there keeps that single-home pattern
  intact rather than sprouting a second on-card action button (B) or a new full-width band
  on every ephemeral row (C).
- The footer already reads "research · {date}"; appending "· expires in N days" (rose in the
  final day, borrowed from C's escalation) is the minimum legible signal and needs no new
  card layout.
- B's on-card pin and C's strip are stronger *nudges*; they're worth revisiting if the owner
  finds expiring reports slip away unnoticed, but the quiet footer is the right default for a
  library you mostly browse, not act on.

If the owner prefers a louder nudge, B or C can be swapped in — the backend (an `expires_at`
field on the report row + a "keep" endpoint that nulls it) is identical for all three.
