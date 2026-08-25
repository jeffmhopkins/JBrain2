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
# Invisible / bidi / zero-width characters — an obfuscation and homograph vector.
# Soft hyphen, zero-width spaces/joiners, LRM/RLM, bidi embeddings/overrides/isolates,
# word joiner, invisible-math operators, and the BOM. Built from explicit codepoints.
_BIDI_ZW_CHARS = (
    "­"  # soft hyphen
    "​‌‍‎‏"  # ZWSP, ZWNJ, ZWJ, LRM, RLM
    "‪‫‬‭‮"  # bidi embeddings / overrides
    "⁠⁡⁢⁣⁤"  # word joiner, invisible math operators
    "⁦⁧⁨⁩"  # bidi isolates
    "﻿"  # BOM / zero-width no-break space
)
_BIDI_ZW = re.compile(f"[{_BIDI_ZW_CHARS}]")


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
    # Never spill past the local day; if the floor already crosses midnight, pin to 23:59.
    end_of_day = datetime.combine(now_local.date(), time(23, 59), tzinfo=now_local.tzinfo)
    if candidate > end_of_day:
        candidate = min(floor, end_of_day) if floor <= end_of_day else end_of_day
    return candidate
