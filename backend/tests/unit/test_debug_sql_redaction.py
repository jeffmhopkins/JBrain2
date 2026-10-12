"""The owner debug SQL console must not hand back plaintext secrets
(docs/plans/JMOLT_HARDENING_PLAN.md, H1 — G17).

The console is read-only, and read-only is not the same as confidential. `app.settings` is
one jsonb row per key and holds the Moltbook bearer key and the Gmail client secret in
plaintext, so `SELECT * FROM app.settings` returned them to anyone holding a debug token —
a token the owner hands out to get help looking at a live box, which should not double as a
credential dump. B9 stops jmolt reading those rows; this stops the console.
"""

from __future__ import annotations

import pytest

from jbrain.api.debug import _REDACTED, _redact_row, _refused_sql


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


# --- S3.3 (docs/plans/SECURITY_REVIEW_FIXES_PLAN.md, F12): the statement guard -----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT query_to_xml('SELECT value FROM app.settings', true, false, '')",
        "SELECT Table_To_XML('app.settings', true, false, '')",
        "SELECT cursor_to_xml('c', 1, false, false, '')",
        "SELECT schema_to_xml('app', true, false, '')",
        "SELECT database_to_xml(true, false, '')",
        "SELECT query_to_xml_and_xmlschema('SELECT 1', true, false, '')",
        "SELECT set_config('app.principal_kind', 'owner', true)",
        "SELECT pg_catalog.set_config('transaction_read_only', 'off', true)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT pg_ls_dir('.')",
        "SELECT lo_get(1234)",
        "SELECT * FROM dblink('host=x', 'SELECT 1') AS t(x int)",
        "SELECT * FROM ts_stat('SELECT to_tsvector(value::text) FROM app.settings')",
        "EXPLAIN SELECT query_to_xml('SELECT 1', true, false, '')",
        "SELECT U&\"query\\005fto_xml\"('SELECT 1', true, false, '')",
    ],
)
def test_functions_that_read_around_the_redaction_are_refused(sql: str) -> None:
    assert _refused_sql(sql) is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT key, value FROM app.settings",
        "SELECT n.id, c.state FROM app.notes n JOIN app.note_conversations c ON c.note_id = n.id",
        "EXPLAIN SELECT * FROM app.notes WHERE id = '00000000-0000-0000-0000-000000000000'",
        "SHOW search_path",
        "SELECT current_setting('transaction_read_only')",
        "SELECT xmlparse(content '<a/>')",
        "SELECT 'hello_world', slot_count FROM app.jobs",
    ],
)
def test_ordinary_diagnostic_reads_pass_the_guard(sql: str) -> None:
    assert _refused_sql(sql) is None
