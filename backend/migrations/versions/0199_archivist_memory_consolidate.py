"""Fold the archivist's orphaned memory rows onto the ACTIVE owner principal.

`app.archivist_memory` is keyed by principal id while its RLS gate is `app.is_owner()`
— the ROLE, not the row. Rotating the owner key (`jbrain reset-owner-key`) revokes the
owner principal and mints a new one, so every rotation left the archivist's working
notes filed under a principal nothing addresses any more. On the owner's box a
2026-06-27 taxonomy document sat unreachable while the persona reported an empty memory
and then apologized for destroying notes it had never touched.

`ArchivistMemoryRepo.read` now carries the newest document forward when the current
principal has no row of its own, which covers every FUTURE rotation. It cannot repair a
box where the new principal has ALREADY written a row, because then there is nothing to
fall back from — which is exactly the state the owner's box is in. This migration is the
one-time repair: fold the superseded documents into the active owner's row so the agent
reads one document and nothing stays stranded.

Three deliberate choices:

- **The TRIAGE CLARIFICATIONS markers are stripped from folded-in text.** The hourly
  sweep reads the FIRST such section (`gmail/triage.py`), and a stale correction set
  resurfacing as the live one is precisely the misfiling this area exists to prevent.
  The current document's section stays the only one.
- **Nothing is deleted.** The superseded rows are left exactly as they are, so a bad
  fold costs nothing — the originals are still addressable by principal id. They are
  inert: the read path only reaches them when the current principal has no row.
- **The result is capped at the tool's own limit.** `archivist_memory_write` refuses
  content over 20k chars, so a fold that blew past it would hand the agent a document it
  could never save back. Blocks go in newest-first while they fit; anything left over
  stays in its own row and the document says so.

Revision ID: 0199
Revises: 0198
Create Date: 2026-09-10
"""

import re
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0199"
down_revision = "0198"
branch_labels = None
depends_on = None

# `archivist_memory_write`'s ceiling. The folded document must stay saveable by the agent.
_MAX_CHARS = 20_000

# The sweep's own marker pair, inlined rather than imported: a migration is a historical
# artifact and must not change meaning when `gmail/triage.py` is edited later.
_CLARIFICATIONS_RE = re.compile(
    r"===\s*TRIAGE CLARIFICATIONS.*?===\s*\n.*?\n?===\s*END TRIAGE CLARIFICATIONS\s*===",
    re.IGNORECASE | re.DOTALL,
)


def _fold_block(content: str, updated_at: datetime) -> str:
    """One superseded document, framed so the agent can tell recovered notes from current
    ones, with any stale corrections section removed."""
    body = _CLARIFICATIONS_RE.sub("", content).strip()
    return (
        f"=== RECOVERED NOTES (saved {updated_at.date().isoformat()},"
        " under a previous owner key) ===\n"
        f"{body}\n"
        "=== END RECOVERED NOTES ==="
    )


def _consolidate(active: str, rows: Sequence[tuple[str, str, datetime]]) -> str | None:
    """The folded document to store for `active`, or None when there is nothing to do.

    `rows` is every `(principal_id, content, updated_at)` in the table, any order. The
    active principal's own document leads — it is the current one, and the sweep reads the
    FIRST corrections section — and each superseded document follows newest-first."""
    docs = sorted((r for r in rows if r[1].strip()), key=lambda r: r[2], reverse=True)
    superseded = [d for d in docs if d[0] != active]
    if not superseded:
        return None  # the common case: one owner, one row, nothing stranded

    current = next((content for pid, content, _ in docs if pid == active), "")
    head = current.strip()
    if not head:
        # No current document. The read fallback would already serve the newest row; fold
        # it in anyway so the active principal ends up owning a real document.
        head = _CLARIFICATIONS_RE.sub("", superseded[0][1]).strip()
        superseded = superseded[1:]

    merged, dropped = head, 0
    for _, content, updated_at in superseded:
        block = _fold_block(content, updated_at)
        if block in merged:
            continue  # idempotence: a document already folded in is not folded again
        if len(merged) + len(block) + 2 > _MAX_CHARS:
            dropped += 1
            continue
        merged = f"{merged}\n\n{block}"
    if dropped:
        merged += (
            f"\n\n(Note: {dropped} older memory document(s) did not fit and were left in"
            " app.archivist_memory under their original principal id.)"
        )
    return merged if merged != current else None


def upgrade() -> None:
    conn = op.get_bind()
    active = conn.execute(
        sa.text(
            "SELECT id::text FROM app.principals"
            " WHERE kind = 'owner' AND revoked_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1"
        )
    ).scalar()
    if active is None:
        return  # no owner yet (a fresh database) — nothing to consolidate

    rows = conn.execute(
        sa.text("SELECT principal_id, content, updated_at FROM app.archivist_memory")
    ).all()
    merged = _consolidate(active, [(r[0], r[1], r[2]) for r in rows])
    if merged is None:
        return
    conn.execute(
        sa.text(
            "INSERT INTO app.archivist_memory (principal_id, content, updated_at)"
            " VALUES (:pid, :content, now())"
            " ON CONFLICT (principal_id) DO UPDATE"
            " SET content = EXCLUDED.content, updated_at = now()"
        ),
        {"pid": active, "content": merged},
    )


def downgrade() -> None:
    # No-op on purpose. The fold overwrites the active owner's document, and the
    # pre-fold text is not recoverable from the merged one. Nothing is lost by not
    # undoing it: the superseded rows this read from were never modified, so the
    # originals remain exactly where they were.
    pass
