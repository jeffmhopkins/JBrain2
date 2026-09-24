"""Owner-only device provisioning flows.

Plaintext keys are generated here and returned exactly once; only their hash is
stored (mirroring `auth.service.rotate_owner_key`). A device key is the same
256-bit primitive as an owner key, so OwnTracks can carry it as an HTTP Basic
password and the auth path hashes it with the same `keys.hash_key`.
"""

import uuid
from dataclasses import dataclass

from jbrain.auth import keys
from jbrain.db.session import SessionContext
from jbrain.devices.repo import DeviceInfo, DeviceRepo, DeviceRole


@dataclass(frozen=True)
class ProvisionedDevice:
    device: DeviceInfo
    key: str  # shown to the owner once, never stored in plaintext


def _valid_id(device_id: str) -> bool:
    try:
        uuid.UUID(device_id)
    except ValueError:
        return False
    return True


async def provision_device(
    repo: DeviceRepo, ctx: SessionContext, label: str, *, device_role: DeviceRole | None = None
) -> ProvisionedDevice:
    key = keys.generate_owner_key()
    device = await repo.provision(
        ctx, label=label, key_hash=keys.hash_key(key), device_role=device_role
    )
    return ProvisionedDevice(device=device, key=key)


async def provision_or_reflash(
    repo: DeviceRepo, ctx: SessionContext, label: str, *, device_role: DeviceRole
) -> ProvisionedDevice:
    """The flash's identity step: re-credential this panel if the box already knows it, else
    enrol it as a new one. Either way the caller gets one plaintext key, exactly once.

    A PANEL IS A THING, NOT A KEY. Re-flashing used to mint a whole new identity every time,
    because the box had no way to recognise the board in front of it — so one panel became
    thirteen subjects with thirteen live keys, the owner's device list became thirteen identical
    rows, and there was nowhere durable to record what that panel was called or what body it
    wore. Matching on name and role is that recognition, and `reflash` says why it is narrow.

    Enrolling is still what happens for a name the box has never seen, which is the first flash
    of a new unit and the only case that SHOULD mint an identity.
    """
    key = keys.generate_owner_key()
    device = await repo.reflash(
        ctx, label=label, device_role=device_role, key_hash=keys.hash_key(key)
    )
    if device is None:
        device = await repo.provision(
            ctx, label=label, key_hash=keys.hash_key(key), device_role=device_role
        )
    return ProvisionedDevice(device=device, key=key)


async def rotate_device_key(repo: DeviceRepo, ctx: SessionContext, device_id: str) -> str | None:
    """Issue a new key for an existing device (revoking the old); None if unknown."""
    if not _valid_id(device_id):
        return None
    key = keys.generate_owner_key()
    rotated = await repo.rotate(ctx, device_id, keys.hash_key(key))
    return key if rotated else None


async def revoke_device(repo: DeviceRepo, ctx: SessionContext, device_id: str) -> bool:
    if not _valid_id(device_id):
        return False
    return await repo.revoke(ctx, device_id)


async def rename_device(repo: DeviceRepo, ctx: SessionContext, device_id: str, label: str) -> bool:
    if not _valid_id(device_id):
        return False
    return await repo.rename(ctx, device_id, label)


async def delete_device(repo: DeviceRepo, ctx: SessionContext, device_id: str) -> bool:
    if not _valid_id(device_id):
        return False
    return await repo.delete(ctx, device_id)
