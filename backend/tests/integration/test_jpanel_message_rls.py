"""Migration 0208 against real Postgres: one twin cannot reach the other twin's post.

THE POINT OF THIS FILE IS THE SIBLING CASE. Owner-versus-panel is the easy half and the half
every RLS test already covers somewhere. The isolation that actually matters here is
panel-versus-panel: these rows are recordings of small children in their bedrooms, made on a
device that lives on a wall and authenticates with a key a four-year-old could hand to a
visitor. A panel that can read its sibling's inbox is a baby monitor pointed the wrong way, and
a panel that can WRITE as its sibling is worse — on a device aimed at four-year-olds, a forged
message from your sister beats eavesdropping for harm.

Asserted in Postgres under the exact context a flashed panel runs as, not inferred from the
shape of the handlers, because "the route filters by recipient" is a code-review convention
rather than a mechanism (CLAUDE.md rule 3, and `0178_settings_deny_jmolt` for the argument).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The two panels, exactly as a flashed unit runs: a device key, no domain scopes, not owner.
ONE = SessionContext(principal_id="panel-one", principal_kind="device_key")
TWO = SessionContext(principal_id="panel-two", principal_kind="device_key")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _send(maker: async_sessionmaker, ctx: SessionContext, to: str, sha: str) -> None:
    """A panel posting to another panel, through its own scoped session."""
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, sender_device, recipient_kind, recipient_device,
                     blob_sha256, composed)
                VALUES ('panel', :me, 'panel', :to, :sha, 'voice')
                """
            ),
            {"me": ctx.principal_id, "to": to, "sha": sha},
        )
        await s.commit()


async def test_a_panel_can_post_to_its_sibling(maker: async_sessionmaker) -> None:
    """The feature itself: if this fails nothing else here means anything."""
    await _send(maker, ONE, to="panel-two", sha="sha-can-post")
    async with scoped_session(maker, TWO) as s:
        row = (
            await s.execute(
                text("SELECT sender_device FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-can-post"},
            )
        ).first()
    assert row is not None, "a panel must be able to read post addressed to it"
    assert row[0] == "panel-one"


async def test_a_panel_cannot_read_post_between_other_people(maker: async_sessionmaker) -> None:
    """THE ONE THIS FILE EXISTS FOR. A message from the owner to twin two is not twin one's."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, recipient_kind, recipient_device, blob_sha256, composed)
                VALUES ('owner', 'panel', 'panel-two', :sha, 'text')
                """
            ),
            {"sha": "sha-not-yours"},
        )
        await s.commit()

    async with scoped_session(maker, ONE) as s:
        seen = (
            await s.execute(
                text("SELECT count(*) FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-not-yours"},
            )
        ).scalar_one()
    assert seen == 0, "a panel read a message addressed to its sibling"

    # And the addressee still can, so the policy is isolating rather than simply denying.
    async with scoped_session(maker, TWO) as s:
        seen = (
            await s.execute(
                text("SELECT count(*) FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-not-yours"},
            )
        ).scalar_one()
    assert seen == 1, "the addressee could not read its own message"


async def test_a_panel_cannot_post_as_its_sibling(maker: async_sessionmaker) -> None:
    """Forging a sender is worse than reading one. A message that appears to come from your
    sister, on a device aimed at a four-year-old, is the harm this table is most exposed to."""
    with pytest.raises(Exception) as caught:
        async with scoped_session(maker, ONE) as s:
            await s.execute(
                text(
                    """
                    INSERT INTO app.jpanel_message
                        (sender_kind, sender_device, recipient_kind, recipient_device,
                         blob_sha256, composed)
                    VALUES ('panel', 'panel-two', 'panel', 'panel-two', :sha, 'voice')
                    """
                ),
                {"sha": "sha-forged"},
            )
            await s.commit()
    # A WITH CHECK violation raises rather than silently dropping the row, which is the
    # behaviour we want: a refused send must not look like a delivered one.
    assert "policy" in str(caught.value).lower()


async def test_a_panel_cannot_mark_its_siblings_message_played(
    maker: async_sessionmaker,
) -> None:
    """Reaching into another inbox to mark something heard would hide a message from the child
    it was for — the one failure this whole design says must never happen silently."""
    await _send(maker, ONE, to="panel-two", sha="sha-still-waiting")

    async with scoped_session(maker, ONE) as s:
        await s.execute(
            text("UPDATE app.jpanel_message SET played_at = now() WHERE blob_sha256 = :sha"),
            {"sha": "sha-still-waiting"},
        )
        await s.commit()

    # THE ASSERTION IS ABOUT THE EFFECT, NOT AN EXCEPTION. An UPDATE that no policy admits
    # matches zero rows and reports success; expecting a raise here would pass a write that had
    # in fact gone through, which is how the endpoint_settings version of this test was wrong
    # once already.
    async with scoped_session(maker, TWO) as s:
        played = (
            await s.execute(
                text("SELECT played_at FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-still-waiting"},
            )
        ).scalar_one()
    assert played is None, "a panel marked its sibling's message as played"


async def test_the_addressee_can_mark_its_own_message_played(
    maker: async_sessionmaker,
) -> None:
    """The other half of the same policy: bounding the UPDATE must not break the inbox."""
    await _send(maker, ONE, to="panel-two", sha="sha-heard")
    async with scoped_session(maker, TWO) as s:
        await s.execute(
            text("UPDATE app.jpanel_message SET played_at = now() WHERE blob_sha256 = :sha"),
            {"sha": "sha-heard"},
        )
        await s.commit()
    async with scoped_session(maker, TWO) as s:
        played = (
            await s.execute(
                text("SELECT played_at FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-heard"},
            )
        ).scalar_one()
    assert played is not None, "the addressee could not mark its own message played"


async def test_the_owner_sees_both_twins(maker: async_sessionmaker) -> None:
    """A parent reads everything — that is the point of the PWA half — and it is worth pinning
    beside the isolation so a future tightening cannot quietly take it away."""
    await _send(maker, ONE, to="panel-two", sha="sha-owner-one")
    await _send(maker, TWO, to="panel-one", sha="sha-owner-two")
    async with scoped_session(maker, OWNER) as s:
        seen = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jpanel_message "
                    "WHERE blob_sha256 IN ('sha-owner-one', 'sha-owner-two')"
                )
            )
        ).scalar_one()
    assert seen == 2


async def test_the_owners_badge_counts_only_what_was_sent_to_him(
    maker: async_sessionmaker,
) -> None:
    """The unplayed badge, and the two bugs that made it wrong.

    Found by building the PWA against the contract: `PanelThread.unplayed` is defined as
    messages from a panel the owner has not dealt with, and the first implementation counted
    the rows the LIST query had fetched. That deflates the badge the moment `limit` truncates —
    a parent who sees "2 waiting" when four are waiting stops trusting the number, and a badge
    nobody trusts is worse than no badge.

    Underneath it was a second one the PWA could not have seen: counting everything a PANEL
    sent includes twin-to-twin post, which is not the owner's to clear. That badge would show a
    number he could never make go away.

    So the count takes both predicates — from a panel, TO the owner, unplayed — and this
    asserts each of the three ways to get it wrong."""
    from jbrain.api.jpanel import _unplayed_by_panel

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.commit()

    # Two to Dad, unplayed. These are the only ones that should count.
    async with scoped_session(maker, ONE) as s:
        for sha in ("badge-a", "badge-b"):
            await s.execute(
                text(
                    """
                    INSERT INTO app.jpanel_message
                        (sender_kind, sender_device, recipient_kind, blob_sha256, composed)
                    VALUES ('panel', :me, 'owner', :sha, 'voice')
                    """
                ),
                {"me": ONE.principal_id, "sha": sha},
            )
        await s.commit()

    # Twin-to-twin: not the owner's, and the bug that counted it left an unclearable badge.
    await _send(maker, ONE, to="panel-two", sha="badge-sibling")

    # Already dealt with: must not count.
    async with scoped_session(maker, ONE) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, sender_device, recipient_kind, blob_sha256, composed,
                     played_at)
                VALUES ('panel', :me, 'owner', 'badge-done', 'voice', now())
                """
            ),
            {"me": ONE.principal_id},
        )
        await s.commit()

    # Dad's own outgoing message is not something Dad has to read.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, recipient_kind, recipient_device, blob_sha256, composed)
                VALUES ('owner', 'panel', :to, 'badge-outgoing', 'text')
                """
            ),
            {"to": ONE.principal_id},
        )
        await s.commit()

    # A fresh session for the read: committing closes the transaction this context manager
    # holds, so counting inside it would run on a finished one.
    async with scoped_session(maker, OWNER) as s:
        counts = await _unplayed_by_panel(s)

    assert counts.get("panel-one") == 2, (
        f"the badge counted {counts.get('panel-one')} rather than the 2 messages actually "
        "waiting for the owner"
    )
    assert "panel-two" not in counts, "a twin who sent nothing to the owner has no badge"
