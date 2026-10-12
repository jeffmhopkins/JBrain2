"""The world's "Power Pack teleport" switch (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10,
transport on request): on by default, saved on the slot, told to the jbrain pack in the
same `jb:config` line as the keep-inventory charm, at every start and at once on the
loaded world. Off, the pack refuses every trip."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from tests.test_minecraft_sidecar import _running_rig, _serve, server


@pytest.fixture(autouse=True)
def _own_slots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The slot records' file is a module constant the shared rig helpers leave alone;
    # a switch turned off here must not leak into another test's world.
    monkeypatch.setattr(server, "SLOTS", tmp_path / "slots.json")


def test_on_by_default_and_applies_live_without_a_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig, sent = _running_rig(monkeypatch, tmp_path)
    assert rig.slots.get("slot1")["power_pack_transport"] is True
    assert server.pack_config(rig.slots.get("slot1")) == (
        'scriptevent jb:config {"power_pack_keep": false, "power_pack_transport": true}'
    )
    out = rig.set_power_pack_transport("slot1", False)
    assert out["power_pack_transport"] is False and out["applies"] == "now"
    # No game rule rides on it, and the charm's own switch is untouched.
    assert sent == [
        'scriptevent jb:config {"power_pack_keep": false,'
        ' "power_pack_transport": false}'
    ]
    assert not rig.slots.get("slot1").get("rules")
    sent.clear()
    out = rig.set_power_pack_transport("slot2", True)
    assert out["applies"] == "next load" and sent == []
    with pytest.raises(ValueError):
        rig.set_power_pack_transport("slot1", "off")
    with pytest.raises(ValueError):
        rig.slots.update("slot1", power_pack_transport="on")


def test_a_record_from_before_the_switch_reads_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pack treats a missing switch as on; the config line agrees for a slot
    saved by an older sidecar."""
    assert '"power_pack_transport": true' in server.pack_config({"id": "slot1"})
    assert '"power_pack_transport": true' in server.pack_config(None)
    rig, _sent = _running_rig(monkeypatch, tmp_path)
    rig.slots.update("slot2", power_pack_transport=False)
    rig._adopt("slot2", power_pack_transport=True)  # an import starts afresh
    assert rig.slots.get("slot2")["power_pack_transport"] is True


def test_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    httpd, _get = _serve(monkeypatch, "s3cret")

    def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/worlds/slot2/power-pack-transport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer s3cret"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        code, got = post({"on": False})
        assert code == 200 and got["power_pack_transport"] is False
        assert got["power_pack_keep"] is False  # the other switch is its own
        assert got["applies"] == "next load"
        assert post({"on": "yes"})[0] == 400
        assert post({})[0] == 400
    finally:
        httpd.shutdown()
