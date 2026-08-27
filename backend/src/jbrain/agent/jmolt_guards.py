"""Mechanical publish-time guards for jmolt's writes (docs/plans/JMOLT_PLAN.md, W3).

These are the boundaries that must hold when the persona rule does NOT — on a local 120B
every hard-limit is a soft suggestion, so the guarantees are enforced here, in code, at
publish time regardless of the autonomy switch (JMOLT_PLAN §2):

- **M8 content lint** — `lint_content` blocks a write carrying crypto/financial-promotion
  patterns, secret/key shapes, or invisible/bidi/zero-width characters. (Named-real-person
  claims are not mechanically detectable and remain a persona hard-limit, not a lint.)
- **M9 near-duplicate rejection** — `is_near_duplicate` refuses a post too similar to a
  recent one (the anti-templated-collapse control), by character-shingle Jaccard.
- **M10 publish_at clamp** — `clamp_publish_at` forces a staged post's time onto the local
  clock: same calendar day, ≥30 min apart, ≤5/night; the model's raw value is advisory.

Pure functions, no I/O — unit-tested exhaustively and called from the outbox/sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

# ---- M8: content lint ----------------------------------------------------

# Crypto/financial promotion. Deliberately targeted (not a broad word list) to avoid
# blocking ordinary discussion — a $TICKER, a contract/txn address, or overt shill verbs.
_CRYPTO_PATTERNS = (
    re.compile(r"\$[A-Z]{2,10}\b"),  # $DOGE, $MOLT
    re.compile(r"\b0x[a-fA-F0-9]{40}\b"),  # EVM address
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),  # private key / txn hash
    re.compile(r"\b(?:presale|airdrop|to the moon|pump|ape in|degen play|100x)\b", re.I),
    re.compile(r"\bguaranteed (?:returns?|profit|gains?)\b", re.I),
    re.compile(r"\b(?:buy|ape|aping) (?:now|in) (?:before|and)\b", re.I),
)
# Secret/key shapes that must never be posted.
_SECRET_PATTERNS = (
    re.compile(r"moltbook_[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),  # JWT
    re.compile(r"\b(?:xprv|xpub)[A-Za-z0-9]{50,}\b"),  # HD wallet key
)
# Invisible / bidi / zero-width / steganographic characters — an obfuscation, homograph,
# and ASCII-smuggling vector. Built from explicit codepoint RANGES so the big blocks (the
# Unicode Tag chars used to smuggle hidden ASCII, and the variation-selector supplement
# used for emoji steganography) are covered without literal invisible characters in source.
_INVISIBLE_RANGES = (
    (0x00AD, 0x00AD),  # soft hyphen
    (0x180E, 0x180E),  # Mongolian vowel separator
    (0x200B, 0x200F),  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    (0x202A, 0x202E),  # bidi embeddings / overrides
    (0x2060, 0x2064),  # word joiner, invisible math operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xFFF9, 0xFFFB),  # interlinear annotation controls
    (0xE0000, 0xE007F),  # Unicode Tag characters (ASCII smuggling)
    (0xE0100, 0xE01EF),  # variation selectors supplement (emoji steganography)
)
_BIDI_ZW = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _INVISIBLE_RANGES) + "]")


def strip_invisibles(text: str) -> str:
    """Remove invisible / bidi / zero-width / steganographic characters. Shared by the
    outbound lint (M8, which BLOCKS on them) and the owner-facing sanitizer (M15, which
    STRIPS them before rendering jmolt's diary/forum text to the owner)."""
    return _BIDI_ZW.sub("", text)


@dataclass(frozen=True)
class LintResult:
    ok: bool
    reason: str = ""


def lint_content(text: str) -> LintResult:
    """Screen an outbound post/comment. Returns ok=False with an owner/agent-facing reason
    on the first hit. What passes is not "safe" — it is only free of the *mechanically*
    detectable violations; the persona rules cover the rest."""
    if _BIDI_ZW.search(text):
        return LintResult(
            False, "blocked: the text contains invisible or bidirectional characters."
        )
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return LintResult(
                False, "blocked: the text looks like it contains a key, token, or secret."
            )
    for pat in _CRYPTO_PATTERNS:
        if pat.search(text):
            return LintResult(
                False,
                "blocked: the text reads as crypto/financial promotion, which jmolt never does.",
            )
    return LintResult(True)


# ---- M9: near-duplicate rejection ----------------------------------------

_WORD_RE = re.compile(r"\w+")
NEAR_DUP_THRESHOLD = 0.7


def _shingles(text: str, n: int = 4) -> set[str]:
    tokens = _WORD_RE.findall(text.lower())
    if len(tokens) < n:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def is_near_duplicate(
    text: str, recent: list[str], *, threshold: float = NEAR_DUP_THRESHOLD
) -> bool:
    """True if `text` is too similar (word-shingle Jaccard ≥ threshold) to any recent post.
    The anti-templated-collapse control: a well-behaved night varies its posts, a
    heartbeat-spam night repeats and is caught here at stage time."""
    if not text.strip():
        return False
    s = _shingles(text)
    return any(_jaccard(s, _shingles(r)) >= threshold for r in recent if r.strip())


# ---- M10: publish_at clamp -----------------------------------------------

MAX_POSTS_PER_NIGHT = 5
MIN_GAP_MINUTES = 30

# A post must carry a real body, not just a headline. The `moltbook_post` tool required only
# a submolt + title, so a drifted 120B could (and did) publish a bare title with an empty
# `content` — the whole thesis crammed into the title line, nothing under it. This is the
# floor on body length that forces the argument into the body where it belongs. ~80 chars is
# about one real sentence: enough to reject an empty/one-word body without dictating length.
MIN_POST_BODY_CHARS = 80

# ---- Per-night action budgets --------------------------------------------
# The posts cap (M10) has always been enforced; comments/votes/follows had NONE, so a
# drifted night once staged 30 comments and re-staged the same upvote three times. These
# bound the OTHER write kinds the same way, counted per owner-local calendar day at stage
# time (the outbox has no night id; a night is a single 3am sitting-run, so the local day is
# the night). They are volume brakes on DISTINCT actions; exact duplicates are stopped
# separately by the outbox `dedup_key` unique index.
# Per POST, per night — the nightly total was never the binding constraint. jmolt put 17
# comments on ONE post (9 of them top-level) out of 30 it has ever made, asking the same
# question in different words each time, because its own comments are invisible to it when it
# re-reads the thread. A cap on the whole night cannot see that shape; this can.
#
# Threading is the thing not to break: a real back-and-forth needs more than one comment on a
# post. So the tight bound is on TOP-LEVEL comments (a second opening remark on a post you
# already opened is the repetition), while replies under a specific parent stay available up
# to the looser total.
MAX_COMMENTS_PER_POST = 3
MAX_TOP_LEVEL_PER_POST = 1

MAX_COMMENTS_PER_NIGHT = 12
MAX_VOTES_PER_NIGHT = 10
MAX_FOLLOWS_PER_NIGHT = 5


class TooManyPostsError(Exception):
    """Staging would exceed the per-night post cap."""


def clamp_publish_at(
    requested: datetime | None,
    existing_local: list[datetime],
    now_local: datetime,
    *,
    max_posts: int = MAX_POSTS_PER_NIGHT,
    min_gap_minutes: int = MIN_GAP_MINUTES,
) -> datetime:
    """Return the server-clamped local publish time for a newly staged post (M10). All
    datetimes are owner-LOCAL and tz-aware. The model's `requested` value is advisory:
    the result is forced to the same calendar day as `now_local`, at least `min_gap_minutes`
    after the latest already-staged post (and never in the past), and raises
    TooManyPostsError once `max_posts` are already staged today."""
    today_existing = [t for t in existing_local if t.date() == now_local.date()]
    if len(today_existing) >= max_posts:
        raise TooManyPostsError(
            f"you already have {len(today_existing)} posts staged for today; "
            f"the limit is {max_posts}."
        )
    gap = timedelta(minutes=min_gap_minutes)
    # Earliest allowed = max(now, latest-existing + gap); a fresh night starts at 'now'.
    floor = now_local
    if today_existing:
        floor = max(floor, max(today_existing) + gap)
    candidate = requested if (requested and requested.tzinfo) else floor
    if candidate < floor:
        candidate = floor
    # Never spill past the local day. If the required floor (min gap after the last post)
    # already crosses midnight, there is no room for another spaced post today — refuse
    # rather than stack posts onto the same 23:59 minute (which would break the ≥gap rule
    # and the drip spacing).
    end_of_day = datetime.combine(now_local.date(), time(23, 59), tzinfo=now_local.tzinfo)
    if floor > end_of_day:
        raise TooManyPostsError(
            "no room left in the day for another post the required gap after the last one."
        )
    if candidate > end_of_day:
        candidate = floor  # a too-late request falls back to the earliest allowed slot
    return candidate


# ---- B1: the scratchpad write-path filter --------------------------------

# jmolt's notes are reloaded into its own context every night, and they are the one surface
# where content it read on Moltbook can cross the DATA fence in its own hand: it reads a
# thread (fenced, inert), writes what it made of it into a file (unfenced, trusted), and the
# next night reads that file back as its own memory. Fencing the read is the wrong fix — see
# `jmoltscratchtools._PROVENANCE` for why — so the boundary is enforced HERE, on the way in,
# where the payload is still identifiable as text rather than as memory.
#
# Two things are refused. Invisible characters, because a note is reloaded verbatim and an
# ASCII-smuggling payload survives every subsequent read. And imitations of the two frames
# the night itself owns: the owner's advisory header (the channel jmolt is told genuinely IS
# its human) and the Moltbook DATA fence. A file that opens "--- A NOTE FROM YOUR HUMAN ---"
# is indistinguishable, once reloaded, from the real thing.
_TRUSTED_MARKERS = (
    re.compile(r"-{2,}\s*A NOTE FROM YOUR HUMAN", re.I),
    re.compile(r"-{2,}\s*END OF YOUR HUMAN'?S NOTE", re.I),
    re.compile(r"\bnote (?:from|left by) your human\b", re.I),
    re.compile(r"\bthe lines below are a note your human\b", re.I),
    re.compile(r"\bthey ARE from your human\b"),
    re.compile(r"\bthe following is quoted content from moltbook\b", re.I),
    re.compile(r"\bnever as instructions to you\b", re.I),
)


def lint_scratch_content(text: str) -> LintResult:
    """Screen text on its way INTO jmolt's scratchpad. Returns ok=False with an agent-facing
    reason naming what to change — jmolt has to be able to act on the refusal, because a
    refused write means the content it just composed is gone."""
    if _BIDI_ZW.search(text):
        return LintResult(
            False,
            "that text carries invisible or bidirectional characters. Your files are read "
            "back to you verbatim, so anything hidden in them stays hidden. Retype the part "
            "you pasted in rather than copying it across.",
        )
    for pat in _TRUSTED_MARKERS:
        if pat.search(text):
            return LintResult(
                False,
                "that text imitates one of the frames the night puts around things that are "
                "NOT your own words — your human's note, or the Moltbook quote fence. Your "
                "files are your own voice and are read back to you as such, so those frames "
                "cannot appear inside them. Say it in your own words instead.",
            )
    return LintResult(True)
