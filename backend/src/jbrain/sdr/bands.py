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

#: The ADC samples at 28.8 MHz, so the first Nyquist zone ends here. Above it a signal
#: aliases to `28.8 MHz − f` and arrives MIRRORED, with sideband sense inverted. Sections
#: above this line are real but come with images of the strong stations below them.
NYQUIST_HZ = 14_400_000

#: The honest span for a single-hop live view. Not the full 2.4 MHz sample rate: the
#: R820T2's IF filter rolls off across the outer ~15%, which reads on a waterfall as
#: "that side of the band is dead" — a lie of exactly the kind `uncovered` exists to
#: prevent in the sweep path.
LIVE_FAST_MAX_HZ = 2_000_000

#: rtl_power's own per-hop ceiling (`MAXIMUM_RATE` in rtl_power.c). The hop count follows
#: from it, and the hop count is what drives duty cycle and therefore `sweep_seconds`.
HOP_MAX_HZ = 2_800_000

#: How a section can be watched live.
LIVE_FAST = "fast"  # one hop; rtl_sdr + FFT; ~10 fps; the radio never looks away
LIVE_SLOW = "slow"  # rtl_power streamed; 1 fps; n hops; reduced duty, and we say so
LIVE_NONE = "none"  # too wide or too bursty to show honestly — survey it instead


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
    def mirrored(self) -> bool:
        """Above the first Nyquist zone: real, but accompanied by images of the strong
        stations below 14.4 MHz, and with USB and LSB swapped."""
        return self.direct_sampling and self.stop_hz > NYQUIST_HZ

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


def validate(sections: tuple[Section, ...] = SECTIONS) -> list[str]:
    """Re-derive the physics from the rows and report every disagreement.

    A wrong row is a lie nothing else contradicts: a section whose `live` says `fast`
    but whose span needs two hops silently hands the owner a fifth of the sensitivity
    in the same colours, and a `channel_hz` that is wrong unfolds one signal into forty
    stations. None of that raises. So the arithmetic runs in CI over the checked-in
    rows, which is the difference between a table that encodes the physics and one that
    encodes somebody's memory of it."""
    problems: list[str] = []
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
        if s.live != LIVE_NONE and not s.surveyable and s.live == LIVE_SLOW:
            # LIVE_SLOW is rtl_power, and rtl_power cannot reach the Q branch at all.
            problems.append(
                f"{where} is below {DIRECT_SAMPLING_MAX_HZ / 1e6:.0f} MHz, where a live "
                f"view cannot come from rtl_power — it hardcodes the wrong ADC branch"
            )
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
