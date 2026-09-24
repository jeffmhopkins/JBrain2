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


async def retire_replaced(
    repo: DeviceRepo, ctx: SessionContext, *, label: str, device_role: str, keep_id: str
) -> int:
    """Revoke the keys a just-flashed unit replaced, and say how many.

    A panel's identity across a re-flash is its NAME — the physical unit carries nothing else
    the box can recognise, since the flash is what mints its credential in the first place. So
    "the same panel" is: the same label, the same role, and not the identity just issued. That
    is narrow on purpose. Matching on label alone would let a display named `Jeff` retire a pet
    named `Jeff`, which is the cross-role reach `device_role` exists to prevent.

    Returns the count rather than nothing so the flash log can tell the owner what it cleaned
    up. Twelve retired keys is worth a line; zero is the normal first flash of a new unit.
    """
    if not _valid_id(keep_id):
        return 0
    return await repo.retire_replaced(ctx, label=label, device_role=device_role, keep_id=keep_id)


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
