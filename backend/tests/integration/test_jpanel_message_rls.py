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

import uuid
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


# ---------------------------------------------------------------------------------------------
# Addressing: who "the other panel" is, and why the panel is not allowed to work it out.
# ---------------------------------------------------------------------------------------------


async def _make_panel(maker: async_sessionmaker, label: str, age_s: int = 0) -> str:
    """A device_key principal exactly as `/flash` mints one, returning its id.

    `created_at` is set rather than defaulted: these tests turn on WHICH key is newest, and
    three inserts a few milliseconds apart is not a margin to rest an assertion on."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, SessionContext(auth_context="bootstrap")) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.principals (id, kind, key_hash, label, created_at)
                VALUES (CAST(:id AS uuid), 'device_key', :kh, :label,
                        now() - make_interval(secs => :age))
                """
            ),
            {"id": pid, "kh": f"hash-{pid}", "label": label, "age": age_s},
        )
        await s.commit()
    return pid


async def test_a_panel_cannot_read_the_roster_itself(maker: async_sessionmaker) -> None:
    """THE BUG THAT MADE PANEL-TO-PANEL POST IMPOSSIBLE ON EVERY BOX, FROM THE FIRST COMMIT.

    `principals_select` opens for the owner, for `auth_ctx()` in ('login','bootstrap'), and for
    a principal reading ITS OWN ROW. Nothing else. `send` resolved "the other panel" inside a
    session scoped to the asking panel, so the roster it read contained exactly one row —
    itself — `others` was always empty, and every sibling message answered 409.

    It is asserted here rather than fixed by widening the policy, because the narrowness is
    correct: a device key on a bedroom wall must not be able to enumerate principals. This test
    is what stops a future "fix" from opening it."""
    mine = await _make_panel(maker, f"panel Alpha{uuid.uuid4().hex[:6]}")
    other = await _make_panel(maker, f"panel Beta{uuid.uuid4().hex[:6]}")
    async with scoped_session(
        maker, SessionContext(principal_id=mine, principal_kind="device_key")
    ) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text FROM app.principals "
                    "WHERE kind = 'device_key' AND revoked_at IS NULL"
                )
            )
        ).all()
    assert [r[0] for r in rows] == [mine], (
        "a panel must see only itself in the principals table — if this widens, a device key "
        "on a wall can enumerate every principal on the box"
    )

    # AND THE OTHER HALF, WHICH IS WHAT MAKES THE FIX LOAD-BEARING: the box CAN see both,
    # through `_panel_names`'s own narrow addressing context. If someone reverts that to take
    # the caller's session, this assertion is the one that fails.
    from jbrain.api.jpanel import _panel_names

    roster = await _panel_names(maker)
    assert mine in roster and other in roster, (
        "the box must be able to resolve the roster even though the panel cannot; "
        f"saw {len(roster)} panels"
    )


async def test_the_roster_collapses_the_keys_a_reflash_leaves_behind(
    maker: async_sessionmaker,
) -> None:
    """ONE ROW PER NAME, NEWEST KEY WINS.

    Every `/flash` mints a fresh device key and nothing retires the old one. The live box had
    thirteen unrevoked principals labelled "panel Elora" — one physical panel, re-flashed — and
    two more unnamed, so `send(to="panel")` saw fifteen candidates where it needs exactly one.
    Even with the RLS fix above, that alone would have kept the feature at 409 forever.

    The newest key for a name is the one that panel is using, because `/flash` rewrites its
    NVS; every older one is dead by construction."""
    from jbrain.api.jpanel import _panel_names

    name = f"panel Reflash{uuid.uuid4().hex[:6]}"
    # Oldest first, so `ids[-1]` is genuinely the newest key for this name.
    ids = [await _make_panel(maker, name, age_s=age) for age in (300, 200, 100)]

    names = await _panel_names(maker)
    mine = [pid for pid in ids if pid in names]
    assert len(mine) == 1, f"three keys for one name must collapse to one, got {len(mine)}"
    assert mine[0] == ids[-1], "and it must be the newest, which is the key the panel now runs"


async def test_a_panel_on_a_superseded_key_does_not_address_itself(
    maker: async_sessionmaker,
) -> None:
    """The second half of the same problem, and the reason `send` filters by NAME not by id.

    A panel that has not been re-flashed since a newer key was minted for its own name is not
    in the roster under its own id. Filtering `pid != principal.id` would therefore leave its
    OWN name in the candidate list and post the child's message straight back to the unit they
    spoke into — which, with exactly two names present, is not a 409 but a wrong delivery.

    This asserts the roster's shape that makes the by-name filter work: the surviving row for a
    name is not the id the older panel is running as."""
    from jbrain.api.jpanel import _display_name, _panel_names

    name = f"panel Stale{uuid.uuid4().hex[:6]}"
    old_key = await _make_panel(maker, name, age_s=300)
    new_key = await _make_panel(maker, name, age_s=100)
    assert old_key != new_key

    names = await _panel_names(maker)
    assert old_key not in names, "the superseded key is not in the roster"
    assert names.get(new_key) == _display_name(name)

    # What `send` does: exclude by the asking panel's own NAME.
    me = _display_name(name)
    assert new_key not in [pid for pid, n in names.items() if n != me], (
        "filtering by name must exclude the panel's own newer key; filtering by id would not"
    )


async def test_dads_recording_reaches_the_panel_it_was_addressed_to(
    maker: async_sessionmaker,
) -> None:
    """THE NEW HALF: the owner may now send his ACTUAL VOICE, not only text to be read out.

    `JPANEL_PLAN.md` §3b made the asymmetry binding — the PWA composed text and nothing else.
    The owner amended it: *"PWA should also be able to actually send audio, a voice message,
    that have the option to send text that gets rendered."* The reason is the one `DAD_VOICE`
    already exists for: a synthesised voice reading a father's words is not his voice, and for
    a child who cannot read it is the only thing that carries who the message is from.

    What has to hold in Postgres is that an owner-composed VOICE row is a first-class message:
    accepted by the schema's `composed` check, delivered to the panel it names, and — the part
    that matters in a bedroom — invisible to that panel's sibling. A recording of a parent is
    not less private than a recording of a child."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, recipient_kind, recipient_device, blob_sha256,
                     transcript, composed, duration_ms)
                VALUES ('owner', 'panel', 'panel-one', 'sha-dad-voice',
                        'five more minutes then teeth', 'voice', 2400)
                """
            )
        )
        await s.commit()

    async with scoped_session(maker, ONE) as s:
        row = (
            await s.execute(
                text(
                    "SELECT sender_kind, composed FROM app.jpanel_message WHERE blob_sha256 = :sha"
                ),
                {"sha": "sha-dad-voice"},
            )
        ).first()
    assert row is not None, "the panel it was addressed to must be able to play it"
    assert row[0] == "owner"
    assert row[1] == "voice", "an owner may compose by voice, not only by text"

    async with scoped_session(maker, TWO) as s:
        seen = (
            await s.execute(
                text("SELECT count(*) FROM app.jpanel_message WHERE blob_sha256 = :sha"),
                {"sha": "sha-dad-voice"},
            )
        ).scalar_one()
    assert seen == 0, "the sibling must not hear a recording addressed to the other twin"


async def test_clearing_a_history_keeps_what_a_child_has_not_heard(
    maker: async_sessionmaker,
) -> None:
    """THE CARVE-OUT, AND IT IS THE POINT OF THE ROUTE RATHER THAN A DETAIL.

    `JPANEL_PLAN.md` §5: *"unplayed messages are kept indefinitely — a message nobody heard is
    the one thing that must not evaporate."* A row addressed to a panel with `played_at IS NULL`
    is sitting on a bedroom wall waiting for a four-year-old to come back to it. The owner
    tidying his own view is not a decision about her post, so the delete steps around it and
    reports how many it left.

    A panel's unread message to the OWNER is a different thing and goes: that is his own badge,
    he is looking at the thread, and clearing is exactly the call he is making."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (sender_kind, sender_device, recipient_kind, recipient_device,
                     blob_sha256, composed, played_at)
                VALUES
                    -- from the panel to Dad, never opened: HIS badge, his call
                    ('panel', 'panel-one', 'owner', NULL, 'clr-unread-to-dad', 'voice', NULL),
                    -- from the panel to Dad, opened
                    ('panel', 'panel-one', 'owner', NULL, 'clr-read-to-dad', 'voice', now()),
                    -- from Dad to the panel, already heard
                    ('owner', NULL, 'panel', 'panel-one', 'clr-heard', 'text', now()),
                    -- from Dad to the panel, NOT heard: must survive
                    ('owner', NULL, 'panel', 'panel-one', 'clr-waiting', 'text', NULL)
                """
            )
        )
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    """
                    WITH mine AS (
                        SELECT id, recipient_kind, played_at
                        FROM app.jpanel_message
                        WHERE (sender_kind = 'panel' AND sender_device = :dev)
                           OR (sender_kind = 'owner' AND recipient_device = :dev)
                    ), gone AS (
                        DELETE FROM app.jpanel_message
                        WHERE id IN (
                            SELECT id FROM mine
                            WHERE NOT (recipient_kind = 'panel' AND played_at IS NULL)
                        )
                        RETURNING 1
                    )
                    SELECT (SELECT count(*) FROM gone),
                           (SELECT count(*) FROM mine
                            WHERE recipient_kind = 'panel' AND played_at IS NULL)
                    """
                ),
                {"dev": "panel-one"},
            )
        ).first()
        await s.commit()
    assert row is not None
    assert row[0] == 3, "everything but the message still waiting to be heard"
    assert row[1] == 1, "and the count of what survived, so the PWA can say so"

    async with scoped_session(maker, OWNER) as s:
        left = [
            r[0]
            for r in (await s.execute(text("SELECT blob_sha256 FROM app.jpanel_message"))).all()
        ]
    assert left == ["clr-waiting"], f"only the unheard message may survive, got {left}"
