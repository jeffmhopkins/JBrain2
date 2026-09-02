#!/usr/bin/env bash
# Rebuild supervisor/tests/fixtures/aprs_kiss_frames.hex from real direwolf output.
#
# The fixture is a CAPTURE, not a construction: a hand-written one would only prove the
# parser agrees with whoever wrote it, and someone else's output format is exactly the
# thing not worth guessing at. This script is how that capture stays reproducible —
# run it when the source packets need to change, and commit the result.
#
# Needs direwolf (scripts/dev-setup.sh installs it). The TESTS need neither direwolf nor
# a radio; they read the committed capture.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
out="$root/supervisor/tests/fixtures/aprs_kiss_frames.hex"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

command -v direwolf >/dev/null || { echo "direwolf not installed" >&2; exit 1; }
command -v gen_packets >/dev/null || { echo "gen_packets not installed" >&2; exit 1; }

# One of each shape the log has to handle: a command, a position beacon, and a third
# party's message addressed to someone else.
cat > "$work/src.txt" <<'PKTS'
KE8XYZ-9>APDW17,WIDE1-1:GATE 7K2M9
KE8XYZ-9>APDW17,WIDE1-1:!4129.96N/08141.66W>088/034 test
W8ABC>APDW17::KE8XYZ-9 :net tonight 8pm{01
PKTS

gen_packets -o "$work/packets.wav" "$work/src.txt" >/dev/null
python3 - "$work" "$out" <<'PY'
import json, socket, subprocess, sys, time, wave
from pathlib import Path

work, out = Path(sys.argv[1]), Path(sys.argv[2])
with wave.open(str(work / "packets.wav"), "rb") as w:
    rate, pcm = w.getframerate(), w.readframes(w.getnframes())

(work / "dw.conf").write_text(
    "ADEVICE stdin null\nACHANNELS 1\nCHANNEL 0\nMODEM 1200\nAGWPORT 0\nKISSPORT 8199\n"
)
proc = subprocess.Popen(
    ["direwolf", "-c", str(work / "dw.conf"), "-t", "0", "-q", "d",
     "-r", str(rate), "-B", "1200", "-"],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
# Attach BEFORE feeding audio: direwolf forwards frames only to clients already
# connected, so a reader that arrives late gets nothing and no history.
sock = None
for _ in range(80):
    try:
        sock = socket.create_connection(("127.0.0.1", 8199), timeout=1)
        break
    except OSError:
        time.sleep(0.25)
if sock is None:
    proc.kill(); sys.exit("direwolf never bound its KISS port")

assert proc.stdin is not None
proc.stdin.write(pcm); proc.stdin.flush()   # the pipe stays open: EOF ends its session
sock.settimeout(8)
frames, buf = [], b""
try:
    while len(frames) < 3:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        while True:
            i = buf.find(b"\xc0")
            j = buf.find(b"\xc0", i + 1) if i >= 0 else -1
            if i < 0 or j < 0:
                break
            payload, buf = buf[i + 1 : j], buf[j + 1 :]
            if len(payload) > 1:
                frames.append(payload)
except socket.timeout:
    pass
proc.kill()
if len(frames) != 3:
    sys.exit(f"expected 3 frames, captured {len(frames)}")

header = out.read_text().splitlines()
comments = [ln for ln in header if ln.startswith("#")]
out.write_text("\n".join(comments + [f.hex() for f in frames]) + "\n")
print(f"captured {len(frames)} frames -> {out}")
PY
