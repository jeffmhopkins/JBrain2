"""What this radio can tune, in one place.

This existed three times — `api/sdr.py`, `agent/sdrtools.py`, and `deploy/sdr/listen.py`
— and two of them said 0.024 MHz against the sidecar's 24. A frequency between those
passed every check the api makes and then came back as a 502 from the radio, which is
the shape of failure a duplicated constant produces: not a wrong answer, a right answer
from the wrong layer, with the error message to match.

The sidecar keeps its own copy because it ships in a different container and imports
nothing from here. `test_tuner_range.py` reads it and fails if the two ever disagree,
which is the only part of this that cannot be solved by sharing a module.

**24 MHz is not the hardware's floor.** It is the R820T2's native tuner range, and the
SMArt v5 is sold as 100 kHz-1.75 GHz because the RTL2832U's ADC can be fed directly,
bypassing the tuner — how every RTL-SDR reaches HF. `deploy/sdr/listen.py` passes
`-E direct2` below `MIN_MHZ`, and everything up to the ADC's Nyquist edge LISTENS.
What does not follow down there is everything the tuner provides: no gain control and
images above 14.4 MHz. Nor does the range meet in the middle — 14.4-24 MHz is bypassed
by the tuner and past the ADC's honest edge, so it is refused rather than tuned into the
second Nyquist zone (`aliased`). `direct_sampling` and `aliased` are how a caller asks
which of those apply, rather than comparing against a floor and guessing.

**`sweepable` is gone, and that is the end of a three-wave story.** It was
`rtl_power`'s question — the tool hardcodes direct sampling mode 1, the ADC's I branch,
where this board wires Q — so a survey could never reach shortwave while a picture of
the same range could. B1 removed the tool from the picture and B2 from the survey
(`docs/archive/SDR_RECEIVER_CONVERGENCE_PLAN.md`), so both now ask `viewable`: one engine,
one answer, and shortwave is surveyable.
"""

from __future__ import annotations

from jbrain.sdr import bands

#: What the R820T2 TUNER reaches. Above this the signal goes through the tuner and has a
#: gain control; below it the tuner is powered down entirely.
MIN_MHZ = 24.0
MAX_MHZ = 1766.0

#: Where the ADC's first Nyquist zone ends, in the unit this module speaks. Everything
#: above it on the DIRECT path arrives as `28.8 MHz − f`, which is another frequency
#: entirely rather than a weaker version of the one asked for.
NYQUIST_MHZ = bands.NYQUIST_HZ / 1_000_000

#: What the RTL2832U's ADC reaches with the tuner bypassed — the NESDR SMArt v5's
#: on-board diplexer feeds HF straight to the **Q branch** (`rtl_fm -E direct2`).
#: Nooelec's datasheet block diagram shows the wiring; no hardware mod is involved.
#: The two ranges OVERLAP on paper and not in practice: direct sampling is honest only
#: to `NYQUIST_MHZ`, so 14.4-24 MHz is inside both endpoints and reachable by neither
#: path (`aliased`). Everything else from here to `MAX_MHZ` is reachable by one or the
#: other.
DIRECT_MIN_MHZ = 0.1

#: The floor of everything this radio can reach, either way. Not a replacement for
#: `MIN_MHZ`: the two ranges are DIFFERENT SIGNAL PATHS, and code that needs to know
#: which one a frequency uses must ask `direct_sampling`, never compare against this.
TUNABLE_MIN_MHZ = DIRECT_MIN_MHZ

#: What the CONVERTER passes at its own INPUT — a bound on the DIAL, not on the tune.
#: A Ham It Up is a 125 MHz mixer behind a low-pass filter, and the filter is the part
#: nothing here asked about: 98 MHz + 125 is 223 MHz, an ordinary tuning this dongle
#: takes without complaint, so an FM sweep through the converter was accepted and came
#: back as noise. Every other bound in this file is about what the DONGLE can do, which
#: is exactly why this one was missing.
#:
#: **The owner gave 300 Hz - 65 MHz on 2026-09-12** and owns the unit; that is the whole
#: provenance, and it is a better one than a datasheet. Nothing in this repo measured it.
#: 300 Hz is written as 0.0003 MHz on purpose — a floor of 0 would silently re-admit the
#: DC end the filter does not pass, which is what an `int()` or a tidier literal costs.
#:
#: ONE pair of numbers, not a per-radio setting: one converter is in play. A second one
#: would make this a table keyed by the stored offset — which is why the names say
#: `CONVERTER` rather than being folded into the tuner's own bounds above.
CONVERTER_MIN_MHZ = 0.0003
CONVERTER_MAX_MHZ = 65.0


def tuned_mhz(mhz: float, upconverter_mhz: float = 0.0) -> float:
    """Where the DONGLE sits to receive `mhz`, given whatever is in front of it.

    The whole of the upconverter, in one line. A Ham It Up mixes the band up by its
    crystal, so to hear 7.200 the dongle tunes 132.200 — and this is the only direction
    the shift is ever computed in. Nothing subtracts it back off, because nothing else
    ever holds the shifted number: every frequency any layer reports is the owner's, and
    the tune is derived from it at the two places that actually talk to the hardware
    (`deploy/sdr/radio.Radio._apply_locked` and the one `-f` argv). A codebase with a
    subtraction in it is a codebase where one caller can forget one."""
    return mhz + max(upconverter_mhz, 0.0)


def direct_sampling(mhz: float, upconverter_mhz: float = 0.0) -> bool:
    """Whether this frequency is reached with the tuner bypassed.

    Three consequences follow, and every one of them is a thing the UI must not offer:
    there is **no gain control** (the tuner is powered down, so `-g` writes to a chip
    that is not listening); **`rtl_power` cannot sweep it**, because it hardcodes direct
    sampling mode 1 — the I branch — while this hardware wires Q; and everything here
    arrives SUMMED with a reversed image of `28.8 MHz − f`, because that is the ADC's
    clock and the fold is in the samples rather than in the picture. The band table
    names the folded band per section (`image_start_hz`); this used to say the images
    started above 14.4 MHz, which was the *aliasing* rule and not the image one."""
    return tuned_mhz(mhz, upconverter_mhz) < MIN_MHZ


def aliased(mhz: float, upconverter_mhz: float = 0.0) -> str | None:
    """Why tuning here would hand back a DIFFERENT frequency, or None.

    The hole between the two signal paths, and the reason it is a refusal rather than a
    caveat. `listen.demod_args` applies `-E direct2` to everything below `MIN_MHZ`, and
    direct sampling digitises a real signal at 28.8 MS/s — so its honest range stops at
    `NYQUIST_MHZ`, and 14.4-24 MHz is sampled into the SECOND zone and folds back onto
    `28.8 MHz − f`. Ask for 18.1 MHz and the radio delivers 10.7 MHz: not a quiet band,
    not an error, a confident picture and clean audio OF SOMETHING ELSE. The 31 m
    broadcast band is in there, so a listener would hear a station and have every
    reason to believe it (SDR_IQ_SPECTRUM_PLAN §8).

    Refused rather than served with a note, because the failure is silent by
    construction: nothing downstream — not the level meter, not whisper, not the
    waterfall's colour scale — can tell the two apart.

    A converter takes the hole away rather than narrowing it: with one inline the dongle
    tunes above `MIN_MHZ`, the R820T2 is back in circuit and mixes properly, and there
    is no direct-sampling fold to refuse. That is what the converter is FOR — not
    "unlocking HF", which direct sampling already does.

    Deliberately asked of the TUNE, and it stays that way: whether the ADC folds depends
    on where the dongle sits and on nothing else. Whether the converter's input filter
    passed the dial is a different question with a different answer, and it belongs to
    `out_of_range` (`CONVERTER_MAX_MHZ`) — the same is true of `direct_sampling`."""
    tuned = tuned_mhz(mhz, upconverter_mhz)
    if NYQUIST_MHZ < tuned < MIN_MHZ:
        return (
            f"{mhz:g} MHz is not receivable: below {MIN_MHZ:g} MHz this radio bypasses "
            f"its tuner and samples directly, and that path is honest only up to "
            f"{NYQUIST_MHZ:g} MHz. Tuning {mhz:g} would in fact deliver "
            f"{bands.ADC_RATE_HZ / 1_000_000 - mhz:g} MHz."
        )
    return None


def out_of_range(mhz: float, upconverter_mhz: float = 0.0) -> str | None:
    """Why this frequency cannot be tuned, or None. One sentence, for an operator.

    Says which END it is outside rather than quoting the whole range, because "0.1 to
    1766" invites the reader to conclude the radio is one thing that tunes all of it —
    and the interesting fact about a frequency near the bottom is which path it takes,
    not that it is legal.

    The range has a HOLE in it, which is the other reason not to quote the ends:
    14.4-24 MHz is inside both numbers and reachable by neither path (`aliased`).

    With a converter inline the question is asked of the TUNE rather than of the dial:
    7.200 MHz is a frequency this dongle cannot mix and 132.200 is an ordinary one, and
    which of those the hardware is asked for is the whole content of the setting. The
    refusals then name BOTH numbers, because an owner reading "132.2 MHz is above what
    this radio reaches" about a request for 7.2 would have no way to connect the two.

    **But the DIAL is asked first, of the converter.** Reachability of the tune was the
    only converter question here, and it is only half of one: the owner set 125 MHz
    inline and swept 88-108, every frequency of which tunes comfortably inside 24-1766
    and none of which the converter's input filter passes — accepted, and noise
    (2026-09-12). First rather than last because it is the NEARER cause: 1700 MHz
    through this converter is both past the dongle's ceiling and outside the converter's
    input, and only one of those two refusals names something the owner can change."""
    if upconverter_mhz > 0:
        tuned = tuned_mhz(mhz, upconverter_mhz)
        if mhz <= 0:
            return f"{mhz:g} MHz is not a frequency."
        if mhz > CONVERTER_MAX_MHZ:
            return (
                f"{mhz:g} MHz is above what the converter passes ({CONVERTER_MAX_MHZ:g} MHz at "
                f"its input), so tuning {tuned:g} MHz would hear nothing — turn the converter "
                f"Off for VHF and up."
            )
        if mhz < CONVERTER_MIN_MHZ:
            # No remedy offered, unlike the top edge: with the converter Off this is
            # below `DIRECT_MIN_MHZ` too, so "turn it off" would be a fix that fails.
            return (
                f"{mhz:g} MHz is below what the converter passes ({CONVERTER_MIN_MHZ:g} MHz at "
                f"its input), so tuning {tuned:g} MHz would hear nothing."
            )
        if tuned > MAX_MHZ:
            return (
                f"{mhz:g} MHz tunes {tuned:g} MHz through the converter, which is above "
                f"what this radio reaches ({MAX_MHZ:g} MHz)."
            )
        if tuned < MIN_MHZ:
            # Not a frequency problem — an OFFSET problem, and saying so is the
            # difference between an owner correcting a setting and an owner concluding
            # the radio cannot hear a band it can.
            return (
                f"{mhz:g} MHz tunes {tuned:g} MHz through the converter, below the "
                f"tuner's floor ({MIN_MHZ:g} MHz) — the offset is too small for it."
            )
        return None
    if mhz > MAX_MHZ:
        return f"{mhz:g} MHz is above what this radio reaches ({MAX_MHZ:g} MHz)."
    if mhz < DIRECT_MIN_MHZ:
        return f"{mhz:g} MHz is below what this radio reaches ({DIRECT_MIN_MHZ:g} MHz)."
    return aliased(mhz)


#: The widest span the sidecar allows, mirroring `MAX_SWEEP_SPAN_HZ` there.
MAX_SPAN_MHZ = 60.0


def viewable(start_mhz: float, stop_mhz: float, upconverter_mhz: float = 0.0) -> str | None:
    """Why a live spectrum cannot cover this range, or None. One sentence, as ever.

    **The only question now, for the picture and for the survey alike.** These used to
    be two predicates that disagreed, because the survey WAS `rtl_power` and the tool
    cannot reach the ADC branch this board wires. One engine draws and integrates now,
    so one answer serves both (B1/B2).

    What replaces it down there is a NARROWER rule, not none: below `MIN_MHZ` the
    picture is one capture or nothing, because the thing that stitches several hops
    together is the tool that cannot go there. `bands.capture_for` answers whether one
    exists, so the band table and this refusal cannot disagree about the same range.

    A converter moves the whole question onto the tuner path: the capture plan is chosen
    against the frequencies the DONGLE will see, because that is what decides which
    rates are legal and whether a span can be hopped. The refusals still name the
    owner's edges, since those are the numbers they typed.

    **A span the converter blocks part of is refused WHOLE, never trimmed.** Trimming
    would hand back a picture of 60-65 MHz labelled with the 60-70 that was asked for —
    and every edge this function returns goes on to caption a waterfall, size a sweep's
    occupancy window and anchor `peaks.find`. A quiet stripe where the converter stops
    is indistinguishable from a quiet band, which is this module's whole reason for
    refusing rather than serving something adjacent. So the refusal says the overlap out
    loud and leaves asking for it to the owner."""
    if stop_mhz <= start_mhz:
        return "a waterfall needs a range, not a single frequency."
    if upconverter_mhz > 0:
        # Asked of the SPAN before the edges, so a blocked range is answered in a
        # sentence about a range: the per-edge `out_of_range` below refuses the same
        # requests, but while talking about one frequency and one tune. Only the edge
        # that is actually in the way is named, as everywhere else in this module.
        if start_mhz > CONVERTER_MAX_MHZ:
            return (
                f"{start_mhz:g}-{stop_mhz:g} MHz is above what the converter passes "
                f"({CONVERTER_MAX_MHZ:g} MHz at its input), so none of it reaches the dongle — "
                f"turn the converter Off for VHF and up."
            )
        if stop_mhz > CONVERTER_MAX_MHZ:
            return (
                f"{start_mhz:g}-{stop_mhz:g} MHz runs past the top of what the converter passes "
                f"({CONVERTER_MAX_MHZ:g} MHz at its input), and a span it blocks part of is "
                f"refused whole rather than trimmed. Ask for {start_mhz:g}-"
                f"{CONVERTER_MAX_MHZ:g} MHz, or turn the converter Off."
            )
        if stop_mhz < CONVERTER_MIN_MHZ:
            return (
                f"{start_mhz:g}-{stop_mhz:g} MHz is below what the converter passes "
                f"({CONVERTER_MIN_MHZ:g} MHz at its input), so none of it reaches the dongle."
            )
        if start_mhz < CONVERTER_MIN_MHZ:
            return (
                f"{start_mhz:g}-{stop_mhz:g} MHz starts below what the converter passes "
                f"({CONVERTER_MIN_MHZ:g} MHz at its input), and a span it blocks part of is "
                f"refused whole rather than trimmed. Ask for {CONVERTER_MIN_MHZ:g}-"
                f"{stop_mhz:g} MHz."
            )
    for edge in (start_mhz, stop_mhz):
        refusal = out_of_range(edge, upconverter_mhz)
        if refusal:
            return refusal
    tuned_start_hz = int(round(tuned_mhz(start_mhz, upconverter_mhz) * 1_000_000))
    tuned_stop_hz = int(round(tuned_mhz(stop_mhz, upconverter_mhz) * 1_000_000))
    if direct_sampling(start_mhz, upconverter_mhz) != direct_sampling(stop_mhz, upconverter_mhz):
        # Not a bandwidth problem, which is why it is said separately: the tuner is
        # powered down on one side of this line and in circuit on the other, so no
        # single capture exists that could cover both halves.
        return (
            f"{start_mhz:g}-{stop_mhz:g} MHz crosses {MIN_MHZ:g} MHz, where the radio "
            f"changes signal path — the tuner is bypassed below it and in circuit "
            f"above. Ask for one side at a time."
        )
    if direct_sampling(start_mhz, upconverter_mhz):
        if bands.capture_for(tuned_start_hz, tuned_stop_hz) is None:
            return (
                f"{stop_mhz - start_mhz:g} MHz at once is more than one capture below "
                f"{MIN_MHZ:g} MHz, and the sweep that stitches several together cannot "
                f"use the shortwave path. Pick a narrower piece of it."
            )
        return None
    if stop_mhz - start_mhz > MAX_SPAN_MHZ:
        return (
            f"{stop_mhz - start_mhz:g} MHz at once is wider than the radio can sweep "
            f"({MAX_SPAN_MHZ:g} MHz). Pick a section of it."
        )
    if (
        bands.capture_for(tuned_start_hz, tuned_stop_hz) is None
        and bands.hop_plan(tuned_start_hz, tuned_stop_hz) is None
    ):
        # THE ENGINE'S OWN LIMIT, said rather than served by a different engine. Until
        # B1 this fell through to `rtl_power`, which draws the range on its own
        # uncalibrated scale — and both engines land on the same `Frame.db`, the same
        # colour map and the same `peaks.find`, whose output reaches the agent's tools
        # as a MEASUREMENT. A silent engine swap that changes what a number means is a
        # correctness bug wearing a robustness costume
        # (`docs/archive/SDR_RECEIVER_CONVERGENCE_PLAN.md` B1).
        #
        # Every one of the 32 curated sections is inside this, so what it refuses is a
        # hand-typed span, and the number comes off the same ladder `hop_plan` walks.
        widest = bands.widest_stitchable_hz() / 1_000_000
        return (
            f"{stop_mhz - start_mhz:g} MHz at once is more than the waterfall can "
            f"stitch in one row ({widest:g} MHz). Pick a narrower piece of it, or a "
            f"band section."
        )
    return None


def nodes_in(usb_payload: object) -> dict[str, str]:
    """Serial -> `/dev/bus/usb/...` node, from the supervisor's scan.

    The map a RESET needs, and the reason it can exist at all: sysfs answers from what
    the kernel cached when the device first enumerated, so it still names a dongle whose
    LIVE descriptor reads have stopped working — which is exactly the device anyone
    would want to reset. Asking librtlsdr instead would fail on the one case that
    matters.

    Empty rather than None on a scan that could not see: a reset needs a node, so "we
    cannot tell" and "no such radio" lead to the same refusal here, unlike `serials_in`
    where they must not be flattened."""
    if not isinstance(usb_payload, dict) or not usb_payload.get("sysfs_readable"):
        return {}
    found = usb_payload.get("sdrs")
    if not isinstance(found, list):
        return {}
    return {
        entry["serial"]: entry["device_node"]
        for entry in found
        if isinstance(entry, dict)
        and isinstance(entry.get("serial"), str)
        and entry["serial"]
        and isinstance(entry.get("device_node"), str)
        and entry["device_node"]
    }


def serials_in(usb_payload: object) -> list[str] | None:
    """The serials of every SDR the supervisor's `/usb` scan found, in serial order —
    or None when the scan could not see.

    ONE place knows that the payload puts them at `sdrs[].serial`, because two places
    knowing it is how this file came to exist: the tuner range was retyped four times
    and two copies were wrong.

    **None and `[]` are different answers and must not be flattened.** The supervisor's
    own scan says why: an empty list means "no devices" only if sysfs was readable, and
    otherwise means "we could not see" — a different fault with a different fix.
    Downstream the gap is larger: "nothing attached" is a state a dedicated radio should
    WAIT on, while "we could not see" is the one case where naming no radio is right,
    because that is what a one-dongle box always did. Collapsing them turns the wait
    into a silent substitution.

    Sorted, and duplicates dropped, so a caller choosing "the first one" gets a stable
    answer rather than USB enumeration order — which is the entire bug this feature
    exists to remove. A device with no serial is skipped: it cannot be named to `-d`, so
    listing it would promise a selection that cannot be made."""
    if not isinstance(usb_payload, dict) or not usb_payload.get("sysfs_readable"):
        return None
    found = usb_payload.get("sdrs")
    if not isinstance(found, list):
        return None
    serials = {
        entry["serial"]
        for entry in found
        if isinstance(entry, dict) and isinstance(entry.get("serial"), str) and entry["serial"]
    }
    return sorted(serials)
