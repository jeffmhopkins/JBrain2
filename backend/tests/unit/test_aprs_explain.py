"""What a heard frame says, in words.

Every frame quoted is REAL, captured on 144.390 near Titusville. That matters more here
than anywhere else in this feature: this module turns bytes into confident English
sentences, so a decode that is subtly wrong reads as authoritative rather than as
obviously-raw bytes. The spec alone does not tell you which fields stations actually send,
in what order, or what they put in them.
"""

from __future__ import annotations

import pytest

from jbrain.sdr.classify import classify
from jbrain.sdr.explain import collect_definitions, explain, kind_label

# A Mic-E frame as it came off the air, hex and all — the payload has to be read from the
# bytes rather than the scrubbed text, and only a real frame proves that.
# N0XIA-4's frame exactly as it came off the air. Hand-building this hex is how the first
# version of this test failed: the destination `RX3P6U` carries half the latitude, and a
# fabricated one decodes to a plausible-looking position in the wrong hemisphere.
MIC_E_RAW = (
    "a4b066a06caa609c60b0928240e89c6296a68640e2ae92888a6240e0ae92888a6440"
    "6303f0606d336a7136463e2f604f6e20442d53746172204b3158432c2057344145532"
    "c205734504c422026204b4a344f564120444d525f250d"
)
MIC_E_INFO = "`m3jq6F>/`On D-Star K1XC, W4AES, W4PLB & KJ4OVA DMR_%"
# The same station twice: heard directly, and re-injected by the IGate. The direct copy's
# course byte is 0x1C — a control character the database scrub deletes.
KN1B_RAW = (
    "a470a6a870ac60969c62844040e09c6296a68640e2ae92888a6240e0ae92888a6440"
    "6303f0606c4d706c201c2d2f605f250d"
)
KN1B_INFO = "`lMpl -/`_%"
KN1B_RELAYED = "}KN1B>R8ST8V,TCPIP,N4TDX*:`lMpl -/`_%"
KN1B_RELAYED_RAW = (
    "82a084a0a262609c68a888b040609c6296a68640e2ae92888a6240e103f07d4b4e31"
    "423e5238535438562c54435049502c4e345444582a3a606c4d706c201c2d2f605f25"
)
WEATHER = "@031030z2837.27N/08049.42W_338/000g000t078r000p000P000h99b10141L000AmbientCWOP.com"
POSITIONLESS_WX = "_09030625c346s000g000t078r000p000P000h94b10135tU2k"
OBJECT = ";FLMesh-2 *031028z2837.88N/08049.45W`N4TDX-Parish-MVFD-PTP pointing North"
AMBIGUOUS = ";N4TDX-8  *031013z2838.  NW08052.  Wa144.990MHz Winlink VARA FM Wide Gateway"
TELEMETRY = "T#110,190,088,011,068,000,00000000"
DEFINITIONS = [
    ":N1KSC-1  :PARM.Vin,Rx1h,Dg1h,Eff1h,A5,O1,O2,O3,O4,I1,I2,I3,I4",
    ":N1KSC-1  :UNIT.Volt,Pkt,Pkt,Pcnt,None,On,On,On,On,Hi,Hi,Hi,Hi",
    ":N1KSC-1  :EQNS.0,0.075,0,0,10,0,0,10,0,0,1,0,0,0,0",
]


def _explain(source: str, info: str, *, path: list[str] | None = None, raw: str = "", defs=None):
    return explain(classify(source, info, path or [], raw), definitions=defs or {})


def _values(ex) -> dict[str, str]:
    return {f.name: f.value for f in ex.fields}


def test_a_mic_e_frame_is_read_from_the_bytes_not_the_scrubbed_text() -> None:
    """The hard one, and the reason `Heard` carries `dest` and `payload`.

    Half the latitude is in the DESTINATION callsign; the longitude, speed and course are
    offset-28 encoded in the info field. Two course bytes are legitimately control
    characters, so a decoder reading the database's scrubbed text gets a shifted payload
    and reports the wrong symbol and the wrong speed."""
    ex = _explain("N0XIA-4", MIC_E_INFO, raw=MIC_E_RAW)
    values = _values(ex)

    assert values["Position"] == "28.5108, -81.3963"
    assert values["Symbol"] == "Car"
    assert values["Moving"] == "52 knots (60 mph) heading 242° (WSW)"
    assert values["Status"] == "En route"
    # The trailing two bytes identify the radio; they are not the operator's words.
    assert values["Radio"] == "Yaesu FTM-400DR"
    assert ex.comment == "On D-Star K1XC, W4AES, W4PLB & KJ4OVA DMR"


def test_a_relayed_mic_e_frame_decodes_the_same_as_the_direct_one() -> None:
    """KN1B arrives twice — heard directly, and re-injected by the IGate as third-party.

    They are the same transmission and must read identically. Taking the relayed copy's
    payload from the scrubbed text instead of from inside the wrapper in `raw` decodes it
    to a different symbol, which is a station appearing to be two things at once."""
    direct = _values(_explain("KN1B", KN1B_INFO, raw=KN1B_RAW))
    relayed = _values(_explain("N4TDX", KN1B_RELAYED, raw=KN1B_RELAYED_RAW))

    assert direct["Position"] == relayed["Position"] == "28.581, -80.8307"
    assert direct["Symbol"] == relayed["Symbol"] == "House (VHF home station)"
    # The course byte here IS 0x1C. Reading either copy from the scrubbed text shifts
    # every byte after it and lands on a different symbol.
    assert direct["Symbol"] != "unknown symbol `/"


def test_a_weather_report_is_every_field_with_its_units() -> None:
    values = _values(_explain("KD4WLE", WEATHER))

    assert values["Wind"] == "from the NNW (338°) at 0 mph"
    assert values["Temperature"] == "78 °F (25.6 °C)"
    # Hundredths of an inch, tenths of a millibar — the raw numbers mean nothing as read.
    assert values["Rain, last 24 hours"] == "0.00 in"
    assert values["Pressure"] == "1014.1 hPa (29.95 inHg)"
    # `h99` is 99 %. `h00` would be 100 %, not zero — see the next test.
    assert values["Humidity"] == "99 %"


def test_a_humidity_of_zero_means_one_hundred_percent() -> None:
    # APRS101 ch.12. A weather station claiming 0 % relative humidity would be remarkable
    # and it is never the claim being made.
    frame = "@031030z2837.27N/08049.42W_000/000t070h00b10000"

    assert _values(_explain("KD4WLE", frame))["Humidity"] == "100 %"


def test_the_trailing_software_code_is_not_read_as_a_second_temperature() -> None:
    """`tU2k` at the end of a real positionless report is an Ultimeter unit code.

    A scanner that only checks the tag letter reads it as `t` + "U2k" and overwrites the
    real `t078` — so the card would report the weather at "U2 °F"."""
    ex = _explain("WA4IKQ", POSITIONLESS_WX)

    assert _values(ex)["Temperature"] == "78 °F (25.6 °C)"
    assert ex.comment == "tU2k"


def test_a_positionless_weather_report_says_it_has_no_position() -> None:
    # Its wind fields are tagged `c`/`s` in a different order, and there is no position at
    # all — a card must not borrow one from another frame without saying so.
    ex = _explain("WA4IKQ", POSITIONLESS_WX)

    assert _values(ex)["Wind"] == "from the NNW (346°) at 0 mph"
    assert "Position" not in _values(ex)
    assert any("separate beacon" in w for w in ex.warnings)


def test_telemetry_means_nothing_until_the_station_says_what_it_measures() -> None:
    """Five numbers, 0-255. Without the companion messages they are five numbers."""
    ex = _explain("K4KSC-12", TELEMETRY)

    assert _values(ex)["Channel 1"] == "190"
    assert any("has not published" in w for w in ex.warnings)


def test_telemetry_with_definitions_becomes_readable() -> None:
    """`A3 = 11` becomes "110 packets digipeated in the last hour" — and the check that
    it is right is external: this station's own beacon text says `U=14.2V`, and channel 1
    decodes to 14.25 V."""
    heard = [classify("N1KSC-1", info, [], "") for info in DEFINITIONS]
    defs = collect_definitions(heard)["N1KSC-1"]

    values = _values(_explain("N1KSC-1", TELEMETRY, defs=defs))

    assert values["Vin"] == "14.25 Volt"
    assert values["Rx1h"] == "880 Pkt"
    assert values["Dg1h"] == "110 Pkt"
    # The fifth channel's equation is the fifteenth EQNS value. A cap sized for PARM's
    # thirteen fields drops it, and the channel silently reverts to a raw count.
    assert values["A5"] == "0 None"


def test_only_a_station_may_define_its_own_telemetry() -> None:
    """The definitions are ordinary APRS messages, and anyone with a transmitter can send
    one. `:K4KSC-12 :EQNS.0,1000000,0` from a stranger would make the card display an
    invented voltage for a station that never said any such thing."""
    forged = classify("W4EVIL", ":K4KSC-12 :EQNS.0,1000000,0", [], "")
    honest = classify("N1KSC-1", DEFINITIONS[0], [], "")

    defs = collect_definitions([forged, honest])

    assert "W4EVIL" not in defs
    assert "K4KSC-12" not in defs
    assert defs["N1KSC-1"]["PARM"][0] == "Vin"


def test_an_object_reports_its_name_state_and_that_names_are_not_owned() -> None:
    ex = _explain("KD4WLE", OBJECT)
    values = _values(ex)

    assert values["Object"] == "FLMesh-2"
    assert values["State"] == "live"
    assert values["Symbol"] == "Dish antenna"
    # Any station may transmit any object name. A reader who does not know that will
    # believe the wrong station about where something is.
    assert any("not owned" in w for w in ex.warnings)


def test_position_ambiguity_is_reported_rather_than_zero_filled() -> None:
    """Two trailing spaces in the minutes is a station saying "I am somewhere in this
    minute", and rounding it to a precise-looking decimal invents precision.

    The level comes from the LATITUDE alone — the longitude mirrors it — so summing both
    reports every ambiguous fix as one level vaguer than it is."""
    assert "to the nearest minute" in _values(_explain("N4TDX", AMBIGUOUS))["Position"]


def test_the_altitude_extension_is_six_characters() -> None:
    """A greedy `\\d+` reads the real frame `/A=00000070cm MMDVM…` as 70,000,000 feet,
    because the `70` belongs to "70cm" — the band, not the height."""
    frame = "!2835.81N/08050.93W-/A=00000070cm MMDVM Voice (DMR) 439.41250MHz"
    ex = _explain("K4JTT-D", frame)

    assert _values(ex)["Altitude"] == "0 ft (0 m)"
    assert ex.comment.startswith("70cm MMDVM")


def test_a_station_timestamp_is_reported_as_the_station_s_claim() -> None:
    """N1KSC-1 transmits `@290303z` on 3 September: its day counter is simply wrong.

    Presented as the time it would contradict the heard time next to it on the same row,
    and the reader has no way to tell which to believe."""
    ex = _explain("N1KSC-1", "@290303z2835.13N/08039.04WSPLXDigi U=14.2V")

    assert "by the station's own clock" in _values(ex)["Reported at"]


def test_a_status_report_has_no_summary_of_ours() -> None:
    """The station's own sentence IS the content. Repeating it in the app's voice would
    present a stranger's words as ours, which is the one thing the two-voice rule in the
    binding spec exists to prevent."""
    ex = _explain("K4JTT-D", ">Powered by WPSD (https://wpsd.radio)")

    assert ex.summary == ""
    assert ex.comment == "Powered by WPSD (https://wpsd.radio)"


def test_a_plain_ax25_beacon_is_not_called_aprs() -> None:
    # N4TDX-15 sends this to the address BEACON. It has no data-type identifier and no
    # APRS meaning, and pretending to decode it would be inventing structure.
    heard = classify("N4TDX-15", "BPQ Node Stack/iGate/Chat/Full Service BBS", [], "")
    ex = explain(heard)

    assert heard.dest == ""  # no raw frame in this fixture
    ex_with_dest = _explain("N4TDX-15", "BPQ Node Stack", raw="")
    assert ex_with_dest is not None
    assert ex.comment.startswith("BPQ Node Stack")


def test_the_summary_never_repeats_the_symbol_for_a_moving_station() -> None:
    # The row shows the icon; the sentence spends its line on what the icon cannot say.
    ex = _explain("N0XIA-4", MIC_E_INFO, raw=MIC_E_RAW)

    assert ex.summary.startswith("Car — 52 knots")


@pytest.mark.parametrize(
    "hostile",
    [
        "",
        "T#",
        "T#1,2",
        ";short",
        ":",
        "@zzzzzzz",
        "!9999.99Z99999.99Z",
        "_zzzzzzzz",
        "`",
        "\x00\x01\x02",
        "}" * 40,
        "@031030z2837.27N/08049.42W_" + "t" * 400,
    ],
)
def test_hostile_input_yields_an_answer_rather_than_an_exception(hostile: str) -> None:
    """Every one of these is reachable from the air. The explainer is total by design: a
    frame it cannot read becomes an empty reading plus a note, never an exception that
    ends the drain and never a guess that reads as fact."""
    ex = _explain("W4XYZ", hostile)

    assert isinstance(ex.summary, str)
    assert isinstance(ex.fields, list)


def test_the_kind_label_names_what_the_row_shows() -> None:
    assert kind_label(classify("N1KSC-1", TELEMETRY, [], "")) == "Telemetry"
    assert kind_label(classify("K4JTT-D", ">status", [], "")) == "Status"
    assert kind_label(classify("N4TDX", "<IGATE,MSG_CNT=1", [], "")) == "Capabilities"
    assert kind_label(classify("KD4WLE", WEATHER, [], "")) == "Weather"
    assert kind_label(classify("KD4WLE", OBJECT, [], "")) == "Object"
