"""The `minecraft` sidecar: Bedrock Dedicated Server plus the control surface around it.

docs/plans/MINECRAFT_BEDROCK_PLAN.md (M0a rig, M1 card backend). The container is on
the host network; the api reaches this control surface with a bearer, and players reach
BDS on 19132 and never this HTTP surface.

Stdlib only, like the endpoint and sdr sidecars: the HTTP surface is `http.server`, the
game server is Mojang's binary in the data volume (install.py), and its console is a
pipe owned by bds.py.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import bds
import install

DATA = Path(os.environ.get("MC_DATA_DIR", "/data"))
SERVER_DIR = DATA / "server"
SNAPSHOT_DIR = DATA / "snapshots"
OVERRIDES = DATA / "properties.json"
SETTINGS = DATA / "settings.json"
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
# How stale the "latest version" answer may be before a status read re-asks Mojang.
VERSION_TTL_S = 6 * 3600
# How long a freshly updated server gets to log "Server started." before the update is
# judged broken and rolled back.
START_TIMEOUT_S = 120.0
# The probe behavior pack (M0 item 5), bundled in the image — nothing uploaded.
PROBE_PACK_SRC = Path(__file__).resolve().parent / "probe-pack"
PROBE_PACK_DIR = "jbrain_probe"


class Rig:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.bds = bds.Bds(SERVER_DIR)
        self.install_error = ""
        self.stopping = False
        # True while the wrapper itself stops and restarts BDS (an update, a pack
        # install), so the watchdog in main() does not read it as a crash.
        self.maintenance = False
        self.latest: dict[str, Any] = {}
        self.update_state: dict[str, Any] = {"state": "idle"}
        # One lifecycle at a time: first-boot install, Start/Stop/Restart, an update,
        # the probe pack. `_busy` names the holder so a refusal can say why.
        self._life = threading.Lock()
        self._busy = ""

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
        OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES.write_text(json.dumps(current, indent=2, sort_keys=True))
        return current

    def settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(SETTINGS.read_text())
        except (FileNotFoundError, ValueError):
            raw = {}
        return {
            "auto_update": bool(raw.get("auto_update", False)),
            # Whether the owner wants the game server up. The Ops screen's Stop stops
            # BDS but keeps this wrapper running, so status, facts and an update still
            # work while it is stopped; this survives a box reboot.
            "run": bool(raw.get("run", True)),
        }

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        for key in ("auto_update", "run"):
            if key in changes:
                current[key] = bool(changes[key])
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(current))
        return current

    def wanted_version(self) -> str:
        """What a server start installs. A pinned MC_BDS_VERSION always wins. Otherwise
        "latest" only when the owner turned auto-update on — by default a start keeps
        what is installed, and updating is the card's explicit, backed-up button."""
        pinned = self.env.get("MC_BDS_VERSION", "latest")
        if pinned not in ("", "latest"):
            return pinned
        have = install.installed_version(SERVER_DIR)
        if have and not self.settings()["auto_update"]:
            return have
        return "latest"

    def bring_up(self) -> None:
        """Install BDS if there is none (retrying forever, with the HTTP surface up so a
        failure is visible), apply auto-update if the owner turned it on and a newer
        version exists, then start the server if the owner wants it running. Holds
        the lifecycle lock throughout, so a Start arriving mid-install is deferred to
        it rather than racing it."""
        with self._life:
            self._busy = "installing"
            try:
                while not self.stopping:
                    try:
                        if install.installed_version(SERVER_DIR):
                            self._auto_update_on_boot()
                        else:
                            install.ensure(SERVER_DIR, self.wanted_version())
                        self.install_error = ""
                        break
                    except Exception as exc:
                        self.install_error = f"{type(exc).__name__}: {exc}"
                        print(f"[install] failed: {self.install_error}", flush=True)
                        time.sleep(INSTALL_RETRY_S)
                if self.stopping:
                    return
                self.write_properties()
                if self.settings()["run"]:
                    self.bds.start()
            finally:
                self._busy = ""

    def _auto_update_on_boot(self) -> None:
        """Only when auto-update is on AND Mojang has something newer: back the world
        up cold, then install. A failed backup skips the update rather than blocking
        the server — the installed version still runs."""
        if self.wanted_version() != "latest":
            install.ensure(
                SERVER_DIR, self.wanted_version()
            )  # a pin, or auto-update off
            return
        have = install.installed_version(SERVER_DIR)
        info = self.version_info(refresh=True)
        if not info["update_available"]:
            return
        try:
            self.cold_snapshot(f"pre-auto-update-{have}")
        except Exception as exc:
            print(f"[auto-update] skipped, backup failed: {exc}", flush=True)
            return
        got = install.ensure(SERVER_DIR, info["latest"])
        if got != info["latest"]:
            print(f"[auto-update] download failed; staying on {got}", flush=True)

    def write_properties(self) -> None:
        install.apply_properties(SERVER_DIR / "server.properties", self.properties())

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
            "players": [
                {"name": n, "xuid": p["xuid"], "joined_at": p["joined_at"]}
                for n, p in sorted(b.players.items())
            ],
            "boot_id": bds.BOOT_ID,
            "lan_ip": lan_ip(),
            "auto_update": self.settings()["auto_update"],
            "run": self.settings()["run"],
            "update": dict(self.update_state),
            "started_at": started,
            "uptime_s": round(time.time() - started) if started and b.running else None,
            "exit_code": b.exit_code,
            "snapshots": len(list_snapshots()),
        }

    def snapshot(self, label: str) -> dict[str, Any]:
        """A backup of the world: hot (save hold/query/resume) while BDS runs, a plain
        copy of the folder while it is stopped."""
        if not self.bds.running:
            return self.cold_snapshot(label)
        data, files = self.bds.snapshot(self.world_dir())
        return self._keep(label, data, len(files))

    def cold_snapshot(self, label: str) -> dict[str, Any]:
        world = self.world_dir()
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(world.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(world).as_posix())
                    count += 1
        return self._keep(label, buf.getvalue(), count)

    def restore(self, name: str) -> None:
        """Replace the world folder with a snapshot's contents. The folder being
        replaced is moved aside, never deleted, so a bad restore is itself undoable."""
        src = (SNAPSHOT_DIR / name).resolve()
        if SNAPSHOT_DIR.resolve() not in src.parents or not src.is_file():
            raise ValueError(f"no such snapshot: {name}")
        world = self.world_dir()
        if world.exists():
            world.rename(world.with_name(f"{world.name}.replaced-{int(time.time())}"))
        world.mkdir(parents=True)
        root = world.resolve()
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                target = (root / info.filename).resolve()
                if root not in target.parents:
                    raise ValueError(
                        f"snapshot member escapes the world: {info.filename}"
                    )
            zf.extractall(world)

    def _keep(self, label: str, data: bytes, files: int) -> dict[str, Any]:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        slug = _LABEL.sub("-", label).strip("-")[:40]
        level = _LABEL.sub("-", self.properties().get("level-name", "world"))
        name = f"{level}-{stamp}{'-' + slug if slug else ''}.mcworld"
        (SNAPSHOT_DIR / name).write_bytes(data)
        prune_automatic()
        return {"name": name, "bytes": len(data), "files": files}

    def version_info(self, refresh: bool = False) -> dict[str, Any]:
        """Running vs latest. The lookup is cached: Mojang's index changes a few times
        a month, and a status poll every few seconds must not become a request each."""
        stale = time.time() - self.latest.get("checked_at", 0) > VERSION_TTL_S
        if refresh or stale:
            try:
                version, url = install.resolve("latest")
                self.latest = {"version": version, "url": url, "error": None}
            except Exception as exc:
                self.latest = {**self.latest, "error": f"{type(exc).__name__}: {exc}"}
            self.latest["checked_at"] = time.time()
        running = self.bds.version or install.installed_version(SERVER_DIR) or ""
        latest = self.latest.get("version") or ""
        return {
            "running": running or None,
            "latest": latest or None,
            "update_available": bool(
                running and latest and version_key(latest) > version_key(running)
            ),
            "checked_at": self.latest.get("checked_at"),
            "check_error": self.latest.get("error"),
        }

    def start_update(self, then_start: bool = False) -> dict[str, Any]:
        """Backup, install the latest BDS, restart — in the background; poll /status.

        The snapshot comes FIRST and a failed snapshot installs nothing: the whole
        point of the button is that an update is never the thing that loses a world.
        `then_start` is a Start/Restart with auto-update on: start the server after
        the install even if it was stopped."""
        if not self._life.acquire(blocking=False):
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        try:
            info = self.version_info(refresh=True)
        except Exception:
            self._life.release()
            raise
        if not info["update_available"]:
            self._life.release()
            return {"state": "current", "running": info["running"]}
        self._busy = "updating"
        self.update_state = {
            "state": "backing_up",
            "from": info["running"],
            "to": info["latest"],
            "backup": None,
            "started_at": time.time(),
            "error": None,
        }
        threading.Thread(
            target=self._run_update, args=(info, then_start), daemon=True
        ).start()
        return dict(self.update_state)

    def _run_update(self, info: dict[str, Any], then_start: bool) -> None:
        """backing_up → downloading → restarting → done, or one of two failures:
        `failed` (nothing new installed; the old version is restarted if it was
        running) and `rolled_back` (installed but would not start; the old version is
        reinstalled and the world restored from the pre-update backup). Every install
        is verified against the version asked for — `install.ensure` keeps what is
        there when a download fails, and that must never read as success."""
        st = self.update_state
        was_running = self.bds.running
        start_after = was_running or then_start
        old, new = info["running"], info["latest"]
        try:
            st["backup"] = self.snapshot(f"pre-update-{old}")["name"]
            st["state"] = "downloading"
            self.maintenance = True
            if was_running:
                self.bds.stop()
            got = ""
            try:
                got = install.ensure(SERVER_DIR, new)
            except Exception as exc:
                st["error"] = f"install: {exc}"
            if got != new:
                st["state"] = "failed"
                st["error"] = st["error"] or f"download of {new} failed; still on {old}"
                if start_after:
                    self.bds.start()
                return
            self.write_properties()
            if not start_after:
                st["state"] = "done"
                return
            st["state"] = "restarting"
            self.bds.start()
            if self.bds.wait_running(START_TIMEOUT_S):
                st["state"] = "done"
                return
            # Installed but would not start: put back what worked, world included —
            # the new version may already have rewritten it before failing.
            self.bds.stop()
            if install.ensure(SERVER_DIR, old) != old:
                # Neither version can run. Leave it stopped and say so, rather than
                # crash-looping the broken one under the container's restart policy.
                self.set_settings({"run": False})
                st.update(
                    state="failed",
                    error=f"{new} did not start and {old} could not be reinstalled; "
                    "the server is stopped, the backup is kept",
                )
                return
            self.restore(st["backup"])
            self.write_properties()
            self.bds.start()
            st.update(
                state="rolled_back",
                error=f"{new} did not start; back on {old} from the backup",
            )
        except Exception as exc:
            st.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            if was_running and not self.bds.running and not self.stopping:
                self.bds.start()
        finally:
            st["finished_at"] = time.time()
            self.maintenance = False
            self._busy = ""
            self._life.release()

    def server_action(self, action: str) -> dict[str, Any]:
        """Start, stop or restart the game server inside the running container.

        - Stop is graceful (a console `stop` that saves the world) and is remembered,
          so a box reboot doesn't bring back a server the owner turned off.
        - With auto-update on and a newer version out, Start and Restart run the
          backed-up update instead (that is what the screen's confirm promised).
        - While first boot is still installing, the wish is recorded and first boot
          acts on it (`deferred`).
        - While an update runs, refused."""
        if action not in ("start", "stop", "restart"):
            raise ValueError(f"unknown action: {action}")
        if self._busy == "updating":
            raise RuntimeError("an update is running")
        if not self._life.acquire(blocking=False):
            if self._busy == "installing":
                self.set_settings({"run": action != "stop"})
                return {"action": action, "deferred": True}
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        update_info: dict[str, Any] | None = None
        try:
            self.set_settings({"run": action != "stop"})
            if action != "stop" and self.settings()["auto_update"]:
                info = self.version_info(refresh=True)
                if info["update_available"]:
                    update_info = info
            if update_info is None:
                self._busy = action
                self.maintenance = True
                if action in ("stop", "restart"):
                    self.bds.stop()
                if action in ("start", "restart"):
                    self.write_properties()
                    self.bds.start()
                return {"action": action, "state": self.bds.state}
        finally:
            self.maintenance = False
            self._busy = ""
            self._life.release()
        return {"action": action, "update": self.start_update(then_start=True)}

    def probe_pack(self, install_it: bool) -> dict[str, Any]:
        """Install (or remove) the bundled probe behavior pack in the active world and
        restart the server so it loads. M0 item 5: does a stable-API pack load with no
        experiments, and does its `console.log` reach this console?"""
        manifest = json.loads((PROBE_PACK_SRC / "manifest.json").read_text())
        pack_id = manifest["header"]["uuid"]
        world = self.world_dir()
        dest = world / "behavior_packs" / PROBE_PACK_DIR
        listing = world / "world_behavior_packs.json"
        try:
            packs = json.loads(listing.read_text())
        except (FileNotFoundError, ValueError):
            packs = []
        packs = [p for p in packs if p.get("pack_id") != pack_id]
        if install_it:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(PROBE_PACK_SRC, dest)
            packs.append({"pack_id": pack_id, "version": manifest["header"]["version"]})
        elif dest.exists():
            shutil.rmtree(dest)
        listing.write_text(json.dumps(packs, indent=2))
        if not self._life.acquire(blocking=False):
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        self.maintenance = True
        try:
            self.bds.stop()
            self.bds.start()
        finally:
            self.maintenance = False
            self._life.release()
        return {"installed": install_it, "pack_id": pack_id, "restarted": True}

    def shutdown(self) -> None:
        self.stopping = True
        self.bds.stop()


# Automatic backups (pre-update, pre-auto-update) are kept to this many; the owner's
# own labelled snapshots are never pruned here (M3 gives them pinning and a count).
KEEP_AUTOMATIC = 10


def prune_automatic() -> None:
    auto = [
        p
        for p in sorted(SNAPSHOT_DIR.glob("*.mcworld"), key=lambda p: p.stat().st_mtime)
        if "-pre-update-" in p.name or "-pre-auto-update-" in p.name
    ]
    for old in auto[:-KEEP_AUTOMATIC]:
        old.unlink(missing_ok=True)


def lan_ip() -> str | None:
    """The box's LAN address, for "how to join". The container is on the host network,
    so the source address the kernel would pick toward the internet is the box's own.
    A UDP connect sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return str(s.getsockname()[0])
    except OSError:
        return None


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


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
        elif url.path == "/events":
            after = int(query.get("after", ["0"])[0])
            self._send(
                200, {"boot_id": bds.BOOT_ID, "events": rig.bds.events_after(after)}
            )
        elif url.path == "/settings":
            self._send(200, rig.settings())
        elif url.path == "/version":
            refresh = query.get("refresh", ["0"])[0] in ("1", "true")
            self._send(200, rig.version_info(refresh=refresh))
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
            elif self.path == "/update":
                self._send(202, rig.start_update())
            elif self.path in ("/server/start", "/server/stop", "/server/restart"):
                self._send(200, rig.server_action(self.path.rsplit("/", 1)[1]))
            elif self.path == "/settings":
                self._send(200, rig.set_settings(body))
            elif self.path == "/probe-pack":
                self._send(200, rig.probe_pack(bool(body.get("install", True))))
            else:
                self._send(404, {"detail": "not found"})
        except bds.ConsoleError as exc:
            code = 409 if "not running" in str(exc) else 400
            self._send(code, {"detail": str(exc)})
        except RuntimeError as exc:
            self._send(409, {"detail": str(exc)})
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})


def crashed(rig: Rig) -> bool:
    """Did BDS exit on its own? Not during a wrapper-driven stop (maintenance), and not
    when the owner stopped it (run is off) — only then does the container exit so its
    restart policy brings the server back."""
    if rig.maintenance or not rig.settings()["run"]:
        return False
    return rig.bds.exit_code is not None and not rig.bds.running


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
        if rig.maintenance:
            time.sleep(1)
            continue
        if crashed(rig):
            print(f"[minecraft] server exited ({rig.bds.exit_code})", flush=True)
            sys.exit(1)
        time.sleep(1)


if __name__ == "__main__":
    main()
