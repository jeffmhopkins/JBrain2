"""AX.25 decoding, pinned against REAL direwolf output.

The fixture is a capture, not a construction: `gen_packets` produced APRS audio,
direwolf decoded it, and the KISS frames it emitted are stored verbatim in
`fixtures/aprs_kiss_frames.hex`. That distinction is the whole point — a hand-written
fixture proves only that the parser agrees with whoever wrote it, and the format of
someone else's output is exactly the thing worth not guessing at.

What is worth pinning beyond "it parses": that frames split across socket reads are
not lost, that a frame off the air can be malformed without ending the stream, and
that the shifted-ASCII address decoding is right — every callsign in the log depends
on that one bit shift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SDR = Path(__file__).resolve().parents[2] / "deploy/sdr"
_spec = importlib.util.spec_from_file_location("sdr_packets", _SDR / "packets.py")
assert _spec and _spec.loader
packets = importlib.util.module_from_spec(_spec)
sys.modules["sdr_packets"] = packets
_spec.loader.exec_module(packets)

_FIXTURE = Path(__file__).parent / "fixtures/aprs_kiss_frames.hex"


def _captured() -> list[bytes]:
    """The real KISS frames direwolf emitted, in order."""
    return [
        bytes.fromhex(line)
        for line in _FIXTURE.read_text().splitlines()
        if line and not line.startswith("#")
    ]


def test_the_fixture_is_three_real_frames() -> None:
    assert len(_captured()) == 3


def test_a_command_packet_decodes_to_its_parts() -> None:
    packet = packets.parse_kiss(_captured()[0])

    assert packet is not None
    assert packet.source == "KE8XYZ-9"
    assert packet.destination == "APDW17"
    assert packet.info == "GATE 7K2M9"


def test_the_digipeater_path_survives() -> None:
    packet = packets.parse_kiss(_captured()[0])

    assert packet is not None
    # The path is how you tell a direct hit from one that came in via the network,
    # which is the difference between "in simplex range" and "somewhere in the state".
    assert packet.path == ["WIDE1-1"]


def test_a_position_beacon_keeps_its_whole_info_field() -> None:
    packet = packets.parse_kiss(_captured()[1])

    assert packet is not None
    assert packet.info == "!4129.96N/08141.66W>088/034 test"


def test_a_third_party_message_names_its_real_sender() -> None:
    packet = packets.parse_kiss(_captured()[2])

    assert packet is not None
    # W8ABC addressed KE8XYZ-9. The SOURCE is the transmitting station — mixing that
    # up with the addressee would make the heard log attribute traffic to the wrong
    # operator, and a callsign is the only identity a packet carries at all.
    assert packet.source == "W8ABC"
    assert packet.info.startswith(":KE8XYZ-9 :")


def test_the_raw_frame_is_kept_verbatim() -> None:
    frame = _captured()[0]
    packet = packets.parse_kiss(frame)

    assert packet is not None
    # Stored so a parser bug found later can be re-run against what was actually
    # heard, rather than the traffic being gone.
    assert bytes.fromhex(packet.raw) == frame[1:]


def test_a_frame_split_across_reads_is_still_whole() -> None:
    frame = bytes([packets.FEND]) + _captured()[0] + bytes([packets.FEND])
    stream = packets.KissStream()

    got: list = []
    for i in range(0, len(frame), 7):  # arbitrary boundaries, as a socket gives
        got += stream.feed(frame[i : i + 7])

    assert [p.info for p in got] == ["GATE 7K2M9"]


def test_two_frames_in_one_read_are_both_delivered() -> None:
    f = packets.FEND
    blob = bytes([f]) + _captured()[0] + bytes([f, f]) + _captured()[1] + bytes([f])
    stream = packets.KissStream()

    got = stream.feed(blob)

    assert len(got) == 2
    assert got[0].info == "GATE 7K2M9"


def test_a_stream_that_begins_mid_frame_recovers() -> None:
    # Attaching to a live channel means arriving in the middle of something. The
    # partial head is noise; the next whole frame must still land.
    stream = packets.KissStream()
    stream.feed(b"\x11\x22\x33 junk with no opening delimiter")

    got = stream.feed(bytes([packets.FEND]) + _captured()[0] + bytes([packets.FEND]))

    assert [p.info for p in got] == ["GATE 7K2M9"]


def test_a_kiss_command_frame_is_not_mistaken_for_traffic() -> None:
    # Command 1 is TXDELAY. Storing it as a heard packet would put invented rows in
    # the log — and worse, feed them to the command path.
    assert packets.parse_kiss(b"\x01\x19") is None


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"\x00",
        b"\x00short",
        b"\x00" + b"\x82\xa0\x88\xae\x62\x6e\xe0",  # one address, chain never ends
        b"\x00"
        + b"\x82\xa0\x88\xae\x62\x6e\xe0" * 2
        + b"\x00\xf0data",  # not a UI frame
    ],
)
def test_a_malformed_frame_is_dropped_not_raised(bad: bytes) -> None:
    # This runs on live radio. A corrupt frame is a normal event on a shared channel,
    # and an exception here would end the whole stream over one bad burst.
    assert packets.parse_kiss(bad) is None


def test_escaped_bytes_are_restored() -> None:
    # FEND cannot appear inside a frame, so KISS transposes it. Getting this wrong
    # corrupts any packet whose payload happens to contain 0xC0 or 0xDB.
    assert packets.unescape(b"\xdb\xdc\xdb\xdd\x41") == b"\xc0\xdb\x41"


def test_a_repeated_digipeater_is_marked() -> None:
    frame = _captured()[0]
    body = bytearray(frame[1:])
    body[13 + 7] |= 0x80  # set the has-been-repeated bit on the first digipeater

    packet = packets.parse_ax25(bytes(body))

    assert packet is not None
    # "WIDE1-1*" means that hop actually handled it. Without the mark a stored path
    # says what was REQUESTED, not what happened.
    assert packet.path == ["WIDE1-1*"]


# --- what a hostile transmitter can put in a row ------------------------------------
# Everything below arrives from anyone with a TNC and a valid CRC. An independent review
# found the parser trusted address bytes it had only shifted, never checked.


def _frame(dest: bytes, src: bytes, info: bytes = b"hi") -> bytes:
    """An AX.25 UI frame from two raw 7-byte address fields."""
    return dest + src + bytes([0x03, 0xF0]) + info


def _addr(call: str, *, last: bool = False) -> bytes:
    padded = call.ljust(6)[:6]
    return bytes((ord(c) << 1) & 0xFF for c in padded) + bytes(
        [0x60 | (1 if last else 0)]
    )


def test_a_wellformed_frame_still_parses() -> None:
    # The guard below must not reject real traffic.
    packet = packets.parse_ax25(_frame(_addr("APDW17"), _addr("KE8XYZ", last=True)))

    assert packet is not None
    assert (packet.destination, packet.source) == ("APDW17", "KE8XYZ")


def test_control_bytes_in_a_callsign_are_refused() -> None:
    # A NUL cannot be stored in a Postgres `text` column at all, so one crafted frame
    # would have made the api's insert raise on receipt — and until then it put control
    # characters into the heard log and the PWA.
    hostile = bytes(b << 1 for b in b"\x00\x01\x02\x03\x04\x05") + bytes([0x60])

    assert packets.parse_ax25(_frame(hostile, _addr("KE8XYZ", last=True))) is None


def test_lowercase_and_punctuation_in_a_callsign_are_refused() -> None:
    # AX.25 v2.2 allows upper-case alphanumerics and space. Anything else is crafted.
    for call in ("ke8xyz", "A<B>C", "A;DROP"):
        hostile = bytes((ord(c) << 1) & 0xFF for c in call.ljust(6)[:6]) + bytes([0x60])
        assert (
            packets.parse_ax25(_frame(hostile, _addr("KE8XYZ", last=True))) is None
        ), call


def test_an_all_space_callsign_is_not_a_station() -> None:
    blank = bytes((ord(" ") << 1) for _ in range(6)) + bytes([0x60])

    assert packets.parse_ax25(_frame(blank, _addr("KE8XYZ", last=True))) is None


def test_a_frame_that_is_not_a_UI_frame_is_refused() -> None:
    # The earlier version of this test used a frame whose address chain never ended, so
    # the control/PID check was never reached and deleting it left the suite green. This
    # one has a valid chain: an I-frame or NET/ROM frame would otherwise be stored as
    # APRS with nothing to notice.
    good = _frame(_addr("APDW17"), _addr("KE8XYZ", last=True))
    not_ui = good[:14] + bytes([0x00, 0xF0]) + good[16:]
    wrong_pid = good[:14] + bytes([0x03, 0xCF]) + good[16:]

    assert packets.parse_ax25(not_ui) is None
    assert packets.parse_ax25(wrong_pid) is None


def test_a_kiss_command_long_enough_to_look_like_a_frame_is_still_refused() -> None:
    # The earlier test used a 2-byte payload, which the AX.25 minimum rejected on its
    # own — so the command-nibble check was undefended. This one is long enough to reach
    # the parser.
    body = _frame(_addr("APDW17"), _addr("KE8XYZ", last=True))

    assert packets.parse_kiss(b"\x01" + body) is None  # TXDELAY
    assert packets.parse_kiss(b"\x06" + body) is None  # SET HARDWARE
    assert packets.parse_kiss(b"\x00" + body) is not None  # data still works


def test_a_single_address_frame_is_refused_not_crashed() -> None:
    assert (
        packets.parse_ax25(_addr("APDW17", last=True) + bytes([0x03, 0xF0, 65])) is None
    )


def test_frames_sharing_one_delimiter_are_both_delivered() -> None:
    # FEND a FEND b FEND is legal KISS. Consuming the shared delimiter left `b` with no
    # opening FEND, so every second frame vanished.
    f = bytes([packets.FEND])
    a, b = _captured()[0], _captured()[1]
    stream = packets.KissStream()

    got = stream.feed(f + a + f + b + f)

    assert [p.info for p in got] == ["GATE 7K2M9", "!4129.96N/08141.66W>088/034 test"]


def test_an_unclosed_frame_cannot_grow_the_buffer_without_bound() -> None:
    # A truncated stream, or a sender that simply stops mid-frame, otherwise accumulates
    # for ever — measured at ~2 MB before this cap.
    stream = packets.KissStream()

    for _ in range(200):
        stream.feed(bytes([packets.FEND]) + b"x" * 4096)

    assert len(stream._buf) <= packets.MAX_BUFFER


def test_a_frame_arriving_after_a_truncated_one_still_decodes() -> None:
    # The cap must drop the junk, not the parser's ability to resynchronise.
    stream = packets.KissStream()
    stream.feed(bytes([packets.FEND]) + b"x" * 9000)

    got = stream.feed(bytes([packets.FEND]) + _captured()[0] + bytes([packets.FEND]))

    assert [p.info for p in got] == ["GATE 7K2M9"]
