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


def _alt(plain: str, html: str) -> GmailMessage:
    return GmailMessage(
        id="m1",
        thread_id="t",
        sender="orders@mouser.com",
        to="me@y.com",
        subject="Confirmation of your order",
        date="2026-01-01",
        snippet="",
        body=plain,
        html=html,
    )


_ORDER_HTML = (
    "<table><tr><td>Mouser #</td><td>Description</td><td>Qty</td></tr>"
    "<tr><td>841-MPXV7002DP</td><td>Board Mount Pressure Sensors</td><td>2</td></tr>"
    "<tr><td>538-10-89-7103</td><td>Headers &amp; Wire Housings</td><td>4</td></tr></table>"
    "<p>Thank you for your order. Order Date: OCT 18, 2025. Customer Number: 1-354DA.</p>"
)


def test_a_stub_plain_part_yields_to_the_html() -> None:
    """The box's real miss: DigiKey's plain alternative is ".", Mouser's a one-line thank
    you, and the line items live only in the HTML — so no search of them could match."""
    for stub in (".", "Thank you for your order."):
        out = render_body(_alt(stub, _ORDER_HTML))
        assert "841-MPXV7002DP" in out
        assert "<td" not in out


def test_an_honest_plain_part_is_kept_over_the_html() -> None:
    plain = (
        "Mouser # 841-MPXV7002DP Board Mount Pressure Sensors qty 2\n"
        "Mouser # 538-10-89-7103 Headers and Wire Housings qty 4\n"
        "Thank you for your order. Order Date: OCT 18, 2025."
    )
    assert render_body(_alt(plain, _ORDER_HTML)) == plain
