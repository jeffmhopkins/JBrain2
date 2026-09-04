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
`-E direct2` below `MIN_MHZ` and the whole range LISTENS. What does not follow down
there is everything the tuner provides: no gain control, no `rtl_power`, and images
above 14.4 MHz. `direct_sampling` and `sweepable` are how a caller asks which of those
apply, rather than comparing against a floor and guessing.
"""

from __future__ import annotations

#: What the R820T2 TUNER reaches. Above this the signal goes through the tuner and has a
#: gain control; below it the tuner is powered down entirely.
MIN_MHZ = 24.0
MAX_MHZ = 1766.0

#: What the RTL2832U's ADC reaches with the tuner bypassed — the NESDR SMArt v5's
#: on-board diplexer feeds HF straight to the **Q branch** (`rtl_fm -E direct2`).
#: Nooelec's datasheet block diagram shows the wiring; no hardware mod is involved.
#: The two ranges MEET at `MIN_MHZ` rather than leaving a gap, so everything from here
#: to `MAX_MHZ` is reachable — by one path or the other.
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
    sampling mode 1 — the I branch — while this hardware wires Q; and above 14.4 MHz the
    signal arrives **mirrored**, because the ADC samples at 28.8 MHz and that is where
    the first Nyquist zone ends."""
    return mhz < MIN_MHZ


def sweepable(mhz: float) -> str | None:
    """Why a SWEEP cannot reach this frequency, or None.

    Separate from `out_of_range` because the two answers differ, and the difference is
    the one an owner most needs explained: shortwave is perfectly listenable and cannot
    be swept. `rtl_power -D` hardcodes `verbose_direct_sampling(dev, 1)` — the ADC's I
    branch — while this hardware wires Q, so the tool would tune something and measure
    nothing, and a flat plausible waterfall is worse than a refusal.

    Lives here rather than in the route's `Query` bounds because a bound produces a 422
    with a validation blob, and this is the one surface an owner with no terminal has
    (CLAUDE.md #10). They need the sentence, not the schema."""
    refusal = out_of_range(mhz)
    if refusal:
        return refusal
    if direct_sampling(mhz):
        return (
            f"a sweep cannot go below {MIN_MHZ:g} MHz — the radio reaches shortwave by "
            f"bypassing its tuner, and the sweep tool cannot use that path. You can "
            f"still listen there."
        )
    return None


def tunable(mhz: float) -> bool:
    """Whether the radio can reach this frequency at all, by either path."""
    return DIRECT_MIN_MHZ <= mhz <= MAX_MHZ


def out_of_range(mhz: float) -> str | None:
    """Why this frequency cannot be tuned, or None. One sentence, for an operator.

    Says which END it is outside rather than quoting the whole range, because "0.1 to
    1766" invites the reader to conclude the radio is one thing that tunes all of it —
    and the interesting fact about a frequency near the bottom is which path it takes,
    not that it is legal."""
    if mhz > MAX_MHZ:
        return f"{mhz:g} MHz is above what this radio reaches ({MAX_MHZ:g} MHz)."
    if mhz < DIRECT_MIN_MHZ:
        return f"{mhz:g} MHz is below what this radio reaches ({DIRECT_MIN_MHZ:g} MHz)."
    return None


#: The most bins one waterfall row carries. Mirrors the sidecar's `SPECTRUM_MAX_BINS`,
#: for the reason the tuner range is mirrored: the sidecar ships in a different container
#: and imports nothing from here. The sidecar refuses a wider frame too, so this copy
#: only decides whether the owner gets a coarser picture or a 502.
SPECTRUM_MAX_BINS = 4096

#: The widest span rtl_power is allowed, mirroring `MAX_SWEEP_SPAN_HZ` in the sidecar.
MAX_SPAN_MHZ = 60.0


def live_bin_hz(span_hz: int, want_hz: int) -> int:
    """The bin width a live view of `span_hz` can actually be drawn at.

    COARSENED, not refused. A live spectrum is the one place the owner asks for a whole
    band at once, and "that is too wide" is a worse answer than a coarser picture they
    can then zoom into — the frame carries its own bin width, so a coarse row draws
    correctly without anything downstream being told.

    Coarsened by DOUBLING rather than by dividing the span, because rtl_power grants the
    largest power-of-two division of its per-hop bandwidth that is no coarser than what
    it was asked for (`bands.sweep_bin_hz` says the same). Walking the sequence the tool
    will itself land on beats computing an exact number it will not honour."""
    bin_hz = max(1, want_hz)
    while span_hz // bin_hz > SPECTRUM_MAX_BINS:
        bin_hz *= 2
    return bin_hz


def viewable(start_mhz: float, stop_mhz: float) -> str | None:
    """Why a live spectrum cannot cover this range, or None. One sentence, as ever.

    A superset of `sweepable` on both edges — the picture is rtl_power, so everything
    that stops a sweep stops a waterfall — plus the span ceiling, which a single
    frequency cannot violate and a range can."""
    if stop_mhz <= start_mhz:
        return "a waterfall needs a range, not a single frequency."
    for edge in (start_mhz, stop_mhz):
        refusal = sweepable(edge)
        if refusal:
            return refusal
    if stop_mhz - start_mhz > MAX_SPAN_MHZ:
        return (
            f"{stop_mhz - start_mhz:g} MHz at once is wider than the radio can sweep "
            f"({MAX_SPAN_MHZ:g} MHz). Pick a section of it."
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
