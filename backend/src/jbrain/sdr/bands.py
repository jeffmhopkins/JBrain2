"""What is worth listening to, and the settings that make each of it sound right.

**The problem this solves is not "a list of nice frequencies".** It is that the
settings a radio needs are only correct TOGETHER. A gain that suits broadcast FM is
wrong for a weak 2 m repeater; 12 kHz of bandwidth is right for a GMRS channel and
mangles a broadcast station; `-E dc` is necessary on AM and lossy on FM. Exposing those
as independent controls guarantees invalid combinations, and the owner discovers each
one as "the radio sounds bad" rather than as an error. Every mature web SDR arrived at
this same answer independently — OpenWebRX calls them profiles, WebSDR band buttons,
KiwiSDR DX labels — because the alternative does not work.

**Sections, not bands.** A band is where a service lives; a SECTION is a piece of one
small enough to be looked at, named the way an operator would name it. That split is
forced by three separate facts, and only the first is about screens:

1. The receiver digitises ~2.4 MHz at once, so a live view of 2 m (4 MHz) cannot exist.
2. `rtl_power` refuses a span over `MAX_SWEEP_SPAN_HZ`.
3. **The one nobody predicts:** the moment a span needs a second retune hop, the
   per-bin duty cycle collapses — 100% at one hop, 20% at two, 1.6% at twenty-two.
   At 22 hops the radio looks away from any given frequency for 193 ms at a time, and
   an APRS burst can fall entirely between visits and leave no trace. **Splitting is a
   sensitivity decision as much as a bandwidth one**, which is why `sweep_seconds` in
   this table rises with hop count instead of being a constant.

All three are decidable once, offline, by someone reading a band plan — and the result
has a name, which "144.000–148.000" does not. `validate()` re-derives the arithmetic
from the rows so a table that drifts from the physics fails a test rather than quietly
mis-tuning a radio.

**Region.** These are US / ITU Region 2 allocations. Channel spacings, sub-band
boundaries and even which services exist are regional: airband is 25 kHz here and
8.33 kHz in much of Europe, and the 2 m plan differs outright. `REGION` says so rather
than leaving a traveller to find out from a radio that sounds wrong.

**Receive only.** Nothing here transmits — the hardware cannot. A section is a place to
listen, and `note` is where "this is data, not voice" belongs, so the owner is not left
wondering why a channel carrying AIS or ATV produces noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: Which band plan these rows encode. Not decoration: a section's `channel_hz` and
#: `step_hz` are regional facts, and a table used against the wrong plan mis-tunes.
REGION = "us"

#: Below this the R820T2 tuner cannot reach and the RTL2832U's ADC is fed directly
#: instead (`rtl_fm -E direct2`, the Q branch the NESDR SMArt v5 wires through its
#: on-board diplexer). It is a different signal path, not a wider range: the tuner is
#: powered down, so THERE IS NO GAIN CONTROL below this, and `rtl_power` cannot be put
#: into that mode at all — it hardcodes the I branch, which this hardware does not wire.
DIRECT_SAMPLING_MAX_HZ = 24_000_000

#: The RTL2832U's ADC clock — and the same crystal librtlsdr divides to reach a sample
#: rate. One constant because it is one oscillator: the first Nyquist zone ends at half
#: of it, every alias is `ADC_RATE_HZ − f`, and every legal rate is this number over a
#: 22-bit ratio.
ADC_RATE_HZ = 28_800_000

#: The ADC samples at 28.8 MHz, so the first Nyquist zone ends here. Above it a signal
#: aliases to `28.8 MHz − f` and arrives MIRRORED, with sideband sense inverted. Sections
#: above this line are real but come with images of the strong stations below them.
NYQUIST_HZ = ADC_RATE_HZ // 2

#: The honest span for a single-hop live view AT 2.4 MS/s. Not the full sample rate: the
#: R820T2's IF filter rolls off across the outer ~15%, which reads on a waterfall as
#: "that side of the band is dead" — a lie of exactly the kind `uncovered` exists to
#: prevent in the sweep path. `TRUSTED_FILL` generalises it to the other rates.
LIVE_FAST_MAX_HZ = 2_000_000

#: How much of a capture is worth drawing, as (numerator, denominator) of its sample
#: rate. 5/6 of 2.4 MS/s is exactly `LIVE_FAST_MAX_HZ`, so this generalises that number
#: rather than competing with it — and it is the standard every other rate here is held
#: to, which is why `mw` is not sampled at a rate that fills 97% of its own passband.
TRUSTED_FILL = (5, 6)

#: What `rtlsdr_set_sample_rate` accepts, inclusive. Everything else returns -EINVAL:
#: `rate <= 225000`, `rate > 3200000`, and the whole band `300000 < rate <= 900000`.
#: **900 kS/s therefore does not exist** — it is inside the excluded band — and the
#: usable neighbour above the gap is 1.024 MS/s.
LEGAL_RATE_RANGES: tuple[tuple[int, int], ...] = (
    (225_001, 300_000),
    (900_001, 3_200_000),
)

#: The finest bin a live row may declare, mirroring the sidecar's `MIN_SWEEP_BIN_HZ`.
#: Mirrored rather than shared for the reason the tuner range is — and load-bearing in a
#: nastier way: `Sweep.of` CLAMPS to it instead of raising, so a section whose `rate / N`
#: came out at 61 Hz would ship frames whose declared `bin_hz` disagrees with the FFT
#: that produced them. A wrong picture nothing contradicts, rather than a refusal.
MIN_LIVE_BIN_HZ = 100

#: The most bins one live row may carry. Ours now, not `rtl_power`'s: every bin crosses
#: the wire once per frame per viewer, and the plan's budget was measured at this width
#: (~5.7 kB gzipped per frame). It is a WIRE budget, not an FFT limit — nothing about
#: the transform needs a ceiling, and nothing about it needs a power of two.
LIVE_MAX_BINS = 4096

#: The capture rates a one-hop live view may use, coarsest last, each paired with the
#: FFT size that divides it EXACTLY. Both halves are chosen, not defaulted:
#:
#: - The rate must be legal (`LEGAL_RATE_RANGES`) and must come back off librtlsdr's
#:   divider unchanged (`achieved_rate_hz`), or `bin_hz` flaps in its last place and
#:   the PWA re-blanks its waterfall on every frame.
#: - `N` follows from the rate so `rate / N` is an integer. It is deliberately NOT
#:   fixed at 4096 and NOT required to be a power of two: 2.4 MS/s over 4096 bins is
#:   585.9375 Hz, which rounds to a frame that is 256 Hz out at the top, while N=4000
#:   is 600 Hz exactly and numpy's pocketfft is just as fast on any 5-smooth size.
#:
#: Below the 300-900 kHz legal gap there are only rates around 250 kS/s, and those are
#: for the DIRECT path alone: with the tuner in circuit the R820T2's IF filter cannot
#: narrow that far, and `rtl_fm` — the only other thing here that captures I/Q — keeps
#: the ADC at a megasample or more for the same reason (`downsample = 1e6/rate + 1`).
LIVE_CAPTURES: tuple[tuple[int, int], ...] = (
    (256_000, 1_024),  # 250 Hz bins — HF only
    (1_024_000, 4_096),  # 250 Hz
    (1_600_000, 4_000),  # 400 Hz
    (2_048_000, 4_096),  # 500 Hz
    (2_400_000, 4_000),  # 600 Hz
)

#: The FFT size for each capture rate, so a section stores the rate alone and the bin
#: count is derived rather than retyped per row.
FFT_BINS_FOR: dict[int, int] = dict(LIVE_CAPTURES)

#: rtl_power's own per-hop ceiling (`MAXIMUM_RATE` in rtl_power.c). The hop count follows
#: from it, and the hop count is what drives duty cycle and therefore `sweep_seconds`.
HOP_MAX_HZ = 2_800_000

#: How a section can be watched live.
LIVE_FAST = "fast"  # one hop; rtl_sdr + FFT; ~10 fps; the radio never looks away
LIVE_SLOW = "slow"  # rtl_power streamed; 1 fps; n hops; reduced duty, and we say so
LIVE_NONE = "none"  # too wide or too bursty to show honestly — survey it instead


def achieved_rate_hz(rate_hz: int) -> float:
    """What the dongle really samples at when asked for `rate_hz`.

    librtlsdr does not take a rate, it takes a divider: `ratio = xtal * 2**22 / rate`,
    truncated, with the bottom two bits cleared and bit 27 copied up into bit 28 the way
    the resampler register is laid out. Ask for 1.5 MS/s and the hardware runs at
    1,500,000.0149; ask for 250 kS/s and it runs at 250,000.0004. A `bin_hz = rate / N`
    derived from THAT flaps in its last place, and a flapping `bin_hz` re-blanks the
    PWA's waterfall and re-freezes its colour scale on every frame (§6.8). So the rates
    in this table have to come back unchanged, and this is what proves they do."""
    if rate_hz <= 0:
        raise ValueError("a sample rate must be positive")
    ratio = ((ADC_RATE_HZ * (1 << 22)) // rate_hz) & 0x0FFFFFFC
    real = ratio | ((ratio & 0x08000000) << 1)
    return (ADC_RATE_HZ * (1 << 22)) / real


def rate_is_legal(rate_hz: int) -> bool:
    """Whether `rtlsdr_set_sample_rate` would accept this rate at all."""
    return any(low <= rate_hz <= high for low, high in LEGAL_RATE_RANGES)


def rate_is_exact(rate_hz: int) -> bool:
    """Legal AND achieved unchanged through the divider — both, or neither counts."""
    return rate_is_legal(rate_hz) and achieved_rate_hz(rate_hz) == float(rate_hz)


def trusted_span_hz(rate_hz: int) -> int:
    """How much of a capture at this rate is worth drawing (`TRUSTED_FILL`)."""
    numerator, denominator = TRUSTED_FILL
    return rate_hz * numerator // denominator


def bin_width_hz(rate_hz: int, bins: int) -> int | float:
    """`rate / bins` — an int when the division is exact, a float when it is not.

    The same convention as the sidecar's `iq.bin_width_hz`, deliberately: never rounded,
    because `Frame.bin_hz` goes on the wire and the PWA compares it exactly, so a
    rounded 585.9375 -> 586 is both a mislabelled frame and a value that flaps against
    the one the next component computes. Duplicated rather than shared for the reason
    the tuner range is — `deploy/sdr/` ships in its own container and imports nothing
    from here — and returning a float rather than rounding is what makes an inexact
    pairing visible at the type instead of on a waterfall that is 256 Hz out at the top.
    """
    if bins <= 0:
        raise ValueError("a frame needs at least one bin")
    return rate_hz // bins if rate_hz % bins == 0 else rate_hz / bins


def window_holds(rate_hz: int, centre_hz: int) -> bool:
    """Whether a DIRECT-path capture here is non-redundant: `R/2 <= fc <= 14.4 - R/2`.

    Direct sampling digitises a real signal, so a capture that reaches below 0 Hz sees
    a mirror of what is just above it (with the ADC's own DC offset sitting in the
    middle of the picture), and one that reaches past 14.4 MHz folds the next Nyquist
    zone onto itself INSIDE THE SAME FRAME. Neither is separable afterwards: the two
    contributions are summed in one bin."""
    return rate_hz <= 2 * centre_hz and rate_hz <= 2 * (NYQUIST_HZ - centre_hz)


def capture_for(start_hz: int, stop_hz: int) -> tuple[int, int] | None:
    """The smallest capture that draws this range in ONE hop — `(rate, N)` — or None.

    None is not a refusal. It means no single capture covers the range, which is the
    rtl_power tier's job: several hops, one row a second, and a duty cycle the picker
    says out loud. The expert path uses this where a curated section uses its own
    `sample_rate_hz`, so a hand-entered range and a band button behave the same way.

    A range that STRADDLES 24 MHz gets None as well, and not for want of bandwidth: the
    tuner is in circuit on one side of that line and powered down on the other, so no
    single capture exists that could cover both halves."""
    span = stop_hz - start_hz
    if span <= 0:
        return None
    if start_hz < DIRECT_SAMPLING_MAX_HZ < stop_hz:
        return None
    centre = (start_hz + stop_hz) // 2
    direct = stop_hz <= DIRECT_SAMPLING_MAX_HZ
    for rate, bins in LIVE_CAPTURES:
        if span > trusted_span_hz(rate):
            continue
        if direct and not window_holds(rate, centre):
            continue
        if not direct and rate <= LEGAL_RATE_RANGES[0][1]:
            # Everything below the 300-900 kHz legal gap is HF-only (`LIVE_CAPTURES`):
            # with the tuner in circuit the R820T2's IF filter cannot narrow that far.
            continue
        return rate, bins
    return None


#: The rate a hopping sweep captures each hop at. The widest this radio takes without
#: leaving librtlsdr's legal set, so it needs the fewest hops — and the hop count is
#: what a sweep's frame rate is made of, because the retune between hops costs far more
#: than the samples do.
HOP_RATE_HZ = 2_400_000
#: FFT sizes a hop may use, finest first. A hop sweep is an OVERVIEW: the whole point is
#: seeing 20 MHz at once, and 600 Hz bins across that is 33,000 columns nobody asked for
#: and nothing can draw. The planner takes the finest that fits the frame budget.
HOP_BINS_LADDER = (4096, 2048, 1024, 512, 256, 128, 64)
#: More hops than this and the picture is too old at one end to be one picture: each bin
#: is measured once per sweep, so a 30-hop sweep is a 30-way time smear pretending to be
#: an instant.
MAX_HOPS = 16


def hop_plan(
    start_hz: int, stop_hz: int, max_bins: int = LIVE_MAX_BINS
) -> tuple[int, int, int] | None:
    """`(rate, bins per hop, hops)` for a span too wide for one capture, or None.

    The I/Q engine can retune on a LIVE stream with no rebuild — F0 measured that on
    this hardware — so a wide span is several captures stitched into one row rather
    than a job for `rtl_power`. That matters because `rtl_power`'s one row a second is
    not a property of hopping: it is `if (interval < 1) interval = 1;` in its own C,
    and this engine has no such clamp.

    Only the tuner side. Below 24 MHz the ADC is fed directly and each hop's centre
    would have to satisfy the Nyquist window separately (`window_holds`); a wide
    shortwave span stays refused rather than being drawn from hops that fold. Above it,
    every hop is the same tuner doing what it already does.

    The bin count is chosen against a FRAME BUDGET rather than for resolution. Each hop
    contributes only the trusted middle of its capture (`TRUSTED_FILL`), so the total
    is `hops * usable`, and the ladder is walked finest-first for the best picture that
    still fits. On 88-108 MHz that lands at 9,375 Hz bins where `rtl_power` gives
    19,531 — finer AND faster, which is the whole argument for owning the samples."""
    span = stop_hz - start_hz
    if span <= 0 or capture_for(start_hz, stop_hz) is not None:
        return None
    if start_hz < DIRECT_SAMPLING_MAX_HZ:
        return None
    for bins in HOP_BINS_LADDER:
        usable = hop_usable_bins(bins)
        if usable <= 0:
            continue
        width = bin_width_hz(HOP_RATE_HZ, bins)
        hops = -(-span // int(usable * width)) if width else 0
        if hops < 2 or hops > MAX_HOPS:
            continue
        if usable * hops <= max_bins:
            return HOP_RATE_HZ, bins, int(hops)
    return None


def hop_usable_bins(bins: int) -> int:
    """The trusted middle of one hop's capture, in bins, and always EVEN.

    Even because the hop's centre sits on the boundary between its two middle bins, so
    an odd count cannot be placed symmetrically around it — and a half-bin offset that
    accumulates across a dozen hops is a picture whose axis drifts from its own label."""
    numerator, denominator = TRUSTED_FILL
    return (bins * numerator // denominator) // 2 * 2


def hop_centres(start_hz: int, plan: tuple[int, int, int]) -> list[int]:
    """Where to tune for each hop, so the trusted middles tile without gap or overlap.

    Derived from the same arithmetic the stitcher uses rather than from the span, so
    the two cannot disagree about which bin a hop owns."""
    rate_hz, bins, hops = plan
    usable = hop_usable_bins(bins)
    width = bin_width_hz(rate_hz, bins)
    return [
        int(round(start_hz + (index * usable + usable / 2) * width))
        for index in range(hops)
    ]


@dataclass(frozen=True, slots=True)
class Channel:
    """A named fixed frequency inside a section.

    Some bands are allocated by channel rather than by sub-range — marine, NOAA, GMRS,
    MURS — and for those a range with a step is the wrong model: it offers hundreds of
    frequencies where seven exist. Others (airband) are allocated by facility, so the
    channels are hints rather than the whole story."""

    hz: int
    name: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Section:
    """One tunable, viewable piece of a band, with everything the pipeline needs.

    Every field here is something the sidecar, the API or the sweep reducer would
    otherwise have to guess — and each guess is a bug the owner would have to diagnose
    from a symptom. `channel_hz` is the sharpest example: it both folds adjacent bins
    into one signal AND sizes the neighbourhood the steady-carrier detector judges a bin
    against. Measured on this box: FM broadcast reports 11 stations at the default and
    13 with `channel_hz` set correctly, and the two it hides are the weak ones."""

    id: str
    band: str
    name: str
    start_hz: int
    stop_hz: int
    mode: str
    #: What ± moves by. Follows the band, not the owner: 5 kHz on 2 m, 25 kHz on airband,
    #: 200 kHz on the FM dial. Zero where the section is a channel list.
    step_hz: int
    #: Channel spacing. **Load-bearing in the sweep reducer**, not documentation. Zero
    #: means the band is continuous (SSB, ISM telemetry) and bins must not be folded.
    channel_hz: int
    note: str = ""
    live: str = LIVE_NONE
    #: The bin width to ASK rtl_power for. It grants the largest power-of-two division
    #: of the per-hop bandwidth that is no coarser than this, so a round number lands
    #: somewhere else entirely — `validate()` reports what each row will actually get.
    sweep_bin_hz: int = 5_000
    #: Seconds to hold the radio for a survey. **Rises with hop count**, because a
    #: multi-hop sweep watches each bin for a fraction of the time and needs more
    #: revisits before occupancy means anything. Nothing else in the system knows this.
    sweep_seconds: int = 120
    #: Whether the signals here are ALWAYS ON — broadcast carriers, not conversations.
    #: It decides whether a low duty cycle costs anything: a multi-hop sweep that watches
    #: each bin a tenth of the time misses bursts and misses nothing at all on a band of
    #: continuous carriers. It is also which sweep output is interesting, since `steady`
    #: finds exactly the carriers that never key down.
    continuous: bool = False
    #: What the radio SAMPLES to draw this section live, in one hop. Zero on the tiers
    #: `rtl_power` still serves (`slow`, `none`), where the tool picks its own.
    #:
    #: Per section rather than one global 2.4 MS/s, because a global rate is a lie in
    #: both directions: `sw-41m` is 250 kHz wide, so 2.4 MS/s would draw 6.125-8.525 MHz
    #: under a button that says 7.200-7.450, while `20m` at 2.4 MS/s would run past
    #: 14.4 MHz and fold the next Nyquist zone onto the picture. The bin count follows
    #: from the rate (`fft_bins`), so this one number decides the whole capture.
    sample_rate_hz: int = 0
    channels: tuple[Channel, ...] = field(default_factory=tuple)

    @property
    def span_hz(self) -> int:
        return self.stop_hz - self.start_hz

    @property
    def centre_hz(self) -> int:
        return (self.start_hz + self.stop_hz) // 2

    @property
    def hops(self) -> int:
        """How many retunes rtl_power needs to cover this section."""
        return max(1, math.ceil(self.span_hz / HOP_MAX_HZ))

    @property
    def duty(self) -> float:
        """Roughly the fraction of a sweep interval each bin is actually observed.

        One hop means the tool reads back to back and never retunes, so the answer is 1.
        Beyond that every visit pays ~5.9 ms of retune and settle for ~3.9 ms of data,
        which is where the cliff comes from."""
        if self.hops == 1:
            return 1.0
        return round(3.9 / (3.9 + 5.9) / self.hops * 2, 4)

    @property
    def direct_sampling(self) -> bool:
        """Whether this section is reached with the tuner bypassed (HF)."""
        return self.stop_hz <= DIRECT_SAMPLING_MAX_HZ

    @property
    def fft_bins(self) -> int:
        """Bins in one live row here — derived from the rate, never a fixed 4096.

        Zero where no capture rate is set, which is the `rtl_power` tiers: there the
        tool grants a power-of-two division of its own choosing and this table has no
        say in it."""
        return FFT_BINS_FOR.get(self.sample_rate_hz, 0)

    @property
    def live_bin_hz(self) -> int | float:
        """The width of one bin of a live row here, or 0 on the `rtl_power` tiers."""
        return bin_width_hz(self.sample_rate_hz, self.fft_bins) if self.fft_bins else 0

    @property
    def capture_start_hz(self) -> int:
        """The low edge of what the radio actually DIGITISES for this section.

        Wider than the section itself, always: the trusted fill leaves the IF rolloff
        outside the band, and on the narrow rows the nearest legal rate is wider still.
        A frame is the PASSBAND, not the section — `mw`'s capture reaches 2.14 MHz, so
        the whole CB image at 1.395-1.835 lands inside a picture whose button says
        0.530-1.700."""
        return self.centre_hz - self.sample_rate_hz // 2

    @property
    def capture_stop_hz(self) -> int:
        return self.centre_hz + self.sample_rate_hz // 2

    @property
    def image_start_hz(self) -> int:
        """The low edge of the reversed image this section carries, or 0.

        Replaces `mirrored`, which was both dead and mis-stated: every section here
        stops at or below 14.35 MHz so the flag was False for all ten HF rows — while
        all ten in fact carry an image, because the ADC samples at 28.8 MHz and
        everything at `28.8 MHz − f` folds onto `f` and is SUMMED into the same bins.
        Reversed, so the top of the frame maps to the bottom of the image.

        Off the PASSBAND, not the section edges: a frame is what the radio digitises
        (`capture_start_hz`), and it is wider — `mw` is captured 0.091-2.139 MHz under
        a button that says 0.530-1.700, so a caveat derived from the edges would leave
        ~440 kHz of folded band unmentioned at each end, and the two loudest sources of
        it (CB at 27 MHz) sit exactly in that margin. Nothing in software can separate
        the two contributions; saying which band is folded in is the only honest thing
        available."""
        return ADC_RATE_HZ - self._frame_stop_hz if self.direct_sampling else 0

    @property
    def image_stop_hz(self) -> int:
        return ADC_RATE_HZ - self._frame_start_hz if self.direct_sampling else 0

    @property
    def _frame_start_hz(self) -> int:
        """What a picture of this section really covers — the passband where there is a
        capture, the declared edges where rtl_power picks its own hops and this table
        has no say in how wide a frame comes out."""
        return self.capture_start_hz if self.sample_rate_hz else self.start_hz

    @property
    def _frame_stop_hz(self) -> int:
        return self.capture_stop_hz if self.sample_rate_hz else self.stop_hz

    @property
    def surveyable(self) -> bool:
        """Whether a sweep can reach it at all.

        False for every HF section, and not by choice: `rtl_power -D` hardcodes direct
        sampling mode 1 (the I branch) while this hardware wires the Q branch. Fixing it
        means patching a C tool, so HF listening works and HF sweeping does not."""
        return not self.direct_sampling


def _s(**kw: object) -> Section:
    return Section(**kw)  # type: ignore[arg-type]


#: The table. Ordered as a person would browse it — the everyday bands first, the
#: amateur bands next, HF last because it needs a different antenna to be worth anything.
#:
#: **Repeater INPUTS are deliberately absent.** On an input you hear only the half of a
#: conversation from stations close enough to reach you directly; the repeater's output —
#: the interesting side, carrying both halves — is elsewhere. A band list for a RECEIVER
#: should not offer them as though they were equivalent.
SECTIONS: tuple[Section, ...] = (
    # --- broadcast and public service ----------------------------------------------
    _s(
        id="fm-broadcast",
        band="FM broadcast",
        name="The dial",
        start_hz=88_000_000,
        stop_hz=108_000_000,
        mode="wbfm",
        step_hz=200_000,
        channel_hz=200_000,
        note="Commercial FM. Carriers are up around the clock, so nothing is missed at "
        "one frame a second and the whole dial is worth watching at once.",
        live=LIVE_SLOW,
        sweep_bin_hz=25_000,
        sweep_seconds=60,
        continuous=True,
    ),
    _s(
        id="wx",
        band="NOAA weather",
        name="WX 1-7",
        start_hz=162_400_000,
        stop_hz=162_550_000,
        mode="fm",
        step_hz=25_000,
        channel_hz=25_000,
        note="Seven channels, continuous automated forecasts. Most sites are "
        "rubidium-locked, which makes these the easiest frequency reference here.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=5_000,
        sweep_seconds=60,
        channels=tuple(
            Channel(hz, f"WX{n}")
            for n, hz in enumerate(
                (
                    162_400_000,
                    162_425_000,
                    162_450_000,
                    162_475_000,
                    162_500_000,
                    162_525_000,
                    162_550_000,
                ),
                start=1,
            )
        ),
    ),
    _s(
        id="air-tower",
        band="Airband",
        name="Tower and ground",
        start_hz=118_000_000,
        stop_hz=120_000_000,
        mode="am",
        step_hz=25_000,
        channel_hz=25_000,
        note="AM, not FM — aircraft use it so two stations transmitting at once beat "
        "audibly instead of one capturing the other, which is what you want when "
        "the message matters.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    _s(
        id="air-guard",
        band="Airband",
        name="Guard and emergency",
        start_hz=121_000_000,
        stop_hz=122_000_000,
        mode="am",
        step_hz=25_000,
        channel_hz=25_000,
        note="121.500 is the international emergency frequency, monitored everywhere "
        "and almost always silent. 121.600-121.900 is usually ground control.",
        live=LIVE_FAST,
        sample_rate_hz=1_600_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
        channels=(Channel(121_500_000, "Guard", "international emergency"),),
    ),
    _s(
        id="air-unicom",
        band="Airband",
        name="CTAF and UNICOM",
        start_hz=122_000_000,
        stop_hz=123_600_000,
        mode="am",
        step_hz=25_000,
        channel_hz=25_000,
        note="Uncontrolled-field traffic advisories — the busiest airband segment away "
        "from a big airport.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
        channels=(
            Channel(122_700_000, "CTAF"),
            Channel(122_800_000, "CTAF"),
            Channel(122_900_000, "Multicom"),
            Channel(123_000_000, "UNICOM"),
        ),
    ),
    _s(
        id="air-centre",
        band="Airband",
        name="Enroute (ARTCC)",
        start_hz=132_000_000,
        stop_hz=134_000_000,
        mode="am",
        step_hz=25_000,
        channel_hz=25_000,
        note="High-altitude centre sectors. Quiet on the ground unless you are under a "
        "airway, and worth a survey before a listen.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    _s(
        id="marine",
        band="Marine VHF",
        name="Ship and coast",
        start_hz=156_000_000,
        stop_hz=157_500_000,
        mode="fm",
        step_hz=25_000,
        channel_hz=25_000,
        note="Channel 16 is the distress and calling frequency and is monitored "
        "continuously. Channel 70 carries DSC data, not voice.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
        channels=(
            Channel(156_450_000, "Ch 09", "hailing"),
            Channel(156_525_000, "Ch 70", "DSC data — not voice"),
            Channel(156_650_000, "Ch 13", "bridge to bridge"),
            Channel(156_800_000, "Ch 16", "distress and calling"),
        ),
    ),
    _s(
        id="gmrs",
        band="GMRS and FRS",
        name="Main channels",
        start_hz=462_500_000,
        stop_hz=462_750_000,
        mode="fm",
        step_hz=12_500,
        channel_hz=25_000,
        note="Family radios and GMRS simplex. The 467 MHz side is repeater inputs and "
        "half-watt handhelds, so this is the half worth listening to.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    _s(
        id="murs",
        band="MURS",
        name="All five channels",
        start_hz=151_800_000,
        stop_hz=154_650_000,
        mode="fm",
        step_hz=0,
        channel_hz=12_500,
        note="Five licence-free VHF channels, often used by shops and site crews.",
        live=LIVE_NONE,
        sweep_bin_hz=5_000,
        sweep_seconds=300,
        channels=(
            Channel(151_820_000, "MURS 1"),
            Channel(151_880_000, "MURS 2"),
            Channel(151_940_000, "MURS 3"),
            Channel(154_570_000, "MURS 4"),
            Channel(154_600_000, "MURS 5"),
        ),
    ),
    # --- 2 m --------------------------------------------------------------------------
    _s(
        id="2m-ssb",
        band="2 m",
        name="Weak signal and SSB",
        start_hz=144_100_000,
        stop_hz=144_300_000,
        mode="usb",
        step_hz=1_000,
        channel_hz=0,
        note="Upper sideband, not FM. 144.200 is the national SSB calling frequency; "
        "this is where long-distance work on 2 m happens.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        channels=(Channel(144_200_000, "Calling", "national SSB calling"),),
    ),
    _s(
        id="2m-aprs",
        band="2 m",
        name="APRS and packet",
        start_hz=144_300_000,
        stop_hz=145_100_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=15_000,
        note="144.390 is the North American APRS channel — position beacons, weather "
        "stations and messages, all digipeated. This is what the box already logs.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=5_000,
        sweep_seconds=120,
        channels=(Channel(144_390_000, "APRS", "national APRS channel"),),
    ),
    _s(
        id="2m-sat",
        band="2 m",
        name="Satellites",
        start_hz=145_800_000,
        stop_hz=146_000_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=15_000,
        note="The amateur satellite subband, including the ISS. Passes are brief and "
        "scheduled, so a survey here is only meaningful during one.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=5_000,
        sweep_seconds=120,
    ),
    _s(
        id="2m-repeaters",
        band="2 m",
        name="Repeater outputs",
        start_hz=146_000_000,
        stop_hz=148_000_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=15_000,
        note="The busiest listening segment on the band, and the one to try first. "
        "146.520 is the national FM simplex calling frequency.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=120,
        channels=(Channel(146_520_000, "Calling", "national FM simplex"),),
    ),
    _s(
        id="2m-all",
        band="2 m",
        name="Whole band (survey)",
        start_hz=144_000_000,
        stop_hz=148_000_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=15_000,
        note="Two hops, so each frequency is watched about a fifth of the time — good "
        "for finding what is active, weaker at catching a short transmission.",
        live=LIVE_SLOW,
        sweep_bin_hz=5_000,
        sweep_seconds=300,
    ),
    # --- 70 cm ------------------------------------------------------------------------
    _s(
        id="70cm-weak",
        band="70 cm",
        name="Weak signal",
        start_hz=432_000_000,
        stop_hz=433_000_000,
        mode="usb",
        step_hz=1_000,
        channel_hz=0,
        note="432.100 is the calling frequency. Upper sideband and CW, not FM.",
        live=LIVE_FAST,
        sample_rate_hz=1_600_000,
        sweep_bin_hz=1_000,
        sweep_seconds=180,
        channels=(Channel(432_100_000, "Calling"),),
    ),
    _s(
        id="70cm-low",
        band="70 cm",
        name="Repeater outputs (442-445)",
        start_hz=442_000_000,
        stop_hz=444_000_000,
        mode="fm",
        step_hz=12_500,
        channel_hz=25_000,
        note="Local-option repeater pairs. The measured busiest part of this band here.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    _s(
        id="70cm-simplex",
        band="70 cm",
        name="Simplex",
        start_hz=445_000_000,
        stop_hz=447_000_000,
        mode="fm",
        step_hz=12_500,
        channel_hz=25_000,
        note="446.000 is the national simplex calling frequency.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
        channels=(Channel(446_000_000, "Calling", "national simplex"),),
    ),
    _s(
        id="70cm-high",
        band="70 cm",
        name="Repeater outputs (447-450)",
        start_hz=447_000_000,
        stop_hz=449_000_000,
        mode="fm",
        step_hz=12_500,
        channel_hz=25_000,
        note="The upper repeater block.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    # --- other VHF/UHF amateur --------------------------------------------------------
    _s(
        id="6m-ssb",
        band="6 m",
        name="SSB and DX",
        start_hz=50_100_000,
        stop_hz=50_300_000,
        mode="usb",
        step_hz=1_000,
        channel_hz=0,
        note="The magic band. Dead most of the time, then openings carry signals "
        "thousands of miles. 50.125 is the calling frequency.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        channels=(Channel(50_125_000, "Calling"),),
    ),
    _s(
        id="6m-fm",
        band="6 m",
        name="FM simplex and repeaters",
        start_hz=52_500_000,
        stop_hz=53_000_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=20_000,
        note="52.525 is the primary FM simplex frequency.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=5_000,
        sweep_seconds=120,
        channels=(Channel(52_525_000, "Calling", "primary FM simplex"),),
    ),
    _s(
        id="125cm",
        band="1.25 m",
        name="Repeater outputs",
        start_hz=223_850_000,
        stop_hz=225_000_000,
        mode="fm",
        step_hz=5_000,
        channel_hz=20_000,
        note="A quiet band with a loyal following. 223.500 is the simplex calling "
        "frequency, just below this range.",
        live=LIVE_FAST,
        sample_rate_hz=1_600_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    _s(
        id="ism-433",
        band="ISM 433",
        name="Telemetry and sensors",
        start_hz=433_050_000,
        stop_hz=434_790_000,
        mode="fm",
        step_hz=25_000,
        channel_hz=0,
        note="Weather stations, tyre sensors, doorbells and remotes. Short bursts all "
        "day. We can see them here but cannot decode them yet.",
        live=LIVE_FAST,
        sample_rate_hz=2_400_000,
        sweep_bin_hz=5_000,
        sweep_seconds=180,
    ),
    # --- HF (direct sampling; listen only) --------------------------------------------
    _s(
        id="mw",
        band="Medium wave",
        name="AM broadcast",
        start_hz=530_000,
        stop_hz=1_700_000,
        mode="am",
        step_hz=10_000,
        channel_hz=10_000,
        note="Local AM stations, and at night distant ones. The strongest signals HF "
        "will see — if the rest of HF sounds overloaded, this is usually why.",
        live=LIVE_FAST,
        # 2.048 rather than the 1.44 the span alone would take: the honest window
        # (`R/2 = 1.024 <= fc = 1.115`) holds, the whole MW band clears the IF
        # rolloff at 57% fill, and the rate is exact where 1.5 MS/s is not.
        sample_rate_hz=2_048_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
    ),
    _s(
        id="sw-49m",
        band="Shortwave",
        name="49 m broadcast",
        start_hz=5_900_000,
        stop_hz=6_200_000,
        mode="am",
        step_hz=5_000,
        channel_hz=5_000,
        note="The most reliable shortwave broadcast band after dark.",
        live=LIVE_FAST,
        # 300 kHz of band, and 300 kS/s would fill 100% of its own passband. The
        # next legal rate up is not 900 kS/s — that is inside the excluded band —
        # so the neighbour is 1.024 MS/s, and the fill is low rather than dishonest.
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
    ),
    _s(
        id="sw-41m",
        band="Shortwave",
        name="41 m broadcast",
        start_hz=7_200_000,
        stop_hz=7_450_000,
        mode="am",
        step_hz=5_000,
        channel_hz=5_000,
        note="Evening broadcast band, sharing space with the 40 m amateur band below it.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
    ),
    _s(
        id="sw-31m",
        band="Shortwave",
        name="31 m broadcast",
        start_hz=9_400_000,
        stop_hz=9_900_000,
        mode="am",
        step_hz=5_000,
        channel_hz=5_000,
        note="Works day and night — the best band to try first on a new antenna.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
    ),
    _s(
        id="sw-25m",
        band="Shortwave",
        name="25 m broadcast",
        start_hz=11_600_000,
        stop_hz=12_100_000,
        mode="am",
        step_hz=5_000,
        channel_hz=5_000,
        note="A daytime band, best in the hours around dusk.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
    ),
    _s(
        id="80m",
        band="80 m",
        name="Voice (LSB)",
        start_hz=3_600_000,
        stop_hz=4_000_000,
        mode="lsb",
        step_hz=100,
        channel_hz=0,
        note="Lower sideband by convention below 10 MHz. Regional at night, and where "
        "most evening nets live.",
        live=LIVE_FAST,
        sample_rate_hz=1_024_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
    ),
    _s(
        id="40m",
        band="40 m",
        name="Voice (LSB)",
        start_hz=7_125_000,
        stop_hz=7_300_000,
        mode="lsb",
        step_hz=100,
        channel_hz=0,
        note="The dependable band — regional by day, continental at night.",
        live=LIVE_FAST,
        sample_rate_hz=256_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
    ),
    _s(
        id="20m",
        band="20 m",
        name="Voice (USB)",
        start_hz=14_150_000,
        stop_hz=14_350_000,
        mode="usb",
        step_hz=100,
        channel_hz=0,
        note="The long-distance daytime band, and the last one that fits below the "
        "14.4 MHz mirror boundary. Upper sideband above 10 MHz.",
        live=LIVE_FAST,
        # FORCED, not chosen: the section runs to 14.35 MHz, so the window leaves
        # `R <= 2 * (14.4 - 14.25) = 300 kHz`. 256 kS/s is the exact rate under it —
        # 250 kS/s achieves 250,000.0004 — and N=1024 keeps bins at 250 Hz, well
        # clear of the 100 Hz floor `Sweep.of` would silently clamp to.
        sample_rate_hz=256_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
    ),
    _s(
        id="wwv-5",
        band="Time signal",
        name="WWV 5 MHz",
        start_hz=4_900_000,
        stop_hz=5_100_000,
        mode="am",
        step_hz=1_000,
        channel_hz=0,
        note="Standard time and frequency from Fort Collins — ticks every second, a "
        "voice announcement each minute, and the most exactly-known carrier "
        "reachable from here. The right first test of an HF antenna, because if "
        "you hear nothing the antenna is the answer.",
        live=LIVE_FAST,
        sample_rate_hz=256_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
        channels=(Channel(5_000_000, "WWV"),),
    ),
    _s(
        id="wwv-10",
        band="Time signal",
        name="WWV 10 MHz",
        start_hz=9_900_000,
        stop_hz=10_100_000,
        mode="am",
        step_hz=1_000,
        channel_hz=0,
        note="The same broadcast as 5 MHz. Whichever of the two is stronger tells you "
        "something about the ionosphere on its own.",
        live=LIVE_FAST,
        sample_rate_hz=256_000,
        sweep_bin_hz=1_000,
        sweep_seconds=120,
        continuous=True,
        channels=(Channel(10_000_000, "WWV"),),
    ),
)

#: The APRS channel, by name rather than as a float repeated in three files. It was
#: `APRS_DEFAULT_MHZ = 144.39` in both `api/sdr.py` and `agent/sdrtools.py`.
APRS_HZ = 144_390_000


def by_id(section_id: str) -> Section | None:
    return next((s for s in SECTIONS if s.id == section_id), None)


def by_edges(start_hz: int, stop_hz: int) -> Section | None:
    """The section whose edges are EXACTLY these, or None.

    What makes the expert path and the band button the same question. A hand-typed
    144.100-144.300 is the 2 m SSB row said in numbers, and without this lookup the two
    are answered by different code: the button gets the rate someone chose while reading
    a band plan, the numbers get the smallest capture that covers them — and for five
    rows those deliberately differ (`mw` is sampled at 2.048 MS/s so `R/2 <= fc` holds,
    where the derived answer is 1.6). Same range, two pictures, no way to tell which
    one is on screen.

    Exact rather than nearest: a range that is not a section's edges is not that
    section, and inheriting a curated rate for a span it does not cover would draw the
    band's edge inside the IF rolloff. Those keep the derived answer."""
    return next((s for s in SECTIONS if (s.start_hz, s.stop_hz) == (start_hz, stop_hz)), None)


def containing(hz: int) -> Section | None:
    """The narrowest section covering this frequency, or None.

    Narrowest because the wide survey rows overlap the specific ones and a caller
    asking "what am I listening to" wants "2 m repeater outputs", not "2 m whole band".
    This is also what gives expert mode its defaults: a manually entered frequency
    inherits the mode, step and channel spacing of wherever it lands."""
    covering = [s for s in SECTIONS if s.start_hz <= hz <= s.stop_hz]
    return min(covering, key=lambda s: s.span_hz) if covering else None


#: What a frequency gets when NO curated section covers it — the expert path, typing a
#: number. Coarse on purpose: these are the broad conventions of the spectrum, not a band
#: plan, and their job is to be un-surprising rather than right. Ordered, first match
#: wins, and the last entry catches everything above.
_FALLBACKS: tuple[tuple[int, str, int, int], ...] = (
    # up to Hz,      mode,  step,    channel spacing
    (1_800_000, "am", 10_000, 10_000),  # medium wave, 10 kHz channels in North America
    (24_000_000, "am", 5_000, 5_000),  # shortwave broadcast is AM on a 5 kHz grid
    (87_500_000, "fm", 12_500, 25_000),  # low VHF: land mobile and 6 m
    (108_000_000, "wbfm", 100_000, 200_000),  # the FM broadcast dial
    (137_000_000, "am", 25_000, 25_000),  # airband is AM, and getting this wrong is silence
    (1_766_000_000, "fm", 12_500, 25_000),  # everything else narrowband
)


def defaults_for(hz: int) -> tuple[str, int, int]:
    """Mode, tuning step and channel spacing for any frequency the radio can reach.

    The curated section when there is one, and a convention when there is not — because
    most of the spectrum is not in the table and never will be, while all of it is
    tunable. Without this the expert path would have to make the owner choose a
    demodulator before they could hear anything, and the commonest wrong choice is
    silent: FM on an airband frequency produces a hiss with a voice buried in it that
    never quite resolves, which reads as a dead channel rather than a wrong setting."""
    section = containing(hz)
    fallback = next(
        ((m, s, c) for ceiling, m, s, c in _FALLBACKS if hz <= ceiling),
        ("fm", 12_500, 25_000),
    )
    if section is None:
        return fallback
    # A CHANNEL-LIST section carries no step — MURS is five fixed frequencies, so
    # stepping through the 2.8 MHz between them is meaningless and `step_hz` is 0. But
    # the expert path can still land there by typing a number, and a step of zero is a
    # pair of ± buttons that do nothing. Take the section's mode, borrow the
    # convention's step.
    return section.mode, section.step_hz or fallback[1], section.channel_hz


def _five_smooth(n: int) -> bool:
    """Whether `n` factors into 2s, 3s and 5s — what pocketfft is fast on.

    The reason `fft_bins` is free to be 4000 instead of 4096. A prime N is not wrong,
    it is O(n**2)-ish, and at ten frames a second that is the difference between 10% of
    a core and the sidecar falling behind the radio."""
    if n <= 0:
        # Not a size, and the loop below would divide 0 by 2 forever looking for one.
        return False
    for prime in (2, 3, 5):
        while n % prime == 0:
            n //= prime
    return n == 1


def ladder_problems(captures: tuple[tuple[int, int], ...] = LIVE_CAPTURES) -> list[str]:
    """Everything that can be wrong with a `(rate, N)` pairing, checked once.

    Here rather than per row because these are facts about the LADDER, not about any
    band: a rate the driver quantises, or an N that does not divide it, is wrong for
    every section that names it. And none of it raises at runtime — the divider is
    silent, and `Sweep.of` clamps the bin width instead of refusing — so the only place
    it can be caught is a test over the checked-in numbers."""
    problems: list[str] = []
    for rate, bins in captures:
        where = f"{rate} Hz:"
        if bins <= 0:
            problems.append(f"{where} {bins} is not a frame size")
            continue
        if not rate_is_legal(rate):
            problems.append(
                f"{where} not a rate librtlsdr accepts — it takes "
                f"{LEGAL_RATE_RANGES[0][0]}-{LEGAL_RATE_RANGES[0][1]} or "
                f"{LEGAL_RATE_RANGES[1][0]}-{LEGAL_RATE_RANGES[1][1]} Hz"
            )
        elif achieved_rate_hz(rate) != rate:
            problems.append(
                f"{where} the divider would give {achieved_rate_hz(rate):.4f} Hz — a "
                f"bin width off that flaps in its last place, which re-blanks the "
                f"waterfall and re-freezes its colour scale on every frame"
            )
        if bins > LIVE_MAX_BINS:
            problems.append(f"{where} {bins} bins is wider than one row carries")
        if not _five_smooth(bins):
            problems.append(f"{where} {bins} bins is not 5-smooth, so the FFT is slow")
        if rate % bins:
            problems.append(
                f"{where} over {bins} bins is {rate / bins:.4f} Hz, which is not exact "
                f"— the top of the frame would be mislabelled"
            )
        elif rate // bins < MIN_LIVE_BIN_HZ:
            problems.append(
                f"{where} over {bins} bins is {rate // bins} Hz, under the "
                f"{MIN_LIVE_BIN_HZ} Hz floor — and `Sweep.of` CLAMPS rather than "
                f"refusing, so the frame would declare a bin width the FFT never used"
            )
    return problems


def _capture_problems(s: Section) -> list[str]:
    """How one section's capture can be wrong even on a sound ladder.

    Every rule here is a way for a row to produce a picture that is WRONG rather than
    absent: a rate too small for the band draws its edges inside the IF rolloff, and a
    direct capture outside the honest window folds the band onto itself in the same
    frame. Nothing raises, and nothing downstream can tell."""
    where = f"{s.id}:"
    rate = s.sample_rate_hz
    if s.live == LIVE_FAST and not rate:
        return [
            f"{where} claims live={LIVE_FAST} but names no sample_rate_hz — the one-hop "
            f"tier IS a capture, and without a rate there is no bin width either"
        ]
    if s.live != LIVE_FAST and rate:
        return [
            f"{where} is live={s.live}, which is rtl_power, and rtl_power chooses its "
            f"own rate — a sample_rate_hz here would describe a capture nothing makes"
        ]
    if not rate:
        return []
    found: list[str] = []
    if rate not in FFT_BINS_FOR:
        found.append(
            f"{where} {rate} Hz is not on the capture ladder, so nothing here knows "
            f"what N to transform it at (see LIVE_CAPTURES)"
        )
    if s.span_hz > trusted_span_hz(rate):
        found.append(
            f"{where} is {s.span_hz / 1e6:.3f} MHz wide inside a {rate / 1e6:.3f} MS/s "
            f"capture — past {trusted_span_hz(rate) / 1e6:.3f} MHz the IF rolloff reads "
            f"as a dead band edge"
        )
    if s.direct_sampling and not window_holds(rate, s.centre_hz):
        found.append(
            f"{where} centres at {s.centre_hz / 1e6:.3f} MHz, where a {rate / 1e6:.3f} "
            f"MS/s direct capture runs outside {NYQUIST_HZ / 1e6:.1f} MHz or below "
            f"0 Hz — the picture would carry a fold of itself"
        )
    if not s.direct_sampling and rate <= LEGAL_RATE_RANGES[0][1]:
        found.append(
            f"{where} is on the TUNER path, where {rate} Hz is below what the R820T2's "
            f"IF filter can bracket — those rates are for direct sampling only"
        )
    return found


def validate(sections: tuple[Section, ...] = SECTIONS) -> list[str]:
    """Re-derive the physics from the rows and report every disagreement.

    A wrong row is a lie nothing else contradicts: a section whose `live` says `fast`
    but whose span needs two hops silently hands the owner a fifth of the sensitivity
    in the same colours, and a `channel_hz` that is wrong unfolds one signal into forty
    stations. None of that raises. So the arithmetic runs in CI over the checked-in
    rows, which is the difference between a table that encodes the physics and one that
    encodes somebody's memory of it."""
    problems: list[str] = ladder_problems()
    seen: set[str] = set()
    for s in sections:
        where = f"{s.id}:"
        if s.id in seen:
            problems.append(f"{where} duplicate id")
        seen.add(s.id)
        if s.stop_hz <= s.start_hz:
            problems.append(f"{where} stop is not above start")
        if s.live == LIVE_FAST and s.span_hz > LIVE_FAST_MAX_HZ:
            problems.append(
                f"{where} claims live={LIVE_FAST} but spans {s.span_hz / 1e6:.2f} MHz — "
                f"over {LIVE_FAST_MAX_HZ / 1e6:.1f} MHz it needs a hop, so it is at best "
                f"{LIVE_SLOW}"
            )
        if s.live == LIVE_SLOW and not s.surveyable:
            # LIVE_SLOW is rtl_power, and rtl_power cannot reach the Q branch at all.
            problems.append(
                f"{where} is below {DIRECT_SAMPLING_MAX_HZ / 1e6:.0f} MHz, where a live "
                f"view cannot come from rtl_power — it hardcodes the wrong ADC branch"
            )
        problems.extend(_capture_problems(s))
        if s.hops > 1 and not s.continuous and s.sweep_seconds < 300:
            problems.append(
                f"{where} needs {s.hops} hops (duty ~{s.duty:.0%}) but surveys for only "
                f"{s.sweep_seconds}s — a multi-hop sweep watches each frequency a "
                f"fraction of the time, so it needs longer to see the same bursts"
            )
        if s.channel_hz and s.sweep_bin_hz > s.channel_hz:
            problems.append(
                f"{where} asks for {s.sweep_bin_hz} Hz bins on a {s.channel_hz} Hz "
                f"channel grid — one bin would straddle two channels"
            )
        if s.step_hz and s.channel_hz and s.channel_hz % s.step_hz:
            problems.append(
                f"{where} step {s.step_hz} does not divide the {s.channel_hz} Hz "
                f"channel spacing, so stepping walks off the grid"
            )
        for ch in s.channels:
            if not s.start_hz <= ch.hz <= s.stop_hz:
                problems.append(f"{where} channel {ch.name} at {ch.hz} is outside it")
    return problems
