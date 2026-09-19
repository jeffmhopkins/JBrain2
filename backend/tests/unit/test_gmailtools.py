"""The archivist's gmail_* tool handlers + their web-gating (docs/EMAIL_ARCHIVIST_PLAN
.md). Handlers run against FakeGmail — no network, the connector/LLM-adapter posture."""

from jbrain.agent.agents import GMAIL_TOOLS
from jbrain.agent.gmailtools import build_gmail_handlers
from jbrain.agent.loop import ToolContext
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry
from jbrain.db.session import SessionContext
from jbrain.gmail import FakeGmail, GmailError, GmailLabel, GmailMessage

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())


def _msg(mid: str, subject: str = "Invoice", body: str = "please pay", sender: str = "a@x.com"):
    return GmailMessage(
        id=mid,
        thread_id="t",
        sender=sender,
        to="me@y.com",
        subject=subject,
        date="2020-01-01",
        snippet=body[:20],
        body=body,
    )


def _handlers(fake: FakeGmail):
    async def get_client():
        return fake

    return build_gmail_handlers(get_client)


async def test_handlers_report_when_gmail_is_not_connected() -> None:
    async def get_client():
        raise GmailError("Gmail isn't connected yet — add your OAuth credentials in Settings.")

    handlers = build_gmail_handlers(get_client)
    out = await handlers["gmail_search"]({"query": "x"}, CTX)
    assert "connect" in out.lower() and "Settings" in out
    out2 = await handlers["gmail_label"]({"message_id": "m1", "add": ["X"]}, CTX)
    assert "connect" in out2.lower()


# --- reads -----------------------------------------------------------------


async def test_search_lists_matches() -> None:
    fake = FakeGmail([_msg("m1", subject="Invoice 2009"), _msg("m2", subject="Hello")])
    out = await _handlers(fake)["gmail_search"]({"query": "invoice"}, CTX)
    assert "[m1]" in out
    assert "Invoice 2009" in out
    assert "[m2]" not in out


async def test_search_empty_query_is_rejected() -> None:
    out = await _handlers(FakeGmail())["gmail_search"]({"query": "  "}, CTX)
    assert "non-empty query" in out


async def test_search_no_matches() -> None:
    out = await _handlers(FakeGmail([_msg("m1")]))["gmail_search"]({"query": "zzz"}, CTX)
    assert "No Gmail messages match" in out


async def test_read_returns_headers_and_body() -> None:
    fake = FakeGmail([_msg("m1", subject="Receipt", body="thanks for your order")])
    out = await _handlers(fake)["gmail_read"]({"message_id": "m1"}, CTX)
    assert "Subject: Receipt" in out
    assert "thanks for your order" in out


async def test_read_missing_message_surfaces_error() -> None:
    out = await _handlers(FakeGmail())["gmail_read"]({"message_id": "nope"}, CTX)
    assert "no such message" in out


# --- labels + writes -------------------------------------------------------


async def test_list_labels() -> None:
    fake = FakeGmail([_msg("m1")], labels=[GmailLabel(id="L1", name="Finance")])
    out = await _handlers(fake)["gmail_list_labels"]({}, CTX)
    assert "Finance" in out
    assert "INBOX" in out  # the system label is real and listed


async def test_create_label() -> None:
    fake = FakeGmail()
    out = await _handlers(fake)["gmail_create_label"]({"name": "Finance/Taxes"}, CTX)
    assert "Finance/Taxes" in out
    assert any(label.name == "Finance/Taxes" for label in await fake.list_labels())


async def test_label_applies_existing_and_moves_message() -> None:
    fake = FakeGmail([_msg("m1")])
    label = await fake.create_label("Finance")
    out = await _handlers(fake)["gmail_label"](
        {"message_id": "m1", "add": ["Finance"], "remove": ["INBOX"]}, CTX
    )
    assert "applied Finance" in out
    assert fake.labels_on("m1") == {label.id}  # in Finance, out of the inbox


async def test_label_refuses_to_invent_a_missing_label() -> None:
    fake = FakeGmail([_msg("m1")])
    out = await _handlers(fake)["gmail_label"]({"message_id": "m1", "add": ["Ghost"]}, CTX)
    assert "don't exist yet" in out
    assert "gmail_create_label" in out
    assert fake.labels_on("m1") == {"INBOX"}  # nothing changed


async def test_label_needs_a_change() -> None:
    out = await _handlers(FakeGmail([_msg("m1")]))["gmail_label"]({"message_id": "m1"}, CTX)
    assert "at least one label" in out


async def test_archive_removes_inbox() -> None:
    fake = FakeGmail([_msg("m1")])
    out = await _handlers(fake)["gmail_archive"]({"message_id": "m1"}, CTX)
    assert "archived" in out
    assert fake.labels_on("m1") == set()  # out of the inbox


async def test_write_error_surfaces_as_tool_message() -> None:
    out = await _handlers(FakeGmail())["gmail_archive"]({"message_id": "ghost"}, CTX)
    assert "no such message" in out


# --- count + bulk ----------------------------------------------------------


async def test_count_reports_total() -> None:
    fake = FakeGmail(
        [_msg("m1", subject="Invoice"), _msg("m2", subject="Invoice"), _msg("m3", subject="Hello")]
    )
    out = await _handlers(fake)["gmail_count"]({"query": "invoice"}, CTX)
    assert "2 message(s)" in out


async def test_count_empty_query_rejected() -> None:
    out = await _handlers(FakeGmail())["gmail_count"]({"query": ""}, CTX)
    assert "non-empty query" in out


async def test_sender_breakdown_ranks_real_domains() -> None:
    # Three chase.com, two amazon.com, one personal — the breakdown must reflect the
    # ACTUAL senders, ranked, not a guessed list.
    fake = FakeGmail(
        [
            _msg("m1", sender="alerts@chase.com"),
            _msg("m2", sender="no-reply@chase.com"),
            _msg("m3", sender="Chase <statements@chase.com>"),
            _msg("m4", sender="ship@amazon.com"),
            _msg("m5", sender="deals@amazon.com"),
            _msg("m6", sender="mom@family.net"),
        ]
    )
    out = await _handlers(fake)["gmail_sender_breakdown"]({"query": "pay"}, CTX)
    assert "chase.com — 3" in out
    assert "amazon.com — 2" in out
    assert "family.net — 1" in out
    assert "domains" in out  # grouped by domain by default


async def test_sender_breakdown_by_address_keeps_full_addresses() -> None:
    fake = FakeGmail([_msg("m1", sender="A <a@x.com>"), _msg("m2", sender="a@x.com")])
    out = await _handlers(fake)["gmail_sender_breakdown"]({"query": "x", "by": "address"}, CTX)
    assert "a@x.com — 2" in out


async def test_sender_breakdown_empty_query_rejected() -> None:
    out = await _handlers(FakeGmail())["gmail_sender_breakdown"]({"query": " "}, CTX)
    assert "non-empty query" in out


async def test_sender_breakdown_flags_a_capped_sample() -> None:
    fake = FakeGmail([_msg(f"m{i}", sender="x@a.com") for i in range(5)])
    out = await _handlers(fake)["gmail_sender_breakdown"]({"query": "x", "sample": 2}, CTX)
    assert "2 sampled" in out
    assert "gmail_count" in out  # nudges the agent to confirm an exact total


async def test_bulk_label_applies_across_the_whole_query() -> None:
    fake = FakeGmail(
        [_msg("m1", subject="Invoice"), _msg("m2", subject="Invoice"), _msg("m3", subject="Hello")]
    )
    label = await fake.create_label("Finance")
    out = await _handlers(fake)["gmail_bulk_label"](
        {"query": "invoice", "add": ["Finance"], "remove": ["INBOX"]}, CTX
    )
    assert "Bulk-updated 2 message(s)" in out
    assert fake.labels_on("m1") == {label.id}
    assert fake.labels_on("m2") == {label.id}
    assert fake.labels_on("m3") == {"INBOX"}  # didn't match the query, untouched


async def test_bulk_label_refuses_to_invent_a_missing_label() -> None:
    fake = FakeGmail([_msg("m1", subject="Invoice")])
    out = await _handlers(fake)["gmail_bulk_label"]({"query": "invoice", "add": ["Ghost"]}, CTX)
    assert "don't exist yet" in out
    assert fake.labels_on("m1") == {"INBOX"}  # nothing changed


async def test_bulk_label_no_matches() -> None:
    fake = FakeGmail([_msg("m1", subject="Invoice")])
    await fake.create_label("Finance")
    out = await _handlers(fake)["gmail_bulk_label"]({"query": "zzz", "add": ["Finance"]}, CTX)
    assert "No messages match" in out


async def test_bulk_label_needs_a_change() -> None:
    out = await _handlers(FakeGmail([_msg("m1")]))["gmail_bulk_label"]({"query": "x"}, CTX)
    assert "at least one label" in out


async def test_empty_arguments_are_rejected() -> None:
    h = _handlers(FakeGmail())
    assert "needs a message_id" in await h["gmail_read"]({}, CTX)
    assert "needs a name" in await h["gmail_create_label"]({"name": " "}, CTX)
    assert "needs a message_id" in await h["gmail_label"]({}, CTX)
    assert "needs a message_id" in await h["gmail_archive"]({}, CTX)


class _BoomGmail(FakeGmail):
    """A client whose every call fails — to exercise each handler's error branch."""

    async def search(self, query: str, *, max_results: int = 25) -> list[str]:
        raise GmailError("upstream down")

    async def list_labels(self):  # type: ignore[override]
        raise GmailError("upstream down")

    async def create_label(self, name: str):  # type: ignore[override]
        raise GmailError("upstream down")


async def test_read_path_errors_surface_cleanly() -> None:
    h = _handlers(_BoomGmail())
    assert "upstream down" in await h["gmail_search"]({"query": "x"}, CTX)
    assert "upstream down" in await h["gmail_list_labels"]({}, CTX)
    assert "upstream down" in await h["gmail_create_label"]({"name": "X"}, CTX)
    assert "upstream down" in await h["gmail_label"]({"message_id": "m1", "add": ["X"]}, CTX)


# --- reading cheaply -------------------------------------------------------

# A marketing email as Gmail actually hands one over: a layout-table shell around three
# lines of prose, with a click-tracking URL far longer than the content it wraps.
_TRACKER = "https://emails.example.com/e3t/Ctc/" + "V" * 400
_HTML_EMAIL = (
    '<!--[if mso | IE]><td class="undefined-outlook" width="700px"><![endif]-->'
    '<div style="margin:0px auto;max-width:700px;">'
    '<table align="center" border="0" cellpadding="0" role="presentation"><tbody><tr><td>'
    "<h1>Your order is on the way</h1>"
    "<p>Order #4412 &mdash; Total: $57.31</p>"
    f'<p><a href="{_TRACKER}">Track your delivery</a></p>'
    "</td></tr></tbody></table></div>"
)


async def test_read_renders_html_to_clean_text_and_collapses_trackers() -> None:
    """The defect this closes: gmail_read used to hand the model the raw HTML alternative,
    so a three-line order email arrived as a wall of Outlook conditionals and a 400-char
    tracking payload — thousands of prefill tokens of markup to see past."""
    fake = FakeGmail([_msg("m1", subject="Your order", body=_HTML_EMAIL)])
    out = await _handlers(fake)["gmail_read"]({"message_id": "m1"}, CTX)
    assert "Total: $57.31" in out and "Order #4412" in out
    assert "<td" not in out and "undefined-outlook" not in out
    assert _TRACKER not in out
    assert "<link: emails.example.com>" in out  # the host survives; the payload doesn't
    assert len(out) < len(_HTML_EMAIL) // 2


async def test_read_keeps_short_readable_urls() -> None:
    """Only the tracking payloads go: a short link names where it leads, so it stays whole."""
    fake = FakeGmail([_msg("m1", body="See https://mychart.example.org/bill/22 for details")])
    out = await _handlers(fake)["gmail_read"]({"message_id": "m1"}, CTX)
    assert "https://mychart.example.org/bill/22" in out


async def test_read_windows_a_long_message_and_names_the_next_offset() -> None:
    fake = FakeGmail([_msg("m1", body="x" * 30_000)])
    out = await _handlers(fake)["gmail_read"]({"message_id": "m1"}, CTX)
    assert "offset=12000" in out and "of 30000" in out
    tail = await _handlers(fake)["gmail_read"]({"message_id": "m1", "offset": 12_000}, CTX)
    assert "offset=24000" in tail


async def test_read_find_jumps_to_the_term() -> None:
    body = "noise " * 3_000 + "CONFIRMATION 8891" + " tail" * 100
    out = await _handlers(FakeGmail([_msg("m1", body=body)]))["gmail_read"](
        {"message_id": "m1", "find": "confirmation"}, CTX
    )
    assert "CONFIRMATION 8891" in out
    assert "found 1 match(es)" in out


async def test_read_find_that_misses_says_so_instead_of_dumping_the_body() -> None:
    out = await _handlers(FakeGmail([_msg("m1", body="hello " * 500)]))["gmail_read"](
        {"message_id": "m1", "find": "invoice"}, CTX
    )
    assert "No match" in out
    assert "hello hello" not in out


async def test_read_extract_returns_matches_not_the_message() -> None:
    fake = FakeGmail([_msg("m1", subject="Your order", body=_HTML_EMAIL)])
    out = await _handlers(fake)["gmail_read"](
        {"message_id": "m1", "find": r"total:\s*\$([\d,.]+)", "extract": True}, CTX
    )
    assert "[groups: 57.31]" in out
    # Only the match and a snippet of context come back, never the message — extracting a
    # value costs a fraction of reading one, which is the whole point.
    padded = _HTML_EMAIL + "<p>" + "filler prose. " * 500 + "</p>"
    long_out = await _handlers(FakeGmail([_msg("m2", body=padded)]))["gmail_read"](
        {"message_id": "m2", "find": r"total:\s*\$([\d,.]+)", "extract": True}, CTX
    )
    assert "[groups: 57.31]" in long_out
    assert "filler prose" not in long_out
    assert len(long_out) < len(padded) // 10


async def test_read_extract_needs_a_pattern_and_rejects_a_bad_one() -> None:
    h = _handlers(FakeGmail([_msg("m1")]))
    assert "needs `find`" in await h["gmail_read"]({"message_id": "m1", "extract": True}, CTX)
    bad = await h["gmail_read"]({"message_id": "m1", "find": "total(", "extract": True}, CTX)
    assert "doesn't compile" in bad


async def test_extract_scans_many_messages_in_one_call() -> None:
    """The call the owner's question needed: thirteen delivery emails, one total each,
    answered without thirteen full bodies in the conversation."""
    fake = FakeGmail(
        [
            _msg(
                "m1", subject="Delivery 1", body="Total: $12.50 delivered", sender="w@walmart.com"
            ),
            _msg("m2", subject="Delivery 2", body="Total: $7.05 delivered", sender="w@walmart.com"),
            _msg("m3", subject="Promo", body="delivered soon, no price", sender="w@walmart.com"),
        ]
    )
    out = await _handlers(fake)["gmail_extract"](
        {"query": "delivered", "find": r"total:\s*\$([\d,.]+)"}, CTX
    )
    assert "[groups: 12.50]" in out and "[groups: 7.05]" in out
    assert "2 with a match" in out
    assert "No match in 1: m3" in out  # the odd one out is named, not silently dropped
    assert "delivered soon, no price" not in out


async def test_extract_validates_its_inputs_before_touching_gmail() -> None:
    h = _handlers(_BoomGmail())
    assert "non-empty query" in await h["gmail_extract"]({"query": " ", "find": "x"}, CTX)
    assert "needs `find`" in await h["gmail_extract"]({"query": "x", "find": ""}, CTX)
    assert "doesn't compile" in await h["gmail_extract"]({"query": "x", "find": "("}, CTX)


async def test_extract_reports_when_nothing_matched_the_pattern() -> None:
    fake = FakeGmail([_msg("m1", body="no amounts here")])
    out = await _handlers(fake)["gmail_extract"]({"query": "amounts", "find": r"\$\d+"}, CTX)
    assert "No match for regex" in out
    assert "gmail_read one of them" in out


async def test_extract_says_when_it_scanned_only_the_most_recent() -> None:
    fake = FakeGmail([_msg(f"m{i}", body="Total: $1.00") for i in range(5)])
    out = await _handlers(fake)["gmail_extract"](
        {"query": "total", "find": r"\$([\d.]+)", "limit": 2}, CTX
    )
    assert "Scanned the 2 most recent" in out


# --- web gating ------------------------------------------------------------


async def _noop(arguments: dict, ctx: ToolContext) -> str:
    return ""


def _gmail_registry() -> ToolRegistry:
    return ToolRegistry(
        [RegisteredTool(load_tool(TOOLS_DIR / f"{name}.tool"), _noop) for name in GMAIL_TOOLS]
    )


def test_gmail_sidecars_are_web_class() -> None:
    reg = _gmail_registry()
    assert all(reg.get(name).spec.permission == "web" for name in GMAIL_TOOLS)


def test_archivist_sees_all_gmail_tools_curator_sees_none() -> None:
    reg = _gmail_registry()
    # The archivist's allowlist admits exactly the gmail tools...
    assert reg.allowed_names(scopes=(), allow=GMAIL_TOOLS) == frozenset(GMAIL_TOOLS)
    # ...while the default knowledge agent (curator, allow=None) is denied the whole
    # opt-in web class, so it can never reach the owner's mailbox.
    assert reg.allowed_names(scopes=(), allow=None) == frozenset()


def test_gmail_sidecars_are_registered_optional() -> None:
    """Every gmail sidecar is in the optional set, so an unconfigured box (no handlers)
    drops them from the registry rather than failing startup."""
    from jbrain.agent.readtools import OPTIONAL_GMAIL_TOOLS

    assert frozenset(GMAIL_TOOLS) == OPTIONAL_GMAIL_TOOLS
