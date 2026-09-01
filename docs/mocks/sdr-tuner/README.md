# SDR tuner — the tuned-station control on the omnibox (GUI-gate mockups)

> **Status:** Living · **Last verified:** 2026-09-01

The omnibox grows a **radio icon to the left of the attach clip**, shown *only*
while a tool holds the SDR lease. Tapping it opens a control surface for the
tuned station: frequency, mode, signal, squelch, playback, record, and release.
These three mockups are the `docs/reference/PROCESS.md` GUI gate for that surface;
the feature is specified in `../../proposed/SDR_RADIO_PLAN.md`.

The load-bearing idea all three share: **the icon is the lease.** The radio has
one tuner, so exactly one surface may hold it. Icon present ⇒ this session holds
the radio; icon absent ⇒ it is idle or held elsewhere. Releasing from the control
surface makes the icon disappear. That collapses two concepts (an invisible device
lease, and a visible "radio is on" indicator) into one thing the owner can see.

All three are single-file and fully offline, dark-first with a working light/dark
toggle, phone-framed (390×820), tokens-only (no raw hex outside the token sheet),
Lucide-style outline icons, ≥44px targets, `prefers-reduced-motion` honored. Each
carries a deck above the phone that simulates the `sdr_listen` tool call granting
the lease, so the appear/disappear behaviour is directly clickable. All three run
the same fixture — **162.550 MHz NFM, NOAA Weather Radio, Melbourne FL, −58 dBm** —
so they compare like-for-like.

## Variants

| Variant | File | Thesis | Interaction / trade-off |
|---|---|---|---|
| **A** | `a-tuner-sheet.html` | *The radio is a place you go.* | Standard bottom sheet over a scrim — the same shell as the "Conversation model" picker. Every control fits one uncramped column. Costs the conversation: the scrim hides it, and each glance is an open/dismiss cycle. |
| **B** | `b-tuner-popover.html` | *A glance, not a destination.* | Small card anchored above the icon, no scrim, light-dismiss. The chat stays readable and scrollable. Cannot hold everything — squelch, gain and presets defer to the Radio launcher — and it is a **new primitive** the codebase does not have. |
| **C** | `c-tuner-dock.html` | *The radio is on, and you can see it.* | No overlay: the composer grows upward into a tuner strip, collapsed to one line, expanded in place by the icon. Zero taps to read state; tuning and typing coexist. Permanently spends 44–210px of transcript height and makes the densest region denser. |

## Trade-offs

- **A** is the cheapest to build and the most conventional — it composes the
  existing `<Sheet>` (`frontend/src/components/Sheet.tsx`), whose header comment
  states that *"bespoke modals are a design-doc violation."* It is also the only
  one with room for the full control set today and room to grow tomorrow. But it
  treats a glance and a deep edit as the same weight of interaction, and a radio
  you are half-listening to invites glances.
- **B** matches the *frequency* of real use best — most radio interactions are
  "still on 162.550? signal holding?" — and it is the only one that keeps the
  conversation visible, which matters because the radio was turned on *by* that
  conversation. It pays for that with a capability ceiling and a genuinely new
  anchored-popover primitive to build, position, and test on small screens.
- **C** is the only one that makes a held lease impossible to forget, which is
  the failure mode worth designing against: a forgotten lease is what makes a
  later sweep report "radio busy" for no visible reason. It is also the furthest
  from anything in the codebase — a new composer region with its own
  collapsed/expanded states — and the most expensive in vertical space, which on
  a phone is the scarcest resource the app has.

A cross-cutting note for whichever wins: **A and B are compatible with the icon
being a pure opener; C reassigns the icon to expand/collapse.** If C is chosen,
the icon's meaning shifts from "open the tuner" to "show me more of the tuner
that is already visible" — which is a weaker justification for the icon existing
at all, and worth deciding deliberately rather than inheriting.

## Try them

- Open any file and press **▶ Tool tunes the radio** — that is the `sdr_listen`
  call taking the lease. Watch the radio icon appear in the omnibox foot.
- Tap the radio icon. Tune with ± (25 kHz steps; 162.400, 146.940 and 118.700
  are labelled stations), switch mode, drag squelch, play/pause.
- Tap **Record** twice — it is arm-then-confirm, per the destructive-action
  doctrine in `../../reference/DESIGN.md`.
- Press **Release** (or the deck's **Release lease**) and watch the icon vanish.

## Decision

> **Decided (owner, 2026-09-01): A — `a-tuner-sheet.html`.** It is now the binding
> spec and carries a BINDING SPEC header naming the component contract. B and C are
> retained here as the record.

Why A: it composes the shared `<Sheet>` rather than introducing a primitive
(`Sheet.tsx`'s own header calls bespoke modals a design-doc violation), it inherits
all five of that shell's dismiss paths for free, and it is the only variant with
room for the full control set today *and* room for what the tuner will grow —
gain, band presets, a jump to the recording just made. C's argument (a held lease
should be impossible to forget) is real but is answered more cheaply by the icon's
own live dot than by spending 44–210px of transcript height on every session.

The consequence to carry forward: **the icon stays a pure opener.** A keeps the
icon's meaning single — "open the tuner" — which is what makes it legible as the
lease indicator. The cross-cutting note above about C reassigning the icon is moot.

Still to gate: the **Radio launcher** (Spectrum + Recordings tabs) needs its own
mock round; this decision covers the omnibox tuner only. The chosen pattern's
reasoning lands in `../../reference/DESIGN.md` in the implementing PR, per that
doc's UI-development process.
