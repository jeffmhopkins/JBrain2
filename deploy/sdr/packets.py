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

from dataclasses import dataclass
from typing import Any

# KISS framing (the protocol is tiny and has no library worth the dependency).
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


@dataclass(frozen=True, slots=True)
class Packet:
    """One decoded AX.25 UI frame — what a row in the heard log is made of."""

    source: str
    destination: str
    path: list[str]
    info: str
    raw: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "path": self.path,
            "info": self.info,
            "raw": self.raw,
        }


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


def _address(chunk: bytes) -> tuple[str, bool, bool]:
    """One AX.25 address: callsign-SSID, whether the chain ends, whether it repeated.

    Callsign characters are shifted left one bit so the low bit can carry the
    end-of-chain flag, which is why every byte is masked back down before use."""
    call = "".join(chr((b >> 1) & 0x7F) for b in chunk[:6]).strip()
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
        addrs.append(addr)
        at += _ADDR_LEN
        if addr[1]:  # end-of-chain bit
            break
    else:
        return None
    if len(addrs) < 2 or not addrs[-1][1]:
        return None  # never found the end of the address chain
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
            del self._buf[: end + 1]
            if not payload:
                continue  # back-to-back FENDs are padding, which is legal
            packet = parse_kiss(payload)
            if packet is not None:
                out.append(packet)
