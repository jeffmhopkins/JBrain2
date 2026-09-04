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
bypassing the tuner — how every RTL-SDR reaches HF. Nothing here enables that: no
direct-sampling flag is passed anywhere in `deploy/sdr/`. So this is what the SOFTWARE
reaches today, and lowering it alone would only move the refusal one layer down.
"""

from __future__ import annotations

MIN_MHZ = 24.0
MAX_MHZ = 1766.0


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
