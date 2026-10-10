"""The running Bedrock server: its process, its console, and hot snapshots.

BDS has no RCON. Its one control channel is the console — commands on stdin, replies
and logs on stdout — so whoever owns that pipe owns graceful stop, consistent backups
and (later) the companion bridge. This module is that owner; nothing else writes to
the server's stdin.
"""

from __future__ import annotations

import collections
import io
import os
import re
import subprocess
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG_LINES = 5000
# Join/leave events kept for the api's drain. It polls every few seconds, so this is
# hours of headroom; a gap (the api down longer than that) loses only events the api
# would reconcile anyway from `/status`'s online list.
EVENTS_KEPT = 2000
# Changes on every wrapper start, so the api can tell "event ids restarted" from "no new
# events" and close any session the restart cut off.
BOOT_ID = uuid.uuid4().hex
_CONNECTED = re.compile(r"Player connected: ([^,]+), xuid: ?(\d*)")
_DISCONNECTED = re.compile(r"Player disconnected: ([^,]+), xuid")
_VERSION = re.compile(r"Version:? ([0-9][0-9.]*[0-9])")
_STARTED = "Server started."
# `save query` prints this line, then the file list on the next line.
_SAVE_READY = "Data saved. Files are now ready to be copied."
_SAVE_FILE = re.compile(r"\s*([^:,][^:]*?):(\d+)\s*$")

# The console is the owner's tool, not a player's; these are refused because each has
# a route of its own that does it safely. `stop` is the supervisor's lifecycle (a
# console stop would leave the container's restart policy to bring it straight back)
# and `save …` is the snapshot's hold/query/resume, which a stray `save hold` with no
# matching resume would leave the world unable to autosave.
REFUSED_COMMANDS = frozenset({"stop", "save"})


class ConsoleError(Exception):
    pass


def refusal(command: str) -> str | None:
    """Why a console command may not be sent, or None if it may."""
    if not command.strip():
        return "empty command"
    if any(c in command for c in "\r\n\x00"):
        return "one command per call (no line breaks)"
    verb = command.strip().lstrip("/").split()[0].lower()
    if verb in REFUSED_COMMANDS:
        return f"`{verb}` is refused on the console; use its own route"
    return None


@dataclass
class Line:
    seq: int
    at: float  # wall clock, for the log view
    mono: float  # monotonic, for the reply quiet window
    text: str


@dataclass
class Bds:
    """One BDS child process and the parsed view of its console.

    `spawn` is injectable so tests drive a fake server script instead of Mojang's
    binary."""

    server_dir: Path
    spawn: Callable[[Path], subprocess.Popen[str]] | None = None
    state: str = "stopped"
    version: str = ""
    # gamertag -> {"xuid", "joined_at"}; joined_at is what the Ops card's session timer
    # counts from.
    players: dict[str, dict[str, Any]] = field(default_factory=dict)
    started_at: float | None = None
    exit_code: int | None = None
    _proc: subprocess.Popen[str] | None = None
    _lines: collections.deque[Line] = field(
        default_factory=lambda: collections.deque(maxlen=LOG_LINES)
    )
    _seq: int = 0
    _cv: threading.Condition = field(default_factory=threading.Condition)
    _cmd_lock: threading.Lock = field(default_factory=threading.Lock)
    _events: collections.deque[dict[str, Any]] = field(
        default_factory=lambda: collections.deque(maxlen=EVENTS_KEPT)
    )
    _event_id: int = 0

    def _default_spawn(self, server_dir: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["./bedrock_server"],
            cwd=server_dir,
            env={**os.environ, "LD_LIBRARY_PATH": "."},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self.players.clear()
        self.exit_code = None
        self.version = ""  # re-read from the banner: an update changes it
        self.state = "starting"
        spawn = self.spawn or self._default_spawn
        self._proc = spawn(self.server_dir)
        self.started_at = time.time()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            self._record(raw.rstrip("\n"))
        code = proc.wait()
        with self._cv:
            # A stopping server logs no disconnects, so the sessions it ends are closed
            # here — otherwise play time would run on through the downtime.
            for name, p in self.players.items():
                self._event("leave", name, p["xuid"])
            self._event("server_stop", "", "")
            self.exit_code = code
            self.state = "stopped"
            self.players.clear()
            self._cv.notify_all()

    def _record(self, text: str) -> None:
        with self._cv:
            self._seq += 1
            self._lines.append(Line(self._seq, time.time(), time.monotonic(), text))
            if _STARTED in text:
                self.state = "running"
                self._event("server_start", "", "")
            if not self.version and (m := _VERSION.search(text)):
                self.version = m.group(1)
            if m := _CONNECTED.search(text):
                name, xuid = m.group(1).strip(), m.group(2)
                self.players[name] = {"xuid": xuid, "joined_at": time.time()}
                self._event("join", name, xuid)
            elif m := _DISCONNECTED.search(text):
                name = m.group(1).strip()
                gone = self.players.pop(name, None)
                self._event("leave", name, gone["xuid"] if gone else "")
            self._cv.notify_all()
        print(text, flush=True)

    def _event(self, kind: str, name: str, xuid: str) -> None:
        # Caller holds self._cv.
        self._event_id += 1
        self._events.append(
            {
                "id": self._event_id,
                "at": time.time(),
                "kind": kind,
                "name": name,
                "xuid": xuid,
            }
        )

    def events_after(self, after: int) -> list[dict[str, Any]]:
        with self._cv:
            return [dict(e) for e in self._events if e["id"] > after]

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def tail(self, n: int) -> list[Line]:
        with self._cv:
            return list(self._lines)[-n:]

    def _send(self, command: str) -> int:
        """Write one line to the console; return the log seq it was sent after."""
        if not self.running or self._proc is None or self._proc.stdin is None:
            raise ConsoleError("server is not running")
        with self._cv:
            mark = self._seq
        self._proc.stdin.write(command + "\n")
        self._proc.stdin.flush()
        return mark

    def _since(self, mark: int) -> list[Line]:
        return [ln for ln in self._lines if ln.seq > mark]

    def command(
        self, command: str, wait_s: float = 3.0, quiet_s: float = 0.4
    ) -> list[str]:
        """Send a console command and return the lines BDS printed in reply.

        BDS tags nothing with the command that caused it, so a reply is "what arrived
        after the send": up to `wait_s`, ending early once output has started and then
        gone quiet for `quiet_s`. A player joining mid-command can interleave a line;
        one command at a time (the lock) keeps replies from each other."""
        reason = refusal(command)
        if reason:
            raise ConsoleError(reason)
        with self._cmd_lock:
            return self._collect(self._send(command.strip()), wait_s, quiet_s)

    def _collect(self, mark: int, wait_s: float, quiet_s: float) -> list[str]:
        deadline = time.monotonic() + wait_s
        with self._cv:
            while True:
                got = self._since(mark)
                now = time.monotonic()
                if now >= deadline:
                    break
                if got and now - got[-1].mono >= quiet_s:
                    break
                self._cv.wait(timeout=min(quiet_s, deadline - now))
            return [ln.text for ln in self._since(mark)]

    def _wait_for(self, mark: int, pred: Callable[[str], bool], wait_s: float) -> int:
        """Block until a console line after `mark` matches; return its seq."""
        deadline = time.monotonic() + wait_s
        with self._cv:
            while True:
                for ln in self._since(mark):
                    if pred(ln.text):
                        return ln.seq
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ConsoleError("timed out waiting for the server")
                self._cv.wait(timeout=left)

    def snapshot(
        self, world_dir: Path, wait_s: float = 60.0
    ) -> tuple[bytes, list[str]]:
        """A consistent `.mcworld` of the running world, players left connected.

        `save hold` pauses autosave; `save query` reports, once the files are flushed,
        every file and the LENGTH that is valid — LevelDB keeps appending past it, so
        each file is copied then truncated to that length. `save resume` always runs,
        even on failure, or the world stops saving until the next restart."""
        with self._cmd_lock:
            self._send("save hold")
            try:
                files = self._query_files(wait_s)
                data = _mcworld(world_dir, files)
            finally:
                self._send("save resume")
        return data, [f"{name}:{length}" for name, length in files]

    def _query_files(self, wait_s: float) -> list[tuple[str, int]]:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            mark = self._send("save query")
            try:
                ready = self._wait_for(mark, lambda t: _SAVE_READY in t, 2.0)
                listing = self._wait_for(ready, _is_file_list, 5.0)
            except ConsoleError:
                continue  # "not ready yet" — ask again
            with self._cv:
                text = next(ln.text for ln in self._lines if ln.seq == listing)
            return parse_file_list(text)
        raise ConsoleError("save query never reported the files ready")

    def stop(self, wait_s: float = 45.0) -> None:
        """Graceful stop: `stop` on the console, then a kill if it never exits."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self.state = "stopping"
        try:
            self._send("stop")
            proc.wait(timeout=wait_s)
        except (ConsoleError, OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=10)


def _is_file_list(text: str) -> bool:
    # Log lines open with a bracketed timestamp; the file list is printed bare.
    return not text.startswith("[") and bool(_SAVE_FILE.match(text.split(",")[0]))


def parse_file_list(text: str) -> list[tuple[str, int]]:
    """`world/db/000005.ldb:1234, world/level.dat:5678` -> [(path, length), ...]."""
    out: list[tuple[str, int]] = []
    for part in text.split(","):
        m = _SAVE_FILE.match(part)
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
    if not out:
        raise ConsoleError(f"unreadable save query file list: {text[:200]}")
    return out


def _mcworld(world_dir: Path, files: Iterable[tuple[str, int]]) -> bytes:
    """Zip the queried files, each cut to its reported length, as a `.mcworld`.

    `save query` names files under the world's folder (`<level-name>/db/…`); a
    `.mcworld` has the world's contents at its root, so that first segment is dropped.
    A path that would escape the world folder is refused rather than followed."""
    root = world_dir.resolve()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, length in files:
            rel = name.split("/", 1)[1] if "/" in name else name
            src = (root / rel).resolve()
            if root not in src.parents:
                raise ConsoleError(f"file outside the world: {name}")
            with open(src, "rb") as fh:
                zf.writestr(rel, fh.read(length))
    return buf.getvalue()
