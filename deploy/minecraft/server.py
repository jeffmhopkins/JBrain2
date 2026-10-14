"""The `minecraft` sidecar: Bedrock Dedicated Server plus the control surface around it.

docs/plans/MINECRAFT_BEDROCK_PLAN.md (M0a rig, M1 card backend). The container is on
the host network; the api reaches this control surface with a bearer, and players reach
BDS on 19132 and never this HTTP surface.

Stdlib only, like the endpoint and sdr sidecars: the HTTP surface is `http.server`, the
game server is Mojang's binary in the data volume (install.py), and its console is a
pipe owned by bds.py.
"""

from __future__ import annotations

import collections
import contextlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import Any

import blocks
import bds
import containers
import entities
import install
import mapping
import structure_sweep
import worlds

DATA = Path(os.environ.get("MC_DATA_DIR", "/data"))
SERVER_DIR = DATA / "server"
SNAPSHOT_DIR = DATA / "snapshots"
OVERRIDES = DATA / "properties.json"
SETTINGS = DATA / "settings.json"
SLOTS = DATA / "slots.json"
SNAPSHOT_INDEX = DATA / "snapshot-index.json"
# Each world's gamertag -> script `player.id`, learned from the asker's `pid` on every
# `/dave` (§M4e): the files key a pet's owner by that id and never name the player, and
# the app's "Hank's pets" has to work while Hank is offline.
PLAYER_IDS = DATA / "player-ids.json"
UPLOAD_DIR = DATA / "uploads"
# The saved structure sweeps (§M8), one file per world folder.
STRUCTURE_DIR = DATA / "structures"
# A sweep that failed (the server stopped mid-way) is retried by a view no sooner than this.
SWEEP_RETRY_S = 900.0
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
# How long players get between the chat warning and the server stopping under them.
WARN_S = 10.0
# A world operation answers within this long, or as 202 and carries on in the background:
# Cloudflare's tunnel cuts a request at about 100 s, and a load or restore of a big world
# (warning, backup, stop, write, start) can take longer than that.
JOB_WAIT_S = 20.0
# server.properties keys that only take effect when the server starts, with BDS's own
# defaults: a key never set is at its default, so setting it to that isn't a change.
RESTART_KEYS = {
    "allow-cheats": "false",
    "allow-list": "false",
    "difficulty": "easy",
    "gamemode": "survival",
    "max-players": "10",
    "server-name": "Dedicated Server",
    "view-distance": "32",
}
# The probe behavior pack (M0 item 5), bundled in the image — nothing uploaded. It only
# logs now: the items it once carried live in the jbrain pack.
PROBE_PACK_SRC = Path(__file__).resolve().parent / "probe-pack"
PROBE_PACK_DIR = "jbrain_probe"
# The kit's old resource pack, retired when its icons moved to the jbrain one: taken out
# of any world that still lists it, so a world never pushes two packs drawing one item.
RETIRED_RESOURCE_PACKS = (("jbrain_probe", "26d6c250-8ead-4bb8-9dd0-d3805e61d61e"),)
# The box's own behavior pack, installed into whichever world loads (§T1: deaths and
# respawns, which BDS never prints; §M9/§M10/§DW1: the Tricorder and the Power Pack).
JBRAIN_PACK_SRC = Path(__file__).resolve().parent / "jbrain-pack"
JBRAIN_PACK_DIR = "jbrain"
# Its resource pack (§3c rule 2): the items' icons, installed beside it in every world.
# BDS pushes a world's resource packs to each client on join, and
# `texturepacks-required` (install.env_overrides) makes taking it a condition of joining.
JBRAIN_RP_SRC = Path(__file__).resolve().parent / "jbrain-rp"
# The spawn wreck's pack (§SW1): installed only into a world the box generated itself,
# armed when it was created; its structures are built into it at image build.
WRECK_PACK_SRC = Path(__file__).resolve().parent / "wreck-pack"
WRECK_PACK_DIR = "jbrain_wreck"
# How long a brand-new armed world's first start may take to generate (a slow box), and
# then how long its pack has to say it loaded.
WRECK_START_S = 600.0
WRECK_WAIT_S = 90.0
# Travel log (§T1): each online player's position, this often, kept only once they have
# moved this far — standing still costs nothing.
TRACK_S = 10.0
# The box's public address, for "how to join" from outside. Asked of Cloudflare's trace
# page by IP, so no DNS is involved, every PUBLIC_IP_S; a lookup that fails keeps the last.
PUBLIC_IP_URL = "https://1.1.1.1/cdn-cgi/trace"
PUBLIC_IP_S = 600.0
# A chest search (§M23) first has the running world write itself out — the snapshot's
# save hold/query/resume, nothing copied — at most this often, waiting this long for it.
FLUSH_EVERY_S = 30.0
# A stock count's points: a player's saved places (Dave caps them at 100 a world) and
# where they stand.
STOCK_CENTRES_MAX = 101
# A stock count's reach around each point: the Tricorder's short-range scan, or a little
# more, never a survey.
STOCK_RADIUS_MAX = 256
FLUSH_WAIT_S = 10.0
# Portals the Tricorder may be pointed through (§M4e/§M9): a few nearest, never a list
# a console line can't carry.
PORTALS_MAX = 64
# A portal still stands when its block is within this many blocks of where its record
# says a traveller arrives.
PORTAL_BLOCK_REACH = 4
TRACK_MIN_MOVE = 4.0
TRACK_KEPT = 20000
_DIMENSIONS = {0: "overworld", 1: "nether", 2: "the_end"}


class Rig:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.bds = bds.Bds(SERVER_DIR)
        self.install_error = ""
        self.stopping = False
        # True while the wrapper itself stops and restarts BDS (an update, a pack
        # install), so the watchdog in main() does not read it as a crash.
        self.maintenance = False
        self.public_ip: str | None = None
        self.latest: dict[str, Any] = {}
        self.update_state: dict[str, Any] = {"state": "idle"}
        # One lifecycle at a time: first-boot install, Start/Stop/Restart, an update,
        # the probe pack. `_busy` names the holder so a refusal can say why.
        self._life = threading.Lock()
        self._busy = ""
        # What a world operation is doing right now, for /status, so a second device or
        # a reload sees the job instead of a server that looks idle.
        self._phase = ""
        self._job_started: float | None = None
        # server.properties as of the last start: a setting that differs from it is saved
        # but waiting for a restart, which every device can then show.
        self._applied: dict[str, str] = {}
        # How the most recent world operation ended, for a client that got a 202.
        self.last_job: dict[str, Any] | None = None
        # Position samples for the api's travel-log drain, numbered per boot like events.
        self._track: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=TRACK_KEPT
        )
        self._track_id = 0
        self._last_pos: dict[str, dict[str, Any]] = {}
        self._track_lock = threading.Lock()
        # The last event note_wreck() has read, and each world's latest build phase (for
        # the PWA's "building — the pod" while it's armed).
        self._wreck_seen = 0
        self._wreck_phase: dict[str, tuple[str, float]] = {}
        # Map tiles (§M8): one chunk index per world folder, and the PNGs it rendered,
        # both dropped when that world's database files change.
        self._map_lock = threading.Lock()
        self._surface_lock = threading.Lock()
        self._map_index: dict[str, mapping.WorldIndex] = {}
        self._block_index: dict[str, blocks.BlockIndex] = {}
        self._container_index: dict[str, containers.ContainerIndex] = {}
        self._entity_index: dict[str, entities.EntityIndex] = {}
        self._ids_lock = threading.Lock()
        self.bds.on_player_id = self.remember_player_id
        # When the running world was last written out for a chest search (§M23).
        self._flushed_at: float | None = None
        self._map_seed: dict[str, str | None] = {}
        self._map_spawn: dict[str, dict[str, int] | None] = {}
        self._predictor = mapping.Predictor()
        self._tiles: dict[tuple, bytes] = {}
        # One structure sweep at a time (it is console time), and how it is going.
        self._sweep_lock = threading.Lock()
        self._sweep: dict[str, Any] = {"state": "idle"}
        self.slots = worlds.SlotStore(
            SLOTS, SERVER_DIR / "worlds", int(self.env.get("MC_SLOTS", "5"))
        )
        self.index = worlds.SnapshotIndex(SNAPSHOT_INDEX, SNAPSHOT_DIR)

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
            # Whether a world the box creates or regenerates starts at the crash site.
            "spawn_wreck": bool(raw.get("spawn_wreck", True)),
        }

    def set_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        for key in ("auto_update", "run", "spawn_wreck"):
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
                    self._start()
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

    def _start(self) -> None:
        """Every start goes through here, so the loaded world's saved rules come back
        each time — not only on a Load — and a change saved while stopped lands."""
        self._step("starting")
        self.write_properties()
        self._applied = dict(self.properties())
        self.bds.world = self.active_folder()
        rec = self.slots.by_folder(self.active_folder())
        fresh = bool(rec and rec.get("wreck") == "armed" and not rec["exists"])
        before = self.bds.events_after(0) if fresh else []
        seen = before[-1]["id"] if before else 0
        # The wreck first: for an armed new world it makes the folder, so the box's own
        # pack reaches that world's first session too.
        self._install_wreck()
        try:
            self.install_pack(JBRAIN_PACK_SRC, JBRAIN_PACK_DIR, True)
            self.install_pack(JBRAIN_RP_SRC, JBRAIN_PACK_DIR, True, kind="resource")
            for dir_name, pack_id in RETIRED_RESOURCE_PACKS:
                self.remove_pack(dir_name, pack_id, kind="resource")
        except OSError as exc:
            # The travel log loses deaths, but the server itself must still start.
            print(f"[minecraft] jbrain pack not installed: {exc}", flush=True)
        # The home-test kit, where a world has it, is refreshed the same way: a copy left
        # from an older image kept registering `/jb:dave` after the command moved to the
        # jbrain pack, so the new one failed to register and the kit ran stale code.
        if (self.world_dir() / "behavior_packs" / PROBE_PACK_DIR).is_dir():
            try:
                self.install_pack(PROBE_PACK_SRC, PROBE_PACK_DIR, True)
            except OSError as exc:
                print(f"[minecraft] test kit not refreshed: {exc}", flush=True)
        self.bds.start()
        if fresh:
            folder = self.active_folder()
            threading.Thread(
                target=self._watch_wreck, args=(folder, seen), daemon=True
            ).start()
        self._apply_rules_when_up(self.active_folder())

    def _stop(self, why: str) -> None:
        """Stop the server for an owner action. Anyone playing is told in chat first and
        given WARN_S to finish what they're doing; the stop itself saves the world."""
        if self.bds.running and self.bds.players:
            self._step("warning players")
            with contextlib.suppress(bds.ConsoleError):
                self.bds.command(f"say {why} - the server stops in {WARN_S:g} seconds")
            time.sleep(WARN_S)
        self._step("stopping")
        self.bds.stop()

    def _step(self, phase: str) -> None:
        self._phase = phase

    def active_folder(self) -> str:
        return self.properties().get("level-name", "world")

    def world_dir(self, folder: str | None = None) -> Path:
        return SERVER_DIR / "worlds" / (folder or self.active_folder())

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
            "public_ip": self.public_ip,
            "auto_update": self.settings()["auto_update"],
            "spawn_wreck": self.settings()["spawn_wreck"],
            "run": self.settings()["run"],
            "update": dict(self.update_state),
            "started_at": started,
            "uptime_s": round(time.time() - started) if started and b.running else None,
            "exit_code": b.exit_code,
            "snapshots": len(self.index.listing()),
            "pending_restart": self.pending_restart(),
            "last_job": self.last_job,
            "job": {
                "what": self._busy,
                "phase": self._phase or None,
                "started_at": self._job_started,
            }
            if self._busy
            else None,
        }

    def pending_restart(self) -> list[str]:
        """Restart-bound settings saved since the server last started. Empty before the
        first start, when there is nothing to compare against."""
        if not self._applied:
            return []
        now = self.properties()
        return sorted(
            k
            for k, default in RESTART_KEYS.items()
            if now.get(k, default) != self._applied.get(k, default)
        )

    def snapshot(
        self,
        label: str,
        folder: str | None = None,
        auto: bool | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """A backup of one world: hot (save hold/query/resume) when it is the one
        running, a plain copy of its folder otherwise. `auto` marks the safety
        snapshots taken before a risky action; by default a `pre-` label is one."""
        folder = folder or self.active_folder()
        auto = label.startswith("pre-") if auto is None else auto
        if folder != self.active_folder() or not self.bds.running:
            return self.cold_snapshot(label, folder, auto, note)
        data, files = self.bds.snapshot(self.world_dir(folder))
        return self._keep(label, data, len(files), folder, auto, note)

    def cold_snapshot(
        self, label: str, folder: str | None = None, auto: bool = True, note: str = ""
    ) -> dict[str, Any]:
        folder = folder or self.active_folder()
        world = self.world_dir(folder)
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(world.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(world).as_posix())
                    count += 1
        return self._keep(label, buf.getvalue(), count, folder, auto, note)

    def snapshot_file(self, name: str) -> Path:
        src = (SNAPSHOT_DIR / name).resolve()
        if SNAPSHOT_DIR.resolve() not in src.parents or not src.is_file():
            raise ValueError(f"no such snapshot: {name}")
        return src

    def restore(self, name: str, folder: str | None = None) -> None:
        """Replace a world folder with a snapshot's contents. The folder being
        replaced is moved aside first and removed only once the restore succeeded."""
        src = self.snapshot_file(name)
        world = self.world_dir(folder)
        root = world.resolve()
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                target = (root / info.filename).resolve()
                if root not in target.parents:
                    raise ValueError(f"snapshot member escapes: {info.filename}")
        aside = world.with_name(f"{world.name}.replaced-{int(time.time() * 1000)}")
        if world.exists():
            world.rename(aside)
        try:
            world.mkdir(parents=True)
            with zipfile.ZipFile(src) as zf:
                zf.extractall(world)
        except Exception:
            shutil.rmtree(world, ignore_errors=True)
            if aside.exists():
                aside.rename(world)
            raise
        shutil.rmtree(aside, ignore_errors=True)

    def _keep(
        self, label: str, data: bytes, files: int, folder: str, auto: bool, note: str = ""
    ) -> dict[str, Any]:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        slug = _LABEL.sub("-", label).strip("-")[:40]
        name = f"{folder}-{stamp}{'-' + slug if slug else ''}.mcworld"
        (SNAPSHOT_DIR / name).write_bytes(data)
        self.index.record(name, folder, label, auto, note)
        self.index.prune(folder)
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
                self._stop("Updating Minecraft")
            got = ""
            try:
                got = install.ensure(SERVER_DIR, new)
            except Exception as exc:
                st["error"] = f"install: {exc}"
            if got != new:
                st["state"] = "failed"
                st["error"] = st["error"] or f"download of {new} failed; still on {old}"
                if start_after:
                    self._start()
                return
            self.write_properties()
            if not start_after:
                st["state"] = "done"
                return
            st["state"] = "restarting"
            self._start()
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
            self._start()
            st.update(
                state="rolled_back",
                error=f"{new} did not start; back on {old} from the backup",
            )
        except Exception as exc:
            st.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            if was_running and not self.bds.running and not self.stopping:
                self._start()
        finally:
            st["finished_at"] = time.time()
            self.maintenance = False
            self._busy = ""
            self._phase = ""
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
                if action == "stop":
                    self._stop("The server is being stopped")
                if action == "restart":
                    self._stop("The server is restarting")
                if action in ("start", "restart"):
                    self._start()
                return {"action": action, "state": self.bds.state}
        finally:
            self.maintenance = False
            self._busy = ""
            self._phase = ""
            self._life.release()
        return {"action": action, "update": self.start_update(then_start=True)}

    def run_job(
        self, what: str, fn: Callable[[], Any], wait_s: float | None = None
    ) -> tuple[int, Any]:
        """Run a world operation in a thread. One that finishes (or fails — a refusal
        is immediate) within `wait_s` answers as usual; a longer one answers 202 and
        the client follows `job` in /status, then reads `last_job` for the outcome."""
        done = threading.Event()
        box: dict[str, Any] = {}

        def go() -> None:
            try:
                box["result"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                err = box.get("error")
                self.last_job = {
                    "what": what,
                    "ok": err is None,
                    "detail": str(err) if err is not None else None,
                    "finished_at": time.time(),
                }
                done.set()

        threading.Thread(target=go, daemon=True).start()
        if done.wait(JOB_WAIT_S if wait_s is None else wait_s):
            if "error" in box:
                raise box["error"]
            return 200, box["result"]
        return 202, {"accepted": True, "what": what}

    @contextlib.contextmanager
    def _locked(self):
        """The lifecycle lock for a quick edit (a rename, a setting, a rule) so it can't
        interleave with a world operation rewriting the same slot; no job is shown."""
        if not self._life.acquire(blocking=False):
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        try:
            yield
        finally:
            self._life.release()

    @contextlib.contextmanager
    def _exclusive(self, what: str):
        """Hold the lifecycle lock for a world operation; refuse (409) when another
        lifecycle (an install, an update, a start/stop) owns the server."""
        if not self._life.acquire(blocking=False):
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        self._busy = what
        self._job_started = time.time()
        self.maintenance = True
        try:
            yield
        finally:
            self.maintenance = False
            self._busy = ""
            self._phase = ""
            self._job_started = None
            self._life.release()

    def worlds_view(self) -> dict[str, Any]:
        active = self.active_folder()
        out = []
        for rec in self.slots.all():
            folder = rec["folder"]
            d = self.world_dir(folder)
            if rec["exists"] and not rec["seed"]:
                # A world from before the slot records (or from before level.dat was
                # read) learns its seed from its own header, once.
                seed = worlds.world_facts(d).get("seed")
                if seed:
                    rec = {**rec, **self.slots.update(rec["id"], seed=seed)}
            backups = self.index.listing(folder)
            out.append(
                {
                    **rec,
                    "active": folder == active,
                    "bytes": worlds.dir_size(d) if rec["exists"] else 0,
                    "last_played": (d / "level.dat").stat().st_mtime
                    if (d / "level.dat").is_file()
                    else None,
                    "backups": len(backups),
                    "last_backup": backups[0]["created"] if backups else None,
                    "last_download": self.index.last_download(folder),
                    # Where an armed world's build has got to: loaded, pod, ship, failed.
                    "wreck_phase": self._phase_of(rec),
                }
            )
        return {"slots": out, "active": active, "keep_per_slot": worlds.KEEP_PER_SLOT}

    def _apply_slot(self, rec: dict[str, Any]) -> None:
        """Point server.properties at a slot: its folder, and its per-world settings.
        The seed only matters the first time a world is generated."""
        self.set_overrides(
            {
                "level-name": rec["folder"],
                "gamemode": rec["gamemode"],
                "difficulty": rec["difficulty"],
                "allow-cheats": "true" if rec["cheats"] else "false",
                "level-seed": rec["seed"],
            }
        )
        self.write_properties()

    def _restart_around(self, folder: str, work, why: str) -> None:
        """Run `work` with the server stopped if it is running `folder`; start it again
        after if the owner wants it running."""
        touching = folder == self.active_folder()
        if touching and self.bds.running:
            self._stop(why)
        try:
            self._step("writing")
            work()
        finally:
            if touching and self.settings()["run"] and not self.bds.running:
                self._start()

    def load_slot(self, slot_id: str) -> dict[str, Any]:
        rec = self.slots.get(slot_id)
        with self._exclusive("loading a world"):
            if rec["folder"] == self.active_folder():
                return {"slot": slot_id, "loaded": False, "detail": "already loaded"}
            if self.bds.running:
                self._step("backing up")
                self.snapshot("pre-load", auto=True, note=f"before loading {rec['name']}")
                self._stop(f"Switching to the world {rec['name']}")
            if not rec["exists"] and not rec["seed"]:
                rec = self.slots.update(
                    slot_id,
                    seed=worlds.new_seed(),
                    origin="new",
                    created_at=time.time(),
                    wreck=self._arm(),
                )
            self._step("writing")
            self._apply_slot(rec)
            if self.settings()["run"]:
                self._start()
        self.slots.update(slot_id, last_loaded=time.time())
        return {"slot": slot_id, "loaded": True}

    def create_slot(self, slot_id: str, body: dict[str, Any]) -> dict[str, Any]:
        rec = self.slots.get(slot_id)
        if rec["exists"]:
            raise ValueError("that slot already holds a world — reset it first")
        if body.get("test") and (rec["name"] or rec["seed"]):
            # The debug API's guard, kept here too: a test world only goes in an empty
            # slot, never over a world the owner made and hasn't generated yet.
            raise ValueError("a test world only goes in an empty slot")
        seed = worlds.check_seed(str(body.get("seed") or "")) or worlds.new_seed()
        rules = worlds.check_rules(dict(body.get("rules") or {}), worlds.DEFAULT_RULES)
        return self.slots.update(
            slot_id,
            name=body.get("name") or f"World {slot_id[-1]}",
            seed=seed,
            gamemode=body.get("gamemode", "survival"),
            difficulty=body.get("difficulty", "normal"),
            cheats=bool(body.get("cheats", False)),
            rules=rules,
            origin="new",
            created_at=time.time(),
            wreck=self._arm(body.get("wreck")),
            test=bool(body.get("test")),
            # Set either way: a named, never-generated slot may hold an old choice.
            power_pack_transport=body.get("power_pack_transport", True),
        )

    def update_slot(self, slot_id: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._locked():
            return self._update_slot(slot_id, body)

    def _update_slot(self, slot_id: str, body: dict[str, Any]) -> dict[str, Any]:
        fields = {k: body[k] for k in ("name", "gamemode", "difficulty") if k in body}
        if "cheats" in body:
            fields["cheats"] = bool(body["cheats"])
        # The owner changing a test world makes it theirs, out of the debug API's reach.
        rec = self.slots.update(slot_id, **fields, test=False)
        active = rec["folder"] == self.active_folder()
        if active and "difficulty" in fields and self.bds.running:
            with contextlib.suppress(bds.ConsoleError):
                self.bds.command(f"difficulty {rec['difficulty']}")
                self._applied["difficulty"] = rec["difficulty"]
        if active and set(fields) - {"name"}:
            self.set_overrides(
                {
                    "gamemode": rec["gamemode"],
                    "difficulty": rec["difficulty"],
                    "allow-cheats": "true" if rec["cheats"] else "false",
                }
            )
        return {**rec, "applies": "next restart" if active else "next load"}

    def import_slot(self, slot_id: str, upload: Path, name: str = "") -> dict[str, Any]:
        """Put an uploaded .mcworld into a slot. An occupied slot is backed up first,
        and its folder is only removed once the new world is in place."""
        rec = self.slots.get(slot_id)
        prefix = worlds.check_world_zip(upload)
        folder = rec["folder"]
        world = self.world_dir(folder)
        with self._exclusive("importing a world"):
            if rec["exists"]:
                self._step("backing up")
                self.snapshot(
                    "pre-import", folder=folder, auto=True, note="before an import"
                )

            def work() -> None:
                aside = world.with_name(f"{folder}.replaced-{int(time.time() * 1000)}")
                if world.exists():
                    world.rename(aside)
                try:
                    worlds.extract_world(upload, prefix, world)
                except Exception:
                    shutil.rmtree(world, ignore_errors=True)
                    if aside.exists():
                        aside.rename(world)
                    raise
                shutil.rmtree(aside, ignore_errors=True)
                self.bds.note_world_replaced(folder)

                # The world brings its own settings; the overwritten slot's game
                # mode, rules and seed belonged to the world that is gone.
                self._adopt(
                    slot_id,
                    name=name or worlds.level_name(world) or "Imported world",
                    origin="imported",
                    created_at=time.time(),
                    rules={},
                    wreck=None,  # the owner's own world: never touched
                    test=False,
                    power_pack_keep=False,
                    power_pack_transport=True,
                )

            self._restart_around(folder, work, "A world is being imported here")
        return self.slots.get(slot_id)

    def reset_slot(
        self,
        slot_id: str,
        mode: str,
        seed: str = "",
        wreck: Any = None,
        test: bool = False,
    ) -> dict[str, Any]:
        rec = self.slots.get(slot_id)
        folder = rec["folder"]
        active = folder == self.active_folder()
        if mode not in ("same_seed", "new_seed", "empty"):
            raise ValueError("reset mode must be same_seed, new_seed or empty")
        if test and not rec["test"]:
            # A test reset keeps a mark, never makes one: the owner's world stays theirs.
            raise ValueError("only a test world can be reset as one")
        if mode == "empty" and active:
            raise ValueError("the loaded world can't be emptied — load another first")
        seed = worlds.check_seed(seed)
        if mode == "same_seed" and not rec["seed"]:
            raise ValueError(
                "this world's seed isn't known, so it can't be regenerated"
            )
        with self._exclusive("resetting a world"):
            if rec["exists"]:
                self._step("backing up")
                self.snapshot("pre-reset", folder=folder, auto=True, note="before a reset")

            def work() -> None:
                shutil.rmtree(self.world_dir(folder), ignore_errors=True)
                self.bds.note_world_replaced(folder)
                if mode == "empty":
                    self.slots.clear(slot_id)
                    return
                new = (
                    rec["seed"] if mode == "same_seed" else (seed or worlds.new_seed())
                )
                fresh = self.slots.update(
                    slot_id,
                    seed=new,
                    origin="reset",
                    created_at=time.time(),
                    wreck=self._arm(wreck),
                    test=test,
                )
                if active:
                    self._apply_slot(fresh)

            self._restart_around(folder, work, "This world is being reset")
        return self.slots.get(slot_id)

    def restore_snapshot(self, name: str, slot_id: str) -> dict[str, Any]:
        rec = self.slots.get(slot_id)
        folder = rec["folder"]
        path = self.snapshot_file(name)  # validates before anything is touched
        source = self.index.entry(name)["folder"]
        src = self.slots.by_folder(source) or {}
        # A restored world is the one in the backup, built or not; it is never re-armed.
        fields: dict[str, Any] = {"origin": "restored", "wreck": None, "test": False}
        if source != folder:
            # Another world's backup: the slot becomes that world, so its old seed and
            # settings no longer describe it. Rules come with the source's choices.
            fields["rules"] = dict(src.get("rules") or {})
            fields["power_pack_keep"] = bool(src.get("power_pack_keep"))
            fields["power_pack_transport"] = src.get("power_pack_transport") is not False
            if not rec["exists"]:
                when = time.strftime("%b %-d", time.localtime(path.stat().st_mtime))
                fields["name"] = f"{src.get('name') or source} ({when})"
                fields["created_at"] = time.time()
        with self._exclusive("restoring a backup"):
            if rec["exists"]:
                self._step("backing up")
                self.snapshot(
                    "pre-restore",
                    folder=folder,
                    auto=True,
                    note=f"before restoring {self.index.entry(name)['label'] or name}",
                )

            def work() -> None:
                self.restore(name, folder)
                self._adopt(slot_id, **fields)

            self._restart_around(folder, work, "This world is being restored")
        return self.slots.get(slot_id)

    def _adopt(self, slot_id: str, **fields: Any) -> None:
        """Record a world that just landed in a slot from outside (an import, another
        world's backup): what its level.dat says about itself, plus `fields`. A seed
        level.dat doesn't hold reads as unknown rather than keeping the old world's."""
        rec = self.slots.get(slot_id)
        facts = worlds.world_facts(self.world_dir(rec["folder"]))
        fresh = self.slots.update(slot_id, **{"seed": None, **facts, **fields})
        if fresh["folder"] == self.active_folder():
            self._apply_slot(fresh)

    def delete_snapshot(self, name: str) -> None:
        path = self.snapshot_file(name)
        if self.index.entry(name)["pinned"]:
            raise ValueError("that backup is pinned — unpin it first")
        path.unlink()
        self.index.forget(name)

    def allowlist(self) -> dict[str, Any]:
        try:
            entries = json.loads((SERVER_DIR / "allowlist.json").read_text())
        except (FileNotFoundError, ValueError):
            entries = []
        return {
            "enabled": self.properties().get("allow-list", "false") == "true",
            "players": sorted(str(e.get("name", "")) for e in entries if e.get("name")),
        }

    def change_allowlist(self, body: dict[str, Any]) -> dict[str, Any]:
        if "enabled" in body:
            self.set_overrides({"allow-list": "true" if body["enabled"] else "false"})
        for verb in ("add", "remove"):
            name = str(body.get(verb, "")).strip()
            if not name:
                continue
            if any(c in name for c in '"\r\n'):
                raise ValueError("that isn't a gamertag")
            if self.bds.running:
                self.bds.command(f'allowlist {verb} "{name}"')
            else:
                path = SERVER_DIR / "allowlist.json"
                try:
                    entries = json.loads(path.read_text())
                except (FileNotFoundError, ValueError):
                    entries = []
                entries = [e for e in entries if e.get("name") != name]
                if verb == "add":
                    entries.append({"ignoresPlayerLimit": False, "name": name})
                path.write_text(json.dumps(entries, indent=2))
        return {**self.allowlist(), "applies": "on/off at next restart; names now"}

    def rules_view(self, slot_id: str) -> dict[str, Any]:
        """A world's rules. The loaded, running world is read live from the console
        (and remembered); any other world shows what it had when last loaded, with
        the owner's saved changes on top, marked pending until it's loaded."""
        rec = self.slots.get(slot_id)
        live = rec["folder"] == self.active_folder() and self.bds.state == "running"
        known = dict(rec.get("rules_known") or worlds.DEFAULT_RULES)
        if live:
            parsed = worlds.parse_gamerules(self.bds.command("gamerule", wait_s=3.0))
            if parsed:
                known = parsed
                self.slots.update(slot_id, rules_known=parsed)
        desired = effective_rules(rec)
        pending = (
            [] if live else sorted(k for k, v in desired.items() if known.get(k) != v)
        )
        return {
            "slot": slot_id,
            "live": live,
            "rules": {**known, **desired} if not live else known,
            "defaults": worlds.DEFAULT_RULES,
            "pending": pending,
            # Rules a box feature holds, by who holds them: the editor shows these as
            # managed rather than as a free switch, and a change to one is refused.
            "managed": managed_rules(rec),
        }

    def set_rules(self, slot_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Change world rules: instantly on the loaded world (console `gamerule`), and
        always saved on the slot, so they're re-applied whenever it's loaded."""
        with self._locked():
            self._set_rules(slot_id, changes)
        return self.rules_view(slot_id)

    def _set_rules(self, slot_id: str, changes: dict[str, Any]) -> None:
        rec = self.slots.get(slot_id)
        known = dict(rec.get("rules_known") or worlds.DEFAULT_RULES)
        held = sorted(set(changes) & set(managed_rules(rec)))
        if held:
            raise ValueError(
                f"{', '.join(held)} is managed by the Power Pack charm while it is on"
            )
        clean = worlds.check_rules(changes, known)
        self.slots.update(slot_id, rules={**dict(rec.get("rules") or {}), **clean})
        live = rec["folder"] == self.active_folder() and self.bds.state == "running"
        if live:
            for key, value in clean.items():
                reply = " ".join(self.bds.command(worlds.rule_command(key, value)))
                if "error" in reply.lower() or "unknown" in reply.lower():
                    raise ValueError(f"the server refused {key}: {reply.strip()}")

    def _apply_rules_when_up(self, folder: str) -> None:
        """After a world loads, re-apply its saved rules once the server reports
        "Server started." — in the background, so a load doesn't wait on it — and tell
        the jbrain pack the world's settings, which it keeps in the world itself."""
        rec = self.slots.by_folder(folder)
        commands = [worlds.rule_command(k, v) for k, v in effective_rules(rec).items()]
        commands.append(pack_config(rec))

        def apply() -> None:
            if not self.bds.wait_running(START_TIMEOUT_S):
                return
            for command in commands:
                try:
                    self.bds.command(command, wait_s=1.0)
                except bds.ConsoleError:
                    return

        threading.Thread(target=apply, daemon=True).start()

    def set_power_pack(self, slot_id: str, on: Any) -> dict[str, Any]:
        """The world's "Power Pack keeps inventory" switch (§M10). Saved on the slot and
        applied at every start; on the loaded, running world also at once: the rule,
        then the pack's own copy of the switch. Turning it off hands keepInventory back
        to the owner's saved choice, saved so a later load agrees."""
        if not isinstance(on, bool):
            raise ValueError("`on` is true or false")
        with self._locked():
            rec = self.slots.get(slot_id)
            rules = dict(rec.get("rules") or {})
            if on and not rec.get("power_pack_keep"):
                # A world with no saved choice (an import, a reset, a new world made
                # without the rule) runs whatever it last loaded with; pin that as the
                # saved choice so turning the switch off returns to what the owner saw,
                # not to vanilla's off.
                known = rec.get("rules_known") or worlds.DEFAULT_RULES
                rules.setdefault("keepInventory", bool(known.get("keepInventory")))
            elif not on:
                rules.setdefault("keepInventory", False)
            rec = self.slots.update(slot_id, power_pack_keep=on, rules=rules)
            live = rec["folder"] == self.active_folder() and self.bds.state == "running"
            if live:
                keep = effective_rules(rec)["keepInventory"]
                self.bds.command(worlds.rule_command("keepInventory", keep))
                self.bds.command(pack_config(rec))
        return {**rec, "applies": "now" if live else "next load"}

    def set_power_pack_transport(self, slot_id: str, on: Any) -> dict[str, Any]:
        """The world's "Power Pack teleport" switch (§M10), on by default. Saved on the
        slot and sent to the pack at every start; on the loaded, running world also at
        once. No game rule rides on it: off, the pack refuses every trip."""
        if not isinstance(on, bool):
            raise ValueError("`on` is true or false")
        with self._locked():
            rec = self.slots.update(slot_id, power_pack_transport=on)
            live = rec["folder"] == self.active_folder() and self.bds.state == "running"
            if live:
                self.bds.command(pack_config(rec))
        return {**rec, "applies": "now" if live else "next load"}

    def install_pack(
        self,
        src: Path,
        dir_name: str,
        install_it: bool,
        kind: str = "behavior",
        create: bool = False,
    ) -> str:
        """Put a bundled behavior or resource pack into the loaded world (or take it
        out) and list it in world_behavior_packs.json / world_resource_packs.json.
        Re-installing replaces the copy, so a world always runs the version this image
        ships."""
        manifest = json.loads((src / "manifest.json").read_text())
        pack_id = manifest["header"]["uuid"]
        world = self.world_dir()
        if create and install_it:
            world.mkdir(parents=True, exist_ok=True)
        if not world.is_dir():
            return pack_id  # a world BDS hasn't generated yet: next start installs it
        self.remove_pack(dir_name, pack_id, kind)
        if install_it:
            # A pack's README is for the repo: a world's packs go out in its .mcworld
            # and a resource pack to every client on join.
            shutil.copytree(
                src,
                world / f"{kind}_packs" / dir_name,
                ignore=shutil.ignore_patterns("README.md"),
            )
            listing = world / f"world_{kind}_packs.json"
            packs = json.loads(listing.read_text())
            packs.append({"pack_id": pack_id, "version": manifest["header"]["version"]})
            listing.write_text(json.dumps(packs, indent=2))
        return pack_id

    def remove_pack(self, dir_name: str, pack_id: str, kind: str = "behavior") -> None:
        """Take a pack out of the loaded world: its folder and its listing entry. The
        listing is written even when the pack wasn't there, so an install can append."""
        world = self.world_dir()
        if not world.is_dir():
            return
        dest = world / f"{kind}_packs" / dir_name
        listing = world / f"world_{kind}_packs.json"
        try:
            packs = json.loads(listing.read_text())
        except (FileNotFoundError, ValueError):
            packs = []
        packs = [p for p in packs if p.get("pack_id") != pack_id]
        if dest.exists():
            shutil.rmtree(dest)
        listing.write_text(json.dumps(packs, indent=2))

    def _phase_of(self, rec: dict[str, Any]) -> str | None:
        # A phase from before the slot's world was (re)made is the old world's build.
        phase, at = self._wreck_phase.get(rec["folder"], ("", 0.0))
        if rec.get("wreck") != "armed" or at < float(rec.get("created_at") or 0):
            return None
        return phase or None

    def _arm(self, want: Any = None) -> str | None:
        """The wreck state a world the box is about to generate starts in: armed unless
        the owner turned the crash site off (for this world, or by default)."""
        on = self.settings()["spawn_wreck"] if want is None else bool(want)
        return "armed" if on else None

    def _install_wreck(self) -> None:
        """The wreck's pack goes into an armed world and comes out of any other, so an
        imported or restored world never gets one. A new world has no folder until BDS
        generates it, and its first load is the only time spawn is untouched, so the
        folder is made for it; BDS then generates the world around the pack."""
        rec = self.slots.by_folder(self.active_folder())
        armed = bool(rec and rec.get("wreck") == "armed")
        try:
            self.install_pack(WRECK_PACK_SRC, WRECK_PACK_DIR, armed, create=armed)
        except OSError as exc:
            # No crash site this start; the server itself must still come up.
            print(f"[minecraft] wreck pack not installed: {exc}", flush=True)

    def _watch_wreck(self, folder: str, seen: int) -> None:
        """A new world's first load is the pack's one chance at an untouched spawn. If
        BDS generated the world without it (it didn't take the pack from the empty
        folder), restart once, now that the folder is a world, before anyone plays."""
        if not self.bds.wait_running(WRECK_START_S):
            return
        deadline = time.monotonic() + WRECK_WAIT_S
        while time.monotonic() < deadline:
            if any(e["kind"] == "wreck" for e in self.bds.events_after(seen)):
                return
            time.sleep(1)
        # A plain restart under the lifecycle lock (never server_action's, which with
        # auto-update on would run an update): if anything else holds it, that job
        # restarts the server itself, and the pack goes in then.
        if not self._life.acquire(blocking=False):
            return
        try:
            rec = self.slots.by_folder(folder)
            if folder != self.active_folder() or not rec or rec.get("wreck") != "armed":
                return
            if self.bds.players:
                return  # someone's already in: a restart now is worse than no wreck
            print("[wreck] the pack didn't load on the first start; restarting", flush=True)
            self._busy = "restart"
            self.maintenance = True
            self._stop("The server is restarting to finish setting up this world")
            self._start()
        finally:
            self.maintenance = False
            self._busy = ""
            self._phase = ""
            self._life.release()

    def note_wreck(self) -> None:
        """A world whose pack reports the wreck built is marked done, so the pack comes
        out at its next start. This is housekeeping, not the guard against a second
        build: a "done" can go unread (the wrapper restarting first), and the pack's own
        record in the world is what stops it building twice."""
        events = self.bds.events_after(self._wreck_seen)
        if not events:
            return
        self._wreck_seen = events[-1]["id"]
        for e in events:
            if e["kind"] != "wreck":
                continue
            folder = str(e.get("world") or "")
            self._wreck_phase[folder] = (str(e.get("phase") or ""), float(e["at"]))
            if e.get("phase") != "done":
                continue
            rec = self.slots.by_folder(folder)
            # A "done" from before the slot's world was (re)generated is the old
            # world's, not this armed one's.
            if not rec or rec.get("wreck") != "armed":
                continue
            if e["at"] < float(rec.get("created_at") or 0):
                continue
            # A done with a cause is the pack passing the world over, not building it.
            skipped = bool(e.get("cause"))
            rec = self.slots.update(rec["id"], wreck="skipped" if skipped else "done")
            if skipped and folder == self.active_folder() and self.bds.state == "running":
                # Loaded as armed, so the pack was told there's a crash site; there isn't.
                # A console fault here is fine: the next start tells it.
                with contextlib.suppress(bds.ConsoleError):
                    self.bds.command(pack_config(rec))

    def sample_positions(self) -> int:
        """One travel-log pass: ask the console where each online player is. A player
        who moved less than TRACK_MIN_MOVE since their last kept sample is skipped.
        Returns how many samples were kept."""
        if self.bds.state != "running" or self.maintenance:
            return 0
        kept = 0
        for name, p in list(self.bds.players.items()):
            try:
                pos = parse_querytarget(
                    self.bds.command(f'querytarget "{name}"', wait_s=2.0)
                )
            except bds.ConsoleError:
                return kept  # stopping under us; the next pass tries again
            if pos is None:
                continue
            last = self._last_pos.get(name)
            if (
                last is not None
                and last["dim"] == pos["dim"]
                and math.dist(
                    (last["x"], last["y"], last["z"]), (pos["x"], pos["y"], pos["z"])
                )
                < TRACK_MIN_MOVE
            ):
                continue
            with self._track_lock:
                self._track_id += 1
                self._track.append(
                    {
                        "id": self._track_id,
                        "at": time.time(),
                        "name": name,
                        "xuid": p.get("xuid", ""),
                        "world": self.active_folder(),
                        **pos,
                    }
                )
            self._last_pos[name] = pos
            kept += 1
        for gone in set(self._last_pos) - set(self.bds.players):
            # Rejoining starts a fresh trail point even if they logged out in place.
            del self._last_pos[gone]
        return kept

    def track_after(self, after: int) -> list[dict[str, Any]]:
        with self._track_lock:
            return [dict(t) for t in self._track if t["id"] > after]

    def _map_for(self, slot_id: str) -> tuple[str, mapping.WorldIndex]:
        rec = self.slots.get(slot_id)
        if not rec["exists"]:
            raise ValueError(f"{slot_id} has no world yet")
        folder = rec["folder"]
        index = self._map_index.get(folder)
        if index is None:
            index = self._map_index[folder] = mapping.WorldIndex(self.world_dir(folder) / "db")
        before = index._signature
        index.refresh()
        if index._signature != before:
            self._tiles = {k: v for k, v in self._tiles.items() if k[0] != folder}
        if index._signature != before or folder not in self._map_seed:
            # Re-read with the index: a restore or reset can change the world under a folder.
            top = worlds.read_level_dat(self.world_dir(folder))
            seed = top.get("RandomSeed")
            self._map_seed[folder] = str(seed) if isinstance(seed, int) else None
            sx, sz = top.get("SpawnX"), top.get("SpawnZ")
            self._map_spawn[folder] = (
                {"x": sx, "z": sz} if isinstance(sx, int) and isinstance(sz, int) else None
            )
        return folder, index

    def map_info(self, slot_id: str, dim: str) -> dict[str, Any]:
        """What a viewer needs before asking for tiles: the generated extent, and the
        tile geometry."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        with self._map_lock:
            folder, index = self._map_for(slot_id)
            area = index.extent(mapping.DIMENSIONS[dim])
            predicted = self._map_seed.get(folder) is not None and self._predictor.available(
                mapping.DIMENSIONS[dim]
            )
            spawn = self._map_spawn.get(folder) if dim == "overworld" else None
        return {
            "slot": slot_id,
            "dimension": dim,
            "extent": area,
            "tile_blocks": mapping.TILE,
            "max_zoom": mapping.MAX_ZOOM,
            "layers": list(mapping.LAYERS),
            # Ground nobody has been to is drawn from the seed; without it, transparent
            # is ungenerated ground.
            "predicted": predicted,
            # The world spawn from level.dat, for the map's Beds & spawn overlay. No y:
            # Bedrock often stores a placeholder there and settles on the surface itself.
            "spawn": spawn,
        }

    def map_point(self, slot_id: str, dim: str, x: int, z: int) -> dict[str, Any]:
        """What is at one block column: its biome and surface height, and whether that is
        explored ground or the satellite's survey (the seed's prediction)."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if abs(x) > 30_000_000 or abs(z) > 30_000_000:
            raise ValueError("that point is outside the world")
        d = mapping.DIMENSIONS[dim]
        with self._map_lock:
            folder, index = self._map_for(slot_id)
            got, source = index.point(d, x, z), "explored"
            seed = self._map_seed.get(folder)
            if got is None and seed is not None:
                got = self._predictor.grid(seed, d, x, z, 1, 1).get((x, z))
                source = "survey"
        if got is None:
            return {"x": x, "z": z, "dimension": dim, "source": "unknown"}
        height, biome = got
        bedrock_id, name = mapping.BIOME_NAMES.get(biome, (f"biome {biome}", f"Biome {biome}"))
        return {
            "x": x,
            "z": z,
            "dimension": dim,
            "source": source,
            "biome": name,
            "biome_id": f"minecraft:{bedrock_id}",
            # The top block's y; the survey's is an estimate, typically within a few blocks.
            "y": height + mapping.floor_of(d) - 1,
        }

    def map_find(
        self, slot_id: str, dim: str, x: int, z: int, names: list[str], limit: int, radius: int
    ) -> dict[str, Any]:
        """The nearest blocks of the named kinds in explored ground around (x, z): "where's
        the nearest diamond". An ore name brings its deepslate twin along, since a player
        asking for diamonds means both."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if abs(x) > 30_000_000 or abs(z) > 30_000_000:
            raise ValueError("that point is outside the world")
        wanted: set[str] = set()
        for raw in names:
            name = raw.strip().lower()
            if not _BLOCK_NAME.fullmatch(name):
                raise ValueError(f"`{raw}` isn't a block id")
            name = name if name.startswith("minecraft:") else f"minecraft:{name}"
            wanted.add(name)
            if name.endswith("_ore") and "deepslate_" not in name:
                wanted.add(name.replace("minecraft:", "minecraft:deepslate_", 1))
            if name == "minecraft:chest":
                wanted |= _STORAGE_BLOCKS
        if not wanted:
            raise ValueError("name at least one block")
        d = mapping.DIMENSIONS[dim]
        with self._map_lock:
            rec = self.slots.get(slot_id)
            if not rec["exists"]:
                raise ValueError(f"{slot_id} has no world yet")
            folder = rec["folder"]
            index = self._block_index.get(folder)
            if index is None:
                index = self._block_index[folder] = blocks.BlockIndex(self.world_dir(folder) / "db")
            index.refresh()
            known = index.names(d)
            hits, searched = index.find(d, x, z, wanted & known, limit, radius)
        missing = sorted(wanted - known)
        bare = {n.removeprefix("minecraft:").removeprefix("deepslate_") for n in missing}
        suggestions = sorted(
            n for n in known if any(b in n for b in bare)
        )[:12] if missing and not (wanted & known) else []
        return {
            "dimension": dim,
            "x": x,
            "z": z,
            "searched": sorted(wanted & known),
            "not_in_explored_ground": missing,
            "suggestions": suggestions,
            "explored_chunks_in_range": searched,
            "radius": radius,
            "found": hits,
            **_freshness(self.world_dir(folder) / "db"),
        }

    def map_items(
        self,
        slot_id: str,
        dim: str,
        x: int,
        z: int,
        names: list[str],
        limit: int,
        radius: int | None,
        flush: bool = False,
    ) -> dict[str, Any]:
        """The nearest containers holding the named items, read from the world's
        BlockEntity records: "which chest has my diamonds" (§M23). `radius` None is the
        whole dimension. `flush` first has a running world write itself out (at most
        every FLUSH_EVERY_S), so a chest filled a minute ago is seen."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if abs(x) > 30_000_000 or abs(z) > 30_000_000:
            raise ValueError("that point is outside the world")
        wanted = _wanted_items(names)
        folder = self._world_folder(slot_id)
        flushed = self._flush_for_search(folder) if flush else "not asked"
        d = mapping.DIMENSIONS[dim]
        with self._map_lock:
            index = self._containers(folder, flushed)
            known = index.names(d)
            hits, totals, matched = index.find(d, x, z, wanted, limit, radius)
            stats = {
                "containers_indexed": len(index.containers),
                "records": index.records,
                "unreadable_records": index.unreadable,
                "unopened_loot": index.unopened,
            }
        missing = sorted(wanted - known)
        bare = {n.split(":", 1)[-1] for n in missing}
        suggestions = (
            # Either way round: "diamonds" should suggest diamond, "log" oak_log.
            sorted(
                n for n in known if any(b in n or n.split(":", 1)[-1] in b for b in bare)
            )[:12]
            if missing and not (wanted & known)
            else []
        )
        return {
            "dimension": dim,
            "x": x,
            "z": z,
            "radius": radius,
            "searched": sorted(wanted),
            "in_no_container": missing,
            "suggestions": suggestions,
            "matched": matched,
            "totals": totals,
            "found": hits,
            "flush": flushed,
            **stats,
            # Just written out, the files are the world as it is now, whatever their mtimes.
            **(
                {"as_of": time.time(), "age_s": 0}
                if flushed == "flushed"
                else _freshness(self.world_dir(folder) / "db")
            ),
        }

    def map_stock(
        self,
        slot_id: str,
        dim: str,
        centres: list[tuple[int, int]],
        names: list[str],
        radius: int,
        flush: bool = False,
    ) -> dict[str, Any]:
        """How many of the named items the containers around several points hold, each
        container counted once (§M24): Dave's "how many iron blocks have I got", over the
        chests near each of a player's saved places and near where they stand."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if not 1 <= len(centres) <= STOCK_CENTRES_MAX:
            raise ValueError(f"name one to {STOCK_CENTRES_MAX} points")
        if any(abs(x) > 30_000_000 or abs(z) > 30_000_000 for x, z in centres):
            raise ValueError("that point is outside the world")
        wanted = _wanted_items(names)
        folder = self._world_folder(slot_id)
        flushed = self._flush_for_search(folder) if flush else "not asked"
        with self._map_lock:
            index = self._containers(folder, flushed)
            per, held_in, totals = index.stock(
                mapping.DIMENSIONS[dim], centres, wanted, radius
            )
        return {
            "dimension": dim,
            "radius": radius,
            "searched": sorted(wanted),
            "centres": [
                {"x": x, "z": z, "containers": n, "totals": t}
                for (x, z), n, t in zip(centres, held_in, per, strict=True)
            ],
            "containers": sum(held_in),
            "totals": totals,
            "flush": flushed,
            **(
                {"as_of": time.time(), "age_s": 0}
                if flushed == "flushed"
                else _freshness(self.world_dir(folder) / "db")
            ),
        }

    def _world_folder(self, slot_id: str) -> str:
        rec = self.slots.get(slot_id)
        if not rec["exists"]:
            raise ValueError(f"{slot_id} has no world yet")
        return rec["folder"]

    def _containers(self, folder: str, flushed: str) -> containers.ContainerIndex:
        """The world's container index, re-read at once after a flush. Under _map_lock."""
        return self._world_indexes(folder, flushed)[0]

    def _entities(self, folder: str, flushed: str) -> entities.EntityIndex:
        """The world's entity index (§M4e), as `_containers`. Under _map_lock."""
        return self._world_indexes(folder, flushed)[1]

    def _world_indexes(
        self, folder: str, flushed: str
    ) -> tuple[containers.ContainerIndex, entities.EntityIndex]:
        """Both of a world's record indexes, refreshed by ONE sweep of its database
        when its files changed: decompressing every table is the cost on a big world,
        so the chests and the entities share it."""
        db = self.world_dir(folder) / "db"
        chests = self._container_index.get(folder)
        if chests is None:
            chests = self._container_index[folder] = containers.ContainerIndex(db)
        mobs = self._entity_index.get(folder)
        if mobs is None:
            mobs = self._entity_index[folder] = entities.EntityIndex(db)
        containers.refresh_together([chests, mobs], force=flushed == "flushed")
        return chests, mobs

    def _fresh(self, folder: str, flushed: str) -> dict[str, Any]:
        # Just written out, the files are the world as it is now, whatever their mtimes.
        if flushed == "flushed":
            return {"as_of": time.time(), "age_s": 0}
        return _freshness(self.world_dir(folder) / "db")

    def remember_player_id(self, world: str, name: str, pid: str) -> None:
        """Keep a world's gamertag -> `player.id`, as the pack reported it on a `/dave`.
        Called on the console thread, so it only writes a small file."""
        with self._ids_lock:
            known = self._player_ids()
            per = known.setdefault(world, {})
            if per.get(name.casefold(), {}).get("id") == pid:
                return
            per[name.casefold()] = {"name": name, "id": pid}
            try:
                PLAYER_IDS.write_text(json.dumps(known, indent=2, sort_keys=True))
            except OSError as exc:
                print(f"[entities] player ids not saved: {exc}", flush=True)

    def _player_ids(self) -> dict[str, dict[str, dict[str, str]]]:
        try:
            raw = json.loads(PLAYER_IDS.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def player_ids(self, folder: str) -> dict[str, dict[str, str]]:
        """A world's known players, by casefolded gamertag: {"name", "id"}."""
        with self._ids_lock:
            per = self._player_ids().get(folder)
        return per if isinstance(per, dict) else {}

    def map_villagers(
        self,
        slot_id: str,
        dim: str,
        x: int,
        z: int,
        *,
        profession: str = "",
        sells: str = "",
        enchant: str = "",
        radius: int | None = None,
        limit: int = 5,
        flush: bool = False,
    ) -> dict[str, Any]:
        """The nearest villagers with a job, or selling an item or an enchantment, from
        the world's actor records (§M4e). Only the trades each offers now: a level it
        hasn't reached is never told (owner, 2026-10-13)."""
        _check_point(dim, x, z)
        profession = _word(profession, "profession")
        sells = containers.item_name(_word(sells, "sells")) if sells else ""
        enchant = _word(enchant, "enchant")
        if enchant and enchant not in entities.ENCHANTMENTS.values():
            raise ValueError(f"`{enchant}` isn't an enchantment")
        folder = self._world_folder(slot_id)
        flushed = self._flush_for_search(folder) if flush else "not asked"
        with self._map_lock:
            index = self._entities(folder, flushed)
            hits, matched = index.villagers(
                mapping.DIMENSIONS[dim],
                x,
                z,
                profession=profession,
                sells=sells,
                enchant=enchant,
                radius=radius,
                limit=limit,
            )
            stats = index.counts()
        return {
            "dimension": dim,
            "x": x,
            "z": z,
            "radius": radius,
            "matched": matched,
            "found": hits,
            "flush": flushed,
            **stats,
            **self._fresh(folder, flushed),
        }

    def map_entities(
        self,
        slot_id: str,
        dim: str,
        box: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        """Every villager, pet and named mob the map draws in one dimension, or in `box`
        (§M4e on the map): the villages by 48-block grouping, each villager without its
        trades (`map_villager` has them), and each pet's owner by name where the world
        knows them. Never flushes: the automatic re-read is the only one. `live` says the
        world is the running one, where the files' age undersells how old a mob's
        position is (a loaded chunk's actors are written when it saves, not every tick)."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if box is not None:
            x0, z0, x1, z1 = box
            if x0 > x1 or z0 > z1 or max(abs(v) for v in box) > 30_000_000:
                raise ValueError("box is x0,z0,x1,z1 inside the world, low corner first")
        folder = self._world_folder(slot_id)
        names = {
            v["id"]: v["name"]
            for v in self.player_ids(folder).values()
            if isinstance(v, dict)
        }
        with self._map_lock:
            index = self._entities(folder, "not asked")
            got = entities.listing(index.mobs, mapping.DIMENSIONS[dim], box)
            stats = index.counts()
        for p in got["pets"]:
            p["owner_name"] = names.get(p["owner"] or "", "")
            p.pop("dim", None)
        for m in got["named"]:
            m.pop("dim", None)
        return {
            "dimension": dim,
            **got,
            "live": folder == self.active_folder() and self.bds.running,
            **stats,
            **_freshness(self.world_dir(folder) / "db"),
        }

    def map_villager(self, slot_id: str, key: str) -> dict[str, Any]:
        """One villager by its map id, with every trade it offers now; `found` is None
        when the index no longer has it (it died, or the files moved on)."""
        if not _MOB_KEY.fullmatch(key):
            raise ValueError("id is a villager's id from /map/entities")
        folder = self._world_folder(slot_id)
        with self._map_lock:
            got = entities.villager_by_id(self._entities(folder, "not asked").mobs, key)
        if got is not None:
            got["dimension"] = _DIMENSIONS.get(got.pop("dim"), "overworld")
        return {"found": got, **_freshness(self.world_dir(folder) / "db")}

    def map_pets(
        self,
        slot_id: str,
        dim: str,
        x: int,
        z: int,
        *,
        owner: str = "",
        owner_name: str = "",
        kind: str = "",
        name: str = "",
        radius: int | None = None,
        limit: int = 5,
        everywhere: bool = False,
        flush: bool = False,
    ) -> dict[str, Any]:
        """Tamed animals (one player's when `owner` or `owner_name` is given), or any mob
        named `name` with that player's first (§M4e). `everywhere` adds the other
        dimensions, with no distance."""
        _check_point(dim, x, z)
        kind = _word(kind, "kind")
        kind = entities.KIND_ALIASES.get(kind, kind)
        if len(name) > 64:
            raise ValueError("name is at most 64 characters")
        folder = self._world_folder(slot_id)
        ids = self.player_ids(folder)
        owner_known = True
        if owner_name:
            got = ids.get(owner_name.strip().casefold())
            owner_known = got is not None
            owner = got["id"] if got else ""
        if owner and not _PLAYER_ID.fullmatch(owner):
            raise ValueError("owner is a player id, a whole number")
        names = {v["id"]: v["name"] for v in ids.values() if isinstance(v, dict)}
        flushed = self._flush_for_search(folder) if flush else "not asked"
        hits: list[dict[str, Any]] = []
        matched = 0
        with self._map_lock:
            index = self._entities(folder, flushed)
            # An unknown owner is no filter for a name, which finds any mob called it.
            if owner_known or name:
                hits, matched = index.pets(
                    mapping.DIMENSIONS[dim],
                    x,
                    z,
                    owner=int(owner) if owner else None,
                    kind=kind,
                    name=name,
                    radius=radius,
                    limit=limit,
                    everywhere=everywhere,
                )
            stats = index.counts()
        for h in hits:
            h["owner_name"] = names.get(h["owner"] or "", "")
            h["dimension"] = _DIMENSIONS.get(h.pop("dim"), "overworld")
        return {
            "dimension": dim,
            "x": x,
            "z": z,
            "radius": radius,
            "owner": owner or None,
            "owner_known": owner_known,
            "matched": matched,
            "found": hits,
            "flush": flushed,
            **stats,
            **self._fresh(folder, flushed),
        }

    def map_portals(self, slot_id: str, dim: str | None, check: bool = True) -> dict[str, Any]:
        """The Nether portals the world recorded (§M4e), in one dimension or all. With
        `check`, a record whose portal block is gone (it was broken) is left out."""
        if dim is not None and dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        folder = self._world_folder(slot_id)
        db = self.world_dir(folder) / "db"
        found = [
            p
            for p in entities.portals(db)
            if p["dim"] in _DIMENSIONS and (dim is None or _DIMENSIONS[p["dim"]] == dim)
        ][:PORTALS_MAX]
        broken = 0
        if check and found:
            with self._map_lock:
                index = self._block_index.get(folder)
                if index is None:
                    index = self._block_index[folder] = blocks.BlockIndex(db)
                index.refresh()
                kept = []
                for p in found:
                    stands, _ = index.find(
                        p["dim"], p["x"], p["z"], {"minecraft:portal"}, 1, PORTAL_BLOCK_REACH
                    )
                    if stands:
                        kept.append(p)
                    else:
                        broken += 1
                found = kept
        for p in found:
            p["dimension"] = _DIMENSIONS[p.pop("dim")]
        return {
            "portals": found,
            "checked": check,
            "broken": broken,
            **_freshness(db),
        }

    def _flush_for_search(self, folder: str) -> str:
        """Ask the running world to write itself out, so the files hold what's in the
        chests now: "flushed", or why it wasn't (another world, a stopped server, one done
        moments ago, or no confirmation in time — then the files are read as they are)."""
        if folder != self.active_folder() or not self.bds.running:
            return "not running"
        now = time.monotonic()
        if self._flushed_at is not None and now - self._flushed_at < FLUSH_EVERY_S:
            return "recent"
        self._flushed_at = now
        try:
            self.bds.flush(FLUSH_WAIT_S)
        except bds.ConsoleError as exc:
            return f"failed: {exc}"
        return "flushed"

    def map_nearby(self, dim: str, x: int, z: int) -> dict[str, Any]:
        """The nearest structure of each kind the dimension has, from (x, z), by the
        running server's own `locate` — exact, and it generates nothing."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if abs(x) > 30_000_000 or abs(z) > 30_000_000:
            raise ValueError("that point is outside the world")
        if not self.bds.running:
            raise RuntimeError("the server isn't running, so it can't search its world")
        found = []
        for kind in mapping.STRUCTURES[mapping.DIMENSIONS[dim]]:
            got = mapping.parse_located(
                self.bds.command(
                    f"execute in {dim} positioned {x} 64 {z} run locate structure {kind}",
                    wait_s=2.0,
                    quiet_s=0.15,
                )
            )
            if got is not None:
                _, sx, sz = got
                distance = round(math.dist((x, z), (sx, sz)))
                found.append({"structure": kind, "x": sx, "z": sz, "distance": distance})
        found.sort(key=lambda f: f["distance"])
        return {"world": self.active_folder(), "dimension": dim, "x": x, "z": z, "nearby": found}

    def _locate(self, dim: str, x: int, z: int, kind: str) -> tuple[int, int] | None:
        got = mapping.parse_located(
            self.bds.command(
                f"execute in {dim} positioned {x} 64 {z} run locate structure {kind}",
                wait_s=2.0,
                quiet_s=0.15,
            )
        )
        return None if got is None else (got[1], got[2])

    def _structure_inputs(
        self, slot_id: str
    ) -> tuple[str, str | None, dict[str, Any], dict[str, tuple[str, ...]]]:
        """A world's folder, seed and explored extents, and the kinds `locate` knows."""
        with self._map_lock:
            folder, index = self._map_for(slot_id)
            seed = self._map_seed.get(folder)
            extents = {
                name: index.extent(dim) for name, dim in mapping.DIMENSIONS.items()
            }
        kinds = {
            name: mapping.STRUCTURES[dim] for name, dim in mapping.DIMENSIONS.items()
        }
        return folder, seed, extents, kinds

    def sweep_view(self, folder: str | None = None) -> dict[str, Any]:
        view = dict(self._sweep)
        if folder is not None and view.get("folder") not in (None, folder):
            return {"state": "idle"}  # another world's sweep says nothing about this one
        return view

    def map_structures(self, slot_id: str, dim: str) -> dict[str, Any]:
        """The saved sweep's structures in one dimension. Starts a fresh sweep, in the
        background, when the saved one is stale and this world is up to be asked."""
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        folder, seed, extents, kinds = self._structure_inputs(slot_id)
        saved = structure_sweep.load(STRUCTURE_DIR, folder)
        reason = structure_sweep.stale_reason(saved, seed, kinds, extents)
        last = self.sweep_view(folder)
        retry = (
            last.get("state") != "failed"
            or time.time() - float(last.get("failed_at") or 0) > SWEEP_RETRY_S
        )
        if reason and retry and folder == self.active_folder() and self.bds.running:
            with contextlib.suppress(RuntimeError):
                self.sweep_structures(slot_id)
        usable = saved is not None and saved.get("seed") == seed
        return {
            "slot": slot_id,
            "dimension": dim,
            "structures": saved["structures"].get(dim, []) if saved and usable else [],
            "swept_at": saved.get("swept_at") if saved and usable else None,
            "stale": reason,
            "sweep": self.sweep_view(folder),
        }

    def sweep_structures(self, slot_id: str) -> dict[str, Any]:
        """Start a sweep of the loaded world in the background, or report the one already
        running. Only the loaded world can be asked: `locate` searches the running one."""
        rec = self.slots.get(slot_id)
        if not rec["exists"]:
            raise ValueError(f"{slot_id} has no world yet")
        folder = rec["folder"]
        if folder != self.active_folder():
            raise RuntimeError("only the loaded world can be searched — load it first")
        if not self.bds.running:
            raise RuntimeError("the server isn't running, so it can't search its world")
        if not self._sweep_lock.acquire(blocking=False):
            return self.sweep_view()
        try:
            folder, seed, extents, kinds = self._structure_inputs(slot_id)
            top = worlds.read_level_dat(self.world_dir(folder))
            spawns = {
                "overworld": (int(top.get("SpawnX", 0) or 0), int(top.get("SpawnZ", 0) or 0)),
                "nether": (0, 0),
                "the_end": (0, 0),
            }
            plan = {
                dim: structure_sweep.points(structure_sweep.area(extents[dim], spawns[dim]), spawns[dim])
                for dim in kinds
            }
            self._sweep = {
                "state": "running",
                "folder": folder,
                "done": 0,
                "total": structure_sweep.total(plan, kinds),
                "started_at": time.time(),
            }
        except BaseException:
            self._sweep_lock.release()
            raise

        def progress(done: int) -> None:
            self._sweep["done"] = done

        def run() -> None:
            try:
                found = structure_sweep.sweep(
                    self._locate,
                    plan,
                    kinds,
                    progress=progress,
                    keep_going=lambda: self.active_folder() == folder and self.bds.running,
                )
                structure_sweep.save(
                    STRUCTURE_DIR,
                    folder,
                    seed=seed,
                    kinds=kinds,
                    explored=extents,
                    found=found,
                    at=time.time(),
                )
                self._sweep = {
                    "state": "done",
                    "folder": folder,
                    "found": sum(len(v) for v in found.values()),
                    "finished_at": time.time(),
                }
            except Exception as exc:  # noqa: BLE001 — a failed sweep keeps the last one
                self._sweep = {
                    "state": "failed",
                    "folder": folder,
                    "error": str(exc),
                    "failed_at": time.time(),
                }
            finally:
                self._sweep_lock.release()

        threading.Thread(target=run, name="structure-sweep", daemon=True).start()
        return self.sweep_view()

    def map_tile(
        self, slot_id: str, dim: str, zoom: int, tx: int, tz: int, layer: str = "surface"
    ) -> bytes:
        if dim not in mapping.DIMENSIONS:
            raise ValueError("dimension must be overworld, nether or the_end")
        if layer not in mapping.LAYERS:
            raise ValueError("layer must be surface or biomes")
        if not 0 <= zoom <= mapping.MAX_ZOOM:
            raise ValueError(f"zoom is 0 to {mapping.MAX_ZOOM}")
        if abs(tx) > 200_000 or abs(tz) > 200_000:
            raise ValueError("that tile is outside the world")
        with self._map_lock:
            folder, index = self._map_for(slot_id)
            key = (folder, dim, zoom, tx, tz, layer)
            cached = self._tiles.get(key)
            signature, seed = index._signature, self._map_seed.get(folder)
            if cached is None and not mapping.true_colour_of(layer, mapping.DIMENSIONS[dim]):
                image = mapping.tile(
                    index, mapping.DIMENSIONS[dim], zoom, tx, tz, self._predictor, seed, layer
                )
                cached = self._keep_tile(key, image)
        if cached is not None:
            return cached
        # A true-colour tile reads block sections and can take a second once the world
        # moves, so it's drawn outside the map lock: the Biomes tiles the PWA shows under
        # it meanwhile are never queued behind it. One true-colour draw at a time.
        with self._surface_lock:
            image = mapping.tile(
                index, mapping.DIMENSIONS[dim], zoom, tx, tz, self._predictor, seed, layer
            )
        with self._map_lock:
            if index._signature == signature:  # not drawn from a world since re-read
                self._keep_tile(key, image)
        return image

    def _keep_tile(self, key: tuple, image: bytes) -> bytes:
        if len(self._tiles) > 512:  # a few MB of PNGs at most
            self._tiles.clear()
        self._tiles[key] = image
        return image

    def probe_pack(self, install_it: bool) -> dict[str, Any]:
        """Install (or remove) the bundled probe behavior pack in the active world and
        restart the server so it loads. M0 item 5: does a stable-API pack load with no
        experiments, and does its `console.log` reach this console?"""
        pack_id = self.install_pack(PROBE_PACK_SRC, PROBE_PACK_DIR, install_it)
        if not self._life.acquire(blocking=False):
            raise RuntimeError(f"busy: {self._busy or 'another action'}")
        self.maintenance = True
        try:
            self._stop("Installing a test pack")
            self._start()
        finally:
            self.maintenance = False
            self._phase = ""
            self._life.release()
        return {"installed": install_it, "pack_id": pack_id, "restarted": True}

    def shutdown(self) -> None:
        self.stopping = True
        self.bds.stop()


def effective_rules(rec: dict[str, Any] | None) -> dict[str, bool | int | str]:
    """The rules a world runs with: the owner's saved choices, with what a box feature
    holds on top — the Power Pack charm keeps keepInventory on (§M10's fail-safe
    direction: if the pack ever fails, everyone keeps their things)."""
    rules = dict((rec or {}).get("rules") or {})
    rules.update(managed_rules(rec))
    return rules


def managed_rules(rec: dict[str, Any] | None) -> dict[str, bool | int | str]:
    return {"keepInventory": True} if (rec or {}).get("power_pack_keep") else {}


def pack_config(rec: dict[str, Any] | None) -> str:
    """The console line that tells the jbrain pack the world's settings. JSON-escaped
    by json.dumps, one line, as every scriptevent the wrapper sends. `wreck` picks
    Dave's welcome lines (§DW1): an armed world is building its crash site on this very
    load, so it counts; a skipped one never got it."""
    cfg = {
        "power_pack_keep": bool((rec or {}).get("power_pack_keep")),
        "power_pack_transport": (rec or {}).get("power_pack_transport") is not False,
        "wreck": (rec or {}).get("wreck") in ("armed", "done"),
    }
    return f"scriptevent jb:config {json.dumps(cfg)}"


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


def public_ip(fetch: Callable[[], bytes] | None = None) -> str | None:
    """The address the internet sees this box at, or None when it can't be told. Only a
    public IPv4 counts: a private one would mean the answer came from inside the home."""

    def ask() -> bytes:
        req = urllib.request.Request(PUBLIC_IP_URL, headers={"User-Agent": "jbrain-minecraft"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read(4096)

    try:
        body = (fetch or ask)().decode("ascii", "replace")
    except OSError:
        return None
    for line in body.splitlines():
        if line.startswith("ip="):
            try:
                ip = ipaddress.ip_address(line[3:].strip())
            except ValueError:
                return None
            return str(ip) if ip.version == 4 and ip.is_global else None
    return None


def watch_public_ip(rig: Rig, lookup: Callable[[], str | None] = public_ip) -> None:
    while not rig.stopping:
        rig.public_ip = lookup() or rig.public_ip
        time.sleep(PUBLIC_IP_S)


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


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
            slot = query.get("slot", [""])[0]
            folder = rig.slots.get(slot)["folder"] if slot else None
            self._send(200, {"snapshots": rig.index.listing(folder)})
        elif m := _SNAP_FILE.match(url.path):
            self._send_file(rig, urllib.parse.unquote(m.group(1)))
        elif url.path == "/worlds":
            self._send(200, rig.worlds_view())
        elif url.path == "/worlds/new-seed":
            self._send(200, {"seed": worlds.new_seed()})
        elif m := _WORLD_RULES.match(url.path):
            self._send(200, rig.rules_view(m.group(1)))
        elif url.path == "/allowlist":
            self._send(200, rig.allowlist())
        elif url.path == "/map/info":
            try:
                self._send(
                    200,
                    rig.map_info(
                        query.get("slot", ["slot1"])[0], query.get("dim", ["overworld"])[0]
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/find":
            try:
                x, z = int(query.get("x", [""])[0]), int(query.get("z", [""])[0])
                limit = max(1, min(int(query.get("limit", ["5"])[0]), 20))
                radius = max(16, min(int(query.get("radius", ["2048"])[0]), 16384))
            except ValueError:
                self._send(400, {"detail": "x, z, limit and radius are whole numbers"})
                return
            try:
                self._send(
                    200,
                    rig.map_find(
                        query.get("slot", ["slot1"])[0],
                        query.get("dim", ["overworld"])[0],
                        x,
                        z,
                        query.get("blocks", [""])[0].split(","),
                        limit,
                        radius,
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/items":
            try:
                x, z = int(query.get("x", [""])[0]), int(query.get("z", [""])[0])
                limit = max(1, min(int(query.get("limit", ["5"])[0]), 20))
                raw_radius = query.get("radius", [""])[0]
                radius = max(1, min(int(raw_radius), 60_000_000)) if raw_radius else None
            except ValueError:
                self._send(400, {"detail": "x, z, limit and radius are whole numbers"})
                return
            try:
                self._send(
                    200,
                    rig.map_items(
                        query.get("slot", ["slot1"])[0],
                        query.get("dim", ["overworld"])[0],
                        x,
                        z,
                        query.get("items", [""])[0].split(","),
                        limit,
                        radius,
                        flush=query.get("flush", [""])[0] in ("1", "true"),
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/stock":
            try:
                centres = [
                    (int(x), int(z))
                    for x, z in (
                        pair.split(",", 1)
                        for pair in query.get("centres", [""])[0].split(";")
                        if pair
                    )
                ]
                radius = int(query.get("radius", ["48"])[0])
            except ValueError:
                self._send(400, {"detail": "centres are x,z;x,z and radius a whole number"})
                return
            try:
                self._send(
                    200,
                    rig.map_stock(
                        query.get("slot", ["slot1"])[0],
                        query.get("dim", ["overworld"])[0],
                        centres,
                        query.get("items", [""])[0].split(","),
                        max(1, min(radius, STOCK_RADIUS_MAX)),
                        flush=query.get("flush", [""])[0] in ("1", "true"),
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/entities":
            q = {k: v[0] for k, v in query.items()}
            corners = [q.get(k, "") for k in ("x0", "z0", "x1", "z1")]
            box: tuple[int, int, int, int] | None = None
            try:
                if any(corners):
                    x0, z0, x1, z1 = (int(v) for v in corners)
                    box = (x0, z0, x1, z1)
            except ValueError:
                self._send(400, {"detail": "a box is x0, z0, x1 and z1, whole numbers"})
                return
            try:
                self._send(
                    200,
                    rig.map_entities(
                        q.get("slot", "slot1"), q.get("dim", "overworld"), box
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/villagers" and query.get("id"):
            try:
                self._send(
                    200,
                    rig.map_villager(
                        query.get("slot", ["slot1"])[0], query.get("id", [""])[0]
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path in ("/map/villagers", "/map/pets"):
            q = {k: v[0] for k, v in query.items()}
            try:
                x, z = int(q.get("x", "")), int(q.get("z", ""))
                limit = max(1, min(int(q.get("limit") or 5), 20))
                radius = max(1, min(int(q["radius"]), 60_000_000)) if q.get("radius") else None
            except ValueError:
                self._send(400, {"detail": "x, z, limit and radius are whole numbers"})
                return
            flush = q.get("flush") in ("1", "true")
            slot, dim = q.get("slot", "slot1"), q.get("dim", "overworld")
            try:
                if url.path == "/map/villagers":
                    got = rig.map_villagers(
                        slot,
                        dim,
                        x,
                        z,
                        profession=q.get("profession", ""),
                        sells=q.get("sells", ""),
                        enchant=q.get("enchant", ""),
                        radius=radius,
                        limit=limit,
                        flush=flush,
                    )
                else:
                    got = rig.map_pets(
                        slot,
                        dim,
                        x,
                        z,
                        owner=q.get("owner", ""),
                        owner_name=q.get("owner_name", ""),
                        kind=q.get("kind", ""),
                        name=q.get("name", ""),
                        everywhere=q.get("everywhere") in ("1", "true"),
                        radius=radius,
                        limit=limit,
                        flush=flush,
                    )
                self._send(200, got)
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/portals":
            dim = query.get("dim", [""])[0] or None
            try:
                self._send(
                    200,
                    rig.map_portals(
                        query.get("slot", ["slot1"])[0],
                        dim,
                        check=query.get("check", ["1"])[0] not in ("0", "false"),
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path == "/map/structures":
            try:
                self._send(
                    200,
                    rig.map_structures(
                        query.get("slot", ["slot1"])[0], query.get("dim", ["overworld"])[0]
                    ),
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
        elif url.path in ("/map/point", "/map/nearby"):
            dim = query.get("dim", ["overworld"])[0]
            try:
                x, z = int(query.get("x", [""])[0]), int(query.get("z", [""])[0])
            except ValueError:
                self._send(400, {"detail": "x and z are whole numbers"})
                return
            try:
                if url.path == "/map/point":
                    self._send(200, rig.map_point(query.get("slot", ["slot1"])[0], dim, x, z))
                else:
                    self._send(200, rig.map_nearby(dim, x, z))
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
            except (RuntimeError, bds.ConsoleError) as exc:
                self._send(409, {"detail": str(exc)})
        elif m := _MAP_TILE.match(url.path):
            try:
                image = rig.map_tile(
                    query.get("slot", ["slot1"])[0],
                    m.group(1),
                    int(m.group(2)),
                    int(m.group(3)),
                    int(m.group(4)),
                    query.get("layer", ["surface"])[0],
                )
            except ValueError as exc:
                self._send(400, {"detail": str(exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
        elif url.path == "/track":
            after = int(query.get("after", ["0"])[0])
            self._send(200, {"boot_id": bds.BOOT_ID, "samples": rig.track_after(after)})
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

    def _send_file(self, rig: Rig, name: str) -> None:
        """Stream a backup out (the owner's download). Recorded per backup and per world, because a
        download is the only copy of a Minecraft backup that leaves the box."""
        try:
            path = rig.snapshot_file(name)
        except ValueError as exc:
            self._send(404, {"detail": str(exc)})
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                self.wfile.write(chunk)
        rig.index.note_download(name, rig.index.entry(name)["folder"])

    def _receive_upload(self) -> Path:
        """Stream the request body to a file; an upload never sits in memory (the
        container is capped at 2 GB and a world can be hundreds of MB)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("empty upload")
        if n > worlds.MAX_IMPORT_BYTES:
            raise ValueError("that file is too big to be a Bedrock world (over 1 GB)")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        path = UPLOAD_DIR / f"upload-{os.getpid()}-{time.time_ns()}.mcworld"
        left = n
        try:
            with open(path, "wb") as out:
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("upload ended early")
                    out.write(chunk)
                    left -= len(chunk)
        except (OSError, ValueError) as exc:
            # A phone that lost signal mid-upload leaves no half-world on the volume.
            path.unlink(missing_ok=True)
            raise ValueError(f"upload ended early: {exc}") from exc
        return path

    def do_POST(self) -> None:
        rig = _rig()
        if not self._authorized():
            return
        if m := _WORLD_IMPORT.match(self.path.split("?", 1)[0]):
            self._import(rig, m.group(1))
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
                slot = str(body.get("slot") or "")
                folder = rig.slots.get(slot)["folder"] if slot else None
                self._send(
                    200,
                    rig.snapshot(str(body.get("label", "")), folder=folder, auto=False),
                )
            elif m := _WORLD_RULES.match(self.path):
                changes = body.get("set", {})
                if not isinstance(changes, dict):
                    raise ValueError("`set` must be an object")
                self._send(200, rig.set_rules(m.group(1), changes))
            elif m := _WORLD_ACTION.match(self.path):
                slot, action = m.group(1), m.group(2)
                if action in ("load", "reset"):
                    what = "loading a world" if action == "load" else "resetting a world"
                    self._send(
                        *rig.run_job(
                            what, lambda: self._world_action(rig, slot, action, body)
                        )
                    )
                else:
                    self._send(200, self._world_action(rig, slot, action, body))
            elif m := _SNAP_ACTION.match(self.path):
                name = urllib.parse.unquote(m.group(1))
                if m.group(2) == "restore":
                    self._send(
                        *rig.run_job(
                            "restoring a backup",
                            lambda: self._snapshot_action(rig, name, "restore", body),
                        )
                    )
                else:
                    self._send(200, self._snapshot_action(rig, name, m.group(2), body))
            elif self.path == "/allowlist":
                self._send(200, rig.change_allowlist(body))
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
            elif self.path == "/map/structures/sweep":
                self._send(202, rig.sweep_structures(str(body.get("slot") or "")))
            elif m := _WORLD_POWER_PACK.match(self.path):
                self._send(200, rig.set_power_pack(m.group(1), body.get("on")))
            elif m := _WORLD_TRANSPORT.match(self.path):
                self._send(
                    200, rig.set_power_pack_transport(m.group(1), body.get("on"))
                )
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

    @staticmethod
    def _world_action(rig: Rig, slot: str, action: str, body: dict[str, Any]) -> Any:
        if action == "load":
            return rig.load_slot(slot)
        if action == "create":
            return rig.create_slot(slot, body)
        if action == "update":
            return rig.update_slot(slot, body)
        if action == "reset":
            return rig.reset_slot(
                slot,
                str(body.get("mode", "")),
                str(body.get("seed") or ""),
                body.get("wreck"),
                bool(body.get("test")),
            )
        raise ValueError(f"unknown world action: {action}")

    @staticmethod
    def _snapshot_action(rig: Rig, name: str, action: str, body: dict[str, Any]) -> Any:
        rig.snapshot_file(name)  # 400 for a name that isn't a backup
        if action == "pin":
            rig.index.set_pinned(name, bool(body.get("pinned", True)))
            return {"name": name, "pinned": bool(body.get("pinned", True))}
        if action == "delete":
            rig.delete_snapshot(name)
            return {"name": name, "deleted": True}
        if action == "restore":
            return rig.restore_snapshot(name, str(body.get("slot") or ""))
        raise ValueError(f"unknown backup action: {action}")

    def _import(self, rig: Rig, slot: str) -> None:
        """Refuse a busy server before reading a byte (a 409 after hundreds of MB is no
        use), and hold the lock while the body arrives so no other device starts a load
        or reset meanwhile; the import itself then runs as a job."""
        upload: Path | None = None
        try:
            with rig._exclusive("receiving a world upload"):
                upload = self._receive_upload()
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get(
                "name", [""]
            )[0]
            received = upload

            def work() -> dict[str, Any]:
                try:
                    return rig.import_slot(slot, received, name)
                finally:
                    received.unlink(missing_ok=True)

            upload = None  # the job owns the file now
            self._send(*rig.run_job("importing a world", work))
        except RuntimeError as exc:
            self._send(409, {"detail": str(exc)})
        except ValueError as exc:
            self._send(400, {"detail": str(exc)})
        finally:
            if upload is not None:
                upload.unlink(missing_ok=True)


_SNAP_FILE = re.compile(r"^/snapshots/([^/]+\.mcworld)/file$")
_SNAP_ACTION = re.compile(r"^/snapshots/([^/]+\.mcworld)/(pin|delete|restore)$")
_WORLD_ACTION = re.compile(r"^/worlds/(slot\d+)/(load|create|update|reset)$")
_WORLD_RULES = re.compile(r"^/worlds/(slot\d+)/rules$")
_WORLD_POWER_PACK = re.compile(r"^/worlds/(slot\d+)/power-pack$")
_WORLD_TRANSPORT = re.compile(r"^/worlds/(slot\d+)/power-pack-transport$")
_WORLD_IMPORT = re.compile(r"^/worlds/(slot\d+)/import$")
_BLOCK_NAME = re.compile(r"^(minecraft:)?[a-z0-9_]{2,60}$")
# An item id, its namespace optional (the box's own items are `jbrain:`).
_ITEM_NAME = re.compile(r"^([a-z0-9_]{1,32}:)?[a-z0-9_]{2,64}$")


_PLAYER_ID = re.compile(r"^-?\d{1,20}$")
# An entity's map id: its UniqueID, or where it stood when it had none.
_MOB_KEY = re.compile(r"^(-?\d{1,20}|at:-?\d{1,2}:-?\d{1,9}:-?\d{1,4}:-?\d{1,9})$")
_WORD = re.compile(r"^[a-z0-9_:]{0,64}$")


def _word(raw: str, what: str) -> str:
    """A lowercase id-like word (spaces as underscores), or ValueError naming `what`."""
    word = raw.strip().lower().replace(" ", "_")
    if not _WORD.fullmatch(word):
        raise ValueError(f"`{raw}` isn't a {what}")
    return word.removeprefix("minecraft:")


def _check_point(dim: str, x: int, z: int) -> None:
    if dim not in mapping.DIMENSIONS:
        raise ValueError("dimension must be overworld, nether or the_end")
    if abs(x) > 30_000_000 or abs(z) > 30_000_000:
        raise ValueError("that point is outside the world")


def _wanted_items(names: list[str]) -> set[str]:
    """1-8 item ids, namespaced; ValueError naming the first that isn't one."""
    wanted: set[str] = set()
    for raw in names:
        name = raw.strip().lower()
        if not _ITEM_NAME.fullmatch(name):
            raise ValueError(f"`{raw}` isn't an item id")
        wanted.add(containers.item_name(name))
    if not wanted or len(wanted) > 8:
        raise ValueError("name one to eight items")
    return wanted
# "Find me a chest" means anywhere things are kept: both chests, barrels, and a shulker
# box of any colour (and the single pre-1.20 id an old world may still hold).
_SHULKER_COLOURS = (
    "white orange magenta light_blue yellow lime pink gray light_gray cyan purple blue"
    " brown green red black undyed"
).split()
_STORAGE_BLOCKS = frozenset(
    {"minecraft:trapped_chest", "minecraft:barrel", "minecraft:shulker_box"}
    | {f"minecraft:{c}_shulker_box" for c in _SHULKER_COLOURS}
)


def _freshness(db: Path) -> dict[str, Any]:
    """How old what's read from a world's files is: the newest write to any of them, as
    a time and an age, both None when there are none."""
    try:
        newest = max((p.stat().st_mtime for p in db.iterdir()), default=None)
    except FileNotFoundError:
        newest = None
    if newest is None:
        return {"as_of": None, "age_s": None}
    return {"as_of": newest, "age_s": max(round(time.time() - newest), 0)}
_MAP_TILE = re.compile(r"^/map/tile/(overworld|nether|the_end)/(\d)/(-?\d+)/(-?\d+)\.png$")


def parse_querytarget(lines: list[str]) -> dict[str, Any] | None:
    """`querytarget` answers with a JSON list that BDS 1.26 pretty-prints over many
    console lines, the first tagged: `[… INFO] Target data: [` … `]`. Only the first
    line carries the log prefix, so the reply is re-joined and decoded from there."""
    text = "\n".join(lines)
    at = text.find("Target data:")
    body = text[at + len("Target data:") :] if at >= 0 else text[max(text.find("[{"), 0) :]
    try:
        data, _ = json.JSONDecoder().raw_decode(body.lstrip())
    except ValueError:
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    first = data[0]
    pos = first.get("position") or {}
    try:
        out: dict[str, Any] = {k: round(float(pos[k]), 1) for k in ("x", "y", "z")}
    except (KeyError, TypeError, ValueError):
        return None
    dim = first.get("dimension")
    out["dim"] = _DIMENSIONS.get(dim, str(dim)) if isinstance(dim, int) else "?"
    if isinstance(first.get("yRot"), (int, float)):
        out["yaw"] = round(float(first["yRot"]), 1)
    return out


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

    def _tracker() -> None:
        while not rig.stopping:
            try:
                rig.sample_positions()
            except Exception as exc:  # noqa: BLE001 — the log must never stop the server
                print(f"[track] {type(exc).__name__}: {exc}", flush=True)
            try:
                rig.note_wreck()
            except Exception as exc:  # noqa: BLE001 — nor may the wreck's bookkeeping
                print(f"[wreck] {type(exc).__name__}: {exc}", flush=True)
            time.sleep(TRACK_S)

    threading.Thread(target=_tracker, daemon=True).start()
    threading.Thread(target=watch_public_ip, args=(rig,), daemon=True).start()
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
