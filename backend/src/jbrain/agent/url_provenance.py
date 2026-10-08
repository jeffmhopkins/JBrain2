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
it was handed — history included, so a reopened chat keeps its sites — after the persisted
results of every earlier turn (`history_replay.Entry.provenance`, which survive a compacted
turn's stub), so when the bound bites the owner's current message is what stays. Each tool
result grows it, less any argument string the call itself carried (`without_echoes`): a
search answering "No web results for '<invented URL>'" must not open the URL it was asked
about. A spawned sub-agent inherits a copy of its parent's set rather than scanning the brief
the parent model wrote, and a plan continuation seeds from the chat's stored transcript
(`history_replay.transcript_sites`) rather than its model-written plan text. Sites are
compared in punycode, so an internationalized name matches either spelling. An IP literal, a
dotless host (`localhost`) or a private-network suffix (`nas.local`) counts only from the
owner's own words; the fetcher's SSRF guard still decides whether it may be reached.

Accepted gaps: a tool that transforms the model's own text before echoing it (python output)
can launder a host in, and a site the model names from memory that the owner also happened to
mention is allowed. The gate stops regurgitated addresses, not an adversary. No site is
blocked as such: the live incident's `aliyuncs.com` is refused only because nothing produced
it, and would be fetched if a search did. The internal fetches the GitHub reader makes
(codeload, the API) never pass through here.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Iterator, Sequence
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


# Suffixes no public list carries but an owner types for a box on his own network. The
# fetcher's SSRF guard still decides whether a private address may be reached at all; this
# only stops the gate from refusing what the owner himself named.
_PRIVATE_SUFFIXES = ("local", "lan", "internal", "home.arpa", "localdomain")


def _idna(domain: str) -> str:
    """One spelling per site: an internationalized name in its ASCII (punycode) form, so a
    link written either way opens the other. A name the codec rejects stays as it is."""
    try:
        return domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return domain


def site_of(url: str) -> str | None:
    """The key a URL is gated on: its registrable domain (IDN-normalized, aliases folded), or
    for a host with none — an IP literal, a dotless name like `localhost` — the host itself;
    None for junk. A scheme-less address reads as http."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    domain = registrable_domain(raw)
    if domain is None:
        try:
            host = (urlsplit(raw).hostname or "").rstrip(".")
        except ValueError:
            return None
        return host if host and _HOST_RE.fullmatch(host) else None
    domain = _idna(domain)
    for suffix, canonical in _ALIASES.items():
        if domain == suffix or domain.endswith("." + suffix):
            return canonical
    return domain


# What `site_of` falls back to for a host with no registrable domain: a dotless label or an
# IPv4 literal (an IPv6 one is the address in brackets, which `hostname` strips).
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?|[0-9a-f:.]+", re.IGNORECASE)


def _owner_only(site: str) -> bool:
    """A site only the owner can vouch for: an IP literal, a dotless host or a private-network
    suffix. A tool result naming one (a page's link to `localhost`) opens nothing."""
    if "." not in site or site.endswith(tuple("." + s for s in _PRIVATE_SUFFIXES)):
        return True
    try:
        ipaddress.ip_address(site)
    except ValueError:
        return False
    return True


def _bare_site(token: str, *, owner: bool) -> str | None:
    """A bare token's site, only when the Public Suffix List knows its suffix — `file.py` and
    `os.path` in prose are not hosts, and the fallback `registrable_domain` allows for an
    unknown suffix would turn every dotted word into one — or, in the owner's own words, when
    it ends in a private-network suffix (`nas.local`)."""
    if get_fld("http://" + token, fail_silently=True) is None:
        private = token.lower().endswith(tuple("." + s for s in _PRIVATE_SUFFIXES))
        if not (owner and private):
            return None
    return site_of(token)


def _sites_in(text: str, *, owner: bool) -> list[str]:
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        site = site_of(match.group(0))
        if site is not None and (owner or not _owner_only(site)):
            found.append(site)
    for match in _BARE_RE.finditer(text):
        site = _bare_site(match.group(1), owner=owner)
        if site is not None and (owner or not _owner_only(site)):
            found.append(site)
    return found


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def without_echoes(content: str, arguments: object) -> str:
    """`content` with every argument string that names a site cut out. A tool that echoes
    what the model sent it — a search's "No web results for '<query>'" — must not vouch for
    the model's own address: searching an invented URL would otherwise open it. Arguments
    that name no site (a plain query) are left in, so a real hit is never cut."""
    for value in sorted(set(_strings(arguments)), key=len, reverse=True):
        if value and _sites_in(value, owner=True):
            content = content.replace(value, " ")
    return content


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

    def copy(self) -> SeenSites:
        clone = SeenSites(self._limit)
        clone._sites = dict(self._sites)
        return clone

    def _add(self, site: str) -> None:
        self._sites.pop(site, None)
        self._sites[site] = None
        while len(self._sites) > self._limit:
            del self._sites[next(iter(self._sites))]

    def add_url(self, url: str) -> None:
        """A URL a tool reached or cited (a web source). Never an owner-only site."""
        site = site_of(url)
        if site is not None and not _owner_only(site):
            self._add(site)

    def add_text(self, text: str, *, owner: bool = False) -> None:
        """Every site a block of text names: its http(s) URLs and its bare hostnames. `owner`
        (a user-side message) also admits IP literals, dotless hosts and private suffixes."""
        for site in _sites_in(text, owner=owner):
            self._add(site)

    def allows(self, url: str) -> bool:
        site = site_of(url)
        return site is not None and site in self._sites


def seeded(
    messages: Sequence[LlmMessage], extra: Iterable[str] = (), *, limit: int = MAX_SITES
) -> SeenSites:
    """A run's set, seeded from what it was handed: `extra` texts first — the persisted
    results of earlier turns, oldest — then every user-side message and tool result in
    `messages` (never an assistant message), so when the bound bites it is the old results
    that fall out and the owner's current message that stays."""
    seen = SeenSites(limit)
    for text in extra:
        seen.add_text(text)
    for message in messages:
        if isinstance(message, UserMessage):
            seen.add_text(message.text, owner=True)
        elif isinstance(message, ToolResultMessage):
            for result in message.results:
                seen.add_text(result.content)
    return seen


def refusal(url: str, seen: SeenSites | None) -> str | None:
    """The refusal for fetching `url`, or None when it may be fetched. No set (a handler
    driven outside the agent loop) means no gate."""
    if seen is None or seen.allows(url):
        return None
    return REFUSAL
