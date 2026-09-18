"""The email-body renderer (`jbrain.gmail.body`): the pass that turns what Gmail hands
back into the compact text both the archivist's tools and the triage sweep read."""

from jbrain.gmail.body import collapse_tracking_urls, render_body
from jbrain.gmail.client import GmailMessage


def _msg(body: str, *, snippet: str = "") -> GmailMessage:
    return GmailMessage(
        id="m1",
        thread_id="t",
        sender="a@x.com",
        to="me@y.com",
        subject="Subject",
        date="2026-01-01",
        snippet=snippet,
        body=body,
    )


def test_html_body_becomes_markdown() -> None:
    html = "<div><h2>Receipt</h2><p>Total: <strong>$9.99</strong></p><style>x{}</style></div>"
    out = render_body(_msg(html))
    assert "## Receipt" in out
    assert "**$9.99**" in out
    assert "<div" not in out and "x{}" not in out


def test_plain_text_body_is_left_alone() -> None:
    out = render_body(_msg("Hi Jeff,\n\nYour package arrives Tuesday.\n"))
    assert out == "Hi Jeff,\n\nYour package arrives Tuesday."


def test_a_stray_angle_bracket_does_not_trigger_the_html_pass() -> None:
    """The HTML hint wants a real tag: a plain-text body comparing numbers keeps its text."""
    out = render_body(_msg("if a < b then ship"))
    assert out == "if a < b then ship"


def test_long_tracking_urls_collapse_to_their_host() -> None:
    url = "https://click.example.net/f/a/" + "Q" * 400
    out = collapse_tracking_urls(f"Open it here: {url} today.")
    assert url not in out
    assert out == "Open it here: <link: click.example.net> today."


def test_short_urls_survive_intact() -> None:
    text = "Pay at https://bill.example.org/inv/8821 before Friday."
    assert collapse_tracking_urls(text) == text


def test_a_collapsed_url_keeps_its_markdown_link_shape() -> None:
    url = "https://click.example.net/" + "Q" * 400
    out = collapse_tracking_urls(f"[Track it]({url})")
    assert out == "[Track it](<link: click.example.net>)"


def test_an_empty_body_falls_back_to_the_snippet() -> None:
    assert render_body(_msg("", snippet="Your order shipped")) == "Your order shipped"
    assert render_body(_msg("")) == ""


def test_the_shared_windowing_honours_a_caller_sized_window() -> None:
    """`window_text` sizes one page for its caller: an email read must not be able to
    spend a web page's 30k-char budget on a single message."""
    from jbrain.web.fetch import window_text

    result = window_text("y" * 50_000, url="", title="t", window=12_000)
    assert len(result.text) == 12_000
    assert result.total_chars == 50_000
    assert len(window_text("y" * 50_000, url="", title="t").text) == 30_000  # web default
