"""USB sysfs parsing and the /usb route."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from supervisor import usb_devices
from tests.conftest import AUTH


def _device(root: Path, name: str, **attrs: str) -> Path:
    """Write one fake /sys/bus/usb/devices/<name> device entry."""
    entry = root / name
    entry.mkdir(parents=True)
    for key, value in attrs.items():
        (entry / key).write_text(value + "\n")
    return entry


def _interface(root: Path, name: str, driver: str | None) -> Path:
    """Write a fake interface entry, optionally bound to `driver`."""
    entry = root / name
    entry.mkdir(parents=True)
    (entry / "bInterfaceNumber").write_text("00\n")
    if driver is not None:
        target = root.parent / "drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        (entry / "driver").symlink_to(target)
    return entry


def test_reads_devices_and_skips_interfaces(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(
        root,
        "1-2",
        idVendor="0bda",
        idProduct="2838",
        manufacturer="Nooelec",
        product="NESDR SMArt v5",
        serial="00000001",
        busnum="1",
        devnum="7",
    )
    _interface(root, "1-2:1.0", driver=None)

    devices = usb_devices.read_usb_devices(root)

    assert len(devices) == 1  # the interface is folded in, not reported
    dev = devices[0]
    assert dev.usb_id == "0bda:2838"
    assert dev.product == "NESDR SMArt v5"
    assert dev.serial == "00000001"
    assert dev.device_node == "/dev/bus/usb/001/007"
    assert dev.is_sdr
    assert dev.sdr_name == "NESDR SMArt v5"  # its own string wins over our table


def test_folds_interface_drivers_onto_the_device(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(root, "1-2", idVendor="0bda", idProduct="2838", busnum="1", devnum="7")
    _interface(root, "1-2:1.0", driver="dvb_usb_rtl28xxu")
    _interface(root, "1-2:1.1", driver="dvb_usb_rtl28xxu")  # same driver, reported once

    (dev,) = usb_devices.read_usb_devices(root)

    assert dev.drivers == ("dvb_usb_rtl28xxu",)


def test_unclaimed_device_reports_no_drivers(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(root, "1-2", idVendor="0bda", idProduct="2838", busnum="1", devnum="7")
    _interface(root, "1-2:1.0", driver=None)

    (dev,) = usb_devices.read_usb_devices(root)

    assert dev.drivers == ()


def test_falls_back_to_the_table_name_when_no_product_string(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(root, "1-2", idVendor="0bda", idProduct="2838", busnum="1", devnum="7")

    (dev,) = usb_devices.read_usb_devices(root)

    assert dev.product is None
    assert dev.sdr_name == "Realtek RTL2838UHIDIR (RTL-SDR)"


def test_non_sdr_devices_are_reported_but_not_flagged(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(root, "usb1", idVendor="1d6b", idProduct="0002", product="xHCI Root Hub")
    _device(root, "1-4", idVendor="046d", idProduct="c52b", product="Unifying Receiver")

    devices = usb_devices.read_usb_devices(root)

    assert len(devices) == 2
    assert not any(d.is_sdr for d in devices)
    assert usb_devices.find_sdrs(devices) == []


def test_missing_sysfs_returns_empty_rather_than_raising(tmp_path: Path) -> None:
    assert usb_devices.read_usb_devices(tmp_path / "nope") == []


def test_malformed_busnum_leaves_the_device_node_unknown(tmp_path: Path) -> None:
    root = tmp_path / "devices"
    _device(root, "1-2", idVendor="0bda", idProduct="2838", busnum="nan", devnum="7")

    (dev,) = usb_devices.read_usb_devices(root)

    assert dev.bus is None
    assert dev.device_node is None


def test_usb_route_returns_devices_and_the_sdr_subset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "devices"
    _device(
        root,
        "1-2",
        idVendor="0bda",
        idProduct="2838",
        product="NESDR SMArt v5",
        busnum="1",
        devnum="7",
    )
    _device(root, "1-4", idVendor="046d", idProduct="c52b", product="Unifying Receiver")
    real = usb_devices.scan  # capture before patching, or the lambda recurses
    monkeypatch.setattr(usb_devices, "scan", lambda: real(root))

    res = client.get("/usb", headers=AUTH)

    assert res.status_code == 200
    body = res.json()
    assert body["sysfs_readable"] is True
    assert {d["usb_id"] for d in body["devices"]} == {"0bda:2838", "046d:c52b"}
    assert [d["usb_id"] for d in body["sdrs"]] == ["0bda:2838"]
    assert body["sdrs"][0]["sdr_name"] == "NESDR SMArt v5"


def test_usb_route_says_so_when_sysfs_is_unreadable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An empty list alone would read as "no dongle"; sysfs_readable is what tells
    # the two apart.
    real = usb_devices.scan
    monkeypatch.setattr(usb_devices, "scan", lambda: real(tmp_path / "absent"))

    body = client.get("/usb", headers=AUTH).json()

    assert body["sysfs_readable"] is False
    assert body["devices"] == []


def test_usb_route_requires_the_token(client: TestClient) -> None:
    assert client.get("/usb").status_code == 401
