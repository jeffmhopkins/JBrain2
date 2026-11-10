"""The endpoint sidecar's GrowBot half (`deploy/endpoint/pico.py` and its `/pico/*`
routes), loaded by path like the rest of `deploy/endpoint`.

Pinned here with no board: the mpremote and picotool argv; the fixed on-device scripts,
run under CPython against a fake filesystem and a fake `machine`; the vid/pid filter
that keeps every Pico route off a panel or a bridge; the name and key regexes; the sysfs
bootloader scan; that the temporary directory holding a robot's secrets is gone however
a job ends; and the `OK …` / `FAILED: <code>: …` line each stream ends with.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import importlib
import io
import json
import os
import sys
import threading
import types
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
sys.path.insert(0, str(DEPLOY / "endpoint"))

ports = importlib.import_module("ports")
monitor = importlib.import_module("monitor")
pico = importlib.import_module("pico")
server = importlib.import_module("server")

KEY = "A" * 20 + "b-_" + "9" * 20
UF2 = b"UF2 image bytes"
SHA = hashlib.sha256(UF2).hexdigest()


def _port(device: str, vid: str, pid: str) -> Any:
    return ports.Port(
        device, vid, pid, "", "", vid == "303a", "label", vid == ports.PICO_VID
    )


PICO = _port("/dev/ttyACM0", "2e8a", "0005")
PANEL = _port("/dev/ttyACM1", "303a", "1001")
BOOT = {"bus": 1, "address": 7, "pid": "000f", "serial": "E66", "label": "boot"}


# -- the pure half ---------------------------------------------------------------------


class TestArgv:
    def test_picotool_gets_only_a_bus_an_address_and_our_file(self) -> None:
        argv = pico.picotool_argv(1, 7, "/tmp/x/micropython.uf2")
        assert argv == [
            "picotool",
            "load",
            "-v",
            "-x",
            "/tmp/x/micropython.uf2",
            "-t",
            "uf2",
            "--bus",
            "1",
            "--address",
            "7",
        ]

    def test_mpremote_commands(self) -> None:
        assert pico.bootloader_argv("/dev/ttyACM0") == [
            "mpremote", "connect", "/dev/ttyACM0", "bootloader",
        ]  # fmt: skip
        assert pico.reset_argv("/dev/ttyACM0")[-1] == "reset"
        assert pico.identify_argv("/dev/ttyACM0")[:4] == [
            "mpremote", "connect", "/dev/ttyACM0", "exec",
        ]  # fmt: skip
        assert pico.run_argv("/dev/ttyACM0", "/tmp/r.py") == [
            "mpremote", "connect", "/dev/ttyACM0", "run", "/tmp/r.py",
        ]  # fmt: skip

    def test_every_file_goes_in_one_session(self) -> None:
        argv = pico.copy_argv(
            "/dev/ttyACM0", [("a.py", "a.py"), ("relay_chip.py", "main.py")]
        )
        assert argv == [
            "mpremote", "connect", "/dev/ttyACM0",
            "cp", "a.py", ":a.py", "+", "cp", "relay_chip.py", ":main.py",
        ]  # fmt: skip
        assert "&&" not in argv


class TestScripts:
    def test_identify_prints_the_pairing_code_relay_chip_uses(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = types.ModuleType("machine")
        fake.unique_id = lambda: bytes.fromhex("e6614c311b2a3f4e")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "machine", fake)
        exec(pico.IDENTIFY_SCRIPT, {})
        line = capsys.readouterr().out.strip()
        assert line.startswith(pico.IDENTIFY_MARK)
        found = json.loads(line[len(pico.IDENTIFY_MARK) :])
        assert found["code"] == "gb-2a3f4e"
        assert found["micropython"].count(".") == 2 and found["machine"]

    def test_rekey_swaps_only_the_key_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        original = (
            'GROWBOT_HOST = "10.0.0.2"\nGROWBOT_KEY = "old"\n'
            'GROWBOT_CA = "MIIBzz"\nWIFI_NETWORKS = []\n'
        )
        (tmp_path / "secrets.py").write_text(original)
        exec(pico.rekey_script(KEY), {})
        assert capsys.readouterr().out.strip() == pico.REKEY_OK
        assert (tmp_path / "secrets.py").read_text() == original.replace("old", KEY)
        assert not (tmp_path / "secrets.py.tmp").exists()

    @pytest.mark.parametrize(
        ("body", "said"),
        [
            ('GROWBOT_CA = "MIIBzz"\nX = 1\n', "GBREKEY NOKEY"),
            (
                'GROWBOT_CA = "MIIBzz"\nGROWBOT_KEY = "a"\nGROWBOT_KEY = "b"\n',
                "GBREKEY NOKEY",
            ),
            # It would dial unverified with its new key: only a reflash fixes that.
            ('GROWBOT_KEY = "a"\n', "GBREKEY NOCA"),
            ('GROWBOT_KEY = "a"\nGROWBOT_CA = None\n', "GBREKEY NOCA"),
            ('GROWBOT_KEY = "a"\nGROWBOT_CA = ""\n', "GBREKEY NOCA"),
            ('GROWBOT_KEY = "a"\nGROWBOT_CA = b"MIIBzz"\n', "GBREKEY NOCA"),
            ('GROWBOT_KEY = "a"\nGROWBOT_CA = "MI" + "IB"\n', "GBREKEY NOCA"),
            (
                'GROWBOT_KEY = "a"\nGROWBOT_CA = "MIIBzz"\nGROWBOT_CA = "MIIByy"\n',
                "GBREKEY NOCA",
            ),
        ],
    )
    def test_rekey_refuses_a_secrets_file_without_one_key_and_ca_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        body: str,
        said: str,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "secrets.py").write_text(body)
        with pytest.raises(SystemExit):
            exec(pico.rekey_script(KEY), {})
        assert capsys.readouterr().out.strip() == said
        assert (tmp_path / "secrets.py").read_text() == body

    @pytest.mark.parametrize(
        "key",
        ["", "short", "x" * 65, 'a" + __import__("os") + "' + "a" * 32, "a b" * 20],
    )
    def test_no_key_but_a_device_key_enters_the_script(self, key: str) -> None:
        with pytest.raises(pico.PicoError):
            pico.rekey_script(key)
        assert not pico.KEY_RE.fullmatch(key)


class TestValidation:
    def test_only_a_raspberry_pi_tty_is_a_pico_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico.ports, "scan", lambda: [PICO, PANEL])
        assert pico.pico_port("/dev/ttyACM0") == "/dev/ttyACM0"
        # A Raspberry Pi tty that is not MicroPython (a debug probe) is not a robot.
        probe = _port("/dev/ttyACM7", "2e8a", "000c")
        monkeypatch.setattr(pico.ports, "scan", lambda: [PICO, PANEL, probe])
        for bad in (
            "/dev/ttyACM1",
            "/dev/ttyACM7",
            "/dev/sda",
            "",
            None,
            "/dev/ttyACM0/../sda",
        ):
            with pytest.raises(ValueError, match="not a Pico"):
                pico.pico_port(bad)

    def test_files_are_flat_python_names_bounded_in_count_and_size(self) -> None:
        def f(name: str, data: bytes = b"x") -> dict[str, str]:
            return {"name": name, "b64": base64.b64encode(data).decode()}

        assert pico.decode_files([f("main.py"), f("PicoRobotics.py")]) == [
            ("main.py", b"x"),
            ("PicoRobotics.py", b"x"),
        ]
        bad_names = [
            "../main.py",
            "a/b.py",
            "main.mpy",
            ".py",
            "secrets.py",
            "main.py\n",
            "é.py",
        ]
        for name in bad_names:
            with pytest.raises(ValueError):
                pico.decode_files([f(name)])
        for bad in (
            [f("a.py"), f("a.py")],
            [f(f"f{i}.py") for i in range(pico.MAX_FILES + 1)],
            [f("big.py", b"x" * (pico.MAX_FILE_BYTES + 1))],
            [{"name": "a.py", "b64": "not base64!"}],
            ["a.py"],
            [],
            "a.py",
        ):
            with pytest.raises(ValueError):
                pico.decode_files(bad)

    def test_secrets_are_bounded_like_wifi_store(self) -> None:
        ok = base64.b64encode(b"GROWBOT_KEY = 'x'\n").decode()
        assert pico.decode_secrets(ok)
        for bad in (
            "",
            base64.b64encode(b"x" * (pico.MAX_SECRETS_BYTES + 1)).decode(),
            "%%",
        ):
            with pytest.raises(ValueError):
                pico.decode_secrets(bad)


def _usb_device(root: Path, name: str, vid: str, pid: str, **attrs: str) -> None:
    node = root / "bus" / "usb" / "devices" / name
    node.mkdir(parents=True)
    for key, value in {"idVendor": vid, "idProduct": pid, **attrs}.items():
        (node / key).write_text(value + "\n")


class TestScanBoot:
    def test_only_raspberry_pi_bootloaders_with_kernel_numbers(
        self, tmp_path: Path
    ) -> None:
        _usb_device(
            tmp_path, "1-2", "2e8a", "000f", busnum="1", devnum="7", serial="E66"
        )
        _usb_device(tmp_path, "1-3", "2e8a", "0003", busnum="1", devnum="8")
        _usb_device(tmp_path, "1-4", "2e8a", "0005", busnum="1", devnum="9")
        _usb_device(tmp_path, "1-5", "303a", "000f", busnum="1", devnum="10")
        _usb_device(tmp_path, "1-6", "2e8a", "000f", busnum="x", devnum="11")
        _usb_device(tmp_path, "1-2:1.0", "2e8a", "000f", busnum="1", devnum="7")
        found = ports.scan_boot(str(tmp_path))
        assert [(d["bus"], d["address"], d["pid"]) for d in found] == [
            (1, 7, "000f"),
            (1, 8, "0003"),
        ]
        assert found[0]["serial"] == "E66" and "Pico 2 W" in found[0]["label"]
        assert "not supported" in found[1]["label"]
        assert set(found[0]) == {"bus", "address", "pid", "serial", "label"}

    def test_no_usb_tree_is_no_bootloader(self, tmp_path: Path) -> None:
        assert ports.scan_boot(str(tmp_path)) == []

    def test_a_pico_tty_is_marked_and_named(self, tmp_path: Path) -> None:
        dev = tmp_path / "dev"
        dev.mkdir()
        (dev / "ttyACM0").write_text("")
        usbdev = tmp_path / "sys" / "class" / "tty" / "ttyACM0" / "device" / ".."
        node = tmp_path / "sys" / "class" / "tty" / "ttyACM0" / "device"
        node.mkdir(parents=True)
        for key, value in (
            ("idVendor", "2e8a"),
            ("idProduct", "0005"),
            ("serial", "E66"),
        ):
            (node / key).write_text(value)
        (found,) = ports.scan(str(tmp_path / "sys"), str(dev))
        assert found.is_pico and not found.is_espressif
        assert "MicroPython" in found.label
        assert ports.usb_serial("/dev/ttyACM0", str(tmp_path / "sys")) == "E66"
        assert ports.usb_serial("/dev/ttyACM9", str(tmp_path / "sys")) == ""
        assert usbdev  # the walk climbs from the interface; see test_endpoint_sidecar

    def test_another_raspberry_pi_device_is_named_by_its_pid(self) -> None:
        assert ports._label("2e8a", "000a", "", "") == "Raspberry Pi device 000a"


# -- the process half, with run() and the device scans faked ---------------------------


class Board:
    """A fake Pico: what `run` yields per argv, and what the scans see as it goes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[str]] = []
        self.boot: list[dict[str, Any]] = [BOOT]
        self.ttys: list[Any] = [PICO]
        self.exit: dict[str, int] = {}
        self.out: dict[str, list[str]] = {}
        self.console: list[str] = ["WIFI_OK 10.0.0.9", "handshake: OK b'HTTP/1.1 101'"]
        self.seen_files: dict[str, bytes] = {}
        self.workdirs: list[str] = []
        monkeypatch.setattr(pico.ports, "scan_boot", lambda: list(self.boot))
        monkeypatch.setattr(pico.ports, "scan", lambda: list(self.ttys))
        monkeypatch.setattr(pico.ports, "usb_serial", lambda port: "E66")
        monkeypatch.setattr(pico, "run", self.run)
        monkeypatch.setattr(pico, "console", self.watch)
        monkeypatch.setattr(pico.time, "sleep", lambda s: None)
        monkeypatch.setattr(pico, "BOOT_WAIT_S", 0.0)
        monkeypatch.setattr(pico, "TTY_WAIT_S", 0.0)
        monkeypatch.setattr(pico, "RESET_WAIT_S", 0.0)
        real = pico.tempfile.mkdtemp

        def mkdtemp(prefix: str = "") -> str:
            path = real(prefix=prefix)
            self.workdirs.append(path)
            return path

        monkeypatch.setattr(pico.tempfile, "mkdtemp", mkdtemp)

    def run(self, argv: list[str], workdir: str, timeout: float = 0) -> Iterator[str]:
        self.calls.append(argv)
        verb = argv[3] if argv[0] == "mpremote" else argv[1]
        if verb == "cp":
            for i, part in enumerate(argv):
                if part == "cp":
                    self.seen_files[argv[i + 2]] = (
                        Path(workdir) / argv[i + 1]
                    ).read_bytes()
        if verb == "bootloader":
            self.ttys = []
        if verb == "load":
            self.boot = []
            self.ttys = [PICO]
        yield from self.out.get(verb, [f"{verb} ran"])
        yield f"exit {self.exit.get(verb, 0)}"

    def watch(self, port: str, seconds: float) -> Iterator[str]:
        yield from self.console

    def verbs(self) -> list[str]:
        return [c[3] if c[0] == "mpremote" else c[1] for c in self.calls]

    def clean(self) -> bool:
        return all(not os.path.exists(d) for d in self.workdirs) and bool(self.workdirs)


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch) -> Board:
    return Board(monkeypatch)


def _drain(lines: Iterator[str]) -> tuple[list[str], Any]:
    out: list[str] = []
    try:
        for line in lines:
            out.append(line)
    except pico.PicoError as exc:
        return out, exc
    return out, None


class TestLoad:
    def test_a_blank_board_is_written_and_comes_up(self, board: Board) -> None:
        board.ttys = []
        lines, err = _drain(pico.load(None, UF2, SHA))
        assert err is None and lines[-1] == "OK port=/dev/ttyACM0"
        assert board.verbs() == ["load"]
        assert board.calls[0][-4:] == ["--bus", "1", "--address", "7"]
        assert board.clean()

    def test_a_running_board_is_sent_to_its_bootloader_first(
        self, board: Board
    ) -> None:
        board.boot = []

        def bootloader(
            argv: list[str], workdir: str, timeout: float = 0
        ) -> Iterator[str]:
            board.calls.append(argv)
            board.boot = [BOOT]
            yield "exit 1"  # the board drops off USB under mpremote; not news

        runs = iter([bootloader, board.run])
        pico.run = lambda *a, **k: next(runs)(*a, **k)  # type: ignore[assignment]
        lines, err = _drain(pico.load("/dev/ttyACM0", UF2, SHA))
        assert err is None and lines[-1] == "OK port=/dev/ttyACM0"
        assert board.verbs() == ["bootloader", "load"]

    def test_a_bad_hash_never_touches_the_board(self, board: Board) -> None:
        _, err = _drain(pico.load("/dev/ttyACM0", UF2, "0" * 64))
        assert err.code == "load_failed" and board.calls == [] and board.clean()

    @pytest.mark.parametrize("boot", [[], [BOOT, {**BOOT, "address": 8}]])
    def test_none_or_two_bootloaders_is_not_bootsel(
        self, board: Board, boot: list[Any]
    ) -> None:
        board.boot = boot
        _, err = _drain(pico.load(None, UF2, SHA))
        assert (
            err.code == "not_bootsel" and "load" not in board.verbs() and board.clean()
        )

    def test_picotool_failing_is_load_failed(self, board: Board) -> None:
        board.exit["load"] = 249
        board.out["load"] = ["No accessible RP-series device in BOOTSEL mode was found"]
        lines, err = _drain(pico.load(None, UF2, SHA))
        assert err.code == "load_failed" and "exit 249" in str(err)
        assert lines[-1].startswith("No accessible") and board.clean()

    def test_no_micropython_afterwards_is_usb_gone(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico, "pico_ttys", lambda: [])
        _, err = _drain(pico.load(None, UF2, SHA))
        assert err.code == "usb_gone" and board.clean()

    def test_a_held_port_is_refused(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico.monitor, "release", lambda port: False)
        _, err = _drain(pico.load("/dev/ttyACM0", UF2, SHA))
        assert err.code == "usb_gone"


class TestIdentify:
    def test_it_reads_the_marked_line(self, board: Board) -> None:
        board.out["exec"] = [
            "noise",
            'GBID {"code": "gb-2a3f4e", "micropython": "1.28.0", "machine": "RP2350"}',
        ]
        assert pico.identify("/dev/ttyACM0") == {
            "code": "gb-2a3f4e",
            "micropython": "1.28.0",
            "machine": "RP2350",
        }
        assert board.clean()

    @pytest.mark.parametrize("out", [["Traceback"], ["GBID {broken"], ["GBID []"]])
    def test_no_answer_is_usb_gone(self, board: Board, out: list[str]) -> None:
        board.out["exec"] = out
        with pytest.raises(pico.PicoError) as err:
            pico.identify("/dev/ttyACM0")
        assert err.value.code == "usb_gone" and board.clean()

    def test_a_held_port_is_refused(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico.monitor, "release", lambda port: False)
        with pytest.raises(pico.PicoError):
            pico.identify("/dev/ttyACM0")


FILES = [("act_engine.py", b"# act"), ("main.py", b"# main")]
SECRETS = b'GROWBOT_KEY = "k"\n'


class TestProvision:
    def test_reset_copy_reset_and_watch_until_the_handshake(self, board: Board) -> None:
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err is None and lines[-1] == "OK"
        assert board.verbs() == ["reset", "cp", "reset"]
        copy = board.calls[1]
        assert copy[-3:] == ["cp", "secrets.py", ":secrets.py"]
        assert board.seen_files == {
            ":act_engine.py": b"# act",
            ":main.py": b"# main",
            ":secrets.py": SECRETS,
        }
        assert "copied" in lines and "WIFI_OK 10.0.0.9" in lines
        assert board.clean()

    def test_a_clock_it_could_not_set_is_clock_unset(self, board: Board) -> None:
        board.console = [
            "WIFI_OK 10.0.0.9",
            "clock: box sntp timeout",
            "handshake: FAIL clock",
        ]
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err.code == "clock_unset" and lines[-1] == "handshake: FAIL clock"
        assert board.clean()

    def test_only_the_sidecars_own_lines_are_marks(self, board: Board) -> None:
        board.console = [
            "OK",
            "port=/dev/sda",
            "copied",
            "WIFI_OK 10.0.0.9",
            "handshake: OK",
        ]
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        marks = [line for line in lines if isinstance(line, pico.Mark)]
        assert err is None and marks == ["port=/dev/ttyACM0", "copied", "OK"]
        assert board.calls[1][2] == "/dev/ttyACM0"

    def test_wifi_that_fails_is_wifi_failed(self, board: Board) -> None:
        board.console = ["PAIRING CODE:  gb-2a3f4e", "WIFI_FAIL"]
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err.code == "wifi_failed" and lines[-1] == "WIFI_FAIL" and board.clean()

    def test_silence_is_timeout(self, board: Board) -> None:
        board.console = ["PAIRING CODE:  gb-2a3f4e"]
        _, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err.code == "timeout" and board.clean()

    def test_joined_without_a_handshake_line_is_still_ok(self, board: Board) -> None:
        board.console = ["WIFI_OK 10.0.0.9", "re-dial in 5s"]
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err is None and lines[-1] == "OK"

    def test_a_long_wait_after_joining_ends_ok(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico, "HANDSHAKE_WAIT_S", -1.0)
        board.console = ["WIFI_OK 10.0.0.9", "wifi: True"]
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err is None and lines[-2:] == ["WIFI_OK 10.0.0.9", "OK"]

    def test_a_copy_that_fails_is_copy_failed_and_leaves_no_secrets(
        self, board: Board
    ) -> None:
        board.exit["cp"] = 1
        _, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert (
            err.code == "copy_failed"
            and board.verbs() == ["reset", "cp"]
            and board.clean()
        )

    def test_a_board_that_does_not_come_back_is_usb_gone(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico, "pico_ttys", lambda: [])
        _, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err.code == "usb_gone" and board.clean()

    def test_the_tty_is_found_again_by_its_serial(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico, "pico_ttys", lambda: ["/dev/ttyACM3", "/dev/ttyACM4"])
        monkeypatch.setattr(
            pico.ports, "usb_serial", lambda p: "E66" if p != "/dev/ttyACM3" else "X"
        )
        lines, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err is None and "port=/dev/ttyACM4" in lines
        assert board.calls[1][2] == "/dev/ttyACM4"

    def test_a_held_port_is_refused(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico.monitor, "release", lambda port: False)
        _, err = _drain(pico.provision("/dev/ttyACM0", FILES, SECRETS, 60))
        assert err.code == "usb_gone"

    def test_watch_join_needs_the_board_back(
        self, board: Board, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pico, "pico_ttys", lambda: [])
        _, err = _drain(pico.watch_join("/dev/ttyACM0", 5))
        assert err.code == "usb_gone"


class TestRekey:
    def test_the_key_goes_in_a_file_never_on_the_command_line(
        self, board: Board
    ) -> None:
        board.out["run"] = ["GBREKEY OK"]
        lines, err = _drain(pico.rekey("/dev/ttyACM0", KEY))
        assert err is None and lines[-1] == "OK" and "key written" in lines
        assert board.verbs() == ["reset", "run", "reset"]
        assert all(KEY not in arg for call in board.calls for arg in call)
        assert all(KEY not in line for line in lines)
        assert board.clean()

    @pytest.mark.parametrize(
        ("out", "code"), [(["GBREKEY NOKEY"], 1), (["nothing"], 0)]
    )
    def test_no_single_key_line_is_copy_failed(
        self, board: Board, out: list[str], code: int
    ) -> None:
        board.out["run"] = out
        board.exit["run"] = code
        _, err = _drain(pico.rekey("/dev/ttyACM0", KEY))
        assert (
            err.code == "copy_failed"
            and board.verbs() == ["reset", "run"]
            and board.clean()
        )

    def test_a_robot_without_a_box_certificate_needs_a_reflash(
        self, board: Board
    ) -> None:
        board.out["run"] = ["GBREKEY NOCA"]
        _, err = _drain(pico.rekey("/dev/ttyACM0", KEY))
        assert (
            err.code == "needs_reflash"
            and board.verbs() == ["reset", "run"]
            and board.clean()
        )

    def test_a_bad_key_is_refused_before_the_board(self, board: Board) -> None:
        _, err = _drain(pico.rekey("/dev/ttyACM0", "short"))
        assert err is not None and board.calls == []


class TestOneAtATime:
    def test_a_second_job_is_refused_while_one_runs(self, board: Board) -> None:
        first = pico.provision("/dev/ttyACM0", FILES, SECRETS, 60)
        next(first)
        _, err = _drain(pico.load(None, UF2, SHA))
        assert err.code == "internal" and "another Pico job" in str(err)
        with pytest.raises(pico.PicoError):
            pico.identify("/dev/ttyACM0")
        first.close()
        assert pico._busy.acquire(blocking=False)
        pico._busy.release()


class TestRealProcesses:
    def test_run_streams_lines_and_the_exit_code(self, tmp_path: Path) -> None:
        code = "import sys; print('one'); print('two'); sys.exit(3)"
        assert list(pico.run([sys.executable, "-c", code], str(tmp_path))) == [
            "one",
            "two",
            "exit 3",
        ]

    def test_a_hung_tool_is_killed(self, tmp_path: Path) -> None:
        code = "import time; print('started', flush=True); time.sleep(30)"
        lines = list(pico.run([sys.executable, "-c", code], str(tmp_path), timeout=0.5))
        assert lines[0] == "started" and lines[-1].startswith("exit -")

    def test_wait_for_polls_until_an_answer_or_the_deadline(self) -> None:
        answers = iter(["", "", "found"])
        naps: list[float] = []
        assert pico.wait_for(lambda: next(answers), 5, naps.append) == "found"
        assert naps == [pico.POLL_S, pico.POLL_S]
        assert pico.wait_for(lambda: "", 0, naps.append) == ""

    def test_the_console_is_read_with_dtr_on_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeSerial:
            def __init__(self) -> None:
                self.data = io.BytesIO(b"PAIRING CODE:  gb-1\r\nWIFI_OK 10.0.0.9\r\n")
                self.closed = False

            def read(self, n: int) -> bytes:
                return self.data.read(n)

            def close(self) -> None:
                self.closed = True

        fake = FakeSerial()
        monkeypatch.setattr(pico, "_open_console", lambda port: fake)
        clock = iter([0.0, 0.0, 0.0, 0.0, 99.0, 99.0])
        monkeypatch.setattr(pico.time, "monotonic", lambda: next(clock))
        assert list(pico.console("/dev/ttyACM0", 5)) == [
            "PAIRING CODE:  gb-1",
            "WIFI_OK 10.0.0.9",
        ]
        assert fake.closed

    def test_the_console_opens_at_115200_with_dtr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[Any] = []

        class Serial:
            def open(self) -> None:
                made.append(self)

        monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=Serial))
        ser = pico._open_console("/dev/ttyACM0")
        assert made == [ser]
        assert (ser.port, ser.baudrate, ser.dtr) == ("/dev/ttyACM0", 115200, True)
        assert pico.BAUD != 1200  # 1200 baud reboots an rp2 into its bootloader

    def test_the_real_device_lists_filter_by_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            pico.ports, "scan_boot", lambda: [BOOT, {**BOOT, "pid": "0003"}]
        )
        monkeypatch.setattr(
            pico.ports, "scan", lambda: [PICO, PANEL, _port("/dev/x", "2e8a", "000a")]
        )
        assert pico.boot_devices() == [BOOT]
        assert pico.pico_ttys() == ["/dev/ttyACM0"]


# -- the routes ------------------------------------------------------------------------


@pytest.fixture
def routes(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[int, list[tuple[str, Any]]]]:
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(server.ports, "scan", lambda: [PICO, PANEL])
    monkeypatch.setattr(server.ports, "scan_boot", lambda: [BOOT])

    def load(port: Any, uf2: bytes, sha: str) -> Iterator[str]:
        seen.append(("load", (port, uf2, sha)))
        yield "picotool"
        yield "OK port=/dev/ttyACM1"  # console text that only looks like a mark
        yield pico.Mark("OK port=/dev/ttyACM0")

    def provision(port: str, files: Any, secrets: bytes, watch: int) -> Iterator[str]:
        seen.append(("provision", (port, files, secrets, watch)))
        yield pico.Mark("copied")
        raise pico.PicoError("wifi_failed", "it could not join any of its networks")

    def rekey(port: str, key: str) -> Iterator[str]:
        seen.append(("rekey", (port, key)))
        yield pico.Mark("key written")
        raise RuntimeError("boom")

    def identify(port: str) -> dict[str, str]:
        seen.append(("identify", port))
        if port == "/dev/ttyACM0":
            return {"code": "gb-2a3f4e", "micropython": "1.28.0", "machine": "RP2350"}
        raise pico.PicoError("usb_gone", "no answer")

    monkeypatch.setattr(server.pico, "load", load)
    monkeypatch.setattr(server.pico, "provision", provision)
    monkeypatch.setattr(server.pico, "rekey", rekey)
    monkeypatch.setattr(server.pico, "identify", identify)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], seen
    httpd.shutdown()


def _post(port: int, path: str, body: Any) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    conn.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def _files() -> list[dict[str, str]]:
    return [{"name": "main.py", "b64": base64.b64encode(b"# main").decode()}]


class TestRoutes:
    def test_ports_lists_the_bootloaders_too(self, routes: tuple[int, Any]) -> None:
        conn = http_client(routes[0])
        conn.request("GET", "/ports")
        body = json.loads(conn.getresponse().read())
        assert body["pico_boot"] == [BOOT]
        assert [p["is_pico"] for p in body["ports"]] == [True, False]

    def test_load_streams_to_ok(self, routes: tuple[int, Any]) -> None:
        body = {"port": None, "uf2_b64": base64.b64encode(UF2).decode(), "sha256": SHA}
        status, text = _post(routes[0], "/pico/load", body)
        assert status == 200
        # Marks are `@ `, every other line `| `: console text never reads as one.
        assert text.splitlines() == [
            "| picotool",
            "| OK port=/dev/ttyACM1",
            "@ OK port=/dev/ttyACM0",
        ]
        assert routes[1] == [("load", (None, UF2, SHA))]
        status, _ = _post(routes[0], "/pico/load", {**body, "port": "/dev/ttyACM0"})
        assert status == 200 and routes[1][-1][1][0] == "/dev/ttyACM0"

    @pytest.mark.parametrize(
        "change",
        [
            {"port": "/dev/ttyACM1"},
            {"port": "/dev/sda"},
            {"sha256": "ABC"},
            {"sha256": SHA.upper()},
            {"uf2_b64": ""},
            {"uf2_b64": "%%%"},
        ],
    )
    def test_load_refuses_before_streaming(
        self, routes: tuple[int, Any], change: dict[str, Any]
    ) -> None:
        body = {
            "port": None,
            "uf2_b64": base64.b64encode(UF2).decode(),
            "sha256": SHA,
            **change,
        }
        status, text = _post(routes[0], "/pico/load", body)
        assert status == 400 and json.loads(text)["error"]
        assert routes[1] == []

    def test_identify_answers_json(
        self, routes: tuple[int, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status, text = _post(routes[0], "/pico/identify", {"port": "/dev/ttyACM0"})
        assert status == 200 and json.loads(text)["code"] == "gb-2a3f4e"
        status, _ = _post(routes[0], "/pico/identify", {"port": "/dev/ttyACM1"})
        assert status == 400
        other = _port("/dev/ttyACM5", "2e8a", "0005")
        monkeypatch.setattr(server.ports, "scan", lambda: [PICO, other])
        status, text = _post(routes[0], "/pico/identify", {"port": "/dev/ttyACM5"})
        assert status == 502 and json.loads(text)["code"] == "usb_gone"

    def test_provision_ends_with_the_failure_code(
        self, routes: tuple[int, Any]
    ) -> None:
        body = {
            "port": "/dev/ttyACM0",
            "files": _files(),
            "secrets_b64": base64.b64encode(SECRETS).decode(),
            "watch_s": 30,
        }
        status, text = _post(routes[0], "/pico/provision", body)
        assert status == 200
        assert text.splitlines() == [
            "@ copied",
            "@ FAILED: wifi_failed: it could not join any of its networks",
        ]
        assert routes[1][0][1][3] == 30

    @pytest.mark.parametrize(
        "change",
        [
            {"watch_s": 0},
            {"watch_s": 121},
            {"watch_s": True},
            {"watch_s": "60"},
            {"files": [{"name": "../x.py", "b64": ""}]},
            {"secrets_b64": ""},
            {"port": "/dev/ttyACM1"},
        ],
    )
    def test_provision_refuses_before_streaming(
        self, routes: tuple[int, Any], change: dict[str, Any]
    ) -> None:
        body = {
            "port": "/dev/ttyACM0",
            "files": _files(),
            "secrets_b64": base64.b64encode(SECRETS).decode(),
            **change,
        }
        assert _post(routes[0], "/pico/provision", body)[0] == 400
        assert routes[1] == []

    def test_rekey_checks_the_key_and_streams_any_crash_as_internal(
        self, routes: tuple[int, Any]
    ) -> None:
        for bad in ("", "short", "x" * 65, "a;b" * 12):
            assert (
                _post(routes[0], "/pico/rekey", {"port": "/dev/ttyACM0", "key": bad})[0]
                == 400
            )
        assert (
            _post(routes[0], "/pico/rekey", {"port": "/dev/ttyACM1", "key": KEY})[0]
            == 400
        )
        assert routes[1] == []
        status, text = _post(
            routes[0], "/pico/rekey", {"port": "/dev/ttyACM0", "key": KEY}
        )
        assert (
            status == 200
            and text.splitlines()[-1] == "@ FAILED: internal: RuntimeError: boom"
        )

    def test_bad_bodies_and_unknown_routes(self, routes: tuple[int, Any]) -> None:
        assert _post(routes[0], "/pico/erase", {})[0] == 404
        assert _post(routes[0], "/pico/load", b"[1]")[0] == 400
        assert _post(routes[0], "/pico/load", b"not json")[0] == 400
        conn = http_client(routes[0])
        conn.request("POST", "/pico/load", body=b"", headers={"Content-Length": "0"})
        assert conn.getresponse().status == 413


def http_client(port: int) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


# -- the image and the compose service -------------------------------------------------


def test_the_image_carries_the_pico_tools_and_checks_them() -> None:
    dockerfile = (DEPLOY / "Dockerfile.endpoint").read_text()
    assert "COPY deploy/endpoint/pico.py ./pico.py" in dockerfile
    assert '"mpremote==1.28.0"' in dockerfile
    assert "picotool" in dockerfile and "FROM python:3.12-slim-trixie" in dockerfile
    assert "import server, ports, flash, monitor, pico" in dockerfile
    assert "mpremote --help" in dockerfile
    assert 'picotool version | grep -q "^picotool v2\\."' in dockerfile


def test_the_endpoint_service_can_reach_a_bootloader_and_nothing_more() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    endpoint = compose["services"]["endpoint"]
    assert "c 189:* rmw" in endpoint["device_cgroup_rules"]
    assert endpoint["networks"] == ["panel"] and not endpoint.get("privileged")
    assert (
        "./src/growbot/firmware:/growbot-firmware:ro"
        in compose["services"]["api"]["volumes"]
    )
    with contextlib.suppress(KeyError):
        assert compose["networks"]["panel"]["internal"] is True


def test_a_stream_abandoned_mid_way_kills_its_tool(tmp_path: Path) -> None:
    code = (
        "import time\nfor i in range(100):\n"
        "    print(i, flush=True)\n    time.sleep(0.1)"
    )
    lines = pico.run([sys.executable, "-c", code], str(tmp_path))
    assert next(lines) == "0"
    lines.close()


def test_bootloader_output_is_passed_through(board: Board) -> None:
    board.boot = []

    def bootloader(argv: list[str], workdir: str, timeout: float = 0) -> Iterator[str]:
        board.calls.append(argv)
        board.boot = [BOOT]
        yield "Traceback: device disconnected"
        yield "exit 1"

    runs = iter([bootloader, board.run])
    pico.run = lambda *a, **k: next(runs)(*a, **k)  # type: ignore[assignment]
    lines, err = _drain(pico.load("/dev/ttyACM0", UF2, SHA))
    assert err is None and "Traceback: device disconnected" in lines


def test_a_watch_past_its_deadline_stops(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    board.console = ["booting", "still booting"]
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    monkeypatch.setattr(pico.time, "monotonic", lambda: next(clock))
    lines, err = _drain(pico.watch_join("/dev/ttyACM0", 5))
    assert err.code == "timeout" and lines == ["booting"]
