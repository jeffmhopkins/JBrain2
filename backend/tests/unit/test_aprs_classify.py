"""What a heard frame is, derived from what was stored.

Every frame quoted here is REAL — captured from the owner's box on 144.390 near
Titusville, 2026-09-03. That matters more than usual: the first build of this feature was
argued from the APRS spec and the spec does not predict what a channel looks like. On the
measured capture the `source` column held 5 values while 15 stations were actually
transmitting, because three quarters of the log was one IGate relaying internet traffic.

So the tests that carry weight are the ones about third-party frames. Get those wrong and
"filter by station" is wrong for most of the log while looking like it works.
"""

from __future__ import annotations

import pytest

from jbrain.sdr.classify import (
    MESSAGE,
    OBJECT,
    OTHER,
    POSITION,
    WEATHER,
    base_call,
    classify,
    dti_from_raw,
    looks_like_station,
)

# Real, from the box. An IGate relaying a D-STAR gateway's position onto RF.
GATED_POSITION = "}N1MPR-C>APDG02,TCPIP,N4TDX*:!2835.06ND08048.98W&RNG0001/A=000010 2m Voice"
# Real. The same IGate relaying a mesh node's object report.
GATED_OBJECT = "}KD4WLE>APRS,TCPIP,N4TDX*:;FLMesh-2 *030003z2837.88N/08049.45W`N4TDX-Parish"
# Real. Heard directly off the air — the KSC club digipeater's own beacon.
DIRECT_POSITION = "@281607z2835.13N/08039.04WSPLXDigi U=14.2V. KSC Amateur Radio Club"
# Real. A weather station, relayed — note the `_` symbol on a POSITION identifier.
GATED_WEATHER = "}KD4WLE>APRS,TCPIP,N4TDX*:@030002z2837.27N/08049.42W_317/002g002t082r000p063"


def test_a_relayed_frame_is_attributed_to_who_actually_sent_it() -> None:
    """The finding the whole feature turns on.

    The AX.25 source of this frame is N4TDX, an IGate. The station that composed it is
    N1MPR-C. On the measured capture, filing by the source collapsed 15 stations into 5
    and filed three quarters of the log under one machine's name."""
    heard = classify("N4TDX", GATED_POSITION, ["N1KSC-1*", "WIDE1*"])

    assert heard.origin == "N1MPR-C"
    assert heard.relay == "N4TDX"


def test_a_relayed_frame_is_typed_by_what_is_INSIDE_it() -> None:
    # `}` is a transport, not a type. Typing by the outer character would file every
    # relayed position, object and message as one meaningless bucket.
    assert classify("N4TDX", GATED_POSITION, []).kind == POSITION
    assert classify("N4TDX", GATED_OBJECT, []).kind == OBJECT


def test_an_ordinary_frame_is_left_alone() -> None:
    heard = classify("N1KSC-1", DIRECT_POSITION, [])

    assert heard.origin == "N1KSC-1"
    assert heard.relay is None
    assert heard.kind == POSITION


def test_gated_means_it_came_from_the_internet_not_merely_that_it_was_wrapped() -> None:
    """Third-party alone does not mean gated.

    A cross-band RF relay, a satellite downlink and a mesh bridge all wrap frames too,
    and those genuinely were heard on the air. Filtering on the `}` character rather than
    on TCPIP would wrongly discard them — and on a receive-only box, wrongly discarding
    something that WAS heard is the failure that matters."""
    assert classify("N4TDX", GATED_POSITION, []).gated is True

    rf_relay = "}W4ABC>APRS,WIDE1-1,N4TDX*:!2835.06N/08048.98W-relayed on RF"
    assert classify("N4TDX", rf_relay, []).gated is False


def test_a_weather_report_wearing_a_position_identifier_is_weather() -> None:
    # APRS101 ch.12: a post-processed weather report may use a position identifier and is
    # distinguished by its `_` symbol. Typing on the identifier alone calls this a
    # position, and the Weather filter then misses the actual weather stations.
    assert classify("N4TDX", GATED_WEATHER, []).kind == WEATHER


def test_direct_means_nobody_repeated_it() -> None:
    # The single fact a ham most wants per packet, and it is already in the stored path.
    assert classify("N1KSC-1", DIRECT_POSITION, []).direct is True
    assert classify("N1KSC-1", DIRECT_POSITION, ["WIDE1-1*"]).direct is False
    # A gated frame is not "direct" in any sense worth reporting.
    assert classify("N4TDX", GATED_POSITION, []).direct is False


def test_a_message_addressee_is_read_as_a_fixed_field() -> None:
    """Nine characters then a colon — never a split on `:`.

    Message text legally contains colons (times, URLs, ratios), so a split-based parser
    breaks on the first URL it meets, which is the first real message anyone sends."""
    assert classify("W4XYZ", ":KE8XYZ   :see you at 19:30{003", []).addressee == "KE8XYZ"
    assert classify("W4XYZ", ":KE8XYZ   :http://a.b/c", []).addressee == "KE8XYZ"


def test_a_message_is_typed_as_a_message_even_when_relayed() -> None:
    # The case the owner's own incoming mail takes: an IGate relays a message to RF only
    # when the addressee has been heard nearby, so his messages arrive wrapped.
    relayed = "}W4XYZ>APRS,TCPIP,N4TDX*::KE8XYZ   :on my way{007"
    heard = classify("N4TDX", relayed, [])

    assert heard.kind == MESSAGE
    assert heard.addressee == "KE8XYZ"
    assert heard.origin == "W4XYZ"


def test_something_that_is_not_a_message_has_no_addressee() -> None:
    assert classify("N1KSC-1", DIRECT_POSITION, []).addressee is None
    # A `:` frame too short to carry a nine-character addressee is not a message.
    assert classify("W4XYZ", ":short", []).addressee is None


def test_a_mic_e_identifier_survives_the_control_character_scrub() -> None:
    """0x1C and 0x1D are legitimate Mic-E identifiers AND control characters.

    The stored `info` has been scrubbed of control characters — a NUL in a text column
    silently loses the whole row — so for these frames the byte saying what the packet is
    has been deleted. `raw` is kept before any of that, which is what makes it
    recoverable, and this is the reason the classifier prefers it."""
    # A minimal AX.25 UI frame: dest, src (end-of-address bit set), control 0x03, PID
    # 0xF0, then the info field starting with the Mic-E identifier.
    dest = bytes([ord(c) << 1 for c in "APRS  "]) + bytes([0x60])
    src = bytes([ord(c) << 1 for c in "N0CALL"]) + bytes([0x61])  # bit 0 = last address
    frame = dest + src + bytes([0x03, 0xF0]) + b"\x1c'l!{" + b'/]"4}'

    # The scrub has removed the identifier from `info`, and `raw` still has it.
    assert dti_from_raw(frame.hex(), "'l!{") == "\x1c"


def test_an_unreadable_raw_frame_falls_back_rather_than_losing_the_row() -> None:
    # A classification problem is never a reason to drop a packet.
    assert dti_from_raw("not hex", "@position") == "@"
    assert dti_from_raw("", "@position") == "@"
    assert dti_from_raw("ff", "@position") == "@"


def test_a_deferred_position_is_still_a_position() -> None:
    # APRS101 ch.5 allows up to 40 characters of unmodifiable leading text before the
    # `!`, for X1J digipeaters. Without this rule those land in Other.
    assert classify("W4XYZ", "BEACON TEXT !2835.13N/08039.04W-", []).kind == POSITION
    # But a `!` beyond the window is not a deferred position.
    assert classify("W4XYZ", "x" * 45 + "!2835.13N", []).kind == OTHER


def test_nested_wrappers_terminate() -> None:
    # Nesting is legal; unbounded recursion on attacker-supplied text is not.
    nested = "}A>B,TCPIP,C*:}D>E,TCPIP,F*:}G>H,TCPIP,I*:!2835.13N/08039.04W-"
    heard = classify("RELAY", nested, [])

    assert heard.origin in {"D", "G"}  # unwrapped, bounded, did not hang


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ("!2835.13N/08039.04W-", POSITION),
        ("=2835.13N/08039.04W-", POSITION),
        ('`l!{/]"4}', POSITION),  # Mic-E
        ("T#292,53.2,-0.8,93,0,16,11000000", OTHER),  # telemetry
        (">Powered by WPSD", OTHER),  # status
        (";FLMesh-2 *030003z2837.88N/08049.45W`", OBJECT),
        ("_10090556c220s004g005t077", WEATHER),
        ("?APRSD", OTHER),  # query
    ],
)
def test_the_five_buckets(info: str, expected: str) -> None:
    # Telemetry, status and queries all land in Other deliberately: each was a small
    # fraction of the measured capture, and a chip row of fifteen is worse than one of
    # five. Telemetry in particular was 9% from essentially one station — the control
    # that wants is muting the station, not filtering the type.
    assert classify("W4XYZ", info, []).kind == expected


def test_a_bare_callsign_covers_every_ssid() -> None:
    # The truck is -9 and the handheld is -7; they are one operator.
    assert base_call("KE8XYZ-9") == base_call("KE8XYZ-7") == "KE8XYZ"
    assert base_call("ke8xyz") == "KE8XYZ"
    assert base_call("") == ""


def test_an_invented_origin_does_not_become_a_station() -> None:
    """The roster is one row per origin, and the origin comes out of ATTACKER TEXT.

    The third-party header is plain characters inside `info` — not an AX.25 address the
    sidecar validated — so one transmitter emitting invented origins puts invented
    stations on the owner's screen. The `*` case is the sharp one: it shadows a real
    callsign, so the roster shows `N4TDX` and `N4TDX*` and the owner cannot tell which
    is theirs."""
    shadow = classify("N4TDX", "}N4TDX*>APRS,TCPIP:!2835.13N/08039.04W-", [])
    assert shadow.origin == "N4TDX"  # filed under the station that actually transmitted

    assert classify("N4TDX", "}<b>x</b>>APRS:!2835.13N", []).origin == "N4TDX"
    assert classify("N4TDX", "}A B C>APRS:!2835.13N", []).origin == "N4TDX"


def test_the_shapes_a_real_station_takes_are_all_accepted() -> None:
    """Looser than AX.25 on purpose — every one of these is in the owner's own capture.

    A third-party header comes from APRS-IS, which carries things AX.25 addresses
    cannot: alphanumeric SSIDs from D-STAR and DMR gateways, and service names longer
    than six characters. Rejecting them would delete real stations from the roster to
    stop a hypothetical one."""
    assert looks_like_station("N1KSC-1")
    assert looks_like_station("K4JTT-D")  # a D-STAR gateway
    assert looks_like_station("WINLINK")  # seven characters, and a real originator
    assert looks_like_station("N4TDX-15")

    assert not looks_like_station("")
    assert not looks_like_station("N4TDX*")
    assert not looks_like_station("TOOLONGCALL-1")
    assert not looks_like_station("N4TDX-")


def test_hostile_input_never_raises() -> None:
    # Every one of these is reachable from the air. The classifier is total by design:
    # a frame it cannot understand becomes Other, never an exception that ends the drain.
    for bad in ["", "}", "}>", "}A>B", "}A>B:", ":", ":::", "\x00", "}" * 50]:
        assert classify("W4XYZ", bad, []).kind in {POSITION, MESSAGE, WEATHER, OBJECT, OTHER}


def test_a_nul_data_type_never_reaches_a_text_column() -> None:
    """One crafted byte would otherwise delete the whole row, `raw` included.

    A frame whose info field begins with 0x00 is a perfectly forwardable AX.25 UI frame,
    so this is reachable by anyone with a transmitter. Postgres rejects a NUL in a text
    column, and the insert path swallows its own errors so the log keeps running — so
    the packet would vanish with nothing recording that it had ever arrived. That
    directly contradicts the property the whole design leans on: a wrong derivation must
    cost a re-run, never a row."""
    dest = bytes([ord(c) << 1 for c in "APRS  "]) + bytes([0x60])
    src = bytes([ord(c) << 1 for c in "N0CALL"]) + bytes([0x61])
    # The scrub has already removed the NUL from `info` on its way to the database; it
    # survives only in `raw`, which is exactly where the classifier reads from.
    hostile = (dest + src + bytes([0x03, 0xF0]) + b"\x00hello").hex()

    # The identifier falls back to the scrubbed info rather than carrying the NUL out.
    assert dti_from_raw(hostile, "hello") == "h"
    # Nothing below space survives except the two legitimate Mic-E identifiers — and the
    # test that matters is the general one, since a text column rejects every control
    # byte's worth of trouble, not only NUL.
    for byte in range(0x20):
        char = chr(byte)
        got = classify("W4XYZ", char + "payload", []).dti
        assert got in {"", "\x1c", "\x1d"} or got >= " ", hex(byte)


def test_the_data_type_is_recovered_through_classify_not_only_directly() -> None:
    """The module's headline justification, tested where it is actually used.

    `classify` preferring `raw` over `info` is the reason `raw` is read at all. Testing
    `dti_from_raw` alone leaves that preference untested — the whole recovery can be
    deleted from `classify` and a suite that only exercises the helper stays green."""
    dest = bytes([ord(c) << 1 for c in "APRS  "]) + bytes([0x60])
    src = bytes([ord(c) << 1 for c in "N0CALL"]) + bytes([0x61])
    mic_e = (dest + src + bytes([0x03, 0xF0]) + b"\x1c'l!{" + b'/]"4}').hex()

    # `info` has been scrubbed of the identifier; `raw` still has it.
    heard = classify("N0CALL", "'l!{/]\"4}", [], mic_e)

    assert heard.dti == "\x1c"
    assert heard.kind == POSITION


def test_the_shortest_real_info_field_is_read_and_the_empty_one_does_not_raise() -> None:
    """The two frames either side of the bounds check, which is one byte wide.

    A frame with exactly ONE info byte is the whole Mic-E recovery in miniature: read it
    and the identifier survives the scrub, refuse it by one and the frame falls back to
    the scrubbed `info` that no longer has it. A frame with NO info field at all is the
    other side, and must fall back rather than raise inside the drain."""
    dest = bytes([ord(c) << 1 for c in "APRS  "]) + bytes([0x60])
    src = bytes([ord(c) << 1 for c in "N0CALL"]) + bytes([0x61])
    header = dest + src + bytes([0x03, 0xF0])

    assert dti_from_raw((header + b"\x1c").hex(), "'scrubbed") == "\x1c"
    assert dti_from_raw(header.hex(), "@x") == "@"


def test_a_relayed_frame_is_never_direct_however_clean_its_inner_path() -> None:
    """`direct` is about how WE received it, so it is read from the OUTER path.

    The inner path of a relayed frame describes somebody else's hop and is
    attacker-authored besides. A transmitter that writes a clean inner path would
    otherwise get a free "heard direct" attribution for any callsign it likes — which is
    exactly the claim a signal-strength reading would hang off."""
    clean_inner = "}W4ABC>APRS,WIDE1-1:!2835.06N/08048.98W-hi"

    assert classify("N4TDX", clean_inner, ["WIDE2-1*"]).direct is False
    # ...including when it is not gated at all.
    assert classify("N4TDX", clean_inner, []).gated is False
    assert classify("N4TDX", clean_inner, []).direct is False


def test_tcpxx_gates_a_frame_as_surely_as_tcpip() -> None:
    # TCPXX is the unverified-login form. A station gated through it was on the internet
    # just as much, and testing only TCPIP leaves half the rule unpinned.
    tcpxx = "}W4ABC>APRS,TCPXX,N4TDX*:!2835.06N/08048.98W-"

    assert classify("N4TDX", tcpxx, []).gated is True


def test_a_compressed_weather_station_is_still_weather() -> None:
    """APRS101 ch.9 defines a SECOND position layout, and it is shorter.

    Compressed: identifier, symbol TABLE, 4 of latitude, 4 of longitude, then the symbol
    CODE — offset 10, not 19. Reading the uncompressed offset misses every compressed
    weather station, which is the exact failure `_is_weather_position` exists to prevent,
    and can find a `_` in a comment and invent one that is not there."""
    assert classify("W4XYZ", "!/5L!!<*e7_7P[", []).kind == WEATHER
    # A compressed position whose COMMENT contains an underscore is not weather.
    assert classify("W4XYZ", "!/5L!!<*e7>7P[ wx_data here", []).kind == POSITION
    # And the timestamped compressed form puts the code seven characters later.
    assert classify("W4XYZ", "@092345z/5L!!<*e7_7P[", []).kind == WEATHER


def test_every_uncompressed_position_identifier_reads_its_symbol_code() -> None:
    # Four identifiers carry a position, and testing one leaves the other three free to
    # be dropped from the table without a test noticing.
    for dti, stamp in (("!", ""), ("=", ""), ("/", "092345z"), ("@", "092345z")):
        wx = f"{dti}{stamp}2837.27N/08049.42W_317/002g002t082"
        assert classify("W4XYZ", wx, []).kind == WEATHER, dti
        pos = f"{dti}{stamp}2837.27N/08049.42W-no weather_here"
        assert classify("W4XYZ", pos, []).kind == POSITION, dti


def test_the_deferred_position_window_is_where_the_spec_puts_it() -> None:
    # APRS101 ch.5 allows up to 40 characters of leading text. The boundary is the
    # assertion — two cases at 12 and 45 leave every value between 13 and 44 passing.
    assert classify("W4XYZ", "x" * 39 + "!2835.13N", []).kind == POSITION
    assert classify("W4XYZ", "x" * 40 + "!2835.13N", []).kind == OTHER


def test_nesting_stops_at_the_second_wrapper_and_says_it_did_not_read_further() -> None:
    """Bounded on attacker-supplied text, and honest about where it stopped.

    Past the cap the payload is still a wrapper, and `}` is a transport that the module
    is explicit is never a type. Storing it as the data type would be recording a
    reading we never took."""
    nested = "}A-1>B,TCPIP,C*:}D-1>E,TCPIP,F*:}G-1>H,TCPIP,I*:!2835.13N/08039.04W-"

    heard = classify("RELAY", nested, [])

    assert heard.origin == "D-1"  # exactly two wrappers opened, not three
    assert heard.dti == ""
