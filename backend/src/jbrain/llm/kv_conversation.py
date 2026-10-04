"""Conversation-level disk caching for the interactive slot — the pure half (FLASH_NEXT F4c).

On a pooled model (Flash-Next) slot 0 holds whichever chat conversation spoke last. When the
owner switches to another conversation, the incoming request overwrites everything past the
shared persona + tools, and switching back pays a re-prefill of the whole transcript — tens of
seconds on a long conversation. `KvPrefixStore` saves the slot to disk as the conversation
leaves it and restores it when that conversation speaks again. This module holds the parts
that need no gateway: what names a file, what a file claims, who may have one, and how a
restore is judged.

A file is restored on IDENTITY alone — the same conversation key and the same base identity
(launch line, system, tools, effort, the prefix store's own fingerprint, which is in the file
name) — never on a comparison of message lists. A chat request is not a stable list: its tail
carries volatile blocks (a timestamped `now` block, resume/artifact/plan context, per-turn
hints) and the turn's own tool steps with their replayed thinking, none of which the next
turn resends, so any message-level prefix test failed from turn two. llama-server compares
TOKENS itself after the restore and re-evaluates from the first divergence, reusing up to the
nearest context checkpoint before it, so the restore is never wrong — only more or less useful.
How useful is MEASURED: the first request after a restore reports `cached_tokens`, judged
against the restored count (`judge`); a file that misses `MISS_LIMIT` times in a row is dropped.

No conversation text is stored in the claim — only the key's hash, the base identity, counts.
The slot file itself holds the conversation's token ids, like the KV in RAM it was taken from —
on disk, outside Postgres, where the domain firewalls (health, finance, location) cannot reach
it. So a conversation gets a file ONLY when it cannot hold firewalled data (`cache_allowed`): a
persona that reads no knowledge base and holds no mail tools, in a session scoped to `general`
alone with no subject, in which no location, mail or records tool has ever run. Brain/curator
chats and the archivist never get one. A deleted or re-scoped session's files are removed
(`KvPrefixStore.forget_conversation`); the toggle, the budget and the clear route remove the rest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final, Literal

# Conversation slot files share the per-model `.kvslots` folder with role prefixes; the prefix
# is how the budget tells them apart (role prefixes are evicted last) and how the owner's
# state read labels them.
FILE_PREFIX: Final = "c-"
SLOT_SUFFIX: Final = ".kvslot"
# What a file claims, beside it: base identity, the key's hash, the saved count, its misses.
META_EXT: Final = ".meta"
META_VERSION: Final = 2
# The slot's cache is the request's prompt plus every generated token but the final stop
# token, so a slot still holding the conversation reads between those two bounds. A small
# slack covers a template's trailing tokens; anything else means the slot moved on.
SAVE_SLACK_TOKENS: Final = 8
# Judging a restore by the first request it served: a HIT reused at least half of what was
# restored; a PARTIAL reused at least the store's prefix floor (worth more than the persona
# alone would have been); anything less is a MISS. A file is dropped after this many misses
# in a row — one miss can be a conversation edited further back; three is a pattern.
HIT_FRACTION: Final = 0.5
PARTIAL_FLOOR_TOKENS: Final = 4096
MISS_LIMIT: Final = 3

# Tools whose results are firewalled-domain data (location) or the owner's mail / records:
# a conversation in which any of them ran never reaches disk, and loses any file it had.
# Deliberately broad — a weather or device read is located — because a false exclusion costs
# one re-prefill and a false inclusion puts that data in a file Postgres cannot police.
EXCLUDED_TOOLS: Final = frozenset(
    {
        "current_location",
        "location_history",
        "location_query",
        "nearby_now",
        "find_when_at",
        "geocode_reverse",
        "neighborhood",
        # APRS: decoded position reports, the owner's station's among them.
        "aprs_recent",
        "sdr_aprs_logging",
        "save_place",
        "time_at_place",
        "where_is",
        "where_was_i",
        "weather",
        "weather_history",
        "hurricane",
        "device_status",
        "home_status",
        "read_labs",
        "read_encounters",
        "read_appointment",
        "read_appointments",
        "manage_appointment",
    }
)
EXCLUDED_TOOL_PREFIXES: Final = ("gmail_",)
# Domains whose rows Postgres firewalls; anything else unknown is treated the same way.
_UNFIREWALLED = frozenset({"general"})

Decision = Literal["restore", "no_file", "base_mismatch", "key_mismatch"]
Judgement = Literal["hit", "partial", "miss"]


def excluded_tool(name: str) -> bool:
    return name in EXCLUDED_TOOLS or name.startswith(EXCLUDED_TOOL_PREFIXES)


def any_excluded(names: Iterable[str]) -> bool:
    return any(excluded_tool(n) for n in names)


def cache_allowed(
    *,
    reads_knowledge_base: bool,
    persona_tools: Collection[str] | None,
    domain_scopes: Iterable[str],
    subject_ids: Collection[str],
    tools_ran: Iterable[str],
) -> bool:
    """Whether a chat may have its slot state written to disk. Only when nothing firewalled or
    private can be in it: the persona reads no knowledge base and is not allowed mail tools (a
    wildcard allowlist, None, counts as allowed everything), the session names no domain but
    `general` (an unknown domain counts as firewalled) and no subject, and none of
    `EXCLUDED_TOOLS` has run in it. Conservative by construction: doubt keeps it in RAM."""
    if reads_knowledge_base or subject_ids or persona_tools is None:
        return False
    if any(n.startswith(EXCLUDED_TOOL_PREFIXES) for n in persona_tools):
        return False
    if not all(domain in _UNFIREWALLED for domain in domain_scopes):
        return False
    return not any_excluded(tools_ran)


def key_hash(conversation_key: str) -> str:
    """The key as stored on disk: enough to find a conversation's files, nothing to read."""
    return hashlib.sha256(conversation_key.encode()).hexdigest()[:32]


def file_name(base_fingerprint: str, conversation_key: str) -> str:
    """The slot file for one conversation under one base identity. Hashed, so neither the
    session id nor anything about the conversation is readable off the volume listing."""
    digest = hashlib.sha256(f"{base_fingerprint}\x00{conversation_key}".encode()).hexdigest()
    return f"{FILE_PREFIX}{digest[:32]}{SLOT_SUFFIX}"


def is_conversation_file(name: str) -> bool:
    return name.startswith(FILE_PREFIX) and name.endswith(SLOT_SUFFIX)


def short_key(conversation_key: str) -> str:
    """A label for the owner's state read: stable per conversation, not the id itself."""
    return key_hash(conversation_key)[:8]


@dataclass(frozen=True)
class ConversationMeta:
    base: str
    key: str  # key_hash, never the key itself
    n_tokens: int
    saved_at: float
    misses: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": META_VERSION,
                "base": self.base,
                "key": self.key,
                "n_tokens": self.n_tokens,
                "saved_at": self.saved_at,
                "misses": self.misses,
            }
        )

    @staticmethod
    def from_json(raw: str) -> ConversationMeta | None:
        """None for anything unreadable — a torn write, an older version, junk. A file whose
        claim cannot be read is never restored."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("v") != META_VERSION:
            return None
        base, key, n_tokens = data.get("base"), data.get("key"), data.get("n_tokens")
        saved_at, misses = data.get("saved_at"), data.get("misses", 0)
        if (
            not isinstance(base, str)
            or not isinstance(key, str)
            or not isinstance(n_tokens, int)
            or isinstance(n_tokens, bool)
            or n_tokens <= 0
            or not isinstance(saved_at, int | float)
            or not isinstance(misses, int)
        ):
            return None
        return ConversationMeta(base, key, n_tokens, float(saved_at), misses)


def restore_decision(meta: ConversationMeta | None, base: str, conversation_key: str) -> Decision:
    """Whether a saved conversation may be restored ahead of a request: its claim must name
    this conversation and this base identity. Nothing about the message list is compared —
    see the module docstring."""
    if meta is None:
        return "no_file"
    if meta.key != key_hash(conversation_key):
        return "key_mismatch"
    if meta.base != base:
        return "base_mismatch"
    return "restore"


def judge(cached_tokens: int, restored_tokens: int) -> Judgement:
    if restored_tokens > 0 and cached_tokens >= restored_tokens * HIT_FRACTION:
        return "hit"
    if cached_tokens >= PARTIAL_FLOOR_TOKENS:
        return "partial"
    return "miss"


@dataclass
class ConversationHold:
    """What this process believes the interactive slot holds: the conversation, its base
    identity, the last request's token counts (None after a restore, until the next turn),
    whether the slot changed since the last save, when it last changed (monotonic), and — right
    after a restore — the restored count the next request is judged against."""

    key: str
    base: str
    input_tokens: int | None
    output_tokens: int | None
    dirty: bool
    at: float
    restored_tokens: int | None = None
    # The store's conversation epoch when this claim was made: a clear (the toggle turned off)
    # bumps it, and a claim from before can never be saved.
    epoch: int = 0

    def still_in_slot(self, n_slot_tokens: int) -> bool:
        """Whether a `/slots` count is this conversation's cache: at least the last prompt, at
        most that prompt plus its output. Anything outside means another request ran there."""
        if self.input_tokens is None or self.output_tokens is None:
            return False
        return (
            self.input_tokens
            <= n_slot_tokens
            <= self.input_tokens + self.output_tokens + SAVE_SLACK_TOKENS
        )
