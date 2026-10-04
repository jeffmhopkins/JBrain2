"""Conversation-level disk caching for the interactive slot — the pure half (FLASH_NEXT F4c).

On a pooled model (Flash-Next) slot 0 holds whichever chat conversation spoke last. When the
owner switches to another conversation, the incoming request overwrites everything past the
shared persona + tools, and switching back pays a re-prefill of the whole transcript — tens of
seconds on a long conversation. `KvPrefixStore` saves the slot to disk as the conversation
leaves it and restores it when that conversation speaks again. This module holds the parts
that need no gateway: what names a file, what a file claims to hold, and whether a request
may reuse it.

What a file claims is checked at MESSAGE level, not token level: the saved state is valid for
a request whose first N messages hash exactly like the N messages the saved request sent,
under the same base identity (launch line, system, tools, effort — the prefix store's own
fingerprint, which is in the file name). llama-server then compares tokens itself and
re-evaluates from the first divergence (the previous answer re-rendered without its
thinking, typically), so a wrong guess here costs a re-prefill, never a wrong answer.

No conversation text is stored in the metadata — only digests and counts. The slot file
itself holds the conversation's token ids, like the KV in RAM it was taken from — on disk,
outside Postgres, where the domain firewalls (health, finance, location) cannot reach it. So a
conversation gets a file ONLY when it cannot hold firewalled data at all
(`conversation_cache_allowed`): a persona that does not read the knowledge base, in a session
with no firewalled domain and no subject. A Brain/curator chat never gets one. The budget, the
owner's toggle and `DELETE /api/debug/llm/kv-prefix` bound and remove the rest.

Replayed reasoning (FLASH_NEXT F3b follow-on): within a turn the router sends each tool step's
own thinking back as `reasoning_content`; the next turn's history is text-only, and the
template renders earlier turns with `preserve_thinking=false`. The digests therefore leave the
reasoning fields out — they describe what the NEXT request resends — and llama-server re-reads
from the first token where the re-render differs (the previous turn's first tool step).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from jbrain.llm.types import LlmMessage

# Conversation slot files share the per-model `.kvslots` folder with role prefixes; the prefix
# is how the budget tells them apart (role prefixes are evicted last) and how the owner's
# state read labels them.
FILE_PREFIX: Final = "c-"
SLOT_SUFFIX: Final = ".kvslot"
# What a file holds, beside it: base identity, the message digests and the saved token count.
META_EXT: Final = ".meta"
META_VERSION: Final = 1
# The slot's cache is the request's prompt plus every generated token but the final stop
# token, so a slot still holding the conversation reads between those two bounds. A small
# slack covers a template's trailing tokens; anything else means the slot moved on.
SAVE_SLACK_TOKENS: Final = 8

Decision = Literal["restore", "no_file", "base_mismatch", "prefix_mismatch"]


# Domains whose rows Postgres firewalls; anything else unknown is treated the same way.
_UNFIREWALLED = frozenset({"general"})
# Per-step thinking replayed within a turn only; never part of the next turn's history.
_REPLAY_ONLY_FIELDS = ("reasoning", "reasoning_model")


def conversation_cache_allowed(
    *, reads_knowledge_base: bool, domain_scopes: Sequence[str], subject_ids: Sequence[str]
) -> bool:
    """Whether a chat may have its slot state written to disk. Only when nothing firewalled can
    be in it: the persona reads no knowledge base (jerv, research-type agents — their turns run
    with empty read scopes), the session names no domain but `general` (an unknown domain counts
    as firewalled) and no subject. Conservative by construction: any doubt keeps it in RAM."""
    if reads_knowledge_base or subject_ids:
        return False
    return all(domain in _UNFIREWALLED for domain in domain_scopes)


def _canonical(message: LlmMessage) -> bytes:
    """One message as stable bytes, without the replay-only reasoning fields. Images and their
    base64 payloads are included: a different picture under the same words is a different
    prompt."""
    body = dataclasses.asdict(message)
    for field in _REPLAY_ONLY_FIELDS:
        body.pop(field, None)
    return json.dumps(
        {"type": type(message).__name__, "body": body}, sort_keys=True, default=str
    ).encode()


def message_digests(messages: Sequence[LlmMessage]) -> tuple[str, ...]:
    """A chained digest per message: entry i covers messages 0..i, so comparing the i-th
    entries of two lists compares their whole prefixes."""
    out: list[str] = []
    running = hashlib.sha256()
    for message in messages:
        running.update(_canonical(message))
        running.update(b"\x00")
        out.append(running.copy().hexdigest()[:24])
    return tuple(out)


def file_name(base_fingerprint: str, conversation_key: str) -> str:
    """The slot file for one conversation under one base identity. Hashed, so neither the
    session id nor anything about the conversation is readable off the volume listing."""
    digest = hashlib.sha256(f"{base_fingerprint}\x00{conversation_key}".encode()).hexdigest()
    return f"{FILE_PREFIX}{digest[:32]}{SLOT_SUFFIX}"


def is_conversation_file(name: str) -> bool:
    return name.startswith(FILE_PREFIX) and name.endswith(SLOT_SUFFIX)


def short_key(conversation_key: str) -> str:
    """A label for the owner's state read: stable per conversation, not the id itself."""
    return hashlib.sha256(conversation_key.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class ConversationMeta:
    base: str
    prefix: tuple[str, ...]
    n_tokens: int
    saved_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": META_VERSION,
                "base": self.base,
                "prefix": list(self.prefix),
                "n_tokens": self.n_tokens,
                "saved_at": self.saved_at,
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
        base, prefix, n_tokens = data.get("base"), data.get("prefix"), data.get("n_tokens")
        saved_at = data.get("saved_at")
        if (
            not isinstance(base, str)
            or not isinstance(prefix, list)
            or not all(isinstance(d, str) for d in prefix)
            or not isinstance(n_tokens, int)
            or isinstance(n_tokens, bool)
            or n_tokens <= 0
            or not isinstance(saved_at, int | float)
        ):
            return None
        return ConversationMeta(base, tuple(prefix), n_tokens, float(saved_at))


def restore_decision(meta: ConversationMeta | None, base: str, digests: Sequence[str]) -> Decision:
    """Whether a saved conversation may be restored ahead of a request.

    `restore` only when the file's base identity is the request's and every message the saved
    request sent is, digest for digest, the start of this one. An equal list counts (the same
    request retried after a failure)."""
    if meta is None:
        return "no_file"
    if meta.base != base:
        return "base_mismatch"
    n = len(meta.prefix)
    if n == 0 or n > len(digests) or tuple(digests[:n]) != meta.prefix:
        return "prefix_mismatch"
    return "restore"


@dataclass
class ConversationHold:
    """What this process believes the interactive slot holds: the conversation, its base
    identity, the digests of the last request it ran there, that request's token counts (None
    after a restore, until the next turn), whether the slot changed since the last save, and
    when it last changed (monotonic)."""

    key: str
    base: str
    prefix: tuple[str, ...]
    input_tokens: int | None
    output_tokens: int | None
    dirty: bool
    at: float

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
