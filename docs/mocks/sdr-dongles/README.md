# Radios in Settings — name, describe, dedicate

> **Status:** Living · **Last verified:** 2026-09-04

The GUI round for **P0b** (`../../plans/APRS_CONTROL_PLAN.md`): telling two RTL-SDR
dongles apart, and saying what each one is for.

**Awaiting the owner's confirmation.** `a-named-roles.html` is a proposal, not yet the
binding spec.

## Why the round exists

Measured 2026-09-03. A second NESDR SMArt v5 is attached and the probe sees both
(`09022796` on bus 1-1, `77192819` on bus 3-4, both unclaimed). But `rtl_fm` and
`rtl_power` are invoked with **no `-d`**, so they open whichever librtlsdr enumerates
first. With one radio on a desk whip and one on a long wire, APRS can move to the wrong
antenna on a re-plug, and the only symptom is worse reception. Nothing in the PWA can
say which radio is doing what, or pin it.

## The shape, and how it was arrived at

Not chosen from three rivals. Two drafts were built — a job-per-row assignment table,
and device cards with jobs moved into them — and **both were discarded** when the owner
gave the shape directly:

> "in settings, we should be able to name the dongles, and add a description, and then
> assign if it's general use or dedicated to APRS (or other service that we create
> later) … this way I can change out antenna or dedicate one to APRS and one to
> shortwave etc"

Both drafts organised around **jobs** ("what is APRS using?"). The owner's model is the
**radio and what it is for**, because the thing that physically changes is the antenna.

Three fields per radio, all keyed to the **serial** so they survive a re-plug — the
measured failure being that one radio's node moved `001/005 → 001/011` across a single
re-plug tonight while its identity should not have:

| field | why it is load-bearing |
|---|---|
| **Name** | `77192819` is not a physical fact. Nobody knows which is which. |
| **Description** | The antenna is what changes. "Long wire via 9:1 unun, north window" is what makes a reading interpretable six months later. |
| **Used for** | General use, or dedicated to one service. |

The service list is **data, not a hardcoded toggle**, so services added later drop in.
Shortwave listening is rendered as the owner's own example and marked *not built yet* —
HF needs the direct-sampling work `SDR_RADIO_PLAN.md` §9 records as missing.

## Decided in the mock

**A dedicated radio does not fall back.** Unplug the radio a service is dedicated to and
that service reports *waiting*, rather than quietly taking a general one. Falling back is
exactly the failure this screen exists to stop.

## Open for the owner

- **Two radios dedicated to the same service** — rendered as an error (every frame logged
  twice), but it could equally be a standby.

## Explicitly not decided here

Whether two services may run **simultaneously** on two radios. That is the rest of P0b —
one session per device in the sidecar — and is not a GUI question. The mock deliberately
keeps today's one-at-a-time contention visible (with one general radio left, the tuner
and sweeps still take turns) so the surface cannot promise what the sidecar does not yet
do.

## The harness is the argument

Learned from round 3 of the APRS mocks, whose first cut was rejected because "the dongle
count is a cosmetic filter on copy, not a second axis of the state model". So the
harness row really unplugs radios, and *What will actually happen* really recomputes.
Nothing is asserted in prose that a control does not draw.
