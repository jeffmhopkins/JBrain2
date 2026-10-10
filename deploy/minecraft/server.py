"""The `minecraft` sidecar: Bedrock Dedicated Server plus the control surface around it.

docs/plans/MINECRAFT_BEDROCK_PLAN.md, wave M0a — the rig the M0 probes run through. The
api reaches this over the `minecraft` network (only `api` joins it); players reach BDS
on the published UDP port and never this HTTP surface.

Stdlib only, like the endpoint and sdr sidecars: the HTTP surface is `http.server`, the
game server is Mojang's binary in the data volume (install.py), and its console is a
pipe owned by bds.py.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bds
import install

DATA = Path(os.environ.get("MC_DATA_DIR", "/data"))
SERVER_DIR = DATA / "server"
SNAPSHOT_DIR = DATA / "snapshots"
OVERRIDES = DATA / "properties.json"
PORT = int(os.environ.get("MC_PORT", "8000"))
# The container runs on the HOST network (NetherNet advertises the server's own
# address and finds LAN clients by broadcast, neither of which survives a bridge
# network), so this control port is reachable from the LAN. Every route but /healthz
# needs the bearer the api holds; with no token configured they all refuse rather than
# run open.
TOKEN = os.environ.get("MC_TOKEN", "")
INSTALL_RETRY_S = 300
# Pinned by compose to the published ports; changing them here would silently move the
# server off the port the LAN and the router know about.
LOCKED_PROPERTIES = frozenset({"server-port", "server-portv6"})
_PROPERTY_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LABEL = re.compile(r"[^A-Za-z0-9_-]+")


class Rig:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.bds = bds.Bds(SERVER_DIR)
        self.install_error = ""
        self.stopping = False

    def overrides(self) -> dict[str, str]:
        try:
            raw = json.loads(OVERRIDES.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def properties(self) -> dict[str, str]:
        return {**install.env_overrides(self.env), **self.overrides()}

    def set_overrides(self, changes: dict[str, Any]) -> dict[str, str]:
        """Merge owner property edits; a null value removes an override.

        Applied at the next server start, not live — BDS reads the file once."""
        current = self.overrides()
        for key, value in changes.items():
            if not _PROPERTY_KEY.match(key):
                raise ValueError(f"not a server.properties key: {key!r}")
            if key in LOCKED_PROPERTIES:
                raise ValueError(f"{key} is pinned to the published port")
            if value is None:
                current.pop(key, None)
            else:
                text = str(value)
                if any(c in text for c in "\r\n"):
                    raise ValueError(f"{key}: value may not contain a line break")
                current[key] = text
        DATA.mkdir(parents=True, exist_ok=True)
        OVERRIDES.write_text(json.dumps(current, indent=2, sort_keys=True))
        return current

    def bring_up(self) -> None:
        """Install (or update) BDS, write its properties, start it. Retries forever on
        a failed first install, keeping the HTTP surface up so the failure is visible
        from the debug console rather than as a container restart loop."""
        while not self.stopping:
            try:
                install.ensure(SERVER_DIR, self.env.get("MC_BDS_VERSION", "latest"))
                self.install_error = ""
                break
            except Exception as exc:
                self.install_error = f"{type(exc).__name__}: {exc}"
                print(f"[install] failed: {self.install_error}", flush=True)
                time.sleep(INSTALL_RETRY_S)
        if self.stopping:
            return
        install.apply_properties(SERVER_DIR / "server.properties", self.properties())
        self.bds.start()

    def world_dir(self) -> Path:
        return SERVER_DIR / "worlds" / self.properties().get("level-name", "world")

    def status(self) -> dict[str, Any]:
        b = self.bds
        started = b.started_at
        if b.running or b.exit_code is not None:
            state = b.state
        else:
            state = "install_failed" if self.install_error else "installing"
        return {
            "state": state,
            "install_error": self.install_error or None,
            "version": b.version or install.installed_version(SERVER_DIR) or None,
            "wanted_version": self.env.get("MC_BDS_VERSION", "latest"),
            "level_name": self.properties().get("level-name"),
            "properties": self.properties(),
            "players": [{"name": n, "xuid": x} for n, x in sorted(b.players.items())],
            "started_at": started,
            "uptime_s": round(time.time() - started) if started and b.running else None,
            "exit_code": b.exit_code,
            "snapshots": len(list_snapshots()),
        }

    def snapshot(self, label: str) -> dict[str, Any]:
        data, files = self.bds.snapshot(self.world_dir())
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        slug = _LABEL.sub("-", label).strip("-")[:40]
        level = _LABEL.sub("-", self.properties().get("level-name", "world"))
        name = f"{level}-{stamp}{'-' + slug if slug else ''}.mcworld"
        (SNAPSHOT_DIR / name).write_bytes(data)
        return {"name": name, "bytes": len(data), "files": len(files)}

    def shutdown(self) -> None:
        self.stopping = True
        self.bds.stop()


def list_snapshots() -> list[dict[str, Any]]:
    if not SNAPSHOT_DIR.is_dir():
        return []
    out = []
    for p in sorted(SNAPSHOT_DIR.glob("*.mcworld"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "bytes": st.st_size, "created": st.st_mtime})
    return out


RIG: Rig | None = None


def _rig() -> Rig:
    assert RIG is not None
    return RIG


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # the BDS console is the log worth reading; requests would drown it

    def _send(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        parsed = json.loads(self.rfile.read(n))
        if not isinstance(parsed, dict):
            raise ValueError("body must be a JSON object")
        return parsed

    def _authorized(self) -> bool:
        if not TOKEN:
            self._send(503, {"detail": "no MC_TOKEN configured; control is disabled"})
            return False
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode()):
            self._send(401, {"detail": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        rig = _rig()
        if url.path == "/healthz":
            self._send(200, {"ok": True})
            return
        if not self._authorized():
            return
        if url.path == "/status":
            self._send(200, rig.status())
        elif url.path == "/logs":
            tail = max(1, min(int(query.get("tail", ["200"])[0]), bds.LOG_LINES))
            lines = [
                {"seq": ln.seq, "at": ln.at, "text": ln.text}
                for ln in rig.bds.tail(tail)
            ]
            self._send(200, {"lines": lines})
        elif url.path == "/snapshots":
            self._send(200, {"snapshots": list_snapshots()})
        elif url.path == "/properties":
            self._send(
                200, {"effective": rig.properties(), "overrides": rig.overrides()}
            )
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:
        rig = _rig()
        if not self._authorized():
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})
            return
        try:
            if self.path == "/command":
                command = str(body.get("command", ""))
                wait_s = max(0.5, min(float(body.get("wait_s", 3.0)), 20.0))
                lines = rig.bds.command(command, wait_s=wait_s)
                self._send(200, {"command": command, "lines": lines})
            elif self.path == "/snapshot":
                self._send(200, rig.snapshot(str(body.get("label", ""))))
            elif self.path == "/properties":
                changes = body.get("set", {})
                if not isinstance(changes, dict):
                    raise ValueError("`set` must be an object")
                overrides = rig.set_overrides(changes)
                self._send(200, {"overrides": overrides, "applies": "next start"})
            else:
                self._send(404, {"detail": "not found"})
        except bds.ConsoleError as exc:
            code = 409 if "not running" in str(exc) else 400
            self._send(code, {"detail": str(exc)})
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})


def main() -> None:
    global RIG
    rig = RIG = Rig()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def _term(_sig: int, _frame: object) -> None:
        rig.shutdown()
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    rig.bring_up()
    # BDS exiting on its own (a crash, a console `stop` that slipped through) ends the
    # container, so `restart: unless-stopped` brings it back rather than leaving a
    # healthy-looking wrapper around a dead game server.
    while not rig.stopping:
        if rig.bds.exit_code is not None and not rig.bds.running:
            print(f"[minecraft] server exited ({rig.bds.exit_code})", flush=True)
            sys.exit(1)
        time.sleep(1)


if __name__ == "__main__":
    main()
