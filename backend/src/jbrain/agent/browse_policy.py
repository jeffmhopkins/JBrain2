"""The browse sub-agent's deterministic rules: what it may do, what it sees, what it returns.

Everything here is pure and decided by code, never by the prompt (docs/plans/
BROWSER_AGENT_PLAN.md §2): the model reading a page is the part an injected page talks to,
so every action it picks passes these checks before the browser sees it.

- **The action gate.** A ref must come from the latest page (no CSS selectors, no stale
  refs); navigation is public http(s) only; typing and selecting are allowed only into
  search, filter, location and date fields (B1's interim stand-in for B2's risk gate — no
  form ever carries personal data); buttons that commit to something are refused.
- **The page view.** playwright-mcp's accessibility snapshot, pruned to what the model can
  act on and the text it needs to read, and capped, so one busy page cannot flood a small
  model's context. When the page is the one the model last saw, only what changed is sent
  (`page_delta`): the loop never rewrites a page it already sent, so every page stays in the
  prompt and an unchanged one must not be paid for twice.
- **The fact check.** An answer is verified by the host, not the model: its names, times and
  numbers must be on the final page's full text (`facts_on_page`).
- **The quarantine.** What returns to jerv is plain text: links, images and markup are
  stripped so a poisoned page cannot turn the answer into a beacon or a clickable lure.
"""

from __future__ import annotations

import difflib
import hashlib
import ipaddress
import re
import socket
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

# --- The page view ------------------------------------------------------------

# Roles the model may act on. Everything else is read, not driven.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "option",
        "checkbox",
        "radio",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
        "treeitem",
    }
)
# Containers whose only job is structure. Kept only when they carry text of their own.
_STRUCTURAL_ROLES = frozenset(
    {"generic", "group", "list", "listitem", "region", "none", "presentation", "separator"}
)
# What one page may occupy in the model's context (~4k tokens at 4 chars/token). Tighter than
# B1's first 24k: every page a run sees now stays in its prompt (browse.py), so a page's size
# is paid on every later step's attention, not just once.
MAX_SNAPSHOT_CHARS = 16_000
_CHARS_PER_TOKEN = 4
# One text line's share of the view: a marketing blurb or a terms paragraph should not cost
# more than a few showtimes. Evidence is checked against the page's FULL text, not this.
MAX_LINE_CHARS = 400
# Text within this many lines of something the model can act on (or a heading) is what it
# reads to choose; text further away is dropped first when a page is over the cap.
_NEAR_LINES = 2
# A changed page is sent as its delta only while that is clearly smaller than the page, and
# only while it stays readable: a few hunks, all under one heading (a film, a day).
_DELTA_MAX_SHARE = 0.6
_DELTA_MAX_HUNKS = 6
_GONE_REFS_NAMED = 12
_HEADING_LINE = re.compile(r"^\s*- heading\b")

# `- role "name" [attr] [ref=e12]: text` — the shape of one aria-snapshot line, optionally
# wrapped in YAML single quotes when the name contains a colon.
_LINE = re.compile(
    r"^(?P<indent>\s*)-\s+'?(?P<role>[a-z][\w-]*)"
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?:\s+\[[^\]]*\])*)'?"
    r"(?::\s*(?P<text>.*))?$"
)
_REF = re.compile(r"\[ref=([A-Za-z0-9]+)\]")
_NOISE_ATTRS = re.compile(r"\s*\[(?:ref=[A-Za-z0-9]+|cursor=[\w-]+|active)\]")
_TEXT_LINE = re.compile(r'^(?P<indent>\s*)-\s+text:\s*"?(?P<text>.*?)"?$')
_URL_LINE = re.compile(r"^\s*-\s+/url:")


@dataclass(frozen=True)
class Element:
    """One actionable element on the page: what the gates judge a click/type/select by."""

    ref: str
    role: str
    name: str
    # Inside a `search` landmark — the page's own declaration that this is a search form.
    in_search: bool = False


@dataclass(frozen=True)
class PageView:
    """The current page as the model sees it, plus what the host checks against."""

    url: str = ""
    title: str = ""
    status: str = ""
    outline: str = ""
    elements: dict[str, Element] = field(default_factory=dict)
    # Every readable string on the page, whitespace-normalized and lowercased — what an
    # answer's facts are checked against (`facts_on_page`).
    text: str = ""
    truncated: bool = False
    # The same strings as the page shows them, one per line — what a run that stops before an
    # answer hands back (quarantined) so the caller can still read the page it ended on.
    readable: str = ""

    @property
    def tokens(self) -> int:
        return len(self.outline) // _CHARS_PER_TOKEN

    @property
    def fingerprint(self) -> str:
        """Changes when the page does — for spotting an action that achieved nothing."""
        digest = hashlib.sha256(f"{self.url}\x00{self.outline}".encode()).hexdigest()
        return digest[:16]

    def render(self) -> str:
        head = [f"URL: {self.url or '(none)'}", f"Title: {self.title or '(none)'}"]
        if self.status:
            head.append(f"HTTP status: {self.status}")
        body = self.outline or "(the page shows nothing readable)"
        note = (
            "\n[The page was longer than this view; some of its text is not shown.]"
            if self.truncated
            else ""
        )
        return "\n".join(head) + "\n\nPage:\n" + body + note


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.replace('\\"', '"')


def _snapshot_yaml(tool_text: str) -> str:
    """The aria snapshot inside a playwright-mcp result (its ```yaml fence), or ''."""
    match = re.search(r"```yaml\n(.*?)(?:```|\Z)", tool_text, re.DOTALL)
    return match.group(1) if match else ""


def _page_field(tool_text: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}:\s*(.*)$", tool_text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_page(tool_text: str, *, cap: int = MAX_SNAPSHOT_CHARS) -> PageView:
    """Turn a `browser_snapshot` result into the pruned view the model reads.

    Kept: every interactive element (with its ref), and every line that carries readable
    text. Dropped: link targets (`/url:` — the model clicks refs, it never needs an
    address), refs on non-interactive lines, and structural wrappers with nothing to say.
    Indentation is halved, which is most of a deep tree's bytes."""
    yaml = _snapshot_yaml(tool_text)
    elements: dict[str, Element] = {}
    texts: list[str] = []
    # (line, anchor): an anchor is something the model acts on or a heading it orients by.
    lines: list[tuple[str, bool]] = []
    search_depth: int | None = None
    for raw in yaml.splitlines():
        if not raw.strip() or _URL_LINE.match(raw):
            continue
        indent = len(raw) - len(raw.lstrip())
        if search_depth is not None and indent <= search_depth:
            search_depth = None
        text_line = _TEXT_LINE.match(raw)
        if text_line:
            value = _unquote(text_line.group("text"))
            if not value:
                continue
            texts.append(value)
            line = f"{' ' * (indent // 2)}- {_clip(value)}"
            anchor = False
        else:
            match = _LINE.match(raw)
            if match is None:
                continue
            role = match.group("role")
            name = _unquote(f'"{match.group("name")}"') if match.group("name") else ""
            attrs = match.group("attrs") or ""
            trailing = _unquote(match.group("text") or "")
            if role == "search":
                search_depth = indent
            ref_match = _REF.search(attrs)
            interactive = role in INTERACTIVE_ROLES and ref_match is not None
            if name:
                texts.append(name)
            if trailing:
                texts.append(trailing)
            if not interactive and not name and not trailing:
                continue
            anchor = interactive or role == "heading"
            if role in _STRUCTURAL_ROLES and not interactive and not name:
                line = f"{' ' * (indent // 2)}- {_clip(trailing)}"
            else:
                shown_attrs = _NOISE_ATTRS.sub("", attrs).strip()
                line = f"{' ' * (indent // 2)}- {role}"
                if name:
                    line += f' "{name}"'
                if shown_attrs:
                    line += f" {shown_attrs}"
                if interactive and ref_match is not None:
                    ref = ref_match.group(1)
                    elements[ref] = Element(
                        ref=ref, role=role, name=name, in_search=search_depth is not None
                    )
                    line += f" [ref={ref}]"
                if trailing:
                    line += f": {_clip(trailing)}"
        lines.append((line, anchor))
    out, truncated = _prune(lines, cap)
    return PageView(
        url=_page_field(tool_text, "Page URL"),
        title=_page_field(tool_text, "Page Title"),
        status=_page_field(tool_text, "HTTP status"),
        outline="\n".join(out),
        elements=elements,
        text=_normalize(" ".join(texts)),
        truncated=truncated,
        readable=_readable(texts, cap),
    )


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1].rstrip() + "…"


def _prune(lines: list[tuple[str, bool]], cap: int) -> tuple[list[str], bool]:
    """The lines that fit `cap`, in page order, and whether any were dropped. Over the cap,
    what the model can act on and the text beside it go in first; far-off text fills what is
    left — so a long article above the showtimes no longer pushes the showtimes out."""
    if sum(len(line) + 1 for line, _ in lines) <= cap:
        return [line for line, _ in lines], False
    anchors = [i for i, (_, anchor) in enumerate(lines) if anchor]
    near = {
        j
        for i in anchors
        for j in range(max(0, i - _NEAR_LINES), min(len(lines), i + _NEAR_LINES + 1))
    }
    keep: set[int] = set()
    size = 0
    for tier in (sorted(near), [i for i in range(len(lines)) if i not in near]):
        for i in tier:
            cost = len(lines[i][0]) + 1
            if size + cost <= cap:
                keep.add(i)
                size += cost
    return [lines[i][0] for i in sorted(keep)], len(keep) < len(lines)


def page_delta(before: PageView | None, after: PageView) -> str | None:
    """`after` as what changed since the model saw `before`, or None to send it in full.

    Only for the SAME page (same URL): a navigation always sends the new page whole. The
    model reads the delta against the view it already holds, so unchanged lines — and their
    refs — still stand; lines that went are counted and their refs named. Each hunk carries
    the heading it sits under, so "9:40 PM" still says which film it is; changes under more
    than one heading, or too many hunks, send the page whole instead, since a scatter of
    context-free lines is what a small model misreads. The gate never reads this: it checks
    refs against `after.elements`, the page as it is now."""
    if before is None or not before.url or before.url != after.url:
        return None
    old, new = before.outline.splitlines(), after.outline.splitlines()
    head = [f"URL: {after.url}"]
    if after.title != before.title:
        head.append(f"Title: {after.title or '(none)'}")
    if after.status and after.status != before.status:
        head.append(f"HTTP status: {after.status}")
    if old == new:
        return "\n".join([*head, "[The page is unchanged since the last view above.]"])
    # The heading each new line sits under (its index), or -1 above the first heading.
    section: list[int] = []
    current = -1
    for j, line in enumerate(new):
        if _HEADING_LINE.match(line):
            current = j
        section.append(current)
    hunks: list[tuple[int, int]] = []
    sections: set[int] = set()
    gone: list[str] = []
    removed = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            removed += i2 - i1
            gone += [r for line in old[i1:i2] for r in _REF.findall(line)]
        # A hunk that opens on its own heading needs none from above it.
        if not (j2 > j1 and _HEADING_LINE.match(new[j1])):
            sections.add(section[j1 - 1] if j1 > 0 else -1)
        hunks.append((j1, j2))
    if len(sections) > 1 or len(hunks) > _DELTA_MAX_HUNKS:
        return None
    shown: list[str] = []
    last = -1
    for j1, j2 in hunks:
        if j2 == j1:
            continue
        above = section[j1 - 1] if j1 > 0 else -1
        opens_section = bool(_HEADING_LINE.match(new[j1]))
        lead = [] if opens_section else [i for i in (above, j1 - 1) if i >= 0 and i > last]
        for i in sorted(set(lead)):
            if shown and i > last + 1:
                shown.append("  …")
            shown.append(new[i])
            last = i
        if shown and j1 > last + 1:
            shown.append("  …")
        shown.extend(new[j1:j2])
        last = j2 - 1
    note = (
        "[Same page; only changes are shown, each under its heading and the line before it."
        " The rest stands."
    )
    if removed:
        note += f" {removed} line(s) are gone"
        names = [r for r in dict.fromkeys(gone) if r not in after.elements]
        if names:
            more = len(names) - _GONE_REFS_NAMED
            note += ", and with them refs " + ", ".join(names[:_GONE_REFS_NAMED])
            note += f" (and {more} more)" if more > 0 else ""
        note += "."
    note += "]"
    body = "\n".join(shown) if shown else "(nothing new; only removals)"
    delta = "\n".join([*head, note, "", "Changed or new:", body])
    if after.truncated:
        delta += "\n[The page is longer than this view allows; some of it is not shown.]"
    if len(delta) > _DELTA_MAX_SHARE * len(after.render()):
        return None
    return delta


def _readable(texts: list[str], cap: int) -> str:
    """The page's strings, each on one line, a repeat of the line before dropped (a link's
    name and its text often say the same thing), bounded like the outline."""
    out: list[str] = []
    size = 0
    for text in texts:
        line = " ".join(text.split())
        if not line or (out and out[-1] == line):
            continue
        if size + len(line) + 1 > cap:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


# --- The action gate ----------------------------------------------------------

_REF_SHAPE = re.compile(r"^[A-Za-z0-9]{1,16}$")
_LAN_SUFFIXES = (".local", ".internal", ".lan", ".localdomain", ".home.arpa", ".localhost")
MAX_URL_CHARS = 2_000
MAX_TYPED_CHARS = 200
MAX_SELECT_VALUES = 5
MAX_WAIT_SECONDS = 5.0
ALLOWED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "ArrowDown",
        "ArrowUp",
        "ArrowLeft",
        "ArrowRight",
        "PageDown",
        "PageUp",
        "Home",
        "End",
    }
)
TAB_ACTIONS = frozenset({"list", "select", "close", "new"})

# Fields typing is allowed into: search and filter boxes, location pickers (a zip, a city, a
# store), and dates. B1's interim rule until B2's risk gate: anything else is refused.
_ALLOWED_FIELD = re.compile(
    # Deliberately no bare "address", "state", "type" or "format": on a checkout or sign-up
    # form those label personal fields. Paired with a search word ("Search by address") the
    # search term admits them anyway.
    r"search|find|filter|query|keyword|look ?up|\bzip\b|zip ?code|postal|post ?code|"
    r"\bcity\b|\btown\b|location|\bnear\b|\bwhere\b|"
    r"\bdate\b|\bwhen\b|\bday\b|\bmonth\b|\byear\b|\bstore\b|theat(?:er|re)|cinema|"
    r"\bsort\b|order by|categor|genre|per page",
    re.IGNORECASE,
)
# Fields refused even when the allow list also matches ("Search your account email").
_DENIED_FIELD = re.compile(
    r"pass(?:word|code|phrase)?\b|e-?mail|phone|mobile|\btel\b|card|\bcvv\b|\bcvc\b|"
    r"security code|expir|\bssn\b|social security|account|routing|\biban\b|user ?name|"
    r"\blog ?in\b|sign ?in|first name|last name|full name|your name|name on|birth|\bdob\b|"
    r"\botp\b|one[- ]time|verification|\b2fa\b|\bpin\b|comment|message|review|reply|"
    r"coupon|promo|gift ?card|licen[cs]e|passport|\btax\b|signature|quantity|\bqty\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# A value with this many digits is a phone, card or account number, never a zip or a date.
_MAX_DIGITS = 9
# Buttons that commit the visitor to something. Links are navigation (a GET) and pass; a
# button that buys, books, signs in or sends does not, whatever the page calls it.
_COMMIT_BUTTON = re.compile(
    r"\bplace (?:my )?order\b|\bpay\b|\bpay now\b|\bpurchase\b|\bcheck ?out\b|\bcheckout\b|"
    r"\bcomplete (?:order|purchase|booking)\b|\bconfirm\b|\bsubmit\b|\bsign ?up\b|"
    r"\bregister\b|\bsubscribe\b|\bsend\b|\bpost\b|\bpublish\b|\bdelete\b|\blog ?in\b|"
    r"\bsign ?in\b|\bdonate\b|\bbook now\b|\breserve\b|\bcreate account\b|\bapply\b|"
    r"\badd to (?:cart|bag|basket)\b|\bcontinue\b|\bnext\b|\bproceed\b",
    re.IGNORECASE,
)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """`is_global` rather than `not is_private`: it also excludes CGNAT 100.64/10, which
    Python counts as neither private nor global."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$", re.IGNORECASE)


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """The address a browser reads a numeric host as — `127.1`, `0177.0.0.1`, `0x7f.1` are
    all 127.0.0.1 to Chromium, and none of them parses as an IP for `ipaddress`."""
    labels = host.split(".")
    if not all(_NUMERIC_LABEL.match(label) for label in labels):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def check_url(url: str) -> str | None:
    """Why `url` may not be opened, or None. Defense in depth: the egress proxy is the real
    fence, but refusing here keeps an obviously internal address out of the trace and gives
    the model a reason it can act on."""
    url = url.strip()
    if not url:
        return "navigate needs a URL."
    if len(url) > MAX_URL_CHARS:
        return "That address is too long."
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return "That is not a valid web address."
    if parts.scheme not in ("http", "https"):
        return "Only http and https addresses can be opened."
    if parts.username or parts.password:
        return "Addresses with a user name or password in them are refused."
    if not host:
        return "That address has no host."
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is None:
        ip = _legacy_ipv4(host)
        if ip is None and all(_NUMERIC_LABEL.match(label) for label in host.split(".")):
            return "That is not a valid web address."
    if ip is not None:
        return None if _is_public_ip(ip) else "That address is on a private network; refused."
    if "." not in host or host == "localhost" or host.endswith(_LAN_SUFFIXES):
        return "That is a local or internal host name, not a public site; refused."
    return None


def _element(page: PageView, ref: object) -> tuple[Element | None, str | None]:
    if not isinstance(ref, str) or not _REF_SHAPE.match(ref.strip()):
        return None, "Pass the element's ref exactly as shown on the latest page, e.g. e46."
    element = page.elements.get(ref.strip())
    if element is None:
        return None, (
            f"There is no actionable element with ref {ref.strip()} on the current page. Refs"
            " change when the page changes — use one from the latest page."
        )
    return element, None


def check_value(text: str) -> str | None:
    """Why a value may not be typed, or None: too long, or shaped like personal data."""
    if len(text) > MAX_TYPED_CHARS:
        return "That is too much text for a search or filter field."
    if _EMAIL.search(text):
        return "Typing an email address is refused."
    if sum(c.isdigit() for c in text) > _MAX_DIGITS:
        return "Typing a long number (a phone, card or account number) is refused."
    return None


def _field_allowed(element: Element) -> bool:
    if _DENIED_FIELD.search(element.name):
        return False
    return (
        element.role == "searchbox"
        or element.in_search
        or bool(_ALLOWED_FIELD.search(element.name))
    )


def check_click(page: PageView, ref: object) -> tuple[Element | None, str | None]:
    element, problem = _element(page, ref)
    if element is None:
        return None, problem
    if element.role in {"button", "menuitem"} and _COMMIT_BUTTON.search(element.name):
        return None, (
            f'Clicking "{element.name}" is refused: it would buy, book, sign in, send or submit'
            " something, and this browser only reads."
        )
    return element, None


_TYPE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
_SELECT_ROLES = frozenset({"combobox", "listbox"})


def check_type(page: PageView, ref: object, text: object) -> tuple[Element | None, str | None]:
    element, problem = _element(page, ref)
    if element is None:
        return None, problem
    if not isinstance(text, str) or not text.strip():
        return None, "type_text needs the text to type."
    if element.role not in _TYPE_ROLES:
        return None, f"Element {element.ref} is a {element.role}, not a text field."
    if not _field_allowed(element):
        label = f'"{element.name}"' if element.name else "an unlabelled field"
        return None, (
            f"Typing into {label} is refused: only search, filter, location and date fields"
            " may be typed into. Click a link or choose from a list instead."
        )
    value_problem = check_value(text)
    if value_problem is not None:
        return None, value_problem
    return element, None


def check_select(page: PageView, ref: object, values: object) -> tuple[Element | None, str | None]:
    element, problem = _element(page, ref)
    if element is None:
        return None, problem
    if element.role not in _SELECT_ROLES:
        return None, f"Element {element.ref} is a {element.role}, not a dropdown; click it instead."
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_SELECT_VALUES
        or not all(isinstance(v, str) and v.strip() for v in values)
    ):
        return None, f"select_option needs 1 to {MAX_SELECT_VALUES} option labels."
    if not _field_allowed(element):
        label = f'"{element.name}"' if element.name else "an unlabelled dropdown"
        return None, (
            f"Choosing in {label} is refused: only location, store, date, sort and filter"
            " dropdowns may be changed."
        )
    for value in values:
        value_problem = check_value(value)
        if value_problem is not None:
            return None, value_problem
    return element, None


def check_key(key: object) -> str | None:
    if not isinstance(key, str) or key not in ALLOWED_KEYS:
        return f"Only these keys may be pressed: {', '.join(sorted(ALLOWED_KEYS))}."
    return None


# --- Verification and quarantine ------------------------------------------------

# The answer is raw facts for jerv to write up, not prose (B1's short-finish fix).
MAX_ANSWER_CHARS = 1_200
# Of an answer's names and other numbers, the share that must be on the page. Times and
# prices are held to all of them: they are the facts a caller acts on, and one invented
# showtime among real ones is still a wrong answer.
MIN_FACT_SHARE = 0.8

# A clock time in any of the spellings a page or a model writes — "7:15PM", "7:15 p.m.",
# "7 pm" — folded to one ("7:15 pm") on BOTH sides, so a respaced time still matches.
_CLOCK = re.compile(r"\b(\d{1,2}(?::\d{2})?)\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
_CLOCK_TOKEN = re.compile(r"\b\d{1,2}(?::\d{2})? [ap]m\b")
# A number standing on its own as a page writes one: a price, a date, a count ("$12.50",
# "10/05", "2,000"). Not a digit inside a word ("F1", "7th"): it would never match whole.
_NUMBER = re.compile(r"(?<!\w)[$€£]?\d+(?:[.,/:-]\d+)*(?!\w)")
# A bare one- or two-digit number is on almost every page (a screen, a rating, a date), so
# finding it proves nothing: it is never evidence for a line. But it must still BE on the page
# — a missing one is an invented number ("12 screens" off a page saying "3 screens"), and sinks
# its line. L0 (2026-10-05) saw correct answers ("11 results." beside the first title, "July 1,
# 1962") marked UNVERIFIED when a line made of small numbers counted as unbacked outright.
_SMALL_INT = re.compile(r"\d{1,2}")
_CURRENCY = "$€£"
_WORD = re.compile(r"[^\W\d_][\w'’-]*")
# Capitalised function words are on every page, so they prove nothing.
_COMMON_WORDS = frozenset({"the", "and", "for", "with", "from", "not", "but", "you", "your"})
_MIN_NAME_CHARS = 3


def _fold(text: str) -> str:
    """Text as the fact check compares it: NFKC, casefolded, single-spaced, times in one
    shape."""
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return _CLOCK.sub(r"\1 \2m", text)


def _is_strict(token: str) -> bool:
    """A time or a price: a fact that must be on the page exactly, every one of them."""
    return bool(_CLOCK_TOKEN.fullmatch(token)) or token[0] in _CURRENCY


def _line_tokens(line: str) -> tuple[list[str], list[str]]:
    """The line's salient tokens, and its bare small numbers apart (see `_SMALL_INT`)."""
    folded = _fold(line)
    tokens = _CLOCK_TOKEN.findall(folded)
    rest = _CLOCK_TOKEN.sub(" ", folded)
    smalls: list[str] = []
    for number in _NUMBER.findall(rest):
        number = number.rstrip(".,/:-")
        (smalls if _SMALL_INT.fullmatch(number) else tokens).append(number)
    for word in _WORD.findall(_CLOCK.sub(" ", unicodedata.normalize("NFKC", line))):
        word = word.strip("'’-")
        if (
            len(word) >= _MIN_NAME_CHARS
            and word[0].isupper()
            and word.casefold() not in _COMMON_WORDS
        ):
            tokens.append(word.casefold())
    return tokens, smalls


def salient_tokens(line: str) -> list[str]:
    """What in one answer line can be checked against the page: its times, its prices and
    other numbers (not a bare one- or two-digit one) and its capitalised words (a film, a
    place, a month) — the parts a model gets wrong when it invents or misreads an answer.
    Folded like the page text."""
    return _line_tokens(line)[0]


@dataclass(frozen=True)
class FactCheck:
    """How much of an answer the host found on the final page."""

    # Names and other numbers: most must be found.
    found: int = 0
    total: int = 0
    # Times and prices: every one must be found.
    strict_found: int = 0
    strict_total: int = 0
    # Lines nothing found backs (an invented line), or that state a small number the page
    # does not have.
    lines_missed: int = 0

    @property
    def verified(self) -> bool:
        return (
            self.total + self.strict_total > 0
            and self.lines_missed == 0
            and self.strict_found == self.strict_total
            and self.found >= MIN_FACT_SHARE * self.total
        )

    def describe(self) -> str:
        if self.total + self.strict_total == 0:
            return "UNVERIFIED: nothing in the answer could be checked against the page"
        head = "verified" if self.verified else "UNVERIFIED"
        missed = f"; {self.lines_missed} line(s) unbacked" if self.lines_missed else ""
        return (
            f"{head}: {self.strict_found} of {self.strict_total} times and prices,"
            f" {self.found} of {self.total} names and numbers on the page{missed}"
        )


def facts_on_page(answer: str, page: PageView) -> FactCheck:
    """Whether an answer was read off the page it claims to come from — success is taken
    from the page, never from the model's word. Each line's salient tokens are looked up, as
    whole tokens, in the page's FULL text (not the capped view): every time and price must be
    there, most of the rest, and every line with something checkable must be backed by at
    least one, so one invented line or showtime fails the answer. A bare small number is
    never evidence, but must be on the page too: one that is not sinks its line. A line of
    nothing but small numbers that are all there ("11 results.") neither backs nor sinks it."""
    text = _fold(page.text)
    found = total = strict_found = strict_total = missed = 0
    for line in answer.splitlines():
        tokens, smalls = _line_tokens(line)
        invented = any(not _small_on_page(n, text) for n in smalls)
        if not tokens:
            missed += invented
            continue
        hits = 0
        for token in tokens:
            hit = _on_page(token, text)
            hits += hit
            if _is_strict(token):
                strict_found += hit
                strict_total += 1
            else:
                found += hit
                total += 1
        missed += hits == 0 or invented
    return FactCheck(
        found=found,
        total=total,
        strict_found=strict_found,
        strict_total=strict_total,
        lines_missed=missed,
    )


# Capitalised words that start a goal's sentences or name the task, not a place or item.
_GOAL_STOPWORDS = frozenset(
    {
        "on", "in", "at", "the", "from", "find", "list", "report", "what", "when", "where",
        "which", "who", "how", "go", "open", "pick", "choose", "select", "show", "get", "tell",
        "give", "read", "look", "search", "check", "and", "for", "with", "today", "tonight",
        "tomorrow", "please", "this", "that", "page", "site", "first", "latest", "all", "any",
        "use", "visit", "then", "its", "their", "near",
    }
)  # fmt: skip


def goal_names(goal: str) -> list[str]:
    """The places and items a goal names: its capitalised words, less the words that only
    start a sentence or name the task. Folded like the page text."""
    out: list[str] = []
    for word in _WORD.findall(unicodedata.normalize("NFKC", goal)):
        word = word.strip("'’-")
        folded = word.casefold()
        if len(word) >= _MIN_NAME_CHARS and word[0].isupper() and folded not in _GOAL_STOPWORDS:
            out.append(folded)
    return list(dict.fromkeys(out))


def goal_names_on_page(goal: str, page: PageView) -> bool:
    """Whether every place and item the goal names is on the page — before an answer read
    off it may stand for the goal (a chain's home page showing another location's times
    would otherwise verify). A goal that names none passes."""
    text = _fold(page.text)
    return all(_on_page(name, text) for name in goal_names(goal))


def _on_page(token: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text) is not None


def _small_on_page(number: str, text: str) -> bool:
    """A small number standing alone on the page, not a piece of a bigger one: the "7" of
    "screen 7" is not backed by "7:15" or "7/10"."""
    pattern = rf"(?<!\w)(?<!\d[.,/:-]){re.escape(number)}(?!\w)(?![.,/:-]\d)"
    return re.search(pattern, text) is not None


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_HTML_TAG = re.compile(r"<[^>\n]{0,500}>")
_SCHEME_URL = re.compile(r"\b(?:https?|ftp|data|javascript|file|mailto|blob):\S+", re.IGNORECASE)
_WWW = re.compile(r"\bwww\.\S+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Invisible characters that are not Unicode format (Cf) characters but still hide text:
# variation selectors and their supplement (emoji steganography). Every Cf character —
# zero-width, bidi, word joiners, the BOM, the Arabic letter mark, the Mongolian vowel
# separator, the Tag block used for ASCII smuggling — is caught by category instead.
_INVISIBLE_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def _visible(c: str) -> bool:
    if unicodedata.category(c) == "Cf" or _CONTROL.match(c):
        return False
    point = ord(c)
    return not any(lo <= point <= hi for lo, hi in _INVISIBLE_RANGES)


def strip_invisible(text: str) -> str:
    """`text` without control, format or other invisible characters, and NFKC-folded, so a
    fullwidth `＜` or a ligature reads as the plain character a later check looks for."""
    return "".join(c for c in unicodedata.normalize("NFKC", text) if _visible(c))


def quarantine(text: str, *, cap: int = MAX_ANSWER_CHARS) -> str:
    """Reduce the sub-agent's answer to inert plain text: no markdown images or links, no
    markup, no addresses, no control, bidi or other invisible characters, bounded length. The
    URLs jerv may cite come from the pages the HOST saw, never from this text. Invisibles go
    FIRST, so `ht<ZWSP>tp://` cannot slip past the address check and close up afterwards."""
    text = strip_invisible(text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_REF_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _SCHEME_URL.sub("[link removed]", text)
    text = _WWW.sub("[link removed]", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > cap:
        text = text[: cap - 1].rstrip() + "…"
    return text


def clean_source(url: str) -> str | None:
    """A visited page's URL as a citation: http(s) only, credentials and fragment dropped."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


MAX_SOURCE_CHARS = 500


def safe_url(url: str) -> str | None:
    """A host-observed URL fit to show jerv: `clean_source`, and nothing in it that could
    break a line or hide text — a page controls its own URL, path and query included."""
    cleaned = clean_source(url)
    if cleaned is None or len(cleaned) > MAX_SOURCE_CHARS:
        return None
    if any(c.isspace() or not c.isprintable() or not _visible(c) for c in cleaned):
        return None
    return cleaned


def sources_from(urls: Sequence[str], *, limit: int = 8) -> tuple[str, ...]:
    seen: list[str] = []
    for url in urls:
        cleaned = safe_url(url)
        if cleaned and cleaned not in seen and check_url(cleaned) is None:
            seen.append(cleaned)
    return tuple(seen[-limit:])
