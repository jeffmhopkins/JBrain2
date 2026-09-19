"""Turning a raw Gmail body into the compact text an agent actually reads.

Gmail hands back whatever the sender composed: a `text/plain` part when there is one,
otherwise the `text/html` alternative — and a modern marketing email's HTML is a wall of
Outlook conditional comments, nested layout tables and inline styles around a few lines of
prose. Feeding that to a model spends thousands of prefill tokens on markup it has to see
through, and a windowed read wastes its whole window on `<td class="undefined-outlook">`.

Two passes fix that, and both are lossless for the decisions the archivist makes:

1. **HTML → markdown** (`jbrain.htmltext`), which drops markup/boilerplate and keeps the
   headings, lists, links and prose. Measured on the owner's own mail: a hospital
   statement went 16,538 → 2,223 chars (an 87% cut) with every fact intact.
2. **Collapsing tracking URLs.** A click-tracking link is a few hundred characters of
   opaque payload — one in that same mailbox ran to 965 — and in a marketing email they
   accounted for 75% of the rendered text. A long URL becomes `<link: host>`, so the
   host (the part that carries meaning: who the link goes to) survives and the payload
   does not. Short, human-readable URLs are left exactly as they are.

Shared by the archivist's `gmail_read`/`gmail_extract` tools and the unattended triage
sweep, which classifies every new message and so pays this cost the most often.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from jbrain.gmail.client import GmailMessage
from jbrain.htmltext import html_to_markdown

# A cheap signal that a body is HTML (a full-document tag or any closing tag), so we
# render it to markdown rather than handing the model raw markup. A text/plain body that
# happens to contain a stray "<" is left untouched.
_HTML_HINT = re.compile(
    r"<(?:html|head|body|div|p|table|td|tr|a|br|span|ul|ol|li|img|font)\b|</[a-zA-Z]+>", re.I
)
# URLs up to this length stay verbatim — a real, readable link (mychart.hf.org/…, an order
# page) is worth its characters. Past it a URL is a tracking payload, and only its host
# carries information the archivist can use.
_URL_KEEP = 100
# Trailing punctuation that belongs to the sentence, not the URL — stripped before the
# length test so `](https://…)` and `see https://….` collapse cleanly.
_URL_TAIL = ").,;:!?'\"»>"
_URL_RE = re.compile(r"https?://[^\s<>\]]+")


def collapse_tracking_urls(text: str) -> str:
    """Replace every over-long URL with `<link: host>`, keeping short ones verbatim."""

    def _shorten(m: re.Match[str]) -> str:
        raw = m.group(0)
        url = raw.rstrip(_URL_TAIL)
        if len(url) <= _URL_KEEP:
            return raw
        host = urlsplit(url).netloc or "link"
        return f"<link: {host}>" + raw[len(url) :]

    return _URL_RE.sub(_shorten, text)


def render_body(msg: GmailMessage) -> str:
    """The message body as clean text: HTML rendered to markdown, tracking URLs collapsed.

    Falls back to the raw body if the markdown pass yields nothing (a malformed part), and
    to the Gmail snippet when there is no body at all — never to an empty string where the
    message had content."""
    body = msg.body or ""
    if _HTML_HINT.search(body):
        body = html_to_markdown(body) or body
    return collapse_tracking_urls(body).strip() or (msg.snippet or "").strip()
