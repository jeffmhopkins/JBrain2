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

## The waveform display (second gate, 2026-09-02)

A separate GUI gate over the same sheet: what to show while audio is playing.
`d-waveform-tape.html` holds all three variants live, driven by a real
`AnalyserNode` so the shapes are the actual data rather than a canned animation.

| Variant | Thesis | Trade-off |
|---|---|---|
| **A · Scope** | *Show the audio itself.* Time-domain trace; speech reads as syllable bursts, static fills edge to edge, a dead channel is flat. | The most honest and the loudest failure signal — but it only ever shows the present instant, and scanner traffic is bursty. |
| **B · Spectrum** | *Show what kind of signal this is.* Frequency bars; voice piles into the low-mid, music spreads, static is flat. | Answers the tuning question directly and will sit naturally beside the waterfall — but it is the least literal, and overlaps the level meter's job. |
| **C · Tape** | *Show what just happened.* Loudness over the last 12 s, scrolling right to left. | The only one with a memory, which is the shape of the question on a voice channel — at the cost of texture: voice and music look alike. |

> **Decided (owner, 2026-09-02): C — the rolling tape**, with the explicit
> constraint *"make sure it's not vertically taking up too much space"*.

Why C: scanner traffic arrives in bursts, so the question being asked is almost
always "did anything just come through", and A and B can only answer it if you
happen to be looking at that exact moment. The owner watches this sheet while doing
something else; a display with no memory is a display they will miss.

How the height constraint is met: the tape rides **inside the transport row**,
between the play button and the LIVE/PAUSED tag, taking the slack width. It is 38px
inside a row already 46px tall, so the display costs **no vertical space of its
own**. It also draws only while the sheet is mounted — a closed tuner spends
nothing, and the tape fills in from the right over its first 12 seconds rather than
pretending to remember what happened while nobody was watching.

## The transport layout (third gate, 2026-09-02)

After the tape shipped, the sheet had three separate bands of status — a `SIGNAL`
header with bars and a percentage, an elapsed time, and a transport row — and the
tape was too short to read. `e-transport-instrument-face.html` holds three
rearrangements, all with a double-height tape that keeps recording while the sheet
is closed.

| Layout | Thesis | Trade-off |
|---|---|---|
| **A · Meter rail** | Play on the left, one thin line of readings above the tape. | Reads in the order the questions come — but keeps every number, including one that was lying. |
| **B · Instrument face** | The tape is the panel; readings sit inset on it. | Cheapest on height, so the tape gets the most of it — at the cost of putting text over a live drawing. |
| **C · Split by meaning** | Signal and elapsed move up into the station card; the transport row becomes purely audio. | The tidiest grouping — but it preserves the signal meter, and the signal meter was the problem. |

> **Decided (owner, 2026-09-02): B — the instrument face.** With it: *"remove the
> signal then altogether, the waveform covers it."*

**The signal meter is deleted, not relabelled.** The owner asked whether it showed
reception strength or volume after decode. It was the latter: `listen.py` measures
`peak` on rtl_fm's demodulated audio output. That makes it the same quantity the tape
already draws, with less detail and no history — and worse, backwards on a dead FM
channel, where rtl_fm's unsquelched hiss drove it high on nothing at all. Real RSSI
is not reachable while listening (see the mock's header), so a meter that could be
trusted was never on the table; removing it is the honest move.

Elapsed time survives, inset on the tape, because it answers a question nothing else
does: how long this session has been holding the one tuner.

