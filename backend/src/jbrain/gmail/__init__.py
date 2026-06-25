"""Gmail access for the `archivist` persona (docs/EMAIL_ARCHIVIST_PLAN.md): a thin,
pinned client over the Gmail API, an in-memory fake for tests, and the typed message
/ label shapes they share. No DB, no notes — the persona is stateless on the box."""

from jbrain.gmail.client import (
    GmailApi,
    GmailClient,
    GmailError,
    GmailLabel,
    GmailMessage,
)
from jbrain.gmail.fake import FakeGmail
from jbrain.gmail.provider import GmailClientProvider

__all__ = [
    "FakeGmail",
    "GmailApi",
    "GmailClient",
    "GmailClientProvider",
    "GmailError",
    "GmailLabel",
    "GmailMessage",
]
