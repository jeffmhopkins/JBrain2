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
from jbrain.devices.repo import DeviceInfo, DeviceRepo


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


async def provision_device(repo: DeviceRepo, ctx: SessionContext, label: str) -> ProvisionedDevice:
    key = keys.generate_owner_key()
    device = await repo.provision(ctx, label=label, key_hash=keys.hash_key(key))
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
