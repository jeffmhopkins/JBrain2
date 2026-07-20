"""The report-title cleaner (external.report_titler._clean_title): reducing a model reply
to one tidy heading. The full read→generate→write handler is covered against real Postgres
in tests/integration/test_research_corpus_pg.py."""

from jbrain.external.report_titler import _TITLE_CAP, _clean_title


def test_takes_first_nonempty_line() -> None:
    assert _clean_title("\n  Solid-State Batteries for Grid Storage  \n\nextra") == (
        "Solid-State Batteries for Grid Storage"
    )


def test_strips_surrounding_quotes_and_title_label() -> None:
    assert (
        _clean_title('"Coffee Milk Alternatives Compared"') == "Coffee Milk Alternatives Compared"
    )
    assert _clean_title("Title: The State of Fusion") == "The State of Fusion"
    assert _clean_title("“Curly Quoted Heading”") == "Curly Quoted Heading"


def test_collapses_whitespace() -> None:
    assert _clean_title("Too    many   spaces") == "Too many spaces"


def test_caps_a_runaway_title_with_an_ellipsis() -> None:
    long = "word " * 40
    out = _clean_title(long)
    assert len(out) <= _TITLE_CAP and out.endswith("…")


def test_blank_reply_is_empty() -> None:
    assert _clean_title("   \n  ") == ""
