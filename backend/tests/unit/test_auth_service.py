import pytest

from jbrain.auth import keys, service
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


@pytest.mark.parametrize("kind", ["device_key", "capability_token", "jcode_share_link"])
async def test_login_accepts_only_the_owner_key(kind: str) -> None:
    # The login form mints a session for the owner alone; another key hashed the same way
    # (a device key is) must not mint one for its own principal.
    repo = FakeAuthRepo()
    await repo.create_principal(kind, keys.hash_key("jb1-other"), "other")
    with pytest.raises(service.InvalidCredentials):
        await service.login(repo, "jb1-other", "pixel")
    assert repo.sessions == []


@pytest.mark.parametrize("kind", ["capability_token", "external_llm"])
async def test_a_session_for_a_kind_that_never_holds_one_is_refused(kind: str) -> None:
    # Such a session can only have been minted by the old unfiltered login; it must not
    # keep authenticating now that login can't mint it.
    repo = FakeAuthRepo()
    await repo.create_principal(kind, "kh", "leftover")
    await repo.create_session(repo.principals[0].id, keys.hash_token("tok"), "old")
    assert await service.authenticate(repo, "tok") is None


@pytest.mark.parametrize("kind", ["owner", "device_key", "jcode_share_link", "intake_link"])
async def test_the_session_holding_kinds_still_authenticate(kind: str) -> None:
    repo = FakeAuthRepo()
    await repo.create_principal(kind, "kh", "holder")
    await repo.create_session(repo.principals[0].id, keys.hash_token("tok"), "s")
    principal = await service.authenticate(repo, "tok")
    assert principal is not None and principal.kind == kind
