"""The owner debug SQL console must not hand back plaintext secrets
(docs/plans/JMOLT_HARDENING_PLAN.md, H1 — G17).

The console is read-only, and read-only is not the same as confidential. `app.settings` is
one jsonb row per key and holds the Moltbook bearer key and the Gmail client secret in
plaintext, so `SELECT * FROM app.settings` returned them to anyone holding a debug token —
a token the owner hands out to get help looking at a live box, which should not double as a
credential dump. B9 stops jmolt reading those rows; this stops the console.
"""

from __future__ import annotations

from jbrain.api.debug import _REDACTED, _redact_row


def test_a_settings_row_naming_a_secret_has_its_value_redacted() -> None:
    """`app.settings` is key/value, so the COLUMN name says nothing — the row's key does."""
    out = _redact_row(["key", "value"], ["moltbook_api_key", "moltbook_live_abc123"])
    assert out == ["moltbook_api_key", _REDACTED]


def test_the_key_itself_is_still_visible() -> None:
    """Redacting the key name too would make the console useless for the thing it is for:
    seeing which settings exist and which are set."""
    out = _redact_row(["key", "value"], ["gmail_client_secret", "s3cret"])
    assert out[0] == "gmail_client_secret"


def test_ordinary_settings_are_untouched() -> None:
    for key, value in (
        ("moltbook_autonomy", True),
        ("moltbook_handle", "DaveFromSpace"),
        ("wiki_build_daily_tokens", 40000),
        ("llm_local_image_min_tokens", 256),
    ):
        assert _redact_row(["key", "value"], [key, value]) == [key, value]


def test_a_column_named_for_a_secret_is_redacted() -> None:
    """The other shape: a dedicated column somewhere else in the schema."""
    out = _redact_row(["id", "api_key", "created_at"], ["1", "sk_live_xyz", "2026-01-01"])
    assert out == ["1", _REDACTED, "2026-01-01"]


def test_a_plural_tokens_counter_is_not_mistaken_for_a_token() -> None:
    """`*_tokens` is this schema's counter idiom (`wiki_build_daily_tokens`); `*_token`
    singular is a credential (`gmail_refresh_token`). The distinction is load-bearing."""
    assert _redact_row(["daily_tokens"], [40000]) == [40000]
    assert _redact_row(["refresh_token"], ["1//abc"]) == [_REDACTED]


def test_a_secret_shape_in_an_unnamed_column_is_still_scrubbed() -> None:
    """Belt and braces — an error string or a joined view that nothing above names."""
    out = _redact_row(["detail"], ["auth failed for moltbook_live_deadbeefcafe"])
    assert "moltbook_live_deadbeefcafe" not in out[0]
    assert "[redacted]" in out[0]
