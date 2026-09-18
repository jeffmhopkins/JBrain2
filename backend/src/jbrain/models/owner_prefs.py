"""`owner_prefs` — the owner's standing instructions, ORM + repo + the document shape
(migration 0195, docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md D15).

One owner-only row per principal holding a single capped document ("how to handle
recipe notes", "stop splitting ingredients"), injected ahead of the note into every
note conversation's prompt. Owner-only RLS (`app.is_owner()`, ENABLE + FORCE) is the
firewall, exactly as for `archivist_memory` — these methods take the caller's
already-scoped `AsyncSession`.

**A rule is one line.** The document is not free Markdown: `content` is the rules
joined by newlines, and the numbering the model addresses (`prefs_write`) is computed
at render time, never stored. That is deliberate. Numbering stored in the text would
let a single edit renumber every rule after it, and a rule allowed to contain a newline
could draw extra numbered lines that the model — and the owner reading the Proposal —
would read as separate standing instructions. Normalizing every rule to one line makes
"what the owner approved is one rule" true of the storage, not of the prose.

The caps live here for the same reason: they are properties of the document, and every
caller checks them on an in-memory `list[str]` BEFORE it opens a write. Nothing in this
module raises on an over-cap edit; `apply_op` returns the refusal as text.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base

# The caps. Deliberately far under `archivist_memory`'s 20k, for a reason that does not
# apply there: the archivist's document is read by ONE tool call in a session the owner
# is watching, while this one is prepended to the system prompt of EVERY note
# conversation — one `agent.turn` per settled ingest, on a serial GPU (plan risk 4). At
# 8k chars the standing instructions cost roughly 2k tokens on every note forever; at
# 20k they would cost ~5k and start crowding the note they are meant to be read
# against. 8k is not tight either: 50 rules averaging 160 characters is already a very
# large standing-order book for "how to handle my notes".
MAX_RULES = 50
MAX_RULE_CHARS = 1_000
MAX_DOC_CHARS = 8_000

OPS = ("add", "replace", "remove")
"""The delta ops. Named here and in the sidecar's prose, never as a JSON-Schema `enum`
— an enum in a `.tool` sidecar segfaults gpt-oss's harmony grammar (plan constraint 8)."""

_WS = re.compile(r"\s+")


def normalize_rule(text: str) -> str:
    """One rule, one line, no runs of whitespace.

    A model told to "write it as bullets" writes newlines; collapsing them is friendlier
    than refusing, and it is safe to do silently because the owner approves the
    NORMALIZED text — the Proposal preview is built from this, so what is shown is what
    lands."""
    return _WS.sub(" ", text).strip()


def parse_rules(content: str) -> list[str]:
    """The stored document as its rules. Blank lines are not rules."""
    return [r for line in content.splitlines() if (r := normalize_rule(line))]


def render_document(rules: list[str]) -> str:
    """The storage form — the inverse of `parse_rules`."""
    return "\n".join(rules)


def render_numbered(rules: list[str]) -> str:
    """The form the model sees: `1. …`, numbered at render time.

    This is the addressing vocabulary `prefs_write.rule_number` uses, so it must be
    generated from the same list the ops apply to and never stored alongside it."""
    return "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))


def apply_op(
    rules: list[str], *, op: str, rule_number: int, text: str
) -> tuple[list[str] | None, str]:
    """Apply ONE delta op to a rule list, purely.

    Returns `(new_rules, "")` or `(None, refusal)` — it never raises and never touches a
    database, so both callers (the staging handler and the enact executor) check the
    caps on the result while there is still no write to abort.

    `text` is load-bearing on all three ops, which is why `remove` takes it too: on
    `replace`/`remove` it is matched against the rule actually sitting at
    `rule_number`, so an edit staged against one numbering can never land on a different
    rule after the document moved under it."""
    rule = normalize_rule(text)
    if op not in OPS:
        return None, f"'{op}' isn't an op — use add, replace or remove."
    if not rule:
        return None, "text is required — the rule to add, the new wording, or the rule to remove."
    if len(rule) > MAX_RULE_CHARS:
        return None, (
            f"that rule is {len(rule)} characters, over the {MAX_RULE_CHARS} limit for one"
            " rule. A standing instruction is a sentence or two — split it or shorten it."
        )
    out = list(rules)
    if op == "add":
        # 0 (or anything past the end) appends; a real number inserts BEFORE that rule,
        # so "put this ahead of rule 2" is expressible without a second field.
        at = len(out) if rule_number <= 0 or rule_number > len(out) else rule_number - 1
        if rule in out:
            return None, f"that rule is already there, as rule {out.index(rule) + 1}."
        out.insert(at, rule)
    elif not 1 <= rule_number <= len(out):
        return None, (
            f"there is no rule {rule_number} — the list has {len(out)} rule(s)."
            if out
            else f"there are no standing instructions yet, so there is nothing to {op}. Use add."
        )
    elif op == "remove":
        target = out[rule_number - 1]
        if target.casefold() != rule.casefold():
            return None, (
                f"rule {rule_number} reads “{target}”, not what you passed — repeat the rule"
                " verbatim to remove it."
            )
        out.pop(rule_number - 1)
    else:
        out[rule_number - 1] = rule
    if len(out) > MAX_RULES:
        return None, (
            f"that would make {len(out)} standing instructions, over the {MAX_RULES} limit."
            " Replace or remove one instead of adding another."
        )
    if len(render_document(out)) > MAX_DOC_CHARS:
        return None, (
            f"that would make the standing instructions {len(render_document(out))} characters,"
            f" over the {MAX_DOC_CHARS} limit. Shorten a rule or remove one first."
        )
    return out, ""


class OwnerPrefs(Base):
    """The owner's standing-instruction document for one principal."""

    __tablename__ = "owner_prefs"
    __table_args__ = {"schema": "app"}

    principal_id: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OwnerPrefsRepo:
    """Reads/writes the standing-instruction row on a caller-supplied RLS-scoped
    session. `write_rules` replaces the document because a rule list has no other
    storage form — but nothing model-facing reaches it: the only write path is one
    delta op the owner already approved (`agent/prefstools.py`)."""

    async def read_rules(self, session: AsyncSession, principal_id: str) -> list[str]:
        row = await session.get(OwnerPrefs, principal_id)
        return parse_rules(row.content if row else "")

    async def write_rules(self, session: AsyncSession, principal_id: str, rules: list[str]) -> None:
        content = render_document(rules)
        stmt = (
            pg_insert(OwnerPrefs)
            .values(principal_id=principal_id, content=content, updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[OwnerPrefs.principal_id],
                set_={"content": content, "updated_at": func.now()},
            )
        )
        await session.execute(stmt)
