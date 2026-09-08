# Tuning by channel on the bands that have channels

> **Status:** Living · **Last verified:** 2026-09-07

**The ask, verbatim:** "common bands like CB, FM radio, AM stations, walkie-talkies /
these all have common band plans / we need to figure out how to make the tuner able to
select between those when we're in those bands / and also maybe show the peaks as some
kind of representation of those bands".

**Why now.** It arrived as the *other route* around peak detection. The held-peak list
is genuinely bad — on 31 m the whole band spans 10 dB with 0.2 dB of headroom, on CB it
spans 5 dB and finds nothing while carriers are plainly visible — and the peaks that do
survive never expire. But on a channelised band the question "where is the signal?" has
already been answered by the FCC: CB has forty channels at known frequencies, FM has a
raster of odd tenths, FRS/GMRS has twenty-two. **On those bands a channel plan beats any
detector**, because it is a list of the only places a signal is allowed to be. The
detector's job shrinks from *find the peaks* to *say which known channels are busy* —
which is a far easier measurement, and one that degrades honestly.

## What the data actually looks like

The three plans in the mocks are real, from 47 CFR, because a mock carrying placeholder
channels cannot show what a shape does with the awkward parts:

- **CB (47 CFR 95.567)** — forty channels, and the raster is *not* uniform. There are
  five gaps where the R/C radio-control channels sit (26.995, 27.045, 27.095, 27.145,
  27.195), and **channel 23 sits at 27.255, above 24 and 25**, an artefact of the 1977
  expansion. Arithmetic on a step size gets this wrong; a table does not. Channels 36–40
  are the SSB segment by convention, so **the mode is part of the channel's identity** —
  27.385 demodulated as AM is unintelligible.
- **FM broadcast (47 CFR 73.201)** — 87.9 + 0.2 n. Every legal carrier is an **odd
  tenth**; snapping to the nearest 100 kHz invents "89.0 FM", which cannot exist. It is
  also the band where the receiver **hops**, so it is the one that forces every shape to
  answer the honest question below.
- **FRS/GMRS (47 CFR 95.563 / 95.1763)** — twenty-two channels at *two different
  bandwidths*: 8–14 are the 467 interstitials at half a watt and 12.5 kHz, 15–22 are the
  462 mains that carry repeater outputs. A shape that draws one lane width for a band is
  already lying here.

## The honest question every shape had to answer

A wide band is **hopped** — the radio visits one slice at a time — so at any instant most
of the band is not being watched. "Nothing heard on channel 12" and "we have not looked
at channel 12" are different facts, and a UI that renders them identically will teach the
owner to distrust it. All three mocks carry four states rather than two:
**on air now · heard recently · quiet while watched · not watched** (hatched). Switch a
mock to FM to see the hatch cover the part of the dial the radio is not on.

## The three shapes

All three are interactive and self-contained — open the file anywhere, tap the band
switcher, tap channels. They share the same three real plans and the same simulated
activity so they can be judged against the same awkward data.

| | shape | what it costs |
|---|---|---|
| **`a-channel-stepper.html`** | **the dial counts in channels** — the `− step +` row the tuner already has steps by channel; the middle pill opens a grid | nothing new on screen, and nothing new on screen: the channel plan is a *step size*, not a picture, so activity is a dot at best |
| `b-lanes-on-the-picture.html` | **channels drawn on the picture** — a lane per channel at its own legal width, over the waterfall, raw spectrum still visible between | at CB's 8 kHz over a 460 kHz span a lane is under 2% wide: the comb reads, the numbers do not fit on it |
| `c-channel-board.html` | **the channel board** — the channels *are* the screen, the picture demoted to a ribbon; sortable by busiest, with a skip list | the picture stops being tunable; you gain a scanner and lose the thing that shows a signal the plan does not know about |

**A is the binding spec, chosen 2026-09-07**, with one condition stated in the choice:

> "I like a, but it shouldn't ever be replacing the actual spectrum view"

So the tuning strip stays exactly what it is — raw bins, the tuned span marked, nothing
drawn from the channel plan. Shape A adds a **step size and a list**, and touches the
picture only by moving the marker that was already on it. The mock was revised after the
choice to draw the strip it leaves alone, because a spec that omits the picture reads as
a spec that replaces it. B's lanes are not a later phase of A: they are the shape that
was not chosen.

**What separates them** is not decoration, it is what each can do that the others cannot:

- **A** is the only one that changes nothing structurally. It is a step size on a control
  that exists, it costs no screen, and free tuning is untouched — the frequency readout
  still takes a typed number, so a channel plan can never become a cage. It is also the
  only one that says nothing about *activity*: there is no room.
- **B** is the only one that keeps the measurement and the plan in the same picture. A
  signal that spills past its lane — off-frequency, over-deviating, or misidentified — is
  visible *as* a spill, which is exactly the case a channel list cannot represent. It is
  the shape that would also improve the peak list, because a peak inside a lane can be
  named and a peak between lanes is a real find.
- **C** is the only one that can **reorder**. A list can sort by busiest and bring the six
  CB channels anyone is on to the top of forty; a dial and a waterfall are stuck in
  frequency order forever. It is also the only one with room for a **skip list**, which is
  what makes a forty-channel plan livable on a phone.

## What shipped

**Backend — `jbrain/sdr/bands.py`.** `Section` gained `channel_plan`, a boolean saying
that `channels` is the WHOLE legal set here rather than a few landmarks. It is what lets
± count, and it is the difference between CB (forty places a signal may be) and airband
(allocated per facility, where a ± walking only between the listed channels would refuse
most of the band). `validate()` refuses a plan with no channels and two channels at one
frequency — the second because stepping between them would leave the radio where it was,
which reads as a dead button.

Complete plans now ship for the four bands the ask named: **CB** (40, written out —
there is no arithmetic that produces the five R/C gaps or channel 23 above 24),
**FM broadcast** (100, generated), **the AM dial** (118, generated, named in kilohertz
because that is what a station calls itself), and **FRS/GMRS below 462.75** (15, written
out — counting stops at 7 and resumes at 15, exactly as the radios do, because 8–14 are
the half-watt 467 MHz interstitials and are a different band). Four sections whose lists
were already complete — NOAA weather, MURS, AIS, 60 m — are flagged rather than rewritten.

**Frontend — `sdrChannels.ts`.** Pure, and it draws nothing: `planAt` (by CONTAINMENT,
because the tuner knows a frequency rather than a section), `channelIndex` with a 100 Hz
tolerance for where the PLL actually landed, and `stepChannel`, which steps by INDEX on a
channel and snaps by FREQUENCY when the radio is between two.

**Frontend — `SdrTunerControls.tsx`.** The pill reads `Ch 19` instead of `10 KHZ` where a
plan covers the radio, opens the channel grid instead of the step list, and the ± count
channels. Everything else is unchanged: the readout still takes a typed frequency, an
explicit step pick still wins (`kHz step` inside the grid, `Channels` to go back), a band
with no plan behaves exactly as before, and a band table that fails to load leaves the
dial counting kilohertz rather than a ± that does nothing.

**Frontend — `SdrRadiosTab.tsx`.** Picking a channelised band to listen now lands on the
channel nearest its centre rather than on `centre_hz` itself, which is arithmetic on the
section's edges and legal only by luck: on the FM dial it is 98.000, which 47 CFR 73.201
does not allow a station on, so opening the dial would have said `Off channel` on a band
that is nothing but channels.

**Backend — `agent/sdrtools.py`.** The same plans would have flooded the model's context:
`sdr_read` printed every named channel, and the AM dial's 118 are a thousand tokens
saying what one sentence says exactly. A complete plan is now stated AS complete — "a
signal is only legal on these" — and listed in full up to 48 channels, so CB's forty are
spelled out (the irregularity is the information) while the two big rasters are described
by their arithmetic plus any channel carrying a note.

**Not covered yet:** marine VHF, whose ~57 channels are a complete plan that this change
does not carry, because getting one of them wrong is a dial that lands where no station
can legally be — and it sounds exactly like a quiet channel. Same for the AAR railroad
plan. Both want the regulatory table read rather than recalled.

## Not built, deliberately

- **Automatic scanning** (stop-on-busy, resume after N seconds). Every shape implies it,
  and it is a *behaviour* question — dwell, hang time, priority — not a layout one. It
  should be decided after the layout is, against a shape that exists.
- **A channel editor.** The plans are law, not preference. The skip list in C is the one
  piece of per-owner state, and it is a strike-through, not an edit.
