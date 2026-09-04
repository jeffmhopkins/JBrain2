"""Which radio a job gets, when there is more than one.

The box has two RTL-SDR dongles. `rtl_fm` and `rtl_power` are invoked with no `-d`, so
they open whichever librtlsdr enumerates FIRST — and with one radio on a desk whip and
one on a long wire, that means APRS can move to the wrong antenna on a re-plug with no
symptom but worse reception. This module is the answer to "which one", and it is pure:
a mapping, a list of what is attached, and a job in; a decision out. No settings, no
radio, no clock.

**Dedicated does not fall back.** A service whose radio is unplugged WAITS. Quietly
moving it to another antenna is the exact failure being fixed, and a detector that
silently changes what it is listening through produces measurements nobody can compare.
So an absent dedicated radio is a refusal carrying a reason, never a substitution.

**A general radio is picked by serial order, not enumeration order.** Which general
radio a tuner gets is arbitrary but it must be REPEATABLE — "arbitrary and stable" is a
choice, "arbitrary and whatever the USB stack did this boot" is the bug.

**A general radio someone is already on is not a free one.** `busy` came later, and
without it this module was the reason two dongles still took turns: with two undedicated
radios, APRS and the tuner both got `generals[0]`, the second caller met the sidecar's
per-radio 409 naming the radio it had asked for, and the other dongle sat idle. Serial
order still decides between the free ones, so the rule above holds where it matters —
`busy` only moves a caller off a radio it could not have had anyway.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: The role of a radio nothing has reserved: the tuner, sweeps, and any service with no
#: radio of its own may all take it, one at a time.
GENERAL = "general"

#: What to call a job in a refusal. `.get` with the id as its own fallback, because a
#: stored role this build does not recognise still RESERVES the radio (`_generals`
#: excludes anything that is not GENERAL) — so the sentence has to name it rather than
#: crash on it or, worse, call it general use.
JOB_LABEL = {GENERAL: "general use", "aprs": "APRS logging"}


def job_label(job: str) -> str:
    return JOB_LABEL.get(job, job)


@dataclass(frozen=True, slots=True)
class Radio:
    """One dongle as the owner described it, keyed by serial everywhere.

    Serial rather than USB node or bus, because those move: this box's first dongle went
    from `/dev/bus/usb/001/005` to `001/011` across a single re-plug, and its identity
    must not move with it."""

    serial: str
    name: str = ""
    description: str = ""
    role: str = GENERAL

    @property
    def label(self) -> str:
        """What to call it in a sentence — the owner's name, or the serial if unnamed.

        Never blank: a message reading "waiting for " helps nobody."""
        return self.name.strip() or self.serial


@dataclass(frozen=True, slots=True)
class Choice:
    """Which radio, and why — including when the answer is none.

    `serial is None` is a real, expected outcome rather than an error case: a dedicated
    radio that is unplugged means the service does not run, and the caller has to be
    able to say WHICH of the reasons applied, because they need different answers from
    the owner (plug it back in / stop double-dedicating / free up a radio)."""

    serial: str | None
    reason: str
    """One of: `dedicated`, `general`, `named`, `waiting`, `ambiguous`, `reserved`,
    `none`. The last four are the ones the OWNER has to act on (`resolve.OWNER_MUST_ACT`);
    the first three are a radio to go ahead with."""
    detail: str
    """A sentence for the operator, naming radios the way they named them."""


def _generals(
    radios: Mapping[str, Radio], attached: Sequence[str], busy: Sequence[str] = ()
) -> list[Radio]:
    """Attached radios nothing has reserved, FREE ones first, in serial order.

    Sorted so the choice is repeatable across reboots. An attached serial with no stored
    entry is a radio the owner has not described yet, which is general use by default —
    plugging in a new dongle must not need a settings visit before anything works.

    Reserved radios are excluded, which is the whole force of "dedicated": a radio kept
    for APRS is not a radio the tuner may borrow while APRS happens to be idle.

    A radio in `busy` is one the sidecar is already running something on. It is sorted
    LAST rather than dropped, because "every general radio is busy" and "there is no
    general radio" are different states: the first is a 409 the caller can act on by
    releasing something, and the second is a settings problem. Dropping it would turn
    the first into the second and send the owner to the wrong screen."""
    return sorted(
        (
            radio
            for radio in (radios.get(s) or Radio(serial=s) for s in set(attached))
            if radio.role == GENERAL
        ),
        key=lambda r: (r.serial in set(busy), r.serial),
    )


def choose(
    radios: Mapping[str, Radio],
    attached: Sequence[str],
    want: str,
    busy: Sequence[str] = (),
) -> Choice:
    """Pick the radio for `want` — a service id, or `GENERAL` for the tuner and sweeps.

    Dedication is read off the STORED entries rather than the attached ones, so a
    service dedicated to an unplugged radio is distinguishable from one dedicated to
    nothing. That distinction is the whole point: the first waits, the second falls back
    to a general radio, and collapsing them is how a service silently changes antenna.

    `busy` is the serials the sidecar already has a session on, and only ever reorders
    the general radios — a DEDICATED radio that is busy is still this service's radio,
    and offering it a different one would be the substitution the whole module exists to
    prevent. Empty by default so the pure callers (and the settings screen) keep working
    with no notion of what is running.
    """
    live = set(attached)
    generals = _generals(radios, attached, busy)

    if want == GENERAL:
        if generals:
            return Choice(
                generals[0].serial,
                "general",
                f"Using {generals[0].label} — general use, so anything may take it.",
            )
        return Choice(
            None,
            "none",
            "No radio available: every attached radio is dedicated to a service."
            if live
            else "No radio attached.",
        )

    dedicated = sorted((r for r in radios.values() if r.role == want), key=lambda r: r.serial)
    if len(dedicated) > 1:
        names = " and ".join(r.label for r in dedicated)
        return Choice(
            None,
            "ambiguous",
            f"{names} are both dedicated to this service. Set one back to general use.",
        )
    if dedicated:
        only = dedicated[0]
        if only.serial in live:
            return Choice(only.serial, "dedicated", f"Using {only.label}, reserved for it.")
        # The refusal this module exists for. A substitution here would be silent, and
        # the owner would find out from a worse signal rather than from a sentence.
        return Choice(
            None,
            "waiting",
            f"Waiting for {only.label}: it is dedicated to this service and not attached.",
        )

    if generals:
        return Choice(
            generals[0].serial,
            "general",
            f"Using {generals[0].label} — no radio is dedicated to this service, so it "
            "shares a general one and the tuner can take it away.",
        )
    return Choice(
        None,
        "none",
        "No radio available: every attached radio is dedicated to something else."
        if live
        else "No radio attached.",
    )


def named(
    radios: Mapping[str, Radio],
    attached: Sequence[str],
    want: str,
    serial: str,
) -> Choice:
    """May THIS radio take this job? The owner pointed at one.

    `choose` answers "which radio should do this"; this answers "may that one". Both are
    needed and neither is the other. The launcher's chosen shape makes the RADIO the
    object, so a tap on a radio is a decision the api has to honour or refuse BY NAME —
    quietly running the job on a different one is the same substitution this module
    exists to prevent, reached from the opposite direction. A refusal that named no
    radio would be just as bad: the owner tapped a specific card.

    **A radio the owner has not described yet is general use**, exactly as `_generals`
    treats it: plugging in a new dongle must not need a settings visit before anything
    works, and that has to hold whether the radio was chosen for you or by you.

    **Being BUSY is not refused here.** The sidecar holds one session per radio and
    answers with a 409 naming the job that has it — a better sentence than anything this
    could compose from a serial list, and the only one that cannot already be stale by
    the time the caller reads it."""
    known = radios.get(serial) or Radio(serial=serial)
    if serial not in set(attached):
        return Choice(None, "waiting", f"{known.label} is not attached.")
    if known.role != GENERAL and known.role != want:
        return Choice(
            None,
            "reserved",
            f"{known.label} is reserved for {job_label(known.role)}. Free it in "
            f"Settings → Radios, or pick another radio.",
        )
    return Choice(serial, "named", f"Using {known.label}, as asked.")


def conflicts(radios: Mapping[str, Radio]) -> dict[str, list[str]]:
    """Services with more than one radio dedicated to them, service -> serials.

    Separate from `choose` because the settings screen has to show the problem on the
    RADIO CARDS, before anyone runs anything, while `choose` only meets it at the moment
    a service tries to start. Two radios logging one frequency stores every frame twice.
    """
    seen: dict[str, list[str]] = {}
    for radio in sorted(radios.values(), key=lambda r: r.serial):
        if radio.role != GENERAL:
            seen.setdefault(radio.role, []).append(radio.serial)
    return {service: serials for service, serials in seen.items() if len(serials) > 1}
