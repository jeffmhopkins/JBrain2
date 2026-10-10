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
    assert running.players == {"Steve": "2535"}
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


def test_the_box_pins_raknet_and_console_script_output() -> None:
    got = install.env_overrides({})
    assert got["transport"] == "raknet"
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
    rig.set_overrides({"transport": "nethernet"})
    assert rig.properties()["transport"] == "nethernet"
    rig.set_overrides({"transport": None})
    assert rig.properties()["transport"] == "raknet"
