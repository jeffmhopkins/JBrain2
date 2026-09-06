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


def direct_sampling(mhz: float) -> bool:
    """Whether this frequency is reached with the tuner bypassed.

    Three consequences follow, and every one of them is a thing the UI must not offer:
    there is **no gain control** (the tuner is powered down, so `-g` writes to a chip
    that is not listening); **`rtl_power` cannot sweep it**, because it hardcodes direct
    sampling mode 1 — the I branch — while this hardware wires Q; and everything here
    arrives SUMMED with a reversed image of `28.8 MHz − f`, because that is the ADC's
    clock and the fold is in the samples rather than in the picture. The band table
    names the folded band per section (`image_start_hz`); this used to say the images
    started above 14.4 MHz, which was the *aliasing* rule and not the image one."""
    return mhz < MIN_MHZ


def aliased(mhz: float) -> str | None:
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
    waterfall's colour scale — can tell the two apart."""
    if NYQUIST_MHZ < mhz < MIN_MHZ:
        return (
            f"{mhz:g} MHz is not receivable: below {MIN_MHZ:g} MHz this radio bypasses "
            f"its tuner and samples directly, and that path is honest only up to "
            f"{NYQUIST_MHZ:g} MHz. Tuning {mhz:g} would in fact deliver "
            f"{bands.ADC_RATE_HZ / 1_000_000 - mhz:g} MHz."
        )
    return None


def out_of_range(mhz: float) -> str | None:
    """Why this frequency cannot be tuned, or None. One sentence, for an operator.

    Says which END it is outside rather than quoting the whole range, because "0.1 to
    1766" invites the reader to conclude the radio is one thing that tunes all of it —
    and the interesting fact about a frequency near the bottom is which path it takes,
    not that it is legal.

    The range has a HOLE in it, which is the other reason not to quote the ends:
    14.4-24 MHz is inside both numbers and reachable by neither path (`aliased`)."""
    if mhz > MAX_MHZ:
        return f"{mhz:g} MHz is above what this radio reaches ({MAX_MHZ:g} MHz)."
    if mhz < DIRECT_MIN_MHZ:
        return f"{mhz:g} MHz is below what this radio reaches ({DIRECT_MIN_MHZ:g} MHz)."
    return aliased(mhz)


#: The widest span the sidecar allows, mirroring `MAX_SWEEP_SPAN_HZ` there.
MAX_SPAN_MHZ = 60.0


def viewable(start_mhz: float, stop_mhz: float) -> str | None:
    """Why a live spectrum cannot cover this range, or None. One sentence, as ever.

    **The only question now, for the picture and for the survey alike.** These used to
    be two predicates that disagreed, because the survey WAS `rtl_power` and the tool
    cannot reach the ADC branch this board wires. One engine draws and integrates now,
    so one answer serves both (B1/B2).

    What replaces it down there is a NARROWER rule, not none: below `MIN_MHZ` the
    picture is one capture or nothing, because the thing that stitches several hops
    together is the tool that cannot go there. `bands.capture_for` answers whether one
    exists, so the band table and this refusal cannot disagree about the same range."""
    if stop_mhz <= start_mhz:
        return "a waterfall needs a range, not a single frequency."
    for edge in (start_mhz, stop_mhz):
        refusal = out_of_range(edge)
        if refusal:
            return refusal
    if direct_sampling(start_mhz) != direct_sampling(stop_mhz):
        # Not a bandwidth problem, which is why it is said separately: the tuner is
        # powered down on one side of this line and in circuit on the other, so no
        # single capture exists that could cover both halves.
        return (
            f"{start_mhz:g}-{stop_mhz:g} MHz crosses {MIN_MHZ:g} MHz, where the radio "
            f"changes signal path — the tuner is bypassed below it and in circuit "
            f"above. Ask for one side at a time."
        )
    if direct_sampling(start_mhz):
        start_hz = int(round(start_mhz * 1_000_000))
        stop_hz = int(round(stop_mhz * 1_000_000))
        if bands.capture_for(start_hz, stop_hz) is None:
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
    start_hz = int(round(start_mhz * 1_000_000))
    stop_hz = int(round(stop_mhz * 1_000_000))
    if bands.capture_for(start_hz, stop_hz) is None and bands.hop_plan(start_hz, stop_hz) is None:
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
