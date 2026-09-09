"""Which note bodies someone other than the owner wrote — and so which conversations
run on the third-party tool set (D10, plan risk 1).

W4's intake half. The port itself needs no wiring: `note.ingested` fires on every
settled ingest whatever the provenance (`ingest/pipeline.py`), so the `untrusted_origin`
note an approved intake submission enacts into (`agent/proposaltools.intake_note_executor`)
has ALREADY been opening a note conversation since W2, and W3 handed that conversation
`assert_fact`. The thing D10 actually owes is the DIFFERENCE — the conversation knowing
whose words it is reading — and this module is where that question is asked.

**The predicate is `notes.provenance`, deliberately not a new column or table.** 0111
already admits `untrusted_origin` under the `notes_provenance_check`, the value is
already set by the only path that turns a stranger's submission into a note, and
`queue.INTEGRATION_BACKFILL_ORDER_BY` already reads it for the same reason (drain trusted
notes first). A second marker would be a second thing to keep true, and the failure mode
of the two disagreeing is a stranger's note running on the owner's tool set.

What is NOT here, and why the name is `third_party` rather than `untrusted`:

- `human` / `agent` / `owner_correction` are all owner-sourced. `agent` is an enacted
  Proposal, which the owner approved as his own words.
- An owner-authored note may still QUOTE a stranger (a forwarded email, a photo of a
  letter). That is plan risk 1's other half, and the answer to it is the nonce-closed
  DATA frame every note body gets (`analysis/noteframe.py`), not this set. The set is
  for the narrower, checkable question "did the owner write this body at all", because
  that is the one a column can answer.
- EMR import (D9) is a sibling task's; whether a decrypted record counts as third-party
  is its call to make, and it joins the set here if it decides so.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import NoteConversationRepo
from jbrain.notes.service import NotesRepo

log = structlog.get_logger()

THIRD_PARTY_PROVENANCE: frozenset[str] = frozenset({"untrusted_origin"})
"""`notes.provenance` values meaning "the owner did not write this body"."""


def is_third_party(provenance: str | None) -> bool:
    """Whether a note's body is somebody else's words.

    `None` and an unrecognised value both read as third-party. A provenance this code
    does not know is a provenance added after it, and the safe reading of an unknown
    origin is that it is not the owner's."""
    return provenance not in {"human", "agent", "owner_correction"}


async def conversation_is_third_party(
    maker: async_sessionmaker[AsyncSession],
    notes: NotesRepo,
    ctx: SessionContext,
    *,
    session_id: str,
) -> bool:
    """Whether the note behind a note conversation is third-party-bodied.

    The `/chat` half of the question: a reply turn knows a session, never a note, so the
    conversation row is the hop. Two round trips on a turn that already makes both
    (`clarify.record_owner_reply` opens the same two), and only on the note persona.

    FAILS CLOSED, at every step: no conversation row, no note, a soft-deleted note, or a
    raised exception all answer True. That is the direction that matters — the answer
    gates a NARROWING, so a failure that says True costs the owner `correct_fact` on one
    reply turn, and a failure that said False would hand a stranger's body a turn holding
    `prefs_write`."""
    try:
        async with scoped_session(maker, ctx) as s:
            conversation = await NoteConversationRepo().get(s, session_id)
        if conversation is None:
            log.warning("note_reply.no_conversation_row", session_id=session_id)
            return True
        note = await notes.get_note(ctx, str(conversation.note_id))
        if note is None:
            log.warning("note_reply.note_gone_for_provenance", session_id=session_id)
            return True
        return is_third_party(note.provenance)
    except Exception as exc:  # noqa: BLE001 — an unreadable origin is a third-party origin
        log.warning("note_reply.provenance_unreadable", session_id=session_id, error=repr(exc))
        return True


__all__ = [
    "THIRD_PARTY_PROVENANCE",
    "conversation_is_third_party",
    "is_third_party",
]
