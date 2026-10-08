"""The web tools' provenance gate: fetch only an address the conversation actually produced.

Seen live 2026-10-08: asked for a file in one of the owner's repos, jerv's first `web_fetch`
went to a signed Alibaba OSS temp-file link that appeared nowhere in the chat — the local model
regurgitating a URL shape from its training data. It 403'd, the box spent 12.8 s on it
(challenge solver included), and only then did jerv build the right github.com address. A
prompt rule alone does not hold a model that is pattern-completing, so the tools refuse too.

The rule: a model-supplied URL (`web_fetch`, `fetch_image`, `browse`'s start_url) is fetched
only when its SITE — the registrable domain (`browse_gate.registrable_domain`, eTLD+1) — has
already appeared in the conversation: in an owner message (or any other user-side message the
server put in the prompt), or in any tool result, this turn or an earlier one (search hits,
fetched pages' final URLs and the links they list, read_artifact, the GitHub reader's links,
a sub-agent's report). The model's own prose never counts: that is where an invented address
comes from. There is no always-allowed site; github.com is open as soon as any github.com
link has appeared, and `githubusercontent.com` / `youtu.be` count as github.com / youtube.com.

The set is per run (`ToolContext.seen_sites`), seeded by the agent loop from the conversation
it was handed — history included, so a reopened chat keeps its sites — plus the persisted
results of turns whose replay was compacted to a stub (`history_replay.Entry.provenance`).
A sub-agent is seeded from its own task text the same way. It is bounded; the oldest sites
fall out first.

Accepted gaps: a tool that echoes the model's own text (python output, a note it wrote) can
launder an invented host into the set, and a site the model names from memory that the owner
also happened to mention is allowed. The gate stops regurgitated addresses, not an adversary.
The internal fetches the GitHub reader makes (codeload, the API) never pass through here.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from tld import get_fld

from jbrain.agent.browse_gate import registrable_domain
from jbrain.llm import LlmMessage, ToolResultMessage, UserMessage

REFUSAL = (
    "Refused: this address didn't come from the owner, a search, or a fetched page. Search"
    " for it first (web_search), or ask the owner for the link."
)
REFUSAL_BRIEF = "refused · unseen address"

# Generous: a long research chat lists a few hundred sites; this only stops a pathological
# history from growing the set without end.
MAX_SITES = 4096

# One site served from another registrable domain: the raw/gist file hosts are GitHub's, and
# the short link is YouTube's. Checked by suffix so every githubusercontent subdomain folds.
_ALIASES = {"githubusercontent.com": "github.com", "youtu.be": "youtube.com"}

_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]}|]+", re.IGNORECASE)
# A bare host the owner typed without a scheme ("check nytimes.com"). Not preceded by a path,
# an @ or a dot, so a file path's last segment and an email's domain stay out.
_BARE_RE = re.compile(
    r"(?<![\w@./:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62})(?![\w-])",
    re.IGNORECASE,
)


def site_of(url: str) -> str | None:
    """The key a URL is gated on: its registrable domain (aliases folded), an IP literal's
    address, or None for junk. A scheme-less address reads as http."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    domain = registrable_domain(raw)
    if domain is None:
        try:
            host = (urlsplit(raw).hostname or "").rstrip(".")
            ipaddress.ip_address(host)
        except ValueError:
            return None
        return host
    for suffix, canonical in _ALIASES.items():
        if domain == suffix or domain.endswith("." + suffix):
            return canonical
    return domain


def _bare_site(token: str) -> str | None:
    """A bare token's site, only when the Public Suffix List knows its suffix: `file.py` and
    `os.path` in prose are not hosts, and the fallback `registrable_domain` allows for an
    unknown suffix would turn every dotted word into one."""
    if get_fld("http://" + token, fail_silently=True) is None:
        return None
    return site_of(token)


class SeenSites:
    """The sites this run's conversation has produced — the provenance gate's memory."""

    def __init__(self, limit: int = MAX_SITES) -> None:
        self._limit = limit
        # Insertion-ordered, so the bound drops the oldest sites first.
        self._sites: dict[str, None] = {}

    def __contains__(self, site: object) -> bool:
        return site in self._sites

    def __len__(self) -> int:
        return len(self._sites)

    def _add(self, site: str | None) -> None:
        if site is None:
            return
        self._sites.pop(site, None)
        self._sites[site] = None
        while len(self._sites) > self._limit:
            del self._sites[next(iter(self._sites))]

    def add_url(self, url: str) -> None:
        self._add(site_of(url))

    def add_text(self, text: str) -> None:
        """Every site a block of text names: its http(s) URLs and its bare hostnames."""
        for match in _URL_RE.finditer(text):
            self._add(site_of(match.group(0)))
        for match in _BARE_RE.finditer(text):
            self._add(_bare_site(match.group(1)))

    def allows(self, url: str) -> bool:
        site = site_of(url)
        return site is not None and site in self._sites


def seeded(messages: Sequence[LlmMessage], extra: Iterable[str] = ()) -> SeenSites:
    """A run's set, seeded from what it was handed: every user-side message and tool result
    in `messages` (never an assistant message), then `extra` texts — the persisted results of
    turns whose replay is only a stub."""
    seen = SeenSites()
    for message in messages:
        if isinstance(message, UserMessage):
            seen.add_text(message.text)
        elif isinstance(message, ToolResultMessage):
            for result in message.results:
                seen.add_text(result.content)
    for text in extra:
        seen.add_text(text)
    return seen


def refusal(url: str, seen: SeenSites | None) -> str | None:
    """The refusal for fetching `url`, or None when it may be fetched. No set (a handler
    driven outside the agent loop) means no gate."""
    if seen is None or seen.allows(url):
        return None
    return REFUSAL
