"""The `deploy/minecraft` wrapper, loaded by path like the sdr and endpoint sidecars.

A fake BDS (a Python script speaking the same console lines) stands in for Mojang's
binary, so what is pinned is the wrapper's half of the contract: replies are collected
per command, players are tracked from the log, the console refuses what has a safer
route, a snapshot is a consistent `.mcworld` cut to the lengths `save query` reported
and always resumes saving, and an update never overwrites the world or the allowlist.
"""

from __future__ import annotations

import importlib.util
import io
import json
import struct
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str, filename: str):
    mc_dir = str(DEPLOY / "minecraft")
    if mc_dir not in sys.path:
        sys.path.insert(0, mc_dir)
    spec = importlib.util.spec_from_file_location(
        name, DEPLOY / f"minecraft/{filename}"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install = _load("install", "install.py")
worlds = _load("worlds", "worlds.py")
bds = _load("bds", "bds.py")
server = _load("mc_server", "server.py")


JBRAIN_PACK_ID = json.loads(
    (DEPLOY / "minecraft/jbrain-pack/manifest.json").read_text()
)["header"]["uuid"]


@pytest.fixture(autouse=True)
def _data_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every wrapper path under a temp dir, for every test. A test that forgot one
    would otherwise write to /data — fine as root locally, a PermissionError on CI."""
    data = tmp_path / "data"
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "SERVER_DIR", data / "server")
    monkeypatch.setattr(server, "SNAPSHOT_DIR", data / "snapshots")
    monkeypatch.setattr(server, "OVERRIDES", data / "properties.json")
    monkeypatch.setattr(server, "SETTINGS", data / "settings.json")
    monkeypatch.setattr(server, "SLOTS", data / "slots.json")
    monkeypatch.setattr(server, "SNAPSHOT_INDEX", data / "snapshot-index.json")
    monkeypatch.setattr(server, "UPLOAD_DIR", data / "uploads")


FAKE_BDS = textwrap.dedent(
    """
    import sys
    print("[2026-10-10 12:00:00:000 INFO] Version: 1.26.52.3", flush=True)
    print("[2026-10-10 12:00:00:001 INFO] Server started.", flush=True)
    queries = 0
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "list":
            print("There are 0/10 players online:", flush=True)
        elif cmd.startswith("join "):
            name = cmd[5:]
            print(f"[x INFO] Player connected: {name}, xuid: 2535", flush=True)
        elif cmd.startswith("leave "):
            name = cmd[6:]
            print(f"[x INFO] Player disconnected: {name}, xuid: 2535", flush=True)
        elif cmd == "save hold":
            print("[x INFO] Saving...", flush=True)
        elif cmd == "save query":
            queries += 1
            if queries == 1:
                print("[x INFO] A previous save has not been completed.", flush=True)
            else:
                ready = "Data saved. Files are now ready to be copied."
                print(f"[x INFO] {ready}", flush=True)
                print("world/db/000005.ldb:4, world/level.dat:3", flush=True)
        elif cmd == "save resume":
            # The marker first: the test waits for the log line, then reads the file.
            with open("resumed", "w") as fh:
                fh.write("yes")
            print("[x INFO] Changes to the level are resumed.", flush=True)
        elif cmd == "stop":
            print("[x INFO] Quit correctly", flush=True)
            sys.exit(0)
    """
)


@pytest.fixture
def running(tmp_path: Path):
    (tmp_path / "fake_bds.py").write_text(FAKE_BDS)

    def spawn(cwd: Path) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "fake_bds.py"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    b = bds.Bds(tmp_path, spawn=spawn)
    b.start()
    b._wait_for(0, lambda t: "Server started." in t, 10)
    yield b
    b.stop(wait_s=5)


def test_the_server_reads_as_running_with_its_version(running) -> None:
    assert running.state == "running"
    assert running.version == "1.26.52.3"


def test_a_command_returns_the_lines_printed_in_reply(running) -> None:
    assert running.command("list", wait_s=3) == ["There are 0/10 players online:"]


def test_players_are_tracked_from_the_console(running) -> None:
    running.command("join Steve", wait_s=2)
    assert running.players["Steve"]["xuid"] == "2535"
    assert running.players["Steve"]["joined_at"] > 0
    running.command("leave Steve", wait_s=2)
    assert running.players == {}


@pytest.mark.parametrize("command", ["stop", "save hold", "/save resume", "list\nstop"])
def test_the_console_refuses_what_has_a_route_of_its_own(running, command: str) -> None:
    with pytest.raises(bds.ConsoleError):
        running.command(command)


def test_a_snapshot_is_cut_to_the_queried_lengths_and_resumes(
    running, tmp_path: Path
) -> None:
    world = tmp_path / "worlds" / "world"
    (world / "db").mkdir(parents=True)
    (world / "db" / "000005.ldb").write_bytes(b"abcdEXTRA")  # LevelDB kept writing
    (world / "level.dat").write_bytes(b"xyzMORE")

    data, files = running.snapshot(world, wait_s=20)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.read("db/000005.ldb") == b"abcd"
        assert zf.read("level.dat") == b"xyz"
    assert files == ["world/db/000005.ldb:4", "world/level.dat:3"]
    running._wait_for(0, lambda t: "resumed" in t, 5)
    assert (tmp_path / "resumed").read_text() == "yes"


def test_a_snapshot_path_outside_the_world_is_refused(tmp_path: Path) -> None:
    world = tmp_path / "w"
    world.mkdir()
    (tmp_path / "secret").write_bytes(b"no")
    with pytest.raises(bds.ConsoleError):
        bds._mcworld(world, [("w/../secret", 2)])


def test_stop_is_graceful(running) -> None:
    running.stop(wait_s=5)
    assert not running.running
    assert running.exit_code == 0


def test_the_file_list_parses_names_with_spaces() -> None:
    got = bds.parse_file_list("Bedrock level/db/1.ldb:10, Bedrock level/level.dat:5")
    assert got == [("Bedrock level/db/1.ldb", 10), ("Bedrock level/level.dat", 5)]


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


LINKS = {
    "result": {
        "links": [
            {"downloadType": "serverBedrockWindows", "downloadUrl": "https://x/w.zip"},
            {
                "downloadType": "serverBedrockLinux",
                "downloadUrl": "https://x/bin-linux/bedrock-server-1.26.52.3.zip",
            },
        ]
    }
}


def _fetcher(zip_bytes: bytes):
    def fetch(url: str, _timeout: float) -> bytes:
        return json.dumps(LINKS).encode() if url == install.LINKS_URL else zip_bytes

    return fetch


def test_latest_resolves_from_the_download_index() -> None:
    version, url = install.resolve("latest", _fetcher(b""))
    assert version == "1.26.52.3"
    assert url.endswith("bedrock-server-1.26.52.3.zip")


def test_a_pinned_version_needs_no_lookup() -> None:
    version, url = install.resolve("1.26.40.1", fetch=None)
    assert version == "1.26.40.1"
    assert url.endswith("bedrock-server-1.26.40.1.zip")


def test_an_update_never_overwrites_the_world_or_the_allowlist(tmp_path: Path) -> None:
    stock = {
        "bedrock_server": b"v1",
        "server.properties": b"level-name=world\n",
        "allowlist.json": b"[]",
        "worlds/world/level.dat": b"stock",
    }
    install.ensure(
        tmp_path, "1.26.40.1", fetch=lambda *_: _zip(stock), log=lambda _: None
    )
    (tmp_path / "allowlist.json").write_text('[{"name": "Steve"}]')
    (tmp_path / "worlds/world/level.dat").write_bytes(b"played")

    newer = {**stock, "bedrock_server": b"v2"}
    got = install.ensure(
        tmp_path, "latest", fetch=_fetcher(_zip(newer)), log=lambda _: None
    )

    assert got == "1.26.52.3"
    assert (tmp_path / "bedrock_server").read_bytes() == b"v2"
    assert "Steve" in (tmp_path / "allowlist.json").read_text()
    assert (tmp_path / "worlds/world/level.dat").read_bytes() == b"played"


def test_a_failed_lookup_keeps_the_installed_server(tmp_path: Path) -> None:
    (tmp_path / install.VERSION_FILE).write_text("1.26.40.1\n")

    def offline(*_a: object) -> bytes:
        raise OSError("no network")

    assert install.ensure(tmp_path, "latest", fetch=offline, log=lambda _: None) == (
        "1.26.40.1"
    )


def test_a_zip_member_escaping_the_server_dir_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        install.extract(_zip({"../evil": b"x"}), tmp_path / "s", first_install=True)


def test_properties_are_set_in_place_and_comments_survive(tmp_path: Path) -> None:
    props = tmp_path / "server.properties"
    props.write_text(
        "# doc\nlevel-name=Bedrock level\n# server-ip=\ntransport=nethernet\n"
    )
    install.apply_properties(
        props, {"level-name": "world", "transport": "raknet", "x": "1"}
    )
    assert props.read_text() == (
        "# doc\nlevel-name=world\n# server-ip=\ntransport=raknet\nx=1\n"
    )


def test_the_box_pins_nethernet_and_console_script_output() -> None:
    # BDS 1.26 refuses players on RakNet ("NetherNet is the only supported transport"),
    # measured on the box in M0b.
    got = install.env_overrides({})
    assert got["transport"] == "nethernet"
    assert got["content-log-console-output-enabled"] == "true"
    assert got["allow-list"] == "false"
    assert "level-seed" not in got
    assert install.env_overrides({"MC_LEVEL_SEED": "42"})["level-seed"] == "42"


def test_property_overrides_refuse_the_pinned_ports_and_line_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    rig = server.Rig(env={})
    with pytest.raises(ValueError):
        rig.set_overrides({"server-port": "25565"})
    with pytest.raises(ValueError):
        rig.set_overrides({"motd": "a\nb"})
    rig.set_overrides({"transport": "raknet"})
    assert rig.properties()["transport"] == "raknet"
    rig.set_overrides({"transport": None})
    assert rig.properties()["transport"] == "nethernet"


def _serve(monkeypatch: pytest.MonkeyPatch, token: str):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    monkeypatch.setattr(server, "TOKEN", token)
    monkeypatch.setattr(server, "RIG", server.Rig(env={}))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path: str, auth: str | None = None) -> int:
        req = urllib.request.Request(base + path)
        if auth is not None:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    return httpd, get


def test_the_control_port_needs_the_bearer_but_health_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Host-networked, so this port is on the LAN: anything but /healthz is the api's.
    httpd, get = _serve(monkeypatch, "s3cret")
    try:
        assert get("/healthz") == 200
        assert get("/properties") == 401
        assert get("/properties", "Bearer wrong") == 401
        assert get("/properties", "Bearer s3cret") == 200
    finally:
        httpd.shutdown()


def test_no_token_configured_refuses_rather_than_running_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpd, get = _serve(monkeypatch, "")
    try:
        assert get("/healthz") == 200
        assert get("/properties", "Bearer ") == 503
    finally:
        httpd.shutdown()


def test_joins_and_leaves_become_events_with_a_session_start(running) -> None:
    running.command("join Steve", wait_s=2)
    joined_at = running.players["Steve"]["joined_at"]
    running.command("leave Steve", wait_s=2)
    kinds = [(e["kind"], e["name"]) for e in running.events_after(0)]
    assert ("join", "Steve") in kinds and ("leave", "Steve") in kinds
    assert kinds[0] == ("server_start", "")
    assert joined_at > 0


def test_a_stopping_server_closes_every_open_session(running) -> None:
    # BDS logs no disconnects on shutdown; without these the play time would run on.
    running.command("join Steve", wait_s=2)
    mark = running.events_after(0)[-1]["id"]
    running.stop(wait_s=5)
    tail = [(e["kind"], e["name"]) for e in running.events_after(mark)]
    assert tail == [("leave", "Steve"), ("server_stop", "")]


def test_versions_compare_numerically_not_as_text() -> None:
    assert server.version_key("1.26.100.1") > server.version_key("1.26.52.3")


class _FakeBds:
    def __init__(self, version: str = "1.26.52.3", *, starts: bool = True) -> None:
        self.version = version
        self.running = True
        self.starts = starts
        self.state = "running"
        self.calls: list[str] = []
        self.players: dict[str, Any] = {}
        self.world = "world"
        self.replaced: list[str] = []

    def note_world_replaced(self, folder: str) -> None:
        self.replaced.append(folder)

    def wait_running(self, _timeout_s: float) -> bool:
        return self.starts

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def start(self) -> None:
        self.calls.append("start")
        self.running = True


def _update_rig(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    snapshot_ok: bool,
    starts: bool = True,
    downloads: tuple[str, ...] = ("1.26.60.4", "1.26.52.3"),
):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    installed: list[str] = []
    monkeypatch.setattr(server.install, "resolve", lambda want: ("1.26.60.4", "u"))

    def ensure(_d: Path, v: str) -> str:
        # Like the real one: a version that can't be fetched leaves what was there.
        if v in downloads:
            installed.append(v)
            return v
        return installed[-1] if installed else "1.26.52.3"

    monkeypatch.setattr(server.install, "ensure", ensure)
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    rig = server.Rig(env={})
    rig.bds = _FakeBds(starts=starts)  # type: ignore[assignment]
    restored: list[str] = []
    monkeypatch.setattr(rig, "restore", restored.append)
    rig.restored = restored  # type: ignore[attr-defined]

    def snap(label: str) -> dict[str, str]:
        if not snapshot_ok:
            raise OSError("disk full")
        return {"name": f"{label}.mcworld"}

    monkeypatch.setattr(rig, "snapshot", snap)
    return rig, installed


def _wait_done(rig) -> dict:
    import time as _t

    for _ in range(100):
        if rig.update_state.get("finished_at"):
            return rig.update_state
        _t.sleep(0.05)
    raise AssertionError("update never finished")


def test_an_update_backs_up_first_then_installs_and_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True)
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "done"
    assert st["backup"] == "pre-update-1.26.52.3.mcworld"
    assert installed == ["1.26.60.4"]
    assert rig.bds.calls == ["stop", "start"]


def test_a_failed_backup_installs_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=False)
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "failed" and "disk full" in st["error"]
    assert installed == []
    assert rig.bds.running  # never stopped


def test_an_up_to_date_server_is_not_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True)
    rig.bds.version = "1.26.60.4"
    assert rig.start_update()["state"] == "current"
    assert installed == [] and rig.bds.calls == []


def test_the_probe_pack_is_listed_in_the_world_and_removable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]
    world = tmp_path / "server" / "worlds" / "world"
    world.mkdir(parents=True)

    out = rig.probe_pack(True)
    listed = json.loads((world / "world_behavior_packs.json").read_text())
    ids = {p["pack_id"] for p in listed}
    # Every start also (re)installs the box's own pack alongside the probe.
    assert ids == {out["pack_id"], JBRAIN_PACK_ID}
    assert (world / "behavior_packs" / "jbrain_probe" / "scripts" / "main.js").exists()
    assert rig.bds.calls == ["stop", "start"]

    rig.probe_pack(False)
    listed = json.loads((world / "world_behavior_packs.json").read_text())
    assert [p["pack_id"] for p in listed] == [JBRAIN_PACK_ID]
    assert not (world / "behavior_packs" / "jbrain_probe").exists()


def test_a_new_version_that_will_not_start_is_rolled_back_with_the_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True, starts=False)
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "rolled_back"
    assert installed == ["1.26.60.4", "1.26.52.3"]  # the new one, then the old again
    assert rig.restored == ["pre-update-1.26.52.3.mcworld"]  # type: ignore[attr-defined]
    assert rig.bds.calls == ["stop", "start", "stop", "start"]


def test_updating_a_stopped_server_leaves_it_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True)
    rig.bds.running = False
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "done" and installed == ["1.26.60.4"]
    assert rig.bds.calls == []  # never started


def test_a_start_keeps_the_installed_version_unless_auto_update_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    (tmp_path / "server").mkdir()
    rig = server.Rig(env={})
    assert rig.wanted_version() == "latest"  # nothing installed yet
    (tmp_path / "server" / install.VERSION_FILE).write_text("1.26.52.3\n")
    assert rig.wanted_version() == "1.26.52.3"  # default: no silent update
    rig.set_settings({"auto_update": True})
    assert rig.wanted_version() == "latest"
    pinned = server.Rig(env={"MC_BDS_VERSION": "1.26.40.1"})
    assert pinned.wanted_version() == "1.26.40.1"  # a pin always wins


def test_a_stopped_world_is_backed_up_cold_and_restores_aside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]
    rig.bds.running = False
    world = tmp_path / "server" / "worlds" / "world"
    (world / "db").mkdir(parents=True)
    (world / "level.dat").write_bytes(b"v1")
    (world / "db" / "1.ldb").write_bytes(b"chunk")

    snap = rig.snapshot("before")
    (world / "level.dat").write_bytes(b"v2-broken")
    rig.restore(snap["name"])

    assert (world / "level.dat").read_bytes() == b"v1"
    assert (world / "db" / "1.ldb").read_bytes() == b"chunk"
    # The replaced folder is moved aside during the restore and removed once it worked.
    assert not [p for p in world.parent.iterdir() if ".replaced-" in p.name]
    with pytest.raises(ValueError):
        rig.restore("../../etc/passwd")


def test_stop_keeps_the_wrapper_up_and_is_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]

    rig.server_action("stop")
    assert rig.bds.calls == ["stop"] and rig.settings()["run"] is False
    rig.server_action("restart")
    assert rig.bds.calls == ["stop", "stop", "start"] and rig.settings()["run"] is True
    with pytest.raises(ValueError):
        rig.server_action("reboot")


def test_a_server_action_waits_for_a_running_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    rig = server.Rig(env={})
    rig._busy = "updating"
    with pytest.raises(RuntimeError):
        rig.server_action("stop")


def test_a_failed_download_is_failed_not_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # install.ensure keeps the old version when a download fails; that must not read
    # as "Done — players can rejoin" while the server is still behind.
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True, downloads=())
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "failed" and "still on 1.26.52.3" in st["error"]
    assert installed == [] and rig.bds.calls == ["stop", "start"]  # old one back up


def test_a_rollback_that_cannot_reinstall_leaves_the_server_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, _installed = _update_rig(
        monkeypatch, tmp_path, snapshot_ok=True, starts=False, downloads=("1.26.60.4",)
    )
    rig.start_update()
    st = _wait_done(rig)
    assert st["state"] == "failed" and "could not be reinstalled" in st["error"]
    assert rig.bds.calls == ["stop", "start", "stop"]  # the broken one isn't restarted
    assert rig.settings()["run"] is False  # so the watchdog doesn't crash-loop it
    assert rig.restored == []  # type: ignore[attr-defined]


def test_restart_with_auto_update_on_runs_the_backed_up_update(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True)
    rig.set_settings({"auto_update": True})
    out = rig.server_action("restart")
    assert out["update"]["to"] == "1.26.60.4"
    st = _wait_done(rig)
    assert st["state"] == "done" and st["backup"] and installed == ["1.26.60.4"]


def test_start_on_a_stopped_server_with_auto_update_starts_it_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, _installed = _update_rig(monkeypatch, tmp_path, snapshot_ok=True)
    rig.bds.running = False
    rig.set_settings({"auto_update": True})
    rig.server_action("start")
    assert _wait_done(rig)["state"] == "done"
    assert rig.bds.calls == ["start"]  # then_start: the owner pressed Start


def test_an_action_during_first_boot_install_is_deferred_to_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    rig = server.Rig(env={})
    rig._life.acquire()
    rig._busy = "installing"
    out = rig.server_action("stop")
    assert out == {"action": "stop", "deferred": True}
    assert rig.settings()["run"] is False  # first boot will honour it


def test_the_watchdog_only_exits_on_a_real_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "SETTINGS", tmp_path / "settings.json")
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]
    rig.bds.running = False
    rig.bds.exit_code = 1  # type: ignore[attr-defined]
    assert server.crashed(rig) is True
    rig.maintenance = True
    assert server.crashed(rig) is False  # a wrapper-driven stop
    rig.maintenance = False
    rig.set_settings({"run": False})
    assert server.crashed(rig) is False  # the owner stopped it


def test_a_fast_restart_keeps_the_new_process_state(running) -> None:
    # The old process's reader must not write "stopped" over its replacement.
    for _ in range(3):
        running.stop(wait_s=5)
        running.start()
        running._wait_for(0, lambda t: "Server started." in t, 10)
        assert running.wait_running(5)
    assert running.state == "running" and running.running


def test_retention_keeps_20_per_world_auto_first_pins_never(tmp_path: Path) -> None:
    import os

    snaps = tmp_path / "snaps"
    snaps.mkdir()
    index = worlds.SnapshotIndex(tmp_path / "index.json", snaps)
    for i in range(18):
        name = f"world-2026{i:02d}-pre-update.mcworld"
        (snaps / name).write_bytes(b"x")
        os.utime(snaps / name, (i, i))
        index.record(name, "world", "pre-update", auto=True)
    for i in range(18, 24):
        name = f"world-2026{i:02d}-mine.mcworld"
        (snaps / name).write_bytes(b"x")
        os.utime(snaps / name, (i, i))
        index.record(name, "world", "mine", auto=False)
    index.set_pinned("world-202600-pre-update.mcworld", True)  # oldest, but pinned
    (snaps / "slot2-202601-x.mcworld").write_bytes(b"x")  # another world: untouched
    index.record("slot2-202601-x.mcworld", "slot2", "x", auto=False)

    removed = index.prune("world")

    left = {s["name"] for s in index.listing("world")}
    assert len([n for n in left if n != "world-202600-pre-update.mcworld"]) == 20
    assert "world-202600-pre-update.mcworld" in left  # pinned is kept and not counted
    assert all("pre-update" in n for n in removed)  # automatic ones went first
    assert all(f"world-2026{i:02d}-mine.mcworld" in left for i in range(18, 24))
    assert index.listing("slot2")


def test_a_restore_refuses_a_zip_member_escaping_the_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "evil.mcworld").write_bytes(_zip({"../../escape": b"x"}))
    rig = server.Rig(env={})
    with pytest.raises(ValueError):
        rig.restore("evil.mcworld")
    assert not (tmp_path / "escape").exists()


def _world_rig(running: bool = False):
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]
    rig.bds.running = running
    return rig


def _make_world(folder: str, content: bytes = b"lvl") -> Path:
    world = server.SERVER_DIR / "worlds" / folder
    (world / "db").mkdir(parents=True)
    (world / "level.dat").write_bytes(content)
    return world


def test_the_existing_world_is_slot_one_and_the_rest_are_empty() -> None:
    _make_world("world")
    view = _world_rig().worlds_view()
    assert [s["id"] for s in view["slots"]] == [f"slot{i}" for i in range(1, 6)]
    first = view["slots"][0]
    assert first["exists"] and first["active"] and first["name"] == "World"
    assert not any(s["exists"] for s in view["slots"][1:])


def test_create_then_load_switches_world_with_a_backup_of_the_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig(running=True)
    monkeypatch.setattr(rig, "snapshot", lambda label, **kw: {"name": label})
    rig.create_slot("slot2", {"name": "Creative", "gamemode": "creative", "seed": "42"})
    rig.load_slot("slot2")
    assert rig.active_folder() == "slot2"
    eff = rig.properties()
    assert (eff["gamemode"], eff["level-seed"]) == ("creative", "42")
    assert rig.bds.calls == ["stop", "start"]
    with pytest.raises(ValueError):
        rig.create_slot("slot1", {})  # occupied


def test_import_validates_backs_up_and_names_from_the_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("slot2", b"old")
    rig = _world_rig()
    rig.slots.update("slot2", name="Old")
    upload = tmp_path / "up.mcworld"
    upload.write_bytes(
        _zip(
            {
                "My World/level.dat": b"new",
                "My World/levelname.txt": b"Castle",
                "My World/db/1.ldb": b"c",
            }
        )
    )
    rec = rig.import_slot("slot2", upload)
    world = server.SERVER_DIR / "worlds" / "slot2"
    assert (world / "level.dat").read_bytes() == b"new"  # unwrapped from its folder
    assert rec["name"] == "Castle" and rec["origin"] == "imported"
    assert any("pre-import" in s["name"] for s in rig.index.listing("slot2"))
    bad = tmp_path / "bad.mcworld"
    bad.write_bytes(_zip({"readme.txt": b"hi"}))
    with pytest.raises(ValueError):
        rig.import_slot("slot3", bad)
    evil = tmp_path / "evil.mcworld"
    evil.write_bytes(_zip({"level.dat": b"x", "../escape": b"x"}))
    with pytest.raises(ValueError):
        rig.import_slot("slot3", evil)


def test_reset_rules_and_same_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    _make_world("slot2")
    rig = _world_rig()
    rig.slots.update("slot2", seed="777")
    with pytest.raises(ValueError):
        rig.reset_slot("slot1", "empty")  # the loaded world can't be emptied
    with pytest.raises(ValueError):
        rig.reset_slot("slot1", "same_seed")  # seed unknown
    rec = rig.reset_slot("slot2", "same_seed")
    assert rec["seed"] == "777" and not rec["exists"]  # regenerates on next load
    assert any("pre-reset" in s["name"] for s in rig.index.listing("slot2"))
    rig.reset_slot("slot2", "empty")
    assert rig.slots.get("slot2")["seed"] is None


def test_restore_into_another_slot_and_pinned_backups_resist_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world", b"v1")
    rig = _world_rig()
    snap = rig.snapshot("mine", auto=False)["name"]
    rig.restore_snapshot(snap, "slot3")
    assert (server.SERVER_DIR / "worlds" / "slot3" / "level.dat").read_bytes() == b"v1"
    rig.index.set_pinned(snap, True)
    with pytest.raises(ValueError):
        rig.delete_snapshot(snap)
    rig.index.set_pinned(snap, False)
    rig.delete_snapshot(snap)
    assert not (server.SNAPSHOT_DIR / snap).exists()


def test_world_operations_refuse_while_an_update_runs() -> None:
    rig = _world_rig()
    rig._life.acquire()
    rig._busy = "updating"
    with pytest.raises(RuntimeError):
        rig.load_slot("slot2")


def test_the_allowlist_edits_the_file_when_stopped_and_the_console_when_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (server.SERVER_DIR).mkdir(parents=True)
    rig = _world_rig()
    rig.change_allowlist({"add": "Steve42", "enabled": True})
    assert rig.allowlist() == {"enabled": True, "players": ["Steve42"]}
    sent: list[str] = []
    rig.bds.running = True
    rig.bds.command = lambda c, **k: sent.append(c) or []  # type: ignore[attr-defined]
    rig.change_allowlist({"remove": "Steve42"})
    assert sent == ['allowlist remove "Steve42"']
    with pytest.raises(ValueError):
        rig.change_allowlist({"add": 'x" op @a'})


LIVE_RULES_LINE = (
    "[2026-10-10 16:42:54:260 INFO] commandBlockOutput = true, doFireTick = true, "
    "keepInventory = false, playerWaypoints = everyone, randomTickSpeed = 1, "
    "spawnRadius = 10"
)


def test_the_live_gamerule_line_parses_into_typed_rules() -> None:
    got = worlds.parse_gamerules([LIVE_RULES_LINE])
    assert got == {
        "commandBlockOutput": True,
        "doFireTick": True,
        "keepInventory": False,
        "playerWaypoints": "everyone",
        "randomTickSpeed": 1,
        "spawnRadius": 10,
    }


def test_rule_changes_are_checked_by_name_and_type() -> None:
    known = worlds.DEFAULT_RULES
    assert worlds.check_rules({"doFireTick": "false", "spawnRadius": 3}, known) == {
        "doFireTick": False,
        "spawnRadius": 3,
    }
    for bad in (
        {"noSuchRule": True},
        {"doFireTick": 5},
        {"spawnRadius": "lots"},
        {"spawnRadius": -1},
        {"playerWaypoints": "a; op @a"},
    ):
        with pytest.raises(ValueError):
            worlds.check_rules(bad, known)


def test_rules_on_a_world_not_loaded_are_saved_and_pending() -> None:
    _make_world("world")
    rig = _world_rig()
    rig.create_slot("slot2", {"name": "Peaceful", "rules": {"doFireTick": False}})
    view = rig.set_rules("slot2", {"keepInventory": True})
    assert view["live"] is False
    assert (
        view["rules"]["doFireTick"] is False and view["rules"]["keepInventory"] is True
    )
    assert view["pending"] == ["doFireTick", "keepInventory"]


def test_rules_on_the_loaded_world_apply_live_and_are_remembered() -> None:
    _make_world("world")
    rig = _world_rig(running=True)
    rig.bds.state = "running"
    sent: list[str] = []

    def command(c: str, **_k: Any) -> list[str]:
        sent.append(c)
        return [LIVE_RULES_LINE] if c == "gamerule" else ["Game rule updated"]

    rig.bds.command = command  # type: ignore[attr-defined]
    view = rig.set_rules("slot1", {"doFireTick": False})
    assert "gamerule doFireTick false" in sent and view["live"] is True
    assert rig.slots.get("slot1")["rules"] == {
        "doFireTick": False
    }  # re-applied on load


def test_a_loaded_world_gets_its_saved_rules_once_it_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _t

    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig(running=False)
    sent: list[str] = []
    rig.bds.command = lambda c, **k: sent.append(c) or []  # type: ignore[attr-defined]
    rig.create_slot("slot2", {"rules": {"keepInventory": True, "doFireTick": False}})
    rig.load_slot("slot2")
    for _ in range(50):
        if len(sent) == 2:
            break
        _t.sleep(0.02)
    assert sorted(sent) == ["gamerule doFireTick false", "gamerule keepInventory true"]


def test_a_manual_seed_is_any_text_on_one_line() -> None:
    assert worlds.check_seed("  glacier 42 ") == "glacier 42"
    with pytest.raises(ValueError):
        worlds.check_seed("a\nb")
    with pytest.raises(ValueError):
        worlds.check_seed("x" * 65)


def _level_dat(**tags: Any) -> bytes:
    """A Bedrock level.dat: the 8-byte header, then a little-endian NBT compound. A
    nested compound and a list are included so the reader has to walk past them."""

    def name(n: str) -> bytes:
        return struct.pack("<H", len(n)) + n.encode()

    body = b""
    for key, value in tags.items():
        if isinstance(value, str):
            body += b"\x08" + name(key) + name(value)
        elif key == "RandomSeed":
            body += b"\x04" + name(key) + struct.pack("<q", value)
        elif isinstance(value, bool) or key.islower():
            body += b"\x01" + name(key) + struct.pack("<b", int(value))
        else:
            body += b"\x03" + name(key) + struct.pack("<i", value)
    body += b"\x0a" + name("abilities") + b"\x05" + name("flySpeed")
    body += struct.pack("<f", 0.05) + b"\x00"
    body += b"\x09" + name("lastOpenedWithVersion") + b"\x03" + struct.pack("<i", 2)
    body += struct.pack("<ii", 1, 26)
    nbt = b"\x0a" + name("") + body + b"\x00"
    return struct.pack("<ii", 10, len(nbt)) + nbt


CASTLE = _level_dat(
    RandomSeed=-2794311108712645813,
    GameType=1,
    Difficulty=0,
    commandsEnabled=True,
    LevelName="Castle Hill",
    dofiretick=False,
    keepinventory=True,
    spawnradius=3,
)


def test_level_dat_tells_a_world_its_seed_settings_and_rules(tmp_path: Path) -> None:
    (tmp_path / "level.dat").write_bytes(CASTLE)
    facts = worlds.world_facts(tmp_path)
    assert facts["seed"] == "-2794311108712645813"
    assert (facts["gamemode"], facts["difficulty"], facts["cheats"]) == (
        "creative",
        "peaceful",
        True,
    )
    rules = facts["rules_known"]
    assert (rules["doFireTick"], rules["keepInventory"], rules["spawnRadius"]) == (
        False,
        True,
        3,
    )
    assert rules["mobGriefing"] is True  # absent from the file: the default
    for junk in (b"", b"\x00" * 8, CASTLE[:-5], b"x" * 40):
        (tmp_path / "level.dat").write_bytes(junk)
        assert worlds.world_facts(tmp_path) == {}


def test_the_first_world_learns_its_seed_from_level_dat() -> None:
    _make_world("world", CASTLE)
    first = _world_rig().worlds_view()["slots"][0]
    assert first["seed"] == "-2794311108712645813"


def test_an_import_takes_its_settings_from_the_file_not_the_old_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("slot2")
    rig = _world_rig()
    rig.slots.update("slot2", name="Creative test", seed="1", difficulty="hard")
    rig.set_rules("slot2", {"doDayLightCycle": False})
    upload = tmp_path / "up.mcworld"
    upload.write_bytes(_zip({"level.dat": CASTLE, "db/1.ldb": b"c"}))
    rec = rig.import_slot("slot2", upload)
    assert rec["seed"] == "-2794311108712645813" and rec["difficulty"] == "peaceful"
    assert rec["rules"] == {} and rec["rules_known"]["keepInventory"] is True
    upload.write_bytes(_zip({"level.dat": b"unreadable", "db/1.ldb": b"c"}))
    assert rig.import_slot("slot2", upload)["seed"] is None  # not the old world's


def test_another_worlds_backup_brings_its_seed_name_and_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world", CASTLE)
    rig = _world_rig()
    rig.bds.state = "stopped"
    rig.slots.update("slot1", name="Castle Hill")
    rig.set_rules("slot1", {"keepInventory": True})
    snap = rig.snapshot("mine", auto=False)["name"]
    _make_world("slot3")
    rig.slots.update("slot3", name="Other", seed="5", gamemode="adventure")
    occupied = rig.restore_snapshot(snap, "slot3")
    assert occupied["seed"] == "-2794311108712645813" and occupied["name"] == "Other"
    assert occupied["gamemode"] == "creative" and occupied["rules"] == {
        "keepInventory": True
    }
    empty = rig.restore_snapshot(snap, "slot4")
    assert empty["name"].startswith("Castle Hill (") and empty["origin"] == "restored"


def test_players_get_a_chat_warning_before_the_server_stops_under_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    monkeypatch.setattr(server, "WARN_S", 0.0)
    _make_world("world")
    rig = _world_rig(running=True)
    sent: list[str] = []
    rig.bds.command = lambda c, **k: sent.append(c) or []  # type: ignore[attr-defined]
    rig.server_action("restart")
    assert sent == []  # nobody on: no message
    rig.bds.players = {"Steve": {}}
    rig.server_action("stop")
    assert sent and sent[0].startswith("say ") and "stops in" in sent[0]


def test_a_running_job_and_its_phase_show_in_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig()
    rig.bds.exit_code = None  # type: ignore[attr-defined]
    rig.bds.started_at = None  # type: ignore[attr-defined]
    rig.bds.version = "1.26.52.3"
    seen: list[Any] = []
    monkeypatch.setattr(
        rig, "restore", lambda name, folder: seen.append(rig.status()["job"])
    )
    snap = rig.snapshot("mine", auto=False)["name"]
    rig.restore_snapshot(snap, "slot1")
    assert seen[0]["what"] == "restoring a backup" and seen[0]["phase"] == "writing"
    assert seen[0]["started_at"] and rig.status()["job"] is None


def test_saved_rules_come_back_on_a_plain_start_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as _t

    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig(running=False)
    rig.bds.state = "stopped"
    sent: list[str] = []
    rig.bds.command = lambda c, **k: sent.append(c) or []  # type: ignore[attr-defined]
    rig.set_rules("slot1", {"doFireTick": False})
    assert sent == []  # stopped: saved, pending
    rig.server_action("start")
    for _ in range(50):
        if sent:
            break
        _t.sleep(0.02)
    assert sent == ["gamerule doFireTick false"]


def test_a_download_is_recorded_on_the_backup_and_the_world() -> None:
    _make_world("world")
    rig = _world_rig()
    snap = rig.snapshot("mine", auto=False)["name"]
    assert rig.index.listing()[0]["downloaded_at"] is None
    rig.index.note_download(snap, "world")
    assert rig.index.listing()[0]["downloaded_at"] and rig.index.last_download("world")


def test_a_setting_saved_since_the_last_start_reads_as_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig(running=False)
    assert rig.pending_restart() == []  # never started: nothing to compare against
    rig.server_action("start")
    rig.bds.command = lambda c, **k: []  # type: ignore[attr-defined]
    rig.update_slot("slot1", {"gamemode": "creative", "difficulty": "hard"})
    rig.set_overrides({"max-players": "8"})
    # difficulty went live through the console, so only the others wait for a restart
    assert rig.pending_restart() == ["gamemode", "max-players"]
    rig.server_action("restart")
    assert rig.pending_restart() == []


def test_an_automatic_backup_says_what_it_was_taken_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    rig = _world_rig(running=False)
    rig.create_slot("slot2", {"name": "Creative test"})
    rig.bds.running = True
    monkeypatch.setattr(
        rig,
        "snapshot",
        lambda label, **kw: rig.cold_snapshot(label, None, True, kw.get("note", "")),
    )
    rig.load_slot("slot2")
    (snap,) = rig.index.listing("world")
    assert snap["note"] == "before loading Creative test" and snap["auto"]


def test_a_long_world_operation_answers_202_and_reports_its_end() -> None:
    import threading

    rig = _world_rig()
    gate = threading.Event()
    code, body = rig.run_job("loading a world", lambda: gate.wait(5), wait_s=0.05)
    assert (code, body["accepted"]) == (202, True)
    gate.set()
    for _ in range(100):
        if rig.last_job:
            break
        threading.Event().wait(0.01)
    assert rig.last_job["what"] == "loading a world" and rig.last_job["ok"] is True
    assert rig.run_job("quick", lambda: {"done": 1}) == (200, {"done": 1})

    def refuse() -> None:
        raise RuntimeError("busy: updating")

    with pytest.raises(RuntimeError):  # a refusal is immediate, so it stays a 409
        rig.run_job("loading a world", refuse)
    assert rig.last_job["ok"] is False and "busy" in rig.last_job["detail"]


def test_an_import_to_a_busy_server_is_refused_before_the_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error
    import urllib.request

    httpd, _get = _serve(monkeypatch, "t")
    try:
        rig = server.RIG
        rig._life.acquire()
        rig._busy = "updating"
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/worlds/slot2/import",
            data=b"x" * 1024,
            method="POST",
            headers={"Authorization": "Bearer t"},
        )
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 409
        assert not any(server.UPLOAD_DIR.glob("*"))  # nothing was read or kept
    finally:
        httpd.shutdown()


def test_quick_edits_wait_their_turn_and_seeds_stay_on_one_line() -> None:
    _make_world("world")
    _make_world("slot2")
    rig = _world_rig()
    rig._life.acquire()
    rig._busy = "importing a world"
    with pytest.raises(RuntimeError):
        rig.update_slot("slot2", {"name": "Mine"})  # an import may be rewriting it
    with pytest.raises(RuntimeError):
        rig.set_rules("slot2", {"pvp": False})
    rig._life.release()
    rig._busy = ""
    with pytest.raises(ValueError):
        rig.reset_slot("slot2", "new_seed", "1\nallow-cheats=true")


QT_LINE = (
    '[2026-10-10 20:01:02:003 INFO] [{"dimension":1,"id":-4294967295,'
    '"position":{"x":12.34,"y":64.0,"z":-80.56},"uniqueId":"-4294967295","yRot":91.2}]'
)


# Captured from the box's BDS 1.26.52.3 (2026-10-10, an armor stand at spawn): the reply
# is pretty-printed over many lines, and only the first carries the log prefix.
QT_REPLY_126 = [
    "[2026-10-10 20:25:06:737 INFO] Target data: [",
    "   {",
    '      "dimension" : 0,',
    '      "id" : -38654705663,',
    '      "position" : {',
    '         "x" : 0.50,',
    '         "y" : 113.4060516357422,',
    '         "z" : 0.50',
    "      },",
    '      "uniqueId" : "-38654705663",',
    '      "yRot" : 0.0',
    "   }",
    "]",
    "",
]


def test_the_real_multi_line_querytarget_reply_parses() -> None:
    assert server.parse_querytarget(QT_REPLY_126) == {
        "x": 0.5,
        "y": 113.4,
        "z": 0.5,
        "dim": "overworld",
        "yaw": 0.0,
    }
    assert server.parse_querytarget(["[x ERROR] No targets matched selector"]) is None


def test_a_querytarget_reply_becomes_a_position() -> None:
    assert server.parse_querytarget([QT_LINE]) == {
        "x": 12.3,
        "y": 64.0,
        "z": -80.6,
        "dim": "nether",
        "yaw": 91.2,
    }
    for junk in ([], ["No targets matched selector"], ["[{not json"], ["[{}]"]):
        assert server.parse_querytarget(junk) is None


def test_positions_are_kept_only_after_a_real_move() -> None:
    rig = _world_rig(running=True)
    rig.bds.state = "running"
    rig.bds.players = {"Steve42": {"xuid": "111"}}
    at = {"x": 0.0}

    def command(c: str, **_k: Any) -> list[str]:
        assert c == 'querytarget "Steve42"'
        return [
            '[x INFO] [{"dimension":0,"position":'
            f'{{"x":{at["x"]},"y":64,"z":0}},"yRot":0}}]'
        ]

    rig.bds.command = command  # type: ignore[attr-defined]
    assert rig.sample_positions() == 1
    at["x"] = 2.0  # under TRACK_MIN_MOVE: standing about costs nothing
    assert rig.sample_positions() == 0
    at["x"] = 9.0
    assert rig.sample_positions() == 1
    got = rig.track_after(0)
    assert [(t["x"], t["xuid"], t["world"], t["dim"]) for t in got] == [
        (0.0, "111", "world", "overworld"),
        (9.0, "111", "world", "overworld"),
    ]
    assert rig.track_after(got[0]["id"]) == [got[1]]
    rig.maintenance = True  # a world operation owns the console
    assert rig.sample_positions() == 0


def test_the_pack_reports_deaths_and_respawns_as_events(tmp_path: Path) -> None:
    b = bds.Bds(tmp_path)
    b.world = "slot2"
    b._record("[x INFO] Player connected: Steve42, xuid: 111")
    b._record(
        '[Scripting] [jbrain] {"ev":"death","name":"Steve42","dim":"overworld",'
        '"x":1.5,"y":64,"z":-3,"cause":"fall","killer":null}'
    )
    b._record(
        '[Scripting] [jbrain] {"ev":"respawn","name":"Steve42","x":0,"y":70,"z":0}'
    )
    b._record('[Scripting] [jbrain] {"ev":"op","name":"Steve42"}')  # unknown: ignored
    b._record("[Scripting] [jbrain] {not json")
    kinds = [(e["kind"], e["xuid"], e["world"]) for e in b.events_after(0)]
    assert kinds == [
        ("join", "111", "slot2"),
        ("death", "111", "slot2"),
        ("respawn", "111", "slot2"),
    ]
    death = b.events_after(1)[0]
    assert (death["cause"], death["x"], death["dim"]) == ("fall", 1.5, "overworld")
    assert "killer" not in death  # null is not a string: dropped, not "None"


def test_a_reset_or_import_marks_the_old_trail_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    _make_world("world")
    _make_world("slot2")
    rig = _world_rig()
    rig.slots.update("slot2", seed="777")
    rig.reset_slot("slot2", "same_seed")
    assert rig.bds.replaced == ["slot2"]
    real = bds.Bds(server.SERVER_DIR)
    real.note_world_replaced("slot3")
    (ev,) = real.events_after(0)
    assert (ev["kind"], ev["world"], real.world) == ("world_replaced", "slot3", "world")
