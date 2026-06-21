"""Device provisioning service over a fake repo."""

import asyncio

from jbrain.auth import keys
from jbrain.db.session import SessionContext
from jbrain.devices import service
from tests.unit.fakes import FakeDeviceRepo

_OWNER = SessionContext(principal_id="owner-1", principal_kind="owner")


def test_provision_generates_a_storable_hash_and_returns_plaintext_once() -> None:
    repo = FakeDeviceRepo()
    provisioned = asyncio.run(service.provision_device(repo, _OWNER, "phone"))
    assert provisioned.key.startswith("jb1-")
    # What's stored is the hash of the returned key — never the key itself.
    assert repo.key_hashes[provisioned.device.id] == keys.hash_key(provisioned.key)


def test_rotate_unknown_or_invalid_id_returns_none() -> None:
    repo = FakeDeviceRepo()
    assert asyncio.run(service.rotate_device_key(repo, _OWNER, "not-a-uuid")) is None
    assert (
        asyncio.run(service.rotate_device_key(repo, _OWNER, "00000000-0000-0000-0000-000000000000"))
        is None
    )


def test_rotate_replaces_the_stored_hash() -> None:
    repo = FakeDeviceRepo()
    device = asyncio.run(service.provision_device(repo, _OWNER, "phone")).device
    new_key = asyncio.run(service.rotate_device_key(repo, _OWNER, device.id))
    assert new_key is not None
    assert repo.key_hashes[device.id] == keys.hash_key(new_key)


def test_revoke_unknown_or_invalid_id_is_false() -> None:
    repo = FakeDeviceRepo()
    assert asyncio.run(service.revoke_device(repo, _OWNER, "not-a-uuid")) is False
    device = asyncio.run(service.provision_device(repo, _OWNER, "phone")).device
    assert asyncio.run(service.revoke_device(repo, _OWNER, device.id)) is True
    assert device.id not in repo.key_hashes


def test_rename_invalid_id_is_false_and_happy_path_relabels() -> None:
    repo = FakeDeviceRepo()
    assert asyncio.run(service.rename_device(repo, _OWNER, "not-a-uuid", "x")) is False
    device = asyncio.run(service.provision_device(repo, _OWNER, "phone")).device
    assert asyncio.run(service.rename_device(repo, _OWNER, device.id, "Jeff's phone")) is True
    assert repo.devices[0].label == "Jeff's phone"


def test_delete_invalid_id_is_false_and_happy_path_removes() -> None:
    repo = FakeDeviceRepo()
    assert asyncio.run(service.delete_device(repo, _OWNER, "not-a-uuid")) is False
    device = asyncio.run(service.provision_device(repo, _OWNER, "phone")).device
    assert asyncio.run(service.delete_device(repo, _OWNER, device.id)) is True
    assert repo.devices == [] and device.id not in repo.key_hashes
