"""The `deploy/endpoint` panel flasher, loaded by path like every `deploy/sdr` module.

What is pinned here is the half with no device in it: which ports the container
reports, and what esptool is actually asked to run. Both matter more than they look.
The port list is the owner's ONLY way to tell "the panel is not enumerating" from
"this container cannot see it" on a box with no terminal (CLAUDE.md #10); and the
esptool argv writes a bootloader to a device that, once it is in a child's bedroom,
has no cable attached to it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str, filename: str):
    endpoint_dir = str(DEPLOY / "endpoint")
    if endpoint_dir not in sys.path:
        sys.path.insert(0, endpoint_dir)
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"endpoint/{filename}")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ports = _load("endpoint_ports", "ports.py")
flash = _load("endpoint_flash", "flash.py")


def _fake_tree(
    tmp_path: Path, name: str, vid: str, pid: str, product: str = ""
) -> tuple[str, str]:
    """A sysfs/dev pair shaped like the kernel's, so `scan` can be tested with no
    board."""
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    (dev / name).write_text("")
    # The tty's `device` link is the USB INTERFACE; the ids live on its parent.
    iface = tmp_path / "sys" / "class" / "tty" / name / "device"
    iface.mkdir(parents=True)
    usbdev = iface.parent / "usbdev"
    usbdev.mkdir()
    (usbdev / "idVendor").write_text(vid + "\n")
    (usbdev / "idProduct").write_text(pid + "\n")
    if product:
        (usbdev / "product").write_text(product + "\n")
    # `device/..` must reach the node carrying the ids, which is how the real tree sits.
    (iface / "sub").mkdir()
    for f in ("idVendor", "idProduct", "product"):
        src = usbdev / f
        if src.exists():
            (iface.parent / f).write_text(src.read_text())
    return str(tmp_path / "sys"), str(dev)


class TestPortScan:
    def test_an_espressif_port_is_recognised_and_labelled(self, tmp_path: Path) -> None:
        sysfs, dev = _fake_tree(tmp_path, "ttyACM0", "303a", "1001")
        (found,) = ports.scan(sysfs, dev)
        assert found.device.endswith("ttyACM0")
        assert found.is_espressif
        assert "ESP32-S3" in found.label

    def test_a_serial_bridge_is_named_but_not_mistaken_for_a_panel(
        self, tmp_path: Path
    ) -> None:
        sysfs, dev = _fake_tree(tmp_path, "ttyUSB0", "10c4", "ea60")
        (found,) = ports.scan(sysfs, dev)
        assert not found.is_espressif
        assert "CP210x" in found.label

    def test_the_panel_sorts_first_so_it_is_the_obvious_pick(
        self, tmp_path: Path
    ) -> None:
        sysfs, dev = _fake_tree(tmp_path, "ttyUSB0", "10c4", "ea60")
        _fake_tree(tmp_path, "ttyACM0", "303a", "1001")
        found = ports.scan(sysfs, dev)
        assert [p.is_espressif for p in found] == [True, False]

    def test_no_ports_is_an_answer_not_an_error(self, tmp_path: Path) -> None:
        """ "This container cannot see the panel" is the single most useful thing the
        flasher can report, so it must arrive as an empty list rather than an
        exception."""
        (tmp_path / "dev").mkdir()
        assert ports.scan(str(tmp_path / "sys"), str(tmp_path / "dev")) == []

    def test_a_missing_dev_directory_does_not_raise(self, tmp_path: Path) -> None:
        assert ports.scan(str(tmp_path / "nope"), str(tmp_path / "nope")) == []

    def test_a_port_with_no_usb_ids_is_still_listed(self, tmp_path: Path) -> None:
        """A device the walk cannot attribute is still a port the owner may want to try
        —
        dropping it would hide the very thing they plugged in."""
        dev = tmp_path / "dev"
        dev.mkdir()
        (dev / "ttyACM0").write_text("")
        (found,) = ports.scan(str(tmp_path / "sys"), str(dev))
        assert found.vid == "" and not found.is_espressif
        assert found.label == "unknown serial device"


class TestNvsCsv:
    def test_every_value_is_a_file_reference(self) -> None:
        """Inline values would mean escaping a multi-line PEM inside a CSV correctly on
        every path. File references have no such surface."""
        csv = flash.nvs_csv(["ssid", "ca"])
        assert "ssid,file,string,./ssid.val" in csv
        assert "ca,file,string,./ca.val" in csv

    def test_the_namespace_row_comes_first(self) -> None:
        """The generator assigns keys to the namespace declared above them, so a
        namespace
        row in the wrong place silently produces an image the firmware cannot read."""
        rows = [r for r in flash.nvs_csv(["ssid"]).splitlines() if r]
        assert rows[0].startswith("key,")
        assert rows[1] == "jbrain,namespace,,"

    def test_the_namespace_matches_what_the_firmware_opens(self) -> None:
        assert flash.NAMESPACE == "jbrain"


class TestEsptoolArgv:
    def test_images_are_written_at_the_offsets_given(self) -> None:
        argv = flash.esptool_argv(
            "/dev/ttyACM0", [("0x0", "boot.bin"), ("0x20000", "app.bin")]
        )
        assert argv[argv.index("0x0") + 1] == "boot.bin"
        assert argv[argv.index("0x20000") + 1] == "app.bin"

    def test_the_flash_size_matches_the_partition_table(self) -> None:
        """firmware/partitions.csv ends at exactly 16M. Writing it as a smaller chip
        truncates the table and the board loses its OTA slots."""
        argv = flash.esptool_argv("/dev/ttyACM0", [("0x0", "boot.bin")])
        assert argv[argv.index("--flash_size") + 1] == "16MB"

    def test_reset_flags_drive_the_onboard_download_circuit(self) -> None:
        """Without these the owner has to hold BOOT while powering a board they cannot
        see — which is the manual step this whole surface exists to remove."""
        argv = flash.esptool_argv("/dev/ttyACM0", [("0x0", "boot.bin")])
        assert argv[argv.index("--before") + 1] == "default_reset"
        assert argv[argv.index("--after") + 1] == "hard_reset"

    def test_no_shell_operator_ever_reaches_the_argv(self) -> None:
        """An earlier draft appended "&&" here to chain an erase. argv is not a shell:
        that ships a literal "&&" to execve. Erase is its own invocation in
        `flash()`."""
        argv = flash.esptool_argv("/dev/ttyACM0", [("0x0", "boot.bin")])
        assert "&&" not in argv
        assert "erase_flash" not in argv


class TestRecoveryNet:
    """The offsets a bricked panel's recovery depends on.

    Asserted here rather than left as prose in the runbook because the whole "a panel in
    a
    bedroom is recoverable without a cable" claim rests on them, and an offset is
    exactly
    the kind of constant that gets adjusted by someone who does not know what it
    carries.
    """

    def _table(self) -> dict[str, tuple[int, int]]:
        rows: dict[str, tuple[int, int]] = {}
        for line in (
            (DEPLOY.parent / "firmware/partitions.csv").read_text().splitlines()
        ):
            if line.startswith("#") or not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                rows[parts[0]] = (int(parts[3], 16), int(parts[4], 16))
        return rows

    def test_the_app_is_flashed_into_the_factory_partition(self) -> None:
        """Rung 2 of the recovery ladder is "invalid otadata falls through to factory",
        and
        that only helps if a USB flash actually put something bootable there. It also
        means
        the day a GROWN app is flashed over USB, factory stops being a safety net and
        becomes a second copy of whatever is broken — see ENDPOINT_RECOVERY.md."""
        api = (DEPLOY.parent / "backend/src/jbrain/api/endpoint.py").read_text()
        factory_offset = self._table()["factory"][0]
        assert f'APP_OFFSET = "{hex(factory_offset)}"' in api, (
            "the flasher must write the app where the bootloader falls back to"
        )

    def test_otadata_is_where_the_flasher_writes_it(self) -> None:
        """A mismatch here corrupts the one partition whose loss costs a cable: the
        bootloader would read which slot to boot from the wrong place."""
        api = (DEPLOY.parent / "backend/src/jbrain/api/endpoint.py").read_text()
        assert f'OTA_DATA_OFFSET = "{hex(self._table()["otadata"][0])}"' in api

    def test_the_nvs_the_flasher_writes_matches_the_table(self) -> None:
        """A generated NVS image larger than its partition overwrites otadata, which is
        the
        single most expensive thing to corrupt on this layout."""
        api = (DEPLOY.parent / "backend/src/jbrain/api/endpoint.py").read_text()
        offset, size = self._table()["nvs"]
        assert f'NVS_OFFSET = "{hex(offset)}"' in api
        assert f'NVS_SIZE = "{hex(size)}"' in api

    def test_two_full_size_ota_slots_survive(self) -> None:
        """Rung 1 is rollback, and rollback needs somewhere to roll back TO. Trading a
        slot
        away for a bigger asset partition would quietly remove it."""
        table = self._table()
        assert table["ota_0"][1] == table["ota_1"][1]
        assert table["ota_0"][1] >= 4 * 1024 * 1024
