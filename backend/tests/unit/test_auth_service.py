import pytest

from jbrain.auth import service
from tests.unit.fakes import FakeAuthRepo


async def test_rotate_then_login_then_authenticate() -> None:
    repo = FakeAuthRepo()
    key = await service.rotate_owner_key(repo)

    token = await service.login(repo, key, "pixel")
    principal = await service.authenticate(repo, token)

    assert principal is not None
    assert principal.kind == "owner"
    assert repo.sessions[0].label == "pixel"


async def test_login_rejects_wrong_key() -> None:
    repo = FakeAuthRepo()
    await service.rotate_owner_key(repo)
    with pytest.raises(service.InvalidCredentials):
        await service.login(repo, "jb1-NOPE", "pixel")


async def test_rotation_revokes_previous_key_and_its_sessions() -> None:
    repo = FakeAuthRepo()
    old_key = await service.rotate_owner_key(repo)
    old_token = await service.login(repo, old_key, "old")

    new_key = await service.rotate_owner_key(repo)

    with pytest.raises(service.InvalidCredentials):
        await service.login(repo, old_key, "stolen")
    assert await service.authenticate(repo, old_token) is None
    assert await service.authenticate(repo, await service.login(repo, new_key, "new")) is not None


async def test_authenticate_empty_or_unknown_token() -> None:
    repo = FakeAuthRepo()
    assert await service.authenticate(repo, "") is None
    assert await service.authenticate(repo, "bogus") is None


async def test_logout_revokes_session() -> None:
    repo = FakeAuthRepo()
    key = await service.rotate_owner_key(repo)
    token = await service.login(repo, key, "pixel")

    await service.logout(repo, token)

    assert await service.authenticate(repo, token) is None


async def test_logout_with_empty_token_is_noop() -> None:
    await service.logout(FakeAuthRepo(), "")
