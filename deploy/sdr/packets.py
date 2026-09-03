"""KISS + AX.25: turning what direwolf heard into rows.

Direwolf does the hard half — bit sync, NRZI, HDLC, the CRC — and hands finished
frames over a KISS TCP socket. This module is only the last step: unwrap KISS, split
an AX.25 UI frame into its addresses and its information field, and hand back
something storable. Stdlib only, like the rest of this sidecar.

**Why parse here rather than in the api.** The KISS client lives here, and `/listen/
packets` mirrors `/listen/segments` by shipping self-describing JSON rather than
bytes the caller has to interpret. The raw frame goes along with it, so nothing is
lost by parsing early.

**Two constraints direwolf imposes, both measured rather than assumed** (a real
`gen_packets` -> WAV -> direwolf -> KISS capture; the frames it produced are pinned as
`supervisor/tests/fixtures/aprs_kiss_frames.hex`):

1. Direwolf forwards frames only to KISS clients that are ALREADY ATTACHED. A client
   that connects late does not get history, and a reconnect loses whatever arrived in
   the gap — so the reader attaches once and stays attached, and a drop is a hole in
   the log rather than something to backfill.
2. EOF on its audio stdin ends the session. That never happens while rtl_fm streams,
   but it is why the pipe is held open rather than fed a finite clip.

**Nothing here trusts its input.** These bytes came off the air from anyone with a
transmitter. Addresses are masked to 7 bits and stripped, the info field is decoded
with `errors="replace"`, and a frame that does not parse is dropped rather than
guessed at. Callsigns are a FILTER, never authentication — they are plain bytes and
forge trivially (docs/plans/APRS_CONTROL_PLAN.md, the two trust tiers).
"""

from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Any

# KISS framing (the protocol is tiny and has no library worth the dependency).
# An AX.25 frame maxes out near 330 bytes; this is generous headroom for one plus its
# escaping, and the ceiling on a partial frame the deframer will hold.
MAX_BUFFER = 4096

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

# An AX.25 address is 6 shifted-ASCII characters plus an SSID byte.
_ADDR_LEN = 7
# UI frame, no layer 3. Anything else is not APRS and is dropped.
_CONTROL_UI = 0x03
_PID_NO_L3 = 0xF0
# The last address in the chain has its low bit set; that is how the chain ends.
_LAST_ADDR_BIT = 0x01
# A digipeater that has already repeated the frame sets this in its SSID byte.
_REPEATED_BIT = 0x80
# Source, destination, and at most 8 digipeaters.
_MAX_ADDRS = 10
# What AX.25 v2.2 permits in an address field. Anything else is a crafted frame.
_ADDR_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")


@dataclass(frozen=True, slots=True)
class Packet:
    """One decoded AX.25 UI frame — what a row in the heard log is made of."""

    source: str
    destination: str
    path: list[str]
    info: str
    raw: str
    # When the frame was DECODED, not when a reader got round to storing it. A queue
    # backlog or a slow consumer would otherwise put insert time in the log, and "when
    # was this heard" is the one question a heard log has to answer.
    heard_at: float = 0.0
    # How strong the transmission was, 0-100, or None when no level could be paired
    # with this frame. None means "not measured", never "weak" — see `audio_level`.
    audio_level: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "heard_at": self.heard_at,
            "source": self.source,
            "destination": self.destination,
            "path": self.path,
            "info": self.info,
            "raw": self.raw,
            "audio_level": self.audio_level,
        }


# Direwolf announces each decode on its stdout before forwarding the frame over KISS:
#
#     N0CALL-9 audio level = 50(14/14)    _||||||__
#     Digipeater TCPIP audio level = 50(14/14)    _|||||___
#
# MEASURED against direwolf 1.7 on this exact pipeline — raw PCM on stdin, `-q d`, KISS
# out — rather than taken from its documentation. Three findings shape how it is used:
#
# 1. **The name is not always the sender.** On a digipeated frame direwolf names the
#    DIGIPEATER. Three quarters of the owner's channel is relayed, so matching a level
#    to a frame BY CALLSIGN would attach almost nothing, and would sometimes attach a
#    relay's level to the wrong station. The name is parsed for the log, not for pairing.
# 2. **The lines are flushed per decode, not block-buffered.** Each arrived in the same
#    millisecond as its own KISS frame, 1:1 and in order, which is what makes pairing by
#    ORDER sound where pairing by callsign is not.
# 3. **A failed decode prints no level line** (checked by feeding noise), so the two
#    streams do not drift apart under bad reception.
_LEVEL_RE = re.compile(r"audio level\s*=\s*(\d{1,3})")


def parse_audio_level(text: str) -> int | None:
    """The 0-100 strength out of one direwolf log line, or None if it is not one.

    Clamped rather than rejected above 100: the number is direwolf's own, and a wider
    range in some later version should read as "very strong", not as "unknown"."""
    found = _LEVEL_RE.search(text)
    return min(int(found.group(1)), 100) if found else None


def unescape(payload: bytes) -> bytes:
    """Undo KISS transposition. FEND cannot appear inside a frame, so it is escaped."""
    if FESC not in payload:
        return payload
    out = bytearray()
    i = 0
    while i < len(payload):
        byte = payload[i]
        if byte == FESC and i + 1 < len(payload):
            nxt = payload[i + 1]
            out.append(FEND if nxt == TFEND else FESC if nxt == TFESC else nxt)
            i += 2
            continue
        out.append(byte)
        i += 1
    return bytes(out)


def _address(chunk: bytes) -> tuple[str, bool, bool] | None:
    """One AX.25 address: callsign-SSID, whether the chain ends, whether it repeated.

    Callsign characters are shifted left one bit so the low bit can carry the
    end-of-chain flag, which is why every byte is masked back down before use.

    The character set is ENFORCED, not assumed. AX.25 v2.2 allows only upper-case
    alphanumerics and space in an address, and a frame carrying anything else is
    crafted rather than merely odd — anyone with a TNC can transmit one with a valid
    CRC. Letting it through put control bytes into the heard log and the PWA, and a
    NUL into a Postgres `text` column, which cannot hold U+0000 at all: one crafted
    frame would have made the insert raise on receipt."""
    call = "".join(chr((b >> 1) & 0x7F) for b in chunk[:6])
    if any(c not in _ADDR_CHARS for c in call):
        return None
    call = call.rstrip()
    if not call:
        return None
    ssid_byte = chunk[6]
    ssid = (ssid_byte >> 1) & 0x0F
    name = f"{call}-{ssid}" if ssid else call
    return name, bool(ssid_byte & _LAST_ADDR_BIT), bool(ssid_byte & _REPEATED_BIT)


def parse_ax25(frame: bytes) -> Packet | None:
    """Split an AX.25 UI frame, or None if it is not one we can store.

    Returning None rather than raising because this runs on live radio: a corrupt or
    simply unfamiliar frame is a normal event on a shared channel, not an error worth
    ending the stream over."""
    if len(frame) < _ADDR_LEN * 2 + 2:
        return None
    addrs: list[tuple[str, bool, bool]] = []
    at = 0
    while at + _ADDR_LEN <= len(frame) and len(addrs) < _MAX_ADDRS:
        addr = _address(frame[at : at + _ADDR_LEN])
        if addr is None:
            return None  # an address that is not one; the whole frame is suspect
        addrs.append(addr)
        at += _ADDR_LEN
        if addr[1]:  # end-of-chain bit
            break
    else:
        return None
    # Only `< 2` is reachable: the loop either breaks on the end-of-chain bit or falls
    # into its `else` and returns, so the last address always has that bit set.
    if len(addrs) < 2:
        return None  # a frame with no source is not attributable to anyone
    if at + 2 > len(frame):
        return None
    if frame[at] != _CONTROL_UI or frame[at + 1] != _PID_NO_L3:
        return None  # not a UI frame with no layer 3 — not APRS
    info = frame[at + 2 :].decode("utf-8", errors="replace").rstrip("\r\n")
    destination, source = addrs[0][0], addrs[1][0]
    # A repeated digipeater is marked with * the way every APRS tool shows it, so the
    # stored path says which hops actually handled the frame rather than only which
    # were requested.
    path = [f"{name}*" if repeated else name for name, _, repeated in addrs[2:]]
    return Packet(
        source=source,
        destination=destination,
        path=path,
        info=info,
        raw=frame.hex(),
        heard_at=time.time(),
    )


def parse_kiss(payload: bytes) -> Packet | None:
    """A whole KISS frame (command byte then AX.25), or None if it is not data.

    The low nibble of the first byte is the command; only 0 carries a received frame.
    The high nibble is the port, which this sidecar ignores — direwolf is configured
    with one channel, so there is nothing to demultiplex."""
    if len(payload) < 2:
        return None
    if payload[0] & 0x0F:
        return None  # a command (TXDELAY, etc.), not data
    return parse_ax25(unescape(payload[1:]))


class KissStream:
    """Incremental KISS deframer: feed it socket reads, get whole frames back.

    A socket hands over arbitrary boundaries, so frames arrive split and coalesced.
    Holding the partial tail here is what keeps a frame that straddles two reads from
    being dropped — the same reason `_segments` in the api buffers its own tail."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Packet]:
        """Append bytes; return whatever completed. Never raises."""
        self._buf.extend(chunk)
        if len(self._buf) > MAX_BUFFER:
            # An opening FEND whose closing FEND never arrives — a truncated stream, or
            # a sender that simply stops — would otherwise accumulate for ever. An AX.25
            # frame tops out near 330 bytes, so anything past the cap is not a frame in
            # progress and the partial is not worth keeping.
            del self._buf[: len(self._buf) - MAX_BUFFER]
        out: list[Packet] = []
        while True:
            start = self._buf.find(FEND)
            if start < 0:
                # Nothing framed yet, and anything before a FEND is noise. Drop it so
                # a stream that begins mid-frame cannot grow the buffer for ever.
                self._buf.clear()
                return out
            end = self._buf.find(FEND, start + 1)
            if end < 0:
                del self._buf[:start]
                return out
            payload = bytes(self._buf[start + 1 : end])
            # Keep the closing delimiter: `FEND a FEND b FEND` is legal KISS, and
            # consuming the shared one left `b` with no opening FEND, silently dropping
            # every second frame. Direwolf 1.7 sends both, but nothing here should
            # depend on that.
            del self._buf[:end]
            if not payload:
                continue  # back-to-back FENDs are padding, which is legal
            packet = parse_kiss(payload)
            if packet is not None:
                out.append(packet)
