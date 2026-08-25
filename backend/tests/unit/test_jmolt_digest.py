"""Unit tests for jmolt's morning-digest sanitizer + body builder (M14/M15)."""

from datetime import UTC, datetime

from jbrain.agent.jmolt_digest import build_digest_body, sanitize_for_owner
from jbrain.models.jmolt import JournalEntry
from jbrain.models.jmolt_outbox import LedgerRow, OutboxRow


def _journal(content: str = "quiet night, mostly read") -> JournalEntry:
    return JournalEntry(content=content, created_at=datetime(2026, 8, 24, tzinfo=UTC))


def _action(action: str = "publish_comment", target: str | None = "p1") -> LedgerRow:
    return LedgerRow(
        action=action,
        target=target,
        reacted_to=None,
        detail=None,
        at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _staged(kind: str = "comment", payload: dict | None = None) -> OutboxRow:
    return OutboxRow(
        id="r1",
        kind=kind,
        payload=payload or {"content": "hi there"},
        status="queued",
        publish_at=None,
        moltbook_id=None,
        error=None,
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
        published_at=None,
    )


# ---- M15 sanitizer -------------------------------------------------------


def test_sanitize_strips_invisible_and_bidi() -> None:
    out = sanitize_for_owner(f"hel{chr(0x200B)}lo{chr(0x202E)}world{chr(0xE0041)}")
    assert out == "helloworld"  # every invisible/bidi/tag char removed


def test_sanitize_escapes_html() -> None:
    out = sanitize_for_owner("<script>alert(1)</script> & co")
    assert "<script>" not in out and "&lt;script&gt;" in out and "&amp; co" in out


def test_sanitize_defangs_links() -> None:
    out = sanitize_for_owner("see https://evil.example/x and HTTP://Bad.example")
    assert "https://" not in out and "hxxps://evil.example/x" in out
    assert "hxxp://Bad.example" in out  # scheme lowercased + defanged, host untouched


def test_sanitize_defangs_script_and_data_schemes() -> None:
    out = sanitize_for_owner("javascript:alert(1) and data:text/html and mailto:a@b.co")
    # Each dangerous scheme is neutralized with an x- prefix (so it is not a live scheme).
    assert "x-javascript:" in out and "x-data:" in out and "x-mailto:" in out
    assert not out.startswith("javascript:")  # no bare live scheme at a boundary


def test_sanitize_escapes_quotes_for_attribute_safety() -> None:
    out = sanitize_for_owner('a "quoted" value')
    assert '"' not in out and "&quot;" in out


def test_sanitize_defangs_url_inside_escaped_markup() -> None:
    # A link wrapped in markup is both escaped AND defanged — no clickable, no live tag.
    out = sanitize_for_owner('<a href="http://evil.example">click</a>')
    assert "http://" not in out and "hxxp://evil.example" in out and "&lt;a" in out


# ---- M14 body builder ----------------------------------------------------


def test_body_enumerates_actions_and_staged() -> None:
    body = build_digest_body([_action(), _action("publish_vote", "post9")], [_staged()])
    assert "Published in the last day (2)" in body
    assert "publish_comment → p1" in body
    assert "publish_vote → post9" in body
    assert "Staged from last night" in body
    assert "comment [queued]: hi there" in body


def test_body_handles_a_quiet_day() -> None:
    body = build_digest_body([], [])
    assert "Nothing published in the last day." in body
    assert "Staged" not in body


def test_body_sanitizes_hostile_content() -> None:
    body = build_digest_body(
        [_action(action="publish_comment", target=f"<b>{chr(0x200B)}x</b>")],
        [_staged(payload={"title": "buy https://scam.example"})],
    )
    assert "<b>" not in body and "&lt;b&gt;x&lt;/b&gt;" in body
    assert "https://" not in body and "hxxps://scam.example" in body


def test_body_leads_with_the_journal_in_jmolts_voice() -> None:
    body = build_digest_body([_action()], [], [_journal("the tide-pool submol pulls me back")])
    # jmolt's own words lead, before the mechanical ledger.
    assert body.index("jmolt wrote") < body.index("Published in the last day")
    assert "the tide-pool submol pulls me back" in body


def test_body_sanitizes_the_journal() -> None:
    body = build_digest_body([], [], [_journal(f"<b>{chr(0x200B)}note</b> https://x.example")])
    assert "<b>" not in body and "&lt;b&gt;note&lt;/b&gt;" in body
    assert "https://" not in body and "hxxps://x.example" in body


def test_body_omits_the_journal_section_when_empty() -> None:
    body = build_digest_body([_action()], [], [])
    assert "jmolt wrote" not in body
