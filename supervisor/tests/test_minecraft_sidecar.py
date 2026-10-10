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
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

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
bds = _load("bds", "bds.py")
server = _load("mc_server", "server.py")

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
            print("[x INFO] Changes to the level are resumed.", flush=True)
            with open("resumed", "w") as fh:
                fh.write("yes")
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
    def __init__(self, version: str = "1.26.52.3") -> None:
        self.version = version
        self.running = True
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def start(self) -> None:
        self.calls.append("start")
        self.running = True


def _update_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, snapshot_ok: bool):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(server, "OVERRIDES", tmp_path / "properties.json")
    installed: list[str] = []
    monkeypatch.setattr(server.install, "resolve", lambda want: ("1.26.60.4", "u"))
    monkeypatch.setattr(server.install, "ensure", lambda d, v: installed.append(v))
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)
    rig = server.Rig(env={})
    rig.bds = _FakeBds()  # type: ignore[assignment]

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
    assert listed == [{"pack_id": out["pack_id"], "version": [1, 0, 0]}]
    assert (world / "behavior_packs" / "jbrain_probe" / "scripts" / "main.js").exists()
    assert rig.bds.calls == ["stop", "start"]

    rig.probe_pack(False)
    assert json.loads((world / "world_behavior_packs.json").read_text()) == []
    assert not (world / "behavior_packs" / "jbrain_probe").exists()
